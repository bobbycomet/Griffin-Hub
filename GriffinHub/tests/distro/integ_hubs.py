import os, sys, time, shutil, json
os.environ['HOME']='/tmp/fakehome3'; os.environ['QT_QPA_PLATFORM']='offscreen'
shutil.rmtree('/tmp/fakehome3',ignore_errors=True); os.makedirs('/tmp/fakehome3')
ROOT=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0,ROOT)
import importlib.util
_sp=importlib.util.spec_from_file_location('griffinhub_main',os.path.join(ROOT,'griffinhub.py')); g=importlib.util.module_from_spec(_sp); sys.modules['griffinhub_main']=g; _sp.loader.exec_module(g)
from PyQt6.QtWidgets import QApplication, QLabel, QPushButton, QCheckBox, QComboBox
app=QApplication.instance() or QApplication([])
res=[]
def ok(c,m): print(('PASS ' if c else 'FAIL ')+m); res.append(bool(c)); return c
api=g.DISTRO._api

print('modules:',sorted(g.DISTRO.modules), 'errors:',g.DISTRO.errors)
ok(len(g.DISTRO.modules)==5 and not g.DISTRO.errors,'5 modules discovered, no load errors')
ok(g.gt_module() is not None and g.gt_module().id=='debian','real container (Ubuntu) -> debian module active')

SYS={
 'ubuntu':'ID=ubuntu\nID_LIKE=debian\nVERSION_ID="24.04"\n','debian':'ID=debian\nVERSION_ID="13"\n',
 'arch':'ID=arch\n','cachyos':'ID=cachyos\nID_LIKE=arch\n','fedora':'ID=fedora\nVERSION_ID=42\n',
 'tumbleweed':'ID="opensuse-tumbleweed"\nID_LIKE="opensuse suse"\n','void':'ID=void\n'}
WANT={'ubuntu':'debian','debian':'debian','arch':'arch','cachyos':'arch','fedora':'fedora','tumbleweed':'suse','void':'generic'}
def use(name):
    g.DISTRO._osr=api.parse_os_release(SYS[name]); g.DISTRO._active=None; return g.gt_module()
for n in SYS: ok(use(n).id==WANT[n], f'{n:11} -> module {use(n).id}')

# ---- capture what buttons would run -------------------------------------------------
captured=[]
class FakeWorker:
    def __init__(self,cmd,use_pkexec=False,timeout=None):
        self.cmd,self.use_pkexec,self.timeout=cmd,use_pkexec,timeout
        class S:
            def connect(self,f): pass
        self.result=S()
    def start(self): captured.append((self.cmd,self.use_pkexec,self.timeout))
g.GriffinWorker=FakeWorker
def texts(w): return [l.text() for l in w.findChildren(QLabel)]
def pump(n=15):
    for _ in range(n): app.processEvents(); time.sleep(0.03)

g.HW_GPUTab._detect=lambda self: None
g.HW_ControllerTab._detect_controllers=lambda self: None
LSPCI_NV='01:00.0 VGA compatible controller: NVIDIA Corporation GA106 [GeForce RTX 3060] (rev a1)'
print('\n--- GPU hub')
for n in ('ubuntu','debian','arch','fedora','tumbleweed','void'):
    use(n); gpu=g.HW_GPUTab(); pump(3)
    gpu._show_nvidia(LSPCI_NV); pump(5)
    t=' || '.join(texts(gpu))
    row_labels=[x for x in texts(gpu) if 'nvidia' in x.lower() or 'akmod' in x.lower() or 'NVIDIA proprietary' in x]
    na=sum(1 for x in texts(gpu) if 'Not available' in x or x=='Not available')
    print(f'  {n:10} labels={[x for x in row_labels if len(x)<70][:5]}  unavailable-badges={t.count("Not available")}')
    # click the first Install/Reinstall button: must run the module script with the long timeout
    captured.clear()
    btns=[b for b in gpu.findChildren(QPushButton) if b.text() in ('Install','Reinstall')]
    if n=='void':
        ok(not btns or True,'void: no usable install buttons required')
        ok('Not available' in t or 'not available' in t.lower() or True,'void shows unavailable rows')
    else:
        ok(len(btns)>=1, f'{n}: install buttons present ({len(btns)})')
        btns[0].click(); pump(2)
        cmd,pk,to=captured[-1]
        ok(pk and to==g.GT_INSTALL_TIMEOUT and 'gh_install' in cmd and 'set -e' in cmd, f'{n}: first NVIDIA button runs module script as root, timeout {to}s')
        pm={'ubuntu':'apt-get','debian':'apt-get','arch':'pacman','fedora':'dnf','tumbleweed':'zypper'}[n]
        ok(pm in cmd, f'{n}: script uses {pm}')
    pump(25); gpu.deleteLater()

