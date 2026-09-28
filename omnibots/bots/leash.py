"""The leash (PLAN.md A15.b): bots may work on their own, and the user can see and stop it.

- Every job records its origin: who or what started it. `user` is you (a goal,
  a chat, a note). Everything else is background work.
- Each project has an autonomy dial. off: the bots start nothing on their own
  there. watch (the default): Omi may look and report, but starts no fix. fix:
  a check may open repair jobs.
- "Pause background work" holds every background start, in every project,
  until it's turned off. It survives a restart. Panic and Stop are separate.

Work you start is never held back by the leash.

The origin travels with the work: `runner.run` sets CURRENT_ORIGIN for the
run, and asyncio tasks copy it, so a job Omi creates inside a run inherits it
(a job created by a `watch` check is a `fix`).
"""

from __future__ import annotations

import json
from contextvars import ContextVar

ORIGINS = ("user", "routine", "trigger", "night", "watch", "fix", "continue", "relay")
LEVELS = ("off", "watch", "fix")
DEFAULT_LEVEL = "watch"                        # user, 2026-09-27
PAUSE_KEY = "background_paused"

CURRENT_ORIGIN: ContextVar[str] = ContextVar("omnibots_origin", default="user")

WHY = {"user": "you started it", "routine": "a routine", "trigger": "a trigger", "night": "the night shift",
       "watch": "Omi checking the project after its files changed", "fix": "a repair from Omi's check",
       "continue": "unfinished work carried on", "relay": "a tool request from another bot"}


def child_origin(origin: str) -> str:
    """The origin of work created inside a run of `origin`."""
    return "fix" if origin == "watch" else origin


class Leash:
    def __init__(self, db):
        self.db = db
        self.paused = False

    async def load(self) -> "Leash":
        row = await self.db.read_one("SELECT value FROM parameters WHERE scope='global' AND bot_id IS NULL AND key=?", (PAUSE_KEY,))
        self.paused = bool(row and row["value"] == "1")
        return self

    async def set_paused(self, on: bool) -> bool:
        # parameters has UNIQUE(scope, bot_id, key), but NULL bot_ids never collide: replace by hand
        await self.db.write("DELETE FROM parameters WHERE scope='global' AND bot_id IS NULL AND key=?", (PAUSE_KEY,))
        await self.db.write("INSERT INTO parameters (scope, bot_id, key, value) VALUES ('global', NULL, ?, ?)",
                            (PAUSE_KEY, "1" if on else "0"))
        await self.db.audit("user", None, "background_paused" if on else "background_resumed", "{}")
        self.paused = bool(on)
        return self.paused

    async def level(self, project_id: str | None) -> str:
        if not project_id:
            return DEFAULT_LEVEL
        row = await self.db.read_one("SELECT autonomy FROM projects WHERE id=?", (project_id,))
        return row["autonomy"] if row and row["autonomy"] in LEVELS else DEFAULT_LEVEL

    async def set_level(self, project_id: str, level: str) -> str:
        if level not in LEVELS:
            raise ValueError(f"autonomy must be one of {', '.join(LEVELS)}")
        await self.db.write("UPDATE projects SET autonomy=? WHERE id=?", (level, project_id))
        await self.db.audit("user", None, "autonomy_set", json.dumps({"project": project_id, "autonomy": level}))
        return level

    async def may_start(self, origin: str, project_id: str | None) -> str | None:
        """None when work of this origin may start in this project now; otherwise why not."""
        if origin == "user":
            return None
        if self.paused:
            return "background work is paused (tray → Pause background work)"
        if not project_id:
            return None                        # routines, triggers and the night shift start new projects
        level = await self.level(project_id)
        if level == "off":
            return "this project's autonomy is Off"
        if level == "watch" and origin == "fix":
            return "this project is on Watch: Omi reports problems but doesn't start fixes (switch it to Fix)"
        return None
