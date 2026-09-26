"""Omi's icon: the app, window, taskbar and tray icon (PLAN.md A11.b.01).

Omi's shape comes from Omni's Buddy face (~/.omni/buddy/Buddy/src/face.js):
a rounded-square LCD face (200 units, corner radius 38, in a 240 viewBox),
two rounded-square eyes centred at (88,110) and (152,110), mouth at y=168.
Redrawn with the LayoutPlan look: a glossy navy visor, glowing cyan eyes and a
blue glow ring. Drawn in code (no image files), so the tray can show Omi's
live state: eye expression, ring color and an approvals badge.

Small sizes (<= 32 px) drop the ring and gloss and thicken shapes so Omi stays
readable in the tray.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush, QColor, QFont, QIcon, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap,
    QRadialGradient,
)

ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


@dataclass(frozen=True)
class Look:
    ring: str        # glow ring / accent
    eye: str         # eye glow color


# Ring color = the team's overall state, at a glance in the tray.
STATE_LOOKS = {
    "idle":     Look("#2f7dff", "#6fe3ff"),
    "working":  Look("#2f7dff", "#6fe3ff"),
    "thinking": Look("#7c4dff", "#b99cff"),
    "paused":   Look("#f5a524", "#ffd98a"),
    "stopped":  Look("#5b6478", "#9aa3b8"),
    "approval": Look("#ff4d6d", "#ffb3c1"),
    "error":    Look("#ff4d6d", "#ffb3c1"),
}

# Eye expression per state (a subset of Omi's 17; the full set is A11.d.02).
EYES = {
    "idle": "square", "working": "square", "thinking": "up", "paused": "line",
    "stopped": "closed", "approval": "wide", "error": "x",
}


def _c(hex_: str, alpha: int = 255) -> QColor:
    c = QColor(hex_)
    c.setAlpha(alpha)
    return c


def render_omi(size: int, state: str = "idle", badge: int | None = None) -> QImage:
    look = STATE_LOOKS.get(state, STATE_LOOKS["idle"])
    small = size <= 32
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = size / 240.0                      # Omi's 240-unit viewBox -> pixels
    p.scale(s, s)

    # Glow ring (the LayoutPlan halo), large sizes only.
    if not small:
        halo = QRadialGradient(QPointF(120, 120), 120)
        halo.setColorAt(0.70, _c(look.ring, 0))
        halo.setColorAt(0.86, _c(look.ring, 150))
        halo.setColorAt(1.00, _c(look.ring, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(halo))
        p.drawEllipse(QRectF(0, 0, 240, 240))
        ring = QPen(_c(look.ring, 230), 7)
        ring.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(ring)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(QRectF(8, 8, 224, 224), 40 * 16, 250 * 16)      # open arc, like the mock

    # Face: Omi's rounded square, as a glossy navy visor.
    inset = 14 if small else 34
    face = QRectF(inset, inset, 240 - 2 * inset, 240 - 2 * inset)
    radius = 38 * face.width() / 200
    body = QLinearGradient(face.topLeft(), face.bottomLeft())
    body.setColorAt(0.0, QColor("#1d2a52"))
    body.setColorAt(1.0, QColor("#0a1024"))
    p.setPen(QPen(_c(look.ring, 255 if small else 200), 14 if small else 5))
    p.setBrush(QBrush(body))
    p.drawRoundedRect(face, radius, radius)

    if not small:                                                  # gloss highlight
        gloss = QPainterPath()
        g = face.adjusted(10, 8, -10, -face.height() * 0.55)
        gloss.addRoundedRect(g, radius * 0.8, radius * 0.8)
        grad = QLinearGradient(g.topLeft(), g.bottomLeft())
        grad.setColorAt(0.0, _c("#ffffff", 46))
        grad.setColorAt(1.0, _c("#ffffff", 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(grad))
        p.drawPath(gloss)

    # Eyes, mapped into the face (Omi: 40-unit eyes at (88,110) / (152,110)).
    k = face.width() / 200
    ox, oy = face.left() - 20 * k, face.top() - 20 * k
    eye_size = (48 if small else 40) * k
    centres = [QPointF(ox + 88 * k, oy + 110 * k), QPointF(ox + 152 * k, oy + 110 * k)]
    _draw_eyes(p, centres, eye_size, EYES.get(state, "square"), look, small)

    # Mouth: Omi's small friendly smile (dropped at 16 px where it turns to mush).
    if size > 16:
        mouth = QPainterPath()
        mx, my = ox + 120 * k, oy + 166 * k
        w = (46 if small else 40) * k
        mouth.moveTo(mx - w / 2, my)
        mouth.quadTo(mx, my + 16 * k, mx + w / 2, my)
        pen = QPen(_c(look.eye), (12 if small else 7) * k)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(mouth)

    if badge:                                                      # e.g. pending approvals
        r = 58
        p.setPen(QPen(QColor("#0a1024"), 8))
        p.setBrush(QColor("#ff4d6d"))
        p.drawEllipse(QRectF(240 - 2 * r, 0, 2 * r, 2 * r))
        f = QFont("Segoe UI")
        f.setBold(True)
        f.setPixelSize(int(r * 1.25))
        p.setFont(f)
        p.setPen(QColor("white"))
        p.drawText(QRectF(240 - 2 * r, 0, 2 * r, 2 * r), Qt.AlignmentFlag.AlignCenter, str(badge) if badge < 10 else "9+")
    p.end()
    return img


def _draw_eyes(p: QPainter, centres, size: float, shape: str, look: Look, small: bool) -> None:
    eye = _c(look.eye)
    for c in centres:
        if not small:                                              # soft glow behind each eye
            glow = QRadialGradient(c, size * 1.1)
            glow.setColorAt(0.0, _c(look.eye, 120))
            glow.setColorAt(1.0, _c(look.eye, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(glow))
            p.drawEllipse(c, size * 1.1, size * 1.1)
        pen = QPen(eye, size * 0.22)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        if shape in ("square", "wide", "up"):
            h = size * (1.2 if shape == "wide" else 1.0)
            dy = -size * 0.15 if shape == "up" else 0
            r = QRectF(c.x() - size / 2, c.y() - h / 2 + dy, size, h)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(eye)
            p.drawRoundedRect(r, size / 4.4, size / 4.4)
        elif shape in ("line", "closed"):
            p.setPen(pen)
            if shape == "closed":                                  # sleepy arc
                path = QPainterPath(QPointF(c.x() - size / 2, c.y()))
                path.quadTo(c.x(), c.y() + size * 0.45, c.x() + size / 2, c.y())
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(path)
            else:
                p.drawLine(QPointF(c.x() - size / 2, c.y()), QPointF(c.x() + size / 2, c.y()))
        elif shape == "x":
            p.setPen(pen)
            d = size / 2.4
            p.drawLine(QPointF(c.x() - d, c.y() - d), QPointF(c.x() + d, c.y() + d))
            p.drawLine(QPointF(c.x() - d, c.y() + d), QPointF(c.x() + d, c.y() - d))


def omi_icon(state: str = "idle", badge: int | None = None) -> QIcon:
    icon = QIcon()
    for sz in ICON_SIZES:
        icon.addPixmap(QPixmap.fromImage(render_omi(sz, state, badge)))
    return icon


def _png_bytes(img: QImage) -> bytes:
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return bytes(data)


def write_ico(path, state: str = "idle", sizes=(16, 20, 24, 32, 40, 48, 64, 128, 256)) -> None:
    """Multi-size .ico with PNG-compressed entries (Windows Vista+ format)."""
    blobs = [(_png_bytes(render_omi(sz, state)), sz) for sz in sizes]
    header = struct.pack("<HHH", 0, 1, len(blobs))
    offset = 6 + 16 * len(blobs)
    entries, payload = b"", b""
    for data, sz in blobs:
        dim = 0 if sz >= 256 else sz
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        payload += data
        offset += len(data)
    with open(path, "wb") as f:
        f.write(header + entries + payload)
