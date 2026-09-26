"""A10.f: the Bot Computers gateway's security logic, with a fake Docker backend
(the real Docker side is tested live on the VPS, A10.f.99)."""

from __future__ import annotations

import base64
import importlib.util
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


class FakeBackend:
    def __init__(self):
        self.up: dict[str, str] = {}
        self.files: dict[tuple[str, str], bytes] = {}
        self.cmds: list[tuple[str, list[str]]] = []
        self.n = 10

    def running(self):
        return list(self.up)

    def start(self, bot, vnc_password):
        self.pw = vnc_password
        if bot not in self.up:
            self.n += 1
            self.up[bot] = f"172.31.99.{self.n}"
        return self.up[bot]

    def stop(self, bot):
        self.up.pop(bot, None)

    def exec(self, bot, cmd, timeout):
        if bot not in self.up:
            raise RuntimeError("the computer is not running (start it first)")
        self.cmds.append((bot, cmd))
        if cmd[0] == "import":
            return 0, b"\x89PNG fake"
        return 0, f"ran {cmd[-1]} on {bot}".encode()

    def put(self, bot, path, data):
        self.files[(bot, path)] = data

    def get(self, bot, path):
        if (bot, path) not in self.files:
            raise FileNotFoundError(path)
        return self.files[(bot, path)]

    def ip(self, bot):
        return self.up.get(bot)


