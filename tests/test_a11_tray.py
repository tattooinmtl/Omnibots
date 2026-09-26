"""A11.b.01/02: the tray control center. The menu is built from `engine.ui_tray()`; one test
runs the REAL engine (its shapes), the rest a fake that answers with the same shapes."""

from __future__ import annotations

import asyncio
import concurrent.futures
import time

from qt_helpers import qapp

from PySide6.QtWidgets import QSystemTrayIcon

from omnibots.ui.tray import Tray, icon_state, short_number, status_line

app = qapp()


def bot(i, group="idle", role="coder", active=False, paused=False, job=""):
    return {"id": i, "name": i.capitalize(), "role": role, "group": group, "job": job, "active": active, "paused": paused, "memory": ""}


class FakeTeam:
    def __init__(self, calls):
        self.calls = calls

    def __getattr__(self, name):
        async def call(*a, **kw):
            self.calls.append((name, *a, *sorted(kw.items())))
            return True if name.endswith("_bot") else {}
        return call


class FakeApprovals:
    def __init__(self, calls):
        self.calls = calls

    async def decide(self, aid, ok, why):
        self.calls.append(("decide", aid, ok))
        return True


class FakeEngine:
    signals = None

    def __init__(self, snap):
        self.snap, self.calls = snap, []
        self.team, self.approvals = FakeTeam(self.calls), FakeApprovals(self.calls)

    def submit(self, coro):
        f = concurrent.futures.Future()
        f.set_result(asyncio.run(coro))
        return f

    async def ui_tray(self):
        return self.snap


class FakeLive:
    def __init__(self):
        self.opened = []

    def bring_to_front(self):
        self.opened.append("omi")

    def open_bot(self, b):
        self.opened.append(b)


def snap(**kw):
    s = {"team": "working", "running": True, "paused": False, "interrupted": 0, "approvals": [], "seats_used": 3,
         "seats_total": 4, "tokens_left": 1_200_000_000,
         "bots": [bot("omi", "working", "boss", True, job="plan the site"), bot("coder", "working", active=True, job="index.html"),
                  bot("writer", "idle", "writer")]}
    s.update(kw)
    return s


def texts(menu):
    return [a.text() for a in menu.actions() if not a.isSeparator()]


def find(menu, start):
    return next(a for a in menu.actions() if a.text().startswith(start))


def make(s=None, answer=True):
    eng, live = FakeEngine(s or snap()), FakeLive()
    asked = []
    tray = Tray(eng, live, confirm=lambda t, x: asked.append(t) or answer, open_settings=lambda: None, quit_app=lambda: None)
    return eng, live, tray, asked


def test_the_real_engine_answers_the_tray(tmp_path):
    from omnibots.engine import Engine
    eng = Engine(tmp_path / "db" / "omnibots.sqlite", keep_awake=False, home=tmp_path, omni_install_root="")
    eng.start()
    try:
        s = eng.submit(eng.ui_tray()).result(timeout=10)
        eng.submit(eng.team.pause()).result(timeout=10)
        paused = eng.submit(eng.ui_tray()).result(timeout=10)
        eng._bot_listener({"bot_id": "omi", "kind": "state", "content": "error: boom"})
        err = eng.submit(eng.ui_tray()).result(timeout=10)
    finally:
        eng.stop()
    assert s["team"] == "idle" and not s["running"] and s["seats_total"] == 4 and s["approvals"] == []
    omi = next(b for b in s["bots"] if b["id"] == "omi")
    assert omi["group"] == "idle" and omi["memory"].endswith("memory.md")
    assert paused["team"] == "paused" and paused["paused"]
    assert next(b for b in err["bots"] if b["id"] == "omi")["group"] == "error"         # live state reaches the groups
    assert status_line(s).startswith("OmniBots — Omi: Idle · 1 bots · 0/4 seats")


def test_status_line_and_icon_state():
    assert status_line(snap()) == "OmniBots — Omi: Working · 3 bots · 3/4 seats · 1.2B tokens left"
    assert short_number(850_000_000) == "850M" and short_number(12_500) == "12.5k" and short_number(None) == ""
    assert icon_state(snap()) == ("working", None)
    assert icon_state(snap(team="paused")) == ("paused", None)
    assert icon_state(snap(approvals=[{"id": "a"}, {"id": "b"}])) == ("approval", 2)


