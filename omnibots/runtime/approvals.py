"""The approval gate (PLAN.md §3.1, A3.a.04; A9.b builds on it).

R0–R2 run automatically. R3 needs the user's approval unless a pre-approved
scope covers it. R4 (money) and R5 (destructive) ALWAYS need a fresh approval.
A request is a row in `approvals` plus an asyncio future: the bot awaits it
(parked, no spinning) until the user decides from the UI, tray or IPC.
Bots and web content can never grant approvals; only `decide()` can, and
only the UI/IPC layer calls it.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from omnibots.runtime.tools import RISK_ORDER


@dataclass
class Scope:
    """A pre-approval: e.g. R3 `write_file` under C:/sites/demo, 5 times, 1 hour."""
    tool: str
    risk: str
    match: str = ""                    # substring the call summary must contain ("" = any)
    remaining: int = 1
    expires_at: float = field(default_factory=lambda: time.time() + 3600)
    domain: str = ""                   # the call's target host must be this domain or a subdomain ("" = any)

    def covers(self, tool: str, risk: str, summary: str, host: str | None = None) -> bool:
        if self.domain:
            h = (host or "").lower().rstrip(".")
            if not (h == self.domain or h.endswith("." + self.domain)):
                return False
        return (self.tool == tool and risk == self.risk and risk == "R3" and self.remaining > 0
                and time.time() < self.expires_at and self.match in summary)


@dataclass
class Decision:
    approved: bool
    reason: str = ""
    approval_id: str | None = None


class ApprovalCenter:
    def __init__(self, db=None, on_event: Callable[[str, dict[str, Any]], Awaitable[None] | None] | None = None,
                 ask_from: str = "R3"):
        """ask_from: the lowest risk that waits for the user. The app uses settings [approvals]
        ask_from = "R4" (user, 2026-09-26: only destructive actions and money ask; R3 runs)."""
        if ask_from not in ("R3", "R4", "R5"):
            raise ValueError("ask_from must be R3, R4 or R5 (R4 money always asks when R5 is chosen too)")
        self.ask_from = ask_from
        self.db = db
        self.on_event = on_event
        self.pending: dict[str, tuple[asyncio.Future, dict[str, Any]]] = {}
        self.scopes: list[Scope] = []

    def needs_approval(self, risk: str) -> bool:
        if risk == "R4":
            return True                                  # money: always the user's click (standing rule)
        return RISK_ORDER.index(risk) >= RISK_ORDER.index(self.ask_from)

    def pre_approve(self, scope: Scope) -> None:
        if scope.risk != "R3":
            raise ValueError("only R3 can be pre-approved; R4/R5 always need a fresh approval")
        self.scopes.append(scope)

    def _scope_for(self, tool: str, risk: str, summary: str, host: str | None = None) -> Scope | None:
        return next((s for s in self.scopes if s.covers(tool, risk, summary, host)), None)

    async def request(self, *, bot_id: str, job_id: str | None, tool: str, risk: str, summary: str,
                      rehearsal: dict[str, Any] | None = None, timeout: float | None = None,
                      host: str | None = None) -> Decision:
        scope = self._scope_for(tool, risk, summary, host)
        if scope:
            scope.remaining -= 1
            return Decision(True, "pre-approved scope")
        aid = f"ap_{uuid.uuid4().hex[:10]}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        info = {"id": aid, "bot_id": bot_id, "job_id": job_id, "tool": tool, "risk": risk, "summary": summary,
                "host": host, "rehearsal": rehearsal or {}}
        self.pending[aid] = (fut, info)
        if self.db:
            await self.db.write(
                "INSERT INTO approvals (id, job_id, bot_id, risk_class, action, summary, rehearsal_json) VALUES (?,?,?,?,?,?,?)",
                (aid, job_id, bot_id, risk, tool, summary, json.dumps(rehearsal) if rehearsal else None))
        await self._emit("approval_request", info)
        try:
            return await (asyncio.wait_for(fut, timeout) if timeout else fut)
        except asyncio.TimeoutError:
            await self._close(aid, "expired")
            return Decision(False, "the approval request expired")
        except asyncio.CancelledError:
            await self._close(aid, "expired")
            raise
        finally:
            self.pending.pop(aid, None)

    async def decide(self, approval_id: str, approved: bool, reason: str = "") -> bool:
        """Called ONLY by the user-facing layer (window, tray, IPC)."""
        item = self.pending.get(approval_id)
        if not item:
            return False
        fut, info = item
        await self._close(approval_id, "approved" if approved else "denied")
        await self._emit("approval_decision", {**info, "approved": approved, "reason": reason})
        if not fut.done():
            fut.set_result(Decision(approved, reason, approval_id=approval_id))
        return True

    def list_pending(self) -> list[dict[str, Any]]:
        return [info for _, info in self.pending.values()]

    async def _close(self, aid: str, status: str) -> None:
        if self.db:
            await self.db.write("UPDATE approvals SET status=?, decided_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (status, aid))

    async def _emit(self, kind: str, data: dict[str, Any]) -> None:
        if self.on_event:
            r = self.on_event(kind, data)
            if r is not None and hasattr(r, "__await__"):
                await r
