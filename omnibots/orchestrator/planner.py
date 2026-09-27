"""The Planner identity (PLAN.md A7.a.09): a goal → subtasks with done-criteria.

A separate model call with its own prompt. Following CORAL, the planner
never names agents or tools: it says WHAT must be true when each subtask is
done; the boss decides WHO and HOW. Output is strict JSON, validated here:
each subtask has a title, a description, done-criteria, dependencies (by
index, earlier subtasks only, so the plan is always a DAG), the skills it
needs, and a risk ceiling.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from omnibots.providers.router import Router

PLANNER_PROMPT = """You are the Planner on the OmniBots team. You turn a goal into a small plan of subtasks.
Rules:
- 2 to 7 subtasks. Each is one person-sized piece of work with a checkable result.
- For each subtask give done_criteria: concrete, verifiable conditions (files that must exist, facts that must be cited with URLs, numbers that must be computed).
- depends_on lists the numbers (1-based) of EARLIER subtasks it needs.
- done_criteria may only check what THIS subtask or the subtasks it depends on produce. Never require output from a
  later subtask (e.g. assets can't be "linked from index.html" if index.html is built afterwards: put the linking in the
  index.html subtask).
- skills: 1-3 short capability words (e.g. research, writing, python, data, review).
- risk: R1 for local work, R2 if it reads the web, R3 if it must act online as the user (publish, deploy, email).
- NEVER name agents, bots, people or tools. Say what must be done and how to know it's done, not who does it or with which tool.
- If the goal is too vague to plan (you can't tell what the result should be), set "question" to ONE short clarifying question and give no subtasks.
Reply with JSON only, no prose:
{"question": null, "subtasks": [{"title": "...", "description": "...", "done_criteria": "...", "depends_on": [], "skills": ["..."], "risk": "R1"}]}"""

NAMES_TOOLS = re.compile(r"\b(web_search|web_fetch|run_python|write_file|read_file|list_dir|send_message|bot_\w+|omi)\b", re.I)


@dataclass
class Subtask:
    title: str
    description: str
    done_criteria: str
    depends_on: list[int]
    skills: list[str]
    risk: str


@dataclass
class Plan:
    question: str | None
    subtasks: list[Subtask]
    raw: str


class PlanError(ValueError):
    pass


def _json_block(text: str) -> dict[str, Any]:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if m:
        text = m.group(1)
    else:
        a, b = text.find("{"), text.rfind("}")
        if a == -1 or b == -1:
            raise PlanError("the planner didn't return JSON")
        text = text[a:b + 1]
    try:
        return json.loads(text)
    except ValueError as exc:
        raise PlanError(f"the planner's JSON didn't parse: {exc}") from exc


def parse_plan(text: str) -> Plan:
    data = _json_block(text)
    q = data.get("question")
    subs = []
    for i, s in enumerate(data.get("subtasks") or [], start=1):
        deps = [int(d) for d in (s.get("depends_on") or []) if str(d).isdigit()]
        if any(d >= i or d < 1 for d in deps):
            raise PlanError(f"subtask {i} depends on {deps}; dependencies must be earlier subtasks")
        title = str(s.get("title") or "").strip()
        if not title or not str(s.get("done_criteria") or "").strip():
            raise PlanError(f"subtask {i} needs a title and done_criteria")
        risk = str(s.get("risk") or "R1").upper()
        subs.append(Subtask(title=title[:120], description=str(s.get("description") or title).strip(),
                            done_criteria=str(s["done_criteria"]).strip(), depends_on=deps,
                            skills=[str(x)[:30] for x in (s.get("skills") or [])][:3],
                            risk=risk if re.fullmatch(r"R[0-5]", risk) else "R2"))
    if not q and not subs:
        raise PlanError("the planner returned neither subtasks nor a question")
    if len(subs) > 10:
        raise PlanError("too many subtasks (max 10)")
    return Plan(question=str(q).strip() if q else None, subtasks=subs, raw=text)


async def make_plan(router: Router, chain: list[str], goal: str, *, context: str = "", attempts: int = 2) -> Plan:
    from omnibots.runtime.context import today_line
    msgs = [{"role": "system", "content": PLANNER_PROMPT + "\n" + today_line()},
            {"role": "user", "content": f"GOAL:\n{goal}" + (f"\n\nCONTEXT:\n{context}" if context else "")}]
    last: Exception | None = None
    for _ in range(attempts):
        routed = await router.chat("planner", chain, msgs, None, priority="council")
        text = routed.result.answer
        try:
            plan = parse_plan(text)
            for s in plan.subtasks:            # CORAL rule: the planner doesn't name agents or tools
                s.description = NAMES_TOOLS.sub("", s.description)
            return plan
        except PlanError as exc:
            last = exc
            msgs += [{"role": "assistant", "content": text},
                     {"role": "user", "content": f"That plan was invalid ({exc}). Reply again with valid JSON only, following the rules."}]
    raise PlanError(str(last))
