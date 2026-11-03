import os
import shutil
import socket
import tempfile
import time
from pathlib import Path

from relay import cli, config, doctor


def test_doctor_reports_missing_roots_and_passes(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RELAY_HOME", str(tmp_path / "data"))
    settings = config.Settings(workspace_roots=[str(tmp_path / "missing")], port=_free_port())
    checks = doctor.run_checks(settings, check_providers=False)
    by_title = {c.title: c for c in checks}
    assert by_title[f"Workspace root {tmp_path / 'missing'}"].status == doctor.WARN
    assert all(c.status != doctor.FAIL for c in checks)
    assert doctor.report(checks) == 0
    assert "warning" in capsys.readouterr().out


def test_doctor_notices_a_busy_port(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_HOME", str(tmp_path / "data"))
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        settings = config.Settings(host="127.0.0.1", port=busy.getsockname()[1])
        check = doctor._port(settings)
    assert check.status == doctor.WARN and "another program" in check.detail


def test_doctor_fails_when_the_data_folder_is_not_writable(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("")
    monkeypatch.setenv("RELAY_HOME", str(blocker))  # data dir can't be created in a file
    assert doctor._data_dir().status == doctor.FAIL


def test_systemd_units():
    system = cli.systemd_unit()
    assert "User=" in system and "WantedBy=multi-user.target" in system
    assert "relay serve" in system and "Restart=on-failure" in system
    user = cli.systemd_unit(user_service=True, port=8500)
    assert "User=" not in user and "WantedBy=default.target" in user
    assert user.rstrip().endswith("default.target") and "serve --port 8500" in user


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_serve_lets_podman_give_each_box_its_own_scope(monkeypatch):
    """Under systemd, podman keeps a container's conmon in the unit it's run from when it sees
    INVOCATION_ID: restarting Relay would then kill the conmons of boxes that outlive it."""
    import os

    import uvicorn

    import relay.app
    seen = {}
    monkeypatch.setenv("INVOCATION_ID", "0123456789abcdef")
    monkeypatch.setattr(relay.app, "create_app", lambda settings: None)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: seen.update(env=os.environ.get("INVOCATION_ID", "gone")))
    assert cli.serve("127.0.0.1", 1) == 0
    assert seen["env"] == "gone"


def test_serve_on_another_port_gives_agents_that_port(monkeypatch, tmp_path):
    """`relay serve --port N` (a second Relay, a trial): agents reach it there, not at the configured port,
    which may be another Relay's on this machine."""
    import uvicorn

    import relay.app
    from relay.web import local_url
    monkeypatch.setenv("RELAY_HOME", str(tmp_path / "data"))
    seen = {}
    monkeypatch.setattr(relay.app, "create_app", lambda settings: seen.update(url=local_url(settings)))
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: seen.update(port=kwargs["port"]))
    assert cli.serve("127.0.0.1", 8497) == 0
    assert seen == {"url": "http://127.0.0.1:8497", "port": 8497}


def test_relay_stops_while_an_answer_streams(monkeypatch):
    """An answer's event stream stays open until its run ends, and Relay stops its runs at the app's shutdown, which
    uvicorn holds back until every connection is done. Without a limit Relay never stopped while a stream was read
    (a browser's, a plugin's own through the SDK): with relay serve's settings, it stops within seconds."""
    import asyncio
    import contextlib
    import threading
    import urllib.request

    import uvicorn
    from starlette.applications import Starlette
    from starlette.responses import StreamingResponse
    from starlette.routing import Route

    import relay.app
    given = {}
    monkeypatch.setattr(relay.app, "create_app", lambda settings: None)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: given.update(kwargs))
    cli.serve("127.0.0.1", 1)
    stopped = threading.Event()

    async def endless(request):
        async def stream():
            while True:  # (a run that goes on until Relay's shutdown stops it)
                yield "data: {}\n\n"
                await asyncio.sleep(0.2)
        return StreamingResponse(stream(), media_type="text/event-stream")

    @contextlib.asynccontextmanager
    async def lifespan(app):
        yield
        stopped.set()  # (where Relay stops its runs)

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(Starlette(routes=[Route("/events", endless)], lifespan=lifespan),
                                           port=port, **{k: v for k, v in given.items() if k not in ("host", "port")}))
    serving = threading.Thread(target=server.run, daemon=True)
    serving.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    reading = urllib.request.urlopen(f"http://127.0.0.1:{port}/events", timeout=30)
    reading.read(10)
    server.should_exit = True
    serving.join(cli.SHUTDOWN_WAIT + 5)
    assert not serving.is_alive() and stopped.is_set()
    reading.close()


