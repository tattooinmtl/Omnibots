"""Projects (PLAN.md A6.a.01, A11.m.02-03): every goal gets a folder whose changes git tracks.

Bots working on the project share that folder (with leases, A4). Every change
is committed, so the reviewer and the Ledger can point at exact diffs, and
nothing a bot does is ever lost.

Where: `<output folder>/<YYYY-MM-DD> <goal words>` (the user's output folder, A11.m.01),
with the history in its own `.git`. Or a folder the user opened (File → Open folder):
then the history lives in `~/.omnibots/project-history/<id>.git`, so the user's folder
gets no `.git` from us and a repo of their own there is never committed to.
The path is stored in `projects.path`; `folder()` reads it.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from omnibots.board.types import topic_project

GIT_ENV = {"GIT_AUTHOR_NAME": "OmniBots", "GIT_AUTHOR_EMAIL": "bots@omnibots.local",
           "GIT_COMMITTER_NAME": "OmniBots", "GIT_COMMITTER_EMAIL": "bots@omnibots.local"}


def _git(folder: Path, *args: str, env_extra: dict[str, str] | None = None, git_dir: Path | None = None) -> subprocess.CompletedProcess:
    """Bots write into project folders, so git here runs hardened (hooks off, config checked, A9.a.03).
    A project whose .git/config was tampered with gets a failed git result (logged), never a hook run."""
    import logging
    from omnibots.runtime.safegit import GitUnsafe, git
    try:
        return git(folder, "-c", "init.defaultBranch=main", *args, timeout=60, env_extra={**GIT_ENV, **(env_extra or {})},
                   git_dir=git_dir)
    except GitUnsafe as exc:
        logging.getLogger(__name__).warning("git refused in %s: %s", folder, exc)
        return subprocess.CompletedProcess(["git", *args], 1, "", f"refused: {exc}")


def folder_name(goal: str, when: float | None = None) -> str:
    """'2026-09-26 omnibots showcase site': the date, then the goal's first words (safe on Windows)."""
    words = re.sub(r"[^\w\s-]", " ", (goal or "").splitlines()[0] if goal else "")
    words = " ".join(words.split()[:8])[:60].strip(" .-") or "project"
    return f"{time.strftime('%Y-%m-%d', time.localtime(when))} {words}"


def unique_folder(parent: Path, name: str) -> Path:
    p, n = parent / name, 2
    while p.exists():
        p, n = parent / f"{name} ({n})", n + 1
    return p


