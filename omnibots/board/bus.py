"""The message bus (PLAN.md ADR-3, A4.a.01).

publish() writes the message to SQL first (durable, gets its id), then fans it
out to every matching live subscriber's asyncio.Queue. Publishing is
serialized, so ids are the global order and every subscriber sees messages
in id order. A subscriber that was away replays what it missed from SQL
(`since_id`), then continues live, with no gap and no duplicates.
There is no polling anywhere.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Callable

from omnibots.board.types import Message, validate


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Subscription:
    def __init__(self, bus: "MessageBus", topics: set[str] | None, recipient: str | None,
                 types: set[str] | None, predicate: Callable[[Message], bool] | None):
        self.bus = bus
        self.topics = topics or set()
        self.recipient = recipient
        self.types = types
        self.predicate = predicate
        self.queue: asyncio.Queue[Message] = asyncio.Queue()
        self.last_id = 0
        self._replaying = False
        self._held: list[Message] = []

    def matches(self, m: Message) -> bool:
        if self.types and m.message_type not in self.types:
            return False
        hit = (self.recipient is not None and m.recipient_id == self.recipient) or any(
            m.topic == t or (t.endswith("*") and m.topic.startswith(t[:-1])) for t in self.topics)
        if not self.topics and self.recipient is None:
            hit = True                                         # firehose (the UI's board view)
        return hit and (self.predicate is None or self.predicate(m))

    def _deliver(self, m: Message) -> None:
        if self._replaying:
            self._held.append(m)
        elif m.id > self.last_id:
            self.last_id = m.id
            self.queue.put_nowait(m)

    async def get(self, timeout: float | None = None) -> Message | None:
        try:
            return await (asyncio.wait_for(self.queue.get(), timeout) if timeout is not None else self.queue.get())
        except asyncio.TimeoutError:
            return None

    def drain(self) -> list[Message]:
        out = []
        while not self.queue.empty():
            out.append(self.queue.get_nowait())
        return out

    def close(self) -> None:
        self.bus._subs.discard(self)


class MessageBus:
    def __init__(self, db):
        self.db = db
        self._subs: set[Subscription] = set()
        self._lock = asyncio.Lock()

    async def publish(self, topic: str, message_type: str, payload: dict[str, Any] | None = None, *,
                      sender_type: str = "system", sender_id: str | None = None, recipient_id: str | None = None,
                      job_id: str | None = None, project_id: str | None = None) -> Message:
        from omnibots.security.vault import scrub_obj
        payload = scrub_obj(dict(payload or {}))
        validate(message_type, sender_type, payload)
        async with self._lock:
            created = _now()
            mid = await self.db.write(
                "INSERT INTO messages (topic, sender_type, sender_id, recipient_id, job_id, project_id, message_type, payload_json, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (topic, sender_type, sender_id, recipient_id, job_id, project_id, message_type,
                 json.dumps(payload, ensure_ascii=False), created))
            msg = Message(mid, topic, sender_type, sender_id, message_type, payload, recipient_id, job_id, project_id, created)
            for sub in list(self._subs):
                if sub.matches(msg):
                    sub._deliver(msg)
        return msg

    async def subscribe(self, topics: set[str] | list[str] | None = None, *, recipient: str | None = None,
                        types: set[str] | None = None, since_id: int | None = None,
                        predicate: Callable[[Message], bool] | None = None) -> Subscription:
        """since_id=None: live only. since_id=N: first replay every matching
        message with id > N from SQL, then continue live, in order, no gaps."""
        sub = Subscription(self, set(topics or ()), recipient, types, predicate)
        if since_id is None:
            async with self._lock:
                row = await self.db.read_one("SELECT COALESCE(MAX(id), 0) AS m FROM messages")
                sub.last_id = row["m"]
                self._subs.add(sub)
            return sub
        sub._replaying = True
        async with self._lock:
            self._subs.add(sub)                    # live messages are held while we replay
        rows = await self.db.read("SELECT * FROM messages WHERE id > ? ORDER BY id", (since_id,))
        sub.last_id = since_id
        for r in rows:
            m = Message.from_row(r)
            if sub.matches(m) and m.id > sub.last_id:
                sub.last_id = m.id
                sub.queue.put_nowait(m)
            elif m.id > sub.last_id:
                sub.last_id = m.id
        sub._replaying = False
        for m in sub._held:
            if m.id > sub.last_id:
                sub.last_id = m.id
                sub.queue.put_nowait(m)
        sub._held.clear()
        return sub

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)
