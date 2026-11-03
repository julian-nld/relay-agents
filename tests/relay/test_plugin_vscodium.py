"""plugins/vscodium: Relay's extension for VSCodium, packed by the plugin as a .vsix and installed into the servers
Remote - SSH leaves on this machine (servers of this test's own), and its client (extension/lib) driven by Node
against a Relay of its own over HTTP, with a stand-in agent that changes a file."""

import asyncio
import base64
import importlib.util
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import textwrap
import threading
import time
import zipfile
from pathlib import Path

import pytest
import uvicorn

from relay import config
from relay.app import create_app
from relay.db import Database
from relay.providers import Registry

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugins" / "vscodium"
sys.path.insert(0, str(ROOT / "sdk" / "python"))
import relay_sdk  # noqa: E402

_spec = importlib.util.spec_from_file_location("relay_plugin_tests", Path(__file__).with_name("test_plugins.py"))
_helpers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_helpers)
H, home, relay_app, signed_in = _helpers.H, _helpers.home, _helpers.relay_app, _helpers.signed_in

# A stand-in agent: in its folder, a.txt's second line changed, said as a tool call; then its words
AGENT = textwrap.dedent("""\
    import json, pathlib, sys
    prompt = sys.stdin.read()
    def emit(**e): print(json.dumps(e), flush=True)
    emit(type="session", session="s1")
    if "change" in prompt:
        emit(type="tool_call", id="t1", name="Edit", input={"file_path": str(pathlib.Path("a.txt").resolve())})
        pathlib.Path("a.txt").write_text("one\\nTWO\\nthree\\n")
        emit(type="tool_result", id="t1", name="Edit", ok=True, output="changed")
    for word in ["Done", ": ", prompt[:20]]:
        emit(type="text", delta=word)
    emit(type="done", status="succeeded")
""")


def linked(home):
    (home / "plugins").mkdir(parents=True, exist_ok=True)
    (home / "plugins" / "vscodium").symlink_to(PLUGIN)


def server(folder, program, script):
    """A VSCodium or VS Code server as Remote - SSH leaves it: its program, a script of ours."""
    path = folder / program
    path.parent.mkdir(parents=True)
    path.write_text("#!/bin/sh\n" + script)
    path.chmod(0o755)
    return path


