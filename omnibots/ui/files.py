"""The File Explorer's file work and the built-in editor (PLAN.md A11.m.03-05).

  FileOps       new file/folder, rename, duplicate, copy/cut/paste, delete to the Recycle Bin,
                copy path, show in Windows Explorer (plain functions, testable without a window)
  Highlighter   light syntax colors for code and text files
  EditorTab     one open file: text (with Undo/Redo/Find) or an image
  EditorTabs    Chat stays the first tab; files open beside it. Unsaved tabs show a ● and ask
                before closing; a file the bots change on disk reloads unless you have unsaved edits.

Deleting always goes to the Recycle Bin (undoable in Windows), after a Yes/No.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QRegularExpression, Qt, Signal
from PySide6.QtGui import QColor, QFont, QKeySequence, QPixmap, QShortcut, QSyntaxHighlighter, QTextCharFormat, QTextDocument
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QInputDialog, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QScrollArea, QTabWidget,
    QVBoxLayout, QWidget,
)

from omnibots.ui import theme

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".svg"}
MAX_OPEN_BYTES = 5_000_000


# ── file operations ────────────────────────────────────────────────────────
class FileOps:
    clipboard: tuple[list[Path], bool] = ([], False)          # (paths, cut?) shared by every window

    @staticmethod
    def new_file(folder: Path, name: str) -> Path:
        p = Path(folder) / name
        if p.exists():
            raise FileExistsError(f"{p.name} already exists")
        p.write_text("", encoding="utf-8")
        return p

    @staticmethod
    def new_folder(folder: Path, name: str) -> Path:
        p = Path(folder) / name
        p.mkdir()
        return p

    @staticmethod
    def rename(path: Path, new_name: str) -> Path:
        if not new_name or re.search(r'[<>:"/\\|?*]', new_name):
            raise ValueError(f"{new_name!r} isn't a valid name")
        target = Path(path).with_name(new_name)
        if target.exists():
            raise FileExistsError(f"{new_name} already exists")
        return Path(path).rename(target)

    @staticmethod
    def free_name(folder: Path, name: str) -> Path:
        p = Path(folder) / name
        stem, suf, n = p.stem, p.suffix, 2
        while p.exists():
            p = Path(folder) / f"{stem} ({n}){suf}"
            n += 1
        return p

    @classmethod
    def duplicate(cls, path: Path) -> Path:
        path = Path(path)
        target = cls.free_name(path.parent, f"{path.stem} copy{path.suffix}")
        (shutil.copytree if path.is_dir() else shutil.copy2)(path, target)
        return target

    @classmethod
    def copy(cls, paths: list[Path], cut: bool = False) -> None:
        cls.clipboard = ([Path(p) for p in paths], cut)

    @classmethod
    def paste(cls, folder: Path) -> list[Path]:
        paths, cut = cls.clipboard
        out = []
        for p in paths:
            if not p.exists():
                continue
            target = cls.free_name(folder, p.name)
            if cut:
                shutil.move(str(p), str(target))
            else:
                (shutil.copytree if p.is_dir() else shutil.copy2)(p, target)
            out.append(target)
        if cut:
            cls.clipboard = ([], False)
        return out

    @staticmethod
    def to_recycle_bin(paths: list[Path]) -> None:
        """Delete to the Recycle Bin (the user can restore it). Windows shell; elsewhere a plain delete."""
        paths = [Path(p) for p in paths if Path(p).exists()]
        if not paths:
            return
        if sys.platform != "win32":
            for p in paths:
                shutil.rmtree(p) if p.is_dir() else p.unlink()
            return
        import ctypes
        from ctypes import wintypes

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                        ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                        ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]
        FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 3, 0x4, 0x10, 0x40, 0x400
        op = SHFILEOPSTRUCTW(None, FO_DELETE, "\0".join(str(p.resolve()) for p in paths) + "\0\0", None,
                             FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI, False, None, None)
        rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        if rc != 0 or any(p.exists() for p in paths):
            raise OSError(f"couldn't move to the Recycle Bin (code {rc})")

    @staticmethod
    def reveal(path: Path) -> None:
        """Show it in Windows Explorer."""
        path = Path(path)
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", str(path)] if path.is_file() else ["explorer", str(path)])
        else:
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path if path.is_dir() else path.parent)))


# ── syntax colors ──────────────────────────────────────────────────────────
KEYWORDS = {
    "py": "and as assert async await break class continue def del elif else except False finally for from global if import in is "
          "lambda None nonlocal not or pass raise return True try while with yield self",
    "js": "async await break case catch class const continue default delete do else export extends false finally for function if "
          "import in instanceof let new null return super switch this throw true try typeof undefined var void while yield",
    "css": "",
    "html": "",
}
LANG = {".py": "py", ".pyw": "py", ".js": "js", ".mjs": "js", ".ts": "js", ".tsx": "js", ".jsx": "js", ".json": "js",
        ".css": "css", ".scss": "css", ".html": "html", ".htm": "html", ".svg": "html", ".xml": "html", ".md": "md",
        ".toml": "py", ".yml": "py", ".yaml": "py", ".sh": "py", ".ps1": "py", ".sql": "js"}


def _fmt(color: str, bold: bool = False, italic: bool = False) -> QTextCharFormat:
    f = QTextCharFormat()
    f.setForeground(QColor(color))
    if bold:
        f.setFontWeight(QFont.Weight.Bold)
    f.setFontItalic(italic)
    return f


class Highlighter(QSyntaxHighlighter):
    def __init__(self, doc: QTextDocument, lang: str):
        super().__init__(doc)
        self.rules: list[tuple[QRegularExpression, QTextCharFormat]] = []
        kw, string, comment, number, tag, attr = (_fmt("#8ab4ff", True), _fmt("#9be28f"), _fmt("#6b7a99", italic=True),
                                                  _fmt("#f5b86b"), _fmt("#6fe3ff"), _fmt("#c9a6ff"))
        words = KEYWORDS.get(lang, "").split()
        if words:
            self.rules.append((QRegularExpression(r"\b(" + "|".join(words) + r")\b"), kw))
        self.rules.append((QRegularExpression(r"\b\d+(\.\d+)?\b"), number))
        if lang in ("html",):
            self.rules += [(QRegularExpression(r"</?[\w:-]+|/?>"), tag), (QRegularExpression(r"\b[\w:-]+(?==)"), attr)]
        if lang == "css":
            self.rules += [(QRegularExpression(r"[.#]?[\w-]+(?=\s*\{)"), tag), (QRegularExpression(r"[\w-]+(?=\s*:)"), attr)]
        if lang == "md":
            self.rules += [(QRegularExpression(r"^#{1,6} .*"), kw), (QRegularExpression(r"`[^`]+`"), string),
                           (QRegularExpression(r"\*\*[^*]+\*\*"), _fmt(theme.TEXT, True))]
        self.rules.append((QRegularExpression(r"\"[^\"\n]*\"|'[^'\n]*'"), string))
        if lang in ("py",):
            self.rules.append((QRegularExpression(r"#[^\n]*"), comment))
        if lang in ("js", "css"):
            self.rules.append((QRegularExpression(r"//[^\n]*|/\*.*\*/"), comment))
        if lang == "html":
            self.rules.append((QRegularExpression(r"<!--.*-->"), comment))

    def highlightBlock(self, text: str) -> None:
        for rx, fmt in self.rules:
            it = rx.globalMatch(text)
            while it.hasNext():
                m = it.next()
                self.setFormat(m.capturedStart(), m.capturedLength(), fmt)


# ── one open file ──────────────────────────────────────────────────────────
class EditorTab(QWidget):
    dirty_changed = Signal(bool)

    def __init__(self, path: Path | None = None, parent=None):
        super().__init__(parent)
        self.path = Path(path) if path else None
        self.is_image = bool(self.path and self.path.suffix.lower() in IMAGE_EXT and self.path.suffix.lower() != ".svg")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.find_bar = QLineEdit()
        self.find_bar.setPlaceholderText("Find…  (Enter = next, Esc = close)")
        self.find_bar.hide()
        self.find_bar.returnPressed.connect(self.find_next)
        lay.addWidget(self.find_bar)
        if self.is_image:
            self.text = None
            area = QScrollArea()
            area.setWidgetResizable(True)
            area.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.image = QLabel()
            self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
            area.setWidget(self.image)
            lay.addWidget(area)
        else:
            self.text = QPlainTextEdit()
            f = QFont("Cascadia Mono")
            f.setStyleHint(QFont.StyleHint.Monospace)
            f.setPixelSize(14)
            self.text.setFont(f)
            self.text.setTabStopDistance(4 * self.text.fontMetrics().horizontalAdvance(" "))
            self.text.setStyleSheet(f"QPlainTextEdit {{ background: {theme.BG1}; color: {theme.TEXT}; border: none; padding: 10px; }}")
            self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
            self.preview = None
            from omnibots.ui.preview import PreviewPane, can_preview
            if can_preview(self.path):                     # A17.f.01: code left, the live page right
                from PySide6.QtWidgets import QSplitter
                split = QSplitter(Qt.Orientation.Horizontal)
                split.addWidget(self.text)
                self.preview = PreviewPane(self.path)
                split.addWidget(self.preview)
                split.setSizes([520, 480])
                lay.addWidget(split)
                self.text.textChanged.connect(lambda: self.preview.show_text(self.text.toPlainText()))
            else:
                lay.addWidget(self.text)
            self.highlighter = Highlighter(self.text.document(), LANG.get(self.path.suffix.lower(), "") if self.path else "")
            self.text.document().modificationChanged.connect(self.dirty_changed.emit)
            QShortcut(QKeySequence("Escape"), self.find_bar, activated=self._close_find)
        if self.path:
            self.load()

    @property
    def dirty(self) -> bool:
        return bool(self.text and self.text.document().isModified())

    @property
    def title(self) -> str:
        return (self.path.name if self.path else "untitled") + (" ●" if self.dirty else "")

    def load(self) -> None:
        if self.is_image:
            pm = QPixmap(str(self.path))
            self.image.setPixmap(pm if not pm.isNull() else QPixmap())
            if pm.isNull():
                self.image.setText("can't show this image")
            return
        if self.path.stat().st_size > MAX_OPEN_BYTES:
            raise ValueError(f"{self.path.name} is over 5 MB; open it with another program")
        data = self.path.read_bytes()
        if b"\0" in data[:4096]:
            raise ValueError(f"{self.path.name} looks like a binary file")
        self.text.setPlainText(data.decode("utf-8", errors="replace"))
        self.text.document().setModified(False)
        if getattr(self, "preview", None) is not None:      # opened, or a bot changed it on disk: show it now
            self.preview.show_text(self.text.toPlainText(), now=True)

    def save(self, path: Path | None = None) -> Path:
        if self.text is None:
            raise ValueError("images can't be saved from here")
        target = Path(path) if path else self.path
        if target is None:
            raise ValueError("choose where to save it (Save as)")
        target.write_text(self.text.toPlainText(), encoding="utf-8", newline="")
        if target != self.path:
            self.path = target
            self.highlighter.setDocument(None)
            self.highlighter = Highlighter(self.text.document(), LANG.get(target.suffix.lower(), ""))
        self.text.document().setModified(False)
        if getattr(self, "preview", None) is not None:
            self.preview.path = target
            self.preview.show_text(self.text.toPlainText(), now=True)
        return target

    # Find
    def show_find(self) -> None:
        if self.text is None:
            return
        self.find_bar.show()
        self.find_bar.setFocus()
        self.find_bar.selectAll()

    def _close_find(self) -> None:
        self.find_bar.hide()
        if self.text:
            self.text.setFocus()

    def find_next(self) -> bool:
        if not self.text or not self.find_bar.text():
            return False
        if self.text.find(self.find_bar.text()):
            return True
        c = self.text.textCursor()                     # wrap around
        c.movePosition(c.MoveOperation.Start)
        self.text.setTextCursor(c)
        return self.text.find(self.find_bar.text())


# ── tabs: Chat + open files ────────────────────────────────────────────────
class EditorTabs(QTabWidget):
    saved = Signal(str)                                  # a path you saved (the bots see it on disk)

    def __init__(self, chat: QWidget, parent=None, *, ask=None, ask_path=None):
        super().__init__(parent)
        self.ask = ask or self._ask                       # (title, text) -> "save" | "discard" | "cancel"
        self.ask_path = ask_path or self._ask_path        # (start) -> Path | None
        self.setDocumentMode(False)                       # document mode paints a flat grey base under the tabs
        self.setTabsClosable(True)
        self.setMovable(True)
        self.tabBar().setDrawBase(False)
        self.tabBar().setExpanding(False)
        self.addTab(chat, "💬  Chat")
        self.tabBar().setTabButton(0, self.tabBar().ButtonPosition.RightSide, None)   # Chat can't be closed
        self.tabCloseRequested.connect(self.close_tab)
        # rounded glass pills on the panel's dark blue (user, 2026-09-26: no grey strip, less square ends)
        self.setStyleSheet(f"""
            QTabWidget::pane {{ border: none; background: transparent; top: 2px; }}
            QTabWidget::tab-bar {{ left: 0px; }}
            QTabBar {{ background: transparent; }}
            QTabBar::tab {{
                background: rgba(17, 26, 53, 150); color: {theme.TEXT_DIM};
                border: 1px solid {theme.BORDER}; border-radius: 14px;
                padding: 6px 12px 6px 16px; margin: 4px 0 6px 6px; min-height: 16px;
            }}
            QTabBar::tab:hover {{ background: {theme.BG3}; color: {theme.TEXT}; }}
            QTabBar::tab:selected {{
                color: {theme.TEXT}; border: 1px solid {theme.ACCENT};
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 rgba(47, 125, 255, 90), stop:1 rgba(124, 77, 255, 80));
            }}
            QTabBar::close-button {{ subcontrol-position: right; margin-left: 6px; }}
        """)
        self.watcher = QFileSystemWatcher(self)
        self.watcher.fileChanged.connect(self._changed_on_disk)

    def editors(self) -> list[EditorTab]:
        return [self.widget(i) for i in range(self.count()) if isinstance(self.widget(i), EditorTab)]

    def current_editor(self) -> EditorTab | None:
        w = self.currentWidget()
        return w if isinstance(w, EditorTab) else None

    def open_file(self, path: Path) -> EditorTab:
        path = Path(path)
        for ed in self.editors():
            if ed.path and ed.path.resolve() == path.resolve():
                self.setCurrentWidget(ed)
                return ed
        ed = EditorTab(path)
        self._add(ed)
        self.watcher.addPath(str(path))
        return ed

    def new_file(self) -> EditorTab:
        ed = EditorTab(None)
        self._add(ed)
        return ed

    def _add(self, ed: EditorTab) -> None:
        i = self.addTab(ed, ed.title)
        self.setTabToolTip(i, str(ed.path or "not saved yet"))
        ed.dirty_changed.connect(lambda _d, e=ed: self._retitle(e))
        self.setCurrentIndex(i)

    def _retitle(self, ed: EditorTab) -> None:
        i = self.indexOf(ed)
        if i >= 0:
            self.setTabText(i, ed.title)
            self.setTabToolTip(i, str(ed.path or "not saved yet"))

    def save(self, ed: EditorTab | None = None, *, save_as: bool = False) -> Path | None:
        ed = ed or self.current_editor()
        if ed is None or ed.text is None:
            return None
        target = ed.path
        if save_as or target is None:
            target = self.ask_path(ed.path)
            if target is None:
                return None
        if ed.path:
            self.watcher.removePath(str(ed.path))
        out = ed.save(target)
        self.watcher.addPath(str(out))
        self._retitle(ed)
        self.saved.emit(str(out))
        return out

    def close_tab(self, index: int) -> bool:
        ed = self.widget(index)
        if not isinstance(ed, EditorTab):
            return False                                  # the Chat tab stays
        if ed.dirty:
            choice = self.ask("Unsaved changes", f"Save your changes to {ed.path.name if ed.path else 'this file'}?")
            if choice == "cancel":
                return False
            if choice == "save" and self.save(ed) is None:
                return False
        if ed.path:
            self.watcher.removePath(str(ed.path))
        self.removeTab(index)
        ed.deleteLater()
        return True

    def close_all(self) -> bool:
        for ed in list(self.editors()):
            if not self.close_tab(self.indexOf(ed)):
                return False
        return True

    def _changed_on_disk(self, path: str) -> None:
        """A bot (or another program) changed an open file: reload it unless you have unsaved edits."""
        for ed in self.editors():
            if ed.path and str(ed.path) == path and ed.path.exists():
                if not ed.dirty:
                    ed.load()
                if path not in self.watcher.files():     # some editors replace the file: watch it again
                    self.watcher.addPath(path)

    def path_renamed(self, old: Path, new: Path) -> None:
        for ed in self.editors():
            if ed.path and ed.path == old:
                self.watcher.removePath(str(old))
                ed.path = new
                self.watcher.addPath(str(new))
                self._retitle(ed)

    @staticmethod
    def _ask(title: str, text: str) -> str:
        b = QMessageBox.question(None, title, text, QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
                                 | QMessageBox.StandardButton.Cancel)
        return {QMessageBox.StandardButton.Save: "save", QMessageBox.StandardButton.Discard: "discard"}.get(b, "cancel")

    @staticmethod
    def _ask_path(start: Path | None) -> Path | None:
        p, _ = QFileDialog.getSaveFileName(None, "Save as", str(start or Path.home()))
        return Path(p) if p else None


def ask_text(parent, title: str, label: str, text: str = "") -> str | None:
    value, ok = QInputDialog.getText(parent, title, label, QLineEdit.EchoMode.Normal, text)
    return value.strip() if ok and value.strip() else None


def copy_to_clipboard(text: str) -> None:
    QApplication.clipboard().setText(text)
