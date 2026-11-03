"""Agents' environment: a podman container per project folder.

Relay, the providers and the agents' programs (Claude Code, opencode, Copilot)
run on the host; the commands an agent runs go into a container. Relay gives
the agent a shell (`shell()`, a small script set as its $SHELL) that hands
every command to `podman exec` in the folder's container, in the same folder.
The folder is mounted at the same path, or the git repository it's in, with
the repository's .git (a worktree's history lives there); so are opencode's
scratch folder and, read-only, Relay's tools, skills and attachments. The
agent is root in it: with rootless podman that's the user outside, so what it
writes is theirs, and it can install what it needs. An agent that wrecks its
environment wrecks a container; the project (and its git history) is all it
can touch.

A folder's container is started on first use, kept warm (a command costs
~0.1 s), stopped once unused for a few hours (`stop_idle`, from the
scheduler; started again as it was, with what was installed) and removed
after two weeks stopped. Read-only modes (Plan, Brainstorm) ask the agent not
to change anything; the container doesn't hold it to that.

opencode is one server for many sessions, so it gets one shell for all of
them, which finds the container by the command's folder in a table Relay
keeps (`route()`); a folder Relay doesn't know (the user's own use of that
opencode) runs on the host as before.

Without a working podman no agent runs: Relay says why and how to fix it
(`check()`).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from opencode_manager import paths
from relay import guard, skills
from relay.sdk import SOURCE as SDK_SOURCE

# Relay's own image, built on first use from container/Containerfile: Debian Trixie with Python
# 3.13 (python:3.13-trixie: git, a compiler; amd64 and arm64), Node.js and npm, ripgrep, jq.
CONTAINERFILE = Path(__file__).parent / "container" / "Containerfile"
# Containers in a box (Containerfile: podman): its /proc unmasked, so podman there can mount a new one for
# its own containers (no more capabilities than the box: what's read-only stays so), their images in a
# volume of the box's
NESTED = ["--security-opt", "unmask=ALL"]
NESTED_STORE = "/var/lib/containers"
BIN = CONTAINERFILE.parent / "bin"  # `relay`, first on agents' commands' PATH (mounted like the rest of it)
DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
BUILD_TIMEOUT = 1800  # seconds: a few minutes usually; a slow disk or network takes longer
LABEL = "io.relay.managed=1"
SPEC = "io.relay.spec"  # how it was made (mounts, image): one made otherwise is made anew
HOME = "/home/relay"  # the agent's home inside, on a volume of its own per folder
# A scratch folder at the same path in every box and WorkBox: what a provider's program writes
# for its commands to see (opencode's, Copilot's detached commands' pid and exit files).
SCRATCH = Path("/tmp/opencode")
# Where podman finds its containers and settings.
PODMAN_ENV = ("HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR", "XDG_STATE_HOME",
              "CONTAINERS_CONF", "CONTAINERS_STORAGE_CONF", "CONTAINER_HOST")
TIMEOUT = 600  # seconds: starting one may pull its image first
REMOVE_DAYS = 14  # a stopped container unused this long is removed, with its home volume
# Which Relay made a box: podman may be shared with another (a test run, the UI walkthrough, a
# second install), whose boxes this one's tidying must leave alone.
INSTANCE = "io.relay.instance"


def instance() -> str:
    """This Relay's mark on its boxes: its data folder's, or "" for the usual one
    (~/.local/share/relay), whose boxes had no mark before."""
    home = paths.relay_home().expanduser().resolve()
    usual = (Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "relay").resolve()
    return "" if home == usual else hashlib.sha256(str(home).encode()).hexdigest()[:8]


def mine(found: str) -> bool:
    """Whether a box with this INSTANCE label ("" when it has none) is this Relay's."""
    return found.strip() in ("", "<no value>") if not instance() else found.strip() == instance()


class ContainerError(Exception):
    """Agents can't run: what's wrong, and how to fix it."""


