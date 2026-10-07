"""A17.a.02 watch_video and A17.a.03 camera_snapshot.

watch_video runs the real ffmpeg on a real video (made by ffmpeg); the vision model is a stand-in that records what
it was sent. The camera is a local HTTP server serving a real JPEG and a real MJPEG stream (an ESP32 CameraWebServer
answers the same way), and the approval gate is the real one: a camera picture asks even under ask_from = "R5"."""

from __future__ import annotations

import asyncio
import http.server
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from omnibots.runtime import eyes
from omnibots.runtime.eyes import eye_tools, first_jpeg, local_address
from omnibots.runtime.tools import ToolContext

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


@pytest.fixture(scope="module")
def jpeg(tmp_path_factory) -> bytes:
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    out = tmp_path_factory.mktemp("img") / "pic.jpg"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=1",
                    "-frames:v", "1", "-y", str(out)], check=True)
    return out.read_bytes()


@needs_ffmpeg
def test_watch_video_sends_evenly_spaced_frames_with_their_times(tmp_path):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25:duration=10",
                    "-pix_fmt", "yuv420p", "-y", str(tmp_path / "clip.mp4")], check=True)
    seen = {}

    async def vision(messages, bot_id, job_id):
        parts = messages[0]["content"]
        seen["text"] = parts[0]["text"]
        seen["images"] = [p for p in parts if p["type"] == "image_url"]
        return "a test pattern with a counter"

    tool = _tool(eye_tools(vision), "watch_video")
    out = asyncio.run(tool.fn({"path": "clip.mp4", "frames": 4, "question": "what is it?"}, ToolContext(bot_id="b", workspace=tmp_path)))
    assert len(seen["images"]) == 1                              # one contact sheet: MiniMax mixes up several images
    assert seen["images"][0]["image_url"]["url"].startswith("data:image/jpeg;base64,/9j/")
    assert "#1 0:01.2, #2 0:03.8, #3 0:06.2, #4 0:08.8" in seen["text"] and "what is it?" in seen["text"]
    assert out.startswith("(watched 4 frames of clip.mp4") and "a test pattern with a counter" in out
    assert not list(tmp_path.glob("frame_*"))                    # frames never land in the project


def test_watch_video_refuses_outside_and_missing_files(tmp_path):
    tool = _tool(eye_tools(lambda *a: None), "watch_video")
    ctx = ToolContext(bot_id="b", workspace=tmp_path)
    assert "outside your workspace" in asyncio.run(tool.fn({"path": "../x.mp4"}, ctx))
    assert "no such video" in asyncio.run(tool.fn({"path": "nope.mp4"}, ctx))
    (tmp_path / "notvideo.mp4").write_text("hello")
    assert asyncio.run(tool.fn({"path": "notvideo.mp4"}, ctx)).startswith("ERROR")


class _Cam(http.server.BaseHTTPRequestHandler):
    jpeg = b""

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/capture":
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(self.jpeg)))
            self.end_headers()
            self.wfile.write(self.jpeg)
        elif self.path == "/stream":                     # MJPEG: never ends
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            try:
                for _ in range(200):
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + self.jpeg + b"\r\n")
                    self.wfile.flush()
                    threading.Event().wait(0.05)
            except OSError:
                pass
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture
def camera(jpeg):
    _Cam.jpeg = jpeg
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Cam)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _home(tmp_path, camera_toml: str) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    from omnibots.settings import DEFAULT_SETTINGS_TOML
    (home / "settings.toml").write_text(DEFAULT_SETTINGS_TOML.replace('[camera]\n', '[camera]\n' + camera_toml + '\n', 1)
                                        .replace('source = ""\nurl = ""\ndevice = ""\n', ''), encoding="utf-8")
    return home


