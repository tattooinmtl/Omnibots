"""Watching videos and the live camera (PLAN.md A17.a.02, A17.a.03).

  watch_video      ffmpeg samples frames evenly across a video in the workspace; the vision model answers a question
                   about them, with each frame's time. R1 (reads a local file, uses the shared vision quota).
  camera_snapshot  one picture from the camera chosen in settings.toml [camera] into the project's camera/ folder:
                   a picture address on the local network (an ESP32 CameraWebServer's http://<ip>/capture, any JPEG
                   or MJPEG URL) or a webcam on this PC through ffmpeg (DirectShow). R3 and ALWAYS asks the user,
                   whatever [approvals] ask_from says: nobody is photographed without a click. A bot can't choose the
                   address (no reaching other machines through it); with no [camera] source, OmniOne's camera setting
                   is used if OmniOne has one (read-only).
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from omnibots.runtime.tools import Tool, ToolContext

MAX_FRAMES = 16
FRAME_WIDTH = 768
SNAP_TIMEOUT = 8.0
MAX_JPEG = 8 * 1024 * 1024
KEEP_PICTURES = 60
OMNIONE_PREFS = Path.home() / ".omnione" / "app" / ".gwn-prefs.json"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _inside(ctx: ToolContext, rel: str) -> Path:
    p = (ctx.workspace / rel).resolve()
    if not p.is_relative_to(ctx.workspace.resolve()):
        raise ValueError("path is outside your workspace")
    return p


def _ffmpeg(name: str = "ffmpeg") -> str | None:
    return shutil.which(name)


async def _run(cmd: list[str], timeout: float) -> tuple[int, bytes, str]:
    def go():
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=NO_WINDOW)
            return r.returncode, r.stdout, r.stderr.decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            return -2, b"", "timed out"
        except OSError as exc:
            return -1, b"", str(exc)
    return await asyncio.to_thread(go)


async def video_duration(path: Path) -> float | None:
    probe = _ffmpeg("ffprobe")
    if not probe:
        return None
    code, out, _ = await _run([probe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)], 30)
    try:
        return float(out.decode().strip()) if code == 0 else None
    except ValueError:
        return None


async def sample_frames(path: Path, count: int, out_dir: Path) -> list[tuple[float, Path]]:
    """`count` JPEG frames spread evenly over the video (the middle of each slice), scaled to FRAME_WIDTH."""
    ff = _ffmpeg()
    if not ff:
        raise RuntimeError("ffmpeg is not installed (winget install Gyan.FFmpeg)")
    dur = await video_duration(path)
    if not dur or dur <= 0:
        raise RuntimeError("can't read the video's length (is it a video ffmpeg understands?)")
    frames = []
    for i in range(count):
        t = dur * (i + 0.5) / count
        dest = out_dir / f"frame_{i:02d}.jpg"
        code, _, err = await _run([ff, "-hide_banner", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1",
                                   "-vf", f"scale='min({FRAME_WIDTH},iw)':-2", "-q:v", "4", "-y", str(dest)], 60)
        if code == 0 and dest.is_file() and dest.stat().st_size:
            frames.append((t, dest))
    if not frames:
        raise RuntimeError(f"ffmpeg got no frames from the video: {err.strip()[-200:]}")
    return frames


def _stamp(t: float) -> str:
    return f"{int(t // 60)}:{t % 60:04.1f}"


def contact_sheet(frames: list[tuple[float, Path]], tile_width: int = 512) -> bytes:
    """The frames as ONE image: a grid, left to right then down, each tile labelled with its number and time.
    MiniMax M3 reads one image reliably but mixes up several in one message (live, 2026-10-07: red + blue came
    back as "brown, white"), so the video goes to the model as a single picture."""
    from io import BytesIO

    from PIL import Image, ImageDraw, ImageFont
    imgs = [Image.open(p).convert("RGB") for _, p in frames]
    cols = 1 if len(imgs) == 1 else 2 if len(imgs) <= 4 else 3 if len(imgs) <= 9 else 4
    rows = -(-len(imgs) // cols)
    th = max(1, round(tile_width * imgs[0].height / imgs[0].width))
    bar = 34
    sheet = Image.new("RGB", (cols * tile_width + (cols + 1) * 6, rows * (th + bar) + (rows + 1) * 6), (255, 255, 255))
    try:
        font = ImageFont.truetype("arial.ttf", 22)
    except OSError:
        font = ImageFont.load_default()
    draw = ImageDraw.Draw(sheet)
    for i, ((t, _), img) in enumerate(zip(frames, imgs)):
        x, y = 6 + (i % cols) * (tile_width + 6), 6 + (i // cols) * (th + bar + 6)
        draw.rectangle([x, y, x + tile_width - 1, y + bar - 1], fill=(20, 20, 20))
        draw.text((x + 8, y + 5), f"#{i + 1}  {_stamp(t)}", fill=(255, 255, 255), font=font)
        sheet.paste(img.resize((tile_width, th)), (x, y + bar))
    out = BytesIO()
    sheet.save(out, "JPEG", quality=85)
    return out.getvalue()


# ── the camera ───────────────────────────────────────────────────────────────

def camera_config(home: Path | None) -> dict[str, str]:
    """[camera] from settings.toml; when it names no source, OmniOne's camera (read-only), else empty."""
    cfg: dict[str, Any] = {}
    if home is not None:
        try:
            from omnibots.settings import load_settings
            cfg = dict(load_settings(home / "settings.toml").get("camera") or {})
        except Exception:
            cfg = {}
    if not cfg.get("source"):
        try:
            c = json.loads(OMNIONE_PREFS.read_text(encoding="utf-8")).get("camera") or {}
            if c.get("source") in ("url", "webcam"):
                cfg = {"source": c["source"], "url": c.get("url", ""), "device": c.get("device", ""),
                       "label": c.get("label", "") or "OmniOne's camera", "from": "OmniOne"}
        except (OSError, ValueError):
            pass
    return {k: str(v) for k, v in cfg.items()}


