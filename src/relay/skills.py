"""Skills: instructions (and scripts) for particular jobs, in the Agent Skills format.

A skill is a folder with a SKILL.md that starts with a little frontmatter:

    ---
    name: release-notes
    description: Writes release notes from the git log. Use when asked for release notes.
    access: read          # optional, Relay's own: "read" if it never changes files
    ---
    (the instructions, read when the skill is used)

Relay ships a few (src/relay/builtin_skills/); your own go in ~/.local/share/relay/skills/, so
they travel with Relay, and win over a built-in one with the same name.

Agents only see names and descriptions up front and read a skill when they
need it. Claude Code loads them as its own skills (from a folder with
.claude/skills that Relay passes with --add-dir); other agents get the list
in Relay's note, with each SKILL.md's path; chat models get tools to list
and read them.

People can also call one themselves, like a command: `/code-review the
parser` sends the agent the skill's instructions with the request (expand()).
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from opencode_manager import paths
from relay import config

BUILTIN = Path(__file__).parent / "builtin_skills"
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
COMMAND = re.compile(r"^/([a-z0-9][a-z0-9-]{0,63})(?:\s+([\s\S]*))?$")
MAX_FILE = 512 * 1024  # a skill file shown or edited in the settings


class SkillError(Exception):
    pass


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    folder: Path
    builtin: bool
    access: str = "edit"  # "read" when it promises not to change files

    @property
    def path(self) -> Path:
        return self.folder / "SKILL.md"

    def public(self) -> dict:
        return {"name": self.name, "description": self.description, "path": str(self.path),
                "builtin": self.builtin, "access": self.access}


def user_dir() -> Path:
    return config.data_dir() / "skills"


def frontmatter(text: str) -> dict[str, str]:
    """The simple `key: value` lines between the first two `---` lines."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    found = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return found
        key, sep, value = line.partition(":")
        if sep and key.strip():
            found[key.strip().lower()] = value.strip().strip("'\"")
    return {}


def load(folder: Path, builtin: bool) -> Skill | None:
    try:
        meta = frontmatter((folder / "SKILL.md").read_text(errors="replace"))
    except OSError:
        return None
    name = meta.get("name") or folder.name
    if not NAME.match(name) or not meta.get("description"):
        return None
    access = "read" if meta.get("access", "").lower() == "read" else "edit"
    return Skill(name, " ".join(meta["description"].split())[:1024], folder, builtin, access)


def all_skills() -> list[Skill]:
    """Every skill, the user's own over a built-in one of the same name."""
    found: dict[str, Skill] = {}
    for base, builtin in ((BUILTIN, True), (user_dir(), False)):
        if not base.is_dir():
            continue
        for folder in sorted(base.iterdir()):
            if folder.is_dir() and (skill := load(folder, builtin)):
                found[skill.name] = skill
    return sorted(found.values(), key=lambda s: s.name)


def get(name: str) -> Skill:
    skill = next((s for s in all_skills() if s.name == name), None)
    if skill is None:
        raise SkillError(f"No skill {name!r}")
    return skill


# For agents


def claude_folder() -> Path | None:
    """A folder with .claude/skills/<name> linking to every skill, for Claude
    Code's --add-dir. On the machine itself, rebuilt as skills come and go."""
    skills = all_skills()
    if not skills:
        return None
    key = hashlib.sha256(str(config.data_dir().resolve()).encode()).hexdigest()[:12]
    root = paths.host_cache_dir() / "relay-skills" / key
    links = root / ".claude" / "skills"
    links.mkdir(parents=True, exist_ok=True)
    wanted = {s.name: s.folder.resolve() for s in skills}
    for link in links.iterdir():
        if link.name not in wanted or not link.is_symlink() or link.resolve() != wanted[link.name]:
            link.unlink() if link.is_symlink() or link.is_file() else shutil.rmtree(link, ignore_errors=True)
    for name, target in wanted.items():
        if not (links / name).exists():
            (links / name).symlink_to(target, target_is_directory=True)
    os.utime(root)  # in use: prune_claude_folders() leaves it alone
    return root


