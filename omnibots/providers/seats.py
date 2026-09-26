"""The MiniMax seat scheduler (PLAN.md ADR-11, A2.b.06).

The minimax.io key allows at most 4 concurrent sessions.
The WAITING LIST (A4.b.01): a bot booked for a job takes a seat lease for the
whole job (`acquire`); when no seat is free it waits in line, and
`queue()` reports each waiter's position and estimated wait. Every change in
the line fires a `seat_queue` event, so the board and tray stay current. Seats are that
limit: at most 4 MiniMax calls run at once, no matter how many bot
identities exist. Seat 1 is pinned to the boss (Omi); seats 2–4 are lent to
whoever needs strong reasoning right now, by priority then arrival order.
A bot holds at most one seat (acquiring again re-enters the same seat).
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from omnibots.lineup import MINIMAX_SEATS

# Lower number = served first.
PRIORITY = {"boss": 0, "council": 1, "review": 2, "work": 3}


@dataclass
class Seat:
    number: int
    holder: str | None = None
    since: float | None = None
    depth: int = 0                      # re-entrant holds by the same bot


class SeatScheduler:
    def __init__(self, seats: int = MINIMAX_SEATS, boss_id: str | None = None,
                 on_event: Callable[[str, dict[str, Any]], Awaitable[None] | None] | None = None):
        self.seats = [Seat(i + 1) for i in range(seats)]
        self.boss_id = boss_id
        self.on_event = on_event
        self._waiters: list[tuple[int, int, str, asyncio.Future]] = []
        self._seq = itertools.count()
        self._freed = asyncio.Event()
        self._holds: list[float] = []            # recent lease durations (for the wait estimate)

    # ── queries ────────────────────────────────────────────────────────
    def _eligible(self, bot_id: str) -> list[Seat]:
        if bot_id == self.boss_id:
            return self.seats[:1]
        return self.seats[1:] if self.boss_id else self.seats

    def held_by(self, bot_id: str) -> Seat | None:
        return next((s for s in self.seats if s.holder == bot_id), None)

    def free_count(self, bot_id: str | None = None) -> int:
        pool = self._eligible(bot_id) if bot_id else self.seats
        return sum(1 for s in pool if s.holder is None)

    def snapshot(self) -> list[dict[str, Any]]:
        now = time.time()
        return [{"seat": s.number, "holder": s.holder, "pinned": "boss" if (s.number == 1 and self.boss_id) else None,
                 "held_s": round(now - s.since, 1) if s.since else None} for s in self.seats] + \
               [{"waiting": [q["bot_id"] for q in self.queue()]}]

    def queue(self) -> list[dict[str, Any]]:
        """The waiting list in serving order, with an estimated wait each."""
        avg = (sum(self._holds) / len(self._holds)) if self._holds else None
        now = time.time()
        busy = sorted((now - s.since) for s in self.seats[1 if self.boss_id else 0:] if s.since)
        pool = max(1, len(self.seats) - (1 if self.boss_id else 0))
        out = []
        workers = 0
        for prio, _, bot_id, fut in sorted(self._waiters):
            if fut.done():
                continue
            if bot_id == self.boss_id:
                pos, eta = 0, None
            else:
                workers += 1
                pos = workers
                eta = None
                if avg is not None:
                    # Seats free roughly every avg/pool seconds; the next frees when the oldest lease ends.
                    first = max(0.0, avg - (busy[-1] if busy else 0.0))
                    eta = round(first + (pos - 1) * avg / pool, 1)
            out.append({"bot_id": bot_id, "position": pos, "priority": prio, "eta_s": eta})
        return out

    def leave_queue(self, bot_id: str) -> bool:
        """Pause/stop: take a bot out of the line (its acquire() raises CancelledError)."""
        for entry in list(self._waiters):
            if entry[2] == bot_id and not entry[3].done():
                entry[3].cancel()
                return True
        return False

    # ── acquire / release ──────────────────────────────────────────────
    def try_acquire(self, bot_id: str) -> Seat | None:
        seat = self.held_by(bot_id)
        if seat:
            seat.depth += 1
            return seat
        # Don't jump the queue: only take a seat nobody eligible is waiting for.
        if any(w[2] != bot_id for w in self._waiters) and bot_id != self.boss_id:
            return None
        free = next((s for s in self._eligible(bot_id) if s.holder is None), None)
        if free:
            self._grant(free, bot_id)
        return free

    async def acquire(self, bot_id: str, priority: str | int = "work", timeout: float | None = None) -> Seat:
        seat = self.held_by(bot_id)
        if seat:
            seat.depth += 1
            return seat
        free = next((s for s in self._eligible(bot_id) if s.holder is None), None)
        if free and not self._waiters_ahead(bot_id):
            self._grant(free, bot_id)
            return free
        prio = PRIORITY.get(priority, 3) if isinstance(priority, str) else int(priority)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        entry = (prio, next(self._seq), bot_id, fut)
        heapq.heappush(self._waiters, entry)
        await self._emit("seat_waiting", {"bot_id": bot_id, "priority": prio, "position": self._position(bot_id)})
        self._fire("seat_queue", {"queue": self.queue()})
        try:
            return await asyncio.wait_for(fut, timeout) if timeout else await fut
        except BaseException:
            if entry in self._waiters:
                self._waiters.remove(entry)
                heapq.heapify(self._waiters)
                self._fire("seat_queue", {"queue": self.queue()})
            if fut.done() and not fut.cancelled() and fut.exception() is None:
                self.release(fut.result())              # granted just as we gave up
            raise

    def _position(self, bot_id: str) -> int:
        return next((q["position"] for q in self.queue() if q["bot_id"] == bot_id), 0)

    def _waiters_ahead(self, bot_id: str) -> bool:
        if bot_id == self.boss_id:
            return False
        return any(w[2] != self.boss_id for w in self._waiters)

    def release(self, seat: Seat) -> None:
        if seat.holder is None:
            return
        seat.depth -= 1
        if seat.depth > 0:
            return
        holder = seat.holder
        if seat.since and holder != self.boss_id:
            self._holds = (self._holds + [time.time() - seat.since])[-20:]
        seat.holder, seat.since, seat.depth = None, None, 0
        self._fire("seat_released", {"seat": seat.number, "bot_id": holder})
        self._hand_off(seat)
        self._freed.set()
        self._freed = asyncio.Event()

    def _hand_off(self, seat: Seat) -> None:
        # Give the freed seat to the best waiter who is eligible for it.
        for entry in sorted(self._waiters):
            _, _, bot_id, fut = entry
            if fut.done():
                self._waiters.remove(entry)
                continue
            if seat in self._eligible(bot_id):
                self._waiters.remove(entry)
                heapq.heapify(self._waiters)
                self._grant(seat, bot_id)
                fut.set_result(seat)
                self._fire("seat_queue", {"queue": self.queue()})
                return

    def _grant(self, seat: Seat, bot_id: str) -> None:
        seat.holder, seat.since, seat.depth = bot_id, time.time(), 1
        self._fire("seat_granted", {"seat": seat.number, "bot_id": bot_id})

    async def wait_for_release(self, timeout: float | None = None) -> bool:
        ev = self._freed
        try:
            await asyncio.wait_for(ev.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    @asynccontextmanager
    async def seat(self, bot_id: str, priority: str | int = "work", timeout: float | None = None):
        s = await self.acquire(bot_id, priority, timeout)
        try:
            yield s
        finally:
            self.release(s)

    # ── events ─────────────────────────────────────────────────────────
    def _fire(self, kind: str, data: dict[str, Any]) -> None:
        if self.on_event:
            r = self.on_event(kind, data)
            if r is not None and hasattr(r, "__await__"):
                asyncio.ensure_future(r)

    async def _emit(self, kind: str, data: dict[str, Any]) -> None:
        if self.on_event:
            r = self.on_event(kind, data)
            if r is not None and hasattr(r, "__await__"):
                await r
