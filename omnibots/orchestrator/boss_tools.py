"""Omi's toolkit (PLAN.md A7): the tools that make the boss the boss.

  plan_goal      the Planner turns the goal into jobs (the DAG, the boss's record)
  ask_user       a QUESTION to the user on the board; waits for their reply
  list_team      bots, roles, skills, lane, status, the seat line
  find_skills    search the skill pool (Omni's skills) for a capability
  create_bot     the Bot Factory, under the spawn governor
  assign_job     start a worker on a ready job, in the project's folder
  list_jobs      the plan's state (jobs, bots, attempts, claims)
  accept_claim   evidence proves the done-criteria → the job is done, dependents unlock
  reject_claim   partial / off-target / wrong metric → refined instructions, retry or substitute
  review_work    the critic reads the project's real diff
  council        three independent views + cross-examination for decisions that matter
A job the worker finishes goes to `review`; it is only `completed` when the
boss accepts its claim (ADR-12: proof-carrying results).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omnibots.board.a2a import Inbox
from omnibots.board.ledger import EvidenceError
from omnibots.board.types import COUNCIL, topic_job, topic_project
from omnibots.bots.profile import BOSS_ID
from omnibots.lineup import MINIMAX_FIRST
from omnibots.orchestrator.council import hold_council
from omnibots.orchestrator.factory import KNOWN_TOOLS, BotFactory, SpawnRefused
from omnibots.orchestrator.planner import PlanError, make_plan
from omnibots.runtime.review import REVIEWER_PROMPT, review
from omnibots.runtime.sandbox import PY_FLAGS, sandbox_python
from omnibots.runtime.tools import RISK_ORDER, Tool, ToolContext

R3 = RISK_ORDER.index("R3")


@dataclass
class GoalContext:
    project_id: str
    goal: str
    folder: Any
    worker_tasks: dict[str, asyncio.Task] = field(default_factory=dict)     # job_id -> running worker
    job_bot: dict[str, str] = field(default_factory=dict)                   # job_id -> bot_id
    extra: dict[str, str] = field(default_factory=dict)                     # job_id -> boss's added instructions
    playbook: Any = None                                                    # the playbook the plan followed (A8.c.01)


class BossToolkit:
    def __init__(self, *, ctx: GoalContext, db, bus, inbox: Inbox, registry, runner, graph, projects, ledger,
                 factory: BotFactory, router, skills: list | None = None, planner_chain: list[str] | None = None,
                 playbooks=None, schedule=None, doctor=None, initiative=None):
        self.c, self.db, self.bus, self.inbox = ctx, db, bus, inbox
        self.registry, self.runner, self.graph, self.projects, self.ledger = registry, runner, graph, projects, ledger
        self.factory, self.router, self.skills = factory, router, skills or []
        self.chain = planner_chain or list(MINIMAX_FIRST)
        self.schedule = schedule                            # A15.f.03: routines, triggers, night shift (the app only)
        self.playbooks = playbooks
        self.doctor = doctor                                # A16.d.03: the app's doctor (the app only)
        self.initiative = initiative                        # A17.e.01: proposals and the journal (the app only)

    # ── helpers ────────────────────────────────────────────────────────
    async def _plan_jobs(self):
        """The plan's jobs: everything in the project except Omi's own "[goal]" job."""
        return [j for j in await self.graph.jobs(self.c.project_id) if not j.title.startswith("[goal]")]

    def _job_task(self, job) -> str:
        parts = [job.description or job.title, f"\nDone when: {job.done_criteria}" if job.done_criteria else "",
                 f"\nGoal of the whole project: {self.c.goal}",
                 "\nWork in the shared project folder: your CURRENT folder IS the project folder, so write files at its top "
                 "level (index.html, not <project-id>/index.html) unless this job names a subfolder. When finished, submit_claim with evidence "
                 "(files you wrote, commands you ran with exit codes, url_quote for facts from the web), then give a short report."]
        if self.c.extra.get(job.id):
            parts.append(f"\nInstructions from Omi: {self.c.extra[job.id]}")
        return "".join(parts)

    async def _start(self, job, bot_id: str) -> None:
        async def work():
            out = await self.runner.run(bot_id, self._job_task(job), title=job.title, job_id=job.id,
                                        project_id=self.c.project_id, workspace=self.c.folder,
                                        budget=job.budget or None)
            if out.status == "skipped":
                return out
            await self.projects.commit(self.c.project_id, f"{job.title} ({bot_id})", author=bot_id)
            if out.status == "completed":
                await self.graph._set(await self.graph.get(job.id), "review")      # done only when the boss accepts
            else:
                await self.graph.continue_or_record(job.id, out)                   # step limit → next round (A15.d.03)
            return out
        self.c.job_bot[job.id] = bot_id
        self.c.worker_tasks[job.id] = asyncio.create_task(work(), name=f"job-{job.id}")

    # ── tools ──────────────────────────────────────────────────────────
    async def plan_goal(self, args: dict[str, Any], ctx: ToolContext) -> str:
        existing = await self._plan_jobs()
        if existing and not args.get("replan"):
            return "A plan already exists; use list_jobs (or plan_goal with replan=true to add jobs)."
        goal = str(args.get("goal") or self.c.goal)
        context = str(args.get("context") or "")
        note = ""
        if self.playbooks is not None and not existing:          # playbooks are looked up first (§4.2 step 2)
            pb = await self.playbooks.choose(goal)
            if pb:
                self.c.playbook = pb
                context = (context + "\n\n" if context else "") + (
                    f"A proven PLAYBOOK for this kind of goal ({pb.name} v{pb.version}, {pb.stats_line()}). "
                    f"Follow its steps where they fit this goal:\n{pb.body_md}")
                note = f"Following playbook {pb.name} v{pb.version} ({pb.stats_line()}).\n"
        try:
            plan = await make_plan(self.router, self.chain, goal, context=context)
        except PlanError as exc:
            return f"ERROR: planning failed: {exc}"
        if plan.question:
            return f"The planner needs a clarification before planning. Ask the user: {plan.question}"
        ids: list[str] = []
        for s in plan.subtasks:
            job = await self.graph.add_job(self.c.project_id, s.title, description=s.description, done_criteria=s.done_criteria,
                                           depends_on=[ids[d - 1] for d in s.depends_on], risk_ceiling=s.risk,
                                           budget={"max_attempts": 2, "skills": s.skills})
            ids.append(job.id)
        return note + "Plan recorded:\n" + await self._jobs_table()

    async def add_job(self, args: dict[str, Any], ctx: ToolContext) -> str:
        title = str(args.get("title") or "").strip()
        done = str(args.get("done_criteria") or "").strip()
        if not title or not done:
            return "ERROR: add_job needs a title and done_criteria"
        deps = [d for d in (args.get("depends_on") or []) if isinstance(d, str)]
        try:
            job = await self.graph.add_job(self.c.project_id, title, description=str(args.get("description") or title),
                                           done_criteria=done, depends_on=deps, risk_ceiling=str(args.get("risk") or "R1"),
                                           budget={"max_attempts": 2})
        except Exception as exc:
            return f"ERROR: {exc}"
        return f"added {job.id} \"{job.title}\" [{job.status}]; assign_job it to an idle bot"

    async def cancel_job(self, args: dict[str, Any], ctx: ToolContext) -> str:
        """A job that's no longer needed or can't work as planned: cancelled (its worker, if any, is stopped)."""
        jid, reason = str(args.get("job_id") or ""), str(args.get("reason") or "").strip()
        j = await self.graph.get(jid)
        if not j or j.project_id != self.c.project_id:
            return f"ERROR: no job {jid!r} in this project"
        if not reason:
            return "ERROR: cancel_job needs a reason"
        task = self.c.worker_tasks.get(jid)
        if task and not task.done():
            task.cancel()
            await asyncio.wait([task], timeout=15)
        await self.graph.cancel(jid, f"cancelled by Omi: {reason}")
        stuck = [d for d in await self.graph.dependents(jid) if d.status == "blocked"]
        return (f"cancelled {jid} \"{j.title}\""
                + (f". Now blocked because they needed it: {', '.join(d.id for d in stuck)}. Point them at the replacement "
                   f"with update_job(depends_on=[...]) or cancel_job them." if stuck else ""))

    async def update_job(self, args: dict[str, Any], ctx: ToolContext) -> str:
        """Re-plan a job that hasn't started: new depends_on (e.g. the replacement of a cancelled job) and/or criteria."""
        jid = str(args.get("job_id") or "")
        j = await self.graph.get(jid)
        if not j or j.project_id != self.c.project_id:
            return f"ERROR: no job {jid!r} in this project"
        deps = args.get("depends_on")
        try:
            j = await self.graph.update(jid, depends_on=[d for d in deps if isinstance(d, str)] if isinstance(deps, list) else None,
                                        done_criteria=(str(args["done_criteria"]).strip() or None) if args.get("done_criteria") else None,
                                        description=str(args["description"]) if args.get("description") else None)
        except Exception as exc:
            return f"ERROR: {exc}"
        return f"updated {j.id} [{j.status}] needs {','.join(j.depends_on) or '-'}; done when: {j.done_criteria}"

    async def _jobs_table(self) -> str:
        rows = []
        for j in await self._plan_jobs():
            deps = ",".join(j.depends_on) or "-"
            rows.append(f"- {j.id} [{j.status}] {j.title} | risk {j.risk_ceiling} | needs {deps} | skills {','.join(j.budget.get('skills', []))} "
                        f"| bot {j.assigned_bot_id or '-'} | attempts {j.attempts}\n    done when: {j.done_criteria}")
        return "\n".join(rows) or "(no jobs yet: call plan_goal)"

    async def list_jobs(self, args: dict[str, Any], ctx: ToolContext) -> str:
        await self.graph.refresh(self.c.project_id)
        claims = await self.ledger.claims(project_id=self.c.project_id)
        pending = [f"- claim #{c['id']} on {c['job_id']} by {c['bot_id']} [{c['status']}]: {c['text'][:120]}" for c in claims if c["status"] == "submitted"]
        return await self._jobs_table() + ("\n\nClaims waiting for your decision:\n" + "\n".join(pending) if pending else "")

    async def list_team(self, args: dict[str, Any], ctx: ToolContext) -> str:
        busy = set(self.runner.active)
        rows = []
        for b in await self.registry.list():
            lane = "minimax" if b.chain and b.chain[0].startswith("minimax") else "cheap"
            state = "you" if b.id == BOSS_ID else ("busy" if b.id in busy else "idle")
            rows.append(f"- {b.id} \"{b.name}\" role={b.role} skills={','.join(b.skills) or '-'} tools={','.join(b.tools)} lane={lane} [{state}]")
        line = self.router.seats.queue()
        return "\n".join(rows) + (f"\nWaiting for a MiniMax seat: {', '.join(q['bot_id'] for q in line)}" if line else "")

    async def find_skills(self, args: dict[str, Any], ctx: ToolContext) -> str:
        words = [w for w in re.findall(r"[a-z0-9]+", str(args.get("query", "")).lower()) if len(w) > 2]
        scored = []
        for s in self.skills:
            hay = f"{s.name} {s.description}".lower()
            score = sum(hay.count(w) for w in words) + sum(3 for w in words if w in s.name.lower())
            if score:
                scored.append((score, s))
        scored.sort(key=lambda x: -x[0])
        return "\n".join(f"- {s.name}: {s.description[:140]}" for _, s in scored[:6]) or "no matching skills"

    async def create_bot(self, args: dict[str, Any], ctx: ToolContext) -> str:
        try:
            prof, lane = await self.factory.create(name=str(args.get("name") or "").strip() or "Worker", role=str(args.get("role") or "worker"),
                                                   description=str(args.get("description") or ""), skills=list(args.get("skills") or []),
                                                   tools=list(args.get("tools") or []), lane=str(args.get("lane") or "minimax"),
                                                   goal=self.c.project_id)
        except SpawnRefused as exc:
            return f"ERROR: not created: {exc}"
        est = await self.factory.estimate()
        return f"created {prof.id} \"{prof.name}\" ({prof.role}), lane {lane}, tools {','.join(prof.tools)}; ~{est['tokens_per_job']} tokens per job ({est['basis']})"

    async def assign_job(self, args: dict[str, Any], ctx: ToolContext) -> str:
        job = await self.graph.get(str(args.get("job_id") or ""))
        bot_id = str(args.get("bot_id") or "")
        if not job or job.project_id != self.c.project_id:
            return "ERROR: no such job in this project (see list_jobs)"
        if bot_id == BOSS_ID:                              # A15.e.03: a small job Omi does itself
            await self.graph.refresh(self.c.project_id)
            job = await self.graph.get(job.id)
            if job.status not in ("ready", "assigned"):
                return f"ERROR: job {job.id} is {job.status}; only a ready job can be taken"
            await self.graph.assign(job.id, BOSS_ID)
            await self.db.write("UPDATE jobs SET status='running', started_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (job.id,))
            return (f"{job.id} is yours: do it now with your tools (done when: {job.done_criteria or 'see the job'}), "
                    f"then complete_own_job(job_id='{job.id}', text, evidence).")
        bot = await self.registry.get(bot_id)
        if not bot or bot.status == "archived":
            return "ERROR: no such worker (see list_team)"
        if bot_id in self.runner.active:
            return f"ERROR: {bot_id} is busy; pick an idle bot or create one"
        await self.graph.refresh(self.c.project_id)
        job = await self.graph.get(job.id)
        if job.status not in ("ready", "assigned"):
            return f"ERROR: job {job.id} is not ready (it is {job.status}); finish its dependencies first"
        if RISK_ORDER.index(job.risk_ceiling) >= R3 and not await self._council_held():
            return f"ERROR: job {job.id} is {job.risk_ceiling}; hold a council on the approach first (council tool)"
        if args.get("instructions"):
            self.c.extra[job.id] = str(args["instructions"])
        try:
            job = await self.graph.assign(job.id, bot_id)
        except Exception as exc:
            return f"ERROR: {exc}"
        await self._start(job, bot_id)
        return f"started {bot_id} on {job.id} \"{job.title}\"; you'll hear back through wait_for_mention"

    async def _council_held(self) -> bool:
        rows = await self.db.read("SELECT id FROM messages WHERE topic=? AND message_type='COUNCIL_VERDICT' AND project_id=? LIMIT 1",
                                  (COUNCIL, self.c.project_id))
        return bool(rows)

    async def _rerun_tests(self, cid: int) -> tuple[list[str], str | None]:
        """A15.c.02: every `test` the claim cites runs again now, in the project folder. Returns
        (notes, failure) where failure is the real output of the first test that fails."""
        notes: list[str] = []
        sandbox = getattr(self.runner, "sandbox", None)
        for ev in await self.db.read("SELECT ref, detail_json FROM evidence WHERE claim_id=? AND kind='test'", (cid,)):
            ran = str(json.loads(ev["detail_json"] or "{}").get("ran") or ev["ref"])
            if sandbox is None:
                notes.append(f"{ran}: not re-run (no sandbox)")
                continue
            if ran.startswith("python <inline"):
                notes.append(f"{ran}: not re-run (inline code isn't kept; ask for tests in a file)")
                continue
            if ran.startswith("python ") and (self.c.folder / ran[7:].strip()).is_file():
                argv: list[str] | str = [sandbox_python(), *PY_FLAGS, str(self.c.folder / ran[7:].strip())]
                res = await sandbox.run(argv, cwd=self.c.folder, timeout=300, grant=[sandbox.new_run_dir(BOSS_ID)])
            else:
                res = await sandbox.run(ran, cwd=self.c.folder, timeout=300)
            if res.timed_out or res.exit_code != 0:
                return notes, f"`{ran}` fails now ({'timed out' if res.timed_out else f'exit {res.exit_code}'}):\n{res.output[-1500:]}"
            notes.append(f"{ran}: passes again (exit 0)")
        return notes, None

    async def _keep_live_url(self, cid: int) -> str | None:
        """A15.c.03: a checked url_quote marked live becomes the project's live address."""
        for ev in await self.db.read("SELECT ref, detail_json FROM evidence WHERE claim_id=? AND kind='url_quote'", (cid,)):
            d = json.loads(ev["detail_json"] or "{}")
            if d.get("live") and d.get("verified"):
                await self.db.write("UPDATE projects SET live_url=?, live_quote=?, live_status='ok', live_error=NULL, live_checked_at=? "
                                    "WHERE id=?", (ev["ref"], d.get("quote"), d.get("checked_at"), self.c.project_id))
                return ev["ref"]
        return None

    async def accept_claim(self, args: dict[str, Any], ctx: ToolContext) -> str:
        cid = int(args.get("claim_id") or 0)
        row = await self.db.read_one("SELECT * FROM claims WHERE id=?", (cid,))
        if not row or row["project_id"] != self.c.project_id:
            return "ERROR: no such claim in this project"
        if row["status"] == "submitted":
            notes, failure = await self._rerun_tests(cid)
            if failure:                                    # done means checked: a test that fails now is a rejection
                out = await self.reject_claim({"claim_id": cid, "reason": f"re-run on accept: {failure[:600]}",
                                               "new_instructions": "Make that test pass, run it again, then submit a new claim."}, ctx)
                return f"NOT accepted: {failure}\n→ {out}"
        else:
            notes = []
        try:
            await self.ledger.decide(cid, True, str(args.get("reason") or "evidence checks out"), BOSS_ID)
        except ValueError as exc:
            return f"ERROR: {exc}"
        job = await self.graph.get(row["job_id"]) if row["job_id"] else None
        if job:
            task = self.c.worker_tasks.get(job.id)
            if task and not task.done():
                await asyncio.wait_for(asyncio.shield(task), 300)     # let the worker finish its turn cleanly
            await self.graph._set(await self.graph.get(job.id), "completed")
            await self.graph.record_outcome(job.id, "completed")
        live = await self._keep_live_url(cid)
        ready = [j for j in await self.graph.ready(self.c.project_id)]
        return (f"claim #{cid} accepted." + (f" Tests re-run: {'; '.join(notes)}." if notes else "")
                + (f" Live at {live} (kept on the project)." if live else "")
                + (f" Now ready: {', '.join(f'{j.id} ({j.title})' for j in ready)}" if ready else ""))

    async def complete_own_job(self, args: dict[str, Any], ctx: ToolContext) -> str:
        """A15.e.03: Omi finishes a job it took. Its evidence gets the same checks as a worker's claim
        (files exist, commands really ran, pages opened, tests re-run); nothing is accepted on its word."""
        job = await self.graph.get(str(args.get("job_id") or ""))
        if not job or job.project_id != self.c.project_id or job.assigned_bot_id != BOSS_ID:
            return "ERROR: that isn't a job you took (assign_job it to omi first)"
        try:
            cid = await self.ledger.submit(bot_id=BOSS_ID, text=str(args.get("text") or ""), evidence=list(args.get("evidence") or []),
                                           job_id=job.id, project_id=self.c.project_id, workspace=self.c.folder, runs=ctx.runs)
        except EvidenceError as exc:
            return f"ERROR: not recorded: {exc}"
        notes, failure = await self._rerun_tests(cid)
        if failure:
            await self.ledger.decide(cid, False, f"re-run: {failure[:600]}", BOSS_ID)
            await self.graph._set(await self.graph.get(job.id), "ready", error="Omi's own test failed on re-run")
            return f"NOT done: {failure}\nThe job is ready again: fix it and complete_own_job again, or assign it to a worker."
        await self.ledger.decide(cid, True, "Omi's own job; evidence checked", BOSS_ID)
        await self.graph._set(await self.graph.get(job.id), "completed")
        await self.graph.record_outcome(job.id, "completed")
        live = await self._keep_live_url(cid)
        ready = await self.graph.ready(self.c.project_id)
        return (f"{job.id} done (claim #{cid}, checked)." + (f" Tests re-run: {'; '.join(notes)}." if notes else "")
                + (f" Live at {live}." if live else "") + (f" Now ready: {', '.join(j.id for j in ready)}" if ready else ""))

    # ── the tool relay (A8.d.02) ──────────────────────────────────────────
    async def relay_tool(self, args: dict[str, Any], ctx: ToolContext) -> str:
        return await self.runner.relay.relay(str(args.get("request_id") or ""), str(args.get("bot_id") or ""))

    async def decline_tool(self, args: dict[str, Any], ctx: ToolContext) -> str:
        return await self.runner.relay.decline(str(args.get("request_id") or ""), str(args.get("reason") or "no reason given"))

    async def answer_tool_request(self, args: dict[str, Any], ctx: ToolContext) -> str:
        return await self.runner.relay.answer(str(args.get("request_id") or ""), str(args.get("result") or ""))

    # ── scheduling for this project (A15.f.03) ─────────────────────────────
    async def add_routine(self, args: dict[str, Any], ctx: ToolContext) -> str:
        try:
            rid = await self.schedule.routines.add(str(args.get("name") or "routine"), str(args.get("goal") or ""),
                                                   str(args.get("schedule") or ""), project_id=self.c.project_id)
        except ValueError as exc:
            return f"ERROR: {exc} (schedule is cron: minute hour day month weekday, e.g. '0 8 * * 1' = Mondays 08:00)"
        await self.bus.publish(topic_project(self.c.project_id), "PROGRESS_UPDATE",
                               {"text": f"Omi added a routine for this project: '{args.get('name')}' ({args.get('schedule')}): {args.get('goal')}"},
                               sender_type="bot", sender_id=BOSS_ID, project_id=self.c.project_id)
        return f"routine {rid} added; it continues this project when it's due (the user sees it in Open projects and can remove it)"

    async def add_trigger(self, args: dict[str, Any], ctx: ToolContext) -> str:
        kind, config = str(args.get("kind") or ""), dict(args.get("config") or {})
        if kind == "file":                                 # the project folder is the bots' world (A15.e.04)
            p = Path(str(config.get("path") or ""))
            p = (p if p.is_absolute() else self.c.folder / p).resolve()
            if not p.is_relative_to(self.c.folder.resolve()):
                return "ERROR: a file trigger watches something inside this project's folder"
            config["path"] = str(p)
        try:
            tid = await self.schedule.triggers.add(str(args.get("name") or "trigger"), kind, config, str(args.get("goal") or ""),
                                                   project_id=self.c.project_id)
        except ValueError as exc:
            return f"ERROR: {exc}"
        return f"trigger {tid} added for this project"

    async def enqueue_night(self, args: dict[str, Any], ctx: ToolContext) -> str:
        goal = str(args.get("goal") or "").strip()
        if not goal:
            return "ERROR: enqueue_night needs a goal"
        row = await self.schedule.night.enqueue_saved(goal, self.c.project_id)
        return f"queued for the night shift (#{row}): it runs on the cheap lane while the user is away"

    async def reject_claim(self, args: dict[str, Any], ctx: ToolContext) -> str:
        cid = int(args.get("claim_id") or 0)
        row = await self.db.read_one("SELECT * FROM claims WHERE id=?", (cid,))
        if not row or row["project_id"] != self.c.project_id:
            return "ERROR: no such claim in this project"
        reason = str(args.get("reason") or "").strip()
        new = str(args.get("new_instructions") or "").strip()
        if not reason or not new:
            return "ERROR: say why (reason) and what to do differently (new_instructions)"
        try:
            await self.ledger.decide(cid, False, reason, BOSS_ID)
        except ValueError as exc:
            return f"ERROR: {exc}"
        job = await self.graph.get(row["job_id"])
        task = self.c.worker_tasks.get(job.id)
        if task and not task.done():
            await asyncio.wait_for(asyncio.shield(task), 300)
        job = await self.graph.get(job.id)
        if job.attempts >= job.max_attempts:
            await self.graph.record_outcome(job.id, "failed", error=f"claim rejected: {reason}")
            return f"claim #{cid} rejected; job {job.id} used its {job.max_attempts} attempts and was escalated (you or the user decide next)"
        self.c.extra[job.id] = f"Your previous attempt was rejected: {reason}. Do this instead: {new}"
        target = str(args.get("substitute_bot_id") or job.assigned_bot_id)
        await self.graph._set(job, "ready")
        await self.graph.assign(job.id, target)
        await self._start(await self.graph.get(job.id), target)
        return f"claim #{cid} rejected; {target} restarted on {job.id} with your new instructions"

    async def review_work(self, args: dict[str, Any], ctx: ToolContext) -> str:
        # found live in A14.a.02: Omi's own edits aren't committed until the report, so the reviewer judged a stale tree
        await self.projects.commit(self.c.project_id, "work before review", author=BOSS_ID)
        diff = await self.projects.diff(self.c.project_id)
        if not diff.strip():
            return "nothing to review yet (no changes in the project)"
        r = await review(self.router, reviewer_id="reviewer", chain=self.chain, task=self.c.goal, changes=diff[:40000])
        await self.bus.publish(topic_project(self.c.project_id), "REVIEW_RESULT", {"text": f"VERDICT {r.verdict}\n" + "\n".join(r.findings)},
                               sender_type="bot", sender_id="reviewer", recipient_id=BOSS_ID, project_id=self.c.project_id)
        return f"VERDICT: {r.verdict}\n" + ("\n".join(r.findings) or "no findings") + f"\n\n{r.text[-1500:]}"

    async def council(self, args: dict[str, Any], ctx: ToolContext) -> str:
        q = str(args.get("question") or "").strip()
        if not q:
            return "ERROR: council needs a question"
        rec = await hold_council(self.router, q, context=str(args.get("context") or ""), chain=self.chain,
                                 bus=self.bus, project_id=self.c.project_id)
        return rec.summary()

    async def ask_user(self, args: dict[str, Any], ctx: ToolContext) -> str:
        q = str(args.get("question") or "").strip()
        timeout = min(float(args.get("timeout_seconds") or 900), 86400)
        if not q:
            return "ERROR: ask_user needs a question"
        await self.bus.publish(topic_project(self.c.project_id), "QUESTION", {"text": q}, sender_type="bot", sender_id=BOSS_ID,
                               recipient_id="user", project_id=self.c.project_id)
        await ctx.event("state", "waiting_answer: " + q[:120])     # the chat now sends answers, not steers
        deferred, deadline = [], asyncio.get_running_loop().time() + timeout
        hold = ctx.waiting_on_user() if ctx.waiting_on_user else contextlib.nullcontext()
        try:
            hold.__enter__()                                  # waiting for the user: the time budget stops
            while True:
                left = deadline - asyncio.get_running_loop().time()
                if left <= 0:
                    return f"the user hasn't answered yet (waited {timeout:.0f}s); continue with your best judgement or ask again later"
                m = await self.inbox.sub.get(timeout=left)
                if m is None:
                    continue
                if m.sender_type == "user":
                    return f"The user answered: {m.text()}"
                deferred.append(m)
        finally:
            hold.__exit__(None, None, None)
            for m in deferred:                              # keep bot messages for wait_for_mention
                self.inbox.sub.queue.put_nowait(m)

    async def call_doctor(self, args: dict[str, Any], ctx: ToolContext) -> str:
        """A16.d.03: the user asked Omi to check the setup (dependencies, providers, folders, config)."""
        from omnibots.doctor.doctor import Finding, Report
        report = await self.doctor(fix=args.get("fix", True) is not False, online=bool(args.get("online")))
        rep = Report(findings=[Finding(**f) for f in report.get("findings", [])], mode=report.get("mode", ""))
        return rep.text() + ("\n(Keys are never shown. Anything the doctor couldn't repair needs the user: tell them the → step.)"
                             if rep.problems() else "")

    async def propose_goal(self, args: dict[str, Any], ctx: ToolContext) -> str:
        """A17.e.01: an idea for the user's inbox; nothing starts until the user accepts it."""
        try:
            pid = await self.initiative.add(str(args.get("title", "")), str(args.get("goal", "")), str(args.get("why", "")))
        except ValueError as exc:
            return f"NOT PROPOSED: {exc}"
        return f"proposed {pid}: it waits in the user's inbox; it starts only if they accept it"

    async def read_journal(self, args: dict[str, Any], ctx: ToolContext) -> str:
        days = await self.initiative.journal(max(1, min(30, int(args.get("days") or 7))))
        return "\n\n".join(f"{d['day']}: {d['entry']}" for d in days) or "(the journal is empty so far)"

    def tools(self) -> list[Tool]:
        T = lambda name, desc, props, fn, req=(), risk="R0", timeout=600.0: Tool(
            name, desc, {"type": "object", "properties": props, "required": list(req)}, risk, fn, timeout=timeout, path_arg=None)
        s = {"type": "string"}
        return [
            T("plan_goal", "Have the Planner turn the goal into jobs with done-criteria and dependencies (recorded as the project's plan).",
              {"goal": s, "context": s, "replan": {"type": "boolean"}}, self.plan_goal, timeout=900),
            T("list_jobs", "The plan's jobs, states, bots, attempts, and claims waiting for your decision.", {}, self.list_jobs),
            T("add_job", "Add a follow-up job to the plan (e.g. a correction after a review). Workers only act on assigned jobs.",
              {"title": s, "description": s, "done_criteria": s, "depends_on": {"type": "array", "items": s}, "risk": s},
              self.add_job, ["title", "done_criteria"]),
            T("cancel_job", "Cancel a job that's no longer needed or can't work as planned (stops its worker). Says which jobs it leaves blocked.",
              {"job_id": s, "reason": s}, self.cancel_job, ["job_id", "reason"], timeout=60),
            T("update_job", "Re-plan a job that hasn't started: new depends_on (e.g. the replacement of a cancelled job) and/or "
              "done_criteria/description. A job blocked by a dead dependency becomes runnable again.",
              {"job_id": s, "depends_on": {"type": "array", "items": s}, "done_criteria": s, "description": s},
              self.update_job, ["job_id"]),
            T("list_team", "The team: every bot's id, role, skills, tools, lane and whether it's idle or busy.", {}, self.list_team),
            T("find_skills", "Search the skill pool for a capability (returns skill names to give a new bot).", {"query": s}, self.find_skills, ["query"]),
            T("create_bot", f"Create a worker (Bot Factory, limited). tools from {sorted(KNOWN_TOOLS)}"
              + (f", or 'mcp:<server>' for an MCP server's tools (servers: {', '.join(self.runner.mcp_servers())})" if self.runner.mcp_servers() else "")
              + "; every worker also gets find_skill/invoke_skill/ask_help. lane 'minimax' (hard work) or 'cheap' (simple work).",
              {"name": s, "role": s, "description": s, "skills": {"type": "array", "items": s}, "tools": {"type": "array", "items": s}, "lane": s},
              self.create_bot, ["name", "role", "tools"]),
            T("assign_job", "Start an idle worker on a ready job. Optional extra instructions.",
              {"job_id": s, "bot_id": s, "instructions": s}, self.assign_job, ["job_id", "bot_id"]),
            T("complete_own_job", "Finish a small job you took yourself (assign_job to omi): your evidence is checked like a worker's claim.",
              {"job_id": s, "text": s, "evidence": {"type": "array", "items": {"type": "object", "properties": {
                  "kind": s, "ref": s, "exit_code": {"type": "integer"}, "quote": s, "live": {"type": "boolean"}}, "required": ["kind", "ref"]}}},
              self.complete_own_job, req=("job_id", "text", "evidence")),
            T("accept_claim", "Accept a claim whose evidence proves the job's done-criteria. The job becomes done and its dependents unlock.",
              {"claim_id": {"type": "integer"}, "reason": s}, self.accept_claim, ["claim_id"], timeout=400),
            T("reject_claim", "Reject a claim (partial, off-target, or measuring the wrong thing): say why and what to do instead; optionally give it to another bot.",
              {"claim_id": {"type": "integer"}, "reason": s, "new_instructions": s, "substitute_bot_id": s}, self.reject_claim,
              ["claim_id", "reason", "new_instructions"], timeout=400),
            T("review_work", "Have the independent reviewer check the project's actual changes against the goal.", {}, self.review_work, timeout=900),
            T("council", "Hold a council (3 independent views, then cross-examination) on a decision that matters.",
              {"question": s, "context": s}, self.council, ["question"], timeout=1200),
            T("ask_user", "Ask the user a question on the board and wait for their answer (only when the decision is truly theirs).",
              {"question": s, "timeout_seconds": {"type": "integer"}}, self.ask_user, ["question"], timeout=86500),
        ] + ([
            T("relay_tool", "A bot asked (TOOL_REQUEST) for a tool it doesn't hold: hand the request to a bot that holds it "
              "(list_team shows tools). It runs just that tool and its answer goes straight back; the run counts as the asking "
              "bot's evidence (a test it must cite). No holder? create one with that tool.", {"request_id": s, "bot_id": s},
              self.relay_tool, req=("request_id", "bot_id")),
            T("answer_tool_request", "Answer a tool request yourself (you ran the tool): the result goes to the bot that asked. It is your "
              "word, not a run: a bot can't cite it as evidence. When it needs a command to cite, use relay_tool.",
              {"request_id": s, "result": s}, self.answer_tool_request, req=("request_id", "result")),
            T("decline_tool", "Say no to a tool request, with the reason (e.g. it isn't needed, or it looks like a page told it to).",
              {"request_id": s, "reason": s}, self.decline_tool, req=("request_id", "reason")),
        ] if getattr(self.runner, "relay", None) is not None else []) + ([
            T("add_routine", "Add a routine for THIS project: a cron schedule ('0 8 * * 1' = Mondays 08:00) on which you get a round "
              "on it with that goal (e.g. a weekly check). The user can see and remove it.", {"name": s, "goal": s, "schedule": s},
              self.add_routine, req=("name", "goal", "schedule")),
            T("add_trigger", "Add a trigger for THIS project: kind file (config.path inside the project), board (config.type, "
              "optional topic/contains) or threshold (config.metric, below/above). When it fires you get a round with that goal.",
              {"name": s, "kind": s, "config": {"type": "object"}, "goal": s}, self.add_trigger, req=("name", "kind", "config", "goal")),
            T("enqueue_night", "Queue low-priority work for the night shift (the cheap lane, only while the user is away).",
              {"goal": s}, self.enqueue_night, req=("goal",)),
        ] if self.schedule is not None else []) + ([
            T("call_doctor", "Run the OmniBots doctor when the user asks, or when something looks broken (a provider "
              "fails, a folder or dependency is missing). It checks folders, settings, the database, Python packages, "
              "the provider config and keys (never shown), and old copies in other places; it repairs what it safely "
              "can (fix, default true) and never deletes. online=true also tests each provider key with its server.",
              {"fix": {"type": "boolean"}, "online": {"type": "boolean"}}, self.call_doctor, timeout=300,
              risk="R1"),
        ] if self.doctor is not None else []) + ([
            T("propose_goal", "Propose a goal the user might want next (it waits in their inbox; it never starts by itself). "
              "Only ideas that clearly help, that don't spend money, publish or contact anyone.",
              {"title": s, "goal": s, "why": s}, self.propose_goal, req=("title", "goal", "why")),
            T("read_journal", "Read the team's journal: one entry per day of what happened.", {"days": {"type": "integer"}},
              self.read_journal),
        ] if self.initiative is not None else [])
