"""Everything that knows how to invoke the opencode CLI for a profile."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
from functools import lru_cache
from pathlib import Path

from opencode_manager import configfile, paths, ports
from opencode_manager.bridge.http import Client, HttpError
from opencode_manager.jsonfile import read_json
from opencode_manager.profiles import Profile, ProfileStore
from opencode_manager.settings import Settings


class OpencodeError(Exception):
    pass


def configured_executable(value: str) -> str:
    """Resolve an executable from settings: "~" is expanded and a relative path
    with a directory is taken relative to the code folder (a bare name is
    looked up on PATH)."""
    path = Path(value).expanduser()
    if len(path.parts) > 1 and not path.is_absolute():
        return str(paths.root() / path)
    return str(path)


def binary(settings: Settings) -> str:
    """The opencode executable to run on this machine."""
    if settings.opencode_bin:
        return configured_executable(settings.opencode_bin)
    return paths.program("opencode")


def command(
    settings: Settings, profile: Profile, directory: Path, *, login_first: bool = False
) -> str:
    """Shell command that runs opencode for `profile` in `directory`."""
    run = [binary(settings)]
    if settings.launch_mode == "standalone":
        run.append("--standalone")
    run.append(str(directory))
    cmd = shlex.join(run)
    if login_first:
        login = shlex.join([binary(settings), "auth", "login"])
        cmd = f"{login}; exec {cmd}"
    return cmd


# opencode's own default port for its background service (used when a
# service.json doesn't name one, i.e. for the "default" profile).
DEFAULT_SERVICE_PORT = 49374

# Programmatic access goes through opencode's background service (`opencode service`), which came with opencode 2:
# opencode.ai/install still gives opencode 1, which has none.
INSTALL_V2 = "curl -fsSL https://opencode.ai/v2/install | bash"


def require_service(settings: Settings) -> None:
    """Raise OpencodeError when the installed opencode has no background service (opencode 1)."""
    path = shutil.which(binary(settings))
    try:
        changed = os.stat(path).st_mtime if path else None
    except OSError:
        return
    if changed is not None and not _has_service(path, changed):
        version = _output([path, "--version"]).strip().removeprefix("opencode ").removeprefix("v")
        raise OpencodeError(f"opencode {version or '(its version unknown)'} has no background service "
                            f"(`opencode service`), which Relay uses: install opencode 2 with `{INSTALL_V2}`")


@lru_cache(maxsize=8)
def _has_service(path: str, changed: float) -> bool:
    """Asked once per program (its path, and when it last changed: an upgrade). Unclear stays True: starting it
    then says what's wrong."""
    output = _output([path, "service", "--help"])
    return output == "" or "opencode service" in output


def _output(argv: list[str]) -> str:
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout + done.stderr


def prepare(settings: Settings, store: ProfileStore, profile: Profile) -> Profile:
    """Make sure the profile's background service uses a port of its own.

    Without this every profile would try to start its service on opencode's
    default port, which is already taken by the default profile's service. If
    the profile's port is taken by something else on this machine, the profile
    moves to a free one. Returns the (possibly updated) profile.
    """
    if settings.launch_mode != "service":
        return profile
    return _assign_service_port(settings, store, profile)


def _assign_service_port(settings: Settings, store: ProfileStore, profile: Profile) -> Profile:
    if profile.is_default or profile.port is None:
        return profile
    if not ports.is_free(profile.port) and not serves(settings, profile):
        profile = store.reassign_port(profile.name)
    if _service_config(profile).get("port") != profile.port:
        _run(settings, profile, "service", "set", "port", str(profile.port))
    return profile


def _service_config(profile: Profile) -> dict:
    return read_json(configfile.config_dir(profile) / "service.json")


def service_port(profile: Profile) -> int:
    port = _service_config(profile).get("port")
    return port if isinstance(port, int) else profile.port or DEFAULT_SERVICE_PORT


