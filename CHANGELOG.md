# Changelog

本文件为官方完成记录。格式见《音量监控软件开发规范》§3.2。每次交付必须追加一个条目并升级 `volmon/__init__.py` 版本号。

> **[Windows 开发预览]** 同一份代码已支持编译 Windows 版（产品名 `volmon-win`），无需重写业务逻辑。
> 平台差异抽象在：`volmon/paths.py`（`%APPDATA%/volmon-win` 数据目录）、`volmon/__init__.py`（平台相关产品名）、
> `volmon/launcher.py` + `volmon.bat`（跨平台一键启动器）、`winapp.py` + `volmon-win.spec`（Windows 桌面打包）。
> 详见 `WINDOWS.md`。Windows 版已发布为 **volmon-win 1.0.11**（2026-09-30），更新记录见下方。

## 1.0.11（2026-09-30）

### 修复：网页修改「报警声输出设备」不生效 + 增加显式保存按钮
- **现象**：MSI 安装后，网页里改了「报警声输出设备」（3.5mm 耳机 / USB 耳机 / 本机扬声器）
  但报警声仍从原设备出来；且没有明确的"保存"动作，用户无法确认是否真的保存成功。
- **根因**：
  1. 报警面板此前是**静默自动保存**（每个控件 `onchange` 就 POST），既无成功/失败反馈，
     又依赖服务主循环下一轮 `sync_from_config` 才把新设备同步到运行中的告警管理器；
     在个别机器上用户改完立刻点「试听」，会仍从旧设备出声，表现为"修改不生效"。
  2. 主「保存设置」按钮的提交体**漏掉了 `alarm_output_device`**，只发输入设备/阈值等，
     输出设备只能靠自动保存那条路径，链路脆弱。
- **方案**：
  1. 后端 `post_config` / `post_schedule` 在写入配置后**立即**调用
     `alarm.sync_from_config(config.get_all())`，设备变更即时生效，不再等主循环。
  2. 报警面板新增**「💾 保存报警设置」按钮**：改完开关/时长/输出设备后必须点它才提交，
     并给出 `✅ 已保存并立即生效：…输出设备：xxx` / `❌ 保存失败：…` 明确反馈；
     取消原来的静默自动保存（仅保留改动时的即时提示文案）。
  3. 主「保存设置」按钮的提交体补上 `alarm_output_device`，两条保存路径都覆盖输出设备。
  4. `GET/POST /api/alarm/test`（试听）现在在返回消息里写明当前生效的输出设备
     （「已播放试听音（约 2 秒），应从【xxx】发出」），方便用户确认识别是否真的生效。
- 验证：源码 + 冻结版（dist10）均验证 `POST /api/config` 改设备后
  `GET /api/alarm/test` 的 `device` 字段即时变为新设备；冻结版 `/` 返回的 HTML
  含 `saveAlarm`、无 `bindAlarmAutosave`。

## 1.0.10（2026-09-29）

### 修复：CLI 历史趋势区残余频闪（真机录屏复现）
- **根因**：v1.0.9 已把重绘频率压下来，但 rich `Live` 每次 refresh 本质是**整屏重写**；
  顶部时钟/当前分贝每秒都在变化，导致从未变化的历史趋势区也跟着每秒被重写一遍。
  Windows 传统控制台（conhost）重写中文双宽字符行时肉眼可见，录屏逐帧分析可捕捉到
  说明行出现半重绘的"变暗帧"。
- **方案**：CLI 实时界面改用**行级 diff 渲染**（`_DiffPainter`）替代 rich `Live`：
  - 启用 Windows VT 序列（ENABLE_VIRTUAL_TERMINAL_PROCESSING）；
  - 每帧渲染为 ANSI 行 + 纯文本行，与上一帧逐行比较；
  - **只重写内容真正变化的行**（VT 光标定位），历史趋势等静态区一个字节都不写；
  - 内容完全无变化时输出 0 字节；窗口尺寸变化时自动全量重绘。
- 实测（重定向 20 秒采样）：输出量从整屏方案约 3600 行降到 324 行（↓90%+）；
  单元验证：变化帧只写 1 行、无变化帧 0 字节、resize 全量重绘正确。
- `--no-screen` 现在的含义改为"回退 rich Live 兼容渲染"（逃生通道，默认无需使用）；
  `--fps` 语义不变（刷新上限）。

## 1.0.9（2026-09-29）

### 修复：CLI 历史趋势区频闪（第一轮）+ 命令行入口
- `volmon-win.exe cli` 无响应：修复 frozen 模式下 `_run("cli")` 自我递归调用，
  改为进程内 `_cli_inproc()`；新增控制台子系统 `volmon-cli.exe`，命令行直达。
- CLI 频闪第一轮修复：`Live(auto_refresh=False)` + 内容变化检测 + fps 节流；
  历史趋势区块**恒定高度**（数据为空也补空行），失败时只改固定提示行不再跳高度；
  趋势数据改异步拉取（不阻塞渲染）+ 严格节流（失败也计时，杜绝每帧重拉）；
  `_fit_lines()` 按**显示宽度**裁行（中文双宽算 2 格），保证"作者行数=渲染行数"
  不变量，杜绝底部趋势被挤出屏幕。
- `cli` 子命令新增 `--fps`（默认 6）、`--no-screen`；`winapp.py` 补 `trend` 白名单。
- 打包：PyInstaller 双入口 spec（GUI + console），`Icon` 与快捷方式指向稳定文件。

## 1.0.8（2026-09-29）

### 修复：真机安装后的两处启动报错
- `launcher._acquire_lock` FileNotFoundError：数据目录未创建（`paths.py` 补 `makedirs`）。
- `ModuleNotFoundError: No module named 'aiohttp'`：误用托管 venv 的 PyInstaller 打包
  （缺 aiohttp/sounddevice/psutil，`collect_all()` 静默返回空）。改用项目 venv 构建。

### 新增：MSI 安装包（首个可分发版本）
- WiX Toolset v3（buildtools\wix311）：heat 采集 onedir → postprocess（相对路径/Win64/
  稳定 FileId）→ candle/light（-loc 固定 GBK 936 中文）。
- 计划任务 VolmonMonitor：安装时注册（Type 34 自定义操作），随登录自启后台服务。
- MajorUpgrade：同 UpgradeCode 换 ProductCode 即可原地覆盖升级，无需先卸载。
- 桌面/开始菜单快捷方式：主程序 + `Volmon 命令行` (volmon-cli.exe cli)。

## [volmon-mac 1.0.3] - 2026-09-27（**1.0 补丁**）

> 对外产品版本**仍称 volmon-mac 1.0**（定版规则：修补按 1.0.x 递增，不改对外名）。
> 本次修复「触发录音」面板的「立即清理」按钮无效的问题。

### 1.0.3：修复「立即清理」按钮无效（改为真正清空所有录音）

**现象（用户反馈）**：点「触发录音」面板的「立即清理」，录音删不掉，看起来像没反应。

**根因**：原「立即清理」调用的是 `recorder.prune()`，而 `prune()` **只按保留策略删文件**
（≥ `rec_keep_days`(默认 30 天) 的旧目录，或超出 `rec_max_files`(500)/`rec_max_mb`(2GB) 配额的最旧文件）。
用户测试时产生的都是**当天、数量很少**的录音，根本不满足任何一条删除条件 → `prune()` 删 0 个
→ 界面显示"已清理 0 个文件"，被误判为按钮坏了。这是**语义错配**：一个叫"立即清理"的按钮，
用户点它是想"把录音清掉"，而不是"按 30 天策略再筛一遍"。

**改法——让按钮名副其实**：
- `volmon/recorder.py`：新增 `clear_all()`，直接**清空所有历史录音**（删按日目录下的全部 WAV + 空目录）。
- `volmon/server.py`：新增 `POST /api/recordings/clear` → 调 `clear_all()`；路由已注册。
- `web/index.html`：「立即清理」按钮改为先 `confirm("确定要立即清空所有录音吗？…不可恢复")`，
  确认后才 `POST /recordings/clear`；提示文案改为"已清空 N 个录音文件"；面板说明同步澄清
  （"立即清理=清空所有历史录音；按保留天数/配额的自动清理仍在后台进行"）。
- 保留 `prune()` 与 `POST /api/recordings/prune`（供 CLI `python main.py rec --prune` 按策略清理用），不受影响。
- `volmon/__init__.py`：`__version__` 1.0.2 → **1.0.3**；`build_dmg.sh` 版本号 1.0.3。

