"""表情发送与导出的插件接口和连接隔离测试；不连接微信或网络。"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from wxbot_framework.sdk import CapabilityError, LegacyService, Message, PluginContext, ServiceFacade

SCRATCH = Path(__file__).resolve().parent.parent / ".runtime/tests"
FRIEND = "wxid_emoticon_friend"
ROOM = "1234567890@chatroom"
GIF_BYTES = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")


class MemoryBackend:
    def __init__(self, session="original", account="wxid_first_robot"):
        self.session_id = session
        self.account = account
        self.sent = []
        self.exports = []
        self.export_result = {"account": account, "files": []}
        self.send_result = True
        self.stopped = False

    def send_emoticon(self, chat_id, path):
        self.sent.append((chat_id, path))
        return self.send_result

    def export_emoticons(self, *, convert=True):
        self.exports.append(convert)
        return self.export_result

    def stop(self):
        self.stopped = True


class WaitingSendLock:
    def __init__(self):
        self.lock = threading.Lock()
        self.waiting = threading.Event()

    def __enter__(self):
        self.waiting.set()
        self.lock.acquire()
        return self

    def __exit__(self, *args):
        self.lock.release()


class EmoticonSDKTests(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="emoticon-sdk-", dir=SCRATCH)
        self.root = Path(self.temporary.name).resolve()
        self.addCleanup(self._cleanup_files)
        self.emoticon = self.root / "完整表情.gif"
        self.emoticon.write_bytes(GIF_BYTES)
        self.backend = MemoryBackend()
        self.service = ServiceFacade(text_delay={"enabled": False})
        self.service.bind(self.backend)
        self.addCleanup(self.service.close)
        self.ctx = PluginContext("emoticon-test", {}, self.service, {"emoticon.send", "emoticon.export"})

    def _cleanup_files(self):
        self.assertTrue(self.root.is_relative_to(SCRATCH.resolve()))
        self.temporary.cleanup()

    def test_plugin_reply_and_legacy_apis_keep_target_and_absolute_path_without_text_delay(self):
        self.service.text_delay = Mock()
        message = Message("", ROOM, session_id="original")
        legacy = LegacyService(self.service, "original")
        self.assertTrue(self.ctx.send_emoticon(FRIEND, self.emoticon))
        self.assertTrue(self.ctx.reply_emoticon(message, str(self.emoticon)))
        self.assertTrue(legacy.send_emoticon("filehelper", self.emoticon))
        self.assertEqual(self.backend.sent, [(FRIEND, str(self.emoticon)),
                                            (ROOM, str(self.emoticon)),
                                            ("filehelper", str(self.emoticon))])
        self.service.text_delay.seconds_for.assert_not_called()

    def test_false_backend_send_result_is_not_reported_as_success(self):
        self.backend.send_result = False
        self.assertFalse(self.ctx.send_emoticon(FRIEND, self.emoticon))

    def test_stale_reply_and_legacy_services_do_not_send_or_export_from_another_account(self):
        message = Message("", FRIEND, session_id="original")
        legacy = LegacyService(self.service, "original")
        self.service.close()
        replacement = MemoryBackend("replacement", "wxid_second_robot")
        self.service.bind(replacement)
        for operation in (lambda: self.ctx.reply_emoticon(message, self.emoticon),
                          lambda: legacy.send_emoticon(FRIEND, self.emoticon),
                          lambda: legacy.export_emoticons()):
            with self.subTest(operation=operation), self.assertRaises(RuntimeError):
                operation()
        self.assertEqual(self.backend.sent, [])
        self.assertEqual(replacement.sent, [])
        self.assertEqual(replacement.exports, [])

    def test_explicit_empty_session_cannot_bypass_reply_or_legacy_isolation(self):
        for operation in (lambda: self.ctx.reply_emoticon(Message("", FRIEND), self.emoticon),
                          lambda: LegacyService(self.service, "").send_emoticon(FRIEND, self.emoticon),
                          lambda: LegacyService(self.service, "").export_emoticons()):
            with self.subTest(operation=operation), self.assertRaises(RuntimeError):
                operation()
        self.assertEqual(self.backend.sent, [])
        self.assertEqual(self.backend.exports, [])

    def test_unsupported_backend_reports_distinct_capability_errors(self):
        self.backend.send_emoticon = None
        self.backend.export_emoticons = None
        for operation in (lambda: self.ctx.send_emoticon(FRIEND, self.emoticon),
                          lambda: self.ctx.export_emoticons()):
            with self.subTest(operation=operation), self.assertRaises(CapabilityError):
                operation()
        self.assertEqual(self.backend.sent, [])
        self.assertEqual(self.backend.exports, [])

    def test_bad_target_missing_file_or_non_gif_never_reaches_backend(self):
        disguised = self.root / "假的表情.gif"
        disguised.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 32)
        for chat, path in (("非法微信ID", self.emoticon), (FRIEND, self.root),
                           (FRIEND, self.root / "missing.gif"), (FRIEND, disguised)):
            with self.subTest(chat=chat, path=str(path)), self.assertRaises(ValueError):
                self.service.send_emoticon(chat, path)
        self.assertEqual(self.backend.sent, [])

    def test_cancelled_and_closed_services_reject_send_and_export(self):
        for shutdown in (self.service.cancel_pending, self.service.close):
            shutdown()
            for operation in (lambda: self.ctx.send_emoticon(FRIEND, self.emoticon),
                              lambda: self.ctx.export_emoticons()):
                with self.subTest(operation=operation), self.assertRaises(RuntimeError):
                    operation()
        self.assertEqual(self.backend.sent, [])
        self.assertEqual(self.backend.exports, [])

    def test_reconnect_during_initial_file_validation_cannot_select_new_account(self):
        self._reconnect_during_validation(change_on=1)

    def test_reconnect_during_final_file_validation_cannot_send_from_either_connection(self):
        self._reconnect_during_validation(change_on=2)

    def _reconnect_during_validation(self, *, change_on):
        from wxbot_framework.emoticons import validate_emoticon_path
        replacement = MemoryBackend("replacement", "wxid_second_robot")
        calls = []

        def validate(path):
            validated = validate_emoticon_path(path)
            calls.append(path)
            if len(calls) == change_on:
                self.service.close()
                self.service.bind(replacement)
            return validated

        with patch("wxbot_framework.emoticons.validate_emoticon_path", side_effect=validate), self.assertRaises(RuntimeError):
            self.service.send_emoticon(FRIEND, self.emoticon)
        self.assertEqual(self.backend.sent, [])
        self.assertEqual(replacement.sent, [])

    def test_delivery_guard_failure_prevents_send(self):
        entered = []

        @contextmanager
        def guard():
            entered.append(True)
            raise RuntimeError("群记录已清空，表情回复已过期")
            yield

        with self.assertRaises(RuntimeError):
            self.ctx.reply_emoticon(Message("", FRIEND, session_id="original"), self.emoticon,
                                    delivery_guard=guard)
        self.assertEqual(entered, [True])
        self.assertEqual(self.backend.sent, [])

    def test_reentrant_delivery_guard_cannot_move_reply_to_a_replacement_connection(self):
        replacement = MemoryBackend("replacement", "wxid_second_robot")

        @contextmanager
        def guard():
            self.service.close()
            self.service.bind(replacement)
            yield

        with self.assertRaises(RuntimeError):
            self.ctx.reply_emoticon(Message("", FRIEND, session_id="original"), self.emoticon,
                                    delivery_guard=guard)
        self.assertEqual(self.backend.sent, [])
        self.assertEqual(replacement.sent, [])

    def test_export_apis_forward_conversion_flag_and_return_backend_result(self):
        self.service.text_delay = Mock()
        legacy = LegacyService(self.service, "original")
        self.assertIs(self.service.export_emoticons(), self.backend.export_result)
        self.assertIs(self.ctx.export_emoticons(convert=False), self.backend.export_result)
        self.assertIs(legacy.export_emoticons(convert=True), self.backend.export_result)
        self.assertEqual(self.backend.exports, [True, False, True])
        self.service.text_delay.seconds_for.assert_not_called()
        self.assertEqual(self.backend.sent, [])

    def test_export_conversion_flag_requires_a_boolean(self):
        for convert in (None, 1, 0, "false", [], {}):
            with self.subTest(convert=convert), self.assertRaises(ValueError):
                self.service.export_emoticons(convert=convert)
        self.assertEqual(self.backend.exports, [])

    def test_queued_send_is_cancelled_after_close(self):
        self._queued_operation(lambda: self.ctx.send_emoticon(FRIEND, self.emoticon), self.service.close,
                               error_type=RuntimeError)
        self.assertEqual(self.backend.sent, [])

    def test_queued_send_does_not_migrate_to_another_account(self):
        replacement = MemoryBackend("replacement", "wxid_second_robot")

        def replace():
            self.service.close()
            self.service.bind(replacement)

        self._queued_operation(lambda: self.ctx.send_emoticon(FRIEND, self.emoticon), replace,
                               error_type=RuntimeError)
        self.assertEqual(self.backend.sent, [])
        self.assertEqual(replacement.sent, [])
        self.assertTrue(self.ctx.send_emoticon(FRIEND, self.emoticon))
        self.assertEqual(replacement.sent, [(FRIEND, str(self.emoticon))])

    def test_rebinding_same_backend_and_session_still_cancels_old_send_token(self):
        def rebind():
            self.service.close()
            self.service.bind(self.backend)

        self._queued_operation(lambda: self.ctx.send_emoticon(FRIEND, self.emoticon), rebind,
                               error_type=RuntimeError)
        self.assertEqual(self.backend.sent, [])

    def test_queued_send_revalidates_removed_or_modified_gif_before_backend_call(self):
        for mutation in (self.emoticon.unlink, lambda: self.emoticon.write_bytes(b"GIF89a truncated")):
            with self.subTest(mutation=mutation):
                self.emoticon.write_bytes(GIF_BYTES)
                self._queued_operation(lambda: self.ctx.send_emoticon(FRIEND, self.emoticon), mutation,
                                       error_type=ValueError)
        self.assertEqual(self.backend.sent, [])

    def test_queued_export_does_not_scan_a_replacement_account(self):
        replacement = MemoryBackend("replacement", "wxid_second_robot")

        def replace():
            self.service.close()
            self.service.bind(replacement)

        self._queued_operation(lambda: self.ctx.export_emoticons(convert=False), replace,
                               error_type=RuntimeError)
        self.assertEqual(self.backend.exports, [])
        self.assertEqual(replacement.exports, [])
        self.assertIs(self.ctx.export_emoticons(convert=False), replacement.export_result)
        self.assertEqual(replacement.exports, [False])

    def test_queued_export_is_cancelled_before_backend_cleanup(self):
        self._queued_operation(lambda: self.ctx.export_emoticons(), self.service.cancel_pending,
                               error_type=RuntimeError)
        self.assertFalse(self.backend.stopped)
        self.assertEqual(self.backend.exports, [])

    def test_running_export_allows_cancellation_and_discards_its_old_result(self):
        self._running_export_shutdown(reconnect=False)

    def test_running_export_does_not_return_old_account_result_after_reconnect(self):
        self._running_export_shutdown(reconnect=True)

    def _running_export_shutdown(self, *, reconnect):
        entered, release, shutdown_done = threading.Event(), threading.Event(), threading.Event()
        results, errors = [], []
        replacement = MemoryBackend("replacement", "wxid_second_robot")

        def export(*, convert=True):
            self.backend.exports.append(convert)
            entered.set()
            if not release.wait(3):
                raise AssertionError("测试导出未被释放")
            return self.backend.export_result

        self.backend.export_emoticons = export

        def run():
            try:
                results.append(self.ctx.export_emoticons())
            except Exception as error:
                errors.append(error)

        def shutdown():
            if reconnect:
                self.service.close()
                self.service.bind(replacement)
            else:
                self.service.cancel_pending()
            shutdown_done.set()

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        closer = threading.Thread(target=shutdown, daemon=True)
        try:
            self.assertTrue(entered.wait(2))
            closer.start()
            # 长导出不能占用连接锁，取消/重连在导出结束之前就应完成。
            self.assertTrue(shutdown_done.wait(1))
            self.assertEqual(self.service.connected, reconnect)
        finally:
            release.set()
            worker.join(3)
            if closer.ident is not None:
                closer.join(3)
        self.assertFalse(worker.is_alive())
        self.assertFalse(closer.is_alive())
        self.assertEqual(results, [])
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RuntimeError)
        self.assertEqual(self.backend.exports, [True])
        self.assertEqual(replacement.exports, [])

    def _queued_operation(self, operation, during_wait, *, error_type):
        gate = WaitingSendLock()
        self.service._send_lock = gate
        gate.lock.acquire()
        errors = []

        def run():
            try:
                operation()
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            self.assertTrue(gate.waiting.wait(3))
            self.assertTrue(self.service.connected)
            during_wait()
        finally:
            gate.lock.release()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], error_type)


if __name__ == "__main__":
    unittest.main()
