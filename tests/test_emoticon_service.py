"""缓存密钥快照、账号边界及表情服务接线；不连接微信。"""
from __future__ import annotations

import ctypes
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from wxbot_framework.account import (APP_GLOBAL_RVA, APP_VTABLE_RVA, APP_CONTEXT_GETTER_RVA,
                                     CONTEXT_VTABLE_RVA, IDENTITY_GETTER_RVA)
from wxbot_framework.emoticon_account import read_emoticon_cache_key

ROOT = Path(__file__).resolve().parents[1]
BASE, APP, CONTEXT, HEAP = 0x100000000, 0x300000000, 0x400000000, 0x500000000
SELF = "wxid_sticker_robot"
MASK = bytes.fromhex("c9629058e9481a7d100824e418f6f17b2ceb0ce2d7e96e9dc00d3e8249539b96")


class CacheMemory:
    def __init__(self, raw):
        self.memory = {}
        self.write(BASE + APP_GLOBAL_RVA, struct.pack("<Q", APP))
        self.write(APP, struct.pack("<Q", BASE + APP_VTABLE_RVA))
        self.write(APP + 0x68, struct.pack("<Q", CONTEXT))
        self.write(BASE + APP_VTABLE_RVA + 0x60, struct.pack("<Q", BASE + APP_CONTEXT_GETTER_RVA))
        self.write(CONTEXT, struct.pack("<Q", BASE + CONTEXT_VTABLE_RVA))
        self.write(BASE + CONTEXT_VTABLE_RVA + 0x18, struct.pack("<Q", BASE + IDENTITY_GETTER_RVA))
        self.write(BASE + IDENTITY_GETTER_RVA, b"\x48\x8d\x41\x48\xc3")
        self.set_string(CONTEXT + 0x48, SELF.encode(), HEAP)
        self.set_string(CONTEXT + 0x2D8,
                        bytes(value ^ MASK[index & 31] for index, value in enumerate(raw)), HEAP + 0x100)

    def write(self, address, value):
        self.memory.update({address + index: byte for index, byte in enumerate(value)})

    def read(self, address, length):
        try:
            return bytes(self.memory[address + index] for index in range(length))
        except KeyError:
            raise OSError("合成对象不可读") from None

    def set_string(self, address, value, heap):
        if len(value) <= 15:
            self.write(address, value.ljust(16, b"\0") + struct.pack("<QQ", len(value), 15))
        else:
            self.write(heap, value)
            self.write(address, struct.pack("<QQQQ", heap, 0, len(value), 127))


class CacheKeyTests(unittest.TestCase):
    def test_inline_heap_and_ascii_key_use_raw_first_sixteen_bytes(self):
        for raw in (b"short", bytes(range(32)), b"0123456789abcdef0123456789abcdef"):
            with self.subTest(length=len(raw)):
                ram = CacheMemory(raw)
                self.assertEqual(read_emoticon_cache_key(ram.read, BASE, expected_account=SELF),
                                 raw[:16].ljust(16, b"\0"))

    def test_wrong_account_is_rejected_before_reading_key(self):
        ram = CacheMemory(b"synthetic_key_01")
        read = Mock(side_effect=ram.read)
        with self.assertRaisesRegex(RuntimeError, "暂不可用或登录会话已变化"):
            read_emoticon_cache_key(read, BASE, expected_account="wxid_other_robot")
        self.assertFalse(any(call.args[0] == CONTEXT + 0x2D8 for call in read.call_args_list))

    def test_changed_key_or_login_invalidates_snapshot(self):
        for change_account in (False, True):
            ram = CacheMemory(b"synthetic_key_012345")
            changed = False

            def changing(address, size):
                nonlocal changed
                value = ram.read(address, size)
                if address == HEAP + 0x100 and not changed:
                    changed = True
                    if change_account:
                        ram.set_string(CONTEXT + 0x48, b"wxid_switched_robot", HEAP)
                    else:
                        ram.write(address, b"\0" * size)
                return value

            with self.subTest(account_changed=change_account), self.assertRaises(RuntimeError):
                read_emoticon_cache_key(changing, BASE, expected_account=SELF)

    def test_empty_invalid_or_short_memory_is_rejected_without_leaking_details(self):
        for header in (b"\0" * 32, b"\0" * 16 + struct.pack("<QQ", 4097, 8192),
                       b"\0" * 16 + struct.pack("<QQ", 16, 15)):
            ram = CacheMemory(b"synthetic_only")
            ram.write(CONTEXT + 0x2D8, header)
            with self.assertRaises(RuntimeError) as caught:
                read_emoticon_cache_key(ram.read, BASE, expected_account=SELF)
            self.assertNotIn("synthetic_only", str(caught.exception))
        with self.assertRaises(RuntimeError):
            read_emoticon_cache_key(lambda _address, _size: b"", BASE, expected_account=SELF)


