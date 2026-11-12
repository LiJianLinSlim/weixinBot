"""本机管理台调用工具；从页面取得临时令牌，不输出令牌或模型密钥。"""
from __future__ import annotations

import json
import re
import time
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener


BASE = "http://127.0.0.1:8765"


def request(route: str, payload=None):
    opener = build_opener(ProxyHandler({}))
    with opener.open(BASE + "/", timeout=10) as response:
        page = response.read().decode("utf-8")
    match = re.search(r'<meta name="helper-token" content="([^"]+)"', page)
    if not match:
        raise RuntimeError("管理台未返回页面令牌")
    call = Request(
        BASE + "/api/" + route,
        data=None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"X-Helper-Token": match.group(1), "Content-Type": "application/json"},
    )
    try:
        with opener.open(call, timeout=30) as response:
            return json.load(response)
    except HTTPError as error:
        try:
            message = json.load(error).get("error", "管理请求失败")
        except (ValueError, AttributeError):
            message = "管理请求失败"
        raise RuntimeError(f"HTTP {error.code}: {message}") from None


def stop():
    request("stop", {})
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        state = request("state")
        if state["state"] in ("stopped", "error"):
            if state.get("error"):
                raise RuntimeError(state["error"])
            return state
        time.sleep(0.3)
    raise RuntimeError("机器人尚未停止，保留连接以等待正常清理")


if __name__ == "__main__":
    import sys
    route = sys.argv[1] if len(sys.argv) > 1 else "state"
    if route == "stop":
        result = stop()
    elif route == "start":
        result = request("start", {})
    else:
        result = request(route)
    print(json.dumps(result, ensure_ascii=False, indent=2))
