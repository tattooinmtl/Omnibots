"""SQLite access (ADR-6): WAL mode, ONE writer, numbered migrations.

- All writes go through a queue drained by a single writer, which owns the
  only write connection and runs on its own thread. Each drained batch is one
  transaction, so there is no "database is locked" and a crash loses at most
  the batch that was in flight.
- Reads use short-lived connections on a small read pool; WAL lets them run
  alongside the writer.
- Migrations: omnibots/db/migrations/NNN_name.sql, applied in order, each in
  its own transaction, tracked with PRAGMA user_version.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_RE = re.compile(r"^(\d{3})_[\w\-]+\.sql$")


def open_connection(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, isolation_level=None, check_same_thread=False)
    else:
        conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def list_migrations(directory: Path = MIGRATIONS_DIR) -> list[tuple[int, Path]]:
    found = []
    for p in directory.iterdir():
        m = _MIGRATION_RE.match(p.name)
        if m:
            found.append((int(m.group(1)), p))
    found.sort()
    numbers = [n for n, _ in found]
    if numbers != list(range(1, len(numbers) + 1)):
        raise RuntimeError(f"migrations must be numbered 001.. with no gaps, found {numbers}")
    return found


def apply_migrations(conn: sqlite3.Connection, directory: Path = MIGRATIONS_DIR) -> int:
    """Apply every migration newer than PRAGMA user_version. Returns the new version."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    migrations = list_migrations(directory)
    if current > len(migrations):
        raise RuntimeError(
            f"database is at schema version {current}, newer than this app knows ({len(migrations)}); "
            "update OmniBots instead of opening this database with an older version"
        )
    for number, path in migrations:
        if number <= current:
            continue
        sql = path.read_text(encoding="utf-8")
        log.info("applying migration %s", path.name)
        try:
            conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {number};\nCOMMIT;")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        current = number
    return current


@dataclass
class _Write:
    sql: str
    params: Sequence[Any] | dict[str, Any] = ()
    many: Iterable[Sequence[Any]] | None = None
    future: asyncio.Future | None = field(default=None, repr=False)


