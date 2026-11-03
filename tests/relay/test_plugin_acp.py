"""plugins/acp: Relay's agents as one ACP agent. Its forwarder (static/relay-acp.py) runs as an ACP client starts it,
against a Relay over HTTP of its own (a port of its own: never this machine's), driven by hand and by Relay's own
ACP client (relay.providers.acp) with Relay's whole check."""

import base64
import importlib.util
import json
import queue
import socket
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from relay import config
from relay.app import create_app
from relay.db import Database
from relay.providers import Context, Registry, conformance, parse_template
from relay.providers.acp import AcpProvider

ROOT = Path(__file__).resolve().parents[2]
FORWARDER = ROOT / "plugins" / "acp" / "static" / "relay-acp.py"
sys.path.insert(0, str(ROOT / "sdk" / "python"))
import relay_sdk  # noqa: E402

# A stand-in agent: a tool call when asked, the prompt echoed, lingering or failing when asked, and who it is
AGENT = textwrap.dedent("""\
    import json, sys, time
    prompt = sys.stdin.read()
    name = sys.argv[2] if len(sys.argv) > 2 else "fake"
    def emit(**e): print(json.dumps(e), flush=True)
    emit(type="session", session="sess-1")
    if "tool" in prompt:
        emit(type="tool_call", id="t1", name="shell", input={"command": "ls"})
        emit(type="tool_result", id="t1", name="shell", ok=True, output="a.txt")
    if "think" in prompt:
        emit(type="reasoning", delta="Hmm.")
    for word in ["You", " said: ", prompt]:
        emit(type="text", delta=word)
    if "who" in prompt: emit(type="text", delta=" I am " + name)
    if "linger" in prompt: time.sleep(30)
    if "fail" in prompt:
        emit(type="error", message="It broke on purpose")
        emit(type="done", status="failed")
        sys.exit(0)
    emit(type="done", status="succeeded")
""")


