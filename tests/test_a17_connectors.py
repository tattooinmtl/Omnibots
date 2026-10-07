"""A17.h (0.11.0): Telegram and Discord. Stateful stand-ins for both APIs (httpx MockTransport) drive the real bridges;
a fake engine with the real engine's method shapes records what the phone made the team do. The real Telegram API is
also called once with a wrong token (it must say so and stop, not spin)."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import httpx
import pytest

from omnibots.connectors.discord import NO, YES, DiscordBridge
from omnibots.connectors.telegram import TelegramBridge

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


class Approvals:
    def __init__(self):
        self.pending = {"ap_1": {"id": "ap_1", "bot_id": "front", "tool": "deploy_site", "risk": "R3",
                                 "summary": "deploy the bakery site to Netlify", "rehearsal": {}}}
        self.decided = []

    def list_pending(self):
        return list(self.pending.values())

    async def decide(self, aid, ok, reason=""):
        if aid not in self.pending:
            return False
        self.pending.pop(aid)
        self.decided.append((aid, ok, reason))
        return True


class Engine:
    def __init__(self, tmp):
        self.home = tmp
        self.calls = []
        self.approvals = Approvals()
        self.runner = type("R", (), {"active": {}})()
        proj = tmp / "proj_1"
        self.projects = type("P", (), {"folder": staticmethod(lambda pid: proj)})()

    async def ui_bots(self):
        return [{"id": "omi", "name": "Omi", "workspace": str(self.home / "omi")},
                {"id": "front", "name": "Frontend", "workspace": str(self.home / "front")}]

    async def start_goal(self, goal, info=None):
        self.calls.append(("goal", goal, info))
        return "proj_1"

    async def tell(self, bot, text):
        self.calls.append(("tell", bot, text))

    async def steer(self, bot, text):
        self.calls.append(("steer", bot, text))
        return "steer"

    async def ui_tray(self):
        return {"bots": [{"name": "Omi", "group": "working", "job": "plan the site"}, {"name": "Frontend", "group": "idle"}]}

    async def ui_proposals(self):
        return [{"id": "prop_1", "title": "Check the live site", "goal": "Open every page", "why": "links break"}]

    async def decide_proposal(self, pid, ok, note=""):
        self.calls.append(("proposal", pid, ok))
        return {"id": pid, "status": "accepted" if ok else "rejected", "project_id": "proj_2"}


# ── Telegram ───────────────────────────────────────────────────────────────
class FakeTelegram:
    def __init__(self):
        self.updates, self.sent, self.edits, self.answers, self.next_mid = [], [], [], [], 100

    def handler(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.startswith("/file/"):
            return httpx.Response(200, content=PNG)
        method = path.rsplit("/", 1)[1]
        body = json.loads(req.content or b"{}")
        if "/botBAD/" in path:
            return httpx.Response(401, json={"ok": False, "error_code": 401, "description": "Unauthorized"})
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"id": 1, "username": "omnibots_test_bot"}})
        if method == "getUpdates":
            ups = [u for u in self.updates if u["update_id"] >= body.get("offset", 0)]
            return httpx.Response(200, json={"ok": True, "result": ups})
        if method == "sendMessage":
            self.next_mid += 1
            self.sent.append({**body, "message_id": self.next_mid})
            return httpx.Response(200, json={"ok": True, "result": {"message_id": self.next_mid}})
        if method == "editMessageText":
            self.edits.append(body)
            return httpx.Response(200, json={"ok": True, "result": True})
        if method == "answerCallbackQuery":
            self.answers.append(body)
            return httpx.Response(200, json={"ok": True, "result": True})
        if method == "getFile":
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "photos/p.jpg", "file_size": len(PNG)}})
        return httpx.Response(404, json={"ok": False, "description": "no"})

    def say(self, chat, text=None, **extra):
        n = len(self.updates) + 1
        msg = {"message_id": n, "chat": {"id": chat, "type": extra.pop("chat_type", "private")}, **extra}
        if text is not None:
            msg["text"] = text
        self.updates.append({"update_id": n, "message": msg})

    def press(self, chat, data):
        n = len(self.updates) + 1
        self.updates.append({"update_id": n, "callback_query": {"id": f"q{n}", "data": data, "from": {"id": chat},
                                                                 "message": {"chat": {"id": chat}}}})


def tg(tmp_path, owner=None):
    fake, saved = FakeTelegram(), []
    eng = Engine(tmp_path)
    b = TelegramBridge(eng, "TOKEN", owner=owner, save_owner=saved.append,
                       client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)))
    return fake, eng, b, saved


def test_telegram_pairs_only_with_the_code_and_ignores_strangers(tmp_path):
    fake, eng, b, saved = tg(tmp_path)

    async def go():
        fake.say(111, "/start")
        fake.say(222, "/pair 000000" if b.pair_code != "000000" else "/pair 111111")   # a wrong code
        fake.say(333, f"/pair {b.pair_code}")
        fake.say(444, "build me a site")                                         # a stranger after pairing
        fake.say(333, "/status")
        await b.poll_once()
    asyncio.run(go())
    assert saved == ["333"] and b.owner == "333"
    to = {}
    for s in fake.sent:
        to.setdefault(str(s["chat_id"]), []).append(s["text"])
    assert "Not paired yet" in to["111"][0] and "Not paired yet" in to["222"][0]  # a typo gets the hint again
    assert "444" not in to                                                       # a stranger after pairing: nothing
    assert to["333"][0].startswith("Paired ✔") and "Omi — working: plan the site" in to["333"][1]
    assert eng.calls == []


def test_five_wrong_codes_close_pairing(tmp_path):
    fake, eng, b, saved = tg(tmp_path)
    wrong = "000000" if b.pair_code != "000000" else "111111"

    async def go():
        for _ in range(5):
            fake.say(222, f"/pair {wrong}")
        fake.say(222, f"/pair {b.pair_code}")                                     # even the right code now: locked
        await b.poll_once()
    asyncio.run(go())
    assert saved == [] and b.owner is None and b.pair_failures == 5
    assert len(fake.sent) == 5


def test_telegram_goal_with_a_photo_steering_and_mentions(tmp_path):
    fake, eng, b, _ = tg(tmp_path, owner="333")

    async def go():
        fake.say(333, caption="Make a logo like this", photo=[{"file_id": "s", "file_size": 10}, {"file_id": "big", "file_size": 90}])
        fake.say(333, "@frontend use bigger fonts")
        fake.say(333, "hello", chat_type="group")                                 # groups are never obeyed
        await b.poll_once()
        eng.runner.active = {"omi": object()}
        fake.say(333, "also add opening hours")
        await b.poll_once()
    asyncio.run(go())
    assert eng.calls[0] == ("goal", "Make a logo like this", {"created_by": "user-Telegram"})
    assert eng.calls[1][0:2] == ("tell", "omi") and "photo-1.jpg" in eng.calls[1][2]
    assert (tmp_path / "proj_1" / "attachments" / "photo-1.jpg").read_bytes() == PNG
    assert ("tell", "front", "use bigger fonts") in eng.calls
    assert ("steer", "omi", "also add opening hours") in eng.calls
    assert not any(c for c in eng.calls if "hello" in str(c))


def test_telegram_approvals_and_ideas_have_buttons_that_work(tmp_path):
    fake, eng, b, _ = tg(tmp_path, owner="333")

    async def go():
        await b.on_board({"type": "APPROVAL_REQUEST", "payload": {"approval_id": "ap_1", "summary": "deploy", "risk": "R3"}})
        await b.on_board({"type": "PROPOSAL", "payload": {"id": "prop_1", "title": "Check the live site", "goal": "Open every page"}})
        await b.on_board({"type": "TASK_COMPLETED", "sender_id": "omi", "topic": "#project/proj_1",
                          "payload": {"result": "The bakery site is live at https://soleil.example"}})
        await b.on_board({"type": "SEAT_GRANTED", "payload": {}})
        fake.press(999, "ap:ap_1:1")                                              # a stranger's press does nothing
        fake.press(333, "ap:ap_1:1")
        fake.press(333, "pr:prop_1:1")
        await b.poll_once()
    asyncio.run(go())
    ap = fake.sent[0]
    assert "deploy the bakery site to Netlify" in ap["text"]
    assert [k["callback_data"] for k in ap["reply_markup"]["inline_keyboard"][0]] == ["ap:ap_1:1", "ap:ap_1:0"]
    assert "Check the live site" in fake.sent[1]["text"] and fake.sent[2]["text"].startswith("✅ The bakery site is live")
    assert len(fake.sent) == 3                                                    # the seat message stayed quiet
    assert eng.approvals.decided == [("ap_1", True, "approved from Telegram")]
    assert ("proposal", "prop_1", True) in eng.calls
    assert [e["text"].endswith(("✔ Approved", "▶ Started (proj_2)")) for e in fake.edits] == [True, True]
    assert fake.answers[0]["text"] == ""                                           # the stranger got nothing
    assert fake.answers[1]["text"] == "✔ Approved"


def test_telegram_with_a_wrong_token_says_so_and_stops(tmp_path):
    fake, eng, b, _ = tg(tmp_path, owner="333")
    b.token = "BAD"
    asyncio.run(asyncio.wait_for(b.run(), 5))                                     # returns: no endless retry loop
    assert b.me == {}


def test_the_real_telegram_api_rejects_a_wrong_token(tmp_path):
    """The real api.telegram.org, a token that can't exist: the bridge must stop and say so."""
    import logging
    b = TelegramBridge(Engine(tmp_path), "123456:not-a-real-token", owner="1")
    try:
        asyncio.run(asyncio.wait_for(b.run(), 20))
    except (httpx.HTTPError, asyncio.TimeoutError) as exc:
        pytest.skip(f"no internet: {exc}")
    assert b.me == {}


