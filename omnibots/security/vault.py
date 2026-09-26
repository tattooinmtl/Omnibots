"""The vault (PLAN.md A9.c.01, ADR-7): secrets by handle, never by value.

  - Values live in Windows Credential Manager (keyring service "omnibots-vault").
    Sandboxed scripts run in an AppContainer that is denied the credential vault.
  - SQL (`secrets_index`) holds only the name, kind, note and the hosts it may go to.
  - Bots write `{{secret:<name>}}` in the arguments a tool declares as secret-capable
    (`Tool.secret_args`). The tool layer substitutes the value right before the call.
    Using a secret is R3 (acting as the user), and a secret with `hosts` is refused for
    any other host, so a prompt-injected bot can't send it elsewhere.
  - Every vault value is redacted to `[secret:<name>]` in tool results (the model
    never sees one), bot events, board messages and logs.

CLI (the user types the value; it's never echoed):
  python -m omnibots.security.vault set github_token --hosts api.github.com --note "repo read"
  python -m omnibots.security.vault list
  python -m omnibots.security.vault delete github_token
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

SERVICE = "omnibots-vault"
HANDLE = re.compile(r"\{\{\s*secret:([A-Za-z0-9_.\-]+)\s*\}\}")
NAME_OK = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
MIN_REDACT = 6

# value -> name, shared by every redaction point in the process
_values: dict[str, str] = {}
_lock = threading.Lock()


def _remember(name: str, value: str | None) -> None:
    if value and len(value) >= MIN_REDACT:
        with _lock:
            _values[value] = name
        from omnibots.logging_setup import register_secret
        register_secret(value)


def _forget(value: str | None) -> None:
    with _lock:
        _values.pop(value or "", None)


def scrub(text: str) -> str:
    """Replace every known vault value with [secret:<name>]."""
    if not text or not _values:
        return text
    with _lock:
        pairs = sorted(_values.items(), key=lambda kv: len(kv[0]), reverse=True)
    for value, name in pairs:
        if value in text:
            text = text.replace(value, f"[secret:{name}]")
    return text


def scrub_obj(obj: Any) -> Any:
    """scrub() through dicts/lists/strings (board payloads)."""
    if isinstance(obj, str):
        return scrub(obj)
    if isinstance(obj, dict):
        return {k: scrub_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub_obj(v) for v in obj]
    return obj


class SecretError(ValueError):
    pass


@dataclass
class SecretInfo:
    name: str
    kind: str
    note: str
    hosts: list[str]

    def allows(self, host: str | None) -> bool:
        if not self.hosts:
            return True
        h = (host or "").lower().rstrip(".")
        return any(h == a or h.endswith("." + a) for a in self.hosts)


class Vault:
    def __init__(self, db, service: str = SERVICE):
        self.db, self.service = db, service
        self._meta: dict[str, SecretInfo] = {}

    @staticmethod
    def _kr():
        import keyring
        return keyring

    async def load(self) -> int:
        """Read the index and register every value for redaction (call once at startup)."""
        rows = await self.db.read("SELECT name, kind, note, hosts FROM secrets_index")
        self._meta = {r["name"]: SecretInfo(r["name"], r["kind"] or "", r["note"] or "",
                                            [h for h in (r["hosts"] or "").split(",") if h]) for r in rows}
        for name in self._meta:
            _remember(name, self._kr().get_password(self.service, name))
        return len(self._meta)

    async def set(self, name: str, value: str, *, kind: str = "", note: str = "", hosts: list[str] | None = None) -> SecretInfo:
        if not NAME_OK.match(name):
            raise SecretError("a secret name is 1-64 letters, digits, _ . -")
        if not value:
            raise SecretError("empty secret value")
        old = self._kr().get_password(self.service, name)
        self._kr().set_password(self.service, name, value)
        if old and old != value:
            _forget(old)
        hosts = [h.strip().lower() for h in (hosts or []) if h.strip()]
        await self.db.write("INSERT INTO secrets_index (name, kind, note, hosts) VALUES (?,?,?,?) "
                            "ON CONFLICT(name) DO UPDATE SET kind=excluded.kind, note=excluded.note, hosts=excluded.hosts",
                            (name, kind, note, ",".join(hosts)))
        info = SecretInfo(name, kind, note, hosts)
        self._meta[name] = info
        _remember(name, value)
        await self.db.audit("user", None, "secret_set", f'{{"name": "{name}", "hosts": "{",".join(hosts)}"}}')
        return info

    async def delete(self, name: str) -> bool:
        value = self._kr().get_password(self.service, name)
        if value is not None:
            self._kr().delete_password(self.service, name)
        indexed = name in self._meta
        await self.db.write("DELETE FROM secrets_index WHERE name=?", (name,))
        self._meta.pop(name, None)
        # keep redacting the old value: it may still sit in old outputs
        await self.db.audit("user", None, "secret_deleted", f'{{"name": "{name}"}}')
        return value is not None or indexed

    def names(self) -> list[SecretInfo]:
        return sorted(self._meta.values(), key=lambda s: s.name)

    def info(self, name: str) -> SecretInfo | None:
        return self._meta.get(name)

    def value(self, name: str) -> str | None:
        """Only the tool layer calls this, right before a tool runs."""
        if name not in self._meta:
            return None
        v = self._kr().get_password(self.service, name)
        _remember(name, v)
        return v

    # ── the tool layer ─────────────────────────────────────────────────────
    def handles_in(self, obj: Any) -> set[str]:
        found: set[str] = set()
        if isinstance(obj, str):
            found.update(HANDLE.findall(obj))
        elif isinstance(obj, dict):
            for v in obj.values():
                found |= self.handles_in(v)
        elif isinstance(obj, list):
            for v in obj:
                found |= self.handles_in(v)
        return found

    def resolve(self, obj: Any, target: str | None) -> Any:
        """Substitute {{secret:x}} handles. `target` is the URL/host the values will go to."""
        host = urlparse(target).hostname if target and "://" in target else target
        if isinstance(obj, str):
            def sub(m: re.Match) -> str:
                name = m.group(1)
                info = self._meta.get(name)
                if info is None:
                    raise SecretError(f"no secret named {name!r} in the vault (ask the user to add it)")
                if not info.allows(host):
                    raise SecretError(f"secret {name!r} may only be sent to {', '.join(info.hosts)}, not {host or 'this target'}")
                value = self.value(name)
                if value is None:
                    raise SecretError(f"secret {name!r} is indexed but missing from the credential vault")
                return value
            return HANDLE.sub(sub, obj)
        if isinstance(obj, dict):
            return {k: self.resolve(v, target) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.resolve(v, target) for v in obj]
        return obj


# ── CLI ─────────────────────────────────────────────────────────────────────
def _main(argv: list[str]) -> int:
    import argparse
    import asyncio
    import getpass

    from omnibots.db import Database
    from omnibots.paths import get_paths

    ap = argparse.ArgumentParser(prog="python -m omnibots.security.vault")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("set")
    s.add_argument("name")
    s.add_argument("--hosts", default="", help="comma-separated hosts this secret may be sent to")
    s.add_argument("--note", default="")
    s.add_argument("--kind", default="token")
    sub.add_parser("list")
    d = sub.add_parser("delete")
    d.add_argument("name")
    a = ap.parse_args(argv)

    async def go() -> int:
        paths = get_paths()
        db = Database(paths.db_file)
        await db.open()
        v = Vault(db)
        await v.load()
        try:
            if a.cmd == "set":
                value = getpass.getpass(f"value for {a.name} (not shown): ")
                info = await v.set(a.name, value, kind=a.kind, note=a.note, hosts=a.hosts.split(","))
                print(f"saved {info.name}" + (f" (only for {', '.join(info.hosts)})" if info.hosts else " (any host)"))
            elif a.cmd == "list":
                for i in v.names():
                    print(f"{i.name:24} {i.kind:8} {','.join(i.hosts) or 'any host':30} {i.note}")
            else:
                print("deleted" if await v.delete(a.name) else "no such secret")
            return 0
        finally:
            await db.close()
    return asyncio.run(go())


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv[1:]))
