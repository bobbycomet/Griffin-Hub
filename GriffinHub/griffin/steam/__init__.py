"""Steam integration: paths, Proton, shortcuts, compat tools."""

from .paths import (
    detect_steam_path_checked,
    find_steam_path,
    find_userdata_path,
    get_steam_accounts,
    is_valid_steam_path,
)
from .proton import SteamProton, find_steam_protons
from .shortcuts import (
    SteamShortcutResult,
    add_game_shortcut,
    remove_steam_shortcut,
    generate_launch_options,
    is_steam_running,
)
from .compat import remove_compat_tool, set_compat_tool

__all__ = [
    "detect_steam_path_checked",
    "find_steam_path",
    "find_userdata_path",
    "get_steam_accounts",
    "is_valid_steam_path",
    "SteamProton",
    "find_steam_protons",
    "SteamShortcutResult",
    "add_game_shortcut",
    "remove_steam_shortcut",
    "generate_launch_options",
    "set_compat_tool",
    "remove_compat_tool",
    "is_steam_running",
]
