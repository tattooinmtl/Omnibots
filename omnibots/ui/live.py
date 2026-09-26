"""The bot windows, live (PLAN.md A11.c): the engine's events drive every open window.

  bot events (per bot)   console/terminal/thinking → its panels (streams stay on one line),
                         state → face mood, status text and its "● Name – doing" line,
                         tool start → the face's prop (keyboard, book, pen…) + the line,
                         tool end → Omi's quip + reaction emoji (at most one quip per 6 s per bot,
                         failures always)
  board messages         → every open window's Message Board; a bot's finished result
                         (TASK_COMPLETED) and questions for the user → that bot's chat
  your chat              → steers a working bot; to an idle Omi it's a new goal; to an idle
                         worker it's delivered as a message

Omi's window is the main window (titled "OmniBots"). With the tray (A11.b.01) closing any
window only hides it; without a system tray, closing Omi's window quits the app.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject

from omnibots.ui import theme
from omnibots.ui.props import action_for_tool
from omnibots.ui.taunts import Taunts
from omnibots.ui.widgets import BoardEntry, BotCard, ChatMessage

log = logging.getLogger(__name__)

STATE_MOOD = {"thinking": "thinking", "tool": "working", "waiting_approval": "thinking", "waiting_answer": "thinking", "waiting_seat": "sleepy",
              "sleeping": "sleepy", "rate_limited": "sleepy", "paused": "paused", "stopped": "paused",
              "done": "happy", "error": "error", "blocked": "sad", "idle": "happy"}
STATE_TEXT = {"thinking": ("Working", "working"), "tool": ("Working", "working"), "waiting_approval": ("Needs approval", "waiting_approval"),
              "waiting_answer": ("Waiting for your answer", "waiting_approval"),
              "waiting_seat": ("Waiting for a seat", "waiting"), "sleeping": ("Waiting", "waiting"), "rate_limited": ("Waiting", "waiting"),
              "paused": ("Paused", "paused"), "stopped": ("Stopped", "stopped"), "done": ("Online", "done"),
              "error": ("Error", "error"), "blocked": ("Blocked", "blocked"), "idle": ("Online", "idle")}
ACTION_WORDS = {"coding": "coding", "reading": "reading", "writing": "writing", "searching": "searching", "browsing": "browsing",
                "running": "running", "thinking": "thinking", "deploying": "deploying", "reviewing": "reviewing",
                "chatting": "talking", "waiting": "waiting", "shopping": "buying", "speaking": "speaking",
                "looking": "looking at", "filming": "making a video of"}
QUIP_EVERY = 6.0
NOISE = {"SEAT_GRANTED", "SEAT_RELEASED"}                    # too chatty for the board view


class LiveUI(QObject):
    def __init__(self, engine, *, window_factory=None, taunts: Taunts | None = None):
        super().__init__()
        self.engine = engine
        self.windows: dict[str, Any] = {}
        self.bots: dict[str, dict[str, Any]] = {}
        self._stream_kind: dict[str, str | None] = {}          # bot -> kind of the open streamed line
        self._last_quip: dict[str, float] = {}
        self._action: dict[str, str] = {}
        self._typed: set[tuple[str, str]] = set()                 # (bot, text) you typed in a window (no echo twice)
        self.window_factory = window_factory or self._make_window
        if taunts is None:
            from omnibots.omni.personality import load
            root = getattr(getattr(getattr(engine, "omni", None), "location", None), "install_root", None)
            taunts = Taunts(load(Path(root) if root else None))
        self.taunts = taunts
        sig = getattr(engine, "signals", None)
        if sig is not None:
            sig.bot_event.connect(self.on_bot_event)
            sig.board_message.connect(self.on_board_message)
            from PySide6.QtCore import QTimer
            self._cards = QTimer(self)
            self._cards.timeout.connect(self.refresh_cards)
            self._cards.start(5000)

    # ── engine calls (the engine runs on its own thread) ───────────────────
    def _run(self, coro, timeout: float = 10):
        return self.engine.submit(coro).result(timeout=timeout)

    def refresh_bots(self) -> None:
        self.bots = {b["id"]: b for b in self._run(self.engine.ui_bots())}

    def name_of(self, bot_id: str | None) -> tuple[str, str]:
        if not bot_id:
            return ("", "")
        if bot_id == "user":
            return ("You", "user")
        b = self.bots.get(bot_id)
        if b is None and bot_id not in ("system", "reviewer"):
            try:
                self.refresh_bots()
            except Exception:
                pass
            b = self.bots.get(bot_id)
        return (b["name"], b["role"]) if b else (bot_id.capitalize(), "system")

    # ── windows ───────────────────────────────────────────────────────────
    def _make_window(self, bot: dict[str, Any], team: list[tuple[str, str, str, str]]):
        from omnibots.ui.bot_window import BotWindow
        card = BotCard(name=bot["name"], role=bot["role"], status="Working" if bot.get("active") else "Online",
                       tagline=self.taunts.status_verb(), seat=f"MiniMax seat {bot['seat']}" if bot.get("seat") else "no seat right now",
                       model=bot.get("model") or "", usage_pct=bot.get("usage_pct", 0.0), accent=theme.role_color(bot["role"]))
        main = bot["id"] == "omi"
        w = BotWindow(card, Path(bot["workspace"]), team=team, bot_id=bot["id"], computers=getattr(self.engine, "computers", None),
                      quit_on_close=main)
        if main:
            w.setWindowTitle("OmniBots")
        return w

    def open_bot(self, bot_id: str = "omi"):
        if bot_id in self.windows:
            w = self.windows[bot_id]
            w.bring_to_front()
            return w
        self.refresh_bots()
        bot = self.bots.get(bot_id)
        if bot is None:
            raise KeyError(f"no bot {bot_id}")
        w = self.window_factory(bot, self.team())
        self.windows[bot_id] = w
        w.chat.prompt_sent.connect(lambda text, b=bot_id: self.on_prompt(b, text))
        if getattr(w, "team_strip", None) is not None:
            w.team_strip.bot_chosen.connect(self.open_bot)
        # history first, then live
        try:
            hist = self._run(self.engine.ui_history(bot_id))
            for ev in hist["events"]:
                self._panel_line(w, ev["kind"], ev["content"] or "")
            for m in hist["board"]:
                if m.get("type") in NOISE:
                    continue
                self._board_into(w, m)
                self._chat_from(bot_id, w, m)
        except Exception:
            log.exception("could not load history for %s", bot_id)
        for b in self.bots.values():
            w.board.set_activity(b["id"], b["name"], b["role"], "working" if b.get("active") else "idle",
                                 "working…" if b.get("active") else "idle")
        w.show()
        return w

    def team(self) -> list[tuple[str, str, str, str]]:
        return [(b["id"], b["name"], b["role"], "working" if b.get("active") else "happy") for b in self.bots.values()]

    def refresh_cards(self) -> None:
        """Seat, model and usage change while bots work; keep the ID cards and team strips current."""
        if not self.windows:
            return
        try:
            before = set(self.bots)
            self.refresh_bots()
        except Exception:
            return
        for bid, w in self.windows.items():
            b = self.bots.get(bid)
            if b is None:
                continue
            c = w.card.card
            c.seat = f"MiniMax seat {b['seat']}" if b.get("seat") else "no seat right now"
            c.model, c.usage_pct = b.get("model") or c.model, b.get("usage_pct", c.usage_pct)
            w.card.refresh()
            if set(self.bots) != before or set(w.team_strip.bot_ids) != set(self.bots):
                w.team_strip.set_team(self.team())

    def bring_to_front(self) -> None:
        (self.windows.get("omi") or self.open_bot("omi")).bring_to_front()

    # ── engine → windows ──────────────────────────────────────────────────
    def _panel_line(self, w, kind: str, content: str) -> None:
        if kind == "thinking":
            w.thinking.append(content)
        elif kind in ("console", "terminal"):
            w.console.append(content)

    def on_bot_event(self, ev: dict[str, Any]) -> None:
        bot, kind, content = ev.get("bot_id"), ev.get("kind"), ev.get("content") or ""
        w = self.windows.get(bot)
        if kind in ("console", "terminal", "thinking") and w is not None:
            panel = w.thinking if kind == "thinking" else w.console
            if ev.get("stream"):
                if self._stream_kind.get(bot) != kind:
                    panel.append("")                             # a fresh line for this stream
                self._stream_kind[bot] = kind
                panel.write(content)
            else:
                self._stream_kind[bot] = None
                panel.append(content)
        elif kind == "state":
            self._on_state(bot, content)
        elif kind == "tool":
            try:
                self._on_tool(bot, json.loads(content))
            except ValueError:
                pass

    def _set_line(self, bot: str, status: str, action: str) -> None:
        name, role = self.name_of(bot)
        for w in self.windows.values():
            w.board.set_activity(bot, name, role, status, action)

    def _on_state(self, bot: str, content: str) -> None:
        state, _, detail = content.partition(":")
        state, detail = state.strip(), detail.strip()
        text, line_status = STATE_TEXT.get(state, ("Working", "working"))
        w = self.windows.get(bot)
        if w is not None:
            w.card.card.status = text
            w.card.refresh()
            if state in ("done", "idle", "stopped", "error", "blocked"):
                w.face.set_action(None)
            if state in ("waiting_approval", "waiting_answer"):
                w.face.set_action("waiting")
            w.face.set_mood(STATE_MOOD.get(state, "happy"))
        words = {"thinking": "thinking" + (f" ({detail})" if detail else ""), "waiting_approval": f"waiting for your click: {detail}",
                 "waiting_answer": f"asked you: {detail}",
                 "waiting_seat": f"waiting for a MiniMax seat {detail}".strip(), "done": "done", "idle": "idle",
                 "paused": "paused", "stopped": "stopped", "error": f"error: {detail}", "blocked": f"blocked: {detail}",
                 "rate_limited": "waiting: providers are rate-limited"}.get(state)
        if words is None:
            words = self._action.get(bot) or (detail or state)
        self._set_line(bot, line_status, words[:90])

    def _on_tool(self, bot: str, t: dict[str, Any]) -> None:
        name, target = t.get("name", ""), t.get("target", "")
        action = action_for_tool(name, {"path": target})
        w = self.windows.get(bot)
        if t.get("phase") == "start":
            label = ACTION_WORDS.get(action or "", name.replace("_", " "))
            short = target.replace("\\", "/").rsplit("/", 1)[-1] if action in ("coding", "writing", "reading") else target
            self._action[bot] = f"{label} {short}".strip()[:90]
            self._set_line(bot, "working", self._action[bot])
            if w is not None and action:
                w.face.set_action(action)
            return
        ok = bool(t.get("ok", True))
        now = time.monotonic()
        if w is None:
            return
        if ok and now - self._last_quip.get(bot, 0) < QUIP_EVERY:
            return
        q = self.taunts.react("tool", name=name, ok=ok)
        if q:
            self._last_quip[bot] = now
            w.face.say(q)

    def _board_into(self, w, m: dict[str, Any]) -> None:
        sender, s_role = self.name_of(m.get("sender_id") or m.get("sender_type"))
        rec, _ = self.name_of(m.get("recipient_id"))
        p = m.get("payload") or {}
        text = str(p.get("text") or p.get("result") or p.get("summary") or p.get("title") or p.get("reason")
                   or p.get("error") or json.dumps(p, ensure_ascii=False)[:200])
        when = str(m.get("created_at") or "")[11:16]
        w.board.add(BoardEntry(when, m.get("sender_id") or "", sender, s_role, m.get("recipient_id"), rec or None,
                               m.get("type", ""), text))

    def _chat_from(self, bot: str, w, m: dict[str, Any]) -> None:
        """A bot's finished result or a question for you → its chat; your goals/messages to it → your side."""
        p, sender, t = m.get("payload") or {}, m.get("sender_id"), m.get("type")
        if sender == bot and t == "TASK_COMPLETED" and p.get("result") and (bot != "omi" or m.get("topic", "").startswith("#project")):
            w.chat.add(ChatMessage("bot", str(p["result"])[:4000]))
        elif sender == bot and t in ("QUESTION", "A2A_MESSAGE") and m.get("recipient_id") == "user":
            w.chat.add(ChatMessage("bot", str(p.get("text") or "")[:4000]))
        elif m.get("sender_type") == "user" and t in ("TASK_RECEIVED", "A2A_MESSAGE", "USER_STEER") and \
                (m.get("recipient_id") == bot or (bot == "omi" and t == "TASK_RECEIVED")):
            w.chat.add(ChatMessage("user", str(p.get("text") or "")[:4000]))

    def on_board_message(self, m: dict[str, Any]) -> None:
        if m.get("type") in NOISE:
            return                                                # too chatty for the board view
        for w in self.windows.values():
            self._board_into(w, m)
        sender = m.get("sender_id")
        if sender in self.windows:
            self._chat_from(sender, self.windows[sender], m)
        elif m.get("sender_type") == "user":                        # you → a bot (goals go to Omi)
            target = m.get("recipient_id") or ("omi" if m.get("type") == "TASK_RECEIVED" else None)
            text = str((m.get("payload") or {}).get("text") or "")
            if target in self.windows and (target, text) not in self._typed:
                self._chat_from(target, self.windows[target], m)
            self._typed.discard((target, text))                     # typed in that window: already shown there
        # a new goal: Omi's file explorer follows the project it works in
        if m.get("type") == "TASK_RECEIVED" and "omi" in self.windows and m.get("project_id"):
            try:
                self.refresh_bots()
                self.windows["omi"].files.set_root(Path(self.bots["omi"]["workspace"]))
            except Exception:
                log.exception("could not switch Omi's files")
        if m.get("type") == "BOT_CREATED":                        # a new clone joined: show it in the team strips
            try:
                self.refresh_bots()
                for w in self.windows.values():
                    w.team_strip.set_team(self.team())
            except Exception:
                log.exception("team refresh failed")

    # ── you → bots ────────────────────────────────────────────────────────
    def on_prompt(self, bot: str, text: str) -> None:
        w = self.windows.get(bot)
        self._typed.add((bot, text))
        active = bot in getattr(getattr(self.engine, "runner", None), "active", {})
        try:
            if active:
                how = self._run(self.engine.steer(bot, text))
                # an answer to the bot's own question: its real reply follows, no canned note
                note = None if how == "answer" else "Got it. I'll read this before my next step."
            elif bot == "omi":
                pid = self._run(self.engine.start_goal(text))
                note = f"On it! Planning this as a new goal ({pid}). Watch the board for the team's progress."
            else:
                self._run(self.engine.tell(bot, text))
                note = "I'm not working right now; your message is on the board for my next job. (Ask Omi to start new work.)"
        except Exception as exc:
            note = f"✖ couldn't deliver that: {exc}"
        if w is not None and note:
            w.chat.add(ChatMessage("bot", note))
