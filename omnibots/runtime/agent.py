"""The bot's turn loop (PLAN.md A3.a.01–06): a port of Omni's runTurn.

Each step: compact the context if needed -> deliver steering notes -> call the
model through the provider router (A2) -> parse tool calls (native or text
protocol) -> risk-gate each call (§3.1) -> run the batch (same-file calls in
order, different files in parallel) -> feed results back. Ends when the model
answers without a tool call, the step budget runs out, or it's cancelled.
Everything is narrated as bot events: console, terminal, thinking, state.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omnibots.lineup import MINIMAX
from omnibots.providers.router import AllProvidersExhausted, Router
from omnibots.providers.toolcalls import (
    build_param_registry, extract_think, has_tool_intent, parse_text_tool_calls, recovery_message,
    strip_think, strip_tool_call_text,
)
from omnibots.board.types import topic_project
from omnibots.runtime.approvals import ALLOCATED, MORE_TOKENS, ApprovalCenter
from omnibots.runtime.context import first_user_goal, maybe_auto_compact, steering_message, trim_old_tool_results
from omnibots.runtime.events import BotEvents, ToolTextFilter
from omnibots.runtime.sandbox import Sandbox
from omnibots.runtime.scope import outside_paths
from omnibots.runtime.tools import RISK_ORDER, RISK_TEXT, ToolContext, ToolRegistry, args_digest, target_host

log = logging.getLogger(__name__)

MAX_PARSE_RECOVERIES = 3

WORKER_PROMPT = """You are {name}, an OmniBots worker: a clone of Omi, specialised as a {role}.
You work in your own workspace folder; relative paths in tools are resolved there.
Work step by step with your tools. Run code with run_python (it runs in a sandbox), and check results before claiming success.
MISSING A TOOL? Don't give up and don't guess: if the job needs a tool you don't have (e.g. run_shell, git_push, the browser),
call request_tool(tool, args, why). Omi has a bot that holds it run it for you, and the result comes back to you.
SKILLS: the team has a library of expert instructions (skills). Before you start, call find_skill with a few words about the
task (e.g. "css modern website", "python tests"); if one fits, invoke_skill it and follow it. Load the skills listed for you below first.
Keep answers short and concrete. When the task is done, reply with a brief report of what you did and the evidence (file names, command output). Do not call a tool after you are finished.
The board is read for you on a short cycle after this job, including a new assignment. Finish this job; do not wait inside it for the next one.
If the user sends a note while you work, follow it: it corrects your course."""


BOSS_REVIEWER = "omi:review"                # A16.e: whose tokens Omi's review of a token ask counts under
LOOP_WARN, LOOP_STOP = 3, 5
LOOP_EXEMPT = {"wait_for_mention"}                       # waiting again is not a loop
LOOP_NOTE = ("[system note] You made the exact same tool call with the exact same result 3 times in a row. Repeating it "
             "will not change anything. Try a different approach, check your assumption (read the file, inspect the output), "
             "or stop and report what is blocking you. Two more identical repeats will end this job.")


def loop_signature(results) -> str | None:
    """One step's calls AND results. None when the step only waited."""
    items = []
    for call, name, result in results:
        if name in LOOP_EXEMPT:
            return None
        fn = call.get("function") or {}
        try:
            args = json.dumps(json.loads(fn.get("arguments") or "{}"), sort_keys=True)
        except (TypeError, ValueError):
            args = str(fn.get("arguments"))
        items.append(f"{name}|{args}|{result}")
    return "\n".join(sorted(items)) or None


@dataclass
class TurnResult:
    status: str                         # done | max_iterations | cancelled | exhausted | error
    answer: str
    steps: int
    messages: list[dict[str, Any]]
    tool_calls: int = 0
    error: str | None = None
    models_used: list[str] = field(default_factory=list)
    runs: list[dict[str, Any]] = field(default_factory=list)   # commands really run (ToolContext.runs), set by the runner


