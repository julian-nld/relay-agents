"""The check of a provider's layer (relay.providers.conformance): its event checker, its scenarios against a fake
program, and the ways to ask for it (the API, `relay provider-check`)."""

import io
import json
import sys
import textwrap

import pytest
from starlette.testclient import TestClient

from relay import config
from relay.app import create_app
from relay.db import Database
from relay.providers import Registry, conformance, parse_template
from relay.providers.command import CommandProvider

H = {"x-relay-request": "1"}

FAKE = textwrap.dedent('''
    import json, re, subprocess, sys, time, uuid
    prompt = sys.stdin.read()
    session = sys.argv[1] or "s-" + uuid.uuid4().hex[:8]
    def emit(**event):
        print(json.dumps(event), flush=True)
    emit(type="session", session=session)
    asked = re.search(r"echo relay-check-[0-9a-f]+", prompt)
    if "Count from 1" in prompt:
        for n in range(1, 301):
            emit(type="text", delta=f"{n}\\n")
            time.sleep(0.05)
    elif asked:
        out = subprocess.run(asked.group(0), shell=True, capture_output=True, text=True).stdout
        emit(type="tool_call", id="t1", name="shell", input={"command": asked.group(0)})
        emit(type="tool_result", id="t1", name="shell", ok=True, output=out)
        emit(type="text", delta=f"It printed {out.strip()}")
    else:
        emit(type="text", delta="OK" + (" again" if sys.argv[1] else ""))
    emit(type="done", status="succeeded")
''')


def fake(tmp_path, **options):
    program = tmp_path / "fake.py"
    program.write_text(FAKE)
    return CommandProvider(parse_template({"id": "fake", "name": "Fake", "kind": "command", "options": {
        "command": [sys.executable, str(program), "{session}"], **options}}))


def test_the_event_checker_says_what_relay_cant_read():
    good = [{"type": "session", "session": "s"}, {"type": "text", "delta": "Hi"},
            {"type": "tool_call", "id": "t", "name": "shell", "input": {}},
            {"type": "tool_result", "id": "t", "ok": True, "output": "x"},
            {"type": "usage", "tokens": {"input_tokens": 1}, "billing": "api"}, {"type": "done", "status": "succeeded"}]
    assert conformance.validate_events(good) == []
    assert conformance.validate_events([{"type": "text", "delta": "Hi"}]) == ['a run ends with one "done" event, not 0']
    assert conformance.validate_events([*good, {"type": "text", "delta": "late"}]) == ['"done" must be the last event']
    assert conformance.validate_events(good[1:]) == [
        'no "session" event: Relay can\'t go on in this session next turn']
    assert conformance.validate_events([{"type": "error", "message": "no"}, {"type": "done", "status": "failed"}]) == []
    assert conformance.validate_events([good[0], {**good[3], "id": "other"}, good[-1]]) == [
        "event 2 (tool_result): no tool_call has its id 'other'"]
    assert conformance.validate_events([good[0], good[2], good[-1]]) == [
        "tool_call 't' got no tool_result, though the run succeeded"]
    assert conformance.validate_events([good[0], good[2], {"type": "done", "status": "interrupted"}]) == []
    found = conformance.validate_events([
        {"type": "session", "session": 7}, {"type": "text", "content": "Hi"}, {"type": "thinking", "delta": "x"},
        {"type": "tool_result", "id": "t", "ok": "yes"}, {"type": "usage", "billing": "monthly"}, "text",
        {"type": "done", "status": "ok"}])
    assert found == [
        "event 1 (session): 'session' must be a str, not 7",
        "event 2 (text): 'delta' must be a str, not None",
        "event 3: Relay doesn't know the type 'thinking' (it's left out)",
        "event 4 (tool_result): 'ok' must be a bool, not 'yes'",
        "event 5 (usage): billing 'monthly' isn't one of 'api', 'subscription', 'free', None",
        "event 6 isn't an object with a type: 'text'",
        "event 7 (done): status 'ok' isn't one of 'succeeded', 'failed', 'interrupted'",
        "event 4 (tool_result): no tool_call has its id 't'"]


def test_a_layer_that_does_what_relay_needs_passes_every_scenario(tmp_path):
    results = conformance.check(fake(tmp_path), "", tmp_path / "scratch")
    assert [(r["name"], r["ok"]) for r in results] == [
        ("Loads", True), ("check()", True), ("Models", True), ("A turn", True), ("Going on", True),
        ("Stopping", True), ("A command", True)]
    assert results[0]["detail"] == "kind command from relay/providers/command/"
    assert "“OK”" in results[3]["detail"]
    assert results[6]["detail"] == "its tool “shell” ran it, and its output came back"
    assert not list((tmp_path / "scratch").iterdir())  # (its scratch folders go)


