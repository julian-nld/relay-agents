"""A subagent's own copy of its caller's folder, merged back when it's done.

When an agent in a code task hands work to a subagent that may change files, the subagent
works in a copy: the caller's files as a checkpoint (relay.checkpoints) put in a folder of
its own. Installed software (node_modules, a virtual environment, …) isn't in a checkpoint;
the copy links to the caller's, and the copy's container gets those folders too.

When the subagent is done, what it changed goes into the caller's folder with a three-way
merge (where they both started, the caller's files now, the subagent's), touching only
the files it changed. When that conflicts with what changed meanwhile, or the subagent
failed or was stopped, nothing is applied: the caller decides (`merge` applies it anyway,
with conflict markers where needed).

Everything goes into the caller's checkpoint repository, under refs/forks/<subagent>/, with
index files of the copy's own: the caller's checkpoints (HEAD, its index) are left alone.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

from relay import checkpoints, config, git

# Installed software a copy links to (rather than copying it: big, and a virtual environment
# doesn't work from another place), at any depth up to LINK_DEPTH.
LINKED = ("node_modules", "bower_components", "jspm_packages", ".venv", "venv", ".bundle", "Pods")
LINK_DEPTH = 4
SKIPPED = (".git", ".hg", ".svn", *git.DEPENDENCIES)  # not looked into for more of them
MAX_DIFF = 20_000
# Small ignored files (a .env, local settings) the copy gets too, so the subagent's tests see what the
# caller's see; ignored folders (build output, caches) don't come along. They don't go back either.
IGNORED_MAX_BYTES = 1024 * 1024
IGNORED_MAX_FILES = 200
_labelling = threading.Lock()


class ForkError(Exception):
    pass


@dataclass
class Fork:
    """A subagent's copy of its caller's folder."""

    caller: str  # the caller's conversation: its checkpoint repository holds everything
    subagent: str
    source: Path  # the caller's folder
    folder: Path  # the copy
    links: list[str] = field(default_factory=list)  # relative paths linked to the caller's folder

    @property
    def root(self) -> Path:
        return self.folder.parent

    def ref(self, name: str) -> str:
        return f"refs/forks/{self.subagent}/{name}"


@dataclass
class Merge:
    """What merging a copy's changes did: "nothing" (it changed nothing), "merged", or
    "conflict" (nothing applied)."""

    status: str
    files: list[str] = field(default_factory=list)  # what it changed in the caller's folder
    conflicts: list[str] = field(default_factory=list)
    diff: str = ""  # the subagent's changes, when they weren't applied

    def describe(self, short: str) -> str:
        """For the caller: what happened to its subagent's changes."""
        if self.status == "nothing":
            return "It changed no files."
        if self.status == "merged":
            text = (f"Its changes are in your folder now ({_files(self.files)})." if self.files
                    else "Its changes were in your folder already.")
            if self.conflicts:
                text += (f" They conflicted with yours in {_files(self.conflicts)}: those files have conflict "
                         "markers (<<<<<<< yours … ======= … >>>>>>> subagent) for you to sort out.")
            return text
        return (f"Its changes conflict with what changed in your folder meanwhile ({_files(self.conflicts)}), "
                f"so they weren't applied. What it changed:\n\n{self.diff}\n\n"
                f"You decide: `merge {short}` applies them with conflict markers in those files for you to sort "
                f"out; or tell {short} gives it your current files (conflicts marked) to sort it out; or take what "
                "you need from the diff yourself.")


def _files(names: list[str]) -> str:
    shown = ", ".join(names[:12])
    return shown + (f" and {len(names) - 12} more" if len(names) > 12 else "")


def folders() -> Path:
    return config.data_dir() / "forks"


