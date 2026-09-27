"""Standing listen loop: no 30-minute cap, the board is read on a short cycle,
an assigned job is picked up, and a file change wakes maintenance."""

from __future__ import annotations

import asyncio
from pathlib import Path

from test_a7_orchestrator import build

from omnibots.board.a2a import Inbox, a2a_tools
from omnibots.bots.presence import TeamPresence, diff_snapshots
from omnibots.bots.profile import BOSS_ID
from omnibots.bots.runner import JobOutcome
from omnibots.runtime.tools import ToolContext


class _Note:
    def __init__(self, bot_id, text):
        self.message_type = "A2A_MESSAGE"
        self.recipient_id = bot_id
        self.sender_type = "user"
        self._text = text

    def text(self):
        return self._text


def _presence(e, **kw):
    kw.setdefault("poll_seconds", 0.05)
    return TeamPresence(db=e["db"], bus=e["bus"], registry=e["reg"], runner=e["runner"], graph=e["graph"],
                        projects=e["projects"], orchestrator=e["orch"], **kw)


def test_listen_loop_does_not_stop_at_thirty_minutes(tmp_path):
    async def run():
        from mock_provider import MockProviders
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            p = _presence(e, poll_seconds=0.02)
            task = asyncio.create_task(p.run())
            await asyncio.sleep(0.35)
            alive = not task.done()
            polls = p.polls
            task.cancel()
            await asyncio.wait([task])
            await e["db"].close()
            return alive, polls
    alive, polls = asyncio.run(run())
    assert alive and polls >= 8


def test_an_assigned_job_is_picked_up_from_the_board(tmp_path):
    async def go():
        from mock_provider import MockProviders
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            bot = await e["reg"].create("Ada", "coder", chain=["work/m"])
            pid = await e["projects"].create("keep the site up")
            job = await e["graph"].add_job(pid, "fix the header", description="repair header", done_criteria="header ok",
                                           assigned_bot_id=bot.id)
            await e["db"].write("UPDATE jobs SET status='assigned' WHERE id=?", (job.id,))
            started = []

            async def fake_run(bot_id, task, **kw):
                started.append((bot_id, kw.get("job_id"), "header" in task))
                return JobOutcome(kw.get("job_id") or "", bot_id, "completed", None, 0.0, [])

            e["runner"].run = fake_run
            p = _presence(e)
            await p._pickup_ready(bot.id)
            await p._jobs[bot.id]
            state = (await e["db"].read_one("SELECT status FROM jobs WHERE id=?", (job.id,)))["status"]
            await e["db"].close()
            return started, state
    started, state = asyncio.run(go())
    assert len(started) == 1 and started[0][0] != BOSS_ID and started[0][2] and state == "review"


def test_a_busy_bot_is_not_started_twice(tmp_path):
    async def go():
        from mock_provider import MockProviders
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            bot = await e["reg"].create("Ada", "coder", chain=["work/m"])
            pid = await e["projects"].create("site")
            job = await e["graph"].add_job(pid, "fix", description="x", done_criteria="y", assigned_bot_id=bot.id)
            await e["db"].write("UPDATE jobs SET status='assigned' WHERE id=?", (job.id,))
            e["runner"]._starting.add(bot.id)
            p = _presence(e)
            await p._pickup_ready(bot.id)
            await e["db"].close()
            return bot.id in p._jobs
    assert asyncio.run(go()) is False


def test_a_file_change_wakes_maintenance_and_a_closed_project_does_not(tmp_path):
    async def go():
        from mock_provider import MockProviders
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            seen = []

            async def maintain(pid, changes):
                seen.append((pid, list(changes)))

            p = _presence(e, maintain=maintain, maintain_cooldown=0)
            pid = await e["projects"].create("the shop")
            folder = e["projects"].folder(pid)
            (folder / "index.html").write_text("<h1>ok</h1>", encoding="utf-8")
            await p._watch_files()                       # first look is the baseline
            assert seen == []
            (folder / "index.html").write_text("<h1>broken</h1>", encoding="utf-8")
            await p._watch_files()
            await p._maint_tasks[pid]
            open_hit = list(seen)
            closed = await e["projects"].create("old")
            await e["projects"].set_status(closed, "cancelled")
            cfolder = e["projects"].folder(closed)
            (cfolder / "gone.txt").write_text("x", encoding="utf-8")
            await p._watch_files()
            await asyncio.sleep(0.05)
            await e["db"].close()
            return open_hit, seen, closed
    open_hit, seen, closed = asyncio.run(go())
    assert len(open_hit) == 1 and any("index.html" in c for c in open_hit[0][1])
    assert all(pid != closed for pid, _ in seen)


def test_a_user_note_on_the_board_is_read_when_the_bot_is_idle(tmp_path):
    async def go():
        from mock_provider import MockProviders
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            bot = await e["reg"].create("Ada", "coder", chain=["work/m"])
            heard = []

            async def fake_run(bot_id, task, **kw):
                heard.append(task)
                return JobOutcome("", bot_id, "completed", None, 0.0, [])

            e["runner"].run = fake_run
            p = _presence(e)
            await p._on_message(bot.id, _Note(bot.id, "the nav link 404s"))
            await p._flush_notes(bot.id)
            await p._jobs[bot.id]
            await e["db"].close()
            return heard
    heard = asyncio.run(go())
    assert heard and "404" in heard[0]


def test_waiting_on_the_board_is_not_capped_and_does_not_spend_the_work_budget(tmp_path):
    async def go():
        from mock_provider import MockProviders
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            inbox = await Inbox.open(e["bus"], BOSS_ID)
            tool = {t.name: t for t in a2a_tools(e["bus"], inbox, bot_id=BOSS_ID, boss_id=BOSS_ID)}["wait_for_mention"]
            entered = []

            class Hold:
                def __enter__(self):
                    entered.append(1)
                    return self

                def __exit__(self, *a):
                    return False

            ctx = ToolContext(bot_id=BOSS_ID, workspace=Path(tmp_path), waiting_on_user=lambda: Hold())
            # A long timeout must return as soon as the board has something, and still count as listening.
            await e["bus"].publish("#bot/omi", "A2A_MESSAGE", {"text": "still here"}, sender_type="user",
                                   sender_id="user", recipient_id=BOSS_ID)
            answer = await tool.fn({"timeout_seconds": 4000}, ctx)
            inbox.close()
            await e["db"].close()
            return tool.timeout, entered, answer
    timeout, entered, answer = asyncio.run(go())
    assert timeout > 1800 and entered == [1] and "still here" in answer


def test_snapshot_diff_names_adds_and_edits():
    assert diff_snapshots({"a.txt": (1, 1)}, {"a.txt": (2, 1), "b.txt": (1, 1)}) == ["changed a.txt", "added b.txt"]
