from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock
import unittest

from wxbot_framework.dialogue import Dialogue
from wxbot_framework.llm import LLMClient, ModelUnavailable
from wxbot_framework.members import MemberStore, SnapshotExpired
from wxbot_framework.personality import PersonalityLLM, PersonalityState
from wxbot_framework.sdk import Message
from wxbot_framework.search_context import contextual_search_query
from wxbot_framework.web_search import SearchError

ROOM, OTHER_ROOM, SELF = '12345678@chatroom', '87654321@chatroom', 'wxid_bot'


class ConversationMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / 'members.json'
        self.config.write_text(json.dumps({'history_enabled': True, 'daily_clear': True,
                                          'groups': {ROOM: [], OTHER_ROOM: []}}), encoding='utf-8')
        self.now = datetime(2026, 11, 3, 8, tzinfo=timezone.utc)
        self.store = MemberStore(self.config, SELF, now=lambda: self.now)
        self.addCleanup(self.store.close)
        self.sequence = 0

    def message(self, text, sender='wxid_a', room=ROOM):
        self.sequence += 1
        self.now += timedelta(seconds=1)
        message = Message(text, room, sender, is_group=True, session_id='test', sequence=self.sequence)
        self.store.record(message)
        return message

    def remember(self, text, reply, sender='wxid_a', room=ROOM, evidence=None):
        msg = self.message(text, sender, room)
        self.store.remember_dialogue(msg, self.store.conversation_snapshot(msg), reply, evidence)
        return msg

    def test_completed_turns_and_search_evidence_are_scoped_by_member_room_and_account(self):
        evidence = {'query': '甲某 身份', 'results': [{'title': '甲某', 'snippet': '公开资料'}]}
        self.remember('评价甲某', '甲某参与某公开活动。', evidence=evidence)
        self.remember('其他人问题', '其他人的回答', sender='wxid_b')
        self.remember('其他群问题', '其他群回答', room=OTHER_ROOM)
        snapshot = self.store.conversation_snapshot(self.message('她呢'))
        self.assertEqual(len(snapshot.turns), 1)
        self.assertEqual(snapshot.turns[0]['reply'], '甲某参与某公开活动。')
        self.assertEqual(snapshot.turns[0]['web_search']['query'], '甲某 身份')
        second = MemberStore(self.config, 'wxid_second', now=lambda: self.now)
        try:
            msg = Message('她呢', ROOM, 'wxid_a', is_group=True, session_id='second', sequence=1)
            second.record(msg)
            self.assertEqual(second.conversation_snapshot(msg).turns, ())
        finally:
            second.close()

    def test_reopen_preserves_recent_complete_turns(self):
        self.remember('查询对象', '回答')
        self.store.close()
        self.store = MemberStore(self.config, SELF, now=lambda: self.now)
        self.addCleanup(self.store.close)
        self.assertEqual(self.store.conversation_snapshot(self.message('继续问')).turns[0]['reply'], '回答')

    def test_three_turn_bound_clear_and_rollover_do_not_reuse_old_answers(self):
        for n in range(5):
            self.remember(f'问题{n}', f'回答{n}')
        snapshot = self.store.conversation_snapshot(self.message('当前'))
        self.assertEqual([t['reply'] for t in snapshot.turns], ['回答2', '回答3', '回答4'])
        self.assertEqual(self.store._connection().execute('SELECT COUNT(*) FROM dialogue_turns').fetchone()[0], 3)
        self.store.clear(ROOM)
        self.assertEqual(self.store._connection().execute('SELECT COUNT(*) FROM dialogue_turns').fetchone()[0], 0)
        self.assertEqual(self.store.conversation_snapshot(self.message('清空后')).turns, ())
        self.remember('今天问题', '今天回答')
        self.now += timedelta(days=1)
        self.store.rollover()
        self.assertEqual(self.store.conversation_snapshot(self.message('明天问题')).turns, ())

    def test_stale_generation_cannot_reintroduce_cleared_answer(self):
        msg = self.message('当前')
        snapshot = self.store.conversation_snapshot(msg)
        self.store.clear(ROOM)
        with self.assertRaises(SnapshotExpired):
            self.store.remember_dialogue(msg, snapshot, '旧答案')

    def test_current_snapshot_never_reads_later_completed_answer(self):
        self.remember('以前问题', '以前回答')
        current = self.message('当前')
        self.remember('以后问题', '以后回答')
        snapshot = self.store.conversation_snapshot(current)
        self.assertEqual([t['reply'] for t in snapshot.turns], ['以前回答'])

    def test_dialogue_receives_previous_assistant_turns_as_data(self):
        self.remember('查询甲某', '甲某是公开活动参与者', evidence={'query': '甲某'})
        snapshot = self.store.conversation_snapshot(self.message('她呢'))
        llm = SimpleNamespace(supports_plain_text=True, model='test', complete_text=Mock(return_value='继续回答'))
        Dialogue(llm).reply(snapshot)
        system, user = llm.complete_text.call_args.args
        self.assertEqual(json.loads(user)['recent_turns'][0]['reply'], '甲某是公开活动参与者')
        self.assertIn('不是权威事实', system)

    def test_stale_personal_qa_expires_but_recent_group_discussion_is_available(self):
        self.remember('评价甲某', '甲某之前的回答')
        self.now += timedelta(minutes=11)
        self.message('周六去惠州玩吧', sender='wxid_b')
        self.message('惠州海边最近会不会下雨', sender='wxid_c')
        snapshot = self.store.conversation_snapshot(self.message('小管家 那边天气怎么样'))
        self.assertEqual(snapshot.messages, ())
        self.assertEqual(snapshot.turns, ())
        self.assertEqual([m['text'] for m in snapshot.group_messages], ['周六去惠州玩吧', '惠州海边最近会不会下雨'])
        self.assertEqual([m['speaker'] for m in snapshot.group_messages], ['P2', 'P3'])
        self.assertTrue(all(not m['is_current_speaker'] for m in snapshot.group_messages))
        # 只改变提示所用会话，不删除SQLite记录。
        self.assertEqual(self.store._connection().execute('SELECT COUNT(*) FROM dialogue_turns').fetchone()[0], 1)

    def test_busy_group_evicts_old_qa_even_within_ten_minutes(self):
        self.remember('旧话题', '旧回答')
        for n in range(31):
            self.message(f'新话题{n}', sender='wxid_b')
        snapshot = self.store.conversation_snapshot(self.message('你怎么看'))
        self.assertEqual(snapshot.turns, ())
        self.assertEqual(snapshot.messages, ())
        self.assertEqual(len(snapshot.group_messages), 30)
        self.assertEqual(snapshot.group_messages[0]['text'], '新话题1')

    def test_recent_followup_stays_available_and_queued_snapshot_uses_input_time(self):
        self.remember('查上海天气', '上海今天下雨')
        current = self.message('那北京呢')
        self.now += timedelta(minutes=12)
        self.message('这是后来的话', sender='wxid_b')
        snapshot = self.store.conversation_snapshot(current)
        self.assertEqual(snapshot.turns[0]['reply'], '上海今天下雨')
        self.assertEqual([r['text'] for r in snapshot.group_messages], ['查上海天气'])

    def test_group_context_is_scoped_and_uses_stable_speaker_labels_without_ids(self):
        self.message('另一个群的消息', sender='wxid_b', room=OTHER_ROOM)
        self.message('我同意周六出发')
        self.message('那我订车票', sender='wxid_b')
        self.message('/清空', sender='wxid_c')
        snapshot = self.store.conversation_snapshot(self.message('小管家 帮我们整理下安排'))
        llm = SimpleNamespace(supports_plain_text=True, model='test', complete_text=Mock(return_value='周六出发'))
        Dialogue(llm).reply(snapshot)
        prompt, raw = llm.complete_text.call_args.args
        payload = json.loads(raw)
        group = payload['group_context']
        self.assertEqual([r['speaker'] for r in group], ['P1', 'P2'])
        self.assertEqual([r['is_current_speaker'] for r in group], [True, False])
        self.assertNotIn('另一个群', raw)
        self.assertNotIn('wxid_', raw)
        self.assertIn('保持发言归属', prompt)
        Dialogue(llm).reply(snapshot, use_context=False)
        raw = llm.complete_text.call_args.args[1]
        self.assertNotIn('group_context', json.loads(raw))
        self.assertEqual(json.loads(raw)['previous'], [])

    def test_window_boundary_is_inclusive_and_reopen_does_not_revive_old_qa(self):
        self.remember('旧问答', '旧回复')
        self.now += timedelta(minutes=10, seconds=-1)
        snapshot = self.store.conversation_snapshot(self.message('继续问'))
        self.assertEqual(len(snapshot.turns), 1)
        self.store.close()
        self.now += timedelta(seconds=1)
        self.store = MemberStore(self.config, SELF, now=lambda: self.now)
        self.addCleanup(self.store.close)
        snapshot = self.store.conversation_snapshot(self.message('换个问题'))
        self.assertEqual(snapshot.turns, ())


