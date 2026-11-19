"""Dark theme stylesheet for Griffin Game Tweaks.

Note on the "dark box behind the title" bug: the base rule ``QWidget { background-color }``
also matches every QLabel, so a label sitting on a *lighter* surface (the sidebar) was
painted with the darker window colour and showed up as a box. Labels, check boxes, radio
buttons and group boxes are therefore explicitly transparent so they always blend into
whatever surface they are placed on.
"""

BG = "#1a1a1f"
SURFACE = "#23232b"
SURFACE_HOVER = "#2d2d38"
BORDER = "#35354a"
ACCENT = "#6496ff"
ACCENT_HOVER = "#7aa5ff"
TEXT = "#ffffff"
MUTED = "#a0a0a0"

STYLESHEET = f"""
QWidget {{
    background-color: {BG};
    color: {TEXT};
    font-size: 14px;
}}
QMainWindow, QDialog {{
    background-color: {BG};
}}

/* Anything that only draws text/indicators must never paint its own background. */
QLabel, QCheckBox, QRadioButton, QGroupBox {{
    background: transparent;
}}
QLabel {{
    border: none;
}}

QFrame#sidebar {{
    background-color: {SURFACE};
    border-right: 1px solid {BORDER};
}}
QWidget#brand {{
    background: transparent;
}}

QPushButton {{
    background-color: {SURFACE_HOVER};
    color: {TEXT};
    border: none;
    border-radius: 4px;
    padding: 8px 16px;
    min-height: 28px;
}}
QPushButton:hover {{
    background-color: {BORDER};
}}
QPushButton:pressed {{
    background-color: {SURFACE_HOVER};
}}
QPushButton:disabled {{
    color: #666666;
    background-color: {SURFACE};
}}
QPushButton#primary {{
    background-color: {ACCENT};
}}
QPushButton#primary:hover {{
    background-color: {ACCENT_HOVER};
}}
QPushButton#primary:disabled {{
    background-color: #3a4a70;
    color: #8890a5;
}}
QPushButton#danger {{
    background-color: #804040;
}}
QPushButton#danger:hover {{
    background-color: #904040;
}}
QPushButton#danger:disabled {{
    background-color: #3a2a2a;
}}
QPushButton#nav {{
    text-align: left;
    padding-left: 12px;
    background-color: transparent;
}}
QPushButton#nav:hover {{
    background-color: {SURFACE_HOVER};
}}
QPushButton#nav:checked {{
    background-color: {BORDER};
}}

QLineEdit, QComboBox, QSpinBox {{
    background-color: {BG};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 6px 8px;
    min-height: 24px;
}}
QLineEdit:focus, QComboBox:focus {{
    border-color: {ACCENT};
}}
QComboBox QAbstractItemView {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    selection-background-color: {BORDER};
    outline: none;
}}

QProgressBar {{
    background-color: {SURFACE};
    border: none;
    border-radius: 4px;
    text-align: center;
    min-height: 10px;
    max-height: 14px;
}}
QProgressBar::chunk {{
    background-color: {ACCENT};
    border-radius: 4px;
}}

QLabel#title {{
    font-size: 24px;
    font-weight: 700;
}}
QLabel#section {{
    font-size: 18px;
    font-weight: 600;
}}
QLabel#muted {{
    color: {MUTED};
}}
QLabel#success {{
    color: #64c864;
}}
QLabel#error {{
    color: #ff6464;
}}

QScrollArea {{
    border: none;
    background: transparent;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 12px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {BORDER};
    border-radius: 6px;
    min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{
    background: #4a4a66;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 12px;
    margin: 0;
}}
QScrollBar::handle:horizontal {{
    background: {BORDER};
    border-radius: 6px;
    min-width: 28px;
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    width: 0;
    height: 0;
}}
QScrollBar::add-page, QScrollBar::sub-page {{
    background: transparent;
}}

QListWidget {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 6px;
    outline: none;
}}
QListWidget::item {{
    padding: 10px;
}}
QListWidget::item:hover {{
    background-color: {SURFACE_HOVER};
}}
QListWidget::item:selected {{
    background-color: #2d3f66;
}}
QTextEdit, QPlainTextEdit {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 4px;
}}
QCheckBox, QRadioButton {{
    spacing: 8px;
}}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid #4a4a66;
    background-color: {BG};
}}
QCheckBox::indicator {{
    border-radius: 4px;
}}
QRadioButton::indicator {{
    border-radius: 9px;
}}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{
    border-color: {ACCENT};
}}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background-color: {ACCENT};
    border-color: {ACCENT};
}}
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{
    border-color: {BORDER};
    background-color: {SURFACE};
}}
QGroupBox {{
    border: 1px solid {BORDER};
    border-radius: 6px;
    margin-top: 10px;
    padding-top: 12px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
}}
QToolTip {{
    background-color: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    padding: 4px;
}}
"""
