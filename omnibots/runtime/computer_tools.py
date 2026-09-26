"""The bot's own computer on the VPS (PLAN.md A10.f.04): tools that drive it over HTTPS.

Each bot logs in to the Bot Computers gateway with its own credentials (the vault secret
`computer_<bot_id>`; the model never sees it) and gets a 1-hour token that reaches only
its own computer. Commands run there (full internet through a logging proxy, isolated by
gVisor), never on the user's PC.

Risk classes: running a command uses command_risk (installs/pushes/downloads = R3,
destructive = R5), clicking/typing/keys act on live sites like the local browser (R3),
start/stop/screenshot/files are R1.
"""

from __future__ import annotations

import base64
import os
import time
from typing import Any, Callable

import httpx

from omnibots.runtime.more_tools import command_risk
from omnibots.runtime.tools import Tool, ToolContext

COMPUTERS_URL = os.environ.get("OMNIBOTS_COMPUTERS_URL", "https://computers.globalwarningnetworks.com").rstrip("/")


class ComputerClient:
    """Logs a bot in (token cached until it expires) and calls its computer's API."""

    def __init__(self, secret_for: Callable[[str], str | None], base_url: str = COMPUTERS_URL,
                 client_factory=lambda: httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=900.0)),
                 owner_key: Callable[[], str | None] | None = None, store_secret=None):
        """owner_key + store_secret: when a bot has no login yet, OmniBots (the owner) provisions one
        on the gateway and stores it in the vault (`computer_<bot>`); the bot then logs in with it."""
        self.secret_for, self.base = secret_for, base_url.rstrip("/")
        self.client_factory = client_factory
        self.owner_key, self.store_secret = owner_key, store_secret
        self._tokens: dict[str, tuple[str, float]] = {}

    async def _provision(self, c: httpx.AsyncClient, bot: str) -> str | None:
        key = self.owner_key() if self.owner_key else None
        if not key or self.store_secret is None:
            return None
        r = await c.post(f"{self.base}/admin/bots", json={"bot": bot}, headers={"X-Owner-Key": key})
        if r.status_code != 200:
            raise RuntimeError(f"couldn't create {bot}'s computer login: HTTP {r.status_code}")
        secret = r.json()["secret"]
        await self.store_secret(bot, secret)
        return secret

    async def _token(self, c: httpx.AsyncClient, bot: str) -> str:
        tok = self._tokens.get(bot)
        if tok and tok[1] > time.time() + 60:
            return tok[0]
        secret = self.secret_for(bot) or await self._provision(c, bot)
        if not secret:
            raise RuntimeError(f"{bot} has no computer login yet (the user adds it: computer_{bot} in the vault)")
        r = await c.post(f"{self.base}/auth/login", json={"bot": bot, "secret": secret})
        if r.status_code != 200:
            raise RuntimeError(f"computer login failed: HTTP {r.status_code}")
        d = r.json()
        self._tokens[bot] = (d["token"], time.time() + d.get("expires_in", 3600))
        return d["token"]

    async def call(self, bot: str, method: str, path: str, **kw) -> httpx.Response:
        async with self.client_factory() as c:
            for attempt in (1, 2):
                headers = {"Authorization": f"Bearer {await self._token(c, bot)}"}
                r = await c.request(method, f"{self.base}{path}", headers=headers, **kw)
                if r.status_code == 401 and attempt == 1:
                    self._tokens.pop(bot, None)                  # expired or revoked: log in once more
                    continue
                return r
        return r

    @staticmethod
    def error(r: httpx.Response) -> str:
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = r.text[:200]
        return f"ERROR: computer HTTP {r.status_code}: {detail}"


