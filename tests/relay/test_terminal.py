"""Terminal ownership/protocol and real PTY behaviour, without containers or models."""

import asyncio
import json
import os
import pty
import re
import signal
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import quote

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from relay import config, guard
from relay.app import create_app
from relay.containers import ContainerError, Containers, instance
from relay.db import Database
from relay.providers import Registry
from relay.terminal import Terminal, dimensions

H = {"x-relay-request": "1"}
RC = Path(__file__).resolve().parents[2] / "src/relay/container/terminal.bash"


class PtyContainers:
    """An isolated host PTY for protocol tests; production always uses podman exec -it."""

    def __init__(self, home):
        self.home = home
        home.mkdir()
        self.started = []
        self.closed = []

    def name(self, directory):
        return "test-work-container"

    def terminal_argv(self, directory, tag):
        self.started.append((directory, tag))
        return ["env", f"HOME={self.home}", "TERM=xterm-256color", f"RELAY_TERMINAL={tag}",
                f"RELAY_TERMINAL_GUARD={guard.__file__}", "bash", "--rcfile", str(RC), "-i"]

    def discard(self, directory):
        pass  # (a chat deleted: its container would go)

    def close_terminal(self, name, tag):
        self.closed.append((name, tag))
        # Only processes with this test's unique tag; also catches detached shell children.
        marker = f"RELAY_TERMINAL={tag}".encode()
        for proc in Path("/proc").glob("[0-9]*"):
            try:
                if marker in (proc / "environ").read_bytes().split(b"\0"):
                    os.kill(int(proc.name), signal.SIGKILL)
            except OSError:
                pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    registry = Registry(tmp_path / "providers")
    registry.add({"id": "fake", "name": "Test", "kind": "command", "options": {"command": ["true"]}})
    monkeypatch.setattr(registry, "ensure_defaults", lambda: None)
    settings = config.Settings(workspace_roots=[str(project)], chat_root=str(tmp_path / "chats"))
    app = create_app(settings, Database(tmp_path / "relay.db"), registry)
    containers = PtyContainers(tmp_path / "home")
    with TestClient(app) as client:
        client.post("/api/setup", json={"username": "me", "password": "correct horse"}, headers=H).raise_for_status()
        app.state.relay.containers = containers
        row = client.post("/api/conversations", json={"provider_id": "fake", "mode": "code", "directory": str(project)}, headers=H).json()
        yield client, app.state.relay, row, containers


def connect(client, row):
    return client.websocket_connect(f"/api/conversations/{row['id']}/terminal")


def ready(socket, cols=80, rows=24):
    socket.send_json({"type": "resize", "cols": cols, "rows": rows})
    event = socket.receive_json()
    assert event["type"] == "ready", event
    return event


def command(socket, text):
    socket.send_json({"type": "input", "data": text + "\r"})


def until(socket, marker):
    # Search after the prompt's echoed command, not in the command itself.
    output = b""
    for _ in range(100):
        message = socket.receive()
        assert message.get("bytes") is not None, message
        output += message["bytes"]
        if marker in output:
            return output
    raise AssertionError(output)


def replayed(socket):
    """A kept shell opened again: what it showed, then that it's all there (its size may change from then on)."""
    output = b""
    while True:
        message = socket.receive()
        if message.get("bytes") is not None:
            output += message["bytes"]
            continue
        assert json.loads(message["text"]) == {"type": "replayed"}, message
        return output


def end(socket):
    """End shell: a chat's shell is kept when its page goes; this ends it."""
    socket.send_json({"type": "end"})
    while True:
        message = socket.receive()
        assert message["type"] != "websocket.close", message  # (nothing more comes: don't wait for ever)
        if message.get("text"):
            event = json.loads(message["text"])
            assert event["type"] == "exit" and event.get("ended"), event
            return event


def wait_closed(relay, containers, count=1):
    deadline = time.monotonic() + 5
    while (relay.terminals or len(containers.closed) < count) and time.monotonic() < deadline:
        time.sleep(.01)
    assert not relay.terminals
    assert len(containers.closed) == count