SHELL = """#!@PYTHON@
# Relay's shell for agents: each command runs in a container (relay/containers.py).
import json, os, re, stat, sys
sys.path.insert(0, @SRC@)
from relay import guard

CONTAINER = @CONTAINER@  # one container (a process of one run), or None: by folder (a shared server)
ROUTES = @ROUTES@
cwd = os.getcwd()


def route():
    try:
        table = json.load(open(ROUTES))
    except (OSError, ValueError):
        return None, None
    found = max((d for d in table if cwd == d or cwd.startswith(d.rstrip("/") + "/")), key=len, default=None)
    return (table[found], found) if found else (None, None)


name, folder = (CONTAINER, @FOLDER@) if CONTAINER else route()
if name is None:
    os.execv("/bin/bash", ["bash", *sys.argv[1:]])  # not Relay's: as it always was
args = sys.argv[1:]
# bash [options] -c [options] command [name [args]]: with -c, the first word that isn't an option.
options = 0
while options < len(args) and args[options].startswith(("-", "+")) and args[options] not in ("-", "--"):
    options += 1
dash_c = any(not a.startswith("--") and a.startswith("-") and "c" in a[1:] for a in args[:options])
at = options + (options < len(args) and args[options] == "--") if dash_c else None
command = args[at] if at is not None and at < len(args) else None
tag = os.urandom(8).hex()
# Claude Code keeps its Bash tool's folder in a file on the machine (pwd -P >| /tmp/claude-…-cwd):
# written in a folder the container shares, and copied there afterwards.
CWD_FILE = re.compile(r"(pwd -P >\\| )(/tmp/claude-[A-Za-z0-9]+-cwd)$")
wanted = CWD_FILE.search(command) if command else None
shared = os.path.join(@EXCHANGE@, tag + "-cwd")
if wanted:
    args[at] = command[:wanted.start(2)] + shared
blocked = guard.check(command) if command else None
if blocked:
    print(f"Relay refused this command: {blocked}", file=sys.stderr)
    sys.exit(126)
where = cwd if cwd == folder or cwd.startswith(folder.rstrip("/") + "/") else folder
# podman as Relay has it: an agent's program may have its own XDG folders (opencode's profiles
# do), where podman would look for other containers.
for key, value in @PODMAN_ENV@.items():
    if value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = value
try:
    os.utime(os.path.join(@USED@, name))
except OSError:
    try:
        open(os.path.join(@USED@, name), "a").close()
    except OSError:
        pass
tools = []
if os.environ.get("RELAY_TOOLS"):  # the conversation's tools script: its MCP address, not the file
    try:
        url = open(@URL_FILE@).read().strip()
    except OSError:
        url = ""
    if url:
        tools = ["-e", f"RELAY_MCP_URL={url}/mcp/" + os.path.basename(os.path.dirname(os.environ["RELAY_TOOLS"]))]
# A script's session (relay.scripts: RELAY_TOKEN), passed on by its name: podman takes its value from here, so
# it's in no command line
passed = [arg for key in ("RELAY_TOKEN",) if os.environ.get(key) for arg in ("-e", key)]
argv = ["podman", "exec", "-i", "-w", where, "-e", "HOME=" + @HOME@, "-e", "RELAY_EXEC=" + tag, *@AGENT_ENV@, *tools,
        *passed, name, "bash", *args]
# podman exec stopped (a time-out, the agent stopped) leaves the command running in the
# container: a watcher of its own, out of this process group, ends what it started then.
done_read, done_write = os.pipe()
if os.fork() == 0:
    os.close(done_write)
    os.setsid()
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)
    finished = os.read(done_read, 1)  # b"" when this shell is gone without saying so
    if not finished:
        kill = f"for p in /proc/[0-9]*; do grep -qa RELAY_EXEC={tag} $p/environ 2>/dev/null && \
kill -9 ${{p#/proc/}}; done"
        os.execvp("podman", ["podman", "exec", name, "sh", "-c", kill])
    os._exit(0)
os.close(done_read)
import signal
for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(number, lambda *_: os._exit(143))
import subprocess
code = subprocess.call(argv)
os.write(done_write, b"1")
written = os.path.join(@BOX_EXCHANGE@, name, tag + "-cwd")  # (where the box's own exchange folder is)
written = written if os.path.lexists(written) else shared
if wanted and os.path.lexists(written):
    # (the box wrote it, and an agent is root there: a plain file only, not what a link points to on this machine)
    try:
        found = os.open(written, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            text = os.read(found, 65536) if stat.S_ISREG(os.fstat(found).st_mode) else None
        finally:
            os.close(found)
        if text is not None:
            with open(wanted.group(2), "wb") as there:
                there.write(text)
    except OSError:
        pass
    try:
        os.unlink(written)
    except OSError:
        pass
sys.exit(code if code >= 0 else 128 - code)
"""