class Database:
    """Async facade used inside the engine loop."""

    BATCH_MAX = 500

    def __init__(self, path: Path, *, backup_home: Path | None = None, backup_keep: int = 7):
        self.path = path
        self.backup_home, self.backup_keep = backup_home, backup_keep   # A16.c: None = no backups (tests)
        self._write_exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="db-writer")
        self._read_exec = ThreadPoolExecutor(max_workers=4, thread_name_prefix="db-reader")
        self._conn: sqlite3.Connection | None = None
        self._queue: asyncio.Queue[_Write | None] | None = None
        self._writer_task: asyncio.Task | None = None
        self.schema_version = 0

    # ── lifecycle ──────────────────────────────────────────────────────
    async def open(self) -> None:
        loop = asyncio.get_running_loop()
        self.path.parent.mkdir(parents=True, exist_ok=True)

        def _open() -> int:
            self._conn = open_connection(self.path)
            self._backup_before_migrating()
            return apply_migrations(self._conn)

        self.schema_version = await loop.run_in_executor(self._write_exec, _open)
        self._queue = asyncio.Queue()
        self._writer_task = asyncio.create_task(self._writer(), name="db-writer")
        log.info("database open at %s (schema v%d)", self.path, self.schema_version)

    async def close(self) -> None:
        """Flush every queued write, then close. Safe to call twice."""
        if self._queue is None:
            return
        await self._queue.put(None)
        if self._writer_task:
            await self._writer_task
        loop = asyncio.get_running_loop()
        if self._conn is not None:
            conn, self._conn = self._conn, None
            await loop.run_in_executor(self._write_exec, conn.close)
        self._queue = None
        self._write_exec.shutdown(wait=True)
        self._read_exec.shutdown(wait=True)
        log.info("database closed")

    def _backup_before_migrating(self) -> None:
        """A16.c.02: a database that's about to change shape is copied first (a failed migration
        then leaves nothing lost)."""
        if self.backup_home is None or self._conn is None:
            return
        current = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if 0 < current < len(list_migrations()):
            from omnibots import backup
            dest = backup.new_backup_folder(self.backup_home, "before-migration")
            backup.copy_db(self._conn, dest / backup.DB_REL)
            backup.finish_backup(self.backup_home, dest, "before-migration", self.backup_keep)
            log.info("schema v%d → v%d: backed up first to %s", current, len(list_migrations()), dest)

    # ── backup and housekeeping (A16.c) ────────────────────────────────
    async def backup_to(self, home: Path, reason: str, keep: int = 7) -> Path:
        """A consistent copy of the live database plus the files around it. Runs on the writer
        thread, so it sits between writes instead of racing them."""
        from omnibots import backup

        def _go() -> Path:
            assert self._conn is not None
            dest = backup.new_backup_folder(home, reason)
            backup.copy_db(self._conn, dest / backup.DB_REL)
            return backup.finish_backup(home, dest, reason, keep)
        return await asyncio.get_running_loop().run_in_executor(self._write_exec, _go)

    async def housekeeping(self, retention_days: float, audit_days: float, *, vacuum: bool = False) -> dict[str, int]:
        """Roll old usage into usage_daily, delete old messages / events / usage / audit rows, and
        optionally VACUUM. Returns rows deleted per table."""
        from omnibots.backup import HOUSEKEEPING, cutoff_iso
        params = {"cutoff": cutoff_iso(retention_days), "audit_cutoff": cutoff_iso(audit_days)}
        tables = ("usage_daily", "provider_usage_events", "messages", "bot_events", "audit_logs")

        def _go() -> dict[str, int]:
            conn = self._conn
            assert conn is not None
            out: dict[str, int] = {}
            conn.execute("BEGIN IMMEDIATE")
            try:
                for table, sql in zip(tables, HOUSEKEEPING):
                    out[table] = conn.execute(sql, params).rowcount
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            if vacuum:
                conn.execute("VACUUM")
            return out
        return await asyncio.get_running_loop().run_in_executor(self._write_exec, _go)

    # ── writes ─────────────────────────────────────────────────────────
    async def write(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> int:
        """Queue one statement; resolves to its lastrowid once committed."""
        return await self._enqueue(_Write(sql, params))

    async def write_many(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:
        return await self._enqueue(_Write(sql, many=list(rows)))

    async def _enqueue(self, item: _Write) -> int:
        if self._queue is None:
            raise RuntimeError("database is not open")
        item.future = asyncio.get_running_loop().create_future()
        await self._queue.put(item)
        return await item.future

    async def _writer(self) -> None:
        assert self._queue is not None
        loop = asyncio.get_running_loop()
        stop = False
        while not stop:
            first = await self._queue.get()
            if first is None:
                break
            batch = [first]
            while len(batch) < self.BATCH_MAX and not self._queue.empty():
                nxt = self._queue.get_nowait()
                if nxt is None:
                    stop = True
                    break
                batch.append(nxt)
            try:
                results = await loop.run_in_executor(self._write_exec, self._commit_batch, batch)
                for item, rowid in zip(batch, results):
                    if not item.future.done():
                        item.future.set_result(rowid)
            except Exception as exc:  # the batch was rolled back; retry one by one to isolate the bad write
                log.warning("write batch of %d failed (%s); retrying individually", len(batch), exc)
                for item in batch:
                    try:
                        (rowid,) = await loop.run_in_executor(self._write_exec, self._commit_batch, [item])
                        item.future.set_result(rowid)
                    except Exception as one_exc:
                        if not item.future.done():
                            item.future.set_exception(one_exc)

    def _commit_batch(self, batch: list[_Write]) -> list[int]:
        conn = self._conn
        assert conn is not None
        results = []
        conn.execute("BEGIN IMMEDIATE")
        try:
            for item in batch:
                if item.many is not None:
                    cur = conn.executemany(item.sql, item.many)
                else:
                    cur = conn.execute(item.sql, item.params)
                results.append(cur.lastrowid or 0)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return results

    # ── reads ──────────────────────────────────────────────────────────
    async def read(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[sqlite3.Row]:
        def _read() -> list[sqlite3.Row]:
            conn = open_connection(self.path, readonly=True)
            try:
                return conn.execute(sql, params).fetchall()
            finally:
                conn.close()

        return await asyncio.get_running_loop().run_in_executor(self._read_exec, _read)

    async def read_one(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Row | None:
        rows = await self.read(sql, params)
        return rows[0] if rows else None

    # ── helpers ────────────────────────────────────────────────────────
    async def audit(self, actor_type: str, actor_id: str | None, action: str, details_json: str | None = None) -> int:
        return await self.write(
            "INSERT INTO audit_logs (actor_type, actor_id, action, details_json) VALUES (?, ?, ?, ?)",
            (actor_type, actor_id, action, details_json),
        )
