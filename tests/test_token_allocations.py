"""A9.c.03 token allocations: work the bots start on their own is allocated per bot, per project,
per day. Omi and work the user starts aren't limited. A bot that runs out asks Omi on the board with
its estimate; Omi puts it to the user as a card; yes adds that much for today and the bot goes on.
Real database, bus and runner; the mock model."""

from __future__ import annotations

import asyncio
import datetime

from mock_provider import MockProviders, sse
from test_a7_orchestrator import build
from test_a9_approvals_budgets import approver
from omnibots.board.query import query
from omnibots.bots.profile import BOSS_ID, DEFAULT_TOOLS
from omnibots.engine import Engine
from omnibots.runtime.approvals import MORE_TOKENS
from omnibots.security.budget import Budget


def run(coro):
    return asyncio.run(coro)


async def setup(e, used: int, *, origin: str = "watch", bot_name: str = "Builder"):
    """A project, a bot, and `used` background tokens it already spent there today."""
    db = e["db"]
    e["runner"].budget = Budget.from_settings(db, {})
    e["runner"].budget.bus = e["bus"]
    bot = await e["reg"].create(bot_name, "coder", chain=["work/m"], tools=list(DEFAULT_TOOLS))
    await db.write("INSERT INTO projects (id, goal, created_by) VALUES ('p1', 'a site', 'user')")
    await db.write("INSERT INTO jobs (id, project_id, title, status, created_by, origin, assigned_bot_id) "
                   "VALUES ('job_earlier', 'p1', 'earlier', 'completed', 'omi', ?, ?)", (origin, bot.id))
    await db.write("INSERT INTO provider_usage_events (provider, model, bot_id, job_id, tokens_in, tokens_out, status_code) "
                   "VALUES ('work','work/m',?,'job_earlier',?,0,200)", (bot.id, used))
    return bot


def work_calls(mock) -> int:
    return len([r for r in mock.requests if r["provider"] == "work"])


def test_a_background_bot_out_of_tokens_asks_omi_and_goes_on_when_approved(tmp_path):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            bot = await setup(e, 250_000)
            mock.script("work", sse('{"tokens": 30000, "why": "the contact page is left."}'), sse("all done"))
            seen: list = []
            ok = asyncio.create_task(approver(e["approvals"], seen, decide=True))
            out = await e["runner"].run(bot.id, "repair the site", project_id="p1", origin="fix")
            ok.cancel()
            alloc = await e["runner"].budget.allocation_today("p1", bot.id)
            asks = [m for m in await query(e["db"], project="p1", limit=50) if m.message_type == "HELP_REQUEST"]
            row = await e["db"].read_one("SELECT day, extra_tokens FROM token_allocations WHERE project_id='p1' AND bot_id=?", (bot.id,))
            calls = work_calls(mock)
            await e["db"].close()
            return bot, out, seen, alloc, asks, dict(row), calls
    bot, out, seen, alloc, asks, row, calls = run(go())
    assert out.status == "completed" and out.result.answer == "all done"
    assert len(asks) == 1 and asks[0].sender_id == bot.id and asks[0].recipient_id == BOSS_ID
    assert "about 30,000 more" in asks[0].text() and "the contact page is left." in asks[0].text()
    assert len(seen) == 1 and seen[0]["tool"] == MORE_TOKENS and seen[0]["bot_id"] == BOSS_ID      # Omi asks you
    assert "Builder" in seen[0]["summary"] and "allocate 30,000 more tokens to Builder" in seen[0]["summary"]
    assert seen[0]["rehearsal"] == {"bot": bot.id, "bot_name": "Builder", "project": "p1",
                                    "used_today": 250_000, "allocated_today": 200_000, "asking_for": 30_000}
    assert alloc == 280_000 and row == {"day": datetime.date.today().isoformat(), "extra_tokens": 80_000}  # overshoot + ask
    assert calls == 2                                        # the estimate + the real step


