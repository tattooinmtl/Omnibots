"""Failover routing across providers (PLAN.md A2.b.04, ADR-11).

One call = walk the bot's chain of models:
  - skip providers that are cooling after a 429 (or broken: bad key);
  - MiniMax needs a seat: use it only if one is free (or the bot already holds
    one); otherwise move on;
  - 429 -> mark the provider cooling (Retry-After / reset headers, else an
    escalating default) and try the next hop right away;
  - 502/503/504 or network trouble -> retry the same hop with backoff
    (Omni's 3s, 6s, 12s… capped at MAX_RETRIES), then move on;
  - 401/403 -> mark the provider broken (the user fixes the key in Omni) and
    move on;
  - other 4xx -> it's our request, raise.
When every hop is unusable, wait for whichever comes first: a cooling provider
resetting, or a MiniMax seat coming free. Only give up (AllProvidersExhausted,
which pauses the bot and notifies the user) when that wait exceeds max_wait.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from omnibots.lineup import MINIMAX
from omnibots.omni.config import OmniConfig
from omnibots.providers.client import ChatResult, ModelSpec, ProviderError, TokenCallback, chat_stream, resolve_model
from omnibots.providers.quota import QuotaManager, estimate_tokens
from omnibots.providers.seats import SeatScheduler

log = logging.getLogger(__name__)

MAX_RETRIES = 5
BASE_DELAY = 3.0


class AllProvidersExhausted(RuntimeError):
    def __init__(self, chain: list[str], next_reset_in: float | None):
        self.chain = chain
        self.next_reset_in = next_reset_in
        when = f"; the next one resets in {next_reset_in:.0f}s" if next_reset_in else ""
        super().__init__(f"every provider in the chain is unavailable ({' -> '.join(chain)}){when}")


@dataclass
class RoutedResult:
    result: ChatResult
    model: ModelSpec
    hops: list[dict[str, Any]] = field(default_factory=list)   # what was tried, for the console/board
    tokens_in: int | None = None
    tokens_out: int | None = None
    estimated: bool = False


class Router:
    def __init__(self, config: Callable[[], OmniConfig], quota: QuotaManager, seats: SeatScheduler, *,
                 client: httpx.AsyncClient | None = None, base_delay: float = BASE_DELAY,
                 max_retries: int = MAX_RETRIES, max_wait: float = 30 * 60, sleep=asyncio.sleep):
        self.config = config
        self.quota = quota
        self.seats = seats
        self.client = client
        self.base_delay = base_delay
        self.max_retries = max_retries
        self.max_wait = max_wait
        self.sleep = sleep

    async def chat(self, bot_id: str, chain: list[str], messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, *,
                   job_id: str | None = None, priority: str = "work",
                   on_token: TokenCallback | None = None, on_think: TokenCallback | None = None,
                   on_hop: Callable[[dict[str, Any]], Any] | None = None) -> RoutedResult:
        cfg = self.config()
        models = []
        for key in chain:
            try:
                models.append(resolve_model(cfg, key))
            except KeyError as exc:
                log.warning("skipping %s: %s", key, exc)
        if not models:
            raise AllProvidersExhausted(chain, None)
        hops: list[dict[str, Any]] = []
        deadline = time.monotonic() + self.max_wait

        while True:
            for model in models:
                if not self.quota.available(model.provider_name):
                    continue
                seat = None
                if model.provider_name == MINIMAX:
                    seat = self.seats.try_acquire(bot_id)
                    if seat is None:
                        hops.append({"model": model.key, "outcome": "no_free_seat"})
                        continue
                try:
                    outcome = await self._try_model(bot_id, job_id, model, messages, tools, on_token, on_think, hops, on_hop)
                finally:
                    if seat is not None:
                        self.seats.release(seat)
                if outcome is not None:
                    return outcome

            # Nothing usable right now: wait for a reset or a free seat.
            names = [m.provider_name for m in models]
            next_reset = self.quota.next_available_at(names)
            wait = None if next_reset is None else max(0.5, next_reset - time.time())
            wants_seat = any(m.provider_name == MINIMAX and self.quota.available(MINIMAX) for m in models)
            remaining = deadline - time.monotonic()
            if remaining <= 0 or (wait is None and not wants_seat):
                raise AllProvidersExhausted([m.key for m in models], wait)
            wait = min(wait if wait is not None else remaining, remaining)
            hops.append({"outcome": "waiting", "seconds": round(wait, 1), "for": "seat or reset" if wants_seat else "reset"})
            if on_hop:
                on_hop(hops[-1])
            if wants_seat:
                await self.seats.wait_for_release(timeout=wait)
            else:
                await self.sleep(wait)

    async def _try_model(self, bot_id, job_id, model: ModelSpec, messages, tools, on_token, on_think, hops, on_hop) -> RoutedResult | None:
        attempt = 0
        while True:
            t0 = time.perf_counter()
            try:
                res = await chat_stream(model, messages, tools, on_token=on_token, on_think=on_think, client=self.client)
            except ProviderError as err:
                ms = int((time.perf_counter() - t0) * 1000)
                hop = {"model": model.key, "status": err.status, "error": err.detail.split("\n")[0][:200]}
                if err.rate_limited:
                    await self.quota.on_rate_limited(model.provider_name, retry_after=err.retry_after_seconds(),
                                                     model=model.id, bot_id=bot_id, job_id=job_id, latency_ms=ms)
                    hops.append({**hop, "outcome": "rate_limited"})
                    on_hop and on_hop(hops[-1])
                    return None
                if err.auth_failed or err.fatal:
                    await self.quota.on_error(model.provider_name, status_code=err.status, message=err.detail, fatal=True,
                                              model=model.id, bot_id=bot_id, job_id=job_id, latency_ms=ms)
                    hops.append({**hop, "outcome": "auth_failed"})
                    on_hop and on_hop(hops[-1])
                    return None
                if err.retryable or err.status is None:
                    attempt += 1
                    await self.quota.on_error(model.provider_name, status_code=err.status, message=err.detail, fatal=False,
                                              model=model.id, bot_id=bot_id, job_id=job_id, latency_ms=ms)
                    if attempt > self.max_retries:
                        hops.append({**hop, "outcome": "gave_up"})
                        on_hop and on_hop(hops[-1])
                        return None
                    delay = self.base_delay * 2 ** (attempt - 1)
                    hops.append({**hop, "outcome": "retry", "attempt": attempt, "delay_s": delay})
                    on_hop and on_hop(hops[-1])
                    await self.sleep(delay)
                    continue
                await self.quota.on_error(model.provider_name, status_code=err.status, message=err.detail, fatal=False,
                                          model=model.id, bot_id=bot_id, job_id=job_id, latency_ms=ms)
                raise
            usage = res.usage or {}
            tin, tout = usage.get("prompt_tokens"), usage.get("completion_tokens")
            estimated = tin is None or tout is None
            if estimated:
                tin = estimate_tokens(messages)
                calls = res.message.get("tool_calls") or []
                tout = math.ceil((len(res.message.get("content") or "") + sum(len(c["function"]["arguments"]) for c in calls)) / 4)
            await self.quota.on_success(model.provider_name, model=model.id, bot_id=bot_id, job_id=job_id,
                                        tokens_in=tin, tokens_out=tout, estimated=estimated, latency_ms=res.latency_ms)
            hops.append({"model": model.key, "outcome": "ok", "latency_ms": res.latency_ms})
            on_hop and on_hop(hops[-1])
            return RoutedResult(res, model, hops, tin, tout, estimated)
