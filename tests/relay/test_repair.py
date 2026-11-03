import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from relay import repair
from relay.db import Database
from relay.providers.base import Provider


@pytest.fixture
def repairs(tmp_path, monkeypatch):
    source = tmp_path / "src"
    files = ("relay/one.py", "relay/two.py")
    for relative in (*files, *repair.REFERENCE):
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("value = 1\n")
    (source / "opencode_manager").mkdir(exist_ok=True)
    monkeypatch.setattr(repair, "SOURCE", source)
    monkeypatch.setitem(repair.KINDS, "test", SimpleNamespace(support_files=lambda: files))
    monkeypatch.setattr(repair, "shadow_test", lambda *args, **kwargs: {"ok": True})
    db = Database(":memory:")
    db.execute("INSERT INTO users (id, username, password_hash, created) VALUES (1, 'me', '!', 0)")
    template = SimpleNamespace(id="test", name="Test", kind="test", home="")
    registry = SimpleNamespace(get=lambda _: template, provider=lambda _: SimpleNamespace(check=lambda: "ok"),
                               directory=tmp_path / "providers")
    manager = repair.Repairs(db, registry)
    base = manager.prepare_folder("0123456789ab", template)
    db.execute("INSERT INTO repairs (id, user_id, target_id, kind, folder, base_commit, status, created) "
               "VALUES ('0123456789ab', 1, 'test', 'test', ?, ?, 'working', 0)",
               (str(repair.repairs_dir() / "0123456789ab"), base))
    row = manager.get(1, "0123456789ab")
    yield manager, row, source, files
    db.close()



def test_a_lost_login_is_the_users_to_sign_in_again(repairs, monkeypatch):
    """What the repair's agent reads of a lost login: where the user signs in again (its Shell, with Provider.SIGN_IN
    if it has one), and that repair_run can't; a program without a box is the machine's to fix."""
    manager, _, _, files = repairs
    template = SimpleNamespace(id="test", name="Test", kind="test", home="")
    monkeypatch.setattr(manager, "trouble", lambda _: {"check": {"ok": True, "message": "1.0"}, "models": {},
                                                        "errors": []})

    def kind(box, **said):
        return type("Kind", (), {"support_files": staticmethod(lambda: files), "PROGRAMS": ("toy",),
                                 "__init__": lambda self, template: None, "has_shell": lambda self: box, **said})

    monkeypatch.setitem(repair.KINDS, "test", kind(True, SIGN_IN="toy login"))
    brief = manager._brief(template, files, "")
    assert "`toy login`" in brief and "`repair_run` has no terminal" in brief and "has it read-only" not in brief
    monkeypatch.setitem(repair.KINDS, "test", kind(True))  # (a box, and no such command)
    brief = manager._brief(template, files, "")
    assert "a shell in its box" in brief and "`repair_run` has no terminal" in brief and "toy login" not in brief
    monkeypatch.setitem(repair.KINDS, "test", kind(False))
    brief = manager._brief(template, files, "")
    assert "this machine's" in brief and "toy login" not in brief

def candidate(row, files, value=2):
    folder = repair.Path(row["folder"])
    for relative in files:
        (folder / relative).write_text(f"value = {value}\n")
    return folder


def test_review_and_discard_leave_concurrent_source_changes_alone(repairs):
    manager, row, source, files = repairs
    candidate(row, files)
    (source / files[0]).write_text("my_edit = True\n")
    (source / files[1]).unlink()
    added = source / "relay/new.py"
    added.write_text("new = True\n")
    review = manager.review(1, row["id"])["review"]
    assert review["restored"] == []
    assert review["left_alone"] == ["relay/new.py", *files]
    manager.discard(1, row["id"])
    assert (source / files[0]).read_text() == "my_edit = True\n"
    assert not (source / files[1]).exists()
    assert added.read_text() == "new = True\n"


