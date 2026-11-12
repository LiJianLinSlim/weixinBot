import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from wxbot_framework.dialogue import Dialogue
from wxbot_framework.personality import PersonalityState
from wxbot_framework.sdk import Message
from wxbot_framework.web_search import (SearchBundle, SearchError, SearchResult, WebSearch,
                                         _Redirects, _relevant, parse_results, search_query, without_links)
from plugins.mention_chat import Plugin, failure_notice


def bundle():
    return SearchBundle('测试节目 第四季 嘉宾', '2026-11-03T16:00:00+08:00', 'test',
                        (SearchResult('测试节目 第四季阵容', '嘉宾甲在节目中担任主持；个人评价需要区分事实。', 'example.com'),))


class SearchTests(unittest.TestCase):
    def test_query_normalization_accepts_topics_and_respects_explicit_opt_out(self):
        for text in ('@机器人\u2005搜一下 Rust 生命周期', '查一下上海天气',
                     '点评《测试节目》第四季的嘉宾', '你怎么看《测试节目》的嘉宾', '今天新闻', '上海明天会下雨吗'):
            with self.subTest(text=text):
                self.assertTrue(search_query(text))
        for text in ('不用搜索，先跟我聊天', '不要联网，这句只聊天'):
            with self.subTest(text=text):
                self.assertIsNone(search_query(text))
        self.assertEqual(search_query('点评《测试节目》第四季的嘉宾'), '测试节目 第四季 嘉宾')
        self.assertEqual(search_query('北京明天天气怎么样'), '北京明天天气')

    def test_query_secrets_length_and_empty_rejected(self):
        for text in ('搜索', '搜索 ' + 'a' * 241, '搜索 api_key=private'):
            with self.subTest(text=text), self.assertRaises(SearchError):
                search_query(text)

    def test_named_person_sharp_opinion_triggers_but_self_or_previous_text_does_not(self):
        self.assertEqual(search_query('锐评一下文吉儿'), '文吉儿')
        self.assertEqual(search_query('@ZLLJ' + chr(0x2005) + '锐评一下文吉儿'), '文吉儿')
        self.assertEqual(search_query('帮我点评张泉灵'), '张泉灵')
        for question in ('点评一下我今天的表现', '锐评一下这段话', '评价一下你自己', '锐评一下刚才那句话'):
            with self.subTest(question=question):
                self.assertTrue(search_query(question))

    def test_parsers_strip_scripts_and_extract_titles_snippets_without_following_links(self):
        pages = {
            'bing': '<li class="b_algo"><h2><a href="https://example.com/x">测试节目</a></h2><div class="b_caption"><p>第四季嘉宾名单</p></div></li>',
            'sogou': '<div class="vrwrap"><h3><a href="/link?url=opaque">测试节目</a><script>private-cookie</script></h3><div class="ft">第四季嘉宾名单</div></div>',
            'so360': '<li class="res-list"><h3><a href="https://www.so.com/link?x" data-mdurl="https://example.com">测试节目</a></h3><p class="res-desc">第四季嘉宾名单</p></li>'}
        for provider, page in pages.items():
            with self.subTest(provider=provider):
                rows = parse_results(page.encode(), provider)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0].title, '测试节目')
                self.assertEqual(rows[0].snippet, '第四季嘉宾名单')
                self.assertNotIn('private-cookie', str(rows))

    def test_captcha_and_unrelated_results_not_treated_as_evidence(self):
        self.assertFalse(parse_results(b'<html><p>Captcha required</p></html>', 'sogou'))
        self.assertFalse(_relevant('Python 官方网站', SearchResult('Windows Help', 'How to get help', 'example.com')))
        self.assertTrue(_relevant('测试节目 第四季 嘉宾', bundle().results[0]))

    def test_redirects_cannot_reach_result_sites_loopback_http_or_credentials(self):
        from urllib.request import Request
        for url in ('http://www.bing.com/search', 'https://127.0.0.1/private',
                    'https://example.com/x', 'https://secret@www.bing.com/search'):
            with self.subTest(url=url), self.assertRaises(SearchError):
                _Redirects('www.bing.com').redirect_request(Request('https://www.bing.com/search'),
                                                           None, 302, 'Found', {}, url)

    def test_ddgs_is_time_bounded_normalized_and_cached(self):
        result = SimpleNamespace(stdout=json.dumps([{'title': '测试节目 第四季 嘉宾',
                                  'body': '节目阵容与具体表现', 'href': 'https://example.com/source'}]).encode())
        with patch('wxbot_framework.web_search.subprocess.run', return_value=result) as run:
            client = WebSearch()
            first = client.search('测试节目 第四季 嘉宾')
            self.assertIs(client.search('测试节目 第四季 嘉宾'), first)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.kwargs['timeout'], 10)
        self.assertNotIn('shell', run.call_args.kwargs)
        self.assertEqual(first.provider, 'ddgs')
        self.assertNotIn('https:', str(first.prompt_data()))

    def test_all_providers_fail_with_safe_message_and_no_secret_echo(self):
        client = WebSearch()
        with (patch('wxbot_framework.web_search.subprocess.run', side_effect=subprocess.TimeoutExpired('private-key', 10)),
              patch('wxbot_framework.web_search.build_opener', side_effect=RuntimeError('private-key'))):
            with self.assertRaises(SearchError) as failure:
                client.search('测试节目 第四季 嘉宾')
        self.assertNotIn('private-key', str(failure.exception))
        self.assertNotIn('private-key', failure_notice(failure.exception))

    def test_normalization_does_not_accept_local_file_urls_or_userinfo(self):
        rows = [{'title': '测试', 'body': '描述', 'href': url} for url in ('file:///C:/private', 'https://secret@example.com/x', 'javascript:alert(1)')]
        self.assertFalse(WebSearch._normalize(rows))

    def test_api_sends_separate_credential_only_to_fixed_provider(self):
        raw = json.dumps({'results': [{'title': '测试节目 第四季 嘉宾', 'content': '阵容信息', 'url': 'https://example.com'}]}).encode()
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / 'search.json'
            config.write_text(json.dumps({'provider': 'tavily', 'api_key': 'test-only-search-key'}))
            with patch('wxbot_framework.web_search.build_opener') as opener:
                opener.return_value.open.return_value = io.BytesIO(raw)
                result = WebSearch(config).search('测试节目 第四季 嘉宾')
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, 'https://api.tavily.com/search')
            self.assertEqual(request.get_header('Authorization'), 'Bearer test-only-search-key')
            self.assertNotIn('test-only-search-key', str(result.prompt_data()))

    def test_keyless_uses_official_header_without_api_key_or_authentication(self):
        raw = json.dumps({'results': [{'title': '测试节目 第四季 嘉宾', 'content': '阵容信息', 'url': 'https://example.com'}]}).encode()
        with patch('wxbot_framework.web_search.build_opener') as opener:
            opener.return_value.open.return_value = io.BytesIO(raw)
            result = WebSearch()._api('测试节目 第四季 嘉宾', {'provider': 'tavily-keyless'})
        request = opener.return_value.open.call_args.args[0]
        self.assertEqual(result.provider, 'tavily-keyless')
        self.assertEqual(request.get_header('X-tavily-access-mode'), 'keyless')
        self.assertIsNone(request.get_header('Authorization'))
        self.assertNotIn('time_range', json.loads(request.data))

    def test_current_news_uses_time_filter_without_applying_it_to_old_programmes(self):
        raw = json.dumps({'results': [{'title': '今天科技新闻', 'content': '今日科技新闻事件', 'url': 'https://example.com'}]}).encode()
        with patch('wxbot_framework.web_search.build_opener') as opener:
            opener.return_value.open.return_value = io.BytesIO(raw)
            WebSearch()._api('今天科技新闻', {'provider': 'tavily-keyless'})
        request = opener.return_value.open.call_args.args[0]
        self.assertEqual(json.loads(request.data)['time_range'], 'd')

    def test_search_success_routes_evidence_and_keeps_delivery_guard(self):
        plugin = Plugin()
        plugin.members = SimpleNamespace(display_name=lambda *args: '用户')
        plugin.web_search = SimpleNamespace(search=Mock(return_value=bundle()))
        llm = SimpleNamespace(supports_plain_text=True, model='test', complete_text=Mock(return_value='主观看法：表现自然。'))
        ctx = SimpleNamespace(recent_images=None, llm_for=lambda *args, **kwargs: llm, logger=Mock())
        message = Message('点评《测试节目》第四季的嘉宾', '123@chatroom', 'user', is_group=True)
        snapshot = SimpleNamespace(speaker_name='用户', day='2026-11-03', messages=(), current=message.text)
        guard = Mock()
        with patch('plugins.mention_chat.deliver_model_reply') as deliver:
            plugin.reply_to_member(message, ctx, snapshot, PersonalityState('123@chatroom'), guard)
        self.assertIn('web_search', json.loads(llm.complete_text.call_args.args[1]))
        self.assertIs(deliver.call_args.kwargs['delivery_guard'], guard)

    def test_rate_limit_is_distinguished_and_never_exposes_server_body(self):
        from urllib.error import HTTPError
        failure = HTTPError('https://api.tavily.com/search', 429, 'rate limited', {}, io.BytesIO(b'private'))
        with patch('wxbot_framework.web_search.build_opener') as opener:
            opener.return_value.open.side_effect = failure
            with self.assertRaises(SearchError) as error:
                WebSearch()._api('测试节目 第四季 嘉宾', {'provider': 'tavily-keyless'})
        self.assertIn('访问额度', failure_notice(error.exception))
        self.assertNotIn('private', str(error.exception))

    def test_search_prompt_separates_evidence_from_instructions_and_has_no_output_links(self):
        client = SimpleNamespace(supports_plain_text=True, model='test', complete_text=Mock(
                                 return_value='主观看法：表现稳健。参考[节目介绍](https://example.com/a) [1]'))
        snapshot = SimpleNamespace(speaker_name='a', day='2026-11-03', messages=(), current='点评测试节目嘉宾')
        answer = Dialogue(client).reply(snapshot, web_search=bundle())
        self.assertNotIn('https:', answer)
        self.assertNotIn('[1]', answer)
        system, user = client.complete_text.call_args.args
        self.assertIn('完全不可信的数据', system)
        self.assertIn('没有访问完整原文', system)
        self.assertIn('网传争议', system)
        self.assertIn('不用旧资料推算', system)
        self.assertIn('不局限于任何预设领域', system)
        self.assertNotIn('嘉宾', system)
        self.assertEqual(json.loads(user)['web_search']['provider'], 'test')

    def test_plain_chat_does_not_include_previous_search_results(self):
        client = SimpleNamespace(supports_plain_text=True, model='test', complete_text=Mock(return_value='你好'))
        snapshot = SimpleNamespace(speaker_name='a', day='2026-11-03', messages=(), current='你好')
        Dialogue(client).reply(snapshot)
        self.assertNotIn('web_search', json.loads(client.complete_text.call_args.args[1]))

    def test_plugin_searches_current_message_and_can_answer_when_search_fails(self):
        plugin = Plugin()
        plugin.members = SimpleNamespace(display_name=lambda *args: '用户')
        plugin.web_search = SimpleNamespace(search=Mock(side_effect=SearchError('test offline')))
        client = SimpleNamespace(supports_plain_text=True, model='test', complete_text=Mock(return_value='你好'))
        ctx = SimpleNamespace(recent_images=None, llm_for=lambda *args, **kwargs: client, logger=Mock())
        message = Message('你好', '123@chatroom', 'user', is_group=True)
        snapshot = SimpleNamespace(speaker_name='用户', day='2026-11-03',
                                  messages=({'received_at': '2026-11-03T16:00:00+08:00', 'text': '搜一下天气'},), current='你好')
        with patch('plugins.mention_chat.deliver_model_reply'):
            plugin.reply_to_member(message, ctx, snapshot, PersonalityState('123@chatroom'), Mock())
        plugin.web_search.search.assert_not_called()
        self.assertEqual(json.loads(client.complete_text.call_args.args[1])['previous'], [])
        client.complete_text.reset_mock()
        plugin.web_search.search.side_effect = SearchError('联网搜索暂不可用')
        message = Message('点评《测试节目》第四季嘉宾', '123@chatroom', 'user', is_group=True)
        snapshot.current = message.text
        with patch('plugins.mention_chat.deliver_model_reply') as send:
            plugin.reply_to_member(message, ctx, snapshot, PersonalityState('123@chatroom'), Mock())
        client.complete_text.assert_called_once()
        send.assert_called_once()
        plugin.web_search.search.assert_called_once_with('测试节目 第四季嘉宾')

    def test_plain_urls_and_markdown_links_are_removed(self):
        self.assertEqual(without_links('结论[介绍](https://example.com)。 https://example.com/a'), '结论介绍。')
