"""Tray → Usage & health… (PLAN.md A13.a.01, A13.a.02).

Tokens and model calls over the last two weeks (a bar per day), per provider (with 429s and errors),
per bot (with your 👍 share, A16.b) and per project; then each bot's health over the last week (jobs
done / failed / interrupted, average job time, stalls) and what's slowing the team down.
The engine is asked in the background; the window never waits on it.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (QDialog, QFrame, QHBoxLayout, QHeaderView, QLabel, QPushButton, QScrollArea, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from omnibots.ui import theme


DARK_WIDGETS = (f"QComboBox {{ background: {theme.BG2}; color: {theme.TEXT}; border: 1px solid {theme.BORDER}; border-radius: 6px; "
                f"padding: 3px 8px; }} QComboBox QAbstractItemView {{ background: {theme.BG2}; color: {theme.TEXT}; "
                f"selection-background-color: {theme.BG3}; }} QHeaderView::section {{ background: {theme.BG2}; "
                f"color: {theme.TEXT_DIM}; border: none; padding: 4px 6px; }} QPushButton:disabled {{ color: {theme.TEXT_DIM}; "
                f"border-color: {theme.BG2}; }}")


def k(n: int) -> str:
    return f"{n / 1_000_000:.1f}M" if n >= 1_000_000 else f"{round(n / 1000):,}k" if n >= 1000 else str(n)


class DayBars(QWidget):
    """Tokens per day as bars, the newest on the right, the day's total on hover (tooltip)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.days: list[dict[str, Any]] = []
        self.setMinimumHeight(130)
        self.setMouseTracking(True)

    def set_days(self, days: list[dict[str, Any]]) -> None:
        self.days = days
        self.update()

    def _bars(self) -> list[tuple[QRectF, dict[str, Any]]]:
        if not self.days:
            return []
        w, h = self.width(), self.height() - 22
        top = max(d["tokens"] for d in self.days) or 1
        step = w / len(self.days)
        out = []
        for i, d in enumerate(self.days):
            bh = max(2.0, (h - 8) * d["tokens"] / top)
            out.append((QRectF(i * step + step * 0.18, h - bh, step * 0.64, bh), d))
        return out

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.days:
            p.setPen(QColor(theme.TEXT_DIM))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No model calls in this period yet")
            return
        for rect, d in self._bars():
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(theme.ACCENT_CYAN))
            p.drawRoundedRect(rect, 3, 3)
            p.setPen(QColor(theme.TEXT_DIM))
            p.drawText(QRectF(rect.x() - 10, self.height() - 18, rect.width() + 20, 16), Qt.AlignmentFlag.AlignCenter, d["day"][5:])
        p.end()

    def mouseMoveEvent(self, ev) -> None:
        for rect, d in self._bars():
            if rect.adjusted(-4, -200, 4, 0).contains(ev.position()):
                self.setToolTip(f"{d['day']}: {d['tokens']:,} tokens, {d['calls']:,} model calls")
                return
        self.setToolTip("")


