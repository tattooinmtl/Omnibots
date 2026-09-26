"""A6: projects under git, the task graph, retries and escalation, budgets,
cron routines, triggers and the night shift."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path

import pytest

from mock_provider import MockProviders, sse, status
from omnibots.board.bus import MessageBus
from omnibots.board.ledger import Ledger
from omnibots.board.query import query
from omnibots.bots.profile import BOSS_ID, BotRegistry
from omnibots.bots.runner import JobRunner
from omnibots.db import Database
from omnibots.omni.config import OmniConfig, ProviderInfo
from omnibots.omni.locate import OmniLocation
from omnibots.projects.graph import GraphError, PlanRunner, TaskGraph
from omnibots.projects.schedule import Cron, CronError, NightShift, Routines, Triggers
from omnibots.projects.store import ProjectStore
from omnibots.providers.quota import QuotaManager
from omnibots.providers.router import Router
from omnibots.providers.seats import SeatScheduler
from omnibots.runtime.approvals import ApprovalCenter
from omnibots.runtime.sandbox import Sandbox


def run(coro):
    return asyncio.run(coro)


def call(name, **args):
    return {"name": name, "args": args}


async def env(tmp: Path, mock: MockProviders | None = None):
    db = Database(tmp / "db" / "omnibots.sqlite")
    await db.open()
    bus = MessageBus(db)
    e = {"db": db, "bus": bus, "projects": ProjectStore(db, tmp / "projects", bus), "graph": TaskGraph(db, bus),
         "reg": BotRegistry(db, tmp / "bots", bus)}
    if mock:
        names = ("good", "slow", "broken", "cheap")
        providers = {n: ProviderInfo(name=n, base_url=mock.url(n), api_key="k", key_source="settings", raw={"reasoningParam": "none"}) for n in names}
        cfg = OmniConfig(OmniLocation(Path("."), Path(".")), providers,
                         {f"{n}/m": {"provider": n, "id": n, "maxTokens": 256} for n in names}, None, None, [], {})
        router = Router(lambda: cfg, QuotaManager(db), SeatScheduler(boss_id=BOSS_ID), base_delay=0.01, max_retries=0)
        e["runner"] = JobRunner(db=db, registry=e["reg"], router=router, approvals=ApprovalCenter(db), home=tmp,
                                sandbox=Sandbox(tmp / "sandbox"), bus=bus, ledger=Ledger(db, bus), learn=False)
        e["plans"] = PlanRunner(e["graph"], e["runner"].run, workspace_for=e["projects"].folder)
    return e


# ── projects ───────────────────────────────────────────────────────────────
def test_project_is_a_git_repo_with_commits_and_diffs(tmp_path):
    async def go():
        e = await env(tmp_path)
        pid = await e["projects"].create("Build a landing page for the bakery")
        folder = e["projects"].folder(pid)
        (folder / "index.html").write_text("<h1>Bakery</h1>\n", encoding="utf-8")
        sha = await e["projects"].commit(pid, "add index.html", author="bot_web")
        nothing = await e["projects"].commit(pid, "no changes")
        diff = await e["projects"].diff(pid)
        log = await e["projects"].log(pid)
        row = await e["db"].read_one("SELECT goal, status, path FROM projects WHERE id=?", (pid,))
        board = [m.message_type for m in await query(e["db"], project=pid)]
        await e["db"].close()
        return pid, folder, sha, nothing, diff, log, dict(row), board
    pid, folder, sha, nothing, diff, log, row, board = run(go())
    assert (folder / ".git").is_dir() and (folder / "GOAL.md").read_text().startswith("# Goal")
    assert sha and len(sha) == 40 and nothing is None
    assert "+<h1>Bakery</h1>" in diff and [l["message"] for l in log] == ["add index.html", "project created"]
    assert log[0]["author"] == "bot_web" and row["status"] == "open" and board == ["TASK_RECEIVED"]


# ── the graph ──────────────────────────────────────────────────────────────
def test_readiness_cycles_blocking_pause_resume(tmp_path):
    async def go():
        e = await env(tmp_path)
        g = e["graph"]
        pid = await e["projects"].create("demo")
        a = await g.add_job(pid, "A")
        b = await g.add_job(pid, "B")
        c = await g.add_job(pid, "C", depends_on=[a.id, b.id])
        d = await g.add_job(pid, "D", depends_on=[c.id])
        errs = []
        for fn in (lambda: g.add_job(pid, "X", depends_on=["job_nope"]), lambda: g.add_dependency(a.id, d.id), lambda: g.add_job(pid, " ")):
            with pytest.raises(GraphError) as ex:
                await fn()
            errs.append(str(ex.value))
        ready1 = [j.title for j in await g.ready(pid)]
        await g._set(a, "completed")
        ready2 = [j.title for j in await g.ready(pid)]
        await g._set(b, "completed")
        ready3 = [j.title for j in await g.ready(pid)]
        await g.pause(c.id)
        paused = (await g.get(c.id)).status
        await g.resume(c.id)
        resumed = (await g.get(c.id)).status
        await g.cancel(c.id)
        after_cancel = {j.title: j.status for j in await g.jobs(pid)}
        snap = await g.snapshot(pid)
        await e["db"].close()
        return errs, ready1, ready2, ready3, paused, resumed, after_cancel, snap, (a, b, c, d)
    errs, r1, r2, r3, paused, resumed, after, snap, (a, b, c, d) = run(go())
    assert "unknown dependencies" in errs[0] and "cycle" in errs[1] and "title" in errs[2]
    assert r1 == ["A", "B"] and r2 == ["B"] and r3 == ["C"]
    assert paused == "paused" and resumed == "ready"
    assert after == {"A": "completed", "B": "completed", "C": "cancelled", "D": "blocked"}     # blocking cascades
    assert sorted(map(tuple, snap["edges"])) == sorted([(a.id, c.id), (b.id, c.id), (c.id, d.id)])


def test_retry_then_escalate_with_a_question_on_the_board(tmp_path):
    async def go():
        e = await env(tmp_path)
        g = e["graph"]
        pid = await e["projects"].create("demo")
        j = await g.add_job(pid, "flaky", budget={"max_attempts": 2})
        dep = await g.add_job(pid, "after flaky", depends_on=[j.id])
        await e["db"].write("UPDATE jobs SET attempts=1 WHERE id=?", (j.id,))
        first = await g.record_outcome(j.id, "failed", error="boom 1")
        await e["db"].write("UPDATE jobs SET attempts=2 WHERE id=?", (j.id,))
        second = await g.record_outcome(j.id, "failed", error="boom 2")
        states = {x.title: x.status for x in await g.jobs(pid)}
        q = [m for m in await query(e["db"], job=j.id) if m.message_type == "QUESTION"]
        await e["db"].close()
        return first, second, states, q
    first, second, states, q = run(go())
    assert first == "ready" and second == "escalated"
    assert states == {"flaky": "blocked", "after flaky": "blocked"}
    assert q and q[0].recipient_id == "omi" and "failed 2 time(s)" in q[0].text() and "boom 2" in q[0].text()


# ── A6.99: a hand-made 5-task DAG across 2 bots ─────────────────────────────
def test_five_task_dag_runs_in_order_across_two_bots_and_escalates_a_failure(tmp_path):
    async def go():
        with MockProviders() as mock:
            step = sse("", tool_calls=[call("write_file", path="PLACEHOLDER", content="x")])
            mock.default["good"] = sse("done")
            mock.default["broken"] = status(400, {"error": {"message": "this bot always fails"}})
            e = await env(tmp_path, mock)
            g = e["graph"]
            b1 = await e["reg"].create("Builder-1", "coder", chain=["good/m"])
            b2 = await e["reg"].create("Builder-2", "coder", chain=["good/m"])
            b3 = await e["reg"].create("Breaker", "coder", chain=["broken/m"])
            pid = await e["projects"].create("five jobs")
            A = await g.add_job(pid, "A", assigned_bot_id=b1.id)
            B = await g.add_job(pid, "B", assigned_bot_id=b2.id)
            C = await g.add_job(pid, "C", depends_on=[A.id, B.id], assigned_bot_id=b1.id)
            D = await g.add_job(pid, "D", depends_on=[C.id], assigned_bot_id=b2.id)
            E = await g.add_job(pid, "E", depends_on=[C.id], assigned_bot_id=b1.id)
            F = await g.add_job(pid, "F (always fails)", assigned_bot_id=b3.id, budget={"max_attempts": 2})
            G = await g.add_job(pid, "G (after F)", depends_on=[F.id], assigned_bot_id=b2.id)
            result = await e["plans"].run(pid)
            rows = {r["title"]: dict(r) for r in await e["db"].read("SELECT title, status, attempts, started_at, finished_at, assigned_bot_id FROM jobs WHERE project_id=?", (pid,))}
            q = [m for m in await query(e["db"], project=pid) if m.message_type == "QUESTION"]
            await e["db"].close()
            return result, rows, q, (b1, b2)
    result, rows, q, (b1, b2) = run(go())
    for t in "ABCDE":
        assert rows[t]["status"] == "completed", rows[t]
    # Order: C only after A and B finished; D and E only after C finished.
    assert rows["C"]["started_at"] >= max(rows["A"]["finished_at"], rows["B"]["finished_at"])
    assert rows["D"]["started_at"] >= rows["C"]["finished_at"] and rows["E"]["started_at"] >= rows["C"]["finished_at"]
    # Two bots really worked in parallel: A (bot 1) and B (bot 2) overlapped.
    assert rows["A"]["assigned_bot_id"] == b1.id and rows["B"]["assigned_bot_id"] == b2.id
    assert rows["B"]["started_at"] < rows["A"]["finished_at"]
    # The failure retried once, then escalated; its dependent was blocked, never started.
    assert rows["F (always fails)"]["status"] == "blocked" and rows["F (always fails)"]["attempts"] == 2
    assert rows["G (after F)"]["status"] == "blocked" and rows["G (after F)"]["started_at"] is None
    assert len(q) == 1 and "F (always fails)" in q[0].text()


def test_shared_project_workspace_and_budgets(tmp_path):
    async def go():
        with MockProviders() as mock:
            mock.script("good", sse("", tool_calls=[call("write_file", path="part1.txt", content="from bot one")]), sse("wrote part1"),
                        sse("", tool_calls=[call("list_dir", path=".")], usage={"prompt_tokens": 900, "completion_tokens": 200}),
                        sse("", tool_calls=[call("list_dir", path=".")]))
            slow = sse("too slow")
            slow["hold"] = 2.0
            mock.default["slow"] = slow
            e = await env(tmp_path, mock)
            bot = await e["reg"].create("W", "coder", chain=["good/m"])
            pid = await e["projects"].create("shared")
            folder = e["projects"].folder(pid)
            ok = await e["runner"].run(bot.id, "write part1", project_id=pid, workspace=folder)
            tok = await e["runner"].run(bot.id, "loop", project_id=pid, workspace=folder, budget={"tokens": 1000})
            slowbot = await e["reg"].create("S", "coder", chain=["slow/m"])
            timed = await e["runner"].run(slowbot.id, "slow", budget={"seconds": 0.5})
            rows = {r["id"]: dict(r) for r in await e["db"].read("SELECT id, status, error_message FROM jobs")}
            await e["db"].close()
            return ok, tok, timed, folder, rows, bot
    ok, tok, timed, folder, rows, bot = run(go())
    assert ok.status == "completed" and (folder / "part1.txt").read_text() == "from bot one"
    assert not (bot.workspace / "part1.txt").exists()                     # the project folder, not the bot's own
    assert tok.status == "blocked" and "token budget of 1000 used up" in rows[tok.job_id]["error_message"]
    assert timed.status == "blocked" and "time budget of 0.5s" in rows[timed.job_id]["error_message"] and timed.seconds < 2


# ── routines, triggers, night shift ────────────────────────────────────────
def test_cron_expressions():
    t = datetime(2026, 9, 25, 10, 30)                                     # a Friday
    assert Cron("0 8 * * 1").next_after(t) == datetime(2026, 9, 28, 8, 0)  # next Monday 08:00
    assert Cron("*/15 * * * *").next_after(t) == datetime(2026, 9, 25, 10, 45)
    assert Cron("0 0 1 * *").next_after(t) == datetime(2026, 10, 1, 0, 0)
    assert Cron("@daily").next_after(t) == datetime(2026, 9, 26, 0, 0)
    assert Cron("30 9 * * 1-5").next_after(datetime(2026, 9, 26, 12, 0)) == datetime(2026, 9, 28, 9, 30)   # Sat -> Mon
    assert Cron("0 12 * * 7").next_after(t) == datetime(2026, 9, 27, 12, 0)   # 7 = Sunday
    for bad in ("* * *", "61 * * * *", "* * * 13 *", "*/0 * * * *"):
        with pytest.raises(CronError):
            Cron(bad)


def test_routines_fire_when_due_and_persist(tmp_path):
    async def go():
        db = Database(tmp_path / "db.sqlite")
        await db.open()
        clock = {"t": datetime(2026, 9, 25, 7, 59)}
        fired = []

        async def fire(goal, info):
            fired.append((goal, info["name"]))
        r = Routines(db, fire, clock=lambda: clock["t"])
        rid = await r.add("Monday SEO check", "check my site's SEO", "0 8 * * *")
        none_yet = await r.tick()
        clock["t"] = datetime(2026, 9, 25, 8, 0)
        once = await r.tick()
        again = await r.tick()                                            # same minute: no double fire
        row = dict((await r.list())[0])
        await db.close()
        return rid, none_yet, once, again, fired, row
    rid, none_yet, once, again, fired, row = run(go())
    assert none_yet == [] and once == [rid] and again == [] and fired == [("check my site's SEO", "Monday SEO check")]
    assert row["last_run_at"] == "2026-09-25T08:00" and row["next_run_at"] == "2026-09-26T08:00"


def test_triggers_file_board_threshold(tmp_path):
    async def go():
        db = Database(tmp_path / "db.sqlite")
        await db.open()
        bus = MessageBus(db)
        fired = []
        disk = {"gb": 50.0}

        async def fire(goal, info):
            fired.append((goal, info["detail"]))
        trg = Triggers(db, fire, bus=bus, metrics={"disk_free_gb": lambda: disk["gb"]}, cooldown_seconds=0)
        watched = tmp_path / "inbox.csv"
        watched.write_text("a", encoding="utf-8")
        await trg.add("new data", "file", {"path": str(watched)}, "process the new CSV")
        await trg.add("low disk", "threshold", {"metric": "disk_free_gb", "below": 10}, "clean up old artifacts")
        await trg.add("failures", "board", {"type": "TASK_FAILED", "contains": "deploy"}, "investigate the failed deploy")
        with pytest.raises(ValueError):
            await trg.add("hook", "webhook", {}, "x")                      # A10
        board = asyncio.create_task(trg.board_loop())
        await asyncio.sleep(0.05)
        await trg.check_files_and_thresholds()                             # baseline
        watched.write_text("a,b", encoding="utf-8")
        disk["gb"] = 5.0
        await trg.check_files_and_thresholds()
        await trg.check_files_and_thresholds()                             # still below: no second fire
        await bus.publish("#job/x", "TASK_FAILED", {"error": "deploy to netlify failed"})
        await bus.publish("#job/y", "TASK_FAILED", {"error": "unit test failed"})
        await asyncio.sleep(0.1)
        board.cancel()
        await db.close()
        return fired
    fired = run(go())
    goals = [g for g, _ in fired]
    assert goals.count("process the new CSV") == 1 and goals.count("clean up old artifacts") == 1
    assert goals.count("investigate the failed deploy") == 1 and len(fired) == 3


def test_night_shift_runs_only_while_the_user_is_away():
    async def go():
        idle = {"s": 30.0}
        ran = []

        async def work(item):
            ran.append(item["goal"])
        ns = NightShift(work, idle_after=900, idle=lambda: idle["s"])
        ns.enqueue("tidy old logs")
        ns.enqueue("re-run the SEO report")
        r1 = await ns.step()                                               # user active
        idle["s"] = 1200
        r2 = await ns.step()
        idle["s"] = 5                                                      # user came back
        r3 = await ns.step()
        return r1, r2, r3, ran, len(ns.queue)
    r1, r2, r3, ran, left = run(go())
    assert (r1, r2, r3) == (False, True, False) and ran == ["tidy old logs"] and left == 1