def test_shell_pty_resize_ctrl_c_unicode_and_exit(env):
    client, relay, row, containers = env
    with connect(client, row) as socket:
        event = ready(socket, cols=91, rows=31)
        assert event["directory"] == row["directory"]
        command(socket, "printf '\\nDIR=%s\\n' \"$PWD\"; stty size; printf 'caf\\303\\251\\n'")
        output = until(socket, b"caf\xc3\xa9\r\n")
        assert f"DIR={row['directory']}\r\n".encode() in output
        assert b"31 91\r\n" in output
        assert b"cannot set terminal process group" not in output
        socket.send_json({"type": "resize", "cols": 65, "rows": 17})
        command(socket, "stty size")
        until(socket, b"17 65\r\n")
        command(socket, "sleep 30")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            found = False
            for proc in Path("/proc").glob("[0-9]*"):
                try:
                    if (proc / "cmdline").read_bytes() == b"sleep\x0030\x00" \
                            and f"RELAY_TERMINAL={containers.started[-1][1]}".encode() in (proc / "environ").read_bytes().split(b"\0"):
                        found = True
                        break
                except OSError:
                    pass
            if found:
                break
            time.sleep(.01)
        assert found, "wait for the foreground command before interrupting it"
        socket.send_json({"type": "input", "data": "\x03"})
        command(socket, "printf 'alive\\n'")
        until(socket, b"alive\r\n")
        command(socket, "exit 7")
        while True:
            message = socket.receive()
            if message.get("text"):
                assert json.loads(message["text"]) == {"type": "exit", "code": 7}
                break
    wait_closed(relay, containers)
    assert client.get(f"/api/conversations/{row['id']}").json()["messages"] == []


def test_disaster_guard_skips_stubbed_command_and_keeps_shell_usable(env):
    client, relay, row, containers = env
    with connect(client, row) as socket:
        ready(socket)
        # rm is a harmless function: never pass a destructive command to a real remover in tests.
        command(socket, "rm() { printf 'STUB_RM_EXECUTED\\n'; }")
        command(socket, "rm -rf /")
        output = until(socket, b"Relay blocked this command:")
        command(socket, "printf 'still-alive\\n'")
        output += until(socket, b"still-alive\r\n")
        assert b"STUB_RM_EXECUTED\r\n" not in output
        command(socket, "touch allowed.txt; rm allowed.txt; printf 'allowed-done\\n'")
        output = until(socket, b"allowed-done\r\n")
        assert b"STUB_RM_EXECUTED\r\n" in output
        end(socket)
    wait_closed(relay, containers)


def test_a_chats_shell_is_kept_when_its_page_goes_and_opened_again(env):
    """Closing the panel (or the page) leaves the chat's shell running, its output kept; opening it again is the same
    shell, with what it showed. End shell ends it, and the next one is new."""
    client, relay, row, containers = env
    with connect(client, row) as socket:
        assert ready(socket)["resumed"] is False
        command(socket, "export KEPT=still-here; printf 'before-%s\\n' leaving")
        until(socket, b"before-leaving\r\n")
    time.sleep(.2)
    assert len(relay.terminals) == 1 and not containers.closed  # (it goes on)
    with connect(client, row) as socket:
        event = ready(socket, cols=70, rows=20)
        assert event["resumed"] is True and event["container"] == "test-work-container"
        assert event["size"] == [80, 24]  # (what it showed was written at that size: the page writes it so)
        assert b"before-leaving\r\n" in replayed(socket)  # (what it showed, first)
        command(socket, "stty size")
        until(socket, b"24 80\r\n")  # (its size stays until the page asks for its own)
        socket.send_json({"type": "resize", "cols": 70, "rows": 20})
        command(socket, "printf 'kept=%s\\n' \"$KEPT\"; stty size")
        output = until(socket, b"20 70\r\n")
        assert b"kept=still-here\r\n" in output
        assert end(socket)["ended"] is True
    wait_closed(relay, containers)
    assert len(containers.started) == 1
    with connect(client, row) as socket:
        assert ready(socket)["resumed"] is False
        command(socket, "printf 'kept=%s\\n' \"${KEPT:-gone}\"")
        until(socket, b"kept=gone\r\n")
        command(socket, "exit 3")  # (its own exit: the next one is new too)
        while not (message := socket.receive()).get("text"):
            pass
        assert json.loads(message["text"]) == {"type": "exit", "code": 3}
    wait_closed(relay, containers, 2)
    assert containers.started[0][1] != containers.started[1][1]


