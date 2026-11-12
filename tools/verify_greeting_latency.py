"""本机合成问候探测：调用搜索和模型，不连接或发送微信。"""
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wxbot_framework.dialogue import Dialogue
from wxbot_framework.llm import LLMClient, LLMError
from wxbot_framework.search_context import contextual_search_query
from wxbot_framework.web_search import SearchError, WebSearch


def main():
    client = LLMClient(json.loads((ROOT / 'llm_config.json').read_text(encoding='utf-8-sig')))
    client.complete_text = Mock(wraps=client.complete_text)
    snapshot = SimpleNamespace(current='你好啊', speaker_name='测试用户', day='2026-11-03',
        messages=({'text': '某工具新版功能', 'received_at': '2026-11-03T08:00:00+00:00'},), turns=())
    start = time.monotonic()
    query = contextual_search_query(client, snapshot)
    planner_calls = client.complete_text.call_count
    evidence = None
    try:
        evidence = WebSearch(ROOT / 'search_config.json').search(query)
    except SearchError:
        pass
    try:
        answer = Dialogue(client).reply(snapshot, web_search=evidence)
        outcome = {'answer': answer, 'actual_model': client.response_model}
    except LLMError as error:
        outcome = {'error': str(error)}
    outcome.update(query=query, planner_calls=planner_calls,
        total_model_calls=client.complete_text.call_count,
        search_results=len(evidence.results) if evidence is not None else 0,
        elapsed=round(time.monotonic()-start, 1), sent_to_group=False)
    (ROOT / 'logs/search-latency-verification.json').write_text(
        json.dumps(outcome, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(outcome, ensure_ascii=False))


if __name__ == '__main__':
    main()
