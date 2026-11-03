"""Code tasks and git: following what they change, reviewing it, branches, merge and discard.

What an agent changes is followed with Relay's own checkpoints (relay.checkpoints): a
repository of Relay's, outside the project, with the files before and after every turn.
That's for a folder without git and for a task working in a repository's folder alike:
Relay never writes to the user's git history by itself. A commit there is a deliberate
step (`commit`: the Commit button, or an agent the user asked, with `relay commit`).
A task on its own branch (`relay/task-…`, in a worktree of its own) is the exception: that
branch is Relay's, its turns are commits there, and Merge puts them on the user's branch as
one commit, when the user says so.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from relay import checkpoints, config, git


class TaskError(Exception):
    pass


@dataclass
class Setup:
    """Where a new code task's agent works, and how it is tracked."""

    directory: Path
    git_mode: str = "none"
    repo_root: str | None = None
    base_branch: str | None = None
    base_commit: str | None = None
    branch: str | None = None
    worktree: str | None = None
    in_place: str | None = None  # why it works in the folder itself, though a branch of its own was asked for


def setup(conversation_id: str, directory: Path, isolate: bool) -> Setup:
    """Prepare a code task in `directory` (a folder the user picked)."""
    root = git.repo_root(directory)
    if root is None:
        return Setup(directory, in_place="it isn't in a git repository" if isolate else None)
    base_commit = git.head(root)
    base_branch = git.current_branch(root)
    relative = directory.resolve().relative_to(root.resolve()).as_posix()
    # In place (the default); also, asked for a branch of its own, without commits or on a detached HEAD (nothing to
    # branch from and merge into), and for a folder a branch would start without: one that isn't committed yet, or
    # has uncommitted changes. Its changes count from a first checkpoint (taken by the caller), and its turns are
    # checkpoints, not commits. Asked for one, it says why not (in_place): a script was none the wiser.
    why = (None if not isolate else "the repository has no commits yet" if base_commit is None
           else "the repository isn't on a branch (a detached HEAD)" if base_branch is None
           else "the folder has changes that aren't committed, or isn't committed yet"
           if not git.clean_in_commit(root, base_commit, relative) else None)
    if not isolate or why:
        return Setup(directory, "direct", str(root), in_place=why)
    worktree = config.worktrees_dir() / conversation_id
    branch = f"relay/task-{conversation_id[:8]}"
    try:
        git.create_worktree(root, worktree, branch, base_commit)
    except git.GitError as exc:
        raise TaskError(f"Couldn't create a branch for this task: {exc}") from exc
    # Keep the agent in the same subfolder the user picked.
    agent_directory = worktree / relative
    return Setup(agent_directory, "worktree", str(root), base_branch, base_commit, branch,
                 str(worktree))


def fork(row: dict[str, Any], conversation_id: str) -> Setup:
    """Where a fork of the code task `row` works.

    A task on its own branch gets another branch, starting from the task's
    files as they are now; its changes count from the same base, so the fork
    shows everything the task did too. Other tasks share their folder.
    """
    if row["git_mode"] != "worktree":
        return Setup(Path(row["directory"]), row["git_mode"], row["repo_root"], row["base_branch"],
                     row["base_commit"])
    root, old = Path(row["repo_root"]), Path(row["worktree"])
    worktree = config.worktrees_dir() / conversation_id
    branch = f"relay/task-{conversation_id[:8]}"
    try:
        start = git.snapshot(old, f"Relay: files of task {row['branch']} when it was forked")
        git.create_worktree(root, worktree, branch, start)
    except git.GitError as exc:
        raise TaskError(f"Couldn't create a branch for the fork: {exc}") from exc
    agent_directory = worktree / Path(row["directory"]).relative_to(old)
    return Setup(agent_directory, "worktree", str(root), row["base_branch"], row["base_commit"],
                 branch, str(worktree))


def checkpointed(row: dict[str, Any]) -> bool:
    """Whether the task's turns are followed with Relay's checkpoints (everything but a task on
    its own branch)."""
    return row["git_mode"] in ("none", "direct")


def tracked_directory(row: dict[str, Any]) -> Path | None:
    """The checkout whose changes belong to the task."""
    if row["git_mode"] == "worktree":
        return Path(row["worktree"])
    if row["git_mode"] == "direct":
        return Path(row["repo_root"])
    return None


def scope(row: dict[str, Any]) -> str | None:
    """For a task working in a repository directly: the subfolder it works in
    (relative to the repository), when that isn't the whole repository."""
    if row["git_mode"] != "direct" or not row["repo_root"]:
        return None
    try:
        relative = Path(row["directory"]).resolve().relative_to(Path(row["repo_root"]).resolve())
    except ValueError:
        return None
    return None if str(relative) == "." else relative.as_posix()


