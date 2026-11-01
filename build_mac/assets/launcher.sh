#!/bin/bash
# WinRM Manager 启动器（macOS）
#
# 应用是自包含的：服务端代码与 Python 依赖都在 Contents/Resources/app 下，
# 解压后可直接双击运行，无需额外安装。

HERE="$(cd "$(dirname "$0")" && pwd)"          # .../Contents/MacOS
APP_DIR="$HERE/../Resources/app"
PORT=5178
URL="http://127.0.0.1:${PORT}"
LOG_DIR="$HOME/.winrm-manager"
LOG="$LOG_DIR/server.log"
mkdir -p "$LOG_DIR"

notify() {
  osascript -e "display dialog \"$1\" buttons {\"好\"} default button 1 \
with title \"WinRM Manager\" with icon caution" >/dev/null 2>&1
}

is_up() {
  curl -s --noproxy '*' -m 2 -o /dev/null "${URL}/api/health" 2>/dev/null
}

# 服务已在运行：直接打开界面
if is_up; then
  open "$URL"
  exit 0
fi

# ---- 找 Python 解释器 ----
# 优先选「vendor 里有对应 cpXX 目录」的解释器，避免版本不匹配导致依赖加载失败
VENDOR="$APP_DIR/vendor"
PY=""
FALLBACK=""
for c in "$(command -v python3 2>/dev/null)" \
         /opt/homebrew/bin/python3 \
         /usr/local/bin/python3 \
         /usr/bin/python3
do
  [ -n "$c" ] && [ -x "$c" ] || continue
  ver="$("$c" -c 'import sys;print("%d%d" % sys.version_info[:2])' 2>/dev/null)"
  [ -z "$ver" ] && continue
  [ -z "$FALLBACK" ] && FALLBACK="$c"
  if [ -d "${VENDOR}/cp${ver}" ]; then
    PY="$c"
    break
  fi
done
[ -z "$PY" ] && PY="$FALLBACK"

if [ -z "$PY" ]; then
  notify "未找到 python3。\n\n请安装 Python 3.9 或更高版本后重试：\nhttps://www.python.org/downloads/"
  exit 1
fi

# ---- 挂载内置依赖 ----
PYVER="$("$PY" -c 'import sys;print("%d%d" % sys.version_info[:2])' 2>/dev/null)"
if [ -d "${VENDOR}/cp${PYVER}" ]; then
  export PYTHONPATH="${VENDOR}/common:${VENDOR}/cp${PYVER}"
elif [ -d "${VENDOR}/common" ]; then
  export PYTHONPATH="${VENDOR}/common"
fi

# ---- 依赖自检 ----
if ! "$PY" -c "import winrm" >/dev/null 2>&1; then
  notify "内置依赖无法在当前 Python（$("$PY" -V 2>&1)）下加载。\n\n本应用内置支持 Python 3.9–3.13。\n你可以装一个受支持的版本，或在终端执行：\n\npip3 install pywinrm requests-ntlm\n\n然后重新打开本应用。"
  exit 1
fi

cd "$APP_DIR" || { notify "应用文件不完整：找不到 ${APP_DIR}"; exit 1; }

# 内网 WinRM 必须直连，清掉代理变量
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
export NO_PROXY="*"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONUNBUFFERED=1

nohup "$PY" server.py --no-browser --port "$PORT" >> "$LOG" 2>&1 &
echo "--- $(date '+%Y-%m-%d %H:%M:%S') 启动 PID $! ---" >> "$LOG"

for _ in $(seq 1 60); do
  if is_up; then
    open "$URL"
    exit 0
  fi
  sleep 0.3
done

notify "服务启动超时。\n\n日志：${LOG}"
exit 1
