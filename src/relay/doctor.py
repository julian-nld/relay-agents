"""`relay doctor`: check that this machine can run Relay and its providers."""

from __future__ import annotations

import re
import shutil
import sqlite3
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from relay import config

GIT_NEEDED = (2, 40)  # git merge-tree --write-tree --merge-base (relay.forks)

OK, WARN, FAIL = "ok", "warn", "fail"
SYMBOL = {OK: "✓", WARN: "!", FAIL: "✗"}


@dataclass
class Check:
    status: str
    title: str
    detail: str = ""


def run_checks(settings: config.Settings | None = None, check_providers: bool = True,
               build: bool = False) -> list[Check]:
    settings = settings or config.load_settings()
    checks = [_python(), _dependencies(), _data_dir(), _git(), *_roots(settings), _accounts(settings),
              _port(settings), *_plugins(settings)]
    if check_providers:  # (podman's check may pull its image first)
        checks += [_containers(settings, build), *_leftovers(), *_providers()]
    else:
        checks += _provider_folders()  # (known without asking a provider anything)
    return checks


def report(checks: list[Check]) -> int:
    """Print the checks; exit status 1 if any of them failed."""
    for check in checks:
        line = f" {SYMBOL[check.status]} {check.title}"
        print(line + (f" — {check.detail}" if check.detail else ""))
    failed = sum(c.status == FAIL for c in checks)
    warned = sum(c.status == WARN for c in checks)
    print()
    if failed:
        print(f"{failed} problem(s) prevent Relay from running properly.")
    elif warned:
        print(f"Relay can run; {warned} warning(s) above may limit what it can do.")
    else:
        print("Everything looks good.")
    return 1 if failed else 0


def _python() -> Check:
    version = ".".join(map(str, sys.version_info[:3]))
    if sys.version_info < (3, 10):  # noqa: UP036 - this check is for whoever starts it on an older one
        return Check(FAIL, f"Python {version}", "Relay needs Python 3.10 or newer")
    return Check(OK, f"Python {version}")


def _dependencies() -> Check:
    missing = []
    for module in ("starlette", "uvicorn", "anyio", "h11"):
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    if missing:
        return Check(FAIL, "Python dependencies", f"missing {', '.join(missing)}; run sh scripts/vendor.sh")
    return Check(OK, "Python dependencies")


def _data_dir() -> Check:
    directory = config.data_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".write-test"
        probe.write_text("x")
        probe.unlink()
    except OSError as exc:
        return Check(FAIL, f"Data folder {directory}", f"not writable: {exc}")
    database = config.database_path()
    if database.exists():
        try:
            sqlite3.connect(f"file:{database}?mode=ro", uri=True).execute("SELECT 1").close()
        except sqlite3.Error as exc:
            return Check(FAIL, f"Database {database}", str(exc))
    if _open_to_others(directory):
        return Check(WARN, f"Data folder {directory}",
                     f"other users of this machine can read it, logins and API keys too: chmod 700 {directory}")
    return Check(OK, f"Data folder {directory}")


def _open_to_others(directory: Path) -> bool:
    """Whether another user can read the folder: it lets them, and so does every folder above it (a home folder
    of 700 keeps a 755 folder in it private). Its group is left out: on most systems it's the user's alone."""
    try:
        folder = directory.resolve()
        return bool(folder.stat().st_mode & stat.S_IROTH) and all(
            parent.stat().st_mode & stat.S_IXOTH for parent in folder.parents)
    except OSError:
        return False


def _git() -> Check:
    if shutil.which("git") is None:
        return Check(WARN, "git", "not installed: code tasks can't show, merge or discard changes")
    version = subprocess.run(["git", "--version"], capture_output=True, text=True).stdout.strip()
    found = re.search(r"(\d+)\.(\d+)", version)
    if found and (int(found.group(1)), int(found.group(2))) < GIT_NEEDED:
        return Check(WARN, version, f"git {GIT_NEEDED[0]}.{GIT_NEEDED[1]} or newer is needed to merge a subagent's "
                     "changes back into its caller's folder (git merge-tree)")
    return Check(OK, version or "git")


def _containers(settings: config.Settings, build: bool) -> Check:
    from relay.containers import ContainerError, Containers
    containers = Containers(settings.container_image or None)
    if shutil.which("podman") and not build and not containers.built():
        return Check(WARN, "Containers (podman)", f"the agents' image {containers.image} isn't built yet: Relay "
                     "builds it when it starts (a few minutes, once), or now with `relay doctor --build`")
    try:
        containers.check()
    except ContainerError as exc:
        return Check(FAIL, "Containers (podman)", str(exc))
    from relay.boxes import Boxes
    boxes = Boxes(containers, settings)
    if not build and not boxes.built():
        return Check(WARN, "Containers (podman)", f"the providers' box image {boxes.image} isn't built yet: Relay "
                     "builds it when it starts, or now with `relay doctor --build`")
    try:
        boxes.check()
    except ContainerError as exc:
        return Check(FAIL, "Containers (podman)", str(exc))
    return Check(OK, "Containers (podman)", f"providers run in {boxes.image}, agents' commands in {containers.image}")


# Boxes whose working folder is no folder of this machine's (relay.containers.stop_idle leaves them alone too):
# another Relay's are never "left behind" by a folder that isn't here
SHARED_BOXES = ("relay-box-", "relay-vpn", "relay-search", "relay-fetch")


