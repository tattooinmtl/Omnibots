"""Build the Windows installer: dist/OmniBots-Setup-<installer>-<app>.exe (PLAN.md A17.99).

    python installer/build.py [--installer-version 1.0]

1. the code: a git bundle of HEAD as branch `master` (the installer installs exactly this commit; updates come
   from GitHub afterwards: install.ps1 points origin back there)
2. Version.cs: the installer's version and OmniBots' version (from omnibots/__init__.py)
3. csc.exe from the .NET Framework that ships with Windows compiles installer/OmniBotsSetup.cs with install.ps1,
   the bundle and Omi's icon embedded. Nothing to install to build it, nothing to install to run it.
Prints the file, its size and its SHA-256.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSC = Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe")


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--installer-version", default="1.0")
    a = ap.parse_args()
    app = re.search(r'__version__ = "([^"]+)"', (ROOT / "omnibots" / "__init__.py").read_text(encoding="utf-8")).group(1)
    if git("status", "--porcelain", "--untracked-files=no", cwd=ROOT):
        print("commit your changes first: the installer carries the committed code only", file=sys.stderr)
        return 1
    head = git("rev-parse", "HEAD", cwd=ROOT)
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="omnibots-installer-") as tmp:
        tmp = Path(tmp)
        clone = tmp / "repo"
        subprocess.run(["git", "clone", "--quiet", "--no-local", str(ROOT), str(clone)], check=True)
        git("checkout", "--quiet", "-B", "master", head, cwd=clone)
        bundle = tmp / "omnibots.bundle"
        git("bundle", "create", str(bundle), "master", cwd=clone)
        git("bundle", "verify", str(bundle), cwd=clone)
        (tmp / "Version.cs").write_text(
            "namespace OmniBotsSetup { public static partial class Info {\n"
            f'    public const string InstallerVersion = "{a.installer_version}";\n'
            f'    public const string AppVersion = "{app}";\n'
            f'    public const string Commit = "{head[:7]}";\n'
            "} }\n", encoding="utf-8")
        out = dist / f"OmniBots-Setup-{a.installer_version}-{app}.exe"
        icon = ROOT / "omnibots" / "ui" / "assets" / "omi.ico"
        cmd = [str(CSC), "/nologo", "/target:winexe", "/optimize+", f"/out:{out}", f"/win32icon:{icon}",
               "/r:System.Windows.Forms.dll", "/r:System.Drawing.dll",
               f"/resource:{ROOT / 'install.ps1'},install.ps1", f"/resource:{bundle},omnibots.bundle", f"/resource:{icon},omi.ico",
               str(ROOT / "installer" / "OmniBotsSetup.cs"), str(tmp / "Version.cs")]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print(r.stdout, r.stderr, file=sys.stderr)
            return r.returncode
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    (out.with_suffix(".exe.sha256")).write_text(f"{digest}  {out.name}\n", encoding="utf-8")
    print(f"{out}\n{out.stat().st_size / 1e6:.1f} MB\nSHA-256 {digest}\nOmniBots {app} ({head[:7]}), installer v{a.installer_version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
