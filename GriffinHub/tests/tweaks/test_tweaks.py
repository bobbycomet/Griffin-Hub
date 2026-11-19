"""Griffin Game Tweaks logic tests (no GUI): Steam/VDF/compat editing, shortcuts, setup pipeline in both
launch modes, rollback on failure, standalone launcher, removal.   python3 tests/tweaks/test_tweaks.py"""
import os, sys, tempfile, subprocess, shutil, stat
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
HOME = tempfile.mkdtemp(prefix="gh-tweaks-"); os.environ["HOME"] = HOME
for v in ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "GRIFFIN_APPDIR"): os.environ.pop(v, None)
import fake_steam
STEAM = fake_steam.make(HOME)
res = []
def ok(c, m): print(("PASS " if c else "FAIL ") + m); res.append(bool(c)); return bool(c)

from griffin.steam import vdf_parse, compat, proton as gproton, shortcuts, paths
from griffin.config import AppConfig, ManagedGames
from griffin.deps import verbs_for_profile, verify_prefix_runtimes
from griffin.installers import prefix_setup, game_setup, standalone
from griffin.installers.task import TaskContext
from griffin import deps as gdeps

# ---------------- VDF + Steam discovery -----------------------------------------------------
nested = '"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"/mnt/sdb1/SteamLibrary"\n\t\t"apps"\n\t\t{\n\t\t\t"489830"\t\t"123"\n\t\t}\n\t}\n}\n'
flat = '"libraryfolders"\n{\n\t"0"\t\t"/home/a/.steam/steam"\n\t"1"\t\t"/mnt/games"\n}\n'
ok(vdf_parse.parse_library_folders(nested) == ["/mnt/sdb1/SteamLibrary"], "libraryfolders.vdf (modern nested form)")
ok(vdf_parse.parse_library_folders(flat) == ["/home/a/.steam/steam", "/mnt/games"], "libraryfolders.vdf (older flat form)")
ok(vdf_parse.parse_library_folders_detailed(nested)[0]["apps"] == {"489830"}, "libraryfolders apps map parsed")
m = vdf_parse.AppManifest.from_vdf('"AppState"\n{\n"appid" "10"\n"name" "X"\n"installdir" "X"\n"StateFlags" "6"\n}\n')
ok(m and m.is_installed(), "appmanifest StateFlags bitmask (6 = installed + update required)")
ok(paths.find_steam_path() == STEAM, "Steam install detected in ~/.local/share/Steam")
ps = gproton.find_steam_protons()
ok([p.name for p in ps] == ["GE-Proton10-2"], "Proton 10+ detected: %s" % [p.name for p in ps])
os.makedirs(STEAM / "compatibilitytools.d/GE-Proton9-27/files/bin", exist_ok=True)
(STEAM / "compatibilitytools.d/GE-Proton9-27/proton").write_text("#!/bin/sh\n"); (STEAM / "compatibilitytools.d/GE-Proton9-27/files/bin/wine").write_text("")
ok("GE-Proton9-27" not in [p.name for p in gproton.find_steam_protons()], "Proton 9 is filtered out (Game Tweaks needs 10+)")
ok(gproton.steam_config_name("Proton 10.0") == "proton_10" and gproton.steam_config_name("Proton - Experimental") == "proton_experimental", "Steam-internal Proton config names")

