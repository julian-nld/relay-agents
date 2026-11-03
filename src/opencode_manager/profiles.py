"""Profiles: isolated sets of opencode config, auth, sessions and state.

Layout of a profile on disk:

    <profiles_dir>/<name>/
        profile.json   metadata (service port)
        config/        -> OPENCODE_CONFIG_DIR  (opencode.json, service.json, ...)
        data/          -> XDG_DATA_HOME        (opencode.db: auth + sessions)
        state/         -> XDG_STATE_HOME

The built-in "default" profile has no directory and no overrides: it is
opencode exactly as it runs without the manager. Unless the store has a default of
its own (own_default: a Relay with data of its own, not the machine's usual one, whose
opencode service another Relay may run): then it's a profile folder like the others.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from opencode_manager.jsonfile import read_json, write_json

DEFAULT_PROFILE = "default"
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

# Files in an opencode config dir that belong to one installation and must not
# be copied into a new profile.
_UNCOPYABLE_CONFIG = {"service.json"}


class ProfileError(Exception):
    pass


@dataclass(frozen=True)
class Profile:
    name: str
    root: Path | None = None
    port: int | None = None

    @property
    def is_default(self) -> bool:
        return self.root is None

    @property
    def config_dir(self) -> Path | None:
        return self.root / "config" if self.root else None

    @property
    def data_home(self) -> Path | None:
        return self.root / "data" if self.root else None

    @property
    def state_home(self) -> Path | None:
        return self.root / "state" if self.root else None

    def env(self) -> dict[str, str]:
        """Environment overrides that point opencode at this profile."""
        if self.root is None:
            return {}
        return {
            "OPENCODE_CONFIG_DIR": str(self.config_dir),
            "XDG_DATA_HOME": str(self.data_home),
            "XDG_STATE_HOME": str(self.state_home),
        }


class ProfileStore:
    def __init__(
        self,
        root: Path,
        base_port: int,
        is_port_free: Callable[[int], bool] = lambda port: True,
        own_default: bool = False,
    ) -> None:
        self.root = root
        self.base_port = base_port
        # Ports must also be free on this machine: another program, or another
        # copy of opencode-manager with its own data, may already use them.
        self.is_port_free = is_port_free
        # The default profile in a folder of its own (paths.own_opencode), not this machine's opencode
        self.own_default = own_default

    def list(self) -> list[Profile]:
        profiles = [self.get(DEFAULT_PROFILE)]
        if self.root.is_dir():
            for entry in sorted(self.root.iterdir()):
                if entry.is_dir() and NAME_PATTERN.match(entry.name) and entry.name != DEFAULT_PROFILE:
                    profiles.append(self._load(entry))
        return profiles

    def exists(self, name: str) -> bool:
        return name == DEFAULT_PROFILE or (self.root / name).is_dir()

    def get(self, name: str) -> Profile:
        if name == DEFAULT_PROFILE:
            return self._own_default() if self.own_default else Profile(DEFAULT_PROFILE)
        path = self.root / name
        if not NAME_PATTERN.match(name) or not path.is_dir():
            raise ProfileError(f"Profile {name!r} does not exist")
        return self._load(path)

    def create(self, name: str, copy_config_from: Path | None = None) -> Profile:
        validate_name(name)
        if self.exists(name):
            raise ProfileError(f"Profile {name!r} already exists")

        path = self.root / name
        profile = Profile(name, root=path, port=self._next_free_port())
        for directory in (profile.config_dir, profile.data_home, profile.state_home):
            directory.mkdir(parents=True)
        if copy_config_from and copy_config_from.is_dir():
            _copy_config(copy_config_from, profile.config_dir)
        write_json(path / "profile.json", {"port": profile.port})
        return profile

    def delete(self, name: str) -> None:
        if name == DEFAULT_PROFILE:
            raise ProfileError("The default profile cannot be deleted")
        shutil.rmtree(self.get(name).root)

    def _own_default(self) -> Profile:
        """The default profile in a folder of its own, made the first time (a port of its own)."""
        path = self.root / DEFAULT_PROFILE
        if not (path / "profile.json").exists():
            used = {self._load(e).port for e in self.root.iterdir() if e.is_dir()} if self.root.is_dir() else set()
            port = self.base_port
            while port in used or not self.is_port_free(port):
                port += 1
            for folder in ("config", "data", "state"):
                (path / folder).mkdir(parents=True, exist_ok=True)
            write_json(path / "profile.json", {"port": port})
        return self._load(path)

    def _load(self, path: Path) -> Profile:
        port = read_json(path / "profile.json").get("port")
        return Profile(path.name, root=path, port=port if isinstance(port, int) else None)

    def reassign_port(self, name: str) -> Profile:
        """Give the profile a new port that is free on this machine."""
        profile = self.get(name)
        updated = replace(profile, port=self._next_free_port(exclude=profile.port))
        write_json(profile.root / "profile.json", {"port": updated.port})
        return updated

    def _next_free_port(self, exclude: int | None = None) -> int:
        used = {p.port for p in self.list() if p.port is not None} | {exclude}
        port = self.base_port
        while port in used or not self.is_port_free(port):
            port += 1
        return port


def validate_name(name: str) -> None:
    if not NAME_PATTERN.match(name):
        raise ProfileError("Use a-z, 0-9, '-' or '_' (max 32 chars)")


def _copy_config(source: Path, target: Path) -> None:
    for entry in source.iterdir():
        if entry.name in _UNCOPYABLE_CONFIG:
            continue
        if entry.is_dir():
            # Follow symlinks: the copy must not point back into this machine's config.
            shutil.copytree(entry, target / entry.name)
        else:
            shutil.copy2(entry, target / entry.name)
