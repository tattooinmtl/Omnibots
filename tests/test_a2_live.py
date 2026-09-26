"""A2.99 live acceptance against the REAL providers (spends a few thousand tokens).

Skipped unless OMNIBOTS_LIVE=1:
    set OMNIBOTS_LIVE=1 && python -m pytest tests/test_a2_live.py -v
"""

from __future__ import annotations

import asyncio
import os

import pytest

from omnibots.lineup import TARGET_PROVIDERS

pytestmark = pytest.mark.skipif(os.environ.get("OMNIBOTS_LIVE") != "1", reason="live provider test; set OMNIBOTS_LIVE=1")


def test_every_provider_completes_a_real_tool_call_and_minimax_takes_4_at_once(home):
    from omnibots.providers.probe import run_probe

    report = asyncio.run(run_probe(None, 4))
    by = {r["provider"]: r for r in report["results"]}
    assert set(by) == set(TARGET_PROVIDERS)
    for name, r in by.items():
        assert "skipped" not in r, f"{name}: {r.get('skipped')}"
        assert r["single"]["tool_call_ok"], f"{name}: tool call failed: {r['single']}"
    assert by["minimax.io"]["parallel"]["ok"] == 4, by["minimax.io"]["parallel"]
    assert by["nvidia"]["single"]["tool_mode"] == "text"          # Omni's text protocol, live
