"""A11.d.02/03: Omi's animation logic (deterministic: driven by advance(dt), seeded)."""

from __future__ import annotations

import os

from qt_helpers import qapp  # noqa: E402

from PySide6.QtGui import QGuiApplication  # noqa: E402

from omnibots.ui.animator import BLINK_TIME, GRAWLIX_TIME, MOOD_FADE, POP_IN, POP_OUT, FaceAnimator  # noqa: E402
from omnibots.ui.taunts import Quip  # noqa: E402

app = qapp()


def run_for(a: FaceAnimator, seconds: float, dt: float = 0.02):
    for _ in range(int(seconds / dt)):
        a.advance(dt)


def test_blinks_happen_and_are_short():
    a = FaceAnimator(seed=1)
    closed, samples = 0, 0
    for _ in range(int(12 / 0.01)):
        a.advance(0.01)
        samples += 1
        closed += a.blink > 0.5
    assert 0 < closed < samples * 0.1                      # blinks, but eyes are open >90% of the time
    b = FaceAnimator(mood="joy", seed=1)                   # arched happy eyes don't blink
    run_for(b, 12)
    assert b.blink == 0


def test_mood_cross_fades():
    a = FaceAnimator(mood="happy")
    a.set_mood("sad")
    assert a._fade == 0.0 and a._prev_mood == "happy"
    run_for(a, MOOD_FADE + 0.05)
    assert a._fade == 1.0


def test_bubble_pops_in_with_overshoot_holds_and_pops_out():
    a = FaceAnimator()
    a.say(Quip("shipped it.", "joy"))
    assert a.mood == "joy"
    peak = 0.0
    for _ in range(int(POP_IN / 0.01)):
        a.advance(0.01)
        peak = max(peak, max(a.bubble_scale()))
    assert peak > 1.02                                     # springy overshoot
    run_for(a, 1.0)
    sx, sy = a.bubble_scale()
    assert 0.97 < sx < 1.03 and 0.97 < sy < 1.03           # holding, gentle bob
    run_for(a, a.bubble.total)                             # past hold + pop-out
    assert a.bubble is None and a.bubble_scale() == (0.0, 0.0)


def test_grumble_shows_the_grawlix_then_the_words():
    a = FaceAnimator()
    a.say(Quip("well, THAT crashed.", "sad", grawlix="@#%$"))
    run_for(a, POP_IN + 0.1)
    words, g = a.bubble_text()
    assert words == "" and g in a.grawlix_cycle
    seen = set()
    for _ in range(int(GRAWLIX_TIME / 0.02)):
        a.advance(0.02)
        if a.bubble_text()[0] == "":
            seen.add(a.bubble_text()[1])
    assert len(seen) >= 3                                  # the symbols cycle
    run_for(a, 0.3)
    assert a.bubble_text() == ("well, THAT crashed.", "@#%$")


def test_frames_render():
    a = FaceAnimator()
    a.say(Quip("any day now.", "sleepy"))
    run_for(a, POP_IN + POP_OUT)
    img = a.frame(120)
    assert not img.isNull() and img.width() > 120
    assert BLINK_TIME < 0.3
