"""Plugins (relay.plugins): folders in RELAY_HOME/plugins, found and loaded at Relay's start (a broken one is said,
and the rest go on); their routes, background tasks, hook for runs that end and tools for agents; the example
plugin (plugins/notes) as docs/WRITING-A-PLUGIN.md has it."""

import dataclasses
import json
import pathlib
import re
import sys
import textwrap
import threading
import time

import pytest
from starlette.testclient import TestClient

from relay import config, doctor
from relay.app import create_app
from relay.db import Database
from relay.plugins import API, Call, Plugin, PluginError, Plugins, ToolCall
from relay.providers import Registry

H = {"x-relay-request": "1"}
ROOT = pathlib.Path(__file__).resolve().parents[2]
NOTES = ROOT / "plugins" / "notes"
GUIDE = (ROOT / "docs" / "WRITING-A-PLUGIN.md").read_text()

# A stand-in agent: answers ok, fails on "fail", and stays at work on "linger" until the test lets it go
AGENT = textwrap.dedent("""\
    import json, os, sys, time
    prompt = sys.stdin.read()
    def emit(**e): print(json.dumps(e), flush=True)
    emit(type="session", session="s1")
    if "linger" in prompt:
        for _ in range(150):
            if os.path.exists("done-lingering"): break
            time.sleep(0.1)
    if "fail" in prompt:
        emit(type="error", message="it broke")
        emit(type="done", status="failed")
    else:
        emit(type="text", delta="ok")
        emit(type="done", status="succeeded")
""")

DEMO = """\
    from relay.plugins import PluginError

    def setup(plugin):
        items = {"1": "one"}
        plugin.route("items", lambda call: items)
        plugin.route("items", lambda call: items.update(call.json) or items, methods=["POST", "PUT"])
        def one(call):
            if call.params["id"] not in items:
                raise PluginError("No such item", 404)
            return {"id": call.params["id"], "text": items[call.params["id"]], "q": call.query.get("q")}
        plugin.route("items/{id}", one)
        plugin.route("boom", lambda call: 1 / 0)
        plugin.route("me", lambda call: call.user)
        plugin.route("when", lambda call: __import__("datetime").datetime.now())
        def upstream(call):
            raise PluginError("The weather service turned us away", 401)
        plugin.route("upstream", upstream)
        plugin.tool("echo", "Say it back.", {"type": "object", "properties": {"text": {"type": "string",
                    "description": "what to say"}}, "required": ["text"]},
                    lambda call: f"echo {call.arguments['text']} in {call.conversation_id}")
        plugin.tool("refuse", "Refuses.", None, lambda call: (_ for _ in ()).throw(PluginError("not today")))
        plugin.tool("crash", "Crashes.", None, lambda call: 1 / 0)
        plugin.tool("steer", "For the Governor too.", None, lambda call: {"steered": True}, governor=True)
"""


def add_plugin(home, name, init=None, manifest=None, static=None):
    folder = home / "plugins" / name
    folder.mkdir(parents=True)
    (folder / "plugin.json").write_text(json.dumps({"label": name.title()} if manifest is None else manifest))
    if init is not None:
        (folder / "__init__.py").write_text(textwrap.dedent(init))
    for path, text in (static or {}).items():
        (folder / "static" / path).parent.mkdir(parents=True, exist_ok=True)
        (folder / "static" / path).write_text(text)
    return folder


@pytest.fixture
def home(tmp_path, monkeypatch):
    found = tmp_path / "data"
    monkeypatch.setenv("RELAY_HOME", str(found))
    return found


def relay_app(tmp_path, **settings):
    agent = tmp_path / "agent.py"
    agent.write_text(AGENT)
    workspace = tmp_path / "ws"
    (workspace / "project").mkdir(parents=True, exist_ok=True)
    registry = Registry(tmp_path / "providers")
    registry.ensure_defaults()
    if not any(t.id == "fake" for t in registry.templates()):
        registry.add({"id": "fake", "name": "Fake agent", "kind": "command",
                      "options": {"command": [sys.executable, str(agent)]}})
    return TestClient(create_app(config.Settings(workspace_roots=[str(workspace)], chat_root=str(tmp_path / "chats"),
                                                 **settings), Database(tmp_path / "relay.db"), registry))


def signed_in(client):
    assert client.post("/api/setup", json={"username": "me", "password": "correct horse"}, headers=H).status_code == 200
    return client


