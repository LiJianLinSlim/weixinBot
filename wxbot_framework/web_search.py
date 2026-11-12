"""有界公开网页搜索：只访问固定搜索入口，不抓取结果网址或执行网页脚本。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .llm import LLMError
from .members import BEIJING
from .mentions import strip_at_labels


class SearchError(LLMError):
    pass


def search_query(text):
    """规范化搜索词；是否搜索由独立决策层判断。"""
    text = strip_at_labels(text).strip()
    if not text or re.match(r'^(?:不要|不用|无需|别)(?:联网|搜索|查)', text):
        return None
    explicit = re.match(r'^(?:(?:请|帮我|麻烦|给我)\s*)?'
                        r'(?:联网搜索|上网搜索|联网查|搜索一下|搜索|搜一下|搜搜|搜|查询一下|查询|查一下|查查|查下)'
                        r'[：:\s]*(.*)$', text)
    query = explicit.group(1).strip() if explicit else text
    if not query:
        raise SearchError('联网搜索缺少关键词，请说明要查的节目、人物或问题')
    if len(query) > 240 or '\n' in query or any(ord(c) < 32 for c in query):
        raise SearchError('联网搜索关键词过长，请缩短本次问题')
    if re.search(r'(?i)(?:sk-[A-Za-z0-9_-]{10,}|bearer\s+\S+|api[_ -]?key\s*[:=])', query):
        raise SearchError('联网搜索关键词疑似包含密钥，请移除后重试')
    query = re.sub(r'^(?:请|帮我|麻烦|给我)?(?:再次|再)?(?:锐评|点评|评价|评一评|分析)(?:一下|下)?', '', query).strip()
    query = query.replace('《', ' ').replace('》', ' ')
    query = re.sub(r'(第.{1,4}季)的(?=嘉宾)', r'\1 ', query)
    query = re.sub(r'怎么样[？?]?$', '', query)
    query = re.sub(r'表现如何[？?]?$', ' 表现', query).strip('？? ')
    query = re.sub(r'\s+', ' ', query)
    return query


@dataclass(frozen=True)
class SearchResult:
    title: str
    snippet: str
    source: str


@dataclass(frozen=True)
class SearchBundle:
    query: str
    retrieved_at: str
    provider: str
    results: tuple[SearchResult, ...]

    def prompt_data(self):
        # 不把网址注入模型，群内只要结论；保留站点名用于判断来源可靠性。
        return {'query': self.query, 'retrieved_at': self.retrieved_at, 'provider': self.provider,
                'results': [{'title': r.title, 'snippet': r.snippet, 'source': r.source}
                            for r in self.results]}


def _text(value, limit):
    return re.sub(r'\s+', ' ', value).strip()[:limit]


class _Results(HTMLParser):
    def __init__(self, provider):
        super().__init__(convert_charrefs=True)
        self.provider = provider
        self.stack = []
        self.current = None
        self.skip = 0
        self.rows = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get('class', '').split()
        if tag in ('script', 'style', 'noscript'):
            self.skip += 1
        if tag in ('br', 'img', 'input', 'meta', 'link', 'hr', 'source', 'wbr', 'area', 'embed'):
            return
        self.stack.append((tag, classes))
        if self.current is None and ((self.provider == 'sogou' and tag == 'div' and
                ('vrwrap' in classes or 'rb' in classes)) or
                (self.provider == 'bing' and tag == 'li' and 'b_algo' in classes) or
                (self.provider == 'so360' and tag == 'li' and 'res-list' in classes)):
            self.current = {'depth': len(self.stack), 'title': [], 'snippet': [], 'href': ''}
        if self.current and tag == 'a':
            title_tag = 'h2' if self.provider == 'bing' else 'h3'
            if any(t == title_tag for t, _ in self.stack) and not self.current['href']:
                self.current['href'] = attrs.get('data-mdurl') or attrs.get('href', '')

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript'):
            self.skip = max(0, self.skip - 1)
        position = next((i for i in range(len(self.stack)-1, -1, -1) if self.stack[i][0] == tag), None)
        if position is None:
            return
        if self.current and position < self.current['depth']:
            self._finish()
        del self.stack[position:]

    def handle_data(self, value):
        if not self.current or self.skip:
            return
        title_tag = 'h2' if self.provider == 'bing' else 'h3'
        if any(tag == title_tag for tag, _ in self.stack):
            self.current['title'].append(value)
        elif any(tag == 'p' or any(c in ('ft', 'fz-mid', 'space-txt', 'b_caption', 'w-content')
                                  or c.startswith('str-text') for c in classes) for tag, classes in self.stack):
            if sum(map(len, self.current['snippet'])) < 2000:
                self.current['snippet'].append(value)

    def _finish(self):
        row, self.current = self.current, None
        title = _text(' '.join(row['title']), 180)
        snippet = _text(' '.join(row['snippet']), 900)
        href = row['href']
        if href.startswith('/') and self.provider == 'sogou':
            href = urljoin('https://www.sogou.com', href)
        try:
            target = urlsplit(href)
            if target.scheme not in ('https', 'http') or not target.hostname or target.username or target.password:
                return
        except ValueError:
            return
        if title and snippet and len(self.rows) < 12:
            self.rows.append(SearchResult(title, snippet, target.hostname))


def parse_results(body, provider):
    if not isinstance(body, bytes) or len(body) > 2 * 1024 * 1024:
        raise SearchError('联网搜索响应过大')
    parser = _Results(provider)
    try:
        parser.feed(body.decode('utf-8', errors='replace'))
        parser.close()
    except Exception:
        raise SearchError('联网搜索结果解析失败') from None
    return parser.rows


def _relevant(query, result):
    value = (result.title + ' ' + result.snippet).lower()
    tokens = re.findall(r'[a-zA-Z][a-zA-Z0-9_-]{1,}|[\u3400-\u9fff]+', query.lower())
    chinese = []
    english = []
    for token in tokens:
        if token.isascii():
            if token not in ('search', 'please', 'the', 'is'):
                english.append(token)
        else:
            chinese.extend(token[i:i+2] for i in range(len(token)-1))
    if english and not any(token in value for token in english):
        return False
    if chinese and sum(token in value for token in set(chinese)) < min(2, len(set(chinese))):
        return False
    return True


class _Redirects(HTTPRedirectHandler):
    def __init__(self, host):
        self.host = host
        self.count = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.count += 1
        target = urlsplit(urljoin(req.full_url, newurl))
        allowed = {self.host, 'sogou.com'} if self.host == 'www.sogou.com' else {self.host}
        if (self.count > 2 or target.scheme != 'https' or target.hostname not in allowed or
                target.port not in (None, 443) or target.username or target.password):
            raise SearchError('联网搜索发生非预期跳转')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class WebSearch:
    """固定服务端点，无 Cookie、微信凭据或模型密钥；不绕过验证码。"""
    def __init__(self, config_file=None):
        self.config_file = Path(config_file) if config_file is not None else None
        self._cache = {}

    def _options(self):
        try:
            data = (json.loads(self.config_file.read_text(encoding='utf-8-sig'))
                    if self.config_file is not None and self.config_file.exists() else {})
            if not isinstance(data, dict) or data.get('provider', 'free') not in ('free', 'tavily', 'tavily-keyless', 'brave'):
                raise ValueError
            if type(data.get('enabled', True)) is not bool:
                raise ValueError
            return data
        except Exception:
            raise SearchError('联网搜索配置无效，请检查本机 search_config.json') from None

    def _api(self, query, options):
        provider = options['provider']
        key = options.get('api_key', '')
        if provider != 'tavily-keyless' and (not isinstance(key, str) or not key.strip()):
            raise SearchError('联网搜索服务未配置密钥，请在本机配置后重试')
        tavily = provider in ('tavily', 'tavily-keyless')
        if tavily:
            url = 'https://api.tavily.com/search'
            payload = {'query': query, 'max_results': 6, 'search_depth': 'basic',
                       'include_answer': False, 'include_raw_content': False}
            # 与参考方案相同，只对明确实时问题加时间过滤，不把历史节目季数过滤掉。
            if re.search(r'今天|今日|实时|现在', query):
                payload['time_range'] = 'd'
            elif re.search(r'最新|近期|最近|本周', query):
                payload['time_range'] = 'w'
            data = json.dumps(payload).encode('utf-8')
            headers = {'Content-Type': 'application/json'}
            if provider == 'tavily-keyless':
                headers['X-Tavily-Access-Mode'] = 'keyless'
            else:
                headers['Authorization'] = 'Bearer ' + key
            req = Request(url, data=data, headers=headers, method='POST')
        else:
            url = 'https://api.search.brave.com/res/v1/web/search?' + urlencode({'q': query, 'count': 6})
            req = Request(url, headers={'X-Subscription-Token': key, 'Accept': 'application/json'})

        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, *_args, **_kwargs):
                return None
        try:
            with build_opener(NoRedirect()).open(req, timeout=12) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError
            result = json.loads(raw)
            rows = result.get('results', []) if tavily else result.get('web', {}).get('results', [])
            parsed = self._normalize(rows, 'url', 'content' if tavily else 'description')
            if not parsed:
                raise ValueError
            return self._bundle(query, provider, parsed)
        except HTTPError as error:
            error.close()
            if error.code == 429:
                raise SearchError('联网搜索服务限流，请稍后重试或配置独立搜索密钥') from None
            raise SearchError('联网搜索服务调用失败，请检查搜索服务或稍后重试') from None
        except Exception:
            raise SearchError('联网搜索服务调用失败，请检查本机搜索密钥或稍后重试') from None

    @staticmethod
    def _normalize(rows, url_field='href', text_field='body'):
        if not isinstance(rows, list):
            return ()
        results = []
        for row in rows[:12]:
            if not isinstance(row, dict) or not all(isinstance(row.get(k), str) for k in ('title', url_field, text_field)):
                continue
            try:
                parsed = urlsplit(row[url_field])
                if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
                    continue
                results.append(SearchResult(_text(row['title'], 180), _text(row[text_field], 900), parsed.hostname))
            except ValueError:
                continue
        return tuple(results)

    def _bundle(self, query, provider, results):
        matches = tuple(row for row in results if _relevant(query, row))[:6]
        if not matches:
            raise SearchError('联网搜索结果与本次问题不相关')
        bundle = SearchBundle(query, datetime.now(BEIJING).isoformat(timespec='seconds'), provider, matches)
        self._cache[query] = (time.monotonic(), bundle)
        if len(self._cache) > 64:
            self._cache.pop(next(iter(self._cache)))
        return bundle

    def search(self, query):
        if not isinstance(query, str) or not 1 <= len(query) <= 240:
            raise SearchError('联网搜索关键词长度无效')
        options = self._options()
        if not options.get('enabled', True):
            raise SearchError('联网搜索服务尚未连接')
        cached = self._cache.get(query)
        if cached is not None and time.monotonic() - cached[0] < 120:
            return cached[1]
        if options.get('provider', 'free') != 'free':
            return self._api(query, options)
        try:
            result = subprocess.run(
                [sys.executable, '-X', 'utf8', str(Path(__file__).resolve().parents[1] / 'tools/search_worker.py')],
                input=json.dumps({'query': query}, ensure_ascii=False).encode('utf-8'),
                capture_output=True, timeout=10, check=True,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if len(result.stdout) > 65536:
                raise ValueError
            rows = json.loads(result.stdout)
            return self._bundle(query, 'ddgs', self._normalize(rows))
        except Exception:
            pass
        deadline = time.monotonic() + 18
        providers = (('so360', 'www.so.com', '/s', {'q': query}),
                     ('sogou', 'www.sogou.com', '/web', {'query': query}),
                     ('bing', 'www.bing.com', '/search', {'q': query, 'mkt': 'zh-CN'}))
        for name, host, path, params in providers:
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                url = 'https://' + host + path + '?' + urlencode(params)
                req = Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'text/html',
                                           'Accept-Encoding': 'identity'})
                with build_opener(_Redirects(host)).open(req, timeout=min(7, remaining)) as response:
                    if response.status != 200 or response.headers.get_content_type() != 'text/html':
                        continue
                    if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
                        continue
                    chunks, total = [], 0
                    read = getattr(response, 'read1', response.read)
                    while True:
                        if time.monotonic() >= deadline:
                            raise SearchError('联网搜索超时')
                        chunk = read(min(65536, 2 * 1024 * 1024 + 1 - total))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        total += len(chunk)
                        if total > 2 * 1024 * 1024:
                            raise SearchError('联网搜索响应过大')
                rows = parse_results(b''.join(chunks), name)
                matches = tuple(row for row in rows if _relevant(query, row))[:6]
                if matches:
                    return self._bundle(query, name, matches)
            except Exception:
                # 不回显 URL、查询正文、响应和网络错误中的凭据；失败不让模型编造。
                continue
        raise SearchError('联网搜索未取得相关结果，可能暂时受限，请稍后重试或补充具体关键词')


def without_links(text):
    text = re.sub(r'\[([^]\n]+)\]\(https?://[^)\s]+\)', r'\1', text, flags=re.I)
    text = re.sub(r'(?:https?://|www\.)[^\s<>，。！？；）】]+', '', text, flags=re.I)
    text = re.sub(r'\[\d{1,2}\]', '', text)
    text = re.sub(r'(?m)^\s*(?:来源链接|参考链接|链接)[:：]\s*$', '', text)
    return text.strip()
