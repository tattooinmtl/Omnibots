"""Omi's animation (PLAN.md A11.d.02/03): blink, antenna bob, eased mood changes, and the
thought bubble popping in and out. The user picked style A ("Visor", 2026-09-26).

FaceAnimator is plain logic: advance(dt) moves time forward, frame(size) paints the
current face + bubble into a QImage. The on-screen FaceWidget drives it with a timer;
tools and tests drive it directly (no timers, deterministic with a seed).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QImage, QPainter

from omnibots.ui.bubble import bubble_size, draw_thought_bubble
from omnibots.ui.omi_face import render_face
from omnibots.ui.taunts import Quip

MOOD_FADE = 0.25          # seconds to cross-fade between moods
BLINK_TIME = 0.16
POP_IN, POP_OUT = 0.35, 0.22
GRAWLIX_TIME = 0.9        # a grumble shows its grawlix this long before the words
GRAWLIX_STEP = 0.15


def _ease_out_back(x: float) -> float:
    c1, c3 = 1.70158, 2.70158
    return 1 + c3 * (x - 1) ** 3 + c1 * (x - 1) ** 2


@dataclass
class _Bubble:
    quip: Quip
    age: float = 0.0
    hold: float = 3.0

    @property
    def total(self) -> float:
        return POP_IN + self.hold + POP_OUT


class FaceAnimator:
    def __init__(self, accent: str = "#2f7dff", mood: str = "happy", seed: int | None = None,
                 badge: str | None = None):
        self.accent = accent
        self.mood = mood
        self.badge = badge            # the job badge (props.JOBS), top-left corner
        self.prop: str | None = None  # the current action extra (props.ACTIONS)
        self._prev_mood: str | None = None
        self._fade = 1.0
        self.t = 0.0
        self.rng = random.Random(seed)
        self._next_blink = self.rng.uniform(2.0, 4.5)
        self._blink_age = -1.0
        self.bubble: _Bubble | None = None
        self.grawlix_cycle = ["@#%$", "@$#%", "^%&$", "$%#%"]
        self.look = (0.0, 0.0)
        self.reactions: list[tuple[str, float]] = []      # (emoji, when), newest last, at most 3

    # ── inputs ─────────────────────────────────────────────────────────────
    def set_action(self, action: str | None) -> None:
        """The extra Omi holds for what he's doing now (keyboard, book, pen...); None clears it."""
        self.prop = action

    def set_mood(self, mood: str) -> None:
        if mood != self.mood:
            self._prev_mood, self.mood, self._fade = self.mood, mood, 0.0

    def react(self, emoji: str) -> None:
        """Pop a reaction emoji next to the face (👍 👀 😮 …); keeps the last three."""
        self.reactions = (self.reactions + [(emoji, self.t)])[-3:]

    def say(self, quip: Quip) -> None:
        if quip.emoji:
            self.react(quip.emoji)
        words = len(quip.text.split())
        hold = min(6.0, 1.6 + 0.28 * words) + (GRAWLIX_TIME if quip.grawlix else 0)
        self.bubble = _Bubble(quip, hold=hold)
        self.set_mood(quip.mood)

    # ── time ───────────────────────────────────────────────────────────────
    def advance(self, dt: float) -> None:
        self.t += dt
        self._fade = min(1.0, self._fade + dt / MOOD_FADE)
        if self._blink_age >= 0:
            self._blink_age += dt
            if self._blink_age > BLINK_TIME:
                self._blink_age = -1.0
                self._next_blink = self.t + self.rng.uniform(2.2, 5.5)
        elif self.t >= self._next_blink and self.mood not in ("joy", "sleepy", "love", "paused", "error"):
            self._blink_age = 0.0
        if self.bubble:
            self.bubble.age += dt
            if self.bubble.age >= self.bubble.total:
                self.bubble = None
        # idle gaze drift
        self.look = (0.6 * math.sin(self.t * 0.7), 0.3 * math.sin(self.t * 1.1 + 1))

    @property
    def blink(self) -> float:
        if self._blink_age < 0:
            return 0.0
        x = self._blink_age / BLINK_TIME
        return math.sin(math.pi * min(1.0, x))              # 0 -> 1 (closed) -> 0

    def bubble_scale(self) -> tuple[float, float]:
        """(sx, sy): pop in with squash-and-stretch, a gentle bob, pop out."""
        b = self.bubble
        if not b:
            return (0.0, 0.0)
        if b.age < POP_IN:
            x = b.age / POP_IN
            s = _ease_out_back(x)
            squash = math.sin(math.pi * x) * 0.12
            return (s * (1 + squash), s * (1 - squash))
        if b.age > POP_IN + b.hold:
            x = min(1.0, (b.age - POP_IN - b.hold) / POP_OUT)
            s = 1 - x * x
            return (s, s)
        bob = 1 + 0.015 * math.sin(self.t * 4)
        return (bob, 2 - bob)

    def bubble_text(self) -> tuple[str, str | None]:
        """(words, grawlix): a grumble cycles its grawlix first, then shows the words under it."""
        b = self.bubble
        if not b:
            return ("", None)
        if b.quip.grawlix:
            age = b.age - POP_IN
            if age < GRAWLIX_TIME:
                g = self.grawlix_cycle[int(max(0.0, b.age) / GRAWLIX_STEP) % len(self.grawlix_cycle)]
                return ("", g)
            return (b.quip.text, b.quip.grawlix)
        return (b.quip.text, None)

    # ── painting ───────────────────────────────────────────────────────────
    def paint_face(self, p: QPainter, rect: QRectF) -> None:
        size = int(min(rect.width(), rect.height()))
        cur = render_face(size, self.mood, self.accent, blink=self.blink, look=self.look, t=self.t,
                          prop=self.prop, badge=self.badge)
        if self._prev_mood and self._fade < 1.0:
            old = render_face(size, self._prev_mood, self.accent, look=self.look, t=self.t,
                              prop=self.prop, badge=self.badge)
            p.setOpacity(1 - self._fade)
            p.drawImage(rect.topLeft(), old)
            p.setOpacity(self._fade)
        p.drawImage(rect.topLeft(), cur)
        p.setOpacity(1.0)

    def paint_bubble(self, p: QPainter, anchor: QPointF, head: QPointF, max_w: float = 230) -> None:
        """anchor = the bubble's bottom-left corner; head = where the thought puffs point."""
        sx, sy = self.bubble_scale()
        if sx <= 0.01:
            return
        text, grawlix = self.bubble_text()
        w, h = bubble_size(self.bubble.quip.text, self.bubble.quip.grawlix, width=max_w)
        rect = QRectF(anchor.x(), anchor.y() - h, w, h)
        p.save()
        p.translate(anchor)                                   # grow from the corner nearest the face
        p.scale(sx, sy)
        p.translate(-anchor)
        draw_thought_bubble(p, rect, text, head, grawlix=grawlix)
        p.restore()

    def frame(self, size: int, *, width: int | None = None) -> QImage:
        """One frame: the face at the bottom-left, the bubble up and to the right."""
        width = width or int(size * 2.3)
        height = int(size * 1.45)
        img = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        face = QRectF(0, height - size, size, size)
        self.paint_face(p, face)
        # bubble up and to the right; the three thought puffs trail down to the top-right of the head
        self.paint_bubble(p, QPointF(size * 1.02, height - size * 0.86), QPointF(size * 0.80, height - size * 0.70))
        p.end()
        return img
