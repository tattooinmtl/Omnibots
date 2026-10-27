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


def preview_allows(url: str, root: Path) -> bool:
    """What a previewed page may load (security audit 2026-10-27). The page is often a bot's work, built from web
    text, so it runs locked in: files from its own folder (its css, images, scripts) and inline data, nothing else.
    Before, it could read any file on the PC through file:// and send it out by navigating to a web address."""
    u = QUrl(url)
    scheme = u.scheme().lower()
    if scheme in ("data", "blob", "about", "qrc"):
        return True
    if scheme != "file":
        return False
    try:
        return Path(u.toLocalFile()).resolve().is_relative_to(Path(root).resolve())
    except (OSError, ValueError):
        return False


def locked_view(parent, root: Path):
    """A QWebEngineView whose page loads nothing remote and no file outside `root`, opens no windows and never
    navigates away from the preview itself (a clicked link does nothing)."""
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings, QWebEngineUrlRequestInterceptor
    from PySide6.QtWebEngineWidgets import QWebEngineView

    class Guard(QWebEngineUrlRequestInterceptor):
        def interceptRequest(self, info) -> None:
            if not preview_allows(info.requestUrl().toString(), root):
                info.block(True)

    class Page(QWebEnginePage):
        def acceptNavigationRequest(self, url, nav_type, is_main_frame) -> bool:
            return preview_allows(url.toString(), root) and nav_type != QWebEnginePage.NavigationType.NavigationTypeLinkClicked

        def createWindow(self, _type):
            return None

    view = QWebEngineView(parent)
    page = Page(view)
    page.guard = Guard(page)
    page.setUrlRequestInterceptor(page.guard)
    s = page.settings()
    s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
    s.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, False)
    s.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanAccessClipboard, False)
    s.setAttribute(QWebEngineSettings.WebAttribute.LocalStorageEnabled, False)
    view.setPage(page)
    return view


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
            self.view = locked_view(self, path.parent)
            self.view.loadFinished.connect(self._loaded)
            lay.addWidget(self.view, 1)
        except Exception as exc:                            # no QtWebEngine: say so, the button still works
            msg = QLabel(f"The live preview needs QtWebEngine ({type(exc).__name__}). Use “Open in browser”.")
            msg.setWordWrap(True)
            lay.addWidget(msg, 1)
        self._pending: str | None = None
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._flush)

    def _loaded(self, ok: bool) -> None:
        self.status.setText(f"Live preview · {self.path.name}" if ok else
                            "The preview couldn't load here (the browser engine didn't start). Use “Open in browser”.")

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
