import json
import os
import subprocess

from relay import config, relocation
from relay.db import Database
from relay.providers.claude import claude_project_folder


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_a_moved_install_rewrites_its_paths_and_repairs_worktrees(tmp_path, monkeypatch):
    old_root, new_root = tmp_path / "old-stick", tmp_path / "new mount" / "stick"
    # A repository and a task worktree inside the folder, as they were before the move
    repo = old_root / "projects" / "app"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "-c", "user.name=T", "-c", "user.email=t@x", "commit", "-q", "--allow-empty", "-m", "start")
    worktree = old_root / "data" / "relay" / "worktrees" / "c1"
    git(repo, "worktree", "add", "-q", "-b", "relay/task-c1", str(worktree))
    monkeypatch.setattr("opencode_manager.paths.root", lambda: old_root)
    monkeypatch.setenv("RELAY_HOME", str(old_root / "data"))
    config.data_dir().mkdir(parents=True, exist_ok=True)
    config.settings_path().write_text(json.dumps({"workspace_roots": [str(old_root / "projects"), "/elsewhere"]}))
    db = Database(config.database_path())
    assert relocation.relocate(db) == []  # the first start just records where it is
    db.execute("INSERT INTO users (id, username, password_hash, created) VALUES (1, 'me', 'x', 0)")
    db.execute("INSERT INTO conversations (id, user_id, title, provider_id, mode, directory, git_mode, repo_root, "
               "worktree, created, updated) VALUES ('c1', 1, 't', 'p', 'code', ?, 'worktree', ?, ?, 0, 0)",
               (str(worktree), str(repo), str(worktree)))
    db.execute("INSERT INTO uploads (id, user_id, name, mime, size, path, created) VALUES ('u1', 1, 'a', 'x', 1, ?, 0)",
               (str(old_root / "data" / "relay" / "uploads" / "u1" / "a"),))
    db.execute("INSERT INTO shares (token, user_id, directory, name, created) VALUES ('t', 1, ?, 'x', 0)", (str(repo),))
    db.execute("INSERT INTO repairs (id, user_id, target_id, kind, folder, status, created) "
               "VALUES ('r', 1, 'p', 'command', ?, 'working', 0)", (str(old_root / "data" / "relay" / "repairs" / "r"),))
    db.close()

    # Copied to another place (another machine, another mount point)
    new_root.parent.mkdir(parents=True)
    os.rename(old_root, new_root)
    monkeypatch.setattr("opencode_manager.paths.root", lambda: new_root)
    monkeypatch.setenv("RELAY_HOME", str(new_root / "data"))
    db = Database(config.database_path())
    notes = relocation.relocate(db)
    assert any("moved from" in n for n in notes)
    row = db.one("SELECT * FROM conversations WHERE id = 'c1'")
    new_worktree = new_root / "data" / "relay" / "worktrees" / "c1"
    assert row["directory"] == row["worktree"] == str(new_worktree)
    assert row["repo_root"] == str(new_root / "projects" / "app")
    assert db.one("SELECT path FROM uploads")["path"].startswith(str(new_root / "data"))
    assert db.one("SELECT directory FROM shares")["directory"] == str(new_root / "projects" / "app")
    assert db.one("SELECT folder FROM repairs")["folder"].startswith(str(new_root / "data"))
    settings = json.loads(config.settings_path().read_text())
    assert settings["workspace_roots"] == [str(new_root / "projects"), "/elsewhere"]
    # git knows the worktree again, from both sides
    status = subprocess.run(["git", "status", "--short"], cwd=new_worktree, capture_output=True, text=True)
    assert status.returncode == 0, status.stderr
    assert relocation.relocate(db) == []  # nothing more to do


def test_relative_workspace_roots_are_inside_the_relay_folder(tmp_path, monkeypatch):
    monkeypatch.setattr("opencode_manager.paths.root", lambda: tmp_path)
    settings = config.Settings(workspace_roots=["projects", str(tmp_path / "abs")])
    assert settings.roots() == [(tmp_path / "projects").resolve(), (tmp_path / "abs").resolve()]


def test_data_from_before_relay_noted_its_place_is_moved_too(tmp_path, monkeypatch):
    old_root, new_root = tmp_path / "old", tmp_path / "new"
    monkeypatch.setattr("opencode_manager.paths.root", lambda: new_root)
    monkeypatch.setenv("RELAY_HOME", str(new_root / "relay"))
    config.data_dir().mkdir(parents=True)
    db = Database(config.database_path())
    db.execute("INSERT INTO users (id, username, password_hash, created) VALUES (1, 'me', 'x', 0)")
    old_chat = old_root / "relay" / "chats" / ("a" * 32)
    db.execute("INSERT INTO conversations (id, user_id, title, provider_id, mode, directory, created, updated) "
               "VALUES ('c', 1, 't', 'p', 'chat', ?, 0, 0)", (str(old_chat),))
    assert relocation.relocate(db)  # no note of where it was: judged by its chat folder
    assert db.one("SELECT directory FROM conversations")["directory"] == str(new_root / "relay" / "chats" / ("a" * 32))


