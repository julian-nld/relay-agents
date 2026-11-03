"""Relay's Python SDK (sdk/python/relay_sdk) against a real Relay over HTTP: signing in, conversations,
answers done and as they come, attachments, the VPN choice, and the wheel Relay serves for pip."""

import base64
import json
import os
import socket
import subprocess
import sys
import textwrap
import threading
import time
import urllib.request
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
import uvicorn

from relay import __version__, config
from relay.app import create_app
from relay.db import Database
from relay.providers import Registry

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sdk" / "python"))
import relay_sdk

# A stand-in agent: a tool call when asked, the prompt echoed (slowly when asked), lingering when asked; it says
# who it is (its provider), its mode's instructions, and writes a file, when asked.
AGENT = textwrap.dedent("""\
    import json, os, sys, time
    prompt = sys.stdin.read()
    name = sys.argv[2] if len(sys.argv) > 2 else "fake"
    def emit(**e): print(json.dumps(e), flush=True)
    emit(type="session", session="sess-1")
    if "tool" in prompt:
        emit(type="tool_call", id="t1", name="shell", input={"command": "ls"})
        emit(type="tool_result", id="t1", name="shell", ok=True, output="a.txt")
    if "write" in prompt:
        with open("agent.txt", "a") as fh:
            fh.write(prompt + "\\n")
    for word in ["You", " said: ", prompt]:
        emit(type="text", delta=word)
        if "slow" in prompt: time.sleep(0.3)
    if "who" in prompt: emit(type="text", delta=" I am " + name)
    if "which mode" in prompt: emit(type="text", delta=" mode=" + os.environ.get("RELAY_MODE_INSTRUCTIONS", ""))
    if "linger" in prompt: time.sleep(30)
    if "timed request" in prompt: time.sleep(0.2)  # (a go-on: not hundreds of them, each a few ms apart)
    emit(type="usage", tokens={"input": 5, "output": 3}, cost=0.01)
    emit(type="done", status="succeeded")
""")


@pytest.fixture
def relay(tmp_path, monkeypatch):
    """A Relay serving on a port of its own, its first account made: (its address, the app)."""
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
        deadline = time.monotonic() + 30  # (a busy machine: the suite in parallel)
        while not server.started and time.monotonic() < deadline:
            time.sleep(.02)
        assert server.started
        url = f"http://127.0.0.1:{port}"
        relay_sdk.Session(url).request("POST", "/api/setup", {"username": "me", "password": "correct horse"})
        yield url, app
    finally:
        server.should_exit = True
        thread.join(30)
        sock.close()


