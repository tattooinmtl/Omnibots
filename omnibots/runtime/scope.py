"""The project folder is the bot's world (PLAN.md A15.e.04; user, 2026-09-28).

"All they do must be based on the project they work on unless Omi runs an external command to the PC,
but this should be run with user permission to exit the project folder … to search the web it's ok, no
permission needed."

`outside_paths()` finds where a tool call would leave the bot's workspace (the project folder during a
job): a `path` argument outside it, or a path written into a shell command or Python code (C:\\…, \\\\server,
~\\…, %USERPROFILE%, $HOME, ..\\…). The agent then asks the user, whatever the risk class and the
`ask_from` setting. Web search and fetch never touch local paths, so they never ask.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

# a Windows drive path, a UNC share, ~/, %USERPROFILE%, $HOME or a ..\ climb, written in text
LOCAL_PATH = re.compile(r"""(?ix)(?<![\w/\\.])(
      [a-z]:[\\/][^\s'"|&<>;,)]*          # C:\Users\... or C:/Users/...
    | \\\\[A-Za-z0-9._-]+\\[A-Za-z0-9$._-]+[^\s'"|&<>;,)]*   # \\server\share (a real share; not an escaped \\n in code)
    | ~[\\/][^\s'"|&<>;,)]*               # ~\Documents
    | %userprofile%[^\s'"|&<>;,)]*        # %USERPROFILE%\...
    | \$home[^\s'"|&<>;,)]*               # $HOME/...
    | \.\.[\\/][^\s'"|&<>;,)]*            # ..\ out of the folder
)""")
TEXT_ARGS = {"run_shell": "command", "run_python": "code"}


def _expand(raw: str) -> Path:
    home = str(Path.home())
    s = re.sub(r"(?i)^%userprofile%", lambda _: home, raw)
    s = re.sub(r"(?i)^\$home", lambda _: home, s)
    return Path(os.path.expanduser(s))


def _outside(ws: Path, raw: str) -> bool:
    try:
        p = _expand(raw)
        p = (p if p.is_absolute() else ws / p).resolve()
    except (OSError, ValueError):
        return True                                   # can't tell where it goes: ask
    return not p.is_relative_to(ws)


def outside_paths(tool_name: str, args: dict[str, Any], workspace: Path) -> list[str]:
    """The places outside `workspace` this call would touch (empty = it stays in the project)."""
    ws = workspace.resolve()
    found: list[str] = []
    p = args.get("path")
    if p and _outside(ws, str(p)):
        found.append(str(p))
    field = TEXT_ARGS.get(tool_name)
    text = str(args.get(field) or "") if field else ""
    # The project's own absolute path is inside, even though its folder name has spaces (found live, A14.a.02:
    # "C:\…\projects\2026-09-28 Build a small…" was cut at the space and read as the parent folder).
    for form in {str(ws), ws.as_posix(), str(ws).replace("\\", "\\\\")}:
        text = re.sub(re.escape(form), ".", text, flags=re.I)
    for m in LOCAL_PATH.finditer(text):
        raw = m.group(1).rstrip(".")
        if raw and _outside(ws, raw) and raw not in found:
            found.append(raw)
    return found
