"""A stand-in for Relay's API, as much of it as the extension uses, to test the extension against.

Like Relay: an app password as Bearer, X-Relay-Request: 1 on what changes something, no CORS (a preflight is
refused), answers as server-sent events with ids (Last-Event-ID resumes), "caught-up" and "end" events.
Relay's chrome plugin (plugins/chrome) is loaded by Relay's own plugin host (relay.plugins), its routes at
/api/plugins/chrome/… and its tools run in the plugin's own threads, as in Relay.
GET /__log: the requests it had; /__reset: none had; GET /__page, /__page/form: pages to read and use.
POST /__tool {name, arguments, conversation_id, run_id, directory}: a plugin tool called as an agent calls it, its
answer {kind, text} once it has one; /__run_ended {run_id}; /__plugin {GONE, WAIT}: the plugin's times.

    python3 mock_relay.py 8421
"""

import json
import os
import shlex
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "vendor")]
# (the plugin's data: a folder of its own, RELAY_MOCK_HOME when given; never a real Relay's)
HOME = Path(os.environ.get("RELAY_MOCK_HOME") or tempfile.mkdtemp(prefix="relay-mock-home-"))
os.environ["RELAY_HOME"] = str(HOME)
from relay.plugins import Call, PluginError, Plugins, ToolCall  # noqa: E402

TOKEN = "relay_test"
REWRITTEN = "Hi! Is it still for sale? I can pick it up today for 35 euros."
PROVIDERS = [
    {"id": "claude-code", "name": "Claude Code", "enabled": True, "tools": True},
    {"id": "opencode", "name": "OpenCode", "enabled": True, "tools": True},
    {"id": "off", "name": "Switched off", "enabled": False},
]
MODELS = {"claude-code": [{"id": "", "name": "Default"}, {"id": "sonnet", "name": "Sonnet"}, {"id": "opus", "name": "Opus"}],
          "opencode": [{"id": "", "name": "Default"}, {"id": "opencode/big-pickle", "name": "Big Pickle"}]}
PAGE = """<!doctype html><html><head><title>The Test Page</title>
<meta name="description" content="A page about testing."></head>
<body><nav>Menu</nav><main><h1>Testing extensions</h1><p id="p">Chrome extensions are tested with Playwright.</p>
<p>""" + "Filler text. " * 60 + """</p></main></body></html>"""

FORM = """<!doctype html><html><head><title>The Form</title></head>
<body><main><h1>Messages</h1>
<div id="list"><p>Buyer: Is it still for sale?</p></div>
<form id="search"><label>Search <input name="q" placeholder="Search"></label></form>
<textarea id="message" aria-label="Message"></textarea>
<button id="send" type="button">Send</button>
<select id="size" aria-label="Size"><option>Small</option><option>Large</option></select>
<label><input type="checkbox" id="agree"> I agree</label>
<a href="/__page">The test page</a>
<p style="display:none">Hidden text</p>
<div id="far" style="margin-top:3000px">The bottom of it</div>
</main>
<script>
const say = (text) => { const p = document.createElement("p"); p.textContent = text; document.getElementById("list").append(p); };
document.getElementById("search").addEventListener("submit", (e) => { e.preventDefault(); say("Searched: " + e.target.q.value); });
document.getElementById("send").addEventListener("click", () => say("Sent: " + document.getElementById("message").value));
</script></body></html>"""

TRICKY = """<!doctype html><html><head><title>Tricky</title></head><body><main>
<div contenteditable="true" aria-label="Editor">old</div>
<div role="button" tabindex="0" onclick="this.textContent='pressed'">Press</div>
<div style="display:contents"><span>In display contents</span></div>
<a href="/item/1"><h3>A whole card as a link</h3><p>""" + "Lots of words about it. " * 8 + """</p></a>
<input type="password" value="secret" aria-label="Password">
</main></body></html>"""

