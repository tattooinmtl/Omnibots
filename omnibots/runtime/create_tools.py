"""Making media (PLAN.md A17.b): pictures, picture edits, music and cloned voices for speech files.

All of these spend money, so they are R4: the user approves each call on a card with the price, and the spend caps
([budgets] money_*) apply. Prices from MiniMax's pay-as-you-go page (platform.minimax.io/docs/guides/pricing-paygo,
checked 2026-10-07) and OpenAI's image pricing.

  generate_image  MiniMax image-01, 1-4 pictures, optional reference photo whose face/subject is kept     $0.0035 each
  edit_image      change a picture by describing the change: OpenAI gpt-image-1 (/images/edits) when an OpenAI
                  key exists; otherwise MiniMax image-01 with the picture as the subject reference, which keeps the
                  subject but redraws the rest (the honest "partly", said in the answer)                  ≤ $0.19
  generate_music  MiniMax music (lyrics + style, or instrumental, or lyrics written by MiniMax)         $0.15 a song
  clone_voice     a recording (10 s – 5 min of clear speech) → a voice_id for text_to_speech. Only the user's
                  own voice or one they have permission to use. For speech FILES: OmniBots has no live voice.  $1.50
"""

from __future__ import annotations

import base64
import json
import mimetypes
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from omnibots.runtime.tools import Tool, ToolContext

BASE = "https://api.minimax.io/v1"
IMAGE_RATIOS = ("1:1", "16:9", "4:3", "3:2", "2:3", "3:4", "9:16", "21:9")
MUSIC_MODELS = ("music-3.0", "music-2.6", "music-3.0-free", "music-2.6-free")
PRICE = {"image": 0.0035, "edit_openai": 0.19, "music": 0.15, "clone": 1.5}
CLONE_EXTS = {".mp3", ".m4a", ".wav"}
VOICES_FILE = "voices.json"                     # in the OmniBots home: the voices cloned so far


def _inside(ctx: ToolContext, rel: str) -> Path:
    p = (ctx.workspace / rel).resolve()
    if not p.is_relative_to(ctx.workspace.resolve()):
        raise ValueError("path is outside your workspace")
    return p


