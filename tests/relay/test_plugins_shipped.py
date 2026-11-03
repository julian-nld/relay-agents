"""The plugins that come with Relay, in plugins/ (Notes, the example, is test_plugins.py's): each loaded as a link in
the plugins folder, as its README says."""

import importlib.util
import pathlib
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
import relay_sdk  # noqa: E402

# (test_plugins.py's helpers: its fixture for the data folder, a Relay app, signing in)
_spec = importlib.util.spec_from_file_location("relay_plugin_tests", pathlib.Path(__file__).with_name("test_plugins.py"))
_helpers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_helpers)
H, home, relay_app, signed_in, mcp, until = (_helpers.H, _helpers.home, _helpers.relay_app, _helpers.signed_in,
                                             _helpers.mcp, _helpers.until)


@pytest.fixture(autouse=True)
def no_real_relay(monkeypatch):
    """The plugins' Relay API calls are each test's own: none reaches a real Relay (the machine's own answers at the
    SDK's default address)."""
    def refused(self, method, path, *args, **kwargs):
        raise AssertionError(f"a test's plugin called Relay's API for real: {method} {path}")
    monkeypatch.setattr(relay_sdk.Session, "request", refused)


def linked(home, *names):
    (home / "plugins").mkdir(parents=True, exist_ok=True)
    for name in names:
        (home / "plugins" / name).symlink_to(ROOT / "plugins" / name)


# Usage limits


def test_usage_limits_keeps_each_window_and_says_when_one_runs_low(home, tmp_path, monkeypatch):
    linked(home, "usage_limits")
    now = time.time()
    rows = [{"provider_id": "claude-code", "name": "Claude Code", "also": ["Claude (work)"], "plan": "max",
             "source": "provider", "checked": now - 60,
             "windows": [{"label": "5-hour", "used": 0.4, "resets_at": now + 3600, "left": None},
                         {"label": "Weekly", "used": 0.2, "resets_at": now + 86400, "left": None}]},
            {"provider_id": "codex", "name": "Codex", "also": [], "plan": None, "source": "last run",
             "checked": now - 7200, "windows": [{"label": "Weekly", "used": 0.3, "resets_at": now + 86400, "left": None}]}]
    asked, told = [], []
    monkeypatch.setattr(relay_sdk, "limits", lambda user=None: asked.append(user) or rows)
    monkeypatch.setattr(relay_sdk, "notify", lambda text, title=None, url=None, user=None, priority=None:
                        told.append((title, text, url)) or True)
    with relay_app(tmp_path) as client:
        signed_in(client)
        plugin = client.app.state.relay.plugins.found["usage_limits"].plugin
        assert client.app.state.relay.plugins.found["usage_limits"].working()
        # The page asks first: a look now, as none was made
        history = client.get("/api/plugins/usage_limits/history?days=7").json()
        assert len(asked) == 1 and asked[0] is not None  # (as the plugin: its own app password)
        keyed = {s["key"]: s for s in history["series"]}
        assert set(keyed) == {"claude-code|5-hour", "claude-code|Weekly", "codex|Weekly"}
        assert keyed["claude-code|5-hour"]["points"] == [[round(now - 60), 0.4, now + 3600]]
        assert keyed["claude-code|5-hour"]["also"] == ["Claude (work)"] and history["threshold"] == 10
        assert client.get("/api/plugins/usage_limits/history").json()["series"] and len(asked) == 1  # (just looked)
        # Every 15 minutes: a provider's new word is kept; a last run's, told again, isn't
        rows[0]["checked"] = now
        rows[0]["windows"][0]["used"] = 0.95
        plugin.tasks[0].run()
        series = plugin.store.get("series")
        assert [p[1] for p in series["claude-code|5-hour"]["points"]] == [0.4, 0.95]
        assert len(series["codex|Weekly"]["points"]) == 1
        # Under 10% left: said once, with when it resets, and the badge counts it
        assert told == [("A usage limit runs low", f"Claude Code · 5-hour: 5% left, resets "
                         f"{time.strftime('%a %H:%M', time.localtime(now + 3600))}", "/p/usage_limits")]
        assert client.get("/api/plugins/usage_limits/low").json() == {"low": 1}
        rows[0]["checked"] = now + 1
        rows[0]["windows"][0]["used"] = 0.97
        plugin.tasks[0].run()
        assert len(told) == 1
        # Back, after it reset: said once more
        rows[0]["checked"] = now + 2
        rows[0]["windows"][0]["used"] = 0.05
        plugin.tasks[0].run()
        assert told[-1][0] == "A usage limit is back" and len(told) == 2
        assert client.get("/api/plugins/usage_limits/low").json() == {"low": 0}
        # A last run's window that has reset since: all of it is back, from when it reset
        rows[1]["windows"][0]["resets_at"] = now - 600
        plugin.tasks[0].run()
        assert plugin.store.get("series")["codex|Weekly"]["points"][-1] == [round(now - 600), 0, None]
        # 14 days are kept
        plugin.store.update("series", lambda series: {**series, "claude-code|Weekly": {
            **series["claude-code|Weekly"], "points": [[now - 15 * 86400, 0.1, None],
                                                       *series["claude-code|Weekly"]["points"]]}})
        rows[0]["checked"] = now + 3
        plugin.tasks[0].run()
        assert all(p[0] > now - 14 * 86400 for p in plugin.store.get("series")["claude-code|Weekly"]["points"])


