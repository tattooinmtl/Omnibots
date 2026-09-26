"""Bot Computers gateway (PLAN.md A10.f): each OmniBots bot gets its own Linux desktop in a
Docker container on the VPS, used over HTTPS, never SSH.

Security model
  - This gateway is the ONLY thing that talks to Docker. The Docker API is never exposed
    (controlling it = root on the server). Bots get a narrow API for their OWN computer.
  - Bot-only logins: POST /auth/login {bot, secret} -> a 1-hour token bound to that bot.
    Secrets are stored hashed. The owner key (OmniBots itself) can mint short-lived screen
    links for any bot so the user can watch or take over.
  - Computers: gVisor runtime, 1 GB / 1 CPU / 256 processes each, at most MAX_RUNNING at
    once, stopped after IDLE_MIN idle minutes, on an internal network whose only way out is
    a logging proxy (every host a bot reaches is recorded).

Admin (on the VPS):
  docker exec bot-computers python app.py bots add <bot_id>     # prints the bot's secret ONCE
  docker exec bot-computers python app.py bots list | revoke <bot_id>
  docker exec bot-computers python app.py owner add <name>       # prints an owner key ONCE
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import sqlite3
import sys
import tarfile
import threading
import time
from contextlib import closing
from pathlib import PurePosixPath
from typing import Any, Protocol

# Module level on purpose: with `from __future__ import annotations` FastAPI resolves type names
# (Request, WebSocket) from the module globals; imported inside create_app they'd become query params.
from fastapi import FastAPI, Header, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, Response

DATA = os.environ.get("DATA_DIR", "/data")
DB = os.path.join(DATA, "gateway.sqlite")
MAX_RUNNING = int(os.environ.get("MAX_RUNNING", "2"))
IDLE_MIN = float(os.environ.get("IDLE_MIN", "15"))
TOKEN_TTL = int(os.environ.get("TOKEN_TTL", "3600"))
VIEW_TTL = int(os.environ.get("VIEW_TTL", "600"))
HOME = "/home/bot"
BOT_RE = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "https://computers.globalwarningnetworks.com").rstrip("/")

_lock = threading.Lock()


# ── storage ─────────────────────────────────────────────────────────────────
def db() -> sqlite3.Connection:
    os.makedirs(DATA, exist_ok=True)
    con = sqlite3.connect(DB, timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS bots (bot TEXT PRIMARY KEY, secret_hash TEXT NOT NULL, created REAL, revoked REAL);
        CREATE TABLE IF NOT EXISTS owners (name TEXT PRIMARY KEY, key_hash TEXT NOT NULL, created REAL, revoked REAL);
        CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, ts REAL, bot TEXT, action TEXT, detail TEXT);
        CREATE TABLE IF NOT EXISTS ips (ip TEXT, bot TEXT, since REAL);
        CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
    """)
    return con


def _h(x: str) -> str:
    return hashlib.sha256(x.encode()).hexdigest()


def server_key() -> bytes:
    with _lock, closing(db()) as con:
        row = con.execute("SELECT v FROM meta WHERE k='signing_key'").fetchone()
        if row:
            return bytes.fromhex(row[0])
        k = secrets.token_bytes(32)
        con.execute("INSERT INTO meta (k, v) VALUES ('signing_key', ?)", (k.hex(),))
        con.commit()
        return k


def audit(bot: str | None, action: str, detail: Any = "") -> None:
    with _lock, closing(db()) as con:
        con.execute("INSERT INTO audit (ts, bot, action, detail) VALUES (?,?,?,?)",
                    (time.time(), bot, action, detail if isinstance(detail, str) else json.dumps(detail)[:2000]))
        con.commit()


# ── tokens: HMAC-signed, bound to one bot and one kind ──────────────────────
def make_token(bot: str, kind: str, ttl: int) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"b": bot, "k": kind, "e": int(time.time()) + ttl}).encode()).decode().rstrip("=")
    sig = hmac.new(server_key(), body.encode(), hashlib.sha256).hexdigest()[:40]
    return f"{body}.{sig}"


