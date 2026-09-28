"""Bot profiles and the registry (PLAN.md A5.a.01–02, A5.a.05).

A bot is an identity (ADR-11): id, name, role, skills, tools, provider chain,
risk ceiling, limits, and a folder with memory.md, a workspace, artifacts and
logs. It is NOT a running model session; the job runner starts those. Omi, the
boss, has the fixed id `omi`; workers get `bot_<uuid>`.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from omnibots.board.types import GENERAL
from omnibots.bots.memory import MemoryFile, template
from omnibots.lineup import MINIMAX_FIRST
from omnibots.runtime.tools import RISK_ORDER

BOSS_ID = "omi"
# A15.e.03: Omi reads, and does small jobs itself (a named list, not every tool: A8.b.03).
BOSS_TOOLS = ["read_file", "list_dir", "write_file", "run_python", "grep", "find_files", "web_search", "web_fetch"]   # + search (A8.d.01)
# A8.d.01: search is a default sense (R2, automatic); every other tool still has to be on the profile (A8.b.03)
DEFAULT_TOOLS = ["read_file", "write_file", "list_dir", "run_python", "grep", "find_files", "web_search", "web_fetch",
                 "find_skill", "invoke_skill", "ask_help"]
USER_PROFILE_TEMPLATE = """# User Profile

> Who the user is and how they like things. Every bot reads this before each job.
> The user edits it freely; Omi may propose changes, which need the user's approval.

# About the user

# Preferences

