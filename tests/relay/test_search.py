"""Relay's web search (relay.search): SearXNG started and kept by Relay, on this machine's
connection or a VPN tunnel's, and the tool every agent gets."""

import json
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from types import SimpleNamespace

import pytest
import uvicorn
from starlette.testclient import TestClient

from relay import config, search, webbox
from relay.app import create_app
from relay.db import Database
from relay.providers import Registry
from relay.toolbox import Toolbox
from relay.vpn import VpnError

H = {"x-relay-request": "1"}
ANSWER = {
    "query": "q", "answers": [{"answer": "42"}], "infoboxes": [], "corrections": [],
    "results": [{"title": "SearXNG docs", "url": "https://docs.searxng.org/", "content": "A  metasearch\n engine.",
                 "engines": ["google cse", "brave"], "publishedDate": "2026-09-01T10:00:00"},
                {"title": "", "url": "https://example.com/", "content": "", "engines": []}],
    "suggestions": ["searxng api"], "unresponsive_engines": [["duckduckgo", "CAPTCHA"]],
}


class Podman:
    """podman as relay.search uses it: containers by name (state, spec, id, start, argv), and SearXNG's
    answers from inside one (the searches asked, kept)."""

    def __init__(self):
        self.boxes: dict[str, dict] = {}
        self.calls: list[list[str]] = []
        self.asked: list[dict] = []
        self.clock = 1000
        self.slow = False  # (SearXNG doesn't answer a search in time)

    def tick(self):
        self.clock += 10
        return self.clock

    def run(self, argv, **kwargs):
        if argv[0] != "podman":
            return subprocess.CompletedProcess(argv, 0, "", "")
        args = argv[1:]
        self.calls.append(args)
        done = lambda out="", code=0: subprocess.CompletedProcess(argv, code, out, "")  # noqa: E731
        if args[:2] == ["container", "inspect"]:
            box = self.boxes.get(args[-1])
            if box is None:
                return done(code=125)
            if "StartedAt.Unix" in args[3]:
                return done(str(box["started"]))
            return done(f"{box['state']} {box['spec']}")
        if args[:2] == ["run", "-d"]:
            name = args[args.index("--name") + 1]
            spec = next(a.split("=", 2)[2] for a in args if a.startswith("--label=io.relay.spec="))
            self.boxes[name] = {"id": f"{name}-id{self.tick()}", "state": "running", "spec": spec, "started": self.clock,
                                "argv": args}
            return done()
        if args[0] in ("start", "restart"):
            self.boxes[args[-1]].update(state="running", started=self.tick())
            return done()
        if args[0] == "stop":
            self.boxes[args[-1]]["state"] = "exited"
            return done()
        if args[0] == "rm":
            return done(code=0 if self.boxes.pop(args[-1], None) else 1)
        if args[0] == "exec":
            url = urllib.parse.urlparse(args[-1])
            if self.boxes.get(args[1], {}).get("state") != "running":
                return done(code=4)
            if url.path == "/healthz":
                return done("OK")
            self.asked.append({k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()})
            self.waited = int(args[args.index("-T") + 1])
            return done(code=4) if self.slow else done(json.dumps(ANSWER))
        if args[0] == "logs":
            return done("searxng: started")
        return done()


class Tunnels:
    """relay.vpn as relay.search uses it: a tunnel that's up (its holder's id) or doesn't connect."""

    def __init__(self, podman):
        self.podman = podman
        self.up = True
        self.tunnels = [{"id": "t1", "name": "Private"}]

    def tunnel(self, tunnel_id):
        return next((t for t in self.tunnels if t["id"] == tunnel_id), None)

    def names(self, tunnel):
        return "relay-vpnnet-t1", "relay-vpn-t1"

    def ensure(self, tunnel):
        if not self.up:
            raise VpnError("Windscribe · Amsterdam: no server found")
        holder = self.podman.boxes.setdefault("relay-vpnnet-t1", {"id": "holder-1", "state": "running", "spec": "x",
                                                                  "started": self.podman.tick(), "argv": []})
        return holder["id"]


