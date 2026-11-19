"""Griffin Play tab for Griffin Hub.

The tab has sub-tabs, like Hub's other tabs:  [ Griffin Play ]  [ Game Tweaks ]
"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QTabWidget, QVBoxLayout, QWidget

from .tweaks_page import GameTweaksPage
from .ui_qt import DARK_QSS, MainWindow


class _PlayView(QWidget):
    """The original Griffin Play UI, embedded in Hub's stack."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("GriffinPlayView")
        self.setStyleSheet(DARK_QSS)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._play = MainWindow()
        central = self._play.takeCentralWidget()
        if central is not None:
            layout.addWidget(central)
        self._play.hide()

    def shutdown(self) -> bool:
        try:
            self._play.close()
        except Exception:
            pass
        return True


class GriffinPlayPage(QWidget):
    """Hosts Griffin Play and Griffin Game Tweaks as sub-tabs."""

    TAB_PLAY, TAB_TWEAKS = 0, 1

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("GriffinPlayPage")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 12)
        layout.setSpacing(0)

        self.tabs = QTabWidget()
        self.tabs.tabBar().setElideMode(Qt.TextElideMode.ElideNone)
        self.tabs.tabBar().setUsesScrollButtons(True)
        self.play = _PlayView()
        self.tweaks = GameTweaksPage()
        self.tabs.addTab(self.play, "Griffin Play")
        self.tabs.addTab(self.tweaks, "Game Tweaks")
        self.tabs.currentChanged.connect(self._on_tab)
        layout.addWidget(self.tabs)

        # kept for code that reached into the old single-view page
        self._play = self.play._play

    def _on_tab(self, index: int) -> None:
        if index == self.TAB_TWEAKS:
            self.tweaks.ensure_built()

    def show_tweaks(self) -> None:
        self.tabs.setCurrentIndex(self.TAB_TWEAKS)

    def shutdown(self) -> bool:
        ok = self.tweaks.shutdown()
        if not ok:
            return False      # a Game Tweaks task is running and the user chose to keep it
        self.play.shutdown()
        return True
