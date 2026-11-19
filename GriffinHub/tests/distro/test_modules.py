import sys, importlib.util, subprocess, os, re
ROOT=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
D=sys.argv[1] if len(sys.argv)>1 else os.path.join(ROOT,'griffinhub','distro_modules')
def load(n):
    s=importlib.util.spec_from_file_location(n,f'{D}/{n}.py'); m=importlib.util.module_from_spec(s); sys.modules[n]=m; s.loader.exec_module(m); return m
api=load('_api'); sys.modules['gh_distro_api']=api
mods={n:load(n).Module for n in ('debian','arch','fedora','suse')}
samples={
 'ubuntu24':('debian','ID=ubuntu\nID_LIKE=debian\nVERSION_ID="24.04"\n'),'debian13':('debian','ID=debian\nVERSION_ID="13"\n'),
 'mint':('debian','ID=linuxmint\nID_LIKE="ubuntu debian"\nVERSION_ID="22"\n'),
 'arch':('arch','ID=arch\n'),'cachyos':('arch','ID=cachyos\nID_LIKE=arch\n'),
 'fedora42':('fedora','ID=fedora\nVERSION_ID=42\n'),'rocky9':('fedora','ID="rocky"\nID_LIKE="rhel centos fedora"\nVERSION_ID="9.4"\n'),
 'tumbleweed':('suse','ID="opensuse-tumbleweed"\nID_LIKE="opensuse suse"\n'),'leap156':('suse','ID="opensuse-leap"\nVERSION_ID="15.6"\n'),
}
bad=0; total=0; unsup={}
for sname,(mn,txt) in samples.items():
    m=mods[mn](api.parse_os_release(txt))
    for k in api.FEATURE_KEYS:
        r=m.resolve(k); total+=1
        if not isinstance(r,api.Resolved): print('FAIL not Resolved',sname,k); bad+=1; continue
        if not r.ok:
            if not r.reason: print('FAIL unsupported without reason',sname,k); bad+=1
            unsup.setdefault(sname,[]).append(k); continue
        if not r.install: print('FAIL ok but no install script',sname,k); bad+=1; continue
        for field in ('install','remove'):
            sc=getattr(r,field)
            if not sc: continue
            b=subprocess.run(['bash','-n','-c',sc],capture_output=True,text=True)
            if b.returncode: print('FAIL bash -n',sname,k,field,b.stderr[:160]); bad+=1
            if re.search(r'\{[A-Z_]+\}|\bNone\b|<function|\$\{\w+:\?\}',sc): print('FAIL placeholder leftovers',sname,k,field); bad+=1
        if not (r.check_pkgs or r.check_any or r.check_cmd) and k in ('nvidia-580','nvidia-open','joystick','xone','xpadneo','wifi-broadcom-wl'):
            print('WARN no check for',sname,k)
    # package manager functions present in prelude
    for fn in ('gh_avail','gh_install','gh_remove','gh_pick','gh_initramfs'):
        if fn not in m.prelude(): print('FAIL prelude missing',sname,fn); bad+=1
print(f'\n{total} feature resolutions across {len(samples)} systems, {bad} problems')
for s,k in unsup.items(): print(f'  unsupported on {s:11}:',', '.join(k))
