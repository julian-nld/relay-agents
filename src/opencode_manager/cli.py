"""Command line entry point.

Besides starting the workspace, the CLI is the scripting interface of
opencode-manager. Commands that return data accept --json. Exit codes:

    0  success
    1  error (message on stderr)
    2  invalid usage
    3  the workspace is not running (for commands that control it)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sys
from pathlib import Path
from typing import Any

from opencode_manager import __version__, opencode, paths, ports, scripting, tmux, workspace
from opencode_manager import settings as settings_mod
from opencode_manager import state as state_mod
from opencode_manager.bridge.http import HttpError
from opencode_manager.profiles import DEFAULT_PROFILE, ProfileError, ProfileStore

EXIT_ERROR = 1
EXIT_NOT_RUNNING = 3
EXIT_INTERRUPTED = 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="opencode-manager",
        description="Run opencode in tmux next to a sidebar for switching profiles.",
        epilog="Exit codes: 0 success, 1 error, 2 invalid usage, 3 workspace not running.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    json_flag = argparse.ArgumentParser(add_help=False)
    json_flag.add_argument("--json", action="store_true", help="print JSON")

    start = sub.add_parser("start", help="start or attach to the workspace (default)")
    start.add_argument("directory", nargs="?", default=".", help="project directory")
    start.add_argument("-p", "--profile", help="profile to start with")
    sub.add_parser(
        "stop", help="close the workspace and stop all profile services (run before ejecting)"
    )

    sub.add_parser("status", parents=[json_flag], help="show workspace and profile status")
    sub.add_parser("profiles", parents=[json_flag], help="list profiles")
    switch = sub.add_parser("switch", parents=[json_flag], help="switch the workspace to a profile")
    switch.add_argument("profile")
    open_ = sub.add_parser("open", parents=[json_flag], help="open a project in the workspace")
    open_.add_argument("directory")
    sub.add_parser("restart", parents=[json_flag], help="restart opencode in the workspace")

    profile = sub.add_parser("profile", help="create or delete profiles")
    profile_sub = profile.add_subparsers(dest="action", metavar="ACTION", required=True)
    create = profile_sub.add_parser("create", parents=[json_flag], help="create a profile")
    create.add_argument("name")
    create.add_argument(
        "--copy-config", action="store_true", help="copy this machine's opencode config"
    )
    delete = profile_sub.add_parser("delete", parents=[json_flag], help="delete a profile")
    delete.add_argument("name")
    delete.add_argument(
        "--yes", action="store_true", help="confirm: its auth and sessions are removed"
    )

    config = sub.add_parser("config", help="locate or check a profile's opencode.json")
    config_sub = config.add_subparsers(dest="action", metavar="ACTION", required=True)
    path = config_sub.add_parser("path", parents=[json_flag], help="print the config file path")
    path.add_argument("profile")
    check = config_sub.add_parser("check", parents=[json_flag], help="validate the config file")
    check.add_argument("profile")

    oc = sub.add_parser(
        "oc",
        help="run an opencode command with a profile's config and auth",
        description="Run opencode with a profile's environment, e.g. "
        "`opencode-manager oc -p work -- run \"fix the tests\"`.",
    )
    oc.add_argument("-p", "--profile", help="profile (default: the active one)")
    oc.add_argument("args", nargs=argparse.REMAINDER, help="arguments for opencode")

    stream = sub.add_parser(
        "stream",
        help="send a prompt to an agent session and stream the events",
        description="Send a prompt to an opencode or Claude Code session (tools enabled, "
        "permissions auto-approved) and stream uniform events as JSON lines. The first "
        "event names the session; continue the conversation with --session ID or --continue.",
    )
    stream.add_argument("prompt", nargs="*", help="the prompt (default: read from stdin)")
    stream.add_argument(
        "-b", "--backend", choices=scripting.BACKENDS, default="opencode",
        help="agent to run (default: opencode)",
    )
    stream.add_argument("-p", "--profile", help="opencode profile (default: the active one)")
    conversation = stream.add_mutually_exclusive_group()
    conversation.add_argument("-s", "--session", help="continue this session")
    conversation.add_argument(
        "-c", "--continue", dest="continue_last", action="store_true",
        help="continue the last session of this profile",
    )
    stream.add_argument("-d", "--dir", help="project directory for a new session (default: cwd)")
    stream.add_argument("-m", "--model", help="provider/model for a new session")
    stream.add_argument("--agent", help="agent for a new session")
    stream.add_argument(
        "--no-auto", dest="auto", action="store_false",
        help="don't auto-approve permission requests",
    )
    stream.add_argument("--text", action="store_true", help="print readable text, not JSON")

    edit = sub.add_parser("edit", help="edit a profile's opencode.json interactively")
    edit.add_argument("profile", help="profile name")

    # Used internally by the workspace.
    sub.add_parser("sidebar")
    pick = sub.add_parser("pick-project")
    pick.add_argument("--current", required=True)
    pick.add_argument("--output", required=True)
    parser.command_names = frozenset(sub.choices)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    # `opencode-manager [dir] [-p profile]` is shorthand for `start`.
    if not argv or argv[0] not in {*parser.command_names, "-h", "--help", "--version"}:
        argv = ["start", *argv]
    args = parser.parse_args(argv)

    settings = settings_mod.load()
    store = ProfileStore(paths.profiles_dir(), settings.base_port, is_port_free=ports.is_free)

    try:
        return _dispatch(args, settings, store)
    except scripting.NotRunning as exc:
        print(f"opencode-manager: {exc}", file=sys.stderr)
        return EXIT_NOT_RUNNING
    except (
        ProfileError,
        scripting.ScriptError,
        tmux.TmuxError,
        opencode.OpencodeError,
        HttpError,
        OSError,
    ) as exc:
        print(f"opencode-manager: {exc}", file=sys.stderr)
        return EXIT_ERROR


def _dispatch(args: argparse.Namespace, settings, store: ProfileStore) -> int:
    command = args.command
    if command == "start":
        return _start(settings, store, Path(args.directory), args.profile)
    if command == "stop":
        return _stop(settings, store)
    if command == "status":
        return _output(args, scripting.status(settings, store), _format_status)
    if command == "profiles":
        return _output(args, scripting.list_profiles(settings, store), _format_profiles)
    if command == "switch":
        data = scripting.switch(settings, store, args.profile)
        return _output(args, data, lambda d: f"Switched to {d['profile']}")
    if command == "open":
        data = scripting.open_project(settings, store, args.directory)
        return _output(args, data, lambda d: f"Opened {d['project']}")
    if command == "restart":
        data = scripting.restart(settings, store)
        return _output(args, data, lambda d: f"Restarted opencode ({d['profile']})")
    if command == "profile":
        return _profile(args, settings, store)
    if command == "config":
        return _config(args, store)
    if command == "stream":
        return _stream(args, settings, store)
    if command == "oc":
        oc_args = args.args[1:] if args.args[:1] == ["--"] else args.args
        scripting.exec_opencode(settings, store, args.profile, oc_args)
    if command == "edit":
        return _edit(store, args.profile)
    if command == "sidebar":
        return _sidebar(settings, store)
    if command == "pick-project":
        return _pick_project(Path(args.current), Path(args.output))
    raise AssertionError(f"unhandled command {command!r}")


def _output(args: argparse.Namespace, data: Any, format_text) -> int:
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2))
    else:
        print(format_text(data))
    return 0


def _format_status(data: dict[str, Any]) -> str:
    state = "running" if data["running"] else "not running"
    lines = [
        f"workspace  {state} (session {data['session']})",
        f"profile    {data['profile']}",
        f"project    {data['project'] or '-'}",
        f"mode       {data['launch_mode']}",
        "",
        _format_profiles(data["profiles"]),
    ]
    return "\n".join(lines)


def _format_profiles(profiles: list[dict[str, Any]]) -> str:
    lines = []
    for p in profiles:
        marker = "*" if p["active"] else " "
        service = p["service"] or ""
        location = p["path"] or "(this machine's own opencode config and data)"
        lines.append(f"{marker} {p['name']:<16} {service:<11} {location}")
    return "\n".join(lines)


def _profile(args: argparse.Namespace, settings, store: ProfileStore) -> int:
    if args.action == "create":
        data = scripting.create_profile(settings, store, args.name, args.copy_config)
        return _output(args, data, lambda d: f"Created profile {d['name']}")
    if not args.yes:
        raise scripting.ScriptError(
            f"Deleting {args.name!r} removes its auth and sessions; pass --yes to confirm"
        )
    data = scripting.delete_profile(settings, store, args.name)
    return _output(args, data, lambda d: f"Deleted profile {d['deleted']}")


def _config(args: argparse.Namespace, store: ProfileStore) -> int:
    if args.action == "path":
        path = scripting.config_path(store, args.profile)
        return _output(args, {"path": path}, lambda d: d["path"])
    data = scripting.check_config(store, args.profile)
    return _output(
        args, data, lambda d: f"OK: {d['path']}" + ("" if d["exists"] else " (does not exist)")
    )


def _stream(args: argparse.Namespace, settings, store: ProfileStore) -> int:
    text = " ".join(args.prompt)
    if not text or text == "-":
        text = sys.stdin.read()
    if not text.strip():
        raise scripting.ScriptError("Empty prompt")
    run = scripting.start_stream(
        settings, store, text,
        backend=args.backend,
        profile_name=args.profile,
        session_id=args.session,
        continue_last=args.continue_last,
        directory=Path(args.dir) if args.dir else None,
        model=args.model,
        agent=args.agent,
        auto_approve=args.auto,
    )
    emit = _emit_text if args.text else _emit_json
    status = "failed"
    # A closed terminal or a `kill` must stop the agent too, not orphan it.
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _raise_interrupt)
    try:
        for ev in run.events():
            emit(ev)
            if ev["type"] == "done":
                status = ev["status"]
    except KeyboardInterrupt:
        emit(run.interrupt())
        status = "interrupted"
    return {"succeeded": 0, "interrupted": EXIT_INTERRUPTED}.get(status, EXIT_ERROR)


def _raise_interrupt(signum, frame) -> None:
    raise KeyboardInterrupt


def _emit_json(ev: dict[str, Any]) -> None:
    print(json.dumps(ev), flush=True)


def _emit_text(ev: dict[str, Any]) -> None:
    kind = ev["type"]
    if kind == "text":
        print(ev["delta"], end="", flush=True)
    elif kind == "session":
        who = ev["profile"] or ev["backend"]
        print(f"[session {ev['session']} · {who} · {ev['directory']}]", file=sys.stderr)
    elif kind == "tool_call":
        print(f"\n▸ {ev['name']}: {json.dumps(ev['input'])[:200]}", file=sys.stderr)
    elif kind == "tool_result" and not ev["ok"]:
        print(f"✗ {ev['name']} failed: {ev['output']}", file=sys.stderr)
    elif kind == "permission":
        print(f"  (permission {ev['action']}: {ev['decision'] or 'waiting'})", file=sys.stderr)
    elif kind == "error":
        print(f"\nerror: {ev['message']}", file=sys.stderr)
    elif kind == "done":
        print(flush=True)  # end the answer's last line
        if ev["status"] != "succeeded":
            print(f"[{ev['status']}]", file=sys.stderr)


def _start(settings, store: ProfileStore, directory: Path, profile_name: str | None) -> int:
    for binary in ("tmux", opencode.binary(settings)):
        if shutil.which(binary) is None:
            print(f"opencode-manager: {binary!r} not found in PATH", file=sys.stderr)
            return EXIT_ERROR
    directory = directory.expanduser().resolve()
    if not directory.is_dir():
        print(f"opencode-manager: {directory} is not a directory", file=sys.stderr)
        return EXIT_ERROR

    if not tmux.has_session(settings.session_name):
        name = profile_name or state_mod.load().active_profile
        if profile_name is None and not store.exists(name):
            name = DEFAULT_PROFILE
        workspace.create(settings, store, store.get(name), directory)
    elif profile_name is not None:
        print(
            f"opencode-manager: session {settings.session_name!r} is already running; "
            "attaching without changing profile",
            file=sys.stderr,
        )
    tmux.attach(settings.session_name)
    return 0


def _stop(settings, store: ProfileStore) -> int:
    if tmux.has_session(settings.session_name):
        workspace.Workspace(settings, store, settings.session_name).quit()
    else:
        opencode.stop_services(settings, store.list())
    print("Stopped. Profile data is no longer in use.")
    return 0


def _sidebar(settings, store: ProfileStore) -> int:
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        print("opencode-manager: the sidebar must run inside tmux", file=sys.stderr)
        return EXIT_ERROR
    _use_real_terminal_size()
    from opencode_manager.sidebar.app import SidebarApp

    SidebarApp(settings, store, workspace.Workspace.from_pane(settings, store, pane)).run()
    return 0


def _use_real_terminal_size() -> None:
    # Textual prefers COLUMNS/LINES over the real terminal size; a value
    # inherited through the tmux session would render at the wrong size.
    os.environ.pop("COLUMNS", None)
    os.environ.pop("LINES", None)


def _pick_project(current: Path, output: Path) -> int:
    from opencode_manager.picker.app import ProjectPickerApp

    _use_real_terminal_size()
    ProjectPickerApp(current, state_mod.recent_projects(), output).run()
    return 0


def _edit(store: ProfileStore, name: str) -> int:
    from opencode_manager import configfile
    from opencode_manager.editor.app import EditorApp

    profile = store.get(name)
    _use_real_terminal_size()
    EditorApp(configfile.config_file(profile), profile.name, profile.is_default).run()
    return 0
