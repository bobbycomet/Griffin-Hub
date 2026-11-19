#!/usr/bin/env python3
"""Griffin Play — PyQt6 presentation layer."""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path

from PyQt6.QtCore import Qt, QObject, pyqtSignal, QThreadPool, QRunnable, QTimer
from PyQt6.QtGui import QFont, QColor, QIcon, QAction
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QTreeWidget, QTreeWidgetItem,
    QTextEdit, QPlainTextEdit, QPushButton, QCheckBox, QComboBox, QFrame,
    QMessageBox, QSplitter, QGroupBox, QSizePolicy, QAbstractItemView,
)

from . import core_logic as core

STAT_COLORS = {
    "native": "#38bdf8", "pending": "#b2bdd1", "unknown": "#b2bdd1",
    "platinum": "#4ade80", "gold": "#a3e635", "silver": "#facc15",
    "bronze": "#fb923c", "borked": "#f87171", "blocked": "#f87171",
}

DARK_QSS = """
QMainWindow, QDialog, QMenu {
    background-color: #262b36;
}
/* Do NOT give plain QWidget a background: it would paint an opaque box
   behind every QLabel/QCheckBox and clash with the Hub's cards. */
QWidget {
    background-color: transparent;
    color: #eef2fa;
    font-size: 14px;
}
QLabel#title {
    font-size: 22px;
    font-weight: bold;
    color: white;
}
QLabel#headerMeta {
    color: #b2bdd1;
    font-size: 14px;
}
QLabel#banner {
    color: #fbbf24;
    font-weight: bold;
}
QLabel#statusLine {
    color: #b2bdd1;
}
QFrame#header {
    background-color: #1f3a73;
}
QListWidget, QTreeWidget, QPlainTextEdit, QTextEdit, QLineEdit, QComboBox {
    background-color: #2f3644;
    color: #eef2fa;
    border: 1px solid #3a4a6b;
    border-radius: 4px;
    padding: 4px;
}
QTreeWidget::item:selected, QListWidget::item:selected {
    background-color: #3d7bff;
    color: white;
}
QHeaderView::section {
    background-color: #1f3a73;
    color: #eef2fa;
    padding: 6px;
    border: none;
    font-weight: bold;
}
QHeaderView::section:hover {
    background-color: #3d7bff;
    color: white;
}
QPushButton {
    background-color: #1f3a73;
    color: #eef2fa;
    border: none;
    border-radius: 4px;
    padding: 8px 12px;
}
QPushButton:hover {
    background-color: #3d7bff;
    color: white;
}
QPushButton#primary {
    background-color: #3d7bff;
    color: white;
    font-weight: bold;
    padding: 10px 14px;
}
QPushButton#primary:hover {
    background-color: #5b93ff;
}
QCheckBox {
    spacing: 8px;
}
QComboBox QAbstractItemView {
    background-color: #2f3644;
    color: #eef2fa;
    selection-background-color: #3d7bff;
}
QGroupBox {
    border: 1px solid #3a4a6b;
    border-radius: 4px;
    margin-top: 10px;
    padding-top: 8px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 8px;
    padding: 0 4px;
    color: #b2bdd1;
}
QPlainTextEdit#log {
    background-color: #1c2029;
    color: #b9c9e6;
    font-family: "DejaVu Sans Mono", monospace;
    font-size: 14px;
}
"""


class Signals(QObject):
    log = pyqtSignal(str)
    next_step = pyqtSignal(str)
    fill = pyqtSignal()
    show_game = pyqtSignal()
    banner = pyqtSignal()
    info = pyqtSignal(str, str)  # title, text
    ask = pyqtSignal(str, str)   # handled via reply queue — use messagebox on main only


