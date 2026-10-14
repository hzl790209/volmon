@echo off
rem Volmon 命令行实时监控入口（由安装包开始菜单「Volmon 命令行监控」调用）
rem 说明：volmon-win.exe 是无控制台窗口程序，直接双击不会开窗口；
rem       本脚本由 cmd.exe 解释执行，会自动打开一个终端窗口并进入实时波形界面。
chcp 65001 >nul 2>&1
cd /d "%~dp0"
"%~dp0volmon-win.exe" cli %*
set _rc=%errorlevel%
if not "%_rc%"=="0" (
    echo.
    echo [volmon] 命令已退出（代码 %_rc%）。按任意键关闭窗口…
    pause >nul
)