#### 验证
- `pytest tests/ -q` ⇒ **101 passed**（新增 `test_clear_all_removes_everything`：写 3 个假 WAV →
  `clear_all()` 返回 3 且目录被删、`count==0`；再调一次幂等返回 0；保留原 100 例）
- 重建 `volmon-mac-1.0.3.dmg`（`hdiutil verify` VALID）并安装到 `/Applications`；
  冻结二进制 `--fake --port 8014` 头less 实测：`/api/health` ⇒ `volmon-mac 1.0.3`；
  `POST /api/recordings/clear` 对带录音的数据目录返回 `removed` 为实际文件数、`count` 归 0。

## [volmon-mac 1.0.2] - 2026-09-27（**1.0 补丁**）

> 对外产品版本**仍称 volmon-mac 1.0**（定版规则：修补按 1.0.x 递增，不改对外名）。
> 本次只改报警冷却时长的**出厂默认值**，不改变任何已保存配置或监测逻辑。

### 1.0.2：报警冷却默认时长 3s → 5s

用户手动把报警冷却设成 5 秒后体验更好，于是把出厂默认值也调成 5 秒，使**新安装 / 「恢复默认设置」后直接就是 5 秒**：

- `volmon/config.py`：出厂默认 `alarm_cooldown_sec` 由 `3.0` 改为 `5.0`；
  分时段策略的 `day_alarm_cooldown_sec` / `night_alarm_cooldown_sec` 同步由 `3.0` 改为 `5.0`。
- 文档：`README.md` 配置表与说明同步（冷却默认 3→5；"播放+冷却默认共 13s"改为 15s）；
  `build_dmg.sh` 版本号 `1.0.1 → 1.0.2`。
- `volmon/__init__.py`：`__version__` 1.0.1 → **1.0.2**。

#### 注意
- **已保存的配置不受影响**：运行中 `data/config.json` 早已是用户手设的 5 秒；本次只动源码默认值与新构建，
  不触碰线上 8000 服务，也不改任何已落盘配置。
- 仅换默认值，报警/监测逻辑一行未改；测试均自建配置 dict、不依赖该默认值，故 `pytest` 全量应仍 **100 passed**。

#### 验证
- `py_compile` 通过；`pytest tests/ -q` ⇒ **100 passed**（预期）
- `dist/volmon-mac.app` 重新构建并安装到 `/Applications` 后：`/api/health` ⇒ `volmon-mac 1.0.2`；
  「恢复默认设置」后 `alarm_cooldown_sec` 应为 5.0

## [volmon-mac 1.0.1] - 2026-09-27（**1.0 补丁**）

> 对外产品版本**仍称 volmon-mac 1.0**（定版规则：修补按 1.0.x 递增，不改对外名）。
> 本次只改"录音与报警声的关系"，监测逻辑、报警逻辑、打包形态均不变。

### 1.0.1：开启声音报警时，报警触发即截断录音（替换原"跳过污染窗口"）

**背景 / 用户反馈**：上版（1.0 增强）用"知道报警声在响的时段、那段直接不写"来避免报警声进录音，
但实测录音**仍被报警音污染**。根因是污染窗口依赖"报警时长 + 余量"去雕刻录音，边界与延迟都敏感，不够可靠。

**改法——从"触发点直接切断"，彻底换思路**：
报警声是从**触发点**才响起来的，所以**报警一触发，立刻把正在进行的录音截断在触发瞬间**——
前置段（触发前那段真实环境音，本来就是干净的）照常写盘；触发点之后的声音（含报警声本身）一个字节都不写。
这比"计算要跳过哪一段"可靠得多：不管报警时长 / 冷却 / 余量怎么设，报警声在物理上不可能再出现在录音里。

- `volmon/recorder.py`：移除旧的"污染窗口跳过"逻辑（`set_alarm_probe` / `_skip_now` / 不写环形缓冲等）；
  新增 `cut_at_trigger(reason)`——把 `pending` 里所有进行中的任务标记为 `cut=True` 并移入 `_cut_queue`，
  `tick()` 立即把 `_cut_queue` 写盘收工；WAV 标注"报警触发·已截断（仅含触发前音频）"。
- `volmon/alarm.py`：移除 `_sounding` 窗口登记与 `is_contaminating()` / `sounding_window()`（不再需要）。
- `volmon/server.py`：报警触发（`alarm_fired`）后调用 `recorder.cut_at_trigger(...)`；
  配置同步项改为 `rec_cut_on_alarm`；`get_recordings` / 状态暴露 `cut_on_alarm`。
- `volmon/config.py`：移除 `rec_exclude_alarm` / `rec_alarm_pad_ms`；新增 `rec_cut_on_alarm`（**默认 true**）。
- Web（`web/index.html`）：「触发录音」面板改为「报警触发即截断（仅保留触发前）」开关（替代原「排除报警声」）；
  状态行显示 **✂ 报警触发即截断**。
- `volmon/__init__.py`：`__version__` 1.0 → **1.0.1**。
- 测试：重写 `tests/test_recorder.py`（5 例，含严格对照：关掉开关时报警声必须被录进去）、
  清理 `tests/test_alarm.py` 的 4 条过时用例。

#### 验证
- 单测：严格对照 + 前置段保留 + 无 pending 时幂等 + 状态暴露 ⇒ `pytest tests/ -q` **100 passed**
  （对照用例：关闭 `rec_cut_on_alarm` 后，同样的报警声确实进入 WAV、文件更长；证明开关真的生效，不是形同虚设）
- **真机 A/B**（本机外置麦克风，报警开 + 录音开，报警声 880 Hz）：
  - 开启截断：报警触发后录音在触发点定格，文件**只含触发前音频**，峰值远低于报警声特征峰
  - 关闭截断：报警声（880 Hz）如实出现在录音里、峰值显著升高
- 页面 `cut_on_alarm=true` 在报警触发后即时显示；日志写"报警触发即截断"

### 打包分发（macOS DMG）— volmon-mac-1.0.1.dmg

产物：`volmon-mac-1.0.1.dmg`（23 MB）+ `dist/volmon-mac.app`（45 MB）。
构建沿用 `build_dmg.sh`（PyInstaller → ad-hoc 签名 → `hdiutil create -format UDZO`），
代码层仅改录音/报警交互，打包流程与 1.0 完全一致。

#### 验证
- `dist/volmon-mac.app` 直接运行：`/api/health` ⇒ `volmon-mac 1.0.1`；数据落在 `~/Library/Application Support/volmon-mac/data/`
- DMG `hdiutil verify` ⇒ VALID；安装到 `/Applications/volmon-mac.app` 后运行正常
- `py_compile` 通过；`pytest tests/ -q` ⇒ **100 passed**

#### 构建修正（本 1.0.1 二次构建）
初版 1.0.1 DMG 中「报警触发即截断」**无法保存**。根因有二：
1. `server.py` 的 `CONFIG_KEYS` 白名单仍列旧键 `rec_exclude_alarm`/`rec_alarm_pad_ms`、漏了 `rec_cut_on_alarm`，
   POST 该键被静默丢弃（`post_config` / WS `set_config` 都按白名单过滤）；
2. `get_recordings` 返回的是**运行时 recorder 标志**而非用户保存的**配置值**，页面复读永远回到旧值。
已修正：`CONFIG_KEYS` 改列 `rec_cut_on_alarm`；`get_recordings` 改返回 `bool(config["rec_cut_on_alarm"])`；
采集循环每轮无条件同步 recorder 开关（不受报警静默期影响）。二次构建的 `volmon-mac-1.0.1.dmg`
已含此修正，`/api/recordings.cut_on_alarm` 与保存值一致（实测 POST false→False、POST true→True 均生效并落盘）。

## [volmon-mac 1.0] - 2026-09-27（**正式定版**）

macOS 版正式定版为 **volmon-mac 1.0**。功能集与内部开发版本 `2.13.0` **完全一致**，
这次只是**对外产品版本重新编号**（从开发用的 2.x 序列，改为面向用户的 1.0），代码未做功能改动。

- 版本号唯一来源仍是 `volmon/__init__.py`：
  - `__product__ = "volmon-mac"`（产品名）
  - `__version__ = "1.0"`（产品版本，**已定版**）
  - `__display__ = "volmon-mac 1.0"`（对外展示串）
- 上屏位置（规范 §1.5）全部改为显示完整产品版本：
  页面徽章 / 浏览器标签标题 / CLI 状态条 / `/api/health`、`/api/status`、`/api/schedule`、WS 广播
  （兼容起见仍保留 `version` 字段，另加 `product` 与 `version_display`）
