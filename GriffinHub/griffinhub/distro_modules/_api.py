"""
Griffin Hub distro module API (v1)
==================================

A *distro module* teaches Griffin Hub how to do everything it does on one
family of Linux distributions: install / remove / check packages, build DKMS
drivers, regenerate the initramfs, pick the right GPU / controller / WiFi
driver packages and so on.  The hubs never mention apt, pacman, dnf or zypper
directly any more; they ask the active module for a *feature* (for example
"nvidia-580" or "wifi-broadcom-wl") and get back a ready-to-run, native
install script plus the information needed to show its state.

Modules live in ``distro_modules/`` next to the app (or in
``~/.local/share/griffinhub/distro_modules`` for user modules) and appear in
the Plugins window, where they can be switched on/off.  The module that best
matches /etc/os-release is picked automatically.

A module file defines one subclass of :class:`DistroModule` named ``Module``.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

API_VERSION = 1

# Every feature key the hubs may ask for.  A module must answer every key,
# either with an install recipe or with ``unsupported`` + a human reason.
FEATURE_KEYS = (
    # GPU hub - NVIDIA
    "nvidia-580", "nvidia-570", "nvidia-550", "nvidia-open",
    "nvidia-cuda", "nvidia-nvenc", "nvidia-settings", "nouveau-swap",
    # GPU hub - AMD / Intel
    "amd-ddx", "amd-rocm", "amd-vulkan", "amd-vaapi",
    "intel-vaapi", "intel-ddx",
    # Controller hub
    "joystick", "xone", "xpadneo",
    # WiFi hub
    "wifi-r8168", "wifi-broadcom-wl", "wifi-b43", "wifi-brcm-firmware",
    "wifi-kernel-upgrade",
    # generic DKMS drivers (neutral recipes on top of each module's native build deps)
    "wifi-rtl8821ce", "wifi-rtl8822be", "wifi-rtl8723de",
    "xpad-noone", "xpad-paroj", "hid-tmff2",
    # shared
    "build-deps", "firmware", "gaming", "game-deps",
)


@dataclass
class Resolved:
    """What a module answers for one feature on the running system."""
    ok: bool = True
    label: str = ""          # optional UI title override ("akmod-nvidia (RPM Fusion)")
    install: str = ""        # root shell script
    remove: str = ""         # root shell script (empty = not removable from the UI)
    check_pkgs: list | None = None    # installed when ALL are installed
    check_any: list | None = None     # installed when ANY of these package lists is fully installed
    check_cmd: str = ""      # shell test, exit 0 = installed (used instead of packages)
    note: str = ""           # shown in the UI / appended to the output
    reason: str = ""         # why it is unsupported (when ok is False)
    reboot: bool = True      # a reboot is recommended afterwards


def parse_os_release(text: str | None = None) -> dict:
    """Return {id, id_like[list], version_id, name, variant_id} from /etc/os-release."""
    if text is None:
        for p in ("/etc/os-release", "/usr/lib/os-release"):
            try:
                text = Path(p).read_text()
                break
            except OSError:
                continue
        else:
            text = ""
    info = {"id": "", "id_like": [], "version_id": "", "name": "", "variant_id": ""}
    for line in text.splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        k, _, v = line.partition("=")
        v = v.strip().strip('"').strip("'")
        k = k.strip()
        if k == "ID":
            info["id"] = v.lower()
        elif k == "ID_LIKE":
            info["id_like"] = v.lower().split()
        elif k == "VERSION_ID":
            info["version_id"] = v
        elif k == "NAME":
            info["name"] = v
        elif k == "VARIANT_ID":
            info["variant_id"] = v.lower()
    return info


def sh(*lines: str) -> str:
    """Join script lines into one bash script (stops on the first error)."""
    return "set -e\n" + "\n".join(l for l in lines if l is not None and l != "")


class DistroModule:
    """Base class.  Subclasses fill in the class attributes and FEATURES."""

    # ── identity ────────────────────────────────────────────────────────────
    id = "base"
    name = "Base"
    version = "1.0.0"
    description = ""
    pkg_manager = "unknown"        # apt | pacman | dnf | zypper
    pkg_install_argv: list = []    # for DistroInfo / Setup tab (root argv prefix)
    family = "unknown"             # debian | arch | rhel | suse
    ids: tuple = ()                # os-release IDs / ID_LIKE values this module claims
    exact_ids: tuple = ()          # IDs that get the highest score
    required_tool = ""             # executable that must exist for the module to be usable
    base_setup_packages: tuple = ("gamemode", "mangohud")

    def __init__(self, osr: dict | None = None):
        self.osr = osr if osr is not None else parse_os_release()

    @property
    def ids_all(self) -> list:
        return [self.osr.get("id", "")] + list(self.osr.get("id_like", []))

    # ── matching ────────────────────────────────────────────────────────────
    @classmethod
    def match_score(cls, osr: dict) -> int:
        """0 = does not match; higher = better match."""
        ids = [osr.get("id", "")] + list(osr.get("id_like", []))
        if osr.get("id") in cls.exact_ids:
            return 100
        if osr.get("id") in cls.ids:
            return 90
        if any(i in cls.ids for i in ids[1:]):
            return 60
        return 0

    # ── package state (runs in-process, call from a worker thread) ──────────
    def is_installed(self, pkg: str) -> bool:
        raise NotImplementedError

    def all_installed(self, pkgs) -> bool:
        return bool(pkgs) and all(self.is_installed(p) for p in pkgs)

    # ── shell prelude: defines gh_avail / gh_install / gh_remove / gh_pick ──
    def pm_functions(self) -> str:
        raise NotImplementedError

    def prelude(self) -> str:
        """Functions every script can use.  Distro independent helpers live here."""
        return self.pm_functions() + r'''
gh_pick() {
  # install the first package set (arg = space separated names) that is fully available
  local set ok p
  for set in "$@"; do
    ok=1
    for p in $set; do gh_avail "$p" || { ok=0; break; }; done
    if [ "$ok" = 1 ]; then echo "Installing: $set"; gh_install $set; return $?; fi
  done
  echo "ERROR: none of these package sets is available in your enabled repositories: $*"
  return 1
}
gh_initramfs() {
  if command -v update-initramfs >/dev/null 2>&1; then update-initramfs -u
  elif command -v mkinitcpio >/dev/null 2>&1; then mkinitcpio -P
  elif command -v dracut >/dev/null 2>&1; then dracut --force --regenerate-all
  elif command -v mkinitrd >/dev/null 2>&1; then mkinitrd
  else echo "No initramfs tool found - skipping"; fi
}
gh_secure_boot() { command -v mokutil >/dev/null 2>&1 && mokutil --sb-state 2>/dev/null | grep -qi 'SecureBoot enabled'; }
gh_sb_note() {
  if gh_secure_boot; then
    echo "NOTE: Secure Boot is enabled. Out-of-tree kernel modules must be signed or the module will not load."
    echo "      Enroll a Machine Owner Key (MOK) or disable Secure Boot, then reboot."
  fi
}
'''

    # ── building blocks used by feature recipes ─────────────────────────────
    def build_deps_lines(self) -> str:
        """Shell lines installing git + dkms + toolchain + headers for the running kernel."""
        raise NotImplementedError

    def build_deps(self) -> str:
        """Complete script: prelude + dependency install."""
        return sh(self.prelude(), self.build_deps_lines())

    def script(self, *lines: str) -> str:
        """prelude + lines as one root script."""
        return sh(self.prelude(), *lines)

    def remove_cmd(self, pkgs) -> str:
        return self.script("gh_remove " + " ".join(shlex.quote(p) for p in pkgs))

    def install_cmd(self, pkgs) -> str:
        return self.script(self.refresh_lines(), "gh_install " + " ".join(shlex.quote(p) for p in pkgs))

    def refresh_lines(self) -> str:
        return ""

    def install_available_cmd(self, pkgs) -> str:
        """Install every package that exists in the enabled repos; list the rest as skipped."""
        names = " ".join(shlex.quote(p) for p in pkgs)
        return self.script(self.refresh_lines(), (
            'ok=""; skipped=""\n'
            f'for p in {names}; do if gh_avail "$p"; then ok="$ok $p"; else skipped="$skipped $p"; fi; done\n'
            '[ -n "$skipped" ] && echo "Skipped (not available on this system):$skipped"\n'
            '[ -n "$ok" ] || { echo "Nothing to install."; exit 1; }\n'
            'gh_install $ok'))

    def install_any_cmd(self, *sets: str) -> str:
        return self.script(self.refresh_lines(), "gh_pick " + " ".join(shlex.quote(s) for s in sets))

    def dkms_git(self, name: str, url: str, build: str, ver: str = "") -> str:
        """Distro neutral: fetch an out-of-tree driver with git and build it with DKMS."""
        d = "/usr/local/src/griffinhub-drivers/" + name
        return (
            "mkdir -p /usr/local/src/griffinhub-drivers\n"
            f"if [ -d {d}/.git ]; then git -C {d} pull --ff-only || true; "
            f"else rm -rf {d}; git clone --depth 1 --recurse-submodules {url} {d}; fi\n"
            f"cd {d}\n{build}"
        )

    def dkms_repo(self, name: str, url: str, default_name: str = "", default_ver: str = "") -> str:
        """Clone `url` and install it with DKMS using the name/version from its dkms.conf."""
        body = (
            r"""N=$(sed -n 's/^PACKAGE_NAME="\{0,1\}\([^"]*\)"\{0,1\}.*/\1/p' dkms.conf 2>/dev/null | head -1)""" "\n"
            r"""V=$(sed -n 's/^PACKAGE_VERSION="\{0,1\}\([^"]*\)"\{0,1\}.*/\1/p' dkms.conf 2>/dev/null | head -1)""" "\n"
            f'[ -n "$N" ] || N={shlex.quote(default_name)}\n'
            f'[ -n "$V" ] || V={shlex.quote(default_ver)}\n'
            '[ -n "$N" ] && [ -n "$V" ] || { echo "ERROR: could not read the DKMS name/version from dkms.conf"; exit 1; }\n'
            'dkms remove -m "$N" -v "$V" --all >/dev/null 2>&1 || true\n'
            'rm -rf "/usr/src/$N-$V"; cp -r . "/usr/src/$N-$V"\n'
            'dkms add -m "$N" -v "$V"\n'
            'dkms build -m "$N" -v "$V"\n'
            'dkms install -m "$N" -v "$V"'
        )
        return self.dkms_git(name, url, body)

    def _dkms_feature(self, name, url, check, dn="", dv="", extra=""):
        return Resolved(
            install=self.script(self.refresh_lines(), self.build_deps_lines(),
                                self.dkms_repo(name, url, dn, dv), extra, "gh_sb_note"),
            check_cmd=f"dkms status 2>/dev/null | grep -qi '{check}'",
            note="Built with DKMS - rebuilds automatically on kernel updates.")

    def feat_wifi_rtl8821ce(self):
        return self._dkms_feature("rtl8821ce", "https://github.com/tomaspinho/rtl8821ce", "^rtl8821ce")

    def feat_wifi_rtl8822be(self):
        return self._dkms_feature("rtl8822be", "https://github.com/nickhardiman/rtl8822be", "^8822be")

    def feat_wifi_rtl8723de(self):
        return self._dkms_feature("rtl8723de", "https://github.com/smlinux/rtl8723de", "^rtl8723de")

    def feat_xpad_noone(self):
        return self._dkms_feature("xpad-noone", "https://github.com/medusalix/xpad-noone",
                                  "^xpad-noone[,/]", "xpad-noone", "1.0")

    def feat_xpad_paroj(self):
        return self._dkms_feature("xpad", "https://github.com/paroj/xpad", "^xpad,", "xpad", "0.4")

    def feat_hid_tmff2(self):
        return Resolved(
            install=self.script(self.refresh_lines(), self.build_deps_lines(),
                                self.dkms_git("hid-tmff2", "https://github.com/Kimplul/hid-tmff2",
                                              "bash dkms/dkms-install.sh\nmake udev-rules"),
                                "gh_sb_note"),
            check_cmd="dkms status 2>/dev/null | grep -qi '^hid-tmff2[,/]'",
            note="Needs Thrustmaster firmware v31 or newer.")

    def feat_game_deps(self):
        """winetricks + cabextract for Griffin Game Tweaks (installs what the distro offers)."""
        return Resolved(install=self.install_available_cmd(["winetricks", "cabextract"]),
                        check_pkgs=["winetricks", "cabextract"], reboot=False,
                        note="Game Tweaks also downloads its own checksum-verified winetricks when needed.")

    # ── features ────────────────────────────────────────────────────────────
    def unsupported(self, key: str, reason: str = "") -> Resolved:
        return Resolved(ok=False, reason=reason or f"Not available on {self.name}.")

    def resolve(self, key: str) -> Resolved:
        fn = getattr(self, "feat_" + key.replace("-", "_"), None)
        if fn is None:
            return self.unsupported(key)
        return fn()

    def feature_keys(self):
        return FEATURE_KEYS

    # ── Setup tab / misc ────────────────────────────────────────────────────
    def setup_packages(self) -> list:
        return list(self.base_setup_packages)

    def setup_install_argv(self, packages) -> list:
        return list(self.pkg_install_argv) + list(packages)

    def power_daemons(self) -> list:
        """Names of active services that may override the CPU governor."""
        found = []
        for unit, label in (("power-profiles-daemon", "power-profiles-daemon"),
                            ("tuned", "tuned"), ("tuned-ppd", "tuned-ppd"),
                            ("tlp", "TLP"), ("auto-cpufreq", "auto-cpufreq"),
                            ("cpupower", "cpupower")):
            try:
                r = subprocess.run(["systemctl", "is-active", unit], capture_output=True,
                                   text=True, timeout=3)
                if r.stdout.strip() == "active":
                    found.append(label)
            except Exception:
                pass
        return found

    def install_failed_hint(self) -> str:
        return "Check your internet connection and package repositories."

    def info(self) -> dict:
        return {"id": self.id, "name": self.name, "version": self.version,
                "pkg_manager": self.pkg_manager, "family": self.family,
                "description": self.description}


def run_quiet(argv, timeout=8) -> tuple[int, str]:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:  # noqa: BLE001
        return 127, str(e)
