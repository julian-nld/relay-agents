import base64
import json
import pathlib
import re
import subprocess
import sys
import textwrap
import threading
import time
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from relay import config, containers, forks, health
from relay.app import create_app
from relay.db import Database
from relay.providers import KINDS, Registry

H = {"x-relay-request": "1"}

# A stand-in agent: prints uniform events. It echoes the prompt, reports the
# session it was given (or a new one), and can be made slow for stop tests.
AGENT = textwrap.dedent("""\
    import json, sys, time, uuid
    session = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else "sess-" + uuid.uuid4().hex[:6]
    prompt = sys.stdin.read()
    def emit(**e): print(json.dumps(e), flush=True)
    emit(type="session", session=session)
    emit(type="step", agent=None, model="fake")
    if "write" in prompt:
        with open("agent.txt", "a") as fh:
            fh.write(prompt + "\\n")
    if "tool" in prompt:
        emit(type="tool_call", id="t1", name="shell", input={"command": "ls"})
        emit(type="tool_result", id="t1", name="shell", ok=True, output="a.txt")
    for word in ["You", " said: ", prompt, " (session ", session, ")"]:
        emit(type="text", delta=word)
        if "slow" in prompt: time.sleep(0.5)
    if "title" in prompt:
        emit(type="title", title="  A title from the agent ")
    if "mcp?" in prompt:
        import os
        emit(type="text", delta=" mcp=" + ("yes" if os.environ.get("RELAY_MCP_URL") else "no")
             + " tools=" + ("yes" if os.access(os.environ.get("RELAY_TOOLS", "/nope"), os.X_OK) else "no"))
    if "which effort" in prompt:
        import os
        emit(type="text", delta=" effort=" + os.environ.get("RELAY_EFFORT", ""))
    if "which mode" in prompt:
        import os
        emit(type="text", delta=" mode=" + os.environ.get("RELAY_MODE", "") + "/" + os.environ.get("RELAY_MODE_ACCESS", "")
             + "/" + os.environ.get("RELAY_MODE_INSTRUCTIONS", ""))
    if "linger" in prompt:  # stays at work until the test lets it go
        import os
        for _ in range(150):
            if os.path.exists("done-lingering"): break
            time.sleep(0.1)
    emit(type="usage", tokens={"output": 3}, cost=0)
    emit(type="done", status="succeeded")
""")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_HOME", str(tmp_path / "data"))
    agent = tmp_path / "agent.py"
    agent.write_text(AGENT)
    workspace = tmp_path / "ws"
    (workspace / "project").mkdir(parents=True)
    settings = config.Settings(workspace_roots=[str(workspace)], chat_root=str(tmp_path / "chats"))
    registry = Registry(tmp_path / "providers")
    registry.ensure_defaults()
    registry.add({"id": "fake", "name": "Fake agent", "kind": "command", "options": {
        "command": [sys.executable, str(agent), ""],
        "resume_command": [sys.executable, str(agent), "{session}"],
    }})
    app = create_app(settings, Database(tmp_path / "relay.db"), registry)
    with TestClient(app) as client:
        client.tmp = tmp_path
        yield client


def signed_in(client):
    r = client.post("/api/setup", json={"username": "me", "password": "correct horse"}, headers=H)
    assert r.status_code == 200, r.text
    return client


def events_of(client, run_id):
    events = []
    with client.stream("GET", f"/api/runs/{run_id}/events") as response:
        for line in response.iter_lines():
            if line.startswith("data: ") and line != "data: {}":
                events.append(json.loads(line[6:]))
    return events


def wait_idle(client, conversation_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if client.get(f"/api/conversations/{conversation_id}").json()["running_run"] is None:
            return
        time.sleep(0.05)
    raise AssertionError("run did not finish")


@pytest.mark.parametrize("answer", [{"use": ["Gaming", "Local AI"], "gpu": "Other: RTX 5070"}, None, "stop"])
def test_question_answers_resume_the_provider_and_are_replayed(env, monkeypatch, answer):
    from relay.providers.base import ProviderRun
    signed_in(env)
    relay = env.app.state.relay
    provider = relay.registry.provider("fake")
    form = {"type": "form", "id": "frm_1", "title": "Questions", "status": "pending",
            "fields": [{"key": "use", "type": "multiselect", "title": "Main use?", "custom": True,
                        "options": [{"value": "Gaming", "label": "Gaming"}]}]}
    class Run(ProviderRun):
        def __init__(self):
            self.ready = threading.Event()
            self.answers = []

        def events(self):
            yield form
            assert self.ready.wait(5)
            yield {"type": "text", "delta": "Received your answers"}
            yield {"type": "done", "status": "succeeded"}

        def answer_form(self, request_id, value):
            assert request_id == "frm_1"
            self.answers.append(value)
            self.ready.set()
            time.sleep(.03)  # the provider can finish before its reply HTTP request returns

        def interrupt(self):
            self.ready.set()

    run = Run()
    monkeypatch.setattr(provider, "start", lambda context, prompt: run)
    monkeypatch.setattr(relay.registry, "provider", lambda _: provider)
    conv = env.post("/api/conversations", json={"provider_id": "fake", "fallback_loop": "off"}, headers=H).json()
    rid = env.post(f"/api/conversations/{conv['id']}/messages", json={"text": "Ask me"}, headers=H).json()["run_id"]
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not relay.runs.waiting_for_answer(conv["id"]):
        time.sleep(.01)
    assert relay.runs.waiting_for_answer(conv["id"])
    pending = relay.runs.active[rid].events[0]
    assert {key: value for key, value in pending.items() if key != "expires"} == form
    assert pending["expires"] == pytest.approx(time.time() + 300, abs=3)
    assert env.post(f"/api/runs/{rid}/form", json={"id": "frm_1"}, headers=H).status_code == 400
    assert env.post(f"/api/runs/{rid}/form", json={"id": "frm_1", "answer": []}, headers=H).status_code == 400
    assert env.post(f"/api/runs/{rid}/form", json={"id": "unknown", "answer": {}}, headers=H).status_code == 400
    assert env.post("/api/runs/unknown/form", json={"id": "frm_1", "answer": {}}, headers=H).status_code == 404
    response = (env.post(f"/api/runs/{rid}/stop", headers=H) if answer == "stop" else
                env.post(f"/api/runs/{rid}/form", json={"id": "frm_1", "answer": answer}, headers=H))
    assert response.status_code == 200, response.text
    wait_idle(env, conv["id"])
    assert run.answers == ([] if answer == "stop" else [answer])
    assert not relay.runs.waiting_for_answer(conv["id"])
    assert env.post(f"/api/runs/{rid}/form", json={"id": "frm_1", "answer": answer}, headers=H).status_code == 400
    stored = events_of(env, rid)
    forms = [e for e in stored if e["type"] == "form"]
    assert forms[0] == pending
    assert forms[1]["status"] == ("cancelled" if answer in (None, "stop") else "answered")
    assert forms[1]["fields"] == form["fields"]
    if answer not in (None, "stop"):
        assert forms[1]["answer"] == answer
    assert stored[-1] == ({"type": "done", "status": "interrupted", "why": "the user stopped it"} if answer == "stop"
                          else {"type": "done", "status": "succeeded"})


# Accounts and security


def test_question_timeout_dismisses_without_a_connected_browser(env, monkeypatch):
    from relay import tunables
    from relay.providers.base import ProviderRun
    signed_in(env)
    monkeypatch.setitem(tunables._values, "questions.dismiss_timeout", .05)
    relay = env.app.state.relay
    provider = relay.registry.provider("fake")
    ready, answers = threading.Event(), []
    class Run(ProviderRun):
        def events(self):
            yield {"type": "form", "id": "frm_1", "title": "Use?", "fields": [], "status": "pending"}
            assert ready.wait(3)
            yield {"type": "done", "status": "succeeded"}

        def answer_form(self, request_id, value):
            assert request_id == "frm_1"
            answers.append(value)
            ready.set()

        def interrupt(self):
            ready.set()

    monkeypatch.setattr(provider, "start", lambda context, prompt: Run())
    monkeypatch.setattr(relay.registry, "provider", lambda _: provider)
    conv = env.post("/api/conversations", json={"provider_id": "fake", "fallback_loop": "off"}, headers=H).json()
    rid = env.post(f"/api/conversations/{conv['id']}/messages", json={"text": "Ask me"}, headers=H).json()["run_id"]
    wait_idle(env, conv["id"])
    assert answers == [None]
    forms = [e for e in relay.db.run_events(rid) if e["type"] == "form"]
    assert forms[0]["expires"] > 0
    assert forms[1]["status"] == "cancelled"
    assert "automatically" in forms[1]["message"]
    assert not relay.runs.waiting_for_answer(conv["id"])


def test_first_visitor_sets_up_then_login_is_required(env):
    state = env.get("/api/state").json()
    assert state["needs_setup"] and state["login_required"] and state["user"] is None
    assert env.get("/api/conversations").status_code == 401

    r = env.post("/api/setup", json={"username": "me", "password": "short"}, headers=H)
    assert r.status_code == 400 and "8 characters" in r.json()["error"]
    signed_in(env)
    assert env.get("/api/state").json()["user"]["username"] == "me"
    assert env.post("/api/setup", json={"username": "x", "password": "another one"},
                    headers=H).status_code == 409

    env.post("/api/logout", headers=H)
    assert env.get("/api/conversations").status_code == 401
    assert env.post("/api/login", json={"username": "me", "password": "wrong password"},
                    headers=H).status_code == 401
    assert env.post("/api/login", json={"username": "me", "password": "correct horse"},
                    headers=H).status_code == 200
    assert env.get("/api/conversations").status_code == 200


def test_writes_need_the_csrf_header(env):
    assert env.post("/api/setup", json={}).status_code == 403


def test_login_throttling(env):
    signed_in(env)
    env.post("/api/logout", headers=H)
    codes = [env.post("/api/login", json={"username": "me", "password": "nope nope"},
                      headers=H).status_code for _ in range(7)]
    assert codes[:5] == [401] * 5 and 429 in codes[5:]


def test_security_headers(env):
    r = env.get("/")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    # The web app's modules are checked each time, so a browser never mixes old and new ones.
    assert r.headers["cache-control"] == "no-cache"
    module = env.get("/static/js/main.js")
    assert module.headers["cache-control"] == "no-cache"
    assert env.get("/static/js/main.js", headers={"If-None-Match": module.headers["etag"]}).status_code == 304



def test_a_model_as_its_program_names_it(env, monkeypatch):
    """Provider.model_id at a conversation's start and at each run's (a model switched to in between): stored so,
    for the page, the run and its health. (Found for real: "claude-code/sonnet", from a script.)"""
    from relay.providers.command import CommandProvider
    monkeypatch.setattr(CommandProvider, "model_id", lambda self, model: (model or "").removeprefix("fake/") or None)
    signed_in(env)
    relay = env.app.state.relay
    conv = env.post("/api/conversations", json={"provider_id": "fake", "model": "fake/x"}, headers=H).json()
    assert relay.db.one("SELECT model FROM conversations WHERE id = ?", (conv["id"],))["model"] == "x"
    env.patch(f"/api/conversations/{conv['id']}", json={"model": "fake/y"}, headers=H)
    run_id = env.post(f"/api/conversations/{conv['id']}/messages", json={"text": "hello"}, headers=H).json()["run_id"]
    wait_idle(env, conv["id"])
    assert relay.db.one("SELECT model FROM conversations WHERE id = ?", (conv["id"],))["model"] == "y"
    assert relay.db.one("SELECT model FROM runs WHERE id = ?", (run_id,))["model"] == "y"


def test_an_agent_left_working_by_a_crash_is_stopped_at_the_next_start(env, monkeypatch):
    """opencode's server outlives Relay, and keeps working (and using a usage limit) on a session."""
    from relay.providers.command import CommandProvider
    signed_in(env)
    relay = env.app.state.relay
    conv = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    run_id = env.post(f"/api/conversations/{conv['id']}/messages", json={"text": "slow slow"}, headers=H).json()["run_id"]
    for _ in range(100):
        if relay.db.one("SELECT session FROM runs WHERE id = ?", (run_id,))["session"]:
            break
        time.sleep(0.05)
    session = relay.db.one("SELECT session FROM runs WHERE id = ?", (run_id,))["session"]
    assert session  # known at once, not only when the run ends
    stopped = []
    monkeypatch.setattr(CommandProvider, "abandon", lambda self, s, d: stopped.append((self.template.id, s)))
    relay.db.execute("UPDATE runs SET status = 'running' WHERE id = ?", (run_id,))  # as a crash leaves it
    wait_idle(env, conv["id"])
    relay.db.execute("UPDATE runs SET status = 'running' WHERE id = ?", (run_id,))
    again = create_app(config.Settings(workspace_roots=[str(env.tmp / "ws")], chat_root=str(env.tmp / "chats")),
                       relay.db, relay.registry)
    with TestClient(again):
        for _ in range(100):
            if stopped:
                break
            time.sleep(0.05)
    assert stopped == [("fake", session)]
    assert relay.db.one("SELECT status FROM runs WHERE id = ?", (run_id,))["status"] == "failed"


def test_login_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_HOME", str(tmp_path / "data"))
    settings = config.Settings(auth="none", chat_root=str(tmp_path / "chats"))
    app = create_app(settings, Database(tmp_path / "relay.db"), Registry(tmp_path / "p"))
    with TestClient(app) as client:
        state = client.get("/api/state").json()
        assert not state["login_required"] and not state["needs_setup"]
        assert state["user"]["username"] == "local"
        assert client.get("/api/conversations").status_code == 200
        # (an app password, from a Relay that had login: nothing to manage, and nothing it needs)
        assert client.post("/api/app-passwords", json={"name": "x"}, headers=H).status_code == 400
        assert client.get("/api/conversations", headers={"Authorization": "Bearer relay_old"}).status_code == 200


def test_app_passwords_sign_scripts_in_apart_from_the_password(env):
    """An app password is for a script or program that uses the API (the SDK): made and revoked in Settings →
    Account, shown once, sent in the Authorization header (Bearer, or Basic with the username), and needing no CSRF
    header (another site's page can't send that header)."""
    signed_in(env)
    made = env.post("/api/app-passwords", json={"name": "  backup\n script "}, headers=H).json()
    assert made["name"] == "backup script" and made["password"].startswith("relay_") and made["last_used"] is None
    other = env.post("/api/app-passwords", json={"name": "phone shortcut"}, headers=H).json()
    listed = env.get("/api/app-passwords").json()
    assert [(p["id"], p["name"]) for p in listed] == [(made["id"], "backup script"), (other["id"], "phone shortcut")]
    assert all("password" not in p for p in listed)
    assert env.post("/api/app-passwords", json={"name": " "}, headers=H).status_code == 400
    assert env.post("/api/app-passwords", json={}, headers=H).status_code == 400

    script = TestClient(env.app)  # (no cookie: a script elsewhere)
    bearer = {"Authorization": f"Bearer {made['password']}"}
    assert script.get("/api/state", headers=bearer).json()["user"]["username"] == "me"
    conv = script.post("/api/conversations", json={"provider_id": "fake"}, headers=bearer)
    assert conv.status_code == 200, conv.text  # (no x-relay-request: none needed)
    assert conv.json()["id"] in [c["id"] for c in env.get("/api/conversations").json()]  # (the user's own)
    basic = {"Authorization": "Basic " + base64.b64encode(f"me:{made['password']}".encode()).decode()}
    assert script.get("/api/conversations", headers=basic).status_code == 200
    someone = {"Authorization": "Basic " + base64.b64encode(f"else:{made['password']}".encode()).decode()}
    assert script.get("/api/conversations", headers=someone).status_code == 401
    assert env.get("/api/app-passwords").json()[0]["last_used"]
    # It can't manage app passwords (a leaked one can't replace itself or shut the others out)
    assert script.get("/api/app-passwords", headers=bearer).status_code == 403
    assert script.post("/api/app-passwords", json={"name": "more"}, headers=bearer).status_code == 403
    assert script.delete(f"/api/app-passwords/{other['id']}", headers=bearer).status_code == 403
    # Wrong, or not an app password at all (a session's token isn't one): no way in, and no cookie counts with it
    for wrong in ("relay_" + "x" * 43, made["password"][:-1], "", env.cookies.get("relay_session")):
        refused = env.get("/api/conversations", headers={"Authorization": f"Bearer {wrong}"})
        assert refused.status_code == 401 and "Settings → Account" in refused.json()["error"], wrong
    assert script.get("/api/conversations", headers={"Authorization": "Basic not base64!"}).status_code == 401
    # Revoked: it stops working; the other and the browser's session don't
    assert env.delete(f"/api/app-passwords/{made['id']}", headers=H).json() == {"ok": True}
    assert env.delete(f"/api/app-passwords/{made['id']}", headers=H).status_code == 404
    assert script.get("/api/conversations", headers=bearer).status_code == 401
    assert script.get("/api/conversations", headers={"Authorization": f"Bearer {other['password']}"}).status_code == 200
    assert env.get("/api/conversations").status_code == 200
    # Changing the account's password leaves app passwords as they are (each is revoked on its own)
    env.post("/api/account/password", json={"current": "correct horse", "new": "battery staple"}, headers=H)
    assert script.get("/api/conversations", headers={"Authorization": f"Bearer {other['password']}"}).status_code == 200



def test_a_proxys_own_sign_in_in_front_of_relay_leaves_its_session_working(env):
    """Behind a reverse proxy with a sign-in of its own (Caddy's basic_auth), the browser sends the proxy's Basic
    credentials with every request. They're no app password, so Relay's session counts, as without them (they were
    taken for a wrong app password: signed in, and shown the sign-in again), and what changes something still needs
    the CSRF header."""
    proxy = {"Authorization": "Basic " + base64.b64encode(b"alex:the proxy's own").decode()}
    env.post("/api/setup", json={"username": "me", "password": "correct horse"}, headers=H)
    env.post("/api/logout", headers=H)
    assert env.get("/api/state", headers=proxy).json()["user"] is None
    signed = env.post("/api/login", json={"username": "me", "password": "correct horse"}, headers={**proxy, **H})
    assert signed.status_code == 200
    assert env.get("/api/state", headers=proxy).json()["user"]["username"] == "me"
    assert env.get("/api/conversations", headers=proxy).status_code == 200
    assert env.post("/api/conversations", json={"provider_id": "fake"}, headers=proxy).status_code == 403
    assert env.post("/api/conversations", json={"provider_id": "fake"}, headers={**proxy, **H}).status_code == 200
    assert env.get("/api/app-passwords", headers=proxy).status_code == 200  # (the browser's session, no app password)
    # Without a session it's no way in; a wrong app password with one is still wrong (no cookie counts with it)
    refused = TestClient(env.app).get("/api/conversations", headers=proxy)
    assert refused.status_code == 401 and "Settings → Account" in refused.json()["error"]
    assert env.get("/api/conversations", headers={"Authorization": "Bearer relay_wrong"}).status_code == 401


def test_behind_a_proxys_own_sign_in_a_script_sends_its_app_password_in_relays_own_header(env):
    """A proxy with a sign-in on every path (Caddy's basic_auth) takes the Authorization header: a script sends the
    proxy's Basic there and its app password in X-Relay-App-Password, which counts as an app password does."""
    signed_in(env)
    made = env.post("/api/app-passwords", json={"name": "from outside"}, headers=H).json()
    proxy = {"Authorization": "Basic " + base64.b64encode(b"alex:the proxy's own").decode()}
    script = TestClient(env.app)  # (no cookie)
    both = {**proxy, "X-Relay-App-Password": made["password"]}
    assert script.get("/api/state", headers=both).json()["user"]["username"] == "me"
    assert script.post("/api/conversations", json={"provider_id": "fake"}, headers=both).status_code == 200
    assert script.get("/api/app-passwords", headers=both).status_code == 403  # (an app password's limits)
    assert script.get("/api/conversations", headers={"X-Relay-App-Password": made["password"]}).status_code == 200
    # A wrong one is wrong, with a session and the proxy's Basic too (a script's mistake, not a proxy's)
    refused = env.get("/api/conversations", headers={**proxy, "X-Relay-App-Password": "relay_wrong"})
    assert refused.status_code == 401 and "Settings → Account" in refused.json()["error"]


def test_a_conversation_started_outside_relays_pages_says_by_whom(env):
    """Relay's pages say they're Relay's (X-Relay-Client: web). A conversation made without that, by a script or a
    program through the API, is marked in the lists (a yellow E), with who started it."""
    signed_in(env)
    web = {**H, "x-relay-client": "web"}
    mine = env.post("/api/conversations", json={"provider_id": "fake"}, headers=web).json()
    assert mine["external"] is None
    program = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    assert program["external"] == "Relay's API"
    sdk = env.post("/api/conversations", json={"provider_id": "fake"}, headers={**H, "user-agent": "relay-sdk/3.1.0"})
    assert sdk.json()["external"] == "the Python SDK"
    app = env.post("/api/app-passwords", json={"name": "nightly"}, headers=web).json()
    script = TestClient(env.app)
    bearer = {"Authorization": f"Bearer {app['password']}"}
    assert script.post("/api/conversations", json={"provider_id": "fake"}, headers=bearer).json()["external"] == \
        "Relay's API, with the app password “nightly”"
    listed = {c["id"]: c["external"] for c in env.get("/api/conversations").json()}
    assert (listed[mine["id"]], listed[program["id"]]) == (None, "Relay's API")
    # A fork is whoever made it: the user's of a program's, a program's of the user's
    for conversation in (mine, program):
        run_turn(env, conversation["id"], "hello")
    assert env.post(f"/api/conversations/{program['id']}/fork", json={}, headers=web).json()["external"] is None
    assert env.post(f"/api/conversations/{mine['id']}/fork", json={}, headers=H).json()["external"] == "Relay's API"


# Conversations and runs


def test_chat_round_trip_with_session_continuation(env):
    signed_in(env)
    conv = env.post("/api/conversations", json={"provider_id": "fake", "mode": "chat"},
                    headers=H).json()
    assert conv["title"] == "New chat"

    run1 = env.post(f"/api/conversations/{conv['id']}/messages", json={"text": "hello tool"},
                    headers=H).json()["run_id"]
    events = events_of(env, run1)
    kinds = [e["type"] for e in events]
    assert kinds[0] == "step" and "tool_call" in kinds and kinds[-1] == "done"
    assert events[-1]["status"] == "succeeded"
    text = "".join(e["delta"] for e in events if e["type"] == "text")
    assert text.startswith("You said: hello tool (session sess-")
    session = text.split("(session ")[1].rstrip(")")

    wait_idle(env, conv["id"])
    run2 = env.post(f"/api/conversations/{conv['id']}/messages", json={"text": "again"},
                    headers=H).json()["run_id"]
    text2 = "".join(e["delta"] for e in events_of(env, run2) if e["type"] == "text")
    assert f"(session {session})" in text2  # the provider's session was continued

    wait_idle(env, conv["id"])
    full = env.get(f"/api/conversations/{conv['id']}").json()
    assert full["title"] == "hello tool"
    roles = [m["role"] for m in full["messages"]]
    assert roles == ["user", "assistant", "user", "assistant"]
    first_answer = full["messages"][1]
    assert first_answer["status"] == "succeeded"
    assert first_answer["content"] == text
    # Stored exactly as streamed, so live event numbers stay valid.
    assert first_answer["events"] == events


def test_replaying_a_finished_run_from_an_offset(env):
    signed_in(env)
    conv = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    run = env.post(f"/api/conversations/{conv['id']}/messages", json={"text": "hi"},
                   headers=H).json()["run_id"]
    events_of(env, run)
    wait_idle(env, conv["id"])
    stored = env.get(f"/api/conversations/{conv['id']}").json()["messages"][1]["events"]
    with env.stream("GET", f"/api/runs/{run}/events?after=2") as response:
        replay = [json.loads(line[6:]) for line in response.iter_lines()
                  if line.startswith("data: ") and line != "data: {}"]
    assert replay == stored[2:]


def test_stop_interrupts_a_run(env):
    signed_in(env)
    conv = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    run = env.post(f"/api/conversations/{conv['id']}/messages", json={"text": "slow please"},
                   headers=H).json()["run_id"]
    time.sleep(0.8)
    assert env.post(f"/api/runs/{run}/stop", headers=H).json() == {"stopped": True}
    events = events_of(env, run)
    assert events[-1] == {"type": "done", "status": "interrupted", "why": "the user stopped it"}
    assert not any(e["type"] == "error" for e in events)
    wait_idle(env, conv["id"])


def test_one_run_at_a_time(env):
    signed_in(env)
    conv = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    env.post(f"/api/conversations/{conv['id']}/messages", json={"text": "slow"}, headers=H)
    r = env.post(f"/api/conversations/{conv['id']}/messages", json={"text": "x"}, headers=H)
    assert r.status_code == 400 and "still answering" in r.json()["error"]
    wait_idle(env, conv["id"])


def test_provider_errors_become_failed_runs(env):
    signed_in(env)
    env.put("/api/providers/fake", json={"id": "fake", "name": "Fake", "kind": "command",
            "options": {"command": [sys.executable, "-c", "import sys; sys.exit('boom')"]}},
            headers=H)
    conv = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    run = env.post(f"/api/conversations/{conv['id']}/messages", json={"text": "hi"},
                   headers=H).json()["run_id"]
    events = events_of(env, run)
    assert events[-2] == {"type": "error", "message": "boom"}
    assert events[-1] == {"type": "done", "status": "failed"}


def test_code_mode_requires_a_folder_inside_a_workspace_root(env):
    signed_in(env)
    project = env.tmp / "ws" / "project"
    ok = env.post("/api/conversations", json={"provider_id": "fake", "mode": "code",
                                              "directory": str(project)}, headers=H)
    assert ok.status_code == 200 and ok.json()["directory"] == str(project)
    for bad in ["/", str(env.tmp), str(env.tmp / "ws" / "project" / ".." / ".."), "relative"]:
        r = env.post("/api/conversations", json={"provider_id": "fake", "mode": "code",
                                                 "directory": bad}, headers=H)
        assert r.status_code == 400, bad


def test_conversations_list_search_rename_pin_delete(env):
    signed_in(env)
    a = env.post("/api/conversations", json={"provider_id": "fake", "title": "Alpha"}, headers=H).json()
    b = env.post("/api/conversations", json={"provider_id": "fake", "title": "Beta"}, headers=H).json()
    env.patch(f"/api/conversations/{a['id']}", json={"pinned": True}, headers=H)
    assert [c["title"] for c in env.get("/api/conversations").json()] == ["Alpha", "Beta"]
    assert [c["title"] for c in env.get("/api/conversations?q=bet").json()] == ["Beta"]
    env.patch(f"/api/conversations/{b['id']}", json={"title": "Gamma"}, headers=H)
    assert env.get(f"/api/conversations/{b['id']}").json()["title"] == "Gamma"

    run = env.post(f"/api/conversations/{b['id']}/messages", json={"text": "hi"},
                   headers=H).json()["run_id"]
    events_of(env, run)
    wait_idle(env, b["id"])
    scratch = env.tmp / "chats" / b["id"]
    assert scratch.is_dir()
    assert env.delete(f"/api/conversations/{b['id']}", headers=H).json() == {"ok": True}
    assert not scratch.exists()
    assert env.get(f"/api/conversations/{b['id']}").status_code == 404


def test_other_users_cannot_see_conversations(env):
    signed_in(env)
    conv = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    env.post("/api/logout", headers=H)
    from relay.auth import Accounts
    Accounts(env.app.state.relay.db).create_user("other", "other password")
    env.post("/api/login", json={"username": "other", "password": "other password"}, headers=H)
    assert env.get(f"/api/conversations/{conv['id']}").status_code == 404
    assert env.get("/api/conversations").json() == []


# Providers


def test_provider_templates_crud_and_secret_masking(env):
    signed_in(env)
    kinds = {k["kind"]: k for k in env.get("/api/provider-kinds").json()}
    # Relay's own (not those the user added: Add puts a new kind's folder among them)
    own = {k for k, v in kinds.items() if v["builtin"] or k == "example"}
    assert own == {"claude-code", "opencode", "copilot", "openai-compatible", "acp", "perplexity", "command", "example"}
    assert [k for k in own if not kinds[k]["listed"]] == ["example"]  # (the guide's: not offered in Settings)
    ids = [p["id"] for p in env.get("/api/providers").json()["providers"]]
    assert ids == ["claude-code", "fake", "opencode"]

    template = {"id": "router", "name": "Router", "kind": "openai-compatible",
                "options": {"base_url": "https://example.test/v1", "api_key": "sk-secret"}}
    created = env.post("/api/providers", json=template, headers=H).json()
    assert created["options"]["api_key"] == "********"
    assert "sk-secret" not in env.get("/api/providers").text

    # Saving with the mask keeps the stored key.
    saved = env.put("/api/providers/router", json={**created, "name": "Router 2"}, headers=H)
    assert saved.status_code == 200, saved.text
    stored = env.app.state.relay.registry.get("router")
    assert stored.name == "Router 2" and stored.options["api_key"] == "sk-secret"
    # (what the API says about a template comes along when a client sends it back)
    claude = next(p for p in env.get("/api/providers").json()["providers"] if p["id"] == "claude-code")
    assert claude["sign_in"] and claude["shell"] is True
    about = {key: claude[key] for key in ("id", "name", "kind", "enabled", "options", "sign_in", "shell")}
    assert env.put("/api/providers/claude-code", json=about, headers=H).status_code == 200

    bad = env.post("/api/providers", json={**template, "id": "x2", "options": {}}, headers=H)
    assert bad.status_code == 400 and "base_url" in bad.json()["error"]
    unknown = env.post("/api/providers", json={**template, "id": "x3", "kind": "nope"}, headers=H)
    assert unknown.status_code == 400
    assert env.delete("/api/providers/router", headers=H).json() == {"ok": True}
    assert env.delete("/api/providers/router", headers=H).status_code == 400


def test_provider_check(env):
    signed_in(env)
    assert env.post("/api/providers/fake/check", headers=H).json()["ok"] is True
    env.post("/api/providers", json={"id": "missing", "name": "M", "kind": "command",
             "options": {"command": ["definitely-not-installed-xyz"]}}, headers=H)
    result = env.post("/api/providers/missing/check", headers=H).json()
    assert result["ok"] is False and "not found" in result["message"]


def test_workspace_browser_stays_inside_roots(env):
    signed_in(env)
    roots = env.get("/api/workspaces").json()
    root = roots["entries"][0]["path"]
    (env.tmp / "ws" / "__pycache__").mkdir()  # (Python's: no project to pick, a simulated user found it offered)
    listing = env.get("/api/workspaces", params={"path": root}).json()
    assert [e["name"] for e in listing["entries"]] == ["project"]
    assert env.get("/api/workspaces", params={"path": "/"}).status_code == 403
    made = env.post("/api/workspaces", json={"parent": root, "name": "new-app"}, headers=H).json()
    assert (env.tmp / "ws" / "new-app").is_dir() and made["path"].endswith("new-app")
    assert env.post("/api/workspaces", json={"parent": "/tmp", "name": "x"},
                    headers=H).status_code == 403


def test_reconnecting_event_source_resumes_after_last_event_id(env):
    signed_in(env)
    conv = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    run = env.post(f"/api/conversations/{conv['id']}/messages", json={"text": "hi"},
                   headers=H).json()["run_id"]
    everything = events_of(env, run)
    with env.stream("GET", f"/api/runs/{run}/events", headers={"Last-Event-ID": "3"}) as response:
        rest = [json.loads(line[6:]) for line in response.iter_lines()
                if line.startswith("data: ") and line != "data: {}"]
    wait_idle(env, conv["id"])
    stored = env.get(f"/api/conversations/{conv['id']}").json()["messages"][1]["events"]
    assert everything == stored and rest == stored[4:]


def test_shutdown_stops_running_agents(env):
    signed_in(env)
    conv = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    run = env.post(f"/api/conversations/{conv['id']}/messages", json={"text": "slow"},
                   headers=H).json()["run_id"]
    time.sleep(0.5)
    env.app.state.relay.runs.shutdown(timeout=10)
    assert env.app.state.relay.runs.active == {}
    status = env.app.state.relay.db.one("SELECT status FROM runs WHERE id = ?", (run,))["status"]
    assert status == "interrupted"


def test_stop_right_after_sending_never_starts_the_agent(env, tmp_path):
    signed_in(env)
    marker = tmp_path / "agent-started"
    env.put("/api/providers/fake", json={"id": "fake", "name": "Fake", "kind": "command", "options": {
        "command": [sys.executable, "-c",
                    f"import time; open({str(marker)!r}, 'w').close(); time.sleep(5)"]}}, headers=H)
    relay = env.app.state.relay
    conv = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    # Stop arrives before the worker thread starts the provider.
    original = relay.runs._execute
    def delayed(run, *args):
        relay.runs.stop(run.id)
        original(run, *args)
    relay.runs._execute = delayed
    run = env.post(f"/api/conversations/{conv['id']}/messages", json={"text": "x"},
                   headers=H).json()["run_id"]
    events = events_of(env, run)
    assert events == [{"type": "done", "status": "interrupted"}]
    assert not marker.exists()


def test_search_treats_wildcards_literally(env):
    signed_in(env)
    for title in ["snake_case", "snakeXcase", "100% done", "100 done"]:
        env.post("/api/conversations", json={"provider_id": "fake", "title": title}, headers=H)
    assert [c["title"] for c in env.get("/api/conversations?q=snake_").json()] == ["snake_case"]
    assert [c["title"] for c in env.get("/api/conversations?q=100%").json()] == ["100% done"]


def test_concurrent_sends_start_one_run(env):
    import concurrent.futures
    signed_in(env)
    conv = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    relay = env.app.state.relay
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda _: _try_start(relay, conv["id"]), range(8)))
    assert results.count("started") == 1
    wait_idle(env, conv["id"])


def _try_start(relay, conversation_id):
    from relay.runs import RunError
    row = relay.db.one("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
    try:
        relay.runs.start(row, "slow")
        return "started"
    except RunError:
        return "refused"


def test_parallel_logins_cannot_skip_the_throttle():
    from relay.auth import LoginThrottle
    throttle = LoginThrottle()
    allowed = [throttle.begin("1.2.3.4") == 0 for _ in range(20)]  # all in flight at once
    assert allowed.count(True) == LoginThrottle.FREE_ATTEMPTS


def test_deleting_a_conversation_during_a_run_leaves_nothing_stuck(env):
    signed_in(env)
    conv = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    env.post(f"/api/conversations/{conv['id']}/messages", json={"text": "slow"}, headers=H)
    time.sleep(0.3)
    env.delete(f"/api/conversations/{conv['id']}", headers=H)
    deadline = time.time() + 10
    while env.app.state.relay.runs.active and time.time() < deadline:
        time.sleep(0.1)
    assert env.app.state.relay.runs.active == {}



def git_repo(path):
    import subprocess
    path.mkdir(parents=True)
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "me@x"], ["config", "user.name", "Me"]):
        subprocess.run(["git", *args], cwd=path, check=True)
    (path / "readme.md").write_text("hello\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "start"], cwd=path, check=True)
    return path


def run_turn(env, conv_id, text):
    run = env.post(f"/api/conversations/{conv_id}/messages", json={"text": text}, headers=H).json()["run_id"]
    events_of(env, run)
    wait_idle(env, conv_id)


def test_a_conversation_has_instructions_of_its_own(env):
    """Its own instructions (Relay's SDK sets them) come after its mode's and reach the agent as a mode's do;
    changed, the next turn has the new ones. Without a mode they're Build's, stored as no mode (no switch told)."""
    signed_in(env)
    relay = env.app.state.relay
    conv = env.post("/api/conversations", json={"provider_id": "fake", "instructions": "  Answer in Dutch. "},
                    headers=H).json()
    assert conv["instructions"] == "Answer in Dutch."
    run_turn(env, conv["id"], "which mode")
    assert answers(env, conv["id"])[-1].endswith("mode=build/edit/For this conversation:\nAnswer in Dutch.")
    assert relay.db.one("SELECT agent_mode FROM runs WHERE conversation_id = ?", (conv["id"],))["agent_mode"] is None
    changed = env.patch(f"/api/conversations/{conv['id']}", json={"instructions": "Be brief.", "agent_mode": "plan"},
                        headers=H).json()
    assert changed["instructions"] == "Be brief." and changed["agent_mode"] == "plan"
    run_turn(env, conv["id"], "which mode")
    said = answers(env, conv["id"])[-1]
    assert "mode=plan/read/" in said and said.endswith("\n\nFor this conversation:\nBe brief.")
    assert env.patch(f"/api/conversations/{conv['id']}", json={"instructions": ""}, headers=H).json()["instructions"] is None
    too_long = env.patch(f"/api/conversations/{conv['id']}", json={"instructions": "x" * 8001}, headers=H)
    assert too_long.status_code == 400 and "at most 8000" in too_long.json()["error"]


def test_a_browser_opening_a_running_turn_is_told_when_it_has_caught_up(env):
    """The events so far, then "caught-up" (the browser shows them at once, as a finished turn), then the live
    ones; a stream that's not behind says so first."""
    signed_in(env)
    relay = env.app.state.relay
    project = env.tmp / "ws" / "project"
    conv = env.post("/api/conversations", json={"provider_id": "fake", "mode": "code", "directory": str(project),
                                                "isolate": False}, headers=H).json()
    run = env.post(f"/api/conversations/{conv['id']}/messages", json={"text": "linger"}, headers=H).json()["run_id"]
    deadline = time.time() + 10
    while time.time() < deadline and not any(e.get("delta") == ")" for e in relay.runs.active[run].events):
        time.sleep(0.05)  # (its last word: then it lingers)
    backlog = relay.runs.event_count(run)
    # It ends a moment after the browser asked (the test client hands over a stream once it's done)
    threading.Timer(1, lambda: (project / "done-lingering").write_text("")).start()
    lines = []
    with env.stream("GET", f"/api/runs/{run}/events") as response:
        for line in response.iter_lines():
            if line.startswith(("data: ", "event: ")) and line != "data: {}":
                lines.append(line if line.startswith("event: ") else json.loads(line[6:])["type"])
    assert lines.index("event: caught-up") == backlog and "text" in lines[:backlog]
    assert lines[backlog + 1:] == ["usage", "done", "event: end"]
    wait_idle(env, conv["id"])
    # Finished: all of it, then caught-up; from where it was seen last, caught-up at once
    total = relay.runs.event_count(run)
    with env.stream("GET", f"/api/runs/{run}/events") as response:
        again = [line for line in response.iter_lines() if line.startswith(("id: ", "event: "))]
    assert again[-2:] == ["event: caught-up", "event: end"] and len(again) == total + 2
    with env.stream("GET", f"/api/runs/{run}/events?after={total}") as response:
        assert [line for line in response.iter_lines() if line.startswith("event: ")] == ["event: caught-up", "event: end"]


def test_a_code_task_works_in_place_unless_a_branch_would_have_its_folder(env):
    import subprocess
    signed_in(env)
    repo = git_repo(env.tmp / "ws" / "projects")  # a repository of several projects
    (repo / "website").mkdir()
    (repo / "website" / "index.html").write_text("<h1>new, not committed</h1>\n")

    def task(folder, **extra):
        return env.post("/api/conversations", json={"provider_id": "fake", "mode": "code",
                                                    "directory": str(folder), **extra}, headers=H).json()

    default = task(repo / "website")
    assert default["git"]["mode"] == "direct" and default["directory"] == str(repo / "website")
    # A branch starts from the last commit, which hasn't the folder (or its latest changes): in place then, and
    # said why (a script asking for a branch of its own was none the wiser)
    made = task(repo / "website", isolate=True)
    assert made["git"]["mode"] == "direct" and made["in_place"] == ("the folder has changes that aren't committed, or "
                                                                    "isn't committed yet")
    assert default["in_place"] is None
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "website"], cwd=repo, check=True)
    (repo / "website" / "index.html").write_text("<h1>changed</h1>\n")
    assert task(repo / "website", isolate=True)["git"]["mode"] == "direct"
    subprocess.run(["git", "checkout", "--", "."], cwd=repo, check=True)
    isolated = task(repo / "website", isolate=True)
    assert isolated["git"]["mode"] == "worktree" and pathlib.Path(isolated["directory"]).is_dir()
    assert isolated["in_place"] is None
    subprocess.run(["git", "checkout", "-q", "--detach"], cwd=repo, check=True)
    assert task(repo / "website", isolate=True)["in_place"] == "the repository isn't on a branch (a detached HEAD)"
    (env.tmp / "ws" / "plain").mkdir()
    assert task(env.tmp / "ws" / "plain", isolate=True)["in_place"] == "it isn't in a git repository"


def test_isolated_code_task_review_merge_and_discard(env):
    signed_in(env)
    repo = git_repo(env.tmp / "ws" / "app")
    conv = env.post("/api/conversations", json={"provider_id": "fake", "mode": "code", "isolate": True,
                                                "directory": str(repo)}, headers=H).json()
    assert conv["git"]["mode"] == "worktree" and conv["git"]["base_branch"] == "main"
    assert conv["directory"] != str(repo)  # the agent works in its own worktree

    run_turn(env, conv["id"], "please write")
    assert not (repo / "agent.txt").exists()  # your checkout is untouched
    changes = env.get(f"/api/conversations/{conv['id']}/changes").json()
    assert [f["path"] for f in changes["files"]] == ["agent.txt"]
    assert "+please write" in changes["files"][0]["diff"]
    answer = env.get(f"/api/conversations/{conv['id']}").json()["messages"][1]
    assert answer["changes"] == {"files": 1, "additions": 1, "deletions": 0}

    merged = env.post(f"/api/conversations/{conv['id']}/merge", headers=H).json()
    assert merged == {"ok": True, "merged_into": "main"}
    assert (repo / "agent.txt").read_text() == "please write\n"
    assert env.get(f"/api/conversations/{conv['id']}/changes").json()["files"] == []

    run_turn(env, conv["id"], "write more")  # the task can go on after a merge
    assert [f["path"] for f in env.get(f"/api/conversations/{conv['id']}/changes").json()["files"]] == ["agent.txt"]
    assert env.post(f"/api/conversations/{conv['id']}/discard", headers=H).json() == {"ok": True}
    assert (repo / "agent.txt").read_text() == "please write\n"  # the merged part stays
    assert not pathlib.Path(conv["directory"]).exists()
    refused = env.post(f"/api/conversations/{conv['id']}/messages", json={"text": "x"}, headers=H)
    assert refused.status_code == 400 and "discarded" in refused.json()["error"]


def test_merge_waits_for_the_agent(env):
    signed_in(env)
    repo = git_repo(env.tmp / "ws" / "app")
    conv = env.post("/api/conversations", json={"provider_id": "fake", "mode": "code",
                                                "directory": str(repo)}, headers=H).json()
    env.post(f"/api/conversations/{conv['id']}/messages", json={"text": "slow"}, headers=H)
    assert env.post(f"/api/conversations/{conv['id']}/merge", headers=H).status_code == 409
    wait_idle(env, conv["id"])


def git_log(repo):
    import subprocess
    return subprocess.run(["git", "log", "--format=%s"], cwd=repo, capture_output=True, text=True).stdout.splitlines()


def test_every_turn_is_recorded_without_commits_and_rewind_brings_files_back(env):
    signed_in(env)
    repo = git_repo(env.tmp / "ws" / "app")
    conv = env.post("/api/conversations", json={"provider_id": "fake", "mode": "code",
                                                "directory": str(repo), "isolate": False}, headers=H).json()
    assert conv["git"]["mode"] == "direct" and conv["directory"] == str(repo)
    run_turn(env, conv["id"], "write one")
    (repo / "mine.txt").write_text("my own edit\n")  # the user works in between
    run_turn(env, conv["id"], "write two")
    assert git_log(repo) == ["start"]  # Relay never writes to the user's history by itself
    changes = env.get(f"/api/conversations/{conv['id']}/changes").json()
    assert sorted(f["path"] for f in changes["files"]) == ["agent.txt", "mine.txt"]  # since the task started
    assert changes["checkpoints"]

    second = env.get(f"/api/conversations/{conv['id']}").json()["messages"][3]
    rewound = env.post(f"/api/conversations/{conv['id']}/rewind", json={"run_id": second["run_id"]},
                       headers=H).json()
    assert (repo / "agent.txt").read_text() == "write one\n" and (repo / "mine.txt").exists()
    env.post(f"/api/conversations/{conv['id']}/rewind", json={"checkpoint": rewound["undo"]}, headers=H)
    assert (repo / "agent.txt").read_text() == "write one\nwrite two\n"
    env.post(f"/api/conversations/{conv['id']}/rewind", json={"to_start": True}, headers=H)
    assert not (repo / "agent.txt").exists() and not (repo / "mine.txt").exists()
    assert git_log(repo) == ["start"]
    assert env.post(f"/api/conversations/{conv['id']}/rewind", json={"checkpoint": "0" * 40},
                    headers=H).status_code == 400
    assert env.post(f"/api/conversations/{conv['id']}/discard", headers=H).status_code == 400
    # A commit is the user's own, deliberate step: with their message
    run_turn(env, conv["id"], "write three")
    assert env.post(f"/api/conversations/{conv['id']}/commit", json={"message": ""}, headers=H).status_code == 400
    # (what it takes beyond the task's own changes is said beforehand: the whole folder goes)
    (repo / "theirs.txt").write_text("from another chat, or by hand\n")
    before = env.get(f"/api/conversations/{conv['id']}/changes", headers=H).json()
    assert "theirs.txt" in before["uncommitted"] and "agent.txt" in before["uncommitted"]
    made = env.post(f"/api/conversations/{conv['id']}/commit", json={"message": "Add agent.txt"}, headers=H).json()
    assert made["commit"] and git_log(repo) == ["Add agent.txt", "start"]
    # (committed is seen: nothing left to review, and the chat is out of Needs review)
    after = env.get(f"/api/conversations/{conv['id']}/changes", headers=H).json()
    assert after["files"] == [] and after["uncommitted"] == [] and made["as_relay"] is False
    again = env.post(f"/api/conversations/{conv['id']}/commit", json={"message": "Again"}, headers=H)
    assert again.status_code == 400 and "Nothing to commit" in again.json()["error"]


def test_deleting_a_task_removes_its_worktree(env):
    signed_in(env)
    repo = git_repo(env.tmp / "ws" / "app")
    conv = env.post("/api/conversations", json={"provider_id": "fake", "mode": "code", "isolate": True,
                                                "directory": str(repo)}, headers=H).json()
    env.delete(f"/api/conversations/{conv['id']}", headers=H)
    assert not pathlib.Path(conv["directory"]).exists()


# Attachments


def upload(env, name, data, mime="text/plain"):
    import urllib.parse
    r = env.post("/api/uploads", content=data,
                 headers={**H, "Content-Type": mime, "X-Filename": urllib.parse.quote(name)})
    assert r.status_code == 200, r.text
    return r.json()


def test_upload_fetch_and_send_to_an_agent(env):
    signed_in(env)
    note = upload(env, "../../evil name?.txt", b"hello file")
    assert note["name"] == "evil name_.txt" and note["size"] == 10  # path parts stripped
    image = upload(env, "shot.png", b"\x89PNG fake", "image/png")
    fetched = env.get(f"/api/uploads/{image['id']}")
    assert fetched.content == b"\x89PNG fake" and fetched.headers["content-type"] == "image/png"
    assert fetched.headers["content-disposition"].startswith("inline")
    svg = upload(env, "x.svg", b"<svg/>", "image/svg+xml")
    assert env.get(f"/api/uploads/{svg['id']}").headers["content-disposition"].startswith("attachment")
    # A type that isn't one (it's sent back as a header): from its name instead (a line break was kept as it came)
    for odd in ("text/plain\r\nX-Evil: 1", "x" * 300):
        assert upload(env, "notes.md", b"# hi", odd)["mime"] == "text/markdown"

    conv = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    run = env.post(f"/api/conversations/{conv['id']}/messages",
                   json={"text": "look", "attachments": [note["id"], image["id"]]}, headers=H).json()["run_id"]
    text = "".join(e.get("delta", "") for e in events_of(env, run) if e["type"] == "text")
    assert "Attached files" in text and "evil name_.txt" in text and "shot.png" in text
    wait_idle(env, conv["id"])
    first = env.get(f"/api/conversations/{conv['id']}").json()["messages"][0]
    assert [a["name"] for a in first["attachments"]] == ["evil name_.txt", "shot.png"]
    # Its commands read them too: copied into its folder's WorkBox, at the paths the agent is told (the box
    # mounts none of Relay's data: a file an agent read with a command wasn't there, the user found)
    relay = env.app.state.relay
    paths = [str(relay.uploads.get(a["id"], 1).path) for a in (note, image)]
    assert relay.containers.provided[conv["directory"]] == paths
    env.post(f"/api/conversations/{conv['id']}/messages", json={"text": "and again"}, headers=H)
    wait_idle(env, conv["id"])
    assert relay.containers.provided[conv["directory"]] == paths * 2  # (an earlier turn's, still its: the box makes sure)

    # An upload belongs to one conversation.
    other = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    reuse = env.post(f"/api/conversations/{other['id']}/messages",
                     json={"text": "x", "attachments": [note["id"]]}, headers=H)
    assert reuse.status_code == 400

    folder = pathlib.Path(env.app.state.relay.uploads.get(note["id"], 1).path).parent
    env.delete(f"/api/conversations/{conv['id']}", headers=H)
    assert not folder.exists()
    assert env.get(f"/api/uploads/{note['id']}").status_code == 404


def test_attachments_alone_can_be_sent(env):
    signed_in(env)
    note = upload(env, "a.txt", b"x")
    conv = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    run = env.post(f"/api/conversations/{conv['id']}/messages",
                   json={"text": "", "attachments": [note["id"]]}, headers=H)
    assert run.status_code == 200
    events_of(env, run.json()["run_id"])
    wait_idle(env, conv["id"])


def test_uploads_are_limited_and_private(env, monkeypatch):
    import relay.uploads
    signed_in(env)
    monkeypatch.setattr(relay.uploads, "MAX_BYTES", 10)
    too_big = env.post("/api/uploads", content=b"x" * 11, headers={**H, "X-Filename": "big.bin"})
    assert too_big.status_code == 400 and "at most" in too_big.json()["error"]
    mine = upload(env, "m.txt", b"mine")
    env.post("/api/logout", headers=H)
    from relay.auth import Accounts
    Accounts(env.app.state.relay.db).create_user("other", "other password")
    env.post("/api/login", json={"username": "other", "password": "other password"}, headers=H)
    assert env.get(f"/api/uploads/{mine['id']}").status_code == 404


def test_unsent_uploads_are_purged(env):
    import time as _time
    signed_in(env)
    stale = upload(env, "old.txt", b"old")
    relay = env.app.state.relay
    relay.db.execute("UPDATE uploads SET created = ? WHERE id = ?", (_time.time() - 3 * 86400, stale["id"]))
    folder = relay.uploads.get(stale["id"], 1).path.parent
    relay.uploads.purge_orphans()
    assert not folder.exists() and relay.uploads.get(stale["id"], 1) is None


def test_notification_settings_and_runs_push(env):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    received = []

    class Catch(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            received.append(self.rfile.read(int(self.headers["Content-Length"])).decode())
            self.send_response(200)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Catch)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/topic"
    try:
        signed_in(env)
        assert env.post("/api/notifications/test", headers=H).status_code == 400  # nothing set up
        bad = env.put("/api/settings", json={"notifications": {"ntfy_url": "ntfy.sh/x"}}, headers=H)
        assert bad.status_code == 400 and bad.json()["error"].startswith("The ntfy topic's address must start with")
        saved = env.put("/api/settings", json={"notifications": {
            "ntfy_url": url, "ntfy_token": "secret", "notify_min_seconds": 0}}, headers=H).json()
        assert saved["notifications"]["ntfy_token"] == "********"
        assert saved["workspace_roots"]  # untouched
        env.put("/api/settings", json={"notifications": {"ntfy_token": "********"}}, headers=H)
        assert env.app.state.relay.settings.ntfy_token == "secret"  # mask keeps the token
        assert env.post("/api/notifications/test", headers=H).json() == {"ok": True, "errors": []}

        conv = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
        run = env.post(f"/api/conversations/{conv['id']}/messages", json={"text": "hi"}, headers=H).json()["run_id"]
        events_of(env, run)
        wait_idle(env, conv["id"])
        deadline = time.time() + 5
        while len(received) < 2 and time.time() < deadline:
            time.sleep(0.05)
        assert received[1].startswith("You said: hi")
        listed = env.get("/api/conversations").json()[0]
        assert listed["last_run"]["id"] == run and listed["last_run"]["status"] == "succeeded"
    finally:
        server.shutdown()


def test_a_notification_from_a_script(env):
    """POST /api/notifications (the SDK's notify()): pushed when it can be; when not, why, not an error (a script
    goes on); its category under Settings → Notifications."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    received, clicked = [], []

    class Catch(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            received.append((self.headers["Title"], self.rfile.read(int(self.headers["Content-Length"])).decode()))
            clicked.append(self.headers["Click"])
            self.send_response(200)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Catch)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        signed_in(env)
        said = env.post("/api/notifications", json={"text": "Done"}, headers=H).json()
        assert said["sent"] is False and "set up" in said["why"]
        for wrong in ({}, {"text": "x", "priority": "loud"}, {"text": "x", "url": "example.com"}):
            assert env.post("/api/notifications", json=wrong, headers=H).status_code == 400, wrong
        env.put("/api/settings", json={"notifications": {
            "ntfy_url": f"http://127.0.0.1:{server.server_address[1]}/topic"}}, headers=H)
        assert env.post("/api/notifications", json={"text": "Found one", "title": "Deals", "priority": "Normal"},
                        headers=H).json() == {"sent": True}  # (as the page names it: ntfy's default)
        assert received == [("Deals", "Found one")]
        # A page of Relay's own (a plugin's, say): at Relay's address when it's known, else without a link
        assert env.post("/api/notifications", json={"text": "Digest", "url": "/p/digest"}, headers=H).json()["sent"]
        env.app.state.relay.settings.public_url = "https://relay.example"  # (Settings → General)
        assert env.post("/api/notifications", json={"text": "Digest", "url": "/p/digest"}, headers=H).json()["sent"]
        assert clicked[1:] == [None, "https://relay.example/p/digest"]
        categories = env.get("/api/settings").json()["notifications"]["categories"]
        assert any(c["id"] == "script" and c["push"] for c in categories)
    finally:
        server.shutdown()


def test_lockout_ends_after_the_delay(monkeypatch):
    from relay import auth
    throttle = auth.LoginThrottle()
    for _ in range(5):
        assert throttle.begin("c") == 0
        throttle.failed("c")
    assert throttle.begin("c") > 0  # locked out now
    count, _ = throttle.failures["c"]
    throttle.failures["c"] = (count, time.time() - 1)  # the lockout has passed
    assert throttle.begin("c") == 0  # one new attempt...
    assert throttle.begin("c") == 1.0  # ...at a time


def test_enabling_login_later_keeps_the_local_conversations(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_HOME", str(tmp_path / "data"))
    db = Database(tmp_path / "relay.db")
    open_app = create_app(config.Settings(auth="none", chat_root=str(tmp_path / "c")), db, Registry(tmp_path / "p"))
    with TestClient(open_app) as client:
        client.post("/api/conversations", json={"provider_id": "claude-code"}, headers=H)
    locked = create_app(config.Settings(chat_root=str(tmp_path / "c")), db, Registry(tmp_path / "p"))
    with TestClient(locked) as client:
        assert client.get("/api/state").json()["needs_setup"] is True
        client.post("/api/setup", json={"username": "me", "password": "long enough"}, headers=H)
        assert len(client.get("/api/conversations").json()) == 1
        assert client.get("/api/state").json()["user"]["username"] == "me"


def test_switching_provider_mid_run_is_not_undone(env):
    signed_in(env)
    conv = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    env.post(f"/api/conversations/{conv['id']}/messages", json={"text": "slow"}, headers=H)
    time.sleep(0.3)
    env.patch(f"/api/conversations/{conv['id']}", json={"provider_id": "claude-code"}, headers=H)
    wait_idle(env, conv["id"])
    row = env.app.state.relay.db.one("SELECT provider_id, provider_session FROM conversations WHERE id = ?",
                                     (conv["id"],))
    assert row == {"provider_id": "claude-code", "provider_session": None}


def test_deleting_waits_for_the_agent_to_stop(env):
    signed_in(env)
    repo = git_repo(env.tmp / "ws" / "app")
    conv = env.post("/api/conversations", json={"provider_id": "fake", "mode": "code", "isolate": True,
                                                "directory": str(repo)}, headers=H).json()
    env.post(f"/api/conversations/{conv['id']}/messages", json={"text": "slow write"}, headers=H)
    time.sleep(0.3)
    assert env.delete(f"/api/conversations/{conv['id']}", headers=H).json() == {"ok": True}
    assert env.app.state.relay.runs.active == {}
    assert not pathlib.Path(conv["directory"]).exists()


@pytest.mark.parametrize("name", ["..", ".", "", "../..", "a/.."])
def test_dot_names_are_safe(env, name):
    signed_in(env)
    stored = upload(env, name, b"x")
    assert stored["name"] == "file"


def test_every_page_of_the_app_loads_by_its_address(env):
    """The paths the app navigates to (static/js: navigate) load when reloaded or opened from a link."""
    from relay import config
    static = config.static_dir() / "js"
    used = {m for f in static.glob("*.js") for m in re.findall(r'navigate\([`"](/[a-z]*)', f.read_text())}
    assert {"/c", "/s", "/tasks", "/agentos", "/scripts"} <= used
    for path in used:
        page = path if path in ("/", "/tasks", "/scheduled", "/agentos", "/scripts") else f"{path}/abc123"
        answer = env.get(page)
        assert answer.status_code == 200 and "relay-style-nonce" in answer.text, page
    # A script's own page (the side column's Scripts view opens one there)
    assert "relay-style-nonce" in env.get("/scripts/private/report.py").text


def test_a_page_that_isnt_there_is_relays_own_for_a_browser(env):
    """A browser gets Relay's page (its look, a way back); the API's callers and tools what they got before."""
    page = env.get("/nothing/<here>", headers={"accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
    assert page.status_code == 404 and "text/html" in page.headers["content-type"]
    assert "Page not found" in page.text and '"error-where">/nothing/&lt;here&gt;<' in page.text
    assert "<here>" not in page.text
    assert 'href="/static/css/app.css"' in page.text and "Back to Relay" in page.text
    assert "script-src 'self'" in page.headers["content-security-policy"]
    for path, accept in (("/api/nothing", "text/html"), ("/static/js/nothing.js", "*/*"), ("/nothing", "*/*")):
        other = env.get(path, headers={"accept": accept})
        assert other.status_code in (401, 404) and "Page not found" not in other.text, path
    assert env.get("/static/js/error-page.js").status_code == 200


def test_a_rejected_settings_save_changes_nothing(env):
    signed_in(env)
    r = env.put("/api/settings", json={"notifications": {
        "ntfy_url": "https://ntfy.sh/new", "webhook_url": "not a url"}}, headers=H)
    assert r.status_code == 400
    assert env.app.state.relay.settings.ntfy_url == ""
    r = env.put("/api/settings", json={"notifications": {"ntfy_url": "https://ntfy.sh/new",
                                                         "notify_categories": {"run.done": {"push": "yes"}}}}, headers=H)
    assert r.status_code == 400 and env.app.state.relay.settings.ntfy_url == ""


def test_notification_categories_are_listed_and_chosen(env):
    signed_in(env)
    listed = env.get("/api/settings").json()["notifications"]
    categories = {c["id"]: c for c in listed["categories"]}
    assert list(categories)[:3] == ["run.done", "run.failed", "approval"] and "high" in listed["priorities"]
    assert categories["agents.done"]["push"] is False and categories["run.failed"]["priority"] == "high"
    assert categories["intervention"]["label"] == "Something needs you" and categories["vpn"]["hint"]
    assert env.put("/api/settings", json={"notifications": {"notify_categories": {
        "nope": {"push": True, "priority": "high"}}}}, headers=H).status_code == 400
    assert env.put("/api/settings", json={"notifications": {"notify_categories": {
        "report": {"push": True, "priority": "loud"}}}}, headers=H).status_code == 400
    saved = env.put("/api/settings", json={"notifications": {"notify_categories": {
        "agents.done": {"push": True, "priority": "min"}}}}, headers=H).json()["notifications"]
    agents = next(c for c in saved["categories"] if c["id"] == "agents.done")
    assert (agents["push"], agents["priority"]) == (True, "min")
    env.put("/api/settings", json={"notifications": {"notify_categories": {
        "report": {"push": False, "priority": "low"}}}}, headers=H)
    from relay import config
    assert config.load_settings().notify_categories == {"agents.done": {"push": True, "priority": "min"},
                                                         "report": {"push": False, "priority": "low"}}


def test_tasks_overview_groups(env):
    signed_in(env)
    repo = git_repo(env.tmp / "ws" / "app")
    review = env.post("/api/conversations", json={"provider_id": "fake", "mode": "code", "isolate": True,
                                                  "directory": str(repo), "title": "Needs review"}, headers=H).json()
    run_turn(env, review["id"], "write")
    env.post("/api/conversations", json={"provider_id": "fake", "mode": "code", "isolate": True, "directory": str(repo),
                                         "title": "Clean"}, headers=H)
    gone = env.post("/api/conversations", json={"provider_id": "fake", "mode": "code", "isolate": True,
                                                "directory": str(repo), "title": "Gone"}, headers=H).json()
    env.post(f"/api/conversations/{gone['id']}/discard", headers=H)
    chat = env.post("/api/conversations", json={"provider_id": "fake", "title": "Chatting"}, headers=H).json()
    env.post(f"/api/conversations/{chat['id']}/messages", json={"text": "slow"}, headers=H)

    groups = env.get("/api/tasks").json()
    titles = {name: [t["title"] for t in items] for name, items in groups.items()}
    assert titles == {"running": ["Chatting"], "review": ["Needs review"], "clean": ["Clean"],
                      "discarded": ["Gone"]}
    assert groups["review"][0]["changes"] == {"files": 1, "additions": 1, "deletions": 0}
    wait_idle(env, chat["id"])
    assert env.get("/api/tasks").json()["running"] == []  # a finished chat isn't a task



def test_a_task_in_its_folder_leaves_review_once_marked_as_reviewed(env):
    signed_in(env)
    repo = git_repo(env.tmp / "ws" / "app")
    task = env.post("/api/conversations", json={"provider_id": "fake", "mode": "code", "directory": str(repo),
                                                "title": "In place"}, headers=H).json()
    run_turn(env, task["id"], "write")
    groups = env.get("/api/tasks").json()
    assert [t["title"] for t in groups["review"]] == ["In place"]
    older = env.post("/api/conversations", json={"provider_id": "fake", "mode": "code", "directory": str(repo),
                                                 "title": "Older"}, headers=H).json()
    relay_db = env.app.state.relay.db
    relay_db.execute("UPDATE conversations SET base_commit = (SELECT base_commit FROM conversations WHERE id = ?), "
                     "updated = 0 WHERE id = ?", (task["id"], older["id"]))  # same place, same changes
    (repo / "mine.txt").write_text("edited meanwhile\n")  # the user's own edit is part of what's seen
    assert env.post(f"/api/conversations/{task['id']}/reviewed", headers=H).json() == {"ok": True}
    groups = env.get("/api/tasks").json()
    assert sorted(t["title"] for t in groups["clean"]) == ["In place", "Older"] and groups["review"] == []
    assert env.get(f"/api/conversations/{task['id']}/changes").json()["files"] == []
    run_turn(env, task["id"], "write")  # new changes count from there
    assert [t["title"] for t in env.get("/api/tasks").json()["review"]] == ["In place"]
    clean = git_repo(env.tmp / "ws" / "clean")
    branch = env.post("/api/conversations", json={"provider_id": "fake", "mode": "code", "isolate": True,
                                                  "directory": str(clean)}, headers=H).json()
    assert branch["git"]["mode"] == "worktree"
    assert env.post(f"/api/conversations/{branch['id']}/reviewed", headers=H).status_code == 400  # merge it



def test_continuing_elsewhere_after_it_went_on_by_itself(env):
    """The banner can be stale: the limit note is gone once an answer worked."""
    signed_in(env)
    env.post("/api/providers", json={"id": "spare", "name": "Spare", "kind": "command",
                                     "options": {"command": [sys.executable, "-c", "print('{}')"]}}, headers=H)
    chat = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    run_turn(env, chat["id"], "hello")
    r = env.post(f"/api/conversations/{chat['id']}/continue", json={"provider_id": "spare", "model": "x"}, headers=H)
    assert r.status_code == 200, r.text
    wait_idle(env, chat["id"])
    assert env.get(f"/api/conversations/{chat['id']}").json()["provider_id"] == "spare"



def test_findings_of_relays_review_of_itself(env):
    """Found by agents reviewing Relay through Relay (Claude Code and ChatGPT)."""
    signed_in(env)
    relay = env.app.state.relay
    conv = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    run_turn(env, conv["id"], "hello")
    # Any title downloads
    env.patch(f"/api/conversations/{conv['id']}", json={"title": "日本語の質問"}, headers=H)
    r = env.get(f"/api/conversations/{conv['id']}/export.md")
    assert r.status_code == 200 and "filename*=UTF-8''%E6%97%A5" in r.headers["content-disposition"]
    # A queued message that can't be sent stays queued
    relay.db.execute("INSERT INTO queued_messages (conversation_id, content, created) VALUES (?, 'later', 0)",
                     (conv["id"],))
    qid = relay.db.one("SELECT id FROM queued_messages WHERE conversation_id = ?", (conv["id"],))["id"]
    relay.db.execute("UPDATE conversations SET provider_id = 'nope' WHERE id = ?", (conv["id"],))
    assert env.post(f"/api/conversations/{conv['id']}/queue/{qid}/send", headers=H).status_code >= 400
    assert relay.db.one("SELECT 1 FROM queued_messages WHERE id = ?", (qid,))
    # A computer task moved to a code task works in the project
    relay.db.execute("UPDATE conversations SET provider_id = 'fake', computer_id = 'pc' WHERE id = ?", (conv["id"],))
    moved = env.post(f"/api/conversations/{conv['id']}/to-code",
                     json={"directory": str(env.tmp / "ws" / "project"), "provider_id": "fake"}, headers=H)
    assert moved.status_code == 200, moved.text
    row = relay.db.one("SELECT computer_id, provider_session FROM conversations WHERE id = ?", (conv["id"],))
    assert row["computer_id"] is None and row["provider_session"] is None



def test_a_fork_of_a_task_without_git_shows_its_changes(env):
    signed_in(env)
    folder = env.tmp / "ws" / "plain"
    folder.mkdir(parents=True)
    (folder / "a.txt").write_text("one\n")
    conv = env.post("/api/conversations", json={"provider_id": "fake", "mode": "code", "directory": str(folder)},
                    headers=H).json()
    run_turn(env, conv["id"], "hello")
    (folder / "a.txt").write_text("two\n")
    fork = env.post(f"/api/conversations/{conv['id']}/fork", json={}, headers=H).json()
    changes = env.get(f"/api/conversations/{fork['id']}/changes").json()
    assert "error" not in changes and [f["path"] for f in changes["files"]] == ["a.txt"]



def test_each_answer_shows_what_it_changed(env):
    signed_in(env)
    repo = git_repo(env.tmp / "ws" / "app")
    task = env.post("/api/conversations", json={"provider_id": "fake", "mode": "code", "directory": str(repo)},
                    headers=H).json()
    run_turn(env, task["id"], "write")
    run_turn(env, task["id"], "just a question")
    answers = [m for m in env.get(f"/api/conversations/{task['id']}").json()["messages"] if m["role"] == "assistant"]
    assert answers[0]["changes"]["files"] == 1
    assert not (answers[1]["changes"] or {}).get("files")  # it changed nothing: no "1 file changed" under it


def test_export_as_markdown(env):
    signed_in(env)
    conv = env.post("/api/conversations", json={"provider_id": "fake", "title": "My: chat/1"}, headers=H).json()
    note = upload(env, "notes.txt", b"x")
    run_turn(env, conv["id"], "hello tool")
    run = env.post(f"/api/conversations/{conv['id']}/messages",
                   json={"text": "again", "attachments": [note["id"]]}, headers=H).json()["run_id"]
    events_of(env, run)
    wait_idle(env, conv["id"])
    r = env.get(f"/api/conversations/{conv['id']}/export.md")
    assert r.headers["content-disposition"] == "attachment; filename=\"My chat-1.md\"; filename*=UTF-8''My%20chat-1.md"
    text = r.text
    env.patch(f"/api/conversations/{conv['id']}", json={"title": "Todo list in main.py: add/done/show"}, headers=H)
    named = env.get(f"/api/conversations/{conv['id']}/export.md").headers["content-disposition"]
    assert 'filename="Todo list in main.py add-done-show.md"' in named
    assert text.startswith("# My: chat/1\n")
    assert "## You\n\nhello tool" in text and "## Assistant" in text
    assert "> 🔧 **shell** `ls` ✓" in text
    assert "You said: hello tool" in text and "📎 notes.txt" in text


def test_titles_from_the_agent_unless_renamed(env):
    signed_in(env)
    conv = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    run_turn(env, conv["id"], "give it a title")
    assert env.get(f"/api/conversations/{conv['id']}").json()["title"] == "A title from the agent"

    env.patch(f"/api/conversations/{conv['id']}", json={"title": "Mine"}, headers=H)
    run_turn(env, conv["id"], "another title please")
    assert env.get(f"/api/conversations/{conv['id']}").json()["title"] == "Mine"

    named = env.post("/api/conversations", json={"provider_id": "fake", "title": "Chosen"}, headers=H).json()
    run_turn(env, named["id"], "title?")
    assert env.get(f"/api/conversations/{named['id']}").json()["title"] == "Chosen"


@pytest.fixture
def echo_chat(env):
    """An OpenAI-compatible server that says what it received."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Echo(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if req.get("stream") is False:  # title request
                body = json.dumps({"choices": [{"message": {"content": "Echo chat"}}]}).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)
                return
            text = f"{len(req['messages'])} messages, last: {req['messages'][-1]['content']}"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunk = {"choices": [{"delta": {"content": text}}]}
            self.wfile.write(f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Echo)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    signed_in(env)
    env.post("/api/providers", json={"id": "echo", "name": "Echo", "kind": "openai-compatible", "options": {
        "base_url": f"http://127.0.0.1:{server.server_address[1]}/v1", "models": ["m"],
        "tools": False}}, headers=H)  # the plain chat path; the tool loop has its own tests
    yield env
    server.shutdown()


def test_an_unchanged_edit_takes_the_place_of_the_message(env):
    signed_in(env)
    conv = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    run_turn(env, conv["id"], "Make a website")
    first = env.get(f"/api/conversations/{conv['id']}").json()["messages"][0]
    r = env.post(f"/api/conversations/{conv['id']}/retry", json={"message_id": first["id"], "text": "Make a website"},
                 headers=H)
    assert r.json()["rewound"]
    wait_idle(env, conv["id"])
    assert [m["role"] for m in env.get(f"/api/conversations/{conv['id']}").json()["messages"]] == ["user", "assistant"]


def test_markup_is_no_title(env):
    signed_in(env)
    relay = env.app.state.relay
    conv = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    relay.db.execute("UPDATE conversations SET title = 'Make a website', title_auto = 1 WHERE id = ?", (conv["id"],))
    for title in ("<tool_call>", "</think>", "...", "<｜｜DSML｜｜ calls>", "<|im_end|>", "<function=shell>"):
        relay.runs._auto_title(conv["id"], title)
    assert env.get(f"/api/conversations/{conv['id']}").json()["title"] == "Make a website"
    relay.runs._auto_title(conv["id"], "LLM explainer website")
    assert env.get(f"/api/conversations/{conv['id']}").json()["title"] == "LLM explainer website"
    relay.runs._auto_title(conv["id"], "Plugin trial<|im_end|>")  # (a special token after a title: the title)
    assert env.get(f"/api/conversations/{conv['id']}").json()["title"] == "Plugin trial"


def test_who_pays_when_the_provider_couldnt_tell(env, monkeypatch):
    """A usage event without who pays (opencode, asked in a moment's hiccup, found by a simulated daily user): the
    provider's word on the model, or its last runs'."""
    signed_in(env)
    relay = env.app.state.relay
    conv = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()

    def usage():
        last = env.get(f"/api/conversations/{conv['id']}").json()["messages"][-1]
        stored = relay.db.one("SELECT usage FROM runs WHERE id = ?", (last["run_id"],))["usage"]
        return next(e for e in last["events"] if e["type"] == "usage"), json.loads(stored)

    run_turn(env, conv["id"], "hello")
    assert all("billing" not in u for u in usage())  # (the fake can't tell)
    monkeypatch.setattr(type(relay.registry.provider("fake")), "billing", lambda self, model: "free")
    run_turn(env, conv["id"], "again")
    assert all(u["billing"] == "free" for u in usage())
    monkeypatch.setattr(type(relay.registry.provider("fake")), "billing", lambda self, model: None)
    run_turn(env, conv["id"], "and again")  # (its last run said)
    assert all(u["billing"] == "free" for u in usage())


def test_a_models_test_runs_where_a_run_would(tmp_path):
    """Placed as a run is: in the provider's box, with its login (outside it, Codex found none and got a 401, as a
    repair's agent found), its commands in the WorkBox; and ended as a run is, its WorkBox gone after."""
    ended = []

    class Box:
        def end(self):
            ended.append("box")

    class Run:
        def events(self):
            yield {"type": "text", "delta": "OK"}
            yield {"type": "done", "status": "succeeded"}

        def interrupt(self):
            pass

    class Agent:
        def start(self, context, prompt):
            assert context.box is not None  # (placed before it starts)
            return Run()

    def place(provider, context):
        context.box = Box()
        return lambda: ended.append("after")

    result = health.test(Agent(), "m", tmp_path, place, lambda folder: ended.append(folder.parent))
    assert result["ok"] and ended == ["box", "after", tmp_path]
    assert not any(tmp_path.iterdir())  # (its scratch folder gone too)


def test_a_title_keeps_the_quotes_within_it(env):
    signed_in(env)
    relay = env.app.state.relay
    conv = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    relay.db.execute("UPDATE conversations SET title_auto = 1 WHERE id = ?", (conv["id"],))
    for given, title in (('Adding note "Buy milk and test Relay"', 'Adding note "Buy milk and test Relay"'),
                         ('"Fix the login."', "Fix the login"), ("'Plan a trip'", "Plan a trip"),
                         ("'Don't panic'", "Don't panic"), ('"Fix the login', "Fix the login"),
                         ('"A" or "B"', '"A" or "B"')):
        relay.runs._auto_title(conv["id"], given)
        assert env.get(f"/api/conversations/{conv['id']}").json()["title"] == title


def test_a_chat_with_its_own_title_asks_no_model_for_one(env, monkeypatch):
    signed_in(env)
    provider = env.app.state.relay.registry.provider("fake")
    asked = []
    monkeypatch.setattr(type(provider), "suggest_title",
                        lambda self, prompt, answer, model=None: asked.append(prompt) or "Suggested")
    named = env.post("/api/conversations", json={"provider_id": "fake", "title": "My own"}, headers=H).json()
    env.post(f"/api/conversations/{named['id']}/messages", json={"text": "hello"}, headers=H)
    wait_idle(env, named["id"])
    # (then one without a title: by the time its title came, the first one would have asked too)
    plain = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    env.post(f"/api/conversations/{plain['id']}/messages", json={"text": "hello there"}, headers=H)
    deadline = time.time() + 10
    while env.get(f"/api/conversations/{plain['id']}").json()["title"] != "Suggested":
        assert time.time() < deadline, "no title came"
        time.sleep(0.05)
    assert asked == ["hello there"] and env.get(f"/api/conversations/{named['id']}").json()["title"] == "My own"


def test_the_governor_and_the_users_scripts(env):
    """The Governor lists, reads and runs the user's public scripts, and makes a task that runs one (Relay runs it,
    no agent; the outcome is Relay's); their private scripts aren't its to see or run."""
    from types import SimpleNamespace

    from relay.agentos import AgentOSError
    from relay.delegation import AGENTOS_TOOLS
    signed_in(env)
    relay = env.app.state.relay
    user = relay.db.one("SELECT id FROM users")["id"]
    root = relay.scripts.folder()
    (root / "public" / "report.py").write_text("print('the report')\n")
    (root / "private" / "secret.py").write_text("print('mine')\n")
    assert {"agentos_script_list", "agentos_script_read", "agentos_script_run", "agentos_script_output"} <= {
        t["name"] for t in AGENTOS_TOOLS}
    run = SimpleNamespace(id="gov-run")

    def capability(which, **arguments):
        return env.portal.call(getattr(relay.agentos, "_" + which.replace(".", "_")), run, user, arguments)
    listed = capability("script.list")
    assert "- report.py; never run" in listed and "examples/ask.py (one of Relay's examples)" in listed
    assert "secret" not in listed
    assert capability("script.read", name="report.py") == "public/report.py:\nprint('the report')\n"
    for private in ("private/secret.py", "../private/secret.py", "public/../private/secret.py"):
        with pytest.raises(AgentOSError):
            capability("script.read", name=private)
        with pytest.raises(AgentOSError):
            capability("script.run", name=private)
    told = capability("script.run", name="public/report.py")
    assert "of public/report.py: succeeded" in told and told.endswith("the report\n")
    run_id = relay.db.one("SELECT id FROM script_runs WHERE script = 'public/report.py'")["id"]
    assert capability("script.output", run=run_id[:8]) == told
    # A task that runs it at its time: Relay runs it, as the user, with no agent; how it ended is the outcome
    with pytest.raises(AgentOSError):
        capability("task.create", name="Mine", when="daily 09:00", script="private/secret.py")
    capability("task.create", name="Daily report", when="daily 08:00", script="report.py")
    task = next(t for t in relay.schedules.all(user) if t["name"] == "Daily report")
    assert (task["script"], task["governed"], task["managed"], task["prompt"]) == ("public/report.py", False, True, "")
    assert "Relay runs the script public/report.py" in capability("task.list")
    env.portal.call(relay.scheduler.run_schedule, task)
    deadline = time.time() + 10
    while not relay.schedules.get(user, task["id"]).get("outcome"):
        assert time.time() < deadline, "no outcome came"
        time.sleep(0.05)
    task = relay.schedules.get(user, task["id"])
    assert (task["outcome"], task["outcome_by"]) == ("done", "relay") and task["outcome_note"].startswith("took ")
    assert task["last_conversation"] is None and task["last_script_run"]
    # Run now, over the API (the page, the SDK): the script's run, not a conversation
    ran = env.post(f"/api/schedules/{task['id']}/run", headers=H).json()
    assert set(ran) == {"script_run"} and relay.scripts.run(ran["script_run"])["script"] == "public/report.py"


def test_a_temporary_chat(env, monkeypatch):
    """In no list, without Relay's memory, no title asked for, no notification; kept on request; a chat only."""
    from relay import relay_tools
    signed_in(env)
    relay = env.app.state.relay
    told = []
    monkeypatch.setattr(relay.runs, "notifier", type("Spy", (), {"run_finished": lambda self, f: told.append(f.title)})())
    temp = env.post("/api/conversations", json={"provider_id": "fake", "temporary": True}, headers=H).json()
    assert (temp["temporary"], temp["title"], temp["tools"]["memory"]) == (True, "Temporary chat", False)
    env.post(f"/api/conversations/{temp['id']}/messages", json={"text": "a secret thing"}, headers=H)
    wait_idle(env, temp["id"])
    normal = env.post("/api/conversations", json={"provider_id": "fake", "title": "Normal"}, headers=H).json()
    env.post(f"/api/conversations/{normal['id']}/messages", json={"text": "hello"}, headers=H)
    wait_idle(env, normal["id"])
    deadline = time.time() + 5
    while not told and time.time() < deadline:  # (the notification comes after the run's end)
        time.sleep(0.05)
    assert told == ["Normal"]  # (not the temporary one)
    assert [c["id"] for c in env.get("/api/conversations").json()] == [normal["id"]]
    assert env.get("/api/conversations?q=secret").json() == []
    tasks = env.get("/api/tasks").json()
    assert temp["id"] not in {t["id"] for group in tasks.values() if isinstance(group, list) for t in group}
    row = relay.db.one("SELECT * FROM conversations WHERE id = ?", (temp["id"],))
    assert "memory" in relay_tools.off(row)
    # Only a chat; it can't become temporary, nor move to a folder before it's kept
    for wrong in ({"mode": "code", "directory": str(env.tmp / "ws" / "project")}, {"computer_id": "nope"}):
        refused = env.post("/api/conversations", json={"provider_id": "fake", "temporary": True, **wrong}, headers=H)
        assert refused.status_code == 400 and "temporary chat is a chat" in refused.json()["error"]
    assert env.patch(f"/api/conversations/{normal['id']}", json={"temporary": True}, headers=H).status_code == 400
    moved = env.post(f"/api/conversations/{temp['id']}/to-code", json={"directory": str(env.tmp / "ws" / "project")},
                     headers=H)
    assert moved.status_code == 400 and "Keep this temporary chat first" in moved.json()["error"]
    # Kept: a chat like any other
    kept = env.patch(f"/api/conversations/{temp['id']}", json={"temporary": False}, headers=H).json()
    assert kept["temporary"] is False and kept["tools"]["memory"] is True
    assert kept["title"] == "a secret thing"  # (named as a chat is: not "Temporary chat" for good)
    assert temp["id"] in [c["id"] for c in env.get("/api/conversations").json()]


def test_temporary_chats_left_behind_go(env, monkeypatch):
    signed_in(env)
    relay = env.app.state.relay
    old, recent, kept = (env.post("/api/conversations", json={"provider_id": "fake", "temporary": True},
                                  headers=H).json() for _ in range(3))
    env.patch(f"/api/conversations/{kept['id']}", json={"temporary": False}, headers=H)
    now = time.time()
    relay.db.execute("UPDATE conversations SET updated = ? WHERE id IN (?, ?)", (now - 25 * 3600, old["id"], kept["id"]))
    env.portal.call(relay.scheduler._temporary_gone, now)
    assert env.get(f"/api/conversations/{old['id']}").status_code == 404
    assert env.get(f"/api/conversations/{recent['id']}").status_code == 200  # (used lately)
    assert env.get(f"/api/conversations/{kept['id']}").status_code == 200  # (kept: not temporary any more)
    assert not (env.tmp / "chats" / old["id"]).exists()


def answers(env, conv_id):
    return [m["content"] for m in env.get(f"/api/conversations/{conv_id}").json()["messages"]]


def test_regenerate_and_edit_with_a_chat_provider(echo_chat):
    env = echo_chat
    conv = env.post("/api/conversations", json={"provider_id": "echo"}, headers=H).json()
    run_turn(env, conv["id"], "first")
    run_turn(env, conv["id"], "second")
    assert answers(env, conv["id"])[-1] == "3 messages, last: second"
    assert env.get(f"/api/conversations/{conv['id']}").json()["title"] == "Echo chat"  # model's title

    run = env.post(f"/api/conversations/{conv['id']}/retry", json={}, headers=H).json()
    assert run["rewound"] is True
    events_of(env, run["run_id"])
    wait_idle(env, conv["id"])
    assert answers(env, conv["id"]) == ["first", "1 messages, last: first", "second",
                                        "3 messages, last: second"]  # replaced, not appended

    run = env.post(f"/api/conversations/{conv['id']}/retry", json={"text": "second, edited"},
                   headers=H).json()
    events_of(env, run["run_id"])
    wait_idle(env, conv["id"])
    assert answers(env, conv["id"])[-2:] == ["second, edited", "3 messages, last: second, edited"]
    assert len(env.app.state.relay.db.all("SELECT id FROM runs WHERE conversation_id = ?",
                                          (conv["id"],))) == 2  # replaced runs are gone


def test_retry_with_an_agent_sends_the_message_again(env):
    signed_in(env)
    conv = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    run_turn(env, conv["id"], "do it")
    run = env.post(f"/api/conversations/{conv['id']}/retry", json={}, headers=H).json()
    assert run["rewound"] is False
    events_of(env, run["run_id"])
    wait_idle(env, conv["id"])
    roles = [m["role"] for m in env.get(f"/api/conversations/{conv['id']}").json()["messages"]]
    assert roles == ["user", "assistant", "user", "assistant"]


def test_retry_needs_a_message_and_an_idle_chat(env):
    signed_in(env)
    conv = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    assert env.post(f"/api/conversations/{conv['id']}/retry", json={}, headers=H).status_code == 400
    env.post(f"/api/conversations/{conv['id']}/messages", json={"text": "slow"}, headers=H)
    assert env.post(f"/api/conversations/{conv['id']}/retry", json={}, headers=H).status_code == 409
    wait_idle(env, conv["id"])


# Fork, edit any message, handover


def send(env, conv_id, path, data):
    run = env.post(f"/api/conversations/{conv_id}/{path}", json=data, headers=H).json()
    events_of(env, run["run_id"])
    wait_idle(env, conv_id)
    return run


def test_clear_starts_the_agent_afresh_and_keeps_the_conversation_on_screen(echo_chat):
    """/clear: the agent's history starts after it (a fresh session, nothing handed over); the user
    still sees everything; a retry after it and a fork after it keep to that, a fork from before it doesn't."""
    env = echo_chat
    relay = env.app.state.relay
    conv = env.post("/api/conversations", json={"provider_id": "echo"}, headers=H).json()
    url = f"/api/conversations/{conv['id']}"
    assert "nothing to clear" in env.post(f"{url}/clear", headers=H).json()["error"]
    run_turn(env, conv["id"], "first")
    run_turn(env, conv["id"], "second")
    assert answers(env, conv["id"])[-1] == "3 messages, last: second"
    relay.db.execute("UPDATE conversations SET provider_session = 'old' WHERE id = ?", (conv["id"],))
    cleared = env.post(f"{url}/clear", headers=H)
    assert cleared.status_code == 200, cleared.text
    row = relay.db.one("SELECT * FROM conversations WHERE id = ?", (conv["id"],))
    last = relay.db.one("SELECT max(id) AS id FROM messages WHERE conversation_id = ?", (conv["id"],))["id"]
    assert row["cleared_after"] == last == cleared.json()["cleared_after"] and row["provider_session"] is None
    run_turn(env, conv["id"], "third")
    assert answers(env, conv["id"]) == ["first", "1 messages, last: first", "second", "3 messages, last: second",
                                        "third", "1 messages, last: third"]  # all on screen; the agent: one
    run_turn(env, conv["id"], "fourth")
    assert answers(env, conv["id"])[-1] == "3 messages, last: fourth"
    run = env.post(f"{url}/retry", json={}, headers=H).json()
    events_of(env, run["run_id"])
    wait_idle(env, conv["id"])
    assert answers(env, conv["id"])[-1] == "3 messages, last: fourth"
    assert env.get(url).json()["title"] == "Echo chat"

    # A fork after the clear starts where this one's agent did; one from before it, as it was then
    messages = env.get(url).json()["messages"]
    after = env.post(f"{url}/fork", json={}, headers=H).json()
    assert after["cleared_after"] is not None
    run_turn(env, after["id"], "fifth")
    assert answers(env, after["id"])[-1] == "5 messages, last: fifth"
    before = env.post(f"{url}/fork", json={"message_id": messages[1]["id"]}, headers=H).json()
    assert before["cleared_after"] is None
    run_turn(env, before["id"], "again")
    assert answers(env, before["id"])[-1] == "3 messages, last: again"


def test_clear_delete_history_wipes_the_conversation_for_everyone(echo_chat):
    """/clear delete-history: the messages, their runs and attachments and the subagents' chats go;
    the conversation itself stays, and starts over."""
    env = echo_chat
    relay = env.app.state.relay
    conv = env.post("/api/conversations", json={"provider_id": "echo"}, headers=H).json()
    url = f"/api/conversations/{conv['id']}"
    env.post(f"{url}/messages", json={"text": "first", "attachments": [upload(env, "notes.txt", b"secret notes")["id"]]},
             headers=H)
    wait_idle(env, conv["id"])
    run_turn(env, conv["id"], "second")
    relay.db.execute("INSERT INTO conversations (id, user_id, title, provider_id, mode, directory, parent_conversation, "
                     "created, updated) SELECT 'kid', user_id, 'sub', 'echo', 'chat', directory, id, 0, 0 "
                     "FROM conversations WHERE id = ?", (conv["id"],))
    env.post(f"{url}/clear", headers=H)  # (a plain clear first: delete-history goes past it too)
    wiped = env.post(f"{url}/clear", json={"delete_history": True}, headers=H)
    assert wiped.status_code == 200 and wiped.json()["cleared_after"] is None
    assert env.get(url).json()["messages"] == []
    for table in ("messages", "runs", "uploads"):
        assert not relay.db.one(f"SELECT 1 FROM {table} WHERE conversation_id = ?", (conv["id"],)), table
    assert not relay.db.one("SELECT 1 FROM conversations WHERE id = 'kid'")
    assert relay.db.one("SELECT provider_session FROM conversations WHERE id = ?", (conv["id"],))["provider_session"] is None
    assert "nothing to clear" in env.post(f"{url}/clear", json={"delete_history": True}, headers=H).json()["error"]
    run_turn(env, conv["id"], "again")
    assert answers(env, conv["id"]) == ["again", "1 messages, last: again"]


def test_editing_an_earlier_message_with_a_chat_provider(echo_chat):
    env = echo_chat
    conv = env.post("/api/conversations", json={"provider_id": "echo"}, headers=H).json()
    for text in ("first", "second", "third"):
        run_turn(env, conv["id"], text)
    first = env.get(f"/api/conversations/{conv['id']}").json()["messages"][0]
    run = send(env, conv["id"], "retry", {"message_id": first["id"], "text": "first, edited"})
    assert run["rewound"] is True
    assert answers(env, conv["id"]) == ["first, edited", "1 messages, last: first, edited"]


def test_editing_an_agent_message_hands_the_conversation_over(env):
    signed_in(env)
    conv = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    run_turn(env, conv["id"], "use a tool")
    run_turn(env, conv["id"], "second")
    before = env.app.state.relay.db.one("SELECT provider_session FROM conversations WHERE id = ?",
                                        (conv["id"],))["provider_session"]
    run = send(env, conv["id"], "retry", {"text": "second, edited"})
    assert run["rewound"] is True
    messages = env.get(f"/api/conversations/{conv['id']}").json()["messages"]
    assert [m["content"].split(" (session")[0] for m in messages][2] == "second, edited"
    answer = messages[3]["content"]
    # A fresh session, which got the conversation so far (tool calls too, but not the old turn).
    assert not answer.endswith(f"(session {before})")
    assert "this conversation started earlier" in answer
    assert "User: use a tool" in answer and "(tool: shell `ls`)" in answer
    assert "User: second\n" not in answer and answer.endswith(")")
    assert len(messages) == 4


def test_switching_provider_hands_the_conversation_over(echo_chat):
    env = echo_chat
    conv = env.post("/api/conversations", json={"provider_id": "echo"}, headers=H).json()
    run_turn(env, conv["id"], "remember kiwi")
    env.patch(f"/api/conversations/{conv['id']}", json={"provider_id": "fake"}, headers=H)
    run_turn(env, conv["id"], "what fruit?")
    data = env.get(f"/api/conversations/{conv['id']}").json()
    answer = data["messages"][-1]["content"]
    assert "User: remember kiwi" in answer and "Assistant: 1 messages, last: remember kiwi" in answer
    # Each answer says who gave it, so the UI can mark the switch.
    assert [m["run_provider"] for m in data["messages"] if m["role"] == "assistant"] == ["echo", "fake"]
    run_turn(env, conv["id"], "and now?")  # the agent's own session from here on
    assert "this conversation started earlier" not in answers(env, conv["id"])[-1]


def test_long_handovers_leave_out_the_middle_and_point_to_a_file(tmp_path, monkeypatch):
    from relay import handover
    monkeypatch.setenv("RELAY_HOME", str(tmp_path))
    monkeypatch.setattr(handover, "MAX_CHARS", 300)
    history = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"message {i} " + "x" * 80}
               for i in range(10)]
    sent = handover.prepare("conv", history, "next")
    assert "message 0 " in sent.prompt and "message 9 " in sent.prompt and "message 4 " not in sent.prompt
    assert sent.file and "message 4 " in sent.file.read_text()
    assert str(sent.file) in sent.prompt and sent.prompt.endswith("next")
    assert handover.prepare("conv", [], "next").prompt == "next"


def test_fork_a_chat_up_to_an_answer(echo_chat):
    env = echo_chat
    conv = env.post("/api/conversations", json={"provider_id": "echo"}, headers=H).json()
    note = upload(env, "note.txt", b"hello")
    run = env.post(f"/api/conversations/{conv['id']}/messages",
                   json={"text": "first", "attachments": [note["id"]]}, headers=H).json()
    events_of(env, run["run_id"])
    wait_idle(env, conv["id"])
    run_turn(env, conv["id"], "second")
    first_answer = env.get(f"/api/conversations/{conv['id']}").json()["messages"][1]

    fork = env.post(f"/api/conversations/{conv['id']}/fork", json={"message_id": first_answer["id"]},
                    headers=H).json()
    assert fork["title"].endswith("(fork)") and fork["forked_from"] == conv["id"]
    data = env.get(f"/api/conversations/{fork['id']}").json()
    assert data["origin"] == {"id": conv["id"], "title": env.get(f"/api/conversations/{conv['id']}").json()["title"]}
    assert [m["content"] for m in data["messages"]] == ["first", first_answer["content"]]
    copied = data["messages"][0]["attachments"][0]
    assert copied["name"] == "note.txt" and copied["id"] != note["id"]
    assert env.get(f"/api/uploads/{copied['id']}").content == b"hello"

    run_turn(env, fork["id"], "other")
    assert answers(env, fork["id"])[-1] == "3 messages, last: other"
    assert answers(env, conv["id"])[-1] == "3 messages, last: second"  # the original goes on as it was
    env.delete(f"/api/conversations/{conv['id']}", headers=H)
    assert env.get(f"/api/uploads/{copied['id']}").content == b"hello"  # the fork keeps its own copy


def test_fork_an_agent_chat(env):
    signed_in(env)
    conv = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    run_turn(env, conv["id"], "remember kiwi")
    fork = env.post(f"/api/conversations/{conv['id']}/fork", json={}, headers=H).json()
    assert fork["directory"] == conv["directory"]  # agents find their sessions per folder
    run_turn(env, fork["id"], "what fruit?")
    # The command kind can't copy sessions, so the fork gets the conversation handed over.
    assert "User: remember kiwi" in answers(env, fork["id"])[-1]
    env.delete(f"/api/conversations/{conv['id']}", headers=H)
    assert pathlib.Path(fork["directory"]).is_dir()  # the shared chat folder stays for the fork
    refused = env.post(f"/api/conversations/{fork['id']}/fork", json={"message_id": 999999}, headers=H)
    assert refused.status_code == 404


def test_fork_a_code_task_on_its_own_branch(env):
    signed_in(env)
    repo = git_repo(env.tmp / "ws" / "app")
    conv = env.post("/api/conversations", json={"provider_id": "fake", "mode": "code", "isolate": True,
                                                "directory": str(repo)}, headers=H).json()
    run_turn(env, conv["id"], "please write")
    fork = env.post(f"/api/conversations/{conv['id']}/fork", json={}, headers=H).json()
    assert fork["git"]["mode"] == "worktree" and fork["git"]["branch"] != conv["git"]["branch"]
    assert fork["directory"] != conv["directory"]
    # The fork starts from the task's files as they were, and counts changes from the same base.
    assert (pathlib.Path(fork["directory"]) / "agent.txt").read_text() == "please write\n"
    assert [f["path"] for f in env.get(f"/api/conversations/{fork['id']}/changes").json()["files"]] == ["agent.txt"]
    run_turn(env, fork["id"], "write in the fork")
    assert "write in the fork" not in (pathlib.Path(conv["directory"]) / "agent.txt").read_text()
    env.post(f"/api/conversations/{conv['id']}/discard", headers=H)
    assert "write in the fork" in (pathlib.Path(fork["directory"]) / "agent.txt").read_text()


# Checkpoints


def test_a_folder_without_git_shows_changes_and_restores_turns(env):
    signed_in(env)
    folder = env.tmp / "ws" / "project"
    (folder / "notes.txt").write_text("x\n")
    conv = env.post("/api/conversations", json={"provider_id": "fake", "mode": "code",
                                                "directory": str(folder)}, headers=H).json()
    assert conv["git"]["mode"] == "none" and not (folder / ".git").exists()
    described = env.get(f"/api/conversations/{conv['id']}/changes").json()
    assert described["checkpoints"] is True and described["files"] == []

    run_turn(env, conv["id"], "write one")
    run_turn(env, conv["id"], "write two")
    changes = env.get(f"/api/conversations/{conv['id']}/changes").json()
    assert [f["path"] for f in changes["files"]] == ["agent.txt"] and "+write two" in changes["files"][0]["diff"]
    messages = env.get(f"/api/conversations/{conv['id']}").json()["messages"]
    assert all(m["checkpoint"] for m in messages if m["role"] == "assistant")

    # Back to before the second answer, and undo that again
    second = messages[3]
    restored = env.post(f"/api/conversations/{conv['id']}/rewind", json={"run_id": second["run_id"]},
                        headers=H).json()
    assert (folder / "agent.txt").read_text() == "write one\n"
    assert restored["changes"] == {"files": 1, "additions": 1, "deletions": 0}
    env.post(f"/api/conversations/{conv['id']}/rewind", json={"checkpoint": restored["undo"]}, headers=H)
    assert (folder / "agent.txt").read_text() == "write one\nwrite two\n"

    # Edit the first message and put the files back as they were before it
    run = env.post(f"/api/conversations/{conv['id']}/retry",
                   json={"message_id": messages[0]["id"], "text": "no writing", "restore_files": True},
                   headers=H).json()
    events_of(env, run["run_id"])
    wait_idle(env, conv["id"])
    assert not (folder / "agent.txt").exists() and (folder / "notes.txt").exists()
    assert len(env.get(f"/api/conversations/{conv['id']}").json()["messages"]) == 2

    bad = env.post(f"/api/conversations/{conv['id']}/rewind", json={"checkpoint": "0" * 40}, headers=H)
    assert bad.status_code == 400
    env.delete(f"/api/conversations/{conv['id']}", headers=H)
    assert not (env.tmp / "data" / "relay" / "checkpoints" / f"{conv['id']}.git").exists()
    assert (folder / "notes.txt").exists()  # the folder itself stays


def test_checkpoints_in_a_task_on_its_own_branch(env):
    signed_in(env)
    repo = git_repo(env.tmp / "ws" / "app")
    conv = env.post("/api/conversations", json={"provider_id": "fake", "mode": "code",
                                                "directory": str(repo)}, headers=H).json()
    run_turn(env, conv["id"], "write this")
    answer = env.get(f"/api/conversations/{conv['id']}").json()["messages"][1]
    env.post(f"/api/conversations/{conv['id']}/rewind", json={"run_id": answer["run_id"]}, headers=H)
    assert not (pathlib.Path(conv["directory"]) / "agent.txt").exists()
    assert env.get(f"/api/conversations/{conv['id']}/changes").json()["files"] == []


# Modes


def test_modes_can_be_added_used_and_removed(echo_chat):
    env = echo_chat
    assert [m["id"] for m in env.get("/api/modes").json()] == ["build", "plan", "readonly", "fast", "brainstorm"]
    critic = env.post("/api/modes", json={"name": "Critic", "instructions": "Find the weak spots.",
                                          "access": "read"}, headers=H).json()
    assert critic["id"] == "critic" and critic["access"] == "read"
    assert env.post("/api/modes", json={"name": "Critic"}, headers=H).status_code == 400  # taken
    assert env.post("/api/modes", json={"name": "x", "access": "root"}, headers=H).status_code == 400
    assert env.put("/api/modes/plan", json={"name": "Mine"}, headers=H).status_code == 400  # built in
    env.put("/api/modes/critic", json={"name": "Critic", "instructions": "Be harsh.", "access": "read"},
            headers=H)

    # A command agent gets the mode in its environment
    agent = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    run_turn(env, agent["id"], "which mode?")
    assert answers(env, agent["id"])[-1].endswith("mode=build/edit/")
    assert env.patch(f"/api/conversations/{agent['id']}", json={"agent_mode": "nope"}, headers=H).status_code == 400
    env.patch(f"/api/conversations/{agent['id']}", json={"agent_mode": "critic"}, headers=H)
    run_turn(env, agent["id"], "which mode now?")
    assert answers(env, agent["id"])[-1].endswith("mode=critic/read/Be harsh.")
    data = env.get(f"/api/conversations/{agent['id']}").json()
    assert data["agent_mode"] == "critic"
    assert [m["run_mode"] for m in data["messages"] if m["role"] == "assistant"] == [None, "critic"]

    # A chat API gets the instructions as a system message
    chat = env.post("/api/conversations", json={"provider_id": "echo"}, headers=H).json()
    env.patch(f"/api/conversations/{chat['id']}", json={"agent_mode": "critic"}, headers=H)
    run_turn(env, chat["id"], "hi")
    assert answers(env, chat["id"])[-1] == "2 messages, last: hi"

    env.delete("/api/modes/critic", headers=H)
    assert env.get(f"/api/conversations/{agent['id']}").json()["agent_mode"] is None
    assert env.delete("/api/modes/build", headers=H).status_code == 400


def test_built_in_modes_other_than_build_and_plan_can_be_replaced(echo_chat):
    env = echo_chat
    modes = {m["id"]: m for m in env.get("/api/modes").json()}
    assert modes["brainstorm"]["access"] == "read" and "three" in modes["brainstorm"]["instructions"]
    assert modes["fast"]["tools_only"] and not modes["brainstorm"]["tools_only"]
    assert env.put("/api/modes/fast", json={"name": "Mine"}, headers=H).status_code == 400
    assert env.post("/api/modes", json={"id": "plan", "name": "My plan"}, headers=H).status_code == 400
    mine = env.post("/api/modes", json={"id": "brainstorm", "name": "My brainstorm", "instructions": "Two views."},
                    headers=H)
    assert mine.status_code == 200, mine.text
    listed = [m for m in env.get("/api/modes").json() if m["id"] == "brainstorm"]
    assert len(listed) == 1 and listed[0]["name"] == "My brainstorm"
    agent = env.post("/api/conversations", json={"provider_id": "fake", "agent_mode": "brainstorm"}, headers=H).json()
    run_turn(env, agent["id"], "which mode?")
    assert answers(env, agent["id"])[-1].endswith("mode=brainstorm/edit/Two views.")
    env.delete("/api/modes/brainstorm", headers=H)  # the built-in one is back, and still in use
    assert env.get(f"/api/conversations/{agent['id']}").json()["agent_mode"] == "brainstorm"
    run_turn(env, agent["id"], "which mode now?")
    assert "mode=brainstorm/read/This mode is for a question" in answers(env, agent["id"])[-1]



def test_modes_have_icons_people_can_change(echo_chat):
    env = echo_chat
    modes = {m["id"]: m for m in env.get("/api/modes").json()}
    assert (modes["build"]["icon"], modes["plan"]["icon"], modes["fast"]["icon"], modes["brainstorm"]["icon"]) \
        == ("hammer", "map", "bolt", "bulb")
    mine = env.post("/api/modes", json={"name": "Critic", "access": "read"}, headers=H).json()
    assert mine["icon"] == "shield"  # by access, until one is picked
    assert env.put("/api/modes/plan/icon", json={"icon": "eye"}, headers=H).json()["icon"] == "eye"  # built-in too
    assert env.put("/api/modes/critic/icon", json={"icon": "bug"}, headers=H).json()["icon"] == "bug"
    assert env.put("/api/modes/critic/icon", json={"icon": "<svg>"}, headers=H).status_code == 400
    assert env.put("/api/modes/nope/icon", json={"icon": "bug"}, headers=H).status_code == 400
    modes = {m["id"]: m for m in env.get("/api/modes").json()}
    assert modes["plan"]["icon"] == "eye" and modes["critic"]["icon"] == "bug"
    assert env.put("/api/modes/plan/icon", json={"icon": None}, headers=H).json()["icon"] == "map"
    env.delete("/api/modes/critic", headers=H)
    again = env.post("/api/modes", json={"name": "Critic"}, headers=H).json()
    assert again["icon"] == "tool"  # a new mode by the same name starts afresh


def test_a_skill_can_be_called_like_a_command(env):
    signed_in(env)
    conv = env.post("/api/conversations", json={"provider_id": "fake", "mode": "code",
                                                "directory": str(env.tmp / "ws" / "project"),
                                                "isolate": False}, headers=H).json()
    run_turn(env, conv["id"], "/code-review only the parser")
    data = env.get(f"/api/conversations/{conv['id']}").json()
    assert data["messages"][0]["content"] == "/code-review only the parser"  # as typed
    sent = data["messages"][1]["content"]
    assert 'use my "code-review" skill' in sent and "# Code review" in sent and "name: code-review" not in sent
    assert "What I ask: only the parser" in sent and "This is a code task in" in sent
    run_turn(env, conv["id"], "/not-a-skill hello")
    assert answers(env, conv["id"])[-1].startswith("You said: /not-a-skill hello")


# Subagents


def mcp(env, token, method, params=None, request_id=1):
    r = env.post(f"/mcp/{token}", json={"jsonrpc": "2.0", "id": request_id, "method": method,
                                        "params": params or {}})
    return r.status_code, (r.json() if r.content else None)


def test_relays_own_tools_can_be_turned_off_per_conversation(echo_chat):
    """Web, memory, subagents, computers: what's off isn't among its agent's MCP tools, a call to one is
    refused, and its note doesn't offer it; turned on again, it's there for the next call. The agent's own
    program keeps its tools. A computer task keeps its computer."""
    from relay.tools_script import note
    env = echo_chat
    relay = env.app.state.relay
    env.post("/api/computers", json={"name": "laptop"}, headers=H)
    conv = env.post("/api/conversations", json={"provider_id": "fake", "tools": {"web": False, "subagents": False}},
                    headers=H).json()
    assert conv["tools"] == {"web": False, "memory": True, "subagents": False, "computers": True}
    env.post(f"/api/conversations/{conv['id']}/messages", json={"text": "linger"}, headers=H)
    token = relay.db.one("SELECT tools_token FROM conversations WHERE id = ?", (conv["id"],))["tools_token"]
    try:
        listed = {t["name"] for t in mcp(env, token, "tools/list")[1]["result"]["tools"]}
        assert not {"search", "fetch", "delegate", "wait", "tell", "agents"} & listed
        assert {"memory_add", "computers", "computer_run"} <= listed
        refused = mcp(env, token, "tools/call", {"name": "delegate", "arguments": {"task": "x"}})[1]["result"]
        assert refused["isError"] and "isn't one of this conversation's" in refused["content"][0]["text"]
        changed = env.patch(f"/api/conversations/{conv['id']}", json={"tools": {"web": True, "memory": False,
                                                                              "computers": False}}, headers=H).json()
        assert changed["tools"] == {"web": True, "memory": False, "subagents": False, "computers": False}
        listed = {t["name"] for t in mcp(env, token, "tools/list")[1]["result"]["tools"]}
        assert {"search", "fetch"} <= listed and not {"memory_add", "computers", "delegate"} & listed
    finally:
        let_go(env, relay.db.one("SELECT * FROM conversations WHERE id = ?", (conv["id"],)))
    for wrong, said in (({"internet": False}, "No such kind of Relay's tools: internet"),
                        ({"web": "no"}, "tools: {kind: true or false}")):
        refused = env.patch(f"/api/conversations/{conv['id']}", json={"tools": wrong}, headers=H)
        assert refused.status_code == 400 and said in refused.json()["error"]
    computer = relay.db.one("SELECT id FROM computers")["id"]
    task = env.post("/api/conversations", json={"provider_id": "fake", "computer_id": computer}, headers=H).json()
    refused = env.patch(f"/api/conversations/{task['id']}", json={"tools": {"computers": False}}, headers=H)
    assert refused.status_code == 400 and "keeps Relay's computer tools" in refused.json()["error"]
    # What its agent is told leaves out what's off
    told = note(pathlib.Path("/x"), via_mcp=True, search=False, fetch=False, subagents=False)
    assert "web fetch" not in told and "hand work to other AI agents" not in told and "Relay's delegate" not in told


def test_an_agent_hands_work_to_subagents(echo_chat):
    env = echo_chat
    relay = env.app.state.relay
    conv = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    run_id = env.post(f"/api/conversations/{conv['id']}/messages", json={"text": "mcp? slow"},
                      headers=H).json()["run_id"]
    token = relay.db.one("SELECT tools_token FROM conversations WHERE id = ?", (conv["id"],))["tools_token"]
    assert token and len(token) > 30
    script = relay.delegation.script_for(relay.db.one("SELECT * FROM conversations WHERE id = ?", (conv["id"],)),
                                         relay.runs.local_url)
    # Test the mode, not effective access: root can write even a read-only file.
    mode = script.stat().st_mode
    assert script.name == "relay" and not (mode & 0o222) and mode & 0o111
    assert token in script.read_text() and "/mcp/" in script.read_text()

    def tool(tool_name, **arguments):
        return mcp(env, token, "tools/call", {"name": tool_name, "arguments": arguments})[1]["result"]

    assert "delegate …" in tool("help")["content"][0]["text"]  # `relay help`: what the note leaves out
    assert "Debian container" in tool("help", topic="container")["content"][0]["text"]
    listed = tool("agents")["content"][0]["text"]
    assert "- fake (you)" in listed and "- echo:" in listed and "- plan: Plan, read-only" in listed
    assert "subagents work at once" in listed  # the limits as they're set
    started = tool("delegate", task="hello sub", provider="echo", label="Echo helper")
    assert not started["isError"] and started["content"][0]["text"].startswith("Started subagent")
    tool("delegate", task="mcp?", provider="fake")  # a subagent gets no tools of its own (the web search only)
    wrong = tool("delegate", task="x", provider="nope")
    assert wrong["isError"] and "The providers are: " in wrong["content"][0]["text"]
    answers_text = ""
    for _ in range(20):
        answers_text += tool("wait")["content"][0]["text"]
        if "Still working" not in answers_text.split("===")[-1] and answers_text.count("===") >= 4:
            break
    assert "1 messages, last: hello sub" in answers_text and "mcp=no tools=yes" in answers_text
    assert tool("wait")["content"][0]["text"] == "No subagents to wait for."

    events_of(env, run_id)
    wait_idle(env, conv["id"])
    data = env.get(f"/api/conversations/{conv['id']}").json()
    answer = data["messages"][-1]["content"]
    assert "mcp=yes tools=yes" in answer  # the agent's own environment has Relay's tools and endpoint
    cards = {e["id"]: e for e in data["messages"][-1]["events"] if e["type"] == "subagent"}
    assert [c["status"] for c in cards.values()] == ["succeeded", "succeeded"]
    first = next(c for c in cards.values() if c["label"] == "Echo helper")
    assert first["provider"] == "echo" and first["run"]

    # The subagent's conversation is hidden from the lists but can be opened
    listed_ids = [c["id"] for c in env.get("/api/conversations").json()]
    assert first["id"] not in listed_ids and conv["id"] in listed_ids
    child = env.get(f"/api/conversations/{first['id']}").json()
    assert child["parent"]["id"] == conv["id"] and child["messages"][0]["content"] == "hello sub"

    assert mcp(env, token, "tools/call", {"name": "agents"})[0] == 404  # no run going: the token does nothing
    assert env.post("/mcp/not-a-token", json={}).status_code == 404
    env.delete(f"/api/conversations/{conv['id']}", headers=H)
    assert env.get(f"/api/conversations/{first['id']}").status_code == 404
    assert not script.exists()


def test_a_code_subagent_works_in_a_copy_and_hands_its_changes_over(echo_chat):
    env = echo_chat
    relay = env.app.state.relay
    project = env.tmp / "ws" / "project"
    (project / "notes.txt").write_text("mine\n")
    conv = env.post("/api/conversations", json={"provider_id": "fake", "mode": "code", "directory": str(project),
                                                "isolate": False}, headers=H).json()
    run_id = env.post(f"/api/conversations/{conv['id']}/messages", json={"text": "linger"},
                      headers=H).json()["run_id"]
    token = relay.db.one("SELECT tools_token FROM conversations WHERE id = ?", (conv["id"],))["tools_token"]

    def tool(name, **arguments):
        return mcp(env, token, "tools/call", {"name": name, "arguments": arguments})[1]["result"]["content"][0]["text"]

    started = tool("delegate", task="write one", provider="fake", wait=True)
    assert "It works in a copy of your folder" not in started  # (that's in delegate's own answer, not wait's)
    assert "Its changes are in your folder now (agent.txt)." in started
    assert (project / "agent.txt").read_text().startswith("write one")
    first = relay.db.one("SELECT * FROM conversations WHERE parent_conversation = ?", (conv["id"],))
    assert first["directory"] != str(project) and first["directory"].endswith("/project")
    assert (pathlib.Path(first["directory"]) / "notes.txt").read_text() == "mine\n"

    plan = tool("delegate", task="write four", provider="fake", mode="plan")
    assert "It works in a copy" not in plan  # read-only: in the folder itself

    # The caller changes the same file while a subagent works: nothing is applied, the caller decides.
    started = tool("delegate", task="write two linger", provider="fake", label="B")
    assert "It works in a copy of your folder" in started
    short = re.search(r"Started subagent (\w+)", started).group(1)
    copy = pathlib.Path(relay.db.one("SELECT directory FROM conversations WHERE id LIKE ?", (short + "%",))["directory"])
    with open(project / "agent.txt", "a") as mine:
        mine.write("the caller's own line\n")
    (copy / "done-lingering").write_text("")  # (that's one of its changes too)
    text = tool("wait", ids=[short])
    assert "so they weren't applied" in text and "agent.txt" in text and "+write two" in text
    assert "the caller's own line" in (project / "agent.txt").read_text()
    assert not (project / "done-lingering").exists()
    assert tool("merge", id="nope").startswith("No subagent")
    copies = forks.folders()
    assert any(copies.iterdir())
    merged = tool("merge", id=short)  # with done-lingering, the caller's run ends now
    assert "conflict markers" in merged and "<<<<<<< yours" in (project / "agent.txt").read_text()

    events_of(env, run_id)
    wait_idle(env, conv["id"])
    assert not any(copies.iterdir())  # gone with the caller's run
    assert len(relay.containers.discarded) == 2


def test_a_subagents_changes_that_werent_handed_over_wait_for_the_user(echo_chat):
    env = echo_chat
    relay = env.app.state.relay
    project = env.tmp / "ws" / "project"
    conv = env.post("/api/conversations", json={"provider_id": "fake", "mode": "code", "directory": str(project),
                                                "isolate": False}, headers=H).json()
    run_id = env.post(f"/api/conversations/{conv['id']}/messages", json={"text": "linger"},
                      headers=H).json()["run_id"]
    token = relay.db.one("SELECT tools_token FROM conversations WHERE id = ?", (conv["id"],))["tools_token"]

    def tool(name, **arguments):
        return mcp(env, token, "tools/call", {"name": name, "arguments": arguments})[1]["result"]["content"][0]["text"]

    started = tool("delegate", task="write it linger", provider="fake")
    short = re.search(r"Started subagent (\w+)", started).group(1)
    child = relay.db.one("SELECT * FROM conversations WHERE id LIKE ?", (short + "%",))
    copy = pathlib.Path(child["directory"])
    (project / "agent.txt").write_text("the caller's own\n")  # so the subagent's agent.txt conflicts
    (copy / "done-lingering").write_text("")
    assert "weren't applied" in tool("wait", ids=[short])
    (project / "done-lingering").write_text("")  # the caller ends without deciding
    events_of(env, run_id)
    wait_idle(env, conv["id"])

    # Its changes stay, in its conversation, for the user: Apply puts them in (with markers).
    assert copy.is_dir()
    shown = env.get(f"/api/conversations/{child['id']}").json()["copy"]
    assert shown == {"source": str(project.resolve()), "files": ["agent.txt", "done-lingering"]}
    # fork.json may be stale (a move whose rewrite of it failed): Apply goes where the caller is now.
    stale = forks.folders() / child["id"] / "fork.json"
    stale.write_text(stale.read_text().replace(str(project.resolve()), str(env.tmp / "old-place")))
    applied = env.post(f"/api/conversations/{child['id']}/copy", json={"action": "apply"}, headers=H).json()
    assert applied["conflicts"] == ["agent.txt"]
    assert "<<<<<<< yours" in (project / "agent.txt").read_text()
    assert not copy.exists() and env.get(f"/api/conversations/{child['id']}").json()["copy"] is None

    # Going on with it gives it a new copy, from the caller's files as they are now.
    (project / "agent.txt").write_text("resolved\n")
    run_turn(env, child["id"], "write more")
    assert (copy / "agent.txt").read_text().startswith("resolved\nwrite more")
    assert env.get(f"/api/conversations/{child['id']}").json()["copy"]["files"] == ["agent.txt"]
    assert env.post(f"/api/conversations/{child['id']}/copy", json={"action": "discard"}, headers=H).json()["ok"]
    assert not copy.exists() and (project / "agent.txt").read_text() == "resolved\n"


def test_a_run_shows_the_containers_being_prepared_and_can_be_stopped(env, monkeypatch):
    import threading
    signed_in(env)
    relay = env.app.state.relay
    ready = threading.Event()
    relay.containers.checked = None  # Relay just started: podman checked, the image built, first
    monkeypatch.setattr(relay.containers, "check", lambda: ready.wait(30), raising=False)
    conv = env.post("/api/conversations", json={"provider_id": "fake", "mode": "code",
                                                "directory": str(env.tmp / "ws" / "project"), "isolate": False},
                    headers=H).json()
    run_id = env.post(f"/api/conversations/{conv['id']}/messages", json={"text": "hi"}, headers=H).json()["run_id"]
    for _ in range(50):
        events = relay.runs.active[run_id].events if run_id in relay.runs.active else []
        if any(e.get("type") == "tool_call" for e in events):
            break
        time.sleep(0.1)
    assert "Preparing the agents' container" in events[0]["input"]["description"]
    assert env.post(f"/api/runs/{run_id}/stop", headers=H).status_code == 200
    wait_idle(env, conv["id"])
    ready.set()
    message = env.get(f"/api/conversations/{conv['id']}").json()["messages"][-1]
    assert message["status"] == "interrupted"
    assert [e["output"] for e in message["events"] if e["type"] == "tool_result"] == ["Stopped"]


def test_deleting_a_chat_removes_its_folder_and_container(env):
    signed_in(env)
    relay = env.app.state.relay
    conv = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    run_turn(env, conv["id"], "hello")
    folder = relay.db.one("SELECT directory FROM conversations WHERE id = ?", (conv["id"],))["directory"]
    env.delete(f"/api/conversations/{conv['id']}", headers=H)
    assert not pathlib.Path(folder).exists()
    assert relay.containers.discarded == [folder]


def test_relay_in_a_container_finds_the_tools_of_the_agent_in_its_folder(echo_chat):
    from relay.runs import RunError
    from relay.tools_script import tools_dir
    env = echo_chat
    relay = env.app.state.relay
    project = env.tmp / "ws" / "project"
    (project / "sub").mkdir(parents=True)

    def start(folder):
        conv = env.post("/api/conversations", json={"provider_id": "fake", "mode": "code", "directory": str(folder),
                                                    "isolate": False}, headers=H).json()
        env.post(f"/api/conversations/{conv['id']}/messages", json={"text": "linger"}, headers=H)
        for _ in range(50):
            token = relay.db.one("SELECT tools_token FROM conversations WHERE id = ?", (conv["id"],))["tools_token"]
            if token and relay.runs.active_run_for(conv["id"]):
                return conv["id"], tools_dir() / token / "relay"
            time.sleep(0.1)
        raise AssertionError("no run")

    outer, outer_script = start(project)
    inner, inner_script = start(project / "sub")
    try:
        assert relay.runs.tools_script_for(str(project / "src")) == outer_script
        assert relay.runs.tools_script_for(str(project / "sub" / "x")) == inner_script  # the most specific
        with pytest.raises(RunError, match="No agent"):
            relay.runs.tools_script_for(str(env.tmp / "elsewhere"))
        third, _ = start(project / "sub")
        with pytest.raises(RunError, match="More than one"):
            relay.runs.tools_script_for(str(project / "sub"))
        # Only for Relay's own containers (on this machine): not from the network
        assert env.get("/tools-for", params={"folder": str(project)}).status_code == 403
    finally:
        (project / "done-lingering").write_text("")
        (project / "sub" / "done-lingering").write_text("")
        for conv_id in (outer, inner):
            wait_idle(env, conv_id)


# AgentOS


@pytest.fixture(autouse=True)
def fake_agents_are_confined(monkeypatch):
    """The tests' fake agent is a command provider, which runs on the machine: here it stands in for a
    confined one, so AgentOS may use it (test_agentos_keeps_to_confined_providers has the real rule)."""
    from relay.providers.command import CommandProvider
    monkeypatch.setattr(CommandProvider, "CONFINED", True)


def agentos_on(env, **extra):
    loop = env.put("/api/fallback-loops", json={"loops": [{"name": "Governor", "entries": "fake"}]},
                   headers=H).json()[0]
    assert env.put("/api/agentos", json={"enabled": True, "loop": "nope"}, headers=H).status_code == 400
    settings = env.put("/api/agentos", json={"enabled": True, "loop": loop["id"], **extra}, headers=H).json()
    assert settings["enabled"] and settings["loop"] == loop["id"]
    return loop


def test_agentos_wakes_the_governor_only_when_something_is_due(echo_chat):
    env = echo_chat
    relay = env.app.state.relay
    agentos_on(env)
    task = env.post("/api/schedules", json={"name": "Atlas", "prompt": "Add the new election data to the atlas",
                                            "when": {"kind": "interval", "minutes": 4320}, "governed": True},
                    headers=H).json()
    assert task["governed"] and task["next_run"]
    user = relay.db.one("SELECT id FROM users")["id"]
    now = time.time()
    relay.agentos._store(user, {**relay.agentos.settings(user), "last_wake": now})  # its round was just now
    env.portal.call(relay.scheduler.tick, now + 60)
    assert relay.agentos.governor(user) is None  # nothing due: no model call at all

    relay.db.execute("UPDATE schedules SET next_run = ? WHERE id = ?", (now - 1, task["id"]))
    env.portal.call(relay.scheduler.tick, now + 60)
    governor = relay.agentos.governor(user)
    assert governor and governor["agent_mode"] == "governor" and governor["mode"] == "code"
    wait_idle(env, governor["id"])
    briefing = env.get(f"/api/conversations/{governor['id']}").json()["messages"][0]["content"]
    assert "1 task is due" in briefing and "Atlas" in briefing and "Add the new election data" in briefing
    assert "Budget, per wake-up (each has its own, also when a task spans several): at most 4 agents" in briefing
    after = relay.schedules.get(user, task["id"])
    assert after["last_conversation"] == governor["id"] and after["next_run"] > now + 3600  # next in 72 h
    assert governor["id"] not in [c["id"] for c in env.get("/api/conversations").json()]  # not a chat of yours

    assert "Project folders are in:" in briefing
    # The user switched the Governor to another agent meanwhile: its approved one starts a session of its own
    relay.db.execute("UPDATE conversations SET provider_id = 'echo', provider_session = 'claude-session' WHERE id = ?",
                     (governor["id"],))
    settings = relay.agentos.settings(user)
    relay.agentos._store(user, {**settings, "last_wake": now - 17 * 3600})  # its round is due
    env.portal.call(relay.scheduler.tick, now + 60)
    wait_idle(env, governor["id"])
    messages = env.get(f"/api/conversations/{governor['id']}").json()["messages"]
    assert "regular round" in [m for m in messages if m["role"] == "user"][-1]["content"]
    switched = relay.db.one("SELECT provider_id, provider_session FROM conversations WHERE id = ?", (governor["id"],))
    assert switched["provider_id"] == "fake" and switched["provider_session"] != "claude-session"

    env.put("/api/agentos", json={"enabled": False}, headers=H)
    relay.db.execute("UPDATE schedules SET next_run = ? WHERE id = ?", (now - 1, task["id"]))
    env.portal.call(relay.scheduler.tick, now + 60)
    assert relay.schedules.get(user, task["id"])["last_error"] == "AgentOS is off, so nobody carried it out"


def test_the_governor_steers_managed_chats_through_its_capabilities(echo_chat):
    import threading
    env = echo_chat
    relay = env.app.state.relay
    agentos_on(env)
    user = relay.db.one("SELECT id FROM users")["id"]
    run_turn(env, env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()["id"], "mine")
    mine = env.get("/api/conversations").json()[0]
    assert not mine["managed"]
    env.post("/api/agentos/wake", headers=H)
    governor = relay.agentos.governor(user)
    wait_idle(env, governor["id"])
    run_id = env.post(f"/api/conversations/{governor['id']}/messages", json={"text": "linger"}, headers=H).json()["run_id"]
    token = relay.db.one("SELECT tools_token FROM conversations WHERE id = ?", (governor["id"],))["tools_token"]
    relay.agentos.budgets[run_id] = {"started": time.time(), "agents": 0, "user": user, "max_agents": 2,
                                     "max_cost": 100.0}

    def tool(tool_name, **arguments):
        result = mcp(env, token, "tools/call", {"name": tool_name, "arguments": arguments})[1]["result"]
        return result["content"][0]["text"], result.get("isError", False)

    try:
        names = [t["name"] for t in mcp(env, token, "tools/list")[1]["result"]["tools"]]
        assert "agentos_chat_create" in names and "agents" in names and "delegate" not in names
        assert "agentos_production_promote" in names and tool("agentos_service_list")[0].startswith("No services")
        assert "No service" in tool("agentos_workbox_clone", service="nope", provider="echo")[0]
        assert "agentos_production_promote" in names and tool("agentos_service_list")[0].startswith("No services")
        assert "No service" in tool("agentos_workbox_clone", service="nope", provider="echo")[0]
        text, error = tool("agentos_chat_create", provider="echo", title="Atlas research", message="hello agentos")
        assert not error and "Created managed chat" in text and "hello agentos" in text, text
        listed = env.get("/api/conversations").json()
        made = next(c for c in listed if c["title"] == "Atlas research")
        assert made["managed"] and governor["id"] not in [c["id"] for c in listed]
        assert made["id"][:8] in tool("agentos_chat_list")[0]
        assert "hello agentos" in tool("agentos_chat_read", id=made["id"][:8])[0]
        text, error = tool("agentos_chat_send", id=made["id"][:8], message="and again")
        assert not error and "and again" in text
        text, error = tool("agentos_chat_send", id=made["id"][:8], message="once more")
        assert error and "budget of this wake-up is used up (2 agents)" in text
        # The user's own chat: not without asking; a managed fork when they allow it
        assert tool("agentos_chat_read", id=mine["id"][:8])[1]
        assert "No managed chat" in tool("agentos_chat_delete", id=mine["id"][:8])[0]

        def fork_answering(choice):
            result = {}
            thread = threading.Thread(target=lambda: result.update(zip(("text", "error"),
                                                                       tool("agentos_chat_fork", id=mine["id"][:8]),
                                                                       strict=True)))
            thread.start()
            asked = None
            for _ in range(200):
                active = relay.runs.active.get(run_id)
                asked = next(iter(active.pending.values()), None) if active else None
                if asked or not thread.is_alive():
                    break
                time.sleep(0.02)
            assert asked and asked["event"]["action"] == "AgentOS: look into your chat"
            env.post(f"/api/runs/{run_id}/permission", json={"id": asked["event"]["id"], "decision": choice}, headers=H)
            thread.join(10)
            return result["text"], result["error"]

        text, error = fork_answering("denied")
        assert error and "didn't allow" in text
        text, error = fork_answering("once")
        assert not error and "as managed" in text
        forks = [c for c in env.get("/api/conversations").json() if c["managed"] and c["title"].endswith("(fork)")]
        assert len(forks) == 1 and not env.get(f"/api/conversations/{mine['id']}").json()["managed"]
        # The user's fork of a managed chat is theirs (in their own list, not the Governor's to touch)
        theirs = env.post(f"/api/conversations/{forks[0]['id']}/fork", json={}, headers=H).json()
        assert not theirs["managed"] and "No managed chat" in tool("agentos_chat_read", id=theirs["id"][:8])[0]
        assert not tool("agentos_chat_delete", id=forks[0]["id"][:8])[1]
        # A new project: its folder is made, in a workspace root only
        new = env.tmp / "ws" / "new-site"
        text, error = tool("agentos_chat_create", provider="fake", folder=str(new), title="New site")
        assert not error and new.is_dir(), text
        assert tool("agentos_chat_create", provider="fake", folder=str(env.tmp / "elsewhere" / "x"))[1]
        assert not (env.tmp / "elsewhere").exists()
        # Tasks of its own: made, changed, deleted; the user's own it only carries out
        project = env.tmp / "ws" / "project"
        text, error = tool("agentos_task_create", name="Check the atlas", message="See whether the data is fresh",
                           when="every 72 hours", folder=str(project))
        assert not error and text.startswith("Made task"), text
        made_task = next(t for t in env.get("/api/schedules").json() if t["name"] == "Check the atlas")
        assert made_task["managed"] and made_task["governed"] and made_task["directory"] == str(project)
        assert "made by you" in tool("agentos_task_list")[0]
        assert tool("agentos_task_create", name="x", message="y", when="sometimes")[1]
        assert tool("agentos_task_create", name="x", message="y", when="daily 08:00", folder="/etc")[1]
        text, error = tool("agentos_task_update", id=made_task["id"][:8], enabled=False, when="weekly mon 07:00")
        assert not error and "; off)" in text, text
        users = env.post("/api/schedules", json={"name": "Mine", "prompt": "mine", "when": {"kind": "daily", "time": "08:00"},
                                                 "governed": True}, headers=H).json()
        text, error = tool("agentos_task_delete", id=users["id"][:8])
        assert error and "is the user's" in text
        assert not tool("agentos_task_delete", id=made_task["id"][:8])[1]
        assert [t["name"] for t in env.get("/api/schedules").json() if t["kind"] != "system"] == ["Mine"]
        text, error = tool("agentos_notify_intervention", message="The atlas source is gone; where now?")
        assert not error
        state = env.get("/api/agentos").json()
        [open_one] = [i for i in state["interventions"] if i["resolved"] is None]
        assert open_one["text"].startswith("The atlas source is gone") and state["governor"]["running"]
        env.post(f"/api/agentos/interventions/{open_one['id']}/resolve", headers=H)
        assert env.get("/api/agentos").json()["interventions"][0]["resolved"]
    finally:
        (pathlib.Path(governor["directory"]) / "done-lingering").write_text("")
        wait_idle(env, governor["id"])
    # Only the Governor has them
    other = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    other_run = env.post(f"/api/conversations/{other['id']}/messages", json={"text": "linger"}, headers=H).json()
    other_token = relay.db.one("SELECT tools_token FROM conversations WHERE id = ?", (other["id"],))["tools_token"]
    result = mcp(env, other_token, "tools/call", {"name": "agentos_chat_list", "arguments": {}})[1]["result"]
    assert result["isError"] and "isn't one of this conversation's" in result["content"][0]["text"]
    relay.runs.stop(other_run["run_id"])
    wait_idle(env, other["id"])


def test_relays_memory_every_agent_gets_and_adds_to(echo_chat, monkeypatch):
    """Settings → Memory: the user's own and the agents' memories, general and per project; each agent is
    told them with Relay's note and adds to them with Relay's tools; off, none of that."""
    env = echo_chat
    relay = env.app.state.relay
    project = env.tmp / "ws" / "project"
    conv = env.post("/api/conversations", json={"provider_id": "fake", "mode": "code", "directory": str(project),
                                                "isolate": False}, headers=H).json()
    seen = []
    provider = type(relay.registry.provider("fake"))
    started = provider.start
    monkeypatch.setattr(provider, "start", lambda self, context, prompt: seen.append(context) or started(self, context, prompt))

    def told(count):  # (the run starts its agent in a thread of its own)
        deadline = time.time() + 5
        while len(seen) < count and time.time() < deadline:
            time.sleep(0.02)
        return seen[count - 1]
    listed = env.get("/api/memory").json()
    assert listed["enabled"] and listed["memories"] == [] and str(project) in [p["key"] for p in listed["projects"]]
    assert env.post("/api/memory", json={"text": " "}, headers=H).status_code == 400
    assert env.post("/api/memory", json={"text": "x", "project": "/not/mine"}, headers=H).status_code == 400
    env.post("/api/memory", json={"text": "Talks Dutch"}, headers=H)
    listed = env.post("/api/memory", json={"text": "Runs on port 8600", "project": str(project)}, headers=H).json()
    assert [(m["text"], m["label"], m["by"]) for m in listed["memories"]] == [
        ("Talks Dutch", "General", "you"), ("Runs on port 8600", str(project), "you")]
    # An agent is told them (with how to add), and adds, changes and forgets with Relay's tools
    env.post(f"/api/conversations/{conv['id']}/messages", json={"text": "linger"}, headers=H)
    token = relay.db.one("SELECT tools_token FROM conversations WHERE id = ?", (conv["id"],))["tools_token"]
    note = told(1).tools_note
    assert "Relay's memory" in note and "Talks Dutch" in note and "About this project" in note and "8600" in note
    assert '`relay memory add --category C "…"`' in note and seen[-1].memory in note

    def tool(name, **arguments):
        result = mcp(env, token, "tools/call", {"name": name, "arguments": arguments})[1]["result"]
        return result["content"][0]["text"], result.get("isError", False)

    names = [t["name"] for t in mcp(env, token, "tools/list")[1]["result"]["tools"]]
    assert {"memory_add", "memory_change", "memory_forget", "memory_list"} <= set(names)
    text, error = tool("memory_add", text="Deploys with make release", project=True, category="setup")
    assert not error and text.startswith("Remembered [") and "(this project, setup)" in text
    short = text.split("[")[1].split("]")[0]
    made = next(m for m in env.get("/api/memory").json()["memories"] if m["short"] == short)
    assert made["project"] == str(project) and made["by"].startswith("Fake") and made["conversation_title"]
    assert made["category"] == "setup"
    assert tool("memory_change", id=short, text="Deploys with make ship")[0].endswith("(setup): Deploys with make ship")
    assert "(decisions)" in tool("memory_change", id=short, category="decisions")[0]
    assert tool("memory_change", id=short)[1] and tool("memory_add", text="x", category="hobby")[1]
    assert "make ship" in tool("memory_list")[0] and "Talks Dutch" in tool("memory_list")[0]
    assert tool("memory_forget", id=short)[0].startswith("Forgot")
    assert tool("memory_forget", id="zzzzzz")[1]
    # The user's own word is theirs: an agent can't change or forget it
    dutch = next(m for m in env.get("/api/memory").json()["memories"] if m["text"] == "Talks Dutch")
    text, error = tool("memory_forget", id=dutch["short"])
    assert error and "the user's own" in text and "(general, other, the user's own) Talks Dutch" in tool("memory_list")[0]
    # The Governor's own memory is its own: no other agent sees it, changes it or forgets it
    governors = env.post("/api/memory", json={
        "text": "Politiclone's cache stays where it is", "project": "governor", "category": "decisions"}, headers=H)
    assert governors.status_code == 400  # (no Governor yet: not one of the user's places)
    relay.db.execute("INSERT INTO conversations (id, user_id, title, provider_id, mode, directory, agentos_role, "
                     "created, updated) VALUES ('gov', 1, 'AgentOS Governor', 'fake', 'code', ?, 'governor', 0, 0)",
                     (str(env.tmp),))
    kept = env.post("/api/memory", json={"text": "Politiclone's cache stays where it is", "project": "governor",
                                         "category": "decisions"}, headers=H).json()
    its = next(m for m in kept["memories"] if m["project"] == "governor")
    assert its["label"] == "The Governor" and kept["projects"][0]["key"] == "governor"
    assert "Politiclone" not in tool("memory_list")[0] and tool("memory_forget", id=its["short"])[1]
    # Agents only read when the user keeps it themselves
    assert env.put("/api/memory", json={"agents_add": False}, headers=H).json()["agents_add"] is False
    text, error = tool("memory_add", text="Likes tea", category="preferences")
    assert error and "keeps Relay's memory themselves" in text and "Talks Dutch" in tool("memory_list")[0]
    assert env.put("/api/memory", json={"agents_add": True}, headers=H).json()["agents_add"] is True
    let_go(env, conv)
    # A chat has no project to remember things about
    chat = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    env.post(f"/api/conversations/{chat['id']}/messages", json={"text": "linger"}, headers=H)
    chat_token = relay.db.one("SELECT tools_token FROM conversations WHERE id = ?", (chat["id"],))["tools_token"]
    answer = mcp(env, chat_token, "tools/call", {"name": "memory_add", "arguments": {"text": "x", "project": True}})
    assert "no project" in answer[1]["result"]["content"][0]["text"]
    assert "About this project" not in told(2).memory
    let_go(env, chat)
    # The user moves one, changes and forgets them in Settings
    dutch = next(m for m in env.get("/api/memory").json()["memories"] if m["text"] == "Talks Dutch")
    moved = env.patch(f"/api/memory/{dutch['id']}", json={"project": str(project), "text": "Talks Dutch here"},
                      headers=H).json()
    assert ("Talks Dutch here", str(project)) in [(m["text"], m["project"]) for m in moved["memories"]]
    assert env.delete("/api/memory/nothing", headers=H).status_code == 404
    # Off: agents are told nothing and can't add; what's kept stays
    assert env.put("/api/memory", json={"enabled": "no"}, headers=H).status_code == 400
    assert env.put("/api/memory", json={"enabled": False}, headers=H).json()["enabled"] is False
    env.post(f"/api/conversations/{conv['id']}/messages", json={"text": "linger"}, headers=H)
    assert told(3).memory is None and "Relay's memory" not in seen[-1].tools_note
    assert "memory_add" not in [t["name"] for t in mcp(env, token, "tools/list")[1]["result"]["tools"]]
    let_go(env, conv)
    listed = env.get("/api/memory").json()
    assert len(listed["memories"]) == 3 and [c["id"] for c in listed["categories"]][:2] == ["preferences", "about"]
    # Forgotten and put back (Undo) as it was: who added it, in which chat, since when
    from relay import memory as memory_
    memory_.add(relay.db, 1, "Builds with make", str(project), "Fake · m", conv["id"], "setup", created=1000.0)
    agents = next(m for m in env.get("/api/memory").json()["memories"] if m["by"] != "you")
    env.delete(f"/api/memory/{agents['id']}", headers=H)
    back = env.post("/api/memory", json={"text": agents["text"], "project": agents["project"], "category": agents["category"],
                                         "restore": agents}, headers=H).json()
    again = next(m for m in back["memories"] if m["text"] == agents["text"])
    assert (again["by"], again["conversation_id"], again["created"], again["category"]) == (
        agents["by"], agents["conversation_id"], agents["created"], agents["category"])
    # Delete all: one place's, or everything
    assert env.delete("/api/memory?project=governor", headers=H).json()["deleted"] == 1
    cleared = env.delete("/api/memory", headers=H).json()
    assert cleared["deleted"] == 3 and cleared["memories"] == []


def test_the_governor_is_asked_once_to_move_its_notes_into_its_memory(echo_chat):
    """It had notes.md (112 KB live), read every wake-up; its own memory comes with every briefing instead.
    Asked while its memory is empty and notes.md is long, at most once a day; not with its memory in use."""
    from relay import agentos as agentos_module
    from relay import memory
    env = echo_chat
    relay = env.app.state.relay
    user = relay.db.one("SELECT id FROM users")["id"]
    agentos = relay.agentos
    notes = agentos_module.notebook(user) / "notes.md"
    notes.parent.mkdir(parents=True, exist_ok=True)
    notes.write_text("# Notes\n" + "the user wants politiclone's cache left alone\n" * 200)
    brief = lambda later=0: agentos.briefing(user, [], time.time() + later, "a round", agentos.settings(user))  # noqa: E731
    memory.add(relay.db, user, "Uses Dutch", memory.GOVERNOR, "GPT · sol")
    assert "you have a memory of your own" not in brief()  # (in use already)
    memory.clear(relay.db, user)
    asked = brief()
    assert "First: you have a memory of your own now" in asked and "Your notes.md is 9 KB" in asked
    assert agentos.settings(user)["notes_moved"] and "you have a memory" not in brief()  # (not again the same day)
    assert "you have a memory of your own" in brief(later=90000)  # (that wake-up didn't do it: the next day)
    memory.add(relay.db, user, "Politiclone's cache stays", memory.GOVERNOR, "GPT · sol", category="decisions")
    assert "you have a memory of your own" not in brief(later=200000)  # (it did)
    assert env.put("/api/agentos", json={"notes_moved": None}, headers=H).json()["notes_moved"]  # (not the user's to set)


def governor_lingering(env, relay):
    """The Governor woken, then working (lingering) on a run of the test's: (governor, run id, tool)."""
    user = relay.db.one("SELECT id FROM users")["id"]
    env.post("/api/agentos/wake", headers=H)
    governor = relay.agentos.governor(user)
    wait_idle(env, governor["id"])
    run_id = env.post(f"/api/conversations/{governor['id']}/messages", json={"text": "linger"}, headers=H).json()["run_id"]
    token = relay.db.one("SELECT tools_token FROM conversations WHERE id = ?", (governor["id"],))["tools_token"]

    def tool(tool_name, **arguments):
        result = mcp(env, token, "tools/call", {"name": tool_name, "arguments": arguments})[1]["result"]
        return result["content"][0]["text"], result.get("isError", False)
    return governor, run_id, tool


def let_go(env, conversation):
    (pathlib.Path(conversation["directory"]) / "done-lingering").write_text("")
    wait_idle(env, conversation["id"])


def test_odd_input_is_refused_not_crashed_on(env):
    """What a fuzz of every route and tool found (Relay 2.10): each failed deep inside, with an error 500; a NaN even
    made a chat that couldn't be opened any more. Now said, as a 400 (or the tool's error)."""
    signed_in(env)
    relay = env.app.state.relay
    conv = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    raw = {**H, "Content-Type": "application/json"}

    def refused(method, path, said, data=None, content=None, status=400):
        r = env.request(method, path, json=data, content=content, headers=raw)
        assert r.status_code == status and said in r.json()["error"], (path, r.status_code, r.text)

    refused("PATCH", f"/api/conversations/{conv['id']}", "model is text", {"model": ["x"]})
    refused("POST", "/api/providers", "kind is text", {"id": "k", "name": "K", "kind": {}, "options": {}})
    refused("POST", "/api/memory", "NUL character", {"text": "a\x00b"})
    refused("POST", f"/api/conversations/{conv['id']}/timed", "NaN isn't a number", content='{"max": NaN}')
    assert env.get(f"/api/conversations/{conv['id']}").status_code == 200  # (before: an error 500 for good)
    refused("GET", "/api/workspaces?path=%00", "Not a directory", status=403)
    refused("POST", "/api/workspaces", "Couldn't make that folder", {"parent": str(env.tmp / "ws"), "name": "x" * 300})
    refused("PUT", "/api/limits", "server must be an object", {"server": True})
    refused("PUT", "/api/subagents", "allowed is a list", {"allowed": 5})
    # A number JSON hasn't got in what a provider said (a cost, say): null in the answer, not a chat that fails
    env.post(f"/api/conversations/{conv['id']}/messages", json={"text": "hello"}, headers=H)
    wait_idle(env, conv["id"])
    run = relay.db.one("SELECT id FROM runs WHERE conversation_id = ?", (conv["id"],))["id"]
    relay.db.execute("INSERT INTO run_events (run_id, seq, event) VALUES (?, 9999, ?)",
                     (run, '{"type": "usage", "cost": NaN}'))
    got = env.get(f"/api/conversations/{conv['id']}")
    assert got.status_code == 200 and got.json()["messages"][-1]["events"][-1] == {"type": "usage", "cost": None}
    # A tool's arguments with a NUL in them
    env.post(f"/api/conversations/{conv['id']}/messages", json={"text": "linger"}, headers=H)
    token = relay.db.one("SELECT tools_token FROM conversations WHERE id = ?", (conv["id"],))["tools_token"]
    try:
        result = mcp(env, token, "tools/call", {"name": "memory_add", "arguments": {"text": "a\x00b"}})[1]["result"]
        assert result["isError"] and "NUL characters" in result["content"][0]["text"]
    finally:
        let_go(env, conv)


def test_a_scheduled_task_or_timed_request_at_its_edges(env):
    """Run now on a task whose provider was removed said nothing at all (a 200 without a chat); a /timed request
    took any length (years)."""
    signed_in(env)
    env.post("/api/providers", json={"id": "gone", "name": "Gone", "kind": "command",
                                     "options": {"command": ["true"]}}, headers=H)
    task = env.post("/api/schedules", json={"name": "T", "prompt": "p", "provider_id": "gone", "when": "daily 08:00"},
                    headers=H).json()
    env.delete("/api/providers/gone", headers=H)
    ran = env.post(f"/api/schedules/{task['id']}/run", json={}, headers=H)
    assert ran.status_code == 409 and "No provider 'gone'" in ran.json()["error"], ran.text
    conv = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    timed = env.post(f"/api/conversations/{conv['id']}/timed", json={"max": 99999}, headers=H)
    assert timed.status_code == 400 and "At most a week" in timed.json()["error"], timed.text


def test_a_short_id_that_fits_two_is_said_not_guessed(env):
    """A service by its name or its id's first characters, a subagent by its id's: the whole name or id wins; a
    prefix that fits two is said (a service was "No service"; a subagent's merge or tell took the first)."""
    from types import SimpleNamespace

    from relay.delegation import ToolError, _subagent
    from relay.production import ProductionError
    signed_in(env)
    relay = env.app.state.relay
    user = relay.db.one("SELECT id FROM users")["id"]
    for sid, name, port in (("abc111", "abc", 29001), ("abc222", "shop", 29002), ("fff111", "x", 29003),
                            ("fff222", "y", 29004)):
        relay.db.execute("INSERT INTO services (id, user_id, name, command, public_port, created) VALUES (?, ?, ?, 'x', "
                         "?, 0)", (sid, user, name, port))
    assert relay.production.get(user, "abc")["id"] == "abc111"  # (its name, though "abc" starts another's id too)
    assert relay.production.get(user, "abc2")["id"] == "abc222" and relay.production.get(user, "shop")["id"] == "abc222"
    with pytest.raises(ProductionError, match="More than one service"):
        relay.production.get(user, "fff")
    with pytest.raises(ProductionError, match="No service"):
        relay.production.get(user, "zzz")
    mine = {i: SimpleNamespace(id=i) for i in ("aa11", "aa22", "aa2")}
    assert _subagent(mine, "aa1").id == "aa11" and _subagent(mine, "aa2").id == "aa2" and _subagent(mine, "b") is None
    with pytest.raises(ToolError, match="More than one subagent"):
        _subagent(mine, "aa")


def test_a_turn_going_on_keeps_its_agent_whatever_the_chat_is_set_to_since(env):
    """A user saw "Now with …" above an AgentOS agent's turn after the chat's model was changed while it worked: the
    turn went on as it started. The API says who answers now (running_agent): the run's; the chat's setting is the
    next turn's."""
    signed_in(env)
    relay = env.app.state.relay
    conv = env.post("/api/conversations", json={"provider_id": "fake", "model": "m1"}, headers=H).json()
    env.post(f"/api/conversations/{conv['id']}/messages", json={"text": "linger"}, headers=H)
    try:
        env.patch(f"/api/conversations/{conv['id']}", json={"model": ""}, headers=H)
        got = env.get(f"/api/conversations/{conv['id']}").json()
        assert got["model"] is None and got["running_agent"] == {"provider": "fake", "model": "m1", "mode": None}
    finally:
        let_go(env, conv)
    assert env.get(f"/api/conversations/{conv['id']}").json()["running_agent"] is None
    env.post(f"/api/conversations/{conv['id']}/messages", json={"text": "next"}, headers=H)
    wait_idle(env, conv["id"])
    models = [r["model"] for r in relay.db.all("SELECT model FROM runs WHERE conversation_id = ? ORDER BY created",
                                               (conv["id"],))]
    assert models == ["m1", None]


def test_the_governors_core_tools_are_listed_from_the_start(echo_chat):
    """What every wake-up uses is marked to load at once (Claude Code defers MCP tools behind its tool search
    until asked); the rare ones stay behind it."""
    env = echo_chat
    relay = env.app.state.relay
    agentos_on(env)
    governor, run_id, tool = governor_lingering(env, relay)
    try:
        token = relay.db.one("SELECT tools_token FROM conversations WHERE id = ?", (governor["id"],))["tools_token"]
        tools = mcp(env, token, "tools/list", {})[1]["result"]["tools"]
        up_front = {t["name"] for t in tools if t.get("_meta", {}).get("anthropic/alwaysLoad")}
        assert {"agentos_chat_create", "agentos_chat_wait", "agentos_task_done", "agentos_notify_report"} <= up_front
        assert not up_front & {"agentos_service_restore", "agentos_provider_repair", "agentos_memory_review"}
    finally:
        let_go(env, governor)


def test_a_governors_token_reaches_only_its_capabilities(echo_chat):
    """The allow-list is enforced where calls arrive, not only in what's listed: no delegate,
    computer_run, commit or restore with the Governor's token, nothing but approve_permission
    with a subagent's, no AgentOS capability with a user's chat's."""
    env = echo_chat
    relay = env.app.state.relay
    agentos_on(env)
    env.post("/api/computers", json={"name": "laptop"}, headers=H)  # computer tools exist for the user
    governor, run_id, tool = governor_lingering(env, relay)
    try:
        for name in ("delegate", "wait", "tell", "merge", "commit", "restore", "changes", "computers", "computer_run",
                     "computer_write", "made_up"):
            text, error = tool(name, task="x", command="id", message="x", turn="1", id="x", path="/tmp/x", content="x")
            assert error and "isn't one of this conversation's" in text, (name, text)
        assert not tool("help")[1] and not tool("agents")[1]  # what it is given works
        text, error = tool("agentos_chat_wait", ids=5)  # (a number for ids: said, not "Relay couldn't do that")
        assert error and text.startswith("ids: the chats' ids"), text
        # An id's first characters that fit two: said so (it was "No managed chat"); more of it, or all, the one
        for suffix in ("1" * 28, "2" * 28):
            relay.db.execute("INSERT INTO conversations (id, user_id, title, provider_id, mode, directory, managed, "
                             "created, updated) SELECT ?, user_id, 'Twin', 'fake', 'chat', '/tmp', 1, 0, 0 FROM "
                             "conversations WHERE id = ?", ("abcd" + suffix, governor["id"]))
        text, error = tool("agentos_chat_read", id="abcd")
        assert error and "More than one managed chat starts with abcd" in text, text
        assert not tool("agentos_chat_read", id="abcd1")[1] and not tool("agentos_chat_read", id="abcd" + "2" * 28)[1]
        refused = [e for e in env.get("/api/agentos").json()["events"] if e["kind"] == "refused"]
        assert {e["detail"]["tool"] for e in refused} >= {"delegate", "computer_run", "commit"}
        assert all(e["actor"] == "governor" and not e["ok"] for e in refused)
    finally:
        let_go(env, governor)
    # A user's own chat: its own tools, not AgentOS's (nothing of it lands in AgentOS's trail)
    other = env.post("/api/conversations", json={"provider_id": "fake", "mode": "code",
                                                 "directory": str(env.tmp / "ws" / "project")}, headers=H).json()
    env.post(f"/api/conversations/{other['id']}/messages", json={"text": "linger"}, headers=H)
    token = relay.db.one("SELECT tools_token FROM conversations WHERE id = ?", (other["id"],))["tools_token"]
    for name in ("agentos_task_list", "agentos_production_promote", "agentos_chat_fork"):
        result = mcp(env, token, "tools/call", {"name": name, "arguments": {}})[1]["result"]
        assert result["isError"] and "isn't one of this conversation's" in result["content"][0]["text"]
    assert not mcp(env, token, "tools/call", {"name": "changes", "arguments": {}})[1]["result"]["isError"]  # its own
    let_go(env, other)
    # A subagent: approve_permission only
    child = relay.db.execute("INSERT INTO conversations (id, user_id, title, provider_id, mode, directory, parent_run, "
                             "parent_conversation, tools_token, created, updated) SELECT 'sub1', user_id, 'sub', 'fake', "
                             "'chat', ?, 'r', ?, 'subtoken', 0, 0 FROM conversations WHERE id = ?",
                             (str(env.tmp / "sub"), other["id"], other["id"]))
    assert child.rowcount == 1
    sub_run = env.post("/api/conversations/sub1/messages", json={"text": "linger"}, headers=H).json()["run_id"]
    assert sub_run
    result = mcp(env, "subtoken", "tools/call", {"name": "agents", "arguments": {}})[1]["result"]
    assert result["isError"] and "A subagent gets no Relay tools" in result["content"][0]["text"]
    (env.tmp / "sub").mkdir(exist_ok=True)
    let_go(env, {"id": "sub1", "directory": str(env.tmp / "sub")})


def test_agentos_agents_read_only_their_own_things(env, tmp_path):
    """The providers' judges hold the Governor, the chats it manages and their subagents to their
    own folder (and what Relay handed them); a user's own chat reads anywhere, as before."""
    from relay.agentos import notebook, read_boundary
    from relay.providers.claude import ClaudeCodeProvider
    from relay.providers.opencode import OpencodeProvider
    signed_in(env)
    relay = env.app.state.relay
    user = relay.db.one("SELECT id FROM users")["id"]
    users_chat = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    managed = env.post("/api/conversations", json={"provider_id": "fake", "mode": "code",
                                                   "directory": str(env.tmp / "ws" / "project")}, headers=H).json()
    relay.db.execute("UPDATE conversations SET managed = 1 WHERE id = ?", (managed["id"],))
    folder = notebook(user)
    folder.mkdir(parents=True)
    relay.db.execute("INSERT INTO conversations (id, user_id, title, provider_id, mode, directory, agentos_role, "
                     "created, updated) VALUES ('gov', ?, 'Governor', 'fake', 'code', ?, 'governor', 0, 0)",
                     (user, str(folder)))
    relay.db.execute("INSERT INTO conversations (id, user_id, title, provider_id, mode, directory, parent_run, "
                     "parent_conversation, created, updated) VALUES ('kid', ?, 'sub', 'fake', 'chat', ?, 'r', ?, 0, 0)",
                     (user, str(tmp_path / "kid"), managed["id"]))
    row = lambda cid: relay.db.one("SELECT * FROM conversations WHERE id = ?", (cid,))  # noqa: E731
    assert read_boundary(relay.db, row(users_chat["id"]), users_chat["directory"]) is None
    governor = read_boundary(relay.db, row("gov"), folder)
    opencode, claude = object.__new__(OpencodeProvider), object.__new__(ClaudeCodeProvider)
    users_folder = users_chat["directory"]
    for reads, own in ((governor, folder), (read_boundary(relay.db, row(managed["id"]), managed["directory"]),
                                           pathlib.Path(managed["directory"])),
                       (read_boundary(relay.db, row("kid"), tmp_path / "kid"), tmp_path / "kid")):
        assert reads is not None
        for other in (users_folder, str(env.tmp / "data" / "uploads"), str(env.tmp / "relay.db"),
                      str(pathlib.Path.home()), "/etc"):
            verdict, why = opencode.judge("external_directory", [f"{other}/*"], own, reads)
            assert verdict == "deny" and "isn't this conversation's" in why, other
            assert claude.judge("Read", {"file_path": f"{other}/secret.txt"}, own, reads)[0] == "deny"
            assert claude.judge("Grep", {"pattern": "x", "path": other}, own, reads)[0] == "deny"
        assert opencode.judge("external_directory", [f"{own}/sub/*"], own, reads)[0] == "allow"
        assert opencode.judge("external_directory", ["/tmp/opencode/*"], own, reads)[0] == "allow"
        assert claude.judge("Read", {"file_path": "notes.md"}, own, reads)[0] == "allow"  # (relative: in its folder)
    # The Governor reads its agents' work (managed chats' folders, the services' clones); they don't read its notebook
    assert opencode.judge("external_directory", [f"{managed['directory']}/*"], folder, governor)[0] == "allow"
    ours, _ = fake_service(relay, user)  # AgentOS's: its versions and clones are the Governor's to read
    theirs, _ = fake_service(relay, user, "mine", live_number=3)
    relay.db.execute("UPDATE services SET managed = 0 WHERE id = ?", (theirs["id"],))  # the user's own: not
    governor = read_boundary(relay.db, row("gov"), folder)
    production = env.tmp / "data" / "production"
    assert opencode.judge("external_directory", [f"{production}/{ours['id']}/1/*"], folder, governor)[0] == "allow"
    assert opencode.judge("external_directory", [f"{production}/{theirs['id']}/1/*"], folder, governor)[0] == "deny"
    managed_reads = read_boundary(relay.db, row(managed["id"]), managed["directory"])
    assert opencode.judge("external_directory", [f"{folder}/*"], managed["directory"], managed_reads)[0] == "deny"
    # The user's own: anywhere, as ever
    assert opencode.judge("external_directory", ["/etc/*"], users_folder, None)[0] == "allow"
    assert claude.judge("Read", {"file_path": "/etc/hosts"}, users_folder, None)[0] == "allow"


def test_tools_for_tells_a_box_only_its_own_agents(env):
    """`relay` in a WorkBox finds its agent's tools only with the box's own folder, signed by
    Relay: another box (the Governor's, say) can't be asked for."""
    from starlette.testclient import TestClient
    signed_in(env)
    relay = env.app.state.relay
    project = env.tmp / "ws" / "project"
    conv = env.post("/api/conversations", json={"provider_id": "fake", "mode": "code", "directory": str(project)},
                    headers=H).json()
    env.post(f"/api/conversations/{conv['id']}/messages", json={"text": "linger"}, headers=H)
    for _ in range(50):
        if relay.runs.active_run_for(conv["id"]) and relay.db.one(
                "SELECT tools_token FROM conversations WHERE id = ?", (conv["id"],))["tools_token"]:
            break
        time.sleep(0.1)
    token = relay.db.one("SELECT tools_token FROM conversations WHERE id = ?", (conv["id"],))["tools_token"]
    local = TestClient(env.app, client=("127.0.0.1", 50000))
    key = relay.containers.box_key(project)
    try:
        found = local.get("/tools-for", params={"folder": str(project / "src"), "box": str(project), "key": key})
        assert found.status_code == 200 and found.json()["url"].endswith(f"/mcp/{token}")
        # No key, a forged key, another box's key: nothing
        for params in ({"folder": str(project)}, {"folder": str(project), "box": str(project), "key": "forged"},
                       {"folder": str(project), "box": str(project), "key": relay.containers.box_key(env.tmp / "ws")}):
            assert local.get("/tools-for", params=params).status_code == 403
        # Another box, asking about this folder: only its own agents (none)
        elsewhere = env.tmp / "ws" / "elsewhere"
        elsewhere.mkdir()
        asked = local.get("/tools-for", params={"folder": str(project), "box": str(elsewhere),
                                                "key": relay.containers.box_key(elsewhere)})
        assert asked.status_code == 404
        assert env.get("/tools-for", params={"folder": str(project), "box": str(project), "key": key}).status_code == 403
    finally:
        let_go(env, conv)


def test_a_governed_tasks_turn_is_a_state_relay_keeps(echo_chat):
    """Dispatched before the Governor starts, once; reported on (task.done) only while it's
    dispatched; a wake-up that breaks off makes it due again, a few times, then the user is told."""
    env = echo_chat
    relay = env.app.state.relay
    agentos = relay.agentos
    agentos_on(env)
    user = relay.db.one("SELECT id FROM users")["id"]
    task = env.post("/api/schedules", json={"name": "Atlas", "prompt": "Update the atlas",
                                            "when": {"kind": "interval", "minutes": 4320}, "governed": True},
                    headers=H).json()
    governor, run_id, tool = governor_lingering(env, relay)
    try:
        text, error = tool("agentos_task_done", id=task["id"][:8])
        assert error and "isn't dispatched to you" in text  # not its turn: nothing to report on
    finally:
        let_go(env, governor)
    now = time.time()
    agentos._store(user, {**agentos.settings(user), "last_wake": now})
    relay.db.execute("UPDATE schedules SET next_run = ? WHERE id = ?", (now - 1, task["id"]))
    due = relay.schedules.due(now)
    first = env.portal.call(agentos.wake, user, due, now, "1 task is due")
    assert first and env.portal.call(agentos.wake, user, due, now, "1 task is due") is None  # once
    row = relay.db.one("SELECT * FROM schedules WHERE id = ?", (task["id"],))
    assert row["dispatch_state"] == "dispatched" and row["dispatch_run"] == first and row["next_run"] > now
    wait_idle(env, governor["id"])
    row = relay.db.one("SELECT * FROM schedules WHERE id = ?", (task["id"],))
    assert row["dispatch_state"] == "awaiting" and row["outcome"] == "unreported" and row["outcome_by"] == "relay"
    # A later wake-up: the briefing asks for the report, which is taken once
    assert "still awaiting your report" in agentos.briefing(user, [], now, "a round", agentos.settings(user))
    env.post(f"/api/conversations/{governor['id']}/messages", json={"text": "linger"}, headers=H).json()["run_id"]
    try:
        # (the note on the script's stdin: message)
        text, error = tool("agentos_task_done", id=task["id"][:8], ok=False, message="the source was down")
        assert not error and "failed" in text
        row = relay.db.one("SELECT * FROM schedules WHERE id = ?", (task["id"],))
        assert (row["dispatch_state"], row["outcome"], row["outcome_by"]) == (None, "failed", "governor")
        assert row["outcome_note"] == "the source was down"
        assert tool("agentos_task_done", id=task["id"][:8])[1]  # not twice
    finally:
        let_go(env, governor)
    # A crash in the middle: dispatching (never started) stays due; dispatched is due again now
    relay.db.execute("UPDATE schedules SET dispatch_state = 'dispatching', next_run = ? WHERE id = ?",
                     (now - 5, task["id"]))
    agentos.reconcile(now)
    row = relay.db.one("SELECT * FROM schedules WHERE id = ?", (task["id"],))
    assert row["dispatch_state"] is None and row["next_run"] == now - 5 and row["attempts"] == 0
    for attempt in (1, 2):
        relay.db.execute("UPDATE schedules SET dispatch_state = 'dispatched', dispatch_run = 'gone', next_run = ? "
                         "WHERE id = ?", (now + 9999, task["id"]))
        agentos.reconcile(now)
        row = relay.db.one("SELECT * FROM schedules WHERE id = ?", (task["id"],))
        assert (row["dispatch_state"], row["outcome"], row["attempts"], row["next_run"]) == (None, "interrupted",
                                                                                             attempt, now)
    relay.db.execute("UPDATE schedules SET dispatch_state = 'dispatched' WHERE id = ?", (task["id"],))
    agentos.reconcile(now)
    row = relay.db.one("SELECT * FROM schedules WHERE id = ?", (task["id"],))
    assert row["attempts"] == 0 and row["last_error"].startswith("Given up")
    state = env.get("/api/agentos").json()
    given_up = [i for i in state["interventions"] if i["task_id"] == task["id"]]
    assert given_up and "gave up on task 'Atlas'" in given_up[0]["text"]
    kinds = [e["kind"] for e in state["events"]]
    for kind in ("wake", "task.dispatched", "task.unreported", "task.reported", "task.interrupted", "task.given_up",
                 "reconciled", "capability"):
        assert kind in kinds, kind
    reported = next(e for e in state["events"] if e["kind"] == "task.reported")
    assert reported["actor"] == "governor" and not reported["ok"]  # its word, kept as such


def test_irreversible_capabilities_pass_the_policy_first(echo_chat):
    import threading
    env = echo_chat
    relay = env.app.state.relay
    agentos_on(env, ask_first=["task.delete"])
    assert env.put("/api/agentos", json={"ask_first": ["chat.list"]}, headers=H).status_code == 400
    governor, run_id, tool = governor_lingering(env, relay)
    try:
        assert not tool("agentos_task_create", name="Mine to drop", message="x", when="daily 08:00")[1]
        made = next(t for t in env.get("/api/schedules").json() if t["name"] == "Mine to drop")

        def delete_answering(choice):
            result = {}
            thread = threading.Thread(target=lambda: result.update(zip(("text", "error"), tool(
                "agentos_task_delete", id=made["id"][:8]), strict=True)))
            thread.start()
            asked = None
            for _ in range(200):
                active = relay.runs.active.get(run_id)
                asked = next(iter(active.pending.values()), None) if active else None
                if asked or not thread.is_alive():
                    break
                time.sleep(0.02)
            assert asked and asked["event"]["action"] == "AgentOS: task.delete"
            env.post(f"/api/runs/{run_id}/permission", json={"id": asked["event"]["id"], "decision": choice}, headers=H)
            thread.join(10)
            return result["text"], result["error"]

        text, error = delete_answering("denied")
        assert error and "didn't allow task.delete" in text
        assert any(t["id"] == made["id"] for t in env.get("/api/schedules").json())
        assert not delete_answering("once")[1]
        assert not any(t["id"] == made["id"] for t in env.get("/api/schedules").json())
        # A deterministic check plugged in: refused without asking anyone
        relay.agentos.policies.append(lambda user, capability, arguments: "frozen for the night"
                                      if capability == "production.rollback" else None)
        text, error = tool("agentos_production_rollback", service="anything")
        assert error and "frozen for the night" in text
        policy = [e for e in env.get("/api/agentos").json()["events"] if e["kind"] == "policy"]
        assert [e["actor"] for e in policy][:3] == ["relay", "user", "user"] and not policy[0]["ok"]
    finally:
        relay.agentos.policies.clear()
        let_go(env, governor)


def fake_service(relay, user, name="site", live_number=2):
    """A service in the database (no podman): versions 1..live_number, the last one live."""
    import uuid as uuid_
    service_id = uuid_.uuid4().hex
    relay.db.execute("INSERT INTO services (id, user_id, name, command, public_port, created) VALUES (?, ?, ?, 'x', ?, 0)",
                     (service_id, user, name, 20000 + live_number + len(name)))
    versions = {}
    for number in range(1, live_number + 1):
        versions[number] = uuid_.uuid4().hex
        relay.db.execute("INSERT INTO service_versions (id, service_id, number, folder, status, created, archived) "
                         "VALUES (?, ?, ?, '/nowhere', ?, 0, ?)", (versions[number], service_id, number,
                                                                    "live" if number == live_number else "archived",
                                                                    None if number == live_number else 1))
    relay.db.execute("UPDATE services SET live_version = ? WHERE id = ?", (versions[live_number], service_id))
    return relay.production.get(user, name), versions


def test_a_services_workbox_agent_is_told_how_its_versions_run(echo_chat, monkeypatch):
    """$PORT, and its data in $DATA_DIR: else a database next to the code stays behind with the old version
    (found for real: a promote that lost the user's books)."""
    env = echo_chat
    relay = env.app.state.relay
    user = relay.db.one("SELECT id FROM users")["id"]
    service, versions = fake_service(relay, user, "shelf")
    seen = []
    provider = type(relay.registry.provider("fake"))
    started = provider.start
    monkeypatch.setattr(provider, "start", lambda self, context, prompt: seen.append(context) or started(self, context, prompt))
    made = []
    for name in ("shelf", "other"):
        folder = env.tmp / "ws" / name
        folder.mkdir(parents=True)
        made.append(env.post("/api/conversations", json={"provider_id": "fake", "mode": "code", "directory": str(folder),
                                                         "isolate": False}, headers=H).json())
    relay.db.execute("INSERT INTO service_clones (conversation_id, service_id, from_version, created) VALUES (?, ?, ?, 0)",
                     (made[0]["id"], service["id"], versions[2]))
    for conv in made:
        run_turn(env, conv["id"], "hi")
    workbox, plain = seen[0].tools_note, seen[1].tools_note
    assert "WorkBox of the live service shelf" in workbox and "$DATA_DIR" in workbox and "DATA_DIR=.data x" in workbox
    assert "live service" not in plain


def test_a_service_with_nothing_live_shows_why_its_last_version_didnt_come_up(echo_chat):
    """Found for real: a first version that crashed (a module installed only in the WorkBox) left the
    user with a log that said "(nothing is live)"."""
    env = echo_chat
    relay = env.app.state.relay
    user = relay.db.one("SELECT id FROM users")["id"]
    service, versions = fake_service(relay, user, "short", live_number=1)
    relay.db.execute("UPDATE service_versions SET status = 'failed', note = ? WHERE id = ?",
                     ("Version 1 didn't come up well; nothing is live. Its log:\nModuleNotFoundError: No module "
                      "named 'uvicorn'", versions[1]))
    relay.db.execute("UPDATE services SET live_version = NULL WHERE id = ?", (service["id"],))
    shown = env.get(f"/api/services/{service['id']}/logs").json()["logs"]
    assert shown.startswith("(nothing is live) Version 1, failed:") and "No module named 'uvicorn'" in shown
    relay.db.execute("UPDATE service_versions SET status = 'archived' WHERE id = ?", (versions[1],))
    assert env.get(f"/api/services/{service['id']}/logs").json()["logs"] == "(nothing is live)"  # (only a failure's)


def test_two_services_promoted_at_once_get_ports_of_their_own(echo_chat, monkeypatch):
    """One operation at a time is per service: two services' promotes may run together, and each
    new version's internal port is taken the moment it's chosen."""
    import asyncio
    import time as time_
    env = echo_chat
    relay = env.app.state.relay
    user = relay.db.one("SELECT id FROM users")["id"]
    production = relay.production
    started = []

    def start(service, version, port):
        started.append((service["name"], port))
        time_.sleep(.3)  # (the box starting: the other promote goes on meanwhile)

    async def well(service, version, port):
        return True, "ok"
    monkeypatch.setattr(production, "_start", start)
    monkeypatch.setattr(production, "_wait_well", well)
    monkeypatch.setattr(production, "_snapshot", lambda service, version, live: None)
    monkeypatch.setattr(production, "_podman", lambda *args, **kwargs: None)
    one, _ = fake_service(relay, user, "one")
    two, _ = fake_service(relay, user, "second")  # (its public port differs by its name's length)
    source = env.tmp / "ws" / "site"
    source.mkdir(parents=True)
    (source / "app.py").write_text("")

    async def both():
        return await asyncio.gather(production.promote(one, source), production.promote(two, source))
    env.portal.call(both)
    ports = [port for _, port in started]
    assert sorted(name for name, _ in started) == ["one", "second"] and len(set(ports)) == 2, started
    from relay.production import OTHER_PORTS
    assert all(port in OTHER_PORTS for port in ports)  # (a Relay with another data folder: not the usual one's range)
    live = relay.db.all("SELECT port FROM service_versions WHERE status = 'live'")
    assert sorted(r["port"] for r in live) == sorted(ports)
    # A version that takes over from a failing one: its history says it's well again (found for real: a
    # service's page said "failing since" hours after the Governor's fix went live)
    relay.db.execute("INSERT INTO service_health (service_id, at, ok, detail) VALUES (?, ?, 0, 'no connection')",
                     (one["id"], time_.time() - 60))
    env.portal.call(production.promote, production.get(user, "one"), source)
    changes = production.history(one)["changes"]
    assert [c["ok"] for c in changes] == [0, 1] and changes[-1]["detail"].startswith("version 4 live")
    env.portal.call(production.promote, production.get(user, "one"), source)
    assert len(production.history(one)["changes"]) == 2  # (well already: nothing to say)


def test_a_service_is_told_failing_after_two_checks_in_a_row(echo_chat, monkeypatch):
    """Not after one: a box still starting (Relay just started, after a reboot) would wake the
    Governor for nothing. Not while an operation works on it (a restart). Once, until it's well for a
    while (WELL_CHECKS in a row)."""
    from relay import production as production_module
    env = echo_chat
    relay = env.app.state.relay
    user = relay.db.one("SELECT id FROM users")["id"]
    production = relay.production
    service, versions = fake_service(relay, user)
    relay.db.execute("UPDATE service_versions SET port = 21999 WHERE id = ?", (versions[2],))
    answers = {"ok": False}
    monkeypatch.setattr(production_module, "probe", lambda port, health, timeout=5: (
        (True, "HTTP 200") if answers["ok"] else (False, "no answer ([Errno 111] Connection refused)")))
    now = [time.time() + 10**6]  # (ahead of the app's own scheduler ticks, which then skip the check)

    def check():
        now[0] += production_module.HEALTH_EVERY
        return [s["name"] for s in env.portal.call(production.check, now[0])]
    assert check() == []  # once: maybe starting
    assert production.get(user, "site")["health_ok"] == 0  # (shown failing at once, though)
    production.busy[service["id"]] = {"kind": "restart", "source": None}
    assert check() == []  # being restarted: not checked
    production.busy.clear()
    assert check() == ["site"]  # twice in a row
    production.alerted(service, now[0])  # (told: the scheduler does this)
    assert check() == []  # once until it's well
    production.failed.clear()  # (Relay restarted: it remembers it was told, not the checks)
    assert check() == [] and check() == []
    answers["ok"] = True
    for _ in range(production_module.WELL_CHECKS - 1):
        assert check() == []
    assert production.get(user, "site")["alerted_at"] is not None  # (well a moment only: the same incident)
    assert check() == [] and production.get(user, "site")["alerted_at"] is None
    answers["ok"] = False
    assert check() == [] and check() == ["site"]  # failing anew: again two in a row
    # The scheduler tells it (AgentOS off here: the user, an intervention) and keeps that it did
    failing = {**production.get(user, "site"), "health_detail": "no answer"}
    env.portal.call(relay.scheduler.service_failing, failing, now[0])
    told = [i["text"] for i in relay.agentos.interventions(user) if not i["resolved"]]
    assert told == ["The service site is failing (no answer)."]
    assert production.get(user, "site")["alerted_at"] == now[0] and check() == []


def test_only_a_fresh_clone_of_the_service_can_be_promoted(echo_chat):
    env = echo_chat
    relay = env.app.state.relay
    agentos_on(env)
    user = relay.db.one("SELECT id FROM users")["id"]
    service, versions = fake_service(relay, user)
    other, other_versions = fake_service(relay, user, "other")
    governor, run_id, tool = governor_lingering(env, relay)

    def managed_task(title, folder):
        folder.mkdir(parents=True, exist_ok=True)
        row = env.post("/api/conversations", json={"provider_id": "fake", "mode": "code", "directory": str(folder),
                                                   "title": title}, headers=H).json()
        relay.db.execute("UPDATE conversations SET managed = 1 WHERE id = ?", (row["id"],))
        return row
    try:
        unrelated = managed_task("Some site", env.tmp / "ws" / "some-site")
        text, error = tool("agentos_production_promote", service="site", id=unrelated["id"][:8])
        assert error and "isn't a clone of site" in text
        # A clone of the other service isn't one of this one
        theirs = managed_task("Other's clone", env.tmp / "ws" / "other-clone")
        relay.db.execute("INSERT INTO service_clones (conversation_id, service_id, from_version, created) VALUES (?, ?, ?, 0)",
                         (theirs["id"], other["id"], other_versions[2]))
        assert "isn't a clone of site" in tool("agentos_production_promote", service="site", id=theirs["id"][:8])[0]
        # A stale clone: made from version 1, version 2 is live now
        stale = managed_task("Old clone", env.tmp / "ws" / "old-clone")
        relay.db.execute("INSERT INTO service_clones (conversation_id, service_id, from_version, created) VALUES (?, ?, ?, 0)",
                         (stale["id"], service["id"], versions[1]))
        text, error = tool("agentos_production_promote", service="site", id=stale["id"][:8])
        assert error and "made from version 1, but version 2 is live" in text
        # The user's own code task: not managed, not the Governor's to promote
        users = env.post("/api/conversations", json={"provider_id": "fake", "mode": "code",
                                                     "directory": str(env.tmp / "ws" / "project")}, headers=H).json()
        assert "No managed chat" in tool("agentos_production_promote", service="site", id=users["id"][:8])[0]
        # A service busy with another operation: one at a time
        fresh = managed_task("Fresh clone", env.tmp / "ws" / "fresh-clone")
        relay.db.execute("INSERT INTO service_clones (conversation_id, service_id, from_version, created) VALUES (?, ?, ?, 0)",
                         (fresh["id"], service["id"], versions[2]))
        relay.production.busy[service["id"]] = {"kind": "rollback", "source": None}
        text, error = tool("agentos_production_promote", service="site", id=fresh["id"][:8])
        assert error and "busy (rollback going on)" in text
        assert "busy" in tool("agentos_production_rollback", service="site")[0]
        assert "busy" in env.delete(f"/api/services/{service['id']}", headers=H).json()["error"]
        relay.production.busy.clear()
        # A deleted service: nothing to promote to
        relay.db.execute("DELETE FROM services WHERE id = ?", (other["id"],))
        assert "No service other" in tool("agentos_production_promote", service="other", id=theirs["id"][:8])[0]
        failed = [e for e in env.get("/api/agentos").json()["events"]
                  if e["kind"] == "capability" and e["detail"]["capability"] == "production.promote"]
        assert failed and not any(e["ok"] for e in failed) and all(e["actor"] == "governor" for e in failed)
    finally:
        let_go(env, governor)


def test_demote_and_promote_by_the_user_without_the_governor(echo_chat, monkeypatch):
    """Demote (the service's page) clones the live version into a WorkBox of the user's own at once
    (no Governor, not managed); Promote to live in its chat makes it the live version; deleting its
    chat takes its folder along. The service stays live meanwhile."""
    import types
    env = echo_chat
    relay = env.app.state.relay
    production = relay.production
    agentos_on(env)
    user = relay.db.one("SELECT id FROM users")["id"]
    service, versions = fake_service(relay, user)
    live_folder = env.tmp / "ws" / "site-v2"
    live_folder.mkdir(parents=True)
    (live_folder / "app.py").write_text("v2")
    relay.db.execute("UPDATE service_versions SET folder = ? WHERE id = ?", (str(live_folder), versions[2]))
    monkeypatch.setattr(production, "_podman", lambda *args, **kwargs: types.SimpleNamespace(returncode=1, stdout="", stderr=""))
    started = []

    async def take_over(service, version, by):
        started.append((version["number"], by))
        relay.db.execute("UPDATE service_versions SET status = 'live', promoted_by = ? WHERE id = ?", (by, version["id"]))
        relay.db.execute("UPDATE services SET live_version = ? WHERE id = ?", (version["id"], service["id"]))
        return f"Version {version['number']} of {service['name']} is live (ok)."
    monkeypatch.setattr(production, "_take_over", take_over)

    url = f"/api/services/{service['id']}"
    assert "provider with tools" in env.post(f"{url}/demote", json={"provider_id": "echo"}, headers=H).json()["error"]
    answer = env.post(f"{url}/demote", json={"provider_id": "fake"}, headers=H)
    assert answer.status_code == 200, answer.text
    clone = answer.json()
    folder = pathlib.Path(clone["directory"])
    assert not clone["managed"] and clone["mode"] == "code" and clone["title"] == "site: work on version 2"
    assert (folder / "app.py").read_text() == "v2" and (folder / ".data").is_dir()
    assert clone["clone"] == {"service_id": service["id"], "service": "site", "version": 2, "own": True, "fresh": True}
    assert relay.agentos.governor(user) is None or not relay.runs.active_run_for(relay.agentos.governor(user)["id"])
    made = [e for e in env.get("/api/agentos").json()["events"] if e["kind"] == "workbox.cloned"]
    assert made and made[0]["actor"] == "user"
    assert production.get(user, "site")["live_version"] == versions[2]  # (still live)
    # The Governor may not promote it: it's the user's own
    governor, run_id, tool = governor_lingering(env, relay)
    try:
        text, error = tool("agentos_production_promote", service="site", id=clone["id"][:8])
        assert error, text
    finally:
        let_go(env, governor)

    # Promote to live: a new version, by the user; then this WorkBox is behind
    (folder / "app.py").write_text("v3")
    other = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    assert "isn't a clone of site" in env.post(f"{url}/promote", json={"conversation_id": other["id"]}, headers=H).json()["error"]
    assert env.post(f"{url}/promote", json={"conversation_id": "nope"}, headers=H).status_code == 404
    promoted = env.post(f"{url}/promote", json={"conversation_id": clone["id"]}, headers=H)
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["message"] == "Version 3 of site is live (ok)." and started == [(3, "user")]
    assert env.get(f"/api/conversations/{clone['id']}").json()["clone"]["fresh"] is True  # (it goes on from v3)
    # Another version live meanwhile: promoting it would undo that
    relay.db.execute("UPDATE service_clones SET from_version = ? WHERE conversation_id = ?", (versions[2], clone["id"]))
    behind = env.post(f"{url}/promote", json={"conversation_id": clone["id"]}, headers=H)
    assert behind.status_code == 409 and "made from version 2, but version 3 is live" in behind.json()["error"]

    # Deleting its chat: its folder goes too; not while it's being promoted
    production.busy[service["id"]] = {"kind": "promote", "source": clone["directory"]}
    assert env.delete(f"/api/conversations/{clone['id']}", headers=H).status_code == 409
    production.busy.clear()
    assert env.delete(f"/api/conversations/{clone['id']}", headers=H).status_code == 200
    assert not folder.parent.exists()
    assert not relay.db.one("SELECT 1 FROM service_clones WHERE conversation_id = ?", (clone["id"],))

    production.busy[service["id"]] = {"kind": "restart", "source": None}
    assert "busy" in env.post(f"{url}/demote", json={"provider_id": "fake"}, headers=H).json()["error"]
    production.busy.clear()
    relay.db.execute("UPDATE services SET live_version = NULL WHERE id = ?", (service["id"],))
    assert "Nothing of site is live" in env.post(f"{url}/demote", json={"provider_id": "fake"}, headers=H).json()["error"]
    assert env.post("/api/services/nope/demote", json={"provider_id": "fake"}, headers=H).status_code == 404


def test_the_governor_sees_only_the_services_agentos_looks_after(echo_chat, monkeypatch):
    """A service is AgentOS's (managed: the Governor sees it and may change it) or the user's own,
    which the Governor doesn't know of: not in its list, briefing, files or interventions, not to
    clone, promote or roll back; failing, it's the user who's told. PATCH switches it."""
    from relay.agentos import read_boundary
    env = echo_chat
    relay = env.app.state.relay
    agentos_on(env)
    user = relay.db.one("SELECT id FROM users")["id"]
    site, _ = fake_service(relay, user)  # (from before there were private ones: managed)
    mine, _ = fake_service(relay, user, "mine", live_number=3)  # (another port)
    answer = env.patch(f"/api/services/{mine['id']}", json={"managed": False}, headers=H)
    assert answer.status_code == 200 and answer.json()["managed"] is False
    assert [s["managed"] for s in env.get("/api/services").json()] == [False, True]  # (mine, site: the user sees both)
    told = relay.agentos.intervene(user, "mine is failing", service_id=mine["id"])
    relay.agentos.intervene(user, "site is failing", service_id=site["id"])
    governor, run_id, tool = governor_lingering(env, relay)
    try:
        listed = tool("agentos_service_list")[0]
        assert "site" in listed and "mine" not in listed
        for capability in ("agentos_service_status", "agentos_service_logs", "agentos_production_rollback"):
            text, error = tool(capability, service="mine")
            assert error and "No service mine" in text, (capability, text)
        text, error = tool("agentos_workbox_clone", service="mine", provider="fake")
        assert error and "No service mine" in text
        assert tool("agentos_notify_resolve", id=told[:8], note="fixed")[1]
        briefing = relay.agentos.briefing(user, [], time.time(), "a round", relay.agentos.settings(user))
        assert "site is failing" in briefing and "mine is failing" not in briefing and "Services: 1" in briefing
        boundary = read_boundary(relay.db, relay.agentos.governor(user), governor["directory"])
        assert any(p.name == site["id"] for p in boundary) and not any(p.name == mine["id"] for p in boundary)
    finally:
        let_go(env, governor)
    # Failing: the user is told, not the Governor
    woken = []

    async def wake(*args, **kwargs):
        woken.append(args)
        return "run"
    monkeypatch.setattr(relay.agentos, "wake", wake)
    env.portal.call(relay.scheduler.service_failing, {**relay.production.get(user, "mine"), "health_detail": "no answer"},
                    time.time())
    assert not woken and any(i["text"] == "The service mine is failing (no answer)." and i["service_id"] == mine["id"]
                             for i in relay.agentos.interventions(user))
    env.portal.call(relay.scheduler.service_failing, {**relay.production.get(user, "site"), "health_detail": "no answer"},
                    time.time())
    assert len(woken) == 1
    # Given to AgentOS: the Governor sees it
    env.patch(f"/api/services/{mine['id']}", json={"managed": True}, headers=H)
    assert relay.production.get(user, "mine", managed=True)["id"] == mine["id"]
    events = [e["kind"] for e in env.get("/api/agentos").json()["events"]]
    assert "service.private" in events and "service.managed" in events


def test_promote_makes_a_code_task_a_service_and_its_workbox_follows_the_live_version(echo_chat, monkeypatch):
    """/promote in a code task: the project becomes a service and the task goes on as its WorkBox
    (promote again: the next version). When the live version moves on meanwhile (the Governor's fix, a
    rollback), Update from live merges it in, three ways, so both sides' changes stay."""
    import shutil
    import types
    env = echo_chat
    relay = env.app.state.relay
    production = relay.production
    user = relay.db.one("SELECT id FROM users")["id"]
    monkeypatch.setattr(production, "_podman", lambda *args, **kwargs: types.SimpleNamespace(returncode=0, stdout="", stderr=""))

    async def take_over(service, version, by):
        relay.db.execute("UPDATE service_versions SET status = 'live', promoted_by = ? WHERE id = ?", (by, version["id"]))
        relay.db.execute("UPDATE services SET live_version = ? WHERE id = ?", (version["id"], service["id"]))
        return f"Version {version['number']} of {service['name']} is live (ok)."
    monkeypatch.setattr(production, "_take_over", take_over)
    project = env.tmp / "ws" / "My Site"
    project.mkdir(parents=True)
    (project / "a.txt").write_text("one\ntwo\nthree\n")
    (project / "b.txt").write_text("b\n")
    (project / "img.bin").write_bytes(b"\0base")
    task = env.post("/api/conversations", json={"provider_id": "fake", "mode": "code", "directory": str(project),
                                                "isolate": False}, headers=H).json()
    guessed = env.get(f"/api/services/guess?folder={project}").json()
    assert guessed["name"] == "my-site" and guessed["command"] == "" and 8500 <= guessed["port"] < 9500
    other = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    made = {"name": "mysite", "folder": str(project), "command": "python3 -m http.server $PORT", "port": guessed["port"]}
    assert "isn't a code task in this folder" in env.post("/api/services", json={**made, "conversation_id": other["id"]},
                                                          headers=H).json()["error"]
    service = env.post("/api/services", json={**made, "conversation_id": task["id"]}, headers=H).json()
    assert service["live"]["number"] == 1 and not service["managed"]
    assert [w["id"] for w in service["workboxes"]] == [task["id"]]
    url = f"/api/conversations/{task['id']}"
    assert env.get(url).json()["clone"] == {"service_id": service["id"], "service": "mysite", "version": 1,
                                            "own": True, "fresh": True}
    assert "WorkBox already" in env.post("/api/services", json={**made, "name": "again", "conversation_id": task["id"]},
                                         headers=H).json()["error"]

    # /promote again: the next version; the WorkBox goes on from it
    (project / "a.txt").write_text("one\ntwo\nthree, v2\n")
    promote = f"/api/services/{service['id']}/promote"
    assert env.post(promote, json={"conversation_id": task["id"]}, headers=H).json()["message"].startswith("Version 2")
    assert env.get(url).json()["clone"]["fresh"] is True
    live = relay.db.one("SELECT folder FROM service_versions WHERE service_id = ? AND number = 2", (service["id"],))

    # The Governor fixes it from a clone of its own: version 3; this WorkBox changed meanwhile
    fix = env.tmp / "fix" / "mysite"
    shutil.copytree(live["folder"], fix)
    (fix / "a.txt").write_text("one\ntwo\nTHREE, fixed\n")
    (fix / "b.txt").write_text("b, fixed\n")
    (fix / "img.bin").write_bytes(b"\0theirs")
    (fix / "c.txt").write_text("new\n")
    env.portal.call(production.promote, production.get(user, "mysite"), fix, "a fix", None, "governor:x")
    (project / "a.txt").write_text("ONE\ntwo\nthree, v2\n")
    (project / "b.txt").write_text("b, mine\n")
    (project / "img.bin").write_bytes(b"\0mine")
    assert env.get(url).json()["clone"]["fresh"] is False
    refused = env.post(promote, json={"conversation_id": task["id"]}, headers=H)
    assert refused.status_code == 409 and "Update it from the live version first" in refused.json()["error"]
    update = f"/api/services/{service['id']}/update-workbox"
    done = env.post(update, json={"conversation_id": task["id"]}, headers=H).json()
    assert done["version"] == 3 and done["conflicts"] == ["b.txt"] and done["kept"] == ["img.bin"]
    assert sorted(done["merged"]) == ["a.txt", "c.txt"]
    assert (project / "a.txt").read_text() == "ONE\ntwo\nTHREE, fixed\n"  # both changes
    assert "<<<<<<< your WorkBox" in (project / "b.txt").read_text() and "b, fixed" in (project / "b.txt").read_text()
    assert (project / "img.bin").read_bytes() == b"\0mine" and (project / "c.txt").read_text() == "new\n"
    assert env.get(url).json()["clone"]["fresh"] is True
    assert env.post(promote, json={"conversation_id": task["id"]}, headers=H).json()["message"].startswith("Version 4")
    again = env.post(update, json={"conversation_id": task["id"]}, headers=H).json()
    assert again == {"merged": [], "conflicts": [], "kept": [], "version": 4}  # (up to date)

    # Made from a version that's gone (archived too long): nothing to merge against
    first = relay.db.one("SELECT id, folder FROM service_versions WHERE service_id = ? AND number = 1", (service["id"],))
    relay.db.execute("UPDATE service_clones SET from_version = ? WHERE conversation_id = ?", (first["id"], task["id"]))
    shutil.rmtree(first["folder"])
    gone = env.post(update, json={"conversation_id": task["id"]}, headers=H)
    assert gone.status_code == 409 and "Demote again" in gone.json()["error"]
    env.delete(f"/api/services/{service['id']}", headers=H)


def test_update_from_live_never_writes_through_a_symlink(tmp_path):
    """Relay's own process merges, so a symlink an agent made in its WorkBox (config -> ~/.config) isn't
    followed: what would go through it is the WorkBox's to sort out (kept)."""
    from relay.production import _merge
    base, theirs, ours, outside = (tmp_path / name for name in ("base", "theirs", "ours", "outside"))
    for root, text in ((base, "1"), (theirs, "2")):
        (root / "config").mkdir(parents=True)
        (root / "config" / "app.json").write_text(text)
    ours.mkdir()
    outside.mkdir()
    (outside / "app.json").write_text("the user's own")
    (ours / "config").symlink_to(outside)
    done = _merge(base, theirs, ours, ("version 1", "version 2"))
    assert done["kept"] == ["config/app.json"] and (outside / "app.json").read_text() == "the user's own"


def podman_answers(production, monkeypatch, answers=None):
    """production's podman faked: (returncode, stderr) per command (its first two words, or its first), else
    success; the calls."""
    import subprocess as subprocess_
    calls = []

    def run(*args, timeout=120):
        calls.append(list(args))
        said = answers or {}
        code, err = said.get(" ".join(args[:2]), said.get(args[0], (0, "")))  # ("volume exists", or "volume")
        if isinstance(code, Exception):
            raise code
        return subprocess_.CompletedProcess(["podman", *args], code, "", err)
    monkeypatch.setattr(production, "_podman", run)
    return calls


def test_a_services_data_is_copied_with_its_box_paused_and_a_failed_copy_is_said(echo_chat, monkeypatch):
    env = echo_chat
    relay = env.app.state.relay
    user = relay.db.one("SELECT id FROM users")["id"]
    service, versions = fake_service(relay, user, "paused")
    live = relay.db.one("SELECT * FROM service_versions WHERE id = ?", (versions[2],))
    candidate = {"id": versions[1], "number": 3}
    calls = podman_answers(relay.production, monkeypatch)
    made = relay.production._snapshot(service, candidate, live)
    assert made and made.endswith("before-3.tar")
    assert [c[0] for c in calls] == ["pause", "volume", "unpause"]  # (one moment's data, the service back at once)
    calls = podman_answers(relay.production, monkeypatch, {"volume": (125, "Error: no space left on device")})
    assert relay.production._snapshot(service, candidate, live) is None and calls[-1][0] == "unpause"
    from relay.production import ProductionError  # (production's podman raises on a time-out: said all the same)
    podman_answers(relay.production, monkeypatch, {"volume": (ProductionError("podman: timed out"), "")})
    assert relay.production._snapshot(service, candidate, live) is None
    failed = [e["detail"]["why"] for e in relay.agentos.events(user) if e["kind"] == "data.snapshot_failed"]
    assert sorted(failed) == ["Error: no space left on device", "podman: timed out"]


def test_deleting_a_service_keeps_its_workboxes(echo_chat, monkeypatch):
    """As the page says: its versions, archive and data go, its WorkBoxes (and their chats) stay."""
    from relay.production import folders
    env = echo_chat
    relay = env.app.state.relay
    user = relay.db.one("SELECT id FROM users")["id"]
    service, _ = fake_service(relay, user, "gone")
    home = folders() / service["id"]
    for path in ("work/ab12/gone/app.py", "1/gone/app.py", "snapshots/before-2.tar"):
        (home / path).parent.mkdir(parents=True, exist_ok=True)
        (home / path).write_text("x")
    podman_answers(relay.production, monkeypatch)
    env.portal.call(relay.production.delete, service)
    assert (home / "work" / "ab12" / "gone" / "app.py").exists()
    assert not (home / "1").exists() and not (home / "snapshots").exists()


def test_a_service_whose_box_wont_stop_isnt_told_stopped(echo_chat, monkeypatch):
    from relay.production import ProductionError
    env = echo_chat
    relay = env.app.state.relay
    user = relay.db.one("SELECT id FROM users")["id"]
    service, _ = fake_service(relay, user, "stubborn")
    podman_answers(relay.production, monkeypatch, {"stop": (125, "Error: timed out waiting for the box")})
    with pytest.raises(ProductionError, match="timed out waiting"):
        env.portal.call(relay.production.stop_service, service)
    assert relay.db.one("SELECT stopped FROM services WHERE id = ?", (service["id"],))["stopped"] is None
    podman_answers(relay.production, monkeypatch, {"stop": (ProductionError("podman: timed out"), "")})
    with pytest.raises(ProductionError, match="podman: timed out"):
        env.portal.call(relay.production.stop_service, service)
    assert relay.db.one("SELECT stopped FROM services WHERE id = ?", (service["id"],))["stopped"] is None


def test_one_services_trouble_at_the_start_doesnt_keep_relay_from_starting(echo_chat, monkeypatch):
    from relay.production import ProductionError
    env = echo_chat
    relay = env.app.state.relay
    user = relay.db.one("SELECT id FROM users")["id"]
    fake_service(relay, user, "hanging")
    fake_service(relay, user, "fine")
    calls = podman_answers(relay.production, monkeypatch, {"inspect": (ProductionError("podman: timed out"), "")})
    env.portal.call(relay.production.start)  # (no exception: each service's trouble is its own)
    assert sum(1 for c in calls if c[0] == "inspect") >= 2  # (the second one was still looked at)


def test_a_restored_backup_gives_a_service_its_data_back(echo_chat, monkeypatch):
    """On a machine without the service's data volume, the one relay backup put in volumes/ is imported
    before its box is made, once."""
    from relay import config
    from relay.production import ProductionError
    env = echo_chat
    relay = env.app.state.relay
    user = relay.db.one("SELECT id FROM users")["id"]
    service, _ = fake_service(relay, user, "restored")
    volume = f"relay-data-{service['id'][:12]}"
    saved = config.data_dir() / "volumes" / f"{volume}.tar"
    saved.parent.mkdir(parents=True, exist_ok=True)
    saved.write_bytes(b"a tar")
    calls = podman_answers(relay.production, monkeypatch, {"volume exists": (1, ""), "volume import": (125, "Error: bad tar")})
    with pytest.raises(ProductionError, match="couldn't be put back"):  # (its box isn't made on empty data)
        relay.production._restore_data(service)
    assert ["volume", "create", volume] in calls and ["volume", "import", volume, str(saved)] in calls
    assert ["volume", "rm", "-f", volume] in calls  # (not left empty, to look restored next time)
    assert saved.exists()  # (kept for another try)
    podman_answers(relay.production, monkeypatch, {"volume exists": (1, "")})
    relay.production._restore_data(service)
    assert not saved.exists() and saved.with_name(saved.name + ".imported").exists()
    calls = podman_answers(relay.production, monkeypatch, {"volume exists": (1, "")})
    relay.production._restore_data(service)
    assert calls == []  # (once)


def test_agentos_keeps_to_confined_providers(echo_chat, monkeypatch):
    """A provider that runs its program on the machine as it is (a command of the user's) isn't for
    AgentOS's agents: they're held in boxes."""
    from relay.agentos import AgentOSError
    from relay.providers.command import CommandProvider
    env = echo_chat
    relay = env.app.state.relay
    user = relay.db.one("SELECT id FROM users")["id"]
    monkeypatch.setattr(CommandProvider, "CONFINED", False)  # (as it is: the autouse fixture lets the tests' one in)
    with pytest.raises(AgentOSError, match="outside the boxes"):
        relay.agentos._provider(user, {"provider": "fake"}, "it's for")
    assert relay.agentos._provider(user, {"provider": "echo", "model": "m"}, "it's for") == ("echo", "m")
    # Nor for a task's agent, a repair's, or a fork of a chat on it (which goes on with its provider)
    task = {"name": "t", "message": "m", "governed": "false", "when": "in 1 hour"}
    mine = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    for call, arguments in ((relay.agentos._task_create, {**task, "provider": "fake"}),
                            (relay.agentos._provider_repair, {"provider": "echo", "helper": "fake"}),
                            (relay.agentos._chat_fork, {"id": mine["id"]})):
        with pytest.raises(AgentOSError, match="outside the boxes"):
            env.portal.call(call, None, user, arguments)
    assert "Made task" in env.portal.call(relay.agentos._task_create, None, user, {**task, "provider": "echo"})
    # And a fallback loop goes on in an AgentOS conversation only with a confined one
    from relay import fallback
    loop = {"entries": [{"provider": "fake", "model": ""}, {"provider": "echo", "model": ""}]}
    stopped = {"provider": "echo", "model": ""}
    assert fallback.next_in(relay.db, relay.registry, loop, stopped, [], False)["provider"] == "fake"
    assert fallback.next_in(relay.db, relay.registry, loop, stopped, [], False, confined=True) is None
    assert not fallback.confined(relay.registry, loop["entries"][0]) and fallback.confined(relay.registry, stopped)


def test_a_new_services_start_is_guessed_from_its_project(tmp_path):
    from relay.production import guess

    class Db:
        def all(self, *args):
            return [{"public_port": 8500}]
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "package.json").write_text('{"scripts": {"dev:web": "tsx src/server.ts", "dev": "x"}}')
    found = guess(Db(), tmp_path / "web", 8420)
    assert found["command"] == "npm run dev:web" and found["port"] != 8500 and found["name"] == "web"
    assert found["data_dir"] is False  # (its data, if any, would stay behind with each version: the dialog says so)
    (tmp_path / "web" / "node_modules").mkdir()
    (tmp_path / "web" / "node_modules" / "dep.js").write_text("process.env.DATA_DIR")  # (not its own code)
    assert guess(Db(), tmp_path / "web", 8420)["data_dir"] is False
    (tmp_path / "web" / "server.js").write_text("const db = join(process.env.DATA_DIR || '.data', 'app.db')")
    assert guess(Db(), tmp_path / "web", 8420)["data_dir"] is True
    (tmp_path / "web" / "package.json").write_text('{"scripts": {"start": "node ."}}')
    assert guess(Db(), tmp_path / "web", 8420)["command"] == "npm start"
    (tmp_path / "py").mkdir()
    (tmp_path / "py" / "main.py").write_text("")
    assert guess(Db(), tmp_path / "py", 8420)["command"] == "python3 main.py"
    (tmp_path / "composed").mkdir()
    (tmp_path / "composed" / "docker-compose.yml").write_text("services: {}")
    assert guess(Db(), tmp_path / "composed", 8420)["command"] == "docker compose up"


def test_a_services_workboxes_are_listed(echo_chat):
    env = echo_chat
    relay = env.app.state.relay
    user = relay.db.one("SELECT id FROM users")["id"]
    service, versions = fake_service(relay, user)

    # Its clones, to open from the service's page; a discarded one isn't listed
    clones = []
    for title in ("site: work on version 2", "site: an old try"):
        folder = env.tmp / "ws" / title.replace(" ", "-").replace(":", "")
        folder.mkdir(parents=True)
        row = env.post("/api/conversations", json={"provider_id": "fake", "mode": "code", "directory": str(folder),
                                                   "title": title}, headers=H).json()
        relay.db.execute("INSERT INTO service_clones (conversation_id, service_id, from_version, created) VALUES (?, ?, ?, ?)",
                         (row["id"], service["id"], versions[2], len(clones)))
        clones.append(row)
    relay.db.execute("UPDATE conversations SET task_state = 'discarded' WHERE id = ?", (clones[1]["id"],))
    shown = env.get(f"/api/services/{service['id']}").json()
    assert [(w["id"], w["title"], w["from_version"], w["running"]) for w in shown["workboxes"]] == \
        [(clones[0]["id"], "site: work on version 2", 2, False)]
    # The chat list knows them (the sidebar folds a service's under its newest); a plain chat isn't one
    listed = {c["id"]: c["clone"] for c in env.get("/api/conversations").json()}
    assert listed[clones[0]["id"]]["service_id"] == service["id"]  # (a WorkBox outside production/: /promote's)
    plain = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    assert plain["clone"] is None
    assert env.get("/api/services/nope").status_code == 404


def test_the_governor_updates_and_settles_interventions(echo_chat):
    """An open intervention: its id comes back and is in the briefing; the Governor may change its
    text and settle it (saying why), which takes it off the user's list and the briefings."""
    import re as re_
    env = echo_chat
    relay = env.app.state.relay
    agentos_on(env)
    user = relay.db.one("SELECT id FROM users")["id"]
    governor, run_id, tool = governor_lingering(env, relay)
    try:
        text, error = tool("agentos_notify_intervention", message="The site overflows on phones; fix the layout?")
        made = re_.search(r"\[([0-9a-f]{8})\]", text)[1]
        assert not error and f"notify.resolve id={made}" in text
        assert f"[{made}]" in relay.agentos.briefing(user, [], time.time(), "a round", relay.agentos.settings(user))
        text, error = tool("agentos_notify_update", id=made, message="The site overflows on phones (500 px); an agent works on it")
        assert not error, text
        [shown] = [i for i in env.get("/api/agentos").json()["interventions"] if i["id"].startswith(made)]
        assert shown["text"].endswith("an agent works on it") and shown["updated"] and not shown["resolved"]
        assert "Say why" in tool("agentos_notify_resolve", id=made)[0]  # (a why is needed)
        assert "No open intervention" in tool("agentos_notify_resolve", id="nothere", note="x")[0]
        text, error = tool("agentos_notify_resolve", id=made, note="Fixed in version 5; checked at 500 px: no overflow")
        assert not error, text
        [shown] = [i for i in env.get("/api/agentos").json()["interventions"] if i["id"].startswith(made)]
        assert shown["resolved"] and shown["resolved_by"] == "governor" and shown["resolution"].startswith("Fixed in")
        assert "Open interventions" not in relay.agentos.briefing(user, [], time.time(), "a round",
                                                                    relay.agentos.settings(user))
        assert "No open intervention" in tool("agentos_notify_update", id=made, message="again")[0]  # (settled)
        kinds = {(e["kind"], e["actor"]) for e in env.get("/api/agentos").json()["events"]}
        assert {("intervention.updated", "governor"), ("intervention.resolved", "governor")} <= kinds
        # The user settles one too: by the user
        other = relay.agentos.intervene(user, "Needs a password")
        env.post(f"/api/agentos/interventions/{other}/resolve", headers=H)
        assert relay.db.one("SELECT resolved_by FROM agentos_interventions WHERE id = ?", (other,))["resolved_by"] == "user"
    finally:
        let_go(env, governor)


def test_the_governor_waits_for_its_chats_reports_and_restarts_a_service(echo_chat, monkeypatch):
    """chat.wait instead of asking again and again; notify.report for what the user only needs to know
    (not an intervention); service.restart and service.delete for AgentOS's services, not the user's own;
    a hint to agent.review when a chat is asked in a managed task's folder."""
    env = echo_chat
    relay = env.app.state.relay
    agentos_on(env)
    user = relay.db.one("SELECT id FROM users")["id"]
    site, _ = fake_service(relay, user)
    mine, _ = fake_service(relay, user, "mine", live_number=3)
    env.patch(f"/api/services/{mine['id']}", json={"managed": False}, headers=H)
    governor, run_id, tool = governor_lingering(env, relay)
    try:
        assert tool("agentos_chat_wait")[0] == "No managed chat is working now."
        text, error = tool("agentos_chat_create", provider="fake", title="Slow one")
        made = relay.db.one("SELECT * FROM conversations WHERE title = 'Slow one'")
        relay.runs.start(made, "linger")
        started = time.time()
        text, error = tool("agentos_chat_wait", ids=[made["id"][:8]], timeout=1)
        assert not error and f"[{made['id'][:8]}] Slow one: still working" in text and time.time() - started < 5
        assert "It says: …You said: linger" in text and "you're woken" in text.lower()  # (how far it got)
        text, error = tool("agentos_chat_send", id=made["id"][:8], message="more")
        assert error and f"chat.wait id={made['id'][:8]}" in text
        (pathlib.Path(made["directory"]) / "done-lingering").write_text("")
        text, error = tool("agentos_chat_wait")  # (all that work now)
        assert not error and "You said: linger" in text and "still working" not in text
        # A report: on the page and pushed, not on the user's list of what needs them
        pushed = []
        monkeypatch.setattr(relay.notifier, "report", lambda text, conversation=None: pushed.append(text))
        text, error = tool("agentos_notify_report", message="The ranking is done: 34 models.")
        assert not error and pushed == ["The ranking is done: 34 models."]
        assert not relay.db.all("SELECT 1 FROM agentos_interventions WHERE text LIKE 'The ranking%'")
        assert any(e["kind"] == "report" and e["detail"]["text"].startswith("The ranking")
                   for e in env.get("/api/agentos").json()["events"])
        # Its services: restarted (its box made anew when it's gone), deleted when the user asks; not the user's own
        done = []

        async def restart(service):
            done.append(("restart", service["name"]))
            return "Restarted version 2: well (HTTP 200)."

        async def delete(service):
            done.append(("delete", service["name"]))
        monkeypatch.setattr(relay.production, "restart", restart)
        monkeypatch.setattr(relay.production, "delete", delete)
        assert tool("agentos_service_restart", service="site") == ("Restarted version 2: well (HTTP 200).", False)
        for capability in ("agentos_service_restart", "agentos_service_delete"):
            text, error = tool(capability, service="mine")
            assert error and "No service mine" in text
        text, error = tool("agentos_service_delete", service="site")
        assert not error and "Deleted the service site" in text
        assert done == [("restart", "site"), ("delete", "site")]
        assert "service.delete" in env.get("/api/agentos").json()["irreversible"]  # (ask first, if the user says so)
        # A chat in a managed code task's folder: agent.review is what looks there
        clone = env.post("/api/conversations", json={"provider_id": "fake", "mode": "code",
                                                     "directory": str(env.tmp / "ws" / "project")}, headers=H).json()
        outside = env.tmp / "elsewhere"
        outside.mkdir()
        relay.db.execute("UPDATE conversations SET managed = 1, directory = ? WHERE id = ?", (str(outside), clone["id"]))
        text, error = tool("agentos_chat_create", provider="fake", folder=str(outside), mode="readonly")
        assert error and f"agent.review id={clone['id'][:8]}" in text
    finally:
        let_go(env, governor)
    # The briefing gives the time with its zone: task times are in it
    briefing = relay.agentos.briefing(user, [], time.time(), "a round", relay.agentos.settings(user))
    assert re.search(r"AgentOS wake-up, \w{3} \d\d \w{3} \d{4} \d\d:\d\d \S+ \(UTC[+-]\d\d:\d\d\)", briefing)


def test_the_governor_dispatches_and_is_woken_when_its_agents_are_done(echo_chat, monkeypatch):
    """It needn't wait: an agent still working when chat.create returns is followed, and when it's done the
    Governor is woken for it (its answer in the briefing); one whose answer it saw while awake isn't news.
    Its own model is the default for its agents (own_model), unless the user turns that off."""
    from relay import agentos as agentos_module
    env = echo_chat
    relay = env.app.state.relay
    agentos_on(env)
    user = relay.db.one("SELECT id FROM users")["id"]
    monkeypatch.setattr(agentos_module, "SEND_WAIT", 1)
    governor, run_id, tool = governor_lingering(env, relay)
    try:
        text, error = tool("agentos_chat_create", title="Slow one", message="linger")  # (no provider: its own)
        assert not error and "you're woken when it's done" in text, text
        slow = relay.db.one("SELECT * FROM conversations WHERE title = 'Slow one'")
        assert slow["provider_id"] == governor["provider_id"] and slow["managed"]
        text, error = tool("agentos_chat_create", title="Quick one", message="quick")
        assert not error and "You said: quick" in text  # (seen while awake: no news)
        assert "Your own model (fake" in tool("agents")[0]
    finally:
        let_go(env, governor)
    assert not relay.agentos.news.get(user)
    let_go(env, slow)
    [news] = relay.agentos.news[user]
    assert news["kind"] == "done" and news["conversation"] == slow["id"] and news["status"] == "succeeded"
    env.portal.call(relay.agentos.wake_for_agents, time.time())  # (not yet: others may be done in a moment)
    assert relay.agentos.news[user]
    env.portal.call(relay.agentos.wake_for_agents, time.time() + agentos_module.DONE_QUIET)
    wait_idle(env, governor["id"])
    briefing = relay.db.all("SELECT content FROM messages WHERE conversation_id = ? AND role = 'user' ORDER BY id",
                            (governor["id"],))[-1]["content"]
    assert "one of your agents is done" in briefing and "Your agents that are done" in briefing
    assert f"[{slow['id'][:8]}] succeeded" in briefing and "You said: linger" in briefing
    assert "Your own model (fake" in briefing and not relay.agentos.news.get(user)
    # Off: it names the provider itself
    env.put("/api/agentos", json={"own_model": False}, headers=H)
    governor, run_id, tool = governor_lingering(env, relay)
    try:
        text, error = tool("agentos_chat_create", title="Whose?")
        assert error and "Say which provider it's for" in text
    finally:
        let_go(env, governor)


def test_a_task_left_awaiting_with_no_agent_at_work_wakes_the_governor_once(echo_chat, monkeypatch):
    """A wake-up that ends with a task awaiting its report, none of its agents working: nothing else would wake
    it before its round, so Relay does, once. A report without a task's note becomes the note."""
    from relay import agentos as agentos_module
    env = echo_chat
    relay = env.app.state.relay
    agentos_on(env)
    user = relay.db.one("SELECT id FROM users")["id"]
    task = env.post("/api/schedules", json={"name": "Look it up", "prompt": "x", "governed": True,
                                            "when": {"kind": "interval", "minutes": 4320}}, headers=H).json()
    env.post(f"/api/schedules/{task['id']}/run", headers=H)  # (the fake Governor answers, reports nothing)
    governor = relay.agentos.governor(user)
    wait_idle(env, governor["id"])
    assert relay.db.one("SELECT dispatch_state FROM schedules WHERE id = ?", (task["id"],))["dispatch_state"] == "awaiting"
    [news] = relay.agentos.news[user]
    assert news["kind"] == "awaiting" and news["title"] == "Look it up"
    env.portal.call(relay.agentos.wake_for_agents, time.time() + agentos_module.DONE_QUIET)
    wait_idle(env, governor["id"])
    briefing = relay.db.all("SELECT content FROM messages WHERE conversation_id = ? AND role = 'user' ORDER BY id",
                            (governor["id"],))[-1]["content"]
    assert "tasks await your report" in briefing and "[" + task["id"][:8] + "] Look it up" in briefing
    assert not relay.agentos.news.get(user)  # (and not again: once)
    assert relay.agentos.news_due[user] > time.time()  # (counted towards agentos.wakes_per_hour)
    # One left awaiting by an earlier wake-up (an agent was at work then): a later wake-up that ends with none
    # at work wakes it for that one too, once
    # (it was reported meanwhile: a turn of it awaiting again is nudged anew)
    relay.db.execute("UPDATE schedules SET dispatch_state = NULL WHERE id = ?", (task["id"],))
    relay.agentos.budgets["between"] = {"user": user, "started": time.time(), "agents": 0, "max_agents": 4, "max_cost": 2}
    relay.agentos.run_ended("between", "succeeded", governor["id"])
    assert task["id"] not in relay.agentos.nudged and not relay.agentos.news.get(user)
    relay.db.execute("UPDATE schedules SET dispatch_state = 'awaiting' WHERE id = ?", (task["id"],))
    relay.agentos.budgets["later"] = {"user": user, "started": time.time(), "agents": 0, "max_agents": 4, "max_cost": 2}
    relay.agentos.run_ended("later", "succeeded", governor["id"])
    assert [n["kind"] for n in relay.agentos.news[user]] == ["awaiting"]
    relay.agentos.news.pop(user)
    # Woken for news no more often than agentos.wakes_per_hour: the rest waits (and comes along later), said once
    from relay import tunables as tunables_
    monkeypatch.setitem(tunables_._values, "agentos.wakes_per_hour", 1)
    relay.agentos.news_due[user] = time.time() + 3600  # (woken just now)
    relay.agentos.news[user] = [{"kind": "done", "conversation": governor["id"], "title": "x", "run": "r1",
                                 "status": "succeeded", "at": time.time() - 60}]
    env.portal.call(relay.agentos.wake_for_agents, time.time())
    env.portal.call(relay.agentos.wake_for_agents, time.time())
    assert relay.agentos.news[user] and sum(e["kind"] == "wake.held" for e in relay.agentos.events(user)) == 1
    relay.agentos.news.pop(user)
    # A rate, not a window: of 6 an hour, 3 may come at once, then one every 10 minutes; news never waits for an
    # hour to pass, and an hour has at most 9
    monkeypatch.setitem(tunables_._values, "agentos.wakes_per_hour", 6)
    relay.agentos.news_due.pop(user)
    now, woken = time.time(), []
    for minute in range(61):
        at = now + minute * 60
        while relay.agentos._news_wait(user, at) == 0:
            woken.append(minute)
            relay.agentos._news_woken(user, at)
    assert woken == [0, 0, 0, 10, 20, 30, 40, 50, 60]
    assert 0 < relay.agentos._news_wait(user, now + 61 * 60) <= 600
    # An agent's run over before it was followed (a provider failing at once): its end is news all the same, once
    relay.agentos._follow("gone-at-once", {"user_id": user, "id": governor["id"], "title": "Quick one"})
    assert "gone-at-once" not in relay.agentos.following
    assert [(n["kind"], n["title"]) for n in relay.agentos.news.pop(user)] == [("done", "Quick one")]
    # Reported without a note, then a report: that's the note
    governor, run_id, tool = governor_lingering(env, relay)
    try:
        assert task["id"] in relay.agentos.nudged
        assert not tool("agentos_task_done", id=task["id"][:8])[1]
        assert task["id"] not in relay.agentos.nudged  # (a later turn awaiting is nudged anew)
        tool("agentos_notify_report", message="Looked it up: 42.")
        assert relay.db.one("SELECT outcome_note FROM schedules WHERE id = ?", (task["id"],))["outcome_note"] == "Looked it up: 42."
    finally:
        let_go(env, governor)


def restart_relay(relay):
    """Relay stopped as a restart stops it (its runs cut off) and started again on the same data: what AgentOS
    knew only in memory is gone with the process."""
    from relay.agentos import AgentOS
    relay.runs.shutdown()
    relay.runs.shutting_down = False
    relay.agentos = AgentOS(relay)
    relay.agentos.reconcile(time.time(), {run["id"] for run in relay.db.fail_unfinished_runs()})
    relay.scheduler.resumed = False


def test_a_restart_of_relay_loses_nothing_the_governor_followed(echo_chat, monkeypatch):
    """What it follows and its news are kept: after a restart, an agent's turn the restart cut off goes on
    (followed again, the Governor told), and a wake-up of its that was cut off is done again. Not an agent the
    user stopped: that's news as it was."""
    from relay import agentos as agentos_module
    env = echo_chat
    relay = env.app.state.relay
    agentos_on(env)
    user = relay.db.one("SELECT id FROM users")["id"]
    monkeypatch.setattr(agentos_module, "SEND_WAIT", 1)
    monkeypatch.setattr(agentos_module, "RESUMED", agentos_module.RESUMED + " (linger)")  # (it works on a while)
    env.portal.call(relay.agentos.wake, user, [], time.time(), "its round (linger)")
    governor = relay.agentos.governor(user)
    token = relay.db.one("SELECT tools_token FROM conversations WHERE id = ?", (governor["id"],))["tools_token"]

    def tool(name, **arguments):
        result = mcp(env, token, "tools/call", {"name": name, "arguments": arguments})[1]["result"]
        return result["content"][0]["text"], result.get("isError", False)
    for title in ("Slow one", "Stopped one"):
        text, error = tool("agentos_chat_create", title=title, message="linger")
        assert not error and "you're woken when it's done" in text, text
    slow, stopped = (relay.db.one("SELECT * FROM conversations WHERE title = ?", (t,)) for t in ("Slow one", "Stopped one"))
    # Only AgentOS's work going: that isn't a reason to wait with a restart (it goes on after); the user's is
    restarted = []
    monkeypatch.setattr("relay.routes.settings.restart_process", lambda: restarted.append(True))
    assert env.post("/api/restart", headers=H).json() == {"ok": True}
    mine = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    run = env.post(f"/api/conversations/{mine['id']}/messages", json={"text": "linger"}, headers=H).json()
    refused = env.post("/api/restart", headers=H)
    assert refused.status_code == 409 and "1 agent is still working" in refused.json()["error"]
    env.post(f"/api/runs/{run['run_id']}/stop", headers=H)
    wait_idle(env, mine["id"])
    env.post(f"/api/runs/{relay.runs.active_run_for(stopped['id'])}/stop", headers=H)  # (the user's Stop)
    wait_idle(env, stopped["id"])
    restart_relay(relay)
    assert not relay.agentos.following
    assert {(n["kind"], n["conversation"], bool(n.get("cut_off"))) for n in relay.agentos.news[user]} == {
        ("restarted", governor["id"], False), ("done", slow["id"], True), ("done", stopped["id"], False)}
    assert {n["conversation"]: n.get("why") for n in relay.agentos.news[user] if n["kind"] == "done"} == {
        slow["id"]: "Relay restarted", stopped["id"]: "the user stopped it"}
    env.portal.call(relay.scheduler.tick, time.time() + agentos_module.DONE_QUIET)
    # The agent the restart cut off went on, followed again; the one the user stopped didn't
    users = [m for m in env.get(f"/api/conversations/{slow['id']}").json()["messages"] if m["role"] == "user"]
    assert users[-1]["relay_action"]["kind"] == "resume" and users[-1]["content"].startswith("Relay restarted while")
    going = relay.runs.active_run_for(slow["id"])
    assert going and going in relay.agentos.following and relay.runs.active_run_for(stopped["id"]) is None
    assert any(e["kind"] == "agent.resumed" and e["conversation_id"] == slow["id"] for e in relay.agentos.events(user))
    # The Governor woken again: told what was cut off, what went on, and what's done
    briefing = relay.db.all("SELECT content FROM messages WHERE conversation_id = ? AND role = 'user' ORDER BY id",
                            (governor["id"],))[-1]["content"]
    assert "Relay restarted during your last wake-up" in briefing and "(its round (linger)), which cut it off" in briefing
    assert f"whose turn its restart cut off (you're woken when they're done): [{slow['id'][:8]}] Slow one" in briefing
    assert f"[{stopped['id'][:8]}] interrupted" in briefing and "cut this turn off" not in briefing
    let_go(env, governor)
    let_go(env, slow)
    [news] = relay.agentos.news[user]
    assert news["kind"] == "done" and news["conversation"] == slow["id"] and news["status"] == "succeeded"


def test_the_governor_hears_why_a_turn_stopped_and_what_its_budget_is(echo_chat, monkeypatch):
    """An agent's turn that didn't succeed comes with who stopped it, after how long, what it cost and its last
    steps. The budget is per wake-up, with what AgentOS spent, and each start says what's left of it."""
    from relay import agentos as agentos_module
    env = echo_chat
    relay = env.app.state.relay
    agentos_on(env)
    user = relay.db.one("SELECT id FROM users")["id"]
    monkeypatch.setattr(agentos_module, "SEND_WAIT", 1)
    env.portal.call(relay.agentos.wake, user, [], time.time(), "its round (linger)")
    governor = relay.agentos.governor(user)
    token = relay.db.one("SELECT tools_token FROM conversations WHERE id = ?", (governor["id"],))["tools_token"]
    result = mcp(env, token, "tools/call", {"name": "agentos_chat_create",
                                            "arguments": {"title": "Slow one", "message": "tool linger"}})[1]["result"]
    assert result["content"][0]["text"].endswith("\n(This wake-up: 1 of 4 agents, about $0.00 of $2.00.)")
    slow = relay.db.one("SELECT * FROM conversations WHERE title = 'Slow one'")
    env.post(f"/api/runs/{relay.runs.active_run_for(slow['id'])}/stop", headers=H)  # (the user's Stop)
    wait_idle(env, slow["id"])
    [news] = relay.agentos.news[user]
    assert (news["status"], news["why"]) == ("interrupted", "the user stopped it")
    assert relay.db.run_events(news["run"])[-1] == {"type": "done", "status": "interrupted", "why": "the user stopped it"}
    relay.db.execute("UPDATE runs SET usage = ? WHERE id = ?", (json.dumps({"cost": 0.42}), news["run"]))
    lines = "\n".join(relay.agentos._news_lines([news]))
    assert f"[{slow['id'][:8]}] interrupted" in lines
    assert "  Interrupted after 0 min (the user stopped it); it cost about $0.42; its last steps:\n  shell ls: ok" in lines
    briefing = relay.agentos.briefing(user, [], time.time(), "a round", relay.agentos.settings(user))
    assert ("Budget, per wake-up (each has its own, also when a task spans several): at most 4 agents started "
            "(chat.create, chat.send, agent.review and the like), about $2.00. AgentOS spent about $0.42 in the last "
            "24 hours") in briefing
    let_go(env, governor)


def test_after_a_crash_the_governor_hears_what_it_cut_off(echo_chat, monkeypatch):
    """A crash keeps nothing of memory, but what was kept is there: the turns it cut off (left going, failed at
    the start) are news as a restart's are, so the agent goes on and the wake-up is done again. A fallback agent
    that takes over an agent's turn is followed in its place, and kept."""
    from relay.agentos import KEPT, AgentOS
    env = echo_chat
    relay = env.app.state.relay
    agentos_on(env)
    user = relay.db.one("SELECT id FROM users")["id"]
    env.post("/api/agentos/wake", headers=H)
    governor = relay.agentos.governor(user)
    wait_idle(env, governor["id"])
    agent = env.post("/api/conversations", json={"provider_id": "fake", "title": "Builder"}, headers=H).json()
    relay.db.execute("UPDATE conversations SET managed = 1 WHERE id = ?", (agent["id"],))
    agent = relay.db.one("SELECT * FROM conversations WHERE id = ?", (agent["id"],))
    # Both at work when Relay died: their runs left going, the agent followed (kept), the wake-up logged
    now = time.time()
    for run_id, conversation in (("crashed-agent", agent["id"]), ("crashed-governor", governor["id"])):
        relay.db.execute("INSERT INTO runs (id, conversation_id, status, created) VALUES (?, ?, 'running', ?)",
                         (run_id, conversation, now - 60))
    relay.agentos.following["crashed-agent"] = {"user": user, "conversation": agent["id"], "title": "Builder",
                                                "started": now - 60}
    relay.agentos._keep()
    relay.agentos.event(user, "wake", run="crashed-governor", conversation=governor["id"], reason="1 task is due")
    relay.agentos = AgentOS(relay)
    relay.agentos.reconcile(time.time(), {run["id"] for run in relay.db.fail_unfinished_runs()})
    assert {(n["kind"], n["run"], bool(n.get("cut_off"))) for n in relay.agentos.news[user]} == {
        ("done", "crashed-agent", True), ("restarted", "crashed-governor", False)}
    assert relay.agentos.resume_agents(time.time()) == [agent["id"]]
    wait_idle(env, agent["id"])
    lines = "\n".join(relay.agentos._news_lines(relay.agentos.news[user]))
    assert "Relay restarted during your wake-up of " in lines and "(1 task is due), which cut it off" in lines
    assert f"[{agent['id'][:8]}] Builder" in lines and f"[{agent['id'][:8]}] succeeded" in lines
    # A fallback agent took over an agent's turn: it isn't done, the new turn is followed (and kept)
    relay.agentos.news.pop(user)
    relay.agentos.following["failing"] = {"user": user, "conversation": agent["id"], "title": "Builder",
                                          "started": time.time()}
    monkeypatch.setattr(relay.runs, "active_run_for", lambda conversation_id: "fallback")
    relay.agentos.run_ended("failing", "failed", agent["id"])
    assert list(relay.agentos.following) == ["fallback"] and not relay.agentos.news.get(user)
    kept = json.loads(relay.db.one("SELECT value FROM meta WHERE key = ?", (KEPT,))["value"])
    assert list(kept["following"]) == ["fallback"]


def test_the_governor_looks_in_and_steers_an_agent_that_seems_to_go_wrong(echo_chat, monkeypatch):
    """Steps failing in a row, or a long turn, wake it (once each), with how far the agent got; chat.read
    shows that too; chat.tell interrupts it with what to do instead; workbox.look reads its folder."""
    from relay import agentos as agentos_module
    env = echo_chat
    relay = env.app.state.relay
    agentos_on(env)
    user = relay.db.one("SELECT id FROM users")["id"]
    monkeypatch.setattr(agentos_module, "SEND_WAIT", 1)
    governor, run_id, tool = governor_lingering(env, relay)
    try:
        project = env.tmp / "ws" / "project"
        tool("agentos_chat_create", provider="fake", folder=str(project), title="Builder", message="linger")
        builder = relay.db.one("SELECT * FROM conversations WHERE title = 'Builder'")
        working = relay.runs.active_run_for(builder["id"])
        active = relay.runs.active[working]
        with active.lock:
            for n in range(4):
                active.events += [{"type": "tool_call", "id": f"t{n}", "name": "Bash", "input": {"command": "npm test"}},
                                  {"type": "tool_result", "id": f"t{n}", "name": "Bash", "ok": False, "output": "1 failed"}]
        relay.agentos.watch(time.time())
        relay.agentos.watch(time.time())  # (once)
        signals = relay.agentos.news[user]
        assert [n["kind"] for n in signals] == ["signal", "signal"] and signals[0]["signal"] == "4 steps failed in a row"
        assert relay.agentos.news[user][-1]["signal"] == "the same step 4 times in a row"  # (npm test, again)
        relay.agentos.watch(time.time() + 31 * 60)
        assert relay.agentos.news[user][-1]["signal"] == "working for 31 minutes on one turn"
        lines = "\n".join(relay.agentos._news_lines(relay.agentos.news[user]))
        assert "may be going wrong" in lines and "Bash npm test: FAILED" in lines and "You said: linger" in lines
        read = tool("agentos_chat_read", id=builder["id"][:8])[0]
        assert "(working now)" in read and "Its turn now:" in read and "Bash npm test: FAILED" in read
        # A long answer comes in parts
        tool("agentos_chat_create", provider="fake", title="Long one", message="y" * 9000)
        long = relay.db.one("SELECT id FROM conversations WHERE title = 'Long one'")["id"]
        first = tool("agentos_chat_read", id=long[:8], last=1)[0]
        assert f"more characters: chat.read id={long[:8]} offset=8000" in first
        rest = tool("agentos_chat_read", id=long[:8], offset=8000)[0]
        assert "its last answer from character 8000" in rest and rest.rstrip().endswith(")") and "more:" not in rest
        # It looks in, read-only
        looked = []
        monkeypatch.setattr(relay.containers, "look", lambda folder, command: looked.append((str(folder), command))
                            or (0, "package.json\nsrc"), raising=False)
        text, error = tool("agentos_workbox_look", id=builder["id"][:8], command="ls")
        assert not error and "(read-only), exit code 0" in text and "package.json" in text
        assert looked == [(str(project), "ls")]
        chat = tool("agentos_chat_create", provider="fake", title="Just a chat")[0]
        assert "a chat, not a code task" in tool("agentos_workbox_look", id=chat.split("[")[1][:8], command="ls")[0]
        # …and steers it: interrupted, the message its next turn
        text, error = tool("agentos_chat_tell", id=builder["id"][:8], message="Fix the failing test before anything else")
        assert not error and text.startswith("Interrupted it;") and "A message from the Governor" in text, text
        assert relay.db.one("SELECT status FROM runs WHERE id = ?", (working,))["status"] == "interrupted"
        assert all(n["conversation"] != builder["id"] or n["kind"] == "signal" for n in relay.agentos.news[user])
    finally:
        let_go(env, governor)


def test_a_service_is_stopped_with_its_data_kept_and_started_again(echo_chat, monkeypatch):
    """Stopped: its address answers nothing, it isn't checked, its versions and data stay; started: its live
    version, checked, at its address again. The user's buttons and the Governor's service.stop/start."""
    import asyncio

    from relay import production as production_
    env = echo_chat
    relay = env.app.state.relay
    agentos_on(env)
    user = relay.db.one("SELECT id FROM users")["id"]
    site, versions = fake_service(relay, user)
    relay.db.execute("UPDATE service_versions SET port = 21998 WHERE id = ?", (versions[2],))
    proxy = SimpleNamespace(target=21998, close=lambda: None)
    relay.production.proxies[site["id"]] = proxy
    calls = []
    monkeypatch.setattr(relay.production, "_podman", lambda *a, **k: calls.append(a) or SimpleNamespace(returncode=0, stdout=""))

    async def well(service, version, port):
        return True, "HTTP 200"
    monkeypatch.setattr(relay.production, "_wait_well", well)
    probed = []
    monkeypatch.setattr(production_, "probe", lambda port, health, timeout=5: probed.append(port) or (True, "HTTP 200"))
    answer = env.post(f"/api/services/{site['id']}/stop", headers=H).json()
    assert "its versions and data are kept" in answer["message"] and proxy.target is None
    assert calls[-1][:1] == ("stop",) and relay.production.get(user, "site")["stopped"]
    asyncio.run(relay.production.check(time.time() + 10**6))
    assert probed == []  # (not checked)
    probed.append("status")  # (service.status checks it just now, stopped or not)
    governor, run_id, tool = governor_lingering(env, relay)
    try:
        assert "stopped (by hand): site" in relay.agentos.briefing(user, [], time.time(), "x", relay.agentos.settings(user))
        assert "STOPPED (by hand)" in tool("agentos_service_status", service="site")[0]
        text, error = tool("agentos_service_start", service="site")
        assert not error and "Started site (version 2): well" in text, text
    finally:
        let_go(env, governor)
    assert proxy.target == 21998 and not relay.production.get(user, "site")["stopped"]
    # A restart of a stopped one starts it
    env.post(f"/api/services/{site['id']}/stop", headers=H)
    assert "Started site" in env.post(f"/api/services/{site['id']}/restart", headers=H).json()["message"]


def test_agentos_can_be_left_out_of_relay(echo_chat):
    """Settings → AgentOS, one switch: out, the UI has none of it (/api/state says so), the Governor isn't woken
    (a round, a task for it, Wake now), its tasks aren't on the Tasks page; what it made stays, and it comes
    back as it was."""
    from relay.agentos import AgentOSError
    env = echo_chat
    relay = env.app.state.relay
    user = relay.db.one("SELECT id FROM users")["id"]
    agentos_on(env)
    assert env.get("/api/state").json()["agentos"] is True and relay.agentos.on(user)
    folder = env.tmp / "ws" / "managed-site"
    folder.mkdir(parents=True)
    task = env.post("/api/conversations", json={"provider_id": "fake", "mode": "code", "directory": str(folder),
                                                "isolate": False}, headers=H).json()
    relay.db.execute("UPDATE conversations SET managed = 1 WHERE id = ?", (task["id"],))
    listed = lambda: [t["id"] for group in env.get("/api/tasks").json().values() if isinstance(group, list)  # noqa: E731
                      for t in group if isinstance(t, dict)]
    assert task["id"] in listed()
    saved = env.put("/api/agentos", json={"included": False}, headers=H).json()
    assert saved["included"] is False and saved["enabled"] is True  # (the Governor's own switch is kept)
    assert env.get("/api/state").json()["agentos"] is False and not relay.agentos.on(user)
    assert not relay.agentos.due_for_round(user, time.time() + 10**6) and user not in relay.agentos.enabled_users()
    with pytest.raises(AgentOSError, match="AgentOS is turned off"):
        env.portal.call(relay.agentos.wake, user, [], time.time(), "a round")
    assert env.post("/api/agentos/wake", headers=H).status_code == 409
    assert task["id"] not in listed()  # (kept, not shown)
    assert relay.db.one("SELECT 1 FROM conversations WHERE id = ?", (task["id"],))
    env.put("/api/agentos", json={"included": True}, headers=H)
    assert relay.agentos.on(user) and task["id"] in listed() and env.get("/api/state").json()["agentos"] is True


def test_the_governor_makes_a_new_service_from_its_agents_task(echo_chat, monkeypatch):
    """service.create (the user: it may, for sites it designs on its own): from a managed code task's folder,
    looked after by AgentOS, the task its WorkBox; its start guessed when not said. Not from the user's own."""
    from relay.agentos import AgentOSError
    env = echo_chat
    relay = env.app.state.relay
    user = relay.db.one("SELECT id FROM users")["id"]
    made = []

    async def create(user_, name, source, command, port, health, managed=False, by="user"):
        made.append((name, str(source), command, port, health, managed, by))
        service, versions = fake_service(relay, user_, name, live_number=1)
        return relay.production.one(user_, service["id"])
    monkeypatch.setattr(relay.production, "create", create)
    site = env.tmp / "ws" / "atlas"
    site.mkdir(parents=True)
    (site / "package.json").write_text('{"scripts": {"start": "node server.js"}}')
    (site / "server.js").write_text("const data = process.env.DATA_DIR")
    task = env.post("/api/conversations", json={"provider_id": "fake", "mode": "code", "directory": str(site),
                                                "isolate": False}, headers=H).json()
    run = type("Run", (), {"id": "run12345"})()
    with pytest.raises(AgentOSError, match="No managed chat"):  # (the user's own: not the Governor's to put live)
        env.portal.call(relay.agentos._service_create, run, user, {"id": task["id"]})
    relay.db.execute("UPDATE conversations SET managed = 1 WHERE id = ?", (task["id"],))
    said = env.portal.call(relay.agentos._service_create, run, user, {"id": task["id"][:8], "health": "/health"})
    name, source, command, port, health, managed, by = made[0]
    assert (name, source, command, health, managed, by) == ("atlas", str(site), "npm start", "/health", True,
                                                            "governor:run12345") and 8500 <= port < 9500
    assert said.startswith("Made service atlas: version 1 is live") and f"id={task['id'][:8]}" in said
    linked = relay.db.one("SELECT service_id FROM service_clones WHERE conversation_id = ?", (task["id"],))
    assert linked and relay.production.one(user, linked["service_id"])["name"] == "atlas"
    # Once its WorkBox, not a second service from it
    with pytest.raises(AgentOSError, match="WorkBox already"):
        env.portal.call(relay.agentos._service_create, run, user, {"id": task["id"], "name": "atlas-two"})
    # Nothing in the folder says how it starts: it's asked
    bare = env.tmp / "ws" / "bare"
    bare.mkdir()
    other = env.post("/api/conversations", json={"provider_id": "fake", "mode": "code", "directory": str(bare),
                                                 "isolate": False}, headers=H).json()
    relay.db.execute("UPDATE conversations SET managed = 1 WHERE id = ?", (other["id"],))
    with pytest.raises(AgentOSError, match="Say how it starts"):
        env.portal.call(relay.agentos._service_create, run, user, {"id": other["id"]})
    from relay.agentos import CAPABILITIES, IRREVERSIBLE
    assert "service.create" in CAPABILITIES and "service.create" in IRREVERSIBLE  # (ask first, if the user says so)


def test_the_governor_puts_a_services_data_back_from_a_snapshot(echo_chat, monkeypatch):
    """service.restore: one of the snapshots service.status lists (an "ask first" action when the user says so)."""
    from relay import agentos as agentos_module
    env = echo_chat
    relay = env.app.state.relay
    user = relay.db.one("SELECT id FROM users")["id"]
    site, _ = fake_service(relay, user)
    relay.db.execute("UPDATE services SET managed = 1 WHERE id = ?", (site["id"],))
    asked = []

    async def restore(service, name, by="user"):
        asked.append((service["name"], name, by))
        return f"{service['name']}'s data is as it was in {name}"
    monkeypatch.setattr(relay.production, "restore", restore)
    assert "service.restore" in agentos_module.CAPABILITIES and "service.restore" in agentos_module.IRREVERSIBLE
    run = type("Run", (), {"id": "run12345"})()
    said = env.portal.call(relay.agentos._service_restore, run, user, {"service": "site", "snapshot": "daily-2026-10-06"})
    assert said.endswith("daily-2026-10-06") and asked == [("site", "daily-2026-10-06", "governor:run12345")]
    from relay.delegation import AGENTOS_TOOLS
    assert any(t["name"] == "agentos_service_restore" for t in AGENTOS_TOOLS)


def test_a_services_last_week_its_failures_and_its_data(echo_chat, monkeypatch):
    """Its health as it changed and its data's size once a day are kept: service.status shows its last week."""
    import asyncio

    from relay import production as production_
    env = echo_chat
    relay = env.app.state.relay
    user = relay.db.one("SELECT id FROM users")["id"]
    site, versions = fake_service(relay, user)
    relay.db.execute("UPDATE service_versions SET port = 21997 WHERE id = ?", (versions[2],))
    states = iter([True, False, False, True, True])
    monkeypatch.setattr(production_, "probe", lambda port, health, timeout=5: (
        (True, "HTTP 200") if next(states) else (False, "no answer (Connection refused)")))
    sizes = iter([1_000_000, 2_500_000])
    monkeypatch.setattr(relay.production, "_data_size", lambda service: next(sizes))
    start = time.time() + 10**6
    for at in [0, 1, 2, 3]:
        asyncio.run(relay.production.check(start + at * 60))
    # (the next calendar day, at noon: start + 86400 is the same day when the clocks go back that night)
    day = time.localtime(start)
    next_day = time.mktime((day.tm_year, day.tm_mon, day.tm_mday + 1, 12, 0, 0, 0, 0, -1))
    asyncio.run(relay.production.check(next_day))  # (its size again)
    found = relay.production.history(site, now=next_day + 100)
    assert [c["ok"] for c in found["changes"]] == [1, 0, 1]
    assert [d["bytes"] for d in found["data"]] == [1_000_000, 2_500_000]
    lines = production_.describe_history(found)
    kept = time.strftime("%a %d %b", time.localtime(start))
    assert lines[0].startswith(f"Its last 7 days (Relay has kept it since {kept}): failing 1 times: ")
    assert "for 2 min (no answer (Connection refused))" in lines[0]
    assert "Its data: 2.5 MB on" in lines[1] and "; 1.0 MB on" in lines[1]
    # A week on, it's the whole week; before any check, it says there's none yet
    later = production_.describe_history(relay.production.history(site, now=start + 8 * 86400))
    assert later[0] == "Its last 7 days: no failed health checks."
    relay.db.execute("DELETE FROM service_health")
    relay.db.execute("DELETE FROM service_data")
    assert production_.describe_history(relay.production.history(site))[0] == (
        "Its last 7 days (Relay keeps none yet): no failed health checks.")
    # A volume that couldn't be measured (huge, du timed out) isn't tried again every minute: once a day
    tried = []
    monkeypatch.setattr(relay.production, "_data_size", lambda service: tried.append(1))
    later = start + 10 * 86400
    for minute in range(3):
        relay.production._note_health(site, True, "HTTP 200", later + minute * 60)
    assert len(tried) == 1


def test_the_governors_own_chat_stays_the_governors_in_another_mode(echo_chat):
    """Read-only chosen in the Governor's chat: it's still the Governor (its capabilities, its rules), asked
    only to look and answer now."""
    env = echo_chat
    relay = env.app.state.relay
    agentos_on(env)
    user = relay.db.one("SELECT id FROM users")["id"]
    env.post("/api/agentos/wake", headers=H)
    governor = relay.agentos.governor(user)
    wait_idle(env, governor["id"])
    env.patch(f"/api/conversations/{governor['id']}", json={"agent_mode": "readonly"}, headers=H)
    run_turn(env, governor["id"], "which mode")
    said = answers(env, governor["id"])[-1]
    assert "mode=governor/" in said and "You are the Governor of Relay AgentOS" in said
    assert "For now the user chose the mode Read-only in your chat: only look and answer" in said
    assert relay.db.one("SELECT agent_mode FROM runs WHERE conversation_id = ? ORDER BY created DESC LIMIT 1",
                        (governor["id"],))["agent_mode"] == "governor"


def test_a_failing_service_that_comes_and_goes_is_one_incident(echo_chat, monkeypatch):
    """Told failing once; well again only after WELL_CHECKS checks in a row, so a service that comes
    and goes doesn't wake the Governor each time. A restart makes a box that's gone anew."""
    import asyncio

    from relay import production as production_
    env = echo_chat
    relay = env.app.state.relay
    user = relay.db.one("SELECT id FROM users")["id"]
    site, versions = fake_service(relay, user)
    relay.db.execute("UPDATE service_versions SET port = 21999 WHERE id = ?", (versions[2],))
    states = iter([False, False, True, True, False, False] + [True] * production_.WELL_CHECKS + [False, False])
    monkeypatch.setattr(production_, "probe", lambda port, health: (next(states), "probed"))
    told = []
    for minute in range(8 + production_.WELL_CHECKS):
        for failing in asyncio.run(relay.production.check(1000.0 + minute * 60)):
            told.append(minute)
            relay.production.alerted(failing, 1000.0 + minute * 60)
    assert told == [1, 7 + production_.WELL_CHECKS]  # (not again at minute 5: it was well only a moment)
    # Restarted: its box gone, made anew
    calls = []
    monkeypatch.setattr(relay.production, "_podman", lambda *a, **k: calls.append(a) or SimpleNamespace(returncode=125))
    monkeypatch.setattr(relay.production, "_start", lambda service, version, port: calls.append(("start", version["number"])))

    async def well(service, version, port):
        return True, "HTTP 200"
    monkeypatch.setattr(relay.production, "_wait_well", well)
    assert asyncio.run(relay.production.restart(site)) == "Restarted version 2: well (HTTP 200)."
    assert calls[0][0] == "restart" and calls[1] == ("start", 2)


def test_task_list_says_when_next_and_how_it_went_last(echo_chat):
    env = echo_chat
    relay = env.app.state.relay
    agentos_on(env)
    governor, run_id, tool = governor_lingering(env, relay)
    try:
        task = env.post("/api/schedules", json={"name": "Atlas", "prompt": "Add the new election data",
                                                "when": {"kind": "daily", "time": "08:00"}, "governed": True,
                                                "timezone": "Europe/Amsterdam"}, headers=H).json()
        listed = tool("agentos_task_list")[0]
        assert "every day at 08:00 (Europe/Amsterdam); next: " in listed and "last: never" in listed
        assert "the user's (carry it out, don't change it)" in listed and "task: Add the new election data" in listed
        done_at = 1759478400  # (Fri 03 Oct 2025 10:00 in Amsterdam)
        relay.db.execute("UPDATE schedules SET last_run = ?, outcome = 'failed', outcome_by = 'governor', "
                         "outcome_note = 'The source was down', outcome_at = ?, dispatch_state = 'awaiting', "
                         "attempts = 1 WHERE id = ?", (done_at - 300, done_at, task["id"]))
        listed = tool("agentos_task_list")[0]
        assert "last: Fri 03 Oct 09:55; outcome FAILED (you reported, Fri 03 Oct 10:00): The source was down" in listed
        assert "now: given to you (attempt 2), awaiting your task.done" in listed
        assert env.put(f"/api/schedules/{task['id']}", json={"enabled": False}, headers=H).status_code == 200
        assert "next: not planned: it's off" in tool("agentos_task_list")[0]
    finally:
        let_go(env, governor)


def test_service_status_says_what_runs(echo_chat):
    """In detail, for finding out what's wrong; here a live version whose box is gone."""
    from relay.production import describe_status
    env = echo_chat
    relay = env.app.state.relay
    user = relay.db.one("SELECT id FROM users")["id"]
    service, versions = fake_service(relay, user)
    relay.db.execute("UPDATE service_versions SET note = 'it stopped\nTraceback …' WHERE id = ?", (versions[1],))
    relay.db.execute("UPDATE services SET health_ok = 0, health_detail = 'no answer', health_at = 1 WHERE id = ?",
                     (service["id"],))
    relay.production.failed[service["id"]] = 3
    found = relay.production.status(service)
    assert found["box"]["status"] == "missing" and found["health"]["failed_in_a_row"] == 3
    text = describe_status(found)
    assert "Live: version 2" in text and "gone (Relay makes it anew" in text
    assert "FAILING (no answer)" in text and "failed 3 times in a row; not told" in text
    assert "- version 1: archived (it stopped)" in text
    shown = env.get(f"/api/services/{service['id']}/status").json()
    assert shown["box"]["status"] == "missing" and shown["text"] == text


def test_conversations_in_a_clones_folder_belong_to_its_service(echo_chat):
    """The sidebar folds a service's WorkBoxes by folder: the clone, a fork of it, a reviewer in it, and a
    clone from before service_clones (no record: no version)."""
    from relay.production import folders
    env = echo_chat
    relay = env.app.state.relay
    user = relay.db.one("SELECT id FROM users")["id"]
    service, versions = fake_service(relay, user)

    def task(title, folder):  # (made in the workspace, then put where Relay's clones are)
        folder.mkdir(parents=True, exist_ok=True)
        row = env.post("/api/conversations", json={"provider_id": "fake", "mode": "code", "title": title,
                                                   "directory": str(env.tmp / "ws" / "project"), "isolate": False},
                       headers=H).json()
        relay.db.execute("UPDATE conversations SET managed = 1, directory = ? WHERE id = ?", (str(folder), row["id"]))
        return row
    clone_folder = folders() / service["id"] / "work" / "aaaa1111" / "site"
    clone = task("site: work on version 2", clone_folder)
    relay.db.execute("INSERT INTO service_clones (conversation_id, service_id, from_version, created) VALUES (?, ?, ?, 0)",
                     (clone["id"], service["id"], versions[2]))
    fork = task("site: work on version 2 (fork)", clone_folder)
    old = task("site: work on version 1", folders() / service["id"] / "work" / "bbbb2222" / "site")
    listed = {c["id"]: c["clone"] for c in env.get("/api/conversations").json()}
    mark = {"service_id": service["id"], "service": "site"}
    assert listed[clone["id"]] == {**mark, "version": 2, "own": True, "fresh": True}  # (made from the live one)
    assert listed[fork["id"]] == {**mark, "version": 2, "own": False, "fresh": False}
    assert listed[old["id"]] == {**mark, "version": None, "own": False, "fresh": False}
    relay.db.execute("DELETE FROM services WHERE id = ?", (service["id"],))  # (a service that's gone: no group)
    assert env.get(f"/api/conversations/{clone['id']}").json()["clone"] is None


def test_the_governor_starts_a_reviewer_in_a_tasks_workbox(echo_chat):
    """agent.review: a fresh agent in a managed code task's folder (a clone's too, outside the workspace
    roots), read-only by default, with what to review; not on a chat, not without what to review."""
    from relay.production import folders
    env = echo_chat
    relay = env.app.state.relay
    agentos_on(env)
    user = relay.db.one("SELECT id FROM users")["id"]
    service, versions = fake_service(relay, user)
    folder = folders() / service["id"] / "work" / "cccc3333" / "site"
    folder.mkdir(parents=True)
    governor, run_id, tool = governor_lingering(env, relay)
    try:
        built = env.post("/api/conversations", json={"provider_id": "fake", "mode": "code", "isolate": False,
                                                     "directory": str(env.tmp / "ws" / "project")}, headers=H).json()
        relay.db.execute("UPDATE conversations SET managed = 1, directory = ? WHERE id = ?", (str(folder), built["id"]))
        assert "doesn't see the task's conversation" in tool("agentos_agent_review", id=built["id"][:8], provider="echo")[0]
        text, error = tool("agentos_agent_review", id=built["id"][:8], provider="echo", model="large",
                           message="Review the change: read it, run the tests, report what's wrong")
        assert not error and "Reviewer [" in text and "(readonly mode)" in text, text
        reviewer = relay.db.one("SELECT * FROM conversations WHERE title = ?", (f"Review: {built['title']}",))
        assert reviewer["directory"] == str(folder) and reviewer["managed"] and reviewer["agent_mode"] == "readonly"
        assert reviewer["provider_id"] == "echo" and reviewer["model"] == "large"
        assert [m["content"] for m in relay.db.all("SELECT content FROM messages WHERE conversation_id = ? AND role = 'user'",
                                                   (reviewer["id"],))] == ["Review the change: read it, run the tests, "
                                                                           "report what's wrong"]  # (fresh: only this)
        chat = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
        relay.db.execute("UPDATE conversations SET managed = 1 WHERE id = ?", (chat["id"],))
        assert "is a chat, not a code task" in tool("agentos_agent_review", id=chat["id"][:8], provider="echo",
                                                     message="x")[0]
    finally:
        let_go(env, governor)


def test_services_are_reconciled_after_a_crash(env, monkeypatch):
    """Versions left on their way are failed (or, going back to one, archived again), exactly one
    is live, and boxes of no live version are stopped or removed."""
    import subprocess as sp
    signed_in(env)
    relay = env.app.state.relay
    user = relay.db.one("SELECT id FROM users")["id"]
    service, versions = fake_service(relay, user, "site", live_number=2)
    sid = service["id"]
    relay.db.execute("UPDATE services SET operation = 'promote' WHERE id = ?", (sid,))
    relay.db.execute("UPDATE service_versions SET status = 'live' WHERE id = ?", (versions[1],))  # two live
    relay.db.execute("INSERT INTO service_versions (id, service_id, number, folder, status, created) "
                     "VALUES ('v3', ?, 3, '/nowhere', 'verifying', 0)", (sid,))
    relay.db.execute("INSERT INTO service_versions (id, service_id, number, folder, status, created, archived) "
                     "VALUES ('v4', ?, 4, '/nowhere', 'starting', 0, 5)", (sid,))  # a rollback to it broke off
    calls = []

    def podman(*args, timeout=120):
        calls.append(list(args))
        out = ""
        if args[:1] == ("ps",):
            out = "\n".join([f"relay-prod-{sid[:8]}-1 running {sid}", f"relay-prod-{sid[:8]}-2 running {sid}",
                             f"relay-prod-{sid[:8]}-3 running {sid}", "relay-prod-deadbeef-1 exited gone",
                             f"relay-prod-{sid[:8]}-9 exited {sid}"]) + "\n"
        return sp.CompletedProcess(args, 0, out, "")
    monkeypatch.setattr(relay.production, "_podman", podman)
    fixed = relay.production.reconcile()
    statuses = {v["number"]: v["status"] for v in relay.db.all("SELECT * FROM service_versions WHERE service_id = ?",
                                                                (sid,))}
    assert statuses == {1: "archived", 2: "live", 3: "failed", 4: "archived"}, fixed
    assert relay.db.one("SELECT operation FROM services WHERE id = ?", (sid,))["operation"] is None
    assert ["stop", "-t", "10", f"relay-prod-{sid[:8]}-1"] in calls  # archived: stopped
    assert ["rm", "-f", "-t", "5", f"relay-prod-{sid[:8]}-3"] in calls  # broke off: removed
    assert ["rm", "-f", "-t", "5", f"relay-prod-{sid[:8]}-9"] in calls  # a version it doesn't have: removed
    # Another Relay's service (podman is shared: another RELAY_HOME, a test run): left alone
    assert not any("relay-prod-deadbeef-1" in c for c in calls)
    assert not any(c[:1] == ["stop"] and c[-1].endswith("-2") for c in calls)  # the live one runs on
    assert any(e["kind"] == "reconciled" for e in env.get("/api/agentos").json()["events"])
    again = relay.production.reconcile()  # the database is right now; (this podman still lists the same boxes)
    assert again and all(line.startswith(("stopped", "removed")) for line in again), again


def test_services_are_checked_before_anything_starts(env):
    signed_in(env)
    assert env.get("/api/services").json() == []
    bad = env.post("/api/services", json={"name": "site", "folder": "/etc", "command": "x", "port": 8600}, headers=H)
    assert bad.status_code == 400 and "workspace roots" in bad.json()["error"]
    project = env.tmp / "ws" / "project"
    for name, port, wanted in (("Bad Name", 8600, "a-z, 0-9"), ("site", 80, "1024"), ("site", 8420, "Relay's own")):
        r = env.post("/api/services", json={"name": name, "folder": str(project), "command": "x", "port": port},
                     headers=H)
        assert r.status_code == 400 and wanted in r.json()["error"], r.text
    assert env.get("/api/services/nope/logs").status_code == 404


def test_agents_are_told_about_relays_tools_once_per_session(env):
    signed_in(env)
    db = env.app.state.relay.db
    conv = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    run_turn(env, conv["id"], "first")
    row = db.one("SELECT * FROM conversations WHERE id = ?", (conv["id"],))
    assert row["tools_session"] == row["provider_session"]  # told; the next turn isn't told again
    fork = env.post(f"/api/conversations/{conv['id']}/fork", json={}, headers=H).json()
    forked = db.one("SELECT * FROM conversations WHERE id = ?", (fork["id"],))
    assert forked["tools_session"] is None and forked["tools_token"] is None  # its own script, told anew


# Permission prompts


def test_relay_answers_claude_codes_requests_itself(env):
    """Claude Code's --permission-prompt-tool calls approve_permission; Relay answers, nobody is asked."""
    signed_in(env)
    relay = env.app.state.relay
    conv = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    run_id = env.post(f"/api/conversations/{conv['id']}/messages", json={"text": "slow slow"},
                      headers=H).json()["run_id"]
    token = relay.runs.active[run_id].mcp_token  # the run's own token, as Claude Code gets

    def approve(tool, tool_input):
        result = mcp(env, token, "tools/call", {"name": "approve_permission", "arguments": {
            "tool_name": tool, "input": tool_input, "tool_use_id": "t"}})[1]["result"]
        return json.loads(result["content"][0]["text"])

    assert approve("Bash", {"command": "rm -rf build"}) == {"behavior": "allow",
                                                             "updatedInput": {"command": "rm -rf build"}}
    assert approve("Bash", {"command": "rm -rf /"})["behavior"] == "deny"
    assert not env.get(f"/api/conversations/{conv['id']}").json()["needs_approval"]
    env.post(f"/api/runs/{run_id}/stop", headers=H)
    wait_idle(env, conv["id"])
    stored = [e for e in env.get(f"/api/conversations/{conv['id']}").json()["messages"][-1]["events"]
              if e["type"] == "permission"]
    assert [e["decision"] for e in stored] == ["denied"]  # only refusals are shown


def test_messages_queued_while_an_agent_works_go_next(env):
    signed_in(env)
    conv = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    first = env.post(f"/api/conversations/{conv['id']}/messages", json={"text": "slow one"}, headers=H).json()
    second = env.post(f"/api/conversations/{conv['id']}/queue", json={"text": "two"}, headers=H).json()
    third = env.post(f"/api/conversations/{conv['id']}/queue", json={"text": "three"}, headers=H).json()
    assert "queued" in second and "queued" in third
    queued = env.get(f"/api/conversations/{conv['id']}").json()["queued"]
    assert [q["content"] for q in queued] == ["two", "three"]
    env.delete(f"/api/conversations/{conv['id']}/queue/{third['queued']}", headers=H)
    events_of(env, first["run_id"])
    deadline = time.time() + 10
    while len(env.get(f"/api/conversations/{conv['id']}").json()["messages"]) < 4:
        assert time.time() < deadline, "the queued message wasn't sent"
        time.sleep(0.1)
    wait_idle(env, conv["id"])
    data = env.get(f"/api/conversations/{conv['id']}").json()
    assert [m["content"] for m in data["messages"] if m["role"] == "user"] == ["slow one", "two"]
    assert data["queued"] == []
    # Nothing running: queueing just sends it
    run = env.post(f"/api/conversations/{conv['id']}/queue", json={"text": "now"}, headers=H).json()
    assert "run_id" in run
    events_of(env, run["run_id"])
    wait_idle(env, conv["id"])



def test_cached_input_counts_as_input():
    from relay.usage import tokens
    claude = {"tokens": {"input_tokens": 6, "cache_read_input_tokens": 30000, "cache_creation_input_tokens": 500,
                         "output_tokens": 1977}}
    opencode = {"tokens": {"input": 12, "output": 5, "cache": {"read": 512, "write": 0}}}
    openai = {"tokens": {"prompt_tokens": 900, "completion_tokens": 40}}
    assert tokens(claude) == (30506, 1977) and tokens(opencode) == (524, 5) and tokens(openai) == (900, 40)


def test_usage_overview_tells_api_costs_from_plan_use(env):
    signed_in(env)
    relay = env.app.state.relay
    conv = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    now = time.time()
    runs = [("r1", {"tokens": {"input_tokens": 10, "output_tokens": 5}, "cost": 0.02, "billing": "api"}, "sonnet"),
            ("r2", {"tokens": {"input": 100, "output": 50}, "cost": 0.5, "billing": "subscription"}, None),
            ("r3", {"tokens": {"prompt_tokens": 7, "completion_tokens": 3}, "cost": None, "billing": "free"}, None),
            ("r4", None, None)]
    for run_id, used, model in runs:
        relay.db.execute("INSERT INTO runs (id, conversation_id, status, usage, created, finished, provider_id, model) "
                         "VALUES (?, ?, 'succeeded', ?, ?, ?, 'fake', ?)",
                         (run_id, conv["id"], json.dumps(used) if used else None, now, now, model))
    data = env.get("/api/usage?days=7").json()
    assert data["totals"]["api"] == {"runs": 1, "cost": 0.02, "input": 10, "output": 5}
    assert data["totals"]["subscription"]["cost"] == 0.5 and data["totals"]["subscription"]["input"] == 100
    assert data["totals"]["free"]["output"] == 3 and data["totals"]["unknown"]["runs"] == 1
    assert data["groups"][0]["billing"] == "subscription"  # biggest first
    assert data["per_day"][0]["runs"] == 4 and data["per_day"][0]["api"] == 0.02
    assert env.get("/api/usage?days=x").status_code == 400


def test_a_commit_says_when_git_doesnt_know_who_you_are(env, monkeypatch):
    signed_in(env)
    repo = git_repo(env.tmp / "ws" / "app")
    conv = env.post("/api/conversations", json={"provider_id": "fake", "mode": "code",
                                                "directory": str(repo), "isolate": False}, headers=H).json()
    run_turn(env, conv["id"], "write one")
    subprocess.run(["git", "config", "--unset", "user.email"], cwd=repo, check=True)
    subprocess.run(["git", "config", "--unset", "user.name"], cwd=repo, check=True)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    made = env.post(f"/api/conversations/{conv['id']}/commit", json={"message": "One"}, headers=H).json()
    assert made["as_relay"] is True
    author = subprocess.run(["git", "log", "-1", "--format=%an"], cwd=repo, capture_output=True, text=True).stdout
    assert author.strip() == "Relay"


def test_the_page_hears_while_relays_images_are_built(env, monkeypatch):
    assert env.get("/api/state").json()["building"] == []
    monkeypatch.setitem(containers.BUILDING, "localhost/relay-agent:x", "the agents' container image")
    assert env.get("/api/state").json()["building"] == ["the agents' container image"]


def test_tasks_in_part_of_a_repository_see_and_commit_only_their_folder(env):
    import subprocess
    signed_in(env)
    repo = git_repo(env.tmp / "ws" / "mono")
    (repo / "a").mkdir()
    (repo / "b").mkdir()
    (repo / "b" / "other.txt").write_text("someone else's work\n")
    first = env.post("/api/conversations", json={"provider_id": "fake", "mode": "code",
                                                 "directory": str(repo / "a"), "isolate": False}, headers=H).json()
    run_turn(env, first["id"], "write in a")
    changes = env.get(f"/api/conversations/{first['id']}/changes").json()
    assert [f["path"] for f in changes["files"]] == ["agent.txt"] and changes["scope"] == "a"  # (in a/)
    second = env.post("/api/conversations", json={"provider_id": "fake", "mode": "code",
                                                  "directory": str(repo / "a"), "isolate": False}, headers=H).json()
    run_turn(env, second["id"], "write more in a")
    tasks = env.get("/api/tasks").json()
    assert [t["id"] for t in tasks["review"]] == [second["id"]]  # one card for the same pending changes
    assert next(t for t in tasks["clean"] if t["id"] == first["id"])["shared_changes"] is True

    # Nothing is committed by itself; a commit (when asked for) takes only the task's folder.
    assert git_log(repo) == ["start"]
    env.post(f"/api/conversations/{second['id']}/commit", json={"message": "Work in a"}, headers=H)
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout
    assert status.strip() == "?? b/" and git_log(repo)[0] == "Work in a"
    # An agent at work elsewhere in the repository (b/) doesn't hold that commit up
    (repo / "a" / "more.txt").write_text("more\n")
    elsewhere = env.post("/api/conversations", json={"provider_id": "fake", "mode": "code",
                                                     "directory": str(repo / "b"), "isolate": False}, headers=H).json()
    busy = env.post(f"/api/conversations/{elsewhere['id']}/messages", json={"text": "slow slow"}, headers=H).json()
    try:
        made = env.post(f"/api/conversations/{second['id']}/commit", json={"message": "More in a"}, headers=H)
        assert made.status_code == 200 and git_log(repo)[0] == "More in a", made.text
    finally:
        env.post(f"/api/runs/{busy['run_id']}/stop", headers=H)
        wait_idle(env, elsewhere["id"])



def test_a_merge_in_progress_is_left_alone(env):
    import subprocess
    signed_in(env)
    repo = git_repo(env.tmp / "ws" / "busy")
    conv = env.post("/api/conversations", json={"provider_id": "fake", "mode": "code",
                                                "directory": str(repo), "isolate": False}, headers=H).json()
    (repo / ".git" / "MERGE_HEAD").write_text(subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                                                             capture_output=True, text=True).stdout)
    run_turn(env, conv["id"], "write it")
    assert git_log(repo)[0] == "start" and (repo / ".git" / "MERGE_HEAD").exists()  # yours to finish
    commit = env.post(f"/api/conversations/{conv['id']}/commit", json={"message": "x"}, headers=H)
    assert commit.status_code == 400 and "middle of" in commit.json()["error"]


def test_a_queued_message_can_be_sent_after_a_failed_run(env):
    signed_in(env)
    relay = env.app.state.relay
    conv = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    queued = relay.db.execute("INSERT INTO queued_messages (conversation_id, content, created) VALUES (?, 'later', 0)",
                              (conv["id"],)).lastrowid
    run = env.post(f"/api/conversations/{conv['id']}/queue/{queued}/send", headers=H).json()
    events_of(env, run["run_id"])
    wait_idle(env, conv["id"])
    assert answers(env, conv["id"])[0] == "later" and env.get(f"/api/conversations/{conv['id']}").json()["queued"] == []
    assert env.post(f"/api/conversations/{conv['id']}/queue/{queued}/send", headers=H).status_code == 404


def test_subagents_still_going_when_the_run_ends_are_stored_as_stopped(env):
    signed_in(env)
    relay = env.app.state.relay
    conv = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    run_id = env.post(f"/api/conversations/{conv['id']}/messages", json={"text": "slow"}, headers=H).json()["run_id"]
    token = relay.db.one("SELECT tools_token FROM conversations WHERE id = ?", (conv["id"],))["tools_token"]
    # A subagent still going when the run that started it ends (here: is stopped)
    mcp(env, token, "tools/call", {"name": "delegate", "arguments": {"task": "slow slow", "provider": "fake"}})
    env.post(f"/api/runs/{run_id}/stop", headers=H)
    events_of(env, run_id)
    wait_idle(env, conv["id"])
    cards = [e for e in env.get(f"/api/conversations/{conv['id']}").json()["messages"][-1]["events"]
             if e["type"] == "subagent"]
    assert cards[-1]["status"] == "interrupted"



def test_effort_is_kept_per_conversation_and_passed_on(echo_chat, monkeypatch):
    env = echo_chat
    conv = env.post("/api/conversations", json={"provider_id": "fake", "effort": "high"}, headers=H).json()
    assert conv["effort"] == "high"
    assert env.patch(f"/api/conversations/{conv['id']}", json={"effort": "extreme"}, headers=H).status_code == 400
    run_turn(env, conv["id"], "which effort?")
    assert answers(env, conv["id"])[-1].endswith("effort=high")
    env.patch(f"/api/conversations/{conv['id']}", json={"effort": "default"}, headers=H)
    run_turn(env, conv["id"], "which effort now?")
    assert answers(env, conv["id"])[-1].endswith("effort=")
    assert env.get(f"/api/conversations/{conv['id']}/commands").json() == {"commands": [], "efforts": []}
    assert env.get("/api/providers/echo/efforts").json() == ["low", "medium", "high"]
    # A provider whose box won't start: why, as an answer (not a server error)
    from relay.containers import ContainerError

    def no_box(self, model):
        raise ContainerError("Couldn't start the provider's box: the name is in use")
    monkeypatch.setattr(type(env.app.state.relay.registry.provider("fake")), "efforts", no_box)
    refused = env.get(f"/api/conversations/{conv['id']}/commands")
    assert refused.status_code == 503 and refused.json() == {"error": "Couldn't start the provider's box: the name is in use"}
    fork = env.post(f"/api/conversations/{conv['id']}/fork", json={}, headers=H).json()
    assert fork["effort"] is None


def test_usage_limits_one_row_per_login_kept_a_while_and_else_what_runs_told(env, monkeypatch):
    """Settings → Usage: each provider's limits() (stubs here: no provider is asked for real); one row
    per login; kept for a while (fresh asks anew); one that can't tell now shows its last run's
    windows, and one that fails doesn't stop the rest."""
    from relay import health
    signed_in(env)
    relay = env.app.state.relay
    ids = [t.id for t in relay.registry.templates() if t.enabled]
    asked = []
    window = {"label": "5-hour", "used": .25, "resets_at": 1791114679.0, "left": None}
    answers = {"claude-code": None, "fake": {"account": "chatgpt:acc-1", "plan": "Plus", "windows": [window],
                                             "source": "provider", "checked": 1.0}}

    class Stub:
        def __init__(self, template_id):
            self.id = template_id

        def limits(self):
            asked.append(self.id)
            if self.id == "broken":
                raise RuntimeError("no")
            return answers.get(self.id, answers["fake"] if self.id.startswith("opencode") else None)
    monkeypatch.setattr(relay.registry, "provider", Stub)
    health.record(relay.db, "claude-code", "opus", True, usage={"status": "allowed", "windows": {
        "five_hour": {"utilization": .5, "resets_at": 1791063000}}})
    rows = env.get("/api/usage/limits").json()
    by_name = {r["provider_id"]: r for r in rows}
    assert by_name["claude-code"]["source"] == "last run" and by_name["claude-code"]["windows"][0]["used"] == .5
    chatgpt = [r for r in rows if r["plan"] == "Plus"]
    assert len(chatgpt) == 1 and chatgpt[0]["windows"] == [window]  # (one login, however many providers)
    sharing = [i for i in ids if i == "fake" or i.startswith("opencode")]
    assert len(chatgpt[0]["also"]) == len(sharing) - 1
    count = len(asked)
    env.get("/api/usage/limits")
    assert len(asked) == count  # (kept a while)
    env.get("/api/usage/limits?fresh=1")
    assert len(asked) == 2 * count
    relay.registry.add({"id": "broken", "name": "Broken", "kind": "command", "options": {"command": ["true"]}})
    rows = env.get("/api/usage/limits?fresh=1").json()
    assert {r["provider_id"]: r for r in rows}["broken"]["error"] == "couldn't ask it (RuntimeError)"
    assert any(r["plan"] == "Plus" for r in rows)


def test_system_tasks_come_with_agentos_and_stay_relays(echo_chat):
    """Relay's own tasks: added when AgentOS goes on (once), carried out by the Governor but not changed
    by it, changed by the user and reset to their default, never deleted (turned off instead)."""
    from relay.schedules import SYSTEM_TASKS
    env = echo_chat
    relay = env.app.state.relay
    user = relay.db.one("SELECT id FROM users")["id"]
    assert [t for t in env.get("/api/schedules").json() if t["kind"] == "system"] == []  # (AgentOS off: none)
    agentos_on(env)
    env.put("/api/agentos", json={"enabled": True}, headers=H)  # (on again: not twice)
    assert relay.schedules.ensure_system(user) == []
    tasks = {t["system"]: t for t in env.get("/api/schedules").json() if t["kind"] == "system"}
    assert sorted(tasks) == sorted(SYSTEM_TASKS) and all(t["enabled"] and t["governed"] for t in tasks.values())
    check = tasks["services-check"]
    assert check["name"] == "Services: weekly check" and not check["changed"] and check["next_run"]
    mine = env.post("/api/schedules", json={"name": "Mine", "prompt": "mine", "when": {"kind": "daily", "time": "08:00"},
                                            "governed": True}, headers=H).json()
    assert mine["kind"] == "manual"
    # The user changes it; Reset gives it back
    changed = env.put(f"/api/schedules/{check['id']}", json={"name": "Services, my way", "enabled": False},
                      headers=H).json()
    assert changed["changed"] and changed["kind"] == "system"
    reset = env.post(f"/api/schedules/{check['id']}/reset", headers=H).json()
    assert reset["name"] == "Services: weekly check" and reset["enabled"] and not reset["changed"]
    assert env.post(f"/api/schedules/{mine['id']}/reset", headers=H).status_code == 400
    refused = env.delete(f"/api/schedules/{check['id']}", headers=H)
    assert refused.status_code == 400 and "turn it off instead" in refused.json()["error"]
    # The Governor carries it out, but doesn't change it
    governor, run_id, tool = governor_lingering(env, relay)
    try:
        assert "Relay's own system task" in tool("agentos_task_list")[0]
        assert tool("agentos_task_update", id=check["id"][:8], name="mine now")[1]  # (an error: not its own)
        assert tool("agentos_task_delete", id=check["id"][:8])[1]
        made = tool("agentos_task_create", name="A check of mine", message="look", when="daily 09:00")[0]
        assert made.startswith("Made task")
        kinds = {t["name"]: t["kind"] for t in env.get("/api/schedules").json()}
        assert kinds["A check of mine"] == "governor" and kinds["Mine"] == "manual"
    finally:
        let_go(env, governor)


def test_the_governor_tidies_relays_memory_up_weekly(echo_chat):
    """A system task: the Governor reviews all of Relay's memory with what Relay found, merges and forgets an
    agent's memories (never the user's own), saying why. A week with nothing new and nothing of an agent's
    gone stale Relay notes itself, without waking it."""
    from relay import memory
    from relay.agentos import IRREVERSIBLE, AgentOSError
    from relay.schedules import SYSTEM_TASKS
    env = echo_chat
    relay = env.app.state.relay
    agentos_on(env)
    user = relay.db.one("SELECT id FROM users")["id"]
    task = next(t for t in relay.schedules.all(user) if t.get("system") == "memory-tidy")
    assert task["governed"] and task["name"] == SYSTEM_TASKS["memory-tidy"]["name"] and task["when"]["days"] == [6]
    assert "memory.forget" in IRREVERSIBLE  # (ask first, if the user says so)
    from relay.delegation import AGENTOS_TOOLS
    assert {"agentos_memory_review", "agentos_memory_change", "agentos_memory_forget"} <= {t["name"] for t in AGENTOS_TOOLS}
    mine = memory.add(relay.db, user, "The user prefers answers in Dutch")
    said = memory.add(relay.db, user, "User prefers answers in Dutch.", by="Claude Code · opus")
    gone = memory.add(relay.db, user, "Runs on port 8501", str(env.tmp / "gone"), by="opencode")
    old = memory.add(relay.db, user, "Its tests run with make check", str(env.tmp / "ws" / "project"), by="opencode")
    relay.db.execute("UPDATE memories SET updated = ? WHERE id = ?", (time.time() - 200 * 86400, old["id"]))
    run = SimpleNamespace(id="tidy-run")

    def capability(which, **arguments):
        return env.portal.call(getattr(relay.agentos, "_" + which.replace(".", "_")), run, user, arguments)
    text = capability("memory.review")
    assert "Likely the same" in text and f"[{mine['id'][:6]}] (General, other, the user's own" in text
    assert f"[{said['id'][:6]}] (General, other, by Claude Code · opus" in text
    assert f"Their place is gone (a folder or a service deleted):\n- [{gone['id'][:6]}]" in text
    assert "No agent got these for 90 days or more" in text and f"[{old['id'][:6]}]" in text and "unused for 200 days" in text
    # The user's own it leaves; an agent's it changes and forgets, saying why (in the activity)
    with pytest.raises(AgentOSError, match="the user's own"):
        capability("memory.forget", id=mine["id"][:6], why="said twice")
    with pytest.raises(AgentOSError, match="Say why"):
        capability("memory.forget", id=said["id"][:6])
    assert capability("memory.forget", id=said["id"][:6], why="the user's own says it") == \
        f"Forgot [{said['id'][:6]}]: User prefers answers in Dutch."
    assert capability("memory.change", id=old["id"][:6], category="setup", message="Its tests: make check") == \
        f"Changed [{old['id'][:6]}] (setup): Its tests: make check"
    capability("memory.forget", id=gone["id"][:6], why="its folder is gone")
    forgotten = [e for e in relay.agentos.events(user) if e["kind"] == "memory.forgotten"]
    assert [(e["actor"], e["detail"]["place"], e["detail"]["why"]) for e in forgotten] == [
        ("governor", str(env.tmp / "gone"), "its folder is gone"), ("governor", "General", "the user's own says it")]
    # The weekly task: the first time, and with something new, the Governor looks; else Relay notes it
    tidied = relay.scheduler._tidied_already
    now = time.time() + 1
    assert env.portal.call(tidied, task, now) is False  # (never looked yet)
    relay.db.execute("UPDATE schedules SET last_run = ? WHERE id = ?", (now, task["id"]))
    task = relay.schedules.get(user, task["id"])
    assert env.portal.call(tidied, task, now + 7 * 86400) is True
    done = relay.schedules.get(user, task["id"])
    assert done["outcome_by"] == "relay" and done["outcome_note"].startswith("Nothing new in the memory")
    relay.db.execute("UPDATE memories SET used = NULL, updated = ? WHERE id = ?", (now - 100 * 86400, old["id"]))
    assert env.portal.call(tidied, done, now + 14 * 86400) is False  # (an agent's, unused for long)


def test_timed_route(env):
    signed_in(env)
    conv = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    set_ = env.post(f"/api/conversations/{conv['id']}/timed", json={"text": "1h30 Build it and test it"}, headers=H)
    assert set_.status_code == 200 and set_.json()["task"] == "Build it and test it"
    timer = set_.json()["timed"]
    assert round((timer["max"] - timer["started"]) / 60) == 90 and timer["min"] is None and timer["ended"] is None
    assert env.get(f"/api/conversations/{conv['id']}").json()["timed"] == timer
    assert "Say for how long" in env.post(f"/api/conversations/{conv['id']}/timed", json={"text": "soon"},
                                          headers=H).json()["error"]
    off = env.post(f"/api/conversations/{conv['id']}/timed", json={"off": True}, headers=H).json()
    assert off["timed"]["ended"] == "you stopped the timer"
    assert env.post(f"/api/conversations/{conv['id']}/timed", json={"off": True}, headers=H).json()["timed"] is None


def test_timed_keeps_the_agent_at_it_and_stops_it_at_max(env, monkeypatch):
    """What Relay does when a run ends (RunManager._timed_after) and at MAX (Scheduler._timed_over),
    with start and stop caught: the agent isn't run here, so a hard part can't go on for ever."""
    from relay.runs import ActiveRun
    signed_in(env)
    relay = env.app.state.relay
    runs = relay.runs
    conv = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    started, stopped = [], []
    monkeypatch.setattr(runs, "start", lambda row, prompt, *args, relay_action=None, **kwargs:
                        started.append((prompt, relay_action)) or "run-next")
    monkeypatch.setattr(runs, "stop", lambda run_id, why=None: stopped.append((run_id, why)) or True)
    now = time.time()

    def timer(**changes):
        relay.db.execute("UPDATE conversations SET timed = ? WHERE id = ?", (json.dumps(
            {"started": now, "min": now + 600, "optimal": now + 900, "max": now + 1200, "asked": False, "ended": None,
             **changes}), conv["id"]))

    def ended(status):
        runs._timed_after(ActiveRun("r", conv["id"]), status)
        found = relay.db.one("SELECT timed FROM conversations WHERE id = ?", (conv["id"],))["timed"]
        return json.loads(found) if found else None

    timer()  # before MIN: go on, every time
    ended("succeeded")
    ended("succeeded")
    assert [p for p, _ in started] == ["Please continue. Per the timed request, you are free in 10 minutes. "
                                       "Keep working until then."] * 2 and started[0][1] == {"kind": "timed"}
    started.clear()
    timer(min=now - 60)  # before OPTIMAL: asked once
    assert ended("succeeded")["asked"] and started[0][0].startswith("You may stop now")
    ended("succeeded")
    assert len(started) == 1
    started.clear()
    timer(min=now - 120, optimal=now - 60)  # after OPTIMAL: it may stop
    ended("succeeded")
    timer()
    ended("failed")  # (a failure goes as it does: the fallback loop, or the user)
    active = runs.active_run_for
    monkeypatch.setattr(runs, "active_run_for", lambda conversation_id: "queued-one")
    ended("succeeded")  # (a queued message went on already)
    monkeypatch.setattr(runs, "active_run_for", active)  # (not monkeypatch.undo(): that undoes the env's too)
    assert started == []
    runs.shutting_down = True
    assert ended("interrupted")["ended"] is None  # (Relay exiting isn't the user stopping it)
    runs.shutting_down = False
    assert ended("interrupted")["ended"] == "you stopped the agent"

    # MAX: the agent is stopped, a wait for a reset is dropped, and nothing goes on after it
    timer(max=now - 1)
    relay.db.execute("UPDATE conversations SET resume_at = ? WHERE id = ?", (now - 5, conv["id"]))
    monkeypatch.setattr(runs, "active_run_for", lambda conversation_id: "run-going")
    relay.scheduler._timed_over(now)
    row = relay.db.one("SELECT timed, resume_at FROM conversations WHERE id = ?", (conv["id"],))
    assert stopped == [("run-going", "its time (/timed) was up")] and json.loads(row["timed"])["ended"] == "max" and row["resume_at"] is None
    monkeypatch.setattr(runs, "active_run_for", lambda conversation_id: None)
    runs._go_on(conv["id"], "Please continue where it left off")  # (a fallback agent: not after MAX)
    relay.db.execute("UPDATE conversations SET resume_at = ? WHERE id = ?", (now - 5, conv["id"]))
    relay.scheduler._resume(now)  # (nor a resume)
    ended("succeeded")
    assert started == []


def test_timed_goes_on_after_a_restart_cut_it_off(env, monkeypatch):
    """A turn the restart of Relay cut off before MIN: the agent goes on at the scheduler's first tick.
    Not after the user's own Stop (that ends the timer), not past MIN, not while it waits for a reset."""
    signed_in(env)
    relay = env.app.state.relay
    runs = relay.runs
    conv = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    now = time.time()
    relay.db.execute("UPDATE conversations SET timed = ? WHERE id = ?", (json.dumps(
        {"started": now, "min": now + 1800, "optimal": now + 1800, "max": now + 3600, "asked": False, "ended": None}),
        conv["id"]))
    run_id = env.post(f"/api/conversations/{conv['id']}/messages", json={"text": "linger"}, headers=H).json()["run_id"]
    runs.shutting_down = True  # (as when Relay exits)
    runs.stop(run_id)
    (pathlib.Path(conv["directory"]) / "done-lingering").write_text("")
    wait_idle(env, conv["id"])
    runs.shutting_down = False
    started = []
    monkeypatch.setattr(runs, "start", lambda row, prompt, *args, relay_action=None, **kwargs:
                        started.append((row["id"], prompt, relay_action)) or "run-next")
    assert runs.resume_timed() == [conv["id"]]
    assert started[0][1].startswith("Please continue. Per the timed request") and started[0][2] == {"kind": "timed"}
    relay.db.execute("UPDATE conversations SET resume_at = ? WHERE id = ?", (now + 60, conv["id"]))
    assert runs.resume_timed() == []  # (it waits for a usage limit's reset)
    relay.db.execute("UPDATE conversations SET resume_at = NULL, timed = json_set(timed, '$.min', ?) WHERE id = ?",
                     (now - 1, conv["id"]))
    assert runs.resume_timed() == []  # (past MIN: it may stop)


def test_timed_asks_once_through_a_real_run(env):
    """The wiring: a run that ends in the soft part starts the question as Relay's next turn, and that
    one ending starts nothing more."""
    signed_in(env)
    relay = env.app.state.relay
    conv = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    now = time.time()
    relay.db.execute("UPDATE conversations SET timed = ? WHERE id = ?", (json.dumps(
        {"started": now, "min": None, "optimal": now + 1800, "max": now + 3600, "asked": False, "ended": None}),
        conv["id"]))
    run_turn(env, conv["id"], "Start the work")
    deadline = time.time() + 10  # (Relay's turn starts just after the first one ends)
    while time.time() < deadline:
        users = [m for m in env.get(f"/api/conversations/{conv['id']}").json()["messages"] if m["role"] == "user"]
        if len(users) == 2:
            break
        time.sleep(.05)
    wait_idle(env, conv["id"])
    time.sleep(.3)  # (and nothing after it)
    messages = env.get(f"/api/conversations/{conv['id']}").json()["messages"]
    users = [m for m in messages if m["role"] == "user"]
    assert [u["content"][:16] for u in users] == ["Start the work", "You may stop now"]
    assert users[1]["relay_action"]["kind"] == "timed"
    assert env.get(f"/api/conversations/{conv['id']}").json()["timed"]["asked"]


def test_skills_in_the_api(env):
    signed_in(env)
    listed = env.get("/api/skills").json()
    assert listed["folder"].endswith("skills") and any(s["name"] == "relay-subagents" for s in listed["skills"])
    made = env.post("/api/skills", json={"name": "greet", "description": "Greets. Use to greet."}, headers=H).json()
    assert made["name"] == "greet"
    detail = env.get("/api/skills/greet").json()
    assert detail["files"] == ["SKILL.md"]
    content = env.get("/api/skills/greet/file?path=SKILL.md").json()["content"]
    env.put("/api/skills/greet/file?path=SKILL.md", json={"content": content.replace("Greets.", "Says hi.")}, headers=H)
    assert env.get("/api/skills/greet").json()["description"].startswith("Says hi.")
    assert env.put("/api/skills/relay-subagents/file?path=SKILL.md", json={"content": "x"}, headers=H).status_code == 400
    assert env.delete("/api/skills/greet", headers=H).json() == {"ok": True}
    assert env.get("/api/skills/greet").status_code == 400


# Which models work


def test_a_provider_is_tried_before_its_added(echo_chat, monkeypatch):
    """Add provider's next step: a template said wrong on the first page, what it names made, and a test message from
    it while it isn't added. Nothing of that stays: not in the providers, not in the models' health, and its box goes,
    unless a provider that's added has that box."""
    env = echo_chat
    relay = env.app.state.relay
    kinds = {k["kind"]: k for k in env.get("/api/provider-kinds").json()}
    assert kinds["claude-code"]["sign_in"] == "claude auth login" and kinds["copilot"]["sign_in"] == "copilot login"
    assert kinds["command"]["sign_in"] is None

    echo = relay.registry.get("echo")
    works = {"id": "new-one", "name": "New", "kind": echo.kind, "options": echo.options}
    tested = env.post(f"/api/provider-kinds/{echo.kind}/test", json={"template": works, "model": "m"}, headers=H).json()
    assert tested["ok"] and "Answered in" in tested["message"] and not tested["login_lost"]
    fails = {"id": "new-two", "name": "Two", "kind": "command", "options": {
        "command": [sys.executable, "-c", "import sys; sys.exit('Not logged in')"]}}
    tested = env.post("/api/provider-kinds/command/test", json={"template": fails}, headers=H).json()
    assert not tested["ok"] and "Not logged in" in tested["message"] and tested["login_lost"]
    assert not {"new-one", "new-two"} & {t.id for t in relay.registry.templates()}
    assert not [key for key in env.get("/api/models/status").json() if key.startswith("new-")]
    # (of the page's kind, and a template at all)
    for kind, template in (("claude-code", works), ("command", {"id": "not an id"}), ("command", None)):
        refused = env.post(f"/api/provider-kinds/{kind}/test", json={"template": template}, headers=H)
        assert refused.status_code == 400, refused.text

    draft = lambda kind, template: env.post(f"/api/provider-kinds/{kind}/draft", json={"template": template}, headers=H)  # noqa: E731
    made = []
    monkeypatch.setattr(KINDS["command"], "prepare_new", lambda self: made.append(self.template.id))
    assert draft("command", fails).json() == {"shell": False, "sign_in": None, "shell_hint": None}
    assert made == ["new-two"]
    assert not {"new-two"} & {t.id for t in relay.registry.templates()}  # (it isn't added yet)
    claude = {"id": "new-claude", "name": "C", "kind": "claude-code", "options": {}}
    assert draft("claude-code", claude).json() == {"shell": True, "sign_in": "claude auth login", "shell_hint": None}
    assert "/connect" in draft("opencode", {"id": "new-oc", "name": "O", "kind": "opencode", "options": {}}).json()["shell_hint"]
    made.clear()
    for kind, template, why in (("command", {**fails, "id": "echo"}, "already exists"),
                                ("claude-code", fails, "not a template of a new"), ("command", {"id": "not an id"}, "id"),
                                ("command", None, "object"), ("command", {**fails, "home": "/tmp"}, "not a template")):
        refused = draft(kind, template)
        assert refused.status_code == 400 and why in refused.json()["error"], refused.text
    assert not made  # (nothing made for one that's said wrong)

    removed = []
    monkeypatch.setattr(relay, "boxes", SimpleNamespace(remove=removed.append, name=lambda key: f"relay-box-{key}"))
    monkeypatch.setattr(KINDS["command"], "box_key", lambda self: self.template.id)
    env.post("/api/provider-kinds/command/test", json={"template": fails}, headers=H)
    assert removed == ["new-two"]
    relay.registry.add(fails)
    env.post("/api/provider-kinds/command/test", json={"template": fails}, headers=H)
    assert removed == ["new-two"]  # (that provider's box now)
    # (one box of several providers, as opencode's profiles have: a new one's test leaves it)
    monkeypatch.setattr(KINDS["command"], "box_key", lambda self: "shared")
    env.post("/api/provider-kinds/command/test", json={"template": {**fails, "id": "new-three"}}, headers=H)
    assert removed == ["new-two"]


def test_runs_and_tests_show_which_models_work(echo_chat, monkeypatch):
    env = echo_chat
    relay = env.app.state.relay
    conv = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    run_turn(env, conv["id"], "hello")
    status = env.get("/api/models/status").json()
    assert status["fake/"]["ok"] and status["fake/"]["source"] == "run"

    placed, place = [], relay.runs.place_check
    monkeypatch.setattr(relay.runs, "place_check", lambda provider, context: placed.append(
        (provider.template.id, context.directory.name)) or place(provider, context))
    tested = env.post("/api/providers/echo/test", json={"model": "m"}, headers=H).json()
    assert tested["ok"] and tested["source"] == "test" and "Answered in" in tested["message"]
    assert placed[0][0] == "echo" and placed[0][1].startswith("test-")  # (where a run would be)
    models = env.get("/api/providers/echo/models").json()["models"]
    assert models[0]["id"] == "m" and models[0]["status"]["ok"]

    env.post("/api/providers", json={"id": "broken", "name": "Broken", "kind": "command", "options": {
        "command": [sys.executable, "-c", "import sys; sys.exit('usage limit reached')"]}}, headers=H)
    tested = env.post("/api/providers/broken/test", json={}, headers=H).json()
    assert not tested["ok"] and "usage limit reached" in tested["message"]
    broken = env.post("/api/conversations", json={"provider_id": "broken"}, headers=H).json()
    run_turn(env, broken["id"], "hi")
    status = env.get("/api/models/status").json()["broken/"]
    assert not status["ok"] and status["source"] == "run"
    listed = {p["id"]: p for p in env.get("/api/providers").json()["providers"]}
    assert listed["broken"]["last_status"]["ok"] is False and listed["echo"]["last_status"]["ok"]


def test_subagents_that_are_not_allowed_are_asked_for(echo_chat, monkeypatch):
    import threading

    from relay import policy
    env = echo_chat
    relay = env.app.state.relay
    sleeper = ("import json, time\nprint(json.dumps({'type': 'session', 'session': 's1'}), flush=True)\n"
               "time.sleep(60)\nprint(json.dumps({'type': 'done', 'status': 'succeeded'}), flush=True)\n")
    env.post("/api/providers", json={"id": "sleeper", "name": "Sleeper", "kind": "command",
                                     "options": {"command": [sys.executable, "-c", sleeper]}}, headers=H)
    env.post("/api/providers", json={"id": "paid", "name": "Paid", "kind": "command",
                                     "options": {"command": [sys.executable, "-c", "print('{}')"]}}, headers=H)
    assert env.get("/api/subagents").json() == policy.DEFAULT
    conv = env.post("/api/conversations", json={"provider_id": "sleeper"}, headers=H).json()
    run_id = env.post(f"/api/conversations/{conv['id']}/messages", json={"text": "go"}, headers=H).json()["run_id"]
    token = relay.db.one("SELECT tools_token FROM conversations WHERE id = ?", (conv["id"],))["tools_token"]
    for _ in range(100):
        if relay.delegation.run_for(token):
            break
        time.sleep(0.05)

    def tool(name, **arguments):
        return mcp(env, token, "tools/call", {"name": name, "arguments": arguments})[1]["result"]

    listed = tool("agents")["content"][0]["text"]
    assert listed.startswith("Allowed without asking the user: provider sleeper, its default model (the same as yours)")
    assert "provider echo, any model (free)" in listed

    def delegate_answering(choice):
        """Ask for a subagent on Paid, and answer the question with `choice`."""
        result = {}
        thread = threading.Thread(target=lambda: result.update(tool("delegate", task="sum it up", provider="paid")))
        thread.start()
        asked = None
        for _ in range(200):
            active = relay.runs.active.get(run_id)
            asked = next(iter(active.pending.values()), None) if active else None
            if asked or not thread.is_alive():
                break
            time.sleep(0.02)
        if choice and asked:
            event = asked["event"]
            assert event["action"] == "Subagent on Paid" and "Cost: not known" in event["resources"][-1]
            assert [o["id"] for o in event["options"]] == ["once", "always", "alt", "denied"]
            r = env.post(f"/api/runs/{run_id}/permission", json={"id": event["id"], "decision": choice}, headers=H)
            assert r.status_code == 200, r.text
        thread.join(10)
        return result["content"][0]["text"], result["isError"]

    text, error = delegate_answering("denied")
    assert error and text.startswith("The user didn't allow a subagent on Paid.")
    assert "provider echo, any model (free)" in text
    text, error = delegate_answering("alt")  # an allowed one other than the caller itself, offered instead
    assert not error and text.startswith("The user didn't allow Paid; they chose Echo instead")
    text, error = delegate_answering("always")
    assert not error and "Paid" in text
    assert env.get("/api/subagents").json()["allowed"] == [{"provider": "paid", "model": ""}]
    text, error = delegate_answering(None)  # allowed now: not asked
    assert not error and "Paid" in text

    env.put("/api/subagents", json={**policy.DEFAULT, "allowed": [], "same": False, "free": False,
                                    "others": "refuse"}, headers=H)
    refused = tool("delegate", task="x", provider="echo")
    text = refused["content"][0]["text"]
    assert refused["isError"] and "isn't allowed" in text and "No subagents are allowed without asking" in text
    env.put("/api/subagents", json={**policy.DEFAULT, "same": False, "free": False}, headers=H)
    from relay import tunables
    monkeypatch.setitem(tunables._values, "subagents.ask_timeout", 0.3)
    text, error = delegate_answering(None)
    assert error and "(no answer in time)" in text
    env.put("/api/subagents", json={**policy.DEFAULT, "same": False}, headers=H)  # with a free one to go to
    text, error = delegate_answering(None)
    assert not error and text.startswith("The user didn't answer about Paid in time, so this subagent runs on Echo")
    assert env.put("/api/subagents", json={"others": "sometimes"}, headers=H).status_code == 400

    env.post(f"/api/runs/{run_id}/stop", headers=H)
    wait_idle(env, conv["id"])
    notes = [e for e in env.get(f"/api/conversations/{conv['id']}").json()["messages"][-1]["events"]
             if e["type"] == "permission" and e.get("decision")]
    assert [n["decision"] for n in notes] == ["denied", "alt", "always", "timeout", "timeout"]
    assert notes[1]["outcome"] == "Echo used instead"



def _parent_with_tools(env, provider_id):
    """A run of `provider_id` that keeps going, and a way to call Relay's tools as it."""
    relay = env.app.state.relay
    conv = env.post("/api/conversations", json={"provider_id": provider_id}, headers=H).json()
    run_id = env.post(f"/api/conversations/{conv['id']}/messages", json={"text": "go"}, headers=H).json()["run_id"]
    token = relay.db.one("SELECT tools_token FROM conversations WHERE id = ?", (conv["id"],))["tools_token"]
    for _ in range(100):
        if relay.delegation.run_for(token):
            break
        time.sleep(0.05)

    def tool(name, **arguments):
        result = mcp(env, token, "tools/call", {"name": name, "arguments": arguments})[1]["result"]
        return result["content"][0]["text"], result["isError"]
    return conv, run_id, tool


SLEEPER = ("import json, time\nprint(json.dumps({'type': 'session', 'session': 's1'}), flush=True)\n"
           "time.sleep(60)\nprint(json.dumps({'type': 'done', 'status': 'succeeded'}), flush=True)\n")


def failing(after):
    return (f"import json, time\ntime.sleep({after})\n"
            "print(json.dumps({'type': 'error', 'message': 'Endpoint is unavailable'}), flush=True)\n"
            "print(json.dumps({'type': 'done', 'status': 'failed'}), flush=True)\n")


def test_a_scheduled_prompt_says_how_its_agent_did(env):
    """Found by a simulated user: a task whose agent failed said "done" (having run was taken for it). Relay follows
    its conversation until it's quiet (a fallback's go-on included), and its last answer's end is the outcome."""
    signed_in(env)
    relay = env.app.state.relay
    user = relay.db.one("SELECT id FROM users")["id"]
    env.post("/api/providers", json={"id": "broken", "name": "Broken", "kind": "command",
                                     "options": {"command": [sys.executable, "-c", failing(0)]}}, headers=H)
    for provider, expected in (("fake", "done"), ("broken", "failed")):
        task = env.post("/api/schedules", json={"name": f"Check {provider}", "prompt": "hello", "provider_id": provider,
                                                "when": "daily 08:00", "fallback_loop": "off"}, headers=H).json()
        env.portal.call(relay.scheduler.run_schedule, relay.schedules.get(user, task["id"]))
        deadline = time.monotonic() + 30
        while not relay.schedules.get(user, task["id"]).get("outcome") and time.monotonic() < deadline:
            time.sleep(0.2)
        found = relay.schedules.get(user, task["id"])
        assert (found["outcome"], found["outcome_by"]) == (expected, "relay"), found
        assert found["last_error"] == (None if expected == "done" else "Endpoint is unavailable"), found
    # A script's (made over the API, with no provider): how it ended, how long, and its output's last line, why
    relay.scripts.create("private/broken.py", "print('working', flush=True)\nraise SystemExit('no network')\n")
    task = env.post("/api/schedules", json={"name": "Broken script", "script": "private/broken.py",
                                            "when": "daily 08:00"}, headers=H).json()
    env.portal.call(relay.scheduler.run_schedule, relay.schedules.get(user, task["id"]))
    deadline = time.monotonic() + 30
    while not relay.schedules.get(user, task["id"]).get("outcome") and time.monotonic() < deadline:
        time.sleep(0.2)
    error = relay.schedules.get(user, task["id"])["last_error"]
    assert error.startswith("exit 1 after ") and error.endswith(" s: no network"), error
    # Saved, a task's script must be there and its arguments must parse: not found out at its time
    for wrong, said in (({"script": "private/gone.py"}, "There's no script private/gone.py"),
                        ({"script": "private/broken.py", "script_args": "'open"}, "Its arguments don't parse")):
        refused = env.post("/api/schedules", json={"name": "Wrong", "when": "daily 08:00", **wrong}, headers=H)
        assert refused.status_code == 400 and said in refused.json()["error"], refused.text
        refused = env.put(f"/api/schedules/{task['id']}", json=wrong, headers=H)
        assert refused.status_code == 400 and said in refused.json()["error"], refused.text
    # (turned off, it's left as it is: one whose script went can still be switched off)
    relay.scripts.delete("private/broken.py")
    assert env.put(f"/api/schedules/{task['id']}", json={"enabled": False}, headers=H).status_code == 200
    # While its script runs, the task says so (the page: "working", not "ran"); after, it doesn't
    gate = relay.scripts.folder() / "private" / "go"
    relay.scripts.create("private/slow.py", "import pathlib, sys, time\n"
                                            "while not pathlib.Path(sys.argv[1]).exists():\n    time.sleep(0.05)\n")
    task = env.post("/api/schedules", json={"name": "Slow script", "script": "slow.py", "script_args": str(gate),
                                            "when": "daily 08:00"}, headers=H).json()
    env.portal.call(relay.scheduler.run_schedule, relay.schedules.get(user, task["id"]))
    assert env.get(f"/api/schedules/{task['id']}", headers=H).json()["running"] is True
    gate.touch()
    deadline = time.monotonic() + 30
    while not relay.schedules.get(user, task["id"]).get("outcome") and time.monotonic() < deadline:
        time.sleep(0.2)
    found = next(t for t in env.get("/api/schedules", headers=H).json() if t["id"] == task["id"])
    assert (found["running"], found["outcome"]) == (False, "done"), found


def test_agents_hear_at_once_when_a_subagent_fails(echo_chat, monkeypatch):
    from relay import delegation
    env = echo_chat
    monkeypatch.setattr(delegation, "EARLY", 4)  # time for its second try too
    for name, script in (("sleeper", SLEEPER), ("broken", failing(0)), ("late", failing(2.5))):
        env.post("/api/providers", json={"id": name, "name": name.title(), "kind": "command",
                                         "options": {"command": [sys.executable, "-c", script]}}, headers=H)
    env.put("/api/subagents", json={"allowed": [{"provider": "broken", "model": "*"},
                                                {"provider": "late", "model": "*"}]}, headers=H)
    conv, run_id, tool = _parent_with_tools(env, "sleeper")
    text, error = tool("delegate", task="write chapter 1", provider="broken")
    assert not error and "failed right away" in text and "Endpoint is unavailable" in text
    assert "delegate it again with another model" in text
    text, error = tool("delegate", task="write chapter 2", provider="late")
    assert text.startswith("Started subagent")
    time.sleep(3)
    text, error = tool("agents")  # any of Relay's tools brings the news
    assert "Meanwhile, subagent" in text and "Endpoint is unavailable" in text
    assert "failed on their last run, better not use: its default model (Endpoint is unavailable)" in text
    assert tool("wait")[0] == "No subagents to wait for."  # told already
    # A name it doesn't offer (mistyped once, say) isn't one to warn of for good
    from relay import health
    health.record(env.app.state.relay.db, "broken", "broken/typo", False, "No such model")
    assert "No such model" not in tool("agents")[0]
    env.post(f"/api/runs/{run_id}/stop", headers=H)
    wait_idle(env, conv["id"])



ECHOING = ("import json, sys\nprompt = sys.stdin.read()\n"
           "print(json.dumps({'type': 'text', 'delta': 'done: ' + prompt}), flush=True)\n"
           "print(json.dumps({'type': 'done', 'status': 'succeeded'}), flush=True)\n")


def test_a_free_subagents_task_goes_on_with_another_free_model(echo_chat, monkeypatch):
    """A free subagent's model fails (unavailable, not in this country): the next free model takes
    its task over at once, without the caller having to; not a paid one, not one that failed lately,
    at most FREE_FALLBACKS times; the caller and the card say which took over."""
    from opencode_manager.bridge.billing import FREE
    from relay import delegation
    env = echo_chat
    relay = env.app.state.relay
    monkeypatch.setattr(delegation, "EARLY", 0.5)
    for name, script in (("sleeper", SLEEPER), ("broken", failing(0)), ("abroad", failing(0)), ("good", ECHOING),
                         ("paid", ECHOING)):
        env.post("/api/providers", json={"id": name, "name": name.title(), "kind": "command",
                                         "options": {"command": [sys.executable, "-c", script]}}, headers=H)
    free = {"broken", "abroad", "good"}
    monkeypatch.setattr(delegation.policy, "billing", lambda db, registry, provider, model: FREE if provider in free else "api")
    monkeypatch.setattr(delegation.policy, "allowed", lambda db, registry, rules, caller: [
        ("paid", "", "on the user's list"),
        *((name, "", "free") for name in ("broken", "abroad", "good") if name in free)])
    conv, run_id, tool = _parent_with_tools(env, "sleeper")
    try:
        text, error = tool("delegate", task="write chapter 1", provider="broken", wait=True)
        assert not error and "Good: finished. It took over from Broken, Abroad, which failed." in text, text
        assert "write chapter 1" in text  # (the task, handed over)
        cards = [e for e in relay.runs.active[run_id].events if e.get("type") == "subagent"]
        assert cards[-1]["provider"] == "good" and cards[-1]["fell_back"] == "Abroad"
        # A model that failed lately isn't gone to again; with no free one left, the caller hears it failed
        free.discard("good")
        text, error = tool("delegate", task="write chapter 2", provider="abroad", wait=True)
        assert "Abroad: failed." in text and "took over" not in text, text
        # A paid subagent isn't moved to another model
        text, error = tool("delegate", task="write chapter 3", provider="paid", wait=True)
        assert "Paid: finished." in text and "took over" not in text
    finally:
        env.post(f"/api/runs/{run_id}/stop", headers=H)
        wait_idle(env, conv["id"])


FLAKY = """import json, pathlib, sys
state = pathlib.Path(sys.argv[1])
prompt = sys.stdin.read()
if not state.exists():
    state.write_text("once")
    print(json.dumps({"type": "error", "message": "stream broke"}), flush=True)
    print(json.dumps({"type": "done", "status": "failed"}), flush=True)
else:
    print(json.dumps({"type": "text", "delta": "second try: " + prompt}), flush=True)
    print(json.dumps({"type": "done", "status": "succeeded"}), flush=True)
"""

SLOW_ECHO = """import json, sys, time
prompt = sys.stdin.read()
if "Hurry" not in prompt:
    time.sleep(60)
print(json.dumps({"type": "text", "delta": "quick: " + prompt}), flush=True)
print(json.dumps({"type": "done", "status": "succeeded"}), flush=True)
"""


def test_a_subagent_gets_a_second_try_and_hears_from_its_caller(echo_chat, monkeypatch):
    from relay import delegation
    env = echo_chat
    monkeypatch.setattr(delegation, "EARLY", 0.5)
    providers = {"sleeper": [sys.executable, "-c", SLEEPER],
                 "flaky": [sys.executable, "-c", FLAKY, str(env.tmp / "flaky-state")],
                 "slowecho": [sys.executable, "-c", SLOW_ECHO]}
    for name, command in providers.items():
        env.post("/api/providers", json={"id": name, "name": name.title(), "kind": "command",
                                         "options": {"command": command}}, headers=H)
    env.put("/api/subagents", json={"allowed": [{"provider": "flaky", "model": "*"},
                                                {"provider": "slowecho", "model": "*"}]}, headers=H)
    conv, run_id, tool = _parent_with_tools(env, "sleeper")
    # One error in its stream: it goes on where it was, once
    text, error = tool("delegate", task="write chapter 1", provider="flaky", wait=True)
    assert not error and "Flaky: finished." in text and "second try: " in text
    assert "Your answer stopped with an error (stream broke). Please go on where you left off." in text
    # Its caller can tell it to hurry up: it's interrupted and goes on with the message
    started, error = tool("delegate", task="think long", provider="slowecho", label="Slow")
    short = started.split()[2]
    time.sleep(1)
    text, error = tool("tell", id=short, message="Hurry up: answer with what you have")
    assert not error and "interrupted it first" in text, text
    text, error = tool("wait", ids=[short])
    assert "quick: " in text and "A message from the agent that gave you this task: Hurry up" in text
    # …and a finished one gets a follow-up
    text, error = tool("tell", id=short, message="Hurry and say more")
    assert not error and "interrupted" not in text
    assert "Hurry and say more" in tool("wait", ids=[short])[0]
    assert tool("tell", id="nope", message="x")[1]  # no such subagent
    env.post(f"/api/runs/{run_id}/stop", headers=H)
    wait_idle(env, conv["id"])



def test_a_subagent_is_stopped_after_its_time_limit(echo_chat, monkeypatch):
    from relay import delegation, tunables
    env = echo_chat
    monkeypatch.setattr(delegation, "EARLY", 0.3)
    monkeypatch.setitem(tunables._values, "subagents.timeout", 0.02)  # 1.2 s
    env.post("/api/providers", json={"id": "sleeper", "name": "Sleeper", "kind": "command",
                                     "options": {"command": [sys.executable, "-c", SLEEPER]}}, headers=H)
    conv, run_id, tool = _parent_with_tools(env, "sleeper")
    started = time.time()
    text, error = tool("delegate", task="take your time", provider="sleeper", wait=True)
    assert "interrupted" in text and time.time() - started < 30, text
    env.post(f"/api/runs/{run_id}/stop", headers=H)
    wait_idle(env, conv["id"])


def test_limits_need_something_to_change(env):
    signed_in(env)
    assert env.put("/api/limits", json={"tunables": {"subagents.timeout": 4}}, headers=H).status_code == 400


def test_subagents_get_an_effort_and_share_a_local_server(echo_chat, monkeypatch):
    from relay import delegation, tunables
    from relay.providers.command import CommandProvider
    env = echo_chat
    monkeypatch.setattr(delegation, "EARLY", 0.5)
    relay = env.app.state.relay
    for name in ("sleeper", "sleeper2"):
        env.post("/api/providers", json={"id": name, "name": name.title(), "kind": "command",
                                         "options": {"command": [sys.executable, "-c", SLEEPER]}}, headers=H)
    env.put("/api/subagents", json={"allowed": [{"provider": "sleeper2", "model": "*"}]}, headers=H)
    conv, run_id, tool = _parent_with_tools(env, "sleeper")
    # Effort: what's asked for, or in Fast mode the model's lowest
    text, error = tool("delegate", task="a", provider="echo", effort="high")
    assert not error and "(effort high)" in text
    text, error = tool("delegate", task="b", provider="echo", effort="max")
    assert "It has no effort 'max' (low, medium, high), so it uses its default." in text
    relay.db.execute("UPDATE conversations SET agent_mode = 'fast' WHERE id = ?", (conv["id"],))
    text, error = tool("delegate", task="c", provider="echo")
    assert "(effort low)" in text
    efforts = [r["effort"] for r in relay.db.all(
        "SELECT effort FROM conversations WHERE parent_conversation = ? ORDER BY created", (conv["id"],))]
    assert efforts == ["high", None, "low"]
    assert "efforts (--effort): low, medium, high" in tool("agents")[0]
    # One model at a time on a local server: the parent's is working there
    monkeypatch.setattr(CommandProvider, "local_server",
                        lambda self, model: "gpu-box" if self.template.id.startswith("sleeper") else None)
    text, error = tool("delegate", task="d", provider="sleeper2")
    assert error and "gpu-box runs local models" in text and "provider sleeper, model its default" in text
    text, error = tool("delegate", task="e", provider="sleeper")  # the same model is fine
    assert not error, text
    monkeypatch.setitem(tunables._values, "subagents.local_models", 2)
    text, error = tool("delegate", task="f", provider="sleeper2")
    assert not error, text
    # Local models turned off (the local server isn't running): a model elsewhere, then
    rules = env.get("/api/subagents").json()
    assert rules["local"] is True
    env.put("/api/subagents", json={**rules, "local": False}, headers=H)
    text, error = tool("delegate", task="g", provider="sleeper2")
    assert error and "turned subagents on local models off" in text and "gpu-box" in text
    assert "turned off by the user" in tool("agents")[0]
    text, error = tool("delegate", task="h", provider="fake")
    assert "turned subagents on local models off" not in text  # not local: as the rules say
    env.post(f"/api/runs/{run_id}/stop", headers=H)
    wait_idle(env, conv["id"])


# A chat becomes a code task


def test_a_chat_moves_to_a_code_task(echo_chat):
    env = echo_chat
    chat = env.post("/api/conversations", json={"provider_id": "echo"}, headers=H).json()
    run_turn(env, chat["id"], "let's plan a script")
    project = str(env.tmp / "ws" / "project")
    url = f"/api/conversations/{chat['id']}/to-code"
    refused = env.post(url, json={"directory": project}, headers=H)
    assert refused.status_code == 400 and "can't work on files" in refused.json()["error"]  # echo has no tools
    assert env.post(url, json={"directory": "/etc", "provider_id": "fake"}, headers=H).status_code == 400
    moved = env.post(url, json={"directory": project, "provider_id": "fake", "isolate": False}, headers=H).json()
    assert moved["mode"] == "code" and moved["directory"] == project and moved["provider_id"] == "fake"
    assert not pathlib.Path(chat["directory"]).exists()  # the empty scratch folder is gone
    run_turn(env, chat["id"], "now write it")
    assert (pathlib.Path(project) / "agent.txt").exists()  # it works in the project now
    answer = answers(env, chat["id"])[-1]
    assert "let's plan a script" in answer  # the new agent got the conversation handed over
    assert env.post(url, json={"directory": project}, headers=H).status_code == 400  # a code task already


def test_a_chat_moving_to_a_code_task_brings_its_files_along(echo_chat):
    """What the chat's agent made goes into the project (once lost from sight: left in the chat's
    folder while the agent worked on in an empty project); a name the project has stays behind and
    is said; while a fork of the chat still works in its folder, they're copied."""
    env = echo_chat
    chat = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    scratch = pathlib.Path(chat["directory"])
    (scratch / "src").mkdir(parents=True)
    (scratch / "src" / "server.ts").write_text("serve()\n")
    (scratch / "package.json").write_text("{}")
    (scratch / "notes.txt").write_text("the chat's\n")
    project = env.tmp / "ws" / "Sitecraft"
    project.mkdir(parents=True)
    (project / "notes.txt").write_text("the project's\n")
    moved = env.post(f"/api/conversations/{chat['id']}/to-code", json={"directory": str(project), "isolate": False},
                     headers=H).json()
    assert (project / "src" / "server.ts").read_text() == "serve()\n" and (project / "package.json").exists()
    assert (project / "notes.txt").read_text() == "the project's\n"  # (not overwritten)
    assert moved["not_moved"] == ["notes.txt"] and moved["left_behind"] == str(scratch)
    assert sorted(p.name for p in scratch.iterdir()) == ["notes.txt"]
    # A fork still in the chat's folder: copied, so the fork keeps them
    chat = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    scratch = pathlib.Path(chat["directory"])
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / "app.py").write_text("x\n")
    run_turn(env, chat["id"], "hello")
    fork = env.post(f"/api/conversations/{chat['id']}/fork", json={}, headers=H).json()
    assert fork["directory"] == str(scratch)
    other = env.tmp / "ws" / "other"
    other.mkdir()
    moved = env.post(f"/api/conversations/{chat['id']}/to-code", json={"directory": str(other), "isolate": False},
                     headers=H).json()
    assert moved["left_behind"] is None and (other / "app.py").exists() and (scratch / "app.py").exists()


# Network drives (WebDAV)



def test_a_network_drive_keeps_git_hidden_and_files_whole(env):
    """Found by ChatGPT reviewing Relay through Relay."""
    signed_in(env)
    project = env.tmp / "ws" / "project"
    (project / ".git").mkdir()
    (project / ".git" / "config").write_text("secret")
    (project / "alias").symlink_to(project / ".git")
    (project / "dir").mkdir()
    (project / "dir" / "file.txt").write_text("x")
    (project / "dir" / "sibling.txt").write_text("y")
    (project / "a%20b.txt").write_text("literal")
    conv = env.post("/api/conversations", json={"provider_id": "fake", "mode": "code", "directory": str(project)},
                    headers=H).json()
    base = env.post(f"/api/conversations/{conv['id']}/share", json={}, headers=H).json()["path"]
    assert env.request("GET", base + "alias/config").status_code == 403  # a symlink to .git
    moved = env.request("MOVE", base + "dir/file.txt", headers={"Destination": base + "dir", "Overwrite": "T"})
    assert moved.status_code == 403 and (project / "dir" / "sibling.txt").exists()  # not onto its own folder
    from relay import webdav
    assert webdav._resolve(project, "a%20b.txt") == project / "a%20b.txt"  # decoded once, by the router


def test_a_network_drive_takes_a_name_too_long_calmly(env, monkeypatch):
    """Found in the live log: an editor (VS Code proposes a pasted text's first line as a file's name) asked the
    scripts' drive for a name too long for a file, and got a 500. Now: no such file (looking), or no (writing).
    And its log keeps refusals only (not an editor looking for its .vscode at every turn), its end only."""
    from opencode_manager import paths
    from relay import webdav
    signed_in(env)
    project = env.tmp / "ws" / "project"
    conv = env.post("/api/conversations", json={"provider_id": "fake", "mode": "code", "directory": str(project)},
                    headers=H).json()
    base = env.post(f"/api/conversations/{conv['id']}/share", json={}, headers=H).json()["path"]
    long = "Mirror finished: 50-odd pages and the assets are downloaded. " * 6
    assert env.request("PROPFIND", base + long, headers={"Depth": "0"}).status_code == 404
    assert env.request("PUT", base + long, content=b"text").status_code == 400
    log = paths.host_cache_dir() / "relay-webdav.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(webdav, "LOG_SIZE", 2000)
    log.write_text("an old line\n" * 400)  # (over its size)
    assert env.request("PROPFIND", base + ".vscode", headers={"Depth": "0"}).status_code == 404
    assert log.read_text() == "an old line\n" * 400  # (looking for what isn't there: not a refusal)
    env.request("PUT", base + long, content=b"text")
    kept = log.read_text().splitlines()
    assert len(log.read_bytes()) < 2000 and kept[0] == "an old line" and kept[-1].split("\t")[1:3] == ["PUT", "400"]


def test_a_code_task_is_shared_as_a_network_drive(env):
    signed_in(env)
    project = env.tmp / "ws" / "project"
    (project / "main.py").write_text("print('hi')\n")
    (project / ".git").mkdir()
    (project / ".git" / "config").write_text("secret")
    chat = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    assert env.post(f"/api/conversations/{chat['id']}/share", json={}, headers=H).status_code == 400
    conv = env.post("/api/conversations", json={"provider_id": "fake", "mode": "code", "directory": str(project),
                                                "isolate": False}, headers=H).json()
    share = env.post(f"/api/conversations/{conv['id']}/share", json={}, headers=H).json()
    base = share["path"]
    assert base.startswith("/dav/") and env.get("/api/shares").json()[0]["token"] == share["token"]

    def dav(method, path="", **kwargs):
        return env.request(method, base + path, **kwargs)

    assert env.options("/").headers["dav"] == "1, 2"  # Windows asks the root first
    listing = dav("PROPFIND", headers={"Depth": "1"})
    assert listing.status_code == 207 and "main.py" in listing.text and ".git" not in listing.text
    assert dav("GET", "main.py").text == "print('hi')\n"
    (project / "evil.html").write_text("<script src='evil.js'></script>")
    served = dav("GET", "evil.html")
    assert served.headers["content-security-policy"].startswith("sandbox;")  # no scripts, no session
    (project / "evil.html").unlink()
    assert dav("GET", "main.py", headers={"Range": "bytes=0-4"}).text == "print"
    assert dav("GET", ".git/config").status_code == 403
    assert dav("MKCOL", ".git").status_code == 403 and dav("PUT", ".git/config", content=b"x").status_code == 403
    assert (project / ".git" / "config").read_text() == "secret"
    assert dav("GET", "%2e%2e/%2e%2e/etc/passwd").status_code == 403
    # A repository copied in (a user's vault, from Dolphin) is a folder like any other: its .git too
    assert dav("MKCOL", "vault").status_code == 201 and dav("MKCOL", "vault/.git").status_code == 201
    assert dav("MKCOL", "vault/.git/refs").status_code == 201
    assert dav("PUT", "vault/.git/HEAD", content=b"ref: refs/heads/main\n").status_code == 201
    assert (project / "vault" / ".git" / "HEAD").read_text() == "ref: refs/heads/main\n"
    assert ".git" in dav("PROPFIND", "vault", headers={"Depth": "1"}).text
    assert ".git" not in dav("PROPFIND", headers={"Depth": "1"}).text  # (the folder's own: still out of sight)
    # What the drive refuses is in its log (it wasn't, for a folder it may not make)
    from opencode_manager import paths
    refused = (paths.host_cache_dir() / "relay-webdav.log").read_text().splitlines()
    assert any(line.split("\t")[1:4] == ["MKCOL", "403", f"{base}.git"] for line in refused), refused
    assert dav("MKCOL", "new dir").status_code == 201
    # Windows sets a new folder's times and attributes, and checks each one is answered
    patched = dav("PROPPATCH", "new%20dir", content=(
        b'<?xml version="1.0"?><D:propertyupdate xmlns:D="DAV:" xmlns:Z="urn:schemas-microsoft-com:"><D:set><D:prop>'
        b'<Z:Win32CreationTime>x</Z:Win32CreationTime><Z:Win32FileAttributes>00000010</Z:Win32FileAttributes>'
        b'</D:prop></D:set></D:propertyupdate>'))
    assert patched.status_code == 207 and "Win32CreationTime" in patched.text and "Win32FileAttributes" in patched.text
    assert dav("PUT", "new%20dir/a.txt", content=b"from my pc").status_code == 201
    assert (project / "new dir" / "a.txt").read_text() == "from my pc"
    assert dav("PUT", "new%20dir/a.txt", content=b"again").status_code == 204
    assert dav("PUT", ".DS_Store", content=b"junk").status_code == 201 and not (project / ".DS_Store").exists()
    moved = dav("MOVE", "new%20dir/a.txt", headers={"Destination": f"http://relay{base}b.txt"})
    assert moved.status_code == 201 and (project / "b.txt").read_text() == "again"
    assert dav("COPY", "b.txt", headers={"Destination": f"{base}b.txt"}).status_code == 403  # onto itself (RFC 4918)
    assert dav("PUT", "c2.txt", content=b"c").status_code == 201
    assert dav("COPY", "b.txt", headers={"Destination": f"{base}c2.txt", "Overwrite": "F"}).status_code == 412
    assert dav("MOVE", "new%20dir", headers={"Destination": f"{base}new%20dir/inside"}).status_code == 403
    locked = dav("LOCK", "c.txt", content=b"<lockinfo/>")
    assert locked.status_code == 201 and locked.headers["lock-token"].startswith("<opaquelocktoken:")
    assert dav("UNLOCK", "c.txt").status_code == 204
    assert dav("DELETE", "new%20dir").status_code == 204 and not (project / "new dir").exists()
    assert dav("DELETE").status_code == 403  # the shared folder itself stays

    env.post(f"/api/conversations/{conv['id']}/share", json={"read_only": True}, headers=H)
    assert dav("PUT", "d.txt", content=b"x").status_code == 403 and dav("GET", "b.txt").status_code == 200
    again = env.post(f"/api/conversations/{conv['id']}/share", json={}, headers=H).json()  # Open files again
    assert again["token"] == share["token"] and again["read_only"]
    env.delete(f"/api/shares/{share['token']}", headers=H)
    assert dav("PROPFIND").status_code == 404


# Usage limits


def test_a_model_that_cant_be_used_gets_no_plan_to_try_it_later(env):
    """Found by simulated users: a mistyped model ("Model unavailable: big-pickle/"), one not served in their
    country, one whose endpoint was down: each got "Relay will resume automatically at …", five hours on, as a
    usage limit does. A model that can't be used gets no such plan; one that only gave no answer still does."""
    signed_in(env)
    for name, said in (("typo", "Model unavailable: big-pickle/"), ("gone", "Model exo-free has been deprecated."),
                       ("silent", "")):
        agent = textwrap.dedent(f"""
            import json, sys
            sys.stdin.read()
            def emit(**e): print(json.dumps(e), flush=True)
            emit(type="session", session="s1")
            if {said!r}:
                emit(type="error", message={said!r})
            emit(type="done", status="failed")
        """)
        env.post("/api/providers", json={"id": name, "name": name.title(), "kind": "command",
                                         "options": {"command": [sys.executable, "-c", agent]}}, headers=H)
        chat = env.post("/api/conversations", json={"provider_id": name, "fallback_loop": "off"}, headers=H).json()
        run_turn(env, chat["id"], "hey")
        data = env.get(f"/api/conversations/{chat['id']}").json()
        if said:
            assert data["limit"]["unusable"] and data["limit"]["gave_up"] and data["resume_at"] is None, data["limit"]
            # Another model, chosen: it answers the message (the first one did nothing; "continue where it left
            # off" had the next model look for work that wasn't there, a simulated user found)
            env.post(f"/api/conversations/{chat['id']}/continue", json={"provider_id": "fake"}, headers=H)
            wait_idle(env, chat["id"])
            took_over = env.get(f"/api/conversations/{chat['id']}").json()["messages"][-2]
            assert took_over["relay_action"]["kind"] == "continue", took_over
            assert took_over["content"].endswith("stopped before it answered, so you answer instead. The user's "
                                                 "message:\n\nhey"), took_over["content"]
        else:
            assert not data["limit"]["unusable"] and data["limit"]["guessed"] and data["resume_at"], data["limit"]


def test_a_code_task_goes_on_when_its_usage_limit_resets(env):
    signed_in(env)
    relay = env.app.state.relay
    marker = env.tmp / "reset"
    resets = time.time() + 3600
    agent = textwrap.dedent(f"""
        import json, os, sys
        prompt = sys.stdin.read()
        def emit(**e): print(json.dumps(e), flush=True)
        emit(type="session", session="s1")
        if os.path.exists({str(marker)!r}):
            emit(type="text", delta="Going on: " + prompt[:40])
            emit(type="done", status="succeeded")
        else:
            emit(type="limit", status="rejected", resets_at={resets}, window="five_hour")
            emit(type="error", message="You've hit your limit")
            emit(type="done", status="failed")
    """)
    env.post("/api/providers", json={"id": "limited", "name": "Limited", "kind": "command",
                                     "options": {"command": [sys.executable, "-c", agent]}}, headers=H)
    task = env.post("/api/conversations", json={"provider_id": "limited", "mode": "code", "isolate": False,
                                                "directory": str(env.tmp / "ws" / "project")}, headers=H).json()
    run_turn(env, task["id"], "build it")
    data = env.get(f"/api/conversations/{task['id']}").json()
    assert data["limit"]["resets_at"] == resets and data["resume_at"] == pytest.approx(resets + 60)
    assert env.get("/api/models/status").json()["limited/"]["resets_at"] == resets
    listed = next(c for c in env.get("/api/conversations").json() if c["id"] == task["id"])
    assert listed["resume_at"]  # the sidebar shows it

    env.portal.call(relay.scheduler.tick, resets)  # not yet
    assert relay.runs.active_run_for(task["id"]) is None
    marker.write_text("")
    env.portal.call(relay.scheduler.tick, resets + 61)
    wait_idle(env, task["id"])
    data = env.get(f"/api/conversations/{task['id']}").json()
    assert data["messages"][-1]["content"].startswith("Going on: The usage limit has reset")
    assert data["messages"][-2]["relay_action"]["kind"] == "resume"
    assert data["limit"] is None and data["resume_at"] is None

    # A reset time in the past doesn't make it go on right away, and it gives up in the end
    from relay import runs as runs_module
    marker.unlink()
    for _ in range(runs_module.MAX_RESUMES + 1):
        run_turn(env, task["id"], "again")
    data = env.get(f"/api/conversations/{task['id']}").json()
    assert data["limit"]["gave_up"] and data["resume_at"] is None
    marker.write_text("")

    # A chat goes on by itself too (opt-out: Cancel), and "Resume then" undoes the Cancel
    marker.unlink()
    chat = env.post("/api/conversations", json={"provider_id": "limited"}, headers=H).json()
    run_turn(env, chat["id"], "hi")
    data = env.get(f"/api/conversations/{chat['id']}").json()
    assert data["limit"] and data["resume_at"] == pytest.approx(resets + 60)
    assert env.post(f"/api/conversations/{chat['id']}/resume", json={"cancel": True}, headers=H).json()["resume_at"] is None
    later = env.post(f"/api/conversations/{chat['id']}/resume", json={}, headers=H).json()
    assert later["resume_at"] == pytest.approx(resets + 60)

    # Continue with another model, now: it takes over
    marker.write_text("")
    env.post("/api/providers", json={"id": "spare", "name": "Spare", "kind": "command",
                                     "options": {"command": [sys.executable, "-c", agent]}}, headers=H)
    env.post(f"/api/conversations/{chat['id']}/continue", json={"provider_id": "spare"}, headers=H)
    wait_idle(env, chat["id"])
    data = env.get(f"/api/conversations/{chat['id']}").json()
    assert data["provider_id"] == "spare" and data["resume_at"] is None and data["limit"] is None
    # (it hit its limit before it did anything: the message is what's to answer, not work to go on with)
    assert data["messages"][-2]["content"].startswith("Limited stopped before it answered, so you answer instead")
    assert data["messages"][-2]["relay_action"]["kind"] == "continue"
    assert data["messages"][-2]["relay_action"]["from"] == {"provider_id": "limited", "model": None}
    assert data["messages"][-1]["content"].startswith("Going on:")  # with the conversation handed over


def test_a_fallback_agent_takes_over_and_an_unknown_reset_waits_once(env):
    signed_in(env)
    silent = textwrap.dedent("""
        import json, sys
        sys.stdin.read()
        print(json.dumps({"type": "session", "session": "s1"}), flush=True)
        print(json.dumps({"type": "error", "message": "Something went wrong upstream"}), flush=True)
        print(json.dumps({"type": "done", "status": "failed"}), flush=True)
    """)
    env.post("/api/providers", json={"id": "broken", "name": "Broken", "kind": "command",
                                     "options": {"command": [sys.executable, "-c", silent]}}, headers=H)
    # No fallback agents: an answer that failed without doing anything waits 5 hours, once
    conv = env.post("/api/conversations", json={"provider_id": "broken"}, headers=H).json()
    run_turn(env, conv["id"], "hi")
    data = env.get(f"/api/conversations/{conv['id']}").json()
    assert data["limit"]["guessed"] and data["resume_at"] == pytest.approx(time.time() + 5 * 3600, abs=30)
    run_turn(env, conv["id"], "again")
    data = env.get(f"/api/conversations/{conv['id']}").json()
    assert data["limit"]["gave_up"] and data["resume_at"] is None

    # In a fallback loop: the next agent of the loop takes over right away
    assert env.put("/api/fallback-loops", json={"loops": [{"name": "x", "entries": "nope/x"}]},
                   headers=H).status_code == 400
    saved = env.put("/api/fallback-loops", json={"loops": [{"name": "Main", "entries": "broken → fake/some-model"}]},
                    headers=H).json()
    assert saved[0]["name"] == "Main" and saved[0]["entries"] == [{"provider": "broken", "model": ""},
                                                                  {"provider": "fake", "model": "some-model"}]
    other = env.post("/api/conversations", json={"provider_id": "broken"}, headers=H).json()
    run_turn(env, other["id"], "hi")
    wait_idle(env, other["id"])
    data = env.get(f"/api/conversations/{other['id']}").json()
    assert data["provider_id"] == "fake" and data["model"] == "some-model" and data["limit"] is None
    assert data["messages"][-2]["content"].startswith("Broken stopped without an answer, so you take over")
    action = data["messages"][-2]["relay_action"]
    assert action == {"kind": "fallback", "from": {"provider_id": "broken", "model": None},
                      "to": {"provider_id": "fake", "model": "some-model"}, "handover": True}
    assert data["messages"][0]["relay_action"] is None
    fork = env.post(f"/api/conversations/{other['id']}/fork", json={}, headers=H).json()
    copied = env.get(f"/api/conversations/{fork['id']}").json()
    assert copied["messages"][-2]["relay_action"] == action
    env.post(f"/api/conversations/{other['id']}/retry", json={}, headers=H)
    wait_idle(env, other["id"])
    retried = env.get(f"/api/conversations/{other['id']}").json()
    assert retried["messages"][-2]["relay_action"]["kind"] == "retry"
    assert retried["messages"][-2]["content"] == data["messages"][-2]["content"]
    # An explicit user edit owns its text; it is no longer a Relay instruction.
    env.post(f"/api/conversations/{other['id']}/retry",
             json={"message_id": retried["messages"][-2]["id"], "text": "My own continuation"}, headers=H)
    wait_idle(env, other["id"])
    edited = env.get(f"/api/conversations/{other['id']}").json()
    assert edited["messages"][-2]["relay_action"] is None
    # Continuing after success retains the ordinary resume prompt, not a false stop claim.
    env.post(f"/api/conversations/{other['id']}/continue", json={"model": "another-model"}, headers=H)
    wait_idle(env, other["id"])
    continued = env.get(f"/api/conversations/{other['id']}").json()
    assert "stopped, so you take over" not in continued["messages"][-2]["content"]
    assert continued["messages"][-2]["relay_action"]["from"] == {"provider_id": "fake", "model": "some-model"}


def test_a_fallback_loop_goes_round_and_starts_again(env, monkeypatch):
    from relay import tunables
    signed_in(env)
    relay = env.app.state.relay
    monkeypatch.setitem(tunables._values, "limits.loop_rounds", 2)
    silent = textwrap.dedent("""
        import json, sys
        sys.stdin.read()
        print(json.dumps({"type": "error", "message": "Something went wrong upstream"}), flush=True)
        print(json.dumps({"type": "done", "status": "failed"}), flush=True)
    """)
    for name in ("first", "second"):
        env.post("/api/providers", json={"id": name, "name": name.title(), "kind": "command",
                                         "options": {"command": [sys.executable, "-c", silent]}}, headers=H)
    # The list from before loops is a loop of its own
    relay.db.execute("INSERT INTO preferences (user_id, key, value) VALUES (1, 'fallback_agents', ?)",
                     (json.dumps([{"provider": "second", "model": ""}]),))
    assert [loop["name"] for loop in env.get("/api/fallback-loops").json()] == ["Fallback agents"]
    loops = env.put("/api/fallback-loops", json={"loops": [
        {"name": "Both", "entries": "first, second"}, {"name": "Fake", "entries": "fake"}]}, headers=H).json()
    conv = env.post("/api/conversations", json={"provider_id": "first"}, headers=H).json()

    def settled():
        deadline = time.time() + 15
        while True:
            data = env.get(f"/api/conversations/{conv['id']}").json()
            if data["running_run"] is None and data["limit"] and (data["resume_at"] or data["limit"].get("gave_up")):
                return data
            assert time.time() < deadline, data
            time.sleep(0.05)

    run_turn(env, conv["id"], "hi")
    data = settled()  # first → second (right away) → nobody left: wait, then the loop again
    limit = data["limit"]
    assert data["provider_id"] == "second" and limit["loop"] == {"id": loops[0]["id"], "name": "Both", "round": 1}
    assert limit["restart"] == {"provider": "first", "model": ""} and limit["guessed"]
    assert data["resume_at"] == pytest.approx(time.time() + 5 * 3600, abs=30)
    relay.scheduler._resume(data["resume_at"] + 1)  # round 2, from the start
    data = settled()
    assert data["limit"]["gave_up"] and data["limit"]["loop"]["round"] == 2 and data["resume_at"] is None
    runs = relay.db.all("SELECT provider_id FROM runs WHERE conversation_id = ? ORDER BY created", (conv["id"],))
    assert [r["provider_id"] for r in runs] == ["first", "second", "first", "second"]

    # Another loop chosen while waiting: that one starts; none: no waiting for a loop
    relay.db.execute("UPDATE conversations SET limit_info = ?, resume_at = ? WHERE id = ?",
                     (json.dumps({**data["limit"], "gave_up": False, "restart": {"provider": "first", "model": ""}}),
                      time.time() + 3600, conv["id"]))
    changed = env.patch(f"/api/conversations/{conv['id']}", json={"fallback_loop": loops[1]["id"]}, headers=H).json()
    assert changed["fallback_loop"] == loops[1]["id"] and changed["limit"]["restart"] == {"provider": "fake", "model": ""}
    off = env.patch(f"/api/conversations/{conv['id']}", json={"fallback_loop": "off"}, headers=H).json()
    assert off["resume_at"] is None and "restart" not in off["limit"]
    assert env.patch(f"/api/conversations/{conv['id']}", json={"fallback_loop": "nope"}, headers=H).status_code == 400


# Scheduled tasks



def test_renaming_a_schedule_keeps_when_it_runs(env):
    signed_in(env)
    made = env.post("/api/schedules", json={"name": "Hourly", "prompt": "check", "provider_id": "fake",
                                            "when": {"kind": "interval", "minutes": 60}}, headers=H).json()
    assert made.get("next_run"), made
    time.sleep(1.1)
    renamed = env.put(f"/api/schedules/{made['id']}", json={"name": "Hourly check"}, headers=H).json()
    assert renamed["name"] == "Hourly check" and renamed["next_run"] == made["next_run"]


def test_scheduled_tasks_run_by_themselves(env):
    signed_in(env)
    relay = env.app.state.relay
    now = time.time()
    daily = env.post("/api/schedules", json={
        "name": "Morning summary", "prompt": "summarise the news", "provider_id": "fake",
        "when": {"kind": "daily", "time": "08:00"}, "timezone": "UTC"}, headers=H).json()
    assert daily["describe"] == "every day at 08:00" and daily["next_run"] > now
    assert env.post("/api/schedules", json={"name": "x", "prompt": "y", "provider_id": "fake",
                                            "when": {"kind": "once", "at": now - 10}}, headers=H).status_code == 400
    assert env.post("/api/schedules", json={"name": "x", "prompt": "y", "provider_id": "fake", "mode": "code",
                                            "when": {"kind": "interval", "minutes": 60}}, headers=H).status_code == 400

    env.portal.call(relay.scheduler.tick, daily["next_run"] + 1)
    first = env.get("/api/schedules").json()[0]  # the only one yet
    assert first["last_conversation"] and first["next_run"] == pytest.approx(daily["next_run"] + 86400)
    wait_idle(env, first["last_conversation"])
    conv = env.get(f"/api/conversations/{first['last_conversation']}").json()
    assert conv["title"].startswith("Morning summary · ") and conv["messages"][0]["content"] == "summarise the news"

    # On in the same chat, once; afterwards it's switched off
    same = env.post("/api/schedules", json={
        "name": "Follow up", "prompt": "and now?", "provider_id": "fake", "target": "same", "agent_mode": "plan",
        "conversation_id": conv["id"], "when": {"kind": "once", "at": now + 100}}, headers=H).json()
    env.portal.call(relay.scheduler.tick, now + 101)
    wait_idle(env, conv["id"])
    assert "and now?" in answers(env, conv["id"])[-1] and "to the Plan mode" in answers(env, conv["id"])[-1]
    assert env.get(f"/api/conversations/{conv['id']}").json()["agent_mode"] == "plan"  # as the schedule is set
    done = next(s for s in env.get("/api/schedules").json() if s["id"] == same["id"])
    assert not done["enabled"] and done["next_run"] is None

    ran = env.post(f"/api/schedules/{daily['id']}/run", headers=H).json()  # now, by hand
    assert ran["conversation_id"] != conv["id"]
    wait_idle(env, ran["conversation_id"])
    kept = next(x for x in env.get("/api/schedules").json() if x["id"] == daily["id"])
    assert kept["next_run"] == pytest.approx(daily["next_run"] + 86400)  # running it by hand moves nothing
    paused = env.put(f"/api/schedules/{daily['id']}", json={"enabled": False}, headers=H).json()
    assert paused["next_run"] is None
    env.delete(f"/api/schedules/{daily['id']}", headers=H)
    assert len(env.get("/api/schedules").json()) == 1


# Limits (tunables)


def test_limits_can_be_changed_and_apply_at_once(env):
    signed_in(env)
    from relay import config, tunables
    data = env.get("/api/limits").json()
    parallel = next(t for t in data["tunables"] if t["key"] == "subagents.parallel")
    assert parallel["value"] == 4 and parallel["default"] == 4 and parallel["kind"] == "int"
    assert data["server"]["port"] == 8420
    changed = env.put("/api/limits", json={"values": {"subagents.parallel": 5, "limits.resume": False},
                                           "server": {"session_days": 7}}, headers=H).json()
    assert tunables.get("subagents.parallel") == 5 and tunables.get("limits.resume") is False
    assert next(t for t in changed["tunables"] if t["key"] == "subagents.parallel")["value"] == 5
    saved = config.load_settings()
    assert saved.limits == {"subagents.parallel": 5, "limits.resume": False} and saved.session_days == 7
    assert env.put("/api/limits", json={"values": {"subagents.parallel": 99}}, headers=H).status_code == 400
    assert env.put("/api/limits", json={"values": {"nope": 1}}, headers=H).status_code == 400
    assert tunables.get("subagents.parallel") == 5  # a bad change changes nothing
    env.put("/api/limits", json={"values": {"subagents.parallel": 4}}, headers=H)  # back to the default
    assert "subagents.parallel" not in config.load_settings().limits


def test_question_timeout_is_configurable_and_zero_disables_it(env):
    from relay import config, tunables
    signed_in(env)
    setting = next(t for t in env.get("/api/limits").json()["tunables"] if t["key"] == "questions.dismiss_timeout")
    assert setting["group"] == "Questions" and setting["value"] == setting["default"] == 300
    for seconds in (10, 0):
        response = env.put("/api/limits", json={"values": {"questions.dismiss_timeout": seconds}}, headers=H)
        assert response.status_code == 200, response.text
        assert tunables.get("questions.dismiss_timeout") == seconds
        assert config.load_settings().limits["questions.dismiss_timeout"] == seconds
    assert env.put("/api/limits", json={"values": {"questions.dismiss_timeout": -1}}, headers=H).status_code == 400
    assert env.put("/api/limits", json={"values": {"questions.dismiss_timeout": 86401}}, headers=H).status_code == 400


# Self-repair


# A repair's agent, for the tests: it lingers when asked to (to hand in its plan by MCP meanwhile), and once its plan
# is approved (or the user found something wrong) it changes the layer, a note beside it and Relay itself (out of
# bounds: reported, never taken)
FIXER = """
import json, os, sys, time
prompt = sys.stdin.read()
if "linger" in prompt:
    for _ in range(150):
        if os.path.exists("done-lingering"):
            break
        time.sleep(0.1)
if "approved" in prompt or "tried it" in prompt:
    open("relay/providers/command/provider.py", "a").write("\\n# repaired\\n")
    open("notes.txt", "w").write("scratch")
    open({app!r}, "a").write("\\nBROKEN = True\\n")
print(json.dumps({{"type": "session", "session": "fix"}}), flush=True)
print(json.dumps({{"type": "text", "delta": "Done."}}), flush=True)
print(json.dumps({{"type": "done", "status": "succeeded"}}), flush=True)
"""


def test_a_provider_is_repaired_on_its_page(env, monkeypatch, host_boxes):
    """Settings → a provider's 🔧 (relay.creations, a repair): what's wrong as Relay sees it; its agent's plan (the
    plan alone, by MCP), redrafted and approved; the repair, on copies of its support files; Relay's whole check of
    it, by itself when the agent is done; a throwaway Relay with the provider as it is and the repaired files; Apply
    (the originals kept) and Undo; one repair going per provider; Cancel leaves the provider's home."""
    import shutil as sh

    from relay import creations, repair
    signed_in(env)
    relay = env.app.state.relay
    relay.repairs.boxes = host_boxes  # (its tests' box: this machine)
    source = env.tmp / "code" / "src"
    for package in ("relay", "opencode_manager"):
        sh.copytree(repair.SOURCE / package, source / package, ignore=sh.ignore_patterns("__pycache__"))
    (source.parent / "vendor").symlink_to(repair.SOURCE.parent / "vendor")
    for module in (repair, creations):
        monkeypatch.setattr(module, "SOURCE", source)  # never the real one
    monkeypatch.setattr(creations, "trial_dir", lambda creation_id: env.tmp / "trials" / creation_id)
    stub = env.tmp / "trial_stub.py"
    stub.write_text(TRIAL_STUB)
    relay.creations.launch = lambda where, environment: subprocess.Popen(
        [sys.executable, str(stub)], cwd=where, env=environment, start_new_session=True, stdin=subprocess.DEVNULL)
    checks = []

    def shadow_test(folder, files, target, model=None, providers_dir=None, boxes=None, new=None, home=None,
                    full=False):  # (Relay's whole check: the first fails, then it passes)
        checks.append({"full": full, "files": files, "target": target})
        ok = len(checks) > 1
        return {"ok": ok, "results": [{"name": "A turn", "ok": ok, "detail": "answered" if ok else "no text"}],
                "mounts": []}
    monkeypatch.setattr(repair, "shadow_test", shadow_test)
    env.post("/api/providers", json={"id": "fixer", "name": "Fixer", "kind": "command", "options": {
        "command": [sys.executable, "-c", FIXER.format(app=str(source / "relay" / "app.py"))]}}, headers=H)
    user = relay.db.one("SELECT id FROM users")["id"]

    def settled(rid):
        deadline = time.time() + 60
        while time.time() < deadline:
            view = env.get(f"/api/provider-repairs/{rid}").json()
            if not view["checking"] and not view["working"]:
                return view
            time.sleep(0.1)
        raise AssertionError("still checking")

    def tools(chat):
        token = relay.db.one("SELECT tools_token FROM conversations WHERE id = ?", (chat,))["tools_token"]
        return {t["name"] for t in mcp(env, token, "tools/list")[1]["result"]["tools"]}

    def call(chat, tool, **arguments):
        token = relay.db.one("SELECT tools_token FROM conversations WHERE id = ?", (chat,))["tools_token"]
        result = mcp(env, token, "tools/call", {"name": tool, "arguments": arguments})[1]["result"]
        return result["content"][0]["text"], result.get("isError")

    # 1. What's wrong, as Relay sees it; its page (one going per provider)
    trouble = env.get("/api/providers/fake/trouble").json()
    assert trouble["check"]["ok"] and trouble["repair"] is None and trouble["provider"]["name"] == "Fake agent"
    made = env.post("/api/provider-repairs", json={"provider_id": "fake"}, headers=H).json()
    rid = made["id"]
    assert made["status"] == "setup" and not made["creates"] and not made["legacy"]
    assert made["provider"]["name"] == "Fake agent" and made["page_path"] == f"/providers/repair/{rid}"
    assert env.post("/api/provider-repairs", json={"provider_id": "fake"}, headers=H).json()["id"] == rid
    assert env.get("/api/providers/fake/trouble").json()["repair"] == rid
    with pytest.raises(repair.RepairError, match="doesn't run in a box"):
        relay.creations.shell(user, rid)
    itself = env.post(f"/api/provider-repairs/{rid}/start", json={"provider_id": "fake"}, headers=H)
    assert itself.status_code == 400 and "another provider" in itself.json()["error"]
    # 2. Its plan: what's wrong to the agent chosen, in Plan mode, with what it needs in REPAIR.md
    started = env.post(f"/api/provider-repairs/{rid}/start", json={"about": "it stopped answering",
                                                                   "provider_id": "fixer"}, headers=H).json()
    assert started["status"] == "planning"
    chat = env.get(f"/api/conversations/{started['conversation_id']}").json()
    assert chat["title"] == "Repair: Fake agent" and chat["agent_mode"] == "plan"
    folder = pathlib.Path(started["folder"])
    brief = (folder / "REPAIR.md").read_text()
    assert "it stopped answering" in brief and "`relay/providers/command/provider.py`" in brief
    assert "provider_plan" in brief and "repair run -- <its program>" in brief  # (no kind's name for a program)
    assert (folder / "reference" / "relay" / "providers" / "base.py").is_file()
    wait_idle(env, chat["id"])
    env.post(f"/api/conversations/{chat['id']}/messages", json={"text": "linger"}, headers=H)
    try:
        assert {"provider_plan", "repair_run", "repair_check"} <= tools(chat["id"])
        text, error = call(chat["id"], "provider_plan", plan="The layer misreads its output; I'll fix its parser.")
        assert not error and "Stop here" in text, text
    finally:
        let_go(env, chat)
    assert env.get(f"/api/provider-repairs/{rid}").json()["status"] == "planned"
    assert env.post(f"/api/provider-repairs/{rid}/redraft", json={"text": "say which file"},
                    headers=H).json()["status"] == "planning"
    wait_idle(env, chat["id"])
    relay.creations.plan(relay.repairs.get(user, rid), "", "", "relay/providers/command/provider.py: its parser.")
    # 3. Approved: the agent repairs it; Relay checks it fully by itself (the first check fails)
    assert env.post(f"/api/provider-repairs/{rid}/approve", json={}, headers=H).json()["status"] == "building"
    assert env.get(f"/api/conversations/{chat['id']}").json()["agent_mode"] is None
    failed = settled(rid)
    assert failed["status"] == "building" and not failed["review"]["test"]["ok"] and failed["trial"] is None
    assert checks[0]["full"] and checks[0]["target"] == "fake"
    review = failed["review"]
    assert review["changed"] == ["relay/providers/command/provider.py"] and "+# repaired" in review["diff"]
    assert "notes.txt" in review["ignored"] and "relay/app.py" in review["left_alone"]  # (reported, not taken)
    # Its failures to its agent; its fix passes, and a throwaway Relay has the provider as it is, repaired
    assert env.post(f"/api/provider-repairs/{rid}/fix", json={}, headers=H).json()["status"] == "building"
    trying = settled(rid)
    assert any("A turn: no text" in (m["content"] or "")
               for m in env.get(f"/api/conversations/{chat['id']}").json()["messages"] if m["role"] == "user")
    assert trying["status"] == "trying" and trying["trial"] == {"url": f"/trial/{rid}/"}
    seen = env.get(f"/trial/{rid}/seen").json()
    assert [(t["id"], t["kind"]) for t in seen["providers"]] == [("fake", "command")]
    assert seen["settings"]["trial"]["back"] == f"/providers/repair/{rid}"
    assert (pathlib.Path(seen["code"]) / "relay" / "providers" / "command" / "provider.py").read_text().endswith(
        "# repaired\n")
    assert env.post(f"/api/provider-repairs/{rid}/add", json={}, headers=H).status_code == 400  # (a repair: Apply)
    # Apply: into Relay, the originals kept; Undo brings them back
    applied = env.post(f"/api/provider-repairs/{rid}/apply", json={}, headers=H).json()
    assert applied["status"] == "applied" and relay.creations.trial(rid) is None
    layer = source / "relay" / "providers" / "command" / "provider.py"
    assert layer.read_text().endswith("# repaired\n") and "BROKEN" in (source / "relay" / "app.py").read_text()
    assert env.post(f"/api/provider-repairs/{rid}/undo", json={}, headers=H).json()["status"] == "undone"
    assert not layer.read_text().endswith("# repaired\n")
    # Cancelled: its folder goes, the provider's own home stays
    home = env.tmp / "toy-home"
    home.mkdir()
    env.post("/api/providers", json={"id": "toy", "name": "Toy", "kind": "example", "home": str(home)}, headers=H)
    other = env.post("/api/provider-repairs", json={"provider_id": "toy"}, headers=H).json()
    assert other["home"] == str(home) and other["id"] != rid
    gone = env.post(f"/api/provider-repairs/{other['id']}/discard", json={}, headers=H).json()
    assert gone["status"] == "discarded" and home.is_dir()


# A trial Relay, smaller: it answers as one would, from its settings (relay.creations: Creations.launch)
TRIAL_STUB = textwrap.dedent("""\
    import json, os
    from pathlib import Path
    import uvicorn
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse, RedirectResponse
    from starlette.routing import Route
    home = Path(os.environ["RELAY_HOME"])
    settings = json.loads((home / "settings.json").read_text())
    def cookie(response, value):
        response.set_cookie("relay_session", value)
        return response
    async def setup(request):
        return cookie(JSONResponse({"ok": True}), "trial-token")
    async def seen(request):
        return JSONResponse({"path": request.url.path, "headers": dict(request.headers), "settings": settings,
                             "providers": [json.loads(p.read_text()) for p in (home / "providers").glob("*.json")],
                             "code": os.environ["PYTHONPATH"].split(os.pathsep)[0]})
    async def go(request):
        return RedirectResponse(f"http://localhost:{settings['port']}/elsewhere", status_code=307)
    async def other(request):
        return cookie(JSONResponse({"ok": True}), "not-for-the-browser")
    app = Starlette(routes=[Route("/api/setup", setup, methods=["POST"]), Route("/seen", seen), Route("/go", go),
                            Route("/other", other)])
    uvicorn.run(app, host="127.0.0.1", port=settings["port"], log_level="warning")
""")


def test_a_provider_relay_doesnt_know_is_added_with_an_agent(env, monkeypatch, host_boxes):
    """Settings → Add provider → Add it with an agent (relay.creations): the user installs it in a box of its own and
    says what it is; their agent studies it and hands in a plan (provider_plan), which they approve or redraft; it
    builds it, Relay checks it fully in a test box and starts a trial Relay with it, at /trial/<id>/ (signed in as
    the user, setting no cookie of its own); something wrong goes back to the agent; Add puts it in Relay with its
    home, Take it out again takes it out; Cancel leaves nothing behind."""
    import shutil as sh

    from relay import creations, repair
    signed_in(env)
    relay = env.app.state.relay
    relay.repairs.boxes = host_boxes  # (its tests' box: this machine)
    source = env.tmp / "code" / "src"
    for package in ("relay", "opencode_manager"):
        sh.copytree(repair.SOURCE / package, source / package, ignore=sh.ignore_patterns("__pycache__"))
    (source.parent / "vendor").symlink_to(repair.SOURCE.parent / "vendor")
    for module in (repair, creations):
        monkeypatch.setattr(module, "SOURCE", source)  # never the real one
    monkeypatch.setenv("HOME", str(env.tmp))
    monkeypatch.setattr(creations, "trial_dir", lambda creation_id: env.tmp / "trials" / creation_id)
    stub = env.tmp / "trial_stub.py"
    stub.write_text(TRIAL_STUB)
    relay.creations.launch = lambda where, environment: subprocess.Popen(
        [sys.executable, str(stub)], cwd=where, env=environment, start_new_session=True, stdin=subprocess.DEVNULL)
    user = relay.db.one("SELECT id FROM users")["id"]

    def settled(cid):
        deadline = time.time() + 120
        while time.time() < deadline:
            view = env.get(f"/api/provider-creations/{cid}").json()
            if not view["checking"] and not view["working"]:
                return view
            time.sleep(0.1)
        raise AssertionError("still checking")

    def tools(chat):
        token = relay.db.one("SELECT tools_token FROM conversations WHERE id = ?", (chat["id"],))["tools_token"]
        return {t["name"] for t in mcp(env, token, "tools/list")[1]["result"]["tools"]}

    def call(chat, tool, **arguments):
        token = relay.db.one("SELECT tools_token FROM conversations WHERE id = ?", (chat["id"],))["tools_token"]
        result = mcp(env, token, "tools/call", {"name": tool, "arguments": arguments})[1]["result"]
        return result["content"][0]["text"], result.get("isError")

    # 1. A home for it, and a box with a shell to install it in (this Relay has no boxes: it says so)
    made = env.post("/api/provider-creations", json={}, headers=H).json()
    cid, home = made["id"], pathlib.Path(made["home"])
    assert made["status"] == "setup" and home.is_dir() and not made["legacy"] and made["trial"] is None
    assert [c["id"] for c in env.get("/api/provider-creations").json()] == [cid]
    with pytest.raises(repair.RepairError, match="boxes"):
        relay.creations.shell(user, cid)
    # 2. What the user installed, to the agent they chose: in Plan mode, with what it needs in CREATE.md
    said = env.post(f"/api/provider-creations/{cid}/start", json={"about": " ", "provider_id": "fake"}, headers=H)
    assert said.status_code == 400 and "installed" in said.json()["error"]
    started = env.post(f"/api/provider-creations/{cid}/start", json={"about": "toy-agent, a tiny agent",
                                                                     "provider_id": "fake"}, headers=H).json()
    assert started["status"] == "planning"
    chat = env.get(f"/api/conversations/{started['conversation_id']}").json()
    assert chat["title"] == "New provider" and chat["agent_mode"] == "plan" and chat["mode"] == "code"
    assert "CREATE.md" in chat["messages"][0]["content"] and "provider_plan" in chat["messages"][0]["content"]
    brief = (pathlib.Path(started["folder"]) / "CREATE.md").read_text()
    assert "toy-agent, a tiny agent" in brief and str(home) in brief and "provider_plan" in brief
    wait_idle(env, chat["id"])
    # Its plan, by its tool: a name Relay hasn't, how it's started, the plan
    env.post(f"/api/conversations/{chat['id']}/messages", json={"text": "linger"}, headers=H)
    try:
        assert {"provider_plan", "repair_run", "repair_check"} <= tools(chat)
        for name, wrong in (("Not A Folder", "lowercase"), ("example", "already")):
            text, error = call(chat, "provider_plan", name=name, program="toy-agent", plan="a plan")
            assert error and wrong in text, text
        text, error = call(chat, "provider_plan", name="toy2", program="toy-agent", plan="Run toy-agent; its JSON.")
        assert not error and "Stop here" in text, text
        text, error = call(chat, "repair_check")
        assert error and "nothing to check" in text, text
    finally:
        let_go(env, chat)
    planned = env.get(f"/api/provider-creations/{cid}").json()
    assert planned["status"] == "planned" and planned["plan"] == "Run toy-agent; its JSON." and planned["target_id"] == "toy2"
    assert env.get(f"/api/conversations/{chat['id']}").json()["title"] == "New provider: toy2"
    # Redraft: the user's words to its agent, which hands in another
    assert env.post(f"/api/provider-creations/{cid}/redraft", json={"text": "use its sessions"},
                    headers=H).json()["status"] == "planning"
    wait_idle(env, chat["id"])
    assert "use its sessions" in env.get(f"/api/conversations/{chat['id']}").json()["messages"][-2]["content"]
    relay.creations.plan(relay.repairs.get(user, cid), "toy2", "toy-agent", "Run toy-agent, with its sessions.")
    # Approved: the guide's example in its folder, its agent asked to build it (not in Plan mode any more)
    approved = env.post(f"/api/provider-creations/{cid}/approve", json={}, headers=H).json()
    assert approved["status"] == "building"
    layer = pathlib.Path(approved["folder"]) / "relay" / "providers" / "toy2" / "provider.py"
    assert layer.is_file() and "relay.providers.example" not in (layer.parent / "__init__.py").read_text()
    assert env.get(f"/api/conversations/{chat['id']}").json()["agent_mode"] is None
    # 3. Its turn over, Relay checks it: as copied it's the example's kind, which Relay has
    failed = settled(cid)
    assert failed["status"] == "building" and not failed["review"]["test"]["ok"] and failed["trial"] is None

    def not_to_review():  # (Relay takes its changes on its page: it's no task to review in Tasks)
        assert chat["id"] not in {t["id"] for t in env.get("/api/tasks").json()["review"]}
        assert env.get(f"/api/conversations/{chat['id']}").json()["changes"] is None
    not_to_review()
    layer.write_text(layer.read_text().replace('KIND = "example"', 'KIND = "toy-two"')
                     .replace('"kind": "example"', '"kind": "toy-two"'))
    env.post(f"/api/provider-creations/{cid}/check", json={}, headers=H)
    trying = settled(cid)
    assert trying["status"] == "trying" and trying["review"]["test"]["ok"], trying["review"]
    assert trying["trial"] == {"url": f"/trial/{cid}/"} and trying["review"]["template"]["kind"] == "toy-two"
    # The trial Relay: its own code and data, the new provider alone (with the home), at /trial/<id>/
    seen = env.get(f"/trial/{cid}/seen?x=1").json()
    assert seen["path"] == "/seen" and seen["headers"]["cookie"] == "relay_session=trial-token"
    assert seen["headers"]["x-forwarded-prefix"] == f"/trial/{cid}" and seen["settings"]["base_path"] == f"/trial/{cid}"
    assert seen["settings"]["host"] == "127.0.0.1" and seen["settings"]["trial"]["back"] == f"/providers/new/{cid}"
    assert [(t["kind"], t["home"]) for t in seen["providers"]] == [("toy-two", str(home))]
    assert (pathlib.Path(seen["code"]) / "relay" / "providers" / "toy2" / "provider.py").is_file()
    assert "set-cookie" not in env.get(f"/trial/{cid}/other").headers  # (the browser keeps Relay's session)
    assert env.get(f"/trial/{cid}/go", follow_redirects=False).headers["location"] == f"/trial/{cid}/elsewhere"
    assert env.post(f"/trial/{cid}/api/logout", json={}, headers=H).json() == {"ok": True}  # (its session stays)
    assert env.get(f"/trial/{cid}", follow_redirects=False).headers["location"] == f"/trial/{cid}/"
    assert env.get("/trial/0123456789ab/seen").status_code == 404
    # Something's wrong: to its agent, the trial gone meanwhile; its fix checked and tried again
    assert env.post(f"/api/provider-creations/{cid}/fix", json={"text": "it answers twice"},
                    headers=H).json()["status"] == "building"
    assert relay.creations.trial(cid) is None and not (env.tmp / "trials" / cid).exists()
    assert settled(cid)["status"] == "trying"
    assert "it answers twice" in env.get(f"/api/conversations/{chat['id']}").json()["messages"][-2]["content"]
    # Add: in Relay, with its template (and the home); the trial gone
    added = env.post(f"/api/provider-creations/{cid}/add", json={}, headers=H).json()
    assert added["status"] == "applied" and (source / "relay" / "providers" / "toy2" / "provider.py").is_file()
    template = json.loads((relay.registry.directory / "toy.json").read_text())
    assert template["kind"] == "toy-two" and template["home"] == str(home) and template["enabled"]
    assert relay.creations.trial(cid) is None and not (env.tmp / "trials" / cid).exists()
    assert env.get("/api/provider-creations").json() == []
    not_to_review()
    # Taken out again: its folder, its template and its home
    assert env.post(f"/api/provider-creations/{cid}/undo", json={}, headers=H).json()["status"] == "undone"
    assert not (source / "relay" / "providers" / "toy2").exists() and not (relay.registry.directory / "toy.json").exists()
    assert not home.exists()
    # Cancelled: nothing of it stays
    other = env.post("/api/provider-creations", json={}, headers=H).json()
    assert env.post(f"/api/provider-creations/{other['id']}/discard", json={}, headers=H).json()["status"] == "discarded"
    assert not pathlib.Path(other["home"]).exists() and not pathlib.Path(other["folder"]).exists()


def test_a_repairs_agent_has_relay_test_and_run_what_its_container_cant(env, monkeypatch, host_boxes):
    """A repair's agent's commands run in its folder's WorkBox, which has neither the program nor Relay's source to
    test in: repair_check and repair_run (only in a repair's conversation, while it's going) do that outside it. A
    user found its agent couldn't run either (`command -v claude` failed in it, and check.py)."""
    import shutil as sh

    from relay import repair
    signed_in(env)
    relay = env.app.state.relay
    source = env.tmp / "src"
    for package in ("relay", "opencode_manager"):
        sh.copytree(repair.SOURCE / package, source / package, ignore=sh.ignore_patterns("__pycache__"))
    monkeypatch.setattr(repair, "SOURCE", source)  # never the real one
    relay.repairs.boxes = host_boxes  # (its tests' box: this machine)
    user = relay.db.one("SELECT id FROM users")["id"]
    made = relay.creations.begin_repair(user, "fake")
    relay.creations.prepare(user, made["id"], "")
    conv, other = (env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json() for _ in range(2))
    relay.repairs.attach(made["id"], conv["id"])
    for chat in (conv, other):
        env.post(f"/api/conversations/{chat['id']}/messages", json={"text": "linger"}, headers=H)

    def tools(chat):
        token = relay.db.one("SELECT tools_token FROM conversations WHERE id = ?", (chat["id"],))["tools_token"]
        return {t["name"] for t in mcp(env, token, "tools/list")[1]["result"]["tools"]}

    def call(chat, name, **arguments):
        token = relay.db.one("SELECT tools_token FROM conversations WHERE id = ?", (chat["id"],))["tools_token"]
        result = mcp(env, token, "tools/call", {"name": name, "arguments": arguments})[1]["result"]
        return result["content"][0]["text"], result.get("isError")

    try:
        assert {"repair_check", "repair_run"} <= tools(conv) and not {"repair_check", "repair_run"} & tools(other)
        text, error = call(conv, "repair_check")
        assert not error and json.loads(text)["ok"], text
        text, error = call(conv, "repair_run", command="fake --version")  # (a command agent: no box of its own)
        assert error and "doesn't run in a box of Relay's here" in text
        text, error = call(other, "repair_check")
        assert error and "isn't one of this conversation's" in text
        relay.repairs.discard(user, made["id"])  # done: its tools go
        assert "repair_check" not in tools(conv)
    finally:
        let_go(env, conv)
        let_go(env, other)


def test_the_governor_has_a_provider_that_keeps_failing_repaired_and_the_user_applies_it(echo_chat, monkeypatch,
                                                                                           host_boxes):
    """A provider whose last runs all failed, not on a limit or something that passes: the Governor is
    woken (without AgentOS the user is told, once until it works again, with its repair page); provider.repair
    has another agent look into it on the repair's page, in a managed code task; its plan, trying it and
    applying it are the user's (a Needs-you item with its page), and applying settles that."""
    import shutil as sh

    from relay import repair
    env = echo_chat
    relay = env.app.state.relay
    relay.repairs.boxes = host_boxes  # (its tests' box: this machine)
    user = relay.db.one("SELECT id FROM users")["id"]
    source = env.tmp / "src"
    for package in ("relay", "opencode_manager"):
        sh.copytree(repair.SOURCE / package, source / package, ignore=sh.ignore_patterns("__pycache__"))
    monkeypatch.setattr(repair, "SOURCE", source)  # never the real one
    fixer = textwrap.dedent("""
        import json, sys
        sys.stdin.read()
        open("relay/providers/command/provider.py", "a").write("\\n# repaired\\n")
        print(json.dumps({"type": "text", "delta": "Fixed the layer, and repair_check passes."}), flush=True)
        print(json.dumps({"type": "done", "status": "succeeded"}), flush=True)
    """)
    env.post("/api/providers", json={"id": "fixer", "name": "Fixer", "kind": "command",
                                     "options": {"command": [sys.executable, "-c", fixer]}}, headers=H)
    conv = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()

    def runs(*outcomes):
        for status, error in outcomes:
            relay.db.execute("INSERT INTO runs (id, conversation_id, status, error, created, finished, provider_id) "
                             "VALUES (?, ?, ?, ?, ?, ?, 'fake')", (uuid_hex(), conv["id"], status, error, time.time(), time.time()))
            time.sleep(0.01)
    runs(("failed", "Usage limit reached"), ("failed", "unexpected output"), ("failed", "unexpected output"))
    assert "fake" not in repair.failing_providers(relay.db)  # (one of them was a limit)
    runs(("failed", "unexpected output: no events"))
    assert repair.failing_providers(relay.db)["fake"][0] == "unexpected output: no events"

    # Without AgentOS: the user is told, once, until it works again
    tick = lambda: env.portal.call(relay.scheduler.providers_failing, time.time())  # noqa: E731
    tick()
    tick()
    told = [i for i in relay.agentos.interventions(user) if "Fake agent failed its last 3 runs" in i["text"]]
    assert len(told) == 1 and told[0]["link"] == "/providers/repair/new?provider=fake"
    runs(("succeeded", None))
    tick()
    runs(("failed", "bad json"), ("failed", "bad json"), ("failed", "bad json"))
    tick()
    assert len([i for i in relay.agentos.interventions(user) if "Fake agent failed" in i["text"]]) == 2

    # With AgentOS: its Governor is woken to look, and have it repaired
    agentos_on(env)
    runs(("succeeded", None))
    tick()
    woken, wake = [], relay.agentos.wake

    async def woke(user_id, due, now, why, move_on=True):
        woken.append(why)
        return "run"
    monkeypatch.setattr(relay.agentos, "wake", woke)  # (not monkeypatch.undo(): that would undo the env's too)
    runs(("failed", "bad json"), ("failed", "bad json"), ("failed", "bad json"))
    tick()
    assert len(woken) == 1 and "provider.repair provider=fake" in woken[0] and "bad json" in woken[0]
    # A lost login: the user's to sign in again, told so; not the Governor's, and no repair
    runs(("succeeded", None))
    tick()
    runs(*[("failed", "Failed to authenticate: OAuth session expired and could not be refreshed")] * 3)
    tick()
    assert len(woken) == 1
    [lost] = [i for i in relay.agentos.interventions(user) if "isn't signed in any more" in i["text"]]
    assert "OAuth session expired" in lost["text"] and "Sign it in again on this machine" in lost["text"]
    assert lost["link"] == "/providers/repair/new?provider=fake"
    assert env.get("/api/providers/fake/trouble").json()["login_lost"]
    monkeypatch.setattr(relay.agentos, "wake", wake)

    monkeypatch.setattr(repair, "shadow_test", lambda *args, **kwargs: {"ok": True, "results": [], "mounts": []})
    from relay import creations
    monkeypatch.setattr(creations, "SOURCE", source)
    monkeypatch.setattr(relay.creations, "_start_trial", lambda creation: None)  # (its trial: the page's test)
    governor, run_id, tool = governor_lingering(env, relay)
    try:
        assert tool("agentos_provider_repair", provider="fake", helper="fake")[1]  # (not by itself)
        text, error = tool("agentos_provider_repair", provider="fake", helper="fixer", message="bad json")
        assert not error and "the user's, on its page" in text, text
        rid = text.split("Started repair ")[1].split(" ")[0]
        chat = relay.db.one("SELECT * FROM conversations WHERE id = (SELECT conversation_id FROM repairs WHERE id = ?)",
                            (rid,))
        assert chat["managed"] and chat["title"] == "Repair: Fake agent" and chat["agent_mode"] == "plan"
        assert "bad json" in (pathlib.Path(relay.repairs.get(user, rid)["folder"]) / "REPAIR.md").read_text()
        [asked] = [i for i in relay.agentos.interventions(user) if i["repair_id"] == rid]
        assert asked["link"] == f"/providers/repair/{rid}" and not asked["resolved"] and "on its page" in asked["text"]
        wait_idle(env, chat["id"], timeout=30)
        assert any(n["conversation"] == chat["id"] and n["kind"] == "done"
                   for n in relay.agentos.news.get(user, [])), relay.agentos.news  # (it's woken when it's done)
        # One repair of a provider at a time: the user's 🔧 opens the same page
        assert "being repaired already" in tool("agentos_provider_repair", provider="fake", helper="fixer")[0]
        assert env.post("/api/provider-repairs", json={"provider_id": "fake"}, headers=H).json()["id"] == rid
        assert "agentos_provider_review" not in {t["name"] for t in mcp(env, relay.db.one(
            "SELECT tools_token FROM conversations WHERE id = ?", (governor["id"],))["tools_token"],
            "tools/list")[1]["result"]["tools"]}
    finally:
        let_go(env, governor)
    # Its plan, approved; checked by Relay; applying it is the user's, and settles the Governor's notice
    relay.creations.plan(relay.repairs.get(user, rid), "", "", "Fix the parser.")
    env.post(f"/api/provider-repairs/{rid}/approve", json={}, headers=H)
    deadline = time.time() + 30
    while env.get(f"/api/provider-repairs/{rid}").json()["status"] != "trying" and time.time() < deadline:
        time.sleep(0.1)
    assert env.post(f"/api/provider-repairs/{rid}/apply", json={}, headers=H).json()["status"] == "applied"
    assert (source / "relay" / "providers" / "command" / "provider.py").read_text().endswith("# repaired\n")
    assert [i for i in relay.agentos.interventions(user) if i["repair_id"] == rid][0]["resolved_by"] == "user"
    # Relay takes a repair's changes on its page: its conversation isn't one to review in Tasks (it stayed there)
    assert chat["id"] not in {t["id"] for t in env.get("/api/tasks").json()["review"]}
    assert env.get(f"/api/conversations/{chat['id']}").json()["changes"] is None



def test_the_governor_adds_providers_relay_can_box_and_sends_the_user_to_add_others(env, monkeypatch):
    """provider.list; provider.add, only of a kind whose program runs in a box of Relay's (or a chat API), without a
    secret of the user's, with a box that mounts nothing it may not; provider.remove, only what it added;
    provider.check, Relay's whole check as Settings runs it; provider.create, for a program Relay doesn't know,
    sends the user to add it with an agent (installing it and logging in are theirs): a Needs-you item with its
    page."""
    from relay.providers import conformance
    signed_in(env)
    relay = env.app.state.relay
    monkeypatch.setenv("HOME", str(env.tmp))
    agentos_on(env)
    user = relay.db.one("SELECT id FROM users")["id"]
    checked = []
    monkeypatch.setattr(conformance, "check", lambda provider, model, scratch, place=None, discard=None: checked.append(
        (provider.template.id, place)) or [{"name": "Loads", "ok": True, "detail": "kind command"},
                                           {"name": "A turn", "ok": False, "detail": "no text"}])
    governor, run_id, tool = governor_lingering(env, relay)
    try:
        listed, error = tool("agentos_provider_list")
        assert not error and "- fake (command): Fake agent; runs on this machine outside a box" in listed, listed
        assert "- openai-compatible:" in listed and "- command:" not in listed and "- acp:" not in listed
        theirs = next(line for line in listed.splitlines() if line.startswith("Kinds that are the user's"))
        assert "command (" in theirs and "acp (" in theirs and "copilot" not in theirs, theirs
        # Not its to add: a program that runs on the machine, a secret, a box that mounts what it may not
        for kind, options, said in (("command", {"command": ["sh"]}, "outside a box"),
                                    ("acp", {"command": ["gemini", "--experimental-acp"]}, "outside a box"),
                                    ("openai-compatible", {"base_url": "http://127.0.0.1:9/v1", "api_key": "sk-1"},
                                     "secret"),
                                    ("claude-code", {"config_dir": str(env.tmp / ".ssh")}, "may not"),
                                    ("nonsense", {}, "no kind")):
            text, error = tool("agentos_provider_add", kind=kind, id="mine", options=options)
            assert error and said in text, (kind, text)
        assert "mine" not in {t.id for t in relay.registry.templates()}
        # A chat API it may: marked as its own, which it may remove (not the user's)
        text, error = tool("agentos_provider_add", kind="openai-compatible", id="local-chat", name="Local chat",
                           options={"base_url": "http://127.0.0.1:9/v1", "models": ["m"]})
        assert not error and "Added provider local-chat" in text and "user's to fill in" in text, text
        assert relay.registry.get("local-chat").managed
        assert "- local-chat (openai-compatible): Local chat; you added it" in tool("agentos_provider_list")[0]
        assert "the user's" in tool("agentos_provider_remove", id="fake")[0]
        assert tool("agentos_provider_remove", id="local-chat") == ("Removed provider local-chat.", False)
        assert "local-chat" not in {t.id for t in relay.registry.templates()}
        # Relay's whole check of one, as Settings' shield runs it
        text, error = tool("agentos_provider_check", id="fake")
        assert not error and "fake: not yet." in text and "✗ A turn: no text" in text, text
        assert checked == [("fake", relay.runs.place_check)]
        # A program Relay doesn't know: the user is sent to add it with an agent, with what's known of it
        text, error = tool("agentos_provider_create")
        assert error and "Say what the program is" in text
        text, error = tool("agentos_provider_create", program="Gemini CLI", message="npm @google/gemini-cli")
        assert not error and "The user is asked to add it with an agent" in text, text
        [asked] = [i for i in relay.agentos.interventions(user) if i["link"]]
        assert asked["link"].startswith("/providers/new?about=Gemini+CLI") and "Gemini CLI" in asked["text"]
        assert not relay.db.all("SELECT 1 FROM repairs WHERE creates = 1")  # (nothing started: it's the user's)
    finally:
        let_go(env, governor)


def uuid_hex():
    import uuid
    return uuid.uuid4().hex


def test_a_code_task_whose_folder_is_gone_says_so(env):
    signed_in(env)
    project = env.tmp / "ws" / "gone"
    project.mkdir()
    conv = env.post("/api/conversations", json={"provider_id": "fake", "mode": "code", "directory": str(project),
                                                "isolate": False}, headers=H).json()
    project.rmdir()
    r = env.post(f"/api/conversations/{conv['id']}/messages", json={"text": "hi"}, headers=H)
    assert r.status_code == 400 and "is gone" in r.json()["error"] and not project.exists()
    assert env.post("/api/schedules", json={"name": "x", "prompt": "y", "provider_id": "fake", "mode": "code",
                                            "directory": "/etc", "when": {"kind": "daily", "time": "08:00"}},
                    headers=H).status_code == 400


def test_a_read_only_agents_subagents_are_read_only_too(echo_chat):
    env = echo_chat
    relay = env.app.state.relay
    sleeper = ("import json, time\nprint(json.dumps({'type': 'session', 'session': 's1'}), flush=True)\n"
               "time.sleep(60)\n")
    env.post("/api/providers", json={"id": "sleeper", "name": "Sleeper", "kind": "command",
                                     "options": {"command": [sys.executable, "-c", sleeper]}}, headers=H)
    conv = env.post("/api/conversations", json={"provider_id": "sleeper", "agent_mode": "plan"}, headers=H).json()
    run_id = env.post(f"/api/conversations/{conv['id']}/messages", json={"text": "go"}, headers=H).json()["run_id"]
    token = relay.db.one("SELECT tools_token FROM conversations WHERE id = ?", (conv["id"],))["tools_token"]
    for _ in range(100):
        if relay.delegation.run_for(token):
            break
        time.sleep(0.05)
    started = mcp(env, token, "tools/call", {"name": "delegate", "arguments": {"task": "change x", "provider": "echo"}})
    assert not started[1]["result"]["isError"], started
    child = relay.db.one("SELECT agent_mode FROM conversations WHERE parent_run = ?", (run_id,))
    assert child["agent_mode"] == "readonly"  # read-only, without Plan's job of making a plan
    env.post(f"/api/runs/{run_id}/stop", headers=H)
    wait_idle(env, conv["id"])


def test_a_mode_switch_is_told_to_a_going_session(env):
    signed_in(env)
    conv = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    run_turn(env, conv["id"], "first")
    assert "switched" not in answers(env, conv["id"])[-1]
    env.patch(f"/api/conversations/{conv['id']}", json={"agent_mode": "plan"}, headers=H)
    run_turn(env, conv["id"], "second")
    assert "(I switched from the Build mode to the Plan mode:" in answers(env, conv["id"])[-1]
    run_turn(env, conv["id"], "third")
    assert "switched" not in answers(env, conv["id"])[-1]


def test_a_chat_folder_inside_a_repository_gets_its_own(env, tmp_path):
    import subprocess
    signed_in(env)
    relay = env.app.state.relay
    outer = tmp_path / "relay-checkout"
    (outer / "data").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(outer)], check=True)
    relay.settings.chat_root = str(outer / "data" / "chats")  # like Relay's own data folder in its checkout
    conv = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    run_turn(env, conv["id"], "hi")
    folder = pathlib.Path(conv["directory"])
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=folder, capture_output=True, text=True).stdout
    assert pathlib.Path(top.strip()).resolve() == folder.resolve()
    relay.settings.chat_root = str(tmp_path / "plain-chats")
    other = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    run_turn(env, other["id"], "hi")
    assert not (pathlib.Path(other["directory"]) / ".git").exists()  # not inside a repository: nothing added


def test_unused_tools_scripts_are_pruned(env):
    import os
    signed_in(env)
    relay = env.app.state.relay
    conv = env.post("/api/conversations", json={"provider_id": "fake"}, headers=H).json()
    run_turn(env, conv["id"], "hi")
    row = relay.db.one("SELECT * FROM conversations WHERE id = ?", (conv["id"],))
    mine = relay.delegation.script_for(row, relay.runs.local_url).parent
    from relay.tools_script import tools_dir
    stray, recent = tools_dir() / "stray-token", tools_dir() / "someone-elses"
    stray.mkdir()
    recent.mkdir()
    old = time.time() - 30 * 86400
    os.utime(stray, (old, old))
    os.utime(mine, (old, old))  # an old conversation of this Relay: kept
    assert relay.delegation.prune_scripts() == 1
    assert not stray.exists() and recent.exists() and mine.exists()


def test_what_relay_answers_itself(env, monkeypatch):
    signed_in(env)
    relay = env.app.state.relay
    # A permission prompt is answered by the provider's judge; it never reaches the user
    from relay.providers.command import CommandProvider
    verdicts = iter([("allow", ""), ("deny", "Only files in the project folder can be changed.")])
    monkeypatch.setattr(CommandProvider, "judge", lambda self, *args: next(verdicts))
    sleeper = ("import json, time\nprint(json.dumps({'type': 'session', 'session': 's1'}), flush=True)\ntime.sleep(60)\n")
    env.post("/api/providers", json={"id": "sleeper", "name": "Sleeper", "kind": "command",
                                     "options": {"command": [sys.executable, "-c", sleeper]}}, headers=H)
    busy = env.post("/api/conversations", json={"provider_id": "sleeper"}, headers=H).json()
    run_id = env.post(f"/api/conversations/{busy['id']}/messages", json={"text": "go"}, headers=H).json()["run_id"]
    token = next(t for t, r in relay.delegation.run_tokens.items() if r == run_id)

    def approve(tool, command):
        result = mcp(env, token, "tools/call", {"name": "approve_permission", "arguments": {
            "tool_name": tool, "input": {"command": command}, "tool_use_id": "t"}})[1]["result"]
        return json.loads(result["content"][0]["text"])

    assert approve("Bash", "ls")["behavior"] == "allow"
    denied = approve("Write", "x")
    assert denied["behavior"] == "deny" and "project folder" in denied["message"]
    assert not relay.runs.active[run_id].pending  # nobody was asked
    env.post(f"/api/runs/{run_id}/stop", headers=H)
    wait_idle(env, busy["id"])
    notes = [e for e in env.get(f"/api/conversations/{busy['id']}").json()["messages"][-1]["events"]
             if e["type"] == "permission"]
    assert notes[-1]["decision"] == "denied" and "project folder" in notes[-1]["outcome"]


def test_relays_tools_as_mcp_tools_and_delegating_in_one_call(echo_chat):
    env = echo_chat
    relay = env.app.state.relay
    sleeper = "import json, time\nprint(json.dumps({'type': 'session', 'session': 's1'}), flush=True)\ntime.sleep(60)\n"
    env.post("/api/providers", json={"id": "sleeper", "name": "Sleeper", "kind": "command",
                                     "options": {"command": [sys.executable, "-c", sleeper]}}, headers=H)
    conv = env.post("/api/conversations", json={"provider_id": "sleeper"}, headers=H).json()
    run_id = env.post(f"/api/conversations/{conv['id']}/messages", json={"text": "go"}, headers=H).json()["run_id"]
    token = next(t for t, r in relay.delegation.run_tokens.items() if r == run_id)
    listed = [t["name"] for t in mcp(env, token, "tools/list")[1]["result"]["tools"]]
    assert listed == ["approve_permission", "search", "fetch", "agents", "delegate", "wait", "tell",  # computers only when
                      "memory_add", "memory_change", "memory_forget", "memory_list"]  # there are some; memory when on
    answer = mcp(env, token, "tools/call", {"name": "delegate", "arguments": {
        "task": "hello sub", "provider": "echo", "wait": True}})[1]["result"]
    assert not answer["isError"] and "1 messages, last: hello sub" in answer["content"][0]["text"]
    env.post(f"/api/runs/{run_id}/stop", headers=H)
    wait_idle(env, conv["id"])


def test_every_agent_gets_relays_web_search_and_fetch_subagents_too(echo_chat):
    from types import SimpleNamespace
    env = echo_chat
    relay = env.app.state.relay
    sleeper = "import json, time\nprint(json.dumps({'type': 'session', 'session': 's1'}), flush=True)\ntime.sleep(60)\n"
    env.post("/api/providers", json={"id": "sleeper", "name": "Sleeper", "kind": "command",
                                     "options": {"command": [sys.executable, "-c", sleeper]}}, headers=H)
    conv = env.post("/api/conversations", json={"provider_id": "sleeper"}, headers=H).json()
    run_id = env.post(f"/api/conversations/{conv['id']}/messages", json={"text": "go"}, headers=H).json()["run_id"]
    token = next(t for t, r in relay.delegation.run_tokens.items() if r == run_id)
    relay.search.text = lambda arguments: f"1. Found {arguments['query']}"
    answer = mcp(env, token, "tools/call", {"name": "search", "arguments": {"query": "relay"}})[1]["result"]
    assert not answer["isError"] and answer["content"][0]["text"] == "1. Found relay"
    relay.fetch.text = lambda arguments: f"A page at {arguments['url']}"
    answer = mcp(env, token, "tools/call", {"name": "fetch", "arguments": {"url": "https://a.example/"}})[1]["result"]
    assert not answer["isError"] and answer["content"][0]["text"] == "A page at https://a.example/"
    # A subagent with a shell (a command agent): of Relay's tools only the search, as `relay search`
    from relay import policy
    env.put("/api/subagents", json={**policy.DEFAULT, "allowed": [{"provider": "fake", "model": ""}]}, headers=H)
    answer = mcp(env, token, "tools/call", {"name": "delegate", "arguments": {
        "task": "mcp?", "provider": "fake", "wait": True}})[1]["result"]
    told = answer["content"][0]["text"]
    assert "mcp=no tools=yes" in told, told  # (its script: RELAY_TOOLS; no other Relay tools)
    sub = relay.db.one("SELECT id FROM conversations WHERE parent_run = ?", (run_id,))["id"]
    assert [t["name"] for t in relay.delegation._tools_of(SimpleNamespace(conversation_id=sub))] == [
        "approve_permission", "search", "fetch"]
    assert relay.delegation.allowed(SimpleNamespace(conversation_id=sub)) == {"approve_permission", "search", "fetch"}
    # Search off: nobody gets it; the fetch stays
    relay.settings.search = {"enabled": False}
    listed = [t["name"] for t in mcp(env, token, "tools/list")[1]["result"]["tools"]]
    assert "search" not in listed and "fetch" in listed
    env.post(f"/api/runs/{run_id}/stop", headers=H)
    wait_idle(env, conv["id"])


def test_an_agent_sees_and_restores_its_turns_and_commits_only_when_asked(env):
    signed_in(env)
    relay = env.app.state.relay
    repo = git_repo(env.tmp / "ws" / "tools")
    conv = env.post("/api/conversations", json={"provider_id": "fake", "mode": "code",
                                                "directory": str(repo), "isolate": False}, headers=H).json()
    run_turn(env, conv["id"], "write one")
    run_turn(env, conv["id"], "write two")
    run_id = env.post(f"/api/conversations/{conv['id']}/messages", json={"text": "slow slow"},
                      headers=H).json()["run_id"]
    token = relay.runs.active[run_id].mcp_token

    def tool(name, **arguments):
        result = mcp(env, token, "tools/call", {"name": name, "arguments": arguments})[1]["result"]
        return result["content"][0]["text"], result.get("isError", False)

    listed = [t["name"] for t in mcp(env, token, "tools/list")[1]["result"]["tools"]]
    assert {"changes", "diff", "restore", "commit"} <= set(listed)
    turns, _ = tool("changes")
    assert "1. " in turns and "“write one”" in turns and "“write two”" in turns and "Since the task started" in turns
    # The turn going on, once it has a checkpoint: no answer yet, its prompt all the same
    relay.db.execute("UPDATE runs SET checkpoint = 'going' WHERE id = ?", (run_id,))
    assert "3. " in (going := tool("changes")[0]) and "“slow slow”: going on now" in going, going
    relay.db.execute("UPDATE runs SET checkpoint = NULL WHERE id = ?", (run_id,))
    diff, error = tool("diff", turn="2")
    assert not error and "+write two" in diff and "+write one" not in diff
    assert "+write one" in tool("diff", turn="all")[0]
    assert tool("diff", turn="9")[1]  # no such turn
    restored, error = tool("restore", turn="2")
    assert not error and (repo / "agent.txt").read_text() == "write one\n" and "To undo this: restore " in restored
    undo = restored.rsplit("restore ", 1)[1].rstrip(".")
    tool("restore", turn=undo)
    assert (repo / "agent.txt").read_text() == "write one\nwrite two\n"
    assert git_log(repo) == ["start"]
    committed, error = tool("commit", message="Agent's work, as asked")
    assert not error and git_log(repo) == ["Agent's work, as asked", "start"]
    env.post(f"/api/runs/{run_id}/stop", headers=H)
    wait_idle(env, conv["id"])


def test_rewind_waits_for_other_chats_writing_the_same_project(env):
    signed_in(env)
    folder = env.tmp / "ws" / "project"
    data = {"provider_id": "fake", "mode": "code", "directory": str(folder)}
    first = env.post("/api/conversations", json=data, headers=H).json()
    second = env.post("/api/conversations", json=data, headers=H).json()
    run_turn(env, first["id"], "write first")
    answer = env.get(f"/api/conversations/{first['id']}").json()["messages"][1]
    run_id = env.post(f"/api/conversations/{second['id']}/messages", json={"text": "linger"}, headers=H).json()["run_id"]
    try:
        before = (folder / "agent.txt").read_text()
        result = env.post(f"/api/conversations/{first['id']}/rewind", json={"run_id": answer["run_id"]}, headers=H)
        assert result.status_code == 409 and "Another agent" in result.json()["error"]
        assert f"in {folder}" in result.json()["error"]  # (where: one told needn't go looking)
        assert (folder / "agent.txt").read_text() == before
    finally:
        env.post(f"/api/runs/{run_id}/stop", headers=H)
        wait_idle(env, second["id"])


def test_a_fork_preserves_turn_checkpoints_mode_and_effort(env):
    signed_in(env)
    folder = env.tmp / "ws" / "project"
    conv = env.post("/api/conversations", json={"provider_id": "fake", "mode": "code", "directory": str(folder)},
                    headers=H).json()
    run_turn(env, conv["id"], "write first")
    run_turn(env, conv["id"], "write second")
    db = env.app.state.relay.db
    db.execute("UPDATE runs SET agent_mode = 'plan', effort = 'low' WHERE conversation_id = ?", (conv["id"],))
    fork = env.post(f"/api/conversations/{conv['id']}/fork", json={}, headers=H).json()
    fields = "checkpoint, after_checkpoint, agent_mode, effort, session"
    assert db.all(f"SELECT {fields} FROM runs WHERE conversation_id = ? ORDER BY created", (fork["id"],)) == \
        db.all(f"SELECT {fields} FROM runs WHERE conversation_id = ? ORDER BY created", (conv["id"],))
    answer = env.get(f"/api/conversations/{fork['id']}").json()["messages"][3]
    result = env.post(f"/api/conversations/{fork['id']}/rewind", json={"run_id": answer["run_id"]}, headers=H)
    assert result.status_code == 200, result.text
    assert (folder / "agent.txt").read_text() == "write first\n"


def test_new_chats_start_with_what_was_picked_last_on_any_device(env, monkeypatch):
    """The provider, model and effort picked for a new chat are the user's (PUT /api/new-chat): every browser's
    new chats start with them, given at the start (/api/state)."""
    signed_in(env)
    # Nothing picked yet: the first provider that's installed (not Claude Code's template without Claude Code)
    installed = set()
    monkeypatch.setattr("relay.routes.accounts.shutil.which", lambda name: f"/bin/{name}" if name in installed else None)
    assert [t.id for t in env.app.state.relay.registry.templates()][:2] == ["claude-code", "fake"]
    assert env.get("/api/state").json()["new_chat"] == {"provider_id": "fake", "model": None, "effort": None}
    installed.add("claude")
    assert env.get("/api/state").json()["new_chat"]["provider_id"] == "claude-code"
    installed.clear()
    chosen = env.put("/api/new-chat", json={"provider_id": "fake", "model": "m-two", "effort": "high"}, headers=H)
    assert chosen.json() == {"provider_id": "fake", "model": "m-two", "effort": "high"}
    assert env.get("/api/state").json()["new_chat"] == {"provider_id": "fake", "model": "m-two", "effort": "high"}
    env.put("/api/new-chat", json={"provider_id": "fake", "model": "", "effort": None}, headers=H)
    assert env.get("/api/state").json()["new_chat"] == {"provider_id": "fake", "model": None, "effort": None}
    for wrong in ({"provider_id": "fake", "model": 3}, {"model": "m"}, {"provider_id": ["fake"]}):
        assert env.put("/api/new-chat", json=wrong, headers=H).status_code == 400
