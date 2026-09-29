"""OmniBots' own Omni-shaped config, for a PC without Omni (PLAN.md A1.s.01).

`<omnibots home>/config` is laid out like Omni's home, so the same loader, the same
Providers page and the same file formats work in both cases:

  settings.json      Omni's format: defaultProvider, defaultModel, providers, models.
                     Provider entries hold names, URLs and flags, never a key.
  .env               Omni's format. Real environment variables (OMNI_<NAME>_KEY) still
                     work; a key typed into this file is moved into the database by the
                     doctor and the line is commented out.
  omni.config.json   Omni's project config: skills and MCP servers (empty to start).

Keys: the `provider_keys` table in `<home>/db/omnibots.sqlite`, DPAPI-encrypted.
If Omni is installed later, OmniBots uses Omni's files again; this folder is left as it is.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from omnibots.omni.locate import OmniLocation

ENV_TEMPLATE = """\
# OmniBots' own provider settings (Omni is not installed on this PC).
# Same format as Omni's .env. Provider keys are NOT kept in this file:
# add them in OmniBots -> Settings -> Providers, where they are stored encrypted
# in OmniBots' database (only your Windows user can read them).
# A key typed below is moved there by the doctor, and its line is commented out.
#
# OMNI_MINIMAX_IO_KEY=
# OMNI_OPENROUTER_KEY=
"""

PROJECT_TEMPLATE = {"skills": [], "mcpServers": {}}
_ENV_KEY = re.compile(r"^\s*OMNI_([A-Z0-9_]+)_KEY\s*=\s*(.*)$")


def settings_template() -> dict:
    from omnibots.omni.config import load_defaults
    d = load_defaults()
    return {"defaultProvider": d.get("defaultProvider"), "defaultModel": d.get("defaultModel"), "providers": {}}


def missing_files(loc: OmniLocation) -> list[Path]:
    return [f for f in (loc.settings_file, loc.home / ".env", loc.project_config_file) if not f.is_file()]


def ensure_standalone(loc: OmniLocation) -> list[Path]:
    """Create whatever is missing. Existing files are never overwritten. Returns what was created."""
    if not loc.standalone:
        raise ValueError("ensure_standalone only works on OmniBots' own config, never on Omni's")
    loc.home.mkdir(parents=True, exist_ok=True)
    made = []
    for f in missing_files(loc):
        if f == loc.settings_file:
            body = json.dumps(settings_template(), indent=2)
        elif f.name == ".env":
            body = ENV_TEMPLATE
        else:
            body = json.dumps(PROJECT_TEMPLATE, indent=2)
        f.write_text(body, encoding="utf-8", newline="\n")
        made.append(f)
    return made


def plaintext_keys(loc: OmniLocation) -> list[str]:
    """Where a key sits as text in the standalone files ("settings.json: groq", ".env: OMNI_GROQ_KEY")."""
    found = []
    try:
        from omnibots.omni.config import read_settings_json
        doc = read_settings_json(loc.settings_file)
    except (OSError, ValueError):
        doc = {}
    for name, p in (doc.get("providers") or {}).items():
        if not isinstance(p, dict):
            continue
        if str(p.get("apiKey") or "").strip() not in ("", "not-needed"):
            found.append(f"settings.json: {name}")
        for acct, v in (p.get("accounts") or {}).items() if isinstance(p.get("accounts"), dict) else ():
            if str(v or "").strip() not in ("", "not-needed"):
                found.append(f"settings.json: {name}/{acct}")
    try:
        for line in (loc.home / ".env").read_text(encoding="utf-8").splitlines():
            m = _ENV_KEY.match(line)
            if m and m.group(2).strip().strip("\"'"):
                found.append(f".env: OMNI_{m.group(1)}_KEY")
    except OSError:
        pass
    return found


def move_plaintext_keys(loc: OmniLocation) -> list[str]:
    """Move text keys from the standalone settings.json and .env into the encrypted store.

    A key is removed from a file only after it's safely stored. Returns what moved.
    """
    from omnibots.omni import keystore
    from omnibots.omni.config import load_defaults, read_settings_json
    from omnibots.omni.providers_store import _commit

    if not loc.keys_in_db:
        return []
    moved: list[str] = []
    try:
        doc = read_settings_json(loc.settings_file)
    except (OSError, ValueError):
        doc = None
    if isinstance(doc, dict) and isinstance(doc.get("providers"), dict):
        changed = False
        for name, p in doc["providers"].items():
            if not isinstance(p, dict):
                continue
            key = str(p.get("apiKey") or "").strip()
            accounts = p.get("accounts") if isinstance(p.get("accounts"), dict) else None
            for acct, v in list((accounts or {}).items()):
                val = str(v or "").strip()
                if val and val != "not-needed":
                    keystore.set_key(loc.db_file, name, val, acct)
                    accounts[acct] = ""
                    moved.append(f"{name}/{acct}")
                    changed = True
            if key and key != "not-needed":
                if not accounts:
                    keystore.set_key(loc.db_file, name, key)
                    moved.append(name)
                p["apiKey"] = ""               # with accounts, apiKey only mirrors the active account
                changed = True
        if changed:
            _commit(loc.settings_file, doc)

    env_file = loc.home / ".env"
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    if lines:
        names = {re.sub(r"[^A-Z0-9]", "_", n.upper()): n for n in (load_defaults().get("providers") or {})}
        names.setdefault("MINIMAX", "minimax.io")         # Omni's OMNI_MINIMAX_KEY alias
        if isinstance(doc, dict):
            names.update({re.sub(r"[^A-Z0-9]", "_", n.upper()): n for n in (doc.get("providers") or {})})
        out, changed = [], False
        for line in lines:
            m = _ENV_KEY.match(line)
            val = m.group(2).strip().strip("\"'") if m else ""
            if m and val:
                provider = names.get(m.group(1), m.group(1).lower())
                keystore.set_key(loc.db_file, provider, val)
                out.append(f"# OMNI_{m.group(1)}_KEY= (moved into OmniBots' encrypted store)")
                moved.append(provider)
                changed = True
            else:
                out.append(line)
        if changed:
            env_file.write_text("\n".join(out) + "\n", encoding="utf-8", newline="\n")
    return moved