class BotAgent:
    def __init__(self, *, bot_id: str, name: str, role: str, workspace: Path, router: Router, chain: list[str],
                 tools: ToolRegistry, approvals: ApprovalCenter, events: BotEvents, sandbox: Sandbox | None = None,
                 system_prompt: str | None = None, max_iterations: int = 40, priority: str = "work",
                 approval_timeout: float | None = None, seat_lease: bool | None = None, token_budget: int | None = None,
                 budget=None, risk_ceiling: str | None = None, summary_prefix: str = "", review_chain: list[str] | None = None):
        self.bot_id, self.name, self.role = bot_id, name, role
        self.budget = budget                     # A9.c.02: security.budget.Budget (daily token caps, spend caps)
        self.risk_ceiling = risk_ceiling         # A8.d.03: a tool whose base risk is above it is refused
        self.summary_prefix = summary_prefix     # A8.d.02: "requested by X, run by Y: " on a relay job's lines and cards
        self.review_chain = review_chain         # A16.e: Omi looks at a token ask first (cheap lane); None = no review
        self.workspace = workspace
        workspace.mkdir(parents=True, exist_ok=True)
        self.router, self.chain = router, chain
        self.tools, self.approvals, self.events = tools, approvals, events
        self.sandbox = sandbox
        self.system_prompt = system_prompt or WORKER_PROMPT.format(name=name, role=role)
        self.max_iterations = max_iterations
        self.priority = priority
        self.approval_timeout = approval_timeout
        self._steer: list[str] = []
        self.step = 0
        self.token_budget = token_budget         # A6.a.03: stop cleanly once the job has used this many tokens
        self.tokens_used = 0
        self._go = asyncio.Event()               # A7.c.01: cleared = paused at the next step boundary
        self._go.set()
        # time spent waiting on the user (approvals, questions, a pause) doesn't use the time budget
        self._held, self._holds, self._hold_since = 0.0, 0, None
        # Waiting list (A4.b.01): a MiniMax-first bot books a seat for the whole job.
        self.seat_lease = seat_lease if seat_lease is not None else bool(chain) and chain[0].split("/")[0].split("::")[0] == MINIMAX
        self.seat = None

    @contextlib.contextmanager
    def waiting_on_user(self):
        """While the bot waits for YOU, its time budget stops (live bug 2026-09-26: a goal 'failed' after
        30 min parked on an approval nobody saw)."""
        self._holds += 1
        if self._holds == 1:
            self._hold_since = time.monotonic()
        try:
            yield
        finally:
            self._holds -= 1
            if self._holds == 0 and self._hold_since is not None:
                self._held += time.monotonic() - self._hold_since
                self._hold_since = None

    def held_seconds(self) -> float:
        return self._held + ((time.monotonic() - self._hold_since) if self._hold_since is not None else 0.0)

    # ── pause / resume (A7.c.01): take effect at the next step boundary ──
    def pause(self) -> None:
        self._go.clear()

    def resume(self) -> None:
        self._go.set()

    @property
    def paused(self) -> bool:
        return not self._go.is_set()

    async def _wait_if_paused(self) -> None:
        if self._go.is_set():
            return
        ev = self.events
        had_seat = self.seat is not None
        if had_seat:                              # give the MiniMax seat back while paused
            self.router.seats.release(self.seat)
            self.seat = None
        await ev.set_state("sleeping", "paused by the user")
        await ev.emit("console", "⏸ paused")
        with self.waiting_on_user():
            await self._go.wait()
        await ev.emit("console", "▶ resumed")
        if had_seat:
            await self._book_seat()

    # ── steering (A3.a.03): called from the UI thread via the engine ────
    def steer(self, note: str) -> None:
        if note and note.strip():
            self._steer.append(note.strip())

    def _take_steering(self) -> list[str]:
        notes, self._steer = self._steer, []
        return notes

    # ── the loop ───────────────────────────────────────────────────────
    async def run(self, task: str, *, messages: list[dict[str, Any]] | None = None, job_id: str | None = None) -> TurnResult:
        ev = self.events
        ev.job_id = job_id
        msgs = messages if messages is not None else [{"role": "system", "content": self.system_prompt}]
        msgs.append({"role": "user", "content": task})
        goal = first_user_goal(msgs)
        schemas = self.tools.schemas()
        registry = build_param_registry(schemas)
        ctx = ToolContext(bot_id=self.bot_id, workspace=self.workspace, job_id=job_id, emit=ev.emit, sandbox=self.sandbox,
                          waiting_on_user=self.waiting_on_user)
        self.last_runs = ctx.runs
        recoveries, nudged, total_calls, models = 0, False, 0, []
        last_step, repeats = None, 0                     # stuck-loop guard (A3.a.10)
        await ev.emit("console", f"▶ task: {task}")
        try:
            if self.seat_lease:
                await self._book_seat()
            for i in range(self.max_iterations):
                self.step = i + 1
                await self._wait_if_paused()
                maybe_auto_compact(msgs, context_window=None, max_tokens=8192, session_goal=goal)
                for note in self._take_steering():
                    msgs.append({"role": "user", "content": steering_message(note)})
                    await ev.emit("console", f"↳ steer received at step {self.step}: {note}")
                if not nudged and self.max_iterations - i == 3:
                    nudged = True
                    msgs.append({"role": "user", "content": "[system note] Only 3 tool iterations remain this turn. Finish the smallest complete unit of work, verify if possible, then summarize what is done and what remains."})

                if (self.budget and self.priority != "boss" and (over := await self.budget.check_allocation(job_id, self.bot_id))
                        and not await self._ask_for_more_tokens(over, msgs, job_id)):
                    why = (f"this bot's background tokens for today in this project are used up "
                           f"({over['used']:,}/{over['cap']:,}) and no more were allocated")
                    await ev.emit("console", f"■ {why}; stopping here")
                    await ev.set_state("blocked", "background tokens used up")
                    await ev.flush()
                    return TurnResult("budget", "", self.step, msgs, total_calls, error=why, models_used=models)
                if self.budget and (why := await self.budget.check_tokens(self.bot_id)):
                    await ev.emit("console", f"■ {why}; stopping here")
                    await ev.set_state("blocked", "token cap reached")
                    await ev.flush()
                    return TurnResult("budget", "", self.step, msgs, total_calls, error=why, models_used=models)
                await ev.set_state("thinking")
                routed = await self._call_model(msgs, schemas, job_id)
                models.append(routed.model.key)
                self.tokens_used += (routed.tokens_in or 0) + (routed.tokens_out or 0)
                msg = dict(routed.result.message)
                raw = strip_think(msg.get("content") or "") if routed.result.thinking else extract_think(msg.get("content") or "")["rest"]
                text_mode = not routed.model.native_tools
                if text_mode and raw:
                    calls = parse_text_tool_calls(raw, registry)
                    if calls:
                        msg["tool_calls"] = calls
                        msg["content"] = strip_tool_call_text(raw).strip()
                    else:
                        msg["content"] = raw.strip()
                else:
                    msg["content"] = raw.strip()
                msgs.append(msg)
                calls = msg.get("tool_calls") or []

                if not calls:
                    truncated = routed.result.finish_reason == "length"
                    attempted = text_mode and has_tool_intent(raw)
                    if (attempted or (truncated and text_mode)) and recoveries < MAX_PARSE_RECOVERIES:
                        recoveries += 1
                        await ev.emit("console", f"⚠ {'truncated' if truncated else 'malformed'} tool call; asking the model to re-emit ({recoveries}/{MAX_PARSE_RECOVERIES})")
                        msgs.append({"role": "user", "content": recovery_message()})
                        continue
                    await ev.set_state("done")
                    await ev.flush()
                    return TurnResult("done", msg["content"], self.step, msgs, total_calls, models_used=models)

                total_calls += len(calls)
                results = await self._run_calls(calls, ctx, job_id)
                for call, name, result in results:
                    msgs.append({"role": "tool", "tool_call_id": call["id"], "name": name, "content": result})
                step_sig = loop_signature(results)
                repeats = repeats + 1 if step_sig is not None and step_sig == last_step else 1
                last_step = step_sig
                if repeats >= LOOP_STOP:
                    what = ", ".join(sorted({n for _, n, _ in results}))
                    await ev.emit("console", f"■ stuck: the same {what} call gave the same result {repeats} times in a row; stopping")
                    await ev.set_state("blocked", "stuck in a loop")
                    await ev.flush()
                    return TurnResult("stuck", "", self.step, msgs, total_calls,
                                      error=f"stuck in a loop: {what} repeated {repeats}x with the same result", models_used=models)
                if repeats == LOOP_WARN:
                    await ev.emit("console", f"⚠ repeated the same call {repeats} times; warning the bot")
                    msgs.append({"role": "user", "content": LOOP_NOTE})
                if self.token_budget and self.tokens_used >= self.token_budget:
                    await ev.emit("console", f"■ token budget used up ({self.tokens_used}/{self.token_budget}); stopping here")
                    await ev.set_state("blocked", "token budget used up")
                    await ev.flush()
                    return TurnResult("budget", "", self.step, msgs, total_calls,
                                      error=f"token budget of {self.token_budget} used up ({self.tokens_used})", models_used=models)
            await ev.emit("console", f"■ stopped after {self.max_iterations} steps")
            await ev.set_state("blocked", "step budget used up")
            await ev.flush()
            return TurnResult("max_iterations", "", self.step, msgs, total_calls, models_used=models)
        except asyncio.CancelledError:
            await ev.set_state("stopped")
            await ev.flush()
            raise
        except AllProvidersExhausted as exc:
            await ev.emit("console", f"✖ {exc}")
            await ev.set_state("rate_limited", "every provider is unavailable")
            await ev.flush()
            return TurnResult("exhausted", "", self.step, msgs, total_calls, error=str(exc), models_used=models)
        except Exception as exc:
            log.exception("bot %s failed", self.bot_id)
            await ev.emit("console", f"✖ error: {exc}")
            await ev.set_state("error", str(exc)[:200])
            await ev.flush()
            return TurnResult("error", "", self.step, msgs, total_calls, error=str(exc), models_used=models)
        finally:
            if self.seat is not None:
                self.router.seats.release(self.seat)
                self.seat = None

    async def _book_seat(self) -> None:
        seats, ev = self.router.seats, self.events
        if seats.held_by(self.bot_id):
            self.seat = await seats.acquire(self.bot_id, self.priority)
            return
        if seats.free_count(self.bot_id) == 0 or seats.queue():
            ahead = len([q for q in seats.queue() if q["bot_id"] != seats.boss_id]) + 1
            await ev.set_state("waiting_seat", f"#{ahead} in line")
            await ev.emit("console", f"⏳ all MiniMax seats are busy: waiting in line (#{ahead})")
        self.seat = await seats.acquire(self.bot_id, self.priority)
        await ev.emit("console", f"✔ MiniMax seat {self.seat.number} booked for this job")

    async def _ask_for_more_tokens(self, over: dict[str, Any], msgs, job_id) -> bool:
        """A9.c.03: this bot's background tokens for today in this project are used up. It asks Omi on
        the board with its estimate; Omi puts the request to the user (a card in Omi's window).
        Yes → that much more for this bot today, and it goes on from the same step."""
        from omnibots.bots.profile import BOSS_ID              # here, not at the top: bots imports runtime
        ev = self.events
        pid, used, cap = over["project_id"], over["used"], over["cap"]
        await ev.emit("console", f"■ my background tokens for today are used up ({used:,}/{cap:,}); asking Omi for more")
        est, why = await self._estimate_tokens_left(msgs, job_id)
        verdict, note = await self._omi_review(msgs, est, why, job_id) if self.review_chain else ("", "")
        if verdict == "no":                                    # A16.e: Omi turns it down itself; only the user can say yes
            await ev.emit("console", f"✖ Omi turned down my request for more tokens: {note}")
            if self.budget.bus:
                await self.budget.bus.publish(
                    topic_project(pid), "PROGRESS_UPDATE",
                    {"text": f"Omi turned down {self.name}'s request for {est:,} more tokens: {note} {self.name} stops and reports."},
                    sender_type="bot", sender_id=BOSS_ID, project_id=pid, job_id=job_id)
            return False
        if self.budget.bus:
            await self.budget.bus.publish(
                topic_project(pid), "HELP_REQUEST",
                {"text": f"I used my {cap:,} background tokens for today in this project. I need about {est:,} more to finish: {why}",
                 "kind": MORE_TOKENS, "tokens": est},
                sender_type="bot", sender_id=self.bot_id, recipient_id=BOSS_ID, project_id=pid, job_id=job_id)
        summary = (f"{self.name} used its {cap:,} background tokens for today on this project and needs about {est:,} more "
                   f"to finish: {why} Approve to allocate {est:,} more tokens to {self.name} for today."
                   + (f" Omi's view: {note}" if note else ""))
        await ev.set_state("waiting_approval", "more tokens (asked Omi)")
        with self.waiting_on_user():
            decision = await self.approvals.request(
                bot_id=BOSS_ID, job_id=job_id, tool=MORE_TOKENS, risk="R2", summary=summary,
                rehearsal={"bot": self.bot_id, "bot_name": self.name, "project": pid,
                           "used_today": used, "allocated_today": cap, "asking_for": est})
        if not decision.approved:
            return False
        if not decision.reason.startswith(ALLOCATED):           # the allocations window already added it
            # a step can overshoot: the grant covers that too, so `est` really is what's left to spend
            await self.budget.allocate(pid, self.bot_id, est + max(0, used - cap))
        await ev.emit("console", f"✔ more tokens allocated (today: {await self.budget.allocation_today(pid, self.bot_id):,})")
        return True

    async def _omi_review(self, msgs, est: int, why: str, job_id) -> tuple[str, str]:
        """A16.e: one short look by Omi (cheap lane) at the bot's recent steps: ("fair" | "no", one sentence).
        Anything that goes wrong here lets the ask through without an opinion; it never blocks it."""
        steps = []
        for m in msgs[-16:]:
            if m.get("role") == "assistant":
                for c in m.get("tool_calls") or []:
                    fn = c.get("function") or {}
                    steps.append(f"called {fn.get('name')}({str(fn.get('arguments') or '')[:100]})")
            elif m.get("role") == "tool":
                steps.append("→ " + " ".join(str(m.get("content") or "").split())[:160])
        prompt = ("You are Omi, the boss of a team of bots. A worker ran out of today's tokens for background work and asks "
                  f"for about {est:,} more to finish. Its reason: {why}\nIts last steps:\n" + "\n".join(steps[-14:]) +
                  "\n\nIs it making real progress (fair), or is it looping, stuck on the same failure, or off-task (no)? "
                  'Reply with ONLY JSON: {"verdict": "fair" or "no", "note": "<one short sentence the user will read>"}')
        try:
            routed = await self.router.chat(f"{BOSS_REVIEWER}", list(self.review_chain), [{"role": "user", "content": prompt}], None,
                                            job_id=job_id, priority="review")
            raw = routed.result.answer or ""
            data = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
            verdict = "no" if str(data.get("verdict", "")).strip().lower().startswith("no") else "fair"
            return verdict, " ".join(str(data.get("note") or "").split())[:200]
        except Exception as exc:
            log.info("Omi's review of %s's token ask failed: %s", self.bot_id, exc)
            return "", ""

    async def _estimate_tokens_left(self, msgs, job_id) -> tuple[int, str]:
        """One short, tool-less call: the bot's own guess of the tokens it still needs. Falls back to
        what this job used so far when the model gives no usable number."""
        est, why = 0, ""
        note = ("[system note] This project's token budget for today is used up; the user decides whether to add more. "
                "Do not call tools. Reply with ONLY this JSON: {\"tokens\": <additional tokens you need to finish this job>, "
                f"\"why\": \"<one short sentence: what is left to do>\"}}. For scale, this job has used {self.tokens_used:,} tokens so far.")
        try:
            routed = await self.router.chat(self.bot_id, self.chain, trim_old_tool_results(msgs + [{"role": "user", "content": note}]),
                                            None, job_id=job_id, priority=self.priority)
            self.tokens_used += (routed.tokens_in or 0) + (routed.tokens_out or 0)
            raw = routed.result.message.get("content") or ""
            raw = strip_think(raw) if routed.result.thinking else extract_think(raw)["rest"]
            start, end = raw.find("{"), raw.rfind("}")
            data = json.loads(raw[start:end + 1])
            est, why = int(float(data.get("tokens") or 0)), " ".join(str(data.get("why") or "").split())[:300]
        except Exception as e:                        # no answer, not JSON, providers busy: fall back below
            log.info("token estimate failed for %s: %s", self.bot_id, e)
        if est <= 0:
            est = max(50_000, self.tokens_used)
            why = why or "(no estimate from the model; this is about what the job used so far)"
        est = min(max(est, 10_000), 2_000_000)
        est = -(-est // 10_000) * 10_000              # round up to the next 10k
        return est, (why if why.endswith((".", "!", "?", ")")) else why + ".")

    async def _call_model(self, msgs, schemas, job_id):
        ev = self.events
        filt = ToolTextFilter()
        started = {"answer": False}

        async def on_token(t: str) -> None:
            out = filt.feed(t)
            if out:
                if not started["answer"]:
                    started["answer"] = True
                    await ev.set_state("thinking", "writing")
                await ev.emit("console", out, stream=True)

        async def on_think(t: str) -> None:
            await ev.emit("thinking", t, stream=True)

        def on_hop(h: dict[str, Any]) -> None:
            if h.get("outcome") in ("rate_limited", "auth_failed", "retry", "gave_up", "waiting", "no_free_seat"):
                asyncio.ensure_future(ev.emit("console", f"⟳ {h.get('model', '')} {h['outcome']}"
                                              + (f" ({h.get('error')})" if h.get("error") else "")
                                              + (f", waiting {h['seconds']}s for {h['for']}" if h["outcome"] == "waiting" else "")))

        routed = await self.router.chat(self.bot_id, self.chain, trim_old_tool_results(msgs), schemas, job_id=job_id,
                                        priority=self.priority, on_token=on_token, on_think=on_think, on_hop=on_hop)
        tail = filt.flush()
        if tail:
            await ev.emit("console", tail, stream=True)
        await ev.flush()
        return routed

    async def _run_calls(self, calls, ctx: ToolContext, job_id) -> list[tuple[dict, str, str]]:
        ev = self.events
        parsed = []
        for call in calls:
            fn = call.get("function") or {}
            name = fn.get("name") or ""
            try:
                args = json.loads(fn.get("arguments") or "{}")
                if not isinstance(args, dict):
                    args = {}
            except ValueError:
                args = {}
            tool = self.tools.get(name)
            summary = self.summary_prefix + (tool.describe(args) if tool else name)
            await ev.emit("console", f"▸ {summary}")
            parsed.append((call, name, args, tool, summary))

        # Risk gate, one call at a time so approval cards don't interleave.
        allowed: dict[int, str | None] = {}
        approved: dict[int, tuple[str, float | None, str | None]] = {}
        for idx, (call, name, args, tool, summary) in enumerate(parsed):
            if tool is None:
                allowed[idx] = None
                continue
            if self.risk_ceiling and RISK_ORDER.index(tool.risk) > RISK_ORDER.index(self.risk_ceiling):
                # A8.d.03: the bot's limit is on what kind of tool it may use at all; a call that becomes riskier
                # (outside the project, destructive) still goes to the user below, as before
                allowed[idx] = (f"REFUSED: {name} is {tool.risk}, above your limit ({self.risk_ceiling}). Don't retry; ask Omi, "
                                "or request_tool so a bot that may run it does it for you.")
                await ev.emit("console", f"✖ above my limit ({tool.risk} > {self.risk_ceiling}): {summary}")
                continue
            risk = tool.risk_for(args, ctx)
            # A15.e.04 (user, 2026-09-28): leaving the project folder always asks, whatever `ask_from` says
            outside = outside_paths(name, args, ctx.workspace)
            if outside:
                risk = risk if RISK_ORDER.index(risk) >= RISK_ORDER.index("R3") else "R3"
                summary = f"leaves the project folder ({', '.join(outside[:3])}): {summary}"
            if self.approvals.needs_approval(risk) or outside:
                frozen = json.loads(json.dumps(args, default=str))          # A9.b.03: what is approved is what runs
                digest = args_digest(frozen)
                rehearsal: dict[str, Any] = {"args_sha256": digest}
                if tool.rehearse:
                    try:
                        rehearsal.update(await tool.rehearse(frozen, ctx))
                    except Exception as exc:
                        rehearsal["rehearsal_error"] = f"{type(exc).__name__}: {exc}"
                cost = None
                if risk == "R4":
                    cost = tool.cost(frozen) if tool.cost else None
                    rehearsal["cost_usd"] = cost
                    refusal = await self.budget.check_spend(bot_id=self.bot_id, job_id=job_id, amount=cost) if self.budget else (
                        None if cost is not None else "this action's cost is unknown")
                    if refusal:
                        allowed[idx] = f"DENIED before asking the user: {refusal}. Do not retry it; report back."
                        await ev.emit("console", f"✖ over budget: {summary} ({refusal})")
                        continue
                await ev.set_state("waiting_approval", summary)
                await ev.emit("console", f"⏸ waiting for your approval ({risk}: {RISK_TEXT[risk]}): {summary}")
                with self.waiting_on_user():
                    decision = await self.approvals.request(bot_id=self.bot_id, job_id=job_id, tool=name, risk=risk,
                                                            summary=summary, timeout=self.approval_timeout,
                                                            rehearsal=rehearsal, host=target_host(tool, frozen, ctx))
                if not decision.approved:
                    allowed[idx] = (f"DENIED: the user did not approve this {risk} action ({decision.reason or 'declined'}). "
                                    "Do not retry it; continue another way or report back.")
                    await ev.emit("console", f"✖ not approved: {summary}")
                    continue
                await ev.emit("console", f"✔ approved: {summary}")
                parsed[idx] = (call, name, frozen, tool, summary)
                approved[idx] = (digest, cost, decision.approval_id)
            allowed[idx] = None

        await ev.set_state("tool", ", ".join(p[1] for p in parsed)[:120])
        locks: dict[str, asyncio.Lock] = {}

        async def one(idx: int):
            call, name, args, tool, summary = parsed[idx]
            if allowed[idx] is not None:
                return call, name, allowed[idx]
            key = None
            if tool and tool.path_arg and args.get(tool.path_arg):
                key = str((ctx.workspace / str(args[tool.path_arg])).resolve())
            if idx in approved and args_digest(args) != approved[idx][0]:
                return call, name, "ERROR: the call changed after it was approved; it was not run"
            # structured events for the bot window (props, reactions, activity line)
            target = str(args.get("path") or args.get("url") or args.get("command") or args.get("query") or "")[:120]
            await ev.emit("tool", json.dumps({"phase": "start", "name": name, "target": target}))
            if key:
                lock = locks.setdefault(key, asyncio.Lock())
                async with lock:
                    result = await self.tools.run(name, args, ctx)
            else:
                result = await self.tools.run(name, args, ctx)
            if idx in approved and approved[idx][1] and self.budget and not result.startswith(("ERROR", "DENIED")):
                await self.budget.record_spend(bot_id=self.bot_id, job_id=ctx.job_id, project_id=None, tool=name,
                                               amount=approved[idx][1], description=summary, approval_id=approved[idx][2])
            first = (result.split("\n")[0] if result else "")[:160]
            await ev.emit("console", f"  ↳ {first}")
            await ev.emit("tool", json.dumps({"phase": "end", "name": name, "target": target,
                                              "ok": not str(result).startswith(("ERROR", "DENIED"))}))
            return call, name, result

        return list(await asyncio.gather(*(one(i) for i in range(len(parsed)))))
