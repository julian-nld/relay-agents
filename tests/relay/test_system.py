"""Settings → System (relay.system): what Relay runs, what each box is for and what it takes, and
what may be done with it."""

import json
import subprocess

import pytest
from starlette.testclient import TestClient

from relay import config, system
from relay.app import create_app
from relay.containers import instance
from relay.db import Database
from relay.providers import Registry

H = {"x-relay-request": "1"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_HOME", str(tmp_path / "data"))
    (tmp_path / "ws" / "site").mkdir(parents=True)
    settings = config.Settings(workspace_roots=[str(tmp_path / "ws")], chat_root=str(tmp_path / "chats"))
    registry = Registry(tmp_path / "providers")
    registry.ensure_defaults()
    app = create_app(settings, Database(tmp_path / "relay.db"), registry)
    with TestClient(app) as client:
        assert client.post("/api/setup", json={"username": "me", "password": "correct horse"}, headers=H).status_code == 200
        client.tmp = tmp_path
        yield client


def podman_world(relay, tmp_path, monkeypatch):
    """podman as relay.system asks it: these containers, their stats, folders and images."""
    mark = instance()
    suffix = f"-{mark}" if mark else ""
    site = str(tmp_path / "ws" / "site")
    user = relay.db.one("SELECT id FROM users")["id"]
    relay.db.execute("INSERT INTO conversations (id, user_id, title, provider_id, mode, directory, created, updated) "
                     "VALUES ('c1', ?, 'Build the site', 'fake', 'code', ?, 1, 2)", (user, site))
    chat = str(config.data_dir() / "chats" / "c2")
    relay.db.execute("INSERT INTO conversations (id, user_id, title, provider_id, mode, directory, created, updated) "
                     "VALUES ('c2', ?, 'A question', 'fake', 'chat', ?, 1, 2)", (user, chat))
    relay.db.execute("INSERT INTO services (id, user_id, name, command, public_port, created) VALUES ('svc1', ?, 'notes', 'x', 8500, 1)",
                     (user,))
    managed = {"io.relay.managed": "1", "io.relay.instance": mark}
    containers = [
        ("relay-box-claude-code" + suffix, "running", {**managed, "io.relay.box": "1"}, "/"),
        ("relay-aaaaaaaaaaaa" + suffix, "running", managed, site),
        ("relay-bbbbbbbbbbbb" + suffix, "exited", managed, chat),
        ("relay-prod-svc1aaaa-3", "running", {"io.relay.production": "svc1"}, "/srv"),
        ("relay-prod-other000-1", "running", {"io.relay.production": "other"}, "/srv"),  # (another Relay's service)
        ("relay-vpnnet-t1" + suffix, "running", {**managed, "io.relay.vpn": "t1"}, "/"),
        ("relay-search" + suffix, "running", {**managed, "io.relay.search": ""}, "/"),
        ("relay-cccccccccccc-feedface", "running", {**managed, "io.relay.instance": "feedface"}, "/x"),  # (another Relay's)
    ]
    listed = [{"Names": [n], "State": s, "Labels": labels, "Image": "localhost/relay-agent:x", "ImageID": "img-agent",
               "StartedAt": 1000, "ExitedAt": 2000} for n, s, labels, _ in containers]
    stats = [{"name": n, "cpu_percent": "1.5%", "mem_usage": "155.6MB / 10GB"} for n, s, _, _ in containers if s == "running"]
    images = [{"Id": "img-agent", "Names": ["localhost/relay-agent:x"], "Size": 3_000_000_000, "Containers": 6},
              {"Id": "img-old", "Names": ["localhost/relay-agent:old"], "Size": 2_900_000_000, "Containers": 0},
              {"Id": "img-gluetun", "Names": ["docker.io/qmcgaw/gluetun:latest"], "Size": 50_000_000, "Containers": 0}]
    calls = []
    real = subprocess.run

    def run(argv, **kwargs):
        if argv[0] != "podman":
            return real(argv, **kwargs)
        calls.append(argv[1:])
        out = ""
        if argv[1:3] == ["ps", "-a"]:
            out = json.dumps(listed)
        elif argv[1] == "stats":
            out = json.dumps(stats)
        elif argv[1:3] == ["container", "inspect"] and "{{.Name}}|{{.Config.WorkingDir}}" in argv:
            out = "\n".join(f"{n}|{f}" for n, _, _, f in containers if n in argv)
        elif argv[1:3] == ["container", "inspect"]:
            found = next((labels for n, _, labels, _ in containers if n == argv[-1]), {})
            out = json.dumps(found)
        elif argv[1:3] == ["system", "df"]:
            out = json.dumps([{"Type": "Images", "TotalCount": 3, "RawSize": 5_950_000_000, "RawReclaimable": 2_950_000_000}])
        elif argv[1] == "images":
            out = json.dumps(images)
        return subprocess.CompletedProcess(argv, 0, out, "")
    monkeypatch.setattr(subprocess, "run", run)
    relay.containers.image = "localhost/relay-agent:x"
    return calls, site


def test_what_each_box_is_for_and_what_it_takes(env, monkeypatch):
    relay = env.app.state.relay
    calls, site = podman_world(relay, env.tmp, monkeypatch)
    boxes = {b["name"].removesuffix(f"-{instance()}" if instance() else ""): b for b in system.boxes(relay)}
    assert "relay-cccccccccccc-feedface" not in boxes and "relay-prod-other000-1" not in boxes  # (another Relay's)
    provider = boxes["relay-box-claude-code"]
    assert provider["kind"] == "provider" and provider["title"] == "Claude Code" and provider["actions"] == ["stop"]
    assert provider["cpu"] == 1.5 and provider["memory"] == 155_600_000
    work = boxes["relay-aaaaaaaaaaaa"]
    assert work["kind"] == "workbox" and work["title"] == "Build the site" and work["link"] == {"conversation": "c1"}
    assert work["detail"] == site and work["actions"] == ["stop"]
    chat = boxes["relay-bbbbbbbbbbbb"]
    assert chat["title"] == "Chat: A question" and chat["state"] == "exited" and chat["actions"] == ["remove"]
    production = boxes["relay-prod-svc1aaaa-3"]
    assert production["title"] == "notes, version 3" and production["actions"] == [] and production["link"] == {"service": "svc1"}
    assert boxes["relay-vpnnet-t1"]["kind"] == "vpn" and boxes["relay-search"]["title"] == "Web search (SearXNG)"
    # An agent at work in a WorkBox: not stopped from here
    monkeypatch.setattr(relay.runs, "busy_folders", lambda: {env.tmp / "ws" / "site"})
    work = next(b for b in system.boxes(relay) if b["name"].startswith("relay-aaaa"))
    assert work["actions"] == [] and work["why"] == "an agent works there"
    # Kinds in order: providers first, the web's last
    assert [b["kind"] for b in system.boxes(relay)][:1] == ["provider"]


def test_stopping_and_removing_from_settings(env, monkeypatch):
    relay = env.app.state.relay
    calls, site = podman_world(relay, env.tmp, monkeypatch)
    done = []
    monkeypatch.setattr(relay.containers, "stop_box", lambda name: done.append(("stop", name)), raising=False)
    monkeypatch.setattr(relay.containers, "discard", lambda folder: done.append(("discard", str(folder))), raising=False)
    work = next(b["name"] for b in system.boxes(relay) if b["name"].startswith("relay-aaaa"))
    stopped_chat = next(b["name"] for b in system.boxes(relay) if b["name"].startswith("relay-bbbb"))
    production = "relay-prod-svc1aaaa-3"
    overview = env.post(f"/api/system/boxes/{work}/stop", headers=H).json()
    assert done == [("stop", work)] and "machine" in overview and overview["machine"]["cores"] >= 1
    # A running WorkBox isn't removed; a ProductionBox is its service's; a stopped WorkBox goes
    assert env.post(f"/api/system/boxes/{work}/remove", headers=H).status_code == 409
    refused = env.post(f"/api/system/boxes/{production}/stop", headers=H)
    assert refused.status_code == 409 and "can't be stopped" in refused.json()["error"]
    env.post(f"/api/system/boxes/{stopped_chat}/remove", headers=H)
    assert done[-1] == ("discard", str(config.data_dir() / "chats" / "c2"))
    assert env.post("/api/system/workboxes/remove-stopped", headers=H).json()["removed"] == 1
    assert env.post("/api/system/boxes/relay-nope/stop", headers=H).status_code == 409
    # The provider's box: not while a run of it is going
    provider = next(b["name"] for b in system.boxes(relay) if b["kind"] == "provider")
    relay.runs.active["r1"] = type("Run", (), {"box_key": "claude-code", "directory": None, "finished": False})()
    try:
        assert "a run of it is going" in env.post(f"/api/system/boxes/{provider}/stop", headers=H).json()["error"]
    finally:
        relay.runs.active.pop("r1")


def test_storage_and_images_nothing_uses(env, monkeypatch):
    relay = env.app.state.relay
    calls, _ = podman_world(relay, env.tmp, monkeypatch)
    found = env.get("/api/system/storage", headers=H).json()
    assert found["totals"]["images"] == {"count": 3, "size": 5_950_000_000, "reclaimable": 2_950_000_000}
    images = {i["id"]: i for i in found["images"]}
    assert images["img-agent"]["whose"].startswith("Relay's WorkBoxes") and not images["img-agent"]["removable"]
    assert images["img-old"]["whose"] == "an older build of Relay's" and images["img-old"]["removable"]
    assert images["img-gluetun"]["whose"] == "Relay's VPN tunnels"
    assert env.post("/api/system/images/img-agent/remove", headers=H).status_code == 409  # (in use)
    env.post("/api/system/images/img-old/remove", headers=H)
    assert ["rmi", "img-old"] in calls


def test_byte_counts_as_podman_writes_them():
    assert system._bytes("155.6kB ") == 155_600 and system._bytes("10.43GB") == 10_430_000_000
    assert system._bytes("3MiB") == 3 * 1024**2 and system._bytes("") is None
    assert system._percent("1.94%") == 1.9 and system._percent("--") is None
