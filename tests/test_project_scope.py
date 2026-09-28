"""A15.e.04 (user, 2026-09-28): the project folder is the bots' world. Scripts run there (relative paths
work); anything that leaves it (a path, or a path written in a shell command or Python code) asks the user,
even with ask_from = R4; the web never asks. Real sandbox, real agent, the mock model."""

from __future__ import annotations

import asyncio
from pathlib import Path

from mock_provider import MockProviders, sse
from test_a7_orchestrator import build, call
from test_a9_approvals_budgets import approver

from omnibots.runtime.scope import outside_paths


def run(coro):
    return asyncio.run(coro)


def test_what_counts_as_leaving_the_project(tmp_path):
    ws = tmp_path / "proj"
    ws.mkdir()
    assert outside_paths("read_file", {"path": "index.html"}, ws) == []
    assert outside_paths("read_file", {"path": "sub/../index.html"}, ws) == []
    assert outside_paths("read_file", {"path": "../other/secret.txt"}, ws) == ["../other/secret.txt"]
    assert outside_paths("list_dir", {"path": "C:\\Users"}, ws) == ["C:\\Users"]
    assert outside_paths("run_shell", {"command": "dir /s C:\\Users\\me\\Documents"}, ws) == ["C:\\Users\\me\\Documents"]
    assert outside_paths("run_shell", {"command": "type %USERPROFILE%\\notes.txt"}, ws) == ["%USERPROFILE%\\notes.txt"]
    assert outside_paths("run_shell", {"command": "python -m pytest tests -q"}, ws) == []
    assert outside_paths("run_python", {"code": "open(r'~/.bashrc').read()"}, ws) == ["~/.bashrc"]
    assert outside_paths("run_python", {"code": f"open(r'{ws / 'a.txt'}').read()"}, ws) == []   # inside, written absolute
    assert outside_paths("web_fetch", {"url": "https://example.com/C:/x"}, ws) == []            # the web never counts


def test_a_script_runs_in_the_project_folder(tmp_path):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            bot = await e["reg"].create("Builder", "coder", chain=["work/m"], tools=["run_python", "write_file"])
            pid = await e["projects"].create("a site")
            folder = e["projects"].folder(pid)
            (folder / "index.html").write_text("<title>Bakery</title>", encoding="utf-8")
            mock.script("work", sse("", tool_calls=[call("run_python", code="print(open('index.html').read())")]), sse("ok"))
            await e["runner"].run(bot.id, "check the title", project_id=pid, workspace=folder)
            tool_out = [m["content"] for m in mock.requests[-1]["body"]["messages"] if m.get("role") == "tool"][0]
            await e["db"].close()
            return tool_out
    out = run(go())
    assert "<title>Bakery</title>" in out and "exit code 0" in out


def test_leaving_the_project_asks_even_when_r3_would_run_silently(tmp_path):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            e["approvals"].ask_from = "R4"                         # the user's setting: R3 runs without asking
            outside = tmp_path / "users_documents"
            outside.mkdir()
            (outside / "notes.txt").write_text("private", encoding="utf-8")
            bot = await e["reg"].create("Finder", "researcher", chain=["work/m"], tools=["read_file", "list_dir", "run_shell"])
            pid = await e["projects"].create("a site")
            folder = e["projects"].folder(pid)
            (folder / "inside.txt").write_text("fine", encoding="utf-8")
            mock.script("work",
                        sse("", tool_calls=[call("read_file", path="inside.txt")]),
                        sse("", tool_calls=[call("read_file", path=str(outside / "notes.txt"))]),
                        sse("", tool_calls=[call("run_shell", command=f"dir {outside}")]),
                        sse("done"))
            seen: list = []
            no = asyncio.create_task(approver(e["approvals"], seen, decide=False))
            out = await e["runner"].run(bot.id, "look around", project_id=pid, workspace=folder)
            no.cancel()
            results = [m["content"] for m in mock.requests[-1]["body"]["messages"] if m.get("role") == "tool"]
            await e["db"].close()
            return out, seen, results
    out, seen, results = run(go())
    assert out.status == "completed"
    assert [s["tool"] for s in seen] == ["read_file", "run_shell"]                         # inside.txt didn't ask
    assert all(s["summary"].startswith("leaves the project folder") for s in seen)
    assert "fine" in results[0] and results[1].startswith("DENIED") and results[2].startswith("DENIED")
    assert "private" not in "".join(results)


def test_the_projects_own_path_with_spaces_and_escaped_newlines_are_not_outside(tmp_path):
    """Found live in A14.a.02: both were taken for places outside the project."""
    ws = tmp_path / "projects" / "2026-09-28 Build a small to-do web app"
    ws.mkdir(parents=True)
    code = (f"import os\nroot = r'{ws}'\nprint(open(os.path.join(root, 'todos.json')).read())\n"
            f"print('a\\nb')\nprint(os.listdir(r'{ws.as_posix()}/static'))\n")
    assert outside_paths("run_python", {"code": code}, ws) == []
    assert outside_paths("run_python", {"code": f"open(r'{ws.parent}\\other project\\x.txt')"}, ws) != []   # a sibling is still outside
    assert outside_paths("run_shell", {"command": "type \\\\fileserver\\share\\notes.txt"}, ws) == ["\\\\fileserver\\share\\notes.txt"]
