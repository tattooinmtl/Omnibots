"""User, 2026-09-26: "the + ... dosent work to add a file to chat", "a clippy icon ... dosent do anything maybe
animate it", "a way to clear all messages or start a new session ... close the session and keep it as old
opened sessions that we can reopen from the File item menu under recent sessions"."""

from __future__ import annotations

import asyncio
import concurrent.futures
from pathlib import Path

from qt_helpers import qapp

from omnibots.ui.sessions import SessionStore

app = qapp()


class Eng:
    signals = None
    computers = None

    def __init__(self, tmp, site):
        self.home = tmp / "home"
        self.runner = type("R", (), {"active": {}})()
        self.calls, self.output_dir = [], tmp / "out"
        from omnibots.projects.store import ProjectStore
        self.projects = ProjectStore(None, tmp / "home" / "projects", output_dir=self.output_dir)
        self.approvals = None
        self.bots = [{"id": "omi", "name": "Omi", "role": "boss", "description": "", "status": "idle", "model": "m",
                      "seat": None, "usage_pct": 0.0, "workspace": str(site), "active": False}]

    def submit(self, coro):
        f = concurrent.futures.Future()
        f.set_result(asyncio.run(coro))
        return f

    async def ui_bots(self):
        return self.bots

    async def ui_history(self, bot_id):
        return {"events": [], "board": []}

    async def start_goal(self, text, info=None):
        self.calls.append((text, (info or {}).get("folder")))
        return "proj_x"


def make(tmp_path):
    from omnibots.omni.personality import load
    from omnibots.ui.live import LiveUI
    from omnibots.ui.taunts import Taunts
    site = tmp_path / "out" / "2026-09-26 website"
    (site / ".git").mkdir(parents=True)
    eng = Eng(tmp_path, site)
    live = LiveUI(eng, taunts=Taunts(load(None), seed=5))
    return eng, live, live.open_bot("omi"), site


def texts(w):
    return [l.text() for l in w.chat.findChildren(type(w.card.name)) if l.text() and not l.pixmap()]


def test_sessions_are_saved_closed_reopened_and_cleared(tmp_path):
    eng, live, w, site = make(tmp_path)
    w.chat.input.setText("make the header blue")
    w.chat._send()
    store = SessionStore(eng.home / "sessions")
    cur = store.current("omi")
    assert cur.title == "make the header blue" and [m["who"] for m in cur.messages] == ["user", "bot"]   # + Omi's note
    w.acts["new_session"].trigger()                                   # File → New session
    assert store.current("omi") is None and "make the header blue" not in texts(w)                # the old bubble is gone
    assert any("File → Recent sessions" in t for t in texts(w))                                     # the note says where it went
    recent = w.recent_sessions()
    assert len(recent) == 1 and "make the header blue" in recent[0][1]
    w._fill_recent()
    assert [a.text() for a in w.recent_menu.actions()] == [recent[0][1]]
    w.chat.input.setText("now the footer")
    w.chat._send()
    w.recent_menu.actions()[0].trigger()                              # reopen the old one
    assert store.current("omi").title == "make the header blue" and any("header blue" in t for t in texts(w))
    assert [s.title for s in store.recent("omi")] == ["now the footer"]    # the one we left is kept
    assert live.work_folder == site                                   # back in that session's project
    w.confirm = lambda *a: True
    w.acts["clear_chat"].trigger()                                    # Edit → Clear chat
    assert store.current("omi").messages == [] and "make the header blue" not in texts(w)
    # a restart shows the current session, not a rebuild from the board
    w.chat.input.setText("after the clear")
    w.chat._send()
    live2 = type(live)(eng, taunts=live.taunts)
    w2 = live2.open_bot("omi")
    assert "after the clear" in texts(w2) and "make the header blue" not in texts(w2)


def test_attached_files_are_copied_for_the_bots_and_the_paperclip_works(tmp_path):
    eng, live, w, site = make(tmp_path)
    doc = tmp_path / "brief.pdf"
    doc.write_bytes(b"%PDF-1.4 brief")
    w.chat.pick_files = lambda: [str(doc)]
    w.chat.clip.click()                                               # the 📎
    assert w.chat.attachments == [doc] and w.chat.clip.count == 1 and not w.chat.chips_box.isHidden()
    w.chat.input.setText("use this brief for the landing page")
    w.chat._send()
    text, folder = eng.calls[-1]
    assert folder == str(site) and (site / "attachments" / "brief.pdf").read_bytes() == b"%PDF-1.4 brief"
    assert "attachments/brief.pdf" in text and w.chat.attachments == [] and w.chat.clip.count == 0
    labels = [a.text() for a in w.chat.plus_menu.actions() if not a.isSeparator()]   # the ＋ menu
    assert labels == ["📎  Attach files…", "🗂  Attach a folder…", "🆕  New session", "🧹  Clear chat…"]
    w.chat.pick_files = lambda: [str(doc)]
    w.chat.plus_menu.actions()[0].trigger()
    w.chat.input.setText("")
    w.chat._send()                                                    # files only, no text: still sent
    assert "Please look at the attached files." in eng.calls[-1][0] and (site / "attachments" / "brief (2).pdf").exists()
