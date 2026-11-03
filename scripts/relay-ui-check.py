#!/usr/bin/env python3
"""Click through Relay's web UI in a headless browser and save screenshots.

Starts a throwaway Relay (temporary data, fake chat and agent providers),
walks through setup, chat, a code task, settings, themes and a phone-sized
screen, and fails on any assertion or browser console error.

Needs Playwright (dev only):
    pip install --target vendor-dev playwright && PYTHONPATH=vendor-dev python3 -m playwright install chromium
Run:
    PYTHONPATH=vendor-dev python3 scripts/relay-ui-check.py [screenshot-dir]
"""

import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

# A fake agent that hands the question to a subagent with Relay's tools script.
FAKE_ORCHESTRATOR = """\
import json, os, subprocess, sys, urllib.request, uuid
session = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else "orch-" + uuid.uuid4().hex[:8]
prompt = sys.stdin.read()
def emit(**e): print(json.dumps(e), flush=True)
def rpc(method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    request = urllib.request.Request(os.environ["RELAY_MCP_URL"], data=body, \
headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(request, timeout=120).read())["result"]
emit(type="session", session=session)
emit(type="step", agent=None, model="fake/orchestrator")
rpc("initialize", {"protocolVersion": "2025-06-18"})
if "permission" in prompt:  # ask the user, as Claude Code's permission prompt does
    result = rpc("tools/call", {"name": "approve_permission", "arguments": {
        "tool_name": "Bash", "input": {"command": "make deploy"}, "tool_use_id": "p1"}})
    allowed = json.loads(result["content"][0]["text"])["behavior"] == "allow"
    emit(type="text", delta="Deploying." if allowed else "Not deploying.")
    emit(type="done", status="succeeded")
    sys.exit()
tools = os.environ["RELAY_TOOLS"]
def shell(call_id, *args, stdin=None):
    command = " ".join([tools, *args])
    emit(type="tool_call", id=call_id, name="bash", input={"command": command})
    out = subprocess.run([tools, *args], input=stdin, capture_output=True, text=True).stdout
    emit(type="tool_result", id=call_id, name="bash", ok=True, output=out)
    return out
shell("a1", "agents")
# "paid": a provider that isn't allowed without asking, so the user is asked
shell("d1", "delegate", "--provider", "fake-agent" if "paid" in prompt else "fake-chat", "--label", "Chat helper",
      stdin=prompt)
text = ""
for i in range(20):
    text += shell(f"w{i}", "wait")
    if "Still working" not in text.split("===")[-1]:
        break
emit(type="text", delta="My helper looked into it: " + ("it answered." if "Relay" in text else "no luck."))
emit(type="done", status="succeeded")
"""

# An agent that integrates a new provider or repairs one (relay/creations.py): its plan by Relay's tool, then the
# guide's example made a kind of its own (what an agent would build), or a line added to the layer it repairs, in the
# folder Relay gave it.
FAKE_BUILDER = """\
import json, os, re, subprocess, sys
from pathlib import Path
prompt = sys.stdin.read()
def emit(**e): print(json.dumps(e), flush=True)
emit(type="session", session="builder-1")
emit(type="step", agent=None, model="fake/builder")
repairing = Path("REPAIR.md").exists()
if "provider_plan" in prompt:
    plan = ("## The repair\\n\\n- Its parser reads the answer's pieces again." if repairing else
            "## Toy agent\\n\\n- Run `toy-agent` (the guide's example) in its box; its JSON lines become Relay's \
events."
            "\\n- Sessions: `--resume`. Models: toy-small, toy-large.")
    done = subprocess.run([os.environ["RELAY_TOOLS"], "repair", "plan", *([] if repairing else ["toy2", "toy-agent"])],
                          input=plan, capture_output=True, text=True)
    emit(type="text", delta="Handed in my plan: " + (done.stdout or done.stderr).strip()[:200])
elif repairing:
    layer = Path(re.search(r"^- `(relay/[^`]+[.]py)`", Path("REPAIR.md").read_text(), re.M).group(1))
    layer.write_text(layer.read_text() + "\\n# repaired by the fake builder\\n")
    emit(type="text", delta="Repaired it; repair_check passes.")
else:
    layer = Path("relay/providers/toy2/provider.py")
    layer.write_text(layer.read_text().replace('KIND = "example"', 'KIND = "toy-two"')
                     .replace('"kind": "example"', '"kind": "toy-two"').replace('LISTED = False', 'LISTED = True'))
    emit(type="text", delta="Built it: the example, as a kind of its own; repair_check passes.")
emit(type="done", status="succeeded")
"""

# An agent whose usage limit is reached; it resets in an hour.
FAKE_LIMITED = """\
import json, sys, time
sys.stdin.read()
def emit(**e): print(json.dumps(e), flush=True)
emit(type="session", session="lim-1")
emit(type="limit", status="rejected", resets_at=time.time() + 3600, window="five_hour",
     windows={"five_hour": {"utilization": 1, "resets_at": time.time() + 3600}})
emit(type="error", message="You've hit your limit")
emit(type="done", status="failed")
"""

ROOT = Path(__file__).resolve().parents[1]
# The plugins that come with Relay, and the parts a plugin of your own brings (own_parts)
PLUGINS = ("notes", "usage_limits", "server", "acp", "openhands", "vscodium", "chrome")
PARTS = {}

FAKE_CHAT = (
    '"""Fake OpenAI-compatible server for UI testing: /v1/models and streaming chat."""\n'
    'import json, time\nfrom http.server import BaseHTTPRequestHandler, ThreadingHTTPServer\n'
    'ANSWER = """Here\'s a quick overview of **Relay** 🚀\n'
    '\n1. It streams answers token by token\n2. It renders *markdown*, tables and code\n'
    '\n| Feature | Status |\n|---|---|\n| Chat | ✅ |\n'
    '| Code | ✅ |\n\n```python\ndef greet(name: str) -> str:\n'
    '    return f"Hello, {name}!"\n```\n\nAnything else you\'d like to know?"""\n'
    'class H(BaseHTTPRequestHandler):\n    def log_message(self, *a): pass\n'
    '    def do_GET(self):\n        self.send_response(200); self.send_header("Content-Type", "application/json"); '
    'self.end_headers()\n        self.wfile.write(json.dumps({"data": [{"id": "fake-small"}, {"id": '
    '"fake-large"}]}).encode())\n    def do_POST(self):\n'
    '        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))\n'
    '        if req.get("stream") is False:  # a title request\n'
    '            self.send_response(200); self.end_headers()\n'
    '            self.wfile.write(json.dumps({"choices": [{"message": {"content": "Tell me about Relay '
    '(overview)"}}]}).encode())\n            return\n'
    '        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()\n'
    '        def chunk(delta, usage=None):\n            d = {"choices": [{"index": 0, "delta": delta}]} '
    'if delta else {"choices": [], "usage": usage}\n'
    '            self.wfile.write(f"data: {json.dumps(d)}\\n\\n".encode()); self.wfile.flush()\n'
    '        for piece in ["The user wants an overview. ", "I\'ll use a list, a table and code."]:\n'
    '            chunk({"reasoning_content": piece}); time.sleep(0.05)\n'
    '        turns = sum(1 for m in req["messages"] if m["role"] == "user")\n'
    '        text = ANSWER if turns == 1 else f"This is reply #{turns}. I remember {sum(1 for m in '
    'req[\'messages\'] if m[\'role\'] != \'system\')} messages."\n'
    '        for i in range(0, len(text), 8):\n'
    '            chunk({"content": text[i:i+8]}); time.sleep(0.015)\n'
    '        self.wfile.write(("data: " + json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": '
    '"stop"}]}) + "\\n\\n").encode())\n        chunk(None, {"prompt_tokens": 42, "completion_tokens": '
    '128})\n        self.wfile.write(b"data: [DONE]\\n\\n")\n'
    'ThreadingHTTPServer(("127.0.0.1", 48130), H).serve_forever()\n')

FAKE_AGENT = (
    '"""Fake coding agent emitting uniform events, with tool calls."""\n'
    'import json, sys, time, uuid\nsession = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else '
    '"sess-" + uuid.uuid4().hex[:8]\nprompt = sys.stdin.read()\n'
    'def emit(**e): print(json.dumps(e), flush=True); time.sleep(0.25)\n'
    'emit(type="session", session=session)\nemit(type="step", agent="build", model="fake/agent")\n'
    'emit(type="reasoning", delta="Let me look at the project first.")\n'
    'emit(type="tool_call", id="t1", name="shell", input={"command": "ls -la", "description": "List '
    'files"})\nemit(type="tool_result", id="t1", name="shell", ok=True, output="total 8\\ndrwxr-xr-x '
    '2 pi pi 4096 .\\n-rw-r--r-- 1 pi pi  120 main.py")\n'
    'emit(type="permission", id="p1", action="edit", resources=["main.py"], decision="once")\n'
    'emit(type="tool_call", id="t2", name="edit", input={"file_path": "main.py", "old": "print(1)", '
    '"new": "print(2)"})\nopen("main.py", "w").write("print(2)\\n")\n'
    'emit(type="tool_result", id="t2", name="edit", ok=True, output="Edited main.py")\n'
    'emit(type="tool_call", id="t3", name="shell", input={"command": "pytest -q"})\n'
    'emit(type="tool_result", id="t3", name="shell", ok=False, output="1 failed, 3 passed")\n'
    'for w in ["I updated ", "`main.py` ", "and ran the tests (the site: http://localhost:5173/). ", '
    '"One test still fails; ", "want me to fix it?"]:\n'
    '    emit(type="text", delta=w)\nemit(type="usage", tokens={"input_tokens": 1200, "output_tokens": '
    '310}, cost=0.0123, billing="subscription")\n'
    'emit(type="done", status="succeeded")\n')


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(port):
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"nothing listening on port {port}")


def shot(page, name):
    # Wait for panels and menus to finish sliding in, so the shot shows them as they end up.
    # (Endless ones, like a pulsing dot, never finish.)
    page.wait_for_function("() => document.getAnimations().every((a) => a.playState !== 'running'"
                           " || a.effect?.getTiming().iterations === Infinity)",
                           timeout=5000)
    page.screenshot(path=f"{SHOTS_DIR}/{name}.png")
    no_stray_text(page, name)


def view(page, name):
    """The side column's view (the activity bar's icon for it), the column shown; the main area stays as it is."""
    shown = page.locator(f".panel-view.shown[data-view={name}]")
    if shown.count() and not page.locator(".layout.sidebar-collapsed").count():
        return
    page.locator(f".activity-bar .bar-item[data-view={name}]").click()
    expect(shown).to_be_visible()


def in_view(page, name, button):
    """A button in a view's header (New conversation, New computer task, …): that view shown first."""
    view(page, name)
    page.locator(f".panel-view[data-view={name}] .panel-head").get_by_role("button", name=button, exact=True).click()


def new_conversation(page):
    in_view(page, "chats", "New conversation")


def scripts_menu(page, item):
    """The Scripts view's + (New script, From a template, With an agent): the Scripts page opens with it."""
    in_view(page, "scripts", "New script")
    page.locator(".menu button", has_text=item).click()


def tasks_page(page):
    """The Tasks page (all of them, with their changes): the Tasks view's button for it."""
    in_view(page, "tasks", "All tasks, with their changes")
    expect(page.locator(".tasks")).to_be_visible()


MERGED = []
WORKSPACE = []  # the workspace root, once made
PAGES = []  # the page being tested, for a screenshot when something fails
CATCHER = {"url": "", "received": []}  # a stand-in ntfy server


def start_catcher():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Catch(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            CATCHER["received"].append(dict(self.headers))
            self.send_response(200)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Catch)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    CATCHER["url"] = f"http://127.0.0.1:{server.server_address[1]}"
TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d494441"
    "54789c6310585cff1f00042b0232cf63ad920000000049454e44ae426082")


