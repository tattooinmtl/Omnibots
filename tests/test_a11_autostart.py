"""A11.q.01 (user, 2026-10-27): "an option to start omnibots on startup … a toggle option on and off".
Real registry, but a throwaway test key under HKCU, never the real Run key."""

from __future__ import annotations

import sys
import winreg

import pytest

from qt_helpers import qapp

from omnibots import autostart
from omnibots.ui.startup_page import StartupPage

qapp()

KEY = r"Software\OmniBotsTest\Run"


@pytest.fixture
def key():
    yield KEY
    for k in (KEY, r"Software\OmniBotsTest"):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, k)
        except OSError:
            pass


def test_command_uses_the_windowless_python_and_module():
    cmd = autostart.command(minimized=True)
    assert cmd.endswith('" -m omnibots --minimized')
    assert "pythonw.exe" in cmd.lower() or sys.executable.lower() in cmd.lower()
    assert not autostart.command(minimized=False).endswith("--minimized")


def test_enable_disable_round_trip(key):
    assert not autostart.is_enabled(key)
    autostart.enable(True, key)
    assert autostart.is_enabled(key) and autostart.is_minimized(key)
    autostart.enable(False, key)
    assert autostart.is_enabled(key) and not autostart.is_minimized(key)
    assert autostart.disable(key) is True
    assert not autostart.is_enabled(key)
    assert autostart.disable(key) is False


def test_refresh_repoints_a_moved_install(key):
    assert autostart.refresh(key) is False                 # off stays off
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as k:
        winreg.SetValueEx(k, autostart.NAME, 0, winreg.REG_SZ, r'"D:\old\pythonw.exe" -m omnibots --minimized')
    assert autostart.refresh(key) is True
    assert autostart.current(key) == autostart.command(True)


def test_the_page_toggles_it(key):
    page = StartupPage(key=key)
    assert not page.on.isChecked() and not page.minimized.isEnabled()
    page.on.setChecked(True)
    assert autostart.current(key) == autostart.command(True)
    assert page.minimized.isEnabled() and "On" in page.status.text()
    page.minimized.setChecked(False)
    assert autostart.current(key) == autostart.command(False)
    page.on.setChecked(False)
    assert not autostart.is_enabled(key) and "Off" in page.status.text()
    autostart.enable(False, key)
    page.reload()
    assert page.on.isChecked() and not page.minimized.isChecked()


def _visible_windows(pid: int) -> list[str]:
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    found: list[str] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        owner = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and u.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            u.GetWindowTextW(hwnd, buf, 256)
            if buf.value:
                found.append(buf.value)
        return True

    u.EnumWindows(each, 0)
    return found


def test_minimized_start_is_tray_only_and_a_second_one_stays_quiet(home):
    from conftest import run_app, send, wait_until_listening
    first = run_app("--minimized", "--exit-after", "60", wait=False)
    try:
        wait_until_listening()
        code, reply = send("tray")
        assert reply.get("ok"), reply                        # the tray is up
        assert _visible_windows(first.pid) == []             # but no window shows
        second = run_app("--minimized")
        assert second.returncode == 3 and "brought" not in second.stdout
        assert _visible_windows(first.pid) == []             # and the second start didn't open one
        code, reply = send("stop")
        assert first.wait(timeout=30) == 0
    finally:
        if first.poll() is None:
            first.kill()
