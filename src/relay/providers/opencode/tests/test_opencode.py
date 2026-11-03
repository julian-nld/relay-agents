"""opencode (kind opencode): its service, models and efforts, forks, modes, Relay's tools."""


import pytest

from relay.providers import Context, ProviderError, Registry, parse_template


def context(tmp_path, **kw):
    return Context(conversation_id="conv", directory=tmp_path, mode="chat",
                   model=kw.get("model"), provider_session=kw.get("session"),
                   history=kw.get("history", []))


class FakeOpencodeClient:
    """Answers /api/model with the given lists in turn, and /api/model/default."""

    def __init__(self, responses, default=None):
        self.responses = list(responses)
        self.default = default
        self.paths = []

    def request(self, method, path, body=None, timeout=None):
        if path.startswith("/api/model/default"):
            return {"data": self.default}
        self.paths.append(path)
        return {"data": self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]}


def opencode_provider(monkeypatch, client, wait=5.0):
    from relay.providers.opencode import OpencodeProvider

    provider = OpencodeProvider(parse_template(OpencodeProvider.EXAMPLE))
    monkeypatch.setattr(provider, "_backend", lambda judge=None: type("B", (), {"client": client})())
    monkeypatch.setattr(provider, "MODELS_WAIT", wait)
    monkeypatch.setattr("relay.providers.opencode.provider.time.sleep", lambda s: None)
    return provider


def test_opencode_models_wait_for_a_cold_service(monkeypatch):
    listing = [
        {"providerID": "opencode", "id": "big-pickle", "modelID": "big-pickle", "name": "Big Pickle"},
        # Variants share the upstream modelID but have their own id:
        {"providerID": "openai", "id": "gpt-5.5", "modelID": "gpt-5.5", "name": "GPT-5.5"},
        {"providerID": "openai", "id": "gpt-5.5-fast", "modelID": "gpt-5.5", "name": "GPT-5.5 Fast"},
        {"providerID": "openai", "id": "old", "modelID": "old", "name": "Old", "enabled": False},
    ]
    client = FakeOpencodeClient([[], [], listing], default={"id": "gpt-5.5", "name": "GPT-5.5"})
    models = opencode_provider(monkeypatch, client).models()
    assert models == [{"id": "", "name": "Default · GPT-5.5"},
                      {"id": "opencode/big-pickle", "name": "Big Pickle"},
                      {"id": "openai/gpt-5.5", "name": "GPT-5.5"},
                      {"id": "openai/gpt-5.5-fast", "name": "GPT-5.5 Fast"}]
    assert len(client.paths) == 3 and "directory=" in client.paths[0]


def test_opencode_models_give_up_after_waiting(monkeypatch):
    client = FakeOpencodeClient([[]])
    assert opencode_provider(monkeypatch, client, wait=0).models() == [
        {"id": "", "name": "Default model"}]


def test_opencode_providers_follow_or_pin_profiles(tmp_path, monkeypatch):
    from opencode_manager import paths, state
    from opencode_manager.profiles import ProfileStore
    from relay.providers.opencode import OpencodeProvider

    monkeypatch.setenv("RELAY_HOME", str(tmp_path / "data"))
    store = ProfileStore(paths.profiles_dir(), 50000)
    store.create("local_ai")
    registry = Registry(tmp_path / "providers")
    registry.ensure_defaults()
    templates = {t.id: t for t in registry.templates()}
    assert set(templates) == {"claude-code", "opencode"}
    assert "profile" not in templates["opencode"].options

    # Without a profile, the provider uses opencode-manager's active profile.
    follower = OpencodeProvider(templates["opencode"])
    assert follower.profile_name() == "default"
    state.remember_profile("local_ai")
    assert follower.profile_name() == "local_ai"
    state.remember_profile("deleted-meanwhile")
    assert follower.profile_name() == "default"

    # Every profile can be pinned; suggestions disappear once pinned.
    assert [s["options"]["profile"] for s in registry.suggestions()] == ["default", "local_ai"]
    registry.add(registry.suggestions()[1])
    assert registry.get("opencode-local-ai").name == "opencode · local_ai"
    assert [s["options"]["profile"] for s in registry.suggestions()] == ["default"]


