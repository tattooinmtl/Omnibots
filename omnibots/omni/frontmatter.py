"""Port of Omni's src/core/frontmatter.mjs (SKILL.md frontmatter).

Kept behaviour-identical, including its fixes: CRLF is normalized first (a
trailing \\r broke every key on Windows), block scalars (| |- > >-) and a bare
`key:` followed by indented lines fold into one line, and YAML quoting rules
for single- and double-quoted values.
"""

from __future__ import annotations

import re

# JS regex semantics, exactly: JS `.` does NOT match \r, U+2028 or
# (Python's does), JS `\w` is ASCII-only, and JS `$` without the m flag is the
# very end of the string (Python's `$` also matches before a final \n, hence
# \Z). Getting any of these wrong makes a file with stray \r characters parse
# differently from Omni, which the parity test caught.
_DOT = "[^\n\r\u2028\u2029]"
_FM = re.compile(r"^---\s*\n([\s\S]*?)\n---\s*\n?([\s\S]*)\Z")
_KV = re.compile(r"^([A-Za-z0-9_-]+):\s*(" + _DOT + r"*)\Z")


def _unquote(val: str) -> str:
    s = str(val).strip()
    if len(s) >= 2 and s[0] == "'" and s.endswith("'"):
        return s[1:-1].replace("''", "'").strip()
    if len(s) >= 2 and s[0] == '"' and s.endswith('"'):
        def esc(m: re.Match) -> str:
            ch = m.group(1)
            return {"n": "\n", "t": "\t", "r": "\r"}.get(ch, ch)
        return re.sub(r"\\(.)", esc, s[1:-1]).strip()
    return s


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    src = str(text or "").replace("\r\n", "\n")
    m = _FM.match(src)
    if not m:
        return {}, src
    meta: dict[str, str] = {}
    raw = m.group(1).split("\n")
    i = 0
    while i < len(raw):
        kv = _KV.match(raw[i])
        if not kv:
            i += 1
            continue
        key, val = kv.group(1), kv.group(2)
        is_block = val in ("|", "|-", ">", ">-")
        nxt = raw[i + 1] if i + 1 < len(raw) else ""
        is_continuation = val == "" and re.match(r"^\s+\S", nxt) is not None
        if is_block or is_continuation:
            block = []
            i += 1
            while i < len(raw):
                line = raw[i]
                if line == "" or re.match(r"^\s", line):
                    block.append(re.sub(r"^\s+", "", line))
                    i += 1
                else:
                    break
            val = re.sub(r"\s+", " ", " ".join(block)).strip()
        else:
            i += 1
        meta[key] = _unquote(val)
    return meta, m.group(2).strip()
