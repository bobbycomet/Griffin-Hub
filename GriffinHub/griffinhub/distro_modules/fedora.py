"""
Griffin Hub distro module: Fedora / RHEL family (dnf)

Fedora, Nobara, Ultramarine, RHEL, CentOS Stream, AlmaLinux, Rocky ...

* NVIDIA / Broadcom / media drivers come from RPM Fusion (akmod-* packages built
  by akmods, not DKMS); the module enables RPM Fusion when it is missing,
  builds the module for the running kernel straight away, regenerates the
  initramfs and explains Secure Boot key enrollment.
* RHEL clones need EPEL + RPM Fusion for EL; the module sets both up.
* Image-based systems (Bazzite, Silverblue, Kinoite ...) cannot use dnf install -
  actions that need it say so instead of failing.
"""

import os
import shlex

from gh_distro_api import DistroModule, Resolved, run_quiet, sh


class Module(DistroModule):
    id = "fedora"
    name = "Fedora / RHEL (dnf)"
    version = "1.0.0"
    description = "dnf, RPM Fusion + akmods, kernel-devel for the running kernel, Secure Boot guidance."
    pkg_manager = "dnf"
    pkg_install_argv = ["dnf", "install", "-y"]
    family = "rhel"
    ids = ("fedora", "rhel", "centos", "almalinux", "rocky", "nobara", "bazzite", "ultramarine",
           "amzn", "ol", "oraclelinux", "silverblue", "kinoite", "aurora", "bluefin")
    exact_ids = ("fedora", "nobara", "bazzite", "rhel", "almalinux", "rocky", "centos")
    base_setup_packages = ("gamemode", "gamescope", "mangohud", "python3-psutil", "python3-pyyaml")

    # ── state ───────────────────────────────────────────────────────────────
    @property
    def immutable(self) -> bool:
        return os.path.exists("/run/ostree-booted")

    def is_installed(self, pkg: str) -> bool:
        return run_quiet(["rpm", "-q", pkg])[0] == 0

    def pm_functions(self) -> str:
        return ('GH_ID="%s"\n' % self.osr.get("id", "")) + r'''
gh_avail()   { dnf -q repoquery "$1" 2>/dev/null | grep -q . || rpm -q "$1" >/dev/null 2>&1; }
gh_install() { dnf install -y "$@"; }
gh_remove()  { dnf remove -y "$@"; }
gh_purge()   { dnf remove -y "$@"; }
gh_rpmfusion() {
  # make sure RPM Fusion (free + nonfree) is configured
  if rpm -q rpmfusion-nonfree-release >/dev/null 2>&1 && rpm -q rpmfusion-free-release >/dev/null 2>&1; then return 0; fi
  case "$GH_ID" in
    fedora|nobara|ultramarine|bazzite)
      V=$(rpm -E %fedora)
      dnf install -y "https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-$V.noarch.rpm" \
                     "https://mirrors.rpmfusion.org/nonfree/fedora/rpmfusion-nonfree-release-$V.noarch.rpm" ;;
    *)
      V=$(rpm -E %rhel)
      dnf install -y epel-release || true
      command -v crb >/dev/null 2>&1 && crb enable || true
      dnf install -y --nogpgcheck "https://mirrors.rpmfusion.org/free/el/rpmfusion-free-release-$V.noarch.rpm" \
                                  "https://mirrors.rpmfusion.org/nonfree/el/rpmfusion-nonfree-release-$V.noarch.rpm" ;;
  esac
}
gh_akmods_build() {
  # build akmod modules for the running kernel now instead of at next boot
  command -v akmods >/dev/null 2>&1 || return 0
  echo "Building kernel modules for $(uname -r) - this can take several minutes..."
  akmods --force --kernels "$(uname -r)" || akmods --force || echo "akmods reported a problem - check /var/cache/akmods/*.failed.log"
}
gh_fedora_sb() {
  if gh_secure_boot; then
    command -v kmodgenca >/dev/null 2>&1 && kmodgenca -a >/dev/null 2>&1 || true
    echo "NOTE: Secure Boot is enabled. To load the new kernel modules enroll the akmods signing key:"
    echo "      run in a terminal:  mokutil --import /etc/pki/akmods/certs/public_key.der"
    echo "      then reboot and choose 'Enroll MOK' (enter the password you just set)."
  fi
}
'''

    def build_deps_lines(self) -> str:
        return ('gh_install git dkms gcc make kernel-headers\n'
                'gh_pick "kernel-devel-$(uname -r)" "kernel-devel"')

    # guard for mutating features on image-based systems
    def resolve(self, key: str) -> Resolved:
        if self.immutable and key not in ("nvidia-580", "nvidia-570", "nvidia-550", "nvidia-open"):
            r = super().resolve(key)
            if r.ok and r.install:
                return Resolved(ok=False, reason="This is an image-based system (rpm-ostree). Use ujust / rpm-ostree "
                                                 "(or the distribution's driver images) instead of dnf.")
            return r
        if self.immutable:
            return Resolved(ok=False, reason="Image-based system: install NVIDIA drivers through your image variant "
                                             "(for Bazzite pick the -nvidia image) or rpm-ostree.")
        return super().resolve(key)

    # ── NVIDIA ──────────────────────────────────────────────────────────────
    def feat_nvidia_580(self):
        return Resolved(
            label="akmod-nvidia (RPM Fusion, current driver)",
            install=self.script(
                "gh_rpmfusion",
                "gh_install kmodtool akmods mokutil openssl gcc kernel-headers",
                'gh_pick "kernel-devel-$(uname -r)" "kernel-devel"',
                "gh_install akmod-nvidia xorg-x11-drv-nvidia-cuda",
                "gh_akmods_build", "gh_initramfs", "gh_fedora_sb",
                'echo "Reboot when the module build above has finished."'),
            remove=self.script("dnf remove -y 'xorg-x11-drv-nvidia*' 'akmod-nvidia*' 'kmod-nvidia*' nvidia-settings || true",
                               "gh_initramfs"),
            check_pkgs=["akmod-nvidia"],
            note="RPM Fusion ships the current NVIDIA driver (plus 580xx / 470xx / 390xx legacy branches).")

    def feat_nvidia_570(self):
        return self.unsupported("nvidia-570", "RPM Fusion offers the current driver and the legacy 580xx, 470xx and "
                                              "390xx branches only - use the first option.")

    def feat_nvidia_550(self):
        return self.unsupported("nvidia-550", "RPM Fusion offers the current driver and the legacy 580xx, 470xx and "
                                              "390xx branches only - use the first option.")

    def feat_nvidia_open(self):
        return self.unsupported("nvidia-open", "RPM Fusion ships a single akmod-nvidia package - use the first option. "
                                               "See rpmfusion.org/Howto/NVIDIA for kernel-module flavour options.")

    def feat_nvidia_cuda(self):
        return Resolved(install=self.script("gh_rpmfusion", "gh_install xorg-x11-drv-nvidia-cuda"),
                        remove=self.script("gh_remove xorg-x11-drv-nvidia-cuda"),
                        check_pkgs=["xorg-x11-drv-nvidia-cuda"], reboot=False,
                        note="Driver-level CUDA libraries and nvidia-smi. The full CUDA toolkit comes from NVIDIA's repository.")

    def feat_nvidia_nvenc(self):
        return Resolved(install=self.script("gh_rpmfusion",
                                            'gh_pick "xorg-x11-drv-nvidia-cuda-libs libva-nvidia-driver" "xorg-x11-drv-nvidia-cuda-libs"'),
                        check_pkgs=["xorg-x11-drv-nvidia-cuda-libs"], reboot=False)

    def feat_nvidia_settings(self):
        return Resolved(install=self.script("gh_rpmfusion", "gh_install nvidia-settings"),
                        remove=self.script("gh_remove nvidia-settings"), check_pkgs=["nvidia-settings"], reboot=False)

    def feat_nouveau_swap(self):
        return Resolved(install=self.script(
            "dnf remove -y 'xorg-x11-drv-nvidia*' 'akmod-nvidia*' 'kmod-nvidia*' nvidia-settings nvidia-modprobe nvidia-persistenced || true",
            "gh_install xorg-x11-drv-nouveau mesa-dri-drivers mesa-vulkan-drivers",
            "gh_initramfs"))

    # ── AMD / Intel ─────────────────────────────────────────────────────────
    def feat_amd_ddx(self):
        return Resolved(install=self.script("gh_install xorg-x11-drv-amdgpu"),
                        remove=self.script("gh_remove xorg-x11-drv-amdgpu"), check_pkgs=["xorg-x11-drv-amdgpu"])

    def feat_amd_rocm(self):
        return Resolved(install=self.script('gh_pick "rocm-hip rocm-opencl rocminfo rocm-smi" "rocm-hip rocminfo"'),
                        remove=self.script("gh_remove rocm-hip rocm-opencl rocminfo rocm-smi 2>/dev/null || true"),
                        check_any=[["rocm-hip"], ["rocminfo"]], reboot=False)

    def feat_amd_vulkan(self):
        return Resolved(install=self.script("gh_install mesa-vulkan-drivers"),
                        remove=self.script("gh_remove mesa-vulkan-drivers"),
                        check_pkgs=["mesa-vulkan-drivers"], reboot=False)

    def feat_amd_vaapi(self):
        return Resolved(
            label="VA-API (mesa-va-drivers-freeworld, RPM Fusion)",
            install=self.script("gh_rpmfusion", "dnf install -y --allowerasing mesa-va-drivers-freeworld"),
            check_any=[["mesa-va-drivers-freeworld"]], reboot=False,
            note="Fedora's stock Mesa has H.264/HEVC hardware decode disabled; the freeworld build enables it.")

    def feat_intel_vaapi(self):
        return Resolved(install=self.script("gh_rpmfusion", "gh_install intel-media-driver"),
                        remove=self.script("gh_remove intel-media-driver"),
                        check_pkgs=["intel-media-driver"], reboot=False)

    def feat_intel_ddx(self):
        return Resolved(install=self.script("gh_install xorg-x11-drv-intel"),
                        remove=self.script("gh_remove xorg-x11-drv-intel"), check_pkgs=["xorg-x11-drv-intel"])

    # ── controllers ─────────────────────────────────────────────────────────
    def feat_joystick(self):
        return Resolved(install=self.script('gh_pick "linuxconsoletools" "joystick"'),
                        remove=self.script("gh_remove linuxconsoletools 2>/dev/null || true"),
                        check_any=[["linuxconsoletools"], ["joystick"]], reboot=False)

    def feat_xone(self):
        return Resolved(
            install=self.script(self.build_deps_lines(),
                                self.dkms_git("xone", "https://github.com/medusalix/xone",
                                              "bash install.sh --release\n"
                                              "command -v xone-get-firmware.sh >/dev/null && xone-get-firmware.sh --skip-disclaimer || true"),
                                "gh_fedora_sb"),
            check_cmd="dkms status 2>/dev/null | grep -qi '^xone[,/]'",
            note="Built from upstream with DKMS.")

    def feat_xpadneo(self):
        return Resolved(
            install=self.script(self.build_deps_lines(),
                                self.dkms_git("xpadneo", "https://github.com/atar-axis/xpadneo", "bash install.sh"),
                                "gh_fedora_sb"),
            check_cmd="dkms status 2>/dev/null | grep -qi '^xpadneo[,/]'")

    # ── WiFi ────────────────────────────────────────────────────────────────
    def feat_wifi_r8168(self):
        # no maintained RPM: build the upstream DKMS tree
        return Resolved(
            install=self.script(self.build_deps_lines(),
                                self.dkms_repo("r8168", "https://github.com/mtorromeo/r8168", "r8168", "8.0"),
                                "gh_fedora_sb"),
            check_cmd="dkms status 2>/dev/null | grep -qi '^r8168'",
            note="Built from the community DKMS tree (mtorromeo/r8168).")

    def feat_wifi_broadcom_wl(self):
        return Resolved(
            label="broadcom-wl (RPM Fusion nonfree, akmod)",
            install=self.script("gh_rpmfusion", "gh_install kmodtool akmods mokutil gcc kernel-headers",
                                'gh_pick "kernel-devel-$(uname -r)" "kernel-devel"',
                                "gh_install broadcom-wl", "gh_akmods_build", "gh_fedora_sb"),
            remove=self.script("dnf remove -y broadcom-wl akmod-wl kmod-wl || true"),
            check_any=[["broadcom-wl"], ["akmod-wl"]])

    def feat_wifi_b43(self):
        return self.unsupported("wifi-b43", "Fedora does not package the b43 firmware installer - use the "
                                            "Broadcom wl (RPM Fusion) option for these chips.")

    def feat_wifi_brcm_firmware(self):
        return Resolved(install=self.script("gh_install linux-firmware"), check_pkgs=["linux-firmware"])

    def feat_wifi_kernel_upgrade(self):
        return Resolved(label="Update the kernel (dnf upgrade kernel*)",
                        install=self.script("dnf upgrade -y --refresh 'kernel*'"),
                        note="Reboot into the new kernel afterwards.")

    # ── shared ──────────────────────────────────────────────────────────────
    def feat_build_deps(self):
        return Resolved(install=self.build_deps(), reboot=False)

    def feat_firmware(self):
        return Resolved(install=self.script("gh_install linux-firmware"), check_pkgs=["linux-firmware"])

    def feat_gaming(self):
        return Resolved(install=self.script(r'''
gh_install gamemode vulkan-loader mesa-vulkan-drivers
gh_install vulkan-loader.i686 mesa-vulkan-drivers.i686 pulseaudio-libs.i686 || echo "32-bit libraries unavailable - skipped"
gh_rpmfusion || true
gh_install mangohud steam-devices || echo "mangohud / steam-devices unavailable - skipped"'''),
                        check_pkgs=["gamemode"], reboot=False)

    def install_failed_hint(self) -> str:
        return "Check your connection and repositories (dnf makecache). Some drivers need RPM Fusion."