@unittest.skipUnless(sys.platform == "win32" and ctypes.sizeof(ctypes.c_void_p) == 8, "Windows 服务")
class EmoticonServiceTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(ROOT / "native"))
        from text_service import NativeTextService
        self.service = NativeTextService(pid=123, account=SELF, preserve_reports=False)
        self.service.router.set_account(SELF)
        self.service._owns_bridge = True
        self.service._read_account = Mock(return_value=SELF)
        self.service._run = Mock(return_value={"success": True})

    def gif(self, directory):
        from PIL import Image
        path = Path(directory) / "synthetic.gif"
        Image.new("RGB", (8, 8), "blue").save(path)
        return path

    def test_send_uses_bound_account_and_rechecks_after_native_call(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.gif(directory)
            self.assertTrue(self.service.send_emoticon("filehelper", path))
            options = self.service._run.call_args.kwargs
            self.assertEqual(options["emoticon_path"], path.resolve())
            self.assertEqual((options["account"], options["target"]), (SELF, "filehelper"))
            self.assertTrue(options["keep_loaded"])
            self.assertFalse(options["preserve_report"])
            self.assertEqual(self.service._read_account.call_count, 2)

    def test_send_cannot_use_another_login(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.gif(directory)
            self.service._read_account.return_value = "wxid_other_robot"
            with self.assertRaisesRegex(RuntimeError, "当前账号发生变化"):
                self.service.send_emoticon("filehelper", path)
        self.service._run.assert_not_called()

    def test_export_requires_explicit_current_account_directory(self):
        with self.assertRaisesRegex(ValueError, "wechat_data_directory"):
            self.service.export_emoticons()
        self.service._run.assert_not_called()

    def test_export_receives_ephemeral_key_and_live_account_guard(self):
        self.service.wechat_data_directory = Path("wxid_sticker_robot_abcd")
        self.service._channel = Mock(core_base=BASE)

        def exporting(account_directory, output_directory, key, *, expected_account, convert, is_current):
            self.assertEqual(account_directory, self.service.wechat_data_directory)
            self.assertEqual(key, b"example_key_1234")
            self.assertEqual(expected_account, SELF)
            self.assertFalse(convert)
            self.assertTrue(is_current())
            self.service._read_account.return_value = "wxid_other_robot"
            self.assertFalse(is_current())
            self.service._read_account.return_value = SELF
            return ("synthetic_result",)

        with patch("wxbot_framework.emoticon_account.read_emoticon_cache_key", return_value=b"example_key_1234") as reader, \
                patch("wxbot_framework.emoticons.export_local_emoticons", side_effect=exporting):
            self.assertEqual(self.service.export_emoticons(convert=False), ("synthetic_result",))
        reader.assert_called_once_with(self.service._channel.read_memory, BASE, expected_account=SELF)
        self.assertFalse(hasattr(self.service, "cache_key"))


if __name__ == "__main__":
    unittest.main()
