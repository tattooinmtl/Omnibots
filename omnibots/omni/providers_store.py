"""Add, edit, and remove providers in Omni's settings.json.

This is the one place OmniBots writes to Omni, and only because Settings →
Providers is the shared editor for that file (PLAN.md A11.p.01, ADR-7).
The write matches Omni's own save: the same path (`<omni home>/settings.json`),
UTF-8, two-space JSON, and the same provider fields (`baseUrl`, `apiKey`,
`label`, `nativeTools`, `accounts`, `activeAccount`). Every other setting in
the file is left as it was.

A key is written only when the user types one. Leaving the box blank keeps
the value already in the file, so a key that lives in the environment is
never copied into settings.json. For a provider with accounts, the key is
stored on the active account and mirrored into `apiKey`, which is what Omni
does in `setProviderKey` / `saveSettings`.

Without Omni (PLAN.md A1.s.01) the same functions edit OmniBots' own Omni-shaped
settings.json, and `keys_db` is set: a key then goes into the encrypted
`provider_keys` table (omni/keystore.py) and the file keeps an empty slot.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omnibots.omni.config import (
    load_defaults,
    mask_key,
    merge_with_defaults,
    read_env_view,
    _migrate,
    _resolve_provider,
)

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
URL_RE = re.compile(r"^https?://", re.I)
KEY_ACTIONS = {"keep", "set", "clear", "keyless"}


class ProviderDocumentError(ValueError):
    """settings.json could not be read or saved. The message never includes a key."""


@dataclass(frozen=True)
class ProviderRow:
    """What the settings page is allowed to show. No key, only a mask."""

    name: str
    label: str
    base_url: str
    key_mask: str
    key_source: str
    builtin: bool
    has_overlay: bool
    disk_has_key: bool
    native_tools: bool
    active_account: str | None
    accounts: tuple[str, ...]
    saved_label: str
    error: str | None


def list_providers(
    path: Path,
    *,
    env_files: list[Path] | None = None,
    real_env: dict[str, str] | None = None,
    keys_db: Path | None = None,
) -> list[ProviderRow]:
    """Providers Omni would load from this file, in name order."""
    if not path.is_file():
        return []
    saved = _read(path)
    defaults = load_defaults()
    settings = merge_with_defaults(saved, defaults)
    _migrate(settings, defaults)
    builtin = set((defaults.get("providers") or {}))
    overlay = set((saved.get("providers") or {}) if isinstance(saved.get("providers"), dict) else ())
    if env_files is None:
        home = path.parent
        env_files = [home.parent / ".env", home / ".env"]
    env = read_env_view(list(env_files), real_env)
    from_store: set[str] = set()
    if keys_db is not None:
        from omnibots.omni import keystore
        from_store = keystore.overlay(settings.get("providers") or {}, keystore.read_keys(keys_db))
    rows: list[ProviderRow] = []
    for name in sorted((settings.get("providers") or {}), key=str):
        raw = (settings.get("providers") or {}).get(name)
        if not isinstance(raw, dict):
            continue
        info = _resolve_provider(str(name), raw, env)
        if name in from_store and info.key_source == "settings":
            info.key_source = "vault"
        accounts = raw.get("accounts") if isinstance(raw.get("accounts"), dict) else {}
        active = raw.get("activeAccount") if accounts else None
        overlay_entry = (saved.get("providers") or {}).get(name) if isinstance(saved.get("providers"), dict) else None
        saved_label = str(overlay_entry.get("label") or "") if isinstance(overlay_entry, dict) else ""
        rows.append(ProviderRow(
            name=str(name),
            label=info.label,
            base_url=info.base_url,
            key_mask=mask_key(info.api_key),
            key_source=info.key_source,
            builtin=str(name) in builtin,
            has_overlay=str(name) in overlay,
            disk_has_key=_disk_has_key(raw),
            native_tools=info.native_tools,
            active_account=str(active) if active else None,
            accounts=tuple(str(a) for a in accounts),
            saved_label=saved_label,
            error=info.error,
        ))
    return rows


def apply_provider(
    path: Path,
    *,
    name: str,
    base_url: str,
    label: str = "",
    native_tools: bool = True,
    key_action: str = "keep",
    api_key: str = "",
    active_account: str | None = None,
    keys_db: Path | None = None,
) -> None:
    """Insert or update one provider. `api_key` is ignored unless key_action is "set"."""
    if key_action not in KEY_ACTIONS:
        raise ProviderDocumentError("Unknown key action. Nothing was saved.")
    name = (name or "").strip()
    if not NAME_RE.match(name):
        raise ProviderDocumentError(
            "The name must start with a letter or number and use only letters, numbers, dots, underscores, and hyphens."
        )
    base_url = (base_url or "").strip()
    if not URL_RE.match(base_url):
        raise ProviderDocumentError("The base URL must start with http:// or https://. Nothing was saved.")

    doc = _read(path) if path.is_file() else {}
    providers = doc.get("providers")
    if providers is None:
        providers = {}
        doc["providers"] = providers
    if not isinstance(providers, dict):
        raise ProviderDocumentError("The providers entry is not an object. Nothing was saved.")
    current = providers.get(name)
    if current is None:
        entry: dict[str, Any] = {}
    elif isinstance(current, dict):
        entry = dict(current)
    else:
        raise ProviderDocumentError(f"{name} is not a provider object. Nothing was saved.")

    defaults = (load_defaults().get("providers") or {}).get(name) or {}
    default_accounts = defaults.get("accounts") if isinstance(defaults.get("accounts"), dict) else None
    saved_accounts = entry.get("accounts") if isinstance(entry.get("accounts"), dict) else None
    account_names = saved_accounts if saved_accounts is not None else default_accounts
    # A key pasted into the URL slot (Omni repairs this on load). Keep it when
    # the user fixes the URL and leaves the key box blank.
    old_url = str(entry.get("baseUrl") or "")
    if key_action == "keep" and old_url and not URL_RE.match(old_url):
        held = str(entry.get("apiKey") or "").strip()
        if not held or held == "not-needed":
            entry["apiKey"] = old_url
    entry["baseUrl"] = base_url
    shown = (label or "").strip()
    if shown:
        entry["label"] = shown
    else:
        entry.pop("label", None)
    entry["nativeTools"] = bool(native_tools)

    chosen: str | None = None
    account_changed = False
    if account_names:
        current_active = str(entry.get("activeAccount") or defaults.get("activeAccount") or next(iter(account_names)))
        chosen = (active_account or "").strip() or current_active
        if chosen not in account_names:
            raise ProviderDocumentError(f"There is no account named {chosen}. Nothing was saved.")
        account_changed = chosen != current_active

    if key_action == "set":
        key = (api_key or "").strip()
        if not key:
            raise ProviderDocumentError("Type a key, or leave the box empty to keep the current one. Nothing was saved.")
        if keys_db is not None:
            _store_key(keys_db, name, key, chosen)
            _write_key(entry, "", chosen, saved_accounts, default_accounts)
        else:
            _write_key(entry, key, chosen, saved_accounts, default_accounts)
    elif key_action == "clear":
        if keys_db is not None:
            _drop_key(keys_db, name, chosen)
        _write_key(entry, "", chosen, saved_accounts, default_accounts)
    elif key_action == "keyless":
        if keys_db is not None:
            _drop_key(keys_db, name, chosen)
        _write_key(entry, "not-needed", chosen, saved_accounts, default_accounts)
    elif account_changed and chosen is not None:
        _switch_account(entry, chosen, saved_accounts, default_accounts)
    if chosen is not None and (key_action != "keep" or account_changed):
        entry["activeAccount"] = chosen
    providers[name] = entry
    _commit(path, doc)


def clear_provider_key(path: Path, name: str, *, keys_db: Path | None = None) -> None:
    """Blank the saved key (and the active account's key) without rewriting the rest of the provider."""
    name = (name or "").strip()
    if keys_db is not None:
        _drop_key(keys_db, name, _active_account(path, name))
    doc = _read(path) if path.is_file() else {}
    providers = doc.get("providers")
    entry = providers.get(name) if isinstance(providers, dict) else None
    if not isinstance(entry, dict):
        if name in (load_defaults().get("providers") or {}):
            return
        raise ProviderDocumentError(f"There is no provider named {name}.")
    defaults = (load_defaults().get("providers") or {}).get(name) or {}
    default_accounts = defaults.get("accounts") if isinstance(defaults.get("accounts"), dict) else None
    saved_accounts = entry.get("accounts") if isinstance(entry.get("accounts"), dict) else None
    account_names = saved_accounts if saved_accounts is not None else default_accounts
    account = None
    if account_names:
        account = str(entry.get("activeAccount") or defaults.get("activeAccount") or next(iter(account_names)))
        if account not in account_names:
            account = next(iter(account_names))
    updated = dict(entry)
    _write_key(updated, "", account, saved_accounts, default_accounts)
    if account is not None:
        updated["activeAccount"] = account
    providers[name] = updated
    _commit(path, doc)


def delete_provider(path: Path, name: str, *, keys_db: Path | None = None) -> str:
    """Drop a provider you added, or forget your edits to one Omni ships with.

    Returns "removed" or "reset". A built-in provider comes back from Omni's
    defaults on the next load; deleting it only removes the saved overlay.
    """
    name = (name or "").strip()
    doc = _read(path) if path.is_file() else {}
    providers = doc.get("providers")
    builtin = name in ((load_defaults().get("providers") or {}))
    if keys_db is not None:
        from omnibots.omni import keystore
        keystore.delete_key(keys_db, name)
    if not isinstance(providers, dict) or name not in providers:
        if builtin:
            return "reset"
        raise ProviderDocumentError(f"There is no provider named {name}.")
    del providers[name]
    _commit(path, doc)
    return "reset" if builtin else "removed"


def _store_key(keys_db: Path, name: str, key: str, account: str | None) -> None:
    from omnibots.omni import keystore
    try:
        keystore.set_key(keys_db, name, key, account or "")
    except keystore.KeystoreError as exc:
        raise ProviderDocumentError(f"The key could not be stored ({exc}). Nothing was saved.") from exc


def _drop_key(keys_db: Path, name: str, account: str | None) -> None:
    from omnibots.omni import keystore
    keystore.delete_key(keys_db, name, account or "")


def _active_account(path: Path, name: str) -> str | None:
    """The account a key action applies to, as apply_provider/clear_provider_key choose it."""
    doc = _read(path) if path.is_file() else {}
    entry = (doc.get("providers") or {}).get(name) if isinstance(doc.get("providers"), dict) else None
    entry = entry if isinstance(entry, dict) else {}
    defaults = (load_defaults().get("providers") or {}).get(name) or {}
    accounts = entry.get("accounts") if isinstance(entry.get("accounts"), dict) else defaults.get("accounts")
    if not isinstance(accounts, dict) or not accounts:
        return None
    active = str(entry.get("activeAccount") or defaults.get("activeAccount") or next(iter(accounts)))
    return active if active in accounts else next(iter(accounts))


def _disk_has_key(provider: dict[str, Any]) -> bool:
    if str(provider.get("apiKey") or "").strip():
        return True
    accounts = provider.get("accounts") if isinstance(provider.get("accounts"), dict) else {}
    active = provider.get("activeAccount")
    return bool(active and str(accounts.get(active) or "").strip())


def _account_map(
    saved: dict[str, Any] | None,
    defaults: dict[str, Any] | None,
) -> dict[str, Any]:
    # Omni merges a provider shallowly, so a saved `accounts` object replaces
    # the default one. Start from the saved map when it exists; otherwise copy
    # the default names (empty keys, never values taken from the environment).
    if saved is not None:
        return dict(saved)
    return {str(k): ("" if v is None else v) for k, v in (defaults or {}).items()}


def _write_key(
    entry: dict[str, Any],
    key: str,
    account: str | None,
    saved_accounts: dict[str, Any] | None,
    default_accounts: dict[str, Any] | None,
) -> None:
    entry["apiKey"] = key
    if account is None:
        return
    accounts = _account_map(saved_accounts, default_accounts)
    accounts[account] = key
    entry["accounts"] = accounts


def _switch_account(
    entry: dict[str, Any],
    account: str,
    saved_accounts: dict[str, Any] | None,
    default_accounts: dict[str, Any] | None,
) -> None:
    """Match Omni's activateAccount: mirror a stored key, or adopt the provider key into an empty account."""
    accounts = _account_map(saved_accounts, default_accounts)
    held = str(accounts.get(account) or "").strip()
    if held:
        entry["apiKey"] = accounts[account]
    else:
        own = str(entry.get("apiKey") or "").strip()
        if own:
            accounts[account] = entry["apiKey"]
    entry["accounts"] = accounts
    entry["activeAccount"] = account


def _read(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProviderDocumentError(f"Could not read {path.name}. Nothing was saved.") from exc
    try:
        stripped = re.sub(r"(?m)^\s*//.*$", "", raw)
        doc = json.loads(stripped) if stripped.strip() else {}
    except json.JSONDecodeError as exc:
        raise ProviderDocumentError(
            "Omni's settings.json is not valid JSON, so nothing was changed. Fix that file before saving."
        ) from exc
    if not isinstance(doc, dict):
        raise ProviderDocumentError("Omni's settings.json is not a JSON object, so nothing was changed.")
    return doc


def _commit(path: Path, doc: dict[str, Any]) -> None:
    # No trailing newline: that is how Omni's JSON.stringify writes the file,
    # and a save that changes nothing else then stays byte-for-byte.
    payload = json.dumps(doc, indent=2, ensure_ascii=False)
    if path.is_file() and path.read_text(encoding="utf-8") == payload:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".omnibots-tmp")
    try:
        tmp.write_text(payload, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
