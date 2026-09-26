"""Budgets and spend caps (PLAN.md A9.c.02).

Tokens: per task (BotAgent.token_budget, A7), per bot per day and for the whole
team per day, counted from `provider_usage_events`. A bot over a cap stops at
its next step with status `budget`.

Money: every R4 tool declares a cost estimate (`Tool.cost`). Before the user is
even asked, the estimate is checked against the caps per task, per bot per day
and per day; over a cap, the action is refused without an approval card. After
an approved R4 action runs, its cost goes into `spend_events`.

0 means "no cap" for token limits. Money caps are always on (R4 = real money).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULTS = {
    "daily_tokens": 0,               # whole team, per day (0 = no cap)
    "bot_daily_tokens": 0,           # each bot, per day (0 = no cap)
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
