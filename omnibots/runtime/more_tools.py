"""The rest of the core tools (PLAN.md A8.b.01).

  grep, find_files     search the workspace (R0)
  run_shell            a shell command in the S0 sandbox environment, cwd = the
                       workspace (R1). Risky commands RAISE the class, via a port
                       of Omni's commandRisk: destructive / system / security
                       changes → R5, installs / docker / pushes → R3.
  git_status, git_diff, git_commit   (R0 / R0 / R1)
  lock_file, unlock_file             leases so two bots never edit the same file (A4)
  ask_help                           a HELP_REQUEST to the boss (workers can't ask other workers)
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
from pathlib import Path
from typing import Any

from omnibots.runtime.tools import Tool, ToolContext

MAX_HITS = 200

# Port of Omni's BLOCKED_PATTERNS / elevated list (src/tools/index.mjs commandRisk).
R5_PATTERNS = [
    # deleting anything asks first (user, 2026-09-26: "only delete, rm, destructive commands ... should prompt")
    (r"\bgit\s+push\b[^\n]*(\s--force(-with-lease)?\b|\s-f\b|\s\+\S)", "force-pushes (rewrites the remote's history)"),
    (r"\bgit\s+branch\s+[^\n]*(-d|--delete)\b", "deletes a git branch"),
    (r"\b(drop\s+(table|database|schema)|truncate\s+table)\b", "deletes database data"),
    (r"\bmkfs\b", "formats a disk"),
    (r"\brm\s+(-[^\n]*r|--recursive)", "recursively deletes files"),
    (r"\bremove-item\b[^\n]*(\s-r|\s-recurse|recursive)", "recursively deletes files"),
    (r"\brmdir\b[^\n]*(/s|-r|--recursive)", "recursively deletes a directory"),
    (r"\bdel\b[^\n]*(/s|/q)", "force/recursively deletes files"),
    (r"\bgit\s+(reset\s+--hard|clean\s+-[^\n]*[xfd])", "discards uncommitted work"),
    (r"\bformat\b\s+[a-z]:", "formats a disk volume"),
    (r"\bshutdown\b", "shuts down or restarts the computer"),
    (r"\breg(?:\.exe)?\s+(add|import|copy|restore|delete)\b", "modifies the Windows Registry"),
    (r"\bregedit\b[^\n]*/s\b", "imports a .reg file into the Registry"),
    (r"\b(set|new|remove)-itemproperty\b[^\n]*\bhk(lm|cu|cr|u|cc)\b", "modifies the Windows Registry"),
    (r"\b(new|remove)-item\b[^\n]*\bhk(lm|cu|cr|u|cc):", "modifies the Windows Registry"),
    (r"\bschtasks\b[^\n]*/create\b", "creates a scheduled task (persistence)"),
    (r"\bsc(?:\.exe)?\s+create\b", "creates a Windows service (persistence)"),
    (r"\bnew-service\b", "creates a Windows service (persistence)"),
    (r"\bset-executionpolicy\b", "changes PowerShell's execution policy"),
    (r"\bset-mppreference\b", "changes Windows Defender settings"),
    (r"\bnetsh\s+advfirewall", "modifies Windows Firewall rules"),
    (r"\bnew-netfirewallrule\b", "adds a firewall rule"),
    (r"\bbcdedit\b", "modifies the boot configuration"),
    # the general delete rules last, so a more precise reason above wins
    (r"(^|[;&|(]|\bsudo|\bxargs|\bthen|\bdo)\s*(rm|del|erase|rmdir|rd|unlink|shred|ri)(\s|$)", "deletes files"),
    (r"\bgit\s+rm\b", "deletes files"),
    (r"\bfind\b[^\n]*\s-delete\b", "deletes files"),
    (r"\bremove-item\b", "deletes files"),
]
R3_PATTERNS = [
    (r"\bnpm\s+(install|i)\b", "installs packages"), (r"\bpnpm\s+(install|add)\b", "installs packages"),
    (r"\byarn\s+(install|add)\b", "installs packages"), (r"\bpip\s+install\b", "installs packages"),
    (r"\bdocker\s+(run|compose|build|pull|push)\b", "runs or publishes containers"),
    (r"\bgh\s+pr\s+(merge|close)\b", "merges or closes a pull request"),
    (r"\bgit\s+push\b", "pushes to a remote"), (r"\b(curl|wget|invoke-webrequest|iwr)\b", "reaches the network"),
]


def command_risk(command: str) -> tuple[str, str]:
    c = str(command or "").lower()
    for pat, why in R5_PATTERNS:
        if re.search(pat, c):
            return "R5", why
    for pat, why in R3_PATTERNS:
        if re.search(pat, c):
            return "R3", why
    return "R1", "no high-risk pattern"


def _ws(ctx: ToolContext) -> Path:
    return ctx.workspace.resolve()


def _inside(ctx: ToolContext, p: str | None) -> Path:
    path = (_ws(ctx) / (p or ".")).resolve()
    if not path.is_relative_to(_ws(ctx)):
        raise ValueError("path is outside your workspace")
    return path


def _files(root: Path, pattern: str = "*"):
    for p in root.rglob("*"):
        if p.is_file() and ".git" not in p.parts and fnmatch.fnmatch(p.name, pattern):
            yield p


async def grep(args: dict[str, Any], ctx: ToolContext) -> str:
    try:
        root = _inside(ctx, args.get("path"))
        rx = re.compile(str(args.get("pattern") or ""), 0 if args.get("case_sensitive") else re.I)
    except (ValueError, re.error) as exc:
        return f"ERROR: {exc}"
    hits = []
    for f in (_files(root, str(args.get("glob") or "*")) if root.is_dir() else [root]):
        try:
            for n, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{f.relative_to(_ws(ctx)).as_posix()}:{n}: {line.strip()[:200]}")
                    if len(hits) >= MAX_HITS:
                        return "\n".join(hits) + f"\n…[stopped at {MAX_HITS} matches]"
        except OSError:
            continue
    return "\n".join(hits) or "no matches"


async def find_files(args: dict[str, Any], ctx: ToolContext) -> str:
    try:
        root = _inside(ctx, args.get("path"))
    except ValueError as exc:
        return f"ERROR: {exc}"
    found = [p.relative_to(_ws(ctx)).as_posix() for p in _files(root, str(args.get("pattern") or "*"))][:MAX_HITS]
    return "\n".join(found) or "no files match"


async def run_shell(args: dict[str, Any], ctx: ToolContext) -> str:
    if ctx.sandbox is None:
        return "ERROR: no sandbox available"
    command = str(args.get("command") or "").strip()
    if not command:
        return "ERROR: run_shell needs a command"
    timeout = min(float(args.get("timeout_seconds") or 120), 600)
    await ctx.event("terminal", f"$ {command}")
    # Network only for R3 commands (installs, git push, downloads): the user approved those before they run.
    res = await ctx.sandbox.run(command, cwd=_ws(ctx), timeout=timeout, network=command_risk(command)[0] == "R3",
                                on_output=lambda line: ctx.event("terminal", line))
    await ctx.event("terminal", f"[{'timed out' if res.timed_out else f'exit {res.exit_code}'} · {res.seconds:.1f}s]")
    ctx.record_run(command, res)
    return res.as_tool_result()


def _git(ctx: ToolContext, *a: str, timeout: float = 60) -> subprocess.CompletedProcess:
    """git runs OUTSIDE the sandbox (it can't run in the AppContainer), so it is hardened: hooks off and
    the repo's config checked for anything that runs a program (A9.a.03)."""
    from omnibots.runtime.safegit import GitUnsafe, git
    try:
        return git(_ws(ctx), *a, timeout=timeout)
    except GitUnsafe as exc:
        return subprocess.CompletedProcess(["git", *a], 1, "", f"ERROR: {exc}")


async def git_status(args: dict[str, Any], ctx: ToolContext) -> str:
    r = _git(ctx, "status", "--short", "--branch")
    return r.stdout.strip() or r.stderr.strip() or "clean"


async def git_diff(args: dict[str, Any], ctx: ToolContext) -> str:
    r = _git(ctx, "diff", *(["--staged"] if args.get("staged") else []), "--", *(args.get("paths") or []))
    out = r.stdout or r.stderr or "no changes"
    return out[:20000] + ("\n…[diff truncated]" if len(out) > 20000 else "")


async def git_commit(args: dict[str, Any], ctx: ToolContext) -> str:
    msg = str(args.get("message") or "").strip()
    if not msg:
        return "ERROR: git_commit needs a message"
    _git(ctx, "add", "-A")
    r = _git(ctx, "-c", f"user.name={ctx.bot_id}", "-c", "user.email=bots@omnibots.local", "commit", "-q", "-m", msg)
    if r.returncode != 0:
        return (r.stdout + r.stderr).strip() or "nothing to commit"
    return "committed " + _git(ctx, "rev-parse", "--short", "HEAD").stdout.strip()


async def git_push(args: dict[str, Any], ctx: ToolContext) -> str:
    remote = str(args.get("remote") or "origin").strip()
    branch = str(args.get("branch") or "").strip() or _git(ctx, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if not remote or remote.startswith("-") or not branch or branch.startswith("-"):
        return "ERROR: git_push needs a remote and a branch (not options)"
    await ctx.event("terminal", f"$ git push {remote} {branch}")
    r = _git(ctx, "push", remote, f"{branch}:{branch}", timeout=300)
    out = (r.stdout + r.stderr).strip()
    for line in out.splitlines():
        await ctx.event("terminal", line)
    await ctx.event("terminal", f"[exit {r.returncode}]")
    return (out or "pushed") if r.returncode == 0 else f"ERROR: push failed (exit {r.returncode}): {out[:2000]}"


async def rehearse_push(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    remote = str(args.get("remote") or "origin")
    branch = str(args.get("branch") or "") or _git(ctx, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    url = _git(ctx, "remote", "get-url", remote).stdout.strip() or remote
    ahead = _git(ctx, "log", "--oneline", "-n", "20", branch, "--not", "--remotes").stdout.strip()
    return {"remote": remote, "url": url, "branch": branch, "commits": ahead.splitlines()[:20] or ["(nothing new, or remote unknown)"]}


async def rehearse_shell(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    command = str(args.get("command") or "")
    risk, why = command_risk(command)
    return {"command": command, "cwd": str(_ws(ctx)), "risk": risk, "why": why, "network": risk == "R3",
            "sandbox": getattr(ctx.sandbox, "level", "none"), "timeout_seconds": min(float(args.get("timeout_seconds") or 120), 600)}


def core_extra_tools() -> list[Tool]:
    s = {"type": "string"}
    return [
        Tool("grep", "Search file contents in your workspace with a regex. Optional path and glob (e.g. *.py).",
             {"type": "object", "properties": {"pattern": s, "path": s, "glob": s, "case_sensitive": {"type": "boolean"}}, "required": ["pattern"]},
             "R0", grep, path_arg=None, summary=lambda a: f"grep /{a.get('pattern')}/"),
        Tool("find_files", "List files in your workspace matching a name pattern (e.g. *.md).",
             {"type": "object", "properties": {"pattern": s, "path": s}}, "R0", find_files, path_arg=None,
             summary=lambda a: f"find_files {a.get('pattern', '*')}"),
        Tool("run_shell", "Run a Windows shell (cmd) command in your workspace, with no secrets in its environment. Risky commands need the user's approval.",
             {"type": "object", "properties": {"command": s, "timeout_seconds": {"type": "integer"}}, "required": ["command"]},
             "R1", run_shell, timeout=620, path_arg=None, classify=lambda a, c: command_risk(a.get("command", ""))[0],
             rehearse=rehearse_shell,
             summary=lambda a: f"run_shell: {str(a.get('command', ''))[:80]}" + (
                 f" ({command_risk(a.get('command', ''))[1]})" if command_risk(a.get("command", ""))[0] != "R1" else "")),
        Tool("git_status", "git status of your workspace.", {"type": "object", "properties": {}}, "R0", git_status, path_arg=None),
        Tool("git_diff", "git diff of your workspace (optionally staged, or some paths).",
             {"type": "object", "properties": {"staged": {"type": "boolean"}, "paths": {"type": "array", "items": s}}}, "R0", git_diff, path_arg=None),
        Tool("git_push", "Push a branch of your workspace repository to a remote (a deploy). The user approves each push.",
             {"type": "object", "properties": {"remote": s, "branch": s}}, "R3", git_push, timeout=320, path_arg=None,
             rehearse=rehearse_push, summary=lambda a: f"git_push {a.get('remote', 'origin')} {a.get('branch', '(current)')}"),
        Tool("git_commit", "Commit all changes in your workspace with a message.",
             {"type": "object", "properties": {"message": s}, "required": ["message"]}, "R1", git_commit, path_arg=None,
             summary=lambda a: f"git_commit \"{str(a.get('message', ''))[:60]}\""),
    ]


def board_tools(*, locks, bus, boss_id: str) -> list[Tool]:
    """lock_file / unlock_file (leases, A4) and ask_help (a HELP_REQUEST to the boss)."""
    from omnibots.board.types import topic_bot
    s = {"type": "string"}

    async def lock_file(args: dict[str, Any], ctx: ToolContext) -> str:
        res = f"file:{(ctx.workspace / str(args.get('path') or '')).resolve().as_posix().lower()}"
        ok = await locks.acquire(res, ctx.bot_id, ttl=float(args.get("minutes") or 10) * 60, timeout=float(args.get("wait_seconds") or 120))
        return f"locked {args.get('path')} for you" if ok else f"ERROR: {args.get('path')} is locked by {await locks.holder(res)}; try later or ask_help"

    async def unlock_file(args: dict[str, Any], ctx: ToolContext) -> str:
        res = f"file:{(ctx.workspace / str(args.get('path') or '')).resolve().as_posix().lower()}"
        return "unlocked" if await locks.release(res, ctx.bot_id) else "you don't hold that lock"

    async def ask_help(args: dict[str, Any], ctx: ToolContext) -> str:
        text = str(args.get("text") or "").strip()
        if not text:
            return "ERROR: ask_help needs text"
        await bus.publish(topic_bot(boss_id), "HELP_REQUEST", {"text": text}, sender_type="bot", sender_id=ctx.bot_id,
                          recipient_id=boss_id, job_id=ctx.job_id)
        return f"asked {boss_id} for help; keep working on what you can, or wait_for_mention for the answer"

    return [
        Tool("lock_file", "Lock a file before editing it so no other bot edits it at the same time (waits if another bot holds it).",
             {"type": "object", "properties": {"path": s, "minutes": {"type": "integer"}, "wait_seconds": {"type": "integer"}}, "required": ["path"]},
             "R0", lock_file, timeout=700, path_arg=None),
        Tool("unlock_file", "Release a file lock you hold.", {"type": "object", "properties": {"path": s}, "required": ["path"]},
             "R0", unlock_file, path_arg=None),
        Tool("ask_help", f"Ask the boss ({boss_id}) for help when you're blocked or missing something.",
             {"type": "object", "properties": {"text": s}, "required": ["text"]}, "R0", ask_help, path_arg=None),
    ]