def test_a_conversation_from_signing_in_to_its_answers(relay, tmp_path):
    url, _ = relay
    with pytest.raises(relay_sdk.RelayError) as wrong:
        relay_sdk.login("me", "not it", url=url)
    assert wrong.value.status == 401 and str(wrong.value) == "Wrong username or password"
    with pytest.raises(relay_sdk.RelayError, match="asks for a login"):
        relay_sdk.connect(url)
    user = relay_sdk.login("me", "correct horse", url=url)
    convo_id = relay_sdk.convo.create(user, title="From the SDK", workdir=str(tmp_path / "ws" / "project"))
    found = relay_sdk.convo.get(convo_id)  # (the default session from now on)
    assert (found["title"], found["mode"], found["provider_id"], found["vpn"]) == ("From the SDK", "code", "fake", None)
    # An answer, done
    message = relay_sdk.convo.send(convo_id, "use a tool, please")
    assert message.status == "succeeded" and message.response == "You said: use a tool, please"
    assert message.tools_called == [{"name": "shell", "input": {"command": "ls"}, "ok": True, "output": "a.txt"}]
    assert "> 🔧 **shell** `ls` ✓" in message.text and message.text.rstrip().endswith("You said: use a tool, please")
    assert message.json["role"] == "assistant" and [e["type"] for e in message.json["events"]][-1] == "done"
    assert message.elapsed_time > 0 and message.usage == {"tokens": {"input": 5, "output": 3}, "cost": 0.01}
    # An answer as it comes: its text in parts, then the same as one that's done
    coming = relay_sdk.convo.send(convo_id, "slow, please", stream=True)
    assert list(coming) == ["You", " said: ", "slow, please"]
    assert coming.response == "You said: slow, please" and coming.elapsed_time >= 0.6 and coming.tools_called == []
    # A file along; the conversations; its answers read back; renamed
    notes = tmp_path / "notes.txt"
    notes.write_text("Some notes\n")
    relay_sdk.convo.send(convo_id, "read it", files=[notes])
    asked = [m for m in relay_sdk.convo.get(convo_id)["messages"] if m["role"] == "user"][-1]
    assert [a["name"] for a in asked["attachments"]] == ["notes.txt"]
    assert relay_sdk.convo.list()[0]["id"] == convo_id
    back = relay_sdk.convo.messages(convo_id)
    assert [m.response for m in back][:2] == ["You said: use a tool, please", "You said: slow, please"]
    assert back[2].response.startswith("You said: read it") and "notes.txt" in back[2].response  # (its path, told)
    assert back[0].tools_called[0]["name"] == "shell" and back[0].elapsed_time >= 0  # (Relay's measure: read back too)
    stored = [m for m in relay_sdk.convo.get(convo_id)["messages"] if m["role"] == "assistant"][0]
    assert stored["run_finished"] >= stored["run_started"] > 0  # (the run's own times, from Relay)
    relay_sdk.convo.rename(convo_id, "Renamed")
    assert relay_sdk.convo.get(convo_id)["title"] == "Renamed"
    # Stopped while it works; then gone
    lingering = relay_sdk.convo.send(convo_id, "linger", stream=True)
    deadline = time.monotonic() + 10
    while not relay_sdk.convo.get(convo_id)["running_run"] and time.monotonic() < deadline:
        time.sleep(.05)
    assert relay_sdk.convo.stop(convo_id) is True
    assert lingering.status == "interrupted" and relay_sdk.convo.stop(convo_id) is False
    relay_sdk.convo.delete(convo_id)
    with pytest.raises(relay_sdk.RelayError) as gone:
        relay_sdk.convo.get(convo_id)
    assert gone.value.status == 404
    # Providers and their models; a chat on the agent of the latest conversation
    assert [p["id"] for p in relay_sdk.convo.providers()] == ["fake", "other"]
    assert relay_sdk.convo.models("fake")[0]["id"] == ""
    chat = relay_sdk.convo.create()
    assert relay_sdk.convo.get(chat)["mode"] == "chat" and isinstance(chat, relay_sdk.Conversation)


def test_a_script_signs_in_with_an_app_password(relay, monkeypatch):
    """Without the account's password: an app password (Settings → Account), given to connect() or in
    $RELAY_APP_PASSWORD, where a script picks it up by itself."""
    url, _ = relay
    me = relay_sdk.login("me", "correct horse", url=url)
    made = me.request("POST", "/api/app-passwords", {"name": "the nightly script"})
    monkeypatch.setattr(relay_sdk, "_default", None)
    user = relay_sdk.connect(url, app_password=made["password"], provider="fake")
    assert user.username == "me" and repr(user) == f"<relay_sdk.Session me at {url}>"
    chat = relay_sdk.convo.create(title="Nightly")
    assert chat.get()["external"] == "the Python SDK, with the app password “the nightly script”"
    message = chat.send("hello")
    assert message.response == "You said: hello"
    assert [e["type"] for e in user.events(message.run_id)][-1] == "done"  # (its stream too)
    # Behind a proxy with a sign-in of its own: its name and password in the address, the app password to Relay
    monkeypatch.setattr(relay_sdk, "_default", None)
    behind = relay_sdk.connect(url.replace("://", "://alex:caddy@"), app_password=made["password"], provider="fake")
    assert behind.username == "me" and behind.url == url
    reply = relay_sdk.convo.create(behind, title="Behind").send("hi")
    assert reply.response == "You said: hi" and [e["type"] for e in behind.events(reply.run_id)][-1] == "done"
    # In the environment: no connect() needed
    monkeypatch.setattr(relay_sdk, "_default", None)
    monkeypatch.setenv("RELAY_URL", url)
    monkeypatch.setenv("RELAY_APP_PASSWORD", made["password"])
    assert "Nightly" in [c["title"] for c in relay_sdk.convo.list()]
    # Revoked: said so
    me.request("DELETE", f"/api/app-passwords/{made['id']}")
    monkeypatch.setattr(relay_sdk, "_default", None)
    with pytest.raises(relay_sdk.RelayError, match="doesn't know that app password") as revoked:
        relay_sdk.convo.list()
    assert revoked.value.status == 401


