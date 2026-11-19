"""
Multi-distro package & driver installation abstraction.

Supports:
  - Debian/Ubuntu family  (apt)
  - Fedora/RHEL family    (dnf)
  - openSUSE              (zypper)
  - Arch/Manjaro/etc.     (pacman + AUR helpers: yay / paru / pamac)

All privileged install commands are returned as shell strings suitable
for pkexec / GriffinWorker.  Detection is passive (reads /etc/os-release
and looks for package-manager binaries).

Behaviour is delegated to the distro modules (griffinhub/distro_modules) via
``_modloader`` so this API, the hubs and the Plugins window never disagree.
The older tables below are only used when no distro module is available.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from . import _modloader

# ── Detection ───────────────────────────────────────────────────────────────

_CACHE: dict | None = None


def detect() -> dict:
    """Return a dict describing the host distro and package manager."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE

    info: dict[str, Any] = {
        "id":          "unknown",
        "id_like":     [],
        "name":        "Unknown Linux",
        "version_id":  "",
        "pkg_manager": "unknown",
        "family":      "unknown",   # debian | rhel | suse | arch | unknown
        "aur_helper":  None,        # yay | paru | pamac | None
        "de":          _detect_de(),
        "compositor":  _detect_compositor(),
        "flatpak":     bool(shutil.which("flatpak")),
        "kernel":      _kernel_release(),
    }

    try:
        text = Path("/etc/os-release").read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
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
    except OSError:
        pass

    all_ids = [info["id"]] + info["id_like"]

    _m = _modloader.active_module()
    if _m is not None and _m.pkg_manager != "unknown":
        info["pkg_manager"] = _m.pkg_manager
        info["family"] = _m.family
        info["module"] = _m.id
        if _m.family == "arch":
            info["aur_helper"] = _find_aur_helper()
        _CACHE = info
        return info

    if any(x in all_ids for x in (
        "ubuntu", "debian", "linuxmint", "pop", "elementary",
        "kali", "zorin", "neon", "mx", "devuan",
    )):
        info["pkg_manager"] = "apt"
        info["family"] = "debian"
    elif any(x in all_ids for x in (
        "fedora", "rhel", "centos", "almalinux", "rocky", "nobara",
    )):
        info["pkg_manager"] = "dnf"
        info["family"] = "rhel"
    elif any(x in all_ids for x in ("opensuse", "suse", "sled", "sles")):
        info["pkg_manager"] = "zypper"
        info["family"] = "suse"
    elif any(x in all_ids for x in (
        "arch", "manjaro", "endeavouros", "garuda", "artix", "cachyos",
    )):
        info["pkg_manager"] = "pacman"
        info["family"] = "arch"
        info["aur_helper"] = _find_aur_helper()
    else:
        # Fallback by binary presence
        if shutil.which("apt-get"):
            info["pkg_manager"] = "apt"
            info["family"] = "debian"
        elif shutil.which("dnf"):
            info["pkg_manager"] = "dnf"
            info["family"] = "rhel"
        elif shutil.which("zypper"):
            info["pkg_manager"] = "zypper"
            info["family"] = "suse"
        elif shutil.which("pacman"):
            info["pkg_manager"] = "pacman"
            info["family"] = "arch"
            info["aur_helper"] = _find_aur_helper()

    _CACHE = info
    return info


def invalidate() -> None:
    global _CACHE
    _CACHE = None
    _modloader.invalidate()


def _detect_de() -> str:
    de = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    if de:
        return de
    return os.environ.get("DESKTOP_SESSION", "").lower() or "unknown"


def _detect_compositor() -> str:
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return "unknown"


def _kernel_release() -> str:
    try:
        return Path("/proc/version").read_text().split()[2]
    except Exception:
        try:
            return subprocess.check_output(["uname", "-r"], text=True).strip()
        except Exception:
            return ""


def _find_aur_helper() -> str | None:
    for helper in ("yay", "paru", "pamac"):
        if shutil.which(helper):
            return helper
    return None


# ── Package helpers ─────────────────────────────────────────────────────────

