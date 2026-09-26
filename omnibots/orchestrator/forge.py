"""The Tool Forge (PLAN.md A10.b.01, §4.4).

A bot decides the team is missing a tool, writes it, and submits it here. A
forged tool is a Python module that defines `build() -> Tool` (a normal
omnibots Tool) plus a `pytest` test file. The forge:

  1. writes the module + test into `home/forge/<name>/` (versioned),
  2. static-checks it: it imports, `build()` returns a Tool, the declared risk
     is R0–R2 (a forged tool may never grant itself R3+ side effects; anything
     that acts on the world must be a reviewed built-in), no obviously-dangerous
     imports at module top level,
  3. runs its tests IN THE SANDBOX (the AppContainer: no network, no user files),
  4. has a reviewer bot read the code and verdict PASS/FAIL,
  5. on PASS: records it in the `tools` table (origin='forge', status active) and
     posts TOOL_CREATED. R0–R2 promote automatically; the (rare) R3 case is left
     `pending_review` for the user.

Forged tools are then loaded into every bot's pool by name, exactly like a
built-in, so the team reuses them.
"""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,40}$")
FORGEABLE_RISK = {"R0", "R1", "R2"}
# imports a sandboxed helper tool has no business doing at module load
BANNED_IMPORTS = {"os.system", "subprocess", "socket", "ctypes", "shutil", "winreg", "requests", "urllib.request", "httpx"}


class ForgeError(RuntimeError):
    pass


def _static_check(code: str) -> None:
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise ForgeError(f"the tool module doesn't parse: {exc}")
    names = {n.name for node in ast.walk(tree) if isinstance(node, ast.Import) for n in node.names}
    froms = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    for bad in BANNED_IMPORTS:
        top = bad.split(".")[0]
        if bad in names or top in names or bad in froms or top in froms:
            raise ForgeError(f"a forged tool may not import {bad!r} (it runs in-process; side effects belong in a reviewed built-in)")
    if not any(isinstance(n, ast.FunctionDef) and n.name == "build" for n in tree.body):
        raise ForgeError("the module must define a top-level build() that returns a Tool")


