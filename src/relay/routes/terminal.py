"""Authenticated, on-demand terminals; no provider sessions or chat events are changed."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import anyio
from starlette.websockets import WebSocket, WebSocketDisconnect

from relay.containers import ContainerError
from relay.production import ProductionError
from relay.terminal import TAKEN, Terminal, dimensions
from relay.web import COOKIE, from_relay


async def _user(socket: WebSocket) -> dict | None:
    """The user a shell is for; else it's refused, saying why (accepted only to say so: no shell)."""
    relay = socket.app.state.relay
    origin = socket.headers.get("origin")
    if not from_relay(socket.headers, relay.settings.public_url):
        why = (f"Relay refused the shell: this page ({origin}) isn't at an address Relay knows as its own "
               f"({socket.headers.get('host')}). Behind a reverse proxy, have it pass the Host header on (or "
               "X-Forwarded-Host), or set Relay's address in Settings → Limits.")
    else:
        user = (relay.accounts.user_for_token(socket.cookies.get(COOKIE)) if relay.login_required
                else relay.accounts.local_user())
        if user:
            return user
        why = "You're not signed in (any more): sign in to Relay again, then open a new shell."
    print(f"relay: {why}", file=sys.stderr, flush=True)
    await socket.accept()
    await socket.send_json({"type": "error", "message": why})
    await socket.close(code=4401)
    return None


async def terminal_socket(socket: WebSocket) -> None:
    """A shell in a conversation's WorkBox: kept when its page goes (relay.terminal.Kept), and that one again when
    the chat's shell is opened again."""
    relay = socket.app.state.relay
    user = await _user(socket)
    if not user:
        return
    await socket.accept()

    def opened() -> Terminal:
        row = relay.db.one("SELECT * FROM conversations WHERE id = ? AND user_id = ?",
                           (socket.path_params["conversation_id"], user["id"]))
        if row is None:
            raise ValueError("Conversation not found")
        if row.get("computer_id"):
            raise ValueError("This task runs on another computer, not in a local WorkBox")
        if row["task_state"] == "discarded":
            raise ValueError("This task was discarded")
        directory = Path(row["directory"])
        if row["mode"] == "chat":
            directory.mkdir(parents=True, exist_ok=True)
        if not directory.is_dir():
            raise ValueError("The task's working folder no longer exists")
        return Terminal(relay.containers, directory)

    await _serve(socket, opened, key=socket.path_params["conversation_id"])


async def service_terminal_socket(socket: WebSocket) -> None:
    """A shell in a service's live ProductionBox (relay.production: Shell)."""
    relay = socket.app.state.relay
    user = await _user(socket)
    if not user:
        return
    await socket.accept()

    def opened() -> Terminal:
        try:
            service = relay.production.get(user["id"], socket.path_params["service_id"])
            box, folder = relay.production.shell(service)
        except ProductionError as exc:
            raise ValueError(str(exc)) from exc
        return Terminal(box, folder)

    await _serve(socket, opened)


async def creation_terminal_socket(socket: WebSocket) -> None:
    """A shell in a new provider's setup box (relay.creations: step 1, where the user installs it and logs in):
    kept, as a chat's."""
    relay = socket.app.state.relay
    user = await _user(socket)
    if not user:
        return
    await socket.accept()
    creation_id = socket.path_params["creation_id"]

    def opened() -> Terminal:
        from relay.repair import RepairError
        try:
            return relay.creations.shell(user["id"], creation_id)
        except (RepairError, ContainerError) as exc:
            raise ValueError(str(exc)) from exc

    await _serve(socket, opened, key=f"creation:{creation_id}")


async def provider_terminal_socket(socket: WebSocket) -> None:
    """A provider's Shell (relay.creations provider_shell): in its own box, or in a box with its own home."""
    relay = socket.app.state.relay
    user = await _user(socket)
    if not user:
        return
    await socket.accept()
    provider_id = socket.path_params["provider_id"]

    def opened() -> Terminal:
        from relay.providers import TemplateError
        from relay.repair import RepairError
        try:
            return relay.creations.provider_shell(relay.registry.get(provider_id))
        except (RepairError, TemplateError, ContainerError) as exc:
            raise ValueError(str(exc)) from exc

    await _serve(socket, opened, key=f"provider:{provider_id}")


