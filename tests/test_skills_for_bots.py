"""User, 2026-09-26: "make sure they can use skills, i have a set of skills in omni they can use or in
c:/.skills/skills". The bots had the skill tools but were never told to use them (0 calls in history),
never saw the skills Omi assigned, and 10 library skills were hidden behind slash-command clashes."""

from __future__ import annotations

from pathlib import Path

from omnibots.omni.config import SkillInfo
from omnibots.runtime.agent import WORKER_PROMPT
from omnibots.runtime.skill_tools import SkillPool


def skill(folder: Path, name: str, desc: str) -> Path:
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\n# {name}\nDo it well.\n", encoding="utf-8")
    return folder / "SKILL.md"


def test_the_library_folder_is_read_by_name_and_archives_are_skipped(tmp_path):
    lib = tmp_path / "skills"
    skill(lib / "web-coding", "web-coding", "modern CSS and HTML")
    skill(lib / "codebase-starters" / "python", "python", "start a python codebase")       # Omni hides it behind /python
    skill(lib / ".deleted-duplicates" / "old" / "web-coding-v1", "web-coding-v1", "archived")
    omni = [SkillInfo(name="python-coding", command="/python", description="python rules", path=tmp_path / "x", source="bundled"),
            SkillInfo(name="web-coding", command="/web-coding", description="omni's copy", path=tmp_path / "y", source="bundled")]
    pool = SkillPool(lambda: omni, tmp_path / "own", [lib])
    names = [s.name for s in pool.all()]
    assert "python" in names and "python-coding" in names                                # both reachable by name
    assert names.count("web-coding") == 1 and pool.get("web-coding").source == "bundled"  # Omni's copy wins a name clash
    assert "web-coding-v1" not in names                                                   # archives stay out
    assert pool.search("start python codebase")[0][1].name == "python"                     # findable by what it does
    assert pool.get("python").path.parent.name == "python"


def test_the_bots_are_told_to_use_skills_and_see_the_ones_omi_gave_them(tmp_path):
    from omnibots.bots.profile import BotProfile
    from omnibots.bots.runner import BOSS_PROMPT, JobRunner
    assert "find_skill" in WORKER_PROMPT and "invoke_skill" in WORKER_PROMPT
    assert "find_skills first and give the new bot the skills" in BOSS_PROMPT
    lib = tmp_path / "skills"
    skill(lib / "web-coding", "web-coding", "modern CSS and HTML")
    runner = JobRunner.__new__(JobRunner)                    # only system_prompt() is exercised
    runner.home = tmp_path
    runner.skill_pool = SkillPool(lambda: [], None, [lib])
    prof = BotProfile(id="b1", name="Frontend", role="frontend engineer", skills=["web-coding"],
                      folder=tmp_path / "bots" / "b1")
    text = runner.system_prompt(prof)
    assert "Your skills (invoke_skill each one" in text and "- web-coding: modern CSS and HTML" in text


def test_assigned_skills_come_loaded_in_the_job(tmp_path):
    from omnibots.bots.profile import BotProfile
    from omnibots.bots.runner import JobRunner
    lib = tmp_path / "skills"
    skill(lib / "web-coding", "web-coding", "modern CSS and HTML")
    runner = JobRunner.__new__(JobRunner)
    runner.skill_pool = SkillPool(lambda: [], None, [lib])
    prof = BotProfile(id="b1", name="Frontend", role="frontend engineer", skills=["web-coding", "no-such-skill"],
                      folder=tmp_path / "bots" / "b1")
    task = runner._with_skills(prof, "Build index.html")
    assert task.startswith("Build index.html") and "### Skill: web-coding" in task and "Do it well." in task
    assert "name: web-coding" not in task                                      # the frontmatter is stripped
    boss = BotProfile(id="omi", name="Omi", role="boss", skills=["web-coding"], folder=tmp_path / "bots" / "omi")
    assert runner._with_skills(boss, "plan it") == "plan it"


def test_code_files_end_with_a_newline_but_tokens_stay_exact(tmp_path):
    """Live 2026-09-26: two extra bots were created just to append a trailing newline."""
    import asyncio
    from omnibots.runtime.core_tools import write_file
    from omnibots.runtime.tools import ToolContext
    ctx = ToolContext(bot_id="b", workspace=tmp_path)
    for name, content, want in (("style.css", "body{}", "body{}\n"), ("index.html", "<h1>x</h1>\n", "<h1>x</h1>\n"),
                                ("token.txt", "abc123", "abc123"), ("KEY", "abc123", "abc123"),
                                ("app.json", "{{secret:api}}", "{{secret:api}}")):
        asyncio.run(write_file({"path": name, "content": content}, ctx))
        assert (tmp_path / name).read_text(encoding="utf-8") == want, name
    asyncio.run(write_file({"path": "exact.css", "content": "a{}", "exact": True}, ctx))
    assert (tmp_path / "exact.css").read_text(encoding="utf-8") == "a{}"
