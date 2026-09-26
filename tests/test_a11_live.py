"""A11.c: the bot windows driven by the engine's events (ui/live.py), with a fake engine
that answers like the real one (same event and message shapes)."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json

from qt_helpers import qapp

from omnibots.omni.personality import load
from omnibots.ui.live import LiveUI
from omnibots.ui.taunts import Taunts

app = qapp()


class FakeEngine:
    def __init__(self, tmp):
        self.signals = None
        self.runner = type("R", (), {"active": {}})()
        self.computers = None
        self.calls: list[tuple] = []
        self.bots = [{"id": "omi", "name": "Omi", "role": "boss", "description": "", "status": "idle", "model": "minimax.io/m3",
                      "seat": 1, "usage_pct": 12.0, "workspace": str(tmp), "active": False},
                     {"id": "coder", "name": "Coder", "role": "coder", "description": "", "status": "working", "model": "nvidia/x",
                      "seat": None, "usage_pct": 3.0, "workspace": str(tmp), "active": True}]

    def submit(self, coro):
        f = concurrent.futures.Future()
        f.set_result(asyncio.run(coro))
        return f

    async def ui_bots(self):
        return self.bots

    async def ui_history(self, bot_id):
        return {"events": [{"kind": "console", "content": "> earlier line"}],
                "board": [{"id": 1, "sender_id": "user", "sender_type": "user", "recipient_id": "omi", "type": "A2A_MESSAGE",
                           "payload": {"text": "hello team"}, "created_at": "2026-09-26T10:00:00Z", "topic": "#bot/omi"},
                          {"id": 2, "sender_id": None, "sender_type": "system", "recipient_id": "omi", "type": "SEAT_RELEASED",
                           "payload": {"seat": 1}, "created_at": "2026-09-26T10:00:01Z", "topic": "#orchestrator"},
                          {"id": 3, "sender_id": "omi", "sender_type": "bot", "recipient_id": "user", "type": "TASK_COMPLETED",
                           "payload": {"result": "Goal complete: hello.md is ready."}, "created_at": "2026-09-26T10:03:00Z",
                           "topic": "#project/proj_1"}]}

    async def steer(self, bot, text):
        self.calls.append(("steer", bot, text))

    async def start_goal(self, text):
        self.calls.append(("goal", text))
        return "proj_1"

    async def tell(self, bot, text):
        self.calls.append(("tell", bot, text))


def make(tmp_path):
    eng = FakeEngine(tmp_path)
    live = LiveUI(eng, taunts=Taunts(load(None), seed=4))
    return eng, live


def test_the_real_engine_answers_the_ui(tmp_path):
    """The fake above mirrors these; this checks the real engine's shapes (a fake hid a bug once)."""
    from omnibots.engine import Engine
    eng = Engine(tmp_path / "db" / "omnibots.sqlite", keep_awake=False, home=tmp_path, omni_install_root="")
    eng.start()
    try:
        bots = eng.submit(eng.ui_bots()).result(timeout=10)
        hist = eng.submit(eng.ui_history("omi")).result(timeout=10)
    finally:
        eng.stop()
    omi = next(b for b in bots if b["id"] == "omi")
    assert {"name", "role", "model", "seat", "usage_pct", "workspace", "active"} <= set(omi)
    assert omi["name"] == "Omi" and isinstance(hist["events"], list) and isinstance(hist["board"], list)


def test_opening_a_window_shows_history_and_activity(tmp_path):
    eng, live = make(tmp_path)
    w = live.open_bot("omi")
    assert w.windowTitle() == "OmniBots" and w.quit_on_close
    assert "> earlier line" in w.console.view.toPlainText()
    assert [e.text for e, _ in w.board.entries] == ["hello team", "Goal complete: hello.md is ready."]   # seat noise skipped
    assert w.board.entries[0][0].sender == "You"
    chat = [l.text() for l in w.chat.findChildren(type(w.card.name))]
    assert any("hello team" in t for t in chat) and any("Goal complete: hello.md is ready." in t for t in chat)
    assert set(w.board.activity.rows) == {"omi", "coder"}
    other = live.open_bot("coder")
    assert other.windowTitle() == "OmniBots · Coder" and not other.quit_on_close
    assert live.open_bot("coder") is other                     # reopening brings the same window forward


