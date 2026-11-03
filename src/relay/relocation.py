"""When Relay's data or code moved: the paths it stored, and its own Claude login.

When the data turns up somewhere else (moved to another folder, the code folder
renamed, or all of it on another machine under another user, whose home folder
is elsewhere), relocate() rewrites the paths Relay stored and repairs the git
worktrees of code tasks. It runs at every start; it does nothing when nothing
moved.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from opencode_manager import paths
from relay import config, forks
from relay.db import Database


def claude_dir() -> Path:
    """Claude Code's config folder of Relay's own (`relay claude`): a login apart from ~/.claude."""
    return config.data_dir() / "claude"


# Moving

# Every stored path, rewritten when Relay moved (a new table with paths belongs here).
STORED_PATHS = (("conversations", "directory"), ("conversations", "repo_root"), ("conversations", "worktree"),
                ("uploads", "path"), ("shares", "directory"), ("schedules", "directory"), ("repairs", "folder"),
                ("service_versions", "folder"), ("service_versions", "source"), ("service_versions", "data_snapshot"))


def relocate(db: Database) -> list[str]:
    """Rewrite stored paths when the install moved; notes on what was done."""
    now = {"root": str(paths.root().resolve()), "data": str(config.data_dir().resolve()),
           "home": str(Path.home().resolve())}
    stored = {row["key"]: row["value"] for row in db.all("SELECT key, value FROM meta")}
    if not stored:
        stored = _guess_where_it_was(db)
    if not stored.get("home") and str(stored.get("data", "")).endswith("/.local/share/relay"):
        # (noted before Relay kept its home folder: the default data folder is in it)
        stored["home"] = stored["data"][:-len("/.local/share/relay")]
    notes = []
    # The data folder first: it's usually inside the root, and more specific; the home folder last
    # (the projects in it, when it's another user's on another machine).
    moves = [(stored[k], now[k]) for k in ("data", "root", "home") if stored.get(k) and stored[k] != now[k]]
    if moves:
        for old, new in moves:
            notes += _move_claude_sessions(db, old, new)
            for table, column in STORED_PATHS:
                db.execute(
                    f"UPDATE {table} SET {column} = ? || substr({column}, ?) "
                    f"WHERE {column} = ? OR substr({column}, 1, ?) = ?",
                    (new, len(old) + 1, old, len(old) + 1, old + "/"))
            _move_settings(old, new)
            forks.moved(old, new)
            notes.append(f"moved from {old} to {new}")
        notes += _repair_worktrees(db)
    for key, value in now.items():
        db.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?",
                   (key, value, value))
    return notes


def _move_claude_sessions(db: Database, old: str, new: str) -> list[str]:
    """Claude Code keeps sessions per folder (~/.claude/projects/<folder>): take those of
    conversations that move along, so they can still be resumed."""
    from relay.providers.claude import claude_project_folder
    bases = {Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude"), claude_dir()}
    rows = db.all("SELECT DISTINCT directory FROM conversations WHERE directory = ? OR substr(directory, 1, ?) = ?",
                  (old, len(old) + 1, old + "/"))
    moved = 0
    for row in rows:
        before, after = row["directory"], new + row["directory"][len(old):]
        for base in bases:
            source = base / "projects" / claude_project_folder(Path(before))
            target = base / "projects" / claude_project_folder(Path(after))
            if source.is_dir() and not target.exists():
                try:
                    source.rename(target)
                    moved += 1
                except OSError:
                    pass
    return [f"moved {moved} Claude Code session folder(s) along"] if moved else []


def _guess_where_it_was(db: Database) -> dict[str, str]:
    """For data from before Relay noted its place: where it was, judging by the
    folders it keeps inside its data (chat folders, task worktrees)."""
    data_name = config.data_dir().name  # "relay", inside opencode-manager's data folder
    for row in db.all("SELECT directory FROM conversations WHERE directory LIKE ? OR directory LIKE ? LIMIT 1",
                      (f"%/{data_name}/chats/%", f"%/{data_name}/worktrees/%")):
        marker = next(m for m in (f"/{data_name}/chats/", f"/{data_name}/worktrees/") if m in row["directory"])
        data = row["directory"].split(marker)[0] + f"/{data_name}"
        guess = {"data": data}
        manager_data = Path(data).parent  # opencode-manager's data folder; the root holds it as data/
        if manager_data.name == "data":
            guess["root"] = str(manager_data.parent)
        return guess
    return {}


def _move_settings(old: str, new: str) -> None:
    path = config.settings_path()
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return

    def moved(value: str) -> str:
        return new + value[len(old):] if value == old or value.startswith(old + "/") else value

    roots = data.get("workspace_roots")
    if isinstance(roots, list):
        data["workspace_roots"] = [moved(r) if isinstance(r, str) else r for r in roots]
    if isinstance(data.get("chat_root"), str):
        data["chat_root"] = moved(data["chat_root"])
    path.write_text(json.dumps(data, indent=2) + "\n")


def _repair_worktrees(db: Database) -> list[str]:
    """git records a worktree's absolute path on both sides; point them at each other again."""
    notes = []
    for row in db.all("SELECT repo_root, worktree FROM conversations "
                      "WHERE git_mode = 'worktree' AND task_state = 'open'"):
        repo, worktree = Path(row["repo_root"] or ""), Path(row["worktree"] or "")
        if repo.is_dir() and worktree.is_dir():
            result = subprocess.run(["git", "worktree", "repair", str(worktree)], cwd=repo,
                                    capture_output=True, text=True)
            if result.returncode != 0:
                notes.append(f"couldn't repair the worktree {worktree}: {result.stderr.strip()}")
    return notes


def remove_old_tools_scripts() -> None:
    """Relay's tools scripts used to live in the old data folder (data/relay/tools); they're on the host now."""
    old = config.data_dir() / "tools"
    if not old.is_dir():
        return
    for folder in [old, *old.rglob("*")]:
        try:
            folder.chmod(0o755 if folder.is_dir() else 0o644)
        except OSError:
            pass
    shutil.rmtree(old, ignore_errors=True)


def run_claude(args: list[str]) -> int:
    """Run Claude Code with Relay's own login folder, e.g. to log in (`/login`)."""
    claude_dir().mkdir(parents=True, exist_ok=True)
    program = paths.program("claude")
    if shutil.which(program) is None:
        print("relay: Claude Code isn't installed here", file=sys.stderr)
        return 1
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(claude_dir())}
    os.execvpe(program, [program, *args], env)
    return 0  # not reached
