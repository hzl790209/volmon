#!/usr/bin/env bash
# 把 volmon-mac 加到终端 PATH：之后可直接 `volmon-mac cli` / `volmon-mac serve`
# 用法：在终端里执行  bash /路径/install-cli.sh
set -e
SRC="/Applications/volmon-mac.app/Contents/MacOS/volmon-mac"
DST="/usr/local/bin/volmon-mac"

if [ ! -x "$SRC" ]; then
  echo "✖ 找不到 $SRC"
  echo "  请先确认已把 volmon-mac.app 拖进「应用程序」。"
  exit 1
fi

mkdir -p /usr/local/bin
if [ -w /usr/local/bin ]; then
  ln -sf "$SRC" "$DST"
else
  echo "需要管理员权限创建 $DST …"
  sudo ln -sf "$SRC" "$DST"
fi

if [ -x "$DST" ]; then
  echo "✔ 已创建快捷命令：$DST"
  echo
  echo "   现在可以直接在终端输入："
  echo "     volmon-mac cli      # 终端实时音量波形（需后端已运行）"
  echo "     volmon-mac serve    # 仅起后端服务（不弹浏览器）"
  echo
  echo "   提示：先打开 volmon-mac.app 让后端跑起来，再开新终端窗口执行 volmon-mac cli"
else
  echo "✖ 创建失败，请检查 /usr/local/bin 写入权限"
  exit 1
fi
