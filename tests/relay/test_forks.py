import pytest

from relay import checkpoints, forks


def project(tmp_path):
    folder = tmp_path / "ws" / "app"
    (folder / "src").mkdir(parents=True)
    (folder / "src" / "main.py").write_text("print(1)\n")
    (folder / "notes.txt").write_text("a\nb\nc\n")
    (folder / "node_modules" / "left-pad").mkdir(parents=True)
    (folder / "node_modules" / "left-pad" / "index.js").write_text("module.exports = 1\n")
    (folder / "web" / ".venv").mkdir(parents=True)
    (folder / "web" / ".venv" / "pyvenv.cfg").write_text("home = /usr/bin\n")
    return folder


def test_a_copy_has_the_files_and_links_installed_software(tmp_path):
    folder = project(tmp_path)
    fork = forks.make("caller", folder, "sub1")
    assert fork.folder.name == "app" and fork.folder != folder
    assert (fork.folder / "src" / "main.py").read_text() == "print(1)\n"
    assert (fork.folder / "node_modules").resolve() == folder / "node_modules"
    assert (fork.folder / "web" / ".venv").resolve() == folder / "web" / ".venv"
    assert sorted(fork.links) == ["node_modules", "web/.venv"]
    assert forks.pending(fork) == ""  # the links aren't changes
    assert forks.merge(fork).status == "nothing"


def test_the_subagents_changes_are_merged_next_to_the_callers(tmp_path):
    folder = project(tmp_path)
    fork = forks.make("caller", folder, "sub1")
    (fork.folder / "src" / "main.py").write_text("print(2)\n")
    (fork.folder / "src" / "new.py").write_text("x = 1\n")
    (fork.folder / "notes.txt").unlink()
    (folder / "README.md").write_text("mine\n")  # the caller works on meanwhile
    head = checkpoints.take("caller", folder, "the caller's own checkpoint")

    result = forks.merge(fork)
    assert result.status == "merged"
    assert sorted(result.files) == ["notes.txt", "src/main.py", "src/new.py"]
    assert (folder / "src" / "main.py").read_text() == "print(2)\n"
    assert (folder / "src" / "new.py").read_text() == "x = 1\n"
    assert not (folder / "notes.txt").exists()
    assert (folder / "README.md").read_text() == "mine\n"
    assert (folder / "node_modules" / "left-pad" / "index.js").exists()  # not touched through the link
    assert "Its changes are in your folder now" in result.describe("sub1")
    # The caller's own checkpoints are left alone; merged changes don't come again.
    assert checkpoints.take("caller", folder, "next") != head
    assert forks.merge(fork).status == "nothing"


def test_a_conflict_is_left_to_the_caller(tmp_path):
    folder = project(tmp_path)
    fork = forks.make("caller", folder, "sub1")
    (fork.folder / "notes.txt").write_text("A\nb\nc\n")
    (folder / "notes.txt").write_text("X\nb\nc\n")

    result = forks.merge(fork)
    assert result.status == "conflict" and result.conflicts == ["notes.txt"]
    assert (folder / "notes.txt").read_text() == "X\nb\nc\n"  # nothing applied
    text = result.describe("sub1")
    assert "-a\n+A" in text and "merge sub1" in text

    marked = forks.merge(fork, markers=True)
    assert marked.status == "merged" and marked.conflicts == ["notes.txt"]
    assert (folder / "notes.txt").read_text() == "<<<<<<< yours\nX\n=======\nA\n>>>>>>> subagent\nb\nc\n"


def test_refresh_brings_the_callers_files_into_the_copy(tmp_path):
    folder = project(tmp_path)
    fork = forks.make("caller", folder, "sub1")
    assert forks.refresh(fork) is None  # nothing new
    (fork.folder / "src" / "main.py").write_text("print(2)\n")  # not handed over yet
    (folder / "README.md").write_text("mine\n")
    assert forks.refresh(fork) == []
    assert (fork.folder / "README.md").read_text() == "mine\n"
    assert (fork.folder / "src" / "main.py").read_text() == "print(2)\n"
    # What's still the subagent's goes over; the caller's own files don't come back as its changes.
    result = forks.merge(fork)
    assert result.files == ["src/main.py"]