def test_a_conversation_through_a_vpn_tunnel(relay):
    """uses_vpn: its folder in a tunnel, kept as Settings keeps it (nothing started); a wrong choice makes nothing."""
    url, app = relay
    relay_sdk.login("me", "correct horse", url=url)
    with pytest.raises(relay_sdk.RelayError, match="There's no VPN tunnel: make one in Settings"):
        relay_sdk.convo.create(uses_vpn=True)
    settings = app.state.relay.settings
    profile = {"id": "p1", "name": "Windscribe", "env": {"VPN_SERVICE_PROVIDER": "windscribe"}}
    settings.vpn = {"profiles": [profile], "tunnels": [
        {"id": "t1", "name": "Private", "folders": [], "exits": [{"profile": "p1", "place": {}}]}]}
    chat = relay_sdk.convo.create(uses_vpn=True)
    found = relay_sdk.convo.get(chat)
    assert found["vpn"] == "Private"
    assert config.load_settings().vpn["tunnels"][0]["folders"] == [str(Path(found["directory"]).resolve())]
    settings.vpn["tunnels"].append({"id": "t2", "name": "Other", "folders": [], "exits": [{"profile": "p1", "place": {}}]})
    with pytest.raises(relay_sdk.RelayError, match="Which VPN tunnel\\? There are 2: Private, Other"):
        relay_sdk.convo.create(uses_vpn=True)
    with pytest.raises(relay_sdk.RelayError, match="No VPN tunnel 'Nope'"):
        relay_sdk.convo.create(uses_vpn="Nope")
    other = relay_sdk.convo.create(uses_vpn="Other")
    assert relay_sdk.convo.get(other)["vpn"] == "Other"
    assert len(relay_sdk.convo.list()) == 2  # (the refused ones made nothing)
    # Undone: the folder is out of the tunnel again
    session = relay_sdk._session(None)
    assert session.request("PATCH", f"/api/conversations/{other}", {"uses_vpn": None})["vpn"] is None
    assert config.load_settings().vpn["tunnels"][1]["folders"] == []


def test_relay_serves_the_sdk_for_pip(relay, tmp_path):
    url, _ = relay
    with urllib.request.urlopen(f"{url}/sdk/") as response:
        page = response.read().decode()
    wheel_url = f"{url}/sdk/relay_sdk-{__version__}-py3-none-any.whl"
    assert f"pip install {wheel_url}" in page and "from relay_sdk import *" in page
    with urllib.request.urlopen(wheel_url) as response:
        archive = zipfile.ZipFile(BytesIO(response.read()))
    info = f"relay_sdk-{__version__}.dist-info"
    assert sorted(archive.namelist()) == sorted(["relay_sdk/__init__.py", "resl/__init__.py", f"{info}/METADATA",
                                                 f"{info}/WHEEL", f"{info}/RECORD"])
    assert {f"Version: {__version__}", "License: MIT"} <= set(archive.read(f"{info}/METADATA").decode().splitlines())
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(f"{url}/sdk/relay_sdk-0.0.1-py3-none-any.whl")
    # pip installs it from Relay, offline, and it's the same version
    target = tmp_path / "installed"
    done = subprocess.run([sys.executable, "-m", "pip", "install", "--no-deps", "--no-index", "--no-cache-dir",
                           "--disable-pip-version-check", "--target", str(target), wheel_url],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
    version = subprocess.run([sys.executable, "-c", "import relay_sdk, resl, json; "
                              "print(json.dumps([relay_sdk.__version__, resl.__version__]))"],
                             capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(target)})
    assert json.loads(version.stdout) == [__version__, __version__] == [relay_sdk.__version__] * 2