def test_doctor_tells_boxes_another_relay_left_behind(tmp_path, monkeypatch):
    """Another Relay's boxes (another mark, or a service this one doesn't have) whose folder is
    gone: a test run that was stopped. This Relay's own, and ones whose folder is there, aren't."""
    import sqlite3 as sqlite
    import subprocess as sub

    from relay.containers import instance
    from relay.db import Database
    Database(config.database_path())  # (this Relay's database, with its one service)
    with sqlite.connect(config.database_path()) as db:
        db.execute("INSERT INTO users (id, username, password_hash, created) VALUES (1, 'me', 'x', 0)")
        db.execute("INSERT INTO services (id, user_id, name, command, public_port, created) "
                   "VALUES ('known', 1, 'site', 'x', 20999, 0)")
    there = tmp_path / "there"
    there.mkdir()
    gone = str(tmp_path / "gone")
    boxes = {  # name: (mark, managed, service, its folder)
        "relay-mine": (instance(), "1", "", gone),
        "relay-theirs-gone": ("feedface", "1", "", gone),
        "relay-theirs-there": ("feedface", "1", "", str(there)),
        "relay-prod-unknown-1": ("", "", "unknownservice", gone),
        "relay-prod-known-1": ("", "", "known", gone),
        # (another Relay's web search and ProviderBox: their folder is the image's, not this machine's)
        "relay-search": ("feedface", "1", "", "/usr/local/searxng"),
        "relay-box-claude-code": ("feedface", "1", "", "/nowhere"),
    }

    def run(argv, **kwargs):
        if argv[:2] == ["podman", "ps"]:
            rows = [f"{name}|{mark}|{managed}|{service}" for name, (mark, managed, service, _) in boxes.items()]
            return sub.CompletedProcess(argv, 0, "\n".join(rows) + "\n", "")
        if argv[:2] == ["podman", "inspect"]:
            names = argv[argv.index("--format") + 2:]
            return sub.CompletedProcess(argv, 0, "".join(f"{n}|{boxes[n][3]}\n" for n in names), "")
        raise AssertionError(argv)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(doctor.subprocess, "run", run)
    found = doctor._leftovers()
    assert len(found) == 1 and found[0].status == doctor.WARN
    assert found[0].detail.endswith("podman rm -f relay-theirs-gone relay-prod-unknown-1")
    del boxes["relay-theirs-gone"], boxes["relay-prod-unknown-1"]
    assert doctor._leftovers() == []


def test_doctor_checks_perplexity_by_podman(monkeypatch):
    """relay doctor has no boxes of Relay's: Perplexity's check asks for podman then (it said "needs podman"
    on a machine where Perplexity worked)."""
    import shutil

    import pytest

    from relay.providers import KINDS, Template
    from relay.providers.base import Provider, ProviderError
    perplexity = KINDS["perplexity"](Template("pplx", "Perplexity", "perplexity", True, {}))
    monkeypatch.setattr(Provider, "probing", True)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/podman")
    assert "runs in its box" in perplexity.check()
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(ProviderError, match="need podman"):
        perplexity.check()


def test_doctor_says_which_provider_folder_cant_be_loaded_even_without_asking_providers(monkeypatch):
    from relay import providers
    monkeypatch.setitem(providers.BROKEN, "providers/oops", "invalid syntax (oops/provider.py, line 3)")
    found = [c for c in doctor.run_checks(check_providers=False) if c.title.startswith("Provider folder")]
    assert [(c.status, c.title, c.detail) for c in found] == [
        (doctor.WARN, "Provider folder providers/oops", "can't be loaded: invalid syntax (oops/provider.py, line 3)")]


def test_a_data_folder_relay_makes_is_private(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_HOME", str(tmp_path / "new" / "data"))
    assert cli.main(["port"]) == 0
    assert (tmp_path / "new" / "data").stat().st_mode & 0o777 == 0o700


def test_doctor_warns_when_other_users_can_read_the_data(monkeypatch):
    # (under /tmp, which everyone may enter: tmp_path's own parents are private already)
    outer = Path(tempfile.mkdtemp(prefix="relay-doctor-"))
    try:
        data = outer / "data"
        data.mkdir(mode=0o755)
        outer.chmod(0o755)
        monkeypatch.setenv("RELAY_HOME", str(data))
        check = doctor._data_dir()
        assert check.status == doctor.WARN and f"chmod 700 {data}" in check.detail
        outer.chmod(0o700)  # (a private folder above it keeps it private)
        assert doctor._data_dir().status == doctor.OK
        outer.chmod(0o755)
        data.chmod(0o700)
        assert doctor._data_dir().status == doctor.OK
    finally:
        shutil.rmtree(outer)


def test_relay_port_keeps_the_port_for_everything_else(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RELAY_HOME", str(tmp_path / "data"))
    config.save_settings(config.Settings(workspace_roots=["/somewhere"]))
    assert cli.main(["port", "8521"]) == 0
    assert capsys.readouterr().out.strip() == "8521"
    assert (config.load_settings().port, config.load_settings().workspace_roots) == (8521, ["/somewhere"])
    assert "serve --port" not in cli.systemd_unit(user_service=True)  # (the unit reads it from there)


def test_relay_finds_agents_where_their_installers_put_them(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert cli.main(["port"]) == 0
    path = os.environ["PATH"].split(os.pathsep)
    assert path[:2] == ["/usr/bin", "/bin"]  # (what was on it comes first)
    assert str(Path.home() / ".opencode" / "bin") in path and str(Path.home() / ".local" / "bin") in path
    assert f"PATH={os.environ['PATH']}" in cli.systemd_unit(user_service=True)
