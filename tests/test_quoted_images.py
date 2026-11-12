"""引用按本群服务器编号匹配，不能用队列号、昵称或最近图片替代。"""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import json
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from xml.sax.saxutils import escape

from PIL import Image
from wxbot_framework.llm import LLMError
from wxbot_framework.members import MemberStore
from wxbot_framework.personality import PersonalityState
from wxbot_framework.quoted_messages import QuoteError, parse_quote
from wxbot_framework.sdk import Message
from wxbot_framework.runtime import BotApplication
from wxbot_framework.vision import RecentImages, image_data_url
from plugins.mention_chat import Plugin

ROOT = Path(__file__).resolve().parents[1]
ROOM, AUTHOR, ASKER, SELF = '12345678@chatroom', 'wxid_photo_author', 'wxid_asker', 'wxid_bot'
SERVER_ID = '1234567890123456789'


def xml(*, kind='3', identifier=SERVER_ID, origin=ROOM, author=AUTHOR, title='@机器人\u2005这张图什么意思？', extra=''):
    return ('<msg><appmsg><type>57</type><title>' + escape(title) + '</title><refermsg>'
            '<type>' + kind + '</type><svrid>' + identifier + '</svrid>'
            '<fromusr>' + origin + '</fromusr><chatusr>' + author + '</chatusr>'
            '<displayname>不可信的昵称</displayname>'
            '<content>' + escape('<msg><img aeskey="secret-do-not-use" cdnmidimgurl="file:///private" /></msg>')
            + '</content>' + extra + '</refermsg></appmsg></msg>')


class QuoteParserTests(unittest.TestCase):
    def test_extracts_only_question_type_id_and_verified_matching_fields(self):
        title, quote = parse_quote(xml(), ROOM)
        self.assertIn('这张图', title)
        self.assertEqual(quote, dict(status='ready', type='3', message_id=SERVER_ID, room=ROOM, sender_id=AUTHOR))
        self.assertNotIn('secret', str(quote))
        self.assertNotIn('file:', str(quote))

    def test_foreign_room_missing_id_and_overflow_are_unavailable(self):
        for changes in ({'origin': 'other@chatroom'}, {'identifier': '0'},
                        {'identifier': str(2**64)}, {'author': ''}):
            with self.subTest(changes=changes):
                self.assertEqual(parse_quote(xml(**changes), ROOM)[1]['status'], 'unavailable')

    def test_sender_variant_and_cdata_question(self):
        self.assertEqual(parse_quote(xml(origin=AUTHOR, author=''), ROOM)[1]['sender_id'], AUTHOR)
        raw = xml().replace(escape('@机器人\u2005这张图什么意思？'), '<![CDATA[@机器人\u2005比较 <A> 和 B]]>')
        self.assertEqual(parse_quote(raw, ROOM)[0], '@机器人\u2005比较 <A> 和 B')

    def test_no_mention_forged_by_reference_body(self):
        title, quote = parse_quote(xml(title='普通引用，没有艾特', extra='<msgsource>&lt;atuserlist&gt;wxid_bot&lt;/atuserlist&gt;</msgsource>'), ROOM)
        self.assertNotIn('at_members', quote)
        self.assertEqual(title, '普通引用，没有艾特')

    def test_invalid_xml_duplicate_and_entities_fail_closed(self):
        examples = [xml(extra='<svrid>99</svrid>'), '<!DOCTYPE x [<!ENTITY s "secret">]>' + xml(),
                    xml().replace('</title>', '<x /></title>'), xml().replace('</msg>', ''),
                    xml().replace('<refermsg>', '<refermsg>' + '<x>' * 33).replace('</refermsg>', '</x>' * 33 + '</refermsg>')]
        for value in examples:
            with self.subTest(value=value[:50]), self.assertRaises(QuoteError):
                parse_quote(value, ROOM)

    def test_ordinary_files_and_large_forwarded_are_ignored(self):
        for value in ['<msg><appmsg><type>6</type><title>x.txt</title></appmsg></msg>',
                      '<msg><appmsg><type>19</type><recorditem>' + 'a' * 100000 + '</recorditem></appmsg></msg>']:
            self.assertIsNone(parse_quote(value, ROOM))


class QuotedImageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.first, self.last = self.root / 'quoted.png', self.root / 'recent.png'
        Image.new('RGB', (10, 10), 'red').save(self.first)
        Image.new('RGB', (10, 10), 'blue').save(self.last)
        self.cache = RecentImages(self.root, clock=lambda: 200)
        self.original = Message('', ROOM, AUTHOR, type=3, is_group=True, session_id='one', sequence=1,
                                received_at=100, data={'image_received_at': 100, 'image_status': 'ready',
                                'native_message_id': SERVER_ID, 'image_path': str(self.first)})
        self.quoted = Message(xml(), ROOM, ASKER, is_group=True, session_id='one', sequence=2,
                              received_at=200, data={'quoted_message': parse_quote(xml(), ROOM)[1]})

    def test_explicit_quote_selects_another_members_exact_image_not_latest(self):
        self.cache.observe(self.original)
        self.cache.observe(replace(self.original, sender_id=ASKER, sequence=3, received_at=150,
                                   data={'image_received_at': 150, 'image_status': 'ready', 'image_path': str(self.last),
                                         'native_message_id': '999'}))
        self.assertEqual(self.cache.for_message(self.quoted), (image_data_url(self.first, self.root),))

    def test_missing_reference_never_falls_back_to_same_senders_latest(self):
        self.cache.observe(replace(self.original, sender_id=ASKER))
        with self.assertRaisesRegex(LLMError, '引用图片未缓存'):
            self.cache.for_message(self.quoted)

    def test_quote_index_retains_images_past_five_minutes_and_four_recent_items(self):
        self.cache.observe(self.original)
        for sequence in range(3, 10):
            self.cache.observe(replace(self.original, sequence=sequence,
                               data={**self.original.data, 'native_message_id': str(sequence)}))
        self.cache.clock = lambda: 800
        question = replace(self.quoted, received_at=800)
        self.assertEqual(self.cache.for_message(question), (image_data_url(self.first, self.root),))
        self.assertFalse(self.cache._rows)

    def test_room_session_author_and_reference_id_must_match(self):
        self.cache.observe(self.original)
        for change in ({'session_id': 'new'}, {'chat_id': 'another@chatroom'},
                       {'data': {'quoted_message': {**self.quoted.data['quoted_message'], 'message_id': '1'}}},
                       {'data': {'quoted_message': {**self.quoted.data['quoted_message'], 'sender_id': ASKER}}}):
            with self.subTest(change=change), self.assertRaises(LLMError):
                self.cache.for_message(replace(self.quoted, **change))

    def test_quote_expiry_is_bounded_and_invalid_quote_no_fallback(self):
        self.cache.observe(self.original)
        self.cache.clock = lambda: 86501
        with self.assertRaises(LLMError):
            self.cache.for_message(replace(self.quoted, received_at=86501))
        self.assertFalse(self.cache._references)
        for quote in ({'status': 'unavailable'}, {'status': 'ready', 'type': '49'}, 'malformed'):
            with self.subTest(quote=quote), self.assertRaises(LLMError):
                self.cache.for_message(replace(self.quoted, data={'quoted_message': quote}))

    def test_pending_reference_updated_without_blocking_receive_thread(self):
        self.cache.observe(replace(self.original, data={**self.original.data, 'image_status': 'pending', 'image_path': ''}))
        result, started = [], threading.Event()
        def read():
            started.set()
            result.append(self.cache.for_message(self.quoted, wait_seconds=2))
        thread = threading.Thread(target=read)
        thread.start()
        self.assertTrue(started.wait(1))
        self.cache.observe(self.original)
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0], (image_data_url(self.first, self.root),))

    def test_reference_cache_is_bounded(self):
        for sequence in range(1005):
            self.cache.observe(replace(self.original, sequence=sequence + 1,
                               data={**self.original.data, 'native_message_id': str(sequence + 1)}))
        self.assertEqual(len(self.cache._references), 1000)


class NativeQuoteTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(ROOT / 'native'))
        from text_service import NativeTextService
        self.service = NativeTextService(pid=123, account=SELF, image_allowlist=(ROOM,))
        self.service.router.set_account(SELF)
        self.service._sender_wechat_name = Mock(return_value=None)
        self.service._file_receiver = Mock()

    def event(self, *, mentioned=True):
        from filehelper_service import ReceivedMediaMessageV5
        source = ('<msgsource><atuserlist>' + SELF + '</atuserlist></msgsource>') if mentioned else ''
        return ReceivedMediaMessageV5(type=49, sequence=42, sender=ROOM.encode(), receiver=SELF.encode(),
                                      image_xml=(ASKER + ':\n' + xml()).encode(), mention_source=source.encode(),
                                      native_message_id=2345678901234567890)

    def test_normalized_question_is_recorded_and_replied_with_real_mention(self):
        data = self.service._map_message(self.event())
        message = Message.from_native(11046, data, 'session')
        self.assertEqual(message.type, 1)
        self.assertEqual(message.sender_id, ASKER)
        self.assertEqual(message.text, '@机器人\u2005这张图什么意思？')
        self.assertTrue(data['is_at_self'])
        self.assertEqual(data['at_members'], (SELF,))
        self.assertEqual(data['quoted_message']['message_id'], SERVER_ID)
        self.assertEqual(data['native_message_id'], '2345678901234567890')
        self.assertEqual(data['raw_content'], '')
        self.service._file_receiver.submit.assert_not_called()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'members.json'
            path.write_text(json.dumps({'history_enabled': True, 'groups': {ROOM: []}}), encoding='utf-8')
            members = MemberStore(path, SELF)
            try:
                self.assertTrue(members.record(message))
                snapshot = members.conversation_snapshot(message)
                self.assertEqual(snapshot.current, message.text)
                plugin = Plugin()
                ctx = SimpleNamespace(members=members, llm=Mock(), personality=lambda _: PersonalityState(ROOM),
                                      logger=Mock(), response_guard=Mock())
                plugin.on_load(ctx)
                plugin.reply_to_member = Mock()
                plugin.on_message(message, ctx)
                plugin.reply_to_member.assert_called_once()
            finally:
                members.close()

    def test_visible_at_in_title_or_quoted_xml_does_not_trigger_bot(self):
        data = self.service._map_message(self.event(mentioned=False))
        self.assertFalse(data['is_at_self'])
        members = SimpleNamespace(groups={ROOM}, account=SELF)
        plugin = Plugin()
        plugin.members = members
        plugin.reply_to_member = Mock()
        plugin.on_message(Message.from_native(11046, data, 'session'), Mock())
        plugin.reply_to_member.assert_not_called()

    def test_quote_title_cannot_run_other_plugins_commands(self):
        data = self.service._map_message(self.event())
        data['msg'] = data['content'] = '/清空'
        message = Message.from_native(11046, data, 'session')
        app = SimpleNamespace(config={'chat_allowlist': [ROOM]}, members=Mock(), plugins=Mock())
        BotApplication._emit_chat(app, message)
        app.members.record.assert_called_once_with(message)
        app.plugins.dispatch.assert_called_once_with(message, only='mention_chat')
        app.plugins.is_active.assert_not_called()
