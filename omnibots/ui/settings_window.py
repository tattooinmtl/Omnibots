"""Settings: Folders (A11.m.01), the Bots & icons legend (A11.d.05), Providers (A11.p.01) and Doctor (A16.d.02)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog, QFrame, QGridLayout, QLabel, QScrollArea, QTabWidget, QVBoxLayout, QWidget,
)

from omnibots.ui import theme
from omnibots.ui.omi_face import render_face
from omnibots.ui.props import ACTIONS, JOBS, badge_image
from omnibots.ui.doctor_page import DoctorPage
from omnibots.ui.providers_page import ProvidersPage

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


def folders_tab(engine=None, settings_path=None) -> QWidget:
    """Settings → Folders: the output folder (new goals go there; existing projects stay put)."""
    from pathlib import Path

    from PySide6.QtWidgets import QPushButton

    from omnibots.paths import get_paths
    from omnibots.settings import load_settings, save_setting
    from omnibots.ui.setup_dialog import FolderPicker, default_output_folder, usable

    path = Path(settings_path) if settings_path else get_paths().settings_file
    current = load_settings(path)["output"]["folder"] or str(default_output_folder())
    w = QWidget()
    v = QVBoxLayout(w)
    v.setContentsMargins(18, 16, 18, 16)
    t = QLabel("Output folder")
    t.setStyleSheet("font-size: 16px; font-weight: 600;")
    v.addWidget(t)
    note = QLabel("Every new goal gets a folder here, named with the date and the goal. Projects that already exist stay "
                  "where they are. To have the bots work in some other folder, use File → Open folder in a bot window.")
    note.setWordWrap(True)
    note.setObjectName("dim")
    v.addWidget(note)
    w.picker = FolderPicker(Path(current))
    v.addWidget(w.picker)
    w.status = QLabel("")
    v.addWidget(w.status)
    save = QPushButton("Use this folder")
    save.setObjectName("primary")

    def apply() -> None:
        folder = w.picker.folder()
        why = usable(folder)
        if why:
            w.status.setText(f"<span style='color:{theme.RED}'>✖ {why}</span>")
            return
        save_setting(path, "output", "folder", str(folder))
        if engine is not None:
            engine.set_output_dir(folder)
        w.status.setText(f"<span style='color:{theme.GREEN}'>✔ New goals go to {folder}</span>")
    save.clicked.connect(apply)
    w.apply = apply
    v.addWidget(save, 0, Qt.AlignmentFlag.AlignLeft)
    v.addStretch(1)
    return w


class SettingsWindow(QDialog):
    def __init__(self, parent=None, engine=None, settings_path=None, omni_settings=None):
        super().__init__(parent)
        self.setWindowTitle("OmniBots · Settings")
        self.setStyleSheet(theme.stylesheet() + f"""
            QDialog {{ background: {theme.BG0}; }}
            QTabWidget::pane {{ border: 1px solid {theme.BORDER}; border-radius: 12px; background: {theme.BG1}; top: -1px; }}
            QTabBar::tab {{ background: {theme.BG2}; border: 1px solid {theme.BORDER}; padding: 8px 18px;
                            border-top-left-radius: 10px; border-top-right-radius: 10px; margin-right: 4px; color: {theme.TEXT_DIM}; }}
            QTabBar::tab:selected {{ background: {theme.BG1}; color: {theme.TEXT}; border-bottom-color: {theme.BG1}; }}
            QListWidget {{ background: {theme.BG2}; border: 1px solid {theme.BORDER}; border-radius: 12px; outline: 0; }}
            QListWidget::item {{ padding: 8px 10px; border-radius: 8px; }}
            QListWidget::item:selected {{ background: {theme.BG3}; color: {theme.TEXT}; }}
            QComboBox {{ background: {theme.BG2}; border: 1px solid {theme.BORDER}; border-radius: 10px; padding: 6px 10px; }}
            QComboBox QAbstractItemView {{ background: {theme.BG1}; color: {theme.TEXT}; selection-background-color: {theme.BG3}; }}
        """)
        self.resize(920, 820)
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 16, 16, 16)
        tabs = QTabWidget()
        self.folders = folders_tab(engine, settings_path)
        tabs.addTab(self.folders, "Folders")
        tabs.addTab(icons_legend(), "Bots && icons")          # "&&" = a literal & (a single & marks a shortcut)
        self.providers = ProvidersPage(omni_settings, engine, autoload=omni_settings is not None)
        tabs.addTab(self.providers, "Providers")
        self.doctor = DoctorPage(engine, getattr(engine, "home", None))
        tabs.addTab(self.doctor, "Doctor")
        tabs.currentChanged.connect(self._tab_changed)
        v.addWidget(tabs)
        self.tabs = tabs

    def _tab_changed(self, index: int) -> None:
        if self.tabs.widget(index) is self.providers:
            self.providers.ensure_loaded()
