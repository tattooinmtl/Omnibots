"""Bot event stream (PLAN.md A3.a.05): console, terminal, thinking, state.

Every event goes to the live listener immediately (bot window, faces). For
the database, streaming kinds (console and thinking tokens) are coalesced into
one row per burst, so a long answer is a handful of rows instead of one per
token. `state` and `terminal` rows are written as they happen.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from omnibots.security.vault import scrub

log = logging.getLogger(__name__)

STREAMING = {"console", "thinking"}
STATES = {"idle", "thinking", "tool", "waiting_approval", "waiting_seat", "blocked", "error", "done", "sleeping", "rate_limited", "stopped"}

Listener = Callable[[dict[str, Any]], Awaitable[None] | None]


class BotEvents:
    def __init__(self, bot_id: str, db=None, listener: Listener | None = None, flush_after: float = 0.4, max_chars: int = 2000):
        self.bot_id = bot_id
        self.db = db
        self.listener = listener
        self.job_id: str | None = None
        self.flush_after = flush_after
        self.max_chars = max_chars
        self._buf_kind: str | None = None
        self._buf: list[str] = []
        self._buf_since = 0.0
        self.state = "idle"

    async def emit(self, kind: str, content: str, *, stream: bool = False) -> None:
        """stream=True: a token-sized piece to append to the current burst."""
        content = scrub(content)
        if kind == "state":
            self.state = content
        if self.listener:
            # A broken display (UI, console) must never take the bot down.
            try:
                r = self.listener({"bot_id": self.bot_id, "job_id": self.job_id, "kind": kind, "content": content, "stream": stream, "ts": time.time()})
                if r is not None and hasattr(r, "__await__"):
                    await r
            except Exception:
                log.exception("bot event listener failed (bot %s); continuing", self.bot_id)
        if not self.db:
            return
        if stream and kind in STREAMING:
            if self._buf_kind not in (None, kind):
                await self.flush()
            if not self._buf:
                self._buf_since = time.monotonic()
            self._buf_kind = kind
            self._buf.append(content)
            if sum(map(len, self._buf)) >= self.max_chars or time.monotonic() - self._buf_since >= self.flush_after:
                await self.flush()
            return
        await self.flush()
        await self._write(kind, content)

    async def flush(self) -> None:
        if self._buf and self._buf_kind:
            text, kind = "".join(self._buf), self._buf_kind
            self._buf, self._buf_kind = [], None
            await self._write(kind, text)

    async def _write(self, kind: str, content: str) -> None:
        content = scrub(content)            # a secret split across streamed chunks is caught here
        await self.db.write("INSERT INTO bot_events (bot_id, job_id, kind, content) VALUES (?,?,?,?)",
                            (self.bot_id, self.job_id, kind, content))

    async def set_state(self, state: str, detail: str = "") -> None:
        await self.emit("state", state if not detail else f"{state}: {detail}")


class ToolTextFilter:
    """Streams answer text but holds back tool-call syntax (text protocol):
    everything from a `<tool_call` / `<function=` opener onward is suppressed."""

    MARKERS = ("<tool_call", "<function=", "<function =")

    def __init__(self) -> None:
        self.buf = ""
        self.cut = False

    def feed(self, text: str) -> str:
        if self.cut:
            return ""
        self.buf += text
        for m in self.MARKERS:
            i = self.buf.find(m)
            if i != -1:
                out, self.buf, self.cut = self.buf[:i], "", True
                return out
        keep = max(len(m) for m in self.MARKERS) - 1
        lt = self.buf.rfind("<", max(0, len(self.buf) - keep))
        if lt == -1:
            out, self.buf = self.buf, ""
        else:
            out, self.buf = self.buf[:lt], self.buf[lt:]
        return out

    def flush(self) -> str:
        out, self.buf = ("" if self.cut else self.buf), ""
        return out
