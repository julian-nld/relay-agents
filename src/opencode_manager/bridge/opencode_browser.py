"""A browser for opencode's browser tools, attached to one of its sessions.

opencode (v2) has browser tools (browser.tabs.open, snapshot, click, screenshot, …) that it
hands to a desktop app over its plugin RPC "experimental.browser": the desktop calls
`attach` for a session (the call stays open while it's connected), hears `control` events
on /api/event, fetches each command with `command`, answers with `result`, and reports its
tabs with `state`. Without one the tools say "No desktop browser is connected".

`BrowserAttachment` is that desktop: it speaks this protocol with opencode and hands the
commands to a browser program it starts (`argv`), as JSON lines on its stdin; the program
answers on its stdout (see relay/container/browser.cjs for the format). The program needs
nothing of opencode's: no address, no password. `attach()` keeps one per session.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

from opencode_manager.bridge.http import Client, HttpError

RPC = "/api/rpc/experimental.browser"
VERSION = 4

_attached: dict[str, BrowserAttachment] = {}
_lock = threading.Lock()


def attach(client: Client, session_id: str, directory: Path, argv: list[str],
           log: Path | None = None) -> BrowserAttachment:
    """The session's browser: the one attached already, or a new one (what the browser program
    says on stderr goes to `log`)."""
    with _lock:
        found = _attached.get(session_id)
        if found and found.alive():
            return found
        attachment = BrowserAttachment(client, session_id, directory, argv, log)
        _attached[session_id] = attachment
    attachment.start()
    return attachment


class BrowserAttachment:
    def __init__(self, client: Client, session_id: str, directory: Path, argv: list[str],
                 log: Path | None = None) -> None:
        self.client = client
        self.log = log
        self.session_id = session_id
        self.location = "?location[directory]=" + quote(str(directory), safe="")
        self.argv = argv
        self.connection = f"relay-{uuid.uuid4()}"
        self.process: subprocess.Popen | None = None
        self.stopped = threading.Event()
        self.listening = threading.Event()  # hearing opencode's events (before attaching: none missed)
        self.attached = threading.Event()  # opencode said so (a control event)
        self.writing = threading.Lock()
        self._response = None  # the open attach call

    def alive(self) -> bool:
        return not self.stopped.is_set() and self.process is not None and self.process.poll() is None

    def start(self) -> None:
        errors = open(self.log, "a") if self.log else subprocess.DEVNULL
        try:
            self.process = subprocess.Popen(self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=errors, text=True, bufsize=1)
        finally:
            if self.log:
                errors.close()
        for target in (self._listen, self._read, self._attach):
            threading.Thread(target=target, name=f"browser-{self.session_id[-8:]}", daemon=True).start()

    def stop(self, why: str = "") -> None:
        if self.stopped.is_set():
            return
        self.stopped.set()
        self._say(f"detached{': ' + why if why else ''}")
        with _lock:
            if _attached.get(self.session_id) is self:
                del _attached[self.session_id]
        if self._response is not None:
            try:
                self._response.close()  # opencode drops the attachment
            except OSError:
                pass
        if self.process and self.process.poll() is None:
            try:
                self.process.stdin.close()
                self.process.wait(10)
            except (OSError, subprocess.TimeoutExpired):
                self.process.kill()

    # opencode → the browser program

    def _attach(self) -> None:
        """Hold the attach call open; when it ends (another desktop, the session gone, the server
        stopped), so does this attachment."""
        body = {"input": {"sessionID": self.session_id, "connectionID": self.connection, "version": VERSION}}
        self.listening.wait(15)
        try:
            self._response = self.client._open("POST", f"{RPC}/attach{self.location}", body, timeout=None)
            self._response.read()
            why = "opencode ended the attachment"
        except (HttpError, OSError) as exc:
            why = f"attach: {exc}"
        self.stop(why)

    def _listen(self) -> None:
        try:
            for event in self.client.events("/api/event"):
                if self.stopped.is_set():
                    return
                if isinstance(event, dict) and event.get("type") == "server.connected":
                    self.listening.set()
                if not isinstance(event, dict) or event.get("type") != "rpc.experimental.browser.control":
                    continue
                data = event.get("data") or {}
                if data.get("connectionID") != self.connection:
                    continue
                if data.get("type") == "attached":
                    self.attached.set()
                elif data.get("type") == "command" and data.get("requestID"):
                    threading.Thread(target=self._command, args=(data["requestID"],), daemon=True).start()
        except (HttpError, OSError) as exc:
            self.stop(f"events: {exc}")
        self.stop("the event stream ended")

    def _command(self, request_id: str) -> None:
        try:
            command = self._rpc("command", {"requestID": request_id})
        except (HttpError, OSError, ValueError):
            return  # withdrawn meanwhile
        self._send({"requestID": request_id, "command": command})

    def _send(self, message: dict[str, Any]) -> None:
        with self.writing:
            try:
                self.process.stdin.write(json.dumps(message) + "\n")
                self.process.stdin.flush()
            except (OSError, ValueError) as exc:
                self.stop(f"the browser program is gone ({exc})")

    # the browser program → opencode

    def _read(self) -> None:
        for line in self.process.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            try:
                if "state" in message:
                    self.attached.wait(30)  # opencode keeps tabs only of an attached browser
                    self._state(message["state"])
                elif message.get("requestID") and "outcome" in message:
                    self._rpc("result", {"requestID": message["requestID"], "outcome": message["outcome"]})
            except (HttpError, OSError, ValueError) as exc:
                self._say(str(exc))
        self.stop("the browser program ended")  # its container stopped, or it failed

    def _state(self, state: dict[str, Any]) -> None:
        """Tell opencode the tabs (it checks every tabID against them); again if it wasn't ready."""
        for attempt in range(10):
            try:
                self._rpc("state", {"state": state})
                return
            except HttpError:
                if attempt == 9 or self.stopped.wait(0.5):
                    raise

    def _say(self, text: str) -> None:
        line = f"opencode's browser for {self.session_id}: {text}"
        if self.log:
            try:
                with open(self.log, "a") as log:
                    log.write(line + "\n")
                return
            except OSError:
                pass
        print(f"relay: {line}", file=sys.stderr, flush=True)

    def _rpc(self, method: str, data: dict[str, Any]) -> Any:
        body = {"input": {"sessionID": self.session_id, "connectionID": self.connection, **data}}
        found = self.client.request("POST", f"{RPC}/{method}{self.location}", body)
        return found.get("output") if isinstance(found, dict) else None
