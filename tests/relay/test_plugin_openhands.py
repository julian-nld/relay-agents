"""plugins/openhands: OpenHands' Agent Canvas in a container of Relay's, with Relay's agents. podman and the Agent
Server's API are this test's own (no image is ever pulled or run here), and so is the plugin's Relay API."""

import importlib.util
import pathlib
import shlex
import shutil
import subprocess
import sys
import urllib.error

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
import relay_sdk  # noqa: E402

_spec = importlib.util.spec_from_file_location("relay_plugin_tests", pathlib.Path(__file__).with_name("test_plugins.py"))
_helpers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_helpers)
H, home, relay_app, signed_in, until = _helpers.H, _helpers.home, _helpers.relay_app, _helpers.signed_in, _helpers.until


class Podman:
    """podman as the plugin calls it: a container of its own, images, and what it was asked."""

    def __init__(self):
        self.calls, self.state, self.images, self.version = [], None, set(), "1.27.0"

    def __call__(self, *args, timeout=60):
        self.calls.append(args)
        out, code = "", 0
        if args[:2] == ("ps", "-a"):
            out = self.state or ""
        elif args[:2] == ("image", "exists"):
            code = 0 if args[2] in self.images else 1
        elif args[0] == "pull":
            self.images.add(args[1])
        elif args[0] == "run":
            self.state = "running"
        elif args[0] == "rm":
            self.state = None
        elif args[0] == "stop":
            self.state = "exited"
        elif args[0] == "logs":
            out = "agent-canvas: started"
        elif args[:2] == ("image", "inspect"):
            out = self.version
        elif args[0] == "tag":
            self.images.add(args[2])
        return subprocess.CompletedProcess(["podman", *args], code, out, "")

    def runs(self):
        return [c for c in self.calls if c[0] == "run"]


class AgentServer:
    """OpenHands' Agent Server as the plugin calls it: its settings, its agent profiles, its apps, and what it was
    asked."""

    def __init__(self):
        self.asked, self.agent, self.apps = [], {"agent_kind": "openhands"}, {}
        self.profiles, self.active = {}, None
        self.conversations = [
            {"id": "oh1", "title": "Fix the header", "execution_status": "running",
             "agent_state": {"acp_session_id": "r1"}},
            {"id": "oh2", "title": "Its own agent", "execution_status": "idle", "agent_state": {}}]

    def __call__(self, port, key, method, path, body=None, timeout=30):
        self.asked.append((port, key, method, path, body))
        if (method, path) == ("GET", "/api/settings"):
            return {"agent_settings": dict(self.agent)}
        if (method, path) == ("PATCH", "/api/settings"):
            self.agent.update(body["agent_settings_diff"])
        elif path == "/api/canvas-extensions/installed":
            return {"canvas_extensions": list(self.apps.values())}
        elif path == "/api/canvas-extensions/install":
            self.apps["relay"] = {"name": "relay", "enabled": False, "source": body["source"]}
            return self.apps["relay"]
        elif path == "/api/agent-profiles":
            if not self.profiles:  # (made once from the settings, by the first look: Canvas' own, as a rule)
                command = self.agent.get("acp_command")
                self.save("default", {"agent_kind": self.agent["agent_kind"], "acp_server": self.agent.get("acp_server"),
                                      "acp_command": shlex.join(command) if command else None,
                                      "acp_model": self.agent.get("acp_model")})
                self.active = self.profiles["default"]["id"]
            return {"profiles": [{"id": p["id"], "name": p["name"], "agent_kind": p["agent_kind"]}
                                 for p in self.profiles.values()], "active_agent_profile_id": self.active}
        elif path.startswith("/api/agent-profiles/") and path.endswith("/activate"):
            self.active = path.split("/")[3]
        elif path.startswith("/api/agent-profiles/"):
            name = path.split("/")[3]
            if method == "GET":
                return {"name": name, "profile": dict(self.profiles[name])}
            self.save(name, body)
        elif path.startswith("/api/conversations/search"):
            return {"items": self.conversations, "next_page_id": None}
        elif path == "/api/canvas-extensions/installed/relay":
            self.apps["relay"]["enabled"] = body["enabled"]
        return {"status": "ok"}

    def save(self, name, profile):
        """A profile saved under its name: a namesake's id kept and its revision on, else a new one."""
        old = self.profiles.get(name)
        self.profiles[name] = {**profile, "name": name, "id": old["id"] if old else f"id-{name}",
                               "revision": old["revision"] + 1 if old else 0}


