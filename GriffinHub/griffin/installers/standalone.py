"""Standalone (non-Steam) launching: launcher script + desktop menu entry.

Some launchers behave better when they are *not* started through Steam
(no Steam overlay, no Steam Input, no pressure-vessel container). In that mode
Griffin creates the Proton prefix in its own folder and generates:

  * a launcher shell script that runs the game with ``proton run``
  * (optionally) a .desktop entry so it shows up in your application menu
"""

from __future__ import annotations

import os
import shlex
import shutil
import stat
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

from griffin.config import AppConfig, ManagedGame
from griffin.logging_utils import log_info, log_warning
from griffin.steam.proton import SteamProton
from griffin.utils import atomic_write_text, clean_env, slugify

ELECTRON_ARGS = ["--disable-gpu", "--no-sandbox", "--disable-gpu-sandbox"]


def parse_extra_args(extra: str) -> list[str]:
    """Split user-typed launch arguments shell-style. Raises ValueError if unbalanced."""
    try:
        return shlex.split(extra or "")
    except ValueError as e:
        raise ValueError(f"Invalid launch arguments ({e}). Check your quotes.") from e


def prefix_dir_name(name: str, app_id: int) -> str:
    return f"{slugify(name)}-{app_id:08x}"


def build_launch_script(
    *,
    name: str,
    exe_path: str,
    start_dir: str,
    proton: SteamProton,
    steam_path: str,
    compat_dir: str,
    app_id: int,
    args: list[str],
    wined3d: bool = False,
    no_esync: bool = False,
    no_fsync: bool = False,
) -> str:
    q = shlex.quote
    env_lines = [
        f"export STEAM_COMPAT_CLIENT_INSTALL_PATH={q(steam_path)}",
        f"export STEAM_COMPAT_DATA_PATH={q(compat_dir)}",
        f"export SteamAppId={app_id}",
        f"export SteamGameId={app_id}",
    ]
    if wined3d:
        env_lines.append("export PROTON_USE_WINED3D=1")
    if no_esync:
        env_lines.append("export PROTON_NO_ESYNC=1")
    if no_fsync:
        env_lines.append("export PROTON_NO_FSYNC=1")
    arg_str = " ".join(q(a) for a in args)
    safe_name = name.replace("\n", " ").replace("\r", " ")
    return f"""#!/usr/bin/env bash
# Griffin Game Tweaks standalone launcher: {safe_name}
# Generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}. Re-running setup regenerates this file.
#
# Set GRIFFIN_USE_UMU=1 to run through umu-launcher (if installed) for a Steam-Runtime container.
set -u
{chr(10).join(env_lines)}
cd {q(start_dir)} || {{ echo {q("Start directory missing: " + start_dir)} >&2; exit 1; }}

if [ "${{GRIFFIN_USE_UMU:-0}}" = "1" ] && command -v umu-run >/dev/null 2>&1; then
    export WINEPREFIX={q(str(Path(compat_dir) / 'pfx'))}
    export PROTONPATH={q(str(proton.path))}
    export GAMEID=umu-{app_id}
    exec umu-run {q(exe_path)} {arg_str}
fi

exec {q(str(proton.proton_script))} run {q(exe_path)} {arg_str}
"""


def _desktop_exec_quote(arg: str) -> str:
    """Quote one argument for a .desktop Exec= line (freedesktop spec)."""
    esc = arg
    for ch in ("\\", '"', "`", "$"):
        esc = esc.replace(ch, "\\" + ch)
    return f'"{esc}"' if (" " in arg or any(c in arg for c in '\t\n"\'\\><~|&;$*?#()`')) else esc


def build_desktop_entry(name: str, script_path: Path, icon: str, start_dir: str) -> str:
    safe = "".join(c for c in name if c.isprintable()).strip() or "Windows Game"
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={safe}\n"
        "Comment=Windows game via Proton (Griffin Game Tweaks)\n"
        f"Exec={_desktop_exec_quote(str(script_path))}\n"
        f"Path={start_dir}\n"
        f"Icon={icon or 'applications-games'}\n"
        "Terminal=false\n"
        "Categories=Game;\n"
        "StartupNotify=true\n"
        "X-Griffin-Managed=true\n"
    )


def applications_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "applications"


def write_standalone_launcher(
    *,
    name: str,
    exe_path: str,
    start_dir: str,
    proton: SteamProton,
    steam_path: str,
    compat_dir: Path,
    app_id: int,
    args: list[str],
    icon_path: Optional[str] = None,
    create_menu_entry: bool = True,
    wined3d: bool = False,
    no_esync: bool = False,
    no_fsync: bool = False,
) -> tuple[Path, str]:
    """Write the launcher script (+ optional .desktop). Returns (script_path, desktop_path or "")."""
    cfg = AppConfig.load()
    launchers = cfg.launchers_dir()
    launchers.mkdir(parents=True, exist_ok=True)
    stem = prefix_dir_name(name, app_id)
    script_path = launchers / f"{stem}.sh"
    text = build_launch_script(
        name=name, exe_path=exe_path, start_dir=start_dir, proton=proton,
        steam_path=steam_path, compat_dir=str(compat_dir), app_id=app_id, args=args,
        wined3d=wined3d, no_esync=no_esync, no_fsync=no_fsync,
    )
    atomic_write_text(script_path, text, mode=0o755)
    log_info(f"Launcher script: {script_path}")

    desktop_path = ""
    if create_menu_entry:
        apps = applications_dir()
        apps.mkdir(parents=True, exist_ok=True)
        dpath = apps / f"griffin-{stem}.desktop"
        atomic_write_text(dpath, build_desktop_entry(name, script_path, icon_path or "", start_dir), mode=0o644)
        desktop_path = str(dpath)
        log_info(f"Menu entry: {desktop_path}")
        try:
            if shutil.which("update-desktop-database"):
                subprocess.run(["update-desktop-database", str(apps)], check=False, timeout=15,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            pass
    return script_path, desktop_path


def remove_standalone_files(launcher_path: str, desktop_path: str = "") -> None:
    for p in (launcher_path, desktop_path):
        if not p:
            continue
        try:
            Path(p).unlink()
            log_info(f"Removed {p}")
        except FileNotFoundError:
            pass
        except OSError as e:
            log_warning(f"Could not remove {p}: {e}")


def launch_standalone(game: ManagedGame) -> Path:
    """Start a standalone game detached from Griffin. Returns the log file path."""
    script = Path(game.launcher_path)
    if not script.is_file():
        raise RuntimeError(f"Launcher script not found: {script}\nRe-run setup for this game to regenerate it.")
    log_dir = AppConfig.default_cache_dir() / "launch-logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{script.stem}.log"
    with open(log_path, "ab") as lf:
        subprocess.Popen(
            [str(script)],
            env=clean_env(),
            stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return log_path
