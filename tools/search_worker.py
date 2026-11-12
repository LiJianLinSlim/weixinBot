"""隔离第三方搜索库，主进程可在超时后终止，不把底层错误带进群聊。"""
import json
import logging
import sys

logging.disable(logging.CRITICAL)

if __name__ == '__main__':
    try:
        data = json.loads(sys.stdin.buffer.read(4096))
        query = data['query']
        if not isinstance(query, str) or not 1 <= len(query) <= 240:
            raise ValueError
        from ddgs import DDGS
        with DDGS(timeout=4) as client:
            rows = client.text(query, region='cn-zh', max_results=6,
                               backend='brave,startpage,duckduckgo')
        rows = [{key: str(row.get(key, ''))[:limit] for key, limit in
                 (('title', 180), ('href', 2048), ('body', 900))} for row in rows[:6]]
        sys.stdout.buffer.write(json.dumps(rows, ensure_ascii=False).encode('utf-8'))
    except Exception:
        sys.stdout.write('[]')
