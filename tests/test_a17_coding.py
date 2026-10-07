"""A17.f (0.9.0): the live preview, MCP over HTTP (a real MCP server over streamable HTTP, in this process), OmniOne's
MCP servers and Blender skills read-only, and the GitHub tools (a stand-in API; the live read is in test_a17_live)."""

from __future__ import annotations

import asyncio
import base64
import json
import socket
import threading
import time

import httpx
import pytest
from qt_helpers import qapp

from omnibots.runtime.tools import ToolContext

try:
    from mcp.server.mcpserver import Image, MCPServer                  # mcp 2.x (the render tool's annotation needs it here)
except ImportError:                                                    # pragma: no cover
    Image = MCPServer = None

app = qapp()


def _wait(cond, seconds=8.0):
    from PySide6.QtCore import QCoreApplication
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_html_opens_split_with_a_live_preview_that_follows_typing_saving_and_bots(tmp_path):
    from omnibots.ui.files import EditorTab
    page = tmp_path / "index.html"
    page.write_text("<h1>Bakery</h1>", encoding="utf-8")
    tab = EditorTab(page)
    pv = tab.preview
    assert pv is not None and pv.renders == 1                       # opened: shown at once
    tab.text.setPlainText("<h1>Bakery</h1><p>fresh bread</p>")
    assert pv.renders == 1                                           # typing waits for a pause…
    assert _wait(lambda: pv.renders == 2)                            # …then refreshes
    page.write_text("<h1>Changed by a bot</h1>", encoding="utf-8")
    tab.load()                                                       # what EditorTabs does when the file changes on disk
    assert pv.renders == 3
    tab.save()
    assert pv.renders == 4
    import sys
    if pv.view is not None and ".venv" in sys.executable:             # the engine's sandbox starts under the app's venv
        # (with a system Python whose Qt lives in the user's site-packages Chromium's sandbox can't start; the
        # installed app always runs from its venv: run this file with ~/.omnibots/.venv to see the page render)
        got = {}
        loaded = []
        pv.view.loadFinished.connect(lambda ok: loaded.append(ok))
        pv.show_text("<h1>Seen in the browser</h1>", now=True)
        assert _wait(lambda: bool(loaded), 20), "the page never finished loading"
        pv.view.page().toPlainText(lambda t: got.setdefault("t", t))
        assert _wait(lambda: "t" in got, 10)
        assert "Seen in the browser" in got["t"]
    py = tmp_path / "x.py"
    py.write_text("print(1)\n")
    assert EditorTab(py).preview is None                             # code files stay plain


def test_markdown_and_svg_previews():
    from pathlib import Path
    from omnibots.ui.preview import html_for
    assert "<h1>Title</h1>" in html_for(Path("a.md"), "# Title\n\n- one")
    assert html_for(Path("a.svg"), "<svg/>").count("<svg/>") == 1 and "<body" in html_for(Path("a.svg"), "<svg/>")


# ── MCP over HTTP: a real server ───────────────────────────────────────────
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def http_mcp():
    port = _free_port()
    srv = MCPServer("scene")

    @srv.tool()
    def scene_info() -> str:
        """What is in the scene."""
        return "Cube, Camera, Light"

    @srv.tool()
    def add_cube(name: str) -> str:
        """Add a cube."""
        return f"added {name}"

    @srv.tool()
    def render() -> Image:
        """Render the scene."""
        return Image(data=PNG, format="png")

    t = threading.Thread(target=lambda: srv.run(transport="streamable-http", host="127.0.0.1", port=port), daemon=True)
    t.start()
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    yield f"http://127.0.0.1:{port}/mcp"


def test_an_http_mcp_server_works_like_a_local_one(tmp_path, http_mcp):
    from omnibots.mcp_client import MCPManager

    async def go():
        m = MCPManager({"scene": {"url": http_mcp, "readOnlyTools": ["scene_info"]}})
        try:
            tools = {t.name: t for t in await m.tools("scene")}
            ctx = ToolContext(bot_id="b", workspace=tmp_path)
            info = await tools["mcp__scene__scene_info"].fn({}, ctx)
            added = await tools["mcp__scene__add_cube"].fn({"name": "Box"}, ctx)
            shot = await tools["mcp__scene__render"].fn({}, ctx)
        finally:
            await m.close()
        return tools, info, added, shot
    tools, info, added, shot = asyncio.run(go())
    assert tools["mcp__scene__scene_info"].risk == "R0"              # readOnlyTools (OmniOne's format)
    assert tools["mcp__scene__add_cube"].risk == "R3"                # anything else asks, as before
    assert info == "Cube, Camera, Light" and added == "added Box"
    saved = list((tmp_path / "mcp").glob("scene-render-*.png"))
    assert saved and saved[0].read_bytes() == PNG and "describe_image" in shot