def test_menu_has_the_planned_shape_and_state_rules():
    eng, live, tray, _ = make()
    tray.rebuild()
    t = texts(tray.menu)
    assert t[0].startswith("OmniBots — Omi: Working") and not tray.menu.actions()[0].isEnabled()
    assert t[1] == "Open OmniBots (Omi's window)"
    assert not find(tray.menu, "▶  Start").isEnabled()                    # running: nothing to start
    assert find(tray.menu, "⏸  Pause").isEnabled() and find(tray.menu, "⏹  Stop").isEnabled()
    assert [x for x in t if x.split(" (")[0] in ("Working", "Idle", "Paused", "Thinking")] == ["Working (2)", "Idle (1)"]   # only non-empty groups
    assert "Settings…" in t and t[-1] == "Exit" and not find(tray.menu, "Reset…").isEnabled()
    seps = [i for i, a in enumerate(tray.menu.actions()) if a.isSeparator()]
    assert len(seps) == 5                                                  # separators between the categories
    # paused: Resume replaces Pause; stopped with interrupted work: Start is on
    eng.snap = snap(paused=True, team="paused")
    tray.rebuild()
    assert find(tray.menu, "⏵  Resume").isEnabled() and not any(x.startswith("⏸") for x in texts(tray.menu))
    eng.snap = snap(running=False, team="stopped", interrupted=2, bots=[bot("omi", "stopped", "boss")])
    tray.rebuild()
    assert find(tray.menu, "▶  Start").isEnabled() and not find(tray.menu, "⏸  Pause").isEnabled()
    assert find(tray.menu, "⟳  Restart Omi").isEnabled()


def test_team_actions_confirm_where_the_plan_says():
    eng, live, tray, asked = make(answer=False)
    tray.rebuild()
    find(tray.menu, "⏸  Pause").trigger()                                 # no question for pause
    find(tray.menu, "⏹  Stop").trigger()                                  # asked, answered No: nothing happens
    find(tray.menu, "⛔  Panic stop").trigger()
    assert eng.calls == [("pause",)] and asked == ["Stop the team?", "PANIC STOP?"]
    eng2, _, tray2, asked2 = make(answer=True)
    tray2.rebuild()
    find(tray2.menu, "⛔  Panic stop").trigger()
    find(tray2.menu, "⟳  Restart Omi").trigger()
    assert eng2.calls == [("stop", ("panic", True)), ("restart",)]


def test_bot_submenus_open_windows_and_control_one_bot():
    eng, live, tray, asked = make()
    tray.rebuild()
    working = find(tray.menu, "Working (2)").menu()
    coder = find(working, "Coder — coder — index.html").menu()
    assert texts(coder) == ["Open window", "Pause", "Stop", "Restart", "Configure…", "Open memory", "View jobs"]
    find(coder, "Open window").trigger()
    find(coder, "Pause").trigger()
    find(coder, "Stop").trigger()
    assert live.opened == ["coder"] and eng.calls == [("pause_bot", "coder"), ("stop_bot", "coder")] and asked == ["Stop coder?"]
    writer = find(find(tray.menu, "Idle (1)").menu(), "Writer").menu()
    assert not find(writer, "Pause").isEnabled() and not find(writer, "Stop").isEnabled()   # idle: nothing to pause


def test_approvals_inbox_decides_through_the_engine():
    a = {"id": "ap1", "bot_id": "coder", "tool": "git_push", "risk": "R3", "summary": "push main to origin", "host": "github.com",
         "rehearsal": {"commits": 2}}
    eng, live, tray, _ = make(snap(approvals=[a]))
    tray.rebuild()
    inbox = find(tray.menu, "Open approvals inbox (1)").menu()
    assert texts(inbox) == ["coder: git_push [R3] push main to origin"]
    seen = {}

    def choose(box):
        seen["text"], seen["details"] = box.text(), box.detailedText()
        return "approve"
    assert tray.review(a, choose) == "approve"
    assert "git_push" in seen["text"] and "github.com" in seen["text"] and '"commits": 2' in seen["details"]
    assert tray.review(a, lambda box: "later") == "later"
    assert eng.calls == [("decide", "ap1", True)]


def test_clicks_icon_and_speed_with_30_bots():
    groups = ["working", "thinking", "approval", "seat", "idle", "paused"]
    roles = ["coder", "writer", "researcher", "designer", "tester"]
    many = [bot("omi", "working", "boss", True)] + [bot(f"bot{i}", groups[i % 6], roles[i % 5], True) for i in range(29)]
    eng, live, tray, _ = make(snap(bots=many, approvals=[{"id": "x", "bot_id": "bot2"}]))
    t0 = time.perf_counter()
    tray.rebuild()                                                          # first open: faces drawn
    first = (time.perf_counter() - t0) * 1000
    assert first < 150, f"menu took {first:.0f} ms"
    assert sum(len(a.menu().actions()) for a in tray.menu.actions() if a.menu() and "(" in a.text() and "inbox" not in a.text()) == 30
    tray.refresh_icon()
    assert tray._icon_key == ("approval", 1) and "30 bots" in tray.icon.toolTip()
    R = QSystemTrayIcon.ActivationReason
    tray._activated(R.Trigger)                                              # a left click opens the menu (after the double-click wait)
    assert tray._click.isActive()
    tray._activated(R.DoubleClick)                                          # a double-click opens Omi instead
    assert not tray._click.isActive() and live.opened == ["omi"]
