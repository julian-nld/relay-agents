import sqlite3
import tarfile

from relay import backup, config
from relay.db import Database


def test_a_backup_has_the_data_and_a_consistent_database(tmp_path, monkeypatch, capsys):
    data = config.data_dir()
    data.mkdir(parents=True)
    db = Database(config.database_path())
    db.execute("INSERT INTO users (id, username, password_hash, created) VALUES (1, 'me', 'x', 0)")
    (data / "settings.json").write_text("{}\n")
    (data / "chats" / "c1").mkdir(parents=True)
    (data / "chats" / "c1" / "notes.md").write_text("hi\n")
    monkeypatch.chdir(tmp_path)

    assert backup.main(None) == 0  # while the database is open
    archive = next(tmp_path.glob("relay-backup-*.tar.gz"))
    assert oct(archive.stat().st_mode & 0o777) == "0o600"
    assert "Wrote" in capsys.readouterr().out
    restored = tmp_path / "restored"
    with tarfile.open(archive) as opened:
        names = opened.getnames()
        opened.extractall(restored, filter="data")
    assert sorted(names) == ["chats/c1/notes.md", "relay.db", "settings.json"]
    with sqlite3.connect(restored / "relay.db") as copy:
        assert copy.execute("SELECT username FROM users").fetchall() == [("me",)]


def test_no_data_no_backup(tmp_path, capsys):
    assert backup.main(str(tmp_path / "x.tar.gz")) == 1
    assert "no Relay data" in capsys.readouterr().err


def test_a_file_that_shrinks_meanwhile_doesnt_spoil_the_archive(tmp_path, monkeypatch):
    data = config.data_dir()
    data.mkdir(parents=True)
    Database(config.database_path())
    (data / "a-growing.log").write_text("x" * 1000)
    (data / "z-after.txt").write_text("still here\n")
    real = backup.tarfile.TarFile.gettarinfo

    def stale(self, *args, **kwargs):  # the size it had a moment ago; the file is shorter now
        info = real(self, *args, **kwargs)
        if info.name == "a-growing.log":
            (data / "a-growing.log").write_text("x" * 10)
            info.size = 1000
        return info
    monkeypatch.setattr(backup.tarfile.TarFile, "gettarinfo", stale)
    target = tmp_path / "b.tar.gz"
    backup.write(target)
    with tarfile.open(target) as opened:
        assert opened.extractfile("z-after.txt").read() == b"still here\n"
        assert len(opened.extractfile("a-growing.log").read()) == 10
    assert not list(tmp_path.glob(".b.tar.gz.partial"))


def test_hard_links_and_named_pipes_dont_spoil_or_stall_the_archive(tmp_path):
    import os
    data = config.data_dir()
    data.mkdir(parents=True)
    Database(config.database_path())
    (data / "a.txt").write_text("same file\n")
    os.link(data / "a.txt", data / "b.txt")
    os.mkfifo(data / "pipe")
    (data / "z.txt").write_text("last\n")
    target = tmp_path / "b.tar.gz"
    backup.write(target)
    with tarfile.open(target) as opened:
        assert opened.extractfile("b.txt").read() == b"same file\n"
        assert opened.extractfile("z.txt").read() == b"last\n"
        assert opened.getmember("pipe").isfifo()


def test_a_backup_has_each_services_data_with_its_box_paused(tmp_path, monkeypatch, capsys):
    """A service's data lives in a podman volume, outside the data folder: it goes in as volumes/<volume>.tar,
    copied while its live box is paused; one that can't be copied is said."""
    import subprocess
    data = config.data_dir()
    data.mkdir(parents=True)
    db = Database(config.database_path())
    db.execute("INSERT INTO users (id, username, password_hash, created) VALUES (1, 'me', 'x', 0)")
    for port, (service_id, name) in enumerate((("a" * 32, "notes"), ("b" * 32, "broken")), 8500):
        db.execute("INSERT INTO services (id, user_id, name, command, public_port, created, live_version) "
                   "VALUES (?, 1, ?, 'x', ?, 0, ?)", (service_id, name, port, f"v-{name}"))
        db.execute("INSERT INTO service_versions (id, service_id, number, folder, status, created) "
                   "VALUES (?, ?, 3, '/x', 'live', 0)", (f"v-{name}", service_id))
    calls = []

    def podman(*args, timeout=120):
        calls.append(list(args))
        if args[0] == "pause":  # (meanwhile the running Relay doesn't health-check it)
            assert db.one("SELECT operation FROM services WHERE id = ?", ("a" * 32,))["operation"] == "backup"
        if args[:2] == ("volume", "export"):
            if args[2] == f"relay-data-{'b' * 12}":
                return subprocess.CompletedProcess(args, 125, "", "Error: no such volume")
            with tarfile.open(args[4], "w") as made:
                note = tmp_path / "counter"
                note.write_text("41\n")
                made.add(note, arcname="counter")
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(backup, "_podman", podman)
    count, skipped = backup.write(tmp_path / "b.tar.gz")
    with tarfile.open(tmp_path / "b.tar.gz") as opened:
        assert f"volumes/relay-data-{'a' * 12}.tar" in opened.getnames()
    assert ["pause", f"relay-prod-{'a' * 8}-3"] in calls and ["unpause", f"relay-prod-{'a' * 8}-3"] in calls
    assert skipped == [f"volumes/relay-data-{'b' * 12}.tar (Error: no such volume)"]
    assert [r["operation"] for r in db.all("SELECT operation FROM services")] == [None, None]  # (cleared again)
