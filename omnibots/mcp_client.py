"""MCP client over Omni's mcpServers (PLAN.md A8.b.02).

Servers come from Omni's config (A1: omni.config.json → mcpServers, with
{{INSTALL_ROOT}} expanded). A server is started lazily, the first time a bot
that is assigned its tools runs, and stays connected (one session per server,
held open by a background task) until shutdown.

Every MCP tool becomes an OmniBots tool named mcp__<server>__<tool> with a
risk class from settings.toml [mcp_risk] ("server.tool" or "server.*").
Anything not listed there is R3: an unknown external tool acts outside the
bot's workspace, so the user approves it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

from omnibots.runtime.tools import RISK_ORDER, Tool, ToolContext

log = logging.getLogger(__name__)
DEFAULT_RISK = "R3"


class MCPManager:
    def __init__(self, servers: Callable[[], dict[str, dict[str, Any]]] | dict[str, dict[str, Any]],
                 risk: dict[str, str] | None = None, connect_timeout: float = 30.0):
        self._servers = servers if callable(servers) else (lambda: servers)
        self.risk = {k: v for k, v in (risk or {}).items() if v in RISK_ORDER}
        self.connect_timeout = connect_timeout
        self.sessions: dict[str, Any] = {}
        self._holders: dict[str, asyncio.Task] = {}
        self._close: dict[str, asyncio.Event] = {}
        self._ready: dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()
        self._tool_cache: dict[str, list] = {}

    @property
    def servers(self) -> dict[str, dict[str, Any]]:
        return self._servers() or {}

    def server_names(self) -> list[str]:
        return list(self.servers)

    def risk_for(self, server: str, tool: str) -> str:
        return self.risk.get(f"{server}.{tool}") or self.risk.get(f"{server}.*") or DEFAULT_RISK

    async def _hold(self, name: str) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        cfg = self.servers[name]
        fut = self._ready[name]
        try:
            if not cfg.get("command"):
                raise RuntimeError(f"MCP server {name!r} has no command (HTTP servers aren't supported yet)")
            params = StdioServerParameters(command=cfg["command"], args=[str(a) for a in cfg.get("args", [])],
                                           env={str(k): str(v) for k, v in cfg["env"].items()} if cfg.get("env") else None,
                                           cwd=cfg.get("cwd"))
            async with stdio_client(params) as (r, w):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    self.sessions[name] = session
                    if not fut.done():
                        fut.set_result(session)
                    await self._close[name].wait()
        except Exception as exc:
            if not fut.done():
                fut.set_exception(exc)
            log.warning("MCP server %s stopped: %s", name, exc)
        finally:
            self.sessions.pop(name, None)

    async def session(self, name: str):
        if name not in self.servers:
            raise KeyError(f"no MCP server named {name!r} in Omni's config")
        async with self._lock:
            if name in self.sessions:
                return self.sessions[name]
            if name not in self._holders or self._holders[name].done():
                self._close[name] = asyncio.Event()
                self._ready[name] = asyncio.get_running_loop().create_future()
                self._holders[name] = asyncio.create_task(self._hold(name), name=f"mcp-{name}")
            fut = self._ready[name]
        return await asyncio.wait_for(asyncio.shield(fut), self.connect_timeout)

    async def tools(self, server: str, only: set[str] | None = None) -> list[Tool]:
        if server not in self._tool_cache:
            session = await self.session(server)
            self._tool_cache[server] = (await session.list_tools()).tools
        out = []
        for t in self._tool_cache[server]:
            if only and t.name not in only:
                continue
            out.append(self._wrap(server, t))
        return out

    def _wrap(self, server: str, t) -> Tool:
        manager = self

        async def call(args: dict[str, Any], ctx: ToolContext) -> str:
            session = await manager.session(server)
            res = await session.call_tool(t.name, args)
            parts = []
            for c in res.content or []:
                parts.append(c.text if getattr(c, "type", "") == "text" else f"[{getattr(c, 'type', 'content')}]")
            structured = getattr(res, "structured_content", None) or getattr(res, "structuredContent", None)
            text = "\n".join(parts) or json.dumps(structured or {})
            return ("ERROR: " if (getattr(res, "is_error", None) or getattr(res, "isError", None)) else "") + text

        schema = getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {"type": "object", "properties": {}}
        return Tool(f"mcp__{server}__{t.name}", f"[MCP {server}] {(t.description or t.name)[:900]}", schema,
                    self.risk_for(server, t.name), call, timeout=120, path_arg=None,
                    rehearse=lambda a, c, n=t.name: _mcp_card(server, n, a),
                    summary=lambda a, n=t.name: f"mcp {server}.{n} {json.dumps(a)[:80]}")

    async def close(self) -> None:
        for ev in self._close.values():
            ev.set()
        tasks = [t for t in self._holders.values() if not t.done()]
        if tasks:
            await asyncio.wait(tasks, timeout=10)


async def _mcp_card(server: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"mcp_server": server, "mcp_tool": tool, "arguments": json.dumps(args, ensure_ascii=False)[:2000]}


def mcp_refs(tool_names: list[str]) -> dict[str, set[str] | None]:
    """Profile entries 'mcp:okf' (every okf tool) or 'mcp:okf.okf_search' (one)."""
    refs: dict[str, set[str] | None] = {}
    for t in tool_names:
        if not t.startswith("mcp:"):
            continue
        server, _, tool = t[4:].partition(".")
        if not tool:
            refs[server] = None
        elif refs.get(server, set()) is not None:
            refs.setdefault(server, set()).add(tool)
    return refs
