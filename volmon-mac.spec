# -*- mode: python ; coding: utf-8 -*-
"""volmon-mac 1.0 macOS 打包配置（PyInstaller）。

产物：dist/volmon-mac.app（无终端窗口，双击后自动打开浏览器面板）
用法：python -m PyInstaller volmon-mac.spec --noconfirm
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
    "volmon.server", "volmon.sim", "volmon.storage",
    # 第三方
    "aiohttp", "numpy", "sounddevice", "websockets", "rich",
    "email.mime.application", "email.mime.multipart", "email.mime.text",
    "smtplib", "wave", "sqlite3",
]

# sounddevice 自带的 PortAudio 动态库（不带上就采集不了音频）
try:
    import sounddevice

    _sd = os.path.dirname(sounddevice.__file__)
    _pa = os.path.join(_sd, "_sounddevice_data", "portaudio-binaries", "libportaudio.dylib")
    if os.path.exists(_pa):
        binaries.append((_pa, "_sounddevice_data/portaudio-binaries"))
except Exception:
    pass

for pkg in ("aiohttp", "rich"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ["macapp.py"],
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

# onedir + COLLECT + BUNDLE 是 macOS .app 的标准写法：
# 依赖直接放在 .app 内部，启动时**不需要**解压几百 MB 到临时目录，秒开。
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,      # 二进制交给 COLLECT，保持 onedir
    name="volmon-mac",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # 桌面版：不弹终端窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="volmon-mac",
)

app = BUNDLE(
    coll,
    name="volmon-mac.app",
    icon=None,
    bundle_identifier="com.volmon.mac",
    info_plist={
        "NSMicrophoneUsageDescription": "volmon 需要麦克风权限来实时监测环境音量与噪音。",
        "NSHighResolutionCapable": True,
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": "1.0",
        "LSUIElement": False,
    },
)
