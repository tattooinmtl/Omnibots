"""A13 usage & health, A11.a.03 the Projects window. Real database rows (jobs, usage, the board, verdicts,
old days rolled up by housekeeping); real Qt windows fed through a fake engine."""

from __future__ import annotations

import asyncio
import concurrent.futures
import time
from types import SimpleNamespace

import pytest

from mock_provider import MockProviders
from qt_helpers import qapp
from test_a7_orchestrator import build

from omnibots import stats
from omnibots.engine import Engine
from omnibots.orchestrator import verdicts
from omnibots.ui.projects_window import ProjectsWindow
from omnibots.ui.usage_window import UsageWindow

app = qapp()
TODAY = time.strftime("%Y-%m-%d", time.gmtime())
OLD = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 5 * 86400))


def run(coro):
    return asyncio.run(coro)


async def seed(e):
    db = e["db"]
    a = await e["reg"].create("Builder", "coder", chain=["work/m"])
    b = await e["reg"].create("Checker", "tester", chain=["plan/m"])
    pid = await e["projects"].create("a landing page")
    jobs = [("j1", a.id, "completed", 60), ("j2", a.id, "failed", 120), ("j3", a.id, "failed", 30), ("j4", b.id, "running", None)]
    for jid, bot, status, secs in jobs:
        fin = f"'{TODAY}T10:{secs // 60:02d}:{secs % 60:02d}Z'" if secs else "NULL"
        await db.write(f"INSERT INTO jobs (id, project_id, title, status, created_by, assigned_bot_id, started_at, finished_at) "
                       f"VALUES (?, ?, 't', ?, 'omi', ?, '{TODAY}T10:00:00Z', {fin})", (jid, pid, status, bot))
    for bot, job, tin, tout, code in ((a.id, "j1", 1000, 200, 200), (a.id, "j2", 500, 0, 429), (b.id, "j4", 300, 100, 200)):
        await db.write("INSERT INTO provider_usage_events (provider, model, bot_id, job_id, tokens_in, tokens_out, status_code) "
                       "VALUES (?, 'm', ?, ?, ?, ?, ?)", ("work" if bot == a.id else "plan", bot, job, tin, tout, code))
    await db.write("INSERT INTO usage_daily (day, provider, model, bot_id, tokens_in, tokens_out, calls) VALUES (?, 'work', 'm', ?, 7000, 0, 9)",
                   (OLD, a.id))
    await db.write("INSERT INTO messages (topic, sender_type, message_type, payload_json) VALUES ('#bot/omi', 'system', 'A2A_MESSAGE', ?)",
                   (f'{{"text": "{b.id} has been silent for 10 min on its job; consider reassigning it."}}',))
    await db.write("INSERT INTO messages (topic, sender_type, message_type, payload_json) VALUES ('#general', 'system', 'SEAT_WAITING', '{\"position\": 1}')")
    await verdicts.rate(db, e["reg"], e["bus"], project_id=pid, verdict=1)
    return a, b, pid


def test_usage_health_and_projects_from_real_rows(tmp_path):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            a, b, pid = await seed(e)
            out = (await stats.usage(e["db"]), await stats.health(e["db"]), await stats.projects(e["db"]))
            await e["db"].close()
            return a, b, pid, out
    a, b, pid, (u, h, projects) = run(go())
    days = {d["day"]: d for d in u["per_day"]}
    assert days[TODAY] == {"day": TODAY, "tokens": 2100, "calls": 3} and days[OLD]["tokens"] == 7000    # old days from usage_daily
    assert u["total_tokens"] == 9100
    work = next(p for p in u["per_provider"] if p["id"] == "work")
    assert work == {"id": "work", "tokens": 1700, "calls": 2, "rate_limited": 1, "errors": 1}
    assert next(p for p in u["per_project"] if p["id"] == pid)["tokens"] == 2100
    hb = {x["id"]: x for x in h["bots"]}
    assert hb[a.id]["completed"] == 1 and hb[a.id]["failed"] == 2 and hb[a.id]["failure_rate"] == 0.67
    assert hb[a.id]["avg_seconds"] == 70.0 and hb[b.id]["stalls"] == 1
    assert any("work said 429" in x for x in h["bottlenecks"]) and any("MiniMax seat 1 time" in x for x in h["bottlenecks"])
    assert any(f"{b.id} went silent 1" in x for x in h["bottlenecks"]) and any(f"{a.id} failed 2 of 3" in x for x in h["bottlenecks"])
    (p,) = projects
    assert p["tokens"] == 2100 and p["tokens_today"] == 2100 and p["active_jobs"] == 1 and p["verdict"] == 1 and p["autonomy"] == "watch"


