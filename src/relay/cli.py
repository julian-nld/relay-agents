"""Relay's command line: run the server and manage accounts."""

from __future__ import annotations

import argparse
import getpass
import os
import socket
import sys
from pathlib import Path

from relay import __version__, config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="relay", description="Self-hosted chat and coding agents.",
                                     epilog="As a service: scripts/install-relay.sh (--help). Upgrading and removing "
                                            "Relay: docs/GUIDE.md, Running Relay.")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    serve = sub.add_parser("serve", help="run the web app")
    serve.add_argument("--host", help="address to listen on (default: settings, all interfaces)")
    serve.add_argument("--port", type=int, help="port (default: settings, 8420)")

    user = sub.add_parser("user", help="manage accounts")
    user_sub = user.add_subparsers(dest="action", metavar="ACTION", required=True)
    add = user_sub.add_parser("add", help="create an account")
    add.add_argument("username")
    password = user_sub.add_parser("password", help="set an account's password")
    password.add_argument("username")

    sub.add_parser("providers", help="list configured providers")
    check = sub.add_parser("provider-check", help="check a provider fully, as Settings → Providers does: the running "
                                                  "Relay runs it through a few short prompts")
    check.add_argument("provider", help="the provider's id (relay providers lists them)")
    check.add_argument("--model", default="", help="the model to try (default: its default)")
    check.add_argument("--user", help="whose Relay account to check it as (default: the only one)")
    sub.add_parser("skills", help="list Relay's skills and where to add your own")
    plugins = sub.add_parser("plugins", help="list the plugins; turn one on or off (from Relay's next start, also "
                                             "when Relay doesn't start); check a plugin's folder")
    plugins.add_argument("action", nargs="?", choices=["on", "off", "check"])
    plugins.add_argument("name", nargs="?", help="a plugin's name (on, off), or its folder (check)")
    claude = sub.add_parser("claude", help="run Claude Code with Relay's own login, apart from ~/.claude "
                                           "(type /login in it to log in)", add_help=False)
    claude.add_argument("args", nargs=argparse.REMAINDER)
    doctor = sub.add_parser("doctor", help="check that this machine can run Relay and its providers")
    doctor.add_argument("--no-providers", action="store_true", help="skip checking the providers")
    doctor.add_argument("--port", type=int, help="check this port instead of the configured one")
    doctor.add_argument("--build", action="store_true",
                        help="build the agents' container image if it isn't there (a few minutes, once)")
    backup = sub.add_parser("backup", help="write all of Relay's data to one archive (also while it runs)")
    backup.add_argument("file", nargs="?", help="the archive (default: relay-backup-<date>.tar.gz here)")
    unit = sub.add_parser("systemd-unit", help="print a systemd service unit for this install")
    unit.add_argument("--user", action="store_true",
                      help="a user service (systemctl --user) instead of a system one")
    unit.add_argument("--port", type=int, help="port to serve on")
    port = sub.add_parser("port", help="the port Relay serves on; with a number, serve on that one from its next "
                                       "start (kept in settings.json)")
    port.add_argument("number", nargs="?", type=int)
    urls = sub.add_parser("urls")  # internal: addresses Relay can be reached at
    urls.add_argument("--port", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["claude"]:
        # Everything after it is Claude Code's, options included.
        from relay import relocation

        return relocation.run_claude(argv[1:])
    args = build_parser().parse_args(argv)
    # The data holds logins and API keys: a data folder Relay makes is its user's alone (one that's there stays so)
    config.data_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    find_agents()
    if args.command == "serve":
        return serve(args.host, args.port)
    if args.command == "user":
        return user(args.action, args.username)
    if args.command == "providers":
        return providers()
    if args.command == "provider-check":
        return provider_check(args.provider, args.model, args.user)
    if args.command == "skills":
        from relay import skills

        for skill in skills.all_skills():
            origin = "built in" if skill.builtin else "yours"
            print(f"{skill.name}  ({origin}{', read-only' if skill.access == 'read' else ''})\n  {skill.description}")
        print(f"\nYour own skills go in {skills.user_dir()}/<name>/SKILL.md")
        return 0
    if args.command == "plugins":
        return plugins_command(args.action, args.name)
    if args.command == "port":
        if args.number:
            config.save_port(args.number)
        print(config.load_settings().port)
        return 0
    if args.command == "urls":
        settings = config.load_settings()
        for address in _addresses(settings.host):
            print(f"http://{address}:{args.port or settings.port}")
        return 0
    if args.command == "doctor":
        from relay import doctor

        settings = config.load_settings()
        if args.port:
            settings.port = args.port
        return doctor.report(doctor.run_checks(settings, check_providers=not args.no_providers, build=args.build))
    if args.command == "backup":
        from relay import backup

        return backup.main(args.file)
    if args.command == "systemd-unit":
        print(systemd_unit(user_service=args.user, port=args.port))
        return 0
    return 2


def plugins_command(action: str | None, name: str | None) -> int:
    """`relay plugins`: what's in the plugins folder (without running a plugin's code); `on`/`off NAME` for Relay's
    next start (a plugin that keeps Relay from working is turned off here); `check FOLDER` loads one as Relay
    would, and says what it adds."""
    from relay import plugins as engine

    settings = config.load_settings()
    if action in ("on", "off"):
        if not name:
            print(f"relay plugins {action} NAME", file=sys.stderr)
            return 2
        settings.plugins_off = [n for n in settings.plugins_off if n != name] + ([name] if action == "off" else [])
        config.save_settings(settings)
        print(f"{name} is {action} from Relay's next start (Settings → Plugins → Restart Relay, or "
              "systemctl --user restart relay)")
        return 0
    if action == "check":
        folder = Path(name or ".").expanduser().resolve()
        found = engine.Plugins(folder=folder.parent).check(folder)
        if found.problem:
            print(f"{found.name} can't be loaded: {found.problem}")
            return 1
        plugin = found.plugin
        print(f"{plugin.label} {plugin.version} ({found.name}) loads.")
        for route in plugin.routes:
            print(f"  route  {'/'.join(route.methods):<14} /api/plugins/{found.name}/{route.path}")
        for tool in plugin.tools:
            print(f"  tool   {tool.name}{' (the Governor too)' if tool.governor else ''}"
                  f"{' (some conversations only)' if tool.only else ''}: {tool.description}")
        for task in plugin.tasks:
            print(f"  task   every {task.every:g} seconds")
        if plugin.hooks:
            print(f"  hook   on_run_ended ×{len(plugin.hooks)}")
        page = [f"static/{file}" for file in ("index.js", "style.css") if (folder / "static" / file).is_file()]
        print(f"  page   {', '.join(page) or 'none'}")
        return 0
    found = engine.Plugins()
    found.load(settings.plugins_off, run=False)
    for plugin in found.found.values():
        state = f"can't be loaded: {plugin.problem}" if plugin.problem else "on" if plugin.enabled else "off"
        print(f"{plugin.name}  {plugin.manifest.get('version') or ''}  {state}\n  {plugin.folder}")
    print(f"\nPlugins go in {found.folder}/<name>/ (docs/WRITING-A-PLUGIN.md)")
    return 0


SHUTDOWN_WAIT = 5  # seconds open requests (an answer's stream) have when Relay stops, before they're cut off


def serve(host: str | None, port: int | None) -> int:
    import uvicorn

    from relay.app import create_app

    settings = config.load_settings()
    # (where it listens is also where agents reach it: their tools and MCP server, local_url)
    host = settings.host = host or settings.host
    port = settings.port = port or settings.port
    # Run by systemd, podman would keep each container's conmon in Relay's own unit (it sees
    # INVOCATION_ID), and stopping or restarting Relay would kill those: a box that outlives it
    # (a ProductionBox, a WorkBox) loses its output (a print then fails: EPIPE), and the stop
    # waits for them. Without it, each conmon gets a scope of its own.
    os.environ.pop("INVOCATION_ID", None)

    app = create_app(settings)
    print(f"Relay {__version__}  (data: {config.data_dir()})", flush=True)
    for address in _addresses(host):
        print(f"  http://{address}:{port}", flush=True)
    if settings.auth == "none":
        print("  Login is disabled: everyone who can open Relay can run agents here.", flush=True)
    # (an answer's event stream stays open until its run ends, and runs are stopped at the app's shutdown, which
    # uvicorn holds back until every connection is done: without a limit, Relay never stopped while one was read)
    uvicorn.run(app, host=host, port=port, log_level="warning", proxy_headers=True,
                forwarded_allow_ips=os.environ.get("RELAY_FORWARDED_ALLOW_IPS", "127.0.0.1"),
                timeout_graceful_shutdown=SHUTDOWN_WAIT)
    return 0


def _addresses(host: str) -> list[str]:
    """Addresses to show for reaching the server."""
    if host not in ("0.0.0.0", "::"):
        return [host]
    addresses = ["localhost"]
    try:
        # Finds the LAN address the default route uses; nothing is sent.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            addresses.append(probe.getsockname()[0])
    except OSError:
        pass
    return addresses


def _ask_password() -> str:
    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Repeat password: "):
        raise SystemExit("relay: passwords don't match")
    return first


def user(action: str, username: str) -> int:
    from relay.auth import Accounts, AuthError
    from relay.db import Database

    accounts = Accounts(Database(config.database_path()))
    try:
        if action == "add":
            accounts.create_user(username, _ask_password())
            print(f"Created {username}")
        else:
            accounts.set_password(username, _ask_password())
            print(f"Password of {username} changed; existing sessions were signed out")
    except AuthError as exc:
        print(f"relay: {exc}", file=sys.stderr)
        return 1
    return 0


def providers() -> int:
    from relay.providers import KINDS, Registry

    registry = Registry(config.providers_dir())
    registry.ensure_defaults()
    for template in registry.templates():
        state = "enabled " if template.enabled else "disabled"
        print(f"{template.id:<20} {state} {KINDS[template.kind].LABEL:<26} {template.name}")
    for name, problem in registry.problems().items():
        print(f"{name:<20} BROKEN   {problem}")
    return 0


# Where agents' installers put them (opencode's, Claude Code's, a user's npm and bun): Relay finds one installed
# after it was, or in a shell that hadn't read its new PATH yet when Relay was started or installed.
AGENT_FOLDERS = ("~/.opencode/bin", "~/.local/bin", "~/.npm-global/bin", "~/.bun/bin")


def find_agents() -> None:
    """Add AGENT_FOLDERS to PATH, after what's on it."""
    path = [p for p in os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin").split(os.pathsep) if p]
    added = [str(Path(f).expanduser()) for f in AGENT_FOLDERS]
    os.environ["PATH"] = os.pathsep.join([*path, *(f for f in added if f not in path)])


def provider_check(provider_id: str, model: str, username: str | None) -> int:
    """The running Relay's check of a provider (POST /api/providers/<id>/conformance), signed in for it with a
    session of its own that ends after: the same check as Settings → Providers → Check it fully."""
    import json
    import urllib.error
    import urllib.request

    from relay.auth import Accounts
    from relay.db import Database
    from relay.web import COOKIE, CSRF_HEADER, local_url

    settings = config.load_settings()
    db = Database(config.database_path())
    accounts = Accounts(db)
    users = db.all("SELECT id, username FROM users ORDER BY id")
    chosen = [u for u in users if u["username"] == username] if username else users
    if len(chosen) != 1:
        print("Name the account with --user: " + ", ".join(u["username"] for u in users) if users
              else "No account yet: open Relay and make one first", file=sys.stderr)
        return 2
    token = accounts.start_session(chosen[0]["id"], 1)
    request = urllib.request.Request(f"{local_url(settings)}/api/providers/{provider_id}/conformance",
                                     data=json.dumps({"model": model}).encode(), method="POST",
                                     headers={"Content-Type": "application/json", CSRF_HEADER: "1",
                                              "Cookie": f"{COOKIE}={token}"})
    print(f"Checking {provider_id} in the running Relay: a few short prompts, a minute or two…", flush=True)
    try:
        with urllib.request.urlopen(request, timeout=1800) as response:
            answer = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        print(f"Relay said: {json.loads(exc.read() or b'{}').get('error') or exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Relay isn't reachable at {local_url(settings)} ({exc}): is it running?", file=sys.stderr)
        return 1
    finally:
        accounts.end_session(token)
    for result in answer["results"]:
        mark = "✓" if result["ok"] else "✗" if result["ok"] is False else "–"
        print(f"  {mark} {result['name']:<10} {result['detail']}")
    print("It does what Relay needs of it." if answer["ok"] else "Not yet: docs/ADDING-A-PROVIDER.md says how.",
          "(– couldn't be shown; it doesn't count against it.)" if any(r["ok"] is None for r in answer["results"])
          else "")
    return 0 if answer["ok"] else 1


def systemd_unit(user_service: bool = False, port: int | None = None) -> str:
    """A systemd unit that runs this install of Relay and restarts it on failure."""
    from opencode_manager import paths

    command = f"/bin/sh {paths.root() / 'relay'} serve" + (f" --port {port}" if port else "")
    if user_service:
        where = "~/.config/systemd/user/relay.service"
        how = "systemctl --user daemon-reload && systemctl --user enable --now relay"
        user_line, target = "", "default.target"
    else:
        where = "/etc/systemd/system/relay.service"
        how = "sudo systemctl daemon-reload && sudo systemctl enable --now relay"
        # Rootless podman (the agents' containers) needs the user's runtime folder; lingering
        # (install-relay.sh turns it on) makes it exist from boot.
        user_line = f"User={getpass.getuser()}\nEnvironment=XDG_RUNTIME_DIR=/run/user/{os.getuid()}\n"
        target = "multi-user.target"
    # Data somewhere other than the default: the service must find it too.
    homes = "".join(f"\nEnvironment={name}={os.environ[name]}"
                    for name in (paths.RELAY_HOME_ENV, paths.HOME_ENV) if os.environ.get(name))
    return f"""\
# Save as {where}, then:
#   {how}
[Unit]
Description=Relay (chat and coding agents)
After=network-online.target
Wants=network-online.target

[Service]
{user_line}WorkingDirectory={Path.home()}
ExecStart={command}
Restart=on-failure
RestartSec=3
Environment=PATH={os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}
Environment=HOME={Path.home()}{homes}

[Install]
WantedBy={target}"""