def until(condition, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


def mcp(client, token, method, params=None):
    return client.post(f"/mcp/{token}", json={"jsonrpc": "2.0", "id": 1, "method": method,
                                               "params": params or {}}).json()


def test_plugins_are_found_and_a_broken_one_never_stops_the_rest(home, monkeypatch):
    add_plugin(home, "good", "def setup(plugin):\n    plugin.route('x', lambda call: 1)\n")
    add_plugin(home, "static_only", static={"index.js": "export default function setup() {}"})
    add_plugin(home, "typo", "def setup(plugin)\n    pass\n")
    add_plugin(home, "slow", "def setup(plugin):\n    plugin.every(5, lambda: None)\n")
    add_plugin(home, "later", manifest={"api": API + 1})
    add_plugin(home, "memory")
    add_plugin(home, "off", "raise SystemExit('never imported')\n")
    add_plugin(home, "quits", "import sys\nsys.exit(3)\n")
    add_plugin(home, "waits", "import time\ndef setup(plugin):\n    time.sleep(5)\n")
    add_plugin(home, "agentos_x")  # (its tools would be taken for AgentOS's)
    add_plugin(home, "aio", "def setup(plugin):\n    async def items(call):\n        return []\n"
                            "    plugin.route('items', items)\n")
    add_plugin(home, "twice", "def setup(plugin):\n    plugin.route('x', lambda c: 1)\n"
                              "    plugin.route('x', lambda c: 2, methods=['GET', 'POST'])\n")
    add_plugin(home, "long", "def setup(plugin):\n    plugin.tool('a' * 50, 'Long.', None, lambda c: '')\n")
    add_plugin(home, "loose", "def setup(plugin):\n    plugin.tool('t', 'T.', {'type': 'object', "
                              "'properties': {'x': 'string'}}, lambda c: '')\n")
    add_plugin(home, "good_b", "def setup(plugin):\n    plugin.tool('c', 'C.', None, lambda c: '')\n")
    add_plugin(home, "goods", "def setup(plugin):\n    plugin.tool('b_c', 'Mine too.', None, lambda c: '')\n")
    (home / "plugins" / "nothing").mkdir()
    (home / "plugins" / "Bad-Name").mkdir()
    plugins = Plugins()
    monkeypatch.setattr("relay.plugins.SETUP_WAIT", 0.5)
    plugins.load(off=["off"])
    found = {p["name"]: p for p in plugins.public()}
    assert found["good"]["loaded"] and found["good"]["problem"] is None
    assert found["static_only"]["loaded"] and found["static_only"]["script"] and not found["static_only"]["style"]
    assert not found["typo"]["loaded"] and "typo/__init__.py, line 1" in found["typo"]["problem"]
    # (a plugin's own mistake with the contract: what, and where)
    assert found["slow"]["problem"] == "every(): at least 60 seconds between runs (slow/__init__.py, line 2)"
    assert f"written for plugin API {API + 1}" in found["later"]["problem"]
    assert "a name Relay's own tools have" in found["memory"]["problem"]
    assert found["nothing"]["problem"] == "it has no plugin.json"
    assert "lowercase letters" in found["Bad-Name"]["problem"]
    assert not found["off"]["enabled"] and not found["off"]["loaded"] and found["off"]["problem"] is None
    # (what would keep Relay from starting, or from answering, keeps only the plugin out)
    assert found["quits"]["problem"] == "it called sys.exit(3) (quits/__init__.py, line 2)"
    assert "didn't end within 0.5 seconds" in found["waits"]["problem"]
    assert found["agentos_x"]["problem"].startswith("it begins with agentos, a name Relay's own tools have")
    assert "route 'items': a plain function (def, not async def)" in found["aio"]["problem"]
    assert "route 'x' takes GET twice" in found["twice"]["problem"]
    assert f"long_{'a' * 50} is over 48 characters" in found["long"]["problem"]
    assert "its properties objects" in found["loose"]["problem"]
    assert found["good_b"]["loaded"] and found["goods"]["problem"] == "its tool goods_b_c is the good_b plugin's too" \
        or found["goods"]["loaded"]  # (a_b's c and a's b_c: one name, the first loaded keeps it)
    assert [p.name for p in plugins.loaded()] == ["good", "good_b", "goods", "static_only"]
    # `relay doctor` and `relay plugins` say what they can without running a plugin's code
    checks = {c.title: c for c in doctor.run_checks(config.Settings(plugins_off=["off"]), check_providers=False)}
    assert checks["Plugin good"].status == "ok" and checks["Plugin good"].detail == "found"
    assert checks["Plugin off"].detail == "turned off" and checks["Plugin quits"].detail == "found"
    assert checks["Plugin typo"].status == "warn" and "typo/__init__.py, line 1" in checks["Plugin typo"].detail


def test_a_plugins_routes_are_relays_api(home, tmp_path):
    add_plugin(home, "demo", DEMO, static={"index.js": "export default () => {}", "img/dot.svg": "<svg/>"})
    with relay_app(tmp_path) as client:
        assert client.get("/api/plugins/demo/items").status_code == 401  # (signed in, as all of Relay's API)
        assert client.get("/plugins/demo/static/img/dot.svg").status_code == 401  # (its page's files too)
        signed_in(client)
        assert client.get("/api/plugins/demo/items").json() == {"1": "one"}
        assert client.post("/api/plugins/demo/items", json={"2": "two"}).status_code == 403  # (its CSRF rule too)
        assert client.post("/api/plugins/demo/items", json={"2": "two"}, headers=H).json() == {"1": "one", "2": "two"}
        assert client.get("/api/plugins/demo/items/2?q=hi").json() == {"id": "2", "text": "two", "q": "hi"}
        assert client.get("/api/plugins/demo/me").json()["username"] == "me"
        missing = client.get("/api/plugins/demo/items/9")
        assert missing.status_code == 404 and missing.json() == {"error": "No such item"}
        crashed = client.get("/api/plugins/demo/boom")
        assert crashed.status_code == 500 and "ZeroDivisionError" in crashed.json()["error"]
        when = client.get("/api/plugins/demo/when")  # (not JSON: the plugin's mistake, said and kept)
        assert when.status_code == 500 and "/when answered with what isn't JSON" in when.json()["error"]
        assert client.get("/api/plugins/demo/upstream").status_code == 502  # (a 401 would sign the page out)
        assert client.head("/api/plugins/demo/items").status_code == 200
        assert client.delete("/api/plugins/demo/items", headers=H).status_code == 405
        assert client.get("/api/plugins/demo/nowhere").status_code == 404
        assert client.get("/api/plugins/other/items").status_code == 404
        bad = client.post("/api/plugins/demo/items", content=b"{nope", headers={**H, "content-type": "application/json"})
        assert bad.status_code == 400
        # Its static files and its pages
        assert client.get("/plugins/demo/static/img/dot.svg").text == "<svg/>"
        proxy = {"Authorization": "Basic dXNlcjpwcm94eQ=="}  # (a proxy's own sign-in in front of Relay: the session)
        assert client.get("/plugins/demo/static/img/dot.svg", headers=proxy).text == "<svg/>"
        assert client.get("/plugins/demo/static/../plugin.json").status_code == 404
        assert client.get("/plugins/demo/static/%2e%2e/plugin.json").status_code == 404
        assert client.get("/plugins/demo/static/a%00b").status_code == 404
        assert client.get("/p/demo/anything/here").status_code == 200
        # Settings → Plugins: the list, its last error, and off (at once)
        listed = client.get("/api/plugins").json()
        demo = next(p for p in listed["plugins"] if p["name"] == "demo")
        assert demo["loaded"] and demo["script"] and "isn't JSON" in demo["errors"]["route"]  # (its last)
        assert set(demo["tools"]) == {"demo_echo", "demo_refuse", "demo_crash", "demo_steer"}
        assert listed["folder"] == str(home / "plugins")
        assert demo["exposed"] is False
        turned = client.patch("/api/plugins/demo", json={"enabled": False}, headers=H).json()
        assert turned["enabled"] is False and turned["loaded"] is False and turned["restart"] is False
        assert config.load_settings().plugins_off == ["demo"]
        # (off, it stops at once: its routes, tools, tasks and hooks; on again, at once too, as Relay loaded it)
        assert client.get("/api/plugins/demo/items").status_code == 404
        assert not client.app.state.relay.plugins.tools()
        turned = client.patch("/api/plugins/demo", json={"enabled": True}, headers=H).json()
        assert turned["loaded"] is True and turned["script"] is True and turned["restart"] is False
        assert client.get("/api/plugins/demo/items").status_code == 200
        assert config.load_settings().plugins_off == []
        client.patch("/api/plugins/demo", json={"enabled": False}, headers=H)
        assert client.patch("/api/plugins/demo", json={"enabled": "no"}, headers=H).status_code == 400
        assert client.patch("/api/plugins/ghost", json={"enabled": True}, headers=H).status_code == 404
    with relay_app(tmp_path, plugins_off=["demo"]) as client:  # (after a restart)
        client.post("/api/login", json={"username": "me", "password": "correct horse"}, headers=H)
        assert client.get("/api/plugins/demo/items").status_code == 404
        assert client.get("/plugins/demo/static/index.js").status_code == 404
        # On again, one Relay didn't load at its start: from the next start
        turned = client.patch("/api/plugins/demo", json={"enabled": True}, headers=H).json()
        assert turned["enabled"] is True and turned["loaded"] is False and turned["restart"] is True


def test_background_tasks_and_the_hook_run_apart(home):
    add_plugin(home, "busy", """\
        import threading
        runs, heard = [], []
        def setup(plugin):
            plugin.every(60, lambda: runs.append(1))
            plugin.every(120, lambda: 1 / 0)
            plugin.on_run_ended(heard.append)
            plugin.on_run_ended(lambda run: run["nope"])
    """)
    plugins = Plugins()
    plugins.load()
    module = sys.modules["relay_plugins.busy"]
    plugins.tick(1000.0)
    assert until(lambda: module.runs == [1] and "task" in plugins.found["busy"].errors)
    assert "ZeroDivisionError" in plugins.found["busy"].errors["task"]
    plugins.tick(1030.0)  # (not due yet)
    plugins.tick(1061.0)
    assert until(lambda: module.runs == [1, 1])
    plugins.run_ended({"conversation_id": "c", "run_id": "r", "status": "failed"})
    assert until(lambda: module.heard and "hook" in plugins.found["busy"].errors)
    assert module.heard == [{"conversation_id": "c", "run_id": "r", "status": "failed"}]
    assert "KeyError" in plugins.found["busy"].errors["hook"]


def test_a_plugins_task_never_runs_twice_at_once(home):
    add_plugin(home, "slowpoke", """\
        import threading, time
        started, go = [], threading.Event()
        def setup(plugin):
            plugin.every(60, lambda: (started.append(1), go.wait(5)))
    """)
    plugins = Plugins()
    plugins.load()
    module = sys.modules["relay_plugins.slowpoke"]
    plugins.tick(0.0)
    assert until(lambda: module.started == [1])
    plugins.tick(500.0)  # (due, but the last run hasn't ended)
    time.sleep(0.2)
    assert module.started == [1]
    module.go.set()
    assert until(lambda: not plugins.loaded()[0].tasks[0].running)


def test_a_plugin_forwards_a_path_to_a_web_app_of_its_own(home, tmp_path):
    """plugin.forward: Relay's /<path>/… to a server of the plugin's (its path as it is), for someone signed in."""
    import http.server
    import socket

    seen = []

    class App(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            seen.append((self.path, self.headers.get("host"), self.headers.get("cookie")))
            if self.path == "/webapp/old":
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{port}/webapp/new")
                self.end_headers()
                return
            body = f"app at {self.path}".encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), App)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    add_plugin(home, "webby", f"""\
        def setup(plugin):
            plugin.forward("webapp", lambda: {port})
        """)
    add_plugin(home, "greedy", """\
        def setup(plugin):
            plugin.forward("api", 1234)
        """)
    add_plugin(home, "zcopy", f"""\
        def setup(plugin):
            plugin.forward("webapp", {port})
        """)
    try:
        with relay_app(tmp_path) as client:
            found = client.app.state.relay.plugins.found
            assert found["webby"].working() and "a path of Relay's own" in found["greedy"].problem
            assert "which the webby plugin does" in found["zcopy"].problem
            assert client.get("/webapp/page?x=1", follow_redirects=False).headers["location"] == "/"  # (sign in first)
            signed_in(client)
            answer = client.get("/webapp/page?x=1")
            assert answer.status_code == 200 and answer.text == "app at /webapp/page?x=1"
            assert seen[-1][1] == f"localhost:{port}" and "relay_session" not in (seen[-1][2] or "")
            moved = client.get("/webapp/old", follow_redirects=False)
            assert moved.status_code == 302 and moved.headers["location"] == "/webapp/new"
            assert client.get("/api/state").json()["user"]  # (Relay's own, as ever)
            server.server_close()
            server.shutdown()
            assert client.get("/webapp/page").status_code == 502  # (nothing answers there)
    finally:
        server.server_close()
    plugin = Plugin("x", tmp_path, {}, lambda *a: None)
    for wrong in ("two/parts", "Upper", ""):
        with pytest.raises(PluginError):
            plugin.forward(wrong, 80)
    with pytest.raises(PluginError):
        plugin.forward("app", 70000)


