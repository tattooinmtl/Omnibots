"""A9.c.01: the vault. Real Windows Credential Manager (a separate test service, cleaned up),
a real local HTTP server, real bot runs against the mock model."""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from mock_provider import MockProviders, sse
from test_a7_orchestrator import build, call
from omnibots.board.query import query
from omnibots.bots.profile import DEFAULT_TOOLS
from omnibots.security.vault import SecretError, Vault, scrub

SERVICE = "omnibots-vault-test"
SECRET = "sk-live-PLANTED-9f8e7d6c5b4a"
OTHER = "ghp_OTHERsecretVALUE123456"


def run(coro):
    return asyncio.run(coro)


class Echo:
    """A local page that contains the secret, and echoes the Authorization header it got."""

    def __init__(self):
        self.seen: list[str] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.seen.append(self.headers.get("Authorization") or "")
                body = (f"<html><body><p>Welcome. Your key is {SECRET}.</p>"
                        f"<p>You sent: {self.headers.get('Authorization') or 'nothing'}</p></body></html>").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}/page"

    def close(self):
        self.srv.shutdown()


@pytest.fixture
def echo():
    e = Echo()
    yield e
    e.close()


@pytest.fixture
def cleanup_keyring():
    yield
    import keyring
    for name in ("demo_token", "github_token", "planted"):
        try:
            keyring.delete_password(SERVICE, name)
        except Exception:
            pass


def test_vault_set_resolve_scope_and_scrub(tmp_path, cleanup_keyring):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            v = Vault(e["db"], service=SERVICE)
            await v.set("demo_token", SECRET, hosts=["127.0.0.1"], note="test")
            await v.set("github_token", OTHER, hosts=["api.github.com"])
            fresh = Vault(e["db"], service=SERVICE)                  # a restart: load() re-registers for redaction
            n = await fresh.load()
            out = dict(
                n=n,
                resolved=fresh.resolve({"Authorization": "Bearer {{secret:demo_token}}"}, "http://127.0.0.1:9/x"),
                names=[(s.name, s.hosts) for s in fresh.names()],
                scrubbed=scrub(f"a {SECRET} b {OTHER}"),
            )
            for bad, target in [("{{secret:github_token}}", "https://evil.example.com/"), ("{{secret:nope}}", "http://127.0.0.1/")]:
                try:
                    fresh.resolve(bad, target)
                    out[bad] = "RESOLVED"
                except SecretError as exc:
                    out[bad] = str(exc)
            row = await e["db"].read_one("SELECT * FROM secrets_index WHERE name='demo_token'")
            out["row"] = dict(row)
            out["deleted"] = await fresh.delete("demo_token")
            await e["db"].close()
            return out
    o = run(go())
    assert o["n"] == 2 and o["resolved"] == {"Authorization": f"Bearer {SECRET}"}
    assert ("github_token", ["api.github.com"]) in o["names"]
    assert o["scrubbed"] == "a [secret:demo_token] b [secret:github_token]"
    assert "may only be sent to api.github.com" in o["{{secret:github_token}}"]
    assert "no secret named 'nope'" in o["{{secret:nope}}"]
    assert SECRET not in json.dumps(o["row"]) and o["row"]["hosts"] == "127.0.0.1"   # SQL never holds the value
    assert o["deleted"] is True


def test_planted_secret_never_reaches_events_board_or_model(tmp_path, echo, cleanup_keyring):
    """A9.99: a secret value planted in a page (and printed by a command) never shows up in
    bot_events or the board: checked by grepping the raw SQLite files."""
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            v = Vault(e["db"], service=SERVICE)
            await v.set("planted", SECRET, hosts=["127.0.0.1"])
            e["runner"].vault = v
            bot = await e["reg"].create("Reader", "researcher", chain=["work/m"], tools=[*DEFAULT_TOOLS, "web_fetch", "run_shell"])
            (bot.workspace / "leak.txt").write_text(f"token={SECRET}\n", encoding="utf-8")
            mock.script("work",
                        sse("", tool_calls=[call("web_fetch", url=echo.url, allow_internal=True)]),
                        sse("", tool_calls=[call("run_shell", command="type leak.txt")]),
                        sse("", tool_calls=[call("send_message", to="omi", text="I read the page and the file")]),
                        sse("done: the page has a welcome message"))
            out = await e["runner"].run(bot.id, "read the page and the file and report")
            model_saw = json.dumps([r["body"]["messages"] for r in mock.requests if r["provider"] == "work"])
            board = json.dumps([m.payload for m in await query(e["db"], limit=500)])
            await e["db"].close()
            return out, model_saw, board
    out, model_saw, board = run(go())
    assert out.status == "completed"
    assert SECRET not in model_saw and "[secret:planted]" in model_saw       # the model got the redacted form
    assert SECRET not in board
    raw = b"".join(p.read_bytes() for p in (tmp_path / "db").glob("omnibots.sqlite*"))
    assert SECRET.encode() not in raw and SECRET.encode("utf-16-le") not in raw   # grep of the database files
    assert b"[secret:planted]" in raw                                          # it was really there, redacted


def test_secret_handles_need_approval_and_respect_hosts(tmp_path, echo, cleanup_keyring):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            v = Vault(e["db"], service=SERVICE)
            await v.set("demo_token", SECRET, hosts=["127.0.0.1"])
            await v.set("github_token", OTHER, hosts=["api.github.com"])
            e["runner"].vault = v
            bot = await e["reg"].create("Caller", "integrator", chain=["work/m"], tools=[*DEFAULT_TOOLS, "web_fetch"])
            mock.script("work",
                        sse("", tool_calls=[call("web_fetch", url=echo.url, allow_internal=True,
                                                 headers={"Authorization": "Bearer {{secret:demo_token}}"})]),
                        sse("", tool_calls=[call("web_fetch", url=echo.url, allow_internal=True,
                                                 headers={"Authorization": "Bearer {{secret:github_token}}"})]),
                        sse("", tool_calls=[call("write_file", path="note.txt", content="{{secret:demo_token}}")]),
                        sse("done"))
            approvals = []

            async def approver():
                while True:
                    for info in e["approvals"].list_pending():
                        approvals.append(dict(info))
                        await e["approvals"].decide(info["id"], True, "test")
                    await asyncio.sleep(0.05)
            t = asyncio.create_task(approver())
            out = await e["runner"].run(bot.id, "call the API with the stored token")
            t.cancel()
            results = [m["content"] for r in mock.requests if r["provider"] == "work"
                       for m in r["body"]["messages"] if m.get("role") == "tool"]
            note = (bot.workspace / "note.txt").read_text(encoding="utf-8")
            await e["db"].close()
            return out, approvals, results, note
    out, approvals, results, note = run(go())
    assert out.status == "completed"
    # the first call carried the real value to the allowed host, after an R3 approval
    assert echo.seen[0] == f"Bearer {SECRET}"
    assert approvals and approvals[0]["risk"] == "R3" and "with a stored credential" in approvals[0]["summary"]
    assert SECRET not in json.dumps(approvals)                                   # the approval card shows the handle only
    assert "You sent: Bearer [secret:demo_token]" in results[0]                 # the echo came back redacted
    # the second call targeted a host the secret isn't scoped to: refused, never sent
    assert len(echo.seen) == 1 and "may only be sent to api.github.com" in results[-2]
    # a handle in a non-secret argument is just text
    assert note == "{{secret:demo_token}}"