- **版本规则**：定版后如还需修补，按 `1.0.x` 递增（1.0.1 / 1.0.2 …），对外仍称 volmon-mac 1.0；
  若新增较大功能再考虑 1.1

### 1.0 包含的能力（对应内部 2.13.0）
实时监测与校准 / 阈值与持续时长预警 / 声音报警（含输出设备选择、静音时段）/
分时段策略（白天·晚间）/ 当前生效策略明细 / 超量日志翻页与按时间段清空 /
日志按日切割与 30 天保留 / 每日报警日报邮件 / 触发录音（环形缓冲 + 前置录音，短促声音不丢开头）/
触发录音排除本机报警声（见下方「1.0 增强」）/ `voicemonitor` 一键启动 / macOS DMG 分发

### 验证
- `py_compile` 通过；`pytest tests/ -q` ⇒ **87 passed**
- 页面徽章与 `/api/health` 实测返回 `volmon-mac 1.0`

### 1.0 增强：开启声音报警时，不把报警声录进录音（2026-09-27）

> 版本号按用户要求**仍为 volmon-mac 1.0**。这属于 1.0 的能力增强（新增一个开关），
> 未改变对外产品版本。

**问题**：报警声是本机扬声器放出去的，会被同一台电脑的麦克风原样拾回（自激串扰）。
录进 WAV 就是一段刺耳的"嘀嘀"声，把真实环境音盖掉——用户明确要求"不要把报警的声音录进去"。

**思路**：软件层面没法把两个混在一起的声音再分开（真做回声消除 AEC 代价过大），
所以退一步采取更可靠的办法：**既然报警声是我们自己放的，那就知道它什么时候在响，那段时间直接不录**。

- `volmon/alarm.py`：播放前后登记**发声时间窗口** `_sounding=(start, end)`，
  新增 `is_contaminating(now_sec, pad_sec)` / `sounding_window()`。
  试听（`test_play`）也会登记——它放的同样是本机报警声。
- `volmon/recorder.py`：`feed()` 在污染窗口内**整块丢弃**音频帧，两个关键点：
  1. **不写环形缓冲** → 触发前的"前置录音"永远是干净的真实环境声
     （报警声那时还没响，本来也不该有它；写进去反而会污染之后所有录音的前置段）
  2. **不计入录音进度** → 后置段按"**有效音频时长**"计，报警响多久就顺延多久，
     最终仍凑满设定秒数，不会因为报警声占掉时间而录成半截。
     `tick()` 的兜底时长同步加上被跳过的时长（上限 60 秒，防止报警连连触发时任务挂死）
- 通过 `set_alarm_probe()` 注入探测函数（server 传入 `alarm.is_contaminating`），
  recorder 不反向依赖 alarm 模块
- 新增配置：`rec_exclude_alarm`（**默认 true**）、`rec_alarm_pad_ms`（默认 300，
  覆盖声卡输入/输出延迟与房间混响尾巴）
- Web：「触发录音」面板加「排除报警声」开关 + 余量输入；状态行显示 **🔇 正在屏蔽报警声**；
  保存日志写明"剔除报警声 X 秒"

#### 验证
- 单测 9 条（含**严格对照**：关掉开关时同样的报警声必须被录进去，否则屏蔽逻辑形同没写也测不出来）
  ⇒ `pytest tests/ -q` **104 passed**
- **真机 A/B**（本机外置麦克风，报警开 + 录音开）：
  - 开启剔除：日志 `录音已保存：rec_204612_160.wav（3.1 秒，其中**剔除报警声 11.5 秒**）`，峰值 **0.022**
  - 关闭剔除：峰值 **0.047**，且**频谱最强峰正好是 880 Hz**（报警声特征频率）
  - 页面 `masking=true` 在 24 秒观测窗内命中 17 次


### 打包分发（macOS DMG）— 2026-09-27

> 版本号按用户要求**仍为 volmon-mac 1.0**（定版不变）：本次只是新增**分发形态**，
> 未改动任何监测逻辑，因此不按 1.0.x 递增。

产物：`volmon-mac-1.0.dmg`（23 MB，含 `volmon-mac.app` 45 MB 压缩后）+ `dist/volmon-mac.app`

- 新增 `macapp.py`：**桌面版入口**。双击 .app 时没有终端，因此它先切到用户数据目录，
  再执行新的 `main.py app` 子命令 = **启动服务 + 就绪后自动打开浏览器**（等健康探测通过才开，
  最多等 30 秒，避免浏览器先报"无法连接"）。带子命令时（`... volmon-mac cli`）原样透传给 `main.py`。
- 新增 `volmon/paths.py`：**程序资源**与**用户数据**分离（打包正确性的关键）
  - `resource_path()` → 包内只读资源（`web/index.html`），打包后指向 `sys._MEIPASS`
  - `data_root()` / `setup_runtime()` → 打包后指向并 `chdir` 到
    `~/Library/Application Support/volmon-mac`（**绝不写 .app 内部**：那里只读，写进去还会破坏签名）
  - 未打包时行为完全不变，源码运行不受影响
- `volmon/server.py`：`ROOT` 改取 `data_root()`；首页改从 `resource_path()` 读
- `volmon/restart.py`：打包后没有 `main.py`，「重启服务」改为重新 exec 自身
- 新增 `volmon-mac.spec`（PyInstaller）：`onedir + COLLECT + BUNDLE`，
  `console=False`（不弹终端）、`NSMicrophoneUsageDescription`（麦克风权限文案）、
  带上 sounddevice 自带的 PortAudio 动态库（不带就采集不了）
- `main.py`：新增 `app` 子命令（`--host/--port/--fake`）
- 新增 `POST /api/service/stop` + 网页「■ 停止服务」按钮：**桌面版没有窗口也没有菜单栏**，
  双击后看不出进程在哪，必须有正规退出途径（实测：点了之后进程退出、端口释放）
- 新增 `build_dmg.sh`：一键构建（PyInstaller → ad-hoc 签名 → 组织 DMG 内容 → `hdiutil create -format UDZO`）

#### 构建环境踩坑（已在脚本内规避，重复构建可直接 `./build_dmg.sh`）
1. `PYTHONPATH` 若被注入 `sitecustomize.py` 代理层，会拦截文件读写导致打包失败 → 构建时 `env -u PYTHONPATH`
2. PyInstaller 二进制缓存默认在 `~/Library/Application Support/pyinstaller`，受限环境可能不可写 → 设 `PYINSTALLER_CONFIG_DIR` 到 /tmp
3. `hdiutil` 需用绝对路径 `/usr/bin/hdiutil`，否则可能被 PATH 里的代理层拦掉 /Volumes 写入
4. 脚本**不做 `rm -rf`**：旧产物一律 `mv` 到 /tmp，中间目录用时间戳新建

#### 验证（实机跑通，非 only-编译通过）
- `dist/volmon-mac.app` 直接运行：`/api/health` ⇒ `volmon-mac 1.0`；首页 200；
  **数据落在 `~/Library/Application Support/volmon-mac/data/`（不在 .app 内部）**
- 安装到 `/Applications` 后运行：读到用户配置（阈值 50 dB / 外置麦克风 / 校准 9.54 / 邮件开 / 录音开），
  音频采集启动，并**实时触发保存了 WAV**（环形缓冲 + 前置录音在包内同样生效）
- DMG：`hdiutil verify` ⇒ `checksum ... is VALID`；挂载后含 `volmon-mac.app` + `Applications` 快捷方式 + `使用说明.txt`
- `py_compile` 通过；`pytest tests/ -q` ⇒ **95 passed**
  （新增 `tests/test_paths.py` 8 例：用 monkeypatch 模拟 `sys.frozen=True`，
  锁死"打包后数据落用户目录 + 资源从 `_MEIPASS` 取 + 源码运行行为不变"）

### 已知限制（本包未做正式签名）
- 未使用付费 Apple 开发者证书，仅 ad-hoc 签名：**本机能跑**；分发给别人会被 Gatekeeper 拦，
  需「右键 → 打开」。若要正式分发，需 `codesign --sign "Developer ID Application: ..."` + `notarytool` 公证。
- 架构为构建机架构（x86_64 / Intel）。Apple Silicon 机器可用 Rosetta 运行；
  若要原生 arm64，需在对应机器上重新执行 `./build_dmg.sh`。

