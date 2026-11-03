"""The suite's shared setup, at the root so every test gets it: tests/ and each provider folder's own
(src/relay/providers/<name>/tests) alike."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

# A backstop under the fixtures' own (private_cache): whatever a test undoes (monkeypatch.undo()
# takes the fixtures' settings with it), Relay's data, the machine's cache and the default workspace
# root are a throwaway's, never the user's real ~/.local/share/relay, ~/.cache and ~/projects.
_BACKSTOP = tempfile.mkdtemp(prefix="relay-tests-")
os.environ["RELAY_HOME"] = os.path.join(_BACKSTOP, "relay-home")
os.environ["XDG_CACHE_HOME"] = os.path.join(_BACKSTOP, "cache")
from relay import config as _config  # noqa: E402
from relay.providers.base import ProviderError  # noqa: E402
from relay.providers.opencode import OpencodeProvider  # noqa: E402

_config.default_workspace_root = lambda: Path(_BACKSTOP) / "projects"


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_BACKSTOP, ignore_errors=True)


# In parallel (pytest -n, pytest-xdist): tests that share something outside their tmp_path run in
# one process, one after another. ProductionBoxes on real podman (their internal ports come from one
# range, and side by side they slow each other down more than they gain); the other real-podman tests
# (they share the image), apart from those. Each Chromium check runs in a container of its own, its
# file's tests in one process. The suite mostly waits (on podman, browsers, agents' replies), so it
# runs with more processes than the machine has cores (AGENTS.md); the groups go first, so they
# aren't the tail the run waits for.
SHARED = {"test_production.py": "production", "test_containers.py": "podman", "test_opencode_browser.py": "podman"}


@pytest.hookimpl(tryfirst=True)  # (before xdist's own, which reads the groups)
def pytest_collection_modifyitems(config, items):
    if not config.pluginmanager.hasplugin("xdist"):
        return
    grouped = set()
    for item in items:
        group = f"chromium-{item.path.stem}" if item.path.name.startswith("test_ui_") else SHARED.get(item.path.name)
        if group:
            item.add_marker(pytest.mark.xdist_group(group))
            grouped.add(item)
    items.sort(key=lambda item: item not in grouped)


@pytest.fixture(autouse=True)
def default_limits():
    """Settings → Limits (relay.tunables) are kept in the module: a test's Relay that saves some
    (the settings UI check) mustn't hand them to the next test in the same process."""
    from relay import tunables
    tunables._values.clear()
    yield
    tunables._values.clear()


@pytest.fixture(autouse=True)
def no_usage_questions(monkeypatch):
    """Providers' limits() would ask Anthropic, OpenAI and GitHub with this machine's own logins
    (Settings → Usage): not from tests. A test that wants an answer puts its own fetch_json."""
    monkeypatch.setattr("relay.limits.fetch_json", lambda url, headers, timeout=8: (None, "not asked in tests"))


