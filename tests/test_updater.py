"""A16.d update from inside the app. The layout check (installer copy vs clone), the About window offering
"Update now" only when it can, and the real PowerShell updater: it waits for the app to exit, runs the
installer it downloads with -InstallDir/-Yes/-NoShortcut, logs, and starts the app again. (A stand-in
installer and a harmless relaunch here; the real N → N+1 update is A16.d.99, at the next release.)"""

from __future__ import annotations

import functools
import http.server
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from qt_helpers import qapp

from omnibots import version as v
from omnibots.ui.about import AboutWindow
from omnibots.updater import install_layout, updater_script

app = qapp()


def test_an_installer_copy_is_told_apart_from_a_clone(tmp_path):
    inst = tmp_path / "OmniBots"
    (inst / ".git").mkdir(parents=True)
    (inst / ".venv" / "Scripts").mkdir(parents=True)
    (inst / "install.ps1").write_text("#", encoding="utf-8")
    exe = inst / ".venv" / "Scripts" / "python.exe"
    exe.write_text("", encoding="utf-8")
    assert install_layout(inst, str(exe))["kind"] == "installer"
    assert install_layout(inst, sys.executable)["kind"] == "clone"                  # a Python from elsewhere
    assert install_layout(tmp_path / "nothing", sys.executable)["kind"] == "unknown"


def test_about_offers_update_now_only_for_an_installer_copy():
    newer = "99.0.0"
    calls = []
    inst = AboutWindow(latest=lambda: newer, update=lambda: calls.append("update"), layout={"kind": "installer"})
    inst._show_result(newer)
    clone = AboutWindow(latest=lambda: newer, update=lambda: calls.append("update"), layout={"kind": "clone"})
    clone._show_result(newer)
    same = AboutWindow(latest=lambda: v.VERSION, update=lambda: None, layout={"kind": "installer"})
    same._show_result(v.VERSION)
    inst.update_btn.click()
    assert not inst.update_btn.isHidden() and calls == ["update"]
    assert clone.update_btn.isHidden() and "git pull" in clone.status.text()
    assert same.update_btn.isHidden() and "up to date" in same.status.text()


@pytest.mark.skipif(sys.platform != "win32", reason="the updater is PowerShell")
def test_the_real_updater_waits_runs_the_installer_logs_and_relaunches(tmp_path):
    served = tmp_path / "site"
    served.mkdir()
    marker = tmp_path / "installer_args.txt"
    (served / "install.ps1").write_text(
        "param([string]$InstallDir, [switch]$Yes, [switch]$NoShortcut)\n"
        f"\"dir=$InstallDir yes=$Yes noshortcut=$NoShortcut\" | Out-File '{marker}' -Encoding utf8\n"
        "Write-Output 'installer ran'\n", encoding="utf-8")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(served))
    handler.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    root = tmp_path / "OmniBots"
    root.mkdir()
    relaunched = tmp_path / "relaunched.txt"
    app_proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1.5)"])                   # "the app"
    script = tmp_path / "update.ps1"
    script.write_text(updater_script(app_proc.pid, str(root), sys.executable, url=f"http://127.0.0.1:{srv.server_address[1]}/install.ps1",
                                     args=("-c", f"open(r'{relaunched}', 'w').write('started')")), encoding="utf-8")
    t0 = time.time()
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)], capture_output=True, text=True, timeout=120)
    waited = time.time() - t0
    for _ in range(100):
        if relaunched.exists():
            break
        time.sleep(0.1)
    srv.shutdown()
    assert r.returncode == 0, r.stderr
    assert waited >= 1.0                                                                        # it waited for the app to exit
    assert marker.read_text(encoding="utf-8-sig").strip() == f"dir={root} yes=True noshortcut=True"
    assert "installer ran" in (root / "update.log").read_text(encoding="utf-8-sig")
    assert relaunched.read_text() == "started"
