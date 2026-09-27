"""Routines, triggers and the night shift (PLAN.md A6.b, §4.5).

- Routines: cron schedules ("0 8 * * 1" = Mondays 08:00) that start a goal.
- Triggers: start a goal when something happens:
    file       a file or folder changes (size/mtime)
    board      a board message matches (type, topic, optional text)
    threshold  a measured value crosses a limit (disk free, MiniMax budget used)
  `webhook` and `connector` triggers need inbound HTTP / app connectors and
  come with A10 (a local listener conflicts with ADR-2 and needs the user's OK).
- Night shift: low-priority goals queue up and run on the cheap lane ONLY
  while the user is away from the PC (no keyboard/mouse input for N minutes),
  and stop taking new work when they come back.
All schedules persist in SQL (`routines`, `triggers`).
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import shutil
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

Fire = Callable[[str, dict[str, Any]], Awaitable[Any]]   # (goal, info) -> starts the work


# ── cron ──────────────────────────────────────────────────────────────────
class CronError(ValueError):
    pass


_FIELDS = [("minute", 0, 59), ("hour", 0, 23), ("day", 1, 31), ("month", 1, 12), ("weekday", 0, 6)]
_ALIASES = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@weekly": "0 0 * * 0", "@monthly": "0 0 1 * *"}


def _field(spec: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s)
            if step < 1:
                raise CronError("step must be >= 1")
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
        else:
            a = b = int(part)
        if a < lo or b > hi or a > b:
            raise CronError(f"{part!r} is outside {lo}-{hi}")
        out.update(range(a, b + 1, step))
    return out


class Cron:
    """Standard 5-field cron: minute hour day-of-month month day-of-week (0=Sunday; 7 also = Sunday)."""

    def __init__(self, expr: str):
        expr = _ALIASES.get(expr.strip(), expr.strip())
        parts = expr.split()
        if len(parts) != 5:
            raise CronError("a schedule needs 5 fields: minute hour day month weekday")
        try:
            sets = [_field(p.replace("7", "0") if i == 4 and p == "7" else p, lo, hi if i != 4 else 7)
                    for i, (p, (_, lo, hi)) in enumerate(zip(parts, _FIELDS))]
        except ValueError as exc:
            raise CronError(str(exc)) from exc
        sets[4] = {0 if d == 7 else d for d in sets[4]}
        self.minute, self.hour, self.day, self.month, self.weekday = sets
        self.dom_any, self.dow_any = parts[2] == "*", parts[4] == "*"
        self.expr = expr

    def matches(self, t: datetime) -> bool:
        dow = (t.weekday() + 1) % 7                            # Python Mon=0 -> cron Sun=0
        day_ok = (t.day in self.day) if not self.dom_any and self.dow_any else \
                 (dow in self.weekday) if self.dom_any and not self.dow_any else \
                 (t.day in self.day or dow in self.weekday) if not self.dom_any else True
        return t.minute in self.minute and t.hour in self.hour and t.month in self.month and day_ok

    def next_after(self, t: datetime) -> datetime:
        t = t.replace(second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(60 * 24 * 366 * 5):                    # at most ~5 years ahead
            if t.month not in self.month:
                t = (t.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(day=1)
                continue
            if self.matches(t):
                return t
            if t.hour not in self.hour:
                t = t.replace(minute=0) + timedelta(hours=1)
                continue
            t += timedelta(minutes=1)
        raise CronError(f"{self.expr!r} never fires")


# ── routines ──────────────────────────────────────────────────────────────
class Routines:
    def __init__(self, db, fire: Fire, *, clock: Callable[[], datetime] = datetime.now, sleep=asyncio.sleep):
        self.db, self.fire, self.clock, self.sleep = db, fire, clock, sleep

    async def add(self, name: str, goal: str, schedule: str) -> str:
        cron = Cron(schedule)                                  # validates
        rid = f"rt_{uuid.uuid4().hex[:8]}"
        nxt = cron.next_after(self.clock())
        await self.db.write("INSERT INTO routines (id, name, goal, schedule, next_run_at) VALUES (?,?,?,?,?)",
                            (rid, name, goal, cron.expr, nxt.isoformat(timespec="minutes")))
        return rid

    async def list(self) -> list[dict[str, Any]]:
        return [dict(r) for r in await self.db.read("SELECT * FROM routines ORDER BY next_run_at")]

    async def set_enabled(self, rid: str, enabled: bool) -> None:
        await self.db.write("UPDATE routines SET enabled=? WHERE id=?", (int(enabled), rid))

    async def tick(self) -> list[str]:
        """Fire every enabled routine that is due. Returns the ids fired."""
        now = self.clock()
        fired = []
        for r in await self.db.read("SELECT * FROM routines WHERE enabled=1"):
            due = datetime.fromisoformat(r["next_run_at"]) if r["next_run_at"] else now
            if due > now:
                continue
            nxt = Cron(r["schedule"]).next_after(now)
            await self.db.write("UPDATE routines SET last_run_at=?, next_run_at=? WHERE id=?",
                                (now.isoformat(timespec="minutes"), nxt.isoformat(timespec="minutes"), r["id"]))
            try:
                await self.fire(r["goal"], {"routine": r["id"], "name": r["name"]})
                fired.append(r["id"])
            except Exception:
                log.exception("routine %s failed to start", r["id"])
        return fired

    async def loop(self) -> None:
        while True:
            await self.tick()
            rows = await self.db.read("SELECT next_run_at FROM routines WHERE enabled=1 AND next_run_at IS NOT NULL")
            now = self.clock()
            waits = [(datetime.fromisoformat(r["next_run_at"]) - now).total_seconds() for r in rows]
            await self.sleep(min([60.0, *[max(1.0, w) for w in waits]]))


# ── triggers ──────────────────────────────────────────────────────────────
def disk_free_gb(path: str = "C:\\") -> float:
    return shutil.disk_usage(path).free / 1e9


class Triggers:
    KINDS = {"file", "board", "threshold"}

    def __init__(self, db, fire: Fire, *, bus=None, metrics: dict[str, Callable[[], float]] | None = None,
                 sleep=asyncio.sleep, poll_seconds: float = 5.0, cooldown_seconds: float = 300.0):
        self.db, self.fire, self.bus, self.sleep = db, fire, bus, sleep
        self.metrics = {"disk_free_gb": disk_free_gb, **(metrics or {})}
        self.poll, self.cooldown = poll_seconds, cooldown_seconds
        self._stamps: dict[str, Any] = {}
        self._last_fire: dict[str, float] = {}
        self.board_ready = asyncio.Event()          # set once board_loop listens (messages before that aren't seen)

    async def add(self, name: str, kind: str, config: dict[str, Any], goal: str) -> str:
        if kind not in self.KINDS:
            raise ValueError(f"trigger kind must be one of {sorted(self.KINDS)} (webhook/connector come with A10)")
        if kind == "file" and not config.get("path"):
            raise ValueError("a file trigger needs 'path'")
        if kind == "board" and not config.get("type"):
            raise ValueError("a board trigger needs the message 'type'")
        if kind == "threshold" and (config.get("metric") not in self.metrics or "below" not in config and "above" not in config):
            raise ValueError(f"a threshold trigger needs a 'metric' from {sorted(self.metrics)} and 'below' or 'above'")
        tid = f"tg_{uuid.uuid4().hex[:8]}"
        await self.db.write("INSERT INTO triggers (id, name, kind, config_json, goal) VALUES (?,?,?,?,?)",
                            (tid, name, kind, json.dumps(config), goal))
        return tid

    async def _all(self, kind: str) -> list[dict[str, Any]]:
        return [dict(r) | {"config": json.loads(r["config_json"])}
                for r in await self.db.read("SELECT * FROM triggers WHERE enabled=1 AND kind=?", (kind,))]

    async def _fire(self, t: dict[str, Any], detail: str) -> None:
        now = time.monotonic()
        if now - self._last_fire.get(t["id"], -1e9) < self.cooldown:
            return                                              # don't start the same goal over and over
        self._last_fire[t["id"]] = now
        await self.db.write("UPDATE triggers SET last_fired_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (t["id"],))
        await self.fire(t["goal"], {"trigger": t["id"], "name": t["name"], "detail": detail})

    async def check_files_and_thresholds(self) -> None:
        for t in await self._all("file"):
            p = Path(t["config"]["path"])
            try:
                st = p.stat()
                stamp = (st.st_size, st.st_mtime_ns) if p.is_file() else tuple(sorted((c.name, c.stat().st_mtime_ns) for c in p.iterdir()))
            except OSError:
                stamp = None
            if t["id"] in self._stamps and self._stamps[t["id"]] != stamp:
                await self._fire(t, f"{p} changed")
            self._stamps[t["id"]] = stamp
        for t in await self._all("threshold"):
            c = t["config"]
            value = self.metrics[c["metric"]]()
            crossed = ("below" in c and value < float(c["below"])) or ("above" in c and value > float(c["above"]))
            was = self._stamps.get(t["id"], False)
            if crossed and not was:                             # fire on the crossing, not while it stays crossed
                await self._fire(t, f"{c['metric']} = {value:.2f}")
            self._stamps[t["id"]] = crossed

    async def board_loop(self) -> None:
        sub = await self.bus.subscribe()                        # the firehose; filtered per trigger below
        self.board_ready.set()
        try:
            while True:
                m = await sub.get()
                for t in await self._all("board"):
                    c = t["config"]
                    if m.message_type != c["type"] or (c.get("topic") and m.topic != c["topic"]):
                        continue
                    if c.get("contains") and c["contains"].lower() not in m.text().lower():
                        continue
                    await self._fire(t, f"{m.message_type} #{m.id}")
        finally:
            sub.close()

    async def poll_loop(self) -> None:
        while True:
            await self.check_files_and_thresholds()
            await self.sleep(self.poll)


# ── night shift ───────────────────────────────────────────────────────────
def idle_seconds() -> float:
    """Seconds since the user last touched the keyboard or mouse (Windows)."""
    if sys.platform != "win32":
        return 0.0

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
    info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    return max(0.0, (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000.0)


class NightShift:
    def __init__(self, run: Callable[[dict[str, Any]], Awaitable[Any]], *, idle_after: float = 15 * 60,
                 idle: Callable[[], float] = idle_seconds, sleep=asyncio.sleep, check_every: float = 60.0,
                 held: Callable[[], bool] = lambda: False):
        self.run, self.idle_after, self.idle, self.sleep, self.check_every = run, idle_after, idle, sleep, check_every
        self.held = held                                        # A15.b.02: "Pause background work" keeps the queue waiting
        self.queue: list[dict[str, Any]] = []
        self.running = False

    def enqueue(self, goal: str, **info: Any) -> None:
        self.queue.append({"goal": goal, **info})

    @property
    def user_away(self) -> bool:
        return self.idle() >= self.idle_after

    async def step(self) -> bool:
        """Run the next queued item if the user is away. Returns True if it ran one."""
        if self.running or not self.queue or not self.user_away or self.held():
            return False
        item = self.queue.pop(0)
        self.running = True
        try:
            await self.run(item)
        finally:
            self.running = False
        return True

    async def loop(self) -> None:
        while True:
            if not await self.step():
                await self.sleep(self.check_every)
