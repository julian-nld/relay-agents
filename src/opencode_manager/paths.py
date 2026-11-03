"""Filesystem locations used by opencode-manager (and by Relay, which builds on it).

The code lives in its folder (with its Python dependencies in vendor/); the data
lives where data goes on this machine: Relay's under RELAY_HOME (default
~/.local/share/relay), opencode-manager's settings, state and profiles in
opencode-manager/ there (or OPENCODE_MANAGER_HOME). The programs (opencode,
claude, …) come from PATH.

    <root>/                     the code
        relay, opencode-manager launchers
        src/opencode_manager/   this package
        src/relay/              Relay
        vendor/                 Python dependencies
    ~/.local/share/relay/       Relay's data
        opencode-manager/       settings, state and profiles
"""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "opencode-manager"
HOME_ENV = "OPENCODE_MANAGER_HOME"
RELAY_HOME_ENV = "RELAY_HOME"


def root() -> Path:
    """The folder with the code (and the launchers)."""
    return Path(__file__).resolve().parents[2]


def relay_home() -> Path:
    """Where Relay keeps its data: RELAY_HOME, else ~/.local/share/relay (XDG_DATA_HOME)."""
    value = os.environ.get(RELAY_HOME_ENV)
    if value:
        return Path(value).expanduser()
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "relay"


def own_opencode() -> bool:
    """Whether this is a Relay with data of its own (RELAY_HOME isn't the usual folder: a test, a
    trial, a second Relay): its "default" opencode profile is then one of its own too, not this
    machine's opencode, whose service and data the usual Relay uses."""
    usual = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "relay"
    return relay_home().expanduser().resolve() != usual.resolve()


def data_dir() -> Path:
    """Where opencode-manager's settings, state and profiles are stored."""
    value = os.environ.get(HOME_ENV)
    return Path(value).expanduser() if value else relay_home() / APP_NAME


def profiles_dir() -> Path:
    return data_dir() / "profiles"


def python_path() -> list[Path]:
    """Import paths needed to run this package from its folder."""
    return [root() / "src", root() / "vendor"]


def program(name: str) -> str:
    """The program to run for `name`: the one on PATH."""
    return name


def host_cache_dir() -> Path:
    """Disposable per-machine cache (never used for data that must travel)."""
    value = os.environ.get("XDG_CACHE_HOME")
    return (Path(value) if value else Path.home() / ".cache") / APP_NAME


def default_opencode_config_dir() -> Path:
    """The host's own opencode config dir (used by the "default" profile)."""
    value = os.environ.get("XDG_CONFIG_HOME")
    return (Path(value) if value else Path.home() / ".config") / "opencode"
