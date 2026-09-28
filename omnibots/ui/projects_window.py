"""Tray → Projects… (PLAN.md A11.a.03, the first panel of A11.a.01's main window).

Every project, open ones first: the goal, status, the autonomy dial (Off/Watch/Fix, A15.b.02), the
live address (A15.c.03), jobs, tokens today and in total, and your verdict (A16.b). Open folder, and
Close project (A15.f.02: it ends the project for good; refused while work still runs in it).
The engine is asked in the background; the window never waits on it.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QComboBox, QDialog, QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton, QScrollArea,
                               QVBoxLayout, QWidget)

from omnibots.ui import theme

LEVELS = ("off", "watch", "fix")
LEVEL_TIPS = {"off": "Starts nothing on its own here", "watch": "Omi checks and reports; no fixes",
              "fix": "Omi's checks may start repair jobs"}
STATUS_COLOR = {"open": theme.ACCENT_CYAN, "done": theme.GREEN, "failed": theme.RED, "cancelled": theme.TEXT_DIM}


DARK_WIDGETS = (f"QComboBox {{ background: {theme.BG2}; color: {theme.TEXT}; border: 1px solid {theme.BORDER}; border-radius: 6px; "
                f"padding: 3px 8px; }} QComboBox QAbstractItemView {{ background: {theme.BG2}; color: {theme.TEXT}; "
                f"selection-background-color: {theme.BG3}; }} QHeaderView::section {{ background: {theme.BG2}; "
                f"color: {theme.TEXT_DIM}; border: none; padding: 4px 6px; }} QPushButton:disabled {{ color: {theme.TEXT_DIM}; "
                f"border-color: {theme.BG2}; }}")


def k(n: int) -> str:
    return f"{n / 1_000_000:.1f}M" if n >= 1_000_000 else f"{round(n / 1000):,}k" if n >= 1000 else str(n)


class ProjectsWindow(QDialog):
    _loaded = Signal(object)
    _done = Signal(str)

    def __init__(self, engine, parent=None, *, refresh_ms: int = 5000, confirm=None):
        super().__init__(parent)
        self.engine = engine
        self.confirm = confirm or (lambda title, text: QMessageBox.question(self, title, text) == QMessageBox.StandardButton.Yes)
        self.setWindowTitle("Projects")
        self.setStyleSheet(theme.stylesheet() + f"QDialog {{ background: {theme.BG0}; }}" + DARK_WIDGETS)
        self.resize(760, 560)
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 16)
        root.setSpacing(10)
        title = QLabel("Projects")
        title.setStyleSheet("font-size: 20px; font-weight: 700;")
        root.addWidget(title)
        intro = QLabel("Open projects first. The dial says what the bots may start on their own there; Close ends a project for "
                       "good (Omi stops watching it).")
        intro.setWordWrap(True)
        intro.setObjectName("dim")
        root.addWidget(intro)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet(f"QScrollArea, QScrollArea > QWidget > QWidget {{ background: {theme.BG0}; }}")
        root.addWidget(self.scroll, 1)
        bottom = QHBoxLayout()
        self.status = QLabel("")
        self.status.setObjectName("dim")
        bottom.addWidget(self.status, 1)
        for text, slot in (("Refresh", self.refresh), ("Close", self.close)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            bottom.addWidget(b)
        root.addLayout(bottom)
        self._loaded.connect(self.show_data)
        self._done.connect(self._after)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(refresh_ms)
        self.data: list[dict[str, Any]] = []
        self.refresh()

    # ── engine ─────────────────────────────────────────────────────────
    def refresh(self) -> None:
        if not self.isVisible() and self.data:
            return
        try:
            fut = self.engine.submit(self.engine.ui_projects())
        except Exception as exc:
            self.status.setText(f"The engine isn't ready: {exc}")
            return
        fut.add_done_callback(lambda f: self._loaded.emit(f.result()) if not f.exception() else None)

    def _ask(self, coro, ok_text) -> None:
        fut = self.engine.submit(coro)
        fut.add_done_callback(lambda f: self._done.emit(ok_text(f.result()) if not f.exception() else f"✖ {f.exception()}"))

    def _after(self, text: str) -> None:
        self.status.setText(text)
        self.refresh()

    def set_level(self, p: dict[str, Any], level: str) -> None:
        if level != p["autonomy"]:
            self._ask(self.engine.set_autonomy(p["id"], level), lambda r: f"{p['goal'][:40]}: on its own = {r.capitalize()}")

    def close_project(self, p: dict[str, Any]) -> None:
        if self.confirm("Close this project?", f"Close “{p['goal'][:80]}” for good?\n\nOmi stops watching it and nothing starts "
                        "there on its own. The files stay where they are."):
            self._ask(self.engine.close_project(p["id"]), lambda r: f"{p['goal'][:40]}: closed")

    # ── view ───────────────────────────────────────────────────────────
    def show_data(self, rows: list[dict[str, Any]]) -> None:
        self.data = rows
        body = QWidget()
        body.setObjectName("projBody")
        body.setStyleSheet(f"QWidget#projBody {{ background: {theme.BG0}; }}")
        v = QVBoxLayout(body)
        v.setContentsMargins(0, 0, 6, 0)
        v.setSpacing(10)
        if not rows:
            empty = QLabel("No projects yet. Give Omi a goal and it shows up here.")
            empty.setObjectName("dim")
            v.addWidget(empty)
        for p in rows:
            v.addWidget(self._row(p))
        v.addStretch(1)
        self.scroll.setWidget(body)

    def _row(self, p: dict[str, Any]) -> QFrame:
        closed = p["status"] == "cancelled"
        row = QFrame()
        row.setObjectName("proj")
        row.setStyleSheet(f"QFrame#proj {{ background: {theme.BG1}; border: 1px solid {theme.BORDER}; border-radius: 12px; }}"
                          "QLabel { background: transparent; border: none; }")
        v = QVBoxLayout(row)
        v.setContentsMargins(14, 10, 14, 10)
        v.setSpacing(6)
        top = QHBoxLayout()
        verdict = {1: "👍 ", -1: "👎 "}.get(p.get("verdict"), "")
        goal = QLabel(f"{verdict}<b>{p['goal'][:90]}</b>")
        goal.setWordWrap(True)
        top.addWidget(goal, 1)
        shown = "closed" if closed else p["status"]
        badge = QLabel(shown)
        badge.setStyleSheet(f"color: {STATUS_COLOR.get(p['status'], theme.TEXT_DIM)}; font-weight: 600;")
        top.addWidget(badge)
        v.addLayout(top)
        info = QHBoxLayout()
        details = (f"{p['jobs']} job(s)" + (f", {p['active_jobs']} active" if p["active_jobs"] else "")
                   + f"  ·  tokens today {k(p['tokens_today'])}, total {k(p['tokens'])}  ·  started {str(p['created_at'])[:10]}")
        d = QLabel(details)
        d.setObjectName("dim")
        d.setWordWrap(True)
        info.addWidget(d, 1)
        v.addLayout(info)
        if p.get("live_url"):
            live = QLabel(f"Live: <a style='color:{theme.ACCENT_CYAN}' href='{p['live_url']}'>{p['live_url']}</a>")
            live.setOpenExternalLinks(True)
            v.addWidget(live)
        actions = QHBoxLayout()
        dial_label = QLabel("On its own:")
        dial_label.setObjectName("dim")
        actions.addWidget(dial_label)
        dial = QComboBox()
        for i, level in enumerate(LEVELS):
            dial.addItem(level.capitalize(), level)
            dial.setItemData(i, LEVEL_TIPS[level], Qt.ItemDataRole.ToolTipRole)
        dial.setCurrentIndex(LEVELS.index(p["autonomy"]) if p["autonomy"] in LEVELS else 1)
        dial.setEnabled(not closed)
        dial.currentIndexChanged.connect(lambda i, pp=p, dd=dial: self.set_level(pp, dd.itemData(i)))
        actions.addWidget(dial)
        actions.addStretch(1)
        folder = QPushButton("Open folder")
        folder.clicked.connect(lambda _=False, f=p.get("folder") or p.get("path"): QDesktopServices.openUrl(QUrl.fromLocalFile(f)))
        actions.addWidget(folder)
        close = QPushButton("Close project")
        close.setEnabled(not closed and not p["active_jobs"])
        close.setToolTip("Already closed" if closed else "Stop its running jobs first" if p["active_jobs"] else
                         "End this project for good: Omi stops watching it")
        close.clicked.connect(lambda _=False, pp=p: self.close_project(pp))
        actions.addWidget(close)
        v.addLayout(actions)
        return row


def open_projects(engine, parent=None) -> ProjectsWindow:
    w = ProjectsWindow(engine, parent)
    w.show()
    w.raise_()
    return w
