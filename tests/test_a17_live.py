"""A17 live, on the real providers (skipped unless OMNIBOTS_LIVE=1): a real bot, through the real JobRunner and
MiniMax, uses each new tool the way a user's goal would make it. Billed tools (images, music, voice cloning) run
once each, at the smallest size, and only with OMNIBOTS_LIVE_PAID=1."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from omnibots.board.bus import MessageBus
from omnibots.board.ledger import Ledger
from omnibots.board.locks import LeaseManager
from omnibots.bots.profile import BotRegistry
from omnibots.bots.runner import JobRunner
from omnibots.db import Database
from omnibots.lineup import LINEUP_MODELS, MINIMAX
from omnibots.omni import load_omni_config, locate_omni
from omnibots.providers.quota import QuotaManager
from omnibots.providers.router import Router
from omnibots.providers.seats import SeatScheduler
from omnibots.runtime.approvals import ApprovalCenter
from omnibots.runtime.pools import runner_pools
from omnibots.runtime.sandbox import Sandbox

pytestmark = pytest.mark.skipif(os.environ.get("OMNIBOTS_LIVE") != "1", reason="live provider test; set OMNIBOTS_LIVE=1")
paid = pytest.mark.skipif(os.environ.get("OMNIBOTS_LIVE_PAID") != "1", reason="billed; set OMNIBOTS_LIVE_PAID=1")


def live_bot(tmp: Path, tools: list[str], task: str, *, approve: bool = True, iterations: int = 14):
    """One real job in the project folder tmp/proj; approvals are answered `approve` as they come."""
    proj = tmp / "proj"
    proj.mkdir(exist_ok=True)

    async def go():
        db = Database(tmp / "db" / "omnibots.sqlite")
        await db.open()
        bus = MessageBus(db)
        cfg = load_omni_config(locate_omni())
        router = Router(lambda: cfg, QuotaManager(db), SeatScheduler(boss_id="omi"))
        approvals = ApprovalCenter(db)
        events: list[dict] = []
        pools = runner_pools(lambda: cfg, tmp, router, LeaseManager(db, bus))
        runner = JobRunner(db=db, registry=BotRegistry(db, tmp / "bots", bus), router=router, approvals=approvals, home=tmp,
                           sandbox=Sandbox(tmp / "sandbox"), bus=bus, ledger=Ledger(db, bus), listener=events.append, learn=False, **pools)
        bot = await runner.registry.create("Live", "assistant", chain=[LINEUP_MODELS[MINIMAX]], tools=tools)
        cards: list[dict] = []

        async def answer():
            while True:
                for p in approvals.list_pending():
                    cards.append(p)
                    await approvals.decide(p["id"], approve, "live test")
                await asyncio.sleep(0.2)
        watcher = asyncio.create_task(answer())
        try:
            out = await asyncio.wait_for(runner.run(bot.id, task, workspace=proj, max_iterations=iterations), 900)
        finally:
            watcher.cancel()
            await pools["mcp"].close()
            await db.close()
        console = [e["content"] for e in events if e["kind"] == "console" and not e["stream"]]
        print("\n".join(console))
        return out, console, cards

    out, console, cards = asyncio.run(go())
    answer = out.result.answer if out.result else ""
    print("ANSWER:", answer)
    return out, answer, console, cards, proj


def test_live_bot_reads_a_pdf_and_a_spreadsheet(tmp_path):
    import openpyxl
    from test_a17_documents import _pdf
    proj = tmp_path / "proj"
    proj.mkdir()
    _pdf(proj / "invoice.pdf", ["Invoice 4471 from Boulangerie Soleil", "Total due: 812 dollars, payable by November 3"])
    wb = openpyxl.Workbook()
    for row in (["Flavour", "Sold"], ["Lemon", 41], ["Cherry", 97], ["Vanilla", 12]):
        wb.active.append(row)
    wb.save(proj / "sales.xlsx")
    out, answer, console, _, _ = live_bot(tmp_path, ["read_file", "list_dir"],
                                          "Read invoice.pdf and sales.xlsx. Answer in one line: the invoice total, its due date, "
                                          "and which flavour sold the most.")
    low = answer.lower()
    assert out.status == "completed" and "812" in low and "november 3" in low and "cherry" in low, answer


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_live_bot_watches_a_video(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=red:s=320x240:d=3", "-f", "lavfi", "-i", "color=c=lime:s=320x240:d=3",
                    "-f", "lavfi", "-i", "color=c=blue:s=320x240:d=3",
                    "-filter_complex", "[0][1][2]concat=n=3:v=1:a=0", "-pix_fmt", "yuv420p", "-y", str(proj / "colors.mp4")], check=True)
    out, answer, console, _, _ = live_bot(tmp_path, ["watch_video", "list_dir"],
                                          "Watch colors.mp4 (6 frames) and answer in one line: the colours the screen shows, in order.")
    low = answer.lower()
    assert any("watch_video" in c for c in console)
    assert 0 <= low.find("red") < low.find("green") < low.find("blue"), answer


def test_live_bot_makes_a_slide_deck_and_a_pdf(tmp_path):
    out, answer, console, _, proj = live_bot(tmp_path, ["make_document", "read_file"],
                                             "Make pitch.pptx: a 3-slide deck pitching a neighbourhood bakery (title slide, "
                                             "'Why us' with 3 bullets, 'Prices' with a small table), then menu.pdf with the "
                                             "same prices. Read pitch.pptx back to check it, then answer 'done'.")
    from omnibots.runtime.documents import document_text
    deck = document_text(proj / "pitch.pptx")
    assert "(PowerPoint, 3 slides)" in deck, deck
    assert "(PDF," in document_text(proj / "menu.pdf")


@paid
def test_live_bot_makes_and_edits_a_picture(tmp_path):
    out, answer, console, cards, proj = live_bot(tmp_path, ["generate_image", "edit_image", "describe_image", "list_dir"],
                                                 "Make ONE square picture of a red bicycle in front of a bakery (save it as "
                                                 "bike.png), then edit it so it is at night (save night.png). Look at night.png "
                                                 "and say in one line what you see.")
    assert {c["tool"] for c in cards} >= {"generate_image", "edit_image"}
    assert (proj / "bike.png").is_file() or list(proj.glob("bike*")), list(proj.rglob("*"))
    assert list(proj.glob("night*")), list(proj.rglob("*"))


@paid
def test_live_bot_makes_a_song(tmp_path):
    out, answer, console, cards, proj = live_bot(tmp_path, ["generate_music", "list_dir"],
                                                 "Make a short cheerful song (2 short verses you write) about fresh bread, "
                                                 "save it as bread.mp3, then answer 'done'.")
    assert any(c["tool"] == "generate_music" for c in cards)
    mp3 = proj / "bread.mp3"
    if not mp3.is_file():          # 2026-10-07: MiniMax closed music to the user's account (410 / 2153); the bot must say so
        assert any(w in answer.lower() for w in ("closed", "no longer available", "not available")), answer
        pytest.skip("MiniMax music is closed to this account: " + answer[:200])
    assert mp3.stat().st_size > 50_000


@paid
def test_live_bot_clones_a_voice_and_speaks_with_it(tmp_path):
    out, answer, console, cards, proj = live_bot(tmp_path, ["text_to_speech", "clone_voice", "list_dir"],
                                                 "1) With text_to_speech, read this into sample.mp3: 'Hello, I am the narrator "
                                                 "of the OmniBots demo. Today we bake bread together, step by step, slowly and "
                                                 "carefully, so that every loaf comes out golden and warm. Let us begin.' "
                                                 "2) clone_voice from sample.mp3 named 'Demo narrator' (it is MiniMax's own "
                                                 "synthetic voice, so we may). 3) text_to_speech 'The bread is ready.' with the "
                                                 "new voice_id into ready.mp3. Answer with the voice_id.", iterations=16)
    assert any(c["tool"] == "clone_voice" for c in cards)
    if not (proj / "ready.mp3").is_file() or "Bots_" not in answer:   # the user's Token Plan lacks voice_clone (2061)
        assert "plan" in answer.lower(), answer
        pytest.skip("voice cloning isn't in this MiniMax plan: " + answer[:200])


def test_live_bot_researches_with_real_sources(tmp_path):
    out, answer, console, _, proj = live_bot(tmp_path, ["research", "read_file"],
                                             "Use research to find out how ESP32-CAM boards are usually powered and what "
                                             "goes wrong with them. Then tell me the report's file name.", iterations=8)
    reports = list((proj / "research").glob("*.md"))
    assert reports, list(proj.rglob("*"))
    text = reports[0].read_text(encoding="utf-8")
    assert "## Sources" in text and "](http" in text and "[1]" in text, text[:2000]
    print(text[:3000])


def test_live_omi_keeps_a_journal_and_proposes_goals(tmp_path):
    """The real engine (temp home, the user's real providers): a journal entry from yesterday's real-shaped history,
    then Omi's own ideas on the real cheap lane. Nothing starts by itself."""
    import datetime as dt
    from omnibots.engine import Engine
    eng = Engine(tmp_path / "db" / "omnibots.sqlite", keep_awake=False, home=tmp_path, omni_install_root="")
    eng.start()
    try:
        y = (dt.date.today() - dt.timedelta(days=1)).isoformat()

        async def seed():
            await eng.db.write("INSERT INTO projects (id, goal, status, created_at) VALUES "
                               "('p1', 'Build a one-page site for Boulangerie Soleil with the menu and opening hours', 'completed', ?)",
                               (y + "T09:00:00Z",))
            for jid, title, status in (("j1", "Write index.html with the menu", "completed"),
                                       ("j2", "Deploy the site to Netlify", "failed"),
                                       ("j3", "Check every link on the live site", "completed")):
                await eng.db.write("INSERT INTO jobs (id, project_id, title, status, assigned_bot_id, finished_at, error_message) "
                                   "VALUES (?, 'p1', ?, ?, 'omi', ?, ?)", (jid, title, status, y + "T15:00:00Z",
                                                                            "netlify token missing" if status == "failed" else None))
            did = await eng.initiative.tick(idle=False)
            made = await eng.initiative.propose()
            return did, made, await eng.initiative.pending(), await eng.initiative.journal(), \
                await eng.db.read("SELECT id FROM projects")
        did, made, pending, journal, projects = eng.submit(seed()).result(timeout=300)
    finally:
        eng.stop()
    print("JOURNAL:", journal[0]["entry"] if journal else None)
    print("PROPOSALS:", [(p["title"], p["goal"], p["why"]) for p in pending])
    assert did.get("journal") and "netlify" in journal[0]["entry"].lower(), journal
    assert len(pending) == len(made) <= 2
    assert [p["id"] for p in projects] == ["p1"]                     # nothing started by itself


def test_live_bot_models_and_renders_in_blender(tmp_path):
    """Blender must be running with its MCP add-on server (OmniOne's config: http://127.0.0.1:8765/mcp). The bot gets
    Blender's tools from OmniOne's MCP config over HTTP, renders, and looks at its own render."""
    import socket
    try:
        socket.create_connection(("127.0.0.1", 8765), 1).close()
    except OSError:
        pytest.skip("Blender's MCP server isn't running")
    from omnibots.mcp_client import omnione_servers
    assert "blender" in omnione_servers()
    out, answer, console, cards, proj = live_bot(
        tmp_path, ["mcp:blender", "describe_image", "list_dir"],
        "In Blender: add a red cube named BotCube at the origin, then render the scene to an image you can see, look "
        "at the render with describe_image, and answer in one line what the render shows.", iterations=20)
    joined = "\n".join(console)
    assert "mcp blender." in joined, joined[-3000:]
    renders = list(proj.rglob("*.png")) + list(proj.rglob("*.jpg"))
    print("RENDERS:", renders)
    assert renders, "the render must land in the project (live 2026-10-07 it landed in Blender's own folder)"
    assert any("describe_image" in c and "outside" not in c for c in console)
    assert out.status == "completed" and "cube" in answer.lower(), answer


def test_live_github_reads_the_real_repository(tmp_path):
    """Read-only, with the gh CLI's own login (writes are covered by the stand-in API in test_a17_coding)."""
    import asyncio, shutil, subprocess
    from omnibots.runtime.github_tools import github_tools
    from omnibots.runtime.tools import ToolContext
    if not shutil.which("gh"):
        pytest.skip("gh not installed")
    token = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True).stdout.strip()
    if not token:
        pytest.skip("gh is not logged in")
    tools = {t.name: t for t in github_tools(lambda: token)}
    ctx = ToolContext(bot_id="b", workspace=tmp_path)
    info = asyncio.run(tools["github_repo"].fn({"repo": "tattooinmtl/Omnibots"}, ctx))
    prs = asyncio.run(tools["github_pull_requests"].fn({"repo": "tattooinmtl/Omnibots", "state": "closed", "limit": 5}, ctx))
    one = asyncio.run(tools["github_pull_requests"].fn({"repo": "tattooinmtl/Omnibots", "number": 23}, ctx))
    print(info, prs, one[:600], sep="\n")
    assert "tattooinmtl/Omnibots" in info and "default branch: master" in info
    assert "#23" in prs and "[closed]" in prs
    assert "TattooAI/repo-cleanup → master" in one and "files:" in one
