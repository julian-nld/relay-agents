import subprocess

from relay import checkpoints


def test_checkpoints_restore_files_without_touching_the_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_HOME", str(tmp_path / "data"))
    folder = tmp_path / "project"
    (folder / "node_modules").mkdir(parents=True)
    (folder / "main.py").write_text("print(1)\n")
    (folder / "node_modules" / "big.js").write_text("x")
    start = checkpoints.take("c1", folder, "start")
    assert start and not (folder / ".git").exists()
    assert checkpoints.take("c1", folder, "again") == start  # nothing changed

    (folder / "main.py").write_text("print(2)\n")
    (folder / "new.txt").write_text("new\n")
    (folder / "node_modules" / "big.js").write_text("changed")
    changes = checkpoints.changes("c1", folder, start)
    assert {f.path: f.status for f in changes.files} == {"main.py": "modified", "new.txt": "added"}
    assert "+print(2)" in changes.files[0].diff

    undo = checkpoints.restore("c1", folder, start)
    assert (folder / "main.py").read_text() == "print(1)\n" and not (folder / "new.txt").exists()
    assert (folder / "node_modules" / "big.js").read_text() == "changed"  # skipped folders stay
    checkpoints.restore("c1", folder, undo)  # and back again
    assert (folder / "main.py").read_text() == "print(2)\n" and (folder / "new.txt").exists()


def test_checkpoints_leave_a_repository_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_HOME", str(tmp_path / "data"))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "a.txt").write_text("a\n")
    (repo / ".gitignore").write_text("secret.txt\n")
    (repo / "secret.txt").write_text("s\n")
    first = checkpoints.take("c2", repo, "start")
    (repo / "a.txt").write_text("b\n")
    checkpoints.restore("c2", repo, first)
    assert (repo / "a.txt").read_text() == "a\n" and (repo / "secret.txt").exists()
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout
    assert status.split() == ["??", ".gitignore", "??", "a.txt"]  # its index and history untouched


def test_big_folders_get_no_checkpoints(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(checkpoints, "MAX_FILES", 3)
    folder = tmp_path / "big"
    folder.mkdir()
    for i in range(5):
        (folder / f"{i}.txt").write_text("x")
    assert checkpoints.take("c3", folder, "start") is None
    assert checkpoints.take("c3", folder, "again") is None


def test_huge_files_are_left_out_of_checkpoints(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(checkpoints, "MAX_FILE_BYTES", 100)
    folder = tmp_path / "project"
    folder.mkdir()
    (folder / "main.py").write_text("print(1)\n")
    (folder / "video [1].mp4").write_bytes(b"x" * 1000)
    start = checkpoints.take("c1", folder, "start")
    kept = subprocess.run(["git", "--git-dir", str(checkpoints.git_dir("c1")), "ls-tree", "-r", "--name-only", start],
                          capture_output=True, text=True).stdout.split()
    assert kept == ["main.py"]
    assert (folder / "video [1].mp4").exists()  # left alone, just not kept


def test_a_new_repository_in_the_folder_leaves_checkpoints_working(tmp_path, monkeypatch):
    """A folder with a repository of its own in it that has no commit yet (git init in a subproject): git can't add
    it, which made every checkpoint fail. It's left out; one with commits is kept as git keeps it."""
    monkeypatch.setenv("RELAY_HOME", str(tmp_path / "data"))
    folder = tmp_path / "project"
    (folder / "fresh").mkdir(parents=True)
    (folder / "main.py").write_text("print(1)\n")
    subprocess.run(["git", "init", "-q"], cwd=folder / "fresh", check=True)
    (folder / "fresh" / "in.txt").write_text("in\n")
    start = checkpoints.take("c3", folder, "start")
    assert start
    (folder / "main.py").write_text("print(2)\n")
    assert [f.path for f in checkpoints.changes("c3", folder, start).files] == ["main.py"]
    checkpoints.restore("c3", folder, start)
    assert (folder / "main.py").read_text() == "print(1)\n" and (folder / "fresh" / "in.txt").read_text() == "in\n"
