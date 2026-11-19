"""Headless integration smoke test: python3 tests/smoke_gui.py  (needs PyQt6; uses an isolated HOME)."""
import os, sys, tempfile, importlib.util, time
ROOT = os.environ.get("GH_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["HOME"] = tempfile.mkdtemp(prefix="gh-smoke-")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests")) if os.path.isdir(os.path.join(ROOT, "tests")) else None
ok_all = True
def check(c, m):
    global ok_all; ok_all &= bool(c); print(("PASS " if c else "FAIL ") + m)

try:
    import fake_steam; fake_steam.make(os.environ["HOME"])          # Steam + one Proton, for the Game Tweaks tab
except ImportError:
    pass
spec = importlib.util.spec_from_file_location("griffinhub_main", os.path.join(ROOT, "griffinhub.py"))
g = importlib.util.module_from_spec(spec); sys.modules["griffinhub_main"] = g; spec.loader.exec_module(g)
from PyQt6.QtWidgets import QApplication, QLabel
app = QApplication.instance() or QApplication([])
from PyQt6.QtWidgets import QMessageBox
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.No)   # Play first-run dialog

# bundled helper files the installers look for
for parts in (("process-sentry", "sentryv3.py"), ("process-sentry", "resource-sentry.service"),
              ("process-sentry", "resource-sentry.openrc"), ("process-sentry", "runit", "run"),
              ("process-sentry", "runit", "finish"), ("kernel-autotune", "kernel-autotune"),
              ("kernel-autotune", "zram-impl.sh")):
    p = g.gt_bundled_file(*parts)
    check(p is not None and p.is_file(), "bundled file found: " + "/".join(parts))
check(g.find_app_icon() is not None, "app icon found: %s" % g.find_app_icon())
check(g.DISTRO is not None and not g.DISTRO.errors and len(g.DISTRO.modules) == 5,
      "distro modules loaded from package layout (%s)" % sorted(g.DISTRO.modules))
check(g.gt_module() is not None, "active distro module: %s" % (g.gt_module().name if g.gt_module() else None))

# package-mode hardware API (griffinhub.hardware.*) must agree with the app
from griffinhub.hardware import distro as hw_distro, controllers as hw_ctrl
d = hw_distro.detect()
check(d["family"] == g._gh_detect_family(), "hardware.distro.detect() family matches the app (%s)" % d["family"])
check(all(callable(v["install"]) or v["install"] in ("", None) for v in hw_ctrl.DRIVERS.values()), "hardware.controllers entries well-formed")
for key in ("xone", "xpadneo", "xpad-noone", "paroj/xpad", "hid-tmff2", "ps-controllers"):
    cmd = hw_ctrl.get_install_command(key)
    check(cmd and "sudo " not in cmd and "$HOME" not in cmd, "controllers.get_install_command(%s) -> native script via module" % key)

w = g.MainWindow(); w.show()
for _ in range(20): app.processEvents(); time.sleep(0.05)
names = [type(p).__name__ for p in w.pages]
print("pages:", names)
check(len(w.pages) == 6, "6 pages built")
play = w.pages[1]
check(type(play).__name__ == "GriffinPlayPage", "Griffin Play tab loaded (not the error fallback): %s" % type(play).__name__)
tx = " ".join(l.text() for l in w.findChildren(QLabel))
check("Griffin Play could not be loaded" not in tx, "no 'could not be loaded' message anywhere")
for _ in range(20): app.processEvents(); time.sleep(0.05)