# Standing instructions
"""


@dataclass
class BotProfile:
    id: str
    name: str
    role: str
    description: str = ""
    status: str = "idle"
    chain: list[str] = field(default_factory=lambda: list(MINIMAX_FIRST))
    multi_provider: bool = True        # A15.a.03: on = fail over across the whole chain; off = stay on the first provider
    risk_ceiling: str = "R3"
    limits: dict[str, Any] = field(default_factory=dict)
    skills: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=lambda: list(DEFAULT_TOOLS))
    created_by: str = "user"
    created_at: str = ""
    last_active_at: str | None = None
    folder: Path = Path(".")

    @property
    def is_boss(self) -> bool:
        return self.id == BOSS_ID

    @property
    def memory(self) -> MemoryFile:
        return MemoryFile(self.folder / "memory.md")

    @property
    def workspace(self) -> Path:
        return self.folder / "workspace"

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        d["folder"] = str(self.folder)
        return d


class ProfileError(ValueError):
    pass


class BotRegistry:
    def __init__(self, db, bots_dir: Path, bus=None):
        self.db, self.bots_dir, self.bus = db, bots_dir, bus

    # ── create / read ──────────────────────────────────────────────────
    async def create(self, name: str, role: str, *, description: str = "", chain: list[str] | None = None,
                     skills: list[str] | None = None, tools: list[str] | None = None, risk_ceiling: str = "R3",
                     limits: dict[str, Any] | None = None, multi_provider: bool = True, created_by: str = "user",
                     bot_id: str | None = None) -> BotProfile:
        name, role = (name or "").strip(), (role or "").strip()
        if not name or not role:
            raise ProfileError("a bot needs a name and a role")
        if risk_ceiling not in RISK_ORDER:
            raise ProfileError(f"risk ceiling must be one of {RISK_ORDER}")
        chain = list(chain) if chain is not None else list(MINIMAX_FIRST)
        if not chain or not all(isinstance(c, str) and c.strip() for c in chain):
            raise ProfileError("a bot needs at least one model in its provider chain")
        bid = bot_id or f"bot_{uuid.uuid4().hex[:12]}"
        if await self.db.read_one("SELECT id FROM bots WHERE id=?", (bid,)):
            raise ProfileError(f"bot {bid} already exists")
        folder = self.bots_dir / bid
        for sub in ("workspace", "artifacts", "logs"):
            (folder / sub).mkdir(parents=True, exist_ok=True)
        mem = folder / "memory.md"
        if not mem.exists():
            mem.write_text(template(bid, name, role), encoding="utf-8", newline="\n")
        prof = BotProfile(id=bid, name=name, role=role, description=description, chain=chain, multi_provider=multi_provider,
                          risk_ceiling=risk_ceiling, limits=dict(limits or {}), skills=list(skills or []),
                          tools=list(tools) if tools is not None else list(DEFAULT_TOOLS), created_by=created_by, folder=folder)
        await self.db.write(
            "INSERT INTO bots (id, name, role, description, status, provider_chain_json, multi_provider, risk_ceiling, limits_json, "
            "profile_json, memory_path, workspace_path, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (bid, name, role, description, "idle", json.dumps(chain), int(multi_provider), risk_ceiling, json.dumps(prof.limits),
             json.dumps({}), str(mem), str(prof.workspace), created_by))
        await self._set_links(bid, prof.skills, prof.tools)
        if self.bus:
            await self.bus.publish(GENERAL, "BOT_CREATED", {"bot_id": bid, "name": name, "role": role, "created_by": created_by},
                                   sender_type="system" if created_by == "user" else "bot",
                                   sender_id=None if created_by == "user" else created_by)
        await self.db.audit("user" if created_by == "user" else "bot", created_by, "bot_created", json.dumps({"bot_id": bid, "role": role}))
        return await self.get(bid)

    async def ensure_boss(self) -> BotProfile:
        existing = await self.get(BOSS_ID)
        if existing:
            missing = [t for t in BOSS_TOOLS if t not in existing.tools]
            if missing:                                   # A15.e.03: an older Omi gets the small-job tools too
                return await self.update(BOSS_ID, tools=existing.tools + missing)
            return existing
        return await self.create("Omi", "boss", description="The boss: plans, relays work to the team, checks the evidence, reports to the user.",
                                 chain=list(MINIMAX_FIRST), tools=list(BOSS_TOOLS), risk_ceiling="R3", bot_id=BOSS_ID)

    async def get(self, bot_id: str) -> BotProfile | None:
        r = await self.db.read_one("SELECT * FROM bots WHERE id=?", (bot_id,))
        if not r:
            return None
        skills = [x["skill_id"] for x in await self.db.read("SELECT skill_id FROM bot_skills WHERE bot_id=? ORDER BY rowid", (bot_id,))]
        tools = [x["tool_id"] for x in await self.db.read("SELECT tool_id FROM bot_tools WHERE bot_id=? ORDER BY rowid", (bot_id,))]
        return BotProfile(id=r["id"], name=r["name"], role=r["role"], description=r["description"] or "", status=r["status"],
                          chain=json.loads(r["provider_chain_json"] or "[]"), multi_provider=bool(r["multi_provider"]),
                          risk_ceiling=r["risk_ceiling"], limits=json.loads(r["limits_json"] or "{}"), skills=skills, tools=tools,
                          created_by=r["created_by"] or "", created_at=r["created_at"], last_active_at=r["last_active_at"],
                          folder=Path(r["memory_path"]).parent if r["memory_path"] else self.bots_dir / r["id"])

    async def list(self, *, include_archived: bool = False) -> list[BotProfile]:
        rows = await self.db.read("SELECT id FROM bots" + ("" if include_archived else " WHERE status != 'archived'") +
                                  " ORDER BY CASE WHEN id=? THEN 0 ELSE 1 END, created_at", (BOSS_ID,))
        return [p for p in [await self.get(r["id"]) for r in rows] if p]

    # ── edit / archive ─────────────────────────────────────────────────
    async def update(self, bot_id: str, **changes: Any) -> BotProfile:
        prof = await self.get(bot_id)
        if not prof:
            raise ProfileError(f"no bot {bot_id}")
        allowed = {"name", "role", "description", "chain", "skills", "tools", "risk_ceiling", "limits", "multi_provider"}
        bad = set(changes) - allowed
        if bad:
            raise ProfileError(f"can't change {sorted(bad)}")
        if "risk_ceiling" in changes and changes["risk_ceiling"] not in RISK_ORDER:
            raise ProfileError(f"risk ceiling must be one of {RISK_ORDER}")
        if "chain" in changes and not changes["chain"]:
            raise ProfileError("a bot needs at least one model in its provider chain")
        cols = {"name": "name", "role": "role", "description": "description", "risk_ceiling": "risk_ceiling"}
        for k, col in cols.items():
            if k in changes:
                await self.db.write(f"UPDATE bots SET {col}=? WHERE id=?", (str(changes[k]), bot_id))
        if "chain" in changes:
            await self.db.write("UPDATE bots SET provider_chain_json=? WHERE id=?", (json.dumps(list(changes["chain"])), bot_id))
        if "limits" in changes:
            await self.db.write("UPDATE bots SET limits_json=? WHERE id=?", (json.dumps(dict(changes["limits"])), bot_id))
        if "multi_provider" in changes:
            await self.db.write("UPDATE bots SET multi_provider=? WHERE id=?", (int(bool(changes["multi_provider"])), bot_id))
        if "skills" in changes or "tools" in changes:
            await self._set_links(bot_id, changes.get("skills", prof.skills), changes.get("tools", prof.tools))
        await self.db.audit("user", None, "bot_updated", json.dumps({"bot_id": bot_id, "changed": sorted(changes)}))
        return await self.get(bot_id)

    async def archive(self, bot_id: str) -> None:
        if bot_id == BOSS_ID:
            raise ProfileError("Omi, the boss, can't be archived")
        await self.db.write("UPDATE bots SET status='archived' WHERE id=?", (bot_id,))
        await self.db.audit("user", None, "bot_archived", json.dumps({"bot_id": bot_id}))

    async def set_status(self, bot_id: str, status: str) -> None:
        await self.db.write("UPDATE bots SET status=?, last_active_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=? AND status != 'archived'",
                            (status, bot_id))

    async def _set_links(self, bot_id: str, skills: list[str], tools: list[str]) -> None:
        await self.db.write("DELETE FROM bot_skills WHERE bot_id=?", (bot_id,))
        await self.db.write("DELETE FROM bot_tools WHERE bot_id=?", (bot_id,))
        if skills:
            await self.db.write_many("INSERT OR IGNORE INTO bot_skills (bot_id, skill_id) VALUES (?,?)", [(bot_id, s) for s in skills])
        if tools:
            await self.db.write_many("INSERT OR IGNORE INTO bot_tools (bot_id, tool_id) VALUES (?,?)", [(bot_id, t) for t in tools])


def user_profile_path(home: Path) -> Path:
    p = home / "user_profile.md"
    if not p.exists():
        p.write_text(USER_PROFILE_TEMPLATE, encoding="utf-8", newline="\n")
    return p


def user_profile_text(home: Path, max_chars: int = 4000) -> str:
    """The profile without its template boilerplate; empty when the user hasn't filled it in."""
    text = user_profile_path(home).read_text(encoding="utf-8")
    body = [l for l in text.splitlines() if l.strip() and not l.startswith(">") and not l.startswith("#")]
    return "\n".join(body)[:max_chars] if body else ""
