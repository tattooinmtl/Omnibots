"""Live bug (user, 2026-09-26): "task failed: time budget of 1800.0s used up" - Omi spent the 30 min
parked on an approval nobody saw, and the planned follow-up jobs were never done. Now: waiting on the
user doesn't use the budget, and a goal that does run out of time is resumable (▶ Start)."""

from __future__ import annotations

import asyncio
import contextlib
import time

import pytest

from mock_provider import MockProviders, sse
from test_a7_orchestrator import build, call

from omnibots.bots.runner import _within_budget


class FakeAgent:
    def __init__(self):
        self.held, self.since = 0.0, None

    @contextlib.contextmanager
    def waiting_on_user(self):
        self.since = time.monotonic()
        try:
            yield
        finally:
            self.held += time.monotonic() - self.since
            self.since = None

    def held_seconds(self):
        return self.held + ((time.monotonic() - self.since) if self.since else 0.0)


def test_waiting_on_the_user_does_not_use_the_budget():
    async def go():
        a = FakeAgent()

        async def job():
            await asyncio.sleep(0.3)                           # real work
            with a.waiting_on_user():
                await asyncio.sleep(2.0)                       # an approval card sits there
            await asyncio.sleep(0.3)
            return "done"
        ok = await _within_budget(a, job(), 1.0, time.monotonic())

        async def busy():
            await asyncio.sleep(3.0)                           # really working past the budget
        b = FakeAgent()
        with pytest.raises(asyncio.TimeoutError):
            await _within_budget(b, busy(), 1.0, time.monotonic())
        return ok
    assert asyncio.run(go()) == "done"


def test_a_goal_that_runs_out_of_time_is_resumable_and_says_whats_left(tmp_path):
    from omnibots.orchestrator.planner import PLANNER_PROMPT  # noqa: F401  (the planner is scripted below)
    from test_a7_orchestrator import plan_json, sub

    async def go():
        with MockProviders() as mock:
            mock.script("plan", sse(plan_json(sub("style the page", done="style.css exists"))))
            slow = sse("", tool_calls=[call("list_jobs")])
            slow["hold"] = 20.0                                # the boss is slow: the 8 s goal budget runs out
            mock.script("boss", sse("", tool_calls=[call("plan_goal", goal="add a css")]), slow)
            e = await build(tmp_path, mock)
            q = await e["bus"].subscribe()
            out = await e["orch"].run_goal("add a css to the index.html", seconds=8.0)   # room for plan_goal on a busy PC
            jobs = {j.title: j.status for j in await e["graph"].jobs(out["project_id"])}
            msgs = []
            while True:
                m = await q.get(timeout=0.2)
                if m is None:
                    break
                msgs.append(m)
            await e["db"].close()
            return out, jobs, msgs
    out, jobs, msgs = asyncio.run(go())
    goal_job = next(s for t, s in jobs.items() if t.startswith("[goal]"))
    assert goal_job == "interrupted"                                          # ▶ Start picks it up
    q = [m for m in msgs if m.message_type == "QUESTION" and m.recipient_id == "user"]
    assert q and "ran out of my time" in q[-1].text() and "style the page" in q[-1].text() and "Start" in q[-1].text()
