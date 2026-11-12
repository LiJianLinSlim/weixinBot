"""隐藏的退出清理守护入口；由原生服务启动，不接收或发送微信消息。"""
import argparse
import logging
from logging.handlers import RotatingFileHandler
import sys

from run_selftest import ROOT

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wxbot_framework.bridge_lifecycle import watch_owner
from filehelper_service import cleanup_loaded_bridges


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner-pid", type=int, required=True)
    parser.add_argument("--wechat-pid", type=int, required=True)
    parser.add_argument("--stop-event", required=True)
    parser.add_argument("--ready-event", required=True)
    args = parser.parse_args()
    directory = ROOT / "logs"
    directory.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO,
                        handlers=[RotatingFileHandler(directory / "bridge_watchdog.log", maxBytes=256 * 1024,
                                                      backupCount=1, encoding="utf-8")],
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        watch_owner(args.owner_pid, args.wechat_pid, args.stop_event, args.ready_event, cleanup_loaded_bridges)
        return 0
    except Exception:
        logging.exception("退出清理守护未能完成；下次启动会再次检查残留")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