def test_apply_uses_the_version_reviewed_and_tested(repairs, monkeypatch):
    manager, row, source, files = repairs
    folder = candidate(row, files)
    def test(snapshot, *args, **kwargs):
        assert snapshot != folder
        assert (snapshot / files[0]).read_text() == "value = 2\n"
        # Editing while the shadow test runs must not change what will be applied.
        candidate(row, files, 3)
        return {"ok": True}
    monkeypatch.setattr(repair, "shadow_test", test)
    review = manager.review(1, row["id"])["review"]
    assert "+value = 2" in review["diff"]
    manager.apply(1, row["id"])
    assert all((source / f).read_text() == "value = 2\n" for f in files)
    manager.undo(1, row["id"])
    assert all((source / f).read_text() == "value = 1\n" for f in files)


def test_undo_checks_every_file_before_writing_any(repairs):
    manager, row, source, files = repairs
    candidate(row, files)
    manager.review(1, row["id"])
    manager.apply(1, row["id"])
    (source / files[1]).write_text("my_edit = True\n")
    with pytest.raises(repair.RepairError, match="changed in Relay after"):
        manager.undo(1, row["id"])
    assert (source / files[0]).read_text() == "value = 2\n"
    assert (source / files[1]).read_text() == "my_edit = True\n"
    assert manager.get(1, row["id"])["status"] == "applied"


def test_apply_refuses_missing_or_changed_originals(repairs):
    manager, row, source, files = repairs
    candidate(row, files)
    manager.review(1, row["id"])
    (source / files[1]).unlink()
    with pytest.raises(repair.RepairError, match="changed in Relay meanwhile"):
        manager.apply(1, row["id"])
    assert (source / files[0]).read_text() == "value = 1\n"


def test_apply_refuses_a_changed_snapshot(repairs):
    manager, row, source, files = repairs
    candidate(row, files)
    review = manager.review(1, row["id"])["review"]
    path = repair._reviewed(row["id"], review["snapshot"]) / files[0]
    path.chmod(0o644)
    path.write_text("value = 99\n")
    with pytest.raises(repair.RepairError, match="snapshot changed"):
        manager.apply(1, row["id"])
    assert (source / files[0]).read_text() == "value = 1\n"


def test_a_command_runs_in_the_programs_own_box(repairs, tmp_path, monkeypatch):
    """repair_run: in the provider's box, with its program on PATH and its environment, by the box's real bash (its
    /bin/bash hands commands to a WorkBox); the one hard rail holds; one that hangs is ended in the box."""
    import subprocess

    from relay.containers import SCRATCH
    manager, row, _, _ = repairs
    program = tmp_path / "bin" / "fakeprog"
    program.parent.mkdir()
    program.write_text("#!/bin/sh\n")
    program.chmod(0o755)
    provider = SimpleNamespace(template=SimpleNamespace(name="Test"), box_key=lambda: "test",
                               box_mounts=lambda: [(program, False), (tmp_path, True)], box_image=lambda: None,
                               box_home=lambda: None, env=lambda: {"TEST_CONFIG_DIR": "/cfg"},
                               box_shell=lambda: Provider.box_shell(provider))
    manager.registry.provider = lambda _: provider
    boxes = SimpleNamespace(ensure=lambda key, mounts, folder, busy, image, home: f"relay-box-{key}")
    ran = []

    def run(argv, **kw):
        ran.append(argv)
        if "sleep 9" in argv[-1]:
            raise subprocess.TimeoutExpired(argv, 1, output=b"half")
        return subprocess.CompletedProcess(argv, 0, "fakeprog 1.2\n", "")
    monkeypatch.setattr(repair.subprocess, "run", run)
    answer = manager.run(manager.get(1, row["id"]), "fakeprog --version", boxes)
    assert answer == "fakeprog 1.2\n[exit 0]"
    argv = ran[0]
    assert argv[:5] == ["podman", "exec", "-i", "-w", str(SCRATCH)]
    assert argv[-4:] == ["relay-box-test", "/usr/lib/relay/bash.real", "-c", "fakeprog --version"]
    assert "TEST_CONFIG_DIR=/cfg" in argv and f"PATH={program.parent}:/usr/local/bin:/usr/bin:/bin" in argv
    with pytest.raises(repair.RepairError, match="blocked"):
        manager.run(manager.get(1, row["id"]), "rm -rf /", boxes)
    ran.clear()
    assert manager.run(manager.get(1, row["id"]), "sleep 9", boxes, timeout=1) == "half\n[stopped after 1 s]"
    assert ran[-1][:3] == ["podman", "exec", "relay-box-test"] and "relay-reap" in ran[-1][3]  # (ended in its box)
    with pytest.raises(repair.RepairError, match="doesn't run in a box"):
        manager.run(manager.get(1, row["id"]), "fakeprog --version", None)
    # A box of the WorkBox image (Perplexity's) has no Relay shell over its bash: its own runs it
    provider.box_image = lambda: "workbox"
    ran.clear()
    manager.run(manager.get(1, row["id"]), "fakeprog --version", boxes)
    assert ran[0][-4:] == ["relay-box-test", "bash", "-c", "fakeprog --version"]
    # One with a home of its own: what's installed there is on PATH, after its mounted program
    provider.box_home = lambda: tmp_path / "home"
    ran.clear()
    manager.run(manager.get(1, row["id"]), "fakeprog --version", boxes)
    assert f"PATH={program.parent}:{tmp_path / 'home' / '.local' / 'bin'}:" in " ".join(ran[0])


