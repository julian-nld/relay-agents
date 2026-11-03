import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from opencode_manager import cli, scripting, state
from opencode_manager.bridge.http import Client, HttpError
from opencode_manager.bridge.opencode_backend import OpencodeBackend, Translator

SID = "ses_1"


def raw(kind, **data):
    return {"type": kind, "data": {"sessionID": SID, **data}}


# Translator


def test_translates_a_run_with_a_tool_call():
    t = Translator(SID, billing={"p/m": "api"}.get)
    events = [
        raw("session.step.started", agent="build", model={"providerID": "p", "id": "m"}),
        raw("session.tool.input.started", id="call_1", name="shell"),
        raw("session.tool.called", id="call_1", input={"command": "ls"}),
        raw("session.tool.success", id="call_1", content=[{"type": "text", "text": "a\n"}]),
        raw("session.reasoning.delta", delta="hmm"),
        raw("session.text.delta", delta="Hi"),
        raw("session.usage.updated", tokens={"input": 1}, cost=0.5),
        raw("session.step.ended", finish="stop"),  # ignored
        raw("session.execution.succeeded"),
    ]
    out = [e for ev in events for e in t.translate(ev)]
    assert out == [
        {"type": "step", "agent": "build", "model": "p/m"},
        {"type": "tool_call", "id": "call_1", "name": "shell", "input": {"command": "ls"}},
        {"type": "tool_result", "id": "call_1", "name": "shell", "ok": True, "output": "a\n"},
        {"type": "reasoning", "delta": "hmm"},
        {"type": "text", "delta": "Hi"},
        {"type": "usage", "tokens": {"input": 1}, "cost": 0.5, "billing": "api"},
        {"type": "done", "status": "succeeded"},
    ]
    assert t.done


def test_failures_and_other_sessions():
    t = Translator(SID)
    assert not t.concerns({"type": "session.text.delta", "data": {"sessionID": "other"}})
    assert not t.concerns({"type": "server.connected"})
    assert t.translate(raw("session.tool.failed", id="x", error={"message": "boom"})) == [
        {"type": "tool_result", "id": "x", "name": None, "ok": False, "output": "boom"}
    ]
    assert t.translate(raw("session.error", message="bad")) == [{"type": "error", "message": "bad"}]
    assert t.translate(raw("session.execution.interrupted")) == [
        {"type": "done", "status": "interrupted"}
    ]


# HTTP client against a real local server


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.seen_auth = self.headers.get("Authorization")
        if self.path == "/missing":
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"message": "nope"}')
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(b'data: {"type": "a"}\n\n: comment\n\ndata: {"type":\ndata: "b"}\n\n')

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({"echo": body}).encode())


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


def test_client_json_sse_auth_and_errors(server):
    client = Client(f"http://127.0.0.1:{server.server_address[1]}", "opencode", "pw")
    assert client.request("POST", "/x", {"a": 1}) == {"echo": {"a": 1}}
    assert list(client.events("/events")) == [{"type": "a"}, {"type": "b"}]
    assert server.seen_auth == "Basic " + base64.b64encode(b"opencode:pw").decode()
    with pytest.raises(HttpError) as exc:
        client.request("GET", "/missing")
    assert exc.value.status == 404


# Backend logic with a fake client


class FakeClient:
    def __init__(self, stream, gone=()):
        self.stream = stream
        self.requests = []
        self.gone = set(gone)  # permission requests opencode withdrew

    def events(self, path, stopped=None):
        yield from self.stream

    def request(self, method, path, body=None):
        self.requests.append((method, path, body))
        if any(f"/permission/{g}/" in path for g in self.gone):
            raise HttpError("Permission request not found", 404)
        if path == f"/api/session/{SID}/message":  # newest first
            return {"data": [{"id": "msg_3", "type": "assistant"}, {"id": "msg_2", "type": "user"},
                             {"id": "msg_1", "type": "user"}]}
        return {"data": {"id": SID}}


def run_backend(stream, judge=None):
    client = FakeClient(stream)
    return list(OpencodeBackend(client, judge=judge).run(SID, "hi")), client.requests


