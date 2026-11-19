"""Application configuration and managed game tracking."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field, asdict, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from griffin.utils import atomic_write_text


def get_home() -> Path:
    return Path.home()


def normalize_path_for_steam(path: str) -> str:
    """Map /var/home/... to /home/... for pressure-vessel compatibility."""
    if path.startswith("/var/home/"):
        return "/home/" + path[len("/var/home/") :]
    return path


@dataclass
class AppConfig:
    selected_proton: Optional[str] = None
    first_run_completed: bool = False
    data_path: str = field(default_factory=lambda: str(get_home() / "GriffinGameTweaks"))
    custom_steam_path: str = ""
    selected_steam_account: str = ""
    cache_location: str = ""
    # Extra winetricks verbs the user always wants
    extra_verbs: list[str] = field(default_factory=list)
    # Default runtime set for new games: minimal | standard | extended
    verb_profile: str = "standard"
    # Expose every mounted volume to Steam's container (off = Steam libraries only)
    broad_mounts: bool = False
    # pinned = checksum-verified winetricks release; latest = unverified master (refreshed daily)
    winetricks_channel: str = "pinned"

    @staticmethod
    def config_dir() -> Path:
        return get_home() / ".config" / "griffin-game-tweaks"

    @staticmethod
    def config_path() -> Path:
        return AppConfig.config_dir() / "config.json"

    @staticmethod
    def default_cache_dir() -> Path:
        return get_home() / ".cache" / "griffin-game-tweaks"

    @staticmethod
    def tmp_path() -> Path:
        return AppConfig.default_cache_dir() / "tmp"

    @staticmethod
    def bin_path() -> Path:
        return AppConfig.config_dir() / "bin"

    def cache_dir(self) -> Path:
        if self.cache_location:
            return Path(self.cache_location)
        return AppConfig.default_cache_dir()

    def get_data_path(self) -> Path:
        return Path(self.data_path).expanduser()

    def prefixes_dir(self) -> Path:
        """Where standalone (non-Steam) game prefixes live."""
        return self.get_data_path() / "prefixes"

    def launchers_dir(self) -> Path:
        """Where standalone launcher scripts live."""
        return self.get_data_path() / "launchers"

    @classmethod
    def load(cls) -> "AppConfig":
        path = cls.config_path()
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                known = {f.name for f in fields(cls)}
                filtered = {k: v for k, v in data.items() if k in known}
                return cls(**filtered)
            except Exception:
                pass
        return cls()

    def save(self) -> None:
        atomic_write_text(self.config_path(), json.dumps(asdict(self), indent=2), mode=0o600)

    def ensure_dirs(self) -> None:
        for d in (self.config_dir(), self.bin_path(), self.tmp_path(), self.cache_dir()):
            d.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(d, 0o700)
            except OSError:
                pass


@dataclass
class ManagedGame:
    app_id: int
    name: str
    exe_path: str
    start_dir: str
    prefix_path: str
    proton_config_name: Optional[str] = None
    created: str = ""
    notes: str = ""
    # --- added in 1.1 (all optional so old managed_games.json files still load) ---
    launch_mode: str = "steam"  # "steam" (non-Steam shortcut) | "standalone"
    windows_version: str = "win11"
    verb_profile: str = "standard"
    dpi: int = 0
    extra_verbs: list[str] = field(default_factory=list)
    launcher_path: str = ""  # standalone: generated launch script
    desktop_path: str = ""  # standalone: menu entry (.desktop)

    @property
    def is_standalone(self) -> bool:
        return self.launch_mode == "standalone"


class ManagedGames:
    _lock = threading.RLock()

    @staticmethod
    def _path() -> Path:
        return AppConfig.config_dir() / "managed_games.json"

    @classmethod
    def load(cls) -> list[ManagedGame]:
        path = cls._path()
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            items = data.get("games", [])
            if not isinstance(items, list):
                raise ValueError("'games' is not a list")
        except Exception:
            # Keep the unreadable file so nothing is lost, then start empty.
            try:
                bak = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
                path.replace(bak)
            except OSError:
                pass
            return []

        known = {f.name for f in fields(ManagedGame)}
        result: list[ManagedGame] = []
        for item in items:
            try:
                kwargs = {k: v for k, v in item.items() if k in known}
                kwargs["app_id"] = int(kwargs["app_id"])
                result.append(ManagedGame(**kwargs))
            except Exception:
                continue  # skip one bad entry, keep the rest
        return result

    @classmethod
    def save(cls, games: list[ManagedGame]) -> None:
        payload = {"games": [asdict(g) for g in games]}
        atomic_write_text(cls._path(), json.dumps(payload, indent=2))

    @classmethod
    def get(cls, app_id: int) -> Optional[ManagedGame]:
        for g in cls.load():
            if g.app_id == app_id:
                return g
        return None

    @classmethod
    def register(
        cls,
        app_id: int,
        name: str,
        exe_path: str,
        start_dir: str,
        prefix_path: str,
        proton_config_name: Optional[str] = None,
        notes: str = "",
        **extra,
    ) -> None:
        with cls._lock:
            games = [g for g in cls.load() if g.app_id != app_id]
            known = {f.name for f in fields(ManagedGame)}
            games.append(
                ManagedGame(
                    app_id=app_id,
                    name=name,
                    exe_path=exe_path,
                    start_dir=start_dir,
                    prefix_path=prefix_path,
                    proton_config_name=proton_config_name,
                    created=datetime.now(timezone.utc).isoformat(),
                    notes=notes,
                    **{k: v for k, v in extra.items() if k in known},
                )
            )
            cls.save(games)

    @classmethod
    def unregister(cls, app_id: int) -> None:
        with cls._lock:
            cls.save([g for g in cls.load() if g.app_id != app_id])