def pkg_installed(pkg: str) -> bool:
    """Return True if *pkg* appears installed (works across apt/dnf/pacman/zypper)."""
    _m = _modloader.active_module()
    if _m is not None and _m.pkg_manager != "unknown":
        return _m.is_installed(pkg)
    family = detect()["family"]
    if family == "debian" and shutil.which("dpkg"):
        r = subprocess.run(["dpkg", "-s", pkg], capture_output=True, text=True, timeout=10)
        return r.returncode == 0 and "Status: install ok installed" in r.stdout
    if family == "rhel" and shutil.which("rpm"):
        r = subprocess.run(["rpm", "-q", pkg], capture_output=True, timeout=10)
        return r.returncode == 0
    if family == "arch" and shutil.which("pacman"):
        r = subprocess.run(["pacman", "-Q", pkg], capture_output=True, timeout=10)
        return r.returncode == 0
    if family == "suse" and shutil.which("rpm"):
        r = subprocess.run(["rpm", "-q", pkg], capture_output=True, timeout=10)
        return r.returncode == 0
    # Generic fallback
    for cmd in (
        ["dpkg", "-s", pkg],
        ["rpm", "-q", pkg],
        ["pacman", "-Q", pkg],
    ):
        if shutil.which(cmd[0]):
            r = subprocess.run(cmd, capture_output=True, timeout=10)
            if r.returncode == 0:
                return True
    return False


def headers_package() -> str:
    """Return the correct kernel-headers package name for the running kernel."""
    family = detect()["family"]
    ker = detect()["kernel"]
    if family == "debian":
        # Prefer the matching headers package if possible
        if ker:
            specific = f"linux-headers-{ker}"
            # Don't claim it exists — just return the generic that pulls the right one
            return "linux-headers-generic"
        return "linux-headers-generic"
    if family == "rhel":
        return "kernel-devel"
    if family == "suse":
        return "kernel-devel"
    if family == "arch":
        # Arch uses linux-headers, linux-lts-headers, linux-zen-headers, etc.
        if "lts" in ker:
            return "linux-lts-headers"
        if "zen" in ker:
            return "linux-zen-headers"
        if "hardened" in ker:
            return "linux-hardened-headers"
        return "linux-headers"
    return "linux-headers"


def build_deps() -> list[str]:
    """Packages needed to build out-of-tree DKMS modules."""
    family = detect()["family"]
    common = ["git", "dkms"]
    if family == "debian":
        return common + [headers_package(), "build-essential"]
    if family == "rhel":
        return common + [headers_package(), "make", "gcc"]
    if family == "suse":
        return common + [headers_package(), "make", "gcc"]
    if family == "arch":
        return common + [headers_package(), "base-devel"]
    return common + [headers_package()]


def install_cmd(packages: list[str], *, update: bool = False) -> str:
    """
    Return a shell command that installs *packages* with the host package manager.
    Ready to run under pkexec.
    """
    _m = _modloader.active_module()
    if _m is not None and _m.pkg_manager != "unknown":
        return _m.install_cmd(list(packages))
    family = detect()["family"]
    pkgs = " ".join(packages)
    if family == "debian":
        prefix = "apt-get update -qq && " if update else ""
        return f"{prefix}apt-get install -y {pkgs}"
    if family == "rhel":
        return f"dnf install -y {pkgs}"
    if family == "suse":
        return f"zypper --non-interactive install {pkgs}"
    if family == "arch":
        return f"pacman -S --noconfirm --needed {pkgs}"
    # Last-resort
    if shutil.which("apt-get"):
        return f"apt-get install -y {pkgs}"
    if shutil.which("dnf"):
        return f"dnf install -y {pkgs}"
    if shutil.which("pacman"):
        return f"pacman -S --noconfirm --needed {pkgs}"
    return f"echo 'No supported package manager found' && false"


def remove_cmd(packages: list[str]) -> str:
    _m = _modloader.active_module()
    if _m is not None and _m.pkg_manager != "unknown":
        return _m.remove_cmd(list(packages))
    family = detect()["family"]
    pkgs = " ".join(packages)
    if family == "debian":
        return f"apt-get remove -y {pkgs}"
    if family == "rhel":
        return f"dnf remove -y {pkgs}"
    if family == "suse":
        return f"zypper --non-interactive remove {pkgs}"
    if family == "arch":
        return f"pacman -R --noconfirm {pkgs}"
    return f"echo 'No supported package manager found' && false"


