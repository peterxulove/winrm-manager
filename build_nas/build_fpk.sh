#!/bin/bash
# ============================================================
# WinRM Manager — 飞牛 fnOS .fpk 构建脚本
#
# 用法：
#   ./build_fpk.sh            # 构建 x86_64 版本
#   ./build_fpk.sh arm        # 构建 ARM64 版本
#
# 前置条件：macOS / Linux 上有 python3 + pip，能联网下载 wheels
# 产物：dist/winrm-manager_<版本>_<架构>.fpk
# ============================================================
set -euo pipefail

ARCH="${1:-x86}"
VERSION="1.1.0"
APPNAME="winrmmanager"
ROOT="$(cd "$(dirname "$0")" && pwd)"
SRC_DIR="$(cd "$ROOT/.." && pwd)"          # winrm-manager/ 源码目录
BUILD="$ROOT"

case "$ARCH" in
  x86)  FPK_ARCH="x86";  PIP_PLATFORM="manylinux_2_28_x86_64" ;;
  arm)  FPK_ARCH="arm";  PIP_PLATFORM="manylinux_2_28_aarch64" ;;
  *)    echo "用法: $0 [x86|arm]"; exit 1 ;;
esac

# 用 Python 的 gzip 压缩（mtime=0、OS=255），与飞牛官方包字节头完全一致。
# macOS 的 `tar -z` 会写入当前时间戳和 OS=3，校验严格的安装器可能因此判定包无效。
pack_gz() {
  local dir="$1" out="$2"
  local tar_tmp="${out}.tar"
  ( cd "$dir" && COPYFILE_DISABLE=1 tar --no-xattrs --uid 0 --gid 0 -cf "$tar_tmp" * )
  python3 - "$tar_tmp" "$out" <<'PY'
import gzip, sys
src, dst = sys.argv[1], sys.argv[2]
# 用 gzip.compress（内部 BytesIO，无 name 属性），否则 gzip 头 FLG=0x08
# 会把文件名写进头部，与飞牛官方包（FLG=0x00）不一致
with open(src, "rb") as fi:
    data = fi.read()
with open(dst, "wb") as fo:
    fo.write(gzip.compress(data, compresslevel=6, mtime=0))
PY
  rm -f "$tar_tmp"
}

echo "==> 构建 ${APPNAME} ${VERSION} (${FPK_ARCH})"

# ---------- 1. 准备干净的构建目录 ----------
# 注意：不要预先创建 fpk/cmd、fpk/config、fpk/wizard ——
# `cp -R src dst` 在 dst 已存在时会复制成 dst/src，导致包内出现 cmd/cmd 这类错误层级
rm -rf "$BUILD/app" "$BUILD/fpk" "$BUILD/wheels"
mkdir -p "$BUILD/app/vendor" "$BUILD/app/bin" "$BUILD/app/ui/images" \
         "$BUILD/app/config" "$BUILD/wheels/common" "$BUILD/wheels/cffi" \
         "$BUILD/fpk" "$BUILD/dist"

# ---------- 2. 下载 Linux 依赖 wheels ----------
echo "==> 下载依赖 wheels"
cd "$BUILD/wheels/common"
python3 -m pip download --quiet --no-deps --only-binary=:all: \
  --platform any --python-version 3.11 --implementation py --abi none \
  pywinrm==0.5.0 requests-ntlm==1.3.0 pyspnego requests urllib3 certifi \
  idna charset-normalizer xmltodict pycparser --dest .

python3 -m pip download --quiet --no-deps --only-binary=:all: \
  --platform "$PIP_PLATFORM" --python-version 3.11 --implementation cp --abi abi3 \
  cryptography --dest .

cd "$BUILD/wheels/cffi"
for v in 3.9 3.10 3.11 3.12 3.13; do
  abi="cp${v/./}"
  python3 -m pip download --quiet --no-deps --only-binary=:all: \
    --platform "$PIP_PLATFORM" --python-version "$v" --implementation cp --abi "$abi" \
    cffi --dest . || echo "  ! cffi for py${v} 下载失败（可忽略）"
done

