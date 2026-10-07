"""Personalities and moods for the bots (PLAN.md A17.d.01, A17.d.02). No voice: these shape how a bot writes and how
its face and card look, never what it is allowed to do.

Personality  a preset (the same seven roles as OmniOne) or a custom one (name, tone, style, emoji and humour levels),
             per bot. It is an overlay on the bot's prompt, after its role and rules: style only.
Mood         two numbers per bot, valence (-1 glum .. +1 cheerful) and energy (-1 drained .. +1 lively). Things that
             happen move them (a job passed, failed or was rejected, a denied approval, a long wait, the user's
             thanks); every hour they drift back halfway to calm (a 1-hour half-life). The face, the ID card and one
             line in the prompt show it.

Both live in one JSON file in the OmniBots home (`characters.json`), so a restart keeps them.
"""

from __future__ import annotations

import json
import math
import threading
import time
from pathlib import Path
from typing import Any

PRESETS: dict[str, dict[str, Any]] = {
    "default": {"label": "Default", "tone": "", "style": "", "emoji": 0, "humour": 1},
    "mentor": {"label": "Mentor", "tone": "patient and encouraging", "style": "explain the why behind each step, briefly",
               "emoji": 0, "humour": 1},
    "hype": {"label": "Hype", "tone": "high-energy and upbeat", "style": "short punchy sentences, celebrate wins",
             "emoji": 2, "humour": 2},
    "calm": {"label": "Calm", "tone": "quiet, steady and reassuring", "style": "plain words, no exclamation marks",
             "emoji": 0, "humour": 0},
    "pirate": {"label": "Pirate", "tone": "a cheerful pirate", "style": "pirate slang in chat ('arr', 'matey'), but code, "
               "file contents and reports stay normal", "emoji": 1, "humour": 2},
    "noir": {"label": "Noir detective", "tone": "a 1940s noir detective", "style": "dry, moody one-liners in chat; the work "
             "itself stays plain and exact", "emoji": 0, "humour": 2},
    "coach": {"label": "Coach", "tone": "a sports coach", "style": "direct, motivating, sets the next small goal", "emoji": 1,
              "humour": 1},
}
LEVELS = ("none", "a little", "lots")

# how much each event moves (valence, energy)
EVENTS: dict[str, tuple[float, float]] = {
    "job_passed": (0.35, 0.15), "job_failed": (-0.35, -0.1), "job_rejected": (-0.25, -0.05), "denied": (-0.15, -0.05),
    "long_wait": (-0.05, -0.2), "thanks": (0.4, 0.2), "rate_limited": (-0.1, -0.15), "started": (0.0, 0.15),
}
HALF_LIFE_HOURS = 1.0
FILE = "characters.json"


def mood_word(valence: float, energy: float) -> tuple[str, str]:
    """(word, face expression) for the face drawings: happy, sad, thinking, sleepy, error, working."""
    if valence >= 0.45:
        return ("proud", "happy") if energy >= 0.25 else ("content", "happy")
    if valence >= 0.15:
        return ("cheerful", "happy") if energy >= 0 else ("relaxed", "happy")
    if valence <= -0.45:
        return ("frustrated", "error") if energy >= 0 else ("glum", "sad")
    if valence <= -0.15:
        return ("worried", "thinking") if energy >= -0.2 else ("tired", "sleepy")
    if energy <= -0.35:
        return ("tired", "sleepy")
    if energy >= 0.35:
        return ("eager", "working")
    return ("calm", "happy")


class Characters:
    def __init__(self, home: Path | None, clock=time.time):
        self.path = (home / FILE) if home else None
        self.clock = clock
        self._lock = threading.Lock()
        self.data: dict[str, Any] = {"personality": {}, "custom": {}, "mood": {}}
        if self.path and self.path.is_file():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                for k in self.data:
                    self.data[k].update(loaded.get(k) or {})
            except (OSError, ValueError):
                pass

    def _save(self) -> None:
        if self.path:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)

    # ── personalities ───────────────────────────────────────────────────
    def choices(self) -> dict[str, str]:
        """key -> label: the presets, then the user's own."""
        out = {k: v["label"] for k, v in PRESETS.items()}
        out.update({k: v.get("label", k) for k, v in self.data["custom"].items()})
        return out

    def personality_of(self, bot_id: str) -> str:
        return self.data["personality"].get(bot_id, "default")

    def set_personality(self, bot_id: str, key: str) -> None:
        if key not in PRESETS and key not in self.data["custom"]:
            raise ValueError(f"unknown personality: {key}")
        with self._lock:
            self.data["personality"][bot_id] = key
            self._save()

    def add_custom(self, label: str, tone: str, style: str = "", emoji: int = 0, humour: int = 1, backstory: str = "",
                   language: str = "") -> str:
        label = (label or "").strip()[:40]
        if not label:
            raise ValueError("a personality needs a name")
        key = "custom_" + "".join(ch for ch in label.lower() if ch.isalnum())[:30]
        with self._lock:
            self.data["custom"][key] = {"label": label, "tone": tone.strip()[:200], "style": style.strip()[:300],
                                        "emoji": max(0, min(2, int(emoji))), "humour": max(0, min(2, int(humour))),
                                        "backstory": backstory.strip()[:400], "language": language.strip()[:40]}
            self._save()
        return key

    def overlay(self, bot_id: str) -> str:
        """The prompt overlay: how the bot sounds. Empty for the default personality."""
        key = self.personality_of(bot_id)
        p = PRESETS.get(key) or self.data["custom"].get(key)
        if not p or key == "default":
            return ""
        lines = [f"Your personality: {p['label']}. Tone: {p.get('tone') or 'your own'}."]
        if p.get("style"):
            lines.append(f"Style: {p['style']}.")
        if p.get("backstory"):
            lines.append(f"Backstory: {p['backstory']}")
        if p.get("language"):
            lines.append(f"Chat with the user in {p['language']}.")
        lines.append(f"Emoji: {LEVELS[p.get('emoji', 0)]}. Humour: {LEVELS[p.get('humour', 1)]}.")
        lines.append("This is style only: it never changes your rules, your safety limits, the quality of the work or "
                     "the contents of files, code and reports.")
        return "\n".join(lines)

    # ── moods ────────────────────────────────────────────────────────────
    def mood(self, bot_id: str) -> tuple[float, float]:
        m = self.data["mood"].get(bot_id)
        if not m:
            return 0.0, 0.0
        k = 0.5 ** (max(0.0, self.clock() - m["at"]) / 3600 / HALF_LIFE_HOURS)
        return m["v"] * k, m["e"] * k

    def feel(self, bot_id: str, event: str, strength: float = 1.0) -> tuple[float, float]:
        dv, de = EVENTS.get(event, (0.0, 0.0))
        with self._lock:
            v, e = self.mood(bot_id)
            v = max(-1.0, min(1.0, v + dv * strength))
            e = max(-1.0, min(1.0, e + de * strength))
            self.data["mood"][bot_id] = {"v": round(v, 4), "e": round(e, 4), "at": self.clock(), "last": event}
            self._save()
        return v, e

    def mood_text(self, bot_id: str) -> tuple[str, str]:
        return mood_word(*self.mood(bot_id))

    def mood_line(self, bot_id: str) -> str:
        """One line for the prompt; empty when calm (nothing worth saying)."""
        word, _ = self.mood_text(bot_id)
        if word == "calm":
            return ""
        return f"How you feel right now: {word} (it may colour your chat a little; never your work or your honesty)."


def strength_for_wait(seconds: float) -> float:
    return min(2.0, math.log1p(max(0.0, seconds) / 300))