class QueryContextTests(unittest.TestCase):
    def snapshot(self, current, history=(), turns=()):
        return SimpleNamespace(current=current, messages=tuple({'text': text} for text in history), turns=turns)

    def test_identity_clarification_combines_name_and_new_qualifier(self):
        llm = SimpleNamespace(complete_text=Mock(return_value='文吉儿 湖南卫视 综艺 嘉宾'))
        result = contextual_search_query(llm, self.snapshot('湖南卫视综艺节目的嘉宾啊',
            ['锐评一下文吉儿', '你现在是什么人格', '再次评价一下文吉儿']))
        self.assertEqual(result, '文吉儿 湖南卫视 综艺 嘉宾')
        system, payload = llm.complete_text.call_args.args
        data = json.loads(payload)
        self.assertIn('文吉儿', data['topics'])
        self.assertNotIn('你现在是什么人格', data['topics'])
        self.assertNotIn('湖南卫视', system)
        self.assertIn('不得凭常识添加', system)

    def test_domain_independent_followup_can_change_city_or_software_version(self):
        for current, previous, expected in [('那北京呢', '上海 明天 天气', '北京 明天 天气'),
                                            ('它支持新版吗', '某工具 Windows 版本', '某工具 新版 支持')]:
            llm = SimpleNamespace(complete_text=Mock(return_value=expected))
            self.assertEqual(contextual_search_query(llm, self.snapshot(current, [previous])), expected)

    def test_greeting_does_not_rewrite_and_new_topic_is_given_clear_reset_instruction(self):
        llm = SimpleNamespace(complete_text=Mock())
        for greeting in ('你好', '你好啊', '你好呀！', '谢谢啦', '晚上好呀'):
            self.assertEqual(contextual_search_query(llm, self.snapshot(greeting, ['评价甲某'])), greeting)
        llm.complete_text.assert_not_called()
        llm.complete_text.return_value = 'Rust 生命周期'
        self.assertEqual(contextual_search_query(llm, self.snapshot('Rust 生命周期', ['评价甲某'])), 'Rust 生命周期')
        self.assertIn('自足的新话题', llm.complete_text.call_args.args[0])

    def test_explicit_no_search_does_not_rewrite_or_send_context(self):
        llm = SimpleNamespace(complete_text=Mock())
        self.assertIsNone(contextual_search_query(llm, self.snapshot('不用搜索，聊聊', ['查询甲某'])))
        llm.complete_text.assert_not_called()

    def test_last_successful_search_and_reply_are_available_to_rewriter(self):
        turn = {'question': '查询某工具', 'reply': '某工具的稳定版是X', 'web_search': {'query': '某工具 稳定版'}}
        llm = SimpleNamespace(complete_text=Mock(return_value='某工具 新版 功能'))
        contextual_search_query(llm, self.snapshot('它的新版呢', turns=(turn,)))
        payload = json.loads(llm.complete_text.call_args.args[1])
        self.assertEqual(payload['topics'], ['某工具 稳定版'])
        self.assertEqual(payload['turns'][0]['reply'], '某工具的稳定版是X')

    def test_rewriter_output_cannot_be_multiline_or_sensitive_payload(self):
        for output in ('', 'bad\npayload', 'https://private.example', 'api_key=secret', None, {}):
            llm = SimpleNamespace(complete_text=Mock(return_value=output))
            with self.subTest(output=output):
                self.assertEqual(contextual_search_query(llm, self.snapshot('那它呢', ['查询某工具'])), '那它呢')

    def test_rewrite_failure_keeps_search_and_answer_available(self):
        llm = SimpleNamespace(complete_text=Mock(side_effect=ModelUnavailable('timeout')))
        self.assertEqual(contextual_search_query(llm, self.snapshot('那它呢', ['查询某工具'])), '那它呢')

    def test_internal_rewrite_bypasses_persona_and_has_short_call_budget(self):
        raw = LLMClient({'api_key': 'test-only', 'model': 'test'})
        raw.complete_text = Mock(return_value='某工具 新版')
        wrapper = PersonalityLLM(raw, PersonalityState(ROOM, '嘴臭'), current=lambda: True)
        self.assertEqual(contextual_search_query(wrapper, self.snapshot('它的新版呢', ['某工具'])), '某工具 新版')
        self.assertEqual(raw.complete_text.call_args.kwargs, {'timeout_seconds': 5})
        self.assertNotIn('超雄', raw.complete_text.call_args.args[0])

    def test_legacy_json_only_client_can_still_search(self):
        self.assertEqual(contextual_search_query(SimpleNamespace(), self.snapshot('那它呢', ['某工具'])), '那它呢')