class ToolForge:
    def __init__(self, db, home: Path, sandbox, router, *, bus=None, reviewer_chain: list[str] | None = None, boss_id: str = "omi"):
        self.db, self.home, self.sandbox, self.router = db, home, sandbox, router
        self.bus, self.boss_id = bus, boss_id
        from omnibots.lineup import CHEAP_FIRST
        self.reviewer_chain = reviewer_chain or list(CHEAP_FIRST)
        self.dir = home / "forge"

    # ── building a tool from a forged module ───────────────────────────────
    def _load(self, name: str) -> Any:
        import importlib.util
        mod_path = self.dir / name / "tool.py"
        spec = importlib.util.spec_from_file_location(f"omnibots_forge_{name}", mod_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if not hasattr(module, "build"):
            raise ForgeError("no build() in the module")
        tool = module.build()
        from omnibots.runtime.tools import Tool
        if not isinstance(tool, Tool):
            raise ForgeError("build() must return an omnibots Tool")
        return tool

    def load_active(self) -> list[Any]:
        """Every promoted forged tool, for the runner's pool. A broken one is skipped, not fatal."""
        out = []
        try:
            rows = self._active_rows_sync()
        except Exception:
            return out
        for name in rows:
            try:
                out.append(self._load(name))
            except Exception as exc:
                log.warning("forged tool %s failed to load: %s", name, exc)
        return out

    def _active_rows_sync(self) -> list[str]:
        import sqlite3
        con = sqlite3.connect(self.db.path)
        try:
            return [r[0] for r in con.execute("SELECT name FROM tools WHERE origin='forge' AND status='active' AND enabled=1")]
        finally:
            con.close()

    # ── the forge pipeline ─────────────────────────────────────────────────
    async def submit(self, *, name: str, code: str, test_code: str, created_by: str,
                     description: str = "") -> dict[str, Any]:
        if not NAME_RE.match(name or ""):
            return {"ok": False, "error": "tool name must be lower_snake_case, 3-40 chars"}
        if await self.db.read_one("SELECT id FROM tools WHERE name=? AND origin!='forge'", (name,)):
            return {"ok": False, "error": f"{name!r} is a built-in tool name; choose another"}
        try:
            _static_check(code)
        except ForgeError as exc:
            return {"ok": False, "stage": "static-check", "error": str(exc)}

        folder = self.dir / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "tool.py").write_text(code, encoding="utf-8", newline="\n")
        (folder / "test_tool.py").write_text(test_code, encoding="utf-8", newline="\n")

        # it must import and build a Tool with a forgeable risk class
        try:
            tool = self._load(name)
        except Exception as exc:
            return {"ok": False, "stage": "load", "error": f"{type(exc).__name__}: {exc}"}
        if tool.name != name:
            return {"ok": False, "stage": "load", "error": f"build() returned a tool named {tool.name!r}, not {name!r}"}
        if tool.risk not in FORGEABLE_RISK:
            return {"ok": False, "stage": "risk", "error": f"a forged tool must be R0-R2; {name!r} declares {tool.risk}. "
                    "Actions that touch the world (R3+) belong in a reviewed built-in."}

        # tests run in the sandbox
        test = await self._run_tests(name, folder)
        if not test["passed"]:
            return {"ok": False, "stage": "tests", "error": "the tool's own tests failed in the sandbox", "output": test["output"]}

        review = await self._review(name, tool, code, test_code, test["output"])
        if review.verdict != "PASS":
            return {"ok": False, "stage": "review", "error": f"the reviewer did not pass it ({review.verdict})",
                    "findings": review.findings, "review": review.text}

        await self.db.write(
            "INSERT INTO tools (id, name, description, risk_class, origin, manifest_json, status, created_by) "
            "VALUES (?,?,?,?, 'forge', ?, 'active', ?) "
            "ON CONFLICT(id) DO UPDATE SET description=excluded.description, risk_class=excluded.risk_class, "
            "status='active', enabled=1",
            (f"forge_{name}", name, description or tool.description, tool.risk, "{}", created_by))
        if self.bus:
            await self.bus.publish("#general", "TOOL_CREATED",
                                   {"text": f"forged tool {name} ({tool.risk}): {tool.description[:120]}",
                                    "name": name, "risk": tool.risk, "created_by": created_by},
                                   sender_type="bot", sender_id=created_by)
        return {"ok": True, "name": name, "risk": tool.risk, "review": review.text, "tests": test["output"][-600:]}

    async def _run_tests(self, name: str, folder: Path) -> dict[str, Any]:
        import site
        import sys
        # A forged tool imports omnibots and its test imports pytest, both outside the sandbox's default
        # reach. Grant those library dirs READ-ONLY (they hold no secrets); the run still has no network,
        # no user documents and no vault access.
        import omnibots
        # The forged tool imports omnibots (read-granted). pytest lives under the user profile, which the
        # sandbox can't traverse into, so a tiny stdlib runner calls every test_* in test_tool.py instead —
        # no third-party dependency, no user-profile access. The run still has no network or vault access.
        src = Path(omnibots.__file__).parent.parent
        runner = (f"import sys, traceback, inspect, asyncio\nsys.path.insert(0, {str(src)!r})\n"
                  "import test_tool\n"
                  "fails = 0; ran = 0\n"
                  "for n in sorted(dir(test_tool)):\n"
                  "    if not n.startswith('test_'): continue\n"
                  "    fn = getattr(test_tool, n)\n"
                  "    if not callable(fn): continue\n"
                  "    ran += 1\n"
                  "    try:\n"
                  "        r = fn()\n"
                  "        if inspect.iscoroutine(r): asyncio.run(r)\n"
                  "        print('PASS', n)\n"
                  "    except Exception:\n"
                  "        fails += 1; print('FAIL', n); traceback.print_exc()\n"
                  "if ran == 0: print('NO TESTS'); sys.exit(2)\n"
                  "print(f'{ran-fails}/{ran} passed'); sys.exit(1 if fails else 0)\n")
        script = folder / "_run.py"
        script.write_text(runner, encoding="utf-8")
        res = await self.sandbox.run(["python", "_run.py"], cwd=folder, timeout=120, grant=[folder], grant_read=[src])
        return {"passed": (res.exit_code == 0 and not res.timed_out), "output": res.output}

    async def _review(self, name: str, tool, code: str, test_code: str, test_output: str):
        from omnibots.runtime.review import review
        task = (f"Review a FORGED tool named {name!r} (risk {tool.risk}, in-process helper). Confirm: it does what its "
                f"description says, has no hidden side effects or network/file access, its tests are meaningful and really "
                f"exercise it, and the risk class is right. VERDICT: PASS only if it is safe and correct.")
        changes = f"=== tool.py ===\n{code}\n\n=== test_tool.py ===\n{test_code}\n\n=== pytest output ===\n{test_output[-2000:]}"
        return await review(self.router, reviewer_id=f"{self.boss_id}:forge-review", chain=self.reviewer_chain,
                            task=task, changes=changes)


def forge_tool(forge: ToolForge):
    """The boss-only `create_tool` tool."""
    from omnibots.runtime.tools import Tool, ToolContext
    s = {"type": "string"}

    async def create_tool(args: dict[str, Any], ctx: ToolContext) -> str:
        res = await forge.submit(name=str(args.get("name") or ""), code=str(args.get("code") or ""),
                                 test_code=str(args.get("test_code") or ""), created_by=ctx.bot_id,
                                 description=str(args.get("description") or ""))
        if res["ok"]:
            return f"forged tool {res['name']} ({res['risk']}) passed tests + review and is now available to the team."
        return f"tool not created (stage: {res.get('stage', '?')}): {res['error']}" + (
            f"\nfindings: {res['findings']}" if res.get("findings") else "") + (
            f"\noutput:\n{res['output'][-800:]}" if res.get("output") else "")

    return Tool(
        "create_tool",
        "Forge a new REUSABLE tool for the team when one is missing. Provide `name` (lower_snake_case), `description`, "
        "`code` (a Python module defining build() -> Tool; risk must be R0-R2, no network/subprocess/file imports), and "
        "`test_code` (a pytest file test_tool.py that imports and exercises it). It's tested in the sandbox and reviewed "
        "before the team can use it.",
        {"type": "object", "properties": {"name": s, "description": s, "code": s, "test_code": s},
         "required": ["name", "code", "test_code"]},
        "R1", create_tool, timeout=300, path_arg=None, summary=lambda a: f"forge tool {a.get('name')}")
