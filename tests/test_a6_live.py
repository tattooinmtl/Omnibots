"""A6.99 live: a real 5-job DAG run by 2 real MiniMax bots in one shared git
project, plus a real failure that retries then escalates. Skipped unless OMNIBOTS_LIVE=1."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from omnibots.board.bus import MessageBus
from omnibots.board.ledger import Ledger
from omnibots.board.query import query
from omnibots.bots.profile import BotRegistry
from omnibots.bots.runner import JobRunner
from omnibots.db import Database
from omnibots.lineup import LINEUP_MODELS
from omnibots.omni import load_omni_config, locate_omni
from omnibots.projects.graph import PlanRunner, TaskGraph
from omnibots.projects.store import ProjectStore
from omnibots.providers.quota import QuotaManager
from omnibots.providers.router import Router
from omnibots.providers.seats import SeatScheduler
from omnibots.runtime.approvals import ApprovalCenter
from omnibots.runtime.sandbox import Sandbox

pytestmark = pytest.mark.skipif(os.environ.get("OMNIBOTS_LIVE") != "1", reason="live provider test; set OMNIBOTS_LIVE=1")


def test_real_dag_across_two_bots(tmp_path):
    async def go():
        db = Database(tmp_path / "db" / "omnibots.sqlite")
        await db.open()
        bus = MessageBus(db)
        reg = BotRegistry(db, tmp_path / "bots", bus)
        cfg = load_omni_config(locate_omni())
        router = Router(lambda: cfg, QuotaManager(db), SeatScheduler(boss_id="omi"), max_retries=1, base_delay=1)
        runner = JobRunner(db=db, registry=reg, router=router, approvals=ApprovalCenter(db), home=tmp_path,
                           sandbox=Sandbox(tmp_path / "sandbox"), bus=bus, ledger=Ledger(db, bus), learn=False)
        projects, graph = ProjectStore(db, tmp_path / "projects", bus), TaskGraph(db, bus)
        mm = [LINEUP_MODELS["minimax.io"]]
        b1 = await reg.create("Builder-1", "data worker", chain=mm)
        b2 = await reg.create("Builder-2", "data worker", chain=mm)
        broken = await reg.create("Broken", "worker", chain=["openrouter::omnibots/this-model-does-not-exist"])
        pid = await projects.create("People report")
        A = await graph.add_job(pid, "names", description="Write names.txt containing exactly three lines: Alice, Bob, Carol.", assigned_bot_id=b1.id)
        B = await graph.add_job(pid, "ages", description="Write ages.txt containing exactly three lines: 30, 25, 35.", assigned_bot_id=b2.id)
        C = await graph.add_job(pid, "combine", depends_on=[A.id, B.id], assigned_bot_id=b1.id,
                                description="Read names.txt and ages.txt (line i of one belongs to line i of the other) and write people.csv with header name,age and one row per person, in the same order.")
        D = await graph.add_job(pid, "average", depends_on=[C.id], assigned_bot_id=b2.id,
                                description="Compute the average age from people.csv with run_python (do not do the arithmetic yourself) and write only the number to avg.txt.")
        E = await graph.add_job(pid, "report", depends_on=[C.id], assigned_bot_id=b1.id,
                                description="Write report.md: a markdown list of the people from people.csv sorted by age, youngest first, one '- Name (age)' line each. Use run_python to sort.")
        F = await graph.add_job(pid, "doomed", description="Say hello.", assigned_bot_id=broken.id, budget={"max_attempts": 2})
        G = await graph.add_job(pid, "after doomed", depends_on=[F.id], description="Say goodbye.", assigned_bot_id=b2.id)
        await PlanRunner(graph, runner.run, workspace_for=projects.folder).run(pid)
        sha = await projects.commit(pid, "team result")
        rows = {r["title"]: dict(r) for r in await db.read("SELECT title, status, attempts, started_at, finished_at FROM jobs WHERE project_id=?", (pid,))}
        questions = [m for m in await query(db, project=pid) if m.message_type == "QUESTION"]
        await db.close()
        return projects.folder(pid), rows, questions, sha
    folder, rows, questions, sha = asyncio.run(go())
    for t in ("names", "ages", "combine", "average", "report"):
        assert rows[t]["status"] == "completed", (t, rows[t])
    assert rows["combine"]["started_at"] >= max(rows["names"]["finished_at"], rows["ages"]["finished_at"])
    assert min(rows["average"]["started_at"], rows["report"]["started_at"]) >= rows["combine"]["finished_at"]
    assert float((folder / "avg.txt").read_text(encoding="utf-8").strip()) == pytest.approx(30.0)
    report = (folder / "report.md").read_text(encoding="utf-8")
    assert report.index("Bob") < report.index("Alice") < report.index("Carol")
    assert rows["doomed"]["status"] == "blocked" and rows["doomed"]["attempts"] == 2
    assert rows["after doomed"]["status"] == "blocked" and rows["after doomed"]["started_at"] is None
    assert len(questions) == 1 and sha
