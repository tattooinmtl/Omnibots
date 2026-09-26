"""Headless harness: run ONE bot on a task from the terminal (PLAN.md A3.99).

  python -m omnibots.runtime.cli "create hello.py that prints hi, run it, report the output"
      [--provider minimax.io|nvidia|...]  [--chain minimax|cheap]  [--workspace DIR]  [--show-thinking]

Uses the real Omni config and providers, the real database in ~/.omnibots, and
the S0 sandbox. While it runs, type a line + Enter to STEER the bot. When the
bot asks for approval (R3+), answer y/n.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import threading
import time
from pathlib import Path

from omnibots.db import Database
from omnibots.lineup import CHEAP_FIRST, LINEUP_MODELS, MINIMAX_FIRST
from omnibots.logging_setup import setup_logging
from omnibots.omni import load_omni_config, locate_omni
from omnibots.paths import get_paths
from omnibots.providers.quota import QuotaManager
from omnibots.providers.router import Router
from omnibots.providers.seats import SeatScheduler
from omnibots.runtime.agent import BotAgent
from omnibots.runtime.approvals import ApprovalCenter
from omnibots.runtime.core_tools import core_registry
from omnibots.runtime.events import BotEvents
from omnibots.runtime.sandbox import Sandbox

COLORS = {"console": "", "terminal": "\033[36m", "thinking": "\033[90m", "state": "\033[35m"}
RESET = "\033[0m"


async def run(task: str, chain: list[str], workspace: Path, show_thinking: bool) -> int:
    for stream in (sys.stdout, sys.stderr):      # Windows consoles default to cp1252
        stream.reconfigure(encoding="utf-8", errors="replace")
    paths = get_paths()
    setup_logging(paths.logs)
    db = Database(paths.db_file)
    await db.open()
    cfg = load_omni_config(locate_omni())
    quota = QuotaManager(db)
    await quota.load()
    router = Router(lambda: cfg, quota, SeatScheduler(boss_id="omi"))
    loop = asyncio.get_running_loop()
    pending: dict[str, dict] = {}
    approvals = ApprovalCenter(db, on_event=lambda k, d: pending.__setitem__(d["id"], d) if k == "approval_request" else None)
    last = {"kind": None}

    def show(e: dict) -> None:
        kind, text = e["kind"], e["content"]
        if kind == "thinking" and not show_thinking:
            return
        if e["stream"]:
            if last["kind"] != kind:
                sys.stdout.write("\n" + COLORS.get(kind, ""))
            sys.stdout.write(text)
        else:
            sys.stdout.write("\n" + COLORS.get(kind, "") + (f"[{text}]" if kind == "state" else text) + RESET)
        sys.stdout.flush()
        last["kind"] = kind if e["stream"] else None

    events = BotEvents("cli_bot", db, listener=show)
    bot = BotAgent(bot_id="cli_bot", name="Omi-CLI", role="general worker", workspace=workspace, router=router,
                   chain=chain, tools=core_registry(), approvals=approvals, events=events, sandbox=Sandbox(paths.dir("sandbox")))

    def stdin_reader():                       # steering + approvals from the terminal
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            if approvals.pending and line.lower() in ("y", "yes", "n", "no"):
                aid = next(iter(approvals.pending))
                asyncio.run_coroutine_threadsafe(approvals.decide(aid, line.lower().startswith("y"), "terminal"), loop)
            else:
                loop.call_soon_threadsafe(bot.steer, line)
    threading.Thread(target=stdin_reader, daemon=True).start()

    t0 = time.time()
    res = await bot.run(task)
    print(f"\n\n== {res.status} in {res.steps} steps, {res.tool_calls} tool calls, {time.time() - t0:.1f}s, models: {sorted(set(res.models_used))}")
    if res.answer:
        print(res.answer)
    await db.close()
    return 0 if res.status == "done" else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m omnibots.runtime.cli")
    ap.add_argument("task")
    ap.add_argument("--provider", help="use only this provider's lineup model")
    ap.add_argument("--chain", choices=["minimax", "cheap"], default="minimax")
    ap.add_argument("--workspace", default="")
    ap.add_argument("--show-thinking", action="store_true")
    a = ap.parse_args(argv)
    chain = [LINEUP_MODELS[a.provider]] if a.provider else (MINIMAX_FIRST if a.chain == "minimax" else CHEAP_FIRST)
    ws = Path(a.workspace) if a.workspace else get_paths().dir("bots") / "cli_bot" / "workspace"
    return asyncio.run(run(a.task, chain, ws, a.show_thinking))


if __name__ == "__main__":
    sys.exit(main())
