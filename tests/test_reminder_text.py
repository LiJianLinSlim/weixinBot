import json
from unittest.mock import Mock, patch
import unittest

from wxbot_framework.llm import LLMError
from wxbot_framework.reminder_text import RepeatedReminderError, generate_reminder, is_repeated, needs_generation


class ReminderTextTests(unittest.TestCase):
    def test_fixed_only_descriptions_never_call_model(self):
        client = Mock()
        for description in ('', '  ', '只需要提醒', '只需要提醒。', '只需提醒', '仅提醒', '原样发送'):
            with self.subTest(description=description):
                self.assertFalse(needs_generation(description))
                self.assertEqual(generate_reminder(client, '该喝水了', description), '该喝水了')
        client.complete_text.assert_not_called()

    def test_custom_description_is_generic_and_previous_text_is_bounded(self):
        client = Mock()
        client.complete_text.return_value = '起来活动一下，给久坐的身体换个节奏。'
        result = generate_reminder(client, '起来活动一下', '用轻松的语气说一句活动的好处',
                                   previous=['1', '2', '3', '4'])
        self.assertIn('活动', result)
        system, user = client.complete_text.call_args.args
        data = json.loads(user)
        self.assertEqual(data['base_message'], '起来活动一下')
        self.assertEqual(data['supplement'], '用轻松的语气说一句活动的好处')
        self.assertEqual(data['previous'], ['1', '2', '3', '4'])
        self.assertIn('不能访问网页', system)
        self.assertIn('改变发送对象和时间', system)
        self.assertNotIn('api_key', user)

    def test_invalid_long_or_link_output_rejected_for_fixed_text_fallback(self):
        for answer in ('', '字' * 201, '请看 https://example.com', chr(96) * 3 + '生成代码', '\0bad'):
            client = Mock()
            client.complete_text.return_value = answer
            with self.subTest(answer=answer[:20]), self.assertRaises(LLMError):
                generate_reminder(client, '该喝水了', '说喝水好处')

    def test_invalid_description_is_rejected_before_model(self):
        client = Mock()
        for description in (None, 'a' * 2001):
            with self.subTest(description=type(description)), self.assertRaises(ValueError):
                generate_reminder(client, '该喝水了', description)
        client.complete_text.assert_not_called()

    def test_exact_punctuation_near_copy_and_reordered_sentences_are_rejected(self):
        old = '到点啦，记得喝口水。适量补水有助于身体正常代谢，也能让精神清爽一些。'
        for text in (old, old.replace('，', '！').replace('。', '～'),
                     old.replace('到点啦', '时间到啦'),
                     '适量补水有助于身体正常代谢，也能让精神清爽一些。到点啦，记得喝口水。'):
            with self.subTest(text=text):
                self.assertTrue(is_repeated(text, [old]))
        self.assertFalse(is_repeated('放下手头的事，小口喝点水，忙碌时也别忽略口渴的信号。', [old]))

    def test_repeated_candidate_regenerated_and_only_fresh_text_returned(self):
        old = '该喝水啦，给自己补点水分。'
        new = '忙碌间隙歇一歇，喝口水润润嗓子，杯子放近些更容易想起补水。'
        client = Mock(timeout=30)
        client.complete_text.side_effect = [old, new]
        self.assertEqual(generate_reminder(client, '该喝水了', '说一些好处', previous=[old]), new)
        self.assertEqual(client.complete_text.call_count, 2)
        second = json.loads(client.complete_text.call_args_list[1].args[1])
        self.assertEqual(second['rejected'], [old])
        self.assertLessEqual(client.complete_text.call_args_list[1].kwargs['timeout_seconds'], 30)

    def test_continuous_repetition_stops_after_three_calls(self):
        old = '喝水啦，给忙碌的自己留一点休息时间。'
        client = Mock(timeout=60)
        client.complete_text.return_value = old
        with self.assertRaises(RepeatedReminderError):
            generate_reminder(client, '喝水了', '语气轻松', previous=[old])
        self.assertEqual(client.complete_text.call_count, 3)

    def test_regeneration_shares_original_time_budget(self):
        old = '喝水啦，给忙碌的自己留一点休息时间。'
        client = Mock(timeout=20)
        client.complete_text.return_value = old
        with patch('wxbot_framework.reminder_text.time.monotonic', side_effect=[0, 1, 21]), self.assertRaises(RepeatedReminderError):
            generate_reminder(client, '喝水了', '语气轻松', previous=[old])
        self.assertEqual(client.complete_text.call_count, 1)
        self.assertEqual(client.complete_text.call_args.kwargs['timeout_seconds'], 19)
