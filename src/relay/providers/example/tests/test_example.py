"""The guide's example layer (kind example): toy-agent's output as Relay's events, how it's started, and the whole
check against it. A layer of your own has tests like these in its folder (providers/<name>/tests)."""

import json
import subprocess
import sys
from types import SimpleNamespace

from relay.providers import KINDS, Context, parse_template
from relay.providers.conformance import check, validate_events
from relay.providers.example.provider import PROGRAM, ToyAgentProvider, translate


def toy(tmp_path, monkeypatch):
    monkeypatch.setattr("relay.config.data_dir", lambda: tmp_path / "data")
    return ToyAgentProvider(parse_template(ToyAgentProvider.EXAMPLE))


def test_it_is_found_but_not_offered_in_settings():
    assert KINDS["example"] is ToyAgentProvider
    assert ToyAgentProvider.describe()["listed"] is False and KINDS["command"].describe()["listed"] is True


def test_toy_agents_own_lines_become_relays_events():
    lines = [{"kind": "hello", "conversation": "c1", "model": "toy-small"}, {"kind": "think", "words": "Hm."},
             {"kind": "use", "call": "k", "tool": "run", "args": {"command": "ls"}},
             {"kind": "used", "call": "k", "result": "a.txt\n", "failed": False},
             {"kind": "say", "words": "Done."}, {"kind": "something new"}, {"kind": "bye", "outcome": "ok"}]
    events = [event for line in lines for event in translate(line)]
    assert [e["type"] for e in events] == ["session", "step", "reasoning", "tool_call", "tool_result", "text", "done"]
    assert validate_events(events) == []
    assert translate({"kind": "bye", "outcome": "error", "why": "no session x"}) == [
        {"type": "error", "message": "no session x"}, {"type": "done", "status": "failed"}]


def test_the_program_says_what_the_guide_says(tmp_path):
    """toy-agent itself, as the guide shows its output: a session, its answer, its bye; then that session again."""
    ask = lambda *args, prompt="Reply with just the word OK.": subprocess.run(  # noqa: E731
        [sys.executable, str(PROGRAM), *args], input=prompt, capture_output=True, text=True,
        env={"TOY_AGENT_HOME": str(tmp_path)})
    said = [json.loads(line) for line in ask().stdout.splitlines()]
    assert [s["kind"] for s in said] == ["hello", "think", "say", "bye"] and said[2]["words"] == "OK"
    again = [json.loads(line) for line in ask("--resume", said[0]["conversation"], prompt="Hi").stdout.splitlines()]
    assert again[2]["words"] == "Toy agent here, turn 2 of this session. You said: Hi"
    assert [json.loads(line) for line in ask("--resume", "nope").stdout.splitlines()] == [
        {"kind": "bye", "outcome": "error", "why": "no session nope"}]


def test_a_turn_starts_in_its_box_and_goes_on_in_its_session(tmp_path, monkeypatch):
    provider = toy(tmp_path, monkeypatch)
    assert provider.box_key() == "toy" and (PROGRAM.parent, False) in provider.box_mounts()
    wrapped = []
    box = SimpleNamespace(wrap=lambda argv, cwd, env: wrapped.append((argv, cwd, env)) or ["podman", "exec", *argv])
    context = Context(conversation_id="c", directory=tmp_path, mode="code", model="toy-large",
                      provider_session="abc", box=box)
    run = provider.start(context, "run: ls")
    argv, cwd, env = wrapped[0]
    assert argv == ["python3", str(PROGRAM), "--resume", "abc", "--model", "toy-large"] and cwd == tmp_path
    assert env == {"TOY_AGENT_HOME": str(tmp_path / "data" / "example" / "toy"), "RELAY_FOLDER": str(tmp_path)}
    assert run.prompt.endswith("run: ls")


def test_it_does_what_relay_needs_of_it(tmp_path, monkeypatch):
    """The whole check (Settings → Providers → Check it fully), here without boxes: each scenario passes."""
    results = check(toy(tmp_path, monkeypatch), "", tmp_path / "scratch")
    assert [(r["name"], r["ok"]) for r in results] == [
        ("Loads", True), ("check()", True), ("Models", True), ("A turn", True), ("Going on", True),
        ("Stopping", True), ("A command", True)], results
