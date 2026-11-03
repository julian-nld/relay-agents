import asyncio
from pathlib import Path

import pytest

from opencode_manager import paths, state
from opencode_manager.picker.app import DirectorySuggester, ProjectPickerApp, resolve


def run(coro):
    asyncio.run(coro)


@pytest.fixture
def stick(tmp_path, monkeypatch):
    """A fake portable folder at <tmp>/stick/opencode-manager."""
    root = tmp_path / "stick" / "opencode-manager"
    root.mkdir(parents=True)
    monkeypatch.setattr(paths, "root", lambda: root)
    return root


def test_recent_projects_most_recent_first_deduplicated_and_capped(stick, tmp_path):
    file = tmp_path / "state.json"
    dirs = [tmp_path / f"p{i}" for i in range(12)]
    for d in dirs:
        d.mkdir()
        state.remember_project(d, file)
    state.remember_project(dirs[5], file)
    recent = state.recent_projects(file)
    assert recent[0] == dirs[5]
    assert len(recent) == state.MAX_RECENT_PROJECTS
    assert recent.count(dirs[5]) == 1


def test_missing_projects_are_hidden(stick, tmp_path):
    file = tmp_path / "state.json"
    gone = tmp_path / "gone"
    gone.mkdir()
    state.remember_project(gone, file)
    gone.rmdir()
    assert state.recent_projects(file) == []


def test_projects_are_stored_absolute_and_old_relative_ones_still_resolve(stick, tmp_path):
    elsewhere = tmp_path / "home" / "code"
    elsewhere.mkdir(parents=True)

    assert state.encode_project(elsewhere) == str(elsewhere)
    assert state.decode_project(str(elsewhere)) == elsewhere
    assert state.decode_project("projects/site") == stick.parent / "projects" / "site"


def test_remember_profile_keeps_recent_projects(stick, tmp_path):
    file = tmp_path / "state.json"
    state.remember_project(tmp_path, file)
    state.remember_profile("work", file)
    loaded = state.load(file)
    assert loaded.active_profile == "work"
    assert loaded.recent_projects == [str(tmp_path)]


def test_suggester_completes_directories(tmp_path):
    (tmp_path / "code").mkdir()
    (tmp_path / "cool").mkdir()
    (tmp_path / "copy.txt").write_text("")
    (tmp_path / ".config").mkdir()
    suggester = DirectorySuggester(tmp_path)

    async def check():
        assert await suggester.get_suggestion("cod") == "code/"
        assert await suggester.get_suggestion("co") == "code/"
        assert await suggester.get_suggestion(str(tmp_path / "coo")) == str(tmp_path / "cool") + "/"
        assert await suggester.get_suggestion("code") is None  # already complete
        assert await suggester.get_suggestion("x") is None
        assert await suggester.get_suggestion(".con") == ".config/"
        assert await suggester.get_suggestion("") is None

    run(check())


def test_resolve_relative_to_current_and_home(tmp_path):
    assert resolve("sub", tmp_path) == tmp_path / "sub"
    assert resolve("~", tmp_path) == Path.home().resolve()


def picker(tmp_path, recent):
    current = tmp_path / "current"
    current.mkdir(exist_ok=True)
    output = tmp_path / "out.txt"
    output.write_text("")
    return ProjectPickerApp(current, recent, output), output


def test_picker_opens_typed_directory(tmp_path):
    (tmp_path / "current" / "sub").mkdir(parents=True)
    app, output = picker(tmp_path, [])

    async def scenario():
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press(*"sub", "enter")
            await pilot.pause()

    run(scenario())
    assert output.read_text() == str(tmp_path / "current" / "sub")


def test_picker_rejects_missing_directory(tmp_path):
    app, output = picker(tmp_path, [])

    async def scenario():
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press(*"nope", "enter")
            await pilot.pause()
            assert "Not a directory" in str(app.query_one("#error").content)
            assert app.is_running

    run(scenario())
    assert output.read_text() == ""


def test_picker_selects_recent_project_and_hides_current(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    app, output = picker(tmp_path, [tmp_path / "current", other])

    async def scenario():
        async with app.run_test(size=(100, 30)) as pilot:
            assert app.query_one("#recent").option_count == 1
            await pilot.press("tab", "enter")
            await pilot.pause()

    run(scenario())
    assert output.read_text() == str(other)


def test_picker_escape_cancels(tmp_path):
    app, output = picker(tmp_path, [])

    async def scenario():
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press(*"abc", "escape")
            await pilot.pause()

    run(scenario())
    assert output.read_text() == ""
