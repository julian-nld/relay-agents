"""Modal dialogs used by the sidebar."""

from __future__ import annotations

from dataclasses import dataclass

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label

from opencode_manager.profiles import ProfileError, validate_name


@dataclass
class NewProfileRequest:
    name: str
    copy_config: bool


class NewProfileScreen(ModalScreen[NewProfileRequest | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, existing: set[str]) -> None:
        super().__init__()
        self.existing = existing

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("New profile", classes="dialog-title")
            yield Input(placeholder="name", id="name")
            yield Checkbox("Copy default config", value=True, id="copy")
            yield Label("", id="error", classes="error")
            with Horizontal(classes="buttons"):
                yield Button("Create", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    def on_input_submitted(self) -> None:
        self._submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            self._submit()
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _submit(self) -> None:
        name = self.query_one("#name", Input).value.strip()
        try:
            validate_name(name)
            if name in self.existing:
                raise ProfileError(f"{name!r} already exists")
        except ProfileError as exc:
            self.query_one("#error", Label).update(str(exc))
            return
        self.dismiss(NewProfileRequest(name, self.query_one("#copy", Checkbox).value))


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("escape,n", "answer(False)", "No"),
        Binding("y", "answer(True)", "Yes"),
    ]

    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.question)
            with Horizontal(classes="buttons"):
                yield Button("Yes", variant="error", id="yes")
                yield Button("No", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_answer(self, answer: bool) -> None:
        self.dismiss(answer)
