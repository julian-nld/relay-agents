"""Claude Code (kind claude-code): sessions, forks, modes, Relay's tools and its permission requests."""

import json

from relay.providers import Context, parse_template
from relay.providers.claude import ClaudeCodeProvider


def context(tmp_path, **kw):
    return Context(conversation_id="conv", directory=tmp_path, mode="chat",
                   model=kw.get("model"), provider_session=kw.get("session"),
                   history=kw.get("history", []))


def test_claude_code_new_and_resumed_sessions(tmp_path, monkeypatch):
    import re
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name)
    config = tmp_path / "claude-config"
    provider = ClaudeCodeProvider(parse_template({**ClaudeCodeProvider.EXAMPLE,
                                                  "options": {"config_dir": str(config)}}))
    new = provider.start(context(tmp_path, model="haiku"), "hi")
    assert new.new and "--session-id" in new.backend.command(new.session_id)
    assert new.backend.env["CLAUDE_CONFIG_DIR"] == str(config)
    # Claude Code keeps sessions per folder, named after its path
    sessions = config / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(tmp_path))
    sessions.mkdir(parents=True)
    (sessions / "abc.jsonl").write_text("{}")
    resumed = provider.start(context(tmp_path, session="abc"), "hi")
    command = resumed.backend.command("abc")
    assert not resumed.new and command[command.index("--resume") + 1] == "abc"
    assert "--session-id" not in command
    # A session that's gone (cleaned up, or the folder moved): a fresh one, with a handover
    gone = provider.start(context(tmp_path, session="old", history=[{"role": "user", "content": "earlier"}]), "hi")
    assert gone.new and gone.session_id != "old" and "User: earlier" in gone.prompt


def test_agents_get_attachment_paths(tmp_path, monkeypatch):
    from relay.uploads import Attachment
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name)
    ctx = context(tmp_path)
    ctx.attachments = [Attachment("1", "a.png", "image/png", 3, tmp_path / "up" / "a.png")]
    provider = ClaudeCodeProvider(parse_template(ClaudeCodeProvider.EXAMPLE))
    run = provider.start(ctx, "look")
    assert str(tmp_path / "up" / "a.png") in run.prompt
    command = run.backend.command(run.session_id)
    assert command[command.index("--add-dir") + 1] == str(tmp_path / "up")


def test_claude_code_forks_or_gets_a_handover(monkeypatch, tmp_path):
    from relay.providers.claude import ClaudeCodeProvider

    monkeypatch.setenv("RELAY_HOME", str(tmp_path / "data"))
    monkeypatch.setattr("relay.providers.claude.provider.shutil.which", lambda name: "/usr/bin/claude")
    provider = ClaudeCodeProvider(parse_template(ClaudeCodeProvider.EXAMPLE))
    history = [{"role": "user", "content": "remember kiwi"}, {"role": "assistant", "content": "ok"}]

    ctx = Context(conversation_id="c", directory=tmp_path, mode="chat", model=None,
                  provider_session=None, history=history, fork_from="old-session")
    run = provider.start(ctx, "what fruit?")
    argv = run.backend.command(run.session_id)
    assert argv[argv.index("--resume") + 1] == "old-session" and "--fork-session" in argv
    assert argv[argv.index("--session-id") + 1] == run.session_id != "old-session"
    assert run.prompt == "what fruit?"  # the copied session has the conversation already

    ctx.fork_from = None  # e.g. after switching to Claude Code: hand the conversation over
    run = provider.start(ctx, "what fruit?")
    assert "User: remember kiwi" in run.prompt and "--fork-session" not in run.backend.command(run.session_id)


