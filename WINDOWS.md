# volmon 在 Windows 上开发与打包

本说明适用于把本项目（macOS 版 `volmon-mac` 的同一份代码）拿到 Windows 上做 **Windows 版（产品名 `volmon-win`）** 的开发与分发。代码结构已做了跨平台抽象，Windows 与 macOS 共用同一套 `volmon/` 包、`web/`、`main.py`，只需少量 Windows 专属胶水文件。

> **📦 安装包状态（2026-10-14）**：本仓库已包含 Windows 版完整源码（产品名 `volmon-win`，版本 **1.0.11**），且 **MSI 安装包已发布**（见 [GitHub Release v1.0.11](https://github.com/hzl790209/volmon/releases/tag/v1.0.11)）。当前在 Windows 上运行有两种方式：
> 1. **源码 / 便携运行**：直接 `start.bat` 或 `volmon.bat`（详见下文 §2）；
> 2. **自行构建便携 EXE**：`build_win.bat` 产出 `dist\volmon-win\volmon-win.exe`（onedir，双击即用，见 §4）。
>
> 若需要双击即装的 **MSI 安装包**（含「登录自启 + 崩溃自愈」计划任务），构建配方见 **§4.1**，需在 Windows 上安装 WiX Toolset v3 后照步骤执行。仓库已附 `volmon.wxs` / `volmon.zh-CN.wxl` / `build_win.bat` 等全部源文件。

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
- `sounddevice` 的全部 `portaudio` DLL 已在 `volmon-win.spec` 里通过 `binaries` 把 `portaudio-binaries/*.dll` 全部带入；若你的 sounddevice 版本路径不同，需确认 DLL 存在。
- 若要做安装包（NSIS / Inno Setup），把 `dist\volmon-win\` 整个目录打进去即可，入口 `volmon-win.exe`。

---

## 4.1 打包成 MSI 安装包（`volmon-win-x64.msi`）

目标：把 `dist\volmon-win\`（含 Python 运行时，无需用户装环境）封成 **双击即装、装完即用** 的 MSI，并自动注册「登录自启 + 崩溃自愈」计划任务。

成品：`volmon-win-x64.msi`（v1.0.9 约 42.6 MB，64 位，含嵌入式 cab，单文件分发；因内含两个入口 exe —— 桌面版 `volmon-win.exe` + 控制台版 `volmon-cli.exe`）。

### 安装包做了什么
- 安装到 `C:\Program Files\Volmon\volmon-win\`（per-machine，需管理员权限）。
- 开始菜单两个入口：
  - **“Volmon 音量监控”** → `volmon-win.exe web`（复用或拉起服务并打开浏览器面板）；
  - **“Volmon 命令行监控”** → `volmon-cli.exe cli`（控制台版，直接进入终端实时波形）。
- 安装末尾（结束页勾选框默认勾选）立即启动面板。
- 安装时以 SYSTEM 身份注册计划任务 `VolmonMonitor`（登录时触发 `volmon-win.exe start`：起服务 + 看门狗，崩溃后由看门狗经 COM 重启实现自愈）。
- 卸载时自动删除该计划任务（含安装失败回滚）。

### 终端实时界面（`cli`）的可调项
终端界面每帧整屏重绘，在 Windows 传统控制台 (conhost) 上重绘越频繁越容易肉眼看到闪烁。
默认已做优化（只在画面变化时重绘 + 不高于 6 次/秒 + 空闲时完全静止），仍觉闪可以：
```bat
volmon-cli.exe cli --fps 4        rem 整屏重绘上限降到 4 次/秒（更稳，代价是波形略顿）
volmon-cli.exe cli --no-screen    rem 不用备用屏（个别老控制台更顺）
volmon-cli.exe cli --no-trend     rem 只保留实时波形
```
> 排查这类"闪"的经验：`screen=True` 时 rich 会把每帧补齐到整屏尺寸，所以重绘量 = 终端面积 × 频率；
> 换 Windows Terminal 跑会明显更顺。改完务必用「整屏重绘次数」而不是肉眼来回归验证
> （见 `volmon/cli.py` 顶部注释与 `CHANGELOG.md` 1.0.9 的验证方法）。

### 构建步骤（用 WiX Toolset v3，`buildtools\wix311\` 已内置）

> **务必用项目自带的 venv 构建**：`venv\Scripts\python.exe -m PyInstaller ...`。
> 托管环境的 PyInstaller 里没有 aiohttp/sounddevice/psutil，`collect_all()` 会**静默**返回空，
> 打出来的包装到别的机器上才会报 `ModuleNotFoundError`。
> 另外沙箱会拦 `rm -rf dist\volmon-win`，用 `--distpath distN --workpath buildN` 侧路构建即可。

```bat
rem 1) 先产出 onedir（若已是最新可跳过）
venv\Scripts\python.exe -m PyInstaller volmon-win.spec --distpath dist10 --workpath build10 --noconfirm

