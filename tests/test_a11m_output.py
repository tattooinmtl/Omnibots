"""A11.m.01-03 (user, 2026-09-26): an output folder, one readable folder per goal, old projects
moved there, and "Open folder" projects whose history stays OUT of the user's folder."""

from __future__ import annotations

import asyncio
import subprocess
import tomllib

from omnibots.db import Database
from omnibots.projects.store import ProjectStore, folder_name
from omnibots.settings import DEFAULT_SETTINGS_TOML, load_settings, save_setting


def git(folder, *a):
    return subprocess.run(["git", *a], cwd=folder, capture_output=True, text=True).stdout.strip()


def test_folder_names_are_readable_and_windows_safe():
    name = folder_name("OmniBots showcase site: omnibots.globalwarningnetworks.com <install> page?", when=1790400000)
    assert name.startswith("2026-09-") and "showcase site" in name
    assert not any(c in name for c in '<>:"/\\|?*')
    assert folder_name("", when=0).endswith(" project")


def test_goals_land_in_the_output_folder_and_opened_folders_stay_clean(tmp_path):
    out, home = tmp_path / "omnibots_output", tmp_path / "home"
    mine = tmp_path / "my_code"                                  # the user's own repo, opened with File → Open folder
    mine.mkdir()
    (mine / "app.py").write_text("print('hi')\n", encoding="utf-8")
    for a in (["init", "-q"], ["add", "-A"], ["-c", "user.name=me", "-c", "user.email=me@x", "commit", "-q", "-m", "mine"]):
        subprocess.run(["git", *a], cwd=mine, check=True)
    my_log_before = git(mine, "log", "--oneline")

    async def go():
        db = Database(home / "db" / "omnibots.sqlite")
        await db.open()
        store = ProjectStore(db, home / "projects", output_dir=out, history_dir=home / "project-history")
        p1 = await store.create("Build the OmniBots showcase site")
        p2 = await store.create("Build the OmniBots showcase site")           # same words: a unique folder
        p3 = await store.create("fix the bug in app.py", folder=mine)
        (mine / "app.py").write_text("print('fixed')\n", encoding="utf-8")   # a bot's change in the user's folder
        sha = await store.commit(p3, "bot fixed app.py", author="coder")
        diff = await store.diff(p3)
        again = ProjectStore(db, home / "projects", output_dir=out, history_dir=home / "project-history")
        await again.load()                                                    # a restart finds every folder
        found = [again.folder(p) for p in (p1, p2, p3)]
        await db.close()
        return [store.folder(p) for p in (p1, p2, p3)], sha, diff, found, store.git_dir(p3)
    (f1, f2, f3), sha, diff, found, hist = asyncio.run(go())
    assert f1.parent == out and f1.name.endswith("Build the OmniBots showcase site") and f2.name.endswith("(2)")
    assert (f1 / "GOAL.md").exists() and (f1 / ".git").is_dir()
    assert f3 == mine and found == [f1, f2, mine]
    assert sha and "print('fixed')" in diff                                  # OmniBots still tracks the change…
    assert hist is not None and hist.parent == home / "project-history"
    assert git(mine, "log", "--oneline") == my_log_before                    # …but never commits to the user's repo
    assert not (mine / "GOAL.md").exists() and git(mine, "status", "--short") == "M app.py"


def test_old_projects_move_once_with_readable_names(tmp_path):
    home, out = tmp_path / "home", tmp_path / "omnibots_output"

    async def go():
        db = Database(home / "db" / "omnibots.sqlite")
        await db.open()
        old = ProjectStore(db, home / "projects")                             # before the output folder existed
        pid = await old.create("hello omi ready to work")
        before = old.folder(pid)
        await old.commit(pid, "x")
        new = ProjectStore(db, home / "projects", output_dir=out)
        await new.load()
        moved = await new.move_old_projects()
        twice = await new.move_old_projects()
        row = await db.read_one("SELECT path FROM projects WHERE id=?", (pid,))
        log = await new.log(pid)
        await db.close()
        return pid, before, moved, twice, row["path"], new.folder(pid), log
    pid, before, moved, twice, path, folder, log = asyncio.run(go())
    assert before.parent == home / "projects" and not before.exists()
    assert moved == [(pid, folder)] and twice == [] and path == str(folder)
    assert folder.parent == out and folder.name.endswith("hello omi ready to work") and (folder / "GOAL.md").exists()
    assert log and log[-1]["message"].startswith("project created")         # the history moved with it


def test_save_setting_keeps_the_users_file(tmp_path):
    p = tmp_path / "settings.toml"
    old = DEFAULT_SETTINGS_TOML.replace('[output]\n# Where', '[outputx]\n# Where').replace('folder = ""\n', "")  # an older file
    p.write_text("# my note\n" + old, encoding="utf-8")
    save_setting(p, "output", "folder", r"C:\omnibots_output")
    s = load_settings(p)
    assert s["output"]["folder"] == r"C:\omnibots_output" and p.read_text(encoding="utf-8").startswith("# my note")
    save_setting(p, "output", "folder", r"D:\bots out")
    assert tomllib.loads(p.read_text(encoding="utf-8"))["output"]["folder"] == r"D:\bots out"
    assert load_settings(p)["budgets"]["money_per_day_usd"] == 10.0


def test_a_follow_up_goal_reuses_our_project_folder_and_history(tmp_path):
    out, home = tmp_path / "omnibots_output", tmp_path / "home"

    async def go():
        db = Database(home / "db" / "omnibots.sqlite")
        await db.open()
        store = ProjectStore(db, home / "projects", output_dir=out, history_dir=home / "project-history")
        p1 = await store.create("Build the OmniBots showcase site")
        site = store.folder(p1)
        (site / "index.html").write_text("<h1>site</h1>\n", encoding="utf-8")
        await store.commit(p1, "site built")
        p2 = await store.create("add a css to the index.html", folder=site)
        log2 = await store.log(p2)
        await db.close()
        return site, store.folder(p2), store.git_dir(p2), log2
    site, f2, gd, log2 = asyncio.run(go())
    assert f2 == site and gd is None                                        # same folder, its own .git
    goal_md = (site / "GOAL.md").read_text(encoding="utf-8")
    assert "Build the OmniBots showcase site" in goal_md and "## Follow-up" in goal_md and "add a css" in goal_md
    assert [l["message"] for l in log2][:2] == ["follow-up goal", "site built"]
