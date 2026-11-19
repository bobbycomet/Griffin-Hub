import sys, importlib.util, subprocess, os, re, shutil
ROOT=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
D=os.path.join(ROOT,'griffinhub','distro_modules')
# stub package managers (log every call); the stubs write to /tmp/fs/log/calls.log
import shutil as _sh
_sh.rmtree('/tmp/fs',ignore_errors=True); os.makedirs('/tmp/fs/log'); _sh.copytree(os.path.join(os.path.dirname(os.path.abspath(__file__)),'stubs'),'/tmp/fs/bin')
for _f in os.listdir('/tmp/fs/bin'): os.chmod('/tmp/fs/bin/'+_f,0o755)
def load(n):
    s=importlib.util.spec_from_file_location(n,f'{D}/{n}.py'); m=importlib.util.module_from_spec(s); sys.modules[n]=m; s.loader.exec_module(m); return m
api=load('_api'); sys.modules['gh_distro_api']=api
M={n:load(n).Module for n in ('debian','arch','fedora','suse')}
LOG='/tmp/fs/log/calls.log'
res=[]
TOOLS={'debian':'apt-get apt-cache update-initramfs dpkg dpkg-query mokutil dkms git uname',
 'arch':'pacman mkinitcpio mokutil dkms git uname runuser makepkg getent chown logname findmnt',
 'fedora':'dnf rpm dracut akmods kmodgenca mokutil dkms git uname',
 'suse':'zypper rpm dracut dkms git uname findmnt mokutil'}
def bindir(mod):
    d=f'/tmp/fs/bin_{mod}'; shutil.rmtree(d,ignore_errors=True); os.makedirs(d)
    for t in TOOLS[mod].split(): os.symlink(f'/tmp/fs/bin/{t}',f'{d}/{t}')
    return d
def run(label, mod, osr, key=None, script=None, env=None, expect=(), forbid=(), rc=0, order=None):
    m=M[mod](api.parse_os_release(osr))
    sc=script if script is not None else m.resolve(key).install
    open(LOG,'w').close()
    e={**os.environ,'PATH':bindir(mod)+':'+os.environ['PATH'],**(env or {})}
    r=subprocess.run(['bash','-c',sc],capture_output=True,text=True,env=e,cwd='/tmp')
    calls=open(LOG).read()
    ok=(r.returncode==rc)
    miss=[x for x in expect if not re.search(x,calls+r.stdout)]
    bad=[x for x in forbid if re.search(x,calls+r.stdout)]
    if order:
        pos=[re.search(x,calls) for x in order]
        idx=[p.start() if p else -1 for p in pos]
        if -1 in idx or idx!=sorted(idx): miss.append('ORDER '+str(order))
    good=ok and not miss and not bad
    res.append(good)
    print(('PASS ' if good else 'FAIL ')+label+('' if good else f'\n   rc={r.returncode} (want {rc}) missing={miss} forbidden={bad}\n   out={r.stdout[-300:]!r} err={r.stderr[-200:]!r}\n   calls={calls[-400:]!r}'))
