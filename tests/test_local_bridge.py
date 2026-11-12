"""本机桥接的冷启动、调用隔离与失败处理；全程使用假上游。"""
import io
import json
import subprocess
import unittest
from unittest.mock import patch
from urllib.error import URLError

from wxbot_framework.llm import LLMClient, LLMError


def response(value):
    return io.BytesIO(json.dumps(value).encode())


class LocalBridgeTests(unittest.TestCase):
    def options(self, **changes):
        return {"base_url": "http://127.0.0.1:3427/v1", "api_key": "test-only",
                "model": "test", "local_bridge_task": "CodexMagpieDucxBridge", **changes}

    def test_healthy_bridge_does_not_start_task(self):
        replies = [response({"ready": True, "source": "ducx", "chatCompletions": True}),
                   response({"choices": [{"message": {"content": '{"ok":true}'}}]})]
        with patch("wxbot_framework.llm.urlopen", side_effect=replies) as calls, \
                patch("wxbot_framework.llm.subprocess.run") as start:
            self.assertEqual(LLMClient(self.options()).complete_json("JSON", "hello"), {"ok": True})
        start.assert_not_called()
        self.assertEqual(calls.call_count, 2)

    def test_offline_bridge_starts_task_before_exactly_one_model_request(self):
        replies = [URLError("offline"),
                   response({"ready": True, "source": "ducx", "chatCompletions": True}),
                   response({"choices": [{"message": {"content": '{"ok":true}'}}]})]
        with patch("wxbot_framework.llm.urlopen", side_effect=replies) as calls, \
                patch("wxbot_framework.llm.subprocess.run") as start:
            self.assertEqual(LLMClient(self.options()).complete_json("JSON", "private question"), {"ok": True})
        start.assert_called_once()
        self.assertNotIn("private question", str(start.call_args))
        posts = [call for call in calls.call_args_list if hasattr(call.args[0], "data")]
        self.assertEqual(len(posts), 1)
        self.assertTrue(posts[0].args[0].full_url.endswith("/chat/completions"))

    def test_task_failure_does_not_submit_inference_or_expose_output(self):
        failure = subprocess.CalledProcessError(1, "powershell", stderr=b"private diagnostic")
        with patch("wxbot_framework.llm.urlopen", side_effect=URLError("offline")) as calls, \
                patch("wxbot_framework.llm.subprocess.run", side_effect=failure):
            with self.assertRaises(LLMError) as raised:
                LLMClient(self.options()).complete_json("JSON", "hello")
        self.assertNotIn("private diagnostic", str(raised.exception))
        self.assertEqual(calls.call_count, 1)

    def test_task_option_cannot_be_used_for_other_addresses_or_commands(self):
        for changes in ({"base_url": "https://remote.example/v1"}, {"base_url": "http://127.0.0.1:9000/v1"},
                        {"local_bridge_task": "other-task; unexpected-command"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                LLMClient(self.options(**changes))


if __name__ == "__main__":
    unittest.main()
