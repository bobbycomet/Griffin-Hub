"""Game setup orchestration."""

from griffin.installers.game_setup import setup_game, GameSetupResult
from griffin.installers.prefix_setup import (
    install_all_dependencies,
    apply_dpi,
    kill_wineserver,
    repair_prefix,
    rerun_winetricks,
)
from griffin.installers.task import TaskContext

__all__ = [
    "setup_game",
    "GameSetupResult",
    "install_all_dependencies",
    "apply_dpi",
    "kill_wineserver",
    "repair_prefix",
    "rerun_winetricks",
    "TaskContext",
]
