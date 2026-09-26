"""OmniBots-only app settings (settings.toml in the home folder).

Never put provider API keys here: providers and keys belong to Omni and are
read from Omni's own files (PLAN.md A1). Secrets the bots use go in the vault.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

DEFAULT_SETTINGS_TOML = """\
# OmniBots settings. Provider keys are NOT stored here (they come from Omni).

[app]
log_level = "INFO"

[omni]
# Leave empty to auto-detect (%OMNI_HOME%, then ~/.omni).
install_root = ""

[engine]
shutdown_timeout_seconds = 10

[keep_awake]
enabled = true

[orchestrator]
# Spawn governor (PLAN.md A7.a.04): limits on the boss creating new bots.
max_bots = 12
max_spawn_per_goal = 5
max_spawn_per_minute = 3
require_approval_for_new_bots = false
# Time budget for one goal, and when a silent bot counts as stalled.
goal_minutes = 30
stall_minutes = 5

[budgets]
# PLAN.md A9.c.02. Token caps: 0 = no cap (providers' own quotas still apply).
daily_tokens = 0
bot_daily_tokens = 0
# Money caps (USD) for R4 actions; every R4 action ALSO needs your click.
money_per_task_usd = 2.0
money_per_bot_day_usd = 5.0
money_per_day_usd = 10.0

[mcp_risk]
# Risk class for MCP tools from Omni's mcpServers (PLAN.md A8.b.02): "server.tool"
# or "server.*". Anything not listed is R3 (the user approves each call).
"okf.okf_search" = "R0"
"okf.okf_get" = "R0"
"okf.okf_list" = "R0"
"okf.okf_browse" = "R0"
"""


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_settings(path: Path) -> dict[str, Any]:
    """Read settings.toml, creating it with defaults on first run.

    Values missing from the user's file fall back to the defaults, so a file
    written by an older version keeps working.
    """
    defaults = tomllib.loads(DEFAULT_SETTINGS_TOML)
    if not path.exists():
        path.write_text(DEFAULT_SETTINGS_TOML, encoding="utf-8")
        return defaults
    with path.open("rb") as f:
        return _merge(defaults, tomllib.load(f))
