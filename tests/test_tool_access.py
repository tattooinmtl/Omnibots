"""A8.d tool access (user decision 2026-09-27: keep the tool rule). Search by default; the tool relay (a bot
asks, Omi routes, a holder runs just that tool, the answer comes back); risk ceilings enforced on a tool's
base risk. Real runner, sandbox, board and database; the mock model."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from mock_provider import MockProviders, sse
from test_a7_orchestrator import build, call

from omnibots.board.query import query
from omnibots.bots.profile import BOSS_ID, BOSS_TOOLS, DEFAULT_TOOLS
from omnibots.engine import Engine
from omnibots.orchestrator.relay import MAX_PER_JOB, RelayDesk


def run(coro):
    return asyncio.run(coro)


def test_search_is_a_default_sense_for_new_bots_and_omi(tmp_path):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            await e["db"].write("DELETE FROM bot_tools WHERE bot_id=? AND tool_id IN ('web_search','web_fetch')", (BOSS_ID,))
            omi = await e["reg"].ensure_boss()                     # an older Omi gets search too
            bot, _ = await e["factory"].create(name="Researcher", role="researcher", tools=None)
            await e["db"].close()
            return omi, bot
    omi, bot = run(go())
    assert {"web_search", "web_fetch"} <= set(DEFAULT_TOOLS) and {"web_search", "web_fetch"} <= set(BOSS_TOOLS)
    assert {"web_search", "web_fetch"} <= set(omi.tools) and {"web_search", "web_fetch"} <= set(bot.tools)
    assert "run_shell" not in bot.tools and "git_push" not in bot.tools                  # still not every tool


def test_the_relay_runs_the_tool_on_a_holder_and_the_answer_comes_back(tmp_path):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            desk = RelayDesk(db=e["db"], bus=e["bus"], registry=e["reg"], runner=e["runner"], projects=e["projects"], home=tmp_path)
            e["runner"].relay = desk
            asker = await e["reg"].create("Writer", "writer", chain=["work/m"], tools=list(DEFAULT_TOOLS))
            holder = await e["reg"].create("Shell", "devops", chain=["plan/m"], tools=[*DEFAULT_TOOLS, "run_shell"], risk_ceiling="R3")
            pid = await e["projects"].create("a site")
            folder = e["projects"].folder(pid)
            mock.script("work", sse("", tool_calls=[call("request_tool", tool="run_shell", args={"command": "echo relayed-ok"},
                                                          why="I need the shell output")]), sse("Got it: relayed-ok."))
            mock.script("plan", sse("", tool_calls=[call("run_shell", command="echo relayed-ok")]), sse("It printed relayed-ok."))
            task = asyncio.create_task(e["runner"].run(asker.id, "check the shell", project_id=pid, workspace=folder))
            for _ in range(300):
                await asyncio.sleep(0.02)
                if desk.waiting(pid):
                    break
            (req,) = desk.waiting(pid)
            relayed = await desk.relay(req["id"], holder.id)                         # what Omi does with relay_tool
            out = await asyncio.wait_for(task, 60)
            await asyncio.wait_for(desk.jobs[req["id"]], 60)
            asker_tool_msgs = [m["content"] for r in mock.requests if r["provider"] == "work" for m in r["body"]["messages"] if m.get("role") == "tool"]
            holder_tools = {t["function"]["name"] for t in next(r for r in mock.requests if r["provider"] == "plan")["body"].get("tools", [])}
            board = [m.message_type for m in await query(e["db"], limit=100)]
            origin = (await e["db"].read_one("SELECT origin FROM jobs WHERE assigned_bot_id=?", (holder.id,)))["origin"]
            await e["db"].close()
            return relayed, out, asker_tool_msgs, holder_tools, board, origin, holder
    relayed, out, msgs, holder_tools, board, origin, holder = run(go())
    assert relayed.startswith("relayed") and out.status == "completed"
    assert any("run_shell result (run by" in m and "relayed-ok" in m for m in msgs)
    assert holder_tools == {"run_shell"}                                           # only that tool: no chains, nothing else
    assert "TOOL_REQUEST" in board and "TOOL_RESULT" in board and origin == "relay"


def test_the_relays_limits(tmp_path):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            desk = RelayDesk(db=e["db"], bus=e["bus"], registry=e["reg"], runner=e["runner"], wait_seconds=0.3)
            asker = await e["reg"].create("Writer", "writer", chain=["work/m"], tools=list(DEFAULT_TOOLS))
            ask = lambda tool, job="job_1": desk.request(bot_id=asker.id, job_id=job, project_id=None, tool=tool, args={}, why="x")
            boss_only, mine, unknown = await ask("assign_job"), await ask("web_fetch"), await ask("teleport")
            timed_out = await ask("run_shell")                                     # nobody answers within 0.3 s
            task = asyncio.create_task(ask("git_push", job="job_2"))
            for _ in range(100):
                await asyncio.sleep(0.01)
                if desk.waiting():
                    break
            declined_id = desk.waiting()[0]["id"]
            await desk.decline(declined_id, "not needed for this job")
            declined = await task
            desk._per_job["job_3"] = MAX_PER_JOB
            capped = await ask("run_shell", job="job_3")
            await e["db"].close()
            return boss_only, mine, unknown, timed_out, declined, capped
    boss_only, mine, unknown, timed_out, declined, capped = run(go())
    assert "can't be relayed" in boss_only and "you have web_fetch yourself" in mine and "no tool called" in unknown
    assert timed_out.startswith("no answer") and "declined by Omi: not needed" in declined and "already asked" in capped


def test_a_bot_above_its_ceiling_is_refused_and_existing_bots_keep_their_tools(tmp_path):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            low = await e["reg"].create("Careful", "writer", chain=["work/m"], tools=list(DEFAULT_TOOLS), risk_ceiling="R1")
            mock.script("work", sse("", tool_calls=[call("web_search", query="bakeries")]), sse("done"))
            out = await e["runner"].run(low.id, "look it up")
            refused = [m["content"] for m in mock.requests[-1]["body"]["messages"] if m.get("role") == "tool"][0]
            old = await e["reg"].create("Old", "devops", chain=["work/m"], tools=[*DEFAULT_TOOLS, "git_push"], risk_ceiling="R2")
            eng = SimpleNamespace(db=e["db"], registry=e["reg"], runner=e["runner"])
            eng._tool_risk = lambda n: Engine._tool_risk(eng, n)
            raised = await Engine._fit_ceilings(eng)
            after = (await e["reg"].get(old.id)).risk_ceiling
            await e["db"].close()
            return out, refused, raised, after, old
    out, refused, raised, after, old = run(go())
    assert out.status == "completed" and refused.startswith("REFUSED: web_search is R2, above your limit (R1)")
    assert after == "R3" and any(old.id in r for r in raised)                      # git_push is R3: kept usable


def test_omis_inbox_hears_a_tool_request(tmp_path):
    """Found live in A14.a.02: TOOL_REQUEST wasn't among the inbox types, so Omi waiting in wait_for_mention never
    saw it and the asking bot timed out."""
    from omnibots.board.a2a import Inbox

    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            desk = RelayDesk(db=e["db"], bus=e["bus"], registry=e["reg"], runner=e["runner"], wait_seconds=5)
            asker = await e["reg"].create("Tester", "tester", chain=["work/m"], tools=list(DEFAULT_TOOLS))
            inbox = await Inbox.open(e["bus"], BOSS_ID)
            ask = asyncio.create_task(desk.request(bot_id=asker.id, job_id="j1", project_id="p1", tool="run_shell",
                                                   args={"command": "python -m unittest"}, why="run the tests"))
            heard = await inbox.sub.get(timeout=3)
            await desk.decline(desk.waiting()[0]["id"], "test over")
            await ask
            inbox.close()
            await e["db"].close()
            return heard
    heard = run(go())
    assert heard is not None and heard.message_type == "TOOL_REQUEST" and "run_shell" in heard.text()


def test_omi_can_see_the_request_id_and_a_wrong_id_names_the_open_ones(tmp_path):
    """Found live in A14.a.02: the TOOL_REQUEST text had no id, so Omi answered 'the tool request above' and the asking
    bot waited 10 minutes for nothing."""
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            desk = RelayDesk(db=e["db"], bus=e["bus"], registry=e["reg"], runner=e["runner"], wait_seconds=5)
            asker = await e["reg"].create("Tester", "tester", chain=["work/m"], tools=list(DEFAULT_TOOLS))
            ask = asyncio.create_task(desk.request(bot_id=asker.id, job_id="j1", project_id=None, tool="run_shell",
                                                   args={"command": "ver"}, why="check the OS"))
            for _ in range(100):
                await asyncio.sleep(0.01)
                if desk.waiting():
                    break
            rid = desk.waiting()[0]["id"]
            text = [m.text() for m in await query(e["db"], types=["TOOL_REQUEST"])][0]
            wrong = await desk.answer("the tool request above", "Microsoft Windows")
            right = await desk.answer(rid, "Microsoft Windows")
            got = await ask
            none_open = await desk.answer("tr_gone", "x")
            await e["db"].close()
            return rid, text, wrong, right, got, none_open
    rid, text, wrong, right, got, none_open = run(go())
    assert rid in text
    assert wrong.startswith("ERROR") and rid in wrong and "run_shell" in wrong
    assert right == f"answered {rid}" and "Microsoft Windows" in got
    assert "None are open" in none_open


def test_a_relayed_run_counts_as_evidence_but_a_typed_answer_does_not(tmp_path):
    """Found live in A14.a.06: the tests bot's tests were run through the relay, then its claim was rejected twice
    ("you did not run it in this job") and the job blocked."""
    from omnibots.board.ledger import EvidenceError, check_evidence

    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            desk = RelayDesk(db=e["db"], bus=e["bus"], registry=e["reg"], runner=e["runner"], projects=e["projects"], home=tmp_path)
            asker = await e["reg"].create("Tester", "tester", chain=["work/m"], tools=list(DEFAULT_TOOLS))
            holder = await e["reg"].create("Shell", "devops", chain=["plan/m"], tools=[*DEFAULT_TOOLS, "run_shell"], risk_ceiling="R3")
            pid = await e["projects"].create("a site")
            mock.script("plan", sse("", tool_calls=[call("run_shell", command="echo relayed-ok")]), sse("It printed relayed-ok."))
            relayed_runs, typed_runs = [], []
            ask = asyncio.create_task(desk.request(bot_id=asker.id, job_id="j1", project_id=pid, tool="run_shell",
                                                   args={"command": "echo relayed-ok"}, why="evidence", record=relayed_runs.append))
            for _ in range(300):
                await asyncio.sleep(0.02)
                if desk.waiting(pid):
                    break
            await desk.relay(desk.waiting(pid)[0]["id"], holder.id)
            await asyncio.wait_for(ask, 60)
            ask2 = asyncio.create_task(desk.request(bot_id=asker.id, job_id="j2", project_id=pid, tool="run_shell",
                                                    args={"command": "echo typed"}, why="evidence", record=typed_runs.append))
            for _ in range(300):
                await asyncio.sleep(0.02)
                if desk.waiting(pid):
                    break
            await desk.answer(desk.waiting(pid)[0]["id"], "EXIT 0\ntyped")          # Omi's word, not a run
            await ask2
            await e["db"].close()
            return relayed_runs, typed_runs, holder
    relayed, typed, holder = run(go())
    assert relayed and relayed[0]["relayed_by"] == holder.id and relayed[0]["exit_code"] == 0 and "relayed-ok" in relayed[0]["output"]
    (ok,) = check_evidence([{"kind": "test", "ref": "echo relayed-ok", "exit_code": 0}], None, relayed)
    assert ok["detail"]["verified"] is True
    assert typed == []
    try:
        check_evidence([{"kind": "test", "ref": "echo typed", "exit_code": 0}], None, typed)
        raise AssertionError("a typed answer must not count as a run")
    except EvidenceError:
        pass
