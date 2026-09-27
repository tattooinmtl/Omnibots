"""Tray → Token allocations (PLAN.md A9.c.03): background tokens per bot, per project, for today.

Every open project, and the bots created in it (idle or working): what each used today on work
it started on its own, what it's allocated, and whether it's waiting on you for more. + buttons
add tokens for today. Omi and work you start aren't limited, so they aren't listed.
The engine is asked in the background; the window never waits on it.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QDialog, QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton, QScrollArea,
                               QVBoxLayout, QWidget)

from omnibots.ui import theme
from omnibots.ui.props import job_for_role
from omnibots.ui.widgets import mini_face

STEPS = (100_000, 500_000)


def k(n: int) -> str:
    return f"{n / 1_000_000:.1f}M" if n >= 1_000_000 else f"{round(n / 1000):,}k"


class AllocationsWindow(QDialog):
    _loaded = Signal(object)                       # the engine's answer, delivered on the UI thread
    _done = Signal(str)

    def __init__(self, engine, parent=None, *, refresh_ms: int = 5000):
        super().__init__(parent)
        self.engine = engine
        self.setWindowTitle("Token allocations")
        self.setStyleSheet(theme.stylesheet() + f"QDialog {{ background: {theme.BG0}; }}")
        self.resize(640, 520)
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 16)
        root.setSpacing(10)
        title = QLabel("Token allocations")
        title.setStyleSheet("font-size: 20px; font-weight: 700;")
        root.addWidget(title)
        self.intro = QLabel("")
        self.intro.setWordWrap(True)
        self.intro.setObjectName("dim")
        root.addWidget(self.intro)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setStyleSheet(f"QScrollArea, QScrollArea > QWidget > QWidget {{ background: {theme.BG0}; }}")
        root.addWidget(self.scroll, 1)
        bottom = QHBoxLayout()
        self.status = QLabel("")
        self.status.setObjectName("dim")
        bottom.addWidget(self.status, 1)
        again = QPushButton("Refresh")
        again.clicked.connect(self.refresh)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        bottom.addWidget(again)
        bottom.addWidget(close)
        root.addLayout(bottom)
        self._loaded.connect(self.show_data)
        self._done.connect(self._after_allocate)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(refresh_ms)
        self.data: dict[str, Any] = {}
        self.refresh()

    # ── engine ─────────────────────────────────────────────────────────
    def refresh(self) -> None:
        if not self.isVisible() and self.data:
            return
        try:
            fut = self.engine.submit(self.engine.ui_allocations())
        except Exception as exc:
            self.status.setText(f"The engine isn't ready: {exc}")
            return
        fut.add_done_callback(lambda f: self._loaded.emit(f.result()) if not f.exception() else None)

    def allocate(self, project_id: str, bot_id: str, name: str, tokens: int) -> None:
        self.status.setText(f"Adding {k(tokens)} for {name}…")
        fut = self.engine.submit(self.engine.allocate_tokens(project_id, bot_id, tokens))
        fut.add_done_callback(lambda f: self._done.emit(
            f"✔ {name}: {k(f.result())} allocated today" if not f.exception() else f"✖ {f.exception()}"))

    def _after_allocate(self, text: str) -> None:
        self.status.setText(text)
        self.refresh()

    # ── view ───────────────────────────────────────────────────────────
    def show_data(self, data: dict[str, Any]) -> None:
        self.data = data
        per = int(data.get("per_bot") or 0)
        self.intro.setText(
            "Work the bots start on their own (checks, repairs, routines, the night shift) is limited per bot, per "
            f"project, per day: <b>{k(per) if per else 'no limit'}</b> each (settings: background_tokens_per_bot). "
            "A bot that runs out asks Omi, and Omi asks you. Omi and the work you start aren't limited.")
        body = QWidget()
        body.setObjectName("allocBody")
        body.setStyleSheet(f"QWidget#allocBody {{ background: {theme.BG0}; }}")
        v = QVBoxLayout(body)
        v.setContentsMargins(0, 0, 6, 0)
        v.setSpacing(12)
        projects = data.get("projects", [])
        if not any(p["bots"] for p in projects):
            empty = QLabel("No bots in an open project yet. They show up here once Omi gives them a job.")
            empty.setObjectName("dim")
            empty.setWordWrap(True)
            v.addWidget(empty)
        for p in projects:
            if not p["bots"]:
                continue
            head = QLabel(f"<b>{p['goal'][:70]}</b>  <span style='color:{theme.TEXT_DIM}'>· on its own: {p['autonomy'].capitalize()}</span>")
            head.setWordWrap(True)
            v.addWidget(head)
            for b in p["bots"]:
                v.addWidget(self._row(p["id"], b))
        v.addStretch(1)
        self.scroll.setWidget(body)

    def _row(self, project_id: str, b: dict[str, Any]) -> QFrame:
        asking = bool(b.get("asking"))
        row = QFrame()
        row.setObjectName("alloc")
        edge = theme.AMBER if asking else theme.BORDER
        row.setStyleSheet(f"QFrame#alloc {{ background: {theme.BG1}; border: 1px solid {edge}; border-radius: 12px; }}")
        h = QHBoxLayout(row)
        h.setContentsMargins(12, 8, 12, 8)
        face = QLabel()
        face.setPixmap(QPixmap(mini_face(theme.role_color(b["role"]), "happy", 32, badge=job_for_role(b["role"]))))
        face.setStyleSheet("background: transparent; border: none;")
        h.addWidget(face)
        mid = QVBoxLayout()
        used, alloc = int(b["used"]), int(b["allocated"])
        name = QLabel(f"<b>{b['name']}</b> <span style='color:{theme.TEXT_DIM}'>{b['role']} · {b['state']}</span>"
                      + (f"  <span style='color:{theme.AMBER}'>· asked Omi for more</span>" if asking else ""))
        name.setStyleSheet("background: transparent; border: none;")
        mid.addWidget(name)
        bar = QProgressBar()
        bar.setTextVisible(True)
        bar.setFixedHeight(16)
        if alloc:
            bar.setRange(0, alloc)
            bar.setValue(min(used, alloc))
            bar.setFormat(f"{k(used)} of {k(alloc)} today")
        else:
            bar.setRange(0, 1)
            bar.setValue(0)
            bar.setFormat(f"{k(used)} today · no limit")
        mid.addWidget(bar)
        h.addLayout(mid, 1)
        for step in STEPS:
            btn = QPushButton(f"+{k(step)}")
            btn.setToolTip(f"Allocate {step:,} more background tokens to {b['name']} in this project, for today"
                           + (" (also answers its waiting request)" if asking else ""))
            btn.setEnabled(bool(alloc))
            btn.clicked.connect(lambda _=False, s=step, bb=b: self.allocate(project_id, bb["id"], bb["name"], s))
            h.addWidget(btn)
        return row


def open_allocations(engine, parent=None) -> AllocationsWindow:
    w = AllocationsWindow(engine, parent)
    w.show()
    w.raise_()
    return w