def test_bot_events_drive_the_panels_face_and_activity(tmp_path):
    eng, live = make(tmp_path)
    w = live.open_bot("coder")
    for piece in ("Here is ", "the plan", "."):
        live.on_bot_event({"bot_id": "coder", "kind": "console", "content": piece, "stream": True})
    live.on_bot_event({"bot_id": "coder", "kind": "terminal", "content": "$ python app.py", "stream": False})
    live.on_bot_event({"bot_id": "coder", "kind": "thinking", "content": "I should ", "stream": True})
    live.on_bot_event({"bot_id": "coder", "kind": "thinking", "content": "test first", "stream": True})
    text = w.console.view.toPlainText()
    assert "Here is the plan." in text and "$ python app.py" in text              # a stream stays on one line
    assert "I should test first" in w.thinking.view.toPlainText()
    live.on_bot_event({"bot_id": "coder", "kind": "tool", "content": json.dumps({"phase": "start", "name": "write_file", "target": "site/index.html"})})
    assert w.face.prop == "coding" and "coding index.html" in w.board.activity.rows["coder"].text()
    live.on_bot_event({"bot_id": "coder", "kind": "tool", "content": json.dumps({"phase": "end", "name": "write_file", "target": "site/index.html", "ok": True})})
    assert w.face.bubble is not None and w.face.reactions[-1][0] == "👍"
    live.on_bot_event({"bot_id": "coder", "kind": "tool", "content": json.dumps({"phase": "end", "name": "run_shell", "ok": False})})
    assert w.face.reactions[-1][0] == "😮"                                       # failures always get a reaction
    live.on_bot_event({"bot_id": "coder", "kind": "state", "content": "waiting_approval: git_push live main"})
    assert w.card.card.status == "Needs approval" and w.face.prop == "waiting"
    assert "waiting for your click: git_push live main" in w.board.activity.rows["coder"].text()
    live.on_bot_event({"bot_id": "coder", "kind": "state", "content": "done"})
    assert w.card.card.status == "Online" and w.face.prop is None


def test_board_messages_reach_every_window_and_results_the_chat(tmp_path):
    eng, live = make(tmp_path)
    wo, wc = live.open_bot("omi"), live.open_bot("coder")
    live.on_board_message({"id": 2, "sender_id": "coder", "sender_type": "bot", "recipient_id": "omi", "type": "CLAIM_SUBMITTED",
                           "payload": {"claim_id": 1, "text": "site built"}, "created_at": "2026-09-26T10:05:00Z"})
    live.on_board_message({"id": 3, "sender_id": "coder", "sender_type": "bot", "recipient_id": "omi", "type": "TASK_COMPLETED",
                           "payload": {"result": "index.html + style.css are ready"}, "created_at": "2026-09-26T10:06:00Z"})
    live.on_board_message({"id": 4, "sender_id": None, "sender_type": "system", "recipient_id": "coder", "type": "SEAT_GRANTED",
                           "payload": {"seat": 2}, "created_at": "2026-09-26T10:06:01Z"})
    for w in (wo, wc):
        assert [e.mtype for e, _ in w.board.entries][-2:] == ["CLAIM_SUBMITTED", "TASK_COMPLETED"]          # seat noise skipped
    labels = [l.text() for l in wc.chat.findChildren(type(wc.card.name))]
    assert any("index.html + style.css are ready" in t for t in labels)


def test_your_goals_from_elsewhere_show_in_omis_chat_once(tmp_path):
    eng, live = make(tmp_path)
    w = live.open_bot("omi")

    def user_bubbles():
        return [l.text() for l in w.chat.findChildren(type(w.card.name)) if "calm colors" in l.text()]
    goal = {"id": 9, "sender_id": "user", "sender_type": "user", "recipient_id": None, "type": "TASK_RECEIVED",
            "payload": {"text": "Write colors.md with three calm colors"}, "created_at": "2026-09-26T10:10:00Z",
            "project_id": "proj_2", "topic": "#project/proj_2"}
    live.on_board_message(goal)                                  # e.g. sent from the tray / pipe
    assert len(user_bubbles()) == 1
    w.chat.input.setText("Write colors.md with three more calm colors")
    w.chat._send()                                               # typed here: shown once, not echoed by the board
    live.on_board_message({**goal, "id": 10, "payload": {"text": "Write colors.md with three more calm colors"}})
    assert len(user_bubbles()) == 2


def test_your_chat_steers_starts_goals_or_messages(tmp_path):
    eng, live = make(tmp_path)
    wo, wc = live.open_bot("omi"), live.open_bot("coder")
    eng.runner.active = {"coder": object()}
    wc.chat.input.setText("use pytest, not unittest")
    wc.chat._send()
    wo.chat.input.setText("build me a landing page")
    wo.chat._send()
    eng.runner.active = {}
    wc.chat.input.setText("later: add dark mode")
    wc.chat._send()
    assert eng.calls == [("steer", "coder", "use pytest, not unittest"), ("goal", "build me a landing page"),
                         ("tell", "coder", "later: add dark mode")]