@pytest.fixture
def relay(tmp_path, monkeypatch):
    """A Relay serving on a port of its own, an account and an app password made: (its address, its app password,
    the app)."""
    monkeypatch.setenv("RELAY_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(relay_sdk, "_default", None)
    agent = tmp_path / "agent.py"
    agent.write_text(AGENT)
    (tmp_path / "ws" / "project").mkdir(parents=True)
    registry = Registry(tmp_path / "providers")
    monkeypatch.setattr(registry, "ensure_defaults", lambda: None)
    registry.add({"id": "fake", "name": "Fake agent", "kind": "command",
                  "options": {"command": [sys.executable, str(agent), ""]}})
    registry.add({"id": "other", "name": "Other agent", "kind": "command",
                  "options": {"command": [sys.executable, str(agent), "", "other"]}})
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    settings = config.Settings(workspace_roots=[str(tmp_path / "ws")], chat_root=str(tmp_path / "chats"),
                               host="127.0.0.1", port=port)
    app = create_app(settings, Database(tmp_path / "relay.db"), registry)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 30
        while not server.started and time.monotonic() < deadline:
            time.sleep(.02)
        assert server.started
        url = f"http://127.0.0.1:{port}"
        me = relay_sdk.Session(url)
        me.request("POST", "/api/setup", {"username": "me", "password": "correct horse"})
        password = me.request("POST", "/api/app-passwords", {"name": "ACP client"})["password"]
        yield url, password, app
    finally:
        server.should_exit = True
        thread.join(30)
        sock.close()


class Client:
    """An ACP client by hand: the forwarder as a subprocess, JSON-RPC a line at a time."""

    def __init__(self, url, password, *args, answer=None):
        self.process = subprocess.Popen([sys.executable, str(FORWARDER), *args], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                        env={"PATH": "/usr/bin:/bin", "RELAY_URL": url, "RELAY_APP_PASSWORD": password})
        self.counter, self.updates, self.answers = 0, [], {}
        self.answer = answer  # (its requests: method, params → result)
        self.inbox = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.process.stdout:
            message = json.loads(line)
            if message.get("method") == "session/update":
                self.updates.append(message["params"]["update"])
            elif message.get("method"):  # (a request of the agent's)
                result = self.answer(message["method"], message["params"]) if self.answer else None
                self.send({"id": message["id"], "result": result})
            else:
                self.inbox.put(message)

    def send(self, message):
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
        self.process.stdin.flush()

    def call(self, method, params=None, timeout=30):
        self.counter += 1
        self.send({"id": self.counter, "method": method, "params": params or {}})
        while True:
            message = self.inbox.get(timeout=timeout)
            if message.get("id") == self.counter:
                return message

    def texts(self, kind):
        return "".join(u["content"]["text"] for u in self.updates if u["sessionUpdate"] == kind)

    def close(self):
        self.process.stdin.close()
        self.process.wait(10)


def test_the_forwarder_speaks_acp_with_relays_agents(relay, tmp_path):
    url, password, app = relay
    client = Client(url, password)
    try:
        started = client.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}})["result"]
        assert started["protocolVersion"] == 1 and started["agentInfo"]["name"] == "relay"
        assert started["agentCapabilities"]["loadSession"] and started["agentCapabilities"]["promptCapabilities"]["embeddedContext"]
        # A folder in a workspace root: a code task there, with every enabled agent to pick
        project = tmp_path / "ws" / "project"
        made = client.call("session/new", {"cwd": str(project), "mcpServers": []})["result"]
        session = made["sessionId"]
        row = app.state.relay.db.one("SELECT mode, directory, provider_id FROM conversations WHERE id = ?", (session,))
        assert row["mode"] == "code" and Path(row["directory"]).resolve() == project.resolve()
        agent, mode = made["configOptions"]
        assert agent["id"] == "model" and agent["category"] == "model" and agent["type"] == "select"
        assert {o["value"].split("|")[0] for o in agent["options"]} == {"fake", "other"}  # (a flat list: OpenHands')
        assert all("value" in o and " · " in o["name"] for o in agent["options"])
        assert agent["currentValue"].split("|")[0] == row["provider_id"]
        assert mode["category"] == "mode" and "build" in {o["value"] for o in mode["options"]}
        assert "governor" not in {o["value"] for o in mode["options"]} and made["modes"]["currentModeId"] == "build"
        # A turn: its tool call, its text, its end
        done = client.call("session/prompt", {"sessionId": session, "prompt": [
            {"type": "text", "text": "hello tool think"},
            {"type": "resource", "resource": {"uri": f"file://{project}/notes.md", "text": "remember this"}}]})
        assert done["result"] == {"stopReason": "end_turn"}
        call = next(u for u in client.updates if u["sessionUpdate"] == "tool_call")
        assert call["kind"] == "execute" and call["title"] == "shell: ls" and call["status"] == "in_progress"
        result = next(u for u in client.updates if u["sessionUpdate"] == "tool_call_update")
        assert result["status"] == "completed" and result["content"][0]["content"]["text"] == "a.txt"
        assert client.texts("agent_thought_chunk") == "Hmm."
        said = client.texts("agent_message_chunk")
        assert "You said: hello tool think" in said and "remember this" in said and f"{project}/notes.md" in said
        # Another agent, picked in the client
        options = client.call("session/set_config_option", {"sessionId": session, "configId": "model",
                                                            "value": "other|"})["result"]["configOptions"]
        assert options[0]["currentValue"] == "other|"
        client.updates.clear()
        assert client.call("session/prompt", {"sessionId": session, "prompt": [
            {"type": "text", "text": "who"}]})["result"] == {"stopReason": "end_turn"}
        assert client.texts("agent_message_chunk").endswith("I am other")
        assert client.call("session/set_mode", {"sessionId": session, "modeId": "plan"})["result"] == {}
        assert relay_sdk.conversation(session, relay_sdk.connect(url, app_password=password)).get()["agent_mode"] == "plan"
        # A failed run: its error
        failed = client.call("session/prompt", {"sessionId": session, "prompt": [{"type": "text", "text": "fail"}]})
        assert failed["error"]["message"] == "It broke on purpose"
        # A folder Relay can't work in: a chat
        chat = client.call("session/new", {"cwd": "/", "mcpServers": []})["result"]["sessionId"]
        assert app.state.relay.db.one("SELECT mode FROM conversations WHERE id = ?", (chat,))["mode"] == "chat"
        assert client.call("session/frobnicate", {})["error"]["code"] == -32601
    finally:
        client.close()
    # Another client goes on with the session: its history first
    again = Client(url, password)
    try:
        again.call("initialize", {"protocolVersion": 1})
        loaded = again.call("session/load", {"sessionId": session, "cwd": str(project), "mcpServers": []})["result"]
        assert loaded["configOptions"][0]["currentValue"] == "other|"
        replayed = [u["sessionUpdate"] for u in again.updates]
        assert replayed[:2] == ["user_message_chunk", "agent_message_chunk"]
        assert again.texts("user_message_chunk").startswith("hello tool think")
        assert again.call("session/load", {"sessionId": "nope", "cwd": "/", "mcpServers": []})["error"]["code"] == -32002
    finally:
        again.close()


