"""Preview Omi with thought-bubble quips (face + taunt + bubble), for design review.

  python tools/taunt_preview.py [out.png]
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

from omnibots.omni.personality import load  # noqa: E402
from omnibots.ui.bubble import bubble_size, draw_thought_bubble  # noqa: E402
from omnibots.ui.omi_face import render_face  # noqa: E402
from omnibots.ui.taunts import Taunts  # noqa: E402

SCENES = [
    ("Omi · wrote a file", "#2f7dff", ("tool", dict(name="write_file", ok=True))),
    ("coder · shell failed", "#6fe3ff", ("tool", dict(name="run_shell", ok=False))),
    ("researcher · found it", "#7c4dff", ("tool", dict(name="web_search", ok=True))),
    ("Omi · waiting on the model", "#2f7dff", ("impatient", {})),
    ("writer · rate limited", "#3ad07a", ("provider", {})),
    ("Omi · needs your click", "#2f7dff", ("approval", dict(kind="waiting"))),
]


def main(out: str) -> None:
    app = QGuiApplication([])  # noqa: F841
    taunts = Taunts(load(Path.home() / ".omni"), seed=7)
    cw, ch, cols = 440, 300, 3
    rows = (len(SCENES) + cols - 1) // cols
    img = QImage(cw * cols, ch * rows + 50, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(QColor("#070b18"))
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    f = QFont("Segoe UI")
    f.setPixelSize(16)
    p.setFont(f)
    p.setPen(QColor("#e7edfb"))
    p.drawText(QPointF(16, 32), f"Omi's quips, live from Omni ({taunts.status_verb()})")
    for i, (label, accent, (event, kw)) in enumerate(SCENES):
        x, y = (i % cols) * cw, 50 + (i // cols) * ch
        q = taunts.react(event, **kw)
        face = render_face(170, q.mood, accent, t=0.6)
        p.drawImage(QPointF(x + 14, y + 90), face)
        w, h = bubble_size(q.text, q.grawlix)
        draw_thought_bubble(p, QRectF(x + 180, y + 18, w, h), q.text, QPointF(x + 150, y + 120), grawlix=q.grawlix)
        p.setFont(f)
        p.setPen(QColor("#8b97b8"))
        p.drawText(QRectF(x, y + 262, cw, 24), Qt.AlignmentFlag.AlignCenter, f"{label}  ·  mood: {q.mood}")
    p.end()
    img.save(out)
    print("saved", out)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "omi_taunts.png")
