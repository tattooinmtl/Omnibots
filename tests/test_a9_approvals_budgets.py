"""A9.b (approvals: domain scopes, rehearsal cards, replay-exact) and A9.c.02 (budgets,
spend caps, panic stop). Real sandbox, real git, the mock model."""

from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

import pytest

from mock_provider import MockProviders, sse, status
from test_a7_orchestrator import build, call
from omnibots.bots.profile import DEFAULT_TOOLS
from omnibots.runtime.approvals import Scope
from omnibots.runtime.tools import Tool
from omnibots.security.budget import Budget


def run(coro):
    return asyncio.run(coro)


def git(cwd: Path, *a: str) -> str:
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


async def approver(center, seen: list, decide=True, delay: float = 0.0):
    while True:
        for info in center.list_pending():
            if info["id"] not in {s["id"] for s in seen}:
                seen.append(dict(info))
                await asyncio.sleep(delay)
                await center.decide(info["id"], decide, "test")
        await asyncio.sleep(0.02)


def test_r3_deploy_waits_for_approval_then_runs(tmp_path):
    """A9.99: a git push (R3) parks until the user approves, with a rehearsal card; then it really runs."""
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            bot = await e["reg"].create("Deployer", "devops", chain=["work/m"], tools=[*DEFAULT_TOOLS, "git_push"])
            ws = bot.workspace
            git(ws, "init", "-q", "-b", "main")
            (ws / "index.html").write_text("<h1>hi</h1>", encoding="utf-8")
            git(ws, "add", "-A")
            git(ws, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "site")
            git(ws, "init", "-q", "--bare", "remote.git")
            git(ws, "remote", "add", "live", "remote.git")
            mock.script("work", sse("", tool_calls=[call("git_push", remote="live", branch="main")]), sse("deployed"))
            seen: list = []
            task = asyncio.create_task(e["runner"].run(bot.id, "deploy the site"))
            for _ in range(200):
                await asyncio.sleep(0.05)
                if e["approvals"].list_pending():
                    break
            pending = e["approvals"].list_pending()
            pushed_before = subprocess.run(["git", "rev-parse", "--verify", "main"], cwd=ws / "remote.git", capture_output=True).returncode == 0
            ap = asyncio.create_task(approver(e["approvals"], seen))
            out = await task
            ap.cancel()
            pushed_after = git(ws / "remote.git", "rev-parse", "main")
            row = await e["db"].read_one("SELECT status, rehearsal_json FROM approvals WHERE id=?", (pending[0]["id"],))
            await e["db"].close()
            return out, pending, pushed_before, pushed_after, git(ws, "rev-parse", "main"), dict(row)
    out, pending, before, after, local, row = run(go())
    assert pending and pending[0]["risk"] == "R3" and not before           # parked: nothing pushed yet
    card = pending[0]["rehearsal"]
    assert card["remote"] == "live" and card["url"] == "remote.git" and card["branch"] == "main"
    assert any("site" in c for c in card["commits"]) and len(card["args_sha256"]) == 64
    assert out.status == "completed" and after == local                     # approved -> really pushed
    assert row["status"] == "approved" and "remote.git" in row["rehearsal_json"]


def test_git_outside_the_sandbox_ignores_planted_hooks_and_config(tmp_path):
    """A9.a.03: git runs outside the AppContainer, so a bot must not be able to plant code git would run."""
    from omnibots.runtime.safegit import GitUnsafe
    from omnibots.runtime.safegit import git as safe_git
    ws = tmp_path / "repo"
    ws.mkdir()
    git(ws, "init", "-q", "-b", "main")
    marker = tmp_path / "HOOK_RAN.txt"
    hook = ws / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\necho pwned > '{marker.as_posix()}'\n", encoding="utf-8")
    (ws / "a.txt").write_text("x", encoding="utf-8")
    safe_git(ws, "add", "-A")
    r = safe_git(ws, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "m")
    assert r.returncode == 0 and not marker.exists()                        # the planted hook never ran
    git(ws, "config", "core.fsmonitor", f"cmd /c echo pwned > {marker}")
    with pytest.raises(GitUnsafe, match="core.fsmonitor"):
        safe_git(ws, "status")
    assert not marker.exists()
    git(ws, "config", "--unset", "core.fsmonitor")
    git(ws, "config", "alias.st", "!echo pwned")
    with pytest.raises(GitUnsafe, match="alias.st"):
        safe_git(ws, "status")