def test_modes_for_claude_code(monkeypatch, tmp_path):
    from relay.modes import PLAN, Mode
    from relay.providers.claude import ClaudeCodeProvider

    monkeypatch.setattr("relay.providers.claude.provider.shutil.which", lambda name: "/usr/bin/claude")
    provider = ClaudeCodeProvider(parse_template(ClaudeCodeProvider.EXAMPLE))

    def argv(mode):
        ctx = Context(conversation_id="c", directory=tmp_path, mode="code", model=None,
                      provider_session="s", agent_mode=mode)
        run = provider.start(ctx, "go")
        return run.backend.command(run.session_id)

    # A mode is what the agent is told: read-only modes ask it not to change anything (its
    # commands run in a container either way), not Claude Code's plan mode (which forbids even reading).
    assert "--dangerously-skip-permissions" in argv(None)
    plan = argv(PLAN)
    assert "--dangerously-skip-permissions" in plan and "--permission-mode" not in plan
    critic = argv(Mode("critic", "Critic", instructions="Be harsh.", access="read"))
    told = critic[critic.index("--append-system-prompt") + 1]
    assert told.startswith("Be harsh.") and "This is a read-only mode: don't change anything." in told
    writer = argv(Mode("writer", "Writer", instructions="Write tests first."))
    assert "--dangerously-skip-permissions" in writer and "read-only" not in " ".join(writer)


def test_claude_code_gets_relays_tools_when_offered(monkeypatch, tmp_path):
    from relay.modes import PLAN
    from relay.providers.claude import ClaudeCodeProvider
    from relay.tools_script import note

    monkeypatch.setattr("relay.providers.claude.provider.shutil.which", lambda name: "/usr/bin/claude")
    provider = ClaudeCodeProvider(parse_template(ClaudeCodeProvider.EXAMPLE))
    script = tmp_path / "tools" / "abc" / "relay"
    ctx = Context(conversation_id="c", directory=tmp_path, mode="chat", model=None, provider_session="s",
                  mcp_url="http://127.0.0.1:8420/mcp/secret", tools_path=script, tools_note=note(script),
                  agent_mode=PLAN)
    run = provider.start(ctx, "go")
    argv = run.backend.command(run.session_id)
    # On top of Claude Code's own tools and MCP servers: allowed even in Plan mode
    assert argv[argv.index("--allowedTools") + 1] == f"Bash({script}:*),Bash(relay:*),mcp__relay"
    assert run.backend.env["RELAY_TOOLS"] == str(script)  # which script `relay` in its container runs
    assert json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"]["relay"]["url"].endswith("/mcp/secret")
    assert "--strict-mcp-config" not in argv  # its own MCP servers stay
    # In a read-only mode what needs permission goes to Relay, not refused unasked
    assert argv[argv.index("--permission-prompt-tool") + 1] == "mcp__relay__approve_permission"
    assert argv[argv.index("--permission-mode") + 1] == "default"
    assert run.prompt == "go"  # the note is in the system prompt, not the message


def test_claude_codes_requests_go_to_relay(monkeypatch, tmp_path):
    from relay.providers.claude import ClaudeCodeProvider

    monkeypatch.setattr("relay.providers.claude.provider.shutil.which", lambda name: "/usr/bin/claude")
    provider = ClaudeCodeProvider(parse_template(ClaudeCodeProvider.EXAMPLE))
    ctx = Context(conversation_id="c", directory=tmp_path, mode="chat", model=None, provider_session="s",
                  mcp_url="http://127.0.0.1:8420/mcp/secret", shell=tmp_path / "shell")
    run = provider.start(ctx, "go")
    argv = run.backend.command(run.session_id)
    assert argv[argv.index("--permission-prompt-tool") + 1] == "mcp__relay__approve_permission"
    assert "--dangerously-skip-permissions" not in argv
    # Its commands run in the folder's container, through Relay's shell
    assert run.backend.env["SHELL"] == run.backend.env["CLAUDE_CODE_SHELL"] == str(tmp_path / "shell")


