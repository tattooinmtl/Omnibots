"""Provider keys OmniBots keeps itself when Omni isn't installed (PLAN.md A1.s.01).

The Omni-shaped config in `<home>/config` (settings.json, .env) holds provider names, URLs
and models, never a key. The keys live in the `provider_keys` table of OmniBots' database,
each one a Windows DPAPI blob (security/dpapi.py), so a copy of the files or the database
alone gives nothing away.

These are short synchronous calls on their own connection (busy_timeout 5 s under WAL), made
from the config loader, the Providers page and the doctor. They're rare, small, user-driven
writes; the engine's writer queue keeps every other write (ADR-6, noted in A1.s.01).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from omnibots.db.database import apply_migrations, open_connection
from omnibots.omni.config import key_fingerprint
from omnibots.security import dpapi


class KeystoreError(RuntimeError):
    """The message never includes a key."""


def _connect(db_file: Path, *, write: bool = False) -> sqlite3.Connection | None:
    """None when there's no key table yet (reads then find nothing). A brand-new database is
    created at the current schema; an older one is upgraded only by the app or the doctor,
    which back it up first."""
    if not write and not db_file.is_file():
        return None
    db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = open_connection(db_file)
    has = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='provider_keys'").fetchone()
    if has:
        return conn
    if conn.execute("PRAGMA user_version").fetchone()[0] == 0 and write:
        apply_migrations(conn)
        return conn
    conn.close()
    if write:
        raise KeystoreError("OmniBots' database needs an upgrade first: start OmniBots once, or run the doctor")
    return None


def read_keys(db_file: Path) -> dict[str, dict[str, str]]:
    """{provider: {account ('' = the provider key): key}}. Unreadable rows are skipped."""
    conn = _connect(db_file)
    if conn is None:
        return {}
    try:
        rows = conn.execute("SELECT provider, account, secret FROM provider_keys").fetchall()
    finally:
        conn.close()
    out: dict[str, dict[str, str]] = {}
    for r in rows:
        try:
            out.setdefault(r["provider"], {})[r["account"] or ""] = dpapi.unprotect(r["secret"])
        except dpapi.DpapiError:
            continue
    return out


def unreadable(db_file: Path) -> list[str]:
    """Rows this Windows user can't decrypt (the database came from another PC or account)."""
    conn = _connect(db_file)
    if conn is None:
        return []
    try:
        rows = conn.execute("SELECT provider, account, secret FROM provider_keys").fetchall()
    finally:
        conn.close()
    bad = []
    for r in rows:
        try:
            dpapi.unprotect(r["secret"])
        except dpapi.DpapiError:
            bad.append(r["provider"] + (f"/{r['account']}" if r["account"] else ""))
    return bad


def set_key(db_file: Path, provider: str, key: str, account: str = "") -> None:
    key = (key or "").strip()
    if not key:
        raise KeystoreError("empty key")
    try:
        blob = dpapi.protect(key)
    except dpapi.DpapiError as exc:
        raise KeystoreError(str(exc)) from exc
    conn = _connect(db_file, write=True)
    try:
        conn.execute("INSERT INTO provider_keys (provider, account, secret, fingerprint) VALUES (?,?,?,?) "
                     "ON CONFLICT(provider, account) DO UPDATE SET secret=excluded.secret, "
                     "fingerprint=excluded.fingerprint, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
                     (provider, account or "", blob, key_fingerprint(key)))
    finally:
        conn.close()


def delete_key(db_file: Path, provider: str, account: str | None = None) -> int:
    """Delete one account's key, or (account=None) every key of the provider."""
    conn = _connect(db_file)
    if conn is None:
        return 0
    try:
        if account is None:
            cur = conn.execute("DELETE FROM provider_keys WHERE provider=?", (provider,))
        else:
            cur = conn.execute("DELETE FROM provider_keys WHERE provider=? AND account=?", (provider, account or ""))
        return cur.rowcount
    finally:
        conn.close()


def overlay(providers: dict[str, Any], keys: dict[str, dict[str, str]]) -> set[str]:
    """Put stored keys into Omni-shaped provider entries (in place) before they're resolved.

    The provider key goes in `apiKey`, account keys in `accounts`. A value already in the
    entry (a plaintext key the doctor hasn't moved yet) wins, like Omni's settings-first rule.
    Returns the providers whose `apiKey` came from the store.
    """
    from_store: set[str] = set()
    for name, entry in providers.items():
        stored = keys.get(name)
        if not stored or not isinstance(entry, dict):
            continue
        if stored.get("") and not str(entry.get("apiKey") or "").strip():
            entry["apiKey"] = stored[""]
            from_store.add(name)
        accounts = entry.get("accounts")
        if isinstance(accounts, dict):
            accounts = dict(accounts)
            for acct, value in stored.items():
                if acct and acct in accounts and not str(accounts[acct] or "").strip():
                    accounts[acct] = value
            entry["accounts"] = accounts
    return from_store
