"""Budgets and spend caps (PLAN.md A9.c.02).

Tokens: per task (BotAgent.token_budget, A7), per bot per day and for the whole
team per day, counted from `provider_usage_events`. A bot over a cap stops at
its next step with status `budget`.

Money: every R4 tool declares a cost estimate (`Tool.cost`). Before the user is
even asked, the estimate is checked against the caps per task, per bot per day
and per day; over a cap, the action is refused without an approval card. After
an approved R4 action runs, its cost goes into `spend_events`.

Per project (A9.c.03): each project may use `project_daily_tokens` a day. It's
not a wall: when it's used up, the bot asks the user for more, with its own
estimate of what finishing needs; each OK adds that much for today only.

0 means "no cap" for token limits. Money caps are always on (R4 = real money).
"""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass
from typing import Any

DEFAULTS = {
    "daily_tokens": 0,               # whole team, per day (0 = no cap)
    "bot_daily_tokens": 0,           # each bot, per day (0 = no cap)
    "project_daily_tokens": 200_000, # each project, per day; the bot asks for more (0 = no cap)
    "money_per_task_usd": 2.0,
    "money_per_bot_day_usd": 5.0,
    "money_per_day_usd": 10.0,
}
# "Today" = since the user's local midnight; timestamps are stored in UTC ('...Z').
TODAY = "created_at >= strftime('%Y-%m-%dT%H:%M:%fZ', 'now', 'localtime', 'start of day', 'utc')"


@dataclass
class Budget:
    db: Any
    limits: dict[str, float]

    @classmethod
    def from_settings(cls, db, settings: dict[str, Any] | None) -> "Budget":
        return cls(db, {**DEFAULTS, **{k: float(v) for k, v in (settings or {}).items() if k in DEFAULTS}})

    # ── tokens ─────────────────────────────────────────────────────────────
    async def tokens_today(self, bot_id: str | None = None) -> int:
        sql = f"SELECT COALESCE(SUM(COALESCE(tokens_in,0)+COALESCE(tokens_out,0)),0) AS t FROM provider_usage_events WHERE {TODAY}"
        row = await self.db.read_one(sql + (" AND bot_id=?" if bot_id else ""), (bot_id,) if bot_id else ())
        return int(row["t"] or 0)

    async def check_tokens(self, bot_id: str) -> str | None:
        """None when the bot may call a model; else why not."""
        team = int(self.limits["daily_tokens"])
        if team and (used := await self.tokens_today()) >= team:
            return f"the team's daily token cap is used up ({used:,}/{team:,})"
        per_bot = int(self.limits["bot_daily_tokens"])
        if per_bot and (used := await self.tokens_today(bot_id)) >= per_bot:
            return f"this bot's daily token cap is used up ({used:,}/{per_bot:,})"
        return None

    # ── per project (A9.c.03) ─────────────────────────────────────────────
    async def project_of(self, job_id: str | None) -> str | None:
        if not job_id:
            return None
        row = await self.db.read_one("SELECT project_id FROM jobs WHERE id=?", (job_id,))
        return row["project_id"] if row else None

    async def project_tokens_today(self, project_id: str) -> int:
        row = await self.db.read_one(
            "SELECT COALESCE(SUM(COALESCE(u.tokens_in,0)+COALESCE(u.tokens_out,0)),0) AS t FROM provider_usage_events u "
            f"JOIN jobs j ON j.id = u.job_id WHERE j.project_id=? AND u.{TODAY}", (project_id,))
        return int(row["t"] or 0)

    async def _project_budget(self, project_id: str) -> dict[str, Any]:
        row = await self.db.read_one("SELECT budget_json FROM projects WHERE id=?", (project_id,))
        try:
            return json.loads(row["budget_json"] or "{}") if row else {}
        except ValueError:
            return {}

    async def project_cap_today(self, project_id: str) -> int:
        """The day's cap plus what the user added today. 0 = no cap."""
        base = int(self.limits["project_daily_tokens"])
        if not base:
            return 0
        extra = (await self._project_budget(project_id)).get("extra_tokens", {})
        return base + int(extra.get(datetime.date.today().isoformat(), 0) if isinstance(extra, dict) else 0)

    async def check_project(self, job_id: str | None) -> dict[str, Any] | None:
        """None while the job's project is under today's cap; else {project_id, used, cap}."""
        pid = await self.project_of(job_id)
        if not pid or not (cap := await self.project_cap_today(pid)):
            return None
        used = await self.project_tokens_today(pid)
        return {"project_id": pid, "used": used, "cap": cap} if used >= cap else None

    async def add_project_tokens(self, project_id: str, tokens: int) -> int:
        """The user said yes: `tokens` more for this project, today only. Returns the new cap."""
        b = await self._project_budget(project_id)
        today = datetime.date.today().isoformat()
        b["extra_tokens"] = {today: int((b.get("extra_tokens") or {}).get(today, 0)) + max(0, int(tokens))}   # older days dropped
        await self.db.write("UPDATE projects SET budget_json=? WHERE id=?", (json.dumps(b), project_id))
        return await self.project_cap_today(project_id)

    # ── money ──────────────────────────────────────────────────────────────
    async def spent(self, *, job_id: str | None = None, bot_id: str | None = None, today: bool = True) -> float:
        where, args = ([TODAY] if today else []), []
        for col, val in (("job_id", job_id), ("bot_id", bot_id)):
            if val:
                where.append(f"{col}=?")
                args.append(val)
        row = await self.db.read_one("SELECT COALESCE(SUM(amount_usd),0) AS s FROM spend_events"
                                     + (" WHERE " + " AND ".join(where) if where else ""), args)
        return float(row["s"] or 0)

    async def check_spend(self, *, bot_id: str, job_id: str | None, amount: float | None) -> str | None:
        """None when `amount` fits every cap; else why not. An unknown cost is refused."""
        if amount is None:
            return "this action's cost is unknown, so it can't be checked against your spend caps"
        checks = [
            ("this task", await self.spent(job_id=job_id, today=False) if job_id else 0.0, self.limits["money_per_task_usd"]),
            ("this bot today", await self.spent(bot_id=bot_id), self.limits["money_per_bot_day_usd"]),
            ("the team today", await self.spent(), self.limits["money_per_day_usd"]),
        ]
        for label, used, cap in checks:
            if used + amount > cap + 1e-9:
                return f"${amount:.2f} would exceed the spend cap for {label} (${used:.2f} of ${cap:.2f} used)"
        return None

    async def record_spend(self, *, bot_id: str, job_id: str | None, project_id: str | None, tool: str,
                           amount: float, description: str = "", approval_id: str | None = None) -> None:
        await self.db.write("INSERT INTO spend_events (bot_id, job_id, project_id, tool, amount_usd, description, approval_id) "
                            "VALUES (?,?,?,?,?,?,?)", (bot_id, job_id, project_id, tool, float(amount), description[:300], approval_id))

    async def snapshot(self) -> dict[str, Any]:
        return {"tokens_today": await self.tokens_today(), "spent_today_usd": round(await self.spent(), 2), "limits": dict(self.limits)}
