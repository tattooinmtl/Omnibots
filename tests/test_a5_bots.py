"""A5: bot registry, memory.md, the shared user profile, the job runner, and
stats that match SQL (mock provider over real HTTP, no tokens)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from mock_provider import MockProviders, sse, status
from omnibots.board.bus import MessageBus
from omnibots.board.ledger import Ledger
from omnibots.board.query import query
from omnibots.bots.memory import MemoryFile, template
from omnibots.bots.profile import BOSS_ID, BotRegistry, ProfileError, user_profile_path, user_profile_text
from omnibots.bots.runner import JobRunner
from omnibots.db import Database
from omnibots.keepawake import KeepAwake
from omnibots.omni.config import OmniConfig, ProviderInfo
from omnibots.omni.locate import OmniLocation
from omnibots.providers.quota import QuotaManager
from omnibots.providers.router import Router
from omnibots.providers.seats import SeatScheduler
from omnibots.runtime.approvals import ApprovalCenter
from omnibots.runtime.sandbox import Sandbox


def run(coro):
    return asyncio.run(coro)


def call(name, **args):
    return {"name": name, "args": args}


async def setup(tmp: Path, mock: MockProviders | None = None):
    db = Database(tmp / "db" / "omnibots.sqlite")
    await db.open()
    bus = MessageBus(db)
    reg = BotRegistry(db, tmp / "bots", bus)
    env = {"db": db, "bus": bus, "reg": reg}
    if mock:
        providers = {n: ProviderInfo(name=n, base_url=mock.url(n), api_key="k", key_source="settings", raw={"reasoningParam": "none"})
                     for n in ("work", "cheap")}
        cfg = OmniConfig(OmniLocation(Path("."), Path(".")), providers,
                         {"work/m": {"provider": "work", "id": "w", "maxTokens": 256}, "cheap/m": {"provider": "cheap", "id": "c", "maxTokens": 256}},
                         None, None, [], {})
        router = Router(lambda: cfg, QuotaManager(db), SeatScheduler(boss_id=BOSS_ID))
        env["runner"] = JobRunner(db=db, registry=reg, router=router, approvals=ApprovalCenter(db), home=tmp, sandbox=Sandbox(tmp / "sandbox"),
                                  bus=bus, ledger=Ledger(db, bus), keep_awake=KeepAwake(enabled=False), lesson_chain=["cheap/m"])
    return env


# ── registry ───────────────────────────────────────────────────────────────
def test_create_edit_archive_and_the_boss(tmp_path):
    async def go():
        e = await setup(tmp_path)
        reg = e["reg"]
        boss = await reg.ensure_boss()
        again = await reg.ensure_boss()
        coder = await reg.create("Coder-01", "coder", description="Writes Python", chain=["work/m"], tools=["read_file", "run_python"],
                                 skills=["coding"], limits={"max_iterations": 12})
        errors = []
        for bad in (dict(name="", role="x"), dict(name="x", role="y", risk_ceiling="R9"), dict(name="x", role="y", chain=[])):
            with pytest.raises(ProfileError) as ex:
                await reg.create(**bad)
            errors.append(str(ex.value))
        edited = await reg.update(coder.id, role="senior coder", tools=["read_file", "write_file"], limits={"max_iterations": 20})
        with pytest.raises(ProfileError):
            await reg.update(coder.id, status="running")
        await reg.archive(coder.id)
        with pytest.raises(ProfileError):
            await reg.archive(BOSS_ID)
        listed = [b.id for b in await reg.list()]
        all_ = [b.id for b in await reg.list(include_archived=True)]
        created = [m.payload["name"] for m in await query(e["db"], types=["BOT_CREATED"])]
        await e["db"].close()
        return boss, again, coder, edited, listed, all_, created, errors
    boss, again, coder, edited, listed, all_, created, errors = run(go())
    assert boss.id == "omi" and boss.role == "boss" and again.id == "omi"
    assert coder.id.startswith("bot_") and coder.tools == ["read_file", "run_python"] and coder.skills == ["coding"]
    assert (coder.folder / "memory.md").is_file() and all((coder.folder / d).is_dir() for d in ("workspace", "artifacts", "logs"))
    assert "a name and a role" in errors[0] and "risk ceiling" in errors[1] and "provider chain" in errors[2]
    assert edited.role == "senior coder" and edited.tools == ["read_file", "write_file"] and edited.limits == {"max_iterations": 20}
    assert listed == ["omi"] and set(all_) == {"omi", coder.id} and created == ["Omi", "Coder-01"]


# ── memory.md ──────────────────────────────────────────────────────────────
def test_memory_template_updates_and_manual_edits_survive(tmp_path):
    path = tmp_path / "memory.md"
    path.write_text(template("bot_1", "Coder", "coder"), encoding="utf-8")
    assert "created_at: 20" in path.read_text() and "# Long-Term Notes" in path.read_text()
    # The user edits the file by hand: a note, and a custom section of their own.
    text = path.read_text().replace("# Long-Term Notes\n", "# Long-Term Notes\n- Prefers small modular functions\n")
    path.write_text(text + "\n# My Scratchpad\nkeep this exactly\n", encoding="utf-8")
    mem = MemoryFile(path)
    mem.set_current_task("job_1", "build the login page")
    assert mem.section("Current Task")[0].startswith("- job_1: build the login page")
    mem.add_job("job_1", "build the login page", "completed", "Login page done, tests pass")
    mem.add_lessons(["Validate file paths before writing", "- validate file paths before writing"])   # duplicate ignored
    mem.clear_current_task()
    out = path.read_text()
    assert "- Prefers small modular functions" in out and "# My Scratchpad\nkeep this exactly" in out
    assert mem.section("Current Task") == ["- none"]
    assert mem.section("Job History")[0].endswith("job_1: build the login page [completed] — Login page done, tests pass")
    assert len(mem.section("Lessons Learned")) == 1
    ctx = mem.context()
    assert "Prefers small modular functions" in ctx and "Validate file paths" in ctx and "job_1" in ctx


def test_memory_is_summarized_over_32kb_and_long_term_notes_are_kept(tmp_path):
    path = tmp_path / "memory.md"
    path.write_text(template("b", "B", "coder").replace("# Long-Term Notes\n", "# Long-Term Notes\n- NEVER TOUCH THIS NOTE\n"), encoding="utf-8")
    mem = MemoryFile(path)
    for i in range(400):
        mem.add_job(f"job_{i}", f"task number {i} " + "x" * 40, "completed", "done " + "y" * 30)
    for i in range(60):
        mem.add_lessons([f"lesson {i}: " + "z" * 60])
    assert mem.size() > 32 * 1024
    calls = []

    async def summarizer(instruction, text):
        calls.append((instruction[:20], text.count("\n") + 1))
        return "- 380 jobs completed, mostly routine tasks\n- no recurring failures"
    assert run(mem.summarize_if_needed(summarizer))
    out = path.read_text()
    assert mem.size() <= 32 * 1024 and "- NEVER TOUCH THIS NOTE" in out
    assert "(summary of 380 earlier entries" in out and "380 jobs completed" in out and "job_399" in out and "job_0:" not in out
    assert len(calls) == 2                                              # job history + lessons

    # A failing summarizer still keeps the file bounded.
    for i in range(400, 800):
        mem.add_job(f"job_{i}", "more " + "x" * 60, "completed")

    async def broken(instruction, text):
        raise RuntimeError("provider down")
    run(mem.summarize_if_needed(broken))
    assert mem.size() <= 32 * 1024 and "folded without a model" in path.read_text() and "- NEVER TOUCH THIS NOTE" in path.read_text()


def test_user_profile_template_and_text(tmp_path):
    p = user_profile_path(tmp_path)
    assert p.is_file() and user_profile_text(tmp_path) == ""                 # template only: nothing injected
    p.write_text(p.read_text() + "\n- Name: Erik. Prefers French-Canadian spelling in client copy.\n", encoding="utf-8")
    assert user_profile_text(tmp_path) == "- Name: Erik. Prefers French-Canadian spelling in client copy."


# ── the job runner ─────────────────────────────────────────────────────────
def test_a_job_updates_sql_memory_board_and_stats(tmp_path):
    async def go():
        with MockProviders() as mock:
            mock.script("work",
                        sse("", tool_calls=[call("write_file", path="app.py", content="print(6*7)")], usage={"prompt_tokens": 100, "completion_tokens": 20}),
                        sse("", tool_calls=[call("run_python", path="app.py")], usage={"prompt_tokens": 150, "completion_tokens": 10}),
                        sse("Printed 42.", usage={"prompt_tokens": 180, "completion_tokens": 5}))
            mock.script("cheap", sse("- Run the script after writing it to confirm the output"))
            e = await setup(tmp_path, mock)
            user_profile_path(tmp_path).write_text("# User Profile\n- The user likes short answers.\n", encoding="utf-8")
            bot = await e["reg"].create("Calc", "coder", chain=["work/m"])
            bot.memory._edit("Long-Term Notes", lambda _: ["- Always print results"])
            out = await e["runner"].run(bot.id, "write app.py that prints 6*7 and run it", project_id="p1")
            first_request = mock.requests[0]["body"]["messages"][0]["content"]
            job = await e["db"].read_one("SELECT * FROM jobs WHERE id=?", (out.job_id,))
            status_after = (await e["reg"].get(bot.id)).status
            stats = await e["runner"].stats(bot.id)
            board = [m.message_type for m in await query(e["db"], job=out.job_id)]
            await e["db"].close()
            return out, first_request, dict(job), status_after, stats, board, bot
    out, prompt, job, status_after, stats, board, bot = run(go())
    assert out.status == "completed" and out.lessons == ["Run the script after writing it to confirm the output"]
    assert "The user likes short answers." in prompt and "Always print results" in prompt   # profile + memory in the prompt
    assert job["status"] == "completed" and job["assigned_bot_id"] == bot.id and job["result_summary"] == "Printed 42."
    assert job["started_at"] and job["finished_at"] and job["project_id"] == "p1"
    mem = bot.memory
    assert mem.section("Current Task") == ["- none"]
    assert mem.section("Job History")[-1].endswith(f"{out.job_id}: write app.py that prints 6*7 and run it [completed] — Printed 42.")
    assert mem.section("Lessons Learned")[-1].endswith("Run the script after writing it to confirm the output")
    assert status_after == "idle" and board == ["WORK_STARTED", "TASK_COMPLETED"]
    assert stats["jobs"] == 1 and stats["success_rate"] == 1.0 and stats["tokens"] == 465 and stats["avg_seconds"] is not None


def test_history_matches_sql_across_several_jobs(tmp_path):
    async def go():
        with MockProviders() as mock:
            mock.script("work", sse("ok 1"), status(400, {"error": {"message": "bad request"}}), sse("ok 3"))
            mock.default["cheap"] = sse("NONE")
            e = await setup(tmp_path, mock)
            bot = await e["reg"].create("Hist", "writer", chain=["work/m"])
            outs = [await e["runner"].run(bot.id, f"job {i}") for i in range(3)]
            rows = await e["db"].read("SELECT id, status FROM jobs WHERE assigned_bot_id=? ORDER BY rowid", (bot.id,))
            stats = await e["runner"].stats(bot.id)
            await e["db"].close()
            return outs, [(r["id"], r["status"]) for r in rows], stats, bot
    outs, rows, stats, bot = run(go())
    hist = bot.memory.section("Job History")
    assert [o.status for o in outs] == ["completed", "failed", "completed"]
    assert [(o.job_id, o.status) for o in outs] == rows
    assert [(l.split(": ")[0].split(" ")[-1], l.split("[")[1].split("]")[0]) for l in hist] == rows   # memory.md == SQL
    assert stats["by_status"] == {"completed": 2, "failed": 1} and stats["success_rate"] == pytest.approx(0.667, abs=1e-3)
    assert all(o.lessons == [] for o in outs)                                # "NONE" -> nothing written


def test_boss_editing_the_user_profile_needs_approval(tmp_path):
    async def go():
        with MockProviders() as mock:
            mock.script("work", sse("", tool_calls=[call("update_user_profile", content="# User Profile\n- Likes blue\n")]), sse("updated"))
            mock.default["cheap"] = sse("NONE")
            e = await setup(tmp_path, mock)
            await e["reg"].ensure_boss()
            await e["reg"].update(BOSS_ID, chain=["work/m"])
            task = asyncio.create_task(e["runner"].run(BOSS_ID, "remember that I like blue"))
            for _ in range(60):
                await asyncio.sleep(0.05)
                if e["runner"].approvals.list_pending():
                    break
            pending = e["runner"].approvals.list_pending()
            before = user_profile_text(tmp_path)
            await e["runner"].approvals.decide(pending[0]["id"], True)
            out = await task
            await e["db"].close()
            return pending, before, out
    pending, before, out = run(go())
    assert pending[0]["risk"] == "R3" and pending[0]["tool"] == "update_user_profile" and before == ""
    assert out.status == "completed" and user_profile_text(tmp_path) == "- Likes blue"
