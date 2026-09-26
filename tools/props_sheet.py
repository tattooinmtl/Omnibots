"""Omi with every action extra (top) and every job badge (bottom), for design review.

  python tools/props_sheet.py [out.png]
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")

from PySide6.QtCore import QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter  # noqa: E402

from omnibots.ui import theme  # noqa: E402
from omnibots.ui.omi_face import render_face  # noqa: E402
from omnibots.ui.props import ACTIONS, JOBS  # noqa: E402

ACTION_MOOD = {"coding": "working", "reading": "happy", "writing": "working", "searching": "thinking",
               "browsing": "happy", "running": "working", "thinking": "thinking", "deploying": "joy",
               "reviewing": "working", "chatting": "happy", "waiting": "thinking", "shopping": "surprised",
               "speaking": "joy", "looking": "surprised", "filming": "happy"}


def main(out: str) -> None:
    app = QGuiApplication([])  # noqa: F841
    cell, cols = 190, 8
    rows_a = (len(ACTIONS) + cols - 1) // cols
    rows_j = (len(JOBS) + cols - 1) // cols
    W, H = cols * cell, 60 + (rows_a + rows_j) * (cell + 34) + 50
    img = QImage(W, H, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(QColor("#070b18"))
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    head = QFont("Segoe UI")
    head.setPixelSize(19)
    head.setBold(True)
    small = QFont("Segoe UI")
    small.setPixelSize(13)
    p.setFont(head)
    p.setPen(QColor("#e7edfb"))
    p.drawText(QPointF(16, 34), "Action extras: what Omi is doing (animated)")
    for i, (key, (label, _)) in enumerate(ACTIONS.items()):
        x, y = (i % cols) * cell, 48 + (i // cols) * (cell + 34)
        p.drawImage(QPointF(x + 5, y), render_face(180, ACTION_MOOD.get(key, "happy"), t=1.3, prop=key, badge="boss"))
        p.setFont(small)
        p.setPen(QColor("#8b97b8"))
        p.drawText(QRectF(x, y + 180, cell, 22), Qt.AlignmentFlag.AlignCenter, label)
    y0 = 48 + rows_a * (cell + 34) + 20
    p.setFont(head)
    p.setPen(QColor("#e7edfb"))
    p.drawText(QPointF(16, y0), "Job badges: the kind of bot (top-left corner)")
    for i, (key, (title, _)) in enumerate(JOBS.items()):
        x, y = (i % cols) * cell, y0 + 14 + (i // cols) * (cell + 34)
        role_col = theme.role_color(key if key != "boss" else "boss")
        p.drawImage(QPointF(x + 5, y), render_face(180, "happy", role_col, t=0.6, badge=key))
        p.setFont(small)
        p.setPen(QColor("#8b97b8"))
        p.drawText(QRectF(x, y + 180, cell, 22), Qt.AlignmentFlag.AlignCenter, title.split(" (")[0])
    p.end()
    img.save(out)
    print("saved", out)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "omi_props.png")