def test_a_layer_that_doesnt_is_told_why(tmp_path):
    program = tmp_path / "rude.py"
    program.write_text("import json\nprint(json.dumps({'type': 'text', 'delta': 'OK'}))\n")  # (no session, no done)
    rude = CommandProvider(parse_template({"id": "rude", "name": "Rude", "kind": "command", "options": {
        "command": [sys.executable, str(program)]}}))
    results = {r["name"]: r for r in conformance.check(rude, "", tmp_path / "scratch")}
    assert results["A turn"]["ok"] is False and "exited with code 0" in results["A turn"]["detail"]
    assert results["Going on"] == {"name": "Going on", "ok": None, "detail": "not tried: the first turn failed"}
    assert results["A command"]["ok"] is False and results["A command"]["detail"] == "no tool_call event"
    missing = CommandProvider(parse_template({"id": "gone", "name": "Gone", "kind": "command", "options": {
        "command": ["a-program-that-isnt-there"]}}))
    assert conformance.check(missing, "", tmp_path / "scratch")[1] == {
        "name": "check()", "ok": False, "detail": "it says: 'a-program-that-isnt-there' not found"}


@pytest.fixture
def env(tmp_path):
    registry = Registry(tmp_path / "providers")
    registry.ensure_defaults = lambda: None
    (tmp_path / "providers").mkdir()
    registry.add(fake(tmp_path).template.to_dict())
    settings = config.Settings(workspace_roots=[str(tmp_path)], chat_root=str(tmp_path / "chats"))
    with TestClient(create_app(settings, Database(tmp_path / "relay.db"), registry)) as client:
        client.post("/api/setup", json={"username": "me", "password": "correct horse"}, headers=H).raise_for_status()
        client.tmp = tmp_path
        yield client


def test_the_api_and_the_command_line_ask_for_the_same_check(env, monkeypatch):
    from relay import cli

    answer = env.post("/api/providers/fake/conformance", json={"model": ""}, headers=H).json()
    assert answer["ok"] and [r["ok"] for r in answer["results"]] == [True] * 7

    class Answer(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    asked = []

    def urlopen(request, timeout):  # (the running Relay: this test's, signed in with the command's own session)
        asked.append(request.full_url)
        name, token = request.headers["Cookie"].split("=", 1)
        env.cookies.clear()
        env.cookies.set(name, token)
        reply = env.post("/" + request.full_url.split("/", 3)[3], content=request.data,
                         headers={**H, "content-type": "application/json"})
        return Answer(reply.content)

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    monkeypatch.setattr(config, "database_path", lambda: env.tmp / "relay.db")
    before = len(env.app.state.relay.db.all("SELECT * FROM auth_sessions"))
    assert cli.main(["provider-check", "fake"]) == 0
    assert asked[0].endswith("/api/providers/fake/conformance")
    assert len(env.app.state.relay.db.all("SELECT * FROM auth_sessions")) == before  # (its own session ended)


def test_a_check_turns_permission_requests_are_answered_as_a_runs_and_nothing_else(env):
    """A check's turn has no conversation: its token (RunManager.place_check) answers permission requests with the
    provider's judge() and offers no other tool; forgotten, it's refused like any unknown one."""
    relay = env.app.state.relay
    provider = relay.registry.provider("fake")
    rpc = lambda token, method, params=None: env.post(f"/mcp/{token}", json={  # noqa: E731
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
    context = conformance.Context(conversation_id="c", directory=env.tmp, mode="code", model=None,
                                  provider_session=None)
    done = relay.runs.place_check(provider, context)
    token = context.approvals_url.rsplit("/", 1)[1]
    assert [t["name"] for t in rpc(token, "tools/list").json()["result"]["tools"]] == ["approve_permission"]
    ask = lambda command: json.loads(rpc(token, "tools/call", {  # noqa: E731
        "name": "approve_permission", "arguments": {"tool_name": "Bash", "input": {"command": command}}}).json()[
        "result"]["content"][0]["text"])
    assert ask("ls")["behavior"] == "allow"
    denied = ask("rm -rf /")
    assert denied["behavior"] == "deny" and "blocked" in denied["message"]
    assert rpc(token, "tools/call", {"name": "delegate", "arguments": {}}).json()["result"]["isError"]
    done()
    assert rpc(token, "tools/list").status_code == 404
