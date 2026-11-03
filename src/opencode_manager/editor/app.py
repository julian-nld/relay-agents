"""A small built-in editor for a profile's opencode.json.

It runs in a tmux popup opened by the sidebar, but also works on its own
(`opencode-manager edit <profile>`). It ships with the code, so it behaves the
same on every machine regardless of which editors are installed.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Static, TextArea

from opencode_manager import configfile
from opencode_manager.sidebar.modals import ConfirmScreen

HELP = "[b]ctrl+s[/] save   [b]ctrl+q[/] close"


class EditorApp(App):
    CSS_PATH = "editor.tcss"
    BINDINGS = [
        Binding("ctrl+s", "save", "Save", priority=True),
        Binding("ctrl+q", "close", "Close", priority=True),
    ]

    def __init__(self, path: Path, profile_name: str, is_default: bool) -> None:
        super().__init__()
        self.path = path
        self.profile_name = profile_name
        self.is_default = is_default
        self.saved_text = configfile.read(path)

    @property
    def baseline(self) -> str:
        """Text the editor considers unmodified."""
        return self.saved_text if self.saved_text is not None else configfile.TEMPLATE

    @property
    def editor(self) -> TextArea:
        return self.query_one(TextArea)

    @property
    def modified(self) -> bool:
        return self.editor.text != self.baseline

    def compose(self) -> ComposeResult:
        yield Static(id="title")
        yield TextArea.code_editor(self.baseline, language=None, id="text")
        yield Static(id="status")
        yield Static(HELP, id="help")

    def on_mount(self) -> None:
        self._update_title()
        if self.saved_text is None:
            self._status("New file. Save to create it.")
        self.editor.focus()

    def on_text_area_changed(self) -> None:
        self._update_title()

    def _update_title(self) -> None:
        marker = " ●" if self.modified else ""
        owner = (
            "[b]default[/] [$warning](this machine's own opencode config)[/]"
            if self.is_default
            else f"profile [b]{self.profile_name}[/]"
        )
        self.query_one("#title", Static).update(f"{self.path.name}{marker} · {owner}")

    def _status(self, message: str, error: bool = False) -> None:
        status = self.query_one("#status", Static)
        status.update(message)
        status.set_class(error, "error")

    def action_save(self) -> None:
        text = self.editor.text
        if error := configfile.validate(text):
            self._status(f"Not saved. {error}", error=True)
            self.editor.move_cursor((error.line - 1, error.column - 1))
            return
        try:
            configfile.save(self.path, text)
        except OSError as exc:
            self._status(f"Not saved: {exc}", error=True)
            return
        self.saved_text = text
        self._update_title()
        self._status("Saved")

    def action_close(self) -> None:
        if not self.modified:
            self.exit()
            return

        def confirmed(yes: bool | None) -> None:
            if yes:
                self.exit()

        self.push_screen(ConfirmScreen("Discard unsaved changes?"), confirmed)