@pytest.fixture
def openhands(home, tmp_path, monkeypatch):
    """Relay with the plugin linked, its podman, its Agent Server and its Relay API this test's: (client, plugin's
    module, podman, Agent Server, the workspace root)."""
    root = tmp_path / "projects"
    for name in ("site", "notes", ".hidden", "scripts/private"):
        (root / name).mkdir(parents=True)
    (home / "plugins").mkdir(parents=True)
    (home / "plugins" / "openhands").symlink_to(ROOT / "plugins" / "openhands")

    def relay_api(self, method, path, *args, **kwargs):
        if path == "/api/settings":
            return {"workspace_roots": [str(root)]}
        if path == "/api/scripts":
            return {"folder": str(root / "scripts")}
        raise AssertionError(f"a real call of Relay's API: {method} {path}")

    monkeypatch.setattr(relay_sdk.Session, "request", relay_api)
    podman, server = Podman(), AgentServer()
    with relay_app(tmp_path) as client:
        signed_in(client)
        module = sys.modules["relay_plugins.openhands"]
        monkeypatch.setattr(module, "podman", podman)
        monkeypatch.setattr(module, "canvas", server)
        monkeypatch.setattr(module, "latest_version", lambda: "1.27.0")  # (never the real registry)
        yield client, module, podman, server, root


def settled(client):
    assert until(lambda: client.get("/api/plugins/openhands/status").json()["doing"] is None, 10)
    return client.get("/api/plugins/openhands/status").json()


