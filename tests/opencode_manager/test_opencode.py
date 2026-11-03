import itertools
from pathlib import Path

import pytest

from opencode_manager import opencode
from opencode_manager.profiles import Profile, ProfileStore
from opencode_manager.settings import Settings


def test_service_mode_command():
    cmd = opencode.command(Settings(), Profile("default"), Path("/my project"))
    assert cmd == "opencode '/my project'"


def test_standalone_mode_command():
    cmd = opencode.command(Settings(launch_mode="standalone"), Profile("default"), Path("/p"))
    assert cmd == "opencode --standalone /p"


def test_login_first_runs_login_then_opencode():
    cmd = opencode.command(Settings(), Profile("default"), Path("/p"), login_first=True)
    assert cmd == "opencode auth login; exec opencode /p"


def _store(tmp_path, free=lambda port: True):
    return ProfileStore(tmp_path / "profiles", base_port=50000, is_port_free=free)


def test_prepare_sets_port_only_when_needed(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(opencode, "_run", lambda s, p, *args, **kw: calls.append(args))
    monkeypatch.setattr(opencode.ports, "is_free", lambda port: True)
    store = _store(tmp_path)
    profile = store.create("work")

    assert opencode.prepare(Settings(), store, profile) == profile
    assert calls == [("service", "set", "port", "50000")]

    calls.clear()
    (profile.config_dir / "service.json").write_text('{"port": 50000}')
    opencode.prepare(Settings(), store, profile)
    opencode.prepare(Settings(), store, Profile("default"))
    opencode.prepare(Settings(launch_mode="standalone"), store, profile)
    assert calls == []


def test_prepare_keeps_port_when_own_service_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(opencode, "_run", lambda *a, **kw: None)
    monkeypatch.setattr(opencode.ports, "is_free", lambda port: False)
    monkeypatch.setattr(opencode, "serves", lambda s, p: True)
    store = _store(tmp_path)
    profile = store.create("work")
    assert opencode.prepare(Settings(), store, profile).port == 50000


def test_prepare_moves_off_port_taken_by_something_else(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(opencode, "_run", lambda s, p, *args, **kw: calls.append(args))
    taken = {50000, 50001}
    monkeypatch.setattr(opencode.ports, "is_free", lambda port: port not in taken)
    monkeypatch.setattr(opencode, "serves", lambda s, p: False)
    store = _store(tmp_path)  # allocation at create time ignores the host here
    profile = store.create("work")
    store.is_port_free = lambda port: port not in taken

    moved = opencode.prepare(Settings(), store, profile)
    assert moved.port == 50002
    assert store.get("work").port == 50002  # persisted
    assert calls == [("service", "set", "port", "50002")]


def test_serves_requires_a_password(tmp_path):
    profile = _store(tmp_path).create("work")
    assert opencode.serves(Settings(), profile) is False


def _program(folder: Path, version: str, help_text: str) -> Path:
    """A stand-in opencode: its --version, and what `service --help` prints."""
    folder.mkdir()
    program = folder / "opencode"
    program.write_text(f"#!/bin/sh\nif [ \"$1\" = --version ]; then echo '{version}'; else cat <<'EOF'\n{help_text}\nEOF\nfi\n")
    program.chmod(0o755)
    return program


def test_opencode_1_is_named_with_how_to_get_opencode_2(tmp_path, monkeypatch):
    # opencode 1 has no `service`: it prints its general help (and would take "service" for a project folder)
    old = _program(tmp_path / "v1", "1.18.35", "Commands:\n  opencode serve    starts a headless opencode server")
    with pytest.raises(opencode.OpencodeError, match=r"opencode 1\.18\.35 has no background service.*/v2/install"):
        opencode.require_service(Settings(opencode_bin=str(old)))
    monkeypatch.setattr(opencode, "_run", lambda *a, **kw: pytest.fail("tried to start it"))
    with pytest.raises(opencode.OpencodeError, match="install opencode 2"):
        opencode.ensure_service(Settings(opencode_bin=str(old)), _store(tmp_path), Profile("default"))
    new = _program(tmp_path / "v2", "opencode v2.0.26", "USAGE\n  opencode service <subcommand> [flags]")
    opencode.require_service(Settings(opencode_bin=str(new)))


def test_a_port_another_program_holds_is_said_soon(tmp_path, monkeypatch):
    monkeypatch.setattr(opencode, "require_service", lambda settings: None)
    monkeypatch.setattr(opencode.ports, "is_free", lambda port: False)
    monkeypatch.setattr(opencode, "serves", lambda s, p: False)
    monkeypatch.setattr(opencode, "_run", lambda *a, **kw: pytest.fail("started a second one"))
    clock = itertools.count()
    monkeypatch.setattr(opencode.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(opencode.time, "sleep", lambda seconds: None)
    with pytest.raises(opencode.OpencodeError, match=r"Port \d+.*taken by another program.*service set port <a"):
        opencode.ensure_service(Settings(), _store(tmp_path), Profile("default"), timeout=30)
    assert next(clock) < 10  # (not the whole 30 s)
    # …with a port that's free, to give it
    port = opencode.service_port(Profile("default"))
    monkeypatch.setattr(opencode.ports, "is_free", lambda p: p == port + 3)
    clock = itertools.count()
    with pytest.raises(opencode.OpencodeError, match=rf"service set port {port + 3}$"):
        opencode.ensure_service(Settings(), _store(tmp_path), Profile("default"), timeout=30)
