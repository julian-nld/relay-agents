"""A program printing uniform events (kind command): placeholders, resuming, stopping."""

import sys

from relay.providers import Context
from relay.providers.base import Template
from relay.providers.command import CommandProvider


def context(tmp_path, **kw):
    return Context(conversation_id="conv", directory=tmp_path, mode="chat",
                   model=kw.get("model"), provider_session=kw.get("session"),
                   history=kw.get("history", []))


def test_command_provider_placeholders_and_resume(tmp_path):
    script = tmp_path / "echo_args.py"
    script.write_text(
        "import json, sys\n"
        "print(json.dumps({'type': 'session', 'session': 's1'}))\n"
        "print(json.dumps({'type': 'text', 'delta': ' | '.join(sys.argv[1:]) + ' | ' + sys.stdin.read()}))\n"
        "print(json.dumps({'type': 'done', 'status': 'succeeded'}))\n")
    provider = CommandProvider(Template("x", "X", "command", True, {
        "command": [sys.executable, str(script), "new", "{model}", "{directory}"],
        "resume_command": [sys.executable, str(script), "resume", "{session}"],
    }))
    first = list(provider.start(context(tmp_path, model="m1"), "hello").events())
    assert first[1]["delta"] == f"new | m1 | {tmp_path} | hello"
    again = list(provider.start(context(tmp_path, session="s1"), "more").events())
    assert again[1]["delta"] == "resume | s1 | more"


def test_command_stderr_flood_does_not_deadlock(tmp_path):
    script = tmp_path / "noisy.py"
    script.write_text(
        "import json, sys\n"
        "sys.stderr.write('x' * 500_000)\n"  # far more than a pipe buffer
        "print(json.dumps({'type': 'text', 'delta': 'ok'}))\n"
        "print(json.dumps({'type': 'done', 'status': 'succeeded'}))\n")
    provider = CommandProvider(Template("n", "N", "command", True, {"command": [sys.executable, str(script)]}))
    events = list(provider.start(context(tmp_path), "hi").events())
    assert events[-1] == {"type": "done", "status": "succeeded"}


def test_placeholders_are_filled_in_one_pass(tmp_path):
    from relay.providers.command.provider import _fill
    values = {"{prompt}": "what does {directory} mean?", "{session}": "", "{model}": "",
              "{directory}": "/home/me/project"}
    assert _fill("{prompt}", values) == "what does {directory} mean?"
    assert _fill("{directory}/x", values) == "/home/me/project/x"


def test_command_stopped_before_start_never_runs(tmp_path):
    marker = tmp_path / "ran"
    provider = CommandProvider(Template("m", "M", "command", True, {
        "command": [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"]}))
    run = provider.start(context(tmp_path), "hi")
    run.interrupt()
    assert list(run.events()) == [{"type": "done", "status": "interrupted"}]
    assert not marker.exists()
