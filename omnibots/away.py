"""What the bots did on their own (PLAN.md A15.g): the "While you were away" note in Omi's chat when the
app opens (A15.g.01) and the "On their own today" strip in Omi's window (A15.g.03).

Background work = jobs whose origin isn't `user` (A15.b.01): Omi's checks, repairs, next rounds,
routines, triggers, the night shift, tool relays.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

LAST_SEEN = "last_seen"
LABELS = {"watch": "check", "fix": "repair", "continue": "round carried on", "routine": "routine run",
          "trigger": "trigger run", "night": "night-shift job", "relay": "tool relay"}


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else ("es" if word.endswith(("ch", "sh")) else "s"))


def _k(n: int) -> str:
    return f"{n / 1_000_000:.1f}M" if n >= 1_000_000 else f"{round(n / 1000):,}k" if n >= 1000 else str(n)


def now_iso() -> str:
    """Like the database's timestamps, to the millisecond (a whole second would count the same second's work twice)."""
    t = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{int(t * 1000) % 1000:03d}Z"


async def get_last_seen(db) -> str | None:
    row = await db.read_one("SELECT value FROM parameters WHERE scope='global' AND bot_id IS NULL AND key=?", (LAST_SEEN,))
    return row["value"] if row else None


async def set_last_seen(db, when: str | None = None) -> None:
    await db.write("DELETE FROM parameters WHERE scope='global' AND bot_id IS NULL AND key=?", (LAST_SEEN,))
    await db.write("INSERT INTO parameters (scope, bot_id, key, value) VALUES ('global', NULL, ?, ?)", (LAST_SEEN, when or now_iso()))


async def background_since(db, since: str) -> dict[str, Any]:
    jobs = await db.read(
        "SELECT j.project_id, p.goal, j.origin, COUNT(*) AS n FROM jobs j LEFT JOIN projects p ON p.id = j.project_id "
        "WHERE j.origin != 'user' AND COALESCE(j.started_at, j.created_at) >= ? GROUP BY j.project_id, j.origin", (since,))
    tokens = await db.read_one(
        "SELECT COALESCE(SUM(COALESCE(u.tokens_in,0)+COALESCE(u.tokens_out,0)),0) AS t FROM provider_usage_events u "
        "JOIN jobs j ON j.id = u.job_id WHERE j.origin != 'user' AND u.created_at >= ?", (since,))
    live = await db.read("SELECT project_id, payload_json FROM messages WHERE created_at >= ? AND sender_id='omi' "
                         "AND (payload_json LIKE '%is back up%' OR payload_json LIKE '%is down (%')", (since,))
    projects: dict[str, dict[str, Any]] = {}
    for r in jobs:
        p = projects.setdefault(r["project_id"] or "", {"goal": r["goal"] or "(no project)", "counts": {}, "live": []})
        p["counts"][r["origin"]] = p["counts"].get(r["origin"], 0) + int(r["n"])
    for r in live:
        p = projects.setdefault(r["project_id"] or "", {"goal": "(a project)", "counts": {}, "live": []})
        p["live"].append("✅ back up" if "back up" in (r["payload_json"] or "") else "⚠ went down")
    return {"projects": projects, "tokens": int(tokens["t"] or 0),
            "runs": sum(sum(p["counts"].values()) for p in projects.values())}


async def away_summary(db, since: str | None) -> str | None:
    """The note for Omi's chat, or None when nothing happened on its own and nothing waits for you."""
    if not since:
        return None
    bg = await background_since(db, since)
    approvals = (await db.read_one("SELECT COUNT(*) AS n FROM approvals WHERE status='pending'"))["n"]
    questions = (await db.read_one("SELECT COUNT(*) AS n FROM messages WHERE message_type='QUESTION' AND recipient_id='user' "
                                   "AND created_at >= ?", (since,)))["n"]
    if not bg["runs"] and not approvals and not questions and not any(p["live"] for p in bg["projects"].values()):
        return None
    try:
        when = datetime.fromisoformat(since.replace("Z", "+00:00")).astimezone().strftime("%b %d %H:%M")
    except ValueError:
        when = since
    lines = [f"While you were away (since {when}):"]          # the chat shows plain text
    for p in bg["projects"].values():
        parts = [_plural(n, LABELS.get(o, o)) for o, n in sorted(p["counts"].items())]
        parts += p["live"]
        lines.append(f"• {p['goal'][:70]}: " + ", ".join(parts))
    if bg["tokens"]:
        lines.append(f"• {_k(bg['tokens'])} tokens of background work")
    waiting = [x for x in (_plural(approvals, "approval") if approvals else "", _plural(questions, "question") if questions else "") if x]
    if waiting:
        lines.append(f"• waiting for you: {' and '.join(waiting)}")
    return "\n".join(lines)


async def today(db, paused: bool) -> dict[str, Any]:
    """For the strip: background work since local midnight, whether it's paused, sites that are down."""
    midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    bg = await background_since(db, midnight.strftime("%Y-%m-%dT%H:%M:%S.000Z"))
    totals: dict[str, int] = {}
    for p in bg["projects"].values():
        for o, n in p["counts"].items():
            totals[o] = totals.get(o, 0) + n
    down = (await db.read_one("SELECT COUNT(*) AS n FROM projects WHERE live_status='down' AND status != 'cancelled'"))["n"]
    return {"totals": totals, "tokens": bg["tokens"], "runs": bg["runs"], "paused": paused, "down": int(down)}


def strip_text(d: dict[str, Any]) -> str:
    parts = [_plural(n, LABELS.get(o, o)) for o, n in sorted(d["totals"].items())] or ["nothing yet"]
    text = "On their own today: " + ", ".join(parts)
    if d["tokens"]:
        text += f" · {_k(d['tokens'])} tokens"
    if d["down"]:
        text += f" · ⚠ {_plural(d['down'], 'live site')} down"
    if d["paused"]:
        text += " · paused"
    return text