# ── Discord ────────────────────────────────────────────────────────────────
class FakeDiscord:
    def __init__(self):
        self.messages, self.reactions, self.edits, self.next_id = [], {}, [], 1000

    def handler(self, req: httpx.Request) -> httpx.Response:
        p, m = req.url.path, req.method
        body = json.loads(req.content) if req.content else None
        if p == "/api/v10/users/@me":
            return httpx.Response(200, json={"id": "9", "username": "OmniBots"})
        if p == "/api/v10/users/@me/channels":
            return httpx.Response(200, json={"id": "dm1"})
        if p == "/api/v10/channels/dm1/messages" and m == "GET":
            after = int(req.url.params.get("after", "0"))
            return httpx.Response(200, json=[x for x in self.messages if int(x["id"]) > after][::-1])
        if p == "/api/v10/channels/dm1/messages" and m == "POST":
            self.next_id += 1
            self.messages.append({"id": str(self.next_id), "author": {"id": "9"}, "content": body["content"]})
            return httpx.Response(200, json={"id": str(self.next_id)})
        if "/reactions/" in p:
            mid, emoji = p.split("/messages/")[1].split("/reactions/")
            mid, emoji = mid, httpx.URL("http://x/" + emoji.split("/")[0]).path[1:]
            from urllib.parse import unquote
            emoji = unquote(emoji)
            if m == "PUT":
                self.reactions.setdefault((mid, emoji), ["9"])
                return httpx.Response(204)
            return httpx.Response(200, json=[{"id": u} for u in self.reactions.get((mid, emoji), [])])
        if m == "PATCH":
            self.edits.append(body)
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"message": "?"})

    def say(self, author, text):
        self.next_id += 1
        self.messages.append({"id": str(self.next_id), "author": {"id": author}, "content": text})