class ApplyWorker(QRunnable):
    def __init__(self, win: "MainWindow", game, dry, admin, force):
        super().__init__()
        self.win = win
        self.game = game
        self.dry = dry
        self.admin = admin
        self.force = force

    def run(self):
        try:
            self.win._apply_body(self.game, self.dry, self.admin, self.force)
        except Exception as e:
            self.win.signals.log.emit(f"! Error: {e}")
            self.win.signals.next_step.emit(f"Error: {e}")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Griffin Play {core.__version__}")
        self.resize(1400, 900)
        self.setMinimumSize(1100, 700)
        icon = core.HERE / "icon.png"
        if icon.exists():
            self.setWindowIcon(QIcon(str(icon)))

        self.signals = Signals()
        self.signals.log.connect(self._append_log)
        self.signals.next_step.connect(self._set_next)
        self.signals.fill.connect(self.fill)
        self.signals.show_game.connect(self.show_game)
        self.signals.banner.connect(self._update_session_banner)

        self.db = core.load_db()
        self.env = core.detect()
        self.cat = "All"
        self.last_applied = {}
        self.history = core.history_load()
        self.similar = {}
        self.next_action = ""
        self.pdb_online = False  # set True after a successful live fetch
        self.pool = QThreadPool.globalInstance()

        self.scan()
        # Curated JSON + every installed Steam title; ProtonDB is live tier source
        self.games = core.merge_library(self.db, self.apps)
        self.pdb = core.pdb_load()
        self._build()
        self.fill()
        # Prefer refreshing installed + curated IDs from ProtonDB first
        prefer = list(self.apps.keys()) + [a for a in self.games if a not in self.apps]
        self.sync(prefer[:400])  # cap burst; rest load on select
        QTimer.singleShot(800, self._check_updates)
        QTimer.singleShot(400, self._refresh_ge_pins)
        QTimer.singleShot(500, self._first_run_hint)
        QTimer.singleShot(200, self._update_session_banner)

    # ----- data helpers -----
    def scan(self):
        try:
            self.roots = core.steam_roots()
        except Exception as e:
            self.roots = []
            print(f"Griffin Play: steam_roots failed: {e}", flush=True)
        self.root_dir = self.roots[0] if self.roots else None
        try:
            self.libs = core.libraries(self.root_dir) if self.root_dir else []
        except Exception as e:
            self.libs = [self.root_dir] if self.root_dir else []
            print(f"Griffin Play: libraries failed: {e}", flush=True)
        try:
            self.apps = core.installed_apps(self.libs)
        except Exception as e:
            self.apps = {}
            print(f"Griffin Play: installed_apps failed: {e}", flush=True)
        if hasattr(self, "db"):
            self.games = core.merge_library(self.db, self.apps)

    def log(self, msg):
        self.signals.log.emit(str(msg))

    def _append_log(self, msg):
        self.logw.appendPlainText(msg)
        self.logw.verticalScrollBar().setValue(self.logw.verticalScrollBar().maximum())

    def _set_next(self, msg):
        self.next_action = msg or ""
        self.status_line.setText(msg or "")

    def tier(self, a):
        """Live ProtonDB tier when available; JSON / native fallback when offline."""
        g = self.games.get(a) or {}
        live = (self.pdb.get(a) or {}).get("tier")
        if live:
            return live
        offline = (g.get("protondb") or {}).get("tier")
        if offline:
            return offline
        if core.is_native_linux(proton=g.get("proton")):
            return "native"
        return "unknown"

    def is_native(self, a):
        g = self.games.get(a) or {}
        return core.is_native_linux(self.tier(a), g.get("proton"))

    def sync(self, ids):
        def w():
            touched = core.pdb_fetch([str(i) for i in ids], self.pdb)
            if touched:
                self.pdb_online = True
                try:
                    self.signals.fill.emit()
                    # light status touch on UI thread
                    self.signals.next_step.emit(
                        f"ProtonDB live · {len(self.pdb)} summaries cached"
                    )
                except RuntimeError:
                    return  # window was closed while the lookup was running
        threading.Thread(target=w, daemon=True).start()

    def plan(self, g, launch_override=None):
        fam = self.env["family"]
        ov = g.get("distro_overrides", {}).get(fam, {})
        prof = self.db["distro_profiles"].get(fam, {})
        env = {**g.get("env", {}), **ov.get("env", {})}
        # Native Linux: never force Proton-oriented defaults
        if core.is_native_linux((self.pdb.get(str(g.get("appid",""))) or {}).get("tier"), g.get("proton")):
            default_launch = "%command%"
        else:
            default_launch = "gamemoderun %command%"
        base = ov.get("launch_options", g.get("launch_options", default_launch))
        if fam == "steamos":
            base = base.replace("gamemoderun ", "")
        if launch_override:
            base = launch_override
            if "=" in launch_override and "%command%" in launch_override:
                env = {}
                base = launch_override
        pkgs = list(dict.fromkeys(prof.get("packages", []) + ov.get("extra_packages", [])))
        proton = g.get("proton", "ge")
        # Live ProtonDB "native" tier means native Linux — prefer that over JSON proton default
        a = str(g.get("appid", ""))
        live_tier = (self.pdb.get(a) or {}).get("tier")
        if core.is_native_linux(live_tier, proton):
            proton = "native"
        pov = self.proton_ov.currentText()
        if pov and pov != "(database)":
            proton = pov
        return dict(
            launch=" ".join([f"{k}={v}" for k, v in env.items()] + [base]).strip(),
            pkgs=pkgs, pre=prof.get("pre"), note=prof.get("note"),
            verbs=list(dict.fromkeys(g.get("winetricks", []) + ov.get("winetricks", []))),
            rt=[self.db["runtimes"][r] for r in g.get("runtimes", []) if r in self.db.get("runtimes", {})],
            proton=proton,
        )

    def cur(self):
        items = self.tree.selectedItems()
        if not items:
            return None
        return self.games.get(items[0].data(0, Qt.ItemDataRole.UserRole))

    # ----- UI construction -----
    def _build(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header
        header = QFrame()
        header.setObjectName("header")
        hl = QHBoxLayout(header)
        title = QLabel(f"Griffin Play {core.__version__}")
        title.setObjectName("title")
        hl.addWidget(title)
        hl.addStretch()
        e = self.env
        gpu_label = e.get("gpu_short") or core.gpu_display_name(e.get("gpu") or "")
        disp = f"{e.get('session', '?')}/{e.get('compositor', '?')}"
        steam = f"{len(self.roots)} Steam root(s)" if self.roots else "Steam not found"
        meta = QLabel(
            f"{e['name']}  |  {e['family']}"
            f"{' (immutable)' if e['immutable'] else ''}  |  {e['kernel']}"
            + (f"  |  {gpu_label}" if gpu_label else "")
            + f"  |  {disp}  |  {steam}"
        )
        meta.setObjectName("headerMeta")
        hl.addWidget(meta)
        root.addWidget(header)

        body = QHBoxLayout()
        body.setContentsMargins(12, 12, 12, 8)
        root.addLayout(body, stretch=1)

        # Categories
        left = QVBoxLayout()
        left.addWidget(QLabel("Categories"))
        self.cats = ["All", "Installed"] + sorted({
            c for g in self.games.values() for c in g.get("categories", [])
        })
        self.lb = QListWidget()
        for c in self.cats:
            self.lb.addItem(c)
        self.lb.setCurrentRow(0)
        self.lb.currentTextChanged.connect(self.pick_cat)
        left.addWidget(self.lb)
        body.addLayout(left, stretch=0)
        self.lb.setMinimumWidth(140)
        self.lb.setMaximumWidth(180)

        # Game list
        mid = QVBoxLayout()
        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search games…")
        self.search.textChanged.connect(self.fill)
        top.addWidget(self.search)
        self.hide_broken = QCheckBox("Hide not-working")
        self.hide_broken.setChecked(True)
        self.hide_broken.toggled.connect(self.fill)
        top.addWidget(self.hide_broken)
        mid.addLayout(top)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Game  (● = installed)", "Categories", "Status", "Anti-cheat"])
        self.tree.setColumnWidth(0, 300)
        self.tree.setColumnWidth(1, 180)
        self.tree.setColumnWidth(2, 120)
        self.tree.setColumnWidth(3, 110)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(False)
        self.tree.itemSelectionChanged.connect(self.show_game)
        mid.addWidget(self.tree)
        body.addLayout(mid, stretch=1)

        # Right panel
        right = QVBoxLayout()
        right.setSpacing(6)
        self.info = QTextEdit()
        self.info.setReadOnly(True)
        self.info.setMinimumWidth(420)
        right.addWidget(self.info, stretch=1)

        self.banner = QLabel("")
        self.banner.setObjectName("banner")
        self.banner.setWordWrap(True)
        right.addWidget(self.banner)

        # Primary actions
        row = QHBoxLayout()
        self.btn_make = QPushButton("Make it work")
        self.btn_make.setObjectName("primary")
        self.btn_make.clicked.connect(self.make_it_work)
        row.addWidget(self.btn_make)
        btn_preview = QPushButton("Preview changes")
        btn_preview.clicked.connect(self.preview_changes)
        row.addWidget(btn_preview)
        btn_launch = QPushButton("Launch")
        btn_launch.clicked.connect(self.launch)
        row.addWidget(btn_launch)
        right.addLayout(row)

        row2 = QHBoxLayout()
        btn_sug = QPushButton("Use suggested opts")
        btn_sug.clicked.connect(self.use_suggested)
        row2.addWidget(btn_sug)
        btn_quit = QPushButton("Quit Steam & retry")
        btn_quit.clicked.connect(self.quit_steam_retry)
        row2.addWidget(btn_quit)
        right.addLayout(row2)

        self.adv_toggle = QCheckBox("Customize (Proton, admin, advanced)")
        self.adv_toggle.toggled.connect(self._toggle_adv)
        right.addWidget(self.adv_toggle)

        self.adv = QGroupBox("Advanced")
        self.adv.setVisible(False)
        adv_l = QVBoxLayout(self.adv)
        ovf = QHBoxLayout()
        ovf.addWidget(QLabel("Proton:"))
        self.proton_ov = QComboBox()
        self.proton_ov.addItems(["(database)", "ge", "experimental", "hotfix", "native"])
        ovf.addWidget(self.proton_ov)
        ovf.addWidget(QLabel("GE pin:"))
        self.ge_pin = QComboBox()
        self.ge_pin.setEditable(True)
        self.ge_pin.addItem("(latest)")
        ovf.addWidget(self.ge_pin)
        adv_l.addLayout(ovf)
        self.sysx = QCheckBox("Allow admin steps (packages, sysctl via Polkit)")
        adv_l.addWidget(self.sysx)
        self.force_vdf = QCheckBox("Force rewrite Steam configs (if parse fails)")
        adv_l.addWidget(self.force_vdf)
        ar = QHBoxLayout()
        for text, slot in [
            ("Copy opts", self.copy), ("ProtonDB", self.open_pdb),
            ("Restore backup", self.restore),
        ]:
            b = QPushButton(text)
            b.clicked.connect(slot)
            ar.addWidget(b)
        adv_l.addLayout(ar)
        ar2 = QHBoxLayout()
        for text, slot in [
            ("Refresh reports", self.refresh_reports),
            ("Reuse last good", self.reuse_history),
            ("Health check", self.health_check),
        ]:
            b = QPushButton(text)
            b.clicked.connect(slot)
            ar2.addWidget(b)
        adv_l.addLayout(ar2)
        ar3 = QHBoxLayout()
        for text, slot in [
            ("Prepare this PC", self.prepare_pc),
            ("Report issue", self.report_issue),
        ]:
            b = QPushButton(text)
            b.clicked.connect(slot)
            ar3.addWidget(b)
        adv_l.addLayout(ar3)
        right.addWidget(self.adv)

        self.status_line = QLabel("")
        self.status_line.setObjectName("statusLine")
        self.status_line.setWordWrap(True)
        right.addWidget(self.status_line)
        body.addLayout(right, stretch=0)

        # Log
        self.logw = QPlainTextEdit()
        self.logw.setObjectName("log")
        self.logw.setReadOnly(True)
        self.logw.setMaximumHeight(180)
        root.addWidget(self.logw)

        self.dry = False  # toggled by preview

    def _toggle_adv(self, on):
        self.adv.setVisible(on)
        if on:
            self._refresh_ge_pins()

    def pick_cat(self, text):
        self.cat = text or "All"
        self.fill()

    def fill(self):
        keep = None
        items = self.tree.selectedItems()
        if items:
            keep = items[0].data(0, Qt.ItemDataRole.UserRole)
        self.tree.clear()
        q = self.search.text().lower()
        for a, g in sorted(self.games.items(), key=lambda x: x[1]["name"]):
            inst = a in self.apps
            if self.cat == "Installed" and not inst:
                continue
            if self.cat not in ("All", "Installed") and self.cat not in g.get("categories", []):
                continue
            if q and q not in g["name"].lower():
                continue
            t = self.tier(a)
            if self.hide_broken.isChecked() and (g.get("blocked") or t == "borked"):
                continue
            label = core.status_label(t, blocked=g.get("blocked"), proton=g.get("proton"))
            cats = list(g.get("categories") or [])
            if g.get("_source") == "steam" and "Installed" not in cats:
                cats = ["Installed"] + cats
            item = QTreeWidgetItem([
                ("● " if inst else "") + g.get("name", a),
                ", ".join(cats),
                label,
                g.get("anticheat") or "none",
            ])
            item.setData(0, Qt.ItemDataRole.UserRole, a)
            if g.get("blocked"):
                color = STAT_COLORS["blocked"]
            elif core.is_native_linux(t, g.get("proton")):
                color = STAT_COLORS["native"]
            else:
                color = STAT_COLORS.get(t, "#eef2fa")
            item.setForeground(2, QColor(color))
            self.tree.addTopLevelItem(item)
            if keep and a == keep:
                item.setSelected(True)

    def show_game(self):
        g = self.cur()
        if not g:
            self.info.clear()
            return
        p = self.plan(g)
        a = str(g["appid"])
        d = self.pdb.get(a, {})
        self.sync([a])
        self._load_similar(a)
        tier = self.tier(a)
        st = core.status_label(tier, blocked=g.get("blocked"), proton=g.get("proton"))
        native = core.is_native_linux(tier, g.get("proton"))
        lines = [f"<h2 style='color:white'>{g.get('name', a)}</h2>"]
        lines.append(
            f"<p style='color:#b2bdd1'>{g.get('engine', '')}  |  anti-cheat: {g.get('anticheat', 'none')}"
            + ("  |  <b style='color:#f87171'>BLOCKED by developer</b>" if g.get("blocked") else "")
            + "</p>"
        )
        # Data source badge
        src_bits = []
        if d.get("tier"):
            src_bits.append("ProtonDB live" if self.pdb_online or d.get("t") else "ProtonDB cache")
        elif g.get("protondb"):
            src_bits.append("offline JSON tier")
        else:
            src_bits.append("no tier yet")
        if g.get("_source") == "steam":
            src_bits.append("installed (not curated)")
        elif g.get("_source") == "json":
            src_bits.append("curated JSON")
        lines.append(
            f"<p style='color:#94a1b8;font-size:13px'>Data: {' · '.join(src_bits)}</p>"
        )
        if native:
            lines.append(
                "<div style='background:#0c2a3a;border:2px solid #38bdf8;border-radius:8px;"
                "padding:12px 14px;margin:8px 0'>"
                "<div style='color:#38bdf8;font-size:17px;font-weight:bold'>"
                "🐧 Runs natively on Linux</div>"
                "<div style='color:#eef2fa;margin-top:6px'>"
                "This title has a <b>native Linux build</b> — it does <b>not</b> need Proton/Wine. "
                "Griffin Play will leave Proton unset and use plain launch options "
                "(<code>%command%</code>).</div>"
                "</div>"
            )
        extra = f"  ({d['total']} reports, trending {d['trending']})" if d.get("total") else ""
        if native:
            # Still show PDB rating for the Windows build if present, but clarify
            pdb_note = ""
            if tier and tier != "native":
                pdb_note = f"<br>ProtonDB also lists a Windows/Proton rating: <b>{tier.title()}</b>{extra}"
            lines.append(
                f"<p><b style='color:#b2bdd1'>Status</b><br>"
                f"<span style='color:#38bdf8;font-weight:bold'>Native Linux</span>"
                f"{pdb_note}<br>"
                f"<a href='https://www.protondb.com/app/{a}'>protondb.com/app/{a}</a></p>"
            )
        else:
            lines.append(f"<p><b style='color:#b2bdd1'>Status</b><br>{st}{extra}<br>"
                         f"<a href='https://www.protondb.com/app/{a}'>protondb.com/app/{a}</a></p>")
        if g.get("blocked"):
            lines.append(
                "<p><b style='color:#f87171'>Blocked title</b><br>"
                "The developer blocks this game on Linux/Proton. Griffin Play will not force a bypass.</p>"
            )
        if self.env.get("immutable"):
            lines.append(
                "<p><b style='color:#fbbf24'>Immutable system</b><br>"
                "Atomic/immutable OS — package installs skipped; Proton & launch options still apply.</p>"
            )
        lines.append(f"<p><b style='color:#b2bdd1'>Notes</b><br>{g.get('notes') or '-'}</p>")
        ov = "" if self.proton_ov.currentText() == "(database)" else "  [override]"
        if p["proton"] == "native" or native:
            lines.append(
                f"<p><b style='color:#b2bdd1'>Compatibility</b><br>"
                f"<span style='color:#38bdf8'>Native Linux</span> — no Proton tool"
                f"{ov}</p>"
            )
        else:
            lines.append(f"<p><b style='color:#b2bdd1'>Proton</b><br>{p['proton']}{ov}</p>")
        lines.append(f"<p><b style='color:#b2bdd1'>Launch options</b><br><code>{p['launch']}</code></p>")
        la = self.last_applied.get(a)
        if la:
            lines.append(
                f"<p><b style='color:#b2bdd1'>Last applied this session</b><br>"
                f"tool={la.get('tool') or 'none'}<br>{la.get('launch', '')}<br>@ {la.get('at', '')}</p>"
            )
        hist = self.history.get(a) or core.history_load().get(a)
        if hist:
            lines.append(
                f"<p><b style='color:#b2bdd1'>Last known good (saved)</b><br>"
                f"tool={hist.get('tool') or 'none'}  via {hist.get('source', '?')}<br>"
                f"{hist.get('launch', '')}<br>@ {hist.get('at', '')}</p>"
            )
        sims = self.similar.get(a) or []
        if sims:
            parts = []
            for i, s in enumerate(sims[:5], 1):
                parts.append(
                    f"#{i} {s.get('confidence', 'match')} (score={s['score']})<br>"
                    f"{s['os']} | {s['gpu'][:36]}<br>"
                    f"Display: {s.get('session', '?')} / {s.get('compositor', '?')}<br>"
                    f"Proton: {s['proton']}<br>"
                    f"Launch: {s['launchOptions'] or '(none listed)'}<br>"
                    f"{s['notes'][:120]}"
                )
            lines.append(
                "<p><b style='color:#b2bdd1'>Similar-system ProtonDB reports</b><br>"
                + "<br><br>".join(parts)
                + "<br><br>Use suggested opts to apply #1's launch command.</p>"
            )
        elif a in self.similar:
            lines.append("<p><b style='color:#b2bdd1'>Similar-system ProtonDB reports</b><br>(none matched yet)</p>")
        self.info.setHtml("".join(lines))

    # ----- actions -----
    def make_it_work(self):
        self.dry = False
        self.apply()

    def preview_changes(self):
        self.dry = True
        self.apply()
        self.dry = False

    def apply(self):
        g = self.cur()
        if not g:
            return
        if not self.root_dir:
            QMessageBox.critical(self, "Griffin Play", "No Steam installation found.")
            return
        if core.steam_running() and not self.dry:
            self._update_session_banner()
            if QMessageBox.question(
                    self, "Steam is running",
                    "Steam is open. Quit Steam and apply now?") == QMessageBox.StandardButton.Yes:
                self.quit_steam_retry()
            return
        if not self.dry:
            if QMessageBox.question(
                    self, "Make it work",
                    f"Apply recommended Proton + launch options for {g['name']}?\n"
                    "Steam configs are backed up first."
            ) != QMessageBox.StandardButton.Yes:
                return
        if g.get("blocked") and not self.dry:
            if QMessageBox.question(
                    self, "Blocked title",
                    f"{g['name']} is marked blocked by the developer on Linux.\n\n"
                    "Continue anyway? (May still fail to run.)"
            ) != QMessageBox.StandardButton.Yes:
                return
        worker = ApplyWorker(self, g, self.dry, self.sysx.isChecked(), self.force_vdf.isChecked())
        self.pool.start(worker)

    def run_cmd(self, cmd, dry):
        self.log("$ " + " ".join(map(shlex.quote, cmd)))
        if dry:
            return
        pr = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in pr.stdout:
            self.log("  " + line.rstrip())
        if pr.wait():
            self.log(f"  ! exit code {pr.returncode}")

    def _prefix_exists(self, appid):
        return any((l / "steamapps/compatdata" / str(appid)).is_dir() for l in self.libs)

    def _wait_for_prefix(self, appid, log, timeout=120):
        log(f"Waiting up to {timeout}s for Steam to create the prefix…")
        for i in range(timeout):
            if self._prefix_exists(appid):
                log(f"Prefix detected after ~{i}s.")
                return True
            time.sleep(1)
        log("! Prefix not detected in time. Close the game and Apply again later.")
        return False

    def _apply_body(self, g, dry, admin, force=False):
        """Worker-thread apply (same pipeline as before; logs via signals)."""
        L = self.log
        p = self.plan(g)
        a = str(g["appid"])
        root = self.root_dir
        L(f"--- {g['name']} {'(preview)' if dry else ''} ---")
        try:
            if g.get("blocked"):
                L("! BLOCKED by developer on Linux — applying may not help.")
                self.signals.next_step.emit("Blocked title — see ProtonDB; do not expect a full fix.")
            if self.tier(a) == "borked":
                L("! Reported as not working: " + g.get("notes", ""))
            tool, kind = None, p["proton"]
            if kind == "ge":
                pin = self.ge_pin.currentText()
                pin = None if not pin or pin == "(latest)" else pin
                try:
                    tool = core.install_ge(root, L, dry, pin=pin)
                    self.signals.banner.emit()  # refresh GE list indirectly
                except Exception as e:
                    L(f"! GE-Proton install failed: {e}")
                    have = core.ge_installed(root)
                    if have:
                        tool = have[-1]
                        L(f"  Falling back to already-installed {tool}")
                    else:
                        tool = None
                        self.signals.next_step.emit("Next: install GE-Proton (or pick a pin) and Make it work again.")
            elif kind in core.VALVE:
                tool = "proton_" + kind
                try:
                    self._steam_install(core.VALVE[kind], dry)
                except Exception as e:
                    L(f"! {e}")
            elif kind == "native":
                tool = None
            for r in p["rt"]:
                try:
                    self._steam_install(r, dry)
                except Exception as e:
                    L(f"! runtime {r}: {e}")
            pt = shutil.which("protontricks")
            if not pt and shutil.which("flatpak"):
                if subprocess.run(
                        ["flatpak", "info", "com.github.Matoking.protontricks"],
                        capture_output=True).returncode == 0:
                    pt = ["flatpak", "run", "com.github.Matoking.protontricks"]
            else:
                pt = [pt] if pt else None
            if p["verbs"]:
                if not pt:
                    L("! protontricks not found — skipping winetricks verbs")
                    L("  Install: flatpak install com.github.Matoking.protontricks")
                    self.signals.next_step.emit("Next: install protontricks, then Make it work again.")
                elif not self._prefix_exists(a):
                    L("! No Wine prefix yet.")
                    if dry:
                        L("  (preview) would launch once and wait for the prefix")
                    else:
                        L("Launching game once to create prefix…")
                        self.signals.next_step.emit("Waiting for Steam to create the prefix…")
                        subprocess.Popen(["xdg-open", f"steam://rungameid/{a}"])
                        if self._wait_for_prefix(a, L):
                            self.run_cmd(pt + [a, "-q"] + p["verbs"], dry)
                            self.signals.next_step.emit("Prefix ready — winetricks ran.")
                        else:
                            self.signals.next_step.emit("Next: close the game if open, then Make it work again.")
                else:
                    self.run_cmd(pt + [a, "-q"] + p["verbs"], dry)

            admin_ops = []
            if p["pkgs"] and core.PM.get(self.env["family"]) and not self.env["immutable"]:
                if p["note"]:
                    L("Note: " + p["note"])
                if p["pre"]:
                    shell = p["pre"] + " && " + shlex.join(core.PM[self.env["family"]] + p["pkgs"])
                    admin_ops.append(("packages", core.admin_cmd(shell_cmd=shell), shell))
                else:
                    argv = core.admin_cmd(op="packages", args=[self.env["family"]] + p["pkgs"])
                    admin_ops.append(("packages", argv, shlex.join(core.PM[self.env["family"]] + p["pkgs"])))
            elif p["pkgs"] and self.env["immutable"]:
                L("Skipping package installs on immutable OS.")
            for k, v in g.get("sysctl", {}).items():
                try:
                    cur = open("/proc/sys/" + k.replace(".", "/")).read().strip()
                except OSError:
                    cur = None
                if cur is not None and int(cur) < int(v):
                    argv = core.admin_cmd(op="sysctl", args=[k, str(v)])
                    admin_ops.append((f"sysctl {k}", argv, f"sysctl -w {k}={v}"))
            for label, argv, manual in admin_ops:
                if admin:
                    if not argv:
                        L("! No pkexec/sudo — run as root:\n  " + manual)
                        continue
                    st = core.polkit_status()
                    if st["pkexec"] and not st["agent"]:
                        L("Note: no Polkit agent detected — you may get a terminal auth prompt.")
                    L(f"Elevating {label} via Polkit…")
                    self.run_cmd(argv, dry)
                else:
                    L("Admin step skipped (enable Customize → Allow admin steps). Manual:\n  " + manual)

            if dry:
                L("(preview: Steam config not modified)  launch options would be: " + p["launch"])
                self.signals.next_step.emit("Preview done — click Make it work to apply for real.")
            else:
                try:
                    core.write_steam(root, a, p["launch"], tool, L, force=force)
                    self.last_applied[a] = {
                        "launch": p["launch"], "tool": tool,
                        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    }
                    core.history_record(a, p["launch"], tool, source="apply")
                    self.history = core.history_load()
                    self.signals.show_game.emit()
                    self.signals.next_step.emit("Done. Launch the game from Steam when ready.")
                except RuntimeError as e:
                    L("! " + str(e))
                    L("Launch options: " + p["launch"])
                    if "Steam is running" in str(e):
                        self.signals.next_step.emit("Next: close Steam, then click Make it work again.")
                    else:
                        self.signals.next_step.emit(f"Next: {e}")
            L("Done.")
        except Exception as e:
            L(f"! Error: {e}")
            self.signals.next_step.emit(f"Error: {e}")

    def _steam_install(self, appid, dry):
        if not dry:
            subprocess.Popen(["xdg-open", f"steam://install/{appid}"])
            self.log(f"Requested Steam install for app {appid}")
        else:
            self.log(f"(preview) would request Steam install for app {appid}")

    def launch(self):
        g = self.cur()
        if g:
            subprocess.Popen(["xdg-open", f"steam://rungameid/{g['appid']}"])

    def copy(self):
        g = self.cur()
        if g:
            QApplication.clipboard().setText(self.plan(g)["launch"])
            self.log("Launch options copied")

    def open_pdb(self):
        g = self.cur()
        if g:
            subprocess.Popen(["xdg-open", f"https://www.protondb.com/app/{g['appid']}"])

    def quit_steam_retry(self):
        g = self.cur()
        if not g:
            return
        if QMessageBox.question(
                self, "Quit Steam & retry",
                "This will ask Steam to quit, wait until it exits, then apply fixes "
                f"for {g['name']}.\n\nContinue?"
        ) != QMessageBox.StandardButton.Yes:
            return

        def work():
            self.log("Requesting Steam to quit…")
            subprocess.Popen(["xdg-open", "steam://exit"])
            for i in range(45):
                if not core.steam_running():
                    self.log(f"Steam exited after ~{i}s.")
                    break
                time.sleep(1)
            else:
                self.log("Steam still running — sending SIGTERM…")
                subprocess.run(["pkill", "-x", "steam"], capture_output=True)
                time.sleep(3)
            if core.steam_running():
                self.signals.next_step.emit("Next: quit Steam manually, then click Make it work.")
                self.signals.banner.emit()
                return
            self.signals.banner.emit()
            self._apply_body(g, False, self.sysx.isChecked(), self.force_vdf.isChecked())

        threading.Thread(target=work, daemon=True).start()

    def _refresh_ge_pins(self):
        cur = self.ge_pin.currentText() or "(latest)"
        self.ge_pin.clear()
        self.ge_pin.addItem("(latest)")
        if self.root_dir:
            for name in reversed(core.ge_installed(self.root_dir)):
                self.ge_pin.addItem(name)
        idx = self.ge_pin.findText(cur)
        self.ge_pin.setCurrentIndex(idx if idx >= 0 else 0)

    def _update_session_banner(self):
        msgs = []
        if self.env.get("immutable"):
            msgs.append("Atomic/immutable OS — package installs disabled; Proton & launch options still work.")
        if core.steam_running():
            msgs.append("Steam is running — close it (or Quit Steam & retry) before writing configs.")
        self.banner.setText("  |  ".join(msgs))

    def _first_run_hint(self):
        pr = core.prefs_load()
        if pr.get("seen_prepare"):
            return
        if QMessageBox.question(
                self, "Prepare this PC?",
                "First run: install common gaming packages (Vulkan, gamemode, 32-bit libs) "
                "and recommended kernel tweaks once?\n\n"
                "You can always do this later under Customize → Prepare this PC.\n"
                "Requires Polkit authentication for package installs."
        ) == QMessageBox.StandardButton.Yes:
            self.sysx.setChecked(True)
            self.prepare_pc()
        pr["seen_prepare"] = True
        core.prefs_save(pr)

    def prepare_pc(self):
        fam = self.env.get("family")
        if self.env.get("immutable"):
            QMessageBox.information(
                self, "Prepare this PC",
                "This system is immutable/atomic. Install gaming layers via the "
                "distro's documented path.\n\n"
                "Griffin Play can still set Proton and launch options per game.")
            return
        prof = self.db.get("distro_profiles", {}).get(fam, {})
        pkgs = list(prof.get("packages") or [])
        extras = {
            "ubuntu": ["steam-devices", "gamemode", "libvulkan1", "mesa-vulkan-drivers"],
            "fedora": ["gamemode", "vulkan-loader", "mesa-vulkan-drivers", "steam-devices"],
            "arch": ["gamemode", "lib32-gamemode", "vulkan-icd-loader", "lib32-vulkan-icd-loader"],
            "suse": ["gamemode", "libvulkan1"],
        }
        for x in extras.get(fam, []):
            if x not in pkgs:
                pkgs.append(x)
        if not pkgs and fam not in core.PM:
            QMessageBox.information(self, "Prepare this PC", f"No package list for family '{fam}'.")
            return
        lines = ["This will (via Polkit):"]
        if pkgs and core.PM.get(fam):
            shown = ", ".join(pkgs[:12]) + ("…" if len(pkgs) > 12 else "")
            lines.append("  • Install: " + shown)
        lines.append("  • Apply recommended sysctl gaming tweaks")
        lines.append("\nAuthenticate when prompted. Continue?")
        if QMessageBox.question(self, "Prepare this PC", "\n".join(lines)) != QMessageBox.StandardButton.Yes:
            return
        self.sysx.setChecked(True)

        def work():
            L = self.log
            L("--- Prepare this PC ---")
            if pkgs and core.PM.get(fam):
                pre = prof.get("pre") or ""
                if pre:
                    argv = core.admin_cmd(shell_cmd=pre + " && " + shlex.join(core.PM[fam] + pkgs))
                else:
                    argv = core.admin_cmd(op="packages", args=[fam] + pkgs)
                if argv:
                    self.run_cmd(argv, False)
                else:
                    L("! No pkexec/sudo — run as root:\n  " + shlex.join(core.PM[fam] + pkgs))
            for k, v in {"vm.max_map_count": "2147483642"}.items():
                try:
                    cur = open("/proc/sys/" + k.replace(".", "/")).read().strip()
                except OSError:
                    cur = None
                if cur is not None and int(cur) < int(v):
                    argv = core.admin_cmd(op="sysctl", args=[k, str(v)])
                    if argv:
                        self.run_cmd(argv, False)
            if not shutil.which("protontricks"):
                L("Tip: install protontricks (Flatpak: com.github.Matoking.protontricks).")
            self.signals.next_step.emit("PC prep finished. Pick a game and click Make it work.")
            L("Done.")

        threading.Thread(target=work, daemon=True).start()

    def health_check(self):
        e = self.env
        lines = [
            f"Griffin Play {core.__version__} (PyQt6)",
            f"Distro: {e.get('name')}  (family={e.get('family')}, immutable={e.get('immutable')})",
            f"Kernel: {e.get('kernel')}",
            f"GPU: {e.get('gpu_short') or e.get('gpu') or '(not detected)'}",
            f"GPU (full): {e.get('gpu') or 'n/a'}",
            f"GPU vendor guess: {e.get('gpu_vendor')}",
            f"Display session: {e.get('session')}  (family={e.get('session_family')})",
            f"Compositor / WM: {e.get('compositor')}  desktop={e.get('desktop')}",
            f"Wayland={e.get('wayland')}  X11={e.get('x11')}",
            "",
            f"Steam roots found: {len(self.roots)}",
        ]
        for r in self.roots:
            lines.append(f"  • {r}")
            ges = core.ge_installed(r)
            if ges:
                lines.append(f"    GE-Proton: {', '.join(ges[-4:])}" + (" …" if len(ges) > 4 else ""))
        lines += [
            f"Libraries: {len(self.libs)}   Installed apps: {len(self.apps)}",
            f"Steam running: {'yes' if core.steam_running() else 'no'}",
            "",
        ]
        pt = shutil.which("protontricks")
        lines.append(f"protontricks: {pt or 'NOT FOUND'}")
        st = core.polkit_status()
        lines.append(f"pkexec: {'yes' if st['pkexec'] else 'NO'}")
        lines.append(f"Polkit agent: {'yes' if st['agent'] else 'no'}")
        lines.append(f"Griffin Play Polkit policy: {'installed' if st['policy'] else 'not installed'}")
        lines.append(f"Cache: {core.CACHE}")
        lines.append(f"Saved history entries: {len(core.history_load())}")
        msg = "\n".join(lines)
        self.log("--- Health check ---\n" + msg)
        QMessageBox.information(self, "Health check", msg)

    def restore(self):
        if not self.root_dir:
            QMessageBox.critical(self, "Griffin Play", "No Steam installation found.")
            return
        backs = core.list_backups(self.root_dir)
        if not backs:
            QMessageBox.information(self, "Restore backup", "No Griffin Play backups found.")
            return
        if QMessageBox.question(
                self, "Restore backup",
                f"Restore {len(backs)} backup file(s)? Steam should be closed."
        ) != QMessageBox.StandardButton.Yes:
            return
        if core.steam_running():
            QMessageBox.warning(self, "Steam is running", "Close Steam first.")
            return

        def work():
            try:
                core.restore_backups(self.root_dir, self.log)
                self.signals.next_step.emit("Backups restored.")
            except Exception as e:
                self.log(f"! {e}")

        threading.Thread(target=work, daemon=True).start()

    def reuse_history(self):
        g = self.cur()
        if not g:
            return
        a = str(g["appid"])
        hist = self.history.get(a) or core.history_load().get(a)
        if not hist or not hist.get("launch"):
            QMessageBox.information(
                self, "Reuse last good opts",
                "No saved launch options for this game yet.")
            return
        if QMessageBox.question(
                self, "Reuse last good opts",
                f"Write these saved options back into Steam?\n\n{hist['launch']}\n\n"
                f"Tool: {hist.get('tool') or 'none'}\nSaved: {hist.get('at')}"
        ) != QMessageBox.StandardButton.Yes:
            return
        if not self.root_dir:
            QMessageBox.critical(self, "Griffin Play", "No Steam installation found.")
            return
        if core.steam_running():
            self.signals.next_step.emit("Next: close Steam, then click Reuse last good again.")
            QMessageBox.warning(self, "Steam is running", "Close Steam first, then try again.")
            return

        def work():
            try:
                core.write_steam(self.root_dir, a, hist["launch"], hist.get("tool"), self.log,
                                 force=self.force_vdf.isChecked())
                core.history_record(a, hist["launch"], hist.get("tool"), source="reuse")
                self.history = core.history_load()
                self.last_applied[a] = hist
                self.signals.next_step.emit("Saved options restored.")
                self.signals.show_game.emit()
            except Exception as e:
                self.log(f"! {e}")

        threading.Thread(target=work, daemon=True).start()

    def _load_similar(self, appid):
        if appid in self.similar:
            return

        def work():
            self.signals.next_step.emit("Fetching ProtonDB reports…")
            try:
                reps = core.pdb_fetch_reports(appid)
                ranked = core.rank_similar_reports(reps or [], self.env, self.env.get("gpu", ""))
                self.similar[appid] = ranked
                self.signals.next_step.emit(f"Loaded {len(ranked)} similar reports")
                self.signals.show_game.emit()
            except Exception:
                self.similar[appid] = []
                self.signals.next_step.emit("ProtonDB reports unavailable (offline or rate-limited)")
                self.signals.show_game.emit()

        threading.Thread(target=work, daemon=True).start()

    def refresh_reports(self):
        g = self.cur()
        if not g:
            return
        a = str(g["appid"])
        self.similar.pop(a, None)
        self._load_similar(a)

    def use_suggested(self):
        g = self.cur()
        if not g:
            return
        a = str(g["appid"])
        sims = self.similar.get(a) or []
        with_opts = [s for s in sims if s.get("launchOptions")]
        if not with_opts:
            QMessageBox.information(self, "Use suggested opts", "No similar reports with launch options yet.")
            return
        s = with_opts[0]
        import re
        ge_hint = None
        m = re.search(r"(GE-Proton[\w.-]+)", s.get("proton") or "", re.I)
        if not m:
            m = re.search(r"(GE-Proton[\w.-]+)", s.get("variant") or "", re.I)
        if m:
            ge_hint = m.group(1)
        extra = f"\n\nAlso set GE pin to {ge_hint}?" if ge_hint else ""
        if QMessageBox.question(
                self, "Use suggested opts",
                f"Apply this community launch command?\n\n{s['launchOptions']}\n\n"
                f"From: {s['os']} | {s['gpu'][:50]}\nProton: {s['proton']}"
                f"{extra}\n\nSteam must be closed."
        ) != QMessageBox.StandardButton.Yes:
            return
        if ge_hint:
            if self.ge_pin.findText(ge_hint) < 0:
                self.ge_pin.addItem(ge_hint)
            self.ge_pin.setCurrentText(ge_hint)
            self.proton_ov.setCurrentText("ge")
        if core.steam_running():
            self.signals.next_step.emit("Next: close Steam, then Use suggested opts again.")
            QMessageBox.warning(self, "Steam is running", "Close Steam first.")
            return

        def work():
            try:
                tool = None
                if self.proton_ov.currentText() == "ge" or g.get("proton") == "ge":
                    pin = self.ge_pin.currentText()
                    pin = None if pin == "(latest)" else pin
                    try:
                        tool = core.install_ge(self.root_dir, self.log, False, pin=pin)
                    except Exception as e:
                        self.log(f"! {e}")
                core.write_steam(self.root_dir, a, s["launchOptions"], tool, self.log,
                                 force=self.force_vdf.isChecked())
                self.last_applied[a] = {
                    "launch": s["launchOptions"], "tool": tool,
                    "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                core.history_record(a, s["launchOptions"], tool, source="suggested")
                self.history = core.history_load()
                self.log("Applied suggested launch options from similar report.")
                self.signals.next_step.emit("Suggested opts saved.")
                self.signals.show_game.emit()
            except Exception as e:
                self.log(f"! {e}")
                self.signals.next_step.emit(f"Next: {e}")

        threading.Thread(target=work, daemon=True).start()

    def report_issue(self):
        from urllib.parse import quote
        g = self.cur()
        e = self.env
        body = [
            f"- Griffin Play {core.__version__}",
            f"- Distro: {e.get('name')} (family={e.get('family')}, immutable={e.get('immutable')})",
            f"- Kernel: {e.get('kernel')}",
            f"- Display: session={e.get('session')} compositor={e.get('compositor')} desktop={e.get('desktop')}",
            f"- GPU: {e.get('gpu') or '?'}",
            f"- Steam roots: {len(self.roots)}, libraries: {len(self.libs)}",
        ]
        if g:
            body.append(f"- Game: {g['name']} ({g['appid']})")
            body.append(f"- Plan proton: {self.plan(g)['proton']}")
        body.append("\n### What happened\n\n")
        url = f"{core.ISSUES_NEW}?title={quote('[Griffin Play] ' + (g['name'] if g else 'General'))}&body={quote(chr(10).join(body))}"
        subprocess.Popen(["xdg-open", url])

    def _check_updates(self):
        def work():
            tag, url = core.check_latest_release()
            if not tag:
                return

            def norm(v):
                return tuple(int(x) for x in re.findall(r"\d+", v)[:3])

            import re
            if norm(tag) <= norm(core.__version__):
                return
            pr = core.prefs_load()

            def ask():
                if core.griffin_updater_installed():
                    QMessageBox.information(
                        self, "Update available",
                        f"Griffin Play {tag} is available (you have {core.__version__}).\n\n"
                        "Griffin Updater is installed — use it to update, or open Releases.")
                    return
                if pr.get("dismiss_griffin_updater"):
                    return
                if QMessageBox.question(
                        self, "Griffin Updater",
                        "Automatic updates are handled by Griffin Updater.\n\n"
                        f"Griffin Play {tag} is available (you have {core.__version__}).\n\n"
                        "Install Griffin Updater now?\n"
                        f"{core.GRIFFIN_UPDATER_URL}"
                ) == QMessageBox.StandardButton.Yes:
                    subprocess.Popen(["xdg-open", core.GRIFFIN_UPDATER_URL])
                else:
                    pr["dismiss_griffin_updater"] = True
                    core.prefs_save(pr)

            # schedule on GUI thread
            QTimer.singleShot(0, ask)

        threading.Thread(target=work, daemon=True).start()


def run():
    import sys
    app = QApplication(sys.argv)
    app.setApplicationName("Griffin Play")
    app.setOrganizationName("Griffin")
    app.setStyle("Fusion")
    app.setStyleSheet(DARK_QSS)
    win = MainWindow()
    win.show()
    return app.exec()
