"""Live preview next to the editor (PLAN.md A17.f.01).

An HTML, SVG or Markdown file opens split: the code on the left, the page on the right in a real browser engine
(QtWebEngine, Chromium). The page refreshes as you type (after a short pause), when you save, and when a bot changes
the file on disk. Relative links (style.css, images, scripts) load from the file's own folder. Without QtWebEngine the
preview says so and offers to open the file in your browser instead.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from omnibots.ui import theme

PREVIEW_EXT = {".html", ".htm", ".svg", ".md", ".markdown"}
DEBOUNCE_MS = 600


def can_preview(path: Path | None) -> bool:
    return bool(path) and path.suffix.lower() in PREVIEW_EXT


def html_for(path: Path, text: str) -> str:
    """What the browser shows for this file's (possibly unsaved) text."""
    suffix = path.suffix.lower()
    if suffix in (".md", ".markdown"):
        from omnibots.runtime.make_docs import md_html
        return md_html(text, path.stem)
    if suffix == ".svg":
        return f"<!doctype html><body style='margin:0;display:grid;place-items:center;min-height:100vh;background:#fff'>{text}</body>"
    return text


class PreviewPane(QWidget):
    def __init__(self, path: Path, parent=None):
        super().__init__(parent)
        self.path = path
        self.renders = 0                                   # how many times the page was refreshed (tests, status)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        bar = QHBoxLayout()
        bar.setContentsMargins(10, 4, 6, 4)
        self.status = QLabel("Live preview")
        self.status.setObjectName("dim")
        bar.addWidget(self.status, 1)
        out = QPushButton("Open in browser ↗")
        out.setStyleSheet(f"QPushButton {{ background: {theme.BG3}; border: 1px solid {theme.BORDER}; border-radius: 8px; padding: 3px 10px; }}")
        out.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.path))))
        bar.addWidget(out)
        lay.addLayout(bar)
        self.view = None
        try:
            from PySide6.QtWebEngineWidgets import QWebEngineView
            self.view = QWebEngineView(self)
            lay.addWidget(self.view, 1)
        except Exception as exc:                            # no QtWebEngine: say so, the button still works
            msg = QLabel(f"The live preview needs QtWebEngine ({type(exc).__name__}). Use “Open in browser”.")
            msg.setWordWrap(True)
            lay.addWidget(msg, 1)
        self._pending: str | None = None
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._flush)

    def show_text(self, text: str, now: bool = False) -> None:
        self._pending = text
        if now:
            self.timer.stop()
            self._flush()
        else:
            self.timer.start(DEBOUNCE_MS)

    def _flush(self) -> None:
        if self._pending is None:
            return
        html, self._pending = html_for(self.path, self._pending), None
        if self.view is not None:
            # the file's folder is the base, so its css, images and scripts load like in a browser
            self.view.setHtml(html, QUrl.fromLocalFile(str(self.path.parent) + "/"))
        self.renders += 1
        self.status.setText(f"Live preview · {self.path.name}")