def served_page(page, BASE):
    """A server here (an agent's), opened at /port/<n>/ through Relay: its page, and the script it asks
    for at an absolute path (/app.js), which Relay sends back under the prefix."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Site(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            pages = {"/": ("text/html", (b"<!doctype html><title>Site</title><link rel=icon href=data:,>"
                                         b"<p id=out>waiting</p><script src='/app.js'></script>")),
                     "/app.js": ("text/javascript", (b"document.getElementById('out').textContent = 'loaded at ' "
                                                     b"+ location.pathname;"))}
            kind, body = pages.get(self.path, ("text/plain", b"no"))
            self.send_response(200 if self.path in pages else 404)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        back = page.url
        port = server.server_address[1]
        page.goto(f"{BASE}/port/{port}")
        expect(page.locator("#out")).to_have_text(f"loaded at /port/{port}/")
        page.goto(back)
    finally:
        server.shutdown()


def model_ranking(page, BASE):
    """AgentOS's model ranking (relay.ranking), as the Governor would leave it (put in the throwaway's database):
    its panel on the dashboard, the ranks in the model picker and in a fallback loop."""
    import sqlite3
    now = time.time()
    with sqlite3.connect(WORKSPACE[0].parent / "data" / "relay" / "relay.db") as db:
        db.execute("INSERT INTO meta (key, value) VALUES ('model_ranking', ?)", (json.dumps(
            {"at": now, "models": [["fake-chat", "fake-small"], ["fake-chat", "fake-large"]]}),))
        db.executemany("INSERT INTO model_scores (name, score, estimated, benchmark, source, noted) "
                       "VALUES (?, ?, ?, ?, ?, ?)",
                       [("Fake Large 2", 61.2, 0, "Artificial Analysis Intelligence Index", "https://example.org/aa",
                         now),
                        ("Fake Small", 30, 1, "Artificial Analysis Intelligence Index", None, now)])
        db.executemany("INSERT INTO model_names (provider_id, model, name, noted) VALUES ('fake-chat', ?, ?, ?)",
                       [("fake-large", "Fake Large 2", now), ("fake-small", "Fake Small", now)])
        user = db.execute("SELECT id FROM users").fetchone()[0]  # (and a report of the Governor's: notify.report)
        db.execute("INSERT INTO agentos_events (user_id, at, kind, actor, detail) VALUES (?, ?, 'report', "
                   "'governor', ?)",
                   (user, now, json.dumps({"text": "The model ranking is up to date: 2 models."})))
    page.reload()
    expect(page.locator(".os-panel.reports")).to_contain_text("The model ranking is up to date: 2 models.")
    panel = page.locator(".os-panel.ranking")
    expect(panel.get_by_role("button", name="Start over")).to_be_visible()
    expect(panel.locator(".rank-row").first).to_contain_text("#1")
    expect(panel.locator(".rank-row").first).to_contain_text("Fake Large 2")
    expect(panel.locator(".rank-row").nth(1)).to_contain_text("estimated")
    panel.scroll_into_view_if_needed()
    shot(page, "08h-model-ranking")
    write = {"X-Relay-Request": "1", "Content-Type": "application/json"}
    assert page.request.put(BASE + "/api/fallback-loops", headers=write, data=json.dumps({"loops": [
        {"name": "High Quality", "entries": [{"provider": "fake-chat", "model": "fake-large"},
                                             {"provider": "fake-chat", "model": "fake-small"}]}]})).ok
    new_conversation(page)
    page.locator(".composer .picker").first.click()
    page.locator(".menu button", has_text="Fake Chat").click()
    page.locator(".composer .picker").nth(1).click()  # the model
    expect(page.locator(".menu button", has_text="fake-large").locator(".menu-hint")).to_have_text("#1")
    shot(page, "08h-model-picker-ranks")
    page.keyboard.press("Escape")
    page.get_by_role("button", name="Settings").first.click()
    page.locator(".settings nav").get_by_role("button", name="Usage", exact=True).click()
    loop = page.locator(".loop-card", has_text="High Quality")
    expect(loop.locator(".loop-rank")).to_have_text(["Intelligence index: #1", "Intelligence index: #2"])
    loop.scroll_into_view_if_needed()
    shot(page, "08h-loop-ranks")
    page.keyboard.press("Escape")
    page.goto(BASE + "/agentos")


def scripts_page(page):
    """Scripts (relay.scripts): one from a template, edited and run in its folder's WorkBox, signed in as you (Relay's
    SDK), its output as it comes and the chat it started; then one an agent writes (a code task with the skill)."""
    draft = page.evaluate("localStorage.getItem('relay.draft')")  # (the phone's part goes on from it)
    new_conversation(page)
    page.locator(".composer .picker").first.click()
    page.locator(".menu button", has_text="Fake Agent").click()  # (the agent that writes one: never a real one here)
    # The Scripts view: the column lists them (Relay's examples too); the main area stays as it was
    view(page, "scripts")
    expect(page.locator(".scripts-list .panel-group.examples .panel-item")).to_have_count(10)
    expect(page.locator(".welcome")).to_be_visible()
    shot(page, "08i-scripts-view")
    scripts_menu(page, "From a template")
    expect(page).to_have_url(re.compile(r"/scripts$"))
    expect(page.locator(".scripts-folder")).to_contain_text(str(WORKSPACE[0] / "scripts"))
    expect(page.locator(".scripts-empty")).to_be_visible()
    expect(page.locator(".template-item")).to_have_count(10)
    shot(page, "08i-script-templates")
    page.get_by_role("button", name=re.compile(r"^ask\.py ")).click()  # (not code_task.py)
    page.locator(".modal").get_by_role("button", name="Create").click()
    editor = page.get_by_label("private/ask.py's code")  # (new ones are private)
    expect(editor).to_have_value(re.compile("from relay_sdk import"))
    expect(page).to_have_url(re.compile(r"/scripts/private/ask\.py$"))  # (its own address)
    # At its start, where its settings are (a simulated user found new ones scrolled to their end)
    assert editor.evaluate("(e) => [e.selectionStart, e.scrollTop]") == [0, 0]
    # Coloured as Python, under the textarea's (transparent) letters, which stay where its copy's are
    expect(page.locator(".script-highlight .hljs-keyword", has_text=re.compile("^from$")).first).to_be_attached()
    assert page.evaluate("""() => {
        const area = document.querySelector('.script-editor-text'), copy = document.querySelector('.script-highlight');
        const a = getComputedStyle(area), c = getComputedStyle(copy);
        return ['fontFamily', 'fontSize', 'lineHeight', 'paddingTop', 'paddingLeft', 'tabSize', 'whiteSpace']
            .every((k) => a[k] === c[k]) && a.color === 'rgba(0, 0, 0, 0)';
    }""")
    editor.fill(editor.input_value().replace("PROVIDER = None", 'PROVIDER = "fake-chat"'))
    expect(page.locator(".scripts-list .panel-item.active .script-unsaved")).to_be_visible()
    editor.press("Control+s")
    expect(page.locator(".scripts-list .panel-item.active .script-unsaved")).to_have_count(0)
    page.get_by_label("Its arguments").fill("what is Relay?")
    page.get_by_role("button", name="Run", exact=True).click()
    output = page.locator(".script-output")
    expect(output).to_contain_text("quick overview of **Relay**", timeout=120000)  # (its WorkBox starts first)
    expect(page.locator(".script-output-head .badge")).to_have_text("Succeeded", timeout=30000)
    expect(output).to_contain_text("tools: none")
    made = page.locator(".script-made .chip")
    expect(made).to_have_text("Asked by a script")
    shot(page, "08j-script-run")
    made.click()
    expect(page.locator(".header .title-btn")).to_contain_text("Asked by a script")
    view(page, "scripts")
    page.locator(".scripts-list .panel-group.private .panel-item", has_text="ask.py").click()  # (back at it, its run)
    expect(page.locator(".script-run-item")).to_have_count(1)
    expect(page.locator(".script-output")).to_contain_text("quick overview")
    # Relay's examples: read-only, copied to be changed
    example = page.locator(".scripts-list .panel-group.examples .panel-item", has_text="refusals.py")
    example.click()
    expect(page.get_by_label("public/examples/refusals.py's code")).to_have_attribute("readonly", "")
    expect(page.locator(".script-bar .badge")).to_have_text("Example")
    expect(page.locator(".script-bar").get_by_role("button", name="Save")).to_have_count(0)
    shot(page, "08ka-script-example")
    page.get_by_role("button", name="Copy to my scripts").click()
    copy = page.get_by_label("private/refusals.py's code", exact=True)
    expect(copy).to_have_value(re.compile("from resl import"))
    expect(copy).not_to_have_attribute("readonly", "")
    expect(page.locator(".scripts-list .panel-group.private .panel-item", has_text="refusals.py")).to_have_count(1)
    # Made public: under Public, for agents too
    page.locator(".script-bar").get_by_role("button", name="More").click()
    page.locator(".menu button", has_text="Make public").click()
    no_stray_text(page, "the Make public dialog")
    page.locator(".modal").get_by_role("button", name="Make public").click()
    expect(page.locator(".scripts-list .panel-group.public .panel-item", has_text="refusals.py")).to_have_count(1)
    expect(page.locator(".script-bar .badge.public")).to_have_text("Public")
    page.locator(".scripts-list .panel-group.private .panel-item", has_text="ask.py").click()
    # The documentation beside it, and the folder for VS Code (a network drive)
    page.get_by_role("button", name="Documentation").click()
    docs = page.locator(".scripts-docs")
    expect(docs.locator(".md h2", has_text="Signing in")).to_be_visible()
    expect(docs).to_contain_text("signed in as you")
    shot(page, "08k-script-docs")
    page.keyboard.press("Escape")
    expect(docs).to_have_count(0)
    page.get_by_role("button", name="Open in VS Code").click()
    drive = page.locator(".modal", has_text="Open in VS Code")
    expect(drive.locator(".skill-folder code")).to_contain_text("/dav/")
    expect(drive).to_contain_text("pip install")
    shot(page, "08l-scripts-drive")
    drive.get_by_role("button", name="Done").click()
    expect(page.locator(".scripts-shared")).to_have_text("Shared as a drive")  # (it stays shared: said so)
    scripts_menu(page, "With an agent")
    writer = page.locator(".modal", has_text="Write a script with an agent")
    expect(writer).to_contain_text("With Fake Agent")
    writer.get_by_label("What should it do?").fill("print the time")
    writer.get_by_role("button", name="Write it").click()
    expect(page.locator(".header .title-btn")).to_contain_text("Script: print the time")
    expect(page.locator(".msg.assistant").first).to_contain_text("want me to fix it?", timeout=30000)
    # (in public/; the column's list kept current while it's shown, the chat on the right as it is)
    expect(page.locator(".scripts-list .panel-group.public .panel-item",
                        has_text="main.py")).to_be_visible(timeout=20000)
    expect(page.locator(".header .title-btn")).to_contain_text("Script: print the time")
    page.evaluate("d => d === null ? localStorage.removeItem('relay.draft') : localStorage.setItem('relay.draft', d)",
                  draft)


def temporary_chats(page, BASE):
    """A temporary chat: in no list, its chip and line, gone when it's left; another one kept."""
    draft = page.evaluate("localStorage.getItem('relay.draft')")
    new_conversation(page)
    page.locator(".composer .picker").first.click()
    page.locator(".menu button", has_text="Fake Chat").click()
    toggle = page.locator(".temporary-toggle")
    expect(toggle).to_have_attribute("aria-pressed", "false")
    toggle.click()
    expect(toggle).to_have_attribute("aria-pressed", "true")
    expect(page.locator(".welcome h1")).to_have_text("Temporary chat")
    shot(page, "08m-temporary-new")
    page.get_by_label("Message").fill("Something to keep to myself")
    page.get_by_label("Message").press("Enter")
    expect(page.locator(".header .chip.temporary")).to_be_visible()
    expect(page.locator(".temporary-note")).to_contain_text("It goes when you leave it")
    expect(page.locator(".msg.assistant").first).to_contain_text("quick overview", timeout=15000)
    expect(page.locator(".composer .send-btn.stop")).to_have_count(0, timeout=15000)  # (its answer done)
    expect(page.locator(".conv", has_text="Temporary chat")).to_have_count(0)  # (in no list)
    shot(page, "08n-temporary-chat")
    gone = page.url.rsplit("/", 1)[-1]
    new_conversation(page)  # leaving it: it goes
    expect(page.locator(".temporary-toggle")).to_have_attribute("aria-pressed", "false")  # (off for each new chat)
    deadline = time.time() + 10  # (asked from outside the page: its 404 is no console error)
    while page.request.get(f"{BASE}/api/conversations/{gone}").status != 404:
        assert time.time() < deadline, "the temporary chat wasn't deleted when it was left"
        time.sleep(0.2)
    # An address of no chat: a new chat, at its own address, and why
    page.goto(BASE + "/c/no-such-chat")
    expect(page.locator(".toast.error")).to_contain_text("Conversation not found")
    expect(page).to_have_url(re.compile(r"^[^?#]*/$"))
    expect(page.get_by_label("Message")).to_be_visible()
    # Another one, kept: a chat like any other
    page.locator(".temporary-toggle").click()
    page.get_by_label("Message").fill("Worth keeping")
    page.get_by_label("Message").press("Enter")
    expect(page.locator(".temporary-note")).to_be_visible()
    page.locator(".temporary-note").get_by_role("button", name="Keep it").click()
    # (named as a chat is, after its first message: not "Temporary chat" for good)
    expect(page.locator(".conv", has_text="Worth keeping")).to_have_count(1)
    expect(page.locator(".conv", has_text="Temporary chat")).to_have_count(0)
    expect(page.locator(".header .chip.temporary")).to_have_count(0)
    page.evaluate("d => d === null ? localStorage.removeItem('relay.draft') : localStorage.setItem('relay.draft', d)",
                  draft)


def usage_limits_plugin(page):
    """plugins/usage_limits: its button (a badge: Fake Limited's window is used up), its page with each window's % left
    over time, the pointer's value in a tooltip, the table view, and its Settings tab."""
    button = page.locator(".activity-bar .plugin-item[data-plugin='usage_limits']")
    button.click()
    expect(page).to_have_url(re.compile(r"/p/usage_limits$"))
    limits = page.locator(".plugin-page .ul-page")
    expect(limits.locator("h1")).to_have_text("Usage limits")
    # (no empty bar above a plugin's page: its header holds only a phone's buttons)
    assert page.locator(".plugin-header").bounding_box()["height"] == 0
    page.set_viewport_size({"width": 390, "height": 800})
    expect(page.locator(".plugin-header").get_by_title("Open sidebar")).to_be_visible()
    page.set_viewport_size({"width": 1280, "height": 820})
    expect(page.locator(".plugin-header")).to_be_empty()
    card = limits.locator(".ul-card", has_text="Fake Limited")  # (its page looked now: the run that hit it is in)
    expect(card.locator(".ul-window-head")).to_contain_text("0% left")
    expect(card.locator(".badge.danger")).to_have_text("low")
    expect(button.locator(".bar-badge")).to_have_text("1")
    chart = card.locator(".ul-chart svg").first
    expect(chart.locator(".ul-line")).to_have_count(1)
    hit = chart.locator(".ul-hit")
    hit.scroll_into_view_if_needed()  # (below the other logins' cards)
    box = hit.bounding_box()
    hit.hover(position={"x": box["width"] - 4, "y": box["height"] / 2})
    expect(card.locator(".ul-tip")).to_contain_text("% left")
    shot(page, "08q-plugin-usage-limits")
    limits.get_by_role("button", name="1 day").click()
    expect(limits.get_by_role("button", name="1 day")).to_have_attribute("aria-pressed", "true")
    limits.get_by_role("button", name="Table").click()
    expect(card.locator(".ul-table tbody tr").first).to_contain_text("0%")
    limits.get_by_role("button", name="Table").click()
    expect(card.locator(".ul-chart")).to_have_count(1)
    page.get_by_role("button", name="Settings").first.click()
    page.locator(".settings nav button", has_text="Usage limits").click()
    expect(page.get_by_label("Low at")).to_have_value("10")
    page.keyboard.press("Escape")


def server_plugin(page):
    """plugins/server: its page with this machine's tiles (each with its sparkline) and the running containers."""
    page.locator(".activity-bar .plugin-item[data-plugin='server']").click()
    expect(page).to_have_url(re.compile(r"/p/server$"))
    server = page.locator(".plugin-page .sv-page")
    expect(server.locator("h1")).to_have_text("Server")
    expect(server.locator(".sv-lead")).to_contain_text("cores")
    expect(server.locator(".sv-tile", has_text="CPU")).to_be_visible()
    expect(server.locator(".sv-tile", has_text="Memory")).to_contain_text("used")
    expect(server.locator(".sv-tile").filter(has_text="Disk /").first).to_contain_text("free")
    expect(server.locator(".sv-table tbody tr").first).to_be_visible()
    shot(page, "08r-plugin-server")


def acp_plugin(page):
    """plugins/acp: its Settings tab, with the setups to copy, an app password made for them."""
    page.get_by_role("button", name="Settings").first.click()
    page.locator(".settings nav").get_by_role("button", name="ACP", exact=True).click()
    pane = page.locator(".settings .pane")
    expect(pane.locator("h3")).to_have_text("ACP")
    expect(pane.locator(".acp-code").first).to_contain_text("relay-acp.py")
    pane.get_by_role("button", name="Make one").click()
    expect(pane.locator(".acp-made")).to_contain_text("ACP client")
    expect(pane.locator(".acp-code").first).to_contain_text("RELAY_APP_PASSWORD=relay_")
    shot(page, "08s-plugin-acp")
    page.keyboard.press("Escape")


def vscodium_plugin(page):
    """plugins/vscodium: its Settings tab, its steps, an app password made for the extension and the .vsix downloaded
    (not installed here: that would go into this machine's own VSCodium server)."""
    page.get_by_role("button", name="Settings").first.click()
    page.locator(".settings nav").get_by_role("button", name="VSCodium", exact=True).click()
    pane = page.locator(".settings .pane")
    expect(pane.locator("h3")).to_have_text("VSCodium")
    expect(pane.locator(".vsc-step")).to_have_count(3)
    expect(pane.get_by_role("button", name="Install on this machine")).to_be_visible()
    with page.expect_download() as got:
        pane.get_by_role("button", name=re.compile(r"^Download relay-[\d.]+\.vsix$")).click()
    assert re.fullmatch(r"relay-[\d.]+\.vsix", got.value.suggested_filename), got.value.suggested_filename
    pane.get_by_role("button", name="Make one").click()
    expect(pane.locator(".vsc-made")).to_contain_text("VSCodium")
    expect(pane.get_by_role("link", name="Sign in VSCodium on this computer")).to_have_attribute(
        "href", re.compile(r"^vscodium://relay\.relay/sign-in\?url=http.*&password=relay_"))
    expect(pane.locator(".vsc-code").first).to_contain_text("http://127.0.0.1:")
    expect(pane.locator(".vsc-code").nth(1)).to_contain_text("http://localhost:")
    expect(pane.locator(".vsc-code").nth(2)).to_contain_text("relay_")
    shot(page, "08s-plugin-vscodium")
    page.keyboard.press("Escape")


def chrome_plugin(page):
    """plugins/chrome: its Settings tab, its steps, the extension downloaded as a zip and an app password made for it
    (the extension itself in Chromium: tests/relay/test_ui_chrome.py)."""
    page.get_by_role("button", name="Settings").first.click()
    page.locator(".settings nav").get_by_role("button", name="Chrome", exact=True).click()
    pane = page.locator(".settings .pane")
    expect(pane.locator("h3")).to_have_text("Chrome")
    expect(pane.locator(".chr-step")).to_have_count(3)
    with page.expect_download() as got:
        pane.get_by_role("button", name=re.compile(r"^Download relay-chrome-[\d.]+\.zip$")).click()
    assert re.fullmatch(r"relay-chrome-[\d.]+\.zip", got.value.suggested_filename), got.value.suggested_filename
    expect(pane.locator(".chr-said")).to_contain_text("No Chrome is connected now")
    pane.get_by_role("button", name="Make one").click()
    expect(pane.locator(".chr-made")).to_contain_text("Chrome")
    expect(pane.locator(".chr-code").first).to_contain_text("http://127.0.0.1:")
    expect(pane.locator(".chr-code").nth(1)).to_contain_text("relay_")
    shot(page, "08s-plugin-chrome")
    page.keyboard.press("Escape")


def openhands_plugin(page):
    """plugins/openhands, not set up: its page says what Set up does (not pressed here: no image is pulled)."""
    page.locator(".activity-bar .plugin-item[data-plugin='openhands']").click()
    expect(page).to_have_url(re.compile(r"/p/openhands$"))
    openhands = page.locator(".plugin-page .oh-page")
    expect(openhands.locator("h1")).to_have_text("OpenHands")
    expect(openhands.locator(".oh-card")).to_contain_text("about 4.4 GB")
    expect(openhands.get_by_role("button", name=re.compile(r"^(Set up|Download and start)$"))).to_be_visible()
    shot(page, "08t-plugin-openhands")


def notes_plugin(page, BASE):
    """The example plugin (plugins/notes), installed as its README says: its button on the bar, its page (with a note
    its hook left for Fake Limited's failed run), its own Settings tab, and Settings → Plugins."""
    button = page.locator(".activity-bar .plugin-item[data-plugin='notes']")
    expect(button).to_be_visible()
    button.click()
    expect(page).to_have_url(re.compile(r"/p/notes$"))
    notes = page.locator(".plugin-page .notes")
    expect(notes.locator("h1")).to_have_text("Notes")
    expect(notes).to_contain_text("A run of fake-limited failed")
    expect(button.locator(".bar-badge")).to_be_visible()  # (how many there are)
    notes.get_by_label("New note").fill("Water the plants")
    notes.get_by_role("button", name="Add").click()
    expect(notes.locator(".notes-item").first).to_contain_text("Water the plants")
    expect(notes.locator(".notes-item").first).to_contain_text("alex · just now")
    expect(button).to_have_class(re.compile(r"\bactive\b"))  # (its page is open)
    shot(page, "08p-plugin-notes")
    notes.locator(".notes-item").first.get_by_role("button", name="Remove").click()
    expect(notes).not_to_contain_text("Water the plants")
    page.reload()  # (its page on its own, as a link opens it)
    expect(page.locator(".plugin-page .notes h1")).to_have_text("Notes")
    expect(button).to_have_class(re.compile(r"\bactive\b"))
    # Settings → Plugins, and its own tab
    page.get_by_role("button", name="Settings").first.click()
    page.locator(".settings nav button", has_text="Plugins").click()
    row = page.locator(".plugin-row", has_text="Notes")
    expect(row).to_contain_text("loaded")
    expect(row).to_contain_text("tools: notes_add")
    shot(page, "08p-settings-plugins")
    # Its switch: off at once (its page's part goes at a reload), on again at once (Relay loaded it at its start)
    row.locator("label.switch").click()
    expect(row.locator(".badge")).to_have_text("off")
    expect(page.locator(".plugins-restart")).to_have_text(
        "Reload the page for the plugin's buttons and pages to follow.")
    row.locator("label.switch").click()
    expect(row.locator(".badge")).to_have_text("loaded")
    expect(page.locator(".plugins-restart")).to_be_hidden()
    page.locator(".settings nav button", has_text="Notes").click()
    expect(page.locator(".settings .pane")).to_contain_text("those older than 30 days go by themselves")
    page.keyboard.press("Escape")
    expect(page.locator(".settings")).to_have_count(0)
    view(page, "chats")
    page.locator(".conv .title", has_text="Greeting script").click()
    expect(page.locator(".header .title-btn")).to_contain_text("Greeting script")


