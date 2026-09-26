"""A9.a.01: the S0 sandbox inside a Windows AppContainer + Job Object. Real processes, no mocks:
each test is something a hostile script might try."""

from __future__ import annotations

import asyncio
import os
import socket
import sys
from pathlib import Path

import pytest

from omnibots.runtime.sandbox import Sandbox

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows AppContainer sandbox")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def sb(tmp_path):
    s = Sandbox(tmp_path / "sandbox", isolation="appcontainer", memory_mb=512, max_processes=16)
    assert s.level == "S0+AppContainer"
    return s


def py(tmp_path: Path, name: str, code: str) -> list[str]:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    (ws / name).write_text(code, encoding="utf-8")
    return [sys.executable, "-I", str(ws / name)]


def test_runs_code_and_reports_the_level(sb, tmp_path):
    r = run(sb.run(py(tmp_path, "a.py", "import os; print(6*7, os.environ['OMNIBOTS_SANDBOX'])"), cwd=tmp_path / "ws", timeout=30))
    assert r.exit_code == 0 and "42 S0+AppContainer" in r.output and "sandbox S0+AppContainer" in r.as_tool_result()


def test_cannot_read_files_outside_the_granted_folders(sb, tmp_path):
    secret_dir = tmp_path / "not-granted"
    secret_dir.mkdir()
    (secret_dir / "keys.env").write_text("OPENAI_API_KEY=sk-planted-secret\n", encoding="utf-8")
    code = (f"p = r'{secret_dir / 'keys.env'}'\n"
            "try:\n    print('LEAK', open(p).read())\nexcept PermissionError:\n    print('DENIED')\n")
    r = run(sb.run(py(tmp_path, "b.py", code), cwd=tmp_path / "ws", timeout=30))
    assert "DENIED" in r.output and "sk-planted-secret" not in r.output


def test_cannot_read_the_real_omni_keys_or_ssh(sb, tmp_path):
    targets = [p for p in (Path.home() / ".omni" / ".env", Path.home() / ".ssh" / "config") if p.exists()]
    if not targets:
        pytest.skip("no ~/.omni/.env or ~/.ssh/config on this machine")
    code = "import sys\nfor p in sys.argv[1:]:\n    try:\n        open(p).read(1); print('READABLE', p)\n    except PermissionError:\n        print('DENIED', p)\n"
    r = run(sb.run(py(tmp_path, "c.py", code) + [str(p) for p in targets], cwd=tmp_path / "ws", timeout=30))
    assert r.output.count("DENIED") == len(targets) and "READABLE" not in r.output


def test_cannot_read_the_credential_vault(sb, tmp_path):
    import keyring
    keyring.set_password("omnibots-a9-test", "planted", "planted-vault-secret")
    try:
        code = ("import ctypes\nfrom ctypes import wintypes as wt\n"
                "n, arr = wt.DWORD(), ctypes.c_void_p()\n"
                "ok = ctypes.windll.advapi32.CredEnumerateW(None, 0, ctypes.byref(n), ctypes.byref(arr))\n"
                "print('VAULT OPEN', n.value) if ok else print('VAULT DENIED', ctypes.GetLastError())\n")
        r = run(sb.run(py(tmp_path, "d.py", code), cwd=tmp_path / "ws", timeout=30))
    finally:
        keyring.delete_password("omnibots-a9-test", "planted")
    assert "VAULT DENIED" in r.output and "planted-vault-secret" not in r.output


def test_writes_only_inside_the_workspace(sb, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    code = (f"open('inside.txt', 'w').write('ok'); print('INSIDE OK')\n"
            f"try:\n    open(r'{outside / 'x.txt'}', 'w').write('x'); print('OUTSIDE WRITTEN')\nexcept PermissionError:\n    print('OUTSIDE DENIED')\n")
    r = run(sb.run(py(tmp_path, "e.py", code), cwd=tmp_path / "ws", timeout=30))
    assert "INSIDE OK" in r.output and "OUTSIDE DENIED" in r.output and not (outside / "x.txt").exists()


def test_no_network_unless_granted(sb, tmp_path):
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=5).close()
    except OSError:
        pytest.skip("this machine has no internet")
    code = ("import socket\ntry:\n    socket.create_connection(('1.1.1.1', 443), timeout=5).close(); print('NET OPEN')\n"
            "except OSError as e:\n    print('NET BLOCKED', type(e).__name__)\n")
    argv = py(tmp_path, "f.py", code)
    blocked = run(sb.run(argv, cwd=tmp_path / "ws", timeout=30))
    allowed = run(sb.run(argv, cwd=tmp_path / "ws", timeout=30, network=True))
    assert "NET BLOCKED" in blocked.output and "NET OPEN" in allowed.output


def test_infinite_loop_is_killed_at_the_time_limit(sb, tmp_path):
    r = run(sb.run(py(tmp_path, "g.py", "while True:\n    pass\n"), cwd=tmp_path / "ws", timeout=3))
    assert r.timed_out and 2.5 < r.seconds < 8 and "TIMED OUT" in r.as_tool_result()


def test_process_and_memory_caps(sb, tmp_path):
    fork = ("import subprocess, sys\nok = 0\n"
            "for _ in range(40):\n    try:\n        subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); ok += 1\n"
            "    except OSError:\n        break\nprint('STARTED', ok)\n")
    r = run(sb.run(py(tmp_path, "h.py", fork), cwd=tmp_path / "ws", timeout=20))
    started = int(r.output.split("STARTED")[1].split()[0])
    assert started < 16                                    # the job's process cap stopped the fork bomb
    m = run(sb.run(py(tmp_path, "i.py", "x = bytearray(1024**3)\nprint('ALLOCATED')\n"), cwd=tmp_path / "ws", timeout=30))
    assert "ALLOCATED" not in m.output and "MemoryError" in m.output


def test_children_die_with_the_run(sb, tmp_path):
    marker = tmp_path / "ws" / "child_alive.txt"
    code = ("import subprocess, sys\n"
            f"subprocess.Popen([sys.executable, '-c', \"import time; time.sleep(4); open('{marker.as_posix()}', 'w').write('alive')\"])\n"
            "print('parent done')\n")
    r = run(sb.run(py(tmp_path, "j.py", code), cwd=tmp_path / "ws", timeout=20))
    assert "parent done" in r.output
    run(asyncio.sleep(6))
    assert not marker.exists()                            # the job was closed: the grandchild never finished


def test_shell_commands_with_quotes(sb, tmp_path):
    (tmp_path / "ws").mkdir(exist_ok=True)
    r = run(sb.run('python -c "print(\'quoted ok\')" && echo second', cwd=tmp_path / "ws", timeout=30))
    assert "quoted ok" in r.output and "second" in r.output and r.exit_code == 0


def test_plain_s0_fallback_still_works(tmp_path):
    s = Sandbox(tmp_path / "sb", isolation="none")
    assert s.level == "S0"
    (tmp_path / "ws").mkdir()
    r = run(s.run('python -c "print(1)"', cwd=tmp_path / "ws", timeout=30))
    assert r.exit_code == 0 and "sandbox S0," in r.as_tool_result()
