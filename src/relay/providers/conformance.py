"""Whether a provider's layer does what Relay needs of it (docs/ADDING-A-PROVIDER.md): the check behind Settings →
Providers → Check it fully, `relay provider-check` and the provider folders' own tests.

It runs the provider as a run does (its program in its ProviderBox, its commands in a WorkBox: RunManager.place),
in a scratch folder, and looks at what comes back: every event as events.py describes it, a session, one "done"
last. The scenarios: it loads, check(), its models, a turn, a turn going on in that session, a turn stopped
halfway, and a command run (an agent with tools). A real model answers the prompts: a few short ones.
"""

from __future__ import annotations

import shutil
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from opencode_manager.bridge.events import DONE_STATUSES
from relay import tunables
from relay.providers.base import Context, Provider, ProviderError

EFFORTS = ("low", "medium", "high", "xhigh", "max")
STOP_WITHIN = 15  # seconds a stopped turn may take to say it stopped

# Each event type and its fields that must be there, with their types (events.py); others may be added
FIELDS: dict[str, dict[str, Any]] = {
    "session": {"session": str},
    "step": {},
    "text": {"delta": str},
    "reasoning": {"delta": str},
    "tool_call": {"id": str, "name": str},
    "tool_result": {"id": str, "ok": bool},
    "permission": {"id": str, "action": str},
    "form": {"id": str, "status": str},
    "usage": {},
    "limit": {"status": str},
    "title": {"title": str},
    "turn": {"id": str},
    "error": {"message": str},
    "done": {"status": str},
}
CHOICES = {("done", "status"): DONE_STATUSES, ("limit", "status"): ("allowed", "warning", "rejected"),
           ("form", "status"): ("pending", "answered", "cancelled"),
           ("usage", "billing"): ("api", "subscription", "free", None)}


def validate_events(events: list[dict[str, Any]]) -> list[str]:
    """What's wrong with a run's events as Relay reads them (none: [])."""
    problems = []
    for index, event in enumerate(events):
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            problems.append(f"event {index + 1} isn't an object with a type: {event!r:.80}")
            continue
        kind = event["type"]
        if kind not in FIELDS:
            problems.append(f"event {index + 1}: Relay doesn't know the type {kind!r} (it's left out)")
            continue
        for name, wanted in FIELDS[kind].items():
            if not isinstance(event.get(name), wanted):
                problems.append(f"event {index + 1} ({kind}): {name!r} must be a {wanted.__name__}, "
                                f"not {event.get(name)!r:.40}")
        for (of, name), allowed in CHOICES.items():
            if kind == of and name in event and event[name] not in allowed:
                problems.append(f"event {index + 1} ({kind}): {name} {event[name]!r} isn't one of "
                                f"{', '.join(map(repr, allowed))}")
    called = [e.get("id") for e in events if isinstance(e, dict) and e.get("type") == "tool_call"]
    answered = [e.get("id") for e in events if isinstance(e, dict) and e.get("type") == "tool_result"]
    for index, event in enumerate(events):  # (a result goes with its call: the page shows them as one)
        if isinstance(event, dict) and event.get("type") == "tool_result" and event.get("id") not in called:
            problems.append(f"event {index + 1} (tool_result): no tool_call has its id {event.get('id')!r}")
    kinds = [e.get("type") for e in events if isinstance(e, dict)]
    if kinds and kinds[-1] == "done" and events[-1].get("status") == "succeeded":
        open_calls = [c for c in called if c not in answered]
        if open_calls:
            problems.append(f"tool_call {open_calls[0]!r} got no tool_result, though the run succeeded")
    if kinds.count("done") != 1:
        problems.append(f"a run ends with one \"done\" event, not {kinds.count('done')}")
    elif kinds[-1] != "done":
        problems.append("\"done\" must be the last event")
    elif events[-1].get("status") == "succeeded" and "session" not in kinds:
        problems.append("no \"session\" event: Relay can't go on in this session next turn")
    return problems