def test_relay_makes_the_plugins_folder(home, tmp_path):
    """So that putting a plugin there (its README's `ln -s`) works on a Relay that has none yet."""
    with relay_app(tmp_path):
        assert (home / "plugins").is_dir()


def test_agents_get_a_plugins_tools(home, tmp_path):
    add_plugin(home, "demo", DEMO)
    with relay_app(tmp_path) as client:
        signed_in(client)
        relay = client.app.state.relay
        conv = client.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
        client.post(f"/api/conversations/{conv['id']}/messages", json={"text": "linger"}, headers=H)
        token = relay.db.one("SELECT tools_token FROM conversations WHERE id = ?", (conv["id"],))["tools_token"]
        row = relay.db.one("SELECT * FROM conversations WHERE id = ?", (conv["id"],))
        try:
            assert until(lambda: relay.delegation.run_for(token) is not None)
            listed = {t["name"]: t for t in mcp(client, token, "tools/list")["result"]["tools"]}
            assert {"demo_echo", "demo_refuse", "demo_crash", "demo_steer"} <= set(listed)
            assert listed["demo_echo"]["description"] == "Say it back. (from the Demo plugin)"
            call = lambda name, arguments: mcp(client, token, "tools/call",  # noqa: E731
                                               {"name": name, "arguments": arguments})["result"]
            said = call("demo_echo", {"text": "hi"})
            assert not said.get("isError") and said["content"][0]["text"] == f"echo hi in {conv['id']}"
            assert json.loads(call("demo_steer", {})["content"][0]["text"]) == {"steered": True}
            refused = call("demo_refuse", {})
            assert refused["isError"] and refused["content"][0]["text"].startswith("not today")
            crashed = call("demo_crash", {})
            assert crashed["isError"] and "The demo plugin failed: ZeroDivisionError" in crashed["content"][0]["text"]
            # `relay help plugins`, and what the agent is told
            helped = call("help", {"topic": "plugins"})["content"][0]["text"]
            assert "relay plugin TOOL KEY=VALUE" in helped and "demo_echo: Say it back." in helped
            assert "  text (string, required): what to say" in helped
            assert "plugin TOOL [KEY=VALUE …]" in call("help", {})["content"][0]["text"]
            assert relay.runs._plugin_tools(conv["id"]) == ["demo_echo", "demo_refuse", "demo_crash", "demo_steer"]
            # A subagent gets none of them
            relay.db.execute("UPDATE conversations SET parent_run = 'someone' WHERE id = ?", (conv["id"],))
            assert not {"demo_echo"} & {t["name"] for t in mcp(client, token, "tools/list")["result"]["tools"]}
            relay.db.execute("UPDATE conversations SET parent_run = NULL WHERE id = ?", (conv["id"],))
        finally:
            (pathlib.Path(row["directory"]) / "done-lingering").write_text("")
            assert until(lambda: relay.runs.active_run_for(conv["id"]) is None)
        # The Governor gets only those meant for it
        assert [t["name"] for t in relay.plugins.tools(governor=True)] == ["demo_steer"]


