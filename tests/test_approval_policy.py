"""The user's approval policy (2026-09-26): "only delete, rm, destructive commands and tools should prompt
the user to approve; the rest should not be blocked". Money (R4) still always asks (standing rule).
Plus: a waiting approval is SHOWN (a card in the chat), and follow-up goals continue the project."""

from __future__ import annotations

import asyncio
import concurrent.futures
from pathlib import Path

from qt_helpers import qapp

from omnibots.runtime.approvals import ApprovalCenter
from omnibots.runtime.core_tools import _outside_is_r3, _write_risk
from omnibots.runtime.more_tools import command_risk
from omnibots.runtime.tools import ToolContext

app = qapp()


def test_only_destructive_and_money_ask_in_the_apps_mode():
    ac = ApprovalCenter(ask_from="R4")                      # settings [approvals] ask_from = "R4" (the app's default)
    runs = ["npm install", "pip install requests", "git push origin main", "curl https://example.com", "python app.py"]
    asks = ["rm notes.md", "del out.txt", "Remove-Item a.txt", "rmdir build", "git rm x", "find . -delete",
            "git push --force origin main", "git branch -D old", "git reset --hard", "DROP TABLE users"]
    assert not any(ac.needs_approval(command_risk(c)[0]) for c in runs)
    assert all(ac.needs_approval(command_risk(c)[0]) for c in asks), [c for c in asks if not ac.needs_approval(command_risk(c)[0])]
    assert ac.needs_approval("R4")                           # money always asks
    assert not ac.needs_approval("R3") and command_risk("echo rm is a word")[0] == "R1"
    strict = ApprovalCenter()                                # the old strict mode is still there ("R3")
    assert strict.needs_approval("R3")


def test_paths_outside_the_project(tmp_path):
    ws = tmp_path / "project"
    ws.mkdir()
    ctx = ToolContext(bot_id="b", workspace=ws)
    existing = tmp_path / "other.txt"
    existing.write_text("x", encoding="utf-8")
    home = Path.home()
    assert _outside_is_r3({"path": "../"}, ctx) == "R3"                                 # the live case: now just runs
    assert _outside_is_r3({"path": str(home / ".ssh" / "id_ed25519")}, ctx) == "R5"      # keys: asks
    assert _outside_is_r3({"path": str(tmp_path / ".env")}, ctx) == "R5"
    assert _write_risk({"path": "index.html"}, ctx) == "R0"
    assert _write_risk({"path": str(tmp_path / "new.txt")}, ctx) == "R3"                 # a new file outside: runs
    assert _write_risk({"path": str(existing)}, ctx) == "R5"                             # overwriting outside: asks


class Eng:
    signals = None
    computers = None

    def __init__(self, tmp, out):
        self.runner = type("R", (), {"active": {}})()
        self.calls, self.output_dir = [], out
        from omnibots.projects.store import ProjectStore
        self.projects = ProjectStore(None, tmp / "home" / "projects", output_dir=out)
        self.approvals = type("A", (), {"list_pending": lambda s: [], "decide": self._decide})()
        self.bots = [{"id": "omi", "name": "Omi", "role": "boss", "description": "", "status": "idle", "model": "m",
                      "seat": None, "usage_pct": 0.0, "workspace": str(out / "2026-09-26 website"), "active": False}]

    async def _decide(self, aid, ok, why):
        self.calls.append(("decide", aid, ok))
        return True

    def submit(self, coro):
        f = concurrent.futures.Future()
        f.set_result(asyncio.run(coro))
        return f

    async def ui_bots(self):
        return self.bots

    async def ui_history(self, bot_id):
        return {"events": [], "board": []}

    async def start_goal(self, text, info=None):
        self.calls.append(("goal", text, (info or {}).get("folder")))
        return "proj_x"


def make(tmp_path):
    from omnibots.omni.personality import load
    from omnibots.ui.live import LiveUI
    from omnibots.ui.taunts import Taunts
    out = tmp_path / "omnibots_output"
    site = out / "2026-09-26 website"
    (site / ".git").mkdir(parents=True)
    (site / "index.html").write_text("<h1>x</h1>", encoding="utf-8")
    eng = Eng(tmp_path, out)
    live = LiveUI(eng, taunts=Taunts(load(None), seed=3))
    return eng, live, live.open_bot("omi"), site


def test_a_waiting_approval_shows_as_a_card_and_approve_reaches_the_engine(tmp_path):
    eng, live, w, _ = make(tmp_path)
    live.on_alert({"kind": "approval_request", "id": "ap_7", "bot_id": "omi", "tool": "run_shell", "risk": "R5",
                   "summary": "run_shell: rm old.css (deletes files)"})
    card = live.cards["ap_7"][0]
    assert "rm old.css" in card.findChildren(type(w.card.name))[1].text() and card.approve.isEnabled()
    card.approve.click()
    assert eng.calls == [("decide", "ap_7", True)] and card.decided is True and not card.approve.isEnabled()
    live.on_alert({"kind": "approval_request", "id": "ap_8", "bot_id": "omi", "tool": "run_shell", "risk": "R5", "summary": "x"})
    live.on_alert({"kind": "approval_decision", "id": "ap_8", "approved": False})        # decided in the tray
    assert "ap_8" not in live.cards


def test_follow_ups_continue_the_project_and_new_project_starts_fresh(tmp_path):
    eng, live, w, site = make(tmp_path)
    assert w.files.root == site
    w.chat.input.setText("can you add a css to the index.html")
    w.chat._send()
    assert eng.calls[-1] == ("goal", "can you add a css to the index.html", str(site))     # the live bug: it got a NEW empty folder
    w.acts["new_project"].trigger()
    w.chat.input.setText("now make a logo")
    w.chat._send()
    assert eng.calls[-1] == ("goal", "now make a logo", None)                            # fresh folder
