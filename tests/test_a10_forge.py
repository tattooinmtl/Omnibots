"""A10.b.01: the Tool Forge. Real sandbox (pytest runs in the AppContainer), real reviewer
(mock model scripted to PASS/FAIL), the tools table, forged tools reused from the pool."""

from __future__ import annotations

import asyncio

from mock_provider import MockProviders, sse
from test_a7_orchestrator import build, call
from omnibots.bots.profile import DEFAULT_TOOLS
from omnibots.orchestrator.forge import ToolForge
from omnibots.runtime.sandbox import Sandbox
from omnibots.runtime.tools import ToolContext

GOOD_TOOL = '''
from omnibots.runtime.tools import Tool, ToolContext


async def roman(args, ctx):
    n = int(args.get("number", 0))
    if not 0 < n < 4000:
        return "ERROR: number must be 1..3999"
    table = [(1000,"M"),(900,"CM"),(500,"D"),(400,"CD"),(100,"C"),(90,"XC"),(50,"L"),(40,"XL"),(10,"X"),(9,"IX"),(5,"V"),(4,"IV"),(1,"I")]
    out = ""
    for v, sym in table:
        while n >= v:
            out += sym; n -= v
    return out


def build():
    return Tool("to_roman", "Convert an integer (1-3999) to Roman numerals.",
                {"type": "object", "properties": {"number": {"type": "integer"}}, "required": ["number"]},
                "R0", roman, path_arg=None)
'''

GOOD_TEST = '''
import asyncio
from tool import build


def test_roman():
    t = build()
    assert t.name == "to_roman" and t.risk == "R0"
    assert asyncio.run(t.fn({"number": 2024}, None)) == "MMXXIV"
    assert asyncio.run(t.fn({"number": 4}, None)) == "IV"
    assert asyncio.run(t.fn({"number": 0}, None)).startswith("ERROR")
'''

BANNED_TOOL = GOOD_TOOL.replace("from omnibots.runtime.tools import Tool, ToolContext",
                                "import subprocess\nfrom omnibots.runtime.tools import Tool, ToolContext")
R3_TOOL = GOOD_TOOL.replace('"R0", roman', '"R3", roman')
FAILING_TEST = GOOD_TEST.replace('== "MMXXIV"', '== "WRONG"')


def run(coro):
    return asyncio.run(coro)


def make_forge(e, verdict="PASS"):
    return ToolForge(e["db"], e["projects"].dir.parent, Sandbox(e["projects"].dir.parent / "sandbox"),
                     e["router"], bus=e["bus"], reviewer_chain=["plan/m"])


def test_forge_accepts_a_good_tool_and_rejects_bad_ones(tmp_path):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            forge = make_forge(e)
            mock.script("plan", sse("VERDICT: PASS\nThe tool is a pure function, well tested, correctly R0."))
            ok = await forge.submit(name="to_roman", code=GOOD_TOOL, test_code=GOOD_TEST, created_by="bot_x")
            banned = await forge.submit(name="sneaky", code=BANNED_TOOL.replace("to_roman", "sneaky"),
                                        test_code=GOOD_TEST.replace("to_roman", "sneaky"), created_by="bot_x")
            r3 = await forge.submit(name="too_risky", code=R3_TOOL.replace("to_roman", "too_risky"),
                                    test_code=GOOD_TEST.replace("to_roman", "too_risky"), created_by="bot_x")
            failing = await forge.submit(name="buggy", code=GOOD_TOOL.replace("to_roman", "buggy"),
                                         test_code=FAILING_TEST.replace("to_roman", "buggy"), created_by="bot_x")
            mock.script("plan", sse("VERDICT: FAIL\n[BLOCKER] does not actually convert numbers"))
            reviewed_out = await forge.submit(name="rejected_by_review", code=GOOD_TOOL.replace("to_roman", "rejected_by_review"),
                                              test_code=GOOD_TEST.replace("to_roman", "rejected_by_review"), created_by="bot_x")
            from omnibots.board.query import query
            created = [m.payload["name"] for m in await query(e["db"], types=["TOOL_CREATED"])]
            rows = {r["name"]: r["status"] for r in await e["db"].read("SELECT name, status FROM tools WHERE origin='forge'")}
            active = [t.name for t in forge.load_active()]
            await e["db"].close()
            return ok, banned, r3, failing, reviewed_out, created, rows, active
    ok, banned, r3, failing, reviewed_out, created, rows, active = run(go())
    assert ok["ok"] and ok["risk"] == "R0"
    assert not banned["ok"] and banned["stage"] == "static-check" and "subprocess" in banned["error"]
    assert not r3["ok"] and r3["stage"] == "risk"
    assert not failing["ok"] and failing["stage"] == "tests"
    assert not reviewed_out["ok"] and reviewed_out["stage"] == "review"
    assert created == ["to_roman"]                              # only the good one was announced
    assert rows.get("to_roman") == "active" and "sneaky" not in rows
    assert "to_roman" in active                                 # it loads back into the pool for reuse


def test_a_bot_forges_a_tool_via_create_tool_and_the_team_reuses_it(tmp_path):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            e["runner"].forge = make_forge(e)
            builder = await e["reg"].create("Builder", "toolsmith", chain=["work/m"], tools=[*DEFAULT_TOOLS, "create_tool"])
            forge_call = {"name": "create_tool", "args": {"name": "to_roman", "description": "roman numerals",
                                                          "code": GOOD_TOOL, "test_code": GOOD_TEST}}
            mock.script("work", sse("", tool_calls=[forge_call]), sse("done: tool forged"))
            mock.script("plan", sse("VERDICT: PASS\nSafe pure function."))
            out = await e["runner"].run(builder.id, "we need a roman-numeral tool")
            result = [m["content"] for r in mock.requests if r["provider"] == "work" for m in r["body"]["messages"] if m.get("role") == "tool"][-1]
            # a NEW bot gets it in its pool and can call it
            user = await e["reg"].create("User", "writer", chain=["work/m"], tools=["to_roman", "write_file"])
            pool_names = sorted(e["runner"].tools_for(user, None, None).tools)
            ctx = ToolContext(bot_id=user.id, workspace=user.workspace)
            called = await e["runner"].tools_for(user, None, None).run("to_roman", {"number": 49}, ctx)
            await e["db"].close()
            return out, result, pool_names, called
    out, result, pool_names, called = run(go())
    assert out.status == "completed" and "passed tests + review" in result
    assert "to_roman" in pool_names and called == "XLIX"