def test_a_turn_is_stopped_and_a_wrong_password_said(relay, tmp_path):
    url, password, _ = relay
    client = Client(url, password, "--provider", "fake", "--chat")
    try:
        client.call("initialize", {"protocolVersion": 1})
        made = client.call("session/new", {"cwd": str(tmp_path / "ws" / "project"), "mcpServers": []})["result"]
        assert {o["value"].split("|")[0] for o in made["configOptions"][0]["options"]} == {"fake"}  # (--provider)
        client.counter += 1
        client.send({"id": client.counter, "method": "session/prompt",
                     "params": {"sessionId": made["sessionId"], "prompt": [{"type": "text", "text": "linger"}]}})
        deadline = time.monotonic() + 15
        while "linger" not in client.texts("agent_message_chunk") and time.monotonic() < deadline:
            time.sleep(0.05)
        client.send({"method": "session/cancel", "params": {"sessionId": made["sessionId"]}})
        assert client.inbox.get(timeout=20)["result"] == {"stopReason": "cancelled"}
    finally:
        client.close()
    wrong = Client(url, "relay_wrong")
    try:
        refused = wrong.call("initialize", {"protocolVersion": 1})["error"]
        assert refused["code"] == -32000 and "RELAY_APP_PASSWORD" in refused["message"]
    finally:
        wrong.close()
    # (one request, then its input ends, as `echo … | relay-acp.py`: still answered)
    once = subprocess.run([sys.executable, str(FORWARDER)], input=json.dumps(
        {"jsonrpc": "2.0", "id": 7, "method": "initialize", "params": {"protocolVersion": 1}}) + "\n",
        capture_output=True, text=True, timeout=30, env={"PATH": "/usr/bin:/bin", "RELAY_URL": url,
                                                          "RELAY_APP_PASSWORD": password})
    assert json.loads(once.stdout)["result"]["agentInfo"]["name"] == "relay"


def test_a_permission_relay_leaves_to_the_user_goes_to_the_client(monkeypatch):
    monkeypatch.setattr(sys, "dont_write_bytecode", True)  # (no __pycache__ in the folder Relay serves)
    spec = importlib.util.spec_from_file_location("relay_acp", FORWARDER)
    forwarder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(forwarder)
    answered = []

    class Question:
        def __init__(self, message, event):
            self.event = event

        def answer(self, value):
            answered.append((self.event["id"], value))

    monkeypatch.setattr(forwarder.relay_sdk, "Question", Question)
    one = forwarder.Forwarder(None, None, False, None)
    asked = []
    one.ask = lambda method, params: asked.append((method, params)) or {"outcome": {"outcome": "selected",
                                                                                    "optionId": "allow"}}
    one.permission("s1", None, {"type": "permission", "id": "p1", "action": "bash", "resources": ["rm x.txt"]})
    method, params = asked[0]
    assert method == "session/request_permission" and params["toolCall"]["title"] == "bash: rm x.txt"
    assert params["toolCall"]["kind"] == "execute" and [o["kind"] for o in params["options"]] == ["allow_once", "reject_once"]
    assert answered == [("p1", True)]
    # Relay's own options (a paid subagent, say): passed on, the chosen one answered
    one.ask = lambda method, params: {"outcome": {"outcome": "selected", "optionId": "free"}}
    one.permission("s1", None, {"type": "permission", "id": "p2", "action": "subagent", "resources": ["x"], "options": [
        {"id": "free", "label": "A free model instead", "outcome": "free"}, {"id": "no", "label": "No", "outcome": "denied"}]})
    assert answered[-1] == ("p2", "free")
    one.ask = lambda method, params: {"outcome": {"outcome": "cancelled"}}  # (the client cancelled the turn: a no)
    one.permission("s1", None, {"type": "permission", "id": "p3", "action": "edit", "resources": ["a.py"]})
    assert answered[-1] == ("p3", False)


