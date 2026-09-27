"""Chat sessions (user, 2026-09-26: "a way to clear all messages or start a new session… close the session and
keep it as old opened sessions that we can reopen from the File menu under recent sessions").

One JSON file per session in ~/.omnibots/sessions/: which bot window it belongs to, a title (your first
message), the project folder it worked in, and the chat messages. Each bot window has one CURRENT session
(closed=None); New/Close session closes it (it stays in Recent sessions) and the next message starts a new one.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Session:
    id: str
    bot: str
    title: str = ""
    created: float = 0.0
    updated: float = 0.0
    closed: float | None = None
    folder: str = ""
    messages: list[dict] = field(default_factory=list)      # {"who": "user"|"bot", "text": str, "t": float}

    @property
    def label(self) -> str:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(self.updated or self.created))
        return f"{(self.title or 'Untitled session')[:50]}  —  {when}  ({len(self.messages)} messages)"


class SessionStore:
    def __init__(self, folder: Path):
        self.dir = Path(folder)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, sid: str) -> Path:
        return self.dir / f"{sid}.json"

    def _save(self, s: Session) -> None:
        tmp = self._path(s.id).with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(s), ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._path(s.id))                     # never a half-written session

    def load(self, sid: str) -> Session | None:
        try:
            return Session(**json.loads(self._path(sid).read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return None

    def all(self, bot: str | None = None) -> list[Session]:
        out = []
        for f in self.dir.glob("*.json"):
            s = self.load(f.stem)
            if s and (bot is None or s.bot == bot):
                out.append(s)
        return sorted(out, key=lambda s: s.updated or s.created, reverse=True)

    def current(self, bot: str) -> Session | None:
        return next((s for s in self.all(bot) if s.closed is None), None)

    def recent(self, bot: str, limit: int = 15) -> list[Session]:
        """Closed sessions with messages, newest first (File → Recent sessions)."""
        return [s for s in self.all(bot) if s.closed is not None and s.messages][:limit]

    def append(self, bot: str, who: str, text: str, folder: str = "") -> Session:
        s = self.current(bot)
        now = time.time()
        if s is None:
            s = Session(id=f"ses_{uuid.uuid4().hex[:10]}", bot=bot, created=now)
        if who == "user" and not s.title:
            s.title = " ".join(text.split())[:80]
        if folder:
            s.folder = folder
        s.messages.append({"who": who, "text": text, "t": now})
        s.updated = now
        self._save(s)
        return s

    def set_folder(self, bot: str, folder: str) -> None:
        s = self.current(bot)
        if s is not None and folder and s.folder != folder:
            s.folder = folder
            self._save(s)

    def close(self, bot: str) -> Session | None:
        """Close the current session (kept in Recent sessions); an empty one is simply removed."""
        s = self.current(bot)
        if s is None:
            return None
        if not s.messages:
            self._path(s.id).unlink(missing_ok=True)
            return None
        s.closed = time.time()
        self._save(s)
        return s

    def reopen(self, sid: str) -> Session | None:
        """Make an old session current again (the one that was current gets closed)."""
        s = self.load(sid)
        if s is None:
            return None
        self.close(s.bot)
        s.closed = None
        s.updated = time.time()
        self._save(s)
        return s

    def clear(self, bot: str) -> None:
        """Clear chat: empty the current session's messages (it stays the current session)."""
        s = self.current(bot)
        if s is not None:
            s.messages, s.title, s.updated = [], "", time.time()
            self._save(s)
