"""Checks and repairs, driven by layout.json (see README.md).

Rules the doctor keeps:
  - It creates what's missing and upgrades what's old; it never deletes a file or a folder.
    Anything in the wrong place is reported with what to do, and left alone.
  - It never changes Omni's files (Omni's own /doctor looks after those). It only reads them.
  - A key is never printed, logged or returned, only "set" or masked.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

LAYOUT_FILE = Path(__file__).with_name("layout.json")
CODE_ROOT = Path(__file__).resolve().parents[2]          # the checkout: <root>/omnibots/doctor/doctor.py
GROUPS = ("python", "folders", "settings", "database", "code", "omni", "providers", "env", "legacy")
STARTUP_GROUPS = ("folders", "settings", "omni")          # quick, offline, safe while the app starts
KEYISH = re.compile(r"(api_?key|token|secret|password)", re.I)
STATUS_ORDER = {"ok": 0, "fixed": 1, "warn": 2, "fail": 3}


@dataclass
class Finding:
    id: str
    group: str
    status: str                 # ok | fixed | warn | fail
    message: str
    hint: str = ""


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    fix: bool = True
    started: float = field(default_factory=time.time)
    mode: str = ""              # "omni" | "standalone" | ""

    def add(self, id: str, group: str, status: str, message: str, hint: str = "") -> Finding:
        f = Finding(id, group, status, message, hint)
        self.findings.append(f)
        return f

    @property
    def worst(self) -> str:
        return max((f.status for f in self.findings), key=STATUS_ORDER.__getitem__, default="ok")

    def counts(self) -> dict[str, int]:
        out = {k: 0 for k in STATUS_ORDER}
        for f in self.findings:
            out[f.status] += 1
        return out

    def problems(self) -> list[Finding]:
        return [f for f in self.findings if f.status in ("warn", "fail", "fixed")]

    def to_dict(self) -> dict[str, Any]:
        return {"worst": self.worst, "counts": self.counts(), "mode": self.mode, "fix": self.fix,
                "at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.started)),
                "findings": [asdict(f) for f in self.findings]}

    def text(self, *, all_lines: bool = False) -> str:
        mark = {"ok": "OK   ", "fixed": "FIXED", "warn": "WARN ", "fail": "FAIL "}
        c = self.counts()
        head = (f"OmniBots doctor: {c['ok']} ok, {c['fixed']} fixed, {c['warn']} warnings, {c['fail']} problems"
                + (f" · providers from {'Omni' if self.mode == 'omni' else 'OmniBots’ own config'}" if self.mode else ""))
        lines = [head]
        for f in self.findings:
            if all_lines or f.status != "ok":
                lines.append(f"  {mark[f.status]} [{f.group}] {f.message}" + (f"\n         → {f.hint}" if f.hint else ""))
        if not all_lines and not self.problems():
            lines.append("  Everything checks out.")
        return "\n".join(lines)


def load_layout() -> dict[str, Any]:
    return json.loads(LAYOUT_FILE.read_text(encoding="utf-8"))


# ── context ──────────────────────────────────────────────────────────────
@dataclass
class Ctx:
    home: Path
    env: dict[str, str]
    settings: dict[str, Any]
    omni_root: Path | None           # Omni's install folder when Omni is installed
    omni_home: Path | None
    tokens: dict[str, str]
    configured_root: str = ""        # [omni] install_root (or the engine's), as locate_omni gets it

    def path(self, template: str) -> Path | None:
        """Expand {tokens}. None when a token has no value (e.g. {output} not chosen yet)."""
        out = template
        for name in re.findall(r"\{(\w+)\}", template):
            val = self.tokens.get(name, "")
            if not val:
                return None
            out = out.replace("{" + name + "}", val)
        return Path(out)


def _settings(home: Path) -> dict[str, Any]:
    try:
        return tomllib.loads((home / "settings.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def make_ctx(home: Path | None = None, env: dict[str, str] | None = None, omni_root: str | None = None) -> Ctx:
    from omnibots.omni.locate import OmniNotFound, locate_omni
    from omnibots.paths import resolve_home
    env = dict(os.environ if env is None else env)
    home = Path(home) if home else resolve_home()
    settings = _settings(home)
    root = omni_root if omni_root is not None else str((settings.get("omni") or {}).get("install_root") or "")
    try:
        loc = locate_omni(root)
        omni_root_path, omni_home = loc.install_root, loc.home
    except OmniNotFound:
        omni_root_path = omni_home = None
    user = Path(env.get("USERPROFILE") or Path.home())
    local = env.get("LOCALAPPDATA") or str(user / "AppData" / "Local")
    roaming = env.get("APPDATA") or str(user / "AppData" / "Roaming")
    ctx_root = root
    tokens = {
        "home": str(home), "code": str(CODE_ROOT), "config": str(home / "config"),
        "omni": str(omni_root_path or user / ".omni"), "omni_home": str(omni_home or ""),
        "appdata": str(Path(local) / "OmniBots"), "localappdata": local, "roaming": roaming,
        "userprofile": str(user), "output": str((settings.get("output") or {}).get("folder") or ""),
    }
    return Ctx(home, env, settings, omni_root_path, omni_home, tokens, ctx_root)


def _same(a: Path | str | None, b: Path | str | None) -> bool:
    if not a or not b:
        return False
    try:
        return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))
    except OSError:
        return False


# ── checks ───────────────────────────────────────────────────────────────
def check_python(ctx: Ctx, lay: dict, rep: Report, *, install_deps: bool = False) -> None:
    need = tuple(int(x) for x in lay["python"]["min"].split("."))
    have = sys.version_info[:2]
    if have >= need:
        rep.add("python.version", "python", "ok", f"Python {have[0]}.{have[1]} ({sys.executable})")
    else:
        rep.add("python.version", "python", "fail", f"Python {have[0]}.{have[1]} is too old (needs {lay['python']['min']}+)",
                "Install Python 3.12 or newer, then run the OmniBots installer again")
    missing = []
    for pkg in lay["packages"]:
        if importlib.util.find_spec(pkg["module"]) is not None:
            rep.add(f"package.{pkg['module']}", "python", "ok", f"{pkg['module']} is installed ({pkg['why']})")
        else:
            missing.append(pkg)
    if missing and install_deps:
        cmd = [sys.executable, "-m", "pip", "install", "--quiet", *[p["pip"] for p in missing]]
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=900).returncode == 0
        importlib.invalidate_caches()
        for pkg in missing:
            ok = done and importlib.util.find_spec(pkg["module"]) is not None
            rep.add(f"package.{pkg['module']}", "python", "fixed" if ok else "fail",
                    f"{pkg['module']} {'was installed' if ok else 'could not be installed'} ({pkg['why']})",
                    "" if ok else f"Run: {sys.executable} -m pip install {pkg['pip']}")
    else:
        for pkg in missing:
            rep.add(f"package.{pkg['module']}", "python", pkg.get("severity", "fail"),
                    f"{pkg['module']} is missing ({pkg['why']})",
                    f"Run the doctor with --install-deps, or: {sys.executable} -m pip install {pkg['pip']}")
    for prog in lay["programs"]:
        found = shutil.which(prog["name"])
        rep.add(f"program.{prog['name']}", "python", "ok" if found else prog.get("severity", "warn"),
                f"{prog['name']} {'found at ' + found if found else 'is not on PATH'} ({prog['why']})",
                "" if found else f"Install {prog['name']} (winget install Git.Git)")


def check_folders(ctx: Ctx, lay: dict, rep: Report) -> None:
    for item in lay["folders"]:
        p = ctx.path(item["path"])
        if p is None:
            if item.get("optional_when_unset"):
                rep.add(item["id"], "folders", "ok", f"{item['why']}: not chosen yet, OmniBots asks on its next start")
            continue
        if p.is_dir():
            rep.add(item["id"], "folders", "ok", f"{p} ({item['why']})")
        elif p.exists():
            rep.add(item["id"], "folders", "fail", f"{p} is a file, but it must be a folder ({item['why']})",
                    "Rename or move that file, then run the doctor again")
        elif rep.fix and item.get("fix"):
            try:
                p.mkdir(parents=True, exist_ok=True)
                rep.add(item["id"], "folders", "fixed", f"created {p} ({item['why']})")
            except OSError as exc:
                rep.add(item["id"], "folders", "fail", f"could not create {p}: {exc.strerror}", "Check the folder's permissions")
        else:
            rep.add(item["id"], "folders", "fail", f"{p} is missing ({item['why']})", "Run the doctor with fixing on")
    out = ctx.path("{output}")
    if out is not None and out.is_dir():
        probe = out / ".omnibots-doctor-probe"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError:
            rep.add("output.writable", "folders", "fail", f"the projects folder {out} can't be written to",
                    "Pick another folder in Settings → Folders")


def check_settings(ctx: Ctx, lay: dict, rep: Report) -> None:
    from omnibots.settings import DEFAULT_SETTINGS_TOML
    f = ctx.home / "settings.toml"
    if not f.exists():
        if rep.fix:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(DEFAULT_SETTINGS_TOML, encoding="utf-8")
            ctx.settings = _settings(ctx.home)
            rep.add("home.settings", "settings", "fixed", f"created {f} with the default settings")
        else:
            rep.add("home.settings", "settings", "fail", f"{f} is missing")
        return
    try:
        doc = tomllib.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        rep.add("home.settings", "settings", "fail", f"{f} can't be read ({exc})",
                "Fix the line it names (the doctor never overwrites your settings)")
        return
    rep.add("home.settings", "settings", "ok", f"{f} reads fine")
    leaks = []

    def walk(d: dict, prefix: str = "") -> None:
        for k, v in d.items():
            if isinstance(v, dict):
                walk(v, f"{prefix}{k}.")
            elif KEYISH.search(k) and isinstance(v, str) and v.strip():
                leaks.append(prefix + k)
    walk(doc)
    if leaks:
        rep.add("home.settings.keys", "settings", "warn", f"settings.toml holds what looks like a secret: {', '.join(leaks)}",
                "Provider keys go in Settings → Providers; other secrets in the vault (python -m omnibots.security.vault set <name>)")


def check_database(ctx: Ctx, lay: dict, rep: Report) -> None:
    from omnibots.db.database import apply_migrations, list_migrations, open_connection
    f = ctx.home / "db" / "omnibots.sqlite"
    newest = len(list_migrations())
    if not f.is_file():
        if rep.fix:
            f.parent.mkdir(parents=True, exist_ok=True)
            conn = open_connection(f)
            try:
                apply_migrations(conn)
            finally:
                conn.close()
            rep.add("home.database", "database", "fixed", f"created the database at {f} (schema v{newest})")
        else:
            rep.add("home.database", "database", "fail", f"{f} is missing", "Start OmniBots once, or run the doctor")
        return
    try:
        conn = open_connection(f)
    except sqlite3.Error as exc:
        rep.add("home.database", "database", "fail", f"the database can't be opened ({exc})",
                "Restore a backup: tray → Backups → Restore…")
        return
    try:
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        if current > newest:
            rep.add("home.database", "database", "fail", f"the database is schema v{current}, newer than this OmniBots (v{newest})",
                    "Update OmniBots (run the install line again)")
            return
        if current < newest:
            if rep.fix:
                from omnibots import backup
                dest = backup.new_backup_folder(ctx.home, "before-migration")
                backup.copy_db(conn, dest / backup.DB_REL)
                backup.finish_backup(ctx.home, dest, "before-migration", 7)
                apply_migrations(conn)
                rep.add("home.database", "database", "fixed", f"upgraded the database v{current} → v{newest} (backup in {dest})")
            else:
                rep.add("home.database", "database", "warn", f"the database is schema v{current}; this OmniBots uses v{newest}",
                        "Start OmniBots, or run the doctor with fixing on")
        else:
            rep.add("home.database", "database", "ok", f"{f} (schema v{current})")
        check = conn.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok":
            rep.add("home.database.integrity", "database", "fail", f"the database is damaged ({check})",
                    "Restore a backup: tray → Backups → Restore…")
    finally:
        conn.close()


def check_code(ctx: Ctx, lay: dict, rep: Report) -> None:
    for item in lay["files"]:
        if not item["path"].startswith("{code}"):
            continue
        p = ctx.path(item["path"])
        ok = p is not None and p.is_file()
        rep.add(item["id"], "code", "ok" if ok else item.get("severity", "fail"),
                f"{p} {'is there' if ok else 'is missing'} ({item['why']})",
                "" if ok else "Run the OmniBots install line again")


def check_omni(ctx: Ctx, lay: dict, rep: Report) -> Any:
    """Returns the OmniLocation the app will use (Omni's, or the standalone config)."""
    from omnibots.omni.locate import standalone_location
    if ctx.omni_root is not None:
        rep.mode = "omni"
        rep.add("omni.found", "omni", "ok", f"Omni is installed at {ctx.omni_root}: both apps share its providers and keys")
        for item in lay["omni"]["files"]:
            p = ctx.path(item["path"])
            ok = p is not None and p.is_file()
            rep.add(item["id"], "omni", "ok" if ok else item.get("severity", "fail"),
                    f"{p} {'is there' if ok else 'is missing'} ({item['why']})",
                    "" if ok else "Run Omni once (or its installer); the doctor doesn't change Omni's files")
            if ok and p.suffix == ".json":
                try:
                    json.loads(re.sub(r"(?m)^\s*//.*$", "", p.read_text(encoding="utf-8")) or "{}")
                except (OSError, ValueError):
                    rep.add(item["id"] + ".json", "omni", "fail", f"{p} is not valid JSON", "Fix it in Omni (/doctor in Omni)")
        try:
            cfg = json.loads((ctx.omni_root / "omni.config.json").read_text(encoding="utf-8"))
            listed = lay["omni"]["extension_listed"] in (cfg.get("extensions") or [])
            rep.add("omni.launcher.listed", "omni", "ok" if listed else "warn",
                    "Omni loads the OmniBots launcher (/omnibots works)" if listed
                    else "Omni's omni.config.json doesn't list extensions/omnibots-launcher.js, so /omnibots won't work in Omni",
                    "" if listed else "Update Omni (3.5.10 or later), or add it to \"extensions\" in Omni's omni.config.json")
        except (OSError, ValueError):
            pass
        standalone = standalone_location(ctx.home)
        if standalone.settings_file.is_file():
            rep.add("config.unused", "omni", "ok",
                    f"OmniBots' own provider config in {standalone.home} is kept but not used while Omni is installed")
        from omnibots.omni.locate import locate_omni
        return locate_omni(ctx.configured_root)

    rep.mode = "standalone"
    loc = standalone_location(ctx.home)
    rep.add("omni.found", "omni", "ok",
            f"Omni isn't installed: OmniBots uses its own provider config in {loc.home} (Omni's format, keys encrypted in its database)",
            "Install Omni first if you want both apps to share one set of keys")
    from omnibots.omni.standalone import ensure_standalone, missing_files, move_plaintext_keys, plaintext_keys
    missing = missing_files(loc)
    if missing and rep.fix:
        for f in ensure_standalone(loc):
            rep.add("config." + f.name.lstrip("."), "omni", "fixed", f"created {f}")
    elif missing:
        for f in missing:
            rep.add("config." + f.name.lstrip("."), "omni", "fail", f"{f} is missing", "Run the doctor with fixing on")
    for item in lay["standalone"]["files"]:
        p = ctx.path(item["path"])
        if p is not None and p.is_file() and p not in missing:
            rep.add(item["id"], "omni", "ok", f"{p} ({item['why']})")
    leaks = plaintext_keys(loc)
    if leaks:
        if rep.fix:
            try:
                moved = move_plaintext_keys(loc)
                rep.add("config.keys", "omni", "fixed",
                        f"moved {len(moved)} key(s) out of the text files into the encrypted store ({', '.join(sorted(set(moved)))})")
            except Exception as exc:
                rep.add("config.keys", "omni", "fail", f"keys are sitting in text files ({'; '.join(leaks)}) and could not be moved: {exc}",
                        "Start OmniBots once so its database is up to date, then run the doctor again")
        else:
            rep.add("config.keys", "omni", "warn", f"keys are sitting in text files: {'; '.join(leaks)}",
                    "Run the doctor with fixing on: it moves them into the encrypted store")
    else:
        rep.add("config.keys", "omni", "ok", "no provider key is stored as text")
    from omnibots.omni import keystore
    try:
        bad = keystore.unreadable(loc.db_file)
    except Exception:
        bad = []
    if bad:
        rep.add("config.keys.unreadable", "omni", "warn",
                f"{len(bad)} stored key(s) were saved by another Windows user or PC and can't be read: {', '.join(bad)}",
                "Type those keys again in Settings → Providers")
    return loc


def check_providers(ctx: Ctx, lay: dict, rep: Report, loc: Any, *, online: bool = False) -> None:
    from omnibots.omni.config import load_omni_config
    if loc is None:
        return
    try:
        cfg = load_omni_config(loc)
    except Exception as exc:
        rep.add("providers.load", "providers", "fail", f"the providers could not be read ({type(exc).__name__})",
                "Open Settings → Providers")
        return
    with_key = [p for p in cfg.providers.values() if p.has_key and p.key_source != "keyless"]
    where = "Omni's settings" if not loc.standalone else "OmniBots' encrypted store"
    if with_key:
        rep.add("providers.keys", "providers", "ok",
                f"{len(with_key)} provider(s) have a key: " + ", ".join(f"{p.name} ({p.key_source})" for p in with_key))
    else:
        rep.add("providers.keys", "providers", "fail", "no provider has a key, so the bots can't think",
                f"Add a key in Settings → Providers (it's saved in {where})")
    d = cfg.provider(str(cfg.default_provider or ""))
    if cfg.default_provider and (d is None or not d.has_key):
        rep.add("providers.default", "providers", "warn",
                f"the default provider {cfg.default_provider} has no key; OmniBots falls back to the providers that do",
                "Add its key in Settings → Providers")
    for p in cfg.providers.values():
        if p.error and (p.has_key or p.name == cfg.default_provider):
            rep.add(f"provider.{p.name}", "providers", "warn", f"{p.name}: {p.error}", "Open Settings → Providers")
    for e in cfg.errors:
        if not e.startswith("provider "):
            rep.add("providers.config", "providers", "warn", e)
    if not online:
        return
    import httpx
    timeout = float(lay["providers"].get("online_timeout_seconds", 10))
    for p in with_key:
        if not re.match(r"^https?://", p.base_url or "", re.I):
            continue
        try:
            r = httpx.get(p.base_url.rstrip("/") + "/models", headers={"Authorization": f"Bearer {p.api_key}"},
                          timeout=timeout, follow_redirects=True)
            code = r.status_code
        except httpx.HTTPError as exc:
            rep.add(f"provider.{p.name}.online", "providers", "warn", f"{p.name}: no answer from {p.base_url} ({type(exc).__name__})",
                    "Check the base URL and your connection")
            continue
        if code < 400:
            rep.add(f"provider.{p.name}.online", "providers", "ok", f"{p.name}: the key works ({p.base_url}, HTTP {code})")
        elif code in (401, 403):
            rep.add(f"provider.{p.name}.online", "providers", "fail", f"{p.name}: the key was refused (HTTP {code})",
                    "Type a new key in Settings → Providers")
        else:
            rep.add(f"provider.{p.name}.online", "providers", "warn",
                    f"{p.name}: HTTP {code} from {p.base_url}/models (some providers don't offer that list; chat may still work)")


def check_env(ctx: Ctx, lay: dict, rep: Report) -> None:
    for item in lay["env"]:
        val = (ctx.env.get(item["name"]) or "").strip()
        if not val:
            continue
        expect = ctx.path(item["expect"])
        if expect is not None and _same(val, expect):
            rep.add(f"env.{item['name']}", "env", "ok", f"{item['name']} = {val}")
        else:
            rep.add(f"env.{item['name']}", "env", "warn", f"{item['name']} points at {val}, not {expect} ({item['why']})",
                    f"Unless you moved it on purpose, remove {item['name']} or set it to {expect}, then restart")


def check_legacy(ctx: Ctx, lay: dict, rep: Report) -> None:
    allowed = [p for p in (ctx.path(t) for t in lay["allowed_roots"]["paths"]) if p is not None]
    clean = True
    for item in lay["legacy"]:
        p = ctx.path(item["path"])
        if p is None or not p.exists():
            continue
        if item.get("is_file"):
            if p.is_file():
                clean = False
                rep.add("legacy." + p.name, "legacy", "warn", f"{p} exists ({item['why']})",
                        "Move any keys into Settings → Providers (or ~/.omni), then delete that file yourself")
            continue
        if any(_same(p, a) for a in allowed if a != ctx.path("{appdata}")) or _same(p, ctx.home) or _same(p, CODE_ROOT):
            continue
        found = [n for n in item["look_for"] if (p / n).exists()]
        if found:
            clean = False
            rep.add("legacy." + p.name, "legacy", "warn",
                    f"{p} still has {', '.join(found)} ({item['why']}); OmniBots doesn't use it",
                    f"Keep only {ctx.home} and {ctx.tokens['omni']}: move anything you need there, then delete the old copy yourself")
    if clean:
        rep.add("legacy", "legacy", "ok", "config and keys are only in "
                + ", ".join(str(a) for a in allowed))


def run_doctor(*, home: Path | None = None, fix: bool = True, online: bool = False, install_deps: bool = False,
               groups: tuple[str, ...] | list[str] | None = None, env: dict[str, str] | None = None,
               save: bool = True, omni_root: str | None = None) -> Report:
    """Run the checks (all groups by default) and return the report. Never raises for a failed check."""
    lay = load_layout()
    ctx = make_ctx(home, env, omni_root)
    rep = Report(fix=fix)
    want = tuple(groups) if groups else GROUPS
    steps: list[tuple[str, Callable[[], Any]]] = [
        ("python", lambda: check_python(ctx, lay, rep, install_deps=install_deps and fix)),
        ("folders", lambda: check_folders(ctx, lay, rep)),
        ("settings", lambda: check_settings(ctx, lay, rep)),
        ("database", lambda: check_database(ctx, lay, rep)),
        ("code", lambda: check_code(ctx, lay, rep)),
    ]
    loc: Any = None
    for name, step in steps:
        if name in want:
            _guard(rep, name, step)
    if "omni" in want or "providers" in want:
        box: list[Any] = [None]

        def omni_step() -> None:
            if "omni" in want:
                box[0] = check_omni(ctx, lay, rep)
            else:
                from omnibots.omni.locate import resolve_location
                box[0] = resolve_location(ctx.configured_root, ctx.home, create=False)
        _guard(rep, "omni", omni_step)
        loc = box[0]
    if "providers" in want:
        _guard(rep, "providers", lambda: check_providers(ctx, lay, rep, loc, online=online))
    if "env" in want:
        _guard(rep, "env", lambda: check_env(ctx, lay, rep))
    if "legacy" in want:
        _guard(rep, "legacy", lambda: check_legacy(ctx, lay, rep))
    if save:
        try:
            (ctx.home / "logs").mkdir(parents=True, exist_ok=True)
            (ctx.home / "logs" / "doctor.json").write_text(json.dumps(rep.to_dict(), indent=2), encoding="utf-8")
        except OSError:
            pass
    return rep


def _guard(rep: Report, group: str, step: Callable[[], Any]) -> None:
    try:
        step()
    except Exception as exc:              # one broken check must not hide the others
        rep.add(f"{group}.error", group, "fail", f"the {group} check stopped: {type(exc).__name__}: {exc}")