def test_a_kept_shell_goes_to_the_page_that_opens_it_last(env):
    client, relay, row, containers = env
    with connect(client, row) as first:
        ready(first)
        with connect(client, row) as second:
            assert ready(second)["resumed"] is True
            replayed(second)
            message = first.receive_json()
            assert message == {"type": "detached", "message": "This shell is open in another window now"}
            command(second, "printf 'second-has-it\\n'")
            until(second, b"second-has-it\r\n")
        time.sleep(.2)
        assert len(relay.terminals) == 1  # (the second page going doesn't end it either)
    with connect(client, row) as socket:
        assert ready(socket)["resumed"] is True
        replayed(socket)
        end(socket)
    wait_closed(relay, containers)
    assert len(containers.started) == 1


def test_a_kept_shell_left_too_long_ends_and_so_do_others_with_relay(env):
    client, relay, row, containers = env
    relay.shells.keep_for = .3
    with connect(client, row) as socket:
        ready(socket)
    wait_closed(relay, containers)  # (nobody opened it again)
    relay.shells.keep_for = 3600
    other = client.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    for conversation in (row, other):
        with connect(client, conversation) as socket:
            ready(socket)
    time.sleep(.2)
    assert len(relay.terminals) == 2
    client.portal.call(relay.shells.close_all)  # (what Relay does when it stops)
    wait_closed(relay, containers, 3)


def test_deleting_a_chat_or_discarding_its_task_ends_its_kept_shell(env):
    client, relay, row, containers = env
    with connect(client, row) as socket:
        ready(socket)
    client.post(f"/api/conversations/{row['id']}/discard", headers=H)
    wait_closed(relay, containers)
    chat = client.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    with connect(client, chat) as socket:
        ready(socket)
    client.delete(f"/api/conversations/{chat['id']}", headers=H).raise_for_status()
    wait_closed(relay, containers, 2)


@pytest.mark.parametrize("kind", ["unsigned", "other-user", "origin", "discarded", "remote", "missing", "bad-size", "bad-input", "podman"])
def test_shell_errors_do_not_open_an_unrelated_or_host_shell(env, kind, monkeypatch):
    client, relay, row, containers = env
    if kind == "unsigned":
        client.cookies.clear()
    elif kind == "other-user":
        user = relay.accounts.create_user("other", "correct horse")
        relay.db.execute("UPDATE conversations SET user_id = ? WHERE id = ?", (user, row["id"]))
    elif kind == "discarded":
        relay.db.execute("UPDATE conversations SET task_state = 'discarded' WHERE id = ?", (row["id"],))
    elif kind == "remote":
        relay.db.execute("UPDATE conversations SET computer_id = 'remote' WHERE id = ?", (row["id"],))
    elif kind == "missing":
        Path(row["directory"]).rmdir()
    elif kind == "podman":
        def fail(*args):
            raise ContainerError("Podman is unavailable")
        monkeypatch.setattr(containers, "terminal_argv", fail)
    path = f"/api/conversations/{row['id']}/terminal"
    headers = {"origin": "https://another-host"} if kind == "origin" else {}
    if kind in ("unsigned", "origin"):
        with client.websocket_connect(path, headers=headers) as socket:  # (only to say why)
            event = socket.receive_json()
            assert event["type"] == "error"
            assert ("isn't at an address Relay knows" if kind == "origin" else "not signed in") in event["message"]
            with pytest.raises(WebSocketDisconnect) as error:
                socket.receive_json()
        assert error.value.code == 4401
    else:
        with client.websocket_connect(path, headers=headers) as socket:
            if kind == "bad-input":
                ready(socket)
                socket.send_json(["not a message"])
            else:
                socket.send_json({"cols": 0 if kind == "bad-size" else 80, "rows": 24})
            message = socket.receive()
            while message.get("bytes") is not None:
                message = socket.receive()
            event = json.loads(message["text"])
            assert event["type"] == "error", event
            if kind == "podman":
                assert event["message"] == "Podman is unavailable"
    if kind != "bad-input":
        assert not containers.started
    wait_closed(relay, containers, int(kind == "bad-input"))


