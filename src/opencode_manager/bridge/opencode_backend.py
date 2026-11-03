"""opencode backend: drives a session through the service's HTTP API.

The service publishes events for all sessions on one server-sent-events
stream (/api/event). A run subscribes first, then sends the prompt, and
translates the events of its own session until the execution ends.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

from opencode_manager.bridge.billing import opencode_billing
from opencode_manager.bridge.events import DONE_STATUSES, Event, event
from opencode_manager.bridge.http import Client, HttpError


class Translator:
    """Turns opencode events for one session into uniform events."""

    def __init__(self, session_id: str,
                 billing: Callable[[str], str | None] | None = None) -> None:
        self.session_id = session_id
        self.billing = billing  # model ("provider/id") -> billing kind
        self.model: str | None = None
        self.tool_names: dict[str, str] = {}
        self.done = False

    def concerns(self, raw: dict[str, Any]) -> bool:
        return _data(raw).get("sessionID") == self.session_id

    def translate(self, raw: dict[str, Any]) -> list[Event]:
        kind, data = raw.get("type", ""), _data(raw)
        if kind == "session.step.started":
            self.model = model_name(data.get("model") or {}) or self.model
            return [event("step", agent=data.get("agent"), model=self.model)]
        if kind == "session.text.delta":
            return [event("text", delta=data.get("delta", ""))]
        if kind == "session.reasoning.delta":
            return [event("reasoning", delta=data.get("delta", ""))]
        if kind == "session.tool.input.started":
            self.tool_names[data.get("id")] = data.get("name")
            return []
        if kind == "session.tool.called":
            tool_id = data.get("id")
            return [event("tool_call", id=tool_id, name=self.tool_names.get(tool_id),
                          input=data.get("input"))]
        if kind in ("session.tool.success", "session.tool.failed"):
            tool_id = data.get("id")
            ok = kind == "session.tool.success"
            output = _content_text(data.get("content")) or (None if ok else _error_text(data))
            return [event("tool_result", id=tool_id, name=self.tool_names.get(tool_id), ok=ok,
                          output=output)]
        if kind == "session.renamed" and data.get("title"):
            return [event("title", title=data["title"])]
        if kind == "session.usage.updated":
            billing = self.billing(self.model) if self.billing and self.model else None
            return [event("usage", tokens=data.get("tokens"), cost=data.get("cost"), billing=billing)]
        if kind in ("session.error", "session.step.failed"):
            return [event("error", message=_error_text(data))]
        if kind.startswith("session.execution.") and kind.rsplit(".", 1)[1] in DONE_STATUSES:
            self.done = True
            status = kind.rsplit(".", 1)[1]
            # A failure may say why only here, e.g. {"type": "provider.no-route", "message": "Model unavailable: …"}.
            why = []
            if status == "failed":
                # Without a reason the answer would just stop: say so, at least.
                why = [event("error", message=_error_text(data) if data.get("error") else
                             "opencode ended this turn as failed without saying why. Try again; if it keeps "
                             "happening, another model may help.")]
            return [*why, event("done", status=status)]
        return []


class OpencodeBackend:
    name = "opencode"

    def __init__(self, client: Client, auto_approve: bool = True,
                 judge: Callable[[str, list[str]], str | None] | None = None) -> None:
        self.client = client
        self.stopped = threading.Event()
        # Answers the permission requests opencode makes: (action, resources) -> None to
        # allow, else why not. Without one, everything the config doesn't deny is allowed
        # (like `opencode --auto`), or, without auto_approve, left for someone else.
        self.judge = judge
        self.auto_approve = auto_approve
        self._parents: dict[str, str | None] = {}
        self._billing: dict[str, str | None] = {}
        self._forms: dict[str, str] = {}  # form id -> owning session, including native subagents

    def create_session(
        self, directory: Path, model: str | None = None, agent: str | None = None
    ) -> str:
        body: dict[str, Any] = {"location": {"directory": str(directory)}}
        if model:
            provider, _, model_id = model.partition("/")
            body["model"] = {"providerID": provider, "id": model_id}
        if agent:
            body["agent"] = agent
        return self.client.request("POST", "/api/session", body)["data"]["id"]

    def fork(self, session_id: str, before: str | None = None) -> str:
        """A copy of the session (up to the message `before`, if given); its id."""
        body = {"before": before} if before else {}
        return self.client.request("POST", f"/api/session/{session_id}/fork", body)["data"]["id"]

    def move(self, session_id: str, directory: Path) -> None:
        """Let the session work in `directory` from now on (e.g. the folder moved)."""
        self.client.request("POST", f"/api/session/{session_id}/move", {"directory": str(directory)})

    def set_agent(self, session_id: str, agent: str) -> None:
        """Use opencode's `agent` (e.g. build or plan) for the session's next turns."""
        self.client.request("POST", f"/api/session/{session_id}/agent", {"agent": agent})

    def set_model(self, session_id: str, model: str, variant: str | None = None) -> None:
        """Use `model` ("provider/id") for the session's next turns, in a
        `variant` (e.g. a reasoning effort: low, medium, high)."""
        provider, _, model_id = model.partition("/")
        chosen = {"providerID": provider, "id": model_id}
        if variant:
            chosen["variant"] = variant
        self.client.request("POST", f"/api/session/{session_id}/model", {"model": chosen})

    def commands(self, directory: Path) -> list[dict[str, str]]:
        """opencode's commands for a folder (built in, like init and review, and your own)."""
        found = self.client.request("GET", "/api/command?directory=" + quote(str(directory)))["data"]
        return [{"name": c["name"], "description": c.get("description") or ""}
                for c in found if isinstance(c, dict) and c.get("name")]

    def variants(self, directory: Path, model: str | None) -> list[str]:
        """The variants (reasoning efforts) a model offers; the default model's without `model`."""
        where = "?directory=" + quote(str(directory))
        if model:
            provider, _, model_id = model.partition("/")
        else:
            default = self.client.request("GET", "/api/model/default" + where).get("data") or {}
            provider, model_id = default.get("providerID"), default.get("id")
        for found in self.client.request("GET", "/api/model" + where)["data"]:
            if found.get("providerID") == provider and found.get("id") == model_id:
                return [v["id"] for v in found.get("variants") or [] if isinstance(v, dict) and v.get("id")]
        return []

    def all_variants(self, directory: Path) -> dict[str, list[str]]:
        """Every model's variants (reasoning efforts), by "provider/id"."""
        where = "?directory=" + quote(str(directory))
        return {f"{m.get('providerID')}/{m.get('id')}":
                [v["id"] for v in m.get("variants") or [] if isinstance(v, dict) and v.get("id")]
                for m in self.client.request("GET", "/api/model" + where)["data"]}

    def set_permissions(self, session_id: str, rules: list[dict[str, str]] | None) -> None:
        """The session's own permission rules ({action, resource, effect}), over the profile's
        config; None: the config's alone."""
        self.client.request("PATCH", f"/api/session/{session_id}", {"permissions": rules})

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        try:
            return self.client.request("GET", f"/api/session/{session_id}")["data"]
        except HttpError as exc:
            if exc.status == 404:
                return None
            raise

    def run(self, session_id: str, text: str, files: list[dict[str, str]] | None = None,
            command: str | None = None) -> Iterator[Event]:
        """Send `text` to the session and yield uniform events until it is done.

        On KeyboardInterrupt the caller should call interrupt().
        """
        translator = Translator(session_id, lambda model: self.billing(session_id, model))
        source = self.client.events("/api/event", stopped=self.stopped)
        stream = (raw for raw in source if isinstance(raw, dict))
        try:
            # Subscribe before prompting, so no event of this run is missed.
            for raw in stream:
                if raw.get("type") == "server.connected":
                    break
            if self.stopped.is_set():
                yield event("done", status="interrupted")
                return
            prompt: dict[str, Any] = {"text": text}
            if files:
                prompt["files"] = files  # [{"uri": "file:///…", "name": …}]
            if command:
                # One of opencode's commands (e.g. init, review): `text` holds its arguments.
                self.client.request("POST", f"/api/session/{session_id}/command", {"name": command, **prompt})
            else:
                self.client.request("POST", f"/api/session/{session_id}/prompt", prompt)
            if self.stopped.is_set():
                # An interrupt raced with prompting: the first request may have
                # reached the server before there was an execution to interrupt.
                try:
                    self.client.request("POST", f"/api/session/{session_id}/interrupt")
                except HttpError:
                    pass  # cancellation still ends the local turn
            for raw in stream:
                if self.stopped.is_set():
                    break
                if raw.get("type") in ("form.created", "form.replied", "form.cancelled"):
                    data = _data(raw)
                    form = data.get("form") if raw["type"] == "form.created" else data
                    if not isinstance(form, dict) or not form.get("id") or not form.get("sessionID"):
                        continue
                    if self._descends_from(form["sessionID"], session_id):
                        if raw["type"] == "form.created":
                            self._forms[form["id"]] = form["sessionID"]
                            yield event("form", id=form["id"], title=form.get("title", "Questions"),
                                        fields=form.get("fields", []), status="pending")
                        else:
                            self._forms.pop(form["id"], None)
                            yield event("form", id=form["id"],
                                        status="answered" if raw["type"] == "form.replied" else "cancelled",
                                        **{k: form[k] for k in ("answer", "message") if k in form})
                    continue
                if raw.get("type") == "permission.asked":
                    # Subagents run in child sessions; their requests block
                    # this run just the same.
                    asking = _data(raw).get("sessionID")
                    if asking and self._descends_from(asking, session_id):
                        yield self._permission(asking, _data(raw))
                    continue
                if not translator.concerns(raw):
                    continue
                out = translator.translate(raw)
                if translator.done:
                    turn = self._last_prompt_id(session_id)
                    if turn:  # just before "done", which must stay last
                        out = [*out[:-1], event("turn", id=turn), out[-1]]
                    yield from out
                    return
                yield from out
            if self.stopped.is_set():
                yield event("done", status="interrupted")
                return
            yield event("error", message="The event stream ended unexpectedly")
            yield event("done", status="failed")
        finally:
            source.close()  # ends the HTTP stream

    def _last_prompt_id(self, session_id: str) -> str | None:
        """The id of the session's newest user message: this run's prompt."""
        try:
            messages = self.client.request("GET", f"/api/session/{session_id}/message")["data"]
        except (HttpError, OSError, KeyError, TypeError):
            return None
        # Newest first.
        return next((m.get("id") for m in messages if isinstance(m, dict) and m.get("type") == "user"), None)

    def _descends_from(self, session_id: str, root: str) -> bool:
        seen: set[str] = set()
        current: str | None = session_id
        while current and current not in seen:
            if current == root:
                return True
            seen.add(current)
            if current not in self._parents:
                info = self.get_session(current) or {}
                self._parents[current] = info.get("parentID")
            current = self._parents[current]
        return False

    def billing(self, session_id: str, model: str) -> str | None:
        """Who pays for `model` ("provider/id"): see billing.py. None if unknown."""
        if model not in self._billing:
            try:
                self._billing[model] = self._look_up_billing(session_id, model)
            except (HttpError, OSError, KeyError, TypeError, ValueError):
                return None  # try again next time
        return self._billing[model]

    def _look_up_billing(self, session_id: str, model: str) -> str | None:
        directory = ((self.get_session(session_id) or {}).get("location") or {}).get("directory")
        return self.model_info(Path(directory), model)["billing"] if directory else None

    def model_info(self, directory: Path, model: str | None) -> dict[str, Any]:
        """What opencode says of a model ("provider/id"; None: the default one):
        who pays for it (billing.py) and the address of the server it runs on, if set."""
        found = self.models_info(directory)
        return found["models"].get(model or found["default"] or "", {"billing": None, "base_url": None})

    def models_info(self, directory: Path) -> dict[str, Any]:
        """model_info() of every model: {"models": {"provider/id": info}, "default": "provider/id"}."""
        where = "?directory=" + quote(str(directory))
        default = self.client.request("GET", "/api/model/default" + where).get("data") or {}
        providers = {p.get("id"): p for p in self.client.request("GET", "/api/provider" + where)["data"]}
        integrations = {}
        if any(p.get("integrationID") for p in providers.values()):
            integrations = {i.get("id"): i for i in self.client.request("GET", "/api/integration" + where)["data"]}
        models = {}
        for details in self.client.request("GET", "/api/model" + where)["data"]:
            provider = providers.get(details.get("providerID"))
            integration = integrations.get((provider or {}).get("integrationID"))
            models[f"{details.get('providerID')}/{details.get('id')}"] = {
                "billing": opencode_billing(provider, integration, details),
                "base_url": ((provider or {}).get("settings") or {}).get("baseURL")}
        return {"models": models,
                "default": f"{default['providerID']}/{default['id']}" if default.get("providerID") else None}

    def interrupt(self, session_id: str) -> None:
        self.stopped.set()  # release the stream even if the interrupt request fails
        self.client.request("POST", f"/api/session/{session_id}/interrupt")

    def answer_form(self, request_id: str, answer: dict[str, Any] | None) -> None:
        session_id = self._forms.get(request_id)
        if session_id is None:
            raise HttpError("This question is no longer waiting for an answer", 409)
        path = f"/api/session/{session_id}/form/{request_id}"
        if answer is None:
            self.client.request("DELETE", path)
        else:
            self.client.request("POST", path + "/reply", {"answer": answer})

    def _reply(self, session_id: str, request_id: str, body: dict[str, Any]) -> bool:
        """Answer a permission request; False when opencode had withdrawn it already
        (rejecting one of several parallel requests withdraws the others)."""
        try:
            self.client.request("POST", f"/api/session/{session_id}/permission/{request_id}/reply", body)
        except HttpError as exc:
            if exc.status == 404:
                return False
            raise
        return True

    def _permission(self, session_id: str, data: dict[str, Any]) -> Event:
        if self.judge is None and not self.auto_approve:
            return event("permission", id=data.get("id"), action=data.get("action"),
                         resources=data.get("resources"), decision=None)
        resources = [str(r) for r in data.get("resources") or []]
        why = self.judge(str(data.get("action") or ""), resources) if self.judge else None
        reply = {"decision": "reject", "message": why} if why else {"decision": "once"}
        decision, outcome = ("denied", why) if why else ("once", None)
        if not self._reply(session_id, data["id"], reply):
            decision, outcome = "denied", "withdrawn by opencode"
        found = event("permission", id=data.get("id"), action=data.get("action"),
                      resources=data.get("resources"), decision=decision)
        return {**found, "outcome": outcome} if outcome else found

def _data(raw: Any) -> dict[str, Any]:
    data = raw.get("data") if isinstance(raw, dict) else None
    return data if isinstance(data, dict) else {}


def model_name(model: dict[str, Any]) -> str | None:
    if not model:
        return None
    return f"{model.get('providerID')}/{model.get('id')}"


def _content_text(content: Any) -> str | None:
    if not isinstance(content, list):
        return None
    parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
    return "".join(parts) or None


def _error_text(data: dict[str, Any]) -> str:
    error = data.get("error") or data.get("message") or data
    if isinstance(error, dict):
        return str(error.get("message") or error)
    return str(error)
