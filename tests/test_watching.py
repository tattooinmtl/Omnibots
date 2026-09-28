"""A15.f watching the result: the live page is re-checked (down on Watch → you're told; on Fix → a repair
round; back up → said so); Omi's routines and triggers belong to the project and continue it; closing the
project turns them off; the night queue survives a restart. Real database, scheduler and toolkit."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

from mock_provider import MockProviders
from test_a7_orchestrator import build

from omnibots.board.a2a import Inbox
from omnibots.board.query import query
from omnibots.bots.leash import Leash
from omnibots.bots.profile import BOSS_ID
from omnibots.engine import Engine
from omnibots.orchestrator.boss_tools import BossToolkit, GoalContext
from omnibots.projects.schedule import NightShift, Routines, Triggers
from omnibots.runtime.tools import ToolContext


def run(coro):
    return asyncio.run(coro)


def test_a_live_page_that_breaks_is_reported_on_watch_and_repaired_on_fix(tmp_path):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            db = e["db"]
            pid = await e["projects"].create("a bakery site")
            await db.write("UPDATE projects SET live_url='https://bakery.example', live_quote='Fresh bread daily' WHERE id=?", (pid,))
            page = {"text": "Welcome! Fresh bread  daily."}
            rounds = []

            async def fetch(url):
                return page["text"]

            async def continue_goal(p, reason):
                rounds.append(reason)
                return "started"
            eng = SimpleNamespace(db=db, bus=e["bus"], leash=await Leash(db).load(), live_fetch=fetch,
                                  orchestrator=SimpleNamespace(continue_goal=continue_goal))
            check = lambda: Engine.check_live(eng, pid)
            states = [await check()]                                     # ok
            page["text"] = "502 Bad Gateway"
            states.append(await check())                                 # down on Watch: you're told
            states.append(await check())                                 # still down: said once
            watch_rounds = list(rounds)
            page["text"] = "Fresh bread daily"
            states.append(await check())                                 # back up
            await eng.leash.set_level(pid, "fix")
            page["text"] = "404"
            states.append(await check())                                 # down on Fix: a repair round
            row = dict(await db.read_one("SELECT live_status, live_error FROM projects WHERE id=?", (pid,)))
            board = [(m.message_type, m.text()) for m in await query(db, project=pid, limit=50)]
            await db.close()
            return states, watch_rounds, rounds, row, board
    states, watch_rounds, rounds, row, board = run(go())
    assert states == ["ok", "down", "down", "ok", "down"]
    assert watch_rounds == [] and len(rounds) == 1 and "is down" in rounds[0] and "Fresh bread daily" in rounds[0]
    told = [t for k, t in board if k == "QUESTION" and "on Watch" in t]
    assert len(told) == 1 and "no longer shows" in told[0]                   # told once, with the reason
    assert any("is back up" in t for _, t in board) and any("Repair round: started" in t for _, t in board)
    assert row["live_status"] == "down" and "no longer shows" in row["live_error"]


def test_omis_routine_belongs_to_the_project_continues_it_and_stops_when_closed(tmp_path):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            db = e["db"]
            clock = [datetime(2026, 9, 28, 7, 59)]
            fired = []

            async def fire(goal, info):
                fired.append(info)
            routines = Routines(db, fire, clock=lambda: clock[0])
            triggers = Triggers(db, fire)
            night = NightShift(lambda item: asyncio.sleep(0))
            await night.load(db)
            pid = await e["projects"].create("a bakery site")
            folder = e["projects"].folder(pid)
            ctx = GoalContext(pid, "a bakery site", folder)
            inbox = await Inbox.open(e["bus"], BOSS_ID)
            kit = BossToolkit(ctx=ctx, db=db, bus=e["bus"], inbox=inbox, registry=e["reg"], runner=e["runner"], graph=e["graph"],
                              projects=e["projects"], ledger=e["ledger"], factory=e["factory"], router=e["router"],
                              schedule=SimpleNamespace(routines=routines, triggers=triggers, night=night))
            names = {t.name for t in kit.tools()}
            tctx = ToolContext(bot_id=BOSS_ID, workspace=folder)
            added = await kit.add_routine({"name": "morning check", "goal": "open the site and fix what broke", "schedule": "0 8 * * *"}, tctx)
            bad = await kit.add_routine({"name": "x", "goal": "y", "schedule": "every morning"}, tctx)
            outside = await kit.add_trigger({"name": "t", "kind": "file", "config": {"path": "../elsewhere.txt"}, "goal": "g"}, tctx)
            inside = await kit.add_trigger({"name": "t", "kind": "file", "config": {"path": "index.html"}, "goal": "g"}, tctx)
            clock[0] += timedelta(minutes=2)
            await routines.tick()
            continued = []

            async def continue_goal(p, reason):
                continued.append((p, reason))
                return "started"
            eng = SimpleNamespace(db=db, bus=e["bus"], leash=await Leash(db).load(), projects=e["projects"],
                                  orchestrator=SimpleNamespace(continue_goal=continue_goal))
            started = await Engine._background_goal(eng, "open the site and fix what broke", fired[0])
            await Engine.close_project(eng, pid)
            left = [dict(r) for r in await db.read("SELECT enabled FROM routines UNION ALL SELECT enabled FROM triggers")]
            inbox.close()
            await db.close()
            return names, added, bad, outside, inside, fired, started, continued, left, pid
    names, added, bad, outside, inside, fired, started, continued, left, pid = run(go())
    assert {"add_routine", "add_trigger", "enqueue_night"} <= names
    assert "added" in added and bad.startswith("ERROR") and "inside this project" in outside and "added" in inside
    assert fired and fired[0]["project"] == pid and started == pid
    assert continued and continued[0][0] == pid and "morning check" in continued[0][1]
    assert all(r["enabled"] == 0 for r in left)                            # closing the project stopped them


def test_the_night_queue_survives_a_restart(tmp_path):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            ran = []

            async def work(item):
                ran.append(item["goal"])
            first = NightShift(work, idle=lambda: 10**6)
            await first.load(e["db"])
            await first.enqueue_saved("tidy the logs", "p1")
            second = NightShift(work, idle=lambda: 10**6)                  # "after a restart"
            n = await second.load(e["db"])
            did = await second.step()
            third = NightShift(work, idle=lambda: 10**6)
            left = await third.load(e["db"])
            await e["db"].close()
            return n, did, ran, left
    n, did, ran, left = run(go())
    assert n == 1 and did and ran == ["tidy the logs"] and left == 0