def changes(row: dict[str, Any], with_diff: bool = True) -> git.Changes | None:
    if checkpointed(row) and row["base_commit"] and checkpoints.has(row["id"], row["base_commit"]):
        # Since a checkpoint: where the task started (or was last marked as reviewed).
        directory = Path(row["directory"])
        if row["task_state"] != "open" or not directory.is_dir():
            return None
        return checkpoints.changes(row["id"], directory, row["base_commit"], with_diff)
    if row["git_mode"] == "none":
        return None
    directory = tracked_directory(row)
    if directory is None or row["task_state"] != "open" or not directory.is_dir():
        return None
    # Since the task started (turns are committed as they go). Other tasks may
    # work elsewhere in the same repository. A repository without commits then
    # started from nothing.
    base = row["base_commit"] or (git.EMPTY_TREE if row["git_mode"] == "direct" else None)
    return git.changes(directory, base, with_diff=with_diff, only=scope(row))


def describe(row: dict[str, Any], with_diff: bool = True) -> dict[str, Any]:
    """The task's git state and changes, for the API."""
    info: dict[str, Any] = {
        "git_mode": row["git_mode"], "task_state": row["task_state"],
        "branch": row["branch"], "base_branch": row["base_branch"], "repo_root": row["repo_root"],
        "files": [], "summary": {"files": 0, "additions": 0, "deletions": 0}, "truncated": False,
        # Changes Relay follows with its checkpoints (not commits).
        "checkpoints": checkpointed(row) and bool(row["base_commit"]),
        "scope": scope(row),  # the subfolder, when working in part of a repository
    }
    try:
        result = changes(row, with_diff)
    except git.GitError as exc:
        info["error"] = str(exc)
        return info
    if result is not None:
        info["files"] = [f.__dict__ for f in result.files]
        info["summary"] = result.summary()
        info["truncated"] = result.truncated
        if with_diff and row["git_mode"] == "direct":
            # (what Commit takes: the whole folder, also what other chats and you changed in it)
            only = scope(row)
            info["uncommitted"] = git.changed_files(Path(row["repo_root"]), [only] if only else None)
    return info


def summary(row: dict[str, Any]) -> dict[str, int] | None:
    try:
        result = changes(row, with_diff=False)
    except git.GitError:
        return None
    return result.summary() if result is not None else None


def merge(row: dict[str, Any]) -> str:
    """Put the task's work on the branch it started from, as one commit.

    Returns the task branch's new head, which becomes the task's base: after a
    merge nothing is left to review until the agent changes more.
    """
    if row["git_mode"] != "worktree" or row["task_state"] != "open":
        raise TaskError("Only an open task on its own branch can be merged")
    worktree = Path(row["worktree"])
    try:
        git.commit_changes(worktree, "Relay: changes made outside Relay")
        git.merge(Path(row["repo_root"]), row["branch"], row["base_branch"], f"{row['title']} (Relay task)")
    except git.GitError as exc:
        raise TaskError(str(exc)) from exc
    # Take the squashed commit back into the task branch, so a later merge
    # only brings what came after. If that conflicts, leave the branch as it
    # was: never a half-done merge the next turn would commit.
    try:
        git.run(worktree, *git.RELAY_COMMIT, "merge", "-q", "--no-edit", row["base_branch"])
    except git.GitError:
        git.run(worktree, "merge", "--abort", check=False)
    return git.head(worktree)


def mark_reviewed(row: dict[str, Any]) -> str:
    """For a task working in a folder itself: its changes so far are seen, so they count from
    here (as a merge does for a task on its own branch). Returns the new base: a checkpoint."""
    if not checkpointed(row) or row["task_state"] != "open":
        raise TaskError("Only an open task working in its folder can be marked as reviewed")
    directory = Path(row["directory"])
    if not directory.is_dir():
        raise TaskError("The task's folder is gone")
    point = checkpoints.take(row["id"], directory, "Reviewed")
    if point is None:
        raise TaskError("Relay can't follow this folder's changes (too many files, or git is missing)")
    return point


def commit(row: dict[str, Any], message: str) -> str:
    """Commit the task's folder as it is now to the user's repository (its current branch, as
    the user): only when the user says so. Returns the commit."""
    message = message.strip()
    if not message:
        raise TaskError("Say what the commit is (its message)")
    if row["git_mode"] == "worktree":
        raise TaskError("This task works on its own branch, where its turns are commits already: merge it")
    if row["git_mode"] != "direct" or not row["repo_root"]:
        raise TaskError("This folder isn't a git repository")
    directory = Path(row["repo_root"])
    try:
        if (what := git.busy(directory)):
            raise TaskError(f"git is in the middle of {what}; finish that first")
        made = git.commit_changes(directory, message, only=scope(row), as_relay=False)
    except git.GitError as exc:
        raise TaskError(f"Couldn't commit: {exc}") from exc
    if made is None:
        raise TaskError("Nothing to commit: the task's folder is as in the last commit")
    return made


def discard(row: dict[str, Any]) -> None:
    """Throw away the task's branch and worktree."""
    if row["git_mode"] != "worktree":
        raise TaskError("Only a task on its own branch can be discarded")
    git.remove_worktree(Path(row["repo_root"]), Path(row["worktree"]), row["branch"])


# Every turn is recorded: before a run the files as they are (the user's edits included), after
# it what the agent did. Checkpoints for a task in a folder; commits on a task's own branch.
# Rewinding makes the files what they were before a turn; nothing is rewritten.


