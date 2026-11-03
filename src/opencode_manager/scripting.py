"""Operations behind the scriptable CLI commands.

Each function returns plain data (dicts, lists, strings) that the CLI prints
as text or JSON, and raises an exception on failure. Commands that control
the workspace raise NotRunning when there is no workspace session.
"""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NoReturn

from opencode_manager import configfile, opencode, paths, ports, tmux
from opencode_manager import state as state_mod
from opencode_manager.bridge.claude_backend import ClaudeCodeBackend
from opencode_manager.bridge.events import Event
from opencode_manager.bridge.events import event as bridge_event
from opencode_manager.bridge.http import Client, HttpError
from opencode_manager.bridge.opencode_backend import OpencodeBackend
from opencode_manager.profiles import Profile, ProfileStore
from opencode_manager.settings import Settings
from opencode_manager.workspace import Workspace


class NotRunning(Exception):
    """The workspace tmux session is not running."""


class ScriptError(Exception):
    """A command could not be carried out (bad input, invalid config, …)."""


def running_workspace(settings: Settings, store: ProfileStore) -> Workspace:
    if not tmux.has_session(settings.session_name):
        raise NotRunning(f"The workspace ({settings.session_name!r}) is not running")
    return Workspace(settings, store, settings.session_name)


def _optional_workspace(settings: Settings, store: ProfileStore) -> Workspace | None:
    try:
        return running_workspace(settings, store)
    except NotRunning:
        return None


def active_profile_name(settings: Settings, store: ProfileStore) -> str:
    ws = _optional_workspace(settings, store)
    return ws.active_profile if ws else state_mod.load().active_profile


# Queries


def service_state(settings: Settings, profile: Profile) -> str | None:
    """"running", "stopped" or "port-taken"; None if the profile has no own service."""
    if settings.launch_mode != "service" or profile.is_default or profile.port is None:
        return None
    if ports.is_free(profile.port):
        return "stopped"
    return "running" if opencode.serves(settings, profile) else "port-taken"


def profile_info(settings: Settings, profile: Profile, active: str) -> dict[str, Any]:
    return {
        "name": profile.name,
        "active": profile.name == active,
        "default": profile.is_default,
        "path": str(profile.root) if profile.root else None,
        "port": profile.port,
        "service": service_state(settings, profile),
        "config": str(configfile.config_file(profile)),
    }


def list_profiles(settings: Settings, store: ProfileStore) -> list[dict[str, Any]]:
    active = active_profile_name(settings, store)
    return [profile_info(settings, p, active) for p in store.list()]


def status(settings: Settings, store: ProfileStore) -> dict[str, Any]:
    ws = _optional_workspace(settings, store)
    return {
        "running": ws is not None,
        "session": settings.session_name,
        "profile": ws.active_profile if ws else state_mod.load().active_profile,
        "project": str(ws.directory) if ws else None,
        "launch_mode": settings.launch_mode,
        "data_dir": str(paths.data_dir()),
        "profiles": list_profiles(settings, store),
    }


# Workspace control


def switch(settings: Settings, store: ProfileStore, name: str) -> dict[str, Any]:
    ws = running_workspace(settings, store)
    ws.launch(store.get(name))
    return {"profile": name}


def open_project(settings: Settings, store: ProfileStore, directory: str) -> dict[str, Any]:
    ws = running_workspace(settings, store)
    path = Path(directory).expanduser().resolve()
    if not path.is_dir():
        raise ScriptError(f"{path} is not a directory")
    ws.open_project(path)
    return {"project": str(path)}


def restart(settings: Settings, store: ProfileStore) -> dict[str, Any]:
    ws = running_workspace(settings, store)
    ws.launch(store.get(ws.active_profile))
    return {"profile": ws.active_profile}


# Profiles


def create_profile(
    settings: Settings, store: ProfileStore, name: str, copy_config: bool
) -> dict[str, Any]:
    source = paths.default_opencode_config_dir() if copy_config else None
    profile = store.create(name, copy_config_from=source)
    return profile_info(settings, profile, active_profile_name(settings, store))


def delete_profile(settings: Settings, store: ProfileStore, name: str) -> dict[str, Any]:
    profile = store.get(name)
    ws = _optional_workspace(settings, store)
    if ws and ws.active_profile == name:
        raise ScriptError(f"{name!r} is active in the workspace; switch to another profile first")
    opencode.stop_service(settings, profile)
    store.delete(name)
    return {"deleted": name}


# Config


def config_path(store: ProfileStore, name: str) -> str:
    return str(configfile.config_file(store.get(name)))


def check_config(store: ProfileStore, name: str) -> dict[str, Any]:
    path = configfile.config_file(store.get(name))
    text = configfile.read(path)
    if text is None:
        return {"path": str(path), "exists": False, "valid": True}
    if error := configfile.validate(text):
        raise ScriptError(f"{path}: {error}")
    return {"path": str(path), "exists": True, "valid": True}


# Running opencode


