"""Provider folders (src/relay/providers/<name>/): found, not listed; a mistake in one never stops Relay."""

import sys
import textwrap
import uuid

import pytest
from starlette.testclient import TestClient

from relay import config, providers
from relay.app import create_app
from relay.db import Database
from relay.providers import KINDS, Registry, TemplateError, discover, parse_template, safely
from relay.providers.base import Provider, ProviderRun

H = {"x-relay-request": "1"}

GOOD = '''
from relay.providers.base import Provider, ProviderRun

class Run(ProviderRun):
    def events(self):
        yield {{"type": "session", "session": "s"}}
        yield {{"type": "done", "status": "succeeded"}}

    def interrupt(self):
        pass

class {name}(Provider):
    KIND = "{kind}"
    LABEL = "Good"
    DESCRIPTION = "A kind that works."
    TOOLS = False
    EXAMPLE = {{"id": "{kind}", "name": "Good", "kind": "{kind}", "options": {{}}}}

    def check(self):
        return "fine"

    def start(self, context, prompt):
        return Run()
'''


@pytest.fixture
def layers(tmp_path):
    """A folder of provider folders, importable as a package of its own: write(name, files) adds one."""
    package = f"layers_{uuid.uuid4().hex[:8]}"
    root = tmp_path / package
    root.mkdir()
    (root / "__init__.py").write_text("")
    sys.path.insert(0, str(tmp_path))

    def write(name, files):
        folder = root / name
        folder.mkdir()
        for file, text in files.items():
            (folder / file).write_text(textwrap.dedent(text))
        return folder

    yield root, package, write
    sys.path.remove(str(tmp_path))
    for module in [m for m in sys.modules if m == package or m.startswith(package + ".")]:
        del sys.modules[module]


def good(kind="good-one", name="GoodProvider"):
    return {"provider.py": GOOD.format(kind=kind, name=name),
            "__init__.py": f"from .provider import {name}\n\nKINDS = ({name},)\n"}


def test_relays_own_kinds_are_found_in_their_folders_in_their_order():
    assert list(KINDS)[:7] == list(providers.ORDER)
    assert not providers.BROKEN
    assert KINDS["claude-code"].support_files()[:2] == ("relay/providers/claude/__init__.py",
                                                       "relay/providers/claude/provider.py")
    assert "opencode_manager/bridge/claude_backend.py" in KINDS["claude-code"].support_files()
    assert "relay/providers/perplexity/perplexity.cjs" in KINDS["perplexity"].support_files()
    assert not any("/tests/" in f for kind in KINDS.values() for f in kind.support_files())


def test_a_mistake_in_a_folder_is_said_and_the_others_load(layers):
    root, package, write = layers
    write("fine", good())
    write("syntax", {"__init__.py": "from .provider import P\nKINDS = (P,)\n", "provider.py": "def broken(:\n"})
    write("imports", {"__init__.py": "import a_module_that_isnt_there\n"})
    write("raises", {"__init__.py": "\n\nraise RuntimeError('the layer fell over')\n"})
    write("nokinds", {"__init__.py": "X = 1\n"})
    write("twice", good("good-one", "Again"))  # (a kind another folder has)
    write("notakind", {"__init__.py": "KINDS = (dict,)\n"})
    write("badkind", good("Bad Kind!"))
    write("abstract", {"__init__.py": textwrap.dedent('''
        from relay.providers.base import Provider
        class Half(Provider):
            KIND = "half"
            LABEL = DESCRIPTION = "Half"
            TOOLS = False
            EXAMPLE = {"id": "half", "name": "Half", "kind": "half", "options": {}}
            def check(self):
                return "fine"
        KINDS = (Half,)
    ''')})
    write("example", {**good("ex", "Ex"), "provider.py": GOOD.format(kind="ex", name="Ex").replace(
        '"kind": "ex", "options": {}', '"kind": "ex", "options": {"unknown": 1}')})
    broken = {}
    found = discover(root, package, broken)
    assert list(found) == ["good-one"]
    assert broken["providers/syntax"].startswith("invalid syntax") and "syntax/provider.py, line 1" in broken["providers/syntax"]
    assert broken["providers/imports"].startswith("ModuleNotFoundError") and "imports/__init__.py, line 1" in broken["providers/imports"]
    assert broken["providers/raises"] == "RuntimeError: the layer fell over (raises/__init__.py, line 3)"
    assert "names no kinds" in broken["providers/nokinds"]
    assert "'good-one' is taken by providers/fine" in broken["providers/twice"]
    assert "isn't a Provider subclass" in broken["providers/notakind"]
    assert "KIND must be lowercase" in broken["providers/badkind"]
    assert broken["providers/abstract"] == "Half doesn't write start yet"
    assert "EXAMPLE isn't valid for it: Unknown option 'unknown'" in broken["providers/example"]


