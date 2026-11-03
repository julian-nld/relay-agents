import subprocess
from pathlib import Path

import pytest

from relay import git


def sh(directory: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=directory, check=True, capture_output=True,
                          text=True).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    sh(root, "init", "-q", "-b", "main")
    sh(root, "config", "user.email", "me@example.com")
    sh(root, "config", "user.name", "Me")
    (root / "keep.txt").write_text("one\ntwo\n")
    (root / "gone.txt").write_text("bye\n")
    sh(root, "add", "-A")
    sh(root, "commit", "-q", "-m", "start")
    return root


def test_changes_include_edits_deletions_and_new_files(repo):
    (repo / "keep.txt").write_text("one\nTWO\nthree\n")
    (repo / "gone.txt").unlink()
    (repo / "new.py").write_text("print(1)\nprint(2)\n")
    (repo / "blob.bin").write_bytes(b"\0\1\2")
    result = git.changes(repo, None)
    files = {f.path: f for f in result.files}
    assert files["keep.txt"].status == "modified"
    assert (files["keep.txt"].additions, files["keep.txt"].deletions) == (2, 1)
    assert "+TWO" in files["keep.txt"].diff
    assert files["gone.txt"].status == "deleted" and files["gone.txt"].deletions == 1
    assert files["new.py"].status == "added" and files["new.py"].additions == 2
    assert "+print(2)" in files["new.py"].diff
    assert files["blob.bin"].binary and files["blob.bin"].diff == ""
    assert result.summary() == {"files": 4, "additions": 4, "deletions": 2}


def test_ignored_files_are_left_out(repo):
    (repo / ".gitignore").write_text("*.log\n")
    (repo / "debug.log").write_text("noise\n")
    assert [f.path for f in git.changes(repo, None).files] == [".gitignore"]


def test_worktree_task_merge_and_discard(repo, tmp_path):
    base = git.head(repo)
    task = tmp_path / "worktrees" / "t1"
    git.create_worktree(repo, task, "relay/t1", base)
    (task / "feature.py").write_text("x = 1\n")
    assert [f.path for f in git.changes(task, base).files] == ["feature.py"]
    assert git.is_clean(repo)  # the user's checkout is untouched

    assert git.commit_changes(task, "Add feature")
    assert git.commit_changes(task, "Nothing") is None
    (task / "feature.py").write_text("x = 2\n")
    git.commit_changes(task, "Change feature")
    # Committed work still counts as a change of the task:
    assert [f.path for f in git.changes(task, base).files] == ["feature.py"]
    assert sh(task, "log", "-1", "--format=%an").strip() == "Relay"

    git.merge(repo, "relay/t1", "main", "Feature (Relay task)")
    assert (repo / "feature.py").read_text() == "x = 2\n"
    # Squashed: one commit on main for all of the task's turns
    assert sh(repo, "log", "--format=%s").split("\n")[:2] == ["Feature (Relay task)", "start"]
    git.remove_worktree(repo, task, "relay/t1")
    assert not task.exists()
    assert "relay/t1" not in sh(repo, "branch")


def test_merge_refuses_a_dirty_or_wrong_checkout(repo, tmp_path):
    task = tmp_path / "wt"
    git.create_worktree(repo, task, "relay/t2", git.head(repo))
    (task / "a.txt").write_text("a\n")
    git.commit_changes(task, "a")
    (repo / "keep.txt").write_text("local edit\n")
    with pytest.raises(git.GitError, match="uncommitted changes"):
        git.merge(repo, "relay/t2", "main", "m")
    sh(repo, "checkout", "-q", "--", "keep.txt")
    sh(repo, "checkout", "-q", "-b", "other")
    with pytest.raises(git.GitError, match="switch to main"):
        git.merge(repo, "relay/t2", "main", "m")


def test_conflicting_merge_is_aborted(repo, tmp_path):
    task = tmp_path / "wt"
    git.create_worktree(repo, task, "relay/t3", git.head(repo))
    (task / "keep.txt").write_text("task version\n")
    git.commit_changes(task, "task edit")
    (repo / "keep.txt").write_text("main version\n")
    sh(repo, "commit", "-q", "-am", "main edit")
    with pytest.raises(git.GitError, match="nothing was changed"):
        git.merge(repo, "relay/t3", "main", "m")
    assert git.is_clean(repo) and (repo / "keep.txt").read_text() == "main version\n"


def test_restore_brings_files_back_as_a_new_commit(repo):
    before = git.head(repo)
    (repo / "keep.txt").write_text("changed\n")
    (repo / "added.txt").write_text("new\n")
    (repo / "sub").mkdir()
    (repo / "sub" / "x.txt").write_text("x\n")
    git.commit_changes(repo, "turn")
    git.restore(repo, before, only=None)
    assert (repo / "keep.txt").read_text() == "one\ntwo\n" and not (repo / "added.txt").exists()
    assert not (repo / "sub" / "x.txt").exists()
    git.commit_changes(repo, "rewind")
    assert sh(repo, "log", "--format=%s").split("\n")[:3] == ["rewind", "turn", "start"]  # nothing rewritten


def test_renames_are_reported_under_their_new_name(repo):
    sh(repo, "mv", "keep.txt", "kept.txt")
    files = git.changes(repo, None).files
    assert [(f.path, f.status) for f in files] == [("kept.txt", "renamed")]


