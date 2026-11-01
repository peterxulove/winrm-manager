#!/bin/bash
# 终端模式启动：前台运行，Ctrl+C 停止。适合排查问题时使用。
cd "$(dirname "$0")" || exit 1

PY=""
for c in \
  "/Users/peter/.workbuddy-ai/binaries/python/versions/3.13.12/bin/python3" \
  "$(command -v python3)" \
  "/opt/homebrew/bin/python3" \
  "/usr/local/bin/python3" \
  "/usr/bin/python3"
do
  if [ -n "$c" ] && [ -x "$c" ] && "$c" -c "import winrm, flask" >/dev/null 2>&1; then
    PY="$c"; break
  fi
done

if [ -z "$PY" ]; then
  echo "未找到装有 pywinrm / flask 的 Python 3。"
  echo "请执行：pip3 install pywinrm requests-ntlm flask"
  read -r -p "按回车退出…" _
  exit 1
fi

unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
export NO_PROXY="*"

exec "$PY" server.py --port 5178
