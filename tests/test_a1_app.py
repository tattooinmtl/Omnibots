"""A1 through the real app: status over the pipe, hot-reload, masked keys only,
a clear error when Omni is missing, and no keys in OmniBots' logs."""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

from conftest import run_app, send, wait_until_listening
from omni_helpers import REAL_OMNI, clean_env
from omnibots.lineup import TARGET_PROVIDERS
from omnibots.omni.config import load_omni_config
from omnibots.omni.locate import OmniLocation, locate_omni

needs_real_omni = pytest.mark.skipif(not (REAL_OMNI / "agent" / "settings.json").is_file(), reason="needs a real ~/.omni")


def write_fake_omni(root, key="first-key-1234567890"):
    (root / "agent").mkdir(parents=True, exist_ok=True)
    (root / "omni.config.json").write_text(json.dumps({"autoDiscoverSkills": False}), encoding="utf-8")
    settings = {"providers": {name: {"baseUrl": f"https://{name}.example/v1", "apiKey": key} for name in TARGET_PROVIDERS}}
    (root / "agent" / "settings.json").write_text(json.dumps(settings), encoding="utf-8")


def test_status_shows_masked_keys_and_hot_reloads(home, tmp_path, monkeypatch):
    fake = tmp_path / "fake-omni"
    write_fake_omni(fake)
    for k in list(__import__("os").environ):
        if k.startswith("OMNI_"):
            monkeypatch.delenv(k)
    monkeypatch.setenv("OMNI_INSTALL_ROOT", str(fake))
    app = run_app("--no-window", wait=False)
    try:
        omni = wait_until_listening()["status"]["omni"]
        assert omni["ok"] and omni["with_key"] == TARGET_PROVIDERS
        raw = json.dumps(omni)
        assert "first-key-1234567890" not in raw                       # masked, never the key
        assert omni["providers"]["minimax.io"]["key"] == "firs…7890"

        write_fake_omni(fake, key="second-key-0987654321")             # user runs /apikey in Omni
        deadline = time.time() + 15
        while time.time() < deadline:
            omni = send("status")[1]["status"]["omni"]
            if omni["providers"]["minimax.io"]["key"] == "seco…4321":
                break
            time.sleep(0.5)
        assert omni["providers"]["minimax.io"]["key"] == "seco…4321", "hot-reload did not pick up the new key"
    finally:
        send("stop")
        app.wait(timeout=30)
    conn = sqlite3.connect(home / "db" / "omnibots.sqlite")
    assert "omni_config_reloaded" in [r[0] for r in conn.execute("SELECT action FROM audit_logs")]
    log_text = (home / "logs" / "app.log").read_text(encoding="utf-8")
    assert "first-key-1234567890" not in log_text and "second-key-0987654321" not in log_text


def test_missing_omni_is_a_clear_error_not_a_crash(home, tmp_path, monkeypatch):
    monkeypatch.setenv("OMNI_INSTALL_ROOT", str(tmp_path / "nope"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "nohome"))   # hide ~/.omni too
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    app = run_app("--no-window", wait=False)
    try:
        omni = wait_until_listening()["status"]["omni"]
        assert omni["ok"] is False and "Omni was not found" in omni["error"]
    finally:
        send("stop")
        app.wait(timeout=30)


@needs_real_omni
def test_real_keys_never_reach_omnibots_logs(home):
    cfg = load_omni_config(locate_omni(), real_env=clean_env())
    keys = [p.api_key for p in cfg.providers.values() if p.api_key and p.api_key != "not-needed"]
    assert keys
    r = run_app("--no-window", "--exit-after", "4")
    assert r.returncode == 0, r.stderr
    text = (home / "logs" / "app.log").read_text(encoding="utf-8")
    assert "Omni loaded from" in text
    for k in keys:
        assert k not in text