def read_token(token: str | None, kinds: tuple[str, ...]) -> str | None:
    """The bot the token is for, or None (bad signature, expired, wrong kind, revoked bot)."""
    if not token or "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sig, hmac.new(server_key(), body.encode(), hashlib.sha256).hexdigest()[:40]):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except ValueError:
        return None
    if data.get("k") not in kinds or data.get("e", 0) < time.time():
        return None
    with _lock, closing(db()) as con:
        row = con.execute("SELECT revoked FROM bots WHERE bot=?", (data["b"],)).fetchone()
    if not row or row[0]:
        return None
    return data["b"]


def check_bot_secret(bot: str, secret: str) -> bool:
    with _lock, closing(db()) as con:
        row = con.execute("SELECT secret_hash, revoked FROM bots WHERE bot=?", (bot,)).fetchone()
    return bool(row) and not row[1] and hmac.compare_digest(row[0], _h(secret))


def check_owner(key: str | None) -> bool:
    if not key:
        return False
    with _lock, closing(db()) as con:
        rows = con.execute("SELECT key_hash FROM owners WHERE revoked IS NULL").fetchall()
    return any(hmac.compare_digest(r[0], _h(key)) for r in rows)


def safe_path(p: str) -> str:
    """A path inside the bot's home, or ValueError (no escaping with .. or absolute paths elsewhere)."""
    pp = PurePosixPath(p if p.startswith("/") else f"{HOME}/{p}")
    parts = []
    for part in pp.parts[1:]:
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise ValueError("path escapes the home folder")
            parts.pop()
            continue
        parts.append(part)
    full = "/" + "/".join(parts)
    if not (full == HOME or full.startswith(HOME + "/")):
        raise ValueError(f"only paths under {HOME} are allowed")
    return full


# ── the Docker side (swappable, so the security logic is testable without Docker) ─
class Backend(Protocol):
    def running(self) -> list[str]: ...
    def start(self, bot: str, vnc_password: str) -> str: ...  # returns the container's IP
    def stop(self, bot: str) -> None: ...
    def exec(self, bot: str, cmd: list[str], timeout: float) -> tuple[int, bytes]: ...
    def put(self, bot: str, path: str, data: bytes) -> None: ...
    def get(self, bot: str, path: str) -> bytes: ...
    def ip(self, bot: str) -> str | None: ...


