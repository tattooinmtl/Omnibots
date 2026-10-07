"""A17.e.01: Omi's own goals (an inbox: nothing starts until the user accepts) and the daily journal.

A real database with every migration; the model is scripted. tests/test_a17_live.py asks the real cheap lane."""

from __future__ import annotations

import asyncio
import datetime as dt
import json

import pytest

from omnibots.db import Database
from omnibots.orchestrator.initiative import Initiative

DAY = dt.date(2026, 10, 6)


async def _db(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    await db.open()
    return db


async def _seed(db):
    await db.write("INSERT INTO bots (id, name, role, status, provider_chain_json, risk_ceiling) VALUES "
                   "('front','Frontend','frontend','idle','[]','R3')")
    await db.write("INSERT INTO projects (id, goal, status, created_at) VALUES ('p1','Build a bakery site','completed','2026-10-06T09:00:00Z')")
    for jid, title, status in (("j1", "Write index.html", "completed"), ("j2", "Deploy to Netlify", "failed")):
        await db.write("INSERT INTO jobs (id, project_id, title, status, assigned_bot_id, finished_at) VALUES (?,?,?,?,?,?)",
                       (jid, "p1", title, status, "front", "2026-10-06T15:00:00Z"))


class Script:
    def __init__(self, *answers):
        self.answers, self.prompts = list(answers), []

    async def __call__(self, messages):
        self.prompts.append(messages[0]["content"])
        return self.answers.pop(0) if self.answers else "[]"


def test_journal_is_written_from_real_facts_once_per_day(tmp_path):
    async def go():
        db = await _db(tmp_path)
        await _seed(db)
        chat = Script("We built the bakery site; the Netlify deploy failed.")
        ini = Initiative(db, chat, tmp_path, today=lambda: DAY + dt.timedelta(days=1))
        first = await ini.tick(idle=False)
        second = await ini.tick(idle=False)
        empty = await ini.write_journal(DAY - dt.timedelta(days=3))
        rows = await ini.journal()
        await db.close()
        return chat, first, second, empty, rows
    chat, first, second, empty, rows = asyncio.run(go())
    assert first == {"journal": "2026-10-06"} and second == {} and empty is None
    assert "Build a bakery site" in chat.prompts[0] and "Deploy to Netlify → failed" in chat.prompts[0]
    assert rows[0]["entry"].startswith("We built the bakery site") and json.loads(rows[0]["stats_json"])["failed"] == 1
    assert (tmp_path / "journal" / "2026-10-06.md").read_text(encoding="utf-8").startswith("# Tuesday 06 October 2026")


def test_proposals_wait_in_the_inbox_until_accepted(tmp_path):
    started = []

    async def start_goal(goal, info):
        started.append((goal, info))
        return "proj_9"

    async def go():
        db = await _db(tmp_path)
        await _seed(db)
        chat = Script('Sure: [{"title": "Fix the Netlify deploy", "goal": "Find why the bakery site deploy failed and fix it", '
                      '"why": "yesterday it failed"}, {"title": "Add a menu page", "goal": "Add a menu page", "why": "the site has none"}, '
                      '{"title": "third", "goal": "x", "why": "y"}]')
        ini = Initiative(db, chat, tmp_path, start_goal=start_goal, settings={"max_pending": 3})
        made = await ini.propose()
        nothing_started = list(started)
        dup = None
        try:
            await ini.add("Fix the Netlify deploy", "again")
        except ValueError as exc:
            dup = str(exc)
        acc = await ini.decide(made[0], True)
        rej = await ini.decide(made[1], False, "not now")
        again = await ini.decide(made[0], False)                         # already decided: unchanged
        pend = await ini.pending()
        await db.close()
        return chat, made, nothing_started, dup, acc, rej, again, pend
    chat, made, nothing_started, dup, acc, rej, again, pend = asyncio.run(go())
    assert len(made) == 2 and nothing_started == []                      # at most 2 a round; none starts by itself
    assert "Build a bakery site (completed)" in chat.prompts[0] and "spends money" in chat.prompts[0]
    assert "already proposed" in dup
    assert acc["status"] == "accepted" and acc["project_id"] == "proj_9"
    assert started == [("Find why the bakery site deploy failed and fix it", {"created_by": "omi-proposal"})]
    assert rej["status"] == "rejected" and again["status"] == "accepted" and pend == []


def test_the_clock_and_the_cap(tmp_path):
    async def go():
        db = await _db(tmp_path)
        chat = Script('[{"title": "A", "goal": "a", "why": "w"}]', '[{"title": "B", "goal": "b", "why": "w"}]')
        ini = Initiative(db, chat, tmp_path, settings={"every_hours": 24, "max_pending": 1})
        t0 = 1_800_000_000.0
        first = await ini.tick(idle=True, now=t0)                        # a new install: only starts the clock
        busy = await ini.tick(idle=False, now=t0 + 90000)                 # the team is working: no ideas
        due = await ini.tick(idle=True, now=t0 + 90000)
        full = await ini.propose()                                       # 1 already waits = the cap
        capped = None
        try:
            await ini.add("C", "c")
        except ValueError as exc:
            capped = str(exc)
        await db.close()
        return first, busy, due, full, capped, chat
    first, busy, due, full, capped, chat = asyncio.run(go())
    assert first == {} and busy == {} and len(due["proposals"]) == 1 and full == [] and "already wait" in capped
    assert len(chat.prompts) == 1


def test_omis_tools_and_the_pipe(tmp_path):
    """propose_goal and read_journal as Omi's tools; the PROPOSAL board message the window turns into a card."""
    from omnibots.board.bus import MessageBus
    from omnibots.orchestrator.boss_tools import BossToolkit as BossTools

    async def go():
        db = await _db(tmp_path)
        bus = MessageBus(db)
        ini = Initiative(db, Script(), tmp_path, bus=bus)
        await db.write("INSERT INTO journal (day, entry) VALUES ('2026-10-05', 'A quiet Sunday.')")
        bt = BossTools.__new__(BossTools)
        bt.initiative = ini
        out = await bt.propose_goal({"title": "Check the live site", "goal": "Open the bakery site and check every link", "why": "links break"}, None)
        journal = await bt.read_journal({"days": 3}, None)
        msgs = await db.read("SELECT message_type, payload_json, recipient_id FROM messages")
        await db.close()
        return out, journal, msgs
    out, journal, msgs = asyncio.run(go())
    assert out.startswith("proposed prop_") and "starts only if they accept" in out
    assert journal == "2026-10-05: A quiet Sunday."
    assert msgs[-1]["message_type"] == "PROPOSAL" and msgs[-1]["recipient_id"] == "user"
    assert json.loads(msgs[-1]["payload_json"])["title"] == "Check the live site"


def test_the_card_in_omis_chat_starts_it(tmp_path):
    from qt_helpers import qapp
    qapp()
    from test_a11_live import make
    eng, live = make(tmp_path)
    decided = []

    async def ui_proposals():
        return [{"id": "prop_1", "title": "Fix the deploy", "goal": "Fix the Netlify deploy", "why": "it failed"}]

    async def decide_proposal(pid, accept, note=""):
        decided.append((pid, accept))
        return {"id": pid, "title": "Fix the deploy", "project_id": "proj_7", "status": "accepted"}
    eng.ui_proposals, eng.decide_proposal = ui_proposals, decide_proposal
    w = live.open_bot("omi")
    card = live._proposals["prop_1"]
    assert "Fix the deploy" in card.findChildren(type(card.state))[0].text()
    live.on_board_message({"type": "PROPOSAL", "payload": {"id": "prop_2", "title": "Menu page", "goal": "Add a menu"},
                           "sender_id": "omi", "sender_type": "bot", "recipient_id": "user", "topic": "#orchestrator"})
    assert "prop_2" in live._proposals
    card.accept.click()
    assert decided == [("prop_1", True)] and not card.accept.isEnabled()
    w.close()