class Containers:
    def __init__(self, image: str | None = None, state: Path | None = None) -> None:
        self.build = not image  # Relay's own image, built when it isn't there yet
        self.image = image or built_image()
        self.state = state or paths.host_cache_dir() / "relay-containers"
        self.lock = threading.Lock()
        self.used: dict[str, float] = {}  # container -> last time a run asked for it
        self.terminals: dict[str, int] = {}  # open interactive shells keep their container warm
        self.checked: str | None = None  # "ok", or why not
        self.checking = threading.Lock()
        # The shells' own files (Claude Code's cwd file): the ProviderBoxes see this folder; each
        # WorkBox has a folder of its own at the same path (box_exchange/<name>), so no box sees
        # another's, and Relay hands what a command wrote back (hand_back).
        self.exchange = self.state / "exchange"
        self.box_exchange = self.state / "exchange-boxes"
        # What every WorkBox may read of Relay's: where Relay is, and the generic tools script
        # (container/bin/relay); never a conversation's token, which only its commands' env has.
        self.public = self.state / "public"
        # The user's scripts (relay.scripts): private/ out of every box's sight, an empty read-only folder in its
        # place (but in its own WorkBox, where they run); public/ in every WorkBox, read-only
        self.hidden: list[Path] = []
        self.scripts_public: Path | None = None
        self.empty = self.state / "empty"
        self.url_file = self.public / "relay-url"
        self.url: str | None = None
        # A VPN for some folders' WorkBoxes (relay.vpn; set by Relay): the network a folder's WorkBox
        # joins (["--network=container:<id>"], and when that container last started), or None for the
        # machine's; whether a folder's goes through it; Relay's address from in there.
        self.network: Callable[[Path], tuple[list[str], float | None] | None] | None = None
        self.via_vpn: Callable[[Path], bool] = lambda directory: False
        self.vpn_url: str | None = None
        # Signs a WorkBox's folder (RELAY_BOX_KEY), so /tools-for tells a box only its own agents'
        # tools; in memory only: a restart makes a new one, and commands get it anew.
        self.secret = secrets.token_bytes(32)
        self.image_path: str | None = None
        # Folders of the machine a folder's container gets too, at their own path: those a
        # subagent's copy of a folder links to (installed software; relay.forks).
        self.also: dict[str, list[Path]] = {}

    # Whether agents can run here

    def check(self) -> None:
        """ContainerError with what to do when podman doesn't work (asked once per Relay); builds
        Relay's image the first time."""
        with self.checking:
            if self.checked is None:
                self.checked = self._check()
        if self.checked != "ok":
            raise ContainerError(self.checked)

    def _check(self) -> str:
        if shutil.which("podman") is None:
            return ("Agents work in containers, and podman isn't installed. Install it (Raspberry Pi OS, "
                    "Debian: sudo apt install podman; Fedora: sudo dnf install podman) and restart Relay.")
        if self.build:
            problem = self._build()
            if problem:
                return problem
        try:
            result = subprocess.run(["podman", "run", "--rm", "--network=host", self.image, "true"],
                                    capture_output=True, text=True, timeout=TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"Agents work in containers, and podman didn't answer: {exc}"
        if result.returncode != 0:
            detail = [line for line in result.stderr.splitlines() if "level=warning" not in line][-3:]
            return ("Agents work in containers, and podman can't run one here: "
                    + " ".join(detail).strip() + " (see docs/GUIDE.md, Containers).")
        return "ok"

    def built(self) -> bool:
        """Whether the image is there (Relay's own is built on first use)."""
        if not self.build:
            return True
        try:
            return subprocess.run(["podman", "image", "exists", self.image], capture_output=True,
                                  timeout=60).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def _build(self) -> str | None:
        """Build Relay's image if it isn't there (once; a few minutes); why not, if it failed."""
        problem = build_image(self.image, CONTAINERFILE, "the agents' container image")
        return f"Agents work in containers, and {problem}" if problem else None

    # Containers

    def name(self, directory: Path) -> str:
        return f"relay-{_key(directory)}"

    def ensure(self, directory: Path) -> str:
        """The folder's container, started if it isn't; its name."""
        self.check()
        directory = Path(directory).resolve()
        name = self.name(directory)
        # Its network: the machine's, or a VPN's (relay.vpn; started first, which may take a while: not
        # while holding up other folders' boxes)
        routed = self.network(directory) if self.network is not None else None
        with self.lock:
            self._mark_used(name)
            self.exchange.mkdir(parents=True, exist_ok=True)
            self.public.mkdir(parents=True, exist_ok=True)
            (self.state / "used").mkdir(parents=True, exist_ok=True)
            project = _project(directory)
            mounts = [f"{path}:{path}" + ("" if writable else ":ro") for path, writable in project]
            own = self.box_exchange / name
            own.mkdir(parents=True, exist_ok=True)
            mounts.append(f"{own}:{self.exchange}")
            for extra in [*_extras(), self.public, *([self.scripts_public] if self.scripts_public else [])]:
                if extra.is_dir() and not any(_overlaps(extra, path) for path, _ in project):
                    mounts.append(f"{extra}:{extra}:ro")
            also = [a for a in self.also.get(str(directory), []) if a.is_dir()
                    and not any(_overlaps(a, path) for path, _ in project)]
            mounts += [f"{a}:{a}" for a in also]
            for shared in _shared():
                mounts.append(f"{shared}:{shared}")
            mounts += self._masks(directory, [path for path, _ in project] + also)
            home = f"relay-home-{_key(directory)}"
            # Root in the container (rootless podman: that's the user outside it, so what it writes is
            # theirs), so the agent can install what it needs (apt-get); kept while it's stopped. The machine's
            # time zone, so the times in it are the user's (`date`, a script's output).
            argv = ["--init", *(routed[0] if routed else ["--network=host"]), "--tz=local", "-e", f"HOME={HOME}",
                    "-v", f"{home}:{HOME}", *NESTED, "-v", f"relay-containers-{_key(directory)}:{NESTED_STORE}",
                    "-w", str(directory)]
            for mount in mounts:
                argv += ["-v", mount]
            argv += [self.image, "sleep", "infinity"]
            # Stopped while unused: started, with what the agent installed in it
            if not up(name, spec_of(argv), argv, "the agent's container", self._state(name)):
                # In the VPN's network: one that started before it last did lost its network with it
                if routed and routed[1] and (started_at(name) or 0) < routed[1]:
                    subprocess.run(["podman", "restart", "-t", "1", name], capture_output=True, timeout=TIMEOUT)
                return name
            # apt downloads as user _apt (65534), which a small subuid range doesn't map (podman in a
            # container): as root, like everything in here.
            subprocess.run(["podman", "exec", name, "sh", "-c", ("[ ! -d /etc/apt/apt.conf.d ] || echo "
                            "'APT::Sandbox::User \"root\";' > /etc/apt/apt.conf.d/99relay")],
                           capture_output=True, timeout=60)
            return name

    def scripts_folders(self, private: Path, public: Path) -> None:
        """The user's scripts (relay.scripts): private/ out of every box's sight, public/ in every WorkBox
        (read-only). A box made otherwise is made anew at its next use."""
        self.hidden = [Path(private).resolve()]
        self.scripts_public = Path(public).resolve()

    def _masks(self, directory: Path, mounted: list[Path]) -> list[str]:
        """An empty, read-only folder in place of each hidden one a mount has in it: not in a box of the hidden
        folder itself (the private scripts' own WorkBox), nor for one that isn't there yet (podman would make it,
        in the user's folder)."""
        found = []
        for hidden in self.hidden:
            if directory == hidden or hidden in directory.parents or not hidden.is_dir():
                continue
            if any(hidden == path or path in hidden.parents for path in map(Path, mounted)):
                self.empty.mkdir(parents=True, exist_ok=True)
                found.append(f"{self.empty}:{hidden}:ro")
        return found

    def _state(self, name: str) -> tuple[str | None, str | None]:
        """(running/exited/…, its spec) of the container of that name; (None, None) if there's none."""
        result = subprocess.run(["podman", "container", "inspect", "--format",
                                 f'{{{{.State.Status}}}} {{{{index .Config.Labels "{SPEC}"}}}}', name],
                                capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return None, None
        state, _, spec = result.stdout.strip().partition(" ")
        return state, spec

    def agent_env(self, tools: str | None = None, directory: Path | None = None) -> list[str]:
        """`podman exec` options for an agent's command: Relay's `relay` command first on its PATH
        (container/bin, which every WorkBox has), where to find Relay, the WorkBox's folder signed
        (for /tools-for), and, when the agent's program says whose tools it has (the script's path),
        their MCP address. Per command, so no container changes; no token is in a file a box sees."""
        # (behind the VPN, 127.0.0.1 is the VPN's: Relay is at another address there)
        vpn = directory is not None and self.vpn_url is not None and self.via_vpn(Path(directory))
        found = ["-e", f"PATH={BIN}:{self._image_path()}",
                 "-e", f"RELAY_URL_FILE={self.public / 'relay-url-vpn' if vpn else self.url_file}",
                 # A login shell (Claude Code's `bash -c -l`) sets PATH anew from /etc/profile; an exported
                 # function, which comes through the environment, stays.
                 "-e", f"BASH_FUNC_relay%%=() {{  {shlex.quote(str(BIN / 'relay'))} \"$@\"; }}"]
        if directory is not None:
            directory = Path(directory).resolve()
            found += ["-e", f"RELAY_BOX={directory}", "-e", f"RELAY_BOX_KEY={self.box_key(directory)}"]
        url = self.mcp_url(tools, self.vpn_url if vpn else None)
        return [*found, "-e", f"RELAY_MCP_URL={url}"] if url else found

    def look(self, directory: Path, command: str, timeout: float = 60) -> tuple[int, str]:
        """A command of AgentOS's Governor in an agent's folder, read-only (relay.agentos workbox.look):
        a box of its own of the WorkBox image, without network, with the folder (its repository too)
        and the WorkBox's home, where its tools and packages are, mounted read-only. (exit code, output.)"""
        self.check()
        directory = Path(directory).resolve()
        name = f"relay-look-{uuid.uuid4().hex[:12]}"
        argv = ["podman", "run", "--rm", "--init", "--name", name, "--network=none", "--label",
                f"{INSTANCE}={instance()}"]
        for path, _ in _project(directory):
            argv += ["-v", f"{path}:{path}:ro"]
        home = f"relay-home-{_key(directory)}"
        if subprocess.run(["podman", "volume", "exists", home], capture_output=True, timeout=30).returncode == 0:
            argv += ["-v", f"{home}:{HOME}:ro"]
        argv += ["-e", f"HOME={HOME}", "-w", str(directory), self.image, "bash", "-lc", command]
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as exc:
            subprocess.run(["podman", "rm", "-f", "-t", "1", name], capture_output=True, timeout=60)
            told = (exc.stdout or b"").decode(errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout or ""
            return 124, f"{told}\n(Stopped after {timeout:g} s.)"
        return done.returncode, (done.stdout + done.stderr).strip()

    def hand_back(self, name: str, command: str | None) -> None:
        """The cwd file a command in WorkBox `name` wrote (`pwd -P >| <exchange>/…-cwd`, as Relay's
        shells rewrite Claude Code's), from the box's own exchange folder to the shared one."""
        found = re.search(r">\| " + re.escape(str(self.exchange)) + r"/([A-Za-z0-9._-]+-cwd)\s*$", command or "")
        if found:
            source = self.box_exchange / name / found.group(1)
            try:
                # (a plain file only: the ProviderBox's shell opens it where the login is, and would follow a link)
                if stat.S_ISREG(source.lstat().st_mode):
                    source.replace(self.exchange / found.group(1))
            except OSError:
                pass

    def box_key(self, directory: Path | str) -> str:
        return hmac.new(self.secret, str(Path(directory).resolve()).encode(), hashlib.sha256).hexdigest()

    def mcp_url(self, tools: str | None, base: str | None = None) -> str | None:
        """The MCP address of a conversation's tools script (tools_dir()/<token>/relay); `base`:
        Relay's address from where it's asked, if not this machine's own."""
        if not tools or not (base or self.url):
            return None
        token = Path(tools).parent.name
        return f"{base or self.url}/mcp/{token}" if token else None

    def set_url(self, url: str) -> None:
        """Where `relay` in a container finds Relay, and the tools script it runs (written at
        Relay's start; the conversation's address comes in RELAY_MCP_URL)."""
        from relay.tools_script import SCRIPT
        self.url = url
        self.public.mkdir(parents=True, exist_ok=True)
        self.url_file.write_text(url + "\n")
        if self.vpn_url:  # (for a WorkBox behind the VPN: agent_env)
            (self.public / "relay-url-vpn").write_text(self.vpn_url + "\n")
        script = self.public / "relay-tools"
        temporary = script.with_suffix(".tmp")
        temporary.write_text(SCRIPT.replace("@PYTHON@", "/usr/bin/env python3").replace("@URL@", ""))
        temporary.chmod(0o755)
        temporary.replace(script)

    def _image_path(self) -> str:
        """The image's own PATH (its tools' folders), asked once."""
        if self.image_path is None:
            found = None
            try:
                listed = subprocess.run(["podman", "image", "inspect", "--format",
                                         "{{range .Config.Env}}{{println .}}{{end}}", self.image],
                                        capture_output=True, text=True, timeout=60)
                found = next((line[5:] for line in listed.stdout.splitlines() if line.startswith("PATH=")), None)
            except (OSError, subprocess.TimeoutExpired):
                pass
            self.image_path = found or DEFAULT_PATH
        return self.image_path

    def exec_argv(self, directory: Path) -> list[str]:
        """How to run a command in the folder's container: argv + ["bash", "-c", command]."""
        name = self.ensure(directory)
        return ["podman", "exec", "-i", "-w", str(Path(directory).resolve()), "-e", f"HOME={HOME}",
                *self.agent_env(directory=directory), name]

    def browser_argv(self, directory: Path) -> list[str]:
        """How to run Relay's browser (container/browser.cjs: Chromium with Playwright) in the
        folder's container, speaking JSON lines on stdin and stdout."""
        return [*self.exec_argv(directory), "node", str(CONTAINERFILE.parent / "browser.cjs")]

    def terminal_argv(self, directory: Path, tag: str) -> list[str]:
        """An interactive shell in the same WorkBox, held until release_terminal()."""
        directory = Path(directory).resolve()
        name = self.name(directory)
        with self.lock:
            self.terminals[name] = self.terminals.get(name, 0) + 1
        try:
            self.ensure(directory)
            guard_file = self.public / "terminal-guard.py"  # (read-only in every WorkBox)
            with self.lock:
                self.public.mkdir(parents=True, exist_ok=True)
                temporary = self.public / ".terminal-guard.tmp"
                temporary.write_bytes(Path(guard.__file__).read_bytes())
                temporary.replace(guard_file)
        except Exception:
            self.release_terminal(name)
            raise
        return ["podman", "exec", "-it", "-w", str(directory), "-e", f"HOME={HOME}",
                *self.agent_env(directory=directory),
                "-e", "TERM=xterm-256color", "-e", f"RELAY_TERMINAL={tag}",
                "-e", f"RELAY_TERMINAL_GUARD={guard_file}", name,
                "bash", "--rcfile", str(CONTAINERFILE.parent / "terminal.bash"), "-i"]

    def close_terminal(self, name: str, tag: str) -> None:
        try:
            end_terminal(name, tag)
        finally:
            self.release_terminal(name)

    def release_terminal(self, name: str) -> None:
        with self.lock:
            count = self.terminals.get(name, 0)
            if count > 1:
                self.terminals[name] = count - 1
            else:
                self.terminals.pop(name, None)
            self._mark_used(name)

    # The agent's shell

    def shell(self, directory: Path) -> Path:
        """A shell (a script for $SHELL) that runs every command in this folder's container."""
        name = self.ensure(directory)
        return self._write_shell(self.state / "shells" / name / "bash", name, Path(directory).resolve())

    def shared_shell(self) -> Path:
        """The one shell for a server that works for many folders (opencode): by `route()`."""
        return self._write_shell(self.state / "shells" / "shared" / "bash", None, None)

    def route(self, directory: Path) -> None:
        """Commands in this folder (by the shared shell) go to its container."""
        name = self.ensure(directory)
        with self.lock:
            table = self._routes()
            table[str(Path(directory).resolve())] = name
            self._save_routes(table)

    def forget_routes(self) -> None:
        """At Relay's start: its containers may be gone (a restart of the machine), and each
        run routes its folder again."""
        with self.lock:
            self._save_routes({})

    def place(self, folder: str | None, cwd: str, within: list[Path]) -> tuple[str, str, Path]:
        """Where a command from a ProviderBox runs (relay.boxes): the container of `folder` (the
        run's), else of the folder Relay routes `cwd` to, else of `cwd` itself; the folder in
        it to run in: `cwd` when it's in there, else the project's; and the project's folder.
        A WorkBox has its folder writable, and both come from the box, where an agent may have got a program of its
        own running: so only in a folder `within` what that box has as projects (Boxes.projects), and never in one
        hidden from every box."""
        if folder:
            base = Path(folder)
        else:
            table = self._routes()
            found = max((d for d in table if cwd == d or cwd.startswith(d.rstrip("/") + "/")), key=len, default=None)
            base = Path(found) if found else Path(cwd)
        base = base.resolve()
        home = Path.home().resolve()
        if (base == Path("/") or base == home or base in home.parents or not _within(base, within)
                or _within(base, self.hidden)):
            raise ContainerError(f"{base} is no project folder: run it in one")
        name = self.ensure(base)
        where = cwd if (cwd == str(base) or cwd.startswith(str(base).rstrip("/") + "/")) else str(base)
        return name, where, base

    def _routes(self) -> dict[str, str]:
        try:
            found = json.loads((self.state / "routes.json").read_text())
        except (OSError, ValueError):
            return {}
        return found if isinstance(found, dict) else {}

    def _save_routes(self, table: dict[str, str]) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        temporary = self.state / "routes.json.tmp"
        temporary.write_text(json.dumps(table))
        temporary.replace(self.state / "routes.json")

    def _write_shell(self, path: Path, name: str | None, folder: Path | None) -> Path:
        text = (SHELL.replace("@PYTHON@", sys.executable)
                .replace("@SRC@", repr(str(Path(guard.__file__).parent.parent)))
                .replace("@CONTAINER@", repr(name)).replace("@FOLDER@", repr(str(folder) if folder else None))
                .replace("@ROUTES@", repr(str(self.state / "routes.json"))).replace("@HOME@", repr(HOME))
                .replace("@EXCHANGE@", repr(str(self.exchange)))
                .replace("@BOX_EXCHANGE@", repr(str(self.box_exchange)))
                .replace("@USED@", repr(str(self.state / "used")))
                .replace("@URL_FILE@", repr(str(self.url_file)))
                .replace("@PODMAN_ENV@", repr({k: os.environ.get(k) for k in PODMAN_ENV}))
                .replace("@AGENT_ENV@", repr(self.agent_env())))
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_text() != text:
            temporary = path.with_suffix(".tmp")
            temporary.write_text(text)
            temporary.chmod(0o755)
            temporary.replace(path)
        return path

    # Tidying

    def provide(self, directory: Path, files: list[Path]) -> None:
        """Files of Relay's (a conversation's attachments) in the folder's WorkBox at their own paths, so its
        commands find them where its program's file tools do: the box mounts none of Relay's data (other
        conversations' attachments are in it too). Copied in, each once."""
        files = [str(f) for f in dict.fromkeys(Path(f) for f in files) if Path(f).is_file()]
        if not files:
            return
        name = self.ensure(directory)
        missing = subprocess.run(["podman", "exec", name, "sh", "-c", 'for f; do [ -e "$f" ] || echo "$f"; done', "sh",
                                  *files], capture_output=True, text=True, timeout=TIMEOUT,
                                 check=False).stdout.splitlines()
        # (only what was asked about: the box answers this, and an agent is root in it, so any path of the machine could
        # come back, to be read here and handed to the box)
        for path in set(missing) & set(files):
            with open(path, "rb") as source:
                subprocess.run(["podman", "exec", "-i", name, "sh", "-c", 'mkdir -p "$(dirname "$1")" && cat >"$1"',
                                "sh", path], stdin=source, capture_output=True, timeout=TIMEOUT, check=False)

    def mount_also(self, directory: Path, folders: list[Path]) -> None:
        """The folder's container gets these folders of the machine too (when it's made)."""
        with self.lock:
            self.also[str(Path(directory).resolve())] = [Path(f).resolve() for f in folders]

    def discard(self, directory: Path) -> None:
        """Remove the folder's container and its home: the folder is gone for good."""
        directory = Path(directory).resolve()
        name = self.name(directory)
        with self.lock:
            self.also.pop(str(directory), None)
            self.used.pop(name, None)
            table = self._routes()
            if table.pop(str(directory), None) is not None:
                self._save_routes(table)
        subprocess.run(["podman", "rm", "-f", "-t", "5", name], capture_output=True, timeout=60)
        subprocess.run(["podman", "volume", "rm", "-f", f"relay-home-{_key(directory)}",
                        f"relay-containers-{_key(directory)}"], capture_output=True, timeout=60)
        shutil.rmtree(self.state / "shells" / name, ignore_errors=True)
        shutil.rmtree(self.box_exchange / name, ignore_errors=True)

    def stop_box(self, name: str) -> None:
        """Stop one WorkBox now (Settings → System), kept with what was installed in it, as an idle one
        is: it starts again when it's needed."""
        with self.lock:
            subprocess.run(["podman", "stop", "-t", "5", name], capture_output=True, timeout=120)
            self._save_routes({d: n for d, n in self._routes().items() if n != name})
            shutil.rmtree(self.state / "shells" / name, ignore_errors=True)

    def stop_idle(self, idle_hours: float, remove_days: float = REMOVE_DAYS) -> int:
        """Stop Relay's containers nothing used in this long (they're kept, with what the agent
        installed in them); remove those stopped for `remove_days`. How many were stopped."""
        with self.lock:
            listed = subprocess.run(["podman", "ps", "-a", "--filter", f"label={LABEL}", "--format",
                                     f'{{{{.Names}}}} {{{{.State}}}} {{{{.ExitedAt}}}}|{{{{index .Labels "'
                                     f'{INSTANCE}"}}}}'],
                                    capture_output=True, text=True, timeout=60)
            rows = []
            for line in listed.stdout.splitlines() if listed.returncode == 0 else []:
                fields, _, owner = line.partition("|")
                if mine(owner):  # (another Relay's boxes are its own to tidy)
                    rows.append(fields.split())
            stale, old = [], []
            for row in rows:
                if len(row) < 2:
                    continue
                name, state = row[0], row[1]
                if name.startswith(("relay-box-", "relay-setup-", "relay-vpn", "relay-search", "relay-fetch")):
                    continue  # providers' boxes (relay.boxes) and the VPN (relay.vpn) keep running: their
                    # services, its WorkBoxes live there; web search and fetch (relay.webbox) keep their own time;
                    # a provider's setup box (a shell the user installs in) goes when its creation ends
                if self.terminals.get(name):
                    continue
                if state == "running" and time.time() - self._last_used(name) > idle_hours * 3600:
                    stale.append(name)
                elif state != "running" and len(row) > 2 and row[2].isdigit() \
                        and time.time() - int(row[2]) > remove_days * 86400:
                    old.append(name)
            if stale:
                subprocess.run(["podman", "stop", "-t", "5", *stale], capture_output=True, timeout=120)
            if old:
                subprocess.run(["podman", "rm", "-f", *old], capture_output=True, timeout=120)
                subprocess.run(["podman", "volume", "rm", "-f",
                                *(f"relay-{kind}-{n.removeprefix('relay-')}" for n in old
                                                                   for kind in ("home", "containers"))],
                               capture_output=True, timeout=120)
            if stale or old:
                gone = set(stale) | set(old)
                self._save_routes({d: n for d, n in self._routes().items() if n not in gone})
                for name in gone:
                    shutil.rmtree(self.state / "shells" / name, ignore_errors=True)
                    shutil.rmtree(self.box_exchange / name, ignore_errors=True)
            return len(stale)

    def _mark_used(self, name: str) -> None:
        """Used now, also on disk: after a restart of Relay a box used a minute ago doesn't look
        unused (stop_idle). Every command reaches it through ensure (the exec gateway's place)."""
        self.used[name] = time.time()
        marker = self.state / "used" / name
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
        except OSError:
            pass

    def _last_used(self, name: str) -> float:
        """When a command last ran in it (or a run, or a shell asked for it), also before a restart."""
        try:
            touched = (self.state / "used" / name).stat().st_mtime
        except OSError:
            touched = 0
        return max(self.used.get(name, 0), touched)


def spec_of(argv: list[str]) -> str:
    """What a box is made of, in short: one made otherwise (an older Relay, another image, the
    folder moved) is made anew."""
    return hashlib.sha256(json.dumps(argv).encode()).hexdigest()[:12]


def up(name: str, spec: str, argv: list[str], what: str, found: tuple[str | None, str | None],
       labels: tuple[str, ...] = (), scoped: bool = False) -> bool:
    """Box `name` running as `argv` makes it: as it is when it's so, started when it's stopped, else
    made anew (with Relay's labels: managed, its spec, this Relay's mark). `found`: its (state,
    spec) now. Whether it was made anew; ContainerError when it can't be. `scoped`: what podman
    leaves running with it (pasta: a network of its own) outlives Relay (_run_scoped)."""
    state, had = found
    if had == spec:
        if state == "running":
            return False
        started = _run_scoped(["podman", "start", name], scoped)
        if started.returncode == 0:
            return False
    remove(name)
    marks = [f"--label={label}" for label in (LABEL, f"{SPEC}={spec}", f"{INSTANCE}={instance()}", *labels)]
    result = _run_scoped(["podman", "run", "-d", "--name", name, *marks, *argv], scoped)
    if result.returncode != 0:
        detail = [line for line in result.stderr.splitlines() if "level=warning" not in line][-2:]
        raise ContainerError(f"Couldn't start {what}: " + " ".join(detail).strip())
    return True


def _run_scoped(argv: list[str], scoped: bool) -> subprocess.CompletedProcess:
    """Relay as a systemd service: a restart ends everything in its cgroup, and pasta (a box's network,
    which podman leaves running) is in it unless it's started in a scope of its own (conmon has one).
    (Not told by INVOCATION_ID: relay serve drops that, for conmon's sake.)"""
    if scoped and _in_a_service() and shutil.which("systemd-run"):
        done = subprocess.run(["systemd-run", "--user", "--scope", "--quiet", "--collect", *argv], capture_output=True,
                              text=True, timeout=TIMEOUT)
        if done.returncode == 0 or not done.stderr.lstrip().startswith("Failed to"):
            return done  # (systemd-run's own "Failed to …": no user manager to ask, so as it is)
    return subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT)


def _in_a_service() -> bool:
    """Whether Relay runs as a systemd service (its cgroup a unit's: relay.service)."""
    try:  # (cgroup v2: one line; v1: one per hierarchy, systemd's among them)
        return any(line.endswith(".service") for line in Path("/proc/self/cgroup").read_text().splitlines())
    except OSError:
        return False


def network_alive(name: str, proc: Path = Path("/proc")) -> bool:
    """Whether a running box's own network (pasta's: --network=pasta) still has its pasta; one an
    older Relay started went with it. True when it isn't one, or that can't be told."""
    found = subprocess.run(["podman", "container", "inspect", "--format",
                            "{{.HostConfig.NetworkMode}}|{{.NetworkSettings.SandboxKey}}", name],
                           capture_output=True, text=True, timeout=60)
    mode, _, sandbox = found.stdout.strip().partition("|")
    if found.returncode != 0 or not mode.startswith("pasta") or not sandbox:
        return True
    wanted = f"--netns\0{sandbox}\0".encode()
    seen = False  # (any pasta at all: none seen may mean this /proc doesn't show them, so it can't be told)
    for process in proc.glob("[0-9]*"):
        try:
            line = (process / "cmdline").read_bytes()
        except OSError:
            continue
        if Path(line.split(b"\0", 1)[0].decode(errors="replace")).name.startswith("pasta"):  # (wherever it is)
            seen = True
            if wanted in line + b"\0":
                return True
    return not seen and not os.access(proc / "1" / "cmdline", os.R_OK)


def started_at(name: str) -> float | None:
    """When a container last started (Unix time), or None."""
    done = subprocess.run(["podman", "container", "inspect", "--format", "{{.State.StartedAt.Unix}}", name],
                          capture_output=True, text=True, timeout=60)
    try:
        return float(done.stdout.strip()) or None
    except ValueError:
        return None


def remove(name: str) -> None:
    """podman rm -f; one whose conmon is gone (killed with an older Relay) may only go the second time."""
    for _ in range(2):
        if subprocess.run(["podman", "rm", "-f", "-t", "5", name], capture_output=True, timeout=60).returncode == 0:
            return


def end_terminal(name: str, tag: str) -> None:
    """End only this shell's processes in box `name`, never another shell or what else runs there."""
    cleanup = ("import os, signal, sys; from pathlib import Path\n"
               "tag = ('RELAY_TERMINAL=' + sys.argv[1]).encode()\n"
               "for p in Path('/proc').glob('[0-9]*'):\n"
               " try:\n"
               "  if tag in (p / 'environ').read_bytes().split(b'\\0'): os.kill(int(p.name), signal.SIGKILL)\n"
               " except (OSError, ValueError): pass\n")
    subprocess.run(["podman", "exec", name, "python3", "-c", cleanup, tag], capture_output=True, timeout=10,
                   check=False)


BUILDING: dict[str, str] = {}  # Relay's images being built now, and what each is: the page says so (/api/state)


def build_image(image: str, file: Path, what: str) -> str | None:
    """Build one of Relay's images (`localhost/<name>:<hash>`) if it isn't there; why not, if
    that failed. Older builds of it go (one a container still uses stays until that's made anew)."""
    try:
        if subprocess.run(["podman", "image", "exists", image], capture_output=True, timeout=60).returncode == 0:
            return None
        print(f"relay: building {what} {image} (once; a few minutes)", flush=True)
        BUILDING[image] = what
        try:
            built = subprocess.run(["podman", "build", "--network=host", "-t", image, "-f", str(file),
                                    str(file.parent)], capture_output=True, text=True, timeout=BUILD_TIMEOUT)
        finally:
            BUILDING.pop(image, None)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"building {what} failed: {exc}"
    if built.returncode != 0:
        detail = [line for line in built.stderr.splitlines() if "level=warning" not in line][-3:]
        return f"building {what} failed: " + " ".join(detail).strip() + " (see docs/GUIDE.md, Containers)."
    repository = image.rsplit(":", 1)[0]
    listed = subprocess.run(["podman", "images", "--filter", f"reference={repository}",
                             "--format", "{{.Repository}}:{{.Tag}}"], capture_output=True, text=True, timeout=60)
    older = [i for i in listed.stdout.split() if i != image]
    if older:
        subprocess.run(["podman", "rmi", *older], capture_output=True, timeout=120)
    return None


def built_image() -> str:
    """Relay's own image, named after its Containerfile and what it copies in: a changed one is built anew."""
    made = hashlib.sha256(CONTAINERFILE.read_bytes())
    for part in sorted((CONTAINERFILE.parent / "image").iterdir()):
        made.update(part.name.encode() + part.read_bytes())
    return f"localhost/relay-agent:{made.hexdigest()[:12]}"


def _key(directory: Path) -> str:
    return hashlib.sha256(str(Path(directory).resolve()).encode()).hexdigest()[:12]


def _project(directory: Path) -> list[tuple[Path, bool]]:
    """What of the machine a folder's commands get, (path, writable), outermost first: the
    folder; when it's inside a git repository (git looks above it), the repository too, but
    only the folder and the .git writable, so other projects in it stay as they are; and the
    repository's common .git, where a worktree keeps its history. Not a repository that is the
    home folder or more (dotfiles): the folder alone then.
    Read from the files here, not asked of git: the agent writes `.git`, and its core.worktree or a made-up `.git` file
    would have the next command's WorkBox get any folder of the machine."""
    home = Path.home().resolve()
    top = next((folder for folder in (directory, *directory.parents) if _has_git(folder)), None)
    if top is None or top == home or top in home.parents:
        return [(directory, True)]
    if top == directory:
        paths = [(top, True)]
    else:
        paths = [(top, False), (directory, True)]
        if (top / ".git").is_dir():
            paths.append((top / ".git", True))
    common = _common_git(top)
    if common is not None and not _overlaps(common, top):
        paths.append((common, True))
    return paths


def _has_git(folder: Path) -> bool:
    """Whether git would take this folder as a repository's: a .git folder that is one, or a .git file; not a link."""
    dot = folder / ".git"
    try:
        if stat.S_ISDIR(dot.lstat().st_mode):
            return (dot / "HEAD").exists()
    except OSError:
        return False
    return (_small_file(dot) or "").startswith("gitdir:")


def _common_git(top: Path) -> Path | None:
    """Where a repository with a .git file (a linked worktree, a submodule) keeps its history, if that's really this
    folder's. The agent writes that file, so the place it names must name this folder back: what only someone who can
    write there could make it do. --separate-git-dir leaves nothing to tell that by: git doesn't work in its WorkBox."""
    dot = top / ".git"
    text = _small_file(dot)
    if text is None:  # (a .git folder: in the folder, and a made-up `commondir` in there leads nowhere from here)
        return None
    try:
        gitdir = (top / text.removeprefix("gitdir:").strip()).resolve()
        where = _small_file(gitdir / "commondir")
        if where is not None:  # (a linked worktree: <common>/worktrees/<name>, whose `gitdir` file names its .git)
            common = (gitdir / where.strip()).resolve()
            back = _small_file(gitdir / "gitdir")
            named = back is not None and (gitdir / back.strip()).resolve() == dot
            return common if named and gitdir.parent == common / "worktrees" else None
        # (a submodule's: its own settings name the folder)
        found = re.search(r"^\s*worktree\s*=\s*(.+?)\s*$", _small_file(gitdir / "config") or "", re.M | re.I)
        return gitdir if found and (gitdir / found.group(1)).resolve() == top else None
    except (OSError, RuntimeError, ValueError):  # (a loop of links, a NUL in the name)
        return None


def _small_file(path: Path) -> str | None:
    """A short plain file a box may have written: never through a link, and a pipe doesn't hang Relay."""
    try:
        found = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except (OSError, ValueError):
        return None
    try:
        return os.read(found, 65536).decode(errors="replace") if stat.S_ISREG(os.fstat(found).st_mode) else None
    except OSError:
        return None
    finally:
        os.close(found)


def _shared() -> list[Path]:
    """Folders of the machine the agent's own tools use, shared with its commands: opencode's
    scratch folder (/tmp/opencode, which it tells the model it may use)."""
    scratch = SCRATCH
    try:
        scratch.mkdir(exist_ok=True)
    except OSError:
        return []
    return [scratch] if scratch.is_dir() and os.access(scratch, os.W_OK) else []


def _extras() -> list[Path]:
    """What an agent's commands may read besides the project: the skills, and Relay's `relay`
    and browser (container/). Not the machine's cache (the conversations' tools scripts),
    attachments or handed-over transcripts: those are other conversations' too. A conversation's
    own attachments are copied into its WorkBox (Containers.provide); its transcript its program's
    file tools read (relay.agentos.read_boundary keeps AgentOS's to theirs). And Relay's Python SDK
    (sdk/python: the scripts' import, and an agent's that writes one)."""
    return [skills.BUILTIN, skills.user_dir(), CONTAINERFILE.parent, SDK_SOURCE.parent]


def _within(path: Path, folders: list[Path]) -> bool:
    return any(path == folder or folder in path.parents for folder in (f.resolve() for f in folders))


def _overlaps(a: Path, b: Path) -> bool:
    a, b = a.resolve(), b.resolve()
    return a == b or a in b.parents or b in a.parents
