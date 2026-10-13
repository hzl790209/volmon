@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist venv\Scripts\python.exe (
    echo 创建虚拟环境并安装依赖...
    python -m venv venv
    call venv\Scripts\activate
    pip install -r requirements.txt
) else (
    call venv\Scripts\activate
)
echo 启动服务：浏览器打开 http://127.0.0.1:8000
python main.py serve
pause
