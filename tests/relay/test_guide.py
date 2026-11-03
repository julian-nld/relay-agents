"""docs/ADDING-A-PROVIDER.md stays true: its code is the example's, word for word, and its reference tables name
every hook, event and Context field there is."""

import dataclasses
import re
from pathlib import Path

from starlette.testclient import TestClient

from relay import config
from relay.app import create_app
from relay.db import Database
from relay.providers import Registry, conformance
from relay.providers.base import Context, Provider, ProviderRun

ROOT = Path(__file__).resolve().parents[2]
GUIDE = (ROOT / "docs" / "ADDING-A-PROVIDER.md").read_text()


def section(title: str) -> str:
    """A reference section: from its heading to the next."""
    found = re.search(rf"^### {re.escape(title)}\n(.*?)(?=^#{{2,3}} |\Z)", GUIDE, re.M | re.S)
    assert found, title
    return found.group(1)


def test_its_code_is_the_examples_word_for_word():
    quoted = re.findall(r"<!-- (example/[\w/.]+) -->\n```python\n(.*?)\n```", GUIDE, re.S)
    assert len(quoted) >= 6
    for file, code in quoted:
        assert code in (ROOT / "src" / "relay" / "providers" / file).read_text(), f"{file} no longer has:\n{code}"


def test_its_reference_names_every_hook_event_and_context_field():
    hooks = section("Provider hooks")
    names = {*Provider.__annotations__, *(n for n, v in vars(Provider).items() if not n.startswith("_")
                                          and (callable(v) or isinstance(v, (classmethod, property)))),
             *(n for n, v in vars(ProviderRun).items() if not n.startswith("_") and callable(v))}
    missing = sorted(n for n in names if not re.search(rf"^\| `{n}[`(]", hooks, re.M))
    assert not missing, f"not in the guide's Provider hooks: {missing}"
    events = section("Uniform events")
    assert not [t for t in conformance.FIELDS if f"| `{t}` |" not in events]
    fields = section("Context")
    assert not [f.name for f in dataclasses.fields(Context) if f"| `{f.name}` |" not in fields]


def test_settings_shows_it(tmp_path):
    settings = config.Settings(workspace_roots=[str(tmp_path)], chat_root=str(tmp_path / "chats"))
    registry = Registry(tmp_path / "providers")
    with TestClient(create_app(settings, Database(tmp_path / "relay.db"), registry)) as client:
        client.post("/api/setup", json={"username": "me", "password": "correct horse"},
                    headers={"x-relay-request": "1"}).raise_for_status()
        assert client.get("/api/providers/guide").json()["markdown"] == GUIDE
        kinds = {k["kind"]: k for k in client.get("/api/provider-kinds").json()}  # (an added kind: its folder)
        assert (kinds["example"]["builtin"], kinds["example"]["folder"]) == (False, "providers/example")
        assert (kinds["claude-code"]["builtin"], kinds["claude-code"]["folder"]) == (True, "providers/claude")
