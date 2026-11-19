"""
Griffin Hub distro module: openSUSE / SUSE family (zypper)

Tumbleweed, Slowroll, Leap, SLE.

* NVIDIA comes from NVIDIA's own openSUSE repository (the one documented in
  SDB:NVIDIA drivers); the module adds it for the running release and installs
  the G06 packages.  The signed open-driver meta package works with Secure Boot.
* Kernel headers use the running kernel flavour (kernel-default-devel,
  kernel-longterm-devel ...).
* MicroOS / Aeon / Kalpa (read-only /usr) are detected and pointed at
  transactional-update instead of failing half way.
"""

import os

from gh_distro_api import DistroModule, Resolved, run_quiet, sh


class Module(DistroModule):
    id = "suse"
    name = "openSUSE / SUSE (zypper)"
    version = "1.0.0"
    description = "zypper, NVIDIA repository setup, kernel flavour aware headers, MicroOS detection."
    pkg_manager = "zypper"
    pkg_install_argv = ["zypper", "--non-interactive", "install"]
    family = "suse"
    ids = ("opensuse", "opensuse-leap", "opensuse-tumbleweed", "opensuse-slowroll", "suse", "sles", "sled",
           "microos", "aeon", "kalpa", "sle-micro")
    exact_ids = ("opensuse-tumbleweed", "opensuse-leap", "opensuse-slowroll", "sles", "sled")
    base_setup_packages = ("gamemode", "gamescope", "mangohud", "python3-psutil", "python3-PyYAML")

    @classmethod
    def match_score(cls, osr):
        i = osr.get("id", "")
        if i.startswith("opensuse") or i in cls.exact_ids:
            return 100
        if i in cls.ids:
            return 90
        if any(x in ("suse", "opensuse") for x in osr.get("id_like", [])):
            return 60
        return 0

    @property
    def immutable(self) -> bool:
        return (self.osr.get("variant_id", "") in ("microos", "aeon", "kalpa")
                or self.osr.get("id") in ("microos", "aeon", "kalpa", "sle-micro"))

    def is_installed(self, pkg: str) -> bool:
        return run_quiet(["rpm", "-q", pkg])[0] == 0

    def pm_functions(self) -> str:
        return ('GH_ID="%s"\nGH_VERSION_ID="%s"\n' % (self.osr.get("id", ""), self.osr.get("version_id", ""))) + r'''
gh_avail()   { zypper --non-interactive info "$1" >/dev/null 2>&1; }
gh_install() { zypper --non-interactive install --auto-agree-with-licenses "$@"; }
gh_remove()  { zypper --non-interactive remove "$@"; }
gh_purge()   { zypper --non-interactive remove "$@"; }
gh_writable() {
  if ! [ -w /usr ] || findmnt -no OPTIONS /usr 2>/dev/null | tr ',' '\n' | grep -qx ro; then
    echo "ERROR: /usr is read-only (MicroOS / transactional system). Use transactional-update pkg install ... and reboot."
    return 1
  fi
}
gh_nvidia_repo() {
  zypper --non-interactive lr -u 2>/dev/null | grep -q 'download.nvidia.com' && return 0
  case "$GH_ID" in
    opensuse-tumbleweed|opensuse-slowroll) U=https://download.nvidia.com/opensuse/tumbleweed ;;
    opensuse-leap) U="https://download.nvidia.com/opensuse/leap/$GH_VERSION_ID" ;;
    *) echo "ERROR: Griffin Hub only knows the NVIDIA repository for Tumbleweed, Slowroll and Leap."; return 1 ;;
  esac
  zypper --non-interactive addrepo --refresh "$U" NVIDIA
  zypper --non-interactive --gpg-auto-import-keys refresh NVIDIA
}
'''

    def refresh_lines(self) -> str:
        return "zypper --non-interactive refresh >/dev/null 2>&1 || echo 'Warning: zypper refresh failed; using cached metadata.'"

    def script(self, *lines):
        return super().script("gh_writable", *lines)

    def build_deps_lines(self) -> str:
        return ('gh_install git dkms gcc make kernel-devel\n'
                'F=$(uname -r | sed "s/.*-//")\n'
                'gh_pick "kernel-$F-devel" "kernel-default-devel"')

    # ── NVIDIA ──────────────────────────────────────────────────────────────
    def feat_nvidia_580(self):
        return Resolved(
            label="NVIDIA proprietary driver G06 (NVIDIA repository)",
            install=self.script(self.refresh_lines(), "gh_nvidia_repo",
                                "gh_install nvidia-video-G06 nvidia-gl-G06 nvidia-compute-G06 nvidia-driver-G06-kmp-default",
                                "gh_initramfs", "gh_sb_note"),
            remove=self.script("zypper --non-interactive remove -u nvidia-video-G06 nvidia-gl-G06 nvidia-compute-G06 nvidia-driver-G06-kmp-default || true",
                               "gh_initramfs"),
            check_pkgs=["nvidia-driver-G06-kmp-default"],
            note="Uses NVIDIA's openSUSE repository (SDB:NVIDIA drivers).")

    def feat_nvidia_570(self):
        return self.unsupported("nvidia-570", "NVIDIA's openSUSE repository tracks one driver branch - use the first option.")

    def feat_nvidia_550(self):
        return self.unsupported("nvidia-550", "NVIDIA's openSUSE repository tracks one driver branch - use the first option.")

    def feat_nvidia_open(self):
        return Resolved(
            label="nvidia-open-driver-G06-signed (open kernel module, Secure Boot ready)",
            install=self.script(self.refresh_lines(), "gh_nvidia_repo",
                                "gh_install nvidia-open-driver-G06-signed-kmp-meta nvidia-video-G06 nvidia-gl-G06",
                                "gh_initramfs"),
            remove=self.script("zypper --non-interactive remove -u nvidia-open-driver-G06-signed-kmp-meta || true", "gh_initramfs"),
            check_pkgs=["nvidia-open-driver-G06-signed-kmp-meta"],
            note="The signed kernel module loads with Secure Boot enabled.")

    def feat_nvidia_cuda(self):
        return Resolved(install=self.script("gh_nvidia_repo", "gh_install nvidia-compute-G06"),
                        remove=self.script("gh_remove nvidia-compute-G06"),
                        check_pkgs=["nvidia-compute-G06"], reboot=False,
                        note="Driver-level CUDA libraries. The full toolkit comes from NVIDIA's CUDA repository.")

    def feat_nvidia_nvenc(self):
        return Resolved(install=self.script("gh_nvidia_repo", "gh_install nvidia-video-G06"),
                        check_pkgs=["nvidia-video-G06"], reboot=False)

    def feat_nvidia_settings(self):
        return Resolved(install=self.script("gh_nvidia_repo", 'gh_pick "nvidia-settings"'),
                        remove=self.script("gh_remove nvidia-settings"), check_pkgs=["nvidia-settings"], reboot=False)

    def feat_nouveau_swap(self):
        return Resolved(install=self.script(r'''
P=$(rpm -qa --qf '%{NAME}\n' | grep -E '^(nvidia-|x11-video-nvidia|openSUSE-repos-NVIDIA)' | grep -v '^$' || true)
[ -z "$P" ] || zypper --non-interactive remove -u $P || true
gh_install Mesa-dri-nouveau xf86-video-nouveau
gh_initramfs'''))

    # ── AMD / Intel ─────────────────────────────────────────────────────────
    def feat_amd_ddx(self):
        return Resolved(install=self.script('gh_pick "xf86-video-amdgpu"'),
                        remove=self.script("gh_remove xf86-video-amdgpu"), check_pkgs=["xf86-video-amdgpu"])

    def feat_amd_rocm(self):
        return Resolved(install=self.script('gh_pick "rocm-hip rocm-opencl rocminfo rocm-smi" "rocminfo rocm-smi"'),
                        remove=self.script("gh_remove rocm-hip rocm-opencl rocminfo rocm-smi 2>/dev/null || true"),
                        check_any=[["rocm-hip"], ["rocminfo"]], reboot=False)

    def feat_amd_vulkan(self):
        return Resolved(install=self.script("gh_install libvulkan_radeon", "gh_avail libvulkan_radeon-32bit && gh_install libvulkan_radeon-32bit || true"),
                        remove=self.script("gh_remove libvulkan_radeon libvulkan_radeon-32bit 2>/dev/null || gh_remove libvulkan_radeon"),
                        check_pkgs=["libvulkan_radeon"], reboot=False)

    def feat_amd_vaapi(self):
        return Resolved(install=self.script('gh_pick "Mesa-libva" "Mesa-dri"'),
                        check_any=[["Mesa-libva"], ["Mesa-dri"]], reboot=False)

    def feat_intel_vaapi(self):
        return Resolved(install=self.script('gh_pick "intel-media-driver"'),
                        remove=self.script("gh_remove intel-media-driver"), check_pkgs=["intel-media-driver"], reboot=False)

    def feat_intel_ddx(self):
        return Resolved(install=self.script('gh_pick "xf86-video-intel"'),
                        remove=self.script("gh_remove xf86-video-intel"), check_pkgs=["xf86-video-intel"])

    # ── controllers ─────────────────────────────────────────────────────────
    def feat_joystick(self):
        return Resolved(install=self.script("gh_install linuxconsoletools"),
                        remove=self.script("gh_remove linuxconsoletools"), check_pkgs=["linuxconsoletools"], reboot=False)

    def feat_xone(self):
        return Resolved(
            install=self.script(self.refresh_lines(), self.build_deps_lines(),
                                self.dkms_git("xone", "https://github.com/medusalix/xone",
                                              "bash install.sh --release\n"
                                              "command -v xone-get-firmware.sh >/dev/null && xone-get-firmware.sh --skip-disclaimer || true"),
                                "gh_sb_note"),
            check_cmd="dkms status 2>/dev/null | grep -qi '^xone[,/]'",
            note="Built from upstream with DKMS.")

    def feat_xpadneo(self):
        return Resolved(
            install=self.script(self.refresh_lines(), self.build_deps_lines(),
                                self.dkms_git("xpadneo", "https://github.com/atar-axis/xpadneo", "bash install.sh"),
                                "gh_sb_note"),
            check_cmd="dkms status 2>/dev/null | grep -qi '^xpadneo[,/]'")

    # ── WiFi ────────────────────────────────────────────────────────────────
    def feat_wifi_r8168(self):
        return Resolved(
            install=self.script(self.refresh_lines(), self.build_deps_lines(),
                                self.dkms_repo("r8168", "https://github.com/mtorromeo/r8168", "r8168", "8.0"), "gh_sb_note"),
            check_cmd="dkms status 2>/dev/null | grep -qi '^r8168'",
            note="Built from the community DKMS tree (mtorromeo/r8168).")

    def feat_wifi_broadcom_wl(self):
        return Resolved(
            install=self.script(self.refresh_lines(), self.build_deps_lines(), r'''
if gh_avail broadcom-wl-kmp-default; then gh_install broadcom-wl broadcom-wl-kmp-default
elif gh_avail broadcom-wl; then gh_install broadcom-wl
else
  echo "ERROR: the Broadcom wl driver is distributed through the Packman repository."
  echo "       Add Packman (https://en.opensuse.org/Additional_package_repositories#Packman), then run this again."
  exit 1
fi''', "gh_sb_note"),
            check_any=[["broadcom-wl-kmp-default"], ["broadcom-wl"]],
            note="Requires the Packman repository.")

    def feat_wifi_b43(self):
        return self.unsupported("wifi-b43", "openSUSE does not package the b43 firmware installer - use the "
                                            "Broadcom wl (Packman) option for these chips.")

    def feat_wifi_brcm_firmware(self):
        return Resolved(install=self.script('gh_pick "kernel-firmware-brcm" "kernel-firmware"'),
                        check_any=[["kernel-firmware-brcm"], ["kernel-firmware"]])

    def feat_wifi_kernel_upgrade(self):
        if self.osr.get("id") == "opensuse-leap":
            return Resolved(label="Update the kernel (zypper update kernel-default)",
                            install=self.script(self.refresh_lines(), "zypper --non-interactive update kernel-default"))
        return Resolved(label="Full distribution upgrade (zypper dup) for the newest kernel",
                        install=self.script(self.refresh_lines(), "zypper --non-interactive dup --auto-agree-with-licenses"),
                        note="Tumbleweed / Slowroll are rolling - a distribution upgrade brings the newest kernel.")

    # ── shared ──────────────────────────────────────────────────────────────
    def feat_build_deps(self):
        return Resolved(install=sh(self.prelude(), "gh_writable", self.refresh_lines(), self.build_deps_lines()), reboot=False)

    def feat_firmware(self):
        return Resolved(install=self.script("gh_install kernel-firmware"), check_pkgs=["kernel-firmware"])

    def feat_gaming(self):
        return Resolved(install=self.install_available_cmd(["gamemode", "libvulkan1", "libvulkan1-32bit", "libpulse0-32bit", "mangohud"]),
                        check_pkgs=["gamemode"], reboot=False)

    def install_failed_hint(self) -> str:
        return "Check your connection and repositories (zypper refresh)."