class DockerBackend:
    IMAGE = os.environ.get("DESKTOP_IMAGE", "omnibots-desktop:1")
    NETWORK = os.environ.get("BOT_NETWORK", "botnet")
    RUNTIME = os.environ.get("BOT_RUNTIME", "runsc")          # gVisor
    PROXY = os.environ.get("BOT_PROXY", "http://egress-proxy:8888")

    def __init__(self):
        import docker
        self.dk = docker.from_env()

    def _name(self, bot: str) -> str:
        return f"botcomp-{bot}"

    def _get(self, bot: str):
        import docker
        try:
            return self.dk.containers.get(self._name(bot))
        except docker.errors.NotFound:
            return None

    def running(self) -> list[str]:
        return [c.labels.get("omnibots.bot") for c in self.dk.containers.list(filters={"label": "omnibots.bot"})]

    def start(self, bot: str, vnc_password: str) -> str:
        c = self._get(bot)
        if c is not None and c.status != "running":
            c.remove()                                   # a fresh container each start (new screen password)
            c = None
        if c is None:
            env = {"HTTP_PROXY": self.PROXY, "HTTPS_PROXY": self.PROXY, "http_proxy": self.PROXY,
                   "https_proxy": self.PROXY, "NO_PROXY": "localhost,127.0.0.1", "DISPLAY": ":1",
                   "VNC_PASSWORD": vnc_password}
            c = self.dk.containers.create(
                self.IMAGE, name=self._name(bot), hostname=f"{bot}-pc", labels={"omnibots.bot": bot},
                runtime=self.RUNTIME or None, network=self.NETWORK, environment=env,
                mem_limit="1g", memswap_limit="1g", nano_cpus=1_000_000_000, pids_limit=256, shm_size="256m",
                security_opt=["no-new-privileges"], cap_drop=["ALL"],
                volumes={f"botcomp-{bot}-home": {"bind": HOME, "mode": "rw"}},
                tmpfs={"/tmp": "size=512m", "/run": "size=16m"}, restart_policy={"Name": "no"})
        if c.status != "running":
            c.start()
        c.reload()
        return c.attrs["NetworkSettings"]["Networks"][self.NETWORK]["IPAddress"]

    def stop(self, bot: str) -> None:
        c = self._get(bot)
        if c is not None:
            c.stop(timeout=10)

    def exec(self, bot: str, cmd: list[str], timeout: float) -> tuple[int, bytes]:
        c = self._get(bot)
        if c is None or c.status != "running":
            raise RuntimeError("the computer is not running (start it first)")
        full = ["timeout", "--kill-after=5", str(int(timeout)), *cmd]
        r = c.exec_run(full, user="bot", workdir=HOME, environment={"DISPLAY": ":1", "HOME": HOME})
        return r.exit_code, r.output or b""

    def put(self, bot: str, path: str, data: bytes) -> None:
        c = self._get(bot)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            info = tarfile.TarInfo(PurePosixPath(path).name)
            info.size, info.uid, info.gid, info.mode = len(data), 1000, 1000, 0o644
            tf.addfile(info, io.BytesIO(data))
        self.exec(bot, ["mkdir", "-p", str(PurePosixPath(path).parent)], 30)
        c.put_archive(str(PurePosixPath(path).parent), buf.getvalue())

    def get(self, bot: str, path: str) -> bytes:
        c = self._get(bot)
        stream, _ = c.get_archive(path)
        buf = io.BytesIO(b"".join(stream))
        with tarfile.open(fileobj=buf) as tf:
            m = tf.next()
            if m is None or not m.isfile():
                raise FileNotFoundError(path)
            return tf.extractfile(m).read()

    def ip(self, bot: str) -> str | None:
        c = self._get(bot)
        if c is None or c.status != "running":
            return None
        return c.attrs["NetworkSettings"]["Networks"][self.NETWORK]["IPAddress"]


