import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from wxbot_framework.dialogue import Dialogue, serious_context, response_tone
from wxbot_framework.llm import LLMClient, LLMError
from wxbot_framework.personality import PersonalityLLM, PersonalityState, PersonalityExpired, PROMPTS


def response(text, **changes):
    return io.BytesIO(json.dumps({'choices': [{'message': {'content': text}, **changes}]}).encode())


class PlainChatTests(unittest.TestCase):
    def setUp(self):
        self.client = LLMClient({'api_key': 'dummy-test-key', 'model': 'test-model', 'json_mode': True})

    def test_plain_answer_requires_no_json_wrapper_or_response_format(self):
        with patch('wxbot_framework.llm.urlopen', return_value=response('你好，我是群机器人。')) as call:
            self.assertEqual(self.client.complete_text('普通问答', '你好'), '你好，我是群机器人。')
        payload = json.loads(call.call_args.args[0].data)
        self.assertNotIn('response_format', payload)
        self.assertEqual(payload['messages'][0]['content'], '普通问答')
        self.assertEqual(call.call_count, 1)

    def test_serious_context_detection_suppresses_humor_topics(self):
        for text in ('你希望他们和好吗', '我最近很难过怎么办', '我被打了该怎么办', '认真问原价怎么算'):
            with self.subTest(text=text): self.assertTrue(serious_context(text))
        for text in ('今天吃什么', '你又开始了', '小管家接个梗'):
            with self.subTest(text=text): self.assertFalse(serious_context(text))

    def test_serious_context_adds_plain_tone_directive(self):
        self.client.complete_text = Mock(return_value='希望他们能好好聊聊 但最后还是看他们自己')
        wrapped = PersonalityLLM(self.client, PersonalityState('room', '毒舌损友'), current=lambda: True)
        snapshot = SimpleNamespace(day='2026-11-06', speaker_name='群友', messages=(),
                                   current='你希望他们和好吗')
        Dialogue(wrapped).reply(snapshot)
        prompt = self.client.complete_text.call_args.args[0]
        self.assertIn('收起幽默和毒舌', prompt)

    def test_serious_scene_bypasses_all_personas_and_length_rules(self):
        for plain in (True, False):
            for name in PROMPTS:
                with self.subTest(plain=plain, name=name):
                    client = LLMClient({'api_key': 'test-only', 'model': 'test'}) if plain else Mock()
                    provider = Mock(return_value='先说说发生了什么' if plain else {'reply': '先说说发生了什么'})
                    if plain:
                        client.complete_text = provider
                    else:
                        client.complete_json = provider
                    wrapped = PersonalityLLM(client, PersonalityState('room', name), current=lambda: True)
                    snapshot = SimpleNamespace(current='那怎么办', day='2026-11-06', speaker_name='群友', messages=(),
                                               turns=({'question': '刚收到裁员通知', 'reply': '先缓一缓'},))
                    Dialogue(wrapped).reply(snapshot, tone='serious')
                    prompt = provider.call_args.args[0]
                    self.assertIn('收起幽默和毒舌', prompt)
                    self.assertNotIn(PROMPTS[name], prompt)
                    self.assertNotIn('正文总长不超过 20', prompt)
                    self.assertIsNone(wrapped.tone)

    def test_followup_fallback_retains_serious_context_without_polluting_new_topic(self):
        snapshot = SimpleNamespace(current='那怎么办', messages=(), turns=({'question': '最近很难过', 'reply': '怎么了'},))
        self.assertEqual(response_tone(snapshot), 'serious')
        self.assertEqual(response_tone(snapshot, use_context=False), 'neutral')
        snapshot.current = '今天吃什么'
        self.assertEqual(response_tone(snapshot), 'neutral')
        self.assertEqual(response_tone(snapshot, 'casual'), 'casual')

    def test_serious_reply_is_not_truncated_by_short_persona_on_delivery(self):
        # Covers the final postprocessor, after both routing and generation.
        from test_personality import PersonalityTests, ROOM
        fixture = PersonalityTests()
        fixture.setUp()
        try:
            fixture.store.set_personality(ROOM, '嘴臭')
            answer = '先确认你现在是否安全 然后把事情经过和具体需要的帮助告诉我 我们一起梳理接下来怎么做'
            plugin = fixture.setup_chat(Mock(return_value={'reply': answer}))
            plugin.on_message(fixture.chat('我被打了该怎么办'), fixture.ctx)
            self.assertEqual(fixture.backend.sent[-1][1], answer)
        finally:
            fixture.tearDown()
            fixture.doCleanups()

    def test_text_parts_supported_without_using_reasoning(self):
        with patch('wxbot_framework.llm.urlopen', return_value=response([{'type': 'text', 'text': '你好'}])):
            self.assertEqual(self.client.complete_text('chat', 'hi'), '你好')

    def test_thinking_is_never_sent_as_final_answer(self):
        with patch('wxbot_framework.llm.urlopen', side_effect=[response('<think>private reasoning'), response('最终回答')]):
            self.assertEqual(self.client.complete_text('chat', 'hi'), '最终回答')
        with patch('wxbot_framework.llm.urlopen', return_value=response('<think>private reasoning</think>最终回答')):
            self.assertEqual(self.client.complete_text('chat', 'hi'), '最终回答')

    def test_truncation_does_not_send_partial_output_or_retry(self):
        with patch('wxbot_framework.llm.urlopen', return_value=response('partial', finish_reason='length')) as call:
            with self.assertRaisesRegex(LLMError, '截断'):
                self.client.complete_text('chat', 'hi')
        self.assertEqual(call.call_count, 1)

    def test_transient_http_502_retried_once_without_leaking_body(self):
        failure = HTTPError('https://example.com', 502, 'gateway', {}, io.BytesIO(b'private-upstream-body'))
        with (patch('wxbot_framework.llm.urlopen', side_effect=[failure, response('正常回答')]) as call,
              patch('wxbot_framework.llm.time.sleep')):
            self.assertEqual(self.client.complete_text('chat', 'hi'), '正常回答')
        self.assertEqual(call.call_count, 2)

    def test_persistent_gateway_error_is_distinguished_and_bounded(self):
        failures = [HTTPError('https://example.com', 502, 'gateway', {}, io.BytesIO(b'private')) for _ in range(2)]
        with (patch('wxbot_framework.llm.urlopen', side_effect=failures) as call,
              patch('wxbot_framework.llm.time.sleep')):
            with self.assertRaisesRegex(LLMError, '上游服务暂时不可用') as error:
                self.client.complete_text('chat', 'hi')
        self.assertEqual(call.call_count, 2)
        self.assertNotIn('private', str(error.exception))

    def test_auth_error_is_not_retried(self):
        failure = HTTPError('https://example.com', 401, 'auth', {}, io.BytesIO(b'private'))
        with patch('wxbot_framework.llm.urlopen', side_effect=failure) as call:
            with self.assertRaisesRegex(LLMError, 'HTTP 401'):
                self.client.complete_text('chat', 'hi')
        self.assertEqual(call.call_count, 1)

    def test_dialogue_uses_text_path_with_configured_model_and_context(self):
        client = SimpleNamespace(supports_plain_text=True, model='configured-alias',
                                 complete_text=Mock(return_value='这是直接的文字回答。'))
        snapshot = SimpleNamespace(speaker_name='测试用户', day='2026-11-03', messages=(), current='@机器人 你好')
        self.assertEqual(Dialogue(client).reply(snapshot), '这是直接的文字回答。')
        system, user = client.complete_text.call_args.args
        self.assertNotIn('configured-alias', system)
        self.assertNotIn('先发图', system)
        self.assertNotIn('没有附带图片', system)
        self.assertNotIn('输出仅为 JSON 对象', system)
        self.assertEqual(json.loads(user)['current'], '你好')

    def test_model_identity_is_available_only_when_current_question_is_related(self):
        client = SimpleNamespace(supports_plain_text=True, model='configured-alias',
                                 complete_text=Mock(return_value='这是配置的模型标识。'))
        snapshot = SimpleNamespace(speaker_name='测试用户', day='2026-11-03', messages=(), current='你是什么模型')
        Dialogue(client).reply(snapshot)
        system, user = client.complete_text.call_args.args
        self.assertIn('configured-alias', system)
        self.assertIn('无法独立核验', system)
        self.assertNotIn('先发图', system)

    def test_old_model_and_image_questions_do_not_add_operational_asides_to_new_greeting(self):
        client = SimpleNamespace(supports_plain_text=True, model='configured-alias',
                                 complete_text=Mock(return_value='你好呀！'))
        snapshot = SimpleNamespace(speaker_name='测试用户', day='2026-11-03', current='你好', messages=(
            {'received_at': '2026-11-03T16:00:00+08:00', 'text': '你是什么模型'},
            {'received_at': '2026-11-03T16:01:00+08:00', 'text': '这张图是什么'},))
        self.assertEqual(Dialogue(client).reply(snapshot), '你好呀！')
        system, user = client.complete_text.call_args.args
        self.assertNotIn('configured-alias', system)
        self.assertNotIn('本次没有附带图片', system)
        self.assertNotIn('先发图', system)
        self.assertIn('不要顺带回答 previous', system)
        self.assertEqual(len(json.loads(user)['previous']), 2)
        self.assertNotIn('images', client.complete_text.call_args.kwargs)

    def test_personality_text_preserves_cancellation_check(self):
        current = [True]
        def change(*_):
            current[0] = False
            return '旧的回答'
        self.client.complete_text = change
        wrapped = PersonalityLLM(self.client, PersonalityState('test-room'), current=lambda: current[0])
        self.assertTrue(wrapped.supports_plain_text)
        with self.assertRaises(PersonalityExpired):
            wrapped.complete_text('chat', 'hi')

    def test_snarky_chat_removes_default_length_pressure_without_truncating_real_answers(self):
        answer = '原价是一百元。八折是原价乘以零点八，因此用八十除以零点八即可。'
        self.client.complete_text = Mock(return_value=answer)
        wrapped = PersonalityLLM(self.client, PersonalityState('test-room', '毒舌损友'), current=lambda: True)
        snapshot = SimpleNamespace(day='2026-11-04', speaker_name='群友', messages=(),
                                   current='八折后80元，原价多少？请解释算法。')
        self.assertEqual(Dialogue(wrapped).reply(snapshot), answer)
        prompt = self.client.complete_text.call_args.args[0]
        self.assertNotIn('50–150', prompt)
        self.assertNotIn('不超过 20 个字', prompt)
        self.assertIn('直接输出给群友的最终文字回答', prompt)

    def test_casual_reaction_preserves_previous_reply_and_topic_change_drops_it(self):
        self.client.complete_text = Mock(return_value='行，收住。')
        wrapped = PersonalityLLM(self.client, PersonalityState('test-room', '毒舌损友'), current=lambda: True)
        snapshot = SimpleNamespace(day='2026-11-04', speaker_name='群友',
            messages=({'received_at': '2026-11-04T16:00:00+08:00', 'text': '不想上班'},),
            turns=({'question': '不想上班', 'reply': '先前那个尬笑话'},), current='太土了')
        self.assertEqual(Dialogue(wrapped).reply(snapshot, use_context=True), '行，收住。')
        payload = json.loads(self.client.complete_text.call_args.args[1])
        self.assertEqual(payload['current'], '太土了')
        self.assertEqual(payload['recent_turns'][0]['reply'], '先前那个尬笑话')
        snapshot.current = '换个话题，80除以0.8是多少'
        Dialogue(wrapped).reply(snapshot, use_context=False)
        payload = json.loads(self.client.complete_text.call_args.args[1])
        self.assertEqual(payload['previous'], [])
        self.assertNotIn('recent_turns', payload)