def before_run(row: dict[str, Any], prompt: str) -> str | None:
    """Record the files before a run; the point to rewind to."""
    if checkpointed(row):
        return checkpoints.take(row["id"], Path(row["directory"]), f"Before: {prompt}")
    directory = tracked_directory(row)
    if directory is None or not directory.is_dir():
        return None
    try:
        if git.busy(directory):
            return None  # the user is in the middle of something: no commits now
        git.commit_changes(directory, "Changes made outside Relay", only=scope(row), as_relay=False)
        return git.head(directory)
    except git.GitError:
        return None


def after_run(row: dict[str, Any], prompt: str) -> str | None:
    """Record what the agent changed in a run; the point after it."""
    first = " ".join(prompt.split())
    subject = first if len(first) <= 60 else first[:59].rstrip() + "…"
    if checkpointed(row):
        return checkpoints.take(row["id"], Path(row["directory"]), f"After: {subject}")
    directory = tracked_directory(row)
    if directory is None or not directory.is_dir():
        return None
    try:
        if git.busy(directory):
            return None
        git.commit_changes(directory, f"Relay: {subject}\n\nTask: {row['title']}\nRelay-Task: {row['id']}",
                           only=scope(row))
        return git.head(directory)
    except git.GitError:
        return None


def turn_diff(row: dict[str, Any], before: str, after: str | None) -> str:
    """What changed from `before` to `after` (None: the files now), as text for an agent."""
    if checkpointed(row) and checkpoints.has(row["id"], before):
        return checkpoints.diff(row["id"], Path(row["directory"]), before, after)
    directory = tracked_directory(row)
    if directory is None or not directory.is_dir():
        raise TaskError("The task's folder is gone")
    target = [after] if after else []
    only = ["--", scope(row)] if scope(row) else []
    try:
        stat = git.run(directory, "diff", "--stat", "--find-renames", before, *target, *only)
        text = git.run(directory, "diff", "--find-renames", before, *target, *only)
    except git.GitError as exc:
        raise TaskError(str(exc)) from exc
    if len(text) > 60_000:
        text = text[:60_000] + f"\n[… {len(text) - 60_000} more characters of diff]"
    return (stat + "\n" + text).strip() or "No changes."


class OthersChanges(TaskError):
    """Rewinding would also undo commits that aren't this task's."""

    def __init__(self, subjects: list[str]) -> None:
        self.subjects = subjects
        listed = "; ".join(f"“{s}”" for s in subjects[:5]) + (" …" if len(subjects) > 5 else "")
        super().__init__(f"This would also undo {len(subjects)} commit{'s' if len(subjects) != 1 else ''} "
                         f"that aren't this task's: {listed}")


def rewind(row: dict[str, Any], target: str, label: str = "", force: bool = False) -> str | None:
    """Make the task's files what they were at `target` (a commit or
    checkpoint); returns the point just before, to undo it.

    Unless `force`, refuses when commits that aren't this task's changed its
    folder since then (yours, or another task's): they'd be undone too."""
    if checkpointed(row) and checkpoints.has(row["id"], target):
        try:
            return checkpoints.restore(row["id"], Path(row["directory"]), target)
        except checkpoints.CheckpointError as exc:
            raise TaskError(str(exc)) from exc
    if row["git_mode"] == "none":
        raise TaskError("That point doesn't exist")
    directory = tracked_directory(row)
    if directory is None or not directory.is_dir():
        raise TaskError("The task's folder is gone")
    if row["git_mode"] == "direct":
        # A point from before Relay followed tasks in folders with checkpoints: a commit of the
        # repository. The files go back to it; nothing is committed (a checkpoint undoes it).
        try:
            git.run(directory, "cat-file", "-e", f"{target}^{{commit}}")
            before = checkpoints.take(row["id"], Path(row["directory"]), "Before restoring files")
            # (the files only: the user's staged changes stay as they are)
            git.run(directory, "restore", f"--source={target}", "--worktree", "--", scope(row) or ".")
        except git.GitError as exc:
            raise TaskError(f"Couldn't rewind: {exc}") from exc
        return before
    try:
        git.run(directory, "cat-file", "-e", f"{target}^{{commit}}")
    except git.GitError as exc:
        raise TaskError("That point doesn't exist in the repository") from exc
    if (what := git.busy(directory)):
        raise TaskError(f"The repository is in the middle of {what}; finish that first")
    if not force:
        mine = f"Relay-Task: {row['id']}"
        others = [subject for subject, message in git.commits_since(directory, target, scope(row))
                  if mine not in message]
        if others:
            raise OthersChanges(others)
    try:
        git.commit_changes(directory, "Changes made outside Relay", only=scope(row), as_relay=False)
        before = git.head(directory)
        git.restore(directory, target, only=scope(row))
        what = f"before “{label}”" if label else target[:10]
        git.commit_changes(directory, f"Relay: rewind to {what}\n\nRelay-Task: {row['id']}", only=scope(row))
    except git.GitError as exc:
        raise TaskError(f"Couldn't rewind: {exc}") from exc
    return before