def aur_install_cmd(packages: list[str]) -> str | None:
    """
    Return a command that installs AUR packages via yay/paru/pamac.
    Returns None if no AUR helper is available (caller should fall back
    to a git-clone + makepkg path or show a message).
    """
    # Note: AUR helpers refuse to run as root, and driver installs run as root.
    # The Arch distro module builds AUR packages itself (as the desktop user)
    # and is what controllers.py / the hubs use; this stays for callers that
    # run as a normal user.
    helper = detect().get("aur_helper")
    if not helper:
        return None
    pkgs = " ".join(packages)
    if helper in ("yay", "paru"):
        # --noconfirm --needed; run as the real user when possible
        return f"{helper} -S --noconfirm --needed {pkgs}"
    if helper == "pamac":
        return f"pamac install --no-confirm {pkgs}"
    return None


def ensure_build_deps_cmd() -> str:
    """Shell command that installs git, dkms, headers (for the running kernel) and build tools."""
    if _modloader.active_module() is not None:
        return _modloader.feature_script("build-deps")
    return install_cmd(build_deps(), update=True)


# ── NVIDIA / AMD / Intel package maps ───────────────────────────────────────

# Maps logical driver IDs → package names per family
_NVIDIA_PKGS = {
    # R580 — current recommended stable (Maxwell → Blackwell)
    "nvidia-580": {
        "debian": ["nvidia-driver-580"],
        "rhel":   ["akmod-nvidia"],          # RPM Fusion tracks latest proprietary
        "suse":   ["nvidia-driver-G06"],
        "arch":   ["nvidia", "nvidia-utils"],
    },
    "nvidia-570": {
        "debian": ["nvidia-driver-570"],
        "rhel":   ["akmod-nvidia"],
        "suse":   ["nvidia-driver-G06"],
        "arch":   ["nvidia-dkms", "nvidia-utils"],
    },
    "nvidia-550": {
        "debian": ["nvidia-driver-550"],
        "rhel":   ["akmod-nvidia"],
        "suse":   ["nvidia-driver-G06"],
        "arch":   ["nvidia-dkms", "nvidia-utils"],
    },
    "nvidia-open": {
        "debian": ["nvidia-open"],
        "rhel":   ["akmod-nvidia"],
        "suse":   ["nvidia-open-driver-G06-signed-kmp-default"],
        "arch":   ["nvidia-open", "nvidia-utils"],
    },
}

_AMD_PKGS = {
    "mesa-vulkan": {
        "debian": ["mesa-vulkan-drivers"],
        "rhel":   ["mesa-vulkan-drivers"],
        "suse":   ["libvulkan_radeon"],
        "arch":   ["vulkan-radeon", "lib32-vulkan-radeon"],
    },
    "mesa-va": {
        "debian": ["mesa-va-drivers"],
        "rhel":   ["mesa-va-drivers"],
        "suse":   ["libva-mesa-driver"],
        "arch":   ["libva-mesa-driver"],
    },
}

_INTEL_PKGS = {
    "media-va": {
        "debian": ["intel-media-va-driver"],
        "rhel":   ["intel-media-driver"],
        "suse":   ["intel-media-driver"],
        "arch":   ["intel-media-driver"],
    },
}


def driver_packages(logical_id: str) -> list[str]:
    """Resolve a logical driver id to the concrete package list for this distro."""
    family = detect()["family"]
    for table in (_NVIDIA_PKGS, _AMD_PKGS, _INTEL_PKGS):
        if logical_id in table:
            return table[logical_id].get(family, table[logical_id].get("debian", []))
    return []


_LOGICAL_TO_FEATURE = {
    "nvidia-580": "nvidia-580", "nvidia-570": "nvidia-570", "nvidia-550": "nvidia-550",
    "nvidia-open": "nvidia-open", "mesa-vulkan": "amd-vulkan", "mesa-va": "amd-vaapi",
    "media-va": "intel-vaapi",
}


def driver_install_cmd(logical_id: str) -> str:
    feat = _LOGICAL_TO_FEATURE.get(logical_id)
    if feat and _modloader.active_module() is not None:
        return _modloader.feature_script(feat)
    pkgs = driver_packages(logical_id)
    if not pkgs:
        return "echo 'No packages mapped for this driver on this distro' && false"
    return install_cmd(pkgs, update=True)