def test_a_conversation_as_an_object_its_agent_instructions_tools_and_turns(relay):
    """A Conversation is its id too; another agent from the next turn on; its own instructions; Relay's tools on
    and off; /timed with its go-ons, the queue, followed until it's quiet; retry, edit and fork."""
    url, _ = relay
    relay_sdk.login("me", "correct horse", url=url, provider="fake")
    chat = relay_sdk.convo.create(title="Objects")
    assert isinstance(chat, str) and relay_sdk.convo.get(chat)["title"] == chat.title == "Objects"
    assert relay_sdk.conversation(str(chat)).provider == "fake"
    # Another agent, from the next turn on
    chat.switch(provider="other")
    answered = chat.send("who are you?")
    assert (chat.provider, answered.provider) == ("other", "other") and answered.response.endswith("I am other")
    # Its own instructions, to every agent; Relay's tools, on and off
    chat.instructions = "Answer in Dutch."
    assert chat.instructions == "Answer in Dutch."
    said = chat.send("which mode").response
    assert said.endswith("mode=For this conversation:\nAnswer in Dutch.")
    chat.tools = {"web": False, "subagents": False}
    assert chat.tools == {"web": False, "memory": True, "subagents": False, "computers": True}
    chat.tools = {"web": True}
    assert chat.tools["web"] and not chat.tools["subagents"]
    # /timed: kept at it (its go-ons are Relay's turns), followed until it's quiet; a task starting with a number
    first = chat.send("3 quick things", timed=(0.05, 0.1))
    assert first.response == "You said: 3 quick things"
    chat.wait()
    went_on = [m for m in chat.get()["messages"] if m["role"] == "user" and (m.get("relay_action") or {}).get("kind")
               == "timed"]
    assert went_on and chat.timer["max"] > chat.timer["started"]
    chat.stop_timer()
    # The queue, followed: the slow one and then what was queued meanwhile
    chat.send("slow, please", stream=True)
    chat.queue("queued after it")
    assert [m.response for m in chat.follow()] == ["You said: slow, please", "You said: queued after it"]
    # Retry, edit, fork
    again = chat.retry()
    assert again.response == "You said: queued after it"
    edited = chat.edit(answered, "who is it now?")
    assert edited.response.startswith("You said: who is it now?")
    assert [m.response for m in chat.messages()][-1] == edited.response and len(chat.messages()) == 1
    fork = chat.fork(at=edited)
    assert isinstance(fork, relay_sdk.Conversation) and fork != chat and len(fork.messages()) == 1