async def provider_draft_terminal_socket(socket: WebSocket) -> None:
    """The same shell for a provider not added yet (Add provider's next step, with its template as the page has it),
    to log in before it's added. It isn't kept: it ends when that page goes, and its box with it if it wasn't added."""
    relay = socket.app.state.relay
    user = await _user(socket)
    if not user:
        return
    await socket.accept()

    def opened() -> Terminal:
        from relay.providers import TemplateError, draft_template
        from relay.repair import RepairError
        try:
            template = draft_template(socket.path_params["kind"], json.loads(socket.query_params.get("template", "")))
            return relay.creations.provider_shell(template, draft=True)
        except (ValueError, RepairError, TemplateError, ContainerError) as exc:
            raise ValueError(str(exc)) from exc

    await _serve(socket, opened)


async def _serve(socket: WebSocket, opened: Callable[[], Terminal], key: str | None = None) -> None:
    """The terminal's protocol: its size first, then input, resizes and output until either ends. With `key` (a
    chat's), the shell is kept: the page going leaves it running, a page opening it again gets it back."""
    relay = socket.app.state.relay
    shells = relay.shells
    session = kept = watcher = None
    startup = None
    tasks = []
    failed = False
    try:
        fresh = await asyncio.to_thread(opened)  # (podman: off the event loop; it also checks it may be opened)
        cols, rows = dimensions(await socket.receive_json())
        kept = shells.running(key) if key else None
        resumed = kept is not None
        if resumed:
            # (its size stays the one its output was written at until the page, having written it again at that
            # size, asks for its own: its terminal wraps it anew then, rather than showing it wrapped wrong)
            session = kept.session
        else:
            session = fresh
            relay.terminals.add(session)
            startup = asyncio.create_task(asyncio.to_thread(session.start, cols, rows))
            await asyncio.shield(startup)
            if key:
                kept = shells.keep(key, session)
        replay = b""
        if kept is not None:
            watcher = kept.attach()
            replay = kept.replay() if resumed else b""
        await socket.send_json({"type": "ready", "container": session.container, "directory": str(session.directory),
                                "resumed": resumed,
                                **({"size": list(session.size)} if resumed and session.size else {})})
        if replay:
            await socket.send_bytes(replay)
        if resumed:
            await socket.send_json({"type": "replayed"})  # (what it showed is written: its size may change now)

        async def output():
            if kept is None:  # (a service's shell: it ends with its page)
                while data := await session.read():
                    await socket.send_bytes(data)
                code = await asyncio.to_thread(session.process.wait)
                await socket.send_json({"type": "exit", "code": code})
                return
            while True:
                item = await watcher.get()
                if isinstance(item, bytes):
                    await socket.send_bytes(item)
                elif item is TAKEN:
                    await socket.send_json({"type": "detached", "message": "This shell is open in another window now"})
                    return
                else:
                    _, code, ended = item
                    await socket.send_json({"type": "exit", "code": code, **({"ended": True} if ended else {})})
                    return

        async def input_():
            while True:
                message = await socket.receive_json()
                if not isinstance(message, dict):
                    raise TypeError("Expected a terminal message")
                if message.get("type") == "resize":
                    session.resize(*dimensions(message))
                elif message.get("type") == "input" and isinstance(message.get("data"), str):
                    await session.write(message["data"])
                elif message.get("type") == "end":  # (End shell; closing the page only leaves a chat's)
                    if kept is None:
                        return
                    await asyncio.shield(shells.end(key, kept))
                else:
                    raise ValueError("Unknown terminal message")

        tasks = [asyncio.create_task(output()), asyncio.create_task(input_())]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except WebSocketDisconnect:
        pass
    except (ContainerError, ProductionError, OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
        failed = True
        try:
            await socket.send_json({"type": "error", "message": str(exc)})
        except (WebSocketDisconnect, OSError):
            pass
    finally:
        # Server/test-client cancellation must not strand the shell or its container hold.
        with anyio.CancelScope(shield=True):
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if startup:
                await asyncio.gather(startup, return_exceptions=True)
            if kept is not None:
                # (a page that spoke nonsense ends it; one that went leaves it running)
                if failed:
                    await shells.end(key, kept)
                else:
                    shells.detach(key, kept, watcher)
            elif session:  # (a service's shell, or one whose start was given up: it ends)
                try:
                    await asyncio.to_thread(session.close)
                finally:
                    relay.terminals.discard(session)
            try:
                await socket.close()
            except (WebSocketDisconnect, RuntimeError, OSError):
                pass
