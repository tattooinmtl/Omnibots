"""Context management (PLAN.md A3.a.02): ports of Omni's compactMessages and
maybeAutoCompact, plus trimming old tool results in what the provider sees."""

from __future__ import annotations

import json
from typing import Any

from omnibots.providers.quota import estimate_tokens

DEFAULT_WINDOW = 32_768


def today_line() -> str:
    """Models don't know the date and assume their training year; tell them."""
    from datetime import datetime
    now = datetime.now().astimezone()
    return f"Today's date is {now:%Y-%m-%d} ({now:%A}), local time {now:%H:%M}. Use this, not your training data, for anything date-related."


def steering_message(note: str) -> str:
    """Omni's /btw wording, so the model treats it as a correction, not a new task."""
    return f"(btw — a note from the user while you work; not a new task. Factor it in and keep going): {str(note or '').strip()}"


def first_user_goal(messages: list[dict[str, Any]]) -> str | None:
    for m in messages:
        if m.get("role") == "user" and isinstance(m.get("content"), str) and not m["content"].startswith("[system note]"):
            return m["content"][:400]
    return None


def compact_messages(messages: list[dict[str, Any]], *, keep_tail: int = 8, session_goal: str | None = None) -> bool:
    """Collapse everything between the system prompt and the last `keep_tail`
    messages into one digest message. Mutates `messages`."""
    if len(messages) < keep_tail + 4:
        return False
    first = 1 if messages and messages[0].get("role") == "system" else 0
    cut = len(messages) - keep_tail
    while cut < len(messages) and messages[cut].get("role") == "tool":   # never orphan tool results
        cut += 1
    if cut - first < 3:
        return False
    dropped = messages[first:cut]
    first_user = next((m for m in dropped if m.get("role") == "user" and isinstance(m.get("content"), str)), None)
    result_by_id = {}
    for m in dropped:
        if m.get("role") == "tool" and m.get("tool_call_id"):
            raw = m.get("content") if isinstance(m.get("content"), str) else json.dumps(m.get("content"))
            result_by_id[m["tool_call_id"]] = (raw.split("\n")[0] if raw else "")[:80]
    trail = []
    for m in dropped:
        for call in m.get("tool_calls") or []:
            fn = call.get("function") or {}
            hint = ""
            try:
                a = json.loads(fn.get("arguments") or "{}")
                hint = a.get("path") or a.get("pattern") or a.get("command") or a.get("query") or ""
            except (ValueError, AttributeError):
                pass
            summary = result_by_id.get(call.get("id"))
            trail.append(f"{fn.get('name', '?')}{f' ({str(hint)[:80]})' if hint else ''}{f' -> {summary}' if summary else ''}")
    digest = "\n".join(x for x in [
        "[CONTEXT COMPACTED] Earlier conversation was condensed to fit the model's context window. Continue the task from the recent messages below.",
        f"Session goal: {session_goal[:400]}" if session_goal else "",
        f"Original request: {first_user['content'][:600]}" if first_user else "",
        (f"Tools already used ({len(trail)}):\n  - " + "\n  - ".join(trail[-40:])) if trail else "",
    ] if x)
    messages[first:cut] = [{"role": "user", "content": digest}]
    return True


def maybe_auto_compact(messages: list[dict[str, Any]], *, context_window: int | None, max_tokens: int, session_goal: str | None) -> bool:
    window = context_window or DEFAULT_WINDOW
    budget = max(window - (max_tokens or 0), window // 2)
    if estimate_tokens(messages) < budget * 0.85:
        return False
    return compact_messages(messages, session_goal=session_goal)


def trim_old_tool_results(messages: list[dict[str, Any]], keep_recent_turns: int = 2, max_chars: int = 2000) -> list[dict[str, Any]]:
    """What the PROVIDER sees: tool results older than the last N assistant
    turns are cut to head + tail. The in-memory history is left intact."""
    seen, old = 0, set()
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "assistant":
            seen += 1
        if seen > keep_recent_turns:
            old.add(i)
    out = []
    for i, m in enumerate(messages):
        c = m.get("content")
        if i in old and m.get("role") == "tool" and isinstance(c, str) and len(c) > max_chars:
            half = max_chars // 2
            m = {**m, "content": f"{c[:half]}\n…[{len(c) - max_chars} chars of an old tool result trimmed]…\n{c[-half:]}"}
        out.append(m)
    return out
