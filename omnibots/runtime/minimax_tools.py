"""MiniMax modalities as tools (PLAN.md A8.b.04), from MiniMax's official docs
(platform.minimax.io, checked 2026-09-25):

  describe_image   image understanding: the image is sent to a vision-capable
                   chat model as an image_url part (R1)
  text_to_speech   POST /v1/t2a_v2 (speech-2.8-*, text < 10,000 chars, voice_id,
                   hex audio) → an mp3 in the workspace (R1: covered by the
                   Token Plan's shared text/image/speech quota)
  generate_video   POST /v1/video_generation → poll /v1/query/video_generation
                   → GET /v1/files/retrieve_content. **R4**: video is NOT in the
                   Token Plan, so it costs real money; the user approves every one.
The key is Omni's minimax.io key (A1); it is never shown to the model.
"""

from __future__ import annotations

import asyncio
import base64
import mimetypes
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from omnibots.runtime.tools import Tool, ToolContext

BASE = "https://api.minimax.io/v1"
# Video output price per second, MiniMax pay-as-you-go page (platform.minimax.io/docs/guides/pricing-paygo),
# checked 2026-09-26. Anything above 768P is priced at the 2K rate (the most expensive listed).
VIDEO_USD_PER_SECOND = {"480P": 0.05, "512P": 0.05, "720P": 0.08, "768P": 0.08, "1080P": 0.13, "2K": 0.13}


def video_cost(args: dict[str, Any]) -> float | None:
    res = str(args.get("resolution") or "768P").upper()
    rate = VIDEO_USD_PER_SECOND.get(res)
    if rate is None:
        return None
    return round(rate * int(args.get("duration") or 6), 2)
VOICE_DEFAULT = "English_Graceful_Lady"


def _key(get_provider) -> str:
    p = get_provider()
    if not p or not p.api_key:
        raise RuntimeError("no minimax.io key in Omni (set it with /apikey minimax.io <key>)")
    return p.api_key


def _path(ctx: ToolContext, rel: str) -> Path:
    p = (ctx.workspace / rel).resolve()
    if not p.is_relative_to(ctx.workspace.resolve()):
        raise ValueError("path is outside your workspace")
    return p


def _check(data: dict[str, Any]) -> None:
    br = data.get("base_resp") or {}
    if br.get("status_code", 0) != 0:
        raise RuntimeError(f"MiniMax error {br.get('status_code')}: {br.get('status_msg')}")


async def _video_card(args: dict[str, Any]) -> dict[str, Any]:
    return {"prompt": str(args.get("prompt") or ""), "model": str(args.get("model") or "MiniMax-Hailuo-2.3"),
            "duration_seconds": int(args.get("duration") or 6), "resolution": str(args.get("resolution") or "768P"),
            "price": "MiniMax pay-as-you-go, per second of output (checked 2026-09-26)"}


