"""
Griffin Hub distro module: Debian / Ubuntu family (apt)

Covers Debian, Ubuntu and derivatives (Linux Mint, Pop!_OS, elementary, Zorin,
KDE neon, Kali, MX, Raspberry Pi OS, Devuan ...).  Ubuntu-style package names
(nvidia-driver-NNN, linux-generic-hwe-XX.XX, bcmwl-kernel-source) are used when
the system is Ubuntu-based; pure Debian uses Debian's own names
(nvidia-driver, broadcom-sta-dkms, firmware-* ...).
"""

import shlex

from gh_distro_api import DistroModule, Resolved, run_quiet, sh


class Module(DistroModule):
    id = "debian"
    name = "Debian / Ubuntu (apt)"
    version = "1.0.0"
    description = "apt, DKMS headers matched to the running kernel, Ubuntu and Debian driver package names."
    pkg_manager = "apt"
    pkg_install_argv = ["apt", "install", "-y"]
    family = "debian"
    ids = ("debian", "ubuntu", "linuxmint", "pop", "elementary", "zorin", "neon", "kali",
           "raspbian", "devuan", "mx", "pureos", "lmde", "tuxedo", "parrot", "deepin")
    exact_ids = ("debian", "ubuntu", "linuxmint", "pop")
    base_setup_packages = ("gamemode", "gamescope", "mangohud", "python3-psutil", "python3-yaml")

    # ── helpers ─────────────────────────────────────────────────────────────
    @property
    def ubuntu_like(self) -> bool:
        return "ubuntu" in self.ids_all or "pop" in self.ids_all or "linuxmint" == self.osr.get("id")

    def is_installed(self, pkg: str) -> bool:
        rc, out = run_quiet(["dpkg-query", "-W", "-f=${Status}", pkg])
        return rc == 0 and "install ok installed" in out

    def pm_functions(self) -> str:
        return r'''
export DEBIAN_FRONTEND=noninteractive
gh_avail()   { apt-cache policy "$1" 2>/dev/null | grep -q 'Candidate: [^(]'; }
gh_install() { apt-get install -y "$@"; }
gh_remove()  { apt-get remove -y "$@"; }
gh_purge()   { apt-get remove -y --purge "$@"; }
'''

    def refresh_lines(self) -> str:
        return "apt-get update -qq || echo 'Warning: apt-get update failed; using cached package lists.'"

    def build_deps_lines(self) -> str:
        return (
            "gh_install git dkms build-essential\n"
            'gh_pick "linux-headers-$(uname -r)" "linux-headers-generic" '
            '"linux-headers-$(dpkg --print-architecture)"'
        )

    def build_deps(self) -> str:
        return sh(self.prelude(), self.refresh_lines(), self.build_deps_lines())

    def _need_nonfree(self) -> str:
        return (
            'if ! gh_avail nvidia-driver; then\n'
            '  echo "ERROR: nvidia-driver is not available. Enable the contrib, non-free and non-free-firmware"\n'
            '  echo "       components in /etc/apt/sources.list (or /etc/apt/sources.list.d/*), then run apt update."\n'
            '  exit 1\nfi'
        )

    # ── NVIDIA ──────────────────────────────────────────────────────────────
    def _nv_branch(self, n: int) -> Resolved:
        pkg = f"nvidia-driver-{n}"
        return Resolved(
            label=f"{pkg}" + (" (Recommended)" if n == 580 else ""),
            install=self.script(self.refresh_lines(), f'gh_pick "{pkg}"', "gh_sb_note"),
            remove=self.script(f"gh_purge {pkg}", "apt-get autoremove -y || true"),
            check_pkgs=[pkg])

    def _nv_debian(self) -> Resolved:
        return Resolved(
            label="nvidia-driver (Debian's packaged branch)",
            install=self.script(self.refresh_lines(), self.build_deps_lines(), self._need_nonfree(),
                                'gh_pick "nvidia-driver"', "gh_sb_note"),
            remove=self.script("gh_purge nvidia-driver nvidia-kernel-dkms", "apt-get autoremove -y || true"),
            check_pkgs=["nvidia-driver"],
            note="Debian ships one NVIDIA branch; it needs the contrib/non-free/non-free-firmware components.")

    def feat_nvidia_580(self):
        return self._nv_branch(580) if self.ubuntu_like else self._nv_debian()

    def feat_nvidia_570(self):
        return self._nv_branch(570) if self.ubuntu_like else self.unsupported(
            "nvidia-570", "Debian packages a single NVIDIA branch - use the first option (nvidia-driver).")

    def feat_nvidia_550(self):
        return self._nv_branch(550) if self.ubuntu_like else self.unsupported(
            "nvidia-550", "Debian packages a single NVIDIA branch - use the first option (nvidia-driver).")

    def feat_nvidia_open(self):
        if not self.ubuntu_like:
            return self.unsupported("nvidia-open",
                "Debian's repositories do not provide a separate open-module driver package "
                "that Griffin Hub can verify - use the first option (nvidia-driver).")
        sets = ["nvidia-driver-580-open", "nvidia-driver-570-open", "nvidia-driver-550-open"]
        return Resolved(
            label="nvidia-driver-NNN-open (open kernel module)",
            install=self.script(self.refresh_lines(),
                                "gh_pick " + " ".join(f'"{s}"' for s in sets), "gh_sb_note"),
            remove=self.script("gh_purge " + " ".join(sets), "apt-get autoremove -y || true"),
            check_any=[[s] for s in sets])

    def feat_nvidia_cuda(self):
        return Resolved(install=self.script(self.refresh_lines(), 'gh_pick "nvidia-cuda-toolkit"'),
                        remove=self.script("gh_remove nvidia-cuda-toolkit"),
                        check_pkgs=["nvidia-cuda-toolkit"], reboot=False)

    def feat_nvidia_nvenc(self):
        if self.ubuntu_like:
            inst = self.script(self.refresh_lines(), r'''
B=$(dpkg-query -W -f='${Package} ${Status}\n' 'nvidia-driver-[0-9]*' 2>/dev/null | awk '/install ok installed/{print $1}' | grep -o '[0-9][0-9]*' | head -1)
[ -n "$B" ] || B=580
gh_install "libnvidia-encode-$B" "libnvidia-decode-$B"''')
            return Resolved(install=inst, check_cmd="dpkg -l 'libnvidia-encode-*' 2>/dev/null | grep -q '^ii'",
                            remove=self.script(r"gh_remove $(dpkg -l 'libnvidia-encode-*' 'libnvidia-decode-*' | awk '/^ii/{print $2}')"),
                            reboot=False)
        return Resolved(install=self.script(self.refresh_lines(), 'gh_pick "libnvidia-encode1 libnvcuvid1" "libnvidia-encode1"'),
                        check_pkgs=["libnvidia-encode1"], remove=self.script("gh_remove libnvidia-encode1 libnvcuvid1"),
                        reboot=False)

    def feat_nvidia_settings(self):
        return Resolved(install=self.script(self.refresh_lines(), 'gh_pick "nvidia-settings"'),
                        remove=self.script("gh_remove nvidia-settings"),
                        check_pkgs=["nvidia-settings"], reboot=False)

    def feat_nouveau_swap(self):
        return Resolved(install=self.script(self.refresh_lines(), r'''
P=$(dpkg-query -W -f='${Package} ${Status}\n' 'nvidia-*' 'libnvidia-*' 'xserver-xorg-video-nvidia*' 2>/dev/null | awk '/install ok installed/{print $1}')
[ -z "$P" ] || gh_purge $P
apt-get autoremove -y || true
gh_install xserver-xorg-video-nouveau mesa-utils
gh_initramfs'''))

    # ── AMD / Intel ─────────────────────────────────────────────────────────
    def feat_amd_ddx(self):
        return self._simple(["xserver-xorg-video-amdgpu"])

    def feat_amd_rocm(self):
        return Resolved(
            install=self.script(self.refresh_lines(), 'gh_pick "rocm" "rocminfo rocm-smi hipcc libamdhip64-5"'),
            remove=self.script("gh_remove rocm rocminfo rocm-smi hipcc libamdhip64-5 2>/dev/null || true"),
            check_any=[["rocm"], ["rocminfo", "rocm-smi"]], reboot=False,
            note="Distro-packaged ROCm runtime. AMD's own repository offers newer releases.")

    def feat_amd_vulkan(self):
        return self._simple(["mesa-vulkan-drivers"], reboot=False)

    def feat_amd_vaapi(self):
        return self._simple(["mesa-va-drivers"], reboot=False)

    def feat_intel_vaapi(self):
        sets = ["intel-media-va-driver-non-free", "intel-media-va-driver"] if not self.ubuntu_like else ["intel-media-va-driver"]
        return Resolved(install=self.script(self.refresh_lines(), "gh_pick " + " ".join(f'"{s}"' for s in sets)),
                        remove=self.script("gh_remove " + " ".join(sets)),
                        check_any=[[s] for s in sets], reboot=False)

    def feat_intel_ddx(self):
        return self._simple(["xserver-xorg-video-intel"])

    def _simple(self, pkgs, reboot=True) -> Resolved:
        return Resolved(install=self.script(self.refresh_lines(), "gh_pick " + shlex.quote(" ".join(pkgs))),
                        remove=self.script("gh_remove " + " ".join(pkgs)),
                        check_pkgs=list(pkgs), reboot=reboot)

    # ── controllers ─────────────────────────────────────────────────────────
    def feat_joystick(self):
        return Resolved(install=self.script(self.refresh_lines(), 'gh_pick "joystick" "linuxconsoletools"'),
                        remove=self.script("gh_remove joystick linuxconsoletools 2>/dev/null || true"),
                        check_any=[["joystick"], ["linuxconsoletools"]], reboot=False)

    def feat_xone(self):
        return Resolved(
            install=self.script(self.refresh_lines(), self.build_deps_lines(),
                                self.dkms_git("xone", "https://github.com/medusalix/xone",
                                              "bash install.sh --release\n"
                                              "command -v xone-get-firmware.sh >/dev/null && xone-get-firmware.sh --skip-disclaimer || true"),
                                "gh_sb_note"),
            check_cmd="dkms status 2>/dev/null | grep -qi '^xone[,/]'",
            note="Built from upstream with DKMS (rebuilds automatically on kernel updates).")

    def feat_xpadneo(self):
        return Resolved(
            install=self.script(self.refresh_lines(), self.build_deps_lines(),
                                self.dkms_git("xpadneo", "https://github.com/atar-axis/xpadneo", "bash install.sh"),
                                "gh_sb_note"),
            check_cmd="dkms status 2>/dev/null | grep -qi '^xpadneo[,/]'")

    # ── WiFi ────────────────────────────────────────────────────────────────
    def feat_wifi_r8168(self):
        return Resolved(install=self.script(self.refresh_lines(), self.build_deps_lines(), 'gh_pick "r8168-dkms"'),
                        remove=self.script("gh_purge r8168-dkms"), check_pkgs=["r8168-dkms"])

    def feat_wifi_broadcom_wl(self):
        sets = ["bcmwl-kernel-source", "broadcom-sta-dkms"] if self.ubuntu_like else ["broadcom-sta-dkms"]
        return Resolved(
            install=self.script(self.refresh_lines(), self.build_deps_lines(),
                                "gh_pick " + " ".join(f'"{s}"' for s in sets), "gh_sb_note"),
            remove=self.script("gh_purge " + " ".join(sets)), check_any=[[s] for s in sets],
            note="" if self.ubuntu_like else "Debian: broadcom-sta-dkms needs the non-free component.")

    def feat_wifi_b43(self):
        return Resolved(install=self.script(self.refresh_lines(), 'gh_pick "firmware-b43-installer"'),
                        remove=self.script("gh_purge firmware-b43-installer"),
                        check_pkgs=["firmware-b43-installer"])

    def feat_wifi_brcm_firmware(self):
        return Resolved(install=self.script(self.refresh_lines(), 'gh_pick "firmware-brcm80211" "linux-firmware"'),
                        check_any=[["firmware-brcm80211"], ["linux-firmware"]], reboot=True)

    def feat_wifi_kernel_upgrade(self):
        if self.osr.get("id") == "ubuntu":
            v = self.osr.get("version_id", "")
            pkg = f"linux-generic-hwe-{v}"
            return Resolved(install=self.script(self.refresh_lines(),
                                                f'gh_avail {pkg} || {{ echo "No HWE kernel for Ubuntu {v}"; exit 1; }}',
                                                f"apt-get install -y --install-recommends {pkg}"),
                            check_pkgs=[pkg], label=f"{pkg} (newer kernel)")
        if self.osr.get("id") == "debian":
            return Resolved(install=self.script(self.refresh_lines(), r'''
CN=$(. /etc/os-release; echo "$VERSION_CODENAME")
A=$(dpkg --print-architecture)
if grep -rqs "$CN-backports" /etc/apt/sources.list /etc/apt/sources.list.d 2>/dev/null; then
  apt-get install -y -t "$CN-backports" "linux-image-$A" "linux-headers-$A"
else
  echo "ERROR: enable the $CN-backports repository first, then run this again."
  exit 1
fi'''))
        return self.unsupported("wifi-kernel-upgrade",
                                "Update your kernel through your distribution's update manager, then reboot.")

    # ── shared ──────────────────────────────────────────────────────────────
    def feat_build_deps(self):
        return Resolved(install=self.build_deps(), reboot=False)

    def feat_firmware(self):
        if self.ubuntu_like:
            return Resolved(install=self.script(self.refresh_lines(), "gh_install linux-firmware"),
                            check_pkgs=["linux-firmware"])
        return Resolved(install=self.script(self.refresh_lines(),
                        'gh_pick "firmware-linux firmware-iwlwifi firmware-realtek firmware-atheros firmware-brcm80211" '
                        '"firmware-linux firmware-iwlwifi firmware-realtek" "firmware-linux-free"'),
                        check_any=[["firmware-linux"], ["firmware-linux-free"]],
                        note="Needs the non-free-firmware component in your apt sources.")

    def feat_gaming(self):
        return Resolved(install=self.install_available_cmd(["gamemode", "mangohud", "gamescope"]),
                        check_pkgs=["gamemode"], reboot=False)
