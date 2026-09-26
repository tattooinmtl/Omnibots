"""A2.a.02/03: the Python tool-call parser and think splitter reproduce Omni's
own results exactly (goldens generated from Omni's toolcalls.mjs)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from omni_helpers import NODE, REAL_OMNI, ROOT
from omnibots.providers import toolcalls as tc

GOLDENS = json.loads((Path(__file__).parent / "goldens" / "toolcalls.json").read_text(encoding="utf-8"))
REG = tc.build_param_registry(GOLDENS["tool_defs"])


@pytest.mark.parametrize("case", GOLDENS["parse"], ids=lambda c: f"{'reg' if c['registry'] else 'noreg'}:{c['input'][:40]!r}")
def test_parse_matches_omni(case):
    got = tc.parse_text_tool_calls(case["input"], REG if case["registry"] else None)
    assert [{"name": c["function"]["name"], "args": json.loads(c["function"]["arguments"])} for c in got] == case["calls"]


@pytest.mark.parametrize("case", GOLDENS["split"], ids=lambda c: repr("".join(c["chunks"])[:40]))
def test_think_splitter_matches_omni(case):
    s = tc.ThinkSplitter()
    think = answer = ""
    for chunk in case["chunks"]:
        r = s.feed(chunk)
        think += r["think"]
        answer += r["answer"]
    r = s.flush()
    assert (think + r["think"], answer + r["answer"]) == (case["think"], case["answer"])


def test_helpers_match_omni():
    for c in GOLDENS["extract_think"]:
        assert tc.extract_think(c["input"]) == {"think": c["think"], "rest": c["rest"]}
    for c in GOLDENS["strip_think"]:
        assert tc.strip_think(c["input"]) == c["output"]
    for c in GOLDENS["strip_tool_call_text"]:
        assert tc.strip_tool_call_text(c["input"]) == c["output"], c["input"]
    for c in GOLDENS["has_tool_intent"]:
        assert tc.has_tool_intent(c["input"]) == c["output"], c["input"]
    assert tc.text_tool_instructions(GOLDENS["tool_defs"][:3]) == GOLDENS["text_tool_instructions"]
    assert tc.recovery_message() == GOLDENS["recovery_message"]


def test_arguments_are_compact_json_like_omni():
    calls = tc.parse_text_tool_calls("<tool_call>\n<function=read_file>\n<parameter=path>a b</parameter>\n</function>\n</tool_call>", REG)
    assert calls[0]["function"]["arguments"] == '{"path":"a b"}'
    assert calls[0]["id"].startswith("txt_")


@pytest.mark.skipif(not NODE or not (REAL_OMNI / "src").is_dir(), reason="needs node + Omni source")
def test_goldens_are_current_with_installed_omni():
    r = subprocess.run([NODE, str(ROOT / "tools" / "gen_toolcall_goldens.mjs"), str(REAL_OMNI), "--check"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
