"""
Modern visual theme for Griffin Hub 1.1.

Design goals:
  - Deeper, richer dark palette with clearer hierarchy
  - Soft glass-like cards with subtle borders and hover states
  - Consistent accent glow without harsh pure-black panels
  - Readable monospace for logs/status without “black box” feel
  - Larger touch targets and clearer focus rings
"""

from __future__ import annotations

COLORS = {
    "bg_darkest":    "#1c2029",
    "bg_dark":       "#212631",
    "bg_mid":        "#262b36",
    "bg_panel":      "#2f3644",
    "bg_card":       "#2f3644",
    "bg_card_hover": "#3a4354",
    "bg_input":      "#2f3644",
    "bg_elevated":   "#3a4354",
    "border":        "#3a4a6b",
    "border_bright": "#4d6597",
    "border_focus":  "#3d7bff",
    "accent":        "#3d7bff",
    "accent_dim":    "#2a5fd4",
    "accent_glow":   "#5b93ff",
    "accent2":       "#4f8cff",
    "silver":        "#e6eef8",
    "silver_dim":    "#b9c9e6",
    "text_primary":  "#eef2fa",
    "text_secondary":"#b2bdd1",
    "text_muted":    "#94a1b8",
    "success":       "#34d399",
    "success_dim":   "#10b981",
    "warning":       "#fbbf24",
    "warning_dim":   "#d97706",
    "error":         "#f87171",
    "error_dim":     "#ef4444",
    "white":         "#ffffff",
    "black":         "#000000",
}