UB='ID=ubuntu\nID_LIKE=debian\nVERSION_ID="24.04"\n'; DB='ID=debian\nVERSION_ID="13"\n'
AR='ID=arch\n'; FE='ID=fedora\nVERSION_ID=42\n'; TW='ID="opensuse-tumbleweed"\nID_LIKE="opensuse suse"\n'; LP='ID="opensuse-leap"\nVERSION_ID="15.6"\n'
# ── Debian / Ubuntu ──
run('ubuntu nvidia-580: update, install driver-580, no DKMS toolchain','debian',UB,'nvidia-580',expect=[r'apt-get update',r'apt-get install -y nvidia-driver-580'],forbid=[r'build-essential'])
run('debian nvidia: needs non-free -> clear error when nvidia-driver unavailable','debian',DB,'nvidia-580',env={'AC_MISSING':'nvidia-driver'},rc=1,expect=[r'non-free'])
run('debian nvidia: installs headers+dkms then nvidia-driver','debian',DB,'nvidia-580',expect=[r'build-essential',r'linux-headers',r'apt-get install -y nvidia-driver'])
run('ubuntu headers fall back to linux-headers-generic when exact missing','debian',UB,script=M['debian'](api.parse_os_release(UB)).build_deps(),env={'AC_MISSING':'linux-headers-6.12.1-1-default'},expect=[r'install -y linux-headers-generic'])
run('debian headers fall back to arch meta package','debian',DB,script=M['debian'](api.parse_os_release(DB)).build_deps(),env={'AC_MISSING':'linux-headers-6.12.1-1-default linux-headers-generic'},expect=[r'install -y linux-headers-amd64'])
run('gh_pick fails clearly when nothing available','debian',UB,script=M['debian'](api.parse_os_release(UB)).script('gh_pick "nope1" "nope2"'),env={'AC_MISSING':'nope1 nope2'},rc=1,expect=[r'none of these package sets'])
run('ubuntu setup skips unavailable (gamescope) instead of failing','debian',UB,script=M['debian'](api.parse_os_release(UB)).install_available_cmd(['gamemode','gamescope','mangohud']),env={'AC_MISSING':'gamescope'},expect=[r'Skipped.*gamescope',r'install -y gamemode mangohud'],forbid=[r'install -y.*gamescope'])
run('ubuntu hwe kernel uses VERSION_ID','debian',UB,'wifi-kernel-upgrade',expect=[r'--install-recommends linux-generic-hwe-24.04'])
# ── Arch ──
mk=lambda k:(os.makedirs(f'/usr/lib/modules/{k}',exist_ok=True))
for kv,pk in (('6.12.1-1-default','linux'),('6.12.1-1-lts','linux-lts'),('6.12.1-1-cachyos','linux-cachyos')):
    mk(kv); open(f'/usr/lib/modules/{kv}/pkgbase','w').write(pk+'\n')
open('/etc/pacman.conf','w').write('[options]\n[multilib]\nInclude = /etc/pacman.d/mirrorlist\n')
run('arch nvidia on stock linux -> nvidia + utils + lib32 + initramfs','arch',AR,'nvidia-580',env={'KVER':'6.12.1-1-default'},expect=[r'pacman -S --noconfirm --needed nvidia nvidia-utils lib32-nvidia-utils',r'mkinitcpio -P'])
run('arch nvidia on linux-lts -> nvidia-lts','arch',AR,'nvidia-580',env={'KVER':'6.12.1-1-lts'},expect=[r'needed nvidia-lts nvidia-utils'])
run('arch nvidia on linux-cachyos -> nvidia-dkms + matching headers','arch',AR,'nvidia-580',env={'KVER':'6.12.1-1-cachyos'},expect=[r'linux-cachyos-headers dkms',r'needed nvidia-dkms nvidia-utils'],order=[r'linux-cachyos-headers',r'nvidia-dkms nvidia-utils'])
run('arch nvidia-open on stock kernel -> nvidia-open','arch',AR,'nvidia-open',env={'KVER':'6.12.1-1-default'},expect=[r'needed nvidia-open nvidia-utils'])
run('arch conflict handling removes installed nvidia-dkms before nvidia','arch',AR,'nvidia-580',env={'KVER':'6.12.1-1-default','PQ_RC':'0'},expect=[r'pacman -Rdd --noconfirm nvidia-dkms'])
run('arch build deps use pkgbase headers','arch',AR,'build-deps',env={'KVER':'6.12.1-1-cachyos'},expect=[r'base-devel git dkms linux-cachyos-headers'])
os.remove('/etc/pacman.conf')
run('arch without multilib skips lib32 but still installs','arch',AR,'nvidia-580',env={'KVER':'6.12.1-1-default'},expect=[r'needed nvidia nvidia-utils\b',r'initcpio|mkinitcpio'],forbid=[r'lib32-nvidia-utils\n'])
open('/etc/pacman.conf','w').write('[multilib]\nInclude = x\n')
run('arch AUR build: builds as user, installs repo deps, pacman -U without debug pkg','arch',AR,script=M['arch'](api.parse_os_release(AR)).script('gh_aur xone-dkms'),env={'PKEXEC_UID':'1000','GIT_AUR':'1'},
    expect=[r'runuser -u tester -- git clone --depth 1 https://aur.archlinux.org/xone-dkms.git',r'--asdeps dkms',r'makepkg HOME=/home/tester',r'pacman -U --noconfirm --needed .*x-1-1-any\.pkg\.tar\.zst'],forbid=[r'asdeps aur-only-dep',r'debug'])
