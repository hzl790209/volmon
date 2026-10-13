@echo off
chcp 65001 >nul
cd /d "%~dp0"
REM 准备 venv（没有就建并装依赖）
if not exist venv\Scripts\python.exe (
  echo 创建虚拟环境并安装依赖...
  python -m venv venv
  call venv\Scripts\activate
  python -m pip install -r requirements.txt
  call venv\Scripts\deactivate
)
set "PY=venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" -m pip install --upgrade pyinstaller
"%PY%" -m PyInstaller volmon-win.spec --noconfirm
echo.
echo 打包完成：dist\volmon-win\volmon-win.exe
echo 首次运行会请求"麦克风"权限，请在 Windows 隐私设置里允许。
pause