def test_opencode_in_its_shell_is_its_profiles_with_its_service_running(tmp_path, monkeypatch):
    """Its Shell (relay.creations _in_its_box): opencode there is that profile's, and its service was started first,
    as Relay's. One that won't start doesn't stop the shell: it may be why it's opened."""
    from opencode_manager import paths
    from opencode_manager.profiles import ProfileStore
    from relay.providers.opencode import OpencodeProvider

    monkeypatch.setenv("RELAY_HOME", str(tmp_path / "data"))
    ProfileStore(paths.profiles_dir(), 50000).create("local_ai")
    template = {"id": "oc", "name": "OC", "kind": "opencode", "options": {"profile": "local_ai"}}
    provider = OpencodeProvider(parse_template(template))
    assert provider.env() == {"OPENCODE_CONFIG_DIR": str(paths.profiles_dir() / "local_ai" / "config"),
                              "XDG_DATA_HOME": str(paths.profiles_dir() / "local_ai" / "data"),
                              "XDG_STATE_HOME": str(paths.profiles_dir() / "local_ai" / "state"),
                              "TMPDIR": "/tmp/opencode"}
    gone = OpencodeProvider(parse_template({**template, "options": {"profile": "deleted-meanwhile"}}))
    assert gone.env() == {"TMPDIR": "/tmp/opencode"}
    assert provider.has_shell() and provider.box_shell() == "/usr/lib/relay/bash.real"
    assert provider.SIGN_IN is None and "/connect" in provider.SHELL_HINT  # (Add provider's page stays as it is)

    started = []
    monkeypatch.setattr(provider, "_backend", lambda: started.append(True))
    provider.shell_ready()
    assert started == [True]

    def fails():
        raise ProviderError("Port 49400 is taken")

    monkeypatch.setattr(provider, "_backend", fails)
    provider.shell_ready()
    # A profile that isn't there stops it: opencode in the shell would be another profile's
    with pytest.raises(ProviderError, match="does not exist"):
        gone.shell_ready()


def test_a_new_opencode_provider_makes_the_profile_it_names(tmp_path, monkeypatch):
    """Add provider's next step: Relay's pages have no other way to make one."""
    from opencode_manager import paths
    from opencode_manager.profiles import ProfileStore
    from relay.providers.opencode import OpencodeProvider

    monkeypatch.setenv("RELAY_HOME", str(tmp_path / "data"))

    def new(**options):
        return OpencodeProvider(parse_template({"id": "oc", "name": "OC", "kind": "opencode", "options": options}))

    store = ProfileStore(paths.profiles_dir(), 50000)
    new(profile="work").prepare_new()
    assert store.exists("work") and new(profile="work").env()["XDG_DATA_HOME"].endswith("work/data")
    (paths.profiles_dir() / "work" / "kept").write_text("a login")
    new(profile="work").prepare_new()  # (one that's there stays as it is)
    assert (paths.profiles_dir() / "work" / "kept").read_text() == "a login"
    new().prepare_new()  # (none named: it follows the active one)
    with pytest.raises(ProviderError, match="a-z"):
        new(profile="Not A Name").prepare_new()


def test_opencode_models_stop_waiting_on_real_errors(monkeypatch):
    from opencode_manager.bridge.http import HttpError

    class Refusing:
        calls = 0

        def request(self, method, path, body=None, timeout=None):
            Refusing.calls += 1
            raise HttpError("unauthorized", 401)

    models = opencode_provider(monkeypatch, Refusing(), wait=60).models()
    assert models == [{"id": "", "name": "Default model"}] and Refusing.calls == 1


