"""微信进程级桥接占用锁，以及 Python 异常退出后的卸载守护。"""
from __future__ import annotations

import ctypes as c
from ctypes import wintypes as w
from functools import lru_cache
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

logger = logging.getLogger("BridgeLifecycle")
_owners = set()
_owners_lock = threading.Lock()


@lru_cache(maxsize=1)
def kernel():
    k = c.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    k.OpenProcess.restype = w.HANDLE
    k.CloseHandle.argtypes = [w.HANDLE]
    k.CloseHandle.restype = w.BOOL
    k.CreateMutexW.argtypes = [c.c_void_p, w.BOOL, w.LPCWSTR]
    k.CreateMutexW.restype = w.HANDLE
    k.ReleaseMutex.argtypes = [w.HANDLE]
    k.ReleaseMutex.restype = w.BOOL
    k.CreateEventW.argtypes = [c.c_void_p, w.BOOL, w.BOOL, w.LPCWSTR]
    k.CreateEventW.restype = w.HANDLE
    k.OpenEventW.argtypes = [w.DWORD, w.BOOL, w.LPCWSTR]
    k.OpenEventW.restype = w.HANDLE
    k.SetEvent.argtypes = [w.HANDLE]
    k.SetEvent.restype = w.BOOL
    k.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    k.WaitForSingleObject.restype = w.DWORD
    k.WaitForMultipleObjects.argtypes = [w.DWORD, c.POINTER(w.HANDLE), w.BOOL, w.DWORD]
    k.WaitForMultipleObjects.restype = w.DWORD
    return k


def checked(value, label):
    if not value:
        raise c.WinError(c.get_last_error(), label)
    return value


class BridgeGuard:
    """同一微信 PID 在所有项目副本中只能由一个机器人持有。"""

    def __init__(self, pid):
        self.pid = pid
        self.k = kernel()
        self.name = f"Local\\WxBotBridge.{pid}"
        self.mutex = self.target = self.stop_event = self.ready_event = None
        self.child = None
        self.owned = False
        with _owners_lock:
            if self.name in _owners:
                raise RuntimeError("这个微信进程已有机器人连接，请先正常退出另一实例")
            try:
                self.target = checked(self.k.OpenProcess(0x100000, False, pid), "打开微信存活检查句柄")
                self.mutex = checked(self.k.CreateMutexW(None, False, self.name), "创建桥接占用锁")
                status = self.k.WaitForSingleObject(self.mutex, 0)
                if status not in (0, 128):
                    raise RuntimeError("这个微信进程已有机器人连接，请先正常退出另一实例")
                self.owned = True
                _owners.add(self.name)
            except BaseException:
                if self.mutex:
                    self.k.CloseHandle(self.mutex)
                if self.target:
                    self.k.CloseHandle(self.target)
                raise

    def target_exited(self):
        status = self.k.WaitForSingleObject(self.target, 0)
        if status == 0xFFFFFFFF:
            raise c.WinError(c.get_last_error())
        return status == 0

    def start_watchdog(self, script):
        token = uuid.uuid4().hex
        stop_name = f"Local\\WxBotBridge.Stop.{token}"
        ready_name = f"Local\\WxBotBridge.Ready.{token}"
        self.stop_event = checked(self.k.CreateEventW(None, True, False, stop_name), "创建桥接清理停止事件")
        self.ready_event = checked(self.k.CreateEventW(None, True, False, ready_name), "创建桥接清理就绪事件")
        self.child = subprocess.Popen(
            [sys.executable, "-B", "-X", "utf8", str(Path(script).resolve()),
             "--owner-pid", str(os.getpid()), "--wechat-pid", str(self.pid),
             "--stop-event", stop_name, "--ready-event", ready_name],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)
        handles = (w.HANDLE * 2)(self.ready_event, int(self.child._handle))
        if self.k.WaitForMultipleObjects(2, handles, False, 10000) != 0:
            raise RuntimeError("桥接退出清理守护未能启动，请查看 logs/bridge_watchdog.log")

    def close(self):
        if not self.owned:
            return
        if self.stop_event:
            checked(self.k.SetEvent(self.stop_event), "结束桥接退出清理守护")
        if self.child is not None:
            self.child.wait(timeout=5)
        # DLL 已卸载后才结束守护并交还跨项目锁。
        checked(self.k.ReleaseMutex(self.mutex), "释放桥接占用锁")
        with _owners_lock:
            _owners.discard(self.name)
        for handle in (self.ready_event, self.stop_event, self.mutex, self.target):
            if handle:
                self.k.CloseHandle(handle)
        self.owned = False
        self.ready_event = self.stop_event = self.mutex = self.target = None


def watch_owner(owner_pid, wechat_pid, stop_name, ready_name, cleanup):
    """守护进程不连接消息、不加载 DLL；仅在原进程消失后调用已验证卸载接口。"""
    k = kernel()
    handles = []
    try:
        owner = checked(k.OpenProcess(0x100000, False, owner_pid), "打开机器人存活检查句柄")
        handles.append(owner)
        target = checked(k.OpenProcess(0x100000, False, wechat_pid), "打开微信存活检查句柄")
        handles.append(target)
        stop = checked(k.OpenEventW(0x100000, False, stop_name), "打开桥接停止事件")
        handles.append(stop)
        ready = checked(k.OpenEventW(2, False, ready_name), "打开桥接就绪事件")
        handles.append(ready)
        checked(k.SetEvent(ready), "确认桥接守护就绪")
        # 正常停止或微信退出时无须再操作；只有 owner 退出才进入恢复。
        wait_handles = (w.HANDLE * 3)(stop, target, owner)
        status = k.WaitForMultipleObjects(3, wait_handles, False, 0xFFFFFFFF)
        if status in (0, 1):
            return
        if status != 2:
            raise RuntimeError(f"等待机器人退出失败：{status}")
        # 目标句柄指向原微信实例，PID 即使被系统复用也不会误清理新进程。
        if k.WaitForSingleObject(target, 0) == 0:
            return
        guard = None
        for attempt in range(20):
            try:
                guard = BridgeGuard(wechat_pid)
                break
            except RuntimeError:
                if attempt == 19:
                    logger.info("另一机器人已接管微信 PID=%s，退出旧守护", wechat_pid)
                    return
                time.sleep(0.2)
        if guard.target_exited():
            guard.close()
            return
        try:
            for attempt in range(4):
                try:
                    cleanup(wechat_pid)
                    logger.info("异常退出后的桥接清理完成，微信 PID=%s", wechat_pid)
                    return
                except Exception:
                    if attempt == 3 or guard.target_exited():
                        raise
                    time.sleep(0.5)
        finally:
            guard.close()
    finally:
        for handle in handles:
            k.CloseHandle(handle)
