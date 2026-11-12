import unittest
from plugins.mention_chat import failure_notice
from wxbot_framework.llm import LLMError


class FailureNoticeTests(unittest.TestCase):
    def test_format_diagnostic_is_helpful_without_exposing_raw_error(self):
        result = failure_notice(LLMError('模型连续返回无效 JSON；private-secret'))
        self.assertIn('切换模型', result)
        self.assertNotIn('private-secret', result)

    def test_error_details_are_never_echoed(self):
        result = failure_notice(LLMError('arbitrary-sensitive-error'))
        self.assertNotIn('arbitrary-sensitive-error', result)
        self.assertIn('运行日志', result)

    def test_timeout_and_authentication_are_distinguished(self):
        self.assertIn('超时', failure_notice(LLMError('大模型连接失败或超时')))
        self.assertIn('认证失败', failure_notice(LLMError('大模型接口 HTTP 401')))
        self.assertIn('暂时出错', failure_notice(LLMError('模型上游服务暂时不可用（HTTP 502）')))