def _leftovers() -> list[Check]:
    """Boxes another Relay left behind: a test run or UI walkthrough that was stopped before it
    tidied up. Not this Relay's (another's mark, or a service this one doesn't have), and their
    folder is gone. Only told, never removed: they may be another live Relay's otherwise."""
    from relay.containers import INSTANCE, LABEL, mine
    from relay.production import PROD_LABEL
    if not shutil.which("podman"):
        return []
    try:
        listed = subprocess.run(["podman", "ps", "-a", "--format",
                                 f'{{{{.Names}}}}|{{{{index .Labels "{INSTANCE}"}}}}|{{{{index .Labels "'
                                 f'{LABEL.split("=")[0]}"}}}}'
                                 f'|{{{{index .Labels "{PROD_LABEL}"}}}}'], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return []
    services = set()
    try:
        with sqlite3.connect(f"file:{config.database_path()}?mode=ro", uri=True) as db:
            services = {row[0] for row in db.execute("SELECT id FROM services")}
    except sqlite3.Error:
        pass
    candidates = []
    for line in listed.stdout.splitlines():
        name, owner, managed, service = (line.split("|") + ["", "", ""])[:4]
        service = service.strip()
        if name.startswith(SHARED_BOXES):
            continue  # (a ProviderBox, the VPN, web search and fetch: their folder isn't the host's to look at)
        if (managed.strip() == "1" and not mine(owner)) or (service and service not in services):
            candidates.append(name)
    if not candidates:
        return []
    try:
        found = subprocess.run(["podman", "inspect", "--format", "{{.Name}}|{{.Config.WorkingDir}}", *candidates],
                               capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return []
    gone = [name for name, _, folder in (line.partition("|") for line in found.stdout.splitlines())
            if folder.startswith("/") and folder != "/" and not Path(folder).exists()]
    if not gone:
        return []
    return [Check(WARN, f"Boxes another Relay left behind ({len(gone)})",
                  f"their folders are gone (a test run that was stopped?); remove them: podman rm -f {' '.join(gone)}")]


def _roots(settings: config.Settings) -> list[Check]:
    checks = []
    for root in settings.roots():
        if root.is_dir():
            checks.append(Check(OK, f"Workspace root {root}"))
        else:
            checks.append(Check(WARN, f"Workspace root {root}",
                                f"doesn't exist; create it (mkdir -p {root}) to start code tasks there"))
    if not checks:
        checks.append(Check(WARN, "Workspace roots", "none configured: code tasks are unavailable"))
    return checks


def _accounts(settings: config.Settings) -> Check:
    if settings.auth == "none":
        return Check(WARN, "Login disabled", "everyone who can open Relay can run agents here")
    from relay.auth import Accounts
    from relay.db import Database

    if not config.database_path().exists() or not Accounts(Database(config.database_path())).has_users():
        return Check(OK, "Accounts", "none yet: the first visitor creates one")
    return Check(OK, "Accounts")


def _port(settings: config.Settings) -> Check:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((settings.host if settings.host != "::" else "0.0.0.0", settings.port))
        except OSError:
            if _relay_answers(settings.port):
                return Check(OK, f"Relay is running on port {settings.port}")
            return Check(WARN, f"Port {settings.port}", "in use by another program")
    return Check(OK, f"Port {settings.port} is free")


def _relay_answers(port: int) -> bool:
    import json
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=3) as response:
            return "version" in json.loads(response.read())
    except (OSError, ValueError):
        return False


def _providers() -> list[Check]:
    from relay.providers import KINDS, Registry
    from relay.providers.base import Provider

    registry = Registry(config.providers_dir())
    registry.ensure_defaults()
    checks = []
    Provider.probing = True  # not Relay: no services started outside the boxes
    try:
        for template in registry.templates():
            title = f"Provider {template.name} ({template.kind})"
            if not template.enabled:
                checks.append(Check(OK, title, "disabled"))
                continue
            try:
                message = KINDS[template.kind](template).check()
            except Exception as exc:  # noqa: BLE001 - report whatever the provider raised
                checks.append(Check(WARN, title, str(exc) or type(exc).__name__))
            else:
                checks.append(Check(OK, title, message))
    finally:
        Provider.probing = False
    for name, problem in registry.problems().items():
        checks.append(Check(WARN, f"Provider file {name}", problem))
    return checks + _provider_folders()


def _provider_folders() -> list[Check]:
    """A provider folder with a mistake in it (relay.providers.BROKEN): the others work."""
    from relay.providers import BROKEN
    return [Check(WARN, f"Provider folder {folder}", f"can't be loaded: {problem}")
            for folder, problem in BROKEN.items()]


def _plugins(settings: config.Settings) -> list[Check]:
    """The plugins (relay.plugins), without running their code: their plugin.json, and whether their Python parses
    (`relay plugins check` runs one)."""
    from relay.plugins import Plugins
    plugins = Plugins()
    plugins.load(settings.plugins_off, run=False)
    return [Check(WARN if f.problem else OK, f"Plugin {f.name}",
                  f"can't be loaded: {f.problem}" if f.problem else "found" if f.enabled else "turned off")
            for f in plugins.found.values()]
