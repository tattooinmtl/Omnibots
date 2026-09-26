"""A4: the board (bus, replay, queries, leases, ledger, A2A hub rule), the
MiniMax waiting list, steering over the board, and the terminal viewer."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from mock_provider import MockProviders, sse
from omnibots.board.a2a import Inbox, a2a_tools, steering_pump
from omnibots.board.bus import MessageBus
from omnibots.board.ledger import EvidenceError, Ledger, check_evidence, claim_tool
from omnibots.board.locks import LeaseManager
from omnibots.board.query import query
from omnibots.board.types import BoardError, topic_bot
from omnibots.db import Database
from omnibots.omni.config import OmniConfig, ProviderInfo
from omnibots.omni.locate import OmniLocation
from omnibots.providers.quota import QuotaManager
from omnibots.providers.router import Router
from omnibots.providers.seats import SeatScheduler
from omnibots.runtime.agent import BotAgent
from omnibots.runtime.approvals import ApprovalCenter
from omnibots.runtime.core_tools import core_registry
from omnibots.runtime.events import BotEvents
from omnibots.runtime.tools import ToolContext

ROOT = Path(__file__).resolve().parents[1]


def run(coro):
    return asyncio.run(coro)


async def open_db(tmp: Path) -> Database:
    db = Database(tmp / "db.sqlite")
    await db.open()
    return db


# ── A4.99 acceptance ───────────────────────────────────────────────────────
def test_post_to_a_bot_reaches_only_that_bot_and_the_ui(tmp_path):
    async def go():
        db = await open_db(tmp_path)
        bus = MessageBus(db)
        x = await bus.subscribe({topic_bot("x")})
        y = await bus.subscribe({topic_bot("y")})
        ui = await bus.subscribe()                                   # firehose: the board window
        await bus.publish(topic_bot("x"), "A2A_MESSAGE", {"text": "only for x"}, sender_type="bot", sender_id="omi", recipient_id="x")
        got = await x.get(1), await y.get(0.2), await ui.get(1)
        await db.close()
        return got
    gx, gy, gui = run(go())
    assert gx.text() == "only for x" and gy is None and gui.id == gx.id


def test_20_bots_x_50_messages_in_order_none_lost(tmp_path):
    async def go():
        db = await open_db(tmp_path)
        bus = MessageBus(db)
        subs = {f"b{i}": await bus.subscribe({topic_bot(f"b{i}")}) for i in range(20)}
        ui = await bus.subscribe()

        async def sender(i):
            for k in range(50):
                await bus.publish(topic_bot(f"b{i}"), "PROGRESS_UPDATE", {"n": k}, sender_type="bot", sender_id=f"b{i}")
        await asyncio.gather(*(sender(i) for i in range(20)))
        per_bot = {b: [m.payload["n"] for m in s.drain()] for b, s in subs.items()}
        all_ids = [m.id for m in ui.drain()]
        count = (await db.read_one("SELECT COUNT(*) AS n FROM messages"))["n"]
        await db.close()
        return per_bot, all_ids, count
    per_bot, all_ids, count = run(go())
    assert all(v == list(range(50)) for v in per_bot.values())
    assert all_ids == sorted(all_ids) and len(all_ids) == 1000 == count


def test_restart_replays_only_what_was_missed_then_continues_live(tmp_path):
    async def first_session():
        db = await open_db(tmp_path)
        bus = MessageBus(db)
        for k in range(10):
            await bus.publish("#general", "PROGRESS_UPDATE", {"n": k})
        await db.close()

    async def second_session():
        db = await open_db(tmp_path)
        bus = MessageBus(db)
        sub = await bus.subscribe({"#general"}, since_id=5)        # "I last saw message 5"
        live = asyncio.create_task(bus.publish("#general", "PROGRESS_UPDATE", {"n": 10}))
        await live
        got = sub.drain()
        await db.close()
        return got
    run(first_session())
    got = run(second_session())
    assert [m.id for m in got] == [6, 7, 8, 9, 10, 11] and [m.payload["n"] for m in got] == [5, 6, 7, 8, 9, 10]


def test_replay_during_concurrent_publishing_has_no_gaps_or_duplicates(tmp_path):
    async def go():
        db = await open_db(tmp_path)
        bus = MessageBus(db)
        for k in range(200):
            await bus.publish("#general", "PROGRESS_UPDATE", {"n": k})
        pub = asyncio.ensure_future(asyncio.gather(*(bus.publish("#general", "PROGRESS_UPDATE", {"n": 200 + k}) for k in range(200))))
        sub = await bus.subscribe({"#general"}, since_id=0)
        await pub
        ids = [m.id for m in sub.drain()]
        await db.close()
        return ids
    ids = run(go())
    assert ids == list(range(1, 401))


def test_lock_contention_resolves_and_ttl_expires(tmp_path):
    async def go():
        db = await open_db(tmp_path)
        bus = MessageBus(db)
        locks = LeaseManager(db, bus)
        assert await locks.try_acquire("app.py", "a", ttl=60)
        waiter = asyncio.create_task(locks.acquire("app.py", "b", ttl=60, timeout=5))
        await asyncio.sleep(0.3)
        blocked = not waiter.done()
        await locks.release("app.py", "a")
        got_b = await asyncio.wait_for(waiter, 3)
        assert await locks.try_acquire("short.txt", "a", ttl=0.4)          # a crashes, never releases
        got_after_expiry = await locks.acquire("short.txt", "c", timeout=5)
        not_owner = await locks.release("app.py", "a")
        events = [m.message_type for m in await query(db, topic="#general")]
        await db.close()
        return blocked, got_b, got_after_expiry, not_owner, events
    blocked, got_b, expired, not_owner, events = run(go())
    assert blocked and got_b and expired and not_owner is False
    assert events.count("LOCK_ACQUIRED") == 4 and events.count("LOCK_RELEASED") == 1


def test_queries_filter_and_paginate_in_sql(tmp_path):
    async def go():
        db = await open_db(tmp_path)
        bus = MessageBus(db)
        for k in range(30):
            await bus.publish(f"#job/j{k % 3}", "PROGRESS_UPDATE" if k % 5 else "TASK_FAILED",
                              {"n": k} if k % 5 else {"error": "boom"}, sender_type="bot", sender_id=f"b{k % 2}", job_id=f"j{k % 3}")
        a = await query(db, job="j1")
        b = await query(db, topic="#job/*", errors_only=True)
        page1 = await query(db, limit=10)
        page2 = await query(db, limit=10, before_id=page1[0].id)
        c = await query(db, bot="b0", types=["TASK_FAILED"])
        await db.close()
        return a, b, page1, page2, c
    a, b, p1, p2, c = run(go())
    assert len(a) == 10 and all(m.job_id == "j1" for m in a)
    assert len(b) == 6 and all(m.message_type == "TASK_FAILED" for m in b)
    assert [m.id for m in p1] == list(range(21, 31)) and [m.id for m in p2] == list(range(11, 21))
    assert all(m.sender_id == "b0" and m.message_type == "TASK_FAILED" for m in c)


def test_bad_messages_are_refused():
    async def go(tmp):
        db = await open_db(tmp)
        bus = MessageBus(db)
        with pytest.raises(BoardError):
            await bus.publish("#general", "NOT_A_TYPE", {})
        with pytest.raises(BoardError):
            await bus.publish("#general", "A2A_MESSAGE", {"no": "text"})
        await db.close()
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        run(go(Path(t)))


# ── ledger ─────────────────────────────────────────────────────────────────
def test_claims_need_real_evidence(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "out.txt").write_text("done", encoding="utf-8")
    ok = check_evidence([{"kind": "file", "ref": "out.txt"}, {"kind": "command", "ref": "python app.py", "exit_code": 0},
                         {"kind": "url_quote", "ref": "https://x.test", "quote": "price: $5"}], ws)
    assert ok[0]["detail"]["bytes"] == 4
    for bad, why in [([], "at least one"), ([{"kind": "file", "ref": "missing.txt"}], "does not exist"),
                     ([{"kind": "command", "ref": "pytest"}], "exit_code"), ([{"kind": "url_quote", "ref": "https://x"}], "quote"),
                     ([{"kind": "vibes", "ref": "trust me"}], "kind must be")]:
        with pytest.raises(EvidenceError, match=why):
            check_evidence(bad, ws)


def test_claim_goes_to_the_boss_and_the_verdict_back_to_the_worker(tmp_path):
    async def go():
        db = await open_db(tmp_path)
        bus = MessageBus(db)
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "report.md").write_text("# report", encoding="utf-8")
        boss_inbox, worker_inbox = await Inbox.open(bus, "omi"), await Inbox.open(bus, "w1")
        ledger = Ledger(db, bus, boss_id="omi")
        tool = claim_tool(ledger)
        ctx = ToolContext(bot_id="w1", workspace=ws, job_id="job_1")
        refused = await tool.fn({"text": "done", "evidence": [{"kind": "file", "ref": "nope.md"}]}, ctx)
        out = await tool.fn({"text": "report written", "evidence": [{"kind": "file", "ref": "report.md"}]}, ctx)
        submitted = await boss_inbox.sub.get(1)
        await ledger.decide(submitted.payload["claim_id"], False, "the report is empty", "omi")
        verdict = await worker_inbox.sub.get(1)
        with pytest.raises(ValueError):
            await ledger.decide(submitted.payload["claim_id"], True, "changed my mind", "omi")
        rows = await ledger.claims(job_id="job_1")
        await db.close()
        return refused, out, submitted, verdict, rows
    refused, out, submitted, verdict, rows = run(go())
    assert refused.startswith("ERROR: claim not recorded") and out.startswith("claim #1")
    assert submitted.message_type == "CLAIM_SUBMITTED" and submitted.recipient_id == "omi" and submitted.topic == "#job/job_1"
    assert verdict.message_type == "CLAIM_REJECTED" and verdict.payload["reason"] == "the report is empty"
    assert rows[0]["status"] == "rejected" and rows[0]["evidence"][0]["ref"] == "report.md"


# ── A2A hub rule ───────────────────────────────────────────────────────────
def test_workers_can_only_talk_to_the_boss(tmp_path):
    async def go():
        db = await open_db(tmp_path)
        bus = MessageBus(db)
        inbox = {b: await Inbox.open(bus, b) for b in ("omi", "w1", "w2")}
        tools = {b: {t.name: t for t in a2a_tools(bus, inbox[b], bot_id=b, boss_id="omi")} for b in inbox}
        ctx = lambda b: ToolContext(bot_id=b, workspace=tmp_path, job_id="j")
        r_bad = await tools["w1"]["send_message"].fn({"to": "w2", "text": "psst"}, ctx("w1"))
        r_up = await tools["w1"]["send_message"].fn({"to": "omi", "text": "need the API key name"}, ctx("w1"))
        heard = await tools["omi"]["wait_for_mention"].fn({"timeout_seconds": 2}, ctx("omi"))
        await tools["omi"]["send_message"].fn({"to": "w2", "text": "w1 needs NETLIFY_TOKEN handle"}, ctx("omi"))
        relayed = await tools["w2"]["wait_for_mention"].fn({"timeout_seconds": 2}, ctx("w2"))
        silence = await tools["w1"]["wait_for_mention"].fn({"timeout_seconds": 0.3}, ctx("w1"))
        return r_bad, r_up, heard, relayed, silence, set(tools["omi"]), set(tools["w1"])
    r_bad, r_up, heard, relayed, silence, boss_tools, worker_tools = run(go())
    assert r_bad.startswith("ERROR: workers can only message the boss (omi)") and r_up == "sent to omi"
    assert heard == "[A2A_MESSAGE from w1] need the API key name"
    assert relayed == "[A2A_MESSAGE from omi] w1 needs NETLIFY_TOKEN handle"
    assert silence.startswith("no messages arrived")
    assert "submit" in boss_tools and "submit" not in worker_tools


# ── waiting list ───────────────────────────────────────────────────────────
def test_waiting_list_positions_order_and_leaving():
    async def go():
        events = []
        s = SeatScheduler(boss_id="omi", on_event=lambda k, d: events.append((k, d)))
        held = [await s.acquire(f"w{i}") for i in range(1, 4)]           # 3 worker seats full
        waiters = [asyncio.create_task(s.acquire(f"w{i}")) for i in range(4, 7)]
        await asyncio.sleep(0.05)
        q1 = s.queue()
        s.release(held[0])                                                # w1 done -> w4 takes over
        await asyncio.sleep(0.05)
        w4_seat = waiters[0].result()
        q2 = s.queue()
        s.leave_queue("w6")                                               # user paused w6
        await asyncio.sleep(0.05)
        q3 = s.queue()
        boss = await s.acquire("omi", "boss")                             # the boss never waits behind workers
        return q1, w4_seat, q2, waiters[2].cancelled(), q3, boss, [k for k, _ in events]
    q1, w4_seat, q2, w6_cancelled, q3, boss, kinds = run(go())
    assert [(q["bot_id"], q["position"]) for q in q1] == [("w4", 1), ("w5", 2), ("w6", 3)]
    assert w4_seat.number == 2 and [(q["bot_id"], q["position"]) for q in q2] == [("w5", 1), ("w6", 2)]
    assert q2[0]["eta_s"] is not None                                     # an estimate once a lease has finished
    assert w6_cancelled and [q["bot_id"] for q in q3] == ["w5"] and boss.number == 1
    assert "seat_waiting" in kinds and "seat_queue" in kinds


def test_minimax_bots_wait_their_turn_instead_of_falling_back(tmp_path):
    async def go():
        with MockProviders() as mock:
            slow = sse("done")
            slow["hold"] = 0.4
            mock.default["minimax.io"] = slow
            mock.default["cheap"] = sse("cheap lane answered")
            providers = {n: ProviderInfo(name=n, base_url=mock.url(n), api_key="k", key_source="settings", raw={"reasoningParam": "none"})
                         for n in ("minimax.io", "cheap")}
            cfg = OmniConfig(OmniLocation(Path("."), Path(".")), providers,
                             {"minimax.io/m3": {"provider": "minimax.io", "id": "MiniMax-M3", "maxTokens": 64},
                              "cheap/m": {"provider": "cheap", "id": "c", "maxTokens": 64}}, None, None, [], {})
            seats = SeatScheduler(boss_id="omi")
            router = Router(lambda: cfg, QuotaManager(), seats)
            seen: dict[str, list] = {}
            bots = []
            for i in range(5):
                bid = f"w{i}"
                seen[bid] = []
                bots.append(BotAgent(bot_id=bid, name=bid, role="coder", workspace=tmp_path / bid, router=router,
                                     chain=["minimax.io/m3", "cheap/m"], tools=core_registry(), approvals=ApprovalCenter(),
                                     events=BotEvents(bid, listener=seen[bid].append)))
            results = await asyncio.gather(*(b.run("hi") for b in bots))
            return results, dict(mock.peak), [r for r in mock.requests if r["provider"] == "cheap"], seen
    results, peak, cheap_calls, seen = run(go())
    assert all(r.status == "done" for r in results)
    assert peak["minimax.io"] <= 3 and cheap_calls == []                  # 3 worker seats; nobody fell back
    waited = [b for b, evs in seen.items() if any(e["kind"] == "state" and e["content"].startswith("waiting_seat") for e in evs)]
    assert len(waited) == 2
    assert all(any("waiting in line (#" in e["content"] for e in seen[b]) for b in waited)


# ── steering over the board, engine wiring ─────────────────────────────────
def test_user_steer_on_the_board_reaches_the_running_bot(tmp_path):
    class FakeBot:
        bot_id = "w1"

        def __init__(self):
            self.notes = []

        def steer(self, text):
            self.notes.append(text)

    async def go():
        db = await open_db(tmp_path)
        bus = MessageBus(db)
        bot = FakeBot()
        pump = asyncio.create_task(steering_pump(bus, bot))
        await asyncio.sleep(0.05)
        await bus.publish(topic_bot("w1"), "USER_STEER", {"text": "use data/clean.csv"}, sender_type="user", sender_id="user", recipient_id="w1")
        await bus.publish(topic_bot("w2"), "USER_STEER", {"text": "not for w1"}, sender_type="user", sender_id="user", recipient_id="w2")
        await asyncio.sleep(0.1)
        pump.cancel()
        await db.close()
        return bot.notes
    assert run(go()) == ["use data/clean.csv"]


def test_engine_puts_the_waiting_list_and_approvals_on_the_board(tmp_path):
    from omnibots.engine import Engine

    async def go():
        eng = Engine(tmp_path / "db.sqlite")
        eng.db = await open_db(tmp_path)
        eng.bus = MessageBus(eng.db)
        await eng._on_provider_event("seat_waiting", {"bot_id": "w5", "priority": 3, "position": 2})
        await eng._on_provider_event("seat_granted", {"seat": 3, "bot_id": "w5"})
        await eng._on_provider_event("approval_request", {"id": "ap_1", "bot_id": "w5", "job_id": "j", "tool": "write_file", "risk": "R3", "summary": "write C:/x"})
        msgs = await query(eng.db)
        await eng.db.close()
        return [(m.topic, m.message_type, m.recipient_id) for m in msgs]
    assert run(go()) == [("#orchestrator", "SEAT_WAITING", "w5"), ("#orchestrator", "SEAT_GRANTED", "w5"), ("#approvals", "APPROVAL_REQUEST", None)]


# ── terminal viewer ────────────────────────────────────────────────────────
def test_terminal_viewer_reads_filters_and_exports(tmp_path):
    async def fill():
        db = await open_db(tmp_path)
        bus = MessageBus(db)
        await bus.publish(topic_bot("w1"), "A2A_MESSAGE", {"text": "please build the page"}, sender_type="bot", sender_id="omi", recipient_id="w1")
        await bus.publish(topic_bot("omi"), "A2A_MESSAGE", {"text": "page built, see index.html"}, sender_type="bot", sender_id="w1", recipient_id="omi")
        await bus.publish("#orchestrator", "SEAT_WAITING", {"position": 1}, recipient_id="w2")
        await db.close()
    run(fill())
    db = tmp_path / "db.sqlite"
    base = [sys.executable, "-m", "omnibots.board", "--db", str(db)]
    all_ = subprocess.run(base, capture_output=True, text=True, encoding="utf-8", cwd=ROOT, timeout=60)
    only_w1 = subprocess.run(base + ["--bot", "w1", "--types", "A2A_MESSAGE"], capture_output=True, text=True, encoding="utf-8", cwd=ROOT, timeout=60)
    out_md = tmp_path / "board.md"
    subprocess.run(base + ["--export", "md", str(out_md)], capture_output=True, text=True, encoding="utf-8", cwd=ROOT, timeout=60)
    assert all_.returncode == 0 and "omi → w1" in all_.stdout and "please build the page" in all_.stdout and "SEAT_WAITING" in all_.stdout
    assert "page built" in only_w1.stdout and "SEAT_WAITING" not in only_w1.stdout
    md = out_md.read_text(encoding="utf-8")
    assert md.startswith("# OmniBots board export") and "> page built, see index.html" in md
