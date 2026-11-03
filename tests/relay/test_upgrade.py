"""An upgrade from Relay 1.0.0: what it left (data/: its database, made by 1.0.0 through its own API, and its
settings.json) opens with this Relay, is brought up to date, and works as before."""

import json
import sqlite3
from pathlib import Path

from starlette.testclient import TestClient

from relay import config
from relay.app import create_app
from relay.db import Database
from relay.providers import Registry

H = {"X-Relay-Request": "1"}
DATA = Path(__file__).parent / "data"


def test_what_relay_1_0_0_left_opens_and_works(tmp_path, monkeypatch):
    raw = sqlite3.connect(tmp_path / "relay.db")
    raw.executescript((DATA / "relay-1.0.0.sql").read_text().replace("@HOME@", str(tmp_path)))
    raw.close()
    (tmp_path / "ws" / "site").mkdir(parents=True)
    # Its settings.json: what's new has its default, what was set stays
    old = tmp_path / "settings.json"
    old.write_text((DATA / "settings-1.0.0.json").read_text().replace("@HOME@", str(tmp_path)))
    settings = config.load_settings(old)
    assert settings.notify_min_seconds == 45 and settings.notify_vpn and settings.notify_categories == {}
    assert settings.workspace_roots == [str(tmp_path / "ws")]
    registry = Registry(tmp_path / "providers")
    monkeypatch.setattr(registry, "ensure_defaults", lambda: None)
    registry.add({"id": "fake", "name": "Fake agent", "kind": "command", "options": {"command": ["true"], "models": ["m"]}})
    app = create_app(settings, Database(tmp_path / "relay.db"), registry)
    with TestClient(app) as client:
        assert client.post("/api/login", json={"username": "me", "password": "correct horse battery"},
                           headers=H).status_code == 200
        listed = {c["title"]: c for c in client.get("/api/conversations").json()}
        assert set(listed) == {"A chat from 1.0", "A code task from 1.0"} and listed["A chat from 1.0"]["pinned"]
        assert not any(c["managed"] for c in listed.values())  # (the user's own, not AgentOS's)
        chat = client.get(f"/api/conversations/{listed['A chat from 1.0']['id']}").json()
        assert [m["content"] for m in chat["messages"]] == ["hello from 1.0"]
        # A finished turn of 1.0's: its run, its events, its answer and its usage; a message still queued
        task = listed["A code task from 1.0"]
        assert task["last_run"]["status"] == "succeeded"
        detail = client.get(f"/api/conversations/{task['id']}").json()
        assert [m["content"] for m in detail["messages"]] == ["list the files", "Done from 1.0."]
        assert "and then the tests" in json.dumps(detail)
        events = client.get("/api/runs/run10/events").text
        assert '"name": "shell"' in events and "Done from 1.0." in events
        # Its preferences: the fallback loop, the subagents' policy, the computer
        assert [loop["name"] for loop in client.get("/api/fallback-loops").json()] == ["My loop"]
        assert {"provider": "fake", "model": "m"} in client.get("/api/subagents").json()["allowed"]
        assert [c["name"] for c in client.get("/api/computers").json()] == ["laptop"]
        # Scheduled tasks became AgentOS's tasks: the user's own, a prompt for the agent they chose
        task = next(t for t in client.get("/api/schedules").json() if t["name"] == "Morning summary")
        assert task["kind"] == "manual" and not task["governed"] and task["provider_id"] == "fake"
        assert "Reviewer" in [m["name"] for m in client.get("/api/modes").json()]
        for path in ("/api/settings", "/api/limits", "/api/agentos", "/api/memory", "/api/services", "/api/tasks",
                     "/api/fallback-loops", "/api/subagents", "/api/system"):
            assert client.get(path).status_code == 200, path
        # What's new works on it: a memory, a new chat
        assert client.post("/api/memory", json={"text": "Upgraded from 1.0"}, headers=H).status_code == 200
        made = client.post("/api/conversations", json={"provider_id": "fake", "title": "New in 2.0"}, headers=H)
        assert made.status_code == 200 and len(client.get("/api/conversations").json()) == 3
    # Opened again: nothing more to do, nothing lost
    Database(tmp_path / "relay.db")
    assert sqlite3.connect(tmp_path / "relay.db").execute("SELECT count(*) FROM conversations").fetchone()[0] == 3
