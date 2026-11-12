import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from plugins.mention_chat import Plugin
from wxbot_framework.decision_router import DecisionRouter, RoutingDecision
from wxbot_framework.dialogue import Dialogue
from wxbot_framework.llm import LLMClient, ModelUnavailable
from wxbot_framework.personality import PersonalityLLM, PersonalityState
from wxbot_framework.sdk import Message
from wxbot_framework.web_search import SearchBundle, SearchResult


class RouterTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.config = Path(folder.name) / 'routing.json'
        self.config.write_text(json.dumps({'model': 'fast-router', 'timeout_seconds': 6}), encoding='utf-8')
        self.router = DecisionRouter(self.config)
        self.client = LLMClient({'base_url': 'https://test.example/v1', 'api_key': 'test-only',
            'model': 'answer-model', 'timeout_seconds': 60, 'extra_body': {'reasoning_effort': 'high'},
            'fallback_models': [{'model': 'backup'}]})

    def snapshot(self, current='那上海呢', history=('北京明天天气怎么样',), turns=()):
        return SimpleNamespace(current=current, day='2026-11-03', speaker_name='用户',
            messages=tuple({'text': text, 'received_at': '2026-11-03T08:00:00+00:00'} for text in history),
            turns=turns)

    def test_two_independent_decisions_and_query_in_single_call(self):
        for search in (True, False):
            for context in (True, False):
                data = json.dumps({'search': search, 'use_context': context, 'query': '上海 明天 天气'})
                with self.subTest(search=search, context=context), patch.object(
                        LLMClient, 'complete_text', autospec=True, return_value=data) as call:
                    result = self.router.decide(self.client, self.snapshot())
                    self.assertEqual((result.search, result.use_context), (search, context))
                    self.assertEqual(result.source, 'fast-router')
                    self.assertEqual(call.call_count, 1)
                    sent_client = call.call_args.args[0]
                    self.assertEqual(sent_client.model, 'fast-router')
                    self.assertEqual(sent_client.timeout, 6)
                    self.assertEqual(sent_client.base_url, self.client.base_url)
                    self.assertEqual(sent_client._api_key, self.client._api_key)
                    self.assertEqual(sent_client.fallback_models, [])
                    self.assertEqual(sent_client.extra_body, {})
                    self.assertEqual(self.client.model, 'answer-model')

    def test_semantic_tone_uses_existing_call_and_defaults_conservatively(self):
        for tone in ('serious', 'casual', 'neutral', 'roast', None, []):
            data = json.dumps({'search': False, 'use_context': True, 'query': '', 'tone': tone})
            with self.subTest(tone=tone), patch.object(LLMClient, 'complete_text', return_value=data) as call:
                decision = self.router.decide(self.client, self.snapshot('那怎么办', history=('最近被裁了',)))
            self.assertEqual(decision.tone, tone if isinstance(tone, str) and tone in ('serious', 'casual', 'neutral') else 'neutral')
            self.assertEqual(call.call_count, 1)
        with patch.object(LLMClient, 'complete_text', side_effect=ModelUnavailable('offline')):
            self.assertEqual(self.router.decide(self.client, self.snapshot()).tone, 'neutral')

    def test_persona_does_not_leak_into_planning_or_update_answer_identity(self):
        wrapped = PersonalityLLM(self.client, PersonalityState('room', '嘴臭'), current=lambda: True)
        self.client._response_state.model = 'actual-answer-model'
        with patch.object(LLMClient, 'complete_text', autospec=True,
                return_value='{"search":false,"use_context":false,"query":""}') as call:
            self.router.decide(wrapped, self.snapshot('你好啊'))
        self.assertNotIn('超雄', call.call_args.args[1])
        self.assertEqual(self.client.response_model, 'actual-answer-model')

    def test_lower_decision_effort_is_explicit_and_does_not_change_answer_effort(self):
        self.config.write_text(json.dumps({'model': 'gpt-6-luna', 'timeout_seconds': 6,
                                          'reasoning_effort': 'low'}), encoding='utf-8')
        with patch.object(LLMClient, 'complete_text', autospec=True,
                return_value='{"search":false,"use_context":false,"query":""}') as call:
            result = self.router.decide(self.client, self.snapshot('你好啊'))
        self.assertEqual(result.source, 'gpt-6-luna')
        self.assertEqual(call.call_args.args[0].extra_body, {'reasoning_effort': 'low'})
        self.assertEqual(self.client.extra_body, {'reasoning_effort': 'high'})

    def test_followup_resolves_query_but_new_topic_cannot_inherit_old_search_subject(self):
        response = '{"search":true,"use_context":true,"query":"上海 明天 天气"}'
        with patch.object(LLMClient, 'complete_text', return_value=response):
            self.assertEqual(self.router.decide(self.client, self.snapshot()).query, '上海 明天 天气')
        response = '{"search":true,"use_context":false,"query":"北京 明天 天气"}'
        with patch.object(LLMClient, 'complete_text', return_value=response):
            result = self.router.decide(self.client, self.snapshot('Rust最新版本'))
        self.assertEqual(result.query, 'Rust最新版本')

    def test_explicit_search_and_opt_out_override_model_choice(self):
        no_search = '{"search":false,"use_context":false,"query":""}'
        with patch.object(LLMClient, 'complete_text', return_value=no_search):
            result = self.router.decide(self.client, self.snapshot('搜索Rust最新版本'))
            self.assertTrue(result.search)
            self.assertEqual(result.query, 'Rust最新版本')
        with patch.object(LLMClient, 'complete_text', return_value=
                '{"search":true,"use_context":true,"query":"不要泄露此搜索词"}'):
            self.assertFalse(self.router.decide(self.client, self.snapshot('不要联网，接着解释')).search)

    def test_empty_history_forces_no_context_and_prevents_hallucinated_query(self):
        with patch.object(LLMClient, 'complete_text', return_value=
                '{"search":true,"use_context":true,"query":"臆造的名字"}'):
            result = self.router.decide(self.client, self.snapshot('北京天气', history=()))
        self.assertFalse(result.use_context)
        self.assertEqual(result.query, '北京天气')

    def test_group_only_context_can_resolve_followup_without_personal_history(self):
        snapshot = self.snapshot('那边明天天气呢', history=())
        snapshot.group_messages = ({'speaker': 'P2', 'name': '群友', 'is_current_speaker': False,
            'text': '周六去惠州海边', 'received_at': '2026-11-03T08:00:00+00:00'},)
        with patch.object(LLMClient, 'complete_text', return_value=
                '{"search":true,"use_context":true,"query":"惠州 明天 天气"}') as call:
            result = self.router.decide(self.client, snapshot)
        self.assertTrue(result.use_context)
        self.assertEqual(result.query, '惠州 明天 天气')
        payload = json.loads(call.call_args.args[1])
        self.assertEqual(payload['group_context'][0]['speaker'], 'P2')
        self.assertEqual(payload['group_context'][0]['text'], '周六去惠州海边')
        from wxbot_framework.decision_router import fallback_decision
        self.assertTrue(fallback_decision(snapshot).use_context)

    def test_timeout_and_invalid_json_do_not_break_chat(self):
        failures = [ModelUnavailable('private-upstream'), '', '{}', '```json\n{}\n```',
                    '{"search":"yes","use_context":true,"query":"天气"}']
        for failure in failures:
            options = {'side_effect': failure} if isinstance(failure, Exception) else {'return_value': failure}
            with self.subTest(failure=type(failure).__name__), patch.object(LLMClient, 'complete_text', **options):
                result = self.router.decide(self.client, self.snapshot('你好啊'))
            self.assertEqual((result.source, result.search, result.use_context), ('fallback', False, False))

    def test_search_queries_cannot_include_credentials_even_if_model_suggests_them(self):
        with patch.object(LLMClient, 'complete_text', return_value=
                '{"search":true,"use_context":true,"query":"api_key=private"}'):
            result = self.router.decide(self.client, self.snapshot('那上海呢'))
        self.assertEqual(result.query, '那上海呢')

    def test_disabled_missing_invalid_config_or_legacy_client_never_calls_cloud(self):
        for contents in ('{"enabled":false,"model":"fast-router"}', '{"model":"x","timeout_seconds":60}', 'broken'):
            self.config.write_text(contents, encoding='utf-8')
            with patch.object(LLMClient, 'complete_text') as call:
                self.router.decide(self.client, self.snapshot())
                call.assert_not_called()
        self.config.unlink()
        with patch.object(LLMClient, 'complete_text') as call:
            self.router.decide(self.client, self.snapshot())
            self.router.decide(SimpleNamespace(), self.snapshot())
            call.assert_not_called()

    def test_input_is_bounded_and_preserves_recent_completed_turn(self):
        turns = tuple({'question': '以前问题', 'reply': '以前回答',
                       'web_search': {'query': '以前主题'}} for _ in range(9))
        with patch.object(LLMClient, 'complete_text', autospec=True,
                return_value='{"search":false,"use_context":true,"query":""}') as call:
            self.router.decide(self.client, self.snapshot('接着说', history=['话'*300]*20, turns=turns), has_image=True)
        payload = json.loads(call.call_args.args[2])
        self.assertEqual(len(payload['previous']), 6)
        self.assertEqual(len(payload['previous'][0]), 200)
        self.assertEqual(len(payload['recent_turns']), 3)
        self.assertTrue(payload['has_image'])

    def test_short_transport_budget_is_bounded_without_answer_model_fallback(self):
        now = [0.0]
        def send(request, timeout):
            now[0] += timeout
            raise TimeoutError('offline')
        with patch('wxbot_framework.llm.time.monotonic', side_effect=lambda: now[0]), \
                patch('wxbot_framework.llm.urlopen', side_effect=send) as call:
            result = self.router.decide(self.client, self.snapshot())
        self.assertEqual(result.source, 'fallback')
        self.assertEqual(now[0], 6)
        self.assertEqual(call.call_count, 1)


