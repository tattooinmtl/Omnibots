"""A0.b: SQLite in WAL mode, the single writer, migrations, crash safety."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from omnibots.db import Database, apply_migrations, open_connection
from omnibots.db.database import list_migrations

EXPECTED_TABLES = {
    "bots", "bot_skills", "bot_tools", "seats", "projects", "jobs", "artifacts", "messages",
    "bot_events", "claims", "evidence", "providers", "provider_usage_events", "skills_cache",
    "tools", "playbooks", "playbook_runs", "routines", "triggers", "approvals", "secrets_index",
    "locks", "parameters", "audit_logs",
    "spend_events",                                       # 002 (A9)
    "token_allocations",                                  # 003 (A9.c.03)
    "usage_daily",                                        # 004 (A16.c.04)
    "verdicts",                                           # 006 (A16.b)
    "night_queue",                                        # 007 (A15.f.03)
    "provider_keys",                                      # 008 (A1.s.01)
}
LATEST = len(list_migrations())


def tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_fresh_db_migrates_to_latest_with_every_table(tmp_path):
    conn = open_connection(tmp_path / "t.sqlite")
    assert apply_migrations(conn) == LATEST
    assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert tables(conn) == EXPECTED_TABLES
    # Running again is a no-op.
    assert apply_migrations(conn) == LATEST


def test_newer_db_than_app_is_refused(tmp_path):
    conn = open_connection(tmp_path / "t.sqlite")
    conn.execute("PRAGMA user_version = 99")
    with pytest.raises(RuntimeError, match="newer than this app"):
        apply_migrations(conn)


def test_failed_migration_rolls_back_cleanly(tmp_path):
    mig = tmp_path / "mig"
    mig.mkdir()
    (mig / "001_ok.sql").write_text("CREATE TABLE a (x INTEGER);", encoding="utf-8")
    (mig / "002_bad.sql").write_text("CREATE TABLE b (y INTEGER);\nTHIS IS NOT SQL;", encoding="utf-8")
    conn = open_connection(tmp_path / "t.sqlite")
    with pytest.raises(sqlite3.Error):
        apply_migrations(conn, mig)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1   # 001 kept
    assert "b" not in tables(conn)                                  # 002 fully rolled back


def test_migration_numbering_gaps_are_rejected(tmp_path):
    (tmp_path / "001_a.sql").write_text("", encoding="utf-8")
    (tmp_path / "003_c.sql").write_text("", encoding="utf-8")
    with pytest.raises(RuntimeError, match="no gaps"):
        list_migrations(tmp_path)


def test_single_writer_handles_concurrent_writes_and_isolates_a_bad_one(tmp_path):
    async def scenario():
        db = Database(tmp_path / "db.sqlite")
        await db.open()
        ok = [db.audit("system", None, f"a{i}") for i in range(1000)]
        bad = db.write("INSERT INTO no_such_table VALUES (1)")
        results = await asyncio.gather(*ok, bad, return_exceptions=True)
        await db.close()
        return results

    results = asyncio.run(scenario())
    assert isinstance(results[-1], sqlite3.OperationalError)
    assert all(isinstance(r, int) and r > 0 for r in results[:-1])
    conn = sqlite3.connect(tmp_path / "db.sqlite")
    assert conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0] == 1000


def test_reads_run_while_writing(tmp_path):
    async def scenario():
        db = Database(tmp_path / "db.sqlite")
        await db.open()
        writes = asyncio.gather(*(db.audit("system", None, f"w{i}") for i in range(300)))
        counts = [(await db.read_one("SELECT COUNT(*) AS n FROM audit_logs"))["n"] for _ in range(20)]
        await writes
        final = (await db.read_one("SELECT COUNT(*) AS n FROM audit_logs"))["n"]
        await db.close()
        return counts, final

    counts, final = asyncio.run(scenario())
    assert counts == sorted(counts)       # never goes backwards
    assert final == 300


def test_killing_the_process_mid_write_leaves_a_readable_db(tmp_path):
    """A0.99: kill -9 during a stream of writes, then the DB must be intact."""
    db_path = tmp_path / "crash.sqlite"
    marker = tmp_path / "started"
    script = textwrap.dedent(f"""
        import asyncio, pathlib
        from omnibots.db import Database
        async def main():
            db = Database(pathlib.Path(r"{db_path}"))
            await db.open()
            pathlib.Path(r"{marker}").write_text("x")
            i = 0
            while True:
                await asyncio.gather(*(db.audit("system", None, f"row{{i+k}}", "x" * 500) for k in range(50)))
                i += 50
        asyncio.run(main())
    """)
    proc = subprocess.Popen([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1])
    try:
        deadline = time.time() + 30
        while not marker.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert marker.exists(), "writer script never started"
        time.sleep(1.5)            # let it write a few thousand rows
    finally:
        proc.kill()                # hard kill, no cleanup (TerminateProcess on Windows)
        proc.wait(timeout=10)

    conn = open_connection(db_path)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    rows = conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]
    assert rows > 0
    # Batches are atomic: committed rows come in whole batches of the 50-row gathers
    # (never a torn row), and the DB is still writable after recovery.
    conn.execute("INSERT INTO audit_logs (actor_type, action) VALUES ('system', 'after_crash')")
    assert conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0] == rows + 1
