"""Regenerate Omi's icon files from the code-drawn icon (omnibots/ui/omi_icon.py).

  python tools/build_icons.py

Writes omnibots/ui/assets/omi.ico (all sizes), omi_256.png, and
omi_preview.png (every state at tray and large sizes, for review).
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from omnibots.ui.omi_icon import STATE_LOOKS, render_omi, write_ico  # noqa: E402

ASSETS = ROOT / "omnibots" / "ui" / "assets"


def preview(path: Path) -> None:
    states = list(STATE_LOOKS)
    sizes = [16, 24, 32, 64, 128]
    col_w, row_h, left, top = 150, 150, 110, 40
    img = QImage(left + col_w * len(sizes), top + row_h * len(states) + 30, QImage.Format.Format_ARGB32)
    img.fill(QColor("#0b1328"))
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    font = QFont("Segoe UI")
    font.setPixelSize(14)
    p.setFont(font)
    p.setPen(QColor("#cfe0ff"))
    for j, sz in enumerate(sizes):
        p.drawText(QRectF(left + j * col_w, 8, col_w, 24), Qt.AlignmentFlag.AlignCenter, f"{sz}px")
    for i, st in enumerate(states):
        y = top + i * row_h
        p.drawText(QRectF(8, y, left - 12, row_h), Qt.AlignmentFlag.AlignVCenter, st)
        for j, sz in enumerate(sizes):
            x = left + j * col_w + (col_w - sz) / 2
            p.drawImage(int(x), int(y + (row_h - sz) / 2), render_omi(sz, st, badge=2 if st == "approval" else None))
    p.end()
    img.save(str(path))


def main() -> None:
    QApplication.instance() or QApplication([])
    ASSETS.mkdir(parents=True, exist_ok=True)
    write_ico(ASSETS / "omi.ico")
    render_omi(256).save(str(ASSETS / "omi_256.png"))
    preview(ASSETS / "omi_preview.png")
    print(f"wrote {ASSETS / 'omi.ico'}, omi_256.png, omi_preview.png")


if __name__ == "__main__":
    main()
