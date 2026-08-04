
#!/usr/bin/env python3
"""
Griffin Hub - Linux System Tuning & Hardware Utility
A PyQt6 desktop app for tuning your gaming/desktop Linux system and
managing hardware (CPU, GPU, controllers, WiFi), extensible via plugins.

Version: 1.0.1

Requirements:
    pip install PyQt6

Run (GUI):
    python3 griffinhub.py

Run (CLI — no display needed):
    python3 griffinhub.py --help
    python3 griffinhub.py plugin list
    python3 griffinhub.py settings list

Error handling contract:
    Core layer (Store, workers)  → raises GriffinHubError subclasses
    CLI layer                    → catches → prints via _cli_err(), exits non-zero
    GUI layer                    → catches → QMessageBox.critical()

Logging:
    All events logged to ~/.local/share/griffinhub/logs/griffinhub.log
    Open: Settings → Open Log Folder

Plugin system:
    Drop a .py file into ~/.local/share/griffinhub/plugins/
    Plugins extend System Tune and the Hardware Hub with extra diagnostic
    checks, one-click actions, and (for trusted plugins) extension patches.
    Each plugin may define:
        PLUGIN_CHECKS     - list of diagnostic check dicts
        PLUGIN_ACTIONS    - list of one-click action dicts
        PLUGIN_EXTENDS    - dict: { "target_name": { patch } }
                            Extends a named extension point (see
                            PluginRegistry.EXTENSION_TARGETS).
        PLUGIN_METADATA   - dict with version, api_version, author, description,
                            trust_level ("safe" | "trusted")
        register(registry) - called at startup (trusted plugins only); registry
                             exposes .add_check(), .add_action(), .extend()
    ⚠ Plugins execute arbitrary Python. Only install plugins you trust.
    ⚠ An AST safety scanner runs before execution and will block plugins that
      use os.system(), eval(), exec(), shell=True subprocesses, shutil.rmtree(),
      or dangerous shell-command literals (e.g. "rm -rf").

Plugin trust tiers:
    Declared in PLUGIN_METADATA["trust_level"]:
      "safe"    — declarative-only. No register() hook, no dict-valued
                  PLUGIN_EXTENDS fields. Default for all community/sandboxed
                  plugins.
      "trusted" — full API access including register() and dict-valued
                  PLUGIN_EXTENDS fields. Requires a .trusted sidecar file.
                  Cannot be self-granted; must be placed manually by the user.

    Mapping to existing execution contexts:
      SANDBOXED (community/store) → trust_level forced to "safe"
      In-process without .trusted → trust_level capped at "safe"
      In-process with .trusted    → trust_level="trusted" respected

Sandbox system:
    Three execution contexts govern how code runs:
      SANDBOXED  — community plugins / store downloads.
                   Evaluated via bubblewrap (if installed) or a stripped
                   subprocess (-S, no site-packages, minimal env).
                   No home dir, no network. Declarative-only output (JSON).
                   register() hook NOT called. Toggle: settings["plugin_sandbox"].
      CONTROLLED — plugin checks / actions run as plain subprocess with an
                   ALLOWED_CHECK_EXECUTABLES allowlist.
      NATIVE     — System Tune / Hardware Hub's own root-requiring writes.
                   All use pkexec so the GUI never has to run as root itself.

    .trusted sidecar: a plugin with a matching .trusted file is loaded
    in-process with full registry access, bypassing the sandbox.
    Must be created manually by the user; cannot be self-granted.

Subprocess safety:
    Worker subprocess calls are guarded by a dual-mechanism timeout:
      1. A watchdog thread that kills silently-stalled processes once the
         wall-clock deadline is reached.
      2. Per-line deadline checks in the output-iteration loop.
    Both mechanisms fire cancel() on the process; whichever triggers first wins.

Plugin community store:
    https://github.com/bobbycomet/GriffinHub-Plugins
"""

import sys
import os
import re
import ast
import json
import copy
import shutil
import tempfile
import hashlib
import functools
import importlib
import importlib.util
import urllib.request
import ssl
import subprocess
import threading
import logging
import logging.handlers
import time
from pathlib import Path
from datetime import datetime
from typing import Any

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QStackedWidget, QScrollArea, QFrame,
    QGridLayout, QProgressBar, QSizePolicy, QSpacerItem,
    QTabWidget, QLineEdit, QComboBox, QCheckBox, QDialog,
    QDialogButtonBox, QTextEdit, QListWidget, QListWidgetItem,
    QSplitter, QGroupBox, QToolButton, QMessageBox, QFileDialog,
    QSlider, QRadioButton, QButtonGroup,
    QSpinBox, QDoubleSpinBox, QPlainTextEdit, QFormLayout,
    QLayout
)
from PyQt6.QtCore import (
    Qt, QThread, pyqtSignal, QTimer, QPropertyAnimation,
    QEasingCurve, QSize, QPoint, QPointF, QRect, QObject, pyqtProperty,
    QRunnable, QThreadPool,
)
from PyQt6.QtGui import (
    QFont, QIcon, QPixmap, QPalette, QColor, QPainter,
    QLinearGradient, QRadialGradient, QBrush, QPen,
    QFontDatabase, QCursor, QPainterPath, QMovie
)

from enum import Enum


# ─────────────────────────────────────────────
#  THEME & CONSTANTS
# ─────────────────────────────────────────────

APP_NAME    = "Griffin Hub"
APP_VERSION = "1.0.1"
APP_ICON_NAME = "griffinhub.png"
BASE_DIR    = Path.home() / ".local" / "share" / "griffinhub"
CACHE_DIR   = BASE_DIR / "cache"
PLUGINS_DIR = BASE_DIR / "plugins"
# Where plugin actions should git-clone driver sources into (e.g. an
# out-of-tree controller driver). User-owned, so a clone action never needs
# pkexec — only a subsequent build/install step (DKMS, make install, ...)
# typically does. See PLUGINS.md.
PLUGIN_DRIVERS_DIR = BASE_DIR / "drivers"
SETTINGS_FILE = BASE_DIR / "settings.json"
LOGS_DIR    = BASE_DIR / "logs"
STORE_CACHE_FILE = BASE_DIR / "cache" / "store_index.json"

DEFAULT_STORE_URL = (
    "https://cdn.jsdelivr.net/gh/bobbycomet/GriffinHub-Plugins@main/index.json"
)
STORE_CACHE_TTL = 3600


def self_invoke_command() -> list:
    """
    Return the argv prefix used to re-launch Griffin Hub itself (e.g. for a
    helper that needs to call back into the app from outside the running
    process).

    When running as an AppImage, $APPIMAGE points to the persistent .AppImage
    file on disk (not the ephemeral squashfs mount under /tmp, which only
    exists while the original process is alive). Re-exec that file directly.

    Otherwise, fall back to "python3 <this script>", which is correct for a
    plain source checkout / non-packaged run.
    """
    appimage = os.environ.get("APPIMAGE")
    if appimage:
        return [appimage]
    return [sys.executable, str(Path(__file__).resolve())]


# ── yaml — required by ProcessSentry config editor ───────────────────────────
try:
    import yaml as _yaml_mod
    _YAML_OK = True
except ImportError:
    _yaml_mod = None  # type: ignore
    _YAML_OK = False

def _yaml_safe_load(text: str) -> Any:
    if _YAML_OK:
        return _yaml_mod.safe_load(text)  # type: ignore
    raise RuntimeError("PyYAML not installed. Run: pip install pyyaml")

def _yaml_dump(obj: Any) -> str:
    if _YAML_OK:
        return _yaml_mod.dump(obj, default_flow_style=False)  # type: ignore
    raise RuntimeError("PyYAML not installed. Run: pip install pyyaml")


# ─────────────────────────────────────────────
#  SYSTEM TUNE — PATH CONSTANTS
# ─────────────────────────────────────────────

GT_SENTRY_CONFIG     = Path("/etc/process-sentry/config.yaml")
GT_SENTRY_BIN        = Path("/usr/local/bin/sentryv3.py")
GT_SENTRY_SERVICE    = "resource-sentry.service"
# Optional live stats the daemon may write (mirrors kernel-autotune's
# state.json convention). Not guaranteed to exist — the Dashboard degrades
# to "—" for anything it can't read from here rather than inventing numbers.
GT_SENTRY_STATS      = Path("/var/lib/process-sentry/stats.json")

GT_KERNEL_BIN        = Path("/usr/local/bin/kernel-autotune")
GT_KERNEL_CONFIG     = Path("/etc/kernel-autotune/config.sh")
GT_KERNEL_STATE      = Path("/etc/kernel-autotune/state.json")
GT_KERNEL_SERVICE    = "kernel-autotune.service"

GT_STEAM_DESKTOP_SRC = Path("/usr/share/applications/steam.desktop")
GT_STEAM_DESKTOP_DST = Path.home() / ".local/share/applications/steam.desktop"
GT_STEAM_DESKTOP_BAK = Path.home() / ".local/share/applications/steam.desktop.bak"
GT_WRAPPER_DIR       = Path("/usr/local/bin")
GT_COMBINED_WRAPPER  = GT_WRAPPER_DIR / "steam-gaming-wrapper"
GT_CONF_DIR          = Path.home() / ".config/griffinhub"
GT_CONF              = GT_CONF_DIR / "steam.conf"
GT_UDEV_RULES        = Path("/etc/udev/rules.d/99-griffinhub.rules")
GT_POLKIT_RULE       = Path("/etc/polkit-1/rules.d/50-griffinhub.rules")
GT_KILL_SWITCH       = Path("/etc/no-auto-throttle")
GT_STEAM_BIN         = Path("/usr/bin/steam")

# ── PolicyKit pkexec helper ───────────────────────────────────────────────
# Every privileged action in Griffin Hub goes through pkexec. That's fine for
# a normal install, but Griffin Hub also ships as an AppImage, and pkexec
# refuses to elevate a target that isn't a stable, root-verifiable file —
# an AppImage is user-owned and FUSE-mounted in the invoking user's session,
# which doesn't satisfy that. The fix is a tiny helper script installed to a
# normal system path (so it's always a valid pkexec target regardless of how
# Griffin Hub itself is packaged) plus a PolicyKit .policy file so the
# authentication prompt is branded instead of generic. Installing it is
# optional but recommended — see Settings → PolicyKit. Until it's installed,
# gt_pkexec_run/gt_pkexec_write/_gh_run_pkexec fall back to calling pkexec
# directly, exactly as before.
GT_POLKIT_HELPER_PATH   = Path("/usr/local/bin/griffinhub-pkexec-helper")
GT_POLKIT_POLICY_PATH   = Path("/usr/share/polkit-1/actions/com.griffinhub.pkexec.policy")
GT_POLKIT_ACTION_ID     = "com.griffinhub.pkexec.run"

GT_POLKIT_HELPER_SCRIPT = """#!/bin/bash
# Griffin Hub PolicyKit helper.
# Installed by Griffin Hub (Settings -> PolicyKit -> Install Helper).
# Not meant to be run directly — it's a pkexec target that simply execs
# whatever argv it's given, so pkexec always has a stable, root-verifiable
# path to elevate regardless of how Griffin Hub itself is packaged/mounted.
exec "$@"
"""

GT_POLKIT_POLICY_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE policyconfig PUBLIC "-//freedesktop//DTD PolicyKit Policy Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/PolicyKit/1/policyconfig.dtd">
<policyconfig>
  <vendor>Griffin Hub</vendor>
  <action id="{GT_POLKIT_ACTION_ID}">
    <description>Run a Griffin Hub system tuning command</description>
    <message>Griffin Hub needs to make a system change</message>
    <icon_name>{APP_ICON_NAME.rsplit('.', 1)[0]}</icon_name>
    <defaults>
      <allow_any>auth_admin</allow_any>
      <allow_inactive>auth_admin</allow_inactive>
      <allow_active>auth_admin_keep</allow_active>
    </defaults>
    <annotate key="org.freedesktop.policykit.exec.path">{GT_POLKIT_HELPER_PATH}</annotate>
    <annotate key="org.freedesktop.policykit.exec.allow_gui">true</annotate>
  </action>
</policyconfig>
"""

GT_CONF_DEFAULTS: dict[str, str] = {
    "GAMEMODE": "1", "GAMESCOPE": "1", "MANGOHUD": "1",
    "GS_W": "1920", "GS_H": "1080",
    "GS_OW": "1920", "GS_OH": "1080",
    "GS_R": "60",
    "GS_FSR": "0", "GS_VRR": "0", "GS_HDR": "0",
}


# ─────────────────────────────────────────────
#  SYSTEM TUNE — SUBPROCESS / CONFIG HELPERS
# ─────────────────────────────────────────────

def gt_run(cmd, capture=True) -> tuple[int, str, str]:
    try:
        r = subprocess.run(
            cmd, shell=isinstance(cmd, str),
            capture_output=capture, text=True, timeout=30
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as exc:
        return 1, "", str(exc)


def gt_shlex_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def _gt_pkexec_prefix() -> list:
    """Prepend the installed PolicyKit helper to a pkexec argv, if installed."""
    return [str(GT_POLKIT_HELPER_PATH)] if GT_POLKIT_HELPER_PATH.exists() else []


def gt_pkexec_write(path: Path, content: str) -> tuple[bool, str]:
    cmd = ["pkexec"] + _gt_pkexec_prefix() + ["sh", "-c", "cat > " + gt_shlex_quote(str(path))]
    try:
        r = subprocess.run(cmd, input=content, capture_output=True,
                           text=True, timeout=30)
        return r.returncode == 0, r.stderr.strip()
    except Exception as exc:
        return False, str(exc)


def gt_pkexec_run(args: list) -> tuple[bool, str]:
    try:
        r = subprocess.run(
            ["pkexec"] + _gt_pkexec_prefix() + args, capture_output=True, text=True, timeout=30
        )
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except Exception as exc:
        return False, str(exc)


def gt_polkit_helper_installed() -> bool:
    return GT_POLKIT_HELPER_PATH.exists() and GT_POLKIT_POLICY_PATH.exists()


def gt_install_polkit_helper() -> tuple[bool, str]:
    """
    Install the pkexec helper script + PolicyKit action file.

    This bootstrap step itself doesn't need the helper (that would be
    the chicken-and-egg problem) — it uses plain pkexec against ordinary
    system binaries (sh/install), which always works. The helper and policy
    content are generated to temp files as the current user first (never
    read out of the AppImage's FUSE mount, which the pkexec'd root process
    may not be able to see), then moved into place as root in one shot.
    """
    try:
        tmp_dir = Path(tempfile.mkdtemp(prefix="griffinhub-polkit-"))
        helper_tmp = tmp_dir / "griffinhub-pkexec-helper"
        policy_tmp = tmp_dir / "com.griffinhub.pkexec.policy"
        helper_tmp.write_text(GT_POLKIT_HELPER_SCRIPT)
        policy_tmp.write_text(GT_POLKIT_POLICY_XML)
        helper_tmp.chmod(0o755)
    except OSError as exc:
        return False, f"Could not prepare helper files: {exc}"

    script = (
        f"install -m 755 {gt_shlex_quote(str(helper_tmp))} {gt_shlex_quote(str(GT_POLKIT_HELPER_PATH))} && "
        f"mkdir -p {gt_shlex_quote(str(GT_POLKIT_POLICY_PATH.parent))} && "
        f"install -m 644 {gt_shlex_quote(str(policy_tmp))} {gt_shlex_quote(str(GT_POLKIT_POLICY_PATH))}"
    )
    ok, err = gt_pkexec_run(["sh", "-c", script])  # not yet installed, so this call uses plain pkexec

    try:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    except OSError:
        pass

    if ok:
        return True, f"Installed helper to {GT_POLKIT_HELPER_PATH}\nand policy to {GT_POLKIT_POLICY_PATH}."
    return False, err or "Installation failed."


def gt_uninstall_polkit_helper() -> tuple[bool, str]:
    ok, err = gt_pkexec_run(["rm", "-f", str(GT_POLKIT_HELPER_PATH), str(GT_POLKIT_POLICY_PATH)])
    if ok:
        return True, "PolicyKit helper removed."
    return False, err or "Removal failed."


def gt_systemctl_status(service: str) -> str:
    _rc, out, _err = gt_run(["systemctl", "is-active", service])
    state = out.strip()
    return state if state else "unknown"


def gt_systemctl_action(action: str, service: str) -> tuple[bool, str]:
    return gt_pkexec_run(["systemctl", action, service])


def gt_is_installed(binary: str) -> bool:
    rc, _, _ = gt_run(["which", binary])
    return rc == 0


def gt_pkg_installed(pkg: str) -> bool:
    """
    Return True if *pkg* appears to be installed.
    Tries the host's package manager (dpkg / rpm / pacman) so the same
    helper works on Debian/Ubuntu, Fedora/RHEL, and Arch-based systems.
    """
    # Debian / Ubuntu / derivatives
    if shutil.which("dpkg"):
        rc, out, _ = gt_run(["dpkg", "-s", pkg])
        if rc == 0 and "Status: install ok installed" in out:
            return True
    # Fedora / RHEL / openSUSE (rpm)
    if shutil.which("rpm"):
        rc, _, _ = gt_run(["rpm", "-q", pkg])
        if rc == 0:
            return True
    # Arch / Manjaro / EndeavourOS
    if shutil.which("pacman"):
        rc, _, _ = gt_run(["pacman", "-Q", pkg])
        if rc == 0:
            return True
    return False


def gt_real_user() -> str:
    return (
        os.environ.get("SUDO_USER")
        or os.environ.get("USER")
        or os.environ.get("LOGNAME", "")
    )


def gt_read_steam_conf() -> dict:
    cfg = dict(GT_CONF_DEFAULTS)
    if GT_CONF.exists():
        for line in GT_CONF.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                cfg[k.strip()] = v.strip()
    return cfg


def gt_write_steam_conf(cfg: dict) -> None:
    GT_CONF_DIR.mkdir(parents=True, exist_ok=True)
    lines = ["# Griffin Hub — Steam wrapper config", "# 1=enabled  0=disabled"]
    for k, v in cfg.items():
        lines.append(k + "=" + v)
    GT_CONF.write_text("\n".join(lines) + "\n")


def gt_backup_exists() -> bool:
    return GT_STEAM_DESKTOP_BAK.exists()


def gt_create_backup() -> tuple[bool, str]:
    if GT_STEAM_DESKTOP_BAK.exists():
        return True, "Backup already exists."
    src = GT_STEAM_DESKTOP_SRC
    if not src.exists():
        return False, "Cannot find " + str(src) + " to back up."
    try:
        GT_STEAM_DESKTOP_DST.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, GT_STEAM_DESKTOP_BAK)
        return True, str(GT_STEAM_DESKTOP_BAK)
    except Exception as exc:
        return False, str(exc)


def gt_restore_from_backup() -> tuple[bool, str]:
    if not GT_STEAM_DESKTOP_BAK.exists():
        return False, "No backup found."
    try:
        shutil.copy(GT_STEAM_DESKTOP_BAK, GT_STEAM_DESKTOP_DST)
        if GT_COMBINED_WRAPPER.exists():
            GT_COMBINED_WRAPPER.unlink()
        if GT_CONF.exists():
            GT_CONF.unlink()
        GT_STEAM_DESKTOP_BAK.unlink()
        return True, ""
    except Exception as exc:
        return False, str(exc)


def gt_steam_desktop_exec() -> str:
    target = GT_STEAM_DESKTOP_DST if GT_STEAM_DESKTOP_DST.exists() else None
    if not target:
        return ""
    in_main = False
    for line in target.read_text().splitlines():
        stripped = line.strip()
        if stripped == "[Desktop Entry]":
            in_main = True
            continue
        if stripped.startswith("[") and in_main:
            break
        if in_main and stripped.startswith("Exec="):
            return stripped[5:].strip()
    return ""


def gt_build_wrapper_script(cfg: dict, gs_flags: str) -> str:
    conf_path = str(GT_CONF)
    steam     = str(GT_STEAM_BIN)
    lines = [
        "#!/bin/bash",
        "# Griffin Hub — config-driven Steam wrapper",
        "# Edit " + conf_path + " to toggle tools without restarting.",
        "",
        "GAMEMODE=1 GAMESCOPE=1 MANGOHUD=1",
        "[ -f " + gt_shlex_quote(conf_path) + " ] && source " + gt_shlex_quote(conf_path),
        "",
        "STEAM=" + gt_shlex_quote(steam),
        "",
        'CMD=("$STEAM" "$@")',
        "",
        'if [ "$MANGOHUD" = "1" ]; then',
        '    CMD=(mangohud "${CMD[@]}")',
        "fi",
        "",
        'if [ "$GAMESCOPE" = "1" ]; then',
        "    CMD=(gamescope \\",
        "        " + gs_flags.replace("\n", "\n        ") + " \\",
        '        -- "${CMD[@]}")',
        "fi",
        "",
        'if [ "$GAMEMODE" = "1" ]; then',
        '    CMD=(gamemoderun "${CMD[@]}")',
        "fi",
        "",
        'exec "${CMD[@]}"',
    ]
    return "\n".join(lines) + "\n"


# ─────────────────────────────────────────────
#  SYSTEM TUNE — BACKGROUND WORKER (QRunnable)
# ─────────────────────────────────────────────



class GtWorkerSignals(QObject):
    result = pyqtSignal(bool, str)


class GtWorker(QRunnable):
    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn     = fn
        self.args   = args
        self.kwargs = kwargs
        self.signals = GtWorkerSignals()

    def run(self):
        try:
            ok, msg = self.fn(*self.args, **self.kwargs)
            self.signals.result.emit(ok, msg)
        except Exception as exc:
            self.signals.result.emit(False, str(exc))


def gt_run_bg(fn, *args, on_result=None, **kwargs) -> None:
    worker = GtWorker(fn, *args, **kwargs)
    if on_result:
        worker.signals.result.connect(on_result)
    QThreadPool.globalInstance().start(worker)

COLORS = {
    "bg_darkest":    "#070d14",
    "bg_dark":       "#0d1825",
    "bg_mid":        "#111f30",
    "bg_panel":      "#162438",
    "bg_card":       "#1a2d42",
    "bg_card_hover": "#1f3550",
    "border":        "#1e3a56",
    "border_bright": "#2a5070",
    "accent":        "#1e9fda",
    "accent_dim":    "#1470a0",
    "accent_glow":   "#40c4ff",
    "accent2":       "#0066ff",
    "silver":        "#c0ccd8",
    "silver_dim":    "#7a9ab8",
    "text_primary":  "#e8f4fd",
    "text_secondary":"#7aa8c8",
    "text_muted":    "#3d6685",
    "success":       "#22c55e",
    "warning":       "#f59e0b",
    "error":         "#ef4444",
    "white":         "#ffffff",
}

STYLESHEET = f"""
QMainWindow, QDialog {{
    background-color: {COLORS['bg_darkest']};
}}

QWidget {{
    background-color: transparent;
    color: {COLORS['text_primary']};
    font-family: 'Segoe UI', 'Ubuntu', 'Noto Sans', sans-serif;
    font-size: 13px;
}}

QScrollArea {{
    background-color: transparent;
    border: none;
}}

QScrollBar:vertical {{
    background: {COLORS['bg_dark']};
    width: 6px;
    border-radius: 3px;
}}
QScrollBar::handle:vertical {{
    background: {COLORS['border_bright']};
    border-radius: 3px;
    min-height: 20px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}

QScrollBar:horizontal {{
    background: {COLORS['bg_dark']};
    height: 6px;
    border-radius: 3px;
}}
QScrollBar::handle:horizontal {{
    background: {COLORS['border_bright']};
    border-radius: 3px;
}}

QToolTip {{
    background-color: {COLORS['bg_card']};
    color: {COLORS['text_primary']};
    border: 1px solid {COLORS['border_bright']};
    border-radius: 4px;
    padding: 6px 10px;
    font-size: 12px;
}}

QMessageBox {{
    background-color: {COLORS['bg_mid']};
}}
QMessageBox QLabel {{
    color: {COLORS['text_primary']};
}}
QMessageBox QPushButton {{
    background-color: {COLORS['accent']};
    color: white;
    border-radius: 6px;
    padding: 8px 20px;
    min-width: 80px;
}}

QComboBox {{
    background-color: {COLORS['bg_card']};
    border: 1px solid {COLORS['border']};
    border-radius: 6px;
    padding: 6px 12px;
    color: {COLORS['text_primary']};
    min-height: 32px;
}}
QComboBox:hover {{
    border-color: {COLORS['accent_dim']};
}}
QComboBox::drop-down {{
    border: none;
    width: 24px;
}}
QComboBox QAbstractItemView {{
    background-color: {COLORS['bg_card']};
    border: 1px solid {COLORS['border_bright']};
    border-radius: 6px;
    color: {COLORS['text_primary']};
    selection-background-color: {COLORS['accent_dim']};
}}

QLineEdit {{
    background-color: {COLORS['bg_card']};
    border: 1px solid {COLORS['border']};
    border-radius: 6px;
    padding: 8px 12px;
    color: {COLORS['text_primary']};
    min-height: 32px;
}}
QLineEdit:focus {{
    border-color: {COLORS['accent']};
}}

QCheckBox {{
    color: {COLORS['text_secondary']};
    spacing: 8px;
}}
QCheckBox::indicator {{
    width: 18px;
    height: 18px;
    border: 2px solid {COLORS['border_bright']};
    border-radius: 4px;
    background-color: {COLORS['bg_card']};
}}
QCheckBox::indicator:checked {{
    background-color: {COLORS['accent']};
    border-color: {COLORS['accent']};
}}
QCheckBox::indicator:hover {{
    border-color: {COLORS['accent']};
}}

QProgressBar {{
    background-color: {COLORS['bg_card']};
    border: 1px solid {COLORS['border']};
    border-radius: 4px;
    height: 8px;
    text-align: center;
    color: transparent;
}}
QProgressBar::chunk {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {COLORS['accent_dim']}, stop:1 {COLORS['accent_glow']});
    border-radius: 4px;
}}

QTabWidget::pane {{
    border: 1px solid {COLORS['border']};
    border-radius: 8px;
    background-color: {COLORS['bg_panel']};
    top: -1px;
}}
QTabBar::tab {{
    background-color: {COLORS['bg_card']};
    color: {COLORS['text_secondary']};
    padding: 10px 20px;
    border-radius: 6px 6px 0 0;
    margin-right: 2px;
    font-size: 13px;
}}
QTabBar::tab:selected {{
    background-color: {COLORS['bg_panel']};
    color: {COLORS['accent_glow']};
    border-bottom: 2px solid {COLORS['accent']};
}}
QTabBar::tab:hover:!selected {{
    background-color: {COLORS['bg_card_hover']};
    color: {COLORS['text_primary']};
}}

QGroupBox {{
    border: 1px solid {COLORS['border']};
    border-radius: 8px;
    margin-top: 16px;
    padding-top: 8px;
    color: {COLORS['text_secondary']};
    font-size: 12px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 12px;
    top: -8px;
    padding: 2px 8px;
    background-color: {COLORS['bg_mid']};
    color: {COLORS['accent']};
    font-size: 11px;
    font-weight: bold;
    text-transform: uppercase;
    letter-spacing: 1px;
}}

QListWidget {{
    background-color: {COLORS['bg_card']};
    border: 1px solid {COLORS['border']};
    border-radius: 8px;
    padding: 4px;
    outline: none;
}}
QListWidget::item {{
    border-radius: 6px;
    padding: 8px 12px;
    margin: 2px 0;
    color: {COLORS['text_secondary']};
}}
QListWidget::item:selected {{
    background-color: {COLORS['accent_dim']};
    color: {COLORS['text_primary']};
}}
QListWidget::item:hover:!selected {{
    background-color: {COLORS['bg_card_hover']};
    color: {COLORS['text_primary']};
}}

QTextEdit {{
    background-color: {COLORS['bg_darkest']};
    border: 1px solid {COLORS['border']};
    border-radius: 8px;
    padding: 8px;
    color: {COLORS['silver_dim']};
    font-family: 'JetBrains Mono', 'Fira Mono', 'Courier New', monospace;
    font-size: 12px;
}}
"""


# ─────────────────────────────────────────────
#  GAMETUNE HUB — SHARED UI WIDGETS
# ─────────────────────────────────────────────

GT_INFO_STYLE = (
    f"background-color:{COLORS['bg_card']};border:1px solid #2d4a7a;"
    f"border-radius:8px;padding:12px 16px;color:{COLORS['accent']};font-size:12px;"
)
GT_WARN_STYLE = (
    f"background-color:#2d1b00;border:1px solid #7a4a00;"
    f"border-radius:8px;padding:12px 16px;color:#e3b341;font-size:12px;"
)


def gt_info_box(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setStyleSheet(GT_INFO_STYLE)
    return lbl


def gt_warn_box(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setStyleSheet(GT_WARN_STYLE)
    return lbl


def gt_separator() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.Shape.HLine)
    f.setStyleSheet(f"background-color:{COLORS['border']}; max-height:1px;")
    return f


def gt_scrolled(widget_factory) -> QScrollArea:
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    inner  = QWidget()
    scroll.setWidget(inner)
    layout = QVBoxLayout(inner)
    layout.setSpacing(14)
    layout.setContentsMargins(16, 16, 16, 16)
    widget_factory(layout)
    layout.addStretch()
    return scroll


class GtServiceStatusWidget(QWidget):
    def __init__(self, service_name: str, display_name: str, parent=None):
        super().__init__(parent)
        self.service_name = service_name
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.dot = QLabel("●")
        self.dot.setFont(QFont("monospace", 14))
        layout.addWidget(self.dot)
        lbl = QLabel(display_name)
        lbl.setStyleSheet("font-weight:600;font-size:13px;")
        layout.addWidget(lbl)
        self.state_lbl = QLabel("…")
        self.state_lbl.setStyleSheet(
            f"color:{COLORS['text_muted']};font-size:12px;"
        )
        layout.addWidget(self.state_lbl)
        layout.addStretch()
        QTimer.singleShot(0, self.refresh)

    def refresh(self):
        state = gt_systemctl_status(self.service_name)
        color_map = {
            "active":   COLORS["success"],
            "inactive": COLORS["text_muted"],
            "failed":   COLORS["error"],
        }
        c = color_map.get(state, "#e3b341")
        self.dot.setStyleSheet(f"color:{c};")
        self.state_lbl.setText(state)
        self.state_lbl.setStyleSheet(f"color:{c};font-size:12px;")


class GtServiceControlGroup(QWidget):
    def __init__(self, service: str, display: str, description: str, parent=None):
        super().__init__(parent)
        self.service = service
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        card = Card(display)
        layout = card.body()
        layout.setSpacing(10)

        self.status = GtServiceStatusWidget(service, "Status")
        layout.addWidget(self.status)

        desc_lbl = QLabel(description)
        desc_lbl.setWordWrap(True)
        desc_lbl.setStyleSheet(
            f"color:{COLORS['text_muted']};font-size:12px;"
        )
        layout.addWidget(desc_lbl)
        layout.addWidget(Divider())

        btn_row = QHBoxLayout()
        self.btn_start   = QPushButton("▶  Start")
        self.btn_stop    = QPushButton("■  Stop")
        self.btn_restart = QPushButton("↻  Restart")
        for b in (self.btn_start, self.btn_stop, self.btn_restart):
            b.setMinimumHeight(34)
            btn_row.addWidget(b)
        layout.addLayout(btn_row)

        self.log_out = QPlainTextEdit()
        self.log_out.setReadOnly(True)
        self.log_out.setMaximumHeight(72)
        self.log_out.setPlaceholderText("Action output will appear here…")
        layout.addWidget(self.log_out)

        outer.addWidget(card)

        self.btn_start.clicked.connect(lambda: self._action("start"))
        self.btn_stop.clicked.connect(lambda: self._action("stop"))
        self.btn_restart.clicked.connect(lambda: self._action("restart"))

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.status.refresh)
        self._timer.start(4000)


    def _action(self, act: str):
        for b in (self.btn_start, self.btn_stop, self.btn_restart):
            b.setEnabled(False)
        self.log_out.setPlainText(
            f"Running systemctl {act} {self.service} …"
        )
        def on_done(ok, msg):
            self.status.refresh()
            self.log_out.setPlainText(
                ("✓ Done\n" if ok else "✗ Failed\n") + msg
            )
            for b in (self.btn_start, self.btn_stop, self.btn_restart):
                b.setEnabled(True)
        gt_run_bg(gt_systemctl_action, act, self.service, on_result=on_done)


class GtConfirmDialog(QDialog):
    def __init__(self, title: str, description: str, actions: list,
                 reversible: bool = False, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(500)
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        head = QLabel(f"<b>{title}</b>")
        head.setStyleSheet(
            f"font-size:16px;color:{COLORS['text_primary']};"
        )
        layout.addWidget(head)

        desc_lbl = QLabel(description)
        desc_lbl.setWordWrap(True)
        desc_lbl.setStyleSheet(
            f"color:{COLORS['text_primary']};font-size:13px;"
        )
        layout.addWidget(desc_lbl)
        layout.addWidget(gt_separator())

        lbl = QLabel("What this will do:")
        lbl.setStyleSheet(
            f"color:{COLORS['accent']};font-size:12px;font-weight:700;"
        )
        layout.addWidget(lbl)
        for act in actions:
            row = QLabel("  •  " + act)
            row.setWordWrap(True)
            row.setStyleSheet(
                f"color:{COLORS['text_primary']};font-size:12px;padding:2px 0;"
            )
            layout.addWidget(row)

        layout.addWidget(gt_separator())
        if reversible:
            layout.addWidget(gt_info_box("This action is reversible from this same tab."))
        else:
            layout.addWidget(gt_warn_box(
                "This action modifies system files. Review carefully before proceeding."
            ))

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("Apply Changes")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)



# ─────────────────────────────────────────────
#  CENTRALIZED LOGGER
# ─────────────────────────────────────────────

def _setup_logger() -> logging.Logger:
    """
    One logger for the whole app.
    • DEBUG+  → rotating file  (~/.local/share/griffinhub/logs/griffinhub.log)
    • WARNING+ → stderr (visible in terminal and captured by CLI)
    GUI workers additionally emit log lines via Qt signals.
    """
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOGS_DIR / "griffinhub.log"

    logger = logging.getLogger("griffinhub")
    if logger.handlers:          # already set up (e.g. re-imported in tests)
        return logger
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Rotating file handler — 1 MB × 3 files
    fh = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=1_048_576, backupCount=3, encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # Console handler for WARNING+ only
    ch = logging.StreamHandler()
    ch.setLevel(logging.WARNING)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    return logger


log = _setup_logger()


# ─────────────────────────────────────────────
#  ERROR HIERARCHY
# ─────────────────────────────────────────────
# Rule:
#   Core layer  → raises GriffinHubError subclasses (never swallows silently)
#   CLI layer   → catches → prints via _cli_err(), exits non-zero
#   GUI layer   → catches → QMessageBox.critical()




# ─────────────────────────────────────────────
#  ERROR HIERARCHY
# ─────────────────────────────────────────────
# Rule:
#   Core layer  → raises GriffinHubError subclasses (never swallows silently)
#   CLI layer   → catches → prints via _cli_err(), exits non-zero
#   GUI layer   → catches → QMessageBox.critical()

class GriffinHubError(Exception):
    """Base class for all Griffin Hub errors."""

class StoreError(GriffinHubError):
    """Raised on settings/plugin-state persistence failures."""

class ValidationError(GriffinHubError):
    """Raised on invalid user input."""

class PluginError(GriffinHubError):
    """Raised on plugin load / sandbox failures."""



class PluginRegistry:
    """
    Central registry that plugins call into.

    Griffin Hub plugins extend two things: System Tune and the Hardware Hub.
    A plugin file may define any of the following:

      PLUGIN_CHECKS   — list of diagnostic checks, e.g.
          {"id": "my_check", "name": "My Check", "cmd": ["which", "foo"],
           "category": "hardware" | "tune", "fix_hint": "Install foo via ..."}
        Shown (with a Run button) in the Plugins page, grouped by category.

      PLUGIN_ACTIONS  — list of one-click actions, e.g.
          {"id": "my_action", "name": "Enable Foo", "cmd": ["systemctl", "enable", "foo"],
           "category": "hardware" | "tune", "use_pkexec": True,
           "description": "Enables the foo service."}
        Shown as buttons in the Plugins page. use_pkexec routes the command
        through pkexec so the GUI never has to run as root.

      PLUGIN_EXTENDS  — dict of {target_name: patch} that extends a named
        extension point exposed by System Tune / Hardware Hub. See
        EXTENSION_TARGETS below for the points a plugin may extend, and
        extend() for merge rules.

      PLUGIN_METADATA — dict, e.g.
          {"version": "1.0", "api_version": 1, "author": "Your Name",
           "description": "What this plugin does",
           "trust_level": "safe"}   # "safe" (default) or "trusted"
                                     # "trusted" requires a .trusted sidecar
                                     # AND this field set — cannot be self-granted

      register(registry) — imperative hook, called at startup for trusted
        in-process plugins only. registry exposes .add_check(), .add_action(),
        and .extend().

    ⚠ Plugins execute arbitrary Python. Only install plugins you trust.
    ⚠ An AST safety scanner runs before execution and will block plugins that
      use os.system(), eval(), exec(), shell=True subprocesses, shutil.rmtree(),
      or dangerous shell-command literals (e.g. "rm -rf").

    Plugin API version history:
      1 — initial Griffin Hub release (checks, actions, extends, register hook)
    """

    PLUGIN_API_VERSION = 1   # current API version this build supports

    #: Extension points a plugin's PLUGIN_EXTENDS dict may target. Each value
    #: is the *base* state for that target; patches merge into it (see extend()).
    EXTENSION_TARGETS: dict = {
        # Extra packages offered on the System Tune → Setup tab's install list.
        "tune_setup_packages": {"packages": []},
        # Extra chips shown on the Hardware Hub overview (informational only).
        "hardware_notes":      {"notes": []},
        # Optional overrides for DistroInfo.detect() results (e.g. force a
        # package-manager command or display name). Plugins merge into this
        # via PLUGIN_EXTENDS["distro_info"] = { "pkg_manager": "...", ... }.
        "distro_info":         {},
    }

    def __init__(self):
        self.checks:  list[dict] = []
        self.actions: list[dict] = []
        self.targets: dict = copy.deepcopy(self.EXTENSION_TARGETS)

        self._errors:  list[str] = []
        self._loaded:  list[str] = []
        self._metadata: dict[str, dict] = {}  # filename -> PLUGIN_METADATA
        # Pending PLUGIN_EXTENDS patches deferred during pass 1 of load_plugins()
        self._pending_extends: list[tuple[dict, str, bool]] = []

    # ── Declarative registration API (used by both sandboxed and in-process
    #    plugins, and by the register() hook for trusted plugins) ──────────

    def add_check(self, check: dict) -> None:
        required = {"id", "name", "cmd", "category"}
        missing  = required - check.keys()
        if missing:
            self._errors.append(f"Plugin check '{check.get('id','?')}' missing keys: {missing}")
            return
        if check["category"] not in ("hardware", "tune"):
            self._errors.append(
                f"Plugin check '{check['id']}': category must be 'hardware' or 'tune'."
            )
            return
        if not isinstance(check.get("cmd"), list):
            self._errors.append(f"Plugin check '{check['id']}': 'cmd' must be a list.")
            return
        if not any(c["id"] == check["id"] for c in self.checks):
            self.checks.append(check)

    def add_action(self, action: dict) -> None:
        required = {"id", "name", "cmd", "category"}
        missing  = required - action.keys()
        if missing:
            self._errors.append(f"Plugin action '{action.get('id','?')}' missing keys: {missing}")
            return
        if action["category"] not in ("hardware", "tune"):
            self._errors.append(
                f"Plugin action '{action['id']}': category must be 'hardware' or 'tune'."
            )
            return
        if not isinstance(action.get("cmd"), list):
            self._errors.append(f"Plugin action '{action['id']}': 'cmd' must be a list.")
            return
        action.setdefault("use_pkexec", False)
        action.setdefault("description", "")
        if not any(a["id"] == action["id"] for a in self.actions):
            self.actions.append(action)

    # ── Extension API ────────────────────────────────────────────────────
    #
    # Merge rules (deterministic, no surprises):
    #   list values   — deduplicated union; plugin entries appended after base.
    #                   Identity = item["id"] for list[dict], else str.strip().
    #   dict values   — merged, plugin keys win on collision.
    #                   Restricted to trusted plugins.
    #   unknown field — warned and skipped (forward-compat guard).
    #   unknown target — warned and skipped.
    #
    # Load order (deterministic, documented):
    #   1. Plugins sorted by (priority ASC, filename ASC).
    #      priority is read from PLUGIN_METADATA["priority"] (default 100).
    #   2. All PLUGIN_CHECKS/PLUGIN_ACTIONS are registered first (pass 1).
    #   3. All PLUGIN_EXTENDS patches are applied second (pass 2), in the
    #      same sorted order — so a plugin extending a target populated by
    #      another plugin always sees it, regardless of filesystem order.

    def extend(self, target_name: str, patch: dict,
               plugin_name: str = "?", trusted: bool = False) -> bool:
        """Apply a single PLUGIN_EXTENDS patch to a named extension target."""
        if target_name not in self.targets:
            self._errors.append(
                f"Plugin '{plugin_name}': unknown PLUGIN_EXTENDS target "
                f"'{target_name}'. Known targets: {sorted(self.targets)}."
            )
            return False
        if not isinstance(patch, dict):
            self._errors.append(
                f"Plugin '{plugin_name}': PLUGIN_EXTENDS['{target_name}'] must be a dict."
            )
            return False

        base = self.targets[target_name]
        for field, value in patch.items():
            if field not in base:
                self._errors.append(
                    f"Plugin '{plugin_name}': unknown PLUGIN_EXTENDS field "
                    f"'{field}' for target '{target_name}' — ignored."
                )
                continue

            current = base[field]
            if isinstance(current, list):
                if not isinstance(value, list):
                    self._errors.append(
                        f"Plugin '{plugin_name}': '{target_name}.{field}' must be a list."
                    )
                    continue
                for item in value:
                    if isinstance(item, dict) and "id" in item:
                        if not any(isinstance(x, dict) and x.get("id") == item["id"] for x in current):
                            current.append(item)
                    else:
                        norm = item.strip() if isinstance(item, str) else item
                        if norm not in current:
                            current.append(norm)

            elif isinstance(current, dict):
                if not trusted:
                    self._errors.append(
                        f"Plugin '{plugin_name}': '{target_name}.{field}' is a dict field "
                        "and requires trust_level='trusted' in PLUGIN_METADATA."
                    )
                    continue
                if not isinstance(value, dict):
                    self._errors.append(
                        f"Plugin '{plugin_name}': '{target_name}.{field}' must be a dict."
                    )
                    continue
                current.update(value)  # plugin wins on key collision

            else:
                self._errors.append(
                    f"Plugin '{plugin_name}': '{target_name}.{field}' has an unsupported "
                    "type for PLUGIN_EXTENDS and was skipped."
                )
        return True

    def _apply_plugin_extends(self, extends: dict, plugin_name: str, trusted: bool = False):
        if not isinstance(extends, dict):
            self._errors.append(
                f"Plugin '{plugin_name}': PLUGIN_EXTENDS must be a dict "
                "of {target_name: patch}."
            )
            return
        for target_name, patch in extends.items():
            self.extend(target_name, patch, plugin_name, trusted=trusted)

    @staticmethod
    def _resolve_trust_level(metadata: dict, is_trusted_sidecar: bool) -> str:
        """
        Determine the effective trust level for a plugin.

        Rules (in priority order):
          1. Sandboxed plugins are always "safe" regardless of metadata.
          2. In-process plugins without a .trusted sidecar are capped at "safe".
          3. In-process plugins WITH a .trusted sidecar can be "trusted" if they
             declare trust_level="trusted" in PLUGIN_METADATA.
          4. All other metadata values default to "safe".
        """
        if not is_trusted_sidecar:
            return "safe"
        declared = metadata.get("trust_level", "safe")
        if declared == "trusted":
            return "trusted"
        return "safe"

    # ── AST safety scanner ─────────────────────────────────────────────────────
    # Inspects plugin source before execution to block obviously dangerous
    # constructs. Defence-in-depth, not a sandbox — users should still only
    # install plugins they trust.

    _BLOCKED_CALLS = {
        ("os", "system"),
        ("os", "popen"),
        ("os", "remove"),
        ("subprocess", "call"),
        ("subprocess", "run"),
        ("subprocess", "Popen"),
        ("subprocess", "check_call"),
        ("subprocess", "check_output"),
        ("subprocess", "getoutput"),
        ("subprocess", "getstatusoutput"),
    }
    _BLOCKED_ATTRS = {"exec", "eval", "__import__", "compile", "breakpoint"}
    _BLOCKED_LITERALS = {
        "rm -rf", "rm -r ", "sudo ",
        "dd if=", "mkfs", "> /dev/",
        "| sh", "| bash", "| python",
        "curl | ", "wget -O- ",
        "chmod +x",
    }
    _BLOCKED_OS_EXEC = {
        "execv", "execve", "execvp", "execvpe",
        "execlp", "execl", "execle",
        "system", "popen",
    }
    _BLOCKED_IMPORTLIB = {"import_module", "reload", "__import__"}

    @classmethod
    def _ast_scan_plugin(cls, source: str, filename: str) -> list[str]:
        """
        Parse plugin source and return a list of violation strings.
        Returns an empty list if the plugin appears safe to load.
        """
        violations: list[str] = []
        try:
            tree = ast.parse(source, filename=filename)
        except SyntaxError as e:
            violations.append(f"Syntax error: {e}")
            return violations

        # Pass 1: collect names that are assigned blocked callables
        tainted_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                val = node.value
                if isinstance(val, ast.Attribute):
                    mod  = val.value.id if isinstance(val.value, ast.Name) else None
                    attr = val.attr
                    if (mod, attr) in cls._BLOCKED_CALLS or (
                        mod == "os" and attr in cls._BLOCKED_OS_EXEC
                    ):
                        for target in node.targets:
                            if isinstance(target, ast.Name):
                                tainted_names.add(target.id)
                                violations.append(
                                    f"Line {node.lineno}: assignment of restricted "
                                    f"callable '{mod}.{attr}' to '{target.id}' — "
                                    "indirect call pattern blocked"
                                )

        # Pass 2: full node walk for call patterns and literals
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func

                if isinstance(func, ast.Name):
                    if func.id in cls._BLOCKED_ATTRS:
                        violations.append(
                            f"Line {node.lineno}: forbidden call '{func.id}()'"
                        )
                    elif func.id in tainted_names:
                        violations.append(
                            f"Line {node.lineno}: call to tainted name '{func.id}()' "
                            "(assigned from a restricted callable)"
                        )

                elif isinstance(func, ast.Attribute):
                    attr = func.attr
                    mod  = func.value.id if isinstance(func.value, ast.Name) else None

                    if (mod, attr) in cls._BLOCKED_CALLS:
                        violations.append(
                            f"Line {node.lineno}: forbidden call '{mod}.{attr}()' — "
                            "subprocess execution is not permitted in safe plugins"
                        )
                    elif mod == "os" and attr in cls._BLOCKED_OS_EXEC:
                        violations.append(
                            f"Line {node.lineno}: forbidden call 'os.{attr}()'"
                        )

                    if attr == "rmtree":
                        violations.append(
                            f"Line {node.lineno}: shutil.rmtree() in plugin — "
                            "use the registry API instead of direct filesystem ops"
                        )

                    if mod in ("importlib", "importlib.util") and attr in cls._BLOCKED_IMPORTLIB:
                        violations.append(
                            f"Line {node.lineno}: forbidden call "
                            f"'{mod}.{attr}()' — dynamic import bypass blocked"
                        )

                    if (
                        isinstance(func.value, ast.Call)
                        and isinstance(func.value.func, ast.Name)
                        and func.value.func.id == "__import__"
                    ):
                        violations.append(
                            f"Line {node.lineno}: __import__() chained attribute "
                            f"access '.{attr}' — dynamic import bypass blocked"
                        )

                elif isinstance(func, ast.Name) and func.id == "getattr":
                    args = node.args
                    if len(args) >= 2 and isinstance(args[1], ast.Constant):
                        target_attr = args[1].value
                        if isinstance(target_attr, str) and (
                            target_attr in cls._BLOCKED_OS_EXEC
                            or target_attr in {"system", "popen", "eval", "exec"}
                            or target_attr in cls._BLOCKED_IMPORTLIB
                        ):
                            violations.append(
                                f"Line {node.lineno}: suspicious getattr() for "
                                f"'{target_attr}' — possible obfuscation attempt"
                            )

            if isinstance(node, ast.Subscript):
                if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                    if node.slice.value in cls._BLOCKED_ATTRS:
                        violations.append(
                            f"Line {getattr(node, 'lineno', '?')}: "
                            f"subscript access to restricted built-in '{node.slice.value}'"
                        )

            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for bad in cls._BLOCKED_LITERALS:
                    if bad in node.value:
                        violations.append(
                            f"Line {getattr(node, 'lineno', '?')}: "
                            f"dangerous literal containing '{bad}'"
                        )

        return violations

    @staticmethod
    def _extract_priority(source: str) -> int:
        """
        Extract PLUGIN_METADATA["priority"] from plugin source via AST,
        without executing the file. Returns 100 (default) on any failure.
        """
        DEFAULT = 100
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return DEFAULT
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "PLUGIN_METADATA" not in targets:
                continue
            if not isinstance(node.value, ast.Dict):
                continue
            for key, val in zip(node.value.keys, node.value.values):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "priority"
                    and isinstance(val, ast.Constant)
                    and isinstance(val.value, int)
                ):
                    return val.value
        return DEFAULT

    def load_plugins(self) -> None:
        """
        Load all plugins from PLUGINS_DIR in two passes.

        Pass 1 — registration:
          For each plugin (sorted by priority ASC, filename ASC):
            • AST safety scan
            • Register PLUGIN_CHECKS, PLUGIN_ACTIONS (and call register()
              for trusted in-process plugins)
            • Defer PLUGIN_EXTENDS patches — stored in self._pending_extends

        Pass 2 — extension:
          Apply all deferred PLUGIN_EXTENDS patches in the same sort order.
        """
        PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
        raw_files = (
            list(PLUGINS_DIR.glob("*.py")) +
            list(PLUGINS_DIR.glob("*/plugin.py"))
        )
        if not raw_files:
            return

        def _sort_key(p: Path) -> tuple:
            try:
                src = p.read_text(encoding="utf-8", errors="replace")
                pri = self._extract_priority(src)
            except Exception:
                pri = 100
            return (pri, p.name)

        plugin_files = sorted(raw_files, key=_sort_key)
        log.info(
            "Loading %d plugin(s) from %s (sorted by priority, then filename)",
            len(plugin_files), PLUGINS_DIR
        )

        self._pending_extends = []
        for path in plugin_files:
            try:
                self._load_one_plugin(path, defer_extends=True)
            except Exception as e:
                msg = f"Plugin load error ({path.name}): {e}"
                log.error(msg)
                self._errors.append(msg)

        if self._pending_extends:
            log.info(
                "Applying PLUGIN_EXTENDS patches from %d plugin(s)",
                len(self._pending_extends)
            )
            for extends, plugin_name, trusted in self._pending_extends:
                self._apply_plugin_extends(extends, plugin_name, trusted=trusted)
        self._pending_extends = []

    def _load_one_plugin(self, path: Path, defer_extends: bool = False):
        """Load a single plugin file, choosing sandboxed or in-process execution."""
        source = path.read_text(encoding="utf-8", errors="replace")
        violations = self._ast_scan_plugin(source, path.name)
        if violations:
            msg = (
                f"Plugin '{path.name}' blocked by safety scanner "
                f"({len(violations)} violation(s)):\n  "
                + "\n  ".join(violations)
            )
            log.error(msg)
            self._errors.append(msg)
            return

        use_sandbox = SandboxEvaluator.should_sandbox(path)

        if use_sandbox:
            log.info("Sandbox-evaluating plugin: %s", path.name)
            try:
                exports = SandboxEvaluator.evaluate(path)
            except PluginError as e:
                msg = f"Plugin sandbox error ({path.name}): {e}"
                log.error(msg)
                self._errors.append(msg)
                return
            self._ingest_declaratives(exports, path.name, defer_extends=defer_extends)
            self._loaded.append(path.name)
            log.info("Plugin sandbox-loaded: %s", path.name)
        else:
            spec = importlib.util.spec_from_file_location(path.stem, path)
            if spec is None:
                raise PluginError(f"Could not determine loader for {path.name}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[path.stem] = module
            try:
                spec.loader.exec_module(module)
                self._ingest_module(module, path.name, defer_extends=defer_extends)
            except PluginError:
                raise
            except Exception as e:
                raise PluginError(f"Plugin '{path.name}' raised during load: {type(e).__name__}: {e}") from e

    def _ingest_declaratives(self, exports: dict, filename: str,
                             defer_extends: bool = False):
        """Ingest declarative plugin exports collected by the sandbox shim."""
        meta = exports.get("PLUGIN_METADATA", {})
        self._metadata[filename] = meta

        api_ver = meta.get("api_version", 1)
        if not isinstance(api_ver, int):
            api_ver = 1
        if api_ver > self.PLUGIN_API_VERSION:
            self._errors.append(
                f"Plugin '{filename}' api_version={api_ver} > supported "
                f"{self.PLUGIN_API_VERSION} (loaded anyway)."
            )

        meta["_effective_trust"] = "safe"  # sandboxed plugins can't self-grant trust

        for c in exports.get("PLUGIN_CHECKS", []):
            self.add_check(c)
        for a in exports.get("PLUGIN_ACTIONS", []):
            self.add_action(a)

        extends = exports.get("PLUGIN_EXTENDS", {})
        if extends:
            if defer_extends:
                self._pending_extends.append((extends, filename, False))
                log.debug("[PluginExtend] Deferred extends from %s (pass 2)", filename)
            else:
                self._apply_plugin_extends(extends, filename, trusted=False)

    def _ingest_module(self, module, filename: str, defer_extends: bool = False):
        """Ingest a fully loaded in-process plugin module (including register hook)."""
        meta = getattr(module, "PLUGIN_METADATA", {})
        self._metadata[filename] = meta

        api_ver = meta.get("api_version", 1)
        if not isinstance(api_ver, int):
            api_ver = 1
        if api_ver > self.PLUGIN_API_VERSION:
            msg = (
                f"Plugin '{filename}' requires API version {api_ver} "
                f"but this build supports up to {self.PLUGIN_API_VERSION}. "
                "Update Griffin Hub or downgrade the plugin."
            )
            log.warning(msg)
            self._errors.append(msg)

        plugin_path  = Path(module.__file__) if hasattr(module, "__file__") else None
        is_trusted   = SandboxEvaluator.plugin_is_trusted(plugin_path) if plugin_path else False
        trust_level  = self._resolve_trust_level(meta, is_trusted)
        meta["_effective_trust"] = trust_level
        is_trusted_plugin = trust_level == "trusted"

        for c in getattr(module, "PLUGIN_CHECKS", []):
            self.add_check(c)
        for a in getattr(module, "PLUGIN_ACTIONS", []):
            self.add_action(a)

        extends = getattr(module, "PLUGIN_EXTENDS", {})
        if extends:
            if defer_extends:
                self._pending_extends.append((extends, filename, is_trusted_plugin))
                log.debug("[PluginExtend] Deferred extends from %s (pass 2)", filename)
            else:
                self._apply_plugin_extends(extends, filename, trusted=is_trusted_plugin)

        if hasattr(module, "register"):
            if is_trusted_plugin:
                try:
                    module.register(self)
                except Exception as e:
                    msg = (
                        f"Plugin '{filename}' register() hook raised "
                        f"{type(e).__name__}: {e} — declarative content was loaded, hook skipped."
                    )
                    log.error(msg)
                    self._errors.append(msg)
            else:
                log.info(
                    "Plugin '%s' has a register() hook but trust_level is 'safe' — "
                    "hook not called. Add a .trusted sidecar and declare "
                    "trust_level='trusted' in PLUGIN_METADATA to enable it.",
                    filename
                )

        self._loaded.append(filename)
        log.info("Plugin in-process loaded [trust=%s]: %s", trust_level, filename)

    # ── Query helpers ────────────────────────────────────────────────────

    def checks_by_category(self, category: str) -> list[dict]:
        return [c for c in self.checks if c.get("category") == category]

    def actions_by_category(self, category: str) -> list[dict]:
        return [a for a in self.actions if a.get("category") == category]

    def apply_distro_overrides(self, info: dict) -> dict:
        """
        Merge any PLUGIN_EXTENDS['distro_info'] patches into a DistroInfo.detect()
        result. Returns a shallow copy with overrides applied; never mutates the
        input. Safe to call even when no distro plugins are loaded (no-op).
        """
        patch = self.targets.get("distro_info") or {}
        if not isinstance(patch, dict) or not patch:
            return info
        merged = dict(info)
        for key, value in patch.items():
            # Only accept known DistroInfo keys to avoid accidental pollution
            if key in (
                "id", "id_like", "name", "version_id",
                "pkg_manager", "pkg_install", "de", "compositor",
            ):
                merged[key] = value
            else:
                self._errors.append(
                    f"distro_info override ignored unknown key '{key}'"
                )
        return merged

    def load_single_plugin(self, path):
        """Load one plugin file immediately (no deferred extends). Used by the Plugin Store."""
        self._load_one_plugin(Path(path), defer_extends=False)


REGISTRY = PluginRegistry()



class _StoreBase:
    """
    Shared JSON persistence primitives used by all store classes.

    Contract:
      _load() returns {} on missing file, raises StoreError on corrupt/unreadable.
      _save() raises StoreError on write failure.
    """

    # Single lock shared across ALL store subclasses — serialises all
    # read-modify-write operations so concurrent workers cannot clobber each other.
    _lock: threading.Lock = threading.Lock()

    @staticmethod
    def _load(path: Path) -> dict:
        if not path.exists():
            return {}
        try:
            text = path.read_text(encoding="utf-8")
            if not text.strip():
                return {}
            data = json.loads(text)
            if not isinstance(data, dict):
                raise StoreError(
                    f"Data file has unexpected format (expected object, got "
                    f"{type(data).__name__}): {path.name}"
                )
            return data
        except json.JSONDecodeError as e:
            log.error("Corrupt JSON in %s: %s", path, e)
            raise StoreError(f"Data file is corrupt: {path.name}\n{e}") from e
        except OSError as e:
            log.error("Cannot read %s: %s", path, e)
            raise StoreError(f"Cannot read data file: {path.name}\n{e}") from e

    @staticmethod
    def _save(path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError as e:
            tmp.unlink(missing_ok=True)
            log.error("Cannot write %s: %s", path, e)
            raise StoreError(f"Cannot save data file: {path.name}\n{e}") from e




class SettingsStore(_StoreBase):
    """Persistence for user settings (settings.json)."""

    @staticmethod
    def load_settings() -> dict:
        defaults: dict = {
            "notifications":            True,
            "cache_dir":                str(CACHE_DIR),
            "store_url":                DEFAULT_STORE_URL,
            "store_cache_ttl":          STORE_CACHE_TTL,
            "plugin_sandbox":           True,
            "plugin_sandbox_verified":  False,
        }
        saved = _StoreBase._load(SETTINGS_FILE)
        defaults.update(saved)
        return defaults

    @staticmethod
    def save_settings(settings: dict) -> None:
        _StoreBase._save(SETTINGS_FILE, settings)



class PluginStore(_StoreBase):
    """Persistence for installed plugin metadata (plugins_state.json)."""

    _PATH: Path = BASE_DIR / "plugins_state.json"

    @classmethod
    def load_plugin_state(cls) -> dict:
        if not cls._PATH.exists():
            return {}
        try:
            return json.loads(cls._PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            log.warning("Corrupt plugin state JSON (%s): %s — returning empty.", cls._PATH, e)
            return {}
        except OSError as e:
            log.warning("Cannot read plugin state (%s): %s — returning empty.", cls._PATH, e)
            return {}

    @classmethod
    def save_plugin_state(cls, state: dict) -> None:
        tmp = cls._PATH.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
            tmp.replace(cls._PATH)
        except OSError as e:
            tmp.unlink(missing_ok=True)
            log.error("Cannot write plugin state: %s", e)
            raise StoreError(f"Cannot save plugin state: {e}") from e

    @classmethod
    def set_plugin_installed(cls, plugin_id: str, meta: dict) -> None:
        with _StoreBase._lock:
            state = cls.load_plugin_state()
            state[plugin_id] = meta
            cls.save_plugin_state(state)

    @classmethod
    def remove_plugin_record(cls, plugin_id: str) -> None:
        with _StoreBase._lock:
            state = cls.load_plugin_state()
            state.pop(plugin_id, None)
            cls.save_plugin_state(state)

    @classmethod
    def get_plugin_record(cls, plugin_id: str) -> dict | None:
        return cls.load_plugin_state().get(plugin_id)




class Store(SettingsStore, PluginStore):
    """
    Backwards-compatibility shim.

    All code can call Store.load_settings(), Store.get_plugin_record(), etc.
    without caring which specific sub-store class implements it.

      SettingsStore — user settings          (settings.json)
      PluginStore   — plugin install records (plugins_state.json)
    """
    # Inherit everything. No additional methods needed.
    pass



class ExecContext(Enum):
    SANDBOXED  = "sandboxed"
    CONTROLLED = "controlled"
    NATIVE     = "native"


# ── Executable allowlist for CONTROLLED context ───────────────────────────────
# Commands in system checks and profile post-install checks are validated
# against this set before execution. Blocks "bash -c curl | sh" style injection.

ALLOWED_CHECK_EXECUTABLES = {
    "python3", "python",
    "which", "ldconfig",
    "dpkg", "rpm", "pacman",
    "vulkaninfo", "glxinfo",
    "strings",
    "gamemoded",
    "pipewire",
    "flatpak",
    "systemctl", "sysctl",
    "cpupower", "lscpu", "lspci", "lsusb",
    "nvidia-smi", "radeontop",
    "upower", "xrandr",
    "udevadm",
    "git",
}


def validate_check_cmd(cmd: list) -> bool:
    """
    Return True if cmd[0] is in the ALLOWED_CHECK_EXECUTABLES allowlist.
    Strips any path prefix so "/usr/bin/wine" matches "wine".
    """
    if not cmd or not isinstance(cmd, list):
        return False
    exe = Path(cmd[0]).name
    return exe in ALLOWED_CHECK_EXECUTABLES


# ── Safe tarfile extraction ───────────────────────────────────────────────────

def safe_tarextract(tf, dest: Path, members=None):
    """
    Extract a tarfile to dest with path-traversal protection.

    On Python 3.12+ uses the native filter='data' which handles this and more.
    On older Pythons manually verifies every member resolves inside dest.

    Always use this instead of tf.extractall() directly.
    """
    import sys as _sys
    dest = dest.resolve()
    target_members = members if members is not None else tf.getmembers()

    # Filter out unsafe members regardless of Python version
    safe = []
    for m in target_members:
        member_path = (dest / m.name).resolve()
        if not str(member_path).startswith(str(dest)):
            log.warning("Skipping unsafe tarball path: %s", m.name)
            continue
        safe.append(m)

    py_minor = _sys.version_info.minor
    py_major = _sys.version_info.major
    if py_major > 3 or (py_major == 3 and py_minor >= 12):
        # Python 3.12+ native safe extraction
        tf.extractall(dest, members=safe, filter="data")
    else:
        tf.extractall(dest, members=safe)


# ── Plugin shim writer ────────────────────────────────────────────────────────
# The shim is a tiny script run inside the sandbox. It imports the plugin,
# collects its declarative exports, and prints them as JSON to stdout.
# The register() hook is intentionally NOT called — sandboxed plugins are
# declarative only. Locally trusted plugins (.trusted sidecar) still get the hook.

_SHIM_PATH = BASE_DIR / "_plugin_shim.py"
_SHIM_SOURCE = (
    "import sys, json, importlib.util, pathlib\n"
    "plugin_path = pathlib.Path(sys.argv[1])\n"
    "spec   = importlib.util.spec_from_file_location(plugin_path.stem, plugin_path)\n"
    "module = importlib.util.module_from_spec(spec)\n"
    "spec.loader.exec_module(module)\n"
    "output = {\n"
    "    'PLUGIN_METADATA': getattr(module, 'PLUGIN_METADATA', {}),\n"
    "    'PLUGIN_CHECKS':   getattr(module, 'PLUGIN_CHECKS',   []),\n"
    "    'PLUGIN_ACTIONS':  getattr(module, 'PLUGIN_ACTIONS',  []),\n"
    "    'PLUGIN_EXTENDS':  getattr(module, 'PLUGIN_EXTENDS',  {}),\n"
    "}\n"
    "print(json.dumps(output))\n"
)

def _ensure_shim():
    """Write the shim to disk if it doesn't exist or is stale."""
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    if not _SHIM_PATH.exists() or _SHIM_PATH.read_text() != _SHIM_SOURCE:
        try:
            _SHIM_PATH.write_text(_SHIM_SOURCE)
            # Fix #13: restrict to owner read/write only so another process on
            # the same machine cannot replace the shim with malicious code
            # that would then be executed with the user's own privileges.
            _SHIM_PATH.chmod(0o600)
        except OSError as e:
            raise PluginError(
                f"Cannot write plugin sandbox shim to {_SHIM_PATH}: {e}\n"
                "Check that the Griffin Hub data directory is writable."
            ) from e


# ── Sandbox evaluator ─────────────────────────────────────────────────────────


class SandboxEvaluator:
    """
    Evaluates a plugin in a restricted execution environment.

    Execution strategy (in priority order):
      1. bubblewrap (bwrap) — full Linux namespace isolation, no home, no net.
      2. minimal subprocess — sys.executable -S (no site-packages), stripped env.
         Used as fallback when bwrap is not installed.

    In both cases the plugin never runs in the main process. The shim collects
    declarative exports (PLUGIN_PROFILES, PLUGIN_MODULES, etc.) and returns them
    as JSON. The register() hook is NOT invoked.

    Locally-trusted plugins (manually placed .trusted sidecar file) bypass this
    evaluator entirely and are loaded in-process with full registry access,
    matching the original behaviour.
    """

    @staticmethod
    def bubblewrap_available() -> bool:
        return shutil.which("bwrap") is not None

    @staticmethod
    def plugin_is_trusted(plugin_path: Path) -> bool:
        """Return True if the user has manually marked this plugin as trusted."""
        return plugin_path.with_suffix(".trusted").exists()

    @classmethod
    def evaluate(cls, plugin_path: Path) -> dict:
        """
        Run the plugin in the sandbox and return its declarative exports as a dict.
        Raises PluginError if execution fails or output is not valid JSON.
        """
        _ensure_shim()
        cmd = [sys.executable, "-S", str(_SHIM_PATH), str(plugin_path)]

        try:
            if cls.bubblewrap_available():
                result = cls._run_bwrap(cmd, plugin_path)
            else:
                result = cls._run_subprocess(cmd, plugin_path)
        except subprocess.TimeoutExpired:
            raise PluginError(
                f"Plugin sandbox timed out after 15s for '{plugin_path.name}'. "
                "The plugin shim did not complete in time."
            )

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()[:400]
            # Surface a clear blocked-access message rather than a bare exit code
            if "Permission denied" in stderr or "No such file" in stderr:
                raise PluginError(
                    f"Sandbox blocked access for plugin '{plugin_path.name}':\n  {stderr}\n"
                    f"Tip: if this plugin needs filesystem access, create a "
                    f"'{plugin_path.stem}.trusted' sidecar file to load it in-process."
                )
            raise PluginError(
                f"Plugin sandbox exited with code {result.returncode} "
                f"for '{plugin_path.name}':\n  {stderr}"
            )
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as e:
            raise PluginError(f"Plugin shim produced invalid JSON: {e}") from e

    @staticmethod
    def _run_bwrap(cmd: list, plugin_path: Path):
        """Run cmd inside a bubblewrap container."""
        bwrap_cmd = [
            "bwrap",
            # Read-only system paths
            "--ro-bind", "/usr",   "/usr",
            "--ro-bind", "/bin",   "/bin",
            "--ro-bind", "/lib",   "/lib",
            "--ro-bind-try", "/lib64", "/lib64",
            "--ro-bind-try", "/lib32", "/lib32",
            # Tmpfs scratch space only — no home directory
            "--tmpfs", "/tmp",
            "--dir",   "/run",
            # Plugin source (read-only)
            "--ro-bind", str(plugin_path),  str(plugin_path),
            "--ro-bind", str(_SHIM_PATH),   str(_SHIM_PATH),
            # Namespace isolation
            "--unshare-net",          # no network
            "--unshare-pid",          # isolated PID space
            "--unshare-uts",          # isolated hostname
            "--die-with-parent",      # killed if Griffin Hub exits
            "--new-session",
            "--",
            *cmd,
        ]
        return subprocess.run(
            bwrap_cmd,
            capture_output=True, text=True, timeout=15,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        )

    @staticmethod
    def _run_subprocess(cmd: list, plugin_path: Path):
        """Fallback: run in a minimal subprocess without bwrap."""
        return subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=15,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        )

    @classmethod
    def should_sandbox(cls, plugin_path: Path) -> bool:
        """
        Return True if this plugin should be evaluated in the sandbox.

        Trust tiers:
          .trusted sidecar   → always in-process (user-granted trust)
          verified plugin    → respects plugin_sandbox_verified setting (default: off)
          community plugin   → respects plugin_sandbox setting (default: on)
        """
        if cls.plugin_is_trusted(plugin_path):
            log.info("Plugin '%s' is marked .trusted — skipping sandbox.", plugin_path.name)
            return False

        settings = Store.load_settings()

        # Check if this plugin is a verified store plugin.
        # dest_dir is PLUGINS_DIR / plugin_id, so parent.name gives the plugin_id
        # for store-installed plugins. Flat single-file plugins won't have a record.
        plugin_id = plugin_path.parent.name
        record = Store.get_plugin_record(plugin_id)
        is_verified = record.get("verified", False) if record else False

        if is_verified:
            return settings.get("plugin_sandbox_verified", False)
        else:
            return settings.get("plugin_sandbox", True)



class PluginStoreManager:
    """
    Manages the remote plugin store (GitHub-backed, served via jsDelivr CDN).

    Flow:
      1. fetch_index()     — downloads / serves from TTL-based disk cache
      2. search(query)     — filter by id/name/description/capability
      3. install(id)       — download → verify checksum → AST scan → register
      4. update(id)        — compare versions, re-download if newer
      5. update_all()      — check every installed plugin
      6. uninstall(id)     — delete local files + remove record

    The store URL is read from settings["store_url"] and defaults to
    DEFAULT_STORE_URL — supports private stores and mirrors.

    index.json schema (hosted on GitHub):
    {
      "schema_version": 1,
      "updated": "2025-01-01T00:00:00Z",
      "plugins": [
        {
          "id":           "my-plugin",
          "name":         "My Plugin",
          "version":      "1.0.0",
          "api_version":  1,
          "author":       "Your Name",
          "description":  "What this plugin does",
          "capabilities": ["wifi_fix"],
          "verified":     true,
          "url":          "https://cdn.jsdelivr.net/.../plugins/my-plugin/",
          "files":        ["plugin.py"],
          "checksum":     "sha256:<hex>"
        }
      ]
    }

    Plugin API deprecation strategy:
      - api_version 1: current (PLUGIN_CHECKS, PLUGIN_ACTIONS, PLUGIN_EXTENDS,
        PLUGIN_METADATA, trust_level, register() hook — see PLUGINS.md)
      - When a breaking change is needed: bump PLUGIN_API_VERSION in PluginRegistry,
        keep old behaviour behind `if api_ver < N` guards for one release cycle,
        then remove.  The store index carries api_version per plugin so the
        install step can reject incompatible ones before download.
    """

    SCHEMA_VERSION = 1

    def __init__(self):
        self._index: dict | None = None

    # ── Store URL ─────────────────────────────────────────────────────────

    @staticmethod
    def store_url() -> str:
        return Store.load_settings().get("store_url", DEFAULT_STORE_URL)

    # ── Index fetching ────────────────────────────────────────────────────

    def fetch_index(self, force: bool = False) -> dict:
        """
        Return the store index, using on-disk cache if fresh enough.
        Raises GriffinHubError on network failure with no cache available.
        force=True bypasses the TTL and always re-fetches.
        """
        if self._index is not None and not force:
            return self._index

        if not force and STORE_CACHE_FILE.exists():
            age = time.time() - STORE_CACHE_FILE.stat().st_mtime
            if age < STORE_CACHE_TTL:
                try:
                    index = json.loads(STORE_CACHE_FILE.read_text(encoding="utf-8"))
                    self._index = index
                    log.info("Plugin store index from cache (age %.0fs)", age)
                    return index
                except Exception:
                    pass

        url = self.store_url()
        log.info("Fetching plugin store index: %s", url)
        try:
            ssl_ctx = ssl.create_default_context()
            req = urllib.request.Request(
                url, headers={"User-Agent": f"GriffinHub/{APP_VERSION}"}
            )
            with urllib.request.urlopen(req, context=ssl_ctx, timeout=15) as resp:
                raw = resp.read()
        except Exception as e:
            if STORE_CACHE_FILE.exists():
                log.warning("Store fetch failed (%s); using stale cache.", e)
                try:
                    index = json.loads(STORE_CACHE_FILE.read_text(encoding="utf-8"))
                    self._index = index
                    return index
                except Exception as cache_err:
                    log.warning("Stale cache is also unreadable (%s); re-raising network error.", cache_err)
                    pass
            raise GriffinHubError(f"Could not fetch plugin store index: {e}") from e

        try:
            index = json.loads(raw)
        except json.JSONDecodeError as e:
            raise GriffinHubError(f"Plugin store index is not valid JSON: {e}") from e

        sv = index.get("schema_version", 1)
        if sv > self.SCHEMA_VERSION:
            log.warning(
                "Store schema_version=%d is newer than supported (%d). "
                "Some fields may be ignored.", sv, self.SCHEMA_VERSION,
            )

        STORE_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STORE_CACHE_FILE.with_suffix(".tmp")
        try:
            tmp.write_bytes(raw)
            tmp.replace(STORE_CACHE_FILE)
        except OSError as e:
            log.warning("Could not persist store index cache (%s) — continuing with in-memory copy.", e)
            tmp.unlink(missing_ok=True)

        self._index = index
        log.info("Plugin store index fetched: %d plugin(s)", len(index.get("plugins", [])))
        return index

    def invalidate_cache(self):
        """Force next fetch_index() to hit the network."""
        self._index = None
        STORE_CACHE_FILE.unlink(missing_ok=True)

    # ── Search ────────────────────────────────────────────────────────────

    def search(self, query: str = "", capability: str = "") -> list:
        """
        Search store index.
        query      — matches id / name / description (case-insensitive substring)
        capability — filter to plugins declaring this capability
        """
        index   = self.fetch_index()
        plugins = index.get("plugins", [])
        q       = query.lower().strip()
        results = []
        for p in plugins:
            if capability and capability not in p.get("capabilities", []):
                continue
            if q:
                haystack = " ".join([
                    p.get("id", ""), p.get("name", ""), p.get("description", "")
                ]).lower()
                if q not in haystack:
                    continue
            results.append(p)
        return results

    def get_plugin_entry(self, plugin_id: str) -> dict | None:
        index = self.fetch_index()
        return next(
            (p for p in index.get("plugins", []) if p["id"] == plugin_id), None
        )

    # ── Install ───────────────────────────────────────────────────────────

    def install(self, plugin_id: str, progress_cb=None) -> tuple:
        """
        Download, verify, scan, and install a plugin from the store.
        progress_cb(pct: int, msg: str) — optional progress callback.
        Returns (success: bool, message: str).
        """
        def _prog(pct, msg):
            log.info("[store] %d%% %s", pct, msg)
            if progress_cb:
                progress_cb(pct, msg)

        entry = self.get_plugin_entry(plugin_id)
        if entry is None:
            return False, f"Plugin '{plugin_id}' not found in store."

        api_ver = entry.get("api_version", 1)
        if api_ver > PluginRegistry.PLUGIN_API_VERSION:
            return False, (
                f"Plugin '{plugin_id}' requires API version {api_ver} "
                f"but this build supports up to {PluginRegistry.PLUGIN_API_VERSION}. "
                "Update Griffin Hub to install this plugin."
            )

        base_url = entry.get("url", "").rstrip("/")
        files    = entry.get("files", ["plugin.py"])
        if not base_url:
            return False, f"Plugin '{plugin_id}' has no download URL."

        dest_dir = PLUGINS_DIR / plugin_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        downloaded = []

        _prog(5, f"Downloading {plugin_id}…")

        is_verified = entry.get("verified", False)
        _settings   = Store.load_settings()
        if is_verified:
            _sandbox_on   = _settings.get("plugin_sandbox_verified", False)
            _sandbox_note = "enabled" if _sandbox_on else "optional (recommended: off)"
        else:
            _sandbox_on   = _settings.get("plugin_sandbox", True)
            _sandbox_note = (
                "enabled by default"
                if _sandbox_on
                else "disabled (use 'griffinhub settings set plugin_sandbox true' to re-enable)"
            )
        _verified_label = "verified" if is_verified else "unverified"
        _prog(5, f"{entry.get('name', plugin_id)} ({_verified_label})")
        _prog(5, f"Sandbox: {_sandbox_note}")
        ssl_ctx = ssl.create_default_context()
        for i, fname in enumerate(files):
            file_url  = f"{base_url}/{fname}"
            file_dest = dest_dir / fname
            _prog(5 + int(i / len(files) * 50), f"Fetching {fname}…")
            try:
                req = urllib.request.Request(
                    file_url, headers={"User-Agent": f"GriffinHub/{APP_VERSION}"}
                )
                with urllib.request.urlopen(req, context=ssl_ctx, timeout=30) as resp:
                    file_dest.write_bytes(resp.read())
                downloaded.append(file_dest)
            except Exception as e:
                for f in downloaded:
                    f.unlink(missing_ok=True)
                shutil.rmtree(dest_dir, ignore_errors=True)
                return False, f"Download failed for '{fname}': {e}"

        _prog(60, "Verifying checksum…")
        expected = entry.get("checksum", "")
        if expected:
            ok, msg = self._verify_checksum(dest_dir, files, expected)
            if not ok:
                for f in downloaded:
                    f.unlink(missing_ok=True)
                shutil.rmtree(dest_dir, ignore_errors=True)
                return False, f"Checksum mismatch for '{plugin_id}': {msg}"

        _prog(70, "Running safety scan…")
        for py_file in dest_dir.glob("*.py"):
            source     = py_file.read_text(encoding="utf-8", errors="replace")
            violations = PluginRegistry._ast_scan_plugin(source, py_file.name)
            if violations:
                for f in downloaded:
                    f.unlink(missing_ok=True)
                shutil.rmtree(dest_dir, ignore_errors=True)
                return False, (
                    f"Plugin '{plugin_id}' blocked by safety scanner "
                    f"({len(violations)} violation(s)):\n  " + "\n  ".join(violations)
                )

        _prog(85, "Registering plugin…")
        main_py  = dest_dir / "plugin.py"
        if not main_py.exists():
            py_files = list(dest_dir.glob("*.py"))
            main_py  = py_files[0] if py_files else None
        live_load_ok = True
        if main_py:
            try:
                REGISTRY.load_single_plugin(main_py)
            except Exception as e:
                # Bug #18 fix: previously swallowed and still recorded success.
                # Now surface the error and do NOT record the plugin as installed.
                log.error("Plugin '%s' live-load error: %s", plugin_id, e)
                live_load_ok = False
                shutil.rmtree(dest_dir, ignore_errors=True)
                return False, (
                    f"Plugin '{entry.get('name', plugin_id)}' downloaded but failed to load: "
                    f"{type(e).__name__}: {e}\n"
                    "Files have been removed. Check the log for details."
                )

        Store.set_plugin_installed(plugin_id, {
            "id":           plugin_id,
            "name":         entry.get("name", plugin_id),
            "version":      entry.get("version", "0.0.0"),
            "api_version":  api_ver,
            "author":       entry.get("author", ""),
            "verified":     entry.get("verified", False),
            "installed_at": datetime.now().isoformat(),
            "source_url":   base_url,
            "files":        files,
            "dest_dir":     str(dest_dir),
        })

        _prog(100, "Done.")
        log.info("Plugin '%s' v%s installed.", plugin_id, entry.get("version", "?"))
        return True, f"Plugin '{entry.get('name', plugin_id)}' installed successfully."

    # ── Update ────────────────────────────────────────────────────────────

    def update(self, plugin_id: str, progress_cb=None) -> tuple:
        """
        Compare local version against store and re-install if newer.
        Returns (success, message). Already-up-to-date is success=True.

        Bug #17 fix: previously deleted old plugin files before calling install(),
        with no rollback if install() failed — leaving the user with no plugin at
        all. Now we rename the old files to a .bak location before deleting, attempt
        the install, and restore from backup if it fails.
        """
        record = Store.get_plugin_record(plugin_id)
        if record is None:
            return False, f"Plugin '{plugin_id}' is not installed."
        entry = self.get_plugin_entry(plugin_id)
        if entry is None:
            return False, f"Plugin '{plugin_id}' not found in store."
        local_ver  = record.get("version", "0.0.0")
        remote_ver = entry.get("version", "0.0.0")
        if not self._version_newer(remote_ver, local_ver):
            return True, f"Plugin '{plugin_id}' is already up to date (v{local_ver})."
        log.info("Updating plugin '%s': %s → %s", plugin_id, local_ver, remote_ver)

        # Backup old files before removing them
        backup_dir  = PLUGINS_DIR / f".update_backup_{plugin_id}"
        old_dest    = record.get("dest_dir", "")
        old_flat    = PLUGINS_DIR / f"{plugin_id}.py"
        backed_up   = False
        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
            if old_dest and Path(old_dest).exists():
                shutil.copytree(old_dest, backup_dir / "dest_dir", dirs_exist_ok=True)
            if old_flat.exists():
                shutil.copy2(old_flat, backup_dir / f"{plugin_id}.py")
            backed_up = True
        except Exception as e:
            log.warning("Plugin update: could not back up '%s' before update: %s", plugin_id, e)

        self._remove_files(plugin_id, record)
        ok, msg = self.install(plugin_id, progress_cb=progress_cb)
        if not ok and backed_up:
            log.warning("Plugin update failed for '%s' — restoring from backup.", plugin_id)
            try:
                # Restore old files
                if (backup_dir / "dest_dir").exists():
                    dest_target = Path(old_dest)
                    if dest_target.exists():
                        shutil.rmtree(dest_target, ignore_errors=True)
                    shutil.copytree(backup_dir / "dest_dir", dest_target)
                bak_flat = backup_dir / f"{plugin_id}.py"
                if bak_flat.exists():
                    shutil.copy2(bak_flat, old_flat)
                log.info("Plugin '%s' restored from backup after failed update.", plugin_id)
                msg += " (previous version restored)"
            except Exception as re:
                log.error("Plugin update rollback failed for '%s': %s", plugin_id, re)
                msg += " (rollback also failed — plugin may be in broken state)"
        # Clean up backup regardless
        try:
            if backup_dir.exists():
                shutil.rmtree(backup_dir, ignore_errors=True)
        except Exception:
            pass
        return ok, msg

    def update_all(self, progress_cb=None) -> list:
        """Check and update all installed plugins. Returns list of (id, ok, msg)."""
        results = []
        for pid in list(Store.load_plugin_state().keys()):
            ok, msg = self.update(pid, progress_cb=progress_cb)
            results.append((pid, ok, msg))
        return results

    # ── Uninstall ─────────────────────────────────────────────────────────

    def uninstall(self, plugin_id: str) -> tuple:
        """Remove plugin files and registry record."""
        record = Store.get_plugin_record(plugin_id)
        if record is None:
            return False, f"Plugin '{plugin_id}' is not recorded as installed."
        self._remove_files(plugin_id, record)
        Store.remove_plugin_record(plugin_id)
        log.info("Plugin '%s' uninstalled.", plugin_id)
        return True, f"Plugin '{plugin_id}' uninstalled."

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _verify_checksum(dest_dir, files: list, expected: str) -> tuple:
        """
        Verify downloaded files against expected checksum.
        Format: "sha256:<hex>" — all files sorted by name are concatenated.
        """
        if not expected.startswith("sha256:"):
            log.warning("Unknown checksum algorithm: %s", expected)
            return True, "skipped (unknown algorithm)"
        expected_hex = expected[7:].lower()
        h = hashlib.sha256()
        for fname in sorted(files):
            fpath = dest_dir / fname
            if fpath.exists():
                h.update(fpath.read_bytes())
        got = h.hexdigest()
        if got != expected_hex:
            return False, f"expected {expected_hex[:16]}… got {got[:16]}…"
        return True, "ok"

    @staticmethod
    def _version_newer(candidate: str, current: str) -> bool:
        """Simple semver comparison: "1.2.3" > "1.2.0"."""
        def _parts(v):
            parts = []
            for seg in v.lstrip("v").split(".")[:3]:
                try:
                    parts.append(int(seg))
                except ValueError:
                    parts.append(0)
            return tuple(parts)
        return _parts(candidate) > _parts(current)

    @staticmethod
    def _remove_files(plugin_id: str, record: dict):
        dest_dir = record.get("dest_dir", "")
        if dest_dir:
            d = Path(dest_dir)
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
        flat = PLUGINS_DIR / f"{plugin_id}.py"
        if flat.exists():
            flat.unlink(missing_ok=True)


# Global store manager instance
STORE = PluginStoreManager()


class HardwareProbe:
    """
    Runtime hardware / environment detection backing the Hardware Hub —
    desktop or laptop, single or multi-GPU, NVIDIA/AMD/Intel, discrete or
    integrated — rather than assuming one specific device.

    The GPU vendor / Mesa-version / RADV-flag detection works on any
    AMD/NVIDIA/Intel system, and includes detection for multiple GPUs per
    machine (laptop hybrid graphics), which GPU is discrete vs. integrated,
    and whether the machine is a laptop or a desktop at all.

    All methods are classmethods and cache their results on the class so
    repeated calls within a session are cheap. Results are intentionally
    never persisted to disk — we re-probe on every Griffin Hub launch so
    that a kernel, driver, or Mesa upgrade is picked up automatically.

    All detection is purely passive (reads /sys, /proc, runs read-only
    CLI tools) and never modifies system state.
    """

    # Per-method lru_cache replaces the old shared _cache dict.
    # Benefits: thread-safe reads (no compound check-then-set race), automatic
    # per-method invalidation via cache_clear(), no shared mutable class state.
    # build_env() and steam_compat_env() take arguments so they keep their own
    # simple cache pattern rather than lru_cache.

    # ── GPU / driver detection ────────────────────────────────────────────────

    # PCI vendor IDs as reported under /sys/class/drm/*/device/vendor.
    _PCI_VENDOR_IDS = {
        "0x1002": "amd",
        "0x10de": "nvidia",
        "0x8086": "intel",
        "0x8087": "intel",
    }

    @classmethod
    @functools.lru_cache(maxsize=None)
    def gpus(cls) -> tuple[dict, ...]:
        """
        Enumerate every GPU on the system, not just the first one found.

        This matters most on laptops, which very commonly have BOTH an
        integrated GPU (Intel/AMD APU graphics) and a discrete GPU
        (NVIDIA/AMD) wired up via hybrid graphics (Optimus / PRIME). The
        single-GPU detection this replaced would silently report whichever
        card happened to sort first under /sys/class/drm, which on a hybrid
        laptop is not necessarily the one that actually matters for gaming
        performance.

        Returns a tuple of dicts, each shaped:
            {
                "drm_card":   "card0",          # /sys/class/drm/<name>
                "vendor":     "nvidia",          # 'amd' | 'nvidia' | 'intel' | 'unknown'
                "pci_id":     "0x10de",
                "discrete":   True,              # see _classify_discrete()
                "boot_vga":   False,              # True = this is the GPU the
                                                   # firmware boots the display on
            }
        Ordered with boot_vga GPUs first, then by drm_card name. Empty tuple
        if no GPU could be enumerated at all (detection failure, not "no
        GPU exists" — every PC has a GPU).
        """
        found: list[dict] = []
        try:
            drm_root = Path("/sys/class/drm")
            if drm_root.exists():
                for card in sorted(drm_root.iterdir()):
                    # Only top-level cardN entries, not cardN-HDMI-A-1 etc.
                    if not re.match(r"^card\d+$", card.name):
                        continue
                    device_dir = card / "device"
                    vendor_file = device_dir / "vendor"
                    if not vendor_file.exists():
                        continue
                    try:
                        pci_id = vendor_file.read_text().strip().lower()
                    except OSError:
                        continue
                    vendor = cls._PCI_VENDOR_IDS.get(pci_id, "unknown")
                    boot_vga = False
                    boot_vga_file = device_dir / "boot_vga"
                    if boot_vga_file.exists():
                        try:
                            boot_vga = boot_vga_file.read_text().strip() == "1"
                        except OSError:
                            pass
                    found.append({
                        "drm_card": card.name,
                        "vendor": vendor,
                        "pci_id": pci_id,
                        "boot_vga": boot_vga,
                    })
        except Exception:
            pass

        # Fallback: lspci text parsing if /sys/class/drm gave us nothing
        # (e.g. inside some containers, or a very unusual driver setup).
        if not found:
            try:
                out = subprocess.check_output(
                    ["lspci", "-mm"], stderr=subprocess.DEVNULL, timeout=4
                ).decode(errors="replace")
                idx = 0
                for line in out.splitlines():
                    low = line.lower()
                    if not any(k in low for k in ("vga", "3d controller", "display controller")):
                        continue
                    if "advanced micro devices" in low or "radeon" in low:
                        vendor = "amd"
                    elif "nvidia" in low:
                        vendor = "nvidia"
                    elif "intel" in low:
                        vendor = "intel"
                    else:
                        vendor = "unknown"
                    found.append({
                        "drm_card": f"lspci{idx}",
                        "vendor": vendor,
                        "pci_id": "",
                        "boot_vga": idx == 0,  # best guess: lspci's first hit
                    })
                    idx += 1
            except Exception:
                pass

        # Classify discrete vs. integrated now that we know how many GPUs
        # there are in total — a single AMD GPU on a desktop is "discrete"
        # even though the same chip family appears integrated in a laptop
        # APU, so the classification needs the full picture, not one card
        # in isolation.
        for gpu in found:
            gpu["discrete"] = cls._classify_discrete(gpu, found)

        found.sort(key=lambda g: (not g["boot_vga"], g["drm_card"]))
        return tuple(found)

    @classmethod
    def _classify_discrete(cls, gpu: dict, all_gpus: list) -> bool:
        """
        Best-effort discrete-vs-integrated classification for one GPU dict,
        given the full list of GPUs found on this machine.

        There is no single universal flag for this in sysfs, so this uses
        the same heuristics real-world tools (e.g. switcheroo-control,
        bolt) rely on:
          • Intel and most AMD APU graphics are near-always integrated.
          • NVIDIA is near-always discrete (NVIDIA does not currently ship
            integrated desktop/laptop GPUs in any configuration Griffin Hub
            is likely to run on).
          • AMD is ambiguous on its own — Ryzen APUs have integrated AMD
            graphics, but AMD also makes plenty of discrete cards. If a
            machine has exactly one GPU and it's AMD, check whether a
            battery is present (battery present + single AMD GPU strongly
            suggests an APU-integrated GPU in a laptop) as a tie-breaker.
            If a machine has multiple GPUs and one of them is AMD while
            another is NVIDIA or Intel, the AMD one is almost always the
            integrated half of a hybrid setup UNLESS it's the boot_vga
            device on a machine with no Intel/integrated sibling — in that
            ambiguous remainder case, this defaults to calling it discrete
            since that is the far more common desktop configuration.
        """
        vendor = gpu["vendor"]
        if vendor == "intel":
            return False
        if vendor == "nvidia":
            return True
        if vendor == "amd":
            other_vendors = {g["vendor"] for g in all_gpus if g is not gpu}
            if "intel" in other_vendors or "nvidia" in other_vendors:
                # Hybrid system with a non-AMD sibling — the AMD card here
                # is overwhelmingly likely to be the discrete half if a
                # discrete vendor (NVIDIA) is present, or the integrated
                # half if Intel is the other GPU and this AMD card is not
                # boot_vga. Use boot_vga as the deciding signal.
                if "nvidia" in other_vendors:
                    return True
                return bool(gpu.get("boot_vga"))
            if len(all_gpus) == 1 and cls.has_battery():
                # Single AMD GPU + battery present = very likely a laptop
                # APU's integrated graphics, not a discrete card (discrete
                # AMD laptop GPUs exist but are far rarer than APU-only
                # laptops).
                return False
            return True
        return False  # unknown vendor — don't claim a classification

    @classmethod
    def primary_gpu(cls) -> dict | None:
        """
        Return the single GPU Griffin Hub should optimise for, or None if no
        GPU could be detected at all.

        Preference order: discrete GPU first (it's almost always what you
        want games rendering on), falling back to whichever GPU is
        boot_vga, falling back to the first GPU found at all. This is the
        same preference order distro tools like switcheroo-control or
        Optimus-style "run on discrete GPU" launchers use, and matches what
        a person manually checking `lspci` would pick for gaming.
        """
        all_gpus = cls.gpus()
        if not all_gpus:
            return None
        for gpu in all_gpus:
            if gpu.get("discrete"):
                return gpu
        return all_gpus[0]  # already sorted boot_vga-first

    @classmethod
    @functools.lru_cache(maxsize=None)
    def gpu_vendor(cls) -> str:
        """
        Return 'amd', 'nvidia', 'intel', or 'unknown' for the PRIMARY GPU
        (see primary_gpu() for the selection rule).

        Kept as a simple string-returning method for every existing call
        site that only cares about one vendor name. New code that needs to
        know about multiple GPUs (laptops with hybrid graphics) should call
        gpus() or primary_gpu() directly instead.
        """
        gpu = cls.primary_gpu()
        return gpu["vendor"] if gpu else "unknown"

    @classmethod
    @functools.lru_cache(maxsize=None)
    def is_radv(cls) -> bool:
        """Return True if the RADV Vulkan driver is active (AMD open-source)."""
        result = False
        try:
            out = subprocess.check_output(
                ["vulkaninfo", "--summary"], stderr=subprocess.STDOUT, timeout=6
            ).decode(errors="replace").lower()
            result = "radv" in out
        except Exception:
            pass
        return result

    # ── Mesa version detection ────────────────────────────────────────────────

    @classmethod
    @functools.lru_cache(maxsize=None)
    def mesa_version(cls) -> tuple[int, int, int]:
        """
        Return Mesa version as (major, minor, patch).
        Returns (0, 0, 0) if Mesa cannot be detected.

        SteamOS fallback: glxinfo/vulkaninfo may not be in PATH when running
        under Flatpak Steam or in a minimal Desktop Mode session. Falls back
        to reading /run/host/os-release (host info from inside a container)
        and /etc/os-release for a MESA_VERSION= line.
        """
        ver = (0, 0, 0)
        # Primary: glxinfo -B
        try:
            out = subprocess.check_output(
                ["glxinfo", "-B"], stderr=subprocess.DEVNULL, timeout=5
            ).decode(errors="replace")
            m = re.search(r"Mesa\s+(\d+)\.(\d+)\.?(\d*)", out)
            if m:
                ver = (int(m.group(1)), int(m.group(2)), int(m.group(3)) if m.group(3) else 0)
        except Exception:
            pass
        # SteamOS fallback: check os-release files for MESA_VERSION
        if ver == (0, 0, 0):
            for candidate in [
                Path("/run/host/os-release"),   # host info from inside Flatpak/container
                Path("/etc/os-release"),
            ]:
                try:
                    text = candidate.read_text()
                    m = re.search(r'MESA_VERSION="?(\d+)\.(\d+)\.?(\d*)', text)
                    if m:
                        ver = (int(m.group(1)), int(m.group(2)),
                               int(m.group(3)) if m.group(3) else 0)
                        break
                except Exception:
                    pass
        return ver

    @classmethod
    @functools.lru_cache(maxsize=None)
    def radv_perftest_flags(cls) -> str | None:
        """
        Return a safe RADV_PERFTEST value for the detected Mesa version,
        or None if no flags should be set.

        History:
          • mesa < 22.0  — gpl not supported; nosam not relevant
          • mesa 22.0–24.x — gpl useful (graphics pipeline library); nosam useful
            on systems without Resizable BAR
          • mesa >= 25.0  — gpl is on by default; nosam is deprecated/no-op
            Setting stale flags on new Mesa is harmless but noisy in logs,
            so we suppress them.

        We intentionally err on the side of omitting flags rather than
        applying ones that might conflict with Proton's own Mesa tuning.
        """
        ver = cls.mesa_version()
        major, minor = ver[0], ver[1]
        flags: list[str] = []
        if major == 0:
            # Could not detect Mesa — don't touch RADV_PERFTEST at all
            result = None
        elif major >= 25:
            # Both flags are default or removed in Mesa 25+
            result = None
        elif major >= 22:
            # gpl: graphics pipeline libraries — reduces stutter; safe on 22–24
            flags.append("gpl")
            # nosam: only helpful when Resizable BAR is absent. We omit it
            # by default — Resizable BAR support varies a lot across
            # desktop/laptop motherboards and GPUs, and setting nosam on a
            # system that DOES have Resizable BAR working correctly can
            # hurt more than it helps. Proton's own BAR detection is a
            # better-informed source of truth than a blanket flag here.
            result = ",".join(flags) if flags else None
        else:
            # Pre-22 Mesa — neither flag is safe
            result = None
        return result

    # ── Gamescope environment detection ────────────────────────────────────────
    #
    # Gamescope is Valve's micro-compositor. It originated on the Steam Deck
    # but runs fine standalone on any desktop Linux system as an optional
    # launch wrapper (see LAUNCH_WRAPPER_PRESETS) — detecting "are we running
    # inside it right now" is general-purpose, unlike detecting specific
    # handheld hardware, which this class no longer does (see class docstring).

    @classmethod
    @functools.lru_cache(maxsize=None)
    def is_gamescope(cls) -> bool:
        """Return True if Griffin Hub is running inside a Gamescope session."""
        # Bug #14 fix: original code used a ternary that only guarded the last
        # or-clause, causing an uncaught exception if /proc/1/comm was unreadable.
        # Rewritten as explicit if/elif to be unambiguous and fully exception-safe.
        result = False
        try:
            if os.environ.get("GAMESCOPE_WAYLAND_DISPLAY") is not None:
                result = True
            elif os.environ.get("XDG_CURRENT_DESKTOP", "").lower() == "gamescope":
                result = True
            elif Path("/proc/1/comm").exists():
                result = Path("/proc/1/comm").read_text().strip() == "gamescope"
        except Exception:
            pass
        return result

    @classmethod
    @functools.lru_cache(maxsize=None)
    def gamescope_resolution(cls) -> tuple[int, int] | None:
        """
        Return (width, height) of the current Gamescope output, or None.
        Used to tune FSR sharpness / resolution hints.
        """
        res = None
        try:
            w = int(os.environ.get("GAMESCOPE_OUTPUT_WIDTH",  0))
            h = int(os.environ.get("GAMESCOPE_OUTPUT_HEIGHT", 0))
            if w > 0 and h > 0:
                res = (w, h)
        except Exception:
            pass
        return res

    # ── CPU topology ─────────────────────────────────────────────────────────

    @classmethod
    @functools.lru_cache(maxsize=None)
    def cpu_core_count(cls) -> int:
        """Return the number of physical CPU cores (not hyperthreads)."""
        count = 0
        try:
            cpuinfo = Path("/proc/cpuinfo").read_text()
            cores   = set(re.findall(r"^core id\s*:\s*(\d+)", cpuinfo, re.M))
            count   = len(cores)
        except Exception:
            pass
        if count == 0:
            count = os.cpu_count() or 4
        return count

    @classmethod
    @functools.lru_cache(maxsize=None)
    def safe_cpu_topology(cls) -> str | None:
        """
        Return a WINE_CPU_TOPOLOGY value only when it is safe and beneficial,
        or None to leave the variable unset (let Proton/Wine decide).

        LIMITATION, stated plainly rather than worked around: WINE_CPU_TOPOLOGY
        needs to be EXACT (real core count : real threads-per-core) to help at
        all — a wrong value can hurt performance more than leaving it unset.
        That was previously only considered "known" for one specific handheld's
        APU, which is exactly the kind of single-device hardcoding this class
        no longer does (see the class docstring). There is no reliable,
        general way to determine real physical-core/thread topology that's
        worth automatically overriding what Proton/Wine already infers from
        the kernel on arbitrary desktop and laptop hardware, so this
        intentionally always leaves WINE_CPU_TOPOLOGY unset now. If you have
        a specific CPU where you've confirmed a topology override helps, set
        WINE_CPU_TOPOLOGY yourself in that app's Launch Options — Griffin Hub
        will never overwrite a value you've set there (see the check below).
        """
        # Honour any existing user override
        if "WINE_CPU_TOPOLOGY" in os.environ:
            return None
        return None

    # ── Chassis / form factor detection ───────────────────────────────────────

    @classmethod
    @functools.lru_cache(maxsize=None)
    def has_battery(cls) -> bool:
        """
        Return True if the system reports a battery in /sys/class/power_supply.

        This is the standard, distro-agnostic signal `upower`, GNOME, and KDE
        all use to decide "is this a laptop" — it works the same way on every
        systemd/udev-based distro (Ubuntu, Kubuntu, Fedora, etc.) without
        needing a DMI chassis-type lookup, which is the fallback below.
        """
        try:
            power_root = Path("/sys/class/power_supply")
            if not power_root.exists():
                return False
            for supply in power_root.iterdir():
                type_file = supply / "type"
                if type_file.exists() and type_file.read_text().strip() == "Battery":
                    return True
        except Exception:
            pass
        return False

    @classmethod
    @functools.lru_cache(maxsize=None)
    def chassis_type(cls) -> str:
        """
        Return 'laptop', 'desktop', or 'unknown'.

        Primary signal: battery presence (has_battery()) — reliable on
        virtually every real laptop and desktop, and doesn't depend on the
        motherboard vendor having filled in DMI fields correctly (a lot of
        desktop boards leave chassis-type blank or wrong).

        Secondary signal, used only if no battery was found (some desktop
        replacement laptops are always plugged in with battery removed, and
        some thin clients/NUCs report no battery despite being small-form
        desktops — DMI chassis_type catches a few more cases here):
        /sys/class/dmi/id/chassis_type, per the SMBIOS spec — 8, 9, 10, 11,
        14 are laptop/notebook/sub-notebook/handheld variants; 3, 4, 5, 6, 7,
        13, 23 are desktop/tower/server variants.
        """
        if cls.has_battery():
            return "laptop"
        try:
            raw = Path("/sys/class/dmi/id/chassis_type").read_text().strip()
            code = int(raw)
            if code in (8, 9, 10, 11, 14, 30, 31, 32):
                return "laptop"
            if code in (3, 4, 5, 6, 7, 13, 15, 16, 17, 23, 24):
                return "desktop"
        except Exception:
            pass
        return "unknown"

    @classmethod
    def is_laptop(cls) -> bool:
        """Convenience boolean wrapper around chassis_type()."""
        return cls.chassis_type() == "laptop"

    # ── Power profile ─────────────────────────────────────────────────────────

    @classmethod
    @functools.lru_cache(maxsize=None)
    def power_profile_cmd(cls) -> list[str] | None:
        """
        Return a command to set a performance power profile before launch,
        or None if no power management tool is available.

        Priority: powerprofilesctl (freedesktop standard) > tuned-adm >
                  ryzenadj (Deck-specific, requires root — skip by default)
        """
        cmd = None
        if shutil.which("powerprofilesctl"):
            cmd = ["powerprofilesctl", "set", "performance"]
        elif shutil.which("tuned-adm"):
            cmd = ["tuned-adm", "profile", "latency-performance"]
        return cmd


    @classmethod
    def reset_cache(cls) -> None:
        """
        Clear all probe caches — useful in tests or after a system change
        (e.g. plugging in a GPU, docking/undocking a laptop, upgrading Mesa).

        Calls cache_clear() on each @functools.lru_cache-decorated method.
        steam_compat_env uses a manual cache dict and is reset separately.
        """
        for method in (
            cls.gpus, cls.gpu_vendor, cls.is_radv, cls.mesa_version,
            cls.radv_perftest_flags, cls.is_gamescope,
            cls.gamescope_resolution, cls.cpu_core_count,
            cls.safe_cpu_topology, cls.has_battery, cls.chassis_type,
            cls.power_profile_cmd,
        ):
            method.cache_clear()
        # Clear manual cache on steam_compat_env
        if hasattr(cls, '_steam_compat_cache'):
            cls._steam_compat_cache = {}



class SystemIntegration:
    """
    Capability-based abstraction for OS-level actions (open file manager, open URL).

    Resolution order for open_file_manager:
      1. User override  — settings["file_manager"] if set
      2. DE-specific    — detected from $XDG_CURRENT_DESKTOP / $DESKTOP_SESSION
      3. xdg-open       — universal fallback (works on GNOME, KDE, XFCE, most DEs)

    Plugins and UI code should always call these methods rather than
    shelling out to xdg-open directly, so sandbox interactions and user
    overrides are handled consistently throughout the app.
    """

    _DE_FILE_MANAGERS = {
        "gnome":   "nautilus",
        "kde":     "dolphin",
        "xfce":    "thunar",
        "lxde":    "pcmanfm",
        "lxqt":    "pcmanfm-qt",
        "mate":    "caja",
        "budgie":  "nautilus",
        "cinnamon": "nemo",
    }

    @classmethod
    def _detect_de(cls) -> str:
        de = (
            os.environ.get("XDG_CURRENT_DESKTOP", "") or
            os.environ.get("DESKTOP_SESSION", "")
        ).lower()
        for key in cls._DE_FILE_MANAGERS:
            if key in de:
                return key
        return ""

    @classmethod
    def _file_manager_cmd(cls) -> list[str]:
        """Return the best available file manager command as a list."""
        # 1. User override
        settings = Store.load_settings()
        override = settings.get("file_manager", "").strip()
        if override and shutil.which(override):
            return [override]

        # 2. DE-specific fallback
        de  = cls._detect_de()
        cmd = cls._DE_FILE_MANAGERS.get(de, "")
        if cmd and shutil.which(cmd):
            return [cmd]

        # 3. xdg-open (universal)
        return ["xdg-open"]

    @classmethod
    def open_file_manager(cls, path) -> tuple[bool, str]:
        """
        Open *path* in the user's file manager.
        Returns (ok, error_message).  Always runs outside the sandbox.
        """
        cmd = cls._file_manager_cmd() + [str(path)]
        try:
            subprocess.Popen(cmd)
            return True, ""
        except FileNotFoundError:
            return False, f"File manager not found: {cmd[0]}"
        except Exception as e:
            return False, f"Could not open file manager: {e}"

    @classmethod
    def open_url(cls, url: str) -> tuple[bool, str]:
        """Open a URL in the default browser via xdg-open."""
        try:
            subprocess.Popen(["xdg-open", url])
            return True, ""
        except FileNotFoundError:
            return False, "xdg-open not found — cannot open URL."
        except Exception as e:
            return False, f"Could not open URL: {e}"




class GlowButton(QPushButton):
    """Accent-coloured button with hover glow. Pass `accent=` to override the
    theme accent color for a single button (e.g. a severity-colored action)."""

    def __init__(self, text, variant="primary", parent=None, accent: str | None = None):
        super().__init__(text, parent)
        self.variant = variant
        self._accent = accent
        self._apply_style()
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))

    def _apply_style(self):
        a      = self._accent or COLORS['accent']
        a_dim  = self._accent or COLORS['accent_dim']
        a_glow = self._accent or COLORS['accent_glow']
        if self.variant == "primary":
            self.setStyleSheet(f"""
                QPushButton {{
                    background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                        stop:0 {a_dim}, stop:1 {a});
                    color: white;
                    border: none;
                    border-radius: 8px;
                    padding: 10px 24px;
                    font-size: 13px;
                    font-weight: bold;
                    letter-spacing: 0.5px;
                }}
                QPushButton:hover {{
                    background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                        stop:0 {a}, stop:1 {a_glow});
                }}
                QPushButton:pressed {{
                    background: {a_dim};
                }}
                QPushButton:disabled {{
                    background: {COLORS['bg_card']};
                    color: {COLORS['text_muted']};
                }}
            """)
        elif self.variant == "secondary":
            self.setStyleSheet(f"""
                QPushButton {{
                    background-color: transparent;
                    color: {a};
                    border: 1px solid {a_dim};
                    border-radius: 8px;
                    padding: 10px 24px;
                    font-size: 13px;
                    font-weight: bold;
                }}
                QPushButton:hover {{
                    background-color: {a_dim}22;
                    border-color: {a};
                    color: {a_glow};
                }}
                QPushButton:pressed {{
                    background-color: {a_dim}44;
                }}
                QPushButton:disabled {{
                    color: {COLORS['text_muted']};
                    border-color: {COLORS['border']};
                }}
            """)
        elif self.variant == "ghost":
            self.setStyleSheet(f"""
                QPushButton {{
                    background-color: transparent;
                    color: {COLORS['text_secondary']};
                    border: none;
                    border-radius: 6px;
                    padding: 8px 16px;
                    font-size: 13px;
                }}
                QPushButton:hover {{
                    background-color: {COLORS['bg_card_hover']};
                    color: {COLORS['text_primary']};
                }}
                QPushButton:disabled {{
                    color: {COLORS['text_muted']};
                }}
            """)
        elif self.variant == "danger":
            self.setStyleSheet(f"""
                QPushButton {{
                    background-color: {COLORS['error']}22;
                    color: {COLORS['error']};
                    border: 1px solid {COLORS['error']}66;
                    border-radius: 8px;
                    padding: 10px 24px;
                    font-size: 13px;
                    font-weight: bold;
                }}
                QPushButton:hover {{
                    background-color: {COLORS['error']}44;
                    border-color: {COLORS['error']};
                }}
                QPushButton:disabled {{
                    color: {COLORS['text_muted']};
                    border-color: {COLORS['border']};
                }}
            """)


class SectionHeader(QLabel):
    def __init__(self, text, color: str | None = None, parent=None):
        super().__init__(text, parent)
        c = color or COLORS['accent']
        self.setStyleSheet(f"""
            QLabel {{
                color: {c};
                font-size: 11px;
                font-weight: bold;
                letter-spacing: 2px;
                padding: 0;
                margin: 0;
            }}
        """)


class Divider(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.HLine)
        self.setFixedHeight(1)
        self.setStyleSheet(f"background-color: {COLORS['border']}; border: none;")


class Card(QFrame):
    """
    The single content-container widget used everywhere in Griffin Hub —
    System Tune, Hardware Hub, Settings, and Plugins all build their
    sections out of this one component so the app has one consistent look
    instead of a different "card" style per page.

    Usage:
        card = Card("Section Title")      # optional title + divider
        layout = card.body()              # a QVBoxLayout to fill in
        layout.addWidget(...)

        # or attach a pre-built layout (QFormLayout, etc.):
        card = Card("Settings")
        form = QFormLayout()
        card.set_body_layout(form)
    """

    def __init__(self, title: str = "", parent=None, accent: str | None = None):
        super().__init__(parent)
        self.setObjectName("card")
        border_color = accent + "66" if accent else COLORS['border']
        bg_color = accent + "14" if accent else COLORS['bg_card']
        self.setStyleSheet(f"""
            QFrame#card {{
                background-color: {bg_color};
                border: 1px solid {border_color};
                border-radius: 10px;
            }}
        """)
        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(16, 14, 16, 14)
        self._outer.setSpacing(10)
        if title:
            self._outer.addWidget(SectionHeader(title, color=accent))
            self._outer.addWidget(Divider())

    def set_body_layout(self, layout) -> None:
        """Attach a pre-built layout (QVBoxLayout, QFormLayout, ...) as the card's content."""
        self._outer.addLayout(layout)

    def body(self) -> QVBoxLayout:
        """Create, attach, and return a QVBoxLayout for this card's content."""
        vbox = QVBoxLayout()
        vbox.setSpacing(8)
        self.set_body_layout(vbox)
        return vbox

    def addWidget(self, widget) -> None:
        """Convenience: append a widget directly to the outer card layout (after title/divider)."""
        self._outer.addWidget(widget)

    def addLayout(self, layout) -> None:
        self._outer.addLayout(layout)


class StatusBadge(QLabel):
    def __init__(self, status, parent=None, color: str | None = None, text: str | None = None):
        super().__init__(parent)
        self.set_status(status, color=color, text=text)

    def set_status(self, status, color: str | None = None, text: str | None = None):
        configs = {
            "ok":           (COLORS['success'],       "✓  OK"),
            "missing":      (COLORS['error'],         "✗  Missing"),
            "warning":      (COLORS['warning'],       "⚠  Warning"),
            "checking":     (COLORS['accent'],        "◌  Checking"),
            "installed":    (COLORS['success'],       "✓  Installed"),
            "not_installed":(COLORS['text_muted'],    "–  Not Installed"),
            "downloading":  (COLORS['accent'],        "↓  Downloading"),
        }
        if color is not None or text is not None:
            color = color or COLORS['text_muted']
            text  = text if text is not None else status
        else:
            color, text = configs.get(status, (COLORS['text_muted'], status))
        self.setText(text)
        self.setStyleSheet(f"""
            QLabel {{
                color: {color};
                background-color: {color}18;
                border: 1px solid {color}44;
                border-radius: 4px;
                padding: 3px 10px;
                font-size: 11px;
                font-weight: bold;
            }}
        """)


class LogPanel(QTextEdit):
    """Auto-scrolling read-only log panel."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)

    def append_line(self, line: str):
        self.append(line)
        sb = self.verticalScrollBar()
        sb.setValue(sb.maximum())



class NavItem(QFrame):
    clicked = pyqtSignal()

    def __init__(self, icon, label, index, parent=None):
        super().__init__(parent)
        self.index     = index
        self.is_active = False
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setFixedHeight(48)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 0, 16, 0)
        layout.setSpacing(12)

        icon_lbl = QLabel(icon)
        icon_lbl.setFixedWidth(20)
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_lbl.setStyleSheet("font-size: 16px; background: transparent;")
        layout.addWidget(icon_lbl)

        self.label = QLabel(label)
        self.label.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 13px; font-weight: 500; background: transparent;")
        layout.addWidget(self.label)
        layout.addStretch()

        self._apply_style(False)

    def _apply_style(self, active):
        if active:
            self.setStyleSheet(f"""
                NavItem {{
                    background-color: {COLORS['accent']}18;
                    border: none;
                    border-left: 3px solid {COLORS['accent']};
                }}
            """)
            self.label.setStyleSheet(f"color: {COLORS['accent_glow']}; font-size: 13px; font-weight: 600; background: transparent;")
        else:
            self.setStyleSheet(f"""
                NavItem {{
                    background-color: transparent;
                    border: none;
                    border-left: 3px solid transparent;
                }}
                NavItem:hover {{
                    background-color: {COLORS['bg_card']};
                    border-left: 3px solid {COLORS['border_bright']};
                }}
            """)
            self.label.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 13px; font-weight: 500; background: transparent;")

    def set_active(self, active):
        self.is_active = active
        self._apply_style(active)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)



class _ClickableCard(QFrame):
    def __init__(self, nav_idx, navigate_signal, parent=None):
        super().__init__(parent)
        self._nav    = nav_idx
        self._signal = navigate_signal
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        # Bug #40 fix: Qt's stylesheet engine uses the C++ class name for QSS
        # selectors, not the Python subclass name.  Python subclasses of QFrame
        # are seen as QFrame by Qt.  Use setObjectName + #name selector, which
        # is the correct Qt idiom for per-instance or per-subclass styling.
        self.setObjectName("ClickableCard")
        self.setStyleSheet(f"""
            QFrame#ClickableCard {{
                background-color: {COLORS['bg_card']};
                border: 1px solid {COLORS['border']};
                border-radius: 10px;
            }}
            QFrame#ClickableCard:hover {{
                background-color: {COLORS['bg_card_hover']};
                border-color: {COLORS['accent_dim']};
            }}
        """)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._signal.emit(self._nav)
        super().mousePressEvent(event)



class DistroInfo:
    """
    Reads /etc/os-release and probes the package manager to determine
    the host distro, package manager, DE, and compositor.

    This is the foundation that distro plugins can read and override.
    """
    _cache: dict | None = None

    @classmethod
    def detect(cls) -> dict:
        if cls._cache:
            return cls._cache
        info = {
            "id":           "unknown",
            "id_like":      [],
            "name":         "Unknown Linux",
            "version_id":   "",
            "pkg_manager":  "unknown",
            "pkg_install":  [],
            "de":           cls._detect_de(),
            "compositor":   cls._detect_compositor(),
            "flatpak":      bool(shutil.which("flatpak")),
        }
        try:
            os_release = Path("/etc/os-release")
            if os_release.exists():
                for line in os_release.read_text().splitlines():
                    if "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    v = v.strip().strip('"')
                    if k == "ID":
                        info["id"] = v.lower()
                    elif k == "ID_LIKE":
                        info["id_like"] = [x.strip() for x in v.lower().split()]
                    elif k == "NAME":
                        info["name"] = v
                    elif k == "VERSION_ID":
                        info["version_id"] = v
        except Exception as e:
            log.warning("Could not read /etc/os-release: %s", e)

        # Determine package manager from distro ID
        all_ids = [info["id"]] + info["id_like"]
        if any(x in all_ids for x in ("ubuntu", "debian", "linuxmint", "pop", "elementary", "kali", "zorin")):
            info["pkg_manager"] = "apt"
            info["pkg_install"] = ["apt-get", "install", "-y"]
        elif any(x in all_ids for x in ("fedora", "rhel", "centos", "almalinux", "rocky")):
            info["pkg_manager"] = "dnf"
            info["pkg_install"] = ["dnf", "install", "-y"]
        elif any(x in all_ids for x in ("opensuse", "suse")):
            info["pkg_manager"] = "zypper"
            info["pkg_install"] = ["zypper", "--non-interactive", "install"]
        elif any(x in all_ids for x in ("arch", "manjaro", "endeavouros", "garuda")):
            info["pkg_manager"] = "pacman"
            info["pkg_install"] = ["pacman", "-S", "--noconfirm"]
        elif shutil.which("apt-get"):
            info["pkg_manager"] = "apt"
            info["pkg_install"] = ["apt-get", "install", "-y"]
        elif shutil.which("dnf"):
            info["pkg_manager"] = "dnf"
            info["pkg_install"] = ["dnf", "install", "-y"]
        elif shutil.which("pacman"):
            info["pkg_manager"] = "pacman"
            info["pkg_install"] = ["pacman", "-S", "--noconfirm"]

        # Let distro plugins override detection results
        try:
            info = REGISTRY.apply_distro_overrides(info)
        except Exception as e:
            log.warning("Distro override plugin raised an exception (skipped): %s: %s",
                        type(e).__name__, e)
        cls._cache = info
        log.info("Distro detected: %s (%s) pkg=%s DE=%s",
                 info["name"], info["id"], info["pkg_manager"], info["de"])
        return info

    @classmethod
    def _detect_de(cls) -> str:
        de = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
        if de:
            return de
        session = os.environ.get("DESKTOP_SESSION", "").lower()
        return session or "unknown"

    @classmethod
    def _detect_compositor(cls) -> str:
        # Wayland vs X11
        if os.environ.get("WAYLAND_DISPLAY"):
            return "wayland"
        if os.environ.get("DISPLAY"):
            return "x11"
        return "unknown"

    @classmethod
    def invalidate(cls) -> None:
        """
        Fix #20: clear the detection cache so the next detect() call re-reads
        /etc/os-release and re-applies any newly loaded distro plugins.
        Call this after REGISTRY.load_single_plugin() in case the new plugin
        is a distro-type plugin that overrides package manager commands.
        """
        cls._cache = None
        log.debug("DistroInfo cache invalidated")


# Package name maps per package manager for all Griffin Hub dependencies

class UpdateCheckWorker(QThread):
    """Check GitHub for a newer Griffin Hub release (non-blocking)."""
    result = pyqtSignal(str, str)   # latest_version, release_url

    GITHUB_API = "https://api.github.com/repos/bobbycomet/GriffinHub/releases/latest"

    def run(self):
        try:
            req = urllib.request.Request(
                self.GITHUB_API,
                headers={"User-Agent": f"GriffinHub/{APP_VERSION}"},
            )
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
                data    = json.loads(resp.read())
                tag     = data.get("tag_name", "").lstrip("v")
                url     = data.get("html_url", "")
                self.result.emit(tag, url)
        except Exception as e:
            log.debug("Update check failed: %s", e)
            self.result.emit("", "")   # silently ignore — update check is optional



# ─────────────────────────────────────────────
#  SETTINGS
# ─────────────────────────────────────────────

class SettingsController:
    """
    Thin controller sitting between SettingsPage (UI) and Store.

    Responsibilities:
      - Validate the cache directory path (absolute, is_dir, writable)
      - Persist settings via Store.save_settings
      - Apply path changes to runtime globals immediately

    SettingsPage is responsible only for rendering widgets, collecting user
    input, and calling controller methods.
    """

    def __init__(self):
        self._settings = Store.load_settings()

    def get(self, key: str, default=None):
        return self._settings.get(key, default)

    def validate_cache_dir(self, raw: str) -> str | None:
        """Return an error string, or None if the path is acceptable."""
        raw = raw.strip()
        if not raw:
            return None
        p = Path(raw)
        if not p.is_absolute():
            return f"Path must be absolute — got: {raw}"
        if p.exists() and not p.is_dir():
            return f"Exists but is not a directory: {p}"
        return None

    def apply(self, toggles: dict[str, bool], cache_dir: str,
              sandbox: bool | None = None, sandbox_verified: bool | None = None) -> str | None:
        """Merge UI values into settings, persist, and apply path globals."""
        self._settings.update(toggles)
        cache_dir = cache_dir.strip()
        if cache_dir:
            self._settings["cache_dir"] = cache_dir
        if sandbox is not None:
            self._settings["plugin_sandbox"] = sandbox
        if sandbox_verified is not None:
            self._settings["plugin_sandbox_verified"] = sandbox_verified

        try:
            Store.save_settings(self._settings)
        except StoreError as e:
            return f"Could not save settings:\n{e}"

        if cache_dir:
            try:
                Path(cache_dir).mkdir(parents=True, exist_ok=True)
                import sys as _sys
                _mod = _sys.modules[__name__]
                new_path = Path(cache_dir)
                setattr(_mod, "CACHE_DIR", new_path)
                setattr(_mod, "STORE_CACHE_FILE", new_path / "store_index.json")
                log.info("Settings: CACHE_DIR → %s", new_path)
            except OSError as e:
                return f"Could not create cache directory:\n{e}"

        return None  # success


class SettingsPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._controller = SettingsController()
        self._build()

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        def build(layout):
            layout.setContentsMargins(32, 28, 32, 28)
            layout.setSpacing(18)

            title = QLabel("Settings")
            title.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: 22px; font-weight: 700;")
            layout.addWidget(title)
            layout.addWidget(Divider())

            # ── General ──────────────────────────────────────────────────
            general_card = Card("General")
            gl = general_card.body()
            self._notif_chk = QCheckBox("Show desktop notifications")
            self._notif_chk.setChecked(self._controller.get("notifications", True))
            gl.addWidget(self._notif_chk)
            layout.addWidget(general_card)

            # ── Plugin sandbox ───────────────────────────────────────────
            plugins_card = Card("Plugins")
            pl = plugins_card.body()
            self._sandbox_chk = QCheckBox("Sandbox community plugins (recommended)")
            self._sandbox_chk.setChecked(self._controller.get("plugin_sandbox", True))
            pl.addWidget(self._sandbox_chk)
            self._sandbox_verified_chk = QCheckBox("Also sandbox verified store plugins")
            self._sandbox_verified_chk.setChecked(self._controller.get("plugin_sandbox_verified", False))
            pl.addWidget(self._sandbox_verified_chk)

            store_row = QHBoxLayout()
            store_row.addWidget(QLabel("Plugin store URL:"))
            self._store_url = QLineEdit(self._controller.get("store_url", DEFAULT_STORE_URL))
            store_row.addWidget(self._store_url)
            pl.addLayout(store_row)
            layout.addWidget(plugins_card)

            # ── Paths ────────────────────────────────────────────────────
            paths_card = Card("Paths")
            pth = paths_card.body()
            cache_row = QHBoxLayout()
            cache_row.addWidget(QLabel("Cache directory:"))
            self._cache_dir = QLineEdit(self._controller.get("cache_dir", str(CACHE_DIR)))
            cache_row.addWidget(self._cache_dir)
            browse_btn = GlowButton("Browse…", "secondary")
            browse_btn.clicked.connect(self._browse_cache_dir)
            cache_row.addWidget(browse_btn)
            pth.addLayout(cache_row)

            open_logs_btn = GlowButton("Open Log Folder", "secondary")
            open_logs_btn.clicked.connect(
                lambda: SystemIntegration.open_file_manager(LOGS_DIR)
            )
            pth.addWidget(open_logs_btn)
            layout.addWidget(paths_card)

            # ── PolicyKit ────────────────────────────────────────────────
            polkit_card = Card("PolicyKit")
            pk = polkit_card.body()
            pk.addWidget(_gh_body_label(
                "Griffin Hub runs every privileged action through pkexec. Installing this "
                "small helper gives pkexec a stable target and a properly branded "
                "authentication prompt — useful for any install, and necessary for the "
                "AppImage build specifically, since pkexec can't elevate the AppImage file "
                "itself. Safe to install even if you're not using the AppImage.",
                muted=True
            ))
            self._polkit_status = QLabel()
            pk.addWidget(self._polkit_status)
            polkit_btn_row = QHBoxLayout()
            self._polkit_install_btn = GlowButton("Install PolicyKit Helper", "primary")
            self._polkit_install_btn.clicked.connect(self._install_polkit)
            self._polkit_uninstall_btn = GlowButton("Uninstall", "secondary")
            self._polkit_uninstall_btn.clicked.connect(self._uninstall_polkit)
            polkit_btn_row.addWidget(self._polkit_install_btn)
            polkit_btn_row.addWidget(self._polkit_uninstall_btn)
            polkit_btn_row.addStretch()
            pk.addLayout(polkit_btn_row)
            layout.addWidget(polkit_card)
            self._refresh_polkit_status()

            layout.addStretch()

            save_btn = GlowButton("Save Settings", "primary")
            save_btn.clicked.connect(self._save)
            layout.addWidget(save_btn)

        outer.addWidget(gt_scrolled(build))

    def _browse_cache_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Choose Cache Directory", self._cache_dir.text())
        if d:
            self._cache_dir.setText(d)

    def _refresh_polkit_status(self):
        installed = gt_polkit_helper_installed()
        self._polkit_status.setText(
            f"✓  Installed at {GT_POLKIT_HELPER_PATH}" if installed
            else "Not installed — pkexec calls will use the generic authentication prompt."
        )
        self._polkit_status.setStyleSheet(
            f"color: {COLORS['success'] if installed else COLORS['text_muted']}; font-size: 12px;"
        )
        self._polkit_install_btn.setEnabled(not installed)
        self._polkit_uninstall_btn.setEnabled(installed)

    def _install_polkit(self):
        ok, msg = gt_install_polkit_helper()
        if ok:
            QMessageBox.information(self, "PolicyKit Helper Installed", msg)
        else:
            QMessageBox.critical(self, "Install Failed", msg)
        self._refresh_polkit_status()

    def _uninstall_polkit(self):
        reply = QMessageBox.question(
            self, "Uninstall PolicyKit Helper",
            "Remove the installed helper and PolicyKit action file?\n\n"
            "Griffin Hub will keep working — pkexec calls just fall back to the generic prompt.",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        ok, msg = gt_uninstall_polkit_helper()
        (QMessageBox.information if ok else QMessageBox.critical)(
            self, "Uninstall" if ok else "Uninstall Failed", msg
        )
        self._refresh_polkit_status()

    def _save(self):
        cache_dir = self._cache_dir.text().strip()
        err = self._controller.validate_cache_dir(cache_dir)
        if err:
            QMessageBox.warning(self, "Invalid Path", err)
            return

        err = self._controller.apply(
            toggles={"notifications": self._notif_chk.isChecked(),
                     "store_url": self._store_url.text().strip() or DEFAULT_STORE_URL},
            cache_dir=cache_dir,
            sandbox=self._sandbox_chk.isChecked(),
            sandbox_verified=self._sandbox_verified_chk.isChecked(),
        )
        if err:
            QMessageBox.critical(self, "Save Failed", err)
        else:
            QMessageBox.information(self, "Saved", "Settings saved.")




# ─────────────────────────────────────────────
#  PLUGINS PAGE
# ─────────────────────────────────────────────

class _PluginRunWorker(QThread):
    """Runs a single plugin check or action command off the GUI thread."""
    finished = pyqtSignal(bool, str)

    def __init__(self, cmd: list, use_pkexec: bool = False):
        super().__init__()
        self.cmd = cmd
        self.use_pkexec = use_pkexec

    def run(self):
        if self.use_pkexec:
            ok, out = gt_pkexec_run(self.cmd)
            self.finished.emit(ok, out)
            return
        if not validate_check_cmd(self.cmd):
            self.finished.emit(False, f"'{self.cmd[0]}' is not in the allowlist for plugin commands.")
            return
        try:
            r = subprocess.run(self.cmd, capture_output=True, text=True, timeout=30)
            ok = r.returncode == 0
            self.finished.emit(ok, (r.stdout + r.stderr).strip())
        except Exception as e:
            self.finished.emit(False, str(e))


class PluginsPage(QWidget):
    """
    Lists loaded plugins and lets the user run the diagnostic checks and
    one-click actions they registered for System Tune and the Hardware Hub.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self._workers = []  # keep references alive while running
        self._build()

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        def build(layout):
            layout.setContentsMargins(32, 28, 32, 28)
            layout.setSpacing(20)

            header_row = QHBoxLayout()
            title_col = QVBoxLayout()
            title = QLabel("Plugins")
            title.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: 22px; font-weight: 700;")
            sub = QLabel("Plugins extend System Tune and the Hardware Hub with extra checks and actions.")
            sub.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 13px;")
            title_col.addWidget(title)
            title_col.addWidget(sub)
            header_row.addLayout(title_col)
            header_row.addStretch()
            reload_btn = GlowButton("↻  Reload Plugins", "secondary")
            reload_btn.clicked.connect(self._reload_plugins)
            header_row.addWidget(reload_btn)
            open_btn = GlowButton("📂  Open Plugins Folder", "secondary")
            open_btn.clicked.connect(lambda: SystemIntegration.open_file_manager(PLUGINS_DIR))
            header_row.addWidget(open_btn)
            layout.addLayout(header_row)
            layout.addWidget(Divider())

            # Loaded plugins summary
            layout.addWidget(SectionHeader("Loaded Plugins"))
            if REGISTRY._loaded:
                for fname in REGISTRY._loaded:
                    meta = REGISTRY._metadata.get(fname, {})
                    trust = meta.get("_effective_trust", "safe")
                    row = QLabel(
                        f"● {fname}  —  v{meta.get('version', '?')}  "
                        f"({trust}){'  by ' + meta['author'] if meta.get('author') else ''}"
                    )
                    row.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 12px;")
                    layout.addWidget(row)
            else:
                empty = QLabel(f"No plugins loaded yet. Drop .py files into {PLUGINS_DIR}")
                empty.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 12px;")
                layout.addWidget(empty)

            if REGISTRY._errors:
                layout.addWidget(gt_warn_box(
                    "Plugin load warnings/errors:\n" + "\n".join(REGISTRY._errors[:10])
                ))

            # Checks + actions, grouped by category
            for cat, label in (("tune", "System Tune Extensions"), ("hardware", "Hardware Hub Extensions")):
                checks  = REGISTRY.checks_by_category(cat)
                actions = REGISTRY.actions_by_category(cat)
                if not checks and not actions:
                    continue
                layout.addWidget(SectionHeader(label))
                for chk in checks:
                    layout.addWidget(self._check_row(chk))
                for act in actions:
                    layout.addWidget(self._action_row(act))

            layout.addStretch()

        outer.addWidget(gt_scrolled(build))

    def _reload_plugins(self):
        REGISTRY.checks.clear()
        REGISTRY.actions.clear()
        REGISTRY.targets = copy.deepcopy(PluginRegistry.EXTENSION_TARGETS)
        REGISTRY._errors.clear()
        REGISTRY._loaded.clear()
        REGISTRY._metadata.clear()
        REGISTRY.load_plugins()
        # Rebuild this page in place
        old_layout = self.layout()
        QWidget().setLayout(old_layout)  # detach old layout so _build() can set a fresh one
        self._build()

    def _row_frame(self) -> tuple[Card, QHBoxLayout]:
        card = Card()
        row = QHBoxLayout()
        card.set_body_layout(row)
        return card, row

    def _check_row(self, chk: dict) -> QFrame:
        frame, row = self._row_frame()
        label = QLabel(chk["name"])
        label.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: 13px;")
        row.addWidget(label)
        row.addStretch()
        status = QLabel("")
        status.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 12px;")
        row.addWidget(status)
        run_btn = QPushButton("Run Check")
        run_btn.clicked.connect(lambda: self._run(chk["cmd"], False, status, chk.get("fix_hint", "")))
        row.addWidget(run_btn)
        return frame

    def _action_row(self, act: dict) -> QFrame:
        frame, row = self._row_frame()
        label = QLabel(act["name"] + ("  🔒" if act.get("use_pkexec") else ""))
        label.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: 13px;")
        label.setToolTip(act.get("description", ""))
        row.addWidget(label)
        row.addStretch()
        status = QLabel("")
        status.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 12px;")
        row.addWidget(status)
        run_btn = QPushButton("Run Action")
        run_btn.clicked.connect(lambda: self._run(act["cmd"], act.get("use_pkexec", False), status, ""))
        row.addWidget(run_btn)
        return frame

    def _run(self, cmd: list, use_pkexec: bool, status_label: QLabel, fix_hint: str):
        status_label.setText("Running…")
        worker = _PluginRunWorker(cmd, use_pkexec)
        self._workers.append(worker)

        def on_done(ok, output):
            status_label.setText("✓ OK" if ok else "✗ Failed")
            status_label.setStyleSheet(
                f"color: {COLORS['success'] if ok else COLORS['error']}; font-size: 12px;"
            )
            if not ok and (output or fix_hint):
                QMessageBox.warning(
                    self, "Command Failed",
                    (output or "No output.") + (f"\n\nHint: {fix_hint}" if fix_hint else "")
                )
            if worker in self._workers:
                self._workers.remove(worker)

        worker.finished.connect(on_done)
        worker.start()



KERNEL_AUTOTUNE_STATE_PATH = Path("/etc/kernel-autotune/state.json")


class KernelAutotuneState:
    """
    Reads /etc/kernel-autotune/state.json and exposes hardware facts and
    applied tuning config as typed attributes.

    All fields are optional — if the file is missing or a key is absent,
    safe defaults are returned.  This allows Griffin Hub to work on systems
    without kernel-autotune installed.
    """

    def __init__(self):
        self._raw: dict = {}
        self._loaded = False
        self.load()

    def load(self) -> bool:
        """Reload state from disk.  Returns True if file was readable."""
        try:
            text = KERNEL_AUTOTUNE_STATE_PATH.read_text(encoding="utf-8")
            self._raw = json.loads(text)
            self._loaded = True
            return True
        except FileNotFoundError:
            self._raw = {}
            self._loaded = False
            return False
        except (json.JSONDecodeError, OSError) as e:
            log.warning("KernelAutotuneState: could not read state.json: %s", e)
            self._raw = {}
            self._loaded = False
            return False

    @property
    def available(self) -> bool:
        return self._loaded

    # ── Script / kernel metadata ──────────────────────────────────────────────
    @property
    def script_version(self) -> str:
        return self._raw.get("script_version", "unknown")

    @property
    def last_updated(self) -> str:
        return self._raw.get("last_updated", "")

    @property
    def kernel(self) -> str:
        """'generic', 'xanmod', 'liquorix', etc."""
        return self._raw.get("kernel", "generic")

    @property
    def device_type(self) -> str:
        """'desktop', 'laptop', 'server'"""
        return self._raw.get("device_type", "desktop")

    @property
    def apply_complete(self) -> bool:
        return self._raw.get("apply_complete", False)

    # ── Hardware facts (used by HardwareProbe and profile selection) ───────────
    @property
    def ram_mb(self) -> int:
        return int(self._raw.get("ram_mb", 0))

    @property
    def cpu_cores(self) -> int:
        return int(self._raw.get("cpu_cores", 0))

    @property
    def has_ssd(self) -> bool:
        return bool(self._raw.get("has_ssd", 0))

    @property
    def is_numa(self) -> bool:
        return bool(self._raw.get("is_numa", 0))

    @property
    def has_thermal(self) -> bool:
        return bool(self._raw.get("has_thermal", 0))

    # ── Applied configuration (current values) ────────────────────────────────
    @property
    def config(self) -> dict:
        return self._raw.get("config", {})

    @property
    def swappiness(self) -> int:
        return int(self.config.get("swappiness", 60))

    @property
    def dirty_ratio(self) -> int:
        return int(self.config.get("dirty_ratio", 20))

    @property
    def dirty_bg_ratio(self) -> int:
        return int(self.config.get("dirty_bg_ratio", 10))

    @property
    def vfs_cache_pressure(self) -> int:
        return int(self.config.get("vfs_cache_pressure", 100))

    @property
    def cpu_governor(self) -> str:
        return self.config.get("cpu_governor", "schedutil")

    @property
    def thp(self) -> str:
        return self.config.get("thp", "madvise")

    @property
    def zram_enabled(self) -> bool:
        return bool(self.config.get("zram_enabled", 0))

    @property
    def zram_ratio(self) -> int:
        return int(self.config.get("zram_ratio", 25))

    @property
    def zram_algo(self) -> str:
        return self.config.get("zram_algo", "lz4")

    @property
    def zswap_enabled(self) -> bool:
        return bool(self.config.get("zswap_enabled", 0))

    @property
    def tcp_congestion(self) -> str:
        return self.config.get("tcp_congestion", "cubic")

    @property
    def net_qdisc(self) -> str:
        return self.config.get("net_qdisc", "fq_codel")

    # ── Step tracking ─────────────────────────────────────────────────────────
    @property
    def applied_steps(self) -> list[str]:
        return self._raw.get("applied_steps", [])

    @property
    def failed_steps(self) -> list[str]:
        return self._raw.get("failed_steps", [])

    def summary(self) -> str:
        """One-line human-readable summary for the UI."""
        if not self._loaded:
            return "kernel-autotune not detected"
        parts = [
            f"kernel-autotune v{self.script_version}",
            f"kernel={self.kernel}",
            f"device={self.device_type}",
            f"governor={self.cpu_governor}",
        ]
        if self.zram_enabled:
            parts.append(f"zram={self.zram_ratio}%/{self.zram_algo}")
        if self.failed_steps:
            parts.append(f"⚠ {len(self.failed_steps)} failed step(s)")
        return "  •  ".join(parts)


# Singleton loaded once at startup; pages call .load() to refresh
KERNEL_AUTOTUNE = KernelAutotuneState()



class SystemTuneWorker(QThread):
    """
    Applies a batch of sysctl / governor / scheduler / zram settings.

    All writes that need root use pkexec so the GUI never has to run as root.
    Non-root writes (per-process limits, env vars) are applied directly.

    settings dict keys (all optional):
      sysctl        — dict of sysctl key → value  (pkexec tee /proc/sys/...)
      cpu_governor  — str e.g. "performance"
      io_scheduler  — str e.g. "mq-deadline"
      zram_ratio    — int percent of RAM (0 = disable zram)
      zram_algo     — str e.g. "zstd"
      zswap_enabled — bool
      thp           — str e.g. "madvise" / "always" / "never" / "defer+madvise"
      net_qdisc     — str e.g. "fq" / "fq_codel"
      tcp_congestion — str e.g. "bbr"
    """

    log      = pyqtSignal(str)
    progress = pyqtSignal(int)
    finished = pyqtSignal(bool, str)

    def __init__(self, settings: dict):
        super().__init__()
        self._settings  = settings
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    # ── helpers ──────────────────────────────────────────────────────────────

    def _run(self, cmd: list[str], *, root: bool = False,
             input_text: str | None = None) -> tuple[bool, str]:
        """Run a command, optionally via pkexec.  Returns (ok, output)."""
        if self._cancelled:
            return False, "Cancelled"
        if root:
            cmd = ["pkexec"] + cmd
        try:
            proc = subprocess.run(
                cmd,
                input=input_text.encode() if input_text else None,
                capture_output=True, timeout=30
            )
            out = (proc.stdout + proc.stderr).decode(errors="replace").strip()
            return proc.returncode == 0, out
        except subprocess.TimeoutExpired:
            return False, f"Timeout: {' '.join(cmd)}"
        except FileNotFoundError:
            return False, f"Not found: {cmd[0]}"
        except Exception as e:
            return False, str(e)

    def _write_sysctl(self, key: str, value: str) -> bool:
        """Write a single sysctl value via /proc/sys path."""
        # Convert sysctl dotted key to /proc/sys path
        proc_path = "/proc/sys/" + key.replace(".", "/")
        ok, out = self._run(
            ["tee", proc_path], root=True, input_text=str(value) + "\n"
        )
        if ok:
            self.log.emit(f"  ✓  {key} = {value}")
        else:
            self.log.emit(f"  ✗  {key} = {value}  ({out})")
        return ok

    def _set_governor(self, gov: str) -> bool:
        # Try to set all CPUs
        cpu_dirs = list(Path("/sys/devices/system/cpu").glob("cpu[0-9]*"))
        ok_count = 0
        for cpu_dir in cpu_dirs:
            gov_file = cpu_dir / "cpufreq" / "scaling_governor"
            if not gov_file.exists():
                continue
            ok, out = self._run(
                ["tee", str(gov_file)], root=True, input_text=gov + "\n"
            )
            if ok:
                ok_count += 1
        if ok_count > 0:
            self.log.emit(f"  ✓  CPU governor → {gov} ({ok_count} CPUs)")
            return True
        # Fallback: cpupower
        ok, out = self._run(
            ["cpupower", "frequency-set", "-g", gov], root=True
        )
        if ok:
            self.log.emit(f"  ✓  CPU governor → {gov} (cpupower)")
        else:
            self.log.emit(f"  ✗  CPU governor → {gov}: {out}")
        return ok

    def _set_io_scheduler(self, scheduler: str) -> bool:
        block_dir = Path("/sys/block")
        ok_count  = 0
        for dev in block_dir.iterdir():
            sched_file = dev / "queue" / "scheduler"
            if not sched_file.exists():
                continue
            ok, _ = self._run(
                ["tee", str(sched_file)], root=True,
                input_text=scheduler + "\n"
            )
            if ok:
                ok_count += 1
        if ok_count:
            self.log.emit(f"  ✓  I/O scheduler → {scheduler} ({ok_count} devices)")
            return True
        self.log.emit(f"  ✗  I/O scheduler → {scheduler}: no writable devices found")
        return False

    def _set_thp(self, mode: str) -> bool:
        thp_file = Path("/sys/kernel/mm/transparent_hugepage/enabled")
        if not thp_file.exists():
            self.log.emit("  ℹ  THP: not available on this kernel")
            return True
        ok, out = self._run(
            ["tee", str(thp_file)], root=True, input_text=mode + "\n"
        )
        if ok:
            self.log.emit(f"  ✓  THP → {mode}")
        else:
            self.log.emit(f"  ✗  THP → {mode}: {out}")
        return ok

    def _setup_zram(self, ratio: int, algo: str) -> bool:
        if ratio <= 0:
            self.log.emit("  ℹ  ZRAM: disabled (ratio=0)")
            return True
        # Compute size
        try:
            meminfo = Path("/proc/meminfo").read_text()
            for line in meminfo.splitlines():
                if line.startswith("MemTotal:"):
                    total_kb = int(line.split()[1])
                    break
            else:
                total_kb = 4 * 1024 * 1024
        except Exception:
            total_kb = 4 * 1024 * 1024
        size_bytes = int(total_kb * 1024 * ratio / 100)
        size_str   = str(size_bytes)

        # modprobe zram
        self._run(["modprobe", "zram"], root=True)
        # Set algorithm
        algo_file = Path("/sys/block/zram0/comp_algorithm")
        if algo_file.exists():
            self._run(["tee", str(algo_file)], root=True, input_text=algo + "\n")
        # Set size
        size_file = Path("/sys/block/zram0/disksize")
        if size_file.exists():
            ok, out = self._run(
                ["tee", str(size_file)], root=True, input_text=size_str + "\n"
            )
            if not ok:
                self.log.emit(f"  ✗  ZRAM disksize: {out}")
                return False
        # mkswap + swapon
        self._run(["mkswap", "/dev/zram0"], root=True)
        ok, out = self._run(["swapon", "-p", "100", "/dev/zram0"], root=True)
        if ok:
            self.log.emit(
                f"  ✓  ZRAM enabled: {size_bytes // (1024*1024)} MB, algo={algo}"
            )
        else:
            self.log.emit(f"  ✗  ZRAM swapon: {out}")
        return ok

    def _set_net(self, congestion: str, qdisc: str) -> bool:
        ok1, _ = self._run(
            ["tee", "/proc/sys/net/ipv4/tcp_congestion_control"],
            root=True, input_text=congestion + "\n"
        )
        if ok1:
            self.log.emit(f"  ✓  TCP congestion → {congestion}")
        # Apply qdisc to all physical interfaces
        try:
            ifaces = [
                p.name for p in Path("/sys/class/net").iterdir()
                if (p / "device").exists()
            ]
        except Exception:
            ifaces = []
        ok2 = True
        for iface in ifaces:
            ok, out = self._run(
                ["tc", "qdisc", "replace", "dev", iface,
                 "root", "handle", "1:", qdisc], root=True
            )
            if ok:
                self.log.emit(f"  ✓  qdisc {qdisc} → {iface}")
            else:
                self.log.emit(f"  ✗  qdisc {qdisc} → {iface}: {out}")
                ok2 = False
        return ok1

    # ── main entry point ──────────────────────────────────────────────────────

    def run(self) -> None:
        s   = self._settings
        steps = []
        if s.get("sysctl"):         steps.append("sysctl")
        if s.get("cpu_governor"):   steps.append("cpu_governor")
        if s.get("io_scheduler"):   steps.append("io_scheduler")
        if "thp" in s:              steps.append("thp")
        if "zram_ratio" in s:       steps.append("zram")
        if "tcp_congestion" in s or "net_qdisc" in s:
            steps.append("network")

        total   = max(len(steps), 1)
        done    = 0
        failed  = []

        def _step(name: str):
            nonlocal done
            done += 1
            self.progress.emit(int(done / total * 100))
            self.log.emit(f"[SystemTune] Applying: {name}")

        # sysctl
        if "sysctl" in steps:
            _step("sysctl")
            for key, val in s["sysctl"].items():
                if not self._write_sysctl(key, str(val)):
                    failed.append(f"sysctl:{key}")

        # CPU governor
        if "cpu_governor" in steps:
            _step("CPU governor")
            if not self._set_governor(s["cpu_governor"]):
                failed.append("cpu_governor")

        # I/O scheduler
        if "io_scheduler" in steps:
            _step("I/O scheduler")
            if not self._set_io_scheduler(s["io_scheduler"]):
                failed.append("io_scheduler")

        # THP
        if "thp" in steps:
            _step("transparent hugepages")
            if not self._set_thp(s["thp"]):
                failed.append("thp")

        # ZRAM
        if "zram" in steps:
            _step("ZRAM")
            if not self._setup_zram(
                s.get("zram_ratio", 25),
                s.get("zram_algo", "zstd")
            ):
                failed.append("zram")

        # Network
        if "network" in steps:
            _step("network")
            if not self._set_net(
                s.get("tcp_congestion", "bbr"),
                s.get("net_qdisc", "fq")
            ):
                failed.append("network")

        self.progress.emit(100)
        if failed:
            self.finished.emit(False,
                f"Applied with {len(failed)} error(s): {', '.join(failed)}")
        else:
            self.finished.emit(True, f"All {len(steps)} step(s) applied successfully.")




class _GtOverviewTab(QWidget):
    """GameTuneHub Overview — install dots, service status, wrapper status."""
    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        def build(layout):
            layout.addWidget(gt_info_box(
                "<b>How the Steam Wrapper works:</b><br>"
                "A shell script intercepts Steam at launch and reads "
                "<code>~/.config/gametune-hub/steam.conf</code> every time Steam starts. "
                "Toggle tools without editing any files again.<br><br>"
                "<b>ProcessSentry v3</b> — daemon that throttles misbehaving processes "
                "while leaving game processes alone.<br><br>"
                "<b>Kernel Autotune</b> — one-shot boot service that tunes memory, I/O, "
                "CPU governor, and network."
            ))
            grp = Card("Installed Binaries")
            gl = grp.body()
            checks = [
                ("gamemoded",        "GameMode daemon (gamemoded)"),
                ("gamescope",        "Gamescope compositor"),
                ("mangohud",         "MangoHud overlay"),
                (str(GT_SENTRY_BIN), "ProcessSentry v3 (sentryv3.py)"),
                (str(GT_KERNEL_BIN), "Kernel Autotune"),
            ]
            self._install_rows = []
            for ident, name in checks:
                row = QHBoxLayout()
                dot   = QLabel("●"); dot.setFont(QFont("monospace", 14))
                label = QLabel(name)
                state = QLabel()
                state.setStyleSheet("font-size:12px;")
                row.addWidget(dot); row.addWidget(label)
                row.addStretch(); row.addWidget(state)
                gl.addLayout(row)
                self._install_rows.append((ident, dot, state))
            layout.addWidget(grp)

            grp2 = Card("Service Status")
            g2 = grp2.body()
            self._svc_widgets = []
            for svc, name in [
                (GT_SENTRY_SERVICE, "ProcessSentry v3"),
                (GT_KERNEL_SERVICE, "Kernel Autotune"),
            ]:
                w = GtServiceStatusWidget(svc, name)
                g2.addWidget(w)
                self._svc_widgets.append(w)
            layout.addWidget(grp2)

            grp3 = Card("Steam Wrapper Status")
            g3 = grp3.body()
            self._wrapper_lbl = QLabel()
            self._wrapper_lbl.setWordWrap(True)
            g3.addWidget(self._wrapper_lbl)
            layout.addWidget(grp3)

        outer.addWidget(gt_scrolled(build))
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(5000)
        QTimer.singleShot(100, self._refresh)

    def _refresh(self):
        for ident, dot, state in self._install_rows:
            installed = Path(ident).exists() if ident.startswith("/") else gt_is_installed(ident)
            c = COLORS["success"] if installed else COLORS["error"]
            dot.setStyleSheet(f"color:{c};")
            state.setText("installed" if installed else "NOT FOUND")
            state.setStyleSheet(f"color:{c};font-size:12px;")
        for w in self._svc_widgets:
            w.refresh()
        exec_line = gt_steam_desktop_exec()
        if exec_line and str(GT_COMBINED_WRAPPER) in exec_line:
            self._wrapper_lbl.setText("Active: " + exec_line)
            self._wrapper_lbl.setStyleSheet(f"color:{COLORS['success']};")
        else:
            self._wrapper_lbl.setText("No Griffin Hub wrapper active — vanilla Steam launch.")
            self._wrapper_lbl.setStyleSheet(f"color:{COLORS['text_muted']};")


class _GtWrappersTab(QWidget):
    """Steam Wrappers — backup, toggles, gamescope flags, apply, copy Steam launch cmd."""
    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        def build(layout):
            layout.addWidget(gt_info_box(
                "A config-driven wrapper handles GameMode, Gamescope, and MangoHud. "
                "Toggle tools here — they take effect on next Steam restart without editing "
                "the desktop file again. Always create a backup first.<br><br>"
                "The <b>Steam Launch Options</b> panel below also generates copy-paste strings "
                "for adding non-Steam games with the exact same wrapper stack."
            ))
            self._build_backup(layout)
            layout.addWidget(gt_separator())
            self._build_toggles(layout)
            self._build_gamescope(layout)
            self._build_preview(layout)
            self._build_steam_launch_opts(layout)
            self._build_apply(layout)

        outer.addWidget(gt_scrolled(build))
        self._update_preview()
        QTimer.singleShot(0, self._refresh_status)

    def _build_backup(self, layout):
        grp = Card("Step 1 Backup Original Steam Launcher")
        gl = grp.body()
        gl.addWidget(gt_warn_box(
            "Do this BEFORE applying. Created from the system original "
            "(/usr/share/applications/steam.desktop) once — never overwritten."
        ))
        self._bak_status_lbl = QLabel()
        self._bak_status_lbl.setWordWrap(True)
        gl.addWidget(self._bak_status_lbl)
        row = QHBoxLayout()
        self._btn_backup  = QPushButton("Backup Original Steam Launcher")
        self._btn_restore = QPushButton("Restore Original (full rollback)")
        self._btn_backup.clicked.connect(self._do_backup)
        self._btn_restore.clicked.connect(self._do_restore)
        row.addWidget(self._btn_backup); row.addWidget(self._btn_restore)
        gl.addLayout(row)
        layout.addWidget(grp)

    def _build_toggles(self, layout):
        grp = Card("Step 2 — Enable / Disable Tools")
        tgl = grp.body()
        tgl.addWidget(gt_info_box(
            "Changes saved to <code>~/.config/gametune-hub/steam.conf</code> immediately."
        ))
        cfg = gt_read_steam_conf()
        self._tog_gm = QCheckBox("GameMode  — CPU/IO scheduling (gamemoderun)")
        self._tog_gs = QCheckBox("Gamescope — nested Wayland micro-compositor")
        self._tog_mh = QCheckBox("MangoHud  — FPS / GPU / CPU overlay")
        self._tog_gm.setChecked(cfg.get("GAMEMODE",  "1") == "1")
        self._tog_gs.setChecked(cfg.get("GAMESCOPE", "1") == "1")
        self._tog_mh.setChecked(cfg.get("MANGOHUD",  "1") == "1")
        for cb in (self._tog_gm, self._tog_gs, self._tog_mh):
            tgl.addWidget(cb)
            cb.stateChanged.connect(self._on_toggle)
        self._toggle_status_lbl = QLabel()
        self._toggle_status_lbl.setStyleSheet(
            f"color:{COLORS['text_muted']};font-size:12px;font-style:italic;"
        )
        tgl.addWidget(self._toggle_status_lbl)
        layout.addWidget(grp)

    def _build_gamescope(self, layout):
        box = Card("Gamescope Flags")
        fl = QFormLayout(); box.set_body_layout(fl)
        cfg = gt_read_steam_conf()
        def iv(key, default):
            try: return int(cfg.get(key, default))
            except ValueError: return int(default)
        self._gs_w  = QSpinBox(); self._gs_w.setRange(640, 7680);  self._gs_w.setValue(iv("GS_W",  1920))
        self._gs_h  = QSpinBox(); self._gs_h.setRange(480, 4320);  self._gs_h.setValue(iv("GS_H",  1080))
        self._gs_ow = QSpinBox(); self._gs_ow.setRange(640, 7680); self._gs_ow.setValue(iv("GS_OW", 1920))
        self._gs_oh = QSpinBox(); self._gs_oh.setRange(480, 4320); self._gs_oh.setValue(iv("GS_OH", 1080))
        self._gs_r  = QSpinBox(); self._gs_r.setRange(24, 360);    self._gs_r.setValue(iv("GS_R",   60))
        self._gs_fsr = QCheckBox("FSR upscaling (-S fsr)")
        self._gs_vrr = QCheckBox("Adaptive sync / VRR (--adaptive-sync)")
        self._gs_hdr = QCheckBox("HDR (--hdr-enabled)")
        self._gs_fsr.setChecked(cfg.get("GS_FSR", "0") == "1")
        self._gs_vrr.setChecked(cfg.get("GS_VRR", "0") == "1")
        self._gs_hdr.setChecked(cfg.get("GS_HDR", "0") == "1")
        for lbl, w in [
            ("Internal render width (-W)",    self._gs_w),
            ("Internal render height (-H)",   self._gs_h),
            ("Output (monitor) width (-w)",   self._gs_ow),
            ("Output (monitor) height (-h)",  self._gs_oh),
            ("FPS / refresh rate limit (-r)", self._gs_r),
        ]:
            fl.addRow(lbl, w)
        for cb in (self._gs_fsr, self._gs_vrr, self._gs_hdr):
            fl.addRow("", cb)
        fl.addRow(QLabel("<i>Tip: lower internal res + FSR = upscaled performance boost.</i>"))
        for w in (self._gs_w, self._gs_h, self._gs_ow, self._gs_oh, self._gs_r):
            w.valueChanged.connect(self._update_preview)
        for cb in (self._gs_fsr, self._gs_vrr, self._gs_hdr):
            cb.stateChanged.connect(self._update_preview)
        layout.addWidget(box)

    def _build_preview(self, layout):
        grp = Card("Wrapper Script Preview")
        gl = grp.body()
        gl.addWidget(QLabel(f"Script will be written to {GT_COMBINED_WRAPPER}:"))
        self._preview = QPlainTextEdit()
        self._preview.setReadOnly(True)
        self._preview.setMaximumHeight(140)
        gl.addWidget(self._preview)
        layout.addWidget(grp)

    def _build_steam_launch_opts(self, layout):
        """
        Steam Launch Options generator — for non-Steam games.
        Builds the ENV=val wrapper %command% string that can be pasted into
        Steam's launch options field, or used directly in a terminal.
        """
        grp = Card("Steam Launch Options (for Non-Steam Games / Terminal)")
        gl = grp.body()
        gl.addWidget(gt_info_box(
            "Use the strings below to add any game to Steam as a Non-Steam Game "
            "with the same wrapper stack, or to test in a terminal.<br><br>"
            "In Steam: Library → Add Non-Steam Game → pick the game EXE → "
            "right-click → Properties → Launch Options → paste the string. "
            "<b>%command%</b> is Steam's placeholder for the actual executable."
        ))

        lbl1 = QLabel("Steam Launch Options string:")
        lbl1.setStyleSheet(f"color:{COLORS['text_primary']};font-weight:600;")
        gl.addWidget(lbl1)
        self._steam_opts_box = QPlainTextEdit()
        self._steam_opts_box.setReadOnly(True)
        self._steam_opts_box.setMaximumHeight(60)
        self._steam_opts_box.setStyleSheet(
            f"background:{COLORS['bg_card']};color:{COLORS['accent']};"
            f"font-family:monospace;font-size:11px;"
            f"border:1px solid {COLORS['border']};border-radius:4px;"
        )
        gl.addWidget(self._steam_opts_box)

        row1 = QHBoxLayout()
        btn_copy_steam = QPushButton("📋  Copy Steam Launch Options")
        btn_copy_steam.clicked.connect(
            lambda: QApplication.clipboard().setText(self._steam_opts_box.toPlainText())
        )
        btn_regen = QPushButton("🔄  Refresh")
        btn_regen.clicked.connect(self._update_preview)
        row1.addWidget(btn_copy_steam); row1.addWidget(btn_regen); row1.addStretch()
        gl.addLayout(row1)

        lbl2 = QLabel("Terminal command (test without Steam):")
        lbl2.setStyleSheet(f"color:{COLORS['text_primary']};font-weight:600;margin-top:8px;")
        gl.addWidget(lbl2)
        self._terminal_opts_box = QPlainTextEdit()
        self._terminal_opts_box.setReadOnly(True)
        self._terminal_opts_box.setMaximumHeight(60)
        self._terminal_opts_box.setStyleSheet(
            f"background:{COLORS['bg_card']};color:{COLORS['text_muted']};"
            f"font-family:monospace;font-size:11px;"
            f"border:1px solid {COLORS['border']};border-radius:4px;"
        )
        gl.addWidget(self._terminal_opts_box)
        btn_copy_term = QPushButton("📋  Copy Terminal Command")
        btn_copy_term.clicked.connect(
            lambda: QApplication.clipboard().setText(self._terminal_opts_box.toPlainText())
        )
        gl.addWidget(btn_copy_term)

        # Explanation of each component
        explain_lbl = QLabel("What each component does:")
        explain_lbl.setStyleSheet(
            f"color:{COLORS['accent']};font-size:11px;font-weight:700;margin-top:6px;"
        )
        gl.addWidget(explain_lbl)
        self._explain_box = QLabel()
        self._explain_box.setWordWrap(True)
        self._explain_box.setStyleSheet(
            f"color:{COLORS['text_muted']};font-size:11px;"
        )
        gl.addWidget(self._explain_box)
        layout.addWidget(grp)

    def _build_apply(self, layout):
        grp = Card("Step 3 Apply Integration to Steam")
        gl = grp.body()
        gl.addWidget(gt_warn_box(
            "Create a backup first (Step 1). The desktop file is patched once. "
            "Use toggles to change tools without further desktop edits."
        ))
        btn_apply = GlowButton("Apply Wrapper to Steam", "primary")
        btn_apply.clicked.connect(self._apply_wrapper)
        gl.addWidget(btn_apply)
        self._apply_status_lbl = QLabel()
        self._apply_status_lbl.setWordWrap(True)
        self._apply_status_lbl.setStyleSheet(
            f"color:{COLORS['text_muted']};font-size:12px;font-style:italic;"
        )
        gl.addWidget(self._apply_status_lbl)
        layout.addWidget(grp)

    # ── helpers ───────────────────────────────────────────────────────────────

    def _gs_flags_str(self) -> str:
        flags = [
            f"-W {self._gs_w.value()} -H {self._gs_h.value()}",
            f"-w {self._gs_ow.value()} -h {self._gs_oh.value()}",
            f"-r {self._gs_r.value()}", "-e -f",
        ]
        if self._gs_fsr.isChecked(): flags.append("-S fsr")
        if self._gs_vrr.isChecked(): flags.append("--adaptive-sync")
        if self._gs_hdr.isChecked(): flags.append("--hdr-enabled")
        return " \\\n        ".join(flags)

    def _current_conf(self) -> dict:
        return {
            "GAMEMODE":  "1" if self._tog_gm.isChecked() else "0",
            "GAMESCOPE": "1" if self._tog_gs.isChecked() else "0",
            "MANGOHUD":  "1" if self._tog_mh.isChecked() else "0",
            "GS_W": str(self._gs_w.value()),   "GS_H": str(self._gs_h.value()),
            "GS_OW": str(self._gs_ow.value()), "GS_OH": str(self._gs_oh.value()),
            "GS_R": str(self._gs_r.value()),
            "GS_FSR": "1" if self._gs_fsr.isChecked() else "0",
            "GS_VRR": "1" if self._gs_vrr.isChecked() else "0",
            "GS_HDR": "1" if self._gs_hdr.isChecked() else "0",
        }

    def _build_steam_cmd_string(self) -> str:
        """Build the %command% Steam launch options string from current toggles."""
        parts: list[str] = []
        explanations: list[str] = []
        cfg = self._current_conf()
        w, h     = self._gs_w.value(), self._gs_h.value()
        ow, oh   = self._gs_ow.value(), self._gs_oh.value()
        r_val    = self._gs_r.value()
        fsr      = self._gs_fsr.isChecked()
        vrr      = self._gs_vrr.isChecked()
        hdr      = self._gs_hdr.isChecked()

        if cfg["GAMEMODE"] == "1":
            parts.append("gamemoderun")
            explanations.append(
                "gamemoderun — requests performance CPU governor, disables background "
                "throttling while game runs. Requires gamemode daemon."
            )
        if cfg["GAMESCOPE"] == "1":
            gs_args = [
                "gamescope",
                f"-W {w}", f"-H {h}", f"-w {ow}", f"-h {oh}", f"-r {r_val}", "-e -f",
            ]
            if fsr: gs_args.append("-S fsr");        explanations.append("FSR upscaling: renders at lower res, upscales to monitor res.")
            if vrr: gs_args.append("--adaptive-sync"); explanations.append("--adaptive-sync: enables VRR / FreeSync / G-Sync.")
            if hdr: gs_args.append("--hdr-enabled");   explanations.append("--hdr-enabled: HDR output (display must support it).")
            gs_args.append("--")
            parts.extend(gs_args)
            explanations.insert(
                1 if cfg["GAMEMODE"] == "1" else 0,
                f"gamescope: micro-compositor. Renders at {w}×{h}, outputs to {ow}×{oh} at {r_val}fps. "
                "-e=fullscreen -f=grab keyboard."
            )
        if cfg["MANGOHUD"] == "1":
            parts.append("mangohud --dlsym")
            explanations.append(
                "mangohud --dlsym: FPS/GPU/CPU overlay. --dlsym needed for Vulkan games."
            )
        parts.append("%command%")
        return " ".join(parts), explanations

    def _build_terminal_cmd(self) -> str:
        """Terminal test command — replaces %command% with a placeholder."""
        steam_str, _ = self._build_steam_cmd_string()
        return steam_str.replace("%command%", "/path/to/game.exe")

    def _update_preview(self):
        cfg    = self._current_conf()
        script = gt_build_wrapper_script(cfg, self._gs_flags_str())
        self._preview.setPlainText(script)
        steam_str, explanations = self._build_steam_cmd_string()
        self._steam_opts_box.setPlainText(steam_str)
        self._terminal_opts_box.setPlainText(self._build_terminal_cmd())
        expl_text = "\n".join(f"• {e}" for e in explanations) if explanations else "(no wrapper tools enabled)"
        self._explain_box.setText(expl_text)

    def _on_toggle(self):
        self._update_preview()
        try:
            gt_write_steam_conf(self._current_conf())
        except Exception as exc:
            self._toggle_status_lbl.setText("Config save failed: " + str(exc))
            return
        if not GT_COMBINED_WRAPPER.exists():
            self._toggle_status_lbl.setText("Config saved (wrapper not yet deployed).")
            return
        def on_write(ok, err):
            if ok:
                gt_run_bg(gt_pkexec_run, ["chmod", "+x", str(GT_COMBINED_WRAPPER)])
                self._toggle_status_lbl.setText("Config saved and wrapper updated.")
            else:
                self._toggle_status_lbl.setText("Saved but wrapper update failed: " + err)
        script = gt_build_wrapper_script(self._current_conf(), self._gs_flags_str())
        gt_run_bg(gt_pkexec_write, GT_COMBINED_WRAPPER, script, on_result=on_write)

    def _do_backup(self):
        ok, msg = gt_create_backup()
        if ok and msg == "Backup already exists.":
            QMessageBox.information(self, "Already Backed Up",
                f"Backup exists:\n{GT_STEAM_DESKTOP_BAK}")
        elif ok:
            QMessageBox.information(self, "Backup Created",
                f"Backup saved to:\n{GT_STEAM_DESKTOP_BAK}")
        else:
            QMessageBox.critical(self, "Backup Failed", msg)
        self._refresh_status()

    def _do_restore(self):
        if not gt_backup_exists():
            QMessageBox.warning(self, "No Backup", f"No backup found.\nExpected: {GT_STEAM_DESKTOP_BAK}")
            return
        dlg = GtConfirmDialog(
            "Restore Original Steam Launcher",
            "Completely roll back Griffin Hub Steam integration.",
            [
                f"{GT_STEAM_DESKTOP_BAK}  →  {GT_STEAM_DESKTOP_DST}",
                f"Remove wrapper: {GT_COMBINED_WRAPPER}",
                f"Remove config: {GT_CONF}",
                "Remove backup file",
            ],
            reversible=False, parent=self
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            ok, err = gt_restore_from_backup()
            if ok: QMessageBox.information(self, "Restored", "Steam launcher restored. Restart Steam.")
            else:  QMessageBox.critical(self, "Restore Failed", err)
            self._refresh_status()

    def _apply_wrapper(self):
        if not gt_backup_exists():
            resp = QMessageBox.question(self, "No Backup Found",
                "Create a backup now before applying?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel)
            if resp == QMessageBox.StandardButton.Cancel: return
            if resp == QMessageBox.StandardButton.Yes:
                ok, msg = gt_create_backup()
                if not ok: QMessageBox.critical(self, "Backup Failed", msg); return
        cfg    = self._current_conf()
        active = [k for k in ("GAMEMODE","GAMESCOPE","MANGOHUD") if cfg.get(k) == "1"]
        dlg = GtConfirmDialog(
            "Apply Steam Wrapper",
            "Deploy the config-driven wrapper to Steam.",
            [
                f"Write wrapper script to {GT_COMBINED_WRAPPER}",
                f"Write config to {GT_CONF}",
                f"Patch Exec= in {GT_STEAM_DESKTOP_DST}",
                "Active tools: " + (", ".join(active) if active else "none"),
            ],
            reversible=True, parent=self
        )
        if dlg.exec() != QDialog.DialogCode.Accepted: return
        errors = []
        script = gt_build_wrapper_script(cfg, self._gs_flags_str())
        ok, err = gt_pkexec_write(GT_COMBINED_WRAPPER, script)
        if not ok: QMessageBox.critical(self, "Failed", "Write wrapper failed: " + err); return
        gt_pkexec_run(["chmod", "+x", str(GT_COMBINED_WRAPPER)])
        try: gt_write_steam_conf(cfg)
        except Exception as exc: errors.append("Write conf: " + str(exc))
        GT_STEAM_DESKTOP_DST.parent.mkdir(parents=True, exist_ok=True)
        if not GT_STEAM_DESKTOP_DST.exists():
            src = GT_STEAM_DESKTOP_BAK if GT_STEAM_DESKTOP_BAK.exists() else GT_STEAM_DESKTOP_SRC
            if src.exists():
                try: shutil.copy(src, GT_STEAM_DESKTOP_DST)
                except Exception as exc: errors.append("Copy desktop: " + str(exc))
        if GT_STEAM_DESKTOP_DST.exists():
            try:
                txt     = GT_STEAM_DESKTOP_DST.read_text()
                patched = re.sub(r'^Exec=.*$', f"Exec={GT_COMBINED_WRAPPER} %U", txt, flags=re.MULTILINE)
                GT_STEAM_DESKTOP_DST.write_text(patched)
            except Exception as exc: errors.append("Patch desktop: " + str(exc))
        if errors: QMessageBox.critical(self, "Errors", "\n".join(errors))
        else: QMessageBox.information(self, "Applied", "Wrapper applied. Restart Steam to activate.")
        self._refresh_status()

    def _refresh_status(self):
        has_bak = gt_backup_exists()
        self._btn_restore.setEnabled(has_bak)
        if has_bak:
            self._bak_status_lbl.setText(f"Backup exists: {GT_STEAM_DESKTOP_BAK}")
            self._bak_status_lbl.setStyleSheet(f"color:{COLORS['success']};font-size:12px;")
            self._btn_backup.setText("Backup Already Created")
            self._btn_backup.setEnabled(False)
        else:
            self._bak_status_lbl.setText("No backup yet. Create one before applying.")
            self._bak_status_lbl.setStyleSheet("color:#e3b341;font-size:12px;")
            self._btn_backup.setText("Backup Original Steam Launcher")
            self._btn_backup.setEnabled(True)
        exec_line = gt_steam_desktop_exec()
        deployed = str(GT_COMBINED_WRAPPER) in exec_line if exec_line else False
        if deployed:
            self._apply_status_lbl.setText("Wrapper deployed. Use toggles to change tools.")
            self._apply_status_lbl.setStyleSheet(f"color:{COLORS['success']};font-size:12px;")
        else:
            self._apply_status_lbl.setText("Wrapper not deployed. Click Apply when ready.")
            self._apply_status_lbl.setStyleSheet(f"color:{COLORS['text_muted']};font-size:12px;")


class _GtSentryTab(QWidget):
    """ProcessSentry v3 — service control + full config editor.

    Mirrors every section of /etc/process-sentry/config.yaml:
    intervals, thresholds, prediction, learning, cgroup, exclusions,
    logging, and advanced. Form fields take priority on Save; the raw
    YAML box is an escape hatch for anything not exposed as a field
    (and for keys this build doesn't know about yet).
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self._cfg: dict = {}
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        def build(layout):
            layout.addWidget(gt_info_box(
                "ProcessSentry watches all running processes and throttles those that "
                "misbehave (excessive CPU, I/O, or memory). Detects GameMode and leaves "
                "game processes completely alone. Learns chronic offenders and pre-throttles them."
            ))
            self._svc = GtServiceControlGroup(
                GT_SENTRY_SERVICE, "ProcessSentry v3 Service",
                "Controls the background daemon."
            )
            layout.addWidget(self._svc)
            boot_row = QHBoxLayout()
            for label, action in [("Enable at Boot","enable"),("Disable at Boot","disable")]:
                btn = QPushButton(label)
                btn.clicked.connect(
                    lambda checked, a=action: gt_run_bg(
                        gt_systemctl_action, a, GT_SENTRY_SERVICE,
                        on_result=lambda ok, m, aa=a: QMessageBox.information(
                            self, aa.capitalize(), aa.capitalize() + "d." if ok else "Failed: " + m
                        )
                    )
                )
                boot_row.addWidget(btn)
            boot_row.addStretch()
            layout.addLayout(boot_row)
            layout.addWidget(gt_separator())

            # ── Polling Intervals ────────────────────────────────────────
            pl = Card("Polling Intervals")
            pf = QFormLayout(); pl.set_body_layout(pf)
            self._poll_base = QDoubleSpinBox(); self._poll_base.setRange(0.5, 60); self._poll_base.setSuffix(" s")
            self._poll_min  = QDoubleSpinBox(); self._poll_min.setRange(0.1, 10);  self._poll_min.setSuffix(" s")
            self._poll_max  = QDoubleSpinBox(); self._poll_max.setRange(1, 120);   self._poll_max.setSuffix(" s")
            self._ema_alpha = QDoubleSpinBox(); self._ema_alpha.setRange(0.01,1.0); self._ema_alpha.setDecimals(2)
            pf.addRow("Base interval:", self._poll_base)
            pf.addRow("Minimum interval (high load):", self._poll_min)
            pf.addRow("Maximum interval (low load):", self._poll_max)
            pf.addRow("EMA smoothing alpha:", self._ema_alpha)
            layout.addWidget(pl)

            # ── Throttle Thresholds ──────────────────────────────────────
            thr = Card("Throttle Thresholds")
            tf = QFormLayout(); thr.set_body_layout(tf)
            self._cpu_thresh  = QDoubleSpinBox(); self._cpu_thresh.setRange(10, 100);  self._cpu_thresh.setSuffix(" %")
            self._io_thresh   = QDoubleSpinBox(); self._io_thresh.setRange(1, 500);    self._io_thresh.setSuffix(" MB/s")
            self._mem_thresh  = QDoubleSpinBox(); self._mem_thresh.setRange(1, 100);   self._mem_thresh.setSuffix(" %")
            self._psi_thresh  = QDoubleSpinBox(); self._psi_thresh.setRange(0, 100);   self._psi_thresh.setSuffix(" avg10")
            tf.addRow("CPU % per process:", self._cpu_thresh)
            tf.addRow("I/O bandwidth:", self._io_thresh)
            tf.addRow("Memory % per process:", self._mem_thresh)
            tf.addRow("PSI memory pressure (0=disable):", self._psi_thresh)
            layout.addWidget(thr)

            # ── Predictive Throttling ────────────────────────────────────
            pred_card = Card("Predictive Throttling")
            predf = QFormLayout(); pred_card.set_body_layout(predf)
            self._history_size  = QSpinBox(); self._history_size.setRange(2, 200)
            self._habit_conf    = QDoubleSpinBox(); self._habit_conf.setRange(0.1,1.0); self._habit_conf.setDecimals(2)
            self._pattern_match = QDoubleSpinBox(); self._pattern_match.setRange(0.0,1.0); self._pattern_match.setDecimals(2)
            predf.addRow("History window size (samples):", self._history_size)
            predf.addRow("Pre-throttle confidence:", self._habit_conf)
            predf.addRow("Pattern match threshold:", self._pattern_match)
            layout.addWidget(pred_card)

            # ── Adaptive Learning ────────────────────────────────────────
            lrn = Card("Adaptive Learning")
            lf = QFormLayout(); lrn.set_body_layout(lf)
            self._up_rate    = QDoubleSpinBox(); self._up_rate.setRange(0.001,1.0);    self._up_rate.setDecimals(3);    self._up_rate.setSingleStep(0.01)
            self._down_rate  = QDoubleSpinBox(); self._down_rate.setRange(0.001,1.0);  self._down_rate.setDecimals(3);  self._down_rate.setSingleStep(0.005)
            self._habit_cap  = QDoubleSpinBox(); self._habit_cap.setRange(0.1,1.0);    self._habit_cap.setDecimals(2)
            self._decay_rate = QDoubleSpinBox(); self._decay_rate.setRange(0.0,0.1);   self._decay_rate.setDecimals(4); self._decay_rate.setSingleStep(0.0001)
            lf.addRow("Habit score rise rate (up_rate):", self._up_rate)
            lf.addRow("Habit score fall rate (down_rate):", self._down_rate)
            lf.addRow("Habit score cap:", self._habit_cap)
            lf.addRow("Passive decay per second (0=disable):", self._decay_rate)
            layout.addWidget(lrn)

            # ── Cgroup v2 Integration ────────────────────────────────────
            cg = Card("Cgroup v2 Integration")
            cgf = QFormLayout(); cg.set_body_layout(cgf)
            self._cpu_quota_throttled = QSpinBox(); self._cpu_quota_throttled.setRange(1, 100);   self._cpu_quota_throttled.setSuffix(" %")
            self._cpu_quota_normal    = QSpinBox(); self._cpu_quota_normal.setRange(1, 100);      self._cpu_quota_normal.setSuffix(" %")
            self._io_weight_throttled = QSpinBox(); self._io_weight_throttled.setRange(1, 10000)
            self._io_weight_normal    = QSpinBox(); self._io_weight_normal.setRange(1, 10000)
            cgf.addRow("CPU quota (throttled):", self._cpu_quota_throttled)
            cgf.addRow("CPU quota (normal):", self._cpu_quota_normal)
            cgf.addRow("IO weight (throttled):", self._io_weight_throttled)
            cgf.addRow("IO weight (normal):", self._io_weight_normal)
            layout.addWidget(cg)

            # ── Exclusions ───────────────────────────────────────────────
            excl_card = Card("Exclusions")
            excl_layout = excl_card.body()
            excl_layout.addWidget(_gh_body_label(
                "One entry per line. Case-insensitive prefix match on process name.",
                muted=True
            ))
            self._never_touch = self._make_list_editor(
                "Never touch (always ignored):", excl_layout
            )
            self._gaming_markers = self._make_list_editor(
                "Gaming markers (searched in name + full cmdline):", excl_layout
            )
            self._system_critical = self._make_list_editor(
                "System critical (kernel threads, 1.5× threshold):", excl_layout
            )
            layout.addWidget(excl_card)

            # ── Logging ──────────────────────────────────────────────────
            lg = Card("Logging")
            lgl = QFormLayout(); lg.set_body_layout(lgl)
            self._log_level = QComboBox()
            self._log_level.addItems(["DEBUG","INFO","WARNING","ERROR","CRITICAL"])
            lgl.addRow("Log level:", self._log_level)
            layout.addWidget(lg)

            # ── Advanced Tuning ──────────────────────────────────────────
            adv = Card("Advanced Tuning")
            af = QFormLayout(); adv.set_body_layout(af)
            self._stale_metrics_age  = QSpinBox(); self._stale_metrics_age.setRange(10, 3600);  self._stale_metrics_age.setSuffix(" s")
            self._habit_save_iv      = QSpinBox(); self._habit_save_iv.setRange(5, 3600);       self._habit_save_iv.setSuffix(" s")
            self._metrics_cleanup_iv = QSpinBox(); self._metrics_cleanup_iv.setRange(5, 3600);  self._metrics_cleanup_iv.setSuffix(" s")
            self._throttle_nice      = QSpinBox(); self._throttle_nice.setRange(0, 19)
            self._ionice_class       = QComboBox(); self._ionice_class.addItems(["0 - None","1 - Realtime","2 - Best-effort","3 - Idle"])
            self._ionice_priority    = QSpinBox(); self._ionice_priority.setRange(0, 7)
            self._high_load_thresh   = QSpinBox(); self._high_load_thresh.setRange(1, 100); self._high_load_thresh.setSuffix(" %")
            self._low_load_thresh    = QSpinBox(); self._low_load_thresh.setRange(1, 100);  self._low_load_thresh.setSuffix(" %")
            af.addRow("Stale metrics age:", self._stale_metrics_age)
            af.addRow("Habit save interval:", self._habit_save_iv)
            af.addRow("Metrics cleanup interval:", self._metrics_cleanup_iv)
            af.addRow("Throttle nice value:", self._throttle_nice)
            af.addRow("Throttle ionice class:", self._ionice_class)
            af.addRow("Throttle ionice priority:", self._ionice_priority)
            af.addRow("High load threshold (→ min interval):", self._high_load_thresh)
            af.addRow("Low load threshold (→ max interval):", self._low_load_thresh)
            layout.addWidget(adv)

            # ── Raw YAML fallback ────────────────────────────────────────
            raw_lbl = QLabel("RAW YAML (form fields take priority on Save)")
            raw_lbl.setStyleSheet(f"color:{COLORS['accent']};font-size:11px;font-weight:700;letter-spacing:1px;")
            layout.addWidget(raw_lbl)
            self._raw_edit = QPlainTextEdit()
            self._raw_edit.setMaximumHeight(150)
            self._raw_edit.setPlaceholderText("Additional YAML keys (optional)…")
            layout.addWidget(self._raw_edit)

            btn_row = QHBoxLayout()
            btn_load = QPushButton("↺  Reload from Disk")
            btn_save = GlowButton("💾  Save Config", "primary")
            btn_load.clicked.connect(self._load_config_ui)
            btn_save.clicked.connect(self._save_config)
            btn_row.addWidget(btn_load); btn_row.addWidget(btn_save); btn_row.addStretch()
            layout.addLayout(btn_row)

            layout.addWidget(gt_separator())
            ks = Card("Emergency Kill Switch")
            ksl = ks.body()
            ksl.addWidget(QLabel(f"Creating {GT_KILL_SWITCH} pauses all throttling without stopping the daemon."))
            ks_row = QHBoxLayout()
            btn_on  = QPushButton("Create Kill Switch")
            btn_off = QPushButton("Remove Kill Switch")
            btn_on.clicked.connect(lambda: gt_run_bg(gt_pkexec_run, ["touch", str(GT_KILL_SWITCH)], on_result=lambda ok, m: None))
            btn_off.clicked.connect(lambda: gt_run_bg(gt_pkexec_run, ["rm", "-f", str(GT_KILL_SWITCH)], on_result=lambda ok, m: None))
            ks_row.addWidget(btn_on); ks_row.addWidget(btn_off)
            ksl.addLayout(ks_row)
            layout.addWidget(ks)

        outer.addWidget(gt_scrolled(build))
        self._load_config_ui()

    @staticmethod
    def _make_list_editor(label: str, parent_layout) -> QPlainTextEdit:
        """Build a labeled, one-entry-per-line editor for a YAML list of strings."""
        lbl = QLabel(label)
        lbl.setStyleSheet(f"color:{COLORS['text_secondary']}; font-size:12px; font-weight:600;")
        parent_layout.addWidget(lbl)
        edit = QPlainTextEdit()
        edit.setMaximumHeight(90)
        parent_layout.addWidget(edit)
        return edit

    @staticmethod
    def _list_to_text(items) -> str:
        if not isinstance(items, list):
            return ""
        return "\n".join(str(i) for i in items)

    @staticmethod
    def _text_to_list(text: str) -> list:
        return [line.strip() for line in text.splitlines() if line.strip()]

    def _load_config(self):
        if GT_SENTRY_CONFIG.exists():
            try:
                loaded = _yaml_safe_load(GT_SENTRY_CONFIG.read_text())
                self._cfg = loaded if isinstance(loaded, dict) else {}
            except Exception:
                self._cfg = {}
        else:
            self._cfg = {}

    def _load_config_ui(self):
        self._load_config()
        c = self._cfg
        def fv(d, key, default):
            try: return float(d.get(key, default))
            except (TypeError, ValueError): return float(default)
        def iv(d, key, default):
            try: return int(d.get(key, default))
            except (TypeError, ValueError): return int(default)

        ivs = c.get("intervals", {})
        self._poll_base.setValue(fv(ivs,"base",5.0))
        self._poll_min.setValue(fv(ivs,"min",1.0))
        self._poll_max.setValue(fv(ivs,"max",15.0))
        self._ema_alpha.setValue(fv(ivs,"ema_alpha",0.3))

        t = c.get("thresholds", {})
        self._cpu_thresh.setValue(fv(t,"cpu_percent",50.0))
        self._io_thresh.setValue(fv(t,"io_mbps",25.0))
        self._mem_thresh.setValue(fv(t,"memory_percent",18.0))
        self._psi_thresh.setValue(fv(t,"memory_pressure_avg10",10.0))

        pred = c.get("prediction", {})
        self._history_size.setValue(iv(pred,"history_size",10))
        self._habit_conf.setValue(fv(pred,"pre_throttle_confidence",0.75))
        self._pattern_match.setValue(fv(pred,"pattern_match_threshold",0.8))

        lrn = c.get("learning", {})
        self._up_rate.setValue(fv(lrn,"up_rate",0.08))
        self._down_rate.setValue(fv(lrn,"down_rate",0.03))
        self._habit_cap.setValue(fv(lrn,"habit_cap",1.0))
        self._decay_rate.setValue(fv(lrn,"decay_rate",0.0002))

        cg = c.get("cgroup", {})
        self._cpu_quota_throttled.setValue(iv(cg,"cpu_quota_throttled",70))
        self._cpu_quota_normal.setValue(iv(cg,"cpu_quota_normal",100))
        self._io_weight_throttled.setValue(iv(cg,"io_weight_throttled",10))
        self._io_weight_normal.setValue(iv(cg,"io_weight_normal",100))

        excl = c.get("exclusions", {})
        self._never_touch.setPlainText(self._list_to_text(excl.get("never_touch_prefixes", [])))
        self._gaming_markers.setPlainText(self._list_to_text(excl.get("gaming_markers", [])))
        self._system_critical.setPlainText(self._list_to_text(excl.get("system_critical", [])))

        lg = c.get("logging", {})
        idx = self._log_level.findText(lg.get("level","INFO"))
        if idx >= 0: self._log_level.setCurrentIndex(idx)

        adv = c.get("advanced", {})
        self._stale_metrics_age.setValue(iv(adv,"stale_metrics_age",300))
        self._habit_save_iv.setValue(iv(adv,"habit_save_interval",180))
        self._metrics_cleanup_iv.setValue(iv(adv,"metrics_cleanup_interval",60))
        self._throttle_nice.setValue(iv(adv,"throttle_nice",15))
        self._ionice_class.setCurrentIndex(max(0, min(3, iv(adv,"throttle_ionice_class",2))))
        self._ionice_priority.setValue(iv(adv,"throttle_ionice_priority",7))
        self._high_load_thresh.setValue(iv(adv,"high_load_threshold",80))
        self._low_load_thresh.setValue(iv(adv,"low_load_threshold",20))

        try: self._raw_edit.setPlainText(_yaml_dump(self._cfg) if self._cfg else "")
        except Exception as e: self._raw_edit.setPlainText(f"# PyYAML not available: {e}")

    def _save_config(self):
        raw_text = self._raw_edit.toPlainText().strip()
        if raw_text:
            try:
                base = _yaml_safe_load(raw_text)
                if not isinstance(base, dict):
                    QMessageBox.critical(self, "YAML Error", "Must be a mapping (dict)."); return
            except Exception as exc:
                QMessageBox.critical(self, "YAML Error", str(exc)); return
        else:
            base = dict(self._cfg) if self._cfg else {}

        base.setdefault("intervals", {})
        base["intervals"].update({
            "base": self._poll_base.value(), "min": self._poll_min.value(),
            "max": self._poll_max.value(), "ema_alpha": self._ema_alpha.value(),
        })

        base.setdefault("thresholds", {})
        base["thresholds"].update({
            "cpu_percent": self._cpu_thresh.value(), "io_mbps": self._io_thresh.value(),
            "memory_percent": self._mem_thresh.value(),
            "memory_pressure_avg10": self._psi_thresh.value(),
        })

        base.setdefault("prediction", {})
        base["prediction"].update({
            "history_size": self._history_size.value(),
            "pre_throttle_confidence": self._habit_conf.value(),
            "pattern_match_threshold": self._pattern_match.value(),
        })

        base.setdefault("learning", {})
        base["learning"].update({
            "up_rate": self._up_rate.value(), "down_rate": self._down_rate.value(),
            "habit_cap": self._habit_cap.value(), "decay_rate": self._decay_rate.value(),
        })

        base.setdefault("cgroup", {})
        base["cgroup"].update({
            "cpu_quota_throttled": self._cpu_quota_throttled.value(),
            "cpu_quota_normal": self._cpu_quota_normal.value(),
            "io_weight_throttled": self._io_weight_throttled.value(),
            "io_weight_normal": self._io_weight_normal.value(),
        })

        base.setdefault("exclusions", {})
        base["exclusions"].update({
            "never_touch_prefixes": self._text_to_list(self._never_touch.toPlainText()),
            "gaming_markers": self._text_to_list(self._gaming_markers.toPlainText()),
            "system_critical": self._text_to_list(self._system_critical.toPlainText()),
        })

        base.setdefault("logging", {})
        base["logging"]["level"] = self._log_level.currentText()

        base.setdefault("advanced", {})
        base["advanced"].update({
            "stale_metrics_age": self._stale_metrics_age.value(),
            "habit_save_interval": self._habit_save_iv.value(),
            "metrics_cleanup_interval": self._metrics_cleanup_iv.value(),
            "throttle_nice": self._throttle_nice.value(),
            "throttle_ionice_class": self._ionice_class.currentIndex(),
            "throttle_ionice_priority": self._ionice_priority.value(),
            "high_load_threshold": self._high_load_thresh.value(),
            "low_load_threshold": self._low_load_thresh.value(),
        })

        try: content = _yaml_dump(base)
        except Exception as exc: QMessageBox.critical(self, "YAML Error", str(exc)); return
        gt_run_bg(gt_pkexec_write, GT_SENTRY_CONFIG, content,
            on_result=lambda ok, e: QMessageBox.information(self,"Saved","Config saved. Restart service.") if ok
                else QMessageBox.critical(self,"Error","Failed: "+e))


def gt_read_kernel_autotune_state() -> tuple[dict | None, str]:
    """Read and parse /etc/kernel-autotune/state.json. Returns (state, error)."""
    if not GT_KERNEL_STATE.exists():
        return None, "state.json not found — run Kernel Autotune at least once"
    try:
        return json.loads(GT_KERNEL_STATE.read_text()), ""
    except Exception as e:
        return None, f"could not parse state.json: {e}"


def gt_format_kernel_autotune_full(s: dict) -> str:
    """Full system-info rendering (host/CPU/GPU/detected facts) — used by the Kernel Autotune tab."""
    cfg = s.get("config", {})
    applied = s.get("applied_steps", []) or []
    failed  = s.get("failed_steps", []) or []
    lines = [
        f"Host:        {s.get('hostname','?')}   ({s.get('distro','?')})",
        f"Kernel:      {s.get('kernel_version', s.get('kernel','?'))}   [{s.get('device_type','?')}]",
        f"CPU:         {s.get('cpu_model','?')}  ({s.get('cpu_cores','?')} cores)",
        f"GPU:         {s.get('gpu_model','?')}   driver {s.get('gpu_driver','?')}",
        f"RAM:         {s.get('ram_mb','?')} MB",
        f"SSD / NUMA / Thermal:  "
        f"{'yes' if s.get('has_ssd') else 'no'} / "
        f"{'yes' if s.get('is_numa') else 'no'} / "
        f"{'yes' if s.get('has_thermal') else 'no'}",
        "",
        f"Last applied:  {s.get('last_updated','?')}"
        f"  ({'complete' if s.get('apply_complete') else 'incomplete'}, "
        f"{s.get('last_apply_duration','?')}s)",
        f"Applied steps: {', '.join(applied) if applied else '—'}",
        f"Failed steps:  {', '.join(failed) if failed else '—'}",
        "",
        f"Live config snapshot (last applied values):",
    ]
    for k, v in cfg.items():
        lines.append(f"  {k}: {v}")
    return "\n".join(lines)


def gt_format_kernel_autotune_summary(s: dict) -> str:
    """Condensed rendering (last apply + config snapshot only) — used by the Dashboard."""
    cfg = s.get("config", {})
    applied = s.get("applied_steps", []) or []
    failed  = s.get("failed_steps", []) or []
    lines = [
        f"Last applied:  {s.get('last_updated','?')}"
        f"  ({'complete' if s.get('apply_complete') else 'incomplete'}, "
        f"{s.get('last_apply_duration','?')}s)",
        f"Applied steps: {', '.join(applied) if applied else '—'}",
        f"Failed steps:  {', '.join(failed) if failed else '—'}",
        "",
        f"Live config snapshot (last applied values):",
    ]
    for k, v in cfg.items():
        lines.append(f"  {k}: {v}")
    return "\n".join(lines)


class _GtKernelTab(QWidget):
    """Kernel Autotune — service control, config.sh editor, state viewer."""
    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        def build(layout):
            layout.addWidget(gt_info_box(
                "Kernel Autotune applies low-level kernel parameters at boot: CPU governor, "
                "memory settings, huge pages, zram, I/O scheduler, and network tuning. "
                "Auto-detects hardware and kernel variant. Runs once at boot and exits."
            ))
            svc_grp = Card("Kernel Autotune Service")
            sl = svc_grp.body()
            self._svc_status = GtServiceStatusWidget(GT_KERNEL_SERVICE, "Last run status")
            sl.addWidget(self._svc_status)
            boot_row = QHBoxLayout()
            btn_rerun   = QPushButton("↻  Re-run Now")
            btn_enable  = QPushButton("Enable at Boot")
            btn_disable = QPushButton("Disable at Boot")
            btn_rerun.clicked.connect(self._rerun)
            btn_enable.clicked.connect(lambda: gt_run_bg(gt_systemctl_action,"enable",GT_KERNEL_SERVICE,
                on_result=lambda ok,m: QMessageBox.information(self,"Enable","Enabled." if ok else "Failed: "+m)))
            btn_disable.clicked.connect(lambda: gt_run_bg(gt_systemctl_action,"disable",GT_KERNEL_SERVICE,
                on_result=lambda ok,m: QMessageBox.information(self,"Disable","Disabled." if ok else "Failed: "+m)))
            for b in (btn_rerun, btn_enable, btn_disable): boot_row.addWidget(b)
            boot_row.addStretch()
            sl.addLayout(boot_row)
            layout.addWidget(svc_grp)

            layout.addWidget(gt_separator())
            info_card = Card("System Info  (read-only — from state.json)")
            info_layout = info_card.body()
            self._state_display = QPlainTextEdit()
            self._state_display.setReadOnly(True)
            self._state_display.setMaximumHeight(150)
            self._state_display.setStyleSheet(
                f"background:{COLORS['bg_darkest']};color:{COLORS['text_secondary']};"
                f"font-family:monospace;font-size:11px;border:none;"
            )
            info_layout.addWidget(self._state_display)
            layout.addWidget(info_card)

            cfg_lbl = QLabel(f"CONFIG FILE: {GT_KERNEL_CONFIG}  (yours to edit)")
            cfg_lbl.setStyleSheet(f"color:{COLORS['accent']};font-size:11px;font-weight:700;letter-spacing:1px;")
            layout.addWidget(cfg_lbl)

            mem = Card("Memory")
            mf = QFormLayout(); mem.set_body_layout(mf)
            self._cfg_fields: dict[str, QSpinBox] = {}
            for key, default, lo, hi, tip in [
                ("SWAPPINESS",         15,  0,   200, "0=never swap, 60=default, >100=zswap-aggressive"),
                ("DIRTY_RATIO",        15,  1,    90, "Max % RAM for dirty write cache"),
                ("DIRTY_BG_RATIO",      5,  1,    50, "% RAM before background flush (must be < DIRTY_RATIO)"),
                ("DIRTY_WRITEBACK_CS",1500, 0, 10000, "Centiseconds between writeback flusher wakeups (vm.dirty_writeback_centisecs)"),
                ("DIRTY_EXPIRE_CS",   3000, 0, 30000, "Centiseconds before dirty data is eligible for writeback (vm.dirty_expire_centisecs)"),
                ("VFS_CACHE_PRESSURE", 60,  0,  1000, "Lower=more caching, 100=default"),
            ]:
                sb = QSpinBox(); sb.setRange(lo, hi); sb.setValue(default); sb.setToolTip(tip)
                self._cfg_fields[key] = sb
                mf.addRow(key + ":", sb)
            layout.addWidget(mem)

            cpu = Card("CPU")
            cf = QFormLayout(); cpu.set_body_layout(cf)
            self._cfg_governor = QComboBox(); self._cfg_governor.addItems(["schedutil","performance","ondemand","conservative","powersave"])
            self._cfg_thp      = QComboBox(); self._cfg_thp.addItems(["madvise","always","never","defer+madvise"])
            cf.addRow("CPU Governor:", self._cfg_governor)
            cf.addRow("Transparent Huge Pages:", self._cfg_thp)
            layout.addWidget(cpu)

            zram = Card("ZRAM / Swap")
            zf = QFormLayout(); zram.set_body_layout(zf)
            self._cfg_zram_enabled  = QCheckBox("Enable ZRAM")
            self._cfg_zram_ratio    = QSpinBox(); self._cfg_zram_ratio.setRange(10,300); self._cfg_zram_ratio.setSuffix(" %")
            self._cfg_zram_algo     = QComboBox(); self._cfg_zram_algo.addItems(["zstd","lz4","lzo","lz4hc"])
            self._cfg_zswap_enabled = QCheckBox("Enable ZSWAP (hybrid mode)")
            zf.addRow("", self._cfg_zram_enabled)
            zf.addRow("ZRAM size (% of RAM):", self._cfg_zram_ratio)
            zf.addRow("Compression algorithm:", self._cfg_zram_algo)
            zf.addRow("", self._cfg_zswap_enabled)
            layout.addWidget(zram)

            sysc = Card("System")
            scf = QFormLayout(); sysc.set_body_layout(scf)
            self._cfg_numa_balancing = QCheckBox("Enable NUMA balancing")
            self._cfg_compact_uneviction = QCheckBox("Compact unevictable memory")
            scf.addRow("", self._cfg_numa_balancing)
            scf.addRow("", self._cfg_compact_uneviction)
            layout.addWidget(sysc)

            io = Card("I/O Scheduler")
            iof = QFormLayout(); io.set_body_layout(iof)
            self._cfg_io_ssd = QComboBox(); self._cfg_io_ssd.addItems(["none","mq-deadline","kyber","bfq"])
            self._cfg_io_hdd = QComboBox(); self._cfg_io_hdd.addItems(["bfq","mq-deadline","kyber","none"])
            iof.addRow("SSD scheduler:", self._cfg_io_ssd)
            iof.addRow("HDD scheduler:", self._cfg_io_hdd)
            layout.addWidget(io)

            net = Card("Network")
            nf = QFormLayout(); net.set_body_layout(nf)
            self._cfg_tcp   = QComboBox(); self._cfg_tcp.addItems(["bbr","cubic","reno","htcp"])
            self._cfg_qdisc = QComboBox(); self._cfg_qdisc.addItems(["fq","fq_codel","cake","pfifo_fast"])
            nf.addRow("TCP Congestion Control:", self._cfg_tcp)
            nf.addRow("Default Qdisc:", self._cfg_qdisc)
            layout.addWidget(net)

            raw_lbl2 = QLabel("RAW CONFIG (config.sh — form fields take priority on Save)")
            raw_lbl2.setStyleSheet(f"color:{COLORS['accent']};font-size:11px;font-weight:700;letter-spacing:1px;")
            layout.addWidget(raw_lbl2)
            self._raw_cfg = QPlainTextEdit()
            self._raw_cfg.setMaximumHeight(150)
            self._raw_cfg.setPlaceholderText("Additional shell config variables (optional)…")
            layout.addWidget(self._raw_cfg)

            btn_row2 = QHBoxLayout()
            btn_load  = QPushButton("↺  Reload from Disk")
            btn_save  = GlowButton("💾  Save Config", "primary")
            btn_regen = QPushButton("⟳  Regenerate Auto-Config")
            btn_load.clicked.connect(self._load_config_ui)
            btn_save.clicked.connect(self._save_config)
            btn_regen.clicked.connect(self._regen_config)
            for b in (btn_load, btn_save, btn_regen): btn_row2.addWidget(b)
            btn_row2.addStretch()
            layout.addLayout(btn_row2)

            layout.addWidget(gt_separator())
            revert = Card("Reset to Hardware-Detected Defaults")
            rl = revert.body()
            rl.addWidget(gt_warn_box(f"Deletes {GT_KERNEL_CONFIG} and state.json, re-detects hardware, regenerates, and re-applies."))
            btn_reset = QPushButton("Reset & Regenerate from Scratch")
            btn_reset.clicked.connect(self._reset_config)
            rl.addWidget(btn_reset)
            layout.addWidget(revert)

        outer.addWidget(gt_scrolled(build))
        self._load_config_ui()

    def _read_config_sh(self):
        if not GT_KERNEL_CONFIG.exists(): return {}, ""
        raw = GT_KERNEL_CONFIG.read_text()
        cfg: dict[str, str] = {}
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line: continue
            k, _, v = line.partition("=")
            cfg[k.strip()] = v.strip().strip('"').strip("'")
        return cfg, raw

    def _load_state(self):
        s, err = gt_read_kernel_autotune_state()
        if s is None:
            self._state_display.setPlainText(f"({err})")
            return
        self._state_display.setPlainText(gt_format_kernel_autotune_full(s))

    def _load_config_ui(self):
        self._load_state()
        cfg, raw = self._read_config_sh()
        def si(key, default):
            try: return int(cfg.get(key, default))
            except (ValueError, TypeError): return int(default)
        for key, default in [
            ("SWAPPINESS",15), ("DIRTY_RATIO",15), ("DIRTY_BG_RATIO",5),
            ("DIRTY_WRITEBACK_CS",1500), ("DIRTY_EXPIRE_CS",3000),
            ("VFS_CACHE_PRESSURE",60),
        ]:
            self._cfg_fields[key].setValue(si(key, default))
        def sc(combo, key, default):
            val = cfg.get(key, default)
            idx = combo.findText(val)
            if idx >= 0: combo.setCurrentIndex(idx)
        sc(self._cfg_governor,"CPU_GOVERNOR","schedutil")
        sc(self._cfg_thp,"THP","madvise")
        sc(self._cfg_zram_algo,"ZRAM_ALGO","zstd")
        sc(self._cfg_io_ssd,"IO_SCHEDULER_SSD","none")
        sc(self._cfg_io_hdd,"IO_SCHEDULER_HDD","bfq")
        sc(self._cfg_tcp,"TCP_CONGESTION","bbr")
        sc(self._cfg_qdisc,"NET_QDISC","fq")
        self._cfg_zram_enabled.setChecked(cfg.get("ZRAM_ENABLED","1") == "1")
        self._cfg_zswap_enabled.setChecked(cfg.get("ZSWAP_ENABLED","0") == "1")
        self._cfg_numa_balancing.setChecked(cfg.get("NUMA_BALANCING","0") == "1")
        self._cfg_compact_uneviction.setChecked(cfg.get("COMPACT_UNEVICTABLE","1") == "1")
        try: self._cfg_zram_ratio.setValue(int(cfg.get("ZRAM_RATIO",50)))
        except (ValueError, TypeError): self._cfg_zram_ratio.setValue(50)
        self._raw_cfg.setPlainText(raw)

    def _save_config(self):
        dirty = self._cfg_fields["DIRTY_RATIO"].value()
        dirty_bg = self._cfg_fields["DIRTY_BG_RATIO"].value()
        if dirty_bg >= dirty:
            QMessageBox.warning(self,"Invalid Config",
                f"DIRTY_BG_RATIO ({dirty_bg}) must be < DIRTY_RATIO ({dirty})."); return
        raw_extra: dict[str, str] = {}
        for line in self._raw_cfg.toPlainText().splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line: continue
            k, _, v = line.partition("=")
            raw_extra[k.strip()] = v.strip()
        form_vals: dict[str, str] = {
            "SWAPPINESS": str(self._cfg_fields["SWAPPINESS"].value()),
            "DIRTY_RATIO": str(self._cfg_fields["DIRTY_RATIO"].value()),
            "DIRTY_BG_RATIO": str(self._cfg_fields["DIRTY_BG_RATIO"].value()),
            "DIRTY_WRITEBACK_CS": str(self._cfg_fields["DIRTY_WRITEBACK_CS"].value()),
            "DIRTY_EXPIRE_CS": str(self._cfg_fields["DIRTY_EXPIRE_CS"].value()),
            "VFS_CACHE_PRESSURE": str(self._cfg_fields["VFS_CACHE_PRESSURE"].value()),
            "CPU_GOVERNOR": f'"{self._cfg_governor.currentText()}"',
            "THP": f'"{self._cfg_thp.currentText()}"',
            "ZRAM_ENABLED": "1" if self._cfg_zram_enabled.isChecked() else "0",
            "ZRAM_RATIO": str(self._cfg_zram_ratio.value()),
            "ZRAM_ALGO": f'"{self._cfg_zram_algo.currentText()}"',
            "ZSWAP_ENABLED": "1" if self._cfg_zswap_enabled.isChecked() else "0",
            "NUMA_BALANCING": "1" if self._cfg_numa_balancing.isChecked() else "0",
            "COMPACT_UNEVICTABLE": "1" if self._cfg_compact_uneviction.isChecked() else "0",
            "IO_SCHEDULER_SSD": f'"{self._cfg_io_ssd.currentText()}"',
            "IO_SCHEDULER_HDD": f'"{self._cfg_io_hdd.currentText()}"',
            "TCP_CONGESTION": f'"{self._cfg_tcp.currentText()}"',
            "NET_QDISC": f'"{self._cfg_qdisc.currentText()}"',
        }
        raw_extra.update(form_vals)
        lines = ["# Griffin Hub — Kernel Autotune config"]
        for k, v in raw_extra.items(): lines.append(k + "=" + v)
        content = "\n".join(lines) + "\n"
        gt_run_bg(gt_pkexec_write, GT_KERNEL_CONFIG, content,
            on_result=lambda ok, e: QMessageBox.information(self,"Saved","Config saved. Re-run Now to apply.") if ok
                else QMessageBox.critical(self,"Error","Failed: "+e))

    def _rerun(self):
        dlg = GtConfirmDialog("Re-run Kernel Autotune","Apply current kernel config immediately.",
            [f"Runs: {GT_KERNEL_BIN} apply","Applies sysctl, CPU governor, zram now"],
            reversible=False, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            gt_run_bg(gt_pkexec_run, [str(GT_KERNEL_BIN), "apply"],
                on_result=lambda ok, m: (self._svc_status.refresh(),
                    QMessageBox.information(self,"Done","Applied.") if ok
                    else QMessageBox.critical(self,"Error",m or "Run failed.")) and None)

    def _regen_config(self):
        dlg = GtConfirmDialog("Regenerate Config","Re-detect hardware and regenerate config.sh.",
            [f"Runs: {GT_KERNEL_BIN} generate", f"Overwrites {GT_KERNEL_CONFIG}"],
            reversible=False, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            gt_run_bg(gt_pkexec_run, [str(GT_KERNEL_BIN),"generate"],
                on_result=lambda ok,m: (self._load_config_ui(),
                    QMessageBox.information(self,"Done","Config regenerated.") if ok
                    else QMessageBox.critical(self,"Error",m)) and None)

    def _reset_config(self):
        dlg = GtConfirmDialog("Reset Kernel Autotune",
            "Delete config, re-detect hardware, regenerate, and re-apply.",
            [f"Delete {GT_KERNEL_CONFIG}", f"Delete {GT_KERNEL_STATE}",
             f"Run: {GT_KERNEL_BIN} generate", f"Run: {GT_KERNEL_BIN} apply"],
            reversible=False, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted: return
        ok_rm, err_rm = gt_pkexec_run(["rm","-f",str(GT_KERNEL_CONFIG),str(GT_KERNEL_STATE)])
        if not ok_rm:
            QMessageBox.critical(self,"Error",f"Failed to delete config: {err_rm}"); return
        def on_gen(ok, msg):
            if not ok: QMessageBox.critical(self,"Error",f"Generate failed: {msg}"); return
            gt_run_bg(gt_pkexec_run,[str(GT_KERNEL_BIN),"apply"],
                on_result=lambda ok2,m: (self._load_config_ui(),
                    QMessageBox.information(self,"Done","Reset and reapplied.") if ok2
                    else QMessageBox.critical(self,"Error",f"Apply failed: {m}")) and None)
        gt_run_bg(gt_pkexec_run,[str(GT_KERNEL_BIN),"generate"],on_result=on_gen)


class _GtSetupTab(QWidget):
    """Setup & Installation."""
    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        def build(layout):
            layout.addWidget(gt_info_box(
                "Install required packages, systemd service files, polkit rules, and udev rules. "
                "All actions are explained before being applied. Requires admin password (pkexec)."
            ))
            pkg_grp = Card("Required Packages")
            pkg_l = pkg_grp.body()
            packages = ["gamemode","gamescope","mangohud","python3-psutil","python3-yaml"]
            packages += [p for p in REGISTRY.targets["tune_setup_packages"]["packages"] if p not in packages]
            self._pkg_rows: dict = {}
            for pkg in packages:
                row  = QHBoxLayout()
                dot   = QLabel("●"); dot.setFont(QFont("monospace",14))
                label = QLabel(pkg)
                state = QLabel(); state.setStyleSheet("font-size:12px;")
                row.addWidget(dot); row.addWidget(label); row.addStretch(); row.addWidget(state)
                pkg_l.addLayout(row)
                self._pkg_rows[pkg] = (dot, state)
            row_btns = QHBoxLayout()
            row_btns.addWidget(QPushButton("Refresh Status").also(
                lambda b: b.clicked.connect(self._refresh_packages)) if False
                else self._make_btn("Refresh Package Status", self._refresh_packages))
            row_btns.addWidget(self._make_btn("Install Missing Packages (apt)",
                lambda: self._install_packages(packages)))
            pkg_l.addLayout(row_btns)
            layout.addWidget(pkg_grp)

            svc_grp = Card("Install Service Files")
            svc_l = svc_grp.body()
            svc_l.addWidget(QLabel(
                "Installs resource-sentry.service and kernel-autotune.service into "
                "/etc/systemd/system/ and reloads the daemon."
            ))
            svc_l.addWidget(gt_warn_box(
                f"ProcessSentry v3 must be at {GT_SENTRY_BIN}\n"
                f"Kernel Autotune must be at {GT_KERNEL_BIN}"
            ))
            svc_l.addWidget(self._make_btn("Install Service Files", self._install_services))
            layout.addWidget(svc_grp)

            for title, desc, info, fn in [
                ("Polkit Rules",
                 f"Rule file: {GT_POLKIT_RULE}\nGrants sudo group manage-units rights for Griffin Hub services.",
                 None, self._install_polkit),
                ("udev Rules",
                 f"Rule file: {GT_UDEV_RULES}\nGrants uaccess to gamepads and controllers.",
                 None, self._install_udev),
            ]:
                g = Card(title)
                gl = g.body()
                gl.addWidget(gt_info_box(desc))
                gl.addWidget(self._make_btn(f"Install {title}", fn))
                layout.addWidget(g)

            grp_grp = Card("GameMode User Group")
            grp_l = grp_grp.body()
            grp_l.addWidget(QLabel(
                "Your user must be in the 'gamemode' group for CPU governor changes. "
                "Requires logout/login."
            ))
            grp_l.addWidget(self._make_btn("Add Current User to gamemode Group", self._add_gamemode_group))
            layout.addWidget(grp_grp)

            self._output = QPlainTextEdit()
            self._output.setReadOnly(True)
            self._output.setMaximumHeight(140)
            self._output.setPlaceholderText("Setup output will appear here…")
            layout.addWidget(self._output)

        outer.addWidget(gt_scrolled(build))
        QTimer.singleShot(200, self._refresh_packages)

    @staticmethod
    def _make_btn(label: str, fn) -> QPushButton:
        b = QPushButton(label)
        b.clicked.connect(fn)
        return b

    def _log(self, msg: str): self._output.appendPlainText(msg)

    def _refresh_packages(self):
        for pkg, (dot, state) in self._pkg_rows.items():
            installed = gt_pkg_installed(pkg)
            c = COLORS["success"] if installed else "#e3b341"
            dot.setStyleSheet(f"color:{c};")
            state.setText("installed" if installed else "not installed")
            state.setStyleSheet(f"color:{c};font-size:12px;")

    def _install_packages(self, packages):
        missing = [p for p in packages if not gt_pkg_installed(p)]
        if not missing: QMessageBox.information(self,"All Installed","All packages already installed."); return
        dlg = GtConfirmDialog("Install Packages","Install via apt:",
            ["apt install -y " + " ".join(missing)], reversible=False, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            gt_run_bg(gt_pkexec_run, ["apt","install","-y"]+missing,
                on_result=lambda ok,m: (self._log("✓ Installed." if ok else "✗ "+m), self._refresh_packages()))

    def _install_services(self):
        sentry_svc = (
            "[Unit]\nDescription=Process Sentry v3\nAfter=network.target\n\n"
            "[Service]\nType=simple\n"
            f"ExecStart=/usr/bin/python3 {GT_SENTRY_BIN}\n"
            "Restart=always\nRestartSec=5\n\n[Install]\nWantedBy=multi-user.target\n"
        )
        kernel_svc = (
            "[Unit]\nDescription=Kernel Autotune v2\nAfter=local-fs.target\n\n"
            "[Service]\nType=oneshot\n"
            f"ExecStart={GT_KERNEL_BIN} apply\n"
            "RemainAfterExit=yes\n\n[Install]\nWantedBy=multi-user.target\n"
        )
        dlg = GtConfirmDialog("Install Service Files","Install systemd service files.",
            ["Write /etc/systemd/system/resource-sentry.service",
             "Write /etc/systemd/system/kernel-autotune.service",
             "Run: systemctl daemon-reload"],
            reversible=False, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted: return
        wrote = False
        for path_str, content in [
            ("/etc/systemd/system/resource-sentry.service", sentry_svc),
            ("/etc/systemd/system/kernel-autotune.service", kernel_svc),
        ]:
            ok, err = gt_pkexec_write(Path(path_str), content)
            self._log(("✓ " if ok else "✗ ") + path_str + (" " + err if err else ""))
            if ok: wrote = True
        if wrote:
            ok2, msg = gt_pkexec_run(["systemctl","daemon-reload"])
            self._log(("✓ " if ok2 else "✗ ") + "daemon-reload" + (" "+msg if msg else ""))

    def _install_polkit(self):
        rule = (
            "polkit.addRule(function(action, subject) {\n"
            "    if (action.id == \"org.freedesktop.systemd1.manage-units\" &&\n"
            "        subject.isInGroup(\"sudo\")) {\n"
            "        var unit = action.lookup(\"unit\");\n"
            "        if (unit == \"resource-sentry.service\" ||\n"
            "            unit == \"kernel-autotune.service\") {\n"
            "            return polkit.Result.YES; }\n"
            "    }\n});\n"
        )
        dlg = GtConfirmDialog("Install Polkit Rule",
            f"Grant sudo group manage-units rights for Griffin Hub services.",
            [f"Write {GT_POLKIT_RULE}"], reversible=False, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            ok, err = gt_pkexec_write(GT_POLKIT_RULE, rule)
            self._log("✓ polkit rule written." if ok else "✗ " + err)

    def _install_udev(self):
        rules = (
            "# Griffin Hub udev rules\n"
            "SUBSYSTEM==\"input\", ATTRS{name}==\"*Controller*\", TAG+=\"uaccess\"\n"
            "SUBSYSTEM==\"input\", ATTRS{name}==\"*Gamepad*\",    TAG+=\"uaccess\"\n"
            "SUBSYSTEM==\"input\", ATTRS{name}==\"*DualSense*\",  TAG+=\"uaccess\"\n"
            "SUBSYSTEM==\"input\", ATTRS{name}==\"*Xbox*\",       TAG+=\"uaccess\"\n"
        )
        dlg = GtConfirmDialog("Install udev Rules","Install udev rules for gaming hardware.",
            [f"Write {GT_UDEV_RULES}", "Run: udevadm control --reload-rules"],
            reversible=False, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            ok, err = gt_pkexec_write(GT_UDEV_RULES, rules)
            self._log("✓ udev rules written." if ok else "✗ " + err)
            if ok:
                ok2, msg = gt_pkexec_run(["udevadm","control","--reload-rules"])
                self._log(("✓ " if ok2 else "✗ ") + "reload-rules" + (" "+msg if msg else ""))

    def _add_gamemode_group(self):
        user = gt_real_user()
        if not user: QMessageBox.warning(self,"Error","Could not determine username."); return
        dlg = GtConfirmDialog("Add to gamemode Group",
            f"Add user '{user}' to the 'gamemode' group.",
            [f"usermod -aG gamemode {user}", "Requires logout/login"],
            reversible=False, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            gt_run_bg(gt_pkexec_run,["usermod","-aG","gamemode",user],
                on_result=lambda ok,m: (
                    self._log(("✓ " if ok else "✗ ")+"usermod: "+(m or "ok")),
                    QMessageBox.information(self,"Done",f"'{user}' added to gamemode group.\nLog out and back in.") if ok else None
                ) and None)



class SystemTunePage(QWidget):
    """
    System Tune — full GameTuneHub integration.
    Tabs: Overview | Steam Wrappers | ProcessSentry | Kernel Autotune | Live Tuning | Setup
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tune_worker: SystemTuneWorker | None = None
        self._build()
        self._load_ka_state()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(32, 28, 32, 28)
        root.setSpacing(16)

        hdr = QHBoxLayout()
        title = QLabel("System Tune")
        title.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: 22px; font-weight: 700;")
        hdr.addWidget(title)
        hdr.addStretch()
        self._ka_badge = QLabel()
        self._ka_badge.setStyleSheet(f"color:{COLORS['text_muted']};font-size:11px;")
        hdr.addWidget(self._ka_badge)
        refresh_btn = GlowButton("↻  Refresh", "secondary")
        refresh_btn.clicked.connect(self._load_ka_state)
        hdr.addWidget(refresh_btn)
        root.addLayout(hdr)
        root.addWidget(Divider())

        self._tabs = QTabWidget()
        root.addWidget(self._tabs)

        self._tabs.addTab(_GtOverviewTab(),   "Overview")
        self._tabs.addTab(_GtWrappersTab(),   "Steam Wrappers")
        self._tabs.addTab(_GtSentryTab(),     "ProcessSentry")

        # Kernel Autotune and Live Tuning are mutually exclusive — Live Tuning
        # exists only as a fallback for systems that don't have kernel-autotune
        # installed, so there's always exactly one "tune the kernel live" tab,
        # never both.
        self._kernel_slot_index = self._tabs.count()
        self._live_tuning_active = not self._kernel_autotune_installed()
        if self._live_tuning_active:
            self._tabs.addTab(self._build_live_tuning_tab(), "Live Tuning")
        else:
            self._tabs.addTab(_GtKernelTab(), "Kernel Autotune")

        self._tabs.addTab(_GtSetupTab(),      "Setup")

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        for label, fn, tip in [
            ("🎮 Gaming Preset",  self._apply_gaming_preset,
             "performance, swappiness=10, madvise, BBR+fq, ZRAM 25% zstd, mq-deadline"),
            ("🖥 Desktop Preset", self._apply_desktop_preset,
             "schedutil, swappiness=15, ZRAM 30% zstd, fq_codel"),
            ("🖧 Server Preset",  self._apply_server_preset,
             "schedutil, swappiness=10, no ZRAM, kyber, large net buffers"),
        ]:
            b = QPushButton(label); b.setToolTip(tip); b.clicked.connect(fn)
            btn_row.addWidget(b)
        self._apply_btn = GlowButton("⚡ Apply Custom Settings", "primary")
        self._apply_btn.clicked.connect(self._apply_custom)
        btn_row.addWidget(self._apply_btn)
        root.addLayout(btn_row)

        self._log_box = QPlainTextEdit()
        self._log_box.setReadOnly(True)
        self._log_box.setMaximumHeight(100)
        self._log_box.setStyleSheet(
            f"background:{COLORS['bg_card']};color:{COLORS['text_muted']};"
            f"font-family:monospace;font-size:11px;"
            f"border:1px solid {COLORS['border']};border-radius:4px;"
        )
        root.addWidget(self._log_box)

    @staticmethod
    def _kernel_autotune_installed() -> bool:
        """True if kernel-autotune is installed on this system (its binary is present)."""
        return GT_KERNEL_BIN.exists()

    def _sync_kernel_tab(self):
        """Swap between the Kernel Autotune tab and the Live Tuning fallback
        if installed-state changed since last build (e.g. Setup just installed
        kernel-autotune, or the user removed it)."""
        installed = self._kernel_autotune_installed()
        should_show_live_tuning = not installed
        if should_show_live_tuning == self._live_tuning_active:
            return  # no change
        idx = self._kernel_slot_index
        old_widget = self._tabs.widget(idx)
        self._tabs.removeTab(idx)
        old_widget.deleteLater()
        if should_show_live_tuning:
            self._tabs.insertTab(idx, self._build_live_tuning_tab(), "Live Tuning")
        else:
            self._tabs.insertTab(idx, _GtKernelTab(), "Kernel Autotune")
        self._live_tuning_active = should_show_live_tuning

    def _build_live_tuning_tab(self) -> QWidget:
        w   = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)

        def build(layout):
            layout.addWidget(gt_info_box(
                "Apply sysctl, CPU governor, I/O scheduler, ZRAM, and network settings "
                "immediately without rebooting. Values pre-populated from kernel-autotune "
                "state.json when available. All root writes use pkexec."
            ))
            form = QFormLayout()
            form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
            form.setSpacing(10)

            self._gov_combo = QComboBox()
            self._gov_combo.addItems(["schedutil","performance","powersave","ondemand","conservative","userspace"])
            self._gov_combo.setToolTip("schedutil: responsive kernel-managed (recommended)\nperformance: always max frequency")
            form.addRow("CPU Governor:", self._gov_combo)

            self._swappiness_spin = self._spin(0, 200, 60, "10 for gaming, 60 default, >100 zswap")
            self._dirty_ratio_spin  = self._spin(1, 80, 20, "Max % RAM for dirty write cache before forcing writes")
            self._dirty_bg_spin     = self._spin(1, 50, 10, "% RAM for dirty cache before background flush")
            self._vfs_spin          = self._spin(1, 500, 100, "Lower=more caching. 50-60 for gaming, 100 default")
            form.addRow("Swappiness:", self._swappiness_spin)
            form.addRow("Dirty Ratio %:", self._dirty_ratio_spin)
            form.addRow("Dirty BG Ratio %:", self._dirty_bg_spin)
            form.addRow("VFS Cache Pressure:", self._vfs_spin)

            self._thp_combo = QComboBox()
            self._thp_combo.addItems(["always","madvise","defer+madvise","never"])
            self._thp_combo.setToolTip("madvise=best for Wine/games, defer+madvise=Xanmod default")
            form.addRow("THP Mode:", self._thp_combo)

            self._tcp_combo = QComboBox()
            for a, t in [("bbr","BBR — gaming+streaming"),("bbr2","BBR v2 — Xanmod/Liquorix"),
                         ("cubic","CUBIC — Linux default"),("reno","Reno — conservative"),
                         ("htcp","H-TCP — fast LANs"),("cake","CAKE — all-in-one AQM")]:
                self._tcp_combo.addItem(a)
                self._tcp_combo.setItemData(self._tcp_combo.count()-1, t, Qt.ItemDataRole.ToolTipRole)
            form.addRow("TCP Congestion:", self._tcp_combo)

            self._qdisc_combo = QComboBox()
            for a, t in [("fq","fq — Fair Queue, best with BBR"),("fq_codel","fq_codel — CoDel AQM"),
                         ("cake","CAKE — advanced AQM"),("pfifo_fast","pfifo_fast — simple FIFO")]:
                self._qdisc_combo.addItem(a)
                self._qdisc_combo.setItemData(self._qdisc_combo.count()-1, t, Qt.ItemDataRole.ToolTipRole)
            form.addRow("Net Qdisc:", self._qdisc_combo)

            self._io_combo = QComboBox()
            for a, t in [("none","none — NVMe/fast SSD, no scheduler"),
                         ("mq-deadline","mq-deadline — SSDs, mixed workloads"),
                         ("kyber","kyber — NVMe, low-latency"),("bfq","BFQ — HDDs")]:
                self._io_combo.addItem(a)
                self._io_combo.setItemData(self._io_combo.count()-1, t, Qt.ItemDataRole.ToolTipRole)
            form.addRow("I/O Scheduler:", self._io_combo)

            zram_grp = Card("ZRAM")
            gz = QFormLayout(); zram_grp.set_body_layout(gz); gz.setSpacing(8)
            self._zram_enable    = QCheckBox("Enable ZRAM swap")
            self._zram_ratio_spin = self._spin(5, 100, 25, "% of total RAM for ZRAM. 25-50% typical.")
            self._zram_algo_combo = QComboBox()
            self._zram_algo_combo.addItems(["zstd","lz4","lzo","lzo-rle","lz4hc"])
            self._zram_algo_combo.setToolTip("zstd: best compression\nlz4: fastest")
            gz.addRow("", self._zram_enable)
            gz.addRow("RAM % for ZRAM:", self._zram_ratio_spin)
            gz.addRow("Compression:", self._zram_algo_combo)
            self._zram_enable.toggled.connect(self._zram_ratio_spin.setEnabled)
            self._zram_enable.toggled.connect(self._zram_algo_combo.setEnabled)
            form.addRow(zram_grp)

            self._net_rmem    = self._spin(65536, 67108864, 4194304, "net.core.rmem_max (bytes)")
            self._net_wmem    = self._spin(65536, 67108864, 4194304, "net.core.wmem_max (bytes)")
            self._net_backlog = self._spin(128, 65535, 1000, "net.core.netdev_max_backlog")
            form.addRow("rmem_max:", self._net_rmem)
            form.addRow("wmem_max:", self._net_wmem)
            form.addRow("netdev_max_backlog:", self._net_backlog)

            self._fstrim_cb = QCheckBox("Run fstrim on SSDs after apply")
            form.addRow("", self._fstrim_cb)
            layout.addLayout(form)

            self._kernel_tips = QLabel()
            self._kernel_tips.setWordWrap(True)
            self._kernel_tips.setStyleSheet(
                f"color:{COLORS['accent']};font-size:12px;"
                f"background:{COLORS['bg_card']};border-radius:4px;padding:8px;"
            )
            layout.addWidget(self._kernel_tips)

        lay.addWidget(gt_scrolled(build))
        return w

    @staticmethod
    def _spin(min_v: int, max_v: int, default: int, tip: str = "") -> QSpinBox:
        sb = QSpinBox()
        sb.setRange(min_v, max_v); sb.setValue(default)
        if tip: sb.setToolTip(tip)
        sb.setStyleSheet(
            f"background:{COLORS['bg_card']};color:{COLORS['text_primary']};"
            f"border:1px solid {COLORS['border']};border-radius:4px;padding:3px 6px;"
        )
        return sb

    def _load_ka_state(self):
        KERNEL_AUTOTUNE.load()
        ka = KERNEL_AUTOTUNE
        self._ka_badge.setText(
            f"kernel-autotune v{ka.script_version}  •  {ka.kernel}"
            if ka.available else "kernel-autotune: not detected"
        )
        self._sync_kernel_tab()
        if self._live_tuning_active and ka.available:
            self._set_combo(self._gov_combo, ka.cpu_governor)
            self._swappiness_spin.setValue(ka.swappiness)
            self._dirty_ratio_spin.setValue(ka.dirty_ratio)
            self._dirty_bg_spin.setValue(ka.dirty_bg_ratio)
            self._vfs_spin.setValue(ka.vfs_cache_pressure)
            self._set_combo(self._thp_combo, ka.thp)
            self._set_combo(self._tcp_combo, ka.tcp_congestion)
            self._set_combo(self._qdisc_combo, ka.net_qdisc)
            self._zram_enable.setChecked(ka.zram_enabled)
            self._zram_ratio_spin.setValue(ka.zram_ratio)
            self._set_combo(self._zram_algo_combo, ka.zram_algo)
            self._zram_ratio_spin.setEnabled(ka.zram_enabled)
            self._zram_algo_combo.setEnabled(ka.zram_enabled)
        if not self._live_tuning_active:
            return
        try:
            uname  = subprocess.check_output(["uname","-r"],timeout=3).decode().strip()
            flavor = ("xanmod" if "xanmod" in uname.lower()
                      else "liquorix" if ("liquorix" in uname.lower() or "lqx" in uname.lower())
                      else "zen" if "zen" in uname.lower() else "generic")
            tips = {
                "xanmod":   "Xanmod: use performance/schedutil, THP=defer+madvise, BBR2+fq, DXVK_ASYNC=1.",
                "liquorix": "Liquorix: use performance, swappiness=10, THP=madvise, BBR+fq.",
                "zen":      "Zen (Arch): use performance, THP=madvise, BBR+fq.",
                "generic":  "Generic kernel: consider Xanmod or Liquorix for better gaming latency.",
            }
            self._kernel_tips.setText(f"Running kernel: {uname}  •  {tips[flavor]}")
        except Exception:
            self._kernel_tips.setText("")

    @staticmethod
    def _set_combo(combo: QComboBox, value: str):
        idx = combo.findText(value)
        if idx >= 0: combo.setCurrentIndex(idx)

    def _log(self, msg: str):
        self._log_box.appendPlainText(msg)
        self._log_box.verticalScrollBar().setValue(self._log_box.verticalScrollBar().maximum())

    def _gaming_settings(self) -> dict:
        return {"sysctl":{"vm.swappiness":10,"vm.dirty_ratio":20,"vm.dirty_background_ratio":5,
            "vm.vfs_cache_pressure":50,"net.core.rmem_max":4194304,"net.core.wmem_max":4194304,
            "net.core.netdev_max_backlog":5000,"net.ipv4.tcp_fastopen":3},
            "cpu_governor":"performance","io_scheduler":"mq-deadline","thp":"madvise",
            "zram_ratio":25,"zram_algo":"zstd","tcp_congestion":"bbr","net_qdisc":"fq"}

    def _desktop_settings(self) -> dict:
        return {"sysctl":{"vm.swappiness":15,"vm.dirty_ratio":20,"vm.dirty_background_ratio":10,
            "vm.vfs_cache_pressure":60},
            "cpu_governor":"schedutil","io_scheduler":"mq-deadline","thp":"madvise",
            "zram_ratio":30,"zram_algo":"zstd","tcp_congestion":"bbr","net_qdisc":"fq_codel"}

    def _server_settings(self) -> dict:
        return {"sysctl":{"vm.swappiness":10,"vm.dirty_ratio":30,"vm.dirty_background_ratio":15,
            "vm.vfs_cache_pressure":100,"net.core.rmem_max":16777216,"net.core.wmem_max":16777216},
            "cpu_governor":"schedutil","io_scheduler":"kyber","thp":"defer+madvise",
            "zram_ratio":0,"tcp_congestion":"bbr","net_qdisc":"fq"}

    def _current_settings(self) -> dict:
        return {"sysctl":{"vm.swappiness":self._swappiness_spin.value(),
            "vm.dirty_ratio":self._dirty_ratio_spin.value(),
            "vm.dirty_background_ratio":self._dirty_bg_spin.value(),
            "vm.vfs_cache_pressure":self._vfs_spin.value(),
            "net.core.rmem_max":self._net_rmem.value(),
            "net.core.wmem_max":self._net_wmem.value(),
            "net.core.netdev_max_backlog":self._net_backlog.value()},
            "cpu_governor":self._gov_combo.currentText(),
            "io_scheduler":self._io_combo.currentText(),
            "thp":self._thp_combo.currentText(),
            "tcp_congestion":self._tcp_combo.currentText(),
            "net_qdisc":self._qdisc_combo.currentText(),
            "zram_ratio":self._zram_ratio_spin.value() if self._zram_enable.isChecked() else 0,
            "zram_algo":self._zram_algo_combo.currentText()}

    def _load_settings_into_ui(self, s: dict):
        sc = s.get("sysctl", {})
        for attr, key in [
            ("_swappiness_spin",  "vm.swappiness"),
            ("_dirty_ratio_spin", "vm.dirty_ratio"),
            ("_dirty_bg_spin",    "vm.dirty_background_ratio"),
            ("_vfs_spin",         "vm.vfs_cache_pressure"),
            ("_net_rmem",         "net.core.rmem_max"),
            ("_net_wmem",         "net.core.wmem_max"),
            ("_net_backlog",      "net.core.netdev_max_backlog"),
        ]:
            if key in sc: getattr(self, attr).setValue(sc[key])
        for combo_attr, key in [
            ("_gov_combo","cpu_governor"),("_io_combo","io_scheduler"),
            ("_thp_combo","thp"),("_tcp_combo","tcp_congestion"),("_qdisc_combo","net_qdisc"),
            ("_zram_algo_combo","zram_algo"),
        ]:
            if key in s: self._set_combo(getattr(self, combo_attr), s[key])
        zr = s.get("zram_ratio", 0)
        self._zram_enable.setChecked(zr > 0)
        if zr > 0: self._zram_ratio_spin.setValue(zr)

    def _apply_preset(self, s: dict, label: str):
        self._load_settings_into_ui(s)
        self._log(f"[SystemTune] {label} preset loaded — applying…")
        self._run_tune(s)

    def _apply_gaming_preset(self):  self._apply_preset(self._gaming_settings(),  "Gaming")
    def _apply_desktop_preset(self): self._apply_preset(self._desktop_settings(), "Desktop")
    def _apply_server_preset(self):  self._apply_preset(self._server_settings(),  "Server")
    def _apply_custom(self):         self._run_tune(self._current_settings())

    def _run_tune(self, settings: dict):
        if self._tune_worker and self._tune_worker.isRunning():
            self._log("[SystemTune] Already running — please wait."); return
        self._log_box.clear()
        self._log("[SystemTune] Starting — a password prompt may appear.")
        worker = SystemTuneWorker(settings)
        worker.log.connect(self._log)
        worker.finished.connect(self._on_tune_done)
        self._tune_worker = worker
        self._apply_btn.setEnabled(False)
        worker.start()

    def _on_tune_done(self, ok: bool, msg: str):
        self._log(f"\n[SystemTune] {'✓' if ok else '✗'}  {msg}")
        self._apply_btn.setEnabled(True)
        self._load_ka_state()
        if not ok: QMessageBox.warning(self, "System Tune", msg)






def _gh_run(cmd, shell=True, timeout=10):
    try:
        r = subprocess.run(cmd, shell=shell, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.returncode
    except Exception as e:
        return str(e), -1

def _gh_run_pkexec(cmd):
    """
    Run *cmd* (a shell string) elevated via pkexec.
    Uses the PolicyKit helper when installed. Quotes the payload safely so
    single quotes or special characters inside *cmd* cannot break the
    outer bash -c invocation.
    """
    # Escape any single quotes in the payload for a safe bash -c '...' wrap
    safe_cmd = cmd.replace("'", "'\\''")
    if GT_POLKIT_HELPER_PATH.exists():
        # Prefer the branded helper + list form (no outer shell)
        try:
            r = subprocess.run(
                ["pkexec", str(GT_POLKIT_HELPER_PATH), "bash", "-c", cmd],
                capture_output=True, text=True, timeout=120,
            )
            return r.stdout.strip(), r.returncode
        except Exception as e:
            return str(e), -1
    return _gh_run(f"pkexec bash -c '{safe_cmd}'", timeout=120)

def _mk_badge(text, color):
    """Thin wrapper so existing call sites can keep passing (text, color) positionally."""
    return StatusBadge(text, color=color, text=text)

def _gh_body_label(text, muted=False):
    c = COLORS['text_secondary'] if muted else COLORS['text_primary']
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setStyleSheet(f"color: {c}; font-size: 12px; line-height: 1.5;")
    return lbl

def _mk_accent_button(text, color=None):
    """Thin wrapper so existing call sites can keep passing (text, color) positionally."""
    return GlowButton(text, "secondary", accent=color)


class GriffinWorker(QThread):
    result = pyqtSignal(str, int)

    def __init__(self, cmd, use_pkexec=False):
        super().__init__()
        self.cmd = cmd
        self.use_pkexec = use_pkexec

    def run(self):
        if self.use_pkexec:
            out, code = _gh_run_pkexec(self.cmd)
        else:
            out, code = _gh_run(self.cmd)
        self.result.emit(out, code)


class _AsyncCheckWorker(QThread):
    """
    Runs a status check (dpkg -s, dkms status, test -f, ...) off the GUI
    thread. `checker` is a zero-arg callable returning bool.

    Every "is this driver/package already installed?" check in the Hardware
    Hub used to run synchronously during widget construction — fine
    individually, but multiplied across a dozen dkms/dpkg calls at startup,
    that blocked the GUI thread (sometimes for minutes on a slow dkms) before
    the window could even show. Everything now renders immediately in its
    default state and flips to "Installed"/"Applied" asynchronously once the
    real check comes back.
    """
    result = pyqtSignal(bool)

    def __init__(self, checker):
        super().__init__()
        self._checker = checker

    def run(self):
        try:
            ok = bool(self._checker())
        except Exception:
            ok = False
        self.result.emit(ok)


# ── CPU Hub Tab ────────────────────────────────────────────────────────────────

class HW_CPUTab(QWidget):
    GOVERNOR_NAMES = {
        "powersave":    ("Power Save",       COLORS['success'],  "Maximum battery life. Keeps the CPU at its lowest frequency."),
        "conservative": ("Conservative",     COLORS['success'],  "Ramps up gently when needed — good middle ground for laptops."),
        "schedutil":    ("Balanced",         COLORS['accent'],   "Modern default. Scales frequency based on actual CPU load. Great all-rounder."),
        "ondemand":     ("Balanced (Legacy)",COLORS['accent'],   "Older balanced mode — jumps to full speed then scales back."),
        "userspace":    ("Manual",           COLORS['warning'],  "Set the exact frequency yourself. Advanced use only."),
        "performance":  ("Performance",      COLORS['warning'],  "Locks CPU at maximum at all times. Best for gaming. Uses more power."),
    }

    def __init__(self):
        super().__init__()
        self.workers = []
        self._build_ui()
        self._refresh()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(18)

        hdr = QHBoxLayout()
        hdr.addWidget(SectionHeader("⚡  CPU Governor Control"))
        hdr.addStretch()
        self.refresh_btn = _mk_accent_button("↺  Refresh")
        self.refresh_btn.clicked.connect(self._refresh)
        hdr.addWidget(self.refresh_btn)
        root.addLayout(hdr)

        root.addWidget(_gh_body_label(
            "The CPU governor decides how your processor manages its speed. "
            "Choosing the right governor balances performance against battery life and heat.",
            muted=True
        ))
        root.addWidget(Divider())

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner_widget = QWidget()
        inner = QVBoxLayout(inner_widget)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(18)

        # Status card
        status_card = Card()
        sc_layout = status_card.body()
        sc_layout.setContentsMargins(16, 14, 16, 14)
        sc_layout.setSpacing(10)
        sc_layout.addWidget(SectionHeader("Current Status"))
        self.status_grid = QGridLayout()
        self.status_grid.setHorizontalSpacing(20)
        self.status_grid.setVerticalSpacing(8)
        sc_layout.addLayout(self.status_grid)
        inner.addWidget(status_card)

        # Picker card
        picker_card = Card()
        pc_layout = picker_card.body()
        pc_layout.setContentsMargins(16, 14, 16, 14)
        pc_layout.setSpacing(14)
        pc_layout.addWidget(SectionHeader("Set Governor", COLORS['warning']))

        pick_row = QHBoxLayout()
        self.gov_combo = QComboBox()
        self.gov_combo.currentIndexChanged.connect(self._update_gov_desc)
        pick_row.addWidget(self.gov_combo)
        self.apply_btn = _mk_accent_button("Apply", COLORS['warning'])
        self.apply_btn.clicked.connect(self._apply_governor)
        pick_row.addWidget(self.apply_btn)
        pick_row.addStretch()
        pc_layout.addLayout(pick_row)

        self.gov_desc = _gh_body_label("", muted=True)
        self.gov_desc.setMinimumHeight(36)
        pc_layout.addWidget(self.gov_desc)
        inner.addWidget(picker_card)

        # Explain card
        explain_card = Card()
        ex_layout = explain_card.body()
        ex_layout.setContentsMargins(16, 14, 16, 14)
        ex_layout.setSpacing(10)
        ex_layout.addWidget(SectionHeader("Governor Reference", COLORS['text_muted']))
        ref_grid = QGridLayout()
        ref_grid.setHorizontalSpacing(10)
        ref_grid.setVerticalSpacing(10)
        ref_grid.setColumnStretch(1, 1)
        for r, (key, (name, color, desc_text)) in enumerate(self.GOVERNOR_NAMES.items()):
            ref_grid.addWidget(_mk_badge(name, color), r, 0, Qt.AlignmentFlag.AlignTop)
            lbl = QLabel(desc_text)
            lbl.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 12px;")
            lbl.setWordWrap(True)
            ref_grid.addWidget(lbl, r, 1)
        ex_layout.addLayout(ref_grid)
        inner.addWidget(explain_card)
        inner.addStretch()

        scroll.setWidget(inner_widget)
        root.addWidget(scroll, 1)

        self.status_msg = QLabel("")
        self.status_msg.setStyleSheet(f"color: {COLORS['success']}; font-size: 12px; font-weight: 600;")
        root.addWidget(self.status_msg)

    def _refresh(self):
        self.refresh_btn.setEnabled(False)
        out, _ = _gh_run("cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null")
        current = out.strip() or "unknown"
        avail, _ = _gh_run("cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_available_governors 2>/dev/null")
        available = avail.split() if avail else list(self.GOVERNOR_NAMES.keys())
        freq_khz, _ = _gh_run("cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq 2>/dev/null")
        try:
            freq_mhz = int(freq_khz) // 1000
        except Exception:
            freq_mhz = 0
        max_khz, _ = _gh_run("cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq 2>/dev/null")
        min_khz, _ = _gh_run("cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_min_freq 2>/dev/null")
        try:
            max_mhz = int(max_khz) // 1000
            min_mhz = int(min_khz) // 1000
        except Exception:
            max_mhz = min_mhz = 0
        cpu_count, _ = _gh_run("nproc")

        while self.status_grid.count():
            item = self.status_grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        name, color, _ = self.GOVERNOR_NAMES.get(current, (current, COLORS['accent'], ""))
        self._add_status_row(0, "Active Governor", name, color)
        self._add_status_row(1, "CPU Cores", cpu_count or "?", COLORS['text_primary'])
        if freq_mhz:
            self._add_status_row(2, "Current Frequency", f"{freq_mhz} MHz", COLORS['accent'])
        if max_mhz:
            self._add_status_row(3, "Max / Min", f"{max_mhz} MHz / {min_mhz} MHz", COLORS['text_muted'])

        self.gov_combo.blockSignals(True)
        self.gov_combo.clear()
        for gov in available:
            n, _, _ = self.GOVERNOR_NAMES.get(gov, (gov, COLORS['text_primary'], ""))
            self.gov_combo.addItem(f"{n}  ({gov})", gov)
            if gov == current:
                self.gov_combo.setCurrentIndex(self.gov_combo.count() - 1)
        self.gov_combo.blockSignals(False)
        self._update_gov_desc()
        self.refresh_btn.setEnabled(True)

    def _add_status_row(self, row, label, value, color):
        lbl = QLabel(label)
        lbl.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 12px;")
        val = QLabel(value)
        val.setStyleSheet(f"color: {color}; font-size: 13px; font-weight: 700;")
        self.status_grid.addWidget(lbl, row, 0)
        self.status_grid.addWidget(val, row, 1)

    def _update_gov_desc(self):
        gov = self.gov_combo.currentData()
        if gov:
            _, _, desc = self.GOVERNOR_NAMES.get(gov, ("", COLORS['text_primary'], "No description available."))
            self.gov_desc.setText(desc)

    def _apply_governor(self):
        gov = self.gov_combo.currentData()
        if not gov:
            return
        self.apply_btn.setEnabled(False)
        self.status_msg.setText("Applying governor…")
        self.status_msg.setStyleSheet(f"color: {COLORS['accent']}; font-size: 12px; font-weight: 600;")
        cpu_count, _ = _gh_run("nproc")
        try:
            n = int(cpu_count)
        except Exception:
            n = 1
        cmds = " && ".join(
            f"echo {gov} > /sys/devices/system/cpu/cpu{i}/cpufreq/scaling_governor"
            for i in range(n)
        )
        w = GriffinWorker(cmds, use_pkexec=True)
        w.result.connect(self._gov_done)
        self.workers.append(w)
        w.start()

    def _gov_done(self, out, code):
        self.apply_btn.setEnabled(True)
        if code == 0:
            self.status_msg.setText("✓  Governor applied successfully.")
            self.status_msg.setStyleSheet(f"color: {COLORS['success']}; font-size: 12px; font-weight: 600;")
            self._refresh()
        else:
            self.status_msg.setText(f"✗  Failed to apply governor. ({out[:80]})")
            self.status_msg.setStyleSheet(f"color: {COLORS['error']}; font-size: 12px; font-weight: 600;")


# ── GPU Hub Tab ────────────────────────────────────────────────────────────────

class HW_GPUTab(QWidget):
    def __init__(self):
        super().__init__()
        self.workers = []
        self._build_ui()
        QTimer.singleShot(200, self._detect)

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(18)

        hdr = QHBoxLayout()
        hdr.addWidget(SectionHeader("🎮  GPU Hub", COLORS['accent']))
        hdr.addStretch()
        self.rescan_btn = _mk_accent_button("↺  Re-scan")
        self.rescan_btn.clicked.connect(self._detect)
        hdr.addWidget(self.rescan_btn)
        root.addLayout(hdr)

        root.addWidget(_gh_body_label(
            "Detects your GPU and helps install the optimal driver. "
            "Each option is explained so you know exactly what you're installing and why.",
            muted=True
        ))
        root.addWidget(Divider())

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner_widget = QWidget()
        inner = QVBoxLayout(inner_widget)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(18)

        det_card = Card()
        det_layout = det_card.body()
        det_layout.setContentsMargins(16, 14, 16, 14)
        self.gpu_info_layout = QVBoxLayout()
        det_layout.addLayout(self.gpu_info_layout)
        inner.addWidget(det_card)

        self.driver_card = Card()
        self.driver_layout = self.driver_card.body()
        self.driver_layout.setContentsMargins(16, 14, 16, 14)
        self.driver_layout.setSpacing(10)
        inner.addWidget(self.driver_card)

        self.extras_card = Card()
        self.extras_layout = self.extras_card.body()
        self.extras_layout.setContentsMargins(16, 14, 16, 14)
        self.extras_layout.setSpacing(8)
        self.extras_card.hide()
        inner.addWidget(self.extras_card)

        swap_card = Card()
        swap_layout = QHBoxLayout(); swap_card.set_body_layout(swap_layout)
        swap_layout.setContentsMargins(16, 12, 16, 12)
        swap_lbl = _gh_body_label("Switching GPU brands (e.g. NVIDIA → AMD)?  Use Hardware Swap to revert to safe open-source drivers first.")
        swap_lbl.setWordWrap(True)
        swap_layout.addWidget(swap_lbl, 1)
        swap_btn = _mk_accent_button("🔄  Hardware Swap", COLORS['warning'])
        swap_btn.clicked.connect(self._hardware_swap)
        swap_layout.addWidget(swap_btn)
        inner.addWidget(swap_card)

        inner.addStretch()
        scroll.setWidget(inner_widget)
        root.addWidget(scroll, 1)

        self.status_msg = QLabel("")
        self.status_msg.setStyleSheet(f"color: {COLORS['success']}; font-size: 12px; font-weight: 600;")
        root.addWidget(self.status_msg)

    def _clear_layout(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                self._clear_layout(item.layout())

    def _detect(self):
        self.rescan_btn.setEnabled(False)
        self._clear_layout(self.gpu_info_layout)
        self._clear_layout(self.driver_layout)
        self._clear_layout(self.extras_layout)
        self.extras_card.hide()

        HardwareProbe.reset_cache()  # re-probe fresh on every manual re-scan
        gpus = HardwareProbe.gpus()
        lspci, _ = _gh_run("lspci | grep -iE 'VGA|3D|Display'")
        lines = [l.strip() for l in lspci.splitlines() if l.strip()]

        if not gpus or not lines:
            self.gpu_info_layout.addWidget(_gh_body_label("No GPU detected via lspci.", muted=True))
            self.rescan_btn.setEnabled(True)
            return

        # Chassis line — tells the person at a glance whether Griffin Hub
        # thinks this is a laptop or desktop, since that changes what
        # "discrete vs. integrated" even means for their hardware.
        chassis = HardwareProbe.chassis_type()
        chassis_label = {"laptop": "💻  Laptop", "desktop": "🖥  Desktop"}.get(
            chassis, "🖥  Desktop/Laptop (unable to determine which)"
        )
        self.gpu_info_layout.addWidget(_gh_body_label(chassis_label, muted=True))

        for line in lines:
            self.gpu_info_layout.addWidget(_gh_body_label(f"🖥  {line}"))

        # Tag each detected GPU as discrete/integrated so a laptop with
        # hybrid graphics (e.g. Intel iGPU + NVIDIA dGPU) shows BOTH,
        # correctly labelled, instead of silently only ever showing
        # whichever vendor string happened to match first in lspci text.
        if len(gpus) > 1:
            tag_row = QHBoxLayout()
            for gpu in gpus:
                kind = "Discrete" if gpu.get("discrete") else "Integrated"
                color = COLORS['success'] if gpu.get("discrete") else COLORS['accent']
                tag_row.addWidget(_mk_badge(f"{gpu['vendor'].upper()} ({kind})", color))
            tag_row.addStretch()
            self.gpu_info_layout.addLayout(tag_row)
            self.gpu_info_layout.addWidget(_gh_body_label(
                "Hybrid graphics detected — driver options below are for the "
                "GPU Griffin Hub will optimise for (the discrete one, if you "
                "have one). Both GPUs may still need their own driver "
                "packages installed; check your distro's hybrid-graphics "
                "switching tool (e.g. envycontrol, system76-power, "
                "supergfxctl) if games aren't using the GPU you expect.",
                muted=True
            ))

        primary = HardwareProbe.primary_gpu()
        vendor = primary["vendor"] if primary else "unknown"

        if vendor == "nvidia":
            self._show_nvidia(lspci)
        elif vendor == "amd":
            self._show_amd(lspci)
        elif vendor == "intel":
            self._show_intel(lspci)
        else:
            self.driver_layout.addWidget(_gh_body_label("GPU brand not recognized. Generic mesa drivers are likely already installed.", muted=True))

        self.rescan_btn.setEnabled(True)

    def _show_nvidia(self, lspci):
        self._clear_layout(self.driver_layout)
        self.driver_layout.addWidget(SectionHeader("NVIDIA Driver Options", COLORS['success']))

        rtx50 = bool(re.search(r'RTX\s*50[0-9][0-9]', lspci, re.IGNORECASE))
        if rtx50:
            warn = QLabel("⚠️  RTX 50-series detected. Driver support is still maturing on Linux — prefer the latest 570+ driver.")
            warn.setStyleSheet(f"color: {COLORS['warning']}; font-size: 12px; font-weight: 600; padding: 6px; background: {COLORS['warning']}11; border-radius:4px;")
            warn.setWordWrap(True)
            self.driver_layout.addWidget(warn)

        if HardwareProbe.is_laptop():
            laptop_note = QLabel(
                "ℹ️  Laptop detected with an NVIDIA GPU — this is very likely a "
                "hybrid graphics (Optimus/PRIME) setup with an Intel or AMD "
                "integrated GPU alongside it. Griffin Hub can install the NVIDIA "
                "driver, but which GPU actually renders a given game depends on "
                "your distro's hybrid-graphics switching configuration "
                "(PRIME render offload, envycontrol, supergfxctl, or similar) "
                "— that's a per-distro, per-laptop-model setup Griffin Hub "
                "doesn't control and can't fully automate. If a game launches "
                "but runs slowly, check that it's actually using the NVIDIA GPU."
            )
            laptop_note.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 11px; padding: 6px; background: {COLORS['bg_dark']}; border-radius:4px;")
            laptop_note.setWordWrap(True)
            self.driver_layout.addWidget(laptop_note)

        current_driver, _ = _gh_run("nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1")
        if current_driver:
            row = QHBoxLayout()
            row.addWidget(_mk_badge("Installed", COLORS['success']))
            row.addWidget(_gh_body_label(f"NVIDIA driver version {current_driver} is active.", muted=True))
            row.addStretch()
            self.driver_layout.addLayout(row)

        drivers = [
            ("nvidia-driver-570 (Recommended)", "Latest stable. Best for RTX 30/40 and most RTX 50 series.", COLORS['success'], "sudo apt-get install -y nvidia-driver-570", ["nvidia-driver-570"]),
            ("nvidia-driver-550", "Previous stable. Solid choice if 570 has issues.", COLORS['accent'], "sudo apt-get install -y nvidia-driver-550", ["nvidia-driver-550"]),
            ("nvidia-driver-open (open kernel module)", "NVIDIA's open-source kernel module for Turing (RTX 20) and newer.", COLORS['accent'], "sudo apt-get install -y nvidia-open", ["nvidia-open"]),
            ("nouveau (open-source, basic)", "Reverse-engineered driver. No 3D acceleration or CUDA. Use only to prep for a GPU swap.", COLORS['warning'], "", None),
        ]
        for dname, ddesc, dcolor, dcmd, dpkgs in drivers:
            self._driver_option_row(dname, ddesc, dcolor, dcmd, dpkgs)
        self._show_extras_nvidia()

    def _show_amd(self, lspci):
        self._clear_layout(self.driver_layout)
        self.driver_layout.addWidget(SectionHeader("AMD Driver Options", COLORS['error']))

        rdna4 = bool(re.search(r'RX\s*9[0-9][0-9][0-9]', lspci, re.IGNORECASE))
        if rdna4:
            warn = QLabel("⚠️  RDNA4 GPU detected. Full Linux support is still rolling out — some features may require kernel 6.8+.")
            warn.setStyleSheet(f"color: {COLORS['warning']}; font-size: 12px; font-weight: 600; padding: 6px; background: {COLORS['warning']}11; border-radius:4px;")
            warn.setWordWrap(True)
            self.driver_layout.addWidget(warn)

        primary = HardwareProbe.primary_gpu()
        if HardwareProbe.is_laptop() and primary and not primary.get("discrete"):
            apu_note = QLabel(
                "ℹ️  This looks like a laptop's integrated AMD APU graphics "
                "(not a discrete GPU) — amdgpu (built into the kernel) is the "
                "right driver and is almost certainly already active. "
                "amdgpu-pro and ROCm below are mainly useful for discrete AMD "
                "cards doing professional compute work; they won't do much "
                "for integrated graphics."
            )
            apu_note.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 11px; padding: 6px; background: {COLORS['bg_dark']}; border-radius:4px;")
            apu_note.setWordWrap(True)
            self.driver_layout.addWidget(apu_note)

        drivers = [
            ("amdgpu (built-in kernel, Recommended)", "Main AMD GPU driver in the Linux kernel. Usually already active — no install needed.", COLORS['success'], "", "builtin"),
            ("amdgpu-pro (hybrid proprietary)", "Adds OpenCL and Vulkan pro features on top of amdgpu for professional compute.", COLORS['accent'], "", None),
            ("xf86-video-amdgpu (Xorg DDX)", "X.Org display server component for AMD. Usually installed automatically.", COLORS['accent'], "sudo apt-get install -y xserver-xorg-video-amdgpu", ["xserver-xorg-video-amdgpu"]),
        ]
        for dname, ddesc, dcolor, dcmd, dpkgs in drivers:
            self._driver_option_row(dname, ddesc, dcolor, dcmd, dpkgs)
        self._show_extras_amd()

    def _show_intel(self, lspci):
        self._clear_layout(self.driver_layout)
        self.driver_layout.addWidget(SectionHeader("Intel GPU Driver Options", COLORS['accent']))

        drivers = [
            ("i915 (built-in kernel, Recommended)", "Intel's integrated and Arc GPU driver, built into the Linux kernel. Almost always already active.", COLORS['success'], "", "builtin"),
            ("intel-media-va-driver", "Hardware video decode/encode acceleration (VA-API). Needed for smooth video playback.", COLORS['accent'], "sudo apt-get install -y intel-media-va-driver", ["intel-media-va-driver"]),
            ("xf86-video-intel (Xorg DDX)", "X.Org component for Intel graphics. Older systems may need this.", COLORS['warning'], "sudo apt-get install -y xserver-xorg-video-intel", ["xserver-xorg-video-intel"]),
        ]
        for dname, ddesc, dcolor, dcmd, dpkgs in drivers:
            self._driver_option_row(dname, ddesc, dcolor, dcmd, dpkgs)

    def _driver_option_row(self, name, desc, color, cmd, check_pkgs=None):
        frame = QFrame()
        frame.setStyleSheet(f"background: {COLORS['bg_dark']}; border: 1px solid {COLORS['border']}; border-radius:6px;")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(12)
        text_col = QVBoxLayout()
        n = QLabel(name)
        n.setStyleSheet(f"color: {color}; font-size: 13px; font-weight: 700;")
        text_col.addWidget(n)
        d = QLabel(desc)
        d.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 11px;")
        d.setWordWrap(True)
        text_col.addWidget(d)
        layout.addLayout(text_col, 1)

        status_area = QHBoxLayout()
        status_area.setSpacing(8)
        layout.addLayout(status_area)
        self.driver_layout.addWidget(frame)

        if check_pkgs == "builtin":
            status_area.addWidget(StatusBadge("builtin", color=COLORS['success'], text="✓ Active (built-in)"))
            return
        if not cmd:
            return

        def render(installed: bool):
            self._clear_layout(status_area)
            if installed:
                status_area.addWidget(StatusBadge("installed", color=COLORS['success'], text="✓ Installed"))
                btn = GlowButton("Reinstall", "secondary", accent=color)
            else:
                btn = GlowButton("Install", "secondary", accent=color)
            btn.setMinimumWidth(100)
            btn.clicked.connect(lambda checked, c=cmd: self._run_install(c))
            status_area.addWidget(btn)

        render(False)  # show immediately; flips to Installed once the check resolves
        if check_pkgs:
            worker = _AsyncCheckWorker(lambda pkgs=check_pkgs: all(gt_pkg_installed(p) for p in pkgs))
            self.workers.append(worker)
            def on_result(installed, w=worker):
                if w in self.workers:
                    self.workers.remove(w)
                render(installed)
            worker.result.connect(on_result)
            worker.start()

    def _show_extras_nvidia(self):
        self.extras_card.show()
        self._clear_layout(self.extras_layout)
        self.extras_layout.addWidget(SectionHeader("NVIDIA Extras", COLORS['accent']))
        extras = [
            ("CUDA Toolkit", "NVIDIA's parallel computing platform. Needed for GPU-accelerated AI/ML, Blender GPU rendering.", "sudo apt-get install -y nvidia-cuda-toolkit", ["nvidia-cuda-toolkit"]),
            ("NVENC/NVDEC (hardware video)", "GPU-accelerated video encoding/decoding. Used by OBS, Handbrake, FFmpeg.", "sudo apt-get install -y libnvidia-encode1 libnvidia-decode1", ["libnvidia-encode1", "libnvidia-decode1"]),
            ("NVIDIA Settings GUI", "Graphical tool to adjust clocks, fan speed, resolution, and display settings.", "sudo apt-get install -y nvidia-settings", ["nvidia-settings"]),
        ]
        for name, desc, cmd, pkgs in extras:
            self.extras_layout.addWidget(self._extras_row(name, desc, cmd, pkgs))

    def _show_extras_amd(self):
        self.extras_card.show()
        self._clear_layout(self.extras_layout)
        self.extras_layout.addWidget(SectionHeader("AMD Extras", COLORS['accent']))
        extras = [
            ("ROCm (GPU compute)", "AMD's open-source GPU computing stack. Required for GPU-accelerated AI/ML on AMD cards.", "sudo apt-get install -y rocm", ["rocm"]),
            ("Mesa Vulkan (radv)", "High-performance open-source Vulkan driver for AMD. Usually already installed with Mesa.", "sudo apt-get install -y mesa-vulkan-drivers", ["mesa-vulkan-drivers"]),
            ("VA-API (hardware video)", "Hardware video decode/encode on AMD GPUs. Speeds up video playback and transcoding.", "sudo apt-get install -y mesa-va-drivers", ["mesa-va-drivers"]),
        ]
        for name, desc, cmd, pkgs in extras:
            self.extras_layout.addWidget(self._extras_row(name, desc, cmd, pkgs))

    def _extras_row(self, name, desc, cmd, check_pkgs=None):
        frame = QFrame()
        frame.setStyleSheet(f"background: {COLORS['bg_dark']}; border: 1px solid {COLORS['border']}; border-radius: 6px;")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(12)
        text_col = QVBoxLayout()
        n = QLabel(name)
        n.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: 13px; font-weight: 600;")
        text_col.addWidget(n)
        d = QLabel(desc)
        d.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 11px;")
        d.setWordWrap(True)
        text_col.addWidget(d)
        layout.addLayout(text_col, 1)

        status_area = QHBoxLayout()
        status_area.setSpacing(8)
        layout.addLayout(status_area)

        def render(installed: bool):
            self._clear_layout(status_area)
            if installed:
                status_area.addWidget(StatusBadge("installed", color=COLORS['success'], text="✓ Installed"))
                btn = GlowButton("Uninstall", "secondary", accent=COLORS['error'])
                btn.clicked.connect(lambda checked, pkgs=check_pkgs: self._run_uninstall(pkgs))
            else:
                btn = GlowButton("Install", "secondary", accent=COLORS['accent'])
                btn.clicked.connect(lambda checked, c=cmd: self._run_install(c))
            btn.setMinimumWidth(100)
            status_area.addWidget(btn)

        render(False)  # show immediately; flips to Installed once the check resolves
        if check_pkgs:
            worker = _AsyncCheckWorker(lambda pkgs=check_pkgs: all(gt_pkg_installed(p) for p in pkgs))
            self.workers.append(worker)
            def on_result(installed, w=worker):
                if w in self.workers:
                    self.workers.remove(w)
                render(installed)
            worker.result.connect(on_result)
            worker.start()

        return frame

    def _run_install(self, cmd):
        self.status_msg.setText("Running install… this may take a moment.")
        self.status_msg.setStyleSheet(f"color: {COLORS['accent']}; font-size: 12px; font-weight: 600;")
        w = GriffinWorker(f"apt-get update -qq && {cmd}", use_pkexec=True)
        w.result.connect(self._install_done)
        self.workers.append(w)
        w.start()

    def _run_uninstall(self, pkgs):
        self.status_msg.setText("Uninstalling…")
        self.status_msg.setStyleSheet(f"color: {COLORS['accent']}; font-size: 12px; font-weight: 600;")
        w = GriffinWorker(f"apt-get remove -y {' '.join(pkgs)}", use_pkexec=True)
        w.result.connect(self._install_done)
        self.workers.append(w)
        w.start()

    def _install_done(self, out, code):
        if code == 0:
            self.status_msg.setText("✓  Done. A reboot is recommended for driver changes to take effect.")
            self.status_msg.setStyleSheet(f"color: {COLORS['success']}; font-size: 12px; font-weight: 600;")
        else:
            self.status_msg.setText("✗  Operation failed. Check your internet connection and apt sources.")
            self.status_msg.setStyleSheet(f"color: {COLORS['error']}; font-size: 12px; font-weight: 600;")
        # Refresh so every badge/button reflects the new post-install state,
        # instead of silently staying on "Install" even if it just succeeded
        # (or staying stuck on a stale state if it failed).
        self._detect()

    def _hardware_swap(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Hardware Swap")
        dlg.setMinimumWidth(480)
        dlg.setStyleSheet(STYLESHEET)
        layout = QVBoxLayout(dlg)
        layout.addWidget(SectionHeader("GPU Hardware Swap", COLORS['warning']))
        layout.addWidget(_gh_body_label(
            "This will remove proprietary GPU drivers and install safe open-source fallbacks.\n\n"
            "Steps:\n1. Click Proceed and authenticate.\n2. Shut down your PC.\n"
            "3. Install your new GPU.\n4. Boot up — Griffin Hub will detect the new card."
        ))
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("Proceed")
        btns.accepted.connect(lambda: (dlg.accept(), self._do_swap()))
        btns.rejected.connect(dlg.reject)
        layout.addWidget(btns)
        dlg.exec()

    def _do_swap(self):
        cmd = (
            "apt-get remove -y --purge nvidia-driver-* nvidia-open amdgpu-pro 2>/dev/null; "
            "apt-get install -y xserver-xorg-video-nouveau mesa-utils 2>/dev/null; "
            "update-initramfs -u"
        )
        self.status_msg.setText("Running hardware swap… please wait.")
        self.status_msg.setStyleSheet(f"color: {COLORS['warning']}; font-size: 12px; font-weight: 600;")
        w = GriffinWorker(cmd, use_pkexec=True)
        w.result.connect(lambda o, c: self.status_msg.setText(
            "✓  Swap complete. Shut down and install your new GPU." if c == 0
            else "✗  Swap had errors — check the system manually."
        ))
        self.workers.append(w)
        w.start()


# ── Controller Hub Tab ─────────────────────────────────────────────────────────

class HW_ControllerTab(QWidget):
    DRIVERS = {
        "xone": {
            "color": COLORS['success'],
            "label": "xone",
            "for": "Xbox One & Xbox Series X|S (USB + Wireless Dongle)",
            "desc": (
                "Recommended for modern Xbox controllers. Supports Xbox One, Series X|S, adaptive controller, "
                "and third-party GIP accessories. Includes audio passthrough and rumble. "
                "Replaces the older xpad driver for Xbox One devices."
            ),
            "note": "Pair xone with xpad-noone to avoid conflicts.",
            "install": (
                "git clone https://github.com/medusalix/xone /tmp/xone && "
                "cd /tmp/xone && bash install.sh --release && "
                "xone-get-firmware.sh --skip-disclaimer"
            ),
            "check": "dkms status 2>/dev/null | grep -qi '^xone[,/]'",
        },
        "xpad-noone": {
            "color": COLORS['accent'],
            "label": "xpad-noone",
            "for": "Xbox / Xbox 360 (used alongside xone)",
            "desc": (
                "Modified xpad driver with Xbox One support removed. Required when xone is installed "
                "so that original Xbox and Xbox 360 controllers still work."
            ),
            "note": "Only needed if you also have original Xbox/360 controllers.",
            "install": (
                "git clone https://github.com/medusalix/xpad-noone /tmp/xpad-noone && "
                "cp -r /tmp/xpad-noone /usr/src/xpad-noone-1.0 && "
                "dkms install -m xpad-noone -v 1.0"
            ),
            "check": "dkms status 2>/dev/null | grep -qi '^xpad-noone[,/]'",
        },
        "paroj/xpad": {
            "color": COLORS['accent'],
            "label": "paroj/xpad (GitHub)",
            "for": "Xbox / Xbox 360 / Xbox One (USB only, community fork)",
            "desc": (
                "Community-maintained fork of the kernel's xpad driver with extra device support. "
                "Good for Xbox 360 and older Xbox One via USB if you don't need the full xone feature set."
            ),
            "note": "USB-only. No Wireless Dongle support.",
            "install": (
                "git clone https://github.com/paroj/xpad.git /usr/src/xpad-0.4 && "
                "dkms install -m xpad -v 0.4"
            ),
            "check": "dkms status 2>/dev/null | grep -qi '^xpad,'",
        },
        "xpad (kernel)": {
            "color": COLORS['text_muted'],
            "label": "xpad (kernel built-in)",
            "for": "Xbox / Xbox 360 / basic Xbox One (USB)",
            "desc": (
                "The xpad driver shipped with the Linux kernel. Supports Xbox and Xbox 360 controllers "
                "out of the box with no installation needed. Community forks offer a better experience for newer controllers."
            ),
            "note": "Already in your kernel — no install needed.",
            "install": "",
        },
        "xpadneo": {
            "color": COLORS['accent'],
            "label": "xpadneo",
            "for": "Xbox One / Series X|S / Xbox Elite 2 (Bluetooth)",
            "desc": (
                "Best driver for Xbox controllers over Bluetooth. Supports rumble (with trigger force feedback), "
                "battery level, paddle buttons (Elite 2), and consistent button mapping."
            ),
            "note": "Bluetooth only. Use xone for USB/dongle.",
            "install": (
                "git clone https://github.com/atar-axis/xpadneo.git /tmp/xpadneo && "
                "cd /tmp/xpadneo && bash install.sh"
            ),
            "check": "dkms status 2>/dev/null | grep -qi '^xpadneo[,/]'",
        },
        "hid-tmff2 (Thrustmaster)": {
            "color": COLORS['warning'],
            "label": "hid-tmff2",
            "for": "Thrustmaster T300RS, T248, TX, T128, TS-PC, TS-XW wheels",
            "desc": (
                "Force feedback kernel module for Thrustmaster racing wheels. "
                "Supports T300RS and T248 fully, experimental support for TX, T128, T598, TS-PC, and TS-XW."
            ),
            "note": "Firmware must be v31 or newer (update via Windows Thrustmaster tools if needed).",
            "install": (
                "apt-get install -y dkms linux-headers-generic joystick && "
                "git clone --recurse-submodules https://github.com/Kimplul/hid-tmff2.git /tmp/hid-tmff2 && "
                "cd /tmp/hid-tmff2 && bash dkms/dkms-install.sh && make udev-rules"
            ),
            "check": "dkms status 2>/dev/null | grep -qi '^hid-tmff2[,/]'",
        },
        "ds4drv / dualsense (PS)": {
            "color": COLORS['accent'],
            "label": "hid-playstation / ds4drv",
            "for": "PlayStation DualShock 4, DualSense (PS5)",
            "desc": (
                "Modern PlayStation controllers are supported via hid-playstation (built-in since kernel 5.12 for DualSense). "
                "DualShock 4 works natively via USB/Bluetooth with SDL2."
            ),
            "note": "DualSense and DS4 usually work out of the box on recent Ubuntu.",
            "install": "apt-get install -y joystick",
            "check_pkgs": ["joystick"],
        },
    }

    def __init__(self):
        super().__init__()
        self._workers = []
        self._build_ui()
        QTimer.singleShot(200, self._detect_controllers)

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(18)

        hdr = QHBoxLayout()
        hdr.addWidget(SectionHeader("🕹  Controller Hub", COLORS['success']))
        hdr.addStretch()
        refresh_btn = _mk_accent_button("↺  Scan Controllers", COLORS['success'])
        refresh_btn.clicked.connect(self._detect_controllers)
        hdr.addWidget(refresh_btn)
        outer.addLayout(hdr)

        outer.addWidget(_gh_body_label(
            "Detects connected controllers and recommends the right driver for each. "
            "Each driver is explained so you know exactly what you're installing and why.",
            muted=True
        ))
        outer.addWidget(Divider())

        det_card = Card()
        det_layout = det_card.body()
        det_layout.setContentsMargins(16, 14, 16, 14)
        det_layout.setSpacing(6)
        det_layout.addWidget(SectionHeader("Detected Controllers", COLORS['success']))
        self.detected_layout = QVBoxLayout()
        det_layout.addLayout(self.detected_layout)
        outer.addWidget(det_card)

        outer.addWidget(SectionHeader("Available Drivers", COLORS['text_primary']))
        outer.addWidget(_gh_body_label(
            "Read the description to pick the right driver for your hardware.",
            muted=True
        ))

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner_widget = QWidget()
        self.driver_list_layout = QVBoxLayout(inner_widget)
        self.driver_list_layout.setSpacing(10)
        self.driver_list_layout.setContentsMargins(0, 4, 0, 4)
        for key, info in self.DRIVERS.items():
            self.driver_list_layout.addWidget(self._make_driver_card(key, info))
        self.driver_list_layout.addStretch()
        scroll.setWidget(inner_widget)
        outer.addWidget(scroll, 1)

        self.status_msg = QLabel("")
        self.status_msg.setStyleSheet(f"color: {COLORS['success']}; font-size: 12px; font-weight: 600;")
        outer.addWidget(self.status_msg)

    def _driver_installed_checker(self, info):
        """Return a zero-arg callable for _AsyncCheckWorker, or None if not checkable."""
        if "check" in info:
            return lambda: _gh_run(info["check"])[1] == 0
        if "check_pkgs" in info:
            return lambda: all(gt_pkg_installed(p) for p in info["check_pkgs"])
        return None

    def _make_driver_card(self, key, info):
        card = Card()
        layout = card.body()
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        top = QHBoxLayout()
        top.addWidget(_mk_badge(info["label"], info["color"]))
        for_lbl = QLabel(f"  {info['for']}")
        for_lbl.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 12px;")
        top.addWidget(for_lbl)
        top.addStretch()
        layout.addLayout(top)

        desc = QLabel(info["desc"])
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: 12px;")
        layout.addWidget(desc)

        if info.get("note"):
            note = QLabel(f"ℹ  {info['note']}")
            note.setWordWrap(True)
            note.setStyleSheet(f"color: {COLORS['warning']}; font-size: 11px;")
            layout.addWidget(note)

        if not info.get("install"):
            # No install command at all — it's a kernel built-in, like stock xpad.
            btn_row = QHBoxLayout()
            btn_row.addStretch()
            btn_row.addWidget(StatusBadge("builtin", color=COLORS['success'], text="✓ Built-in"))
            layout.addLayout(btn_row)
            return card

        status_row = QHBoxLayout()
        layout.addLayout(status_row)

        def render(installed: bool):
            self._clear_row(status_row)
            if installed:
                status_row.addWidget(StatusBadge("installed", color=COLORS['success'], text="✓ Installed"))
                status_row.addStretch()
                btn = GlowButton("Reinstall", "secondary", accent=info["color"])
            else:
                status_row.addStretch()
                btn = GlowButton("Install", "secondary", accent=info["color"])
            btn.setMinimumWidth(100)
            btn.clicked.connect(lambda ch, cmd=info["install"]: self._do_install(cmd))
            status_row.addWidget(btn)

        render(False)  # show immediately; flips to Installed once the check resolves
        checker = self._driver_installed_checker(info)
        if checker:
            worker = _AsyncCheckWorker(checker)
            self._workers.append(worker)
            def on_result(installed, w=worker):
                if w in self._workers:
                    self._workers.remove(w)
                render(installed)
            worker.result.connect(on_result)
            worker.start()

        return card

    @staticmethod
    def _clear_row(layout):
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _detect_controllers(self):
        while self.detected_layout.count():
            item = self.detected_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        lsusb_out, _ = _gh_run("lsusb 2>/dev/null")
        controllers_found = []
        if lsusb_out:
            for line in lsusb_out.splitlines():
                lu = line.upper()
                if any(k in lu for k in ["XBOX", "XINPUT", "CONTROLLER", "GAMEPAD", "THRUSTMASTER", "LOGITECH G", "SONY", "DUALSHOCK", "DUALSENSE"]):
                    controllers_found.append(line.strip())

        if controllers_found:
            for c in controllers_found:
                row = QHBoxLayout()
                dot = QLabel("●")
                dot.setStyleSheet(f"color: {COLORS['success']}; font-size: 14px;")
                row.addWidget(dot)
                row.addWidget(_gh_body_label(c))
                row.addStretch()
                w = QWidget()
                w.setLayout(row)
                self.detected_layout.addWidget(w)
        else:
            self.detected_layout.addWidget(_gh_body_label("No controllers detected via USB. Bluetooth controllers appear in your system Bluetooth settings.", muted=True))

    def _rebuild_driver_cards(self):
        while self.driver_list_layout.count():
            item = self.driver_list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for key, info in self.DRIVERS.items():
            self.driver_list_layout.addWidget(self._make_driver_card(key, info))
        self.driver_list_layout.addStretch()

    def _do_install(self, cmd):
        self.status_msg.setText("Installing… this may take a minute.")
        self.status_msg.setStyleSheet(f"color: {COLORS['accent']}; font-size: 12px; font-weight: 600;")
        w = GriffinWorker(f"apt-get install -y git dkms linux-headers-generic 2>/dev/null; {cmd}", use_pkexec=True)
        w.result.connect(self._install_done)
        self._workers.append(w)
        w.start()

    def _install_done(self, out, code):
        if code == 0:
            self.status_msg.setText("✓  Installed. Plug in your controller and test it.")
            self.status_msg.setStyleSheet(f"color: {COLORS['success']}; font-size: 12px; font-weight: 600;")
        else:
            self.status_msg.setText("✗  Install failed. Check internet and try again.")
            self.status_msg.setStyleSheet(f"color: {COLORS['error']}; font-size: 12px; font-weight: 600;")
        # Re-check every driver's installed state so cards reflect what just
        # happened — success flips the button to "Installed", and a failure
        # (e.g. re-running an install that's already applied) stays visible
        # here instead of only living in the status line above.
        self._rebuild_driver_cards()


# ── WiFi Hub Tab ───────────────────────────────────────────────────────────────

class HW_WiFiTab(QWidget):
    REALTEK_WIFI = [
        ("RTL8821CE", "rtw88_8821ce", "rtl8821ce (DKMS)", COLORS['error'],
         "Very common laptop chip. The kernel driver often connects but is unstable. The DKMS driver from GitHub is more reliable.",
         "apt-get install -y git dkms linux-headers-generic && git clone https://github.com/tomaspinho/rtl8821ce /tmp/rtl8821ce && cd /tmp/rtl8821ce && dkms add . && dkms build rtl8821ce/$(ls) && dkms install rtl8821ce/$(ls)",
         "dkms status 2>/dev/null | grep -qi '^rtl8821ce'"),

        ("RTL8822BE", "rtw88_8822be", "rtl8822be (DKMS)", COLORS['warning'],
         "Causes random disconnects with the kernel driver. The DKMS fork improves stability significantly.",
         "apt-get install -y git dkms linux-headers-generic && git clone https://github.com/nickhardiman/rtl8822be /tmp/rtl8822be && cd /tmp/rtl8822be && dkms add . && dkms build 8822be/$(ls) && dkms install 8822be/$(ls)",
         "dkms status 2>/dev/null | grep -qi '^8822be'"),

        ("RTL8852AE", "rtw89_8852ae", "(Kernel 5.16+ required)", COLORS['warning'],
         "Wi-Fi 6 chip. Requires a modern kernel (5.16 or later). Try updating your kernel first before installing DKMS drivers.",
         "apt-get install -y --install-recommends linux-generic-hwe-22.04",
         "dpkg -s linux-generic-hwe-22.04 >/dev/null 2>&1"),

        ("RTL8723DE", "rtw88_8723de", "rtl8723de (DKMS)", COLORS['warning'],
         "Weak signal issues with the kernel driver. The DKMS variant improves sensitivity and connection stability.",
         "apt-get install -y git dkms linux-headers-generic && git clone https://github.com/smlinux/rtl8723de /tmp/rtl8723de && cd /tmp/rtl8723de && dkms add . && dkms build rtl8723de/$(ls) && dkms install rtl8723de/$(ls)",
         "dkms status 2>/dev/null | grep -qi '^rtl8723de'"),

        ("RTL8188EE", "rtl8188ee", "Alternative Git driver", COLORS['warning'],
         "Packet loss issues with the stock driver. Try disabling power management first.",
         "echo 'options rtl8188ee ips=0 fwlps=0' | tee /etc/modprobe.d/rtl8188ee.conf && modprobe -r rtl8188ee && modprobe rtl8188ee",
         "test -f /etc/modprobe.d/rtl8188ee.conf"),

        ("RTL8111/RTL8168 (Ethernet)", "r8169", "r8168 (vendor)", COLORS['text_muted'],
         "Common Ethernet chip — the vendor's r8168 driver often fixes link drops and low speed issues.",
         "apt-get install -y r8168-dkms",
         "dpkg -s r8168-dkms >/dev/null 2>&1"),
    ]

    BROADCOM_WIFI = [
        ("BCM43142", "wl (broadcom-wl)", "bcmwl-kernel-source", COLORS['error'],
         "Most problematic Broadcom chip on Linux. Requires the proprietary broadcom-wl driver. Install via DKMS so it rebuilds after kernel updates.",
         "apt-get install -y bcmwl-kernel-source",
         "dpkg -s bcmwl-kernel-source >/dev/null 2>&1"),

        ("BCM4360", "wl", "broadcom-wl (proprietary only)", COLORS['error'],
         "Proprietary driver is the only option. No open-source support available.",
         "apt-get install -y bcmwl-kernel-source",
         "dpkg -s bcmwl-kernel-source >/dev/null 2>&1"),

        ("BCM4352", "brcmfmac or wl", "depends on revision", COLORS['warning'],
         "Try brcmfmac (open) first; some revisions need the proprietary wl driver.",
         "apt-get install -y firmware-brcm80211 2>/dev/null || apt-get install -y bcmwl-kernel-source",
         "dpkg -s firmware-brcm80211 >/dev/null 2>&1 || dpkg -s bcmwl-kernel-source >/dev/null 2>&1"),

        ("BCM4312 / BCM4313", "b43", "firmware needed", COLORS['warning'],
         "Uses the open-source b43 driver but requires Broadcom's firmware blob. Blacklist the conflicting STA driver.",
         "apt-get install -y firmware-b43-installer && echo blacklist bcm43xx | tee /etc/modprobe.d/blacklist-bcm43xx.conf",
         "dpkg -s firmware-b43-installer >/dev/null 2>&1"),

        ("BCM4321 / BCM4322", "b43", "b43 + firmware", COLORS['accent'],
         "Older Broadcom chips with reasonable open-source support via b43. Install firmware and the driver works.",
         "apt-get install -y firmware-b43-installer",
         "dpkg -s firmware-b43-installer >/dev/null 2>&1"),

        ("BCM43228", "wl", "broadcom-wl (unstable on new kernels)", COLORS['warning'],
         "Proprietary wl driver works but can break on major kernel updates. Keep DKMS installed so it rebuilds automatically.",
         "apt-get install -y bcmwl-kernel-source",
         "dpkg -s bcmwl-kernel-source >/dev/null 2>&1"),
    ]

    def __init__(self):
        super().__init__()
        self._workers = []
        self._build_ui()
        QTimer.singleShot(200, self._detect_wifi)

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(18)

        hdr = QHBoxLayout()
        hdr.addWidget(SectionHeader("WiFi & Ethernet Hub"))
        hdr.addStretch()
        scan_btn = GlowButton("↺  Scan Hardware", "secondary")
        scan_btn.clicked.connect(self._detect_wifi)
        hdr.addWidget(scan_btn)
        outer.addLayout(hdr)

        outer.addWidget(_gh_body_label(
            "Realtek and Broadcom network chips are notorious for poor Linux support. "
            "This hub detects your chip and offers the best driver fix for it.",
            muted=True
        ))
        outer.addWidget(Divider())

        # Everything below (detected hardware + both fix libraries) lives in
        # ONE scroll area so there's a single, full-height scrollable region —
        # not a small scrollbox squeezed underneath a bunch of fixed content.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner_widget = QWidget()
        inner = QVBoxLayout(inner_widget)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(18)

        det_card = Card("Detected Network Hardware")
        det_vbox = det_card.body()
        self.net_detected_layout = QVBoxLayout()
        det_vbox.addLayout(self.net_detected_layout)
        inner.addWidget(det_card)

        self.fix_layout = QVBoxLayout()
        self.fix_layout.setSpacing(10)
        inner.addLayout(self.fix_layout)
        inner.addStretch()

        scroll.setWidget(inner_widget)
        outer.addWidget(scroll, 1)

        self.status_msg = QLabel("")
        self.status_msg.setStyleSheet(f"color: {COLORS['success']}; font-size: 12px; font-weight: 600;")
        outer.addWidget(self.status_msg)

    def _clear_layout(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _detect_wifi(self):
        self._clear_layout(self.net_detected_layout)
        self._clear_layout(self.fix_layout)

        lspci, _ = _gh_run("lspci | grep -iE 'network|ethernet|wireless|wi-fi'")
        lsusb, _ = _gh_run("lsusb | grep -iE 'realtek|broadcom|wireless|wi-fi'")
        combined_hw = (lspci + "\n" + lsusb).lower()

        detected = [l.strip() for l in (lspci + "\n" + lsusb).splitlines() if l.strip()]
        if detected:
            for d in detected:
                row = QHBoxLayout()
                dot = QLabel("●")
                dot.setStyleSheet(f"color: {COLORS['accent']}; font-size: 14px;")
                row.addWidget(dot)
                row.addWidget(_gh_body_label(d))
                row.addStretch()
                w = QWidget()
                w.setLayout(row)
                self.net_detected_layout.addWidget(w)
        else:
            self.net_detected_layout.addWidget(_gh_body_label(
                "No Realtek/Broadcom devices detected (or lspci/lsusb not available).", muted=True
            ))

        self.fix_layout.addWidget(SectionHeader("Realtek Fix Library"))
        for chip, kern_drv, alt_drv, color, desc, cmd, check in self.REALTEK_WIFI:
            highlight = chip.lower().replace(" ", "") in combined_hw.replace(" ", "")
            self.fix_layout.addWidget(self._wifi_card(chip, kern_drv, alt_drv, color, desc, cmd, check, highlight))

        self.fix_layout.addWidget(Divider())
        self.fix_layout.addWidget(SectionHeader("Broadcom Fix Library", color=COLORS['error']))
        for chip, kern_drv, alt_drv, color, desc, cmd, check in self.BROADCOM_WIFI:
            highlight = chip.lower().replace(" ", "") in combined_hw.replace(" ", "")
            self.fix_layout.addWidget(self._wifi_card(chip, kern_drv, alt_drv, color, desc, cmd, check, highlight))
        self.fix_layout.addStretch()

    def _wifi_card(self, chip, kern_drv, alt_drv, color, desc, cmd, check=None, highlight=False):
        # Only a detected chip gets an accent tint — everything else stays the
        # same neutral card as the rest of the app, so severity color is
        # carried by the badge alone rather than by recoloring the whole card.
        card = Card(accent=color if highlight else None)
        layout = card.body()
        layout.setSpacing(6)

        top = QHBoxLayout()
        top.addWidget(StatusBadge(chip, color=color, text=chip))
        if highlight:
            top.addWidget(StatusBadge("detected", color=COLORS['success'], text="●  DETECTED"))
        applied_badge_slot = QHBoxLayout()
        top.addLayout(applied_badge_slot)
        top.addStretch()
        km = QLabel(f"Kernel: {kern_drv}  →  Fix: {alt_drv}")
        km.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 11px;")
        top.addWidget(km)
        layout.addLayout(top)

        d = _gh_body_label(desc)
        layout.addWidget(d)

        if not cmd:
            return card

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        layout.addLayout(btn_row)

        def render(applied: bool):
            self._clear_row(applied_badge_slot)
            self._clear_row(btn_row)
            if applied:
                applied_badge_slot.addWidget(StatusBadge("applied", color=COLORS['success'], text="✓  APPLIED"))
            btn_row.addStretch()
            fix_btn = GlowButton("Reapply Fix" if applied else "Apply Fix", "secondary", accent=color)
            fix_btn.setMinimumWidth(110)
            fix_btn.clicked.connect(lambda ch, c=cmd: self._apply_fix(c))
            btn_row.addWidget(fix_btn)

        render(False)  # show immediately; flips to Applied once the check resolves
        if check:
            worker = _AsyncCheckWorker(lambda c=check: _gh_run(c)[1] == 0)
            self._workers.append(worker)
            def on_result(applied, w=worker):
                if w in self._workers:
                    self._workers.remove(w)
                render(applied)
            worker.result.connect(on_result)
            worker.start()

        return card

    @staticmethod
    def _clear_row(layout):
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _apply_fix(self, cmd):
        self.status_msg.setText("Applying fix… please wait.")
        self.status_msg.setStyleSheet(f"color: {COLORS['accent']}; font-size: 12px; font-weight: 600;")
        w = GriffinWorker(f"apt-get update -qq && {cmd}", use_pkexec=True)
        w.result.connect(self._fix_done)
        self._workers.append(w)
        w.start()

    def _fix_done(self, out, code):
        if code == 0:
            self.status_msg.setText("✓  Fix applied. Reboot for changes to take effect.")
            self.status_msg.setStyleSheet(f"color: {COLORS['success']}; font-size: 12px; font-weight: 600;")
        else:
            self.status_msg.setText("✗  Fix failed. Ensure you have an internet connection.")
            self.status_msg.setStyleSheet(f"color: {COLORS['error']}; font-size: 12px; font-weight: 600;")
        # Re-check every chip's applied status — success flips the badge/button,
        # and a failure (e.g. re-running a fix that's already applied) is now
        # visible on the card itself, not just in the status line above.
        self._detect_wifi()



class HardwarePage(QWidget):
    """Hardware management hub — CPU, GPU, Controller, and WiFi driver tools."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 28, 32, 28)
        layout.setSpacing(16)

        header_row = QHBoxLayout()
        title = QLabel("Hardware Hub")
        title.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: 22px; font-weight: 700;")
        subtitle = QLabel("CPU governor · GPU drivers · Controller drivers · WiFi fixes")
        subtitle.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 13px;")
        title_col = QVBoxLayout()
        title_col.setSpacing(4)
        title_col.addWidget(title)
        title_col.addWidget(subtitle)
        header_row.addLayout(title_col)
        header_row.addStretch()
        layout.addLayout(header_row)
        layout.addWidget(Divider())

        tabs = QTabWidget()
        tabs.tabBar().setElideMode(Qt.TextElideMode.ElideNone)
        tabs.tabBar().setUsesScrollButtons(True)
        tabs.addTab(HW_CPUTab(),        "⚡  CPU")
        tabs.addTab(HW_GPUTab(),        "🎮  GPU")
        tabs.addTab(HW_ControllerTab(), "🕹  Controllers")
        tabs.addTab(HW_WiFiTab(),       "📡  WiFi")
        layout.addWidget(tabs)




# ─────────────────────────────────────────────
#  DASHBOARD
# ─────────────────────────────────────────────
# Deliberately narrow in scope: five at-a-glance gauges, the two things only
# Griffin Hub knows about (ProcessSentry + Kernel Autotune's live state), and
# four simple 60-second history charts. No process tree, no network/disk
# analyzer — just enough to answer "did clicking that button actually do
# something", not to replace htop.

class DashboardSamplerThread(QThread):
    """
    Samples CPU/RAM/GPU/VRAM/temperature/ProcessSentry facts once a second,
    entirely off the GUI thread, and emits one dict per cycle.

    This runs as a background QThread rather than a QTimer calling
    subprocess synchronously — the Hardware Hub tabs used to do exactly
    that for install-status checks and it froze the whole app for minutes
    on a slow call. Sampling every second on the GUI thread would have the
    same problem, just smaller and continuous instead of one big freeze.
    """
    sample_ready = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._running = True
        self._prev_cpu_jiffies: tuple[int, int] | None = None

    def stop(self):
        self._running = False

    def run(self):
        while self._running:
            try:
                data = self._sample_once()
            except Exception:
                data = {}
            self.sample_ready.emit(data)
            for _ in range(10):  # ~1s total, in small chunks so stop() is responsive
                if not self._running:
                    return
                time.sleep(0.1)

    def _sample_once(self) -> dict:
        gpu = self._gpu_metrics()
        return {
            "cpu_percent":      self._cpu_percent(),
            "ram":              self._ram_info(),
            "gpu":              gpu,
            "cpu_temp":         self._cpu_temp(),
            "highest_process":  self._highest_cpu_process(),
            "sentry_status":    gt_systemctl_status(GT_SENTRY_SERVICE),
            "sentry_throttled": self._sentry_throttled_count(),
            "sentry_stats":     self._sentry_stats(),
            "cpu_governor":     self._current_governor(),
            "gamemode_enabled": gt_read_steam_conf().get("GAMEMODE", "0") == "1",
        }

    def _cpu_percent(self) -> float | None:
        try:
            with open("/proc/stat") as f:
                parts = f.readline().split()
            vals = list(map(int, parts[1:8]))  # user nice system idle iowait irq softirq
            idle, total = vals[3] + vals[4], sum(vals)
        except Exception:
            return None
        if self._prev_cpu_jiffies is None:
            self._prev_cpu_jiffies = (idle, total)
            return None
        prev_idle, prev_total = self._prev_cpu_jiffies
        self._prev_cpu_jiffies = (idle, total)
        d_total, d_idle = total - prev_total, idle - prev_idle
        if d_total <= 0:
            return None
        return max(0.0, min(100.0, 100.0 * (d_total - d_idle) / d_total))

    @staticmethod
    def _ram_info() -> dict | None:
        try:
            meminfo = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    k, _, v = line.partition(":")
                    meminfo[k.strip()] = int(v.strip().split()[0])  # kB
            total_kb = meminfo.get("MemTotal", 0)
            avail_kb = meminfo.get("MemAvailable", meminfo.get("MemFree", 0))
            if total_kb <= 0:
                return None
            used_kb = max(0, total_kb - avail_kb)
            return {
                "used_gb": used_kb / (1024 * 1024),
                "total_gb": total_kb / (1024 * 1024),
                "percent": 100.0 * used_kb / total_kb,
            }
        except Exception:
            return None

    @staticmethod
    def _cpu_temp() -> float | None:
        try:
            for hwmon in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
                name_f = hwmon / "name"
                if name_f.exists() and name_f.read_text().strip().lower() in ("coretemp", "k10temp", "zenpower"):
                    temp_f = hwmon / "temp1_input"
                    if temp_f.exists():
                        return int(temp_f.read_text().strip()) / 1000.0
        except Exception:
            pass
        return None

    @staticmethod
    def _gpu_metrics() -> dict | None:
        vendor = HardwareProbe.gpu_vendor()
        if vendor == "nvidia":
            try:
                out = subprocess.check_output(
                    ["nvidia-smi",
                     "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
                     "--format=csv,noheader,nounits"],
                    stderr=subprocess.DEVNULL, timeout=3,
                ).decode().strip()
                util, mem_used, mem_total, temp = [x.strip() for x in out.split(",")]
                return {
                    "percent": float(util),
                    "vram_used_gb": float(mem_used) / 1024,
                    "vram_total_gb": float(mem_total) / 1024,
                    "temp": float(temp),
                }
            except Exception:
                return None
        if vendor == "amd":
            try:
                for card in sorted(Path("/sys/class/drm").glob("card[0-9]*")):
                    busy_f = card / "device" / "gpu_busy_percent"
                    if not busy_f.exists():
                        continue
                    percent = float(busy_f.read_text().strip())
                    vram_used_f  = card / "device" / "mem_info_vram_used"
                    vram_total_f = card / "device" / "mem_info_vram_total"
                    vram_used  = int(vram_used_f.read_text().strip())  / (1024 ** 3) if vram_used_f.exists()  else None
                    vram_total = int(vram_total_f.read_text().strip()) / (1024 ** 3) if vram_total_f.exists() else None
                    temp = None
                    hwmon_dir = card / "device" / "hwmon"
                    if hwmon_dir.exists():
                        for hwmon in sorted(hwmon_dir.glob("hwmon*")):
                            temp_f = hwmon / "temp1_input"
                            if temp_f.exists():
                                temp = int(temp_f.read_text().strip()) / 1000.0
                                break
                    return {"percent": percent, "vram_used_gb": vram_used, "vram_total_gb": vram_total, "temp": temp}
            except Exception:
                return None
        return None  # intel / unknown — no simple, reliable one-shot query

    @staticmethod
    def _highest_cpu_process() -> tuple[str, float] | None:
        try:
            out = subprocess.check_output(
                ["ps", "-eo", "comm,%cpu", "--sort=-%cpu", "--no-headers"],
                stderr=subprocess.DEVNULL, timeout=3,
            ).decode(errors="replace").strip().splitlines()
            if not out:
                return None
            parts = out[0].split()
            if len(parts) < 2:
                return None
            return " ".join(parts[:-1]), float(parts[-1])
        except Exception:
            return None

    @staticmethod
    def _sentry_throttled_count() -> int | None:
        # Try the couple of cgroup layouts systemd is likely to have created
        # sentry.slice under. If neither exists, we honestly don't know.
        for path in (
            Path("/sys/fs/cgroup/sentry.slice/cgroup.procs"),
            Path("/sys/fs/cgroup/system.slice/sentry.slice/cgroup.procs"),
        ):
            if path.exists():
                try:
                    return len([l for l in path.read_text().splitlines() if l.strip()])
                except Exception:
                    return None
        return None

    @staticmethod
    def _sentry_stats() -> dict | None:
        if not GT_SENTRY_STATS.exists():
            return None
        try:
            return json.loads(GT_SENTRY_STATS.read_text())
        except Exception:
            return None

    @staticmethod
    def _current_governor() -> str | None:
        p = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
        if p.exists():
            try:
                return p.read_text().strip()
            except Exception:
                return None
        return None


class GaugeBar(QWidget):
    """
    One labeled gauge: a title, then a monospace block-bar and a value —
    e.g. 'CPU' / '███████░░░ 41%'. Deliberately just text and color, no
    custom painting needed for something this simple.
    """
    BAR_WIDTH = 22  # characters

    def __init__(self, title: str, color: str, parent=None):
        super().__init__(parent)
        self._color = color
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        title_lbl = QLabel(title)
        title_lbl.setStyleSheet(
            f"color: {COLORS['text_secondary']}; font-size: 11px; font-weight: 600; letter-spacing: 0.5px;"
        )
        layout.addWidget(title_lbl)
        self._bar = QLabel("—")
        self._bar.setTextFormat(Qt.TextFormat.RichText)
        self._bar.setStyleSheet("font-family: monospace; font-size: 14px;")
        layout.addWidget(self._bar)

    def set_value(self, fraction: float | None, value_text: str):
        """fraction: 0.0-1.0, or None to render an empty/unavailable bar."""
        if fraction is None:
            self._bar.setText(
                f'<span style="color:{COLORS["border"]}">{"░" * self.BAR_WIDTH}</span>'
                f'&nbsp;&nbsp;<span style="color:{COLORS["text_muted"]}">{value_text}</span>'
            )
            return
        fraction = max(0.0, min(1.0, fraction))
        filled = round(fraction * self.BAR_WIDTH)
        empty = self.BAR_WIDTH - filled
        self._bar.setText(
            f'<span style="color:{self._color}">{"█" * filled}</span>'
            f'<span style="color:{COLORS["border"]}">{"░" * empty}</span>'
            f'&nbsp;&nbsp;<span style="color:{COLORS["text_primary"]}">{value_text}</span>'
        )


class MiniHistoryChart(QWidget):
    """
    Tiny rolling line+area chart for the last N samples. One series, one
    color, no axes/legend/zoom/pan — just enough to see "yes, that changed".
    """
    def __init__(self, title: str, color: str, max_samples: int = 60, y_max: float = 100.0, parent=None):
        super().__init__(parent)
        self._title = title
        self._color = QColor(color)
        self._max_samples = max_samples
        self._y_max = y_max
        self._samples: list[float] = []
        self.setMinimumHeight(90)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def add_sample(self, value: float | None):
        self._samples.append(0.0 if value is None else value)
        if len(self._samples) > self._max_samples:
            self._samples.pop(0)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()
        pad_top, pad_bottom, pad_side = 18, 4, 4
        chart_rect = QRect(pad_side, pad_top, rect.width() - 2 * pad_side, rect.height() - pad_top - pad_bottom)

        painter.setPen(QColor(COLORS['text_secondary']))
        painter.setFont(QFont("sans-serif", 9, QFont.Weight.DemiBold))
        painter.drawText(QRect(pad_side, 0, chart_rect.width() // 2, pad_top),
                          Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self._title)
        current = self._samples[-1] if self._samples else 0.0
        painter.setPen(self._color)
        painter.drawText(QRect(pad_side, 0, chart_rect.width(), pad_top),
                          Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                          f"{current:.0f}")

        painter.setPen(QPen(QColor(COLORS['border']), 1, Qt.PenStyle.DotLine))
        mid_y = chart_rect.top() + chart_rect.height() // 2
        painter.drawLine(chart_rect.left(), mid_y, chart_rect.right(), mid_y)

        if len(self._samples) < 2:
            return

        n = len(self._samples)
        step_x = chart_rect.width() / max(1, self._max_samples - 1)
        offset = self._max_samples - n  # right-align: newest sample at the right edge

        def point_for(i, v):
            x = chart_rect.left() + (offset + i) * step_x
            frac = max(0.0, min(1.0, v / self._y_max))
            y = chart_rect.bottom() - frac * chart_rect.height()
            return QPointF(x, y)

        pts = [point_for(i, v) for i, v in enumerate(self._samples)]

        path = QPainterPath()
        path.moveTo(pts[0])
        for p in pts[1:]:
            path.lineTo(p)

        area = QPainterPath()
        area.moveTo(pts[0].x(), chart_rect.bottom())
        for p in pts:
            area.lineTo(p)
        area.lineTo(pts[-1].x(), chart_rect.bottom())
        area.closeSubpath()

        grad = QLinearGradient(0, chart_rect.top(), 0, chart_rect.bottom())
        fill_color = QColor(self._color); fill_color.setAlpha(90)
        transparent = QColor(self._color); transparent.setAlpha(0)
        grad.setColorAt(0, fill_color)
        grad.setColorAt(1, transparent)
        painter.fillPath(area, QBrush(grad))

        painter.setPen(QPen(self._color, 2))
        painter.drawPath(path)


class DashboardPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._build()
        self._sampler = DashboardSamplerThread(self)
        self._sampler.sample_ready.connect(self._on_sample)
        self._sampler.start()

    def stop_sampler(self):
        self._sampler.stop()
        self._sampler.wait(2000)

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        def build(layout):
            layout.setContentsMargins(32, 28, 32, 28)
            layout.setSpacing(18)

            title = QLabel("Dashboard")
            title.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: 22px; font-weight: 700;")
            layout.addWidget(title)
            layout.addWidget(Divider())

            # ── At a glance ──────────────────────────────────────────────
            glance = Card("System at a Glance")
            gl = glance.body()
            grid = QGridLayout()
            grid.setHorizontalSpacing(32)
            grid.setVerticalSpacing(14)
            self._gauge_cpu  = GaugeBar("CPU",  COLORS['accent'])
            self._gauge_ram  = GaugeBar("RAM",  COLORS['success'])
            self._gauge_gpu  = GaugeBar("GPU",  COLORS['warning'])
            self._gauge_vram = GaugeBar("VRAM", COLORS['error'])
            grid.addWidget(self._gauge_cpu,  0, 0)
            grid.addWidget(self._gauge_ram,  0, 1)
            grid.addWidget(self._gauge_gpu,  1, 0)
            grid.addWidget(self._gauge_vram, 1, 1)
            gl.addLayout(grid)
            gl.addWidget(Divider())
            temp_col = QVBoxLayout()
            temp_title = QLabel("Temperature")
            temp_title.setStyleSheet(
                f"color: {COLORS['text_secondary']}; font-size: 11px; font-weight: 600; letter-spacing: 0.5px;"
            )
            temp_col.addWidget(temp_title)
            self._temp_value = QLabel("CPU  —      GPU  —")
            self._temp_value.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: 14px; font-family: monospace;")
            temp_col.addWidget(self._temp_value)
            gl.addLayout(temp_col)
            layout.addWidget(glance)

            # ── ProcessSentry + Kernel Autotune ──────────────────────────
            status_row = QHBoxLayout()
            status_row.setSpacing(18)

            sentry_card = Card("Process Sentry")
            sf = sentry_card.body()
            self._sentry_labels: dict[str, QLabel] = {}
            for key, label in [
                ("status", "Status"),
                ("throttled", "Processes throttled"),
                ("reprioritized", "Processes reprioritized"),
                ("highest", "Highest CPU process"),
                ("governor", "CPU profile"),
                ("gamemode", "GameMode"),
            ]:
                row = QHBoxLayout()
                l = QLabel(label)
                l.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 12px;")
                v = QLabel("—")
                v.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: 12px; font-weight: 600;")
                row.addWidget(l); row.addStretch(); row.addWidget(v)
                sf.addLayout(row)
                self._sentry_labels[key] = v
            status_row.addWidget(sentry_card, 1)

            kernel_card = Card("Kernel Autotune")
            kf = kernel_card.body()
            self._kernel_display = QPlainTextEdit()
            self._kernel_display.setReadOnly(True)
            self._kernel_display.setMinimumHeight(180)
            self._kernel_display.setStyleSheet(
                f"background: {COLORS['bg_darkest']}; color: {COLORS['text_secondary']};"
                f"font-family: monospace; font-size: 11px; border: none;"
            )
            kf.addWidget(self._kernel_display)
            status_row.addWidget(kernel_card, 1)

            layout.addLayout(status_row)

            # ── History ──────────────────────────────────────────────────
            history_card = Card("History (last 60 seconds)")
            hl = history_card.body()
            hgrid = QGridLayout()
            hgrid.setHorizontalSpacing(24)
            hgrid.setVerticalSpacing(16)
            self._chart_cpu  = MiniHistoryChart("CPU %",  COLORS['accent'],  y_max=100)
            self._chart_ram  = MiniHistoryChart("RAM %",  COLORS['success'], y_max=100)
            self._chart_gpu  = MiniHistoryChart("GPU %",  COLORS['warning'], y_max=100)
            self._chart_vram = MiniHistoryChart("VRAM %", COLORS['error'],   y_max=100)
            hgrid.addWidget(self._chart_cpu,  0, 0)
            hgrid.addWidget(self._chart_ram,  0, 1)
            hgrid.addWidget(self._chart_gpu,  1, 0)
            hgrid.addWidget(self._chart_vram, 1, 1)
            hl.addLayout(hgrid)
            layout.addWidget(history_card)

            layout.addStretch()

        outer.addWidget(gt_scrolled(build))
        self._load_kernel_autotune()

    def _load_kernel_autotune(self):
        s, err = gt_read_kernel_autotune_state()
        if s is None:
            self._kernel_display.setPlainText(f"({err})")
        else:
            self._kernel_display.setPlainText(gt_format_kernel_autotune_summary(s))

    def _on_sample(self, data: dict):
        if not data:
            return

        cpu = data.get("cpu_percent")
        self._gauge_cpu.set_value(None if cpu is None else cpu / 100, "—" if cpu is None else f"{cpu:.0f}%")
        self._chart_cpu.add_sample(cpu)

        ram = data.get("ram")
        if ram:
            self._gauge_ram.set_value(ram["percent"] / 100, f'{ram["used_gb"]:.1f} / {ram["total_gb"]:.0f} GB')
            self._chart_ram.add_sample(ram["percent"])
        else:
            self._gauge_ram.set_value(None, "—")
            self._chart_ram.add_sample(None)

        gpu = data.get("gpu")
        if gpu and gpu.get("percent") is not None:
            self._gauge_gpu.set_value(gpu["percent"] / 100, f'{gpu["percent"]:.0f}%')
            self._chart_gpu.add_sample(gpu["percent"])
        else:
            self._gauge_gpu.set_value(None, "not available")
            self._chart_gpu.add_sample(None)

        if gpu and gpu.get("vram_total_gb"):
            vram_pct = 100.0 * gpu["vram_used_gb"] / gpu["vram_total_gb"]
            self._gauge_vram.set_value(vram_pct / 100, f'{gpu["vram_used_gb"]:.1f} / {gpu["vram_total_gb"]:.0f} GB')
            self._chart_vram.add_sample(vram_pct)
        else:
            self._gauge_vram.set_value(None, "not available")
            self._chart_vram.add_sample(None)

        cpu_temp = data.get("cpu_temp")
        gpu_temp = gpu.get("temp") if gpu else None
        self._temp_value.setText(
            f"CPU  {'—' if cpu_temp is None else f'{cpu_temp:.0f}°C'}      "
            f"GPU  {'—' if gpu_temp is None else f'{gpu_temp:.0f}°C'}"
        )

        status = data.get("sentry_status", "unknown")
        self._sentry_labels["status"].setText("✓ Running" if status == "active" else status.capitalize())
        throttled = data.get("sentry_throttled")
        self._sentry_labels["throttled"].setText("—" if throttled is None else str(throttled))
        stats = data.get("sentry_stats")
        reprioritized = stats.get("processes_reprioritized") if stats else None
        self._sentry_labels["reprioritized"].setText("—" if reprioritized is None else str(reprioritized))
        hp = data.get("highest_process")
        self._sentry_labels["highest"].setText("—" if not hp else f"{hp[0]} ({hp[1]:.0f}%)")
        self._sentry_labels["governor"].setText(data.get("cpu_governor") or "—")
        self._sentry_labels["gamemode"].setText("Enabled" if data.get("gamemode_enabled") else "Disabled")


# ─────────────────────────────────────────────
#  SIDEBAR
# ─────────────────────────────────────────────

def find_app_icon() -> Path | None:
    """
    Locate griffinhub.png.

    The primary copy lives right next to griffinhub.py itself
    (Path(__file__).parent) — that's the one the app uses internally for its
    own UI (the Sidebar logo, the window icon), and it's what the AppImage
    build copies into usr/src/ alongside the script for exactly that reason.

    The AppDir-root copy ($APPDIR/griffinhub.png) is a separate, second copy
    that exists purely for the OS side of things — appimagetool/.desktop/the
    icon theme — and isn't what the app itself reads. It's kept here only as
    a fallback in case the primary copy is ever missing.
    """
    candidates = [Path(__file__).parent / APP_ICON_NAME]
    appdir = os.environ.get("APPDIR")
    if appdir:
        candidates.append(Path(appdir) / APP_ICON_NAME)
        candidates.append(Path(appdir) / "usr" / "share" / "icons" / "hicolor" / "256x256" / "apps" / APP_ICON_NAME)
    candidates.append(BASE_DIR / APP_ICON_NAME)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


class Sidebar(QWidget):
    page_changed = pyqtSignal(int)

    NAV_ITEMS = [
        ("🏠", "Dashboard",     0),
        ("🎮", "System Tune",  1),
        ("🔧", "Hardware Hub", 2),
        ("🧩", "Plugins",      3),
        ("⚙",  "Settings",     4),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(220)
        self.nav_items = []
        self._build()

    def _build(self):
        self.setStyleSheet(f"""
            Sidebar {{
                background-color: {COLORS['bg_dark']};
                border-right: 1px solid {COLORS['border']};
            }}
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        logo_widget = QWidget()
        logo_widget.setFixedHeight(120)
        logo_widget.setStyleSheet(f"background-color: {COLORS['bg_darkest']}; border-bottom: 1px solid {COLORS['border']};")
        ll = QHBoxLayout(logo_widget)
        ll.setContentsMargins(16, 16, 16, 16)

        logo_icon = QLabel()
        logo_icon.setFixedSize(85, 85)
        logo_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo_icon.setStyleSheet(f"""
            background: qlineargradient(x1:0,y1:0,x2:1,y2:1,
                stop:0 {COLORS['bg_mid']}, stop:1 {COLORS['accent_dim']});
            color: {COLORS['accent_glow']};
            font-size: 16px; font-weight: 900;
            border-radius: 10px; border: 1px solid {COLORS['accent_dim']};
        """)
        logo_icon.setText("GH")

        icon_path = find_app_icon()
        if icon_path:
            pix = QPixmap(str(icon_path)).scaled(
                120, 120, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            )
            logo_icon.setPixmap(pix)
            logo_icon.setStyleSheet("border-radius: 10px; background: transparent;")

        logo_text = QVBoxLayout()
        logo_text.setSpacing(0)
        name_lbl = QLabel("Griffin Hub")
        name_lbl.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: 16px; font-weight: 700; letter-spacing: -0.3px;")
        tagline = QLabel("System Tune & Hardware")
        tagline.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 12px; letter-spacing: 0.3px;")
        logo_text.addWidget(name_lbl)
        logo_text.addWidget(tagline)
        ll.addWidget(logo_icon)
        ll.addLayout(logo_text)
        ll.addStretch()
        layout.addWidget(logo_widget)

        nav_section = QWidget()
        nl = QVBoxLayout(nav_section)
        nl.setContentsMargins(0, 12, 0, 12)
        nl.setSpacing(2)

        top_label = QLabel("  MAIN")
        top_label.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 10px; letter-spacing: 2px; padding: 0 16px 4px 16px;")
        nl.addWidget(top_label)

        for icon, label, idx in self.NAV_ITEMS:
            item = NavItem(icon, label, idx)
            item.clicked.connect(lambda item=item: self._on_nav(item))
            self.nav_items.append(item)
            nl.addWidget(item)

        nl.addStretch()
        layout.addWidget(nav_section)

        ver = QLabel(f"v{APP_VERSION}")
        ver.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ver.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 10px; padding: 10px;")
        layout.addWidget(ver)

        self.nav_items[0].set_active(True)

    def _on_nav(self, item):
        for nav in self.nav_items:
            nav.set_active(nav.index == item.index)
        self.page_changed.emit(item.index)

    def set_page(self, idx):
        for nav in self.nav_items:
            nav.set_active(nav.index == idx)


# ─────────────────────────────────────────────
#  MAIN WINDOW
# ─────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Griffin Hub")
        self.setMinimumSize(1100, 720)
        self.resize(1200, 780)
        self._set_icon()
        self._build()

    def _set_icon(self):
        icon_path = find_app_icon()
        if icon_path:
            self.setWindowIcon(QIcon(str(icon_path)))

    def _build(self):
        central = QWidget()
        central.setStyleSheet(f"background-color: {COLORS['bg_darkest']};")
        self.setCentralWidget(central)

        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.sidebar = Sidebar()
        root.addWidget(self.sidebar)

        self.stack = QStackedWidget()
        self.stack.setStyleSheet(f"background-color: {COLORS['bg_mid']};")
        root.addWidget(self.stack)

        self.dashboard_pg = DashboardPage()
        self.systune_pg   = SystemTunePage()
        self.hardware_pg  = HardwarePage()
        self.plugins_pg   = PluginsPage()
        self.settings_pg  = SettingsPage()

        self.pages = [
            self.dashboard_pg, self.systune_pg, self.hardware_pg, self.plugins_pg, self.settings_pg,
        ]
        for page in self.pages:
            self.stack.addWidget(page)

        self.sidebar.page_changed.connect(self._navigate)

    def _navigate(self, idx: int):
        self.stack.setCurrentIndex(idx)
        self.sidebar.set_page(idx)

    def closeEvent(self, event):
        self.dashboard_pg.stop_sampler()
        super().closeEvent(event)




# ─────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────

def main():
    # ── CLI mode — intercept before Qt loads ──────────────────────────────
    CLI_COMMANDS = {"plugin", "store", "settings", "--version", "--help", "-h"}
    if len(sys.argv) > 1 and sys.argv[1] in CLI_COMMANDS:
        _cli_main()
        return

    # ── GUI mode ──────────────────────────────────────────────────────────
    try:
        for d in [BASE_DIR, CACHE_DIR, PLUGINS_DIR, PLUGIN_DRIVERS_DIR, LOGS_DIR]:
            d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"ERROR: Cannot create Griffin Hub data directories: {e}", file=sys.stderr)
        print(f"       Check permissions on: {BASE_DIR}", file=sys.stderr)
        sys.exit(1)

    log.info("=== Griffin Hub %s starting (GUI) ===", APP_VERSION)
    REGISTRY.load_plugins()

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setStyleSheet(STYLESHEET)

    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window,          QColor(COLORS["bg_darkest"]))
    palette.setColor(QPalette.ColorRole.WindowText,      QColor(COLORS["text_primary"]))
    palette.setColor(QPalette.ColorRole.Base,            QColor(COLORS["bg_card"]))
    palette.setColor(QPalette.ColorRole.AlternateBase,   QColor(COLORS["bg_mid"]))
    palette.setColor(QPalette.ColorRole.Text,            QColor(COLORS["text_primary"]))
    palette.setColor(QPalette.ColorRole.Button,          QColor(COLORS["bg_card"]))
    palette.setColor(QPalette.ColorRole.ButtonText,      QColor(COLORS["text_primary"]))
    palette.setColor(QPalette.ColorRole.Highlight,       QColor(COLORS["accent"]))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(COLORS["white"]))
    palette.setColor(QPalette.ColorRole.Link,            QColor(COLORS["accent"]))
    app.setPalette(palette)

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


# ─────────────────────────────────────────────
#  CLI  (headless — no Qt required)
# ─────────────────────────────────────────────

def _cli_main():
    """Dispatch headless CLI commands."""
    import argparse

    # QThread workers emit Qt signals; a QCoreApplication is required
    # for cross-thread signal delivery even in headless CLI mode.
    from PyQt6.QtCore import QCoreApplication as _QCA
    _qapp = _QCA.instance() or _QCA(sys.argv)

    try:
        for d in [BASE_DIR, CACHE_DIR, PLUGINS_DIR, PLUGIN_DRIVERS_DIR, LOGS_DIR]:
            d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"ERROR: Cannot create Griffin Hub data directories: {e}", file=sys.stderr)
        print(f"       Check permissions on: {BASE_DIR}", file=sys.stderr)
        sys.exit(1)
    log.info("=== Griffin Hub %s starting (CLI: %s) ===", APP_VERSION, " ".join(sys.argv[1:]))
    REGISTRY.load_plugins()

    parser = argparse.ArgumentParser(
        prog="griffinhub",
        description=(
            f"Griffin Hub {APP_VERSION} — System Tune & Hardware Hub\n\n"
            "Examples:\n"
            "  griffinhub store search\n"
            "  griffinhub plugin install my-plugin\n"
            "  griffinhub plugin update --all\n"
            "  griffinhub settings list\n"
            "  griffinhub settings set plugin_sandbox true\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="store_true", help="Print version and exit")
    sub = parser.add_subparsers(dest="cmd", metavar="COMMAND")

    # ── plugin subcommand ───────────────────────────────────────────────
    p_plug = sub.add_parser(
        "plugin",
        help="Manage Griffin Hub plugins from the community store",
        description=(
            "Install, update, remove, list, and inspect plugins.\n\n"
            "  griffinhub plugin list\n"
            "  griffinhub plugin install my-plugin\n"
            "  griffinhub plugin update my-plugin\n"
            "  griffinhub plugin update --all\n"
            "  griffinhub plugin remove my-plugin\n"
            "  griffinhub plugin info my-plugin\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_plug.add_argument(
        "action",
        choices=["list", "install", "update", "remove", "info"],
        metavar="ACTION",
        help="list | install | update | remove | info",
    )
    p_plug.add_argument(
        "plugin_id", nargs="?", default="",
        metavar="PLUGIN_ID",
        help="Plugin ID (required for install / update / remove / info)",
    )
    p_plug.add_argument(
        "--all", dest="all_plugins", action="store_true",
        help="Apply action to all installed plugins (only valid with 'update')",
    )
    p_plug.add_argument(
        "--force-refresh", action="store_true",
        help="Bypass the store index cache and re-fetch from the network",
    )

    # ── store subcommand ──────────────────────────────────────────────────
    p_store = sub.add_parser(
        "store",
        help="Browse the community plugin store",
        description=(
            "Search and browse available plugins.\n\n"
            "  griffinhub store search\n"
            "  griffinhub store search hardware\n"
            "  griffinhub store refresh\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_store.add_argument(
        "action",
        nargs="?", default="search",
        choices=["search", "refresh"],
        metavar="ACTION",
        help="search (default) | refresh (clear cache and re-fetch index)",
    )
    p_store.add_argument(
        "query", nargs="?", default="",
        metavar="QUERY",
        help="Search query (substring match against id / name / description)",
    )
    p_store.add_argument(
        "--capability", "-c", default="",
        metavar="CAP",
        help="Filter results to plugins declaring this capability",
    )
    p_store.add_argument(
        "--verified", action="store_true",
        help="Only show verified plugins",
    )

    # ── settings subcommand ───────────────────────────────────────────────
    p_set = sub.add_parser(
        "settings",
        help="Read or write Griffin Hub settings from the CLI",
        description=(
            "Read and write settings without opening the GUI.\n\n"
            "  griffinhub settings list\n"
            "  griffinhub settings get plugin_sandbox\n"
            "  griffinhub settings set plugin_sandbox true\n"
            "  griffinhub settings set store_url https://example.com/index.json\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_set.add_argument(
        "action",
        choices=["list", "get", "set"],
        metavar="ACTION",
        help="list | get | set",
    )
    p_set.add_argument(
        "key", nargs="?", default="",
        metavar="KEY",
        help="Setting key (required for get / set)",
    )
    p_set.add_argument(
        "value", nargs="?", default=None,
        metavar="VALUE",
        help="New value (required for set)",
    )

    args = parser.parse_args(sys.argv[1:])

    if args.version:
        print(f"Griffin Hub {APP_VERSION}")
        return

    dispatch = {
        "plugin":   _cli_plugin,
        "store":    _cli_store,
        "settings": _cli_settings,
    }
    fn = dispatch.get(args.cmd)
    if fn is None:
        parser.print_help()
        return
    sys.exit(fn(args) or 0)


# ── CLI helpers ────────────────────────────────────────────────────────────

def _cli_ok(m):   print(f"\033[32m✓\033[0m  {m}")
def _cli_info(m): print(f"   {m}")
def _cli_warn(m): print(f"\033[33m⚠\033[0m  {m}")
def _cli_err(m):  print(f"\033[31m✗\033[0m  {m}", file=sys.stderr)


def _cli_plugin(args) -> int:
    """Manage plugins from the community store."""
    action      = args.action
    plugin_id   = getattr(args, "plugin_id", "").strip()
    all_flag    = getattr(args, "all_plugins", False)
    force       = getattr(args, "force_refresh", False)

    if force:
        STORE.invalidate_cache()

    # ── list ──────────────────────────────────────────────────────────────
    if action == "list":
        state = Store.load_plugin_state()
        if not state:
            _cli_info("No plugins installed. Use `griffinhub store search` to browse.")
            return 0
        print(f"{'ID':<25}  {'Name':<28}  {'Version':<10}  {'Author':<20}  Verified")
        print("─" * 95)
        for pid, rec in state.items():
            ver      = rec.get("version", "?")
            name     = rec.get("name", pid)
            author   = rec.get("author", "")
            verified = "✓" if rec.get("verified") else " "
            print(f"{pid:<25}  {name:<28}  {ver:<10}  {author:<20}  {verified}")
        return 0

    # ── update --all ──────────────────────────────────────────────────────
    if action == "update" and all_flag:
        results = STORE.update_all(
            progress_cb=lambda pct, msg: _cli_info(f"[{pct:3d}%] {msg}")
        )
        if not results:
            _cli_info("No plugins installed.")
            return 0
        errors = 0
        for pid, ok, msg in results:
            ((_cli_ok if ok else _cli_err))(f"{pid}: {msg}")
            if not ok:
                errors += 1
        return 1 if errors else 0

    # ── actions that need a plugin_id ─────────────────────────────────────
    if not plugin_id:
        _cli_err(f"Action '{action}' requires a PLUGIN_ID argument.")
        return 1

    if action == "install":
        _cli_info(f"Installing plugin '{plugin_id}' from store…")
        ok, msg = STORE.install(
            plugin_id,
            progress_cb=lambda pct, m: _cli_info(f"[{pct:3d}%] {m}")
        )
        (_cli_ok if ok else _cli_err)(msg)
        return 0 if ok else 1

    if action == "update":
        _cli_info(f"Checking for updates: '{plugin_id}'…")
        ok, msg = STORE.update(
            plugin_id,
            progress_cb=lambda pct, m: _cli_info(f"[{pct:3d}%] {m}")
        )
        (_cli_ok if ok else _cli_err)(msg)
        return 0 if ok else 1

    if action == "remove":
        ok, msg = STORE.uninstall(plugin_id)
        (_cli_ok if ok else _cli_err)(msg)
        return 0 if ok else 1

    if action == "info":
        try:
            entry = STORE.get_plugin_entry(plugin_id)
        except Exception as e:
            _cli_warn(f"Could not reach store: {e}")
            entry = None
        record = Store.get_plugin_record(plugin_id)

        if entry is None and record is None:
            _cli_err(f"Plugin '{plugin_id}' not found in store or locally installed.")
            return 1

        if entry:
            print(f"\n  {'Name':<14} {entry.get('name', plugin_id)}")
            print(f"  {'ID':<14} {entry['id']}")
            print(f"  {'Version':<14} {entry.get('version','?')}")
            print(f"  {'Author':<14} {entry.get('author','')}")
            print(f"  {'Verified':<14} {'Yes' if entry.get('verified') else 'No'}")
            print(f"  {'API version':<14} {entry.get('api_version', 1)}")
            caps = ", ".join(entry.get("capabilities", [])) or "—"
            print(f"  {'Capabilities':<14} {caps}")
            print(f"  {'Description':<14} {entry.get('description','')}")

        if record:
            print(f"\n  Installed:")
            print(f"    Version     {record.get('version','?')}")
            print(f"    Installed   {record.get('installed_at','?')}")
            print(f"    Location    {record.get('dest_dir','?')}")
            if entry:
                local_v  = record.get("version", "0")
                remote_v = entry.get("version", "0")
                if STORE._version_newer(remote_v, local_v):
                    _cli_warn(f"Update available: {local_v} → {remote_v}")
                else:
                    _cli_ok("Up to date.")
        else:
            _cli_info("Not installed locally.")
        print()
        return 0

    _cli_err(f"Unknown plugin action: '{action}'")
    return 1


def _cli_store(args) -> int:
    """Browse the community plugin store."""
    action     = getattr(args, "action", "search") or "search"
    query      = getattr(args, "query", "") or ""
    capability = getattr(args, "capability", "") or ""
    only_veri  = getattr(args, "verified", False)

    if action == "refresh":
        STORE.invalidate_cache()
        _cli_info("Store cache cleared.")
        action = "search"

    _cli_info("Fetching store index…")
    try:
        results = STORE.search(query=query, capability=capability)
    except GriffinHubError as e:
        _cli_err(str(e))
        return 1

    if only_veri:
        results = [r for r in results if r.get("verified")]

    if not results:
        _cli_info("No plugins found matching your criteria.")
        return 0

    installed_ids = set(Store.load_plugin_state().keys())
    print()
    print(f"  {'ID':<25}  {'Name':<28}  {'Ver':<8}  {'Caps':<22}  Status")
    print("  " + "─" * 95)
    for p in results:
        pid   = p.get("id", "?")
        name  = p.get("name", pid)
        ver   = p.get("version", "?")
        caps  = ", ".join(p.get("capabilities", []))[:20] or "—"
        vmark = " ✓" if p.get("verified") else "  "
        inst  = " [installed]" if pid in installed_ids else ""
        rec   = Store.get_plugin_record(pid)
        if rec and STORE._version_newer(ver, rec.get("version", "0")):
            inst = " [update available]"
        print(f"  {pid:<25}  {name:<28}  {ver:<8}  {caps:<22}  {vmark}{inst}")
    print()
    _cli_info(f"{len(results)} plugin(s) found. Install with: griffinhub plugin install <id>")
    return 0


def _cli_settings(args) -> int:
    """Read or write Griffin Hub settings from the CLI."""
    action = args.action
    key    = getattr(args, "key", "").strip()
    value  = getattr(args, "value", None)

    settings = Store.load_settings()

    if action == "list":
        print(f"\n  {'Key':<30}  Value")
        print("  " + "─" * 60)
        for k, v in sorted(settings.items()):
            print(f"  {k:<30}  {v!r}")
        print()
        return 0

    if action == "get":
        if not key:
            _cli_err("'settings get' requires a KEY argument.")
            return 1
        if key not in settings:
            _cli_err(f"Unknown setting: '{key}'.  Use `griffinhub settings list` to see all keys.")
            return 1
        print(f"{key} = {settings[key]!r}")
        return 0

    if action == "set":
        if not key:
            _cli_err("'settings set' requires KEY and VALUE arguments.")
            return 1
        if value is None:
            _cli_err(f"'settings set {key}' requires a VALUE argument.")
            return 1

        current = settings.get(key)
        if isinstance(current, bool) or value.lower() in ("true", "false", "1", "0", "yes", "no"):
            value = value.lower() in ("true", "1", "yes")
        elif isinstance(current, int):
            try:
                value = int(value)
            except ValueError:
                _cli_err(f"Setting '{key}' expects an integer, got: {value!r}")
                return 1

        settings[key] = value
        Store.save_settings(settings)
        _cli_ok(f"Set {key} = {value!r}")

        if key == "plugin_sandbox" and value is True:
            if not shutil.which("bwrap"):
                _cli_warn(
                    "plugin_sandbox enabled but bubblewrap (bwrap) not found. "
                    "Plugins will fall back to minimal subprocess evaluation. "
                    "Install bubblewrap for full isolation: sudo apt install bubblewrap"
                )
        return 0

    _cli_err(f"Unknown settings action: '{action}'")
    return 1


if __name__ == "__main__":
    main()