## [2.13.0] - 2026-09-27
### Added
- **触发录音：把"触发报警的那段真实声音"完整存成 WAV**（新模块 `volmon/recorder.py`）
  - **核心设计——常驻环形缓冲 + 前置录音（pre-roll）**：声音往往很短（敲门/喊叫/器物掉落），
    而"发声 → 算 dB → 判超阈值 → 判持续时长 → 打开文件准备写入"这条链路有延迟，
    **等报警了才按下录音键必然漏掉开头**。因此采集流一直在跑，
    最近 N 秒原始音频**常驻内存环形缓冲**；触发瞬间直接从缓冲里把"触发前 N 秒"取出来，
    再接着录"触发后 M 秒"，拼成完整一段。触发链路再慢、声音再短，开头都不缺。
  - 环形缓冲用 numpy 预分配数组，只做 memcpy（一块 2048 帧≈8 KB）；
    内存占用 ≈ 采样率 × 秒数 × 通道 × 4 B（44.1 kHz × 10 s × 1 ≈ 1.8 MB）
  - 存 **16-bit PCM WAV**（标准库 `wave`，**不新增依赖**），按日存放
    `data/recordings/YYYY-MM-DD/rec_HHMMSS_ms.wav`
  - 保留策略：默认 **30 天 + 最多 500 个 + 2 GB**，超出自动删最旧的；
    可点「立即清理」或 `python main.py rec --prune`
  - **与告警记录打通**：`alerts` 表与 CSV 新增 `audio_file` 列；录音写盘后用触发时刻
    反查所属那段超标并回填（`Storage.link_audio()`）；
    **超量告警表格新增「录音」列，点 ▶ 直接播放/下载**
  - 页面「触发录音」面板：开关、触发时机（正式报警 / 刚超阈值 / 不自动录）、
    前后时长、文件数与容量配额、保留天数、最近录音列表（可直接播放）
  - 命令行：`python main.py rec` / `--list` / `--prune`
  - 接口：`GET /api/recordings`、`POST /api/recordings/prune`、`GET /api/audio/<day>/<file>.wav`
- 采集引擎新增原始音频出口：`AudioEngine.set_frame_sink(fn)`（在 **PortAudio 回调线程**里调用，
  必须只做 memcpy；录音器内部即如此）。模拟引擎 `SimEngine` 同样会产出模拟音频帧，
  因此 `--fake` 演示模式下录音链路也能跑通
### Changed
- 录音判据以**实际收到的音频帧数**为准（够 post 秒即收工），真实时间只作兜底，
  避免采集流中断时任务永远挂着不写盘
- 默认**关闭**录音（`rec_enabled=false`）——涉及录音，由用户自行确认后开启
### 已知限制
- 服务刚启动的头几秒环形缓冲还在填充，此时的录音"前置段"会短一些；运行片刻后即稳定
- 若同时开启了声音报警，报警声从扬声器播出也会被麦克风录进去（物理串扰）。
  建议报警输出改接耳机/远离麦克风的外放；录音里会如实包含报警声
### 验证
- L0 通过；`pytest tests/ -q` ⇒ **87 passed**（新增 9 条）
  - **关键用例**：喂 2 s 安静 → 插 0.2 s 突发 → 再喂 4 s（模拟判定延迟 4 秒）才触发，
    断言写出的 WAV **包含该突发声且位于前置段**（证明是"补录"来的，不是触发后才录到）
  - 另含：环形缓冲回绕取最近数据、未开启时不触发、未录够不写盘、并发上限 3、
    按天/按文件数/按大小清理、录音回填到**正确那条**告警、CSV 含 audio_file 列
- 端到端实跑（`--fake` 演示模式，pre=3 s/post=2 s）：服务启动后自动触发并生成
  `data/recordings/2026-09-27/rec_190851_301.wav`（188 KB）；验证后已清理演示文件并恢复配置

## [2.12.0] - 2026-09-27
### Added
- **`voicemonitor` 变成真正的一键启动**：以前不带参数只跑 `main.py cli`，
  而 CLI 依赖后端服务，**服务没起时输入命令等于"什么都没发生"**（本次实际复现：8000 端口 Connection refused）。
  现在 `voicemonitor`（无参数）会：
  1. 用 `/api/health` 探测服务；没在跑就 `nohup` **后台自动拉起**并等待就绪（最多 20 秒，超时打印 `data/restart.log` 末尾）
  2. 就绪后进入终端实时波形
- 新增便捷子命令：`voicemonitor start`（只起服务）、`status`（看版本/采集设备/报警输出）、
  `stop`（停服务）、`restart`；`web` / `cli` 也会自动确保服务已起。
  其余参数（`quiet` / `policy` / `schedule` / `clear` / `logs` / `mail` / `config` …）原样透传给 `main.py`
- 脚本改为**按自身所在目录定位项目**（`BASH_SOURCE` 推导 `PROJ`），整个目录搬走也能用；
  端口从 `data/config.json` 读取（不再写死 8000）
### Fixed
- 日报范围与发送时间的逻辑冲突：用户设了「每天 00:00 发 + 今天 00:00 至今」，
  这种组合在 00:00 触发时统计区间几乎是空的（必然是 0 条）。
  已将 `mail_day_offset` 由 `0` 改为 `1`（昨天全天），配合 00:00 即为"跨过午夜立刻出昨天完整日报"。
  若想收"当天"的日报，应把发送时间改到 23:50 左右、范围设为「今天 00:00 起」。
### 验证
- `bash -n` 语法检查通过；实测 `voicemonitor status`（未运行时提示）、
  `voicemonitor start`（自动拉起并就绪）、`voicemonitor mail`（透传正常）
- 用户环境：163 邮箱已配置成功并**实发成功**（18:40:31 已发送至 hunk.hu@163.com，251 条告警 + CSV 附件）

## [2.11.0] - 2026-09-27
### Added
- **每日报警日志自动发邮件**（新模块 `volmon/mailer.py`，只用标准库 `smtplib`，**不新增依赖**）
  - **报表内容**：当日超量次数 / 峰值 dB 及发生时刻 / 平均峰值 / 累计超标时长 / 最长一次 /
    时段分布（白天·晚间·全局）/ 阈值分布 / 其中开启声音报警的条数 / 明细表（正文最多 50 条）
  - **正文同时提供 HTML 与纯文本**，可选附带当天告警 CSV 附件（优先用已归档的
    `alerts_YYYY-MM-DD.csv`，没有就即时从库里导出一份）
  - **自动发送**：每天到点（默认 09:00）由服务后台发一次，报表范围默认**昨天全天**
    （可改为"今天 00:00 起"）；服务当天启动晚了会**补发一次**，同一天不会重复发；
    失败当天最多重试 3 次，不刷屏
  - 支持 SMTP_SSL(465) / STARTTLS(587) / 不加密；内置 QQ / 163 / Gmail / Outlook /
    腾讯企业邮 **一键填入** SMTP 参数
  - 页面「**每日报警邮件**」面板：开关、发送时间、报表范围、SMTP 与端口、账号、
    **授权码**、收件人（可多个）、附件与"无告警时不发"开关；三个按钮
    **保存 / 预览报表（新窗口打开邮件正文）/ 立即发送测试邮件**
  - 新接口 `GET /api/mail`（配置，密码脱敏）、`POST /api/mail/send`
    （`{"dry_run":true}` 预览、`{"day":"2026-09-26"}` 补发、`{"to":"..."}` 临时改收件人）
  - 命令行：`python main.py mail`（看配置）/ `--preview`（预览）/ `--send [--day YYYY-MM-DD] [--to x@y.com]`
- 新增配置项：`mail_enabled`、`mail_host`、`mail_port`、`mail_ssl`、`mail_starttls`、`mail_user`、
  `mail_password`、`mail_from`、`mail_to`、`mail_send_time`(09:00)、`mail_day_offset`(1=昨天)、
  `mail_attach_csv`、`mail_skip_if_empty`、`mail_timeout_sec`；内部状态 `mail_last_sent_day` /
  `mail_last_result`（页面上显示最近一次发送结果）
### Changed
- `Storage` 新增 `get_alerts_range(since_ts, until_ts)` 与 `export_alerts_range_csv()`
  （按闭开区间取告警，供日报与附件使用）；`get_alerts()` 的行转换抽成 `_alert_dict()` 复用
### 安全
- 邮箱密码请填**授权码**（QQ/163/Gmail 需在邮箱设置里开启 SMTP 后生成），**不要填登录密码**；
  `/api/mail` 返回给页面的密码一律脱敏为 `●●●●●●`