def test_with_ask_in_relay_a_permission_waits_in_relay(monkeypatch):
    """--ask-in-relay (a client that allows anything unasked, as OpenHands does): the client is told, Relay asks."""
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    spec = importlib.util.spec_from_file_location("relay_acp", FORWARDER)
    forwarder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(forwarder)

    class Message:
        def events(self):
            yield {"type": "permission", "id": "p1", "action": "bash", "resources": ["rm x.txt"]}
            yield {"type": "done", "status": "succeeded"}

    class Conversation:
        def send(self, text, stream, files=()):
            return Message()

    monkeypatch.setattr(forwarder.relay_sdk, "conversation", lambda session, relay: Conversation())
    for ask_in_relay in (True, False):
        one = forwarder.Forwarder(None, None, False, None, ask_in_relay=ask_in_relay)
        one._signed_in = lambda: None
        updates, asked = [], []
        one.update = lambda session, update, updates=updates: updates.append(update)
        one.permission = lambda *args, asked=asked: asked.append(args)
        assert one.prompt({"sessionId": "s1", "prompt": [{"type": "text", "text": "go"}]}) == {"stopReason": "end_turn"}
        time.sleep(0.2)  # (a permission for the client is asked in a thread of its own)
        if ask_in_relay:
            assert asked == [] and "asks to be allowed: bash: rm x.txt. Answer it in Relay" in \
                updates[0]["content"]["text"]
        else:
            assert len(asked) == 1 and updates == []


def test_images_and_files_a_prompt_embeds_go_along_as_attachments(relay):
    """An image pasted in OpenHands comes as base64 in the prompt, and so does a file that isn't text: each becomes a
    Relay attachment, as one attached on Relay's page does, so the agent gets it."""
    url, password, app = relay
    client = Client(url, password)
    try:
        started = client.call("initialize", {"protocolVersion": 1})["result"]
        assert started["agentCapabilities"]["promptCapabilities"]["image"]
        session = client.call("session/new", {"cwd": "/", "mcpServers": []})["result"]["sessionId"]
        picture, data = b"\x89PNG not really", bytes(range(256))
        assert client.call("session/prompt", {"sessionId": session, "prompt": [
            {"type": "text", "text": "what is this"},
            {"type": "image", "mimeType": "image/png", "data": base64.b64encode(picture).decode()},
            {"type": "resource", "resource": {"uri": "file:///tmp/data.bin", "mimeType": "application/octet-stream",
                                              "blob": base64.b64encode(data).decode()}}]})["result"] == \
            {"stopReason": "end_turn"}
        said = client.texts("agent_message_chunk")
        assert "(image.png, image/png)" in said and "(data.bin, application/octet-stream)" in said
        assert "passes none on" not in said and "isn't text" not in said
        sent = relay_sdk.conversation(session, relay_sdk.connect(url, app_password=password)).get()["messages"][0]
        assert [(a["name"], a["mime"]) for a in sent["attachments"]] == [
            ("image.png", "image/png"), ("data.bin", "application/octet-stream")]
        stored = app.state.relay.uploads.in_conversation(session, [a["id"] for a in sent["attachments"]])
        assert {a.name: a.path.read_bytes() for a in stored} == {"image.png": picture, "data.bin": data}
        # An image alone
        client.updates.clear()
        assert client.call("session/prompt", {"sessionId": session, "prompt": [
            {"type": "image", "mimeType": "image/jpeg", "data": base64.b64encode(picture).decode()}]})["result"] == \
            {"stopReason": "end_turn"}
        assert "(image.jpg, image/jpeg)" in client.texts("agent_message_chunk")
    finally:
        client.close()