def build_stylesheet() -> str:
    c = COLORS
    return f"""
/* ── Base ─────────────────────────────────────────────────────────────── */
QMainWindow, QDialog {{
    background-color: {c['bg_darkest']};
}}

QWidget {{
    background-color: transparent;
    color: {c['text_primary']};
    font-family: 'Inter', 'Segoe UI', 'Ubuntu', 'Noto Sans', sans-serif;
    font-size: 14px;
}}

/* ── Scroll areas ─────────────────────────────────────────────────────── */
QScrollArea {{
    background-color: transparent;
    border: none;
}}

QScrollBar:vertical {{
    background: {c['bg_dark']};
    width: 8px;
    border-radius: 4px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {c['border_bright']};
    border-radius: 4px;
    min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{
    background: {c['accent_dim']};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}

QScrollBar:horizontal {{
    background: {c['bg_dark']};
    height: 8px;
    border-radius: 4px;
}}
QScrollBar::handle:horizontal {{
    background: {c['border_bright']};
    border-radius: 4px;
}}

/* ── Tooltips ─────────────────────────────────────────────────────────── */
QToolTip {{
    background-color: {c['bg_elevated']};
    color: {c['text_primary']};
    border: 1px solid {c['border_bright']};
    border-radius: 8px;
    padding: 8px 12px;
    font-size: 14px;
}}

/* ── Message boxes ────────────────────────────────────────────────────── */
QMessageBox {{
    background-color: {c['bg_mid']};
}}
QMessageBox QLabel {{
    color: {c['text_primary']};
}}
QMessageBox QPushButton {{
    background-color: {c['accent']};
    color: white;
    border: none;
    border-radius: 8px;
    padding: 10px 22px;
    min-width: 88px;
    font-weight: 600;
}}
QMessageBox QPushButton:hover {{
    background-color: {c['accent_glow']};
}}

/* ── Combo / LineEdit ─────────────────────────────────────────────────── */
QComboBox {{
    background-color: {c['bg_card']};
    border: 1px solid {c['border']};
    border-radius: 8px;
    padding: 8px 14px;
    color: {c['text_primary']};
    min-height: 34px;
}}
QComboBox:hover {{
    border-color: {c['accent_dim']};
}}
QComboBox:focus {{
    border-color: {c['border_focus']};
}}
QComboBox::drop-down {{
    border: none;
    width: 28px;
}}
QComboBox QAbstractItemView {{
    background-color: {c['bg_card']};
    border: 1px solid {c['border_bright']};
    border-radius: 8px;
    color: {c['text_primary']};
    selection-background-color: {c['accent_dim']};
    padding: 4px;
}}

QLineEdit {{
    background-color: {c['bg_card']};
    border: 1px solid {c['border']};
    border-radius: 8px;
    padding: 9px 14px;
    color: {c['text_primary']};
    min-height: 34px;
}}
QLineEdit:focus {{
    border-color: {c['border_focus']};
}}

/* ── Checkboxes ───────────────────────────────────────────────────────── */
QCheckBox {{
    color: {c['text_secondary']};
    spacing: 10px;
}}
QCheckBox::indicator {{
    width: 18px;
    height: 18px;
    border: 2px solid {c['border_bright']};
    border-radius: 5px;
    background-color: {c['bg_card']};
}}
QCheckBox::indicator:checked {{
    background-color: {c['accent']};
    border-color: {c['accent']};
}}
QCheckBox::indicator:hover {{
    border-color: {c['accent']};
}}

/* ── Progress ─────────────────────────────────────────────────────────── */
QProgressBar {{
    background-color: {c['bg_card']};
    border: 1px solid {c['border']};
    border-radius: 5px;
    height: 10px;
    text-align: center;
    color: transparent;
}}
QProgressBar::chunk {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {c['accent_dim']}, stop:1 {c['accent_glow']});
    border-radius: 5px;
}}

/* ── Tabs ─────────────────────────────────────────────────────────────── */
QTabWidget::pane {{
    border: 1px solid {c['border']};
    border-radius: 10px;
    background-color: {c['bg_panel']};
    top: -1px;
}}
QTabBar::tab {{
    background-color: {c['bg_card']};
    color: {c['text_secondary']};
    padding: 11px 22px;
    border-radius: 8px 8px 0 0;
    margin-right: 3px;
    font-size: 14px;
    font-weight: 500;
}}
QTabBar::tab:selected {{
    background-color: {c['bg_panel']};
    color: {c['accent_glow']};
    border-bottom: 2px solid {c['accent']};
}}
QTabBar::tab:hover:!selected {{
    background-color: {c['bg_card_hover']};
    color: {c['text_primary']};
}}

/* ── Group boxes ──────────────────────────────────────────────────────── */
QGroupBox {{
    border: 1px solid {c['border']};
    border-radius: 10px;
    margin-top: 18px;
    padding-top: 10px;
    color: {c['text_secondary']};
    font-size: 14px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 14px;
    top: -8px;
    padding: 3px 10px;
    background-color: {c['bg_mid']};
    color: {c['accent']};
    font-size: 13px;
    font-weight: 700;
    letter-spacing: 0.8px;
}}

/* ── Lists ────────────────────────────────────────────────────────────── */
QListWidget {{
    background-color: {c['bg_card']};
    border: 1px solid {c['border']};
    border-radius: 10px;
    padding: 6px;
    outline: none;
}}
QListWidget::item {{
    border-radius: 8px;
    padding: 10px 14px;
    margin: 2px 0;
    color: {c['text_secondary']};
}}
QListWidget::item:selected {{
    background-color: {c['accent_dim']};
    color: {c['text_primary']};
}}
QListWidget::item:hover:!selected {{
    background-color: {c['bg_card_hover']};
    color: {c['text_primary']};
}}

/* ── Text / log panels — avoid pure black “boxes” ─────────────────────── */
QTextEdit, QPlainTextEdit {{
    background-color: {c['bg_card']};
    border: 1px solid {c['border']};
    border-radius: 10px;
    padding: 12px 14px;
    color: {c['silver_dim']};
    font-family: 'JetBrains Mono', 'Fira Code', 'Cascadia Code', 'Courier New', monospace;
    font-size: 14px;
    selection-background-color: {c['accent_dim']};
}}

/* ── Buttons (base) ───────────────────────────────────────────────────── */
QPushButton {{
    background-color: {c['bg_card']};
    color: {c['text_primary']};
    border: 1px solid {c['border']};
    border-radius: 8px;
    padding: 9px 18px;
    font-weight: 600;
    min-height: 34px;
}}
QPushButton:hover {{
    background-color: {c['bg_card_hover']};
    border-color: {c['border_bright']};
}}
QPushButton:pressed {{
    background-color: {c['bg_elevated']};
}}
QPushButton:disabled {{
    color: {c['text_muted']};
    background-color: {c['bg_dark']};
}}

/* ── Spin boxes / sliders ─────────────────────────────────────────────── */
QSpinBox, QDoubleSpinBox {{
    background-color: {c['bg_card']};
    border: 1px solid {c['border']};
    border-radius: 8px;
    padding: 6px 10px;
    color: {c['text_primary']};
    min-height: 32px;
}}
QSlider::groove:horizontal {{
    height: 6px;
    background: {c['bg_card']};
    border-radius: 3px;
}}
QSlider::handle:horizontal {{
    width: 16px;
    height: 16px;
    margin: -5px 0;
    background: {c['accent']};
    border-radius: 8px;
}}
QSlider::sub-page:horizontal {{
    background: {c['accent_dim']};
    border-radius: 3px;
}}
"""


# Convenience aliases used by widgets
STYLESHEET = build_stylesheet()

# Card / badge helper styles
CARD_STYLE = f"""
    background-color: {COLORS['bg_card']};
    border: 1px solid {COLORS['border']};
    border-radius: 12px;
"""

CARD_HOVER_STYLE = f"""
    background-color: {COLORS['bg_card_hover']};
    border: 1px solid {COLORS['border_bright']};
    border-radius: 12px;
"""

INFO_STYLE = (
    f"background-color:{COLORS['bg_card']};"
    f"border:1px solid {COLORS['border']};"
    f"border-radius:8px;padding:12px 14px;"
    f"color:{COLORS['text_secondary']};font-size:14px;"
)

WARN_STYLE = (
    f"background-color:{COLORS['bg_card']};"
    f"border:1px solid {COLORS['warning']};"
    f"border-radius:8px;padding:12px 14px;"
    f"color:{COLORS['warning']};font-size:14px;"
)