class ProjectStore:
    def __init__(self, db, projects_dir: Path, bus=None, *, output_dir: Path | None = None, history_dir: Path | None = None):
        self.db, self.dir, self.bus = db, projects_dir, bus
        self.out = Path(output_dir) if output_dir else projects_dir           # where new goals' folders go
        self.history = history_dir or projects_dir.parent / "project-history"   # histories of folders the user opened
        self._paths: dict[str, Path] = {}

    async def load(self) -> None:
        """Remember every project's folder (call once at startup)."""
        for r in await self.db.read("SELECT id, path FROM projects"):
            if r["path"]:
                self._paths[r["id"]] = Path(r["path"])

    def folder(self, project_id: str) -> Path:
        return self._paths.get(project_id) or self.dir / project_id

    def is_ours(self, folder: Path) -> bool:
        """A project folder OmniBots made (in the output folder, with its own .git), not a user's folder."""
        folder = Path(folder)
        try:
            inside = folder.resolve().is_relative_to(self.out.resolve()) or folder.resolve().is_relative_to(self.dir.resolve())
        except OSError:
            return False
        return inside and (folder / ".git").is_dir()

    def git_dir(self, project_id: str) -> Path | None:
        """The history folder of a project in a folder the user opened (None: it's the folder's own .git)."""
        g = self.history / f"{project_id}.git"
        return g if g.is_dir() else None

    async def create(self, goal: str, *, created_by: str = "user", budget: dict[str, Any] | None = None,
                     folder: Path | None = None) -> str:
        """A new project. `folder`: work in that existing folder (File → Open folder, A11.m.03)."""
        goal = (goal or "").strip()
        if not goal:
            raise ValueError("a project needs a goal")
        pid = f"proj_{uuid.uuid4().hex[:10]}"
        if folder is None:
            folder = unique_folder(self.out, folder_name(goal))
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "GOAL.md").write_text(f"# Goal\n\n{goal}\n", encoding="utf-8", newline="\n")
            await asyncio.to_thread(self._init_repo, folder, None)
        else:
            folder = Path(folder)
            if not folder.is_dir():
                raise ValueError(f"{folder} is not a folder")
            if self.is_ours(folder):
                # a follow-up in one of OUR project folders: same folder, same history; the goal is added to GOAL.md
                with (folder / "GOAL.md").open("a", encoding="utf-8", newline="\n") as f:
                    f.write(f"\n## Follow-up ({time.strftime('%Y-%m-%d %H:%M')})\n\n{goal}\n")
                await asyncio.to_thread(lambda: (_git(folder, "add", "-A"), _git(folder, "commit", "-q", "-m", "follow-up goal")))
            else:
                self.history.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(self._init_repo, folder, self.history / f"{pid}.git")
        self._paths[pid] = folder
        await self.db.write("INSERT INTO projects (id, goal, status, path, budget_json, created_by) VALUES (?,?,?,?,?,?)",
                            (pid, goal, "open", str(folder), json.dumps(budget or {}), created_by))
        if self.bus:
            await self.bus.publish(topic_project(pid), "TASK_RECEIVED", {"text": goal}, sender_type="user" if created_by == "user" else "bot",
                                   sender_id=created_by, project_id=pid)
        return pid

    @staticmethod
    def _init_repo(folder: Path, git_dir: Path | None) -> None:
        if git_dir is not None:
            r = subprocess.run(["git", "init", "-q", "--bare", str(git_dir)], capture_output=True, text=True, timeout=60)
            if r.returncode == 0:
                r = _git(folder, "config", "core.bare", "false", git_dir=git_dir)
        else:
            r = _git(folder, "init", "-q")
        if r.returncode != 0:
            raise RuntimeError(f"git init failed: {r.stderr.strip()}")
        _git(folder, "add", "-A", git_dir=git_dir)
        msg = "project created" if git_dir is None else "project created (the folder as it was)"
        _git(folder, "commit", "-q", "--allow-empty", "-m", msg, git_dir=git_dir)

    async def move_old_projects(self) -> list[tuple[str, Path]]:
        """A11.m.02: projects still under ~/.omnibots/projects/<id> move to the output folder with
        readable names (once; nothing deleted, git history moves with them)."""
        if self.out == self.dir:
            return []
        moved = []
        for r in await self.db.read("SELECT id, goal, path, created_at FROM projects"):
            old = Path(r["path"]) if r["path"] else self.dir / r["id"]
            if old.parent != self.dir or not old.is_dir():
                continue
            when = time.mktime(time.strptime(r["created_at"][:10], "%Y-%m-%d")) if r["created_at"] else None
            new = unique_folder(self.out, folder_name(r["goal"], when))
            self.out.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(shutil.move, str(old), str(new))
            await self.db.write("UPDATE projects SET path=? WHERE id=?", (str(new), r["id"]))
            self._paths[r["id"]] = new
            moved.append((r["id"], new))
        return moved

    async def commit(self, project_id: str, message: str, author: str = "omnibots") -> str | None:
        """Commit everything that changed. Returns the new commit id, or None if nothing changed."""
        folder, gd = self.folder(project_id), self.git_dir(project_id)

        def work() -> str | None:
            _git(folder, "add", "-A", git_dir=gd)
            if _git(folder, "diff", "--cached", "--quiet", git_dir=gd).returncode == 0:
                return None
            r = _git(folder, "commit", "-q", "-m", message, env_extra={"GIT_AUTHOR_NAME": author}, git_dir=gd)
            if r.returncode != 0:
                raise RuntimeError(f"git commit failed: {r.stderr.strip()}")
            return _git(folder, "rev-parse", "HEAD", git_dir=gd).stdout.strip()
        return await asyncio.to_thread(work)

    async def diff(self, project_id: str, since: str | None = None) -> str:
        """What changed since a commit (default: since the project was created)."""
        folder, gd = self.folder(project_id), self.git_dir(project_id)

        def work() -> str:
            base = since or _git(folder, "rev-list", "--max-parents=0", "HEAD", git_dir=gd).stdout.strip()
            return _git(folder, "diff", base, "HEAD", git_dir=gd).stdout
        return await asyncio.to_thread(work)

    async def log(self, project_id: str, n: int = 20) -> list[dict[str, str]]:
        r = await asyncio.to_thread(lambda: _git(self.folder(project_id), "log", f"-{n}", "--pretty=%H%x1f%an%x1f%s",
                                                 git_dir=self.git_dir(project_id)))
        return [dict(zip(("sha", "author", "message"), l.split("\x1f"))) for l in r.stdout.splitlines() if l]

    # the reviewer's latest verdict on a project is PASS (the proof the goal was met); SQL, so startup can reuse it
    PASSED_SQL = "(SELECT payload_json FROM messages WHERE project_id={pid} AND message_type='REVIEW_RESULT' ORDER BY id DESC LIMIT 1) LIKE '%VERDICT PASS%'"

    async def review_passed(self, project_id: str) -> bool:
        row = await self.db.read_one("SELECT " + self.PASSED_SQL.format(pid="?") + " AS ok", (project_id,))
        return bool(row and row["ok"])

    async def set_status(self, project_id: str, status: str) -> None:
        extra = ", finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')" if status in ("done", "failed", "cancelled") else ""
        await self.db.write(f"UPDATE projects SET status=?{extra} WHERE id=?", (status, project_id))