def test_claude_code_asks_for_a_summary_of_the_thinking(monkeypatch, tmp_path):
    from relay.providers.claude import ClaudeCodeProvider

    monkeypatch.setattr("relay.providers.claude.provider.shutil.which", lambda name: "/usr/bin/claude")
    ctx = Context(conversation_id="c", directory=tmp_path, mode="chat", model=None, provider_session=None)
    argv = ClaudeCodeProvider(parse_template(ClaudeCodeProvider.EXAMPLE)).start(ctx, "go")
    argv = argv.backend.command(argv.session_id)
    assert argv[argv.index("--thinking-display") + 1] == "summarized"
    quiet = ClaudeCodeProvider(parse_template({**ClaudeCodeProvider.EXAMPLE, "options": {"thinking_display": "omitted"}}))
    run = quiet.start(ctx, "go")
    assert run.backend.command(run.session_id)[-1] != "summarized"
    argv = run.backend.command(run.session_id)
    assert argv[argv.index("--thinking-display") + 1] == "omitted"


def test_a_resumed_claude_session_gets_changed_instructions_in_the_message(monkeypatch, tmp_path):
    """Claude Code keeps the system prompt a session started with (--resume ignores a new one)."""
    from relay.modes import BRAINSTORM, PLAN
    from relay.providers.claude import ClaudeCodeProvider

    monkeypatch.setattr("relay.providers.claude.provider.shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr("relay.providers.claude.provider.claude_session_exists", lambda *args: True)
    provider = ClaudeCodeProvider(parse_template(ClaudeCodeProvider.EXAMPLE))

    def sent(**changes):
        ctx = Context(conversation_id="c", directory=tmp_path, mode="code", model=None, provider_session="s1",
                      **changes)
        return provider.start(ctx, "go on").prompt

    assert sent(agent_mode=BRAINSTORM) == "go on"  # nothing changed: the session knows
    switched = sent(agent_mode=BRAINSTORM, mode_switched=True)
    assert "I switched from the Build mode to the Brainstorm mode" in switched
    assert 'work in my "Brainstorm" mode' in switched and "worth weighing" in switched
    assert "Find out what is needed first" in sent(agent_mode=PLAN, mode_switched=True)
    told = sent(tools_note="Relay's note", tell_tools=True)
    assert told.startswith("A note before my message:\n\nRelay's note") and told.endswith("go on")


def test_what_relay_answers_for_claude_code_itself(tmp_path):
    from relay.providers.claude import ClaudeCodeProvider
    judge = ClaudeCodeProvider(parse_template(ClaudeCodeProvider.EXAMPLE)).judge
    # Its commands run in a container: anything but what the guard refuses
    assert judge("Bash", {"command": "npm install && rm -rf build"}, tmp_path) == ("allow", "")
    assert judge("Bash", {"command": "rm -rf /"}, tmp_path)[0] == "deny"
    # Its own tools read anywhere, and change files in the project only
    assert judge("Read", {"file_path": "/etc/hosts"}, tmp_path) == ("allow", "")
    assert judge("Write", {"file_path": str(tmp_path / "a.py")}, tmp_path) == ("allow", "")
    assert judge("Edit", {"file_path": "/etc/hosts"}, tmp_path)[0] == "deny"
    assert judge("mcp__github__create_issue", {}, tmp_path) == ("allow", "")


def test_claude_code_takes_a_model_with_a_provider_in_front():
    """Found for real: a script gave "claude-code/sonnet" (opencode writes its models so), and Claude Code knew no
    such model. The name after a provider's is Claude Code's; any other stays as it is."""
    from relay.providers.claude import ClaudeCodeProvider
    provider = ClaudeCodeProvider(parse_template(ClaudeCodeProvider.EXAMPLE))
    for given, meant in (("claude-code/sonnet", "sonnet"), ("anthropic/claude-sonnet-5-5", "claude-sonnet-5-5"),
                         ("sonnet", "sonnet"), ("", ""), (None, None), ("openai/gpt-5", "openai/gpt-5"),
                         ("claude-code/", "claude-code/")):
        assert provider.model_id(given) == meant, given
