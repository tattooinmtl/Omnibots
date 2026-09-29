"""S0 sandbox (PLAN.md §3.2, A9.a.01), the part A3 needs.

Runs a command in a fresh folder with a scrubbed environment (no API keys, no
secrets, no Omni/OmniBots variables), a time limit, and kills the WHOLE
process tree on timeout or cancel. Output streams out as `terminal` events.

Honest limits of S0: it contains and time-limits processes but does NOT block
the network or cap CPU/memory yet. A9.a.01 adds a Windows Job Object
(CPU/memory caps, kill-on-close); A9.a.02 adds S1 (WSL2 + containers).
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

# Anything that looks like a credential never reaches sandboxed code.
_SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|COOKIE|SESSION)", re.I)
_KEEP = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "NUMBER_OF_PROCESSORS",
         "PROCESSOR_ARCHITECTURE", "OS", "LANG", "PYTHONIOENCODING", "USERNAME"}


# Python flags for sandboxed runs: -I without -E. The environment is already an allowlist (scrubbed_env), and -E
# would drop the PYTHONPATH that loads the sandbox's sitecustomize below.
PY_FLAGS = ["-s", "-P"]

# Found live in A14.a.02: in the AppContainer, tempfile.mkdtemp()/TemporaryDirectory() failed with "Access is
# denied". Python 3.12 on Windows turns mkdir(mode=0o700) into an owner-only ACL, which locks the container out of
# the folder it just made. A normal mkdir inherits the granted folder's access instead.
SITECUSTOMIZE = """\
import os
if os.name == "nt" and os.environ.get("OMNIBOTS_SANDBOX", "").endswith("AppContainer"):
    _mkdir = os.mkdir

    def _sandbox_mkdir(path, mode=0o777, *, dir_fd=None):
        return _mkdir(path, 0o777 if mode == 0o700 else mode, dir_fd=dir_fd)

    os.mkdir = _sandbox_mkdir
