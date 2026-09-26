"""Settings (PLAN.md A11.a): the "Bots & icons" legend the user asked for (2026-09-26):
which badge means which job, and which extra Omi shows for which action."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog, QFrame, QGridLayout, QLabel, QScrollArea, QTabWidget, QVBoxLayout, QWidget,
)

from omnibots.ui import theme
from omnibots.ui.omi_face import render_face
from omnibots.ui.props import ACTIONS, JOBS, badge_image

LEGEND_MOOD = {"coding": "working", "writing": "working", "running": "working", "reviewing": "working",
               "searching": "thinking", "thinking": "thinking", "waiting": "thinking", "deploying": "joy",
               "speaking": "joy", "shopping": "surprised", "looking": "surprised"}


# the matcher uses word stems ("investigat" matches investigator/investigation); show real words
STEM_WORDS = {"investigat": "investigator", "purchas": "purchasing", "secur": "secure", "translat": "translator",
              "locali": "localization", "analysis": "analysis", "copy": "copywriter", "dev": "dev"}


def _section(title: str, note: str) -> QWidget:
    w = QWidget()
    v = QVBoxLayout(w)
    v.setContentsMargins(0, 8, 0, 4)
    t = QLabel(title)
    t.setStyleSheet("font-size: 17px; font-weight: 600;")
    n = QLabel(note)
    n.setObjectName("dim")
    n.setWordWrap(True)
    v.addWidget(t)
    v.addWidget(n)
    return w


def icons_legend() -> QWidget:
    page = QWidget()
    v = QVBoxLayout(page)
    v.setContentsMargins(18, 10, 18, 18)
    v.addWidget(_section("Job badges", "The round badge in the top-left corner of a bot's picture says what kind of bot it "
                                       "is. The Bot Factory picks it from the bot's role; the ring color is the role color."))
    grid = QGridLayout()
    grid.setHorizontalSpacing(14)
    grid.setVerticalSpacing(8)
    for i, (key, (title, words)) in enumerate(JOBS.items()):
        r, c = divmod(i, 2)
        icon = QLabel()
        icon.setPixmap(QPixmap.fromImage(badge_image(key, theme.role_color(key), 44)))
        shown = ", ".join(STEM_WORDS.get(w, w) for w in words) if words else "anything else"
        name = QLabel(f"<b>{title}</b><br><span style='color:{theme.TEXT_DIM}; font-size:11px'>"
                      f"role mentions: {shown}</span>")
        grid.addWidget(icon, r, c * 2)
        grid.addWidget(name, r, c * 2 + 1)
    grid.setColumnStretch(1, 1)
    grid.setColumnStretch(3, 1)
    v.addLayout(grid)

    line = QFrame()
    line.setFixedHeight(1)
    line.setStyleSheet(f"background: {theme.BORDER}; margin: 10px 0;")
    v.addWidget(line)
    v.addWidget(_section("Action extras", "What Omi holds or shows while he works, animated. It changes with every tool the "
                                          "bot uses, and never covers his face."))
    grid2 = QGridLayout()
    grid2.setHorizontalSpacing(14)
    grid2.setVerticalSpacing(6)
    for i, (key, (label, triggers)) in enumerate(ACTIONS.items()):
        r, c = divmod(i, 2)
        icon = QLabel()
        icon.setPixmap(QPixmap.fromImage(render_face(84, LEGEND_MOOD.get(key, "happy"), t=1.3, prop=key)))
        name = QLabel(f"<b>{label}</b><br><span style='color:{theme.TEXT_DIM}; font-size:11px'>{triggers}</span>")
        name.setWordWrap(True)
        grid2.addWidget(icon, r, c * 2)
        grid2.addWidget(name, r, c * 2 + 1)
    grid2.setColumnStretch(1, 1)
    grid2.setColumnStretch(3, 1)
    v.addLayout(grid2)
    v.addStretch(1)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setWidget(page)
    scroll.setStyleSheet(f"QScrollArea, QScrollArea > QWidget > QWidget {{ background: {theme.BG1}; }}")
    return scroll


class SettingsWindow(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("OmniBots · Settings")
        self.setStyleSheet(theme.stylesheet() + f"""
            QDialog {{ background: {theme.BG0}; }}
            QTabWidget::pane {{ border: 1px solid {theme.BORDER}; border-radius: 12px; background: {theme.BG1}; top: -1px; }}
            QTabBar::tab {{ background: {theme.BG2}; border: 1px solid {theme.BORDER}; padding: 8px 18px;
                            border-top-left-radius: 10px; border-top-right-radius: 10px; margin-right: 4px; color: {theme.TEXT_DIM}; }}
            QTabBar::tab:selected {{ background: {theme.BG1}; color: {theme.TEXT}; border-bottom-color: {theme.BG1}; }}
        """)
        self.resize(920, 820)
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 16, 16, 16)
        tabs = QTabWidget()
        tabs.addTab(icons_legend(), "Bots && icons")          # "&&" = a literal & (a single & marks a shortcut)
        v.addWidget(tabs)
        self.tabs = tabs
