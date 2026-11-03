import asyncio

from opencode_manager import configfile
from opencode_manager.editor.app import EditorApp


def run(coro):
    asyncio.run(coro)


def status(app) -> str:
    return str(app.query_one("#status").content)


def test_new_file_starts_from_template_and_closes_without_prompt(tmp_path):
    path = tmp_path / "opencode.json"
    app = EditorApp(path, "work", is_default=False)

    async def scenario():
        async with app.run_test(size=(100, 30)) as pilot:
            assert app.editor.text == configfile.TEMPLATE
            assert "New file" in status(app)
            await pilot.press("ctrl+q")
            await pilot.pause()
        assert app.return_code == 0
        assert not path.exists()

    run(scenario())


def test_save_valid_config(tmp_path):
    path = tmp_path / "opencode.json"
    path.write_text('{\n  "model": "a"\n}\n')
    app = EditorApp(path, "work", is_default=False)

    async def scenario():
        async with app.run_test(size=(100, 30)) as pilot:
            app.editor.text = '{\n  // comment\n  "model": "b",\n}\n'
            await pilot.pause()
            assert "●" in str(app.query_one("#title").content)
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert "Saved" in status(app)
            assert "●" not in str(app.query_one("#title").content)
        assert path.read_text() == '{\n  // comment\n  "model": "b",\n}\n'

    run(scenario())


def test_invalid_config_is_not_saved_and_cursor_jumps_to_error(tmp_path):
    path = tmp_path / "opencode.json"
    path.write_text("{}\n")
    app = EditorApp(path, "work", is_default=False)

    async def scenario():
        async with app.run_test(size=(100, 30)) as pilot:
            app.editor.text = '{\n  "a": 1\n  "b": 2\n}\n'
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert "Not saved. Line 3, column 3" in status(app)
            assert app.editor.cursor_location == (2, 2)
        assert path.read_text() == "{}\n"

    run(scenario())


def test_close_with_unsaved_changes_asks_first(tmp_path):
    path = tmp_path / "opencode.json"
    path.write_text("{}\n")
    app = EditorApp(path, "work", is_default=False)

    async def scenario():
        async with app.run_test(size=(100, 30)) as pilot:
            app.editor.text = '{"a": 1}'
            await pilot.press("ctrl+q")
            await pilot.pause()
            assert app.is_running and len(app.screen_stack) == 2
            await pilot.press("n")
            await pilot.pause()
            assert app.is_running and len(app.screen_stack) == 1
            await pilot.press("ctrl+q", "y")
            await pilot.pause()
        assert path.read_text() == "{}\n"

    run(scenario())


def test_default_profile_is_labelled_as_machine_config(tmp_path):
    app = EditorApp(tmp_path / "opencode.json", "default", is_default=True)

    async def scenario():
        async with app.run_test(size=(100, 30)):
            assert "this machine's own opencode config" in str(app.query_one("#title").content)

    run(scenario())
