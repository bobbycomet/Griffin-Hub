#!/usr/bin/env python3
"""Prune a pip-installed PyQt6 + standalone Python down to what Griffin Hub needs.
usage: prune_qt.py <python-root>   (keeps QtCore/QtGui/QtWidgets/QtDBus/QtSvg + their real dependency closure)"""
import os, re, shutil, subprocess, sys
from pathlib import Path
root = Path(sys.argv[1]).resolve()
sp = next(root.glob("lib/python3*/site-packages"))
pq = sp / "PyQt6"; qt = pq / "Qt6"; qlib = qt / "lib"; plug = qt / "plugins"
KEEP_PY = {"sip", "QtCore", "QtGui", "QtWidgets", "QtDBus", "QtSvg", "QtSvgWidgets"}
KEEP_PLUGINS = {"platforms", "platformthemes", "platforminputcontexts", "imageformats", "iconengines",
                "xcbglintegrations", "wayland-decoration-client", "wayland-graphics-integration-client",
                "wayland-shell-integration", "egldeviceintegrations", "generic", "styles"}
def needed(p):
    out = subprocess.run(["readelf", "-d", str(p)], capture_output=True, text=True).stdout
    return re.findall(r"\(NEEDED\)\s+Shared library: \[([^\]]+)\]", out)
# drop plugins Griffin Hub never uses (PDF image format pulls in the whole Qt PDF stack)
for rel, keep in (("platforms", {"libqxcb.so", "libqwayland.so", "libqoffscreen.so", "libqminimal.so"}),
                  ("imageformats", None)):
    d = plug / rel
    if d.exists():
        for f in d.glob("*.so"):
            if (keep is not None and f.name not in keep) or (keep is None and f.name == "libqpdf.so"):
                f.unlink()
# seeds: kept PyQt6 extension modules + every kept plugin
seeds = []
for p in pq.glob("*.so"):
    if p.name.split(".")[0] in KEEP_PY: seeds.append(p)
for d in plug.iterdir() if plug.exists() else []:
    if d.name in KEEP_PLUGINS: seeds += list(d.rglob("*.so*"))
libs = {p.name: p for p in qlib.glob("*.so*")}
keep, todo = set(), list(seeds)
while todo:
    f = todo.pop()
    for n in needed(f):
        if n in libs and n not in keep:
            keep.add(n); todo.append(libs[n])
removed = 0
def rm(p):
    global removed
    removed += p.stat().st_size if p.is_file() else sum(x.stat().st_size for x in p.rglob("*") if x.is_file())
    shutil.rmtree(p) if p.is_dir() else p.unlink()
for name, p in libs.items():
    if name not in keep and p.exists(): rm(p)
for p in pq.glob("*.so"):
    if p.name.split(".")[0] not in KEEP_PY: rm(p)
for p in list(pq.glob("*.pyi")) + list(pq.glob("py.typed")):
    rm(p)
for d in ("bindings", "uic", "lupdate"):
    if (pq / d).exists(): rm(pq / d)
for d in ("qml", "translations", "metatypes", "libexec", "resources"):
    if (qt / d).exists() and d != "resources": rm(qt / d)
for d in (plug.iterdir() if plug.exists() else []):
    if d.name not in KEEP_PLUGINS: rm(d)
# Python itself: tests, tk, docs, static libs, pip
lp = next(root.glob("lib/python3*"))
for rel in ("test", "idlelib", "tkinter", "turtledemo", "lib2to3", "ensurepip", "site-packages/pip",
            "site-packages/setuptools", "site-packages/pkg_resources", "site-packages/_distutils_hack"):
    if (lp / rel).exists(): rm(lp / rel)
for pat in ("config-*", "lib-dynload/_tkinter*", "lib-dynload/_test*", "lib-dynload/xxlimited*"):
    for p in lp.glob(pat): rm(p)
for rel in ("include", "share", "lib/tcl8.6", "lib/tk8.6", "lib/itcl4.2.4", "lib/tcl8", "lib/thread2.8.9", "lib/libpython3.12.a"):
    p = root / rel
    if p.exists(): rm(p)
for p in list(root.glob("lib/tcl*")) + list(root.glob("lib/tk*")) + list(root.glob("lib/itcl*")) + list(root.glob("lib/thread*")) + list(root.glob("lib/*.a")):
    if p.exists(): rm(p)
for b in root.glob("bin/*"):
    if b.name not in ("python3", "python3.12", "python"): rm(b)
for pc in root.rglob("__pycache__"): shutil.rmtree(pc, ignore_errors=True)
print("kept Qt libs:", len(keep), "| removed %.0f MB" % (removed / 1e6))
