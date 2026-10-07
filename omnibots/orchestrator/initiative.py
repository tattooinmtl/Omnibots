"""Initiative (PLAN.md A17.e.01): goals Omi proposes on its own, and the team's daily journal.

Journal   once a day (the first hourly upkeep of a new day), Omi writes a short entry about the day before from what
          really happened: the goals, the jobs and how they ended, what the user said about the results. Saved in the
          `journal` table and as journal/<day>.md in the OmniBots home. Nothing happened → no entry.
Proposals every `every_hours` (settings [initiative]), when no bot is working and fewer than `max_pending` wait, Omi
          reads the recent journal, the user's profile and the recent goals, and may propose up to two goals that
          would help the user. They wait in the inbox (Omi's chat, the tray, the pipe): **nothing starts until you
          accept one**; an accepted proposal becomes a normal goal. Omi can also propose one while it works
          (boss tool `propose_goal`). Rejected ones are remembered so they aren't proposed again.
Both use the cheap lane with a small token cap; neither has any tools, so neither can act.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

DEFAULTS = {"enabled": True, "every_hours": 24.0, "max_pending": 3, "journal": True}


def _json_array(text: str) -> list[dict[str, Any]]:
    m = re.search(r"\[[\s\S]*\]", text or "")
    try:
        items = json.loads(m.group(0)) if m else []
    except ValueError:
        return []
    return [i for i in items if isinstance(i, dict)]


class Initiative:
    def __init__(self, db, chat: Callable[[list[dict[str, Any]]], Awaitable[str]], home: Path, *, bus=None,
                 settings: dict[str, Any] | None = None, start_goal: Callable[[str, dict], Awaitable[str]] | None = None,
                 today: Callable[[], dt.date] = dt.date.today, user_profile: Callable[[], str] | None = None):
        self.db, self.chat, self.home, self.bus = db, chat, home, bus
        self.settings = {**DEFAULTS, **(settings or {})}
        self.start_goal = start_goal
        self.today = today
        self.user_profile = user_profile or (lambda: "")

    # ── the journal ────────────────────────────────────────────────────
    async def day_facts(self, day: dt.date) -> dict[str, Any]:
        lo, hi = day.isoformat(), (day + dt.timedelta(days=1)).isoformat()
        goals = await self.db.read("SELECT id, goal, status FROM projects WHERE created_at >= ? AND created_at < ?", (lo, hi))
        jobs = await self.db.read("SELECT j.title, j.status, j.error_message, COALESCE(b.name, j.assigned_bot_id) AS bot FROM jobs j "
                                  "LEFT JOIN bots b ON b.id = j.assigned_bot_id "
                                  "WHERE j.finished_at >= ? AND j.finished_at < ? ORDER BY j.finished_at", (lo, hi))
        try:
            verdicts = await self.db.read("SELECT verdict, note FROM verdicts WHERE created_at >= ? AND created_at < ?", (lo, hi))
        except Exception:
            verdicts = []
        return {"goals": [dict(r) for r in goals], "jobs": [dict(r) for r in jobs], "verdicts": [dict(r) for r in verdicts]}

    async def write_journal(self, day: dt.date) -> str | None:
        if await self.db.read_one("SELECT day FROM journal WHERE day=?", (day.isoformat(),)):
            return None
        facts = await self.day_facts(day)
        if not facts["goals"] and not facts["jobs"]:
            return None
        lines = [f"- goal: {g['goal'][:160]} ({g['status']})" for g in facts["goals"]]
        lines += [f"- job by {j['bot']}: {j['title'][:120]} → {j['status']}"
                  + (f" (why: {j['error_message'][:160]})" if j.get("error_message") and j["status"] != "completed" else "")
                  for j in facts["jobs"][:60]]
        lines += [f"- the user's verdict: {v['verdict']} {v.get('note') or ''}".strip() for v in facts["verdicts"]]
        text = await self.chat([{"role": "user", "content": (
            f"You are Omi, the boss of a team of AI bots. Write the team's journal entry for {day:%A %d %B %Y}: 4 to 8 short "
            "sentences in the first person plural, honest and specific (what we worked on, what passed, what failed and "
            "why if known, what the user thought, what to do better). Only use these facts:\n" + "\n".join(lines))}])
        entry = (text or "").strip() or "\n".join(lines)
        stats = {"goals": len(facts["goals"]), "jobs": len(facts["jobs"]),
                 "completed": sum(j["status"] == "completed" for j in facts["jobs"]),
                 "failed": sum(j["status"] == "failed" for j in facts["jobs"])}
        await self.db.write("INSERT OR REPLACE INTO journal (day, entry, stats_json) VALUES (?,?,?)",
                            (day.isoformat(), entry, json.dumps(stats)))
        folder = self.home / "journal"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{day.isoformat()}.md").write_text(f"# {day:%A %d %B %Y}\n\n{entry}\n", encoding="utf-8")
        return entry

    async def journal(self, days: int = 7) -> list[dict[str, Any]]:
        return [dict(r) for r in await self.db.read("SELECT day, entry, stats_json FROM journal ORDER BY day DESC LIMIT ?", (days,))]

    # ── proposals ──────────────────────────────────────────────────────
    async def pending(self) -> list[dict[str, Any]]:
        return [dict(r) for r in await self.db.read("SELECT * FROM proposals WHERE status='pending' ORDER BY created_at")]

    async def add(self, title: str, goal: str, why: str = "", bot_id: str = "omi") -> str | None:
        title, goal = (title or "").strip()[:120], (goal or "").strip()[:2000]
        if not title or not goal:
            raise ValueError("a proposal needs a title and a goal")
        if len(await self.pending()) >= int(self.settings["max_pending"]):
            raise ValueError(f"{self.settings['max_pending']} proposals already wait for the user; wait for answers first")
        same = await self.db.read_one("SELECT id, status FROM proposals WHERE lower(title)=lower(?) ORDER BY created_at DESC", (title,))
        if same:
            raise ValueError(f"already proposed ({same['status']})")
        pid = "prop_" + uuid.uuid4().hex[:10]
        await self.db.write("INSERT INTO proposals (id, bot_id, title, goal, why) VALUES (?,?,?,?,?)",
                            (pid, bot_id, title, goal, (why or "").strip()[:600]))
        if self.bus is not None:
            await self.bus.publish("#orchestrator", "PROPOSAL", {"id": pid, "title": title, "goal": goal, "why": why},
                                   sender_type="bot", sender_id=bot_id, recipient_id="user")
        return pid

    async def propose(self) -> list[str]:
        """One round of Omi's own ideas (0-2 proposals)."""
        room = int(self.settings["max_pending"]) - len(await self.pending())
        if room <= 0:
            return []
        recent = await self.db.read("SELECT goal, status FROM projects ORDER BY created_at DESC LIMIT 12")
        decided = await self.db.read("SELECT title, status FROM proposals WHERE status != 'pending' ORDER BY created_at DESC LIMIT 20")
        journal = await self.journal(5)
        prompt = (
            "You are Omi, the boss of a team of AI bots on the user's PC. Your team can research, write code, build and "
            "deploy websites, make documents and media, and check its own work. Propose at most 2 goals the team could do "
            "next that would clearly help THIS user, based only on what you know below. Good proposals continue or "
            "improve the user's own recent work (fix what failed, finish what's half done, check a live site). Never "
            "propose anything that spends money, publishes, contacts people, or touches files outside the projects. "
            "Don't repeat a rejected or accepted idea. If nothing is worth proposing, answer []. Answer ONLY a JSON array "
            'of {"title": "...", "goal": "the goal as the user would type it", "why": "one sentence"}.\n\n'
            f"The user's profile:\n{self.user_profile()[:2000] or '(empty)'}\n\n"
            "Recent goals:\n" + ("\n".join(f"- {r['goal'][:200]} ({r['status']})" for r in recent) or "(none)") + "\n\n"
            "Recent journal:\n" + ("\n".join(f"{j['day']}: {j['entry'][:600]}" for j in journal) or "(none)") + "\n\n"
            "Already decided proposals:\n" + ("\n".join(f"- {d['title']} ({d['status']})" for d in decided) or "(none)"))
        made = []
        for item in _json_array(await self.chat([{"role": "user", "content": prompt}]))[:min(2, room)]:
            try:
                pid = await self.add(str(item.get("title", "")), str(item.get("goal", "")), str(item.get("why", "")))
                if pid:
                    made.append(pid)
            except ValueError as exc:
                log.info("proposal skipped: %s", exc)
        await self._mark("last_proposals")
        return made

    async def decide(self, pid: str, accept: bool, note: str = "") -> dict[str, Any]:
        row = await self.db.read_one("SELECT * FROM proposals WHERE id=?", (pid,))
        if row is None:
            raise KeyError(f"no proposal {pid}")
        if row["status"] != "pending":
            return dict(row)
        project = None
        if accept and self.start_goal is not None:
            project = await self.start_goal(row["goal"], {"created_by": "omi-proposal"})
        await self.db.write("UPDATE proposals SET status=?, project_id=?, note=?, decided_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                            "WHERE id=?", ("accepted" if accept else "rejected", project, note[:300], pid))
        return dict(await self.db.read_one("SELECT * FROM proposals WHERE id=?", (pid,)))

    # ── the hourly tick (engine upkeep) ─────────────────────────────────
    async def _last(self, key: str) -> float | None:
        row = await self.db.read_one("SELECT value FROM parameters WHERE scope='global' AND bot_id IS NULL AND key=?", (key,))
        return float(row["value"]) if row and row["value"] else None

    async def _mark(self, key: str) -> None:
        import time
        await self.db.write("DELETE FROM parameters WHERE scope='global' AND bot_id IS NULL AND key=?", (key,))
        await self.db.write("INSERT INTO parameters (scope, bot_id, key, value) VALUES ('global', NULL, ?, ?)", (key, str(time.time())))

    async def tick(self, *, idle: bool, now: float | None = None) -> dict[str, Any]:
        import time
        now = now if now is not None else time.time()
        did: dict[str, Any] = {}
        if self.settings.get("journal", True):
            yesterday = self.today() - dt.timedelta(days=1)
            entry = await self.write_journal(yesterday)
            if entry:
                did["journal"] = yesterday.isoformat()
        if self.settings.get("enabled", True) and idle:
            last = await self._last("last_proposals")
            if last is None:
                await self._mark("last_proposals")            # a new install: the clock starts, no ideas on day one
            elif now - last >= float(self.settings["every_hours"]) * 3600:
                did["proposals"] = await self.propose()
        return did
