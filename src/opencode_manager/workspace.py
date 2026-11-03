"""The tmux workspace: a sidebar pane on the left, opencode on the right.

Pane ids and the working directory are stored as tmux session options, so the
sidebar (a separate process) can find them again from inside the session.
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys
import tempfile
from pathlib import Path

from opencode_manager import configfile, opencode, paths, tmux
from opencode_manager import state as state_mod
from opencode_manager.profiles import Profile, ProfileStore
from opencode_manager.settings import Settings

OPT_OPENCODE_PANE = "@ocm_opencode_pane"
OPT_SIDEBAR_PANE = "@ocm_sidebar_pane"
OPT_DIRECTORY = "@ocm_directory"
OPT_PROFILE = "@ocm_profile"

# New tmux panes inherit the tmux *server's* environment, which may predate the
# shell that started us. Forward what decides where things are found.
_FORWARDED_ENV = (
    "PATH",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    paths.HOME_ENV,
    "PYTHONPYCACHEPREFIX",
)


def manager_command(*args: str) -> list[str]:
    return [sys.executable, "-m", "opencode_manager", *args]


def sidebar_command() -> str:
    return shlex.join(manager_command("sidebar"))


def _manager_env() -> dict[str, str]:
    """Environment for running this package in a new tmux pane or popup."""
    python_path = os.pathsep.join(str(p) for p in paths.python_path())
    return _pane_env({"PYTHONPATH": python_path})


def _pane_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {key: os.environ[key] for key in _FORWARDED_ENV if key in os.environ}
    return {**env, **(extra or {})}


def create(settings: Settings, store: ProfileStore, profile: Profile, directory: Path) -> None:
    """Create the tmux session with the sidebar and opencode panes."""
    name = settings.session_name
    profile = opencode.prepare(settings, store, profile)
    size = shutil.get_terminal_size()
    main = tmux.new_session(
        name,
        directory,
        opencode.command(settings, profile, directory),
        _pane_env(profile.env()),
        size=(size.columns, size.lines),
    )
    # Keep the pane around when opencode exits so it can be restarted.
    tmux.set_pane_option(main, "remain-on-exit", "on")
    # The sidebar reads these on startup, so they must be set before it runs.
    tmux.set_session_option(name, OPT_OPENCODE_PANE, main)
    tmux.set_session_option(name, OPT_DIRECTORY, str(directory))
    tmux.set_session_option(name, OPT_PROFILE, profile.name)

    sidebar = tmux.split_left(
        main, directory, sidebar_command(), settings.sidebar_width, env=_manager_env()
    )
    # If the sidebar crashes, keep its traceback visible instead of losing the pane.
    tmux.set_pane_option(sidebar, "remain-on-exit", "on")
    tmux.set_session_option(name, OPT_SIDEBAR_PANE, sidebar)
    # tmux scales panes proportionally when the window resizes, which squeezes
    # the sidebar to a few columns in a narrow window. Pin its width instead.
    tmux.set_session_hook(
        name, "window-resized", f"resize-pane -t {sidebar} -x {settings.sidebar_width}"
    )
    tmux.select_pane(main)
    state_mod.remember_profile(profile.name)
    state_mod.remember_project(directory)


class Workspace:
    """Controls a running workspace session; used by the sidebar."""

    def __init__(self, settings: Settings, store: ProfileStore, session: str) -> None:
        self.settings = settings
        self.store = store
        self.session = session

    @classmethod
    def from_pane(cls, settings: Settings, store: ProfileStore, pane: str) -> Workspace:
        return cls(settings, store, tmux.session_of_pane(pane))

    def _option(self, name: str) -> str:
        value = tmux.get_session_option(self.session, name)
        if value is None:
            raise tmux.TmuxError(f"Session {self.session!r} is missing {name}")
        return value

    @property
    def directory(self) -> Path:
        return Path(self._option(OPT_DIRECTORY))

    @property
    def active_profile(self) -> str:
        return self._option(OPT_PROFILE)

    def launch(self, profile: Profile, *, login_first: bool = False) -> None:
        """(Re)start opencode in the main pane with `profile`."""
        profile = opencode.prepare(self.settings, self.store, profile)
        directory = self.directory
        tmux.respawn_pane(
            self._option(OPT_OPENCODE_PANE),
            directory,
            opencode.command(self.settings, profile, directory, login_first=login_first),
            _pane_env(profile.env()),
        )
        tmux.set_session_option(self.session, OPT_PROFILE, profile.name)
        state_mod.remember_profile(profile.name)

    def open_project(self, directory: Path) -> None:
        """Restart opencode in `directory`, keeping the active profile."""
        tmux.set_session_option(self.session, OPT_DIRECTORY, str(directory))
        self.launch(self.store.get(self.active_profile))
        state_mod.remember_project(directory)

    def pick_project(self) -> Path | None:
        """Show the project picker in a popup; return the chosen directory."""
        fd, result_file = tempfile.mkstemp(prefix="opencode-manager-project-")
        os.close(fd)
        try:
            command = manager_command(
                "pick-project", "--current", str(self.directory), "--output", result_file
            )
            env = [f"{key}={value}" for key, value in _manager_env().items()]
            tmux.popup(
                self._option(OPT_SIDEBAR_PANE),
                self.directory,
                shlex.join(["env", *env, *command]),
                title="Open project",
            )
            chosen = Path(result_file).read_text().strip()
        finally:
            Path(result_file).unlink(missing_ok=True)
        return Path(chosen) if chosen else None

    def edit_config(self, profile: Profile) -> str | None:
        """Open the profile's opencode.json in a popup editor; block until closed.

        Returns a warning if the configured external editor was unavailable.
        """
        path = configfile.config_file(profile)
        warning = None
        external = shlex.split(self.settings.editor)
        if external and shutil.which(external[0]):
            command = [*external, str(path)]
        else:
            if external:
                warning = f"{external[0]!r} not found; used the built-in editor"
            command = manager_command("edit", profile.name)
        # Popups don't inherit our environment, so pass it along explicitly.
        env = [f"{key}={value}" for key, value in _manager_env().items()]
        tmux.popup(
            self._option(OPT_SIDEBAR_PANE),
            path.parent if path.parent.is_dir() else self.directory,
            shlex.join(["env", *env, *command]),
            title=f"{path.name} · {profile.name}",
        )
        return warning

    def focus_opencode(self) -> None:
        tmux.select_pane(self._option(OPT_OPENCODE_PANE))

    def quit(self) -> None:
        """Stop opencode and every profile service, then close the session.

        Profile data may live on removable media, so nothing may keep it open
        after quitting. The client goes first so it cannot restart a service.
        """
        tmux.kill_pane(self._option(OPT_OPENCODE_PANE))
        opencode.stop_services(self.settings, self.store.list())
        tmux.kill_session(self.session)