PICKY = """\
    def setup(plugin):
        plugin.tool("secret", "Only for its members.", None, lambda call: "psst",
                    only=lambda conversation_id: conversation_id in plugin.store.get("members", []))
        plugin.tool("broken", "Its only fails.", None, lambda call: "never", only=lambda conversation_id: 1 / 0)
"""


def test_settings_offers_the_plugins_that_come_with_relay_and_shows_one_just_added(home, tmp_path, monkeypatch):
    bundled = tmp_path / "shipped"
    (bundled / "handy").mkdir(parents=True)
    (bundled / "handy" / "plugin.json").write_text('{"label": "Handy", "description": "Does a thing"}')
    (bundled / "noplugin").mkdir()
    monkeypatch.setattr("relay.plugins.BUNDLED", bundled)
    with relay_app(tmp_path) as client:
        signed_in(client)
        listed = client.get("/api/plugins").json()
        assert listed["plugins"] == []
        assert listed["bundled"] == [{"name": "handy", "label": "Handy", "description": "Does a thing"}]
        assert client.post("/api/plugins/noplugin", headers=H).status_code == 404
        added = client.post("/api/plugins/handy", headers=H).json()
        assert added["enabled"] and not added["loaded"] and added["restart"]
        assert (home / "plugins" / "handy").resolve() == bundled / "handy"
        listed = client.get("/api/plugins").json()
        assert listed["bundled"] == [] and [p["name"] for p in listed["plugins"]] == ["handy"]
        assert client.post("/api/plugins/handy", headers=H).status_code == 404  # (it's there already)
        # One linked by hand while Relay runs is said too, and both load at the next start
        add_plugin(home, "byhand", "def setup(plugin):\n    pass\n")
        byhand = next(p for p in client.get("/api/plugins").json()["plugins"] if p["name"] == "byhand")
        assert byhand["enabled"] and not byhand["loaded"]
    with relay_app(tmp_path) as client:
        assert client.post("/api/login", json={"username": "me", "password": "correct horse"}, headers=H).status_code == 200
        assert {p["name"]: p["loaded"] for p in client.get("/api/plugins").json()["plugins"]} == {"byhand": True,
                                                                                                  "handy": True}


