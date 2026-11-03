"""ACP agents (GitHub Copilot and others), against a fake agent that speaks the protocol."""

import sys
import textwrap
import threading

import pytest

from relay.modes import PLAN
from relay.providers import Context, parse_template
from relay.providers.acp import AcpProvider, CopilotProvider

FAKE_AGENT = textwrap.dedent('''
    import json, sys, uuid
    sessions = {}
    def send(m): print(json.dumps({"jsonrpc": "2.0", **m}), flush=True)
    def update(sid, u): send({"method": "session/update", "params": {"sessionId": sid, "update": u}})
    pending = {}
    for line in sys.stdin:
        m = json.loads(line)
        method, mid, p = m.get("method"), m.get("id"), m.get("params") or {}
        if method is None:  # an answer to our permission request
            sid, command, prompt_id = pending.pop(mid)
            outcome = m["result"]["outcome"]
            allowed = outcome.get("optionId") == "yes"
            update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "completed" if allowed else "failed",
                         "content": [{"type": "content", "content": {"type": "text", "text": "ran" if allowed else "refused"}}]})
            update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": f"{command}: {'allowed' if allowed else 'refused'}. "}})
            update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": f"I remember {len(sessions[sid])} prompts."}})
            send({"id": prompt_id, "result": {"stopReason": "end_turn", "usage": {"inputTokens": 7, "outputTokens": 3}}})
        elif method == "initialize":
            send({"id": mid, "result": {"protocolVersion": 1, "agentCapabilities": {"loadSession": True},
                                        "agentInfo": {"name": "fake", "title": "Fake ACP", "version": "0.1"}}})
        elif method == "session/new":
            sid = str(uuid.uuid4()); sessions[sid] = []
            send({"id": mid, "result": {"sessionId": sid, "modes": {"availableModes": [{"id": "agent"}, {"id": "plan"}]}}})
        elif method == "session/load":
            sid = p["sessionId"]
            sessions[sid] = ["earlier"] if sid.startswith("known") else None
            if sessions[sid] is None:
                send({"id": mid, "error": {"code": -32002, "message": "Session not found"}})
            else:
                update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "OLD HISTORY"}})
                send({"id": mid, "result": {}})
        elif method == "session/set_mode":
            send({"id": mid, "result": {}})
        elif method == "session/prompt":
            sid = p["sessionId"]; text = p["prompt"][0]["text"]; sessions[sid].append(text)
            command = "rm -rf /" if "danger" in text else "echo hi > x.txt"
            update(sid, {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "thinking"}})
            update(sid, {"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "Run", "kind": "execute",
                         "status": "pending", "rawInput": {"command": command}})
            pending[1000] = (sid, command, mid)
            send({"id": 1000, "method": "session/request_permission", "params": {"sessionId": sid,
                  "toolCall": {"toolCallId": "t1", "kind": "execute", "rawInput": {"commands": [command]}},
                  "options": [{"optionId": "yes", "kind": "allow_once", "name": "Allow"},
                              {"optionId": "no", "kind": "reject_once", "name": "Deny"}]}})
''')


def provider(tmp_path, **options):
    script = tmp_path / "fake_acp.py"
    script.write_text(FAKE_AGENT)
    return AcpProvider(parse_template({"id": "fake", "name": "Fake", "kind": "acp",
                                       "options": {"command": [sys.executable, str(script)], **options}}))


def run(p, tmp_path, prompt, session=None, mode=None, answer=None):
    context = Context("c1", tmp_path, "code", None, session, agent_mode=mode)
    turn = p.start(context, prompt)
    events = []
    for event in turn.events():
        events.append(event)
        if event["type"] == "permission" and event["decision"] is None:
            threading.Thread(target=turn.answer_permission, args=(event["id"], answer)).start()
    return events


def text(events):
    return "".join(e["delta"] for e in events if e["type"] == "text")


def test_a_turn_with_a_tool_call(tmp_path):
    p = provider(tmp_path)
    assert p.check().startswith("Fake ACP 0.1 works")  # it answers, and opens a session
    events = run(p, tmp_path, "hello")
    assert events[0]["type"] == "session" and events[-1] == {"type": "done", "status": "succeeded"}
    kinds = [e["type"] for e in events]
    assert kinds.index("tool_call") < kinds.index("permission") < kinds.index("tool_result")
    assert next(e for e in events if e["type"] == "permission")["decision"] == "once"
    assert text(events) == "echo hi > x.txt: allowed. I remember 1 prompts."
    assert next(e for e in events if e["type"] == "reasoning")["delta"] == "thinking"
    usage = next(e for e in events if e["type"] == "usage")
    assert usage["tokens"] == {"input_tokens": 7, "output_tokens": 3}


def test_sessions_go_on_and_their_history_isnt_repeated(tmp_path):
    p = provider(tmp_path)
    events = run(p, tmp_path, "again", session="known-1")
    assert events[0]["session"] == "known-1" and "OLD HISTORY" not in text(events)
    assert text(events).endswith("I remember 2 prompts.")
    gone = run(p, tmp_path, "again", session="lost-1")  # can't be loaded: a new one
    assert gone[0]["session"] != "lost-1" and gone[-1]["status"] == "succeeded"


def test_relay_answers_its_requests(tmp_path):
    p = provider(tmp_path)
    blocked = run(p, tmp_path, "danger")
    note = next(e for e in blocked if e["type"] == "permission")
    assert note["decision"] == "denied" and "Relay blocked this command" in note["outcome"]
    assert "rm -rf /: refused" in text(blocked)
    # A read-only mode only asks the agent not to change anything (its commands run in a container).
    assert "allowed" in text(run(p, tmp_path, "hello", mode=PLAN))
    assert p.judge("edit", {"paths": ["src/a.py"]}, tmp_path) == ("allow", "")
    assert p.judge("edit", {"paths": ["/etc/passwd"]}, tmp_path)[0] == "deny"
    assert p.judge("read", {"paths": ["/etc/passwd"]}, tmp_path) == ("allow", "")


def test_copilots_command_line():
    p = CopilotProvider(parse_template({"id": "copilot", "name": "Copilot", "kind": "copilot",
                                        "options": {"binary": "/opt/copilot"}}))
    context = Context("c", None, "code", "claude-sonnet-4.5", None, effort="high")
    assert p.argv(context) == ["/opt/copilot", "--acp", "--no-auto-update", "--model", "claude-sonnet-4.5",
                               "--reasoning-effort", "high"]
    assert p.billing(None) == "subscription"


def test_a_title_that_is_just_the_start_of_the_message_is_no_title():
    from relay.providers.acp.provider import _starts
    sent = "A note before my message:\n\nRelay, the app this conversation runs in, adds tools"
    assert _starts(sent, "A note before my message: Relay, the app this conversation runs in…")
    assert not _starts(sent, "Feedback on the environment")


def test_copilot_checks_its_env():
    from relay.providers.base import TemplateError
    with pytest.raises(TemplateError):
        CopilotProvider(parse_template({**CopilotProvider.EXAMPLE, "options": {"env": {"BAD": 1}}}))