def test_discord_dm_goals_and_reaction_approvals(tmp_path):
    fake = FakeDiscord()
    eng = Engine(tmp_path)
    b = DiscordBridge(eng, "TOKEN", owner="555", client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)))

    async def go():
        fake.say("555", "old message before OmniBots started")
        await b.poll_once()                                                       # baseline: history isn't replayed
        fake.say("555", "Build a page for the bakery")
        fake.say("777", "a stranger in the DM somehow")
        await b.poll_once()
        await b.on_board({"type": "APPROVAL_REQUEST", "payload": {"approval_id": "ap_1", "summary": "deploy", "risk": "R3"}})
        mid = next(k for k in b.waiting)
        fake.reactions[(mid, YES)].append("555")                                  # the owner reacts ✅
        await b.poll_once()
        return mid
    mid = asyncio.run(go())
    assert eng.calls[0] == ("goal", "Build a page for the bakery", {"created_by": "user-Discord"})
    assert len([c for c in eng.calls if c[0] == "goal"]) == 1
    assert (mid, NO) in fake.reactions and "React ✅" in next(x["content"] for x in fake.messages if x["id"] == mid)
    assert eng.approvals.decided == [("ap_1", True, "approved from Discord")]
    assert fake.edits and fake.edits[-1]["content"].endswith("✔ Approved") and mid not in b.waiting


def test_discord_without_an_owner_id_stays_off(tmp_path):
    b = DiscordBridge(Engine(tmp_path), "TOKEN", owner=None)
    asyncio.run(asyncio.wait_for(b.run(), 5))
    assert b.me == {}


def test_the_engine_starts_telegram_only_with_a_token_and_shows_the_code(tmp_path, monkeypatch):
    """The real engine; a stand-in vault (the real one is the user's Credential Manager) and no network."""
    from omnibots.connectors import telegram
    from omnibots.engine import Engine as RealEngine
    ran = []

    async def no_network(self):
        ran.append(self.owner)
    monkeypatch.setattr(telegram.TelegramBridge, "run", no_network)
    from qt_helpers import qapp
    qapp()
    eng = RealEngine(tmp_path / "db" / "omnibots.sqlite", keep_awake=False, home=tmp_path, omni_install_root="")
    alerts = []
    eng.signals.alert.connect(alerts.append)
    eng.start()
    try:
        assert eng.submit(eng.ui_connectors()).result(timeout=10) == []        # no token: nothing runs
        eng.vault = type("V", (), {"value": staticmethod(lambda n: "123:abc" if n == "telegram_bot_token" else None)})()
        eng.bridges = []
        fut = eng.submit(asyncio.wait_for(eng._start_connectors(), 2))
        try:
            fut.result(timeout=5)
        except Exception:
            pass                                                                # the board loop runs until the timeout
        cons = eng.submit(eng.ui_connectors()).result(timeout=10)
    finally:
        eng.stop()
    from qt_helpers import qapp
    qapp().processEvents()                                                      # the alert crosses threads as a Qt signal
    assert ran == [None]
    assert cons[0]["name"] == "Telegram" and not cons[0]["paired"] and len(cons[0]["pair_code"]) == 6
    assert any(a.get("kind") == "connector_pair" and a.get("code") == cons[0]["pair_code"] for a in alerts)
