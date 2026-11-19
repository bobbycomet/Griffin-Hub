#!/usr/bin/env bash
# Build GriffinHub-<version>-<arch>.AppImage
#
#   ./packaging/build-appimage.sh            (x86_64 or aarch64 host)
#
# Needs: bash, curl, tar, python3 (any), readelf (binutils), internet access for
# the standalone Python + PyQt6 wheels + appimagetool.  FUSE is NOT required.
# Output: dist/GriffinHub-<version>-<arch>.AppImage
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$(cd "$HERE/.." && pwd)"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$SRC/griffinhub/__init__.py")"
APPV="$(sed -n 's/^APP_VERSION = "\(.*\)"/\1/p' "$SRC/griffinhub.py")"
[ -n "$VERSION" ] && [ "$VERSION" = "$APPV" ] || { echo "version mismatch: package=$VERSION griffinhub.py=$APPV"; exit 1; }

ARCH="${ARCH:-$(uname -m)}"
case "$ARCH" in x86_64|aarch64) ;; *) echo "unsupported ARCH=$ARCH (x86_64 or aarch64)"; exit 1;; esac
PBS_TAG="${PBS_TAG:-20261026}"
PY_VER="${PY_VER:-3.12.16}"
# sha256 of the x86_64 stripped install_only tarball for the default tag/version
PBS_SHA256_X86_64="ce4f6d0a6cfdb3bed6e44c2f9affd85943c76a42e25ad3a259002f31d6fb8a20"

OUT="${OUT:-$SRC/dist}"; WORK="${WORK:-$SRC/build/appimage}"; DL="${DL:-$SRC/build/downloads}"
APPDIR="$WORK/AppDir"
mkdir -p "$OUT" "$DL"; rm -rf "$WORK"; mkdir -p "$APPDIR/usr/src" "$APPDIR/usr/lib"
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(date +%s)}"

echo "==> Standalone Python $PY_VER ($ARCH)"
PY_TGZ="$DL/cpython-$PY_VER+$PBS_TAG-$ARCH-unknown-linux-gnu-install_only_stripped.tar.gz"
if [ ! -f "$PY_TGZ" ]; then
  curl -fL --retry 3 -o "$PY_TGZ" "https://github.com/astral-sh/python-build-standalone/releases/download/$PBS_TAG/$(basename "$PY_TGZ" | sed 's/+/%2B/')"
fi
if [ "$ARCH" = x86_64 ] && [ "$PY_VER" = 3.12.16 ] && [ "$PBS_TAG" = 20261026 ]; then
  echo "$PBS_SHA256_X86_64  $PY_TGZ" | sha256sum -c -
fi
tar -xzf "$PY_TGZ" -C "$APPDIR/usr"; mv "$APPDIR/usr/python" "$APPDIR/usr/python.tmp"; mv "$APPDIR/usr/python.tmp" "$APPDIR/usr/python"
PY="$APPDIR/usr/python/bin/python3"

echo "==> PyQt6 (pinned)"
"$PY" -m pip install -q --no-cache-dir --no-compile -r "$HERE/requirements-appimage.txt"
echo "==> Pruning Qt / Python to what Griffin Hub uses"
python3 "$HERE/prune_qt.py" "$APPDIR/usr/python"

echo "==> Bundled xcb helper libraries"
if [ -d "$HERE/xcb-libs/$ARCH" ]; then
  cp -a "$HERE/xcb-libs/$ARCH/." "$APPDIR/usr/lib/"
  cp "$HERE/xcb-libs/LICENSES.txt" "$APPDIR/usr/lib/XCB-LIBS-LICENSES.txt"
else
  echo "   (no bundled xcb libs for $ARCH - the host needs libxcb-cursor0 and the xcb-util libraries)"
fi

echo "==> Application sources"
cp "$SRC/griffinhub.py" "$SRC/griffinhub.png" "$SRC/griffinhub-56.png" "$APPDIR/usr/src/"
cp -a "$SRC/griffinhub" "$APPDIR/usr/src/griffinhub"
cp -a "$SRC/griffin" "$APPDIR/usr/src/griffin"      # Griffin Game Tweaks
find "$APPDIR/usr/src" -name __pycache__ -prune -exec rm -rf {} +
"$PY" -m compileall -q "$APPDIR/usr/src" "$APPDIR/usr/python/lib" || true

echo "==> AppDir metadata"
install -m 755 "$HERE/AppRun" "$APPDIR/AppRun"
install -m 644 "$HERE/griffinhub.desktop" "$APPDIR/griffinhub.desktop"
install -D -m 644 "$HERE/griffinhub.desktop" "$APPDIR/usr/share/applications/griffinhub.desktop"
cp "$SRC/griffinhub.png" "$APPDIR/griffinhub.png"; ln -sf griffinhub.png "$APPDIR/.DirIcon"
"$PY" "$HERE/make_icons.py" "$SRC/griffinhub.png" "$APPDIR/usr/share/icons/hicolor" >/dev/null

echo "==> appimagetool"
TOOL="$DL/appimagetool-$ARCH.AppImage"
[ -f "$TOOL" ] || curl -fL --retry 3 -o "$TOOL" "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-$ARCH.AppImage"
chmod +x "$TOOL"
APPIMAGE="$OUT/GriffinHub-$VERSION-$ARCH.AppImage"
rm -f "$APPIMAGE"
ARCH="$ARCH" "$TOOL" --appimage-extract-and-run --no-appstream "$APPDIR" "$APPIMAGE"
chmod +x "$APPIMAGE"
echo; echo "Built: $APPIMAGE ($(du -h "$APPIMAGE" | cut -f1))"
