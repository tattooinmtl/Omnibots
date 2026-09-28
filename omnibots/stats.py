"""Usage, health and projects, for the windows (PLAN.md A13.a.01, A13.a.02; A11.a.03).

Tokens come from `provider_usage_events` (recent rows) plus `usage_daily` (older days rolled up by
housekeeping, A16.c.04), so history survives the retention window. No cost in dollars: the providers
don't report prices, and the plan counts tokens (MiniMax's 1.5B budget, the cheap lane's quotas).
"""

from __future__ import annotations

import time
from typing import Any

TOK = "COALESCE(tokens_in,0)+COALESCE(tokens_out,0)"


def _since(days: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() - days * 86400))


async def usage(db, days: int = 14) -> dict[str, Any]:
    """Tokens and model calls per day, per provider, per bot and per project over the last `days`."""
    since = _since(days)
    per_day: dict[str, dict[str, int]] = {}
    for r in await db.read(f"SELECT substr(created_at,1,10) AS d, SUM({TOK}) AS t, COUNT(*) AS n FROM provider_usage_events "
                           "WHERE substr(created_at,1,10) >= ? GROUP BY d", (since,)):
        per_day[r["d"]] = {"tokens": int(r["t"] or 0), "calls": int(r["n"])}
    for r in await db.read(f"SELECT day AS d, SUM({TOK}) AS t, SUM(calls) AS n FROM usage_daily WHERE day >= ? GROUP BY day", (since,)):
        cur = per_day.setdefault(r["d"], {"tokens": 0, "calls": 0})
        cur["tokens"] += int(r["t"] or 0)
        cur["calls"] += int(r["n"] or 0)

    async def grouped(col: str, extra_join: str = "") -> list[dict[str, Any]]:
        rows = await db.read(
            f"SELECT {col} AS k, SUM({TOK.replace('tokens', 'u.tokens')}) AS t, COUNT(*) AS n, "
            f"SUM(u.status_code = 429) AS r429, SUM(u.status_code >= 400) AS errs "
            f"FROM provider_usage_events u {extra_join} WHERE substr(u.created_at,1,10) >= ? GROUP BY k ORDER BY t DESC", (since,))
        return [{"id": r["k"] or "(none)", "tokens": int(r["t"] or 0), "calls": int(r["n"]), "rate_limited": int(r["r429"] or 0),
                 "errors": int(r["errs"] or 0)} for r in rows]

    days_sorted = sorted(per_day.items())
    return {"days": days, "per_day": [{"day": d, **v} for d, v in days_sorted],
            "total_tokens": sum(v["tokens"] for v in per_day.values()),
            "per_provider": await grouped("u.provider"),
            "per_bot": await grouped("u.bot_id"),
            "per_project": await grouped("j.project_id", "LEFT JOIN jobs j ON j.id = u.job_id")}


async def health(db, days: int = 7) -> dict[str, Any]:
    """Per bot: jobs finished / failed / interrupted, average job time, stalls; and what slows the team down."""
    since = _since(days)
    bots = []
    for r in await db.read(
            "SELECT assigned_bot_id AS b, SUM(status='completed') AS ok, SUM(status='failed') AS bad, "
            "SUM(status='interrupted') AS cut, COUNT(*) AS n, "
            "AVG(CASE WHEN finished_at IS NOT NULL AND started_at IS NOT NULL "
            "    THEN (julianday(finished_at) - julianday(started_at)) * 86400 END) AS secs "
            "FROM jobs WHERE assigned_bot_id IS NOT NULL AND substr(created_at,1,10) >= ? GROUP BY b ORDER BY n DESC", (since,)):
        stalls = (await db.read_one("SELECT COUNT(*) AS n FROM messages WHERE substr(created_at,1,10) >= ? "
                                    "AND sender_type='system' AND payload_json LIKE ?", (since, f'%{r["b"]} has been silent%')))["n"]
        finished = int(r["ok"] or 0) + int(r["bad"] or 0)
        bots.append({"id": r["b"], "jobs": int(r["n"]), "completed": int(r["ok"] or 0), "failed": int(r["bad"] or 0),
                     "interrupted": int(r["cut"] or 0), "avg_seconds": round(float(r["secs"] or 0), 1),
                     "failure_rate": round(int(r["bad"] or 0) / finished, 2) if finished else 0.0, "stalls": int(stalls)})
    seat_waits = (await db.read_one("SELECT COUNT(*) AS n FROM messages WHERE message_type='SEAT_WAITING' "
                                    "AND substr(created_at,1,10) >= ?", (since,)))["n"]
    limited = await db.read("SELECT provider, COUNT(*) AS n FROM provider_usage_events WHERE status_code=429 "
                            "AND substr(created_at,1,10) >= ? GROUP BY provider ORDER BY n DESC", (since,))
    bottlenecks = []
    for r in limited:
        bottlenecks.append(f"{r['provider']} said 429 (rate limit) {r['n']} time(s)")
    if seat_waits:
        bottlenecks.append(f"bots waited for a MiniMax seat {seat_waits} time(s)")
    for b in bots:
        if b["stalls"]:
            bottlenecks.append(f"{b['id']} went silent {b['stalls']} time(s)")
        if b["failure_rate"] >= 0.5 and b["failed"] >= 2:
            bottlenecks.append(f"{b['id']} failed {b['failed']} of {b['completed'] + b['failed']} jobs")
    return {"days": days, "bots": bots, "seat_waits": int(seat_waits), "bottlenecks": bottlenecks}


async def projects(db, limit: int = 40) -> list[dict[str, Any]]:
    """Open and recent projects: goal, status, the dial, live address, jobs, tokens today and in total, your verdict."""
    today = time.strftime("%Y-%m-%d", time.gmtime())
    out = []
    for p in await db.read("SELECT id, goal, status, autonomy, live_url, path, created_at FROM projects "
                           "ORDER BY (status = 'cancelled'), created_at DESC LIMIT ?", (limit,)):
        tok = await db.read_one(
            f"SELECT SUM({TOK.replace('tokens', 'u.tokens')}) AS t, "
            f"SUM(CASE WHEN substr(u.created_at,1,10) = ? THEN {TOK.replace('tokens', 'u.tokens')} END) AS today "
            "FROM provider_usage_events u JOIN jobs j ON j.id = u.job_id WHERE j.project_id=?", (today, p["id"]))
        jobs = await db.read_one("SELECT COUNT(*) AS n, SUM(status IN ('running','assigned','ready','review')) AS live "
                                 "FROM jobs WHERE project_id=?", (p["id"],))
        verdict = await db.read_one("SELECT verdict FROM verdicts WHERE project_id=? AND claim_id IS NULL ORDER BY id DESC LIMIT 1",
                                    (p["id"],))
        out.append({"id": p["id"], "goal": p["goal"], "status": p["status"], "autonomy": p["autonomy"],
                    "live_url": p["live_url"], "path": p["path"], "created_at": p["created_at"],
                    "tokens": int(tok["t"] or 0) if tok else 0, "tokens_today": int(tok["today"] or 0) if tok else 0,
                    "jobs": int(jobs["n"] or 0), "active_jobs": int(jobs["live"] or 0),
                    "verdict": verdict["verdict"] if verdict else None})
    return out