def test_the_plugin_packs_the_extension_as_a_vsix(home, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    linked(home)
    with relay_app(tmp_path) as client:
        signed_in(client)
        assert client.app.state.relay.plugins.found["vscodium"].working()
        version = json.loads((PLUGIN / "plugin.json").read_text())["version"]
        assert client.get("/api/plugins/vscodium/version").json() == {"version": version}
        got = client.get("/api/plugins/vscodium/extension").json()
        assert got["version"] == version and got["name"] == f"relay-{version}.vsix"
        data = base64.b64decode(got["data"])
        vsix = zipfile.ZipFile(io.BytesIO(data))
        names = vsix.namelist()
        assert names[:2] == ["extension.vsixmanifest", "[Content_Types].xml"]
        assert {"extension/package.json", "extension/extension.js", "extension/lib/client.js", "extension/media/chat.js",
                "extension/media/icon.png", "extension/README.md"} <= set(names)
        assert not [n for n in names if "__pycache__" in n or "/." in n]
        package = json.loads(vsix.read("extension/package.json"))
        assert package["version"] == version and package["extensionKind"] == ["workspace"]
        manifest = vsix.read("extension.vsixmanifest").decode()
        assert f'Id="relay" Version="{version}" Publisher="relay"' in manifest
        assert '"Microsoft.VisualStudio.Code.ExtensionKind" Value="workspace"' in manifest
        assert "<Icon>extension/media/icon.png</Icon>" in manifest
        types = vsix.read("[Content_Types].xml").decode()
        assert 'Extension=".js" ContentType="application/javascript"' in types
        assert 'Extension=".png" ContentType="image/png"' in types
        # The same files, the same .vsix (each file's time is fixed)
        assert base64.b64decode(client.get("/api/plugins/vscodium/extension").json()["data"]) == data
        # Every file the manifest points at is in it, and the extension's own files are all JavaScript Node takes
        for asset in ("extension/package.json", "extension/README.md", "extension/media/icon.png"):
            assert asset in names
        for name in (n for n in names if n.endswith(".js")):
            checked = subprocess.run(["node", "--check", "-"], input=vsix.read(name).decode(), capture_output=True,
                                     text=True)
            assert checked.returncode == 0, (name, checked.stderr)


def test_install_puts_it_in_the_servers_remote_ssh_left(home, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    linked(home)
    me = tmp_path / "me"
    monkeypatch.setenv("HOME", str(me))
    with relay_app(tmp_path) as client:
        signed_in(client)
        refused = client.post("/api/plugins/vscodium/install", headers=H)
        assert refused.status_code == 409 and "Open Remote - SSH" in refused.json()["error"]
        log = tmp_path / "asked.txt"
        # VSCodium's server (an older one too: the newest is taken), and VS Code's of its newer layout, which fails
        old = server(me, ".vscodium-server/bin/aaa/bin/codium-server", "exit 9\n")
        os.utime(old, (1, 1))
        server(me, ".vscodium-server/bin/bbb/bin/codium-server",
               f'echo "$@" >> {log}\ncp "$2" {tmp_path}/got.vsix\necho "Extension \'relay.vsix\' was successfully installed."\n')
        server(me, ".vscode-server/cli/servers/Stable-1/server/bin/code-server", "echo 'No space left' >&2\nexit 1\n")
        done = client.post("/api/plugins/vscodium/install", headers=H).json()
        assert done["installed"] == [
            {"product": "VSCodium", "ok": True, "said": "Extension 'relay.vsix' was successfully installed."},
            {"product": "VS Code", "ok": False, "said": "No space left"}]
        asked = log.read_text().split()
        assert asked[0] == "--install-extension" and asked[1].endswith("/relay.vsix") and asked[2] == "--force"
        assert zipfile.ZipFile(tmp_path / "got.vsix").read("extension/package.json")
        assert not Path(asked[1]).exists()  # (its .vsix was a temporary one)
        # None that installed it: said, in each one's words
        shutil.rmtree(me / ".vscodium-server")
        failed = client.post("/api/plugins/vscodium/install", headers=H)
        assert failed.status_code == 502 and failed.json()["error"] == "VS Code: No space left"


@pytest.fixture
def served(tmp_path, monkeypatch):
    """A Relay serving on a port of its own with the stand-in agent and an app password: (its address, the app
    password, the project folder, the app)."""
    monkeypatch.setenv("RELAY_HOME", str(tmp_path / "data"))
    agent = tmp_path / "agent.py"
    agent.write_text(AGENT)
    project = tmp_path / "ws" / "project"
    project.mkdir(parents=True)
    (project / "a.txt").write_text("one\ntwo\nthree\n")
    registry = Registry(tmp_path / "providers")
    monkeypatch.setattr(registry, "ensure_defaults", lambda: None)
    registry.add({"id": "fake", "name": "Fake agent", "kind": "command",
                  "options": {"command": [sys.executable, str(agent)]}})
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    settings = config.Settings(workspace_roots=[str(tmp_path / "ws")], chat_root=str(tmp_path / "chats"),
                               host="127.0.0.1", port=port)
    app = create_app(settings, Database(tmp_path / "relay.db"), registry)
    running = uvicorn.Server(uvicorn.Config(app, log_level="error", timeout_graceful_shutdown=1))
    loop = asyncio.new_event_loop()  # (the server's, for what a test has Relay do itself: computers.call)
    app.state.loop = loop
    thread = threading.Thread(target=loop.run_until_complete, args=(running.serve(sockets=[sock]),), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 30
        while not running.started and time.monotonic() < deadline:
            time.sleep(.02)
        assert running.started
        url = f"http://127.0.0.1:{port}"
        me = relay_sdk.Session(url)
        me.request("POST", "/api/setup", {"username": "me", "password": "correct horse"})
        password = me.request("POST", "/api/app-passwords", {"name": "VSCodium"})["password"]
        yield url, password, project, app
    finally:
        running.should_exit = True
        thread.join(30)
        sock.close()


# What the extension does, with its own client: a code task in the folder, a message, its answer as it comes, the
# turns from what Relay kept, and the changed file as it was
DRIVER = textwrap.dedent("""\
    const lib = (name) => require(process.argv[2] + "/" + name);
    const { Client } = lib("client.js");
    const { answer, apply, turns } = lib("events.js");
    const { before } = lib("patch.js");
    (async () => {
      const [url, password, folder] = process.argv.slice(3);
      const client = new Client(url, password, { version: "1.0.0" });
      const said = {};
      said.state = (await client.get("/api/state")).user.username;
      const made = await client.post("/api/conversations", { provider_id: "fake", mode: "code", directory: folder });
      const run = await client.post(`/api/conversations/${made.id}/messages`, { text: "change it", attachments: [] });
      const live = answer(run.run_id);
      const seen = [];
      await client.events(run.run_id, (event) => { seen.push(event.type); apply(live, event); });
      said.seen = seen;
      said.live = live.blocks.map((b) => b.type === "text" ? b.text : `${b.type}:${b.name}:${b.status}`);
      said.status = live.status;
      const convo = await client.get(`/api/conversations/${made.id}`);
      said.turns = turns(convo.messages).map((t) => t.role);
      said.external = convo.external;
      const changes = await client.get(`/api/conversations/${made.id}/changes`);
      const file = changes.files[0];
      const now = require("fs").readFileSync(require("path").join(folder, file.path), "utf8");
      said.before = before(now, file.diff);
      said.list = (await client.get("/api/conversations")).map((c) => [c.title, c.mode]);
      try { await client.post(`/api/conversations/nope/messages`, { text: "x" }); } catch (e) { said.refused = [e.status, e.message]; }
      process.stdout.write(JSON.stringify(said));
    })().catch((e) => { console.error(e); process.exit(1); });
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")
def test_the_extensions_client_works_with_relays_api(served, tmp_path):
    url, password, project, app = served
    driver = tmp_path / "driver.cjs"
    driver.write_text(DRIVER)
    done = subprocess.run(["node", str(driver), str(PLUGIN / "extension" / "lib"), url, password, str(project)],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    said = json.loads(done.stdout)
    assert said["state"] == "me"
    assert said["seen"][-1] == "done" and "tool_call" in said["seen"] and "tool_result" in said["seen"]
    assert said["live"] == ["tool:Edit:done", "Done: change it"] and said["status"] == "succeeded"
    assert said["turns"] == ["user", "assistant"]
    assert said["external"] == "Relay's API, with the app password “VSCodium”"
    assert (project / "a.txt").read_text() == "one\nTWO\nthree\n"
    assert said["before"] == "one\ntwo\nthree\n"
    assert said["list"][0][1] == "code"
    assert said["refused"][0] == 404


# This computer as one of Relay's: the extension's connector (lib/connector.js) polling a served Relay, which has it
# run a command, write a file and read it, as an agent's `relay run` and `push`/`pull` do
CONNECTOR = textwrap.dedent("""\
    const { Connector, token } = require(process.argv[2] + "/connector.js");
    const [url, script] = process.argv.slice(3);
    const key = token(require("fs").readFileSync(script, "utf8"));
    if (!key) { console.error("no token in the script"); process.exit(2); }
    new Connector({ url, token: key, say: (t) => console.log(t) }).start();
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")
def test_the_extension_is_a_computer_relays_agents_can_work_on(served, tmp_path):
    url, password, project, app = served
    me = relay_sdk.Session(url)
    me._app_password = password
    computer = me.request("POST", "/api/computers", {"name": "Laptop"})
    script = me.request("GET", f"/api/computers/{computer['id']}/script")
    assert re.search(r'^TOKEN = "[^"]+"$', script, re.M)  # (what the extension takes from it)
    (tmp_path / "relay-computer.py").write_text(script)
    (tmp_path / "connector.cjs").write_text(CONNECTOR)
    process = subprocess.Popen(["node", str(tmp_path / "connector.cjs"), str(PLUGIN / "extension" / "lib"), url,
                                str(tmp_path / "relay-computer.py")], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True)
    try:
        deadline = time.monotonic() + 20
        while not next(c for c in me.request("GET", "/api/computers") if c["id"] == computer["id"])["online"]:
            assert time.monotonic() < deadline and process.poll() is None, process.stderr.read()
            time.sleep(0.2)
        found = next(c for c in me.request("GET", "/api/computers") if c["id"] == computer["id"])
        assert found["info"]["client"] == "vscodium" and found["info"]["connector"] == 1

        def call(kind, **args):
            return asyncio.run_coroutine_threadsafe(app.state.relay.computers.call(found, kind, args, 20),
                                                    app.state.loop).result(40)

        ran = call("run", command="echo hello; pwd", cwd=str(tmp_path))
        assert ran["ok"] and ran["exit"] == 0 and ran["output"].split() == ["hello", str(tmp_path)]
        wrote = call("write", path=str(tmp_path / "out" / "note.txt"), content_b64=base64.b64encode(b"hi").decode())
        assert wrote == {"ok": True, "path": str(tmp_path / "out" / "note.txt"), "size": 2, "id": wrote["id"]}
        assert (tmp_path / "out" / "note.txt").read_text() == "hi"
        got = call("read", path=str(tmp_path / "out" / "note.txt"))
        assert base64.b64decode(got["content_b64"]) == b"hi"
    finally:
        process.kill()
        process.wait(10)


# Agents use VSCodium: the plugin's tools on Relay's own plugin host, each a question for one of the user's windows

ME = {"id": 1, "username": "me"}
TOOLS = ["vscodium_definition", "vscodium_hover", "vscodium_problems", "vscodium_references", "vscodium_symbols"]


class Host:
    """The plugin loaded by Relay's plugin host: a window's side of it (its routes) and an agent's (its tools), each
    called in one of the plugin's threads, as in Relay."""

    def __init__(self, folder):
        from relay.plugins import Plugins
        linked(folder)
        self.host = Plugins(folder=folder / "plugins")
        self.host.load()
        found = self.host.found["vscodium"]
        assert found.problem is None, found.problem
        self.plugin = found.plugin
        self.versions = {}

    def route(self, method, path, query=None, body=None, user=ME):
        from relay.plugins import Call, PluginError
        try:
            return 200, self.plugin.pool.submit(self.host.call, "vscodium", Call(method, path, {}, query or {}, body,
                                                                                  user)).result(30)
        except PluginError as exc:
            return exc.status, {"error": str(exc)}

    def tool(self, name, arguments, conversation="c1", run="r1", directory="/srv/project"):
        from relay.plugins import ToolCall
        return self.plugin.pool.submit(self.host.call_tool, f"vscodium_{name}",
                                       ToolCall(conversation, run, 1, directory, arguments))

    def window(self, client, folders=(), tasks=(), focused=False, user=ME):
        return self.route("POST", "windows", body={"client": client, "folders": list(folders), "tasks": list(tasks),
                                                   "focused": focused}, user=user)[0]

    def questions(self, client, wait=0):
        status, found = self.route("GET", "tools", {"client": client, "since": str(self.versions.get(client, -1)),
                                                    "wait": str(wait)})
        assert status == 200, found
        self.versions[client] = found["version"]
        return found["questions"]

    def next_question(self, client):
        for _ in range(50):
            found = self.questions(client, wait=1)
            if found:
                return found[-1]
        raise AssertionError(f"no question came to {client}")

    def answer(self, question, client, **result):
        return self.route("POST", f"tools/{question['id']}/result", body={"client": client, **result})[0]


def said(future, timeout=10):
    from relay.plugins import PluginError
    try:
        return "ok", future.result(timeout)
    except PluginError as exc:
        return "error", str(exc)


@pytest.fixture
def plugin_host(home, tmp_path):
    found = Host(tmp_path)
    yield found
    found.plugin.pool.shutdown(wait=False, cancel_futures=True)


def test_its_tools_need_a_window_and_check_what_they_get(plugin_host):
    assert sorted(t.name for t in plugin_host.plugin.tools) == TOOLS
    kind, text = said(plugin_host.tool("problems", {}))
    assert kind == "ok" and "VSCodium isn't connected" in text
    kind, text = said(plugin_host.tool("definition", {"path": "a.js", "line": 0}))
    assert kind == "error" and "line is a line's number, from 1" in text
    assert said(plugin_host.tool("symbols", {}))[0] == "error"
    assert plugin_host.route("GET", "tools", {"client": "nobody"})[0] == 404  # (a window says what it has first)


def test_a_question_goes_to_the_window_with_the_conversations_folder(plugin_host):
    plugin_host.window("laptop", folders=["/home/alex/site"], tasks=["c-site"], focused=True)
    plugin_host.window("remote", folders=["/srv/project"])
    # A code task in a folder on Relay's machine: the window that has it open, not the one in front
    agent = plugin_host.tool("definition", {"path": "src/a.js", "line": "12", "symbol": "go"}, directory="/srv/project/x")
    question = plugin_host.next_question("remote")
    assert (question["action"], question["args"], question["directory"]) == \
        ("definition", {"path": "src/a.js", "line": 12, "symbol": "go"}, "/srv/project/x")
    assert plugin_host.questions("laptop") == []
    assert plugin_host.answer(question, "laptop", ok=True, text="no") == 404  # (another window's)
    assert plugin_host.answer(question, "remote", ok=True, text="src/b.js:3:10") == 200
    assert said(agent) == ("ok", "src/b.js:3:10")
    # A computer task: the window that has its folder
    agent = plugin_host.tool("problems", {"path": "index.html"}, conversation="c-site", directory="/data/chats/c-site")
    question = plugin_host.next_question("laptop")
    assert question["conversation_id"] == "c-site"
    plugin_host.answer(question, "laptop", ok=False, error="No file index.html")
    assert said(agent) == ("error", "No file index.html")
    # Neither: the window the user had in front of them last
    agent = plugin_host.tool("symbols", {"query": "Foo"}, conversation="c-chat", directory="/data/chats/c-chat")
    plugin_host.answer(plugin_host.next_question("laptop"), "laptop", ok=True, text="class Foo")
    assert said(agent) == ("ok", "class Foo")


def test_a_question_ends_with_its_run_or_its_window(plugin_host, monkeypatch):
    plugin_host.window("w")
    agent = plugin_host.tool("hover", {"path": "a.py", "line": 1}, run="r7")
    plugin_host.next_question("w")
    plugin_host.host.run_ended({"run_id": "r7", "conversation_id": "c1", "status": "interrupted"})
    assert said(agent) == ("ok", "Cancelled: the run ended.")
    monkeypatch.setattr(sys.modules["relay_plugins.vscodium"], "GONE", 0.5)
    agent = plugin_host.tool("hover", {"path": "a.py", "line": 1})
    kind, text = said(agent, timeout=15)
    assert kind == "ok" and "VSCodium isn't connected" in text
