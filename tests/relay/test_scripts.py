"""The Scripts page's scripts (relay.scripts) against a real Relay over HTTP: files in their folder, runs signed in
as you with Relay's SDK, their output as it comes, how they end, what they started; and the skill's templates."""

import base64
import hashlib
import json
import py_compile
import socket
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest
import uvicorn

from relay import auth, config, scripts, skills, tunables
from relay.app import create_app
from relay.db import Database
from relay.providers import Registry
from relay.routes import settings as settings_routes
from relay.tools_script import note

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sdk" / "python"))
import relay_sdk

# A stand-in agent: the prompt echoed; lingering when asked
AGENT = textwrap.dedent("""\
    import json, sys, time
    prompt = sys.stdin.read()
    def emit(**e): print(json.dumps(e), flush=True)
    emit(type="session", session="sess-1")
    emit(type="text", delta="You said: " + prompt)
    if "linger" in prompt: time.sleep(30)
    emit(type="done", status="succeeded")
""")


@pytest.fixture
def relay(tmp_path, monkeypatch):
    """A Relay serving on a port of its own, signed in: (a Session, the Relay)."""
    monkeypatch.setenv("RELAY_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(relay_sdk, "_default", None)
    monkeypatch.delenv("RELAY_TOKEN", raising=False)
    agent = tmp_path / "agent.py"
    agent.write_text(AGENT)
    (tmp_path / "ws").mkdir()
    registry = Registry(tmp_path / "providers")
    monkeypatch.setattr(registry, "ensure_defaults", lambda: None)
    registry.add({"id": "fake", "name": "Fake agent", "kind": "command",
                  "options": {"command": [sys.executable, str(agent), ""]}})
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
        session = relay_sdk.Session(f"http://127.0.0.1:{port}")
        session.request("POST", "/api/setup", {"username": "me", "password": "correct horse"})
        yield session, app.state.relay
    finally:
        server.should_exit = True
        thread.join(30)
        sock.close()


def full(name):
    """A script's name with its folder: private/ when it says none."""
    return name if name.startswith(("private/", "public/")) else f"private/{name}"


def write(session, name, text):
    return session.request("PUT", f"/api/scripts/{full(name)}", {"text": textwrap.dedent(text)})


def run(session, name, args=""):
    return session.request("POST", f"/api/scripts/{full(name)}/run", {"args": args})["run"]


def output(session, run_id, after=0):
    """A run's output as it comes, to its end: (its text, the conversations it made, how it ended)."""
    request = urllib.request.Request(f"{session.url}/api/scripts/runs/{run_id}/output?after={after}",
                                     headers={"Accept": "text/event-stream", **session._session_header()})
    text, made, ended = "", [], None
    with urllib.request.urlopen(request, timeout=60) as response:
        kind, data = None, []
        for raw in response:
            line = raw.decode().rstrip("\r\n")
            if line:
                if line.startswith("event:"):
                    kind = line[6:].strip()
                elif line.startswith("data:"):
                    data.append(line[5:].lstrip())
                continue
            if not data:  # (a sign of life: ": keep-alive")
                continue
            found = json.loads("\n".join(data))
            if kind is None:
                text += found["text"]
            elif kind == "conversations":
                made = found
            elif kind == "end":
                ended = found
                break
            kind, data = None, []
    return text, made, ended


def until(check, seconds=20):
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, "it didn't happen in time"
        time.sleep(.05)


def test_scripts_are_files_in_their_folder(relay, tmp_path):
    session, _ = relay
    found = session.request("GET", "/api/scripts")
    folder = tmp_path / "ws" / "scripts"
    assert found["folder"] == str(folder.resolve()) and all(s["example"] for s in found["scripts"])  # (Relay's)
    assert sorted(p.name for p in folder.iterdir()) == ["DO_NOT_PUT_FILES_HERE", "private", "public"]
    assert "relay-sdk-template-writer" in (folder / "public" / "README.md").read_text()  # (for an agent there)
    # Every agent is told where the public ones are; no box shows the private ones (relay.containers)
    public = folder.resolve() / "public"
    assert relay[1].runs._public_scripts() == str(public)
    assert relay[1].containers.hidden == [folder.resolve() / "private"]
    assert f"The user's public scripts (Python with Relay's SDK) are in {public}" in note(Path("/x/relay"),
                                                                                         scripts=str(public))
    write(session, "hello.py", "print('hi')\n")
    listed = [s for s in session.request("GET", "/api/scripts")["scripts"] if not s["example"]]
    assert [(s["name"], s["size"], s["running"], s["private"]) for s in listed] == [("private/hello.py", 12, False, True)]
    assert session.request("GET", "/api/scripts/private/hello.py") == {
        "name": "private/hello.py", "text": "print('hi')\n", "runs": [], "tasks": []}
    for wrong in ("private/hello.txt", "private/.hidden.py", "private/a%20b.py", "private/..%2Fout.py",
                  "public/examples%2F..%2Fout.py", "other/hello.py", "private/examples/x.py"):
        with pytest.raises(relay_sdk.RelayError) as refused:  # (as sent; one without its folder is private/'s)
            session.request("PUT", f"/api/scripts/{wrong}", {"text": "x = 1\n"})
        assert refused.value.status in (400, 404, 405)
    with pytest.raises(scripts.ScriptError):  # (a trailing newline: the route takes it off, the name check too)
        relay[1].scripts.path("private/hello.py\n")
    assert not (tmp_path / "ws" / "out.py").exists() and not (folder / "hello.py").exists()
    # Renamed (not over another), made public, then deleted
    write(session, "other.py", "x = 1\n")
    with pytest.raises(relay_sdk.RelayError, match="already"):
        session.request("POST", "/api/scripts/private/hello.py/rename", {"to": "private/other.py"})
    session.request("POST", "/api/scripts/private/hello.py/rename", {"to": "public/greet.py"})
    assert sorted(p.name for p in (folder / "private").glob("*.py")) == ["other.py"]
    assert sorted(p.name for p in (folder / "public").glob("*.py")) == ["greet.py"]
    session.request("DELETE", "/api/scripts/private/other.py")
    with pytest.raises(relay_sdk.RelayError) as gone:
        session.request("GET", "/api/scripts/private/other.py")
    assert gone.value.status == 400
    # Another folder: in a workspace root only, and one that can be made there
    (tmp_path / "ws" / "a-file").write_text("not a folder")
    for outside in (str(tmp_path / "elsewhere"), "relative/folder", str(tmp_path / "ws" / "a-file")):
        with pytest.raises(relay_sdk.RelayError):
            session.request("PUT", "/api/scripts", {"folder": outside})
    assert config.load_settings().scripts_folder == ""
    moved = session.request("PUT", "/api/scripts", {"folder": str(tmp_path / "ws" / "mine" / "scripts")})
    assert moved["folder"] == str((tmp_path / "ws" / "mine" / "scripts").resolve())
    assert moved["scripts"] and all(s["example"] for s in moved["scripts"])  # (its examples, written there too)
    assert config.load_settings().scripts_folder == moved["folder"]


def test_a_script_runs_signed_in_as_you(relay):
    session, app = relay
    sessions = app.db.one("SELECT count(*) AS n FROM auth_sessions")["n"]
    write(session, "ask.py", """\
        import os, sys
        from relay_sdk import *
        chat = convo.create(title="From a script")
        print("answer:", chat.send("hello " + " ".join(sys.argv[1:])).response, flush=True)
        print("token in the environment:", bool(os.environ.get("RELAY_TOKEN")))
    """)
    run_id = run(session, "ask.py", "there 'two words'")
    text, made, ended = output(session, run_id)
    assert "answer: You said: hello there two words" in text and "token in the environment: True" in text
    assert ended["status"] == "succeeded" and ended["exit_code"] == 0 and ended["finished"] >= ended["started"]
    assert [c["title"] for c in made] == ["From a script"]
    # (started outside Relay's pages: by that script, its mark in the lists says)
    assert app.db.one("SELECT external FROM conversations WHERE id = ?", (made[0]["id"],))["external"] == \
        "the script private/ask.py"
    # Kept: its output, its arguments, the conversation it made; its session ended with it
    kept = session.request("GET", f"/api/scripts/runs/{run_id}")
    assert kept["output"] == text and kept["args"] == "there 'two words'"
    assert kept["conversations"] == made and kept["conversations"][0]["id"] in {c["id"] for c in relay_sdk.convo.list(session)}
    assert [r["id"] for r in session.request("GET", "/api/scripts/private/ask.py")["runs"]] == [run_id]
    assert app.db.one("SELECT count(*) AS n FROM auth_sessions")["n"] == sessions
    # Read again from where a page was (the characters it saw)
    assert output(session, run_id, after=len("answer: "))[0] == text[len("answer: "):]
    # Nobody else's to see
    app.accounts.create_user("you", "another horse")
    other = relay_sdk.Session(session.url)
    other.request("POST", "/api/login", {"username": "you", "password": "another horse"})
    with pytest.raises(relay_sdk.RelayError) as hidden:
        other.request("GET", f"/api/scripts/runs/{run_id}")
    assert hidden.value.status == 404 and other.request("GET", "/api/scripts/private/ask.py")["runs"] == []
    # A script in the folder named as a module of Python's hides it from none (the SDK's urllib imports random)
    write(session, "random.py", "raise SystemExit('hid random')\n")
    text, _, ended = output(session, run(session, "ask.py"))
    assert ended["status"] == "succeeded" and "answer: You said: hello" in text


def test_how_a_script_ends(relay, monkeypatch):
    session, app = relay
    write(session, "fails.py", "import sys\nprint('trying', flush=True)\nsys.exit(3)\n")
    text, _, ended = output(session, run(session, "fails.py"))
    assert text == "trying\n" and (ended["status"], ended["exit_code"]) == ("failed", 3)
    write(session, "broken.py", "print('nearly'\n")
    text, _, ended = output(session, run(session, "broken.py"))
    assert "SyntaxError" in text and (ended["status"], ended["exit_code"]) == ("failed", 1)
    with pytest.raises(relay_sdk.RelayError, match="parse"):
        run(session, "fails.py", "'unclosed")
    # Stopped: it, and the agent it started that still works
    write(session, "waits.py", """\
        import time
        from relay_sdk import *
        chat = convo.create(title="Lingering")
        chat.send("linger, please", stream=True)
        print("sent", flush=True)
        time.sleep(60)
    """)
    run_id = run(session, "waits.py")
    until(lambda: "sent" in session.request("GET", f"/api/scripts/runs/{run_id}")["output"])
    assert {s["name"]: s["running"] for s in session.request("GET", "/api/scripts")["scripts"]}["private/waits.py"] is True
    monkeypatch.setattr(settings_routes, "restart_process", lambda: pytest.fail("restarted"))
    with pytest.raises(relay_sdk.RelayError, match="agent is still working"):  # (the one it started, first)
        session.request("POST", "/api/restart")
    assert session.request("POST", f"/api/scripts/runs/{run_id}/stop") == {"stopped": True}
    text, made, ended = output(session, run_id)
    assert ended["status"] == "stopped" and "[Relay] Stopped the agent it started in 1 conversation" in text
    chat = relay_sdk.conversation(made[0]["id"], session)
    until(lambda: not chat.running)
    assert chat.messages()[-1].status == "interrupted"
    assert session.request("POST", f"/api/scripts/runs/{run_id}/stop") == {"stopped": False}
    write(session, "sleeps.py", "import time\nprint('zzz', flush=True)\ntime.sleep(60)\n")
    sleeping = run(session, "sleeps.py")
    until(lambda: "zzz" in session.request("GET", f"/api/scripts/runs/{sleeping}")["output"])
    with pytest.raises(relay_sdk.RelayError, match="1 script is still running") as refused:  # (it would end it)
        session.request("POST", "/api/restart")
    assert refused.value.status == 409
    session.request("POST", f"/api/scripts/runs/{sleeping}/stop")
    assert output(session, sleeping)[2]["status"] == "stopped"
    # Deleted while it runs: its run ends with it (it went on unseen, gone from the list, and couldn't be stopped)
    orphan = run(session, "sleeps.py")
    until(lambda: "zzz" in session.request("GET", f"/api/scripts/runs/{orphan}")["output"])
    session.request("DELETE", "/api/scripts/private/sleeps.py")
    until(lambda: orphan not in app.scripts.active)
    write(session, "sleeps.py", "import time\nprint('zzz', flush=True)\ntime.sleep(60)\n")
    # Stopped while its WorkBox starts: it doesn't start then
    real_shell = app.containers.shell
    monkeypatch.setattr(app.containers, "shell", lambda folder: time.sleep(1.5) or real_shell(folder))
    late = run(session, "sleeps.py")
    assert session.request("POST", f"/api/scripts/runs/{late}/stop") == {"stopped": True}
    text, _, ended = output(session, late)
    assert ended["status"] == "stopped" and "It didn't run: it was stopped first" in text and "zzz" not in text
    monkeypatch.setattr(app.containers, "shell", real_shell)
    # Out of time
    real = tunables.get
    monkeypatch.setattr(tunables, "get", lambda key: 0.02 if key == "scripts.timeout" else real(key))
    write(session, "slow.py", "import time\nprint('on it', flush=True)\ntime.sleep(60)\n")
    text, _, ended = output(session, run(session, "slow.py"))
    assert text == "on it\n" and ended["status"] == "timed out"


def test_runs_are_kept_within_bounds(relay, monkeypatch):
    session, app = relay
    monkeypatch.setattr(scripts, "KEPT", 2)
    monkeypatch.setattr(scripts, "OUTPUT", 100)
    write(session, "loud.py", "print('x' * 450 + 'end')\n")
    for _ in range(3):
        _, _, ended = output(session, run(session, "loud.py"))
        assert ended["status"] == "succeeded"
    runs = session.request("GET", "/api/scripts/private/loud.py")["runs"]
    assert len(runs) == 2  # (the last KEPT)
    kept = session.request("GET", f"/api/scripts/runs/{runs[0]['id']}")
    assert kept["cut"] == 354 and kept["output"] == "x" * 96 + "end\n"  # (as it was while it ran)
    # A run Relay's restart cut off: failed, and its session ended (a script still at work can't go on as you)
    token = app.accounts.start_session(1, 1)
    app.db.execute("INSERT INTO script_runs (id, user_id, script, args, started, status, session) "
                   "VALUES ('cut', 1, 'private/loud.py', '', 1, 'running', ?)", (auth.token_hash(token),))
    app.scripts.reconcile()
    found = session.request("GET", "/api/scripts/runs/cut")
    assert found["status"] == "failed" and "[Relay restarted during this run]" in found["output"]
    assert app.accounts.user_for_token(token) is None
    # Renamed and deleted with their script
    session.request("POST", "/api/scripts/private/loud.py/rename", {"to": "private/quiet.py"})
    assert app.db.one("SELECT count(*) AS n FROM script_runs WHERE script = 'private/quiet.py'")["n"] == 3
    session.request("DELETE", "/api/scripts/private/quiet.py")
    assert app.db.one("SELECT count(*) AS n FROM script_runs")["n"] == 0


def test_the_skill_and_its_templates(relay, tmp_path):
    session, _ = relay
    skill = next(s for s in skills.all_skills() if s.name == "relay-sdk-template-writer")
    assert skill.builtin and "Scripts page" in skill.description
    for name in ("reference.md", "templates/ask.py"):
        assert (skill.folder / name).is_file()  # (what its SKILL.md points to, next to it)
    templates = session.request("GET", "/api/scripts/templates")["templates"]
    assert [t["name"] for t in templates] == ["ask.py", "best_answer.py", "checked_code_task.py", "code_task.py",
                                              "compare_models.py", "extract_data.py", "refusals.py", "room_left.py",
                                              "service_watch.py", "timed_work.py"]
    for template in templates:
        assert template["description"] and template["description"].endswith(".")
        path = tmp_path / template["name"]
        path.write_text(template["text"])
        py_compile.compile(str(path), doraise=True)
    # One runs as it is: asked, with its question as its arguments
    ask = next(t for t in templates if t["name"] == "ask.py")
    session.request("PUT", "/api/scripts/private/ask.py", {"text": ask["text"]})
    text, made, ended = output(session, run(session, "ask.py", "what is two and two?"))
    assert ended["status"] == "succeeded", text
    assert "You said: what is two and two?" in text and "tools: none" in text and len(made) == 1
    # The one that looks at the live services (Production boxes) runs too: there are none here
    watch = next(t for t in templates if t["name"] == "service_watch.py")
    session.request("POST", "/api/scripts/private/watch.py", {"text": watch["text"]})
    text, _, ended = output(session, run(session, "watch.py"))
    assert ended["status"] == "succeeded" and text == "No live services to look at.\n", text


def test_the_folder_as_a_network_drive_and_the_documentation(relay, tmp_path):
    session, _ = relay
    write(session, "hello.py", "print('hi')\n")
    assert session.request("GET", "/api/scripts")["shared"] is False
    share = session.request("POST", "/api/scripts/share", {})
    assert share["path"].startswith("/dav/") and share["name"] == "Scripts"
    assert session.request("GET", "/api/scripts")["shared"] is True  # (the page says so, beside the folder)
    assert share["directory"] == str((tmp_path / "ws" / "scripts").resolve())
    assert session.request("POST", "/api/scripts/share", {}) == share  # (the same one, asked again)
    # As an editor on your computer reaches it: no session, its address is the key
    drive = session.url + share["path"]
    listing = urllib.request.urlopen(urllib.request.Request(drive + "private/", method="PROPFIND",
                                                            headers={"Depth": "1"}))
    assert listing.status == 207 and b"hello.py" in listing.read()
    saved = urllib.request.urlopen(urllib.request.Request(drive + "public/from_vscode.py", data=b"print('edited')\n",
                                                          method="PUT"))
    assert saved.status == 201
    assert "public/from_vscode.py" in [s["name"] for s in session.request("GET", "/api/scripts")["scripts"]]
    assert session.request("GET", "/api/scripts/public/from_vscode.py")["text"] == "print('edited')\n"
    session.request("POST", "/api/scripts/share", {"read_only": True})
    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(urllib.request.Request(drive + "private/more.py", data=b"x = 1\n", method="PUT"))
    assert refused.value.code == 403
    # The documentation: how scripts run here, then the SDK (the skill's reference)
    docs = session.request("GET", "/api/scripts/docs")["markdown"]
    assert docs.startswith("# Scripts") and "Open in VS Code" in docs and "## Signing in" in docs
    assert "# RESL" in docs and "agent_refused" in docs  # (and RESL, after the SDK)


def test_the_examples_relay_keeps(relay, tmp_path, monkeypatch):
    session, _ = relay
    folder = tmp_path / "ws" / "scripts" / "public" / "examples"
    listed = {s["name"]: s for s in session.request("GET", "/api/scripts")["scripts"]}
    templates = sorted(p.name for p in scripts.TEMPLATES.glob("*.py"))
    assert sorted(name for name in listed if listed[name]["example"]) == [f"public/examples/{t}" for t in templates]
    assert not any(listed[name]["private"] for name in listed if listed[name]["example"])
    assert (folder / "ask.py").read_text() == (scripts.TEMPLATES / "ask.py").read_text()
    assert "keeps them up to date" in (folder / "README.md").read_text()
    # Relay's: read, run, copied; not changed, renamed or deleted here
    found = session.request("GET", "/api/scripts/public/examples/ask.py")
    assert found["name"] == "public/examples/ask.py" and found["text"].startswith('"""Ask an agent')
    write(session, "hello.py", "print('hi')\n")
    for refused in (lambda: write(session, "public/examples/ask.py", "x = 1\n"),
                    lambda: session.request("POST", "/api/scripts/public/examples/ask.py/rename",
                                            {"to": "private/mine.py"}),
                    lambda: session.request("POST", "/api/scripts/private/hello.py/rename",
                                            {"to": "public/examples/hello.py"}),
                    lambda: session.request("DELETE", "/api/scripts/public/examples/ask.py")):
        with pytest.raises(relay_sdk.RelayError, match="Relay keeps the examples up to date"):
            refused()
    text, made, ended = output(session, run(session, "public/examples/ask.py", "an example?"))
    assert ended["status"] == "succeeded" and "You said: an example?" in text and len(made) == 1
    assert [r["script"] for r in session.request("GET", "/api/scripts/public/examples/ask.py")["runs"]] == [
        "public/examples/ask.py"]
    # Changed meanwhile: it stays until Relay's next version, which writes them anew and drops what it hasn't now
    (folder / "ask.py").write_text("# mine now\n")
    (folder / "gone.py").write_text("# an older Relay's\n")
    marker = json.loads((folder / ".relay").read_text())
    (folder / ".relay").write_text(json.dumps({**marker, "files": [*marker["files"], "gone.py"]}))
    session.request("GET", "/api/scripts")
    assert (folder / "ask.py").read_text() == "# mine now\n"
    monkeypatch.setattr(scripts, "__version__", "99.0.0")
    session.request("GET", "/api/scripts")
    assert (folder / "ask.py").read_text() == (scripts.TEMPLATES / "ask.py").read_text()
    assert not (folder / "gone.py").exists() and json.loads((folder / ".relay").read_text())["version"] == "99.0.0"



def test_a_folder_from_before_private_and_public_settles(tmp_path, monkeypatch):
    """The scripts' folder of 2.3–2.5 (scripts beside Relay's README and examples): what's the user's moves into
    private/, the safe side, with its runs; Relay's own goes; .git and the like stay. A folder chosen otherwise
    keeps what it has."""
    from relay.app import Relay
    monkeypatch.setenv("RELAY_HOME", str(tmp_path / "data"))
    old = tmp_path / "ws" / "scripts"
    (old / "examples").mkdir(parents=True)
    (old / "examples" / ".relay").write_text("{}")
    (old / "README.md").write_text(scripts.OLD_README)
    (old / "report.py").write_text("print('report')\n")
    (old / "data.csv").write_text("a,b\n")
    (old / ".git").mkdir()
    (old / "private").mkdir()
    (old / "private" / "report.py").write_text("# there already\n")
    before = Database(tmp_path / "relay.db")  # (its runs as 2.5 named them, then opened by this Relay)
    before.execute("INSERT INTO users (id, username, password_hash, created) VALUES (1, 'me', 'x', 0)")
    for script in ("report.py", "examples/ask.py"):
        before.execute("INSERT INTO script_runs (id, user_id, script, args, started, status) "
                       "VALUES (?, 1, ?, '', 1, 'succeeded')", (script, script))
    before.conn.close()
    settings = config.Settings(workspace_roots=[str(tmp_path / "ws")], chat_root=str(tmp_path / "chats"))
    relay = Relay(settings, Database(tmp_path / "relay.db"), Registry(tmp_path / "providers"))
    assert [r["script"] for r in relay.db.all("SELECT script FROM script_runs")] == ["private/report.py"]
    relay.scripts.show()  # (Relay's start: settled, and the boxes told)
    assert (old / "private" / "report_2.py").exists() and relay.containers.hidden == [old / "private"]
    names = [s["name"] for s in relay.scripts.listed() if not s["example"]]
    assert names == ["private/report.py", "private/report_2.py"]
    assert (old / "private" / "report_2.py").read_text() == "print('report')\n"  # (the one there stays as it was)
    assert (old / "private" / "data.csv").exists() and (old / ".git").is_dir()
    assert sorted(p.name for p in old.iterdir()) == [".git", "DO_NOT_PUT_FILES_HERE", "private", "public"]
    assert [r["script"] for r in relay.db.all("SELECT script FROM script_runs")] == ["private/report_2.py"]
    # Once: a file put there later stays where it is (not a script: neither listed nor run)
    (old / "loose.py").write_text("x = 1\n")
    relay.scripts.listed()
    assert (old / "loose.py").exists()
    # Another folder chosen for the scripts, with things of its own: they stay
    mine = tmp_path / "ws" / "project"
    mine.mkdir()
    (mine / "main.py").write_text("print('mine')\n")
    relay.scripts.set_folder(str(mine))
    assert (mine / "main.py").exists() and not (mine / "private" / "main.py").exists()
    assert (mine / "DO_NOT_PUT_FILES_HERE").exists() and relay.containers.hidden == [mine / "private"]
    # One that isn't there yet is made at its first use, not at Relay's start
    relay.settings.scripts_folder = str(tmp_path / "ws" / "later")
    relay.scripts.show()
    assert not (tmp_path / "ws" / "later").exists()


def test_the_packages_a_script_names():
    """A `# /// script` block (PEP 723): its dependencies, as pip takes them."""
    assert scripts.needs("print('hi')\n") == []
    block = '# /// script\n# requires-python = ">=3.11"\n# dependencies = [\n#   "requests<3",\n#   "rich",\n# ]\n# ///\n'
    assert scripts.needs(f'"""What it does."""\n{block}import requests\n') == ["requests<3", "rich"]
    assert scripts.needs("# /// pyproject\n# x = 1\n# ///\n") == []  # (another kind of block)
    with pytest.raises(scripts.ScriptError, match="two"):
        scripts.needs(block + "import os\n" + block)
    with pytest.raises(scripts.ScriptError, match="list of packages"):
        scripts.needs('# /// script\n# dependencies = "requests"\n# ///\n')


def wheel(folder, name, files):
    """A wheel of `files` ({path: text}) as pip installs it, without an index."""
    info = f"{name}-1.0.dist-info"
    files = {**files, f"{info}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n",
             f"{info}/WHEEL": "Wheel-Version: 1.0\nGenerator: relay-tests\nRoot-Is-Purelib: true\nTag: py3-none-any\n"}
    record = [f"{path},sha256={base64.urlsafe_b64encode(hashlib.sha256(text.encode()).digest()).rstrip(b'=').decode()},"
              f"{len(text.encode())}" for path, text in files.items()]
    path = folder / f"{name}-1.0-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as packed:
        for name_in, text in files.items():
            packed.writestr(name_in, text)
        packed.writestr(f"{info}/RECORD", "\n".join([*record, f"{info}/RECORD,,"]) + "\n")
    return path


def test_a_script_gets_the_packages_it_names(relay, tmp_path, monkeypatch):
    """The packages a script names are installed with pip before it runs, for its WorkBox's user (there: this
    test's own); the second run finds them. A block that isn't TOML: the script doesn't run, and says why."""
    session, _ = relay
    package = wheel(tmp_path, "hello_relay", {"hello_relay/__init__.py": "GREETING = 'hello from a package'\n"})
    monkeypatch.setenv("PYTHONUSERBASE", str(tmp_path / "user"))
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    monkeypatch.setenv("PIP_BREAK_SYSTEM_PACKAGES", "1")  # (this machine's Python: a WorkBox's has no such rule)
    write(session, "packaged.py", "\n".join([
        "# /// script", f'# dependencies = ["hello-relay @ {package.as_uri()}"]', "# ///",
        "import hello_relay", "print(hello_relay.GREETING)", ""]))
    for _ in range(2):
        text, _, ended = output(session, run(session, "packaged.py"))
        assert ended["status"] == "succeeded" and text == "hello from a package\n", text
    assert list((tmp_path / "user").glob("lib/python3*/site-packages/hello_relay/__init__.py"))
    write(session, "broken.py", "# /// script\n# dependencies = [\n# ///\nprint('never')\n")
    text, _, ended = output(session, run(session, "broken.py"))
    assert ended["status"] == "failed" and "isn't TOML" in text and "never" not in text, text
    # One pip can't install: pip says why, then Relay
    write(session, "missing.py", '# /// script\n# dependencies = ["no-such-package-for-relay"]\n# ///\nprint(1)\n')
    text, _, ended = output(session, run(session, "missing.py"))
    assert ended["status"] == "failed" and "The packages it names couldn't be installed" in text, text



def test_what_the_pages_buttons_do_is_relays(relay, tmp_path):
    """New, Copy to my scripts, Make public/private and the templates' names are the API's (relay.scripts), for
    the SDK as for the page: a new script never over one that's there; a copy or a move under the next name free,
    which Relay picks."""
    session, _ = relay
    folder = tmp_path / "ws" / "scripts"
    session.request("POST", "/api/scripts/private/new.py", {"text": "x = 1\n"})
    with pytest.raises(relay_sdk.RelayError, match="already"):
        session.request("POST", "/api/scripts/private/new.py", {"text": "x = 2\n"})
    assert (folder / "private" / "new.py").read_text() == "x = 1\n"

    def copy(script, **data):
        return session.request("POST", f"/api/scripts/{script}/copy", data)["name"]

    def move(name, to):
        return session.request("POST", f"/api/scripts/{name}/move", {"to": to})["name"]

    assert copy("public/examples/ask.py") == "private/ask.py"
    assert copy("public/examples/ask.py") == "private/ask_2.py"
    assert copy("private/new.py", to="public", name="shared.py") == "public/shared.py"
    assert (folder / "private" / "ask_2.py").read_text() == (folder / "public" / "examples" / "ask.py").read_text()
    assert move("private/ask.py", "public") == "public/ask.py"
    assert move("private/ask_2.py", "public") == "public/ask_2.py"
    assert copy("public/examples/ask.py") == "private/ask.py"
    assert move("private/ask.py", "public") == "public/ask_3.py"  # (its name and the next one taken there)
    assert move("public/ask_3.py", "public") == "public/ask_3.py"  # (there already)
    assert move("public/ask_3.py", "private") == "private/ask_3.py"  # (back, under its own name: free there)
    with pytest.raises(relay_sdk.RelayError, match="examples"):
        move("public/examples/ask.py", "private")
    with pytest.raises(relay_sdk.RelayError, match="private or public"):
        move("private/ask_3.py", "elsewhere")
    assert copy("public/examples/room_left.py") == "private/room_left.py"
    templates = {t["name"]: t["free"] for t in session.request("GET", "/api/scripts/templates")["templates"]}
    assert templates["room_left.py"] == "private/room_left_2.py" and templates["ask.py"] == "private/ask.py"
    # A name without its folder (as a person or a script may write it): private/'s, else public/'s; and one moved
    # says where it is now (found by a simulated user)
    assert session.request("GET", "/api/scripts/new.py")["name"] == "private/new.py"
    assert session.request("GET", "/api/scripts/shared.py")["name"] == "public/shared.py"
    assert session.request("POST", "/api/scripts/fresh.py", {"text": "x = 3\n"})["name"] == "private/fresh.py"
    assert session.request("POST", "/api/scripts/shared.py/rename", {"to": "common.py"})["name"] == "public/common.py"
    move("private/fresh.py", "public")
    with pytest.raises(relay_sdk.RelayError, match="it's public/fresh.py now"):
        session.request("GET", "/api/scripts/private/fresh.py")


def test_with_an_agent_is_relays_too(relay, tmp_path):
    """With an agent: a code task in public/ (where agents may see), named for what the script should do, sent
    Relay's skill for it with that; a script's, when a script asks (its Stop stops it)."""
    session, app = relay
    made = session.request("POST", "/api/scripts/write",
                           {"description": "Print the time\nevery minute", "provider_id": "fake"})
    row = app.db.one("SELECT * FROM conversations WHERE id = ?", (made["conversation"],))
    assert row["mode"] == "code" and row["directory"] == str((tmp_path / "ws" / "scripts" / "public").resolve())
    assert row["title"] == "Script: Print the time"
    long = session.request("POST", "/api/scripts/write", {"provider_id": "fake", "description":
                           "Ask a model for a fun fact (about space or animals) and print it nicely"})
    assert app.db.one("SELECT title FROM conversations WHERE id = ?", (long["conversation"],))["title"] == (
        "Script: Ask a model for a fun fact (about space or animals) and…")  # (a whole word last)
    first = session.request("GET", f"/api/conversations/{made['conversation']}")["messages"][0]
    assert first["content"] == "/relay-sdk-template-writer Print the time\nevery minute"
    assert app.db.one("SELECT conversation_id FROM runs WHERE id = ?", (made["run"],))["conversation_id"] == row["id"]
    with pytest.raises(relay_sdk.RelayError, match="description"):
        session.request("POST", "/api/scripts/write", {"description": " ", "provider_id": "fake"})


def test_a_quiet_scripts_output_says_its_alive(relay, monkeypatch):
    """A script that says nothing for a while: its output's stream says it's alive meanwhile (for a reader's
    time-out, a proxy's), and readers pass that by (the SDK, the page's EventSource)."""
    from relay.routes import scripts as routes
    monkeypatch.setattr(routes, "HEARTBEAT", 0.3)
    session, _ = relay
    write(session, "quiet.py", "import time\nprint('a', flush=True)\ntime.sleep(1.2)\nprint('b', flush=True)\n")
    request = urllib.request.Request(f"{session.url}/api/scripts/runs/{run(session, 'quiet.py')}/output",
                                     headers={"Accept": "text/event-stream", **session._session_header()})
    with urllib.request.urlopen(request, timeout=30) as response:
        assert ": keep-alive" in response.read().decode()
    assert "".join(relay_sdk.ScriptRun(run(session, "quiet.py"), session)) == "a\nb\n"


def test_tasks_follow_a_script(relay):
    """A task that runs a script: named with it (its delete says it fails without it); the user's own follow it
    when it's renamed or moved, the Governor's don't (made private, it's out of the Governor's sight)."""
    session, app = relay
    write(session, "report.py", "print('report')\n")
    user = app.db.one("SELECT id FROM users")["id"]
    mine = app.schedules.save(user, {"name": "Daily report", "script": "private/report.py", "when": "daily 08:00"})
    governors = app.schedules.save(user, {"name": "Its report", "script": "private/report.py", "when": "daily 09:00",
                                          "managed": True})
    assert session.request("GET", "/api/scripts/private/report.py")["tasks"] == ["Daily report", "Its report"]
    session.request("POST", "/api/scripts/private/report.py/move", {"to": "public"})
    assert app.schedules.get(user, mine["id"])["script"] == "public/report.py"
    assert app.schedules.get(user, governors["id"])["script"] == "private/report.py"  # (its own to see to)