def test_a_plugins_tool_can_be_for_some_conversations_only(home, tmp_path):
    """`only`: a tool in the conversations it says yes to (a plugin's own agents, say), and no other's: not
    listed, not told of, refused when called. An `only` that fails says no, as the plugin's last error."""
    add_plugin(home, "picky", PICKY)
    with relay_app(tmp_path) as client:
        signed_in(client)
        relay = client.app.state.relay
        member, other = (client.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()["id"]
                         for _ in range(2))
        relay.plugins.found["picky"].plugin.store.set("members", [member])
        rows = {}
        for conv in (member, other):
            client.post(f"/api/conversations/{conv}/messages", json={"text": "linger"}, headers=H)
            rows[conv] = relay.db.one("SELECT * FROM conversations WHERE id = ?", (conv,))
        try:
            tokens = {conv: rows[conv]["tools_token"] for conv in rows}
            assert until(lambda: all(relay.delegation.run_for(t) is not None for t in tokens.values()))
            listed = {conv: {t["name"] for t in mcp(client, tokens[conv], "tools/list")["result"]["tools"]}
                      for conv in tokens}
            assert "picky_secret" in listed[member] and "picky_secret" not in listed[other]
            assert "picky_broken" not in listed[member] | listed[other]
            call = lambda conv: mcp(client, tokens[conv], "tools/call",  # noqa: E731
                                    {"name": "picky_secret", "arguments": {}})["result"]
            assert call(member)["content"][0]["text"] == "psst"
            assert call(other)["isError"]
            assert relay.runs._plugin_tools(member) == ["picky_secret"] and relay.runs._plugin_tools(other) == []
            assert "broken's only: ZeroDivisionError" in relay.plugins.found["picky"].errors["tool"]
        finally:
            for conv in rows:
                (pathlib.Path(rows[conv]["directory"]) / "done-lingering").write_text("")
            assert until(lambda: all(relay.runs.active_run_for(conv) is None for conv in rows))
        # Without a conversation (Settings → Plugins, its check), all of them
        assert {"picky_secret", "picky_broken"} <= {t["name"] for t in relay.plugins.tools()}
    # And refused at the call itself, whatever asks
    plugins = Plugins(folder=home / "plugins")
    plugins.load()
    plugins.found["picky"].plugin.store.set("members", ["m"])
    assert plugins.call_tool("picky_secret", ToolCall("m", "r", 1, str(tmp_path), {})) == "psst"
    with pytest.raises(PluginError, match="isn't for this conversation"):
        plugins.call_tool("picky_secret", ToolCall("x", "r", 1, str(tmp_path), {}))


