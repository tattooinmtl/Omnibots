"""A15.a.03: multi_provider means what it says. On (the default, and what every bot did before):
a failing provider hands over to the next one in the chain. Off: the bot stays on its first provider.
Real runner and router, the mock providers."""

from __future__ import annotations

import asyncio

from mock_provider import MockProviders, sse, status
from test_a7_orchestrator import build

from omnibots.bots.runner import provider_chain


def test_on_fails_over_and_off_stays_on_the_first_provider(tmp_path):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            multi = await e["reg"].create("Multi", "coder", chain=["work/m", "plan/m"])
            pinned = await e["reg"].create("Pinned", "coder", chain=["work/m", "plan/m"], multi_provider=False)
            mock.script("work", status(401), status(401))       # the first provider refuses (auth), no retry wait
            mock.script("plan", sse("done on plan"))
            a = await e["runner"].run(multi.id, "say hi")
            plan_after_multi = len([r for r in mock.requests if r["provider"] == "plan"])
            b = await e["runner"].run(pinned.id, "say hi")
            plan_after_pinned = len([r for r in mock.requests if r["provider"] == "plan"])
            fresh = await e["reg"].get(pinned.id)
            await e["db"].close()
            return a, b, plan_after_multi, plan_after_pinned, multi, fresh
    a, b, plan1, plan2, multi, pinned = run(go())
    assert multi.multi_provider is True and pinned.multi_provider is False       # new bots default to on; off is stored
    assert a.status == "completed" and a.result.answer == "done on plan" and plan1 == 1
    assert b.status != "completed" and plan2 == 1                                 # the pinned bot never reached "plan"
    assert provider_chain(multi) == ["work/m", "plan/m"] and provider_chain(pinned) == ["work/m"]


def run(coro):
    return asyncio.run(coro)