def test_an_agents_questions_are_answered_by_the_script(relay, monkeypatch):
    """on_question gets each question and its answer reaches the agent; without it, they're listed."""
    from relay.providers.base import ProviderRun
    url, app = relay
    registry = app.state.relay.registry
    real = registry.provider
    asking = real("fake")
    got = []

    class Asking(ProviderRun):
        def __init__(self):
            self.answered = threading.Event()

        def events(self):
            yield {"type": "form", "id": "frm_1", "title": "Which colour?", "status": "pending",
                   "fields": [{"key": "colour", "type": "text", "title": "Colour"}]}
            self.answered.wait(10)
            yield {"type": "text", "delta": f"You chose {got[-1]['colour'] if got and got[-1] else 'nothing'}"}
            yield {"type": "done", "status": "succeeded"}

        def answer_form(self, request_id, value):
            got.append(value)
            self.answered.set()

        def interrupt(self):
            self.answered.set()
    monkeypatch.setattr(asking, "start", lambda context, prompt: Asking())
    monkeypatch.setattr(registry, "provider", lambda provider_id: asking if provider_id == "fake" else real(provider_id))
    relay_sdk.login("me", "correct horse", url=url, provider="fake")
    chat = relay_sdk.convo.create()
    seen = []

    def on_question(question):
        seen.append((question.kind, question.title, [f["key"] for f in question.fields]))
        return {"colour": "green"}
    message = chat.send("ask me", on_question=on_question)
    assert seen == [("form", "Which colour?", ["colour"])] and got == [{"colour": "green"}]
    assert message.response == "You chose green" and message.questions[0].answered
    assert repr(message).endswith(" succeeded>")  # (how it ended: a failed one says so)
    # By hand, while it comes
    coming = chat.send("ask me again", stream=True)
    for event in coming.events():
        if event.get("type") == "form" and event.get("status") == "pending":
            coming.answer(event["id"], {"colour": "blue"})
    assert coming.response == "You chose blue"


def test_a_code_task_from_its_changes_to_a_commit(relay, tmp_path):
    """changes(), rewind to its start, commit; on a branch of its own, merge."""
    url, _ = relay
    project = tmp_path / "ws" / "repo"
    project.mkdir()
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "me@example.org"], ["config", "user.name", "Me"]):
        subprocess.run(["git", *args], cwd=project, check=True)
    (project / "README.md").write_text("# Repo\n")
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(["git", "commit", "-qm", "Start"], cwd=project, check=True)
    relay_sdk.login("me", "correct horse", url=url, provider="fake")
    task = relay_sdk.convo.create(workdir=str(project))
    task.send("write a file")
    changed = task.changes()
    assert [f["path"] for f in changed["files"]] == ["agent.txt"] and changed["summary"]["files"] == 1
    assert task.rewind("start")["undo"] and not (project / "agent.txt").exists()
    task.send("write it again")
    made = task.commit("The agent's file")
    log = subprocess.run(["git", "log", "--format=%s"], cwd=project, capture_output=True, text=True).stdout
    assert made and log.splitlines()[0] == "The agent's file"
    # On a branch of its own: merged into the one it came from
    branch = relay_sdk.convo.create(workdir=str(project), isolate=True)
    branch.send("write on a branch")
    assert branch.merge() == "main"
    assert "write on a branch" in (project / "agent.txt").read_text()
    # Asked for one where it can't be (uncommitted changes): in the folder itself, and a warning says why
    (project / "agent.txt").write_text("changed, not committed\n")
    with pytest.warns(RuntimeWarning, match="in the folder itself: the folder has changes that aren't committed"):
        relay_sdk.convo.create(workdir=str(project), isolate=True)


def test_usage_limits_and_the_default_agent(relay):
    url, _ = relay
    relay_sdk.login("me", "correct horse", url=url)
    first = relay_sdk.convo.create(provider="other")
    assert relay_sdk.convo.create().provider == "other"  # (the latest conversation's agent)
    relay_sdk.login("me", "correct horse", url=url, provider="fake")
    assert relay_sdk.convo.create().provider == "fake"  # (the session's)
    first.send("hello")
    spent = relay_sdk.usage(days=7)
    assert spent["days"] == 7 and sum(kind["runs"] for kind in spent["totals"].values()) >= 1
    assert isinstance(relay_sdk.limits(), list)
    with pytest.raises(TypeError):
        relay_sdk.convo.create(mode="plan")  # (2.2: agent_mode)


