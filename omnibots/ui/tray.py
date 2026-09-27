"""The tray icon is the control center (PLAN.md A11.b.01).

  click (left or right)   opens the menu; a double-click opens Omi's window
  the icon                Omi's face showing the team's state (working, paused, stopped,
                          needs approval) with a badge for pending approvals
  the menu                rebuilt from the engine's state each time it opens (one engine
                          round trip, `engine.ui_tray()`), never polled

Stop, Restart Omi and Panic stop ask Yes/No first; Pause and Resume don't. Team and bot
actions run on the engine thread; the result comes back as a tray notification, so the
menu never freezes while a stop waits for bots to finish their step.
Items for views that don't exist yet (A11.a.01, A12) are shown disabled, with the item
that will build them in the tooltip, so the menu already has its final shape.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QCursor, QDesktopServices, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from omnibots.ui import theme
from omnibots.ui.omi_icon import omi_icon

log = logging.getLogger(__name__)

# (group, label, face mood) in menu order; only non-empty groups are listed
GROUPS = [("working", "Working", "working"), ("thinking", "Thinking", "thinking"),
          ("approval", "Waiting for approval", "thinking"), ("question", "Waiting for your answer", "thinking"),
          ("seat", "Waiting for a seat", "sleepy"),
          ("rate_limited", "Rate-limited", "sleepy"), ("blocked", "Blocked", "sad"), ("paused", "Paused", "paused"),
          ("idle", "Idle", "happy"), ("error", "Error", "error"), ("stopped", "Stopped", "paused")]
LABEL = {g: label for g, label, _ in GROUPS}
MOOD = {g: mood for g, _, mood in GROUPS}
SOON = "Not built yet: comes with {}."


def short_number(n: float | None) -> str:
    if n is None:
        return ""
    for div, unit in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(n) >= div:
            return f"{n / div:.1f}{unit}".replace(".0", "")
    return str(int(n))


def status_line(s: dict[str, Any]) -> str:
    omi = next((b for b in s.get("bots", []) if b["id"] == "omi"), None)
    omi_text = "Paused" if s.get("paused") else LABEL.get(omi["group"], "Idle") if omi else "missing"
    parts = [f"Omi: {omi_text}", f"{len(s.get('bots', []))} bots", f"{s.get('seats_used', 0)}/{s.get('seats_total', 0)} seats"]
    if s.get("tokens_left") is not None:
        parts.append(f"{short_number(s['tokens_left'])} tokens left")
    return "OmniBots — " + " · ".join(parts)


def icon_state(s: dict[str, Any]) -> tuple[str, int | None]:
    n = len(s.get("approvals", []))
    if n:
        return "approval", n
    return {"working": "working", "paused": "paused", "stopped": "stopped"}.get(s.get("team", "idle"), "idle"), None


class Tray(QObject):
    finished = Signal(str, str)          # (title, text) from the engine thread → a notification
    _snap = Signal(object, object)       # (snapshot, then) from the engine thread

    def __init__(self, engine, live, *, confirm: Callable[[str, str], bool] | None = None,
                 open_settings: Callable[[], None] | None = None, quit_app: Callable[[], None] | None = None):
        super().__init__()
        self.engine, self.live = engine, live
        self.confirm = confirm or self._ask
        self.open_settings = open_settings or self._settings
        self.quit_app = quit_app or self._exit
        self.snapshot: dict[str, Any] = {}
        self._faces: dict[tuple[str, str], QIcon] = {}
        self._icon_key: tuple | None = None
        self._settings_win = None
        self.icon = QSystemTrayIcon(omi_icon("idle"))
        self.menu = QMenu()
        self.menu.setToolTipsVisible(True)
        self.menu.setStyleSheet(theme.stylesheet())
        self.menu.aboutToShow.connect(self.rebuild)
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._activated)
        self.finished.connect(self._on_finished)
        self._snap.connect(lambda s, then: then(s))
        # a single click waits one double-click interval, so a double-click doesn't also pop the menu
        self._click = QTimer(self)
        self._click.setSingleShot(True)
        self._click.setInterval(QApplication.doubleClickInterval())
        self._click.timeout.connect(self.popup)
        # the icon follows the team: refreshed (debounced) when a bot changes state or the board moves
        self._soon = QTimer(self)
        self._soon.setSingleShot(True)
        self._soon.setInterval(400)
        self._soon.timeout.connect(self.refresh_icon)
        sig = getattr(engine, "signals", None)
        if sig is not None:
            sig.bot_event.connect(lambda ev: ev.get("kind") == "state" and self._soon.start())
            sig.board_message.connect(lambda _m: self._soon.start())
            sig.alert.connect(self._on_alert)

    def _on_alert(self, a: dict[str, Any]) -> None:
        self._soon.start()
        if a.get("kind") == "approval_request" and QSystemTrayIcon.supportsMessages() and self.icon.isVisible():
            self.icon.showMessage(f"{a.get('bot_id')} needs your OK", f"{a.get('tool')}: {str(a.get('summary') or '')[:120]}",
                                  omi_icon("approval", 1), 8000)

    def show(self) -> None:
        self.refresh_icon()
        self.icon.show()

    # ── engine ─────────────────────────────────────────────────────────
    def fetch(self, wait: float = 0.5) -> dict[str, Any]:
        """For the menu: a fresh snapshot, but never wait long (a busy engine → the last snapshot)."""
        try:
            self.snapshot = self.engine.submit(self.engine.ui_tray()).result(timeout=wait)
        except Exception:
            log.info("tray snapshot not ready in %.1fs; showing the last one", wait)
        return self.snapshot

    def fetch_later(self, then) -> None:
        """For the icon: ask without waiting; `then(snapshot)` runs on the UI thread."""
        try:
            fut = self.engine.submit(self.engine.ui_tray())
        except Exception:
            return

        def done(f):
            try:
                self._snap.emit(f.result(), then)
            except Exception:
                pass
        fut.add_done_callback(done)

    def _later(self, coro, title: str, describe: Callable[[Any], str]) -> None:
        """Run on the engine thread; report back as a notification (the menu doesn't wait)."""
        fut = self.engine.submit(coro)

        def done(f):
            try:
                self.finished.emit(title, describe(f.result()))
            except Exception as exc:
                self.finished.emit(title, f"✖ failed: {exc}")
        fut.add_done_callback(done)

    def _on_finished(self, title: str, text: str) -> None:
        if QSystemTrayIcon.supportsMessages() and self.icon.isVisible():
            self.icon.showMessage(title, text, omi_icon(), 4000)
        self.refresh_icon()

    # ── the icon ───────────────────────────────────────────────────────
    def refresh_icon(self) -> None:
        self.fetch_later(self._apply_icon)             # never freeze the UI for the icon

    def _apply_icon(self, s: dict[str, Any]) -> None:
        self.snapshot = s
        key = icon_state(s)
        if key != self._icon_key:
            self._icon_key = key
            self.icon.setIcon(omi_icon(*key))
        self.icon.setToolTip(status_line(s)[:127])            # Windows cuts tray tooltips at 128

    def _activated(self, reason) -> None:
        R = QSystemTrayIcon.ActivationReason
        if reason == R.DoubleClick:
            self._click.stop()
            self.live.bring_to_front()
        elif reason == R.Trigger:                              # left click: the menu too (not only right-click)
            self._click.start()

    def popup(self) -> None:
        self.menu.popup(QCursor.pos())

    # ── the menu ───────────────────────────────────────────────────────
    def _act(self, menu: QMenu, text: str, slot=None, *, enabled: bool = True, tip: str = "", icon: QIcon | None = None) -> QAction:
        a = menu.addAction(text)
        if icon is not None:
            a.setIcon(icon)
        a.setEnabled(enabled and slot is not None)
        if tip:
            a.setToolTip(tip)
        if slot is not None:
            a.triggered.connect(slot)
        return a

    def face(self, role: str, group: str) -> QIcon:
        key = (role, group)
        if key not in self._faces:
            from omnibots.ui.props import job_for_role
            from omnibots.ui.widgets import mini_face
            self._faces[key] = QIcon(mini_face(theme.role_color(role), MOOD.get(group, "happy"), 24, badge=job_for_role(role)))
        return self._faces[key]

    def rebuild(self) -> None:
        s = self.fetch()
        m = self.menu
        m.clear()
        running, paused, interrupted = s.get("running", False), s.get("paused", False), s.get("interrupted", 0)
        head = m.addAction(status_line(s))
        head.setEnabled(False)
        self._act(m, "Open OmniBots (Omi's window)", self.live.bring_to_front)

        m.addSeparator()                                        # ── Omi and the whole team
        self._act(m, "▶  Start", self.start, enabled=not running and interrupted > 0,
                  tip=f"Pick up {interrupted} interrupted job(s)" if interrupted else "Nothing interrupted to start")
        if paused:
            self._act(m, "⏵  Resume", self.resume)
        else:
            self._act(m, "⏸  Pause  (Omi and all sub-agents)", self.pause, enabled=running)
        self._act(m, "⏹  Stop  (Omi and all sub-agents)", self.stop, enabled=running or paused)
        self._act(m, "⟳  Restart Omi", self.restart, enabled=running or paused or interrupted > 0)
        self._act(m, "⛔  Panic stop", self.panic, tip="Denies every pending approval, then kills every job, sandbox and browser now")

        m.addSeparator()                                        # ── bots, grouped by state
        by: dict[str, list[dict[str, Any]]] = {}
        for b in s.get("bots", []):
            by.setdefault(b["group"], []).append(b)
        for g, label, _ in GROUPS:
            if by.get(g):
                sub = m.addMenu(f"{label} ({len(by[g])})")
                sub.setToolTipsVisible(True)
                sub.setIcon(self.face(by[g][0]["role"], g))
                for b in by[g]:
                    self._bot_menu(sub, b)

        m.addSeparator()                                        # ── open views
        self._act(m, "Open message board", self.live.bring_to_front, tip="The Message Board is in the lower right of Omi's window")
        self._act(m, "Open running jobs", tip=SOON.format("A11.a.01"))
        pend = s.get("approvals", [])
        if pend:
            sub = m.addMenu(f"Open approvals inbox ({len(pend)})")
            for a in pend:
                self._act(sub, f"{a.get('bot_id')}: {a.get('tool')} [{a.get('risk')}] {str(a.get('summary') or '')[:60]}",
                          lambda _=False, a=a: self.review(a))
        else:
            self._act(m, "Open approvals inbox (0)", enabled=False)
        for text, item in (("Open projects", "A11.a.01"), ("Open ledger and council", "A11.a.01"), ("Open logs and audit", "A11.a.01")):
            self._act(m, text, tip=SOON.format(item))

        m.addSeparator()                                        # ── settings and configuration
        self._act(m, "Settings…", self.open_settings)
        self._act(m, "About OmniBots…", self.about)
        for text in ("Bot configurations…", "Provider config…", "Tools pool…", "Skills pool…", "Playbooks…",
                     "Routines and triggers…", "Parameters and orchestrator…"):
            self._act(m, text, tip=SOON.format("A11.a.01"))

        m.addSeparator()
        self._act(m, "Reset…", tip=SOON.format("A12"))
        self._act(m, "Exit", self.quit_app)

    def _bot_menu(self, parent: QMenu, b: dict[str, Any]) -> None:
        title = f"{b['name']} — {b['role']}" + (f" — {b['job'][:50]}" if b.get("job") else "")
        sub = parent.addMenu(self.face(b["role"], b["group"]), title)
        sub.setToolTipsVisible(True)
        bid = b["id"]
        self._act(sub, "Open window", lambda _=False: self.live.open_bot(bid))
        if b.get("paused"):
            self._act(sub, "Resume", lambda _=False: self.bot_action("resume_bot", bid))
        else:
            self._act(sub, "Pause", lambda _=False: self.bot_action("pause_bot", bid), enabled=b.get("active", False))
        self._act(sub, "Stop", lambda _=False: self.bot_action("stop_bot", bid), enabled=b.get("active", False))
        self._act(sub, "Restart", tip=SOON.format("A11.a.01 (for now: Stop, then Omi reassigns the job)"))
        self._act(sub, "Configure…", tip=SOON.format("A11.a.01"))
        mem = b.get("memory") or ""
        self._act(sub, "Open memory", (lambda _=False: QDesktopServices.openUrl(QUrl.fromLocalFile(mem))) if mem and Path(mem).exists() else None,
                  tip=mem or "no memory.md yet")
        self._act(sub, "View jobs", tip=SOON.format("A11.a.01"))

    # ── actions ────────────────────────────────────────────────────────
    def _ask(self, title: str, text: str) -> bool:
        return QMessageBox.question(None, title, text) == QMessageBox.StandardButton.Yes

    def start(self) -> None:
        self._later(self.engine.team.start(), "Start", lambda r: f"Resuming {len(r.get('resumed_projects', []))} goal(s)")

    def pause(self) -> None:
        self._later(self.engine.team.pause(), "Paused", lambda r: f"{len(r.get('paused', []))} bot(s) pause at their next step")

    def resume(self) -> None:
        self._later(self.engine.team.resume(), "Resumed", lambda r: f"{len(r.get('resumed', []))} bot(s) continue")

    def stop(self) -> None:
        if self.confirm("Stop the team?", "Stop Omi and every bot now? Their jobs become 'interrupted' (Start picks them up again)."):
            self._later(self.engine.team.stop(), "Stopped", lambda r: f"{len(r.get('stopped', []))} job(s) interrupted")

    def restart(self) -> None:
        if self.confirm("Restart Omi?", "Stop everything, then Omi picks up every interrupted goal again?"):
            self._later(self.engine.team.restart(), "Restarted", lambda r: f"Resuming {len(r.get('resumed_projects', []))} goal(s)")

    def panic(self) -> None:
        if self.confirm("PANIC STOP?", "Deny every pending approval and kill every job, sandbox and browser right now?"):
            self._later(self.engine.team.stop(panic=True), "PANIC STOP",
                        lambda r: f"{len(r.get('stopped', []))} job(s) killed, {r.get('approvals_denied', 0)} approval(s) denied")

    def bot_action(self, name: str, bot_id: str) -> None:
        if name == "stop_bot" and not self.confirm(f"Stop {bot_id}?", f"Stop {bot_id} now? Its job becomes 'interrupted' and Omi is told."):
            return
        verb = {"pause_bot": "paused", "resume_bot": "resumed", "stop_bot": "stopped"}[name]
        self._later(getattr(self.engine.team, name)(bot_id), bot_id.capitalize(),
                    lambda ok: f"{bot_id} {verb}" if ok else f"{bot_id} isn't working right now")

    def review(self, a: dict[str, Any], choose: Callable[[QMessageBox], str] | None = None) -> str:
        """One pending approval: what, where and the rehearsal, then Approve / Deny (or close = decide later)."""
        box = QMessageBox()
        box.setWindowTitle(f"Approval · {a.get('bot_id')}")
        box.setIcon(QMessageBox.Icon.Warning if a.get("risk") in ("R4", "R5") else QMessageBox.Icon.Question)
        box.setText(f"<b>{a.get('bot_id')}</b> wants to run <b>{a.get('tool')}</b> ({a.get('risk')})"
                    + (f" on <b>{a.get('host')}</b>" if a.get("host") else ""))
        box.setInformativeText(str(a.get("summary") or ""))
        if a.get("rehearsal"):
            box.setDetailedText(json.dumps(a["rehearsal"], indent=2, ensure_ascii=False))
        yes = box.addButton("Approve", QMessageBox.ButtonRole.AcceptRole)
        no = box.addButton("Deny", QMessageBox.ButtonRole.RejectRole)
        box.addButton("Later", QMessageBox.ButtonRole.DestructiveRole)
        if choose is not None:
            pick = choose(box)
        else:
            box.exec()
            pick = "approve" if box.clickedButton() is yes else "deny" if box.clickedButton() is no else "later"
        if pick in ("approve", "deny"):
            self._later(self.engine.approvals.decide(a["id"], pick == "approve", "user via tray"),
                        "Approved" if pick == "approve" else "Denied", lambda ok: f"{a.get('tool')} for {a.get('bot_id')}")
        return pick

    def _exit(self) -> None:
        """Exit: unsaved files in any bot window ask first (Save / Discard / Cancel)."""
        for w in list(getattr(self.live, "windows", {}).values()):
            tabs = getattr(w, "tabs", None)
            if tabs is not None and any(ed.dirty for ed in tabs.editors()):
                w.bring_to_front()
                if not tabs.close_all():
                    return
        QApplication.instance().quit()

    def about(self) -> None:
        from omnibots.ui.about import open_about
        self._about_win = open_about()

    def _settings(self) -> None:
        from omnibots.ui.settings_window import SettingsWindow
        if self._settings_win is None:
            self._settings_win = SettingsWindow()
        self._settings_win.show()
        self._settings_win.raise_()
