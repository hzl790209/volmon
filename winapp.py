"""volmon-win 桌面版的 PyInstaller 入口脚本（Windows）。

双击 .exe 时没有终端界面（console=False），这里：
  1. 先切到用户数据目录（%APPDATA%/volmon-win）
  2. 启动后端服务并自动打开浏览器

开发模式下直接 `python winapp.py` 也是同样行为，方便验证。
"""
import os
import sys

# 打包为「无控制台窗口」程序（console=False）后，从 cmd/PowerShell 运行时默认
# 拿不到控制台句柄，sys.stdout/stderr 为 None，于是所有提示与 `cli` 实时波形
# 都看不见（表现为"敲了命令没反应"）。这里主动附加到父进程控制台并重开
# 标准流：
#   - 从终端运行：正常输出，cli 实时界面可用
#   - 双击运行：无父控制台，AttachConsole 失败 → 静默跳过，行为不变
if getattr(sys, "frozen", False) and sys.platform == "win32":
    try:
        import ctypes

        # ATTACH_PARENT_PROCESS = -1
        if ctypes.windll.kernel32.AttachConsole(-1):
            if sys.stdout is None:
                sys.stdout = open("CONOUT$", "w", encoding="utf-8",
                                  errors="replace", buffering=1)
            if sys.stderr is None:
                sys.stderr = open("CONOUT$", "w", encoding="utf-8",
                                  errors="replace", buffering=1)
            if sys.stdin is None:
                try:
                    sys.stdin = open("CONIN$", "r", encoding="utf-8",
                                     errors="replace")
                except Exception:
                    pass
    except Exception:
        pass

# 保证无论从哪被调用都能 import 到项目模块
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from volmon import paths  # noqa: E402

paths.setup_runtime()     # 打包后切到用户数据目录（必须在导入配置之前）

from main import main as volmon_main  # noqa: E402

# 若显式给了子命令（如 `volmon-win cli`），原样透传；否则按桌面版处理：
# 起服务 + 自动开浏览器
# 注意：这里必须与 main.py 的子命令列表保持一致，漏掉一个（例如 trend）就会被
# 当成桌面版参数处理 -> 变成 `app <那个子命令>` 而报 "unrecognized arguments"。
_SUBS = {"serve", "web", "cli", "calibrate", "config", "quiet",
         "schedule", "policy", "clear", "logs", "mail", "rec", "app", "trend"}
# 看门狗循环由 launcher 以 `volmon-win.exe _watchdog loop` 拉起，这里单独分发
# 给 volmon.watchdog.main（避免再走 main.py 子命令路由）。
_LAU_VERBS = {"start", "stop", "restart", "status", "install", "uninstall", "web", "cli"}
_args = sys.argv[1:]
if _args and _args[0] == "_watchdog":
    # 内部看门狗入口：把剩余参数（如 loop / --interval）交给看门狗解析
    from volmon import watchdog as _wd
    sys.argv = [sys.argv[0]] + _args[1:]
    _wd.main()
elif _args and _args[0] in _LAU_VERBS:
    # 启动器动词：交给跨平台 launcher（负责 start/stop/install 等）
    from volmon import launcher as _lau
    _lau.main()
elif _args and _args[0] in _SUBS:
    volmon_main()
elif _args and _args[0].startswith("-"):
    # 全局参数（--help / -h 等）：原样交给主解析器，别塞成桌面版参数
    volmon_main()
else:
    sys.argv = [sys.argv[0], "app"] + _args
    volmon_main()