### 验证
- L0 通过；`pytest tests/ -q` ⇒ **78 passed**（新增 13 条：报表汇总与分布、空日报渲染、
  HTML/纯文本关键字段、SSL 与附件、STARTTLS 路径、空日报跳过、dry_run 不连 SMTP、
  发送失败只报错不抛异常、指定日期补发、到点判断与配置脱敏、`/api/mail`+预览+真发+保存配置、
  **同一天只发一次**、**失败当天最多重试 3 次**）
- 实跑：`/api/mail` 正常；昨日预览（0 条）与今日预览（224 条，峰值 97.4 dB）均正确生成

## [2.10.0] - 2026-09-27
### Added
- **日志按日切割成独立文件**（新模块 `volmon/logrot.py`）：此前 `data/volmon.log`（1.3 MB/9440 行）、
  `data/alerts.csv`、`data/restart.log` 都是**单文件无限追加**，现在按天切分：
  - `data/volmon.log` 只装**今天**，跨日自动归档为 `data/volmon_YYYY-MM-DD.log`
  - `data/alerts.csv` 只装**今天**，跨日自动归档为 `data/alerts_YYYY-MM-DD.csv`（保留 .csv 后缀，Excel 可直接开）
  - `data/restart.log` 同上，归档为 `data/restart_YYYY-MM-DD.log`
  - **采样数据不切割**：仍在单库 `data/volmon.db`，用「日志维护（清空）」清理
  - **超过保留天数（默认 30 天）的归档自动删除**；`log_keep_days=0` 表示永不删除
  - 切割在**跨天第一次写日志/写告警时**自动发生，也会在**服务启动时**把"昨天遗留的当前文件"归位，
    不会出现"今天的内容追加到昨天的文件里"
- 新增配置项：`log_rotate_daily`（默认 `true`）、`log_keep_days`（默认 `30`），
  可在页面「日志文件（按日切割）」面板直接改，也可 `python main.py config set log_keep_days 60`
- 新 `GET /api/logs/files`：返回三类日志的「当前文件 + 各天归档」清单（名称/大小/更新时间）
- 新 `POST /api/logs/rotate`：`{"force":true}` 立刻归档一次（主要用于验证/手动整理）
- 新面板「**日志文件（按日切割）**」：显示每类日志的今天文件与历史归档（带大小），
  可改「按日切割」开关与「保留天数」，并有「立即切割一次」按钮
- 命令行：`python main.py logs --files` 打印日志清单；`python main.py logs --rotate [--force]` 立刻切割
### Changed
- **按时间段清空告警时会同步重写对应日期的归档 CSV**：清掉 9 月 20 日的告警，
  `alerts_2026-09-20.csv` 也会跟着重写/删除，不会出现"库里没了、归档 CSV 里还在"
- `ensure_alerts_csv()` 升级老表头时，会**顺带把库里的历史告警按天拆成各自的归档 CSV**
- 配置项白名单统一收敛为 `server.CONFIG_KEYS`（REST 与 WebSocket 共用一份，避免两处漏改）
- `Config.set()` 对布尔项做了字符串兼容（`bool("false")` 原本会得到 `True`）
### 验证
- L0 通过；`pytest tests/ -q` ⇒ **65 passed**（新增 9 条：归档命名与解析、仅切割跨天文件、
  **handler 跨零点切割**、30 天保留与 `keep_days=0` 不删、告警 CSV 跨日切割、
  **清空后同步重写归档 CSV**、按天拆档重建、`/api/logs/files` 清单、`/api/logs/rotate` 强制切割）

## [2.9.0] - 2026-09-27
### Added
- **「当前生效策略」面板（策略明细）**：右栏顶部新面板，一张表列出此刻真正生效的一整套策略——
  策略来源（全局 / 分时段·白天·晚间）、当前时段与时段区间、距下次切换、
  预警阈值与持续时长、**声音报警开关 + 响铃时长 + 冷却**、报警输出设备、报警静音剩余、
  实际采集设备、校准状态、日志条数；有空档/重叠时黄字提示。每 20 秒自动刷新，也有手动刷新按钮。
  新接口 `GET /api/policy`；命令行 `python main.py policy` 可打印同样明细。
- **日志维护：一键清空 + 按时间段清空**：新面板「日志维护（清空）」
  - 范围：采样 + 超量告警 / 仅采样 / 仅超量告警
  - 时间：**全部（一键清空）** / 近 1 小时 / 近 24 小时 / 近 7 天 / **自定义起止时间**
  - 新接口 `POST /api/logs/clear`（`{"all":true}` / `{"hours":N}` / `{"start_ts":..,"end_ts":..}` + `scope`），
    非法参数返回 400；命令行 `python main.py clear --all|--hours N|--start ... --end ... [--scope ...] [--yes]`
  - **清空告警后会同步重写本地 `data/alerts.csv`**，不会出现"库里没了、CSV 里还在"
  （影响：volmon/storage.py〔`clear`/`counts`/`rebuild_alerts_csv`〕, volmon/server.py, main.py, web/index.html）
- **超量告警日志带上"当时生效的策略"**：`alerts` 表与 CSV 新增
  `period`（白天/晚间）、`schedule_applied`（是否分时段）、`alarm_enabled`（是否启用声音报警）、
  `alarm_duration_s`、`alarm_cooldown_s` 五列；告警表格新增「时段/策略」「声音报警」两列；
  文本日志也改为 `… | 策略=晚间（分时段） | 声音报警=开 10s`。
  实现：`AlertManager.set_context()` 保存策略快照，落库时一并写入。
### Changed
- 老库/老 CSV **自动迁移**：`alerts` 表缺列时 `ALTER TABLE` 补齐（历史数据不丢）；
  启动时若发现 `data/alerts.csv` 表头不是当前版本，自动按库内数据重写为含策略列的新表头。
### 验证
- L0 通过；`pytest tests/ -q` ⇒ **56 passed**（新增 9 条：策略列入库与 CSV、无 context 的兼容、
  告警器传递 context、**老库迁移补列且保留历史数据**、按范围/按时间窗清空、清空后重写 CSV、
  非法 scope 报错、`/api/policy` 明细字段、**`/api/logs/clear` 按小时+全清+非法参数 400**）

## [2.8.0] - 2026-09-27
### Added
- **分时段策略：白天 / 晚间两套管理策略**（新模块 `volmon/schedule.py`）
  - 两个时段**各自可设起止时刻**（HH:MM），**支持跨零点**（如晚间 22:00 → 07:00）
  - 每个时段独立设置：`阈值 dB` / `持续时长` / `报警开关` / `报警时长` / `报警后冷却`
  - 总开关 `schedule_enabled`（**默认关闭**）——关闭时完全沿用全局阈值与报警设置，
    与旧行为**逐字节一致**，不会悄悄改掉用户现有阈值（有回归用例保护）
  - **不写回配置**：只在运行时计算"有效值"，关掉分时段即可干净回到全局设置
  - 时段判定顺序「先白天后晚间」；两个时段若未覆盖满 24 小时，`validate()` 会给出
    「有 N 分钟空档 / N 分钟重叠」提示并在页面上黄字显示，空档时段按白天执行，
    保证任何时刻都有确定策略
  - 新接口 `GET /api/schedule`（当前时段/生效阈值/距下次切换/配置校验）与 `POST /api/schedule`
  - WS 广播新增 `period` / `period_label` / `schedule_applied`；阈值本身已是"有效值"，
    因此 Web 与终端无需改动即可显示当前时段生效的阈值
  - Web 新增「分时段策略（白天 / 晚间）」面板：总开关 + 两个时段的起止时刻与策略，
    **改动立即生效无需保存/重启**；底部显示 `当前：白天（分时段已启用）　生效阈值 70 dB · 报警 关　|　距切换约 X 小时 Y 分`
  - 终端状态条显示 `[白天策略]` / `[白天（全局）]`
  - 新子命令 `python main.py schedule`（查看）与 `python main.py schedule <键> <值>`（设置，如 `schedule night_threshold_db 55`）
  （影响：volmon/schedule.py〔新〕, volmon/config.py, volmon/server.py, volmon/cli.py, main.py, web/index.html）
### Changed
- 服务端采集循环改为使用**分时段解析后的有效配置**：阈值/持续时长同步给 `AlertManager`、
  报警参数同步给 `AlarmManager`（仅在变化时同步并打印日志，避免每轮无谓更新）。
  副作用：阈值现在始终以配置（含分时段）为准，不再沿用构造时传入的值。
