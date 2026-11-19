#!/usr/bin/env bash
# Run every test.  Needs PyQt6 (system python3-pyqt6 or pip).  Uses throw-away HOME directories.
set -u
cd "$(dirname "$0")/.."
export QT_QPA_PLATFORM=offscreen
fail=0
t() { echo; echo "=== $1"; shift; "$@" 2>&1 | grep -v "^QStandardPaths\|^qt\.\|libEGL\|propagateSize" | tail -${TAIL:-6}; [ "${PIPESTATUS[0]}" -eq 0 ] || fail=1; }
HOME=$(mktemp -d) t "GUI smoke test (all 6 pages, bundled files, Griffin Play, hardware API)" python3 tests/smoke_gui.py
HOME=$(mktemp -d) t "Griffin Game Tweaks: Steam/VDF/compat editing, setup pipeline (Steam + standalone), rollback, removal" python3 tests/tweaks/test_tweaks.py
t "distro modules: every feature on every distro, bash -n" python3 tests/distro/test_modules.py
t "distro modules: 31 shell flows against stub package managers" python3 tests/distro/test_flows.py
HOME=$(mktemp -d) TAIL=4 t "hubs: GPU / controller / WiFi / Setup / Plugins window" timeout 900 python3 tests/distro/integ_hubs.py
echo; [ $fail -eq 0 ] && echo "ALL TEST SUITES OK" || { echo "SOME SUITES FAILED"; exit 1; }
