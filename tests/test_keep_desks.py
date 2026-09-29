"""A15.f.04: a bot's VPS computer stays up while its project is open, on Fix, and within budget: the app checks
in (the gateway's idle clock counts any request), never starts a computer, never creates a login, and stops
checking in when paused, on Watch/Off, closed, or out of background tokens. Real database, budget, leash."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from mock_provider import MockProviders
from test_a7_orchestrator import build

from omnibots.bots.leash import Leash
from omnibots.engine import Engine
from omnibots.security.budget import Budget


def run(coro):
    return asyncio.run(coro)


class Gateway:
    def __init__(self, logins: set[str], running: set[str]):
        self.logins, self.running, self.calls = logins, running, []

    def secret_for(self, bot):
        return "secret" if bot in self.logins else None

    async def call(self, bot, method, path, **kw):
        self.calls.append((bot, method, path))
        return SimpleNamespace(status_code=200, json=lambda: {"running": bot in self.running})


def test_only_running_desks_of_open_fix_projects_within_budget_are_kept_up(tmp_path):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            db = e["db"]
            leash = await Leash(db).load()
            bots = {n: await e["reg"].create(n, "coder", chain=["work/m"]) for n in ("fixer", "watcher", "closed", "nologin", "broke", "off")}
            ids = {n: b.id for n, b in bots.items()}
            for n, (autonomy, status) in {"fixer": ("fix", "open"), "watcher": ("watch", "open"), "closed": ("fix", "cancelled"),
                                          "nologin": ("fix", "open"), "broke": ("fix", "open"), "off": ("fix", "open")}.items():
                pid = await e["projects"].create(n)
                await db.write("UPDATE projects SET autonomy=?, status=? WHERE id=?", (autonomy, status, pid))
                await db.write("INSERT INTO jobs (id, project_id, title, status, created_by, origin, assigned_bot_id) "
                               "VALUES (?, ?, 't', 'completed', 'omi', 'fix', ?)", (f"j_{n}", pid, ids[n]))
            await db.write("INSERT INTO provider_usage_events (provider, model, bot_id, job_id, tokens_in, tokens_out, status_code) "
                           "VALUES ('work', 'm', ?, 'j_broke', 300000, 0, 200)", (ids["broke"],))       # out of background tokens
            gw = Gateway(logins={ids[n] for n in ("fixer", "watcher", "closed", "broke", "off")},
                         running={ids[n] for n in ("fixer", "watcher", "closed", "nologin", "broke")})
            eng = SimpleNamespace(db=db, leash=leash, computers=gw, budget=Budget.from_settings(db, {}))
            kept = await Engine.keep_desks(eng)
            first_calls = list(gw.calls)
            await leash.set_paused(True)
            gw.calls.clear()
            paused = await Engine.keep_desks(eng)
            await db.close()
            return ids, kept, first_calls, paused, gw.calls
    ids, kept, calls, paused, paused_calls = run(go())
    assert kept == [ids["fixer"]]                                          # running, on Fix, open, logged in, within budget
    called = {b for b, _, _ in calls}
    assert ids["nologin"] not in called and ids["watcher"] not in called and ids["closed"] not in called and ids["broke"] not in called
    assert ids["off"] in called and all(m == "GET" and p == "/computer" for _, m, p in calls)   # a check, never a start
    assert paused == [] and paused_calls == []
