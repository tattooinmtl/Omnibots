"""Running a goal end to end (PLAN.md A7.a.01, A7.a.06, A7.a.08).

goal → project (git folder) → Omi's "[goal]" job, run with the boss toolkit and
a time budget (default 30 min) → Omi plans, staffs, assigns, verifies claims,
reviews, submits → REPORT.md in the project: the result, each job with its
bot and outcome, the accepted claims with their evidence, the files, and the
tokens each bot used. Committed and announced on the board.
"""

from __future__ import annotations

import asyncio
import time
import json
import logging
from typing import Any

from omnibots.board.a2a import Inbox
from omnibots.board.query import query
from omnibots.lineup import MINIMAX_FIRST
from omnibots.board.types import topic_project
from omnibots.bots.profile import BOSS_ID
from omnibots.lineup import CHEAP_FIRST
from omnibots.projects.graph import DONE
from omnibots.orchestrator.boss_tools import BossToolkit, GoalContext

log = logging.getLogger(__name__)

RESUME_NOTE = ("RESUMING: this goal was cut off or stopped, or it's a new round of it. Start with list_jobs, re-assign "
               "interrupted, stopped or ready jobs, decide any claims waiting for you, and continue from where it stopped.")


class Orchestrator:
    def __init__(self, *, db, bus, registry, runner, graph, projects, ledger, router, factory, skills=lambda: [],
                 goal_seconds: float = 1800, boss_iterations: int = 200, planner_chain: list[str] | None = None,
                 playbooks=None, retro_chain: list[str] | None = None, grace_seconds: float = 300):
        self.db, self.bus, self.registry, self.runner = db, bus, registry, runner
        self.graph, self.projects, self.ledger, self.router, self.factory = graph, projects, ledger, router, factory
        self.skills = skills
        self.goal_seconds, self.boss_iterations = goal_seconds, boss_iterations
        self.planner_chain = planner_chain
        self.playbooks, self.retro_chain = playbooks, retro_chain or list(CHEAP_FIRST)
        self.last_retro: dict[str, Any] | None = None
        self.grace_seconds = grace_seconds                # workers get this long after the boss stops (A7.a.11)
        self.goals: dict[str, GoalContext] = {}
        self.boss_tasks: dict[str, asyncio.Task] = {}
        # A15.d: the app keeps work going (workers outlive Omi's turn; out of time → a next round).
        # Off by default: the CLI harness and the tests keep A7.a.11's "no worker outlives its goal".
        self.keep_working = False
        self.max_rounds = 8                                # continuation rounds per project per day
        self.leash = None                                  # bots.leash.Leash, set by the engine
        self.schedule = None                               # routines/triggers/night shift (A15.f.03), set by the engine
        self.doctor = None                                 # async (fix, online) -> report dict (A16.d.03), set by the engine
        self._rounds: dict[tuple[str, str], int] = {}

    async def run_goal(self, goal: str, *, project_id: str | None = None, resume: bool = False,
                       seconds: float | None = None, note: str = "") -> dict[str, Any]:
        pid = project_id or await self.projects.create(goal)
        ctx = self.goals.get(pid) or GoalContext(project_id=pid, goal=goal, folder=self.projects.folder(pid))
        self.goals[pid] = ctx
        boss_job = next((j for j in await self.graph.jobs(pid) if j.title.startswith("[goal]")), None)
        if boss_job is None:
            boss_job = await self.graph.add_job(pid, f"[goal] {goal.splitlines()[0][:90]}", description=goal,
                                                assigned_bot_id=BOSS_ID, created_by="user", budget={"max_attempts": 99})
        inbox = await Inbox.open(self.bus, BOSS_ID)
        kit = BossToolkit(ctx=ctx, db=self.db, bus=self.bus, inbox=inbox, registry=self.registry, runner=self.runner,
                          graph=self.graph, projects=self.projects, ledger=self.ledger, factory=self.factory,
                          router=self.router, skills=self.skills(), planner_chain=self.planner_chain, playbooks=self.playbooks, schedule=getattr(self, 'schedule', None),
                          doctor=getattr(self, 'doctor', None))
        existing = [p.name for p in ctx.folder.iterdir() if p.name not in ("GOAL.md", ".git")] if ctx.folder.is_dir() else []
        here = (f"\n\nYou're working in an EXISTING folder ({ctx.folder}) that already has: {', '.join(sorted(existing)[:25])}. "
                "This goal continues that work: look at those files first (list_dir / read_file) and build on them."
                if existing and not resume else "")
        task_text = ((RESUME_NOTE + "\n\n" if resume else "") + f"GOAL from the user:\n{goal}{here}\n\n"
                     f"Project id: {pid} (a label for your tools, NOT a folder name). Your current folder IS the project "
                     f"folder ({ctx.folder}); files go at its top level. Run it with your tools, then submit.")
        if note:
            task_text = f"{note}\n\n{task_text}"
        if resume:                                         # A15.d: what the team left for Omi while it was away
            waiting = await self.db.read("SELECT id, bot_id, text FROM claims WHERE project_id=? AND status='submitted' ORDER BY id", (pid,))
            if waiting:
                task_text += "\n\nClaims waiting for your decision (accept_claim / reject_claim):\n" + "\n".join(
                    f"- claim #{c['id']} from {c['bot_id']}: {c['text'][:200]}" for c in waiting)
            desk = getattr(self.runner, "relay", None)
            if desk is not None and desk.waiting(pid):           # A8.d.02
                task_text += "\n\nTool requests waiting for you (relay_tool / answer_tool_request / decline_tool):\n" + "\n".join(
                    f"- {r['id']}: {r['bot_id']} needs {r['tool']} because {r['why'][:150]}" for r in desk.waiting(pid))
        try:
            out = await self.runner.run(BOSS_ID, task_text, title=boss_job.title, job_id=boss_job.id, project_id=pid,
                                        workspace=ctx.folder, extra_tools=kit.tools(), inbox=inbox,
                                        budget={"seconds": seconds or self.goal_seconds}, max_iterations=self.boss_iterations)
        finally:
            inbox.close()
        # Workers still running when the boss stopped get a grace period, then the report.
        if not self.keep_working:                          # A7.a.11 (CLI, tests): no worker outlives its goal
            live = [t for t in ctx.worker_tasks.values() if not t.done()]
            if live:
                await asyncio.wait(live, timeout=self.grace_seconds)
            await self._stop_leftover_workers(ctx)
        await self._resumable_if_out_of_time(pid, boss_job.id, goal)
        if out.status == "completed":
            await self._review_if_skipped(pid, ctx)
        report = await self.write_report(pid, out)
        await self.projects.set_status(pid, "done" if out.status == "completed" else "failed" if out.status == "failed" else "open")
        if out.status == "completed":
            await self._close_unneeded_jobs(pid)
        if self.playbooks is not None and out.status in ("completed", "failed", "blocked"):
            await self.retrospect(pid, ctx, out, report)
        return {"project_id": pid, "status": out.status, "report": report, "boss_answer": (out.result.answer if out.result else "")}

    async def _resumable_if_out_of_time(self, pid: str, boss_job_id: str, goal: str) -> bool:
        """Live bug 2026-09-26: the goal hit its time limit, stayed `blocked`, and its planned jobs were never
        done (▶ Start only resumes interrupted work). Out of time → interrupted + tell the user what's left."""
        row = await self.db.read_one("SELECT status, error_message FROM jobs WHERE id=?", (boss_job_id,))
        if not row or row["status"] != "blocked" or "time budget" not in (row["error_message"] or ""):
            return False
        await self.db.write("UPDATE jobs SET status='interrupted' WHERE id=?", (boss_job_id,))
        left = [j for j in await self.graph.jobs(pid) if j.status in ("pending", "ready", "blocked", "assigned", "running")
                and not j.title.startswith("[goal]")]
        todo = "; ".join(j.title for j in left[:6]) or "checking and finishing up"
        if self.keep_working and self.rounds_left(pid) > 0:  # A15.d.03: out of time → a next round, not "press Start"
            await self.bus.publish(topic_project(pid), "PROGRESS_UPDATE",
                                   {"text": f"⏱ Out of time this round with {len(left)} job(s) left ({todo}); carrying on in a new round."},
                                   sender_type="bot", sender_id=BOSS_ID, project_id=pid)
            self.schedule_continue(pid, "Last round ran out of time. Carry on with what's left.", after=asyncio.current_task())
            return True
        await self.bus.publish(topic_project(pid), "QUESTION",
                               {"text": f"⏱ I ran out of my time on \"{goal.splitlines()[0][:80]}\" with {len(left)} job(s) left "
                                        f"({todo}). Press ▶ Start in the tray to continue where I stopped."},
                               sender_type="bot", sender_id=BOSS_ID, recipient_id="user", project_id=pid)
        return True

    async def _review_if_skipped(self, pid: str, ctx) -> str | None:
        """A15.a.07 (user, 2026-09-28: "omi can skip trivial one"): a goal that produced more than two files gets
        its review even when Omi skipped review_work (found live in A14.a.02: a 4-file app went unreviewed).
        A FAIL in the app starts a round to fix what the review found."""
        from omnibots.runtime.review import review
        if await query(self.db, project=pid, types=["REVIEW_RESULT"]):
            return None                                    # Omi reviewed it
        folder = self.projects.folder(pid)
        files = [p for p in folder.rglob("*") if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts
                 and p.name not in ("GOAL.md", "REPORT.md")] if folder.is_dir() else []
        if len(files) <= 2:
            return None                                    # trivial: Omi may skip it
        # found live in A14.a.02: Omi had deleted a risky script but not committed yet; the review saw it and FAILed
        await self.projects.commit(pid, "work before review", author=BOSS_ID)
        diff = await self.projects.diff(pid)
        if not diff.strip():
            return None
        try:
            r = await review(self.router, reviewer_id="reviewer", chain=list(self.planner_chain or MINIMAX_FIRST),
                             task=ctx.goal, changes=diff[:40000])
        except Exception as exc:
            log.warning("automatic review of %s failed: %s", pid, exc)
            return None
        await self.bus.publish(topic_project(pid), "REVIEW_RESULT",
                               {"text": f"VERDICT {r.verdict} (automatic: Omi skipped the review)\n" + "\n".join(r.findings)},
                               sender_type="bot", sender_id="reviewer", recipient_id=BOSS_ID, project_id=pid)
        if r.verdict != "PASS" and self.keep_working:
            self.schedule_continue(pid, "The automatic review found problems. Fix them: " + "; ".join(r.findings)[:800],
                                   after=asyncio.current_task())
        return r.verdict

    # ── rounds (A15.d) ─────────────────────────────────────────────────
    def rounds_left(self, pid: str) -> int:
        return self.max_rounds - self._rounds.get((pid, time.strftime("%Y-%m-%d")), 0)

    def schedule_continue(self, pid: str, reason: str, *, after: asyncio.Task | None = None) -> asyncio.Task:
        """Start the next round once `after` (usually the round that's ending) is done."""
        async def go():
            if after is not None and not after.done():
                await asyncio.wait([after])
            return await self.continue_goal(pid, reason)
        return asyncio.create_task(go(), name=f"continue-{pid}")

    async def continue_goal(self, pid: str, reason: str) -> str:
        """A next round of a project's goal: Omi picks up what's left (A15.d). Held by the leash (Off,
        Pause background work) and by the daily round cap, after which you're asked as before."""
        from omnibots.bots.leash import CURRENT_ORIGIN
        task = self.boss_tasks.get(pid)
        if task is not None and not task.done():
            return "a round is already running"
        row = await self.db.read_one("SELECT goal, status FROM projects WHERE id=?", (pid,))
        if not row or row["status"] == "cancelled":
            return "closed"
        if self.leash is not None and (why := await self.leash.may_start("continue", pid)):
            return f"held: {why}"
        if self.rounds_left(pid) <= 0:
            await self.bus.publish(topic_project(pid), "QUESTION",
                                   {"text": f"I've done {self.max_rounds} rounds on \"{row['goal'].splitlines()[0][:80]}\" today and "
                                            "there's still work left. Press ▶ Start in the tray to keep going."},
                                   sender_type="bot", sender_id=BOSS_ID, recipient_id="user", project_id=pid)
            return "round cap reached"
        key = (pid, time.strftime("%Y-%m-%d"))
        self._rounds[key] = self._rounds.get(key, 0) + 1
        for j in await self.graph.jobs(pid):
            if j.status == "interrupted" and not j.title.startswith("[goal]"):
                await self.graph.resume(j.id)
        await self.bus.publish(topic_project(pid), "PROGRESS_UPDATE",
                               {"text": f"↻ Round {self._rounds[key]} of {self.max_rounds} today: {reason}"},
                               sender_type="bot", sender_id=BOSS_ID, project_id=pid)
        token = CURRENT_ORIGIN.set("continue")
        try:
            self.boss_tasks[pid] = asyncio.create_task(self.run_goal(row["goal"], project_id=pid, resume=True, note=reason),
                                                       name=f"goal-{pid}")
        finally:
            CURRENT_ORIGIN.reset(token)
        return "started"

    async def _close_unneeded_jobs(self, pid: str) -> list[str]:
        """A6.c.02: the goal passed review, so jobs Omi planned but routed around (never started)
        weren't needed. Cancel them with a reason, so no finished project keeps open work.
        Only on a reviewer PASS: a boss that merely stopped talking keeps its planned jobs."""
        if not await self.projects.review_passed(pid):
            return []
        rows = await self.db.read("SELECT id FROM jobs WHERE project_id=? AND status IN ('pending','ready','blocked')", (pid,))
        for r in rows:
            await self.graph.cancel(r["id"], "not needed: the goal was completed without it")
        return [r["id"] for r in rows]

    async def _stop_leftover_workers(self, ctx: GoalContext) -> list[str]:
        """A7.a.11: no worker outlives its goal. Still running after the grace period →
        cancelled, and its job becomes `interrupted` (resumable with Start)."""
        left = {jid: t for jid, t in ctx.worker_tasks.items() if not t.done()}
        if not left:
            return []
        for t in left.values():
            t.cancel()
        await asyncio.wait(list(left.values()), timeout=30)
        ids = list(left)
        await self.db.write(f"UPDATE jobs SET status='interrupted' WHERE id IN ({','.join('?' * len(ids))}) "
                            "AND status NOT IN ('completed','failed')", ids)
        log.info("goal %s: stopped %d worker(s) still running after the grace period", ctx.project_id, len(ids))
        return ids

    async def retrospect(self, pid: str, ctx: GoalContext, out, report: str) -> dict[str, Any]:
        """A8.c.02. Success = Omi completed AND every planned job was accepted."""
        from omnibots.orchestrator.playbooks import retrospective
        jobs = [j for j in await self.graph.jobs(pid) if not j.title.startswith("[goal]")]
        success = out.status == "completed" and bool(jobs) and all(j.status in DONE for j in jobs)
        row = await self.db.read_one(
            "SELECT SUM(COALESCE(e.tokens_in,0)+COALESCE(e.tokens_out,0)) AS tok FROM provider_usage_events e "
            "JOIN jobs j ON j.id = e.job_id WHERE j.project_id=?", (pid,))
        boss = await self.registry.get(BOSS_ID)
        from omnibots.orchestrator.verdicts import recent_notes
        mine = await recent_notes(self.db, ctx.playbook.id if ctx.playbook else None)
        self.last_retro = await retrospective(user_verdicts=mine, router=self.router, chain=self.retro_chain, store=self.playbooks,
                                              boss_memory=boss.memory, goal=ctx.goal, report=report, success=success,
                                              project_id=pid, playbook=ctx.playbook, tokens=int((row["tok"] if row else 0) or 0),
                                              seconds=out.seconds)
        return self.last_retro

    async def write_report(self, pid: str, boss_outcome) -> str:
        goal_row = await self.db.read_one("SELECT goal FROM projects WHERE id=?", (pid,))
        # Omi's `submit` posts on the project thread; the runner's own job-finished notice is on the job thread.
        submitted = [m for m in await query(self.db, project=pid, types=["TASK_COMPLETED"])
                     if m.sender_id == BOSS_ID and m.topic == topic_project(pid)]
        result = submitted[-1].text() if submitted else ((boss_outcome.result.answer if boss_outcome and boss_outcome.result else "") or "(no final result)")
        jobs = [j for j in await self.graph.jobs(pid) if not j.title.startswith("[goal]")]
        claims = await self.ledger.claims(project_id=pid)
        usage = await self.db.read(
            "SELECT e.bot_id, SUM(COALESCE(e.tokens_in,0)+COALESCE(e.tokens_out,0)) AS tok, COUNT(*) AS calls "
            "FROM provider_usage_events e JOIN jobs j ON j.id = e.job_id WHERE j.project_id=? GROUP BY e.bot_id ORDER BY tok DESC", (pid,))
        names = {b.id: b.name for b in await self.registry.list(include_archived=True)}
        files = sorted(p.relative_to(self.projects.folder(pid)).as_posix() for p in self.projects.folder(pid).rglob("*")
                       if p.is_file() and ".git" not in p.parts and p.name != "REPORT.md")
        councils = len([m for m in await query(self.db, project=pid, types=["COUNCIL_VERDICT"])])
        lines = [f"# Report: {goal_row['goal'] if goal_row else pid}", "", "## Result", "", result.strip(), "",
                 "## Jobs", "", "| Job | Bot | Status | Attempts |", "|---|---|---|---|"]
        for j in jobs:
            lines.append(f"| {j.title} | {names.get(j.assigned_bot_id, j.assigned_bot_id or '-')} | {j.status} | {j.attempts} |")
        live = await self.db.read_one("SELECT live_url, live_checked_at FROM projects WHERE id=?", (pid,))
        if live and live["live_url"]:
            lines += ["", "## Live", "", f"- {live['live_url']} (checked {live['live_checked_at']})"]
        lines += ["", "## Evidence (accepted claims)", ""]
        acc = [c for c in claims if c["status"] == "accepted"]
        for c in acc:
            ev = "; ".join(f"{e['kind']}: {e['ref']}" + (f" (exit {e['detail'].get('exit_code')})" if "exit_code" in e["detail"] else "") for e in c["evidence"])
            lines.append(f"- **{names.get(c['bot_id'], c['bot_id'])}**: {c['text'][:300]}  \n  _{ev}_")
        if not acc:
            lines.append("- (none)")
        rejected = [c for c in claims if c["status"] == "rejected"]
        if rejected:
            lines += ["", f"{len(rejected)} claim(s) were rejected and redone."]
        lines += ["", "## Files", ""] + [f"- `{f}`" for f in files] + ["", "## Cost", "", "| Bot | Tokens | Model calls |", "|---|---|---|"]
        total = 0
        for u in usage:
            total += u["tok"] or 0
            lines.append(f"| {names.get(u['bot_id'], u['bot_id'])} | {u['tok']:,} | {u['calls']} |")
        lines += [f"| **total** | **{total:,}** | |", "", f"Councils held: {councils}."]
        text = "\n".join(lines) + "\n"
        (self.projects.folder(pid) / "REPORT.md").write_text(text, encoding="utf-8", newline="\n")
        await self.projects.commit(pid, "final report", author=BOSS_ID)
        await self.bus.publish(topic_project(pid), "ARTIFACT_READY", {"text": "REPORT.md", "path": str(self.projects.folder(pid) / "REPORT.md")},
                               sender_type="bot", sender_id=BOSS_ID, project_id=pid, recipient_id="user")
        return text
