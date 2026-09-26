"""Read Omni's providers, keys, models, skills and MCP servers, read-only.

Providers and keys follow Omni's exact rules (src/core/config.mjs):
  - settings.json: whole-line // comments stripped, then JSON.
  - .env files: <install>/.env then <home>/.env; a real environment variable
    is never overridden by a file; OMNI_HOME in a file is ignored.
  - Key precedence: a NON-EMPTY key in settings.json wins; OMNI_<NAME>_KEY
    (non-alphanumerics -> "_", e.g. minimax.io -> OMNI_MINIMAX_IO_KEY) only
    fills an empty slot. Aliases: ATRIA_API_KEY (atria), OMNI_MINIMAX_KEY
    (minimax.io), OMNI_AGNES_KEY2 (agnes2 account).
  - Providers with `accounts`: account keys follow the same rule (per account
    env var); an empty first account is seeded from apiKey; the active
    account's key (when non-empty) becomes the provider key. One account per
    provider for OmniBots: only the active one is used (PLAN.md scope).
Omni layers its built-in DEFAULT_SETTINGS under the saved file on every load;
OmniBots does the same from omni_defaults.json, a snapshot generated from
Omni's own code by tools/omni_defaults_snapshot.mjs (a test fails when the
snapshot drifts from the installed Omni). Only the migrations that change how
a provider is called are ported (nvidia text tool protocol, baseUrl repair,
the MiniMax fold and output cap, known vision models). The parity test (tests/test_a1_omni_parity.py) runs Omni's own loader and fails if
anything OmniBots uses differs, which is how drift gets caught.

Keys are held in memory only, registered with the log redactor, and shown
masked. Nothing here writes to Omni.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omnibots.logging_setup import register_secret
from omnibots.omni.frontmatter import parse_frontmatter
from omnibots.omni.locate import OmniLocation

log = logging.getLogger(__name__)

PROVIDER_ENV_ALIASES = {"atria": ["ATRIA_API_KEY"]}
MINIMAX = "minimax.io"
MINIMAX_LEGACY = "minimax"
MINIMAX_MAX_OUTPUT = 524288
MINIMAX_SAFE_OUTPUT = 128000
KNOWN_VISION_MODEL_IDS = [re.compile(p, re.I) for p in (r"^gpt-4\.1(-mini|-nano)?$", r"^gpt-4o(-mini)?$", r"^o4-mini$", r"^o3(-mini)?$")]
KEYLESS = {"not-needed", ""}
DEFAULTS_FILE = Path(__file__).with_name("omni_defaults.json")


def load_defaults() -> dict[str, Any]:
    """Omni's DEFAULT_SETTINGS snapshot (providers with blank keys, models)."""
    return json.loads(DEFAULTS_FILE.read_text(encoding="utf-8"))


