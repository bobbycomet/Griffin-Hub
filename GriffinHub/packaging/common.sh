# sourced by the build scripts
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$(cd "$HERE/.." && pwd)"
# shellcheck disable=SC1091
. "$HERE/pkg.env"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$SRC/griffinhub/__init__.py")"
APPV="$(sed -n 's/^APP_VERSION = "\(.*\)"/\1/p' "$SRC/griffinhub.py")"
[ -n "$VERSION" ] && [ "$VERSION" = "$APPV" ] || { echo "version mismatch: package=$VERSION griffinhub.py=$APPV" >&2; exit 1; }
OUT="${OUT:-$SRC/dist}"; BUILD="${BUILD:-$SRC/build}"
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(date +%s)}"
mkdir -p "$OUT" "$BUILD"

# Copy the installable application tree to <dest> (a /usr/lib/griffinhub style directory)
stage_app() {
  local dest="$1"
  install -d "$dest"
  cp "$SRC/griffinhub.py" "$SRC/griffinhub.png" "$SRC/griffinhub-56.png" "$dest/"
  cp -a "$SRC/griffinhub" "$dest/griffinhub"
  cp -a "$SRC/griffin" "$dest/griffin"          # Griffin Game Tweaks (sub-tab of Griffin Play)
  find "$dest" -name __pycache__ -prune -exec rm -rf {} +
  chmod 755 "$dest/griffinhub.py"
  find "$dest/griffinhub/bundled" -type f \( -name 'run' -o -name 'finish' -o -name 'kernel-autotune' -o -name '*.sh' -o -name 'sentryv3.py' -o -name '*.openrc' \) -exec chmod 755 {} +
}
# Install launcher, desktop file, icons below <root>
stage_desktop() {
  local root="$1"
  install -D -m 755 "$HERE/griffinhub.sh" "$root/usr/bin/griffinhub"
  install -D -m 644 "$HERE/griffinhub.desktop" "$root/usr/share/applications/griffinhub.desktop"
  install -D -m 644 "$SRC/griffinhub.png" "$root/usr/share/pixmaps/griffinhub.png"
  if [ -d "$HERE/icons/hicolor" ]; then
    install -d "$root/usr/share/icons"
    cp -a "$HERE/icons/hicolor" "$root/usr/share/icons/"
  fi
}
