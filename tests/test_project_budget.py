"""A9.c.03: each project has a daily token cap that isn't a wall. When it's used up the bot
estimates what finishing needs and asks; Approve adds that much for today and the bot goes on,
Deny stops it. Real database and runner, the mock model."""

from __future__ import annotations

import asyncio
import datetime
import json

from mock_provider import MockProviders, sse
from test_a7_orchestrator import build
from test_a9_approvals_budgets import approver
from omnibots.bots.profile import DEFAULT_TOOLS
from omnibots.runtime.approvals import MORE_TOKENS
from omnibots.security.budget import Budget


def run(coro):
    return asyncio.run(coro)


async def spent(db, project_id: str, tokens: int) -> None:
    """Earlier work on the project today: a job and its token use."""
    await db.write("INSERT INTO projects (id, goal, created_by) VALUES (?, 'a site', 'user')", (project_id,))
    await db.write("INSERT INTO jobs (id, project_id, title, status, created_by) VALUES ('job_earlier', ?, 'earlier', 'done', 'user')",
                   (project_id,))
    await db.write("INSERT INTO provider_usage_events (provider, model, bot_id, job_id, tokens_in, tokens_out, status_code) "
                   "VALUES ('work','work/m','x','job_earlier',?,0,200)", (tokens,))


def work_calls(mock) -> int:
    return len([r for r in mock.requests if r["provider"] == "work"])


def test_over_the_cap_the_bot_asks_with_its_estimate_and_goes_on_when_approved(tmp_path):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            db = e["db"]
            e["runner"].budget = Budget.from_settings(db, {})               # the default: 200k a project a day
            await spent(db, "p1", 250_000)
            bot = await e["reg"].create("Builder", "coder", chain=["work/m"], tools=list(DEFAULT_TOOLS))
            mock.script("work", sse('{"tokens": 30000, "why": "the contact page is left."}'), sse("all done"))
            seen: list = []
            ok = asyncio.create_task(approver(e["approvals"], seen, decide=True))
            out = await e["runner"].run(bot.id, "finish the site", project_id="p1")
            ok.cancel()
            cap = await e["runner"].budget.project_cap_today("p1")
            used = await e["runner"].budget.project_tokens_today("p1")
            row = await db.read_one("SELECT budget_json FROM projects WHERE id='p1'")
            calls = work_calls(mock)
            await db.close()
            return out, seen, cap, used, json.loads(row["budget_json"]), calls
    out, seen, cap, used, budget_json, calls = run(go())
    assert out.status == "completed" and out.result.answer == "all done"
    assert len(seen) == 1 and seen[0]["tool"] == MORE_TOKENS
    assert "about 30,000 more" in seen[0]["summary"] and "the contact page is left." in seen[0]["summary"]
    assert seen[0]["rehearsal"] == {"used_today": 250_000, "cap_today": 200_000, "asking_for": 30_000}
    assert cap == 280_000                           # the 50k overshoot + the 30k asked for: 30k really left to spend
    assert list(budget_json["extra_tokens"]) == [datetime.date.today().isoformat()]
    assert calls == 2                               # the estimate + the real step


def test_denied_the_bot_stops_and_says_why(tmp_path):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            e["runner"].budget = Budget.from_settings(e["db"], {})
            await spent(e["db"], "p1", 200_000)
            bot = await e["reg"].create("Builder", "coder", chain=["work/m"], tools=list(DEFAULT_TOOLS))
            mock.script("work", sse('{"tokens": 40000, "why": "tests are left."}'), sse("should not run"))
            seen: list = []
            no = asyncio.create_task(approver(e["approvals"], seen, decide=False))
            out = await e["runner"].run(bot.id, "finish the site", project_id="p1")
            no.cancel()
            cap = await e["runner"].budget.project_cap_today("p1")
            calls = work_calls(mock)
            await e["db"].close()
            return out, seen, cap, calls
    out, seen, cap, calls = run(go())
    assert out.status == "blocked" and "used up (200,000/200,000)" in (out.result.error or "")
    assert len(seen) == 1 and cap == 200_000 and calls == 1          # only the estimate ran


def test_no_usable_estimate_falls_back_to_what_the_job_used(tmp_path):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            e["runner"].budget = Budget.from_settings(e["db"], {})
            await spent(e["db"], "p1", 200_000)
            bot = await e["reg"].create("Builder", "coder", chain=["work/m"], tools=list(DEFAULT_TOOLS))
            mock.script("work", sse("I think maybe a lot?"), sse("done"))
            seen: list = []
            ok = asyncio.create_task(approver(e["approvals"], seen, decide=True))
            out = await e["runner"].run(bot.id, "finish", project_id="p1")
            ok.cancel()
            await e["db"].close()
            return out, seen
    out, seen = run(go())
    assert out.status == "completed"
    assert seen[0]["rehearsal"]["asking_for"] == 50_000 and "no estimate from the model" in seen[0]["summary"]


def test_cap_zero_means_no_cap_and_other_projects_are_not_counted(tmp_path):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            db = e["db"]
            await spent(db, "p1", 900_000)
            await db.write("INSERT INTO projects (id, goal, created_by) VALUES ('p2', 'other', 'user')")
            await db.write("INSERT INTO jobs (id, project_id, title, status, created_by) VALUES ('job_p2', 'p2', 't', 'running', 'user')")
            uncapped = Budget.from_settings(db, {"project_daily_tokens": 0})
            capped = Budget.from_settings(db, {})
            # an extra from an earlier day doesn't count today
            await db.write("UPDATE projects SET budget_json=? WHERE id='p1'", (json.dumps({"extra_tokens": {"2000-01-01": 10**9}}),))
            res = (await uncapped.check_project("job_earlier"), await capped.check_project("job_p2"),
                   await capped.check_project("job_earlier"), await capped.check_project(None))
            await db.close()
            return res
    none_cap, other, over, no_job = run(go())
    assert none_cap is None and other is None and no_job is None
    assert over == {"project_id": "p1", "used": 900_000, "cap": 200_000}
