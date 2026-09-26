"""The bot's live screen (PLAN.md A10.f.05, user 2026-09-26: "a button to launch this Docker so
the user can visualise the work done in Docker from the bot window").

The 🖥 Computer button on a bot window: log in as that bot (its secret from the vault),
start its computer if it's off, ask the gateway for a 10-minute screen link, and show the
noVNC screen in an embedded browser. Watch by default; "Take over" gives you the mouse and
keyboard (the handoff flow: a login or a verification the bot must not do).
"""

from __future__ import annotations

import asyncio
import threading

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMainWindow, QPushButton, QVBoxLayout, QWidget

from omnibots.ui import theme

WAIT_PAGE = """<html><body style="margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
background:#070b18;color:#8b97b8;font:16px 'Segoe UI'">{msg}</body></html>"""


async def screen_link(client, bot: str, view_only: bool) -> str:
    """Start the bot's computer if needed, then get a screen link (as the bot itself)."""
    st = await client.call(bot, "GET", "/computer")
    if st.status_code != 200:
        raise RuntimeError(client.error(st))
    if not st.json().get("running"):
        r = await client.call(bot, "POST", "/computer/start")
        if r.status_code != 200:
            raise RuntimeError(client.error(r))
    r = await client.call(bot, "POST", "/auth/screen-link", json={"bot": bot, "view_only": view_only})
    if r.status_code != 200:
        raise RuntimeError(client.error(r))
    return r.json()["url"]


class _Fetch(QObject):
    done = Signal(str)
    failed = Signal(str)


class ComputerWindow(QMainWindow):
    def __init__(self, client, bot_id: str, bot_name: str, parent=None):
        super().__init__(parent)
        from PySide6.QtWebEngineWidgets import QWebEngineView
        self.client, self.bot_id = client, bot_id
        self.view_only = True
        self.setWindowTitle(f"OmniBots · {bot_name}'s computer")
        self.setStyleSheet(theme.stylesheet() + f"QMainWindow {{ background: {theme.BG0}; }}")
        self.resize(1320, 900)
        body = QWidget()
        v = QVBoxLayout(body)
        v.setContentsMargins(10, 10, 10, 10)
        bar = QHBoxLayout()
        self.status = QLabel(f"🖥 {bot_name}'s computer")
        self.status.setStyleSheet("font-size: 15px; font-weight: 600;")
        bar.addWidget(self.status)
        bar.addStretch(1)
        self.mode = QPushButton("🖐 Take over")
        self.mode.clicked.connect(self._toggle)
        reload_ = QPushButton("⟳ Reconnect")
        reload_.clicked.connect(self.load)
        for b in (self.mode, reload_):
            bar.addWidget(b)
        v.addLayout(bar)
        self.web = QWebEngineView()
        v.addWidget(self.web, 1)
        self.setCentralWidget(body)
        self._sig = _Fetch()
        self._sig.done.connect(self._show)
        self._sig.failed.connect(self._fail)
        self.load()

    def load(self) -> None:
        self.web.setHtml(WAIT_PAGE.format(msg="Starting the computer and connecting to its screen…"))
        view_only = self.view_only

        def work():
            try:
                self._sig.done.emit(asyncio.run(screen_link(self.client, self.bot_id, view_only)))
            except Exception as exc:                                  # shown in the window, never raised
                self._sig.failed.emit(str(exc))
        threading.Thread(target=work, name=f"screen-{self.bot_id}", daemon=True).start()

    def _show(self, url: str) -> None:
        self.web.setUrl(QUrl(url))
        self.status.setText(("👀 Watching" if self.view_only else "🖐 You have the mouse and keyboard") + f" · {self.bot_id}")

    def _fail(self, msg: str) -> None:
        hint = ("<br><br><span style='font-size:13px'>The bot needs a computer login: on the VPS run "
                f"<code>bots add {self.bot_id}</code> and store the secret as <code>computer_{self.bot_id}</code> "
                "in the vault.</span>") if "no computer login" in msg else ""
        self.web.setHtml(WAIT_PAGE.format(msg=f"✖ {msg}{hint}"))
        self.status.setText("Not connected")

    def _toggle(self) -> None:
        self.view_only = not self.view_only
        self.mode.setText("🖐 Take over" if self.view_only else "👀 Just watch")
        self.load()