# ── the API ─────────────────────────────────────────────────────────────────
def create_app(backend: Backend | None = None):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(_app):
        async def reaper():                               # stop computers nobody used for IDLE_MIN minutes
            while True:
                await asyncio.sleep(60)
                await asyncio.to_thread(reap_idle, be(), state["activity"])
        task = asyncio.create_task(reaper())
        yield
        task.cancel()

    app = FastAPI(title="OmniBots · Bot Computers", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    state: dict[str, Any] = {"backend": backend, "activity": {}, "vnc": {}}

    def be() -> Backend:
        if state["backend"] is None:
            state["backend"] = DockerBackend()
        return state["backend"]

    def touch(bot: str) -> None:
        state["activity"][bot] = time.time()

    def bot_from(authorization: str | None) -> str:
        tok = authorization[7:] if authorization and authorization.lower().startswith("bearer ") else None
        bot = read_token(tok, ("bot",))
        if not bot:
            raise HTTPException(401, "log in first: POST /auth/login {bot, secret}")
        touch(bot)
        return bot

    @app.get("/", response_class=HTMLResponse)
    def login_page():
        return LOGIN_PAGE

    @app.post("/auth/login")
    async def login(req: Request):
        body = await req.json()
        bot, secret = str(body.get("bot", "")), str(body.get("secret", ""))
        if not BOT_RE.match(bot) or not check_bot_secret(bot, secret):
            audit(bot if BOT_RE.match(bot) else None, "login_failed")
            raise HTTPException(401, "wrong bot or secret")
        audit(bot, "login")
        return {"token": make_token(bot, "bot", TOKEN_TTL), "expires_in": TOKEN_TTL, "bot": bot}

    @app.post("/auth/screen-link")
    async def screen_link(req: Request, authorization: str | None = Header(None), x_owner_key: str | None = Header(None)):
        body = await req.json()
        target = str(body.get("bot", ""))
        if check_owner(x_owner_key):
            who = "owner"
        else:
            who = bot_from(authorization)
            if who != target:
                raise HTTPException(403, "a bot can only view its own screen")
        if not bot_exists(target):
            raise HTTPException(404, "no such bot")
        pw = state["vnc"].get(target)
        if not pw or target not in be().running():
            raise HTTPException(409, "the computer is not running (start it first)")
        view = make_token(target, "view", VIEW_TTL)
        audit(target, "screen_link", {"by": who})
        return {"url": f"{PUBLIC_URL}/screen/{target}/vnc.html?token={view}&path=screen/{target}/websockify%3Ftoken%3D{view}"
                       f"&password={pw}&autoconnect=1&resize=scale&reconnect=1"
                       f"&view_only={str(bool(body.get('view_only', True))).lower()}",
                "expires_in": VIEW_TTL}

    @app.get("/computer")
    def status(authorization: str | None = Header(None)):
        bot = bot_from(authorization)
        return {"bot": bot, "running": bot in be().running(), "running_total": len(be().running()), "max": MAX_RUNNING}

    @app.post("/computer/start")
    def start(authorization: str | None = Header(None)):
        bot = bot_from(authorization)
        running = be().running()
        if bot not in running and len(running) >= MAX_RUNNING:
            raise HTTPException(429, f"{len(running)} bot computers are already running (the limit is {MAX_RUNNING}); try again later")
        if bot not in running:
            state["vnc"][bot] = secrets.token_urlsafe(6)[:8]          # VNC passwords are 8 chars max
        ip = be().start(bot, state["vnc"].setdefault(bot, secrets.token_urlsafe(6)[:8]))
        with _lock, closing(db()) as con:
            con.execute("INSERT INTO ips (ip, bot, since) VALUES (?,?,?)", (ip, bot, time.time()))
            con.commit()
        audit(bot, "start", {"ip": ip})
        return {"started": True, "bot": bot}

    @app.post("/computer/stop")
    def stop(authorization: str | None = Header(None)):
        bot = bot_from(authorization)
        be().stop(bot)
        audit(bot, "stop")
        return {"stopped": True}

    @app.post("/computer/exec")
    async def run(req: Request, authorization: str | None = Header(None)):
        bot = bot_from(authorization)
        body = await req.json()
        cmd = body.get("cmd")
        if isinstance(cmd, str):
            cmd = ["bash", "-lc", cmd]
        if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) for x in cmd):
            raise HTTPException(400, "cmd must be a string or a list of strings")
        timeout = max(1.0, min(float(body.get("timeout", 120)), 900))
        try:
            code, out = await asyncio.to_thread(be().exec, bot, cmd, timeout)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        audit(bot, "exec", {"cmd": cmd[-1][:300], "exit": code})
        return {"exit_code": code, "output": out.decode("utf-8", "replace")[-60000:], "timed_out": code == 124}

    @app.post("/computer/upload")
    async def upload(req: Request, authorization: str | None = Header(None)):
        bot = bot_from(authorization)
        body = await req.json()
        try:
            path = safe_path(str(body.get("path", "")))
            data = base64.b64decode(body.get("content_b64", ""))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        if len(data) > 50 * 1024 * 1024:
            raise HTTPException(413, "files are limited to 50 MB")
        await asyncio.to_thread(be().put, bot, path, data)
        audit(bot, "upload", {"path": path, "bytes": len(data)})
        return {"path": path, "bytes": len(data)}

    @app.get("/computer/download")
    async def download(path: str, authorization: str | None = Header(None)):
        bot = bot_from(authorization)
        try:
            full = safe_path(path)
            data = await asyncio.to_thread(be().get, bot, full)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except FileNotFoundError:
            raise HTTPException(404, "no such file")
        return Response(data, media_type="application/octet-stream")

    @app.get("/computer/screenshot")
    async def screenshot(authorization: str | None = Header(None)):
        bot = bot_from(authorization)
        code, png = await asyncio.to_thread(be().exec, bot, ["import", "-window", "root", "-display", ":1", "png:-"], 30)
        if code != 0 or not png.startswith(b"\x89PNG"):
            raise HTTPException(500, "screenshot failed")
        return Response(png, media_type="image/png")

    @app.post("/computer/input")
    async def user_input(req: Request, authorization: str | None = Header(None)):
        bot = bot_from(authorization)
        b = await req.json()
        act = b.get("action")
        if act == "click":
            cmd = ["xdotool", "mousemove", str(int(b["x"])), str(int(b["y"])), "click", str(int(b.get("button", 1)))]
        elif act == "move":
            cmd = ["xdotool", "mousemove", str(int(b["x"])), str(int(b["y"]))]
        elif act == "type":
            cmd = ["xdotool", "type", "--delay", "20", "--", str(b.get("text", ""))[:5000]]
        elif act == "key":
            key = str(b.get("key", ""))
            if not re.match(r"^[A-Za-z0-9_+]{1,40}$", key):
                raise HTTPException(400, "bad key name (e.g. Return, ctrl+l, Tab)")
            cmd = ["xdotool", "key", key]
        else:
            raise HTTPException(400, "action must be click, move, type or key")
        code, out = await asyncio.to_thread(be().exec, bot, cmd, 30)
        audit(bot, "input", {"action": act})
        return {"ok": code == 0, "output": out.decode("utf-8", "replace")[-500:]}

    @app.post("/admin/bots")
    async def provision(req: Request, x_owner_key: str | None = Header(None)):
        """Owner only (OmniBots itself): create or rotate a bot's computer login. The secret is
        returned ONCE; OmniBots stores it in its vault and the bot logs in with it."""
        if not check_owner(x_owner_key):
            raise HTTPException(401, "owner key required")
        bot = str((await req.json()).get("bot", ""))
        if not BOT_RE.match(bot):
            raise HTTPException(400, "bot ids are 1-40 letters, digits, _ or -")
        secret = "bc_" + secrets.token_urlsafe(32)
        with _lock, closing(db()) as con:
            con.execute("INSERT OR REPLACE INTO bots (bot, secret_hash, created, revoked) VALUES (?,?,?,NULL)",
                        (bot, _h(secret), time.time()))
            con.commit()
        audit(bot, "provisioned")
        return {"bot": bot, "secret": secret}

    @app.post("/admin/bots/revoke")
    async def revoke(req: Request, x_owner_key: str | None = Header(None)):
        if not check_owner(x_owner_key):
            raise HTTPException(401, "owner key required")
        bot = str((await req.json()).get("bot", ""))
        with _lock, closing(db()) as con:
            n = con.execute("UPDATE bots SET revoked=? WHERE bot=? AND revoked IS NULL", (time.time(), bot)).rowcount
            con.commit()
        if bot in be().running():
            be().stop(bot)
        audit(bot, "revoked")
        return {"revoked": bool(n)}

    @app.get("/egress")
    def egress(x_owner_key: str | None = Header(None), bot: str | None = None, limit: int = 200):
        """Owner only: the hosts each bot reached (from the proxy log)."""
        if not check_owner(x_owner_key):
            raise HTTPException(401, "owner key required")
        return {"entries": read_egress(bot, limit)}

    # ── the live screen (noVNC): static files + the websocket, behind a view token ──
    async def _screen_bot(bot: str, token: str | None) -> str:
        if read_token(token, ("view",)) != bot:
            raise HTTPException(401, "this screen link expired or is for another bot")
        ip = await asyncio.to_thread(be().ip, bot)
        if not ip:
            raise HTTPException(409, "the computer is not running")
        touch(bot)
        return ip

    @app.get("/screen/{bot}/{path:path}")
    async def screen_file(bot: str, path: str, token: str | None = None):
        import httpx
        ip = await _screen_bot(bot, token)
        if ".." in path:
            raise HTTPException(400, "bad path")
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"http://{ip}:6080/{path}")
        return Response(r.content, status_code=r.status_code, media_type=r.headers.get("content-type"))

    @app.websocket("/screen/{bot}/websockify")
    async def screen_ws(ws: WebSocket, bot: str, token: str | None = None):
        try:
            ip = await _screen_bot(bot, token)
        except HTTPException:
            await ws.close(code=4401)
            return
        import websockets
        await ws.accept(subprotocol="binary")
        async with websockets.connect(f"ws://{ip}:6080/websockify", subprotocols=["binary"], max_size=None) as up:
            async def down():
                async for msg in up:
                    await ws.send_bytes(msg if isinstance(msg, bytes) else msg.encode())

            async def upward():
                while True:
                    m = await ws.receive()
                    if m.get("type") == "websocket.disconnect":
                        break
                    data = m.get("bytes") or (m.get("text") or "").encode()
                    await up.send(data)
                    touch(bot)
            tasks = [asyncio.create_task(down()), asyncio.create_task(upward())]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in tasks:
                t.cancel()

    app.state.gw = state
    return app