### 验证
- L0 通过；`pytest tests/ -q` ⇒ **47 passed**（新增 8 条：时间解析、跨零点窗口判定、
  白天/晚间时段判定、`schedule_enabled=false` 时与全局配置**完全一致**的回归保护、
  启用后按时段覆盖阈值与报警开关、空档/重叠提示、距下次切换、
  **L2 服务端按当前时段下发有效阈值并在时段切换后即时改变**）

## [2.7.0] - 2026-09-27
### Fixed
- **输入设备改了却不生效（选了 USB 耳机但拾音还是内置麦克风）**：
  根因是 `InputStream` 在**服务启动时**就打开了，之后只改 `device` 配置不会影响正在跑的流，
  用户必须重启服务才生效——而界面此前只在 README 里提了一句，页面上没有任何提示，很容易以为"切换失败"。
  现改为**热切换**：`AudioEngine.switch_device()`（停流→换设备→重启流；失败自动回滚到原设备并重启采集，
  避免采集停死），服务端 `MonitorServer._sync_input_device()` 在广播循环里检测配置变化并即时切换（约 100ms 内）。
  首次进入循环只记录不重启，避免开机时无谓重启一次采集流。
  （影响：volmon/audio.py, volmon/sim.py, volmon/server.py）
### Added
- **页面上显示"实际在用的设备"**（回答"到底在用哪个麦克风"）：
  `AudioEngine.active_device_info()` 读取采集流真正打开的设备（索引/名称/采样率），
  经 WS 的 `input_device` 字段下发；「实时音量」面板新增一行
  `采集设备：HECATE GM400（索引 0 · 44100Hz）`；若与所选设备不一致会**红字告警**。
- **报警输出设备当前值上屏**：「音量报警」面板显示 `报警输出：xxx（改选后立即生效，可点试听确认）`，
  WS 同步下发 `alarm_output_device`。
- 排障接口 `GET /api/status`：一次性返回 `input_device`（实际）/ `configured_input`（配置）/
  `alarm_output_device` / `version`。
### 验证
- L0 通过；`pytest tests/ -q` ⇒ **39 passed**（新增 3 条：切换失败回滚且重新采集、
  未启动时 `active_device_info`、**L2 改配置后引擎确实热切换 + `/api/status` + 广播带 `input_device`**）

## [2.6.0] - 2026-09-27
### Added
- **报警声输出设备可选**（3.5mm 耳机 / USB 耳机 / 本机扬声器）：
  新配置 `alarm_output_device`（空 = 系统默认输出），新接口 `GET /api/output-devices`
  枚举输出设备；Web「音量报警」面板新增输出设备下拉，**改动立即生效无需重启**——
  播放器在**每次发声时**才读取当前设备（`make_sounddevice_player(device_getter=...)`）。
  配合「🔊 试听报警音」即可确认声音到底从哪一路出来。
  （影响：volmon/config.py, volmon/alarm.py, volmon/server.py, main.py, web/index.html）
- **热插拔设备重扫描，无需重启服务**：`POST /api/devices/rescan` +
  `AudioEngine.rescan()`（停止采集 → `sd._terminate()` → `sd._initialize()` → 恢复采集）。
  **根因**：PortAudio 在进程内只在初始化时枚举一次音频设备，插入 USB 声卡后
  不重新初始化就永远看不到新设备（正是"插了 USB 耳机但列表里没有"的原因）。
  Web 上新增「🔄 刷新输入设备列表」「🔄 刷新输出设备列表」两个按钮。
  （影响：volmon/audio.py, volmon/sim.py, volmon/server.py, web/index.html）
- 设备下拉：配置里已存但**当前被拔掉**的设备仍保留一项并标注「（当前未连接）」，避免静默丢配置。
### 验证
- L0 通过；`pytest tests/ -q` ⇒ **36 passed**（新增 4 条：输出设备来自配置、
  `sd.play` 收到 `device=`（选中设备 / 未选为 None）、`set_player` 注入、
  `/api/output-devices` + `/api/devices/rescan` + `alarm_output_device` 可写）

## [2.5.1] - 2026-09-27
### Changed
- **版本号上屏显著化**（《开发规范》新增 §1.5「版本号必须上屏」，列为强制项）：
  此前版本徽章挤在副标题里、样式无底色，容易被忽略；且 WS 未连上时显示为 `--`。现改为：
  - 版本号**独立成标题栏徽章**，紧跟 `🔊 音量 / 噪音实时监控` 标题，蓝底蓝字（`.pill.ver`）
  - 同步写入**浏览器标签标题**（`音量监控 · 实时面板 v2.5.1`），切标签页也能看到版本
  - **兜底取数**：页面加载时先调 `/api/health` 取 `version` 并显示，WS 未连上/服务刚重启也能立刻看到版本
  - 仍严格遵循 §1.2 唯一版本源：`#ver` 初始值不硬编码，一律由后端 `__version__` 下发
  （影响：web/index.html）
### Added
- 《音量监控软件开发规范.md》§1.5「版本号必须上屏（强制）」：规定 Web 页面 / CLI 终端 /
  `/api/health` / WS 广播四处必须带版本，且前端禁止硬编码，并需有回归用例保护。
- 回归用例 `test_web_page_shows_version_badge`：断言页面含 `#ver` 与 `showVersion`、
  且不得在页面里写死具体版本号。
### 验证
- L0 通过；`pytest tests/ -q` ⇒ **32 passed**

## [2.5.0] - 2026-09-27
### Added
- **报警静音时段**（"布置完环境后人要离开，怕走动声触发报警"）：
  设定一段时间后，该时段内**超阈值也不播放报警声**；到期自动恢复发声，也可随时手动取消。
  与 2.1.0 的"报警自激静默"是两套机制，关键区别是**静音时段内监测与超量记录照常进行**
  （采样继续落库、超量告警照样写入 `data/alerts.csv`），只是不响——不会因此丢数据。
  - 新配置 `quiet_until_ms`（epoch 毫秒，0 = 未静音）；已加入可写配置集合，`恢复默认设置`会一并清除
  - 新接口 `GET /api/quiet`（查询剩余时间/截止时刻）、`POST /api/quiet`
    （`{"minutes":60}` 或 `{"until_ms":...}` 设置、`{"cancel":true}` 取消）
  - WebSocket 广播新增 `quiet_active` / `quiet_remaining_sec`，Web 与终端实时显示**倒计时**
  - Web「报警静音时段」面板：30 分钟 / 1 小时 / 2 小时快捷按钮 + 自定义分钟数 + 取消静音
  - 终端状态条显示 `[静音中 剩 45:12]`，并在图例下方注明"期间超阈值不发声（监测与超量记录照常）"
  - 新子命令：`python main.py quiet 60`（静音 60 分钟）、`python main.py quiet`（查状态）、
    `python main.py quiet --cancel`（取消）。服务运行时走 REST 即时生效，无需重启。
  （影响：volmon/config.py, volmon/alarm.py, volmon/server.py, volmon/cli.py, main.py, web/index.html）
### 验证
- L0 通过；`pytest tests/ -q` ⇒ **31 passed**（新增 7 条：静音拦截发声但 `is_suppressed()` 仍为 False、
  剩余秒数计算、默认不静音、**L2 静音时段采样照常落库且不出声→取消后恢复发声**、
  `/api/quiet` 增删改查 + 非法参数 400、广播带 `quiet_active`、终端倒计时与 `_fmt_dur` 格式化）

## [2.4.0] - 2026-09-27
### Added
- **终端（bash/CLI）波形改为与 Web 页面相同的坐标图呈现**（影响：volmon/cli.py，新增 tests/test_cli.py）
  此前 `python main.py cli`（即 `voicemonitor`）只画一条裸的波形带，没有坐标，难以读出"多大声音、什么时候"。
  现重写为**带完整坐标轴的实时字符图**，视觉结构与 Web 波形图对齐：
  - **纵轴**：左侧固定 7 字符宽标签区（如 `   80 |`），按音量 dB 自适应刻度，取整到 10
  - **横轴**：底部时间刻度，左=起点、中=中间、右=`现在`（格式 `HH:MM:SS`）
  - **阈值行**：整行用 `-` 画出并在行尾标注 `<=阈值70.0`，与实际阈值一致（行标签是离散刻度，故单独标注真实值）
  - **自适应 Y 范围**：与 Web 端 `yRange()` 同一套算法——含数据极值 **与阈值**，最小跨度 30dB，
    避免安静环境下波形被压成一条直线
  - **描点 + 竖直连线**：`*` 打点、`|` 补纵向连线，波形连续可读
  - **顶部状态条**：报警播放中(静默) / 冷却中(静默) / 超量告警! / 超阈值 / 正常，外加校准状态
  - **底部图例**：`纵轴=音量 dB (20~80)   横轴=时间   红色行=预警阈值 70.0 dB`
  - 行高按终端高度自适应（`terminal.lines - 12`），宽度按 `terminal.columns`；右侧预留 12 字符给阈值标注防止换行错位
  - **全部使用 ASCII 字符**，避免中文/全角字符宽度不一致导致终端错位
