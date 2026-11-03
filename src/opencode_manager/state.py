"""Runtime state remembered between runs: last active profile, recent projects."""

from __future__ import annotations

import fcntl
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

from opencode_manager import paths
from opencode_manager.jsonfile import read_json, write_json
from opencode_manager.profiles import DEFAULT_PROFILE

MAX_RECENT_PROJECTS = 10
MAX_SESSION_DIRECTORIES = 200


@dataclass
class State:
    active_profile: str = DEFAULT_PROFILE
    # Most recent first; encoded with encode_project().
    recent_projects: list[str] = field(default_factory=list)
    # Last bridge session per "<backend>:<profile>" (or "<backend>"), for
    # `stream --continue`.
    last_sessions: dict[str, str] = field(default_factory=dict)
    # Project directory per "<backend>:<session id>", for backends that can only
    # resume a session from its own directory (Claude Code).
    session_directories: dict[str, str] = field(default_factory=dict)


def state_path() -> Path:
    return paths.data_dir() / "state.json"


def load(path: Path | None = None) -> State:
    raw = read_json(path or state_path())
    active = raw.get("active_profile")
    recent = raw.get("recent_projects")
    sessions = raw.get("last_sessions")
    directories = raw.get("session_directories")
    return State(
        active_profile=active if isinstance(active, str) else DEFAULT_PROFILE,
        recent_projects=[p for p in recent if isinstance(p, str)] if isinstance(recent, list) else [],
        last_sessions={
            k: v for k, v in sessions.items() if isinstance(k, str) and isinstance(v, str)
        } if isinstance(sessions, dict) else {},
        session_directories={
            k: v for k, v in directories.items() if isinstance(k, str) and isinstance(v, str)
        } if isinstance(directories, dict) else {},
    )


def save(state: State, path: Path | None = None) -> None:
    write_json(path or state_path(), asdict(state))


@contextmanager
def _updating(path: Path | None):
    """Load, change and save the state while holding a lock, so concurrent
    opencode-manager processes (the sidebar, scripts, Relay) don't overwrite
    each other's changes."""
    file = path or state_path()
    file.parent.mkdir(parents=True, exist_ok=True)
    with open(file.with_name(file.name + ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = load(file)
        yield state
        save(state, file)


def remember_profile(name: str, path: Path | None = None) -> None:
    with _updating(path) as state:
        state.active_profile = name


def remember_project(directory: Path, path: Path | None = None) -> None:
    with _updating(path) as state:
        encoded = encode_project(directory)
        state.recent_projects = [encoded] + [p for p in state.recent_projects if p != encoded]
        del state.recent_projects[MAX_RECENT_PROJECTS:]


def remember_session(
    key: str, session_id: str, path: Path | None = None, *, directory: Path | None = None
) -> None:
    with _updating(path) as state:
        state.last_sessions[key] = session_id
        if directory is not None:
            entry = f"{key}:{session_id}"
            directories = state.session_directories
            directories.pop(entry, None)  # re-insert as the most recent
            directories[entry] = encode_project(directory)
            for old in list(directories)[:-MAX_SESSION_DIRECTORIES]:
                del directories[old]


def session_directory(key: str, path: Path | None = None) -> Path | None:
    encoded = load(path).session_directories.get(key)
    return decode_project(encoded) if encoded else None


def last_session(key: str, path: Path | None = None) -> str | None:
    return load(path).last_sessions.get(key)


def recent_projects(path: Path | None = None) -> list[Path]:
    """Recent project directories that exist on this machine, most recent first."""
    found = []
    for encoded in load(path).recent_projects:
        directory = decode_project(encoded)
        if directory.is_dir() and directory not in found:
            found.append(directory)
    return found


# Projects are stored as absolute paths. Older entries may be relative to the
# folder above the code (from when it was a portable folder); those still resolve.


def encode_project(directory: Path) -> str:
    return str(directory)


def decode_project(encoded: str) -> Path:
    path = Path(encoded)
    return path if path.is_absolute() else paths.root().parent / path