def minimax_tools(get_provider: Callable[[], Any], *, vision_chat: Callable | None = None,
                  client_factory=lambda: httpx.AsyncClient(timeout=httpx.Timeout(120.0)), poll_every: float = 10.0) -> list[Tool]:
    async def text_to_speech(args: dict[str, Any], ctx: ToolContext) -> str:
        text = str(args.get("text") or "")
        if not text.strip() or len(text) >= 10_000:
            return "ERROR: text is required and must be under 10,000 characters"
        out = _path(ctx, str(args.get("path") or "speech.mp3"))
        body = {"model": str(args.get("model") or "speech-2.8-turbo"), "text": text, "stream": False,
                "voice_setting": {"voice_id": str(args.get("voice_id") or VOICE_DEFAULT), "speed": float(args.get("speed") or 1), "vol": 1, "pitch": 0},
                "audio_setting": {"format": "mp3", "sample_rate": 32000, "bitrate": 128000, "channel": 1}}
        async with client_factory() as c:
            r = await c.post(f"{BASE}/t2a_v2", json=body, headers={"Authorization": f"Bearer {_key(get_provider)}"})
        if r.status_code >= 400:
            return f"ERROR: MiniMax TTS HTTP {r.status_code}: {r.text[:300]}"
        data = r.json()
        try:
            _check(data)
        except RuntimeError as exc:
            return f"ERROR: {exc}"
        audio = bytes.fromhex(data["data"]["audio"])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(audio)
        info = data.get("extra_info") or {}
        return f"wrote {out.name} ({len(audio):,} bytes, {info.get('audio_length', '?')} ms of audio)"

    async def describe_image(args: dict[str, Any], ctx: ToolContext) -> str:
        if vision_chat is None:
            return "ERROR: no vision-capable model is configured"
        img = _path(ctx, str(args.get("path") or ""))
        if not img.is_file():
            return f"ERROR: no such image: {args.get('path')}"
        mime = mimetypes.guess_type(img.name)[0] or "image/png"
        if not mime.startswith("image/") or img.stat().st_size > 8_000_000:
            return "ERROR: not an image, or larger than 8 MB"
        url = f"data:{mime};base64,{base64.b64encode(img.read_bytes()).decode()}"
        question = str(args.get("question") or "Describe this image in detail.")
        msgs = [{"role": "user", "content": [{"type": "text", "text": question}, {"type": "image_url", "image_url": {"url": url}}]}]
        return await vision_chat(msgs, ctx.bot_id, ctx.job_id)

    async def generate_video(args: dict[str, Any], ctx: ToolContext) -> str:
        prompt = str(args.get("prompt") or "").strip()
        if not prompt or len(prompt) > 2000:
            return "ERROR: prompt is required (max 2000 characters)"
        out = _path(ctx, str(args.get("path") or "video.mp4"))
        headers = {"Authorization": f"Bearer {_key(get_provider)}"}
        body = {"model": str(args.get("model") or "MiniMax-Hailuo-2.3"), "prompt": prompt,
                "duration": int(args.get("duration") or 6), "resolution": str(args.get("resolution") or "768P")}
        async with client_factory() as c:
            r = await c.post(f"{BASE}/video_generation", json=body, headers=headers)
            data = r.json() if r.status_code < 400 else {"base_resp": {"status_code": r.status_code, "status_msg": r.text[:200]}}
            _check(data)
            task_id = data["task_id"]
            await ctx.event("console", f"  🎬 video task {task_id} started; checking every {poll_every:.0f}s")
            deadline = time.monotonic() + 20 * 60
            while True:
                await asyncio.sleep(poll_every)
                q = (await c.get(f"{BASE}/query/video_generation", params={"task_id": task_id}, headers=headers)).json()
                status = str(q.get("status", "")).lower()
                if status == "success":
                    f = await c.get(f"{BASE}/files/retrieve_content", params={"file_id": q["file_id"]}, headers=headers)
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_bytes(f.content)
                    return f"wrote {out.name} ({len(f.content):,} bytes, {q.get('video_width')}x{q.get('video_height')})"
                if status == "fail" or time.monotonic() > deadline:
                    return f"ERROR: video task {task_id} {'failed' if status == 'fail' else 'timed out'}"

    s = {"type": "string"}
    return [
        Tool("describe_image", "Look at an image in your workspace and answer a question about it (vision).",
             {"type": "object", "properties": {"path": s, "question": s}, "required": ["path"]},
             "R1", describe_image, timeout=180, path_arg=None, summary=lambda a: f"describe_image {a.get('path')}"),
        Tool("text_to_speech", "Turn text (< 10,000 chars) into an mp3 in your workspace with MiniMax speech.",
             {"type": "object", "properties": {"text": s, "path": s, "voice_id": s, "speed": {"type": "number"}, "model": s}, "required": ["text"]},
             "R1", text_to_speech, timeout=180, path_arg=None, summary=lambda a: f"text_to_speech → {a.get('path', 'speech.mp3')} ({len(str(a.get('text', '')))} chars)"),
        Tool("generate_video", "Generate a short video from a text prompt with MiniMax Hailuo. COSTS MONEY (not in the Token Plan): the user approves each one.",
             {"type": "object", "properties": {"prompt": s, "path": s, "duration": {"type": "integer"}, "resolution": s, "model": s}, "required": ["prompt"]},
             "R4", generate_video, timeout=1300, path_arg=None, cost=video_cost,
             rehearse=lambda a, c: _video_card(a),
             summary=lambda a: f"generate_video ({a.get('duration', 6)}s {a.get('resolution', '768P')}, PAID): {str(a.get('prompt', ''))[:80]}"),
    ]