def test_prompt_is_sent_after_subscribing_and_other_sessions_are_ignored():
    stream = [
        {"type": "server.connected", "data": {}},
        {"type": "session.text.delta", "data": {"sessionID": "other", "delta": "x"}},
        raw("session.text.delta", delta="ok"),
        raw("session.execution.succeeded"),
        raw("session.text.delta", delta="after done"),
    ]
    events, requests = run_backend(stream)
    assert requests == [("POST", f"/api/session/{SID}/prompt", {"text": "hi"}),
                        ("GET", f"/api/session/{SID}/message", None)]
    # The turn's own message id comes last before "done", for forking before it later.
    assert events[1:] == [{"type": "turn", "id": "msg_2"}, {"type": "done", "status": "succeeded"}]
    assert events[0]["type"] == "text"


def test_relay_answers_permission_requests_itself():
    ask = raw("permission.asked", id="per_1", action="shell", resources=["ls"])
    edit = raw("permission.asked", id="per_2", action="edit", resources=["../elsewhere.py"])
    stream = [{"type": "server.connected"}, ask, edit, raw("session.execution.succeeded")]
    events, requests = run_backend(stream)  # without a judge: everything the config doesn't deny
    assert ("POST", f"/api/session/{SID}/permission/per_1/reply", {"decision": "once"}) in requests
    assert events[0] == {"type": "permission", "id": "per_1", "action": "shell",
                         "resources": ["ls"], "decision": "once"}

    events, requests = run_backend(stream, judge=lambda action, resources: "Not there." if action == "edit" else None)
    replies = {path.rsplit("/", 2)[1]: body for _, path, body in requests if "reply" in path}
    assert replies == {"per_1": {"decision": "once"}, "per_2": {"decision": "reject", "message": "Not there."}}
    assert [(e["decision"], e.get("outcome")) for e in events if e["type"] == "permission"] == [
        ("once", None), ("denied", "Not there.")]


def test_without_auto_approve_requests_are_left_open():
    ask = raw("permission.asked", id="per_1", action="shell", resources=["ls"])
    client = FakeClient([{"type": "server.connected"}, ask, raw("session.execution.succeeded")])
    events = list(OpencodeBackend(client, auto_approve=False).run(SID, "hi"))
    assert events[0]["decision"] is None and not any("reply" in path for _, path, _ in client.requests)


def test_stream_ending_early_is_a_failure():
    events, _ = run_backend([{"type": "server.connected"}, raw("session.text.delta", delta="a")])
    assert events[-2]["type"] == "error"
    assert events[-1] == {"type": "done", "status": "failed"}


def test_question_forms_are_not_auto_approved_and_can_be_answered():
    fields = [{"key": "0", "type": "multiselect", "title": "Main use?", "custom": True,
               "options": [{"value": "Gaming", "label": "Gaming", "description": "Games"}]}]
    form = {"id": "frm_1", "sessionID": SID, "title": "Questions", "fields": fields}
    client = FakeClient([{"type": "server.connected"}, {"type": "form.created", "data": {"form": form}},
                         raw("form.replied", id="frm_1", answer={"0": ["Gaming", "AI"]}),
                         raw("session.execution.succeeded")])
    backend = OpencodeBackend(client)
    stream = backend.run(SID, "hi")
    assert next(stream) == {"type": "form", "id": "frm_1", "title": "Questions", "fields": fields,
                            "status": "pending"}
    assert not any("/reply" in path for _, path, _ in client.requests)
    backend.answer_form("frm_1", {"0": ["Gaming", "AI"]})
    assert client.requests[-1] == ("POST", f"/api/session/{SID}/form/frm_1/reply",
                                   {"answer": {"0": ["Gaming", "AI"]}})
    assert next(stream) == {"type": "form", "id": "frm_1", "status": "answered",
                            "answer": {"0": ["Gaming", "AI"]}}
    list(stream)
    with pytest.raises(HttpError, match="no longer waiting"):
        backend.answer_form("frm_1", {})


def test_forms_from_native_subagents_use_the_owning_session():
    child = "ses_child"
    form = {"id": "frm_child", "sessionID": child, "title": "Question", "fields": []}
    unrelated = {**form, "id": "frm_other", "sessionID": "ses_other"}
    client = FakeClient([{"type": "server.connected"},
                         {"type": "form.created", "data": {"form": unrelated}},
                         {"type": "form.created", "data": {"form": form}},
                         {"type": "form.cancelled", "data": {"sessionID": child, "id": "frm_child"}},
                         raw("session.execution.succeeded")])
    backend = OpencodeBackend(client)
    backend._parents.update({child: SID, "ses_other": None})
    stream = backend.run(SID, "hi")
    assert next(stream)["id"] == "frm_child"
    backend.answer_form("frm_child", None)
    assert client.requests[-1] == ("DELETE", f"/api/session/{child}/form/frm_child", None)
    assert next(stream) == {"type": "form", "id": "frm_child", "status": "cancelled"}
    list(stream)


