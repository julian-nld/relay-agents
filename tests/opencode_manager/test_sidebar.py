import asyncio
from pathlib import Path

import pytest

from opencode_manager import configfile, opencode
from opencode_manager.profiles import ProfileStore
from opencode_manager.settings import Settings
from opencode_manager.sidebar.app import SidebarApp


class FakeWorkspace:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.active_profile = "default"
        self.launched: list[tuple[str, bool]] = []
        self.edit_writes: str | None = None  # what the "user" saves in the editor
        self.picks: Path | None = None  # what the "user" picks in the project picker
        self.opened: list[Path] = []

    def pick_project(self):
        return self.picks

    def open_project(self, directory):
        self.opened.append(directory)
        self.directory = directory

    def edit_config(self, profile):
        if self.edit_writes is not None:
            configfile.save(configfile.config_file(profile), self.edit_writes)
        return None

    def launch(self, profile, *, login_first=False):
        self.launched.append((profile.name, login_first))
        self.active_profile = profile.name

    def focus_opencode(self):
        pass

    def quit(self):
        pass


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    store = ProfileStore(tmp_path / "profiles", base_port=50000)
    store.create("work")
    return SidebarApp(Settings(launch_mode="standalone"), store, FakeWorkspace(tmp_path))


def run(coro):
    asyncio.run(coro)


def lines(app) -> list[str]:
    return [str(item.query_one("Label").content) for item in app.query("ProfileItem")]


def test_lists_profiles_with_active_marker(app):
    async def scenario():
        async with app.run_test(size=(32, 40)):
            assert lines(app) == ["● default", "○ work"]

    run(scenario())


def test_switch_launches_selected_profile(app):
    async def scenario():
        async with app.run_test(size=(32, 40)) as pilot:
            await pilot.press("down", "enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.workspace.launched == [("work", False)]
            assert lines(app) == ["○ default", "● work"]

    run(scenario())


def test_login_launches_with_login_first(app):
    async def scenario():
        async with app.run_test(size=(32, 40)) as pilot:
            await pilot.press("down", "a")
            await app.workers.wait_for_complete()
            assert app.workspace.launched == [("work", True)]

    run(scenario())


def test_create_profile_and_wrapping_status(app):
    async def scenario():
        async with app.run_test(size=(32, 40)) as pilot:
            await pilot.press("n", *"client-x", "enter")
            await pilot.pause()
            assert "client-x" in [p.name for p in app.store.list()]
            assert lines(app) == ["● default", "○ client-x", "○ work"]
            status = app.query_one("#status")
            assert status.size.height >= 2, "long status messages must wrap"

    run(scenario())


def test_cannot_delete_active_profile(app):
    async def scenario():
        async with app.run_test(size=(32, 40)) as pilot:
            await pilot.press("d")
            await pilot.pause()
            assert app.store.exists("default")
            assert "can't be deleted" in str(app.query_one("#status").content)

    run(scenario())


def make_service_app(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    calls = []
    monkeypatch.setattr(opencode, "_run", lambda s, p, *args, **kw: calls.append((p.name, args)))
    store = ProfileStore(tmp_path / "profiles", base_port=50000)
    store.create("work")
    workspace = FakeWorkspace(tmp_path)
    workspace.active_profile = "work"
    return SidebarApp(Settings(launch_mode="service"), store, workspace), calls


def test_edit_then_apply_restarts_service_and_opencode(tmp_path, monkeypatch):
    app, calls = make_service_app(tmp_path, monkeypatch)
    app.workspace.edit_writes = '{"model": "x/y"}\n'

    async def scenario():
        async with app.run_test(size=(32, 40)) as pilot:
            await pilot.press("down", "e")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert "restarts work" in str(app.screen.query_one("Label").content)
            await pilot.press("y")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert calls == [("work", ("service", "stop"))]
            assert app.workspace.launched == [("work", False)]
            assert "Applied" in str(app.query_one("#status").content)

    run(scenario())


def test_edit_without_changes_does_nothing(tmp_path, monkeypatch):
    app, calls = make_service_app(tmp_path, monkeypatch)

    async def scenario():
        async with app.run_test(size=(32, 40)) as pilot:
            await pilot.press("down", "e")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert len(app.screen_stack) == 1
            assert "No changes" in str(app.query_one("#status").content)
            assert calls == [] and app.workspace.launched == []

    run(scenario())


def test_invalid_external_edit_is_reported_not_applied(tmp_path, monkeypatch):
    app, calls = make_service_app(tmp_path, monkeypatch)
    app.workspace.edit_writes = '{"model": }'

    async def scenario():
        async with app.run_test(size=(32, 40)) as pilot:
            await pilot.press("down", "e")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert len(app.screen_stack) == 1
            assert "is invalid" in str(app.query_one("#status").content)
            assert calls == [] and app.workspace.launched == []

    run(scenario())


def test_pick_project_opens_it_and_updates_sidebar(app, tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    app.workspace.picks = target

    async def scenario():
        async with app.run_test(size=(32, 40)) as pilot:
            await pilot.press("p")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.workspace.opened == [target]
            assert str(app.query_one("#project").content).endswith("elsewhere")

    run(scenario())


def test_cancelled_or_same_project_does_nothing(app):
    async def scenario():
        async with app.run_test(size=(32, 40)) as pilot:
            await pilot.press("p")
            await app.workers.wait_for_complete()
            app.workspace.picks = app.workspace.directory
            await pilot.press("p")
            await app.workers.wait_for_complete()
            assert app.workspace.opened == []

    run(scenario())


def test_long_project_path_is_shortened_to_one_line(tmp_path):
    from opencode_manager.sidebar.app import _fit

    assert _fit("/short", 30) == "/short"
    long = "/very/long/path/to/some/deeply/nested/project-name"
    assert _fit(long, 30) == "…" + long[-29:]
    assert len(_fit(long, 30)) == 30


def test_sidebar_follows_changes_made_by_scripts(app, tmp_path):
    other = tmp_path / "other-project"
    other.mkdir()

    async def scenario():
        async with app.run_test(size=(32, 40)) as pilot:
            assert lines(app) == ["● default", "○ work"]
            # What `opencode-manager switch/open/profile create` do elsewhere:
            app.workspace.active_profile = "work"
            app.workspace.directory = other
            app.store.create("scripted")
            await pilot.pause(1.5)
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert lines(app) == ["○ default", "○ scripted", "● work"]
            assert str(app.query_one("#project").content).endswith("other-project")

    run(scenario())
