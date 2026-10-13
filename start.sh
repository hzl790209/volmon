#!/usr/bin/env bash
# 一键启动（macOS / Linux / 本沙箱）：自动建 venv、装依赖、启动服务
set -e
cd "$(dirname "$0")"
if [ ! -x venv/bin/python ]; then
  echo "创建虚拟环境并安装依赖..."
  python3 -m venv venv
  venv/bin/pip install -r requirements.txt
fi
echo "启动服务：浏览器打开 http://127.0.0.1:8000"
exec venv/bin/python main.py serve