def test_a_shell_behind_a_reverse_proxy_that_ends_https(env):
    """The page is https://…, Relay's own connection plain: the origin's host counts (Host, X-Forwarded-Host or
    Relay's address), not its scheme; another site's page is still refused."""
    client, relay, row, containers = env
    path = f"/api/conversations/{row['id']}/terminal"
    for headers in ({"origin": "https://relay.example", "host": "relay.example"},
                    {"origin": "https://relay.example", "host": "192.168.1.5:8420", "x-forwarded-host": "relay.example"},
                    {"origin": "https://relay.example:443", "host": "relay.example"}):
        with client.websocket_connect(path, headers=headers) as socket:
            ready(socket)
            end(socket)
    relay.settings.public_url = "https://relay.example/"
    with client.websocket_connect(path, headers={"origin": "https://relay.example", "host": "192.168.1.5:8420"}) as socket:
        ready(socket)
        end(socket)
    with client.websocket_connect(path, headers={"origin": "https://evil.example", "host": "192.168.1.5:8420",
                                                 "x-forwarded-host": "relay.example"}) as socket:
        assert "evil.example" in socket.receive_json()["message"]
    wait_closed(relay, containers, 4)


def test_loginless_chat_shell_creates_only_its_scratch_folder(env):
    client, relay, _, containers = env
    relay.settings.auth = "none"
    user = relay.accounts.local_user()
    row = client.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    assert not Path(row["directory"]).exists()
    with connect(client, row) as socket:
        ready(socket)
        command(socket, "printf 'chat-folder=%s\\n' \"$PWD\"")
        until(socket, f"chat-folder={row['directory']}\r\n".encode())
        end(socket)
    wait_closed(relay, containers)
    assert relay.db.one("SELECT user_id FROM conversations WHERE id = ?", (row["id"],))["user_id"] == user["id"]


def test_failed_start_releases_pty_and_container_hold(tmp_path, monkeypatch):
    directory = tmp_path / "project"
    directory.mkdir()
    containers = PtyContainers(tmp_path / "home")
    session = Terminal(containers, directory)
    def fail(*args, **kwargs):
        raise OSError("No process slots")
    monkeypatch.setattr("relay.terminal.subprocess.Popen", fail)
    with pytest.raises(OSError, match="No process slots"):
        session.start(80, 24)
    fd = session.fd
    session.close()
    session.close()
    assert len(containers.closed) == 1
    with pytest.raises(OSError):
        os.fstat(fd)


def test_cancelled_connection_waits_for_startup_then_closes_the_shell(env, monkeypatch):
    client, relay, row, containers = env
    original = containers.terminal_argv
    started, release = threading.Event(), threading.Event()
    def delayed(*args):
        started.set()
        assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(containers, "terminal_argv", delayed)
    timer = threading.Timer(.2, release.set)
    try:
        with connect(client, row) as socket:
            socket.send_json({"cols": 80, "rows": 24})
            assert started.wait(5)
            timer.start()
        wait_closed(relay, containers)
    finally:
        release.set()
        timer.cancel()


