"""The skill & tool pools a JobRunner draws from (PLAN.md A8), built in one place
for the engine, tools/run_goal.py and the live tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from omnibots.lineup import LINEUP_MODELS, MINIMAX
from omnibots.mcp_client import MCPManager
from omnibots.runtime.minimax_tools import minimax_tools
from omnibots.runtime.skill_tools import SkillPool


def runner_pools(config: Callable[[], Any], home: Path, router, locks=None, mcp_risk: dict[str, str] | None = None) -> dict[str, Any]:
    """JobRunner keyword arguments: locks, skill_pool, media_tools, mcp. `config` returns
    the current OmniConfig (or None). Close `mcp` on shutdown."""

    async def vision_chat(messages: list[dict[str, Any]], bot_id: str, job_id: str | None) -> str:
        # MiniMax M3 reads the image under the calling bot's own seat (seats are re-entrant per bot).
        routed = await router.chat(bot_id, [LINEUP_MODELS[MINIMAX]], messages, None, job_id=job_id)
        return routed.result.answer.strip() or "ERROR: the vision model returned nothing"

    cfg = lambda: config()
    return {
        "locks": locks,
        "skill_pool": SkillPool(lambda: (cfg().skills if cfg() else []), home / "skills"),
        "media_tools": minimax_tools(lambda: (cfg().providers.get(MINIMAX) if cfg() else None), vision_chat=vision_chat),
        "mcp": MCPManager(lambda: (cfg().mcp_servers if cfg() else {}), mcp_risk),
    }
