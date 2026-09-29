"""Only what the app needs is published (user, 2026-09-29): README.md is the one doc, no mockups or
screenshots, no website, and nothing that holds keys or user data. GitGuardian scans every PR for keys;
this catches the files that must never be tracked in the first place."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def tracked() -> list[str]:
    if not shutil.which("git") or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [line for line in out.splitlines() if line]


def test_readme_is_the_only_doc():
    docs = [f for f in tracked() if f.lower().endswith(".md") and not f.startswith("tests/injection/")]
    assert docs == ["README.md"]


def test_no_mockups_screenshots_or_website():
    images = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4")
    stray = [f for f in tracked() if f.lower().endswith(images) and not f.startswith("omnibots/ui/assets/")]
    assert stray == []
    assert not [f for f in tracked() if f.startswith(("website/", "docs/"))]


def test_nothing_that_holds_keys_or_user_data():
    bad = [f for f in tracked()
           if Path(f).name in (".env", "settings.toml", "settings.json", "user_profile.md")
           or f.startswith(("config/", "db/", "bots/", "projects/", "sessions/", "logs/", "profiles/", "backups/"))
           or f.endswith((".sqlite", ".pem", ".pfx", ".p12"))]
    assert bad == []