def merge_with_defaults(saved: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    """Omni's loadSettings merge: defaults under saved; providers merged per
    provider (saved fields win), models merged per key (saved entry wins)."""
    settings = {"defaultProvider": defaults.get("defaultProvider"), "defaultModel": defaults.get("defaultModel")}
    settings.update(saved)
    providers = copy.deepcopy(defaults.get("providers") or {})
    for name, p in (saved.get("providers") or {}).items():
        providers[name] = {**(providers.get(name) or {}), **(p or {})}
    settings["providers"] = providers
    settings["models"] = {**copy.deepcopy(defaults.get("models") or {}), **(saved.get("models") or {})}
    return settings


def provider_key_env_var(name: str) -> str:
    return "OMNI_" + re.sub(r"[^A-Z0-9]", "_", str(name).upper()) + "_KEY"


def provider_key_env_vars(name: str) -> list[str]:
    return [provider_key_env_var(name), *PROVIDER_ENV_ALIASES.get(str(name).lower(), [])]


def key_fingerprint(key: str | None) -> str:
    """A short, non-reversible id for comparing keys without showing them."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16] if key else ""


def mask_key(key: str | None) -> str:
    if not key:
        return "(none)"
    if key == "not-needed":
        return "(keyless)"
    return f"{key[:4]}…{key[-4:]}" if len(key) > 12 else "set"


# ── .env ──────────────────────────────────────────────────────────────────
def read_env_view(files: list[Path], real_env: dict[str, str] | None = None) -> dict[str, str]:
    """The environment Omni would see: the real env, plus .env values for
    names the real env doesn't have. Never mutates os.environ."""
    env = dict(os.environ if real_env is None else real_env)
    for f in files:
        try:
            raw = f.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in raw.splitlines():
            t = line.strip()
            if not t or t.startswith("#") or "=" not in t:
                continue
            key, _, val = t.partition("=")
            key, val = key.strip(), val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            if key == "OMNI_HOME":   # Omni: OMNI_HOME is shell-only
                continue
            if key and key not in env:
                env[key] = val
    # Back-compat aliases Omni applies before reading keys.
    if not env.get("OMNI_AGNES2_KEY") and env.get("OMNI_AGNES_KEY2"):
        env["OMNI_AGNES2_KEY"] = env["OMNI_AGNES_KEY2"]
    if not env.get("OMNI_MINIMAX_IO_KEY") and env.get("OMNI_MINIMAX_KEY"):
        env["OMNI_MINIMAX_IO_KEY"] = env["OMNI_MINIMAX_KEY"]
    return env


def _env_key(env: dict[str, str], name: str) -> str | None:
    for var in provider_key_env_vars(name):
        val = env.get(var)
        if val and str(val).strip():
            return val
    return None


# ── data ─────────────────────────────────────────────────────────────────
@dataclass
class ProviderInfo:
    name: str
    base_url: str
    api_key: str = field(default="", repr=False)
    key_source: str = "none"            # settings | env | account:<name> | keyless | none
    label: str = ""
    native_tools: bool = True
    reasoning_param: str | None = None
    api: str | None = None
    active_account: str | None = None
    accounts: list[str] = field(default_factory=list)
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def has_key(self) -> bool:
        return bool(self.api_key) and self.api_key not in KEYLESS or self.key_source == "keyless"

    @property
    def key_hash(self) -> str:
        return key_fingerprint(self.api_key)

    def public(self) -> dict[str, Any]:
        """Safe to log, show or send over IPC: no key, only a mask."""
        return {
            "name": self.name, "label": self.label, "base_url": self.base_url,
            "key": mask_key(self.api_key), "key_source": self.key_source,
            "native_tools": self.native_tools, "reasoning_param": self.reasoning_param,
            "active_account": self.active_account, "error": self.error,
        }


@dataclass
class SkillInfo:
    name: str
    command: str
    description: str
    path: Path
    source: str                          # bundled | external
    category: str = ""

    def body(self) -> str:
        return parse_frontmatter(self.path.read_text(encoding="utf-8"))[1]


@dataclass
class OmniConfig:
    location: OmniLocation
    providers: dict[str, ProviderInfo]
    models: dict[str, dict[str, Any]]
    default_provider: str | None
    default_model: str | None
    skills: list[SkillInfo]
    mcp_servers: dict[str, dict[str, Any]]
    errors: list[str] = field(default_factory=list)
    reasoning: str = "medium"            # Omni settings.reasoning (effort tier)

    def provider(self, name: str) -> ProviderInfo | None:
        return self.providers.get(name)

    def models_for(self, provider: str) -> dict[str, dict[str, Any]]:
        return {k: v for k, v in self.models.items() if v.get("provider") == provider}

    def summary(self, targets: list[str] | None = None) -> dict[str, Any]:
        names = targets or sorted(self.providers)
        return {
            "omni_install_root": str(self.location.install_root),
            "omni_home": str(self.location.home),
            "providers": {n: self.providers[n].public() for n in names if n in self.providers},
            "missing_providers": [n for n in names if n not in self.providers],
            "models": len(self.models),
            "skills": {"bundled": sum(s.source == "bundled" for s in self.skills),
                       "external": sum(s.source == "external" for s in self.skills)},
            "mcp_servers": sorted(self.mcp_servers),
            "errors": self.errors,
        }


# ── settings.json ─────────────────────────────────────────────────────────
def read_settings_json(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    stripped = re.sub(r"(?m)^\s*//.*$", "", raw)
    return json.loads(stripped)


def _migrate(settings: dict[str, Any], defaults: dict[str, Any]) -> None:
    """The subset of Omni's migrateSettings that changes provider calls."""
    providers = settings.setdefault("providers", {})
    models = settings.setdefault("models", {})
    for entry in models.values():
        if isinstance(entry, dict) and "vision" not in entry and any(r.search(str(entry.get("id", ""))) for r in KNOWN_VISION_MODEL_IDS):
            entry["vision"] = True
    nv = providers.get("nvidia")
    if isinstance(nv, dict):
        nv["nativeTools"] = False
        if not nv.get("api"):
            nv["api"] = "openai-completions"
        nv.pop("reasoningParam", None)
    # A key pasted into the baseUrl slot: restore the default URL and keep the key.
    for name, p in providers.items():
        if isinstance(p, dict) and p.get("baseUrl") and not re.match(r"^https?://", str(p["baseUrl"]), re.I):
            default_url = ((defaults.get("providers") or {}).get(name) or {}).get("baseUrl")
            if default_url:
                if not p.get("apiKey") or p.get("apiKey") == "not-needed":
                    p["apiKey"] = p["baseUrl"]
                p["baseUrl"] = default_url
    # Fold the legacy "minimax" provider into "minimax.io".
    for key in [k for k, e in models.items() if isinstance(e, dict) and e.get("provider") == MINIMAX_LEGACY]:
        entry = models.pop(key)
        if re.match(r"^(sk-|nvapi-|gsk_|xai-|hf_)|^[A-Za-z0-9_-]{48,}$", key):
            continue
        suffix = key[len(MINIMAX_LEGACY) + 1:] if key.startswith(MINIMAX_LEGACY + "/") else str(entry.get("id", key))
        moved = f"{MINIMAX}/{suffix}"
        models.setdefault(moved, {**entry, "provider": MINIMAX})
        if settings.get("defaultModel") == key:
            settings["defaultModel"] = moved
    if settings.get("defaultProvider") == MINIMAX_LEGACY:
        settings["defaultProvider"] = MINIMAX
    legacy = providers.pop(MINIMAX_LEGACY, None)
    if isinstance(legacy, dict):
        canonical = providers.setdefault(MINIMAX, copy.deepcopy((defaults.get("providers") or {}).get(MINIMAX) or {"baseUrl": "https://api.minimax.io/v1", "apiKey": ""}))
        if not str(canonical.get("apiKey") or "").strip() and str(legacy.get("apiKey") or "").strip():
            canonical["apiKey"] = legacy["apiKey"]
    for entry in models.values():
        if isinstance(entry, dict) and entry.get("provider") == MINIMAX:
            try:
                if float(entry.get("maxTokens") or 0) > MINIMAX_MAX_OUTPUT:
                    entry["maxTokens"] = MINIMAX_SAFE_OUTPUT
            except (TypeError, ValueError):
                pass


def _resolve_provider(name: str, p: dict[str, Any], env: dict[str, str]) -> ProviderInfo:
    on_disk = str(p.get("apiKey") or "").strip()
    env_val = _env_key(env, name)
    api_key, source = (on_disk, "settings") if on_disk else ((env_val, "env") if env_val else ("", "none"))

    accounts = p.get("accounts") if isinstance(p.get("accounts"), dict) else None
    active = p.get("activeAccount")
    if accounts is not None:
        accounts = dict(accounts)
        acct_source: dict[str, str] = {}
        for acct in accounts:
            acct_disk = str(accounts[acct] or "").strip()
            acct_env = env.get(provider_key_env_var(acct))
            if acct_disk:
                acct_source[acct] = "settings"
            elif acct_env:
                accounts[acct] = acct_env
                acct_source[acct] = "env"
        if accounts:
            first = next(iter(accounts))
            if not str(accounts[first] or "").strip() and api_key:
                accounts[first] = api_key
                acct_source[first] = source
        if active in accounts and str(accounts[active] or "").strip():
            api_key = accounts[active]
            source = f"account:{active}"

    info = ProviderInfo(
        name=name,
        base_url=str(p.get("baseUrl") or ""),
        api_key=api_key,
        key_source=source,
        label=str(p.get("label") or name),
        native_tools=p.get("nativeTools") is not False,
        reasoning_param=p.get("reasoningParam"),
        api=p.get("api"),
        active_account=active if accounts is not None else None,
        accounts=list(accounts) if accounts else [],
        raw={k: v for k, v in p.items() if k not in ("apiKey", "accounts")},
    )
    if api_key == "not-needed":
        info.key_source = "keyless"
    if not re.match(r"^https?://", info.base_url, re.I):
        info.error = f"invalid baseUrl {info.base_url!r} (must start with http:// or https://); fix it in Omni"
    elif accounts is not None and active and not str(accounts.get(active) or "").strip() and not api_key:
        info.error = f"active account {active!r} has no key; set it in Omni (/apikey {active} <key>)"
    if api_key and api_key not in KEYLESS:
        register_secret(api_key)
    for v in (accounts or {}).values():
        if v and str(v) not in KEYLESS:
            register_secret(str(v))
    return info


# ── skills ────────────────────────────────────────────────────────────────
def _skill_from_dir(directory: Path, source: str, category: str = "", fallback_name: str | None = None) -> SkillInfo | None:
    f = directory / "SKILL.md"
    try:
        meta, _ = parse_frontmatter(f.read_text(encoding="utf-8"))
    except OSError:
        return None
    name = meta.get("name") or fallback_name or directory.name
    return SkillInfo(
        name=name,
        command=meta.get("command") or "/" + name if source == "bundled" else "/" + name,
        description=meta.get("description", ""),
        path=f,
        source=source,
        category=category,
    )


def load_skills(loc: OmniLocation, project: dict[str, Any]) -> list[SkillInfo]:
    """Bundled skills (Omni's loadSkills) + the external index (loadScannedSkills)."""
    root = loc.install_root
    entries: list[Path] = []
    for e in project.get("skills") or []:
        p = Path(e) if os.path.isabs(e) else root / e
        entries.append(p.parent if p.name == "SKILL.md" else p)
    if project.get("autoDiscoverSkills"):
        skills_root = root / "skills"
        if skills_root.is_dir():
            entries += sorted(p.parent for p in skills_root.rglob("SKILL.md"))
    by_command: dict[str, SkillInfo] = {}
    for d in entries:
        rel = d.relative_to(root).as_posix() if d.is_relative_to(root) else ""
        cat = rel.split("/")[1].replace("-", " ").title() if rel.startswith("skills/") and len(rel.split("/")) > 1 else "Process Skills"
        s = _skill_from_dir(d, "bundled", cat)
        if s:
            by_command[s.command] = s          # last wins, like Omni
    bundled = list(by_command.values())

    external: list[SkillInfo] = []
    index = project.get("skillIndex")
    if isinstance(index, str) and index.strip():
        try:
            data = json.loads(Path(index.strip()).read_text(encoding="utf-8"))
            items = list(data.get("entries") or []) + list(data.get("nested") or [])
        except (OSError, ValueError):
            items = []
        taken = {s.command.lower() for s in bundled}
        seen: set[str] = set()
        for item in items:
            path = str(item.get("path") or "")
            if not path:
                continue
            cat = f"External · {item['parentPack']}" if item.get("parentPack") else "External"
            s = _skill_from_dir(Path(path), "external", cat, fallback_name=item.get("name"))
            if not s:
                continue
            cmd = s.command.lower()
            if cmd in taken or cmd in seen:
                continue
            seen.add(cmd)
            external.append(s)
    return bundled + external


# ── MCP ───────────────────────────────────────────────────────────────────
def _expand(value: Any, root: Path) -> Any:
    if isinstance(value, str):
        return value.replace("{{INSTALL_ROOT}}", str(root))
    if isinstance(value, list):
        return [_expand(v, root) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v, root) for k, v in value.items()}
    return value


def load_mcp_servers(loc: OmniLocation, project: dict[str, Any], workspace: Path | None = None) -> dict[str, dict[str, Any]]:
    servers = dict(project.get("mcpServers") or {})
    if workspace is not None:
        try:
            dot = json.loads((workspace / ".mcp.json").read_text(encoding="utf-8"))
            servers.update(dot.get("mcpServers") or {})
        except (OSError, ValueError):
            pass
    return {name: _expand(cfg, loc.install_root) for name, cfg in servers.items()}


# ── entry point ───────────────────────────────────────────────────────────
def load_omni_config(loc: OmniLocation, *, real_env: dict[str, str] | None = None, workspace: Path | None = None) -> OmniConfig:
    errors: list[str] = []
    env = read_env_view(loc.env_files, real_env)
    defaults = load_defaults()
    try:
        saved = read_settings_json(loc.settings_file)
    except FileNotFoundError:
        saved = {}
        errors.append(f"Omni settings not found at {loc.settings_file}. Run Omni once to create it.")
    except ValueError as exc:
        saved = {}
        errors.append(f"Omni settings at {loc.settings_file} are not valid JSON ({exc}). Fix them in Omni; OmniBots will not guess.")
    settings = merge_with_defaults(saved, defaults)
    _migrate(settings, defaults)

    providers = {}
    for name, p in (settings.get("providers") or {}).items():
        if isinstance(p, dict):
            info = _resolve_provider(name, p, env)
            providers[name] = info
            if info.error:
                errors.append(f"provider {name}: {info.error}")

    try:
        project = json.loads(loc.project_config_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        project = {}
        errors.append(f"could not read {loc.project_config_file}: {exc}")

    return OmniConfig(
        location=loc,
        providers=providers,
        models={k: v for k, v in (settings.get("models") or {}).items() if isinstance(v, dict)},
        default_provider=settings.get("defaultProvider"),
        default_model=settings.get("defaultModel"),
        skills=load_skills(loc, project),
        mcp_servers=load_mcp_servers(loc, project, workspace),
        errors=errors,
        reasoning=str(settings.get("reasoning") or "medium"),
    )
