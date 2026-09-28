"""A15.b the leash: every job records who started it, each project has an Off/Watch/Fix dial,
"Pause background work" holds everything the bots start on their own, and the file watch waits
for the folder to settle. Real database, bus and runner; the mock model."""

from __future__ import annotations

import asyncio

from mock_provider import MockProviders, sse
from test_a7_orchestrator import build
from test_presence import _presence

from omnibots.board.query import query
from omnibots.bots.leash import CURRENT_ORIGIN, Leash
from omnibots.bots.presence import WATCH_TOOLS
from omnibots.bots.runner import JobOutcome
from omnibots.engine import Engine
from omnibots.projects.schedule import NightShift


def run(coro):
    return asyncio.run(coro)


def test_the_rules(tmp_path):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            leash = await Leash(e["db"]).load()
            pid = await e["projects"].create("a site")
            out = {"default": await leash.level(pid)}
            out["watch"] = [await leash.may_start(o, pid) is None for o in ("user", "watch", "fix", "routine")]
            await leash.set_level(pid, "fix")
            out["fix"] = [await leash.may_start(o, pid) is None for o in ("user", "watch", "fix")]
            await leash.set_level(pid, "off")
            out["off"] = [await leash.may_start(o, pid) is None for o in ("user", "watch", "fix")]
            await leash.set_level(pid, "fix")
            await leash.set_paused(True)
            out["paused"] = [await leash.may_start(o, x) is None for o, x in (("user", pid), ("fix", pid), ("routine", None))]
            out["reloaded"] = (await Leash(e["db"]).load()).paused          # survives a restart
            await leash.set_paused(False)
            out["resumed"] = (await Leash(e["db"]).load()).paused
            try:
                await leash.set_level(pid, "sometimes")
                out["bad"] = "accepted"
            except ValueError:
                out["bad"] = "refused"
            await e["db"].close()
            return out
    out = run(go())
    assert out["default"] == "watch"                                 # user, 2026-09-27
    assert out["watch"] == [True, True, False, True]                 # on Watch a check may run, a fix may not
    assert out["fix"] == [True, True, True]
    assert out["off"] == [True, False, False]                        # yours always runs
    assert out["paused"] == [True, False, False] and out["reloaded"] is True and out["resumed"] is False
    assert out["bad"] == "refused"


def test_a_run_records_its_origin_and_work_it_creates_inherits_it(tmp_path):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            bot = await e["reg"].create("Ada", "coder", chain=["work/m"])
            pid = await e["projects"].create("a site")
            mock.script("work", sse("checked"))
            out = await e["runner"].run(bot.id, "look at it", project_id=pid, origin="night")
            row = await e["db"].read_one("SELECT origin FROM jobs WHERE id=?", (out.job_id,))
            started = [m for m in await query(e["db"], limit=50) if m.message_type == "WORK_STARTED"]
            after = CURRENT_ORIGIN.get()                               # the caller's origin came back
            token = CURRENT_ORIGIN.set("watch")                        # a job made inside Omi's check is a fix
            fix_job = await e["graph"].add_job(pid, "repair the header")
            CURRENT_ORIGIN.reset(token)
            plain = await e["graph"].add_job(pid, "your own job")
            origins = {r["id"]: r["origin"] for r in await e["db"].read("SELECT id, origin FROM jobs")}
            await e["db"].close()
            return row["origin"], started, after, origins[fix_job.id], origins[plain.id]
    origin, started, after, fix_origin, plain_origin = run(go())
    assert origin == "night" and after == "user"
    assert started and started[-1].payload["origin"] == "night" and "night shift" in started[-1].payload["why"]
    assert fix_origin == "fix" and plain_origin == "user"


def test_a_held_job_waits_with_one_note_and_starts_when_the_dial_allows(tmp_path):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            leash = await Leash(e["db"]).load()
            bot = await e["reg"].create("Ada", "coder", chain=["work/m"])
            pid = await e["projects"].create("keep the site up")
            token = CURRENT_ORIGIN.set("watch")
            job = await e["graph"].add_job(pid, "fix the header", assigned_bot_id=bot.id)
            CURRENT_ORIGIN.reset(token)
            await e["db"].write("UPDATE jobs SET status='assigned' WHERE id=?", (job.id,))
            started = []

            async def fake_run(bot_id, task, **kw):
                started.append(kw.get("job_id"))
                return JobOutcome(kw.get("job_id") or "", bot_id, "completed", None, 0.0, [])

            e["runner"].run = fake_run
            p = _presence(e, leash=leash)
            await p._pickup_ready(bot.id)                               # on Watch: a fix waits
            await p._pickup_ready(bot.id)
            held = list(started)
            notes = [m for m in await query(e["db"], project=pid, limit=50) if "is holding" in m.text()]
            await leash.set_level(pid, "fix")
            await p._pickup_ready(bot.id)
            await p._jobs[bot.id]
            await e["db"].close()
            return held, notes, started, job.id
    held, notes, started, jid = run(go())
    assert held == [] and len(notes) == 1 and "on Watch" in notes[0].text()
    assert started == [jid]


