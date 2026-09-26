"""A10.a: the browser tools. Real Chromium (Playwright) against a local test site and,
when online, a real public page. The bot drives it through the mock model."""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from mock_provider import MockProviders, sse
from test_a7_orchestrator import build, call
from omnibots.bots.profile import DEFAULT_TOOLS
from omnibots.runtime.browser import BrowserSession, browser_tools
from omnibots.runtime.tools import ToolContext

try:
    from playwright.async_api import async_playwright  # noqa: F401
    HAVE_PW = True
except ImportError:
    HAVE_PW = False

pytestmark = pytest.mark.skipif(not HAVE_PW, reason="playwright not installed")

PAGE = """<!doctype html><html><head><title>Demo Shop</title></head><body>
<h1>Widget Store</h1>
<p>A blue widget for your desk.</p>
<form action="/submit" method="get">
  <input name="q" placeholder="search products" />
  <input name="pw" type="password" placeholder="password" />
  <button type="submit">Search</button>
</form>
<a href="/cart">View cart (0)</a>
</body></html>"""


class Site:
    def __init__(self):
        outer = self
        self.hits = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.hits.append(self.path)
                body = (f"<html><body><h1>Results</h1><p>You searched. Path: {self.path}</p></body></html>"
                        if self.path.startswith("/submit") else PAGE).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}/"

    def close(self):
        self.srv.shutdown()


@pytest.fixture
def site():
    s = Site()
    yield s
    s.close()


def run(coro):
    return asyncio.run(coro)


def test_browser_navigate_read_and_type_on_a_local_site(tmp_path, site):
    async def go():
        sess = BrowserSession(tmp_path / "profile", headless=True)
        tools = {t.name: t for t in browser_tools(lambda ctx: sess)}
        ctx = ToolContext(bot_id="b", workspace=tmp_path)
        try:
            nav = await tools["browser_navigate"].fn({"url": site.url, "allow_internal": True}, ctx)
            # local host is refused without allow_internal
            refused = await tools["browser_navigate"].fn({"url": site.url}, ctx)
            typed = await tools["browser_type"].fn({"index": _find(nav, "input"), "text": "blue widget", "submit": True}, ctx)
            shot = await tools["browser_screenshot"].fn({}, ctx)
            return nav, refused, typed, shot
        finally:
            await sess.close()

    def _find(page_text, kind):
        for line in page_text.splitlines():
            if line.startswith("[") and kind in line:
                return int(line[1:line.index("]")])
        raise AssertionError(f"no {kind} element in {page_text!r}")

    nav, refused, typed, shot = run(go())
    assert nav.startswith("[UNTRUSTED WEB CONTENT") and "Demo Shop" in nav and "Widget Store" in nav
    assert "INTERACTIVE ELEMENTS" in nav and "input" in nav
    assert refused.startswith("ERROR") and "localhost" not in refused.lower() or "refus" in refused.lower()
    assert "Results" in typed and "q=blue" in typed and any("/submit" in h and "q=blue+widget" in h for h in site.hits)
    assert shot.endswith("png)") or ".png" in shot
    assert (tmp_path / shot.split()[0]).exists()


def test_browser_fill_secret_uses_the_vault_and_redacts(tmp_path, site):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            from omnibots.security.vault import Vault
            v = Vault(e["db"], service="omnibots-vault-test-a10")
            await v.set("site_pw", "hunter2-SECRET-pw", hosts=["127.0.0.1"])
            e["runner"].vault = v
            bot = await e["reg"].create("Filler", "web", chain=["work/m"], tools=[*DEFAULT_TOOLS, "browser_navigate", "browser_read", "browser_fill_secret"])
            mock.script("work",
                        sse("", tool_calls=[call("browser_navigate", url=site.url, allow_internal=True)]),
                        sse("", tool_calls=[call("browser_fill_secret", index=1, text="{{secret:site_pw}}")]),
                        sse("done: I filled the password field"))
            approved = []

            async def approver():
                while True:
                    for info in e["approvals"].list_pending():
                        approved.append(dict(info))
                        await e["approvals"].decide(info["id"], True, "test")
                    await asyncio.sleep(0.03)
            t = asyncio.create_task(approver())
            out = await e["runner"].run(bot.id, "fill the password field with the stored secret")
            t.cancel()
            await e["runner"].close_browsers()
            model_saw = json.dumps([r["body"]["messages"] for r in mock.requests if r["provider"] == "work"])
            try:
                import keyring
                keyring.delete_password("omnibots-vault-test-a10", "site_pw")
            except Exception:
                pass
            await e["db"].close()
            return out, approved, model_saw
    out, approved, model_saw = run(go())
    assert out.status == "completed"
    assert approved and approved[0]["risk"] == "R3" and "stored secret" in approved[0]["summary"]
    assert "hunter2-SECRET-pw" not in model_saw                 # the value never reached the model
    assert "filled [1] with the stored credential" in model_saw


@pytest.mark.skipif(not HAVE_PW, reason="playwright")
def test_browser_on_a_real_public_page(tmp_path):
    import socket
    try:
        socket.create_connection(("example.com", 443), timeout=5).close()
    except OSError:
        pytest.skip("offline")

    async def go():
        sess = BrowserSession(tmp_path / "p", headless=True)
        tools = {t.name: t for t in browser_tools(lambda ctx: sess)}
        ctx = ToolContext(bot_id="b", workspace=tmp_path)
        try:
            nav = await tools["browser_navigate"].fn({"url": "https://example.com"}, ctx)
            blocked = await tools["browser_navigate"].fn({"url": "http://169.254.169.254/latest/meta-data/"}, ctx)
            return nav, blocked
        finally:
            await sess.close()
    nav, blocked = run(go())
    assert "Example Domain" in nav and nav.startswith("[UNTRUSTED")
    assert blocked.startswith("ERROR")                          # SSRF: link-local metadata endpoint refused