def local_address(url: str) -> bool:
    """A camera must be on this PC or the local network: a private, loopback or link-local address (or .local)."""
    host = urlparse(url).hostname or ""
    if host.endswith(".local") or host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


def first_jpeg(buf: bytes) -> bytes | None:
    start = buf.find(b"\xff\xd8\xff")
    if start < 0:
        return None
    end = buf.find(b"\xff\xd9", start + 3)
    return buf[start:end + 2] if end >= 0 else None


async def grab_url(url: str) -> bytes:
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(SNAP_TIMEOUT), follow_redirects=False, trust_env=False) as c:
            async with c.stream("GET", url, headers={"Cache-Control": "no-cache"}) as r:
                if r.status_code >= 400:
                    raise RuntimeError(f"the camera answered HTTP {r.status_code}")
                multipart = "multipart" in r.headers.get("content-type", "")
                buf = b""
                async for chunk in r.aiter_bytes():
                    buf += chunk
                    jpg = first_jpeg(buf)
                    if jpg and multipart:              # a stream never ends: the first whole picture is enough
                        return jpg
                    if len(buf) > MAX_JPEG:
                        break
                jpg = first_jpeg(buf)
                if jpg:
                    return jpg
    except httpx.TimeoutException:
        raise RuntimeError(f"the camera at {url} didn't answer within {SNAP_TIMEOUT:.0f}s (is it on, on this network?)")
    except httpx.HTTPError as exc:
        raise RuntimeError(f"can't reach the camera at {url}: {exc}")
    raise RuntimeError("the camera sent something that isn't a JPEG picture")


async def grab_webcam(device: str) -> bytes:
    ff = _ffmpeg()
    if not ff:
        raise RuntimeError("ffmpeg is not installed (winget install Gyan.FFmpeg)")
    code, out, err = await _run([ff, "-hide_banner", "-loglevel", "error", "-f", "dshow", "-i", f"video={device}", "-frames:v", "1",
                                 "-q:v", "3", "-f", "image2", "-c:v", "mjpeg", "pipe:1"], 15)
    if code != 0 or not out:
        raise RuntimeError(f'the webcam "{device}" gave no picture: {(err.strip().splitlines() or ["no output"])[-1]}')
    return out


