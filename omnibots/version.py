"""Version, build and credits (user, 2026-09-26: "add a version control to app in the about page also Erik Boivin
with email erik.boivin@proton.me and copyrights to omnibots.globalwarningnetworks.com show the version number").

  VERSION        the release number, from omnibots/__init__.py (one source; pyproject.toml must match, a test checks)
  build()        the git commit and date this copy runs from ("" when not a git checkout)
  latest()       the version on GitHub's main branch (About → Check for updates; the website reads the same file)
"""

from __future__ import annotations

import re
import subprocess
from functools import lru_cache
from pathlib import Path

from omnibots import __version__ as VERSION

AUTHOR = "Erik Boivin"
EMAIL = "erik.boivin@proton.me"
WEBSITE = "https://omnibots.globalwarningnetworks.com"
COPYRIGHT = "© 2026 omnibots.globalwarningnetworks.com. All rights reserved."
REPO = "tattooinmtl/Omnibots"
BRANCHES = ("main", "master")                          # the GitHub default branch (whichever exists)
ROOT = Path(__file__).resolve().parents[1]


@lru_cache(maxsize=1)
def build() -> dict[str, str]:
    """{'commit': 'f9b1312', 'date': '2026-09-26', 'dirty': '+changes'} from git, or {} outside a checkout."""
    if not (ROOT / ".git").exists():
        return {}

    def git(*a: str) -> str:
        r = subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    try:
        commit = git("rev-parse", "--short", "HEAD")
        date = git("log", "-1", "--format=%cs")
        dirty = "+changes" if git("status", "--porcelain", "--untracked-files=no") else ""
    except (OSError, subprocess.SubprocessError):
        return {}
    return {"commit": commit, "date": date, "dirty": dirty} if commit else {}


def version_line() -> str:
    b = build()
    extra = f" (build {b['commit']}{' ' + b['dirty'] if b.get('dirty') else ''}, {b['date']})" if b else ""
    return f"OmniBots v{VERSION}{extra}"


def parse_version(text: str) -> str | None:
    m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', text)
    return m.group(1) if m else None


def newer(a: str, b: str) -> bool:
    """Is version a newer than b? (1.10.0 > 1.9.2; a trailing text part is ignored)"""
    def parts(v: str) -> tuple[int, ...]:
        return tuple(int(x) for x in re.findall(r"\d+", v.split("-")[0])[:3])
    return parts(a) > parts(b)


def latest(timeout: float = 8.0) -> str | None:
    """The version on GitHub (the default branch's omnibots/__init__.py). None when offline/unreachable."""
    import httpx
    for branch in BRANCHES:
        try:
            r = httpx.get(f"https://raw.githubusercontent.com/{REPO}/{branch}/omnibots/__init__.py", timeout=timeout,
                          follow_redirects=True)
        except httpx.HTTPError:
            return None
        if r.status_code == 200:
            return parse_version(r.text)
    return None