def test_opencode_applies_a_model_and_effort_chosen_halfway(monkeypatch, tmp_path):
    from relay.providers.opencode import OpencodeProvider

    class Backend:
        def __init__(self):
            self.calls = []
            self.model = {"providerID": "openai", "id": "gpt-5.5", "variant": "default"}

        def set_permissions(self, session_id, rules):
            self.permissions = rules

        def get_session(self, session_id):
            return {"id": session_id, "model": dict(self.model)}

        def set_model(self, session_id, model, variant=None):
            self.calls.append((model, variant))
            provider, _, model_id = model.partition("/")
            self.model = {"providerID": provider, "id": model_id, "variant": variant or "default"}

        def variants(self, directory, model):
            assert directory == tmp_path
            return ["low", "high"] if model == "openai/gpt-5.5" else []

        def set_agent(self, session_id, agent):
            pass

        def commands(self, directory):
            return []

    backend = Backend()
    provider = OpencodeProvider(parse_template(OpencodeProvider.EXAMPLE))
    monkeypatch.setattr(provider, "_backend", lambda judge=None: backend)

    def run(**kw):
        ctx = context(tmp_path, session="ses_1", model=kw.get("model"))
        ctx.effort = kw.get("effort")
        provider.start(ctx, "go")

    run(model="openai/gpt-5.5")
    run()  # the default model: whatever the session has
    assert backend.calls == []
    run(model="openai/gpt-5.5", effort="high")
    run(model="openai/gpt-5.5", effort="high")  # unchanged
    assert backend.calls == [("openai/gpt-5.5", "high")]
    run(model="opencode/big-pickle", effort="high")
    assert backend.calls[-1] == ("opencode/big-pickle", "default")
    count = len(backend.calls)
    run(model="opencode/big-pickle", effort="max")  # not one of its variants: its default then
    assert len(backend.calls) == count
    run(model="opencode/longcat-2.5-preview-free", effort="high")
    assert backend.calls[-1] == ("opencode/longcat-2.5-preview-free", "default")


@pytest.mark.parametrize("chosen_model", [None, "opencode/big-pickle"])
def test_opencode_clears_an_invalid_variant_already_in_the_session(tmp_path, chosen_model):
    from relay.providers.opencode.provider import _choose_model

    class Backend:
        def variants(self, directory, model):
            assert directory == tmp_path and model == "opencode/big-pickle"
            return []

        def set_model(self, session_id, model, variant=None):
            self.chosen = (session_id, model, variant)

    backend = Backend()
    ctx = context(tmp_path, session="ses_1", model=chosen_model)
    ctx.effort = "high"
    session = {"model": {"providerID": "opencode", "id": "big-pickle", "variant": "high"}}
    _choose_model(backend, "ses_1", session, ctx)
    assert backend.chosen == ("ses_1", "opencode/big-pickle", "default")


def test_opencode_model_selection_does_not_hide_http_errors(tmp_path):
    from opencode_manager.bridge.http import HttpError
    from relay.providers.opencode.provider import _choose_model

    class Backend:
        def variants(self, directory, model):
            return ["high"]

        def set_model(self, session_id, model, variant=None):
            raise HttpError("not signed in", 401)

    ctx = context(tmp_path, session="ses_1", model="openai/gpt-5.5")
    ctx.effort = "high"
    with pytest.raises(HttpError, match="not signed in"):
        _choose_model(Backend(), "ses_1", {}, ctx)


def test_opencode_forks_before_a_message_or_falls_back_to_a_handover(monkeypatch, tmp_path):
    from relay.providers.opencode import OpencodeProvider

    monkeypatch.setenv("RELAY_HOME", str(tmp_path / "data"))

    class Backend:
        def __init__(self, sessions):
            self.sessions = sessions
            self.forks = []

        def set_permissions(self, session_id, rules):
            self.permissions = rules

        def get_session(self, session_id):
            return {"id": session_id} if session_id in self.sessions else None

        def fork(self, session_id, before=None):
            self.forks.append((session_id, before))
            self.sessions.add("ses_fork")
            return "ses_fork"

        def create_session(self, directory, model=None, agent=None):
            return "ses_new"

        def set_agent(self, session_id, agent):
            pass

    provider = OpencodeProvider(parse_template(OpencodeProvider.EXAMPLE))
    history = [{"role": "user", "content": "remember kiwi"}]
    ctx = Context(conversation_id="c", directory=tmp_path, mode="chat", model=None, provider_session=None,
                  history=history, fork_from="ses_old", fork_before="msg_2")

    backend = Backend({"ses_old"})
    monkeypatch.setattr(provider, "_backend", lambda judge=None: backend)
    run = provider.start(ctx, "what fruit?")
    assert backend.forks == [("ses_old", "msg_2")] and run.session_id == "ses_fork"
    assert run.prompt == "what fruit?"

    backend = Backend(set())  # the original session is gone
    monkeypatch.setattr(provider, "_backend", lambda judge=None: backend)
    run = provider.start(ctx, "what fruit?")
    assert backend.forks == [] and run.session_id == "ses_new" and "User: remember kiwi" in run.prompt