def test_the_relay_command_calls_a_plugins_tool(home, tmp_path):
    """`relay plugin TOOL KEY=VALUE …`, one JSON object, or that on stdin: its tools/call, as the script sends it."""
    import http.server
    import subprocess

    from relay.tools_script import SCRIPT
    calls = []

    class Relay(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            calls.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "done"}]}})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body.encode())

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Relay)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    script = tmp_path / "relay"
    script.write_text(SCRIPT.replace("@PYTHON@", sys.executable).replace("@URL@", f"http://127.0.0.1:{server.server_port}/"))
    try:
        for fields, stdin in ((["text=hello there", "count=3"], ""), (['{"text": "as json"}'], ""),
                              ([], '{"text": "piped"}')):
            done = subprocess.run([sys.executable, str(script), "plugin", "notes_add", *fields], input=stdin,
                                  capture_output=True, text=True, timeout=30)
            assert done.returncode == 0 and done.stdout.strip() == "done", done.stderr
    finally:
        server.shutdown()
    assert [c["params"] for c in calls] == [
        {"name": "notes_add", "arguments": {"text": "hello there", "count": 3}},
        {"name": "notes_add", "arguments": {"text": "as json"}},
        {"name": "notes_add", "arguments": {"text": "piped"}}]


def test_a_plugin_gets_relays_api_with_a_password_of_its_own(home, tmp_path):
    add_plugin(home, "demo", DEMO)
    with relay_app(tmp_path) as client:
        signed_in(client)
        relay = client.app.state.relay
        plugin = relay.plugins.loaded()[0]
        sessions = []
        threads = [threading.Thread(target=lambda: sessions.append(plugin.sdk)) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        names = lambda: [p["name"] for p in relay.accounts.app_passwords(1)]  # noqa: E731
        assert names() == ["plugin demo"]  # (one, however many threads asked at once)
        session = sessions[0]
        assert {id(s) for s in sessions} == {id(session)}
        assert session.url == relay.runs.local_url and session._app_password.startswith("relay_")
        kept = home / "plugin-data" / ".app-passwords" / "demo"  # (apart from its data: never in what it clears)
        assert kept.stat().st_mode & 0o777 == 0o600 and not (home / "plugin-data" / "demo").exists()
        assert plugin.sdk is session
        relay.accounts.remove_app_password(1, relay.accounts.app_passwords(1)[0]["id"])  # (the user revoked it)
        assert plugin.sdk._app_password != session._app_password and names() == ["plugin demo"]
        import relay_sdk  # (on the path for plugins, at the top of their code too)
        assert relay_sdk.Session


def test_the_example_plugin_works_as_the_guide_says(home, tmp_path):
    (home / "plugins").mkdir(parents=True)
    (home / "plugins" / "notes").symlink_to(NOTES)  # (as its README says: a link in the plugins folder)
    with relay_app(tmp_path) as client:
        signed_in(client)
        relay = client.app.state.relay
        notes = next(p for p in client.get("/api/plugins").json()["plugins"] if p["name"] == "notes")
        assert notes["loaded"] and notes["script"] and notes["style"] and notes["tools"] == ["notes_add"]
        assert "export default function setup(relay)" in client.get("/plugins/notes/static/index.js").text
        added = client.post("/api/plugins/notes/notes", json={"text": " Buy milk "}, headers=H).json()
        assert added["text"] == "Buy milk" and added["by"] == "me"
        assert client.post("/api/plugins/notes/notes", json={"text": " "}, headers=H).json() == {
            "error": "A note needs some text"}
        long = client.post("/api/plugins/notes/notes", json={"text": "x" * 2001}, headers=H)  # (not cut, unsaid)
        assert long.status_code == 400 and long.json()["error"] == "A note is at most 2000 characters: this one has 2001"
        # An agent adds one with its tool; a run that fails leaves one (in a chat of Relay's pages)
        conv = client.post("/api/conversations", json={"provider_id": "fake"},
                           headers={**H, "x-relay-client": "web"}).json()
        client.post(f"/api/conversations/{conv['id']}/messages", json={"text": "linger"}, headers=H)
        token = relay.db.one("SELECT tools_token FROM conversations WHERE id = ?", (conv["id"],))["tools_token"]
        row = relay.db.one("SELECT * FROM conversations WHERE id = ?", (conv["id"],))
        try:
            assert until(lambda: relay.delegation.run_for(token) is not None)
            said = mcp(client, token, "tools/call", {"name": "notes_add", "arguments": {"text": "Tests pass"}})
            assert said["result"]["content"][0]["text"].startswith("Noted (")
            # (its arguments as its schema has them: `relay plugin notes_add text=2024` gives a number)
            said = mcp(client, token, "tools/call", {"name": "notes_add", "arguments": {"text": 2024}})
            assert not said["result"].get("isError")
            said = mcp(client, token, "tools/call", {"name": "notes_add", "arguments": {}})
            assert said["result"]["isError"] and said["result"]["content"][0]["text"].startswith("notes_add: text is needed")
        finally:
            (pathlib.Path(row["directory"]) / "done-lingering").write_text("")
            assert until(lambda: relay.runs.active_run_for(conv["id"]) is None)
        client.post(f"/api/conversations/{conv['id']}/messages", json={"text": "fail"}, headers=H)
        assert until(lambda: len(client.get("/api/plugins/notes/notes").json()) == 4)
        listed = client.get("/api/plugins/notes/notes").json()
        assert [(n["text"], n["by"], n["conversation"]) for n in listed] == [
            ("Buy milk", "me", None), ("Tests pass", "an agent", conv["id"]), ("2024", "an agent", conv["id"]),
            ("A run of fake failed: it broke", "Relay", conv["id"])]
        # Hourly, those older than 30 days go; one by one, or all
        notes = relay.plugins.found["notes"].plugin
        notes.store.set("notes", [{**listed[0], "at": time.time() - 31 * 86400}, *listed[1:]])
        notes.tasks[0].run()
        assert len(client.get("/api/plugins/notes/notes").json()) == 3
        assert json.loads((home / "plugin-data" / "notes" / "store.json").read_text())["notes"][0]["text"] == "Tests pass"
        assert client.delete(f"/api/plugins/notes/notes/{listed[1]['id']}", headers=H).json() == {"removed": listed[1]["id"]}
        assert client.delete("/api/plugins/notes/notes/nope", headers=H).status_code == 404
        assert client.delete("/api/plugins/notes/notes", headers=H).json() == {"removed": "all"}
        assert client.get("/api/plugins/notes/notes").json() == []


def test_the_guides_code_is_the_examples_word_for_word():
    quoted = re.findall(r"<!-- (notes/[\w/.]+) -->\n```\w+\n(.*?)\n```", GUIDE, re.S)
    assert len(quoted) >= 10
    for file, code in quoted:
        assert code in (ROOT / "plugins" / file).read_text(), f"{file} no longer has:\n{code}"


def test_the_guides_reference_names_the_whole_contract():
    """Every part of `plugin`, `Call`, `ToolCall` and the page's `relay` is in the guide's tables."""
    plugin = {*(n for n in vars(Plugin) if not n.startswith("_")), "name", "label", "version", "folder"}
    missing = sorted(n for n in plugin if not re.search(rf"^\| `{n}[`(]", GUIDE, re.M))
    assert not missing, f"not in the guide's plugin table: {missing}"
    fields = [f.name for kind in (Call, ToolCall) for f in dataclasses.fields(kind)]
    assert not [f for f in fields if f"| `{f}` |" not in GUIDE]
    script = (ROOT / "src" / "relay" / "static" / "js" / "plugins.js").read_text()
    page = re.search(r"function pluginApi\(.*?\n}\n", script, re.S).group(0)
    keys = set(re.findall(r"^    (\w+)(?:: |\()", page, re.M))
    assert keys >= {"api", "ui", "barItem", "page", "settingsTab", "navigate"}
    assert not [k for k in keys if f"`{k}" not in GUIDE.split("| `relay` |")[1]]
    # …and Relay's icons by name, all of them (agents in a WorkBox can't read ui.js)
    ui = (ROOT / "src" / "relay" / "static" / "js" / "ui.js").read_text()
    icons = re.findall(r"^  (\w+): '", ui[ui.index("const ICONS = {"):ui.index("\n};", ui.index("const ICONS = {"))], re.M)
    listed = re.search(r"Relay's:\s(.*?)\.\n", GUIDE[GUIDE.index("**Icons:**"):], re.S).group(1)
    assert re.findall(r"`(\w+)`", listed) == icons


def test_the_skill_carries_the_guide_and_the_example():
    """Agents in a WorkBox can't read Relay's docs/ or plugins/, but they do read its skills: relay-plugin-writer has
    copies of the guide and the example, kept the same. (After a change: copy them again.)"""
    copy = ROOT / "src" / "relay" / "builtin_skills" / "relay-plugin-writer" / "reference"
    assert (copy / "WRITING-A-PLUGIN.md").read_text() == GUIDE, "cp docs/WRITING-A-PLUGIN.md to the skill's reference/"
    files = lambda root: sorted(p.relative_to(root) for p in root.rglob("*")  # noqa: E731
                                if p.is_file() and "__pycache__" not in p.parts)
    assert files(copy / "notes") == files(NOTES)
    for path in files(NOTES):
        assert (copy / "notes" / path).read_bytes() == (NOTES / path).read_bytes(), f"cp plugins/notes/{path} too"


def test_a_plugins_store_keeps_each_change_whole(tmp_path):
    from relay.plugins import PluginError, Store
    store = Store(tmp_path / "store.json")
    assert store.get("n", 0) == 0 and store.keys() == []
    threads = [threading.Thread(target=lambda: [store.update("n", lambda n: n + 1, 0) for _ in range(50)])
               for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert store.get("n") == 400 and json.loads((tmp_path / "store.json").read_text()) == {"n": 400}
    kept = store.get("list", [])
    kept.append(1)  # (a copy: what's kept changes only through the store)
    assert store.get("list", []) == []
    with pytest.raises(TypeError):
        store.set("when", time)
    store.delete("n")
    assert Store(tmp_path / "store.json").keys() == []  # (read anew, as after a restart)
    (tmp_path / "broken.json").write_text("{half")
    with pytest.raises(PluginError, match="isn't JSON any more"):
        Store(tmp_path / "broken.json").get("x")


def test_a_tools_arguments_and_paths_are_checked():
    from relay.plugins import PluginError, ToolCall, checked
    schema = {"type": "object", "required": ["text"], "properties": {
        "text": {"type": "string"}, "count": {"type": "integer"}, "share": {"type": "number"},
        "loud": {"type": "boolean"}, "tags": {"type": "array"}}}
    assert checked(schema, {"text": 2024, "count": "3", "share": "0.5", "loud": "yes", "tags": ["a"]}) == {
        "text": "2024", "count": 3, "share": 0.5, "loud": True, "tags": ["a"]}
    assert checked(schema, {"text": True, "count": 2.0})["text"] == "true"
    for wrong, said in (({}, "text is needed"), ({"text": "x", "count": "many"}, "count is a number"),
                        ({"text": "x", "count": 2.5}, "count is a whole number"),
                        ({"text": "x", "loud": "maybe"}, "loud is true or false")):
        with pytest.raises(PluginError, match=said):
            checked(schema, wrong)
    call = ToolCall("c", "r", 1, "/srv/project", {})
    assert call.within("src/a.py") == pathlib.Path("/srv/project/src/a.py")
    assert call.within("/srv/project/b") == pathlib.Path("/srv/project/b")
    for outside in ("../etc/passwd", "/etc/passwd"):
        with pytest.raises(PluginError, match="isn't in this conversation's folder"):
            call.within(outside)


def test_the_hook_says_who_started_the_run(home, tmp_path):
    add_plugin(home, "ears", "heard = []\ndef setup(plugin):\n    plugin.on_run_ended(heard.append)\n")
    with relay_app(tmp_path) as client:
        signed_in(client)
        module = sys.modules["relay_plugins.ears"]
        mine = client.post("/api/conversations", json={"provider_id": "fake"}, headers={**H, "x-relay-client": "web"}).json()
        client.post(f"/api/conversations/{mine['id']}/messages", json={"text": "fail"}, headers=H)
        assert until(lambda: len(module.heard) == 1)
        scripted = client.post("/api/conversations", json={"provider_id": "fake", "temporary": True}, headers=H).json()
        client.post(f"/api/conversations/{scripted['id']}/messages", json={"text": "hello"}, headers=H)
        assert until(lambda: len(module.heard) == 2)
    first, second = module.heard
    assert (first["status"], first["error"], first["started_by"], first["temporary"]) == ("failed", "it broke", None, False)
    assert (second["status"], second["error"], second["started_by"], second["temporary"]) == (
        "succeeded", None, "Relay's API", True)
    assert set(first) == {"conversation_id", "run_id", "user_id", "provider_id", "model", "status", "error",
                          "subagent", "temporary", "started_by"}


def test_relay_plugins_on_the_command_line(home, capsys):
    from relay import cli
    add_plugin(home, "demo", DEMO)
    add_plugin(home, "typo", "def setup(plugin)\n")
    assert cli.main(["plugins"]) == 0
    listed = capsys.readouterr().out
    assert "demo  " in listed and "on" in listed and "typo" in listed and "can't be loaded" in listed
    assert cli.main(["plugins", "off", "demo"]) == 0 and config.load_settings().plugins_off == ["demo"]
    assert cli.main(["plugins", "on", "demo"]) == 0 and config.load_settings().plugins_off == []
    capsys.readouterr()
    assert cli.main(["plugins", "check", str(home / "plugins" / "demo")]) == 0
    checked = capsys.readouterr().out
    assert "Demo  (demo) loads." in checked and "/api/plugins/demo/items/{id}" in checked
    assert "tool   demo_steer (the Governor too)" in checked
    add_plugin(home, "picky", PICKY)
    assert cli.main(["plugins", "check", str(home / "plugins" / "picky")]) == 0
    assert "tool   picky_secret (some conversations only): Only for its members." in capsys.readouterr().out
    assert cli.main(["plugins", "check", str(home / "plugins" / "typo")]) == 1
    assert "typo can't be loaded" in capsys.readouterr().out


def test_a_plugin_in_a_workspace_root_is_said(home, tmp_path):
    """Agents work in the workspace roots: a plugin linked from there is one they could change."""
    folder = add_plugin(tmp_path / "ws", "mine")  # (tmp_path/ws/plugins/mine: in the root)
    (home / "plugins").mkdir(parents=True, exist_ok=True)
    (home / "plugins" / "mine").symlink_to(folder)
    with relay_app(tmp_path) as client:
        signed_in(client)
        assert next(p for p in client.get("/api/plugins").json()["plugins"] if p["name"] == "mine")["exposed"]
