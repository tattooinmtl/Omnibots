"""A0.99 acceptance, driven through the real `python -m omnibots` process."""

from __future__ import annotations

import sqlite3
import sys
import threading
import time

import pytest

from omnibots.db.database import list_migrations

from conftest import run_app, send, wait_until_listening
from omnibots.keepawake import KeepAwake


def audit_actions(home) -> list[str]:
    conn = sqlite3.connect(home / "db" / "omnibots.sqlite")
    return [r[0] for r in conn.execute("SELECT action FROM audit_logs ORDER BY id")]


def test_starts_and_exits_cleanly_with_fresh_home(home):
    r = run_app("--no-window", "--exit-after", "2")
    assert r.returncode == 0, r.stderr
    for sub in ("db", "bots", "projects", "sandbox", "profiles", "logs", "skills"):
        assert (home / sub).is_dir()
    assert (home / "settings.toml").is_file()
    conn = sqlite3.connect(home / "db" / "omnibots.sqlite")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(list_migrations())
    assert audit_actions(home) == ["session_start", "bot_created", "session_end"]     # A5: Omi is created on first start
    assert conn.execute("SELECT id, role FROM bots").fetchall() == [("omi", "boss")]
    assert (home / "bots" / "omi" / "memory.md").is_file() and (home / "user_profile.md").is_file()
    assert "exited cleanly" in (home / "logs" / "app.log").read_text(encoding="utf-8")


def test_second_launch_hands_off_and_control_commands_work(home):
    first = run_app("--no-window", wait=False)
    try:
        status = wait_until_listening()
        assert status["status"]["schema_version"] == len(list_migrations())

        second = run_app("--no-window")
        assert second.returncode == 3, second.stdout + second.stderr   # ALREADY_RUNNING
        assert "already running" in second.stdout

        code, reply = send("goal")                     # A7: goals are real now; an empty one is refused (no tokens spent)
        assert reply == {"ok": False, "error": "goal needs text"}

        code, reply = send("stop")
        assert code == 0 and reply["ok"]
        assert first.wait(timeout=30) == 0
    finally:
        if first.poll() is None:
            first.kill()
    assert audit_actions(home)[-1] == "session_end"
    code, reply = send("status")
    assert code == 4 and reply["error"] == "OmniBots is not running"


def test_recovers_after_a_hard_crash(home):
    first = run_app("--no-window", wait=False)
    wait_until_listening()
    first.kill()                     # no cleanup: stale lock file and pipe left behind
    first.wait(timeout=10)
    r = run_app("--no-window", "--exit-after", "2")
    assert r.returncode == 0, r.stdout + r.stderr
    conn = sqlite3.connect(home / "db" / "omnibots.sqlite")
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    actions = audit_actions(home)
    assert actions.count("session_start") == 2 and actions[-1] == "session_end"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows focus rules")
def test_second_launch_focuses_the_first_window(home):
    import win32gui

    # an installed app: the output folder was chosen on the first start (A11.m.01), so no setup question
    from omnibots.settings import DEFAULT_SETTINGS_TOML
    home.mkdir(parents=True, exist_ok=True)
    out = (home.parent / "output").as_posix()
    (home / "settings.toml").write_text(DEFAULT_SETTINGS_TOML.replace('folder = ""', f'folder = "{out}"').replace("splash = true", "splash = false"), encoding="utf-8")
    first = run_app(wait=False)     # with the real window
    try:
        wait_until_listening()
        time.sleep(1.0)
        # Put some other window in front, then launch again.
        hwnd = win32gui.FindWindow(None, "OmniBots")
        assert hwnd, "main window not found"
        second = run_app()
        assert second.returncode == 3
        time.sleep(1.0)
        fg = win32gui.GetForegroundWindow()
        assert win32gui.GetWindowText(fg) == "OmniBots", f"foreground is {win32gui.GetWindowText(fg)!r}"
        assert win32gui.IsWindowVisible(hwnd)
    finally:
        send("stop")
        try:
            first.wait(timeout=30)
        except Exception:
            first.kill()


def test_keep_awake_is_reference_counted_and_thread_bound():
    ka = KeepAwake(enabled=True)
    ka.acquire()
    ka.acquire()
    assert ka.active
    ka.release()
    assert ka.active
    ka.release()
    assert not ka.active
    errors = []
    t = threading.Thread(target=lambda: errors.append(pytest.raises(RuntimeError, ka.acquire)))
    t.start()
    t.join()
    assert errors and errors[0] is not None


def test_approvals_are_answerable_over_the_pipe(home):
    """A3: a parked R3 action can be approved/denied from outside the bot (UI, tray, Omni)."""
    app = run_app("--no-window", wait=False)
    try:
        wait_until_listening()
        code, reply = send("approvals")
        assert code == 0 and reply == {"ok": True, "pending": []}
        code, reply = send("approve", "--text", "ap_does_not_exist")
        assert reply["ok"] is False and "no pending approval" in reply["error"]
    finally:
        send("stop")
        app.wait(timeout=30)