def side_column(page, BASE):
    """The column (Relay 2.11): as wide as it's dragged, in every view and after a reload; folded away with its
    view's icon; Ctrl+K's switcher to any chat; the Shell, a panel under the chat."""
    view(page, "chats")
    page.locator(".conv .title", has_text="Update main.py and run").click()
    expect(page.locator(".header .title-btn")).to_contain_text("Update main.py and run")
    handle = page.locator(".panel-resize").bounding_box()
    before = page.locator(".side-panel").bounding_box()["width"]
    page.mouse.move(handle["x"] + 4, handle["y"] + 300)
    page.mouse.down()
    page.mouse.move(handle["x"] + 84, handle["y"] + 300, steps=6)
    page.mouse.up()
    wider = page.locator(".side-panel").bounding_box()["width"]
    assert abs(wider - before - 80) <= 2, (before, wider)
    page.reload()
    expect(page.locator(".header .title-btn")).to_contain_text("Update main.py and run")
    for other in ("scripts", "agentos", "chats"):
        view(page, other)
        assert abs(page.locator(".side-panel").bounding_box()["width"] - wider) <= 1, other
    page.locator(".panel-resize").dblclick()  # (as it was)
    expect(page.locator(".panel-resize")).to_have_attribute("aria-valuenow", "288")
    # Folded away with its view's icon: the page gets the room; kept after a reload; the icon brings it back
    work = page.locator(".work").bounding_box()["width"]
    page.locator(".activity-bar [data-view=chats]").click()
    expect(page.locator(".layout.sidebar-collapsed")).to_have_count(1)
    assert page.locator(".work").bounding_box()["width"] > work + 200
    shot(page, "08n-column-folded")
    page.reload()
    expect(page.locator(".layout.sidebar-collapsed")).to_have_count(1)
    page.locator(".activity-bar [data-view=chats]").click()
    expect(page.locator(".layout.sidebar-collapsed")).to_have_count(0)
    # Ctrl+K: titles as you type, then what was said in them (the server's search); the column stays as it is
    view(page, "scripts")
    page.keyboard.press("Control+k")
    switcher = page.locator(".switcher")
    expect(switcher.get_by_label("Find a chat")).to_be_focused()
    page.keyboard.type("todo")
    expect(switcher.locator(".switcher-item").first).to_contain_text("Todo app")
    shot(page, "08n-switcher")
    page.keyboard.press("Enter")
    expect(page.locator(".header .title-btn")).to_contain_text("Todo app")
    expect(page.locator(".panel-view.shown")).to_have_attribute("data-view", "scripts")
    page.keyboard.press("Control+k")
    page.keyboard.type("edited follow")
    found = switcher.locator(".switcher-item", has_text="Tell me about Relay")
    expect(found).to_contain_text("in its messages")
    page.keyboard.press("ArrowDown")
    page.keyboard.press("ArrowUp")
    page.keyboard.press("Enter")
    expect(page.locator(".header .title-btn")).to_contain_text("Tell me about Relay")
    page.keyboard.press("Control+k")
    page.keyboard.press("Escape")
    expect(switcher).to_have_count(0)
    # The Shell: a panel under the chat, on its WorkBox; it follows the chat that's open
    view(page, "chats")
    page.locator(".conv .title", has_text="Update main.py and run").click()
    expect(page.locator(".header .title-btn")).to_contain_text("Update main.py and run")
    page.locator(".activity-bar").get_by_role("button", name="Shell", exact=True).click()
    panel = page.locator(".bottom-panel")
    expect(panel.locator(".shell-status")).to_contain_text("Connected", timeout=120000)
    expect(panel.locator(".shell-directory")).to_contain_text("demo-app")  # (where that task works now)
    page.keyboard.type("ls")
    page.keyboard.press("Enter")
    expect(panel.locator(".xterm-rows")).to_contain_text("main.py")
    shot(page, "08o-shell-panel")
    page.goto(BASE + "/tasks")  # (a new page load: the panel closed)
    expect(panel).to_be_hidden()


