"""A15.a.01: a pipe command that waits on the engine doesn't freeze the window. While one command
waits for its answer, another is answered (the Qt event loop is free), and one the engine never
answers gets a clear error at its timeout. Real named pipe, real event loop, and each client in its
own process, like `python -m omnibots --send` (PySide's blocking socket waits hold the GIL, so a
client thread in this process would stall the server's loop).

No timing thresholds: "slow" can only be answered after the server has answered "fast". With the
old blocking handlers, "fast" would never get through and "slow" would time out."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import Future
from pathlib import Path

from PySide6.QtCore import QCoreApplication

from omnibots.ipc import Deferred, SingleInstance

ROOT = Path(__file__).resolve().parents[1]
CLIENT = """
import json, sys, time
from omnibots.ipc import send_command
time.sleep(float(sys.argv[3]))
reply = send_command({"cmd": sys.argv[2]}, name=sys.argv[1])
print(json.dumps({"end": time.time(), "reply": reply}))
"""


def _failed() -> Future:
    f: Future = Future()
    f.set_exception(RuntimeError("engine blew up"))
    return f


def test_a_slow_command_does_not_block_the_others(tmp_path):
    app = QCoreApplication.instance() or QCoreApplication([])
    name = f"omnibots-test-{uuid.uuid4().hex[:8]}"
    slow_result: Future = Future()
    order: list[str] = []

    def slow(_m):
        order.append("slow asked")
        return Deferred(slow_result, lambda r: {"ok": True, "r": r}, timeout=30)

    def fast(_m):
        order.append("fast answered")
        threading.Timer(0.3, lambda: slow_result.done() or slow_result.set_result(42)).start()   # only now can slow finish
        return {"ok": True}

    inst = SingleInstance(tmp_path / "test.lock", {
        "slow": slow, "fast": fast,
        "never": lambda m: Deferred(Future(), lambda r: {"ok": True}, timeout=0.5),
        "broken": lambda m: Deferred(_failed(), lambda r: {"ok": True}, timeout=5),
    }, name=name)
    assert inst.acquire()
    def launch(cmd: str) -> subprocess.Popen:
        return subprocess.Popen([sys.executable, "-c", CLIENT, name, cmd, "0"], cwd=ROOT,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    procs = {cmd: launch(cmd) for cmd in ("slow", "never", "broken")}
    deadline = time.time() + 60
    while time.time() < deadline:
        app.processEvents()
        if "fast" not in procs and "slow asked" in order:
            procs["fast"] = launch("fast")                  # only once "slow" is really waiting on the server
        if "fast" in procs and all(p.poll() is not None for p in procs.values()):
            break
        time.sleep(0.01)
    inst.release()
    stuck = [c for c, p in procs.items() if p.poll() is None]
    for c in stuck:
        procs[c].kill()
    assert not stuck and "fast" in procs, f"clients still running: {stuck}; order so far: {order}"
    out = {}
    for cmd, p in procs.items():
        if p.poll() is None:
            p.kill()
        stdout, stderr = p.communicate()
        assert stdout.strip(), f"{cmd}: no output; {stderr[-500:]}"
        out[cmd] = json.loads(stdout.strip().splitlines()[-1])

    assert order == ["slow asked", "fast answered"]                          # fast was served while slow waited
    assert out["fast"]["reply"] == {"ok": True}
    assert out["slow"]["reply"] == {"ok": True, "r": 42} and out["fast"]["end"] < out["slow"]["end"]
    assert out["never"]["reply"] == {"ok": False, "error": "no answer from the engine within 0.5s"}
    assert out["broken"]["reply"] == {"ok": False, "error": "engine blew up"}