def make(caller: str, source: Path, subagent: str) -> Fork:
    """Copy the caller's folder for a subagent (its files as they are now). Whatever goes
    wrong, nothing half-made stays behind: ForkError says why."""
    source = source.resolve()
    if not source.is_dir():
        raise ForkError("the folder is gone")
    fork = Fork(caller, subagent, source, folders() / subagent / (source.name or "work"))
    try:
        fork.root.mkdir(parents=True, exist_ok=True)
        base = checkpoints.record(caller, source, f"Before subagent {subagent[:8]}", fork.ref("base"),
                                  fork.root / "caller.index")
        if base is None:
            raise ForkError("Relay can't copy this folder (too many files, or git is missing)")
        fork.folder.mkdir(parents=True)
        env = _env(fork, fork.folder, "copy.index")
        git.run(fork.folder, "read-tree", base, env=env)
        git.run(fork.folder, "checkout-index", "-a", "-f", "-u", env=env)  # -u: the index knows the files
        git.run(fork.folder, "update-ref", fork.ref("copy"), base, env=env)
        fork.links = _link(fork)
        _copy_ignored(fork)
        _save(fork)
    except BaseException as exc:
        remove(fork)
        if isinstance(exc, ForkError) or not isinstance(exc, Exception):
            raise
        raise ForkError(f"Couldn't copy the folder: {exc}") from exc
    return fork


def _copy_ignored(fork: Fork) -> None:
    """Copy the caller's small ignored files (not ignored folders) into the copy."""
    env = {"GIT_DIR": str(checkpoints.git_dir(fork.caller)), "GIT_WORK_TREE": str(fork.source)}
    listed = git.run(fork.source, "ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory",
                     env=env)
    copied = 0
    for relative in filter(None, listed.split("\0")):
        if relative.endswith("/") or copied >= IGNORED_MAX_FILES:
            continue  # a whole ignored folder: build output, caches, installed software
        source, target = fork.source / relative, fork.folder / relative
        try:
            if source.is_symlink() or not source.is_file() or source.stat().st_size > IGNORED_MAX_BYTES:
                continue
            if target.exists() or target.is_symlink() or _through_link(fork.folder, target):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied += 1
        except OSError:
            continue


def _through_link(folder: Path, path: Path) -> bool:
    """Whether `path`, in the copy, is inside one of its links (to the caller's installed software)."""
    return any((folder / parent).is_symlink() for parent in path.relative_to(folder).parents if str(parent) != ".")


def _save(fork: Fork) -> None:
    """What a copy is, next to it: it outlives its caller's run when it has changes not handed over."""
    data = {"caller": fork.caller, "subagent": fork.subagent, "source": str(fork.source),
            "folder": str(fork.folder), "links": fork.links}
    _write(fork.root / "fork.json", data)