- **版本号上屏**：Web 顶部状态条新增「版本：v2.4.0」、终端状态条尾部显示 `v2.4.0`，
  `/api/health` 与 WebSocket 广播均带 `version` 字段。用于快速确认"改完代码到底有没有重启生效"
  （影响：volmon/server.py, web/index.html, volmon/cli.py）
### 验证
- L0 通过；`pytest tests/ -q` ⇒ **24 passed**（新增 5 条用例：终端图表 4 条 + 版本显示 1 条；
  另 `/api/health` 断言 `version == __version__`）
- 离线渲染确认布局无杂字符（`Text.plain` 输出干净）；实跑 `voicemonitor` 观察约 100 帧，
  波形随时间推进正常刷新，时间轴 `13:02:34 → 13:02:37 → 13:02:40 现在` 正确滚动
- 说明：后台捕获输出中偶见的单个 `m` 字符是 ANSI 颜色转义序列 `\x1b[1;36m` 被管道剥离后的残留，
  纯文本渲染无此现象，**非程序缺陷**

## [2.3.1] - 2026-09-27
### Fixed
- **未校准时音量显示为负数**：这不是计算错误，而是**单位错配**——麦克风算出的是 **dBFS**
  （满刻度为 0，真实声音恒为负，-60~-20），此前被直接当作 dB 显示，而用户读"dB"期待的是
  **dB SPL**（30~90）。现已按 `dB SPL ≈ dBFS + ref_spl_offset_db + calib_offset_db` 换算，并钳制为非负。
- 新增配置 `ref_spl_offset_db`（默认 **90.0**）：dBFS→dB SPL 参考偏移。
  典型映射：-46 dBFS→44 dB（安静房间）/ -30→60（正常交谈）/ -10→80（嘈杂），与常见声压级相符。
- 校准逻辑同步修正：`/api/raw_avg` 现返回 `base_avg`（= 原始 dBFS 均值 + 参考偏移，即界面显示的估算值），
  校准修正量 = 实测值 − base_avg，使"校准后读数 == 专业设备实测值"依旧成立（Web 与 CLI 均已更新）。
### Changed
- 界面「未校准」改为「**未校准(估算)**」，校准面板说明估算与校准后的区别
- `ref_spl_offset_db` 已加入可写配置集合，可按麦克风灵敏度微调
### 验证
- L0 通过；`pytest tests/ -q` ⇒ **19 passed**（新增回归用例：未校准不得出现负值、典型 dBFS→SPL 映射、校准后精确等于实测值）
- 实跑：原始 -64 dBFS → 显示 **约 25 dB**（非负）；`/api/raw_avg` 返回 `{"raw_avg":-64.21,"ref_offset_db":90.0,"base_avg":25.79,"displayed":25.79}`

## [2.3.0] - 2026-09-27
### Added
- **超量日志分页**：每页 100 条，**超出部分可翻页查看、不再丢弃**。
  新增 `/api/alerts/count`（总数）与 `/api/alerts?limit=&offset=` 分页参数；
  Web 面板加「← 上一页 / 下一页 →」与「第 X/Y 页 · 显示 a-b 条 / 共 N 条」
  （影响：volmon/server.py, web/index.html）
- **超量告警本地文件落盘**：每条告警**实时追加**写入 `data/alerts.csv`（UTF-8 BOM，Excel/文本工具可直接打开），
  不依赖数据库。启动时若文件不存在会自动用库内历史告警初始化。
  配置项 `alerts_csv_path`（默认 `data/alerts.csv`）（影响：volmon/storage.py, volmon/config.py, main.py）
- CLI 新增 `python main.py logs --export-alerts <路径>` 导出告警 CSV
### 验证
- L0 通过；`pytest tests/ -q` ⇒ **18 passed**（新增分页+CSV 用例：造 250 条，三页拼接 == 全量；CSV 含表头+250 行）
- 实跑：`/api/alerts/count` ⇒ 769 条；分页 offset=0/100/700 ⇒ 100/100/69，页间首条不重复；
  `data/alerts.csv` 已生成（表头+769 行，含历史）；用「重启服务」按钮重启后新代码生效

## [2.2.0] - 2026-09-27
### Added
- **「恢复默认设置」按钮** + `POST /api/config/reset` + `Config.reset()`：一键把阈值/校准/报警/设备等还原为出厂默认值（影响：volmon/config.py, volmon/server.py, web/index.html）
- **「重启服务」按钮** + `POST /api/service/restart` + 新模块 `volmon/restart.py`：
  派生脱离会话的接替进程 → 等原进程退出释放端口 → 按**原启动参数** exec 重启，原进程在回执后退出。换设备/改代码后无需手工杀进程（影响：volmon/restart.py, volmon/server.py, web/index.html）
- 波形/趋势图 **Y 轴自适应**：按数据（并含阈值）取整、最小跨度 30dB，未校准时负值不再被钳在 30 处显示为一条直线
### Fixed
- **恢复音量采集**：定位到根因并非权限，而是 `device=default` 指向了 PortAudio 默认输入**设备 0**（当时是已断开的 iPhone continuity 麦克风）持续吐静音。
  实测各设备：`MacBook Pro麦克风` -82.7~-47.6 dBFS ✓、`AU05` -65.9~-62.5 ✓、`OrayVirtualAudioDevice` 静音 ✗。已将设备切到 `MacBook Pro麦克风` 并重启，读数恢复真实波动。
- **修复 WebSocket 广播序列化崩溃**：真实音频下 `np.log10` 产出 `np.float64`，使 `over = db > threshold` 变成 `np.bool_`，
  `json.dumps` 抛 "Object of type bool is not JSON serializable"，导致**所有实时广播失败**（此前静音时 RMS=0 走 DBFS_FLOOR 分支为 Python float，掩盖了该缺陷）。
  修复：`audio._callback` 强转 `float`，server 侧 `raw/db` 转 float、`over` 转 bool。（影响：volmon/audio.py, volmon/server.py）
- 顺带复位在静音状态下算出的无效校准偏移（144 → 0）。
### 验证
- L0 通过；`pytest tests/ -q` ⇒ **17 passed**（新增 numpy 类型回归用例：回调产出必须是 Python float/bool 且可 json 序列化）
- 实跑：WS 读数 `db -47.8 ~ -44.9` 真实波动、`mic_silent: False`；重启接口实测通过（旧进程退出→接替进程接管→health OK→采样继续）

## [2.1.2] - 2026-09-27
### Added
- **麦克风静音自检告警**：`AudioEngine.is_silent()` 判定读数是否持续为数字静音（DBFS_FLOOR=-100），
  经 `mic_silent` 字段广播到 Web 与 CLI，页面/终端红色提示"疑似未授权麦克风权限 / 输入被静音 / 设备选错"。
  解决"波形是一条直线、被误判为采集停止"却无任何提示的问题（影响：volmon/audio.py, volmon/sim.py, volmon/server.py, web/index.html, volmon/cli.py）
### Fixed
- 排查用户"感觉音量采集停止"：**采集并未停止**（服务存活、采样每 1s 正常落库），
  实际是麦克风持续输出数字静音（2s 内 43 个回调样本全部恰为 -100.0，流无 status 告警），
  根因为该进程未获得 macOS 麦克风授权（流可打开但被静音成全零）。
### 验证
- L0 通过；`pytest tests/ -q` ⇒ **16 passed**（新增静音自检用例：无数据/全地板→True，有真实读数→False）
- 实跑：WS 消息 `raw_db: -100.0 | db: 44.0 | mic_silent: True`，Web/CLI 均会显示该告警

