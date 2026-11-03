"""ProductionBoxes (relay.production) with real podman: a tiny web service behind Relay's proxy,
promoted, broken, rolled back, cloned. Skipped without podman or the WorkBox image."""

import json
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from relay import config
from relay.app import create_app
from relay.containers import Containers, built_image
from relay.db import Database
from relay.providers import Registry

APP = '''\
import json, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
VERSION = "{version}"
COUNTER = os.path.join(os.environ["DATA_DIR"], "counter")
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass
    def reply(self, text):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(text.encode())
    def do_GET(self):
        if self.path == "/health":
            return self.reply("ok")
        count = open(COUNTER).read() if os.path.exists(COUNTER) else "0"
        self.reply(f"version {{VERSION}}, count {{count}}")
    def do_POST(self):
        count = int(open(COUNTER).read()) if os.path.exists(COUNTER) else 0
        with open(COUNTER, "w") as out:
            out.write(str(count + 1))
        self.reply("counted")
{start}
ThreadingHTTPServer(("127.0.0.1", int(os.environ["PORT"])), Handler).serve_forever()
'''


def podman_ready():
    if not shutil.which("podman"):
        return False
    found = subprocess.run(["podman", "image", "exists", built_image()], capture_output=True)
    return found.returncode == 0


pytestmark = pytest.mark.skipif(not podman_ready(), reason="needs podman and the WorkBox image")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def get(port, method="GET"):
    request = urllib.request.Request(f"http://127.0.0.1:{port}/", method=method, data=b"" if method == "POST" else None)
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.read().decode()


def write_app(folder, version, start=""):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "app.py").write_text(APP.format(version=version, start=start))
    return folder


