"""Skills on demand (PLAN.md A8.a.01): Omni's find_skill / invoke_skill pattern.

The skill pool = Omni's skills (bundled + the external index, A1) plus
OmniBots' own skills in ~/.omnibots/skills/<name>/SKILL.md. Bodies are NOT
put in every prompt (hundreds of skills would drown the context): a bot
searches with find_skill and loads one playbook with invoke_skill when a job
needs it. OmniBots' own skills win on a name clash.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from omnibots.omni.config import SkillInfo
from omnibots.omni.frontmatter import parse_frontmatter
from omnibots.runtime.rank import bm25
from omnibots.runtime.tools import Tool, ToolContext

MAX_BODY = 14_000


class SkillPool:
    def __init__(self, omni_skills: Callable[[], list[SkillInfo]], own_dir: Path | None = None,
                 folders: list[Path] | None = None):
        """folders: extra skill libraries (settings [skills] folders, e.g. C:/.skills/skills), read by NAME:
        a skill Omni hides behind another skill's slash command is still there for the bots."""
        self.omni_skills, self.own_dir = omni_skills, own_dir
        self.folders = [Path(f) for f in (folders or [])]
        self._folder_cache: tuple[tuple, list[SkillInfo]] | None = None

    def _from_folders(self) -> list[SkillInfo]:
        key = tuple((str(f), f.stat().st_mtime if f.exists() else 0) for f in self.folders)
        if self._folder_cache and self._folder_cache[0] == key:
            return self._folder_cache[1]
        out: list[SkillInfo] = []
        for root in self.folders:
            if not root.is_dir():
                continue
            for f in sorted(root.rglob("SKILL.md")):
                rel = f.relative_to(root).parts
                if any(part.startswith(".") for part in rel):      # archives like .deleted-duplicates
                    continue
                try:
                    meta, _ = parse_frontmatter(f.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError):
                    continue
                name = meta.get("name") or f.parent.name
                out.append(SkillInfo(name=name, command="/" + name, description=meta.get("description", ""), path=f,
                                     source="library", category=rel[0] if len(rel) > 2 else "Library"))
        self._folder_cache = (key, out)
        return out

    def all(self) -> list[SkillInfo]:
        own: list[SkillInfo] = []
        if self.own_dir and self.own_dir.is_dir():
            for f in sorted(self.own_dir.glob("*/SKILL.md")):
                meta, _ = parse_frontmatter(f.read_text(encoding="utf-8"))
                name = meta.get("name") or f.parent.name
                own.append(SkillInfo(name=name, command="/" + name, description=meta.get("description", ""), path=f,
                                     source="omnibots", category="OmniBots"))
        names = {s.name.lower() for s in own}
        omni = []
        for s in self.omni_skills():                   # Omni can hold two skills with one name (bundled + index): first wins
            if s.name.lower() not in names:
                names.add(s.name.lower())
                omni.append(s)
        extra = []
        for s in self._from_folders():
            if s.name.lower() not in names:
                names.add(s.name.lower())
                extra.append(s)
        return own + omni + extra

    def search(self, query: str, limit: int = 8) -> list[tuple[float, SkillInfo]]:
        """BM25 over name + description (name hits count triple)."""
        name = lambda s: s.name.replace("-", " ").replace("_", " ")
        return bm25(query, [(s, f"{name(s)} {name(s)} {name(s)} {s.description}") for s in self.all()])[:limit]

    def get(self, name: str) -> SkillInfo | None:
        """An exact NAME wins over a slash command (invoke_skill("python") must not load python-coding's /python)."""
        key = name.strip().lstrip("/").lower()
        skills = self.all()
        return (next((s for s in skills if s.name.lower() == key), None)
                or next((s for s in skills if s.command.lstrip("/").lower() == key), None))


def skill_tools(pool: SkillPool) -> list[Tool]:
    async def find_skill(args: dict[str, Any], ctx: ToolContext) -> str:
        hits = pool.search(str(args.get("query") or ""), int(args.get("limit") or 8))
        if not hits:
            return "no matching skills (try other words)"
        return "\n".join(f"- {s.name} [{s.source}]: {s.description[:160]}" for _, s in hits) + \
            "\nLoad one with invoke_skill(name) to read its instructions."

    async def invoke_skill(args: dict[str, Any], ctx: ToolContext) -> str:
        s = pool.get(str(args.get("name") or ""))
        if not s:
            return f"ERROR: no skill named {args.get('name')!r} (use find_skill)"
        try:
            body = s.body()
        except OSError as exc:
            return f"ERROR: could not read the skill: {exc}"
        if len(body) > MAX_BODY:
            body = body[:MAX_BODY] + "\n…[skill truncated]"
        await ctx.event("console", f"  📘 skill loaded: {s.name}")
        return f"# Skill: {s.name}\n{s.description}\n\n{body}\n\n(Follow these instructions for the current job where they apply.)"

    return [
        Tool("find_skill", "Search the skill pool for instructions/playbooks that help with a task (e.g. 'deploy static site', 'write pytest tests').",
             {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]},
             "R0", find_skill, path_arg=None, summary=lambda a: f"find_skill \"{a.get('query', '')}\""),
        Tool("invoke_skill", "Load a skill's full instructions by name (from find_skill) and follow them.",
             {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
             "R0", invoke_skill, path_arg=None, summary=lambda a: f"invoke_skill {a.get('name')}"),
    ]
