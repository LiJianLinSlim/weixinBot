"""真实搜索/模型调用和插件链路检查；发送接口用内存替身，不写微信或成员数据库。"""
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plugins.mention_chat import Plugin
from wxbot_framework.decision_router import DecisionRouter
from wxbot_framework.llm import LLMClient, LLMError
from wxbot_framework.personality import PersonalityLLM, PersonalityState
from wxbot_framework.sdk import Message
from wxbot_framework.web_search import WebSearch


def main():
    reports = []
    for current in ('你好啊', '那上海呢'):
        base = LLMClient.from_file(ROOT / 'llm_config.json')
        state = PersonalityState('synthetic-test', '嘴臭')
        wrapped = PersonalityLLM(base, state, current=lambda: True)
        plugin = Plugin()
        plugin.members = SimpleNamespace(display_name=lambda *args: '测试用户')
        router = DecisionRouter(ROOT / 'routing_config.json')
        plugin.decision_router = SimpleNamespace(decide=Mock(wraps=router.decide))
        web = WebSearch(ROOT / 'search_config.json')
        plugin.web_search = SimpleNamespace(search=Mock(wraps=web.search))
        ctx = SimpleNamespace(logger=Mock(), recent_images=None, llm_for=lambda *args, **kw: wrapped)
        snapshot = SimpleNamespace(current=current, day='2026-11-03', speaker_name='测试用户', turns=(),
            messages=({'text': '北京明天天气怎么样', 'received_at': '2026-11-03T08:00:00+00:00'},))
        msg = Message(current, '123@chatroom', 'synthetic-user', is_group=True)
        start = time.monotonic()
        report = {'current': current, 'sent_to_group': False}
        try:
            with patch('plugins.mention_chat.deliver_model_reply') as deliver:
                plugin.reply_to_member(msg, ctx, snapshot, state, Mock())
                report['answer'] = deliver.call_args.args[2]
        except LLMError as error:
            report['error'] = str(error)
        # 获取同一次真实决策的结果，避免再发一轮请求。
        for call in ctx.logger.info.call_args_list:
            if call.args[0].startswith('问答决策'):
                report.update(decision_model=call.args[1], search=call.args[2], context=call.args[3])
        report.update(search_calls=plugin.web_search.search.call_count, actual_answer_model=base.response_model,
                      elapsed=round(time.monotonic()-start, 2))
        reports.append(report)
    (ROOT / 'logs/cloud-routing-verification.json').write_text(
        json.dumps(reports, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(reports, ensure_ascii=False))


if __name__ == '__main__':
    main()