class CatalogClient(FakeClient):
    """Answers opencode's session, provider, integration and model lookups."""

    def request(self, method, path, body=None):
        self.requests.append((method, path, body))
        if any(f"/permission/{g}/" in path for g in self.gone):
            raise HttpError("Permission request not found", 404)
        if path == f"/api/session/{SID}":
            return {"data": {"id": SID, "location": {"directory": "/home/me/p"}}}
        assert path.endswith("?directory=/home/me/p")
        if path.startswith("/api/provider"):
            return {"data": [{"id": "openai", "integrationID": "openai", "settings": {}},
                             {"id": "ollama", "settings": {"baseURL": "http://127.0.0.1:11434/v1"}}]}
        if path.startswith("/api/model/default"):
            return {"data": {"providerID": "ollama", "id": "qwen"}}
        if path.startswith("/api/integration"):
            return {"data": [{"id": "openai", "connections": [{"method": "oauth"}]}]}
        if path.startswith("/api/model"):
            return {"data": [{"providerID": "openai", "id": "gpt", "cost": []},
                             {"providerID": "ollama", "id": "qwen", "cost": []}]}
        return {"data": {}}


def test_billing_is_looked_up_once_per_model():
    client = CatalogClient([])
    backend = OpencodeBackend(client)
    assert backend.billing(SID, "openai/gpt") == "subscription"
    assert backend.billing(SID, "ollama/qwen") == "free"
    assert backend.billing(SID, "gone/model") is None  # a provider opencode no longer has
    lookups = len(client.requests)
    assert backend.billing(SID, "openai/gpt") == "subscription"
    assert len(client.requests) == lookups


def test_what_opencode_says_of_its_models():
    backend = OpencodeBackend(CatalogClient([]))
    found = backend.models_info(Path("/home/me/p"))
    assert found["default"] == "ollama/qwen"
    assert found["models"]["ollama/qwen"] == {"billing": "free", "base_url": "http://127.0.0.1:11434/v1"}
    assert backend.model_info(Path("/home/me/p"), None)["billing"] == "free"  # the default model
    assert backend.model_info(Path("/home/me/p"), "gone/model") == {"billing": None, "base_url": None}


# CLI


class FakeBackend:
    name = "opencode"

    def __init__(self, events, interrupt_after=None):
        self._events = events
        self.interrupt_after = interrupt_after
        self.interrupted = False

    def run(self, session_id, text):
        for i, ev in enumerate(self._events):
            if i == self.interrupt_after:
                raise KeyboardInterrupt
            yield ev

    def interrupt(self, session_id):
        self.interrupted = True


def fake_stream(monkeypatch, backend):
    first = {"type": "session", "backend": "opencode", "session": SID, "profile": "default",
             "directory": "/p", "new": True}
    run = scripting.StreamRun(backend, SID, first, "hi")
    monkeypatch.setattr(scripting, "start_stream", lambda *a, **kw: run)


