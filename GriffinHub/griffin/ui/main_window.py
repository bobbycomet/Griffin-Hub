"""Griffin Game Tweaks main window (PyQt6)."""

from __future__ import annotations

import sys
from enum import Enum, auto
from pathlib import Path
from typing import Callable, Optional

from PyQt6.QtCore import QDir, QObject, QThread, QUrl, Qt, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QFont, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from griffin import __version__
from griffin.config import AppConfig, ManagedGame, ManagedGames
from griffin.deps import OPTIONAL_VERBS, PROFILE_DESCRIPTIONS, PROFILES
from griffin.installers.game_setup import remove_game, setup_game
from griffin.installers.prefix_setup import repair_prefix, rerun_winetricks
from griffin.installers.standalone import launch_standalone
from griffin.installers.task import TaskContext
from griffin.logging_utils import get_log_path, init_logger, log_error, log_info
from griffin.steam import detect_steam_path_checked, find_steam_protons, is_steam_running
from griffin.ui.theme import STYLESHEET
from griffin.utils import clean_env

ASSETS = Path(__file__).resolve().parent / "assets"
SYSTEM_HELPER = Path("/usr/lib/griffin-game-tweaks/griffin-pkexec-helper")

# A host application (Griffin Hub) can set this to a callable returning the root command that
# installs winetricks + cabextract natively for the running distro.  The standalone package
# ships a pkexec helper instead (SYSTEM_HELPER), which takes precedence when it is installed.
DEPS_COMMAND_PROVIDER: Optional[Callable[[], Optional[list]]] = None


def _deps_command() -> list:
    if SYSTEM_HELPER.is_file():
        return ["pkexec", str(SYSTEM_HELPER), "deps"]
    if DEPS_COMMAND_PROVIDER is not None:
        cmd = DEPS_COMMAND_PROVIDER()
        if cmd:
            return list(cmd)
    raise RuntimeError(
        "No installer is available for winetricks / cabextract on this system.\n"
        "Install them with your package manager (Game Tweaks also downloads its own copies when needed)."
    )

DPI_CHOICES = (
    ("Default — don't change", 0),
    ("120 DPI (125 %)", 120),
    ("144 DPI (150 %)", 144),
    ("192 DPI (200 %)", 192),
)


def _app_icon() -> QIcon:
    candidates = [
        ASSETS / "griffin.png",
        Path(__file__).resolve().parent.parent.parent / "resources" / "icons" / "griffin.png",
    ]
    for p in candidates:
        if p.is_file():
            return QIcon(str(p))
    return QIcon()


def _pick_directory(parent: QWidget, title: str, start: str = "") -> str:
    dlg = QFileDialog(parent, title)
    dlg.setFileMode(QFileDialog.FileMode.Directory)
    dlg.setOption(QFileDialog.Option.ShowDirsOnly, True)
    dlg.setOption(QFileDialog.Option.DontUseNativeDialog, True)
    dlg.setFilter(QDir.Filter.AllDirs | QDir.Filter.Hidden | QDir.Filter.NoDotAndDotDot)
    dlg.setDirectory(start or str(Path.home()))
    if dlg.exec():
        selected = dlg.selectedFiles()
        if selected:
            return selected[0]
    return ""


def _pick_exe(parent: QWidget, title: str = "Select game launcher EXE", start: str = "") -> str:
    path, _ = QFileDialog.getOpenFileName(
        parent, title, start or str(Path.home()), "Windows Executable (*.exe);;All files (*)"
    )
    return path or ""


def _pick_icon(parent: QWidget, start: str = "") -> str:
    path, _ = QFileDialog.getOpenFileName(
        parent, "Select icon", start or str(Path.home()),
        "Images (*.png *.svg *.ico *.xpm);;All files (*)",
    )
    return path or ""


def _muted(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setObjectName("muted")
    return lbl


class Page(Enum):
    SETUP = auto()
    MANAGED = auto()
    SETTINGS = auto()
    ABOUT = auto()


class WorkerSignals(QObject):
    status = pyqtSignal(str)
    log = pyqtSignal(str)
    progress = pyqtSignal(float)
    finished = pyqtSignal(object)
    error = pyqtSignal(str)


class SetupWorker(QThread):
    """Runs ``fn(*args, ctx=ctx, **kwargs)`` off the GUI thread; results come back as signals."""

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        ctx = TaskContext(
            status_callback=self.signals.status.emit,
            log_callback=self.signals.log.emit,
            progress_callback=self.signals.progress.emit,
            cancel_flag=lambda: self._cancel,
        )
        try:
            result = self.fn(*self.args, ctx=ctx, **self.kwargs)
            self.signals.finished.emit(result)
        except Exception as e:  # noqa: BLE001 — surface everything to the UI
            self.signals.error.emit(str(e))


class RemoveDialog(QDialog):
    def __init__(self, parent: QWidget, game: ManagedGame) -> None:
        super().__init__(parent)
        self.setWindowTitle("Remove game")
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"Remove “{game.name}” from Griffin?"))
        if game.is_standalone:
            lay.addWidget(_muted("The launcher script and application-menu entry will be deleted."))
        self.shortcut_cb = QCheckBox("Also remove the Steam shortcut (Steam must be closed)")
        self.shortcut_cb.setVisible(not game.is_standalone)
        lay.addWidget(self.shortcut_cb)
        self.prefix_cb = QCheckBox("Also delete the Wine prefix (saves/settings inside it are lost)")
        lay.addWidget(self.prefix_cb)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)


