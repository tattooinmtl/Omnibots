"""The Bot Factory and its spawn governor (PLAN.md A7.a.03–04).

The boss creates the workers it needs, but never without limits:
  - max_bots             total active bots (the boss included)
  - max_spawn_per_goal   new bots for one goal
  - max_spawn_per_minute a rolling minute (the "cycle", audit §9.8)
  - a cost estimate shown before spawning (tokens per job, from history)
  - require_approval_for_new_bots → creating a bot becomes an R3 action
Lane choice: "minimax" (strong reasoning, a seat per job) or "cheap" (the
cheap lane). Quota-aware: when the MiniMax reservoir is ≥ 90% used, new bots
go to the cheap lane regardless.
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from omnibots.bots.profile import BotProfile, BotRegistry
from omnibots.lineup import CHEAP_FIRST, MINIMAX, MINIMAX_FIRST

from omnibots.bots.profile import DEFAULT_TOOLS

# The tool pool (PLAN.md A8.b): what the factory may give a bot. MCP tools are
# assigned as "mcp:<server>" (all its tools) or "mcp:<server>.<tool>".
KNOWN_TOOLS = {
    "read_file", "write_file", "list_dir", "run_python", "grep", "find_files", "run_shell",
    "git_status", "git_diff", "git_commit", "git_push", "web_search", "web_fetch",
    "find_skill", "invoke_skill", "lock_file", "unlock_file", "ask_help",
    "describe_image", "text_to_speech", "generate_video",
    "browser_navigate", "browser_read", "browser_screenshot", "browser_click",
    "browser_type", "browser_fill_secret", "browser_press", "create_tool",
    "computer_start", "computer_stop", "computer_run", "computer_upload", "computer_download",
    "computer_screenshot", "computer_click", "computer_type", "computer_key",
}
_MCP_REF = re.compile(r"^mcp:[A-Za-z0-9_.-]+$")


def is_known_tool(name: str) -> bool:
    return name in KNOWN_TOOLS or bool(_MCP_REF.match(name))


@dataclass
class GovernorLimits:
    max_bots: int = 12
    max_spawn_per_goal: int = 5
    max_spawn_per_minute: int = 3
    require_approval: bool = False


class SpawnRefused(RuntimeError):
    pass


@dataclass
class SpawnGovernor:
    limits: GovernorLimits = field(default_factory=GovernorLimits)
    clock: Any = time.monotonic
    _recent: deque = field(default_factory=deque)
    _per_goal: dict[str, int] = field(default_factory=dict)

    def check(self, *, active_bots: int, goal: str | None) -> None:
        now = self.clock()
        while self._recent and now - self._recent[0] > 60:
            self._recent.popleft()
        if active_bots >= self.limits.max_bots:
            raise SpawnRefused(f"the team is at its limit of {self.limits.max_bots} bots; reuse an idle bot or archive one")
        if goal and self._per_goal.get(goal, 0) >= self.limits.max_spawn_per_goal:
            raise SpawnRefused(f"this goal already created {self.limits.max_spawn_per_goal} bots (the per-goal limit); reuse them")
        if len(self._recent) >= self.limits.max_spawn_per_minute:
            raise SpawnRefused(f"at most {self.limits.max_spawn_per_minute} new bots per minute; wait or reuse a bot")

    def record(self, goal: str | None) -> None:
        self._recent.append(self.clock())
        if goal:
            self._per_goal[goal] = self._per_goal.get(goal, 0) + 1


class BotFactory:
    def __init__(self, registry: BotRegistry, governor: SpawnGovernor, *, db=None, quota=None):
        self.registry, self.governor, self.db, self.quota = registry, governor, db, quota

    async def estimate(self) -> dict[str, Any]:
        """Rough cost of a new worker's job, from the team's history."""
        tokens = None
        if self.db:
            r = await self.db.read_one(
                "SELECT AVG(t) AS avg FROM (SELECT job_id, SUM(COALESCE(tokens_in,0)+COALESCE(tokens_out,0)) AS t "
                "FROM provider_usage_events WHERE job_id IS NOT NULL GROUP BY job_id)")
            tokens = int(r["avg"]) if r and r["avg"] else None
        return {"tokens_per_job": tokens or 20000, "basis": "team history" if tokens else "default guess"}

    def lane_chain(self, lane: str) -> tuple[list[str], str]:
        lane = (lane or "minimax").lower()
        if lane == "minimax" and self.quota is not None:
            st = self.quota.snapshot().get(MINIMAX, {})
            if (st.get("used_pct") or 0) >= 90:
                return list(CHEAP_FIRST), "cheap (the MiniMax budget is 90%+ used)"
        return (list(MINIMAX_FIRST), "minimax") if lane != "cheap" else (list(CHEAP_FIRST), "cheap")

    risk_of = None                        # name -> base risk (set by the engine from the tool pool)

    def ceiling_for(self, tools: list[str]) -> str:
        """A8.d.03: the highest base risk among the bot's tools, never above R3 without the user."""
        order = ["R0", "R1", "R2", "R3", "R4", "R5"]
        risks = [self.risk_of(t) for t in tools] if self.risk_of else []
        top = max([order.index(r) for r in risks if r in order] or [order.index("R2")])
        return order[min(top, order.index("R3"))]

    async def create(self, *, name: str, role: str, description: str = "", skills: list[str] | None = None,
                     tools: list[str] | None = None, lane: str = "minimax", goal: str | None = None,
                     created_by: str = "omi") -> tuple[BotProfile, str]:
        active = len(await self.registry.list())
        self.governor.check(active_bots=active, goal=goal)
        tools = [t for t in (tools or DEFAULT_TOOLS) if is_known_tool(t)]
        tools += [t for t in ("find_skill", "invoke_skill", "ask_help") if t not in tools]   # every worker can learn and ask
        chain, lane_used = self.lane_chain(lane)
        prof = await self.registry.create(name, role, description=description, skills=skills or [], tools=tools,
                                          chain=chain, created_by=created_by, risk_ceiling=self.ceiling_for(tools))
        self.governor.record(goal)
        return prof, lane_used