def test_old_databases_get_new_columns(tmp_path):
    import sqlite3

    from relay.db import SCHEMA, Database
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(SCHEMA)  # the original tables, without the git columns
    old.close()
    db = Database(path)
    columns = {r["name"] for r in db.all("PRAGMA table_info(conversations)")}
    assert {"git_mode", "branch", "task_state"} <= columns
    Database(path)  # running the migration twice is harmless


def test_unusual_file_names_come_through_intact(repo):
    for name in ["café.txt", "with space.md", "tab\tname.txt", 'quote".txt']:
        (repo / name).write_text("x\n")
    (repo / "keep.txt").write_text("one\ntwo\nthree\n")
    sh(repo, "add", "keep.txt")
    sh(repo, "mv", "gone.txt", "naïve.txt")
    files = {f.path: f for f in git.changes(repo, None).files}
    assert files["café.txt"].additions == 1 and "+x" in files["café.txt"].diff
    assert files["with space.md"].status == "added"
    assert "tab\tname.txt" in files and 'quote".txt' in files
    assert files["naïve.txt"].status == "renamed"
    assert files["keep.txt"].additions == 1


def test_many_or_huge_new_files_are_cut_short(repo, monkeypatch):
    monkeypatch.setattr(git, "MAX_FILES", 5)
    monkeypatch.setattr(git, "MAX_DIFF_BYTES", 50)
    for n in range(20):
        (repo / f"new{n:02}.txt").write_text("line\n" * 100)
    changes = git.changes(repo, None)
    assert len(changes.files) == 5 and changes.truncated
    assert changes.files[0].diff.endswith("… file cut off …\n") and changes.files[0].diff.startswith("+line")


def test_installed_software_is_neither_counted_nor_committed(repo):
    for path in ["node_modules/left-pad/index.js", "web/node_modules/x/y.js", ".venv/lib/site.py",
                 "tools/myenv/pyvenv.cfg", "tools/myenv/lib/big.py", "app/__pycache__/m.pyc"]:
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text("x\n" * 1000)
    (repo / "app" / "main.py").write_text("print(1)\n")
    (repo / "we*ird[1].py").write_text("x\n")
    (repo / "keep.txt").write_text("changed\n")
    assert [f.path for f in git.changes(repo, None).files] == ["app/main.py", "keep.txt", "we*ird[1].py"]
    start = git.head(repo)
    git.commit_changes(repo, "turn")
    assert sorted(sh(repo, "diff", "--name-only", start, "HEAD").split()) == ["app/main.py", "keep.txt",
                                                                            "we*ird[1].py"]
    assert git.changes(repo, start).summary()["files"] == 3
    # Tracked already: then it's the project's own, and it goes along as usual.
    sh(repo, "add", "-f", "node_modules/left-pad/index.js")
    sh(repo, "commit", "-q", "-m", "vendored")
    (repo / "node_modules/left-pad/index.js").write_text("patched\n")
    (repo / "gone.txt").unlink()
    git.commit_changes(repo, "turn 2")
    assert sorted(sh(repo, "show", "--name-only", "--format=", "HEAD").split()) == [
        "gone.txt", "node_modules/left-pad/index.js"]
    # A fork's snapshot leaves them out too.
    (repo / "node_modules/new/pkg.js").parent.mkdir(parents=True)
    (repo / "node_modules/new/pkg.js").write_text("x\n")
    (repo / "fresh.py").write_text("x\n")
    tree = sh(repo, "ls-tree", "-r", "--name-only", git.snapshot(repo, "fork")).split()
    assert "fresh.py" in tree and "node_modules/new/pkg.js" not in tree and ".venv/lib/site.py" not in tree


def test_big_new_files_are_neither_committed_nor_counted(repo, monkeypatch):
    monkeypatch.setattr(git, "MAX_NEW_FILE", 1000)
    (repo / "model.bin").write_bytes(b"x" * 5000)
    (repo / "small.txt").write_text("x\n")
    assert [f.path for f in git.changes(repo, None).files] == ["small.txt"]
    git.commit_changes(repo, "turn")
    assert sh(repo, "show", "--name-only", "--format=", "HEAD").split() == ["small.txt"]
    assert (repo / "model.bin").exists()  # left alone


def test_a_task_in_a_repository_without_commits_is_followed_with_checkpoints(tmp_path, monkeypatch):
    from relay import checkpoints, config, tasks
    monkeypatch.setattr(config, "data_dir", lambda: tmp_path / "data")
    root = tmp_path / "empty"
    root.mkdir()
    sh(root, "init", "-q", "-b", "main")
    setup = tasks.setup("c1", root, False)
    assert setup.git_mode == "direct" and setup.base_commit is None
    row = {"id": "c1", "git_mode": "direct", "repo_root": str(root), "directory": str(root),
           "base_commit": checkpoints.take("c1", root, "Start of the task"), "task_state": "open", "worktree": None,
           "title": "t"}
    before = tasks.before_run(row, "write")
    (root / "new.py").write_text("print(1)\n")  # the agent's first turn
    after = tasks.after_run(row, "write")
    assert [f.path for f in tasks.changes(row, with_diff=False).files] == ["new.py"]
    assert "+print(1)" in tasks.turn_diff(row, before, after)
    assert not (root / ".git" / "refs" / "heads" / "main").exists()  # still no commits: none of Relay's

