"""Standing listen loop (user, 2026-09-27).

Every bot stays up for the life of the app. There is no 30-minute cap on this
loop. Each cycle it reads the board (messages addressed to it, and for Omi the
project threads too) and, if it is idle, starts a job that was assigned to it.
Omi also looks at each project folder that is not cancelled. When the files
change and nobody is writing them right now, he checks the project and fixes
what broke. goal_minutes still bounds one boss *work* turn; this loop is not
that turn.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from omnibots.board.a2a import Inbox
from omnibots.board.query import query
from omnibots.board.types import topic_project
from omnibots.bots.leash import Leash, child_origin
from omnibots.bots.profile import BOSS_ID
from omnibots.orchestrator.boss_tools import BossToolkit, GoalContext

log = logging.getLogger(__name__)

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".omnibots", ".venv", "venv"}
MAINTAIN_PROMPT = """MAINTENANCE. The project is still open. This is not a new goal, and watching it has no time limit.

Files that changed since the last look:
{changes}

Recent board:
{board}

Read those files and list_jobs. If something is broken, fix it or assign_job a bot who can.
If a job is already assigned, leave it — that bot reads the board and will pick it up.
Do not close the project. When the check is done, give a short report of what you looked at and what you fixed."""

# A15.b.02: on Watch, Omi looks and reports. The tools that start or change work aren't given.
WATCH_PROMPT = """CHECK (report only). The project is still open and on WATCH: you look and report, you don't fix.

Files that changed since the last look:
{changes}

Recent board:
{board}

