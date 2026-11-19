"""Prefix initialization, winetricks, registry, DPI, repair for Griffin Game Tweaks."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

from griffin.config import AppConfig
from griffin.deps import (
    SOFT_FAIL_VERBS,
    ensure_winetricks,
    install_standard_deps,
    run_winetricks,
    verify_prefix_runtimes,
    winetricks_env,
)
from griffin.installers.task import TaskContext
from griffin.logging_utils import log_debug, log_install, log_warning
from griffin.steam.paths import find_steam_path
from griffin.steam.proton import SteamProton
from griffin.utils import clean_env, windows_path

VALID_WINDOWS_VERSIONS = ("win11", "win10", "win81", "win8", "win7")


def _reg_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def build_wine_registry(dpi: int = 0, exe_path: str = "") -> str:
    """Return the .reg text applied to every prefix.

    No global DLL overrides are written: Proton (DXVK/vkd3d-proton) and winetricks set
    the overrides they need, and forcing ``native,builtin`` on dxgi/d3d12/wininet/… can
    fight them. DPI scaling is only written when ``dpi`` > 0.
    """
    lines = [
        "Windows Registry Editor Version 5.00",
        "",
        "[HKEY_CURRENT_USER\\Software\\Wine]",
        '"ShowDotFiles"="Y"',
        "",
        "[HKEY_CURRENT_USER\\Control Panel\\Desktop]",
        '"FontSmoothing"="2"',
        '"FontSmoothingType"=dword:00000002',
    ]
    if dpi and dpi > 0:
        lines.append(f'"LogPixels"=dword:{int(dpi):08x}')
        if exe_path:
            # The compat flag is keyed by the *exe path* (value name), not the default value.
            lines += [
                "",
                "[HKEY_CURRENT_USER\\Software\\Microsoft\\Windows NT\\CurrentVersion\\AppCompatFlags\\Layers]",
                f'"{_reg_escape(windows_path(exe_path))}"="~ HIGHDPIAWARE"',
            ]
    return "\r\n".join(lines) + "\r\n"


# Kept for API compatibility (older code imported this constant).
WINE_SETTINGS_REG = build_wine_registry()


def _wine_env(prefix: Path) -> dict:
    env = clean_env()
    env["WINEPREFIX"] = str(prefix)
    env["WINEDEBUG"] = "-all"
    env["PROTON_USE_XALIA"] = "0"
    return env


def initialize_prefix(prefix_root: Path, proton: SteamProton, app_id: int, ctx: TaskContext) -> None:
    """Create/refresh a Proton prefix. ``prefix_root`` must be ``<compat data dir>/pfx``."""
    proton_script = proton.proton_script
    if not proton_script.exists():
        raise RuntimeError(f"Proton wrapper missing: {proton_script}")
    steam = find_steam_path()
    if not steam:
        raise RuntimeError("Steam not found")
    prefix_root = Path(prefix_root)
    compat = prefix_root.parent
    compat.mkdir(parents=True, exist_ok=True)
    env = clean_env()
    env.update({
        "STEAM_COMPAT_CLIENT_INSTALL_PATH": str(steam),
        "STEAM_COMPAT_DATA_PATH": str(compat),
        "SteamAppId": str(app_id),
        "SteamGameId": str(app_id),
        "DISPLAY": "",
        "WAYLAND_DISPLAY": "",
        "WINEDEBUG": "-all",
        "WINEDLLOVERRIDES": "msdia80.dll=n;conhost.exe=d;cmd.exe=d",
    })
    log_install(f"Initializing prefix with proton: {proton_script}")
    kill_wineserver(prefix_root, proton)
    ret = ctx.run_cancellable([str(proton_script), "run", "wineboot", "-u"], env=env, timeout=600)
    has_registry = (prefix_root / "system.reg").is_file()
    if ret != 0 and not has_registry:
        raise RuntimeError(f"proton wineboot failed (exit {ret}) and no prefix was created")
    if ret != 0:
        log_warning(f"proton wineboot exited with {ret}, but the prefix exists — continuing")
    time.sleep(2)
    if not prefix_root.exists():
        raise RuntimeError("Prefix not created after wineboot")


def apply_wine_registry(
    prefix: Path,
    proton: SteamProton,
    log_cb: Callable[[str], None],
    *,
    dpi: int = 0,
    exe_path: str = "",
) -> None:
    wine = proton.wine_binary()
    if not wine:
        raise RuntimeError("Wine binary not found")
    tmp = AppConfig.tmp_path()
    tmp.mkdir(parents=True, exist_ok=True)
    reg = tmp / "wine_settings.reg"
    reg.write_text(build_wine_registry(dpi, exe_path), encoding="utf-8")
    env = _wine_env(prefix)
    env["WINEDLLOVERRIDES"] = "mshtml=d"
    log_cb("Applying Wine registry settings…")
    try:
        subprocess.run(
            [str(wine), "regedit", str(reg)],
            env=env, check=False, timeout=90,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        log_install("Wine registry settings applied")
    except subprocess.TimeoutExpired:
        log_warning("regedit timed out; registry tweaks may not have been applied")
    finally:
        reg.unlink(missing_ok=True)


def set_windows_version(
    prefix: Path,
    proton: SteamProton,
    ctx: TaskContext,
    version: str = "win11",
) -> None:
    """version: win11 | win10 | win81 | win8 | win7"""
    version = (version or "win11").lower().strip()
    if version not in VALID_WINDOWS_VERSIONS:
        version = "win11"
    wt = ensure_winetricks()
    env = winetricks_env(prefix, proton)
    ctx.set_status(f"Setting Windows version → {version}")
    ret = ctx.run_cancellable([str(wt), "-q", version], env=env, timeout=300)
    if ret != 0:
        raise RuntimeError(f"winetricks {version} failed: {ret}")


def apply_dpi(prefix: Path, proton: SteamProton, dpi: int = 120) -> None:
    wine = proton.wine_binary()
    if not wine:
        raise RuntimeError("Wine not found")
    subprocess.run(
        [str(wine), "reg", "add", r"HKCU\Control Panel\Desktop",
         "/v", "LogPixels", "/t", "REG_DWORD", "/d", str(dpi), "/f"],
        env=_wine_env(prefix), check=False, timeout=30,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    log_install(f"DPI {dpi} applied")


def kill_wineserver(prefix: Path, proton: SteamProton) -> None:
    ws = proton.wineserver_binary()
    if not ws:
        return
    try:
        subprocess.run(
            [str(ws), "-k"], env=_wine_env(prefix), check=False, timeout=30,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log_debug(f"wineserver -k: {e}")


def _soft_fail_note(failed: list[str]) -> Optional[str]:
    if failed and all(v in SOFT_FAIL_VERBS for v in failed):
        return (
            "Note: older VC++ / d3dx9 verbs failed, but key runtimes are present "
            "(vcrun2022 usually covers 2015–2022). Safe to ignore."
        )
    return None


def install_all_dependencies(
    prefix_root: Path,
    proton: SteamProton,
    ctx: TaskContext,
    start_progress: float = 0.2,
    end_progress: float = 0.9,
    app_id: int = 0,
    extra_verbs: Optional[list[str]] = None,
    windows_version: str = "win11",
    *,
    verb_profile: str = "standard",
    dpi: int = 0,
    exe_path: str = "",
) -> list[str]:
    """Returns list of failed winetricks verbs (may be empty)."""
    AppConfig.tmp_path().mkdir(parents=True, exist_ok=True)
    span = end_progress - start_progress
    ctx.set_status("Setting up Windows compatibility layer…")
    # A prefix that could not be created is fatal — carrying on would only produce
    # a confusing pile of winetricks errors.
    initialize_prefix(prefix_root, proton, app_id, ctx)
    ctx.set_progress(start_progress + span * 0.12)
    if ctx.is_cancelled():
        raise RuntimeError("Cancelled")

    ctx.set_status(f"Installing Windows components ({verb_profile} set — this can take several minutes)…")

    def log_cb(m: str) -> None:
        ctx.log(m)

    failed: list[str] = []
    try:
        failed = install_standard_deps(
            prefix_root, proton, log_cb, ctx.is_cancelled,
            extra_verbs=extra_verbs, profile=verb_profile,
        )
    except RuntimeError as e:
        if "Cancelled" in str(e):
            raise
        log_warning(f"Winetricks issues: {e}")
        ctx.log(f"Warning: {e}")
    ctx.set_progress(start_progress + span * 0.70)
    if ctx.is_cancelled():
        raise RuntimeError("Cancelled")

    ctx.set_status("Configuring Windows registry…")
    apply_wine_registry(prefix_root, proton, ctx.log, dpi=dpi, exe_path=exe_path)
    try:
        set_windows_version(prefix_root, proton, ctx, windows_version)
    except RuntimeError as e:
        if "Cancelled" in str(e):
            raise
        log_warning(f"windows version ({windows_version}): {e}")

    found, missing = verify_prefix_runtimes(prefix_root)
    if missing:
        msg = (
            f"Prefix verification: {len(found)} key DLLs present, "
            f"{len(missing)} missing: {', '.join(Path(m).name for m in missing)}"
        )
        log_warning(msg)
        ctx.log(msg)
    else:
        ctx.log(f"Prefix verification OK ({len(found)} key DLLs present)")
        note = _soft_fail_note(failed)
        if note:
            ctx.log(note)

    kill_wineserver(prefix_root, proton)
    ctx.set_progress(end_progress)
    ctx.set_status("Dependencies installed")
    return failed


def repair_prefix(
    prefix_root: Path,
    proton: SteamProton,
    ctx: TaskContext,
    *,
    windows_version: str = "win11",
    reinstall_verbs: bool = False,
    extra_verbs: Optional[list[str]] = None,
    verb_profile: str = "standard",
    dpi: int = 0,
    exe_path: str = "",
) -> list[str]:
    """
    Re-apply registry/DPI/Windows version. Optionally re-run winetricks.
    Returns failed verbs if reinstall_verbs else [].
    """
    prefix_root = Path(prefix_root)
    if not prefix_root.is_dir():
        raise RuntimeError(f"Prefix not found: {prefix_root}")

    ctx.set_status("Repairing prefix…")
    kill_wineserver(prefix_root, proton)

    failed: list[str] = []
    try:
        if reinstall_verbs:
            ctx.set_status("Re-running winetricks…")
            failed = install_standard_deps(
                prefix_root, proton, ctx.log, ctx.is_cancelled,
                extra_verbs=extra_verbs, profile=verb_profile,
            )

        ctx.set_status("Re-applying registry…")
        apply_wine_registry(prefix_root, proton, ctx.log, dpi=dpi, exe_path=exe_path)
        try:
            set_windows_version(prefix_root, proton, ctx, windows_version)
        except RuntimeError as e:
            if "Cancelled" in str(e):
                raise
            log_warning(f"windows version: {e}")

        found, missing = verify_prefix_runtimes(prefix_root)
        if missing:
            ctx.log(f"Still missing after repair: {', '.join(Path(m).name for m in missing)}")
        else:
            ctx.log(f"Verification OK ({len(found)} key DLLs)")
    finally:
        kill_wineserver(prefix_root, proton)
    ctx.set_status("Repair complete")
    return failed


def rerun_winetricks(
    prefix_root: Path,
    proton: SteamProton,
    ctx: TaskContext,
    verbs: Optional[list[str]] = None,
    *,
    verb_profile: str = "standard",
    extra_verbs: Optional[list[str]] = None,
) -> list[str]:
    """Re-run selected (or the profile's) winetricks verbs on an existing prefix."""
    prefix_root = Path(prefix_root)
    if not prefix_root.is_dir():
        raise RuntimeError(f"Prefix not found: {prefix_root}")
    kill_wineserver(prefix_root, proton)
    try:
        if verbs:
            failed = run_winetricks(
                prefix_root, proton, verbs, ctx.log, ctx.is_cancelled, per_verb=True
            )
        else:
            failed = install_standard_deps(
                prefix_root, proton, ctx.log, ctx.is_cancelled,
                extra_verbs=extra_verbs, profile=verb_profile,
            )
    finally:
        kill_wineserver(prefix_root, proton)
    return failed