def test_removing_a_copy(tmp_path):
    folder = project(tmp_path)
    fork = forks.make("caller", folder, "sub1")
    forks.remove(fork)
    assert not fork.root.exists()
    assert (folder / "node_modules" / "left-pad" / "index.js").exists()


def test_a_copy_is_found_again_and_tidied_at_start(tmp_path):
    folder = project(tmp_path)
    busy = forks.make("caller", folder, "busy")
    (busy.folder / "src" / "main.py").write_text("print(3)\n")
    idle = forks.make("caller", folder, "idle")
    orphan = forks.make("caller", folder, "orphan")
    (orphan.folder / "x.txt").write_text("x\n")
    (forks.folders() / "stray").mkdir()

    loaded = forks.load("busy")
    assert loaded == busy and forks.changed(loaded) == ["src/main.py"]
    kept = forks.tidy(lambda subagent: subagent != "orphan")
    assert kept == ["busy"]
    assert busy.folder.is_dir()  # its changes wait for the user
    assert not idle.root.exists() and not orphan.root.exists() and not (forks.folders() / "stray").exists()


def test_a_failed_copy_leaves_nothing_behind(tmp_path, monkeypatch):
    folder = project(tmp_path)

    def broken(fork):
        raise OSError("disk full")
    monkeypatch.setattr(forks, "_link", broken)
    with pytest.raises(forks.ForkError, match="disk full"):
        forks.make("caller", folder, "sub1")
    assert not (forks.folders() / "sub1").exists()
    assert forks.load("sub1") is None


def test_a_copy_gets_small_ignored_files_but_not_ignored_folders(tmp_path):
    folder = project(tmp_path)
    (folder / ".gitignore").write_text(".env\nbuild/\n*.log\n")
    (folder / ".env").write_text("TOKEN=x\n")
    (folder / "build").mkdir()
    (folder / "build" / "app.bin").write_bytes(b"\0" * 10)
    (folder / "big.log").write_bytes(b"x" * (forks.IGNORED_MAX_BYTES + 1))
    fork = forks.make("caller", folder, "sub1")
    assert (fork.folder / ".env").read_text() == "TOKEN=x\n"
    assert not (fork.folder / "build").exists() and not (fork.folder / "big.log").exists()
    assert (fork.folder / "node_modules").is_symlink()  # linked, not copied into
    (fork.folder / ".env").write_text("TOKEN=changed\n")  # ignored: not the subagent's work to hand over
    assert forks.changed(fork) == []


def test_a_copy_that_cant_be_checked_is_kept(tmp_path, monkeypatch):
    folder = project(tmp_path)
    fork = forks.make("caller", folder, "sub1")

    def broken(fork):
        raise forks.ForkError("disk full")
    monkeypatch.setattr(forks, "changed", broken)
    assert forks.tidy(lambda subagent: True) == ["sub1"]
    assert fork.folder.is_dir()


def test_copies_follow_when_relay_or_the_project_moved(tmp_path, monkeypatch):
    folder = project(tmp_path)
    forks.make("caller", folder, "sub1")
    forks.moved(str(tmp_path / "ws"), str(tmp_path / "elsewhere"))
    moved = forks.load("sub1")
    assert moved.source == tmp_path / "elsewhere" / "app"
    assert (moved.folder / "node_modules").readlink() == tmp_path / "elsewhere" / "app" / "node_modules"
    monkeypatch.setenv("RELAY_HOME", str(tmp_path / "new-home"))  # the data moved: the copy is where it is now
    (tmp_path / "new-home" / "forks" / "sub1").mkdir(parents=True)
    (tmp_path / "new-home" / "forks" / "sub1" / "fork.json").write_text(
        (tmp_path / "relay-home" / "forks" / "sub1" / "fork.json").read_text())
    assert forks.load("sub1").folder == tmp_path / "new-home" / "forks" / "sub1" / "app"


def test_a_copy_whose_fork_json_cant_be_read_is_kept(tmp_path):
    folder = project(tmp_path)
    fork = forks.make("caller", folder, "sub1")
    (fork.root / "fork.json").write_text("{half")
    assert forks.tidy(lambda subagent: True) == []
    assert fork.folder.is_dir()