Read those files and list_jobs. Do not edit files, plan, assign or create bots. If something looks broken, say
exactly what and how you would fix it; the user decides (switching this project to Fix lets you repair it).
Give a short report: what you looked at, what is fine, what is broken."""
WATCH_TOOLS = {"list_jobs", "list_team", "find_skills", "review_work"}


def snapshot(folder: Path) -> dict[str, tuple[int, int]]:
    """Relative path -> (mtime_ns, size). Skips VCS and dependency trees."""
    out: dict[str, tuple[int, int]] = {}
    if not folder.is_dir():
        return out
    for p in folder.rglob("*"):
        if not p.is_file():
            continue
        parts = set(p.relative_to(folder).parts)
        if parts & SKIP_DIRS:
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        out[p.relative_to(folder).as_posix()] = (st.st_mtime_ns, st.st_size)
    return out


def diff_snapshots(old: dict[str, tuple[int, int]], new: dict[str, tuple[int, int]]) -> list[str]:
    lines = []
    for name in sorted(set(old) | set(new)):
        if name not in old:
            lines.append(f"added {name}")
        elif name not in new:
            lines.append(f"removed {name}")
        elif old[name] != new[name]:
            lines.append(f"changed {name}")
    return lines


class TeamPresence:
    def __init__(self, *, db, bus, registry, runner, graph, projects, orchestrator,
                 poll_seconds: float = 5.0, maintain_cooldown: float = 90.0, settle_seconds: float = 0.0,
                 paused: Callable[[], bool] | None = None, leash: Leash | None = None,
                 maintain: Callable[[str, list[str]], Awaitable[Any]] | None = None,
                 sleep=asyncio.sleep, clock=time.monotonic):
        self.db, self.bus, self.registry, self.runner = db, bus, registry, runner
        self.graph, self.projects, self.orchestrator = graph, projects, orchestrator
        self.poll, self.maintain_cooldown = poll_seconds, maintain_cooldown
        self.settle = settle_seconds          # A15.b.03: the folder must be quiet this long before Omi checks it
        self.leash = leash
        self._seen: dict[str, dict[str, tuple[int, int]]] = {}
        self._changed_at: dict[str, float] = {}
        self._held_told: set[str] = set()     # jobs whose "held by the leash" note is already on the board
        self._paused = paused or (lambda: False)
        self._maintain_fn = maintain
        self._sleep, self._clock = sleep, clock
        self._tasks: dict[str, asyncio.Task] = {}
        self._jobs: dict[str, asyncio.Task] = {}          # bot_id -> pickup / user-note task
        self._snapshots: dict[str, dict[str, tuple[int, int]]] = {}
        self._last_maintain: dict[str, float] = {}
        self._maintaining: set[str] = set()
        self._maint_tasks: dict[str, asyncio.Task] = {}
        self._notes: dict[str, list[str]] = {}
        self._boss_notes: dict[str, list] = {}          # bot_id -> Omi's messages (Message) waiting for an idle worker
        self._for_omi: dict[str, str] = {}               # project -> why Omi should look (results arrived between rounds)
        self._last_round: dict[str, float] = {}
        self.round_cooldown = 60.0
        self.polls = 0

    def busy(self, bot_id: str) -> bool:
        return bot_id in self.runner.active or bot_id in self.runner._starting or (
            bot_id in self._jobs and not self._jobs[bot_id].done())

    async def run(self) -> None:
        try:
            while True:
                await self._sync()
                self.polls += 1
                await self._sleep(self.poll)
        finally:
            for t in list(self._tasks.values()) + list(self._jobs.values()) + list(self._maint_tasks.values()):
                t.cancel()
            pending = [t for t in list(self._tasks.values()) + list(self._jobs.values()) + list(self._maint_tasks.values()) if not t.done()]
            if pending:
                await asyncio.wait(pending, timeout=5)

    async def _sync(self) -> None:
        live = {b.id for b in await self.registry.list() if b.status != "archived"}
        for bid in live:
            task = self._tasks.get(bid)
            if task is None or task.done():
                self._tasks[bid] = asyncio.create_task(self._life(bid), name=f"listen-{bid}")
        for bid in list(self._tasks):
            if bid not in live:
                self._tasks.pop(bid).cancel()

    async def _life(self, bot_id: str) -> None:
        topics = {"#general", "#orchestrator", "#approvals", "#council", "#project/*"} if bot_id == BOSS_ID else set()
        sub = await self.bus.subscribe(topics, recipient=bot_id)
        try:
            while True:
                prof = await self.registry.get(bot_id)
                if not prof or prof.status == "archived":
                    return
                if bot_id != BOSS_ID:
                    sub.topics = await self._project_topics(bot_id)
                msg = await sub.get(timeout=self.poll)
                batch = [msg, *sub.drain()] if msg is not None else []
                for m in batch:
                    await self._on_message(bot_id, m)
                if not self._paused():
                    await self._pickup_ready(bot_id)
                    await self._flush_notes(bot_id)
                    await self._flush_boss_notes(bot_id)
                    if bot_id == BOSS_ID:
                        await self._rounds_for_omi()
                    if bot_id == BOSS_ID:
                        await self._watch_files()
        finally:
            sub.close()

    async def _project_topics(self, bot_id: str) -> set[str]:
        rows = await self.db.read(
            "SELECT DISTINCT project_id FROM jobs WHERE assigned_bot_id=? AND project_id IS NOT NULL", (bot_id,))
        return {topic_project(r["project_id"]) for r in rows}

    async def _on_message(self, bot_id: str, m) -> None:
        if m.message_type == "A2A_MESSAGE" and m.recipient_id == bot_id and m.sender_type == "user" and bot_id != BOSS_ID:
            text = m.text().strip()
            if text:
                self._notes.setdefault(bot_id, []).append(text)
        elif (bot_id == BOSS_ID and m.recipient_id == BOSS_ID and m.project_id and m.sender_type == "bot"
              and m.message_type in ("CLAIM_SUBMITTED", "HELP_REQUEST", "A2A_MESSAGE", "BLOCKED", "TOOL_REQUEST")
              and getattr(self.orchestrator, "keep_working", False)):
            # A15.d.01: a worker that outlived Omi's round reports; Omi needs a round to decide on it
            self._for_omi[m.project_id] = f"{m.sender_id} sent a {m.message_type.lower().replace('_', ' ')} while you were between rounds."
        elif (m.message_type == "A2A_MESSAGE" and m.recipient_id == bot_id and m.sender_id == BOSS_ID
              and bot_id != BOSS_ID and not self.busy(bot_id) and m.text().strip()):
            # A15.a.06: send_message tells Omi an idle worker "will see this"; now it does. (A worker
            # inside a job reads it through its own inbox, so only idle workers are woken here.)
            self._boss_notes.setdefault(bot_id, []).append(m)

    async def _pickup_ready(self, bot_id: str) -> None:
        if self.busy(bot_id):
            return
        row = await self.db.read_one(
            "SELECT id FROM jobs WHERE assigned_bot_id=? AND status='assigned' ORDER BY created_at LIMIT 1", (bot_id,))
        if not row:
            return
        job = await self.graph.get(row["id"])
        if not job:
            return
        if self.leash:
            origin = (await self.db.read_one("SELECT origin FROM jobs WHERE id=?", (job.id,)))["origin"]
            if why := await self.leash.may_start(origin, job.project_id):
                if job.id not in self._held_told:
                    self._held_told.add(job.id)
                    await self.bus.publish(topic_project(job.project_id) if job.project_id else "#general", "PROGRESS_UPDATE",
                                           {"text": f"{bot_id} is holding '{job.title}': {why}."}, sender_type="system",
                                           project_id=job.project_id, job_id=job.id)
                return
            self._held_told.discard(job.id)
        for ctx in getattr(self.orchestrator, "goals", {}).values():
            task = ctx.worker_tasks.get(job.id)
            if task is not None and not task.done():
                return
        self._jobs[bot_id] = asyncio.create_task(self._run_assigned(bot_id, job), name=f"pickup-{job.id}")

    async def _run_assigned(self, bot_id: str, job) -> None:
        try:
            goal = ""
            if job.project_id:
                row = await self.db.read_one("SELECT goal FROM projects WHERE id=?", (job.project_id,))
                goal = row["goal"] if row else ""
            folder = self.projects.folder(job.project_id) if job.project_id else None
            text = (f"{job.description or job.title}\n\nDone when: {job.done_criteria}\n"
                    f"Goal of the whole project: {goal}\n"
                    "You were assigned this on the board. Do it, submit_claim with evidence, and report. "
                    "The listen loop keeps reading the board after this job; do not wait here for the next one.")
            out = await self.runner.run(bot_id, text, title=job.title, job_id=job.id, project_id=job.project_id, workspace=folder)
            if out.status == "skipped":
                return
            if job.project_id:
                await self.projects.commit(job.project_id, f"{job.title} ({bot_id})", author=bot_id)
            if out.status == "completed":
                fresh = await self.graph.get(job.id)
                if fresh is not None:
                    await self.graph._set(fresh, "review")
            elif out.status not in ("cancelled",):
                await self.graph.continue_or_record(job.id, out)                   # step limit → next round (A15.d.03)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("pickup failed for %s", bot_id)

    async def _rounds_for_omi(self) -> None:
        for pid, why in list(self._for_omi.items()):
            task = getattr(self.orchestrator, "boss_tasks", {}).get(pid)
            if task is not None and not task.done():
                self._for_omi.pop(pid, None)          # a round is running: Omi reads it there
                continue
            if self.busy(BOSS_ID) or self._clock() - self._last_round.get(pid, -1e9) < self.round_cooldown:
                continue
            self._for_omi.pop(pid, None)
            self._last_round[pid] = self._clock()
            await self.orchestrator.continue_goal(pid, why + " Decide on what the team sent.")

    async def _flush_boss_notes(self, bot_id: str) -> None:
        msgs = self._boss_notes.get(bot_id)
        if not msgs or self.busy(bot_id):
            return
        first = msgs[0]
        origin = "user"
        if first.job_id:                       # Omi's own job says who started this: your goal, or a check (→ a fix)
            row = await self.db.read_one("SELECT origin FROM jobs WHERE id=?", (first.job_id,))
            origin = child_origin(row["origin"]) if row else "user"
        if self.leash and await self.leash.may_start(origin, first.project_id):
            return                             # held by the dial or the pause: kept until it's allowed
        self._boss_notes[bot_id] = []
        text = ("Omi wrote to you on the board while you weren't in a job:\n" + "\n".join(m.text().strip() for m in msgs)
                + "\n\nDo what Omi asks, then answer Omi with send_message (and submit_claim if it's a result).")
        folder = self.projects.folder(first.project_id) if first.project_id else None
        self._jobs[bot_id] = asyncio.create_task(
            self.runner.run(bot_id, text, title=f"Omi: {msgs[0].text().strip()[:70]}", project_id=first.project_id,
                            workspace=folder, origin=origin), name=f"boss-note-{bot_id}")

    async def _flush_notes(self, bot_id: str) -> None:
        notes = self._notes.get(bot_id)
        if not notes or self.busy(bot_id):
            return
        self._notes[bot_id] = []
        text = "The user wrote on the board while you were listening:\n" + "\n".join(notes)
        self._jobs[bot_id] = asyncio.create_task(
            self.runner.run(bot_id, text, title=notes[0][:80], origin="user"), name=f"note-{bot_id}")

    def _project_busy(self, folder: Path) -> bool:
        try:
            target = folder.resolve()
        except OSError:
            return False
        for agent in self.runner.active.values():
            try:
                if Path(agent.workspace).resolve() == target:
                    return True
            except OSError:
                continue
        return False

    async def _watch_files(self) -> None:
        rows = await self.db.read("SELECT id, status FROM projects WHERE status != 'cancelled'")
        for r in rows:
            pid = r["id"]
            if pid in self._maintaining:
                continue
            folder = self.projects.folder(pid)
            new = snapshot(folder)
            old = self._snapshots.get(pid)
            if old is None:
                self._snapshots[pid] = new
                continue
            if self._project_busy(folder):
                continue                               # judge the diff once the folder is quiet, so edits aren't lost
            changes = diff_snapshots(old, new)
            if not changes:
                continue
            now = self._clock()
            if self._seen.get(pid) != new:                 # still changing: wait for the folder to settle
                self._seen[pid], self._changed_at[pid] = new, now
            if now - self._changed_at.get(pid, now) < self.settle:
                continue
            if now - self._last_maintain.get(pid, -1e9) < self.maintain_cooldown:
                continue
            if self.leash and await self.leash.may_start("watch", pid):
                continue                                   # Off or paused: the changes wait, checked when allowed again
            self._last_maintain[pid] = now
            self._snapshots[pid] = new
            self._maintaining.add(pid)
            # Don't block the listen loop on the check. The loop keeps reading the board.
            self._maint_tasks[pid] = asyncio.create_task(self._finish_maintain(pid, changes), name=f"maintain-{pid}")

    async def _finish_maintain(self, pid: str, changes: list[str]) -> None:
        try:
            await (self._maintain_fn or self._maintain)(pid, changes)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("maintenance failed for %s", pid)
        finally:
            self._snapshots[pid] = snapshot(self.projects.folder(pid))
            self._maintaining.discard(pid)

    async def _maintain(self, pid: str, changes: list[str]) -> None:
        """Omi checks one project. No time budget: this is upkeep, not a 30-minute goal."""
        if self.busy(BOSS_ID):
            await self.bus.publish(topic_project(pid), "PROGRESS_UPDATE",
                                   {"text": "project changed while Omi was busy: " + "; ".join(changes[:12])},
                                   sender_type="system", project_id=pid, recipient_id=BOSS_ID)
            self.runner.steer(BOSS_ID, "The project changed: " + "; ".join(changes[:12]) + ". Check it when you can.")
            return
        row = await self.db.read_one("SELECT goal FROM projects WHERE id=?", (pid,))
        goal = row["goal"] if row else pid
        folder = self.projects.folder(pid)
        ctx = self.orchestrator.goals.get(pid) or GoalContext(project_id=pid, goal=goal, folder=folder)
        self.orchestrator.goals[pid] = ctx
        inbox = await Inbox.open(self.bus, BOSS_ID)
        skills = self.orchestrator.skills() if callable(self.orchestrator.skills) else self.orchestrator.skills
        kit = BossToolkit(ctx=ctx, db=self.db, bus=self.bus, inbox=inbox, registry=self.registry, runner=self.runner,
                          graph=self.graph, projects=self.projects, ledger=self.orchestrator.ledger,
                          factory=self.orchestrator.factory, router=self.orchestrator.router, skills=skills or [],
                          planner_chain=self.orchestrator.planner_chain, playbooks=self.orchestrator.playbooks, schedule=getattr(self.orchestrator, 'schedule', None))
        board = await query(self.db, project=pid, limit=12)
        board_text = "\n".join(f"- {m.message_type}: {m.text()[:180]}" for m in board[-12:]) or "(nothing yet)"
        fix = (await self.leash.level(pid) == "fix") if self.leash else True
        tools = kit.tools() if fix else [t for t in kit.tools() if t.name in WATCH_TOOLS]
        prompt = (MAINTAIN_PROMPT if fix else WATCH_PROMPT).format(changes="\n".join(f"- {c}" for c in changes[:40]), board=board_text)
        try:
            await self.runner.run(BOSS_ID, prompt, title=f"[{'maintain' if fix else 'check'}] {goal[:60]}", project_id=pid,
                                  workspace=folder, extra_tools=tools, inbox=inbox, max_iterations=80, origin="watch")
        finally:
            inbox.close()