def test_usage_limits_is_a_tool_for_agents(home, tmp_path, monkeypatch):
    """usage_limits_now, for agents and the Governor: what's left of each window, when it resets, when it would run out
    at its pace, and which logins it's also; one provider's, or the last known when Relay can't be asked."""
    linked(home, "usage_limits")
    now = time.time()
    rows = [{"provider_id": "claude-code", "name": "Claude Code", "also": [], "plan": "Pro", "checked": now - 60,
             "windows": [{"label": "5-hour", "used": 0.6, "resets_at": now + 7200, "left": None},
                         {"label": "Weekly", "used": 0.95, "resets_at": now + 3 * 86400, "left": None}]},
            {"provider_id": "copilot", "name": "GitHub Copilot", "also": ["Copilot (work)"], "plan": "Free",
             "checked": now - 60, "windows": [{"label": "Chat messages", "used": 0.25, "resets_at": now + 20 * 86400,
                                               "left": "150 of 200"}]},
            {"provider_id": "perplexity", "name": "Perplexity", "windows": [], "error": "couldn't ask it (Timeout)"}]
    monkeypatch.setattr(relay_sdk, "limits", lambda user=None: rows)
    monkeypatch.setattr(relay_sdk, "notify", lambda text, **kw: True)
    with relay_app(tmp_path) as client:
        signed_in(client)
        relay = client.app.state.relay
        assert "usage_limits_now" in [t["name"] for t in relay.plugins.tools(governor=True)]
        plugin = relay.plugins.found["usage_limits"].plugin
        # The 5-hour window climbed from 30% to 45% used in the last 50 minutes: at that pace it runs out first
        plugin.store.set("series", {"claude-code|5-hour": {
            "points": [[round(now - 3000), 0.3, now + 7200], [round(now - 1500), 0.45, now + 7200]], "low": False,
            "provider_id": "claude-code", "name": "Claude Code", "also": [], "plan": "Pro", "label": "5-hour",
            "left": None}})
        conv = client.post("/api/conversations", json={"provider_id": "fake"},
                           headers={**H, "x-relay-client": "web"}).json()
        client.post(f"/api/conversations/{conv['id']}/messages", json={"text": "linger"}, headers=H)
        token = relay.db.one("SELECT tools_token FROM conversations WHERE id = ?", (conv["id"],))["tools_token"]
        row = relay.db.one("SELECT * FROM conversations WHERE id = ?", (conv["id"],))

        def asked(**arguments):
            said = mcp(client, token, "tools/call", {"name": "usage_limits_now", "arguments": arguments})
            return said["result"]["content"][0]["text"]

        try:
            assert until(lambda: relay.delegation.run_for(token) is not None)
            text = asked()
            assert text.startswith("Usage limits of the user's subscriptions, as of ") and "low is 10% left" in text
            five = next(line for line in text.splitlines() if "5-hour" in line)
            assert five.startswith("- Claude Code (claude-code, Pro) · 5-hour: 40% left, resets ")
            assert "(in 2 h 0 min)" in five or "(in 1 h 59 min)" in five
            assert "at the pace of the last hours it runs out " in five and "before it resets" in five
            assert "· Weekly: 5% left (LOW), resets " in text and "(in 3 d 0 h)" in text
            assert "· Chat messages: 150 of 200 left (75%), resets " in text
            assert "The same login as: Copilot (work)" in text
            assert "- Perplexity (perplexity): couldn't ask it (Timeout)" in text
            # One provider's, by its id or name
            only = asked(provider="copilot")
            assert "Chat messages" in only and "Claude Code" not in only and "Perplexity" not in only
            assert "5-hour" in asked(provider="Claude") and asked(provider="nope").startswith("No usage limits known")
            # Relay can't be asked: the last known, said so
            def unreachable(user=None):
                raise relay_sdk.RelayError("Can't reach Relay")
            monkeypatch.setattr(relay_sdk, "limits", unreachable)
            assert "Relay couldn't be asked now (Can't reach Relay): these are the last known." in asked()
        finally:
            (pathlib.Path(row["directory"]) / "done-lingering").write_text("")
            assert until(lambda: relay.runs.active_run_for(conv["id"]) is None)


