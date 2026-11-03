"""Thin wrapper around the tmux CLI."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


class TmuxError(Exception):
    pass


def run(*args: str) -> str:
    result = subprocess.run(["tmux", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise TmuxError(result.stderr.strip() or f"tmux {args[0]} failed")
    return result.stdout.strip()


def inside_tmux() -> bool:
    return bool(os.environ.get("TMUX"))


def has_session(name: str) -> bool:
    try:
        run("has-session", "-t", f"={name}")
    except TmuxError:
        return False
    return True


def _env_args(env: dict[str, str]) -> list[str]:
    return [arg for key, value in env.items() for arg in ("-e", f"{key}={value}")]


def new_session(
    name: str,
    cwd: Path,
    command: str,
    env: dict[str, str],
    size: tuple[int, int] | None = None,
) -> str:
    """Create a detached session and return the id of its first pane."""
    args = ["new-session", "-d", "-s", name, "-c", str(cwd), "-P", "-F", "#{pane_id}"]
    if size:
        args += ["-x", str(size[0]), "-y", str(size[1])]
    return run(*args, *_env_args(env), command)


def split_left(
    target: str, cwd: Path, command: str, width: int, env: dict[str, str]
) -> str:
    """Split `target` with a new pane of `width` columns on its left."""
    return run(
        "split-window", "-h", "-b", "-l", str(width), "-t", target,
        "-c", str(cwd), "-P", "-F", "#{pane_id}", *_env_args(env), command,
    )


def respawn_pane(pane: str, cwd: Path, command: str, env: dict[str, str]) -> None:
    run("respawn-pane", "-k", "-t", pane, "-c", str(cwd), *_env_args(env), command)


def popup(target: str, cwd: Path, command: str, title: str) -> int:
    """Run `command` in a popup over the session; block until it closes.

    Returns the command's exit status.
    """
    result = subprocess.run(
        [
            "tmux", "display-popup", "-E", "-t", target, "-d", str(cwd),
            "-w", "90%", "-h", "90%", "-T", f" {title} ", command,
        ],
        capture_output=True,
        text=True,
    )
    return result.returncode


def select_pane(pane: str) -> None:
    run("select-pane", "-t", pane)


# set-option/show-options resolve their target as a pane, so an exact session
# match needs the trailing ":" ("=name" alone fails with "no such session").
def set_session_option(session: str, name: str, value: str) -> None:
    run("set-option", "-t", f"={session}:", name, value)


def get_session_option(session: str, name: str) -> str | None:
    value = run("show-options", "-v", "-q", "-t", f"={session}:", name)
    return value or None


def set_session_hook(session: str, hook: str, command: str) -> None:
    run("set-hook", "-t", f"={session}:", hook, command)


def set_pane_option(pane: str, name: str, value: str) -> None:
    run("set-option", "-p", "-t", pane, name, value)


def session_of_pane(pane: str) -> str:
    return run("display-message", "-p", "-t", pane, "#{session_name}")


def kill_pane(pane: str) -> None:
    run("kill-pane", "-t", pane)


def kill_session(name: str) -> None:
    run("kill-session", "-t", f"={name}")


def attach(name: str) -> None:
    """Replace the current process with a tmux client for session `name`."""
    if inside_tmux():
        os.execvp("tmux", ["tmux", "switch-client", "-t", f"={name}"])
    os.execvp("tmux", ["tmux", "attach-session", "-t", f"={name}"])