@pytest.fixture(autouse=True)
def private_cache(tmp_path, monkeypatch):
    """Every test gets a cache of its own: Relay writes tools scripts and skill
    links to the machine's cache (paths.host_cache_dir), which is the user's real
    ~/.cache otherwise."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    # …and data of its own: never the user's Relay (RELAY_HOME, ~/.local/share/relay).
    monkeypatch.setenv("RELAY_HOME", str(tmp_path / "relay-home"))
    # …and a workspace root of its own when it gives none: never the user's ~/projects, with their scripts'
    # folder (a Relay's start puts that in order: relay.scripts).
    monkeypatch.setattr("relay.config.default_workspace_root", lambda: tmp_path / "projects")
    monkeypatch.delenv("OPENCODE_MANAGER_HOME", raising=False)
    # …and PATH as it was after: `relay`'s CLI adds the agents' usual folders to it (relay.cli.find_agents).
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    # update-ref writes reflogs too. Without an identity Git may look up the
    # container hostname (slow/failing DNS), making checkpoint tests time out.
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Relay tests")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "relay-tests@localhost")


class FakeContainers:
    """Relay's containers (relay.containers) without podman: commands run here."""

    def __init__(self, image=None, state=None):
        self.image = image
        self.routes = {}
        self.also = {}
        self.discarded = []
        self.checked = "ok"
        self.hidden, self.scripts_public = [], None
        self.provided = {}  # folder: the files copied into its WorkBox (here they're simply there)

    def scripts_folders(self, private, public):
        self.hidden, self.scripts_public = [private], public

    def check(self):
        pass

    def shell(self, directory):
        return self._shell()

    def shared_shell(self):
        return self._shell()

    def route(self, directory):
        self.routes[str(directory)] = True

    def forget_routes(self):
        self.routes = {}

    def browser_argv(self, directory):
        return ["true"]

    @property
    def state(self):
        from opencode_manager import paths
        return paths.host_cache_dir() / "fake-containers"

    def stop_idle(self, idle_hours):
        return 0

    def set_url(self, url):
        self.url = url

    def agent_env(self, tools=None, directory=None):
        return []

    def box_key(self, directory):
        return "key-" + str(Path(directory).resolve())

    def provide(self, directory, files):
        self.provided.setdefault(str(directory), []).extend(str(f) for f in files)

    def mount_also(self, directory, folders):
        self.also[str(directory)] = list(folders)

    def discard(self, directory):
        self.discarded.append(str(directory))

    def _shell(self):
        import os

        from opencode_manager import paths
        path = paths.host_cache_dir() / "fake-containers" / "bash"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('#!/bin/sh\nexec /bin/bash "$@"\n')
            os.chmod(path, 0o755)
        return path


@pytest.fixture(autouse=True)
def no_podman(monkeypatch):
    """Tests don't start containers (tests/test_containers.py tests those)."""
    monkeypatch.setattr("relay.app.Containers", FakeContainers)
    monkeypatch.setattr("relay.app.Boxes", lambda *args, **kwargs: None)  # providers' programs run here


FAKE_PODMAN = textwrap.dedent("""\
    #!{python}
    # Logs its calls; `exec` runs the command here, in the -w folder.
    import json, os, sys
    args = sys.argv[1:]
    with open({log!r}, "a") as log:
        log.write(json.dumps(args) + "\\n")
    if args[:2] == ["container", "inspect"]:
        sys.exit(1)  # none runs yet
    if args[:2] == ["image", "exists"] and os.environ.get("FAKE_NO_IMAGE"):
        sys.exit(1)
    if args[:1] == ["exec"]:
        where = args[args.index("-w") + 1]
        rest = args[1:]
        while rest[0].startswith("-"):
            rest = rest[2:] if rest[0] in ("-w", "-e") else rest[1:]
        os.chdir(where)
        program = "bash" if rest[1] == "/usr/lib/relay/bash.real" else rest[1]  # (a ProviderBox's real shell)
        os.execvp(program, rest[1:])
    """)


@pytest.fixture
def podman(tmp_path, monkeypatch):
    """A fake podman on PATH; the calls it got."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "podman.log"
    fake = bin_dir / "podman"
    fake.write_text(FAKE_PODMAN.format(python=sys.executable, log=str(log)))
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return calls


class HostBoxes:
    """A test box that is this machine (Boxes.test, where repairs are tested: relay.repair.shadow_test), and what
    each one was given."""
    settings = None

    def __init__(self):
        self.calls = []

    def test(self, command, mounts, env, cwd, image=None, timeout=300):
        self.calls.append({"mounts": mounts, "image": image, "env": env,
                           "templates": sorted(p.name for p in (Path(cwd) / "providers").iterdir())})
        return subprocess.run(command, env={**os.environ, **env}, cwd=cwd, capture_output=True, text=True,
                              timeout=timeout)


@pytest.fixture
def host_boxes():
    return HostBoxes()


@pytest.fixture(autouse=True)
def no_opencode_services(monkeypatch):
    """The default opencode provider would start this machine's own opencode service
    (e.g. when the agents tool lists models); tests that need one fake _backend."""
    def refuse(self, judge=None):
        raise ProviderError("tests start no opencode services")
    monkeypatch.setattr(OpencodeProvider, "_backend", refuse)
