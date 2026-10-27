"""Walking a bot's folder without leaving it (security audit 2026-10-27).

A bot can make a junction or symlink inside its workspace (`mklink /J link C:\\Users\\me\\.ssh` needs no
privilege, even from the sandbox). `Path.rglob` walks into it, and a host-side tool that reads what it
finds (grep, find_files, deploy_site) would then read or publish files outside the workspace. `files_inside`
never enters a linked folder and drops any file whose real path is outside `workspace`.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Iterator


def is_link(path: Path) -> bool:
    """A symlink or a Windows junction (any reparse point that redirects)."""
    try:
        return path.is_symlink() or path.is_junction()
    except OSError:
        return True


def inside(path: Path, workspace: Path) -> bool:
    """The real path (links followed) is the workspace or under it."""
    try:
        return path.resolve().is_relative_to(workspace.resolve())
    except (OSError, ValueError):
        return False


def files_inside(root: Path, workspace: Path, pattern: str = "*", skip_dirs: set[str] | frozenset[str] = frozenset()) -> Iterator[Path]:
    """Every regular file under `root` whose name matches `pattern`, sorted per folder, never following a link
    out of `workspace`."""
    ws = workspace.resolve()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        dirnames[:] = sorted(d for d in dirnames if d not in skip_dirs and not is_link(here / d))
        for name in sorted(filenames):
            p = here / name
            if fnmatch.fnmatch(name, pattern) and not is_link(p) and p.is_file() and inside(p, ws):
                yield p