def test_a_services_shell_opens_in_its_live_box_and_only_for_its_owner(env, monkeypatch, tmp_path):
    client, relay, _, containers = env
    user = relay.db.one("SELECT id FROM users")["id"]
    folder = tmp_path / "live-version"
    folder.mkdir()
    relay.db.execute("INSERT INTO services (id, user_id, name, command, public_port, created) "
                     "VALUES ('a1b2c3d4e5f6', ?, 'site', 'x', 20999, 0)", (user,))

    def shell_error():
        with client.websocket_connect("/api/services/a1b2c3d4e5f6/terminal") as socket:
            socket.send_json({"type": "resize", "cols": 80, "rows": 24})
            event = socket.receive_json()
            assert event["type"] == "error", event
            return event["message"]
    assert shell_error() == "Nothing of site is live"
    other = relay.accounts.create_user("other", "another horse")
    relay.db.execute("UPDATE services SET user_id = ? WHERE id = 'a1b2c3d4e5f6'", (other,))
    assert shell_error() == "No service a1b2c3d4e5f6"  # (another user's)
    relay.db.execute("UPDATE services SET user_id = ? WHERE id = 'a1b2c3d4e5f6'", (user,))
    monkeypatch.setattr(relay.production, "shell", lambda service: (containers, folder))
    with client.websocket_connect("/api/services/a1b2c3d4e5f6/terminal") as socket:
        assert ready(socket)["directory"] == str(folder)
        command(socket, "printf 'in=%s\\n' \"$PWD\"")
        until(socket, f"in={folder}\r\n".encode())
    wait_closed(relay, containers)


def test_a_production_shell_that_cant_be_prepared_says_why(env, monkeypatch):
    from relay.production import ProductionError
    client, relay, _, containers = env
    user = relay.db.one("SELECT id FROM users")["id"]
    relay.db.execute("INSERT INTO services (id, user_id, name, command, public_port, created) "
                     "VALUES ('b1b2c3d4e5f6', ?, 'site', 'x', 20998, 0)", (user,))

    class Unprepared:
        def name(self, directory):
            return "relay-prod-b1b2c3d4-1"

        def terminal_argv(self, directory, tag):
            raise ProductionError("Couldn't prepare a shell in the box: no such container")
    monkeypatch.setattr(relay.production, "shell", lambda service: (Unprepared(), Path("/")))
    with client.websocket_connect("/api/services/b1b2c3d4e5f6/terminal") as socket:
        socket.send_json({"type": "resize", "cols": 80, "rows": 24})
        assert socket.receive_json() == {"type": "error", "message": "Couldn't prepare a shell in the box: no such container"}
    assert not relay.terminals


