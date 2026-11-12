"""首次绑定只使用模拟账号，验证等待、多开选择和本地持久化。"""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import main as entry
from wxbot_framework.config import ConfigStore, initialize_configs
from wxbot_framework.startup import ensure_account_binding, scan_logged_accounts

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = ROOT / ".runtime/tests"
FIRST = {"pid": 123, "wxid": "wxid_example_first", "name": "测试账号甲"}
SECOND = {"pid": 456, "wxid": "wxid_example_second", "name": "测试账号乙"}


class Timer:
    def __init__(self):
        self.now = 0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class StartupTests(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.root = Path(self.temporary.name).resolve()
        self.assertTrue(self.root.is_relative_to(SCRATCH.resolve()))
        for stem in ("bot_config", "members_config", "llm_config"):
            shutil.copyfile(ROOT / (stem + ".example.json"), self.root / (stem + ".example.json"))
        initialize_configs(self.root)
        self.store = ConfigStore(self.root / "bot_config.json")
        self.output = Mock()

    def tearDown(self):
        self.assertTrue(self.root.is_relative_to(SCRATCH.resolve()))
        self.temporary.cleanup()

    def update(self, **options):
        config = self.store.load()
        config.update(options)
        self.store.path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    def test_single_account_is_saved_before_member_store_initialization(self):
        scan = Mock(return_value=[FIRST])
        result = ensure_account_binding(self.root, self.store, scan=scan, output=self.output)
        self.assertEqual(result["wechat_account"], FIRST["wxid"])
        self.assertEqual(self.store.load()["wechat_account"], FIRST["wxid"])
        self.assertFalse((self.root / ".runtime").exists())
        scan.assert_called_with()
        self.assertIn(FIRST["wxid"], "\n".join(call.args[0] for call in self.output.call_args_list))

    def test_old_placeholder_and_empty_account_use_first_start_flow(self):
        for value in ("wxid_example_robot", "", None):
            with self.subTest(account=value):
                self.update(wechat_account=value)
                result = ensure_account_binding(self.root, self.store, scan=lambda: [FIRST], output=self.output)
                self.assertEqual(result["wechat_account"], FIRST["wxid"])

    def test_waits_for_login_without_requiring_filehelper_message(self):
        scan = Mock(side_effect=[[], [], [FIRST], [FIRST]])
        timer = Timer()
        result = ensure_account_binding(self.root, self.store, scan=scan, output=self.output,
                                        sleep=timer.sleep, clock=timer.clock)
        self.assertEqual(result["wechat_account"], FIRST["wxid"])
        self.assertEqual(timer.now, 4)
        self.assertEqual(scan.call_count, 4)

    def test_wait_timeout_keeps_config_unbound(self):
        timer = Timer()
        with self.assertRaises(TimeoutError):
            ensure_account_binding(self.root, self.store, scan=lambda: [], output=self.output,
                                   sleep=timer.sleep, clock=timer.clock, timeout=1)
        self.assertIsNone(self.store.load()["wechat_account"])

    def test_multiple_accounts_require_number_and_save_selected_identity(self):
        choice = Mock(return_value="2")
        result = ensure_account_binding(self.root, self.store, scan=lambda: [FIRST, SECOND],
                                        input_fn=choice, output=self.output)
        self.assertEqual(result["wechat_account"], SECOND["wxid"])
        self.assertIsNone(result["wechat_pid"])
        rendered = "\n".join(call.args[0] for call in self.output.call_args_list)
        self.assertIn(FIRST["name"], rendered)
        self.assertIn(SECOND["name"], rendered)
        choice.assert_called_once()

    def test_refresh_and_invalid_input_do_not_accidentally_select_first_account(self):
        choice = Mock(side_effect=["abc", "0", "99", "r", "2"])
        result = ensure_account_binding(self.root, self.store, scan=lambda: [FIRST, SECOND],
                                        input_fn=choice, output=self.output)
        self.assertEqual(result["wechat_account"], SECOND["wxid"])
        self.assertEqual(choice.call_count, 5)

    def test_noninteractive_multiple_accounts_are_not_silently_bound(self):
        self.update(console=False)
        with self.assertRaisesRegex(RuntimeError, "交互终端"):
            ensure_account_binding(self.root, self.store, scan=lambda: [FIRST, SECOND], output=self.output)
        self.assertIsNone(self.store.load()["wechat_account"])

    def test_quit_keeps_config_unbound(self):
        with self.assertRaises(KeyboardInterrupt):
            ensure_account_binding(self.root, self.store, scan=lambda: [FIRST, SECOND],
                                   input_fn=lambda _: "q", output=self.output)
        self.assertIsNone(self.store.load()["wechat_account"])

    def test_logged_out_selection_is_not_replaced_by_remaining_account(self):
        scan = Mock(side_effect=[[FIRST, SECOND], [SECOND], [SECOND]])
        choice = Mock(side_effect=["1", "q"])
        with self.assertRaises(KeyboardInterrupt):
            ensure_account_binding(self.root, self.store, scan=scan, input_fn=choice, output=self.output)
        self.assertIsNone(self.store.load()["wechat_account"])
        self.assertEqual(choice.call_count, 2)

    def test_duplicate_processes_for_same_account_do_not_persist_ambiguous_binding(self):
        duplicate = dict(FIRST, pid=789)
        timer = Timer()
        with self.assertRaises(TimeoutError):
            ensure_account_binding(self.root, self.store, scan=lambda: [FIRST, duplicate],
                                   input_fn=lambda _: "1", output=self.output,
                                   sleep=timer.sleep, clock=timer.clock, timeout=1)
        self.assertIsNone(self.store.load()["wechat_account"])

    def test_existing_binding_does_not_probe_or_switch_to_another_account(self):
        self.update(wechat_account=FIRST["wxid"])
        scan = Mock(return_value=[SECOND])
        choice = Mock()
        result = ensure_account_binding(self.root, self.store, scan=scan, input_fn=choice, output=self.output)
        self.assertEqual(result["wechat_account"], FIRST["wxid"])
        scan.assert_not_called()
        choice.assert_not_called()

    def test_explicit_pid_filters_candidates_before_first_binding(self):
        self.update(wechat_pid=SECOND["pid"])
        choice = Mock()
        result = ensure_account_binding(self.root, self.store, scan=lambda: [FIRST, SECOND],
                                        input_fn=choice, output=self.output)
        self.assertEqual(result["wechat_account"], SECOND["wxid"])
        choice.assert_not_called()

    def test_binding_preserves_other_settings_and_refuses_overwriting_another_account(self):
        self.update(chat_allowlist=["87654321@chatroom"], custom_option={"keep": True})
        self.store.bind_account(FIRST["wxid"])
        result = self.store.load()
        self.assertEqual(result["chat_allowlist"], ["87654321@chatroom"])
        self.assertEqual(result["custom_option"], {"keep": True})
        self.assertFalse(result["plugins"]["mention_chat"]["enabled"])
        with self.assertRaisesRegex(RuntimeError, "已被修改"):
            self.store.bind_account(SECOND["wxid"])
        with self.assertRaises(ValueError):
            self.store.bind_account("wxid_example_robot")
        self.assertEqual(self.store.load()["wechat_account"], FIRST["wxid"])

    def test_config_initializer_does_not_overwrite_local_account_or_key(self):
        self.update(wechat_account=FIRST["wxid"])
        key_path = self.root / "llm_config.json"
        key_path.write_text('{"api_key":"local-test-value"}', encoding="utf-8")
        self.assertEqual(initialize_configs(self.root), [])
        self.assertEqual(self.store.load()["wechat_account"], FIRST["wxid"])
        self.assertEqual(json.loads(key_path.read_text(encoding="utf-8"))["api_key"], "local-test-value")

    def test_main_binds_before_constructing_application(self):
        fresh = self.root / "first-run"
        fresh.mkdir()
        for stem in ("bot_config", "members_config", "llm_config"):
            shutil.copyfile(self.root / (stem + ".example.json"), fresh / (stem + ".example.json"))

        def construct(root, store):
            self.assertEqual(store.load()["wechat_account"], FIRST["wxid"])
            return Mock()

        with patch.object(entry, "ROOT", fresh), patch.object(entry, "configure_logging"), \
                patch.object(entry, "InstanceLock"), patch.object(entry, "BotApplication", side_effect=construct) as app, \
                patch("wxbot_framework.startup.scan_logged_accounts", return_value=[FIRST]), \
                patch.object(sys, "argv", ["main.py", "--run-seconds", "0.1"]), redirect_stdout(io.StringIO()):
            self.assertEqual(entry.main(), 0)
        app.assert_called_once()
        self.assertEqual(ConfigStore(fresh / "bot_config.json").load()["wechat_account"], FIRST["wxid"])
        self.assertTrue((fresh / "members_config.json").is_file())
        self.assertTrue((fresh / "llm_config.json").is_file())

    def test_check_and_plugin_listing_work_without_login_or_saved_identity(self):
        shutil.copytree(ROOT / "plugins", self.root / "plugins")
        for option in ("--check", "--list-plugins"):
            with self.subTest(option=option), patch.object(entry, "ROOT", self.root), \
                    patch.object(entry, "ensure_account_binding") as bind, \
                    patch.object(entry, "BotApplication") as app, \
                    patch.object(sys, "argv", ["main.py", option]), redirect_stdout(io.StringIO()):
                self.assertEqual(entry.main(), 0)
                bind.assert_not_called()
                app.assert_not_called()
            self.assertIsNone(self.store.load()["wechat_account"])
            self.assertFalse((self.root / "logs").exists())
            self.assertFalse((self.root / ".runtime").exists())

    @unittest.skipUnless(sys.platform == "win32", "本机只读枚举模块检查")
    def test_account_scan_ignores_exited_process_and_keeps_names(self):
        native = str(ROOT / "native")
        if native not in sys.path:
            sys.path.insert(0, native)
        with patch("text_service.wechat_candidates", return_value=[SECOND["pid"], 999, FIRST["pid"]]), \
                patch("text_service.inspect_profile", side_effect=[SECOND, OSError("进程退出"), FIRST]), \
                patch("text_service.run_text") as bridge:
            self.assertEqual(scan_logged_accounts(ROOT), [FIRST, SECOND])
            bridge.assert_not_called()


if __name__ == "__main__":
    unittest.main()