def test_close_project_is_refused_while_work_runs(tmp_path):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            a, b, pid = await seed(e)
            eng = SimpleNamespace(db=e["db"], projects=e["projects"])
            try:
                await Engine.close_project(eng, pid)
                refused = None
            except RuntimeError as exc:
                refused = str(exc)
            await e["db"].write("UPDATE jobs SET status='completed' WHERE id='j4'")
            closed = await Engine.close_project(eng, pid)
            status = (await e["db"].read_one("SELECT status FROM projects WHERE id=?", (pid,)))["status"]
            await e["db"].close()
            return refused, closed, status
    refused, closed, status = run(go())
    assert refused and "still running" in refused and closed == "closed" and status == "cancelled"


class FakeEngine:
    def __init__(self, projects=None, stats_data=None):
        self.projects_data, self.stats_data, self.calls = projects or [], stats_data, []

    def submit(self, coro):
        result = asyncio.run(coro)
        f = concurrent.futures.Future()
        f.set_result(result)
        return f

    async def ui_projects(self):
        return self.projects_data

    async def ui_stats(self, days):
        return self.stats_data

    async def set_autonomy(self, pid, level):
        self.calls.append(("dial", pid, level))
        return level

    async def close_project(self, pid):
        self.calls.append(("close", pid))
        return "closed"


PROJECTS = [{"id": "p1", "goal": "a landing page for the bakery", "status": "open", "autonomy": "watch", "live_url": "https://bakery.example",
             "path": "", "folder": ".", "created_at": f"{TODAY}T10:00:00Z", "tokens": 812_000, "tokens_today": 64_000,
             "jobs": 5, "active_jobs": 0, "verdict": 1},
            {"id": "p2", "goal": "SEO report", "status": "cancelled", "autonomy": "off", "live_url": None, "path": "", "folder": ".",
             "created_at": f"{OLD}T10:00:00Z", "tokens": 12_500, "tokens_today": 0, "jobs": 2, "active_jobs": 0, "verdict": None}]


def test_the_projects_window_changes_the_dial_and_closes_after_asking(tmp_path):
    eng = FakeEngine(PROJECTS)
    asked = []
    w = ProjectsWindow(eng, refresh_ms=10**9, confirm=lambda t, x: asked.append(t) or True)
    w.show_data(PROJECTS)
    w.set_level(PROJECTS[0], "fix")
    w.close_project(PROJECTS[0])
    app.processEvents()
    assert eng.calls == [("dial", "p1", "fix"), ("close", "p1")] and asked == ["Close this project?"]
    w.resize(760, 520)
    assert w.grab().save(str(tmp_path / "projects.png"))


def test_the_usage_window_renders_real_numbers(tmp_path):
    data = {"usage": {"days": 14, "total_tokens": 9100, "per_day": [{"day": OLD, "tokens": 7000, "calls": 9},
                                                                     {"day": TODAY, "tokens": 2100, "calls": 3}],
                      "per_provider": [{"id": "work", "tokens": 1700, "calls": 2, "rate_limited": 1, "errors": 1}],
                      "per_bot": [{"id": "b1", "tokens": 1700, "calls": 2, "rate_limited": 1, "errors": 1}],
                      "per_project": [{"id": "p1", "tokens": 2100, "calls": 3, "rate_limited": 0, "errors": 0}]},
            "health": {"days": 7, "bots": [{"id": "b1", "jobs": 3, "completed": 1, "failed": 2, "interrupted": 0, "avg_seconds": 70.0,
                                             "failure_rate": 0.67, "stalls": 0}], "seat_waits": 1,
                       "bottlenecks": ["work said 429 (rate limit) 1 time(s)"]},
            "verdicts": {"bot_id": [{"id": "b1", "up": 3, "down": 1, "share_up": 0.75}]}, "names": {"b1": "Builder"},
            "projects": {"p1": "a landing page"}}
    w = UsageWindow(FakeEngine(stats_data=data), refresh_ms=10**9)
    w.show_data(data)
    texts = [l.text() for l in w.findChildren(type(w.status))]
    assert "9k" in texts and any("429" in t for t in texts)
    assert w.grab().save(str(tmp_path / "usage.png"))
