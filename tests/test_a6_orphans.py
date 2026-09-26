"""A6.c.01: after a crash or kill, startup marks cut-off work as interrupted (so Start picks it
up), puts working bots back to idle and expires approvals nobody waits for any more."""

from __future__ import annotations

import sqlite3

from omnibots.engine import Engine


def test_startup_recovers_what_a_kill_left_behind(tmp_path):
    db = tmp_path / "db" / "omnibots.sqlite"
    eng = Engine(db, keep_awake=False, home=tmp_path, omni_install_root="")
    eng.start()
    eng.stop()
    # what a kill mid-goal leaves in SQL (the app died; nothing got to clean up)
    c = sqlite3.connect(db)
    c.execute("INSERT INTO projects (id, goal, created_by) VALUES ('p1', 'write a haiku', 'user')")
    c.execute("INSERT INTO projects (id, goal, status, created_by) VALUES ('p2', 'old goal', 'done', 'user')")
    c.execute("INSERT INTO jobs (id, project_id, title, status) VALUES ('j_old', 'p2', 'routed around', 'ready')")
    c.execute("INSERT INTO messages (topic, sender_type, sender_id, message_type, payload_json, project_id) "
              "VALUES ('#project/p2', 'bot', 'reviewer', 'REVIEW_RESULT', '{\"text\": \"VERDICT PASS\\n\"}', 'p2')")
    # done but never passed review (the boss just stopped): its planned work stays
    c.execute("INSERT INTO projects (id, goal, status, created_by) VALUES ('p3', 'unreviewed', 'done', 'user')")
    c.execute("INSERT INTO jobs (id, project_id, title, status) VALUES ('j_keep', 'p3', 'still planned', 'ready')")
    for jid, status in (("j_run", "running"), ("j_rev", "review"), ("j_wait", "waiting_approval"), ("j_done", "completed"),
                        ("j_new", "pending")):
        c.execute("INSERT INTO jobs (id, project_id, title, status, assigned_bot_id) VALUES (?, 'p1', ?, ?, 'omi')", (jid, jid, status))
    c.execute("UPDATE bots SET status='working' WHERE id='omi'")
    c.execute("INSERT INTO approvals (id, job_id, bot_id, risk_class, action, summary) VALUES ('a1', 'j_wait', 'omi', 'R3', 'git_push', 'push')")
    c.commit()
    c.close()

    eng = Engine(db, keep_awake=False, home=tmp_path, omni_install_root="")
    eng.start()
    try:
        tray = eng.submit(eng.ui_tray()).result(timeout=10)
        # the counting helper really counts (it used SELECT changes() on a read connection: always 0)
        n = eng.submit(eng.team._set_jobs(("interrupted",), "interrupted")).result(timeout=10)
    finally:
        eng.stop()
    c = sqlite3.connect(db)
    jobs = dict(c.execute("SELECT id, status FROM jobs"))
    assert jobs.pop("j_old") == "cancelled"                                # a passed project keeps no open work
    assert jobs.pop("j_keep") == "ready"                                   # no PASS: nothing is thrown away
    assert jobs == {"j_run": "interrupted", "j_rev": "interrupted", "j_wait": "interrupted", "j_done": "completed", "j_new": "pending"}
    assert c.execute("SELECT status FROM bots WHERE id='omi'").fetchone()[0] == "idle"
    assert c.execute("SELECT status FROM approvals WHERE id='a1'").fetchone()[0] == "expired"
    audit = c.execute("SELECT details_json FROM audit_logs WHERE action='orphans_recovered'").fetchone()
    assert audit and '"jobs": 4' in audit[0]
    # the tray sees a stopped team with work to pick up: Start is offered
    assert tray["team"] == "stopped" and tray["interrupted"] == 3 and not tray["running"]
    assert n == 3