class MainWindow(QMainWindow):
    def __init__(self, *, embedded: bool = False) -> None:
        """``embedded=True``: used as a page inside a host app (Griffin Hub) - no brand header, no fixed size."""
        super().__init__()
        self.embedded = embedded
        self.setWindowTitle(f"Griffin Game Tweaks v{__version__}")
        if not embedded:
            self.setMinimumSize(700, 520)
            self.resize(1000, 720)
        self.setWindowIcon(_app_icon())
        self.config = AppConfig.load()
        self.config.ensure_dirs()
        self.steam_path = detect_steam_path_checked()
        self.protons = find_steam_protons()
        self._worker: Optional[SetupWorker] = None
        self._managed: list[ManagedGame] = []
        self._build_ui()
        self._refresh_managed()

    # ------------------------------------------------------------------ shell
    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        side = QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(190)
        side_lay = QVBoxLayout(side)
        side_lay.setContentsMargins(12, 16, 12, 16)

        if not self.embedded:  # the host app has its own branding
            brand = QWidget()
            brand.setObjectName("brand")
            brand_lay = QHBoxLayout(brand)
            brand_lay.setContentsMargins(0, 0, 0, 0)
            brand_lay.setSpacing(8)
            logo_path = ASSETS / "griffin-56.png"
            if logo_path.is_file():
                logo = QLabel()
                logo.setPixmap(
                    QPixmap(str(logo_path)).scaled(
                        40, 40, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
                    )
                )
                logo.setFixedSize(40, 40)
                brand_lay.addWidget(logo)
            names = QVBoxLayout()
            names.setSpacing(0)
            title = QLabel("Griffin")
            title.setObjectName("title")
            sub = QLabel("Game Tweaks")
            sub.setObjectName("muted")
            names.addWidget(title)
            names.addWidget(sub)
            brand_lay.addLayout(names)
            brand_lay.addStretch()
            side_lay.addWidget(brand)
            side_lay.addSpacing(20)

        self.nav_buttons: dict[Page, QPushButton] = {}
        for page, label in (
            (Page.SETUP, "Setup Game"),
            (Page.MANAGED, "Managed Games"),
            (Page.SETTINGS, "Settings"),
            (Page.ABOUT, "About"),
        ):
            btn = QPushButton(label)
            btn.setObjectName("nav")
            btn.setCheckable(True)
            btn.clicked.connect(lambda _checked, p=page: self._goto(p))
            side_lay.addWidget(btn)
            self.nav_buttons[page] = btn
        side_lay.addStretch()
        layout.addWidget(side)

        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)
        self.page_index = {Page.SETUP: 0, Page.MANAGED: 1, Page.SETTINGS: 2, Page.ABOUT: 3}
        for builder in (self._page_setup, self._page_managed, self._page_settings, self._page_about):
            self.stack.addWidget(builder())
        self._goto(Page.SETUP)

    def _goto(self, page: Page) -> None:
        for p, btn in self.nav_buttons.items():
            btn.setChecked(p == page)
        self.stack.setCurrentIndex(self.page_index[page])
        if page == Page.MANAGED:
            self._refresh_managed()

    def _wrap_scroll(self, w: QWidget) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        w.setMinimumWidth(0)
        w.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        scroll.setWidget(w)
        scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        return scroll

    def _make_log(self, height: int = 160) -> QTextEdit:
        view = QTextEdit()
        view.setReadOnly(True)
        view.setMaximumHeight(height)
        view.document().setMaximumBlockCount(3000)  # winetricks is chatty — don't grow forever
        return view

    # ------------------------------------------------------------------ Setup
    def _page_setup(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(24, 20, 24, 20)

        t = QLabel("Setup a game with its own launcher")
        t.setObjectName("section")
        lay.addWidget(t)
        lay.addWidget(
            _muted(
                "Many Windows games ship a custom launcher EXE (updates, login, character select, "
                "anti-cheat) that then starts the real game. Pick that .exe — not a storefront "
                "client like Epic or GOG. Griffin creates a Proton prefix, installs common runtimes, "
                "and either adds the game to Steam or makes it launchable on its own."
            )
        )

        name_box = QGroupBox("Display name")
        name_lay = QVBoxLayout(name_box)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g. My Game, Game Name Launcher, …")
        name_lay.addWidget(self.name_edit)
        lay.addWidget(name_box)

        exe_box = QGroupBox("Game launcher executable")
        exe_lay = QVBoxLayout(exe_box)
        row = QHBoxLayout()
        self.exe_edit = QLineEdit()
        self.exe_edit.setPlaceholderText("Path to the game’s launcher .exe")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_exe)
        row.addWidget(self.exe_edit)
        row.addWidget(browse)
        exe_lay.addLayout(row)
        lay.addWidget(exe_box)

        dir_box = QGroupBox("Start directory (usually the folder containing the .exe)")
        dir_lay = QVBoxLayout(dir_box)
        row2 = QHBoxLayout()
        self.dir_edit = QLineEdit()
        self.dir_edit.setPlaceholderText("Leave empty to use the EXE’s folder")
        browse_dir = QPushButton("Browse…")
        browse_dir.clicked.connect(self._browse_dir)
        row2.addWidget(self.dir_edit)
        row2.addWidget(browse_dir)
        dir_lay.addLayout(row2)
        lay.addWidget(dir_box)

        icon_box = QGroupBox("Icon (optional)")
        icon_lay = QHBoxLayout(icon_box)
        self.icon_edit = QLineEdit()
        self.icon_edit.setPlaceholderText("PNG / SVG / ICO shown in Steam or your application menu")
        browse_icon = QPushButton("Browse…")
        browse_icon.clicked.connect(self._browse_icon)
        icon_lay.addWidget(self.icon_edit)
        icon_lay.addWidget(browse_icon)
        lay.addWidget(icon_box)

        mode_box = QGroupBox("How should it launch?")
        mode_lay = QVBoxLayout(mode_box)
        self.standalone_check = QCheckBox("Make standalone — launch without Steam (don’t add a Steam shortcut)")
        self.standalone_check.toggled.connect(self._on_mode_toggled)
        mode_lay.addWidget(self.standalone_check)
        self.mode_hint = _muted("")
        mode_lay.addWidget(self.mode_hint)
        self.menu_entry_check = QCheckBox("Add an entry to the application menu")
        self.menu_entry_check.setChecked(True)
        mode_lay.addWidget(self.menu_entry_check)
        lay.addWidget(mode_box)

        proton_box = QGroupBox("Proton version")
        proton_lay = QVBoxLayout(proton_box)
        self.proton_combo = QComboBox()
        self._populate_protons()
        proton_lay.addWidget(self.proton_combo)
        lay.addWidget(proton_box)

        runtime_box = QGroupBox("Windows components (winetricks)")
        runtime_lay = QVBoxLayout(runtime_box)
        self.profile_combo = QComboBox()
        for key in ("minimal", "standard", "extended"):
            self.profile_combo.addItem(key.capitalize(), key)
        idx = self.profile_combo.findData(self.config.verb_profile)
        self.profile_combo.setCurrentIndex(idx if idx >= 0 else 1)
        self.profile_combo.currentIndexChanged.connect(self._update_profile_hint)
        runtime_lay.addWidget(self.profile_combo)
        self.profile_hint = _muted("")
        runtime_lay.addWidget(self.profile_hint)
        lay.addWidget(runtime_box)
        self._update_profile_hint()

        opts = QGroupBox("Options")
        opts_lay = QVBoxLayout(opts)
        self.electron_check = QCheckBox(
            "Electron / Chromium-based game launcher (--no-sandbox, --disable-gpu-sandbox)"
        )
        opts_lay.addWidget(self.electron_check)

        win_row = QHBoxLayout()
        win_row.addWidget(QLabel("Windows version for prefix:"))
        self.win_version = QComboBox()
        self.win_version.addItem("Windows 11 (default)", "win11")
        self.win_version.addItem("Windows 10 (better for some older launchers)", "win10")
        self.win_version.addItem("Windows 7", "win7")
        win_row.addWidget(self.win_version)
        win_row.addStretch()
        opts_lay.addLayout(win_row)

        dpi_row = QHBoxLayout()
        dpi_row.addWidget(QLabel("DPI scaling:"))
        self.dpi_combo = QComboBox()
        for label, value in DPI_CHOICES:
            self.dpi_combo.addItem(label, value)
        dpi_row.addWidget(self.dpi_combo)
        dpi_row.addStretch()
        opts_lay.addLayout(dpi_row)

        self.launch_extra = QLineEdit()
        self.launch_extra.setPlaceholderText("Optional extra launch args after %command% (e.g. --fullscreen)")
        opts_lay.addWidget(self.launch_extra)

        self.wined3d_check = QCheckBox("PROTON_USE_WINED3D=1 (fallback OpenGL; troubleshooting)")
        self.no_esync_check = QCheckBox("PROTON_NO_ESYNC=1")
        self.no_fsync_check = QCheckBox("PROTON_NO_FSYNC=1")
        for cb in (self.wined3d_check, self.no_esync_check, self.no_fsync_check):
            opts_lay.addWidget(cb)

        self.block_steam_check = QCheckBox("Block setup while Steam is running (recommended for Steam mode)")
        self.block_steam_check.setChecked(True)
        opts_lay.addWidget(self.block_steam_check)

        self.advanced_check = QCheckBox("Show advanced winetricks options")
        self.advanced_check.toggled.connect(lambda on: self.extra_verbs_widget.setVisible(on))
        opts_lay.addWidget(self.advanced_check)

        self.extra_verbs_widget = QWidget()
        extra_lay = QVBoxLayout(self.extra_verbs_widget)
        extra_lay.setContentsMargins(0, 0, 0, 0)
        self.extra_verbs_checks: dict[str, QCheckBox] = {}
        extra_lay.addWidget(_muted("Optional extra verbs, installed on top of the selected component set:"))
        for v in OPTIONAL_VERBS:
            cb = QCheckBox(v)
            self.extra_verbs_checks[v] = cb
            extra_lay.addWidget(cb)
        self.extra_verbs_widget.setVisible(False)
        opts_lay.addWidget(self.extra_verbs_widget)
        lay.addWidget(opts)

        actions = QHBoxLayout()
        self.setup_btn = QPushButton("Set up game")
        self.setup_btn.setObjectName("primary")
        self.setup_btn.clicked.connect(self._run_setup)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._cancel_setup)
        actions.addWidget(self.setup_btn)
        actions.addWidget(self.cancel_btn)
        actions.addStretch()
        lay.addLayout(actions)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        lay.addWidget(self.progress)

        self.status_label = QLabel("Ready.")
        self.status_label.setWordWrap(True)
        lay.addWidget(self.status_label)

        self.log_view = self._make_log()
        lay.addWidget(self.log_view)
        lay.addStretch()

        self._on_mode_toggled(False)
        return self._wrap_scroll(w)

    def _populate_protons(self) -> None:
        self.proton_combo.clear()
        for p in self.protons:
            kind = "Steam" if p.is_steam_proton else "Custom"
            self.proton_combo.addItem(f"{p.name} ({kind})", p)
        if not self.protons:
            self.proton_combo.addItem("No Proton 10+ found", None)
        else:
            default_idx = next(
                (i for i, p in enumerate(self.protons) if p.is_experimental), 0
            )
            self.proton_combo.setCurrentIndex(default_idx)

    def _update_profile_hint(self) -> None:
        self.profile_hint.setText(PROFILE_DESCRIPTIONS.get(self.profile_combo.currentData(), ""))

    def _on_mode_toggled(self, standalone: bool) -> None:
        self.menu_entry_check.setVisible(standalone)
        self.block_steam_check.setEnabled(not standalone)
        if standalone:
            self.mode_hint.setText(
                "Standalone: no Steam shortcut. Griffin keeps the prefix in its own folder and writes a "
                "launcher script. No Steam overlay or Steam Input. Steam is still required for Proton itself."
            )
        else:
            self.mode_hint.setText(
                "Steam (recommended): the launcher is added as a non-Steam game, so you get the Steam "
                "overlay, controller support, and Steam’s container. Tick the box above for launchers "
                "that misbehave when started from Steam."
            )

    def _browse_exe(self) -> None:
        path = _pick_exe(self, "Select game launcher EXE", self.exe_edit.text().strip())
        if path:
            self.exe_edit.setText(path)
            p = Path(path)
            if not self.dir_edit.text().strip():
                self.dir_edit.setText(str(p.parent))
            if not self.name_edit.text().strip():
                self.name_edit.setText(p.parent.name or p.stem)

    def _browse_dir(self) -> None:
        start = self.dir_edit.text().strip() or self.exe_edit.text().strip() or str(Path.home())
        d = _pick_directory(self, "Start directory", start)
        if d:
            self.dir_edit.setText(d)

    def _browse_icon(self) -> None:
        p = _pick_icon(self, self.icon_edit.text().strip())
        if p:
            self.icon_edit.setText(p)

    def _confirm_steam_closed(self) -> bool:
        if not is_steam_running():
            return True
        if self.block_steam_check.isChecked():
            QMessageBox.warning(
                self,
                "Steam is running",
                "Steam is running and “Block setup while Steam is running” is enabled.\n\n"
                "Fully exit Steam (Steam → Exit), then try again.\n"
                "You can uncheck the block option if you understand the risk.",
            )
            return False
        r = QMessageBox.question(
            self,
            "Steam is running",
            "Steam is currently running. It may overwrite shortcuts.vdf when it exits.\n\n"
            "Continue anyway? (Recommended: fully exit Steam first.)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return r == QMessageBox.StandardButton.Yes

    def _run_setup(self) -> None:
        name = self.name_edit.text().strip()
        exe = self.exe_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "Griffin", "Enter a display name.")
            return
        if not exe or not Path(exe).is_file():
            QMessageBox.warning(self, "Griffin", "Select a valid .exe file.")
            return
        proton = self.proton_combo.currentData()
        if not proton:
            QMessageBox.warning(
                self, "Griffin",
                "No Proton 10+ found. Install Proton Experimental or GE-Proton 10+ in Steam.",
            )
            return
        standalone = self.standalone_check.isChecked()
        if not standalone and not self._confirm_steam_closed():
            return

        extra = [v for v, cb in self.extra_verbs_checks.items() if cb.isChecked()]
        start_dir = self.dir_edit.text().strip() or None
        icon = self.icon_edit.text().strip() or None

        self.setup_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.progress.setValue(0)
        self.status_label.setText("Starting setup…")
        self.log_view.clear()

        self._worker = SetupWorker(
            setup_game,
            name,
            Path(exe),
            proton,
            start_dir=Path(start_dir) if start_dir else None,
            is_electron=self.electron_check.isChecked(),
            extra_verbs=extra or None,
            icon_path=icon,
            windows_version=self.win_version.currentData() or "win11",
            extra_launch_options=self.launch_extra.text().strip(),
            require_steam_exit=False,  # the dialog above already handled it
            proton_use_wined3d=self.wined3d_check.isChecked(),
            proton_no_esync=self.no_esync_check.isChecked(),
            proton_no_fsync=self.no_fsync_check.isChecked(),
            standalone=standalone,
            verb_profile=self.profile_combo.currentData() or "standard",
            dpi=int(self.dpi_combo.currentData() or 0),
            create_menu_entry=self.menu_entry_check.isChecked(),
        )
        s = self._worker.signals
        s.status.connect(self.status_label.setText)
        s.log.connect(self.log_view.append)
        s.progress.connect(lambda p: self.progress.setValue(int(p * 100)))
        s.finished.connect(self._on_setup_finished)
        s.error.connect(self._on_setup_error)
        self._worker.start()

    def _on_setup_finished(self, result) -> None:
        self.setup_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.progress.setValue(100)
        warn = ""
        if getattr(result, "failed_verbs", None):
            warn += f"\nFailed verbs: {', '.join(result.failed_verbs)}"
        if getattr(result, "missing_dlls", None):
            warn += f"\nMissing DLLs: {', '.join(Path(m).name for m in result.missing_dlls)}"
        if result.launch_mode == "standalone":
            how = (
                f"Launch it from Managed Games"
                + (" or your application menu." if result.desktop_path else ".")
            )
            detail = f"Launcher: {result.launcher_path}"
        else:
            how = "Fully exit Steam, then start it again to see the game."
            detail = f"AppID: {result.app_id}"
        self.status_label.setText(f"Done — {result.name}. {how}{warn}")
        self._refresh_managed()
        QMessageBox.information(
            self, "Griffin",
            f"Setup complete!\n\nName: {result.name}\n{detail}\nPrefix: {result.prefix_path}{warn}\n\n"
            f"{how}\nUse Managed Games → Repair if something is still missing.",
        )

    def _on_setup_error(self, msg: str) -> None:
        self.setup_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        cancelled = "Cancelled" in msg
        self.status_label.setText("Cancelled — partial setup was cleaned up." if cancelled else f"Failed: {msg}")
        log_error(msg)
        if not cancelled:
            QMessageBox.critical(self, "Griffin", f"Setup failed:\n\n{msg}")

    def _cancel_setup(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self.status_label.setText("Cancelling…")

    # ------------------------------------------------------------------ Managed
    def _page_managed(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(24, 20, 24, 20)
        t = QLabel("Managed games")
        t.setObjectName("section")
        lay.addWidget(t)

        self.managed_list = QListWidget()
        self.managed_list.setMinimumHeight(160)
        lay.addWidget(self.managed_list)

        row = QHBoxLayout()
        self.launch_btn = QPushButton("Launch")
        self.launch_btn.setObjectName("primary")
        self.launch_btn.setToolTip("Standalone games only — Steam games are launched from Steam")
        self.launch_btn.clicked.connect(self._launch_managed)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self._refresh_managed)
        self.repair_btn = QPushButton("Repair prefix")
        self.repair_btn.setToolTip("Re-apply registry, DPI, Windows version; verify key DLLs")
        self.repair_btn.clicked.connect(self._repair_managed)
        self.rerun_btn = QPushButton("Re-run winetricks")
        self.rerun_btn.setToolTip("Re-install this game’s winetricks verbs on its prefix")
        self.rerun_btn.clicked.connect(self._rerun_winetricks_managed)
        open_pfx = QPushButton("Open prefix folder")
        open_pfx.clicked.connect(self._open_prefix)
        self.remove_btn = QPushButton("Remove…")
        self.remove_btn.setObjectName("danger")
        self.remove_btn.clicked.connect(self._remove_managed)
        for b in (self.launch_btn, refresh, self.repair_btn, self.rerun_btn, open_pfx, self.remove_btn):
            row.addWidget(b)
        row.addStretch()
        lay.addLayout(row)

        self.managed_detail = _muted("")
        lay.addWidget(self.managed_detail)

        self.managed_cancel_btn = QPushButton("Cancel task")
        self.managed_cancel_btn.setEnabled(False)
        self.managed_cancel_btn.clicked.connect(self._cancel_managed)
        lay.addWidget(self.managed_cancel_btn, alignment=Qt.AlignmentFlag.AlignLeft)

        self.managed_status = QLabel("")
        self.managed_status.setWordWrap(True)
        lay.addWidget(self.managed_status)
        self.managed_log = self._make_log(140)
        lay.addWidget(self.managed_log)
        lay.addStretch()

        self.managed_list.currentRowChanged.connect(self._on_managed_selected)
        self._managed_buttons = [self.launch_btn, self.repair_btn, self.rerun_btn, self.remove_btn]
        return self._wrap_scroll(w)

    def _refresh_managed(self) -> None:
        self.managed_list.clear()
        self._managed = ManagedGames.load()
        for g in self._managed:
            tag = "Standalone" if g.is_standalone else "Steam"
            self.managed_list.addItem(f"{g.name}   [{tag}]   (AppID {g.app_id})")
        self.managed_detail.setText(
            f"{len(self._managed)} managed game(s)." if self._managed else "No games set up yet."
        )
        self._update_managed_buttons()

    def _selected_managed(self) -> Optional[ManagedGame]:
        row = self.managed_list.currentRow()
        if row < 0 or row >= len(self._managed):
            return None
        return self._managed[row]

    def _update_managed_buttons(self) -> None:
        busy = self._worker is not None and self._worker.isRunning()
        g = self._selected_managed()
        self.launch_btn.setEnabled(bool(g and g.is_standalone) and not busy)
        for b in (self.repair_btn, self.rerun_btn, self.remove_btn):
            b.setEnabled(g is not None and not busy)

    def _on_managed_selected(self, row: int) -> None:
        self._update_managed_buttons()
        g = self._selected_managed()
        if not g:
            return
        lines = [
            f"Name: {g.name}",
            f"Mode: {'Standalone (no Steam)' if g.is_standalone else 'Steam non-Steam shortcut'}",
            f"AppID: {g.app_id}",
            f"EXE: {g.exe_path}",
            f"Start dir: {g.start_dir}",
            f"Prefix: {g.prefix_path}",
            f"Proton: {g.proton_config_name or '(unknown)'}",
            f"Components: {g.verb_profile}" + (f" + {', '.join(g.extra_verbs)}" if g.extra_verbs else ""),
        ]
        if g.is_standalone:
            lines.append(f"Launcher: {g.launcher_path}")
            if g.desktop_path:
                lines.append(f"Menu entry: {g.desktop_path}")
        lines.append(f"Created: {g.created}")
        self.managed_detail.setText("\n".join(lines))
        self.managed_detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

    def _resolve_proton_for_managed(self, g: ManagedGame):
        protons = find_steam_protons()
        if not protons:
            return None
        want = (g.proton_config_name or "").lower()
        for p in protons:
            if p.config_name.lower() == want or p.name.lower() == want:
                return p
        self.managed_log.append(
            f"Note: original Proton “{g.proton_config_name}” not found — using {protons[0].name}."
        )
        return protons[0]

    def _start_managed_task(self, label: str, fn, *args, **kwargs) -> None:
        self.managed_log.clear()
        self.managed_status.setText(label)
        self.managed_cancel_btn.setEnabled(True)
        self._worker = SetupWorker(fn, *args, **kwargs)
        s = self._worker.signals
        s.status.connect(self.managed_status.setText)
        s.log.connect(self.managed_log.append)
        s.finished.connect(self._on_managed_done)
        s.error.connect(self._on_managed_error)
        self._update_managed_buttons()
        self._worker.start()

    def _managed_task_over(self) -> None:
        self.managed_cancel_btn.setEnabled(False)
        # the QThread is still finishing when its signal fires; defer the button refresh a tick
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(150, self._update_managed_buttons)

    def _on_managed_done(self, result) -> None:
        self._managed_task_over()
        if isinstance(result, str):
            self.managed_status.setText(result)
        elif isinstance(result, list):
            self.managed_status.setText(
                "Done." + (f" Failed verbs: {', '.join(result)}" if result else " All verbs OK.")
            )
        self._refresh_managed()

    def _on_managed_error(self, msg: str) -> None:
        self._managed_task_over()
        self.managed_status.setText("Cancelled." if "Cancelled" in msg else f"Failed: {msg}")
        log_error(msg)

    def _cancel_managed(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self.managed_status.setText("Cancelling…")

    def _launch_managed(self) -> None:
        g = self._selected_managed()
        if not g or not g.is_standalone:
            return
        try:
            log_path = launch_standalone(g)
            self.managed_status.setText(f"Launched {g.name}. Output is logged to {log_path}")
        except Exception as e:  # noqa: BLE001
            self.managed_status.setText(f"Launch failed: {e}")
            QMessageBox.warning(self, "Griffin", str(e))

    def _repair_managed(self) -> None:
        g = self._selected_managed()
        if not g:
            QMessageBox.warning(self, "Griffin", "Select a managed game first.")
            return
        proton = self._resolve_proton_for_managed(g)
        if not proton:
            QMessageBox.warning(self, "Griffin", "No Proton found.")
            return
        self._start_managed_task(
            f"Repairing prefix for {g.name}…",
            repair_prefix, Path(g.prefix_path), proton,
            windows_version=g.windows_version, reinstall_verbs=False,
            extra_verbs=g.extra_verbs or None, verb_profile=g.verb_profile,
            dpi=g.dpi, exe_path=g.exe_path,
        )

    def _rerun_winetricks_managed(self) -> None:
        g = self._selected_managed()
        if not g:
            QMessageBox.warning(self, "Griffin", "Select a managed game first.")
            return
        proton = self._resolve_proton_for_managed(g)
        if not proton:
            QMessageBox.warning(self, "Griffin", "No Proton found.")
            return
        r = QMessageBox.question(
            self, "Re-run winetricks?",
            f"Re-install the “{g.verb_profile}” winetricks set on:\n{g.prefix_path}\n\n"
            "This can take several minutes.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if r != QMessageBox.StandardButton.Yes:
            return
        self._start_managed_task(
            f"Re-running winetricks for {g.name}…",
            rerun_winetricks, Path(g.prefix_path), proton,
            verb_profile=g.verb_profile, extra_verbs=g.extra_verbs or None,
        )

    def _remove_managed(self) -> None:
        g = self._selected_managed()
        if not g:
            return
        dlg = RemoveDialog(self, g)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        delete_prefix = dlg.prefix_cb.isChecked()
        remove_shortcut = dlg.shortcut_cb.isChecked() and not g.is_standalone
        if remove_shortcut and is_steam_running():
            QMessageBox.warning(self, "Steam is running", "Fully exit Steam before removing its shortcut.")
            return
        self._start_managed_task(
            f"Removing {g.name}…", remove_game, g,
            delete_prefix=delete_prefix, remove_shortcut=remove_shortcut,
        )

    def _open_prefix(self) -> None:
        g = self._selected_managed()
        if not g:
            return
        p = Path(g.prefix_path)
        if p.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))
        else:
            QMessageBox.warning(self, "Griffin", f"Prefix not found:\n{p}")

    # ------------------------------------------------------------------ Settings
    def _page_settings(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(24, 20, 24, 20)
        t = QLabel("Settings")
        t.setObjectName("section")
        lay.addWidget(t)

        steam_box = QGroupBox("Steam path (optional override)")
        steam_lay = QVBoxLayout(steam_box)
        row = QHBoxLayout()
        self.custom_steam = QLineEdit(self.config.custom_steam_path or "")
        self.custom_steam.setPlaceholderText("Leave empty for auto-detect")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_steam)
        row.addWidget(self.custom_steam)
        row.addWidget(browse)
        steam_lay.addLayout(row)
        self.steam_label = _muted("")
        steam_lay.addWidget(self.steam_label)
        lay.addWidget(steam_box)
        self._update_steam_label()

        defaults_box = QGroupBox("Defaults")
        d_lay = QVBoxLayout(defaults_box)
        prow = QHBoxLayout()
        prow.addWidget(QLabel("Default component set for new games:"))
        self.default_profile = QComboBox()
        for key in ("minimal", "standard", "extended"):
            self.default_profile.addItem(key.capitalize(), key)
        i = self.default_profile.findData(self.config.verb_profile)
        self.default_profile.setCurrentIndex(i if i >= 0 else 1)
        prow.addWidget(self.default_profile)
        prow.addStretch()
        d_lay.addLayout(prow)

        wrow = QHBoxLayout()
        wrow.addWidget(QLabel("Winetricks source:"))
        self.wt_channel = QComboBox()
        self.wt_channel.addItem("Pinned release (checksum-verified) — recommended", "pinned")
        self.wt_channel.addItem("Latest from GitHub master (unverified)", "latest")
        j = self.wt_channel.findData(self.config.winetricks_channel)
        self.wt_channel.setCurrentIndex(j if j >= 0 else 0)
        wrow.addWidget(self.wt_channel)
        wrow.addStretch()
        d_lay.addLayout(wrow)

        self.broad_mounts_check = QCheckBox(
            "Expose ALL mounted volumes to Steam’s container (Steam mode; weakens isolation)"
        )
        self.broad_mounts_check.setChecked(self.config.broad_mounts)
        d_lay.addWidget(self.broad_mounts_check)
        d_lay.addWidget(
            _muted("Off = only your Steam libraries and the drives they live on are visible to the game. "
                   "Applies to games set up from now on.")
        )
        save = QPushButton("Save settings")
        save.setObjectName("primary")
        save.clicked.connect(self._save_settings)
        d_lay.addWidget(save, alignment=Qt.AlignmentFlag.AlignLeft)
        lay.addWidget(defaults_box)

        tools_box = QGroupBox("Tools")
        t_lay = QVBoxLayout(tools_box)
        trow = QHBoxLayout()
        open_logs = QPushButton("Open log folder")
        open_logs.clicked.connect(self._open_logs)
        self.sys_deps_btn = QPushButton("Install system winetricks / cabextract (admin)…")
        self.sys_deps_btn.clicked.connect(self._install_system_deps)
        has_helper = SYSTEM_HELPER.is_file()
        self.sys_deps_btn.setEnabled(has_helper)
        if not has_helper:
            self.sys_deps_btn.setToolTip("Available when Griffin is installed from the .deb package")
        trow.addWidget(open_logs)
        trow.addWidget(self.sys_deps_btn)
        trow.addStretch()
        t_lay.addLayout(trow)
        t_lay.addWidget(_muted("Normal setup never needs root. The admin helper is optional."))
        lay.addWidget(tools_box)

        verbs_box = QGroupBox("Component sets (winetricks verbs)")
        v_lay = QVBoxLayout(verbs_box)
        for key in ("minimal", "standard", "extended"):
            lbl = QLabel(f"<b>{key.capitalize()}</b>: {', '.join(PROFILES[key])}")
            lbl.setWordWrap(True)
            lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            v_lay.addWidget(lbl)
        lay.addWidget(verbs_box)

        lay.addStretch()
        return self._wrap_scroll(w)

    def _update_steam_label(self) -> None:
        self.steam_label.setText(f"Detected: {self.steam_path}" if self.steam_path else "Steam not detected.")

    def _browse_steam(self) -> None:
        d = _pick_directory(self, "Steam folder", str(Path.home() / ".steam"))
        if d:
            self.custom_steam.setText(d)

    def _save_settings(self) -> None:
        self.config.custom_steam_path = self.custom_steam.text().strip()
        self.config.verb_profile = self.default_profile.currentData() or "standard"
        self.config.winetricks_channel = self.wt_channel.currentData() or "pinned"
        self.config.broad_mounts = self.broad_mounts_check.isChecked()
        self.config.save()
        self.steam_path = detect_steam_path_checked()
        self.protons = find_steam_protons()
        self._populate_protons()
        idx = self.profile_combo.findData(self.config.verb_profile)
        if idx >= 0:
            self.profile_combo.setCurrentIndex(idx)
        self._update_steam_label()
        QMessageBox.information(self, "Griffin", "Settings saved.")

    def _open_logs(self) -> None:
        lp = get_log_path()
        folder = lp.parent if lp else AppConfig.default_cache_dir()
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _install_system_deps(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            QMessageBox.information(self, "Griffin", "Another task is running.")
            return
        r = QMessageBox.question(
            self, "Install system packages?",
            "This asks for your administrator password (pkexec) and installs the distro "
            "packages winetricks and cabextract.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if r != QMessageBox.StandardButton.Yes:
            return
        self._goto(Page.MANAGED)

        def run_helper(ctx: TaskContext) -> str:
            ctx.set_status("Waiting for administrator authentication…")
            code = ctx.run_cancellable(_deps_command(), env=clean_env())
            if code != 0:
                raise RuntimeError(f"Helper exited with code {code}")
            return "System packages installed."

        self._start_managed_task("Installing system packages…", run_helper)

    # ------------------------------------------------------------------ About
    def _page_about(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(24, 20, 24, 20)
        t = QLabel("About")
        t.setObjectName("section")
        lay.addWidget(t)
        lay.addWidget(QLabel(f"Griffin Game Tweaks v{__version__}"))
        lay.addWidget(QLabel("by Bobby Comet"))
        lay.addWidget(
            _muted(
                "Automates Wine/Proton setup for Windows games that ship with their own launcher EXE "
                "(updates, login, anti-cheat, etc.). Creates a Proton prefix, installs common runtimes "
                "via winetricks, and either adds the game as a non-Steam title in Steam or makes it "
                "launchable standalone.\n\n"
                "Not for storefront clients (Epic, GOG, EA App, …) — for the game’s own launcher.\n\n"
                "Based on architecture patterns from Kalium (MO2 helper)."
            )
        )
        lay.addStretch()
        return self._wrap_scroll(w)

    # ------------------------------------------------------------------ lifecycle
    def closeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        if self._worker is not None and self._worker.isRunning():
            r = QMessageBox.question(
                self, "Task running",
                "A task is still running. Cancel it and quit?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if r != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._worker.cancel()
            self._worker.wait(8000)
        event.accept()


def run_app() -> None:
    init_logger()
    log_info("Griffin Game Tweaks GUI starting...")
    app = QApplication(sys.argv)
    app.setApplicationName("Griffin Game Tweaks")
    app.setOrganizationName("Bobby Comet")
    app.setDesktopFileName("griffin-game-tweaks")  # matches the .desktop file (taskbar icon on Wayland)
    # Use a sans-serif UI font even if the system default resolves to a serif face.
    font = QFont()
    font.setFamilies(["Noto Sans", "Inter", "Cantarell", "DejaVu Sans", "Liberation Sans", "Sans Serif"])
    font.setStyleHint(QFont.StyleHint.SansSerif)
    font.setPointSize(10)
    app.setFont(font)
    icon = _app_icon()
    if not icon.isNull():
        app.setWindowIcon(icon)
    app.setStyleSheet(STYLESHEET)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
