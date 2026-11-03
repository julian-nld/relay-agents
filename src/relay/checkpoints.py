"""Checkpoints: a code task's files before every turn, to put them back.

Relay keeps them in a git repository of its own, in its data folder, that
looks at the task's folder from outside: the folder gets no .git, and a
repository it may already be part of is left alone. The folder's .gitignore
files are respected, and folders that are never worth keeping (installed
software, git.DEPENDENCIES, and build output) are skipped.

For a folder that isn't a git repository, the first checkpoint also serves as
the task's starting point, so Relay can show what the agent changed.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from relay import config, git

EXCLUDE = "".join(f"{pattern}\n" for pattern in (*git.DEPENDENCIES, "dist/", "build/", "target/", ".DS_Store"))
MAX_FILES = 20_000  # folders with more files than this get no checkpoints
MAX_FILE_BYTES = git.MAX_NEW_FILE  # bigger files aren't kept (a video would fill the disk turn by turn)
TOO_BIG = "relay-too-big"


class CheckpointError(Exception):
    pass


def git_dir(conversation_id: str) -> Path:
    return config.data_dir() / "checkpoints" / f"{conversation_id}.git"


def _env(conversation_id: str, directory: Path) -> dict[str, str]:
    return {"GIT_DIR": str(git_dir(conversation_id)), "GIT_WORK_TREE": str(directory)}


def copy(conversation_id: str, new_id: str) -> None:
    """Give a fork a copy of the conversation's checkpoints, so its start (the original's) is in it."""
    source, target = git_dir(conversation_id), git_dir(new_id)
    if source.is_dir() and not target.exists():
        shutil.copytree(source, target, symlinks=True)


def take(conversation_id: str, directory: Path, label: str) -> str | None:
    """Record the folder's files as they are now; the checkpoint's id.

    None when the folder can't be kept: it's gone, too big, or git is missing.
    """
    return record(conversation_id, directory, label)


def record(conversation_id: str, directory: Path, label: str, ref: str = "HEAD",
           index: Path | None = None) -> str | None:
    """take(), into `ref` (and with an index file of its own): another folder's files (a
    subagent's copy, relay.forks) next to the task's own, sharing what they have in common."""
    repo = git_dir(conversation_id)
    if (repo / TOO_BIG).exists() or not directory.is_dir():
        return None
    env = _env(conversation_id, directory)
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    try:
        if not repo.exists():
            repo.parent.mkdir(parents=True, exist_ok=True)
            git.run(directory, "init", "-q", env=env)
            (repo / "info").mkdir(exist_ok=True)
            exclude = EXCLUDE
            try:  # Relay's own data (these checkpoints included) may sit inside the folder
                exclude += f"/{config.data_dir().resolve().relative_to(directory.resolve()).as_posix()}/\n"
            except ValueError:
                pass
            (repo / "info" / "exclude").write_text(exclude)
            listed = git.run(directory, "ls-files", "-z", "--others", "--exclude-standard", env=env)
            if listed.count("\0") > MAX_FILES:
                (repo / TOO_BIG).write_text(f"More than {MAX_FILES} files\n")
                return None
        _skip_big_files(directory, env, repo)
        git.add_all(directory, env=env)
        tree = git.run(directory, "write-tree", env=env).strip()
        parent = git.run(directory, "rev-parse", "-q", "--verify", f"{ref}^{{commit}}", env=env,
                         check=False).strip() or None
        if parent and git.run(directory, "rev-parse", f"{parent}^{{tree}}", env=env).strip() == tree:
            return parent  # nothing changed since the last checkpoint
        args = ["commit-tree", tree, "-m", label[:200] or "checkpoint"]
        if parent:
            args += ["-p", parent]
        commit = git.run(directory, *args, identity=True, env=env).strip()
        git.run(directory, "update-ref", ref, commit, env=env)
        return commit
    except git.GitError:
        return None


def _skip_big_files(directory: Path, env: dict[str, str], repo: Path) -> None:
    """Leave files over MAX_FILE_BYTES out of the checkpoints, from now on."""
    listed = git.run(directory, "ls-files", "-z", "--others", "--modified", "--exclude-standard", env=env)
    big = []
    for relative in filter(None, listed.split("\0")):
        try:
            if (directory / relative).stat().st_size > MAX_FILE_BYTES:
                big.append(relative)
        except OSError:
            continue
    if big:
        exclude = repo / "info" / "exclude"
        known = exclude.read_text() if exclude.exists() else ""
        lines = [git.pattern_for(b) for b in big if git.pattern_for(b) not in known.splitlines()]
        if lines:
            exclude.write_text(known + "# too big to keep\n" + "\n".join(lines) + "\n")


def restore(conversation_id: str, directory: Path, checkpoint: str) -> str | None:
    """Put the folder's files back as they were at `checkpoint`.

    Files added since are removed; ignored files are left alone. Returns a
    checkpoint of the files just before, so this can be undone too.
    """
    env = _env(conversation_id, directory)
    if not git_dir(conversation_id).exists():
        raise CheckpointError("There are no checkpoints for this task")
    try:
        git.run(directory, "cat-file", "-e", f"{checkpoint}^{{commit}}", env=env)
    except git.GitError as exc:
        raise CheckpointError("That checkpoint doesn't exist") from exc
    before = take(conversation_id, directory, "Before restoring files")
    try:
        # The index holds the files as they are (take() just added them), so
        # this also removes files that didn't exist at the checkpoint.
        git.run(directory, "read-tree", "--reset", "-u", checkpoint, env=env)
    except git.GitError as exc:
        raise CheckpointError(f"Couldn't restore the files: {exc}") from exc
    return before


def changes(conversation_id: str, directory: Path, base: str, with_diff: bool = True) -> git.Changes:
    """What differs in the folder from checkpoint `base`."""
    return git.changes(directory, base, with_diff, env=_env(conversation_id, directory))


def has(conversation_id: str, point: str | None) -> bool:
    """Whether `point` is one of the conversation's checkpoints."""
    if not point or not git_dir(conversation_id).exists():
        return False
    try:
        git.run(Path.home(), "cat-file", "-e", f"{point}^{{commit}}", env={"GIT_DIR": str(git_dir(conversation_id))})
    except git.GitError:
        return False
    return True


def diff(conversation_id: str, directory: Path, before: str, after: str | None, limit: int = 60_000) -> str:
    """What changed from checkpoint `before` to `after` (None: the files as they are now), as a
    summary and a unified diff."""
    env = _env(conversation_id, directory)
    if after is None:
        after = take(conversation_id, directory, "Now") or before
    stat = git.run(directory, "diff", "--stat", "--find-renames", before, after, env=env)
    text = git.run(directory, "diff", "--find-renames", before, after, env=env)
    if len(text) > limit:
        text = text[:limit] + f"\n[… {len(text) - limit} more characters of diff]"
    return (stat + "\n" + text).strip() or "No changes."


def remove(conversation_id: str) -> None:
    shutil.rmtree(git_dir(conversation_id), ignore_errors=True)
