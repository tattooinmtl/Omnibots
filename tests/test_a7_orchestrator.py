"""A7: the orchestrator. Planner, factory + governor, council, the boss toolkit
(claims, council gate, ask_user), a full goal run to REPORT.md with a scripted
boss, team controls that cascade, and the stall monitor. Mock providers, no tokens."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from mock_provider import MockProviders, sse
from omnibots.board.a2a import Inbox
from omnibots.board.bus import MessageBus
from omnibots.board.ledger import Ledger
from omnibots.board.query import query
from omnibots.board.types import topic_bot
from omnibots.bots.profile import BOSS_ID, BotRegistry
from omnibots.bots.runner import JobRunner
from omnibots.db import Database
from omnibots.omni.config import OmniConfig, ProviderInfo
from omnibots.omni.locate import OmniLocation
from omnibots.orchestrator.boss_tools import BossToolkit, GoalContext
from omnibots.orchestrator.council import hold_council
from omnibots.orchestrator.factory import BotFactory, GovernorLimits, SpawnGovernor, SpawnRefused
from omnibots.orchestrator.goal import Orchestrator
from omnibots.orchestrator.planner import PlanError, make_plan, parse_plan
from omnibots.orchestrator.team import TeamController
from omnibots.projects.graph import TaskGraph
from omnibots.projects.store import ProjectStore
from omnibots.providers.quota import QuotaManager
from omnibots.providers.router import Router
from omnibots.providers.seats import SeatScheduler
from omnibots.runtime.approvals import ApprovalCenter
from omnibots.runtime.sandbox import Sandbox
from omnibots.runtime.tools import ToolContext

PROVIDERS = ("boss", "work", "plan", "slow")


def call(name, **args):
    return {"name": name, "args": args}


def plan_json(*subtasks, question=None):
    return json.dumps({"question": question, "subtasks": list(subtasks)})


def sub(title, deps=(), risk="R1", done="the file exists"):
    return {"title": title, "description": f"do {title}", "done_criteria": done, "depends_on": list(deps), "skills": ["writing"], "risk": risk}


async def build(tmp: Path, mock: MockProviders):
    db = Database(tmp / "db" / "omnibots.sqlite")
    await db.open()
    bus = MessageBus(db)
    providers = {n: ProviderInfo(name=n, base_url=mock.url(n), api_key="k", key_source="settings", raw={"reasoningParam": "none"}) for n in PROVIDERS}
    cfg = OmniConfig(OmniLocation(Path("."), Path(".")), providers, {f"{n}/m": {"provider": n, "id": n, "maxTokens": 256} for n in PROVIDERS},
                     None, None, [], {})
    seats = SeatScheduler(boss_id=BOSS_ID)
    router = Router(lambda: cfg, QuotaManager(db), seats, base_delay=0.01, max_retries=0)
    reg = BotRegistry(db, tmp / "bots", bus)
    await reg.ensure_boss()
    await reg.update(BOSS_ID, chain=["boss/m"])
    ledger = Ledger(db, bus)
    approvals = ApprovalCenter(db)
    runner = JobRunner(db=db, registry=reg, router=router, approvals=approvals, home=tmp, sandbox=Sandbox(tmp / "sandbox"),
                       bus=bus, ledger=ledger, learn=False)
    graph, projects = TaskGraph(db, bus), ProjectStore(db, tmp / "projects", bus)
    factory = BotFactory(reg, SpawnGovernor(), db=db)
    orch = Orchestrator(db=db, bus=bus, registry=reg, runner=runner, graph=graph, projects=projects, ledger=ledger,
                        router=router, factory=factory, planner_chain=["plan/m"], goal_seconds=60)
    team = TeamController(db=db, bus=bus, runner=runner, graph=graph, orchestrator=orch, approvals=approvals, seats=seats, stall_after=60)
    return dict(db=db, bus=bus, reg=reg, runner=runner, graph=graph, projects=projects, ledger=ledger, factory=factory,
                orch=orch, team=team, router=router, approvals=approvals, seats=seats)


def run(coro):
    return asyncio.run(coro)


# ── planner ────────────────────────────────────────────────────────────────
def test_plan_parsing_rules():
    p = parse_plan("```json\n" + plan_json(sub("a"), sub("b", [1])) + "\n```")
    assert [s.title for s in p.subtasks] == ["a", "b"] and p.subtasks[1].depends_on == [1]
    assert parse_plan(plan_json(question="Which site?")).question == "Which site?"
    for bad, why in [(plan_json(sub("a", [1])), "earlier"), (plan_json({"title": "x", "done_criteria": ""}), "done_criteria"),
                     ("no json here", "JSON"), (plan_json(), "neither"), (plan_json(*[sub(str(i)) for i in range(11)]), "too many")]:
        with pytest.raises(PlanError, match=why):
            parse_plan(bad)


def test_planner_retries_invalid_json_and_never_names_tools(tmp_path):
    async def go():
        with MockProviders() as mock:
            mock.script("plan", sse("Sure! Here's my plan: step 1..."),
                        sse(plan_json({**sub("gather"), "description": "use web_search and ask bot_abc to gather prices"})))
            e = await build(tmp_path, mock)
            plan = await make_plan(e["router"], ["plan/m"], "compare hosts")
            second = mock.requests[1]["body"]["messages"][-1]["content"]
            await e["db"].close()
            return plan, second
    plan, second = run(go())
    assert "That plan was invalid" in second
    assert "web_search" not in plan.subtasks[0].description and "bot_abc" not in plan.subtasks[0].description


# ── factory + governor ─────────────────────────────────────────────────────
def test_spawn_governor_limits():
    t = {"now": 0.0}
    g = SpawnGovernor(GovernorLimits(max_bots=5, max_spawn_per_goal=2, max_spawn_per_minute=2), clock=lambda: t["now"])
    g.check(active_bots=1, goal="p1")
    g.record("p1")
    g.record("p1")
    with pytest.raises(SpawnRefused, match="per-goal"):
        g.check(active_bots=3, goal="p1")
    with pytest.raises(SpawnRefused, match="per minute"):
        g.check(active_bots=3, goal="p2")
    t["now"] = 61                                                  # the rolling minute passes
    g.check(active_bots=3, goal="p2")
    with pytest.raises(SpawnRefused, match="limit of 5"):
        g.check(active_bots=5, goal="p3")


def test_factory_is_quota_aware_and_filters_tools(tmp_path):
    class Quota:
        def snapshot(self):
            return {"minimax.io": {"used_pct": 95.0}}

    async def go():
        db = Database(tmp_path / "db.sqlite")
        await db.open()
        reg = BotRegistry(db, tmp_path / "bots")
        f = BotFactory(reg, SpawnGovernor(), db=db, quota=Quota())
        prof, lane = await f.create(name="R", role="researcher", tools=["web_search", "rm_rf_everything", "read_file"], lane="minimax", goal="p")
        est = await f.estimate()
        await db.close()
        return prof, lane, est
    prof, lane, est = run(go())
    assert lane.startswith("cheap") and "90%" in lane and prof.chain[0].startswith("nvidia")
    assert prof.tools == ["web_search", "read_file", "find_skill", "invoke_skill", "ask_help"] and prof.created_by == "omi" and est["tokens_per_job"] > 0


# ── council ────────────────────────────────────────────────────────────────
def test_council_three_views_cross_examined_and_recorded(tmp_path):
    async def go():
        with MockProviders() as mock:
            mock.script("plan", sse("Use Netlify.\nRECOMMENDATION: Netlify"), sse("Vercel lock-in.\nRECOMMENDATION: Cloudflare Pages"),
                        sse("Cheapest.\nRECOMMENDATION: Netlify"), sse("Still Netlify.\nRECOMMENDATION: Netlify"),
                        sse("Changed my mind.\nRECOMMENDATION: Netlify"), sse("Agree.\nRECOMMENDATION: Netlify"))
            e = await build(tmp_path, mock)
            rec = await hold_council(e["router"], "Which static host?", chain=["plan/m"], bus=e["bus"], project_id="p1")
            second_round = [r["body"]["messages"][-1]["content"] for r in mock.requests[3:]]
            board = [m.message_type for m in await query(e["db"], topic="#council")]
            await e["db"].close()
            return rec, second_round, board
    rec, second_round, board = run(go())
    assert set(rec.answers) == {"pragmatist", "skeptic", "user advocate"} and rec.agreement == 1.0
    assert all("Cross-examine" in c for c in second_round)
    assert board == ["COUNCIL_OPENED", "COUNCIL_VERDICT"]


# ── the boss toolkit ───────────────────────────────────────────────────────
def test_toolkit_claims_council_gate_and_rejection(tmp_path):
    async def go():
        with MockProviders() as mock:
            mock.script("plan", sse(plan_json(sub("write notes", done="notes.md exists"), sub("publish", [1], risk="R3", done="publish.txt exists"))),
                        *[sse(f"view {i}\nRECOMMENDATION: go") for i in range(6)])
            mock.script("work",
                        sse("", tool_calls=[call("write_file", path="notes.md", content="# notes")]),
                        sse("", tool_calls=[call("submit_claim", text="notes written", evidence=[{"kind": "file", "ref": "notes.md"}])]),
                        sse("notes done"),
                        sse("", tool_calls=[call("write_file", path="publish.txt", content="draft")]),
                        sse("", tool_calls=[call("submit_claim", text="published", evidence=[{"kind": "file", "ref": "publish.txt"}])]),
                        sse("published"),
                        sse("", tool_calls=[call("write_file", path="publish.txt", content="final")]),
                        sse("", tool_calls=[call("submit_claim", text="published final", evidence=[{"kind": "file", "ref": "publish.txt"}])]),
                        sse("published again"))
            e = await build(tmp_path, mock)
            pid = await e["projects"].create("notes then publish")
            ctx = GoalContext(pid, "notes then publish", e["projects"].folder(pid))
            inbox = await Inbox.open(e["bus"], BOSS_ID)
            kit = BossToolkit(ctx=ctx, db=e["db"], bus=e["bus"], inbox=inbox, registry=e["reg"], runner=e["runner"], graph=e["graph"],
                              projects=e["projects"], ledger=e["ledger"], factory=e["factory"], router=e["router"], planner_chain=["plan/m"])
            w = await e["reg"].create("Writer", "writer", chain=["work/m"], bot_id="bot_w1")
            t = ToolContext(bot_id=BOSS_ID, workspace=ctx.folder)
            out = {}
            out["plan"] = await kit.plan_goal({}, t)
            jobs = await kit._plan_jobs()
            out["early"] = await kit.assign_job({"job_id": jobs[1].id, "bot_id": w.id}, t)
            out["assign1"] = await kit.assign_job({"job_id": jobs[0].id, "bot_id": w.id}, t)
            await ctx.worker_tasks[jobs[0].id]
            out["status_after_work"] = (await e["graph"].get(jobs[0].id)).status
            out["list"] = await kit.list_jobs({}, t)
            claim1 = (await e["ledger"].claims(job_id=jobs[0].id))[0]["id"]
            out["accept"] = await kit.accept_claim({"claim_id": claim1, "reason": "notes.md exists"}, t)
            out["gate"] = await kit.assign_job({"job_id": jobs[1].id, "bot_id": w.id}, t)
            out["council"] = await kit.council({"question": "publish now?"}, t)
            out["assign2"] = await kit.assign_job({"job_id": jobs[1].id, "bot_id": w.id}, t)
            await ctx.worker_tasks[jobs[1].id]
            claim2 = (await e["ledger"].claims(job_id=jobs[1].id))[0]["id"]
            out["reject"] = await kit.reject_claim({"claim_id": claim2, "reason": "still a draft", "new_instructions": "write the final text"}, t)
            await ctx.worker_tasks[jobs[1].id]
            restarted_prompt = next(r["body"]["messages"][1]["content"] for r in mock.requests if r["provider"] == "work"
                                    and "previous attempt was rejected" in r["body"]["messages"][1]["content"])
            claim3 = [c for c in await e["ledger"].claims(job_id=jobs[1].id) if c["status"] == "submitted"][0]["id"]
            out["accept2"] = await kit.accept_claim({"claim_id": claim3}, t)
            final = {j.title: (j.status, j.attempts) for j in await kit._plan_jobs()}
            inbox.close()
            await e["db"].close()
            return out, final, restarted_prompt, ctx.folder
    out, final, restarted, folder = run(go())
    assert "Plan recorded" in out["plan"] and "write notes" in out["plan"]
    assert out["early"].startswith("ERROR") and "not ready" in out["early"]
    assert out["assign1"].startswith("started bot_w1") and out["status_after_work"] == "review"
    assert "Claims waiting for your decision" in out["list"]
    assert "accepted" in out["accept"] and "Now ready" in out["accept"] and "publish" in out["accept"]
    assert out["gate"].startswith("ERROR") and "council" in out["gate"]
    assert "COUNCIL on: publish now?" in out["council"] and out["assign2"].startswith("started")
    assert "rejected" in out["reject"] and "restarted" in out["reject"]
    assert "still a draft" in restarted and "write the final text" in restarted
    assert final == {"write notes": ("completed", 1), "publish": ("completed", 2)}
    assert (folder / "publish.txt").read_text() == "final"


def test_ask_user_waits_for_the_users_answer_and_keeps_bot_messages(tmp_path):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            pid = await e["projects"].create("x")
            ctx = GoalContext(pid, "x", e["projects"].folder(pid))
            inbox = await Inbox.open(e["bus"], BOSS_ID)
            kit = BossToolkit(ctx=ctx, db=e["db"], bus=e["bus"], inbox=inbox, registry=e["reg"], runner=e["runner"], graph=e["graph"],
                              projects=e["projects"], ledger=e["ledger"], factory=e["factory"], router=e["router"])
            task = asyncio.create_task(kit.ask_user({"question": "Which domain?", "timeout_seconds": 5}, ToolContext(bot_id=BOSS_ID, workspace=ctx.folder)))
            await asyncio.sleep(0.1)
            await e["bus"].publish(topic_bot(BOSS_ID), "A2A_MESSAGE", {"text": "worker update"}, sender_type="bot", sender_id="bot_w1", recipient_id=BOSS_ID)
            await e["bus"].publish(topic_bot(BOSS_ID), "A2A_MESSAGE", {"text": "use bakery.example"}, sender_type="user", sender_id="user", recipient_id=BOSS_ID)
            answer = await task
            kept = inbox.sub.drain()
            q = [m for m in await query(e["db"], project=pid) if m.message_type == "QUESTION"]
            inbox.close()
            await e["db"].close()
            return answer, kept, q
    answer, kept, q = run(go())
    assert answer == "The user answered: use bakery.example"
    assert [m.text() for m in kept] == ["worker update"] and q[0].recipient_id == "user"


# ── a whole goal, scripted boss, to REPORT.md ──────────────────────────────
def _last_tool(body):
    return "\n".join(m.get("content") or "" for m in body["messages"] if m.get("role") == "tool")


def test_full_goal_run_produces_a_report(tmp_path):
    async def go():
        with MockProviders() as mock:
            mock.script("plan", sse(plan_json(sub("write notes", done="notes.md exists with a title"))))
            mock.script("boss",
                        sse("", tool_calls=[call("plan_goal")]),
                        lambda body: sse("", tool_calls=[call("assign_job", job_id=re.findall(r"(job_[0-9a-f]{10})", _last_tool(body))[-1], bot_id="bot_w1")]),
                        sse("", tool_calls=[call("wait_for_mention", timeout_seconds=30)]),
                        lambda body: sse("", tool_calls=[call("accept_claim", claim_id=int(re.findall(r"claim #(\d+)", _last_tool(body))[-1]), reason="notes.md exists")]),
                        sse("", tool_calls=[call("submit", result="Notes written and verified.")]),
                        sse("Goal complete: notes written and verified."))
            mock.script("work",
                        sse("", tool_calls=[call("write_file", path="notes.md", content="# Notes\nhello")], usage={"prompt_tokens": 50, "completion_tokens": 10}),
                        sse("", tool_calls=[call("submit_claim", text="notes.md written", evidence=[{"kind": "file", "ref": "notes.md"}])]),
                        sse("done"))
            e = await build(tmp_path, mock)
            await e["reg"].create("Writer", "writer", chain=["work/m"], bot_id="bot_w1")
            res = await e["orch"].run_goal("Write notes")
            board = [m.message_type for m in await query(e["db"], project=res["project_id"], limit=500)]
            log = await e["projects"].log(res["project_id"])
            status = (await e["db"].read_one("SELECT status FROM projects WHERE id=?", (res["project_id"],)))["status"]
            await e["db"].close()
            return res, board, log, status
    res, board, log, status = run(go())
    assert res["status"] == "completed" and status == "done"
    r = res["report"]
    assert "## Result\n\nNotes written and verified." in r and "| write notes | Writer | completed | 1 |" in r
    assert "notes.md written" in r and "file: notes.md" in r and "- `notes.md`" in r
    assert re.search(r"\| Writer \| [\d,]+ \| 3 \|", r) and re.search(r"\| Omi \| [\d,]+ \| 6 \|", r)   # per-bot cost rows
    for t in ("TASK_RECEIVED", "TASK_PLANNED", "TASK_ASSIGNED", "WORK_STARTED", "CLAIM_SUBMITTED", "CLAIM_ACCEPTED", "TASK_COMPLETED", "ARTIFACT_READY"):
        assert t in board, t
    assert log[0]["message"] == "final report"


# ── team controls ──────────────────────────────────────────────────────────
def test_pause_holds_at_the_next_step_and_resume_continues(tmp_path):
    async def go():
        with MockProviders() as mock:
            slow = lambda body: {**sse("", tool_calls=[call("list_dir", path=".")]), "hold": 0.3}
            mock.script("slow", slow, slow, slow, sse("finished"))
            e = await build(tmp_path, mock)
            w = await e["reg"].create("Slow", "worker", chain=["slow/m"])
            job = asyncio.create_task(e["runner"].run(w.id, "list things"))
            while len(mock.requests) < 1:
                await asyncio.sleep(0.02)
            await e["team"].pause()
            await asyncio.sleep(1.0)
            during = len(mock.requests)
            await asyncio.sleep(0.8)
            still = len(mock.requests)
            st_paused = (await e["db"].read_one("SELECT status FROM jobs"))["status"]
            await e["team"].resume()
            out = await asyncio.wait_for(job, 10)
            await e["db"].close()
            return during, still, st_paused, out, len(mock.requests)
    during, still, st_paused, out, total = run(go())
    assert during == still and during <= 2               # nothing new started while paused
    assert st_paused == "paused" and out.status == "completed" and total == 4


def test_stop_interrupts_and_start_resumes_the_goal(tmp_path):
    async def go():
        with MockProviders() as mock:
            mock.default["slow"] = {**sse("", tool_calls=[call("list_dir", path=".")]), "hold": 0.5}
            mock.default["boss"] = sse("Resumed and checked the plan.")
            e = await build(tmp_path, mock)
            w = await e["reg"].create("Slow", "worker", chain=["slow/m"])
            pid = await e["projects"].create("long goal")
            j = await e["graph"].add_job(pid, "long job", assigned_bot_id=w.id)
            e["orch"].goals[pid] = GoalContext(pid, "long goal", e["projects"].folder(pid))
            task = asyncio.create_task(e["runner"].run(w.id, "keep listing", job_id=j.id, project_id=pid))
            e["orch"].goals[pid].worker_tasks[j.id] = task
            while not mock.requests:
                await asyncio.sleep(0.02)
            stopped = await e["team"].stop()
            st = (await e["graph"].get(j.id)).status
            started = await e["team"].start()
            await asyncio.wait_for(e["orch"].boss_tasks[pid], 20)
            after = (await e["graph"].get(j.id)).status
            boss_prompt = next(r["body"]["messages"][-1]["content"] for r in mock.requests if r["provider"] == "boss")
            await e["db"].close()
            return stopped, st, started, after, boss_prompt, task
    stopped, st, started, after, boss_prompt, task = run(go())
    assert stopped["stopped"] and st == "interrupted" and task.done()
    assert started["resumed_projects"] and after == "ready" and "RESUMING after a stop" in boss_prompt


def test_panic_denies_approvals_and_clears_the_seat_line(tmp_path):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            ap = asyncio.create_task(e["approvals"].request(bot_id="b", job_id=None, tool="write_file", risk="R3", summary="x"))
            held = [await e["seats"].acquire(f"h{i}") for i in range(3)]
            waiter = asyncio.create_task(e["seats"].acquire("late"))
            await asyncio.sleep(0.05)
            res = await e["team"].stop(panic=True)
            decision = await ap
            await asyncio.sleep(0.05)
            await e["db"].close()
            return res, decision, waiter.cancelled()
    res, decision, cancelled = run(go())
    assert res["approvals_denied"] == 1 and res["left_seat_line"] == 1
    assert decision.approved is False and cancelled


def test_stall_monitor_nudges_then_tells_omi_then_asks_the_user(tmp_path):
    class FakeAgent:
        paused = False

    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            clock = {"t": 1000.0}
            team = TeamController(db=e["db"], bus=e["bus"], runner=e["runner"], graph=e["graph"], orchestrator=e["orch"],
                                  approvals=e["approvals"], seats=e["seats"], stall_after=60, clock=lambda: clock["t"])
            steered = []
            e["runner"].active["bot_x"] = FakeAgent()
            e["runner"].steer = lambda b, n: steered.append((b, n)) or True
            e["runner"].last_activity["bot_x"] = 1000.0
            stages = []
            for t in (1030, 1065, 1070, 1125, 1185):
                clock["t"] = t
                stages.append(await team.check_stalls())
            msgs = [(m.message_type, m.recipient_id) for m in await query(e["db"])]
            await e["db"].close()
            return stages, steered, msgs
    stages, steered, msgs = run(go())
    assert stages == [[], [("bot_x", 1)], [], [("bot_x", 2)], [("bot_x", 3)]]
    assert steered and "Status check" in steered[0][1]
    assert ("A2A_MESSAGE", "omi") in msgs and ("QUESTION", "user") in msgs


# ── fixes from the live A7.99 run ──────────────────────────────────────────
def test_prompts_carry_todays_date_and_idle_workers_are_flagged(tmp_path):
    from datetime import datetime

    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            prof = await e["reg"].get(BOSS_ID)
            prompt = e["runner"].system_prompt(prof)
            pid = await e["projects"].create("x")
            ctx = GoalContext(pid, "x", e["projects"].folder(pid))
            inbox = await Inbox.open(e["bus"], BOSS_ID)
            kit = BossToolkit(ctx=ctx, db=e["db"], bus=e["bus"], inbox=inbox, registry=e["reg"], runner=e["runner"], graph=e["graph"],
                              projects=e["projects"], ledger=e["ledger"], factory=e["factory"], router=e["router"])
            tools = e["runner"].tools_for(prof, inbox, pid).tools
            t = ToolContext(bot_id=BOSS_ID, workspace=ctx.folder)
            idle = await tools["send_message"].fn({"to": "bot_done", "text": "touch up the file"}, t)
            added = await kit.add_job({"title": "fix the date", "done_criteria": "report.md shows today's date"}, t)
            bad = await kit.add_job({"title": "no criteria"}, t)
            jobs = [j.title for j in await kit._plan_jobs()]
            inbox.close()
            await e["db"].close()
            return prompt, idle, added, bad, jobs
    prompt, idle, added, bad, jobs = run(go())
    assert f"Today's date is {datetime.now():%Y-%m-%d}" in prompt
    assert "not inside a job right now" in idle and "keep reading the board" in idle and "assign_job" in idle
    assert added.startswith("added job_") and "[ready]" in added and bad.startswith("ERROR") and jobs == ["fix the date"]
