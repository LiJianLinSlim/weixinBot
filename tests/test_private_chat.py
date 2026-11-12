"""私聊白名单、隔离、持久上下文及直接文字发送；只用合成消息。"""
import json
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import test_chat_summary as daily
from plugins.mention_chat import Plugin
from plugins.personality import Plugin as PersonalityPlugin
from wxbot_framework.members import MemberError, SnapshotExpired
from wxbot_framework.personality import prepare_switch
from wxbot_framework.runtime import BotApplication
from wxbot_framework.sdk import Message


class PrivateChatTests(unittest.TestCase):
    tearDown = daily.DailySummaryTests.tearDown
    new_store = daily.DailySummaryTests.new_store
    message = daily.DailySummaryTests.message
    model = daily.DailySummaryTests.model

    def setUp(self):
        daily.DailySummaryTests.setUp(self)
        options = json.loads(self.path.read_text(encoding='utf-8'))
        options.update(private_chats=[daily.CAT, daily.OTHER], default_personality='毒舌损友')
        self.path.write_text(json.dumps(options), encoding='utf-8')
        self.store.close()
        self.store = self.new_store()
        self.ctx._members = self.store
        self.ctx._llm = Mock(complete_json=Mock(return_value={'reply': '在呢 直接说'}))
        self.chat = Plugin()
        self.chat.on_load(self.ctx)
        self.chat.decision_router = SimpleNamespace(decide=Mock(return_value=
            SimpleNamespace(use_context=True, search=False, source='synthetic')))

    def private(self, text='你好', sender=daily.CAT, sequence=1, **kwargs):
        return Message(text, sender, sender_id=sender, is_group=False,
                       session_id='test', sequence=sequence, **kwargs)

    def test_private_without_at_is_sent_once_to_peer_without_mention(self):
        message = self.private()
        self.assertTrue(self.store.record(message))
        self.chat.on_message(message, self.ctx)
        self.assertEqual(self.backend.sent, [(daily.CAT, '在呢 直接说')])
        prompt, payload = self.ctx._llm.complete_json.call_args.args
        self.assertIn('一对一私聊', prompt)
        self.assertIn('当前人格：毒舌损友', prompt)
        self.assertEqual(json.loads(payload)['current'], '你好')
        self.assertEqual(self.store.personality(daily.ROOM).name, '毒舌损友')

    def test_other_peers_self_echo_system_media_and_wrong_sender_never_call_model(self):
        messages = [self.private(sender='wxid_stranger'), self.private(is_self=True),
            self.private(data={'bot_generated': True}),
            self.private(data={'dispatch_to_plugins': False}),
            Message('spoof', daily.CAT, daily.OTHER, session_id='test'),
            self.private(type=3)]
        for message in messages:
            with self.subTest(message=message):
                self.assertFalse(self.store.record(message))
                self.chat.on_message(message, self.ctx)
        self.assertEqual(self.backend.sent, [])
        self.ctx._llm.complete_json.assert_not_called()

    def test_history_persists_and_isolates_private_group_account_and_other_peer(self):
        group = self.message('群里的秘密', sequence=10)
        self.store.record(group)
        other = self.private('另一个私聊的秘密', daily.OTHER, sequence=11)
        self.store.record(other)
        old = self.private('我想吃面', sequence=12)
        self.store.record(old)
        snapshot = self.store.conversation_snapshot(old)
        self.store.remember_dialogue(old, snapshot, '你刚说想吃面')
        restarted = self.new_store()
        current = self.private('刚才我想吃什么', sequence=13)
        restarted.record(current)
        snapshot = restarted.conversation_snapshot(current)
        self.assertEqual([r['text'] for r in snapshot.messages], ['我想吃面'])
        self.assertEqual(snapshot.turns[0]['reply'], '你刚说想吃面')
        self.assertEqual(snapshot.group_messages, ())
        other_account = self.new_store('wxid_second_robot')
        with self.assertRaises(SnapshotExpired):
            other_account.conversation_snapshot(current)
        other_account.record(self.private('第二个账号自己的记录', sequence=14))
        second_current = self.private('继续', sequence=15)
        other_account.record(second_current)
        self.assertEqual([r['text'] for r in other_account.conversation_snapshot(second_current).messages],
                         ['第二个账号自己的记录'])
        self.assertNotIn('群里的秘密', str(snapshot))
        self.assertNotIn('另一个私聊的秘密', str(snapshot))

    def test_private_clear_invalidates_pending_reply_and_preserves_group_history(self):
        group = self.message('保留群记录', sequence=20)
        self.store.record(group)
        message = self.private('私聊记录', sequence=21)
        self.store.record(message)
        snapshot = self.store.conversation_snapshot(message)
        self.chat.on_message(self.private('/清空', sequence=22), self.ctx)
        self.assertEqual(self.backend.sent[-1], (daily.CAT, '私聊上下文已清空'))
        self.assertFalse(self.store.snapshot_is_current(snapshot))
        self.assertEqual(len(self.store.today_group_snapshot(daily.ROOM).messages), 1)
        self.ctx._llm.complete_json.assert_not_called()
        with self.assertRaises(SnapshotExpired):
            with self.store.delivery_guard(snapshot):
                self.fail('已清空的回复不能再发送')

    def test_persona_command_in_private_is_independent_and_never_calls_answer_model(self):
        message = prepare_switch(self.private('/切换人格 夸夸'), self.store)
        ack = PersonalityPlugin()
        ack.on_load(self.ctx)
        ack.on_message(message, self.ctx)
        self.chat.on_message(message, self.ctx)
        self.assertEqual(len(self.backend.sent), 1)
        self.assertIn('已切换：夸夸', self.backend.sent[0][1])
        self.assertEqual(self.new_store().personality(daily.CAT).name, '夸夸')
        self.assertEqual(self.store.personality(daily.ROOM).name, '毒舌损友')
        self.ctx._llm.complete_json.assert_not_called()

    def test_rollover_clears_private_history_and_invalidates_reply(self):
        message = self.private()
        self.store.record(message)
        snapshot = self.store.conversation_snapshot(message)
        self.now += timedelta(days=1)
        self.store.rollover()
        self.assertFalse(self.store.snapshot_is_current(snapshot))
        rows = self.store._connection().execute('SELECT count(*) FROM messages WHERE room=?', (daily.CAT,)).fetchone()
        self.assertEqual(rows[0], 0)

    def test_runtime_white_list_controls_recording_and_private_dispatch(self):
        app = object.__new__(BotApplication)
        app.config, app.members, app.plugins = {'chat_allowlist': [daily.CAT]}, self.store, Mock()
        app.plugins.is_active.return_value = True
        app._emit_chat(self.private('直接发，不用艾特', sequence=31))
        self.assertEqual(app.plugins.dispatch.call_count, 1)
        self.assertEqual(self.store.conversation_snapshot(self.private('直接发，不用艾特', sequence=31)).current,
                         '直接发，不用艾特')
        app._emit_chat(self.private(sender=daily.OTHER, sequence=32))
        self.assertEqual(app.plugins.dispatch.call_count, 1)

    def test_invalid_private_chat_config_rejected(self):
        original = json.loads(self.path.read_text(encoding='utf-8'))
        for peers in ('all', [daily.ROBOT], [daily.ROOM], ['filehelper'], ['weixin'], [None]):
            with self.subTest(peers=peers):
                self.path.write_text(json.dumps({**original, 'private_chats': peers}), encoding='utf-8')
                with self.assertRaises(ValueError):
                    self.new_store()


if __name__ == '__main__':
    unittest.main()
