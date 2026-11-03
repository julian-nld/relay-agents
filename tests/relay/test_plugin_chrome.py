"""plugins/chrome: Relay's agents in the user's own Chrome. The plugin on Relay's own plugin host: its tools called as an
agent calls them, its routes asked as the extension's side panel asks them (each in the plugin's own threads, as in
Relay), and the extension zipped for the page. The extension itself, in Chromium: test_ui_chrome.py."""

import base64
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest

from relay.plugins import Call, PluginError, Plugins, ToolCall

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugins" / "chrome"
TOOLS = ["chrome_click", "chrome_open", "chrome_read", "chrome_screenshot", "chrome_scroll", "chrome_tabs", "chrome_type"]
ME = {"id": 1, "username": "me"}

_spec = importlib.util.spec_from_file_location("relay_plugin_tests", Path(__file__).with_name("test_plugins.py"))
_helpers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_helpers)
H, home, relay_app, signed_in = _helpers.H, _helpers.home, _helpers.relay_app, _helpers.signed_in


class Chrome:
    """The plugin loaded by Relay's plugin host, and the extension's side of it (its routes) and an agent's (its
    tools), each called in one of the plugin's threads."""

    def __init__(self, folder):
        (folder / "plugins").mkdir()
        (folder / "plugins" / "chrome").symlink_to(PLUGIN)
        self.host = Plugins(folder=folder / "plugins")
        self.host.load()
        found = self.host.found["chrome"]
        assert found.problem is None, found.problem
        self.plugin = found.plugin
        self.module = sys.modules["relay_plugins.chrome"]
        self.folder = folder
        self.version = -1

    def route(self, method, path, query=None, body=None, user=ME):
        """(status, answer), as Relay answers the extension."""
        try:
            return 200, self.plugin.pool.submit(self.host.call, "chrome", Call(method, path, {}, query or {}, body,
                                                                                user)).result(30)
        except PluginError as exc:
            return exc.status, {"error": str(exc)}

    def tool(self, name, arguments, conversation="c1", run="r1", directory=None):
        """An agent's call of chrome_<name>, waiting in the plugin's thread: its future."""
        call = ToolCall(conversation, run, 1, str(directory or self.folder), arguments)
        return self.plugin.pool.submit(self.host.call_tool, f"chrome_{name}", call)

    def poll(self, wait=0, user=ME):
        status, found = self.route("GET", "commands", {"since": str(self.version), "wait": str(wait)}, user=user)
        assert status == 200, found
        self.version = found["version"]
        return found["commands"]

    def next_command(self):
        for _ in range(50):
            commands = self.poll(wait=1)
            if commands:
                return commands[-1]
        raise AssertionError("no command came")

    def post(self, path, body, user=ME):
        return self.route("POST", path, body=body, user=user)[0]


def answer(future, timeout=10):
    """A tool's answer as the agent gets it: ("ok", text) or ("error", what it hears)."""
    try:
        return "ok", future.result(timeout)
    except PluginError as exc:
        return "error", str(exc)


@pytest.fixture
def chrome(home, tmp_path):
    found = Chrome(tmp_path)
    yield found
    found.plugin.pool.shutdown(wait=False, cancel_futures=True)


def test_its_tools_and_status(chrome):
    assert sorted(t.name for t in chrome.plugin.tools) == TOOLS
    assert chrome.route("GET", "status") == (200, {"plugin": "chrome", "version": chrome.plugin.version,
                                                   "connected": False})


def test_without_the_extension_it_says_so(chrome):
    kind, text = answer(chrome.tool("read", {}))
    assert kind == "ok" and "isn't connected" in text


def test_a_command_claimed_and_answered(chrome):
    chrome.poll()
    agent = chrome.tool("read", {"tab": 7}, conversation="c9", run="r9")
    command = chrome.next_command()
    assert (command["action"], command["args"], command["conversation_id"], command["run_id"]) == \
        ("read", {"tab": 7, "offset": 0}, "c9", "r9")
    assert chrome.route("GET", "status")[1]["connected"]
    assert chrome.post(f"commands/{command['id']}/claim", {"client": "a"}) == 200
    assert chrome.post(f"commands/{command['id']}/claim", {"client": "a"}) == 200  # (again: still its)
    assert chrome.post(f"commands/{command['id']}/claim", {"client": "b"}) == 409
    assert chrome.poll()[0]["claimed"] == "a"
    assert chrome.post(f"commands/{command['id']}/result", {"client": "b", "ok": True, "text": "no"}) == 409
    assert chrome.post(f"commands/{command['id']}/result", {"client": "a", "ok": True, "text": "Page"}) == 200
    assert answer(agent) == ("ok", "Page")
    assert chrome.poll() == []
    assert chrome.post(f"commands/{command['id']}/result", {"ok": True}) == 404  # (answered already)


