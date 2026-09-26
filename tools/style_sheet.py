"""Compare Omi's candidate styles (rows) across a few moods (columns).

  python tools/style_sheet.py [out.png]
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

from omnibots.ui.omi_styles import STYLES  # noqa: E402

MOODS = ["happy", "joy", "thinking", "sad", "sleepy"]
ACCENTS = ["#2f7dff", "#2f7dff", "#7c4dff", "#6fe3ff", "#3ad07a"]


def main(out: str) -> None:
    app = QGuiApplication([])  # noqa: F841
    cell, label_w = 200, 170
    W, H = label_w + cell * len(MOODS), 60 + len(STYLES) * (cell + 24)
    img = QImage(W, H, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(QColor("#070b18"))
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    f = QFont("Segoe UI")
    f.setPixelSize(15)
    p.setFont(f)
    p.setPen(QColor("#8b97b8"))
    for j, m in enumerate(MOODS):
        p.drawText(QRectF(label_w + j * cell, 20, cell, 24), Qt.AlignmentFlag.AlignCenter, m)
    big = QFont("Segoe UI")
    big.setPixelSize(22)
    big.setBold(True)
    for i, (name, fn) in enumerate(STYLES.items()):
        y = 52 + i * (cell + 24)
        p.setFont(big)
        p.setPen(QColor("#e7edfb"))
        p.drawText(QRectF(16, y, label_w - 16, cell), Qt.AlignmentFlag.AlignVCenter, name)
        for j, (m, acc) in enumerate(zip(MOODS, ACCENTS)):
            p.drawImage(QPointF(label_w + j * cell + 5, y), fn(190, m, acc, t=0.6))
    p.end()
    img.save(out)
    print("saved", out)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "omi_styles.png")
