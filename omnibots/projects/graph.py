"""The task graph (PLAN.md A6.a.02–04).

Jobs form a DAG inside a project. The graph is the BOSS'S RECORD (ADR-10): it
tracks plans, dependencies, states, attempts and budgets, and computes which
jobs are ready. It does not decide who works on what; the boss does, through
its tools (A7). `PlanRunner` below is a plain executor for hand-made plans,
routines and the night shift, where no boss is deciding.

States: pending → ready → assigned → running → completed | failed | blocked |
cancelled, plus waiting_approval, review, paused and interrupted (resumable).
A job becomes ready when all its dependencies are completed. When a
dependency fails for good or is cancelled, its dependents become blocked.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from omnibots.board.types import topic_job, topic_project
from omnibots.bots.leash import CURRENT_ORIGIN, child_origin

DONE = {"completed"}
DEAD = {"failed", "cancelled"}
ACTIVE = {"assigned", "running", "waiting_approval", "review"}
RESUMABLE = {"paused", "interrupted", "stopped", "blocked"}   # stopped = you stopped it (A15.d.01); only ▶ Start resumes it
MAX_ROUNDS = 5                  # A15.d.03: a job that hits its step limit carries on this many more times
DEP_BLOCKED = "a dependency failed or was cancelled"


class GraphError(ValueError):
    pass


@dataclass
class Job:
    id: str
    project_id: str | None
    title: str
    description: str
    status: str
    priority: int
    depends_on: list[str]
    done_criteria: str
    verifier: str
    budget: dict[str, Any]
    risk_ceiling: str
    assigned_bot_id: str | None
    attempts: int
    result_summary: str | None
    error_message: str | None
    max_attempts: int = 2

    @classmethod
    def from_row(cls, r) -> "Job":
        budget = json.loads(r["budget_json"] or "{}")
        return cls(id=r["id"], project_id=r["project_id"], title=r["title"], description=r["description"] or "",
                   status=r["status"], priority=r["priority"], depends_on=json.loads(r["depends_on_json"] or "[]"),
                   done_criteria=r["done_criteria"] or "", verifier=r["verifier"] or "", budget=budget,
                   risk_ceiling=r["risk_ceiling"], assigned_bot_id=r["assigned_bot_id"], attempts=r["attempts"],
                   result_summary=r["result_summary"], error_message=r["error_message"],
                   max_attempts=int(budget.get("max_attempts", 2)))


class TaskGraph:
    def __init__(self, db, bus=None):
        self.db, self.bus = db, bus

    # ── building the plan ──────────────────────────────────────────────
    async def add_job(self, project_id: str, title: str, *, description: str = "", depends_on: list[str] | None = None,
                      done_criteria: str = "", verifier: str = "", budget: dict[str, Any] | None = None,
                      risk_ceiling: str = "R2", priority: int = 0, assigned_bot_id: str | None = None,
                      job_id: str | None = None, created_by: str = "omi") -> Job:
        if not title.strip():
            raise GraphError("a job needs a title")
        deps = list(dict.fromkeys(depends_on or []))
        known = {j.id: j for j in await self.jobs(project_id)}
        missing = [d for d in deps if d not in known]
        if missing:
            raise GraphError(f"unknown dependencies in this project: {missing}")
        jid = job_id or f"job_{uuid.uuid4().hex[:10]}"
        status = "ready" if all(known[d].status in DONE for d in deps) else "pending"
        if any(known[d].status in DEAD for d in deps):
            status = "blocked"
        await self.db.write(
            "INSERT INTO jobs (id, project_id, title, description, status, priority, depends_on_json, done_criteria, verifier, "
            "budget_json, risk_ceiling, assigned_bot_id, created_by, attempts, origin) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)",
            (jid, project_id, title, description, status, priority, json.dumps(deps), done_criteria, verifier,
             json.dumps(budget or {}), risk_ceiling, assigned_bot_id, created_by, child_origin(CURRENT_ORIGIN.get())))
        if self.bus:
            await self.bus.publish(topic_project(project_id), "TASK_PLANNED",
                                   {"job_id": jid, "title": title, "depends_on": deps, "done_criteria": done_criteria},
                                   sender_type="bot", sender_id=created_by, job_id=jid, project_id=project_id)
        return await self.get(jid)

    async def add_dependency(self, job_id: str, depends_on: str) -> None:
        job, dep = await self.get(job_id), await self.get(depends_on)
        if not job or not dep or job.project_id != dep.project_id:
            raise GraphError("both jobs must exist in the same project")
        if job_id == depends_on or await self._reaches(depends_on, job_id):
            raise GraphError(f"adding {job_id} → {depends_on} would create a cycle")
        deps = list(dict.fromkeys(job.depends_on + [depends_on]))
        await self.db.write("UPDATE jobs SET depends_on_json=? WHERE id=?", (json.dumps(deps), job_id))
        await self.refresh(job.project_id)

    async def update(self, job_id: str, *, depends_on: list[str] | None = None, done_criteria: str | None = None,
                     description: str | None = None) -> "Job":
        """Re-plan one job that hasn't started (pending, ready or blocked): new dependencies (checked:
        same project, no cycle) and/or criteria. A job blocked only by a dead dependency comes back
        to pending, then ready once its new dependencies are done."""
        j = await self.get(job_id)
        if not j:
            raise GraphError(f"no job {job_id}")
        if j.status not in ("pending", "ready", "blocked"):
            raise GraphError(f"{job_id} is {j.status}; only jobs that haven't started can be changed")
        sets, args = [], []
        if depends_on is not None:
            deps = list(dict.fromkeys(depends_on))
            for d in deps:
                dep = await self.get(d)
                if not dep or dep.project_id != j.project_id:
                    raise GraphError(f"dependency {d} is not a job in this project")
                if d == job_id or await self._reaches(d, job_id):
                    raise GraphError(f"{job_id} → {d} would create a cycle")
            sets.append("depends_on_json=?")
            args.append(json.dumps(deps))
        if done_criteria is not None:
            sets.append("done_criteria=?")
            args.append(done_criteria)
        if description is not None:
            sets.append("description=?")
            args.append(description)
        if sets:
            await self.db.write(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", (*args, job_id))
        j = await self.get(job_id)
        if j.status == "blocked" and j.attempts == 0:         # blocked by a dependency, never run itself
            await self._set(j, "pending", error=None)
        await self.refresh(j.project_id)
        return await self.get(job_id)

    async def dependents(self, job_id: str) -> list["Job"]:
        j = await self.get(job_id)
        return [x for x in await self.jobs(j.project_id) if job_id in x.depends_on] if j else []

    async def _reaches(self, start: str, target: str) -> bool:
        """Does `start` (transitively) depend on `target`?"""
        seen, stack = set(), [start]
        while stack:
            j = await self.get(stack.pop())
            if not j or j.id in seen:
                continue
            seen.add(j.id)
            if target in j.depends_on:
                return True
            stack.extend(j.depends_on)
        return False

    # ── reading ────────────────────────────────────────────────────────
    async def get(self, job_id: str) -> Job | None:
        r = await self.db.read_one("SELECT * FROM jobs WHERE id=?", (job_id,))
        return Job.from_row(r) if r else None

    async def jobs(self, project_id: str) -> list[Job]:
        return [Job.from_row(r) for r in await self.db.read("SELECT * FROM jobs WHERE project_id=? ORDER BY rowid", (project_id,))]

    async def ready(self, project_id: str) -> list[Job]:
        await self.refresh(project_id)
        return sorted([j for j in await self.jobs(project_id) if j.status == "ready"], key=lambda j: -j.priority)

    async def snapshot(self, project_id: str) -> dict[str, Any]:
        """Nodes and edges for the UI's graph view."""
        js = await self.jobs(project_id)
        return {"nodes": [{"id": j.id, "title": j.title, "status": j.status, "bot": j.assigned_bot_id, "attempts": j.attempts} for j in js],
                "edges": [[d, j.id] for j in js for d in j.depends_on]}

    # ── readiness ──────────────────────────────────────────────────────
    async def refresh(self, project_id: str) -> None:
        """Recompute readiness until nothing changes (blocking cascades down the graph)."""
        changed = True
        while changed:
            changed = False
            js = {j.id: j for j in await self.jobs(project_id)}
            for j in js.values():
                dep_states = [js[d].status for d in j.depends_on if d in js]
                if j.status == "blocked" and j.attempts == 0 and j.error_message == DEP_BLOCKED:
                    # blocked only by a dependency, which was re-planned (update): back in line
                    if not any(s in DEAD or s == "blocked" for s in dep_states):
                        await self._set(j, "pending", error=None)
                        changed = True
                    continue
                if j.status not in ("pending", "ready"):
                    continue
                if any(s in DEAD or (s == "blocked") for s in dep_states):
                    await self._set(j, "blocked", error=DEP_BLOCKED)
                    changed = True
                elif j.status == "pending" and all(s in DONE for s in dep_states):
                    await self._set(j, "ready")
                    changed = True

    # ── transitions ────────────────────────────────────────────────────
    async def assign(self, job_id: str, bot_id: str, *, by: str = "omi") -> Job:
        j = await self.get(job_id)
        if not j or j.status not in ("ready", "assigned"):
            raise GraphError(f"job {job_id} is not ready (it is {j.status if j else 'missing'})")
        await self.db.write("UPDATE jobs SET status='assigned', assigned_bot_id=? WHERE id=?", (bot_id, job_id))
        if self.bus:
            await self.bus.publish(topic_job(job_id), "TASK_ASSIGNED", {"title": j.title, "done_criteria": j.done_criteria},
                                   sender_type="bot", sender_id=by, recipient_id=bot_id, job_id=job_id, project_id=j.project_id)
        return await self.get(job_id)

    async def reassign(self, job_id: str, bot_id: str, *, by: str = "omi") -> Job:
        j = await self.get(job_id)
        if not j or j.status in DONE | DEAD:
            raise GraphError(f"job {job_id} can't be reassigned (it is {j.status if j else 'missing'})")
        await self._set(j, "ready")
        return await self.assign(job_id, bot_id, by=by)

    async def cancel(self, job_id: str, reason: str = "cancelled") -> None:
        j = await self.get(job_id)
        if j and j.status not in DONE:
            await self._set(j, "cancelled", error=reason)
            await self.refresh(j.project_id)

    async def pause(self, job_id: str) -> None:
        j = await self.get(job_id)
        if j and j.status in ACTIVE | {"ready", "pending"}:
            await self._set(j, "paused")

    async def interrupt(self, job_id: str) -> None:
        j = await self.get(job_id)
        if j and j.status in ACTIVE:
            await self._set(j, "interrupted")

    async def resume(self, job_id: str) -> None:
        """Paused / interrupted / blocked → back in the queue (pending, then ready when its deps allow)."""
        j = await self.get(job_id)
        if j and j.status in RESUMABLE:
            await self._set(j, "pending", error=None)
            await self.refresh(j.project_id)

    async def continue_or_record(self, job_id: str, out) -> str:
        """A15.d.03: a run that ended at its step limit isn't a failure: the same job goes back to its
        bot (assigned) with a note of where it got to, and the listen loop starts the next round.
        After MAX_ROUNDS it's an ordinary failure (retry / escalate). Anything else: record_outcome."""
        res = getattr(out, "result", None)
        j = await self.get(job_id)
        if j and res is not None and getattr(res, "status", "") == "max_iterations":
            budget = dict(j.budget or {})
            rounds = int(budget.get("rounds", 0)) + 1
            if rounds <= MAX_ROUNDS:
                budget["rounds"] = rounds
                progress = (getattr(res, "answer", "") or "").strip()[-1500:] or "(no summary)"
                desc = (f"{j.description or j.title}\n\n[Round {rounds} ended at the step limit. Where it got to:]\n{progress}\n"
                        "Carry on from there; don't redo the finished parts.")
                await self.db.write("UPDATE jobs SET status='assigned', budget_json=?, description=?, error_message=NULL WHERE id=?",
                                    (json.dumps(budget), desc, job_id))
                # the runner had already marked it blocked, which blocked its dependents too: back in line (found by A14.a.01)
                await self.refresh(j.project_id)
                if self.bus:
                    await self.bus.publish(topic_job(job_id), "PROGRESS_UPDATE",
                                           {"text": f"'{j.title}' hit its step limit; carrying on in round {rounds + 1}"},
                                           sender_type="system", job_id=job_id, project_id=j.project_id)
                return "assigned"
        return await self.record_outcome(job_id, out.status, error=getattr(res, "error", None) if res else None)

    async def record_outcome(self, job_id: str, status: str, *, error: str | None = None) -> str:
        """After a run: completed → dependents may become ready; failed → retry
        (back to ready) while attempts remain, otherwise ESCALATE: blocked, with a
        QUESTION to the boss and the user on the board. Returns the new state."""
        j = await self.get(job_id)
        if not j:
            raise GraphError(f"no job {job_id}")
        if status == "completed":
            await self.refresh(j.project_id)
            return "completed"
        if status in ("failed", "blocked") and j.attempts < j.max_attempts:
            await self._set(j, "ready", error=error)
            if self.bus:
                await self.bus.publish(topic_job(job_id), "PROGRESS_UPDATE",
                                       {"text": f"attempt {j.attempts} of {j.max_attempts} failed; retrying", "error": error},
                                       sender_type="system", job_id=job_id, project_id=j.project_id)
            return "ready"
        if status in ("failed", "blocked"):
            await self._set(j, "blocked", error=error)
            if self.bus:
                await self.bus.publish(topic_job(job_id), "QUESTION",
                                       {"text": f"Job '{j.title}' failed {j.attempts} time(s) and needs a decision "
                                                f"(retry differently, reassign, or cancel). Last error: {error}"},
                                       sender_type="system", recipient_id="omi", job_id=job_id, project_id=j.project_id)
            await self.refresh(j.project_id)
            return "escalated"
        return status

    async def _set(self, j: Job, status: str, *, error: str | None | object = ...) -> None:
        if error is ...:
            await self.db.write("UPDATE jobs SET status=? WHERE id=?", (status, j.id))
        else:
            await self.db.write("UPDATE jobs SET status=?, error_message=? WHERE id=?", (status, error, j.id))


