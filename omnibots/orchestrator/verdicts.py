"""Your verdict on the work (PLAN.md A16.b): 👍 / 👎 plus an optional note, on a finished goal or on
one bot's accepted claim.

It counts more than the bots grading themselves:
- a 👎 on a goal marks that goal's playbook run failed, whatever the retrospective said;
- a note becomes a lesson in the bot's memory.md ("From the user: …"), Omi's for a goal;
- the next retrospective on that playbook reads your recent verdicts first;
- `stats()` gives the share of 👍 per bot and per provider (A13 and the bot editor show it).
"""

from __future__ import annotations

import json
from typing import Any

from omnibots.board.types import topic_project

BOSS_ID = "omi"


def _provider(chain: list[str]) -> str:
    return chain[0].split("/")[0].split("::")[0] if chain else ""


async def rate(db, registry, bus, *, project_id: str, verdict: int, note: str = "", claim_id: int | None = None) -> dict[str, Any]:
    if verdict not in (1, -1):
        raise ValueError("verdict is 1 (👍) or -1 (👎)")
    note = " ".join(str(note or "").split())[:500]
    if claim_id is not None:
        claim = await db.read_one("SELECT bot_id, project_id, status FROM claims WHERE id=?", (claim_id,))
        if not claim or claim["project_id"] != project_id:
            raise ValueError(f"no claim {claim_id} in project {project_id}")
        bot_id = claim["bot_id"]
    else:
        bot_id = BOSS_ID
    prof = await registry.get(bot_id)
    run = await db.read_one("SELECT id, playbook_id FROM playbook_runs WHERE project_id=? ORDER BY id DESC LIMIT 1", (project_id,))
    vid = await db.write(
        "INSERT INTO verdicts (project_id, claim_id, bot_id, provider, playbook_id, verdict, note) VALUES (?,?,?,?,?,?,?)",
        (project_id, claim_id, bot_id, _provider(prof.chain) if prof else "", run["playbook_id"] if run else None, verdict, note))
    effects = []
    if claim_id is None and verdict < 0 and run:
        await db.write("UPDATE playbook_runs SET success=0 WHERE id=?", (run["id"],))
        effects.append("the playbook run is marked failed")
    if note and prof:
        prof.memory.add_lessons([f"From the user ({'👍' if verdict > 0 else '👎'}): {note}"])
        effects.append(f"{prof.name}'s memory has your note")
    what = f"claim #{claim_id} by {prof.name if prof else bot_id}" if claim_id is not None else "the goal"
    await bus.publish(topic_project(project_id), "PROGRESS_UPDATE",
                      {"text": f"You rated {what} {'👍' if verdict > 0 else '👎'}" + (f": {note}" if note else "")},
                      sender_type="user", sender_id="user", project_id=project_id)
    await db.audit("user", None, "verdict", json.dumps({"project": project_id, "claim": claim_id, "verdict": verdict}))
    return {"id": vid, "bot_id": bot_id, "effects": effects}


async def card_data(db, registry, project_id: str) -> dict[str, Any]:
    """What the "How did it go?" card shows: the goal and each accepted claim, with any verdict given."""
    goal = await db.read_one("SELECT goal FROM projects WHERE id=?", (project_id,))
    given = {(r["claim_id"]): r["verdict"] for r in await db.read(
        "SELECT claim_id, verdict FROM verdicts WHERE project_id=? ORDER BY id", (project_id,))}
    claims = []
    for c in await db.read("SELECT id, bot_id, text FROM claims WHERE project_id=? AND status='accepted' ORDER BY id", (project_id,)):
        prof = await registry.get(c["bot_id"])
        claims.append({"id": c["id"], "bot_id": c["bot_id"], "bot_name": prof.name if prof else c["bot_id"],
                       "text": c["text"][:200], "verdict": given.get(c["id"])})
    return {"project_id": project_id, "goal": goal["goal"] if goal else project_id, "verdict": given.get(None), "claims": claims}


async def recent_notes(db, playbook_id: str | None, limit: int = 5) -> list[str]:
    """Your latest verdicts on runs of this playbook, newest first, for the retrospective."""
    if not playbook_id:
        return []
    rows = await db.read("SELECT verdict, note FROM verdicts WHERE playbook_id=? AND claim_id IS NULL ORDER BY id DESC LIMIT ?",
                         (playbook_id, limit))
    return [("👍" if r["verdict"] > 0 else "👎") + (f" {r['note']}" if r["note"] else "") for r in rows]


async def stats(db) -> dict[str, list[dict[str, Any]]]:
    """Share of 👍 per bot and per provider (claims and goals together)."""
    out = {}
    for key in ("bot_id", "provider"):
        rows = await db.read(f"SELECT {key} AS k, SUM(verdict > 0) AS up, SUM(verdict < 0) AS down FROM verdicts "
                             f"WHERE {key} IS NOT NULL AND {key} != '' GROUP BY {key} ORDER BY {key}")
        out[key] = [{"id": r["k"], "up": int(r["up"] or 0), "down": int(r["down"] or 0),
                     "share_up": round((r["up"] or 0) / max(1, (r["up"] or 0) + (r["down"] or 0)), 2)} for r in rows]
    return out
