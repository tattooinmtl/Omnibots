"""Omi's quips for the thought bubble (PLAN.md A11.d), in Omi's own voice from Omni.

Taunts turns what a bot just did into a short quip + a face mood:
  react("tool", name="write_file", ok=True)   -> cheer from Omi's "coding" pool, mood joy
  react("tool", name="run_shell", ok=False)   -> grumble from "running", grawlix first, mood sad
  react("impatient")                          -> one of Omi's IMPATIENT lines, mood sleepy
  react("provider")                           -> provider grumble, mood error
  react("approval", kind="waiting")           -> an OmniBots line in the same voice
status_verb() -> "Percolising…" (Omi's FUNNY_WORDS), for the activity line.
Recent lines aren't repeated.
"""

from __future__ import annotations

import random
from collections import deque
from dataclasses import dataclass

from omnibots.omni.personality import GRAWLIX, TOOL_ACTION


@dataclass
class Quip:
    text: str
    mood: str                   # a face mood (omi_face.MOODS)
    grawlix: str | None = None  # shown first in the bubble, for grumbles
    source: str = "omni"        # omni | omnibots
    emoji: str | None = None    # the reaction that pops up next to the face


class Taunts:
    def __init__(self, personality: dict, seed: int | None = None):
        self.p = personality
        self.rng = random.Random(seed)
        self._recent: deque[str] = deque(maxlen=6)
        self._g = 0

    def _pick(self, pool: list[str]) -> str:
        fresh = [x for x in pool if x not in self._recent] or list(pool)
        line = self.rng.choice(fresh)
        self._recent.append(line)
        return line

    def grawlix(self) -> str:
        g = GRAWLIX[self._g % len(GRAWLIX)]
        self._g += 1
        return g

    def status_verb(self) -> str:
        return self.rng.choice(self.p["FUNNY_WORDS"]) + "…"

    def react(self, event: str, *, name: str = "", ok: bool = True, kind: str = "") -> Quip | None:
        actions = self.p["ACTION_LINES"]
        ours = self.p.get("OMNIBOTS_LINES", {})
        if event == "tool":
            action = TOOL_ACTION.get(name)
            pools = actions.get(action or "", {})
            pool = pools.get("cheer" if ok else "grumble")
            if not pool:
                return None
            looks = action in ("reading", "searching")
            return Quip(self._pick(pool), "joy" if ok else "sad", None if ok else self.grawlix(),
                        emoji=("👀" if looks else "👍") if ok else "😮")
        if event == "thinking":
            return Quip(self._pick(actions["thinking"]["cheer" if ok else "grumble"]), "happy" if ok else "thinking",
                        emoji="💡" if ok else "🤔")
        if event == "impatient":
            return Quip(self._pick(self.p["IMPATIENT"]), "sleepy", emoji="💤")
        if event == "provider":
            return Quip(self._pick(actions["provider"]["grumble"]), "error", self.grawlix(), emoji="😮")
        if event in ours:
            k = kind or ("cheer" if ok else "grumble")
            pool = ours[event].get(k)
            if not pool:
                return None
            mood = {"cheer": "joy", "grumble": "sad", "waiting": "thinking"}.get(k, "happy")
            emoji = {"cheer": "✅", "grumble": "😮", "waiting": "⏳"}.get(k)
            return Quip(self._pick(pool), mood, self.grawlix() if k == "grumble" else None, source="omnibots", emoji=emoji)
        return None