class Turn:
    """One run of the provider, its events collected as they come (in a thread of its own)."""

    def __init__(self, provider: Provider, context: Context, prompt: str,
                 after: Callable[[], Any] | None = None) -> None:
        self.after = after  # (what placing it asks to be done when it's over)
        self.events: list[dict[str, Any]] = []
        self.error: str | None = None
        self.started = time.monotonic()
        self.ended: float | None = None
        self.text = threading.Event()  # (its first text came)
        self.run = None
        self.thread = threading.Thread(target=self._go, args=(provider, context, prompt), daemon=True)
        self.thread.start()

    def _go(self, provider: Provider, context: Context, prompt: str) -> None:
        events = None
        try:
            self.run = provider.start(context, prompt)
            events = self.run.events()
            for event in events:
                self.events.append(event)
                if isinstance(event, dict) and event.get("type") == "text" and event.get("delta"):
                    self.text.set()
        except Exception as exc:  # noqa: BLE001 - whatever the layer raised is the answer
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            # (as a run ends: the provider's own cleanup, and what it started in its box)
            for end in (getattr(events, "close", None), getattr(context.box, "end", None), self.after):
                try:
                    if end is not None:
                        end()
                except Exception:  # noqa: BLE001 - never lose the result over it
                    pass
            self.ended = time.monotonic()
            self.text.set()

    def wait(self, seconds: float) -> bool:
        self.thread.join(seconds)
        if self.thread.is_alive() and self.run is not None:
            self.run.interrupt()
            self.thread.join(STOP_WITHIN)
        return not self.thread.is_alive()

    @property
    def status(self) -> str | None:
        done = [e for e in self.events if isinstance(e, dict) and e.get("type") == "done"]
        return done[-1].get("status") if done else None

    @property
    def answer(self) -> str:
        return " ".join("".join(str(e.get("delta") or "") for e in self.events
                                if isinstance(e, dict) and e.get("type") == "text").split())

    @property
    def session(self) -> str | None:
        found = [e.get("session") for e in self.events if isinstance(e, dict) and e.get("type") == "session"]
        return found[-1] if found else None

    def seconds(self) -> float:
        return round((self.ended or time.monotonic()) - self.started, 1)

    def said(self) -> str:
        """How it ended, in a few words."""
        errors = [str(e.get("message")) for e in self.events if isinstance(e, dict) and e.get("type") == "error"]
        return self.error or (errors[-1] if errors else f"it ended {self.status or 'without a done event'}")