rem 1.5) 确认 distN 里没有冒烟测试的临时文件（_srv*.log / _trend.txt 等），否则会被打进安装包

rem 2) 重新采集冻结目录 -> appfiles.wxs（注意 -var 与 -dr）
buildtools\wix311\heat.exe dir dist10\volmon-win ^
  -cg AppFiles -gg -sfrag -sreg -srd -scom -dr INSTALLFOLDER -var DistSrc -out appfiles.wxs

rem 3) 后处理（相对路径 + Win64 + 稳定 File Id），脚本已固化：
venv\Scripts\python.exe packaging\postprocess_appfiles.py dist10

rem 4) 编译 + 链接（用 -loc 固定中文代码页 GBK/936）
buildtools\wix311\candle.exe volmon.wxs appfiles.wxs -out obj\ -ext WixUIExtension
buildtools\wix311\light.exe -ext WixUIExtension -loc volmon.zh-CN.wxl ^
  obj\volmon.wixobj obj\appfiles.wixobj -o volmon-win-x64.msi -spdb

rem 5) 打交付 zip（MSI + 文档 + 源码 + 构建脚本）到桌面
venv\Scripts\python.exe packaging\make_zip.py
```

> 换成新版本目录（如 `dist10`）时，记得同步改两处：`volmon.wxs` 里 `Icon SourceFile="dist10/..."`，
> 以及后处理脚本的参数（脚本会自动把 `Source=` 指到同一目录，避免两边不一致）。

### 版本发布（新版本能原地升级）
每次交付都要同步升级三处，否则"同版本不同内容"装不上：
1. `volmon/__init__.py` 的 `__version__`（页面徽章 / CLI 状态条 / 接口都取它）；
2. `volmon.wxs` 顶部的 `<?define Version = "..." ?>`；
3. `volmon.wxs` 里 `Product/@Id` **换一个新 GUID**（`UpgradeCode` 保持不变）。
   配合已有的 `<MajorUpgrade>`，用户直接装新版即可覆盖旧版，不必先手动卸载。

### 必改项说明
- **中文代码页**：本机构建机若是非中文 ANSI（如 1252），`Product/@Codepage="936"` 会被 link 忽略，必须用 `volmon.zh-CN.wxl`（`Codepage="936"`）+ `light -loc` 才能把中文写入 MSI 字符串表。
- **`$(DistSrc)` 不能靠 `-d` 绑定**：`heat -var DistSrc` 生成的是 `$(DistSrc)\...`，直接 `-dDistSrc=...` 在 link 阶段不生效；改为在 `appfiles.wxs` 里把 `$(DistSrc)\` 文本替换成相对路径 `dist\volmon-win\`（相对 `.wxs` 所在项目根）最稳妥。
- **自定义操作选 Type 34**（`FileKey`+`ExeCommand`），不要用裸 `ExeCommand` 或 `Property` 型 Type 50：前者在 WiX 3 需要额外 `BinaryKey/Directory/FileKey/Property` 之一，后者需 DLL 包装（WixQuietExec）。
- **`msilib` 不可用**：本机所有 Python 都没有 `msilib`，无法用标准库生成 MSI，故走 WiX。

### 验证
- `dark.exe -x _extract volmon-win-x64.msi` 应能导出约 330+ 个文件（含 `volmon-win.exe`、`_internal\web\index.html`、`_internal\_sounddevice_data\portaudio-binaries\libportaudio64bit.dll`）。
- 计划任务注册需安装以管理员运行（MSI 提升）；普通用户双击安装会被 UAC 提权。

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
