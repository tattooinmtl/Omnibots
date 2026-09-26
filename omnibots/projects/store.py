"""Projects (PLAN.md A6.a.01): every goal gets a folder that is a git repo.

Bots working on the project share that folder (with leases, A4). Every change
is committed, so the reviewer and the Ledger can point at exact diffs, and
nothing a bot does is ever lost.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import uuid
from pathlib import Path
from typing import Any

from omnibots.board.types import topic_project

GIT_ENV = {"GIT_AUTHOR_NAME": "OmniBots", "GIT_AUTHOR_EMAIL": "bots@omnibots.local",
           "GIT_COMMITTER_NAME": "OmniBots", "GIT_COMMITTER_EMAIL": "bots@omnibots.local"}


def _git(folder: Path, *args: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Bots write into project folders, so git here runs hardened (hooks off, config checked, A9.a.03).
    A project whose .git/config was tampered with gets a failed git result (logged), never a hook run."""
    import logging
    from omnibots.runtime.safegit import GitUnsafe, git
    try:
        return git(folder, "-c", "init.defaultBranch=main", *args, timeout=60, env_extra={**GIT_ENV, **(env_extra or {})})
    except GitUnsafe as exc:
        logging.getLogger(__name__).warning("git refused in %s: %s", folder, exc)
        return subprocess.CompletedProcess(["git", *args], 1, "", f"refused: {exc}")


class ProjectStore:
    def __init__(self, db, projects_dir: Path, bus=None):
        self.db, self.dir, self.bus = db, projects_dir, bus

    def folder(self, project_id: str) -> Path:
        return self.dir / project_id

    async def create(self, goal: str, *, created_by: str = "user", budget: dict[str, Any] | None = None) -> str:
        goal = (goal or "").strip()
        if not goal:
            raise ValueError("a project needs a goal")
        pid = f"proj_{uuid.uuid4().hex[:10]}"
        folder = self.folder(pid)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "GOAL.md").write_text(f"# Goal\n\n{goal}\n", encoding="utf-8", newline="\n")
        await asyncio.to_thread(self._init_repo, folder)
        await self.db.write("INSERT INTO projects (id, goal, status, path, budget_json, created_by) VALUES (?,?,?,?,?,?)",
                            (pid, goal, "open", str(folder), json.dumps(budget or {}), created_by))
        if self.bus:
            await self.bus.publish(topic_project(pid), "TASK_RECEIVED", {"text": goal}, sender_type="user" if created_by == "user" else "bot",
                                   sender_id=created_by, project_id=pid)
        return pid

    @staticmethod
    def _init_repo(folder: Path) -> None:
        r = _git(folder, "init", "-q")
        if r.returncode != 0:
            raise RuntimeError(f"git init failed: {r.stderr.strip()}")
        _git(folder, "add", "-A")
        _git(folder, "commit", "-q", "-m", "project created")

    async def commit(self, project_id: str, message: str, author: str = "omnibots") -> str | None:
        """Commit everything that changed. Returns the new commit id, or None if nothing changed."""
        folder = self.folder(project_id)

        def work() -> str | None:
            _git(folder, "add", "-A")
            if _git(folder, "diff", "--cached", "--quiet").returncode == 0:
                return None
            r = _git(folder, "commit", "-q", "-m", message, env_extra={"GIT_AUTHOR_NAME": author})
            if r.returncode != 0:
                raise RuntimeError(f"git commit failed: {r.stderr.strip()}")
            return _git(folder, "rev-parse", "HEAD").stdout.strip()
        return await asyncio.to_thread(work)

    async def diff(self, project_id: str, since: str | None = None) -> str:
        """What changed since a commit (default: since the project was created)."""
        folder = self.folder(project_id)

        def work() -> str:
            base = since or _git(folder, "rev-list", "--max-parents=0", "HEAD").stdout.strip()
            return _git(folder, "diff", base, "HEAD").stdout
        return await asyncio.to_thread(work)

    async def log(self, project_id: str, n: int = 20) -> list[dict[str, str]]:
        r = await asyncio.to_thread(_git, self.folder(project_id), "log", f"-{n}", "--pretty=%H%x1f%an%x1f%s")
        return [dict(zip(("sha", "author", "message"), l.split("\x1f"))) for l in r.stdout.splitlines() if l]

    # the reviewer's latest verdict on a project is PASS (the proof the goal was met); SQL, so startup can reuse it
    PASSED_SQL = "(SELECT payload_json FROM messages WHERE project_id={pid} AND message_type='REVIEW_RESULT' ORDER BY id DESC LIMIT 1) LIKE '%VERDICT PASS%'"

    async def review_passed(self, project_id: str) -> bool:
        row = await self.db.read_one("SELECT " + self.PASSED_SQL.format(pid="?") + " AS ok", (project_id,))
        return bool(row and row["ok"])

    async def set_status(self, project_id: str, status: str) -> None:
        extra = ", finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')" if status in ("done", "failed", "cancelled") else ""
        await self.db.write(f"UPDATE projects SET status=?{extra} WHERE id=?", (status, project_id))
