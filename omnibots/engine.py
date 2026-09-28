"""The engine: one asyncio loop on a dedicated thread (ADR-1).

The Qt main thread owns the UI and tray. Everything else (orchestrator, bots,
bus, database writer) lives on this loop. Talking to it:
  UI -> engine : engine.submit(coro)   (thread-safe, returns a concurrent Future)
  engine -> UI : Qt signals on EngineSignals (queued across threads by Qt)

Clean shutdown (defines "Exit cleanly", audit §10):
  stop intake -> cancel bot tasks -> audit "session_end" -> flush the writer
  -> close the DB -> stop the loop.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import threading
from pathlib import Path
from typing import Any, Awaitable, Coroutine

from PySide6.QtCore import QObject, Signal

from omnibots import __version__
from omnibots.db import Database
from omnibots.keepawake import KeepAwake
from omnibots.lineup import TARGET_PROVIDERS
from omnibots.omni import OmniConfig, OmniNotFound, load_omni_config, locate_omni
from omnibots.omni.watch import watch_omni
from omnibots.providers.quota import QuotaManager
from omnibots.providers.router import Router
from omnibots.providers.seats import SeatScheduler
from omnibots.board.bus import MessageBus
from omnibots.board.ledger import Ledger
from omnibots.board.locks import LeaseManager
from omnibots.board.types import APPROVALS, ORCHESTRATOR, topic_bot
from omnibots.bots.leash import CURRENT_ORIGIN, Leash
from omnibots.bots.profile import BotRegistry
from omnibots.bots.runner import JobRunner
from omnibots.lineup import CHEAP_FIRST, MINIMAX
from omnibots.orchestrator.forge import ToolForge
from omnibots.orchestrator.playbooks import PlaybookStore
from omnibots.runtime.pools import runner_pools
from omnibots.runtime.approvals import ALLOCATED, MORE_TOKENS
from omnibots.security.budget import Budget
from omnibots.security.vault import Vault
from omnibots.projects.graph import PlanRunner, TaskGraph
from omnibots.projects.schedule import NightShift, Routines, Triggers
from omnibots.projects.store import ProjectStore
from omnibots.orchestrator.factory import BotFactory, GovernorLimits, SpawnGovernor
from omnibots.orchestrator.goal import Orchestrator
from omnibots.orchestrator.team import TeamController
from omnibots.runtime.approvals import ApprovalCenter
from omnibots.runtime.sandbox import Sandbox

BOSS_ID = "omi"   # Omi, the boss: always holds MiniMax seat 1 (ADR-11)

log = logging.getLogger(__name__)


class EngineSignals(QObject):
    started = Signal(dict)       # {"schema_version": int}
    status = Signal(dict)        # status snapshots for the UI
    stopped = Signal()
    failed = Signal(str)
    omni_changed = Signal(dict)  # Omni bridge summary (masked keys only)
    alert = Signal(dict)         # provider cooling/recovered/error, reservoir thresholds
    bot_event = Signal(dict)     # console / terminal / thinking / state / tool from running bots (A11 bot windows)
    board_message = Signal(dict) # every message on the board, live (the bot windows' Message Board)
    startup_stage = Signal(str, float)  # (what's loading, 0..1): the splash follows the real startup


class Engine:
    def __init__(self, db_path: Path, *, keep_awake: bool = True, shutdown_timeout: float = 10.0,
                 omni_install_root: str = "", watch_interval: float = 2.0, home: Path | None = None,
                 orchestrator_settings: dict[str, Any] | None = None, mcp_risk: dict[str, str] | None = None,
                 budgets: dict[str, Any] | None = None, output_dir: str | Path | None = None, approvals_ask_from: str = "R3",
                 skill_folders: list[str] | None = None, backup_settings: dict[str, Any] | None = None):
        self.backup_settings = {"every_hours": 24, "keep": 7, "retention_days": 90, "audit_days": 365, **(backup_settings or {})}
        self.db = Database(db_path, backup_home=home, backup_keep=int(self.backup_settings["keep"]))
        self.output_dir = Path(output_dir) if output_dir else None      # A11.m.01: the user's output folder
        self.approvals_ask_from = approvals_ask_from                          # settings [approvals] ask_from
        self.skill_folders = list(skill_folders or [])                        # settings [skills] folders
        self.home = home or db_path.parent.parent            # ~/.omnibots (db lives in <home>/db/)
        self.orch_settings = dict(orchestrator_settings or {})
        self.mcp_risk = dict(mcp_risk or {})
        self.budget_settings = dict(budgets or {})
        self.budget: Budget | None = None
        self.playbooks: PlaybookStore | None = None
        self.vault: Vault | None = None
        self.mcp = None                                       # MCPManager over Omni's mcpServers (A8.b.02)
        self.omni_install_root = omni_install_root
        self.watch_interval = watch_interval
        self.omni: OmniConfig | None = None
        self.omni_error: str | None = None
        self.quota: QuotaManager | None = None
        self.seats: SeatScheduler | None = None
        self.router: Router | None = None
        self.approvals: ApprovalCenter | None = None
        self.bus: MessageBus | None = None
        self.locks: LeaseManager | None = None
        self.ledger: Ledger | None = None
        self.registry: BotRegistry | None = None
        self.runner: JobRunner | None = None
        self.projects: ProjectStore | None = None
        self.graph: TaskGraph | None = None
        self.plans: PlanRunner | None = None
        self.routines: Routines | None = None
        self.triggers: Triggers | None = None
        self.night: NightShift | None = None
        self.factory: BotFactory | None = None
        self.orchestrator: Orchestrator | None = None
        self.team: TeamController | None = None
        self.bot_states: dict[str, str] = {}          # bot -> its latest live state (thinking, rate_limited…), for the tray
        self.signals = EngineSignals()
        self.keep_awake = KeepAwake(enabled=keep_awake)
        self.shutdown_timeout = shutdown_timeout
        self.accepting = False
        self.loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._start_error: BaseException | None = None
        self._tasks: set[asyncio.Task] = set()   # bot/orchestrator tasks, cancelled on shutdown
        self._stopped = False

    # ── start ──────────────────────────────────────────────────────────
    def start(self, timeout: float = 15.0) -> None:
        self.begin()
        self.wait_started(timeout)

    def begin(self) -> None:
        """Start the engine thread without waiting (the splash animates meanwhile)."""
        self._thread = threading.Thread(target=self._run, name="engine", daemon=True)
        self._thread.start()

    @property
    def started(self) -> bool:
        return self._ready.is_set()

    def wait_started(self, timeout: float = 15.0) -> None:
        """Wait for startup; raise its error, if any."""
        if not self._ready.wait(timeout):
            raise TimeoutError("engine did not start in time")
        if self._start_error:
            raise RuntimeError(f"engine failed to start: {self._start_error}") from self._start_error

    def _run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._startup())
        except BaseException as exc:  # report to the starting thread
            self._start_error = exc
            log.exception("engine startup failed")
            self._ready.set()
            self.loop.close()
            return
        self._ready.set()
        try:
            self.loop.run_forever()
        finally:
            self.loop.close()
            log.info("engine loop closed")

    def _stage(self, text: str, done: float) -> None:
        self.signals.startup_stage.emit(text, done)

    async def _startup(self) -> None:
        self._stage("Opening the database", 0.05)
        await self.db.open()
        await self.db.audit("system", None, "session_start", json.dumps({"version": __version__}))
        self._stage("Unlocking the vault", 0.15)
        self.vault = Vault(self.db)
        n = await self.vault.load()                        # every value registered for redaction (A9.c.01)
        log.info("vault: %d secret(s) indexed", n)
        self.budget = Budget.from_settings(self.db, self.budget_settings)
        self._stage("Reading Omni's providers and skills", 0.25)
        await self._load_omni()
        self._stage("Cleaning up after the last session", 0.40)
        self.bus = MessageBus(self.db)
        self.budget.bus = self.bus                               # A9.c.03: a bot's ask for tokens goes to Omi
        self.locks = LeaseManager(self.db, self.bus)
        await self.locks.clear_all()                       # leases never survive a restart
        await self._recover_orphans()                      # nor do running jobs (A6.c.01)
        self.ledger = Ledger(self.db, self.bus, boss_id=BOSS_ID)
        self._stage("Providers, seats and quotas", 0.50)
        self.quota = QuotaManager(self.db, on_event=self._on_provider_event)
        await self.quota.load()
        self.seats = SeatScheduler(boss_id=BOSS_ID, on_event=self._on_provider_event)
        self.router = Router(lambda: self.omni, self.quota, self.seats)
        self.approvals = ApprovalCenter(self.db, on_event=self._on_provider_event, ask_from=self.approvals_ask_from)
        self._stage("Waking up Omi and the team", 0.60)
        self.registry = BotRegistry(self.db, self.home / "bots", self.bus)
        await self.registry.ensure_boss()                     # Omi always exists (id "omi")
        self._stage("Loading tools, skills and MCP servers", 0.70)
        pools = runner_pools(lambda: self.omni, self.home, self.router, self.locks, self.mcp_risk, self.skill_folders)   # A8 skill & tool pools
        self.mcp = pools["mcp"]
        self.runner = JobRunner(db=self.db, registry=self.registry, router=self.router, approvals=self.approvals,
                                home=self.home, sandbox=Sandbox(self.home / "sandbox"), bus=self.bus, ledger=self.ledger,
                                keep_awake=self.keep_awake, listener=self._bot_listener, vault=self.vault, budget=self.budget, **pools)
        self.forge = ToolForge(self.db, self.home, self.runner.sandbox, self.router, bus=self.bus)   # A10.b.01
        self.runner.forge = self.forge
        from omnibots.runtime.computer_tools import ComputerClient              # A10.f.04: each bot's own computer
        self.computers = ComputerClient(lambda bot: self.vault.value(f"computer_{bot}"),
                                        owner_key=self._computers_owner_key, store_secret=self._store_computer_login)
        self.runner.computers = self.computers
        self._stage("Projects and the output folder", 0.82)
        self.projects = ProjectStore(self.db, self.home / "projects", self.bus, output_dir=self.output_dir,
                                     history_dir=self.home / "project-history")
        await self.projects.load()
        if self.output_dir:
            try:
                for pid, new in await self.projects.move_old_projects():            # A11.m.02, once
                    log.info("moved project %s to %s", pid, new)
                    await self.db.audit("system", None, "project_moved", json.dumps({"project": pid, "path": str(new)}))
            except Exception:
                log.exception("could not move the old projects to %s", self.output_dir)
        self._stage("Orchestrator, routines and team controls", 0.92)
        self.playbooks = PlaybookStore(self.db, self.bus)                  # A8.c.01
        self.graph = TaskGraph(self.db, self.bus)
        self.plans = PlanRunner(self.graph, self.runner.run, workspace_for=self.projects.folder)
        self.leash = await Leash(self.db).load()                           # A15.b: the dial and "Pause background work"
        self.routines = Routines(self.db, self._background_goal)
        self.triggers = Triggers(self.db, self._background_goal, bus=self.bus,
                                 metrics={"minimax_used_pct": lambda: self.quota.snapshot().get(MINIMAX, {}).get("used_pct", 0.0)})
        self.night = NightShift(self._night_run, held=lambda: self.leash.paused)
        self.spawn(self.routines.loop(), name="routines")
        self.spawn(self.triggers.poll_loop(), name="triggers-poll")
        self.spawn(self.triggers.board_loop(), name="triggers-board")
        self.spawn(self.night.loop(), name="night-shift")
        o = self.orch_settings
        self.factory = BotFactory(self.registry, SpawnGovernor(GovernorLimits(
            max_bots=int(o.get("max_bots", 12)), max_spawn_per_goal=int(o.get("max_spawn_per_goal", 5)),
            max_spawn_per_minute=int(o.get("max_spawn_per_minute", 3)),
            require_approval=bool(o.get("require_approval_for_new_bots", False)))), db=self.db, quota=self.quota)
        self.orchestrator = Orchestrator(db=self.db, bus=self.bus, registry=self.registry, runner=self.runner, graph=self.graph,
                                         projects=self.projects, ledger=self.ledger, router=self.router, factory=self.factory,
                                         skills=lambda: self.runner.skill_pool.all(),       # Omni + OmniBots + library folders
                                         goal_seconds=float(o.get("goal_minutes", 30)) * 60,
                                         playbooks=self.playbooks)
        self.team = TeamController(db=self.db, bus=self.bus, runner=self.runner, graph=self.graph, orchestrator=self.orchestrator,
                                   approvals=self.approvals, seats=self.seats, stall_after=float(o.get("stall_minutes", 5)) * 60)
        self.spawn(self.team.monitor(), name="stall-monitor")
        from omnibots.bots.presence import TeamPresence
        self.presence = TeamPresence(db=self.db, bus=self.bus, registry=self.registry, runner=self.runner,
                                     graph=self.graph, projects=self.projects, orchestrator=self.orchestrator,
                                     poll_seconds=float(o.get("listen_seconds", 5)),
                                     settle_seconds=float(o.get("watch_settle_seconds", 180)),
                                     paused=lambda: bool(self.team and self.team.paused), leash=self.leash)
        self.spawn(self.presence.run(), name="presence")
        self.spawn(self._forward_board(), name="board-to-ui")
        if self.home is not None:
            self.spawn(self._upkeep(), name="backup-and-housekeeping")      # A16.c
        self.accepting = True
        self._stage("Ready", 1.0)
        self.signals.started.emit({"schema_version": self.db.schema_version})

    # ── Omni bridge (A1) ───────────────────────────────────────────────
    async def _recover_orphans(self) -> dict[str, int]:
        """At startup nothing runs yet, so anything still marked live was cut off (a crash, a kill,
        a power loss): jobs become `interrupted` (Start / Restart Omi picks them up), bots go idle,
        and approvals nobody is waiting for any more expire."""
        live = ("assigned", "running", "review", "waiting_approval", "paused")
        counts = {}
        marks = ",".join("?" * len(live))
        for name, where, args, set_ in (
                ("jobs", f"status IN ({marks})", live, "status='interrupted'"),
                ("bots", "status='working'", (), "status='idle'"),
                # A6.c.02: never-started jobs left in finished projects (before the fix) weren't needed
                ("jobs", "status IN ('pending','ready','blocked') AND project_id IN (SELECT id FROM projects WHERE status='done' AND "
                         + ProjectStore.PASSED_SQL.format(pid="projects.id") + ")", (),
                 "status='cancelled', error_message='not needed: the goal was completed without it'"),
                ("approvals", "status='pending'", (), "status='expired', decided_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')")):
            row = await self.db.read_one(f"SELECT COUNT(*) AS n FROM {name} WHERE {where}", args)
            n = row["n"] if row else 0
            counts[name] = counts.get(name, 0) + n
            if n:
                await self.db.write(f"UPDATE {name} SET {set_} WHERE {where}", args)
        if any(counts.values()):
            log.warning("startup cleanup (cut-off or unneeded work): %s", counts)
            await self.db.audit("system", None, "orphans_recovered", json.dumps(counts))
        return counts

    async def _load_omni(self) -> None:
        try:
            loc = locate_omni(self.omni_install_root)
        except OmniNotFound as exc:
            self.omni, self.omni_error = None, str(exc)
            log.error("%s", exc)
            return
        self.omni = await asyncio.to_thread(load_omni_config, loc)
        self.omni_error = None
        for err in self.omni.errors:
            log.warning("Omni: %s", err)
        log.info("Omni loaded from %s: %d providers, %d models, %d skills, %d MCP servers",
                 loc.install_root, len(self.omni.providers), len(self.omni.models),
                 len(self.omni.skills), len(self.omni.mcp_servers))
        self.spawn(watch_omni(loc, self._on_omni_changed, interval=self.watch_interval), name="omni-watch")

    async def _on_provider_event(self, kind: str, data: dict[str, Any]) -> None:
        """Provider/seat events: audited, and surfaced to the UI/tray as alerts."""
        board = {"seat_granted": "SEAT_GRANTED", "seat_released": "SEAT_RELEASED", "seat_waiting": "SEAT_WAITING"}
        if kind in board and self.bus:                # the waiting list is visible on the board (A4.b.01)
            await self.bus.publish(ORCHESTRATOR, board[kind], data, sender_type="system",
                                   recipient_id=data.get("bot_id"))
            return
        if kind == "seat_queue":
            self.signals.alert.emit({"kind": kind, **data})    # tray "Waiting for a seat (N)" (A11)
            return
        if kind in ("approval_request", "approval_decision") and self.bus:
            payload = {"approval_id": data["id"], "summary": data.get("summary", ""), "risk": data.get("risk"),
                       **({"approved": data.get("approved")} if kind == "approval_decision" else {})}
            await self.bus.publish(APPROVALS, kind.upper(), payload, sender_type="bot" if kind == "approval_request" else "user",
                                   sender_id=data.get("bot_id") if kind == "approval_request" else "user",
                                   recipient_id=None, job_id=data.get("job_id"))
        await self.db.audit("system", None, kind, json.dumps(data))
        self.signals.alert.emit({"kind": kind, **data})

    def set_output_dir(self, folder: Path) -> None:
        """Settings → Folders: new goals go there from now on (existing projects stay where they are)."""
        self.output_dir = Path(folder)
        if self.projects:
            self.projects.out = self.output_dir

    async def start_goal(self, goal: str, info: dict[str, Any] | None = None) -> str:
        """A new goal (from the user, a routine or a trigger): Omi plans it,
        staffs it, verifies the claims and writes REPORT.md (A7).
        info["folder"]: work in that existing folder (File → Open folder, A11.m.03)."""
        info = info or {}
        pid = await self.projects.create(goal, created_by=info.get("created_by", "user"),
                                         folder=Path(info["folder"]) if info.get("folder") else None)
        origin = "routine" if info.get("routine") else "trigger" if info.get("trigger") else "user"
        token = CURRENT_ORIGIN.set(origin)                 # the goal's task copies it (A15.b.01)
        try:
            task = self.spawn(self.orchestrator.run_goal(goal, project_id=pid), name=f"goal-{pid}")
        finally:
            CURRENT_ORIGIN.reset(token)
        self.orchestrator.boss_tasks[pid] = task
        await self.db.audit("system", None, "goal_started", json.dumps({"project": pid, **{k: v for k, v in info.items() if k != "created_by"}}))
        return pid

    @staticmethod
    def _computers_owner_key() -> str | None:
        """OmniBots' owner key for the Bot Computers gateway (Windows Credential Manager)."""
        try:
            import keyring
            key = keyring.get_password("omnibots", "computers_owner")
        except Exception:
            return None
        from omnibots.security.vault import _remember
        _remember("computers_owner", key)                   # redacted everywhere, like vault values
        return key

    async def _store_computer_login(self, bot: str, secret: str) -> None:
        from omnibots.runtime.computer_tools import COMPUTERS_URL
        from urllib.parse import urlparse
        await self.vault.set(f"computer_{bot}", secret, kind="computer", note="Bot Computers login (auto-provisioned)",
                             hosts=[urlparse(COMPUTERS_URL).hostname or ""])

    def preapprove(self, tool: str, *, match: str = "", domain: str = "", count: int = 1, minutes: float = 60) -> dict[str, Any]:
        """A9.b.01: the user pre-approves up to `count` R3 calls of `tool` (matching text / domain) for `minutes`.
        R4 and R5 can never be pre-approved."""
        import time as _t
        from omnibots.runtime.approvals import Scope
        scope = Scope(tool=tool, risk="R3", match=match, remaining=max(1, int(count)),
                      expires_at=_t.time() + max(1.0, float(minutes)) * 60, domain=domain.lower().strip())
        self.approvals.pre_approve(scope)
        return {"tool": tool, "match": match, "domain": scope.domain, "count": scope.remaining, "minutes": minutes}

    # ── the UI's view (A11: bot windows) ───────────────────────────────
    async def _forward_board(self) -> None:
        sub = await self.bus.subscribe()
        while True:
            m = await sub.get()
            self.signals.board_message.emit(message_dict(m))

    def _bot_listener(self, ev: dict[str, Any]) -> None:
        """Every bot event: remember the bot's latest state (for the tray groups), then on to the windows."""
        if ev.get("kind") == "state":
            self.bot_states[str(ev.get("bot_id"))] = str(ev.get("content") or "").partition(":")[0].strip()
        self.signals.bot_event.emit(ev)

    async def ui_tray(self) -> dict[str, Any]:
        """Everything the tray menu shows, in one engine round trip (A11.b.01)."""
        running = bool(self.runner.active) or any(not t.done() for t in self.orchestrator.boss_tasks.values())
        interrupted = (await self.db.read_one("SELECT COUNT(*) AS n FROM jobs WHERE status='interrupted'"))["n"]
        jobs = {r["assigned_bot_id"]: r["title"] for r in await self.db.read(
            "SELECT assigned_bot_id, title FROM jobs WHERE status IN ('assigned','running','review','waiting_approval','paused') "
            "AND assigned_bot_id IS NOT NULL ORDER BY started_at")}
        pending = self.approvals.list_pending()
        asking = {a["bot_id"] for a in pending}
        queue = [q["bot_id"] for q in self.seats.queue()]
        bots = []
        for b in await self.registry.list():
            if b.status == "archived":
                continue
            agent = self.runner.active.get(b.id)
            live = self.bot_states.get(b.id, "")
            if b.id in asking:
                group = "approval"
            elif b.id in queue:
                group = "seat"
            elif agent is not None and agent.paused:
                group = "paused"
            elif agent is not None and live == "waiting_answer":
                group = "question"
            elif agent is not None:
                group = {"thinking": "thinking", "rate_limited": "rate_limited", "blocked": "blocked",
                         "error": "error"}.get(live, "working")
            elif live in ("error", "blocked", "stopped"):
                group = live
            else:
                group = "idle"
            bots.append({"id": b.id, "name": b.name, "role": b.role, "group": group, "job": jobs.get(b.id, "") if agent is not None else "",
                         "active": agent is not None, "paused": bool(agent is not None and agent.paused),
                         "memory": str(b.memory.path)})
        if self.team.paused:
            team = "paused"
        elif running:
            team = "working"
        elif interrupted:
            team = "stopped"
        else:
            team = "idle"
        held = [s for s in self.seats.snapshot() if "seat" in s]
        fc = await self.quota.forecast() if self.quota else {}
        projects = [{"id": r["id"], "goal": r["goal"], "autonomy": r["autonomy"]} for r in await self.db.read(
            "SELECT id, goal, autonomy FROM projects WHERE status != 'cancelled' ORDER BY created_at DESC LIMIT 12")]
        return {"team": team, "running": running, "paused": self.team.paused, "interrupted": interrupted,
                "background_paused": self.leash.paused, "projects": projects,
                "bots": bots, "approvals": pending, "seats_used": sum(1 for s in held if s.get("holder")),
                "seats_total": len(held), "tokens_left": fc.get("tokens_left")}

    async def ui_bots(self) -> list[dict[str, Any]]:
        """Every bot with what its ID card needs."""
        quota = self.quota.snapshot() if self.quota else {}
        latest = await self.db.read_one("SELECT id FROM projects ORDER BY created_at DESC LIMIT 1")
        # a worker's files = the project of its latest job (A11.m.02); Omi's = the latest project
        last_job = {r["assigned_bot_id"]: r["project_id"] for r in await self.db.read(
            "SELECT assigned_bot_id, project_id FROM jobs WHERE assigned_bot_id IS NOT NULL AND project_id IS NOT NULL "
            "ORDER BY COALESCE(started_at, created_at)")}
        # the snapshot ends with a {"waiting": [...]} entry; only real, held seats count here
        seats = {s["holder"]: s["seat"] for s in (self.seats.snapshot() if self.seats else []) if s.get("holder")}
        out = []
        for b in await self.registry.list():
            first = b.chain[0] if b.chain else ""
            provider = first.split("::")[0].split("/")[0]
            out.append({"id": b.id, "name": b.name, "role": b.role, "description": b.description, "status": b.status,
                        "model": first, "seat": seats.get(b.id), "usage_pct": float((quota.get(provider) or {}).get("used_pct") or 0),
                        "workspace": str(self.projects.folder(latest["id"]) if (b.id == BOSS_ID and latest and self.projects)
                                         else self.projects.folder(last_job[b.id]) if (b.id in last_job and self.projects)
                                         else b.workspace), "active": b.id in self.runner.active})
        return out

    async def ui_history(self, bot_id: str, events: int = 300, messages: int = 80) -> dict[str, Any]:
        """What a newly opened window shows first: this bot's recent console/thinking, and the board."""
        rows = await self.db.read("SELECT kind, content FROM bot_events WHERE bot_id=? AND kind IN ('console','terminal','thinking') "
                                  "ORDER BY id DESC LIMIT ?", (bot_id, events))
        msgs = await self.db.read("SELECT * FROM messages ORDER BY id DESC LIMIT ?", (messages,))
        board = []
        for r in reversed(msgs):
            board.append({"id": r["id"], "topic": r["topic"], "sender_type": r["sender_type"], "sender_id": r["sender_id"],
                          "recipient_id": r["recipient_id"], "type": r["message_type"], "payload": json.loads(r["payload_json"] or "{}"),
                          "job_id": r["job_id"], "project_id": r["project_id"], "created_at": r["created_at"]})
        return {"events": [dict(r) for r in reversed(rows)], "board": board}

    async def tell(self, bot_id: str, text: str) -> None:
        """The user talks to a bot (answers Omi's ask_user, or instructs a worker)."""
        await self.bus.publish(topic_bot(bot_id), "A2A_MESSAGE", {"text": text}, sender_type="user", sender_id="user", recipient_id=bot_id)

    async def _background_goal(self, goal: str, info: dict[str, Any] | None = None) -> str:
        """A routine or a trigger starts a goal, unless background work is paused (A15.b.02)."""
        origin = "routine" if (info or {}).get("routine") else "trigger"
        if why := await self.leash.may_start(origin, None):
            await self.bus.publish(ORCHESTRATOR, "PROGRESS_UPDATE", {"text": f"Skipped the {origin} '{(info or {}).get('name', goal[:40])}': {why}."},
                                   sender_type="system")
            return ""
        return await self.start_goal(goal, info)

    # ── backup and housekeeping (A16.c) ────────────────────────────────────
    async def backup_now(self, reason: str = "manual") -> str:
        if self.home is None:
            raise RuntimeError("no home folder: backups are off")
        return str(await self.db.backup_to(self.home, reason, int(self.backup_settings["keep"])))

    async def _upkeep(self, check_every: float = 3600.0, first_after: float = 120.0) -> None:
        """Hourly: a daily backup when the last one is older than every_hours; a weekly housekeeping
        pass while no bot is working. The first pass waits `first_after` s, so startup stays quick."""
        from omnibots import backup
        import time as _time
        await asyncio.sleep(first_after)
        while True:
            try:
                newest = next((b for b in backup.list_backups(self.home) if b["reason"] == "daily"), None)
                age_h = ((_time.time() - Path(newest["path"]).stat().st_mtime) / 3600) if newest else 1e9
                if age_h >= float(self.backup_settings["every_hours"]):
                    await self.backup_now("daily")
                row = await self.db.read_one("SELECT value FROM parameters WHERE scope='global' AND bot_id IS NULL AND key='last_housekeeping'")
                last = float(row["value"]) if row and row["value"] else None
                if last is None:                           # a new install: start the weekly clock, nothing to clean yet
                    await self.db.write("INSERT INTO parameters (scope, bot_id, key, value) VALUES ('global', NULL, 'last_housekeeping', ?)",
                                        (str(_time.time()),))
                elif _time.time() - last >= 7 * 86400 and not self.runner.active:
                    done = await self.db.housekeeping(float(self.backup_settings["retention_days"]),
                                                      float(self.backup_settings["audit_days"]), vacuum=True)
                    await self.db.write("DELETE FROM parameters WHERE scope='global' AND bot_id IS NULL AND key='last_housekeeping'")
                    await self.db.write("INSERT INTO parameters (scope, bot_id, key, value) VALUES ('global', NULL, 'last_housekeeping', ?)",
                                        (str(_time.time()),))
                    await self.db.audit("system", None, "housekeeping", json.dumps(done))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("backup/housekeeping pass failed")
            await asyncio.sleep(check_every)

    # ── usage, health, projects (A13, A11.a.03) ────────────────────────────
    async def ui_stats(self, days: int = 14) -> dict[str, Any]:
        from omnibots import stats
        from omnibots.orchestrator import verdicts
        names = {b.id: b.name for b in await self.registry.list()}
        goals = {r["id"]: r["goal"] for r in await self.db.read("SELECT id, goal FROM projects")}
        return {"usage": await stats.usage(self.db, days), "health": await stats.health(self.db, min(days, 7)),
                "verdicts": await verdicts.stats(self.db), "names": names, "projects": goals}

    async def ui_projects(self) -> list[dict[str, Any]]:
        from omnibots import stats
        rows = await stats.projects(self.db)
        for r in rows:
            r["folder"] = str(self.projects.folder(r["id"]))
        return rows

    async def close_project(self, project_id: str) -> str:
        """A15.f.02 (first part): Close ends a project for good: Omi stops watching it and nothing
        starts there on its own. Refused while work is running in it (stop that first)."""
        busy = await self.db.read_one("SELECT COUNT(*) AS n FROM jobs WHERE project_id=? AND status IN ('running','assigned','review')",
                                      (project_id,))
        if busy and busy["n"]:
            raise RuntimeError(f"{busy['n']} job(s) are still running or waiting in this project; stop them first")
        await self.projects.set_status(project_id, "cancelled")
        await self.db.audit("user", None, "project_closed", json.dumps({"project": project_id}))
        return "closed"

    # ── your verdict on the work (A16.b) ───────────────────────────────────
    async def rate(self, project_id: str, verdict: int, note: str = "", claim_id: int | None = None) -> dict[str, Any]:
        from omnibots.orchestrator import verdicts
        return await verdicts.rate(self.db, self.registry, self.bus, project_id=project_id, verdict=verdict, note=note, claim_id=claim_id)

    async def ui_verdict_card(self, project_id: str) -> dict[str, Any]:
        from omnibots.orchestrator import verdicts
        return await verdicts.card_data(self.db, self.registry, project_id)

    async def verdict_stats(self) -> dict[str, Any]:
        from omnibots.orchestrator import verdicts
        return await verdicts.stats(self.db)

    async def set_background_paused(self, on: bool) -> bool:
        return await self.leash.set_paused(on)

    async def set_autonomy(self, project_id: str, level: str) -> str:
        return await self.leash.set_level(project_id, level)

    # ── token allocations (A9.c.03) ────────────────────────────────────────
    async def ui_allocations(self) -> dict[str, Any]:
        """For the Token allocations window: every open project, the bots created in it (idle or
        working) with today's background tokens used and allocated, and any ask waiting for you."""
        asks = {(a["rehearsal"].get("project"), a["rehearsal"].get("bot")): a["id"]
                for a in self.approvals.list_pending() if a.get("tool") == MORE_TOKENS and a.get("rehearsal")}
        projects = []
        for p in await self.db.read("SELECT id, goal, autonomy FROM projects WHERE status != 'cancelled' ORDER BY created_at DESC LIMIT 20"):
            bots = []
            for r in await self.db.read(
                    "SELECT DISTINCT assigned_bot_id AS b FROM jobs WHERE project_id=? AND assigned_bot_id IS NOT NULL AND assigned_bot_id != ?",
                    (p["id"], BOSS_ID)):
                prof = await self.registry.get(r["b"])
                if not prof or prof.status == "archived":
                    continue
                bots.append({"id": prof.id, "name": prof.name, "role": prof.role,
                             "state": "working" if prof.id in self.runner.active else "idle",
                             "used": await self.budget.background_used_today(p["id"], prof.id),
                             "allocated": await self.budget.allocation_today(p["id"], prof.id),
                             "asking": asks.get((p["id"], prof.id))})
            projects.append({"id": p["id"], "goal": p["goal"], "autonomy": p["autonomy"], "bots": bots})
        return {"per_bot": int(self.budget.limits["background_tokens_per_bot"]), "projects": projects}

    async def allocate_tokens(self, project_id: str, bot_id: str, tokens: int) -> int:
        """The user adds background tokens for one bot in one project, for today. A card the bot
        is waiting on for that project is approved too (the bot goes on without adding again)."""
        total = await self.budget.allocate(project_id, bot_id, tokens)
        for a in self.approvals.list_pending():
            r = a.get("rehearsal") or {}
            if a.get("tool") == MORE_TOKENS and r.get("project") == project_id and r.get("bot") == bot_id:
                await self.approvals.decide(a["id"], True, f"{ALLOCATED} {int(tokens):,} from the Token allocations window")
        return total

    async def _night_run(self, item: dict[str, Any]) -> None:
        """Night shift: low-priority work on the cheap lane while the user is away."""
        bot = await self.registry.get("bot_nightshift") or await self.registry.create(
            "Night Shift", "night-shift worker", chain=list(CHEAP_FIRST), bot_id="bot_nightshift",
            description="Runs low-priority work on the cheap lane while the user is away.")
        pid = await self.projects.create(item["goal"], created_by="night-shift")
        await self.runner.run(bot.id, item["goal"], project_id=pid, workspace=self.projects.folder(pid), chain=list(CHEAP_FIRST),
                              origin="night")

    async def steer(self, bot_id: str, text: str) -> str:
        """The user's chat box on a working bot (A11.c.03). Normally a USER_STEER, which the bot's
        runtime reads before its next step. But a bot waiting on its own question to the user takes
        no next step until answered, so then the text goes to its inbox as the answer.
        Returns "answer" or "steer"."""
        if self.bot_states.get(bot_id) == "waiting_answer":
            self.bot_states[bot_id] = "thinking"               # one answer; the next state event takes over
            await self.tell(bot_id, text)
            return "answer"
        await self.bus.publish(topic_bot(bot_id), "USER_STEER", {"text": text}, sender_type="user",
                               sender_id="user", recipient_id=bot_id)
        return "steer"

    async def _on_omni_changed(self, cfg: OmniConfig) -> None:
        self.omni = cfg
        if self.quota:
            for name in cfg.providers:          # the user may have fixed a key in Omni
                self.quota.clear_error(name)
        await self.db.audit("system", None, "omni_config_reloaded", json.dumps({"providers": len(cfg.providers), "errors": len(cfg.errors)}))
        self.signals.omni_changed.emit(self.omni_summary())

    def omni_summary(self) -> dict[str, Any]:
        if self.omni is None:
            return {"ok": False, "error": self.omni_error or "Omni not loaded"}
        summary = self.omni.summary(TARGET_PROVIDERS)
        summary["ok"] = not summary["missing_providers"]
        summary["with_key"] = [n for n, p in summary["providers"].items() if self.omni.providers[n].has_key]
        return summary

    # ── talking to the engine ──────────────────────────────────────────
    def submit(self, coro: Coroutine[Any, Any, Any]) -> concurrent.futures.Future:
        """Run a coroutine on the engine loop from any thread."""
        if self.loop is None or not self.accepting:
            coro.close()
            raise RuntimeError("engine is not accepting work")
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def spawn(self, coro: Awaitable[Any], name: str) -> asyncio.Task:
        """Engine-thread only: start a long-running task that shutdown will cancel."""
        task = asyncio.ensure_future(coro)
        task.set_name(name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def status(self) -> dict[str, Any]:
        row = await self.db.read_one("SELECT COUNT(*) AS n FROM bots WHERE status != 'archived'")
        working = await self.db.read_one("SELECT COUNT(*) AS n FROM bots WHERE status = 'working'")
        return {
            "version": __version__,
            "schema_version": self.db.schema_version,
            "accepting": self.accepting,
            "bots": row["n"] if row else 0,
            "bots_working": working["n"] if working else 0,
            "tasks": len(self._tasks),
            "keep_awake": self.keep_awake.active,
            "omni": self.omni_summary(),
            "providers": self.quota.snapshot() if self.quota else {},
            "seats": self.seats.snapshot() if self.seats else [],
            "reservoir": (await self.quota.forecast()) if self.quota else None,
            "approvals_pending": self.approvals.list_pending() if self.approvals else [],
        }

    # ── shutdown ───────────────────────────────────────────────────────
    async def _shutdown(self) -> None:
        self.accepting = False                                  # 1. stop intake
        # quitting mid-goal keeps the work resumable, like the tray's Stop: remember the live jobs,
        # because cancelling a bot marks its job `cancelled`
        live = ("assigned", "running", "review", "waiting_approval", "paused")
        live_ids = [r["id"] for r in await self.db.read(
            f"SELECT id FROM jobs WHERE status IN ({','.join('?' * len(live))})", live)]
        tasks = [t for t in self._tasks if not t.done()]
        tasks += [t for t in (self.runner.tasks.values() if self.runner else ()) if not t.done() and t not in tasks]
        for t in tasks:                                         # 2. cancel bots
            t.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=self.shutdown_timeout)
        if live_ids:
            await self.db.write(f"UPDATE jobs SET status='interrupted' WHERE id IN ({','.join('?' * len(live_ids))})", live_ids)
        self.keep_awake.release_all()
        if self.mcp:
            await self.mcp.close()                              # MCP server processes
        if self.runner:
            await self.runner.close_browsers()                  # Chromium contexts (A10.a.01)
        try:
            await self.db.audit("system", None, "session_end", json.dumps({"cancelled_tasks": len(tasks)}))
        finally:
            await self.db.close()                               # 3. flush writer + close DB

    def stop(self) -> None:
        """Blocking clean shutdown, callable from the UI thread. Idempotent."""
        if self._stopped or self.loop is None or not self.loop.is_running():
            return
        self._stopped = True
        fut = asyncio.run_coroutine_threadsafe(self._shutdown(), self.loop)
        try:
            fut.result(timeout=self.shutdown_timeout + 5)
        except Exception:
            log.exception("engine shutdown did not finish cleanly")
        self.loop.call_soon_threadsafe(self.loop.stop)
        if self._thread:
            self._thread.join(timeout=5)
        self.signals.stopped.emit()
        log.info("engine stopped")


def message_dict(m) -> dict[str, Any]:
    return {"id": m.id, "topic": m.topic, "sender_type": m.sender_type, "sender_id": m.sender_id, "recipient_id": m.recipient_id,
            "type": m.message_type, "payload": m.payload, "job_id": m.job_id, "project_id": m.project_id, "created_at": m.created_at}
