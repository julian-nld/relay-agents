import json
import stat
import sys
import textwrap
import time

import pytest

from opencode_manager import cli, paths, scripting, state
from opencode_manager.bridge.claude_backend import ClaudeCodeBackend, Translator
from opencode_manager.settings import Settings


def msg(kind, **fields):
    return {"type": kind, "parent_tool_use_id": None, **fields}


def stream_event(ev):
    return msg("stream_event", event=ev)


RUN = [
    {"type": "system", "subtype": "init", "session_id": "s", "apiKeySource": "none"},
    stream_event({"type": "message_start", "message": {"model": "claude-x"}}),
    stream_event({"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "hm"}}),
    msg("assistant", message={"content": [
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]}),
    msg("user", message={"content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "a.txt", "is_error": False}]}),
    stream_event({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Done"}}),
    # A subagent's own work is not part of the main stream:
    {**stream_event({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "sub"}}),
     "parent_tool_use_id": "t9"},
    {"type": "result", "subtype": "success", "is_error": False, "usage": {"output_tokens": 3},
     "total_cost_usd": 0.01, "permission_denials": []},
]


def test_translator_maps_a_run():
    t = Translator(agent="reviewer")
    out = [e for raw in RUN for e in t.translate(raw)]
    assert out == [
        {"type": "step", "agent": "reviewer", "model": "claude-x"},
        {"type": "reasoning", "delta": "hm"},
        {"type": "tool_call", "id": "t1", "name": "Bash", "input": {"command": "ls"}},
        {"type": "tool_result", "id": "t1", "name": "Bash", "ok": True, "output": "a.txt"},
        {"type": "text", "delta": "Done"},
        {"type": "usage", "tokens": {"output_tokens": 3}, "cost": 0.01, "billing": "subscription"},
        {"type": "done", "status": "succeeded"},
    ]


def test_translator_tells_an_api_key_from_a_subscription():
    t = Translator()
    t.translate({"type": "system", "subtype": "init", "apiKeySource": "ANTHROPIC_API_KEY"})
    assert t.translate({"type": "result", "subtype": "success", "total_cost_usd": 0.2})[0]["billing"] == "api"
    assert started().translate({"type": "result", "subtype": "success"})[0]["billing"] is None


def started(**kwargs):
    """A translator past its run's init, as claude's output always starts."""
    t = Translator(**kwargs)
    t.translate({"type": "system", "subtype": "init"})
    return t


def test_a_result_before_the_runs_init_isnt_its_end():
    """A resumed session first reports what it left (a command it moved to the background), with a
    result of its own: taken for the end, Relay stopped reading the turn that followed."""
    t = Translator()
    assert t.translate({"type": "result", "subtype": "success", "total_cost_usd": 0.24, "usage": {}}) == []
    assert not t.done
    out = [e for raw in RUN for e in t.translate(raw)]
    assert t.done and out[-1] == {"type": "done", "status": "succeeded"} and {"type": "text", "delta": "Done"} in out


def test_translator_reports_failures_and_denials():
    t = started()
    out = t.translate({"type": "result", "subtype": "error_max_turns", "is_error": True,
                       "result": "Too many turns",
                       "permission_denials": [{"tool_use_id": "t2", "tool_name": "Bash",
                                               "tool_input": {"command": "rm x"}}]})
    assert out[1] == {"type": "permission", "id": "t2", "action": "Bash",
                      "resources": {"command": "rm x"}, "decision": "denied"}
    assert out[2] == {"type": "error", "message": "Too many turns"}
    assert out[3] == {"type": "done", "status": "failed"}


def test_a_model_claude_code_doesnt_know_says_what_to_do_in_relay():
    """Its advice is for its command line ("Run --model to pick a different model"); Relay's is its model menu."""
    out = started().translate({"type": "result", "subtype": "error_during_execution", "is_error": True,
                               "result": "There's an issue with the selected model (sonnet-5-5). It may not exist or "
                                         "you may not have access to it. Run --model to pick a different model."})
    error = next(e for e in out if e["type"] == "error")["message"]
    assert "'sonnet-5-5'" in error and "model menu: sonnet, opus or haiku" in error and "--model" not in error


def test_a_lost_login_says_where_to_sign_in_again_in_relay():
    """Its advice ("Please run /login") is for its own terminal; Relay's is the provider's Shell. Its own words stay:
    they say what happened, and Relay knows a lost login by them (relay.repair LOGIN_LOST)."""
    out = started().translate({"type": "result", "subtype": "error_during_execution", "is_error": True,
                               "result": "Failed to authenticate: OAuth session expired and could not be refreshed"})
    error = next(e for e in out if e["type"] == "error")["message"]
    assert error.startswith("Failed to authenticate: OAuth session expired and could not be refreshed. ")
    assert "Settings → Providers → Claude Code" in error and "`claude auth login`" in error
    from relay.repair import LOGIN_LOST
    assert LOGIN_LOST.search(error) and not LOGIN_LOST.search("There's an issue with the selected model (x).")

def test_command_line_for_new_and_resumed_sessions(tmp_path):
    new = ClaudeCodeBackend(tmp_path, resume=False, model="haiku", agent="rev").command("id1")
    assert new[:2] == ["claude", "-p"]
    assert new[new.index("--session-id") + 1] == "id1"
    assert "--resume" not in new
    assert new[new.index("--model") + 1] == "haiku" and new[new.index("--agent") + 1] == "rev"
    assert "--dangerously-skip-permissions" in new

    resumed = ClaudeCodeBackend(tmp_path, resume=True, auto_approve=False).command("id1")
    assert resumed[resumed.index("--resume") + 1] == "id1"
    assert "--dangerously-skip-permissions" not in resumed


@pytest.fixture
def fake_claude(tmp_path):
    """A stand-in `claude` that records how it was called and replays RUN."""
    log = tmp_path / "calls.jsonl"
    script = tmp_path / "claude"
    script.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import json, os, sys, time
        call = {{"argv": sys.argv[1:], "cwd": os.getcwd(), "stdin": sys.stdin.read()}}
        open({str(log)!r}, "a").write(json.dumps(call) + "\\n")
        if os.environ.get("FAKE_CLAUDE_HANG"):
            print({json.dumps(json.dumps(RUN[1]))}, flush=True)
            time.sleep(60)
        if os.environ.get("FAKE_CLAUDE_CRASH"):
            print("fatal: not logged in", file=sys.stderr)
            sys.exit(1)
        if os.environ.get("FAKE_CLAUDE_EARLY_ERROR"):  # (no init: a session it couldn't resume, say)
            print(json.dumps({{"type": "result", "subtype": "error_during_execution", "is_error": True,
                               "result": "No conversation found with session ID x"}}), flush=True)
            sys.exit(1)
        if os.environ.get("FAKE_CLAUDE_LEFTOVER"):  # (a resumed session's report on what it left, first)
            print(json.dumps({{"type": "result", "subtype": "success", "total_cost_usd": 0.24}}), flush=True)
        for line in {json.dumps([json.dumps(r) for r in RUN])}:
            print(line, flush=True)
        if os.environ.get("FAKE_CLAUDE_MORE"):  # (more than a pipe holds, after its result)
            print("x" * 300_000, flush=True)
        if os.environ.get("FAKE_CLAUDE_LINGER"):
            time.sleep(60)
        """))
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script, log


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.HOME_ENV, str(tmp_path / "data"))
    return tmp_path


def calls(log):
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_stream_new_then_continue_from_elsewhere(home, fake_claude, monkeypatch, capsys):
    script, log = fake_claude
    settings = Settings(claude_bin=str(script))
    monkeypatch.setattr(cli.settings_mod, "load", lambda: settings)
    project = home / "project"
    project.mkdir()

    assert cli.main(["stream", "-b", "claude-code", "-d", str(project), "-m", "haiku", "hi"]) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[0]["type"] == "session" and events[0]["new"] is True
    assert events[0]["directory"] == str(project)
    assert events[-1] == {"type": "done", "status": "succeeded"}
    session_id = events[0]["session"]

    monkeypatch.chdir(home)  # somewhere else: the session's directory is remembered
    assert cli.main(["stream", "-b", "claude-code", "-c", "--text", "again"]) == 0
    first, second = calls(log)
    assert first["argv"][first["argv"].index("--session-id") + 1] == session_id
    assert first["cwd"] == str(project) and first["stdin"] == "hi"
    assert second["argv"][second["argv"].index("--resume") + 1] == session_id
    assert second["cwd"] == str(project) and second["stdin"] == "again"
    assert capsys.readouterr().out == "Done\n"


def test_crash_without_result_is_reported(home, fake_claude, monkeypatch, capsys):
    script, _ = fake_claude
    monkeypatch.setenv("FAKE_CLAUDE_CRASH", "1")
    backend = ClaudeCodeBackend(home, resume=False, binary=str(script))
    events = list(backend.run("id", "hi"))
    assert events[-2] == {"type": "error", "message": "fatal: not logged in"}
    assert events[-1] == {"type": "done", "status": "failed"}


def test_a_run_ends_whatever_claude_does_after_its_result(home, fake_claude, monkeypatch):
    """Found for real: a resumed session's leftover result came first, the run stopped reading, and claude,
    with more to say, waited for ever (so did the run). A result before the run's init isn't its end;
    after the end what it says is read, and one that doesn't end is stopped."""
    from opencode_manager.bridge import claude_backend
    script, _ = fake_claude
    monkeypatch.setenv("FAKE_CLAUDE_LEFTOVER", "1")
    monkeypatch.setenv("FAKE_CLAUDE_MORE", "1")
    monkeypatch.setattr(claude_backend, "AFTER_RESULT", 600)  # (read to its end: not stopped after waiting)
    started = time.monotonic()
    events = list(ClaudeCodeBackend(home, resume=False, binary=str(script)).run("id", "hi"))
    assert {"type": "text", "delta": "Done"} in events and events[-1] == {"type": "done", "status": "succeeded"}
    assert time.monotonic() - started < 20
    monkeypatch.setenv("FAKE_CLAUDE_LINGER", "1")
    monkeypatch.setattr(claude_backend, "AFTER_RESULT", 0.5)
    backend = ClaudeCodeBackend(home, resume=False, binary=str(script))
    started = time.monotonic()
    assert list(backend.run("id", "hi"))[-1] == {"type": "done", "status": "succeeded"}
    assert time.monotonic() - started < 20 and backend.process.poll() is not None
    # An error before its init is said, not lost
    monkeypatch.delenv("FAKE_CLAUDE_LINGER")
    monkeypatch.setenv("FAKE_CLAUDE_EARLY_ERROR", "1")
    events = list(ClaudeCodeBackend(home, resume=True, binary=str(script)).run("id", "hi"))
    assert events[-2:] == [{"type": "error", "message": "No conversation found with session ID x"},
                           {"type": "done", "status": "failed"}]


def test_interrupt_stops_the_process(home, fake_claude, monkeypatch):
    script, _ = fake_claude
    monkeypatch.setenv("FAKE_CLAUDE_HANG", "1")
    backend = ClaudeCodeBackend(home, resume=False, binary=str(script))
    events = backend.run("id", "hi")
    assert next(events)["type"] == "step"
    backend.interrupt("id")
    assert backend.process.poll() is not None


def test_errors(home, monkeypatch):
    settings = Settings(claude_bin="/nonexistent/claude")
    with pytest.raises(scripting.ScriptError, match="Profiles apply"):
        scripting.start_stream(settings, None, "hi", backend="claude-code", profile_name="work")
    with pytest.raises(scripting.ScriptError, match="No previous Claude Code session"):
        scripting.start_stream(settings, None, "hi", backend="claude-code", continue_last=True)
    with pytest.raises(scripting.ScriptError, match="not found"):
        scripting.start_stream(settings, None, "hi", backend="claude-code", directory=home)


def test_session_directories_are_remembered(home):
    project = home / "proj"
    project.mkdir()
    state.remember_session("claude-code", "s1", directory=project)
    assert state.load().session_directories == {"claude-code:s1": str(project)}
    assert state.session_directory("claude-code:s1") == project


def test_an_answer_only_in_the_result_is_passed_on():
    # e.g. /context: nothing streamed, the output is in the result
    t = started()
    out = t.translate({"type": "result", "subtype": "success", "result": "## Context Usage"})
    assert out[0] == {"type": "text", "delta": "## Context Usage"}
    streamed = started()
    streamed.translate(stream_event({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi"}}))
    assert streamed.translate({"type": "result", "subtype": "success", "result": "Hi"})[0]["type"] == "usage"