def test_a_new_providers_command_runs_where_the_user_installed_it(repairs, tmp_path, monkeypatch):
    """repair_run for a new provider (relay.creations): in its setup box, in its home, by its real bash."""
    import subprocess
    manager, _, _, _ = repairs
    ran = []
    monkeypatch.setattr(repair.subprocess, "run", lambda argv, **kw: ran.append(argv) or subprocess.CompletedProcess(
        argv, 0, "gemini 0.63\n", ""))
    boxes = SimpleNamespace(setup=lambda key, home: f"relay-setup-{key}")
    creation = {"id": "abc", "creates": 1, "home": str(tmp_path / "home"), "status": "planning", "target_id": "",
                "folder": str(tmp_path / "abc")}
    assert manager.run(creation, "gemini --version", boxes) == "gemini 0.63\n[exit 0]"
    assert ran[0][:5] == ["podman", "exec", "-i", "-w", str(tmp_path / "home")]
    assert ran[0][-4:] == ["relay-setup-new-abc", "/usr/lib/relay/bash.real", "-c", "gemini --version"]
    with pytest.raises(repair.RepairError, match="plan first"):
        manager.check(creation)


def templates(folder):
    folder.mkdir()
    (folder / "toy.json").write_text(json.dumps({"id": "toy", "name": "Toy", "kind": "example", "options": {}}))
    (folder / "keyed.json").write_text(json.dumps({"id": "keyed", "name": "Keyed", "kind": "openai-compatible",
                                                   "options": {"base_url": "http://x", "api_key": "sk-secret"}}))
    return folder


def test_a_repairs_test_runs_in_a_box_with_its_template_alone(tmp_path, monkeypatch, host_boxes):
    """The agent's code is tested in a box (Boxes.test), twice: first it says what it would have mounted, then the
    test runs with that. Of the user's templates only the provider's own is there (not the others' keys)."""
    monkeypatch.setenv("HOME", str(tmp_path))  # (the example's folder, in Relay's data, is in the home folder)
    boxes = host_boxes
    result = repair.shadow_test(tmp_path, (), "toy", None, templates(tmp_path / "providers"), boxes)
    assert result["ok"], result
    assert [c["templates"] for c in boxes.calls] == [["toy.json"], ["toy.json"]]
    first, second = (len(c["mounts"]) for c in boxes.calls)
    assert second == first + 2 and [Path(m) for m, _ in result["mounts"]][-1].name == "toy"