def test_relays_own_acp_client_works_with_it(relay, tmp_path):
    """Two ends of ACP agreeing: Relay's ACP provider kind, with the forwarder as its agent, passes Relay's check."""
    url, password, _ = relay
    provider = AcpProvider(parse_template({"id": "relay-acp", "name": "Relay over ACP", "kind": "acp", "options": {
        "command": [sys.executable, str(FORWARDER), "--provider", "fake"],
        "env": {"RELAY_URL": url, "RELAY_APP_PASSWORD": password}}}))
    folder = tmp_path / "scratch"
    folder.mkdir()
    turn = provider.start(Context("c1", folder, "chat", None, None), "say hi")
    events = list(turn.events())
    assert "You said:" in "".join(e["delta"] for e in events if e["type"] == "text")
    assert events[-1] == {"type": "done", "status": "succeeded"}
    results = {r["name"]: r for r in conformance.check(provider, None, tmp_path / "checks")}
    assert results["A turn"]["ok"] and results["Going on"]["ok"], results


def test_a_client_in_a_container_works_in_relays_folders(relay, tmp_path, monkeypatch):
    """--map: a folder the client sees elsewhere (/projects in OpenHands' container) is Relay's own; --skip-context:
    what a client adds after the prompt for its own agent (OpenHands' skills) isn't passed on."""
    url, password, app = relay
    client = Client(url, password, "--map", f"/projects={tmp_path / 'ws'}", "--skip-context")
    try:
        client.call("initialize", {"protocolVersion": 1})
        session = client.call("session/new", {"cwd": "/projects/project", "mcpServers": []})["result"]["sessionId"]
        row = app.state.relay.db.one("SELECT mode, directory FROM conversations WHERE id = ?", (session,))
        assert row["mode"] == "code" and Path(row["directory"]).resolve() == (tmp_path / "ws" / "project").resolve()
        assert client.call("session/prompt", {"sessionId": session, "prompt": [
            {"type": "text", "text": "hello"},
            {"type": "resource_link", "name": "notes.md", "uri": "file:///projects/project/notes.md"},
            {"type": "text", "text": "<SKILLS>\nThe following skills are available…</SKILLS>"}]})["result"]["stopReason"] == "end_turn"
        said = client.texts("agent_message_chunk")
        assert f"{tmp_path / 'ws' / 'project' / 'notes.md'}" in said and "SKILLS" not in said
    finally:
        client.close()
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    spec = importlib.util.spec_from_file_location("relay_acp", FORWARDER)
    forwarder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(forwarder)
    one = forwarder.Forwarder(None, None, False, None, ["/projects=/home/me/projects"])
    assert one.to_relay("/projects") == "/home/me/projects" and one.to_relay("/projectsx/a") == "/projectsx/a"
    tool = forwarder._tool({"id": "t1", "name": "edit", "input": {"file_path": "/home/me/projects/site/a.py"}},
                           one.to_client)
    assert tool["locations"] == [{"path": "/projects/site/a.py"}] and tool["title"] == "edit: /projects/site/a.py"
    # (without --skip-context: the client's own context, a paragraph of its own)
    assert forwarder._text([{"type": "text", "text": "say ok"}, {"type": "text", "text": "<SKILLS>x</SKILLS>"}]) == \
        "say ok \n\n<SKILLS>x</SKILLS>"
    # An image the prompt only links to: where it is; ones it embeds: files, each in a folder of its own
    assert forwarder._text([{"type": "text", "text": "see"}, {"type": "image", "mimeType": "image/png", "data": "",
                                                              "uri": "file:///projects/a.png"}], one.to_relay) == \
        "see /home/me/projects/a.png"
    embedded = tmp_path / "embedded"
    embedded.mkdir()
    files = forwarder._files([{"type": "image", "mimeType": "image/png", "data": "eA=="},
                              {"type": "image", "mimeType": "image/png", "data": "eQ=="},
                              {"type": "image", "mimeType": "image/jpeg", "data": "eg==", "uri": "file:///projects/photo"},
                              {"type": "text", "text": "x"}], embedded)
    assert [(f.name, f.read_bytes()) for f in files] == [("image.png", b"x"), ("image.png", b"y"), ("photo.jpg", b"z")]
