"""Set up a Windows game (with its own launcher EXE) via Proton.

Two launch modes:

* **Steam** (default): add the launcher as a non-Steam shortcut, with Proton chosen
  in Steam's compat settings. You get the overlay, Steam Input, and Steam's container.
* **Standalone**: no Steam shortcut. Griffin creates the prefix in its own folder and a
  launcher script (+ menu entry). Better for launchers that dislike running under Steam.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from griffin.config import AppConfig, ManagedGames
from griffin.deps import verify_prefix_runtimes
from griffin.installers.prefix_setup import install_all_dependencies, kill_wineserver
from griffin.installers.standalone import (
    ELECTRON_ARGS,
    parse_extra_args,
    prefix_dir_name,
    write_standalone_launcher,
    remove_standalone_files,
)
from griffin.installers.task import TaskContext
from griffin.logging_utils import log_install, log_warning
from griffin.steam import (
    add_game_shortcut,
    find_steam_path,
    is_steam_running,
    remove_steam_shortcut,
)
from griffin.steam.shortcuts import steam_shortcut_app_id
from griffin.steam.proton import SteamProton


@dataclass
class GameSetupResult:
    app_id: int
    prefix_path: Path
    name: str
    failed_verbs: list[str] = field(default_factory=list)
    missing_dlls: list[str] = field(default_factory=list)
    launch_mode: str = "steam"
    launcher_path: str = ""
    desktop_path: str = ""


def is_safe_compat_dir(compat: Path, expected_name: str) -> bool:
    """Guard for recursive deletes: only ever delete ``.../compatdata/<id>`` or ``.../prefixes/<name>``."""
    try:
        compat = Path(compat).resolve()
    except OSError:
        return False
    return (
        compat.name == expected_name
        and compat.parent.name in ("compatdata", "prefixes")
        and compat != Path.home().resolve()
        and len(compat.parts) > 3
    )


def delete_compat_dir(compat: Path, expected_name: str) -> bool:
    if not is_safe_compat_dir(compat, expected_name):
        log_warning(f"Refusing to delete unexpected directory: {compat}")
        return False
    shutil.rmtree(compat, ignore_errors=True)
    return True


def _cleanup(app_id: int, compat: Path, expected_name: str, *, steam_mode: bool, delete_dir: bool) -> None:
    log_warning(f"Cleaning up failed install AppID={app_id}")
    if steam_mode:
        try:
            remove_steam_shortcut(app_id)
        except Exception as e:
            log_warning(str(e))
    if delete_dir and compat.exists():
        delete_compat_dir(compat, expected_name)
    ManagedGames.unregister(app_id)


def setup_game(
    name: str,
    exe_path: Path,
    proton: SteamProton,
    ctx: TaskContext,
    *,
    start_dir: Optional[Path] = None,
    is_electron: bool = False,
    extra_verbs: Optional[list[str]] = None,
    icon_path: Optional[str] = None,
    windows_version: str = "win11",
    extra_launch_options: str = "",
    require_steam_exit: bool = True,
    proton_use_wined3d: bool = False,
    proton_no_esync: bool = False,
    proton_no_fsync: bool = False,
    standalone: bool = False,
    verb_profile: str = "standard",
    dpi: int = 0,
    create_menu_entry: bool = True,
) -> GameSetupResult:
    """
    Full setup pipeline:
      1. (Steam mode) create a non-Steam shortcut pointing at the launcher EXE
      2. Initialize the Proton prefix
      3. Install winetricks verbs + registry tweaks
      4. Verify key DLLs
      5. (Standalone mode) write launcher script + menu entry
      6. Register in ManagedGames
    """
    exe_path = Path(exe_path).expanduser().resolve()
    if not exe_path.is_file():
        raise RuntimeError(f"EXE not found: {exe_path}")
    if exe_path.suffix.lower() != ".exe":
        log_warning(f"Selected file does not end in .exe: {exe_path.name}")

    start_dir = Path(start_dir).expanduser().resolve() if start_dir else exe_path.parent
    if not start_dir.is_dir():
        raise RuntimeError(f"Start directory does not exist: {start_dir}")

    steam = find_steam_path()
    if not steam:
        raise RuntimeError("Steam not found (Proton needs a Steam install, even in standalone mode)")

    # Validate launch args up front so a typo can't fail the setup half-way through.
    args: list[str] = []
    if standalone:
        args = (ELECTRON_ARGS if is_electron else []) + parse_extra_args(extra_launch_options)

    if not standalone and is_steam_running():
        msg = (
            "Steam is running — shortcut may be overwritten when Steam exits. "
            "Fully exit Steam (Steam → Exit) before setup for a reliable install."
        )
        log_warning(msg)
        ctx.log(msg)
        if require_steam_exit:
            raise RuntimeError(msg)

    launcher_path = ""
    desktop_path = ""

    if standalone:
        quoted = f'"{exe_path}"'
        app_id = steam_shortcut_app_id(quoted, name)
        dir_name = prefix_dir_name(name, app_id)
        compat = AppConfig.load().prefixes_dir() / dir_name
        prefix = compat / "pfx"
        existed_before = compat.exists()
        compat.mkdir(parents=True, exist_ok=True)
        ctx.set_progress(0.05)
    else:
        ctx.set_status("Creating Steam shortcut…")
        ctx.set_progress(0.05)
        steam_result = add_game_shortcut(
            name,
            str(exe_path),
            str(start_dir),
            proton.config_name,
            is_electron=is_electron,
            icon_path=icon_path,
            extra_launch_options=extra_launch_options or "",
            proton_use_wined3d=proton_use_wined3d,
            proton_no_esync=proton_no_esync,
            proton_no_fsync=proton_no_fsync,
        )
        app_id = steam_result.app_id
        prefix = steam_result.prefix_path
        compat = prefix.parent
        dir_name = str(app_id)
        # add_game_shortcut creates the (empty) compat folder itself, so "existed before"
        # means "already contains a prefix".
        existed_before = (prefix / "system.reg").exists()

    try:
        ctx.set_status("Setting up Proton prefix and dependencies…")
        failed = install_all_dependencies(
            prefix,
            proton,
            ctx,
            start_progress=0.10,
            end_progress=0.92,
            app_id=app_id,
            extra_verbs=extra_verbs,
            windows_version=windows_version,
            verb_profile=verb_profile,
            dpi=dpi,
            exe_path=str(exe_path),
        )

        _found, missing = verify_prefix_runtimes(prefix)
        if missing:
            ctx.log(
                "Some runtime DLLs are still missing — try Repair or re-run winetricks "
                "from Managed Games."
            )

        if standalone:
            ctx.set_status("Writing launcher script…")
            script, desktop = write_standalone_launcher(
                name=name,
                exe_path=str(exe_path),
                start_dir=str(start_dir),
                proton=proton,
                steam_path=str(steam),
                compat_dir=compat,
                app_id=app_id,
                args=args,
                icon_path=icon_path,
                create_menu_entry=create_menu_entry,
                wined3d=proton_use_wined3d,
                no_esync=proton_no_esync,
                no_fsync=proton_no_fsync,
            )
            launcher_path, desktop_path = str(script), desktop

        ManagedGames.register(
            app_id,
            name,
            str(exe_path),
            str(start_dir),
            str(prefix),
            proton.config_name,
            notes=extra_launch_options or "",
            launch_mode="standalone" if standalone else "steam",
            windows_version=windows_version,
            verb_profile=verb_profile,
            dpi=dpi,
            extra_verbs=list(extra_verbs or []),
            launcher_path=launcher_path,
            desktop_path=desktop_path,
        )

        ctx.set_progress(1.0)
        if standalone:
            msg = f"Setup complete! Launch it from Managed Games" + (
                " or your application menu." if desktop_path else "."
            )
        else:
            vdf_hint = getattr(steam_result, "shortcuts_vdf", "") or ""
            msg = (
                f"Setup complete! AppID {app_id}.\n"
                "Fully exit Steam (Steam → Exit), then start Steam again to see the non-Steam game."
            )
            if vdf_hint:
                msg += f"\nshortcuts.vdf: {vdf_hint}"
        if failed:
            msg += f"\nWarning: {len(failed)} winetricks verb(s) failed: {', '.join(failed)}"
        if missing:
            msg += f"\nWarning: missing DLLs: {', '.join(Path(m).name for m in missing)}"
        ctx.set_status(msg)
        log_install(f"Game setup complete: {name} appid={app_id} mode={'standalone' if standalone else 'steam'}")
        return GameSetupResult(
            app_id=app_id,
            prefix_path=prefix,
            name=name,
            failed_verbs=failed,
            missing_dlls=missing,
            launch_mode="standalone" if standalone else "steam",
            launcher_path=launcher_path,
            desktop_path=desktop_path,
        )
    except Exception:
        try:
            kill_wineserver(prefix, proton)
        except Exception:
            pass
        # Only delete a prefix directory we created in this run — never a working
        # prefix that was there before.
        _cleanup(app_id, compat, dir_name, steam_mode=not standalone, delete_dir=not existed_before)
        if standalone:
            try:
                remove_standalone_files(launcher_path, desktop_path)
            except Exception:
                pass
        raise


def remove_game(
    game,
    ctx: TaskContext,
    *,
    delete_prefix: bool = False,
    remove_shortcut: bool = False,
) -> str:
    """Remove a managed game. Always unregisters it from Griffin.

    * Standalone games: the launcher script and menu entry are always deleted.
    * Steam games: ``remove_shortcut`` also removes the non-Steam shortcut (Steam must be closed).
    * ``delete_prefix`` deletes the Wine prefix folder (saves/settings stored inside are lost).
    """
    notes: list[str] = []
    if game.launch_mode == "standalone":
        ctx.set_status("Removing launcher script and menu entry…")
        remove_standalone_files(game.launcher_path, game.desktop_path)
        notes.append("launcher + menu entry removed")
    elif remove_shortcut:
        if is_steam_running():
            raise RuntimeError("Steam is running. Fully exit Steam before removing its shortcut.")
        ctx.set_status("Removing Steam shortcut…")
        remove_steam_shortcut(game.app_id)
        notes.append("Steam shortcut removed")

    if delete_prefix:
        compat = Path(game.prefix_path).parent
        ctx.set_status("Deleting Wine prefix…")
        if delete_compat_dir(compat, compat.name):
            notes.append("prefix deleted")
        else:
            notes.append("prefix NOT deleted (unexpected location — delete it manually)")

    ManagedGames.unregister(game.app_id)
    notes.append("removed from Griffin")
    return ", ".join(notes).capitalize() + "."