def test_cli_stream_json_and_exit_codes(monkeypatch, capsys):
    fake_stream(monkeypatch, FakeBackend([{"type": "text", "delta": "Hi"},
                                          {"type": "done", "status": "succeeded"}]))
    assert cli.main(["stream", "hello"]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [e["type"] for e in lines] == ["session", "text", "done"]

    fake_stream(monkeypatch, FakeBackend([{"type": "done", "status": "failed"}]))
    assert cli.main(["stream", "hello"]) == cli.EXIT_ERROR


def test_cli_stream_text_mode(monkeypatch, capsys):
    fake_stream(monkeypatch, FakeBackend([
        {"type": "tool_call", "id": "1", "name": "shell", "input": {"command": "ls"}},
        {"type": "text", "delta": "Hel"}, {"type": "text", "delta": "lo"},
        {"type": "done", "status": "succeeded"},
    ]))
    assert cli.main(["stream", "--text", "hello"]) == 0
    out, err = capsys.readouterr()
    assert out == "Hello\n"
    assert "▸ shell" in err


def test_cli_stream_ctrl_c_interrupts_the_session(monkeypatch, capsys):
    backend = FakeBackend([{"type": "text", "delta": "a"}, {"type": "text", "delta": "b"}],
                          interrupt_after=1)
    fake_stream(monkeypatch, backend)
    assert cli.main(["stream", "hello"]) == cli.EXIT_INTERRUPTED
    assert backend.interrupted
    assert json.loads(capsys.readouterr().out.splitlines()[-1]) == {
        "type": "done", "status": "interrupted"
    }


def test_cli_stream_reads_prompt_from_stdin(monkeypatch, capsys):
    seen = {}

    def start(settings, store, text, **kw):
        seen["text"] = text
        return scripting.StreamRun(FakeBackend([{"type": "done", "status": "succeeded"}]),
                                   SID, {"type": "session"}, text)

    monkeypatch.setattr(scripting, "start_stream", start)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("from stdin\n"))
    assert cli.main(["stream"]) == 0
    assert seen["text"] == "from stdin\n"


def test_last_session_is_remembered_per_backend_and_profile(tmp_path):
    file = tmp_path / "state.json"
    state.remember_session("opencode:work", "ses_a", file)
    state.remember_session("opencode:home", "ses_b", file)
    state.remember_profile("work", file)
    assert state.last_session("opencode:work", file) == "ses_a"
    assert state.last_session("opencode:home", file) == "ses_b"
    assert state.last_session("claude-code:work", file) is None


def test_session_directories_are_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "MAX_SESSION_DIRECTORIES", 3)
    file = tmp_path / "state.json"
    for i in range(5):
        state.remember_session("claude-code", f"s{i}", file, directory=tmp_path)
    state.remember_session("claude-code", "s2", file, directory=tmp_path)  # refreshed
    assert list(state.load(file).session_directories) == [
        "claude-code:s3", "claude-code:s4", "claude-code:s2"]


def test_subagent_permissions_are_approved():
    child = "ses_child"
    stream = [
        {"type": "server.connected"},
        {"type": "permission.asked", "data": {"sessionID": child, "id": "per_9", "action": "shell",
                                               "resources": ["ls"]}},
        {"type": "permission.asked", "data": {"sessionID": "unrelated", "id": "per_x",
                                               "action": "shell", "resources": ["rm"]}},
        raw("session.execution.succeeded"),
    ]

    class Client(FakeClient):
        def request(self, method, path, body=None):
            self.requests.append((method, path, body))
            parents = {f"/api/session/{child}": SID, "/api/session/unrelated": None}
            if method == "GET" and path in parents:
                return {"data": {"id": path.rsplit("/", 1)[1], "parentID": parents[path]}}
            return {"data": {"id": SID}}

    client = Client(stream)
    events = list(OpencodeBackend(client).run(SID, "hi"))
    replies = [path for _, path, _ in client.requests if path.endswith("/reply")]
    assert replies == [f"/api/session/{child}/permission/per_9/reply"]
    assert events[0]["type"] == "permission" and events[0]["id"] == "per_9"


def test_malformed_events_are_ignored():
    events, _ = run_backend([{"type": "server.connected"}, {"type": "x", "data": None},
                             "not even a dict", raw("session.execution.succeeded")])
    assert [e["type"] for e in events] == ["turn", "done"]


