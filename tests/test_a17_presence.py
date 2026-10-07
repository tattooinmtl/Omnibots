"""A17.d presence without a voice: personalities, moods, always-on-top windows and the team's mind.

The characters store is real (a JSON file in a temp home, a fake clock for the drift); the windows are the real
BotWindow and MindView offscreen, driven through the real LiveUI with test_a11_live's fake engine."""

from __future__ import annotations

import json

import pytest
from qt_helpers import qapp

from omnibots.bots.character import PRESETS, Characters, mood_word

app = qapp()


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def test_presets_overlay_and_custom_personalities_persist(tmp_path):
    c = Characters(tmp_path)
    assert set(PRESETS) == {"default", "mentor", "hype", "calm", "pirate", "noir", "coach"}
    assert c.overlay("coder") == ""                                   # default: nothing added to the prompt
    c.set_personality("coder", "pirate")
    o = c.overlay("coder")
    assert "Pirate" in o and "style only" in o and "contents of files" in o
    key = c.add_custom("Grumpy chef", "a gruff but kind head chef", "kitchen metaphors", emoji=0, humour=2, language="French")
    c.set_personality("omi", key)
    again = Characters(tmp_path)                                      # a restart keeps both
    assert again.personality_of("coder") == "pirate" and "Chat with the user in French" in again.overlay("omi")
    assert again.choices()[key] == "Grumpy chef"
    with pytest.raises(ValueError):
        again.set_personality("omi", "nobody")


def test_moods_move_with_events_and_drift_back(tmp_path):
    clock = Clock()
    c = Characters(tmp_path, clock=clock)
    assert c.mood_text("coder")[0] == "calm" and c.mood_line("coder") == ""
    c.feel("coder", "job_passed")
    c.feel("coder", "job_passed")
    assert c.mood_text("coder") == ("proud", "happy")
    assert "proud" in c.mood_line("coder") and "never your work" in c.mood_line("coder")
    v0, _ = c.mood("coder")
    clock.t += 3600                                                   # one half-life
    assert c.mood("coder")[0] == pytest.approx(v0 / 2, rel=1e-3)
    clock.t += 6 * 3600
    assert c.mood_text("coder")[0] == "calm"
    for _ in range(3):
        c.feel("tester", "job_failed")
    assert c.mood_text("tester")[1] in ("sad", "error")
    assert mood_word(0.0, -0.6) == ("tired", "sleepy")


def test_the_prompt_carries_personality_and_mood(tmp_path):
    import asyncio
    from omnibots.bots.profile import BotRegistry
    from omnibots.bots.runner import JobRunner
    from omnibots.db import Database
    from omnibots.runtime.approvals import ApprovalCenter
    from omnibots.runtime.sandbox import Sandbox

    async def go():
        db = Database(tmp_path / "db.sqlite")
        await db.open()
        reg = BotRegistry(db, tmp_path / "bots")
        runner = JobRunner(db=db, registry=reg, router=None, approvals=ApprovalCenter(db), home=tmp_path, sandbox=Sandbox(tmp_path / "sb"))
        bot = await reg.create("Coach", "coder")
        runner.characters.set_personality(bot.id, "coach")
        runner.characters.feel(bot.id, "thanks")
        prompt = runner.system_prompt(bot)
        await db.close()
        return prompt
    prompt = asyncio.run(go())
    assert "Your personality: Coach" in prompt and "How you feel right now: cheerful" in prompt


def _live(tmp_path):
    from test_a11_live import make
    eng, live = make(tmp_path)
    eng.home = tmp_path
    from omnibots.bots.character import Characters
    eng.runner.characters = Characters(tmp_path)
    for b in eng.bots:
        b["tools"], b["skills"] = ["read_file", "web_search"], ["web-coding"]
    return eng, live


def test_keep_on_top_is_a_layout_toggle_that_is_remembered(tmp_path):
    from PySide6.QtCore import Qt
    eng, live = _live(tmp_path)
    w = live.open_bot("coder")
    assert not (w.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
    w.on_top_act.setChecked(True)
    assert w.windowFlags() & Qt.WindowType.WindowStaysOnTopHint and w.isVisible()
    assert json.loads((tmp_path / "ui_state.json").read_text())["on_top"] == ["coder"]
    w.close()
    live.windows.clear()
    w2 = live.open_bot("coder")                                       # a new window (as after a restart) floats again
    assert w2.windowFlags() & Qt.WindowType.WindowStaysOnTopHint and w2.on_top_act.isChecked()
    w2.on_top_act.setChecked(False)
    assert json.loads((tmp_path / "ui_state.json").read_text())["on_top"] == []
    w2.close()


def test_personality_menu_mood_on_the_card_and_thanks(tmp_path):
    eng, live = _live(tmp_path)
    w = live.open_bot("omi")
    w._fill_personalities()
    labels = [a.text() for a in w.personality_menu.actions() if a.text()]
    assert labels[:2] == ["Default", "Mentor"] and labels[-1] == "New personality…"
    w.personality_chosen.emit("noir")
    assert eng.runner.characters.personality_of("omi") == "noir"
    assert "Noir detective" in w.card.facts.text()
    w.chat.input.setText("thanks, great job!")
    w.chat._send()
    assert eng.runner.characters.mood_text("omi")[0] in ("cheerful", "proud", "content")
    assert "mood: cheerful" in w.card.facts.text() or "mood: proud" in w.card.facts.text()
    w.close()


def test_the_mind_draws_the_team_and_lights_up_live(tmp_path):
    from PySide6.QtGui import QImage
    eng, live = _live(tmp_path)
    live.refresh_bots()
    live.open_mind()
    m = live._mind
    assert {"bot:omi", "bot:coder", "tool:read_file", "tool:web_search", "skill:web-coding"} <= set(m.graph.nodes)
    assert ("bot:coder", "tool:web_search") in m.graph.edges
    live.on_bot_event({"bot_id": "coder", "kind": "tool", "content": json.dumps({"phase": "start", "name": "web_search", "target": "x"})})
    assert m.graph.nodes["tool:web_search"].glow == 1.0 and m.graph.edges[("bot:coder", "tool:web_search")] == 1.0
    live.on_bot_event({"bot_id": "omi", "kind": "state", "content": "thinking"})
    assert m.graph.nodes["bot:omi"].busy
    img = QImage(m.size(), QImage.Format.Format_ARGB32)
    m.render(img)
    lit = sum(1 for x in range(0, img.width(), 7) for y in range(0, img.height(), 7) if img.pixelColor(x, y).lightness() > 120)
    assert lit > 20                                                   # nodes and labels were really drawn
    yaw = m.yaw
    m._tick()
    assert m.yaw != yaw                                               # it turns by itself
    m.close()