def table(headers: list[str], rows: list[list[Any]]) -> QTableWidget:
    t = QTableWidget(len(rows), len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().setVisible(False)
    t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    t.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
    t.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    for c in range(1, len(headers)):
        t.horizontalHeader().setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
    for r, row in enumerate(rows):
        for c, val in enumerate(row):
            item = QTableWidgetItem(str(val))
            if c:
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            t.setItem(r, c, item)
    t.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    t.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    t.resizeRowsToContents()
    t.setFixedHeight(t.horizontalHeader().sizeHint().height() + sum(t.rowHeight(r) for r in range(len(rows))) + 2 * t.frameWidth() + 2)
    t.setStyleSheet(f"QTableWidget {{ background: {theme.BG1}; border: 1px solid {theme.BORDER}; border-radius: 8px; }}")
    return t


class UsageWindow(QDialog):
    _loaded = Signal(object)

    def __init__(self, engine, parent=None, *, refresh_ms: int = 15000, days: int = 14):
        super().__init__(parent)
        self.engine, self.days = engine, days
        self.setWindowTitle("Usage & health")
        self.setStyleSheet(theme.stylesheet() + f"QDialog {{ background: {theme.BG0}; }}" + DARK_WIDGETS)
        self.resize(760, 680)
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 16)
        root.setSpacing(10)
        title = QLabel("Usage & health")
        title.setStyleSheet("font-size: 20px; font-weight: 700;")
        root.addWidget(title)
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
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(refresh_ms)
        self.data: dict[str, Any] = {}
        self.refresh()

    def refresh(self) -> None:
        if not self.isVisible() and self.data:
            return
        try:
            fut = self.engine.submit(self.engine.ui_stats(self.days))
        except Exception as exc:
            self.status.setText(f"The engine isn't ready: {exc}")
            return
        fut.add_done_callback(lambda f: self._loaded.emit(f.result()) if not f.exception() else None)

    @staticmethod
    def _section(v: QVBoxLayout, text: str) -> None:
        h = QLabel(text)
        h.setStyleSheet("font-size: 15px; font-weight: 600; margin-top: 8px;")
        v.addWidget(h)

    def show_data(self, data: dict[str, Any]) -> None:
        self.data = data
        u, hl, names = data["usage"], data["health"], data.get("names", {})
        goals = data.get("projects", {})
        shares = {x["id"]: x for x in data.get("verdicts", {}).get("bot_id", [])}
        body = QWidget()
        body.setObjectName("useBody")
        body.setStyleSheet(f"QWidget#useBody {{ background: {theme.BG0}; }}")
        v = QVBoxLayout(body)
        v.setContentsMargins(0, 0, 6, 0)
        v.setSpacing(8)
        calls = sum(d["calls"] for d in u["per_day"])
        limited = sum(p["rate_limited"] for p in u["per_provider"])
        tiles = QHBoxLayout()
        for label, val in ((f"tokens, last {u['days']} days", k(u["total_tokens"])), ("model calls", f"{calls:,}"),
                           ("rate limits (429)", f"{limited:,}")):
            f = QFrame()
            f.setObjectName("tile")
            f.setStyleSheet(f"QFrame#tile {{ background: {theme.BG1}; border: 1px solid {theme.BORDER}; border-radius: 12px; }}"
                            "QLabel { background: transparent; border: none; }")
            fv = QVBoxLayout(f)
            big = QLabel(val)
            big.setStyleSheet(f"font-size: 22px; font-weight: 700; color: {theme.ACCENT_CYAN};")
            small = QLabel(label)
            small.setObjectName("dim")
            fv.addWidget(big)
            fv.addWidget(small)
            tiles.addWidget(f)
        v.addLayout(tiles)
        bars = DayBars()
        bars.set_days(u["per_day"])
        v.addWidget(bars)
        self._section(v, "Per provider")
        v.addWidget(table(["Provider", "Tokens", "Calls", "429s", "Errors"],
                          [[p["id"], k(p["tokens"]), p["calls"], p["rate_limited"], p["errors"]] for p in u["per_provider"]]))
        self._section(v, "Per bot")
        v.addWidget(table(["Bot", "Tokens", "Calls", "Your 👍"],
                          [[names.get(b["id"], b["id"]), k(b["tokens"]), b["calls"],
                            (f"{shares[b['id']]['share_up']:.0%} of {shares[b['id']]['up'] + shares[b['id']]['down']}"
                             if b["id"] in shares else "–")] for b in u["per_bot"]]))
        self._section(v, "Per project")
        v.addWidget(table(["Project", "Tokens", "Calls"],
                          [[goals.get(p["id"], p["id"])[:60], k(p["tokens"]), p["calls"]] for p in u["per_project"]]))
        self._section(v, f"Health, last {hl['days']} days")
        v.addWidget(table(["Bot", "Jobs", "Done", "Failed", "Interrupted", "Avg time", "Stalls"],
                          [[names.get(b["id"], b["id"]), b["jobs"], b["completed"], b["failed"], b["interrupted"],
                            f"{b['avg_seconds'] / 60:.1f} min" if b["avg_seconds"] else "–", b["stalls"]] for b in hl["bots"]]))
        self._section(v, "What slows the team down")
        def named(line: str) -> str:
            for bid, name in names.items():
                line = line.replace(f"{bid} ", f"{name} ")
            return line
        slow = QLabel("\n".join(f"• {named(x)}" for x in hl["bottlenecks"]) or "Nothing stands out.")
        slow.setWordWrap(True)
        v.addWidget(slow)
        v.addStretch(1)
        self.scroll.setWidget(body)


def open_usage(engine, parent=None) -> UsageWindow:
    w = UsageWindow(engine, parent)
    w.show()
    w.raise_()
    return w
