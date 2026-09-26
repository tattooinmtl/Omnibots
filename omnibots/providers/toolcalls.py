"""Port of Omni's src/core/toolcalls.mjs: text-protocol tool calls and <think>
splitting, for providers without native OpenAI tool calling (nvidia).

Behaviour-identical to Omni; tests/goldens/toolcalls.json is generated from
Omni's own code (tools/gen_toolcall_goldens.mjs) and the Python port must
reproduce it exactly. Regexes use re.ASCII because JS `\\w` is ASCII-only.

The canonical format models are asked to emit:
  <tool_call>
  <function=tool_name>
  <parameter=arg_name>value</parameter>
  </function>
  </tool_call>
In practice models drift (GLM arg_key/arg_value, Qwen JSON, hybrids, missing
closing tags); every one of those shapes is parsed so a tool attempt is never
mistaken for a final answer.
"""

from __future__ import annotations

import itertools
import json
import re
import time
from typing import Any

STRUCTURAL = {
    "tool_call", "tool_calls", "function", "functions", "parameter", "parameters",
    "param", "arg", "args", "arg_key", "arg_value", "invoke", "think", "thinking",
    "tool_response", "tool_result", "response",
}

Registry = dict[str, dict[str, str]]

_ids = itertools.count()


def build_param_registry(tool_defs: list[dict[str, Any]]) -> Registry:
    reg: Registry = {}
    for t in tool_defs or []:
        fn = t.get("function") or {}
        if not fn.get("name"):
            continue
        props = ((fn.get("parameters") or {}).get("properties") or {})
        reg[fn["name"]] = {k: (v or {}).get("type", "") if isinstance(v, dict) else "" for k, v in props.items()}
    return reg


def has_tool_intent(content: str) -> bool:
    return re.search(r"<tool_call|<function\s*=|<arg_key>|<parameter\s*=|<invoke\b", str(content or ""), re.I | re.A) is not None


def strip_think(content: str) -> str:
    rest = re.sub(r"<think>[\s\S]*?</think>", "", str(content or ""))
    i = rest.find("<think>")
    return rest[:i] if i != -1 else rest


def extract_think(content: str) -> dict[str, str]:
    think = []

    def grab(m: re.Match) -> str:
        think.append(m.group(1))
        return ""

    rest = re.sub(r"<think>([\s\S]*?)</think>", grab, str(content or ""))
    i = rest.find("<think>")
    if i != -1:
        think.append(rest[i + len("<think>"):])
        rest = rest[:i]
    return {"think": "".join(think), "rest": rest}


class ThinkSplitter:
    """Split a live token stream into think / answer text, tolerant of tags
    split across chunks (holds back a short tail until it can decide)."""

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self) -> None:
        self.buf = ""
        self.in_think = False

    def _scan(self) -> dict[str, str]:
        think, answer = "", ""
        while True:
            if not self.in_think:
                i = self.buf.find(self.OPEN)
                if i == -1:
                    safe = max(0, len(self.buf) - len(self.OPEN) + 1)
                    answer += self.buf[:safe]
                    self.buf = self.buf[safe:]
                    break
                answer += self.buf[:i]
                self.buf = self.buf[i + len(self.OPEN):]
                self.in_think = True
            else:
                i = self.buf.find(self.CLOSE)
                if i == -1:
                    safe = max(0, len(self.buf) - len(self.CLOSE) + 1)
                    think += self.buf[:safe]
                    self.buf = self.buf[safe:]
                    break
                think += self.buf[:i]
                self.buf = self.buf[i + len(self.CLOSE):]
                self.in_think = False
        return {"think": think, "answer": answer}

    def feed(self, token: str) -> dict[str, str]:
        self.buf += str(token or "")
        return self._scan()

    def flush(self) -> dict[str, str]:
        out = {"think": self.buf, "answer": ""} if self.in_think else {"think": "", "answer": self.buf}
        self.buf = ""
        return out


def strip_tool_call_text(content: str) -> str:
    s = re.sub(r"<tool_call>[\s\S]*?</tool_call>", "", str(content or ""))
    i = s.find("<tool_call>")
    if i != -1:
        s = s[:i]
    return re.sub(r"<function\s*=[^>]*>[\s\S]*?(?:</function>|$(?![\s\S]))", "", s)


# ── value helpers ─────────────────────────────────────────────────────────
def _json_parse(t: str) -> Any:
    """JSON.parse semantics for the values models emit."""
    return json.loads(t)


_SCALAR = re.compile(r"(true|false|null|-?\d+(\.\d+)?)", re.A)


def _coerce(raw: str, typ: str = "") -> Any:
    t = str(raw).strip()
    if t == "":
        return t
    if typ == "string":
        return t
    if typ in ("boolean", "integer", "number", "array", "object"):
        try:
            return _json_parse(t)
        except ValueError:
            return t
    if _SCALAR.fullmatch(t) or t[:1] in ("[", "{"):
        try:
            return _json_parse(t)
        except ValueError:
            return t
    return t


