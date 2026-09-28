"""Omi's personality, linked to Omni (PLAN.md A11.d; user, 2026-09-26: "keep it linked to the
taunts from Omi in .omni so it keeps a similar humour").

Omni keeps Omi's voice in `src/ui.mjs`: FUNNY_WORDS (the "-ising" status verbs),
ACTION_LINES (cheer/grumble quips per action), IMPATIENT (long-wait lines). We read
those constants LIVE from Omni (read-only, never written), so edits there show up
here; a bundled snapshot (`omi_personality.json`) is the fallback when Omni isn't
installed or the file changed shape.

OmniBots adds pools for situations Omni doesn't have (approvals, seats, claims,
team events), written in the same voice; they're marked `origin: omnibots`.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SNAPSHOT = Path(__file__).with_name("omi_personality.json")
CONSTS = ("FUNNY_WORDS", "ACTION_LINES", "IMPATIENT")

# New situations, same voice (chipper, a little sarcastic, impatient). Not from Omni.
OMNIBOTS_LINES: dict[str, dict[str, list[str]]] = {
    "approval": {
        "cheer": ["the human said yes. we ride.", "approved. I'll try not to let it go to my head.", "green light. finally."],
        "grumble": ["denied. I'll just sit here, then.", "the human said no. respect. mild sulking.", "vetoed. back to the drawing board."],
        "waiting": ["waiting on your click. no pressure.", "your approval, whenever you're ready. I'll wait. patiently. ish.",
                    "one button between me and glory."],
    },
    "seat": {
        "cheer": ["got a MiniMax seat. let's cook.", "seat secured. elbows out."],
        "waiting": ["in line for a seat. like the DMV, but faster.", "all four seats taken. I'm next. probably.",
                    "queueing for compute. riveting."],
    },
    "claim": {
        "cheer": ["claim accepted. receipts matter.", "proof checked out. told you.", "evidence: airtight."],
        "grumble": ["claim bounced. fair, the proof was thin.", "rejected. back to show my work.", "no receipts, no glory."],
    },
    "team": {
        "cheer": ["new bot on the team. welcome, clone.", "goal done. report's on your desk.", "the whole crew delivered."],
        "grumble": ["panic button pressed. everybody freeze.", "team paused. we'll pretend that was planned.",
                    "someone got stuck in a loop. not naming names."],
    },
}

# OmniBots events -> Omni's action pools
TOOL_ACTION = {
    "write_file": "coding", "git_commit": "coding", "git_diff": "coding", "create_tool": "coding",
    "run_python": "running", "run_shell": "running", "git_push": "running",
    "read_file": "reading", "list_dir": "reading", "grep": "reading", "find_files": "reading",
    "invoke_skill": "reading", "find_skill": "searching",
    "web_search": "searching", "web_fetch": "searching", "browser_navigate": "searching", "browser_read": "reading",
    "computer_run": "running", "computer_click": "searching", "computer_type": "coding",
}

GRAWLIX = ["@#%$", "@$#%%", "^%&$", "$%#%"]          # the user's thought-bubble symbols (A11.d)


def _block(text: str, name: str) -> str | None:
    """The literal after `const NAME =`, found by bracket matching (strings respected)."""
    m = re.search(rf"\bconst\s+{name}\s*=\s*", text)
    if not m:
        return None
    i = m.end()
    if i >= len(text) or text[i] not in "[{":
        return None
    depth, quote, j = 0, None, i
    while j < len(text):
        ch = text[j]
        if quote:
            if ch == "\\":
                j += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'`":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth == 0:
                return text[i:j + 1]
        j += 1
    return None


def _js_to_json(src: str) -> Any:
    """A JS object/array literal (strings, numbers, nested, bare keys, comments, trailing
    commas) -> Python. A tiny tokenizer, so apostrophes inside strings are safe."""
    out, i, n = [], 0, len(src)
    while i < n:
        ch = src[i]
        if ch in "\"'`":                                  # a string: re-emit as a JSON string
            j, buf = i + 1, []
            while j < n and src[j] != ch:
                if src[j] == "\\" and j + 1 < n:
                    esc = src[j + 1]
                    buf.append({"n": "\n", "t": "\t", "r": "\r"}.get(esc, esc))
                    j += 2
                    continue
                buf.append(src[j])
                j += 1
            out.append(json.dumps("".join(buf), ensure_ascii=False))
            i = j + 1
        elif src.startswith("//", i):                    # line comment
            while i < n and src[i] != "\n":
                i += 1
        elif src.startswith("/*", i):                    # block comment
            i = src.find("*/", i + 2)
            i = n if i < 0 else i + 2
        elif ch.isalpha() or ch == "_":                  # identifier: a bare key, or true/false/null
            j = i
            while j < n and (src[j].isalnum() or src[j] == "_"):
                j += 1
            word = src[i:j]
            k = j
            while k < n and src[k] in " \t\r\n":
                k += 1
            out.append(json.dumps(word) if k < n and src[k] == ":" else word)
            i = j
        else:
            out.append(ch)
            i += 1
    s = re.sub(r",(\s*[\]}])", r"\1", "".join(out))       # trailing commas (only structure is left)
    return json.loads(s)


def read_omni(ui_mjs: Path) -> dict[str, Any]:
    text = ui_mjs.read_text(encoding="utf-8")
    out = {}
    for name in CONSTS:
        blk = _block(text, name)
        if blk is None:
            raise ValueError(f"{name} not found in {ui_mjs}")
        out[name] = _js_to_json(blk)
    if not all(isinstance(out[k], list) for k in ("FUNNY_WORDS", "IMPATIENT")) or not isinstance(out["ACTION_LINES"], dict):
        raise ValueError("Omi's personality constants changed shape")
    return out


def load(omni_root: Path | None) -> dict[str, Any]:
    """Omi's voice: live from Omni when possible, else the bundled snapshot."""
    data, source = None, "snapshot"
    if omni_root is not None:
        ui = Path(omni_root) / "src" / "ui.mjs"
        if ui.exists():
            try:
                data, source = read_omni(ui), str(ui)
            except Exception as exc:
                log.warning("Omi's personality couldn't be read from %s (%s); using the snapshot", ui, exc)
    if data is None:
        data = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    data = dict(data)
    data["source"] = source
    data["OMNIBOTS_LINES"] = OMNIBOTS_LINES
    return data


def write_snapshot(omni_root: Path) -> Path:
    """Maintenance, not called by the app: refresh the bundled fallback from Omni after Omni's
    `src/ui.mjs` changes. Run: python -c "from pathlib import Path; from omnibots.omni.personality import
    write_snapshot; print(write_snapshot(Path.home() / '.omni'))" (point it at Omni's install root)."""
    data = read_omni(Path(omni_root) / "src" / "ui.mjs")
    SNAPSHOT.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return SNAPSHOT