def reap_idle(backend: Backend, activity: dict[str, float], now: float | None = None) -> list[str]:
    """Stop computers nobody used for IDLE_MIN minutes. Returns the bots stopped."""
    now = now or time.time()
    stopped = []
    for bot in backend.running():
        if now - activity.get(bot, 0) > IDLE_MIN * 60:
            backend.stop(bot)
            audit(bot, "idle_stop")
            stopped.append(bot)
    return stopped


EGRESS_LOG = os.environ.get("EGRESS_LOG", "/proxylog/access.log")
# squid access.log (native format), one line per request:
#   1727345740.321    120 172.31.99.11 TCP_TUNNEL/200 5230 CONNECT pypi.org:443 - HIER_DIRECT/151.101.0.223 -
#   1727345741.002      0 172.31.99.12 TCP_DENIED/403 3902 CONNECT 10.0.0.5:443 - HIER_NONE/- text/html
_SQUID = re.compile(r"^(\d+)\.\d+\s+\d+\s+(\d+\.\d+\.\d+\.\d+)\s+(\S+)/(\d+)\s+\d+\s+([A-Z]+)\s+(\S+)")


def read_egress(bot: str | None, limit: int, path: str | None = None) -> list[dict[str, str]]:
    """Every request the proxy saw, attributed to the bot whose computer had that IP."""
    path = path or EGRESS_LOG
    if not os.path.exists(path):
        return []
    with _lock, closing(db()) as con:
        ip_bot = dict(con.execute("SELECT ip, bot FROM ips ORDER BY since").fetchall())   # later starts win
    out: list[dict[str, str]] = []
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()[-20000:]
    for line in lines:
        m = _SQUID.match(line)
        if not m:
            continue
        who = ip_bot.get(m.group(2), "?")
        if bot and who != bot:
            continue
        out.append({"time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(m.group(1)))), "bot": who,
                    "method": m.group(5), "target": m.group(6)[:300], "status": m.group(4),
                    "blocked": "DENIED" in m.group(3)})
    return out[-limit:]