def test_claude_session_is_remembered_only_once_started(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_MANAGER_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(scripting.shutil, "which", lambda name, **kw: "/usr/bin/claude")
    settings = __import__("opencode_manager.settings", fromlist=["Settings"]).Settings()
    run = scripting.start_stream(settings, None, "hi", backend="claude-code", directory=tmp_path)
    run.backend = FakeBackend([{"type": "error", "message": "not logged in"},
                               {"type": "done", "status": "failed"}])
    list(run.events())
    assert state.last_session("claude-code") is None  # nothing to --continue

    run = scripting.start_stream(settings, None, "hi", backend="claude-code", directory=tmp_path)
    run.backend = FakeBackend([{"type": "step", "agent": None, "model": "m"},
                               {"type": "done", "status": "succeeded"}])
    list(run.events())
    assert state.last_session("claude-code") == run.session_id

    elsewhere = tmp_path / "other"
    elsewhere.mkdir()
    resumed = scripting.start_stream(settings, None, "again", backend="claude-code",
                                     continue_last=True, directory=elsewhere)
    assert resumed.backend.directory == tmp_path  # the session's own directory wins


def test_backend_failure_still_ends_with_done(tmp_path):
    class Exploding(FakeBackend):
        def run(self, session_id, text):
            yield {"type": "text", "delta": "a"}
            raise OSError("pipe broke")

    run = scripting.StreamRun(Exploding([]), SID, {"type": "session"}, "hi")
    events = list(run.events())
    assert events[-2] == {"type": "error", "message": "pipe broke"}
    assert events[-1] == {"type": "done", "status": "failed"}


def test_a_withdrawn_request_doesnt_end_the_run():
    """Rejecting one of two parallel requests makes opencode withdraw the other."""
    cat = raw("permission.asked", id="per_1", action="edit", resources=["../main.py"])
    tool = raw("permission.asked", id="per_2", action="shell", resources=["/tools/abc/relay agents"])
    client = FakeClient([{"type": "server.connected"}, cat, tool, raw("session.execution.succeeded")], gone={"per_2"})
    backend = OpencodeBackend(client, judge=lambda action, resources: "No." if action == "edit" else None)
    events = list(backend.run(SID, "hi"))
    notes = [e for e in events if e["type"] == "permission"]
    assert [n["decision"] for n in notes] == ["denied", "denied"] and notes[1]["outcome"] == "withdrawn by opencode"
    assert events[-1] == {"type": "done", "status": "succeeded"}


def test_a_failed_execution_says_why():
    translator = Translator(SID)
    failed = raw("session.execution.failed", error={"type": "provider.no-route",
                                                     "message": "Model unavailable: ollama/qwen"})
    assert translator.translate(failed) == [{"type": "error", "message": "Model unavailable: ollama/qwen"},
                                             {"type": "done", "status": "failed"}]
    unexplained = Translator(SID).translate(raw("session.execution.failed"))
    assert unexplained[0]["type"] == "error" and "without saying why" in unexplained[0]["message"]
    assert unexplained[-1] == {"type": "done", "status": "failed"}


class SilentHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        if self.server.chunked:
            self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        data = b'data: {"type": "server.connected"}\n\n'
        self.wfile.write((f"{len(data):x}\r\n".encode() + data + b"\r\n") if self.server.chunked else data)
        self.wfile.flush()
        self.server.release.wait(5)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path.endswith("/prompt"):
            self.server.prompted.set()
        if self.path.endswith("/interrupt") and self.server.reject_interrupt:
            self.send_response(500)
        else:
            self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{}')


@pytest.mark.parametrize("reject_interrupt", [False, True])
@pytest.mark.parametrize("chunked", [False, True])
def test_interrupt_finishes_a_silent_stream_even_when_the_request_fails(reject_interrupt, chunked):
    from relay.providers.bridge_run import BridgeRun
    server = ThreadingHTTPServer(("127.0.0.1", 0), SilentHandler)
    server.release = threading.Event()
    server.prompted = threading.Event()
    server.reject_interrupt = reject_interrupt
    server.chunked = chunked
    threading.Thread(target=server.serve_forever, daemon=True).start()
    backend = OpencodeBackend(Client(f"http://127.0.0.1:{server.server_address[1]}", "u", "p"))
    run = BridgeRun(backend, SID, "hi", True)
    events, errors = [], []
    ended = threading.Event()
    def consume():
        try:
            events.extend(run.events())
        except Exception as exc:  # noqa: BLE001 - report worker exceptions in the test thread
            errors.append(exc)
        finally:
            ended.set()
    thread = threading.Thread(target=consume, daemon=True)
    try:
        thread.start()
        assert server.prompted.wait(2)
        if reject_interrupt:
            with pytest.raises(HttpError):
                run.interrupt()
        else:
            run.interrupt()
        assert ended.wait(2), "stop still needs an end event from the server"
        assert not errors
        assert events[-1] == {"type": "done", "status": "interrupted"}
    finally:
        server.release.set()
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_interrupt_before_stream_start_does_not_prompt():
    backend = OpencodeBackend(FakeClient([{"type": "server.connected"}]))
    backend.interrupt(SID)
    assert list(backend.run(SID, "hi")) == [{"type": "done", "status": "interrupted"}]
    assert not any(path.endswith("/prompt") for _, path, _ in backend.client.requests)