# ---------- 3. 组装 app/ ----------
echo "==> 组装应用目录"
for w in "$BUILD"/wheels/common/*.whl; do unzip -qo "$w" -d "$BUILD/app/vendor/common"; done
for v in 39 310 311 312 313; do
  w=$(ls "$BUILD"/wheels/cffi/cffi-*-cp${v}-*.whl 2>/dev/null | head -1 || true)
  if [ -n "$w" ]; then
    mkdir -p "$BUILD/app/vendor/cp${v}"
    unzip -qo "$w" -d "$BUILD/app/vendor/cp${v}"
  fi
done
find "$BUILD/app/vendor" -type d -name "*.dist-info" -exec rm -rf {} + 2>/dev/null || true
find "$BUILD/app/vendor" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find "$BUILD/app/vendor" -name "*.pyc" -delete 2>/dev/null || true

cp "$SRC_DIR/server.py" "$BUILD/app/server.py"
cp -R "$SRC_DIR/static" "$BUILD/app/static"
cp "$ROOT/assets/bin/run.sh" "$BUILD/app/bin/run.sh"
chmod 755 "$BUILD/app/bin/run.sh"

# 图标：从 1024 母版缩放
ICON_SRC="$ROOT/assets/icon1024.png"
if [ -f "$ICON_SRC" ]; then
  if command -v sips >/dev/null 2>&1; then
    sips -z 64 64   "$ICON_SRC" --out "$BUILD/app/ui/images/icon_64.png"  >/dev/null 2>&1
    sips -z 256 256 "$ICON_SRC" --out "$BUILD/app/ui/images/icon_256.png" >/dev/null 2>&1
  else
    python3 - "$ICON_SRC" "$BUILD/app/ui/images" <<'PY'
import sys
try:
    from PIL import Image
except ImportError:
    sys.exit("需要 Pillow 或 sips 来缩放图标")
src, out = sys.argv[1], sys.argv[2]
im = Image.open(src)
for s in (64, 256):
    im.resize((s, s), Image.LANCZOS).save(f"{out}/icon_{s}.png")
PY
  fi
fi

# ---------- 4. app 内配置 ----------
cp "$ROOT/assets/ui-config.json"          "$BUILD/app/ui/config"
cp "$ROOT/assets/config-privilege.json"   "$BUILD/app/config/privilege"
cp "$ROOT/assets/config-resource.json"    "$BUILD/app/config/resource"

# ---------- 5. 打包 app.tgz ----------
echo "==> 打包 app.tgz"
pack_gz "$BUILD/app" "$BUILD/fpk/app.tgz"

# ---------- 6. fpk 骨架 ----------
# 参照原生参考包：根目录只放 config/，ui/ 仅存在于 app.tgz 内
echo "==> 组装 fpk"
rm -rf "$BUILD/fpk/cmd" "$BUILD/fpk/config" "$BUILD/fpk/wizard"
cp -R "$BUILD/app/config"   "$BUILD/fpk/config"
cp -R "$ROOT/assets/cmd"    "$BUILD/fpk/cmd"
cp -R "$ROOT/assets/wizard" "$BUILD/fpk/wizard"
cp "$BUILD/app/ui/images/icon_64.png"  "$BUILD/fpk/ICON.PNG"
cp "$BUILD/app/ui/images/icon_256.png" "$BUILD/fpk/ICON_256.PNG"
chmod 755 "$BUILD/fpk/cmd/"*

CHECKSUM=$(md5 -q "$BUILD/fpk/app.tgz" 2>/dev/null || md5sum "$BUILD/fpk/app.tgz" | awk '{print $1}')
{
  printf '%-22s= %s\n' appname               "$APPNAME"
  printf '%-22s= %s\n' version               "$VERSION"
  printf '%-22s= %s\n' display_name          "WinRM Manager"
  printf '%-22s= %s\n' desc                  "远程管理 Windows 主机的进程：查看进程列表与 CPU/内存占用、结束进程、启动进程。目标机无需安装客户端，只需启用 WinRM。"
  printf '%-22s= %s\n' platform              "all"
  printf '%-22s= %s\n' source                "thirdparty"
  printf '%-22s= %s\n' desktop_uidir         "ui"
  printf '%-22s= %s\n' desktop_applaunchname "winrmmanager.Application"
  printf '%-22s= %s\n' maintainer            "winrm-manager"
  printf '%-22s= %s\n' install_type          "root"
  printf '%-22s= %s\n' reloadui              "yes"
  printf '%-22s= %s\n' checksum              "$CHECKSUM"
} > "$BUILD/fpk/manifest"

# ---------- 7. 输出 .fpk（属主 root，gzip 头对齐官方包）----------
OUT="$BUILD/dist/${APPNAME}_${VERSION}_${ARCH}.fpk"
rm -f "$OUT"
pack_gz "$BUILD/fpk" "$OUT"

echo
echo "==> 完成: $OUT"
ls -lh "$OUT"
echo "    app.tgz md5 = $CHECKSUM"

# ---------- 8. 自检（结构错了就别交付）----------
echo "==> 自检包结构"
FAIL=0
LIST=$(mktemp)
tar -tzf "$OUT" > "$LIST" 2>/dev/null || { echo "  ✗ fpk 无法解包"; exit 1; }

for f in manifest app.tgz ICON.PNG ICON_256.PNG \
         cmd/main cmd/install_init cmd/install_callback \
         cmd/uninstall_init cmd/uninstall_callback \
         config/privilege config/resource wizard/config wizard/install; do
  grep -qx "$f" "$LIST" || { echo "  ✗ fpk 缺少 $f"; FAIL=1; }
done

# cp -R 陷阱检测：不允许出现 cmd/cmd、config/config、wizard/wizard
# 注意不能写成 (cmd|config|wizard)/(cmd|config|wizard) —— wizard/config 是合法文件
if grep -qE '^(cmd/cmd|config/config|wizard/wizard)(/|$)' "$LIST"; then
  echo "  ✗ 检测到重复目录层级（如 cmd/cmd），cp -R 目标已存在"
  FAIL=1
fi

# app.tgz 内部
APP_LIST=$(mktemp)
tar -tzf "$BUILD/fpk/app.tgz" > "$APP_LIST" 2>/dev/null || { echo "  ✗ app.tgz 无法解包"; FAIL=1; }
for f in server.py static/index.html static/app.js static/style.css \
         bin/run.sh ui/config ui/images/icon_64.png; do
  grep -qx "$f" "$APP_LIST" || { echo "  ✗ app.tgz 缺少 $f"; FAIL=1; }
done
grep -q '^vendor/common/winrm/' "$APP_LIST" || { echo "  ✗ app.tgz 缺少 vendor/common/winrm"; FAIL=1; }
grep -q '^vendor/cp311/cffi/' "$APP_LIST" || { echo "  ✗ app.tgz 缺少 vendor/cp311/cffi"; FAIL=1; }
rm -f "$LIST" "$APP_LIST"

if [ "$FAIL" -ne 0 ]; then
  echo "==> 自检失败，产物不可用"
  exit 1
fi
echo "  ✓ 结构完整"
