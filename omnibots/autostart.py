"""Start with Windows (A11.q.01): one value under the user's own Run key, nothing else.

HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run\\OmniBots = "<venv>\\pythonw.exe" -m omnibots [--minimized]
Only the current user's key (no admin, no other users). Turning it off deletes that one value.
"""

from __future__ import annotations

import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
NAME = "OmniBots"


def _winreg():
    import winreg
    return winreg


def command(minimized: bool = True, exe: str | None = None) -> str:
    """The command Windows runs at sign-in: the windowless Python next to this one (no console flashes)."""
    py = Path(exe or sys.executable)
    pyw = py.with_name("pythonw.exe")
    if pyw.is_file():
        py = pyw
    return f'"{py}" -m omnibots' + (" --minimized" if minimized else "")


def current(key: str = RUN_KEY, name: str = NAME) -> str | None:
    """The command registered now, or None."""
    wr = _winreg()
    try:
        with wr.OpenKey(wr.HKEY_CURRENT_USER, key) as k:
            value, _ = wr.QueryValueEx(k, name)
            return str(value)
    except OSError:
        return None


def is_enabled(key: str = RUN_KEY, name: str = NAME) -> bool:
    return current(key, name) is not None


def is_minimized(key: str = RUN_KEY, name: str = NAME) -> bool:
    return "--minimized" in (current(key, name) or "")


def enable(minimized: bool = True, key: str = RUN_KEY, name: str = NAME) -> str:
    wr = _winreg()
    cmd = command(minimized)
    with wr.CreateKey(wr.HKEY_CURRENT_USER, key) as k:
        wr.SetValueEx(k, name, 0, wr.REG_SZ, cmd)
    return cmd


def disable(key: str = RUN_KEY, name: str = NAME) -> bool:
    """Remove the value. False when it wasn't there."""
    wr = _winreg()
    try:
        with wr.OpenKey(wr.HKEY_CURRENT_USER, key, 0, wr.KEY_SET_VALUE) as k:
            wr.DeleteValue(k, name)
        return True
    except OSError:
        return False


def refresh(key: str = RUN_KEY, name: str = NAME) -> bool:
    """If it's on, point it at this install again (the folder or venv moved). True when it changed."""
    old = current(key, name)
    if old is None:
        return False
    new = command("--minimized" in old)
    if new == old:
        return False
    enable("--minimized" in old, key, name)
    return True
