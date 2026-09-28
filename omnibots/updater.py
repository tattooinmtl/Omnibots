"""Update from inside the app (PLAN.md A16.d): About → "Update now".

For an installer copy (install.ps1 put the code in a git clone with its own .venv, and the app runs from
that venv), the update is the same install.ps1 again, run against the install folder. The app can't
update files it's running from, so it writes a small PowerShell script, starts it detached, and quits;
the script waits for the app to exit, runs the installer (its output goes to update.log in the install
folder), then starts OmniBots again. A backup is taken first (A16.c). A developer clone gets "git pull"
instead.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from omnibots.version import ROOT

INSTALLER_URL = "https://raw.githubusercontent.com/tattooinmtl/Omnibots/master/install.ps1"


def install_layout(root: Path = ROOT, executable: str = sys.executable) -> dict[str, str]:
    """{'kind': 'installer'|'clone'|'unknown', 'root', 'pythonw'}. An installer copy is a git clone with its own
    .venv that this Python runs from."""
    root = Path(root).resolve()
    venv = root / ".venv"
    if not (root / ".git").exists():
        return {"kind": "unknown", "root": str(root), "pythonw": ""}
    exe = Path(executable).resolve()
    if venv.is_dir() and exe.is_relative_to(venv.resolve()) and (root / "install.ps1").is_file():
        return {"kind": "installer", "root": str(root), "pythonw": str(venv / "Scripts" / "pythonw.exe")}
    return {"kind": "clone", "root": str(root), "pythonw": ""}


def updater_script(pid: int, root: str, pythonw: str, url: str = INSTALLER_URL, args: tuple[str, ...] = ("-m", "omnibots")) -> str:
    """The PowerShell the updater runs once the app has exited."""
    q = lambda s: "'" + str(s).replace("'", "''") + "'"
    return "\n".join([
        "$ErrorActionPreference = 'Continue'",
        f"$dir = {q(root)}",
        f"Wait-Process -Id {int(pid)} -Timeout 120 -ErrorAction SilentlyContinue",
        "$log = Join-Path $dir 'update.log'",
        "\"OmniBots update $(Get-Date -Format s)\" | Out-File $log -Encoding utf8",
        "try {",
        f"    $installer = [scriptblock]::Create((Invoke-RestMethod {q(url)}))",
        # one encoding in the log: `*>>` would append UTF-16 to a UTF-8 file in Windows PowerShell 5 (found by the test)
        "    & $installer -InstallDir $dir -Yes -NoShortcut *>&1 | Out-File $log -Append -Encoding utf8",
        "} catch { \"update failed: $_\" | Out-File $log -Append -Encoding utf8 }",
        # one properly quoted command line: Start-Process joins a list with bare spaces, which breaks paths with spaces
        f"Start-Process -FilePath {q(pythonw)} -ArgumentList {q(subprocess.list2cmdline(list(args)))} -WorkingDirectory $dir",
    ]) + "\n"


def start_update(pid: int, layout: dict[str, str]) -> Path:
    """Write the script and start it detached (it outlives the app). Returns the script's path."""
    if layout.get("kind") != "installer":
        raise RuntimeError("only an installer copy updates itself; in a clone run: git pull, then pip install -r requirements.lock")
    script = Path(tempfile.gettempdir()) / f"omnibots-update-{pid}.ps1"
    script.write_text(updater_script(pid, layout["root"], layout["pythonw"]), encoding="utf-8")
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", str(script)],
                     creationflags=flags, close_fds=True)
    return script