def test_your_own_work_and_omi_are_not_limited(tmp_path):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            bot = await setup(e, 900_000)
            mock.script("work", sse("did it"))
            seen: list = []
            watcher = asyncio.create_task(approver(e["approvals"], seen, decide=True))
            mine = await e["runner"].run(bot.id, "do what I asked", project_id="p1")              # origin user
            await e["db"].write("INSERT INTO provider_usage_events (provider, model, bot_id, job_id, tokens_in, tokens_out, status_code) "
                                "VALUES ('work','work/m',?,'job_earlier',900000,0,200)", (BOSS_ID,))
            boss = await e["reg"].get(BOSS_ID)
            mock.script(boss.chain[0].split("/")[0], sse("checked"))
            omi = await e["runner"].run(BOSS_ID, "check the project", project_id="p1", origin="watch")
            watcher.cancel()
            await e["db"].close()
            return mine, omi, seen
    mine, omi, seen = run(go())
    assert mine.status == "completed" and omi.status == "completed" and seen == []


def test_denied_the_bot_stops_and_says_why(tmp_path):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            bot = await setup(e, 200_000)
            mock.script("work", sse('{"tokens": 40000, "why": "tests are left."}'), sse("should not run"))
            seen: list = []
            no = asyncio.create_task(approver(e["approvals"], seen, decide=False))
            out = await e["runner"].run(bot.id, "repair", project_id="p1", origin="fix")
            no.cancel()
            alloc = await e["runner"].budget.allocation_today("p1", bot.id)
            calls = work_calls(mock)
            await e["db"].close()
            return out, seen, alloc, calls
    out, seen, alloc, calls = run(go())
    assert out.status == "blocked" and "used up (200,000/200,000)" in (out.result.error or "")
    assert len(seen) == 1 and alloc == 200_000 and calls == 1          # only the estimate ran


def test_no_usable_estimate_falls_back_to_what_the_job_used(tmp_path):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            bot = await setup(e, 200_000)
            mock.script("work", sse("I think maybe a lot?"), sse("done"))
            seen: list = []
            ok = asyncio.create_task(approver(e["approvals"], seen, decide=True))
            out = await e["runner"].run(bot.id, "repair", project_id="p1", origin="fix")
            ok.cancel()
            await e["db"].close()
            return out, seen
    out, seen = run(go())
    assert out.status == "completed"
    assert seen[0]["rehearsal"]["asking_for"] == 50_000 and "no estimate from the model" in seen[0]["summary"]


def test_the_allocations_window_lists_the_project_bots_and_answers_a_waiting_ask(tmp_path):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            bot = await setup(e, 250_000)
            idle = await e["reg"].create("Tester", "tester", chain=["work/m"])
            await e["db"].write("INSERT INTO jobs (id, project_id, title, status, created_by, assigned_bot_id) "
                                "VALUES ('job_t', 'p1', 'tests', 'completed', 'omi', ?)", (idle.id,))
            await e["db"].write("INSERT INTO jobs (id, project_id, title, status, created_by, assigned_bot_id) "
                                "VALUES ('job_o', 'p1', '[goal] a site', 'running', 'user', ?)", (BOSS_ID,))
            eng = type("Eng", (), {})()                 # only what the two engine methods use
            eng.db, eng.approvals, eng.registry, eng.runner, eng.budget = e["db"], e["approvals"], e["reg"], e["runner"], e["runner"].budget
            mock.script("work", sse('{"tokens": 30000, "why": "one page left."}'), sse("all done"))
            task = asyncio.create_task(e["runner"].run(bot.id, "repair", project_id="p1", origin="fix"))
            for _ in range(200):
                await asyncio.sleep(0.02)
                if e["approvals"].list_pending():
                    break
            view = await Engine.ui_allocations(eng)
            total = await Engine.allocate_tokens(eng, "p1", bot.id, 100_000)
            out = await task
            after = await e["runner"].budget.allocation_today("p1", bot.id)
            await e["db"].close()
            return bot, idle, view, total, out, after
    bot, idle, view, total, out, after = run(go())
    (p,) = view["projects"]
    rows = {b["id"]: b for b in p["bots"]}
    assert view["per_bot"] == 200_000 and set(rows) == {bot.id, idle.id}            # Omi isn't listed
    assert 250_000 <= rows[bot.id]["used"] < 251_000 and rows[bot.id]["allocated"] == 200_000 and rows[bot.id]["asking"]
    assert rows[idle.id]["state"] == "idle" and rows[idle.id]["used"] == 0 and not rows[idle.id]["asking"]
    assert total == 300_000 and out.status == "completed" and after == 300_000      # the bot didn't add its 30k again
