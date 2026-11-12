import base64
import io
import json
import threading
import unittest
from urllib.error import HTTPError, URLError
from unittest.mock import patch

from wxbot_framework.llm import LLMClient, LLMError, ModelUnavailable


def response(text='正常回答', **changes):
    return io.BytesIO(json.dumps({'choices': [{'message': {'content': text}, **changes}]}).encode())


def http_error(code):
    return HTTPError('https://example.com', code, 'server', {}, io.BytesIO(b'private-upstream-body'))


class FallbackTests(unittest.TestCase):
    def client(self, **changes):
        return LLMClient({'api_key': 'test-only', 'base_url': 'https://gateway.example/v1',
                          'model': 'primary', 'timeout_seconds': 60,
                          'fallback_models': [{'model': 'vision-backup', 'vision': True},
                                              {'model': 'text-backup', 'vision': False}], **changes})

    def test_primary_failure_uses_backup_without_mutating_primary_or_endpoint(self):
        client = self.client()
        with patch('wxbot_framework.llm.urlopen', side_effect=[http_error(502), response()]) as call:
            self.assertEqual(client.complete_text('system', 'question'), '正常回答')
        bodies = [json.loads(c.args[0].data) for c in call.call_args_list]
        self.assertEqual([b['model'] for b in bodies], ['primary', 'vision-backup'])
        self.assertEqual(bodies[0]['messages'], bodies[1]['messages'])
        self.assertEqual(client.model, 'primary')
        self.assertEqual(client.response_model, 'vision-backup')
        self.assertEqual(call.call_args_list[0].args[0].full_url, call.call_args_list[1].args[0].full_url)

    def test_success_stops_chain_and_next_question_starts_with_primary(self):
        client = self.client()
        with patch('wxbot_framework.llm.urlopen', side_effect=[http_error(502), response('备用回答'), response('主模型恢复')]) as call:
            self.assertEqual(client.complete_text('system', 'first'), '备用回答')
            self.assertEqual(client.complete_text('system', 'second'), '主模型恢复')
        self.assertEqual([json.loads(c.args[0].data)['model'] for c in call.call_args_list],
                         ['primary', 'vision-backup', 'primary'])
        self.assertEqual(client.response_model, 'primary')

    def test_images_never_use_text_only_backup_or_strip_image(self):
        client = self.client()
        url = 'data:image/jpeg;base64,' + base64.b64encode(b'\xff\xd8\xfftest').decode()
        with patch('wxbot_framework.llm.urlopen', side_effect=[http_error(502), http_error(503)]) as call:
            with self.assertRaises(ModelUnavailable):
                client.complete_text('system', 'image question', images=(url,))
        bodies = [json.loads(c.args[0].data) for c in call.call_args_list]
        self.assertEqual([b['model'] for b in bodies], ['primary', 'vision-backup'])
        self.assertEqual(bodies[0]['messages'][1]['content'], bodies[1]['messages'][1]['content'])
        self.assertEqual(bodies[1]['messages'][1]['content'][1]['image_url']['url'], url)

    def test_unverified_backup_is_skipped_for_images(self):
        client = self.client(fallback_models=[{'model': 'text-only'}])
        url = 'data:image/jpeg;base64,' + base64.b64encode(b'\xff\xd8\xfftest').decode()
        with patch('wxbot_framework.llm.urlopen', side_effect=http_error(502)) as call:
            with self.assertRaises(ModelUnavailable):
                client.complete_text('system', 'image question', images=(url,))
        self.assertEqual(call.call_count, 1)

    def test_all_candidates_fail_only_three_attempts_and_safe_error(self):
        client = self.client()
        with patch('wxbot_framework.llm.urlopen', side_effect=[http_error(502), http_error(503), http_error(504)]) as call:
            with self.assertRaises(ModelUnavailable) as error:
                client.complete_text('system', 'question')
        self.assertEqual(call.call_count, 3)
        self.assertIn('备用', str(error.exception))
        self.assertNotIn('private', str(error.exception))
        self.assertIsNone(client.response_model)

    def test_auth_quota_bad_request_and_content_refusal_do_not_fall_back(self):
        for code in (400, 401, 402, 403, 404, 429):
            with self.subTest(code=code), patch('wxbot_framework.llm.urlopen', side_effect=http_error(code)) as call:
                with self.assertRaises(LLMError):
                    self.client().complete_text('system', 'question')
                self.assertEqual(call.call_count, 1)
        for reply in (response('拒绝', finish_reason='content_filter'), response('部分结果', finish_reason='length')):
            with patch('wxbot_framework.llm.urlopen', return_value=reply) as call:
                with self.assertRaises(LLMError):
                    self.client().complete_text('system', 'question')
                self.assertEqual(call.call_count, 1)

    def test_connection_timeout_and_empty_final_answer_can_fall_back(self):
        for failure in (TimeoutError('private'), URLError('private'), response(None)):
            with self.subTest(failure=type(failure).__name__), patch('wxbot_framework.llm.urlopen',
                                                                    side_effect=[failure, response()]) as call:
                self.assertEqual(self.client().complete_text('system', 'question'), '正常回答')
                self.assertEqual(call.call_count, 2)

    def test_shared_deadline_does_not_multiply_timeout(self):
        client = self.client()
        now, limits = [0], []
        def send(request, timeout):
            limits.append(timeout)
            now[0] += timeout
            raise TimeoutError('private')
        with patch('wxbot_framework.llm.time.monotonic', side_effect=lambda: now[0]), \
                patch('wxbot_framework.llm.urlopen', side_effect=send) as call:
            with self.assertRaises(ModelUnavailable):
                client.complete_text('system', 'question')
        self.assertEqual(limits, [20, 20, 20])
        self.assertEqual(now[0], 60)
        self.assertEqual(call.call_count, 3)

    def test_short_internal_call_budget_does_not_change_answer_budget(self):
        client = self.client()
        now = [0]
        def send(request, timeout):
            now[0] += timeout
            raise TimeoutError('offline')
        with patch('wxbot_framework.llm.time.monotonic', side_effect=lambda: now[0]), \
                patch('wxbot_framework.llm.urlopen', side_effect=send):
            with self.assertRaises(ModelUnavailable):
                client.complete_text('system', 'question', timeout_seconds=5)
        self.assertAlmostEqual(now[0], 5)
        self.assertEqual(client.timeout, 60)

    def test_short_budget_applies_without_fallback_and_rejects_invalid_values(self):
        client = self.client(fallback_models=[])
        with patch('wxbot_framework.llm.urlopen', return_value=response()) as send:
            client.complete_text('system', 'question', timeout_seconds=5)
        self.assertLessEqual(send.call_args.kwargs['timeout'], 5)
        for value in (True, 0, 61, float('nan')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                client.complete_text('system', 'question', timeout_seconds=value)

    def test_model_identity_prompt_uses_model_that_actually_answered(self):
        client = self.client()
        system = '身份\n运行配置提供的模型标识为："primary"。无法核验实际后端。'
        with patch('wxbot_framework.llm.urlopen', side_effect=[http_error(502), response('vision-backup')]) as call:
            client.complete_text(system, '你是什么模型')
        final = json.loads(call.call_args.args[0].data)
        self.assertIn('"vision-backup"', final['messages'][0]['content'])
        self.assertNotIn('"primary"', final['messages'][0]['content'])

    def test_slower_backup_can_have_larger_budget_within_same_total_deadline(self):
        client = self.client(fallback_models=[{'model': 'gpt-backup', 'vision': True, 'timeout_seconds': 30},
                                              {'model': 'vision-backup', 'vision': True}])
        now, limits = [0], []
        def send(request, timeout):
            limits.append(timeout)
            now[0] += timeout
            raise TimeoutError('private')
        with patch('wxbot_framework.llm.time.monotonic', side_effect=lambda: now[0]), \
                patch('wxbot_framework.llm.urlopen', side_effect=send):
            with self.assertRaises(ModelUnavailable):
                client.complete_text('system', 'question')
        self.assertEqual(limits, [20, 30, 10])
        self.assertEqual(now[0], 60)

    def test_last_response_model_is_thread_local(self):
        client = self.client()
        client._response_state.model = 'main-thread'
        values = []
        thread = threading.Thread(target=lambda: (values.append(client.response_model),
                                                  setattr(client._response_state, 'model', 'child')))
        thread.start()
        thread.join()
        self.assertEqual(values, [None])
        self.assertEqual(client.response_model, 'main-thread')

    def test_config_rejects_unbounded_or_invalid_models_and_deduplicates(self):
        for value in (['not-object'], [{'model': ''}], [{'model': 'x', 'vision': 'yes'}],
                      [{'model': 'x', 'url': 'https://other.example'}], [{'model': 'x'}] * 3):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.client(fallback_models=value)
        client = self.client(fallback_models=[{'model': 'primary'}, {'model': 'vision-backup'}])
        self.assertEqual(client.fallback_models, [{'model': 'vision-backup', 'vision': False}])

    def test_invalid_image_does_not_fall_back_or_send_request(self):
        with patch('wxbot_framework.llm.urlopen') as send, self.assertRaises(LLMError):
            self.client().complete_text('system', 'question', images=('https://private.example/image',))
        send.assert_not_called()