def test_a_production_shell_brings_its_guard_into_the_box(monkeypatch):
    from relay.production import SHELL_FILES, Shell
    calls = []
    monkeypatch.setattr("relay.production.subprocess.run",
                        lambda argv, **kwargs: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    argv = Shell("relay-prod-a1b2c3d4-3").terminal_argv(Path("/srv/site/3/site"), "tag-one")
    assert calls[0] == ["podman", "exec", "relay-prod-a1b2c3d4-3", "mkdir", "-p", SHELL_FILES]
    assert [c[:2] for c in calls[1:]] == [["podman", "cp"]] * 2
    assert {c[3] for c in calls[1:]} == {f"relay-prod-a1b2c3d4-3:{SHELL_FILES}/terminal-guard.py",
                                         f"relay-prod-a1b2c3d4-3:{SHELL_FILES}/terminal.bash"}
    assert argv[:3] == ["podman", "exec", "-it"] and argv[argv.index("-w") + 1] == "/srv/site/3/site"
    assert f"RELAY_TERMINAL_GUARD={SHELL_FILES}/terminal-guard.py" in argv and "RELAY_TERMINAL=tag-one" in argv
    assert argv[-4:] == ["bash", "--rcfile", f"{SHELL_FILES}/terminal.bash", "-i"]


def test_terminal_style_nonce_is_per_page_and_the_rest_of_csp_stays_strict(env):
    client, _, _, _ = env
    first, second = client.get("/"), client.get("/tasks")
    nonce = re.search(r'name="relay-style-nonce" content="([^"]+)"', first.text)[1]
    other = re.search(r'name="relay-style-nonce" content="([^"]+)"', second.text)[1]
    assert nonce != other and "@STYLE_NONCE@" not in first.text
    csp = first.headers["content-security-policy"]
    assert f"style-src 'self' 'nonce-{nonce}'" in csp
    assert "script-src 'self'" in csp and "'unsafe-inline'" not in csp


def test_container_shell_uses_the_same_container_and_stays_warm(tmp_path, podman, monkeypatch):
    directory = tmp_path / "project"
    directory.mkdir()
    containers = Containers(state=tmp_path / "containers")
    agent = containers.exec_argv(directory)
    argv = containers.terminal_argv(directory, "tag-one")
    name = containers.name(directory)
    assert name == agent[-1] and name in argv
    assert argv[:3] == ["podman", "exec", "-it"]
    assert argv[argv.index("-w") + 1] == str(directory)
    assert "RELAY_TERMINAL=tag-one" in argv
    containers.terminal_argv(directory, "tag-two")
    original = subprocess.run
    def listed(argv, **kwargs):
        if argv[:2] == ["podman", "ps"]:
            return subprocess.CompletedProcess(argv, 0, stdout=f"{name} running 0|{instance()}\n")
        return original(argv, **kwargs)
    monkeypatch.setattr("relay.containers.subprocess.run", listed)
    monkeypatch.setattr(containers, "_last_used", lambda _: 0)
    assert containers.stop_idle(0) == 0
    containers.release_terminal(name)
    assert containers.terminals[name] == 1
    containers.release_terminal(name)
    assert name not in containers.terminals
    assert containers.stop_idle(0) == 1


@pytest.mark.parametrize("data", [{}, {"cols": 1, "rows": 20}, {"cols": 80, "rows": 0},
                                  {"cols": 1001, "rows": 20}, {"cols": None, "rows": 20}])
def test_invalid_terminal_dimensions(data):
    with pytest.raises(ValueError):
        dimensions(data)


def test_cancelled_pty_read_does_not_leave_an_event_loop_reader(tmp_path):
    async def run():
        containers = PtyContainers(tmp_path / "home")
        directory = tmp_path / "project"
        directory.mkdir()
        session = Terminal(containers, directory)
        session.fd, slave = pty.openpty()
        os.set_blocking(session.fd, False)
        task = asyncio.create_task(session.read())
        await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not asyncio.get_running_loop().remove_reader(session.fd)
        os.close(slave)
        session.close()
    asyncio.run(run())


def test_a_new_providers_shell_and_an_added_ones_are_in_a_box_with_its_home(env, podman, tmp_path, monkeypatch):
    """Adding a provider with an agent (relay.creations): the shell where the user installs it and logs in is in its
    setup box, in its home; an added provider's (to log in again) in a box with its home, which goes with the shell."""
    from types import SimpleNamespace
    client, relay, _, _ = env
    monkeypatch.setattr("relay.production.subprocess.run",  # (the guard and rcfile copied into the box)
                        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "", ""))
    removed = []
    relay.boxes = SimpleNamespace(setup=lambda key, home=None, mounts=(), env=None: f"relay-setup-{key}",
                                  remove_setup=removed.append, setup_name=lambda key: f"relay-setup-{key}")

    def opened():  # (its shell's podman exec, once it ran)
        deadline = time.monotonic() + 5
        while not (found := [c for c in podman() if c[:2] == ["exec", "-it"]]) and time.monotonic() < deadline:
            time.sleep(0.05)
        return found

    made = client.post("/api/provider-creations", json={}, headers=H).json()
    with client.websocket_connect(f"/api/provider-creations/{made['id']}/terminal") as socket:
        ready(socket)
        shells = opened()
        end(socket)
    assert f"relay-setup-new-{made['id']}" in shells[-1] and shells[-1][shells[-1].index("-w") + 1] == made["home"]
    home = tmp_path / "toy-home"
    home.mkdir()
    relay.registry.add({"id": "toy", "name": "Toy", "kind": "example", "home": str(home)})
    with client.websocket_connect("/api/providers/toy/terminal") as socket:
        ready(socket)
        deadline = time.monotonic() + 5
        while len(shells := opened()) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        end(socket)
    assert "relay-setup-home-toy" in shells[-1] and shells[-1][shells[-1].index("-w") + 1] == str(home)
    deadline = time.monotonic() + 5
    while not removed and time.monotonic() < deadline:
        time.sleep(0.05)
    assert removed == ["home-toy"]  # (its box went with its shell)
    with client.websocket_connect("/api/providers/fake/terminal") as socket:
        socket.send_json({"cols": 80, "rows": 24})
        event = socket.receive_json()
        assert event["type"] == "error" and "doesn't run in a box" in event["message"], event


