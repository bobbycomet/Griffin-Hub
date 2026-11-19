"""Griffin Game Tweaks as a sub-tab of Griffin Play.

The Game Tweaks UI (``griffin.ui.main_window.MainWindow``) is embedded as-is: same code as the
standalone app, so fixes made there show up here.  It is built lazily the first time the tab is
opened because creating it scans Steam libraries and Proton versions.
"""
from __future__ import annotations

import logging
import traceback

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget

log = logging.getLogger("griffinhub.tweaks")


def _deps_command() -> list:
    """Root command that installs winetricks + cabextract natively for this distro (via the distro module)."""
    from griffinhub.hardware import _modloader
    return ["pkexec", "bash", "-c", _modloader.feature_script("game-deps")]


def _embed_reset_style(assets) -> str:
    """Neutralise the properties Hub's application-wide stylesheet would otherwise leak into Game Tweaks.

    Qt merges the application stylesheet with a widget's own, so anything Game Tweaks' theme does not
    set explicitly (group-box title colour/offset, bold buttons, the combo-box arrow ...) would pick up
    Hub's value.  These rules restore Game Tweaks' own look inside the embedded view only.
    """
    from griffin.ui.theme import ACCENT, BG, BORDER, SURFACE, TEXT
    chevron = (assets / "chevron-down.png").as_posix()
    return f"""
QGroupBox {{ color: {TEXT}; font-size: 14px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; top: 0px; padding: 0 4px;
    background: transparent; color: {TEXT}; font-size: 14px; font-weight: 400; letter-spacing: 0px; }}
QPushButton {{ font-weight: 400; }}
QCheckBox, QRadioButton {{ color: {TEXT}; }}
QLineEdit:focus, QComboBox:focus {{ background-color: {BG}; border-color: {ACCENT}; }}
QComboBox:hover {{ border-color: {BORDER}; }}
QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right; width: 24px; border: none; }}
QComboBox::down-arrow {{ image: url({chevron}); width: 12px; height: 12px; }}
QProgressBar {{ color: {TEXT}; }}
QListWidget {{ padding: 0px; }}
QListWidget::item {{ margin: 0px; border-radius: 0px; color: {TEXT}; }}
QListWidget::item:selected {{ background-color: #2d3f66; color: {TEXT}; }}
QTextEdit, QPlainTextEdit {{ border-radius: 4px; padding: 6px 8px; background-color: {SURFACE}; }}
QTextEdit:read-only, QPlainTextEdit:read-only {{ background-color: {SURFACE}; color: {TEXT}; border: 1px solid {BORDER}; }}
"""


class GameTweaksPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("GameTweaksPage")
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._win = None
        self._built = False
        self._msg = QLabel("Griffin Game Tweaks loads when you open this tab.")
        self._msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._msg.setWordWrap(True)
        self._lay.addWidget(self._msg)

    # -- lazy build -------------------------------------------------------------
    def ensure_built(self) -> bool:
        if self._built:
            return self._win is not None
        self._built = True
        try:
            from griffin.logging_utils import get_log_path, init_logger, log_info
            from griffin.ui import main_window as gmw
            from griffin.ui.theme import STYLESHEET

            gmw.DEPS_COMMAND_PROVIDER = _deps_command   # native winetricks/cabextract install per distro
            if get_log_path() is None:
                init_logger()
            log_info("Griffin Game Tweaks embedded in Griffin Hub")
            win = gmw.MainWindow(embedded=True)
            win.setStyleSheet(STYLESHEET + _embed_reset_style(gmw.ASSETS))   # scoped to this subtree only
            self._lay.removeWidget(self._msg)
            self._msg.hide()
            self._lay.addWidget(win)
            self._win = win
            return True
        except ImportError as e:
            hint = ""
            if "requests" in str(e):
                hint = "\n\nInstall the Python 'requests' module (python3-requests / python-requests) and restart Griffin Hub."
            self._fail(f"Griffin Game Tweaks could not be loaded:\n{e}{hint}")
        except Exception as e:  # never let this tab take the whole app down
            log.exception("Game Tweaks failed to start")
            self._fail("Griffin Game Tweaks could not start:\n" + "".join(
                traceback.format_exception_only(type(e), e)).strip())
        return False

    def _fail(self, text: str) -> None:
        self._msg.setText(text)
        self._msg.setStyleSheet("color: #f87171; font-size: 14px; padding: 24px;")

    # -- lifecycle ---------------------------------------------------------------
    def shutdown(self) -> bool:
        """Close the embedded window (asks before cancelling a running task). False = user said no."""
        if self._win is None:
            return True
        try:
            return bool(self._win.close())
        except Exception:
            return True
