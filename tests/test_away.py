"""A15.g how it feels: "While you were away" in Omi's chat, the "On their own today" strip with a pause
toggle, and the board saying what started a background job. Real database rows, real Qt widgets."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from mock_provider import MockProviders
from qt_helpers import qapp
from test_a7_orchestrator import build

from omnibots import away
from omnibots.engine import Engine
from omnibots.ui.live import LiveUI
from omnibots.ui.widgets import OwnStrip

app = qapp()
SINCE = "2026-01-01T00:00:00.000Z"


def run(coro):
    return asyncio.run(coro)


async def seed(e):
    db = e["db"]
    bot = await e["reg"].create("Builder", "coder", chain=["work/m"])
    pid = await e["projects"].create("a bakery site")
    for i, origin in enumerate(("watch", "watch", "fix", "continue", "user")):
        await db.write("INSERT INTO jobs (id, project_id, title, status, created_by, origin, assigned_bot_id, started_at) "
                       "VALUES (?, ?, 't', 'completed', 'omi', ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))", (f"j{i}", pid, origin, bot.id))
    await db.write("INSERT INTO provider_usage_events (provider, model, bot_id, job_id, tokens_in, tokens_out, status_code) "
                   "VALUES ('work', 'm', ?, 'j2', 30000, 8000, 200)", (bot.id,))
    await db.write("INSERT INTO provider_usage_events (provider, model, bot_id, job_id, tokens_in, tokens_out, status_code) "
                   "VALUES ('work', 'm', ?, 'j4', 99000, 0, 200)", (bot.id,))                  # yours: not counted
    await e["bus"].publish(f"#project/{pid}", "PROGRESS_UPDATE", {"text": "⚠ https://bakery.example is down (404). Repair round: started."},
                           sender_type="bot", sender_id="omi", project_id=pid)
    await e["bus"].publish(f"#project/{pid}", "PROGRESS_UPDATE", {"text": "✅ https://bakery.example is back up."},
                           sender_type="bot", sender_id="omi", project_id=pid)
    await e["bus"].publish("#general", "QUESTION", {"text": "which domain?"}, sender_type="bot", sender_id="omi", recipient_id="user")
    return pid


def test_while_you_were_away_says_what_happened_and_what_waits(tmp_path):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            empty = await away.away_summary(e["db"], SINCE)
            first_run = await away.away_summary(e["db"], None)
            await seed(e)
            text = await away.away_summary(e["db"], SINCE)
            today = await away.today(e["db"], paused=True)
            eng = SimpleNamespace(db=e["db"])
            once = await Engine.ui_away_summary(eng)           # no last_seen yet: nothing, and it starts counting now
            later = await Engine.ui_away_summary(eng)          # nothing new since: nothing again
            await e["db"].close()
            return empty, first_run, text, today, once, later
    empty, first_run, text, today, once, later = run(go())
    assert empty is None and first_run is None and once is None and later is None
    assert text.startswith("While you were away (since ")
    assert "a bakery site: 1 round carried on, 1 repair, 2 checks, ⚠ went down, ✅ back up" in text
    assert "• 38k tokens of background work" in text and "waiting for you: 1 question" in text
    assert today["totals"] == {"watch": 2, "fix": 1, "continue": 1} and today["tokens"] == 38000
    assert away.strip_text(today) == "On their own today: 1 round carried on, 1 repair, 2 checks · 38k tokens · paused"


def test_the_strip_shows_the_day_and_toggles_the_pause():
    strip = OwnStrip()
    toggled = []
    strip.pause_toggled.connect(toggled.append)
    strip.set_data({"text": "On their own today: 2 checks", "paused": False})
    strip.pause.click()
    strip.set_data({"text": "On their own today: 2 checks · paused", "paused": True})   # the engine agrees: no second signal
    assert toggled == [True] and strip.label.text().endswith("paused") and strip.pause.text() == "▶ Resume"


def test_the_board_says_what_started_a_background_job():
    shown = []
    ui = LiveUI(SimpleNamespace(home=None, omni=None))
    ui.bots = {"b1": {"id": "b1", "name": "Builder", "role": "coder"}}
    w = SimpleNamespace(board=SimpleNamespace(add=shown.append))
    ui._board_into(w, {"type": "WORK_STARTED", "sender_id": "b1", "payload": {"title": "fix the header", "origin": "fix",
                                                                              "why": "a repair from Omi's check"}})
    ui._board_into(w, {"type": "WORK_STARTED", "sender_id": "b1", "payload": {"title": "your job", "origin": "user"}})
    assert shown[0].text == "fix the header — on its own: a repair from Omi's check" and shown[1].text == "your job"
