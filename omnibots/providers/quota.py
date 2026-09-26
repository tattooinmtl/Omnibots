"""Provider quota state, 429 cooling/recovery, usage events, and the MiniMax
token reservoir (PLAN.md A2.b.01–03, A2.b.07).

State lives in memory for speed and is written through to SQL (`providers`,
`provider_usage_events`), so it survives restarts and feeds the stats views.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from omnibots.lineup import MINIMAX, MINIMAX_TOKEN_BUDGET

log = logging.getLogger(__name__)

DEFAULT_COOLDOWN = 60.0          # seconds, when a 429 carries no reset header
MAX_COOLDOWN = 15 * 60.0         # escalation cap for repeated 429s
RESERVOIR_THRESHOLDS = (0.50, 0.75, 0.90)


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Port of Omni's estimateTokens: ~4 chars per token, +20 per message."""
    chars = 0
    for m in messages or []:
        c = m.get("content")
        if isinstance(c, str):
            n = len(c)
        elif isinstance(c, list):
            n = sum(len(p) if isinstance(p, str) else len((p or {}).get("text") or "") for p in c
                    if isinstance(p, str) or (p or {}).get("type") == "text")
        else:
            n = 0
        chars += n + 20
        for call in m.get("tool_calls") or []:
            fn = call.get("function") or {}
            chars += len(fn.get("arguments") or "") + len(fn.get("name") or "") + 30
    return math.ceil(chars / 4)


def iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z") if ts else None


@dataclass
class ProviderState:
    name: str
    status: str = "ok"              # ok | cooling | error
    reset_at: float | None = None   # epoch seconds; cooling ends
    last_429_at: float | None = None
    consecutive_429: int = 0
    error: str | None = None
    tokens_used: int = 0
    token_budget: int | None = None
    thresholds_hit: set[float] = field(default_factory=set)