def _dest(ctx: ToolContext, given: str | None, kind: str, ext: str, i: int = 0) -> Path:
    if given:
        rel = given if i == 0 else re.sub(r"(\.\w+)?$", lambda m: f"-{i + 1}{m.group(1) or '.' + ext}", given, count=1)
        if not Path(rel).suffix:
            rel += f".{ext}"
    else:
        rel = f"media/{kind}-{time.strftime('%Y%m%d-%H%M%S')}{f'-{i + 1}' if i else ''}.{ext}"
    p = _inside(ctx, rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or ""
    if mime not in ("image/png", "image/jpeg", "image/webp", "image/gif"):
        raise ValueError(f"{path.name} isn't a png, jpg, gif or webp picture")
    if path.stat().st_size > 10 * 1024 * 1024:
        raise ValueError(f"{path.name} is over 10 MB")
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def _rel(ctx: ToolContext, p: Path) -> str:
    return p.relative_to(ctx.workspace.resolve()).as_posix()


# Account limits seen live (2026-10-07), said plainly so the bot reports them instead of retrying
ACCOUNT_LIMITS = {
    2153: "MiniMax has closed its music API to this account (only older paying customers keep it), so music can't be "
          "made with this key. Tell the user; don't retry.",
    2061: "this MiniMax plan doesn't include that feature (MiniMax: 'your current token plan not support model'). Tell "
          "the user it needs a MiniMax plan that includes it; don't retry.",
}


def _check(data: dict[str, Any]) -> None:
    br = data.get("base_resp") or {}
    code = br.get("status_code", 0)
    if code != 0:
        raise RuntimeError(ACCOUNT_LIMITS.get(code) or f"MiniMax error {code}: {br.get('status_msg')}")


def image_request(args: dict[str, Any], ctx: ToolContext, reference: Path | None = None) -> dict[str, Any]:
    ratio = str(args.get("ratio") or "1:1")
    if ratio not in IMAGE_RATIOS:
        raise ValueError(f"ratio must be one of {', '.join(IMAGE_RATIOS)}")
    body = {"model": "image-01", "prompt": str(args.get("prompt") or "").strip(), "aspect_ratio": ratio,
            "response_format": "base64", "n": max(1, min(4, int(args.get("n") or 1))), "prompt_optimizer": False}
    ref = reference or (_inside(ctx, str(args["reference_image"])) if args.get("reference_image") else None)
    if ref is not None:
        if not ref.is_file():
            raise ValueError(f"no such picture: {ref.name}")
        body["subject_reference"] = [{"type": "character", "image_file": _data_url(ref)}]
    return body


def make_voice_id(name: str) -> str:
    """MiniMax: 8-256 chars, starts with a letter, letters/digits/-/_, not ending in - or _."""
    import unicodedata
    s = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode()
    s = re.sub(r"[^\w-]+", "_", s)
    s = re.sub(r"^[^A-Za-z]+", "", s).rstrip("-_") or "Voice"
    return f"Bots_{s}_{time.strftime('%y%m%d%H%M')}"[:64].rstrip("-_")


def create_tools(get_minimax: Callable[[], Any], get_openai: Callable[[], Any] | None = None, home: Path | None = None,
                 client_factory=lambda: httpx.AsyncClient(timeout=httpx.Timeout(600.0))) -> list[Tool]:
    def mm_key() -> str:
        p = get_minimax()
        if not p or not getattr(p, "api_key", ""):
            raise RuntimeError("no minimax.io key (add it in Settings → Providers or in Omni)")
        return p.api_key

    async def mm_post(path: str, body: dict[str, Any]) -> dict[str, Any]:
        async with client_factory() as c:
            r = await c.post(f"{BASE}{path}", json=body, headers={"Authorization": f"Bearer {mm_key()}"})
        try:
            data = r.json()
        except ValueError:
            data = None
        if isinstance(data, dict) and (data.get("base_resp") or {}).get("status_code"):
            _check(data)                                    # MiniMax's own reason, also on an HTTP 4xx (the 410 for music)
        if r.status_code >= 400 or not isinstance(data, dict):
            raise RuntimeError(f"MiniMax HTTP {r.status_code}: {r.text[:300]}")
        return data

    def save_pictures(ctx: ToolContext, blobs: list[bytes], dest: str | None, kind: str) -> list[str]:
        saved = []
        for i, b in enumerate(blobs):
            ext = "png" if b[:4] == b"\x89PNG" else "jpg"
            p = _dest(ctx, dest, kind, ext, i)
            p.write_bytes(b)
            saved.append(_rel(ctx, p))
        return saved

    async def generate_image(args: dict[str, Any], ctx: ToolContext) -> str:
        prompt = str(args.get("prompt") or "").strip()
        if not prompt or len(prompt) > 1500:
            return "ERROR: prompt is required (max 1500 characters)"
        try:
            body = image_request(args, ctx)
            data = await mm_post("/image_generation", body)
        except (ValueError, RuntimeError) as exc:
            return f"ERROR: {exc}"
        pics = (data.get("data") or {}).get("image_base64") or []
        if not pics:
            return "ERROR: MiniMax made no picture (its safety check may have refused the prompt)"
        saved = save_pictures(ctx, [base64.b64decode(b) for b in pics], args.get("path"), "image")
        await ctx.event("console", f"  🖼 {', '.join(saved)}")
        return f"saved {len(saved)} picture(s): {', '.join(saved)}. Look at them with describe_image before you use them."

    async def edit_image(args: dict[str, Any], ctx: ToolContext) -> str:
        try:
            src = _inside(ctx, str(args.get("image") or ""))
        except ValueError as exc:
            return f"ERROR: {exc}"
        if not src.is_file():
            return f"ERROR: no such picture: {args.get('image')}"
        change = str(args.get("prompt") or "").strip()
        if not change:
            return "ERROR: prompt (the change to make) is required"
        oa = get_openai() if get_openai else None
        if oa is not None and getattr(oa, "api_key", ""):
            mime = mimetypes.guess_type(src.name)[0] or ""
            if mime not in ("image/png", "image/jpeg", "image/webp"):
                return "ERROR: the picture must be png, jpg or webp"
            base = (getattr(oa, "base_url", "") or "https://api.openai.com/v1").rstrip("/")
            async with client_factory() as c:
                r = await c.post(f"{base}/images/edits", headers={"Authorization": f"Bearer {oa.api_key}"},
                                 data={"model": "gpt-image-1", "prompt": change},
                                 files={"image": (src.name, src.read_bytes(), mime)})
            if r.status_code >= 400:
                return f"ERROR: OpenAI HTTP {r.status_code}: {r.text[:300]}"
            b64 = ((r.json().get("data") or [{}])[0]).get("b64_json")
            if not b64:
                return "ERROR: OpenAI returned no picture"
            saved = save_pictures(ctx, [base64.b64decode(b64)], args.get("path"), "edit")
            return f"saved the edited picture {saved[0]} (OpenAI gpt-image-1). Look at it with describe_image."
        # no OpenAI key: MiniMax keeps the subject from the picture and redraws the rest from the description
        try:
            body = image_request({"prompt": f"The same subject as the reference picture. {change}", "ratio": args.get("ratio") or "1:1"},
                                 ctx, reference=src)
            data = await mm_post("/image_generation", body)
        except (ValueError, RuntimeError) as exc:
            return f"ERROR: {exc}"
        pics = (data.get("data") or {}).get("image_base64") or []
        if not pics:
            return "ERROR: MiniMax made no picture (its safety check may have refused the prompt)"
        saved = save_pictures(ctx, [base64.b64decode(pics[0])], args.get("path"), "edit")
        return (f"saved {saved[0]}. Note: there is no OpenAI key, so this is a NEW picture that keeps the subject of "
                f"{src.name} with the change, not a pixel edit of the original. Look at it with describe_image.")

    async def generate_music(args: dict[str, Any], ctx: ToolContext) -> str:
        prompt = str(args.get("prompt") or "").strip()
        lyrics = str(args.get("lyrics") or "").strip()
        model = str(args.get("model") or "music-3.0")
        if not prompt or len(prompt) > 2000:
            return "ERROR: prompt (style and mood) is required, max 2000 characters"
        if len(lyrics) > 3500:
            return "ERROR: lyrics are over 3500 characters"
        if model not in MUSIC_MODELS:
            return f"ERROR: model must be one of {', '.join(MUSIC_MODELS)}"
        body: dict[str, Any] = {"model": model, "prompt": prompt, "output_format": "hex",
                                "audio_setting": {"sample_rate": 44100, "bitrate": 256000, "format": "mp3"}}
        if args.get("instrumental"):
            body["is_instrumental"] = True
        elif lyrics:
            body["lyrics"] = lyrics
        else:
            body["lyrics_optimizer"] = True
        try:
            out = _dest(ctx, args.get("path"), "song", "mp3")
            await ctx.event("console", "  🎵 composing (this can take a few minutes)")
            data = await mm_post("/music_generation", body)
        except (ValueError, RuntimeError) as exc:
            return f"ERROR: {exc}"
        audio = (data.get("data") or {}).get("audio") or ""
        if not re.fullmatch(r"[0-9a-fA-F]+", audio or "x"):
            return "ERROR: MiniMax returned no audio"
        out.write_bytes(bytes.fromhex(audio))
        info = data.get("extra_info") or {}
        secs = round((info.get("music_duration") or 0) / 1000)
        return f"wrote {_rel(ctx, out)} ({out.stat().st_size:,} bytes{f', {secs} s' if secs else ''}, {model})"

    def voices_path() -> Path | None:
        return home / VOICES_FILE if home else None

    async def clone_voice(args: dict[str, Any], ctx: ToolContext) -> str:
        try:
            sample = _inside(ctx, str(args.get("sample") or ""))
        except ValueError as exc:
            return f"ERROR: {exc}"
        if not sample.is_file():
            return f"ERROR: no such recording: {args.get('sample')}"
        name = str(args.get("name") or "").strip() or sample.stem
        with tempfile.TemporaryDirectory(prefix="omnibots-voice-") as tmp:
            src = sample
            if sample.suffix.lower() not in CLONE_EXTS:
                src = Path(tmp) / "sample.wav"
                r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(sample), "-ac", "1", "-ar", "32000", str(src)],
                                   capture_output=True, timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if r.returncode != 0 or not src.is_file():
                    return "ERROR: the recording must be mp3, m4a or wav (converting it needs ffmpeg)"
            if src.stat().st_size > 20 * 1024 * 1024:
                return "ERROR: the recording is over 20 MB; use 10 seconds to 5 minutes of clear speech"
            try:
                async with client_factory() as c:
                    up = await c.post(f"{BASE}/files/upload", headers={"Authorization": f"Bearer {mm_key()}"},
                                      data={"purpose": "voice_clone"}, files={"file": (src.name, src.read_bytes())})
                if up.status_code >= 400:
                    return f"ERROR: MiniMax upload HTTP {up.status_code}: {up.text[:200]}"
                upj = up.json()
                _check(upj)
                file_id = (upj.get("file") or {}).get("file_id")
                if file_id is None:
                    return "ERROR: MiniMax upload returned no file_id"
                voice_id = make_voice_id(name)
                body: dict[str, Any] = {"file_id": file_id, "voice_id": voice_id}
                preview = str(args.get("preview_text") or "").strip()
                if preview:
                    body.update(text=preview[:300], model="speech-2.8-turbo")
                data = await mm_post("/voice_clone", body)
            except RuntimeError as exc:
                return f"ERROR: {exc}"
        if (data.get("input_sensitive") or {}).get("type"):
            return "ERROR: MiniMax refused the recording in its safety check"
        vp = voices_path()
        if vp is not None:
            try:
                voices = json.loads(vp.read_text(encoding="utf-8")) if vp.is_file() else []
            except ValueError:
                voices = []
            voices = [v for v in voices if v.get("voice_id") != voice_id] + [
                {"voice_id": voice_id, "name": name[:60], "created": time.strftime("%Y-%m-%d %H:%M"), "sample": sample.name}]
            vp.write_text(json.dumps(voices, indent=2), encoding="utf-8")
        note = ""
        if data.get("demo_audio"):
            try:
                async with client_factory() as c:
                    d = await c.get(data["demo_audio"])
                if d.status_code < 400:
                    p = _dest(ctx, None, "voice-preview", "mp3")
                    p.write_bytes(d.content)
                    note = f"; a preview is in {_rel(ctx, p)}"
            except httpx.HTTPError:
                pass
        return (f"cloned the voice as voice_id {voice_id}{note}. Use it with text_to_speech (voice_id). MiniMax deletes a "
                "cloned voice that isn't used within 7 days.")

    async def list_voices(args: dict[str, Any], ctx: ToolContext) -> str:
        vp = voices_path()
        try:
            voices = json.loads(vp.read_text(encoding="utf-8")) if vp and vp.is_file() else []
        except ValueError:
            voices = []
        if not voices:
            return "no cloned voices yet (the default speech voice is English_Graceful_Lady)"
        return "\n".join(f"{v['voice_id']}  {v.get('name', '')}  (cloned {v.get('created', '?')})" for v in voices)

    def music_cost(a: dict[str, Any]) -> float:
        return 0.0 if str(a.get("model") or "").endswith("-free") else PRICE["music"]

    def edit_cost(a: dict[str, Any]) -> float:
        oa = get_openai() if get_openai else None
        return PRICE["edit_openai"] if oa is not None and getattr(oa, "api_key", "") else PRICE["image"]

    async def card(kind: str, a: dict[str, Any]) -> dict[str, Any]:
        return {"what": kind, **{k: (str(v)[:300]) for k, v in a.items()}}

    s = {"type": "string"}
    return [
        Tool("generate_image", "Make 1-4 pictures from a description with MiniMax image-01 (saved as files; default media/). "
             "`reference_image`: a picture in your workspace whose face or subject to keep. COSTS MONEY ($0.0035 each): "
             "the user approves each call.",
             {"type": "object", "properties": {"prompt": s, "ratio": {"type": "string", "enum": list(IMAGE_RATIOS)},
                                               "n": {"type": "integer"}, "reference_image": s, "path": s}, "required": ["prompt"]},
             "R4", generate_image, timeout=300, path_arg=None, cost=lambda a: round(PRICE["image"] * max(1, min(4, int(a.get("n") or 1))), 4),
             rehearse=lambda a, c: card("pictures (MiniMax image-01)", a),
             summary=lambda a: f"generate_image ×{a.get('n', 1)} (PAID): {str(a.get('prompt', ''))[:80]}"),
        Tool("edit_image", "Change a picture in your workspace by describing the change (\"make it night\", \"remove the "
             "background\"); saves a new file. With an OpenAI key it edits the picture itself; without one it makes a new "
             "picture that keeps the subject. COSTS MONEY: the user approves each call.",
             {"type": "object", "properties": {"image": s, "prompt": s, "path": s, "ratio": s}, "required": ["image", "prompt"]},
             "R4", edit_image, timeout=300, path_arg=None, cost=edit_cost, rehearse=lambda a, c: card("picture edit", a),
             summary=lambda a: f"edit_image {a.get('image')} (PAID): {str(a.get('prompt', ''))[:80]}"),
        Tool("generate_music", "Make a song or an instrumental with MiniMax music (an mp3; default media/). `prompt`: style, "
             "mood, instruments, tempo, voice. Then `lyrics` (with [Verse] [Chorus] [Bridge] tags), or instrumental: true, or "
             "neither and MiniMax writes the words. Takes a few minutes. COSTS MONEY (~$0.15): the user approves it.",
             {"type": "object", "properties": {"prompt": s, "lyrics": s, "instrumental": {"type": "boolean"},
                                               "model": {"type": "string", "enum": list(MUSIC_MODELS)}, "path": s}, "required": ["prompt"]},
             "R4", generate_music, timeout=900, path_arg=None, cost=music_cost, rehearse=lambda a, c: card("music (MiniMax)", a),
             summary=lambda a: f"generate_music (PAID): {str(a.get('prompt', ''))[:80]}"),
        Tool("clone_voice", "Clone a voice from a recording in your workspace (10 s to 5 min of clear speech) into a voice_id "
             "for text_to_speech. ONLY the user's own voice or one they say they may use; refuse anyone else's. COSTS MONEY "
             "($1.50): the user approves it.",
             {"type": "object", "properties": {"sample": s, "name": s, "preview_text": s}, "required": ["sample", "name"]},
             "R4", clone_voice, timeout=300, path_arg=None, cost=lambda a: PRICE["clone"],
             rehearse=lambda a, c: card("voice clone (MiniMax)", a),
             summary=lambda a: f"clone_voice '{a.get('name')}' from {a.get('sample')} (PAID)"),
        Tool("list_voices", "List the voices cloned so far (voice_id and name) for text_to_speech.",
             {"type": "object", "properties": {}}, "R0", list_voices, path_arg=None, summary=lambda a: "list_voices"),
    ]