def test_own_and_omnione_servers_and_blender_skills(tmp_path, monkeypatch):
    from omnibots import mcp_client
    from omnibots.runtime import pools
    (tmp_path / "mcp.json").write_text(json.dumps({"mcpServers": {"mine": {"url": "http://127.0.0.1:1/mcp"}}}))
    one = tmp_path / "one.json"
    one.write_text(json.dumps({"mcpServers": {"blender": {"url": "http://127.0.0.1:8765/mcp", "readOnlyTools": ["scene_info"]}}}))
    monkeypatch.setattr(mcp_client, "OMNIONE_MCP", str(one))
    assert set(mcp_client.own_servers(tmp_path)) == {"mine"}
    assert mcp_client.omnione_servers()["blender"]["url"].endswith("/mcp")
    skills = tmp_path / "home" / ".omnione" / "app" / "skills" / "blender-hd-render"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("---\nname: blender-hd-render\n---\n")
    monkeypatch.setattr(pools.Path, "home", lambda: tmp_path / "home")
    assert [p.name for p in pools.omnione_blender_skills()] == ["blender-hd-render"]


# ── GitHub ─────────────────────────────────────────────────────────────────
class GH:
    def __init__(self):
        self.calls = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else None
        self.calls.append((req.method, req.url.path, body, req.headers.get("authorization")))
        p = req.url.path
        if p == "/user/repos":
            return httpx.Response(200, json=[{"full_name": "me/site", "private": True, "stargazers_count": 2, "open_issues_count": 1,
                                              "description": "bakery"}])
        if p == "/repos/me/site" and req.method == "GET":
            return httpx.Response(200, json={"full_name": "me/site", "default_branch": "main", "private": True, "html_url": "https://github.com/me/site"})
        if p == "/repos/me/site/issues" and req.method == "GET":
            return httpx.Response(200, json=[{"number": 3, "title": "Menu typo", "state": "open", "user": {"login": "me"}, "comments": 1},
                                             {"number": 4, "title": "PR", "state": "open", "user": {"login": "bot"}, "pull_request": {}}])
        if p == "/repos/me/site/issues" and req.method == "POST":
            return httpx.Response(201, json={"number": 5, "html_url": "https://github.com/me/site/issues/5"})
        if p == "/repos/me/site/pulls" and req.method == "POST":
            return httpx.Response(201, json={"number": 6, "html_url": "https://github.com/me/site/pull/6"})
        if p == "/repos/me/nope":
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(404, json={"message": "?"})


def test_github_reads_and_publishes_only_with_a_click(tmp_path):
    from omnibots.runtime.github_tools import github_tools
    gh = GH()
    tools = {t.name: t for t in github_tools(lambda: "ghp_test", client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(gh.handler)))}
    ctx = ToolContext(bot_id="b", workspace=tmp_path)
    run = lambda name, args: asyncio.run(tools[name].fn(args, ctx))
    assert "me/site (private)" in run("github_repos", {}) and gh.calls[0][3] == "Bearer ghp_test"
    issues = run("github_issues", {"repo": "https://github.com/me/site.git"})
    assert "#3 Menu typo" in issues and "#4 PR PR [open]" in issues and "UNTRUSTED" in issues
    assert run("github_pull_request", {"repo": "me/site", "head": "fix-menu", "title": "Fix the menu"}).startswith("opened pull request #6 (fix-menu → main)")
    assert gh.calls[-1][2]["base"] == "main"
    assert "issues/5" in run("github_create_issue", {"repo": "me/site", "title": "Add hours"})
    assert run("github_repo", {"repo": "me/nope"}) == "ERROR: GitHub HTTP 404: Not Found"
    assert run("github_repo", {"repo": "not a repo"}) == "ERROR: repo must look like owner/name"
    for name in ("github_create_issue", "github_comment", "github_pull_request"):
        assert tools[name].always_ask and tools[name].risk == "R3"   # publishing as the user: always a click
    for name in ("github_repos", "github_repo", "github_issues", "github_pull_requests"):
        assert not tools[name].always_ask and tools[name].risk == "R2"
    none = {t.name: t for t in github_tools(lambda: None)}
    assert "no GitHub token" in asyncio.run(none["github_repos"].fn({}, ctx))


def test_relative_paths_for_mcp_servers_land_in_the_project(tmp_path):
    from omnibots.mcp_client import project_paths
    out = project_paths({"output_path": "renders/cube.png", "filepath": "C:/abs/x.blend", "url": "http://a/b",
                         "name": "cube.png", "dir": "../escape"}, tmp_path)
    assert out["output_path"] == str((tmp_path / "renders" / "cube.png").resolve())
    assert out["filepath"] == "C:/abs/x.blend" and out["url"] == "http://a/b" and out["name"] == "cube.png"
    assert out["dir"] == str((tmp_path / "../escape").resolve())       # made absolute; leaving the project still asks


def test_an_mcp_call_counts_as_a_real_run_for_claims(tmp_path, http_mcp):
    from omnibots.board.ledger import match_run
    from omnibots.mcp_client import MCPManager

    async def go():
        m = MCPManager({"scene": {"url": http_mcp}})
        try:
            tools = {t.name: t for t in await m.tools("scene")}
            ctx = ToolContext(bot_id="b", workspace=tmp_path)
            await tools["mcp__scene__add_cube"].fn({"name": "Box"}, ctx)
        finally:
            await m.close()
        return ctx.runs
    runs = asyncio.run(go())
    assert match_run("mcp__scene__add_cube", runs)["exit_code"] == 0
