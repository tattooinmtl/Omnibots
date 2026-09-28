"""A2: streaming client, 429 cooling/recovery, failover, seats and the token
reservoir, over real HTTP against a local mock provider (no tokens spent)."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from mock_provider import MockProviders, sse, status
from omnibots.db import Database
from omnibots.lineup import MINIMAX
from omnibots.omni.config import OmniConfig, ProviderInfo
from omnibots.omni.locate import OmniLocation
from omnibots.providers.client import ProviderError, build_chat_body, chat_stream, resolve_model
from omnibots.providers.quota import QuotaManager, estimate_tokens
from omnibots.providers.router import AllProvidersExhausted, Router
from omnibots.providers.seats import SeatScheduler

READ_TOOL = [{"type": "function", "function": {"name": "read_file", "description": "Read a file",
              "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}]


def make_cfg(mock: MockProviders, names=("minimax.io", "cheap_a", "cheap_b"), native=None) -> OmniConfig:
    native = native or {}
    providers = {n: ProviderInfo(name=n, base_url=mock.url(n), api_key=f"key-{n}", key_source="settings",
                                 native_tools=native.get(n, True), raw={"reasoningParam": "none"}) for n in names}
    models = {f"{n}/m": {"provider": n, "id": f"{n}-model", "maxTokens": 256} for n in names}
    return OmniConfig(location=OmniLocation(Path("."), Path(".")), providers=providers, models=models,
                      default_provider=None, default_model=None, skills=[], mcp_servers={})


class Clock:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t


def run(coro):
    return asyncio.run(coro)


# ── client ─────────────────────────────────────────────────────────────────
def test_stream_splits_answer_thinking_and_assembles_tool_calls():
    with MockProviders() as mock:
        mock.script("cheap_a", sse("<think>plan it</think>Here you go.", reasoning="deep thoughts",
                                   tool_calls=[{"name": "read_file", "args": {"path": "a.txt"}}],
                                   usage={"prompt_tokens": 11, "completion_tokens": 7}))
        cfg = make_cfg(mock)
        toks, thinks = [], []

        async def go():
            return await chat_stream(resolve_model(cfg, "cheap_a/m"), [{"role": "user", "content": "hi"}], READ_TOOL,
                                     on_token=toks.append, on_think=thinks.append)
        res = run(go())
    assert res.answer == "Here you go." and "".join(toks) == "Here you go."
    assert res.thinking == "deep thoughtsplan it" == "".join(thinks)
    assert res.message["tool_calls"] == [{"id": "call_0", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'}}]
    assert res.usage == {"prompt_tokens": 11, "completion_tokens": 7}
    assert res.finish_reason == "tool_calls"
    sent = mock.requests[0]
    assert sent["auth"] == "Bearer key-cheap_a" and sent["body"]["stream_options"] == {"include_usage": True}
    assert sent["body"]["tools"] == READ_TOOL and "reasoning_effort" not in sent["body"]   # reasoningParam "none"


def test_request_body_matches_omni_rules():
    with MockProviders() as mock:
        cfg = make_cfg(mock, native={"cheap_b": False})
        cfg.providers["cheap_a"].raw = {}                               # no reasoningParam -> reasoning_effort
        msgs = [{"role": "system", "content": "A"}, {"role": "user", "content": "q"}, {"role": "system", "content": "B"},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"x"}'}}]},
                {"role": "tool", "name": "read_file", "tool_call_id": "1", "content": "data"}]
        native = build_chat_body(resolve_model(cfg, "cheap_a/m"), msgs, READ_TOOL)
        assert native["messages"][0] == {"role": "system", "content": "A\n\nB"}          # system hoisted
        assert "name" not in native["messages"][-1] and native["reasoning_effort"] == "medium"
        text = build_chat_body(resolve_model(cfg, "cheap_b/m"), msgs, READ_TOOL)
        assert "tools" not in text and text["messages"][-1] == {"role": "user", "content": "Tool result (read_file):\ndata"}


def test_text_protocol_provider_gets_instructions_and_history_in_text_form():
    with MockProviders() as mock:
        mock.script("cheap_b", sse("<tool_call>\n<function=read_file>\n<parameter=path>b.txt</parameter>\n</function>\n</tool_call>"))
        cfg = make_cfg(mock, native={"cheap_b": False})
        res = run(chat_stream(resolve_model(cfg, "cheap_b/m"), [{"role": "system", "content": "You are Omi."}, {"role": "user", "content": "read b"}], READ_TOOL))
        system = mock.requests[0]["body"]["messages"][0]["content"]
    assert system.startswith("You are Omi.\n") and "# Tool Calling Protocol" in system and '"read_file"' in system
    from omnibots.providers.toolcalls import parse_text_tool_calls
    assert json.loads(parse_text_tool_calls(res.message["content"])[0]["function"]["arguments"]) == {"path": "b.txt"}


def test_errors_are_classified_with_reset_times():
    with MockProviders() as mock:
        mock.script("cheap_a", status(429, headers={"Retry-After": "7"}),
                    status(429, headers={"x-ratelimit-reset-requests": "1m30s"}),
                    status(401, {"error": {"message": "bad key"}}), status(503))
        cfg = make_cfg(mock)
        m = resolve_model(cfg, "cheap_a/m")
        errs = []
        for _ in range(4):
            with pytest.raises(ProviderError) as e:
                run(chat_stream(m, [{"role": "user", "content": "x"}]))
            errs.append(e.value)
    assert errs[0].rate_limited and errs[0].retry_after_seconds() == 7
    assert errs[1].retry_after_seconds() == 90
    assert errs[2].auth_failed and "/apikey cheap_a" in str(errs[2]) and not errs[2].retryable
    assert errs[3].retryable and not errs[3].rate_limited


# ── router: failover, cooling, recovery ────────────────────────────────────
def test_429_fails_over_cools_and_recovers(tmp_path):
    async def go():
        db = Database(tmp_path / "db.sqlite")
        await db.open()
        clock = Clock()
        events = []
        quota = QuotaManager(db, clock=clock, on_event=lambda k, d: events.append(k))
        with MockProviders() as mock:
            mock.script("cheap_a", status(429, headers={"Retry-After": "30"}), sse("a is back", usage={"prompt_tokens": 5, "completion_tokens": 2}))
            mock.default["cheap_b"] = sse("from b", usage={"prompt_tokens": 5, "completion_tokens": 2})
            cfg = make_cfg(mock)
            router = Router(lambda: cfg, quota, SeatScheduler(), base_delay=0.01)
            chain = ["cheap_a/m", "cheap_b/m"]
            r1 = await router.chat("bot_x", chain, [{"role": "user", "content": "1"}])
            r2 = await router.chat("bot_x", chain, [{"role": "user", "content": "2"}])     # a still cooling -> b
            clock.t += 31                                                                   # reset window passes
            r3 = await router.chat("bot_x", chain, [{"role": "user", "content": "3"}])
            hits_a = [r for r in mock.requests if r["provider"] == "cheap_a"]
        reloaded = QuotaManager(db, clock=clock)
        await reloaded.load()
        rows = await db.read("SELECT provider, status_code, tokens_in FROM provider_usage_events ORDER BY id")
        await db.close()
        return r1, r2, r3, hits_a, events, quota, reloaded, rows

    r1, r2, r3, hits_a, events, quota, reloaded, rows = run(go())
    assert r1.result.answer == "from b" and [h["outcome"] for h in r1.hops] == ["rate_limited", "ok"]
    assert r2.result.answer == "from b" and r2.hops[0]["model"] == "cheap_b/m"     # a skipped, no request sent
    assert len(hits_a) == 2                                                         # the 429 + the recovery call
    assert r3.result.answer == "a is back" and "provider_recovered" in events
    assert events.index("provider_cooling") < events.index("provider_recovered")
    assert quota.state("cheap_a").status == "ok" and reloaded.state("cheap_a").status == "ok"
    assert [r["status_code"] for r in rows] == [429, 200, 200, 200]


def test_cooling_state_survives_restart(tmp_path):
    async def go():
        db = Database(tmp_path / "db.sqlite")
        await db.open()
        q = QuotaManager(db)
        await q.on_rate_limited("cheap_a", retry_after=600, model="m", bot_id=None, job_id=None, latency_ms=1)
        q2 = QuotaManager(db)
        await q2.load()
        await db.close()
        return q2
    q2 = run(go())
    assert q2.state("cheap_a").status == "cooling" and not q2.available("cheap_a")


def test_5xx_retries_same_hop_with_backoff():
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    with MockProviders() as mock:
        mock.script("cheap_a", status(503), status(502), sse("third time lucky"))
        cfg = make_cfg(mock)
        router = Router(lambda: cfg, QuotaManager(), SeatScheduler(), base_delay=3.0, sleep=fake_sleep)
        r = run(router.chat("b", ["cheap_a/m", "cheap_b/m"], [{"role": "user", "content": "x"}]))
    assert r.result.answer == "third time lucky" and sleeps == [3.0, 6.0]            # Omni's 3s, 6s, 12s… backoff
    assert r.estimated and r.tokens_in == estimate_tokens([{"role": "user", "content": "x"}])


def test_bad_key_marks_provider_broken_and_moves_on():
    with MockProviders() as mock:
        mock.script("cheap_a", status(401, {"error": {"message": "invalid api key"}}))
        mock.default["cheap_b"] = sse("b works")
        cfg = make_cfg(mock)
        quota = QuotaManager()
        router = Router(lambda: cfg, quota, SeatScheduler())
        r = run(router.chat("b", ["cheap_a/m", "cheap_b/m"], [{"role": "user", "content": "x"}]))
    assert r.result.answer == "b works" and quota.state("cheap_a").status == "error"
    assert not quota.available("cheap_a")
    quota.clear_error("cheap_a")                                                      # user fixed the key in Omni
    assert quota.available("cheap_a")


def test_other_4xx_is_raised_not_failed_over():
    with MockProviders() as mock:
        mock.script("cheap_a", status(400, {"error": {"message": "bad request"}}))
        cfg = make_cfg(mock)
        router = Router(lambda: cfg, QuotaManager(), SeatScheduler())
        with pytest.raises(ProviderError) as e:
            run(router.chat("b", ["cheap_a/m", "cheap_b/m"], [{"role": "user", "content": "x"}]))
    assert e.value.status == 400


# ── seats ─────────────────────────────────────────────────────────────────
def test_minimax_never_exceeds_4_sessions_and_seat_1_is_the_boss():
    async def go():
        with MockProviders() as mock:
            step = sse("ok")
            step["hold"] = 0.4                               # keep each call open so they overlap
            mock.default["minimax.io"] = step
            mock.default["cheap_a"] = step
            cfg = make_cfg(mock)
            seats = SeatScheduler(boss_id="omi")
            router = Router(lambda: cfg, QuotaManager(), seats, base_delay=0.01)
            chain = ["minimax.io/m", "cheap_a/m"]
            workers = [router.chat(f"w{i}", chain, [{"role": "user", "content": str(i)}]) for i in range(8)]
            boss = router.chat("omi", chain, [{"role": "user", "content": "boss"}])
            results = await asyncio.gather(boss, *workers)
            return results, dict(mock.peak), seats
    results, peak, seats = run(go())
    # failed once in a full run (2026-09-26) and never again in ~45 runs, even under CPU load: say what happened
    seen = f"peak={peak} providers={[r.model.provider_name for r in results]}"
    assert peak["minimax.io"] <= 4, seen
    assert results[0].model.provider_name == MINIMAX, seen                   # the boss always gets seat 1
    worker_on_minimax = sum(r.model.provider_name == MINIMAX for r in results[1:])
    assert worker_on_minimax == 3 and peak["cheap_a"] >= 5, seen             # 3 lent seats, the rest used the cheap lane
    assert all(s.holder is None for s in seats.seats)                         # every seat released


def test_seat_priority_and_reentrancy():
    async def go():
        s = SeatScheduler(seats=2, boss_id="omi")          # 1 boss seat + 1 shared
        order = []
        first = await s.acquire("w1")
        assert s.try_acquire("w1") is first and first.depth == 2
        s.release(first)
        assert first.holder == "w1"                          # still held (re-entrant)

        async def want(bot, prio):
            seat = await s.acquire(bot, prio)
            order.append(bot)
            s.release(seat)
        t_work = asyncio.create_task(want("w2", "work"))
        await asyncio.sleep(0)
        t_review = asyncio.create_task(want("rev", "review"))
        await asyncio.sleep(0)
        assert s.try_acquire("late") is None                # no queue jumping
        s.release(first)
        await asyncio.gather(t_work, t_review)
        boss = s.try_acquire("omi")
        return order, boss
    order, boss = run(go())
    assert order == ["rev", "w2"] and boss.number == 1


def test_all_exhausted_waits_for_a_seat_then_uses_minimax():
    async def go():
        with MockProviders() as mock:
            mock.default["cheap_a"] = status(429, headers={"Retry-After": "300"})
            mock.default["minimax.io"] = sse("minimax answered")
            cfg = make_cfg(mock)
            seats = SeatScheduler(seats=2, boss_id="omi")
            quota = QuotaManager()
            router = Router(lambda: cfg, quota, seats, base_delay=0.01, max_wait=5)
            blocker = await seats.acquire("other")            # the only worker seat is busy
            waiting = asyncio.Event()
            call = asyncio.create_task(router.chat("w", ["cheap_a/m", "minimax.io/m"], [{"role": "user", "content": "x"}],
                                                   on_hop=lambda h: waiting.set() if h.get("outcome") == "waiting" else None))
            await asyncio.wait_for(waiting.wait(), 10)        # the router is really waiting (not a guess at how long that takes)
            assert not call.done()                            # waiting, not failing
            seats.release(blocker)
            return await asyncio.wait_for(call, 5)
    r = run(go())
    assert r.result.answer == "minimax answered" and any(h["outcome"] == "waiting" for h in r.hops)


def test_gives_up_only_after_max_wait():
    with MockProviders() as mock:
        mock.default["cheap_a"] = status(429, headers={"Retry-After": "300"})
        cfg = make_cfg(mock)
        router = Router(lambda: cfg, QuotaManager(), SeatScheduler(), max_wait=0.3)
        with pytest.raises(AllProvidersExhausted) as e:
            run(router.chat("b", ["cheap_a/m"], [{"role": "user", "content": "x"}]))
    assert e.value.next_reset_in and e.value.next_reset_in > 200


# ── reservoir ─────────────────────────────────────────────────────────────
def test_reservoir_thresholds_fire_once_and_forecast(tmp_path):
    async def go():
        db = Database(tmp_path / "db.sqlite")
        await db.open()
        events = []
        q = QuotaManager(db, on_event=lambda k, d: events.append((k, d.get("threshold"))), minimax_budget=1000)
        for _ in range(10):
            await q.on_success(MINIMAX, model="MiniMax-M3", bot_id="omi", job_id=None, tokens_in=60, tokens_out=40, estimated=False, latency_ms=5)
        fc = await q.forecast()
        q2 = QuotaManager(db, minimax_budget=1000)
        await q2.load()
        await db.close()
        return events, fc, q2
    events, fc, q2 = run(go())
    assert [t for k, t in events if k == "reservoir_threshold"] == [0.5, 0.75, 0.9]
    assert fc["tokens_used"] == 1000 and fc["tokens_left"] == 0 and fc["tokens_per_day"] == pytest.approx(1000)
    assert q2.state(MINIMAX).tokens_used == 1000 and q2.state(MINIMAX).thresholds_hit == {0.5, 0.75, 0.9}
