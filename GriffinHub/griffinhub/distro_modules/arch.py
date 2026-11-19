"""
Griffin Hub distro module: Arch Linux family (pacman)

Arch, Manjaro, EndeavourOS, CachyOS, Garuda, Artix, SteamOS ...

* Kernel headers always match the *running* kernel package (linux, linux-lts,
  linux-zen, linux-cachyos, linux612 ...), read from /usr/lib/modules/<ver>/pkgbase.
* NVIDIA uses the official repository packages (nvidia / nvidia-lts /
  nvidia-dkms, nvidia-open*), regenerates the initramfs and handles the
  nvidia <-> nvidia-open conflict.
* AUR-only packages (xone, xpadneo, b43-firmware ...) are built *without* an AUR
  helper: Griffin Hub installs run as root and AUR helpers / makepkg refuse
  root, so the module builds as the desktop user and installs with pacman -U.
  If the AUR build fails it falls back to the upstream DKMS recipe.
"""

import shlex

from gh_distro_api import DistroModule, Resolved, run_quiet, sh


class Module(DistroModule):
    id = "arch"
    name = "Arch Linux (pacman)"
    version = "1.0.0"
    description = "pacman, headers for the running kernel, multilib aware, AUR builds without a helper."
    pkg_manager = "pacman"
    pkg_install_argv = ["pacman", "-S", "--noconfirm", "--needed"]
    family = "arch"
    ids = ("arch", "manjaro", "endeavouros", "cachyos", "garuda", "artix", "steamos",
           "archcraft", "arcolinux", "blendos", "rebornos", "biglinux", "holo")
    exact_ids = ("arch", "manjaro", "endeavouros", "cachyos", "garuda", "artix", "steamos")
    base_setup_packages = ("gamemode", "gamescope", "mangohud", "python-psutil", "python-yaml")

    def is_installed(self, pkg: str) -> bool:
        return run_quiet(["pacman", "-Q", pkg])[0] == 0

    def pm_functions(self) -> str:
        return r'''
gh_avail()   { pacman -Si "$1" >/dev/null 2>&1; }
gh_install() { pacman -S --noconfirm --needed "$@"; }
gh_remove()  { pacman -Rns --noconfirm "$@"; }
gh_purge()   { pacman -Rns --noconfirm "$@"; }
gh_multilib() { grep -Eq '^\[multilib\]' /etc/pacman.conf 2>/dev/null; }
gh_kernel_pkg() {
  # package that provides the running kernel: linux, linux-lts, linux-zen, linux-cachyos, linux612 ...
  local k b; k=$(uname -r); b=$(cat "/usr/lib/modules/$k/pkgbase" 2>/dev/null || true)
  if [ -n "$b" ]; then echo "$b"; else
    case "$k" in *lts*) echo linux-lts;; *zen*) echo linux-zen;; *hardened*) echo linux-hardened;; *) echo linux;; esac
  fi
}
gh_lib32() {
  # echo the lib32 variants of the given packages when [multilib] is enabled
  if gh_multilib; then for p in "$@"; do echo "lib32-$p"; done; else echo "Note: [multilib] is not enabled - skipping 32-bit libraries" >&2; fi
}
gh_aur() {
  # Build and install AUR packages without an AUR helper (makepkg refuses to run as root).
  local U H d p deps dep
  U=$(getent passwd "${PKEXEC_UID:-}" 2>/dev/null | cut -d: -f1)
  { [ -n "$U" ] && [ "$U" != root ]; } || U=$(logname 2>/dev/null || true)
  { [ -n "$U" ] && [ "$U" != root ]; } || { echo "ERROR: cannot determine the desktop user to build AUR packages as."; return 1; }
  H=$(getent passwd "$U" | cut -d: -f6)
  gh_install base-devel git || return 1
  d=$(mktemp -d /tmp/gh-aur.XXXXXX); chown "$U" "$d"
  for p in "$@"; do
    runuser -u "$U" -- git clone --depth 1 "https://aur.archlinux.org/$p.git" "$d/$p" \
      || { echo "ERROR: could not fetch AUR package $p"; rm -rf "$d"; return 1; }
    deps=$(sed -n 's/^[[:space:]]*\(make\)\{0,1\}depends\(_x86_64\)\{0,1\} = \([^<>=: ]*\).*/\3/p' "$d/$p/.SRCINFO" | sort -u)
    for dep in $deps; do gh_avail "$dep" && pacman -S --noconfirm --needed --asdeps "$dep" >/dev/null 2>&1 || true; done
    ( cd "$d/$p" && runuser -u "$U" -- env HOME="$H" makepkg -f --noconfirm ) \
      || { echo "ERROR: makepkg failed for $p"; rm -rf "$d"; return 1; }
    pacman -U --noconfirm --needed $(ls "$d/$p"/*.pkg.tar.* | grep -v -- '-debug-') || { rm -rf "$d"; return 1; }
  done
  rm -rf "$d"
}
gh_nv_conflicts() {
  # remove conflicting NVIDIA kernel-module flavours (keeps nvidia-utils); $1 = the package being installed
  local c
  for c in nvidia nvidia-lts nvidia-dkms nvidia-open nvidia-open-lts nvidia-open-dkms; do
    [ "$c" = "$1" ] && continue
    pacman -Q "$c" >/dev/null 2>&1 && { echo "Replacing conflicting package $c"; pacman -Rdd --noconfirm "$c"; }
  done
  return 0
}
'''

    def build_deps_lines(self) -> str:
        return 'gh_install base-devel git dkms "$(gh_kernel_pkg)-headers"'

    # ── NVIDIA ──────────────────────────────────────────────────────────────
    def _nv(self, flavour: str) -> Resolved:
        """flavour: '' (proprietary) or 'open'."""
        pre = "nvidia-open" if flavour else "nvidia"
        script = self.script(f'''
K=$(gh_kernel_pkg)
case "$K" in
  linux) P={pre} ;;
  linux-lts) P={pre}-lts ;;
  *) P={pre}-dkms; gh_install "$K-headers" dkms ;;
esac
gh_nv_conflicts "$P"
L=$(gh_lib32 nvidia-utils)
gh_install "$P" nvidia-utils $L
gh_initramfs
gh_sb_note''')
        return Resolved(
            label=("nvidia-open (open kernel module, Arch repository)" if flavour
                   else "nvidia (proprietary, Arch repository)"),
            install=script,
            remove=self.script("P=$(pacman -Qq | grep -E '^(lib32-)?nvidia(-utils|-open|-lts|-dkms|-open-lts|-open-dkms)?$' || true)",
                               '[ -z "$P" ] || pacman -Rdd --noconfirm $P', "gh_initramfs"),
            check_any=[[pre], [pre + "-lts"], [pre + "-dkms"]],
            note="Arch ships NVIDIA's current driver branch; older branches are AUR-only (nvidia-580xx-dkms, 470xx, 390xx).")

    def feat_nvidia_580(self):
        return self._nv("")

    def feat_nvidia_570(self):
        return self.unsupported("nvidia-570", "Arch does not package this branch - use the first option (current "
                                              "repository driver). Legacy branches exist in the AUR (nvidia-580xx-dkms).")

    def feat_nvidia_550(self):
        return self.unsupported("nvidia-550", "Arch does not package this branch - use the first option (current "
                                              "repository driver). Legacy branches exist in the AUR (nvidia-470xx-dkms).")

    def feat_nvidia_open(self):
        return self._nv("open")

    def feat_nvidia_cuda(self):
        return Resolved(install=self.script("gh_install cuda"), remove=self.script("gh_remove cuda"),
                        check_pkgs=["cuda"], reboot=False)

    def feat_nvidia_nvenc(self):
        return Resolved(install=self.script('gh_pick "nvidia-utils libva-nvidia-driver" "nvidia-utils"'),
                        check_pkgs=["nvidia-utils"], reboot=False,
                        note="NVENC/NVDEC libraries ship inside nvidia-utils on Arch.")

    def feat_nvidia_settings(self):
        return Resolved(install=self.script("gh_install nvidia-settings"), remove=self.script("gh_remove nvidia-settings"),
                        check_pkgs=["nvidia-settings"], reboot=False)

    def feat_nouveau_swap(self):
        return Resolved(install=self.script(r'''
P=$(pacman -Qq | grep -E '^(lib32-)?(nvidia|opencl-nvidia)' || true)
[ -z "$P" ] || pacman -Rdd --noconfirm $P
gh_install mesa xf86-video-nouveau
gh_avail vulkan-nouveau && gh_install vulkan-nouveau || true
gh_initramfs'''))

    # ── AMD / Intel ─────────────────────────────────────────────────────────
    def feat_amd_ddx(self):
        return Resolved(install=self.script("gh_install xf86-video-amdgpu"),
                        remove=self.script("gh_remove xf86-video-amdgpu"), check_pkgs=["xf86-video-amdgpu"])

    def feat_amd_rocm(self):
        return Resolved(install=self.script("gh_install rocm-hip-sdk rocm-opencl-sdk"),
                        remove=self.script("gh_remove rocm-hip-sdk rocm-opencl-sdk"),
                        check_pkgs=["rocm-hip-sdk", "rocm-opencl-sdk"], reboot=False)

    def feat_amd_vulkan(self):
        return Resolved(install=self.script('L=$(gh_lib32 vulkan-radeon)', "gh_install vulkan-radeon $L"),
                        remove=self.script("gh_remove vulkan-radeon lib32-vulkan-radeon 2>/dev/null || gh_remove vulkan-radeon"),
                        check_pkgs=["vulkan-radeon"], reboot=False)

    def feat_amd_vaapi(self):
        return Resolved(install=self.script('gh_pick "libva-mesa-driver" "mesa"'),
                        check_any=[["libva-mesa-driver"], ["mesa"]], reboot=False,
                        note="Recent Mesa builds include the VA-API driver in the mesa package.")

    def feat_intel_vaapi(self):
        return Resolved(install=self.script("gh_install intel-media-driver"),
                        remove=self.script("gh_remove intel-media-driver"),
                        check_pkgs=["intel-media-driver"], reboot=False)

    def feat_intel_ddx(self):
        return Resolved(install=self.script("gh_install xf86-video-intel"),
                        remove=self.script("gh_remove xf86-video-intel"), check_pkgs=["xf86-video-intel"])

    # ── controllers ─────────────────────────────────────────────────────────
    def feat_joystick(self):
        return Resolved(install=self.script('gh_pick "linuxconsole" "linuxconsoletools" "joyutils"'),
                        remove=self.script("gh_remove linuxconsole 2>/dev/null || true"),
                        check_any=[["linuxconsole"], ["linuxconsoletools"], ["joyutils"]], reboot=False)

    def _aur_or_git(self, aur_pkgs: str, git_script: str, extra_pre: str = "") -> str:
        return (
            f'if gh_aur {aur_pkgs}; then\n  echo "Installed from the AUR."\nelse\n'
            '  echo "AUR build failed - falling back to the upstream DKMS build"\n'
            f"{git_script}\nfi")

    def feat_xone(self):
        git = self.dkms_git("xone", "https://github.com/medusalix/xone",
                            "bash install.sh --release\n"
                            "command -v xone-get-firmware.sh >/dev/null && xone-get-firmware.sh --skip-disclaimer || true")
        pick = ('PK=xone-dkms\n'
                'kv=$(uname -r | cut -d. -f1,2); ma=${kv%%.*}; mi=${kv#*.}\n'
                '{ [ "$ma" -gt 6 ] || { [ "$ma" -eq 6 ] && [ "$mi" -ge 4 ]; }; } && PK=xone-dkms-git')
        return Resolved(
            install=self.script(self.build_deps_lines(), pick,
                                self._aur_or_git('"$PK" xone-dongle-firmware', git), "gh_sb_note"),
            check_cmd="dkms status 2>/dev/null | grep -qi '^xone[,/]'",
            note="Installed from the AUR (xone-dkms / xone-dkms-git on kernels 6.4+); falls back to the upstream DKMS build.")

    def feat_xpadneo(self):
        git = self.dkms_git("xpadneo", "https://github.com/atar-axis/xpadneo", "bash install.sh")
        return Resolved(
            install=self.script(self.build_deps_lines(), self._aur_or_git("xpadneo-dkms", git), "gh_sb_note"),
            check_cmd="dkms status 2>/dev/null | grep -qi '^xpadneo[,/]'")

    # ── WiFi ────────────────────────────────────────────────────────────────
    def feat_wifi_r8168(self):
        return Resolved(
            install=self.script(self.build_deps_lines(),
                                'gh_pick "r8168-dkms" "r8168" || gh_aur r8168-dkms'),
            remove=self.script("gh_remove r8168-dkms r8168 2>/dev/null || true"),
            check_any=[["r8168-dkms"], ["r8168"]])

    def feat_wifi_broadcom_wl(self):
        return Resolved(
            install=self.script(self.build_deps_lines(),
                                'gh_pick "broadcom-wl-dkms" || gh_aur broadcom-wl-dkms', "gh_sb_note"),
            remove=self.script("gh_remove broadcom-wl-dkms broadcom-wl 2>/dev/null || true"),
            check_any=[["broadcom-wl-dkms"], ["broadcom-wl"]])

    def feat_wifi_b43(self):
        return Resolved(
            install=self.script("gh_install b43-fwcutter", "gh_aur b43-firmware"),
            remove=self.script("gh_remove b43-firmware"), check_pkgs=["b43-firmware"])

    def feat_wifi_brcm_firmware(self):
        return Resolved(install=self.script('gh_pick "linux-firmware-broadcom" "linux-firmware"'),
                        check_any=[["linux-firmware-broadcom"], ["linux-firmware"]])

    def feat_wifi_kernel_upgrade(self):
        return Resolved(label="Full system update (pacman -Syu) for the newest kernel",
                        install=self.script("pacman -Syu --noconfirm"),
                        note="Arch is rolling - a full update brings the newest kernel.")

    # ── shared ──────────────────────────────────────────────────────────────
    def feat_build_deps(self):
        return Resolved(install=self.build_deps(), reboot=False)

    def feat_firmware(self):
        return Resolved(install=self.script('gh_pick "linux-firmware"'), check_pkgs=["linux-firmware"])

    def feat_gaming(self):
        return Resolved(install=self.script(r'''
L=$(gh_lib32 gamemode mangohud vulkan-icd-loader libpulse)
gh_install gamemode mangohud vulkan-icd-loader $L
gh_avail steam-devices && gh_install steam-devices || true'''),
                        check_pkgs=["gamemode"], reboot=False)

    def install_failed_hint(self) -> str:
        return ("Check your connection and mirrors. If pacman reports 404 errors run a full update "
                "(pacman -Syu) first - Arch does not support partial upgrades.")
