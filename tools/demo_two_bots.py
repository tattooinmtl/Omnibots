"""Live demo (PLAN.md A4): Omi (the boss) and a worker talk through the board.

  python tools/demo_two_bots.py

Writes to the real board (~/.omnibots). Read the conversation afterwards:
  python -m omnibots.board --follow
"""

from __future__ import annotations

import asyncio
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omnibots.board.a2a import Inbox, a2a_tools  # noqa: E402
from omnibots.board.bus import MessageBus  # noqa: E402
from omnibots.board.ledger import Ledger, claim_tool  # noqa: E402
from omnibots.board.types import topic_project  # noqa: E402
from omnibots.db import Database  # noqa: E402
from omnibots.lineup import LINEUP_MODELS  # noqa: E402
from omnibots.omni import load_omni_config, locate_omni  # noqa: E402
from omnibots.paths import get_paths  # noqa: E402
from omnibots.providers.quota import QuotaManager  # noqa: E402
from omnibots.providers.router import Router  # noqa: E402
from omnibots.providers.seats import SeatScheduler  # noqa: E402
from omnibots.runtime.agent import BotAgent  # noqa: E402
from omnibots.runtime.approvals import ApprovalCenter  # noqa: E402
from omnibots.runtime.core_tools import core_registry  # noqa: E402
from omnibots.runtime.events import BotEvents  # noqa: E402
from omnibots.runtime.sandbox import Sandbox  # noqa: E402

BOSS = "omi"
BOSS_PROMPT = """You are Omi, the boss of the OmniBots team. You coordinate; you don't do the work yourself.
You MONITOR the work, INQUIRE the right bot, and RELAY what it needs. Workers can only talk to you.
Use send_message to give a worker its task, then wait_for_mention to get its reply and its claim.
Check the claim's evidence against what you asked. When satisfied, call submit with the final result, then answer in one line."""
WORKER_PROMPT = """You are {name}, an OmniBots worker (an Omi clone) specialised as a calculator.
You can only talk to the boss, omi. Start by calling wait_for_mention to receive your assignment.
Do the work with your tools (compute with run_python; never do arithmetic in your head).
Then call submit_claim with the result and evidence of kind "command" (ref = what you ran, exit_code = its exit code),
then send_message to omi with the result, and finish with a one-line report."""


async def main() -> int:
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")
    paths = get_paths()
    db = Database(paths.db_file)
    await db.open()
    bus = MessageBus(db)
    cfg = load_omni_config(locate_omni())
    seats = SeatScheduler(boss_id=BOSS)
    router = Router(lambda: cfg, QuotaManager(db), seats)
    ledger = Ledger(db, bus, boss_id=BOSS)
    project = f"demo_{uuid.uuid4().hex[:6]}"
    worker = f"w_calc_{project[-6:]}"
    sandbox = Sandbox(paths.dir("sandbox"))

    def show(e):
        if e["kind"] == "console" and not e["stream"]:
            print(f"  [{e['bot_id']}] {e['content']}", flush=True)

    boss_inbox, worker_inbox = await Inbox.open(bus, BOSS), await Inbox.open(bus, worker)
    boss_tools = core_registry().subset([])
    for t in a2a_tools(bus, boss_inbox, bot_id=BOSS, boss_id=BOSS, project_id=project):
        boss_tools.add(t)
    worker_tools = core_registry().subset(["run_python", "write_file", "read_file"])
    for t in a2a_tools(bus, worker_inbox, bot_id=worker, boss_id=BOSS, project_id=project):
        worker_tools.add(t)
    worker_tools.add(claim_tool(ledger, project_id=project))

    chain = [LINEUP_MODELS["minimax.io"]]
    boss = BotAgent(bot_id=BOSS, name="Omi", role="boss", workspace=paths.dir("bots") / BOSS / "workspace", router=router,
                    chain=chain, tools=boss_tools, approvals=ApprovalCenter(db), events=BotEvents(BOSS, db, listener=show),
                    sandbox=sandbox, system_prompt=BOSS_PROMPT, priority="boss", max_iterations=12)
    wbot = BotAgent(bot_id=worker, name=worker, role="calculator", workspace=paths.dir("bots") / worker / "workspace", router=router,
                    chain=chain, tools=worker_tools, approvals=ApprovalCenter(db), events=BotEvents(worker, db, listener=show),
                    sandbox=sandbox, system_prompt=WORKER_PROMPT.format(name=worker), max_iterations=12)

    await bus.publish(topic_project(project), "TASK_RECEIVED", {"text": "compute 1234 × 5678 + 91"}, sender_type="user", sender_id="user", project_id=project)
    t0 = time.time()
    results = await asyncio.gather(
        boss.run(f"The user wants 1234 × 5678 + 91 computed exactly. Your worker is `{worker}`. Delegate it, get the answer with evidence, verify, and submit."),
        wbot.run("Begin: wait for your assignment from omi."),
    )
    print(f"\n== boss: {results[0].status} ({results[0].steps} steps) · worker: {results[1].status} ({results[1].steps} steps) · {time.time() - t0:.1f}s")
    print(f"== project {project}. Read the conversation with:\n   python -m omnibots.board --project {project}\n   python -m omnibots.board --bot {worker}")
    await db.close()
    return 0 if all(r.status == "done" for r in results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
