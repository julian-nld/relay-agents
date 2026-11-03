"""Project picker: choose the directory opencode runs in.

Runs in a tmux popup opened by the sidebar and writes the chosen directory to
an output file (empty if cancelled).
"""

from __future__ import annotations

import os
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.suggester import Suggester
from textual.widgets import Input, Label, OptionList, Static
from textual.widgets.option_list import Option

HELP = "[b]enter[/] open   [b]→[/] complete   [b]tab[/] recent list   [b]esc[/] cancel"


class DirectorySuggester(Suggester):
    """Completes the last path component to an existing directory."""

    def __init__(self, base: Path) -> None:
        super().__init__(use_cache=False, case_sensitive=True)
        self.base = base

    async def get_suggestion(self, value: str) -> str | None:
        if not value or value.endswith(os.sep):
            return None
        typed = Path(os.path.expanduser(value))
        parent = typed.parent if typed.is_absolute() else self.base / typed.parent
        prefix = typed.name
        try:
            names = sorted(
                entry.name
                for entry in os.scandir(parent)
                if entry.is_dir()
                and entry.name.startswith(prefix)
                and (prefix.startswith(".") or not entry.name.startswith("."))
            )
        except OSError:
            return None
        if not names or names[0] == prefix:
            return None
        return value + names[0][len(prefix):] + os.sep


def resolve(value: str, base: Path) -> Path:
    path = Path(os.path.expanduser(value.strip()))
    return (path if path.is_absolute() else base / path).resolve()


def short(path: Path) -> str:
    try:
        return "~/" + path.relative_to(Path.home()).as_posix()
    except ValueError:
        return str(path)


class ProjectPickerApp(App):
    CSS_PATH = "picker.tcss"
    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

    def __init__(self, current: Path, recent: list[Path], output: Path) -> None:
        super().__init__()
        self.current = current
        self.recent = [p for p in recent if p != current]
        self.output = output

    def compose(self) -> ComposeResult:
        yield Static(f"Open project · current: [b]{short(self.current)}[/]", id="title")
        yield Input(
            placeholder="Type a directory path…",
            suggester=DirectorySuggester(self.current),
            id="path",
        )
        yield Label("", id="error")
        yield Label("RECENT", classes="section")
        if self.recent:
            yield OptionList(*(Option(short(p), id=str(p)) for p in self.recent), id="recent")
        else:
            yield Static("No other recent projects yet.", id="empty")
        yield Static(HELP, id="help")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if not event.value.strip():
            return
        path = resolve(event.value, self.current)
        if not path.is_dir():
            self.query_one("#error", Label).update(f"Not a directory: {path}")
            return
        self._choose(path)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self._choose(Path(event.option.id))

    def action_cancel(self) -> None:
        self.exit()

    def _choose(self, path: Path) -> None:
        self.output.write_text(str(path))
        self.exit()
