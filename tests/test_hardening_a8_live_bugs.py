"""Fixes for the three bugs the A8 live run found:
  A4.a.08  command/test evidence must be a command the bot really ran (real exit code + output)
  A3.a.10  stuck-loop guard (same calls + same results: warn at 3, stop at 5)
  A7.a.11  workers don't outlive their goal (cancelled after the grace period → interrupted)"""

from __future__ import annotations

import asyncio
import json

import pytest

from mock_provider import MockProviders, sse
from test_a7_orchestrator import build, call
from omnibots.board.ledger import EvidenceError, check_evidence, claim_tool
from omnibots.bots.profile import DEFAULT_TOOLS
from omnibots.orchestrator.boss_tools import GoalContext
from omnibots.runtime.agent import LOOP_EXEMPT, LOOP_NOTE, loop_signature
from omnibots.runtime.core_tools import core_registry
from omnibots.runtime.sandbox import Sandbox
from omnibots.runtime.tools import ToolContext


def run(coro):
    return asyncio.run(coro)


# ── A4.a.08 ────────────────────────────────────────────────────────────────
def test_command_evidence_must_be_a_real_run(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "hello.md").write_text("Hello from the OmniBots team!", encoding="utf-8")     # 29 bytes, no newline
    ctx = ToolContext(bot_id="w", workspace=ws, job_id="j1", sandbox=Sandbox(tmp_path / "sb"))
    reg = core_registry()

    # Nothing ran yet: the exact fabrication from the live run is refused.
    with pytest.raises(EvidenceError, match="did not run 'wc -c hello.md'"):
        check_evidence([{"kind": "command", "ref": "wc -c hello.md", "exit_code": 0, "quote": "30 hello.md"}], ws, ctx.runs)

    out = run(reg.run("run_shell", {"command": "python -c \"import os;print(os.path.getsize('hello.md'))\""}, ctx))
    assert "29" in out and ctx.runs and ctx.runs[-1]["exit_code"] == 0
    cmd = ctx.runs[-1]["command"]
    with pytest.raises(EvidenceError, match="claimed exit code 1"):
        check_evidence([{"kind": "command", "ref": cmd, "exit_code": 1}], ws, ctx.runs)
    with pytest.raises(EvidenceError, match="quoted output is not"):
        check_evidence([{"kind": "command", "ref": cmd, "quote": "30"}], ws, ctx.runs)
    items = check_evidence([{"kind": "command", "ref": cmd, "quote": "29"}], ws, ctx.runs)        # exit code filled in
    d = items[0]["detail"]
    assert d["verified"] is True and d["exit_code"] == 0 and "29" in d["output_tail"] and d["ran"] == cmd

    run(reg.run("run_python", {"code": "import sys; print('boom'); sys.exit(3)"}, ctx))
    assert ctx.runs[-1]["command"] == "python <inline code>" and ctx.runs[-1]["exit_code"] == 3
    with pytest.raises(EvidenceError, match="really exited 3"):
        check_evidence([{"kind": "test", "ref": "run_python <inline code>", "exit_code": 0}], ws, ctx.runs)
    # Legacy callers without a run log keep the old rule (exit_code required).
    with pytest.raises(EvidenceError, match="needs its 'exit_code'"):
        check_evidence([{"kind": "command", "ref": "x"}], ws, None)


def test_claim_tool_uses_the_bots_run_log(tmp_path):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            ws = tmp_path / "ws2"
            ws.mkdir()
            ctx = ToolContext(bot_id="w", workspace=ws, job_id=None, sandbox=Sandbox(tmp_path / "sb"))
            tool = claim_tool(e["ledger"])
            fake = await tool.fn({"text": "size is 30", "evidence": [{"kind": "command", "ref": "wc -c hello.md", "exit_code": 0}]}, ctx)
            await core_registry().run("run_shell", {"command": "echo real-output"}, ctx)
            real = await tool.fn({"text": "echo works", "evidence": [{"kind": "command", "ref": "echo real-output", "quote": "real-output"}]}, ctx)
            claims = await e["ledger"].claims(bot_id="w")
            await e["db"].close()
            return fake, real, claims
    fake, real, claims = run(go())
    assert fake.startswith("ERROR: claim not recorded") and "did not run" in fake
    assert real.startswith("claim #")
    assert len(claims) == 1 and claims[0]["evidence"][0]["detail"]["verified"] is True