def test_openhands_is_set_up_and_works_with_relays_agents(openhands):
    client, module, podman, server, root = openhands
    asked = server.asked
    now = client.get("/api/plugins/openhands/status").json()
    assert not now["set_up"] and not now["has_password"] and now["state"] is None and now["port"] == 8000
    assert client.app.state.relay.accounts.app_passwords(1) == []  # (looking makes no app password)
    assert client.post("/api/plugins/openhands/setup", json={"app_password": "nope"}, headers=H).status_code == 400
    assert client.post("/api/plugins/openhands/setup", json={"app_password": "relay_abc"}, headers=H).status_code == 200
    now = settled(client)
    assert now["set_up"] and now["state"] == "running" and now["wanted"] and now["error"] is None
    assert ("pull", f"{module.IMAGE}:{module.PINNED}") in podman.calls
    # Its container: as the user, its port on this machine only (its key in its page), no telemetry, Relay's
    # projects, the forwarder and Relay's app read-only
    run = " ".join(podman.runs()[0])
    # (a Relay elsewhere than ~/.local/share/relay, as this test's: a container name of its own)
    assert now["container"] == module.container_name() and now["container"].startswith("relay-openhands-")
    assert f"--name {now['container']} --label io.relay.plugin=openhands --userns=keep-id:uid=10001,gid=10001" in run
    assert "-p 127.0.0.1:8000:8000" in run and f"LOCAL_BACKEND_API_KEY={now['api_key']}" in run
    assert "AGENT_CANVAS_ALLOW_LAN_SESSION_KEY=true" in run and "DO_NOT_TRACK=1" in run
    assert "ACP_USAGE_UPDATE_TIMEOUT=0" in run  # (no 2 s wait after each answer for a token count)
    assert f"{root}:/projects" in run and f"{module.FORWARDER}:/opt/relay/plugins/acp/static:ro" in run
    assert f"{module.SDK}:/opt/relay/sdk/python:ro" in run
    assert f"{module.APP}:/opt/relay/plugins/openhands/canvas-app:ro" in run
    # (the private scripts out of its sight: an empty folder in their place, nothing of theirs copied into it)
    assert "--tmpfs /projects/scripts/private:ro,size=1k,notmpcopyup" in run
    assert run.endswith(f"{module.IMAGE}:{module.PINNED}")
    # OpenHands set up: its agent Relay through the forwarder, the secrets, the projects as workspaces
    settings = next(body for _, _, method, path, body in asked if (method, path) == ("PATCH", "/api/settings"))
    agent = settings["agent_settings_diff"]
    assert agent["agent_kind"] == "acp" and agent["acp_server"] == "custom" and agent["acp_model"] is None
    assert agent["acp_command"] == ["python3", "/opt/relay/plugins/acp/static/relay-acp.py", "--skip-context",
                                    "--ask-in-relay", "--map", f"/projects={root}"]
    assert settings["misc_settings_diff"]["app_preferences"]["user_consents_to_analytics"] is False
    secrets = {body["name"]: body["value"] for _, _, method, path, body in asked if path == "/api/settings/secrets"}
    assert secrets == {"RELAY_URL": "http://host.containers.internal:8420", "RELAY_APP_PASSWORD": "relay_abc"}
    spaces = next(body for _, _, method, path, body in asked if path == "/api/workspaces")["workspaces"]
    assert sorted(w["path"] for w in spaces) == ["/projects/notes", "/projects/scripts", "/projects/site"]
    assert {key for _, key, *_ in asked} == {now["api_key"]}
    # (Canvas starts its conversations from its active agent profile: Relay's, as the settings)
    assert server.profiles["default"]["acp_command"] == shlex.join(agent["acp_command"])
    assert server.active == server.profiles["default"]["id"] and server.profiles["default"]["acp_model"] is None
    # Relay's app in it: installed from its folder in the container, and on
    assert server.apps == {"relay": {"name": "relay", "enabled": True, "source": module.APP_PLACE}}
    assert (module.APP / "canvas-extension.json").is_file() and (module.APP / "extension.js").is_file()
    # Its UI at Relay's /canvas, after Relay's sign-in
    plugins = client.app.state.relay.plugins
    assert plugins.forwarded("/canvas/conversations/1") == 8000 and plugins.forwarded("/canvasx") is None
    assert client.get("/api/plugins/openhands/status").json()["relay_agent"] is True
    # Its conversations, each with the Relay conversation it is; Relay's projects in it at once
    assert client.get("/api/plugins/openhands/conversations").json()["conversations"] == [
        {"id": "oh1", "title": "Fix the header", "status": "running", "relay": "r1"},
        {"id": "oh2", "title": "Its own agent", "status": "idle", "relay": None}]
    spaces = len([a for a in asked if a[3] == "/api/workspaces"])
    assert client.post("/api/plugins/openhands/sync", headers=H).json() == {"ok": True}
    assert len([a for a in asked if a[3] == "/api/workspaces"]) == spaces + 1
    # Another agent: OpenHands set anew, no restart
    assert client.put("/api/plugins/openhands/settings", json={"agent": "sonnet"}, headers=H).status_code == 400
    assert client.put("/api/plugins/openhands/settings", json={"port": 8420}, headers=H).status_code == 400
    client.put("/api/plugins/openhands/settings", json={"agent": "opencode|opencode/big-pickle"}, headers=H)
    last = [body for _, _, method, path, body in asked if (method, path) == ("PATCH", "/api/settings")][-1]
    assert last["agent_settings_diff"]["acp_model"] == "opencode|opencode/big-pickle"
    assert server.profiles["default"]["acp_model"] == "opencode|opencode/big-pickle"  # (its next conversations')
    assert len(podman.runs()) == 1
    assert len([a for a in asked if a[3] == "/api/canvas-extensions/install"]) == 1  # (it's there: not again)
    # Another agent chosen in OpenHands: the page says so, and sets Relay's again
    server.save("mine", {"agent_kind": "openhands"})
    server.active = "id-mine"
    assert client.get("/api/plugins/openhands/status").json()["relay_agent"] is False
    assert client.post("/api/plugins/openhands/use-relay", headers=H).json()["relay_agent"] is True
    assert server.active == "id-default" and server.profiles["mine"] == {"agent_kind": "openhands", "name": "mine",
                                                                          "id": "id-mine", "revision": 0}
    # Another port: made anew on it
    client.put("/api/plugins/openhands/settings", json={"port": 8100}, headers=H)
    settled(client)
    assert "-p 127.0.0.1:8100:8000" in " ".join(podman.runs()[-1])
    assert plugins.forwarded("/canvas/") == 8100
    assert client.get("/api/plugins/openhands/logs").json() == {"logs": "agent-canvas: started"}
    # Only ever its own container
    touched = [c for c in podman.calls if c[0] in ("rm", "stop")]
    assert touched and all(now["container"] in c for c in touched)