lock = threading.Condition()
plugins: dict = {}
conversations: dict[str, dict] = {}
runs: dict[str, dict] = {}
log: list[dict] = []
uploads: dict[str, dict] = {}
script_runs: dict[str, dict] = {}

SCRIPTS = {
    "private/hello.py": '"""Say hello to whoever you name.\n\nGive names as arguments; without any, it greets the world.\n"""\n\nimport sys\n',
    "private/slow.py": '"""Counts slowly, to stop."""\n',
    "public/report.py": "print('a report')\n",
    "public/examples/fails.py": '"""Fails at once."""\nraise SystemExit(1)\n',
}


def now() -> float:
    return time.time()


def fresh_plugin() -> None:
    """Relay's chrome plugin, loaded anew (as at Relay's start) from a plugins folder with a link to it."""
    folder = Path(tempfile.mkdtemp(prefix="plugins-", dir=HOME))
    (folder / "chrome").symlink_to(ROOT / "plugins" / "chrome")
    host = Plugins(folder=folder)
    host.load()
    found = host.found["chrome"]
    if found.problem:
        raise SystemExit(f"the chrome plugin can't be loaded: {found.problem}")
    plugins.update(host=host, chrome=found.plugin, chrome_module=sys.modules["relay_plugins.chrome"])


def in_plugin(work, *args):
    """`work(*args)` in one of the plugin's threads, as Relay runs its routes and tools: its answer, or PluginError."""
    return plugins["chrome"].pool.submit(work, *args).result(timeout=700)


def make_run(conv: dict, text: str) -> dict:
    run = {"id": uuid.uuid4().hex, "conv": conv["id"], "events": [], "done": False, "stop": False, "permissions": {},
           "started": now()}
    runs[run["id"]] = run
    conv["running_run"] = run["id"]
    threading.Thread(target=answer, args=(run, conv, text), daemon=True).start()
    return run


def emit(run: dict, event: dict) -> None:
    with lock:
        run["events"].append(event)
        lock.notify_all()


def answer(run: dict, conv: dict, text: str) -> None:
    """The agent's answer: an echo with some Markdown, a tool call, and a permission when asked for one."""
    emit(run, {"type": "step", "model": conv["model"] or "default-model"})
    emit(run, {"type": "reasoning", "delta": "Let me think."})
    time.sleep(0.2)
    status = "succeeded"
    if "permission" in text:
        emit(run, {"type": "permission", "id": "p1", "action": "Run a command", "resources": ["rm -rf /tmp/x"],
                   "decision": None, "options": [{"id": "allow", "label": "Allow once"}, {"id": "deny", "label": "Deny"}]})
        with lock:
            lock.wait_for(lambda: "p1" in run["permissions"] or run["stop"], timeout=30)
            decision = run["permissions"].get("p1", "timeout")
        emit(run, {"type": "permission", "id": "p1", "action": "Run a command", "resources": ["rm -rf /tmp/x"],
                   "decision": "once" if decision == "allow" else "denied"})
    emit(run, {"type": "tool_call", "id": "t1", "name": "WebSearch", "input": {"query": "playwright extensions"}})
    time.sleep(0.2)
    emit(run, {"type": "tool_result", "id": "t1", "ok": True, "output": "3 results"})
    first = text.split("\n\nFrom my browser:")[0]
    reply = (f"You asked: **{first}**\n\nIt had {len(text)} characters"
             f"{' and a page' if '<page ' in text else ''}.\n\n```python\nprint('hi')\n```\n\n- one\n- two\n")
    if "slow" in text:
        reply += " ".join(f"word{i}" for i in range(40))
    if "Answer with only the new text" in text:  # (a rewrite, as the menu's Rewrite with Relay asks it)
        reply = REWRITTEN
    for i in range(0, len(reply), 7):
        with lock:
            if run["stop"]:
                status = "interrupted"
                break
        emit(run, {"type": "text", "delta": reply[i:i + 7]})
        time.sleep(0.15 if "slow" in text else 0.01)
    emit(run, {"type": "usage", "input_tokens": 10, "output_tokens": 20, "cost": 0.0123})
    content = "".join(e["delta"] for e in run["events"] if e["type"] == "text")
    with lock:
        conv["messages"].append({"id": len(conv["messages"]) + 1, "role": "assistant", "content": content,
                                 "events": list(run["events"]) + [{"type": "done", "status": status}],
                                 "status": status, "run_id": run["id"], "run_model": conv["model"] or "default-model",
                                 "run_provider": conv["provider_id"], "run_started": run["started"], "run_finished": now()})
        if not conv.get("title"):
            conv["title"] = first[:40]
        conv["running_run"] = None
        conv["updated_at"] = now()
        # The next one goes as this one ends, before its stream does; after a stopped or failed one, the queue waits.
        if conv["queued"] and status == "succeeded":
            send_queued(conv, conv["queued"][0]["id"])
    if "lingers" in text:
        time.sleep(1.5)  # (the run is over, its stream doesn't say so yet)
    emit(run, {"type": "done", "status": status})
    with lock:
        run["done"] = True
        lock.notify_all()