## [2.1.1] - 2026-09-27
### Fixed
- **排查"点开报警音没有声音"**：根因是端口 8000 被**旧会话遗留的服务进程**占用（`serve --fake`，旧代码且 `--fake` 不创建播放器），新代码从未生效。已清理，并用新代码重启验证。
- `main.py` 参数回退缺陷（会导致 `python main.py serve`/`cli`/`calibrate` 崩溃）：`main()` 中 `args.host` 无条件访问 → 改用 `getattr`；`cmd_serve`/`cmd_cli`/`cmd_calibrate` 缺省参数时回退到配置里的 `host`/`port`（此前 `python main.py serve` 会因 `%d % None` 直接崩溃、`cli` 报 AttributeError）。
- `cli` 子命令补齐 `--host/--port` 可选参数。
### Added
- **报警试听**：`AlarmManager.test_play(seconds)` + `GET/POST /api/alarm/test`，可独立验证扬声器/播放器是否可用（只发声，不进入静默窗口）。Web 报警面板新增「🔊 试听报警音」按钮。
- Web 报警设置（开关/时长/冷却）改为**改动即生效**（onchange 自动 POST），此前需点另一个面板的「保存设置」才会写入，容易造成"勾了没反应"。
- 报警状态行增加触发条件提示（当前 dB vs 阈值、需持续 N 秒），避免误以为"开启了就该响"。
- **`voicemonitor` 命令行入口**：安装到 `~/bin/voicemonitor`，bash 下直接输入 `voicemonitor` 启动终端实时波形（默认 cli），也支持 `voicemonitor serve/web/cli/logs ...`。
### 验证
- `pytest tests/ -q` ⇒ **15 passed**（新增试听用例：只发声不静默、无播放器时返回 False）
- 实跑：`python main.py serve` 无参数正常绑定配置端口 8000（修复前会崩溃）；`/api/alarm/test` 返回 `{"ok": true}` 并在 MacBook Pro 扬声器实际播放约 2 秒报警音；`voicemonitor` 成功连接 `ws://127.0.0.1:8000/ws`

## [2.1.0] - 2026-09-27
### Added
- **音量报警（电脑发声）**：当监测到音量持续超过阈值，由本机扬声器播放"嘀—嘀—"报警声（默认持续 10s），提示现场人员。新增模块 `volmon/alarm.py`（`AlarmManager` 状态机 + `make_sounddevice_player` 用 sounddevice 播放），播放在独立守护线程，不阻塞采集广播。
- 配置项 `alarm_enabled`（默认关）/ `alarm_duration_sec`（默认 10s）/ `alarm_cooldown_sec`（默认 3s），Web 设置面板与 REST/WS 均支持读写。
- Web 实时面板新增「音量报警」设置块与状态行（🔊 播放中静默 / ⏸ 冷却中 / ● 正常）；CLI 终端同步显示报警相位。
### Changed
- **报警自激防护**：报警声会被同一台电脑麦克风拾回，故在「播放中 + 冷却期（默认共 13s）」内噪声监测**整体静默**——不写入采样、不触发/不延长噪声超量告警；冷却结束后才恢复检测（用户明确需求）。WS 消息新增 `alarm_phase`(idle/playing/cooldown) 与 `suppressed` 字段；静默期内隐藏噪声横幅。
### 验证
- L0 语法通过；pytest 全量 **13 passed**（新增 `tests/test_alarm.py`：L1 状态机 + L2 服务端静默抑制——报警/冷却期间 `add_sample` 调用次数为 0，恢复后 >0）
- `serve --fake` 全链路可用；真实模式在 macOS 下 `sounddevice` 输出报警声（沙箱无音频设备仅记警告不崩溃）

## [2.0.3] - 2026-09-27
### Added
- Web 实时波形图新增 **Y 轴（音量 dB 标签：30/40/50/60/70/80/90/100）与 X 轴（时间标签：-60s … 现在）**，并标注"音量 dB"轴标题；波形改为**右对齐滚动**（最新点固定在最右），更符合真实观测习惯
- Web 历史趋势图新增 **Y 轴（音量 dB 标签）与 X 轴（起/中/止真实时刻标签）**，同样标注"音量 dB"轴标题
### Changed
- 模拟引擎 `volmon/sim.py`（`--fake` 演示/测试模式）由"单一正弦"改为**真实噪声模型**：缓慢漂移 + 非整周期中频起伏 + 高频随机抖动 + 偶发突发（指数衰减包络）。Web/CLI 波形不再规律重复，肉眼接近真实环境噪声
- **真实模式（`serve` 无 `--fake`）直接使用麦克风实时 dB**，本就不规则，与事实一致
- 超量告警日志改为**仅展示最近 100 条**（前端 `slice(0,100)`），超出部分自动隐藏，并标注"显示最近 100 条（共 N 条）"
### 验证
- L0/L1/L2 全绿（`pytest tests/ -q` ⇒ 8 passed）
- macOS（darwin）真实模式：`sounddevice` 枚举到 `MacBook Pro麦克风` 等输入设备，`AudioEngine.start()/stop()` 正常（采集返回 -100 静音地板仅为沙箱无麦克风输入/授权所致，非代码问题）
- `serve --fake` 全链路验证：`/api/health`、`/api/samples`(字段 `t/raw_db/db`)、`/api/alerts` 均正常，Web 页面成功渲染带坐标轴的新波形

## [2.0.2] - 2026-09-27
### Changed
- Web 预警横幅改为**固定在页面底部浮层**（`position: fixed; bottom: 0`），并为 body 增加底边距；预警出现/消失不再改变页面整体布局
### Added
- 一键启动脚本 `start.sh`（macOS/Linux/沙箱）与 `start.bat`（Windows）：自动建 venv、装依赖、启动服务
### Docs
- README 明确运行必须带子命令 `serve`，并提供 start 脚本用法
### 验证
- L0/L1/L2 仍全绿；Web 页面仍正常返回（横幅仅 CSS 位置变化）

## [2.0.1] - 2026-09-27
### Added
- 模拟引擎 `volmon/sim.py`（SimEngine）+ `serve --fake`：无麦克风即可演示 / 自动化测试全栈（PRD/规范要求的"无设备也能验证"）
### Fixed
- 修复 `server.handle_index` 在 aiohttp 3.14 下 `content_type` 含 charset 导致 `/` 返回 500 的问题
- 为 `SimEngine` 补齐 `start()/stop()` 接口，避免 `serve --fake` 启动崩溃
- 修复 `serve --fake` 未隔离音频依赖导入，确保演示模式不依赖 sounddevice
### Changed
- 依赖说明：aiohttp 运行时还需 `aiohappyeyeballs`（requirements.txt 已含完整依赖链）
### 验证
- L0/L1/L2 全绿（pytest 8 passed，含 `serve --fake` 真实子进程部署测试：Web+WS+REST）
- L3 真实麦克风：本沙箱无声卡输入（返回 -100 静音地板），但设备枚举/流打开/广播/WS 全链路正常；需在 Windows 授权麦克风后实测

## [2.0.0] - 2026-09-27
### Added
- 后端服务架构（aiohttp：WebSocket 实时广播 + REST + 托管 Web 页面）
- Web 实时面板：滚动波形（带阈值线）、历史趋势图（1h/24h/7d）、超量红/黄横幅、校准面板、预警设置、超量告警日志列表（影响文件：web/index.html, volmon/server.py）
- 命令行实时波形客户端（websockets + rich，终端滚动波形 + 阈值线 + 超量横幅）（影响文件：volmon/cli.py, main.py）
- 专业设备噪音校准：实测值 → 软件原始均值 → 偏移对齐（影响文件：volmon/calibrate.py, web/index.html, volmon/server.py）
- 阈值 + 持续时长超量监控：超量片段落 SQLite 告警表 + 文本日志（影响文件：volmon/alerting.py, volmon/storage.py）
- SQLite 采样/告警存储 + CSV 导出（影响文件：volmon/storage.py, main.py）
- 配置持久化与子命令入口（serve/web/cli/calibrate/config/logs）（影响文件：volmon/config.py, main.py）
### Changed
- 技术架构由 v1「桌面原生(PySide6)」改为 v2「后端 + Web + CLI」三层（见 PRD v2 变更说明）
### Fixed
- 无（首版交付）
### 验证
- L0 语法校验（`py_compile` 全量）本地通过
- L1 核心逻辑（config/storage/alerting）pytest 本地通过（4 passed）
- L2 集成测试已补充 `tests/test_server.py`（REST + WebSocket 广播 + 校准偏移计算）；aiohttp 装入后本地可跑，本沙箱因内存限制未能安装 aiohttp，需在 Windows 实跑
- L3 真机（Windows+麦克风）待用户环境实测
### 遗留
- 真机麦克风/Web 未在本沙箱验证（无音频设备）
- 打包成 exe / 系统托盘后台常驻（M5）待做