class PlanRunner:
    """Executes a project's ready jobs on their assigned bots: one job per bot
    at a time, all bots in parallel, with retries and escalation from the graph.
    For hand-made plans, routines and the night shift (the boss drives its own
    plans through tools, A7)."""

    def __init__(self, graph: TaskGraph, run_job: Callable[..., Awaitable[Any]], *, workspace_for=None):
        self.graph, self.run_job, self.workspace_for = graph, run_job, workspace_for

    async def run(self, project_id: str, *, default_bot: str | None = None) -> dict[str, str]:
        busy: dict[str, asyncio.Task] = {}                    # bot_id -> its running job
        order: list[str] = []
        while True:
            for job in await self.graph.ready(project_id):
                bot = job.assigned_bot_id or default_bot
                if not bot or bot in busy:
                    continue
                await self.graph.assign(job.id, bot)
                order.append(job.id)
                busy[bot] = asyncio.create_task(self._one(job, bot))
            if not busy:
                break
            done, _ = await asyncio.wait(busy.values(), return_when=asyncio.FIRST_COMPLETED)
            for bot in [b for b, t in busy.items() if t in done]:
                busy.pop(bot).result()
        return {j.id: j.status for j in await self.graph.jobs(project_id)} | {"_order": ",".join(order)}

    async def _one(self, job: Job, bot: str) -> None:
        task = job.description or job.title
        if job.done_criteria:
            task += f"\n\nDone when: {job.done_criteria}"
        ws = self.workspace_for(job.project_id) if self.workspace_for else None
        out = await self.run_job(bot, task, title=job.title, job_id=job.id, project_id=job.project_id, workspace=ws, budget=job.budget or None)
        await self.graph.record_outcome(job.id, out.status, error=getattr(out.result, "error", None) if out.result else None)