@pytest.mark.parametrize("path", ["/capture", "/stream"])
def test_camera_snapshot_saves_one_picture_from_a_local_camera(tmp_path, camera, jpeg, path):
    home = _home(tmp_path, f'source = "url"\nurl = "{camera}{path}"\ndevice = ""')
    ws = tmp_path / "proj"
    ws.mkdir()
    out = asyncio.run(_tool(eye_tools(None, home), "camera_snapshot").fn({"why": "see the board"}, ToolContext(bot_id="b", workspace=ws)))
    pics = list((ws / "camera").glob("camera-*.jpg"))
    assert len(pics) == 1 and pics[0].read_bytes() == jpeg, out
    assert out.startswith("saved camera/camera-")


def test_camera_refuses_public_addresses_and_says_how_to_set_up(tmp_path, monkeypatch):
    monkeypatch.setattr(eyes, "OMNIONE_PREFS", tmp_path / "none.json")
    ctx = ToolContext(bot_id="b", workspace=tmp_path)
    home = _home(tmp_path, 'source = "url"\nurl = "http://93.184.216.34/capture"\ndevice = ""')
    assert "local network" in asyncio.run(_tool(eye_tools(None, home), "camera_snapshot").fn({"why": "x"}, ctx))
    empty = tmp_path / "e"
    empty.mkdir()
    (empty / "settings.toml").write_text("")
    assert "no camera is set up" in asyncio.run(_tool(eye_tools(None, empty), "camera_snapshot").fn({"why": "x"}, ctx))
    assert local_address("http://192.168.40.13/capture") and local_address("http://esp32cam.local/")
    assert not local_address("http://example.com/x.jpg")
    assert first_jpeg(b"junk\xff\xd8\xff\xe0abc\xff\xd9tail") == b"\xff\xd8\xff\xe0abc\xff\xd9"


def test_camera_falls_back_to_omnione_setting(tmp_path, monkeypatch):
    prefs = tmp_path / "prefs.json"
    prefs.write_text('{"camera": {"source": "url", "url": "http://192.168.40.13/capture", "label": "ESP32 camera"}}')
    monkeypatch.setattr(eyes, "OMNIONE_PREFS", prefs)
    empty = tmp_path / "h"
    empty.mkdir()
    (empty / "settings.toml").write_text("")
    c = eyes.camera_config(empty)
    assert c["url"] == "http://192.168.40.13/capture" and c["from"] == "OmniOne"


def test_a_camera_picture_always_asks_even_when_nothing_else_does(tmp_path, camera, jpeg):
    """The real agent gate: ask_from = "R5" lets R3 tools run without a click, but never camera_snapshot."""
    from mock_provider import MockProviders, sse
    from test_a3_runtime import Harness, call
    from omnibots.db import Database
    from omnibots.runtime.approvals import ApprovalCenter
    home = _home(tmp_path, f'source = "url"\nurl = "{camera}/capture"\ndevice = ""')

    async def go():
        db = Database(tmp_path / "db.sqlite")
        await db.open()
        with MockProviders() as mock:
            mock.script("fake", sse("", tool_calls=[call("camera_snapshot", why="check the 3D print")]), sse("it looks fine"))
            h = Harness(tmp_path, mock, db=db)
            h.bot.approvals = h.approvals = ApprovalCenter(db, ask_from="R5")
            for t in eye_tools(None, home):
                h.bot.tools.add(t)
            task = asyncio.create_task(h.bot.run("look at the printer"))
            for _ in range(500):
                await asyncio.sleep(0.02)
                if h.approvals.list_pending():
                    break
            pending = h.approvals.list_pending()
            nothing_yet = not (tmp_path / "ws" / "camera").exists()
            await h.approvals.decide(pending[0]["id"], True, "ok")
            res = await asyncio.wait_for(task, 15)
        await db.close()
        return pending, nothing_yet, res

    pending, nothing_yet, res = asyncio.run(go())
    assert pending and pending[0]["risk"] == "R3" and "camera_snapshot: check the 3D print" in pending[0]["summary"]
    assert nothing_yet
    assert res.status == "done"
    assert [p.read_bytes() for p in (tmp_path / "ws" / "camera").glob("*.jpg")] == [jpeg]