def bot_exists(bot: str) -> bool:
    with _lock, closing(db()) as con:
        row = con.execute("SELECT revoked FROM bots WHERE bot=?", (bot,)).fetchone()
    return bool(row) and not row[0]


LOGIN_PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>OmniBots · Bot Computers</title><style>
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:radial-gradient(circle at 20% 10%,#16307a55,transparent 45%),#070b18;color:#e7edfb;font:15px "Segoe UI",system-ui,sans-serif}
.card{width:min(92vw,420px);background:#0b1226;border:1px solid #22345f;border-radius:18px;padding:28px;box-shadow:0 0 40px #2f7dff22}
h1{font-size:21px;margin:0 0 4px}p{color:#8b97b8;margin:0 0 18px}label{display:block;font-size:13px;color:#8b97b8;margin:12px 0 6px}
input{width:100%;box-sizing:border-box;background:#111a35;border:1px solid #22345f;border-radius:10px;color:#e7edfb;padding:10px 12px;font-size:15px}
input:focus{outline:none;border-color:#2f7dff}button{margin-top:18px;width:100%;background:#2f7dff;border:0;border-radius:10px;color:#fff;padding:11px;font-size:15px;font-weight:600;cursor:pointer}
#out{margin-top:16px;font-size:13px;color:#8b97b8;white-space:pre-wrap}.row{display:flex;gap:8px}.row button{margin-top:10px}
iframe{width:100%;height:60vh;border:1px solid #22345f;border-radius:12px;margin-top:14px;background:#000}</style></head><body>
<div class="card" id="card"><h1>🖥 Bot Computers</h1><p>Bot logins only. Each bot signs in to its own computer.</p>
<form id="f"><label>Bot id</label><input id="bot" autocomplete="username" required>
<label>Bot secret</label><input id="secret" type="password" autocomplete="current-password" required>
<button>Sign in</button></form><div id="out"></div></div>
<script>
const $=id=>document.getElementById(id);let tok=null,bot=null;
async function api(p,o={}){o.headers={...(o.headers||{}),'Content-Type':'application/json',...(tok?{Authorization:'Bearer '+tok}:{})};const r=await fetch(p,o);const j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.detail||r.status);return j}
$('f').onsubmit=async e=>{e.preventDefault();try{const j=await api('/auth/login',{method:'POST',body:JSON.stringify({bot:$('bot').value,secret:$('secret').value})});tok=j.token;bot=j.bot;dash()}catch(x){$('out').textContent='✖ '+x.message}};
async function dash(){const s=await api('/computer');$('card').innerHTML=`<h1>🖥 ${bot}'s computer</h1><p>${s.running?'Running':'Stopped'} · ${s.running_total}/${s.max} computers in use</p>
<div class="row"><button id="go">${s.running?'Open screen':'Start computer'}</button><button id="st" style="background:#16224a">Stop</button></div><div id="out"></div><div id="scr"></div>`;
$('go').onclick=async()=>{try{if(!(await api('/computer')).running){await api('/computer/start',{method:'POST'});}const l=await api('/auth/screen-link',{method:'POST',body:JSON.stringify({bot,view_only:false})});$('scr').innerHTML=`<iframe src="${l.url}"></iframe>`}catch(x){$('out').textContent='✖ '+x.message}};
$('st').onclick=async()=>{await api('/computer/stop',{method:'POST'});dash()}}
</script></body></html>"""


# ── admin CLI ───────────────────────────────────────────────────────────────
def cli(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "bots":
        with closing(db()) as con:
            if argv[1] == "add" and len(argv) >= 3 and BOT_RE.match(argv[2]):
                secret = "bc_" + secrets.token_urlsafe(32)
                con.execute("INSERT OR REPLACE INTO bots (bot, secret_hash, created, revoked) VALUES (?,?,?,NULL)",
                            (argv[2], _h(secret), time.time()))
                con.commit()
                print(secret)
                return 0
            if argv[1] == "list":
                for b, c, r in con.execute("SELECT bot, created, revoked FROM bots ORDER BY bot"):
                    print(f"{b:24} {'revoked' if r else 'active'}")
                return 0
            if argv[1] == "revoke" and len(argv) >= 3:
                n = con.execute("UPDATE bots SET revoked=? WHERE bot=?", (time.time(), argv[2])).rowcount
                con.commit()
                print("revoked" if n else "no such bot")
                return 0
    if len(argv) >= 3 and argv[:2] == ["owner", "add"]:
        key = "bo_" + secrets.token_urlsafe(32)
        with closing(db()) as con:
            con.execute("INSERT OR REPLACE INTO owners (name, key_hash, created, revoked) VALUES (?,?,?,NULL)",
                        (argv[2], _h(key), time.time()))
            con.commit()
        print(key)
        return 0
    print(__doc__)
    return 2


app = None if __name__ == "__main__" else create_app()

if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
