"""A8.99 live, on the real providers (skipped unless OMNIBOTS_LIVE=1):
  - a bot finds and uses one of Omni's real skills
  - a bot calls an MCP tool from Omni's config (okf)
  - a bot without run_shell can't run a shell
  - a bot turns text into speech with MiniMax (Token Plan; video is never run live: it costs money)
  - a small goal ends with a retrospective (lessons + a playbook)"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from omnibots.board.bus import MessageBus
from omnibots.board.ledger import Ledger
from omnibots.board.locks import LeaseManager
from omnibots.bots.profile import DEFAULT_TOOLS, BotRegistry
from omnibots.bots.runner import JobRunner
from omnibots.db import Database
from omnibots.lineup import LINEUP_MODELS, MINIMAX
from omnibots.omni import load_omni_config, locate_omni
from omnibots.providers.quota import QuotaManager
from omnibots.providers.router import Router
from omnibots.providers.seats import SeatScheduler
from omnibots.runtime.approvals import ApprovalCenter
from omnibots.runtime.pools import runner_pools
from omnibots.runtime.sandbox import Sandbox

pytestmark = pytest.mark.skipif(os.environ.get("OMNIBOTS_LIVE") != "1", reason="live provider test; set OMNIBOTS_LIVE=1")
ROOT = Path(__file__).resolve().parents[1]


async def live_job(tmp: Path, tools: list[str], task: str):
    db = Database(tmp / "db" / "omnibots.sqlite")
    await db.open()
    bus = MessageBus(db)
    cfg = load_omni_config(locate_omni())
    router = Router(lambda: cfg, QuotaManager(db), SeatScheduler(boss_id="omi"))
    events: list[dict] = []
    pools = runner_pools(lambda: cfg, tmp, router, LeaseManager(db, bus), {"okf.okf_search": "R0", "okf.okf_list": "R0", "okf.okf_get": "R0"})
    runner = JobRunner(db=db, registry=BotRegistry(db, tmp / "bots", bus), router=router, approvals=ApprovalCenter(db), home=tmp,
                       sandbox=Sandbox(tmp / "sandbox"), bus=bus, ledger=Ledger(db, bus), listener=events.append, learn=False, **pools)
    bot = await runner.registry.create("Live", "assistant", chain=[LINEUP_MODELS[MINIMAX]], tools=tools)
    try:
        out = await asyncio.wait_for(runner.run(bot.id, task, max_iterations=14), 600)
    finally:
        await pools["mcp"].close()
        await db.close()
    console = [e["content"] for e in events if e["kind"] == "console" and not e["stream"]]
    terminal = [e["content"] for e in events if e["kind"] == "terminal"]
    print("\n".join(console))
    return out, bot, console, terminal


def test_live_bot_finds_and_uses_an_omni_skill(tmp_path):
    out, bot, console, _ = asyncio.run(live_job(tmp_path, list(DEFAULT_TOOLS),
        "Before writing any code, use find_skill to look for a skill about writing Python code, load the best match with "
        "invoke_skill, and follow it. Then create fizz.py that prints FizzBuzz for 1..15 and run it. "
        "In your final answer, name the skill you used."))
    assert any(l.startswith("▸ find_skill") for l in console)
    loaded = [l for l in console if "📘 skill loaded:" in l]
    assert loaded, "no skill was loaded"
    assert out.status == "completed" and (bot.workspace / "fizz.py").exists()
    assert loaded[0].split("skill loaded:")[1].strip() in out.result.answer


def test_live_bot_calls_the_okf_mcp_tool(tmp_path):
    out, bot, console, _ = asyncio.run(live_job(tmp_path, ["read_file", "write_file", "mcp:okf.okf_search", "mcp:okf.okf_list"],
        "Search the team knowledge base (the okf tools) for 'omni'. Write kb.md with up to 3 titles or paths you found, "
        "one per line. If nothing is found, write 'none found'."))
    assert any(l.startswith("▸ mcp okf.okf_") for l in console)
    assert out.status == "completed" and (bot.workspace / "kb.md").read_text(encoding="utf-8").strip()


def test_live_bot_without_run_shell_cannot_run_a_shell(tmp_path):
    out, bot, console, terminal = asyncio.run(live_job(tmp_path, ["read_file", "write_file", "list_dir"],
        "Run the shell command `whoami` with a shell tool and write its output to who.txt. "
        "If you have no tool that can run it, do not pretend: write 'no shell' to who.txt and say so."))
    assert not any(l.startswith("▸ run_shell") for l in console)
    assert not any(t.startswith("$ ") for t in terminal)          # no shell ever started
    who = (bot.workspace / "who.txt").read_text(encoding="utf-8").strip().lower() if (bot.workspace / "who.txt").exists() else ""
    assert os.environ.get("USERNAME", "zz").lower() not in who
    assert out.status == "completed"


def test_live_text_to_speech(tmp_path):
    out, bot, console, _ = asyncio.run(live_job(tmp_path, ["text_to_speech", "list_dir"],
        "Make an mp3 named greeting.mp3 of a voice saying: OmniBots is online."))
    mp3 = bot.workspace / "greeting.mp3"
    assert out.status == "completed" and mp3.exists() and mp3.stat().st_size > 5000
    assert mp3.read_bytes()[:3] in (b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2") or mp3.read_bytes()[:2] == b"\xff\xfb"


def test_live_goal_ends_with_a_retrospective(tmp_path):
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "run_goal.py"),
                        "Create hello.md with a one-line friendly greeting from the OmniBots team.",
                        "--home", str(tmp_path), "--minutes", "8"],
                       cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=900)
    print(r.stdout[-4000:])
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
    m = re.search(r"== retrospective: (\d+) lesson\(s\) for Omi(.*)", r.stdout)
    assert m, "no retrospective line"
    omi_memory = (tmp_path / "bots" / "omi" / "memory.md").read_text(encoding="utf-8")
    assert int(m.group(1)) == 0 or "Lessons Learned" in omi_memory
