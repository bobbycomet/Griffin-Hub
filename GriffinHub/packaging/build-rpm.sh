#!/usr/bin/env bash
# Build griffinhub-<ver>-1.noarch.rpm  (needs rpmbuild; run packaging/build-tarball.sh first)
set -euo pipefail
. "$(dirname "$0")/common.sh"
command -v rpmbuild >/dev/null || { echo "rpmbuild not found (apt install rpm / dnf install rpm-build)"; exit 1; }
TGZ="$OUT/$PKG_NAME-$VERSION.tar.gz"; [ -f "$TGZ" ] || "$HERE/build-tarball.sh"
TOP="$BUILD/rpm"; rm -rf "$TOP"; mkdir -p "$TOP"/{BUILD,RPMS,SOURCES,SPECS,SRPMS}
cp "$TGZ" "$TOP/SOURCES/"
sed -e "s/@VERSION@/$VERSION/g" -e "s|@HOMEPAGE@|$PKG_HOMEPAGE|g" -e "s|@SUMMARY@|$PKG_SUMMARY|g" \
    -e "s|@LICENSE@|$PKG_LICENSE_SPDX|g" -e "s|@DESCRIPTION@|$PKG_DESCRIPTION|g" "$HERE/griffinhub.spec.in" > "$TOP/SPECS/griffinhub.spec"
rpmbuild -bb --define "_topdir $TOP" --define "_build_id_links none" --define "source_date_epoch_from_changelog 0" "$TOP/SPECS/griffinhub.spec" >/dev/null
cp "$TOP"/RPMS/noarch/*.rpm "$OUT/"
echo "Built: $(ls "$OUT"/*.rpm | tail -1)"