class QuotaManager:
    def __init__(self, db=None, *, clock: Callable[[], float] = time.time,
                 on_event: Callable[[str, dict[str, Any]], Awaitable[None] | None] | None = None,
                 minimax_budget: int = MINIMAX_TOKEN_BUDGET):
        self.db = db
        self.clock = clock
        self.on_event = on_event
        self.states: dict[str, ProviderState] = {}
        self.minimax_budget = minimax_budget

    def state(self, name: str) -> ProviderState:
        st = self.states.get(name)
        if st is None:
            st = self.states[name] = ProviderState(name, token_budget=self.minimax_budget if name == MINIMAX else None)
        return st

    async def load(self) -> None:
        """Restore cooling/usage state from SQL after a restart."""
        if not self.db:
            return
        for row in await self.db.read("SELECT * FROM providers"):
            st = self.state(row["name"])
            st.status = row["status"] or "ok"
            st.tokens_used = row["tokens_used"] or 0
            if row["token_budget"]:
                st.token_budget = row["token_budget"]
            if row["reset_at"]:
                st.reset_at = datetime.fromisoformat(row["reset_at"].replace("Z", "+00:00")).timestamp()
            if st.token_budget:
                st.thresholds_hit = {t for t in RESERVOIR_THRESHOLDS if st.tokens_used >= t * st.token_budget}

    # ── availability ───────────────────────────────────────────────────
    def available(self, name: str) -> bool:
        st = self.state(name)
        if st.status == "error":
            return False
        if st.status == "cooling":
            return st.reset_at is not None and self.clock() >= st.reset_at   # the next call is the recovery probe
        return True

    def next_available_at(self, names: list[str]) -> float | None:
        times = [self.state(n).reset_at for n in names if self.state(n).status == "cooling" and self.state(n).reset_at]
        return min(times) if times else None

    def snapshot(self) -> dict[str, dict[str, Any]]:
        now = self.clock()
        out = {}
        for n, st in self.states.items():
            out[n] = {"status": st.status, "reset_in_s": max(0, round(st.reset_at - now)) if st.reset_at and st.status == "cooling" else None,
                      "tokens_used": st.tokens_used, "token_budget": st.token_budget, "error": st.error}
            if st.token_budget:
                out[n]["used_pct"] = round(100 * st.tokens_used / st.token_budget, 3)
        return out

    # ── outcomes ───────────────────────────────────────────────────────
    async def on_success(self, name: str, *, model: str, bot_id: str | None, job_id: str | None,
                         tokens_in: int | None, tokens_out: int | None, estimated: bool, latency_ms: int, status_code: int = 200) -> None:
        st = self.state(name)
        recovered = st.status == "cooling"
        st.status, st.consecutive_429, st.error = "ok", 0, None
        st.reset_at = None
        st.tokens_used += (tokens_in or 0) + (tokens_out or 0)
        await self._record(name, model, bot_id, job_id, tokens_in, tokens_out, estimated, status_code, latency_ms)
        await self._persist(st)
        if recovered:
            await self._emit("provider_recovered", {"provider": name})
        await self._check_reservoir(st)

    async def on_rate_limited(self, name: str, *, retry_after: float | None, model: str, bot_id: str | None, job_id: str | None, latency_ms: int) -> float:
        st = self.state(name)
        st.consecutive_429 += 1
        cooldown = retry_after if retry_after is not None else min(MAX_COOLDOWN, DEFAULT_COOLDOWN * 2 ** (st.consecutive_429 - 1))
        now = self.clock()
        st.status, st.last_429_at, st.reset_at = "cooling", now, now + max(1.0, cooldown)
        await self._record(name, model, bot_id, job_id, None, None, False, 429, latency_ms)
        await self._persist(st)
        await self._emit("provider_cooling", {"provider": name, "reset_in_s": round(st.reset_at - now), "from_header": retry_after is not None})
        log.warning("%s rate-limited; cooling for %.0fs", name, st.reset_at - now)
        return st.reset_at

    async def on_error(self, name: str, *, status_code: int | None, message: str, fatal: bool, model: str, bot_id: str | None, job_id: str | None, latency_ms: int) -> None:
        st = self.state(name)
        if fatal:                      # e.g. 401/403: needs the user to fix the key in Omni
            st.status, st.error = "error", message.split("\n")[0][:300]
            await self._persist(st)
            await self._emit("provider_error", {"provider": name, "error": st.error})
        await self._record(name, model, bot_id, job_id, None, None, False, status_code, latency_ms)

    def clear_error(self, name: str) -> None:
        """Called when Omni's config reloads (the user may have fixed the key)."""
        st = self.state(name)
        if st.status == "error":
            st.status, st.error = "ok", None

    # ── reservoir ──────────────────────────────────────────────────────
    async def _check_reservoir(self, st: ProviderState) -> None:
        if not st.token_budget:
            return
        for t in RESERVOIR_THRESHOLDS:
            if t not in st.thresholds_hit and st.tokens_used >= t * st.token_budget:
                st.thresholds_hit.add(t)
                await self._emit("reservoir_threshold", {"provider": st.name, "threshold": t, "tokens_used": st.tokens_used, "token_budget": st.token_budget})

    async def forecast(self, name: str = MINIMAX, window_hours: float = 24.0) -> dict[str, Any]:
        """Burn rate over the last window and days until the reservoir is empty."""
        st = self.state(name)
        rate = None
        if self.db:
            since = iso(self.clock() - window_hours * 3600)
            row = await self.db.read_one(
                "SELECT COALESCE(SUM(COALESCE(tokens_in,0)+COALESCE(tokens_out,0)),0) AS t FROM provider_usage_events WHERE provider=? AND created_at >= ?",
                (name, since))
            rate = (row["t"] if row else 0) / window_hours * 24      # tokens per day
        left = (st.token_budget - st.tokens_used) if st.token_budget else None
        return {"provider": name, "tokens_used": st.tokens_used, "token_budget": st.token_budget, "tokens_left": left,
                "tokens_per_day": rate, "days_left": (left / rate) if (left is not None and rate) else None}

    # ── persistence ────────────────────────────────────────────────────
    async def _record(self, name, model, bot_id, job_id, tin, tout, estimated, status, latency) -> None:
        if self.db:
            await self.db.write(
                "INSERT INTO provider_usage_events (provider, model, bot_id, job_id, tokens_in, tokens_out, estimated, status_code, latency_ms, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (name, model, bot_id, job_id, tin, tout, int(estimated), status, latency, iso(self.clock())))

    async def _persist(self, st: ProviderState) -> None:
        if self.db:
            await self.db.write(
                "INSERT INTO providers (name, status, reset_at, last_429_at, tokens_used, token_budget, usage_pct) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET status=excluded.status, reset_at=excluded.reset_at, last_429_at=excluded.last_429_at, "
                "tokens_used=excluded.tokens_used, token_budget=excluded.token_budget, usage_pct=excluded.usage_pct",
                (st.name, st.status, iso(st.reset_at), iso(st.last_429_at), st.tokens_used, st.token_budget,
                 (100.0 * st.tokens_used / st.token_budget) if st.token_budget else 0))

    async def _emit(self, kind: str, data: dict[str, Any]) -> None:
        if self.on_event:
            r = self.on_event(kind, data)
            if r is not None and hasattr(r, "__await__"):
                await r
