"""Running a goal end to end (PLAN.md A7.a.01, A7.a.06, A7.a.08).

goal → project (git folder) → Omi's "[goal]" job, run with the boss toolkit and
a time budget (default 30 min) → Omi plans, staffs, assigns, verifies claims,
reviews, submits → REPORT.md in the project: the result, each job with its
bot and outcome, the accepted claims with their evidence, the files, and the
tokens each bot used. Committed and announced on the board.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from omnibots.board.a2a import Inbox
from omnibots.board.query import query
from omnibots.board.types import topic_project
from omnibots.bots.profile import BOSS_ID
from omnibots.lineup import CHEAP_FIRST
from omnibots.projects.graph import DONE
from omnibots.orchestrator.boss_tools import BossToolkit, GoalContext

log = logging.getLogger(__name__)

RESUME_NOTE = ("RESUMING after a stop: some jobs were interrupted. Start with list_jobs, re-assign interrupted or ready jobs, "
               "and continue the goal from where it stopped.")


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

    async def run_goal(self, goal: str, *, project_id: str | None = None, resume: bool = False,
                       seconds: float | None = None) -> dict[str, Any]:
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
                          router=self.router, skills=self.skills(), planner_chain=self.planner_chain, playbooks=self.playbooks)
        existing = [p.name for p in ctx.folder.iterdir() if p.name not in ("GOAL.md", ".git")] if ctx.folder.is_dir() else []
        here = (f"\n\nYou're working in an EXISTING folder ({ctx.folder}) that already has: {', '.join(sorted(existing)[:25])}. "
                "This goal continues that work: look at those files first (list_dir / read_file) and build on them."
                if existing and not resume else "")
        task_text = ((RESUME_NOTE + "\n\n" if resume else "") + f"GOAL from the user:\n{goal}{here}\n\n"
                     f"Project id: {pid} (a label for your tools, NOT a folder name). Your current folder IS the project "
                     f"folder ({ctx.folder}); files go at its top level. Run it with your tools, then submit.")
        try:
            out = await self.runner.run(BOSS_ID, task_text, title=boss_job.title, job_id=boss_job.id, project_id=pid,
                                        workspace=ctx.folder, extra_tools=kit.tools(), inbox=inbox,
                                        budget={"seconds": seconds or self.goal_seconds}, max_iterations=self.boss_iterations)
        finally:
            inbox.close()
        # Workers still running when the boss stopped get a grace period, then the report.
        live = [t for t in ctx.worker_tasks.values() if not t.done()]
        if live:
            await asyncio.wait(live, timeout=self.grace_seconds)
        await self._stop_leftover_workers(ctx)
        await self._resumable_if_out_of_time(pid, boss_job.id, goal)
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
        await self.bus.publish(topic_project(pid), "QUESTION",
                               {"text": f"⏱ I ran out of my time on \"{goal.splitlines()[0][:80]}\" with {len(left)} job(s) left "
                                        f"({todo}). Press ▶ Start in the tray to continue where I stopped."},
                               sender_type="bot", sender_id=BOSS_ID, recipient_id="user", project_id=pid)
        return True

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
        self.last_retro = await retrospective(router=self.router, chain=self.retro_chain, store=self.playbooks,
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
