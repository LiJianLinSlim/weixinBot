"""用合成输入比较决策通道和思考强度；仅记录白名单响应指标，不输出认证或思考正文。"""
import concurrent.futures
import json
from pathlib import Path
import statistics
import sys
import time
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wxbot_framework.decision_router import DecisionRouter, SYSTEM
from wxbot_framework.llm import LLMClient

CASES = [
    ('你好啊', ['点评一下某节目的嘉宾'], False, False),
    ('那上海呢', ['北京明天天气怎么样'], True, True),
    ('把刚才那段改成一句话', ['今天我骑车十公里，慢慢找回了运动状态'], False, True),
]


def probe(option):
    model, effort = option[:2]
    token_limit = option[2] if len(option) > 2 else 512
    client = LLMClient.from_file(ROOT / 'llm_config.json')
    client._ensure_local_bridge()
    reports = []
    for current, history, expected_search, expected_context in CASES:
        payload = {'model': model, 'messages': [{'role': 'system', 'content': SYSTEM},
            {'role': 'user', 'content': json.dumps({'current': current, 'previous': history,
                'recent_turns': [], 'date': '2026-11-03', 'has_image': False}, ensure_ascii=False)}],
            client.token_limit_parameter: token_limit, 'stream': False}
        if effort is not None:
            payload['reasoning_effort'] = effort
        request = Request(client.endpoint, data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
            headers={'Authorization': 'Bearer ' + client._api_key, 'Content-Type': 'application/json'}, method='POST')
        start = time.monotonic()
        row = {'case': current}
        try:
            with urlopen(request, timeout=12) as response:
                data = json.loads(response.read(1024*1024))
            choice = data['choices'][0]
            message = choice['message']
            usage = data.get('usage') or {}
            row.update(status=200, finish=choice.get('finish_reason'),
                completion_tokens=usage.get('completion_tokens'),
                reasoning_tokens=(usage.get('completion_tokens_details') or {}).get('reasoning_tokens'))
            snapshot = SimpleNamespace(current=current, messages=tuple({'text': t} for t in history), turns=())
            try:
                decision = DecisionRouter._parse(message.get('content'), snapshot)
                row.update(valid=True, correct=decision.search == expected_search and decision.use_context == expected_context,
                           search=decision.search, context=decision.use_context, query=decision.query)
            except (ValueError, TypeError):
                row.update(valid=False, correct=False)
        except HTTPError as error:
            row.update(status=error.code, valid=False, correct=False)
            error.close()
        except (URLError, OSError, ValueError, KeyError, IndexError, TypeError):
            row.update(status='timeout-or-format', valid=False, correct=False)
        row['elapsed'] = round(time.monotonic()-start, 2)
        reports.append(row)
    valid_times = [r['elapsed'] for r in reports if r.get('valid')]
    return {'model': model, 'reasoning_effort': effort, 'token_limit': token_limit, 'cases': reports,
            'median_valid_seconds': statistics.median(valid_times) if valid_times else None}


if __name__ == '__main__':
    options = [('gpt-6-luna', 'low'), ('gpt-6-luna', 'none'),
               ('gpt-5.6-luna', 'low'), ('GLM-5.3-Flash', None), ('DeepSeek-V4.1-Flash', None)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        reports = list(pool.map(probe, options))
    (ROOT / 'logs/router-latency-benchmark.json').write_text(
        json.dumps(reports, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(reports, ensure_ascii=False))