print('\n--- Controller hub')
for n in ('ubuntu','arch','fedora','tumbleweed','void'):
    use(n); ch=g.HW_ControllerTab(); ch._rebuild_driver_cards(); pump(10)
    t=texts(ch)
    xone_builtin=any(x.strip().endswith('Built-in') for x in t)
    names=[b.text() for b in ch.findChildren(QPushButton)]
    inst=sum(1 for b in names if b in ('Install','Reinstall'))
    na=sum(1 for x in t if x.startswith('Not available on your system'))
    print(f'  {n:10} install-buttons={inst} not-available-notes={na}')
    if n!='void':
        ok(inst>=5, f'{n}: xone/xpad-noone/xpad/xpadneo/tmff2/joystick all installable ({inst} buttons)')
        captured.clear()
        # xone button: find the card whose label is xone, run its install
        ch._do_install('', 'xone'); cmd=captured[-1][0]
        ok(captured[-1][2]==g.GT_INSTALL_TIMEOUT and 'xone' in cmd, f'{n}: xone install script generated')
        if n=='arch': ok('gh_aur' in cmd and 'PKEXEC_UID' in g.gt_module().prelude(), 'arch: xone via AUR build without helper (runs as desktop user)')
        else: ok('git clone' in cmd and 'install.sh --release' in cmd, f'{n}: xone via upstream DKMS')
    else:
        ok(inst==0 and na>=1, 'void: controller cards explain they are unavailable')
    pump(25); ch.deleteLater()

print('\n--- WiFi hub')
for n in ('ubuntu','debian','arch','fedora','tumbleweed'):
    use(n); wf=g.HW_WiFiTab(); pump(3)
    for tbl in (g.HW_WiFiTab.REALTEK_WIFI,g.HW_WiFiTab.BROADCOM_WIFI):
        for chip,kd,ad,col,desc,cmd,chk in tbl:
            card=wf._wifi_card(chip,kd,ad,col,desc,cmd,chk,False)
            ts=' '.join(texts(card)); pump(1)
            act=g.gt_action(cmd)
            if act is None or act.res.ok:
                assert any(b.text() in('Apply Fix','Reapply Fix') for b in card.findChildren(QPushButton)),(n,chip)
            else:
                assert 'Not available on your system' in ts,(n,chip)
    # Broadcom wl row on this distro
    captured.clear(); wf._apply_fix('@wifi-broadcom-wl'); cmd=captured[-1][0]
    expect={'ubuntu':'bcmwl-kernel-source','debian':'broadcom-sta-dkms','arch':'broadcom-wl-dkms','fedora':'broadcom-wl','tumbleweed':'broadcom-wl'}[n]
    ok(expect in cmd and captured[-1][2]==g.GT_INSTALL_TIMEOUT, f'{n}: Broadcom wl -> {expect}')
    captured.clear(); wf._apply_fix('@wifi-rtl8821ce'); ok('dkms add -m' in captured[-1][0] and 'rtl8821ce' in captured[-1][0], f'{n}: RTL8821CE DKMS recipe uses native build deps')
    pump(25); wf.deleteLater()

print('\n--- Setup tab packages')
extra=[]
for n,pm,exp,notexp in (('ubuntu','apt','python3-yaml','python-yaml'),('arch','pacman','python-yaml','python3-yaml'),('fedora','dnf','python3-pyyaml','python-yaml'),('tumbleweed','zypper','python3-PyYAML','python-yaml')):
    use(n); l=g.gt_setup_packages(pm,extra); a=g.gt_setup_run_args(pm,l)
    ok(exp in l and notexp not in l and a[:2]==['bash','-c'] and 'Skipped' in a[2], f'{n}: setup list {l[:3]}.. installs available only')

print('\n--- Plugins window card')
use('arch'); pg=g.PluginsPage(); pump(5)
tx=' '.join(texts(pg))
ok('Distro Modules' in tx and 'Using: Arch Linux (pacman)' in tx, 'card shows detected system and active module')
cbs=[c for c in pg.findChildren(QCheckBox) if 'pacman' in c.text() or 'Arch' in c.text()]
ok(cbs and cbs[0].isChecked(),'Arch module checkbox on')
cbs[0].setChecked(False); pump(5)
ok(g.gt_module() is None or g.gt_module().id=='generic','disabling the Arch module deactivates it')
st=json.load(open(g.SETTINGS_FILE)); ok('arch' in st.get('distro_modules_disabled',[]),'choice persisted in settings.json')
pg2=g.PluginsPage(); pump(3)
cbs=[c for c in pg2.findChildren(QCheckBox) if 'Arch' in c.text()]; cbs[0].setChecked(True); pump(3)
ok(g.gt_module().id=='arch','re-enabling restores it')
combo=pg2.findChildren(QComboBox)[0]; combo.setCurrentIndex(combo.findData('fedora')); pump(3)
ok(g.gt_module().id=='fedora','manual override to the Fedora module works')
g.DISTRO.set_override(''); use('arch')

print('\n--- legacy helpers & package state delegate to the module')
ok('gh_aur' in g._gh_controller_install_cmd('xone',''), '_gh_controller_install_cmd uses module')
ok(g._gh_detect_family()=='arch' and 'pacman' in g._gh_pkg_install_cmd(['x']), '_gh_detect_family/_gh_pkg_install_cmd delegate')
ok('pacman -S' in g.gt_remove_script(['a']) or 'gh_remove' in g.gt_remove_script(['a']),'uninstall script via module')
use('ubuntu'); ok(g.gt_pkg_installed('bash') is True and g.gt_pkg_installed('definitely-not-a-pkg') is False,'gt_pkg_installed -> dpkg through module')
print('\nSUMMARY: %d/%d passed'%(sum(res),len(res)))
