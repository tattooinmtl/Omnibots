"""Read the bots' conversations from the terminal (PLAN.md A4.c.01).

  python -m omnibots.board                  the last 100 messages, all topics
  python -m omnibots.board --follow         ...then keep printing new ones live
  python -m omnibots.board --bot omi        everything to/from one bot
  python -m omnibots.board --topic "#orchestrator"   one topic ("#job/*" = all jobs)
  python -m omnibots.board --job job_12 --types A2A_MESSAGE,CLAIM_SUBMITTED
  python -m omnibots.board --export md board.md       (or json)

Read-only: it opens the database in read-only mode and never writes. Until
the A11 window exists, this is how to watch the bots talk.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

from omnibots.board.query import query_sync
from omnibots.board.types import Message
from omnibots.paths import get_paths

ICON = {"A2A_MESSAGE": "💬", "TASK_ASSIGNED": "📋", "CLAIM_SUBMITTED": "📎", "CLAIM_ACCEPTED": "✅", "CLAIM_REJECTED": "❌",
        "SEAT_WAITING": "⏳", "SEAT_GRANTED": "💺", "SEAT_RELEASED": "🪑", "USER_STEER": "🧭", "TASK_COMPLETED": "🏁",
        "TASK_FAILED": "💥", "BLOCKED": "⛔", "LOCK_ACQUIRED": "🔒", "LOCK_RELEASED": "🔓", "QUESTION": "❓",
        "APPROVAL_REQUEST": "✋", "APPROVAL_DECISION": "👍", "BOT_CREATED": "🤖", "SYSTEM_RESET": "♻"}


def local_time(iso: str) -> str:
    """Board times are stored in UTC; show them in the user's local time."""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime("%H:%M:%S")
    except (ValueError, AttributeError):
        return "--:--:--"


def line(m: Message, color: bool) -> str:
    t = local_time(m.created_at)
    who = m.sender_id or m.sender_type
    to = f" → {m.recipient_id}" if m.recipient_id else ""
    head = f"{t}  {ICON.get(m.message_type, '•')} {who}{to}  [{m.message_type}] {m.topic}"
    body = m.text()
    if color:
        head = f"\033[90m{t}\033[0m  {ICON.get(m.message_type, '•')} \033[1m{who}\033[0m{to}  \033[36m[{m.message_type}]\033[0m \033[90m{m.topic}\033[0m"
    return head + ("\n      " + body.replace("\n", "\n      ") if body else "")


def to_markdown(msgs: list[Message]) -> str:
    out = ["# OmniBots board export", ""]
    for m in msgs:
        to = f" → **{m.recipient_id}**" if m.recipient_id else ""
        out.append(f"- `{m.created_at}` **{m.sender_id or m.sender_type}**{to} `{m.message_type}` `{m.topic}`")
        if m.text():
            out.append("  > " + m.text().replace("\n", "\n  > "))
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m omnibots.board", description="Read the OmniBots message board")
    ap.add_argument("--topic")
    ap.add_argument("--bot")
    ap.add_argument("--job")
    ap.add_argument("--project")
    ap.add_argument("--types", help="comma-separated message types")
    ap.add_argument("--errors", action="store_true", help="only failures, blocks and rejected claims")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--follow", "-f", action="store_true")
    ap.add_argument("--export", choices=["md", "json"])
    ap.add_argument("out", nargs="?", help="export file")
    ap.add_argument("--db", help="database path (default: ~/.omnibots/db/omnibots.sqlite)")
    a = ap.parse_args(argv)
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")
    db = Path(a.db) if a.db else get_paths().db_file
    if not db.exists():
        print(f"no board yet: {db} does not exist (start OmniBots first)")
        return 1
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    filters = dict(topic=a.topic, bot=a.bot, job=a.job, project=a.project, errors_only=a.errors,
                   types=[t.strip() for t in a.types.split(",")] if a.types else None)
    msgs = query_sync(conn, limit=a.limit, **filters)
    if a.export:
        text = to_markdown(msgs) if a.export == "md" else json.dumps([m.__dict__ for m in msgs], indent=2, ensure_ascii=False)
        if a.out:
            Path(a.out).write_text(text, encoding="utf-8")
            print(f"exported {len(msgs)} messages to {a.out}")
        else:
            print(text)
        return 0
    color = sys.stdout.isatty()
    for m in msgs:
        print(line(m, color))
    last = msgs[-1].id if msgs else 0
    if not a.follow:
        if not msgs:
            print("(no messages match)")
        return 0
    try:
        while True:                                   # separate process: it reads, it doesn't share the bus
            time.sleep(1.0)
            for m in query_sync(conn, after_id=last, limit=500, **filters):
                print(line(m, color), flush=True)
                last = m.id
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