def _write(file: Path, data: dict) -> None:
    """Whole or not at all: a half-written fork.json would make its copy unknown."""
    temporary = file.with_name(f".{file.name}.tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(temporary, file)


def load(subagent: str) -> Fork | None:
    """The subagent's copy, if it has one."""
    try:
        data = json.loads((folders() / subagent / "fork.json").read_text())
        # The copy is where its fork.json is (Relay's data may have moved since); the caller's
        # folder is rewritten by moved() when Relay moved.
        return Fork(data["caller"], data["subagent"], Path(data["source"]),
                    folders() / subagent / Path(data["folder"]).name, list(data.get("links") or []))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def changed(fork: Fork) -> list[str]:
    """The files the subagent changed that its caller's folder hasn't got yet."""
    base = _resolve(fork, "base")
    theirs = _record(fork, fork.folder, "copy", "Subagent's files")
    return _names(fork, base, theirs)


def tidy(known, containers=None) -> list[str]:
    """At Relay's start: copies whose conversation is gone, or that have nothing left to hand
    over, go; the others stay (their changes wait for the user) and their containers get the
    linked folders again. `known(subagent)` says whether that conversation still exists."""
    kept = []
    if not folders().is_dir():
        return kept
    for root in folders().iterdir():
        fork = load(root.name)
        try:
            keep = fork is not None and known(fork.subagent) and fork.folder.is_dir() and bool(changed(fork))
        except (ForkError, git.GitError) as exc:  # can't tell (a full disk?): it may hold work, so it stays
            print(f"relay: kept the subagent's copy {root.name}: {exc}", file=sys.stderr, flush=True)
            keep = True
        if keep:
            if containers is not None and fork is not None and fork.links:
                containers.mount_also(fork.folder, [fork.source / link for link in fork.links])
            kept.append(root.name)
            continue
        if fork is not None:
            remove(fork, containers)
        elif (root / "fork.json").exists():  # there, but unreadable: it may hold work
            print(f"relay: kept the subagent's copy {root.name}: its fork.json can't be read", file=sys.stderr,
                  flush=True)
        else:
            shutil.rmtree(root, ignore_errors=True)  # never finished (make() writes fork.json last)
    return kept


def moved(old: str, new: str) -> None:
    """Relay moved (relay.relocation): the copies' callers' folders that were under `old` are
    under `new` now."""
    if not folders().is_dir():
        return
    for file in folders().glob("*/fork.json"):
        try:
            data = json.loads(file.read_text())
            source = data["source"]
            if source == old or source.startswith(old + "/"):
                data["source"] = new + source[len(old):]
                _write(file, data)
                folder = file.parent / Path(data["folder"]).name
                for link in data.get("links") or []:  # the links to its installed software, too
                    path = folder / link
                    if path.is_symlink():
                        path.unlink()
                        path.symlink_to(Path(data["source"]) / link)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue


def _link(fork: Fork) -> list[str]:
    """Link the caller's installed software into the copy; the relative paths linked."""
    linked = []
    for top, dirs, _ in os.walk(fork.source):
        here = Path(top)
        depth = len(here.relative_to(fork.source).parts)
        for name in list(dirs):
            path = here / name
            if name in LINKED or (path / "pyvenv.cfg").is_file():
                relative = path.relative_to(fork.source)
                target = fork.folder / relative
                if not target.exists() and not target.is_symlink():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.symlink_to(path)
                    linked.append(relative.as_posix())
                dirs.remove(name)
            elif name in SKIPPED or f"{name}/" in SKIPPED or depth + 1 >= LINK_DEPTH:
                dirs.remove(name)
    if linked:
        # The links aren't the subagent's work: left out of its changes.
        exclude = checkpoints.git_dir(fork.caller) / "info" / "exclude"
        known = exclude.read_text() if exclude.exists() else ""
        new = [git.pattern_for(relative) for relative in linked]
        new = [pattern for pattern in new if pattern not in known.splitlines()]
        if new:
            exclude.write_text(known + "# a subagent's links to installed software\n" + "\n".join(new) + "\n")
    return linked


def merge(fork: Fork, markers: bool = False) -> Merge:
    """Put what the subagent changed into the caller's folder.

    On a conflict nothing is applied, unless `markers`: then the conflicting files get
    conflict markers. Afterwards the subagent's changes so far count as merged."""
    for attempt in (1, 2):
        base = _resolve(fork, "base")
        theirs = _record(fork, fork.folder, "copy", "Subagent's files")
        if _tree(fork, theirs) == _tree(fork, base):
            return Merge("nothing")
        ours = _record(fork, fork.source, "caller", "Caller's files")
        tree, conflicts = _merge_tree(fork, base, ours, theirs)
        if conflicts and not markers:
            return Merge("conflict", conflicts=conflicts, diff=pending(fork))
        files = _names(fork, ours, tree)
        try:
            # Only the files the merge changes; one the caller changed after `ours` stops it.
            git.run(fork.source, "read-tree", "-m", "-u", ours, tree, env=_env(fork, fork.source, "caller.index"))
        except git.GitError as exc:
            if attempt == 1:
                continue  # the caller's files changed just now: once more, from how they are now
            raise ForkError(f"Couldn't put the changes in your folder: {exc}") from exc
        _set(fork, "base", theirs)
        return Merge("merged", files, conflicts)
    raise AssertionError("unreachable")


def pending(fork: Fork) -> str:
    """The subagent's changes not merged yet: a summary and a unified diff ("" for none)."""
    base = _resolve(fork, "base")
    theirs = _record(fork, fork.folder, "copy", "Subagent's files")
    if _tree(fork, theirs) == _tree(fork, base):
        return ""
    stat = _git(fork, "diff", "--stat", "--find-renames", base, theirs)
    text = _git(fork, "diff", "--find-renames", base, theirs)
    if len(text) > MAX_DIFF:
        text = text[:MAX_DIFF] + f"\n[… {len(text) - MAX_DIFF} more characters of diff]"
    return (stat + "\n" + text).strip()


def refresh(fork: Fork) -> list[str] | None:
    """Bring the caller's files as they are now into the copy, next to what the subagent changed
    and didn't hand over yet (before it goes on with a message). The files that conflicted (they
    have markers now); None when the caller's files hadn't changed."""
    base = _resolve(fork, "base")
    ours = _record(fork, fork.source, "caller", "Caller's files")
    if _tree(fork, ours) == _tree(fork, base):
        return None
    theirs = _record(fork, fork.folder, "copy", "Subagent's files")
    tree, conflicts = _merge_tree(fork, base, theirs, ours, ("yours", "caller"))
    git.run(fork.folder, "read-tree", "-m", "-u", theirs, tree, env=_env(fork, fork.folder, "copy.index"))
    _set(fork, "base", ours)
    return conflicts


def remove(fork: Fork, containers=None) -> None:
    """The copy is done with: its folder, container and refs go."""
    if containers is not None:
        try:
            containers.discard(fork.folder)
        except Exception as exc:  # noqa: BLE001 - the folder and refs go anyway
            print(f"relay: couldn't remove the container of {fork.folder}: {exc}", file=sys.stderr, flush=True)
    shutil.rmtree(fork.root, ignore_errors=True)
    if fork.root.exists():  # files the container made that the user can't remove (rootless podman)
        subprocess.run(["podman", "unshare", "rm", "-rf", str(fork.root)], capture_output=True, timeout=120)
    for name in ("base", "copy", "caller"):
        _git(fork, "update-ref", "-d", fork.ref(name), check=False)


# git, in the caller's checkpoint repository

def _env(fork: Fork, work_tree: Path, index: str) -> dict[str, str]:
    return {"GIT_DIR": str(checkpoints.git_dir(fork.caller)), "GIT_WORK_TREE": str(work_tree),
            "GIT_INDEX_FILE": str(fork.root / index)}


def _git(fork: Fork, *args: str, check: bool = True) -> str:
    return git.run(Path.home(), *args, env={"GIT_DIR": str(checkpoints.git_dir(fork.caller))}, check=check)


def _record(fork: Fork, folder: Path, name: str, label: str) -> str:
    point = checkpoints.record(fork.caller, folder, label, fork.ref(name), fork.root / f"{name}.index")
    if point is None:
        raise ForkError("Relay couldn't read the folder's files")
    return point


def _resolve(fork: Fork, name: str) -> str:
    return _git(fork, "rev-parse", fork.ref(name)).strip()


def _set(fork: Fork, name: str, point: str) -> None:
    _git(fork, "update-ref", fork.ref(name), point)


def _tree(fork: Fork, point: str) -> str:
    return _git(fork, "rev-parse", f"{point}^{{tree}}").strip()


def _merge_tree(fork: Fork, base: str, ours: str, theirs: str,
                labels: tuple[str, str] = ("yours", "subagent")) -> tuple[str, list[str]]:
    """Merge `theirs` into `ours` (both from `base`) without touching any files: the resulting
    tree (with conflict markers where they conflict) and the conflicting files."""
    # Conflict markers are labelled with the names merge-tree is given: branches by these names.
    with _labelling:
        for label, point in zip(labels, (ours, theirs), strict=True):
            _git(fork, "update-ref", f"refs/heads/{label}", point)
        try:
            out = _git(fork, "merge-tree", "--write-tree", "--name-only", "--no-messages",
                       f"--merge-base={base}", *labels, check=False)
        finally:
            for label in labels:
                _git(fork, "update-ref", "-d", f"refs/heads/{label}", check=False)
    lines = out.splitlines()
    if not lines:
        raise ForkError("Couldn't merge the changes")
    return lines[0].strip(), [line for line in lines[1:] if line.strip()]


def _names(fork: Fork, before: str, after: str) -> list[str]:
    return [n for n in _git(fork, "diff", "--name-only", "--no-renames", before, after).splitlines() if n]
