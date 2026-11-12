"""仅在测试进程调用 DLL 参数校验；不注入或连接微信。"""
import ctypes as c
import os
from pathlib import Path
import sys
import unittest


@unittest.skipUnless(sys.platform == 'win32', 'Windows 原生桥接')
class AtAllNativeTests(unittest.TestCase):
    def test_marker_is_accepted_but_cannot_mix_with_individual_members(self):
        root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(root / 'native'))
        from run_selftest import AtTextConfig
        path = Path(os.environ.get('AT_ALL_TEST_BRIDGE', root / 'native/build_text/text_bridge.dll'))
        bridge = c.WinDLL(str(path.resolve()))
        kernel = c.WinDLL('kernel32', use_last_error=True)
        kernel.FreeLibrary.argtypes = [c.c_void_p]
        kernel.FreeLibrary.restype = c.c_int
        try:
            send = bridge.WxSendAtText
            send.argtypes, send.restype = [c.c_void_p], c.c_uint32
            config = AtTextConfig()
            config.size = c.sizeof(config)
            config.pid = os.getpid()
            config.text = b'local validation only'
            config.target = b'123@chatroom'
            for value, expected in ((b'notify@all', 4), (b'wxid_member', 4),
                                    (b'notify@all,wxid_member', 87), (b'other@all', 87)):
                with self.subTest(marker=value):
                    config.members = value
                    # 4表示参数有效但当前Python进程不存在Weixin.dll，87表示参数不合法。
                    self.assertEqual(send(c.byref(config)), expected)
        finally:
            self.assertTrue(kernel.FreeLibrary(bridge._handle))