def test_a_provider_with_a_home_of_its_own_is_tested_with_it(tmp_path, monkeypatch, host_boxes):
    """A provider whose program the user installed in a home of its own (Template.home): its test box has that
    home, as its HOME, with what's installed there first on PATH, as its box has."""
    monkeypatch.setenv("HOME", str(tmp_path))
    home = tmp_path / "provider-homes" / "toy"
    (home / ".local" / "bin").mkdir(parents=True)
    folder = templates(tmp_path / "providers")
    toy = json.loads((folder / "toy.json").read_text())
    (folder / "toy.json").write_text(json.dumps({**toy, "home": str(home)}))
    result = repair.shadow_test(tmp_path, (), "toy", None, folder, host_boxes)
    assert result["ok"], result
    asked, tested = host_boxes.calls
    assert (home, True) in tested["mounts"] and [str(home), True] in result["mounts"]
    assert tested["env"]["HOME"] == str(home) and tested["env"]["NPM_CONFIG_PREFIX"] == str(home / ".local")
    assert tested["env"]["PATH"].split(":").index(str(home / ".local" / "bin")) < \
        tested["env"]["PATH"].split(":").index("/usr/bin")


def test_what_a_provider_may_not_have_mounted_ends_its_test(tmp_path, monkeypatch, host_boxes):
    monkeypatch.setenv("HOME", str(tmp_path))
    layer = repair.SOURCE / "relay/providers/example/provider.py"
    changed = tmp_path / "candidate" / "relay/providers/example/provider.py"
    changed.parent.mkdir(parents=True)
    text = layer.read_text()
    old = "        return [(PROGRAM.parent, False), (self.folder(), True)]"
    assert old in text
    changed.write_text(text.replace(old, '        return [(Path.home() / ".ssh", True)]'))
    boxes = host_boxes
    result = repair.shadow_test(tmp_path / "candidate", ("relay/providers/example/provider.py",), "toy", None,
                                templates(tmp_path / "providers"), boxes)
    assert not result["ok"] and ".ssh" in result["error"] and "kept out" in result["error"], result
    assert len(boxes.calls) == 1  # (what it would mount only: its test never ran)


def test_what_may_be_mounted_in_a_test_box(tmp_path, monkeypatch):
    from relay import boxes, config
    monkeypatch.setenv("HOME", str(tmp_path))
    data = config.data_dir()
    for path, writable in ((tmp_path / ".gemini", True), (Path("/usr/local/bin/gemini"), False),
                           (Path("/usr/lib/node_modules/x"), False), (data / "example" / "toy", True)):
        assert boxes.refused_mount(path, writable) is None, path
    assert "too much" in boxes.refused_mount(tmp_path, True) and "too much" in boxes.refused_mount(Path("/"), False)
    assert "in the home folder" in boxes.refused_mount(Path("/opt/x"), True)
    for kept in (tmp_path / ".ssh", tmp_path / ".ssh" / "id_rsa", tmp_path / ".aws", data / "relay.db", data / "providers",
                 data, repair.SOURCE.parent):
        assert boxes.refused_mount(kept, False), kept
    roots = SimpleNamespace(workspace_roots=[str(tmp_path / "projects")])
    assert boxes.refused_mount(tmp_path / "projects" / "a", True, roots)
    monkeypatch.setenv("RELAY_HOME", "/srv/relay")  # (Relay's data elsewhere: a provider's folder there is fine)
    assert boxes.refused_mount(Path("/srv/relay/opencode-manager/profiles"), True) is None
    assert boxes.refused_mount(Path("/srv/relay/relay.db"), True)


def test_without_boxes_theres_no_test(tmp_path):
    result = repair.shadow_test(tmp_path, (), "toy", None, templates(tmp_path / "providers"), None)
    assert not result["ok"] and "podman" in result["error"]
