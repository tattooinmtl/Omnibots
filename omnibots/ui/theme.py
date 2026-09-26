"""The OmniBots look (PLAN.md A11): the dark-navy "glass" theme from LayoutPlan.png.

Colors and a Qt stylesheet in one place, so every window and the tray share the
same palette. Rounded panels, thin blue borders, a soft blue glow, cyan accents.
"""

from __future__ import annotations

# ── palette ─────────────────────────────────────────────────────────────────
BG0 = "#070b18"          # window background (deepest)
BG1 = "#0b1226"          # panel background
BG2 = "#111a35"          # raised element / input
BG3 = "#16224a"          # hover / selected
BORDER = "#22345f"       # thin panel border
BORDER_GLOW = "#2f7dff"  # accent border / glow (LayoutPlan blue)
ACCENT = "#2f7dff"       # primary blue (buttons, user bubbles)
ACCENT_CYAN = "#6fe3ff"  # Omi's eye cyan / highlights
TEXT = "#e7edfb"         # primary text
TEXT_DIM = "#8b97b8"     # secondary text
TEXT_FAINT = "#5b6688"   # faint labels
GREEN = "#3ad07a"        # online / success
AMBER = "#f5a524"        # paused / warning
RED = "#ff4d6d"          # error / needs-approval / panic
PURPLE = "#7c4dff"       # thinking

# Console/terminal colors
TERM_BG = "#060912"
TERM_TEXT = "#c9d4ee"
TERM_GREEN = "#5ef0a6"
TERM_BLUE = "#7fb4ff"
TERM_DIM = "#6b7699"

# Role ring colors (each bot gets one, A11.d.05)
ROLE_COLORS = {
    "boss": "#2f7dff", "coder": "#6fe3ff", "researcher": "#7c4dff", "writer": "#3ad07a",
    "devops": "#f5a524", "reviewer": "#ff8fab", "designer": "#ff4d6d", "analyst": "#4de3c4",
}


def role_color(role: str) -> str:
    r = (role or "").lower()
    for key, col in ROLE_COLORS.items():
        if key in r:
            return col
    # stable colour from the role name for anything unlisted
    palette = list(ROLE_COLORS.values())
    return palette[sum(map(ord, r)) % len(palette)] if r else ACCENT


FONT_UI = "Segoe UI"
FONT_MONO = "Cascadia Mono, Consolas, monospace"


def stylesheet() -> str:
    return f"""
* {{ color: {TEXT}; font-family: "{FONT_UI}"; font-size: 13px; }}
QMainWindow, QWidget#root {{ background: {BG0}; }}
QToolTip {{ background: {BG2}; color: {TEXT}; border: 1px solid {BORDER}; padding: 4px 8px; }}

QWidget#glass {{
    background: {BG1};
    border: 1px solid {BORDER};
    border-radius: 16px;
}}
QWidget#glassAccent {{
    background: {BG1};
    border: 1px solid {BORDER_GLOW};
    border-radius: 16px;
}}
QLabel#panelTitle {{ color: {TEXT}; font-size: 13px; font-weight: 600; }}
QLabel#dim {{ color: {TEXT_DIM}; }}
QLabel#faint {{ color: {TEXT_FAINT}; font-size: 11px; }}

QPushButton {{
    background: {BG2}; border: 1px solid {BORDER}; border-radius: 10px;
    padding: 7px 14px; color: {TEXT};
}}
QPushButton:hover {{ background: {BG3}; border-color: {BORDER_GLOW}; }}
QPushButton:pressed {{ background: {BG3}; }}
QPushButton#primary {{ background: {ACCENT}; border: none; color: white; font-weight: 600; }}
QPushButton#primary:hover {{ background: #4a90ff; }}
QPushButton#chip {{ border-radius: 12px; padding: 8px 14px; }}

QPlainTextEdit, QTextEdit {{
    background: {BG2}; border: 1px solid {BORDER}; border-radius: 12px;
    padding: 8px; selection-background-color: {ACCENT};
}}
QLineEdit {{
    background: {BG2}; border: 1px solid {BORDER}; border-radius: 12px; padding: 9px 12px;
}}
QLineEdit:focus {{ border-color: {BORDER_GLOW}; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 5px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {BORDER_GLOW}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; }}
QScrollBar::handle:horizontal {{ background: {BORDER}; border-radius: 5px; min-width: 30px; }}

QTreeView {{ background: transparent; border: none; outline: 0; }}
QTreeView::item {{ padding: 4px 2px; border-radius: 6px; }}
QTreeView::item:hover {{ background: {BG2}; }}
QTreeView::item:selected {{ background: {BG3}; color: {TEXT}; }}

QMenu {{ background: {BG1}; border: 1px solid {BORDER}; border-radius: 10px; padding: 6px; }}
QMenu::item {{ padding: 7px 26px 7px 12px; border-radius: 7px; }}
QMenu::item:selected {{ background: {BG3}; }}
QMenu::item:disabled {{ color: {TEXT_DIM}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 6px 8px; }}
"""
