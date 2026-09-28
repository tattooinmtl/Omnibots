"""A8: skill & tool pools. Skills on demand, the rest of the core tools (grep,
run_shell in the sandbox with Omni's command risk, git, locks, ask_help),
per-bot tool enforcement through a real bot run, the MCP client against
Omni's real `okf` server, the MiniMax media tools (HTTP mocked: video costs
money), playbooks (versions, A/B variants, retirement, planner lookup) and
the retrospective. Mock providers, no tokens."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import httpx
import pytest

from mock_provider import MockProviders, sse
from test_a7_orchestrator import build, call, plan_json, sub
from omnibots.board.a2a import Inbox
from omnibots.board.query import query
from omnibots.bots.profile import BOSS_ID, DEFAULT_TOOLS
from omnibots.mcp_client import MCPManager, mcp_refs
from omnibots.omni.config import SkillInfo
from omnibots.orchestrator.boss_tools import BossToolkit, GoalContext
from omnibots.orchestrator.factory import is_known_tool
from omnibots.orchestrator.playbooks import FAIL_STREAK, TRIALS, PlaybookStore, retrospective
from omnibots.runtime.minimax_tools import minimax_tools
from omnibots.runtime.more_tools import board_tools, command_risk
from omnibots.runtime.core_tools import core_registry
from omnibots.runtime.sandbox import Sandbox
from omnibots.runtime.skill_tools import SkillPool, skill_tools
from omnibots.runtime.tools import ToolContext


def run(coro):
    return asyncio.run(coro)


def ctx_for(ws: Path, sandbox: Sandbox | None = None, events: list | None = None) -> ToolContext:
    async def emit(kind, content):
        if events is not None:
            events.append((kind, content))
    return ToolContext(bot_id="bot_t", workspace=ws, job_id="job_t", emit=emit, sandbox=sandbox)


# ── risk classification (port of Omni's commandRisk) ───────────────────────
@pytest.mark.parametrize("cmd,risk", [
    ("dir", "R1"), ("python -m pytest -q", "R1"), ("git status", "R1"),
    ("npm install left-pad", "R3"), ("pip install requests", "R3"), ("git push origin main", "R3"),
    ("curl https://example.com", "R3"), ("docker run alpine", "R3"),
    ("rmdir /s /q build", "R5"), ("rm -rf node_modules", "R5"), ("del /q *.tmp", "R5"), ("git reset --hard HEAD~1", "R5"),
    ("reg add HKCU\\Software\\X /v a /d b", "R5"), ("shutdown /s /t 0", "R5"), ("schtasks /create /tn x /tr y", "R5"),
    ("Set-ExecutionPolicy Bypass", "R5"), ("netsh advfirewall set allprofiles state off", "R5"),
])
def test_command_risk(cmd, risk):
    assert command_risk(cmd)[0] == risk


def test_run_shell_risk_can_only_go_up(tmp_path):
    shell = core_registry().get("run_shell")
    c = ctx_for(tmp_path)
    assert shell.risk_for({"command": "echo hi"}, c) == "R1"
    assert shell.risk_for({"command": "rmdir /s /q x"}, c) == "R5"
    assert "recursively deletes" in shell.describe({"command": "rmdir /s /q x"})


# ── core extra tools, for real ─────────────────────────────────────────────
def test_grep_find_shell_and_git_in_the_workspace(tmp_path):
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "app.py").write_text("def main():\n    return 42  # TODO tidy\n", encoding="utf-8")
    (ws / "notes.md").write_text("# Notes\ntodo: nothing\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
    reg, sb, events = core_registry(), Sandbox(tmp_path / "sandbox"), []
    c = ctx_for(ws, sb, events)

    async def go():
        r = {}
        r["grep"] = await reg.run("grep", {"pattern": "todo"}, c)
        r["grep_py"] = await reg.run("grep", {"pattern": "todo", "glob": "*.py", "case_sensitive": True}, c)
        r["find"] = await reg.run("find_files", {"pattern": "*.py"}, c)
        r["escape"] = await reg.run("grep", {"pattern": "x", "path": ".."}, c)
        r["shell"] = await reg.run("run_shell", {"command": "echo hello from %CD% && set"}, c)
        r["status"] = await reg.run("git_status", {}, c)
        r["commit"] = await reg.run("git_commit", {"message": "first"}, c)
        r["status2"] = await reg.run("git_status", {}, c)
        (ws / "notes.md").write_text("# Notes\nchanged\n", encoding="utf-8")
        r["diff"] = await reg.run("git_diff", {}, c)
        return r
    r = run(go())
    assert "src/app.py:2:" in r["grep"] and "notes.md:2:" in r["grep"]
    assert r["grep_py"] == "no matches"                         # case-sensitive: TODO ≠ todo
    assert r["find"].strip() == "src/app.py"
    assert "outside your workspace" in r["escape"]
    assert "hello from" in r["shell"] and str(ws.name) in r["shell"]
    assert "API_KEY" not in r["shell"].upper().replace("OMNIBOTS", "")       # S0: no secrets in the environment
    assert any(k == "terminal" and v.startswith("$ echo") for k, v in events)
    assert "??" in r["status"] and r["commit"].startswith("committed ") and "??" not in r["status2"]
    assert "+changed" in r["diff"]


def test_lock_file_and_ask_help(tmp_path):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            from omnibots.board.locks import LeaseManager
            locks = LeaseManager(e["db"], e["bus"])
            tools = {t.name: t for t in board_tools(locks=locks, bus=e["bus"], boss_id=BOSS_ID)}
            a = ToolContext(bot_id="bot_a", workspace=tmp_path)
            b = ToolContext(bot_id="bot_b", workspace=tmp_path)
            got = await tools["lock_file"].fn({"path": "x.md"}, a)
            busy = await tools["lock_file"].fn({"path": "X.md", "wait_seconds": 0.2}, b)      # same file, other case
            freed = await tools["unlock_file"].fn({"path": "x.md"}, a)
            got_b = await tools["lock_file"].fn({"path": "x.md", "wait_seconds": 1}, b)
            helped = await tools["ask_help"].fn({"text": "which folder is the site in?"}, a)
            msgs = await query(e["db"], types=["HELP_REQUEST"])
            await e["db"].close()
            return got, busy, freed, got_b, helped, msgs
    got, busy, freed, got_b, helped, msgs = run(go())
    assert got.startswith("locked") and "locked by bot_a" in busy and freed == "unlocked" and got_b.startswith("locked")
    assert "asked omi" in helped
    assert msgs and msgs[-1].sender_id == "bot_a" and msgs[-1].recipient_id == BOSS_ID


# ── skills on demand ───────────────────────────────────────────────────────
def _skill(tmp: Path, folder: str, name: str, desc: str, body: str) -> SkillInfo:
    f = tmp / folder / name / "SKILL.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(f"---\nname: {name}\ndescription: {desc}\n---\n{body}\n", encoding="utf-8")
    return SkillInfo(name=name, command="/" + name, description=desc, path=f, source="bundled", category="x")


def test_skill_pool_search_invoke_and_override(tmp_path):
    omni = [_skill(tmp_path, "omni", "pdf-tools", "Read, merge and split PDF files", "Use pypdf."),
            _skill(tmp_path, "omni", "deploy-static", "Deploy a static website to Netlify or Cloudflare Pages", "OMNI VERSION"),
            _skill(tmp_path, "omni", "git-helper", "Everyday git commands", "git it")]
    _skill(tmp_path, "own", "deploy-static", "Deploy a static website (OmniBots house rules)", "OMNIBOTS VERSION: rehearse first.")
    pool = SkillPool(lambda: omni, tmp_path / "own")
    tools = {t.name: t for t in skill_tools(pool)}
    events: list = []
    c = ctx_for(tmp_path, events=events)

    async def go():
        return (await tools["find_skill"].fn({"query": "put my website online (static site deploy)"}, c),
                await tools["invoke_skill"].fn({"name": "/deploy-static"}, c),
                await tools["invoke_skill"].fn({"name": "nope"}, c),
                await tools["find_skill"].fn({"query": "quantum chromodynamics"}, c))
    found, body, missing, nothing = run(go())
    assert found.splitlines()[0].startswith("- deploy-static [omnibots]")
    assert "OMNIBOTS VERSION" in body and "OMNI VERSION" not in body
    assert ("console", "  📘 skill loaded: deploy-static") in events
    assert missing.startswith("ERROR") and nothing.startswith("no matching skills")
    assert [s.name for s in pool.all()].count("deploy-static") == 1


# ── per-bot tool enforcement (A8.b.03), through a real bot run ─────────────
def test_a_bot_without_run_shell_cannot_run_a_shell(tmp_path):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            e["runner"].skill_pool = SkillPool(lambda: [], None)
            from omnibots.board.locks import LeaseManager
            e["runner"].locks = LeaseManager(e["db"], e["bus"])
            plain = await e["reg"].create("Plain", "writer", chain=["work/m"], tools=list(DEFAULT_TOOLS))
            sheller = await e["reg"].create("Shell", "coder", chain=["slow/m"], tools=[*DEFAULT_TOOLS, "run_shell"])
            mock.script("work", sse("", tool_calls=[call("run_shell", command="echo SHOULD_NOT_RUN")]), sse("could not"))
            mock.script("slow", sse("", tool_calls=[call("run_shell", command="echo RAN_%USERNAME%_OK")]), sse("done"))
            names = {b: sorted(e["runner"].tools_for(await e["reg"].get(b), None, None).tools) for b in (plain.id, sheller.id)}
            o1 = await e["runner"].run(plain.id, "run a shell command")
            o2 = await e["runner"].run(sheller.id, "run a shell command")
            reqs = {p: [r for r in mock.requests if r["provider"] == p] for p in ("work", "slow")}
            await e["db"].close()
            return names, o1, o2, reqs, plain.id, sheller.id
    names, o1, o2, reqs, pid, sid = run(go())
    assert "run_shell" not in names[pid] and "run_shell" in names[sid]
    assert "find_skill" in names[pid] and "ask_help" in names[pid]
    # the model was never even offered run_shell
    assert "run_shell" not in [t["function"]["name"] for t in reqs["work"][0]["body"].get("tools", [])]
    tool_result = [m for m in reqs["work"][1]["body"]["messages"] if m.get("role") == "tool"][-1]["content"]
    assert 'ERROR: unknown tool "run_shell"' in tool_result and "SHOULD_NOT_RUN" not in tool_result
    ok_result = [m for m in reqs["slow"][1]["body"]["messages"] if m.get("role") == "tool"][-1]["content"]
    assert "RAN_" in ok_result and "_OK" in ok_result and "exit code 0" in ok_result
    assert o1.status == "completed" and o2.status == "completed"


def test_factory_pool_names():
    assert all(is_known_tool(t) for t in ["run_shell", "git_commit", "invoke_skill", "text_to_speech", "mcp:okf", "mcp:okf.okf_search"])
    assert not any(is_known_tool(t) for t in ["rm_rf", "mcp:", "mcp:okf;rm", "shell"])
    assert mcp_refs(["read_file", "mcp:okf.okf_search", "mcp:okf.okf_get", "mcp:gh"]) == {"okf": {"okf_search", "okf_get"}, "gh": None}
    assert mcp_refs(["mcp:okf.okf_get", "mcp:okf"]) == {"okf": None}


# ── MCP: Omni's real okf server over stdio ─────────────────────────────────
def _omni_okf():
    from omnibots.omni.config import load_omni_config
    from omnibots.omni.locate import OmniNotFound, locate_omni
    try:
        cfg = load_omni_config(locate_omni(""))
    except OmniNotFound:
        pytest.skip("Omni is not installed")
    if "okf" not in cfg.mcp_servers:
        pytest.skip("Omni has no okf MCP server")
    return cfg.mcp_servers


def test_mcp_okf_tools_risk_and_call(tmp_path):
    servers = _omni_okf()

    async def go():
        m = MCPManager(servers, {"okf.okf_search": "R0", "okf.*": "R3"})
        try:
            all_tools = await m.tools("okf")
            some = await m.tools("okf", {"okf_search"})
            search = some[0]
            res = await search.fn({"query": "omni"}, ToolContext(bot_id="t", workspace=tmp_path))
            again = await m.session("okf")                  # the same session is reused
            return all_tools, some, res, again is m.sessions.get("okf")
        finally:
            await m.close()
    all_tools, some, res, reused = run(go())
    names = {t.name for t in all_tools}
    assert {"mcp__okf__okf_search", "mcp__okf__okf_get", "mcp__okf__okf_add"} <= names
    risk = {t.name: t.risk for t in all_tools}
    assert risk["mcp__okf__okf_search"] == "R0" and risk["mcp__okf__okf_add"] == "R3"
    assert [t.name for t in some] == ["mcp__okf__okf_search"]
    assert isinstance(res, str) and res and not res.startswith("ERROR") and reused


def test_mcp_unknown_tools_default_to_r3_and_bad_server_is_contained(tmp_path):
    async def go():
        m = MCPManager({"broken": {"command": "definitely-not-a-real-command-xyz"}}, {}, connect_timeout=10)
        try:
            with pytest.raises(Exception):
                await m.tools("broken")
            with pytest.raises(KeyError):
                await m.tools("missing")
        finally:
            await m.close()
        return m.risk_for("x", "y")
    assert run(go()) == "R3"


def test_bot_gets_mcp_tools_from_its_profile(tmp_path):
    servers = _omni_okf()

    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            e["runner"].mcp = MCPManager(servers, {"okf.okf_search": "R0"})
            bot = await e["reg"].create("Librarian", "researcher", chain=["work/m"], tools=["read_file", "mcp:okf.okf_search"])
            mock.script("work", sse("", tool_calls=[call("mcp__okf__okf_search", query="omni")]), sse("found it"))
            out = await e["runner"].run(bot.id, "search the knowledge base for omni")
            reqs = [r for r in mock.requests if r["provider"] == "work"]
            await e["runner"].mcp.close()
            await e["db"].close()
            return out, reqs
    out, reqs = run(go())
    offered = [t["function"]["name"] for t in reqs[0]["body"]["tools"]]
    assert "mcp__okf__okf_search" in offered and "mcp__okf__okf_add" not in offered
    result = [m for m in reqs[1]["body"]["messages"] if m.get("role") == "tool"][-1]["content"]
    assert result and not result.startswith("ERROR") and out.status == "completed"


# ── MiniMax media tools (HTTP mocked) ──────────────────────────────────────
class _Prov:
    api_key = "mm-test-key"


def test_minimax_tts_video_and_vision(tmp_path):
    seen: list[httpx.Request] = []
    polls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.path == "/v1/t2a_v2":
            body = json.loads(req.content)
            assert body["voice_setting"]["voice_id"] == "English_Graceful_Lady" and body["audio_setting"]["format"] == "mp3"
            return httpx.Response(200, json={"data": {"audio": b"ID3fake-mp3".hex(), "status": 2},
                                             "extra_info": {"audio_length": 1234}, "base_resp": {"status_code": 0, "status_msg": "success"}})
        if req.url.path == "/v1/video_generation":
            return httpx.Response(200, json={"task_id": "t123", "base_resp": {"status_code": 0}})
        if req.url.path == "/v1/query/video_generation":
            polls["n"] += 1
            st = "Processing" if polls["n"] < 2 else "Success"
            return httpx.Response(200, json={"status": st, "file_id": "f9", "video_width": 1366, "video_height": 768})
        if req.url.path == "/v1/files/retrieve_content":
            assert req.url.params["file_id"] == "f9"
            return httpx.Response(200, content=b"\x00\x00\x00 ftypmp4-fake")
        return httpx.Response(404)

    got: dict = {}

    async def vision(msgs, bot_id, job_id):
        got.update(msgs=msgs, bot=bot_id, job=job_id)
        return "a red circle"

    (tmp_path / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    tools = {t.name: t for t in minimax_tools(lambda: _Prov(), vision_chat=vision, poll_every=0,
                                             client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))}
    c = ctx_for(tmp_path, events=[])

    async def go():
        return (await tools["text_to_speech"].fn({"text": "Hello, I am Omi.", "path": "out/hello.mp3"}, c),
                await tools["text_to_speech"].fn({"text": "x" * 10_000}, c),
                await tools["generate_video"].fn({"prompt": "a robot waving", "path": "clip.mp4"}, c),
                await tools["describe_image"].fn({"path": "pic.png", "question": "what is it?"}, c))
    tts, too_long, video, seen_img = run(go())
    assert tts.startswith("wrote hello.mp3") and (tmp_path / "out" / "hello.mp3").read_bytes() == b"ID3fake-mp3"
    assert too_long.startswith("ERROR")
    assert video.startswith("wrote clip.mp4") and (tmp_path / "clip.mp4").read_bytes().endswith(b"mp4-fake") and polls["n"] == 2
    assert seen_img == "a red circle" and got["bot"] == "bot_t" and got["job"] == "job_t"
    part = got["msgs"][0]["content"][1]
    assert part["type"] == "image_url" and part["image_url"]["url"].startswith("data:image/png;base64,")
    assert all(r.headers["Authorization"] == "Bearer mm-test-key" for r in seen)
    assert tools["generate_video"].risk == "R4" and tools["text_to_speech"].risk == "R1" and tools["describe_image"].risk == "R1"
    with pytest.raises(ValueError, match="outside"):
        run(tools["describe_image"].fn({"path": "../x.png"}, c))


# ── playbooks ──────────────────────────────────────────────────────────────
PB_BODY = """## When to use
Comparing hosting providers or pricing plans for a website.
## Steps
1. One researcher per provider reads the official pricing page (web_fetch) and cites it.
2. A writer builds a comparison table.
## Decision rules
Prefer official pages over blogs.
## Required outputs
comparison.md with cited URLs.
## Approval limits
No purchases."""


def test_playbook_versions_ab_test_and_retirement(tmp_path):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            store = PlaybookStore(e["db"], e["bus"])
            v1 = await store.create("Compare Hosting Providers", PB_BODY)
            hit = await store.choose("compare static website hosting providers and their pricing")
            miss = await store.choose("bake a sourdough bread recipe")
            v2 = await store.create(v1.name, PB_BODY + "\nAlso check bandwidth limits.", variant=True)
            picks, v1_runs = [], 0
            for i in range(TRIALS * 2):                         # alternate until both have their trials
                pb = await store.choose("compare website hosting providers pricing")
                picks.append(pb.version)
                v1_runs += pb.version == 1
                ok = pb.version == 2 or v1_runs == 1                  # v1: 1 of 3 succeed (no fail streak); v2: 3 of 3
                await store.record_run(pb.id, project_id=None, success=ok, tokens=1000 * pb.version, seconds=60)
            after = {p.version: p.status for p in await store.all(include_retired=True)}
            # a version that keeps failing is retired
            v3 = await store.create(v1.name, PB_BODY + "\nv3")
            for _ in range(FAIL_STREAK):
                await store.record_run(v3.id, project_id=None, success=False)
            final = {p.version: p.status for p in await store.all(include_retired=True)}
            events = [m.payload for m in await query(e["db"], types=["PLAYBOOK_UPDATED"])]
            await e["db"].close()
            return v1, hit, miss, v2, picks, after, final, events
    v1, hit, miss, v2, picks, after, final, events = run(go())
    assert v1.name == "compare-hosting-providers" and v1.version == 1 and v1.status == "active"
    assert hit and hit.id == v1.id and miss is None
    assert v2.status == "variant" and v2.parent_id == v1.id
    assert sorted(picks) == [1, 1, 1, 2, 2, 2]                    # alternated, each got TRIALS runs
    assert after == {1: "retired", 2: "active"}                   # the winner (100% success) is kept
    assert final == {1: "retired", 2: "retired", 3: "retired"}     # v3 replaced v2, then failed 3 times in a row
    changes = [p["change"] for p in events]
    assert "created" in changes and "new variant (A/B test)" in changes
    assert any(c.startswith("won the A/B test") for c in changes) and any(c.startswith("retired after") for c in changes)


def test_planner_looks_up_the_playbook_first(tmp_path):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            store = PlaybookStore(e["db"], e["bus"])
            await store.create("compare-hosting-providers", PB_BODY)
            mock.script("plan", sse(plan_json(sub("research netlify"), sub("write comparison", [1]))))
            pid = await e["projects"].create("compare website hosting providers")
            ctx = GoalContext(pid, "Compare website hosting providers for my portfolio", e["projects"].folder(pid))
            inbox = await Inbox.open(e["bus"], BOSS_ID)
            kit = BossToolkit(ctx=ctx, db=e["db"], bus=e["bus"], inbox=inbox, registry=e["reg"], runner=e["runner"], graph=e["graph"],
                              projects=e["projects"], ledger=e["ledger"], factory=e["factory"], router=e["router"],
                              planner_chain=["plan/m"], playbooks=store)
            out = await kit.plan_goal({}, ToolContext(bot_id=BOSS_ID, workspace=ctx.folder))
            inbox.close()
            req = [r for r in mock.requests if r["provider"] == "plan"][0]
            await e["db"].close()
            return out, req, ctx
    out, req, ctx = run(go())
    assert out.startswith("Following playbook compare-hosting-providers v1")
    user = req["body"]["messages"][-1]["content"]
    assert "PLAYBOOK" in user and "One researcher per provider" in user
    assert ctx.playbook is not None and ctx.playbook.name == "compare-hosting-providers"


def test_retrospective_learns_and_makes_or_improves_playbooks(tmp_path):
    retro = json.dumps({"lessons": ["Give each researcher one provider", "Check the date in reports"],
                        "playbook": {"name": "Compare Hosting", "body": PB_BODY}})

    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            store = PlaybookStore(e["db"], e["bus"])
            boss = await e["reg"].get(BOSS_ID)
            mock.script("plan", sse(retro),
                        sse(json.dumps({"lessons": ["Repeat: one provider per researcher"], "playbook": {"name": "x", "body": "ignored"}})),
                        sse(json.dumps({"lessons": [], "playbook": {"name": "x", "body": PB_BODY + "\nfixed"}})))
            kw = dict(router=e["router"], chain=["plan/m"], store=store, boss_memory=boss.memory, goal="compare hosts",
                      report="# Report\nall good", project_id=None, tokens=5000, seconds=300)
            first = await retrospective(success=True, playbook=None, **kw)             # new playbook
            pb = first["playbook"]
            worked = await retrospective(success=True, playbook=pb, **kw)              # recipe worked: lessons, no new version
            failed = await retrospective(success=False, playbook=pb, **kw)             # improved variant
            lessons = boss.memory.section("Lessons Learned")
            calls = len([r for r in mock.requests if r["provider"] == "plan"])
            versions = {p.version: p.status for p in await store.all(include_retired=True)}
            stats = await store.get(pb.id)
            await e["db"].close()
            return first, worked, failed, lessons, calls, versions, stats
    first, worked, failed, lessons, calls, versions, stats = run(go())
    assert first["lessons"] == ["Give each researcher one provider", "Check the date in reports"]
    assert any("one provider" in l for l in lessons)
    assert first["playbook"].name == "compare-hosting" and first["playbook"].status == "active"
    assert worked["recorded"] and worked["playbook"] is None and calls == 3          # A15.e.02: lessons after every goal
    assert failed["playbook"].status == "variant" and failed["playbook"].parent_id == first["playbook"].id
    assert versions == {1: "active", 2: "variant"} and stats.runs == 2 and stats.successes == 1


# ── web_search through the private search gateway (A8.b.05) ───────────────
def test_web_search_uses_the_gateway_then_falls_back(tmp_path, monkeypatch):
    import omnibots.runtime.web_tools as wt
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.host == "gw.test":
            if req.url.params["q"] == "busy":
                return httpx.Response(429, json={"detail": "rate limit"})
            if req.url.params["q"] == "down":
                return httpx.Response(502, json={"detail": "backend"})
            return httpx.Response(200, json={"query": req.url.params["q"], "answers": [], "results": [
                {"title": "Stack Overflow: gather", "url": "https://stackoverflow.com/q/1", "snippet": "use return_exceptions", "engines": ["stackoverflow"]}]})
        if req.url.host == "api.duckduckgo.com":
            return httpx.Response(200, json={"Heading": "X", "AbstractText": "instant answer text", "RelatedTopics": []})
        return httpx.Response(404)

    monkeypatch.setattr(wt, "SEARCH_URL", "https://gw.test")
    monkeypatch.setattr(wt, "_search_key", "sg_test_key")
    monkeypatch.setattr(wt, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    tool = {t.name: t for t in wt.web_tools()}["web_search"]
    c = ctx_for(tmp_path)

    async def go():
        return [await tool.fn(a, c) for a in ({"query": "asyncio gather", "category": "it"}, {"query": "down"}, {"query": "busy"})]
    ok, down, busy = run(go())
    assert "stackoverflow.com/q/1" in ok and ok.startswith("[UNTRUSTED")
    first = seen[0]
    assert first.headers["Authorization"] == "Bearer sg_test_key" and first.url.params["category"] == "it"
    assert "instant answer text" in down                         # gateway failed -> DDG instant answer
    assert "instant answer text" in busy                         # rate limited -> fallback too
    monkeypatch.setattr(wt, "_search_key", "")                   # no key configured: straight to the fallback
    seen.clear()
    assert "instant answer text" in run(tool.fn({"query": "x"}, c)) and all(r.url.host != "gw.test" for r in seen)
