"""The 1980s-cartoon thought bubble next to Omi's face (PLAN.md A11.d).

A bumpy cloud (a ring of overlapping circles around a rounded core), a thick
navy outline, and three shrinking "thought" circles trailing to the bot's head.
Grumbles show a grawlix (@#%$) in bold red first, then Omi's line.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen

FILL = QColor("#f7f9ff")
INK = QColor("#141c3a")
GRAWLIX_INK = QColor("#e8345a")


def cloud_path(r: QRectF, bump: float = 16.0) -> QPainterPath:
    """A cartoon cloud: circles along the edge of an inner rounded rect, unioned."""
    path = QPainterPath()
    path.addRoundedRect(r.adjusted(bump * 0.6, bump * 0.6, -bump * 0.6, -bump * 0.6), bump, bump)
    perim = 2 * (r.width() + r.height())
    n = max(8, int(perim / (bump * 1.6)))
    cx, cy = r.center().x(), r.center().y()
    a, b = r.width() / 2 - bump * 0.55, r.height() / 2 - bump * 0.55
    for i in range(n):
        t = 2 * math.pi * i / n
        # superellipse points, so the cloud follows a rounded rectangle, not an oval
        ct, st = math.cos(t), math.sin(t)
        x = cx + a * math.copysign(abs(ct) ** 0.5, ct)
        y = cy + b * math.copysign(abs(st) ** 0.5, st)
        rad = bump * (0.95 + 0.25 * math.sin(i * 2.3))
        c = QPainterPath()
        c.addEllipse(QPointF(x, y), rad, rad)
        path = path.united(c)
    return path.simplified()


def draw_thought_bubble(p: QPainter, rect: QRectF, text: str, tail_to: QPointF, *,
                        grawlix: str | None = None, font_px: int = 15, outline: float = 3.0) -> None:
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(INK, outline)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    # trailing thought circles, from the bubble toward the head
    start = QPointF(rect.left() + rect.width() * 0.22, rect.bottom())
    for k, rad in enumerate((9.0, 6.0, 3.8)):
        f = (k + 1) / 4
        c = QPointF(start.x() + (tail_to.x() - start.x()) * f, start.y() + (tail_to.y() - start.y()) * f)
        p.setPen(pen)
        p.setBrush(FILL)
        p.drawEllipse(c, rad, rad)
    # soft drop shadow, then the cloud
    cloud = cloud_path(rect)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(0, 0, 0, 70))
    p.drawPath(cloud.translated(3, 4))
    p.setPen(pen)
    p.setBrush(FILL)
    p.drawPath(cloud)
    # text
    inner = rect.adjusted(22, 16, -22, -16)
    y = inner.top()
    if grawlix:
        f = QFont("Comic Sans MS")
        f.setBold(True)
        f.setPixelSize(int(font_px * 1.5))
        p.setFont(f)
        p.setPen(GRAWLIX_INK)
        h = QFontMetricsF(f).height()
        p.drawText(QRectF(inner.left(), y, inner.width(), h), Qt.AlignmentFlag.AlignHCenter, grawlix)
        y += h * 0.95
    f = QFont("Comic Sans MS")
    f.setPixelSize(font_px)
    p.setFont(f)
    p.setPen(INK)
    p.drawText(QRectF(inner.left(), y, inner.width(), inner.bottom() - y),
               Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextWordWrap, text)
    p.restore()


def bubble_size(text: str, grawlix: str | None, font_px: int = 15, width: float = 230) -> tuple[float, float]:
    f = QFont("Comic Sans MS")
    f.setPixelSize(font_px)
    fm = QFontMetricsF(f)
    lines = fm.boundingRect(QRectF(0, 0, width - 44, 1000), int(Qt.TextFlag.TextWordWrap), text).height()
    extra = fm.height() * 1.45 if grawlix else 0
    return width, max(78.0, lines + extra + 40)
