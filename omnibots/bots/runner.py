"""The job runner (PLAN.md A5.a.03–05): give a bot a job, start to finish.

  1. record the job in SQL (`jobs`), mark the bot busy, set memory "Current Task"
  2. build the bot's prompt: its role + the shared user profile + its memory
  3. give it its assigned tools (+ board tools when a bus is attached)
  4. run it (BotAgent: seat lease, steering, risk gate, events)
  5. record the outcome in SQL, append the job to memory.md, distill lessons,
     summarize memory if it's over 32 KB, post WORK_STARTED / TASK_COMPLETED /
     TASK_FAILED on the job's thread
Keep-awake is held while any job runs (A0.c.03).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from omnibots.board.a2a import Inbox, a2a_tools, steering_pump
from omnibots.board.ledger import claim_tool
from omnibots.board.types import topic_job
from omnibots.bots.profile import BOSS_ID, BotProfile, BotRegistry, user_profile_path, user_profile_text
from omnibots.lineup import CHEAP_FIRST
from omnibots.providers.router import Router
from omnibots.runtime.agent import WORKER_PROMPT, BotAgent, TurnResult
from omnibots.runtime.approvals import ApprovalCenter
from omnibots.runtime.core_tools import core_registry
from omnibots.runtime.events import BotEvents
from omnibots.runtime.sandbox import Sandbox
from omnibots.runtime.tools import Tool, ToolContext, ToolRegistry

log = logging.getLogger(__name__)

BOSS_PROMPT = """You are Omi, the boss of the OmniBots team. You coordinate; you don't do the work yourself.
Your job: MONITOR the work, INQUIRE the right bot, RELAY what it needs (CORAL hub). Workers can only talk to you.

How you run a goal:
1. plan_goal: the Planner turns the goal into jobs with done-criteria. If it comes back with a question, ask_user it.
2. list_team, then for each ready job pick an idle bot with the right skills. Only if none fits, create_bot
   (lane "minimax" for hard reasoning/coding/research, "cheap" for simple writing or formatting). Reuse bots.
