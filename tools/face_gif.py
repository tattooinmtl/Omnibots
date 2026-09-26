"""Render Omi's animation to a GIF (blinks, antenna bob, mood changes, bubble pops).

  python tools/face_gif.py [out.gif]
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")

from PIL import Image  # noqa: E402
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF  # noqa: E402
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter  # noqa: E402

from omnibots.omni.personality import load  # noqa: E402
from omnibots.ui.animator import FaceAnimator  # noqa: E402
from omnibots.ui.taunts import Taunts  # noqa: E402

FPS = 25
# (at second, event) — a little story: works, succeeds, fails, waits, gets approved
SCRIPT = [
    (0.4, ("tool", dict(name="write_file", ok=True))),
    (4.0, ("mood", "working")),
    (5.0, ("tool", dict(name="run_shell", ok=False))),
    (9.2, ("impatient", {})),
    (13.0, ("approval", dict(kind="cheer"))),
]
LENGTH = 17.0


def to_pil(img: QImage) -> Image.Image:
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return Image.open(io.BytesIO(bytes(data))).convert("RGBA")


def main(out: str) -> None:
    app = QGuiApplication([])  # noqa: F841
    taunts = Taunts(load(Path.home() / ".omni"), seed=3)
    anim = FaceAnimator(seed=5)
    frames, events, dt = [], list(SCRIPT), 1 / FPS
    for i in range(int(LENGTH * FPS)):
        now = i * dt
        while events and events[0][0] <= now:
            _, (kind, arg) = events.pop(0)
            if kind == "mood":
                anim.set_mood(arg)
            else:
                anim.say(taunts.react(kind, **arg))
        anim.advance(dt)
        face = anim.frame(180, width=460)
        bg = QImage(face.width(), face.height(), QImage.Format.Format_ARGB32_Premultiplied)
        bg.fill(QColor("#0b1226"))
        p = QPainter(bg)
        p.drawImage(QPointF(0, 0), face)
        p.end()
        frames.append(to_pil(bg).convert("P", palette=Image.Palette.ADAPTIVE, colors=128))
    frames[0].save(out, save_all=True, append_images=frames[1:], duration=int(1000 / FPS), loop=0, optimize=True)
    print("saved", out, len(frames), "frames")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "omi.gif")