def computer_tools(client: ComputerClient) -> list[Tool]:
    s, i = {"type": "string"}, {"type": "integer"}

    async def start(args: dict[str, Any], ctx: ToolContext) -> str:
        r = await client.call(ctx.bot_id, "POST", "/computer/start")
        return "your computer is on (Linux desktop: Chromium, Python, Node, git; full internet, logged)" if r.status_code == 200 \
            else client.error(r)

    async def stop(args: dict[str, Any], ctx: ToolContext) -> str:
        r = await client.call(ctx.bot_id, "POST", "/computer/stop")
        return "your computer is off (your files in /home/bot are kept)" if r.status_code == 200 else client.error(r)

    async def run(args: dict[str, Any], ctx: ToolContext) -> str:
        cmd = str(args.get("command") or "").strip()
        if not cmd:
            return "ERROR: computer_run needs a command"
        await ctx.event("terminal", f"🖥 $ {cmd}")
        r = await client.call(ctx.bot_id, "POST", "/computer/exec", json={"cmd": cmd, "timeout": int(args.get("timeout_seconds") or 300)})
        if r.status_code != 200:
            return client.error(r)
        d = r.json()
        for line in d["output"].splitlines()[-40:]:
            await ctx.event("terminal", line)
        status = "TIMED OUT" if d.get("timed_out") else f"exit code {d['exit_code']}"
        ctx.runs.append({"command": f"computer: {cmd}", "exit_code": None if d.get("timed_out") else d["exit_code"],
                         "timed_out": bool(d.get("timed_out")), "output": d["output"][-8000:]})
        return f"[{status}, on your computer]\n{d['output'][-12000:]}".rstrip()

    async def upload(args: dict[str, Any], ctx: ToolContext) -> str:
        src = (ctx.workspace / str(args.get("path", ""))).resolve()
        if not src.is_relative_to(ctx.workspace.resolve()) or not src.is_file():
            return "ERROR: upload a file from your workspace (path relative to it)"
        dest = str(args.get("to") or src.name)
        r = await client.call(ctx.bot_id, "POST", "/computer/upload",
                              json={"path": dest, "content_b64": base64.b64encode(src.read_bytes()).decode()})
        return f"uploaded {src.name} to {r.json()['path']} on your computer" if r.status_code == 200 else client.error(r)

    async def download(args: dict[str, Any], ctx: ToolContext) -> str:
        path = str(args.get("path") or "")
        r = await client.call(ctx.bot_id, "GET", "/computer/download", params={"path": path})
        if r.status_code != 200:
            return client.error(r)
        dest = (ctx.workspace / str(args.get("to") or os.path.basename(path))).resolve()
        if not dest.is_relative_to(ctx.workspace.resolve()):
            return "ERROR: the destination must be inside your workspace"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.content)
        return f"downloaded {path} → {dest.name} ({len(r.content):,} bytes)"

    async def screenshot(args: dict[str, Any], ctx: ToolContext) -> str:
        r = await client.call(ctx.bot_id, "GET", "/computer/screenshot")
        if r.status_code != 200:
            return client.error(r)
        name = f"computer-{int(time.time())}.png"
        (ctx.workspace / name).write_bytes(r.content)
        return f"saved your computer's screen as {name}; look at it with describe_image"

    def _input(action: str):
        async def fn(args: dict[str, Any], ctx: ToolContext) -> str:
            r = await client.call(ctx.bot_id, "POST", "/computer/input", json={"action": action, **args})
            return f"{action} done" if r.status_code == 200 and r.json().get("ok") else client.error(r)
        return fn

    return [
        Tool("computer_start", "Turn on your own Linux computer on the server (desktop, Chromium, Python, Node, git, internet).",
             {"type": "object", "properties": {}}, "R1", start, timeout=180, path_arg=None),
        Tool("computer_stop", "Turn your computer off (your files are kept). It also stops by itself after 15 idle minutes.",
             {"type": "object", "properties": {}}, "R1", stop, timeout=60, path_arg=None),
        Tool("computer_run", "Run a bash command on your own computer (not the user's PC). Long jobs: set timeout_seconds (max 900).",
             {"type": "object", "properties": {"command": s, "timeout_seconds": i}, "required": ["command"]},
             "R1", run, timeout=960, path_arg=None, classify=lambda a, c: command_risk(a.get("command", ""))[0],
             summary=lambda a: f"🖥 computer_run: {str(a.get('command', ''))[:80]}"),
        Tool("computer_upload", "Copy a file from your workspace to your computer (to = a path under /home/bot).",
             {"type": "object", "properties": {"path": s, "to": s}, "required": ["path"]}, "R1", upload, timeout=180, path_arg=None),
        Tool("computer_download", "Copy a file from your computer (/home/bot/...) into your workspace.",
             {"type": "object", "properties": {"path": s, "to": s}, "required": ["path"]}, "R1", download, timeout=180, path_arg=None),
        Tool("computer_screenshot", "Save a screenshot of your computer's screen into your workspace.",
             {"type": "object", "properties": {}}, "R1", screenshot, timeout=60, path_arg=None),
        Tool("computer_click", "Click at (x, y) on your computer's screen (1280x800). Acts on live sites.",
             {"type": "object", "properties": {"x": i, "y": i, "button": i}, "required": ["x", "y"]}, "R3", _input("click"),
             timeout=40, path_arg=None, summary=lambda a: f"🖥 click ({a.get('x')}, {a.get('y')})"),
        Tool("computer_type", "Type text on your computer (into whatever has focus). Acts on live sites.",
             {"type": "object", "properties": {"text": s}, "required": ["text"]}, "R3", _input("type"),
             timeout=60, path_arg=None, summary=lambda a: f"🖥 type {len(str(a.get('text', '')))} chars"),
        Tool("computer_key", "Press a key or combo on your computer (Return, Tab, ctrl+l, ctrl+c...).",
             {"type": "object", "properties": {"key": s}, "required": ["key"]}, "R3", _input("key"),
             timeout=40, path_arg=None, summary=lambda a: f"🖥 key {a.get('key')}"),
    ]
