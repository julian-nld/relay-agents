"""ProviderBoxes: every provider's program runs in a container of its own.

    Relay (machine) ── starts ──▶ ProviderBox (a container per provider)
       ▲                            claude / opencode-manager's services / copilot
       │                            /bin/bash, /bin/sh = Relay's shell (container/box/relay-shell)
       └──── exec socket ◀──────────┘
       │
       └── podman exec ──▶ the project's WorkBox (relay.containers)

A provider's commands reach the WorkBox however it starts its shell: the box's
/bin/bash and /bin/sh hand every command to Relay over a socket (`ExecGateway`), and Relay
runs it in the WorkBox of its folder (the run's folder, which the provider's process
passes on as RELAY_FOLDER; else the folder the command runs in; only one the box has as a
project: Boxes.projects), past the guard (relay.guard), and ends it when its shell is
stopped. No provider has to cooperate, and the WorkBox never sees a provider's login.

A box has the provider's program (from the machine, read-only), its login and settings, the
workspace roots, Relay's chat and task folders and what Relay hands agents (tools, skills,
attachments); a project elsewhere is added when a run needs it. One box per provider (all of
opencode-manager's services share one), kept running; one made otherwise (other mounts,
another image) is made anew when no run is using it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import socketserver
import struct
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from relay import config, guard
from relay.containers import (
    CONTAINERFILE,
    HOME,
    INSTANCE,
    SCRATCH,
    ContainerError,
    Containers,
    build_image,
    instance,
    mine,
    spec_of,
    up,
)

BOXFILE = CONTAINERFILE.parent / "Boxfile"
BOX_LABEL = "io.relay.box=1"
SETUP_LABEL = "io.relay.setup=1"  # a box a provider's program is installed in (Boxes.setup)
SOCKET_IN_BOX = "/run/relay/exec.sock"


def box_image() -> str:
    """The box image, named after what it's built from: changed, it's built anew."""
    digest = hashlib.sha256()
    for part in (BOXFILE, BOXFILE.parent / "box" / "relay-shell", BOXFILE.parent / "box" / "relay-reap"):
        digest.update(part.read_bytes())
    return f"localhost/relay-box:{digest.hexdigest()[:12]}"


def home_env(home: Path) -> dict[str, str]:
    """A box whose provider has a home of its own (Provider.box_home): that's its HOME, npm's global installs go
    there, and what's installed there comes first on PATH."""
    from relay.providers.base import HOME_BINS
    home = Path(home)
    return {"HOME": str(home), "NPM_CONFIG_PREFIX": str(home / ".local"),
            "PATH": ":".join([*(str(home / folder) for folder in HOME_BINS), "/usr/local/sbin", "/usr/local/bin",
                              "/usr/sbin", "/usr/bin", "/sbin", "/bin"])}


# A provider's own home, made: a login shell (bash -l: its /etc/profile sets PATH anew) has what's installed there too
PROFILE = """# Relay: what's installed in this home first on PATH, in a login shell too (relay.boxes.home_env)
export PATH="$HOME/.local/bin:$HOME/bin:$PATH"
export NPM_CONFIG_PREFIX="$HOME/.local"
"""


def made_home(home: Path) -> Path:
    """A provider's own home, there (with its .profile, unless it has one)."""
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    if not (home / ".profile").exists():
        (home / ".profile").write_text(PROFILE)
    return home


def _wanted(mounts: list[tuple[Path, bool]]) -> list[tuple[Path, Path, bool]]:
    """What's mounted where, (real, place, writable), of the paths that exist. A link (or a path through one) is
    mounted where it points, and at its own path too."""
    wanted, seen = [], set()
    for path, writable in mounts:
        path = Path(path)
        if not path.exists():
            continue
        real = path.resolve()
        for place in ([real] if real == path else [real, path]):
            if str(place) not in seen:
                seen.add(str(place))
                wanted.append((real, place, writable))
    return wanted


def _volumes(wanted: list[tuple[Path, Path, bool]]) -> list[str]:
    """podman's -v for them. A link's own path only when what's around it isn't in the box already (then the link
    is there itself)."""
    destinations = [place for _, place, _ in wanted]
    argv = []
    for real, place, writable in wanted:
        if real != place and any(other != place and other in place.parents for other in destinations):
            continue
        argv += ["-v", f"{real}:{place}" + ("" if writable else ":ro")]
    return argv


