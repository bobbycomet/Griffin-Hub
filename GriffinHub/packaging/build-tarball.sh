#!/usr/bin/env bash
# Source tarball (for the PKGBUILD) + source/build-kit zip, plus rendered PKGBUILD and .spec
# Output: dist/griffinhub-<ver>.tar.gz  dist/griffinhub-<ver>-source-and-build-kit.zip  dist/PKGBUILD
set -euo pipefail
. "$(dirname "$0")/common.sh"

# hicolor icons so the distro packages need no Python/ImageMagick at build time
if python3 -c "import PyQt6.QtGui" 2>/dev/null; then
  rm -rf "$HERE/icons"; python3 "$HERE/make_icons.py" "$SRC/griffinhub.png" "$HERE/icons/hicolor" >/dev/null
elif [ -d "$HERE/icons/hicolor" ]; then :; else echo "note: PyQt6 not available, icons/ not regenerated"; fi

STAGE="$BUILD/tarball/$PKG_NAME-$VERSION"; rm -rf "$BUILD/tarball"; mkdir -p "$STAGE"
rsync_ex() { tar -C "$SRC" --exclude='./build' --exclude='./dist' --exclude='__pycache__' --exclude='*.pyc' -cf - . | tar -C "$STAGE" -xf -; }
rsync_ex
TGZ="$OUT/$PKG_NAME-$VERSION.tar.gz"
tar -C "$BUILD/tarball" --sort=name --owner=0 --group=0 --numeric-owner --mtime="@$SOURCE_DATE_EPOCH" -czf "$TGZ" "$PKG_NAME-$VERSION"
echo "Built: $TGZ"

SHA="$(sha256sum "$TGZ" | cut -d' ' -f1)"
sed -e "s/@VERSION@/$VERSION/g" -e "s/@SHA256@/$SHA/g" -e "s|@HOMEPAGE@|$PKG_HOMEPAGE|g" \
    -e "s|@MAINTAINER@|$PKG_MAINTAINER|g" -e "s|@SUMMARY@|$PKG_SUMMARY|g" "$HERE/PKGBUILD.in" > "$OUT/PKGBUILD"
echo "Rendered: $OUT/PKGBUILD"

if command -v zip >/dev/null 2>&1; then
  ZIP="$OUT/$PKG_NAME-$VERSION-source-and-build-kit.zip"; rm -f "$ZIP"
  (cd "$BUILD/tarball" && zip -qr "$ZIP" "$PKG_NAME-$VERSION")
  echo "Built: $ZIP"
else echo "note: 'zip' not installed - skipped the zip kit"; fi
