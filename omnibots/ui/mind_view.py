"""The team's mind as a 3D network (PLAN.md A17.d.04), like OmniOne's neural view.

Every bot is a big node, every tool and skill a bot holds a small one, linked to the bots that hold it. The nodes sit
on a slowly turning sphere (drawn in perspective with QPainter: no OpenGL needed), and light up live: a bot's node
glows while it thinks, and when it uses a tool the bot, the link and the tool flash. Drag to turn it, scroll to zoom,
hover a node to see its name. Opened from Layout → The team's mind.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from PySide6.QtCore import QPointF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QRadialGradient
from PySide6.QtWidgets import QWidget

from omnibots.ui import theme

KIND_COLOR = {"bot": theme.ACCENT_CYAN, "tool": "#7c4dff", "skill": "#3ad07a"}


@dataclass
class Node:
    key: str
    label: str
    kind: str                         # bot | tool | skill
    pos: tuple[float, float, float]   # on the unit sphere (bots nearer the middle)
    color: str = ""
    glow: float = 0.0                 # 0..1, fades
    busy: bool = False                # a bot that's working right now


@dataclass
class Graph:
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: dict[tuple[str, str], float] = field(default_factory=dict)   # (bot, item) -> glow


def _sphere(i: int, n: int, r: float) -> tuple[float, float, float]:
    """Fibonacci sphere: n points spread evenly."""
    y = 1 - (i + 0.5) * 2 / max(1, n)
    rad = math.sqrt(max(0.0, 1 - y * y))
    th = i * math.pi * (3 - math.sqrt(5))
    return (math.cos(th) * rad * r, y * r, math.sin(th) * rad * r)


def build_graph(bots: list[dict]) -> Graph:
    g = Graph()
    items: dict[str, tuple[str, str]] = {}
    for b in bots:
        for t in b.get("tools") or []:
            items.setdefault(f"tool:{t}", ("tool", t))
        for s in b.get("skills") or []:
            items.setdefault(f"skill:{s}", ("skill", s))
    for i, b in enumerate(bots):
        g.nodes[f"bot:{b['id']}"] = Node(f"bot:{b['id']}", b.get("name") or b["id"], "bot", _sphere(i, len(bots), 0.42),
                                         theme.role_color(b.get("role") or ""), busy=bool(b.get("active")))
    for i, (key, (kind, label)) in enumerate(sorted(items.items())):
        g.nodes[key] = Node(key, label.replace("mcp:", "MCP "), kind, _sphere(i, len(items), 1.0), KIND_COLOR[kind])
    for b in bots:
        for t in b.get("tools") or []:
            g.edges[(f"bot:{b['id']}", f"tool:{t}")] = 0.0
        for s in b.get("skills") or []:
            g.edges[(f"bot:{b['id']}", f"skill:{s}")] = 0.0
    return g


class MindView(QWidget):
    def __init__(self, bots: list[dict] | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("OmniBots · The team's mind")
        self.setMinimumSize(520, 420)
        self.resize(900, 720)
        self.setMouseTracking(True)
        self.graph = build_graph(bots or [])
        self.yaw, self.pitch, self.zoom = 0.0, 0.35, 1.0
        self.auto_spin = True
        self._drag: QPointF | None = None
        self._hover: str | None = None
        self._screen: dict[str, tuple[float, float, float]] = {}
        self._last = time.monotonic()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(33)

    # ── live data ───────────────────────────────────────────────────────
    def set_bots(self, bots: list[dict]) -> None:
        glow = {k: n.glow for k, n in self.graph.nodes.items()}
        self.graph = build_graph(bots)
        for k, v in glow.items():
            if k in self.graph.nodes:
                self.graph.nodes[k].glow = v
        self.update()

    def pulse_tool(self, bot_id: str, tool: str) -> None:
        b, t = f"bot:{bot_id}", f"tool:{tool}"
        if t not in self.graph.nodes:                    # a tool used through a relay or MCP: show it anyway
            self.graph.nodes[t] = Node(t, tool, "tool", _sphere(len(self.graph.nodes), len(self.graph.nodes) + 1, 1.0), KIND_COLOR["tool"])
        for k in (b, t):
            if k in self.graph.nodes:
                self.graph.nodes[k].glow = 1.0
        self.graph.edges[(b, t)] = 1.0

    def set_busy(self, bot_id: str, busy: bool) -> None:
        n = self.graph.nodes.get(f"bot:{bot_id}")
        if n:
            n.busy = busy
            if busy:
                n.glow = max(n.glow, 0.6)

    # ── drawing ─────────────────────────────────────────────────────────
    def _tick(self) -> None:
        now = time.monotonic()
        dt, self._last = now - self._last, now
        if self.auto_spin and self._drag is None:
            self.yaw += dt * 0.18
        fade = 0.5 ** (dt / 0.9)
        for n in self.graph.nodes.values():
            n.glow *= fade
            if n.busy:
                n.glow = max(n.glow, 0.35 + 0.25 * math.sin(now * 4))
        for k in list(self.graph.edges):
            self.graph.edges[k] *= fade
        self.update()

    def project(self, p: tuple[float, float, float]) -> tuple[float, float, float]:
        x, y, z = p
        cy, sy, cp, sp = math.cos(self.yaw), math.sin(self.yaw), math.cos(self.pitch), math.sin(self.pitch)
        x, z = x * cy - z * sy, x * sy + z * cy
        y, z = y * cp - z * sp, y * sp + z * cp
        d = 3.2
        f = d / (d + z)
        s = min(self.width(), self.height()) * 0.38 * self.zoom
        return self.width() / 2 + x * f * s, self.height() / 2 + y * f * s, f

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor("#050814"))
        bg = QRadialGradient(QPointF(self.width() / 2, self.height() / 2), max(self.width(), self.height()) * 0.6)
        bg.setColorAt(0, QColor(47, 125, 255, 40))
        bg.setColorAt(1, QColor(0, 0, 0, 0))
        p.fillRect(self.rect(), bg)
        self._screen = {k: self.project(n.pos) for k, n in self.graph.nodes.items()}
        for (a, b), glow in self.graph.edges.items():
            if a not in self._screen or b not in self._screen:
                continue
            (x1, y1, f1), (x2, y2, f2) = self._screen[a], self._screen[b]
            c = QColor(self.graph.nodes[a].color)
            c.setAlpha(int(25 + 200 * glow) if glow > 0.02 else int(18 + 30 * (f1 + f2 - 1.4)))
            p.setPen(QPen(c, 1 + 2.5 * glow))
            p.drawLine(QPointF(x1, y1), QPointF(x2, y2))
        font = QFont(p.font())
        for key in sorted(self._screen, key=lambda k: self._screen[k][2]):          # far ones first
            n, (x, y, f) = self.graph.nodes[key], self._screen[key]
            r = (13 if n.kind == "bot" else 5) * f * self.zoom ** 0.5
            c = QColor(n.color)
            if n.glow > 0.03:
                halo = QRadialGradient(QPointF(x, y), r * (2.5 + 3 * n.glow))
                hc = QColor(c)
                hc.setAlpha(int(170 * n.glow))
                halo.setColorAt(0, hc)
                hc.setAlpha(0)
                halo.setColorAt(1, hc)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(halo)
                p.drawEllipse(QPointF(x, y), r * (2.5 + 3 * n.glow), r * (2.5 + 3 * n.glow))
            c.setAlpha(int(110 + 145 * min(1.0, (f - 0.7) * 2 + n.glow)))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c)
            p.drawEllipse(QPointF(x, y), r, r)
            if n.kind == "bot" or key == self._hover or n.glow > 0.3:
                font.setPointSizeF(10.5 if n.kind == "bot" else 8.5)
                font.setBold(n.kind == "bot")
                p.setFont(font)
                tc = QColor(theme.TEXT)
                tc.setAlpha(int(150 + 105 * min(1.0, f - 0.6 + n.glow)))
                p.setPen(tc)
                p.drawText(QPointF(x + r + 4, y + 4), n.label)
        p.setPen(QColor(theme.TEXT_DIM))
        font.setPointSizeF(9)
        font.setBold(False)
        p.setFont(font)
        bots = sum(1 for n in self.graph.nodes.values() if n.kind == "bot")
        p.drawText(14, self.height() - 14, f"{bots} bots · {len(self.graph.nodes) - bots} tools and skills · "
                                           "drag to turn, scroll to zoom, double-click to stop the spin")
        p.end()

    # ── mouse ───────────────────────────────────────────────────────────
    def node_at(self, x: float, y: float) -> str | None:
        best, dist = None, 14.0
        for k, (sx, sy, _) in self._screen.items():
            d = math.hypot(sx - x, sy - y)
            if d < dist:
                best, dist = k, d
        return best

    def mousePressEvent(self, e) -> None:
        self._drag = e.position()

    def mouseMoveEvent(self, e) -> None:
        if self._drag is not None:
            d = e.position() - self._drag
            self.yaw += d.x() * 0.008
            self.pitch = max(-1.4, min(1.4, self.pitch + d.y() * 0.008))
            self._drag = e.position()
        else:
            self._hover = self.node_at(e.position().x(), e.position().y())
            n = self.graph.nodes.get(self._hover or "")
            self.setToolTip(f"{n.kind}: {n.label}" if n else "")

    def mouseReleaseEvent(self, _e) -> None:
        self._drag = None

    def mouseDoubleClickEvent(self, _e) -> None:
        self.auto_spin = not self.auto_spin

    def wheelEvent(self, e) -> None:
        self.zoom = max(0.4, min(3.0, self.zoom * (1.0015 ** e.angleDelta().y())))