# What a provider's code may not have mounted in a box where it's tested (relay.repair): the machine's keys and
# credentials, Relay's own data and code. A provider's box in use has the workspace roots anyway; a test box has none.
SECRETS = (".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".netrc", ".git-credentials", ".password-store",
           ".local/share/keyrings")


def refused_mount(path: Path, writable: bool, settings=None) -> str | None:
    """Why a provider's code may not have `path` mounted (writable) in a test box, or None. A program (a file one
    may run) may be mounted read-only from anywhere; a folder writable only inside the home folder or Relay's data
    (a provider's own folder there: opencode-manager's profiles)."""
    from relay.repair import SOURCE
    path, home = Path(path).expanduser().absolute(), Path.home()
    if path == Path("/") or path in home.parents or path == home:
        return "too much: a folder of its own is enough"
    data = config.data_dir()
    guarded = [*(home / s for s in SECRETS), data / "relay.db", data / "settings.json", data / "providers",
               data / "repairs", SOURCE.parent,
               *(Path(r).expanduser() for r in (getattr(settings, "workspace_roots", None) or []))]
    for kept in guarded:
        if path == kept or path in kept.parents or kept in path.parents:
            return f"{kept} is kept out of a provider's tests"
    if writable and home not in path.parents and data not in path.parents:
        return "a writable folder must be in the home folder or Relay's data"
    return None


def box_key(text: str) -> str:
    return re.sub(r"[^a-z0-9_.-]", "-", text.lower())[:60] or "provider"


def command_of(args: list[str]) -> str | None:
    """bash [options] -c [options] command …: with -c, the first word that isn't an option."""
    options = 0
    while options < len(args) and args[options].startswith(("-", "+")) and args[options] not in ("-", "--"):
        options += 1
    if not any(not a.startswith("--") and a.startswith("-") and "c" in a[1:] for a in args[:options]):
        return None
    at = options + (options < len(args) and args[options] == "--")
    return args[at] if at < len(args) else None


@dataclass
class BoxLaunch:
    """A run's provider program, started in its box."""
    name: str
    tag: str = field(default_factory=lambda: uuid.uuid4().hex)

    def wrap(self, argv: list[str], cwd: Path | str, env: dict[str, str]) -> list[str]:
        """argv to run `argv` in the box, in `cwd`, with `env` (and this run's tag, to end it)."""
        flags = []
        for key, value in {**env, "RELAY_RUN": self.tag}.items():
            flags += ["-e", f"{key}={value}"]
        return ["podman", "exec", "-i", "-w", str(cwd), *flags, self.name, *argv]

    def end(self) -> None:
        """End what the run started in the box (stopping podman exec leaves it running)."""
        subprocess.run(["podman", "exec", self.name, "/usr/lib/relay/relay-reap", self.tag],
                       capture_output=True, timeout=60)