"""


def scrubbed_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k.upper() in _KEEP and not _SECRET_NAME.search(k)}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env["OMNIBOTS_SANDBOX"] = "S0"
    env.update(extra or {})
    return env


@dataclass
class RunResult:
    exit_code: int | None
    output: str
    timed_out: bool
    seconds: float
    run_dir: Path
    level: str = "S0"

    def as_tool_result(self, limit: int = 12000) -> str:
        out = self.output if len(self.output) <= limit else self.output[:limit // 2] + "\n…[output truncated]…\n" + self.output[-limit // 2:]
        status = "TIMED OUT" if self.timed_out else f"exit code {self.exit_code}"
        return f"[{status}, {self.seconds:.1f}s, sandbox {self.level}, cwd {self.run_dir.name}]\n{out}".rstrip()


class Sandbox:
    """isolation: "auto" (AppContainer + Job Object on Windows when available, else plain S0),
    "appcontainer" (required: fail instead of falling back), or "none" (plain S0)."""

    def __init__(self, root: Path, isolation: str = "auto", memory_mb: int = 1024, max_processes: int = 32):
        self.root = root
        self.isolation = isolation
        self.memory_mb, self.max_processes = memory_mb, max_processes

    def _container(self):
        if self.isolation == "none" or sys.platform != "win32":
            return None
        from omnibots.runtime.appcontainer import container
        ac = container()
        if ac is None and self.isolation == "appcontainer":
            raise RuntimeError("the AppContainer sandbox is required but unavailable")
        return ac

    @property
    def level(self) -> str:
        return "S0+AppContainer" if self._container() else "S0"

    def new_run_dir(self, bot_id: str) -> Path:
        d = self.root / bot_id / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def run(self, argv: list[str] | str, *, cwd: Path, timeout: float = 60.0,
                  on_output: Callable[[str], Awaitable[None]] | None = None,
                  network: bool = False, grant: list[Path] | None = None, grant_read: list[Path] | None = None) -> RunResult:
        """`argv` as a list runs a program directly. As a string it is a shell command line,
        passed to the shell exactly as written (a list through cmd.exe would get Python's
        \\" quoting, which cmd doesn't understand: `python -c "..."` broke).
        In the AppContainer: only `cwd` and the `grant` folders are reachable, and there is no
        network unless `network=True` (for R3 commands the user approved, like installs)."""
        ac = self._container()
        if ac is not None:
            return await self._run_contained(ac, argv, cwd=cwd, timeout=timeout, on_output=on_output,
                                             network=network, grant=grant or [], grant_read=grant_read or [])
        t0 = time.perf_counter()
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        common = dict(cwd=str(cwd), env=scrubbed_env(), stdin=asyncio.subprocess.DEVNULL,
                      stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, creationflags=flags)
        if isinstance(argv, str):
            proc = await asyncio.create_subprocess_shell(argv, **common)
        else:
            proc = await asyncio.create_subprocess_exec(*argv, **common)
        chunks: list[str] = []

        async def pump():
            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").replace("\r\n", "\n")   # Windows CRLF -> LF for the model
                chunks.append(text)
                if on_output:
                    await on_output(text.rstrip("\r\n"))

        timed_out = False
        try:
            await asyncio.wait_for(asyncio.gather(pump(), proc.wait()), timeout)
        except asyncio.TimeoutError:
            timed_out = True
            await self._kill_tree(proc)
        except asyncio.CancelledError:
            await self._kill_tree(proc)
            raise
        return RunResult(proc.returncode, "".join(chunks), timed_out, time.perf_counter() - t0, cwd)

    async def _run_contained(self, ac, argv, *, cwd: Path, timeout: float, on_output, network: bool,
                             grant: list[Path], grant_read: list[Path]) -> RunResult:
        t0 = time.perf_counter()
        for folder in (cwd, *grant):
            await asyncio.to_thread(ac.grant, folder)
        for folder in grant_read:
            await asyncio.to_thread(ac.grant, folder, write=False)
        tmp = cwd / ".tmp"
        tmp.mkdir(exist_ok=True)
        shim = self._python_shim()
        await asyncio.to_thread(ac.grant, shim, write=False)
        # LOCALAPPDATA: Windows builds the container's own data folder from it (CreateProcess fails
        # with error 203 without it). It's only a path; the container still can't read the user's files.
        env = scrubbed_env({"TEMP": str(tmp), "TMP": str(tmp), "OMNIBOTS_SANDBOX": "S0+AppContainer", "PYTHONPATH": str(shim),
                            "LOCALAPPDATA": os.environ.get("LOCALAPPDATA", "")})
        if isinstance(argv, str):
            cmdline = f'{env.get("COMSPEC", "cmd.exe")} /d /c "{argv}"'
        else:
            cmdline = subprocess.list2cmdline(argv)
        proc = await asyncio.to_thread(ac.spawn, cmdline, cwd=cwd, env=env, network=network,
                                       memory_mb=self.memory_mb, max_processes=self.max_processes)
        loop = asyncio.get_running_loop()
        lines: asyncio.Queue = asyncio.Queue()

        def reader() -> None:
            try:
                for raw in iter(proc.stdout.readline, b""):
                    loop.call_soon_threadsafe(lines.put_nowait, raw)
            except (OSError, ValueError):
                pass
            finally:
                loop.call_soon_threadsafe(lines.put_nowait, None)

        threading.Thread(target=reader, name=f"sandbox-out-{proc.pid}", daemon=True).start()
        chunks: list[str] = []

        async def pump() -> None:
            while (raw := await lines.get()) is not None:
                text = raw.decode("utf-8", "replace").replace("\r\n", "\n")
                chunks.append(text)
                if on_output:
                    await on_output(text.rstrip("\r\n"))

        timed_out, code = False, None
        pumping = asyncio.ensure_future(pump())
        try:
            # The run ends when the MAIN process exits: leftover children in the job are killed
            # (they'd otherwise hold the output pipe open, and the run, until the time limit).
            code = await asyncio.wait_for(asyncio.to_thread(proc.wait), timeout)
            try:
                await asyncio.wait_for(asyncio.shield(pumping), 1.0)       # drain what's already written
            except asyncio.TimeoutError:
                pass
        except asyncio.TimeoutError:
            timed_out = True
            proc.kill()
        except asyncio.CancelledError:
            proc.kill()
            raise
        finally:
            proc.kill()                      # whatever is still in the job dies with the run
            try:
                await asyncio.wait_for(pumping, 2.0)                        # pipe closes once the tree is dead
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pumping.cancel()
            proc.close()
        return RunResult(code, "".join(chunks), timed_out, time.perf_counter() - t0, cwd, level="S0+AppContainer")

    def _python_shim(self) -> Path:
        shim = self.root / "_python"
        shim.mkdir(parents=True, exist_ok=True)
        f = shim / "sitecustomize.py"
        if not f.is_file() or f.read_text(encoding="utf-8") != SITECUSTOMIZE:
            f.write_text(SITECUSTOMIZE, encoding="utf-8")
        return shim

    @staticmethod
    async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        if sys.platform == "win32":
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(proc.pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await killer.wait()
        else:
            proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), 10)
        except asyncio.TimeoutError:
            pass
