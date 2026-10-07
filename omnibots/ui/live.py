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
import re
import logging
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal

from omnibots.ui import theme
from omnibots.ui.props import action_for_tool
from omnibots.ui.taunts import Taunts
from omnibots.ui.widgets import BoardEntry, BotCard, ChatMessage

log = logging.getLogger(__name__)
THANKS = re.compile(r"(?i)\b(thanks?|thank you|thx|merci|great job|well done|good job|bravo)\b")

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
    # an engine answer, delivered on the UI thread: (then, result, error)
    _delivered = Signal(object, object, object)

    def __init__(self, engine, *, window_factory=None, taunts: Taunts | None = None):
        super().__init__()
        self._delivered.connect(lambda then, r, e: then(r, e) if then else None)
        self._refreshing = False
        self.engine = engine
        self.windows: dict[str, Any] = {}
        self._verdict_asked: set[str] = set()                   # A16.b: one "How did it go?" card per goal
        self.bots: dict[str, dict[str, Any]] = {}
        self._stream_kind: dict[str, str | None] = {}          # bot -> kind of the open streamed line
        self._last_quip: dict[str, float] = {}
        self._action: dict[str, str] = {}
        self._typed: set[tuple[str, str]] = set()                 # (bot, text) you typed in a window (no echo twice)
        self.work_folder: Path | None = None                      # File → Open folder: new goals work there (A11.m.03)
        self.fresh_next = False                                   # File → New project: the next goal gets a new folder
        home = getattr(engine, "home", None)
        from omnibots.ui.sessions import SessionStore
        self.sessions = SessionStore(Path(home) / "sessions") if home else None    # chat sessions (ui/sessions.py)
        self.window_factory = window_factory or self._make_window
        if taunts is None:
            from omnibots.omni.personality import load
            root = getattr(getattr(getattr(engine, "omni", None), "location", None), "install_root", None)
            taunts = Taunts(load(Path(root) if root else None))
        self.taunts = taunts
        self.cards: dict[str, list] = {}                         # approval id -> its cards (A11.e.01)
        sig = getattr(engine, "signals", None)
        if sig is not None:
            sig.bot_event.connect(self.on_bot_event)
            sig.board_message.connect(self.on_board_message)
            sig.alert.connect(self.on_alert)
            from PySide6.QtCore import QTimer
            self._cards = QTimer(self)
            self._cards.timeout.connect(self.refresh_cards)
            self._cards.start(5000)

    # ── engine calls (the engine runs on its own thread) ───────────────────
    def _run(self, coro, timeout: float = 10):
        return self.engine.submit(coro).result(timeout=timeout)

    def _later(self, coro, then=None) -> None:
        """Ask the engine WITHOUT waiting (live bug 2026-09-26: the window froze 7-10 s while the
        engine was busy). `then(result, error)` runs on the UI thread when the answer arrives."""
        try:
            fut = self.engine.submit(coro)
        except Exception as exc:
            if then:
                then(None, exc)
            return

        def done(f):
            try:
                r, e = f.result(), None
            except BaseException as exc:
                r, e = None, exc
            self._delivered.emit(then, r, e)
        fut.add_done_callback(done)

    def refresh_bots(self) -> None:
        self.bots = {b["id"]: b for b in self._run(self.engine.ui_bots())}

    def refresh_bots_later(self, then=None) -> None:
        """Refresh the bot list in the background; `then()` runs after it (skipped if it failed)."""
        def got(bots, err):
            if err is None and bots is not None:
                self.bots = {b["id"]: b for b in bots}
                if then:
                    try:
                        then()
                    except Exception:
                        log.exception("after the bot refresh")
        self._later(self.engine.ui_bots(), got)

    def name_of(self, bot_id: str | None) -> tuple[str, str]:
        if not bot_id:
            return ("", "")
        if bot_id == "user":
            return ("You", "user")
        b = self.bots.get(bot_id)
        if b is None and bot_id not in ("system", "reviewer") and not self._refreshing:
            self._refreshing = True                      # a new bot: learn its name in the background

            def done():
                self._refreshing = False
            self.refresh_bots_later(done)
            QTimer.singleShot(3000, done)
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
        if hasattr(w, "folder_opened"):
            w.engine = self.engine
            w.folder_opened.connect(self.set_work_folder)
            w.new_project.connect(self.start_new_project)
            w.session_action.connect(lambda action, sid, b=bot_id: self.on_session(b, action, sid))
            if self.sessions is not None:
                w.recent_sessions = lambda b=bot_id: [(s.id, s.label) for s in self.sessions.recent(b)]
        if hasattr(w, "on_top_changed"):                          # A17.d: Layout menu
            w.on_top_changed.connect(lambda on, b=bot_id: self.remember_on_top(b, on))
            w.personality_chosen.connect(lambda key, b=bot_id: self.choose_personality(b, key))
            w.mind_requested.connect(self.open_mind)
            chars = self.characters()
            if chars is not None:
                w.personalities = lambda b=bot_id, c=chars: (c.choices(), c.personality_of(b))
            if bot_id in self.ui_state().get("on_top", []):
                w.set_on_top(True)
            self._show_mood(bot_id, w)
        current = self.sessions.current(bot_id) if self.sessions is not None else None
        if self.sessions is not None and hasattr(w.chat, "added"):
            w.chat.added.connect(lambda who, text, b=bot_id: self._save_chat(b, who, text))
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
                if current is None:                               # no saved session yet: rebuild the chat from the board
                    self._chat_from(bot_id, w, m)
        except Exception:
            log.exception("could not load history for %s", bot_id)
        if current is not None:
            self._replay(w, current)
        for b in self.bots.values():
            w.board.set_activity(b["id"], b["name"], b["role"], "working" if b.get("active") else "idle",
                                 "working…" if b.get("active") else "idle")
        # approvals already waiting (asked before this window opened) show up too
        approvals = getattr(self.engine, "approvals", None)
        for a in (approvals.list_pending() if approvals is not None else []):
            if a.get("bot_id") == bot_id or bot_id == "omi":
                self.cards.setdefault(str(a["id"]), []).append(w.chat.add_approval(a, self.decide))
        if bot_id == "omi" and hasattr(self.engine, "ui_connectors"):      # A17.h: a phone still to pair
            self._later(self.engine.ui_connectors(), lambda cs, err: [
                self.on_alert({"kind": "connector_pair", "connector": c["name"], "code": c["pair_code"]})
                for c in (cs or []) if c.get("pair_code")] if not err else None)
        if bot_id == "omi" and hasattr(self.engine, "ui_proposals"):        # A17.e.01: ideas waiting in the inbox
            self._later(self.engine.ui_proposals(), lambda props, err: [self.show_proposal(p) for p in (props or [])] if not err else None)
        if bot_id == "omi" and getattr(w, "own_strip", None) is not None and hasattr(self.engine, "ui_background_today"):
            self._own_strip(w)
        w.show()
        return w

    # ── Omi's own ideas (A17.e.01) ─────────────────────────────────────────
    def show_proposal(self, p: dict[str, Any]) -> None:
        w = self.windows.get("omi")
        shown = getattr(self, "_proposals", None)
        if shown is None:
            shown = self._proposals = {}
        if w is None or p.get("id") in shown or not hasattr(w.chat, "add_proposal"):
            return
        shown[p["id"]] = w.chat.add_proposal(p, self.decide_proposal)

    def decide_proposal(self, pid: str, accept: bool) -> None:
        w = self.windows.get("omi")

        def then(r, err):
            if w is None:
                return
            if err:
                w.chat.add(ChatMessage("bot", f"✖ couldn't do that: {err}"))
            elif accept:
                w.chat.add(ChatMessage("bot", f"On it! Starting “{r.get('title')}” ({r.get('project_id')})."))
        self._later(self.engine.decide_proposal(pid, accept), then)

    # ── presence: on top, personalities, moods, the mind (A17.d) ──────────
    def characters(self):
        return getattr(getattr(self.engine, "runner", None), "characters", None)

    def ui_state(self) -> dict[str, Any]:
        home = getattr(self.engine, "home", None)
        try:
            return json.loads((Path(home) / "ui_state.json").read_text(encoding="utf-8")) if home else {}
        except (OSError, ValueError):
            return {}

    def remember_on_top(self, bot: str, on: bool) -> None:
        home = getattr(self.engine, "home", None)
        if not home:
            return
        st = self.ui_state()
        tops = [b for b in st.get("on_top", []) if b != bot] + ([bot] if on else [])
        st["on_top"] = tops
        (Path(home) / "ui_state.json").write_text(json.dumps(st, indent=2), encoding="utf-8")

    def choose_personality(self, bot: str, key: str) -> None:
        chars = self.characters()
        if chars is None:
            return
        w = self.windows.get(bot)
        if not key:
            from omnibots.ui.personality_dialog import PersonalityDialog
            dlg = PersonalityDialog(w)
            if not dlg.exec():
                return
            try:
                key = chars.add_custom(**dlg.values())
            except ValueError as exc:
                if w is not None:
                    w.chat.add(ChatMessage("bot", f"✖ {exc}"))
                return
        chars.set_personality(bot, key)
        if w is not None:
            label = chars.choices().get(key, key)
            w.chat.add(ChatMessage("bot", f"Personality: {label}. You'll hear it in my next answers (style only; the work stays the same)."))
            self._show_mood(bot, w)

    def _show_mood(self, bot: str, w=None) -> None:
        """The ID card shows the mood and personality; an idle face wears the mood."""
        chars = self.characters()
        w = w or self.windows.get(bot)
        if chars is None or w is None or not hasattr(w, "card"):
            return
        word, face = chars.mood_text(bot)
        label = chars.choices().get(chars.personality_of(bot), "Default")
        w.card.card.mood = f"{word}" + (f" · {label}" if label != "Default" else "")
        w.card.refresh()
        return face

    def open_mind(self) -> None:
        from omnibots.ui.mind_view import MindView
        if getattr(self, "_mind", None) is None:
            self._mind = MindView(list(self.bots.values()))
        self.refresh_bots_later(lambda: self._mind.set_bots(list(self.bots.values())) if self._mind else None)
        self._mind.show()
        self._mind.raise_()
        self._mind.activateWindow()

    def _mind_event(self, bot: str, kind: str, content: str) -> None:
        mind = getattr(self, "_mind", None)
        if mind is None or not mind.isVisible():
            return
        if kind == "tool":
            try:
                t = json.loads(content)
            except ValueError:
                return
            if t.get("phase") == "start":
                mind.pulse_tool(bot, t.get("name", ""))
        elif kind == "state":
            state = content.partition(":")[0].strip()
            mind.set_busy(bot, state in ("thinking", "tool", "waiting_approval", "waiting_answer"))

    # ── on their own (A15.g) ──────────────────────────────────────────────
    def _own_strip(self, w) -> None:
        w.own_strip.pause_toggled.connect(
            lambda on: self._later(self.engine.set_background_paused(on), lambda r, e: self.refresh_strip()))
        self._strip_timer = QTimer(self)
        self._strip_timer.timeout.connect(self.refresh_strip)
        self._strip_timer.start(30_000)
        self.refresh_strip()

        def away(text, err):
            if err is None and text and "omi" in self.windows:
                self.windows["omi"].chat.add(ChatMessage("bot", text))
        self._later(self.engine.ui_away_summary(), away)

    def refresh_strip(self) -> None:
        w = self.windows.get("omi")
        if w is None or getattr(w, "own_strip", None) is None:
            return
        self._later(self.engine.ui_background_today(), lambda d, e: w.own_strip.set_data(d) if e is None and d else None)

    # ── chat sessions (File → New/Close session, Recent sessions; Edit → Clear chat) ──
    def _save_chat(self, bot: str, who: str, text: str) -> None:
        w = self.windows.get(bot)
        folder = str(w.files.root) if w is not None and getattr(w, "files", None) is not None else ""
        try:
            self.sessions.append(bot, who, text, folder)
        except OSError:
            log.exception("could not save the chat session")

    def _replay(self, w, s) -> None:
        w.chat.replaying = True
        try:
            for m in s.messages:
                w.chat.add(ChatMessage(m.get("who", "bot"), m.get("text", "")))
        finally:
            w.chat.replaying = False

    def on_session(self, bot: str, action: str, sid: str = "") -> None:
        w = self.windows.get(bot)
        if w is None or self.sessions is None:
            return
        if action in ("new", "close"):
            closed = self.sessions.close(bot)
            w.chat.clear()
            w.chat.replaying = True                      # a note for you, not part of the new session
            w.chat.add(ChatMessage("bot", "🆕 New session." + (f" The last one is in File → Recent sessions (\"{closed.title[:40]}\")."
                                                               if closed else "")))
            w.chat.replaying = False
        elif action == "clear":
            self.sessions.clear(bot)
            w.chat.clear()
        elif action == "reopen":
            s = self.sessions.reopen(sid)
            if s is None:
                return
            w.chat.clear()
            self._replay(w, s)
            if s.folder and Path(s.folder).is_dir() and bot == "omi":
                self.work_folder, self.fresh_next = Path(s.folder), False     # back in that session's project
                w.files.set_root(Path(s.folder), working=True)

    def _attach(self, bot: str, w, text: str) -> str:
        """Files you attached: copied into the project's attachments/ folder (every bot and the sandbox can open
        them); with no project folder yet, their full paths are given instead."""
        files = w.chat.take_attachments() if w is not None and hasattr(w.chat, "take_attachments") else []
        if not files:
            return text
        target = self.goal_folder() if bot == "omi" else (Path(w.files.root) if getattr(w, "files", None) else None)
        names = []
        if target is not None and target.is_dir():
            import shutil
            from omnibots.ui.files import FileOps
            dest = target / "attachments"
            dest.mkdir(exist_ok=True)
            for p in files:
                to = FileOps.free_name(dest, p.name)
                try:
                    (shutil.copytree if p.is_dir() else shutil.copy2)(p, to)
                    names.append(f"attachments/{to.name}")
                except OSError as exc:
                    names.append(f"{p} (couldn't copy: {exc.strerror or exc})")
            return text + "\n\nAttached (copied into the project's attachments/ folder): " + ", ".join(names)
        return text + "\n\nAttached files (full paths): " + ", ".join(str(p) for p in files)

    def start_new_project(self) -> None:
        self.work_folder, self.fresh_next = None, True
        omi = self.windows.get("omi")
        out = getattr(self.engine, "output_dir", None)
        if omi is not None:
            if out and Path(out).is_dir():
                omi.files.set_root(Path(out), working=False)
            omi.chat.add(ChatMessage("bot", "🆕 Fresh start: my next goal gets its own new project folder."))

    def goal_folder(self) -> Path | None:
        """Where Omi's next goal works: the folder you opened, else the project his File Explorer shows
        (so "add a css to the index.html" continues the website), else a new folder."""
        if self.fresh_next:
            return None
        if self.work_folder:
            return self.work_folder
        omi = self.windows.get("omi")
        root = Path(omi.files.root) if omi is not None else None
        out = getattr(self.engine, "output_dir", None)
        projects = getattr(self.engine, "projects", None)
        if root and root.is_dir() and not (out and root == Path(out)) and projects is not None and projects.is_ours(root):
            return root
        return None

    def set_work_folder(self, folder: str) -> None:
        """The user opened a folder: Omi's next goal is created in it (the bots work there)."""
        self.work_folder, self.fresh_next = Path(folder), False
        omi = self.windows.get("omi")
        if omi is not None:
            omi.files.set_root(self.work_folder, working=True)
            omi.chat.add(ChatMessage("bot", f"📌 Got it: my next goal works in {self.work_folder}. "
                                            "(Your files stay yours: I keep my change history outside that folder.)"))

    # ── approvals you can see (A11.e.01) ──────────────────────────────────
    def on_alert(self, a: dict[str, Any]) -> None:
        if a.get("kind") == "connector_pair" and "omi" in self.windows:             # A17.h: pair the phone
            self.windows["omi"].chat.add(ChatMessage("bot", f"{a.get('connector')} is connected. To talk to me from your phone, "
                                                            f"send this to your bot once:  /pair {a.get('code')}"))
            return
        if a.get("kind") == "approval_request":
            self.show_approval(a)
        elif a.get("kind") == "approval_decision":
            for card in self.cards.pop(str(a.get("id")), []):
                if card.decided is None:
                    card.mark_decided(bool(a.get("approved")), "(elsewhere)")

    def show_approval(self, a: dict[str, Any]) -> None:
        """A card in the asking bot's chat, and in Omi's (the window you're most likely looking at)."""
        aid = str(a.get("id"))
        if aid in self.cards:
            return
        targets = [b for b in dict.fromkeys((a.get("bot_id"), "omi")) if b in self.windows]
        cards = []
        for b in targets:
            cards.append(self.windows[b].chat.add_approval(a, self.decide))
            self.windows[b].face.set_action("waiting")
        self.cards[aid] = cards

    def decide(self, aid: str, ok: bool) -> None:
        for card in self.cards.get(aid, []):
            if card.decided is None:
                card.mark_decided(ok)
        self.cards.pop(aid, None)
        self._later(self.engine.approvals.decide(aid, ok, "user via the chat card"))

    def team(self) -> list[tuple[str, str, str, str]]:
        return [(b["id"], b["name"], b["role"], "working" if b.get("active") else "happy") for b in self.bots.values()]

    def refresh_cards(self) -> None:
        """Seat, model and usage change while bots work; keep the ID cards and team strips current."""
        if not self.windows:
            return
        before = set(self.bots)
        self.refresh_bots_later(lambda: self._apply_cards(before))

    def _apply_cards(self, before: set[str]) -> None:
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
        if kind in ("tool", "state"):
            self._mind_event(bot, kind, content)
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
            mood_face = self._show_mood(bot, w) if state in ("done", "idle") else None
            if state in ("waiting_approval", "waiting_answer"):
                w.face.set_action("waiting")
            w.face.set_mood(mood_face or STATE_MOOD.get(state, "happy"))
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
        if m.get("type") == "WORK_STARTED" and p.get("origin") not in (None, "user"):    # A15.g.02: say what started it
            text += f" — on its own: {p.get('why') or p['origin']}"
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

    def ask_verdict(self, project_id: str) -> None:
        if project_id in self._verdict_asked or "omi" not in self.windows or not hasattr(self.engine, "ui_verdict_card"):
            return
        self._verdict_asked.add(project_id)

        def got(data, err):
            if err is None and data and "omi" in self.windows:
                self.windows["omi"].chat.add_verdict(data, self.rate)
        self._later(self.engine.ui_verdict_card(project_id), got)

    def rate(self, project_id: str, claim_id, verdict: int, note: str, done) -> None:
        def got(r, err):
            if err is not None:
                done(False, str(err))
            else:
                done(True, "thanks" + (" · " + "; ".join(r.get("effects", [])) if r.get("effects") else ""))
        self._later(self.engine.rate(project_id, verdict, note, claim_id), got)

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
        if m.get("type") == "PROPOSAL":                              # A17.e.01: a new idea from Omi → its card
            self.show_proposal({**(m.get("payload") or {})})
        # A16.b: a goal wrote its REPORT.md → ask how it went, in Omi's chat
        if m.get("type") == "ARTIFACT_READY" and (m.get("payload") or {}).get("text") == "REPORT.md" and m.get("project_id"):
            self.ask_verdict(m["project_id"])
        # a new goal: Omi's file explorer follows the project it works in
        if m.get("type") == "TASK_RECEIVED" and "omi" in self.windows and m.get("project_id"):
            def omi_files():
                root = Path(self.bots["omi"]["workspace"])
                self.windows["omi"].files.set_root(root, working=bool(self.work_folder and root == self.work_folder))
            self.refresh_bots_later(omi_files)
        # a worker got a job: its explorer follows that job's project (A11.m.02)
        rec = m.get("recipient_id")
        if m.get("type") == "TASK_ASSIGNED" and rec in self.windows and rec != "omi":
            self.refresh_bots_later(lambda: self.windows[rec].files.set_root(Path(self.bots[rec]["workspace"])))
        if m.get("type") == "BOT_CREATED":                        # a new clone joined: show it in the team strips
            def strips():
                for w in self.windows.values():
                    w.team_strip.set_team(self.team())
            self.refresh_bots_later(strips)

    # ── you → bots ────────────────────────────────────────────────────────
    def on_prompt(self, bot: str, text: str) -> None:
        w = self.windows.get(bot)
        chars = self.characters()
        if chars is not None and THANKS.search(text or ""):          # A17.d.02: being thanked cheers a bot up
            try:
                chars.feel(bot, "thanks")
                self._show_mood(bot, w)
            except OSError:
                pass
        text = self._attach(bot, w, text)
        self._typed.add((bot, text))
        active = bot in getattr(getattr(self.engine, "runner", None), "active", {})

        def reply(note_for):
            def then(result, err):
                note = f"✖ couldn't deliver that: {err}" if err else note_for(result)
                if w is not None and note:
                    w.chat.add(ChatMessage("bot", note))
            return then
        # never wait here: the window keeps drawing; the note comes when the engine answers
        if active:
            # an answer to the bot's own question: its real reply follows, no canned note
            self._later(self.engine.steer(bot, text),
                        reply(lambda how: None if how == "answer" else "Got it. I'll read this before my next step."))
        elif bot == "omi":
            folder = self.goal_folder()
            self.fresh_next = False
            info = {"folder": str(folder)} if folder else {}
            where = f" in {folder.name}" if folder else " in a new project folder"
            self._later(self.engine.start_goal(text, info),
                        reply(lambda pid: f"On it! Working on this{where} ({pid}). Watch the board for the team's progress."))
        else:
            self._later(self.engine.tell(bot, text), reply(
                lambda _r: "Heard. I keep reading the board, so I'll pick this up if it's mine. Ask Omi when it needs a new job."))
