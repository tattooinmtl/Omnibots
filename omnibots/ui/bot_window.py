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

from PySide6.QtCore import QPoint, QPointF, Qt, Signal
from PySide6.QtGui import QAction, QColor, QKeySequence, QLinearGradient, QPainter, QPixmap, QRadialGradient
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QHBoxLayout, QLabel, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QSplitter, QToolButton,
    QTreeView, QVBoxLayout, QWidget,
)

from omnibots.ui import theme
from omnibots.ui.files import EditorTabs, FileOps, ask_text, copy_to_clipboard
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
            elif m == "About":
                b.clicked.connect(self._open_about)
            elif m in ("File", "Edit", "Layout") and hasattr(window, "file_menu"):
                b.setMenu({"File": window.file_menu, "Edit": window.edit_menu, "Layout": window.layout_menu}[m])
                b.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            b.setStyleSheet(f"QToolButton {{ border: none; padding: 6px 12px; font-size: 14px; color: {theme.TEXT}; }}"
                            f"QToolButton:hover {{ background: {theme.BG2}; border-radius: 8px; }}"
                            f"QToolButton::menu-indicator {{ image: none; }}")
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
        self._settings = SettingsWindow(self.win, engine=getattr(self.win, "engine", None))
        self._settings.show()

    def _open_about(self) -> None:
        from omnibots.ui.about import open_about
        self._about = open_about(self.win)

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
    folder_opened = Signal(str)                         # File → Open folder: the bots work there (A11.m.03)
    new_project = Signal()                              # File → New project: the next goal starts a fresh folder
    session_action = Signal(str, str)                   # ("new" | "close" | "clear" | "reopen", session id)
    on_top_changed = Signal(bool)                       # A17.d.03: Layout → Keep this window on top (LiveUI remembers it)
    personality_chosen = Signal(str)                    # A17.d.01: a personality key, or "" = make a new one
    mind_requested = Signal()                           # A17.d.04: Layout → The team's mind

    def __init__(self, card: BotCard, workspace: Path, team: list[tuple[str, str, str, str]] | None = None,
                 bot_id: str = "omi", computers=None, quit_on_close: bool = False):
        super().__init__()
        self.quit_on_close = quit_on_close              # only when there's no system tray (A11.b.01)
        self.computers = computers                      # runtime.computer_tools.ComputerClient (A10.f.05)
        self._computer_win = None
        self.bot_id = bot_id
        self.engine = None                              # set by LiveUI (Settings → Folders uses it)
        self.confirm = lambda title, text: QMessageBox.question(self, title, text) == QMessageBox.StandardButton.Yes
        self.pick_folder = lambda start: QFileDialog.getExistingDirectory(self, "Open folder", str(start)) or None
        self.pick_file = lambda start: QFileDialog.getOpenFileName(self, "Open file", str(start))[0] or None
        self.recent_sessions = lambda: []                 # LiveUI sets it: [(id, label)] for File → Recent sessions
        self.personalities = lambda: ({}, "default")      # LiveUI sets it: ({key: label}, this bot's key)
        self._build_actions()
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
        self.own_strip = None
        if bot_id == "omi":                              # A15.g.03: what the bots did on their own today
            from omnibots.ui.widgets import OwnStrip
            self.own_strip = OwnStrip()
            center.addWidget(self.own_strip)
        self.chat = ChatPanel(card.accent, badge=job_for_role(card.role))
        self.chat.action.connect(lambda a: self._clear_chat() if a == "clear" else self.session_action.emit("new", ""))
        self.tabs = EditorTabs(self.chat)                # Chat first; opened files beside it (A11.m.04)
        center.addWidget(self.tabs, 1)

        # right column: File Explorer on top, the Message Board below (user, 2026-09-26), resizable
        self.files = FilesPanel(workspace)
        self.files.file_opened.connect(lambda p: self.open_file(Path(p)))
        self.files.context_menu.connect(self._explorer_menu)
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

    # ── File / Edit (A11.m.04-05) ─────────────────────────────────────────
    def _build_actions(self) -> None:
        def act(text, slot, keys=None):
            a = QAction(text, self)
            if keys:
                a.setShortcut(QKeySequence(keys))
                a.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
            a.triggered.connect(lambda _=False: self._safely(slot))
            self.addAction(a)
            return a
        self.acts = {
            "new_file": act("New file…", self.new_file, "Ctrl+N"),
            "open_file": act("Open file…", self.open_file_dialog, "Ctrl+O"),
            "new_project": act("New project  (the next goal gets a fresh folder)", self.new_project.emit, None),
            "new_session": act("New session  (saves this chat to Recent sessions)", lambda: self.session_action.emit("new", ""),
                               "Ctrl+Shift+T"),
            "close_session": act("Close session", lambda: self.session_action.emit("close", ""), None),
            "clear_chat": act("Clear chat…", self._clear_chat, None),
            "open_folder": act("Open folder…  (the bots work there)", self.open_folder_dialog, "Ctrl+Shift+O"),
            "new_folder": act("Create folder…", self.new_folder, "Ctrl+Shift+N"),
            "save": act("Save", self.save, "Ctrl+S"),
            "save_as": act("Save as…", lambda: self.save(save_as=True), "Ctrl+Shift+S"),
            "close_tab": act("Close tab", self.close_tab, "Ctrl+W"),
            "undo": act("Undo", lambda: self._text("undo"), None),
            "redo": act("Redo", lambda: self._text("redo"), None),
            "cut": act("Cut", lambda: self._edit("cut"), None),
            "copy": act("Copy", lambda: self._edit("copy"), None),
            "paste": act("Paste", lambda: self._edit("paste"), None),
            "find": act("Find…", self.find, "Ctrl+F"),
            "rename": act("Rename…", self.rename, "F2"),
            "delete": act("Delete  (to the Recycle Bin)", self.delete, "Delete"),
            "duplicate": act("Duplicate", self.duplicate, "Ctrl+D"),
            "copy_path": act("Copy path", self.copy_path, None),
            "reveal": act("Show in Windows Explorer", self.reveal, None),
        }
        self.acts["delete"].setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.file_menu = QMenu(self)
        for k in ("new_session", "close_session", None):
            self.file_menu.addSeparator() if k is None else self.file_menu.addAction(self.acts[k])
        self.recent_menu = self.file_menu.addMenu("Recent sessions")
        self.file_menu.addSeparator()
        for k in ("new_project", None, "new_file", "open_file", "open_folder", "new_folder", None, "save", "save_as", None,
                  "close_tab"):
            self.file_menu.addSeparator() if k is None else self.file_menu.addAction(self.acts[k])
        self.file_menu.aboutToShow.connect(self._fill_recent)
        # Layout (A17.d): keep on top, the bot's personality, the team's mind
        self.layout_menu = QMenu(self)
        self.on_top_act = self.layout_menu.addAction("Keep this window on top")
        self.on_top_act.setCheckable(True)
        self.on_top_act.toggled.connect(lambda on: (self.set_on_top(on), self.on_top_changed.emit(on)))
        self.personality_menu = self.layout_menu.addMenu("Personality")
        self.personality_menu.aboutToShow.connect(self._fill_personalities)
        self.layout_menu.addSeparator()
        self.layout_menu.addAction("The team's mind  (3D network)", self.mind_requested.emit)
        self.edit_menu = QMenu(self)
        for k in ("undo", "redo", None, "cut", "copy", "paste", None, "find", None, "rename", "duplicate", "delete", None,
                  "copy_path", "reveal", None, "clear_chat"):
            self.edit_menu.addSeparator() if k is None else self.edit_menu.addAction(self.acts[k])

    def _fill_recent(self) -> None:
        self.recent_menu.clear()
        items = list(self.recent_sessions() or [])
        for sid, label in items:
            self.recent_menu.addAction(label, lambda sid=sid: self.session_action.emit("reopen", sid))
        if not items:
            a = self.recent_menu.addAction("(no closed sessions yet)")
            a.setEnabled(False)

    def _fill_personalities(self) -> None:
        self.personality_menu.clear()
        choices, current = self.personalities()
        for key, label in choices.items():
            a = self.personality_menu.addAction(label, lambda k=key: self.personality_chosen.emit(k))
            a.setCheckable(True)
            a.setChecked(key == current)
        self.personality_menu.addSeparator()
        self.personality_menu.addAction("New personality…", lambda: self.personality_chosen.emit(""))

    def set_on_top(self, on: bool) -> None:
        """A17.d.03: float above other windows (Qt needs the window shown again after a flag change)."""
        if bool(self.windowFlags() & Qt.WindowType.WindowStaysOnTopHint) == on:
            return
        visible = self.isVisible()
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, on)
        if visible:
            self.show()
        if self.on_top_act.isChecked() != on:
            self.on_top_act.blockSignals(True)
            self.on_top_act.setChecked(on)
            self.on_top_act.blockSignals(False)

    def _clear_chat(self) -> None:
        if self.confirm("Clear chat?", "Remove every message in this session? (To keep them, use File → New session instead.)"):
            self.session_action.emit("clear", "")

    def _safely(self, fn) -> None:
        try:
            fn()
        except Exception as exc:                        # a file problem is a message, never a crash
            QMessageBox.warning(self, "OmniBots", str(exc))

    def _focus_in_text(self) -> QPlainTextEdit | None:
        w = QApplication.focusWidget()
        return w if isinstance(w, QPlainTextEdit) else None

    def _text(self, op: str) -> None:
        ed = self._focus_in_text() or (self.tabs.current_editor().text if self.tabs.current_editor() else None)
        if ed is not None:
            getattr(ed, op)()

    def _edit(self, op: str) -> None:
        """Cut/Copy/Paste: text in the editor, files in the explorer."""
        ed = self._focus_in_text()
        if ed is not None or not isinstance(QApplication.focusWidget(), QTreeView):
            if ed is not None:
                getattr(ed, op)()
            return
        if op == "paste":
            for p in FileOps.paste(self.files.target_folder()):
                self.files.select(p)
        else:
            FileOps.copy(self.files.selected_paths(), cut=(op == "cut"))

    def open_file(self, path: Path) -> None:
        self._safely(lambda: self.tabs.open_file(path))

    def open_file_dialog(self) -> None:
        p = self.pick_file(self.files.target_folder())
        if p:
            self.open_file(Path(p))

    def open_folder_dialog(self) -> None:
        p = self.pick_folder(self.files.root)
        if p:
            self.open_folder(Path(p))

    def open_folder(self, folder: Path) -> None:
        """Load a folder into the explorer; the bots work there from the next goal on (A11.m.03)."""
        self.files.set_root(folder, working=True)
        self.folder_opened.emit(str(folder))

    def new_file(self) -> None:
        name = ask_text(self, "New file", "File name:", "notes.md")
        if name:
            p = FileOps.new_file(self.files.target_folder(), name)
            self.files.select(p)
            self.open_file(p)

    def new_folder(self) -> None:
        name = ask_text(self, "Create folder", "Folder name:", "new folder")
        if name:
            self.files.select(FileOps.new_folder(self.files.target_folder(), name))

    def save(self, save_as: bool = False) -> None:
        self.tabs.save(save_as=save_as)

    def close_tab(self) -> None:
        if self.tabs.current_editor():
            self.tabs.close_tab(self.tabs.currentIndex())

    def find(self) -> None:
        if self.tabs.current_editor():
            self.tabs.current_editor().show_find()

    def rename(self) -> None:
        sel = self.files.selected_paths()
        if len(sel) != 1:
            return
        name = ask_text(self, "Rename", "New name:", sel[0].name)
        if name and name != sel[0].name:
            new = FileOps.rename(sel[0], name)
            self.tabs.path_renamed(sel[0], new)
            self.files.select(new)

    def delete(self) -> None:
        sel = self.files.selected_paths()
        if not sel:
            return
        what = sel[0].name if len(sel) == 1 else f"{len(sel)} items"
        if self.confirm("Delete?", f"Move {what} to the Recycle Bin? (You can restore it from there.)"):
            for ed in self.tabs.editors():
                if ed.path and any(ed.path == p or p in ed.path.parents for p in sel):
                    ed.text and ed.text.document().setModified(False)
                    self.tabs.close_tab(self.tabs.indexOf(ed))
            FileOps.to_recycle_bin(sel)

    def duplicate(self) -> None:
        for p in self.files.selected_paths():
            self.files.select(FileOps.duplicate(p))

    def copy_path(self) -> None:
        sel = self.files.selected_paths() or [self.files.root]
        copy_to_clipboard("\n".join(str(p) for p in sel))

    def reveal(self) -> None:
        FileOps.reveal((self.files.selected_paths() or [self.files.root])[0])

    def _explorer_menu(self, pos) -> None:
        m = QMenu(self)
        for k in ("open_file", "new_file", "new_folder", None, "cut", "copy", "paste", "duplicate", "rename", "delete", None,
                  "copy_path", "reveal", None, "open_folder"):
            m.addSeparator() if k is None else m.addAction(self.acts[k])
        self.files.tree.setFocus()
        m.popup(self.files.tree.viewport().mapToGlobal(pos))

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
        if self.quit_on_close and not self.tabs.close_all():   # quitting: unsaved files ask first (hiding keeps them)
            e.ignore()
            return
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
