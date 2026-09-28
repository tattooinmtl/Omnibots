"""A16.c backup, restore and housekeeping. Real SQLite files, real folders, a real app process for
the restore-at-start path."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from conftest import run_app

from omnibots import backup
from omnibots.db.database import Database, apply_migrations, list_migrations, open_connection


def run(coro):
    return asyncio.run(coro)


def make_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / "bots" / "omi").mkdir(parents=True)
    (home / "bots" / "omi" / "memory.md").write_text("# Omi\n- remembers the user likes blue\n", encoding="utf-8")
    (home / "settings.toml").write_text("[app]\nlog_level = \"INFO\"\n", encoding="utf-8")
    (home / "user_profile.md").write_text("# User\n", encoding="utf-8")
    (home / "profiles" / "omi").mkdir(parents=True)
    (home / "profiles" / "omi" / "Cookies").write_text("session=secret", encoding="utf-8")      # never backed up
    return home


def test_a_live_backup_holds_the_database_and_the_files_but_no_browser_profiles(tmp_path):
    home = make_home(tmp_path)

    async def go():
        db = Database(home / backup.DB_REL)
        await db.open()
        writes = [asyncio.create_task(db.audit("test", None, f"row{i}", "{}")) for i in range(200)]
        dest = await db.backup_to(home, "manual", keep=7)
        await asyncio.gather(*writes)
        await db.close()
        return dest
    dest = run(go())
    copy = sqlite3.connect(dest / backup.DB_REL)
    n = copy.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]
    version = copy.execute("PRAGMA user_version").fetchone()[0]
    copy.close()
    assert 0 <= n <= 200 and version == len(list_migrations())            # a consistent snapshot, mid-writes
    assert (dest / "bots" / "omi" / "memory.md").read_text(encoding="utf-8").endswith("likes blue\n")
    assert (dest / "settings.toml").is_file() and (dest / "user_profile.md").is_file()
    assert not (dest / "profiles").exists()
    assert json.loads((dest / "backup.json").read_text(encoding="utf-8"))["reason"] == "manual"
    assert [b["name"] for b in backup.list_backups(home)] == [dest.name]


def test_prune_keeps_the_newest_of_each_kind(tmp_path):
    home = make_home(tmp_path)
    for reason in ("daily",) * 4 + ("manual",) * 2:
        d = backup.new_backup_folder(home, reason)
        (d / backup.DB_REL).parent.mkdir(parents=True)
        (d / backup.DB_REL).write_bytes(b"x")
        (d / "backup.json").write_text(json.dumps({"reason": reason}), encoding="utf-8")
    gone = backup.prune(home, keep=2)
    left = [b["reason"] for b in backup.list_backups(home)]
    assert len(gone) == 2 and sorted(left) == ["daily", "daily", "manual", "manual"]


def test_restore_happens_before_the_database_opens_and_saves_the_current_state_first(tmp_path):
    home = make_home(tmp_path)

    async def seed(marker: str):
        db = Database(home / backup.DB_REL)
        await db.open()
        await db.audit("test", None, marker, "{}")
        await db.close()
    run(seed("before"))
    old = backup.backup_closed(home, "manual")
    run(seed("after"))                                                      # the state we'll roll back
    (home / "bots" / "omi" / "memory.md").write_text("# Omi\n- forgot everything\n", encoding="utf-8")
    backup.request_restore(home, old)
    msg = backup.apply_pending_restore(home)
    conn = sqlite3.connect(home / backup.DB_REL)
    actions = [r[0] for r in conn.execute("SELECT action FROM audit_logs")]
    conn.close()
    kinds = [b["reason"] for b in backup.list_backups(home)]
    assert msg.startswith(f"restored {old.name}") and "before-restore" in msg
    assert "before" in actions and "after" not in actions
    assert "likes blue" in (home / "bots" / "omi" / "memory.md").read_text(encoding="utf-8")
    assert "before-restore" in kinds and not (home / backup.REQUEST).exists()
    assert backup.apply_pending_restore(home) is None                       # nothing pending any more


def test_a_database_about_to_be_upgraded_is_backed_up_first(tmp_path):
    home = make_home(tmp_path)
    db_file = home / backup.DB_REL
    db_file.parent.mkdir(parents=True, exist_ok=True)
    older = tmp_path / "older_migrations"
    older.mkdir()
    for n, p in list_migrations()[:2]:
        (older / p.name).write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
    conn = open_connection(db_file)
    assert apply_migrations(conn, older) == 2                                # an install from before the upgrade
    conn.close()

    async def go():
        db = Database(db_file, backup_home=home)
        await db.open()
        await db.close()
        return db.schema_version
    version = run(go())
    (b,) = backup.list_backups(home)
    copy = sqlite3.connect(Path(b["path"]) / backup.DB_REL)
    assert b["reason"] == "before-migration" and copy.execute("PRAGMA user_version").fetchone()[0] == 2
    copy.close()
    assert version == len(list_migrations())


def test_housekeeping_keeps_daily_totals_and_deletes_old_rows(tmp_path):
    async def go():
        db = Database(tmp_path / "db.sqlite")
        await db.open()
        old, new = "2020-01-02T10:00:00.000Z", time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        for ts, n in ((old, 3), (new, 2)):
            for _ in range(n):
                await db.write("INSERT INTO provider_usage_events (provider, model, bot_id, tokens_in, tokens_out, status_code, created_at) "
                               "VALUES ('minimax.io', 'm', 'b1', 100, 50, 200, ?)", (ts,))
                await db.write("INSERT INTO messages (topic, sender_type, message_type, payload_json, created_at) "
                               "VALUES ('#general', 'system', 'PROGRESS_UPDATE', '{\"text\":\"x\"}', ?)", (ts,))
                await db.write("INSERT INTO audit_logs (actor_type, action, created_at) VALUES ('system', 'x', ?)", (ts,))
        counts = await db.housekeeping(90, 365, vacuum=True)
        daily = [dict(r) for r in await db.read("SELECT * FROM usage_daily")]
        left = {t: (await db.read_one(f"SELECT COUNT(*) AS n FROM {t}"))["n"] for t in ("provider_usage_events", "messages", "audit_logs")}
        await db.close()
        return counts, daily, left
    counts, daily, left = run(go())
    assert daily == [{"day": "2020-01-02", "provider": "minimax.io", "model": "m", "bot_id": "b1",
                      "tokens_in": 300, "tokens_out": 150, "calls": 3}]
    assert left == {"provider_usage_events": 2, "messages": 2, "audit_logs": 2}
    assert counts["provider_usage_events"] == 3 and counts["messages"] == 3 and counts["audit_logs"] == 3


def test_a_restart_waits_for_the_old_instance_to_exit():
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1.0)"])
    t0 = time.perf_counter()
    assert backup.wait_for_pid(p.pid, timeout=10)
    assert time.perf_counter() - t0 >= 0.5 and p.wait(5) == 0
    assert backup.wait_for_pid(p.pid, timeout=1)                            # already gone: returns at once


def test_the_app_applies_a_pending_restore_at_start(home):
    """Real `python -m omnibots` process: restore.json is applied before the database is opened."""
    r = run_app("--no-window", "--exit-after", "1")
    assert r.returncode == 0, r.stderr
    snap = backup.backup_closed(home, "manual")
    conn = sqlite3.connect(home / backup.DB_REL)
    conn.execute("INSERT INTO audit_logs (actor_type, action) VALUES ('test', 'made-after-the-backup')")
    conn.commit()
    conn.close()
    backup.request_restore(home, snap)
    r = run_app("--no-window", "--exit-after", "1")
    assert r.returncode == 0, r.stderr
    conn = sqlite3.connect(home / backup.DB_REL)
    actions = [x[0] for x in conn.execute("SELECT action FROM audit_logs")]
    conn.close()
    assert "made-after-the-backup" not in actions and not (home / backup.REQUEST).exists()
    assert "backup: restored" in (home / "logs" / "app.log").read_text(encoding="utf-8")
