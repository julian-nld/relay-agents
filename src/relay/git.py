"""Git operations behind code tasks: isolated worktrees, change lists and diffs.

A code task in a git repository can work on its own branch in a separate
worktree, so the agent never touches the checkout you work in. Relay then
shows what changed since the task started and can merge or discard it.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # git's tree without files
MAX_DIFF_BYTES = 400_000  # per file; bigger diffs are cut off
MAX_FILES = 300
TIMEOUT = 60
# Installed and downloaded software, and caches, that a project may forget to
# ignore (gitignore patterns, at any depth). Relay doesn't commit them and a
# task's changes don't count them, unless git tracks them already. Virtual
# environments by another name are recognised by their pyvenv.cfg.
DEPENDENCIES = (
    "node_modules/", "bower_components/", "jspm_packages/", ".venv/", "venv/", "__pycache__/", "*.pyc",
    "*.egg-info/", ".eggs/", "site-packages/", ".tox/", ".nox/", ".pytest_cache/", ".mypy_cache/",
    ".ruff_cache/", ".cache/", ".gradle/", ".next/", ".nuxt/", ".svelte-kit/", ".angular/", ".turbo/",
    ".parcel-cache/", ".dart_tool/", ".bundle/", "**/vendor/bundle/", "Pods/",
)
# A new file bigger than this (a downloaded model, a video) isn't committed or counted
# either: in a git history it would stay forever. One git tracks already goes along.
MAX_NEW_FILE = 50 * 1024 * 1024


class GitError(Exception):
    pass


def run(directory: Path, *args: str, check: bool = True, identity: bool = False,
        env: dict[str, str] | None = None) -> str:
    command = ["git"]
    if identity:
        command += _identity(directory, env)
    command += list(args)
    try:
        # Always UTF-8: with a C locale (common on servers) non-ASCII file
        # names would otherwise come out mangled.
        result = subprocess.run(command, cwd=directory, capture_output=True, text=True,
                                encoding="utf-8", errors="replace",
                                timeout=TIMEOUT, stdin=subprocess.DEVNULL,
                                env={**os.environ, **env} if env else None)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError(f"git {args[0]}: {exc}") from exc
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise GitError(detail[-1] if detail else f"git {args[0]} failed")
    return result.stdout


def _identity(directory: Path, env: dict[str, str] | None = None) -> list[str]:
    """Commit as Relay when the repository has no identity configured."""
    configured = subprocess.run(["git", "config", "user.email"], cwd=directory,
                                capture_output=True, text=True,
                                env={**os.environ, **env} if env else None).stdout.strip()
    return [] if configured else ["-c", "user.name=Relay", "-c", "user.email=relay@localhost"]


def has_identity(directory: Path) -> bool:
    """Whether git knows who commits here (else its commits are Relay's)."""
    return not _identity(directory)


def repo_root(directory: Path) -> Path | None:
    try:
        top = run(directory, "rev-parse", "--show-toplevel").strip()
    except GitError:
        return None
    return Path(top) if top else None


def head(directory: Path, env: dict[str, str] | None = None) -> str | None:
    try:
        return run(directory, "rev-parse", "HEAD", env=env).strip()
    except GitError:
        return None  # a repository without commits


def current_branch(directory: Path) -> str | None:
    name = run(directory, "rev-parse", "--abbrev-ref", "HEAD", check=False).strip()
    return None if not name or name == "HEAD" else name


def clean_in_commit(root: Path, commit: str, relative: str) -> bool:
    """Whether the folder `relative` (to the repository `root`) is in `commit` and has no
    changes or new files since: then a branch from `commit` starts with it as it is."""
    if relative not in ("", "."):
        try:
            run(root, "cat-file", "-e", f"{commit}:{relative}")
        except GitError:
            return False  # not committed yet
    return not run(root, "status", "--porcelain", "--", relative or ".").strip()


def is_clean(directory: Path) -> bool:
    return not run(directory, "status", "--porcelain").strip()


def create_worktree(root: Path, path: Path, branch: str, base: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    run(root, "worktree", "add", "-q", "-b", branch, str(path), base)


def remove_worktree(root: Path, path: Path, branch: str) -> None:
    run(root, "worktree", "remove", "--force", str(path), check=False)
    run(root, "worktree", "prune", check=False)
    run(root, "branch", "-D", branch, check=False)


def untracked(directory: Path, scope: list[str] | None = None, env: dict[str, str] | None = None) -> list[str]:
    """New files (in `scope`, pathspecs): not ignored, not DEPENDENCIES and not over MAX_NEW_FILE."""
    excludes = [f"--exclude={pattern}" for pattern in DEPENDENCIES]
    for found in run(directory, "ls-files", "-z", "--others", "--exclude-standard", *excludes,
                     "--", ":(glob)**/pyvenv.cfg", env=env).split("\0"):
        if "/" in found:  # not the folder itself
            excludes.append(f"--exclude={pattern_for(found.rpartition('/')[0])}/")
    found = run(directory, "ls-files", "-z", "--others", "--exclude-standard", *excludes, "--", *(scope or ["."]),
                env=env).split("\0")
    return [path for path in found if path and _size(directory / path) <= MAX_NEW_FILE
            and not (path.endswith("/") and _no_commit(directory / path))]


def _no_commit(path: Path) -> bool:
    """A folder that's a repository of its own (git lists it as `dir/`) without a commit yet: git can't add it, and
    one such folder made the whole `git add` fail."""
    done = subprocess.run(["git", "-C", str(path), "rev-parse", "-q", "--verify", "HEAD"], capture_output=True,
                          env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")})
    return done.returncode != 0


def _size(path: Path) -> int:
    try:
        return path.lstat().st_size
    except OSError:
        return 0


def changed_files(directory: Path, scope: list[str] | None = None, env: dict[str, str] | None = None) -> list[str]:
    """What `git add -A` (in `scope`) takes: changed and new files, without DEPENDENCIES that aren't tracked yet."""
    changed = [path for path in run(directory, "ls-files", "-z", "--modified", "--deleted", "--",
                                    *(scope or ["."]), env=env).split("\0") if path]
    return [*dict.fromkeys(changed), *untracked(directory, scope, env)]  # a deleted file is listed twice


def add_all(directory: Path, scope: list[str] | None = None, env: dict[str, str] | None = None) -> None:
    """`git add -A` (in `scope`), without DEPENDENCIES that aren't tracked yet."""
    paths = changed_files(directory, scope, env)
    if paths:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".paths") as listed:
            listed.write("\0".join(paths))
            listed.flush()
            run(directory, "add", f"--pathspec-from-file={listed.name}", "--pathspec-file-nul",
                env={**(env or {}), "GIT_LITERAL_PATHSPECS": "1"})


def pattern_for(path: str) -> str:
    """A gitignore pattern that matches just this path."""
    return "/" + "".join("\\" + c if c in "*?[]!#\\" else c for c in path)


def snapshot(directory: Path, message: str) -> str:
    """A commit of the working tree as it is now (new files included, ignored
    ones and DEPENDENCIES not), on top of HEAD, without touching the index, HEAD
    or any branch."""
    with tempfile.TemporaryDirectory() as tmp:
        index = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        run(directory, "read-tree", "HEAD", env=index)
        add_all(directory, env=index)
        tree = run(directory, "write-tree", env=index).strip()
    return run(directory, "commit-tree", tree, "-p", "HEAD", "-m", message, identity=True).strip()


# Relay's own commits: as Relay, without hooks or signing prompts that could
# block an automatic commit.
RELAY_COMMIT = ["-c", "user.name=Relay", "-c", "user.email=relay@localhost", "-c", "commit.gpgsign=false",
                "-c", "core.hooksPath=/dev/null"]  # (--no-verify leaves prepare-commit-msg and post-commit)


def busy(directory: Path) -> str | None:
    """What git is in the middle of (a merge, rebase, …) in `directory`, if anything:
    committing then would take half-done work, conflict markers included."""
    for name, what in (("MERGE_HEAD", "a merge"), ("rebase-merge", "a rebase"), ("rebase-apply", "a rebase"),
                       ("CHERRY_PICK_HEAD", "a cherry-pick"), ("REVERT_HEAD", "a revert"),
                       ("BISECT_LOG", "a bisect")):
        path = run(directory, "rev-parse", "--git-path", name, check=False).strip()
        if path and (directory / path).exists():
            return what
    return None


def commits_since(directory: Path, since: str, only: str | None = None) -> list[tuple[str, str]]:
    """(subject, full message) of the commits after `since` (touching `only`)."""
    output = run(directory, "log", "--format=%H%x1f%s%x1f%B%x1e", f"{since}..HEAD", "--", only or ".")
    found = []
    for record in output.split("\x1e"):
        parts = record.strip("\n").split("\x1f")
        if len(parts) == 3:
            found.append((parts[1], parts[2]))
    return found


def commit_changes(directory: Path, message: str, only: str | None = None, as_relay: bool = True) -> str | None:
    """Commit what changed in `directory` (or its subfolder `only`); the new
    commit, or None when nothing changed."""
    scope = ["--", only or "."]
    add_all(directory, [only or "."])
    if not run(directory, "diff", "--cached", "--name-only", *scope).strip():
        return None
    if as_relay:
        run(directory, *RELAY_COMMIT, "commit", "-q", "--no-verify", "-m", message, *scope)
    else:
        run(directory, "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "commit", "-q",
            "--no-verify", "-m", message, *scope, identity=True)
    return head(directory)


def restore(directory: Path, source: str, only: str | None = None) -> None:
    """Make the files (in subfolder `only`) what they were at commit `source`:
    changed files back, added ones removed. Ignored files are left alone."""
    run(directory, "restore", f"--source={source}", "--staged", "--worktree", "--", only or ".")


def merge(root: Path, branch: str, into: str, message: str) -> None:
    """Squash `branch` into one commit on `into`, which must be checked out and
    clean in `root`."""
    checked_out = current_branch(root)
    if checked_out != into:
        raise GitError(f"{root} has {checked_out or 'a detached HEAD'} checked out; "
                       f"switch to {into} to merge")
    if not is_clean(root):
        raise GitError(f"{root} has uncommitted changes; commit or stash them first")
    try:
        run(root, "merge", "--squash", branch)
        if not run(root, "diff", "--cached", "--name-only").strip():
            raise GitError(f"{into} has everything from {branch} already")
        run(root, "-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m", message, identity=True)
    except GitError as exc:
        run(root, "reset", "-q", "--hard", "HEAD", check=False)  # it was clean before
        raise GitError(f"Merge failed, nothing was changed: {exc}") from exc


@dataclass
class FileChange:
    path: str
    status: str  # added | modified | deleted | renamed
    additions: int = 0
    deletions: int = 0
    binary: bool = False
    diff: str = ""


@dataclass
class Changes:
    files: list[FileChange] = field(default_factory=list)
    truncated: bool = False

    @property
    def additions(self) -> int:
        return sum(f.additions for f in self.files)

    @property
    def deletions(self) -> int:
        return sum(f.deletions for f in self.files)

    def summary(self) -> dict:
        return {"files": len(self.files), "additions": self.additions, "deletions": self.deletions}


STATUS = {"A": "added", "M": "modified", "D": "deleted", "R": "renamed", "C": "added", "T": "modified"}


def changes(directory: Path, base: str | None, with_diff: bool = True,
            env: dict[str, str] | None = None, only: str | None = None) -> Changes:
    """What differs in `directory`'s working tree from commit `base`
    (HEAD when None), untracked files included. `env` can point git at
    another repository (GIT_DIR) that tracks the directory; `only` limits it
    to a subfolder (relative to `directory`)."""
    base = base or head(directory, env)
    scope = ["--", only] if only else []
    result = Changes()
    if base:
        # -z: NUL-separated and unquoted, so any file name comes through as is.
        counts = _numstat(run(directory, "diff", "--numstat", "-z", "-M", base, *scope, env=env))
        for code, path in _name_status(run(directory, "diff", "--name-status", "-z", "-M", base,
                                           *scope, env=env)):
            added, deleted, binary = counts.get(path, (0, 0, False))
            result.files.append(FileChange(path, STATUS.get(code, "modified"), added, deleted, binary))
    tracked = {f.path for f in result.files}
    new = sorted(path for path in untracked(directory, [only] if only else None, env) if path not in tracked)
    # Count only what will be shown: an unignored node_modules has tens of thousands.
    room = max(0, MAX_FILES - len(result.files))
    if len(new) > room:
        new, result.truncated = new[:room], True
    for path in new:
        result.files.append(FileChange(path, "added", *_count_new(directory / path)))
    result.files.sort(key=lambda f: f.path)
    if len(result.files) > MAX_FILES:
        result.files, result.truncated = result.files[:MAX_FILES], True
    if with_diff:
        for change in result.files:
            change.diff = _diff(directory, base if change.path in tracked else None, change, env)
    return result


def _name_status(output: str) -> list[tuple[str, str]]:
    """(status letter, path) from `diff --name-status -z`; renames give the new path."""
    tokens = output.split("\0")
    found, i = [], 0
    while i < len(tokens) and tokens[i]:
        code = tokens[i][:1]
        if code in ("R", "C"):
            found.append((code, tokens[i + 2]))  # status, old path, new path
            i += 3
        else:
            found.append((code, tokens[i + 1]))
            i += 2
    return found


def _numstat(output: str) -> dict[str, tuple[int, int, bool]]:
    """path -> (added, deleted, binary) from `diff --numstat -z`."""
    counts = {}
    tokens = output.split("\0")
    i = 0
    while i < len(tokens) and tokens[i]:
        added, deleted, path = tokens[i].split("\t", 2)
        if path:
            i += 1
        else:  # a rename: the old and new paths follow as separate fields
            path = tokens[i + 2]
            i += 3
        binary = added == "-"
        counts[path] = (0 if binary else int(added), 0 if binary else int(deleted), binary)
    return counts


def _count_new(path: Path) -> tuple[int, int, bool]:
    try:
        with open(path, "rb") as fh:  # at most the start of it: a new file may be huge
            data = fh.read(MAX_DIFF_BYTES)
    except OSError:
        return 0, 0, False
    if b"\0" in data:
        return 0, 0, True
    return data.count(b"\n") + (0 if data.endswith(b"\n") or not data else 1), 0, False


def _diff(directory: Path, base: str | None, change: FileChange,
          env: dict[str, str] | None = None) -> str:
    """The file's diff against `base`; None for files git doesn't track yet."""
    if change.binary:
        return ""
    if base:
        text = run(directory, "diff", "-M", base, "--", change.path, check=False, env=env)
    else:
        try:
            size = (directory / change.path).stat().st_size
        except OSError:
            size = 0
        if size > MAX_DIFF_BYTES:  # a new file that big: its start is enough
            with open(directory / change.path, "rb") as fh:
                head_text = fh.read(MAX_DIFF_BYTES).decode("utf-8", "replace")
            return "".join(f"+{line}\n" for line in head_text.splitlines()) + "\n… file cut off …\n"
        text = run(directory, "diff", "--no-index", "--", "/dev/null", change.path, check=False)
    if len(text) > MAX_DIFF_BYTES:
        text = text[:MAX_DIFF_BYTES] + "\n… diff cut off …\n"
    return text
