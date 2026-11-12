"""比较现有云端决策模型，不连接微信或向群发送消息。"""
import concurrent.futures
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wxbot_framework.decision_router import DecisionRouter
from wxbot_framework.llm import LLMClient

CASES = [
    ('greeting', '你好啊', ['点评某节目的嘉宾'], False, False),
    ('weather', '北京明天天气怎么样', [], True, False),
    ('followup', '那上海呢', ['北京明天天气怎么样'], True, True),
    ('new_topic', 'Python的列表和元组有什么区别', ['北京明天天气怎么样'], False, False),
]
EXTRA_CASES = [
    ('rewrite_previous', '把刚才那段改成一句话', ['今天我骑车十公里，慢慢找回了运动状态'], False, True),
    ('image_only', '这张截图的报错是什么意思', ['北京明天天气怎么样'], False, False),
    ('named_subject', '湖南卫视综艺节目的嘉宾啊', ['点评一下文吉儿'], True, True),
    ('opt_out', '不要联网，解释一下量子纠缠', ['点评某节目嘉宾'], False, False),
    ('explicit_search', '搜索一下Rust最新版本', ['北京明天天气怎么样'], True, False),
]


def probe(model):
    client = LLMClient.from_file(ROOT / 'llm_config.json')
    reports = []
    with tempfile.TemporaryDirectory() as folder:
        config = Path(folder) / 'routing.json'
        options = {'model': model, 'timeout_seconds': 6}
        effort = next((a.split('=', 1)[1] for a in sys.argv if a.startswith('--effort=')), None)
        if effort is not None:
            options['reasoning_effort'] = effort
        config.write_text(json.dumps(options), encoding='utf-8')
        router = DecisionRouter(config)
        for kind, current, history, search, context in (CASES + EXTRA_CASES if '--extended' in sys.argv else CASES):
            snapshot = SimpleNamespace(current=current, day='2026-11-03', turns=(),
                messages=tuple({'text': t} for t in history))
            start = time.monotonic()
            decision = router.decide(client, snapshot, has_image=kind == 'image_only')
            reports.append({'case': kind, 'search': decision.search, 'context': decision.use_context,
                'query': decision.query, 'source': decision.source, 'elapsed': round(time.monotonic()-start, 2),
                'correct': decision.search == search and decision.use_context == context})
    return {'model': model, 'cases': reports}


if __name__ == '__main__':
    requested = next((a.split('=', 1)[1] for a in sys.argv if a.startswith('--model=')), None)
    models = [requested] if requested else (['GLM-5.3-Flash'] if '--extended' in sys.argv else ['gpt-6-luna', 'GLM-5.3-Flash'])
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        reports = list(pool.map(probe, models))
    filename = ('decision-model-selected.json' if requested else
                ('decision-model-extended.json' if '--extended' in sys.argv else 'decision-model-probes.json'))
    (ROOT / 'logs' / filename).write_text(
        json.dumps(reports, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(reports, ensure_ascii=False))
