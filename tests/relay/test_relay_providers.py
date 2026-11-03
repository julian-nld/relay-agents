
import pytest

from relay.providers import KINDS, ORDER, Context, Registry, TemplateError, parse_template


def test_every_kind_has_a_valid_example():
    for kind, cls in KINDS.items():
        template = parse_template(cls.EXAMPLE)
        assert template.kind == kind
        assert cls.describe()["fields"]


@pytest.mark.parametrize("data, message", [
    ([], "JSON object"),
    ({"id": "Bad Id", "name": "x", "kind": "command"}, "'id'"),
    ({"id": "ok", "name": "", "kind": "command"}, "'name'"),
    ({"id": "ok", "name": "x", "kind": "nope"}, "'kind'"),
    ({"id": "ok", "name": "x", "kind": "command", "options": {}}, "'command' is required"),
    ({"id": "ok", "name": "x", "kind": "command", "options": {"command": "ls"}}, "string-list"),
    ({"id": "ok", "name": "x", "kind": "command", "options": {"command": ["ls"], "bogus": 1}}, "Unknown option"),
    ({"id": "ok", "name": "x", "kind": "openai-compatible", "options": {"base_url": "ftp://x"}}, "http"),
    ({"id": "ok", "name": "x", "kind": "command", "extra": 1}, "Unknown template keys"),
])
def test_invalid_templates_are_rejected(data, message):
    with pytest.raises(TemplateError, match=message):
        parse_template(data)


