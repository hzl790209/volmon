# 参与贡献（Contributing）

感谢你关注 **volmon**！本项目欢迎 Issue、功能建议与 Pull Request。下面是参与开发的简明指南。

## 行为准则

请保持友善、就事论事。我们保留对不友善或偏离主题的发言进行管理的权利。

## 提 Issue

- **Bug 报告**：尽量包含复现步骤、操作系统、软件版本、日志片段（`data/volmon.log` 或终端输出）。
- **功能建议**：说明使用场景与期望行为，便于评估必要性。
- **安全漏洞**：**不要**在公开 Issue 中披露，请走 [SECURITY.md](./SECURITY.md) 的私报告知渠道。

## 开发环境

volmon 的桌面端（macOS / Windows）共用同一套 Python 后端，仅需 Python 3.9+：

```bash
python -m venv venv
# Windows: venv\Scripts\activate
source venv/bin/activate        # macOS / Linux
pip install -r requirements.txt

# 启动后端 + 打开 Web 面板
./start.sh                     # macOS / Linux
# start.bat                    # Windows

# 或无麦克风演示模式（合成音量，验证全栈无需真实声卡）
python main.py serve --fake
```

- 入口子命令：`serve / web / cli / calibrate / config / quiet / schedule / policy / clear / logs / mail`
- 测试：`pytest tests/ -q`
- 改动代码后需重启服务（`↳ 重启服务` 或 `python main.py serve` 重新拉起）才会生效。

## 代码结构与规范

- 后端：`volmon/`（采集 `audioengine`、存储 `storage`、报警 `alertmanager`、服务 `server`、客户端 `launcher`）
- 前端：`web/index.html`（单体页面，WebSocket 实时推送）
- 平台差异已抽象：`volmon/paths.py`（数据目录）、`volmon/__init__.py`（产品名/版本）、`macapp.py` / `winapp.py`（打包入口）
- 代码风格：PEP 8；新增会持续追加的日志必须复用 `volmon/logrot.py`
- 打包后 `.app` / `.exe` 内部**只读**：写数据走 `volmon/paths.py`，**禁止**用 `__file__` 拼路径

详见仓库内的 [音量监控软件开发规范.md](./音量监控软件开发规范.md)（中文开发规范）。

## 提交 PR

1. Fork 本仓库并基于 `main` 创建特性分支。
2. 保持提交原子化、信息清晰（如 `feat: 增加 XX`、`fix: 修复 XX`）。
3. 确保本地测试通过（`pytest`）。
4. 在 PR 中说明改动目的与验证方式。

## 版本与发布

- 桌面端版本号唯一来源：`volmon/__init__.py` 的 `__version__`。
- 当前打包发布版本为 **volmon-mac 1.0.3**（macOS）；Windows 与 Android 为开发预览，欢迎共建。
- 发布产物（`.dmg` / `.exe` / `.apk`）通过 GitHub Releases 提供，不纳入仓库源码。