run('arch xone: AUR failure falls back to upstream DKMS build','arch',AR,'xone',env={'PKEXEC_UID':'1000','GIT_AUR':'1','MAKEPKG_FAIL':'1','KVER':'6.12.1-1-default'},expect=[r'falling back to the upstream DKMS',r'install\.sh'])
run('arch xone on kernel>=6.4 picks xone-dkms-git','arch',AR,'xone',env={'PKEXEC_UID':'1000','GIT_AUR':'1','KVER':'6.12.1-1-default'},expect=[r'aur\.archlinux\.org/xone-dkms-git\.git',r'xone-dongle-firmware'])
os.remove('/etc/pacman.conf')
# ── Fedora ──
run('fedora nvidia: rpmfusion, akmods build, dracut, Secure Boot guidance','fedora',FE,'nvidia-580',env={'SB':'1'},expect=[r'/fedora/rpmfusion-free-release-42',r'install -y kmodtool akmods mokutil',r'akmod-nvidia xorg-x11-drv-nvidia-cuda',r'akmods --force --kernels',r'dracut --force',r'mokutil --import /etc/pki/akmods'],order=[r'rpmfusion-free',r'akmod-nvidia xorg',r'akmods --force',r'dracut'])
run('fedora nvidia: skips rpmfusion when already configured','fedora',FE,'nvidia-580',env={'RPM_Q_RC':'0'},forbid=[r'rpmfusion-free-release-42\.noarch'])
run('fedora broadcom-wl via rpmfusion + akmods','fedora',FE,'wifi-broadcom-wl',expect=[r'install -y broadcom-wl',r'akmods --force'])
run('rocky nvidia uses EL rpmfusion + EPEL','fedora','ID="rocky"\nID_LIKE="rhel centos fedora"\nVERSION_ID="9.4"\n','nvidia-580',expect=[r'epel-release',r'/el/rpmfusion-free-release-42'],forbid=[r'/fedora/rpmfusion'])
run('fedora build deps pick kernel-devel for running kernel','fedora',FE,'build-deps',env={'KVER':'6.12.1-1.fc42.x86_64'},expect=[r'install -y kernel-devel-6\.12\.1-1\.fc42\.x86_64'])
run('fedora kernel-devel fallback when exact build missing','fedora',FE,'build-deps',env={'KVER':'6.12.1-1.fc42.x86_64','DNF_MISSING':'kernel-devel-6.12.1-1.fc42.x86_64'},expect=[r'install -y kernel-devel kernel-headers|install -y kernel-devel\b'])
# ── openSUSE ──
run('tumbleweed nvidia: adds NVIDIA repo then installs G06','suse',TW,'nvidia-580',expect=[r'addrepo --refresh https://download.nvidia.com/opensuse/tumbleweed NVIDIA',r'install --auto-agree-with-licenses nvidia-video-G06'],order=[r'addrepo',r'nvidia-driver-G06-kmp-default'])
run('leap nvidia uses leap/VERSION_ID repo','suse',LP,'nvidia-580',expect=[r'opensuse/leap/15\.6'])
run('suse nvidia: repo not re-added when present','suse',TW,'nvidia-580',env={'HAVE_NV_REPO':'1'},forbid=[r'addrepo'])
run('suse open driver installs signed meta package','suse',TW,'nvidia-open',expect=[r'nvidia-open-driver-G06-signed-kmp-meta'])
run('suse headers use running kernel flavour','suse',TW,'build-deps',env={'KVER':'6.12.1-1-longterm'},expect=[r'kernel-longterm-devel'])
run('suse broadcom explains Packman when package missing','suse',TW,'wifi-broadcom-wl',env={'ZY_MISSING':'broadcom-wl-kmp-default broadcom-wl'},rc=1,expect=[r'Packman'])
# ── generic DKMS recipe: parses dkms.conf ──
run('dkms recipe reads name/version from dkms.conf and runs add/build/install','debian',UB,'wifi-rtl8821ce',expect=[r'dkms add -m mydrv -v 1.2.3',r'dkms build -m mydrv -v 1.2.3',r'dkms install -m mydrv -v 1.2.3',r'git clone --depth 1 --recurse-submodules https://github.com/tomaspinho/rtl8821ce'])
shutil.rmtree('/usr/local/src/griffinhub-drivers',ignore_errors=True)
for kv in ('6.12.1-1-default','6.12.1-1-lts','6.12.1-1-cachyos'): shutil.rmtree(f'/usr/lib/modules/{kv}',ignore_errors=True)
print(f'\nSUMMARY: {sum(res)}/{len(res)} flow tests passed')
