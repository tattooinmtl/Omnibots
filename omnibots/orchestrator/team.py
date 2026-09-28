"""Team controls and stall monitoring (PLAN.md A7.c.01, A7.a.05).

Controls cascade from Omi to every bot:
  pause    every running bot stops at its next step boundary; in-flight calls
           finish; MiniMax seats are handed back; jobs show `paused`
  resume   everyone continues where they stopped
  stop     every running job is cancelled now (sandboxed processes killed);
           jobs become `stopped` (A15.d.01: nothing restarts them on its own; work cut off by a
           crash or a quit is `interrupted`, and that carries on by itself)
  start    goals with stopped or interrupted work are picked up again by Omi
  restart  stop, then start (Omi reloads its memory, projects, interrupted jobs)
  panic    like stop, plus: every pending approval is denied and everyone
           waiting for a seat is sent away; audited as a panic
Per-bot versions affect one bot; Omi is told on the board.

Stall monitor: a working bot with no events for `stall_after` seconds is
nudged; at 2× Omi is told; at 3× the user is asked.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from omnibots.board.types import GENERAL, topic_bot
from omnibots.bots.profile import BOSS_ID

ACTIVE_JOB = ("assigned", "running", "review", "waiting_approval")


class TeamController:
    def __init__(self, *, db, bus, runner, graph, orchestrator, approvals, seats, stall_after: float = 300.0,
                 clock=time.monotonic):
        self.db, self.bus, self.runner, self.graph, self.orch = db, bus, runner, graph, orchestrator
        self.approvals, self.seats = approvals, seats
        self.stall_after, self.clock = stall_after, clock
        self.paused = False
        self._stage: dict[str, int] = {}

    async def _say(self, text: str, actor: str = "user") -> None:
        await self.bus.publish(GENERAL, "PROGRESS_UPDATE", {"text": text}, sender_type="user" if actor == "user" else "system", sender_id=actor)
        await self.db.audit("user" if actor == "user" else "system", actor, "team_control", json.dumps({"text": text}))

    async def _set_jobs(self, from_states: tuple[str, ...], to: str, bot_id: str | None = None) -> int:
        where = f"status IN ({','.join('?' * len(from_states))})"
        args: list[Any] = [*from_states]
        if bot_id:
            where += " AND assigned_bot_id=?"
            args.append(bot_id)
        # count first: reads use their own connections, where SELECT changes() is always 0
        row = await self.db.read_one(f"SELECT COUNT(*) AS n FROM jobs WHERE {where}", args)
        await self.db.write(f"UPDATE jobs SET status=? WHERE {where}", [to, *args])
        return row["n"] if row else 0

    # ── the whole team ─────────────────────────────────────────────────
    async def pause(self) -> dict[str, Any]:
        self.paused = True
        bots = list(self.runner.active)
        for agent in self.runner.active.values():
            agent.pause()
        await self._set_jobs(("running",), "paused")
        await self._say(f"team paused ({len(bots)} bot(s))")
        return {"paused": bots}

    async def resume(self) -> dict[str, Any]:
        self.paused = False
        bots = list(self.runner.active)
        for agent in self.runner.active.values():
            agent.resume()
        await self._set_jobs(("paused",), "running")
        await self._say(f"team resumed ({len(bots)} bot(s))")
        return {"resumed": bots}

    async def stop(self, *, panic: bool = False) -> dict[str, Any]:
        # Remember exactly which jobs were live, so only those become `interrupted`.
        live_ids = [r["id"] for r in await self.db.read(
            f"SELECT id FROM jobs WHERE status IN ({','.join('?' * (len(ACTIVE_JOB) + 1))})", (*ACTIVE_JOB, "paused"))]
        denied, sent_away = 0, 0
        if panic:                    # first: nothing waiting for the user may go through (recorded as denied)
            for info in list(self.approvals.list_pending()):
                await self.approvals.decide(info["id"], False, "panic stop")
                denied += 1
            for q in list(self.seats.queue()):
                sent_away += self.seats.leave_queue(q["bot_id"])
        stopped = [b for b in list(self.runner.tasks) if self.runner.cancel(b)]
        for ctx in self.orch.goals.values():
            for t in ctx.worker_tasks.values():
                t.cancel()
        for t in self.orch.boss_tasks.values():
            t.cancel()
        waiting = [t for t in (*self.orch.boss_tasks.values(), *[w for c in self.orch.goals.values() for w in c.worker_tasks.values()]) if not t.done()]
        if waiting:
            await asyncio.wait(waiting, timeout=15)
        await asyncio.sleep(0)
        if live_ids:
            await self.db.write(f"UPDATE jobs SET status='stopped' WHERE id IN ({','.join('?' * len(live_ids))})", live_ids)
        self.paused = False
        await self._say(("PANIC STOP" if panic else "team stopped") + f": {len(stopped)} job(s) stopped"
                        + (f", {denied} approval(s) denied, {sent_away} left the seat line" if panic else ""))
        return {"stopped": stopped, "approvals_denied": denied, "left_seat_line": sent_away}

    async def start(self) -> dict[str, Any]:
        """Pick up every goal that has interrupted work."""
        rows = await self.db.read("SELECT DISTINCT project_id FROM jobs WHERE status IN ('interrupted','stopped') AND project_id IS NOT NULL")
        resumed = []
        for r in rows:
            pid = r["project_id"]
            for j in await self.graph.jobs(pid):
                if j.status in ("interrupted", "stopped"):
                    await self.graph.resume(j.id)
            goal = (await self.db.read_one("SELECT goal FROM projects WHERE id=?", (pid,)))["goal"]
            self.orch.boss_tasks[pid] = asyncio.create_task(self.orch.run_goal(goal, project_id=pid, resume=True), name=f"goal-{pid}")
            resumed.append(pid)
        await self._say(f"team started: resuming {len(resumed)} goal(s)")
        return {"resumed_projects": resumed}

    async def restart(self) -> dict[str, Any]:
        s = await self.stop()
        st = await self.start()
        return {**s, **st}

    # ── one bot ────────────────────────────────────────────────────────
    async def pause_bot(self, bot_id: str) -> bool:
        agent = self.runner.active.get(bot_id)
        if not agent:
            return False
        agent.pause()
        await self._set_jobs(("running",), "paused", bot_id)
        await self._tell_boss(f"the user paused {bot_id}")
        return True

    async def resume_bot(self, bot_id: str) -> bool:
        agent = self.runner.active.get(bot_id)
        if not agent:
            return False
        agent.resume()
        await self._set_jobs(("paused",), "running", bot_id)
        await self._tell_boss(f"the user resumed {bot_id}")
        return True

    async def stop_bot(self, bot_id: str) -> bool:
        live_ids = [r["id"] for r in await self.db.read(
            f"SELECT id FROM jobs WHERE assigned_bot_id=? AND status IN ({','.join('?' * (len(ACTIVE_JOB) + 1))})", (bot_id, *ACTIVE_JOB, "paused"))]
        task = self.runner.tasks.get(bot_id)
        if not self.runner.cancel(bot_id):
            return False
        if task:
            await asyncio.wait([task], timeout=15)
        if live_ids:
            await self.db.write(f"UPDATE jobs SET status='stopped' WHERE id IN ({','.join('?' * len(live_ids))})", live_ids)
        await self._tell_boss(f"the user stopped {bot_id}; its job is stopped (reassign it, or the user resumes it with Start)")
        return True

    async def _tell_boss(self, text: str) -> None:
        await self.bus.publish(topic_bot(BOSS_ID), "A2A_MESSAGE", {"text": text}, sender_type="user", sender_id="user", recipient_id=BOSS_ID)

    # ── stall monitor ──────────────────────────────────────────────────
    async def check_stalls(self) -> list[tuple[str, int]]:
        acted = []
        now = self.clock()
        for bot_id, agent in list(self.runner.active.items()):
            if bot_id == BOSS_ID or agent.paused:
                continue
            quiet = now - self.runner.last_activity.get(bot_id, now)
            stage = int(quiet // self.stall_after)
            if stage <= self._stage.get(bot_id, 0):
                if stage == 0:
                    self._stage.pop(bot_id, None)
                continue
            self._stage[bot_id] = stage
            mins = quiet / 60
            if stage == 1:
                self.runner.steer(bot_id, f"Status check from Omi: no activity for {mins:.0f} min. If you're stuck, say what's blocking you; otherwise continue.")
            elif stage == 2:
                await self.bus.publish(topic_bot(BOSS_ID), "A2A_MESSAGE", {"text": f"{bot_id} has been silent for {mins:.0f} min on its job; consider reassigning it."},
                                       sender_type="system", recipient_id=BOSS_ID)
            elif stage == 3:
                await self.bus.publish(GENERAL, "QUESTION", {"text": f"{bot_id} has made no progress for {mins:.0f} min. Stop it, or let it continue?"},
                                       sender_type="system", recipient_id="user")
            acted.append((bot_id, stage))
        return acted

    async def monitor(self, every: float = 30.0) -> None:
        while True:
            await asyncio.sleep(every)
            try:
                await self.check_stalls()
            except Exception:
                logging.getLogger(__name__).exception("stall check failed")
