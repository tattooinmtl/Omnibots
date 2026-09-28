"""A3: the bot turn loop, risk gate, steering, sandbox, events, compaction and
critic, driven by a scripted mock provider over real HTTP (no tokens)."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

from mock_provider import MockProviders, sse
from omnibots.db import Database
from omnibots.omni.config import OmniConfig, ProviderInfo
from omnibots.omni.locate import OmniLocation
from omnibots.providers.quota import QuotaManager
from omnibots.providers.router import Router
from omnibots.providers.seats import SeatScheduler
from omnibots.runtime.agent import BotAgent
from omnibots.runtime.approvals import ApprovalCenter, Scope
from omnibots.runtime.context import compact_messages, steering_message, trim_old_tool_results
from omnibots.runtime.core_tools import core_registry
from omnibots.runtime.events import BotEvents, ToolTextFilter
from omnibots.runtime.review import review
from omnibots.runtime.sandbox import Sandbox, scrubbed_env


def call(name, **args):
    return {"name": name, "args": args}


def cfg_for(mock, native=True) -> OmniConfig:
    p = ProviderInfo(name="fake", base_url=mock.url("fake"), api_key="k", key_source="settings", native_tools=native, raw={"reasoningParam": "none"})
    return OmniConfig(location=OmniLocation(Path("."), Path(".")), providers={"fake": p},
                      models={"fake/m": {"provider": "fake", "id": "fake-model", "maxTokens": 512}},
                      default_provider=None, default_model=None, skills=[], mcp_servers={})


class Harness:
    def __init__(self, tmp: Path, mock: MockProviders, *, native=True, db=None, max_iterations=10, listener=None):
        self.cfg = cfg_for(mock, native)
        self.seen = []

        def listen(e):
            self.seen.append(e)
            if listener:
                return listener(e)
        self.approvals = ApprovalCenter(db)
        self.events = BotEvents("bot_t", db, listener=listen)
        self.bot = BotAgent(bot_id="bot_t", name="Tester", role="coder", workspace=tmp / "ws",
                            router=Router(lambda: self.cfg, QuotaManager(), SeatScheduler(), base_delay=0.01),
                            chain=["fake/m"], tools=core_registry(), approvals=self.approvals, events=self.events,
                            sandbox=Sandbox(tmp / "sandbox"), max_iterations=max_iterations)

    def console(self):
        return "\n".join(e["content"] for e in self.seen if e["kind"] == "console")

    def states(self):
        return [e["content"].split(":")[0] for e in self.seen if e["kind"] == "state"]


def test_full_loop_writes_runs_and_reports(tmp_path):
    async def go():
        db = Database(tmp_path / "db.sqlite")
        await db.open()
        with MockProviders() as mock:
            mock.script("fake",
                        sse("", tool_calls=[call("write_file", path="hello.py", content="print('Hello from Omi')\nprint(2+3)\n")]),
                        sse("", tool_calls=[call("run_python", path="hello.py")]),
                        sse("Done. Output:\nHello from Omi\n5"))
            h = Harness(tmp_path, mock, db=db)
            res = await h.bot.run("create and run hello.py")
            reqs = [r["body"] for r in mock.requests]
        rows = await db.read("SELECT kind, content FROM bot_events WHERE bot_id='bot_t' ORDER BY id")
        await db.close()
        return res, h, reqs, rows

    res, h, reqs, rows = asyncio.run(go())
    assert res.status == "done" and res.steps == 3 and res.tool_calls == 2
    assert (tmp_path / "ws" / "hello.py").read_text() == "print('Hello from Omi')\nprint(2+3)\n"
    tool_msgs = [m for m in res.messages if m["role"] == "tool"]
    assert "Hello from Omi\n5" in tool_msgs[1]["content"] and "exit code 0" in tool_msgs[1]["content"]
    assert reqs[2]["messages"][-1]["role"] == "tool"                 # results were fed back
    terminal = [c for k, c in rows if k == "terminal"]
    assert terminal[0] == "$ python hello.py" and "Hello from Omi" in terminal and terminal[-1].startswith("[exit 0")
    assert h.states()[0] == "thinking" and "tool" in h.states() and h.states()[-1] == "done"
    assert "▸ write hello.py" in h.console() and "▸ run_python hello.py" in h.console()


def test_text_protocol_parses_calls_and_recovers_from_a_malformed_one(tmp_path):
    with MockProviders() as mock:
        mock.script("fake",
                    sse("Let me write it.\n<tool_call>\n<function=write_file>\n<parameter=path>a.txt</parameter>\n"),   # broken: no content, cut off
                    sse("<tool_call>\n<function=write_file>\n<parameter=path>a.txt</parameter>\n<parameter=content>hi</parameter>\n</function>\n</tool_call>"),
                    sse("written"))
        h = Harness(tmp_path, mock, native=False)
        res = asyncio.run(h.bot.run("write a.txt"))
        reqs = [r["body"] for r in mock.requests]
    # The half-written call parses (path only) and runs; the tool error goes back to the model.
    assert res.status == "done" and (tmp_path / "ws" / "a.txt").read_text() == "hi"
    assert "# Tool Calling Protocol" in reqs[0]["messages"][0]["content"] and "tools" not in reqs[0]
    assert "<tool_call>" not in h.console()                              # tool syntax never streamed to the console


def test_malformed_text_call_triggers_recovery_message(tmp_path):
    with MockProviders() as mock:
        mock.script("fake", sse("<arg_key>oops</arg_key>"), sse("ok, finished"))
        h = Harness(tmp_path, mock, native=False)
        res = asyncio.run(h.bot.run("do it"))
        second = mock.requests[1]["body"]["messages"]
    assert res.status == "done"
    assert any("Your tool call was malformed" in (m.get("content") or "") for m in second)
    assert "malformed tool call" in h.console()


def test_r3_parks_until_approved_then_runs(tmp_path):
    outside = tmp_path / "users_folder" / "note.txt"

    async def go():
        db = Database(tmp_path / "db.sqlite")
        await db.open()
        with MockProviders() as mock:
            mock.script("fake", sse("", tool_calls=[call("write_file", path=str(outside), content="from the bot")]), sse("saved it"))
            h = Harness(tmp_path, mock, db=db)
            task = asyncio.create_task(h.bot.run("save a note to my folder"))
            for _ in range(500):                              # up to 10 s: wait for the card, not a guess (A15.a.05)
                await asyncio.sleep(0.02)
                if h.approvals.list_pending():
                    break
            await asyncio.sleep(0.2)                          # parked: still nothing written a moment later
            parked = not task.done() and not outside.exists()
            pending = h.approvals.list_pending()
            row = await db.read_one("SELECT risk_class, status, summary FROM approvals")
            assert await h.approvals.decide(pending[0]["id"], True, "user clicked approve")
            res = await asyncio.wait_for(task, 10)
            final = await db.read_one("SELECT status FROM approvals")
        await db.close()
        return parked, pending, dict(row), res, final["status"], h

    parked, pending, row, res, final, h = asyncio.run(go())
    assert parked and pending[0]["risk"] == "R3" and row == {"risk_class": "R3", "status": "pending", "summary": f"write {outside} (12 chars)"}
    assert res.status == "done" and outside.read_text() == "from the bot" and final == "approved"
    assert "waiting_approval" in h.states() and "⏸ waiting for your approval (R3" in h.console()


def test_denied_action_is_not_run_and_the_model_is_told(tmp_path):
    outside = tmp_path / "users_folder" / "x.txt"

    async def go():
        with MockProviders() as mock:
            mock.script("fake", sse("", tool_calls=[call("write_file", path=str(outside), content="nope")]), sse("ok, I won't"))
            h = Harness(tmp_path, mock)
            task = asyncio.create_task(h.bot.run("save it"))
            for _ in range(50):
                await asyncio.sleep(0.05)
                if h.approvals.list_pending():
                    break
            await h.approvals.decide(h.approvals.list_pending()[0]["id"], False)
            return await task
    res = asyncio.run(go())
    assert not outside.exists()
    assert next(m for m in res.messages if m["role"] == "tool")["content"].startswith("DENIED:")


def test_scopes_cover_r3_only_and_are_consumed():
    ac = ApprovalCenter()
    with pytest.raises(ValueError):
        ac.pre_approve(Scope("buy", "R4"))                                  # money always needs a fresh click
    ac.pre_approve(Scope("write_file", "R3", match="C:/sites", remaining=1))

    async def go():
        d1 = await ac.request(bot_id="b", job_id=None, tool="write_file", risk="R3", summary="write C:/sites/index.html")
        t = asyncio.create_task(ac.request(bot_id="b", job_id=None, tool="write_file", risk="R3", summary="write C:/sites/b.html"))
        await asyncio.sleep(0.05)
        still_pending = not t.done()
        t.cancel()
        return d1, still_pending
    d1, still_pending = asyncio.run(go())
    assert d1.approved and d1.reason == "pre-approved scope" and still_pending


def test_steering_mid_run_reaches_the_model(tmp_path):
    with MockProviders() as mock:
        mock.script("fake",
                    sse("", tool_calls=[call("write_file", path="greet.py", content="print('hello')")]),
                    sse("", tool_calls=[call("write_file", path="greet.py", content="print('hello from Omi')")]),
                    sse("done"))
        holder = {}

        def steer_once(e):
            if e["kind"] == "console" and e["content"].startswith("▸ write greet.py") and not holder.get("done"):
                holder["done"] = True
                holder["bot"].steer("actually make it print 'hello from Omi'")
        h = Harness(tmp_path, mock, listener=steer_once)
        holder["bot"] = h.bot
        res = asyncio.run(h.bot.run("write greet.py"))
        second = mock.requests[1]["body"]["messages"]
    assert res.status == "done"
    assert second[-1] == {"role": "user", "content": steering_message("actually make it print 'hello from Omi'")}
    assert "↳ steer received at step 2: actually make it print 'hello from Omi'" in h.console()


def test_step_budget_nudges_then_stops(tmp_path):
    with MockProviders() as mock:
        # a different call each step (identical steps would trip the stuck-loop guard, A3.a.10, before the budget)
        mock.default["fake"] = lambda body: sse("", tool_calls=[call("list_dir", path=f"d{len(body['messages'])}")])
        h = Harness(tmp_path, mock, max_iterations=5)
        res = asyncio.run(h.bot.run("loop forever"))
        bodies = [r["body"]["messages"] for r in mock.requests]
    assert res.status == "max_iterations" and len(bodies) == 5
    assert any("Only 3 tool iterations remain" in (m.get("content") or "") for m in bodies[2])
    assert h.states()[-1] == "blocked"


def test_cancel_kills_the_sandboxed_process_tree(tmp_path):
    code = "import subprocess, sys, time\nsubprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\nprint('started', flush=True)\ntime.sleep(60)\n"

    async def go():
        with MockProviders() as mock:
            mock.script("fake", sse("", tool_calls=[call("run_python", code=code)]))
            h = Harness(tmp_path, mock)
            task = asyncio.create_task(h.bot.run("run a long script"))
            for _ in range(100):
                await asyncio.sleep(0.1)
                if any(e["kind"] == "terminal" and e["content"] == "started" for e in h.seen):
                    break
            t0 = time.monotonic()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return time.monotonic() - t0, h
    elapsed, h = asyncio.run(go())
    assert elapsed < 10 and h.states()[-1] == "stopped"


def test_sandbox_hides_secrets_and_enforces_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNI_MINIMAX_IO_KEY", "sk-should-not-leak")
    monkeypatch.setenv("MY_API_TOKEN", "tok-should-not-leak")
    assert "OMNI_MINIMAX_IO_KEY" not in scrubbed_env() and "MY_API_TOKEN" not in scrubbed_env()

    async def go():
        sb = Sandbox(tmp_path / "sb")
        d = sb.new_run_dir("b")
        (d / "env.py").write_text("import os; print(sorted(os.environ))", encoding="utf-8")
        r1 = await sb.run([sys.executable, str(d / "env.py")], cwd=d)
        (d / "slow.py").write_text("import time; time.sleep(30)", encoding="utf-8")
        r2 = await sb.run([sys.executable, str(d / "slow.py")], cwd=d, timeout=1)
        return r1, r2
    r1, r2 = asyncio.run(go())
    assert "sk-should-not-leak" not in r1.output and "MY_API_TOKEN" not in r1.output and "OMNIBOTS_SANDBOX" in r1.output
    assert r2.timed_out and r2.seconds < 10


def test_compaction_keeps_goal_and_never_orphans_tool_results():
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "build the thing"}]
    for i in range(12):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}", "function": {"name": "read_file", "arguments": json.dumps({"path": f"f{i}.py"})}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": f"contents {i}"})
    assert compact_messages(msgs, session_goal="build the thing")
    assert msgs[0]["role"] == "system" and msgs[1]["content"].startswith("[CONTEXT COMPACTED]")
    assert "Session goal: build the thing" in msgs[1]["content"] and "read_file (f0.py) -> contents 0" in msgs[1]["content"]
    assert msgs[2]["role"] != "tool"
    big = [{"role": "assistant", "content": "a"}, {"role": "tool", "tool_call_id": "x", "content": "y" * 5000},
           {"role": "assistant", "content": "b"}, {"role": "assistant", "content": "c"}, {"role": "tool", "tool_call_id": "z", "content": "w" * 5000},
           {"role": "assistant", "content": "d"}]                                    # the x result is now 3 turns old
    view = trim_old_tool_results(big)
    assert len(view[1]["content"]) < 2200 and len(view[4]["content"]) == 5000 and len(big[1]["content"]) == 5000


def test_stream_events_are_coalesced_and_listener_errors_are_contained(tmp_path):
    async def go():
        db = Database(tmp_path / "db.sqlite")
        await db.open()
        ev = BotEvents("b", db, listener=lambda e: 1 / 0)          # a broken UI
        for t in "the quick brown fox jumps over the lazy dog".split():
            await ev.emit("console", t + " ", stream=True)
        await ev.emit("state", "done")
        rows = await db.read("SELECT kind, content FROM bot_events")
        await db.close()
        return rows
    rows = asyncio.run(go())
    assert [(r["kind"], r["content"]) for r in rows] == [("console", "the quick brown fox jumps over the lazy dog "), ("state", "done")]


def test_tool_text_filter_hides_protocol_split_across_chunks():
    f = ToolTextFilter()
    out = "".join(f.feed(c) for c in ["I'll write it now. <too", "l_call>\n<function=write_file>", "..."]) + f.flush()
    assert out == "I'll write it now. "


def test_critic_returns_verdict_and_findings(tmp_path):
    with MockProviders() as mock:
        mock.script("fake", sse("[MAJOR] hello.py:1 - prints the wrong text - print 'Hello from Omi'\nVERDICT: FAIL"))
        cfg = cfg_for(mock)
        router = Router(lambda: cfg, QuotaManager(), SeatScheduler())
        r = asyncio.run(review(router, reviewer_id="rev", chain=["fake/m"], task="print Hello from Omi", changes="=== hello.py ===\nprint('hi')"))
        sent = mock.requests[0]["body"]["messages"]
    assert r.verdict == "FAIL" and r.findings == ["[MAJOR] hello.py:1 - prints the wrong text - print 'Hello from Omi'"]
    assert "not how they got there" in sent[0]["content"] and "print('hi')" in sent[1]["content"]