def _opener_at_line_start(src: str, idx: int) -> bool:
    i = idx - 1
    while i >= 0 and src[i] in " \t":
        i -= 1
    return i < 0 or src[i] in "\n\r"


def _closer_at_line_end(src: str, idx: int) -> bool:
    j = idx
    while j < len(src) and src[j] in " \t":
        j += 1
    return j >= len(src) or src[j] in "\n\r<"


def _unescape(v: Any) -> Any:
    return v.replace("\\</", "</") if isinstance(v, str) else v


def _normalize_name(raw: Any, registry: Registry | None) -> str | None:
    tokens = re.findall(r"[A-Za-z_][\w.-]*", str(raw or ""), re.A)
    if not tokens:
        return None
    if registry:
        for tok in tokens:
            if tok in registry:
                return tok
        lc = {k.lower(): k for k in registry}
        for tok in tokens:
            if tok.lower() in lc:
                return lc[tok.lower()]
    return tokens[0]


_TAG_RE = re.compile(r'<(/?)([A-Za-z_][\w-]*)(?:\s*=\s*"?([^>"\n]+?)"?|\s+name\s*=\s*"?([^>"\n]+?)"?)?\s*/?>', re.A)


def _parse_block(body: str, registry: Registry | None) -> dict[str, Any] | None:
    src = str(body or "").strip()
    if not src:
        return None

    # Qwen / JSON form
    if src.startswith("{"):
        try:
            obj = _json_parse(src)
            if isinstance(obj, dict):
                name = _normalize_name(obj.get("name") or obj.get("tool") or obj.get("function"), registry)
                if name:
                    raw_args = obj.get("arguments")
                    if raw_args is None:
                        raw_args = obj.get("parameters")
                    if raw_args is None:
                        raw_args = obj.get("args", {})
                    if isinstance(raw_args, str):
                        try:
                            raw_args = _json_parse(raw_args)
                        except ValueError:
                            raw_args = {}
                    return {"name": name, "args": raw_args if isinstance(raw_args, (dict, list)) else {}}
        except ValueError:
            pass

    st: dict[str, Any] = {"name": None, "mode": None, "key": None, "pending": None, "buf": "", "free": ""}
    args: dict[str, Any] = {}

    def params_for():
        n = st["name"]
        return registry.get(n) if (n and registry and n in registry) else None

    def type_of(k: str) -> str:
        p = params_for()
        return (p or {}).get(k, "") or ""

    def flush() -> None:
        if not st["name"] and st["free"].strip():
            st["name"] = _normalize_name(st["free"], registry)
        if st["mode"] == "param" and st["key"] is not None:
            args[st["key"]] = _unescape(_coerce(st["buf"], type_of(st["key"])))
        elif st["mode"] == "arg_key":
            st["pending"] = st["buf"].strip()
        elif st["mode"] == "arg_value" and st["pending"]:
            args[st["pending"]] = _unescape(_coerce(st["buf"], type_of(st["pending"])))
        st["mode"] = None
        st["key"] = None
        st["buf"] = ""

    last = 0
    for m in _TAG_RE.finditer(src):
        text = src[last:m.start()]
        full = m.group(0)
        closing, tag = m.group(1), m.group(2)
        attr = (m.group(3) or m.group(4) or "").strip()
        tag_lc = tag.lower()
        escaped = m.start() > 0 and src[m.start() - 1] == "\\"
        mode, key = st["mode"], st["key"]
        if closing:
            is_closer = tag_lc in STRUCTURAL or (mode == "param" and key is not None and tag_lc == key.lower())
            is_protocol = is_closer and not escaped and (mode is None or _closer_at_line_end(src, m.end()))
        elif escaped:
            is_protocol = False
        elif tag_lc in STRUCTURAL or attr:
            is_protocol = mode is None or _opener_at_line_start(src, m.start())
        elif mode is None:
            known = params_for()
            is_protocol = (tag in known) if known is not None else True
        else:
            is_protocol = False

        if not is_protocol:
            if st["mode"] is not None:
                st["buf"] += text + full
            else:
                st["free"] += text + full
            last = m.end()
            continue

        if st["mode"] is not None:
            st["buf"] += text
        else:
            st["free"] += text

        if closing:
            flush()
        elif tag_lc in ("function", "invoke") and attr:
            flush()
            st["name"] = st["name"] or _normalize_name(attr, registry)
        elif tag_lc in ("parameter", "param", "arg") and attr:
            flush()
            st["mode"], st["key"] = "param", attr
        elif tag_lc == "arg_key":
            flush()
            st["mode"] = "arg_key"
        elif tag_lc == "arg_value":
            flush()
            st["mode"] = "arg_value"
        elif tag_lc not in STRUCTURAL:
            flush()
            st["mode"], st["key"] = "param", tag
        else:
            flush()
        last = m.end()

    if st["mode"] is not None:
        st["buf"] += src[last:]
    else:
        st["free"] += src[last:]
    flush()

    name = st["name"] or _normalize_name(st["free"], registry)
    if not name:
        return None
    if not args:
        jm = re.search(r"\{[\s\S]*\}", st["free"])
        if jm:
            try:
                obj = _json_parse(jm.group(0))
                if isinstance(obj, dict):
                    inner = obj.get("arguments") if obj.get("arguments") is not None else obj
                    if isinstance(inner, dict):
                        args.update(inner)
            except ValueError:
                pass
    return {"name": name, "args": args}


