#!/usr/bin/env python3
"""relay-acp: Relay's agents as one ACP agent (the Agent Client Protocol, agentclientprotocol.com).

An ACP client (OpenHands' ACPAgent, Agent Canvas, Zed, JetBrains, Toad, …) starts this program and speaks JSON-RPC to
it over stdin and stdout; it speaks Relay's HTTP API (through Relay's Python SDK). Each ACP session is a Relay
conversation: a code task when the client's folder is in one of Relay's workspace roots (on Relay's machine), else a
chat. Its model picker lists Relay's agents (provider · model): they run as Relay runs them, with their logins in
their ProviderBoxes and their commands in the folder's WorkBox, and the conversation shows in Relay too.

    RELAY_URL=http://<relay>:8420 RELAY_APP_PASSWORD=relay_… python3 relay-acp.py [--provider ID] [--model M] [--chat]
        [--map /projects=/home/me/projects] [--skip-context]

`--provider`/`--model`: the agent new sessions start with (else your latest conversation's), and with --provider
only that provider's models to pick from; `--chat`: always a chat, whatever the client's folder; `--map`: a folder the
client sees at another path than Relay's machine has it (a client in a container), as many as needed;
`--skip-context`: leave out what a client adds to a prompt for its own agent (OpenHands' list of its skills).

Images and other files a prompt embeds go along as Relay attachments, as files attached in Relay's own page do.
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

# Relay's SDK: in the clone this file is part of (plugins/acp/static/), or installed (pip install from Relay's /sdk/)
for _parent in Path(__file__).resolve().parents:
    if (_parent / "sdk" / "python" / "relay_sdk").is_dir():
        sys.path.insert(0, str(_parent / "sdk" / "python"))
        break
try:
    import relay_sdk
except ImportError:
    sys.exit("relay-acp: Relay's Python SDK is needed: pip install http://<your-relay>:8420/sdk/"
             "relay_sdk-<version>-py3-none-any.whl (Settings → Account says which)")

PROTOCOL = 1
OUTPUT = 20_000  # characters of a tool's output passed on
KINDS = {"bash": "execute", "shell": "execute", "run_command": "execute", "execute": "execute",
         "edit": "edit", "write": "edit", "edit_file": "edit", "write_file": "edit", "multiedit": "edit",
         "patch": "edit", "apply_patch": "edit", "read": "read", "read_file": "read", "view": "read",
         "grep": "search", "glob": "search", "search": "search", "list": "search", "ls": "search",
         "webfetch": "fetch", "fetch": "fetch", "websearch": "fetch", "web_search": "fetch",
         "delete": "delete", "move": "move", "think": "think", "todowrite": "think"}
MODELS_WAIT = 20  # seconds one provider may take to list its models
# A text block a client adds after the prompt for its own agent: a tagged section (<SKILLS> of OpenHands)
CONTEXT = re.compile(r"\s*<[A-Z][A-Z_]*>")


class Failed(Exception):
    """An answer that is a JSON-RPC error."""

    def __init__(self, message: str, code: int = -32603) -> None:
        super().__init__(message)
        self.code = code


class Forwarder:
    def __init__(self, provider: str | None, model: str | None, chat: bool, url: str | None,
                 maps: list[str] = (), skip_context: bool = False, out=None, ask_in_relay: bool = False) -> None:
        self.provider, self.model, self.chat, self.url = provider, model, chat, url
        self.skip_context, self.ask_in_relay = skip_context, ask_in_relay
        # (the client's path → Relay's: "/projects=/home/me/projects")
        self.maps = [tuple(p.rstrip("/") or "/" for p in m.split("=", 1)) for m in maps if "=" in m]
        self.out = out or sys.stdout
        self.writing = threading.Lock()
        self.relay: relay_sdk.Session | None = None
        self.asked: dict[str, tuple[threading.Event, list]] = {}  # (its requests to the client: their answers)
        self.counter = 0
        self.going: dict[str, Any] = {}  # session → the conversation answering now
        self.agents: list[dict[str, Any]] | None = None  # providers with their models, asked once

    # JSON-RPC over stdio, a message a line

    def send(self, message: dict[str, Any]) -> None:
        with self.writing:
            self.out.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
            self.out.flush()

    def update(self, session: str, update: dict[str, Any]) -> None:
        self.send({"method": "session/update", "params": {"sessionId": session, "update": update}})

    def ask(self, method: str, params: dict[str, Any]) -> Any:
        """A request to the client; its result (None when it answered with an error)."""
        with self.writing:
            self.counter += 1
            request_id = f"relay-{self.counter}"
        answered = threading.Event()
        box: list = []
        self.asked[request_id] = (answered, box)
        self.send({"id": request_id, "method": method, "params": params})
        answered.wait()
        return box[0] if box else None

    def serve(self, stream=None) -> None:
        working = []
        for line in stream or sys.stdin:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except ValueError:
                self.send({"id": None, "error": {"code": -32700, "message": "Parse error"}})
                continue
            method = message.get("method")
            if method is None and "id" in message:  # (the client's answer to one of ours)
                waiting = self.asked.pop(str(message["id"]), None)
                if waiting is not None:
                    waiting[1].append(message.get("result"))
                    waiting[0].set()
            elif method == "session/cancel":
                self.cancel(message.get("params") or {})
            elif method is not None and "id" in message:  # (each request in a thread: a prompt takes its time)
                working.append(threading.Thread(target=self.answer, args=(message,), daemon=True))
                working[-1].start()
        for answered, _ in list(self.asked.values()):  # (the client went: nothing more will come)
            answered.set()
        for thread in working:  # (what it asked last is still answered)
            thread.join()

    def answer(self, message: dict[str, Any]) -> None:
        handler = HANDLERS.get(message["method"])
        try:
            if handler is None:
                raise Failed(f"{message['method']} isn't supported", -32601)
            result = handler(self, message.get("params") or {})
            self.send({"id": message["id"], "result": result})
        except Failed as exc:
            self.send({"id": message["id"], "error": {"code": exc.code, "message": str(exc)}})
        except relay_sdk.RelayError as exc:
            self.send({"id": message["id"], "error": {"code": -32002 if exc.status == 404 else -32603,
                                                      "message": str(exc)}})
        except Exception as exc:  # noqa: BLE001 - an answer still: the client waits for one
            self.send({"id": message["id"], "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"}})

    # The protocol

    def initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            self.relay = relay_sdk.connect(self.url, self.provider, self.model)
        except relay_sdk.RelayError as exc:
            raise Failed(f"Relay: {exc} (set RELAY_URL and RELAY_APP_PASSWORD: Settings → Account)", -32000) from None
        return {"protocolVersion": PROTOCOL,
                "agentCapabilities": {"loadSession": True,
                                      "promptCapabilities": {"image": True, "audio": False, "embeddedContext": True},
                                      "mcpCapabilities": {"http": False, "sse": False},
                                      "sessionCapabilities": {"resume": {}, "close": {}}},
                "authMethods": [],
                "agentInfo": {"name": "relay", "title": "Relay", "version": relay_sdk.__version__}}

    def new_session(self, params: dict[str, Any]) -> dict[str, Any]:
        relay = self._signed_in()
        folder = None if self.chat else self.to_relay(params.get("cwd"))
        try:
            conversation = relay_sdk.convo.create(relay, workdir=folder, provider=self.provider, model=self.model)
        except relay_sdk.RelayError:
            if folder is None:
                raise
            # (a folder Relay can't work in, e.g. outside its workspace roots or on another machine: a chat)
            conversation = relay_sdk.convo.create(relay, provider=self.provider, model=self.model)
        return {"sessionId": str(conversation), **self._choices(conversation)}

    def load_session(self, params: dict[str, Any]) -> dict[str, Any]:
        conversation = relay_sdk.conversation(str(params.get("sessionId")), self._signed_in())
        for message in conversation.get().get("messages") or []:  # (its history, as the protocol has it)
            if message.get("content"):
                kind = "user_message_chunk" if message.get("role") == "user" else "agent_message_chunk"
                self.update(str(conversation), {"sessionUpdate": kind,
                                                "content": {"type": "text", "text": message["content"]}})
        return self._choices(conversation)

    def resume_session(self, params: dict[str, Any]) -> dict[str, Any]:
        conversation = relay_sdk.conversation(str(params.get("sessionId")), self._signed_in())
        conversation.get()  # (there, or "not found")
        return self._choices(conversation)

    def close_session(self, params: dict[str, Any]) -> dict[str, Any]:
        return {}  # (the conversation stays in Relay)

    def set_option(self, params: dict[str, Any]) -> dict[str, Any]:
        conversation = relay_sdk.conversation(str(params.get("sessionId")), self._signed_in())
        value = str(params.get("value") or "")
        if params.get("configId") == "model":
            provider, _, model = value.partition("|")
            if provider != conversation.get()["provider_id"]:
                conversation.switch(provider=provider, model=model)
            else:
                conversation.switch(model=model)
        elif params.get("configId") == "mode":
            conversation.switch(agent_mode=value)
        else:
            raise Failed(f"No option {params.get('configId')!r}", -32602)
        return {"configOptions": self._choices(conversation)["configOptions"]}

    def set_mode(self, params: dict[str, Any]) -> dict[str, Any]:
        conversation = relay_sdk.conversation(str(params.get("sessionId")), self._signed_in())
        conversation.switch(agent_mode=str(params.get("modeId") or "build"))
        return {}

    def prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        session = str(params.get("sessionId"))
        conversation = relay_sdk.conversation(session, self._signed_in())
        blocks = params.get("prompt") or []
        with tempfile.TemporaryDirectory(prefix="relay-acp-") as folder:  # (send uploads them before it returns)
            message = conversation.send(_text(blocks, self.to_relay, self.skip_context), stream=True,
                                        files=_files(blocks, Path(folder)))
        self.going[session] = conversation
        errors, status = [], None
        try:
            for event in message.events():
                kind = event.get("type")
                if kind == "text" and event.get("delta"):
                    self.update(session, {"sessionUpdate": "agent_message_chunk",
                                          "content": {"type": "text", "text": event["delta"]}})
                elif kind == "reasoning" and event.get("delta"):
                    self.update(session, {"sessionUpdate": "agent_thought_chunk",
                                          "content": {"type": "text", "text": event["delta"]}})
                elif kind == "tool_call":
                    self.update(session, {"sessionUpdate": "tool_call", **_tool(event, self.to_client)})
                elif kind == "tool_result":
                    output = str(event.get("output") or "")[:OUTPUT]
                    self.update(session, {"sessionUpdate": "tool_call_update", "toolCallId": str(event.get("id")),
                                          "status": "completed" if event.get("ok", True) else "failed",
                                          "content": [{"type": "content", "content": {"type": "text", "text": output}}]
                                          if output else []})
                elif kind == "permission" and event.get("decision") is None and event.get("id"):
                    if self.ask_in_relay:  # (a client that would allow anything unasked: the user answers in Relay)
                        self.update(session, {"sessionUpdate": "agent_message_chunk", "content": {
                            "type": "text", "text": f"\n\n(The agent asks to be allowed: {_asked(event)}. "
                                                    "Answer it in Relay.)\n\n"}})
                    else:
                        threading.Thread(target=self.permission, args=(session, message, event), daemon=True).start()
                elif kind == "form" and event.get("status") == "pending":
                    self.update(session, {"sessionUpdate": "agent_message_chunk", "content": {
                        "type": "text", "text": f"\n\n(The agent asks: {event.get('title') or 'a question'}. "
                                                "Answer it in Relay.)\n\n"}})
                elif kind == "title" and event.get("title"):
                    self.update(session, {"sessionUpdate": "session_info_update", "title": event["title"]})
                elif kind == "error":
                    errors.append(str(event.get("message") or "error"))
                elif kind == "done":
                    status = event.get("status")
        finally:
            self.going.pop(session, None)
        if status == "succeeded":
            return {"stopReason": "end_turn"}
        if status == "interrupted":
            return {"stopReason": "cancelled"}
        raise Failed(errors[-1] if errors else f"The run {status or 'ended without an answer'}")

    def permission(self, session: str, message: Any, event: dict[str, Any]) -> None:
        """A permission request Relay leaves to the user: the client's user answers it."""
        options = event.get("options") or []
        offered = ([{"optionId": str(o["id"]), "name": str(o.get("label") or o["id"]),
                     "kind": "reject_once" if str(o.get("outcome", "")).startswith("denied") else "allow_once"}
                    for o in options] if options else
                   [{"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "deny", "name": "Don't allow", "kind": "reject_once"}])
        resources = event.get("resources")
        result = self.ask("session/request_permission", {"sessionId": session, "options": offered, "toolCall": {
            "toolCallId": str(event["id"]), "title": _asked(event),
            "kind": KINDS.get(str(event.get("action") or "").lower(), "other"), "status": "pending",
            "rawInput": {"action": event.get("action"), "resources": resources}}})
        outcome = (result or {}).get("outcome") or {}
        chosen = outcome.get("optionId") if outcome.get("outcome") == "selected" else None
        kind = next((o["kind"] for o in offered if o["optionId"] == chosen), "")
        allowed = chosen is not None and kind.startswith("allow")
        try:
            relay_sdk.Question(message, event).answer(chosen if options and chosen else allowed)
        except relay_sdk.RelayError:
            pass  # (answered in Relay meanwhile, or the run is over)

    def cancel(self, params: dict[str, Any]) -> None:
        conversation = self.going.get(str(params.get("sessionId")))
        if conversation is not None:
            try:
                conversation.stop()
            except relay_sdk.RelayError:
                pass

    # Paths, as the client sees them and as Relay's machine has them (--map)

    def to_relay(self, path: str | None) -> str | None:
        return _moved(path, [(client, relay) for client, relay in self.maps])

    def to_client(self, path: str | None) -> str | None:
        return _moved(path, [(relay, client) for client, relay in self.maps])

    # What a session can choose: the agent (Relay's providers and their models) and the mode

    def _signed_in(self) -> relay_sdk.Session:
        if self.relay is None:
            raise Failed("initialize first", -32600)
        return self.relay

    def _catalog(self) -> list[dict[str, Any]]:
        if self.agents is None:
            relay = self._signed_in()
            providers = [p for p in relay_sdk.providers(relay) if p.get("enabled")
                         and (self.provider is None or p["id"] == self.provider)]

            def models(provider: dict[str, Any]) -> list[dict[str, Any]]:
                try:
                    return relay_sdk.models(provider["id"], relay)
                except relay_sdk.RelayError:
                    return [{"id": "", "name": "Default model"}]

            pool = ThreadPoolExecutor(max(1, len(providers)))
            asked = [pool.submit(models, p) for p in providers]
            pool.shutdown(wait=False)  # (one that's slow only goes without its list)
            found = []
            for future in asked:
                try:
                    found.append(future.result(timeout=MODELS_WAIT))
                except Exception:  # noqa: BLE001 - its default model, then
                    found.append([{"id": "", "name": "Default model"}])
            self.agents = [{"id": p["id"], "name": p.get("name") or p["id"], "models": m}
                           for p, m in zip(providers, found, strict=True)]
        return self.agents

    def _choices(self, conversation: Any) -> dict[str, Any]:
        info = conversation.get()
        provider, model = info.get("provider_id") or "", info.get("model") or ""
        # (one flat list, each name saying its provider: OpenHands reads no groups)
        agents = [{"value": f"{p['id']}|{m.get('id') or ''}",
                   "name": f"{p['name']} · {m.get('name') or m.get('id') or 'default'}"}
                  for p in self._catalog() for m in (p["models"] or [{"id": "", "name": "Default model"}])]
        current = f"{provider}|{model}"
        if not any(o["value"] == current for o in agents):  # (a model none of the lists has)
            agents.insert(0, {"value": current, "name": f"{provider} · {model or 'default'}"})
        modes = [m for m in self._signed_in().request("GET", "/api/modes") if m.get("id") != "governor"]
        mode = info.get("agent_mode") or "build"
        return {
            "configOptions": [
                {"id": "model", "name": "Agent", "category": "model", "type": "select", "currentValue": current,
                 "options": agents},
                {"id": "mode", "name": "Mode", "category": "mode", "type": "select", "currentValue": mode,
                 "options": [{"value": m["id"], "name": m.get("name") or m["id"]} for m in modes]}],
            "modes": {"currentModeId": mode, "availableModes": [
                {"id": m["id"], "name": m.get("name") or m["id"], "description": m.get("description") or None}
                for m in modes]}}


def _asked(event: dict[str, Any]) -> str:
    """What a permission request asks for: "bash: rm x.txt"."""
    resources = event.get("resources")
    what = ", ".join(map(str, resources)) if isinstance(resources, list) else json.dumps(resources)
    return f"{event.get('action') or 'tool'}: {what}"[:300]


def _moved(path: str | None, maps: list[tuple[str, str]]) -> str | None:
    """`path` under the first of `maps`' (from, to) folders it's in, moved to the other; as it is otherwise."""
    for start, to in maps:
        if path and (path == start or path.startswith(start.rstrip("/") + "/")):
            return to + path[len(start):] if start != "/" else to.rstrip("/") + path
    return path


def _text(blocks: list[dict[str, Any]], place=lambda path: path, skip_context: bool = False) -> str:
    """A prompt's blocks as one message: its text, files it points at by their path, text files it embeds with theirs
    (`place`: a client's path as Relay's machine has it); other files it embeds go along as attachments (`_files`).
    A client's own context after the prompt is a paragraph of its own, or left out (`skip_context`)."""
    parts = []
    for index, block in enumerate(blocks):
        kind = block.get("type")
        if kind == "text":
            text = str(block.get("text") or "")
            if index and CONTEXT.match(text):
                if not skip_context:
                    parts.append("\n\n" + text.strip())
                continue
            parts.append(text)
        elif kind == "resource_link":
            parts.append(place(_path(str(block.get("uri") or ""))) or str(block.get("name") or ""))
        elif kind == "resource":
            resource = block.get("resource") or {}
            if "text" in resource:
                parts.append(f"\n{place(_path(str(resource.get('uri') or '')))}:\n```\n{resource['text']}\n```\n")
        elif kind in ("image", "audio") and not block.get("data"):  # (only where it is: a link)
            parts.append(place(_path(str(block.get("uri") or ""))))
    return " ".join(p for p in parts if p).strip()


def _files(blocks: list[dict[str, Any]], folder: Path) -> list[Path]:
    """The files a prompt embeds (images, audio, files that aren't text), written under `folder` to go along as
    attachments: each in a folder of its own, its name ending as its type does (Relay goes by the name)."""
    files = []
    for index, block in enumerate(blocks):
        kind, given = block.get("type"), block.get("resource") or {}
        if kind in ("image", "audio") and block.get("data"):
            data, mime, uri = block["data"], block.get("mimeType"), block.get("uri")
        elif kind == "resource" and given.get("blob"):
            kind, data, mime, uri = "file", given["blob"], given.get("mimeType"), given.get("uri")
        else:
            continue
        name = Path(unquote(urlparse(str(uri or "")).path)).name or kind
        if mime and mimetypes.guess_type(name)[0] != mime:
            name += mimetypes.guess_extension(mime) or ""
        (folder / str(index)).mkdir()
        files.append(folder / str(index) / name)
        files[-1].write_bytes(base64.b64decode(data))
    return files


def _path(uri: str) -> str:
    return unquote(urlparse(uri).path) if uri.startswith("file://") else uri


def _tool(event: dict[str, Any], place=lambda path: path) -> dict[str, Any]:
    name = str(event.get("name") or "tool")
    given = event.get("input") if isinstance(event.get("input"), dict) else {}
    path = given.get("file_path") or given.get("path") or given.get("filePath")
    path = place(str(path)) if path and os.path.isabs(str(path)) else path  # (as the client sees it, title too)
    what = given.get("command") or given.get("description") or path or given.get("pattern") or given.get("url") \
        or given.get("title") or ""
    return {"toolCallId": str(event.get("id")), "title": f"{name}: {what}"[:300] if what else name,
            "kind": KINDS.get(name.lower(), "other"), "status": "in_progress", "rawInput": given,
            **({"locations": [{"path": path}]} if path and os.path.isabs(str(path)) else {})}


HANDLERS = {"initialize": Forwarder.initialize, "session/new": Forwarder.new_session,
            "session/load": Forwarder.load_session, "session/resume": Forwarder.resume_session,
            "session/close": Forwarder.close_session, "session/prompt": Forwarder.prompt,
            "session/set_config_option": Forwarder.set_option, "session/set_mode": Forwarder.set_mode,
            "authenticate": lambda self, params: {}}


def main() -> None:
    parser = argparse.ArgumentParser(description="Relay's agents as one ACP agent, over stdin and stdout.")
    parser.add_argument("--url", help="Relay's address (else $RELAY_URL, else http://localhost:8420)")
    parser.add_argument("--provider", help="the provider new sessions start with, and the only one to pick from")
    parser.add_argument("--model", help="the model new sessions start with")
    parser.add_argument("--chat", action="store_true", help="always a chat, whatever the client's folder")
    parser.add_argument("--skip-context", action="store_true",
                        help="leave out what a client adds to a prompt for its own agent (OpenHands' <SKILLS>)")
    parser.add_argument("--map", action="append", default=[], metavar="FROM=TO",
                        help="a folder the client sees as FROM, which is TO on Relay's machine (a client in a "
                             "container)")
    parser.add_argument("--ask-in-relay", action="store_true",
                        help="leave what Relay asks the user to allow to Relay's page, not the client (one that "
                             "allows anything unasked, as OpenHands does)")
    args = parser.parse_args()
    Forwarder(args.provider, args.model, args.chat, args.url, args.map, args.skip_context,
              ask_in_relay=args.ask_in_relay).serve()


if __name__ == "__main__":
    main()