def test_claude_code_sessions_move_along_with_their_conversations(tmp_path, monkeypatch):
    old, new = tmp_path / "old" / "relay", tmp_path / "new" / "relay"
    claude = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.setenv("RELAY_HOME", str(old))
    old.mkdir(parents=True)
    db = Database(config.database_path())
    relocation.relocate(db)
    db.execute("INSERT INTO users (id, username, password_hash, created) VALUES (1, 'me', 'x', 0)")
    db.execute("INSERT INTO conversations (id, user_id, title, provider_id, mode, directory, created, updated) "
               "VALUES ('c1', 1, 't', 'p', 'chat', ?, 0, 0)", (str(old / "chats" / "c1"),))
    db.close()
    sessions = claude / "projects" / claude_project_folder(old / "chats" / "c1")
    sessions.mkdir(parents=True)
    (sessions / "s1.jsonl").write_text("{}\n")

    new.parent.mkdir()
    os.rename(old, new)
    monkeypatch.setenv("RELAY_HOME", str(new))
    db = Database(config.database_path())
    notes = relocation.relocate(db)
    assert "moved 1 Claude Code session folder(s) along" in notes
    assert (claude / "projects" / claude_project_folder(new / "chats" / "c1") / "s1.jsonl").is_file()


def test_another_machine_and_user_moves_the_projects_and_services_too(tmp_path, monkeypatch):
    """All of it copied to a server, under another user: the data, the code and the projects in the
    home folder each land elsewhere; a service's versions follow the data folder."""
    old_home, new_home = tmp_path / "home" / "workstation", tmp_path / "home" / "relay"
    for home, root in ((old_home, tmp_path / "workspace" / "relay"), (new_home, new_home / "relay")):
        (home / ".local" / "share" / "relay").mkdir(parents=True)
        root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(old_home))
    monkeypatch.setattr("opencode_manager.paths.root", lambda: tmp_path / "workspace" / "relay")
    monkeypatch.setenv("RELAY_HOME", str(old_home / ".local" / "share" / "relay"))
    config.settings_path().write_text(json.dumps({"workspace_roots": [str(old_home / "projects")]}))
    db = Database(config.database_path())
    relocation.relocate(db)
    db.execute("DELETE FROM meta WHERE key = 'home'")  # (noted by a Relay from before it kept its home folder)
    db.execute("INSERT INTO users (id, username, password_hash, created) VALUES (1, 'me', 'x', 0)")
    project = old_home / "projects" / "site"
    db.execute("INSERT INTO conversations (id, user_id, title, provider_id, mode, directory, created, updated) "
               "VALUES ('c', 1, 't', 'claude-code', 'code', ?, 0, 0)", (str(project),))
    db.execute("INSERT INTO services (id, user_id, name, command, public_port, created) VALUES ('s', 1, 'n', 'x', 9999, 0)")
    version = old_home / ".local" / "share" / "relay" / "production" / "s" / "1" / "n"
    db.execute("INSERT INTO service_versions (id, service_id, number, folder, source, status, data_snapshot, created) "
               "VALUES ('v', 's', 1, ?, ?, 'live', ?, 0)", (str(version), str(project), str(version.parent / "x.tar")))
    sessions = old_home / ".claude" / "projects" / claude_project_folder(project)
    sessions.mkdir(parents=True)
    db.close()

    # On the server: /home/relay, the code in ~/relay, the data and ~/.claude copied along
    import shutil
    shutil.copytree(old_home / ".local", new_home / ".local", dirs_exist_ok=True)
    shutil.copytree(old_home / ".claude", new_home / ".claude")
    monkeypatch.setenv("HOME", str(new_home))
    monkeypatch.setattr("opencode_manager.paths.root", lambda: new_home / "relay")
    monkeypatch.setenv("RELAY_HOME", str(new_home / ".local" / "share" / "relay"))
    db = Database(config.database_path())
    notes = relocation.relocate(db)
    assert sum("moved from" in n for n in notes) == 3, notes  # data, code and home
    assert db.one("SELECT directory FROM conversations")["directory"] == str(new_home / "projects" / "site")
    row = db.one("SELECT * FROM service_versions")
    assert row["folder"] == str(new_home / ".local" / "share" / "relay" / "production" / "s" / "1" / "n")
    assert row["source"] == str(new_home / "projects" / "site") and row["data_snapshot"].startswith(str(new_home))
    assert json.loads(config.settings_path().read_text())["workspace_roots"] == [str(new_home / "projects")]
    assert (new_home / ".claude" / "projects" / claude_project_folder(new_home / "projects" / "site")).is_dir()
    assert relocation.relocate(db) == []  # nothing more to do