def test_denied_or_failed(chrome):
    chrome.poll()
    agent = chrome.tool("click", {"ref": 3})
    chrome.post(f"commands/{chrome.next_command()['id']}/result", {"denied": True, "reason": "not now"})
    kind, text = answer(agent)
    assert kind == "ok" and "didn't allow" in text and "not now" in text
    agent = chrome.tool("click", {"ref": 3})
    chrome.post(f"commands/{chrome.next_command()['id']}/result", {"ok": False, "error": "No element [3] on the page now"})
    assert answer(agent) == ("error", "No element [3] on the page now")


def test_a_poll_waits_for_a_command_and_wakes_at_once(chrome):
    chrome.poll()
    took = {}

    def asked():
        start = time.monotonic()
        took["commands"] = chrome.poll(wait=10)
        took["seconds"] = time.monotonic() - start

    waiting = threading.Thread(target=asked)
    waiting.start()
    time.sleep(0.3)
    agent = chrome.tool("tabs", {})
    waiting.join(5)
    assert took["commands"][0]["action"] == "tabs" and took["seconds"] < 2
    chrome.post(f"commands/{took['commands'][0]['id']}/result", {"ok": True, "text": "tabs"})
    assert answer(agent) == ("ok", "tabs")


def test_two_wait_at_once_at_most(chrome):
    """Each waiting step holds one of the plugin's four threads: two at most, so the extension always has one."""
    chrome.poll()
    first, second = chrome.tool("read", {}), chrome.tool("read", {})
    for _ in range(50):
        commands = chrome.poll(wait=1)
        if len(commands) == 2:
            break
    assert len(commands) == 2
    assert "busy with 2 other steps" in answer(chrome.tool("read", {}))[1]
    for command in commands:
        chrome.post(f"commands/{command['id']}/result", {"ok": True, "text": "read"})
    assert (answer(first), answer(second)) == (("ok", "read"), ("ok", "read"))
    third = chrome.tool("read", {})
    chrome.post(f"commands/{chrome.next_command()['id']}/result", {"ok": True, "text": "and now"})
    assert answer(third) == ("ok", "and now")


def test_only_its_user_sees_and_answers_it(chrome):
    chrome.poll()
    other = {"id": 2, "username": "someone"}
    chrome.route("GET", "commands", {"since": "-1"}, user=other)
    agent = chrome.tool("read", {})
    command = chrome.next_command()
    assert chrome.route("GET", "commands", {"since": "-1"}, user=other)[1]["commands"] == []
    assert chrome.post(f"commands/{command['id']}/result", {"ok": True}, user=other) == 404
    chrome.post(f"commands/{command['id']}/result", {"ok": True, "text": "mine"})
    assert answer(agent) == ("ok", "mine")


def test_the_run_ending_takes_its_step_along(chrome):
    chrome.poll()
    agent = chrome.tool("read", {}, run="r5")
    command = chrome.next_command()
    chrome.host.run_ended({"conversation_id": "c1", "run_id": "r5", "user_id": 1, "provider_id": "claude-code",
                           "model": "", "status": "interrupted", "error": None, "subagent": False, "temporary": False,
                           "started_by": None})
    assert "Cancelled" in answer(agent)[1]
    assert chrome.poll() == []
    assert chrome.post(f"commands/{command['id']}/result", {"ok": True}) == 404


def test_unanswered_or_the_extension_gone(chrome):
    chrome.module.WAIT = 1
    chrome.poll()
    assert "within 5 minutes" in answer(chrome.tool("read", {}))[1]
    chrome.module.WAIT, chrome.module.GONE = 300, 1
    chrome.poll()
    assert "isn't connected" in answer(chrome.tool("read", {}), timeout=15)[1]


