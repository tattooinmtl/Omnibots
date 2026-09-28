"""Playbooks and the retrospective (PLAN.md A8.c.01–02, §4.4).

A playbook is a reusable recipe learned from real goals: when to use it,
steps, decision rules, required outputs and approval limits (markdown in
`playbooks.body_md`), plus stats from `playbook_runs` (runs, success rate,
average tokens and time).

  lookup      plan_goal asks for the best playbook for the goal FIRST and gives
              it to the Planner as context.
  variants    a name can have one `active` version and `variant`s. While any of
              them has fewer than TRIALS runs, the least-tried one is picked;
              after that the winner (success rate, then fewer tokens) becomes
              the active version and the others are retired.
  retirement  a version whose last FAIL_STREAK runs all failed is retired.
  retro       after every goal: Omi's lessons go to its memory.md; a successful
              goal with no playbook becomes a new one; a failed run with a
              playbook produces an improved variant. Each change is announced
              with PLAYBOOK_UPDATED on #orchestrator.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any

from omnibots.board.types import ORCHESTRATOR
from omnibots.runtime.rank import bm25, words

log = logging.getLogger(__name__)

TRIALS = 3          # runs each variant gets before a winner is chosen
FAIL_STREAK = 3     # consecutive failures that retire a version


@dataclass
class Playbook:
    id: str
    name: str
    version: int
    parent_id: str | None
    body_md: str
    status: str
    runs: int = 0
    successes: int = 0
    avg_tokens: float = 0.0
    avg_seconds: float = 0.0

    @property
    def success_rate(self) -> float:
        return self.successes / self.runs if self.runs else 0.0

    def stats_line(self) -> str:
        if not self.runs:
            return "no runs yet"
        return f"{self.runs} runs, {self.success_rate:.0%} success, ~{self.avg_tokens:,.0f} tokens, ~{self.avg_seconds / 60:.1f} min"


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "playbook"


class PlaybookStore:
    def __init__(self, db, bus=None):
        self.db, self.bus = db, bus

    async def _rows(self, where: str = "1=1", params: tuple = ()) -> list[Playbook]:
        rows = await self.db.read(
            "SELECT p.*, COUNT(r.id) AS runs, COALESCE(SUM(r.success),0) AS ok, COALESCE(AVG(r.tokens),0) AS tok, "
            "COALESCE(AVG(r.seconds),0) AS sec FROM playbooks p LEFT JOIN playbook_runs r ON r.playbook_id = p.id "
            f"WHERE {where} GROUP BY p.id ORDER BY p.name, p.version", params)
        return [Playbook(r["id"], r["name"], r["version"], r["parent_id"], r["body_md"], r["status"],
                         r["runs"], r["ok"], float(r["tok"]), float(r["sec"])) for r in rows]

    async def get(self, pid: str) -> Playbook | None:
        rows = await self._rows("p.id=?", (pid,))
        return rows[0] if rows else None

    async def all(self, *, include_retired: bool = False) -> list[Playbook]:
        return await self._rows("1=1" if include_retired else "p.status != 'retired'")

    async def _announce(self, pb: Playbook, change: str, project_id: str | None = None) -> None:
        if self.bus:
            await self.bus.publish(ORCHESTRATOR, "PLAYBOOK_UPDATED",
                                   {"text": f"playbook {pb.name} v{pb.version}: {change}", "playbook_id": pb.id,
                                    "name": pb.name, "version": pb.version, "status": pb.status, "change": change},
                                   sender_type="bot", sender_id="omi", project_id=project_id)

    async def create(self, name: str, body_md: str, *, parent_id: str | None = None, variant: bool = False,
                     project_id: str | None = None) -> Playbook:
        """A new playbook, or a new version of one. variant=True keeps the current active
        version and A/B-tests the new one against it; otherwise the new version replaces it."""
        name = slug(name)
        cur = await self._rows("p.name=?", (name,))
        version = max((p.version for p in cur), default=0) + 1
        active = next((p for p in cur if p.status == "active"), None)
        status = "variant" if (variant and active) else "active"
        if status == "active" and active:
            await self.db.write("UPDATE playbooks SET status='retired' WHERE id=?", (active.id,))
        pid = f"pb_{uuid.uuid4().hex[:10]}"
        await self.db.write("INSERT INTO playbooks (id, name, version, parent_id, body_md, status) VALUES (?,?,?,?,?,?)",
                            (pid, name, version, parent_id or (active.id if active else None), body_md.strip() + "\n", status))
        pb = await self.get(pid)
        await self._announce(pb, "new variant (A/B test)" if status == "variant" else ("new version" if version > 1 else "created"), project_id)
        return pb

    async def find(self, goal: str, limit: int = 3) -> list[Playbook]:
        """Playbooks that fit a goal (name + 'when to use' + steps), best first."""
        cands = await self._rows("p.status IN ('active','variant')")
        need = max(2, round(len(set(words(goal))) * 0.3))
        docs = [(p, f"{p.name.replace('-', ' ')} {p.name.replace('-', ' ')} {p.body_md[:1500]}") for p in cands]
        return [p for _, p in bm25(goal, docs, min_terms=need)][:limit]

    async def choose(self, goal: str) -> Playbook | None:
        """The playbook to run for this goal: among the best-matching name's versions,
        the least-tried while any is under TRIALS runs, else the best."""
        hits = await self.find(goal, 1)
        if not hits:
            return None
        versions = [p for p in await self._rows("p.name=? AND p.status IN ('active','variant')", (hits[0].name,))]
        if len(versions) > 1:
            fresh = [p for p in versions if p.runs < TRIALS]
            if fresh:
                return min(fresh, key=lambda p: (p.runs, p.version))
        return max(versions, key=lambda p: (p.success_rate, -p.avg_tokens))

    async def record_run(self, playbook_id: str, *, project_id: str | None, success: bool, tokens: int = 0,
                         seconds: float = 0.0) -> Playbook | None:
        await self.db.write("INSERT INTO playbook_runs (playbook_id, project_id, success, tokens, seconds) VALUES (?,?,?,?,?)",
                            (playbook_id, project_id, int(success), int(tokens), float(seconds)))
        pb = await self.get(playbook_id)
        if pb is None:
            return None
        last = await self.db.read("SELECT success FROM playbook_runs WHERE playbook_id=? ORDER BY id DESC LIMIT ?",
                                  (playbook_id, FAIL_STREAK))
        if pb.status != "retired" and len(last) == FAIL_STREAK and not any(r["success"] for r in last):
            await self.db.write("UPDATE playbooks SET status='retired' WHERE id=?", (playbook_id,))
            pb = await self.get(playbook_id)
            await self._announce(pb, f"retired after {FAIL_STREAK} failed runs in a row", project_id)
            await self._promote_if_orphaned(pb.name, project_id)
        await self._settle(pb.name, project_id)
        return await self.get(playbook_id)

    async def _promote_if_orphaned(self, name: str, project_id: str | None) -> None:
        live = await self._rows("p.name=? AND p.status IN ('active','variant')", (name,))
        if live and not any(p.status == "active" for p in live):
            best = max(live, key=lambda p: (p.success_rate, -p.avg_tokens))
            await self.db.write("UPDATE playbooks SET status='active' WHERE id=?", (best.id,))
            await self._announce(await self.get(best.id), "promoted to active", project_id)

    async def _settle(self, name: str, project_id: str | None) -> None:
        """Every version has had its trials: keep the winner, retire the rest."""
        live = await self._rows("p.name=? AND p.status IN ('active','variant')", (name,))
        if len(live) < 2 or any(p.runs < TRIALS for p in live):
            return
        win = max(live, key=lambda p: (p.success_rate, -p.avg_tokens))
        for p in live:
            if p.id != win.id:
                await self.db.write("UPDATE playbooks SET status='retired' WHERE id=?", (p.id,))
        if win.status != "active":
            await self.db.write("UPDATE playbooks SET status='active' WHERE id=?", (win.id,))
        await self._announce(await self.get(win.id), f"won the A/B test ({win.stats_line()})", project_id)


# ── retrospective (A8.c.02) ────────────────────────────────────────────────
RETRO_PROMPT = """You run the retrospective of a finished multi-bot goal for the boss bot, Omi.
Reply with ONE JSON object, nothing else:
{"lessons": ["0-4 short reusable lessons for Omi (what to repeat or avoid when running goals)"],
 "playbook": {"name": "short-kebab-name for this KIND of goal", "body": "markdown"} or null}
