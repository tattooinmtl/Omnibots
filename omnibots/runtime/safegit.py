"""Running git on folders bots can write to, without letting a bot escape the sandbox
(PLAN.md A9.a.03).

Git can't run inside the AppContainer (Windows can't resolve the normalized
working-directory path there; git treats that as fatal), so the git tools and the
project store run the trusted git binary OUTSIDE the sandbox. But a bot can write
into `.git/` from inside the sandbox, and git executes commands named in its config
and hooks. So every run here:
  - disables hooks (core.hooksPath -> an empty folder OmniBots owns),
  - refuses a repo whose .git/config names anything that can run a command
    (fsmonitor, sshCommand, editor/pager, filter/diff/merge drivers, credential
    helpers, aliases, includes, gpg, ...),
  - ignores the system config, never prompts, never pages.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

# Config keys that make git run a program (or pull in other config that might).
RISKY_KEY = re.compile(
    r"^(core\.(fsmonitor|sshcommand|editor|pager|hookspath|askpass|gitproxy|alternaterefscommand)"
    r"|sequence\.editor|diff\..*\.(command|textconv)|diff\.external|merge\..*\.driver|filter\..*"
    r"|credential\..*|alias\..*|include\..*|includeif\..*|gpg\..*|ssh\..*|uploadpack\..*|receivepack\..*"
    r"|remote\..*\.(uploadpack|receivepack|vcs)|protocol\.ext\..*|url\..*\.insteadof|http\..*\.(proxy|sslcainfo)"
    r"|pager\..*|web\.browser|browser\..*|man\..*|instaweb\..*|safe\.directory)$", re.I)

_EMPTY_HOOKS = Path(tempfile.gettempdir()) / "omnibots-empty-hooks"


class GitUnsafe(RuntimeError):
    pass


def _git_dir(repo: Path) -> Path | None:
    g = repo / ".git"
    if g.is_dir():
        return g
    if g.is_file():                     # a gitlink ("gitdir: ...") can point anywhere: not allowed
        raise GitUnsafe(f"{repo} has a .git FILE (a gitlink), which OmniBots won't follow")
    return None


def check_repo(repo: Path, git_dir: Path | None = None) -> None:
    """Raise GitUnsafe if the repo's own config could make git run a command.
    With `git_dir` (a history folder kept outside the work tree), that folder's config is checked."""
    g = git_dir if git_dir is not None else _git_dir(repo)
    if g is None or not g.exists():
        return
    cfg = g / "config"
    if not cfg.exists():
        return
    out = subprocess.run(["git", "config", "--file", str(cfg), "--name-only", "--list"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
                         env=_env())
    if out.returncode != 0:
        raise GitUnsafe(f"can't read {cfg}: {out.stderr.strip()[:200]}")
    bad = sorted({k for k in out.stdout.split() if RISKY_KEY.match(k)})
    if bad:
        raise GitUnsafe(f"the repository config sets {', '.join(bad)}, which can make git run programs; "
                        "OmniBots won't run git here (remove those settings to continue)")


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0", GIT_PAGER="cat", PAGER="cat", GIT_EDITOR="true",
               GIT_ASKPASS="", SSH_ASKPASS="")
    return env


def git(repo: Path, *args: str, timeout: float = 120, env_extra: dict[str, str] | None = None,
        git_dir: Path | None = None) -> subprocess.CompletedProcess:
    """Run git in `repo` with hooks off and a checked config. Raises GitUnsafe.
    `git_dir`: keep the history in that folder instead of `repo/.git` (a folder the user opened:
    their files, and any git repo of their own there, are never touched by OmniBots' commits)."""
    check_repo(repo, git_dir)
    if git_dir is not None:
        args = ("--git-dir", str(git_dir), "--work-tree", str(repo), *args)
    _EMPTY_HOOKS.mkdir(exist_ok=True)
    cmd = ["git", "-c", f"core.hooksPath={_EMPTY_HOOKS.as_posix()}", "-c", "core.fsmonitor=false",
           "-c", "core.autocrlf=false", "-c", "protocol.ext.allow=never", "--no-pager", *args]
    return subprocess.run(cmd, cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, env={**_env(), **(env_extra or {})})
