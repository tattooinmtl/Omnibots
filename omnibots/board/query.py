"""Board queries (PLAN.md A4.a.03): filtered and paginated in SQL, so a big
board never has to be loaded into the UI."""

from __future__ import annotations

from typing import Any

from omnibots.board.types import ERROR_TYPES, Message


def build_query(*, topic: str | None = None, bot: str | None = None, job: str | None = None, project: str | None = None,
                types: set[str] | list[str] | None = None, since: str | None = None, until: str | None = None,
                after_id: int | None = None, before_id: int | None = None, errors_only: bool = False,
                limit: int = 200) -> tuple[str, list[Any]]:
    where, args = [], []
    if topic:
        if topic.endswith("*"):
            where.append("topic LIKE ?")
            args.append(topic[:-1].replace("%", r"\%") + "%")
        else:
            where.append("topic = ?")
            args.append(topic)
    if bot:
        where.append("(sender_id = ? OR recipient_id = ? OR topic = ?)")
        args += [bot, bot, f"#bot/{bot}"]
    if job:
        where.append("job_id = ?")
        args.append(job)
    if project:
        where.append("project_id = ?")
        args.append(project)
    kinds = set(types or ())
    if errors_only:
        kinds = (kinds & ERROR_TYPES) if kinds else set(ERROR_TYPES)
    if kinds:
        where.append(f"message_type IN ({','.join('?' * len(kinds))})")
        args += sorted(kinds)
    if since:
        where.append("created_at >= ?")
        args.append(since)
    if until:
        where.append("created_at <= ?")
        args.append(until)
    if after_id is not None:
        where.append("id > ?")
        args.append(after_id)
    if before_id is not None:
        where.append("id < ?")
        args.append(before_id)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    # Newest page first (for "load older"), returned oldest-first by the caller.
    order = "ASC" if after_id is not None else "DESC"
    return f"SELECT * FROM messages{clause} ORDER BY id {order} LIMIT ?", args + [int(limit)]


async def query(db, **filters) -> list[Message]:
    sql, args = build_query(**filters)
    rows = await db.read(sql, args)
    msgs = [Message.from_row(r) for r in rows]
    return sorted(msgs, key=lambda m: m.id)


def query_sync(conn, **filters) -> list[Message]:
    """Same query on a plain sqlite3 connection (the terminal viewer)."""
    sql, args = build_query(**filters)
    return sorted((Message.from_row(r) for r in conn.execute(sql, args).fetchall()), key=lambda m: m.id)