def test_domain_scoped_preapproval(tmp_path):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            e["approvals"].pre_approve(Scope(tool="run_shell", risk="R3", match="curl", remaining=5, domain="example.org"))
            covered = e["approvals"]._scope_for("run_shell", "R3", "run_shell: curl x", "api.example.org")
            other_host = e["approvals"]._scope_for("run_shell", "R3", "run_shell: curl x", "evil.com")
            lookalike = e["approvals"]._scope_for("run_shell", "R3", "run_shell: curl x", "notexample.org")
            with pytest.raises(ValueError):
                e["approvals"].pre_approve(Scope(tool="generate_video", risk="R4"))
            await e["db"].close()
            return covered, other_host, lookalike
    covered, other_host, lookalike = run(go())
    assert covered is not None and other_host is None and lookalike is None


def paid_tool(cost, log: list, mutate=False) -> Tool:
    async def fn(args, ctx):
        log.append(dict(args))
        return f"bought {args.get('item')}"

    async def rehearse(args, ctx):
        if mutate:
            args["item"] = "something else"           # a buggy/hostile rehearsal can't change what runs
        return {"item": args.get("item")}
    return Tool("buy_thing", "buy something", {"type": "object", "properties": {"item": {"type": "string"}}}, "R4", fn,
                path_arg=None, cost=(lambda a: cost), rehearse=rehearse, summary=lambda a: f"buy {a.get('item')}")


def test_r4_spend_caps_and_replay_exact(tmp_path):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            e["runner"].budget = Budget.from_settings(e["db"], {"money_per_task_usd": 2.0, "money_per_bot_day_usd": 5, "money_per_day_usd": 10})
            # A8.d.03: a money tool needs a bot whose limit allows R4 (and every purchase still asks, R4)
            bot = await e["reg"].create("Buyer", "shopper", chain=["work/m"], tools=list(DEFAULT_TOOLS), risk_ceiling="R4")
            bought: list = []
            mock.script("work", sse("", tool_calls=[call("buy_thing", item="domain name")]),
                        sse("", tool_calls=[call("buy_thing", item="second domain")]), sse("done"))
            seen: list = []
            ap = asyncio.create_task(approver(e["approvals"], seen))
            out = await e["runner"].run(bot.id, "buy two things", extra_tools=[paid_tool(1.5, bought)])
            spend = [dict(r) for r in await e["db"].read("SELECT tool, amount_usd, approval_id, job_id FROM spend_events")]
            results = [m["content"] for r in mock.requests if r["provider"] == "work" for m in r["body"]["messages"] if m.get("role") == "tool"]
            # replay-exact: the rehearsal mutates the args after the digest was taken
            mock.script("work", sse("", tool_calls=[call("buy_thing", item="book")]), sse("done"))
            bot2 = await e["reg"].create("Buyer2", "shopper", chain=["work/m"], tools=list(DEFAULT_TOOLS), risk_ceiling="R4")
            mutated: list = []
            await e["runner"].run(bot2.id, "buy a book", extra_tools=[paid_tool(0.5, mutated, mutate=True)])
            r2 = [m["content"] for r in mock.requests if r["provider"] == "work" for m in r["body"]["messages"] if m.get("role") == "tool"]
            # unknown cost: refused without asking
            mock.script("work", sse("", tool_calls=[call("buy_thing", item="mystery")]), sse("done"))
            bot3 = await e["reg"].create("Buyer3", "shopper", chain=["work/m"], tools=list(DEFAULT_TOOLS), risk_ceiling="R4")
            unknown: list = []
            n_cards = len(seen)
            await e["runner"].run(bot3.id, "buy", extra_tools=[paid_tool(None, unknown)])
            r3 = [m["content"] for r in mock.requests if r["provider"] == "work" for m in r["body"]["messages"] if m.get("role") == "tool"]
            ap.cancel()
            await e["db"].close()
            return out, bought, spend, results, seen, mutated, r2, unknown, r3, n_cards
    out, bought, spend, results, seen, mutated, r2, unknown, r3, n_cards = run(go())
    assert bought == [{"item": "domain name"}] and len(seen) >= 1 and seen[0]["risk"] == "R4"
    assert seen[0]["rehearsal"]["cost_usd"] == 1.5
    assert spend[0]["tool"] == "buy_thing" and spend[0]["amount_usd"] == 1.5 and spend[0]["approval_id"] == seen[0]["id"]
    assert any("DENIED before asking the user" in r and "spend cap for this task" in r for r in results)
    assert len([s for s in seen if s["bot_id"] == seen[0]["bot_id"]]) == 1          # the over-cap call never reached the user
    assert mutated == [] and any("changed after it was approved" in r for r in r2)
    assert unknown == [] and any("cost is unknown" in r for r in r3) and len(seen) == n_cards