def test_an_address_as_a_browser_takes_it():
    """Found by a simulated user: login(url="127.0.0.1:8499") said "unknown url type". An address without its
    scheme is http's."""
    assert relay_sdk.Session("127.0.0.1:8499").url == "http://127.0.0.1:8499"
    assert relay_sdk.Session("https://relay.example/").url == "https://relay.example"
    # A proxy's own sign-in in the address: sent to it, and out of the address (errors, links, repr)
    behind = relay_sdk.Session("https://alex:p%40ss@relay.example/")
    assert behind.url == "https://relay.example" and "p@ss" not in repr(behind) and "%40" not in repr(behind)
    proxy = "Basic " + base64.b64encode(b"alex:p@ss").decode()
    assert behind._session_header() == {"Authorization": proxy}
    behind._app_password = "relay_x"
    assert behind._session_header() == {"Authorization": proxy, "X-Relay-App-Password": "relay_x"}
    assert relay_sdk.Session("alex:pw@192.168.1.2:8420").url == "http://192.168.1.2:8420"


def test_a_relay_that_hangs_up_is_a_relay_error():
    """A connection lost before the answer (or a time-out) is a RelayError too, so a script's `except RelayError`
    covers it."""
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()

        def hang_up():
            connection, _ = server.accept()
            connection.recv(65536)
            connection.close()

        threading.Thread(target=hang_up, daemon=True).start()
        with pytest.raises(relay_sdk.RelayError, match="didn't answer"):
            relay_sdk.Session(f"http://127.0.0.1:{server.getsockname()[1]}").request("GET", "/api/state")


def test_a_temporary_chat_from_the_sdk(relay):
    url, _ = relay
    user = relay_sdk.login("me", "correct horse", url=url)
    chat = relay_sdk.convo.create(user, provider="fake", temporary=True)
    assert chat.temporary and chat.title == "Temporary chat" and chat.tools["memory"] is False
    assert chat.send("hello").response == "You said: hello"
    assert chat not in [c["id"] for c in relay_sdk.convo.list(user)]
    chat.keep()
    assert not chat.temporary and chat in [c["id"] for c in relay_sdk.convo.list(user)]


def test_scripts_from_the_sdk(relay):
    """scripts: made (never over one that's there), read, run with arguments and followed as they write, their end,
    output and conversations; stopped; moved, copied and renamed under the names Relay picks; one an agent writes."""
    url, _ = relay
    relay_sdk.login("me", "correct horse", url=url, provider="fake")
    hello = relay_sdk.scripts.create("private/hello.py", textwrap.dedent("""\
        import sys
        from relay_sdk import *
        print("hi", *sys.argv[1:], flush=True)
        convo.create(title="From a script", provider="fake").send("hello")
    """))
    assert hello == "private/hello.py" and hello.text.startswith("import sys")
    with pytest.raises(relay_sdk.RelayError, match="already"):
        relay_sdk.scripts.create("private/hello.py", "x = 1\n")
    run = hello.run(["there", "two words"])
    assert "".join(run) == "hi there two words\n"
    assert (run.status, run.exit_code, run.output) == ("succeeded", 0, "hi there two words\n")
    assert [chat.title for chat in run.conversations] == ["From a script"]
    assert run.script == hello and hello.runs() == [run]
    slow = relay_sdk.scripts.create("private/slow.py", "import time\nprint('start', flush=True)\ntime.sleep(60)\n")
    going = relay_sdk.scripts.run("private/slow.py")
    assert next(iter(going)) == "start\n" and going.get()["status"] == "running"
    assert going.stop() and going.status == "stopped"
    # Where they are, and what they're called: Relay's to pick
    assert hello.move("public") == "public/hello.py"
    mine = relay_sdk.scripts.get("public/examples/ask.py").copy()
    assert (mine, mine.copy(), mine.rename("asked.py")) == ("private/ask.py", "private/ask_2.py", "private/asked.py")
    relay_sdk.scripts.get("private/ask_2.py").delete()
    slow.delete()
    assert {s["name"] for s in relay_sdk.scripts.list() if not s["example"]} == {"public/hello.py", "private/asked.py"}
    # One an agent writes: a code task in public/, with Relay's skill for it
    chat = relay_sdk.scripts.write("Print the date")
    assert chat.title == "Script: Print the date" and chat.get()["directory"].endswith("/scripts/public")
    asked = chat.wait()[-1].response  # (the skill's command, as Relay hands it to the agent)
    assert '"relay-sdk-template-writer" skill' in asked and "What I ask: Print the date" in asked


