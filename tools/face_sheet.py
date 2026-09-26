"""Render every Omi mood on one sheet (for reviewing the face design).

  python tools/face_sheet.py [out.png]
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

from omnibots.ui.omi_face import MOODS, render_face  # noqa: E402

ROLE_ACCENTS = [("Omi · boss", "#2f7dff"), ("coder", "#6fe3ff"), ("researcher", "#7c4dff"), ("writer", "#3ad07a")]


def main(out: str) -> None:
    app = QGuiApplication([])  # noqa: F841
    cell, cols = 220, 6
    rows_moods = (len(MOODS) + cols - 1) // cols
    W, H = cols * cell, (rows_moods + 1) * (cell + 30) + 60
    sheet = QImage(W, H, QImage.Format.Format_ARGB32_Premultiplied)
    sheet.fill(QColor("#070b18"))
    p = QPainter(sheet)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    f = QFont("Segoe UI")
    f.setPixelSize(16)
    p.setFont(f)
    p.setPen(QColor("#e7edfb"))
    p.drawText(QPointF(16, 34), "Omi: moods (top two rows) and role accents (bottom)")
    for i, mood in enumerate(MOODS):
        x, y = (i % cols) * cell, 50 + (i // cols) * (cell + 30)
        p.drawImage(QPointF(x + 10, y), render_face(200, mood, t=0.6))
        p.setPen(QColor("#8b97b8"))
        p.drawText(QRectF(x, y + 200, cell, 24), Qt.AlignmentFlag.AlignCenter, mood)
    y = 50 + rows_moods * (cell + 30)
    for i, (label, acc) in enumerate(ROLE_ACCENTS):
        x = i * cell
        p.drawImage(QPointF(x + 10, y), render_face(200, "happy", acc, t=0.6))
        p.setPen(QColor("#8b97b8"))
        p.drawText(QRectF(x, y + 200, cell, 24), Qt.AlignmentFlag.AlignCenter, label)
    x = 4 * cell + 20                                       # tray sizes
    for j, sz in enumerate((64, 32, 24, 16)):
        p.drawImage(QPointF(x + j * 80, y + 60), render_face(sz, "happy", ring=False))
    p.setPen(QColor("#8b97b8"))
    p.drawText(QRectF(x, y + 200, 2 * cell - 20, 24), Qt.AlignmentFlag.AlignCenter, "tray: 64 / 32 / 24 / 16 px")
    p.end()
    sheet.save(out)
    print("saved", out)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "omi_faces.png")
