"""A15.c honest "done": a cited page is opened and must hold the quote; a cited test runs again when
Omi accepts, and one that fails now is a rejection with the real output; a checked live page becomes
the project's live address. Real database, sandbox and a local web server."""

from __future__ import annotations

import asyncio
import functools
import http.server
import socket
import threading

import pytest

from mock_provider import MockProviders
from test_a7_orchestrator import build

from omnibots.board.a2a import Inbox
from omnibots.board.ledger import EvidenceError
from omnibots.bots.profile import BOSS_ID
from omnibots.orchestrator.boss_tools import BossToolkit, GoalContext
from omnibots.runtime.tools import ToolContext


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    root = tmp_path_factory.mktemp("site")
    (root / "shop.html").write_text("<html><body><h1>Bakery</h1><p>Price: $5 per box</p></body></html>", encoding="utf-8")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_a_cited_page_is_opened_and_must_hold_the_quote(tmp_path, site):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            ok = await e["ledger"].submit(bot_id="b1", text="the box costs $5",
                                          evidence=[{"kind": "url_quote", "ref": f"{site}/shop.html", "quote": "price:  $5 PER box"}])
            errors = []
            for ref, quote in ((f"{site}/shop.html", "Price: $9 per box"), (f"http://127.0.0.1:{closed_port()}/x", "anything")):
                try:
                    await e["ledger"].submit(bot_id="b1", text="lie", evidence=[{"kind": "url_quote", "ref": ref, "quote": quote}])
                    errors.append(None)
                except EvidenceError as exc:
                    errors.append(str(exc))
            (claim,) = await e["ledger"].claims(bot_id="b1")
            await e["db"].close()
            return ok, claim, errors
    ok, claim, (made_up, unreachable) = run(go())
    assert claim["id"] == ok and claim["evidence"][0]["detail"]["verified"] is True       # whitespace and case don't matter
    assert "the quote isn't on" in made_up and "Bakery" in made_up                         # says what the page really starts with
    assert "couldn't open" in unreachable


def test_accept_reruns_the_cited_test_and_a_failing_one_is_rejected_with_its_output(tmp_path, site):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            pid = await e["projects"].create("a site")
            folder = e["projects"].folder(pid)
            ctx = GoalContext(pid, "a site", folder)
            inbox = await Inbox.open(e["bus"], BOSS_ID)
            kit = BossToolkit(ctx=ctx, db=e["db"], bus=e["bus"], inbox=inbox, registry=e["reg"], runner=e["runner"],
                              graph=e["graph"], projects=e["projects"], ledger=e["ledger"], factory=e["factory"], router=e["router"])
            bot = await e["reg"].create("Builder", "coder", chain=["work/m"])
            tctx = ToolContext(bot_id=BOSS_ID, workspace=folder)

            async def claim(test_body: str, *, live: bool = False) -> int:
                job = await e["graph"].add_job(pid, f"job {test_body[:10]}", assigned_bot_id=bot.id, budget={"max_attempts": 1})
                await e["db"].write("UPDATE jobs SET attempts=1 WHERE id=?", (job.id,))   # a failure escalates, no model restart
                (folder / "test_site.py").write_text(test_body, encoding="utf-8")
                ran = [{"command": "python test_site.py", "exit_code": 0, "output": "ok", "timed_out": False}]
                ev = [{"kind": "test", "ref": "python test_site.py", "exit_code": 0}]
                if live:
                    ev.append({"kind": "url_quote", "ref": f"{site}/shop.html", "quote": "Price: $5", "live": True})
                return await e["ledger"].submit(bot_id=bot.id, text="done", evidence=ev, job_id=job.id, project_id=pid,
                                                workspace=folder, runs=ran)

            good = await claim("print('ok')\n", live=True)
            accepted = await kit.accept_claim({"claim_id": good}, tctx)
            bad = await claim("import sys\nprint('header is missing')\nsys.exit(1)\n")
            refused = await kit.accept_claim({"claim_id": bad}, tctx)
            status = {c["id"]: c["status"] for c in await e["ledger"].claims(project_id=pid)}
            reason = (await e["db"].read_one("SELECT reason FROM claims WHERE id=?", (bad,)))["reason"]
            live = await e["db"].read_one("SELECT live_url, live_checked_at FROM projects WHERE id=?", (pid,))
            inbox.close()
            await e["db"].close()
            return good, bad, accepted, refused, status, reason, dict(live)
    good, bad, accepted, refused, status, reason, live = run(go())
    assert "accepted" in accepted and "passes again (exit 0)" in accepted and "Live at" in accepted
    assert refused.startswith("NOT accepted") and "exit 1" in refused and "header is missing" in refused
    assert status == {good: "accepted", bad: "rejected"} and "header is missing" in reason
    assert live["live_url"].endswith("/shop.html") and live["live_checked_at"]
