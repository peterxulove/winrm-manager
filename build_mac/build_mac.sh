#!/bin/bash
# ============================================================
# WinRM Manager — macOS 自包含 .app 构建脚本
#
# 产物：dist/WinRM-Manager-macOS.zip
#      解压得到 WinRM Manager.app，双击即用，无需 pip 安装。
#
# 依赖内置在 .app 内：服务端代码 + Python 包（含 cffi 各版本）。
# 内置依赖兼容 Python 3.9–3.13，因此 macOS 自带的 /usr/bin/python3 也能直接跑。
#
# 用法：./build_mac.sh
# ============================================================
set -euo pipefail

VERSION="1.0.4"
APP_NAME="WinRM Manager"
ROOT="$(cd "$(dirname "$0")" && pwd)"
SRC_DIR="$(cd "$ROOT/.." && pwd)"
BUILD="$ROOT"

APP="$BUILD/$APP_NAME.app"
RES="$APP/Contents/Resources"
APPCODE="$RES/app"

echo "==> 构建 ${APP_NAME} ${VERSION} (macOS)"

# ---------- 1. 清理 ----------
rm -rf "$APP" "$BUILD/wheels" "$BUILD/dist"
mkdir -p "$APP/Contents/MacOS" "$APPCODE/vendor" "$BUILD/wheels/common" \
         "$BUILD/wheels/cffi" "$BUILD/dist"

# ---------- 2. 下载依赖（按 Python 3.9 解析，保证向下兼容）----------
# 关键：不能用最新版 —— urllib3 2.8 用了 `bytes | str` 语法，Python 3.9 直接语法错误。
echo "==> 下载依赖 wheels（兼容 Python 3.9）"
cd "$BUILD/wheels/common"
python3 -m pip download --quiet --no-deps --only-binary=:all: \
  --platform macosx_11_0_arm64 --platform macosx_10_12_universal2 --platform macosx_10_9_universal2 \
  --python-version 3.9 --implementation cp --abi cp39 --abi abi3 --abi none \
  pywinrm requests-ntlm requests urllib3 certifi idna charset-normalizer \
  xmltodict pycparser cryptography pyspnego --dest .

# charset_normalizer 需要纯 Python 版（上面按 cp39 解析可能拿到平台特定 wheel）
rm -f charset_normalizer-*cp39*.whl
python3 -m pip download --quiet --no-deps --only-binary=:all: --platform any \
  --python-version 3.9 --implementation py --abi none charset-normalizer --dest .

echo "==> 下载 cffi（cp39–cp313）"
cd "$BUILD/wheels/cffi"
for v in 3.9 3.10 3.11 3.12 3.13; do
  abi="cp${v/./}"
  python3 -m pip download --quiet --no-deps --only-binary=:all: \
    --platform macosx_11_0_arm64 --python-version "$v" --implementation cp --abi "$abi" \
    "cffi==1.17.1" --dest .
done

# ---------- 3. 组装 app bundle ----------
echo "==> 组装 .app"
cp "$SRC_DIR/server.py" "$APPCODE/server.py"
cp -R "$SRC_DIR/static" "$APPCODE/static"

for w in "$BUILD"/wheels/common/*.whl; do
  unzip -qo "$w" -d "$APPCODE/vendor/common"
done
for v in 39 310 311 312 313; do
  w=$(ls "$BUILD"/wheels/cffi/cffi-*-cp${v}-*.whl 2>/dev/null | head -1 || true)
  if [ -n "$w" ]; then
    mkdir -p "$APPCODE/vendor/cp${v}"
    unzip -qo "$w" -d "$APPCODE/vendor/cp${v}"
  fi
done
find "$APPCODE/vendor" -type d -name "*.dist-info" -exec rm -rf {} + 2>/dev/null || true
find "$APPCODE/vendor" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

cp "$BUILD/assets/launcher.sh" "$APP/Contents/MacOS/launcher"
chmod 755 "$APP/Contents/MacOS/launcher"
cp "$BUILD/assets/AppIcon.icns" "$RES/AppIcon.icns"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>WinRM Manager</string>
    <key>CFBundleDisplayName</key><string>WinRM Manager</string>
    <key>CFBundleIdentifier</key><string>local.winrm.manager</string>
    <key>CFBundleVersion</key><string>VERSION_PLACEHOLDER</string>
    <key>CFBundleShortVersionString</key><string>VERSION_PLACEHOLDER</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleExecutable</key><string>launcher</string>
    <key>CFBundleIconFile</key><string>AppIcon</string>
    <key>LSMinimumSystemVersion</key><string>11.0</string>
    <key>NSHighResolutionCapable</key><true/>
    <key>LSUIElement</key><true/>
</dict>
</plist>
PLIST
sed -i.bak "s/VERSION_PLACEHOLDER/${VERSION}/g" "$APP/Contents/Info.plist"
rm -f "$APP/Contents/Info.plist.bak"

# ---------- 4. ad-hoc 签名 ----------
echo "==> ad-hoc 签名"
codesign --force --deep --sign - "$APP" 2>/dev/null || echo "  ! 签名失败，用户需右键打开"

# ---------- 5. 打包 ----------
echo "==> 打包 zip"
OUT="$BUILD/dist/WinRM-Manager-macOS.zip"
ditto -c -k --sequesterRsrc --keepParent "$APP" "$OUT"

# ---------- 6. 自检 ----------
echo "==> 自检"
FAIL=0
[ -f "$APP/Contents/MacOS/launcher" ] || { echo "  ✗ 缺少 launcher"; FAIL=1; }
[ -f "$APPCODE/server.py" ] || { echo "  ✗ 缺少 server.py"; FAIL=1; }
[ -f "$APPCODE/static/index.html" ] || { echo "  ✗ 缺少前端"; FAIL=1; }
for v in 39 311 313; do
  [ -d "$APPCODE/vendor/cp${v}" ] || { echo "  ✗ 缺少 vendor/cp${v}"; FAIL=1; }
done

# 逐版本验证依赖可加载
for PY in /usr/bin/python3 /opt/homebrew/bin/python3 "$(command -v python3)"; do
  [ -x "$PY" ] || continue
  ver=$("$PY" -c 'import sys;print("%d%d"%sys.version_info[:2])' 2>/dev/null) || continue
  P="$APPCODE/vendor/common"
  [ -d "$APPCODE/vendor/cp$ver" ] && P="$P:$APPCODE/vendor/cp$ver"
  if PYTHONPATH="$P" "$PY" -c "import winrm, requests_ntlm, cryptography" 2>/dev/null; then
    echo "  ✓ Python $("$PY" -V 2>&1 | cut -d' ' -f2) 依赖可加载"
  else
    echo "  ✗ Python $("$PY" -V 2>&1 | cut -d' ' -f2) 依赖加载失败"
    FAIL=1
  fi
done

if [ "$FAIL" -ne 0 ]; then
  echo "==> 自检失败"
  exit 1
fi

echo
echo "==> 完成: $OUT"
ls -lh "$OUT"