def walkthrough(p, BASE, SHOTS, problems):
    browser = p.chromium.launch()
    context = browser.new_context(viewport={"width": 1280, "height": 820}, color_scheme="light")
    page = context.new_page()
    PAGES.append(page)
    page.on("console", lambda m: m.type in ("error", "warning") and problems.append(f"console {m.type}: {m.text}"))
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))

    page.goto(BASE)
    expect(page.get_by_text("Welcome to Relay")).to_be_visible()
    shot(page, "01-setup")
    page.get_by_placeholder("Username").fill("alex")
    page.get_by_placeholder("Password", exact=True).fill("relay-password")
    page.get_by_placeholder("Repeat password").fill("relay-password")
    page.get_by_role("button", name="Create account").click()
    expect(page.get_by_text("What can I help with?")).to_be_visible()

    # Chat with the fake OpenAI-compatible provider
    page.locator(".composer .picker").first.click()
    page.locator(".menu button", has_text="Fake Chat").click()
    # Effort: hovering a model shows its levels beside it
    page.locator(".composer .picker").nth(1).click()
    page.locator(".menu:not(.submenu) button").first.hover()
    expect(page.locator(".menu.submenu button", has_text="Default effort")).to_be_visible()
    shot(page, "02-effort-submenu")
    page.locator(".menu.submenu button").filter(has_text=re.compile(r"^high$")).click()
    expect(page.locator(".menu")).to_have_count(0)
    expect(page.locator(".composer .picker").nth(1)).to_contain_text("· high")
    shot(page, "02-new-chat")
    page.get_by_label("Message").fill("Tell me about Relay")
    page.get_by_label("Message").press("Enter")
    expect(page.locator(".msg.assistant .md table")).to_be_visible(timeout=15000)
    expect(page.locator(".msg.assistant .msg-actions .info")).to_contain_text("42 in", timeout=15000)
    # When each message was sent: the time today, in full on hover
    expect(page.locator(".msg.user .msg-time").first).to_have_text(re.compile(r"^\d{1,2}:\d{2}"))
    expect(page.locator(".msg.assistant .msg-time").first).to_have_attribute("title", re.compile(r"\d{4}"))
    expect(page.locator(".msg.assistant .msg-actions .cost.free").first).to_have_text("free")  # a local server
    assert "/c/" in page.url, page.url
    shot(page, "03-chat-answer")
    # Reply to a part of the answer: select it, and a Reply button quotes it
    page.evaluate("""() => {
      const cell = document.querySelector(".msg.assistant .md td");
      const range = document.createRange();
      range.selectNodeContents(cell);
      getSelection().removeAllRanges();
      getSelection().addRange(range);
    }""")
    expect(page.locator(".reply-float")).to_be_visible()
    shot(page, "03-reply-selection")
    page.locator(".reply-float").click()
    cell = page.locator(".msg.assistant .md td").first.inner_text().strip()
    expect(page.get_by_label("Message")).to_have_value(f"> {cell}\n\n")
    expect(page.locator(".reply-float")).to_be_hidden()
    # Attach a text file and an image to the follow-up
    page.locator(".composer input[type=file]").set_input_files([
        {"name": "notes.txt", "mimeType": "text/plain", "buffer": b"some notes"},
        {"name": "dot.png", "mimeType": "image/png", "buffer": TINY_PNG},
    ])
    expect(page.locator(".attachment-tray .attachment")).to_have_count(2)
    expect(page.locator(".attachment-tray")).to_contain_text("notes.txt")
    expect(page.locator(".attachment-tray .spinner")).to_have_count(0)
    page.get_by_label("Message").fill("And a follow-up?")
    shot(page, "03b-attachments")
    page.get_by_label("Message").press("Enter")
    expect(page.locator(".msg.user .sent-attachments a")).to_have_count(2)
    expect(page.locator(".attachment-tray .attachment")).to_have_count(0)
    # (It may still be on its way: give it a moment.)
    page.wait_for_function("() => [...document.querySelectorAll('.sent-image img')]"
                           ".every((img) => img.complete && img.naturalWidth > 0)", timeout=5000)
    expect(page.locator(".conv .title", has_text="(overview)")).to_be_visible(timeout=10000)  # model's title

    # Regenerate replaces the last answer; editing the last message resends it
    expect(page.locator(".msg.assistant")).to_have_count(2)
    page.get_by_role("button", name="Regenerate", exact=True).click()
    expect(page.locator(".msg.assistant").last).to_contain_text("I remember 3 messages", timeout=15000)
    expect(page.locator(".msg.assistant")).to_have_count(2)
    page.locator(".msg.user").last.get_by_title("Edit and send again").click()
    page.locator(".edit-area").fill("An edited follow-up")
    page.get_by_role("button", name="Save & send").click()
    expect(page.locator(".msg.user").last).to_contain_text("An edited follow-up", timeout=15000)
    expect(page.locator(".msg.assistant").last).to_contain_text("I remember 3 messages", timeout=15000)
    expect(page.locator(".msg.user")).to_have_count(2)
    expect(page.locator(".msg.user .sent-attachments a")).to_have_count(2)  # attachments kept

    # Export as Markdown
    item = page.locator(".conv", has_text="Tell me about Relay")
    item.hover()
    item.locator(".menu-btn").click()
    with page.expect_download() as download:
        page.locator(".menu button", has_text="Export as Markdown").click()
    exported = Path(download.value.path()).read_text()
    assert exported.startswith("# Tell me about Relay") and "An edited follow-up" in exported, exported[:200]
    expect(page.locator(".msg.assistant").nth(1)).to_contain_text("I remember 3 messages", timeout=15000)
    expect(page.locator(".conv .title", has_text="Tell me about Relay")).to_be_visible()

    # Fork from the first answer: a copy that goes its own way
    page.locator(".msg.assistant").first.hover()
    page.locator(".msg.assistant").first.get_by_role("button", name="More", exact=True).click()
    page.get_by_role("menuitem", name="Fork from here", exact=True).click()
    expect(page.locator(".header .title-btn")).to_contain_text("(fork)", timeout=10000)
    expect(page.locator(".thread-note")).to_contain_text("Forked from Tell me about Relay")
    expect(page.locator(".msg")).to_have_count(2)
    page.get_by_label("Message").fill("Something else")
    page.get_by_label("Message").press("Enter")
    expect(page.locator(".msg.assistant").last).to_contain_text("I remember 3 messages", timeout=15000)
    # Editing an earlier message rewinds the chat to it, after a warning
    page.locator(".msg.user").first.get_by_title("Edit and send again").click()
    expect(page.locator(".modal")).to_contain_text("The 3 messages after it will be removed")
    no_stray_text(page, "the Edit dialog")
    page.locator(".modal").get_by_role("button", name="Edit", exact=True).click()
    page.locator(".edit-area").fill("Tell me about forks")
    page.get_by_role("button", name="Save & send").click()
    expect(page.locator(".msg.user")).to_have_count(1, timeout=15000)
    expect(page.locator(".msg.user")).to_contain_text("Tell me about forks")
    expect(page.locator(".msg.assistant")).to_have_count(1, timeout=15000)
    expect(page.locator(".msg.assistant .md table")).to_be_visible(timeout=15000)
    # Another provider takes over, with the conversation handed over
    page.locator(".composer .picker").first.click()
    page.locator(".menu button", has_text="Fake Agent").click()
    page.get_by_label("Message").fill("Carry on")
    page.get_by_label("Message").press("Enter")
    expect(page.locator(".switch-note")).to_contain_text("Now with Fake Agent")
    page.locator(".switch-note summary").click()
    expect(page.locator(".switch-note")).to_contain_text("handed over")
    expect(page.locator(".composer .picker").first).to_contain_text("Fake Agent")
    expect(page.locator(".msg.assistant").last).to_contain_text("want me to fix it?", timeout=15000)
    # The agent's server link opens through Relay (relay.forward): this page's address, /port/<n>/
    site = page.locator(".msg.assistant").last.locator("a", has_text="/port/5173/")
    expect(site).to_have_attribute("href", BASE + "/port/5173/")
    shot(page, "03c-fork")
    served_page(page, BASE)
    page.reload()  # the note comes from what's stored too
    expect(page.locator(".switch-note")).to_contain_text("Now with Fake Agent")
    page.locator(".thread-note a").click()
    expect(page.locator(".msg.user")).to_have_count(2)  # the original is as it was
    fork_item = page.locator(".conv", has_text="(fork)")
    fork_item.hover()
    fork_item.locator(".menu-btn").click()
    page.locator(".menu button", has_text="Delete").click()
    page.locator(".modal").get_by_role("button", name="Delete", exact=True).click()
    expect(page.locator(".conv", has_text="(fork)")).to_have_count(0)

    # Code task with the fake agent: a new conversation in a project folder
    new_conversation(page)
    page.locator(".composer .picker").first.click()
    page.locator(".menu button", has_text="Fake Agent").click()
    expect(page.get_by_text("What can I help with?")).to_be_visible()
    page.locator(".picker", has_text="No project folder").click()
    page.locator(".dir-list button", has_text="demo-app").click()
    shot(page, "04-folder-picker")
    page.get_by_role("button", name="Use this folder").click()
    expect(page.get_by_text("What should we build?")).to_be_visible()
    expect(page.get_by_label("Message")).to_have_attribute("placeholder", "Describe a coding task…")
    page.get_by_label("Message").fill("Update main.py and run the tests")
    page.get_by_label("Message").press("Enter")
    expect(page.locator(".tool")).to_have_count(3, timeout=15000)
    expect(page.locator(".msg.assistant")).to_contain_text("want me to fix it?", timeout=15000)
    plan = page.locator(".msg.assistant .msg-actions .cost.plan").last
    expect(plan).to_have_text("in plan")
    assert "about $0.01" in plan.get_attribute("title"), plan.get_attribute("title")
    page.locator(".msg.assistant").last.hover()
    plan.scroll_into_view_if_needed()
    shot(page, "05d-usage")
    # Finished steps are bundled under Activity; the head says what a command is for.
    page.locator(".activity-head").last.click()
    page.locator(".tool-head", has_text="List files").click()
    expect(page.locator(".tool.open pre").first).to_contain_text("ls -la")
    shot(page, "05-code-task")

    # The folder isn't a git repository: Relay follows its changes with checkpoints.
    page.locator(".changes-btn").click()
    expect(page.locator(".changes-panel")).to_contain_text("not a git repository")
    expect(page.locator(".change-file")).to_have_count(1)
    expect(page.locator(".changes-panel")).to_contain_text("Rewind to the start")
    shot(page, "05e-changes-no-git")
    page.keyboard.press("Escape")
    # /rewind: pick the answer from its menu, and undo the rewind again
    demo_main = WORKSPACE[0] / "demo-app" / "main.py"
    assert demo_main.exists()
    page.get_by_label("Message").fill("/rew")
    expect(page.locator(".menu button", has_text="/rewind")).to_be_visible()
    page.locator(".menu button", has_text="/rewind").click()
    expect(page.get_by_label("Message")).to_have_value("/rewind ")
    choice = page.locator(".menu button", has_text="Last answer")
    expect(choice).to_contain_text("before “Update main.py and run the tests”")
    shot(page, "05ea-rewind-menu")
    choice.click()
    page.locator(".modal").get_by_role("button", name="Rewind").click()
    rewound = page.locator(".toast", has_text="Files rewound")
    expect(rewound).to_be_visible()
    assert not demo_main.exists(), "the file the agent made is still there"
    rewound.get_by_role("button", name="Undo").click()
    expect(page.locator(".toast", has_text="Files as they were again")).to_be_visible()
    assert demo_main.exists()
    # Typed in full: /rewind goes back one answer
    page.get_by_label("Message").fill("/rewind")
    page.get_by_label("Message").press("Enter")
    page.locator(".modal").get_by_role("button", name="Rewind").click()
    expect(page.locator(".toast", has_text="Files rewound").last).to_be_visible()
    assert not demo_main.exists()
    page.locator(".toast", has_text="Files rewound").last.get_by_role("button", name="Undo").click()
    # (The toast from the first undo may still be up: wait for the file itself.)
    for _ in range(50):
        if demo_main.exists():
            break
        page.wait_for_timeout(100)
    assert demo_main.exists()
    expect(page.get_by_label("Message")).to_have_value("")  # a command isn't sent as a message
    expect(page.locator(".msg.user")).to_have_count(1)

    # Modes: add one in the settings, switch to it with /mode, and back with /mode build
    page.get_by_role("button", name="Settings").first.click()
    page.locator(".settings nav button", has_text="Modes").click()
    expect(page.locator(".provider-row")).to_have_count(5)  # Build, Plan, Read-only, Fast and Brainstorm
    page.get_by_role("button", name="Add mode").click()
    page.get_by_placeholder("e.g. Critic").fill("Critic")
    page.locator(".settings select:not([aria-label='Settings category'])").select_option("read")
    page.locator(".settings textarea").fill("Find the weak spots.")
    page.locator(".settings").get_by_role("button", name="Add mode").click()
    expect(page.locator(".provider-row", has_text="Critic")).to_contain_text("read-only")
    # Its icon: a shield (read-only) until another is picked
    page.locator(".provider-row", has_text="Critic").locator(".mode-icon").click()
    expect(page.locator(".menu.icon-grid")).to_be_visible()
    shot(page, "05f-mode-icons")
    page.locator(".menu.icon-grid button[title=bug]").click()
    expect(page.locator(".provider-row", has_text="Critic").locator(".mode-icon svg rect")).to_have_count(1)
    shot(page, "05f-modes")
    page.keyboard.press("Escape")
    page.get_by_label("Message").fill("/mo")
    page.locator(".menu button", has_text="/mode — switch mode").click()
    expect(page.get_by_label("Message")).to_have_value("/mode ")
    expect(page.locator(".menu button", has_text="Plan · read-only")).to_be_visible()  # its choices
    shot(page, "05fa-mode-menu")
    page.locator(".menu button", has_text="Critic").click()
    expect(page.locator(".toast", has_text="Critic mode")).to_be_visible()
    expect(page.locator(".composer .picker.on")).to_contain_text("Critic")
    expect(page.get_by_label("Message")).to_have_value("")
    page.get_by_label("Message").fill("Is this any good?")
    page.get_by_label("Message").press("Enter")
    expect(page.locator(".switch-note").last).to_contain_text("Now in Critic mode")
    expect(page.locator(".msg.assistant").last).to_contain_text("want me to fix it?", timeout=15000)
    expect(page.locator(".menu")).to_have_count(0)  # the / menu closed after choosing
    expect(page.locator(".composer .send-btn.stop")).to_have_count(0, timeout=15000)  # the run is done
    page.get_by_label("Message").fill("/mode build")
    page.get_by_label("Message").press("Enter")
    expect(page.locator(".toast", has_text="Build mode")).to_be_visible()
    expect(page.get_by_label("Message")).to_have_value("")  # a command isn't sent
    page.get_by_label("Message").fill("/mode nonsense")
    page.get_by_label("Message").press("Enter")
    expect(page.locator(".toast.error", has_text="No mode “nonsense”")).to_be_visible()
    # Tab completes a command and its choice, like a shell
    page.get_by_label("Message").fill("/mod")
    page.get_by_label("Message").press("Tab")
    expect(page.get_by_label("Message")).to_have_value("/mode")  # as far as it's certain: /mode or /model
    page.get_by_label("Message").press_sequentially(" bu")
    page.get_by_label("Message").press("Tab")
    expect(page.get_by_label("Message")).to_have_value("/mode build")
    page.get_by_label("Message").fill("")
    page.keyboard.press("Escape")
    # Effort, thinking, help
    page.get_by_label("Message").fill("/effort high")
    page.get_by_label("Message").press("Enter")
    expect(page.locator(".toast.error", has_text="no effort levels")).to_be_visible()  # a command agent has none
    page.get_by_label("Message").fill("/thinking show")
    page.get_by_label("Message").press("Enter")
    expect(page.locator("details.reasoning").first).to_have_attribute("open", "")
    page.get_by_label("Message").fill("/thinking")  # toggles back
    page.get_by_label("Message").press("Enter")
    expect(page.locator("details.reasoning[open]")).to_have_count(0)
    page.get_by_label("Message").fill("/help")
    page.get_by_label("Message").press("Enter")
    expect(page.locator(".modal")).to_contain_text("/rewind")
    expect(page.locator(".modal")).to_contain_text("/effort")
    expect(page.locator(".modal")).to_contain_text("/code-review")
    expect(page.locator(".modal")).not_to_contain_text("null")
    shot(page, "05fb-help")
    page.keyboard.press("Escape")
    page.get_by_label("Message").fill("/code")  # skills are commands too
    expect(page.locator(".menu button", has_text="/code-review")).to_be_visible()
    page.get_by_label("Message").fill("")
    page.keyboard.press("Escape")
    page.get_by_label("Message").fill("carry on")
    page.get_by_label("Message").press("Enter")
    expect(page.locator(".switch-note").last).to_contain_text("Now in Build mode")
    expect(page.locator(".msg.user").last).to_contain_text("carry on")
    expect(page.locator(".msg.assistant").last).to_contain_text("want me to fix it?", timeout=15000)

    # A message typed while the agent works waits its turn
    expect(page.locator(".composer .send-btn.stop")).to_have_count(0, timeout=15000)
    page.get_by_label("Message").fill("First of two")
    page.get_by_label("Message").press("Enter")
    expect(page.locator(".composer .send-btn.stop")).to_be_visible()
    page.get_by_label("Message").fill("Second of two, " + "and a long one that waits its turn " * 4
                                      + "with/a/path/that/has/no/spaces/to/break/at/at/all/in/it")
    expect(page.locator(".composer .send-btn.queue")).to_be_visible()
    page.get_by_label("Message").press("Enter")
    expect(page.locator(".queued-item")).to_contain_text("Second of two")
    shot(page, "05i-queue")
    page.set_viewport_size({"width": 390, "height": 800})  # (on a phone, cut short: the page never wider)
    if page.locator(".queued-item").count():
        assert page.evaluate("document.documentElement.scrollWidth") <= 390, "a long queued message widens the page"
    page.set_viewport_size({"width": 1280, "height": 820})
    expect(page.locator(".msg.user").last).to_contain_text("Second of two", timeout=20000)
    expect(page.locator(".queued-item")).to_have_count(0)
    expect(page.locator(".msg.assistant").last).to_contain_text("want me to fix it?", timeout=15000)
    expect(page.locator(".composer .send-btn.stop")).to_have_count(0, timeout=15000)

    # A code task in a git repository works in the folder, a commit per turn
    new_conversation(page)
    page.get_by_label("Message").fill("Update main.py")  # typed before picking a folder
    page.locator(".picker", has_text="No project folder").click()  # (a new chat starts without one)
    expect(page.locator(".modal")).to_contain_text("demo-app")  # the picker opens at the folder picked last
    page.get_by_role("button", name="Up", exact=True).click()
    page.locator(".dir-list button", has_text="git-app").click()
    page.get_by_role("button", name="Use this folder").click()
    expect(page.locator(".composer .picker", has_text="Work in place")).to_have_count(0)  # no toggle any more
    expect(page.get_by_label("Message")).to_have_value("Update main.py")  # kept
    page.get_by_label("Message").press("Enter")
    expect(page.locator(".turn-changes")).to_contain_text("Review changes · 1 file", timeout=15000)
    expect(page.locator(".composer-note")).to_contain_text("git-app")

    # The tasks overview lists it under "Needs review"; Review opens its changes
    # (the Tasks view lists it too, under its group; a row opens its chat)
    view(page, "tasks")
    expect(page.locator(".tasks-list .panel-group.review .panel-item", has_text="Update main.py")).to_be_visible()
    shot(page, "05-tasks-view")
    tasks_page(page)
    review = page.locator(".task-group.review .task-card", has_text="Update main.py")
    expect(review).to_be_visible()
    expect(review).to_contain_text("git-app")
    expect(review.locator(".task-changes")).to_contain_text("+1")
    shot(page, "05a-tasks")
    review.get_by_role("button", name="Review changes", exact=True).click()
    expect(page.locator(".changes-panel")).to_be_visible()
    expect(page.locator(".change-file")).to_have_count(1)
    page.locator(".change-head", has_text="main.py").click()
    expect(page.locator(".diff .dl.add")).to_contain_text("print(2)")
    expect(page.locator(".diff .dl.del")).to_have_count(2)
    expect(page.locator(".diff .dl.del").nth(0)).to_contain_text("print(1)")
    expect(page.locator(".diff .dl.del").nth(1)).to_have_text("--- note")
    shot(page, "05b-changes-panel")
    expect(page.get_by_role("button", name="Merge into main")).to_have_count(0)  # it's on main already
    page.get_by_role("button", name="Mark as reviewed").click()
    expect(page.locator(".toast", has_text="Marked as reviewed")).to_be_visible(timeout=10000)
    expect(page.locator(".changes-panel")).to_have_count(0)  # (its work done: the panel closes)
    page.locator(".header .changes-btn").click()  # (opened again: nothing left to review)
    expect(page.locator(".changes-empty")).to_contain_text("No changes to review", timeout=10000)
    expect(page.get_by_role("button", name="Mark as reviewed")).to_be_disabled()
    page.keyboard.press("Escape")
    tasks_page(page)
    expect(page.locator(".task-group.review .task-card", has_text="Update main.py")).to_have_count(0)
    MERGED.append(True)

    # A run that finishes in the background is announced
    view(page, "chats")
    page.locator(".conv .title", has_text="Update main.py and run").click()
    # (Wait for it to open, or the message goes to the chat still on screen.)
    expect(page.locator(".header .title-btn")).to_contain_text("Update main.py and run")
    page.get_by_label("Message").fill("once more")
    page.get_by_label("Message").press("Enter")
    new_conversation(page)
    # (An earlier run of this chat may still have its toast up; the unread
    # marker only appears once this run is done.)
    expect(page.locator(".conv", has_text="Update main.py and run").locator(".unread")).to_be_visible(timeout=30000)
    toast = page.locator(".toast", has_text="Finished: Update main.py and run").last
    expect(toast).to_be_visible()
    shot(page, "05c-finished-toast")
    toast.get_by_role("button", name="Open").click()
    expect(page.locator(".header .title-btn")).to_contain_text("Update main.py and run")
    expect(page.locator(".conv.active .unread")).to_have_count(0)

    # Reload: the stored conversation renders the same
    page.reload()
    expect(page.locator(".tool")).to_have_count(18)  # six turns of three tool calls
    expect(page.locator(".msg.assistant").last).to_contain_text("want me to fix it?")
    # /clear: the agent starts afresh; the conversation stays on screen, with a line where it was cleared
    page.get_by_label("Message").fill("/clear")
    page.get_by_label("Message").press("Enter")
    expect(page.locator(".thread > .clear-note")).to_have_text("Cleared: the agent starts afresh from here")
    expect(page.locator(".tool")).to_have_count(18)
    page.reload()
    expect(page.locator(".thread > .clear-note")).to_have_count(1)
    expect(page.locator(".thread > :last-child")).to_have_class(re.compile("clear-note"))

    # Subagents: an agent hands work to another provider, shown as a card
    new_conversation(page)
    page.locator(".composer .picker").first.click()
    page.locator(".menu button", has_text="Fake Orchestrator").click()
    if page.locator(".composer .picker", has_text="git-app").count():  # an agent: a chat without the folder
        page.locator(".composer .picker", has_text="git-app").click()
        page.locator(".menu button", has_text="No project folder").click()
        expect(page.get_by_text("What can I help with?")).to_be_visible()
    page.get_by_label("Message").fill("What is Relay, briefly?")
    page.get_by_label("Message").press("Enter")
    card = page.locator(".tool.subagent")
    expect(card).to_contain_text("Chat helper", timeout=15000)
    expect(card).to_contain_text("Fake Chat")
    expect(page.locator(".msg.assistant").last).to_contain_text("it answered", timeout=15000)
    expect(card.locator(".state.ok")).to_be_visible(timeout=15000)
    expect(card.locator(".cost.free")).to_be_visible()  # the subagent's own cost label
    # Starting and waiting show as the card; only `relay agents` stays a tool call
    expect(page.locator(".tool:not(.subagent)")).to_have_count(1)
    expect(page.locator(".tool:not(.subagent) .summary")).to_have_text("relay agents")  # not the long path
    card.locator(".tool-head").click()
    expect(card.locator(".subagent-thread .md table")).to_be_visible(timeout=15000)  # its own answer
    shot(page, "05g-subagent")
    card.get_by_role("link", name="Open as a chat").click()
    expect(page.locator(".thread-note")).to_contain_text("Subagent of")
    expect(page.locator(".conv", has_text="Chat helper")).to_have_count(0)  # not in the sidebar
    page.locator(".thread-note a").click()
    expect(page.locator(".tool.subagent")).to_be_visible()

    # Dark theme and settings
    page.locator(".activity-bar .bar-user").click()
    page.locator(".menu button", has_text="Dark theme").click()
    page.goto(BASE + "/")
    page.locator(".conv .title", has_text="Tell me about Relay").click()
    expect(page.locator(".msg.assistant .md table")).to_be_visible()
    assert page.evaluate("document.scrollingElement.scrollTop") == 0, "the whole page scrolled"
    shot(page, "06-dark-chat")
    page.get_by_role("button", name="Settings").first.click()
    page.locator(".settings nav button", has_text="Providers").click()
    expect(page.locator(".provider-row:not(.suggestion)")).to_have_count(7)
    expect(page.locator(".provider-row.suggestion")).to_contain_text("opencode · default")
    expect(page.locator(".provider-row",
                        has_text="Fake Limited").locator(".badge.managed")).to_have_text("added by AgentOS")
    shot(page, "07-settings-providers")
    assert "sk-test-123" not in page.content(), "API key leaked into the page"
    # Which models work: the one used in the chat, and a test of another
    page.locator(".provider-row", has_text="Fake Chat").get_by_role("button", name="Models").click()
    expect(page.locator(".model-row", has_text="Default model")).to_contain_text("Worked · used")
    expect(page.locator(".model-row", has_text="fake-small")).to_contain_text("Not used or tested yet")
    page.locator(".model-row", has_text="fake-small").get_by_role("button", name="Test").click()
    expect(page.locator(".model-row", has_text="fake-small")).to_contain_text("Worked · tested", timeout=15000)
    expect(page.locator(".model-row", has_text="fake-small").locator(".status-dot.ok")).to_have_count(1)
    shot(page, "07-settings-models")
    page.get_by_role("button", name="Back").click()
    # Push notifications: a test reaches the configured ntfy URL
    page.locator(".settings nav button", has_text="Notifications").click()
    page.get_by_placeholder("https://ntfy.sh/your-secret-topic").fill(CATCHER["url"] + "/topic")
    page.get_by_role("button", name="Send test").click()
    expect(page.locator(".check-result.ok")).to_contain_text("Test sent", timeout=10000)
    assert any(h.get("Title") == "Done: Test notification" for h in CATCHER["received"]), CATCHER["received"]
    # What to notify of, per category: AgentOS's runs not pushed at first; pushed at a priority once saved;
    # this browser's own choice kept at once, only where the browser hears of it
    agents = page.locator(".notify-row", has_text="AgentOS: done")
    expect(agents.get_by_label("Push: AgentOS: done")).not_to_be_checked()
    expect(agents.get_by_label("Priority: AgentOS: done")).to_be_disabled()
    agents.get_by_label("Push: AgentOS: done").check()
    agents.get_by_label("Priority: AgentOS: done").select_option("min")
    page.locator(".notification-actions").get_by_role("button", name="Save", exact=True).click()
    expect(page.get_by_text("Notifications saved")).to_be_visible()
    listed = page.request.get(BASE + "/api/settings").json()["notifications"]["categories"]
    chosen = next(c for c in listed if c["id"] == "agents.done")
    assert (chosen["push"], chosen["priority"]) == (True, "min"), chosen
    here = page.get_by_label("This browser: AgentOS: failed")
    expect(here).to_be_checked()
    here.uncheck()
    assert page.evaluate("JSON.parse(localStorage.getItem('relay.notifyCategories'))['agents.failed']") is False
    here.check()
    expect(page.get_by_label("This browser: VPN tunnel problems")).to_have_count(0)
    shot(page, "07b-notifications")
    # Memory, a bank as Perplexity's: each memory in a category; added, found (search, a category's chip),
    # changed, forgotten (and back as it was), all deleted
    page.locator(".settings nav button", has_text="Memory").click()
    expect(page.get_by_label("Agents use Relay's memory")).to_be_checked()
    expect(page.get_by_label("Agents save what they learn")).to_be_checked()
    for text, category in (("I'd like answers in Dutch.", "Preferences"), ("I work with TypeScript.", "Tools")):
        page.get_by_label("Something to remember").fill(text)
        page.locator(".memory-add").get_by_label("Category").select_option(label=category)
        page.get_by_role("button", name="Remember", exact=True).click()
        expect(page.locator(".memory-row", has_text=text)).to_contain_text(category)
    expect(page.locator(".memory-row", has_text="answers in Dutch")).to_contain_text("Added by you")
    page.locator(".memory-chip", has_text="Tools").click()
    expect(page.locator(".memory-row")).to_have_count(1)
    expect(page.locator(".memory-row")).to_contain_text("TypeScript")
    page.locator(".memory-chip", has_text="All").click()
    page.get_by_label("Search memories").fill("dutch")
    expect(page.locator(".memory-row")).to_have_count(1)
    page.get_by_label("Search memories").fill("")
    expect(page.locator(".memory-row")).to_have_count(2)
    page.get_by_role("button", name="Change: I'd like answers in Dutch.").click()
    page.get_by_label("The memory").fill("I'd like answers in Dutch, code in English.")
    page.locator(".memory-edit").get_by_label("Category").select_option(label="About you")
    page.locator(".memory-edit").get_by_role("button", name="Save", exact=True).click()
    changed = page.locator(".memory-row", has_text="code in English")
    expect(changed).to_have_count(1)
    expect(changed.locator(".memory-category")).to_have_text("About you")
    shot(page, "07i-memory")
    changed.get_by_role("button", name=re.compile("^Forget")).click()
    expect(page.locator(".memory-row")).to_have_count(1)
    page.locator(".toast").get_by_role("button", name="Undo").click()
    expect(page.locator(".memory-row",
                        has_text="code in English").locator(".memory-category")).to_have_text("About you")
    page.get_by_role("button", name="Delete all memories").click()
    page.locator(".modal", has_text="Delete all memories?").get_by_role("button", name="Delete all").click()
    expect(page.locator(".memory-row")).to_have_count(0)
    expect(page.get_by_text("Nothing remembered yet.")).to_be_visible()
    # Relay's web search and fetch: on this server's own connection (no search or fetch here: it would
    # start their boxes)
    page.locator(".settings nav button", has_text="General").click()
    web = page.locator(".settings-section", has_text="Web search and fetch")
    expect(web.get_by_label("Give agents Relay's web search")).to_be_checked()
    expect(web.get_by_label("Route")).to_have_value("")
    expect(web).to_contain_text("Stopped: each starts with the next search or fetch")
    web.evaluate("e => e.scrollIntoView({block: 'start'})")
    shot(page, "07b-web-search")
    # What Relay runs on this server and what it takes (its boxes; podman's images)
    page.locator(".settings nav button", has_text="System").click()
    system = page.locator(".settings .pane")
    expect(system.locator(".system-card", has=page.get_by_text("Memory", exact=True))).to_contain_text("in use")
    expect(system.locator(".system-card", has=page.get_by_text("Processor", exact=True))).to_contain_text("core")
    expect(system).to_contain_text("Relay's boxes")
    expect(system.locator(".settings-section",
                          has_text="Storage").locator(".provider-row").first).to_be_visible(timeout=15000)
    shot(page, "07b-system")
    # App passwords (Account): made and shown once, used by a script (no cookie, no CSRF header), revoked
    page.locator(".settings nav button", has_text="Account").click()
    apps = page.locator(".settings-section", has_text="App passwords")
    # (the Usage limits plugin's own, made when it first asked Relay what's left)
    expect(apps.locator(".app-password-row")).to_have_text(re.compile(r"^plugin usage_limits"))
    apps.get_by_label("What will use it").fill("backup script")
    apps.get_by_role("button", name="Create", exact=True).click()
    secret = apps.locator(".app-password-made code").first.inner_text()
    assert secret.startswith("relay_"), secret
    backup = apps.locator(".app-password-row", has_text="backup script")
    expect(backup).to_be_visible()
    shot(page, "07b-app-passwords")
    script = p.request.new_context()  # (none of the browser's cookies)
    bearer = {"Authorization": f"Bearer {secret}"}
    assert script.get(BASE + "/api/conversations", headers=bearer).ok
    backup.get_by_title("Revoke").click()
    page.locator(".modal", has_text="Revoke app password?").get_by_role("button", name="Revoke").click()
    expect(backup).to_have_count(0)
    expect(apps.locator(".app-password-row")).to_have_count(1)
    assert script.get(BASE + "/api/conversations", headers=bearer).status == 401
    script.dispose()
    # VPNs for WorkBoxes: a profile (its fields from gluetun's server list: podman), a tunnel with two
    # exits, rotation and a fallback loop; without folders, so no gluetun starts
    page.locator(".settings nav button", has_text="Workspaces").click()
    vpn = page.locator(".settings-section", has_text="Tunnels")
    expect(vpn).to_contain_text("Add a profile first")
    expect(vpn.get_by_role("button", name="Add tunnel")).to_be_disabled()
    vpn.get_by_role("button", name="Add profile").click()
    dialog = page.locator(".modal", has_text="New VPN profile")
    provider = dialog.get_by_label("VPN provider")
    # (gluetun's list when the dialog opens, from podman: slow when busy, and the first time it downloads gluetun)
    expect(provider.locator("option", has_text="Windscribe")).to_have_count(1, timeout=120000)
    provider.select_option(label="Windscribe")
    expect(dialog.get_by_label("Name", exact=True)).to_have_value("Windscribe")
    dialog.get_by_label("Protocol").select_option(label="OpenVPN")
    dialog.get_by_label("Username").fill("me")
    expect(dialog.get_by_label("Password")).to_have_attribute("type", "password")
    dialog.get_by_label("Password").fill("pw")
    dialog.locator("summary", has_text="More settings").click()
    dialog.locator("textarea").fill("FIREWALL_VPN_INPUT_PORTS 8080")
    dialog.get_by_role("button", name="Save").click()
    expect(page.locator(".toast", has_text="More settings, line 1: NAME=value")).to_be_visible()
    dialog.locator("textarea").fill("")
    dialog.get_by_role("button", name="Save").click()
    expect(vpn.locator(".provider-row", has_text="Windscribe · OpenVPN")).to_be_visible()
    vpn.get_by_role("button", name="Add tunnel").click()
    dialog = page.locator(".modal", has_text="New VPN tunnel")
    dialog.get_by_label("Name", exact=True).fill("Private")
    dialog.get_by_label("Exit 1: region").select_option("Netherlands")
    dialog.get_by_label("Exit 1: city").select_option("Amsterdam")
    dialog.get_by_role("button", name="Add exit").click()
    dialog.get_by_label("Exit 2: region").select_option("Germany")
    dialog.get_by_label("Rotate every (minutes)").fill("30")
    dialog.get_by_label("Rotate to").select_option("next")
    dialog.get_by_label("Fall back after (seconds)").fill("45")
    shot(page, "07c-workspaces-vpn-tunnel")
    dialog.get_by_role("button", name="Save").click()
    row = vpn.locator(".provider-row", has_text="Private")
    expect(row).to_contain_text("No WorkBoxes yet")
    expect(row).to_contain_text("2 exits · rotates every 30 min (next exit) · falls back after 45 s")
    expect(row.get_by_role("button", name="Reconnect")).to_be_disabled()  # (no WorkBoxes: nothing to connect for)
    row.evaluate("e => e.scrollIntoView({block: 'start'})")
    shot(page, "07c-workspaces-vpn")
    row.get_by_role("button", name="Edit").click()
    dialog = page.locator(".modal", has_text="Tunnel: Private")
    expect(dialog.get_by_label("Exit 1: city")).to_have_value("Amsterdam")
    dialog.get_by_role("button", name="Delete").click()
    page.locator(".modal", has_text="Delete this tunnel?").get_by_role("button", name="Delete").click()
    expect(vpn.locator(".provider-row", has_text="Private")).to_have_count(0)
    vpn.locator(".provider-row", has_text="Windscribe · OpenVPN").get_by_title("Delete").click()
    page.locator(".modal", has_text="Delete this profile?").get_by_role("button", name="Delete").click()
    expect(vpn).to_contain_text("Add a profile first")
    page.locator(".settings nav").get_by_role("button", name="Usage", exact=True).click()
    expect(page.locator(".usage-card.plan")).to_contain_text("runs")  # the fake agent says "in plan"
    expect(page.locator(".usage-table tbody tr").first).to_be_visible()
    # What's left of the plans' limits: asked from the providers (here the machine's own logins, or none)
    limits = page.locator(".usage-limits")
    expect(limits).not_to_contain_text("Asking the providers", timeout=30000)
    expect(page.locator(".limits-section h4")).to_have_text("Usage limits")
    shot(page, "07c-usage")
    # Skills: the built-in ones, and one of your own
    page.locator(".settings nav button", has_text="Skills").click()
    expect(page.locator(".provider-row", has_text="relay-subagents")).to_contain_text("built in")
    expect(page.locator(".skill-folder code")).to_contain_text("skills")
    page.get_by_role("button", name="New skill").click()
    page.get_by_placeholder("e.g. release-notes").fill("greet")
    page.get_by_placeholder("What it does, and when to use it").fill("Greets warmly. Use when asked to greet.")
    page.locator(".settings").get_by_role("button", name="Add skill").click()
    expect(page.locator(".settings textarea")).to_have_value(re.compile("name: greet"))
    shot(page, "07d-skill-editor")
    page.locator(".settings").get_by_role("button", name="Back").click()
    row = page.locator(".provider-row", has_text="greet")
    expect(row).to_contain_text("yours")
    row.get_by_title("Delete").click()
    page.locator(".modal").last.get_by_role("button", name="Delete", exact=True).click()
    expect(page.locator(".provider-row", has_text="greet")).to_have_count(0)

    # Limits: change one, it's saved
    page.locator(".settings nav").get_by_role("button", name="Limits", exact=True).click()
    field = page.locator(".settings .field", has_text=re.compile(r"^At once")).locator("input")
    expect(field).to_have_value("4")
    field.fill("2")
    page.locator(".settings").get_by_role("button", name="Save").click()
    expect(page.locator(".toast", has_text="Limits saved")).to_be_visible()
    assert page.request.get(BASE + "/api/limits").json()["tunables"][0]["value"] == 2
    expect(page.locator(".settings").get_by_role("button", name="Restart Relay")).to_be_visible()
    shot(page, "07g-limits")
    page.locator(".settings .field", has_text=re.compile(r"^At once")).get_by_role("button", name="Default").click()
    page.locator(".settings").get_by_role("button", name="Save").click()
    # Your own computer: add it, run its connector, see it online, remove it
    page.locator(".settings nav button", has_text="Computers").click()
    page.get_by_placeholder("e.g. laptop").fill("Laptop")
    page.get_by_role("button", name="Add computer").click()
    expect(page.locator(".computer-row", has_text="laptop")).to_contain_text("offline")
    expect(page.locator(".settings")).to_contain_text("python3 relay-computer.py")
    computer_id = page.request.get(BASE + "/api/computers").json()[0]["id"]
    connector_file = Path(SHOTS) / "relay-computer.py"
    connector_file.write_bytes(page.request.get(f"{BASE}/api/computers/{computer_id}/script").body())
    connector = subprocess.Popen([sys.executable, str(connector_file)], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
    try:
        expect(page.locator(".computer-row", has_text="laptop")).to_contain_text("online", timeout=15000)
        shot(page, "07f-computers")
    finally:
        connector.terminate()
    page.locator(".computer-row", has_text="laptop").get_by_title("Remove").click()
    page.locator(".modal").last.get_by_role("button", name="Remove", exact=True).click()
    expect(page.locator(".computer-row")).to_have_count(0)
    # Subagents allowed without asking
    page.locator(".settings nav button", has_text="Subagents").click()
    expect(page.get_by_label(re.compile("own provider and model"))).to_be_checked()
    page.locator(".settings .row-inline select").first.select_option("fake-agent")
    page.locator(".settings .row-inline").get_by_role("button", name="Allow").click()
    expect(page.locator(".provider-row", has_text="Fake Agent")).to_contain_text("Any model")
    shot(page, "07e-subagents")
    page.locator(".provider-row", has_text="Fake Agent").get_by_title("Remove").click()
    expect(page.locator(".provider-row", has_text="Fake Agent")).to_have_count(0)
    page.locator(".settings nav button", has_text="Providers").click()
    # Check it fully: the provider run as Relay runs it, scenario by scenario (the fake agent prints the same
    # answer whatever it's asked: its command isn't the one asked for)
    page.locator(".provider-row", has_text="Fake Agent").get_by_role("button", name="Check it fully").click()
    checking = page.locator(".modal", has_text="Check Fake Agent fully")
    checking.get_by_role("button", name="Check", exact=True).click()
    expect(checking.locator(".conformance-row")).to_have_count(7, timeout=120000)
    expect(checking.locator(".conformance-row.ok", has_text="A turn")).to_be_visible()
    shot(page, "08-provider-check")
    checking.locator(".modal-foot").get_by_role("button", name="Close").click()
    # Deleting a provider takes a click on Delete: Enter is Cancel
    page.locator(".provider-row", has_text="Fake Agent").get_by_title("Delete").click()
    deleting = page.locator(".modal", has_text="Delete provider?")
    expect(deleting.get_by_role("button", name="Cancel")).to_be_focused()
    page.keyboard.press("Enter")
    expect(deleting).to_have_count(0)
    expect(page.locator(".provider-row", has_text="Fake Agent")).to_be_visible()
    page.get_by_role("button", name="Add provider").click()
    expect(page.locator(".kind-card", has_text="Example: toy-agent")).to_have_count(0)  # (the guide's: not offered)
    # A provider Relay doesn't know: the guide, in Settings
    page.locator(".kind-card.guide-card").click()
    guide = page.locator(".provider-guide")
    expect(guide.locator("h2", has_text="First: maybe a kind fits already")).to_be_visible()
    expect(guide.locator("pre code", has_text="class ToyAgentProvider(Provider):")).to_be_visible()
    shot(page, "08-provider-guide")
    page.get_by_role("button", name="Back").click()
    page.locator(".kind-card", has_text="OpenAI-compatible").click()
    shot(page, "08-add-provider")
    # Its next step: a taken id said on this page, then a test message, and only then it's added
    template = page.locator(".settings textarea")
    chat = next(p for p in page.request.get(f"{BASE}/api/providers").json()["providers"] if p["id"] == "fake-chat")
    second = {"id": "fake-chat", "name": "Second Chat", "kind": "openai-compatible", "enabled": True,
              "options": {"base_url": chat["options"]["base_url"], "api_key": "sk-second",
                          "models": [{"id": "fake-small", "name": "Small"}]}}
    template.fill(json.dumps(second))
    noted = len(problems)
    page.get_by_role("button", name="Next step").click()
    expect(page.locator(".settings .error-text")).to_contain_text("already exists")
    page.wait_for_timeout(300)
    del problems[noted:]  # (the browser's own note of that refusal)
    template.fill(json.dumps({**second, "id": "second-chat"}))
    page.get_by_role("button", name="Next step").click()
    expect(page.locator(".signin-check")).to_contain_text("It works", timeout=30000)
    expect(page.locator(".settings .shell-terminal")).to_have_count(0)  # (no box: only the test)
    expect(page.locator(".provider-row", has_text="Second Chat")).to_have_count(0)  # (it isn't added yet)
    shot(page, "08-next-step")
    page.get_by_role("button", name="Back").click()
    expect(template).to_have_value(re.compile("second-chat"))
    page.get_by_role("button", name="Next step").click()
    expect(page.locator(".signin-check")).to_contain_text("It works", timeout=30000)
    page.get_by_role("button", name="Add provider").click()
    added = page.locator(".provider-row", has_text="Second Chat")
    expect(added).to_be_visible()
    added.get_by_title("Delete").click()
    page.locator(".modal", has_text="Delete provider?").get_by_role("button", name="Delete", exact=True).click()
    expect(added).to_have_count(0)
    page.get_by_role("button", name="Add provider").click()
    page.locator(".kind-card.guide-card").click()
    add_with_an_agent(page)
    repair_on_its_page(page)

    # A chat gets a project folder later, from the picker under its message box
    new_conversation(page)
    page.locator(".composer .picker").first.click()
    page.locator(".menu button", has_text="Fake Chat").click()
    page.get_by_label("Message").fill("Sketch a todo app")
    page.get_by_label("Message").press("Enter")
    expect(page.locator(".msg.assistant").last).to_contain_text("Anything else", timeout=15000)
    expect(page.locator(".composer .send-btn.stop")).to_have_count(0, timeout=15000)
    page.locator(".composer .picker", has_text="No project folder").click()
    page.get_by_role("button", name="Use this folder").click()  # (it opens in the folder picked last)
    expect(page.locator(".composer-note")).to_contain_text("git-app")
    expect(page.locator(".conv.active .mode")).to_have_count(1)  # the code icon in the sidebar
    expect(page.locator(".composer .picker", has_text="No project folder")).to_have_count(0)
    page.get_by_label("Message").fill("/rename Todo app")  # (the fake names every chat the same)
    page.get_by_label("Message").press("Enter")
    expect(page.locator(".conv.active .title")).to_have_text("Todo app")
    # /clear delete-history: the messages go, for the user too; the chat stays, and starts over
    page.get_by_label("Message").fill("/clear delete-history")
    page.get_by_label("Message").press("Enter")
    expect(page.locator(".toast", has_text="History deleted")).to_be_visible()
    expect(page.locator(".thread > .msg")).to_have_count(0)
    expect(page.locator(".conv.active .title")).to_have_text("Todo app")

    # A chat moves to a code task (/code), in the folder picked last
    new_conversation(page)
    page.locator(".composer .picker").first.click()
    page.locator(".menu button", has_text="Fake Chat").click()
    expect(page.locator(".composer .picker", has_text="No project folder")).to_be_visible()  # a chat
    page.get_by_label("Message").fill("Plan a greeting script")
    page.get_by_label("Message").press("Enter")
    expect(page.locator(".msg.assistant").last).to_contain_text("Anything else", timeout=15000)
    expect(page.locator(".composer .send-btn.stop")).to_have_count(0, timeout=15000)
    page.get_by_label("Message").fill("/code")
    page.get_by_label("Message").press("Enter")
    move = page.locator(".modal", has_text="Move to a code task")
    expect(move.locator(".picker", has_text="git-app")).to_be_visible()
    shot(page, "08b-move-to-code")
    move.get_by_role("button", name="Move").click()
    expect(page.locator(".composer-note")).to_contain_text("git-app", timeout=15000)  # (a checkpoint: slow when busy)
    expect(page.locator(".conv.active .mode")).to_have_count(1)  # the code icon in the sidebar
    page.get_by_label("Message").fill("/rename Greeting script")
    page.get_by_label("Message").press("Enter")
    expect(page.locator(".conv.active .title")).to_have_text("Greeting script")
    # Open files: its folder on this computer, by an address that is the key
    page.locator(".composer .picker", has_text="Open files").click()
    expect(page.locator(".menu button", has_text="Open in the file manager")).to_be_visible()  # Linux: dav://
    shot(page, "08c-open-files")
    page.locator(".menu button", has_text="Copy the address").click()
    expect(page.locator(".toast", has_text="Paste it in the file manager's address bar")).to_be_visible()
    # Its folder as a network drive, with more ways and options
    page.get_by_label("Message").fill("/mount")
    page.get_by_label("Message").press("Enter")
    drive = page.locator(".modal", has_text="Open as a network drive")
    address = drive.locator(".skill-folder code").inner_text()
    assert address.startswith(BASE + "/dav/"), address
    listing = page.request.fetch(address, method="PROPFIND", headers={"Depth": "1"})
    assert listing.status == 207 and "main.py" in listing.text(), listing.status
    shot(page, "08c-network-drive")
    drive.get_by_label("Read-only").check()
    expect(drive.get_by_label("Read-only")).to_be_checked()
    assert page.request.fetch(address + "x.txt", method="PUT", data="x").status == 403
    page.keyboard.press("Escape")
    page.locator(".composer .picker", has_text="Open files").click()  # shares again: still read-only
    # (its menu opens once the share is back: an Escape before that left it to open later and take the Enter below)
    expect(page.locator(".menu button", has_text="Copy the address")).to_be_visible()
    page.keyboard.press("Escape")
    assert page.request.fetch(address + "x.txt", method="PUT", data="x").status == 403
    page.get_by_label("Message").fill("/mount")
    page.get_by_label("Message").press("Enter")
    drive.get_by_role("button", name="Stop sharing").click()
    assert page.request.fetch(address, method="PROPFIND").status == 404

    # AgentOS's tasks: this chat's prompt again later (/schedule), a weekly one of its own, a Governor's
    page.get_by_label("Message").fill("/schedule check the build")
    page.get_by_label("Message").press("Enter")
    expect(page.locator("h1", has_text="New task")).to_be_visible()
    expect(page.locator(".schedules-view textarea")).to_have_value("check the build")
    expect(page.locator(".schedules-view")).to_contain_text("on in the chat it was made from")
    page.get_by_role("button", name="Schedule", exact=True).click()
    expect(page.locator(".schedule-row", has_text="Greeting script")).to_contain_text("Next:")
    page.locator(".schedules-view").get_by_role("button", name="New task").click()
    expect(page.locator(".mode-toggle button.on")).to_have_text("The Governor decides")  # by default
    page.get_by_role("button", name="An agent you choose").click()
    page.get_by_placeholder("e.g. Morning summary").fill("Morning summary")
    page.get_by_placeholder("What should be done each time", exact=False).fill("Summarise yesterday")
    page.locator(".schedules-view .row-inline select").first.select_option("fake-chat")  # not a real agent
    page.locator(".schedules-view .row-inline select").last.select_option("weekly")
    page.locator(".day-picks .day", has_text="Wed").locator("input").check()
    shot(page, "08e-schedule-form")
    page.get_by_role("button", name="Schedule", exact=True).click()
    row = page.locator(".schedule-row", has_text="Morning summary")
    expect(row).to_contain_text("Mon, Wed at 08:00")
    row.get_by_role("button", name="Run now").click()
    expect(page.locator(".msg.user").first).to_contain_text("Summarise yesterday", timeout=15000)
    expect(page.locator(".header .title-btn")).to_contain_text("Morning summary ·")
    # The AgentOS view: its overview (the dashboard), its tasks by kind; a task opens its form there
    view(page, "agentos")
    expect(page.locator(".header .title-btn")).to_contain_text("Morning summary ·")  # (the chat stays)
    task = page.locator(".agentos-list .panel-group.manual .panel-item", has_text="Morning summary")
    task.click()
    expect(page).to_have_url(re.compile(r"/agentos\?task=[0-9a-f]+$"))
    expect(page.locator("h1", has_text="Edit Morning summary")).to_be_visible()
    expect(task).to_have_class(re.compile(r"\bactive\b"))
    shot(page, "08f-agentos-view")
    page.locator(".agentos-list .panel-item", has_text="Overview").click()
    expect(page.locator(".governor-row")).to_contain_text("Governor")
    # The Governor view: its conversation first; not woken yet, so it says so and stays here
    view(page, "governor")
    page.locator(".panel-view[data-view=governor] .governor-chat").click()
    expect(page.locator(".toast", has_text="hasn't been woken yet")).to_be_visible()
    expect(page).to_have_url(re.compile(r"/agentos$"))
    shot(page, "08f-sidebar-gov")
    view(page, "agentos")
    page.locator(".schedules-view").get_by_role("button", name="New task").click()
    page.get_by_placeholder("e.g. Morning summary").fill("Atlas data")
    page.get_by_placeholder("What should be done each time", exact=False).fill("Add new election data to the atlas")
    page.get_by_role("button", name="Schedule", exact=True).click()
    expect(page.locator(".schedule-row", has_text="Atlas data")).to_contain_text("Governor decides")
    # One of your scripts at its times (Relay runs it, with no agent): the form has it, without a prompt
    page.locator(".schedules-view").get_by_role("button", name="New task").click()
    page.get_by_role("button", name="A script of yours").click()
    expect(page.locator(".schedules-view textarea")).to_be_hidden()
    page.get_by_placeholder("e.g. Morning summary").fill("Ask each morning")
    page.get_by_label("Script", exact=True).select_option("public/examples/ask.py")
    page.get_by_label("Arguments").fill("what is two and two?")
    shot(page, "08ea-schedule-script")
    page.get_by_role("button", name="Schedule", exact=True).click()
    expect(page.locator(".schedule-row", has_text="Ask each morning")).to_contain_text(
        "runs public/examples/ask.py what is two and two?")
    # The dashboard: the figures at a glance, then the sections
    expect(page.locator(".os-tile", has_text="Tasks")).to_contain_text("next")
    expect(page.locator(".os-tile", has_text="Governor")).to_contain_text("Off")
    expect(page.locator(".section-title", has_text="Services")).to_be_visible()
    page.locator(".schedules-view").get_by_role("button", name="New service").click()
    expect(page.locator(".modal h2", has_text="New service")).to_be_visible()
    shot(page, "08g-new-service")
    page.get_by_role("button", name="Cancel").click()
    shot(page, "08f-scheduled")
    # A chat managed by AgentOS (its Governor made it): in the Governor view, not among your chats; its icon in
    # Relay's green, with "os" by it (marked in the throwaway's database)
    import sqlite3
    with sqlite3.connect(WORKSPACE[0].parent / "data" / "relay" / "relay.db") as db:
        db.execute("UPDATE conversations SET managed = 1 WHERE title LIKE 'Tell me about Relay%'")
    page.reload()
    view(page, "governor")
    expect(page.locator(".panel-view[data-view=chats] .conv", has_text="Tell me about Relay")).to_have_count(0)
    managed = page.locator(".panel-view[data-view=governor] .conv",
                           has_text="Tell me about Relay").locator(".mode-wrap.managed")
    expect(managed.locator(".os")).to_have_text("os")
    assert page.locator(".panel-view[data-view=governor] .conv .mode-wrap.managed").count() == 1
    page.locator(".sidebar").screenshot(path=f"{SHOTS}/08f-managed-icon.png")
    # A service's WorkBoxes (clones): only the newest in the list, the older ones under its arrow
    write = {"X-Relay-Request": "1", "Content-Type": "application/json"}
    made = [page.request.post(BASE + "/api/conversations", headers=write, data=json.dumps(
        {"provider_id": "fake-chat", "title": f"notes: work on version {n}"})).json()["id"] for n in (1, 2)]
    with sqlite3.connect(WORKSPACE[0].parent / "data" / "relay" / "relay.db") as db:
        user = db.execute("SELECT id FROM users").fetchone()[0]
        db.execute("INSERT INTO services (id, user_id, name, command, public_port, created) "
                   "VALUES ('a07e5', ?, 'notes', 'x', 29999, 0)", (user,))
        for n, conversation in zip((1, 2), made, strict=True):
            db.execute("INSERT INTO service_versions (id, service_id, number, folder, status, created) "
                       "VALUES (?, 'a07e5', ?, '/nowhere', 'archived', 0)", (f"v{n}", n))
            db.execute("INSERT INTO service_clones (conversation_id, service_id, from_version, created) "
                       "VALUES (?, 'a07e5', ?, 0)", (conversation, f"v{n}"))
            folder = WORKSPACE[0].parent / "data" / "relay" / "production" / "a07e5" / "work" / f"w{n}" / "notes"
            db.execute("UPDATE conversations SET managed = 1, mode = 'code', directory = ? WHERE id = ?",
                       (str(folder), conversation))
    page.reload()
    view(page, "governor")  # (reloaded on /agentos: the AgentOS view)
    newest = page.locator(".panel-view[data-view=governor] .conv", has_text="work on version 2")
    expect(newest.locator(".clones-btn")).to_contain_text("1")
    expect(page.locator(".panel-view[data-view=governor] .conv", has_text="work on version 1")).to_have_count(0)
    newest.locator(".clones-btn").click()
    expect(page.locator(".panel-view[data-view=governor] .conv.nested", has_text="work on version 1")).to_have_count(1)
    expect(page).to_have_url(re.compile(r"/agentos$"))  # (the arrow doesn't open the chat)
    newest.scroll_into_view_if_needed()
    page.locator(".sidebar").screenshot(path=f"{SHOTS}/08f-clones.png")
    newest.locator(".clones-btn").click()
    expect(page.locator(".panel-view[data-view=governor] .conv", has_text="work on version 1")).to_have_count(0)
    # That service has nothing live (its versions archived): not shown green, and its page asks no box (a simulated
    # user saw "…" there for good)
    other = page.context.new_page()
    other.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    other.goto(BASE + "/agentos")
    row = other.locator(".service-row", has_text="notes")
    expect(row.locator(".os-state")).to_have_text("nothing live")
    expect(row.locator(".status-dot")).not_to_have_class(re.compile(r"\bok\b"))
    row.locator(".service-name").click()  # (its page: /s/<its id>)
    expect(other.locator(".service-view .service-meta")).to_contain_text("nothing live")
    expect(other.locator(".service-tiles")).to_contain_text("nothing live")
    expect(other.locator(".service-tiles")).not_to_contain_text("…")
    other.close()
    # A long chat opens from its last messages; the part before on a click, its turns' actions too (16,000 events
    # took 2.5 s to open, 160,000 half a minute)
    long_chat = page.request.post(BASE + "/api/conversations", headers=write, data=json.dumps(
        {"provider_id": "fake-chat", "title": "Long chat"})).json()
    with sqlite3.connect(WORKSPACE[0].parent / "data" / "relay" / "relay.db") as db:
        for turn in range(40):
            run_id = f"{turn:032x}"
            db.execute("INSERT INTO messages (conversation_id, role, content, created) VALUES (?, 'user', ?, 0)",
                       (long_chat["id"], f"Question {turn}"))
            db.execute("INSERT INTO runs (id, conversation_id, status, created, finished, provider_id) "
                       "VALUES (?, ?, 'succeeded', 0, 0, 'fake-chat')", (run_id, long_chat["id"]))
            db.execute("INSERT INTO messages (conversation_id, role, content, run_id, created) "
                       "VALUES (?, 'assistant', 'Done', ?, 0)", (long_chat["id"], run_id))
            db.executemany("INSERT INTO run_events (run_id, seq, event) VALUES (?, ?, ?)", [
                (run_id, n, json.dumps({"type": "tool_call", "id": f"t{n}", "name": "shell", "input": {"command": "ls"}}
                                       if n % 2 else {"type": "text", "delta": f"step {n} "})) for n in range(120)])
    other = page.context.new_page()
    other.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    other.goto(BASE + f"/c/{long_chat['id']}")
    earlier = other.locator(".thread > .earlier-messages")
    expect(earlier).to_contain_text("earlier messages")
    shown = other.locator(".thread > .msg").count()
    assert 2 <= shown < 80, f"a long chat showed {shown} of its 80 messages at once"
    earlier.click()
    expect(other.locator(".thread > .msg")).not_to_have_count(shown)
    assert other.locator(".thread > .msg").count() > shown
    expect(other.locator(".thread > .msg.user .turn-action").first).to_be_attached()  # (Edit, on the earlier ones too)
    # Made through the API (not Relay's pages): yellow, with an "E", and who started it
    view(other, "chats")
    started = other.locator(".panel-view[data-view=chats] .conv", has_text="Long chat").locator(".mode-wrap.external")
    expect(started.locator(".os")).to_have_text("E")
    assert "Relay's API" in started.get_attribute("title"), started.get_attribute("title")
    other.locator(".sidebar").screenshot(path=f"{SHOTS}/08f-external-icon.png")
    view(other, "governor")  # (the view is this browser's: the reload below expects it)
    other.close()
    # Opened from there, it's marked there; another view leaves it open on the right, and a reload keeps the view
    page.locator(".panel-view[data-view=governor] .conv", has_text="Tell me about Relay").click()
    expect(page.locator(".panel-view[data-view=governor] .conv.active")).to_contain_text("Tell me about Relay")
    expect(page.locator(".header .title-btn")).to_contain_text("Tell me about Relay")
    for other_view in ("chats", "tasks", "scripts"):
        view(page, other_view)
        expect(page.locator(".header .title-btn")).to_contain_text("Tell me about Relay")
        assert "/c/" in page.url, page.url
    view(page, "governor")
    page.locator(".sidebar").screenshot(path=f"{SHOTS}/08f-governor-view.png")
    page.reload()
    expect(page.locator(".panel-view.shown")).to_have_attribute("data-view", "governor")
    with sqlite3.connect(WORKSPACE[0].parent / "data" / "relay" / "relay.db") as db:  # (the user's own again)
        db.execute("UPDATE conversations SET managed = 0 WHERE title LIKE 'Tell me about Relay%'")
    page.goto(BASE + "/agentos")
    # Tasks in three kinds. A system task (made one here: AgentOS on would wake a Governor without models)
    # warns before it's edited, has no Delete, and resets to Relay's default
    made_task = page.request.post(BASE + "/api/schedules", headers=write, data=json.dumps(
        {"name": "Services, my way", "prompt": "x", "when": {"kind": "weekly", "days": [0], "time": "09:00"},
         "governed": True})).json()
    with sqlite3.connect(WORKSPACE[0].parent / "data" / "relay" / "relay.db") as db:
        db.execute("UPDATE schedules SET system = 'services-check' WHERE id = ?", (made_task["id"],))
    page.reload()
    system = page.locator(".os-task-kind.system")
    expect(system.locator("h3")).to_contain_text("System tasks")
    expect(system).to_contain_text("edited")  # (not as Relay ships it)
    expect(page.locator(".os-task-kind.manual")).to_contain_text("Atlas data")
    expect(system.locator("button[title='Delete']")).to_have_count(0)
    system.locator("button[title='Edit']").click()
    expect(page.locator(".modal", has_text="Edit one of Relay's own tasks?")).to_be_visible()
    page.get_by_role("button", name="Edit anyway").click()
    expect(page.locator("h1", has_text="Edit Services, my way")).to_be_visible()
    shot(page, "08f-system-task-edit")
    page.get_by_role("button", name="Cancel").click()
    system.locator(".reset-task").click()
    page.locator(".modal").get_by_role("button", name="Reset").click()
    expect(system).to_contain_text("Services: weekly check")
    expect(system).not_to_contain_text("edited")
    shot(page, "08f-task-kinds")
    model_ranking(page, BASE)
    # A live service under Production boxes: its page (a real one): Overview, a shell in its ProductionBox, its log
    site = WORKSPACE[0] / "demo-app"
    (site / "index.html").write_text("hello from demo\n")
    write = {"X-Relay-Request": "1", "Content-Type": "application/json"}
    made = page.request.post(BASE + "/api/services", headers=write, data=json.dumps({
        "name": "demo-site", "folder": str(site), "command": "python3 -u -m http.server $PORT",
        "port": free_port()})).json()
    assert made.get("live"), made
    try:
        view(page, "production")
        page.locator(".production-list .panel-item", has_text="demo-site").click()
        expect(page).to_have_url(re.compile(r"/s/[0-9a-f]+$"))
        expect(page.locator(".service-view h1")).to_contain_text("demo-site")
        overview = page.locator(".service-overview")
        expect(overview.locator(".os-tile", has_text="Live")).to_contain_text("v1", timeout=15000)
        expect(overview.locator(".os-tile", has_text="Health")).to_contain_text("Well")
        expect(overview.locator(".service-version.live")).to_contain_text("Made live by you")
        expect(overview.locator(".service-facts")).to_contain_text("python3 -u -m http.server $PORT")
        expect(page.locator(".service-view .shell-terminal")).to_have_count(0)  # (connected when it's opened)
        shot(page, "08h-production-overview")
        page.set_viewport_size({"width": 390, "height": 800})
        assert page.evaluate("document.documentElement.scrollWidth") <= 390
        shot(page, "08h-production-overview-phone")
        page.set_viewport_size({"width": 1280, "height": 820})
        page.get_by_role("tab", name="Log").click()
        expect(page.locator(".service-log-text")).to_contain_text("HTTP", timeout=15000)
        page.get_by_role("tab", name="Shell").click()
        expect(page.locator(".service-view .shell-status")).to_contain_text("Connected", timeout=30000)
        page.locator(".service-view .xterm-helper-textarea").focus()
        page.keyboard.type("ls")
        page.keyboard.press("Enter")
        expect(page.locator(".service-view .xterm-rows")).to_contain_text("index.html")
        expect(page.locator(".production-list .panel-item.active")).to_have_text("demo-site")
        expect(page.get_by_role("button", name="New shell")).to_be_hidden()  # (until the shell ends)
        shot(page, "08h-production-box")
        page.set_viewport_size({"width": 390, "height": 800})
        expect(page.locator(".service-view .shell-terminal")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth") <= 390
        shot(page, "08h-production-box-phone")
        page.set_viewport_size({"width": 1280, "height": 820})
        # Your own (new services are): the Governor doesn't see it; given to AgentOS, the os mark in the sidebar
        agentos = page.locator(".service-view .agentos-toggle")
        expect(agentos).to_have_text("Private")
        expect(page.locator(".production-list .panel-item",
                            has_text="demo-site").locator(".mode-wrap.managed")).to_have_count(0)
        agentos.click()
        expect(agentos).to_have_text("Managed by AgentOS")
        expect(page.locator(".production-list .panel-item",
                            has_text="demo-site").locator(".mode-wrap.managed .os")).to_have_text("os")
        agentos.click()
        expect(agentos).to_have_text("Private")
        # Demote: a WorkBox of your own at once (no Governor); /promote (or Promote to live) in its chat
        page.get_by_role("button", name="Demote").click()
        expect(page.locator(".header .title-btn")).to_contain_text("demo-site: work on version 1", timeout=30000)
        workbox = page.url
        promote = page.get_by_role("button", name="Promote to live")
        expect(promote).to_be_enabled()
        shot(page, "08h-workbox")
        page.get_by_label("Message").fill("/promote")
        page.get_by_label("Message").press("Enter")
        expect(page.locator(".toast", has_text="Version 2 of demo-site is live")).to_be_visible(timeout=60000)
        expect(promote).to_be_enabled()  # (it goes on from version 2: promote it again later)
        page.locator(".production-list .panel-item", has_text="demo-site").click()
        expect(page.locator(".service-view .service-meta")).to_contain_text("version 2 live")
        expect(page.locator(".service-workboxes")).to_contain_text("demo-site: work on version 1")
        expect(page.locator(".service-version.live")).to_contain_text("from demo-site: work on version 1")
        expect(page.locator(".service-snapshot", has_text="Before version 2")).to_have_count(1)  # (its data's copy)
        # Back to a version of your choice (once it's checked), from its row
        page.locator(".service-version.archived", has_text="v1").get_by_role("button", name="Roll back to this").click()
        page.locator(".modal").get_by_role("button", name="Roll back", exact=True).click()
        expect(page.locator(".service-view .service-meta")).to_contain_text("version 1 live", timeout=60000)
        expect(page.locator(".service-version.archived", has_text="v2")).to_have_count(1)
        page.goto(workbox)
        view(page, "chats")  # (your own WorkBox: among your chats)
        item = page.locator(".conv.active")
        item.hover()
        item.locator(".menu-btn").click()
        page.locator(".menu button", has_text="Delete").click()
        page.locator(".modal").get_by_role("button", name="Delete", exact=True).click()
        expect(page.locator(".conv", has_text="demo-site: work on version 1")).to_have_count(0)
        # Deleted from its own page
        view(page, "production")
        page.locator(".production-list .panel-item", has_text="demo-site").click()
        page.get_by_title("Delete this service (its versions, archive and data)").click()
        page.locator(".modal").get_by_role("button", name="Delete with its data").click()
        expect(page).to_have_url(re.compile(r"/agentos$"), timeout=30000)  # (its boxes and volume go first)
        expect(page.locator(".production-list .panel-item", has_text="demo-site")).to_have_count(0)
    finally:
        page.request.delete(f"{BASE}/api/services/{made['id']}", headers=write)
    # AgentOS left out of Relay (Settings → AgentOS, one switch): its page is your scheduled tasks, and the
    # Governor, Production boxes, AgentOS's conversations and /promote are gone; on again, all of it is back
    page.locator(".activity-bar").get_by_role("button", name="Settings", exact=True).click()
    page.locator(".settings nav button", has_text="AgentOS").click()
    switch = page.locator("label.switch", has=page.get_by_label("AgentOS in Relay"))
    switch.click()
    expect(page.get_by_label("AgentOS in Relay")).not_to_be_checked()
    expect(page.locator(".toast", has_text="AgentOS is off")).to_be_visible()
    shot(page, "08i-agentos-off-setting")
    page.keyboard.press("Escape")
    bar = page.locator(".activity-bar")
    expect(bar.locator("[data-view=agentos]")).to_have_attribute("aria-label", "Scheduled tasks")
    expect(bar.locator("[data-view=governor]")).to_be_hidden()
    expect(bar.locator("[data-view=production]")).to_be_hidden()
    expect(page.locator(".panel-view[data-view=governor] .conv")).to_have_count(0)
    view(page, "agentos")
    expect(page.locator(".panel-view[data-view=agentos] .panel-title")).to_have_text("Scheduled")
    page.locator(".agentos-list .panel-item", has_text="Scheduled tasks").click()
    expect(page.locator(".schedules-view h1")).to_have_text("Scheduled tasks")
    shot(page, "08i-agentos-off")
    view(page, "chats")
    page.locator(".conv", has_text="Greeting script").click()
    expect(page.locator(".header .title-btn")).to_contain_text("Greeting script")
    page.get_by_label("Message").fill("/pro")  # (on, the same shows /promote: below)
    expect(page.locator(".menu button", has_text="/promote")).to_have_count(0)
    page.get_by_label("Message").fill("")
    page.locator(".activity-bar").get_by_role("button", name="Settings", exact=True).click()
    page.locator(".settings nav button", has_text="AgentOS").click()
    switch.click()
    expect(page.get_by_label("AgentOS in Relay")).to_be_checked()
    expect(page.locator(".toast", has_text="AgentOS is on")).to_be_visible()
    page.keyboard.press("Escape")
    expect(bar.locator("[data-view=agentos]")).to_have_attribute("aria-label", "AgentOS")
    expect(bar.locator("[data-view=production]")).to_be_visible()
    expect(page.locator(".header .title-btn")).to_contain_text("Greeting script")  # (still open)
    page.get_by_label("Message").fill("/pro")
    expect(page.locator(".menu button", has_text="/promote")).to_be_visible()
    page.get_by_label("Message").fill("")
    # /promote in a code task: the project becomes a ProductionBox, and the task goes on as its WorkBox
    page.get_by_label("Message").fill("/promote")
    page.get_by_label("Message").press("Enter")
    dialog = page.locator(".modal", has_text="Make a ProductionBox")
    expect(dialog.get_by_label("Name", exact=True)).to_have_value("git-app")  # (from the folder)
    expect(dialog.get_by_label("Starts with")).to_have_value("python3 main.py")  # (a guess)
    dialog.get_by_label("Starts with").fill("python3 -m http.server $PORT")
    shot(page, "08j-promote-dialog")
    dialog.get_by_role("button", name="Make it live").click()
    expect(page.locator(".toast", has_text="git-app is live")).to_be_visible(timeout=60000)
    expect(page.get_by_role("button", name="Promote to live")).to_be_visible()
    promoted = next(s for s in page.request.get(BASE + "/api/services").json() if s["name"] == "git-app")
    assert [w["title"] for w in promoted["workboxes"]] == ["Greeting script"], promoted["workboxes"]
    page.request.delete(f"{BASE}/api/services/{promoted['id']}", headers={"X-Relay-Request": "1"})
    # /timed: a timer bar, the task sent; in its soft part Relay asks the agent once more (a max only:
    # no min, so the fake agent isn't sent back again and again); /timed off puts the bar away
    expect(page.locator(".composer .send-btn.stop")).to_have_count(0, timeout=15000)
    # A command that fails gives back what was typed, to fix it (here one refused in the page itself)
    page.get_by_label("Message").fill("/mode nonexistent")
    page.get_by_label("Message").press("Enter")
    expect(page.locator(".toast", has_text="No mode")).to_be_visible()
    expect(page.get_by_label("Message")).to_have_value("/mode nonexistent")
    page.get_by_label("Message").fill("/timed 2 Say hello for the timer")
    page.get_by_label("Message").press("Enter")
    expect(page.locator(".timed-bar")).to_contain_text("stops at")
    expect(page.locator(".msg.user", has_text="Say hello for the timer")).to_have_count(1)
    expect(page.locator(".msg.relay", has_text="the timed request")).to_have_count(1, timeout=20000)
    expect(page.locator(".composer .send-btn.stop")).to_have_count(0, timeout=15000)
    shot(page, "08i-timed")
    page.get_by_role("button", name="Stop timer").click()
    expect(page.locator(".timed-bar")).to_have_count(0)

    # A computer task: none yet, then one with its connector running
    in_view(page, "chats", "New computer task")
    expect(page.locator(".welcome h1")).to_have_text("No computers yet")
    shot(page, "08g-computer-task-none")
    added = page.request.post(BASE + "/api/computers", data=json.dumps({"name": "laptop"}),
                              headers={"X-Relay-Request": "1", "Content-Type": "application/json"}).json()
    task_connector = Path(SHOTS) / "relay-computer-task.py"
    task_connector.write_bytes(page.request.get(f"{BASE}/api/computers/{added['id']}/script").body())
    running_connector = subprocess.Popen([sys.executable, str(task_connector)], stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 15
        while not page.request.get(BASE + "/api/computers").json()[0]["online"] and time.time() < deadline:
            time.sleep(0.2)
        in_view(page, "chats", "New computer task")
        expect(page.locator(".welcome h1")).to_have_text("What should we do on your computer?")
        page.locator(".composer .picker").first.click()
        page.locator(".menu button", has_text="Fake Agent").click()
        expect(page.locator(".composer .picker", has_text="laptop").locator(".status-dot.ok")).to_have_count(1)
        shot(page, "08h-computer-task")
        page.get_by_label("Message").fill("How much disk space is left?")
        page.get_by_label("Message").press("Enter")
        expect(page.locator(".header .chip.computer")).to_contain_text("laptop")
        expect(page.locator(".conv.active .mode")).to_have_count(1)
        expect(page.locator(".composer-note")).to_contain_text("The agent works on laptop")
        expect(page.locator(".composer .send-btn.stop")).to_have_count(0, timeout=15000)
    finally:
        running_connector.terminate()
    page.request.delete(f"{BASE}/api/computers/{added['id']}", headers={"X-Relay-Request": "1"})
    page.locator(".conv", has_text="Greeting script").click()
    expect(page.locator(".header .title-btn")).to_contain_text("Greeting script")  # open before picking

    # A usage limit: Relay says when it resets and goes on then (or not: Cancel)
    page.locator(".composer .picker").first.click()
    page.locator(".menu button", has_text="Fake Limited").click()
    page.get_by_label("Message").fill("Add a greeting")
    page.get_by_label("Message").press("Enter")
    note = page.locator(".turn-recovery")
    expect(note.locator("summary")).to_be_visible(timeout=15000)
    note.locator("summary").click()
    expect(note).to_contain_text("Fake Limited hit its usage limit. Relay will tell it to go on when it resets, at",
                                 timeout=15000)
    expect(page.locator(".conv.active .resume-badge")).to_contain_text("Resumes at")
    shot(page, "08d-usage-limit")
    # The activity bar's ring: what's left of the tightest limit (Fake Limited's 5-hour window: nothing), every
    # provider's windows a click away
    ring = page.locator(".activity-bar .usage-item")
    expect(ring).to_be_visible(timeout=20000)
    expect(ring).to_have_class(re.compile(r"\bbad\b"))
    expect(ring).to_have_attribute("title", re.compile(r"^Usage limits: .*: 0% left"))
    ring.click()
    popover = page.locator(".usage-popover")
    expect(popover.locator(".limit-row", has_text="Fake Limited")).to_contain_text("0% left")
    expect(popover.get_by_role("button", name="All of it in Settings → Usage")).to_be_visible()
    shot(page, "08d-usage-ring")
    page.keyboard.press("Escape")
    expect(popover).to_have_count(0)
    expect(ring).to_have_attribute("aria-expanded", "false")
    ring.click()
    popover.get_by_role("button", name="All of it in Settings → Usage").click()
    expect(popover).to_have_count(0)
    expect(page.locator(".settings .limits-section .limit-row", has_text="Fake Limited")).to_contain_text("0% left")
    page.keyboard.press("Escape")
    expect(page.locator(".settings")).to_have_count(0)
    note.get_by_role("button", name="Cancel").click()
    note.locator("summary").click()
    expect(note).to_contain_text("It resets at")
    expect(page.locator(".conv.active .resume-badge")).to_have_count(0)
    note.get_by_role("button", name="Resume then").click()
    expect(note.get_by_role("button", name="Cancel")).to_be_visible()
    # …or another model takes over now
    note.get_by_role("button", name="Choose another model").click()
    page.locator(".menu button", has_text="Fake Agent").click()
    page.locator(".menu button", has_text="Default").click()
    relay_note = page.locator(".msg.relay").last
    expect(relay_note).to_be_visible(timeout=15000)
    relay_note.locator("summary").click()
    # (it hit its limit before it said or did anything: the message is what's left to answer)
    expect(relay_note).to_contain_text("Fake Limited stopped before it answered, so you answer instead")
    expect(relay_note).to_contain_text("Add a greeting")
    expect(page.locator(".composer .send-btn.stop")).to_have_count(0, timeout=15000)
    expect(page.locator(".composer .picker").first).to_contain_text("Fake Agent")
    expect(page.locator(".turn-recovery")).to_have_count(0)
    page.locator(".composer .picker").first.click()
    page.locator(".menu button", has_text="Fake Chat").click()

    scripts_page(page)
    temporary_chats(page, BASE)
    side_column(page, BASE)
    notes_plugin(page, BASE)
    usage_limits_plugin(page)
    server_plugin(page)
    for part in PARTS.values():
        part.check(page, shot)
    acp_plugin(page)
    vscodium_plugin(page)
    chrome_plugin(page)
    openhands_plugin(page)

    # Another browser (signed in, nothing of this one's kept): its new chats start with the provider and model
    # picked last here (Relay keeps them, not the browser)
    picked = page.request.get(BASE + "/api/state").json()["new_chat"]
    assert picked and picked["provider_id"], picked
    elsewhere = browser.new_context(storage_state={"cookies": context.storage_state()["cookies"], "origins": []})
    other = elsewhere.new_page()
    other.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    other.goto(BASE + "/")
    expect(other.locator(".welcome h1")).to_be_visible()
    draft = json.loads(other.evaluate("localStorage.getItem('relay.draft')"))
    assert (draft["providerId"], draft["model"] or None) == (picked["provider_id"], picked["model"]), (draft, picked)
    elsewhere.close()

    # Mobile
    mobile = browser.new_context(viewport={"width": 390, "height": 800}, color_scheme="dark",
                                 storage_state=context.storage_state())
    mpage = mobile.new_page()
    mpage.on("pageerror", lambda e: problems.append(f"mobile pageerror: {e}"))
    mpage.goto(BASE + "/")
    expect(mpage.locator(".welcome h1")).to_be_visible()
    shot(mpage, "09-mobile-new")
    # The layout is as tall as the visual viewport (js/viewport.js), so the on-screen keyboard doesn't cover it
    assert mpage.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--app-height')") == "800px"
    assert mpage.evaluate("document.querySelector('.layout').getBoundingClientRect().height") == 800
    mpage.locator(".header .icon-btn").first.click()
    view(mpage, "chats")  # (the drawer: the bar and the column, in the view left last)
    mpage.wait_for_timeout(400)
    box = mpage.locator(".sidebar").bounding_box()
    assert box["x"] >= 0, f"sidebar not on screen: {box}"
    no_sideways_scroll(mpage, "mobile drawer")
    shot(mpage, "10-mobile-sidebar")
    # (the kept temporary chat is named alike)
    mpage.locator(".conv .title", has_text="Tell me about Relay").last.click()
    expect(mpage.locator(".msg.assistant .md table")).to_be_visible()
    # No hover on a phone: the actions under answers stay visible.
    assert mpage.locator(".msg.assistant .msg-actions").last.evaluate(
        "e => getComputedStyle(e).opacity") == "1", "message actions hidden on a phone"
    no_sideways_scroll(mpage, "mobile chat")
    shot(mpage, "11-mobile-chat")
    mpage.goto(BASE + "/")
    mpage.locator(".header .icon-btn").first.click()
    view(mpage, "chats")
    mpage.locator(".conv .title", has_text="Update main.py and run").click()
    expect(mpage.locator(".msg.assistant").first).to_contain_text("want me to fix it?")
    no_sideways_scroll(mpage, "mobile code task")
    # One row, always: the title and the actions in a strip that scrolls sideways where they don't fit (as
    # the pickers do); here they do, on the narrowest phone they're a scroll away
    assert mpage.locator(".header").bounding_box()["height"] <= 53, mpage.locator(".header").bounding_box()
    mpage.wait_for_function("""() => {
        const strip = document.querySelector('.header-strip');
        return strip.querySelector('.header-heading').getBoundingClientRect().right
            <= strip.querySelector('.meta').getBoundingClientRect().left;  // (side by side)
    }""")
    reach_changes(mpage)
    mpage.set_viewport_size({"width": 320, "height": 700})
    mpage.wait_for_function("""() => {
        const strip = document.querySelector('.header-strip'), heading = strip.querySelector('.header-heading');
        const meta = strip.querySelector('.meta').getBoundingClientRect();
        return strip.scrollWidth > strip.clientWidth && getComputedStyle(strip).overflowX === 'auto'
            && heading.getBoundingClientRect().right <= meta.left;  // (side by side, not on top of each other)
    }""")
    assert mpage.locator(".header").bounding_box()["height"] <= 53
    fits_on_screen(mpage, ".header .activity-badge")
    reach_changes(mpage)
    mpage.set_viewport_size({"width": 390, "height": 800})
    shot(mpage, "12-mobile-code-task")
    mpage.locator(".changes-btn").click()
    expect(mpage.locator(".changes-panel")).to_be_visible()
    shot(mpage, "13-mobile-changes")
    mpage.keyboard.press("Escape")
    mpage.goto(BASE + "/tasks")
    expect(mpage.locator(".task-card").first).to_be_visible()
    no_sideways_scroll(mpage, "mobile tasks")
    shot(mpage, "14-mobile-tasks")
    mpage.goto(BASE + "/scripts/private/ask.py")
    expect(mpage.get_by_label("private/ask.py's code")).to_be_visible()
    no_sideways_scroll(mpage, "mobile scripts")
    shot(mpage, "14b-mobile-scripts")
    mpage.locator(".header .icon-btn").first.click()  # (the drawer: in the Scripts view, as the page)
    expect(mpage.locator(".panel-view.shown")).to_have_attribute("data-view", "scripts")
    expect(mpage.locator(".scripts-list .panel-item.active")).to_have_text("ask.py")
    no_sideways_scroll(mpage, "mobile scripts view")
    shot(mpage, "14c-mobile-scripts-view")
    mpage.locator(".scripts-list .panel-group.public .panel-item", has_text="refusals.py").click()  # (made public)
    expect(mpage.get_by_label("public/refusals.py's code")).to_be_visible()
    expect(mpage.locator(".layout.sidebar-open")).to_have_count(0)  # (the drawer closes as it opens one)
    mpage.goto(BASE + "/p/usage_limits")  # (a plugin's page, dark, on a phone)
    expect(mpage.locator(".ul-chart svg").first).to_be_visible()
    no_sideways_scroll(mpage, "mobile usage limits")
    shot(mpage, "14d-mobile-usage-limits")
    mpage.goto(BASE + "/p/server")
    expect(mpage.locator(".sv-tile").first).to_be_visible()
    no_sideways_scroll(mpage, "mobile server")
    shot(mpage, "14e-mobile-server")
    mpage.goto(BASE + "/")
    mpage.locator(".header .icon-btn").first.click()
    mpage.get_by_role("button", name="Settings").first.click()
    mpage.get_by_label("Settings category").select_option(label="Providers")
    no_sideways_scroll(mpage, "mobile settings")
    fits_on_screen(mpage, ".modal")
    shot(mpage, "15-mobile-settings")
    for tab, name in (("Modes", "16-mobile-modes"), ("Usage", "17-mobile-usage"), ("Subagents", "17b-mobile-subagents"),
                      ("Computers", "17c-mobile-computers"), ("Limits", "17d-mobile-limits"),
                      ("Providers", "17e-mobile-providers"), ("Skills", "17f-mobile-skills")):
        mpage.get_by_label("Settings category").select_option(label=tab)
        mpage.wait_for_timeout(300)
        fits_on_screen(mpage, ".modal")
        no_sideways_scroll(mpage, f"mobile {tab.lower()}")
        shot(mpage, name)
    mpage.keyboard.press("Escape")

    # The orchestrator chat on a phone: subagent card, the answered permission
    mpage.goto(BASE + "/")
    mpage.locator(".header .icon-btn").first.click()
    view(mpage, "chats")
    mpage.locator(".conv .title", has_text="What is Relay, briefly?").click()
    expect(mpage.locator(".tool.subagent")).to_be_visible()
    mpage.locator(".tool.subagent .tool-head").click()
    expect(mpage.locator(".subagent-thread .md")).to_be_visible(timeout=10000)
    no_sideways_scroll(mpage, "mobile subagent")
    shot(mpage, "18-mobile-subagent")
    # AgentOS on a phone
    mpage.goto(BASE + "/agentos")
    expect(mpage.locator(".schedule-row").first).to_be_visible()
    no_sideways_scroll(mpage, "mobile scheduled")
    shot(mpage, "18b-mobile-scheduled")
    mpage.locator(".schedules-view").get_by_role("button", name="New task").click()
    no_sideways_scroll(mpage, "mobile schedule form")
    shot(mpage, "18c-mobile-schedule-form")
    mpage.goto(BASE + "/")
    mpage.locator(".header .icon-btn").first.click()
    view(mpage, "chats")
    mpage.locator(".conv .title", has_text="What is Relay, briefly?").click()
    expect(mpage.locator(".header")).to_contain_text("What is Relay, briefly?")  # opened before typing
    # Claude Code's permission prompt: Relay answers it itself (commands run in a container)
    mpage.get_by_label("Message").fill("Deploy it again, with permission")
    mpage.get_by_label("Message").press("Enter")
    expect(mpage.locator(".msg.assistant").last).to_contain_text("Deploying.", timeout=15000)
    expect(mpage.locator(".composer .send-btn.stop")).to_have_count(0, timeout=15000)
    mpage.get_by_label("Message").fill("Then tidy up")
    mpage.get_by_label("Message").press("Enter")  # a phone keyboard would use the button; same thing
    no_sideways_scroll(mpage, "mobile answer")
    shot(mpage, "19-mobile-answer")
    # A subagent that isn't allowed without asking: the choices, and an allowed one instead
    expect(mpage.locator(".msg.assistant").last).to_contain_text("My helper looked into it", timeout=20000)
    expect(mpage.locator(".composer .send-btn.stop")).to_have_count(0, timeout=15000)
    mpage.get_by_label("Message").fill("Ask the paid helper")
    mpage.get_by_label("Message").press("Enter")
    ask = mpage.locator(".run-note.ask")
    expect(ask).to_contain_text("Subagent on Fake Agent", timeout=15000)
    expect(ask.locator(".ask-countdown")).to_contain_text("denied in")
    fits_on_screen(mpage, ".run-note.ask")
    no_sideways_scroll(mpage, "mobile subagent choice")
    shot(mpage, "19b-mobile-subagent-choice")
    ask.get_by_role("button", name=re.compile("Use Fake Chat")).click()
    expect(mpage.locator(".run-note.answered").last).to_contain_text("Fake Chat used instead")
    expect(mpage.locator(".msg.assistant").last).to_contain_text("it answered.", timeout=20000)
    # The / menu for modes
    mpage.get_by_label("Message").fill("/")
    expect(mpage.locator(".menu")).to_be_visible()
    fits_on_screen(mpage, ".menu")
    shot(mpage, "20-mobile-slash")
    mpage.keyboard.press("Escape")
    # A project folder from what says so: on a phone its picker is a scroll away (a simulated user found)
    mpage.locator(".header .icon-btn").first.click()
    in_view(mpage, "chats", "New conversation")
    mpage.locator(".welcome-lead").get_by_role("button", name="Choose a project folder").click()
    expect(mpage.locator(".modal")).to_be_visible()
    fits_on_screen(mpage, ".modal")
    for button in mpage.locator(".modal-foot .btn").all():  # ("New folder" stuck out to the left, once)
        box = button.bounding_box()
        assert box and box["x"] >= 0 and box["x"] + box["width"] <= 390.5, f"a dialog's button sticks out: {box}"
    mpage.keyboard.press("Escape")
    # Installable: the manifest and icons are served
    manifest = mpage.request.get(BASE + "/static/manifest.webmanifest")
    assert manifest.ok and manifest.headers["content-type"].startswith("application/manifest+json")
    # (its paths relative to it: Relay under a path of its site has them too)
    assert urllib.parse.urljoin(manifest.url, manifest.json()["start_url"]) == BASE + "/"
    for icon in manifest.json()["icons"]:
        assert mpage.request.get(urllib.parse.urljoin(manifest.url, icon["src"])).ok, icon["src"]
    assert mpage.locator('meta[name="theme-color"]').first.get_attribute("content") == "#212121"  # dark
    browser.close()


def add_with_an_agent(page):
    """Settings → Add provider → A provider Relay doesn't know → Add it with an agent (relay/creations.py): its page.
    1. A shell in a box of its own (real: node is there to install with); what was installed, and who integrates it.
    2. The agent's plan, by Relay's tool: Redraft, then Approve.
    3. Relay's whole check in a test box, then a throwaway Relay with it at /trial/<id>/, signed in, with its banner and
    the new provider. Cancel at the end (Add would put it in this Relay's code, the real one's)."""
    page.get_by_role("button", name="Add it with an agent").click()
    expect(page).to_have_url(re.compile(r"/providers/new$"))
    expect(page.locator(".newprovider-intro")).to_contain_text("you've decided to add a new provider with an agent")
    page.get_by_role("button", name="Start").click()
    expect(page).to_have_url(re.compile(r"/providers/new/[0-9a-f]{12}$"))
    step1, step2, step3 = (page.locator(".newprovider-step").nth(i) for i in range(3))
    expect(step1).to_have_class(re.compile("current"))
    expect(step1.locator(".shell-status")).to_contain_text("Connected", timeout=120000)
    step1.locator(".shell-terminal").click()
    page.keyboard.type("node --version; echo home=$HOME\n")
    expect(step1.locator(".xterm-rows")).to_contain_text("v22", timeout=30000)
    expect(step1.locator(".xterm-rows")).to_contain_text("provider-homes")
    step1.get_by_label("What did you install?").fill("toy-agent, the guide's example (a tiny coding agent)")
    step1.get_by_label("Integrated by").select_option(label="Fake Builder")
    shot(page, "08-newprovider-install")
    step1.get_by_role("button", name="Continue").click()
    expect(step2.get_by_role("button", name="Approve")).to_be_visible(timeout=60000)
    expect(step2.locator(".newprovider-plan")).to_contain_text("Run toy-agent")
    shot(page, "08-newprovider-plan")
    step2.get_by_role("button", name="Redraft").click()
    step2.get_by_placeholder("What should be different?").fill("Say which models it has")
    step2.get_by_role("button", name="Send").click()
    expect(step2.get_by_role("button", name="Approve")).to_be_visible(timeout=60000)
    step2.get_by_role("button", name="Approve").click()
    # Built, Relay's whole check of it in a test box (a few real turns of toy-agent), then its throwaway Relay
    visit = step3.get_by_role("link", name="Open the throwaway Relay")
    expect(visit).to_be_visible(timeout=600000)
    expect(step3).to_contain_text("Please visit it")
    shot(page, "08-newprovider-try")
    with page.context.expect_page() as opened:
        visit.click()
    trial = opened.value
    expect(trial.locator(".trial-banner")).to_contain_text("Trial Relay for Toy agent", timeout=30000)
    expect(trial).to_have_url(re.compile(r"/trial/[0-9a-f]{12}/$"))
    expect(trial.locator(".composer .picker").first).to_contain_text("Toy agent", timeout=30000)
    shot(trial, "08-newprovider-trial")
    trial.close()
    page.reload()  # (this Relay's sign-in is its own still)
    expect(step3.get_by_role("button", name="Add")).to_be_visible()
    # Cancel: its box, its home, the agent's work and the throwaway Relay go
    page.get_by_role("button", name="Cancel adding it").click()
    page.locator(".modal").get_by_role("button", name="Throw it away").click()
    expect(step3).to_contain_text("Cancelled", timeout=60000)


def repair_on_its_page(page):
    """Settings → Providers → a provider's 🔧 (relay/creations.py, a repair): its page. What's wrong as Relay sees it;
    what the user says, and who repairs it; the agent's plan, approved; Relay's whole check of the repair in a test box
    (Try it anyway when a scenario fails); a real throwaway Relay with it; Cancel (Apply would change this Relay's
    code, the real one's)."""
    page.get_by_role("button", name="Settings").first.click()
    page.locator(".settings nav button", has_text="Providers").click()
    page.locator(".provider-row", has_text="Fake Chat").get_by_title(re.compile("^Repair")).click()
    expect(page).to_have_url(re.compile(r"/providers/repair/new\?provider=fake-chat$"))
    expect(page.locator(".newprovider-trouble")).to_contain_text("Its check")
    page.get_by_role("button", name="Start").click()
    expect(page).to_have_url(re.compile(r"/providers/repair/[0-9a-f]{12}$"))
    step1, step2, step3 = (page.locator(".newprovider-step").nth(i) for i in range(3))
    expect(page.locator("h1")).to_contain_text("Repair Fake Chat")
    expect(step1).to_contain_text("this machine's")  # (no home of its own: no shell)
    step1.get_by_label("What's wrong, in your words").fill("Its answers stopped coming")
    step1.get_by_label("Repaired by").select_option(label="Fake Builder")
    shot(page, "08-repair-wrong")
    step1.get_by_role("button", name="Continue").click()
    expect(step2.get_by_role("button", name="Approve")).to_be_visible(timeout=60000)
    expect(step2.locator(".newprovider-plan")).to_contain_text("Its parser")
    shot(page, "08-repair-plan")
    step2.get_by_role("button", name="Approve").click()
    visit = step3.get_by_role("link", name="Open the throwaway Relay")
    anyway = step3.get_by_role("button", name="Try it anyway")
    expect(visit.or_(anyway)).to_be_visible(timeout=600000)
    if anyway.is_visible():
        shot(page, "08-repair-checked")
        anyway.click()
        expect(visit).to_be_visible(timeout=300000)
    expect(step3).to_contain_text("click Apply below")
    shot(page, "08-repair-try")
    with page.context.expect_page() as opened:
        visit.click()
    trial = opened.value
    expect(trial.locator(".trial-banner")).to_contain_text("Trial Relay for Fake Chat", timeout=30000)
    trial.close()
    page.get_by_role("button", name="Cancel the repair").click()
    page.locator(".modal").get_by_role("button", name="Throw it away").click()
    expect(step3).to_contain_text("Cancelled", timeout=60000)


def reach_changes(page):
    """Review changes, scrolled to in the header's strip and measured in one go (the header is rendered anew
    as the page polls), then the strip back at its start."""
    box = page.evaluate("""() => {
        const button = document.querySelector('.header .changes-btn');
        button.scrollIntoView({ block: 'nearest', inline: 'nearest' });
        const box = button.getBoundingClientRect();
        document.querySelector('.header-strip').scrollLeft = 0;
        return { x: box.x, width: box.width };
    }""")
    width = page.viewport_size["width"]
    assert box["x"] >= 0 and box["x"] + box["width"] <= width + 0.5, f"Review changes sticks out: {box}"


def fits_on_screen(page, selector):
    box = page.locator(selector).first.bounding_box()
    width = page.viewport_size["width"]
    assert box and box["x"] >= 0 and box["x"] + box["width"] <= width + 0.5, f"{selector} sticks out: {box}"


def no_stray_text(page, where):
    """No missing value shown as text: "null", "undefined", "[object Object]" (outside code). Found by a simulated
    user: every confirmation dialog said "null" between its buttons (the DOM's own append shows a null so)."""
    stray = page.evaluate("""() => {
        const found = [], walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        while (walker.nextNode()) {
            const text = walker.currentNode.textContent.trim();
            if (["null", "undefined", "[object Object]", "NaN"].includes(text)
                && !walker.currentNode.parentElement.closest("pre, code")) found.push(text);
        }
        return found;
    }""")
    assert not stray, f"{where}: {stray[0]!r} shown as text on the page"


def no_sideways_scroll(page, where):
    width = page.evaluate("[document.scrollingElement.scrollWidth, innerWidth]")
    if width[0] > width[1]:
        culprits = page.evaluate("""() => [...document.querySelectorAll('body *')]
            .filter((e) => e.getBoundingClientRect().right > innerWidth + 0.5)
            .map((e) => e.tagName.toLowerCase() + '.' + [...e.classList].join('.')
                 + ' ' + Math.round(e.getBoundingClientRect().right)).slice(0, 8)""")
        raise AssertionError(f"{where}: the page scrolls sideways ({width[0]} > {width[1]}): {culprits}")


def remove_containers(tmp: Path) -> None:
    """The throwaway Relay's boxes (WorkBoxes with their homes and image stores, ProviderBoxes): podman keeps them
    after it. They carry its mark (relay.containers.INSTANCE), which no other Relay's do. A new provider's trial
    Relay's (relay/creations.py: its folder in /tmp) too, when it was left running."""
    import hashlib
    import sqlite3
    try:
        made = [row[0] for row in sqlite3.connect(tmp / "data" / "relay" / "relay.db").execute(
            "SELECT id FROM repairs WHERE creates = 1")]
    except sqlite3.Error:
        made = []
    for creation in made:
        trial = Path(tempfile.gettempdir()) / f"relay-trial-{creation}"
        if trial.exists():
            remove_marked(hashlib.sha256(str((trial / "data").resolve()).encode()).hexdigest()[:8])
            shutil.rmtree(trial, ignore_errors=True)
            if trial.exists():
                subprocess.run(["podman", "unshare", "rm", "-rf", str(trial)], capture_output=True)
    remove_marked(hashlib.sha256(str((tmp / "data" / "relay").resolve()).encode()).hexdigest()[:8])


def remove_marked(mark: str) -> None:
    names = subprocess.run(["podman", "ps", "-a", "--filter", f"label=io.relay.instance={mark}", "--format",
                            "{{.Names}}"], capture_output=True, text=True).stdout.split()
    for name in names:
        subprocess.run(["podman", "rm", "-f", "-t", "1", name], capture_output=True)
        if not name.startswith("relay-box-"):
            subprocess.run(["podman", "volume", "rm", "-f", *(f"relay-{kind}-{name.removeprefix('relay-')}"
                                                              for kind in ("home", "containers"))], capture_output=True)


def own_parts():
    """A plugin of your own in plugins/ can bring its part of this walkthrough: a ui_check.py with `check(page, shot)`,
    and `prepare(data)` for what must be there before Relay starts (what it returns goes in Relay's environment)."""
    parts = {}
    for found in sorted(ROOT.glob("plugins/*/ui_check.py")):
        spec = importlib.util.spec_from_file_location(f"relay_ui_{found.parent.name}", found)
        parts[found.parent.name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(parts[found.parent.name])
    return parts


def main():
    from relay.providers import KINDS

    global SHOTS_DIR
    SHOTS_DIR = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="relay-shots-")
    Path(SHOTS_DIR).mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="relay-ui-"))
    (tmp / "ws" / "demo-app").mkdir(parents=True)
    WORKSPACE.append(tmp / "ws")
    repo = tmp / "ws" / "git-app"
    repo.mkdir()
    (repo / "main.py").write_text("print(1)\n-- note\n")  # a line diff headers could hide
    for args in (["init", "-q", "-b", "main"], ["add", "-A"],
                 ["-c", "user.name=T", "-c", "user.email=t@x", "commit", "-q", "-m", "start"]):
        subprocess.run(["git", *args], cwd=repo, check=True)
    chat_port, relay_port = free_port(), free_port()
    (tmp / "fakechat.py").write_text(FAKE_CHAT.replace("48130", str(chat_port)))
    (tmp / "fakeagent.py").write_text(FAKE_AGENT)
    env = {**os.environ, "RELAY_HOME": str(tmp / "data" / "relay"),
           "PYTHONPATH": f"{ROOT / 'src'}:{ROOT / 'vendor'}",
           "XDG_CACHE_HOME": str(tmp / "cache")}  # its tools scripts, not in the real ~/.cache
    (tmp / "data" / "relay" / "plugins").mkdir(parents=True)
    # (as their READMEs say)
    PARTS.update(own_parts())
    for name in (*PLUGINS, *PARTS):
        (tmp / "data" / "relay" / "plugins" / name).symlink_to(ROOT / "plugins" / name)
    for part in PARTS.values():
        if hasattr(part, "prepare"):
            env.update(part.prepare(tmp / "data" / "relay") or {})
    (tmp / "data" / "relay" / "settings.json").write_text(json.dumps(
        {"host": "127.0.0.1", "port": relay_port, "workspace_roots": [str(tmp / "ws")]}))
    providers = tmp / "data" / "relay" / "providers"
    providers.mkdir()
    (providers / "fake-chat.json").write_text(json.dumps({
        "id": "fake-chat", "name": "Fake Chat", "kind": "openai-compatible", "enabled": True,
        "options": {"base_url": f"http://127.0.0.1:{chat_port}/v1", "api_key": "sk-test-123"}}))
    (tmp / "fakeorchestrator.py").write_text(FAKE_ORCHESTRATOR)
    (providers / "fake-orchestrator.json").write_text(json.dumps({
        "id": "fake-orchestrator", "name": "Fake Orchestrator", "kind": "command", "enabled": True,
        "options": {"command": [sys.executable, str(tmp / "fakeorchestrator.py"), ""],
                    "resume_command": [sys.executable, str(tmp / "fakeorchestrator.py"), "{session}"]}}))
    (providers / "fake-agent.json").write_text(json.dumps({
        "id": "fake-agent", "name": "Fake Agent", "kind": "command", "enabled": True,
        "options": {"command": [sys.executable, str(tmp / "fakeagent.py"), ""],
                    "resume_command": [sys.executable, str(tmp / "fakeagent.py"), "{session}"]}}))
    (tmp / "fakebuilder.py").write_text(FAKE_BUILDER)
    (providers / "fake-builder.json").write_text(json.dumps({
        "id": "fake-builder", "name": "Fake Builder", "kind": "command", "enabled": True,
        "options": {"command": [sys.executable, str(tmp / "fakebuilder.py"), ""],
                    "resume_command": [sys.executable, str(tmp / "fakebuilder.py"), "{session}"]}}))
    (tmp / "fakelimited.py").write_text(FAKE_LIMITED)
    (providers / "fake-limited.json").write_text(json.dumps({
        "id": "fake-limited", "name": "Fake Limited", "kind": "command", "enabled": True, "managed": True,
        "options": {"command": [sys.executable, str(tmp / "fakelimited.py")]}}))  # (as if AgentOS added it)
    for name in ("claude-code", "opencode"):  # the defaults, as on a fresh install
        (providers / f"{name}.json").write_text(json.dumps({**KINDS[name].EXAMPLE, "enabled": True}))
    processes = [
        subprocess.Popen([sys.executable, str(tmp / "fakechat.py")]),
        subprocess.Popen([sys.executable, "-m", "relay", "serve"], env=env),
    ]
    problems = []
    try:
        wait_for(chat_port)
        wait_for(relay_port)
        start_catcher()
        with sync_playwright() as p:
            try:
                walkthrough(p, f"http://127.0.0.1:{relay_port}", SHOTS_DIR, problems)
            except Exception:
                if PAGES:
                    PAGES[-1].screenshot(path=f"{SHOTS_DIR}/failure.png")
                    print(f"Screenshot of the failure: {SHOTS_DIR}/failure.png")
                if problems:
                    print("Browser problems so far:", *problems, sep="\n  ")
                raise
        if MERGED and (repo / "main.py").read_text() != "print(2)\n":
            problems.append("the merged change is not in the repository")
    finally:
        for process in processes:
            process.terminate()
            process.wait()
        remove_containers(tmp)
        shutil.rmtree(tmp, ignore_errors=True)
        if tmp.exists():  # (files its containers wrote are podman's user's: only it removes them)
            subprocess.run(["podman", "unshare", "rm", "-rf", str(tmp)], capture_output=True)
    if problems:
        print("Browser problems:", *problems, sep="\n  ")
        return 1
    print(f"UI check passed; screenshots in {SHOTS_DIR}")
    return 0


if __name__ == "__main__":
    sys.path[:0] = [str(ROOT / "src"), str(ROOT / "vendor")]
    raise SystemExit(main())
