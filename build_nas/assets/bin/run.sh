#!/bin/bash
# WinRM Manager 启动包装脚本
# 负责：挑选 Python 解释器 → 组装内置依赖的 PYTHONPATH → exec 服务进程

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# 找 python3：优先 PATH，其次常见绝对路径
PY="$(command -v python3 2>/dev/null)"
if [ -z "$PY" ]; then
  for c in /usr/bin/python3 /usr/local/bin/python3 /opt/bin/python3; do
    [ -x "$c" ] && PY="$c" && break
  done
fi
if [ -z "$PY" ]; then
  echo "ERROR: 未找到 python3，无法启动 WinRM Manager" >&2
  exit 1
fi

# cffi 是 C 扩展，必须与 Python 主次版本严格匹配
PYVER="$("$PY" -c 'import sys;print("%d%d" % sys.version_info[:2])' 2>/dev/null)"
CFI_DIR=""
if [ -n "$PYVER" ] && [ -d "${APP_DIR}/vendor/cp${PYVER}" ]; then
  CFI_DIR="${APP_DIR}/vendor/cp${PYVER}"
else
  echo "WARN: 未找到适配 Python ${PYVER} 的 cffi 运行时，NTLM 认证可能不可用" >&2
fi

export PYTHONPATH="${APP_DIR}/vendor/common${CFI_DIR:+:${CFI_DIR}}"
export PYTHONUNBUFFERED=1
export PYTHONDONTWRITEBYTECODE=1
export NO_PROXY="*"

WINRM_HOST="${WINRM_HOST:-0.0.0.0}"
WINRM_PORT="${WINRM_PORT:-5178}"

exec "$PY" "${APP_DIR}/server.py" \
  --host "$WINRM_HOST" \
  --port "$WINRM_PORT" \
  --no-browser
