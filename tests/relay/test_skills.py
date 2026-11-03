import os

import pytest

from relay import skills
from relay.tools_script import note


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    return tmp_path


def test_built_in_skills_and_your_own(home):
    names = [s.name for s in skills.all_skills()]
    assert "relay-subagents" in names and "relay-skill-writing" in names
    mine = skills.create("release-notes", "Writes release notes. Use when asked for them.", "1. Look at git log.", "read")
    assert mine.access == "read" and not mine.builtin
    assert skills.frontmatter(mine.path.read_text())["name"] == "release-notes"
    # Your own one wins over a built-in one of the same name
    skills.create("relay-subagents", "My own take on subagents.")
    assert skills.get("relay-subagents").description == "My own take on subagents."
    with pytest.raises(skills.SkillError):
        skills.create("Bad Name", "x")
    with pytest.raises(skills.SkillError):
        skills.create("release-notes", "again")
    with pytest.raises(skills.SkillError):
        skills.delete("relay-skill-writing")  # built in
    with pytest.raises(skills.SkillError):
        skills.read_file("release-notes", "../../escape")
    skills.write_file("release-notes", "notes.py", "print(1)\n")
    assert skills.files("release-notes") == ["SKILL.md", "notes.py"]
    skills.delete("release-notes")
    assert "release-notes" not in [s.name for s in skills.all_skills()]


def test_a_skill_without_a_description_is_not_offered(home):
    folder = skills.user_dir() / "half"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: half\n---\nNo description.")
    assert "half" not in [s.name for s in skills.all_skills()]


def test_skills_reach_agents(home):
    skills.create("deploy", "Deploys the app. Use when asked to deploy.")
    root = skills.claude_folder()
    link = root / ".claude" / "skills" / "deploy"
    assert link.is_symlink() and (link / "SKILL.md").is_file()  # what Claude Code loads with --add-dir
    skills.delete("deploy")
    skills.claude_folder()
    assert not link.exists()  # gone skills are unlinked
    listed = skills.note(skills.all_skills())
    assert "relay-subagents:" in listed and "SKILL.md" in listed  # for agents that don't load skills
    told = note(home / "relay", [s.name for s in skills.all_skills()])
    assert "relay-skill-writing" in told and "relay help skills" in told  # names; the rest on asking
    from relay.tools_script import help_text
    more = help_text("skills", skills_text=listed)
    assert "relay-subagents:" in more and str(skills.user_dir()) in more


def test_expanding_a_skill_command(home):
    assert skills.expand("review this") is None and skills.expand("/nope x") is None
    text = skills.expand("/code-review\nthe parser, please", "Context line.")
    assert text.startswith('Please use my "code-review" skill') and "Context line." in text
    assert text.endswith("What I ask: the parser, please")
    assert skills.expand("/code-review").endswith("Go ahead.")
    assert skills.body("---\nname: x\n---\n\nBody.") == "Body."


def test_unused_skill_folders_of_other_relays_are_pruned(home):
    import time
    skills.create("deploy", "Deploys. Use when asked.")
    own = skills.claude_folder()
    other = own.parent / "0123456789ab"
    other.mkdir()
    os.utime(other, (time.time() - 40 * 86400,) * 2)
    os.utime(own, (time.time() - 40 * 86400,) * 2)
    skills.claude_folder()  # used again: fresh
    assert skills.prune_claude_folders() == 1 and own.exists() and not other.exists()