def test_tasks_from_the_sdk(relay):
    """tasks: one that runs a script, one that asks an agent; when in words; changed, run now (the script's run, or
    the conversation), deleted."""
    url, _ = relay
    relay_sdk.login("me", "correct horse", url=url, provider="fake")
    relay_sdk.scripts.create("public/report.py", "import sys\nprint('the report', *sys.argv[1:])\n")
    report = relay_sdk.tasks.create("Daily report", "daily 08:00", script="report.py", args="--short")  # (public/'s)
    found = report.get()
    assert (found["script"], found["script_args"], found["when"], found["provider_id"]) == (
        "public/report.py", "--short", {"kind": "daily", "time": "08:00"}, "")
    ran = report.run()
    assert isinstance(ran, relay_sdk.ScriptRun) and ran.output == "the report --short\n"
    asked = relay_sdk.tasks.create("Morning question", "weekdays 09:00", prompt="Anything new?")
    assert (asked.get()["provider_id"], asked.get()["prompt"]) == ("fake", "Anything new?")
    asked.update(name="Soon", when="in 10 minutes")
    assert asked.get()["name"] == "Soon" and asked.get()["when"]["kind"] == "once"
    chat = asked.run()
    assert isinstance(chat, relay_sdk.Conversation) and "Anything new?" in chat.wait()[-1].response
    assert {"Daily report", "Soon"} <= {t["name"] for t in relay_sdk.tasks.list()}
    report.delete()
    with pytest.raises(relay_sdk.RelayError) as gone:
        report.get()
    assert gone.value.status == 404


