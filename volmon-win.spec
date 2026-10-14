# -*- mode: python ; coding: utf-8 -*-
"""volmon-win Windows 打包配置（PyInstaller）。

产物：dist/volmon-win/volmon-win.exe（无控制台窗口，双击后自动打开浏览器面板）
用法：python -m PyInstaller volmon-win.spec --noconfirm
"""
import os

from PyInstaller.utils.hooks import collect_all

block_cipher = None

datas = [("web/index.html", "web")]
binaries = []
hiddenimports = [
    # 项目内部模块（很多是运行时延迟导入，静态分析扫不到，必须显式列出）
    "volmon.alerting", "volmon.alarm", "volmon.audio", "volmon.calibrate",
    "volmon.cli", "volmon.config", "volmon.logrot", "volmon.mailer",
    "volmon.paths", "volmon.recorder", "volmon.restart", "volmon.schedule",
    "volmon.server", "volmon.sim", "volmon.storage", "volmon.launcher",
    "volmon.watchdog",
    # 第三方
    "aiohttp", "numpy", "sounddevice", "websockets", "rich",
    "email.mime.application", "email.mime.multipart", "email.mime.text",
    "smtplib", "wave", "sqlite3",
    # Windows 专属（COM 计划任务 + 进程管理，运行时才 import，必须显式列出）
    "pywin32", "win32api", "win32con", "win32com", "win32com.client",
    "win32com.server", "win32com.shell", "pythoncom", "psutil",
]

# sounddevice 自带的 PortAudio 动态库：Windows 上实际文件名是
# libportaudio64bit.dll / libportaudio64bit-asio.dll 等，并不存在 portaudio.dll，
# 必须把整个 portaudio-binaries 目录下的 .dll 都带进包，否则冻结后无法加载音频。
try:
    import sounddevice

    _sd = os.path.dirname(sounddevice.__file__)
    _pa_dir = os.path.join(_sd, "_sounddevice_data", "portaudio-binaries")
    if os.path.isdir(_pa_dir):
        for _f in os.listdir(_pa_dir):
            if _f.lower().endswith(".dll"):
                binaries.append((os.path.join(_pa_dir, _f),
                                 "_sounddevice_data/portaudio-binaries"))
except Exception:
    pass

for pkg in ("aiohttp", "rich"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ["winapp.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # 图像处理/科学计算全家桶不参与打包（会显著增大体积且用不上）
    excludes=["tkinter", "matplotlib", "scipy", "pandas", "pytest", "PIL", "Pillow"],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# onedir：依赖放在 dist/volmon-win/ 内部，启动快；双击 volmon-win.exe 即用。
# （Windows 没有 BUNDLE，直接 EXE + COLLECT 即可）
#
# 两个入口：
#   volmon-win.exe  —— GUI 子系统（console=False），双击开浏览器面板
#   volmon-cli.exe  —— 控制台子系统（console=True），命令行实时监控/脚本调用
#   分开的原因：GUI 子系统程序从 cmd 启动时不继承控制台，stdout 为 None，
#   CLI 输出全丢；控制台版天生带窗口，cmd 也会等待其结束，体验正常。
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="volmon-win",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # 桌面版：不弹终端窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
)

exe_cli = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="volmon-cli",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,           # 命令行版：自带控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
)

coll = COLLECT(
    exe,
    exe_cli,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="volmon-win",
)