def prune_claude_folders(older_than: float = 30 * 86400) -> int:
    """Remove other Relays' skill folders (other data folders, test runs) nobody used for a month."""
    base = paths.host_cache_dir() / "relay-skills"
    own = hashlib.sha256(str(config.data_dir().resolve()).encode()).hexdigest()[:12]
    removed = 0
    for entry in base.iterdir() if base.is_dir() else []:
        try:
            if entry.name != own and entry.stat().st_mtime < time.time() - older_than:
                shutil.rmtree(entry)
                removed += 1
        except OSError:
            continue
    return removed


def note(skills: list[Skill]) -> str:
    """The skills, for agents that don't load them themselves."""
    if not skills:
        return ""
    lines = [f"- {s.name}: {s.description} (read {s.path})" for s in skills]
    return ("Relay also offers skills: instructions for particular jobs. When one fits, read its SKILL.md "
            "first and follow it:\n" + "\n".join(lines))


def body(text: str) -> str:
    """A SKILL.md without its frontmatter."""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for index, line in enumerate(lines[1:], 1):
            if line.strip() == "---":
                return "\n".join(lines[index + 1:]).strip()
    return text.strip()


def command_name(prompt: str) -> str | None:
    """The skill a prompt calls as a command (`/code-review …`), if any."""
    match = COMMAND.match(prompt.strip())
    if not match or not any(s.name == match.group(1) for s in all_skills()):
        return None
    return match.group(1)


def expand(prompt: str, context: str = "") -> str | None:
    """A skill called as a command, as the prompt an agent gets: the skill's
    instructions, what Relay knows that helps (`context`) and the request.
    None if the prompt doesn't call a skill."""
    name = command_name(prompt)
    if name is None:
        return None
    skill = get(name)
    asked = (COMMAND.match(prompt.strip()).group(2) or "").strip()
    # Worded as the user asking, like a mode's instructions.
    text = (f"Please use my \"{skill.name}\" skill for this. Its instructions (from {skill.path}; "
            f"paths in it are relative to {skill.folder}):\n\n<skill>\n{body(read_file(name))}\n</skill>")
    if context:
        text += f"\n\n{context}"
    return text + (f"\n\nWhat I ask: {asked}" if asked else "\n\nGo ahead.")


def signature(skills: list[Skill]) -> str:
    """Changes when skills come or go, so agents are told again."""
    return ",".join(s.name for s in skills)


# Managing your own


def create(name: str, description: str, instructions: str = "", access: str = "edit") -> Skill:
    name = name.strip().lower()
    if not NAME.match(name):
        raise SkillError("A skill's name is lowercase letters, digits and dashes")
    if not description.strip():
        raise SkillError("A skill needs a description: what it does and when to use it")
    folder = user_dir() / name
    if folder.exists():
        raise SkillError(f"There's already a skill {name!r}")
    folder.mkdir(parents=True)
    head = f"---\nname: {name}\ndescription: {' '.join(description.split())}\n"
    if access == "read":
        head += "access: read\n"
    (folder / "SKILL.md").write_text(head + "---\n\n" + (instructions.strip() or "Describe the steps here.") + "\n")
    return get(name)


def delete(name: str) -> None:
    skill = get(name)
    if skill.builtin:
        raise SkillError("Built-in skills can't be removed")
    shutil.rmtree(skill.folder)


def files(name: str) -> list[str]:
    skill = get(name)
    return sorted(str(p.relative_to(skill.folder)) for p in skill.folder.rglob("*")
                  if p.is_file() and ".git" not in p.parts)


def _inside(skill: Skill, relative: str) -> Path:
    target = (skill.folder / relative).resolve()
    if skill.folder.resolve() not in target.parents:
        raise SkillError("That file isn't in the skill's folder")
    return target


def read_file(name: str, relative: str = "SKILL.md") -> str:
    target = _inside(get(name), relative)
    if not target.is_file():
        raise SkillError(f"No file {relative!r} in skill {name!r}")
    if target.stat().st_size > MAX_FILE:
        raise SkillError("That file is too big to show")
    return target.read_text(errors="replace")


def write_file(name: str, relative: str, content: str) -> Skill:
    skill = get(name)
    if skill.builtin:
        raise SkillError("Built-in skills can't be changed; make your own with the same name to replace one")
    target = _inside(skill, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    if relative == "SKILL.md" and load(skill.folder, False) is None:
        raise SkillError("Saved, but SKILL.md needs a frontmatter with a name and a description to be used")
    return get(name) if relative != "SKILL.md" else (load(skill.folder, False) or skill)