def test_usage_limits_settings(home, tmp_path, monkeypatch):
    linked(home, "usage_limits")
    rows = [{"provider_id": "copilot", "name": "Copilot", "also": [], "plan": "pro", "source": "provider",
             "checked": time.time(), "windows": [{"label": "Requests", "used": 0.85, "resets_at": None,
                                                   "left": "45 of 300"}]}]
    told = []
    monkeypatch.setattr(relay_sdk, "limits", lambda user=None: rows)
    monkeypatch.setattr(relay_sdk, "notify", lambda text, **kw: told.append(text) or True)
    with relay_app(tmp_path) as client:
        signed_in(client)
        assert client.get("/api/plugins/usage_limits/settings").json() == {"threshold": 10, "notify": True}
        put = lambda body: client.put("/api/plugins/usage_limits/settings", json=body, headers=H)  # noqa: E731
        assert put({"threshold": 0}).status_code == 400 and put({"threshold": "20"}).status_code == 400
        assert put({"notify": "yes"}).status_code == 400
        assert put({"threshold": 20, "notify": False}).json() == {"threshold": 20, "notify": False}
        # 15% left is low at 20%: kept as low, not said
        history = client.get("/api/plugins/usage_limits/history?days=1").json()
        assert history["threshold"] == 20 and history["series"][0]["left"] == "45 of 300"
        assert client.get("/api/plugins/usage_limits/low").json() == {"low": 1} and told == []
        assert client.get("/api/plugins/usage_limits/history?days=x").status_code == 400


# Server


CHAT_ID = "0123456789abcdef0123456789abcdef"


def server_module(monkeypatch, memory, cpu, containers, disk_used=0.4):
    """Server's measuring, with this test's numbers: memory {available fraction}, cpu [(idle, total), …] in turn,
    containers (listed, used)."""
    import shutil
    import sys as _sys

    module = _sys.modules["relay_plugins.server"]
    total = 16_000_000_000
    monkeypatch.setattr(module, "meminfo", lambda: {"MemTotal": total, "MemAvailable": int(total * memory["available"]),
                                                    "SwapTotal": 2_000_000_000, "SwapFree": 1_500_000_000})
    monkeypatch.setattr(module, "cpu_times", lambda: cpu.pop(0) if len(cpu) > 1 else cpu[0])
    monkeypatch.setattr(module, "uptime", lambda: 7200.0)
    monkeypatch.setattr(module, "containers_running", lambda: containers)
    monkeypatch.setattr(module, "podman", lambda *args: "")
    monkeypatch.setattr(shutil, "disk_usage", lambda path: shutil._ntuple_diskusage(1000, int(1000 * disk_used),
                                                                                    1000 - int(1000 * disk_used)))
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    return module


