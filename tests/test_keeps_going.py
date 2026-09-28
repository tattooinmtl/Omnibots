"""A15.d work keeps going: your Stop is `stopped` (only ▶ Start resumes it); work cut off by a crash or
a quit carries on after a restart; out of time and the step limit hand over to a next round, held by
the leash and capped per day. Real database, graph, orchestrator and the mock model."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from mock_provider import MockProviders, sse
from test_a7_orchestrator import build, call, plan_json, sub

from omnibots.board.query import query
from omnibots.bots.leash import Leash
from omnibots.bots.presence import TeamPresence
from omnibots.bots.profile import BOSS_ID
from omnibots.engine import Engine
from omnibots.projects.graph import MAX_ROUNDS


def run(coro):
    return asyncio.run(coro)


def at_step_limit(answer: str):
    return SimpleNamespace(status="blocked", result=SimpleNamespace(status="max_iterations", answer=answer, error="max steps"))


def test_a_job_at_its_step_limit_carries_on_then_counts_as_a_failure(tmp_path):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            bot = await e["reg"].create("Builder", "coder", chain=["work/m"])
            pid = await e["projects"].create("a site")
            job = await e["graph"].add_job(pid, "build the menu page", description="menu.html with 3 sections", assigned_bot_id=bot.id)
            states = []
            for i in range(MAX_ROUNDS + 1):
                states.append(await e["graph"].continue_or_record(job.id, at_step_limit(f"sections 1..{i + 1} done")))
            j = await e["graph"].get(job.id)
            notes = [m.text() for m in await query(e["db"], limit=50) if "carrying on in round" in m.text()]
            await e["db"].close()
            return states, j, notes
    states, j, notes = run(go())
    assert states[:MAX_ROUNDS] == ["assigned"] * MAX_ROUNDS and states[-1] != "assigned"      # then retry / escalate
    assert j.budget["rounds"] == MAX_ROUNDS and "Where it got to:" in j.description and "sections 1..5 done" in j.description
    assert len(notes) == MAX_ROUNDS


def test_after_a_restart_cut_off_work_carries_on_but_stopped_and_closed_do_not(tmp_path):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            ids = {}
            for name, job_status, proj_status in (("cut", "interrupted", "open"), ("mine", "stopped", "open"),
                                                  ("closed", "interrupted", "cancelled")):
                pid = await e["projects"].create(name)
                await e["projects"].set_status(pid, proj_status)
                job = await e["graph"].add_job(pid, f"{name} job")
                await e["db"].write("UPDATE jobs SET status=? WHERE id=?", (job_status, job.id))
                ids[name] = pid
            asked = []

            async def continue_goal(pid, reason):
                asked.append((pid, reason))
                return "started"
            eng = SimpleNamespace(db=e["db"], orchestrator=SimpleNamespace(continue_goal=continue_goal))
            started = await Engine._carry_on_after_restart(eng, delay=0)
            await e["db"].close()
            return ids, started, asked
    ids, started, asked = run(go())
    assert started == [ids["cut"]] and len(asked) == 1 and "restarted" in asked[0][1]


def test_a_next_round_is_held_by_pause_capped_per_day_and_sees_the_waiting_claims(tmp_path):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            orch, db = e["orch"], e["db"]
            orch.keep_working, orch.max_rounds = True, 1
            orch.leash = await Leash(db).load()
            pid = await e["projects"].create("a site")
            bot = await e["reg"].create("Builder", "coder", chain=["work/m"])
            await e["ledger"].submit(bot_id=bot.id, text="menu.html is done", evidence=[{"kind": "file", "ref": "GOAL.md"}],
                                     project_id=pid, workspace=e["projects"].folder(pid))
            await orch.leash.set_paused(True)
            held = await orch.continue_goal(pid, "results arrived")
            await orch.leash.set_paused(False)
            mock.script("boss", sse("I accepted the menu page."))
            started = await orch.continue_goal(pid, "results arrived")
            again = await orch.continue_goal(pid, "results arrived")               # a round is running
            await orch.boss_tasks[pid]
            capped = await orch.continue_goal(pid, "more results")
            prompt = next(r for r in mock.requests if r["provider"] == "boss")["body"]["messages"][-1]["content"]
            board = [m.text() for m in await query(db, project=pid, limit=50)]
            await db.close()
            return held, started, again, capped, prompt, board
    held, started, again, capped, prompt, board = run(go())
    assert held.startswith("held: background work is paused") and started == "started" and again == "a round is already running"
    assert capped == "round cap reached" and any("I've done 1 rounds" in t for t in board)
    assert "results arrived" in prompt and "claim #1 from" in prompt and "menu.html is done" in prompt
    assert any("Round 1 of 1 today" in t for t in board)


def test_out_of_time_in_the_app_is_a_next_round_not_press_start(tmp_path):
    async def go():
        with MockProviders() as mock:
            mock.script("plan", sse(plan_json(sub("style the page", done="style.css exists"))))
            slow = sse("", tool_calls=[call("list_jobs")])
            slow["hold"] = 20.0
            mock.script("boss", sse("", tool_calls=[call("plan_goal", goal="add a css")]), slow, sse("Carried on and finished."))
            e = await build(tmp_path, mock)
            orch = e["orch"]
            orch.keep_working = True
            out = await asyncio.create_task(orch.run_goal("add a css to the index.html", seconds=8.0))   # its own task, like the app
            nxt = orch.boss_tasks.get(out["project_id"])
            for _ in range(100):                                        # the next round is scheduled after this one ends
                await asyncio.sleep(0.1)
                nxt = orch.boss_tasks.get(out["project_id"])
                if nxt is not None:
                    break
            if nxt is not None:
                await asyncio.wait_for(nxt, 60)
            board = [(m.message_type, m.text()) for m in await query(e["db"], project=out["project_id"], limit=100)]
            await e["db"].close()
            return board
    board = run(go())
    assert not [t for k, t in board if k == "QUESTION" and "ran out of my time" in t]         # no "press Start"
    assert any("carrying on in a new round" in t for _, t in board) and any("Round 1 of 8 today" in t for _, t in board)


def test_a_result_between_rounds_starts_a_round_for_omi(tmp_path):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            asked = []

            async def continue_goal(pid, reason):
                asked.append((pid, reason))
                return "started"
            orch = SimpleNamespace(keep_working=True, boss_tasks={}, goals={}, continue_goal=continue_goal)
            p = TeamPresence(db=e["db"], bus=e["bus"], registry=e["reg"], runner=e["runner"], graph=e["graph"],
                             projects=e["projects"], orchestrator=orch, poll_seconds=0.05)
            msg = SimpleNamespace(message_type="CLAIM_SUBMITTED", recipient_id=BOSS_ID, project_id="p1", sender_type="bot",
                                  sender_id="bot_x", text=lambda: "claim")
            await p._on_message(BOSS_ID, msg)
            await p._rounds_for_omi()
            await p._on_message(BOSS_ID, msg)
            await p._rounds_for_omi()                                     # within the cooldown: not twice
            await e["db"].close()
            return asked
    asked = run(go())
    assert len(asked) == 1 and asked[0][0] == "p1" and "bot_x sent a claim submitted" in asked[0][1]


def test_a_job_carried_on_unblocks_what_depends_on_it(tmp_path):
    """Found by the A14.a.01 live run: the runner marks a job that hit its step limit `blocked`, which blocked its
    dependents; carrying it on must put them back in line, or the report job never runs."""
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            g = e["graph"]
            bot = await e["reg"].create("Researcher", "researcher", chain=["work/m"])
            pid = await e["projects"].create("a report")
            research = await g.add_job(pid, "research", assigned_bot_id=bot.id)
            report = await g.add_job(pid, "write the report", depends_on=[research.id])
            await e["db"].write("UPDATE jobs SET status='blocked', error_message='step budget used up' WHERE id=?", (research.id,))
            await g.refresh(pid)
            before = (await g.get(report.id)).status
            await g.continue_or_record(research.id, at_step_limit("half the facts found"))
            after = (await g.get(report.id)).status
            await e["db"].close()
            return before, after
    before, after = run(go())
    assert before == "blocked" and after == "pending"
