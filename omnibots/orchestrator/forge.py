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

Forged code NEVER runs in OmniBots' own process (security audit 2026-10-27: the
module used to be imported in-process before its tests and review, and the import
ban is easy to get around with __import__, so a prompt-injected boss could run
anything as the user). Reading its name/risk, testing it and every later call run
in the sandbox; the pool gets a proxy built from the stored manifest.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

RESULT_MARK = "@@OMNIBOTS-FORGED@@"
# Runs inside the sandbox: imports the forged module from its folder (read-only), then describes it or calls it.
RUNNER = """\
import asyncio, importlib.util, inspect, json, os, sys
from pathlib import Path
src, folder, mode = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, src)
spec = importlib.util.spec_from_file_location("forged_tool", os.path.join(folder, "tool.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
from omnibots.runtime.tools import Tool
t = mod.build()
if not isinstance(t, Tool):
    out = {"error": "build() must return an omnibots Tool"}
elif mode == "describe":
    out = {"name": t.name, "description": t.description, "parameters": t.parameters, "risk": t.risk, "timeout": t.timeout}
else:
    class Ctx:
        bot_id, workspace, job_id, runs, saw_outside = os.environ.get("OMNIBOTS_BOT", ""), Path.cwd(), None, [], False
        async def event(self, kind, content): pass
        def record_run(self, command, result): pass
    args = json.loads(Path(sys.argv[4]).read_text(encoding="utf-8"))
    r = t.fn(args, Ctx())
    out = {"result": str(asyncio.run(r) if inspect.isawaitable(r) else r)}
print("\\n" + %r + json.dumps(out))
""" % RESULT_MARK

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

    # ── running forged code: always in the sandbox ─────────────────────────
    async def _sandboxed(self, name: str, mode: str, cwd: Path, args: dict[str, Any] | None = None,
                         timeout: float = 120) -> dict[str, Any]:
        from omnibots.runtime.sandbox import PY_FLAGS, sandbox_python
        if not self.sandbox.contained:
            raise ForgeError("forged tools only run in the AppContainer sandbox, and it isn't available on this PC")
        folder, src = self.dir / name, self.code_snapshot()
        run_dir = self.sandbox.new_run_dir(f"forge-{name}")
        runner = run_dir / "_forged.py"
        runner.write_text(RUNNER, encoding="utf-8")
        argv = [sandbox_python(), *PY_FLAGS, str(runner), str(src), str(folder), mode]
        if args is not None:
            (run_dir / "call.json").write_text(json.dumps(args), encoding="utf-8")
            argv.append(str(run_dir / "call.json"))
        res = await self.sandbox.run(argv, cwd=cwd, timeout=timeout, grant=[run_dir], grant_read=[folder, src])
        if res.timed_out:
            raise ForgeError(f"{name} timed out after {timeout:g}s")
        for line in reversed(res.output.splitlines()):
            if line.startswith(RESULT_MARK):
                out = json.loads(line[len(RESULT_MARK):])
                if out.get("error"):
                    raise ForgeError(out["error"])
                return out
        raise ForgeError(f"{name} failed in the sandbox (exit {res.exit_code}): {res.output[-1500:]}")

    async def describe(self, name: str) -> dict[str, Any]:
        """The forged tool's name, description, parameters, risk and timeout, read in the sandbox."""
        return await self._sandboxed(name, "describe", self.dir / name)

    def _proxy(self, name: str, m: dict[str, Any]) -> Any:
        from omnibots.runtime.tools import Tool
        timeout = float(m.get("timeout") or 120)

        async def call(args: dict[str, Any], ctx) -> str:
            out = await self._sandboxed(name, "call", Path(ctx.workspace), args, timeout=timeout)
            return str(out.get("result", ""))
        return Tool(name, str(m.get("description") or name), m.get("parameters") or {"type": "object", "properties": {}},
                    str(m.get("risk") or "R2"), call, timeout=timeout + 30, path_arg=None)

    def load_active(self) -> list[Any]:
        """Every promoted forged tool, for the runner's pool, as a sandboxed proxy (nothing forged runs here)."""
        out = []
        try:
            rows = self._active_rows_sync()
        except Exception:
            return out
        for name, manifest in rows:
            try:
                m = json.loads(manifest or "{}")
            except ValueError:
                m = {}
            if m.get("risk") not in FORGEABLE_RISK:
                log.warning("forged tool %s has no sandbox manifest yet; skipped", name)
                continue
            out.append(self._proxy(name, m))
        return out

    async def describe_missing(self) -> int:
        """Tools forged before the sandboxed manifest existed: describe them once in the sandbox."""
        n = 0
        for name, manifest in await asyncio.to_thread(self._active_rows_sync):
            if (manifest or "{}") != "{}":
                continue
            try:
                m = await self.describe(name)
            except Exception as exc:
                log.warning("forged tool %s can't be described in the sandbox: %s", name, exc)
                continue
            if m.get("name") == name and m.get("risk") in FORGEABLE_RISK:
                await self.db.write("UPDATE tools SET manifest_json=? WHERE name=? AND origin='forge'", (json.dumps(m), name))
                n += 1
        return n

    def _active_rows_sync(self) -> list[tuple[str, str]]:
        import sqlite3
        con = sqlite3.connect(self.db.path)
        try:
            return [(r[0], r[1]) for r in con.execute(
                "SELECT name, manifest_json FROM tools WHERE origin='forge' AND status='active' AND enabled=1")]
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

        # it must import and build a Tool with a forgeable risk class: checked in the sandbox, never here
        try:
            m = await self.describe(name)
        except Exception as exc:
            return {"ok": False, "stage": "load", "error": f"{type(exc).__name__}: {exc}"}
        if m.get("name") != name:
            return {"ok": False, "stage": "load", "error": f"build() returned a tool named {m.get('name')!r}, not {name!r}"}
        if m.get("risk") not in FORGEABLE_RISK:
            return {"ok": False, "stage": "risk", "error": f"a forged tool must be R0-R2; {name!r} declares {m.get('risk')}. "
                    "Actions that touch the world (R3+) belong in a reviewed built-in."}
        tool = self._proxy(name, m)

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
            "manifest_json=excluded.manifest_json, status='active', enabled=1",
            (f"forge_{name}", name, description or tool.description, tool.risk, json.dumps(m), created_by))
        if self.bus:
            await self.bus.publish("#general", "TOOL_CREATED",
                                   {"text": f"forged tool {name} ({tool.risk}): {tool.description[:120]}",
                                    "name": name, "risk": tool.risk, "created_by": created_by},
                                   sender_type="bot", sender_id=created_by)
        return {"ok": True, "name": name, "risk": tool.risk, "review": review.text, "tests": test["output"][-600:]}

    async def _run_tests(self, name: str, folder: Path) -> dict[str, Any]:
        # The forged tool imports omnibots, from a read-only copy of the package (code_snapshot). pytest lives under
        # the user profile, which the sandbox can't traverse into, so a tiny stdlib runner calls every test_* in
        # test_tool.py instead — no third-party dependency, no user-profile access. No network or vault access.
        from omnibots.runtime.sandbox import sandbox_python
        src = self.code_snapshot()
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
        res = await self.sandbox.run([sandbox_python(), "_run.py"], cwd=folder, timeout=120, grant=[folder], grant_read=[src])
        return {"passed": (res.exit_code == 0 and not res.timed_out), "output": res.output}

    def code_snapshot(self) -> Path:
        """A read-only copy of the omnibots package (only its .py files) for forged-tool tests. Found 2026-09-29: the
        package's parent folder used to be the code checkout; since the installer puts OmniBots in ~/.omnibots, that
        folder also holds the database, logs and the bots' browser profiles, and granting it would have opened them
        to a forged tool's tests. The copy is made once per code version and older copies are removed."""
        import hashlib
        import shutil

        import omnibots
        pkg = Path(omnibots.__file__).parent
        files = sorted(p for p in pkg.rglob("*.py") if "__pycache__" not in p.parts)
        h = hashlib.sha256()
        for p in files:
            h.update(p.relative_to(pkg).as_posix().encode())
            h.update(p.read_bytes())
        libs = Path(self.sandbox.root) / "_lib"
        lib = libs / h.hexdigest()[:12]
        if not (lib / "omnibots" / "__init__.py").is_file():
            tmp = libs / f"{lib.name}.tmp"
            shutil.rmtree(tmp, ignore_errors=True)
            for p in files:
                dest = tmp / "omnibots" / p.relative_to(pkg)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(p, dest)
            shutil.rmtree(lib, ignore_errors=True)
            tmp.rename(lib)
        for old in libs.iterdir():
            if old != lib:
                shutil.rmtree(old, ignore_errors=True)
        return lib

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
