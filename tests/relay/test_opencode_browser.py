"""Relay's browser for opencode's browser tools: the protocol side (bridge/opencode_browser.py)
and the browser program (relay/container/browser.cjs)."""

import json
import queue
import shutil
import subprocess
import sys
import threading
import time

import pytest

from opencode_manager.bridge import opencode_browser
from opencode_manager.bridge.opencode_browser import RPC

# Answers each command with the action's type, and says its tabs first.
PROGRAM = """
import json, sys
print(json.dumps({"state": {"tabs": [], "focusedTabID": None}}), flush=True)
for line in sys.stdin:
    message = json.loads(line)
    action = message["command"]["action"]["type"]
    print(json.dumps({"requestID": message["requestID"], "outcome": {"type": "success",
          "result": {"value": {"did": action}, "files": []}}}), flush=True)
"""


class FakeOpencode:
    """opencode's side: attach held open, control events, command and result RPCs."""

    def __init__(self):
        self.calls = []
        self.events_queue = queue.Queue()
        self.detached = threading.Event()
        self.connection = None

    def _open(self, method, path, body=None, timeout=None):  # the attach call
        self.calls.append((method, path, body))
        self.connection = body["input"]["connectionID"]
        self.events_queue.put({"type": "rpc.experimental.browser.control",
                               "data": {"type": "attached", "connectionID": self.connection, "version": 4}})
        fake = self

        class Held:
            def read(self):
                fake.detached.wait(20)
                return b'{"output": "closed"}'

            def close(self):
                fake.detached.set()
        return Held()

    def events(self, path):
        yield {"type": "server.connected"}
        while not self.detached.is_set():
            try:
                yield self.events_queue.get(timeout=0.05)
            except queue.Empty:
                continue

    def request(self, method, path, body=None, timeout=None):
        self.calls.append((method, path, body))
        if path.startswith(f"{RPC}/command"):
            return {"output": {"action": {"type": "tabs.list"}, "files": []}}
        return {}

    def command(self, request_id):
        self.events_queue.put({"type": "rpc.experimental.browser.control",
                               "data": {"type": "command", "connectionID": self.connection, "requestID": request_id}})

    def rpc_calls(self, method):
        return [body["input"] for _, path, body in self.calls if path.startswith(f"{RPC}/{method}")]


def until(condition, seconds=10):
    deadline = time.time() + seconds
    while not condition():
        assert time.time() < deadline, "timed out"
        time.sleep(0.02)


def test_relay_stands_in_for_opencodes_desktop_browser(tmp_path):
    opencode = FakeOpencode()
    attachment = opencode_browser.attach(opencode, "ses_1", tmp_path, [sys.executable, "-c", PROGRAM],
                                         log=tmp_path / "browser.log")
    try:
        until(lambda: opencode.rpc_calls("attach"))
        attach = opencode.rpc_calls("attach")[0]
        assert attach["sessionID"] == "ses_1" and attach["version"] == 4
        assert f"location[directory]={tmp_path}".replace("/", "%2F") in opencode.calls[0][1]
        until(lambda: opencode.rpc_calls("state"))  # the tabs, once attached
        assert opencode.rpc_calls("state")[0]["state"] == {"tabs": [], "focusedTabID": None}
        opencode.command("req-1")
        until(lambda: opencode.rpc_calls("result"))
        result = opencode.rpc_calls("result")[0]
        assert result["requestID"] == "req-1" and result["outcome"]["result"]["value"] == {"did": "tabs.list"}
        # One per session: asked again, the same one
        assert opencode_browser.attach(opencode, "ses_1", tmp_path, [sys.executable, "-c", PROGRAM]) is attachment
    finally:
        attachment.stop()
    assert opencode.detached.is_set() and attachment.process.wait(10) is not None  # opencode lets go too
    assert "detached" in (tmp_path / "browser.log").read_text()


def test_when_the_browser_program_ends_the_attachment_does(tmp_path):
    opencode = FakeOpencode()
    attachment = opencode_browser.attach(opencode, "ses_2", tmp_path, [sys.executable, "-c", "pass"])
    until(lambda: attachment.stopped.is_set())
    assert opencode.detached.is_set()


def _image():
    from relay.containers import built_image
    try:
        found = shutil.which("podman") and subprocess.run(["podman", "image", "exists", built_image()],
                                                          capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return None
    return built_image() if found else None


@pytest.mark.skipif(not _image(), reason="Relay's container image isn't built here")
def test_the_browser_program_with_chromium(tmp_path):
    from relay.containers import CONTAINERFILE
    page = tmp_path / "page.html"
    page.write_text("<title>T</title><label>Name <input></label><button onclick=\"document.title='hi '+"
                    "document.querySelector('input').value\">Go</button>")
    folder = CONTAINERFILE.parent
    program = subprocess.Popen(["podman", "run", "--rm", "-i", "--network=host", "-v", f"{folder}:{folder}:ro",
                                "-v", f"{tmp_path}:{tmp_path}:ro", _image(), "node", str(folder / "browser.cjs")],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
    answers = {}

    def ask(request_id, action, **more):
        program.stdin.write(json.dumps({"requestID": request_id, "command": {"action": action, "files": [], **more}})
                            + "\n")
        program.stdin.flush()
        while request_id not in answers:
            message = json.loads(program.stdout.readline())
            if "requestID" in message:
                answers[message["requestID"]] = message["outcome"]
        return answers[request_id]

    try:
        # file:// isn't for opencode's tools, but fine to test the program with
        opened = ask("1", {"type": "tabs.open", "url": page.as_uri()})
        tab = opened["result"]["value"]
        assert opened["type"] == "success" and tab["id"].startswith("tab_") and tab["title"] == "T"
        snapshot = ask("2", {"type": "snapshot", "tabID": tab["id"]})["result"]["value"]["content"]
        textbox = snapshot.split('textbox "Name" [ref=')[1].split("]")[0]
        button = snapshot.split('button "Go" [ref=')[1].split("]")[0]
        ask("3", {"type": "fill", "tabID": tab["id"], "ref": textbox, "text": "Relay"})
        assert ask("4", {"type": "click", "tabID": tab["id"], "ref": button})["result"]["value"]["title"] == "hi Relay"
        shot = ask("5", {"type": "screenshot", "tabID": tab["id"]})["result"]
        assert shot["files"][0]["mime"] == "image/png" and shot["files"][0]["id"].startswith("file_")
        failed = ask("6", {"type": "lighthouse", "tabID": tab["id"]})
        assert failed["type"] == "failure" and failed["code"] == "unsupported"
        assert ask("7", {"type": "snapshot", "tabID": "tab_gone"})["code"] == "tab_unavailable"
    finally:
        program.stdin.close()
        program.wait(30)
