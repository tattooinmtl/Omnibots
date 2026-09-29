"""Give Omi a goal from the terminal and watch the team work (PLAN.md A7).

  python tools/run_goal.py "research 3 static-site hosts and write a comparison report"
      [--home DIR]   (default: a fresh temporary home, so your real ~/.omnibots is untouched)
      [--minutes 20]

Uses your real providers (via Omni). Prints the board live, then the path of
REPORT.md. Needs no UI; the same engine pieces the app uses.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
import tomllib
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omnibots.board.bus import MessageBus  # noqa: E402
from omnibots.board.ledger import Ledger  # noqa: E402
from omnibots.board.locks import LeaseManager  # noqa: E402
from omnibots.orchestrator.playbooks import PlaybookStore  # noqa: E402
from omnibots.runtime.pools import runner_pools  # noqa: E402
from omnibots.settings import DEFAULT_SETTINGS_TOML  # noqa: E402
from omnibots.bots.profile import BotRegistry  # noqa: E402
from omnibots.bots.runner import JobRunner  # noqa: E402
from omnibots.db import Database  # noqa: E402
from omnibots.logging_setup import setup_logging  # noqa: E402
from omnibots.omni import load_omni_config, locate_omni  # noqa: E402
from omnibots.orchestrator.factory import BotFactory, SpawnGovernor  # noqa: E402
from omnibots.orchestrator.goal import Orchestrator  # noqa: E402
from omnibots.projects.graph import TaskGraph  # noqa: E402
from omnibots.projects.store import ProjectStore  # noqa: E402
from omnibots.providers.quota import QuotaManager  # noqa: E402
from omnibots.providers.router import Router  # noqa: E402
from omnibots.providers.seats import SeatScheduler  # noqa: E402
from omnibots.runtime.approvals import ApprovalCenter  # noqa: E402
from omnibots.runtime.sandbox import Sandbox  # noqa: E402

SHOW = {"TASK_PLANNED", "BOT_CREATED", "TASK_ASSIGNED", "WORK_STARTED", "CLAIM_SUBMITTED", "CLAIM_ACCEPTED", "CLAIM_REJECTED",
        "A2A_MESSAGE", "QUESTION", "COUNCIL_VERDICT", "REVIEW_RESULT", "TASK_COMPLETED", "TASK_FAILED", "ARTIFACT_READY",
        "SEAT_WAITING", "APPROVAL_REQUEST", "TOOL_REQUEST", "TOOL_RESULT"}


async def main(goal: str, home: Path, minutes: float, learn: bool = True, chaos: str = "") -> int:
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")
    setup_logging(home / "logs")
    db = Database(home / "db" / "omnibots.sqlite")
    await db.open()
    bus = MessageBus(db)
    cfg = load_omni_config(locate_omni())
    quota = QuotaManager(db)
    seats = SeatScheduler(boss_id="omi")
    router = Router(lambda: cfg, quota, seats)
    reg = BotRegistry(db, home / "bots", bus)
    await reg.ensure_boss()
    approvals = ApprovalCenter(db)
    ledger = Ledger(db, bus)
    t0 = time.time()

    def console(e):
        if e["kind"] == "console" and not e["stream"] and e["content"].startswith(("▸", "✖", "⏳", "⏸", "⟳")):
            print(f"  {time.time() - t0:6.0f}s  [{e['bot_id']}] {e['content'][:160]}", flush=True)

    pools = runner_pools(lambda: cfg, home, router, LeaseManager(db, bus),
                         tomllib.loads(DEFAULT_SETTINGS_TOML)["mcp_risk"])
    runner = JobRunner(db=db, registry=reg, router=router, approvals=approvals, home=home, sandbox=Sandbox(home / "sandbox"),
                       bus=bus, ledger=ledger, listener=console, learn=learn, **pools)
    from omnibots.orchestrator.relay import RelayDesk                  # the tool relay, as in the app (A8.d.02)
    runner.relay = RelayDesk(db=db, bus=bus, registry=reg, runner=runner, home=home)
    projects, graph = ProjectStore(db, home / "projects", bus), TaskGraph(db, bus)
    runner.relay.projects = projects
    orch = Orchestrator(db=db, bus=bus, registry=reg, runner=runner, graph=graph, projects=projects, ledger=ledger, router=router,
                        factory=BotFactory(reg, SpawnGovernor(), db=db, quota=quota), skills=lambda: cfg.skills,
                        goal_seconds=minutes * 60, playbooks=PlaybookStore(db, bus))

    async def board():
        sub = await bus.subscribe()
        while True:
            m = await sub.get()
            if m.message_type in SHOW:
                to = f" → {m.recipient_id}" if m.recipient_id else ""
                print(f"{time.time() - t0:6.0f}s  {m.sender_id or m.sender_type}{to}  [{m.message_type}]  {m.text()[:220]}", flush=True)
    watcher = asyncio.create_task(board())
    # the listen loop, as in the app (A7.a.15): a job that carries on to its next round (A15.d.03) is picked up
    from omnibots.bots.presence import TeamPresence
    presence = TeamPresence(db=db, bus=bus, registry=reg, runner=runner, graph=graph, projects=projects, orchestrator=orch,
                            poll_seconds=5.0, maintain=lambda pid, changes: asyncio.sleep(0))
    listening = asyncio.create_task(presence.run())

    async def auto_approve_nothing():                   # this harness never grants R3+; it denies and says so
        while True:
            await asyncio.sleep(1)
            for info in approvals.list_pending():
                print(f"  !! approval requested ({info['risk']}): {info['summary']}; denied by the terminal harness", flush=True)
                await approvals.decide(info["id"], False, "the terminal harness doesn't approve R3+ actions")
    guard = asyncio.create_task(auto_approve_nothing())

    injected = {"n": 0}
    if chaos:                                           # A14.a.06: a provider answers 429 now and then (Retry-After 30 s)
        import random
        import omnibots.providers.router as router_module
        from omnibots.providers.client import ProviderError
        victim, rate = chaos.split(":")
        real_stream = router_module.chat_stream

        async def chaotic(model, messages, tools=None, **kw):
            if model.provider_name == victim and random.random() < float(rate):
                injected["n"] += 1
                raise ProviderError(victim, 429, "rate limited (chaos test)", {"retry-after": "30"})
            return await real_stream(model, messages, tools, **kw)
        router_module.chat_stream = chaotic
        print(f"CHAOS: {victim} answers 429 on {float(rate):.0%} of calls")
    print(f"GOAL: {goal}\nhome: {home}\n")
    res = await orch.run_goal(goal)
    watcher.cancel()
    guard.cancel()
    listening.cancel()
    await pools["mcp"].close()
    if orch.last_retro:
        pb = orch.last_retro.get("playbook")
        print(f"== retrospective: {len(orch.last_retro.get('lessons') or [])} lesson(s) for Omi"
              + (f"; playbook {pb.name} v{pb.version} [{pb.status}]" if pb else "")
              + ("; playbook run recorded" if orch.last_retro.get("recorded") else ""))
    if chaos:
        print(f"== chaos: {injected['n']} injected 429(s)")
    print(f"\n== {res['status']} in {time.time() - t0:.0f}s · project {res['project_id']}")
    print(f"== report: {projects.folder(res['project_id']) / 'REPORT.md'}")
    await db.close()
    return 0 if res["status"] == "completed" else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("goal")
    ap.add_argument("--home", default="")
    ap.add_argument("--minutes", type=float, default=20)
    ap.add_argument("--no-learn", action="store_true", help="don't write lessons to the bots' memory after jobs")
    ap.add_argument("--chaos", default="", help="PROVIDER:RATE, e.g. minimax.io:0.3 = that provider answers 429 on 30%% of calls")
    a = ap.parse_args()
    home = Path(a.home) if a.home else Path(tempfile.mkdtemp(prefix="omnibots-goal-"))
    sys.exit(asyncio.run(main(a.goal, home, a.minutes, learn=not a.no_learn, chaos=a.chaos)))