class Faulty(Provider):
    """A kind whose layer raises where it answers questions: Relay goes on without its answers."""
    KIND = "faulty"
    LABEL = "Faulty"
    DESCRIPTION = "Raises."
    TOOLS = False
    EXAMPLE = {"id": "faulty", "name": "Faulty", "kind": "faulty", "options": {}}

    def validate(self):
        if self.options.get("explode"):
            raise ValueError("its own options check fell over")

    def models(self):
        raise RuntimeError("no models today")

    def detail(self):
        raise RuntimeError("no detail")

    def efforts(self, model):
        raise RuntimeError("no efforts")

    def free_models(self):
        raise RuntimeError("no free models")

    def model_id(self, model):
        raise RuntimeError("no model ids")

    def check(self):
        raise RuntimeError("its check fell over")

    def start(self, context, prompt):
        class Run(ProviderRun):
            def events(self):
                raise RuntimeError("its run fell over")

            def interrupt(self):
                pass
        return Run()


def test_a_layer_that_raises_is_that_providers_trouble_never_relays(tmp_path, monkeypatch):
    monkeypatch.setitem(KINDS, "faulty", Faulty)
    monkeypatch.setitem(providers.BROKEN, "providers/oops", "invalid syntax (oops/provider.py, line 3)")
    with pytest.raises(TemplateError, match="couldn't check the options: ValueError: its own options check fell over"):
        parse_template({"id": "f", "name": "F", "kind": "faulty", "options": {"explode": True}})
    with pytest.raises(TemplateError, match=r"kind 'oops' isn't loaded: a provider folder can't be \(providers/oops"):
        parse_template({"id": "f", "name": "F", "kind": "oops", "options": {}})
    assert safely(Faulty(parse_template(Faulty.EXAMPLE)).models, []) == []
    settings = config.Settings(workspace_roots=[str(tmp_path)], chat_root=str(tmp_path / "chats"))
    registry = Registry(tmp_path / "providers")
    registry.ensure_defaults = lambda: None
    (tmp_path / "providers").mkdir()
    registry.add(Faulty.EXAMPLE)
    registry.add({"id": "echo", "name": "Echo", "kind": "command", "options": {"command": ["true"]}})
    (tmp_path / "providers" / "gone.json").write_text('{"id": "gone", "name": "Gone", "kind": "oops", "options": {}}')
    with TestClient(create_app(settings, Database(tmp_path / "relay.db"), registry)) as client:
        client.post("/api/setup", json={"username": "me", "password": "correct horse"}, headers=H).raise_for_status()
        listed = client.get("/api/providers").json()
        assert [p["id"] for p in listed["providers"]] == ["echo", "faulty"]
        assert listed["broken_kinds"] == {"providers/oops": "invalid syntax (oops/provider.py, line 3)"}
        assert "providers/oops" in listed["problems"]["gone.json"]
        assert {k["kind"] for k in client.get("/api/provider-kinds").json()} >= {"faulty", "command"}
        answer = client.get("/api/providers/faulty/models")
        assert answer.status_code == 502 and "no models today" in answer.json()["error"]
        assert client.post("/api/providers/faulty/check", headers=H).json() == {
            "ok": False, "message": "RuntimeError: its check fell over"}
        chat = client.post("/api/conversations", json={"provider_id": "faulty"}, headers=H)
        assert chat.status_code == 200, chat.text  # (its model_id fell over: the model as given)
