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
# The launch intro (7 s, follows the real startup; click or Esc skips it).
splash = true

[output]
# Where the bots' projects go: one folder per goal, named "<date> <goal words>" (PLAN.md A11.m.01).
# Empty = not chosen yet: the app asks on its next start.
folder = ""

[omni]
# Leave empty to auto-detect (%OMNI_HOME%, then ~/.omni).
install_root = ""

[engine]
shutdown_timeout_seconds = 10

[backup]
# PLAN.md A16.c. A backup of ~/.omnibots (database, bots' memory, settings, sessions, skills; never
# secrets or browser profiles) every `every_hours`, and before every database upgrade.
# The newest `keep` of each kind are kept (tray → Backups → Restore…).
every_hours = 24
keep = 7
# Housekeeping (weekly, while the team is idle): board messages, bot events and usage rows older than
# retention_days are deleted (usage is kept as daily totals); the audit log keeps audit_days.
retention_days = 90
audit_days = 365

[keep_awake]
enabled = true

[orchestrator]
# Spawn governor (PLAN.md A7.a.04): limits on the boss creating new bots.
max_bots = 12
max_spawn_per_goal = 5
max_spawn_per_minute = 3
require_approval_for_new_bots = false
# Time budget for one boss WORK turn (thinking and tools). Listening is not part of it:
# wait_for_mention does not spend this budget, and the standing listen loop has no time limit.
goal_minutes = 30
stall_minutes = 5
# How often each bot reads the board and Omi checks project folders for changes.
listen_seconds = 5
# PLAN.md A15.b.03: a project folder must be quiet this long before Omi checks the changes
# (one burst of edits = one check, yours included).
watch_settle_seconds = 180
# PLAN.md A15.d: when a round of a goal runs out of time (goal_minutes) or results arrive between rounds,
# Omi carries on in a new round by itself, at most this many per project per day (then it asks you).
rounds_per_day = 8

[skills]
# Extra skill libraries the bots can use (on top of Omni's skills and ~/.omnibots/skills).
# Every SKILL.md below these folders counts; folders starting with "." (archives) are skipped.
folders = ["C:/.skills/skills"]

[approvals]
# Which actions wait for your click (PLAN.md §3). "R4" (default): only destructive actions
# (deleting, overwriting outside the project, force-push, registry/firewall, secret places)
# and money. "R3": also installs, pushes, network commands, browser clicks, outside-folder access.
ask_from = "R4"

[budgets]
# PLAN.md A9.c.02. Token caps: 0 = no cap (providers' own quotas still apply).
daily_tokens = 0
bot_daily_tokens = 0
# PLAN.md A9.c.03. Background work only (checks, repairs, routines, the night shift): each bot
# gets this many tokens per project per day. Omi and work you start aren't limited. Not a wall:
# a bot that runs out asks Omi, and Omi asks you (tray → Token allocations shows them all). 0 = no cap.
background_tokens_per_bot = 200000
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


def save_setting(path: Path, section: str, key: str, value: str) -> None:
    """Set one string value in settings.toml, keeping the user's other lines and comments."""
    import json
    import re
    text = path.read_text(encoding="utf-8") if path.exists() else DEFAULT_SETTINGS_TOML
    line = f"{key} = {json.dumps(value)}"                     # a JSON string is a valid TOML basic string
    head = re.search(rf"(?m)^\[{re.escape(section)}\]\s*$", text)
    if head is None:
        text = text.rstrip("\n") + f"\n\n[{section}]\n{line}\n"
    else:
        nxt = re.search(r"(?m)^\[", text[head.end():])
        end = head.end() + (nxt.start() if nxt else len(text) - head.end())
        body = text[head.end():end]
        if re.search(rf"(?m)^{re.escape(key)}\s*=", body):
            body = re.sub(rf"(?m)^{re.escape(key)}\s*=.*$", lambda _m: line, body, count=1)
        else:
            body = "\n" + line + body if not body.startswith("\n") else "\n" + line + body
        text = text[:head.end()] + body + text[end:]
    path.write_text(text, encoding="utf-8")
    tomllib.loads(text)                                         # never leave a broken file behind


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