async def list_webcams() -> list[str]:
    ff = _ffmpeg()
    if not ff:
        return []
    _, _, err = await _run([ff, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"], 10)
    import re
    return re.findall(r'"([^"]+)"\s+\(video\)', err)


def _camera_card(home: Path | None):
    async def card(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        c = camera_config(home)
        where = c.get("url") if c.get("source") == "url" else f"webcam {c.get('device') or '(not chosen)'}"
        return {"camera": c.get("label") or where, "source": where, "saves_to": "camera/ in the project",
                "why": str(args.get("why") or "")}
    return card


def eye_tools(vision_chat: Callable | None, home: Path | None = None) -> list[Tool]:
    async def watch_video(args: dict[str, Any], ctx: ToolContext) -> str:
        if vision_chat is None:
            return "ERROR: no vision-capable model is configured"
        try:
            video = _inside(ctx, str(args.get("path") or ""))
        except ValueError as exc:
            return f"ERROR: {exc}"
        if not video.is_file():
            return f"ERROR: no such video: {args.get('path')}"
        count = max(1, min(MAX_FRAMES, int(args.get("frames") or 8)))
        question = str(args.get("question") or "Describe what happens in this video, scene by scene.")
        with tempfile.TemporaryDirectory(prefix="omnibots-frames-") as tmp:
            try:
                frames = await sample_frames(video, count, Path(tmp))
            except RuntimeError as exc:
                return f"ERROR: {exc}"
            await ctx.event("console", f"  🎞 {len(frames)} frames from {video.name}")
            sheet = await asyncio.to_thread(contact_sheet, frames)
            content: list[dict[str, Any]] = [
                {"type": "text", "text": (
                    f"This one picture is a grid of {len(frames)} frames sampled evenly from the video {video.name}, read "
                    "left to right, then top to bottom. Each frame's dark bar shows its number and its time in the video ("
                    + ", ".join(f"#{i + 1} {_stamp(t)}" for i, (t, _) in enumerate(frames)) + f").\n\n{question}")},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(sheet).decode()}}]
        answer = await vision_chat([{"role": "user", "content": content}], ctx.bot_id, ctx.job_id)
        return f"(watched {len(frames)} frames of {video.name} at {', '.join(_stamp(t) for t, _ in frames)})\n{answer}"

    async def camera_snapshot(args: dict[str, Any], ctx: ToolContext) -> str:
        c = camera_config(home)
        try:
            if c.get("source") == "url":
                url = c.get("url", "")
                if not url:
                    return "ERROR: no camera address in settings.toml [camera] url"
                if not local_address(url):
                    return "ERROR: the camera address must be on this PC or the local network"
                jpg = await grab_url(url)
            elif c.get("source") == "webcam":
                if not c.get("device"):
                    cams = await list_webcams()
                    return ("ERROR: no webcam chosen in settings.toml [camera] device. Webcams on this PC: "
                            + (", ".join(cams) if cams else "none found"))
                jpg = await grab_webcam(c["device"])
            else:
                return ('ERROR: no camera is set up. The user sets settings.toml [camera] source = "url" (with url = '
                        '"http://<camera-ip>/capture") or "webcam" (with device = "<name>").')
        except RuntimeError as exc:
            return f"ERROR: {exc}"
        folder = ctx.workspace / "camera"
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / time.strftime("camera-%Y%m%d-%H%M%S.jpg")
        n = 1
        while dest.exists():
            dest = folder / time.strftime(f"camera-%Y%m%d-%H%M%S-{n}.jpg")
            n += 1
        dest.write_bytes(jpg)
        old = sorted(folder.glob("camera-*.jpg"))[:-KEEP_PICTURES]
        for f in old:
            f.unlink(missing_ok=True)
        await ctx.event("console", f"  📷 camera picture → camera/{dest.name} ({len(jpg):,} bytes)")
        return f"saved camera/{dest.name} ({len(jpg):,} bytes) from {c.get('label') or c.get('source')}; look at it with describe_image"

    s = {"type": "string"}
    return [
        Tool("watch_video", "Watch a video in your workspace: frames are sampled evenly across it and you get an answer to "
             "your question about them, with each frame's time. `frames` 1-16 (default 8).",
             {"type": "object", "properties": {"path": s, "question": s, "frames": {"type": "integer"}}, "required": ["path"]},
             "R1", watch_video, timeout=300, path_arg=None, summary=lambda a: f"watch_video {a.get('path')}"),
        Tool("camera_snapshot", "Take ONE picture with the camera the user set up (a webcam or a camera on the local network) "
             "into camera/ in your project; then look at it with describe_image. The user approves every picture; say why.",
             {"type": "object", "properties": {"why": s}, "required": ["why"]},
             "R3", camera_snapshot, timeout=60, path_arg=None, always_ask=True, rehearse=_camera_card(home),
             summary=lambda a: f"camera_snapshot: {str(a.get('why') or '')[:100]}"),
    ]
