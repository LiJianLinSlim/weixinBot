"""从公开模板创建本地配置；已经存在的文件不会被覆盖。"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wxbot_framework.config import initialize_configs


def initialize(root=ROOT):
    return initialize_configs(root)


if __name__ == '__main__':
    created = initialize()
    print('已生成：' + '、'.join(created) if created else '本地配置已存在，未覆盖。')
