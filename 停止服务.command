#!/bin/bash
# 停止后台运行的 WinRM Manager 服务
if pgrep -f "server.py --no-browser" >/dev/null 2>&1; then
  pkill -f "server.py --no-browser"
  echo "已停止 WinRM Manager 服务。"
else
  echo "服务未在运行。"
fi
sleep 1
