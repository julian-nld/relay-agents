import json

import pytest

from opencode_manager import cli, opencode, paths, scripting, tmux


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.HOME_ENV, str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "host-config"))
    monkeypatch.setattr(tmux, "has_session", lambda name: False)
    monkeypatch.setattr(cli.ports, "is_free", lambda port: True)
    monkeypatch.setattr(scripting.ports, "is_free", lambda port: True)
    return tmp_path


def run(capsys, *argv):
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_status_json_when_not_running(home, capsys):
    code, out, _ = run(capsys, "status", "--json")
    data = json.loads(out)
    assert code == 0
    assert data["running"] is False and data["project"] is None
    assert data["profile"] == "default"
    assert [p["name"] for p in data["profiles"]] == ["default"]


@pytest.mark.parametrize("argv", [["switch", "default"], ["open", "."], ["restart"]])
def test_workspace_commands_exit_3_when_not_running(home, capsys, argv):
    code, _, err = run(capsys, *argv)
    assert code == cli.EXIT_NOT_RUNNING
    assert "not running" in err


def test_profile_create_list_delete(home, capsys, monkeypatch):
    # Deletion stops the service first, but this CLI test must not need or
    # invoke an installed opencode program.
    stopped = []
    monkeypatch.setattr(opencode, "stop_service", lambda settings, profile: stopped.append(profile.name))
    code, out, _ = run(capsys, "profile", "create", "work", "--json")
    assert code == 0 and json.loads(out)["name"] == "work"

    code, _, err = run(capsys, "profile", "create", "work")
    assert code == 1 and "already exists" in err

    code, out, _ = run(capsys, "profiles", "--json")
    work = next(p for p in json.loads(out) if p["name"] == "work")
    assert work["service"] == "stopped" and work["active"] is False

    code, _, err = run(capsys, "profile", "delete", "work")
    assert code == 1 and "--yes" in err

    code, out, _ = run(capsys, "profile", "delete", "work", "--yes")
    assert code == 0 and out.strip() == "Deleted profile work"
    assert stopped == ["work"]


def test_profile_create_can_copy_host_config(home, capsys):
    host = home / "host-config" / "opencode"
    host.mkdir(parents=True)
    (host / "opencode.json").write_text('{"model": "x/y"}')
    run(capsys, "profile", "create", "work", "--copy-config")
    _, out, _ = run(capsys, "config", "path", "work")
    assert open(out.strip()).read() == '{"model": "x/y"}'


def test_config_check(home, capsys):
    run(capsys, "profile", "create", "work")
    code, out, _ = run(capsys, "config", "check", "work", "--json")
    data = json.loads(out)
    assert code == 0 and data["exists"] is False and data["valid"] is True
    assert data["path"].endswith("profiles/work/config/opencode.json")

    _, path, _ = run(capsys, "config", "path", "work")
    with open(path.strip(), "w") as fh:
        fh.write('{"a": 1 "b": 2}')
    code, _, err = run(capsys, "config", "check", "work")
    assert code == 1 and "Line 1, column 9" in err


def test_unknown_profile_is_an_error(home, capsys):
    code, _, err = run(capsys, "config", "path", "nope")
    assert code == 1 and "does not exist" in err


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["oc", "-p", "work", "--", "run", "--model", "a/b", "hi"], ["run", "--model", "a/b", "hi"]),
        (["oc", "-p", "work", "models"], ["models"]),
    ],
)
def test_oc_runs_opencode_with_profile_env(home, capsys, monkeypatch, argv, expected):
    run(capsys, "profile", "create", "work")
    monkeypatch.setattr(opencode, "_run", lambda *a, **kw: None)
    monkeypatch.setattr(scripting.shutil, "which", lambda *a, **kw: "/bin/opencode")
    calls = []

    def fake_exec(binary, args, env):
        calls.append((args, env))
        raise SystemExit(0)

    monkeypatch.setattr(scripting.os, "execvpe", fake_exec)
    with pytest.raises(SystemExit):
        cli.main(argv)
    args, env = calls[0]
    assert args[1:] == expected
    assert env["OPENCODE_CONFIG_DIR"].endswith("profiles/work/config")
    assert env["XDG_DATA_HOME"].endswith("profiles/work/data")


def test_bare_directory_still_means_start(home, monkeypatch):
    started = []
    monkeypatch.setattr(cli, "_start", lambda s, st, d, p: started.append((str(d), p)) or 0)
    assert cli.main(["some/dir", "-p", "work"]) == 0
    assert started == [("some/dir", "work")]
