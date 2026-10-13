# volmon 在 Windows 上开发与打包

本说明适用于把本项目（macOS 版 `volmon-mac` 的同一份代码）拿到 Windows 上做 **Windows 版（产品名 `volmon-win`）** 的开发与分发。代码结构已做了跨平台抽象，Windows 与 macOS 共用同一套 `volmon/` 包、`web/`、`main.py`，只需少量 Windows 专属胶水文件。

---

## 1. 目录中与 Windows 相关的文件

| 文件 | 作用 |
| --- | --- |
| `volmon/paths.py` | `app_support_dir()` 已按平台分流：Windows → `%APPDATA%/volmon-win`，macOS → `~/Library/Application Support/volmon-mac`。打包后数据写这里，不写进 `.exe` 目录（`.exe` 只读且会破坏签名）。 |
| `volmon/__init__.py` | `__product__` 随平台变成 `volmon-win` / `volmon-mac`，页面与接口展示的版本名自动正确。 |
| `volmon/launcher.py` | 跨平台一键启动器（start/stop/restart/status/web/cli + 透传）。被 `volmon.bat` 调用。 |
| `volmon.bat` | Windows 版 `volmon` 命令入口（双击或命令行 `volmon …`）。 |
| `winapp.py` | Windows 桌面版打包入口（`console=False`，双击自动起服务 + 开浏览器），对应 macOS 的 `macapp.py`。 |
| `volmon-win.spec` | Windows PyInstaller 打包配置（onedir EXE）。 |
| `build_win.bat` | 一键建 venv + 装依赖 + 打包。 |
| `start.bat` | 简易启动：建 venv、装依赖、前台 `python main.py serve`。 |

> `main.py` 的所有子命令（serve / web / cli / calibrate / config / quiet / logs / mail / rec / schedule / policy / clear）在 Windows 上均可用，逻辑本身跨平台。

---

## 2. 开发环境准备

### 2.1 前置条件
- Windows 10 / 11（64 位）
- Python 3.11+（建议 3.11 或 3.12；本项目在 macOS 上用 3.13 验证，Windows 上 3.11/3.12 更稳）
  - 安装时勾选 **“Add Python to PATH”**
- 一个麦克风（集成或 USB 均可）

### 2.2 安装依赖（自动）
仓库根目录已带两种一键启动脚本，首次运行会自动建 `venv` 并装依赖：

```bat
rem 方式 A：简易前台运行（看日志最方便，推荐开发期用）
start.bat

rem 方式 B：用 volmon 命令管理后台服务（与 macOS 体验一致）
volmon.bat serve        rem 前台起服务
rem 或：
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

依赖：`numpy` / `sounddevice` / `aiohttp` / `websockets` / `rich`。
其中 `sounddevice` 在 Windows 上会自带 `portaudio.dll`，**无需**单独装 Visual C++ 运行库以外的东西（PyInstaller 打包时会自动带上该 DLL 与 VC 运行库）。

### 2.3 运行与访问
```bat
volmon.bat            rem 后台拉起服务 + 自动打开浏览器面板
volmon.bat status     rem 看服务是否在跑（版本/采集设备/报警输出）
volmon.bat cli        rem 终端实时波形
volmon.bat stop       rem 停止后台服务
```
浏览器打开 `http://127.0.0.1:8000/`。

> 首次采集需要 **麦克风权限**：Windows 设置 → 隐私和安全性 → 麦克风 → 允许“桌面应用”访问麦克风。若权限未开，读数会恒为 `-100 dB`（数字静音，与 macOS 未授权表现一致）。

---

## 3. 数据落在哪

未打包（源码开发）时，数据写项目内的 `data/`（与 macOS 开发一致）：
`data/config.json`、`data/volmon.db`、`data/volmon.log`、`data/recordings/YYYY-MM-DD/*.wav`、`data/alerts.csv`。

打包成 `.exe` 后，数据落到：
```
%APPDATA%\volmon-win\        （即 C:\Users\<你>\AppData\Roaming\volmon-win\）
    data\config.json
    data\volmon.db
    data\recordings\...
```
这样 `.exe` 目录保持只读可携带，重装不丢数据。

---

## 4. 打包成 Windows 应用

```bat
build_win.bat
```
产物：`dist\volmon-win\volmon-win.exe`（onedir，双击即用，无控制台窗口，自动开浏览器）。

要点：
- 用 `onedir + COLLECT + EXE`（`console=False`），**不用 onefile**：避免每次启动解压上百 MB、启动慢。
- `sounddevice` 的 `portaudio.dll` 已在 `volmon-win.spec` 里通过 `binaries` 自动带入；若你的 sounddevice 版本路径不同，脚本里的 `try/except` 会优雅跳过（但采集将不可用，需手动确认 DLL 存在）。
- 若要做安装包（NSIS / Inno Setup），把 `dist\volmon-win\` 整个目录打进去即可，入口 `volmon-win.exe`。

---

## 5. 与 macOS 版的差异（开发时注意）

| 关注点 | macOS | Windows |
| --- | --- | --- |
| 产品名 | `volmon-mac` | `volmon-win` |
| 数据目录 | `~/Library/Application Support/volmon-mac` | `%APPDATA%/volmon-win` |
| 桌面入口 | `macapp.py` | `winapp.py` |
| 打包配置 | `volmon-mac.spec`（含 `BUNDLE`） | `volmon-win.spec`（无 `BUNDLE`） |
| 一键命令 | `~/bin/voicemonitor`→`volmon`（bash） | `volmon.bat` |
| 后台服务管理 | `pgrep`/`nohup` | `taskkill` + `.volmon.pid`（见 `launcher.py`） |
| 麦克风权限 | 系统弹窗授权 | 设置→隐私→麦克风 |

代码里一律用 `sys.platform == "win32"` 判断平台，**不要**写死 macOS 路径；新增“写磁盘”的功能请用 `volmon/paths.py` 的 `data_root()`，新增“读随程序分发的静态文件”用 `resource_path()`，并加进对应 spec 的 `datas`。

---

## 6. 已知坑（Windows 实测前先知道）

1. **麦克风权限**：未授权 → 读数恒 `-100 dB`，波形一条直线。先去系统设置开权限。
2. **杀毒软件**：首次运行 `volmon-win.exe` 可能被 Windows Defender 误报（PyInstaller 打包常见），加入白名单或“仍要运行”即可。
3. **中文路径**：项目目录若放在含中文/空格的路径下，PyInstaller 一般能处理；但建议放在纯英文短路径（如 `D:\dev\volmon`）以减少意外。
4. **设备热插拔**：插拔 USB 麦克风后无需重启，Web 页面“重新扫描设备”即可（`AudioEngine.rescan_devices`）。
5. **后台服务残留**：用 `volmon.bat stop` 正常停；异常退出时 `data/restart.log` 里有日志，必要时任务管理器结束 `python.exe` / `volmon-win.exe`。

---

## 7. 测试

```bat
venv\Scripts\activate
pytest tests/ -q
```
（测试套件在 macOS 上已 100+ 用例通过；Windows 上路径相关用例会自动走 `win32` 分支。）
