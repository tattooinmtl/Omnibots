"""The bot window (PLAN.md A11.c, the LayoutPlan layout). Every bot gets this same window:
its ID card, its console and thinking, a chat with it, and its project files.

  ┌ OmniBots  File Edit Layout Settings Help About ─────────────── _ □ × ┐
  │ ┌ ID card ─────┐ ┌ team strip ─────────────────────┐ ┌ Files ───┐ │
  │ │ Omi ● Online │ │ chat (bot left, you right)      │ │ tree     │ │
  │ ├ Console ─────┤ │                                 │ │          │ │
  │ ├ Thinking ────┤ │ [+] prompt…               📎 ➤  │ │          │ │
  │ └──────────────┘ └─────────────────────────────────┘ └──────────┘ │
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPixmap, QRadialGradient
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMainWindow, QSplitter, QToolButton, QVBoxLayout, QWidget

from omnibots.ui import theme
from omnibots.ui.omi_face import render_face
from omnibots.ui.props import job_for_role
from omnibots.ui.widgets import BoardPanel, BotCard, ChatPanel, FilesPanel, IDCard, TeamStrip, TerminalPanel


class Backdrop(QWidget):
    """The window background: deep navy with soft blue light, like LayoutPlan."""

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        g = QLinearGradient(QPointF(0, 0), QPointF(0, self.height()))
        g.setColorAt(0, QColor("#08102a"))
        g.setColorAt(1, QColor("#050814"))
        p.fillRect(self.rect(), g)
        for cx, cy, r, a in ((0.15, 0.1, 0.45, 55), (0.85, 0.95, 0.5, 40)):
            rg = QRadialGradient(QPointF(self.width() * cx, self.height() * cy), self.width() * r)
            c = QColor(theme.ACCENT)
            c.setAlpha(a)
            rg.setColorAt(0, c)
            c.setAlpha(0)
            rg.setColorAt(1, c)
            p.fillRect(self.rect(), rg)
        p.end()


class TitleBar(QWidget):
    def __init__(self, window: QMainWindow, parent=None):
        super().__init__(parent)
        self.win = window
        self._drag: QPoint | None = None
        self.setFixedHeight(52)
        row = QHBoxLayout(self)
        row.setContentsMargins(18, 0, 8, 0)
        logo = QLabel()
        logo.setPixmap(QPixmap.fromImage(render_face(30, "happy", ring=False)))
        name = QLabel("OmniBots")
        name.setStyleSheet("font-size: 20px; font-weight: 700; padding-left: 6px;")
        row.addWidget(logo)
        row.addWidget(name)
        row.addSpacing(28)
        for m in ("File", "Edit", "Layout", "Settings", "Help", "About"):
            b = QToolButton()
            b.setText(m)
            if m == "Settings":
                b.clicked.connect(self._open_settings)
            b.setStyleSheet(f"QToolButton {{ border: none; padding: 6px 12px; font-size: 14px; color: {theme.TEXT}; }}"
                            f"QToolButton:hover {{ background: {theme.BG2}; border-radius: 8px; }}")
            row.addWidget(b)
        row.addStretch(1)
        for glyph, act in (("—", window.showMinimized), ("☐", self._toggle_max), ("✕", window.close)):
            b = QToolButton()
            b.setText(glyph)
            b.setFixedSize(44, 36)
            b.clicked.connect(act)
            b.setStyleSheet(f"QToolButton {{ border: none; font-size: 14px; color: {theme.TEXT_DIM}; }}"
                            f"QToolButton:hover {{ background: {theme.BG3}; color: {theme.TEXT}; border-radius: 8px; }}")
            row.addWidget(b)

    def _open_settings(self) -> None:
        from omnibots.ui.settings_window import SettingsWindow
        self._settings = SettingsWindow(self.win)
        self._settings.show()

    def _toggle_max(self) -> None:
        self.win.showNormal() if self.win.isMaximized() else self.win.showMaximized()

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.win.frameGeometry().topLeft()

    def mouseMoveEvent(self, e) -> None:
        if self._drag is not None and not self.win.isMaximized():
            self.win.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, _e) -> None:
        self._drag = None

    def mouseDoubleClickEvent(self, _e) -> None:
        self._toggle_max()


class BotWindow(QMainWindow):
    """One bot's window. Closing it hides it (bots keep working; the tray reopens it)."""

    on_first_hide = None                                # the tray's "still running" hint, shown once

    def __init__(self, card: BotCard, workspace: Path, team: list[tuple[str, str, str, str]] | None = None,
                 bot_id: str = "omi", computers=None, quit_on_close: bool = False):
        super().__init__()
        self.quit_on_close = quit_on_close              # only when there's no system tray (A11.b.01)
        self.computers = computers                      # runtime.computer_tools.ComputerClient (A10.f.05)
        self._computer_win = None
        self.bot_id = bot_id
        self.setWindowTitle(f"OmniBots · {card.name}")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.setStyleSheet(theme.stylesheet())
        self.resize(1536, 1000)
        root = Backdrop()
        root.setObjectName("root")
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(TitleBar(self))
        line = QWidget()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background: {theme.BORDER};")
        outer.addWidget(line)
        body = QHBoxLayout()
        body.setContentsMargins(16, 16, 16, 16)
        body.setSpacing(16)

        left = QVBoxLayout()
        left.setSpacing(16)
        self.card = IDCard(card)
        self.card.setFixedHeight(250)
        self.card.computer_btn.clicked.connect(self.open_computer)
        self.console = TerminalPanel("Terminal / Console", "console", dot=theme.GREEN)
        self.thinking = TerminalPanel("Thinking", "thinking", dot=theme.PURPLE)
        left.addWidget(self.card)
        left.addWidget(self.console, 1)
        left.addWidget(self.thinking, 1)

        center = QVBoxLayout()
        center.setSpacing(10)
        self.team_strip = TeamStrip(team or [], bot_id)
        center.addWidget(self.team_strip)
        self.chat = ChatPanel(card.accent, badge=job_for_role(card.role))
        center.addWidget(self.chat, 1)

        # right column: File Explorer on top, the Message Board below (user, 2026-09-26), resizable
        self.files = FilesPanel(workspace)
        self.board = BoardPanel(bot_id)
        right = QSplitter(Qt.Orientation.Vertical)
        right.setHandleWidth(12)
        right.setStyleSheet("QSplitter::handle { background: transparent; }")
        right.addWidget(self.files)
        right.addWidget(self.board)
        right.setSizes([420, 520])
        body.addLayout(left, 23)
        body.addLayout(center, 52)
        body.addWidget(right, 25)
        outer.addLayout(body, 1)
        self.setCentralWidget(root)

    def open_computer(self) -> None:
        """🖥 Open computer: the bot's live screen from the server."""
        from omnibots.ui.computer_view import ComputerWindow
        if self.computers is None:
            from omnibots.runtime.computer_tools import ComputerClient
            self.computers = ComputerClient(lambda _bot: None)           # shows "needs a computer login"
        if self._computer_win is None:
            self._computer_win = ComputerWindow(self.computers, self.bot_id, self.card.card.name, self)
        self._computer_win.show()
        self._computer_win.raise_()

    @property
    def face(self):
        return self.card.face.anim

    def bring_to_front(self) -> None:
        import sys
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()
        if sys.platform == "win32":
            from omnibots.ui.main_window import _force_foreground
            _force_foreground(int(self.winId()))

    def closeEvent(self, e) -> None:
        if self.quit_on_close:                                # no system tray: closing the main window quits
            e.accept()
            from PySide6.QtWidgets import QApplication
            QApplication.instance().quit()
            return
        e.ignore()                                            # hide; the bots keep working, the tray reopens it
        self.hide()
        hint, BotWindow.on_first_hide = BotWindow.on_first_hide, None
        if hint is not None:
            hint()