def test_modes_for_opencode(monkeypatch, tmp_path):
    from relay.modes import Mode
    from relay.providers.opencode import OpencodeProvider

    class Backend:
        def set_permissions(self, session_id, rules):
            self.permissions = rules

        def __init__(self):
            self.agents = []

        def get_session(self, session_id):
            return {"id": session_id}

        def set_agent(self, session_id, agent):
            self.agents.append(agent)

    backend = Backend()
    provider = OpencodeProvider(parse_template(OpencodeProvider.EXAMPLE))
    monkeypatch.setattr(provider, "_backend", lambda judge=None: backend)
    critic = Mode("critic", "Critic", instructions="Be harsh.", access="read")
    ctx = Context(conversation_id="c", directory=tmp_path, mode="code", model=None,
                  provider_session="ses_1", agent_mode=critic)
    run = provider.start(ctx, "review this")
    # Its build agent, told not to change anything (opencode's plan agent refuses even reading commands)
    assert backend.agents == ["build"]
    assert "Be harsh." in run.prompt and "read-only mode" in run.prompt and run.prompt.endswith("review this")
    # opencode asks Relay before changing files, whatever the profile's config says (Relay keeps them to the project)
    assert {r["action"] for r in backend.permissions} >= {"edit", "write"}
    ctx.agent_mode = None
    run = provider.start(ctx, "build it")
    assert backend.agents == ["build", "build"] and run.prompt == "build it"
    assert {r["action"] for r in backend.permissions} >= {"edit", "write"}
    assert provider.judge("edit", ["src/a.py"], tmp_path) == ("allow", "")
    assert provider.judge("edit", ["../../etc/x"], tmp_path)[0] == "deny"
    assert provider.judge("shell", ["rm -rf /"], tmp_path)[0] == "deny"
    assert provider.judge("shell", ["rm -rf build"], tmp_path) == ("allow", "")


def test_opencode_is_told_about_relays_tools_in_its_first_message(monkeypatch, tmp_path):
    from relay.providers.opencode import OpencodeProvider
    from relay.tools_script import note

    class Backend:
        read_only = False
        always_allow = []

        def set_permissions(self, session_id, rules):
            self.permissions = rules

        def get_session(self, session_id):
            return {"id": session_id}

        def set_agent(self, session_id, agent):
            pass

    backend = Backend()
    provider = OpencodeProvider(parse_template(OpencodeProvider.EXAMPLE))
    monkeypatch.setattr(provider, "_backend", lambda judge=None: backend)
    script = tmp_path / "tools" / "abc" / "relay"
    ctx = Context(conversation_id="c", directory=tmp_path, mode="code", model=None, provider_session="ses_1",
                  tools_path=script, tools_note=note(script), tell_tools=True)
    run = provider.start(ctx, "build it")
    assert run.prompt.startswith("A note before my message:") and "`relay help`" in run.prompt
    assert run.prompt.endswith("build it")
    ctx.tell_tools = False
    assert provider.start(ctx, "more").prompt == "more"


def test_opencode_takes_a_model_without_its_provider(monkeypatch):
    """Found by a simulated user: model="big-pickle" (no "opencode/" in front) and opencode said "Model unavailable:
    big-pickle/". The one model of that name is meant; with two of that name, or none, it stays as given."""
    from relay.providers.opencode import OpencodeProvider
    provider = OpencodeProvider(parse_template(OpencodeProvider.EXAMPLE))
    monkeypatch.setattr(provider, "models", lambda: [{"id": ""}, {"id": "opencode/big-pickle"}, {"id": "openai/gpt-5"},
                                                     {"id": "azure/gpt-5"}])
    assert provider.model_id("big-pickle") == "opencode/big-pickle"
    assert [provider.model_id(m) for m in ("gpt-5", "nothing", "opencode/big-pickle", "", None)] == [
        "gpt-5", "nothing", "opencode/big-pickle", "", None]