3. assign_job for each ready job (several at once when they don't depend on each other).
4. wait_for_mention to hear back. Workers send claims with evidence and a finish notice.
5. Check every claim against the job's done-criteria: accept_claim if the evidence really proves it,
   reject_claim with concrete new instructions if it's partial, off-target or measures the wrong thing.
   When a job is accepted, assign the jobs it unblocked.
6. Use council for vendor/architecture choices, spending, or anything R3+ (assign_job refuses R3+ jobs without one).
7. When every job is accepted, review_work on the project. If it needs fixes, add_job a follow-up and assign it
   (a worker whose job is over no longer reads messages). Then submit a short final result for the user.
Never do the workers' jobs yourself. Keep messages short and concrete. Ask the user only when a decision is truly theirs."""

LESSON_PROMPT = """You extract lessons from a finished job for the bot's memory.
Reply with 0 to 3 short, concrete, reusable lessons as '- ' bullets (things to do or avoid next time).
If there is nothing worth remembering, reply exactly: NONE"""


@dataclass
class JobOutcome:
    job_id: str
    bot_id: str
    status: str                    # completed | failed | cancelled | blocked
    result: TurnResult | None
    seconds: float
    lessons: list[str]


def _job_status(turn_status: str) -> str:
    return {"done": "completed", "max_iterations": "blocked", "exhausted": "blocked", "budget": "blocked",
            "error": "failed", "cancelled": "cancelled", "stuck": "blocked"}.get(turn_status, "failed")


class JobRunner:
    def __init__(self, *, db, registry: BotRegistry, router: Router, approvals: ApprovalCenter, home: Path,
                 sandbox: Sandbox, bus=None, ledger=None, keep_awake=None,
                 listener: Callable[[dict[str, Any]], Any] | None = None, learn: bool = True,
                 lesson_chain: list[str] | None = None, memory_limit: int | None = None,
                 locks=None, skill_pool=None, media_tools: list[Tool] | None = None, mcp=None, vault=None, budget=None):
        self.db, self.registry, self.router, self.approvals = db, registry, router, approvals
        self.home, self.sandbox, self.bus, self.ledger, self.keep_awake = home, sandbox, bus, ledger, keep_awake
        self.listener, self.learn = listener, learn
        self.lesson_chain = lesson_chain or list(CHEAP_FIRST)
        self.memory_limit = memory_limit
        self.locks, self.skill_pool, self.media_tools, self.mcp = locks, skill_pool, list(media_tools or []), mcp
        self.forge = None                                  # orchestrator.forge.ToolForge (A10.b.01)
        self.computers = None                              # runtime.computer_tools.ComputerClient (A10.f.04)
        self._browsers: dict[str, Any] = {}                # bot_id -> BrowserSession (persistent per bot, A10.a.01)
        self.vault = vault                                 # A9.c.01: resolves {{secret:x}} inside the tool layer
        self.budget = budget                               # A9.c.02: daily token caps and money caps
        self.active: dict[str, BotAgent] = {}             # bot_id -> running agent (for steering / pause)
        self.tasks: dict[str, asyncio.Task] = {}          # bot_id -> the task running its job (for stop)
        self.last_activity: dict[str, float] = {}         # bot_id -> monotonic time of its last event (stall detection)
        user_profile_path(home)                            # make sure the shared profile exists

    # ── prompt + tools ─────────────────────────────────────────────────
    def system_prompt(self, prof: BotProfile) -> str:
        base = BOSS_PROMPT if prof.is_boss else WORKER_PROMPT.format(name=prof.name, role=prof.role)
        from omnibots.runtime.context import today_line
        parts = [base, today_line()]
        if prof.description:
            parts.append(f"Your job description: {prof.description}")
        profile = user_profile_text(self.home)
        if profile:
            parts.append("What you know about the user (shared by the whole team):\n" + profile)
        memory = prof.memory.context()
        if memory:
            parts.append("Your memory (memory.md):\n" + memory)
        return "\n\n".join(parts)

    def pool(self, *, boss: bool = False) -> ToolRegistry:
        """Every tool a bot could be given (A8.b): core + web + shell/git + skills + board + MiniMax media."""
        reg = core_registry()
        if self.skill_pool is not None:
            from omnibots.runtime.skill_tools import skill_tools
            for t in skill_tools(self.skill_pool):
                reg.add(t)
        if self.locks is not None and self.bus is not None:
            from omnibots.runtime.more_tools import board_tools
            for t in board_tools(locks=self.locks, bus=self.bus, boss_id=BOSS_ID):
                if not (boss and t.name == "ask_help"):
                    reg.add(t)
        for t in self.media_tools:
            reg.add(t)
        from omnibots.runtime.browser import browser_tools
        for t in browser_tools(self._browser_for):
            reg.add(t)
        if self.computers is not None:
            from omnibots.runtime.computer_tools import computer_tools
            for t in computer_tools(self.computers):
                reg.add(t)
        if self.forge is not None:
            from omnibots.orchestrator.forge import forge_tool
            reg.add(forge_tool(self.forge))
            for t in self.forge.load_active():             # forged tools the team already made
                reg.add(t)
        return reg

    def _browser_for(self, ctx):
        from omnibots.runtime.browser import BrowserSession
        acct = "default"
        if ctx.bot_id not in self._browsers:
            self._browsers[ctx.bot_id] = BrowserSession(self.home / "profiles" / ctx.bot_id / acct)
        return self._browsers[ctx.bot_id]

    async def close_browsers(self, bot_id: str | None = None) -> None:
        for bid in ([bot_id] if bot_id else list(self._browsers)):
            sess = self._browsers.pop(bid, None)
            if sess is not None:
                try:
                    await sess.close()
                except Exception:
                    log.debug("browser close failed for %s", bid, exc_info=True)

    def mcp_servers(self) -> list[str]:
        return sorted(self.mcp.server_names()) if self.mcp else []

    async def mcp_tools_for(self, prof: BotProfile, emit: Callable | None = None) -> list[Tool]:
        """MCP tools named in the profile ("mcp:okf" or "mcp:okf.okf_search"). A server that fails
        to start costs the bot those tools, not the job."""
        from omnibots.mcp_client import mcp_refs
        out: list[Tool] = []
        for server, only in mcp_refs(prof.tools).items():
            if not self.mcp:
                break
            try:
                out += await self.mcp.tools(server, only)
            except Exception as exc:
                log.warning("MCP %s unavailable for %s: %s", server, prof.id, exc)
                if emit:
                    await emit("console", f"⚠ MCP server {server} is unavailable: {exc}")
        return out

    def tools_for(self, prof: BotProfile, inbox: Inbox | None, project_id: str | None) -> ToolRegistry:
        """A bot only gets the tools it was assigned (A8.b.03), plus the board tools of its role."""
        reg = self.pool(boss=prof.is_boss).subset(prof.tools)
        reg.vault = self.vault
        if self.bus and inbox:
            for t in a2a_tools(self.bus, inbox, bot_id=prof.id, boss_id=BOSS_ID, project_id=project_id,
                               is_active=lambda b: b in self.active):
                reg.add(t)
        if self.ledger and not prof.is_boss:
            reg.add(claim_tool(self.ledger, project_id=project_id))
        if prof.is_boss:
            reg.add(self._user_profile_tool())
        return reg

    def _user_profile_tool(self) -> Tool:
        home = self.home

        async def rehearse_profile(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
            from omnibots.runtime.core_tools import text_diff
            old = user_profile_path(home).read_text(encoding="utf-8") if user_profile_path(home).exists() else ""
            return {"diff": text_diff(old, str(args.get("content") or ""), "user_profile.md")}

        async def update_user_profile(args: dict[str, Any], ctx: ToolContext) -> str:
            text = str(args.get("content") or "")
            if not text.strip():
                return "ERROR: content is required (the whole new profile)"
            user_profile_path(home).write_text(text, encoding="utf-8", newline="\n")
            return "user profile updated"
        return Tool("update_user_profile",
                    "Replace the shared user profile (user_profile.md) with new content. The user must approve this.",
                    {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]},
                    "R3", update_user_profile, path_arg=None, summary=lambda a: f"update user profile ({len(str(a.get('content', '')))} chars)",
                    rehearse=rehearse_profile)

    # ── running a job ──────────────────────────────────────────────────
    async def run(self, bot_id: str, task: str, *, title: str | None = None, job_id: str | None = None,
                  project_id: str | None = None, workspace: Path | None = None, chain: list[str] | None = None,
                  budget: dict[str, Any] | None = None, extra_tools: list[Tool] | None = None,
                  max_iterations: int | None = None, inbox: Inbox | None = None) -> JobOutcome:
        """Run one job. `job_id` may be an existing (graph) job: it's restarted as a new attempt.
        `workspace`: a shared project folder instead of the bot's own. `chain`: override the
        profile's providers (the night shift uses the cheap lane). `budget`: {"tokens", "seconds"}."""
        prof = await self.registry.get(bot_id)
        if not prof or prof.status == "archived":
            raise ValueError(f"no active bot {bot_id}")
        job_id = job_id or f"job_{uuid.uuid4().hex[:10]}"
        title = (title or task.splitlines()[0])[:120]
        if project_id and not await self.db.read_one("SELECT id FROM projects WHERE id=?", (project_id,)):
            # A6 creates projects properly; until then a job may name one that doesn't exist yet.
            await self.db.write("INSERT INTO projects (id, goal, created_by) VALUES (?,?, 'user')", (project_id, title))
        if await self.db.read_one("SELECT id FROM jobs WHERE id=?", (job_id,)):
            await self.db.write(
                "UPDATE jobs SET status='running', assigned_bot_id=?, attempts=attempts+1, error_message=NULL, finished_at=NULL, "
                "started_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (bot_id, job_id))
        else:
            await self.db.write(
                "INSERT INTO jobs (id, project_id, title, description, status, assigned_bot_id, created_by, attempts, started_at) "
                "VALUES (?,?,?,?, 'running', ?, 'user', 1, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                (job_id, project_id, title, task, bot_id))
        budget = dict(budget or {})
        await self.registry.set_status(bot_id, "working")
        prof.memory.set_current_task(job_id, title)
        if self.bus:
            await self.bus.publish(topic_job(job_id), "WORK_STARTED", {"title": title}, sender_type="bot", sender_id=bot_id,
                                   job_id=job_id, project_id=project_id)
        own_inbox = inbox is None
        inbox = inbox or (await Inbox.open(self.bus, bot_id) if self.bus else None)
        tools = self.tools_for(prof, inbox, project_id)
        events = BotEvents(bot_id, self.db, listener=self._listen)
        for t in [*await self.mcp_tools_for(prof, events.emit), *(extra_tools or [])]:
            tools.add(t)
        agent = BotAgent(bot_id=bot_id, name=prof.name, role=prof.role, workspace=workspace or prof.workspace, router=self.router,
                         chain=chain or prof.chain, tools=tools, approvals=self.approvals,
                         events=events, sandbox=self.sandbox,
                         system_prompt=self.system_prompt(prof), max_iterations=max_iterations or int(prof.limits.get("max_iterations", 40)),
                         priority="boss" if prof.is_boss else "work", token_budget=budget.get("tokens"), budget=self.budget)
        self.active[bot_id] = agent
        self.tasks[bot_id] = asyncio.current_task()
        self.last_activity[bot_id] = time.monotonic()
        pump = asyncio.create_task(steering_pump(self.bus, agent)) if self.bus else None
        if self.keep_awake:
            self.keep_awake.acquire()
        t0 = time.monotonic()
        result: TurnResult | None = None
        status = "failed"
        time_error = None
        try:
            if budget.get("seconds"):
                result = await asyncio.wait_for(agent.run(task, job_id=job_id), float(budget["seconds"]))
            else:
                result = await agent.run(task, job_id=job_id)
            status = _job_status(result.status)
        except asyncio.TimeoutError:
            status, time_error = "blocked", f"time budget of {budget['seconds']}s used up"
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        finally:
            seconds = time.monotonic() - t0
            if self.keep_awake:
                self.keep_awake.release()
            if pump:
                pump.cancel()
            if inbox and own_inbox:
                inbox.close()
            self.active.pop(bot_id, None)
            self.tasks.pop(bot_id, None)
            answer = (result.answer if result else "") or ""
            error = time_error or (result.error if result else None) or (None if status == "completed" else f"job {status}")
            await self.db.write("UPDATE jobs SET status=?, result_summary=?, error_message=?, finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
                                (status, answer[:4000], error, job_id))
            await self.registry.set_status(bot_id, "idle")
            prof.memory.add_job(job_id, title, status, answer or (error or ""))
            prof.memory.clear_current_task()
            if self.bus:
                kind, payload = ("TASK_COMPLETED", {"result": answer[:2000] or "(no text)"}) if status == "completed" else ("TASK_FAILED", {"error": error or status})
                await self.bus.publish(topic_job(job_id), kind, payload, sender_type="bot", sender_id=bot_id, job_id=job_id, project_id=project_id,
                                       recipient_id=None if prof.is_boss else BOSS_ID)
        lessons: list[str] = []
        if self.learn and result is not None:
            lessons = await self._lessons(bot_id, task, result)
            prof.memory.add_lessons(lessons)
        await prof.memory.summarize_if_needed(self._summarize, **({"limit": self.memory_limit} if self.memory_limit else {}))
        return JobOutcome(job_id, bot_id, status, result, seconds, lessons)

    async def _lessons(self, bot_id: str, task: str, result: TurnResult) -> list[str]:
        trail = []
        for m in result.messages[-24:]:
            if m.get("role") == "tool" and str(m.get("content", "")).startswith(("ERROR", "DENIED")):
                trail.append(f"tool {m.get('name')}: {str(m['content'])[:200]}")
        brief = (f"TASK: {task[:1500]}\nOUTCOME: {result.status}\nFINAL ANSWER: {result.answer[:1500]}\n"
                 f"STEPS: {result.steps}, TOOL CALLS: {result.tool_calls}\nPROBLEMS:\n" + ("\n".join(trail) or "none"))
        try:
            routed = await self.router.chat(f"{bot_id}:memory", self.lesson_chain,
                                            [{"role": "system", "content": LESSON_PROMPT}, {"role": "user", "content": brief}])
        except Exception:
            log.warning("lesson extraction failed for %s", bot_id, exc_info=True)
            return []
        text = routed.result.answer.strip()
        if not text or text.upper().startswith("NONE"):
            return []
        return [l.strip()[2:].strip() for l in text.splitlines() if l.strip().startswith(("- ", "* "))][:3]

    async def _summarize(self, instruction: str, text: str) -> str:
        routed = await self.router.chat("memory:summarizer", self.lesson_chain,
                                        [{"role": "system", "content": instruction}, {"role": "user", "content": text[:60000]}])
        return routed.result.answer

    def _listen(self, e: dict[str, Any]):
        self.last_activity[e["bot_id"]] = time.monotonic()
        return self.listener(e) if self.listener else None

    # ── controls ───────────────────────────────────────────────────────
    def cancel(self, bot_id: str) -> bool:
        """Stop a bot's current job now (its sandboxed processes are killed)."""
        task = self.tasks.get(bot_id)
        if task and not task.done():
            task.cancel()
            return True
        return False

    def steer(self, bot_id: str, note: str) -> bool:
        agent = self.active.get(bot_id)
        if agent:
            agent.steer(note)
        return bool(agent)

    # ── stats (A5.a.04) ────────────────────────────────────────────────
    async def stats(self, bot_id: str) -> dict[str, Any]:
        rows = await self.db.read("SELECT status, COUNT(*) AS n FROM jobs WHERE assigned_bot_id=? GROUP BY status", (bot_id,))
        by = {r["status"]: r["n"] for r in rows}
        finished = sum(by.get(k, 0) for k in ("completed", "failed", "blocked"))
        t = await self.db.read_one(
            "SELECT AVG((julianday(finished_at) - julianday(started_at)) * 86400.0) AS s FROM jobs "
            "WHERE assigned_bot_id=? AND finished_at IS NOT NULL AND started_at IS NOT NULL", (bot_id,))
        u = await self.db.read_one(
            "SELECT COALESCE(SUM(COALESCE(tokens_in,0)+COALESCE(tokens_out,0)),0) AS tok, COALESCE(SUM(cost_usd),0) AS cost, "
            "COALESCE(SUM(estimated),0) AS est, COUNT(*) AS calls FROM provider_usage_events WHERE bot_id=?", (bot_id,))
        return {"bot_id": bot_id, "jobs": sum(by.values()), "by_status": by,
                "success_rate": round(by.get("completed", 0) / finished, 3) if finished else None,
                "avg_seconds": round(t["s"], 1) if t and t["s"] is not None else None,
                "tokens": u["tok"], "cost_usd": u["cost"], "model_calls": u["calls"], "estimated_calls": u["est"]}
