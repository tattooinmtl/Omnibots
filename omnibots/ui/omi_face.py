"""Omi's face (PLAN.md A11.d): a cute, cartoonish robot, drawn in code.

The user asked for a cute cartoon robot (2026-09-26), not Omni's Buddy LCD
demo. Drawn with QPainter, no image files, so every mood can animate:
  - a round, soft-white head with a thick navy cartoon outline and a gloss spot
  - a dark "screen" face where the eyes and mouth glow
  - big glossy eyes with sparkle highlights, pink blush cheeks
  - a glowing antenna bulb and ear pods in the bot's accent (role) color

render_face(size, mood, accent, blink=0.0, look=(0,0), t=0.0) -> QImage
  mood:  happy | joy | working | thinking | surprised | sleepy | sad | wink | love | paused | error
  blink: 0 (open) .. 1 (closed), for the idle blink animation
  look:  (-1..1, -1..1) where the pupils point
  t:     seconds, drives the antenna bob and glow pulse
All geometry lives in a 240-unit box, scaled to `size`.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush, QColor, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QRadialGradient,
)

MOODS = ("happy", "joy", "working", "thinking", "surprised", "sleepy", "sad", "wink", "love", "paused", "error")

OUTLINE = QColor("#141c3a")
SCREEN_TOP = QColor("#17224a")
SCREEN_BOTTOM = QColor("#0a1030")
EYE = QColor("#7df0ff")
EYE_CORE = QColor("#e9fdff")
BLUSH = QColor(255, 120, 170, 110)
HEAD_TOP = QColor("#ffffff")
HEAD_BOTTOM = QColor("#c9d6f5")


def _c(color: str | QColor, alpha: int) -> QColor:
    c = QColor(color)
    c.setAlpha(alpha)
    return c


def render_face(size: int, mood: str = "happy", accent: str = "#2f7dff", *, blink: float = 0.0,
                look: tuple[float, float] = (0.0, 0.0), t: float = 0.0, ring: bool = True,
                prop: str | None = None, badge: str | None = None) -> QImage:
    """prop: the action extra (props.ACTIONS, e.g. "coding" = keyboard); badge: the job badge
    (props.JOBS, e.g. "coder"), drawn in the top-left corner, never over the face."""
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(size / 240.0, size / 240.0)
    acc = QColor(accent)
    small = size <= 40
    bob = math.sin(t * 2.2) * 2.5                      # antenna bob
    pulse = 0.5 + 0.5 * math.sin(t * 3.0)              # glow pulse 0..1

    # ── glow ring (the ID-card halo) ─────────────────────────────────────
    if ring and not small:
        halo = QRadialGradient(QPointF(120, 128), 118)
        halo.setColorAt(0.62, _c(acc, 0))
        halo.setColorAt(0.82, _c(acc, int(70 + 50 * pulse)))
        halo.setColorAt(1.00, _c(acc, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(halo))
        p.drawEllipse(QRectF(2, 10, 236, 236))

    # ── antenna ──────────────────────────────────────────────────────────
    stem = QPen(OUTLINE, 7)
    stem.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(stem)
    top = QPointF(120 + bob * 0.4, 30 + bob)
    p.drawLine(QPointF(120, 58), top)
    bulb_glow = QRadialGradient(top, 26)
    bulb_glow.setColorAt(0.0, _c(acc, int(150 + 80 * pulse)))
    bulb_glow.setColorAt(1.0, _c(acc, 0))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(bulb_glow))
    p.drawEllipse(top, 26, 26)
    p.setPen(QPen(OUTLINE, 5))
    bulb = QRadialGradient(top + QPointF(-3, -3), 12)
    bulb.setColorAt(0.0, QColor("white"))
    bulb.setColorAt(0.5, acc.lighter(130))
    bulb.setColorAt(1.0, acc)
    p.setBrush(QBrush(bulb))
    p.drawEllipse(top, 11, 11)

    # ── ear pods ─────────────────────────────────────────────────────────
    p.setPen(QPen(OUTLINE, 6))
    for x in (22, 190):
        pod = QLinearGradient(QPointF(x, 110), QPointF(x + 28, 150))
        pod.setColorAt(0.0, acc.lighter(140))
        pod.setColorAt(1.0, acc.darker(120))
        p.setBrush(QBrush(pod))
        p.drawRoundedRect(QRectF(x, 106, 28, 50), 12, 12)

    # ── head ─────────────────────────────────────────────────────────────
    head = QRectF(38, 58, 164, 150)
    grad = QLinearGradient(head.topLeft(), head.bottomLeft())
    grad.setColorAt(0.0, HEAD_TOP)
    grad.setColorAt(1.0, HEAD_BOTTOM)
    p.setPen(QPen(OUTLINE, 8))
    p.setBrush(QBrush(grad))
    p.drawRoundedRect(head, 62, 62)
    if not small:                                       # gloss spot, top-left
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_c("#ffffff", 200))
        p.drawEllipse(QRectF(64, 72, 30, 14))
        p.drawEllipse(QRectF(56, 90, 9, 9))

    # ── screen (the face) ────────────────────────────────────────────────
    screen = QRectF(58, 88, 124, 96)
    sg = QLinearGradient(screen.topLeft(), screen.bottomLeft())
    sg.setColorAt(0.0, SCREEN_TOP)
    sg.setColorAt(1.0, SCREEN_BOTTOM)
    p.setPen(QPen(OUTLINE, 5))
    p.setBrush(QBrush(sg))
    p.drawRoundedRect(screen, 40, 40)
    if not small:                                       # screen reflection
        refl = QPainterPath()
        refl.addRoundedRect(QRectF(70, 94, 60, 12), 6, 6)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_c("#ffffff", 22))
        p.drawPath(refl)

    # ── cheeks ───────────────────────────────────────────────────────────
    if mood not in ("paused", "error") and not small:
        p.setPen(Qt.PenStyle.NoPen)
        blush = QColor(BLUSH)
        if mood in ("joy", "love", "wink"):
            blush.setAlpha(160)
        p.setBrush(blush)
        p.drawEllipse(QRectF(64, 150, 22, 12))
        p.drawEllipse(QRectF(154, 150, 22, 12))

    # ── eyes + mouth ─────────────────────────────────────────────────────
    lx, rx, ey = 96.0, 144.0, 128.0
    dx, dy = look[0] * 5, look[1] * 4
    _eyes(p, mood, QPointF(lx + dx, ey + dy), QPointF(rx + dx, ey + dy), blink, small)
    _mouth(p, mood, QPointF(120 + dx * 0.5, 162), small)

    # ── extras (the mood's corner extras step aside when an action prop is showing) ──
    if not small and (prop is None or mood == "sad"):
        if mood == "thinking":
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_c(EYE, 200))
            for i, r in enumerate((4, 6, 8)):
                p.drawEllipse(QPointF(196 + i * 11, 70 - i * 12), r, r)
        elif mood == "sleepy":
            _text(p, "z", QPointF(186, 64), 22)
            _text(p, "Z", QPointF(202, 44), 30)
        elif mood == "sad":
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_c("#7fd8ff", 220))
            tear = QPainterPath(QPointF(rx + 10, ey + 12))
            tear.quadTo(rx + 20, ey + 30, rx + 10, ey + 34)
            tear.quadTo(rx, ey + 30, rx + 10, ey + 12)
            p.drawPath(tear)
        elif mood == "working":
            p.setPen(QPen(_c(acc, 230), 4))               # little "busy" sparks
            for a in (-40, -10, 20):
                r = math.radians(a - 90)
                c = QPointF(200, 64)
                p.drawLine(c + QPointF(math.cos(r) * 8, math.sin(r) * 8), c + QPointF(math.cos(r) * 16, math.sin(r) * 16))
    if prop and size > 40:
        from omnibots.ui.props import draw_prop
        draw_prop(p, prop, t, accent)
    if badge and size > 28:
        from omnibots.ui.props import draw_badge
        draw_badge(p, badge, accent)
    p.end()
    return img


def _glow(p: QPainter, c: QPointF, r: float) -> None:
    g = QRadialGradient(c, r)
    g.setColorAt(0.0, _c(EYE, 150))
    g.setColorAt(1.0, _c(EYE, 0))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(g))
    p.drawEllipse(c, r, r)


def _oval_eye(p: QPainter, c: QPointF, w: float, h: float, small: bool, sparkle: bool = True) -> None:
    if not small:
        _glow(p, c, max(w, h) * 1.1)
    eg = QRadialGradient(c + QPointF(0, -h * 0.2), h)
    eg.setColorAt(0.0, EYE_CORE)
    eg.setColorAt(0.6, EYE)
    eg.setColorAt(1.0, EYE.darker(120))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(eg))
    p.drawEllipse(c, w / 2, h / 2)
    if sparkle and not small:
        p.setBrush(QColor("white"))
        p.drawEllipse(c + QPointF(-w * 0.18, -h * 0.2), w * 0.16, w * 0.16)
        p.drawEllipse(c + QPointF(w * 0.16, h * 0.14), w * 0.07, w * 0.07)


def _arc_eye(p: QPainter, c: QPointF, w: float, up: bool = True, width: float = 7) -> None:
    pen = QPen(EYE, width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    path = QPainterPath(QPointF(c.x() - w / 2, c.y() + (4 if up else -4)))
    path.quadTo(c.x(), c.y() + (-14 if up else 14), c.x() + w / 2, c.y() + (4 if up else -4))
    p.drawPath(path)


def _heart(p: QPainter, c: QPointF, s: float) -> None:
    _glow(p, c, s * 1.1)
    path = QPainterPath(QPointF(c.x(), c.y() + s * 0.55))
    path.cubicTo(c.x() - s * 1.1, c.y() - s * 0.1, c.x() - s * 0.5, c.y() - s * 0.9, c.x(), c.y() - s * 0.3)
    path.cubicTo(c.x() + s * 0.5, c.y() - s * 0.9, c.x() + s * 1.1, c.y() - s * 0.1, c.x(), c.y() + s * 0.55)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#ff7eb6"))
    p.drawPath(path)
    p.setBrush(QColor("white"))
    p.drawEllipse(c + QPointF(-s * 0.35, -s * 0.3), s * 0.13, s * 0.13)


def _eyes(p: QPainter, mood: str, l: QPointF, r: QPointF, blink: float, small: bool) -> None:
    w, h = 26.0, 32.0
    if mood == "joy":
        _arc_eye(p, l, 26); _arc_eye(p, r, 26); return
    if mood == "sleepy":
        _arc_eye(p, l, 24, up=False, width=6); _arc_eye(p, r, 24, up=False, width=6); return
    if mood == "love":
        _heart(p, l, 15); _heart(p, r, 15); return
    if mood == "wink":
        _oval_eye(p, l, w, h * (1 - blink * 0.9), small); _arc_eye(p, r, 24); return
    if mood == "paused":
        pen = QPen(EYE, 7); pen.setCapStyle(Qt.PenCapStyle.RoundCap); p.setPen(pen)
        for c in (l, r):
            p.drawLine(c + QPointF(-11, 0), c + QPointF(11, 0))
        return
    if mood == "error":
        pen = QPen(QColor("#ff6b8b"), 7); pen.setCapStyle(Qt.PenCapStyle.RoundCap); p.setPen(pen)
        for c in (l, r):
            p.drawLine(c + QPointF(-9, -9), c + QPointF(9, 9)); p.drawLine(c + QPointF(-9, 9), c + QPointF(9, -9))
        return
    if mood == "surprised":
        w, h = 30.0, 36.0
    elif mood == "working":
        h = 22.0                                         # focused, slightly narrowed
    elif mood == "sad":
        h = 26.0
    elif mood == "thinking":
        l, r = l + QPointF(4, -5), r + QPointF(4, -5)    # looking up and away
    h *= max(0.08, 1 - blink)
    if h < 6:                                            # mid-blink: a line
        pen = QPen(EYE, 6); pen.setCapStyle(Qt.PenCapStyle.RoundCap); p.setPen(pen)
        for c in (l, r):
            p.drawLine(c + QPointF(-w / 2, 0), c + QPointF(w / 2, 0))
        return
    _oval_eye(p, l, w, h, small)
    _oval_eye(p, r, w, h, small)
    if mood == "sad":                                    # droopy brows
        pen = QPen(EYE, 4); pen.setCapStyle(Qt.PenCapStyle.RoundCap); p.setPen(pen)
        p.drawLine(l + QPointF(-12, -22), l + QPointF(10, -26))
        p.drawLine(r + QPointF(-10, -26), r + QPointF(12, -22))


def _mouth(p: QPainter, mood: str, c: QPointF, small: bool) -> None:
    pen = QPen(EYE, 5 if not small else 8)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    if mood in ("joy", "love", "wink"):                  # open happy mouth
        path = QPainterPath(QPointF(c.x() - 14, c.y() - 4))
        path.quadTo(c.x(), c.y() + 18, c.x() + 14, c.y() - 4)
        path.closeSubpath()
        p.setBrush(QColor("#ff7eb6") if mood != "joy" else _c(EYE, 90))
        p.drawPath(path)
        if mood == "wink":                               # tongue
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#ff9ec4"))
            p.drawEllipse(c + QPointF(4, 6), 5, 4)
    elif mood == "surprised":
        p.drawEllipse(c, 7, 8)
    elif mood in ("sad", "error"):
        path = QPainterPath(QPointF(c.x() - 11, c.y() + 5))
        path.quadTo(c.x(), c.y() - 6, c.x() + 11, c.y() + 5)
        p.drawPath(path)
    elif mood in ("working", "paused", "thinking"):
        p.drawLine(c + QPointF(-8, 1), c + QPointF(8 if mood != "thinking" else 4, 1 if mood != "thinking" else -2))
    elif mood == "sleepy":
        p.drawEllipse(c + QPointF(0, 2), 4, 3)
    else:                                                # happy: little smile
        path = QPainterPath(QPointF(c.x() - 12, c.y() - 2))
        path.quadTo(c.x(), c.y() + 10, c.x() + 12, c.y() - 2)
        p.drawPath(path)


def _text(p: QPainter, s: str, at: QPointF, px: int) -> None:
    from PySide6.QtGui import QFont
    f = QFont("Segoe UI")
    f.setBold(True)
    f.setPixelSize(px)
    p.setFont(f)
    p.setPen(_c(EYE, 220))
    p.drawText(at, s)
