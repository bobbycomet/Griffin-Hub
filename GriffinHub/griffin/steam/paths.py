"""Steam path and account detection."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from griffin.config import AppConfig
from griffin.logging_utils import log_info, log_warning

STEAM_RELATIVE_PATHS = [
    ".steam/steam",
    ".local/share/Steam",
    ".var/app/com.valvesoftware.Steam/.steam/steam",
    ".var/app/com.valvesoftware.Steam/data/Steam",
    "snap/steam/common/.local/share/Steam",
]


def is_valid_steam_path(path: Path) -> bool:
    return path.exists() and (path / "steamapps").exists()


def find_steam_path() -> Optional[Path]:
    cfg = AppConfig.load()
    if cfg.custom_steam_path:
        custom = Path(cfg.custom_steam_path)
        if is_valid_steam_path(custom):
            return custom
    home = Path.home()
    for rel in STEAM_RELATIVE_PATHS:
        p = home / rel
        if is_valid_steam_path(p):
            return p
    return None


def detect_steam_path_checked() -> Optional[str]:
    path = find_steam_path()
    if path:
        s = str(path)
        log_info(f"Steam detected at: {s}")
        return s
    log_warning("Steam installation not detected! Griffin Game Tweaks requires Steam (Proton).")
    return None


def find_all_steam_libraries() -> list[Path]:
    """
    Return every Steam library root (primary + secondary drives).

    Reads libraryfolders.vdf from the main Steam install. Each entry is a
    directory that contains a ``steamapps`` folder, e.g.::

        /home/user/.steam/steam
        /mnt/sdb1/SteamLibrary

    Non-Steam shortcut prefixes still live under the *primary* install's
    ``steamapps/compatdata/<appid>`` (Steam's own rule). Game installs and
    their Proton prefixes, however, may live on any library — including
    external drives. Callers that need the game files visible inside Proton
    should add these paths to ``STEAM_COMPAT_MOUNTS``.
    """
    steam = find_steam_path()
    if not steam:
        return []

    libraries: list[Path] = []
    seen: set[Path] = set()

    def _add(p: Path) -> None:
        try:
            canon = p.resolve()
        except OSError:
            canon = p
        if canon in seen:
            return
        # Accept either the library root or a path that already ends with steamapps
        if (canon / "steamapps").is_dir():
            seen.add(canon)
            libraries.append(canon)
        elif canon.name == "steamapps" and canon.parent.is_dir():
            parent = canon.parent
            if parent not in seen:
                seen.add(parent)
                libraries.append(parent)

    _add(steam)

    for vdf in (
        steam / "steamapps" / "libraryfolders.vdf",
        steam / "config" / "libraryfolders.vdf",
    ):
        if not vdf.is_file():
            continue
        try:
            content = vdf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            from griffin.steam.vdf_parse import parse_library_folders

            for path_str in parse_library_folders(content):
                _add(Path(path_str))
        except Exception as e:
            log_warning(f"libraryfolders.vdf parse issue ({vdf}): {e}")

    if len(libraries) > 1:
        log_info(
            "Steam libraries: "
            + ", ".join(str(p) for p in libraries)
        )
    return libraries



def parse_all_library_entries() -> list[dict]:
    """
    Return every Steam library entry from libraryfolders.vdf with AppIDs.

    Each dict: ``{"path": Path, "apps": set[str]}``.
    Falls back to path-only entries when the VDF has no apps map.
    """
    steam = find_steam_path()
    if not steam:
        return []
    entries: list[dict] = []
    seen: set[Path] = set()

    def _add(path: Path, apps: set[str]) -> None:
        try:
            canon = path.resolve()
        except OSError:
            canon = path
        if canon in seen:
            # Merge apps into existing
            for e in entries:
                try:
                    if e["path"].resolve() == canon:
                        e["apps"] |= apps
                        break
                except OSError:
                    if e["path"] == canon:
                        e["apps"] |= apps
                        break
            return
        # Normalize to library root (parent of steamapps if needed)
        if canon.name == "steamapps" and canon.parent.is_dir():
            canon = canon.parent
        if not (canon / "steamapps").is_dir() and canon != steam:
            # Still record if path exists — may be offline drive
            if not canon.exists():
                return
        seen.add(canon)
        entries.append({"path": canon, "apps": set(apps)})

    _add(steam, set())

    for vdf in (
        steam / "steamapps" / "libraryfolders.vdf",
        steam / "config" / "libraryfolders.vdf",
    ):
        if not vdf.is_file():
            continue
        try:
            content = vdf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            from griffin.steam.vdf_parse import parse_library_folders_detailed

            for item in parse_library_folders_detailed(content):
                _add(Path(item["path"]), set(item.get("apps") or set()))
        except Exception as e:
            log_warning(f"libraryfolders.vdf detailed parse issue ({vdf}): {e}")

    # If apps maps were empty, scan appmanifests to populate ownership
    for entry in entries:
        if entry["apps"]:
            continue
        steamapps = entry["path"] / "steamapps"
        if not steamapps.is_dir():
            continue
        try:
            for f in steamapps.iterdir():
                name = f.name
                if name.startswith("appmanifest_") and name.endswith(".acf"):
                    app_id = name[len("appmanifest_") : -len(".acf")]
                    if app_id.isdigit():
                        entry["apps"].add(app_id)
        except OSError:
            pass

    return entries


def find_library_for_app(app_id: str | int) -> Optional[Path]:
    """
    Locate the Steam library root that owns ``app_id`` (e.g. 489830 = Skyrim SE).

    Preference order:
      1. libraryfolders.vdf ``apps`` map listing the AppID
      2. library containing ``steamapps/appmanifest_<id>.acf``
      3. library containing ``steamapps/common/...`` install dir (via manifest)
    """
    app_id = str(app_id)
    entries = parse_all_library_entries()

    # 1) VDF apps map
    for e in entries:
        if app_id in e["apps"]:
            # Confirm appmanifest or common folder still exists
            sa = e["path"] / "steamapps"
            if (sa / f"appmanifest_{app_id}.acf").is_file() or (
                sa / "compatdata" / app_id
            ).exists():
                log_info(f"AppID {app_id} owned by library {e['path']} (vdf apps map)")
                return e["path"]

    # 2) appmanifest scan
    for e in entries:
        acf = e["path"] / "steamapps" / f"appmanifest_{app_id}.acf"
        if acf.is_file():
            log_info(f"AppID {app_id} owned by library {e['path']} (appmanifest)")
            return e["path"]

    # Also search all known libraries from find_all_steam_libraries
    for lib in find_all_steam_libraries():
        acf = lib / "steamapps" / f"appmanifest_{app_id}.acf"
        if acf.is_file():
            log_info(f"AppID {app_id} owned by library {lib} (library scan)")
            return lib

    return None


def find_compatdata_for_app(app_id: str | int) -> Optional[Path]:
    """
    Return the compatdata directory for a Steam AppID across all libraries.

    Example for Skyrim SE on a secondary SSD::

        /mnt/sdb1/SteamLibrary/steamapps/compatdata/489830

    Prefer the library that owns the game install, then any library that has
    the folder, then the primary Steam install.
    """
    app_id = str(app_id)
    candidates: list[Path] = []

    owner = find_library_for_app(app_id)
    if owner:
        candidates.append(owner / "steamapps" / "compatdata" / app_id)

    for lib in find_all_steam_libraries():
        candidates.append(lib / "steamapps" / "compatdata" / app_id)

    primary = find_steam_path()
    if primary:
        candidates.append(primary / "steamapps" / "compatdata" / app_id)

    seen: set[Path] = set()
    for c in candidates:
        try:
            canon = c.resolve()
        except OSError:
            canon = c
        if canon in seen:
            continue
        seen.add(canon)
        if c.is_dir():
            log_info(f"compatdata for AppID {app_id}: {c}")
            return c
    return None


def find_pfx_for_app(app_id: str | int) -> Optional[Path]:
    """Return ``compatdata/<appid>/pfx`` if present."""
    compat = find_compatdata_for_app(app_id)
    if not compat:
        return None
    pfx = compat / "pfx"
    return pfx if pfx.is_dir() else compat


def steam_compat_mounts(broad: Optional[bool] = None) -> list[str]:
    """
    Build the list of paths that should appear in ``STEAM_COMPAT_MOUNTS``.

    By default this is *only* what the container needs to see your games:
    every Steam library root plus the mount point that holds it (e.g.
    ``/mnt/sdb1`` for ``/mnt/sdb1/SteamLibrary``). Your home directory is
    already visible inside the container, so it is not added.

    With ``broad=True`` (Settings → "Expose all volumes") every automount root
    and top-level directory is added as well. That is convenient when a
    launcher lives on a removable drive, but it weakens the container's
    isolation, so it is opt-in.

    Implemented here (not via shortcuts) to avoid circular imports.
    """
    if broad is None:
        broad = AppConfig.load().broad_mounts
    mounts: list[str] = []
    seen: set[str] = set()
    home = str(Path.home())

    def _add(path: str) -> None:
        path = (path or "").rstrip("/") or path
        if not path or path in seen or path in ("/", home, "/home"):
            return
        if not Path(path).is_dir():
            return
        seen.add(path)
        mounts.append(path)

    for lib in find_all_steam_libraries():
        _add(str(lib))
        parent = lib.parent
        if str(parent) not in ("/", "", home, "/home"):
            _add(str(parent))

    if broad:
        for extra in ("/mnt", "/media", "/run/media"):
            _add(extra)
            try:
                root = Path(extra)
                if root.is_dir():
                    for child in root.iterdir():
                        if child.is_dir() and not child.name.startswith("."):
                            _add(str(child))
            except OSError:
                pass
        already = {
            "bin", "etc", "home", "lib", "lib32", "lib64",
            "overrides", "run", "sbin", "tmp", "usr", "var",
        }
        system = {"proc", "sys", "dev", "boot", "root", "lost+found", "snap"}
        try:
            for entry in Path("/").iterdir():
                name = entry.name
                if name.startswith(".") or name in already or name in system:
                    continue
                if entry.is_dir():
                    _add(f"/{name}")
        except OSError:
            pass

    mounts.sort()
    return mounts


def steam_compat_mounts_env() -> str:
    """Colon-joined ``STEAM_COMPAT_MOUNTS`` value for export / launch options."""
    return ":".join(steam_compat_mounts())


@dataclass
class SteamAccount:
    account_id: str
    persona_name: str
    most_recent: bool = False
    timestamp: int = 0


def _parse_vdf_kv(line: str) -> Optional[tuple[str, str]]:
    parts = re.findall(r'"([^"]*)"', line)
    if len(parts) >= 2:
        return parts[0], parts[1]
    return None


def get_steam_accounts() -> list[SteamAccount]:
    steam = find_steam_path()
    if not steam:
        return []
    loginusers = steam / "config" / "loginusers.vdf"
    userdata = steam / "userdata"
    if not loginusers.exists():
        return []
    try:
        content = loginusers.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    accounts: list[SteamAccount] = []
    current_sid: Optional[str] = None
    persona = ""
    most_recent = False
    timestamp = 0

    def flush():
        nonlocal current_sid, persona, most_recent, timestamp
        if current_sid:
            try:
                steam64 = int(current_sid)
                account_id = str(steam64 - 76561197960265728)
            except ValueError:
                current_sid = None
                return
            if (userdata / account_id).exists():
                accounts.append(
                    SteamAccount(
                        account_id=account_id,
                        persona_name=persona or account_id,
                        most_recent=most_recent,
                        timestamp=timestamp,
                    )
                )
        current_sid = None
        persona = ""
        most_recent = False
        timestamp = 0

    for line in content.splitlines():
        trimmed = line.strip()
        m = re.match(r'^"(\d{17})"$', trimmed)
        if m and m.group(1).startswith("7656"):
            flush()
            current_sid = m.group(1)
            continue
        kv = _parse_vdf_kv(trimmed)
        if kv and current_sid:
            key, val = kv
            kl = key.lower()
            if kl == "personaname":
                persona = val
            elif kl == "mostrecent":
                most_recent = val == "1"
            elif kl == "timestamp":
                try:
                    timestamp = int(val)
                except ValueError:
                    timestamp = 0
    flush()
    accounts.sort(key=lambda a: a.timestamp, reverse=True)
    return accounts


def find_userdata_path() -> Optional[Path]:
    cfg = AppConfig.load()
    steam = find_steam_path()
    if not steam:
        return None
    userdata = steam / "userdata"
    if not userdata.exists():
        return None
    if cfg.selected_steam_account:
        p = userdata / cfg.selected_steam_account
        if p.exists():
            return p
    accounts = get_steam_accounts()
    for acc in accounts:
        if acc.most_recent:
            p = userdata / acc.account_id
            if p.exists():
                return p
    if accounts:
        p = userdata / accounts[0].account_id
        if p.exists():
            return p
    # Fallback: newest numeric dir except "0"
    candidates = []
    try:
        for entry in userdata.iterdir():
            if entry.is_dir() and entry.name != "0" and entry.name.isdigit():
                candidates.append(entry)
    except OSError:
        return None
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]