The playbook body has these sections: ## When to use, ## Steps (numbered; which roles/tools/lanes), ## Decision rules,
## Required outputs, ## Approval limits. Keep it general (no one-off names, dates or numbers from this run).
Give a playbook only when the goal is a repeatable kind of work. When an existing playbook is shown, write the improved
version of it (same name) that fixes what went wrong."""


def _json_obj(text: str) -> dict[str, Any] | None:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        v = json.loads(m.group(0))
        return v if isinstance(v, dict) else None
    except ValueError:
        return None


async def retrospective(*, router, chain: list[str], store: PlaybookStore, boss_memory, goal: str, report: str,
                        success: bool, project_id: str, playbook: Playbook | None, tokens: int, seconds: float,
                        user_verdicts: list[str] | None = None) -> dict[str, Any]:
    """Record the playbook run, learn lessons, create or improve a playbook. Never raises."""
    out: dict[str, Any] = {"lessons": [], "playbook": None, "recorded": False}
    try:
        if playbook:
            await store.record_run(playbook.id, project_id=project_id, success=success, tokens=tokens, seconds=seconds)
            out["recorded"] = True
        if success and playbook and not any(v.startswith("👎") for v in user_verdicts or []):
            return out                  # the recipe worked as written; its stats say so (and you haven't said otherwise)
        from omnibots.runtime.context import today_line
        user = [f"GOAL:\n{goal}", f"OUTCOME: {'succeeded' if success else 'did not succeed'}", f"REPORT:\n{report[:12000]}"]
        if user_verdicts:               # A16.b.02: the user's own verdicts count more than the bots' self-grading
            user.insert(0, "THE USER'S VERDICTS ON EARLIER RUNS OF THIS PLAYBOOK (newest first; they outweigh the bots' own "
                           "grading):\n" + "\n".join(f"- {v}" for v in user_verdicts))
        if playbook:
            user.append(f"PLAYBOOK THAT WAS FOLLOWED ({playbook.name} v{playbook.version}, {playbook.stats_line()}):\n{playbook.body_md}")
        routed = await router.chat("omi:retro", chain, [{"role": "system", "content": RETRO_PROMPT + "\n" + today_line()},
                                                        {"role": "user", "content": "\n\n".join(user)}], None, priority="review")
        data = _json_obj(routed.result.answer) or {}
        lessons = [str(x).strip().lstrip("- ").strip() for x in (data.get("lessons") or []) if str(x).strip()][:4]
        if lessons:
            boss_memory.add_lessons(lessons)
            out["lessons"] = lessons
        pbd = data.get("playbook") if isinstance(data.get("playbook"), dict) else None
        if pbd and str(pbd.get("body") or "").strip():
            if playbook:        # a failed run: an improved variant to A/B against the current one
                pb = await store.create(playbook.name, str(pbd["body"]), parent_id=playbook.id, variant=True, project_id=project_id)
            elif success:
                pb = await store.create(str(pbd.get("name") or goal[:40]), str(pbd["body"]), project_id=project_id)
            else:
                pb = None
            out["playbook"] = pb
    except Exception as exc:
        log.warning("retrospective for %s failed: %s", project_id, exc)
        out["error"] = str(exc)
    return out