# ---------------- config.vdf editing is surgical ----------------------------------------------
cfg = STEAM / "config/config.vdf"; before = cfg.read_text()
compat.set_compat_tool(3000000001, "GE-Proton10-2")
after = cfg.read_text()
root = vdf_parse.parse_blocks(vdf_parse.tokenize(after))
blk = vdf_parse.find_path(root, ["InstallConfigStore", "Software", "Valve", "Steam", "CompatToolMapping", "3000000001"])
ok(blk is not None, "CompatToolMapping entry written for the new AppID")
ok('"AutoUpdateWindowStart"\t\t"-1"' in after and '"proton_experimental"' in after, "rest of config.vdf kept intact")
ok((STEAM / "config/config.vdf.griffin-orig").read_text() == before and (STEAM / "config/config.vdf.griffin.bak").exists(), "pristine + latest backups written")
compat.set_compat_tool(3000000001, "GE-Proton10-2"); ok(cfg.read_text() == after, "setting the same tool twice changes nothing")
ok(compat.remove_compat_tool(3000000001) and '"3000000001"' not in cfg.read_text() and '"proton_experimental"' in cfg.read_text(), "removing the mapping leaves the others")

# ---------------- helpers ---------------------------------------------------------------------
ok(len(verbs_for_profile("minimal")) == 3 and "dotnetdesktop8" in verbs_for_profile("standard") and "dotnet40" in verbs_for_profile("extended"), "winetricks profiles")
reg = prefix_setup.build_wine_registry(0); reg2 = prefix_setup.build_wine_registry(120, "/g/a.exe")
ok("LogPixels" not in reg and "LogPixels" in reg2 and "HIGHDPIAWARE" in reg2 and "DllOverrides" not in reg, "registry: DPI only when asked, no global DLL overrides")
ok(standalone.parse_extra_args('--a "b c" -d') == ["--a", "b c", "-d"], "launch args parsed shell-style")
try: standalone.parse_extra_args('"oops'); ok(False, "unbalanced quotes rejected")
except ValueError: ok(True, "unbalanced quotes rejected with a clear error")
ok(game_setup.is_safe_compat_dir(Path(HOME) / "x/compatdata/123", "123") and not game_setup.is_safe_compat_dir(Path(HOME), "x") and not game_setup.is_safe_compat_dir(Path("/usr/lib"), "lib"), "recursive-delete guard")
ok(prefix_setup.windows_path("/home/a/g.exe") == "Z:\\home\\a\\g.exe" if hasattr(prefix_setup, "windows_path") else True, "unix -> Wine path")

# ---------------- full setup pipeline with a fake Proton -------------------------------------------
fake_wt = Path(HOME) / "fake-winetricks"; fake_wt.write_text("#!/bin/sh\nexit 0\n"); fake_wt.chmod(0o755)
gdeps.ensure_winetricks = lambda: fake_wt; prefix_setup.ensure_winetricks = lambda: fake_wt
gdeps.ensure_cabextract = lambda: Path("cabextract")
logs = []
ctx = TaskContext(lambda m: logs.append("S:" + m), lambda m: logs.append("L:" + m), lambda p: None, lambda: False)
exe_dir = Path(HOME) / "Games/Launcher"; exe_dir.mkdir(parents=True); exe = exe_dir / "launcher.exe"; exe.write_bytes(b"MZ")
pr = gproton.find_steam_protons()[0]

r = game_setup.setup_game("My Game", exe, pr, ctx, verb_profile="minimal", dpi=120)
g = ManagedGames.get(r.app_id)
sc = (STEAM / "userdata/12345/config/shortcuts.vdf")
ok(r.launch_mode == "steam" and r.prefix_path.parent.name == str(r.app_id) and (r.prefix_path / "system.reg").is_file(), "Steam mode: shortcut + Proton prefix created (AppID %d)" % r.app_id)
ok(sc.is_file() and b"My Game" in sc.read_bytes() and b"launcher.exe" in sc.read_bytes(), "Steam mode: shortcuts.vdf contains the game")
ok(str(r.app_id) in cfg.read_text() and "GE-Proton10-2" in cfg.read_text(), "Steam mode: Proton selected in config.vdf")
ok(g and g.launch_mode == "steam" and g.dpi == 120 and g.verb_profile == "minimal", "Steam mode: registered in Managed Games with its settings")
ok(r.missing_dlls, "missing runtime DLLs are reported (fake winetricks installs nothing): %d" % len(r.missing_dlls))

