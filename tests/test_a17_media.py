"""A17.b: generate_image, edit_image, generate_music, clone_voice (against a local stand-in for the MiniMax and OpenAI
APIs that records each request) and make_document (real files, read back with read_file's document reader).
The real APIs are tests/test_a17_live.py (OMNIBOTS_LIVE=1 OMNIBOTS_LIVE_PAID=1)."""

from __future__ import annotations

import asyncio
import base64
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnibots.runtime import create_tools as ct
from omnibots.runtime.create_tools import create_tools, make_voice_id
from omnibots.runtime.documents import document_text
from omnibots.runtime.make_docs import make_document
from omnibots.runtime.tools import ToolContext

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


@dataclass
class Prov:
    api_key: str
    base_url: str = "https://api.openai.com/v1"


class Fake:
    """httpx MockTransport standing in for api.minimax.io and api.openai.com."""

    def __init__(self):
        self.calls: list[tuple[str, Any]] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.headers.get("content-type", "").startswith("application/json"):
            body = json.loads(req.content)
        else:
            body = req.content
        self.calls.append((f"{req.url.host}{path}", body))
        ok = {"base_resp": {"status_code": 0, "status_msg": "success"}}
        if path == "/v1/image_generation":
            return httpx.Response(200, json={**ok, "data": {"image_base64": [base64.b64encode(PNG).decode()] * body["n"]}})
        if path == "/v1/music_generation":
            return httpx.Response(200, json={**ok, "data": {"audio": b"ID3fakemp3".hex(), "status": 2},
                                             "extra_info": {"music_duration": 31000}})
        if path == "/v1/files/upload":
            return httpx.Response(200, json={**ok, "file": {"file_id": 987}})
        if path == "/v1/voice_clone":
            return httpx.Response(200, json={**ok, "demo_audio": "https://cdn.example/demo.mp3"})
        if path == "/demo.mp3":
            return httpx.Response(200, content=b"ID3demo")
        if path == "/v1/images/edits":
            return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG).decode()}]})
        return httpx.Response(404)



@pytest.fixture
def fake():
    return Fake()


def tools(fake, openai=None, home=None):
    factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    return {t.name: t for t in create_tools(lambda: Prov("mm-key"), (lambda: openai), home, client_factory=factory)}


def run(tool, args, ws):
    return asyncio.run(tool.fn(args, ToolContext(bot_id="b", workspace=ws)))


def test_generate_image_asks_with_a_price_and_keeps_a_reference_face(fake, tmp_path):
    (tmp_path / "me.png").write_bytes(PNG)
    t = tools(fake)["generate_image"]
    assert t.risk == "R4" and t.cost({"n": 3}) == pytest.approx(0.0105)
    out = run(t, {"prompt": "a bakery logo", "n": 2, "ratio": "16:9", "reference_image": "me.png", "path": "media/logo.png"}, tmp_path)
    url, body = fake.calls[0]
    assert url == "api.minimax.io/v1/image_generation"
    assert body["model"] == "image-01" and body["n"] == 2 and body["aspect_ratio"] == "16:9"
    assert body["subject_reference"][0]["image_file"].startswith("data:image/png;base64,")
    assert (tmp_path / "media/logo.png").read_bytes() == PNG and (tmp_path / "media/logo-2.png").is_file()
    assert "media/logo.png, media/logo-2.png" in out
    assert "ratio must be" in run(t, {"prompt": "x", "ratio": "5:1"}, tmp_path)


def test_edit_image_uses_openai_when_there_is_a_key_else_says_it_redrew(fake, tmp_path):
    (tmp_path / "shop.png").write_bytes(PNG)
    with_key = tools(fake, openai=Prov("sk-test"))["edit_image"]
    out = run(with_key, {"image": "shop.png", "prompt": "make it night"}, tmp_path)
    assert fake.calls[-1][0] == "api.openai.com/v1/images/edits" and b"gpt-image-1" in fake.calls[-1][1]
    assert "OpenAI gpt-image-1" in out and with_key.cost({}) == 0.19
    no_key = tools(fake)["edit_image"]
    out = run(no_key, {"image": "shop.png", "prompt": "make it night"}, tmp_path)
    assert fake.calls[-1][0] == "api.minimax.io/v1/image_generation" and "subject_reference" in fake.calls[-1][1]
    assert "NEW picture that keeps the subject" in out and no_key.cost({}) == 0.0035


