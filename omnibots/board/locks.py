"""Leases on files and resources (PLAN.md §4.3, A4.a.04), so two bots never
edit the same thing at once.

A lease has a TTL: a bot that dies can't block others forever. Acquire is
atomic (one conditional upsert through the single DB writer). Waiters sleep
until a LOCK_RELEASED for that resource arrives on the board, or the current
lease expires, whichever comes first, so there's no polling loop.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from omnibots.board.bus import MessageBus
from omnibots.board.types import GENERAL


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


class LeaseManager:
    def __init__(self, db, bus: MessageBus, *, clock=time.time):
        self.db = db
        self.bus = bus
        self.clock = clock

    async def clear_all(self) -> None:
        """At startup: leases from a previous session are meaningless."""
        await self.db.write("DELETE FROM locks")

    async def try_acquire(self, resource: str, bot_id: str, ttl: float = 300) -> bool:
        now = self.clock()
        await self.db.write(
            "INSERT INTO locks (resource, holder_bot_id, expires_at) VALUES (?,?,?) "
            "ON CONFLICT(resource) DO UPDATE SET holder_bot_id=excluded.holder_bot_id, expires_at=excluded.expires_at "
            "WHERE locks.expires_at < ? OR locks.holder_bot_id = excluded.holder_bot_id",
            (resource, bot_id, _iso(now + ttl), _iso(now)))
        row = await self.db.read_one("SELECT holder_bot_id FROM locks WHERE resource=?", (resource,))
        got = bool(row) and row["holder_bot_id"] == bot_id
        if got:
            await self.bus.publish(GENERAL, "LOCK_ACQUIRED", {"resource": resource, "ttl_s": ttl},
                                   sender_type="bot", sender_id=bot_id)
        return got

    async def acquire(self, resource: str, bot_id: str, ttl: float = 300, timeout: float = 600) -> bool:
        deadline = time.monotonic() + timeout
        sub = await self.bus.subscribe({GENERAL}, types={"LOCK_RELEASED"},
                                       predicate=lambda m: m.payload.get("resource") == resource)
        try:
            while True:
                if await self.try_acquire(resource, bot_id, ttl):
                    return True
                row = await self.db.read_one("SELECT expires_at FROM locks WHERE resource=?", (resource,))
                expires_in = max(0.05, _ts(row["expires_at"]) - self.clock()) if row else 0.05
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                await sub.get(timeout=min(expires_in, remaining))
        finally:
            sub.close()

    async def release(self, resource: str, bot_id: str) -> bool:
        row = await self.db.read_one("SELECT holder_bot_id FROM locks WHERE resource=?", (resource,))
        if not row or row["holder_bot_id"] != bot_id:
            return False
        await self.db.write("DELETE FROM locks WHERE resource=? AND holder_bot_id=?", (resource, bot_id))
        await self.bus.publish(GENERAL, "LOCK_RELEASED", {"resource": resource}, sender_type="bot", sender_id=bot_id)
        return True

    async def holder(self, resource: str) -> str | None:
        row = await self.db.read_one("SELECT holder_bot_id, expires_at FROM locks WHERE resource=?", (resource,))
        if not row or _ts(row["expires_at"]) < self.clock():
            return None
        return row["holder_bot_id"]