# Griffin Play -> sub-tabs [Griffin Play | Game Tweaks]; Game Tweaks is built lazily on first open
tabs = [play.tabs.tabText(i) for i in range(play.tabs.count())]
check(tabs == ["Griffin Play", "Game Tweaks"], "Griffin Play has the sub-tabs %s" % tabs)
check(play.tweaks._win is None, "Game Tweaks is not built until its tab is opened (fast startup)")
play.show_tweaks()
for _ in range(30): app.processEvents(); time.sleep(0.05)
check(play.tweaks._win is not None, "Game Tweaks builds when its tab is opened")
from PyQt6.QtWidgets import QComboBox, QPushButton
combos = play.tweaks._win.findChildren(QComboBox)
protons = [c.itemText(i) for c in combos for i in range(c.count()) if "Proton" in c.itemText(i)]
check(any("GE-Proton10-2" in p for p in protons) or not os.path.isdir(os.path.join(ROOT, "tests")), "Game Tweaks lists the Proton builds from Steam: %s" % protons[:2])
check(play.tweaks._win.embedded and not play.tweaks._win.findChild(QLabel, "brand"), "embedded mode: no duplicate brand header")
check(g.find_app_logo() is not None and g.find_app_logo().name == "griffinhub-56.png", "sidebar uses the 56px griffin logo: %s" % g.find_app_logo())
check(g.find_app_icon() is not None and g.find_app_icon().name == "griffinhub.png", "window/desktop icon is griffinhub.png")
from PyQt6.QtGui import QImage
check(QImage(str(g.find_app_icon())).width() == 512, "icon is the 512px griffin image")
# System Tune: a fresh system must be able to install Kernel Autotune from the UI (regression test)
import inspect, pathlib
from PyQt6.QtWidgets import QTabWidget
st_tabs = [t for t in w.systune_pg.findChildren(QTabWidget)][0]
tab_names = [st_tabs.tabText(i) for i in range(st_tabs.count())]
check("Live Tuning" in tab_names and "Kernel Autotune" not in tab_names, "System Tune fallback: Live Tuning shown when Kernel Autotune is not installed: %s" % tab_names)
live_btns = [b for b in w.systune_pg.findChildren(QPushButton) if b.text().startswith("Install Kernel Autotune")]
check(live_btns and live_btns[0].text() == "Install Kernel Autotune v" + g.gt_kernel_autotune_version(),
      "first-time install button is on the Live Tuning tab and shows the bundled version: %s" % (live_btns[0].text() if live_btns else None))
check(g.gt_kernel_autotune_version().startswith("2.4"), "label version comes from the bundled script (%s), not a hard-coded string" % g.gt_kernel_autotune_version())
_fake_bin = pathlib.Path(os.environ["HOME"]) / "kernel-autotune"
def _fake_install():
    _fake_bin.write_text("#!/bin/sh\necho kernel-autotune 2.4.0\n"); _fake_bin.chmod(0o755)
    return True, ""
_orig_inst, _orig_bin = g.gt_install_kernel_autotune, g.GT_KERNEL_BIN
g.gt_install_kernel_autotune, g.GT_KERNEL_BIN = _fake_install, _fake_bin
live_btns[0].click()
for _ in range(20): app.processEvents(); time.sleep(0.05)
tab_names2 = [st_tabs.tabText(i) for i in range(st_tabs.count())]
check("Kernel Autotune" in tab_names2 and "Live Tuning" not in tab_names2, "after a successful install the Kernel Autotune tab replaces Live Tuning: %s" % tab_names2)
g.gt_install_kernel_autotune, g.GT_KERNEL_BIN = _orig_inst, _orig_bin
_src = open(os.path.join(ROOT, "griffinhub.py"), encoding="utf-8").read()
check("gametune-hub" not in _src and "Kernel Autotune v2.3" not in _src, "no stale 'gametune-hub' path or 'v2.3' label text left in the UI")
_pol = inspect.getsource(g._GtSetupTab._install_polkit)
check('isInGroup(\\"wheel\\")' in _pol and 'isInGroup(\\"sudo\\")' in _pol, "polkit rule covers sudo AND wheel admins (Fedora/Arch/openSUSE use wheel)")
w.close()
for _ in range(10): app.processEvents(); time.sleep(0.05)
# AppImage child-environment restore
os.environ.update({"GH_APPRUN": "1", "LD_LIBRARY_PATH": "/app/usr/lib", "GH_ORIG_LD_LIBRARY_PATH": "/opt/x",
                   "PYTHONPATH": "/app/usr/src", "PYTHONHOME": "/app/py", "GH_SET_SSL_CERT_FILE": "1", "SSL_CERT_FILE": "/x"})
g.gt_restore_child_env()
check(os.environ.get("LD_LIBRARY_PATH") == "/opt/x" and not any(k in os.environ for k in ("PYTHONPATH", "PYTHONHOME", "SSL_CERT_FILE", "GH_ORIG_LD_LIBRARY_PATH")),
      "AppImage env restore keeps bundled libs/python out of child processes")
print("\nRESULT:", "ALL PASS" if ok_all else "FAILURES")
sys.exit(0 if ok_all else 1)