def test_generate_music_lyrics_instrumental_or_written_for_you(fake, tmp_path):
    t = tools(fake)["generate_music"]
    out = run(t, {"prompt": "upbeat bakery jingle", "lyrics": "[Verse]\nfresh bread", "path": "jingle.mp3"}, tmp_path)
    body = fake.calls[-1][1]
    assert body["model"] == "music-3.0" and body["lyrics"].startswith("[Verse]") and body["output_format"] == "hex"
    assert (tmp_path / "jingle.mp3").read_bytes() == b"ID3fakemp3" and "31 s" in out
    run(t, {"prompt": "calm piano", "instrumental": True}, tmp_path)
    assert fake.calls[-1][1]["is_instrumental"] is True and "lyrics" not in fake.calls[-1][1]
    run(t, {"prompt": "a sea shanty about bots"}, tmp_path)
    assert fake.calls[-1][1]["lyrics_optimizer"] is True
    assert t.cost({}) == 0.15 and t.cost({"model": "music-3.0-free"}) == 0.0


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_clone_voice_converts_uploads_clones_and_remembers(fake, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=f=220:d=11", "-y", str(ws / "me.ogg")], check=True)
    t = tools(fake, home=home)
    assert t["clone_voice"].cost({}) == 1.5 and t["clone_voice"].risk == "R4"
    out = run(t["clone_voice"], {"sample": "me.ogg", "name": "Érik narrator", "preview_text": "Hello there"}, ws)
    paths = [c[0] for c in fake.calls]
    assert paths[:2] == ["api.minimax.io/v1/files/upload", "api.minimax.io/v1/voice_clone"]
    assert b'name="purpose"' in fake.calls[0][1] and b"voice_clone" in fake.calls[0][1] and b"RIFF" in fake.calls[0][1]  # wav
    clone = fake.calls[1][1]
    assert clone["file_id"] == 987 and clone["voice_id"].startswith("Bots_Erik_narrator_") and clone["text"] == "Hello there"
    assert "voice_id Bots_Erik_narrator_" in out and "preview is in media/voice-preview-" in out
    assert clone["voice_id"] in run(t["list_voices"], {}, ws)
    assert make_voice_id("123 !!") .startswith("Bots_Voice_")


def test_make_document_writes_real_office_files_and_pdfs(tmp_path):
    ctx = ToolContext(bot_id="b", workspace=tmp_path)
    docx_md = "# Bakery plan\n\nOpen on **Friday**.\n\n- buy flour\n  - organic\n1. bake\n\n| Item | Price |\n|---|---|\n| Bread | 4.50 |\n"
    assert asyncio.run(make_document({"path": "plan.docx", "content": docx_md}, ctx)).startswith("created plan.docx")
    t = document_text(tmp_path / "plan.docx")
    assert "# Bakery plan" in t and "Open on Friday." in t and "- buy flour" in t and "Bread | 4.50" in t

    deck = "# OmniBots\nA team of bots\n\n# Why\n- they plan\n- they check\nNotes: smile\n\n# Prices\n| Plan | $ |\n|--|--|\n| Free | 0 |\n"
    assert "3 slides" in asyncio.run(make_document({"path": "deck.pptx", "content": deck}, ctx))
    t = document_text(tmp_path / "deck.pptx")
    assert "(PowerPoint, 3 slides)" in t and "they check" in t and "notes: smile" in t and "Free" in t

    sheet = "## Sales\n| Flavour | Sold |\n|---|---|\n| Lemon | 41 |\n| Cherry | 97 |\n\n## Costs\nflour,12.5\nsugar,3\n"
    assert "2 sheet(s)" in asyncio.run(make_document({"path": "s.xlsx", "content": sheet}, ctx))
    import openpyxl
    wb = openpyxl.load_workbook(tmp_path / "s.xlsx")
    assert wb.sheetnames == ["Sales", "Costs"] and wb["Sales"]["B3"].value == 97 and wb["Costs"]["B1"].value == 12.5

    out = asyncio.run(make_document({"path": "report.pdf", "content": "# Report\n\nRevenue grew **12%**.\n\n- one\n- two\n"}, ctx))
    assert "page(s)" in out, out
    assert "Revenue grew 12%" in document_text(tmp_path / "report.pdf")

    assert "outside your workspace" in asyncio.run(make_document({"path": "../x.docx", "content": "a"}, ctx))
    assert "must end in" in asyncio.run(make_document({"path": "x.txt", "content": "a"}, ctx))


def test_account_limits_are_said_plainly(tmp_path):
    """Live 2026-10-07: music came back HTTP 410 / 2153 and voice cloning 2061 on the user's MiniMax plan."""
    def handler(req):
        if req.url.path == "/v1/music_generation":
            return httpx.Response(410, json={"base_resp": {"status_code": 2153, "status_msg": "no longer available"}})
        return httpx.Response(200, json={"base_resp": {"status_code": 2061, "status_msg": "your current token plan not support model, voice_clone"},
                                         "file": {"file_id": 1}})
    factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    t = {x.name: x for x in create_tools(lambda: Prov("k"), None, tmp_path, client_factory=factory)}
    out = run(t["generate_music"], {"prompt": "jazz"}, tmp_path)
    assert "closed its music API to this account" in out and "don't retry" in out
    (tmp_path / "s.mp3").write_bytes(b"ID3")
    assert "plan doesn't include" in run(t["clone_voice"], {"sample": "s.mp3", "name": "me"}, tmp_path)