def ensure_service(
    settings: Settings, store: ProfileStore, profile: Profile, timeout: float = 30,
    env: dict[str, str] | None = None,
) -> tuple[str, str]:
    """Start the profile's background service if needed; return (url, password).

    Works in every launch mode: programmatic access always goes through the
    profile's service. `env` goes to a service started now (it keeps the
    environment it started with).
    """
    require_service(settings)
    profile = _assign_service_port(settings, store, profile)
    port = service_port(profile)
    taken = not ports.is_free(port)
    if not taken:
        _run(settings, profile, "service", "start", env=env)
    started = time.monotonic()
    while True:
        password = _service_config(profile).get("password")
        if password and serves(settings, profile):
            return f"http://127.0.0.1:{port}", password
        if taken and time.monotonic() > started + 5:  # (a moment for one that's starting; then it's not ours)
            free = next((p for p in range(port + 1, port + 500) if ports.is_free(p)), None)
            raise OpencodeError(
                f"Port {port}, where opencode's service for {profile.name!r} runs, is taken by another program "
                f"(another user's opencode, say). Give it another one, as the user Relay runs as: "
                f"opencode service set port {free or '<a free port>'}")
        if time.monotonic() > started + timeout:
            raise OpencodeError(f"The opencode service for {profile.name!r} did not start")
        time.sleep(0.3)


def serves(settings: Settings, profile: Profile) -> bool:
    """True if the server on the profile's port is the profile's own service.

    Only the profile's own service accepts the password in its service.json.
    (/api/session checks it; /global/health does not.)
    """
    password = _service_config(profile).get("password")
    if not password:
        return False
    client = Client(f"http://127.0.0.1:{service_port(profile)}", "opencode", password, timeout=5)
    try:
        client.request("GET", "/api/session?limit=1")
    except HttpError:
        return False
    return True


def stop_service(settings: Settings, profile: Profile) -> None:
    """Stop the profile's background service, if it has its own."""
    if not profile.is_default:
        _run(settings, profile, "service", "stop", check=False)


def restart_service(settings: Settings, profile: Profile) -> None:
    """Restart the service for `profile` so it rereads its config.

    A stopped profile service is started again by the next opencode client.
    """
    if settings.launch_mode != "service":
        return
    if profile.is_default:
        _run(settings, profile, "service", "restart")
    else:
        _run(settings, profile, "service", "stop")


def start_service_over(
    settings: Settings, store: ProfileStore, profile: Profile, env: dict[str, str]
) -> tuple[str, str]:
    """Stop the profile's service (the default profile's too: here, and where LAUNCHER runs it)
    and start it with `env` (where LAUNCHER runs it)."""
    _run(settings, profile, "service", "stop", check=False, here=True)
    if LAUNCHER is not None:
        _run(settings, profile, "service", "stop", check=False)
    # Its port takes a moment to come free; started before that, the service would move to another.
    deadline = time.monotonic() + 10
    while not ports.is_free(service_port(profile)) and time.monotonic() < deadline:
        time.sleep(0.2)
    return ensure_service(settings, store, profile, env=env)


def service_processes(profile: Profile) -> list[tuple[int, dict[str, str]]]:
    """(pid, environment) of the profile's running service processes (Linux; [] elsewhere)."""
    found = []
    want = profile.env().get("OPENCODE_CONFIG_DIR")
    for entry in Path("/proc").glob("[0-9]*") if Path("/proc").is_dir() else []:
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
            if not any(a.endswith(b"opencode") for a in argv[:1]) or b"--service" not in argv:
                continue
            environ = dict(item.split("=", 1) for item in (entry / "environ").read_bytes().decode(
                errors="replace").split("\0") if "=" in item)
        except OSError:
            continue
        have = environ.get("OPENCODE_CONFIG_DIR")
        if have == want or (have and want and os.path.realpath(have) == os.path.realpath(want)):
            found.append((int(entry.name), environ))
    return found


def stop_services(settings: Settings, profiles: list[Profile]) -> None:
    """Stop the services of all `profiles`, so their data is no longer in use."""
    for profile in profiles:
        stop_service(settings, profile)


# (argv, env) -> argv that runs an opencode command elsewhere (e.g. in a container, so the
# services it starts run there too), with that env; None: here. Set by Relay.
LAUNCHER = None


def _run(settings: Settings, profile: Profile, *args: str, check: bool = True,
         env: dict[str, str] | None = None, here: bool = False) -> None:
    cmd = [binary(settings), *args]
    launcher = None if here else LAUNCHER
    try:
        subprocess.run(
            launcher(cmd, {**profile.env(), **(env or {})}) if launcher else cmd,
            # (with a launcher the profile's environment goes along in its command: what runs it,
            # e.g. podman, keeps ours, or it would look for its containers in the profile's folders)
            env=os.environ if launcher else {**os.environ, **profile.env(), **(env or {})},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=check,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip().splitlines()
        raise OpencodeError(f"`{shlex.join(cmd)}` failed: {detail[-1] if detail else exc}") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OpencodeError(f"`{shlex.join(cmd)}` failed: {exc}") from exc
