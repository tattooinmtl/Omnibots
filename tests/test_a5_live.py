"""A5.99 live: a real job updates memory.md + SQL, and a real cheap-lane model
summarizes an over-limit memory. Skipped unless OMNIBOTS_LIVE=1."""

from __future__ import annotations

import asyncio
import os

import pytest

from omnibots.board.bus import MessageBus
from omnibots.board.ledger import Ledger
from omnibots.bots.profile import BotRegistry
from omnibots.bots.runner import JobRunner
from omnibots.db import Database
from omnibots.lineup import CHEAP_FIRST, LINEUP_MODELS
from omnibots.omni import load_omni_config, locate_omni
from omnibots.providers.quota import QuotaManager
from omnibots.providers.router import Router
from omnibots.providers.seats import SeatScheduler
from omnibots.runtime.approvals import ApprovalCenter
from omnibots.runtime.sandbox import Sandbox

pytestmark = pytest.mark.skipif(os.environ.get("OMNIBOTS_LIVE") != "1", reason="live provider test; set OMNIBOTS_LIVE=1")


def test_real_job_updates_memory_and_sql_and_memory_gets_summarized(tmp_path):
    async def go():
        db = Database(tmp_path / "db" / "omnibots.sqlite")
        await db.open()
        bus = MessageBus(db)
        reg = BotRegistry(db, tmp_path / "bots", bus)
        cfg = load_omni_config(locate_omni())
        runner = JobRunner(db=db, registry=reg, router=Router(lambda: cfg, QuotaManager(db), SeatScheduler(boss_id="omi")),
                           approvals=ApprovalCenter(db), home=tmp_path, sandbox=Sandbox(tmp_path / "sandbox"), bus=bus,
                           ledger=Ledger(db, bus), memory_limit=8 * 1024, lesson_chain=list(CHEAP_FIRST))
        bot = await reg.create("Live-Coder", "coder", chain=[LINEUP_MODELS["minimax.io"]])
        for i in range(150):                                # an old, long history -> over the 8 KB test limit
            bot.memory.add_job(f"job_old_{i}", f"refactor module {i % 7} and fix the flaky test", "completed" if i % 5 else "failed")
        before = bot.memory.size()
        out = await runner.run(bot.id, "Create squares.py that prints the squares of 1..5 on one line, run it, and report the output.")
        job = await db.read_one("SELECT status, result_summary FROM jobs WHERE id=?", (out.job_id,))
        stats = await runner.stats(bot.id)
        await db.close()
        return out, dict(job), stats, before, bot
    out, job, stats, before, bot = asyncio.run(go())
    assert out.status == "completed" and job["status"] == "completed" and "25" in job["result_summary"]
    text = bot.memory.path.read_text(encoding="utf-8")
    assert f"{out.job_id}: Create squares.py" in text and "[completed]" in text
    assert before > 8 * 1024 >= bot.memory.size()                           # summarized under the limit
    assert "(summary of" in text and "folded without a model" not in text   # a real model wrote the summary
    assert stats["tokens"] > 0 and stats["model_calls"] >= 2 and stats["success_rate"] == 1.0
