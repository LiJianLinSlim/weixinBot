"""桌面服务菜单：后台启动、正常关闭、重启和查看状态。"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from pathlib import Path
import socket
import subprocess
import sys
import time
import webbrowser

from local_control import BASE, request
from wxbot_framework.locking import InstanceLock

ROOT = Path(__file__).resolve().parent


def panel_listening():
    try:
        with socket.create_connection(('127.0.0.1', 8765), timeout=0.5):
            return True
    except OSError:
        return False


def wait_for(check, seconds, message):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.3)
    raise RuntimeError(message)


def start_service():
    if not panel_listening():
        print('正在后台启动管理服务…', flush=True)
        logs = ROOT / 'logs'
        logs.mkdir(exist_ok=True)
        with (logs / 'desktop-service.log').open('ab') as output:
            subprocess.Popen([sys.executable, '-X', 'utf8', '-u', str(ROOT / 'web.py'), '--no-browser'],
                cwd=ROOT, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)
        wait_for(panel_listening, 15, '管理服务未启动，请查看 helper/logs/desktop-service.log')
    state = request('state')
    if state['state'] == 'stopping':
        wait_for(lambda: request('state')['state'] != 'stopping', 40, '机器人仍在清理，请稍后再启动')
        state = request('state')
    if state['state'] not in ('running', 'starting'):
        print('正在启动机器人…', flush=True)
        request('start', {})

    def connected():
        current = request('state')
        if current.get('error'):
            raise RuntimeError(current['error'])
        return current.get('connected') is True

    wait_for(connected, 40, '管理台已启动，但机器人尚未连接，请确认微信已登录并打开管理台查看状态')
    print('服务已启动，机器人已连接。关闭这个菜单窗口不影响后台运行。')


def wait_for_process_exit(pid):
    """等待管理进程的正常退出；不向任何进程发送终止信号。"""
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x00100000, False, pid)
    if not handle:
        if ctypes.get_last_error() == 87:  # 已退出。
            return
        raise RuntimeError('无法等待管理服务退出，请稍后查看状态')
    try:
        if kernel.WaitForSingleObject(handle, 45000) != 0:
            raise RuntimeError('服务仍在清理，已保留进程，请稍后再查看状态')
    finally:
        kernel.CloseHandle(handle)


def stop_service():
    if not panel_listening():
        print('服务已经关闭。')
        return
    print('正在正常停止机器人并退出管理服务，请稍候…', flush=True)
    result = request('service-stop', {})
    pid = result.get('pid')
    if result.get('ok') is not True or type(pid) is not int or pid <= 0:
        raise RuntimeError('管理服务未确认退出请求')
    wait_for_process_exit(pid)
    wait_for(lambda: not panel_listening(), 5, '端口仍被占用，请检查管理台状态')
    print('机器人及管理服务已关闭，微信客户端保留。')


def show_status():
    if not panel_listening():
        print('当前状态：服务未启动')
        return
    labels = {'running': '运行中', 'starting': '启动中', 'stopping': '停止中', 'stopped': '已停止', 'error': '异常'}
    print('管理服务：运行中')
    for row in request('robots')['robots']:
        print(f"  {row['name']}：{labels.get(row['state'], row['state'])}" + ('，已连接' if row.get('connected') else ''))


def perform(action):
    if action == 'status':
        show_status()
    elif action == 'open':
        if not panel_listening():
            print('服务未启动，请先选择启动服务。')
        else:
            webbrowser.open(BASE)
    else:
        lock = InstanceLock(ROOT / '.runtime/service-control.lock')
        try:
            lock.acquire()
        except RuntimeError:
            raise RuntimeError('另一个服务脚本正在操作，请等待它完成') from None
        try:
            if action in ('stop', 'restart'):
                stop_service()
            if action in ('start', 'restart'):
                start_service()
        finally:
            lock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', default='menu', choices=('menu', 'start', 'stop', 'restart', 'status', 'open'))
    action = parser.parse_args().action
    if action != 'menu':
        try:
            perform(action)
            return 0
        except Exception as error:
            print(f'操作未完成：{error}')
            return 1
    choices = {'1': 'start', '2': 'stop', '3': 'restart', '4': 'open', '5': 'status'}
    print('微信机器人服务管理')
    show_status()
    while True:
        print('\n1 启动服务   2 关闭服务   3 重启服务\n4 打开管理台 5 查看状态   0 退出菜单')
        try:
            choice = input('请选择：').strip()
            if choice == '0':
                return 0
            if choice in choices:
                perform(choices[choice])
            else:
                print('请输入菜单中的数字')
        except (KeyboardInterrupt, EOFError):
            return 0
        except Exception as error:
            print(f'操作未完成：{error}')


if __name__ == '__main__':
    raise SystemExit(main())