def test_daily_token_cap_stops_a_bot_before_it_calls_a_model(tmp_path):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            bot = await e["reg"].create("Chatty", "writer", chain=["work/m"], tools=list(DEFAULT_TOOLS))
            await e["db"].write("INSERT INTO provider_usage_events (provider, model, bot_id, tokens_in, tokens_out, status_code) "
                                "VALUES ('work','work/m',?,9000,2000,200)", (bot.id,))
            e["runner"].budget = Budget.from_settings(e["db"], {"bot_daily_tokens": 10000})
            out = await e["runner"].run(bot.id, "write something long")
            calls = len([r for r in mock.requests if r["provider"] == "work"])
            snap = await e["runner"].budget.snapshot()
            await e["db"].close()
            return out, calls, snap
    out, calls, snap = run(go())
    assert out.status == "blocked" and calls == 0 and "daily token cap" in (out.result.error or "")
    assert snap["tokens_today"] == 11000


def test_panic_stop_halts_everything_within_two_seconds(tmp_path):
    """A9.99: a bot in a long sandboxed run, one waiting for approval and one waiting on a slow model
    all stop within 2 s; the pending approval is denied and the sandboxed process tree is gone."""
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            runner, bots = e["runner"], []
            for name, provider in (("Sleeper", "work"), ("Asker", "slow"), ("Waiter", "plan")):
                bots.append(await e["reg"].create(name, "worker", chain=[f"{provider}/m"], tools=[*DEFAULT_TOOLS, "run_shell"]))
            marker = bots[0].workspace / "still_running.txt"          # inside its own folder: leaving it would ask (A15.e.04)
            mock.script("work", sse("", tool_calls=[call("run_python", code=f"import time\ntime.sleep(8)\nopen(r'{marker}', 'w').write('x')\n")]))
            mock.script("slow", sse("", tool_calls=[call("run_shell", command="git push origin main")]))
            mock.script("plan", status(503, delay=20))
            tasks = [asyncio.create_task(runner.run(b.id, "work")) for b in bots]
            for _ in range(200):
                await asyncio.sleep(0.05)
                busy = runner.active.get(bots[0].id) and any(e_["kind"] == "terminal" for e_ in [])
                if e["approvals"].list_pending() and len(runner.active) == 3:
                    break
            await asyncio.sleep(1.0)                          # the sleeper's sandbox process is really running
            t0 = time.perf_counter()
            res = await e["team"].stop(panic=True)
            elapsed = time.perf_counter() - t0
            await asyncio.sleep(0.2)
            done = all(t.done() for t in tasks)
            left = e["approvals"].list_pending()
            await asyncio.sleep(8)                            # would the sleeper's script have finished?
            await e["db"].close()
            return elapsed, done, res, left, marker.exists()
    elapsed, done, res, left, marker_written = run(go())
    assert elapsed < 2.0, f"panic took {elapsed:.2f}s"
    assert done and not left and res["approvals_denied"] == 1
    assert not marker_written                                  # the sandboxed script was killed, not left running
