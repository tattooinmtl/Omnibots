"""GoodLayoutNew.png (user, 2026-09-26): reaction emoji next to Omi's face, and live
"● Name – what it's doing" lines at the top of the message board."""

from __future__ import annotations

from qt_helpers import qapp

from omnibots.omni.personality import load
from omnibots.ui.animator import FaceAnimator
from omnibots.ui.taunts import Quip, Taunts
from omnibots.ui.widgets import ActivityList, BoardPanel

app = qapp()


def test_quips_carry_reaction_emoji():
    t = Taunts(load(None), seed=2)
    assert t.react("tool", name="write_file", ok=True).emoji == "👍"
    assert t.react("tool", name="read_file", ok=True).emoji == "👀"
    assert t.react("tool", name="run_shell", ok=False).emoji == "😮"
    assert t.react("thinking", ok=False).emoji == "🤔"
    assert t.react("impatient").emoji == "💤"
    assert t.react("approval", kind="waiting").emoji == "⏳"
    assert t.react("claim", kind="cheer").emoji == "✅"


def test_the_face_keeps_the_last_three_reactions():
    a = FaceAnimator()
    for e in ("👍", "👀", "😮", "✅"):
        a.say(Quip("x", "happy", emoji=e))
        a.advance(0.5)
    assert [e for e, _ in a.reactions] == ["👀", "😮", "✅"]


def test_board_activity_lines():
    b = BoardPanel("omi")
    b.show()
    assert not b.activity.isVisible()                         # hidden until a bot is active
    b.set_activity("omi", "Omi", "boss", "working", "reviewing a claim")
    b.set_activity("coder", "Coder", "coder", "working", "coding site/index.html")
    b.set_activity("omi", "Omi", "boss", "waiting_approval", "waiting for your click")   # updates in place
    assert b.activity.isVisible() and list(b.activity.rows) == ["omi", "coder"]
    assert "waiting for your click" in b.activity.rows["omi"].text() and "reviewing" not in b.activity.rows["omi"].text()
    b.activity.remove("omi")
    b.activity.remove("coder")
    assert not b.activity.isVisible()
    assert isinstance(b.activity, ActivityList)