def _is_envelope_close(src: str, idx: int) -> bool:
    if idx > 0 and src[idx - 1] == "\\":
        return False
    prev = src[idx - 1] if idx > 0 else ""
    if prev not in ("\n", "\r", ">", "}"):
        return False
    return _closer_at_line_end(src, idx + len("</tool_call>"))


def parse_text_tool_calls(content: str, registry: Registry | None = None) -> list[dict[str, Any]]:
    """Parse assistant text into OpenAI-format tool_calls."""
    calls: list[dict[str, Any]] = []
    src = strip_think(content)
    if "<" not in src:
        return calls
    blocks: list[str] = []
    rest = ""
    cursor = 0
    while True:
        open_ = src.find("<tool_call>", cursor)
        if open_ == -1:
            rest += src[cursor:]
            break
        body_start = open_ + len("<tool_call>")
        close = -1
        scan = body_start
        while True:
            scan = src.find("</tool_call>", scan)
            if scan == -1:
                break
            if _is_envelope_close(src, scan):
                close = scan
                break
            scan += 1
        if close == -1:
            rest += src[cursor:]
            break
        rest += src[cursor:open_] + "\n"
        blocks.append(src[body_start:close])
        cursor = close + len("</tool_call>")

    parts = rest.split("<tool_call>")
    if len(parts) > 1:
        blocks.extend(parts[1:])
        rest = parts[0]

    if not blocks:
        blocks.extend(m.group(0) for m in re.finditer(r"<function\s*=[^>]+>[\s\S]*?(?:</function>|$(?![\s\S]))", rest))

    stamp = int(time.time() * 1000)
    for block in blocks:
        parsed = _parse_block(block, registry)
        if not parsed:
            continue
        calls.append({
            "id": f"txt_{stamp}_{len(calls)}_{next(_ids)}",
            "type": "function",
            "function": {"name": parsed["name"], "arguments": json.dumps(parsed["args"], ensure_ascii=False, separators=(",", ":"))},
        })
    return calls


def text_tool_instructions(tool_defs: list[dict[str, Any]]) -> str:
    defs = []
    for t in tool_defs:
        fn = t.get("function") or {}
        d = {"name": fn.get("name")}
        if fn.get("description") is not None:       # JSON.stringify drops undefined fields
            d["description"] = fn["description"]
        d["parameters"] = fn.get("parameters") or {"type": "object", "properties": {}}
        defs.append(d)
    return "\n".join([
        "",
        "# Tool Calling Protocol",
        "This provider has no native tool calling. To use a tool, emit EXACTLY this XML (and close every tag):",
        "<tool_call>",
        "<function=tool_name>",
        "<parameter=argument_name>value</parameter>",
        "</function>",
        "</tool_call>",
        "",
        "Worked example — list a directory recursively:",
        "<tool_call>",
        "<function=list_dir>",
        "<parameter=path>src</parameter>",
        "<parameter=recursive>true</parameter>",
        "</function>",
        "</tool_call>",
        "",
        "Rules:",
        "- One <parameter=NAME>VALUE</parameter> line per argument; NAME is the argument name from the schema.",
        "- Plain string values are written as-is (no quotes). Booleans, numbers, arrays, and objects are written as JSON.",
        "- Values may contain other tags (HTML, XML) as-is. But if a value must contain a literal PROTOCOL closing tag (</parameter>, </function>, </tool_call>), escape it as \\</parameter> — the parser restores it.",
        "- Emit the tool call at the END of your message and output NOTHING after </tool_call>.",
        "- To call several tools at once, emit several complete <tool_call> blocks.",
        "- Never invent tool names. Never describe or explain the tool call.",
        "- After each tool result is returned, either call the next tool or give your final answer.",
        "- Do not stop working right after a tool call — the result always comes back to you.",
        "",
        "Available tools (JSON schemas):",
        json.dumps(defs, ensure_ascii=False, separators=(",", ":")),
    ])


def recovery_message() -> str:
    return "\n".join([
        "SYSTEM: Your tool call was malformed and could NOT be executed.",
        "Re-emit it now using EXACTLY this format, closing every tag:",
        "<tool_call>",
        "<function=tool_name>",
        "<parameter=argument_name>value</parameter>",
        "</function>",
        "</tool_call>",
        "If a value must contain a literal closing tag like </parameter>, escape it as \\</parameter>.",
        "Output only the corrected <tool_call> block(s) — no other text.",
    ])