def check(provider: Provider, model: str | None, scratch: Path,
          place: Callable[[Provider, Context], Any] | None = None,
          discard: Callable[[Path], Any] | None = None) -> list[dict[str, Any]]:
    """The scenarios, each {name, ok (True, False, or None: not shown), detail}. `place` puts a turn where Relay
    runs a run (RunManager.place_check: its box, its WorkBox, its permission requests answered), and may return
    what to call when it's over; without, here. `discard` takes the scratch folder's WorkBox away after."""
    results: list[dict[str, Any]] = []

    def add(name: str, ok: bool | None, detail: str) -> None:
        results.append({"name": name, "ok": ok, "detail": detail})

    kind = type(provider)
    add("Loads", True, f"kind {kind.KIND} from {Path(kind.support_files()[0]).parent}/")
    try:
        add("check()", True, str(provider.check()))
    except ProviderError as exc:
        add("check()", False, f"it says: {exc}")
    except Exception as exc:  # noqa: BLE001 - a layer's mistake: said
        add("check()", False, f"it raised {type(exc).__name__}: {exc} (a ProviderError says why)")
    try:
        models = provider.models()
        wrong = [m for m in models if not (isinstance(m, dict) and isinstance(m.get("id"), str)
                                           and isinstance(m.get("name"), str))]
        efforts = [e for e in provider.efforts(model or None) if e not in EFFORTS]
        add("Models", not wrong and not efforts and bool(models),
            f"{len(models)} model{'s' * (len(models) != 1)}" if models and not wrong and not efforts else
            f"models() must give [{{id, name}}]: {wrong[:2]!r}" if wrong else
            f"efforts must be of {', '.join(EFFORTS)}: {efforts}" if efforts else "models() gave none")
    except Exception as exc:  # noqa: BLE001
        add("Models", False, f"it raised {type(exc).__name__}: {exc}")
    folder = scratch / f"check-{uuid.uuid4().hex[:12]}"
    folder.mkdir(parents=True, exist_ok=True)
    limit = tunables.get("tests.timeout")
    try:
        def turn(prompt: str, session: str | None = None, mode: str = "chat") -> Turn:
            context = Context(conversation_id=folder.name, directory=folder, mode=mode, model=model or None,
                              provider_session=session)
            after = place(provider, context) if place is not None else None
            return Turn(provider, context, prompt, after if callable(after) else None)

        first = turn(tunables.get("tests.prompt"))
        first.wait(limit)
        problems = validate_events(first.events)
        if first.error or first.status != "succeeded" or problems or not first.answer:
            add("A turn", False, "; ".join(problems[:3]) if problems and not first.error else
                first.said() if first.status != "succeeded" or first.error else "it gave no text")
        else:
            add("A turn", True, f"answered in {first.seconds():g} s: “{first.answer[:60]}”")
        if first.session and first.status == "succeeded":
            again = turn("Reply with just the word OK again.", first.session)
            again.wait(limit)
            problems = validate_events(again.events)
            ok = not again.error and again.status == "succeeded" and not problems
            add("Going on", ok, f"in session {first.session[:24]}, {again.seconds():g} s" if ok else
                "; ".join(problems[:3]) or again.said())
        else:
            add("Going on", None, "not tried: the first turn failed" if first.status != "succeeded"
                else "not tried: the first turn gave no session")
        counting = turn("Count from 1 to 300, one number per line, and nothing else.")
        counting.text.wait(limit)
        if counting.status is None and counting.run is not None:
            asked = time.monotonic()
            counting.run.interrupt()
            counting.thread.join(STOP_WITHIN)
            took = round(time.monotonic() - asked, 1)
            stopped = counting.status == "interrupted" and not counting.thread.is_alive()
            add("Stopping", stopped, (f"stopped in {took:g} s" if took >= 0.1 else "stopped at once") if stopped else
                f"after {took:g} s: {counting.said() if not counting.thread.is_alive() else 'still going'}"
                + " (a stopped run ends with done: interrupted)")
        else:
            add("Stopping", None if counting.status == "succeeded" else False,
                "not shown: it finished before it could be stopped" if counting.status == "succeeded"
                else counting.said())
            counting.wait(STOP_WITHIN)
        if provider.has_tools():
            token = uuid.uuid4().hex[:10]
            command = turn(f"Run this shell command and tell me what it printed: echo relay-check-{token}",
                           mode="code")
            command.wait(limit)
            called = [e for e in command.events if isinstance(e, dict) and e.get("type") == "tool_call"]
            came = f"relay-check-{token}" in command.answer or any(
                f"relay-check-{token}" in str(e.get("output")) for e in command.events
                if isinstance(e, dict) and e.get("type") == "tool_result")
            problems = validate_events(command.events)
            ok = bool(called) and came and not problems and not command.error
            results_ = [e for e in command.events if isinstance(e, dict)
                        and e.get("type") in ("tool_result", "permission")]
            seen = (" (it got: "
                    f"{' '.join(str(results_[-1].get('output') or results_[-1].get('outcome') or '').split())[:120]})"
                    if results_ else "")
            add("A command", ok, f"its tool “{called[0].get('name')}” ran it, and its output came back" if ok else
                "; ".join(problems[:3]) or ("no tool_call event" if not called else
                                             "its output didn't come back" + seen if not came else command.said()))
    finally:
        if discard is not None:
            try:
                discard(folder)
            except Exception:  # noqa: BLE001 - an idle WorkBox goes by itself later
                pass
        shutil.rmtree(folder, ignore_errors=True)
    return results
