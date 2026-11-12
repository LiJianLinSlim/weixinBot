"""模型目录读取：本地假服务验证请求、错误脱敏和重定向隔离。"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest

from wxbot_framework.llm import LLMClient, LLMError


class ModelCatalogTests(unittest.TestCase):
    def setUp(self):
        owner = self
        self.calls = []
        self.status = 200
        self.payload = {"data": [{"id": "model-a"}, {"id": "model-b"}]}
        self.location = None

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                owner.calls.append((self.path, self.headers.get("Authorization")))
                raw = json.dumps(owner.payload).encode()
                self.send_response(owner.status)
                if owner.location:
                    self.send_header("Location", owner.location)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        self.options = {"base_url": self.base, "api_key": "private-test-key", "model": "saved-model"}

    def test_reads_catalog_with_saved_key_and_returns_only_deduplicated_ids(self):
        self.payload = {"data": [{"id": "model-a", "private": "private-test-key"},
                                 {"id": "model-a"}, {"id": "model-b"}, {"id": ""},
                                 {"id": "bad\nname"}, {"id": 123}]}
        models = LLMClient(self.options).list_models()
        self.assertEqual(models, ["model-a", "model-b"])
        self.assertEqual(self.calls, [("/v1/models", "Bearer private-test-key")])
        self.assertNotIn("private-test-key", json.dumps(models))

    def test_full_chat_endpoint_uses_models_sibling(self):
        client = LLMClient({**self.options, "base_url": self.base + "/chat/completions"})
        self.assertEqual(client.list_models(), ["model-a", "model-b"])
        self.assertEqual(self.calls[0][0], "/v1/models")

    def test_upstream_auth_errors_do_not_expose_response_body(self):
        self.status = 401
        self.payload = {"error": "private-test-key was rejected"}
        with self.assertRaises(LLMError) as raised:
            LLMClient(self.options).list_models()
        self.assertIn("401", str(raised.exception))
        self.assertNotIn("private-test-key", str(raised.exception))

    def test_redirects_do_not_forward_credential(self):
        self.status = 302
        self.location = self.base + "/unexpected-destination"
        with self.assertRaisesRegex(LLMError, "重定向"):
            LLMClient(self.options).list_models()
        self.assertEqual(len(self.calls), 1)

    def test_missing_or_malformed_catalog_gives_actionable_error(self):
        self.status = 404
        with self.assertRaisesRegex(LLMError, "手动填写"):
            LLMClient(self.options).list_models()
        self.status = 200
        self.payload = {"wrong": []}
        with self.assertRaisesRegex(LLMError, "格式"):
            LLMClient(self.options).list_models()


if __name__ == "__main__":
    unittest.main()
