"""
Multi-distro controller driver definitions and install recipes.

Each driver entry provides:
  - Human-readable metadata (label, desc, note)
  - check: shell snippet that exits 0 when the driver is present
  - install: callable that returns a shell command tailored to the host distro

Install recipes are provided by the active distro module (see
griffinhub/distro_modules).  On Arch that means AUR packages are built as the
desktop user without an AUR helper (installs run as root, and makepkg / AUR
helpers refuse root), with an automatic fallback to the upstream DKMS build.
"""

from __future__ import annotations

from .distro import detect  # noqa: F401  (kept for callers importing it from here)


# ── Per-driver install recipes ──────────────────────────────────────────────
#
# The recipes live in the distro modules (griffinhub/distro_modules): each one
# knows its package manager, the right kernel headers, AUR / RPM Fusion / DKMS
# handling and Secure Boot guidance.  These wrappers just ask the active module.

from . import _modloader


def _xone_install() -> str:
    return _modloader.feature_script("xone")


def _xpad_noone_install() -> str:
    return _modloader.feature_script("xpad-noone")


def _paroj_xpad_install() -> str:
    return _modloader.feature_script("xpad-paroj")


def _xpadneo_install() -> str:
    return _modloader.feature_script("xpadneo")


def _hid_tmff2_install() -> str:
    return _modloader.feature_script("hid-tmff2")


def _ps_controllers_install() -> str:
    return _modloader.feature_script("joystick")


# ── Driver catalogue ────────────────────────────────────────────────────────

DRIVERS: dict[str, dict] = {
    "xone": {
        "color": "success",
        "label": "xone",
        "for": "Xbox One & Xbox Series X|S (USB + Wireless Dongle)",
        "desc": (
            "Recommended for modern Xbox controllers. Supports Xbox One, Series X|S, "
            "adaptive controller, and third-party GIP accessories. Includes audio "
            "passthrough and rumble. Replaces the older xpad driver for Xbox One devices."
        ),
        "note": (
            "Pair with xpad-noone to avoid conflicts. "
            "On Arch: prefers AUR xone-dkms (+ xone-dongle-firmware). "
            "On newer kernels (6.4+) the -git package is recommended. "
            "Secure Boot may block unsigned DKMS modules."
        ),
        "check": "dkms status 2>/dev/null | grep -qi '^xone[,/]'",
        "install": _xone_install,
    },
    "xpad-noone": {
        "color": "accent",
        "label": "xpad-noone",
        "for": "Xbox / Xbox 360 (used alongside xone)",
        "desc": (
            "Modified xpad driver with Xbox One support removed. Required when xone "
            "is installed so that original Xbox and Xbox 360 controllers still work."
        ),
        "note": "Only needed if you also have original Xbox/360 controllers.",
        "check": "dkms status 2>/dev/null | grep -qi '^xpad-noone[,/]'",
        "install": _xpad_noone_install,
    },
    "paroj/xpad": {
        "color": "accent",
        "label": "paroj/xpad (GitHub)",
        "for": "Xbox / Xbox 360 / Xbox One (USB only, community fork)",
        "desc": (
            "Community-maintained fork of the kernel's xpad driver with extra device "
            "support. Good for Xbox 360 and older Xbox One via USB if you don't need "
            "the full xone feature set."
        ),
        "note": "USB-only. No Wireless Dongle support.",
        "check": "dkms status 2>/dev/null | grep -qi '^xpad,'",
        "install": _paroj_xpad_install,
    },
    "xpad (kernel)": {
        "color": "text_muted",
        "label": "xpad (kernel built-in)",
        "for": "Xbox / Xbox 360 / basic Xbox One (USB)",
        "desc": (
            "The xpad driver shipped with the Linux kernel. Supports Xbox and Xbox 360 "
            "controllers out of the box with no installation needed."
        ),
        "note": "Already in your kernel — no install needed.",
        "check": None,
        "install": "",
    },
    "xpadneo": {
        "color": "accent",
        "label": "xpadneo",
        "for": "Xbox One / Series X|S / Xbox Elite 2 (Bluetooth)",
        "desc": (
            "Best driver for Xbox controllers over Bluetooth. Supports rumble "
            "(with trigger force feedback), battery level, paddle buttons (Elite 2), "
            "and consistent button mapping."
        ),
        "note": "Bluetooth only. Use xone for USB/dongle.",
        "check": "dkms status 2>/dev/null | grep -qi '^xpadneo[,/]'",
        "install": _xpadneo_install,
    },
    "hid-tmff2": {
        "color": "warning",
        "label": "hid-tmff2",
        "for": "Thrustmaster T300RS, T248, TX, T128, TS-PC, TS-XW wheels",
        "desc": (
            "Force feedback kernel module for Thrustmaster racing wheels. "
            "Supports T300RS and T248 fully; experimental support for TX, T128, "
            "T598, TS-PC, and TS-XW."
        ),
        "note": "Firmware must be v31 or newer (update via Windows Thrustmaster tools if needed).",
        "check": "dkms status 2>/dev/null | grep -qi '^hid-tmff2[,/]'",
        "install": _hid_tmff2_install,
    },
    "ps-controllers": {
        "color": "accent",
        "label": "hid-playstation / joystick",
        "for": "PlayStation DualShock 4, DualSense (PS5)",
        "desc": (
            "Modern PlayStation controllers are supported via hid-playstation "
            "(built-in since kernel 5.12 for DualSense). DualShock 4 works natively "
            "via USB/Bluetooth with SDL2."
        ),
        "note": "DualSense and DS4 usually work out of the box on recent kernels.",
        "check": None,
        "check_pkgs": {
            "debian": ["joystick"],
            "rhel":   ["joystick"],
            "suse":   ["joystick"],
            "arch":   ["joystick"],
        },
        "install": _ps_controllers_install,
    },
}


def get_install_command(driver_key: str) -> str:
    """
    Return the fully-resolved shell install command for *driver_key*
    on the current distro.  Empty string means "nothing to install".
    """
    entry = DRIVERS.get(driver_key)
    if not entry:
        return ""
    inst = entry.get("install")
    if inst is None or inst == "":
        return ""
    if callable(inst):
        return inst()
    return str(inst)


def get_check_command(driver_key: str) -> str | None:
    entry = DRIVERS.get(driver_key)
    if not entry:
        return None
    return entry.get("check")


def list_drivers() -> list[tuple[str, dict]]:
    """Return (key, metadata) pairs in display order."""
    return list(DRIVERS.items())