def send_queued(conv: dict, queued_id: str) -> dict:
    """A queued message sent now (under the lock)."""
    asked = next(q for q in conv["queued"] if q["id"] == queued_id)
    conv["queued"].remove(asked)
    conv["messages"].append({"id": len(conv["messages"]) + 1, "role": "user", "content": asked["content"],
                             "attachments": []})
    return make_run(conv, asked["content"])


def script(run: dict) -> None:
    """A script's run: what it prints as it goes, a chat it starts, how it ends."""
    def say(text: str) -> None:
        with lock:
            run["output"] += text
            lock.notify_all()

    name, args = run["script"], shlex.split(run["args"])
    status, code = "succeeded", 0
    if name == "private/hello.py":
        for who in args or ["world"]:
            say(f"Hello, {who}!\n")
            time.sleep(0.1)
        made = {"id": uuid.uuid4().hex[:12], "title": "Started by hello.py", "provider_id": "claude-code", "model": "",
                "mode": "chat", "messages": [], "running_run": None, "queued": [], "updated_at": now()}
        with lock:
            conversations[made["id"]] = made
            run["conversations"].append({"id": made["id"], "title": made["title"]})
            lock.notify_all()
        say(f"args: {args}\n")
    elif name == "private/slow.py":
        for i in range(1, 60):
            with lock:
                if run["stop"]:
                    status, code = "stopped", -15
                    break
            say(f"{i}\n")
            time.sleep(0.2)
    elif name == "public/examples/fails.py":
        say("Traceback: it failed\n")
        status, code = "failed", 1
    else:
        say("a report\n")
    with lock:
        run.update(status=status, exit_code=code, finished=now())
        lock.notify_all()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # (quiet)
        pass

    def send_json(self, status: int, data) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def signed_in(self) -> bool:
        return self.headers.get("Authorization") == f"Bearer {TOKEN}"

    def do_OPTIONS(self):
        self.send_json(403, {"error": "Missing request header"})

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_PATCH(self):
        self.route("PATCH")

    def do_DELETE(self):
        self.route("DELETE")

    def route(self, method: str) -> None:
        url = urlsplit(self.path)
        path, query = url.path, parse_qs(url.query)
        raw = self.body() if method != "GET" else b""
        entry = {"method": method, "path": self.path, "origin": self.headers.get("Origin"),
                 "auth": self.headers.get("Authorization"), "x_relay": self.headers.get("X-Relay-Request"),
                 "last_event_id": self.headers.get("Last-Event-ID"), "type": self.headers.get("Content-Type"),
                 "filename": self.headers.get("X-Filename"), "size": len(raw)}
        if path == "/__log":
            return self.send_json(200, log)
        if path == "/__reset":
            with lock:
                conversations.clear(), runs.clear(), log.clear(), uploads.clear(), script_runs.clear()
                fresh_plugin()
                plugins["off"] = False
            return self.send_json(200, {"reset": True})
        if path == "/__tool":
            data = json.loads(raw)
            call = ToolCall(data.get("conversation_id", "c1"), data.get("run_id", "r1"), 1, data.get("directory", "/tmp"),
                            data.get("arguments", {}))
            try:
                return self.send_json(200, {"kind": "ok", "text": in_plugin(plugins["host"].call_tool, data["name"], call)})
            except PluginError as error:
                return self.send_json(200, {"kind": "error", "text": str(error)})
        if path == "/__run_ended":
            plugins["host"].run_ended({"conversation_id": "c1", "run_id": json.loads(raw)["run_id"], "user_id": 1,
                                       "provider_id": "claude-code", "model": "", "status": "interrupted", "error": None,
                                       "subagent": False, "temporary": False, "started_by": None})
            return self.send_json(200, {"ok": True})
        if path == "/__plugin":
            for key, value in json.loads(raw).items():
                if key == "off":
                    plugins["off"] = value
                else:
                    setattr(plugins["chrome_module"], key, value)
            return self.send_json(200, {"ok": True})
        if path in ("/__page", "/__page/form", "/__page/tricky"):
            body = {"/__page": PAGE, "/__page/form": FORM, "/__page/tricky": TRICKY}[path].encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        log.append(entry)
        if path == "/api/state":
            user = {"username": "alex"} if self.signed_in() else None
            return self.send_json(200, {"user": user, "login_required": True, "version": "4.2.0"})
        if self.headers.get("Authorization", "").startswith("Bearer ") and not self.signed_in():
            return self.send_json(401, {"error": "That app password isn't Relay's (any more): see Settings → Account"})
        if not self.signed_in():
            return self.send_json(401, {"error": "Not signed in"})
        if method != "GET" and self.headers.get("X-Relay-Request") != "1":
            return self.send_json(403, {"error": "Missing request header"})
        data = json.loads(raw or b"null") if (self.headers.get("Content-Type") or "").startswith("application/json") else None
        if raw and data is not None:
            entry["json"] = data
        parts = [unquote(part) for part in path.strip("/").split("/")]

        if path == "/api/plugins" and method == "GET":
            chrome = plugins["chrome"]
            return self.send_json(200, {"folder": "/home/alex/.local/share/relay/plugins", "plugins": [
                {"name": "chrome", "label": chrome.label, "version": chrome.version, "enabled": not plugins.get("off"),
                 "loaded": True, "tools": sorted(t.name for t in chrome.tools)},
                {"name": "notes", "label": "Notes", "version": "1.0", "enabled": True, "loaded": True, "tools": []}]})
        if parts[:3] == ["api", "plugins", "chrome"] and not plugins.get("off"):
            call = Call(method, "/".join(parts[3:]), {}, {k: v[-1] for k, v in query.items()}, data,
                        {"id": 1, "username": "alex"})
            try:
                return self.send_json(200, in_plugin(plugins["host"].call, "chrome", call))
            except PluginError as error:
                return self.send_json(error.status, {"error": str(error)})
        if parts[:2] == ["api", "plugins"]:
            return self.send_json(404, {"error": "No such plugin"})
        if parts[:3] == ["api", "scripts", "runs"] and len(parts) >= 4:
            run = script_runs.get(parts[3])
            if not run:
                return self.send_json(404, {"error": "No such run"})
            if len(parts) == 4:
                with lock:
                    return self.send_json(200, {k: v for k, v in run.items() if k != "stop"})
            if parts[4] == "output":
                return self.script_output(run, int((query.get("after") or ["0"])[0]))
            if parts[4] == "stop" and method == "POST":
                with lock:
                    going = run["status"] == "running"
                    run["stop"] = True
                return self.send_json(200, {"stopped": going})
        if path == "/api/scripts" and method == "GET":
            return self.send_json(200, {"scripts": [
                {"name": name, "size": len(text), "modified": now() - 3600, "private": name.startswith("private/"),
                 "example": name.startswith("public/examples/"),
                 "running": any(r["script"] == name and r["status"] == "running" for r in script_runs.values())}
                for name, text in SCRIPTS.items()]})
        if parts[:2] == ["api", "scripts"] and len(parts) >= 3:
            name = parts[2]
            if name not in SCRIPTS:
                return self.send_json(404, {"error": "No such script"})
            if len(parts) == 3 and method == "GET":
                its = sorted((r for r in script_runs.values() if r["script"] == name), key=lambda r: r["started"],
                             reverse=True)
                return self.send_json(200, {"name": name, "text": SCRIPTS[name], "tasks": [], "runs": [
                    {k: r[k] for k in ("id", "script", "args", "started", "finished", "status", "exit_code")} for r in its]})
            if len(parts) == 4 and parts[3] == "run" and method == "POST":
                run = {"id": uuid.uuid4().hex, "script": name, "args": data.get("args", ""), "started": now(),
                       "finished": None, "status": "running", "exit_code": None, "output": "", "cut": 0,
                       "conversations": [], "stop": False}
                script_runs[run["id"]] = run
                threading.Thread(target=script, args=(run,), daemon=True).start()
                return self.send_json(200, {"run": run["id"]})

        if path == "/api/providers":
            return self.send_json(200, {"providers": PROVIDERS})
        if parts[:2] == ["api", "providers"] and len(parts) == 4 and parts[3] == "models":
            return self.send_json(200, {"models": MODELS.get(parts[2], [])})
        if path == "/api/uploads" and method == "POST":
            made = uuid.uuid4().hex
            uploads[made] = {"name": self.headers.get("X-Filename"), "type": self.headers.get("Content-Type"),
                             "size": len(raw)}
            return self.send_json(200, {"id": made})
        if path == "/api/conversations" and method == "GET":
            q = (query.get("q") or [""])[0].lower()
            rows = sorted(conversations.values(), key=lambda c: c["updated_at"], reverse=True)
            rows = [c for c in rows if q in (c["title"] or "").lower()] if q else rows
            return self.send_json(200, [{k: c[k] for k in ("id", "title", "provider_id", "model", "mode", "updated_at",
                                                          "running_run")} for c in rows])
        if path == "/api/conversations" and method == "POST":
            made = {"id": uuid.uuid4().hex[:12], "title": data.get("title"), "provider_id": data["provider_id"],
                    "model": data.get("model") or "", "mode": data.get("mode", "chat"), "messages": [],
                    "running_run": None, "queued": [], "updated_at": now()}
            conversations[made["id"]] = made
            return self.send_json(200, made)
        if parts[:2] == ["api", "conversations"] and len(parts) >= 3:
            conv = conversations.get(parts[2])
            if not conv:
                return self.send_json(404, {"error": "No such conversation"})
            if len(parts) == 3 and method == "GET":
                with lock:
                    return self.send_json(200, conv)
            if len(parts) == 3 and method == "PATCH":
                for key in ("title", "provider_id", "model", "effort"):
                    if key in data:
                        conv[key] = data[key] if data[key] is not None else ""
                return self.send_json(200, conv)
            if len(parts) == 4 and parts[3] == "messages" and method == "POST":
                if conv["running_run"]:
                    return self.send_json(409, {"error": "An agent is working in it already"})
                with lock:
                    conv["messages"].append({"id": len(conv["messages"]) + 1, "role": "user", "content": data["text"],
                                             "attachments": [{"id": a, **uploads.get(a, {})} for a in data.get("attachments", [])]})
                    run = make_run(conv, data["text"])
                return self.send_json(200, {"run_id": run["id"]})
            if len(parts) == 4 and parts[3] == "queue" and method == "POST":
                with lock:
                    made = {"id": uuid.uuid4().hex[:8], "content": data["text"], "attachments": data.get("attachments", [])}
                    conv["queued"].append(made)
                    if not conv["running_run"] and len(conv["queued"]) == 1:
                        # (the answer had just ended, nothing waits: it goes right away)
                        entry["right_away"] = True
                        return self.send_json(200, {"run_id": send_queued(conv, made["id"])["id"]})
                return self.send_json(200, {"queued": made["id"]})
            if len(parts) == 5 and parts[3] == "queue":
                with lock:
                    if not any(q["id"] == parts[4] for q in conv["queued"]):
                        return self.send_json(404, {"error": "Not queued (any more)"})
                    if method == "DELETE":
                        conv["queued"] = [q for q in conv["queued"] if q["id"] != parts[4]]
                        return self.send_json(200, {"ok": True})
            if len(parts) == 6 and parts[3] == "queue" and parts[5] == "send" and method == "POST":
                with lock:
                    if conv["running_run"]:
                        return self.send_json(409, {"error": "An agent is working in it already"})
                    if not any(q["id"] == parts[4] for q in conv["queued"]):
                        return self.send_json(404, {"error": "Not queued (any more)"})
                    return self.send_json(200, {"run_id": send_queued(conv, parts[4])["id"]})
        if parts[:2] == ["api", "runs"] and len(parts) == 4:
            run = runs.get(parts[2])
            if not run:
                return self.send_json(404, {"error": "No such run"})
            if parts[3] == "events" and method == "GET":
                return self.stream(run)
            if parts[3] == "stop" and method == "POST":
                with lock:
                    run["stop"] = True
                    lock.notify_all()
                return self.send_json(200, {"stopped": True})
            if parts[3] == "permission" and method == "POST":
                with lock:
                    run["permissions"][data["id"]] = data["decision"]
                    lock.notify_all()
                return self.send_json(200, {"ok": True})
        return self.send_json(404, {"error": f"Not found: {method} {path}"})

    def script_output(self, run: dict, after: int) -> None:
        """A script run's output from `after` characters on, as Relay's stream has it: text, conversations, end."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        sent, told = after, 0
        try:
            while True:
                with lock:
                    lock.wait_for(lambda sent=sent, told=told: len(run["output"]) > sent or run["status"] != "running"
                                  or len(run["conversations"]) != told, timeout=15)
                    text, made = run["output"][sent:], list(run["conversations"])
                    ended = run["status"] != "running"
                if text:
                    sent += len(text)
                    self.wfile.write(f"data: {json.dumps({'text': text})}\n\n".encode())
                if len(made) != told:
                    told = len(made)
                    self.wfile.write(f"event: conversations\ndata: {json.dumps(made)}\n\n".encode())
                if not text:
                    self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
                if ended and sent >= len(run["output"]):
                    end = {k: run[k] for k in ("status", "exit_code", "finished")}
                    self.wfile.write(f"event: end\ndata: {json.dumps(end)}\n\n".encode())
                    self.wfile.flush()
                    return
        except (BrokenPipeError, ConnectionResetError):
            return

    def stream(self, run: dict) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        sent = int(self.headers.get("Last-Event-ID") or 0)
        caught_up = False
        try:
            while True:
                with lock:
                    lock.wait_for(lambda sent=sent: len(run["events"]) > sent or run["done"], timeout=15)
                    fresh = run["events"][sent:]
                    done = run["done"] and len(run["events"]) == sent + len(fresh)
                for event in fresh:
                    sent += 1
                    self.wfile.write(f"id: {sent}\ndata: {json.dumps(event)}\n\n".encode())
                if not caught_up:
                    caught_up = True
                    self.wfile.write(b"event: caught-up\ndata: {}\n\n")
                if not fresh:
                    self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
                if done:
                    self.wfile.write(b"event: end\ndata: {}\n\n")
                    self.wfile.flush()
                    return
        except (BrokenPipeError, ConnectionResetError):
            return


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8421
    fresh_plugin()
    print(f"mock Relay at http://127.0.0.1:{port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
