"""Settings → Tokens: add, test and remove the tokens bots use on other sites (ADR-7, A9.c.04).

Values go straight into the vault (Windows Credential Manager). This page never shows one:
the box is masked, a saved token reads as "saved", and a test reports only who it belongs to.
"""

from __future__ import annotations

from html import escape
from typing import Any, Callable

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QSplitter, QVBoxLayout, QWidget,
)

from omnibots.security.token_catalog import CATALOG, CUSTOM, Site, by_key, check
from omnibots.security.vault import NAME_OK, SecretError
from omnibots.ui import theme

HIDDEN_KINDS = {"computer"}          # auto-provisioned logins (Bot Computers): not the user's to edit here


class TokensPage(QWidget):
    def __init__(self, engine=None, *, vault=None, run: Callable[[Any], Any] | None = None):
        super().__init__()
        self.engine = engine
        self._vault = vault
        self._run = run
        self._pending: list[tuple[Any, Callable[[Any], None]]] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        title = QLabel("Tokens")
        title.setStyleSheet("font-size: 16px; font-weight: 600;")
        root.addWidget(title)
        note = QLabel("Tokens for the sites bots work on (GitHub, Vercel, …). Each is kept in Windows "
                      "Credential Manager and may only be sent to the hosts listed with it. Bots never see "
                      "the value, and neither does this page once it's saved. Model keys are under Providers.")
        note.setWordWrap(True)
        note.setObjectName("dim")
        root.addWidget(note)

        split = QSplitter()
        split.setChildrenCollapsible(False)
        self.list = QListWidget()
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.list.currentItemChanged.connect(lambda _c, _p: self._show_selected())
        split.addWidget(self.list)

        form_host = QWidget()
        col = QVBoxLayout(form_host)
        col.setContentsMargins(12, 8, 0, 0)
        self.heading = QLabel()
        self.heading.setStyleSheet("font-size: 15px; font-weight: 600;")
        col.addWidget(self.heading)
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.hint.setObjectName("dim")
        col.addWidget(self.hint)
        self.get_btn = QPushButton("Get a token ↗")
        self.get_btn.clicked.connect(self.open_get_url)
        get_row = QHBoxLayout()
        get_row.addWidget(self.get_btn)
        get_row.addStretch(1)
        col.addLayout(get_row)

        form = QFormLayout()
        self.name = QLineEdit()
        self.name.setPlaceholderText("my_site_token")
        self.hosts = QLineEdit()
        self.hosts.setPlaceholderText("api.example.com (comma-separated)")
        self.note = QLineEdit()
        self.value = QLineEdit()
        self.value.setEchoMode(QLineEdit.EchoMode.Password)
        paste = QPushButton("Paste")
        paste.setToolTip("Paste from the clipboard (and clear the clipboard)")
        paste.clicked.connect(self.paste)
        vrow = QHBoxLayout()
        vrow.addWidget(self.value, 1)
        vrow.addWidget(paste)
        form.addRow("Name", self.name)
        form.addRow("Sent only to", self.hosts)
        form.addRow("Note", self.note)
        form.addRow("Token", vrow)
        col.addLayout(form)

        buttons = QHBoxLayout()
        self.save_btn = QPushButton("Save")
        self.save_btn.setObjectName("primary")
        self.save_btn.clicked.connect(self.save)
        self.test_btn = QPushButton("Test")
        self.test_btn.clicked.connect(self.test)
        self.remove_btn = QPushButton("Remove")
        self.remove_btn.clicked.connect(self.remove)
        for b in (self.save_btn, self.test_btn, self.remove_btn):
            buttons.addWidget(b)
        buttons.addStretch(1)
        col.addLayout(buttons)
        self.status = QLabel()
        self.status.setWordWrap(True)
        col.addWidget(self.status)
        col.addStretch(1)
        split.addWidget(form_host)
        split.setSizes([260, 560])
        root.addWidget(split, 1)

        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._poll)
        self.reload()

    # ── the vault, on the engine's loop ───────────────────────────────────
    @property
    def vault(self):
        return self._vault if self._vault is not None else getattr(self.engine, "vault", None)

    def _call(self, coro, done: Callable[[Any], None]) -> None:
        """Run a vault/HTTP coroutine without freezing the window; `done` gets the result or the exception."""
        if self._run is not None:
            try:
                done(self._run(coro))
            except Exception as exc:          # noqa: BLE001 - shown to the user
                done(exc)
            return
        submit = getattr(self.engine, "submit", None)
        if submit is None:
            coro.close()
            done(RuntimeError("OmniBots is still starting"))
            return
        try:
            self._pending.append((submit(coro), done))
        except Exception as exc:              # noqa: BLE001
            done(exc)
            return
        self._timer.start()

    def _poll(self) -> None:
        still = []
        for fut, done in self._pending:
            if fut.done():
                exc = fut.exception()
                done(exc if exc is not None else fut.result())
            else:
                still.append((fut, done))
        self._pending = still
        if not still:
            self._timer.stop()

    # ── list ──────────────────────────────────────────────────────────────
    def _saved(self) -> dict:
        v = self.vault
        return {i.name: i for i in v.names() if i.kind not in HIDDEN_KINDS} if v is not None else {}

    def reload(self, select: str | None = None) -> None:
        current = select or self._current_key()
        saved = self._saved()
        self.list.blockSignals(True)
        self.list.clear()
        rows = [(s.key, s.label) for s in CATALOG] + [(n, n) for n in saved if by_key(n) is None]
        for key, label in rows:
            mark = "✔" if key in saved else "○"
            item = QListWidgetItem(f"{mark}  {label}")
            item.setData(256, key)
            self.list.addItem(item)
        item = QListWidgetItem("＋  Custom (any other site)")
        item.setData(256, "")
        self.list.addItem(item)
        self.list.blockSignals(False)
        keys = [self.list.item(i).data(256) for i in range(self.list.count())]
        self.list.setCurrentRow(keys.index(current) if current in keys else 0)
        self._show_selected()
        if self.vault is None:
            self._say("OmniBots is still starting: the vault isn't open yet.", bad=True)

    def _current_key(self) -> str | None:
        item = self.list.currentItem()
        return item.data(256) if item is not None else None

    def _site(self) -> Site:
        key = self._current_key() or ""
        return by_key(key) or (Site(key, key, ()) if key else CUSTOM)

    def _show_selected(self) -> None:
        site = self._site()
        info = self._saved().get(site.key) if site.key else None
        self.heading.setText(site.label if site.key or site is CUSTOM else site.key)
        self.hint.setText(site.hint)
        self.hint.setVisible(bool(site.hint))
        self.get_btn.setVisible(bool(site.get_url))
        self.name.setText(site.key)
        self.name.setReadOnly(by_key(site.key) is not None or info is not None)
        self.hosts.setText(", ".join(info.hosts if info else site.hosts))
        self.note.setText(info.note if info else "")
        self.value.clear()
        self.value.setPlaceholderText("saved: paste a new one to replace it" if info else "paste the token here")
        self.test_btn.setEnabled(bool(site.test_url) and (info is not None))
        self.remove_btn.setEnabled(info is not None)
        self.status.clear()

    # ── actions ───────────────────────────────────────────────────────────
    def open_get_url(self) -> None:
        if self._site().get_url:
            QDesktopServices.openUrl(QUrl(self._site().get_url))

    def paste(self) -> None:
        cb = QGuiApplication.clipboard()
        text = (cb.text() or "").strip()
        if not text:
            self._say("The clipboard is empty: copy the token first.", bad=True)
            return
        self.value.setText(text)
        cb.clear()
        self._say("Pasted (and cleared from the clipboard). Press Save.")

    def save(self) -> None:
        v = self.vault
        site = self._site()
        name = self.name.text().strip()
        value = self.value.text().strip()
        hosts = [h.strip().lower() for h in self.hosts.text().replace(";", ",").split(",") if h.strip()]
        if v is None:
            return self._say("OmniBots is still starting: try again in a moment.", bad=True)
        if not NAME_OK.match(name):
            return self._say("A name is 1-64 letters, digits, _ . -", bad=True)
        if not value:
            return self._say("Paste the token first.", bad=True)
        if any(c.isspace() for c in value):
            return self._say("That has spaces or line breaks in it: copy just the token.", bad=True)
        if not hosts and not self.confirm("Any host?", f"No host is listed, so bots could send {name} to any site. Save anyway?"):
            return
        kind = site.kind if by_key(name) else "token"

        def saved(result) -> None:
            if isinstance(result, Exception):
                return self._say(str(result) if isinstance(result, SecretError) else f"Couldn't save: {type(result).__name__}", bad=True)
            self.reload(select=name)
            where = ", ".join(result.hosts) or "any host"
            self._say(f"Saved {name} (only for {where}).")
            if by_key(name) and by_key(name).test_url:
                self.test(value)

        self.value.clear()
        self._call(v.set(name, value, kind=kind, note=self.note.text().strip(), hosts=hosts), saved)

    def test(self, value: str | None = None) -> None:
        site = self._site()
        v = self.vault
        if not site.test_url:
            return self._say("There's no automatic test for this one.", bad=True)
        value = value or (v.value(site.key) if v is not None else None)
        if not value:
            return self._say("Save a token first.", bad=True)
        self._say(f"Asking {site.label}…")

        def tested(result) -> None:
            if isinstance(result, Exception):
                return self._say(f"Test failed: {type(result).__name__}", bad=True)
            ok, text = result
            self._say(text, bad=not ok)

        self._call(check(site, value), tested)

    def remove(self) -> None:
        v = self.vault
        name = self._site().key
        if v is None or not name:
            return
        if not self.confirm("Remove token", f"Remove {name} from the vault? Bots that use it will stop working "
                                              f"until you add one again. (This doesn't revoke it on the site.)"):
            return

        def removed(result) -> None:
            if isinstance(result, Exception):
                return self._say(f"Couldn't remove: {type(result).__name__}", bad=True)
            self.reload(select=name if by_key(name) else None)
            self._say(f"Removed {name}. Revoke it on the site too if you no longer need it.")

        self._call(v.delete(name), removed)

    def confirm(self, title: str, body: str) -> bool:
        answer = QMessageBox.question(self, title, body, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                      QMessageBox.StandardButton.No)
        return answer == QMessageBox.StandardButton.Yes

    def _say(self, text: str, *, bad: bool = False) -> None:
        color = theme.RED if bad else theme.GREEN
        mark = "✖" if bad else "✔"
        self.status.setText(f"<span style='color:{color}'>{mark} {escape(text)}</span>")
