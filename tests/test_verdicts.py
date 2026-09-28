"""A16.b your verdict on the work: 👍 / 👎 with a note on a goal or a bot's accepted claim. It counts
more than the bots' self-grading (a 👎 marks the playbook run failed, the note lands in memory, the
next retrospective reads it), shows per bot and provider, and has a card and a pipe command.
Real database and registry, a real Qt card, a real app process for the pipe."""

from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace

from conftest import run_app, send, wait_until_listening
from mock_provider import MockProviders
from qt_helpers import qapp
from test_a7_orchestrator import build

from omnibots.board.query import query
from omnibots.bots.profile import BOSS_ID
from omnibots.orchestrator import verdicts
from omnibots.orchestrator.playbooks import PlaybookStore, retrospective
from omnibots.ui.widgets import VerdictCard

app = qapp()


def run(coro):
    return asyncio.run(coro)


async def a_finished_goal(e):
    """A project whose goal followed a playbook that the bots graded a success, with one accepted claim."""
    store = PlaybookStore(e["db"], e["bus"])
    pb = await store.create("make a landing page", "1. write index.html\n2. check it")
    pid = await e["projects"].create("a landing page")
    await store.record_run(pb.id, project_id=pid, success=True, tokens=900, seconds=60)
    bot = await e["reg"].create("Builder", "coder", chain=["work/m"])
    cid = await e["ledger"].submit(bot_id=bot.id, text="index.html is written", evidence=[{"kind": "file", "ref": "GOAL.md"}],
                                   project_id=pid, workspace=e["projects"].folder(pid))
    await e["ledger"].decide(cid, True, "ok", BOSS_ID)
    return store, pb, pid, bot, cid


def test_a_thumbs_down_outweighs_the_bots_and_a_note_becomes_a_lesson(tmp_path):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            store, pb, pid, bot, cid = await a_finished_goal(e)
            card0 = await verdicts.card_data(e["db"], e["reg"], pid)
            down = await verdicts.rate(e["db"], e["reg"], e["bus"], project_id=pid, verdict=-1, note="the page had no contact form")
            up = await verdicts.rate(e["db"], e["reg"], e["bus"], project_id=pid, verdict=1, note="clean HTML", claim_id=cid)
            run_ok = (await e["db"].read_one("SELECT success FROM playbook_runs WHERE project_id=?", (pid,)))["success"]
            omi_mem = (await e["reg"].get(BOSS_ID)).memory.section("Lessons Learned")
            bot_mem = (await e["reg"].get(bot.id)).memory.section("Lessons Learned")
            rows = [dict(r) for r in await e["db"].read("SELECT claim_id, bot_id, provider, playbook_id, verdict FROM verdicts ORDER BY id")]
            card1 = await verdicts.card_data(e["db"], e["reg"], pid)
            stats = await verdicts.stats(e["db"])
            board = [m.text() for m in await query(e["db"], project=pid, limit=50) if "You rated" in m.text()]
            notes = await verdicts.recent_notes(e["db"], pb.id)
            await e["db"].close()
            return locals()
    r = run(go())
    assert r["card0"]["verdict"] is None and [c["bot_name"] for c in r["card0"]["claims"]] == ["Builder"]
    assert r["run_ok"] == 0 and "the playbook run is marked failed" in r["down"]["effects"]    # the bots said success
    assert any("From the user (👎): the page had no contact form" in l for l in r["omi_mem"])
    assert any("From the user (👍): clean HTML" in l for l in r["bot_mem"])
    assert r["rows"] == [{"claim_id": None, "bot_id": BOSS_ID, "provider": "boss", "playbook_id": r["pb"].id, "verdict": -1},
                         {"claim_id": r["cid"], "bot_id": r["bot"].id, "provider": "work", "playbook_id": r["pb"].id, "verdict": 1}]
    assert r["card1"]["verdict"] == -1 and r["card1"]["claims"][0]["verdict"] == 1
    assert {"id": "work", "up": 1, "down": 0, "share_up": 1.0} in r["stats"]["provider"]
    assert len(r["board"]) == 2 and r["notes"] == ["👎 the page had no contact form"]


def test_the_next_retrospective_reads_your_verdicts_first_and_learns_after_a_thumbs_down():
    seen = []

    class Router:
        async def chat(self, bot_id, chain, messages, tools, **kw):
            seen.append(messages[-1]["content"])
            return SimpleNamespace(result=SimpleNamespace(answer='{"lessons": ["add a contact form to landing pages"]}'))

    class Memory:
        def __init__(self):
            self.lessons = []

        def add_lessons(self, xs):
            self.lessons += xs

    class Store:
        async def record_run(self, *a, **k):
            return None

    pb = SimpleNamespace(id="pb1", name="landing", version=1, body_md="1. write", stats_line=lambda: "1 run")
    mem = Memory()
    out = run(retrospective(router=Router(), chain=["c/m"], store=Store(), boss_memory=mem, goal="a landing page",
                            report="done", success=True, project_id="p2", playbook=pb, tokens=1, seconds=1,
                            user_verdicts=["👎 the page had no contact form"]))
    assert seen and seen[0].startswith("THE USER'S VERDICTS") and "no contact form" in seen[0]   # first, not skipped
    assert out["lessons"] == ["add a contact form to landing pages"] and mem.lessons == out["lessons"]


def test_the_card_sends_the_row_you_clicked_with_the_note():
    calls = []
    data = {"project_id": "p1", "goal": "a landing page", "verdict": None,
            "claims": [{"id": 7, "bot_id": "b1", "bot_name": "Builder", "text": "index.html is written", "verdict": None}]}
    card = VerdictCard(data, lambda pid, cid, v, note, done: (calls.append((pid, cid, v, note)), done(True, "thanks")))
    card.note.setText("no contact form")
    card.rows[7][1].click()                                               # 👎 on Builder's claim
    card.rows[None][0].click()                                            # 👍 on the goal, note box now empty
    assert calls == [("p1", 7, -1, "no contact form"), ("p1", None, 1, "")]
    assert not card.rows[7][0].isEnabled() and card.rows[7][2].text() == "thanks" and card.note.text() == ""
    card.resize(560, card.sizeHint().height())
    assert card.grab().width() == 560


def test_the_pipe_rates_a_goal_on_the_running_app(home):
    app_proc = run_app("--no-window", wait=False)
    try:
        wait_until_listening()
        conn = sqlite3.connect(home / "db" / "omnibots.sqlite")
        conn.execute("INSERT INTO projects (id, goal, created_by) VALUES ('p_pipe', 'a report', 'user')")
        conn.commit()
        conn.close()
        code, reply = send("rate", "--id", "p_pipe", "--text", "down: it missed the summary")
        bad_code, bad = send("rate", "--id", "p_pipe", "--text", "maybe")
    finally:
        send("stop")
        app_proc.wait(timeout=30)
    assert code == 0 and reply["ok"] and reply["bot_id"] == BOSS_ID
    assert bad_code != 0 and "rate needs" in bad["error"]
    conn = sqlite3.connect(home / "db" / "omnibots.sqlite")
    assert conn.execute("SELECT verdict, note FROM verdicts").fetchall() == [(-1, "it missed the summary")]
    conn.close()
