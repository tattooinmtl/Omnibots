"""The critic (PLAN.md A3.a.07): Omni's self_review idea.

An independent review run that sees ONLY the task and the resulting changes,
never the worker's reasoning (a second opinion from the same context tends to
agree with itself). It can't modify anything. Output: findings tagged
[BLOCKER|MAJOR|MINOR] with concrete fixes, ending in a VERDICT line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from omnibots.providers.router import Router

REVIEWER_PROMPT = """You are an independent reviewer on the OmniBots team.
You see only the original task and the files the worker produced, not how they got there.
Check that the result actually does what the task asked, handles obvious edge cases, and breaks nothing.
Report each problem on its own line as: [BLOCKER|MAJOR|MINOR] file:line - problem - concrete fix.
If there are no problems, say so in one line.
End with exactly one line: VERDICT: PASS or VERDICT: FAIL (FAIL if there is any BLOCKER or MAJOR)."""


@dataclass
class Review:
    verdict: str          # PASS | FAIL | UNKNOWN
    findings: list[str]
    text: str
    model: str


def workspace_snapshot(files: list[Path], root: Path, max_chars: int = 40_000) -> str:
    parts, used = [], 0
    for f in files:
        try:
            body = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = f.relative_to(root) if f.is_relative_to(root) else f
        chunk = f"=== {rel} ===\n{body}\n"
        if used + len(chunk) > max_chars:
            parts.append(f"=== {rel} === (omitted, size limit)")
            continue
        parts.append(chunk)
        used += len(chunk)
    return "\n".join(parts)


async def review(router: Router, *, reviewer_id: str, chain: list[str], task: str, changes: str) -> Review:
    from omnibots.runtime.context import today_line
    msgs = [{"role": "system", "content": REVIEWER_PROMPT + "\n" + today_line()},
            {"role": "user", "content": f"TASK:\n{task}\n\nRESULT (files produced):\n{changes}"}]
    routed = await router.chat(reviewer_id, chain, msgs, None, priority="review")
    text = routed.result.answer.strip()
    m = re.search(r"VERDICT:\s*(PASS|FAIL)", text, re.I)
    findings = [l.strip() for l in text.splitlines() if re.match(r"\s*\[(BLOCKER|MAJOR|MINOR)\]", l, re.I)]
    return Review(m.group(1).upper() if m else "UNKNOWN", findings, text, routed.model.key)