def test_a_shell_to_sign_in_again_has_the_machines_program_and_login(env, podman, tmp_path, monkeypatch):
    """A provider with a box of its own: its Shell in Settings → Providers, and on its repair's page, opens in that
    box, in a real shell with what its program needs, in the machine's HOME, and the box stays when the shell goes."""
    from types import SimpleNamespace
    client, relay, _, _ = env
    monkeypatch.setattr("relay.production.subprocess.run",  # (the guard and rcfile copied into the box)
                        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "", ""))
    made, removed, boxes = [], [], []

    def setup(key, home=None, mounts=(), env=None):
        made.append((key, home, list(mounts), env))
        return f"relay-setup-{key}"

    def ensure(key, mounts, folder=None, busy=False, image=None, home=None):
        boxes.append((key, list(mounts), busy, image))
        return f"relay-box-{key}"

    dropped = []
    relay.boxes = SimpleNamespace(setup=setup, remove_setup=removed.append, ensure=ensure, remove=dropped.append,
                                  name=lambda key: f"relay-box-{key}", setup_name=lambda key: f"relay-setup-{key}")

    def shell_in(path, kept=True):  # (its shell's podman exec, once it ran)
        before = len([c for c in podman() if c[:2] == ["exec", "-it"]])
        with client.websocket_connect(path) as socket:
            ready(socket)
            deadline = time.monotonic() + 5
            while len(found := [c for c in podman() if c[:2] == ["exec", "-it"]]) <= before \
                    and time.monotonic() < deadline:
                time.sleep(0.05)
            if kept:  # (one that isn't ends with its page)
                end(socket)
        return found[-1]

    login = tmp_path / "claude-login"
    login.mkdir()
    relay.registry.add({"id": "cc", "name": "Claude", "kind": "claude-code", "options": {"config_dir": str(login)}})
    listed = {p["id"]: p for p in client.get("/api/providers", headers=H).json()["providers"]}
    assert listed["cc"]["sign_in"] == "claude auth login" and "sign_in" not in listed["fake"]
    assert listed["cc"]["shell"] is True and listed["fake"]["shell"] is False  # (a box, or none)
    shell = shell_in("/api/providers/cc/terminal")
    assert shell[shell.index("relay-box-cc") + 1] == "/usr/lib/relay/bash.real"
    assert shell[shell.index("-w") + 1] == str(Path.home())
    assert f"CLAUDE_CONFIG_DIR={login}" in shell and any(flag.startswith("PATH=") for flag in shell)
    [(key, mounts, busy, image)] = boxes
    assert key == "cc" and (login, True) in mounts and busy and image is None
    deadline = time.monotonic() + 5
    while relay.terminals and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not relay.terminals and not removed and not made  # (no box of its own, and its own box stays)

    # What its program attaches to comes first, and a provider without a box has no such shell
    said = []
    toy = SimpleNamespace(box_key=lambda: "toy", box_mounts=list, box_image=lambda: "workbox",
                          box_shell=lambda: "bash", env=lambda: {"A": "1"},
                          shell_ready=lambda: said.append(list(boxes)), template=SimpleNamespace(name="Toy"))
    before = list(boxes)
    terminal = relay.creations._in_its_box(toy)
    assert said == [before] and boxes[-1][0] == "toy" and boxes[-1][3] == "workbox"
    argv = terminal.containers.terminal_argv(Path.home(), "tag")
    assert argv[argv.index("relay-box-toy") + 1] == "bash" and "A=1" in argv
    boxes.pop()
    with client.websocket_connect("/api/providers/fake/terminal") as socket:
        socket.send_json({"cols": 80, "rows": 24})
        event = socket.receive_json()
        assert event["type"] == "error" and "doesn't run in a box" in event["message"], event

    # Not added yet (Add provider's next step): that shell in the box its template makes, which goes with it
    def gone(count):
        deadline = time.monotonic() + 5
        while (relay.terminals or len(dropped) < count) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not relay.terminals

    other = tmp_path / "other-login"
    other.mkdir()
    draft = {"id": "cc2", "name": "Mine", "kind": "claude-code", "options": {"config_dir": str(other)}}
    boxes.clear()
    path = f"/api/provider-kinds/claude-code/terminal?template={quote(json.dumps(draft))}"
    shell = shell_in(path, kept=False)
    assert shell[shell.index("relay-box-cc2") + 1] == "/usr/lib/relay/bash.real"
    assert shell[shell.index("-w") + 1] == str(Path.home()) and f"CLAUDE_CONFIG_DIR={other}" in shell
    [(key, mounts, busy, image)] = boxes
    assert key == "cc2" and (other, True) in mounts and not busy and not made  # (its JSON's, in no setup box)
    gone(1)
    assert dropped == ["cc2"]
    # (added while it was open: the box is that provider's now, and a box an added provider has isn't remade)
    relay.registry.add(draft)
    boxes.clear()
    shell_in(path, kept=False)
    gone(1)
    assert dropped == ["cc2"] and boxes[0][2] is True
    relay.registry.remove("cc2")
    # (another shell still open in it)
    toy = SimpleNamespace(box_key=lambda: "toy")
    relay.terminals.add(type("Open", (), {"container": "relay-box-toy", "tag": "open"}))
    relay.creations.drop_unused_box(toy, "leaving")
    assert dropped == ["cc2"]
    relay.creations.drop_unused_box(toy, "open")
    assert dropped == ["cc2", "toy"]
    relay.terminals.clear()
    # (of the page's kind, with a box of its own, as JSON)
    command = {"id": "x", "name": "X", "kind": "command", "options": {"command": ["true"]}}
    for kind, template, why in (("claude-code", {"id": "cc", "name": "Mine", "kind": "copilot"}, "not a template of a new"),
                                ("command", command, "doesn't run in a box"), ("claude-code", "not json{", "Expecting"),
                                ("claude-code", {**draft, "home": str(tmp_path)}, "not a template of a new")):
        text = template if isinstance(template, str) else json.dumps(template)
        with client.websocket_connect(f"/api/provider-kinds/{kind}/terminal?template={quote(text)}") as socket:
            socket.send_json({"cols": 80, "rows": 24})
            event = socket.receive_json()
            assert event["type"] == "error" and why in event["message"], event

    repair = client.post("/api/provider-repairs", json={"provider_id": "cc"}, headers=H).json()
    assert repair["provider"]["sign_in"] == "claude auth login" and not repair.get("home")
    assert repair["provider"]["shell"] is True and repair["setup_box"] is None
    before = len(boxes)
    shell = shell_in(f"/api/provider-repairs/{repair['id']}/terminal")
    assert "relay-box-cc" in shell and shell[shell.index("-w") + 1] == str(Path.home())
    assert len(boxes) > before and boxes[-1][0] == "cc"
    assert client.post(f"/api/provider-repairs/{repair['id']}/discard", json={}, headers=H).status_code == 200

