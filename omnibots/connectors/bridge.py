"""What Telegram and Discord share (PLAN.md A17.h): what the owner can say, and what the team tells the owner.

From the owner                                   To the owner
  plain text      → Omi: a new goal, or a steer     a goal's result (TASK_COMPLETED from Omi on a project)
  @Name text      → that bot                        a bot's question or message for the user
  photos, files   → the project's attachments/      an approval, with Approve / Deny buttons
  /status         → who's working, what waits       one of Omi's ideas, with Start it / Not now
  /approvals      → the waiting approvals again     a goal that failed
  /proposals      → Omi's ideas again
  /help
Anyone else gets nothing back, except the pairing reply. Pairing: the app shows a 6-digit code; the owner sends
"/pair <code>" to the bot once and that chat becomes the only one obeyed (saved in settings.toml).
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

HELP = ("I'm Omi, the boss of your OmniBots team.\n"
        "• Write a goal and the team starts on it (or I take it as a note while we work).\n"
        "• @Name message — talk to one bot.\n"
        "• Send photos or files with a goal: they go into the project's attachments.\n"
        "• /status — who is working · /approvals — what waits for your OK · /proposals — my ideas")
NOISY = {"SEAT_GRANTED", "SEAT_RELEASED", "SEAT_WAITING"}


@dataclass
class Button:
    text: str
    data: str                       # "ap:<approval id>:1" | "pr:<proposal id>:0" …


@dataclass
class Outgoing:
    text: str
    buttons: list[Button] = field(default_factory=list)
    key: str = ""                   # what the message is about ("ap:12"), so a decision can update it


class ChatBridge:
    """The connector-independent part. A connector calls on_message / on_button and implements send()."""

    name = "chat"

    def __init__(self, engine, *, owner: str | None, save_owner: Callable[[str], None] | None = None):
        self.engine = engine
        self.owner = str(owner) if owner else None
        self.save_owner = save_owner
        self.pair_code = f"{secrets.randbelow(10**6):06d}"
        self.pair_failures = 0                         # 5 wrong codes close pairing until the next start (no brute force)
        self.sent: dict[str, Any] = {}              # key -> the connector's message handle (to update buttons)

    # ── the connector implements these ───────────────────────────────────
    async def send(self, chat: str, msg: Outgoing) -> Any:
        raise NotImplementedError

    async def mark_done(self, handle: Any, note: str) -> None:
        """Remove the buttons of a decided approval/proposal and add a note (optional)."""

    # ── from the owner ──────────────────────────────────────────────────
    def is_owner(self, chat: str) -> bool:
        return self.owner is not None and str(chat) == self.owner

    async def on_message(self, chat: str, text: str, files: list[Path] | None = None) -> None:
        text = (text or "").strip()
        if not self.is_owner(chat):
            m = re.match(r"^/pair\s+(\d{6})$", text)
            if self.owner is None and self.pair_failures >= 5:
                return                                       # locked: silence until OmniBots restarts with a new code
            if m and self.owner is None and not secrets.compare_digest(m.group(1), self.pair_code):
                self.pair_failures += 1
                log.warning("%s: wrong pairing code from chat %s (%d of 5)", self.name, chat, self.pair_failures)
            if m and self.owner is None and secrets.compare_digest(m.group(1), self.pair_code):
                self.owner = str(chat)
                if self.save_owner:
                    self.save_owner(self.owner)
                await self.send(chat, Outgoing("Paired ✔ This chat now talks to your OmniBots team.\n\n" + HELP))
            elif text.startswith(("/start", "/pair")) and self.owner is None:
                await self.send(chat, Outgoing("Not paired yet. Open OmniBots on your PC: Omi shows a 6-digit code. Send "
                                               "/pair <code> here."))
            return                                       # strangers get nothing else
        if text in ("/start", "/help") or not (text or files):
            await self.send(chat, Outgoing(HELP))
            return
        if text == "/status":
            await self.send(chat, Outgoing(await self.status_text()))
            return
        if text == "/approvals":
            pending = self.engine.approvals.list_pending()
            if not pending:
                await self.send(chat, Outgoing("Nothing is waiting for your OK."))
            for a in pending:
                await self.push_approval(a)
            return
        if text == "/proposals":
            props = await self.engine.ui_proposals() if hasattr(self.engine, "ui_proposals") else []
            if not props:
                await self.send(chat, Outgoing("No ideas waiting."))
            for p in props:
                await self.push_proposal(p)
            return
        await self.deliver(text, files or [])

    async def deliver(self, text: str, files: list[Path]) -> None:
        m = re.match(r"^@(\w[\w-]*)\s+(.+)$", text, re.S)
        bots = await self.engine.ui_bots()
        target = None
        if m:
            target = next((b for b in bots if b["name"].lower() == m.group(1).lower() or b["id"] == m.group(1).lower()), None)
            if target is None:
                await self.send(self.owner, Outgoing(f"No bot called {m.group(1)}. The team: " + ", ".join(b["name"] for b in bots)))
                return
            text = m.group(2)
        bot = target["id"] if target else "omi"
        active = bot in getattr(self.engine.runner, "active", {})
        if bot == "omi" and not active:
            pid = await self.engine.start_goal(text or "Please look at the attached files.", {"created_by": f"user-{self.name}"})
            names = self._attach(files, pid)
            if names:
                await self.engine.tell("omi", "The user sent these files with the goal (in the project's attachments/ folder): "
                                       + ", ".join(names))
            await self.send(self.owner, Outgoing(f"On it! New goal ({pid})." + (f" Files: {', '.join(names)}." if names else "")))
        elif active:
            ws = next((b["workspace"] for b in bots if b["id"] == bot), None)
            names = self._attach(files, None, Path(ws) if ws else None)
            note = text + (f"\n\nAttached (in the project's attachments/ folder): {', '.join(names)}" if names else "")
            how = await self.engine.steer(bot, note)
            await self.send(self.owner, Outgoing("Got it, passed on." if how != "answer" else "Answer delivered."))
        else:
            await self.engine.tell(bot, text)
            await self.send(self.owner, Outgoing(f"Told {target['name'] if target else 'Omi'}."))

    def _attach(self, files: list[Path], project_id: str | None, folder: Path | None = None) -> list[str]:
        """Files from the phone → <project>/attachments/ (a new goal's project, or the bot's current folder)."""
        if not files:
            return []
        try:
            if project_id and getattr(self.engine, "projects", None):
                folder = Path(self.engine.projects.folder(project_id))
        except Exception:
            pass
        if folder is None:
            folder = Path(self.engine.home) / "inbox"
        dest = folder / "attachments"
        dest.mkdir(parents=True, exist_ok=True)
        names = []
        for f in files:
            to = dest / f.name
            n = 1
            while to.exists():
                to = dest / f"{f.stem}-{n}{f.suffix}"
                n += 1
            shutil.copy2(f, to)
            names.append(to.name)
        return names

    async def on_button(self, chat: str, data: str) -> str:
        """A button press (or a reaction). Returns a short note for the connector to show."""
        if not self.is_owner(chat):
            return ""
        kind, _, rest = data.partition(":")
        ident, _, verdict = rest.rpartition(":")
        ok = verdict == "1"
        if kind == "ap":
            done = await self.engine.approvals.decide(ident, ok, f"{'approved' if ok else 'denied'} from {self.name}")
            note = ("✔ Approved" if ok else "✖ Denied") if done else "Already decided"
        elif kind == "pr":
            r = await self.engine.decide_proposal(ident, ok)
            note = f"▶ Started ({r.get('project_id')})" if ok and r.get("status") == "accepted" else (
                "Not now" if not ok else f"Already {r.get('status')}")
        else:
            return ""
        handle = self.sent.pop(f"{kind}:{ident}", None)
        if handle is not None:
            await self.mark_done(handle, note)
        return note

    # ── to the owner ─────────────────────────────────────────────────────
    async def status_text(self) -> str:
        t = await self.engine.ui_tray()
        lines = []
        for b in t.get("bots", []):
            state = b.get("group") or "idle"
            job = f": {b['job']}" if b.get("job") else ""
            lines.append(f"• {b.get('name')} — {state}{job}")
        waits = len(self.engine.approvals.list_pending())
        return ("Team:\n" + "\n".join(lines) if lines else "No bots yet.") + f"\n\nWaiting for your OK: {waits}"

    async def push_approval(self, a: dict[str, Any]) -> None:
        if not self.owner:
            return
        money = a.get("risk") == "R4"
        reh = a.get("rehearsal") or {}
        cost = f" (about ${reh['cost_usd']})" if reh.get("cost_usd") is not None else ""
        text = (f"⚠ {a.get('bot_name') or a.get('bot_id') or 'A bot'} asks: {a.get('summary') or a.get('tool')}"
                f"\n{a.get('risk')} — {'costs money' + cost if money else 'needs your OK'}")
        key = f"ap:{a.get('id')}"
        self.sent[key] = await self.send(self.owner, Outgoing(text, [Button("✔ Approve", f"{key}:1"), Button("✖ Deny", f"{key}:0")], key))

    async def push_proposal(self, p: dict[str, Any]) -> None:
        if not self.owner:
            return
        key = f"pr:{p.get('id')}"
        text = f"💡 An idea from Omi: {p.get('title')}\n{p.get('goal', '')}" + (f"\nWhy: {p['why']}" if p.get("why") else "")
        self.sent[key] = await self.send(self.owner, Outgoing(text, [Button("▶ Start it", f"{key}:1"), Button("Not now", f"{key}:0")], key))

    async def on_board(self, m: dict[str, Any]) -> None:
        """Every board message; only what the owner should hear goes out."""
        if not self.owner or m.get("type") in NOISY:
            return
        t, p, sender = m.get("type"), m.get("payload") or {}, m.get("sender_id")
        if t == "APPROVAL_REQUEST":
            a = next((x for x in self.engine.approvals.list_pending() if str(x.get("id")) == str(p.get("approval_id"))), None)
            await self.push_approval(a or {"id": p.get("approval_id"), "summary": p.get("summary"), "risk": p.get("risk")})
        elif t == "APPROVAL_DECISION":
            handle = self.sent.pop(f"ap:{p.get('approval_id')}", None)
            if handle is not None:
                await self.mark_done(handle, "✔ Approved" if p.get("approved") else "✖ Denied")
        elif t == "PROPOSAL":
            await self.push_proposal(p)
        elif t == "TASK_COMPLETED" and sender == "omi" and str(m.get("topic", "")).startswith("#project"):
            await self.send(self.owner, Outgoing("✅ " + str(p.get("result") or "Done.")[:3500]))
        elif t == "TASK_FAILED" and sender == "omi":
            await self.send(self.owner, Outgoing("❌ " + str(p.get("error") or "A goal failed.")[:1500]))
        elif t in ("QUESTION", "A2A_MESSAGE") and m.get("recipient_id") == "user" and m.get("sender_type") == "bot":
            name = next((b["name"] for b in await self.engine.ui_bots() if b["id"] == sender), sender or "A bot")
            await self.send(self.owner, Outgoing(f"{name}: {str(p.get('text') or '')[:3500]}"))


def save_setting_owner(home: Path, section: str, key: str) -> Callable[[str], None]:
    def save(value: str) -> None:
        from omnibots.settings import save_setting
        save_setting(Path(home) / "settings.toml", section, key, value)
    return save
