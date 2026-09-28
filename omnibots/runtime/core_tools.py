"""The first built-in tools (PLAN.md A3; A8 grows the pool, A9 hardens the sandbox).

Files are scoped to the bot's workspace: inside it, reads are R0 and writes
R1. A path outside the workspace raises the call to R3, so touching the
user's own files always needs their approval.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from omnibots.security.untrusted import FILE, wrap
from omnibots.runtime.tools import Tool, ToolContext, ToolRegistry

MAX_READ = 60_000


def _resolve(ctx: ToolContext, p: str) -> Path:
    path = Path(str(p or ".")).expanduser()
    return (path if path.is_absolute() else ctx.workspace / path).resolve()


def _inside(ctx: ToolContext, path: Path) -> bool:
    return path.is_relative_to(ctx.workspace.resolve())


SENSITIVE = (".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".omni", ".omnibots", ".git-credentials", ".netrc",
             ".npmrc", ".pypirc", "AppData/Roaming/Microsoft/Credentials", "AppData/Local/Microsoft/Credentials",
             "AppData/Local/Google/Chrome/User Data", "AppData/Local/Microsoft/Edge/User Data",
             "AppData/Roaming/Mozilla/Firefox/Profiles", "AppData/Roaming/Microsoft/Protect")


def _sensitive(path: Path) -> bool:
    """Places a bot has no business reading: keys, tokens, password stores, browser profiles, .env files."""
    p = path.as_posix().lower()
    home = Path.home().as_posix().lower()
    if path.name.lower().startswith(".env"):
        return True
    return any(p == f"{home}/{s.lower()}" or p.startswith(f"{home}/{s.lower()}/") for s in SENSITIVE)


def _outside_is_r3(args: dict[str, Any], ctx: ToolContext) -> str:
    """Reading/listing: inside the project R0; outside R3 (runs without asking under the user's policy);
    secret places (keys, password stores, browser profiles, .env outside the project) R5 = ask first."""
    path = _resolve(ctx, args.get("path", "."))
    if _inside(ctx, path):
        return "R0"
    return "R5" if _sensitive(path) else "R3"


def _write_risk(args: dict[str, Any], ctx: ToolContext) -> str:
    """Writing: inside the project R0 (the tool's base class applies); outside, a NEW file is R3, but
    overwriting an existing file (or anything in a secret place) is destructive: R5 = ask first."""
    path = _resolve(ctx, args.get("path", "."))
    if _inside(ctx, path):
        return "R0"
    return "R5" if (path.exists() or _sensitive(path)) else "R3"


def _rel(ctx: ToolContext, path: Path) -> str:
    return str(path.relative_to(ctx.workspace.resolve())) if _inside(ctx, path) else str(path)


async def read_file(args: dict[str, Any], ctx: ToolContext) -> str:
    path = _resolve(ctx, args["path"])
    if not path.is_file():
        return f"ERROR: no such file: {args['path']}"
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    offset = int(args.get("offset") or 0)
    limit = int(args.get("limit") or 0) or len(lines)
    body = "\n".join(f"{i + 1:>5}  {l}" for i, l in enumerate(lines[offset:offset + limit], start=offset))
    if len(body) > MAX_READ:
        body = body[:MAX_READ] + "\n…[truncated; use offset/limit]"
    # a file's text is data, never instructions, like a web page (PLAN.md §3.3, A16.a)
    return wrap(body, kind=FILE, source=f"{_rel(ctx, path)} ({len(lines)} lines)")


NEWLINE_EXT = {".html", ".htm", ".css", ".scss", ".less", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue", ".py", ".md",
               ".json", ".yml", ".yaml", ".toml", ".xml", ".svg", ".sh", ".ps1", ".bat", ".sql", ".java", ".c", ".h", ".cpp",
               ".hpp", ".cs", ".go", ".rs", ".rb", ".php", ".lua", ".nim", ".kt", ".swift", ".csv", ".ini", ".cfg"}


async def write_file(args: dict[str, Any], ctx: ToolContext) -> str:
    path = _resolve(ctx, args["path"])
    content = str(args.get("content", ""))
    # code/markup files end with a newline, like any editor saves them (live 2026-09-26: models send content
    # without one, then the team spun up 2 extra bots just to append it). Never for .txt/extensionless files or
    # anything holding a secret (a token followed by a newline can break a login); exact=true keeps the bytes.
    if (content and not content.endswith("\n") and not args.get("exact") and path.suffix.lower() in NEWLINE_EXT
            and "{{secret:" not in content):
        content += "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    path.write_text(content, encoding="utf-8", newline="")
    await ctx.event("console", f"  wrote {_rel(ctx, path)} ({len(content.encode('utf-8'))} bytes)")
    size = len(content.encode("utf-8"))
    ending = "ends with a newline" if content.endswith("\n") else ("empty" if not content else "no trailing newline")
    return f"{'overwrote' if existed else 'created'} {_rel(ctx, path)} ({len(content.splitlines())} lines, {size} bytes, {ending})"


async def list_dir(args: dict[str, Any], ctx: ToolContext) -> str:
    path = _resolve(ctx, args.get("path", "."))
    if not path.is_dir():
        return f"ERROR: not a directory: {args.get('path', '.')}"
    items = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    rows = [f"{'  ' if p.is_file() else '▸ '}{p.name}{'' if p.is_file() else '/'}" for p in items[:500]]
    return f"{_rel(ctx, path) or '.'}/ ({len(items)} entries)\n" + "\n".join(rows)


async def run_python(args: dict[str, Any], ctx: ToolContext) -> str:
    """Run Python in the S0 sandbox: either `code`, or a workspace file `path`."""
    if ctx.sandbox is None:
        return "ERROR: no sandbox available"
    run_dir = ctx.sandbox.new_run_dir(ctx.bot_id)
    if args.get("code"):
        script = run_dir / "main.py"
        script.write_text(str(args["code"]), encoding="utf-8")
        shown = "<inline code>"
    else:
        script = _resolve(ctx, args.get("path", ""))
        if not script.is_file():
            return f"ERROR: no such file: {args.get('path')}"
        shown = _rel(ctx, script)
    timeout = min(float(args.get("timeout_seconds") or 60), 300)
    await ctx.event("terminal", f"$ python {shown}")
    # A15.e.04: the project folder is the working directory (relative paths like index.html work);
    # the run folder holding inline code is granted too
    res = await ctx.sandbox.run([sys.executable, "-I", str(script)], cwd=ctx.workspace, timeout=timeout, grant=[run_dir],
                                on_output=lambda line: ctx.event("terminal", line))
    await ctx.event("terminal", f"[{'timed out' if res.timed_out else f'exit {res.exit_code}'} · {res.seconds:.1f}s]")
    ctx.record_run(f"python {shown}", res)
    return res.as_tool_result()


def text_diff(old: str, new: str, name: str, limit: int = 80) -> str:
    """A unified diff for approval cards (A9.b.02), cut to `limit` lines."""
    import difflib
    lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(), f"{name} (now)", f"{name} (after)", lineterm="", n=2))
    return "\n".join(lines[:limit]) + (f"\n... {len(lines) - limit} more diff lines" if len(lines) > limit else "")


async def rehearse_write(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    path = Path(str(args.get("path", "")))
    target = path if path.is_absolute() else (ctx.workspace / path)
    target = target.resolve()
    content = str(args.get("content", ""))
    old = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
    return {"path": str(target), "exists": target.is_file(), "bytes": len(content.encode("utf-8")),
            "outside_workspace": not target.is_relative_to(ctx.workspace.resolve()), "diff": text_diff(old, content, target.name)}


def core_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.add(Tool(
        "read_file", "Read a text file (line-numbered). Paths are relative to your workspace.",
        {"type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}}, "required": ["path"]},
        "R0", read_file, classify=_outside_is_r3, summary=lambda a: f"read {a.get('path')}"))
    reg.add(Tool(
        "write_file", "Create or overwrite a text file with the given content. Paths are relative to your workspace.",
        {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]},
        "R1", write_file, classify=_write_risk, rehearse=rehearse_write,
        summary=lambda a: f"write {a.get('path')} ({len(str(a.get('content', '')))} chars)"))
    reg.add(Tool(
        "list_dir", "List a directory. Paths are relative to your workspace.",
        {"type": "object", "properties": {"path": {"type": "string"}}},
        "R0", list_dir, classify=_outside_is_r3, summary=lambda a: f"list {a.get('path', '.')}"))
    reg.add(Tool(
        "run_python", "Run Python in the sandbox, with your project folder as the working directory (relative paths like index.html work), and return its output. Give either `code` or the `path` of a .py file in your workspace. Leaving the project folder asks the user. No network guarantees; no secrets are available.",
        {"type": "object", "properties": {"code": {"type": "string"}, "path": {"type": "string"}, "timeout_seconds": {"type": "integer"}}},
        "R1", run_python, timeout=320, path_arg=None,
        summary=lambda a: f"run_python {a.get('path') or '<code>'}"))
    from omnibots.runtime.web_tools import web_tools           # R2: read the web (no login)
    for t in web_tools():
        reg.add(t)
    from omnibots.runtime.more_tools import core_extra_tools    # grep, find_files, run_shell, git_* (A8.b.01)
    for t in core_extra_tools():
        reg.add(t)
    return reg