# ── A3.a.10 ────────────────────────────────────────────────────────────────
def test_loop_signature():
    c = lambda n, a: {"function": {"name": n, "arguments": json.dumps(a)}}
    s1 = loop_signature([(c("write_file", {"path": "a", "content": "x"}), "write_file", "ok")])
    s2 = loop_signature([(c("write_file", {"content": "x", "path": "a"}), "write_file", "ok")])      # key order doesn't matter
    s3 = loop_signature([(c("write_file", {"path": "a", "content": "x"}), "write_file", "different result")])
    assert s1 == s2 and s1 != s3
    assert "wait_for_mention" in LOOP_EXEMPT and loop_signature([(c("wait_for_mention", {}), "wait_for_mention", "no messages")]) is None


def test_a_bot_stuck_in_a_loop_is_warned_then_stopped(tmp_path):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            bot = await e["reg"].create("Loopy", "writer", chain=["work/m"], tools=list(DEFAULT_TOOLS))
            same = sse("", tool_calls=[call("write_file", path="hello.txt", content="Hello!")])
            mock.script("work", *[same] * 8)
            out = await e["runner"].run(bot.id, "write hello.txt ending with a newline")
            reqs = [r for r in mock.requests if r["provider"] == "work"]
            job = await e["db"].read_one("SELECT status, error_message FROM jobs WHERE id=?", (out.job_id,))
            await e["db"].close()
            return out, reqs, dict(job)
    out, reqs, job = run(go())
    assert out.status == "blocked" and job["status"] == "blocked" and "stuck in a loop" in (job["error_message"] or "")
    # step 1 "created", steps 2..6 "overwrote" (identical): stopped at the 5th identical step, not after 8
    assert len(reqs) == 6
    notes = [m for m in reqs[4]["body"]["messages"] if m.get("role") == "user" and m.get("content") == LOOP_NOTE]
    assert notes, "the bot was warned after the 3rd repeat"
    tool_msgs = [m["content"] for m in reqs[1]["body"]["messages"] if m.get("role") == "tool"]
    assert "6 bytes, no trailing newline" in tool_msgs[-1]      # write_file now shows what the worker couldn't see


# ── A7.a.11 ────────────────────────────────────────────────────────────────
def test_leftover_workers_are_stopped_and_interrupted(tmp_path):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            pid = await e["projects"].create("g")
            job = await e["graph"].add_job(pid, "slow work", description="x", done_criteria="y")
            await e["db"].write("UPDATE jobs SET status='running' WHERE id=?", (job.id,))
            ctx = GoalContext(pid, "g", e["projects"].folder(pid))
            ctx.worker_tasks[job.id] = asyncio.create_task(asyncio.sleep(3600))
            done_job = await e["graph"].add_job(pid, "finished", description="x", done_criteria="y")
            await e["db"].write("UPDATE jobs SET status='completed' WHERE id=?", (done_job.id,))
            ctx.worker_tasks[done_job.id] = asyncio.create_task(asyncio.sleep(0))
            await asyncio.sleep(0.01)
            stopped = await e["orch"]._stop_leftover_workers(ctx)
            states = {r["id"]: r["status"] for r in await e["db"].read("SELECT id, status FROM jobs WHERE project_id=?", (pid,))}
            cancelled = ctx.worker_tasks[job.id].cancelled()
            await e["db"].close()
            return stopped, states, cancelled, job.id, done_job.id
    stopped, states, cancelled, jid, did = run(go())
    assert stopped == [jid] and cancelled
    assert states[jid] == "interrupted" and states[did] == "completed"