def test_notifications_from_the_sdk(relay):
    """notify(): pushed (here to a stand-in for ntfy), with its title; not set up: False, and a warning says why."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    url, _ = relay
    user = relay_sdk.login("me", "correct horse", url=url)
    received = []

    class Catch(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            received.append((self.headers["Title"], self.rfile.read(int(self.headers["Content-Length"])).decode()))
            self.send_response(200)
            self.end_headers()

    with pytest.warns(RuntimeWarning, match="set up"):
        assert relay_sdk.notify("Done") is False
    server = ThreadingHTTPServer(("127.0.0.1", 0), Catch)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        user.request("PUT", "/api/settings", {"notifications": {
            "ntfy_url": f"http://127.0.0.1:{server.server_address[1]}/topic"}})
        assert relay_sdk.notify("A GPU for 300 euro", title="Deals", priority="high") is True
        assert received == [("Deals", "A GPU for 300 euro")]
        with pytest.raises(relay_sdk.RelayError, match="priority"):
            relay_sdk.notify("x", priority="loud")
    finally:
        server.shutdown()


def test_live_services_from_the_sdk(relay, monkeypatch):
    """services: by name or id; their status, logs and snapshots, and what's done to them, in Relay's words (its
    Production boxes stood in for: they need podman, and their own tests have it)."""
    import functools

    from relay.production import ProductionError
    url, app = relay
    production, done = app.state.relay.production, []
    site = {"id": "5e1f00aa", "name": "site", "snapshots": [{"name": "daily-2026-10-08", "kind": "daily"}]}

    def get(user, wanted, managed=False):
        if wanted not in ("site", "5e1f00aa"):
            raise ProductionError(f"No service {wanted}")
        return site

    async def act(name, service, *args):
        done.append((name, *args))
        return f"{name}: done"

    monkeypatch.setattr(production, "get", get)
    monkeypatch.setattr(production, "one", get)
    monkeypatch.setattr(production, "services", lambda user, managed=False: [site])
    monkeypatch.setattr(production, "status", lambda service: {"running": True})
    monkeypatch.setattr(production, "logs", lambda service, since, lines: "started\n")
    monkeypatch.setattr("relay.production.describe_status", lambda found: "It's well.")
    for name in ("start_service", "stop_service", "restart", "rollback", "restore"):
        monkeypatch.setattr(production, name, functools.partial(act, name))
    relay_sdk.login("me", "correct horse", url=url)
    assert [s["name"] for s in relay_sdk.services.list()] == ["site"]
    service = relay_sdk.services.get("site")
    assert service == "5e1f00aa" and relay_sdk.services.get("5e1f00aa") == service
    assert (service.status()["text"], service.logs(), service.snapshots()[0]["name"]) == (
        "It's well.", "started\n", "daily-2026-10-08")
    assert [service.restart(), service.stop(), service.start()] == [
        "restart: done", "stop_service: done", "start_service: done"]
    service.rollback()
    service.rollback(3)
    service.restore("daily-2026-10-08")
    assert done == [("restart",), ("stop_service",), ("start_service",), ("rollback", None), ("rollback", 3),
                    ("restore", "daily-2026-10-08")]
    with pytest.raises(relay_sdk.RelayError) as missing:
        relay_sdk.services.get("nope")
    assert missing.value.status == 404


def test_the_sdk_calls_only_relays_own_routes(tmp_path):
    """One API, thin clients (AGENTS.md): each call the SDK makes (request, a class's _call, a stream) is one of
    Relay's routes with its method, so the SDK can't drift from the API. Read from its source, every call."""
    import ast
    tree = ast.parse((Path(relay_sdk.__file__)).read_text())

    def path_of(node, path=None):
        """A path as it's written (placeholders as x, `path` as given, no query); None if it isn't one."""
        if isinstance(node, ast.BinOp):  # ("/api/usage/limits" + "?fresh=1" or "")
            return path_of(node.left, path)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value.split("?")[0]
        if isinstance(node, ast.JoinedStr):
            text = "".join(part.value if isinstance(part, ast.Constant) else
                           (path if isinstance(part.value, ast.Name) and part.value.id == "path" else "x")
                           for part in node.values)
            return text.split("?")[0]
        return None

    calls, bases = [], {}
    for cls in [node for node in tree.body if isinstance(node, ast.ClassDef)]:
        for method in cls.body:
            if isinstance(method, ast.FunctionDef) and method.name == "_call":
                bases[cls.name] = next(node for node in ast.walk(method) if isinstance(node, ast.JoinedStr))
    for cls in [node for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef))]:
        for node in ast.walk(cls):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            name, args = node.func.attr, node.args
            if name == "_call" and cls.name in bases and args and isinstance(args[0], ast.Constant):
                path = path_of(args[1]) if len(args) > 1 else ""
                calls.append((args[0].value, path_of(bases[cls.name], path), node.lineno))
            elif name == "request" and len(args) >= 2 and isinstance(args[0], ast.Constant):
                if isinstance(args[1], ast.Name):
                    continue  # (a class's _call: above)
                calls.append((args[0].value, path_of(args[1]), node.lineno))
            elif name == "_stream" and args:
                calls.append(("GET", path_of(args[0]), node.lineno))
    assert len(calls) > 60 and all(path and path.startswith("/api/") for _, path, _ in calls), calls
    settings = config.Settings(workspace_roots=[str(tmp_path)], chat_root=str(tmp_path / "chats"))
    routes = create_app(settings, Database(tmp_path / "relay.db"), Registry(tmp_path / "providers")).routes
    for method, path, line in calls:
        assert any(route.path_regex.match(path) and method in (getattr(route, "methods", None) or {method})
                   for route in routes), f"line {line}: {method} {path} is no route of Relay's"
