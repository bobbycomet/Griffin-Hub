"""Detect Steam Proton versions (10+)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from griffin.logging_utils import log_info, log_warning
from griffin.steam.paths import find_all_steam_libraries, find_steam_path


@dataclass
class SteamProton:
    name: str
    config_name: str
    path: Path
    is_steam_proton: bool = False
    is_experimental: bool = False

    def wine_binary(self) -> Optional[Path]:
        for rel in ("files/bin/wine", "dist/bin/wine"):
            p = self.path / rel
            if p.exists():
                return p
        return None

    def wineserver_binary(self) -> Optional[Path]:
        for rel in ("files/bin/wineserver", "dist/bin/wineserver"):
            p = self.path / rel
            if p.exists():
                return p
        return None

    def bin_dir(self) -> Optional[Path]:
        w = self.wine_binary()
        return w.parent if w else None

    @property
    def proton_script(self) -> Path:
        return self.path / "proton"


_NUM = re.compile(r"\d+")


def version_tuple(name: str) -> tuple[int, ...]:
    """Numeric version parts of a Proton name, e.g. 'GE-Proton10-3' -> (10, 3)."""
    return tuple(int(x) for x in _NUM.findall(name)[:4])


def _is_hotfix(name: str) -> bool:
    return "hotfix" in name.lower()


def _is_proton_10_or_newer(p: SteamProton) -> bool:
    name = p.name
    if p.is_experimental or "Experimental" in name or _is_hotfix(name):
        return True
    if "Runtime" in name or name == "LegacyRuntime":
        return False
    if "CachyOS" in name or "cachy" in name.lower():
        return True
    vt = version_tuple(name)
    if vt:
        return vt[0] >= 10
    return True  # unknown naming scheme: let the user decide


def steam_config_name(dir_name: str) -> str:
    """Name Steam uses internally for its own Proton builds ("Proton 10.0" -> "proton_10")."""
    low = dir_name.lower()
    if "experimental" in low:
        return "proton_experimental"
    if "hotfix" in low:
        return "proton_hotfix"
    m = _NUM.search(dir_name)
    return f"proton_{m.group(0)}" if m else dir_name


def _scan_dir(directory: Path, is_steam: bool) -> list[SteamProton]:
    found: list[SteamProton] = []
    if not directory.is_dir():
        return found
    try:
        entries = list(directory.iterdir())
    except OSError:
        return found
    for entry in entries:
        if not entry.is_dir():
            continue
        name = entry.name
        has_proton = (entry / "proton").exists()
        has_vdf = (entry / "compatibilitytool.vdf").exists()
        if is_steam:
            if not (name.startswith("Proton") and has_proton):
                continue
            found.append(
                SteamProton(
                    name=name,
                    config_name=steam_config_name(name),
                    path=entry,
                    is_steam_proton=True,
                    is_experimental="experimental" in name.lower(),
                )
            )
        else:
            if not (has_proton or has_vdf):
                continue
            found.append(
                SteamProton(
                    name=name,
                    config_name=name,
                    path=entry,
                    is_steam_proton=False,
                    is_experimental=False,
                )
            )
    return found


def _sort_key(p: SteamProton) -> tuple:
    rank = 2 if p.is_experimental else 1 if _is_hotfix(p.name) else 0
    return (rank, version_tuple(p.name), p.is_steam_proton, p.name)


def find_steam_protons() -> list[SteamProton]:
    steam = find_steam_path()
    if not steam:
        return []
    is_flatpak = ".var/app/com.valvesoftware.Steam" in str(steam)
    protons: list[SteamProton] = []

    # Valve's Proton can be installed into *any* Steam library, not just the primary one.
    libraries = find_all_steam_libraries() or [steam]
    for lib in libraries:
        protons.extend(_scan_dir(lib / "steamapps" / "common", is_steam=True))
    protons.extend(_scan_dir(steam / "compatibilitytools.d", is_steam=False))
    if not is_flatpak:
        protons.extend(_scan_dir(Path("/usr/share/steam/compatibilitytools.d"), is_steam=False))
    else:
        log_info("Flatpak Steam detected — skipping system protons")

    protons = [p for p in protons if _is_proton_10_or_newer(p)]
    seen: set[Path] = set()
    filtered: list[SteamProton] = []
    for p in protons:
        try:
            key = p.path.resolve()
        except OSError:
            key = p.path
        if key in seen:
            continue
        seen.add(key)
        if p.wine_binary():
            filtered.append(p)
        else:
            log_warning(f"Skipping Proton '{p.name}': wine binary not found")
    # Experimental first, then Hotfix, then newest version first (numeric, not alphabetical).
    filtered.sort(key=_sort_key, reverse=True)
    return filtered