class RoutingIntegrationTests(unittest.TestCase):
    def test_plugin_obeys_each_decision_without_another_rewriter_call(self):
        for search in (True, False):
            for context in (True, False):
                with self.subTest(search=search, context=context):
                    plugin = Plugin()
                    plugin.members = SimpleNamespace(display_name=lambda *args: '用户')
                    plugin.decision_router = SimpleNamespace(decide=Mock(return_value=
                        RoutingDecision(search, context, '上海 明天 天气', 'fast-router', 'serious')))
                    evidence = SearchBundle('上海 明天 天气', '2026-11-03', 'test',
                        (SearchResult('天气', '测试资料', 'example.com'),))
                    plugin.web_search = SimpleNamespace(search=Mock(return_value=evidence))
                    client = SimpleNamespace(supports_plain_text=True, model='answer-model',
                        complete_text=Mock(return_value='回答'))
                    ctx = SimpleNamespace(logger=Mock(), recent_images=None, llm_for=lambda *args, **kw: client)
                    snapshot = SimpleNamespace(current='那上海呢', day='2026-11-03', speaker_name='用户',
                        messages=({'text': '北京明天天气', 'received_at': '2026-11-03T08:00:00+00:00'},),
                        turns=({'question': '北京天气', 'reply': '此前回答'},))
                    message = Message('那上海呢', '123@chatroom', 'user', is_group=True)
                    with patch('plugins.mention_chat.deliver_model_reply') as deliver:
                        plugin.reply_to_member(message, ctx, snapshot, PersonalityState('123@chatroom'), Mock())
                    plugin.decision_router.decide.assert_called_once()
                    self.assertEqual(plugin.web_search.search.call_count, int(search))
                    client.complete_text.assert_called_once()
                    self.assertIn('收起幽默和毒舌', client.complete_text.call_args.args[0])
                    deliver.assert_called_once()
                    payload = json.loads(client.complete_text.call_args.args[1])
                    self.assertEqual(bool(payload['previous']), context)
                    self.assertEqual('recent_turns' in payload, context)
                    self.assertEqual('web_search' in payload, search)

    def test_skipping_context_does_not_drop_explicit_image_input(self):
        client = SimpleNamespace(supports_plain_text=True, supports_images=True, model='vision',
            complete_text=Mock(return_value='红色'))
        snapshot = SimpleNamespace(current='什么颜色', speaker_name='用户', day='2026-11-03',
            messages=({'text': '无关旧话题'},), turns=({'question': '旧图', 'reply': '旧描述'},))
        Dialogue(client).reply(snapshot, images=('image-data',), use_context=False)
        self.assertEqual(client.complete_text.call_args.kwargs['images'], ('image-data',))
        payload = json.loads(client.complete_text.call_args.args[1])
        self.assertEqual(payload['previous'], [])
        self.assertNotIn('recent_turns', payload)
