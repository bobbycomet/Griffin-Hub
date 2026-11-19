#!/usr/bin/env bash
# Build everything this machine can: tarball+zip, PKGBUILD, .deb, .rpm, AppImage, SHA256SUMS
set -uo pipefail
. "$(dirname "$0")/common.sh"
run() { echo; echo "######## $*"; "$@" || echo "!! $1 failed (continuing)"; }
run "$HERE/build-tarball.sh"
command -v dpkg-deb >/dev/null && run "$HERE/build-deb.sh" || echo "-- skipping .deb (dpkg-deb not installed)"
command -v rpmbuild >/dev/null && run "$HERE/build-rpm.sh" || echo "-- skipping .rpm (rpmbuild not installed)"
run "$HERE/build-appimage.sh"
( cd "$OUT" && sha256sum -- * > SHA256SUMS 2>/dev/null; echo; echo "dist/:"; ls -lh )