@pytest.fixture
def relay(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_HOME", str(tmp_path / "data"))
    settings = config.Settings(host="127.0.0.1", workspace_roots=[str(tmp_path / "ws")],
                               chat_root=str(tmp_path / "chats"))
    registry = Registry(tmp_path / "providers")
    registry.ensure_defaults()
    registry.add({"id": "fake", "name": "Fake", "kind": "command", "options": {"command": [sys.executable, "-c", ""]}})
    app = create_app(settings, Database(tmp_path / "relay.db"), registry)
    with TestClient(app) as client:
        relay = app.state.relay
        relay.containers = Containers()  # real ones, for the ProductionBoxes
        relay.db.execute("INSERT INTO users (id, username, password_hash, created) VALUES (1, 'me', 'x', 0)")
        relay.client = client
        yield relay
        for service in relay.db.all("SELECT * FROM services"):
            client.portal.call(relay.production.delete, service)


def test_a_service_lives_behind_relay_and_changes_without_moving(relay, tmp_path):
    production, call = relay.production, relay.client.portal.call
    port = free_port()
    service = call(production.create, 1, "demo", write_app(tmp_path / "ws" / "demo", "1"), "python3 app.py", port,
                   "/health")
    assert service["live"]["number"] == 1, service["versions"]
    assert get(port) == "version 1, count 0"
    assert get(port, "POST") == "counted"
    row = production.get(1, "demo")

    # A new version takes over at the same address; the data stays. The old box stops at once
    # (an init passes podman's stop on), not after podman's timeout.
    message = call(production.promote, row, write_app(tmp_path / "work" / "demo", "2"))
    assert "Version 2 of demo is live" in message and "version 1 is archived" in message
    ended = subprocess.run(["podman", "inspect", "--format", "{{.State.Status}} {{.State.ExitCode}}",
                            f"relay-prod-{row['id'][:8]}-1"], capture_output=True, text=True).stdout.split()
    assert ended == ["exited", "143"], f"the old box wasn't ended by its stop signal (137: killed after the timeout): {ended}"
    assert get(port) == "version 2, count 1"
    # One that doesn't come up well doesn't take over; one that crashes (and is started again
    # and again) is seen at once, not after START_TIMEOUT
    broken = write_app(tmp_path / "broken" / "demo", "3", start="raise SystemExit('oops')")
    with pytest.raises(Exception, match=r"didn't come up well \(it stopped"):  # (not "… after 90 s")
        call(production.promote, production.get(1, "demo"), broken)
    assert get(port) == "version 2, count 1"
    statuses = {v["number"]: v["status"] for v in production.services(1)[0]["versions"]}
    assert statuses == {1: "archived", 2: "live", 3: "failed"}
    # Back to the archived one
    assert "Version 1 of demo is live" in call(production.rollback, production.get(1, "demo"))
    assert get(port) == "version 1, count 1"

    # Health: a live version that stops is noticed, once
    live = production.services(1)[0]["live"]
    subprocess.run(["podman", "stop", "-t", "1", f"relay-prod-{row['id'][:8]}-{live['number']}"], capture_output=True)
    # (a clock ahead of the app's own scheduler, whose ticks then skip the check)
    ahead = production.checked = time.time() + 10**6
    production.failed.clear()
    assert call(production.check, ahead + 60) == [] and not production.services(1)[0]["health_ok"]  # (once: starting?)
    failing = call(production.check, ahead + 120)
    assert [s["name"] for s in failing] == ["demo"] and not production.services(1)[0]["health_ok"]
    assert "Restarted version 1: well" in call(production.restart, production.get(1, "demo"))

    # A clone: its live code and a snapshot of its data, as a managed code task (the Governor's)
    clone = call(production.clone, 1, production.get(1, "demo"), "fake", None, None, "run-test")
    folder = Path(clone["directory"])
    assert clone["managed"] and (tmp_path / "data").resolve() in folder.resolve().parents
    assert 'VERSION = "1"' in (folder / "app.py").read_text() and (folder / ".data" / "counter").read_text() == "1"

    # Promoted as the Governor promotes: a fresh clone of the live version, with where it came from
    write_app(folder, "4")
    (folder / ".data" / "counter").write_text("999")  # the clone's copy of the data: never production's
    message = call(production.promote_clone, production.get(1, "demo"), clone, "governor:test")
    assert "Version 4 of demo is live" in message
    assert get(port) == "version 4, count 1"
    v4 = relay.db.one("SELECT * FROM service_versions WHERE number = 4")
    assert (v4["origin_conversation"], v4["from_version"], v4["promoted_by"], v4["replaced_version"]) == (
        clone["id"], 1, "governor:test", 1)
    assert json.loads(v4["validation"])["ok"] and json.loads(v4["agents"]) == []  # (nobody worked in it yet)
    # It goes on from version 4 now; one the live version moved on from can't (it would undo that)
    assert relay.db.one("SELECT v.number FROM service_clones k JOIN service_versions v ON v.id = k.from_version "
                        "WHERE k.conversation_id = ?", (clone["id"],))["number"] == 4
    first = relay.db.one("SELECT id FROM service_versions WHERE number = 1")["id"]
    relay.db.execute("UPDATE service_clones SET from_version = ? WHERE conversation_id = ?", (first, clone["id"]))
    with pytest.raises(Exception, match="made from version 1, but version 4 is live"):
        call(production.promote_clone, production.get(1, "demo"), clone, "governor:test")
    events = [e["kind"] for e in relay.agentos.events(1)]
    assert "version.live" in events and "version.failed" in events and "workbox.cloned" in events


def test_one_operation_at_a_time(relay, tmp_path):
    import asyncio

    from relay.production import ProductionError
    production, call = relay.production, relay.client.portal.call
    port = free_port()
    call(production.create, 1, "solo", write_app(tmp_path / "ws" / "solo", "1"), "python3 app.py", port, "/health")
    row = production.get(1, "solo")

    async def both():
        return await asyncio.gather(production.promote(row, write_app(tmp_path / "a" / "solo", "2")),
                                    production.promote(row, write_app(tmp_path / "b" / "solo", "3")),
                                    return_exceptions=True)
    results = call(both)
    busy = [r for r in results if isinstance(r, ProductionError)]
    assert len(busy) == 1 and "busy (promote going on)" in str(busy[0]), results
    live = [v for v in production.services(1)[0]["versions"] if v["status"] == "live"]
    assert len(live) == 1 and get(port) == f"version {live[0]['number']}, count 0"
    assert relay.db.one("SELECT operation FROM services WHERE id = ?", (row["id"],))["operation"] is None


def test_archived_versions_go_after_their_days(relay, tmp_path):
    production, call = relay.production, relay.client.portal.call
    port = free_port()
    call(production.create, 1, "keep", write_app(tmp_path / "ws" / "keep", "1"), "python3 app.py", port, None)
    call(production.promote, production.get(1, "keep"), write_app(tmp_path / "w" / "keep", "2"))
    old = relay.db.one("SELECT * FROM service_versions WHERE number = 1")
    assert old["status"] == "archived" and Path(old["folder"]).is_dir()
    production.tidy(time.time() + 8 * 86400)
    assert relay.db.one("SELECT * FROM service_versions WHERE number = 1") is None
    assert not Path(old["folder"]).exists()
    assert get(port) == "version 2, count 0"


def test_another_relays_boxes_are_left_alone(relay):
    """podman is shared with other Relays (the user's own, while tests or the UI walkthrough run):
    reconciling never touches a box of a service this Relay doesn't know."""
    from relay.production import PROD_LABEL
    name = "relay-prod-feedface-1"
    subprocess.run(["podman", "rm", "-f", "-t", "1", name], capture_output=True)
    subprocess.run(["podman", "run", "-d", "--network=host", "--name", name, "--label",
                    f"{PROD_LABEL}=feedface{'0' * 24}", built_image(), "sleep", "600"], check=True, capture_output=True)
    try:
        relay.production.reconcile()
        state = subprocess.run(["podman", "inspect", "--format", "{{.State.Status}}", name],
                               capture_output=True, text=True)
        assert state.returncode == 0 and state.stdout.strip() == "running"
    finally:
        subprocess.run(["podman", "rm", "-f", "-t", "1", name], capture_output=True)


def test_a_shell_in_the_live_box_has_the_guard_and_leaves_the_service_up(relay, tmp_path):
    import asyncio

    from relay.terminal import Terminal
    production, call = relay.production, relay.client.portal.call
    port = free_port()
    call(production.create, 1, "shelly", write_app(tmp_path / "ws" / "shelly", "1"), "python3 app.py", port, "/health")
    box, folder = production.shell(production.get(1, "shelly"))
    session = Terminal(box, folder)

    async def talk():
        output = b""

        async def until(marker):
            nonlocal output
            while marker not in output:
                output += await asyncio.wait_for(session.read(), 20)
        await session.write("printf 'at=%s\\n' \"$PWD\"\r")
        await until(f"at={folder}\r\n".encode())
        await session.write("rm -rf /\r")  # the WorkBox shell's guard, in this box too
        await until(b"blocked")
        await session.write("printf 'still%s\\n' here\r")  # (and the shell goes on)
        await until(b"stillhere")
    try:
        session.start(100, 30)
        asyncio.run(talk())
    finally:
        session.close()
    assert get(port) == "version 1, count 0"  # (closing the shell ends only the shell)
    # Containers in it too, as in a WorkBox: podman there, their images in a volume of the service's
    service = production.get(1, "shelly")
    mounts = subprocess.run(["podman", "inspect", "--format", "{{range .Mounts}}{{.Name}}:{{.Destination}} {{end}}",
                             box.box], capture_output=True, text=True).stdout
    assert f"relay-containers-{service['id'][:12]}:/var/lib/containers" in mounts
    assert subprocess.run(["podman", "exec", box.box, "podman", "--version"], capture_output=True).returncode == 0
    from relay.production import ProductionError
    subprocess.run(["podman", "stop", "-t", "1", box.box], capture_output=True)
    with pytest.raises(ProductionError, match="isn't running"):
        production.shell(production.get(1, "shelly"))


def test_a_box_that_lost_its_conmon_is_made_anew_at_the_start(relay, tmp_path):
    """Under systemd an older Relay's restart could kill the conmons of the boxes it started: the
    box runs on, but its output has nowhere to go (a print fails in it). The next start sees that."""
    import os
    import signal
    production, call = relay.production, relay.client.portal.call
    port = free_port()
    call(production.create, 1, "orphan", write_app(tmp_path / "ws" / "orphan", "1"), "python3 app.py", port, "/health")
    box = f"relay-prod-{production.get(1, 'orphan')['id'][:8]}-1"
    before = subprocess.run(["podman", "inspect", "--format", "{{.Id}} {{.State.ConmonPid}} {{.State.Status}}", box],
                            capture_output=True, text=True).stdout.split()
    # Only a real conmon of this test's own running box: os.kill(0, …) would end this whole test run
    assert len(before) == 3 and before[2] == "running" and int(before[1]) > 1, before
    assert b"conmon" in Path(f"/proc/{before[1]}/cmdline").read_bytes()
    os.kill(int(before[1]), signal.SIGKILL)
    call(production.start)
    after = subprocess.run(["podman", "inspect", "--format", "{{.State.Status}} {{.State.ConmonPid}}", box],
                           capture_output=True, text=True).stdout.split()
    assert after[0] == "running" and after[1] != before[1] and Path(f"/proc/{after[1]}").exists(), after
    for _ in range(50):
        try:
            assert get(port) == "version 1, count 0"
            break
        except OSError:
            time.sleep(.2)
    else:
        raise AssertionError("it didn't come back")


def test_status_tells_what_runs_in_the_box(relay, tmp_path):
    from relay.production import describe_status
    production, call = relay.production, relay.client.portal.call
    port = free_port()
    call(production.create, 1, "seen", write_app(tmp_path / "ws" / "seen", "1"), "python3 app.py", port, "/health")
    found = production.status(production.get(1, "seen"))
    box = found["box"]
    assert box["status"] == "running" and box["init"] and box["restarts"] == 0 and box["conmon_alive"], box
    assert any("python3 app.py" in line for line in found["processes"]), found["processes"]
    assert found["probe"] == {"ok": True, "detail": "HTTP 200"}
    assert "never restarted by podman" in describe_status(found) and "crashed" not in describe_status(found)
    subprocess.run(["podman", "stop", "-t", "5", box["name"]], capture_output=True)
    found = production.status(production.get(1, "seen"))
    assert found["box"]["status"] == "exited" and found["box"]["exit_code"] == 143 and not found["processes"]
    assert not found["probe"]["ok"]
    text = describe_status(found)
    assert "exited" in text and "last exit code 143" in text and "Checked just now: NO ANSWER" in text


def test_its_data_is_copied_daily_and_put_back(relay, tmp_path, monkeypatch):
    """A copy of its data once a day (after 04:00), the newest kept; one put back makes the data as it was,
    the data before kept first, so putting that back undoes it. Found for real: kort's broken database had
    nothing to go back to but the copy made before its last version."""
    from relay import production as production_module
    from relay import tunables
    production, call = relay.production, relay.client.portal.call
    monkeypatch.setitem(tunables._values, "services.daily_snapshots", 2)
    port = free_port()
    call(production.create, 1, "keeper", write_app(tmp_path / "ws" / "keeper", "1"), "python3 app.py", port, "/health")
    get(port, "POST")
    service = production.get(1, "keeper")
    folder = production_module.folders() / service["id"] / "snapshots"
    night = time.mktime(time.strptime("2026-10-07 03:30", "%Y-%m-%d %H:%M"))
    assert not production.daily_due(night)  # (before 04:00: not yet)
    morning = night + 3600
    assert production.daily_due(morning)
    folder.mkdir(parents=True, exist_ok=True)
    for old in ("daily-2026-10-05", "daily-2026-10-06"):  # (older ones: two are kept)
        (folder / f"{old}.tar").write_bytes(b"")
    call(production.daily_snapshots, morning)
    assert sorted(p.name for p in folder.glob("daily-*.tar")) == ["daily-2026-10-06.tar", "daily-2026-10-07.tar"]
    assert not production.daily_due(morning + 60) and get(port) == "version 1, count 1"  # (paused, not stopped)
    get(port, "POST")
    assert get(port) == "version 1, count 2"
    # Put back: as it was that morning; the data before is a snapshot of its own, to undo it. Undone in the same
    # second (a script or an agent can): found for real, that kept the data over the one it put back
    stamp, strftime = time.strftime("%Y%m%d-%H%M%S"), time.strftime
    monkeypatch.setattr(time, "strftime", lambda form, *at: stamp if form == "%Y%m%d-%H%M%S" else strftime(form, *at))
    said = call(production.restore, production.get(1, "keeper"), "daily-2026-10-07")
    assert "as it was in daily-2026-10-07" in said and "It's well" in said, said
    assert get(port) == "version 1, count 1"
    kept = next(s for s in production.snapshots(service) if s["kind"] == "restore")
    call(production.restore, production.get(1, "keeper"), kept["name"])
    assert get(port) == "version 1, count 2"
    assert sorted(s["name"] for s in production.snapshots(service) if s["kind"] == "restore") == [
        f"before-restore-{stamp}", f"before-restore-{stamp}-2"]
    with pytest.raises(production_module.ProductionError, match="no snapshot 'nope'"):
        call(production.restore, production.get(1, "keeper"), "nope")
    events = [e["kind"] for e in relay.agentos.events(1)]
    assert events.count("data.restored") == 2
