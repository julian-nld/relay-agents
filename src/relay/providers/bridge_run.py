"""A turn of an agent that runs through an opencode-manager bridge backend."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from opencode_manager.bridge.http import HttpError
from relay.providers.base import Event, ProviderError, ProviderRun


class BridgeRun(ProviderRun):
    """Runs a bridge backend turn, announcing the session first."""

    def __init__(self, backend, session_id: str, prompt: str, new: bool, **run_options) -> None:
        self.backend = backend
        self.session_id = session_id
        self.prompt = prompt
        self.new = new
        self.run_options = run_options  # extra arguments for backend.run()
        self.stopped = False
        self.interrupts_sent = 0

    def events(self) -> Iterator[Event]:
        yield {"type": "session", "session": self.session_id, "new": self.new}
        for event in self.backend.run(self.session_id, self.prompt, **self.run_options):
            # A stop that came before the backend had started (e.g. before
            # opencode received the prompt) had nothing to interrupt yet.
            if self.stopped and self.interrupts_sent < 2 and event.get("type") != "done":
                self.interrupts_sent += 1
                self.backend.interrupt(self.session_id)
            yield event

    def interrupt(self) -> None:
        self.stopped = True
        self.interrupts_sent += 1
        self.backend.interrupt(self.session_id)

    def waits_for_answers(self) -> bool:
        return hasattr(self.backend, "answer")  # opencode leaves requests open until answered

    def answer_permission(self, request_id: str, approve: bool) -> None:
        if not self.waits_for_answers():
            super().answer_permission(request_id, approve)
        self.backend.answer(request_id, approve)

    def answer_form(self, request_id: str, answer: dict[str, Any] | None) -> None:
        if not hasattr(self.backend, "answer_form"):
            super().answer_form(request_id, answer)
        try:
            self.backend.answer_form(request_id, answer)
        except HttpError as exc:
            raise ProviderError(str(exc)) from exc
