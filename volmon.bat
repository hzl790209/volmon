@echo off
chcp 65001 >nul
REM ============================================================
REM volmon (Windows) —— 音量监控软件一键启动入口
REM 对应 macOS 的 ~/bin/volmon；实际逻辑在 volmon/launcher.py（跨平台）
REM
REM   volmon               一键：后台拉起服务 + 打开实时面板(浏览器)
REM   volmon serve         前台启动后端服务（Ctrl-C 停止）
REM   volmon start         只后台拉起服务（不打开面板）
REM   volmon web           浏览器打开实时面板
REM   volmon cli           终端实时波形（服务会自动起）
REM   volmon status        看服务是否在跑（版本/采集设备/报警输出）
REM   volmon stop          停止后台服务
REM   volmon restart       重启后台服务
REM   volmon <其它>        原样传给 main.py（quiet/policy/clear/config…）
REM ============================================================
set "PROJ=%~dp0"
set "PY=%PROJ%venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
REM 避免代理拦截本地回环
if defined NO_PROXY (set "NO_PROXY=%NO_PROXY%;127.0.0.1;localhost") else (set "NO_PROXY=127.0.0.1;localhost")
if defined no_proxy (set "no_proxy=%no_proxy%;127.0.0.1;localhost") else (set "no_proxy=127.0.0.1;localhost")
cd /d "%PROJ%"
"%PY%" -m volmon.launcher %*