def test_a_watch_check_gets_only_the_tools_that_look(tmp_path):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            leash = await Leash(e["db"]).load()
            pid = await e["projects"].create("a shop")
            calls = []

            async def fake_run(bot_id, task, **kw):
                calls.append(({t.name for t in kw["extra_tools"]}, task, kw.get("origin"), kw.get("title")))
                return JobOutcome("", bot_id, "completed", None, 0.0, [])

            e["runner"].run = fake_run
            p = _presence(e, leash=leash)
            await p._maintain(pid, ["changed index.html"])              # Watch (the default)
            await leash.set_level(pid, "fix")
            await p._maintain(pid, ["changed index.html"])
            await e["db"].close()
            return calls
    (watch_tools, watch_task, watch_origin, watch_title), (fix_tools, fix_task, _, fix_title) = run(go())
    assert watch_tools == WATCH_TOOLS and "report only" in watch_task and watch_origin == "watch"
    assert watch_title.startswith("[check]")
    assert {"assign_job", "create_bot", "plan_goal"} <= fix_tools and fix_title.startswith("[maintain]")


def test_the_file_watch_waits_for_the_folder_to_settle_and_respects_off(tmp_path):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            leash = await Leash(e["db"]).load()
            now = [1000.0]
            seen = []

            async def maintain(pid, changes):
                seen.append(pid)

            p = _presence(e, leash=leash, maintain=maintain, maintain_cooldown=0, settle_seconds=100, clock=lambda: now[0])
            pid = await e["projects"].create("a site")
            f = e["projects"].folder(pid) / "index.html"
            f.write_text("v1", encoding="utf-8")
            await p._watch_files()                                      # baseline
            f.write_text("v2", encoding="utf-8")
            await p._watch_files()
            now[0] += 60
            f.write_text("v3 longer", encoding="utf-8")                 # still editing: the clock restarts
            await p._watch_files()
            now[0] += 60
            await p._watch_files()
            early = list(seen)
            now[0] += 50                                                # 110 s quiet
            await p._watch_files()
            if pid in p._maint_tasks:
                await p._maint_tasks[pid]
            settled = list(seen)
            await leash.set_level(pid, "off")
            f.write_text("v4 even longer", encoding="utf-8")
            await p._watch_files()
            now[0] += 500
            await p._watch_files()
            await asyncio.sleep(0.05)
            off = list(seen)
            await e["db"].close()
            return early, settled, off, pid
    early, settled, off, pid = run(go())
    assert early == [] and settled == [pid] and off == [pid]


def test_pause_holds_the_night_shift_and_routines(tmp_path):
    async def go():
        ran = []

        async def work(item):
            ran.append(item["goal"])

        paused = [True]
        ns = NightShift(work, idle=lambda: 10**6, held=lambda: paused[0])
        ns.enqueue("tidy logs")
        first = await ns.step()
        paused[0] = False
        second = await ns.step()

        # a routine firing while paused is skipped and says so; once resumed it starts its goal
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            goals = []

            class Eng:                                                  # just what _background_goal uses
                async def start_goal(self, goal, info=None):
                    goals.append(goal)
                    return "p_new"

            eng = Eng()
            eng.leash, eng.bus = await Leash(e["db"]).load(), e["bus"]
            await eng.leash.set_paused(True)
            skipped = await Engine._background_goal(eng, "check the site", {"routine": 1, "name": "Monday check"})
            await eng.leash.set_paused(False)
            started = await Engine._background_goal(eng, "check the site", {"routine": 1, "name": "Monday check"})
            notes = [m.text() for m in await query(e["db"], limit=50) if "Skipped the routine" in m.text()]
            await e["db"].close()
        return first, second, ran, skipped, started, goals, notes
    first, second, ran, skipped, started, goals, notes = run(go())
    assert first is False and second is True and ran == ["tidy logs"]
    assert skipped == "" and started == "p_new" and goals == ["check the site"]
    assert len(notes) == 1 and "Monday check" in notes[0] and "paused" in notes[0]
