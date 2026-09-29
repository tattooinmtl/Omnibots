"""No Omni on the PC: OmniBots' own Omni-shaped config with encrypted keys (A1.s.01), and the doctor (A16.d).

Every test runs against throwaway folders: USERPROFILE, LOCALAPPDATA and APPDATA point into tmp_path,
so "~/.omni" doesn't exist there and nothing touches the real ~/.omnibots or ~/.omni.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from omnibots.db.database import apply_migrations, list_migrations, open_connection
from omnibots.doctor import run_doctor
from omnibots.doctor.doctor import GROUPS, load_layout
from omnibots.omni import keystore
from omnibots.omni.config import load_omni_config
from omnibots.omni.locate import OmniNotFound, locate_omni, resolve_location, standalone_location
from omnibots.omni.providers_store import apply_provider, clear_provider_key, delete_provider, list_providers
from omnibots.omni.standalone import move_plaintext_keys, plaintext_keys
from omnibots.security import dpapi

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")

KEY = "sk-standalone-secret-key-1234567890"
KEY2 = "nv-account-two-secret-0987654321"
OFFLINE = tuple(g for g in GROUPS if g not in ("python",))      # no pip, no network


@pytest.fixture
def pc(tmp_path, monkeypatch):
    """A PC without Omni: a fake user folder, AppData, and OmniBots home."""
    user = tmp_path / "user"
    user.mkdir()
    for var in ("OMNI_INSTALL_ROOT", "OMNI_HOME", "OMNIBOTS_DIR"):
        monkeypatch.delenv(var, raising=False)
    for var in [k for k in os.environ if k.startswith("OMNI_") and k.endswith("_KEY")]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("USERPROFILE", str(user))
    monkeypatch.setenv("HOME", str(user))
    monkeypatch.setenv("LOCALAPPDATA", str(user / "AppData" / "Local"))
    monkeypatch.setenv("APPDATA", str(user / "AppData" / "Roaming"))
    home = user / ".omnibots"
    monkeypatch.setenv("OMNIBOTS_HOME", str(home))
    return home


def _all_text(folder: Path) -> str:
    out = []
    for f in folder.rglob("*"):
        if f.is_file():
            out.append(f.read_bytes().decode("latin-1"))
    return "\n".join(out)


def _fresh_db(home: Path) -> Path:
    db = home / "db" / "omnibots.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = open_connection(db)
    apply_migrations(conn)
    conn.close()
    return db


# ── DPAPI + the key table ─────────────────────────────────────────────────
def test_dpapi_round_trip_never_stores_the_text():
    blob = dpapi.protect(KEY)
    assert KEY.encode() not in blob and dpapi.unprotect(blob) == KEY
    with pytest.raises(dpapi.DpapiError):
        dpapi.unprotect(blob[:-4] + b"xxxx")


def test_keystore_keeps_only_ciphertext_in_sqlite(pc):
    db = _fresh_db(pc)
    keystore.set_key(db, "groq", KEY)
    keystore.set_key(db, "nvidia", KEY2, "nvidia2")
    assert keystore.read_keys(db) == {"groq": {"": KEY}, "nvidia": {"nvidia2": KEY2}}
    raw = db.read_bytes() + (db.with_name(db.name + "-wal").read_bytes() if db.with_name(db.name + "-wal").exists() else b"")
    assert KEY.encode() not in raw and KEY2.encode() not in raw
    assert keystore.delete_key(db, "nvidia") == 1 and "nvidia" not in keystore.read_keys(db)


def test_an_older_database_is_not_upgraded_behind_the_apps_back(pc):
    db = pc / "db" / "omnibots.sqlite"
    db.parent.mkdir(parents=True)
    conn = open_connection(db)
    apply_migrations(conn)
    conn.execute("DROP TABLE provider_keys")
    conn.execute("PRAGMA user_version = 7")
    conn.close()
    assert keystore.read_keys(db) == {}
    with pytest.raises(keystore.KeystoreError):
        keystore.set_key(db, "groq", KEY)


# ── standalone config ─────────────────────────────────────────────────────
def test_no_omni_means_an_omni_shaped_config_in_omnibots_home(pc):
    with pytest.raises(OmniNotFound):
        locate_omni()
    loc = resolve_location("", pc)
    assert loc.standalone and loc.home == (pc / "config").resolve()
    settings = json.loads(loc.settings_file.read_text(encoding="utf-8"))
    assert set(settings) == {"defaultProvider", "defaultModel", "providers"} and settings["providers"] == {}
    assert (loc.home / ".env").is_file() and json.loads(loc.project_config_file.read_text()) == {"skills": [], "mcpServers": {}}
    assert not (pc.parent / ".omni").exists()                       # Omni's folders are never created
    cfg = load_omni_config(loc)
    assert "groq" in cfg.providers and not cfg.providers["groq"].has_key


def test_providers_page_store_puts_keys_in_the_database_not_the_file(pc):
    db = _fresh_db(pc)
    loc = resolve_location("", pc)
    apply_provider(loc.settings_file, name="groq", base_url="https://api.groq.com/openai/v1",
                   key_action="set", api_key=KEY, keys_db=db)
    apply_provider(loc.settings_file, name="mybox", base_url="http://localhost:1234/v1",
                   key_action="set", api_key=KEY2, keys_db=db)
    assert KEY not in _all_text(loc.home) and KEY2 not in _all_text(loc.home)
    cfg = load_omni_config(loc)
    assert cfg.providers["groq"].api_key == KEY and cfg.providers["groq"].key_source == "vault"
    assert cfg.providers["mybox"].api_key == KEY2
    rows = {r.name: r for r in list_providers(loc.settings_file, env_files=[], real_env={}, keys_db=db)}
    assert rows["groq"].key_source == "vault" and rows["groq"].disk_has_key and KEY not in rows["groq"].key_mask

    clear_provider_key(loc.settings_file, "groq", keys_db=db)
    assert not load_omni_config(loc).providers["groq"].has_key
    assert delete_provider(loc.settings_file, "mybox", keys_db=db) == "removed"
    assert keystore.read_keys(db) == {} and "mybox" not in load_omni_config(loc).providers


def test_account_keys_go_to_the_active_account(pc):
    db = _fresh_db(pc)
    loc = resolve_location("", pc)
    apply_provider(loc.settings_file, name="nvidia", base_url="https://integrate.api.nvidia.com/v1",
                   key_action="set", api_key=KEY2, active_account="nvidia2", keys_db=db)
    assert keystore.read_keys(db) == {"nvidia": {"nvidia2": KEY2}}
    p = load_omni_config(loc).providers["nvidia"]
    assert p.api_key == KEY2 and p.active_account == "nvidia2"
    assert KEY2 not in loc.settings_file.read_text(encoding="utf-8")


def test_keys_typed_into_the_files_are_moved_into_the_store(pc):
    db = _fresh_db(pc)
    loc = resolve_location("", pc)
    doc = json.loads(loc.settings_file.read_text(encoding="utf-8"))
    doc["providers"]["groq"] = {"baseUrl": "https://api.groq.com/openai/v1", "apiKey": KEY}
    loc.settings_file.write_text(json.dumps(doc), encoding="utf-8")
    (loc.home / ".env").write_text("# mine\nOMNI_MINIMAX_IO_KEY=" + KEY2 + "\n", encoding="utf-8")
    assert plaintext_keys(loc) == ["settings.json: groq", ".env: OMNI_MINIMAX_IO_KEY"]
    assert sorted(move_plaintext_keys(loc)) == ["groq", "minimax.io"]
    assert KEY not in _all_text(loc.home) and KEY2 not in _all_text(loc.home)
    assert "# mine" in (loc.home / ".env").read_text(encoding="utf-8")
    cfg = load_omni_config(loc)
    assert cfg.providers["groq"].api_key == KEY and cfg.providers["minimax.io"].api_key == KEY2
    assert plaintext_keys(loc) == []


# ── the doctor ────────────────────────────────────────────────────────────
def test_the_layout_file_is_complete():
    lay = load_layout()
    for section in ("allowed_roots", "folders", "files", "omni", "standalone", "python", "packages", "programs", "env", "legacy"):
        assert section in lay, section
    ids = [i["id"] for i in lay["folders"] + lay["files"] + lay["omni"]["files"] + lay["standalone"]["files"]]
    assert len(ids) == len(set(ids))
    assert {"PySide6", "keyring", "httpx", "pydantic", "playwright"} <= {p["module"] for p in lay["packages"]}


def test_doctor_sets_up_a_fresh_pc_without_omni_and_is_idempotent(pc):
    rep = run_doctor(home=pc, fix=True, groups=OFFLINE)
    by = {f.id: f for f in rep.findings}
    assert rep.mode == "standalone"
    for sub in ("db", "bots", "projects", "sandbox", "profiles", "logs", "skills", "sessions", "backups"):
        assert (pc / sub).is_dir(), sub
    assert (pc / "settings.toml").is_file() and (pc / "db" / "omnibots.sqlite").is_file()
    assert (pc.parent / "AppData" / "Local" / "OmniBots" / "cache").is_dir()
    assert (pc / "config" / "settings.json").is_file() and (pc / "config" / ".env").is_file()
    assert by["providers.keys"].status == "fail"                   # nothing to think with yet: said plainly
    assert [f.id for f in rep.findings if f.status == "fail"] == ["providers.keys"]
    assert (pc / "logs" / "doctor.json").is_file()

    again = run_doctor(home=pc, fix=True, groups=OFFLINE)
    assert not [f for f in again.findings if f.status == "fixed"]


def test_doctor_moves_text_keys_and_never_shows_one(pc):
    run_doctor(home=pc, fix=True, groups=OFFLINE)
    (pc / "config" / ".env").write_text("OMNI_GROQ_KEY=" + KEY + "\n", encoding="utf-8")
    check = run_doctor(home=pc, fix=False, groups=("omni",))
    assert {f.id: f.status for f in check.findings}["config.keys"] == "warn"
    rep = run_doctor(home=pc, fix=True, groups=OFFLINE)
    assert {f.id: f.status for f in rep.findings}["config.keys"] == "fixed"
    assert {f.id: f.status for f in rep.findings}["providers.keys"] == "ok"
    blob = rep.text(all_lines=True) + json.dumps(rep.to_dict()) + (pc / "logs" / "doctor.json").read_text(encoding="utf-8")
    assert KEY not in blob and KEY not in _all_text(pc / "config")


def test_doctor_upgrades_an_old_database_after_a_backup(pc):
    db = pc / "db" / "omnibots.sqlite"
    db.parent.mkdir(parents=True)
    conn = open_connection(db)
    apply_migrations(conn)
    conn.execute("DROP TABLE provider_keys")
    conn.execute("PRAGMA user_version = 7")
    conn.close()
    rep = run_doctor(home=pc, fix=True, groups=("database",))
    assert {f.id: f.status for f in rep.findings}["home.database"] == "fixed"
    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(list_migrations())
    conn.close()
    assert list((pc / "backups").rglob("*.sqlite"))


def test_doctor_reports_old_copies_and_leaves_them(pc):
    old = pc.parent / "AppData" / "Local" / "OmniBots"
    (old / "db").mkdir(parents=True)
    (old / "settings.toml").write_text("[app]\n", encoding="utf-8")
    loose = pc.parent / ".env"
    loose.write_text("OMNI_GROQ_KEY=x\n", encoding="utf-8")
    rep = run_doctor(home=pc, fix=True, groups=("legacy",))
    warns = [f.message for f in rep.findings if f.status == "warn"]
    assert any(str(old) in m and "settings.toml" in m for m in warns)
    assert any(str(loose) in m for m in warns)
    assert (old / "settings.toml").is_file() and loose.is_file()     # reported, never deleted


def test_doctor_never_changes_omnis_files(pc, tmp_path):
    omni = pc.parent / ".omni"
    (omni / "agent").mkdir(parents=True)
    (omni / "omni.config.json").write_text(json.dumps({"extensions": []}), encoding="utf-8")
    (omni / "agent" / "settings.json").write_text(json.dumps({"providers": {"groq": {"apiKey": KEY}}}), encoding="utf-8")
    before = {f: (f.stat().st_mtime_ns, f.read_bytes()) for f in omni.rglob("*") if f.is_file()}
    rep = run_doctor(home=pc, fix=True, groups=OFFLINE)
    after = {f: (f.stat().st_mtime_ns, f.read_bytes()) for f in omni.rglob("*") if f.is_file()}
    assert before == after and rep.mode == "omni"
    by = {f.id: f for f in rep.findings}
    assert by["omni.launcher"].status == "warn" and by["omni.launcher.listed"].status == "warn"
    assert not (pc / "config").exists()                               # Omni is there: no standalone copy
    assert KEY not in rep.text(all_lines=True)


def test_doctor_flags_secrets_in_settings_toml(pc):
    pc.mkdir(parents=True)
    (pc / "settings.toml").write_text('[providers]\napi_key = "abc123"\n', encoding="utf-8")
    rep = run_doctor(home=pc, fix=True, groups=("settings",))
    f = {f.id: f for f in rep.findings}["home.settings.keys"]
    assert f.status == "warn" and "abc123" not in f.message
    assert 'api_key = "abc123"' in (pc / "settings.toml").read_text(encoding="utf-8")     # never rewritten


def test_omi_gets_call_doctor_when_the_app_has_a_doctor(pc):
    from types import SimpleNamespace

    from omnibots.orchestrator.boss_tools import BossToolkit
    calls = []

    async def doctor(*, fix, online):
        calls.append((fix, online))
        return run_doctor(home=pc, fix=fix, groups=OFFLINE).to_dict()

    runner = SimpleNamespace(mcp_servers=lambda: [], relay=None)
    kw = dict(ctx=None, db=None, bus=None, inbox=None, registry=None, runner=runner, graph=None, projects=None,
              ledger=None, factory=None, router=None)
    assert "call_doctor" not in {t.name for t in BossToolkit(**kw).tools()}
    kit = BossToolkit(**kw, doctor=doctor)
    tool = {t.name: t for t in kit.tools()}["call_doctor"]
    out = asyncio.run(tool.fn({}, None))
    assert calls == [(True, False)] and "OmniBots doctor:" in out and "no provider has a key" in out


def test_settings_has_a_doctor_tab_that_shows_the_report(pc):
    from qt_helpers import qapp
    qapp()
    from omnibots.ui.doctor_page import DoctorPage, report_html
    from omnibots.ui.settings_window import SettingsWindow
    db = _fresh_db(pc)
    loc = resolve_location("", pc)
    w = SettingsWindow(omni_settings=loc.settings_file)
    names = [w.tabs.tabText(i) for i in range(w.tabs.count())]
    assert names[-2:] == ["Providers", "Doctor"]
    page = DoctorPage(home=pc)
    page._work(True, False)                              # the worker body, run inline
    html = report_html(run_doctor(home=pc, fix=False, groups=OFFLINE).to_dict())
    assert "no provider has a key" in html and "Everything checks out" not in html
    assert db.is_file()


def test_a_named_install_root_that_is_wrong_is_an_error_not_standalone(pc, monkeypatch):
    """[omni] install_root (or OMNI_INSTALL_ROOT) set on purpose: a wrong folder is reported, and
    OmniBots doesn't quietly switch to its own config (which would hide the mistake)."""
    with pytest.raises(OmniNotFound):
        resolve_location(str(pc.parent / "no-such-omni"), pc)
    monkeypatch.setenv("OMNI_INSTALL_ROOT", str(pc.parent / "also-missing"))
    with pytest.raises(OmniNotFound):
        resolve_location("", pc)
    assert not (pc / "config").exists()
    monkeypatch.delenv("OMNI_INSTALL_ROOT")
    assert resolve_location("", pc).standalone            # nothing named: no Omni means standalone


def test_the_providers_page_honours_install_root_from_settings(pc):
    """Grok's page called locate_omni() without [omni] install_root; it now reads it like the engine."""
    from qt_helpers import qapp
    qapp()
    from omnibots.ui.providers_page import ProvidersPage
    omni = pc.parent / "elsewhere" / "omni"
    (omni / "agent").mkdir(parents=True)
    (omni / "omni.config.json").write_text("{}", encoding="utf-8")
    (omni / "agent" / "settings.json").write_text('{"providers": {}}', encoding="utf-8")
    pc.mkdir(parents=True)
    (pc / "settings.toml").write_text(f'[omni]\ninstall_root = "{omni.as_posix()}"\n', encoding="utf-8")
    page = ProvidersPage()
    assert page.path == (omni / "agent" / "settings.json").resolve() and page.keys_db is None
    (pc / "settings.toml").write_text('[omni]\ninstall_root = ""\n', encoding="utf-8")
    page = ProvidersPage()
    assert page.path == (pc / "config" / "settings.json").resolve() and page.keys_db is not None
