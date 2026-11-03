"""OpenAI-compatible chat APIs (kind openai-compatible): streaming, history, attachments, Relay's tool loop."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from relay.providers import Context
from relay.providers.base import Template
from relay.providers.chat import OpenAICompatibleProvider


def context(tmp_path, **kw):
    return Context(conversation_id="conv", directory=tmp_path, mode="chat",
                   model=kw.get("model"), provider_session=kw.get("session"),
                   history=kw.get("history", []))


class ChatHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({"data": [{"id": "b-model"}, {"id": "a-model"}]}).encode())

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append((dict(self.headers), request))
        if self.server.mode == "error":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"error": "bad key"}')
            return
        if request.get("stream") is False:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"choices": [{"message": {"content": '"Fixing Line 40"'}}]}).encode())
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        chunks = [{"choices": [{"delta": {"reasoning_content": "hm"}}]},
                  {"choices": [{"delta": {"content": "Hel"}}]},
                  {"choices": [{"delta": {"content": "lo"}}]},
                  {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2}}]
        for chunk in chunks:
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.flush()
            if self.server.mode == "slow":
                time.sleep(0.5)
            if self.server.mode == "cut":
                return  # the connection ends halfway
        self.wfile.write(b"data: [DONE]\n\n")


@pytest.fixture
def chat_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), ChatHandler)
    server.requests, server.mode = [], "ok"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


def chat_provider(server, **options):
    url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    return OpenAICompatibleProvider(Template("c", "C", "openai-compatible", True,
                                             {"base_url": url, "tools": False, **options}))


def test_openai_compatible_streams_and_sends_history(chat_server, tmp_path):
    provider = chat_provider(chat_server, api_key="sk-1", system_prompt="Be brief.")
    assert provider.models() == [{"id": "a-model", "name": "a-model"}, {"id": "b-model", "name": "b-model"}]
    history = [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}]
    events = list(provider.start(context(tmp_path, history=history), "q2").events())
    assert [e["type"] for e in events] == ["session", "step", "reasoning", "text", "text", "usage", "done"]
    assert events[-1] == {"type": "done", "status": "succeeded"}
    assert events[-2]["billing"] == "free"  # a server on this machine
    headers, body = chat_server.requests[0]
    assert headers["Authorization"] == "Bearer sk-1"
    assert body["model"] == "a-model" and body["stream"] is True
    assert body["messages"] == [{"role": "system", "content": "Be brief."}, *history,
                                {"role": "user", "content": "q2"}]


def test_a_chat_model_gets_relays_memory_in_its_system_prompt(chat_server, tmp_path):
    provider = chat_provider(chat_server, system_prompt="Be brief.")
    told = context(tmp_path)
    told.memory = "Relay's memory, which every agent gets.\n\nWhat it holds:\n- [abc123] Talks Dutch"
    list(provider.start(told, "hi").events())
    _, body = chat_server.requests[0]
    assert body["messages"][0] == {"role": "system", "content": f"Be brief.\n\n{told.memory}"}


def test_openai_compatible_http_error(chat_server, tmp_path):
    chat_server.mode = "error"
    events = list(chat_provider(chat_server, models=["m"]).start(context(tmp_path), "hi").events())
    assert events[-2]["type"] == "error" and "HTTP 401" in events[-2]["message"]
    assert events[-1] == {"type": "done", "status": "failed"}


def test_openai_compatible_interrupt(chat_server, tmp_path):
    chat_server.mode = "slow"
    run = chat_provider(chat_server, models=["m"]).start(context(tmp_path), "hi")
    events = run.events()
    seen = [next(events), next(events), next(events)]  # session, step, reasoning
    run.interrupt()
    rest = list(events)
    assert seen[-1]["type"] == "reasoning"
    assert rest[-1] == {"type": "done", "status": "interrupted"}


def test_openai_compatible_key_from_environment(chat_server, tmp_path, monkeypatch):
    monkeypatch.setenv("MY_KEY", "sk-env")
    list(chat_provider(chat_server, api_key_env="MY_KEY", models=["m"]).start(context(tmp_path), "x").events())
    assert chat_server.requests[0][0]["Authorization"] == "Bearer sk-env"


def test_unreachable_server_lists_no_models(tmp_path):
    provider = OpenAICompatibleProvider(Template("c", "C", "openai-compatible", True,
                                                 {"base_url": "http://127.0.0.1:9/v1"}))
    assert provider.models() == []


# Templates


def test_chat_attachments_become_content_parts(chat_server, tmp_path):
    from relay.uploads import Attachment
    (tmp_path / "notes.md").write_text("# Notes\nhello")
    (tmp_path / "pic.png").write_bytes(b"PNGDATA")
    (tmp_path / "data.bin").write_bytes(b"\0\1")
    attachments = [
        Attachment("1", "notes.md", "text/markdown", 13, tmp_path / "notes.md"),
        Attachment("2", "pic.png", "image/png", 7, tmp_path / "pic.png"),
        Attachment("3", "data.bin", "application/octet-stream", 2, tmp_path / "data.bin"),
    ]
    ctx = context(tmp_path)
    ctx.attachments = attachments
    list(chat_provider(chat_server, models=["m"]).start(ctx, "what is this?").events())
    content = chat_server.requests[0][1]["messages"][-1]["content"]
    assert content[0]["type"] == "text"
    assert "--- notes.md ---\n# Notes\nhello" in content[0]["text"]
    assert "data.bin (application/octet-stream) can't be read" in content[0]["text"]
    import base64
    assert content[1] == {"type": "image_url", "image_url": {
        "url": "data:image/png;base64," + base64.b64encode(b"PNGDATA").decode()}}


def test_earlier_attachments_stay_in_the_chat_history(chat_server, tmp_path):
    from relay.uploads import Attachment
    (tmp_path / "log.txt").write_text("ERROR at line 40")
    earlier = Attachment("1", "log.txt", "text/plain", 16, tmp_path / "log.txt")
    ctx = context(tmp_path, history=[
        {"role": "user", "content": "what's wrong?", "attachments": [earlier]},
        {"role": "assistant", "content": "Line 40 fails."},
    ])
    list(chat_provider(chat_server, models=["m"]).start(ctx, "and why?").events())
    messages = chat_server.requests[0][1]["messages"]
    assert "ERROR at line 40" in messages[0]["content"][0]["text"]
    assert messages[1] == {"role": "assistant", "content": "Line 40 fails."}
    assert messages[2] == {"role": "user", "content": "and why?"}


def test_chat_models_suggest_titles(chat_server):
    provider = chat_provider(chat_server, models=["m"])
    assert provider.suggest_title("why does line 40 fail?", "Because…", "m2") == '"Fixing Line 40"'
    assert chat_server.requests[-1][1]["model"] == "m2"


class ToolChat(BaseHTTPRequestHandler):
    """An OpenAI-style server that first calls a tool, then answers with what it got back."""

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(body)
        if self.server.refuse_tools and "tools" in body:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error": "this model does not support tools"}')
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        results = [m for m in body["messages"] if m["role"] == "tool"]
        if "tools" in body and not results:
            chunks = [  # the arguments arrive in pieces, as they stream
                {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "type": "function",
                                                         "function": {"name": self.server.tool, "arguments": "{\"pa"}}]}}]},
                {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": self.server.rest}}]}}]},
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
            ]
        else:
            said = results[-1]["content"] if results else "no tools"
            chunks = [{"choices": [{"delta": {"content": f"Got: {said}"}}]},
                      {"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 7}}]
        for chunk in chunks:
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


@pytest.fixture
def tool_chat():
    server = ThreadingHTTPServer(("127.0.0.1", 0), ToolChat)
    server.requests, server.refuse_tools = [], False
    server.tool, server.rest = "read_file", 'th": "hello.txt"}'
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


def tool_provider(server, **options):
    from relay.providers.chat import provider as chat
    chat.NO_TOOLS.clear()
    url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    return OpenAICompatibleProvider(Template("t", "T", "openai-compatible", True,
                                             {"base_url": url, "models": ["m"], **options}))


def code_context(tmp_path, **kw):
    (tmp_path / "hello.txt").write_text("hi there\n")
    return Context(conversation_id="c", directory=tmp_path, mode="code", model="m", provider_session=None, **kw)


def test_chat_models_get_tools_and_the_loop_runs_them(tool_chat, tmp_path):
    provider = tool_provider(tool_chat)
    assert provider.has_tools()
    events = list(provider.start(code_context(tmp_path), "what's in hello.txt?").events())
    kinds = [e["type"] for e in events]
    assert kinds == ["session", "step", "tool_call", "tool_result", "text", "usage", "done"]
    assert events[2]["input"] == {"path": "hello.txt"}
    assert events[3]["ok"] and "hi there" in events[3]["output"]
    assert events[4]["delta"].startswith("Got:") and "hi there" in events[4]["delta"]
    assert events[5]["tokens"] == {"prompt_tokens": 30, "completion_tokens": 12}  # both rounds
    first, second = tool_chat.requests
    assert "read_file" in [t["function"]["name"] for t in first["tools"]]
    assert "project folder" in first["messages"][0]["content"]  # the system prompt says where it works
    assert second["messages"][-2]["tool_calls"][0]["function"]["arguments"] == '{"path": "hello.txt"}'


def test_servers_without_function_calling_just_chat(tool_chat, tmp_path):
    tool_chat.refuse_tools = True
    provider = tool_provider(tool_chat)
    events = list(provider.start(code_context(tmp_path), "hi").events())
    assert [e["type"] for e in events][-3:] == ["text", "usage", "done"] and events[-1]["status"] == "succeeded"
    assert "tools" in tool_chat.requests[0] and "tools" not in tool_chat.requests[1]
    list(provider.start(code_context(tmp_path), "again").events())
    assert "tools" not in tool_chat.requests[2]  # remembered


def test_read_only_modes_only_ask_for_no_changes(tool_chat, tmp_path):
    from relay.modes import PLAN
    list(tool_provider(tool_chat).start(code_context(tmp_path, agent_mode=PLAN), "look").events())
    offered = [t["function"]["name"] for t in tool_chat.requests[0]["tools"]]
    assert "read_file" in offered and "run_command" in offered and "write_file" in offered
    assert "This is a read-only mode: don't change anything." in tool_chat.requests[0]["messages"][0]["content"]


def test_tool_calls_with_the_same_id_stay_apart():
    from relay.providers.chat.provider import _Call, _ChatRun

    class Box:
        def call(self, name, arguments):
            return True, "ok"

    run = _ChatRun("http://x", {}, {"messages": []}, "s", "m", "free", toolbox=Box())
    ids = []
    for round_ in range(2):  # a model that numbers its calls anew each round
        call = _Call()
        call.id, call.name, call.arguments = "call_0", "read_file", "{}"
        messages = []
        events = list(run._run_tools({0: call}, messages, round_, ""))
        ids.append(events[0]["id"])
        assert events[-1]["id"] == events[0]["id"] == messages[-1]["tool_call_id"]
    assert ids[0] != ids[1]


def test_a_chat_stream_that_breaks_off_is_a_failure(chat_server, tmp_path):
    chat_server.mode = "cut"
    provider = chat_provider(chat_server, models=["m"])
    ctx = Context(conversation_id="c", directory=tmp_path, mode="chat", model="m", provider_session=None)
    events = list(provider.start(ctx, "hi").events())
    assert events[-1] == {"type": "done", "status": "failed"}
    assert any(e["type"] == "error" and "broke off" in e["message"] for e in events)