def test_a_completed_goal_closes_jobs_it_never_needed(tmp_path):
    """A6.c.02 (live 2026-09-26): Omi planned two jobs, routed around them, the goal passed, and
    they stayed ready/pending forever in a finished project."""
    import asyncio

    from mock_provider import MockProviders
    from test_a7_orchestrator import build

    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path, mock)
            pid = await e["projects"].create("g")
            g = e["graph"]
            first = await g.add_job(pid, "select colors", description="x", done_criteria="y")
            second = await g.add_job(pid, "write colors.md", description="x", done_criteria="y", depends_on=[first.id])
            used = await g.add_job(pid, "overwrite colors.md", description="x", done_criteria="y")
            await e["db"].write("UPDATE jobs SET status='completed' WHERE id=?", (used.id,))
            unreviewed = await e["orch"]._close_unneeded_jobs(pid)             # no reviewer PASS yet: keep everything
            await e["bus"].publish(f"#project/{pid}", "REVIEW_RESULT", {"text": "VERDICT FAIL\nmissing file"}, sender_type="bot",
                                   sender_id="reviewer", project_id=pid)
            failed = await e["orch"]._close_unneeded_jobs(pid)
            await e["bus"].publish(f"#project/{pid}", "REVIEW_RESULT", {"text": "VERDICT PASS\n"}, sender_type="bot",
                                   sender_id="reviewer", project_id=pid)
            closed = await e["orch"]._close_unneeded_jobs(pid)
            rows = {r["id"]: (r["status"], r["error_message"]) for r in await e["db"].read("SELECT * FROM jobs WHERE project_id=?", (pid,))}
            await e["db"].close()
            return first, second, used, closed, rows, unreviewed, failed
    first, second, used, closed, rows, unreviewed, failed = asyncio.run(go())
    assert unreviewed == [] and failed == []
    assert sorted(closed) == sorted([first.id, second.id])
    assert rows[first.id] == ("cancelled", "not needed: the goal was completed without it") and rows[second.id][0] == "cancelled"
    assert rows[used.id][0] == "completed"


def test_quitting_mid_goal_leaves_the_work_resumable(tmp_path):
    """Live 2026-09-26: Exit/restart turned Omi's running goal into `cancelled` (lost); the tray's
    Stop made it `interrupted`. A normal quit now matches Stop."""
    import time
    from pathlib import Path

    from mock_provider import MockProviders, sse
    from omnibots.bots.profile import BOSS_ID
    from omnibots.omni.config import OmniConfig, ProviderInfo
    from omnibots.omni.locate import OmniLocation

    with MockProviders() as mock:
        mock.script("boss", sse("", tool_calls=[{"name": "ask_user", "args": {"question": "What's the goal?", "timeout_seconds": 60}}]))
        eng = Engine(tmp_path / "db" / "omnibots.sqlite", keep_awake=False, home=tmp_path, omni_install_root="")
        eng.start()
        try:
            providers = {"boss": ProviderInfo(name="boss", base_url=mock.url("boss"), api_key="k", key_source="settings",
                                              raw={"reasoningParam": "none"})}
            eng.omni = OmniConfig(OmniLocation(Path("."), Path(".")), providers,
                                  {"boss/m": {"provider": "boss", "id": "boss", "maxTokens": 256}}, None, None, [], {})
            eng.submit(eng.registry.update(BOSS_ID, chain=["boss/m"])).result(timeout=10)
            eng.submit(eng.start_goal("hello omi")).result(timeout=10)
            end = time.monotonic() + 15
            while eng.bot_states.get("omi") != "waiting_answer" and time.monotonic() < end:
                time.sleep(0.05)
            assert eng.bot_states.get("omi") == "waiting_answer"
        finally:
            eng.stop()                                               # a normal quit, mid-goal
    c = sqlite3.connect(tmp_path / "db" / "omnibots.sqlite")
    assert [r[0] for r in c.execute("SELECT status FROM jobs")] == ["interrupted"]
