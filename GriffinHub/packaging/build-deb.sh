#!/usr/bin/env bash
# Build griffinhub_<ver>-1_all.deb  (needs dpkg-deb; run as any user)
set -euo pipefail
. "$(dirname "$0")/common.sh"
ROOT="$BUILD/deb/${PKG_NAME}_${VERSION}-1_all"; rm -rf "$BUILD/deb"; mkdir -p "$ROOT/DEBIAN"
stage_app "$ROOT/usr/lib/griffinhub"
stage_desktop "$ROOT"
D="$ROOT/usr/share/doc/griffinhub"; install -d "$D"
printf 'Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\nUpstream-Name: Griffin Hub\nSource: %s\n\nFiles: *\nCopyright: Bobby Comet\nLicense: Unspecified\n %s\n' "$PKG_HOMEPAGE" "$PKG_LICENSE_TEXT" > "$D/copyright"
printf 'griffinhub (%s-1) unstable; urgency=medium\n\n  * Griffin Game Tweaks sub-tab; new icons; distro modules; plugin API v2; packaging.\n\n -- %s  %s\n' "$VERSION" "$PKG_MAINTAINER" "$(date -R -d "@$SOURCE_DATE_EPOCH")" | gzip -9n > "$D/changelog.Debian.gz"
SIZE=$(du -sk "$ROOT/usr" | cut -f1)
cat > "$ROOT/DEBIAN/control" <<CTL
Package: $PKG_NAME
Version: $VERSION-1
Section: utils
Priority: optional
Architecture: all
Depends: python3 (>= 3.10), python3-pyqt6, python3-requests, polkitd | policykit-1
Recommends: python3-psutil, python3-yaml, dkms, bubblewrap, cabextract
Suggests: winetricks
Installed-Size: $SIZE
Maintainer: $PKG_MAINTAINER
Homepage: $PKG_HOMEPAGE
Description: $PKG_SUMMARY
 $PKG_DESCRIPTION
CTL
printf '#!/bin/sh\nset -e\nif [ "$1" = configure ]; then\n  python3 -m compileall -q /usr/lib/griffinhub >/dev/null 2>&1 || true\nfi\nexit 0\n' > "$ROOT/DEBIAN/postinst"
printf '#!/bin/sh\nset -e\nif [ "$1" = remove ] || [ "$1" = purge ]; then\n  rm -rf /usr/lib/griffinhub\nfi\nexit 0\n' > "$ROOT/DEBIAN/postrm"
chmod 755 "$ROOT/DEBIAN/postinst" "$ROOT/DEBIAN/postrm"
find "$ROOT" -type d -exec chmod 755 {} +
DEB="$OUT/${PKG_NAME}_${VERSION}-1_all.deb"
dpkg-deb --root-owner-group -Zxz --build "$ROOT" "$DEB" >/dev/null
echo "Built: $DEB ($(du -h "$DEB" | cut -f1))"
