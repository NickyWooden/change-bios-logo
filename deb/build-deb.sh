#!/bin/sh
# =============================================================================
# build-deb.sh — 构建 change-bios-logo 的 .deb 软件包
#
# 用法：
#   cd <项目根目录>/deb
#   ./build-deb.sh
#
# 前提（构建机）：
#   - dpkg-deb（dpkg 自带，Ubuntu/Debian 默认就有）
#   - python3（CPython 3.10+，建议 3.14；venv 的 ABI 与解释器版本绑定，
#     换版本重建 venv 即可，源码本身跨版本通用）
#   - 建议装 uv（uv 0.12+）：建 venv 与装依赖都更快
#
# 流程：
#   1. 把项目源码复制到 pkg/opt/change-bios-logo/
#   2. 在 pkg/opt/change-bios-logo/venv/ 建虚拟环境并装依赖（幂等，可重复跑）
#   3. 用 du 重算 Installed-Size 并写回 DEBIAN/control
#   4. dpkg-deb --build --root-owner-group 产出 .deb
#
# 说明：
#   - pkg/opt/ 是「源码副本 + venv」，体积大，已用 .gitignore 忽略、不入库；
#     本脚本负责在构建时重建它，所以 clone 下来即可直接构建。
#   - 产出的 .deb 落在 deb/ 下，同样被 .gitignore 忽略；要发布请上传到
#     GitHub Releases（或自行分发）。
# =============================================================================
set -eu

# 本脚本位于 deb/ 下，项目根目录是上一级
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PKG="pkg"
OPT="$PKG/opt/change-bios-logo"
VENV="$OPT/venv"

echo "==> [1/4] 复制源码到 $OPT/"
mkdir -p "$OPT"
cp "$ROOT/change_bios_logo.py" "$ROOT/bioslogo.py" "$ROOT/glass.py" \
   "$ROOT/requirements.txt" "$ROOT/app.ico" "$ROOT/README.md" "$OPT/"
# docs/ 截图（可选，缺失则跳过）
if [ -d "$ROOT/docs" ]; then
    rm -rf "$OPT/docs"
    cp -r "$ROOT/docs" "$OPT/docs"
fi
# 运行时备份目录（0777，任何用户可写）
mkdir -p "$OPT/backup"
chmod 0777 "$OPT/backup"

echo "==> [2/4] 创建 venv 并安装依赖"
if [ ! -x "$VENV/bin/python" ]; then
    if command -v uv >/dev/null 2>&1; then
        uv venv "$VENV"
    else
        python3 -m venv "$VENV"
    fi
fi
if command -v uv >/dev/null 2>&1; then
    uv pip install --python "$VENV/bin/python" -r "$OPT/requirements.txt"
else
    "$VENV/bin/python" -m pip install -r "$OPT/requirements.txt"
fi
# （可选）预编译 pyc，加快首次启动；缺失时 Python 运行时会自行编译
"$VENV/bin/python" -m py_compile "$OPT/change_bios_logo.py" \
    "$OPT/bioslogo.py" "$OPT/glass.py" || true

echo "==> [3/4] 重算 Installed-Size"
# 块占用（KiB），排除 DEBIAN 元数据目录
SIZE="$(du -sk --exclude=DEBIAN "$PKG" | awk '{print $1}')"
sed -i "s/^Installed-Size: .*/Installed-Size: $SIZE/" "$PKG/DEBIAN/control"
echo "    Installed-Size: $SIZE"

echo "==> [4/4] 构建 .deb"
VERSION="$(sed -n 's/^Version: //p' "$PKG/DEBIAN/control" | head -1)"
ARCH="$(sed -n 's/^Architecture: //p' "$PKG/DEBIAN/control" | head -1)"
OUT="change-bios-logo_${VERSION}_${ARCH}.deb"
dpkg-deb --build --root-owner-group "$PKG" "$OUT"
echo "==> 完成：$OUT"