def exec_opencode(
    settings: Settings, store: ProfileStore, name: str | None, args: list[str]
) -> NoReturn:
    """Replace this process with opencode, run with a profile's environment."""
    profile = store.get(name or active_profile_name(settings, store))
    profile = opencode.prepare(settings, store, profile)
    binary = opencode.binary(settings)
    env = {**os.environ, **profile.env()}
    if shutil.which(binary, path=env.get("PATH")) is None:
        raise ScriptError(f"{binary!r} not found in PATH")
    os.execvpe(binary, [binary, *args], env)



# Bridge


class StreamRun:
    """One prompt sent to an agent session, with its uniform event stream."""

    def __init__(self, backend, session_id: str, first: Event, text: str,
                 on_started=None) -> None:
        self.backend = backend
        self.session_id = session_id
        self._first = first
        self._text = text
        # Called once the agent has really begun (its first model step).
        self._on_started = on_started

    def events(self) -> Iterator[Event]:
        """The run's events; "done" is always last, even if the backend fails."""
        yield self._first
        try:
            for event in self.backend.run(self.session_id, self._text):
                if event["type"] == "step" and self._on_started:
                    self._on_started()
                    self._on_started = None
                yield event
        except (HttpError, OSError) as exc:
            yield bridge_event("error", message=str(exc))
            yield bridge_event("done", status="failed")

    def interrupt(self) -> Event:
        self.backend.interrupt(self.session_id)
        return bridge_event("done", status="interrupted")


BACKENDS = ("opencode", "claude-code")


def start_stream(
    settings: Settings,
    store: ProfileStore,
    text: str,
    *,
    backend: str = "opencode",
    profile_name: str | None = None,
    session_id: str | None = None,
    continue_last: bool = False,
    directory: Path | None = None,
    model: str | None = None,
    agent: str | None = None,
    auto_approve: bool = True,
) -> StreamRun:
    """Prepare a run of `text` in a new or existing agent session."""
    if backend == "claude-code":
        if profile_name:
            raise ScriptError("Profiles apply to the opencode backend only")
        return _start_claude_stream(
            settings, text, session_id=session_id, continue_last=continue_last,
            directory=directory, model=model, agent=agent, auto_approve=auto_approve,
        )
    return _start_opencode_stream(
        settings, store, text, profile_name=profile_name, session_id=session_id,
        continue_last=continue_last, directory=directory, model=model, agent=agent,
        auto_approve=auto_approve,
    )


def _new_session_directory(directory: Path | None) -> Path:
    path = (directory or Path.cwd()).expanduser().resolve()
    if not path.is_dir():
        raise ScriptError(f"{path} is not a directory")
    return path


def _start_opencode_stream(
    settings, store, text, *, profile_name, session_id, continue_last, directory, model, agent,
    auto_approve,
) -> StreamRun:
    profile = store.get(profile_name or active_profile_name(settings, store))
    url, password = opencode.ensure_service(settings, store, profile)
    backend = OpencodeBackend(Client(url, "opencode", password), auto_approve=auto_approve)
    key = f"{backend.name}:{profile.name}"

    if continue_last:
        session_id = state_mod.last_session(key)
        if session_id is None:
            raise ScriptError(f"No previous session for profile {profile.name!r}")
    if session_id:
        info = backend.get_session(session_id)
        if info is None:
            raise ScriptError(f"Session {session_id!r} does not exist for {profile.name!r}")
        location = (info.get("location") or {}).get("directory")
        new = False
    else:
        location = str(_new_session_directory(directory))
        session_id = backend.create_session(Path(location), model=model, agent=agent)
        new = True
    state_mod.remember_session(key, session_id)

    first = bridge_event(
        "session", backend=backend.name, session=session_id, profile=profile.name,
        directory=location, new=new,
    )
    return StreamRun(backend, session_id, first, text)


def _start_claude_stream(
    settings, text, *, session_id, continue_last, directory, model, agent, auto_approve,
) -> StreamRun:
    key = "claude-code"
    if continue_last:
        session_id = state_mod.last_session(key)
        if session_id is None:
            raise ScriptError("No previous Claude Code session")
    if session_id:
        # Claude Code finds a session only from the directory it was started
        # in, so a known directory wins over --dir.
        known = state_mod.session_directory(f"{key}:{session_id}")
        location = known if known else _new_session_directory(directory)
        new = False
    else:
        location = _new_session_directory(directory)
        session_id = str(uuid.uuid4())
        new = True
    binary = opencode.configured_executable(settings.claude_bin or "claude")
    if shutil.which(binary) is None:
        raise ScriptError(f"{binary!r} not found in PATH")

    def remember() -> None:
        state_mod.remember_session(key, session_id, directory=location)

    if not new:
        remember()

    backend = ClaudeCodeBackend(
        location, resume=not new, binary=binary, model=model, agent=agent,
        auto_approve=auto_approve,
    )
    first = bridge_event(
        "session", backend=backend.name, session=session_id, profile=None,
        directory=str(location), new=new,
    )
    # A new session exists only once claude has started it; remembering it
    # earlier would make --continue point at nothing after a failed start.
    return StreamRun(backend, session_id, first, text, on_started=remember if new else None)
