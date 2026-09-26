"""A3.99 live acceptance on REAL providers (spends a few thousand tokens).

Skipped unless OMNIBOTS_LIVE=1:
    set OMNIBOTS_LIVE=1 && python -m pytest tests/test_a3_live.py -v
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from omnibots.lineup import LINEUP_MODELS
from omnibots.omni import load_omni_config, locate_omni
from omnibots.providers.quota import QuotaManager
from omnibots.providers.router import Router
from omnibots.providers.seats import SeatScheduler
from omnibots.runtime.agent import BotAgent
from omnibots.runtime.approvals import ApprovalCenter
from omnibots.runtime.core_tools import core_registry
from omnibots.runtime.events import BotEvents
from omnibots.runtime.sandbox import Sandbox

pytestmark = pytest.mark.skipif(os.environ.get("OMNIBOTS_LIVE") != "1", reason="live provider test; set OMNIBOTS_LIVE=1")

HELLO = "Create hello.py that prints 'Hello from Omi' and then the sum of 2+3, run it with run_python, and report the exact output."


def make_bot(tmp: Path, provider: str, listener=None):
    cfg = load_omni_config(locate_omni())
    seen = []

    def listen(e):
        seen.append(e)
        if listener:
            listener(e)
    approvals = ApprovalCenter()
    bot = BotAgent(bot_id=f"live_{provider.replace('.', '_')}", name="Omi-Live", role="coder", workspace=tmp / "ws",
                   router=Router(lambda: cfg, QuotaManager(), SeatScheduler()), chain=[LINEUP_MODELS[provider]],
                   tools=core_registry(), approvals=approvals, events=BotEvents("live", None, listener=listen),
                   sandbox=Sandbox(tmp / "sandbox"), max_iterations=12)
    return bot, approvals, seen


@pytest.mark.parametrize("provider", ["minimax.io", "nvidia"])
def test_bot_writes_runs_and_reports(tmp_path, provider):
    bot, _, seen = make_bot(tmp_path, provider)
    res = asyncio.run(bot.run(HELLO))
    assert res.status == "done", res.error
    assert (tmp_path / "ws" / "hello.py").is_file()
    terminal = [e["content"] for e in seen if e["kind"] == "terminal"]
    assert "Hello from Omi" in terminal and "5" in terminal
    assert "Hello from Omi" in res.answer and "5" in res.answer


def test_steering_mid_run_changes_the_result(tmp_path):
    holder = {}

    def steer_after_first_write(e):
        if e["kind"] == "console" and e["content"].startswith("▸ write") and not holder.get("sent"):
            holder["sent"] = True
            holder["bot"].steer("Change of plan: greet.py must print exactly 'hello from Omi' instead. Update the file and run it again.")
    bot, _, seen = make_bot(tmp_path, "minimax.io", steer_after_first_write)
    holder["bot"] = bot
    res = asyncio.run(bot.run("Create greet.py that prints 'hello', then run it with run_python and report the output."))
    assert res.status == "done" and holder.get("sent")
    assert "hello from Omi" in (tmp_path / "ws" / "greet.py").read_text(encoding="utf-8")
    assert any(e["kind"] == "console" and e["content"].startswith("↳ steer received") for e in seen)


def test_r3_action_waits_for_approval(tmp_path):
    outside = tmp_path / "users_documents" / "omi_note.txt"

    async def go():
        bot, approvals, seen = make_bot(tmp_path, "minimax.io")
        task = asyncio.create_task(bot.run(f"Write the text 'approved by the user' to the file {outside} (an absolute path outside your workspace) using write_file, then report."))
        for _ in range(600):
            await asyncio.sleep(0.1)
            if approvals.list_pending():
                break
        pending = approvals.list_pending()
        parked = bool(pending) and not outside.exists()
        await asyncio.sleep(2)                                    # still parked, not spinning
        still = not task.done() and not outside.exists()
        await approvals.decide(pending[0]["id"], True, "test approves")
        res = await asyncio.wait_for(task, 180)
        return pending, parked, still, res
    pending, parked, still, res = asyncio.run(go())
    assert parked and still and pending[0]["risk"] == "R3"
    assert res.status == "done" and outside.read_text(encoding="utf-8").strip() == "approved by the user"
