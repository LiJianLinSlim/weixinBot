"""智能意图触发只用合成群消息，不连接微信。"""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import test_chat_summary as daily
from plugins.mention_chat import Plugin, smart_intent
from wxbot_framework.sdk import Message


class SmartReplyTests(unittest.TestCase):
    new_store = daily.DailySummaryTests.new_store
    model = daily.DailySummaryTests.model
    def setUp(self):
        daily.DailySummaryTests.setUp(self)
        options = json.loads(self.path.read_text(encoding='utf-8'))
        options['smart_reply'] = True
        self.path.write_text(json.dumps(options), encoding='utf-8')
        self.store.close()
        self.store = self.new_store()
        self.ctx._members = self.store
        self.ctx._llm = Mock(complete_json=Mock(return_value={'reply': '我来接'}))
        self.ctx._speech = None
        self.ctx.reply = lambda message, text, delivery_guard=None: self.backend.sent.append((message.chat_id, text)) or True
        self.plugin = Plugin()
        self.plugin.on_load(self.ctx)
        self.plugin.decision_router = SimpleNamespace(decide=Mock(return_value=
            SimpleNamespace(use_context=False, search=False, source='synthetic')))

    tearDown = daily.DailySummaryTests.tearDown

    def message(self, text, sequence=1, sender=None):
        sender = sender or daily.CAT
        return Message(text, daily.ROOM, sender_id=sender, is_group=True,
                       session_id='smart', sequence=sequence, data={})

    def test_local_intent_rules_are_conservative(self):
        yes = ('小管家你觉得这件事怎么办', '机器人 解释一下这个概念',
               '小管家 怎么设置定时任务', '小助手你觉得今天吃什么？', 'ZLLJ 帮我看看这个',
               '小管家', '嘿 小管家 在吗', 'zllj你好')
        no = ('今天下雨了', '我觉得这电影不错', '你们先聊', '有人知道几点了吗', '/清空', '成语接龙',
              '我突然发现，子安这张是不是去年你俩在惠州给拍的?',
              '怎么设置定时任务', '你觉得今天吃什么？', '帮我看看这个', '为什么今天还没来',
              '能不能帮我带杯水', '请问你到哪了？', '你俩周末去哪玩？', '何意味，我没叫他',
              '这个机器人又乱回复', '刚才小管家说了什么', '机器人又抢话了', '小管家的回复太长',
              '机器人说得不对', '“小管家 帮我查一下”这句话是例子', 'ZLLJ123今天怎么没来',
              '@群友\u2005小管家这事你怎么看', '@群友 小管家帮我看看')
        for text in yes:
            with self.subTest(text=text): self.assertTrue(smart_intent(text))
        for text in no:
            with self.subTest(text=text): self.assertFalse(smart_intent(text))

    def test_smart_message_without_at_replies_but_ordinary_chat_stays_silent(self):
        ordinary = self.message('今天下雨了')
        self.assertTrue(self.store.record(ordinary))
        self.plugin.on_message(ordinary, self.ctx)
        self.assertEqual(self.ctx._llm.complete_json.call_count, 0)
        question = self.message('小管家你觉得今天吃什么？', sequence=2)
        self.assertTrue(self.store.record(question))
        self.plugin.on_message(question, self.ctx)
        self.assertEqual(self.ctx._llm.complete_json.call_count, 1)
        self.assertEqual(self.backend.sent[-1], (daily.ROOM, '我来接'))

    def test_smart_mode_can_be_disabled_without_changing_real_at(self):
        self.store.smart_reply = False
        message = self.message('小管家你觉得今天吃什么？')
        self.store.record(message)
        self.plugin.on_message(message, self.ctx)
        self.assertEqual(self.ctx._llm.complete_json.call_count, 0)
        mentioned = self.message('问题', sequence=2)
        mentioned.data.update(is_at_self=True, at_members=(self.store.account,))
        self.store.record(mentioned)
        self.plugin.on_message(mentioned, self.ctx)
        self.assertEqual(self.ctx._llm.complete_json.call_count, 1)

    def test_screenshot_and_generic_questions_never_reach_model_or_search(self):
        for seq, text in enumerate(('我突然发现，子安这张是不是去年你俩在惠州给拍的?',
                                    '你觉得今天吃什么？', '为什么他还没来', '帮我拿一下'), 1):
            message = self.message(text, sequence=seq)
            self.store.record(message)
            self.plugin.on_message(message, self.ctx)
        self.plugin.decision_router.decide.assert_not_called()
        self.ctx._llm.complete_json.assert_not_called()
        self.assertEqual(self.backend.sent, [])

    def test_other_member_mentions_and_quotes_do_not_trigger_smart_reply(self):
        for seq, data in enumerate(({'at_members': (daily.OTHER,)},
                                   {'quoted_message': {'sender_id': daily.OTHER}}), 1):
            message = self.message('小管家 帮我看看', sequence=seq)
            message.data.update(data)
            self.store.record(message)
            self.plugin.on_message(message, self.ctx)
        self.plugin.decision_router.decide.assert_not_called()
        self.assertEqual(self.backend.sent, [])

    def test_real_bot_mention_still_works_when_quoting_another_member(self):
        message = self.message('你怎么看', sequence=1)
        message.data.update(is_at_self=True, at_members=(self.store.account,),
                            quoted_message={'sender_id': daily.OTHER})
        self.store.record(message)
        self.plugin.reply_to_member = Mock()
        self.plugin.on_message(message, self.ctx)
        self.plugin.reply_to_member.assert_called_once()


if __name__ == '__main__': unittest.main()