def test_openhands_conversations_start_with_the_agent_chosen_in_relay(openhands):
    # As it was live: Canvas made its profile of the settings at its first look, before an agent was chosen (and
    # with an older command), and starts its conversations from it, whatever the settings say since
    client, module, podman, server, root = openhands
    server.save("default", {"agent_kind": "acp", "acp_server": "custom", "acp_model": None,
                            "acp_command": "python3 /opt/relay/plugins/acp/static/relay-acp.py --skip-context"})
    server.active = "id-default"
    client.post("/api/plugins/openhands/setup", json={"app_password": "relay_abc"}, headers=H)
    assert settled(client)["relay_agent"] is True
    made = server.profiles["default"]
    assert "--ask-in-relay" in made["acp_command"] and made["revision"] == 1 and server.active == "id-default"
    client.put("/api/plugins/openhands/settings", json={"agent": "claude-code|"}, headers=H)
    assert server.profiles["default"]["acp_model"] == "claude-code|" and server.profiles["default"]["revision"] == 2
    client.put("/api/plugins/openhands/settings", json={"agent": "claude-code|"}, headers=H)
    assert server.profiles["default"]["revision"] == 2  # (the same: not saved again)
    # Relay's profile gone, another one on: Relay's made anew, and on
    del server.profiles["default"]
    server.save("mine", {"agent_kind": "openhands"})
    server.active = "id-mine"
    assert client.get("/api/plugins/openhands/status").json()["relay_agent"] is False
    client.post("/api/plugins/openhands/use-relay", headers=H)
    assert server.active == "id-relay" and server.profiles["relay"]["acp_model"] == "claude-code|"
    assert server.profiles["mine"]["revision"] == 0  # (not Relay's: left as it is)
    assert client.get("/api/plugins/openhands/status").json()["relay_agent"] is True


def test_openhands_on_and_off_is_remembered(openhands):
    client, module, podman, server, root = openhands
    client.post("/api/plugins/openhands/setup", json={"app_password": "relay_abc"}, headers=H)
    settled(client)
    plugin = client.app.state.relay.plugins.found["openhands"].plugin
    # Off: it stays off
    assert client.post("/api/plugins/openhands/stop", headers=H).json()["state"] == "exited"
    plugin.tasks[0].run()
    assert len(podman.runs()) == 1 and not plugin.store.get("running")
    # On, and stopped by itself: started again, but not again and again
    client.post("/api/plugins/openhands/start", headers=H)
    settled(client)
    for _ in range(4):
        podman.state = "exited"
        plugin.tasks[0].run()
        settled(client)
    assert len(podman.runs()) == 4  # (Start, and two starts again: three within ten minutes)
    assert "again and again" in client.get("/api/plugins/openhands/status").json()["error"]
    # Removed, with its data
    (plugin.data / "state").mkdir(parents=True, exist_ok=True)
    gone = client.post("/api/plugins/openhands/remove", json={"data": True}, headers=H).json()
    assert gone["state"] is None and not gone["wanted"] and not (plugin.data / "state").exists()
    assert plugin.store.get("api_key") is None
    # Started again: a new key of its own
    client.post("/api/plugins/openhands/start", headers=H)
    now = settled(client)
    assert now["state"] == "running" and len(now["api_key"]) == 48
    assert f"LOCAL_BACKEND_API_KEY={now['api_key']}" in " ".join(podman.runs()[-1])


def test_relays_app_in_openhands_comes_anew_when_it_changed(openhands, monkeypatch, tmp_path):
    client, module, podman, server, root = openhands
    app = tmp_path / "canvas-app"
    shutil.copytree(module.APP, app)
    monkeypatch.setattr(module, "APP", app)
    client.post("/api/plugins/openhands/setup", json={"app_password": "relay_abc"}, headers=H)
    settled(client)
    installs = lambda: len([a for a in server.asked if a[3] == "/api/canvas-extensions/install"])  # noqa: E731
    client.post("/api/plugins/openhands/use-relay", headers=H)
    assert installs() == 1
    (app / "extension.js").write_text((app / "extension.js").read_text() + "\n// changed\n")
    client.post("/api/plugins/openhands/use-relay", headers=H)
    assert installs() == 2
    # (changed with the clone while it runs: anew within a minute)
    (app / "extension.js").write_text((app / "extension.js").read_text() + "\n// again\n")
    plugin = client.app.state.relay.plugins.found["openhands"].plugin
    plugin.tasks[0].run()
    assert installs() == 3
    plugin.tasks[0].run()
    assert installs() == 3
    # An OpenHands without apps: it works on without Relay's, and says so
    def no_apps(port, key, method, path, body=None, timeout=30):
        if "canvas-extensions" in path:
            raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)
        return server(port, key, method, path, body, timeout)

    monkeypatch.setattr(module, "canvas", no_apps)
    now = client.post("/api/plugins/openhands/use-relay", headers=H).json()
    assert now["state"] == "running" and now["relay_agent"] is True and "Relay's app" in now["app_error"]