@pytest.fixture
def gw(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    spec = importlib.util.spec_from_file_location("botcomp_gateway", ROOT / "deploy/bot-computers/gateway/app.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["botcomp_gateway"] = mod
    spec.loader.exec_module(mod)
    from fastapi.testclient import TestClient
    fake = FakeBackend()
    client = TestClient(mod.create_app(fake))
    return mod, client, fake


def add_bot(mod, bot, capsys):
    assert mod.cli(["bots", "add", bot]) == 0
    return capsys.readouterr().out.strip()


def login(client, bot, secret):
    r = client.post("/auth/login", json={"bot": bot, "secret": secret})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_bot_only_logins(gw, capsys):
    mod, c, _ = gw
    secret = add_bot(mod, "scout", capsys)
    assert secret.startswith("bc_")
    assert c.post("/auth/login", json={"bot": "scout", "secret": "wrong"}).status_code == 401
    assert c.post("/auth/login", json={"bot": "nobody", "secret": secret}).status_code == 401
    assert c.get("/computer").status_code == 401                               # no token
    assert c.get("/computer", headers={"Authorization": "Bearer forged.token"}).status_code == 401
    h = login(c, "scout", secret)
    assert c.get("/computer", headers=h).json()["bot"] == "scout"
    assert "Bot logins only" in c.get("/").text                                 # the login page
    mod.cli(["bots", "revoke", "scout"])
    assert c.get("/computer", headers=h).status_code == 401                    # revoking kills live tokens


def test_each_bot_only_reaches_its_own_computer(gw, capsys):
    mod, c, fake = gw
    ha = login(c, "alpha", add_bot(mod, "alpha", capsys))
    hb = login(c, "beta", add_bot(mod, "beta", capsys))
    c.post("/computer/start", headers=ha)
    out = c.post("/computer/exec", headers=ha, json={"cmd": "whoami"}).json()
    assert out["exit_code"] == 0 and "on alpha" in out["output"]
    # beta's token runs on beta's computer, which isn't started: it can't reach alpha's
    assert c.post("/computer/exec", headers=hb, json={"cmd": "whoami"}).status_code == 409
    assert all(bot == "alpha" for bot, _ in fake.cmds)
    # screen links: a bot can only view its own screen
    assert c.post("/auth/screen-link", headers=hb, json={"bot": "alpha"}).status_code == 403
    link = c.post("/auth/screen-link", headers=ha, json={"bot": "alpha"}).json()["url"]
    assert "/screen/alpha/vnc.html?token=" in link and "view_only=true" in link
    assert f"password={fake.pw}" in link and len(fake.pw) == 8               # the computer's own screen password
    view = link.split("token=")[1].split("&")[0]
    assert mod.read_token(view, ("view",)) == "alpha"
    assert mod.read_token(view, ("bot",)) is None                              # a view link can't drive the API


def test_owner_key_mints_screen_links_for_any_bot(gw, capsys):
    mod, c, _ = gw
    ha = login(c, "alpha", add_bot(mod, "alpha", capsys))
    assert mod.cli(["owner", "add", "omnibots"]) == 0
    key = capsys.readouterr().out.strip()
    stopped = c.post("/auth/screen-link", headers={"X-Owner-Key": key}, json={"bot": "alpha"})
    assert stopped.status_code == 409                                          # nothing to show yet
    c.post("/computer/start", headers=ha)
    r = c.post("/auth/screen-link", headers={"X-Owner-Key": key}, json={"bot": "alpha", "view_only": False})
    assert r.status_code == 200 and "view_only=false" in r.json()["url"]
    assert c.post("/auth/screen-link", headers={"X-Owner-Key": "bo_wrong"}, json={"bot": "alpha"}).status_code == 401
    assert c.post("/auth/screen-link", headers={"X-Owner-Key": key}, json={"bot": "ghost"}).status_code == 404


def test_cap_on_running_computers(gw, capsys):
    mod, c, _ = gw
    hs = [login(c, b, add_bot(mod, b, capsys)) for b in ("a1", "a2", "a3")]
    assert c.post("/computer/start", headers=hs[0]).status_code == 200
    assert c.post("/computer/start", headers=hs[1]).status_code == 200
    r = c.post("/computer/start", headers=hs[2])
    assert r.status_code == 429 and "limit is 2" in r.json()["detail"]
    assert c.post("/computer/start", headers=hs[0]).status_code == 200         # restarting your own is fine
    c.post("/computer/stop", headers=hs[1])
    assert c.post("/computer/start", headers=hs[2]).status_code == 200


def test_files_stay_in_the_home_folder(gw, capsys):
    mod, c, fake = gw
    h = login(c, "alpha", add_bot(mod, "alpha", capsys))
    c.post("/computer/start", headers=h)
    ok = c.post("/computer/upload", headers=h, json={"path": "site/index.html", "content_b64": base64.b64encode(b"<h1>hi</h1>").decode()})
    assert ok.status_code == 200 and ok.json()["path"] == "/home/bot/site/index.html"
    assert c.get("/computer/download", headers=h, params={"path": "site/index.html"}).content == b"<h1>hi</h1>"
    for bad in ("../../etc/passwd", "/etc/shadow", "/home/bot/../root/.ssh/id_rsa", "/home/botnet/x"):
        assert c.post("/computer/upload", headers=h, json={"path": bad, "content_b64": ""}).status_code == 400, bad
        assert c.get("/computer/download", headers=h, params={"path": bad}).status_code == 400, bad


def test_screenshot_and_input(gw, capsys):
    mod, c, fake = gw
    h = login(c, "alpha", add_bot(mod, "alpha", capsys))
    c.post("/computer/start", headers=h)
    assert c.get("/computer/screenshot", headers=h).content.startswith(b"\x89PNG")
    assert c.post("/computer/input", headers=h, json={"action": "click", "x": 10, "y": 20}).json()["ok"]
    assert c.post("/computer/input", headers=h, json={"action": "key", "key": "ctrl+l"}).json()["ok"]
    assert c.post("/computer/input", headers=h, json={"action": "key", "key": "a; rm -rf /"}).status_code == 400
    assert c.post("/computer/input", headers=h, json={"action": "rm"}).status_code == 400
    typed = [cmd for _, cmd in fake.cmds if cmd[:2] == ["xdotool", "type"]]
    c.post("/computer/input", headers=h, json={"action": "type", "text": "$(reboot)"})
    typed = [cmd for _, cmd in fake.cmds if cmd[:2] == ["xdotool", "type"]]
    assert typed[-1][-1] == "$(reboot)" and typed[-1][-2] == "--"             # passed as an argument, never a shell


def test_idle_computers_are_stopped(gw, capsys):
    mod, c, fake = gw
    h1 = login(c, "a1", add_bot(mod, "a1", capsys))
    h2 = login(c, "a2", add_bot(mod, "a2", capsys))
    c.post("/computer/start", headers=h1)
    c.post("/computer/start", headers=h2)
    activity = c.app.state.gw["activity"]
    activity["a1"] = time.time() - mod.IDLE_MIN * 60 - 5                       # a1 went quiet
    assert mod.reap_idle(fake, activity) == ["a1"]
    assert fake.running() == ["a2"]


def test_omnibots_tools_and_screen_button_drive_the_real_gateway(gw, capsys, tmp_path):
    """OmniBots' client + tools against the real gateway code (in-process; only Docker is fake)."""
    import asyncio

    import httpx

    from omnibots.runtime.computer_tools import ComputerClient, computer_tools
    from omnibots.runtime.tools import ToolContext
    from omnibots.ui.computer_view import screen_link
    mod, c, fake = gw
    secrets_ = {"coder": add_bot(mod, "coder", capsys)}
    transport = httpx.ASGITransport(app=c.app)
    client = ComputerClient(secrets_.get, base_url="http://gw",
                            client_factory=lambda: httpx.AsyncClient(transport=transport, base_url="http://gw"))
    tools = {t.name: t for t in computer_tools(client)}
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "app.py").write_text("print('hi')\n", encoding="utf-8")
    ctx = ToolContext(bot_id="coder", workspace=ws)

    async def go():
        out = {"start": await tools["computer_start"].fn({}, ctx)}
        out["run"] = await tools["computer_run"].fn({"command": "python3 app.py"}, ctx)
        out["up"] = await tools["computer_upload"].fn({"path": "app.py", "to": "proj/app.py"}, ctx)
        out["down"] = await tools["computer_download"].fn({"path": "proj/app.py", "to": "back.py"}, ctx)
        out["shot"] = await tools["computer_screenshot"].fn({}, ctx)
        out["click"] = await tools["computer_click"].fn({"x": 5, "y": 6}, ctx)
        out["link"] = await screen_link(client, "coder", view_only=False)
        stranger = ComputerClient(lambda b: None, base_url="http://gw",
                                  client_factory=lambda: httpx.AsyncClient(transport=transport, base_url="http://gw"))
        try:
            await stranger.call("coder", "GET", "/computer")
            out["stranger"] = "got in"
        except RuntimeError as exc:
            out["stranger"] = str(exc)
        return out
    o = asyncio.run(go())
    assert o["start"].startswith("your computer is on")
    assert "[exit code 0, on your computer]" in o["run"] and "ran python3 app.py on coder" in o["run"]
    assert ctx.runs[-1]["command"] == "computer: python3 app.py"             # real-run evidence for claims (A4.a.08)
    assert "/home/bot/proj/app.py" in o["up"] and (ws / "back.py").read_text(encoding="utf-8") == "print('hi')\n"
    assert o["shot"].startswith("saved your computer's screen") and o["click"] == "click done"
    assert "/screen/coder/vnc.html" in o["link"] and "view_only=false" in o["link"]
    assert "no computer login yet" in o["stranger"]
    assert tools["computer_run"].risk_for({"command": "pip install requests"}, ctx) == "R3"
    assert tools["computer_run"].risk_for({"command": "rm -rf /home/bot"}, ctx) == "R5"
    assert tools["computer_click"].risk == "R3"


def test_owner_provisions_bot_logins_automatically(gw, capsys):
    """A new bot with no login: OmniBots (owner key) creates one, stores it, and the bot logs in."""
    import asyncio

    import httpx

    from omnibots.runtime.computer_tools import ComputerClient
    mod, c, _ = gw
    assert mod.cli(["owner", "add", "omnibots"]) == 0
    key = capsys.readouterr().out.strip()
    vault: dict[str, str] = {}

    async def store(bot, secret):
        vault[f"computer_{bot}"] = secret
    transport = httpx.ASGITransport(app=c.app)
    client = ComputerClient(lambda bot: vault.get(f"computer_{bot}"), base_url="http://gw",
                            client_factory=lambda: httpx.AsyncClient(transport=transport, base_url="http://gw"),
                            owner_key=lambda: key, store_secret=store)

    async def go():
        first = await client.call("newbie", "GET", "/computer")
        again = await client.call("newbie", "GET", "/computer")                # uses the stored login, no re-provision
        return first, again
    first, again = asyncio.run(go())
    assert first.status_code == 200 and first.json()["bot"] == "newbie" and again.status_code == 200
    assert list(vault) == ["computer_newbie"] and vault["computer_newbie"].startswith("bc_")
    assert c.post("/admin/bots", json={"bot": "x"}).status_code == 401          # owner key required
    assert c.post("/admin/bots", headers={"X-Owner-Key": key}, json={"bot": "bad id!"}).status_code == 400
    assert c.post("/admin/bots/revoke", headers={"X-Owner-Key": key}, json={"bot": "newbie"}).json()["revoked"]


def test_egress_log_is_attributed_to_bots(gw, capsys, tmp_path):
    mod, c, _ = gw
    ha = login(c, "alpha", add_bot(mod, "alpha", capsys))
    hb = login(c, "beta", add_bot(mod, "beta", capsys))
    c.post("/computer/start", headers=ha)                                      # 172.31.99.11
    c.post("/computer/start", headers=hb)                                      # 172.31.99.12
    log = tmp_path / "access.log"
    log.write_text(
        "1727345740.321    120 172.31.99.11 TCP_TUNNEL/200 5230 CONNECT pypi.org:443 - HIER_DIRECT/151.101.0.223 -\n"
        "1727345741.002     30 172.31.99.12 TCP_MISS/200 1256 GET http://example.com/ - HIER_DIRECT/93.184.215.14 text/html\n"
        "1727345742.500      0 172.31.99.12 TCP_DENIED/403 3902 CONNECT 173.212.202.219:5432 - HIER_NONE/- text/html\n",
        encoding="utf-8")
    entries = mod.read_egress(None, 50, path=str(log))
    assert [(e["bot"], e["target"], e["blocked"]) for e in entries] == [
        ("alpha", "pypi.org:443", False), ("beta", "http://example.com/", False), ("beta", "173.212.202.219:5432", True)]
    assert [e["bot"] for e in mod.read_egress("beta", 50, path=str(log))] == ["beta", "beta"]