def test_registry_defaults_and_broken_files(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_HOME", str(tmp_path / "data"))
    registry = Registry(tmp_path / "providers")
    registry.ensure_defaults()
    assert [t.id for t in registry.templates()] == ["claude-code", "opencode"]
    (tmp_path / "providers" / "broken.json").write_text("{not json")
    assert "broken.json" in registry.problems()
    assert [t.id for t in registry.templates()] == ["claude-code", "opencode"]
    registry.ensure_defaults()  # doesn't re-add removed or change existing ones
    registry.remove("opencode")
    registry.ensure_defaults()
    assert [t.id for t in registry.templates()] == ["claude-code"]


def test_template_files_are_private(tmp_path):
    registry = Registry(tmp_path / "providers")
    registry.add({"id": "k", "name": "K", "kind": "openai-compatible",
                  "options": {"base_url": "https://x.test/v1", "api_key": "secret"}})
    mode = (tmp_path / "providers" / "k.json").stat().st_mode & 0o777
    assert mode == 0o600


# Agents


def test_agents_own_commands_are_passed_on(monkeypatch, tmp_path):
    from relay.providers.claude import ClaudeCodeProvider
    from relay.providers.opencode import OpencodeProvider

    monkeypatch.setattr("relay.providers.claude.provider.shutil.which", lambda name: "/usr/bin/claude")
    (tmp_path / ".claude" / "commands").mkdir(parents=True)
    (tmp_path / ".claude" / "commands" / "ship.md").write_text("---\ndescription: Ship it\n---\nDo it.")
    claude = ClaudeCodeProvider(parse_template({**ClaudeCodeProvider.EXAMPLE,
                                                "options": {"config_dir": str(tmp_path / "cfg")}}))
    names = {c["name"]: c["description"] for c in claude.commands(tmp_path)}
    assert names["ship"] == "Ship it" and "init" in names and "compact" in names
    # Claude Code runs slash commands itself: sent as typed, even in a fresh session with history
    run = claude.start(Context(conversation_id="c", directory=tmp_path, mode="code", model=None,
                               provider_session=None, history=[{"role": "user", "content": "x"}]), "/ship now")
    assert run.prompt == "/ship now"
    assert claude.efforts(None) == ["low", "medium", "high", "xhigh", "max"]

    class Backend:
        read_only = False
        always_allow = []

        def set_permissions(self, session_id, rules):
            self.permissions = rules

        def get_session(self, session_id):
            return {"id": session_id, "model": {}}

        def set_agent(self, session_id, agent):
            pass

        def commands(self, directory):
            return [{"name": "review", "description": "review changes"}]

    opencode = OpencodeProvider(parse_template(OpencodeProvider.EXAMPLE))
    monkeypatch.setattr(opencode, "_backend", lambda judge=None: Backend())
    ctx = Context(conversation_id="c", directory=tmp_path, mode="code", model=None, provider_session="ses_1")
    run = opencode.start(ctx, "/review the last commit")
    assert run.run_options["command"] == "review" and run.prompt == "the last commit"
    assert "command" not in opencode.start(ctx, "/unknown thing").run_options  # just text then


def test_the_interface_covers_details_programs_and_answers(tmp_path):
    from relay.providers import KINDS
    from relay.providers.base import ProviderError
    from relay.providers.bridge_run import BridgeRun as _BridgeRun
    from relay.providers.claude import ClaudeCodeProvider
    from relay.providers.opencode import OpencodeProvider

    pinned = OpencodeProvider(parse_template({**OpencodeProvider.EXAMPLE, "options": {"profile": "work"}}))
    assert pinned.detail() == "profile work"
    assert ClaudeCodeProvider(parse_template(ClaudeCodeProvider.EXAMPLE)).detail() is None
    # (Relay's own kinds: one the user added has its own programs)
    assert sorted({p for name, kind in KINDS.items() if name in ORDER for p in kind.PROGRAMS}) == \
        ["claude", "copilot", "opencode"]

    class Answers:
        def answer(self, request_id, approve):
            self.answered = (request_id, approve)

    class Silent:
        pass

    waiting = _BridgeRun(Answers(), "s", "hi", False)
    assert waiting.waits_for_answers()
    waiting.answer_permission("p1", True)
    assert waiting.backend.answered == ("p1", True)
    silent = _BridgeRun(Silent(), "s", "hi", False)
    assert not silent.waits_for_answers()
    with pytest.raises(ProviderError):
        silent.answer_permission("p1", True)


def test_brainstorm_is_for_what_is_worth_weighing():
    from relay.modes import BRAINSTORM
    assert BRAINSTORM.instructions.startswith("This mode is for a question, idea or decision worth weighing.")


def test_templates_with_a_retired_option_still_load():
    from relay.providers.claude import ClaudeCodeProvider
    ClaudeCodeProvider(parse_template({**ClaudeCodeProvider.EXAMPLE, "options": {"auto_approve": False}}))


def test_a_handover_only_says_messages_were_left_out_when_some_were(monkeypatch):
    from relay import handover, tunables
    monkeypatch.setitem(tunables._values, "handover.chars", 2000)
    blocks, left_out = handover._fit(["x" * 5000])
    assert left_out == 0 and not any("left out" in b for b in blocks)
    blocks, left_out = handover._fit(["first", *(["y" * 900] * 5)])
    assert left_out > 0 and any(f"{left_out} message" in b for b in blocks)


def test_an_answers_parts_are_paragraphs():
    """What an agent says before, between and after its tool calls is stored as one answer, not run together."""
    from relay.handover import answer_text
    events = [{"type": "text", "delta": "I'll commit the files."}, {"type": "tool_call", "name": "shell"},
              {"type": "tool_result", "ok": True}, {"type": "step"}, {"type": "text", "delta": "The tests "},
              {"type": "usage"}, {"type": "text", "delta": "pass."}, {"type": "tool_call"},
              {"type": "text", "delta": "\n\nDone."}]
    assert answer_text(events) == "I'll commit the files.\n\nThe tests pass.\n\nDone."
    assert answer_text([]) == "" and answer_text([{"type": "tool_call"}]) == ""



def test_doctor_checks_opencode_without_starting_a_service(monkeypatch):
    from relay import doctor
    from relay.providers.base import Provider
    from relay.providers.opencode import OpencodeProvider

    monkeypatch.setattr(OpencodeProvider, "_backend", lambda self, judge=None: pytest.fail("started a service"))
    monkeypatch.setattr("relay.providers.opencode.provider.shutil.which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr("relay.providers.opencode.provider.opencode.serves", lambda settings, profile: False)
    monkeypatch.setattr("relay.providers.claude.provider.ClaudeCodeProvider.check", lambda self: "fine")
    checks = {c.title: c.detail for c in doctor._providers()}
    assert "isn't running; Relay starts it when needed" in checks["Provider opencode (opencode)"]
    assert Provider.probing is False
