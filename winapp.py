"""volmon-win 桌面版的 PyInstaller 入口脚本（Windows）。

双击 .exe 时没有终端界面（console=False），这里：
  1. 先切到用户数据目录（%APPDATA%/volmon-win）
  2. 启动后端服务并自动打开浏览器

开发模式下直接 `python winapp.py` 也是同样行为，方便验证。
"""
import os
import sys

# 保证无论从哪被调用都能 import 到项目模块
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from volmon import paths  # noqa: E402

paths.setup_runtime()     # 打包后切到用户数据目录（必须在导入配置之前）

from main import main as volmon_main  # noqa: E402

# 若显式给了子命令（如 `volmon-win cli`），原样透传；否则按桌面版处理：
# 起服务 + 自动开浏览器
_SUBS = {"serve", "web", "cli", "calibrate", "config", "quiet",
         "schedule", "policy", "clear", "logs", "mail", "rec", "app"}
_args = sys.argv[1:]
if _args and _args[0] in _SUBS:
    volmon_main()
else:
    sys.argv = [sys.argv[0], "app"] + _args
    volmon_main()
