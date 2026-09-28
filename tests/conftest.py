"""Shared fixtures. Tests run the REAL app code against a throwaway home folder
and a private pipe name, so they never touch ~/.omnibots or a running OmniBots."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    h = tmp_path / "omnibots-home"
    monkeypatch.setenv("OMNIBOTS_HOME", str(h))
    monkeypatch.setenv("OMNIBOTS_PIPE", f"omnibots-test-{uuid.uuid4().hex[:8]}")
    return h


def run_app(*args: str, wait: bool = True, timeout: float = 60) -> subprocess.CompletedProcess | subprocess.Popen:
    cmd = [sys.executable, "-m", "omnibots", *args]
    kw = dict(cwd=ROOT, env=os.environ.copy(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if wait:
        return subprocess.run(cmd, timeout=timeout, **kw)
    return subprocess.Popen(cmd, **kw)


def send(cmd: str, *extra: str) -> tuple[int, dict]:
    r = run_app("--send", cmd, *extra, timeout=90)   # longer than the client's own reply wait (ipc.REPLY_WAIT_MS, A15.a.01)
    out = r.stdout.strip().splitlines()
    return r.returncode, (json.loads(out[-1]) if out else {})


def wait_until_listening(timeout: float = 30) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        code, reply = send("status")
        if code == 0 and reply.get("ok"):
            return reply
        time.sleep(0.3)
    raise TimeoutError("app never answered on its pipe")
