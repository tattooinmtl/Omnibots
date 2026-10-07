"""MCP client over Omni's mcpServers (PLAN.md A8.b.02).

Servers come from Omni's config (A1: omni.config.json → mcpServers, with
{{INSTALL_ROOT}} expanded). A server is started lazily, the first time a bot
that is assigned its tools runs, and stays connected (one session per server,
held open by a background task) until shutdown.

Every MCP tool becomes an OmniBots tool named mcp__<server>__<tool> with a
risk class from settings.toml [mcp_risk] ("server.tool" or "server.*").
Anything not listed there is R3: an unknown external tool acts outside the
bot's workspace, so the user approves it.

A17.f.02: a server can also be HTTP: {"url": "http://127.0.0.1:8765/mcp"} (streamable HTTP; {"type": "sse"} or a
URL ending in /sse for the older SSE transport), with optional "headers". A server's "readOnlyTools" list (OmniOne's
format) makes those tools R0. OmniBots' own servers live in ~/.omnibots/mcp.json ({"mcpServers": {...}}), next to
Omni's, which OmniBots never writes; on a name clash Omni's entry wins.
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
        ro = (self.servers.get(server) or {}).get("readOnlyTools") or []
        return self.risk.get(f"{server}.{tool}") or self.risk.get(f"{server}.*") or ("R0" if tool in ro else DEFAULT_RISK)

    async def _hold(self, name: str) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        cfg = self.servers[name]
        fut = self._ready[name]
        try:
            if cfg.get("url"):                                  # A17.f.02: an HTTP server
                await self._hold_http(name, cfg, fut)
                return
            if not cfg.get("command"):
                raise RuntimeError(f"MCP server {name!r} has neither a command nor a url")
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

    async def _hold_http(self, name: str, cfg: dict[str, Any], fut) -> None:
        from mcp import ClientSession
        url = str(cfg["url"])
        headers = {str(k): str(v) for k, v in (cfg.get("headers") or {}).items()}
        if cfg.get("type") == "sse" or url.rstrip("/").endswith("/sse"):
            from mcp.client.sse import sse_client
            transport = sse_client(url, headers=headers or None)
        else:
            from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
            transport = streamable_http_client(url, http_client=create_mcp_http_client(headers=headers or None))
        async with transport as streams:
            r, w = streams[0], streams[1]
            async with ClientSession(r, w) as session:
                await session.initialize()
                self.sessions[name] = session
                if not fut.done():
                    fut.set_result(session)
                await self._close[name].wait()

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
            res = await session.call_tool(t.name, project_paths(args, ctx.workspace))
            parts = []
            for n, c in enumerate(res.content or []):
                kind = getattr(c, "type", "")
                if kind == "text":
                    parts.append(c.text)
                elif kind == "image" and getattr(c, "data", None):   # A17.f.03: a render → a file describe_image can open
                    import base64
                    import time
                    ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(getattr(c, "mimeType", ""), "png")
                    out = ctx.workspace / "mcp" / f"{server}-{t.name}-{time.strftime('%Y%m%d-%H%M%S')}-{n}.{ext}"
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_bytes(base64.b64decode(c.data))
                    parts.append(f"[image saved as {out.relative_to(ctx.workspace).as_posix()}; look at it with describe_image]")
                else:
                    parts.append(f"[{kind or 'content'}]")
            structured = getattr(res, "structured_content", None) or getattr(res, "structuredContent", None)
            text = "\n".join(parts) or json.dumps(structured or {})
            failed = bool(getattr(res, "is_error", None) or getattr(res, "isError", None))
            # found live 2026-10-07: a claim citing a Blender render was refused ("you did not run …"); an MCP call is a
            # real run, so the ledger can check it like a command
            ctx.runs.append({"command": f"mcp__{server}__{t.name} {json.dumps(args, ensure_ascii=False)}",
                             "tool": f"mcp__{server}__{t.name}", "exit_code": 1 if failed else 0, "timed_out": False,
                             "output": text[-8000:]})
            return ("ERROR: " if failed else "") + text

        schema = getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {"type": "object", "properties": {}}
        return Tool(f"mcp__{server}__{t.name}", f"[MCP {server}] {(t.description or t.name)[:900]} (Runs in another program: "
                    "a relative file path you give is made absolute inside your project folder.)", schema,
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


PATH_KEY = __import__("re").compile(r"(?i)(path|file|filename|filepath|dir|directory|folder|output)$")


def project_paths(args: dict[str, Any], workspace) -> dict[str, Any]:
    """A17.f.03 (found live): an MCP server runs in another program with its own working folder (Blender rendered
    'bot_cube_render.png' into the user's home), so a relative path in a file-ish argument becomes an absolute path
    inside the bot's project folder. Absolute paths, URLs and other arguments are left alone."""
    from pathlib import Path, PureWindowsPath
    out = {}
    for k, v in (args or {}).items():
        if isinstance(v, str) and v and PATH_KEY.search(k) and "://" not in v and not v.startswith(("//", "\\")) \
                and not PureWindowsPath(v).is_absolute() and not Path(v).is_absolute():
            v = str((Path(workspace) / v).resolve())
        out[k] = v
    return out


def own_servers(home, filename: str = "mcp.json") -> dict[str, dict[str, Any]]:
    """OmniBots' own MCP servers: ~/.omnibots/mcp.json {"mcpServers": {...}} (A17.f.02)."""
    from pathlib import Path
    try:
        data = json.loads((Path(home) / filename).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    return {str(k): v for k, v in (servers or {}).items() if isinstance(v, dict)}


OMNIONE_MCP = None                 # set by tests; default ~/.omnione/app/.gwn-mcp.json


def omnione_servers() -> dict[str, dict[str, Any]]:
    """OmniOne's MCP servers (read-only, never written), e.g. Blender over HTTP (A17.f.03)."""
    from pathlib import Path
    path = OMNIONE_MCP or (Path.home() / ".omnione" / "app" / ".gwn-mcp.json")
    return own_servers(Path(path).parent, Path(path).name)


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
