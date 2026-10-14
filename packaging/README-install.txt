Volmon 音量监控 · MSI 安装包  v1.0.9
=====================================

用途：双击安装即可使用，无需在目标电脑部署 Python 环境。

安装步骤：
1. 双击 volmon-win-x64.msi（右键「以管理员身份运行」更稳；UAC 点「是」）。
   本版本支持**原地升级**：若已装过旧版，直接装这个即可，不必先卸载。
2. 选择安装目录（默认 C:\Program Files\Volmon\volmon-win）。
3. 安装结束勾选「启动 Volmon」，会立即打开监控面板。

开始菜单（Volmon）有两个入口：
- 「Volmon 音量监控」    → 打开浏览器监控面板
- 「Volmon 命令行监控」  → 打开终端窗口，实时波形界面（Ctrl+C 退出）

命令行用法（安装目录默认 C:\Program Files\Volmon\volmon-win）：
  cd /d "C:\Program Files\Volmon\volmon-win"

  volmon-cli.exe cli                 # 终端实时波形 + 下方历史趋势
  volmon-cli.exe cli --fps 4         # 若仍觉闪烁：把整屏重绘降到 4 次/秒
  volmon-cli.exe cli --no-screen     # 极端情况下不用备用屏渲染
  volmon-cli.exe cli --no-trend      # 只要实时波形（不要历史趋势）
  volmon-cli.exe cli --trend-hours 6 # 历史趋势区间改为 6 小时
  volmon-cli.exe trend --hours 24    # 单独看历史趋势图（直读数据库，无需服务）
  volmon-cli.exe status              # 服务/设备/校准状态
  volmon-cli.exe logs --hours 24     # 近 24 小时告警日志（--export x.csv 可导出）
  volmon-cli.exe stop                # 停止后台服务
  volmon-cli.exe --help              # 全部子命令

  注：volmon-win.exe 是桌面版（无控制台窗口），命令行请用 volmon-cli.exe。

本版修复（1.0.9）——命令行历史趋势区频闪：
- 整屏重绘从「固定 15 次/秒」改为「只在画面变化时重绘，且不超过 6 次/秒」，
  空闲时完全静止（原来每秒无条件重绘 15 次，传统控制台 conhost 下整屏闪烁）；
- 历史趋势区块高度恒定（原来拉到数据 / 拉不到数据时高度在 2 行与 14 行之间跳变）；
- 历史数据改为异步拉取 + 严格 60 秒节流（原来接口失败会每帧重拉，
  且每 60 秒会阻塞画面最多 5 秒）；
- 纵向布局按终端高度精确预算、逐行按显示宽度裁剪，杜绝折行/溢出导致的画面跳动。
- 新增 `--fps`（重绘上限）与 `--no-screen` 两个可调项；
- 修复 `volmon-cli.exe trend`（入口白名单漏项，报 unrecognized arguments）；
- 安装包改为原地升级（同一 UpgradeCode、新 ProductCode），无需先卸载旧版。

历史修订：
- 1.0.8: 修复首装 FileNotFoundError（%APPDATA% 下 data 目录自动创建）；
         修复打包缺 aiohttp（改用项目 venv 构建）；
         修复 cli 自我递归 spawn；新增控制台版 volmon-cli.exe 与命令行监控入口。

卸载：控制面板 → 程序和功能 → Volmon 音量监控（会一并清理计划任务）。
