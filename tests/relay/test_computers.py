"""The user's own computers: the connector script, and agents using it through Relay."""

import asyncio
import base64
import http.server
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest
import uvicorn

from relay import computers, config
from relay.app import create_app
from relay.db import Database
from relay.providers import Registry
from relay.routes import runs as runs_routes

H = {"X-Relay-Request": "1", "Content-Type": "application/json"}
SLEEPER = ("import json, time\nprint(json.dumps({'type': 'session', 'session': 's1'}), flush=True)\n"
           "time.sleep(120)\nprint(json.dumps({'type': 'done', 'status': 'succeeded'}), flush=True)\n")


@pytest.fixture
def relay(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(computers, "POLL_WAIT", 1)
    monkeypatch.setattr(computers, "ONLINE_GRACE", 2)
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
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    yield {"url": f"http://127.0.0.1:{port}", "app": app, "tmp": tmp_path}
    server.should_exit = True
    thread.join(10)


def call(base, method, path, body=None, raw=False):
    request = urllib.request.Request(base + path, method=method, headers=H,
                                     data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read()
    return data if raw else json.loads(data or b"null")


def until(condition, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.1)
    raise AssertionError("timed out")


def test_agents_work_on_the_users_computer(relay):
    base, tmp = relay["url"], relay["tmp"]
    laptop = call(base, "POST", "/api/computers", {"name": "My Laptop"})
    assert laptop["name"] == "my-laptop" and not laptop["ask"] and not laptop["online"]  # usability first
    call(base, "PATCH", f"/api/computers/{laptop['id']}", {"ask": True})  # this test is about asking
    script = tmp / "relay-computer.py"
    script.write_bytes(call(base, "GET", f"/api/computers/{laptop['id']}/script", raw=True))
    assert base in script.read_text() and "@TOKEN@" not in script.read_text()
    home = tmp / "home"
    home.mkdir()
    connector = subprocess.Popen([sys.executable, str(script)], env={**os.environ, "HOME": str(home)},
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        until(lambda: call(base, "GET", "/api/computers")[0]["online"])
        info = call(base, "GET", "/api/computers")[0]["info"]
        assert info["home"] == str(home) and info["os"]

        conv = call(base, "POST", "/api/conversations", {"provider_id": "sleeper"})
        run_id = call(base, "POST", f"/api/conversations/{conv['id']}/messages", {"text": "go"})["run_id"]
        db = relay["app"].state.relay.db
        token = db.one("SELECT tools_token FROM conversations WHERE id = ?", (conv["id"],))["tools_token"]
        until(lambda: relay["app"].state.relay.delegation.run_for(token) is not None)

        def tool(name, **arguments):
            body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
            result = call(base, "POST", f"/mcp/{token}", body)["result"]
            return result["content"][0]["text"], result["isError"]

        listed, _ = tool("computers")
        assert listed.startswith("- my-laptop: online;") and "asked before each command" in listed

        # Asked first: allowed for the rest of the answer
        answered = {}
        thread = threading.Thread(target=lambda: answered.update(result=tool("computer_run", command="echo hello")))
        thread.start()
        active = relay["app"].state.relay.runs.active[run_id]
        until(lambda: active.pending)
        event = next(iter(active.pending.values()))["event"]
        assert event["action"] == "Command on my-laptop" and event["resources"] == ["echo hello"]
        call(base, "POST", f"/api/runs/{run_id}/permission", {"id": event["id"], "decision": "answer"})
        thread.join(30)
        assert answered["result"] == ("Exit code 0\nhello", False)
        text, error = tool("computer_run", computer="my-laptop", command="exit 3")  # not asked again
        assert text.startswith("Exit code 3") and not error

        text, error = tool("computer_write", path="~/notes/a.txt", content="from Relay\n")
        assert not error and (home / "notes" / "a.txt").read_text() == "from Relay\n"
        assert tool("computer_read", path="~/notes/a.txt") == ("from Relay\n", False)
        text, error = tool("computer_read", path="~/nope.txt")
        assert error and "No file" in text
        text, error = tool("computer_run", computer="desktop", command="ls")
        assert error and "No computer 'desktop'" in text

        # The relay script agents run
        script_path = relay["app"].state.relay.delegation.script_for(
            db.one("SELECT * FROM conversations WHERE id = ?", (conv["id"],)), base)
        work = tmp / "work"
        work.mkdir()
        (work / "up.bin").write_bytes(bytes(range(256)))

        def relay_cli(*args, stdin=None):
            return subprocess.run([str(script_path), *args], cwd=work, input=stdin, capture_output=True, text=True)

        done = relay_cli("run", "--on", "my-laptop", "--", "echo", "via", "script")
        assert done.returncode == 0 and "via script" in done.stdout
        # Separate words keep their quoting; one word is a whole command line, for the computer's shell
        done = relay_cli("run", "--on", "my-laptop", "--", "sh", "-c", "echo quoted > ~/q.txt")
        assert done.returncode == 0 and (home / "q.txt").read_text() == "quoted\n"
        assert "a b" in relay_cli("run", "--on", "my-laptop", "echo a b > ~/r.txt; cat ~/r.txt").stdout
        assert "on stdin" in relay_cli("run", "--on", "my-laptop", stdin="echo on stdin").stdout
        assert relay_cli("push", "up.bin", "~/bin.dat").returncode == 0
        assert (home / "bin.dat").read_bytes() == bytes(range(256))
        pulled = relay_cli("pull", "~/bin.dat", "back.bin")
        assert pulled.returncode == 0 and (work / "back.bin").read_bytes() == bytes(range(256))

        # Read-only modes only ask the agent not to change anything
        call(base, "PATCH", f"/api/conversations/{conv['id']}", {"agent_mode": "plan"})
        assert tool("computer_run", command="ls ~") == ("Exit code 0\nbin.dat\nnotes\nq.txt\nr.txt", False)
        assert tool("computer_read", path="~/notes/a.txt")[1] is False

        # Offline
        connector.terminate()
        connector.wait(10)
        until(lambda: not call(base, "GET", "/api/computers")[0]["online"])
        text, error = tool("computer_read", path="~/notes/a.txt")
        assert error and "offline" in text
        call(base, "POST", f"/api/runs/{run_id}/stop")
    finally:
        connector.kill()
        output = connector.communicate()[0]
    assert "my-laptop > echo hello" in output and "Connected." in output


def test_a_removed_computers_connector_stops(relay):
    base, tmp = relay["url"], relay["tmp"]
    pc = call(base, "POST", "/api/computers", {"name": "pc"})
    script = tmp / "c.py"
    script.write_bytes(call(base, "GET", f"/api/computers/{pc['id']}/script", raw=True))
    connector = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True)
    until(lambda: call(base, "GET", "/api/computers")[0]["online"])
    call(base, "DELETE", f"/api/computers/{pc['id']}")
    assert connector.wait(20) == 1
    assert "doesn't know this connector" in connector.stdout.read()


def test_a_connector_behind_a_proxys_own_sign_in(relay):
    """Caddy's basic_auth in front of Relay: the connector takes its name and password from the address, and says how
    when it has none. The browser's address goes in the script, as Relay behind a proxy can't tell it."""
    base, tmp = relay["url"], relay["tmp"]
    pc = call(base, "POST", "/api/computers", {"name": "laptop"})
    script = tmp / "c.py"
    script.write_bytes(call(base, "GET", f"/api/computers/{pc['id']}/script?url=https%3A%2F%2Frelay.example%2Fr%2F",
                            raw=True))
    assert 'RELAY = "https://relay.example/r"' in script.read_text()
    script.write_bytes(call(base, "GET", f"/api/computers/{pc['id']}/script?url=javascript%3Ax", raw=True))
    assert f'RELAY = "{base}"' in script.read_text()
    wanted = "Basic " + base64.b64encode(b"alex:p@ss").decode()

    class Proxy(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            data = self.rfile.read(int(self.headers["Content-Length"]))
            if self.headers.get("Authorization") != wanted:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="restricted"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            try:
                with urllib.request.urlopen(urllib.request.Request(base + self.path, data=data, headers={
                        "Content-Type": "application/json"}), timeout=60) as response:
                    status, answer = response.status, response.read()
            except urllib.error.HTTPError as exc:
                status, answer = exc.code, exc.read()
            self.send_response(status)
            self.send_header("Content-Length", str(len(answer)))
            self.end_headers()
            self.wfile.write(answer)

        def log_message(self, *args):
            pass

    proxy = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    there = f"127.0.0.1:{proxy.server_address[1]}"
    try:
        bare = subprocess.run([sys.executable, str(script), "--url", f"http://{there}"], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=30)
        assert bare.returncode == 1 and "asks for a sign-in of its own" in bare.stdout
        assert "--url https://name:password@host" in bare.stdout
        connector = subprocess.Popen([sys.executable, str(script)], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True,
                                     env={**os.environ, "RELAY_URL": f"http://alex:p%40ss@{there}/"})
        try:
            until(lambda: call(base, "GET", "/api/computers")[0]["online"])
            time.sleep(1.5)  # (its first poll back: "Connected.")
        finally:
            connector.kill()
        output = connector.communicate()[0]
        assert f"Relay at http://{there}. Press" in output and "Connected." in output and "p@ss" not in output
    finally:
        proxy.shutdown()


def test_a_computer_task_works_on_its_own_computer(relay):
    base, tmp = relay["url"], relay["tmp"]
    agent = tmp / "agent.py"
    agent.write_text(
        "import json, os, subprocess, sys\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'type': 'session', 'session': 's1'}), flush=True)\n"
        "done = subprocess.run([os.environ['RELAY_TOOLS'], 'run', 'echo on-the-laptop'], capture_output=True, text=True)\n"
        "print(json.dumps({'type': 'text', 'delta': done.stdout + done.stderr}), flush=True)\n"
        "print(json.dumps({'type': 'done', 'status': 'succeeded'}), flush=True)\n")
    registry = relay["app"].state.relay.registry
    registry.add({"id": "worker", "name": "Worker", "kind": "command", "options": {"command": [sys.executable, str(agent)]}})
    laptop = call(base, "POST", "/api/computers", {"name": "laptop"})
    call(base, "POST", "/api/computers", {"name": "desktop"})  # two: without the task's, `run` wouldn't know which
    call(base, "PATCH", f"/api/computers/{laptop['id']}", {"ask": False})
    script = tmp / "c.py"
    script.write_bytes(call(base, "GET", f"/api/computers/{laptop['id']}/script", raw=True))
    connector = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        until(lambda: next(c for c in call(base, "GET", "/api/computers") if c["name"] == "laptop")["online"])
        conv = call(base, "POST", "/api/conversations", {"provider_id": "worker", "computer_id": laptop["id"]})
        assert conv["computer"] == {"id": laptop["id"], "name": "laptop", "online": True} and conv["mode"] == "chat"
        call(base, "POST", f"/api/conversations/{conv['id']}/messages", {"text": "go"})
        until(lambda: not call(base, "GET", f"/api/conversations/{conv['id']}")["running_run"], timeout=30)
        answer = call(base, "GET", f"/api/conversations/{conv['id']}")["messages"][-1]["content"]
        assert "on-the-laptop" in answer and "Exit code 0" in answer
    finally:
        connector.kill()
    try:
        call(base, "POST", "/api/conversations", {"provider_id": "worker", "computer_id": "nope"})
        raise AssertionError("an unknown computer was accepted")
    except urllib.error.HTTPError as exc:
        assert exc.code == 400


def test_an_answer_the_agent_stopped_waiting_for_comes_with_its_next_call(relay, monkeypatch):
    """Its shell's time-out stopped `relay` (a long `run --on`, a `wait`) before Relay answered: what
    was done isn't lost, its next call has the answer, once."""
    base = relay["url"]
    conv = call(base, "POST", "/api/conversations", {"provider_id": "sleeper"})
    call(base, "POST", f"/api/conversations/{conv['id']}/messages", {"text": "go"})
    delegation = relay["app"].state.relay.delegation
    token = relay["app"].state.relay.db.one("SELECT tools_token FROM conversations WHERE id = ?",
                                            (conv["id"],))["tools_token"]
    until(lambda: delegation.run_for(token) is not None)

    async def slow_wait(run, arguments):
        await asyncio.sleep(1.5)
        return "Subagent 1234abcd (Reviewer) is done: it looks good."

    monkeypatch.setattr(delegation, "wait", slow_wait)
    monkeypatch.setattr(runs_routes, "SLOW", 1)

    def body(name):
        return json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": name, "arguments": {}}}).encode()

    # The command is stopped half a second in: its connection goes
    host, port = base.removeprefix("http://").split(":")
    with socket.create_connection((host, int(port))) as stopped:
        stopped.sendall(f"POST /mcp/{token} HTTP/1.1\r\nHost: {host}\r\nContent-Type: application/json\r\n"
                        f"Content-Length: {len(body('wait'))}\r\n\r\n".encode() + body("wait"))
        time.sleep(0.5)
    time.sleep(1.5)

    def tool(name):
        return call(base, "POST", f"/mcp/{token}", json.loads(body(name)))["result"]["content"][0]["text"]

    told = tool("help")
    assert "Your wait call of" in told and "was stopped before Relay answered" in told
    assert told.endswith("Subagent 1234abcd (Reviewer) is done: it looks good.")
    assert "1234abcd" not in tool("help")  # once
    # One that waits for its answer gets it as always, and nothing is kept
    assert tool("wait") == "Subagent 1234abcd (Reviewer) is done: it looks good."
    assert not delegation.missed
