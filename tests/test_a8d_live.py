"""A8.d.99 live: with REAL models, a bot without a shell gets a shell command's output through the tool
relay (it finds request_tool by itself; the test plays Omi's relay_tool), and a brand-new bot made with no
tool list can search the web. Skipped unless OMNIBOTS_LIVE=1 (a few thousand tokens)."""

from __future__ import annotations

import asyncio
import os

import pytest

from omnibots.board.bus import MessageBus
from omnibots.board.ledger import Ledger
from omnibots.bots.profile import DEFAULT_TOOLS, BotRegistry
from omnibots.bots.runner import JobRunner
from omnibots.db import Database
from omnibots.lineup import LINEUP_MODELS
from omnibots.omni import load_omni_config, locate_omni
from omnibots.orchestrator.factory import BotFactory, GovernorLimits, SpawnGovernor
from omnibots.orchestrator.relay import RelayDesk
from omnibots.providers.quota import QuotaManager
from omnibots.providers.router import Router
from omnibots.providers.seats import SeatScheduler
from omnibots.runtime.approvals import ApprovalCenter
from omnibots.runtime.sandbox import Sandbox

pytestmark = pytest.mark.skipif(os.environ.get("OMNIBOTS_LIVE") != "1", reason="live provider test; set OMNIBOTS_LIVE=1")


async def setup(tmp_path):
    db = Database(tmp_path / "db" / "omnibots.sqlite")
    await db.open()
    bus = MessageBus(db)
    reg = BotRegistry(db, tmp_path / "bots", bus)
    cfg = load_omni_config(locate_omni())
    runner = JobRunner(db=db, registry=reg, router=Router(lambda: cfg, QuotaManager(db), SeatScheduler(boss_id="omi")),
                       approvals=ApprovalCenter(db, ask_from="R4"), home=tmp_path, sandbox=Sandbox(tmp_path / "sandbox"), bus=bus,
                       ledger=Ledger(db, bus))
    runner.relay = RelayDesk(db=db, bus=bus, registry=reg, runner=runner, home=tmp_path, wait_seconds=300)
    return db, reg, runner


def test_a_bot_without_a_shell_gets_the_answer_through_the_relay(tmp_path):
    async def go():
        db, reg, runner = await setup(tmp_path)
        asker = await reg.create("Writer", "writer", chain=[LINEUP_MODELS["nvidia"]], tools=list(DEFAULT_TOOLS))
        holder = await reg.create("Shell", "devops", chain=[LINEUP_MODELS["minimax.io"]], tools=[*DEFAULT_TOOLS, "run_shell"], risk_ceiling="R3")
        # (a first try asked for `python --version` and the bot simply used run_python instead: resourceful, but it
        # skipped the relay, so this task needs the shell tool itself)
        task = asyncio.create_task(runner.run(asker.id, "I need the output of the Windows shell command `ver`, run by the shell "
                                                        "tool run_shell itself (not Python, not a guess). You don't have run_shell.",
                                              max_iterations=8))
        req = None
        for _ in range(600):                                           # up to ~2 min for the model to ask
            await asyncio.sleep(0.2)
            if runner.relay.waiting() or task.done():
                req = (runner.relay.waiting() or [None])[0]
                break
        relayed = await runner.relay.relay(req["id"], holder.id) if req else "no request"
        out = await asyncio.wait_for(task, 600)
        await db.close()
        return req, relayed, out
    req, relayed, out = asyncio.run(go())
    real = "Microsoft Windows"                                     # what `ver` prints
    print(ascii(f"request: {req and req['tool']} {req and req['args']} | relayed: {relayed} | answer: {out.result.answer[:300]}"))
    assert req is not None and req["tool"] == "run_shell", "the model never asked for the shell through request_tool"
    assert relayed.startswith("relayed") and out.status == "completed" and real in out.result.answer


def test_a_new_bot_with_no_tool_list_searches_the_web(tmp_path):
    async def go():
        db, reg, runner = await setup(tmp_path)
        factory = BotFactory(reg, SpawnGovernor(GovernorLimits(max_bots=5, max_spawn_per_goal=5, max_spawn_per_minute=5)), db=db)
        bot, _ = await factory.create(name="Scout", role="researcher", tools=None, lane="cheap")
        out = await runner.run(bot.id, "Search the web: what is the official website of the Python programming language? "
                                       "Answer with the URL.", max_iterations=6)
        used = [m for m in (out.result.messages if out.result else []) if m.get("role") == "tool" and "UNTRUSTED WEB CONTENT" in (m.get("content") or "")]
        await db.close()
        return bot, out, used
    bot, out, used = asyncio.run(go())
    print(ascii(f"tools: {bot.tools} | answer: {out.result.answer[:200]}"))
    assert "web_search" in bot.tools and used and "python.org" in out.result.answer.lower()
