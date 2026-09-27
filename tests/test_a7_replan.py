"""Live bug (user, 2026-09-26, the OmniBots website goal): the Designer's job failed for good, Omi
had no way to cancel or re-plan, so he invented a "Cancel placeholder" job and the 4 jobs after the
Designer (index, css, js, validate) stayed blocked forever. Now Omi has cancel_job and update_job,
and re-planned jobs come back to life down the chain."""

from __future__ import annotations

import asyncio

from mock_provider import MockProviders
from test_a7_orchestrator import build

from omnibots.board.a2a import Inbox
from omnibots.bots.profile import BOSS_ID
from omnibots.orchestrator.boss_tools import BossToolkit, GoalContext
from omnibots.orchestrator.planner import PLANNER_PROMPT
from omnibots.bots.runner import BOSS_PROMPT
from omnibots.runtime.tools import ToolContext


def test_omi_cancels_a_dead_job_and_repoints_the_chain(tmp_path):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            g = e["graph"]
            pid = await e["projects"].create("showcase site")
            ctx = GoalContext(pid, "showcase site", e["projects"].folder(pid))
            inbox = await Inbox.open(e["bus"], BOSS_ID)
            kit = BossToolkit(ctx=ctx, db=e["db"], bus=e["bus"], inbox=inbox, registry=e["reg"], runner=e["runner"], graph=g,
                              projects=e["projects"], ledger=e["ledger"], factory=e["factory"], router=e["router"])
            tc = ToolContext(bot_id=BOSS_ID, workspace=ctx.folder)
            names = {t.name for t in kit.tools()}
            scaffold = await g.add_job(pid, "scaffold", done_criteria="README exists")
            design = await g.add_job(pid, "svg assets", done_criteria="svgs exist", depends_on=[scaffold.id])
            index = await g.add_job(pid, "index.html", done_criteria="index exists", depends_on=[scaffold.id, design.id])
            css = await g.add_job(pid, "styles.css", done_criteria="css exists", depends_on=[index.id])
            js = await g.add_job(pid, "main.js", done_criteria="js exists", depends_on=[index.id, css.id])
            await e["db"].write("UPDATE jobs SET status='completed' WHERE id=?", (scaffold.id,))
            await e["db"].write("UPDATE jobs SET status='blocked', attempts=2, error_message='stuck in a loop' WHERE id=?", (design.id,))
            await g.refresh(pid)
            before = {j.id: j.status for j in await g.jobs(pid)}

            cancelled = await kit.cancel_job({"job_id": design.id, "reason": "criteria needed index.html"}, tc)
            add = await kit.add_job({"title": "svg assets (v2)", "done_criteria": "svgs exist", "depends_on": [scaffold.id]}, tc)
            v2 = add.split()[1]
            updated = await kit.update_job({"job_id": index.id, "depends_on": [scaffold.id, v2]}, tc)
            mid = {j.id: j.status for j in await g.jobs(pid)}
            await e["db"].write("UPDATE jobs SET status='completed' WHERE id=?", (v2,))
            await g.refresh(pid)
            after = {j.id: j.status for j in await g.jobs(pid)}
            cycle = await kit.update_job({"job_id": index.id, "depends_on": [css.id]}, tc)
            running = await kit.update_job({"job_id": scaffold.id, "done_criteria": "x"}, tc)
            inbox.close()
            await e["db"].close()
            return names, before, cancelled, updated, mid, after, cycle, running, (scaffold, design, index, css, js, v2)
    names, before, cancelled, updated, mid, after, cycle, running, (scaffold, design, index, css, js, v2) = asyncio.run(go())
    assert {"cancel_job", "update_job"} <= names
    assert [before[j.id] for j in (index, css, js)] == ["blocked"] * 3                  # the live bug's starting point
    assert index.id in cancelled and "update_job" in cancelled                        # Omi is told what it left blocked
    assert "pending" in updated and mid[design.id] == "cancelled"
    assert mid[index.id] == "pending" and mid[css.id] == "pending" and mid[js.id] == "pending"   # the chain comes back
    assert after[index.id] == "ready" and after[css.id] == "pending"                 # and runs in order once v2 is done
    assert "cycle" in cycle and "ERROR" in running                                    # guarded


def test_prompts_steer_away_from_the_live_mistakes():
    assert "later subtask" in PLANNER_PROMPT and "index.html" in PLANNER_PROMPT       # criteria can't need later output
    assert "cancel_job" in BOSS_PROMPT and "placeholder" in BOSS_PROMPT