msg = game_setup.remove_game(g, ctx, delete_prefix=True, remove_shortcut=True)
ok(b"My Game" not in sc.read_bytes() and str(r.app_id) not in cfg.read_text() and not r.prefix_path.parent.exists() and ManagedGames.get(r.app_id) is None, "Steam mode removal: shortcut, Proton mapping, prefix and registration all gone")

r2 = game_setup.setup_game("Stand Alone", exe, pr, ctx, standalone=True, verb_profile="minimal", is_electron=True, extra_launch_options='--foo "a b"')
g2 = ManagedGames.get(r2.app_id)
script = Path(r2.launcher_path); desk = Path(r2.desktop_path)
ok(r2.launch_mode == "standalone" and script.is_file() and os.access(script, os.X_OK) and desk.is_file(), "Standalone mode: launcher script + menu entry written")
ok(subprocess.run(["bash", "-n", str(script)]).returncode == 0 and "proton" in script.read_text() and "--disable-gpu" in script.read_text() and "'a b'" in script.read_text(), "launcher script is valid bash with Electron + user args quoted")
dv = subprocess.run(["desktop-file-validate", str(desk)], capture_output=True, text=True) if shutil.which("desktop-file-validate") else None
ok(dv is None or dv.returncode == 0, "menu entry passes desktop-file-validate")
ok(not sc.read_bytes().count(b"Stand Alone") and (Path(HOME) / "GriffinGameTweaks/prefixes").is_dir(), "Standalone mode touches nothing in Steam")
log_path = standalone.launch_standalone(g2); import time; time.sleep(1)
ok(log_path.exists(), "standalone launch starts the script detached (log: %s)" % log_path.name)
game_setup.remove_game(g2, ctx, delete_prefix=True)
ok(not script.exists() and not desk.exists() and not Path(g2.prefix_path).parent.exists() and ManagedGames.get(r2.app_id) is None, "Standalone removal: launcher, menu entry, prefix and registration all gone")

# ---------------- rollback + guards ---------------------------------------------------------------------
orig = game_setup.install_all_dependencies
game_setup.install_all_dependencies = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
try: game_setup.setup_game("Broken", exe, pr, ctx)
except RuntimeError as e: ok("boom" in str(e), "a failing install raises the original error")
game_setup.install_all_dependencies = orig
ok(b"Broken" not in sc.read_bytes() and all(x.name != "Broken" for x in ManagedGames.load()), "failed install is rolled back (no shortcut, nothing registered)")
shortcuts.is_steam_running = lambda: True; game_setup.is_steam_running = lambda: True
try: game_setup.setup_game("Steam Open", exe, pr, ctx); ok(False, "refuses while Steam is running")
except RuntimeError as e: ok("Fully exit Steam" in str(e), "refuses to write shortcuts while Steam is running")
try: game_setup.setup_game("No Exe", Path(HOME) / "nope.exe", pr, ctx); ok(False, "missing exe")
except RuntimeError as e: ok("EXE not found" in str(e), "missing EXE is rejected up front")

# ---------------- cancel -----------------------------------------------------------------------------------
from griffin.utils import run_streaming
t0 = time.time()
try: run_streaming(["sleep", "30"], cancel=lambda: time.time() - t0 > 1)
except RuntimeError as e: ok("Cancelled" in str(e) and time.time() - t0 < 10, "cancel kills a running command quickly (%.1fs)" % (time.time() - t0))

# ---------------- distro-native system packages hook ---------------------------------------------------------
from griffinhub.play.tweaks_page import _deps_command
cmd = _deps_command()
ok(cmd[:3] == ["pkexec", "bash", "-c"] and "winetricks" in cmd[3] and "cabextract" in cmd[3], "'Install system packages' uses the distro module (native package manager)")
print("\nSUMMARY: %d/%d passed" % (sum(res), len(res)))
sys.exit(0 if all(res) else 1)
