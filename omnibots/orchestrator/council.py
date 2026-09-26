"""The Council (PLAN.md ADR-13, A7.b.01) for decisions that matter.

Round 1: three members answer the SAME question independently, each through a
different lens. Round 2: each sees the other two answers, cross-examines them,
and gives a final position ending in one RECOMMENDATION line. The boss then
decides; the record (answers, critiques, recommendations, dissent) goes on
#council and back to the boss. Members use MiniMax seats with priority
"council" (ahead of ordinary work) and fall back to the cheap lane when no
seat is free.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field

from omnibots.board.types import COUNCIL
from omnibots.lineup import MINIMAX_FIRST
from omnibots.providers.router import Router

LENSES = [
    ("pragmatist", "You are a pragmatic senior engineer. Favour what works reliably with the least effort."),
    ("skeptic", "You are a skeptic and risk reviewer. Look for what could go wrong, hidden costs, lock-in and failure modes."),
    ("user advocate", "You are the user's advocate. Favour low cost, simplicity, privacy and what the user can maintain themselves."),
]
ROUND1 = "Answer the question below independently and concretely. End with one line: RECOMMENDATION: <your pick in a few words>."
ROUND2 = ("Here are the other council members' answers. Cross-examine them: where are they wrong or missing something? "
          "Then give your final position. End with one line: RECOMMENDATION: <your final pick in a few words>.")


@dataclass
class CouncilRecord:
    question: str
    answers: dict[str, str] = field(default_factory=dict)
    finals: dict[str, str] = field(default_factory=dict)
    recommendations: dict[str, str] = field(default_factory=dict)

    @property
    def agreement(self) -> float:
        recs = [re.sub(r"[^a-z0-9 ]", "", r.lower()).strip() for r in self.recommendations.values()]
        if not recs:
            return 0.0
        top = max(recs.count(r) for r in recs)
        return round(top / len(recs), 2)

    def summary(self) -> str:
        lines = [f"COUNCIL on: {self.question}", f"agreement: {self.agreement:.0%}"]
        for who, rec in self.recommendations.items():
            lines.append(f"- {who}: {rec}")
        for who, text in self.finals.items():
            lines.append(f"\n## {who}\n{text.strip()[:1500]}")
        return "\n".join(lines)


def _rec(text: str) -> str:
    m = re.findall(r"RECOMMENDATION:\s*(.+)", text, re.I)
    return m[-1].strip()[:160] if m else text.strip().splitlines()[-1][:160] if text.strip() else "(none)"


async def hold_council(router: Router, question: str, *, context: str = "", chain: list[str] | None = None,
                       bus=None, project_id: str | None = None, asked_by: str = "omi") -> CouncilRecord:
    chain = chain or list(MINIMAX_FIRST)
    rec = CouncilRecord(question=question)
    if bus:
        await bus.publish(COUNCIL, "COUNCIL_OPENED", {"text": question}, sender_type="bot", sender_id=asked_by, project_id=project_id)
    q = question + (f"\n\nCONTEXT:\n{context}" if context else "")

    async def ask(i: int, lens: tuple[str, str], messages: list[dict]) -> str:
        from omnibots.runtime.context import today_line
        routed = await router.chat(f"council_{i + 1}", chain, [{"role": "system", "content": lens[1] + "\n" + today_line()}, *messages], None, priority="council")
        return routed.result.answer

    first = await asyncio.gather(*(ask(i, lens, [{"role": "user", "content": f"{ROUND1}\n\nQUESTION:\n{q}"}]) for i, lens in enumerate(LENSES)))
    for (name, _), text in zip(LENSES, first):
        rec.answers[name] = text

    async def second(i: int, lens: tuple[str, str]) -> str:
        others = "\n\n".join(f"### {n}\n{rec.answers[n]}" for n, _ in LENSES if n != lens[0])
        return await ask(i, lens, [{"role": "user", "content": f"{ROUND1}\n\nQUESTION:\n{q}"},
                                   {"role": "assistant", "content": rec.answers[lens[0]]},
                                   {"role": "user", "content": f"{ROUND2}\n\n{others}"}])
    finals = await asyncio.gather(*(second(i, lens) for i, lens in enumerate(LENSES)))
    for (name, _), text in zip(LENSES, finals):
        rec.finals[name] = text
        rec.recommendations[name] = _rec(text)
    if bus:
        await bus.publish(COUNCIL, "COUNCIL_VERDICT",
                          {"text": f"agreement {rec.agreement:.0%}: " + " | ".join(f"{k}: {v}" for k, v in rec.recommendations.items()),
                           "recommendations": rec.recommendations, "agreement": rec.agreement},
                          sender_type="bot", sender_id=asked_by, project_id=project_id)
    return rec
