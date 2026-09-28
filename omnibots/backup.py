"""Backup, restore and housekeeping (PLAN.md A16.c).

What a backup holds: the database (SQLite's online backup API, safe while the app runs in WAL
mode), every bot's memory.md, settings.toml, user_profile.md, sessions/ and skills/. Never the
browser profiles (big, and full of cookies) and never secret values: the vault keeps those in
Windows Credential Manager; only its index (a database table) is copied.

Backups live in `~/.omnibots/backups/<YYYY-MM-DD_HHMMSS>-<reason>/`; the newest `keep` of each
reason are kept. A restore can't swap the database under a running engine, so Tray → Restore…
records the choice in `restore.json` and restarts; `apply_pending_restore` runs at the next start,
before the database is opened, and first saves the current state as a `before-restore` backup.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DB_REL = Path("db") / "omnibots.sqlite"
FILES = ("settings.toml", "user_profile.md")
TREES = ("sessions", "skills")
REQUEST = "restore.json"


def backups_dir(home: Path) -> Path:
    return home / "backups"


def _copy_files(src_home: Path, dest: Path) -> None:
    """settings, the user profile, sessions/, skills/ and every bot's memory.md: src_home → dest."""
    for name in FILES:
        if (src_home / name).is_file():
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_home / name, dest / name)
    for tree in TREES:
        if (src_home / tree).is_dir():
            shutil.copytree(src_home / tree, dest / tree, dirs_exist_ok=True)
    bots = src_home / "bots"
    for mem in bots.glob("*/memory.md") if bots.is_dir() else []:
        target = dest / "bots" / mem.parent.name / "memory.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(mem, target)


def copy_db(src_conn: sqlite3.Connection, dest_file: Path) -> None:
    """A consistent copy of a live database (the online backup API)."""
    dest_file.parent.mkdir(parents=True, exist_ok=True)
    out = sqlite3.connect(dest_file)
    try:
        src_conn.backup(out)
    finally:
        out.close()


def new_backup_folder(home: Path, reason: str) -> Path:
    stamp = time.strftime("%Y-%m-%d_%H%M%S")
    dest, n = backups_dir(home) / f"{stamp}-{reason}", 2
    while dest.exists():                                   # two backups in the same second
        dest, n = backups_dir(home) / f"{stamp}-{reason}-{n}", n + 1
    dest.mkdir(parents=True)
    return dest


def finish_backup(home: Path, dest: Path, reason: str, keep: int) -> Path:
    """Copy the files next to the database copy, write the manifest, prune old ones."""
    _copy_files(home, dest)
    (dest / "backup.json").write_text(json.dumps({"reason": reason, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}, indent=1),
                                      encoding="utf-8")
    prune(home, keep)
    log.info("backup (%s) written to %s", reason, dest)
    return dest


def backup_closed(home: Path, reason: str, keep: int = 7) -> Path | None:
    """A backup while the app doesn't hold the database (before a migration or a restore)."""
    db = home / DB_REL
    if not db.is_file():
        return None
    dest = new_backup_folder(home, reason)
    src = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        copy_db(src, dest / DB_REL)
    finally:
        src.close()
    return finish_backup(home, dest, reason, keep)


def list_backups(home: Path) -> list[dict[str, Any]]:
    """Newest first: {path, name, reason, at, size_mb}."""
    out = []
    root = backups_dir(home)
    for d in sorted(root.iterdir(), reverse=True) if root.is_dir() else []:
        if not d.is_dir() or not (d / DB_REL).is_file():
            continue
        try:
            meta = json.loads((d / "backup.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
        out.append({"path": str(d), "name": d.name, "reason": meta.get("reason") or d.name[18:],
                    "at": meta.get("at", ""), "size_mb": round(size / 1e6, 2)})
    return out


def prune(home: Path, keep: int) -> list[str]:
    """Keep the newest `keep` backups of each reason (at least 1); returns the names removed."""
    by: dict[str, list[dict[str, Any]]] = {}
    for b in list_backups(home):
        by.setdefault(b["reason"], []).append(b)
    gone = []
    for items in by.values():
        for b in items[max(1, keep):]:
            shutil.rmtree(b["path"], ignore_errors=True)
            gone.append(b["name"])
    return gone


def request_restore(home: Path, backup: Path) -> Path:
    if not (Path(backup) / DB_REL).is_file():
        raise ValueError(f"{backup} is not an OmniBots backup (no database in it)")
    req = home / REQUEST
    req.write_text(json.dumps({"backup": str(backup)}), encoding="utf-8")
    return req


def apply_pending_restore(home: Path, keep: int = 7) -> str | None:
    """At startup, before the database is opened: restore the backup the user chose.
    Returns what happened (for the log and a note to the user), or None when nothing was pending."""
    req = home / REQUEST
    if not req.is_file():
        return None
    try:
        src = Path(json.loads(req.read_text(encoding="utf-8"))["backup"])
    except (OSError, ValueError, KeyError) as exc:
        req.unlink(missing_ok=True)
        return f"restore skipped: the request was unreadable ({exc})"
    if not (src / DB_REL).is_file():
        req.unlink(missing_ok=True)
        return f"restore skipped: {src} has no database"
    safety = backup_closed(home, "before-restore", keep)       # what we're about to replace
    db = home / DB_REL
    for extra in (db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
        extra.unlink(missing_ok=True)
    db.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src / DB_REL, db)
    _copy_files(src, home)
    req.unlink(missing_ok=True)
    return f"restored {src.name}" + (f" (the state before it is saved as {safety.name})" if safety else "")


def wait_for_pid(pid: int, timeout: float = 30.0) -> bool:
    """Windows: wait for the old instance to exit before a restart goes on (it holds the lock).
    (Not os.kill(pid, 0): on Windows that terminates the process.)"""
    if sys.platform != "win32" or pid <= 0:
        return True
    import ctypes
    SYNCHRONIZE, WAIT_OBJECT_0 = 0x00100000, 0
    h = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not h:
        return True                                             # already gone
    try:
        return ctypes.windll.kernel32.WaitForSingleObject(h, int(timeout * 1000)) == WAIT_OBJECT_0
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


# ── housekeeping (A16.c.04) ──────────────────────────────────────────────
# Old usage rows are rolled up into usage_daily first, so totals (A13) survive the clean-up.
HOUSEKEEPING = (
    """INSERT INTO usage_daily (day, provider, model, bot_id, tokens_in, tokens_out, calls)
       SELECT substr(created_at, 1, 10), provider, COALESCE(model, ''), COALESCE(bot_id, ''),
              COALESCE(SUM(tokens_in), 0), COALESCE(SUM(tokens_out), 0), COUNT(*)
       FROM provider_usage_events WHERE created_at < :cutoff
       GROUP BY 1, 2, 3, 4
       ON CONFLICT(day, provider, model, bot_id) DO UPDATE SET
           tokens_in = tokens_in + excluded.tokens_in, tokens_out = tokens_out + excluded.tokens_out,
           calls = calls + excluded.calls""",
    "DELETE FROM provider_usage_events WHERE created_at < :cutoff",
    "DELETE FROM messages WHERE created_at < :cutoff",
    "DELETE FROM bot_events WHERE created_at < :cutoff",
    "DELETE FROM audit_logs WHERE created_at < :audit_cutoff",
)


def cutoff_iso(days: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - days * 86400))
