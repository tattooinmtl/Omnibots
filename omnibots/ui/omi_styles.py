"""Alternative cute Omi styles for the user to pick from (A11.d.02 design round 2).

  B "chibi": big round head, huge shiny anime eyes right on a soft white face, big blush, tiny mouth.
  C "kitty": a round bot with cat ears, a dark screen face, kawaii ^ω^ eyes and an ω mouth.
Style A ("visor") is omi_face.render_face. All three share the 240-unit box and moods.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QRadialGradient

from omnibots.ui.omi_face import render_face

INK = QColor("#1b2140")
PUPIL = QColor("#232a52")
BLUSH = QColor(255, 120, 165, 150)


def _c(color, a):
    c = QColor(color)
    c.setAlpha(a)
    return c


def _start(size: int):
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(size / 240.0, size / 240.0)
    return img, p


def _halo(p, acc, t):
    pulse = 0.5 + 0.5 * math.sin(t * 3)
    g = QRadialGradient(QPointF(120, 130), 118)
    g.setColorAt(0.62, _c(acc, 0))
    g.setColorAt(0.82, _c(acc, int(70 + 50 * pulse)))
    g.setColorAt(1.0, _c(acc, 0))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(g))
    p.drawEllipse(QRectF(2, 12, 236, 236))


def _pen(color, w):
    pen = QPen(color, w)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def _arc(p, c: QPointF, w: float, up=True, width=8, color=INK):
    p.setPen(_pen(color, width))
    p.setBrush(Qt.BrushStyle.NoBrush)
    path = QPainterPath(QPointF(c.x() - w / 2, c.y() + (5 if up else -5)))
    path.quadTo(c.x(), c.y() + (-15 if up else 15), c.x() + w / 2, c.y() + (5 if up else -5))
    p.drawPath(path)


def _star(p, c: QPointF, r: float, color: QColor):
    path = QPainterPath()
    for i in range(10):
        a = -math.pi / 2 + i * math.pi / 5
        rr = r if i % 2 == 0 else r * 0.45
        pt = QPointF(c.x() + rr * math.cos(a), c.y() + rr * math.sin(a))
        path.moveTo(pt) if i == 0 else path.lineTo(pt)
    path.closeSubpath()
    p.setPen(_pen(INK, 4))
    p.setBrush(color)
    p.drawPath(path)


# ── B: chibi ────────────────────────────────────────────────────────────────
def render_chibi(size: int, mood: str = "happy", accent: str = "#2f7dff", t: float = 0.6) -> QImage:
    img, p = _start(size)
    acc = QColor(accent)
    _halo(p, acc, t)
    bob = math.sin(t * 2.2) * 3
    # antenna with a star
    p.setPen(_pen(INK, 6))
    p.drawLine(QPointF(120, 52), QPointF(120, 26 + bob))
    _star(p, QPointF(120, 20 + bob), 17, acc.lighter(120))
    # ear bolts
    for x in (28, 212):
        g = QRadialGradient(QPointF(x - 3, 128), 16)
        g.setColorAt(0, acc.lighter(160))
        g.setColorAt(1, acc)
        p.setPen(_pen(INK, 6))
        p.setBrush(QBrush(g))
        p.drawEllipse(QPointF(x, 132), 15, 17)
    # head
    head = QRectF(34, 48, 172, 164)
    hg = QLinearGradient(head.topLeft(), head.bottomLeft())
    hg.setColorAt(0, QColor("#ffffff"))
    hg.setColorAt(1, QColor("#dfe7fb"))
    p.setPen(_pen(INK, 9))
    p.setBrush(QBrush(hg))
    p.drawRoundedRect(head, 78, 74)
    p.setPen(Qt.PenStyle.NoPen)                                   # gloss
    p.setBrush(_c("#ffffff", 230))
    p.drawEllipse(QRectF(62, 64, 34, 15))
    # blush
    p.setBrush(BLUSH if mood not in ("sad", "error") else _c(BLUSH, 80))
    p.drawEllipse(QRectF(56, 150, 30, 17))
    p.drawEllipse(QRectF(154, 150, 30, 17))
    L, R = QPointF(88, 126), QPointF(152, 126)
    if mood == "joy":
        _arc(p, L, 34, width=9); _arc(p, R, 34, width=9)
    elif mood == "sleepy":
        _arc(p, L, 30, up=False, width=8); _arc(p, R, 30, up=False, width=8)
    else:
        dy = -6 if mood == "thinking" else (4 if mood == "sad" else 0)
        dx = 5 if mood == "thinking" else 0
        for c in (L, R):
            c = c + QPointF(dx * 0, dy * 0)
            eg = QLinearGradient(QPointF(c.x(), c.y() - 26), QPointF(c.x(), c.y() + 26))
            eg.setColorAt(0, QColor("#141a38"))
            eg.setColorAt(1, QColor("#35407a"))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(eg))
            p.drawEllipse(c, 21, 26)
            lower = QPainterPath()                                # blue iris shine at the bottom
            lower.addEllipse(c + QPointF(0, 11), 13, 8)
            p.setBrush(_c(acc.lighter(150), 170))
            p.drawPath(lower)
            p.setBrush(QColor("white"))                           # sparkles, shifted by gaze
            p.drawEllipse(c + QPointF(-7 + dx, -10 + dy), 8, 8)
            p.drawEllipse(c + QPointF(8 + dx, 4 + dy), 3.5, 3.5)
        if mood == "sad":
            p.setPen(_pen(INK, 5))
            p.drawLine(L + QPointF(-18, -34), L + QPointF(10, -40))
            p.drawLine(R + QPointF(-10, -40), R + QPointF(18, -34))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#86d6ff"))
            p.drawEllipse(R + QPointF(20, 18), 5, 8)
    # mouth
    m = QPointF(120, 162)
    if mood == "joy":
        path = QPainterPath(m + QPointF(-13, -3))
        path.quadTo(m + QPointF(0, 18), m + QPointF(13, -3))
        path.closeSubpath()
        p.setPen(_pen(INK, 5))
        p.setBrush(QColor("#ff6f9d"))
        p.drawPath(path)
    elif mood == "sad":
        _arc(p, m + QPointF(0, 6), 18, up=True, width=5)
    elif mood == "thinking":
        p.setPen(_pen(INK, 5))
        p.drawLine(m + QPointF(-6, 0), m + QPointF(6, -2))
    else:                                                         # cat "w" mouth
        p.setPen(_pen(INK, 5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        path = QPainterPath(m + QPointF(-12, -2))
        path.quadTo(m + QPointF(-6, 6), m + QPointF(0, -1))
        path.quadTo(m + QPointF(6, 6), m + QPointF(12, -2))
        p.drawPath(path)
    p.end()
    return img


# ── C: kitty-bot ────────────────────────────────────────────────────────────
def render_kitty(size: int, mood: str = "happy", accent: str = "#2f7dff", t: float = 0.6) -> QImage:
    img, p = _start(size)
    acc = QColor(accent)
    _halo(p, acc, t)
    body = acc.lighter(165)
    # ears
    for sx in (-1, 1):
        ear = QPainterPath(QPointF(120 + sx * 34, 66))
        ear.lineTo(QPointF(120 + sx * 86, 26))
        ear.lineTo(QPointF(120 + sx * 88, 92))
        ear.closeSubpath()
        p.setPen(_pen(INK, 8))
        p.setBrush(body)
        p.drawPath(ear)
        inner = QPainterPath(QPointF(120 + sx * 48, 70))
        inner.lineTo(QPointF(120 + sx * 80, 44))
        inner.lineTo(QPointF(120 + sx * 80, 84))
        inner.closeSubpath()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#ff9ec4"))
        p.drawPath(inner)
    # head
    head = QRectF(32, 56, 176, 158)
    hg = QLinearGradient(head.topLeft(), head.bottomLeft())
    hg.setColorAt(0, body.lighter(112))
    hg.setColorAt(1, body.darker(108))
    p.setPen(_pen(INK, 9))
    p.setBrush(QBrush(hg))
    p.drawRoundedRect(head, 74, 70)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(_c("#ffffff", 190))
    p.drawEllipse(QRectF(58, 70, 30, 13))
    # screen
    scr = QRectF(58, 96, 124, 86)
    sg = QLinearGradient(scr.topLeft(), scr.bottomLeft())
    sg.setColorAt(0, QColor("#1c2552"))
    sg.setColorAt(1, QColor("#0c1233"))
    p.setPen(_pen(INK, 6))
    p.setBrush(QBrush(sg))
    p.drawRoundedRect(scr, 36, 36)
    glow = QColor("#8ff4ff")
    L, R = QPointF(96, 132), QPointF(144, 132)
    if mood in ("happy", "joy"):
        _arc(p, L, 26, width=8, color=glow); _arc(p, R, 26, width=8, color=glow)
    elif mood == "sleepy":
        _arc(p, L, 24, up=False, width=7, color=glow); _arc(p, R, 24, up=False, width=7, color=glow)
    elif mood == "sad":
        p.setPen(_pen(glow, 7))
        for c, s in ((L, -1), (R, 1)):                            # inner ends up = sad (down would read angry)
            p.drawLine(c + QPointF(-11, -6 * s), c + QPointF(11, 6 * s))
    else:                                                         # thinking: dot eyes looking up
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(glow)
        for c in (L, R):
            p.drawEllipse(c + QPointF(4, -6), 8, 9)
    # whiskers
    p.setPen(_pen(_c(INK, 200), 3))
    for sx in (-1, 1):
        for dy in (-6, 6):
            p.drawLine(QPointF(120 + sx * 70, 150 + dy), QPointF(120 + sx * 96, 146 + dy * 1.8))
    # ω mouth
    m = QPointF(120, 160)
    p.setPen(_pen(glow, 5))
    p.setBrush(Qt.BrushStyle.NoBrush)
    if mood == "sad":
        _arc(p, m + QPointF(0, 6), 16, up=True, width=5, color=glow)
    else:
        path = QPainterPath(m + QPointF(-12, -2))
        path.quadTo(m + QPointF(-6, 7), m + QPointF(0, -1))
        path.quadTo(m + QPointF(6, 7), m + QPointF(12, -2))
        p.drawPath(path)
    # blush on the head, under the screen
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(BLUSH)
    p.drawEllipse(QRectF(46, 176, 26, 13))
    p.drawEllipse(QRectF(168, 176, 26, 13))
    p.end()
    return img


STYLES = {"A · Visor": render_face, "B · Chibi": render_chibi, "C · Kitty-bot": render_kitty}