@pytest.fixture
def podman(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_HOME", str(tmp_path / "data"))
    fake = Podman()
    monkeypatch.setattr(subprocess, "run", fake.run)
    monkeypatch.setattr(webbox, "time", SimpleNamespace(time=time.time, monotonic=time.monotonic, sleep=lambda s: None))
    return fake


def make(podman, **found):
    settings = config.Settings()
    settings.search = found
    return search.Search(settings, Tunnels(podman))


def test_a_search_starts_searxng_and_reads_as_text(podman):
    found = make(podman)
    text = found.text({"query": "searxng", "category": "news", "time_range": "week", "language": "nl", "page": 2})
    assert text.splitlines()[0] == "Web search (SearXNG) for 'searxng', page 2:"
    assert "Answer: 42" in text
    assert "1. SearXNG docs\n   https://docs.searxng.org/\n   A metasearch engine. (google cse, brave; 2026-09-01)" in text
    assert "2. https://example.com/\n   https://example.com/" in text
    assert "Also try: searxng api" in text and "(Didn't answer: duckduckgo (CAPTCHA))" in text
    assert podman.asked[-1] == {"q": "searxng", "format": "json", "categories": "news", "pageno": "2",
                                "time_range": "week", "language": "nl", "timeout_limit": "8"}
    assert podman.waited == 10  # (search.timeout: the engines get 8 s, so SearXNG answers within 10)
    # Something it doesn't know is left out; nothing to search for is said
    found.text({"query": "x", "category": "images", "time_range": "decade", "page": "many"})
    assert podman.asked[-1] == {"q": "x", "format": "json", "categories": "general", "pageno": "1",
                                "timeout_limit": "8"}
    with pytest.raises(search.SearchError, match="What to search for"):
        found.text({"query": " "})
    # Its container: its own network, listening on 127.0.0.1 only, its settings read-only
    argv = podman.boxes[found.name()]["argv"]
    assert not any(a.startswith("--network") for a in argv)
    assert "GRANIAN_HOST=127.0.0.1" in argv and f"GRANIAN_PORT={search.Search.PORT}" in argv and "FORCE_OWNERSHIP=false" in argv
    settings_file = config.data_dir() / "searxng" / "settings.yml"
    assert f"{settings_file}:/etc/searxng/settings.yml:ro" in argv
    text = settings_file.read_text()
    assert "formats: [html, json]" in text and "limiter: false" in text and oct(settings_file.stat().st_mode)[-3:] == "600"
    secret = (config.data_dir() / "searxng" / "secret").read_text()
    assert secret in text and len(secret) >= 32
    found._settings_file()
    assert (config.data_dir() / "searxng" / "secret").read_text() == secret  # (kept)


def test_a_search_waits_as_long_as_the_setting_says(podman, monkeypatch):
    """search.timeout (Settings → Limits): SearXNG answers within it with the engines that did; one that
    doesn't answer in time is said, with where to change it."""
    from relay import tunables
    monkeypatch.setitem(tunables._values, "search.timeout", 5)
    found = make(podman)
    found.text({"query": "x"})
    assert podman.asked[-1]["timeout_limit"] == "3" and podman.waited == 5
    podman.slow = True
    with pytest.raises(search.SearchError, match=r"didn't answer the search within 5 s \(Settings → Limits → Web search\)"):
        found.text({"query": "x"})


def test_through_a_tunnel_and_never_around_it(podman):
    found = make(podman, tunnel="t1")
    found.text({"query": "a"})
    argv = podman.boxes[found.name()]["argv"]
    assert "--network=container:holder-1" in argv
    # The tunnel's network started anew (after a reconnect, a restart): so does SearXNG in it
    podman.boxes["relay-vpnnet-t1"]["started"] = podman.tick()
    found.text({"query": "b"})
    assert ["restart", "-t", "1", found.name()] in podman.calls
    # The tunnel doesn't connect: nothing searched, not through this machine's own connection either
    found.vpn.up = False
    asked = len(podman.asked)
    with pytest.raises(search.SearchError, match="through the VPN tunnel Private, which isn't up: Windscribe"):
        found.text({"query": "c"})
    assert len(podman.asked) == asked
    found.vpn.tunnels = []
    with pytest.raises(search.SearchError, match="a VPN tunnel that's gone"):
        found.text({"query": "d"})
    # Another route: made anew
    found.settings.search = {"tunnel": ""}
    found.text({"query": "e"})
    assert not any(a.startswith("--network") for a in podman.boxes[found.name()]["argv"])


def test_unused_it_stops_and_the_next_search_starts_it(podman):
    found = make(podman)
    found.text({"query": "a"})
    now = time.time()
    found.tick(now + 60)
    assert podman.boxes[found.name()]["state"] == "running"
    found.tick(now + webbox.IDLE_AFTER + 1)
    assert podman.boxes[found.name()]["state"] == "exited" and not found.status()["running"]
    found.text({"query": "b"})
    assert podman.boxes[found.name()]["state"] == "running" and ["start", found.name()] in podman.calls
    assert make(podman).config() == {"enabled": True, "tunnel": ""}
    assert not make(podman, enabled=False).enabled()


def test_chat_models_get_web_search(tmp_path):
    asked = []

    def search_for(arguments):
        asked.append(arguments)
        if arguments["query"] == "down":
            raise search.SearchError("SearXNG didn't answer")
        return "1. Found"

    box = Toolbox(tmp_path, code=False, search=search_for)
    assert "web_search" in [d["function"]["name"] for d in box.definitions()]
    assert box.call("web_search", {"query": "relay", "category": "news"}) == (True, "1. Found")
    assert asked == [{"query": "relay", "category": "news"}]
    assert box.call("web_search", {"query": "down"}) == (False, "SearXNG didn't answer")
    assert "web_search" not in [d["function"]["name"] for d in Toolbox(tmp_path, code=True).definitions()]


SLEEPER = ("import json, time\nprint(json.dumps({'type': 'session', 'session': 's1'}), flush=True)\n"
           "time.sleep(60)\nprint(json.dumps({'type': 'done', 'status': 'succeeded'}), flush=True)\n")


def test_agents_with_a_shell_search_and_fetch_with_relay(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    settings = config.Settings(host="127.0.0.1", port=port, auth="none", workspace_roots=[str(tmp_path)],
                               chat_root=str(tmp_path / "chats"))
    registry = Registry(tmp_path / "providers")
    registry.add({"id": "sleeper", "name": "Sleeper", "kind": "command",
                  "options": {"command": [sys.executable, "-c", SLEEPER]}})
    app = create_app(settings, Database(tmp_path / "relay.db"), registry)
    relay = app.state.relay
    asked = []
    monkeypatch.setattr(relay.search, "text", lambda arguments: asked.append(arguments) or "1. Found it")
    monkeypatch.setattr(relay.fetch, "text", lambda arguments: asked.append(arguments) or "A page")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"
    try:
        with TestClient(app, base_url=base) as client:
            conv = client.post("/api/conversations", json={"provider_id": "sleeper"}, headers=H).json()
            run_id = client.post(f"/api/conversations/{conv['id']}/messages", json={"text": "go"}, headers=H).json()["run_id"]
            script = relay.delegation.script_for(relay.db.one("SELECT * FROM conversations WHERE id = ?", (conv["id"],)), base)
            done = subprocess.run([str(script), "search", "--category", "news", "--time", "week", "relay", "searxng"],
                                  capture_output=True, text=True, timeout=30)
            assert done.returncode == 0 and done.stdout.strip() == "1. Found it", done.stderr
            assert asked == [{"query": "relay searxng", "category": "news", "time_range": "week", "language": None,
                              "page": None}]
            done = subprocess.run([str(script), "fetch", "--offset", "20000", "https://a.example/"], capture_output=True,
                                  text=True, timeout=30)
            assert done.returncode == 0 and done.stdout.strip() == "A page" and asked[-1] == {
                "url": "https://a.example/", "offset": 20000}
            told = subprocess.run([str(script), "help"], capture_output=True, text=True).stdout
            assert "search QUERY" in told and "fetch URL" in told
            client.post(f"/api/runs/{run_id}/stop", headers=H)
    finally:
        server.should_exit = True
        thread.join(10)


def test_the_search_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_HOME", str(tmp_path / "data"))
    settings = config.Settings(workspace_roots=[str(tmp_path)], chat_root=str(tmp_path / "chats"))
    settings.vpn = {"profiles": [{"id": "p1", "name": "W", "env": {"VPN_SERVICE_PROVIDER": "windscribe"}}],
                    "tunnels": [{"id": "t1", "name": "Private", "folders": [], "exits": [{"profile": "p1", "place": {}}]}]}
    registry = Registry(tmp_path / "providers")
    registry.ensure_defaults()
    app = create_app(settings, Database(tmp_path / "relay.db"), registry)
    relay = app.state.relay
    stopped = []
    monkeypatch.setattr(relay.search, "stop", lambda: stopped.append(True))
    monkeypatch.setattr(relay.search, "running", lambda: False)
    with TestClient(app) as client:
        assert client.post("/api/setup", json={"username": "me", "password": "correct horse"}, headers=H).status_code == 200
        found = client.get("/api/search").json()
        assert found["enabled"] and found["tunnel"] == "" and found["tunnels"] == [{"id": "t1", "name": "Private"}]
        assert client.put("/api/search", json={"tunnel": "nope"}, headers=H).status_code == 400
        found = client.put("/api/search", json={"tunnel": "t1"}, headers=H).json()
        assert found["tunnel"] == "t1" and found["status"]["route"] == "Private" and stopped == [True]
        assert config.load_settings().search == {"enabled": True, "tunnel": "t1"}
        # Its tunnel stays while it goes through it
        assert client.delete("/api/vpn/tunnels/t1", headers=H).status_code == 409
        assert not client.put("/api/search", json={"enabled": False}, headers=H).json()["enabled"]
        # A search to try: what an agent gets, and why not
        monkeypatch.setattr(relay.search, "query", lambda q: ANSWER)
        tried = client.post("/api/search/try", json={"query": "searxng"}, headers=H).json()
        assert tried["results"][0]["url"] == "https://docs.searxng.org/" and tried["unresponsive"] == ["duckduckgo (CAPTCHA)"]
        assert "1. SearXNG docs" in tried["text"]
        monkeypatch.setattr(relay.search, "query", lambda q: (_ for _ in ()).throw(search.SearchError("no podman")))
        assert client.post("/api/search/try", json={"query": "x"}, headers=H).status_code == 409
        assert client.post("/api/search/try", json={"query": " "}, headers=H).status_code == 400