def test_openhands_is_ready_once_a_start_has_set_it_up(openhands, monkeypatch):
    """Canvas stays framed while OpenHands runs and is Relay's; not after a start that failed."""
    client, module, podman, server, root = openhands
    client.post("/api/plugins/openhands/setup", json={"app_password": "relay_abc"}, headers=H)
    assert settled(client)["ready"]
    # Not answering: no conversations, and not ready after a start that failed
    monkeypatch.setattr(module, "canvas", lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("refused")))
    monkeypatch.setattr(module, "STARTING", 0)
    assert client.get("/api/plugins/openhands/conversations").json() == {"conversations": []}
    client.post("/api/plugins/openhands/start", headers=H)
    now = settled(client)
    assert now["state"] == "running" and not now["ready"] and "didn't answer" in now["error"]


def test_openhands_says_when_a_newer_one_is_out(openhands, monkeypatch):
    client, module, podman, server, root = openhands
    looked = []
    monkeypatch.setattr(module, "latest_version", lambda: looked.append(1) or "1.27.0")
    client.post("/api/plugins/openhands/setup", json={"app_password": "relay_abc"}, headers=H)
    now = settled(client)
    assert now["ready"] and now["newer"] is None
    plugin = client.app.state.relay.plugins.found["openhands"].plugin
    plugin.tasks[0].run()
    plugin.tasks[0].run()
    assert looked == [1]  # (once a day)
    assert client.get("/api/plugins/openhands/status").json()["newer"] == "1.27.0"
    # Updated: this one is the newest
    podman.version = "1.27.0"
    client.post("/api/plugins/openhands/update", headers=H)
    now = settled(client)
    assert now["image"].endswith(":1.27.0") and now["newer"] is None and now["ready"]


def test_openhands_without_the_acp_plugin_says_so(openhands, monkeypatch):
    client, module, podman, server, root = openhands
    monkeypatch.setattr(module, "FORWARDER", root / "nowhere")
    client.post("/api/plugins/openhands/setup", json={"app_password": "relay_abc"}, headers=H)
    now = settled(client)
    assert "ACP plugin" in now["error"] and podman.runs() == []


def test_only_the_relay_at_its_usual_place_has_the_plain_container_name(monkeypatch, tmp_path):
    """A trial's or a test's Relay on the same machine never takes the user's OpenHands for its own: its container has
    that Relay's mark, as its boxes do."""
    spec = importlib.util.spec_from_file_location("openhands_plugin", ROOT / "plugins" / "openhands" / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    spec.loader.exec_module(module)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    monkeypatch.setenv("RELAY_HOME", str(tmp_path / "share" / "relay"))
    assert module.container_name() == "relay-openhands"
    monkeypatch.setenv("RELAY_HOME", str(tmp_path / "trial"))
    assert module.container_name() == f"relay-openhands-{module.containers.instance()}"
    assert len(module.container_name()) == 24


def test_openhands_runs_in_a_scope_of_its_own_under_a_service(monkeypatch):
    """Relay as a service: `podman run` in a systemd scope, so a restart of Relay leaves its network running."""
    spec = importlib.util.spec_from_file_location("openhands_plugin", ROOT / "plugins" / "openhands" / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    spec.loader.exec_module(module)
    ran = []
    monkeypatch.setattr(module.subprocess, "run", lambda argv, **kw: ran.append(argv) or
                        subprocess.CompletedProcess(argv, 0, "", ""))
    monkeypatch.setattr(module.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(module, "in_a_service", lambda: True)
    module.podman("run", "-d", "x")
    module.podman("ps", "-a")
    assert ran == [["systemd-run", "--user", "--scope", "--quiet", "--collect", "podman", "run", "-d", "x"],
                   ["podman", "ps", "-a"]]
    monkeypatch.setattr(module, "in_a_service", lambda: False)
    module.podman("run", "-d", "x")
    assert ran[-1] == ["podman", "run", "-d", "x"]
