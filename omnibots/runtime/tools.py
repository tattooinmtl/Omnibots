"""Tools and risk classes (PLAN.md ADR-8, §3.1).

A tool declares a base risk class. It may also classify a specific call
HIGHER from its arguments (writing outside the bot's workspace is R3, not
R1); the runtime takes the max of the two, so a tool can never lower its own
class. Every tool result is a string the model sees.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

RISK_ORDER = ["R0", "R1", "R2", "R3", "R4", "R5"]
RISK_TEXT = {
    "R0": "read local", "R1": "write local / run in the sandbox", "R2": "read the web",
    "R3": "act as the user online / run live", "R4": "money or legal commitment", "R5": "destructive or irreversible",
}


def args_digest(args: dict[str, Any]) -> str:
    """Stable fingerprint of a call's arguments: an approval covers exactly this call (A9.b.03)."""
    import hashlib
    return hashlib.sha256(json.dumps(args, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def call_target(tool: "Tool", args: dict[str, Any], ctx: "ToolContext | None" = None):
    """A tool's secret_target accepts (args) or (args, ctx); this calls whichever it declares."""
    fn = tool.secret_target
    if fn is None:
        return args.get("url")
    try:
        return fn(args, ctx)
    except TypeError:
        return fn(args)


def target_host(tool: "Tool", args: dict[str, Any], ctx: "ToolContext | None" = None) -> str | None:
    from urllib.parse import urlparse
    t = call_target(tool, args, ctx)
    return (urlparse(str(t)).hostname if t and "://" in str(t) else (str(t) if t else None))


def risk_max(a: str, b: str) -> str:
    return a if RISK_ORDER.index(a) >= RISK_ORDER.index(b) else b


@dataclass
class ToolContext:
    bot_id: str
    workspace: Path                                   # the bot's own folder; files are scoped here
    job_id: str | None = None
    emit: Callable[[str, str], Awaitable[None]] | None = None   # (kind, content) -> bot_events
    sandbox: Any = None                               # runtime.sandbox.Sandbox
    runs: list[dict[str, Any]] = field(default_factory=list)   # commands really run in this job (A4.a.08)
    waiting_on_user: Any = None                       # () -> context manager: the time budget stops meanwhile
    saw_outside: bool = False                         # A15.e.01: web or browser text reached this job (remember() marks notes)

    async def event(self, kind: str, content: str) -> None:
        if self.emit:
            await self.emit(kind, content)

    def record_run(self, command: str, result: Any) -> None:
        """Remember a command this bot really ran, so a claim can only cite real runs."""
        self.runs.append({"command": command, "exit_code": None if result.timed_out else result.exit_code,
                          "timed_out": bool(result.timed_out), "output": (result.output or "")[-8000:]})


ToolFn = Callable[[dict[str, Any], ToolContext], Awaitable[str]]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    risk: str
    fn: ToolFn
    timeout: float = 120.0
    classify: Callable[[dict[str, Any], ToolContext], str] | None = None   # may raise the class for a call
    summary: Callable[[dict[str, Any]], str] | None = None                # one line for console / approvals
    path_arg: str | None = "path"                                          # file lock key (same-file calls serialize)
    secret_args: tuple[str, ...] = ()        # args where {{secret:name}} handles are substituted (A9.c.01)
    secret_target: Callable[[dict[str, Any]], str | None] | None = None   # where the values go (URL/host), for host scopes
    rehearse: Callable[[dict[str, Any], "ToolContext"], Awaitable[dict[str, Any]]] | None = None   # A9.b.02: what WILL happen
    cost: Callable[[dict[str, Any]], float | None] | None = None          # A9.c.02: USD estimate for an R4 call

    def schema(self) -> dict[str, Any]:
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": self.parameters}}

    def uses_secrets(self, args: dict[str, Any]) -> bool:
        from omnibots.security.vault import HANDLE
        return any(HANDLE.search(json.dumps(args.get(k), default=str)) for k in self.secret_args if k in args)

    def risk_for(self, args: dict[str, Any], ctx: ToolContext) -> str:
        cls = self.risk
        if self.secret_args and self.uses_secrets(args):
            cls = risk_max(cls, "R3")          # using a stored credential = acting as the user
        if self.classify:
            try:
                cls = risk_max(cls, self.classify(args, ctx))
            except Exception:
                cls = risk_max(cls, "R3")      # can't tell -> treat as risky
        return cls

    def describe(self, args: dict[str, Any]) -> str:
        if self.summary:
            try:
                return self.summary(args)
            except Exception:
                pass
        hint = args.get("path") or args.get("command") or args.get("query") or ""
        return f"{self.name} {hint}".strip()


@dataclass
class ToolRegistry:
    tools: dict[str, Tool] = field(default_factory=dict)
    vault: Any = None                          # security.vault.Vault: resolves {{secret:x}} handles

    def add(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self.tools.get(name)

    def subset(self, names: list[str] | None) -> "ToolRegistry":
        """A bot only gets the tools it was assigned (PLAN.md A8.b.03)."""
        if names is None:
            return ToolRegistry(dict(self.tools))
        return ToolRegistry({n: self.tools[n] for n in names if n in self.tools}, vault=self.vault)

    def schemas(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self.tools.values()]

    async def run(self, name: str, args: dict[str, Any], ctx: ToolContext) -> str:
        tool = self.tools.get(name)
        if tool is None:
            return f"ERROR: unknown tool \"{name}\". Available: {', '.join(self.tools)}"
        from omnibots.security.vault import SecretError, scrub
        if tool.secret_args and tool.uses_secrets(args):
            if self.vault is None:
                return f"ERROR: {name} was given a secret handle, but no vault is available"
            try:
                target = call_target(tool, args, ctx)
                args = {k: (self.vault.resolve(v, target) if k in tool.secret_args else v) for k, v in args.items()}
            except SecretError as exc:
                return f"ERROR: {exc}"
        try:
            # Scrubbed: a secret value never reaches the model, even if a page or a command echoes it.
            return scrub(await asyncio.wait_for(tool.fn(args, ctx), tool.timeout))
        except asyncio.TimeoutError:
            return f"ERROR: {name} timed out after {tool.timeout:.0f}s"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the model sees the error and can recover
            return scrub(f"ERROR: {type(exc).__name__}: {exc}")
