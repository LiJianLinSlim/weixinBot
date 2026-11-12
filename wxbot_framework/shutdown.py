"""将控制台退出信号转换为正常停止请求，避免中断正在进行的原生调用。"""
from __future__ import annotations

import ctypes as c
from ctypes import wintypes as w
import signal
import sys
import threading


class ShutdownHandlers:
    def __init__(self, application):
        self.app = application
        self.saved = {}
        self.console_callback = None
        self.k = None

    def _request_stop(self, *_):
        self.app.stop_event.set()

    def __enter__(self):
        if threading.current_thread() is not threading.main_thread():
            return self
        for name in ("SIGINT", "SIGBREAK", "SIGTERM"):
            value = getattr(signal, name, None)
            if value is not None:
                self.saved[value] = signal.getsignal(value)
                signal.signal(value, self._request_stop)
        if sys.platform == "win32":
            self.k = c.WinDLL("kernel32", use_last_error=True)
            callback_type = c.WINFUNCTYPE(w.BOOL, w.DWORD)

            def console_close(event):
                if event not in (2, 5, 6):
                    return False
                self._request_stop()
                # Windows 关闭窗口只给短暂清理时间，超时后由独立守护接手。
                self.app.stopped_event.wait(4)
                return True

            self.console_callback = callback_type(console_close)
            self.k.SetConsoleCtrlHandler.argtypes = [callback_type, w.BOOL]
            self.k.SetConsoleCtrlHandler.restype = w.BOOL
            if not self.k.SetConsoleCtrlHandler(self.console_callback, True):
                self.__exit__(None, None, None)
                raise c.WinError(c.get_last_error(), "注册控制台退出清理失败")
        return self

    def __exit__(self, *_):
        if self.console_callback is not None:
            self.k.SetConsoleCtrlHandler(self.console_callback, False)
            self.console_callback = None
        for value, handler in self.saved.items():
            signal.signal(value, handler)
        self.saved.clear()