def test_a_screenshot_goes_in_the_conversations_folder(chrome, tmp_path):
    chrome.module.SHOTS = 2
    folder = tmp_path / "conversation"
    folder.mkdir()
    chrome.poll()
    names = []
    for i in range(3):
        agent = chrome.tool("screenshot", {}, directory=folder)
        image = base64.b64encode(b"\xff\xd8\xff" + bytes([i]) * 100).decode()
        chrome.post(f"commands/{chrome.next_command()['id']}/result",
                    {"ok": True, "text": "Tab 4", "image": image, "mime": "image/jpeg"})
        kind, text = answer(agent)
        assert kind == "ok" and ".relay-chrome/screenshot-" in text and text.endswith("Tab 4")
        names.append(text.split(".relay-chrome/")[1].split(" ")[0])
        time.sleep(1.1)  # (its name has the second)
    assert sorted(p.name for p in (folder / ".relay-chrome").iterdir()) == names[1:]  # (the last few kept)
    assert (folder / ".relay-chrome" / names[2]).read_bytes()[:3] == b"\xff\xd8\xff"
    agent = chrome.tool("screenshot", {}, directory=folder)
    chrome.post(f"commands/{chrome.next_command()['id']}/result", {"ok": True, "image": "not base64!"})
    assert answer(agent)[0] == "error"


def test_its_arguments(chrome):
    """Relay's own check of a tool's arguments (its schema), then the plugin's."""
    assert answer(chrome.tool("click", {"ref": 0}))[0] == "error"
    assert answer(chrome.tool("click", {"ref": True}))[0] == "error"
    assert answer(chrome.tool("type", {"ref": 2, "text": "x" * 20001}))[0] == "error"
    assert answer(chrome.tool("open", {})) == ("error", "chrome_open: url is needed")
    chrome.poll()
    agent = chrome.tool("type", {"ref": 2.0, "text": "Hi", "submit": 1})
    command = chrome.next_command()
    assert command["args"] == {"ref": 2, "text": "Hi", "submit": True}
    chrome.post(f"commands/{command['id']}/result", {"ok": True, "text": "typed"})
    assert answer(agent) == ("ok", "typed")
    assert chrome.route("GET", "commands", {"since": "x"})[0] == 400


def test_in_relay_with_the_extension_zipped_for_its_page(home, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    (home / "plugins").mkdir(parents=True, exist_ok=True)
    (home / "plugins" / "chrome").symlink_to(PLUGIN)
    with relay_app(tmp_path) as client:
        signed_in(client)
        listed = next(p for p in client.get("/api/plugins").json()["plugins"] if p["name"] == "chrome")
        assert listed["loaded"] and listed["problem"] is None
        assert set(TOOLS) <= {t["name"] for t in client.app.state.relay.plugins.tools()}
        assert client.get("/plugins/chrome/static/index.js").status_code == 200
        version = json.loads((PLUGIN / "extension" / "manifest.json").read_text())["version"]
        assert client.get("/api/plugins/chrome/version").json() == {"version": version}
        got = client.get("/api/plugins/chrome/extension").json()
        assert got["version"] == version and got["name"] == f"relay-chrome-{version}.zip"
        data = base64.b64decode(got["data"])
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        # (all in one folder: unzipped over the last one, Chrome reloads the extension from it)
        assert all(n.startswith("relay-chrome/") for n in names)
        assert {"relay-chrome/manifest.json", "relay-chrome/background.js", "relay-chrome/sidepanel.html",
                "relay-chrome/lib/agent.js", "relay-chrome/icons/icon-128.png"} <= set(names)
        assert not [n for n in names if "__pycache__" in n or "/." in n]
        zipped = zipfile.ZipFile(io.BytesIO(data))
        assert json.loads(zipped.read("relay-chrome/manifest.json"))["version"] == version
        # (its marked and DOMPurify are Relay's own, linked: in the zip as the files themselves)
        assert zipped.read("relay-chrome/vendor/marked.min.js") == (ROOT / "src/relay/static/vendor/marked.min.js").read_bytes()
        # The same files, the same zip (each file's time is fixed)
        assert base64.b64decode(client.get("/api/plugins/chrome/extension").json()["data"]) == data
        if shutil.which("node"):
            for name in (n for n in names if n.endswith(".js") and "/vendor/" not in n):
                checked = subprocess.run(["node", "--check", "--input-type=module", "-"],
                                         input=zipped.read(name).decode(), capture_output=True, text=True)
                assert checked.returncode == 0, (name, checked.stderr)