def test_server_says_how_the_machine_is_doing(home, tmp_path, monkeypatch):
    linked(home, "server")
    told = []
    monkeypatch.setattr(relay_sdk, "notify", lambda text, title=None, url=None, user=None, priority=None:
                        told.append((title, text, url)) or True)
    monkeypatch.setattr(relay_sdk.services, "list", lambda user=None: [{"id": "f" * 32, "name": "site"}])
    monkeypatch.setattr(relay_sdk.convo, "list", lambda user=None: [{"id": CHAT_ID, "title": "Plan the trip"}])
    home_dir = str(pathlib.Path.home())
    listed = {"a": {"name": "relay-box-claude-code", "labels": {"io.relay.box": "1", "io.relay.managed": "1"}, "mounts": []},
              "b": {"name": "relay-prod-ffffffff-2", "labels": {"io.relay.production": "f" * 32}, "mounts": ["/data"]},
              "c": {"name": "relay-1a2b", "labels": {"io.relay.managed": "1"},
                    "mounts": ["/tmp/opencode", home_dir, f"{home_dir}/.local/share/relay/chats/{CHAT_ID}",
                               f"{home_dir}/relay/src/relay/builtin_skills"]},
              "d": {"name": "relay-3c4d", "labels": {"io.relay.managed": "1"},
                    "mounts": ["/home/relay", f"{home_dir}/projects/site",  # (/home/relay: the box's own home)
                               "/home/x/.cache/opencode-manager/relay-containers/exchange"]},
              "e": {"name": "nginx", "labels": {}, "mounts": []},
              "f": {"name": "relay-5e6f", "labels": {"io.relay.managed": "1"},
                    "mounts": ["/home/relay", f"{home_dir}/projects/scripts/public"]}}
    used = {"a": [10.0, 400_000_000], "b": [5.0, 20_000_000], "c": [1.0, 1_000_000], "d": [0.0, 0], "e": [0.0, 5_000_000]}
    containers = (listed, {k: tuple(v) for k, v in used.items()})
    memory = {"available": 0.5}
    cpu = [(100, 1000), (150, 1100)]  # (a first look, then 50 of 100 idle since)
    with relay_app(tmp_path) as client:
        signed_in(client)
        relay = client.app.state.relay
        module = server_module(monkeypatch, memory, cpu, containers)
        plugin = relay.plugins.found["server"].plugin
        assert relay.plugins.found["server"].working() and [t.name for t in plugin.tools] == ["server_status"]
        client.get("/api/plugins/server/status")
        status = client.get("/api/plugins/server/status").json()
        assert status["cpu"] == 0.5 and status["memory"]["used"] == 0.5 and status["swap"]["used"] == 0.25
        assert status["disks"][0]["used"] == 0.4 and status["warnings"] == [] and status["uptime"] == 7200.0
        kinds = {c["name"]: (c["kind"], c["what"], c["chat"]) for c in status["containers"]}
        assert kinds == {"relay-box-claude-code": ("ProviderBox", "claude-code", None),
                         "relay-prod-ffffffff-2": ("Service", "site", None),
                         "relay-1a2b": ("WorkBox", "chat: Plan the trip", CHAT_ID),
                         "relay-3c4d": ("WorkBox", "~/projects/site", None),
                         "relay-5e6f": ("WorkBox", "scripts", None),
                         "nginx": ("Container", "nginx", None)}
        # Busiest first: CPU since the last look, as a share of the whole machine
        used["a"][0] += 2 * module.os.cpu_count()  # (two seconds a core: a second's worth of all of them, and more)
        containers[1]["a"] = tuple(used["a"])
        status = client.get("/api/plugins/server/status").json()
        assert status["containers"][0]["name"] == "relay-box-claude-code" and status["containers"][0]["cpu"] > 0
        # Every minute: a point of history, and the warnings, each said once
        memory["available"] = 0.05
        plugin.tasks[0].run()
        assert client.get("/api/plugins/server/warnings").json() == ["Memory is low: 5% available (0.8 GB of 16.0 GB)"]
        plugin.tasks[0].run()
        memory["available"] = 0.12  # (not fine yet: no back and forth)
        plugin.tasks[0].run()
        assert told == [("This server", "Memory is low: 5% available (0.8 GB of 16.0 GB)", "/p/server")]
        assert len(client.get("/api/plugins/server/warnings").json()) == 1
        memory["available"] = 0.5
        plugin.tasks[0].run()
        assert client.get("/api/plugins/server/warnings").json() == []
        history = client.get("/api/plugins/server/history").json()
        assert len(history) == 4 and history[-1]["memory"] == 0.5 and set(history[-1]) == {"t", "cpu", "memory", "swap", "disks"}
        # The CPU busy for 10 minutes
        for minute in range(10):
            cpu[:] = [(160 + minute, 2000 + minute * 100), (160 + minute, 2000 + minute * 100)]
            plugin.tasks[0].run()
        assert told[-1][1] == "The CPU has been over 90% for 10 minutes"


def test_server_status_is_a_tool_for_agents(home, tmp_path, monkeypatch):
    linked(home, "server")
    monkeypatch.setattr(relay_sdk, "notify", lambda text, **kw: True)
    monkeypatch.setattr(relay_sdk.services, "list", lambda user=None: [])
    monkeypatch.setattr(relay_sdk.convo, "list", lambda user=None: [])
    with relay_app(tmp_path) as client:
        signed_in(client)
        relay = client.app.state.relay
        server_module(monkeypatch, {"available": 0.03}, [(0, 100), (50, 200)],
                      ({"a": {"name": "relay-box-codex", "labels": {"io.relay.box": "1"}, "mounts": []}},
                       {"a": (1.0, 300_000_000)}), disk_used=0.95)
        relay.plugins.found["server"].plugin.tasks[0].run()  # (its warnings: a minute's look)
        conv = client.post("/api/conversations", json={"provider_id": "fake"},
                           headers={**H, "x-relay-client": "web"}).json()
        client.post(f"/api/conversations/{conv['id']}/messages", json={"text": "linger"}, headers=H)
        token = relay.db.one("SELECT tools_token FROM conversations WHERE id = ?", (conv["id"],))["tools_token"]
        row = relay.db.one("SELECT * FROM conversations WHERE id = ?", (conv["id"],))
        try:
            assert until(lambda: relay.delegation.run_for(token) is not None)
            said = mcp(client, token, "tools/call", {"name": "server_status", "arguments": {}})
            text = said["result"]["content"][0]["text"]
            assert text.startswith("CPU: ") and "Memory: " in text and "Disks: System (/) 95% full" in text
            assert "Busiest containers: ProviderBox codex" in text and "300 MB" in text
            assert "Warnings: The disk of / is 95% full" in text and "Memory is low: 3% available" in text
        finally:
            (pathlib.Path(row["directory"]) / "done-lingering").write_text("")
            assert until(lambda: relay.runs.active_run_for(conv["id"]) is None)