class Boxes:
    def __init__(self, containers: Containers, settings=None) -> None:
        self.containers = containers
        self.settings = settings
        self.state = containers.state / "boxes"
        self.image = box_image()
        self.lock = threading.Lock()
        self.gateways: dict[str, ExecGateway] = {}
        self.checked: str | None = None

    def check(self) -> None:
        """ContainerError when podman doesn't work, or the box image can't be built."""
        self.containers.check()
        with self.lock:
            if self.checked is None:
                problem = build_image(self.image, BOXFILE, "the providers' box image")
                self.checked = f"Providers run in containers, and {problem}" if problem else "ok"
        if self.checked != "ok":
            raise ContainerError(self.checked)

    def drop_stale(self) -> list[str]:
        """Boxes that see a removed folder where one of Relay's is now: a box keeps running across
        Relay's restarts, and a folder of Relay's that was removed and made again (the subagents'
        copies, before 1.0.0rc1; the Governors' notebooks) is an empty, removed one in it. Those
        boxes go (each is made anew when a run needs it); the names of the ones that went."""
        from relay import forks
        watched = [forks.folders(), config.data_dir() / "agentos", config.data_dir() / "production",
                   config.data_dir() / "repairs"]
        for folder in watched:
            folder.mkdir(parents=True, exist_ok=True)
        inodes = [str(folder.stat().st_ino) for folder in watched]
        try:
            listed = subprocess.run(["podman", "ps", "--filter", f"label={BOX_LABEL}", "--format",
                                     f'{{{{.Names}}}}|{{{{index .Labels "{INSTANCE}"}}}}'], capture_output=True,
                                    text=True,
                                    timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return []
        dropped = []
        for line in listed.stdout.splitlines():
            name, _, owner = line.partition("|")
            if not mine(owner):
                continue  # another Relay's (a test run's): its own folders, not this one's
            seen = subprocess.run(["podman", "exec", name, "stat", "-c", "%i", *map(str, watched)],
                                  capture_output=True, text=True, timeout=60)
            if seen.returncode == 0 and seen.stdout.split() != inodes:
                subprocess.run(["podman", "rm", "-f", "-t", "5", name], capture_output=True, timeout=60)
                dropped.append(name)
        return dropped

    def built(self) -> bool:
        try:
            return subprocess.run(["podman", "image", "exists", self.image], capture_output=True,
                                  timeout=60).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def name(self, key: str) -> str:
        mark = instance()  # (another Relay on this podman has boxes of its own)
        return f"relay-box-{box_key(key)}" + (f"-{mark}" if mark else "")

    def ensure(self, key: str, mounts: list[tuple[Path, bool]], folder: Path | None = None,
               busy: bool = False, image: str | None = None, home: Path | None = None) -> str:
        """The provider's box, running; `folder` (a run's project) is in it. A box made otherwise is
        made anew, unless it's `busy` (another run of the provider is going on): then as it is.
        `home`: the provider's own (Provider.box_home)."""
        self.check()
        key = box_key(key)
        name = self.name(key)
        with self.lock:
            self._gateway(key)
            extras = self._extras(key, folder)
            argv = self._argv(key, mounts, extras, image, home)
            spec = spec_of(argv)
            state, found = self.containers._state(name)
            if state == "running" and found != spec and busy:
                return name  # made otherwise, but in use: anew next time
            up(name, spec, argv, "the provider's box", (state, found), labels=(BOX_LABEL,))
            return name

    def test(self, command: list[str], mounts: list[tuple[Path, bool]], env: dict[str, str], cwd: Path,
             image: str | None = None, timeout: float = 300) -> subprocess.CompletedProcess:
        """`command` in a box of its own made for it, ended with it: a provider's code tested (relay.repair), with
        only `mounts` of the machine's and the network. Its shell is a real one: there's no Relay here to hand
        commands to, so a program's commands run in the box itself. `image`: the provider's (Provider.box_image)."""
        self.check()
        argv = ["podman", "run", "--rm", "--init", "--name", f"relay-test-{uuid.uuid4().hex[:12]}", "--network=host",
                "--label", f"{INSTANCE}={instance()}", "--timeout", str(int(timeout)), *_volumes(_wanted(mounts))]
        for key, value in env.items():
            argv += ["-e", f"{key}={value}"]
        if image == "workbox":
            self.containers.check()  # (built if it isn't there; its shells are its own)
            argv += ["-w", str(cwd), self.containers.image]
        else:  # (Relay's box image: its shells hand commands to Relay, so a real one goes over them here)
            argv += ["-v", f"{self._here_shell()}:/usr/lib/relay/relay-shell:ro", "-w", str(cwd), self.image]
        return subprocess.run([*argv, *command], capture_output=True, text=True, timeout=timeout + 30,
                              stdin=subprocess.DEVNULL, check=False)

    def _here_shell(self) -> Path:
        """What goes over Relay's shell in a box where shells are real ones (a test box, a setup box)."""
        here = self.state / "here-shell"
        if not here.is_file():
            here.parent.mkdir(parents=True, exist_ok=True)
            here.write_text('#!/usr/lib/relay/bash.real\nexec /usr/lib/relay/bash.real "$@"\n')
            here.chmod(0o755)
        return here

    def remove(self, key: str) -> None:
        """A provider's box that nothing will use (one not added after all: relay.creations drop_unused_box)."""
        with self.lock:
            gateway = self.gateways.pop(box_key(key), None)
        if gateway is not None:
            gateway.stop()
        subprocess.run(["podman", "rm", "-f", "-t", "5", self.name(key)], capture_output=True, timeout=60)

    def setup_name(self, key: str) -> str:
        mark = instance()
        return f"relay-setup-{box_key(key)}" + (f"-{mark}" if mark else "")

    def setup(self, key: str, home: Path) -> str:
        """A box to install a provider's program in and log in, in a shell (a provider added with an agent:
        relay.creations): Relay's box image with its shells real ones (the user installs: nothing goes to a
        WorkBox), with the provider's home (home_env) as its own box will have it. Running until it's removed."""
        self.check()
        name = self.setup_name(key)
        argv = ["--init", "--network=host", *_volumes(_wanted([(made_home(home), True)]))]
        for variable, value in home_env(home).items():
            argv += ["-e", f"{variable}={value}"]
        argv += ["-v", f"{self._here_shell()}:/usr/lib/relay/relay-shell:ro", "-w", str(home), self.image,
                 "sleep", "infinity"]
        with self.lock:
            up(name, spec_of(argv), argv, "the provider's setup box", self.containers._state(name),
               labels=(SETUP_LABEL,))
        return name

    def remove_setup(self, key: str) -> None:
        subprocess.run(["podman", "rm", "-f", "-t", "5", self.setup_name(key)], capture_output=True, timeout=60)

    def launch(self, key: str, mounts: list[tuple[Path, bool]], folder: Path, busy: bool = False,
               detached: Callable[[str], bool] | None = None, image: str | None = None,
               home: Path | None = None) -> BoxLaunch:
        """`detached`: the provider's word on which of its commands outlive the run (Provider.detached);
        `image`: "workbox" for a box of the WorkBox image (Provider.box_image); `home`: its own
        (Provider.box_home)."""
        launch = BoxLaunch(self.ensure(key, mounts, folder, busy, image, home))
        if detached is not None:
            self.gateways[box_key(key)].detached = detached
        return launch

    def _argv(self, key: str, mounts: list[tuple[Path, bool]], extras: list[Path],
              image: str | None = None, home: Path | None = None) -> list[str]:
        exchange = self.containers.exchange
        exchange.mkdir(parents=True, exist_ok=True)
        sockets = self.state / key
        argv = ["--init", "--network=host"]
        if home is not None:  # (a home of its own: what the user installed and logged in there)
            mounts = [*mounts, (made_home(home), True)]
            for name, value in home_env(home).items():
                argv += ["-e", f"{name}={value}"]
        else:
            argv += ["-e", f"HOME={Path.home()}"]
        argv += ["-e", "RELAY_BOX=1", "-e", f"RELAY_EXCHANGE={exchange}", "-e", f"RELAY_EXEC_SOCKET={SOCKET_IN_BOX}",
                 "-v", f"{sockets}:{Path(SOCKET_IN_BOX).parent}"]
        wanted = _wanted([*self._common(), *[(p, True) for p in extras], *mounts])
        argv += _volumes(wanted)
        # The user's private scripts (relay.scripts): an empty, read-only folder in their place, wherever a mount
        # has them (a workspace root does). Once they're there: podman would make the folder.
        masked = set()
        for hidden in filter(Path.is_dir, self.containers.hidden):
            for real, place, _ in wanted:
                if hidden == real or real in hidden.parents:
                    where = place / hidden.relative_to(real)
                    if str(where) not in masked:
                        masked.add(str(where))
                        self.containers.empty.mkdir(parents=True, exist_ok=True)
                        argv += ["-v", f"{self.containers.empty}:{where}:ro"]
        if image == "workbox":
            # The WorkBox image (Chromium, Node, Playwright), for a provider that needs a browser; its
            # shells are its own (it runs no agent's commands), Relay's reaper comes along.
            self.containers.check()  # (built if it isn't there)
            argv += ["-v", f"{CONTAINERFILE.parent / 'box'}:/usr/lib/relay:ro"]
            return [*argv, self.containers.image, "sleep", "infinity"]
        return [*argv, self.image, "sleep", "infinity"]

    def _common(self) -> list[tuple[Path, bool]]:
        """What every box has: the projects, Relay's chat and task folders (subagents' copies of
        them too, repairs' folders), and what Relay hands agents (its tools and skills, attachments, handed-over
        transcripts), and the scratch folders."""
        from opencode_manager import paths
        from relay import forks, skills
        data = config.data_dir()
        forks.folders().mkdir(parents=True, exist_ok=True)
        (data / "agentos").mkdir(parents=True, exist_ok=True)  # the Governors' notebooks (relay.agentos)
        (data / "production").mkdir(parents=True, exist_ok=True)  # services' versions and clones (relay.production)
        (data / "repairs").mkdir(parents=True, exist_ok=True)  # repairs' and new kinds' folders (relay.repair)
        (paths.host_cache_dir() / "relay-skills").mkdir(parents=True, exist_ok=True)  # (Claude Code's, relay.skills)
        roots = [Path(r).expanduser() for r in (getattr(self.settings, "workspace_roots", None) or [])]
        chats = self.settings.chat_directory() if self.settings is not None else data / "chats"
        # (there from the start: a folder that came later would make a box anew, and end a shell open in it)
        for folder in (chats, config.worktrees_dir(), data / "uploads", data / "handover", skills.user_dir()):
            folder.mkdir(parents=True, exist_ok=True)
        found = [*[(r, True) for r in roots], (chats, True), (config.worktrees_dir(), True), (forks.folders(), True),
                 (data / "agentos", True), (data / "production", True),
                 # (there from the start: a busy box isn't made anew for a new one, and its agent couldn't start)
                 (data / "repairs", True),
                 # Of the machine's cache only the skills as Claude Code loads them (not the
                 # conversations' tools scripts, whose tokens no box needs: their path is passed on).
                 (data / "uploads", False), (data / "handover", False),
                 (paths.host_cache_dir() / "relay-skills", False),
                 (skills.BUILTIN, False), (skills.user_dir(), False), (self.containers.exchange, True)]
        SCRATCH.mkdir(exist_ok=True)
        return [*found, (SCRATCH, True)]

    def _extras(self, key: str, folder: Path | None) -> list[Path]:
        """Projects outside the workspace roots that runs of this provider worked in (kept, so
        the box isn't made anew for each)."""
        file = self.state / f"{key}.folders.json"
        try:
            known = [Path(p) for p in json.loads(file.read_text())]
        except (OSError, ValueError):
            known = []
        if folder is not None:
            folder = Path(folder).resolve()
            covered = [p for p, writable in self._common() if writable] + known
            if not any(folder == p.resolve() or p.resolve() in folder.parents for p in covered if p.exists()):
                known.append(folder)
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text(json.dumps([str(p) for p in known]))
        return [p for p in known if p.exists()]

    def projects(self, key: str) -> list[Path]:
        """Where this box's commands may run, so where a WorkBox may be made for it: what it has writable of the
        workspace's and Relay's, and the folders its runs worked in. Not what's its own (a login), nor what it may only
        read (other conversations' attachments)."""
        return [path for path, writable in self._common() if writable] + self._extras(key, None)

    def _gateway(self, key: str) -> None:
        if key not in self.gateways:
            gateway = ExecGateway(self.state / key / "exec.sock", self.containers, self.state / f"{key}.commands.log",
                                  lambda: self.projects(key))
            gateway.start()
            self.gateways[key] = gateway

    def stop(self) -> None:
        for gateway in self.gateways.values():
            gateway.stop()


# What a command still running when its shell goes (a stop, the run's end) leaves behind: all of it
# (its RELAY_EXEC tag), but for what detached itself into a session of its own (`setsid -f`, a server
# for the user). The shell podman exec started leads a session too, but has no parent (ppid 0).
END = (r"for p in /proc/[0-9]*; do grep -qa RELAY_EXEC={tag} $p/environ 2>/dev/null || continue; "
       r"set -- $(sed 's/.*) //' $p/stat 2>/dev/null); echo ${{p#/proc/}} $2 $4; done | "
       r"awk '{{pid[NR]=$1; sid[NR]=$3; if ($1 == $3 && $2 != 0) kept[$1]=1}} "
       r"END {{for (i = 1; i <= NR; i++) if (!(sid[i] in kept)) print pid[i]}}' | xargs -r kill -9")


class ExecGateway:
    """Runs the commands a box's shells hand over (container/box/relay-shell) in WorkBoxes."""

    def __init__(self, path: Path, containers: Containers, log: Path, projects: Callable[[], list[Path]]) -> None:
        self.path = path
        self.containers = containers
        self.log = log
        self.projects = projects  # where its box's commands may run (Boxes.projects)
        self.server: socketserver.ThreadingUnixStreamServer | None = None
        self.detached: Callable[[str], bool] = lambda command: False

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        gateway = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                gateway.handle(self.request)

        self.server = socketserver.ThreadingUnixStreamServer(str(self.path), Handler)
        self.server.daemon_threads = True
        os.chmod(self.path, 0o600)
        threading.Thread(target=self.server.serve_forever, name="exec-gateway", daemon=True).start()

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()

    def handle(self, connection: socket.socket) -> None:
        reader = connection.makefile("rb")
        writing = threading.Lock()

        def send(kind: bytes, data: bytes = b"") -> None:
            with writing:
                try:
                    connection.sendall(kind + struct.pack(">I", len(data)) + data)
                except OSError:
                    pass

        try:
            header = json.loads(reader.readline() or b"{}")
            args = [str(a) for a in header.get("argv") or []]
            name = "sh" if header.get("name") == "sh" else "bash"
            cwd = str(header.get("cwd") or "/")
        except (ValueError, TypeError):
            return
        command = command_of(args)
        blocked = guard.check(command) if command else None
        if blocked:
            self._log(cwd, command, "refused")
            send(b"r", f"Relay refused this command: {blocked}\n".encode())
            send(b"x", b"126")
            return
        try:
            container, where, base = self.containers.place(header.get("folder"), cwd, self.projects())
        except ContainerError as exc:
            send(b"r", f"Relay can't run this: {exc}\n".encode())
            send(b"x", b"125")
            return
        tag = uuid.uuid4().hex
        tools = header.get("tools") if isinstance(header.get("tools"), str) else None
        # One the provider detached on purpose (a server): in a session of its own, which END spares
        # when its shell goes; `setsid -w` waits for it, so the shell still sees it run and end. Its
        # output goes straight to the files the shell's went to (its log, in the scratch folder every
        # box has), not through the shell, which is gone when the run ends.
        keep, logs = [], []
        if command and self.detached(command):
            keep = ["setsid", "-w"]
            files = header.get("files") if isinstance(header.get("files"), dict) else {}
            out, err = (str(files.get(k) or "") for k in ("out", "err"))
            if all(p.startswith(f"{SCRATCH}/") for p in (out, err)):
                logs = ["-e", f"RELAY_OUT={out}", "-e", f"RELAY_ERR={err}"]
                keep += ["sh", "-c", 'exec "$@" >>"$RELAY_OUT" 2>>"$RELAY_ERR"', "sh"]
        argv = ["podman", "exec", "-i", "-w", where, "-e", f"HOME={HOME}", "-e", f"RELAY_EXEC={tag}", *logs,
                *self.containers.agent_env(tools, base), container, *keep, name, *args]
        self._log(where, command or f"({name} {' '.join(args)})".strip(), container)
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        finished = threading.Event()

        def end_it() -> None:
            if finished.is_set():
                return
            subprocess.run(["podman", "exec", container, "sh", "-c", END.format(tag=tag)], capture_output=True,
                           timeout=60)
            process.kill()

        def pump(stream, kind: bytes) -> None:
            for chunk in iter(lambda: stream.read1(65536), b""):
                send(kind, chunk)

        def listen() -> None:
            try:
                while True:
                    head = reader.read(5)
                    if len(head) < 5:
                        end_it()  # its shell is gone (stopped, or the provider ended)
                        return
                    kind, size = head[:1], struct.unpack(">I", head[1:])[0]
                    data = reader.read(size)
                    if kind == b"i":
                        process.stdin.write(data)
                        process.stdin.flush()
                    elif kind == b"e":
                        process.stdin.close()
                    elif kind == b"k":
                        end_it()
                        return
            except (OSError, ValueError):
                end_it()

        pumps = [threading.Thread(target=pump, args=(process.stdout, b"o"), daemon=True),
                 threading.Thread(target=pump, args=(process.stderr, b"r"), daemon=True)]
        for thread in pumps:
            thread.start()
        threading.Thread(target=listen, daemon=True).start()
        code = process.wait()
        for thread in pumps:
            thread.join(10)
        finished.set()
        self.containers.hand_back(container, command)
        send(b"x", str(code if code >= 0 else 128 - code).encode())

    def _log(self, where: str, what: str, outcome: str) -> None:
        try:
            with open(self.log, "a") as log:
                log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{outcome}\t{where}\t{what[:500]!r}\n")
        except OSError:
            pass

