"""Scripts: the user's own Python scripts that use Relay's SDK, written and run on Relay's Scripts page.

They're files in one folder (settings.scripts_folder; by default `scripts` in the first workspace root), in
two folders there: private/, the user's alone (every box Relay makes has an empty one in its place:
Containers.hide), and public/, which every agent may read (every WorkBox has it, read-only) and AgentOS's
Governor may run (relay.agentos: script.*; a task can run one). The root holds only those, and
DO_NOT_PUT_FILES_HERE. Agents write scripts in public/: a code task there, with the built-in skill
relay-sdk-template-writer, whose templates are Relay's examples too (public/examples, kept up to date).
A script runs in its folder's WorkBox (private/'s or public/'s), as agents' commands do (Containers.shell),
with Relay's SDK on its path (sdk/python, which every WorkBox mounts read-only) and signed in as the user: a
session of its own, given as RELAY_TOKEN (which the WorkBox's shell passes on by name: in no file and no
command line) and ended with the run. Its output is there as it's written; each run is kept (script_runs,
the last KEPT of a script) with its output (the last OUTPUT characters). The conversations a run makes with
its session are its: the page shows them with it, and a Stop (or its time limit) stops their agents too,
as a script killed can't.
"""

from __future__ import annotations

import codecs
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from relay import __version__, auth, config, tunables

try:
    import tomllib
except ImportError:  # (Python 3.10: a script that names packages says it can't, as it runs)
    tomllib = None
from relay.sdk import SOURCE

if TYPE_CHECKING:
    from relay.app import Relay

PRIVATE, PUBLIC = "private", "public"  # the user's alone; every agent's to read, the Governor's to run
EXAMPLES = f"{PUBLIC}/examples"  # Relay's, kept up to date
FILE = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\.py"
NAME = re.compile(rf"(?:{PRIVATE}/|{PUBLIC}/(?:examples/)?){FILE}")  # a script: private/a.py, public/b.py, …
KEEP_OUT = "DO_NOT_PUT_FILES_HERE"
KEPT = 20  # runs kept per script
OUTPUT = 200_000  # characters of a run's output kept (its last)
LONGEST = 1_000_000  # characters of a script
SETTLE = 15  # seconds a run's end waits for its Stop to be done with the agents it started
SKILL = Path(__file__).parent / "builtin_skills" / "relay-sdk-template-writer"
TEMPLATES = SKILL / "templates"
EXAMPLES_README = """\
# Relay's examples

Example scripts with Relay's SDK and RESL, as this version of Relay has them. Relay
keeps them up to date: at its next update, what was changed here goes. To change one,
copy it: Copy to my scripts, on the Scripts page.
"""
README = """\
# Relay's scripts

The user's public scripts: Python with Relay's SDK (`from relay_sdk import *`), written
and run on Relay's Scripts page, signed in as them (no password in them). Every agent
may read them; AgentOS's Governor may run them, or make a task that does. An agent
writing one here: the skill relay-sdk-template-writer says how.
"""
PRIVATE_README = """\
# Your private scripts

No agent sees this folder, not even AgentOS's Governor: only you, on the Scripts page
and its network drive. They run in a WorkBox of their own, signed in as you.
"""
KEEP_OUT_TEXT = """\
Relay's scripts are in the two folders beside this file:

  private/  yours alone: no agent sees them, not even AgentOS's Governor
  public/   every agent may read them; AgentOS's Governor may run them, or make a task that does

Files put here, beside them, aren't scripts: Relay neither lists nor runs them.
"""
# The folder's README before private/ and public/ (2.3–2.5): such a folder's scripts move into private/
OLD_README = """\
# Relay's scripts

Python scripts that use Relay's SDK (`from relay_sdk import *`), written and run on
Relay's Scripts page. They run in this folder's WorkBox, signed in as you: no password
in them. An agent writing one here: the skill relay-sdk-template-writer says how.
"""


# The page's documentation, before the SDK's reference (the skill's, which agents read too)
GUIDE = """\
# Scripts

Your own Python, with Relay's SDK. A script on this page:

- **is private or public.** A private one is yours alone: no agent sees it, not even AgentOS's Governor. A public
  one every agent may read, and the Governor may run it, or make a task that does. **Make public** and **Make
  private** (its menu) move it;
- **runs in its folder's WorkBox,** with the SDK on its path, **signed in as you**: `from relay_sdk import *`, then
  `convo.create(…)`, without `login()`;
- **gets its arguments** from the field next to **Run** (`sys.argv[1:]`);
- **gets the packages it names** in a `# /// script` block at its top (PEP 723: `# dependencies = ["requests"]`):
  Relay installs them with pip, in its WorkBox, before it runs;
- **shows what it prints** as it prints it (`print(…, flush=True)`), then how it ended: its exit code (0: it went
  well) and its time. Each script keeps its last 20 runs;
- **lists the conversations it started.** **Stop**, or its time limit (Settings → Limits → Scripts), ends it and
  stops their agents.

Its folder isn't on its import path, so each script is whole in its file. Times in it are the machine's.

**In VS Code:** **Open in VS Code** shares the scripts' folder as a network drive (WebDAV). Open the drive as a
folder in VS Code, or in any editor: what you save there shows here. For completions, install the SDK for the
Python your editor uses ([the SDK for pip](/sdk/)). Run the scripts here: on your own computer a script needs
`login()` first.

**More than the SDK:** RESL, Relay's Extended Standard Library (`from resl import *`, below the SDK): another model
judging an answer (did it refuse? is it done?), answers to data, agents checking agents.

**An agent can write one:** **With an agent**, or `/relay-sdk-template-writer <what it should do>` in a code task.

---

"""


class ScriptError(Exception):
    pass


class ScriptRun:
    """One run of a script: its output as it comes, then how it ended."""

    def __init__(self, user: int, script: str, args: str) -> None:
        self.id = uuid.uuid4().hex
        self.user, self.script, self.args = user, script, args
        self.started = time.time()
        self.finished: float | None = None
        self.status = "running"  # succeeded, failed, stopped, timed out
        self.exit_code: int | None = None
        self.output = ""
        self.cut = 0  # characters of output dropped from its start (OUTPUT)
        self.process: subprocess.Popen | None = None
        self.token: str | None = None  # its session
        self.stopping: str | None = None  # why it's being stopped: "stopped", "timed out"
        self.settled = threading.Event()  # the Stop is done (its agents stopped too)
        self.conversations: list[str] = []  # made with its session (Scripts.made)
        self.lock = threading.Lock()

    def add(self, text: str) -> None:
        with self.lock:
            self.output += text
            if len(self.output) > OUTPUT:
                self.cut += len(self.output) - OUTPUT
                self.output = self.output[-OUTPUT:]

    def public(self) -> dict[str, Any]:
        with self.lock:
            return {"id": self.id, "script": self.script, "args": self.args, "started": self.started,
                    "finished": self.finished, "status": self.status, "exit_code": self.exit_code,
                    "output": self.output, "cut": self.cut, "conversations": list(self.conversations)}


# The packages a script needs: a `# /// script` block with `dependencies = ["requests", …]` (PEP 723, inline script
# metadata), which Relay installs with pip before it runs
NEEDS = re.compile(r"(?m)^# /// (?P<type>[a-zA-Z0-9-]+)$\s(?P<content>(^#(| .*)$\s)+)^# ///$")


def needs(text: str) -> list[str]:
    """The packages a script names in its `# /// script` block, as pip takes them; none without one."""
    blocks = [found for found in NEEDS.finditer(text) if found.group("type") == "script"]
    if not blocks:
        return []
    if len(blocks) > 1:
        raise ScriptError("It has two `# /// script` blocks: one says what it needs")
    if tomllib is None:
        raise ScriptError("Its `# /// script` block takes Python 3.11 or newer for Relay itself to read")
    content = "".join(line[2:] if line.startswith("# ") else line[1:]
                      for line in blocks[0].group("content").splitlines(keepends=True))
    try:
        packages = tomllib.loads(content).get("dependencies", [])
    except tomllib.TOMLDecodeError as exc:
        raise ScriptError(f"Its `# /// script` block isn't TOML ({exc})") from None
    if not isinstance(packages, list) or not all(isinstance(package, str) for package in packages):
        raise ScriptError("Its `# /// script` block's dependencies are a list of packages, as pip takes them")
    return packages


def _made(path: Path) -> Path:
    """The folder with its private/ and public/ (each with a README), made when they aren't there yet."""
    try:
        for part, readme in ((PRIVATE, PRIVATE_README), (PUBLIC, README)):
            (path / part).mkdir(parents=True, exist_ok=True)
            if not (path / part / "README.md").exists():
                (path / part / "README.md").write_text(readme)
    except OSError as exc:
        raise ScriptError(f"Relay can't keep the scripts in {path} ({exc.strerror or exc}): choose another "
                          "folder") from None
    _examples(path / PUBLIC)
    return path


def _examples(path: Path) -> None:
    """The examples (the skill's templates) in examples/, as this version of Relay has them: written when the
    version changed or they're gone, and what an older one wrote that this one doesn't have removed. Changes made
    there in between stay until then."""
    folder = path / "examples"
    marker = folder / ".relay"
    try:
        kept = json.loads(marker.read_text())
    except (OSError, ValueError):
        kept = {}
    if kept.get("version") == __version__ and folder.is_dir():
        return
    try:
        folder.mkdir(exist_ok=True)
        names = []
        for source in sorted(TEMPLATES.glob("*.py")):
            (folder / source.name).write_text(source.read_text())
            names.append(source.name)
        for old in kept.get("files") or []:
            if old not in names and re.fullmatch(FILE, old):
                (folder / old).unlink(missing_ok=True)
        (folder / "README.md").write_text(EXAMPLES_README)
        marker.write_text(json.dumps({"version": __version__, "files": names}))
    except OSError as exc:  # (no examples is no reason to keep the scripts from the page)
        print(f"relay: couldn't write the scripts' examples: {exc}", file=sys.stderr, flush=True)


def _example(name: str) -> bool:
    return name.startswith(f"{EXAMPLES}/")


class Scripts:
    def __init__(self, relay: Relay) -> None:
        self.relay = relay
        self.active: dict[str, ScriptRun] = {}
        self.sessions: dict[str, ScriptRun] = {}  # a going run's session token -> the run
        self.lock = threading.Lock()

    # The folder and its scripts

    def _root(self) -> Path | None:
        """Where the scripts' folder is, or will be: the one chosen, else `scripts` in the first workspace root."""
        chosen = self.relay.settings.scripts_folder
        if chosen:
            return Path(chosen).expanduser().resolve()
        roots = self.relay.settings.roots()
        return (roots[0] / "scripts").resolve() if roots else None

    def folder(self) -> Path:
        """The scripts' folder: made when it isn't there yet, the boxes told where it is, settled (_settle)."""
        root = self._root()
        if root is None:
            raise ScriptError("The scripts' folder is in a workspace root: add one first (Settings → Workspaces)")
        root = _made(root)
        self._tell(root)
        if not (root / KEEP_OUT).exists():
            self._settle(root, default=not self.relay.settings.scripts_folder)
        return root

    def _settle(self, root: Path, default: bool) -> None:
        """Once a folder has private/ and public/: what a scripts' folder from before them (Relay's default one, or
        one with Relay's old README) has beside them moves into private/, the user's alone (the safe side); Relay's
        own old README and examples go. Then DO_NOT_PUT_FILES_HERE says it's done. (Another folder chosen for the
        scripts keeps what it has.) Their runs are private/'s already (relay.db); one renamed here, its runs too."""
        readme = root / "README.md"
        try:
            old = readme.is_file() and readme.read_text(errors="replace") == OLD_README
            if default or old:
                renamed = {}
                for entry in sorted(root.iterdir()):
                    if entry.name in (PRIVATE, PUBLIC, KEEP_OUT) or entry.name.startswith("."):
                        continue  # (.git and the like stay)
                    if entry.name == "README.md" and old:
                        entry.unlink()
                        continue
                    if entry.name == "examples" and (entry / ".relay").exists():
                        shutil.rmtree(entry, ignore_errors=True)  # (Relay's: anew in public/examples)
                        continue
                    target, more = root / PRIVATE / entry.name, 2
                    while target.exists():
                        target, more = root / PRIVATE / f"{entry.stem}_{more}{entry.suffix}", more + 1
                    entry.rename(target)
                    renamed[entry.name] = target.name
                for was, now in renamed.items():
                    if now != was:
                        self.relay.db.execute("UPDATE script_runs SET script = ? WHERE script = ?",
                                              (f"{PRIVATE}/{now}", f"{PRIVATE}/{was}"))
            (root / KEEP_OUT).write_text(KEEP_OUT_TEXT)
        except OSError as exc:
            raise ScriptError(f"Relay couldn't put the scripts in {root} in order ({exc.strerror or exc})") from None

    def public_folder(self) -> Path:
        return self.folder() / PUBLIC

    def set_folder(self, value: str) -> Path:
        """Another folder for the scripts: in a workspace root (agents work there too)."""
        path = Path(str(value or "")).expanduser()
        if not path.is_absolute():
            raise ScriptError("Give the folder's full path")
        path = path.resolve()
        if not any(path == root or root in path.parents for root in self.relay.settings.roots()):
            raise ScriptError("The scripts' folder must be in a workspace root (Settings → Workspaces)")
        _made(path)  # (kept only once it's there)
        self.relay.settings.scripts_folder = str(path)
        config.save_settings(self.relay.settings)
        self.folder()  # (settled, and the boxes told)
        return path

    def show(self) -> None:
        """At Relay's start, a scripts' folder that's there: settled (one from before private/ and public/ too)
        and the boxes told where it is. One that isn't there yet is made at its first use, not before."""
        root = self._root()
        if root is not None and root.exists():
            try:
                self.folder()
            except ScriptError:
                self._tell(root)  # (settled or not, its private/ stays out of sight)

    def _tell(self, root: Path) -> None:
        """No box shows private/ (an empty folder in its place; a private script's own WorkBox does), every
        WorkBox has public/, read-only (relay.containers)."""
        containers = self.relay.containers
        if hasattr(containers, "scripts_folders"):
            containers.scripts_folders(root / PRIVATE, root / PUBLIC)

    def listed(self) -> list[dict[str, Any]]:
        with self.lock:
            going = {run.script for run in self.active.values()}
        found = []
        folder = self.folder()
        for prefix in (f"{PRIVATE}/", f"{PUBLIC}/", f"{EXAMPLES}/"):
            for path in sorted((folder / prefix).glob("*.py"), key=lambda p: p.name.lower()):
                name = prefix + path.name
                if path.is_file() and NAME.fullmatch(name):
                    stat = path.stat()
                    found.append({"name": name, "size": stat.st_size, "modified": stat.st_mtime,
                                  "running": name in going, "private": prefix == f"{PRIVATE}/",
                                  "example": prefix == f"{EXAMPLES}/"})
        return found

    def path(self, name: str) -> Path:
        name = str(name or "")
        if not NAME.fullmatch(name):
            raise ScriptError("A script's name: private/ or public/, then letters, digits, - _ and ., ending in .py "
                              "(e.g. private/ask.py)")
        return self.folder() / name

    def named(self, name: str) -> str:
        """A script's full name. One given without its folder ("report.py", as a person, or a script, may write it)
        is private/'s, else public/'s, whichever has it; private/'s when neither does (a new one)."""
        name = str(name or "")
        if "/" in name or not re.fullmatch(FILE, name):
            return name
        found = next((f"{folder}/{name}" for folder in (PRIVATE, PUBLIC) if (self.folder() / folder / name).is_file()),
                     None)
        return found or f"{PRIVATE}/{name}"

    def read(self, name: str) -> str:
        path = self.path(name)
        if not path.is_file():
            # (made public or private since: say where it is)
            elsewhere = next((f"{folder}/{path.name}" for folder in (PRIVATE, PUBLIC)
                              if folder != name.split("/")[0] and (self.folder() / folder / path.name).is_file()), None)
            raise ScriptError(f"There's no script {name}" + (f": it's {elsewhere} now" if elsewhere else ""))
        return path.read_text(errors="replace")

    def _yours(self, name: str) -> Path:
        """A script that's the user's to change: not one of Relay's examples."""
        if _example(str(name or "")):
            raise ScriptError("Relay keeps the examples up to date: copy one to change it (Copy to my scripts)")
        return self.path(name)

    def write(self, name: str, text: str) -> dict[str, Any]:
        if len(text) > LONGEST:
            raise ScriptError(f"A script is at most {LONGEST} characters")
        path = self._yours(name)
        # Whole or not at all: a run that starts meanwhile reads the one or the other
        written = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}")
        written.write_text(text)
        os.replace(written, path)
        stat = path.stat()
        return {"name": name, "size": stat.st_size, "modified": stat.st_mtime}

    def create(self, name: str, text: str) -> dict[str, Any]:
        """A new script: never over one that's there (write saves over it)."""
        if self._yours(name).exists():
            raise ScriptError(f"There's a script {name} already")
        return self.write(name, text)

    def rename(self, name: str, to: str) -> None:
        source, target = self._yours(name), self._yours(to)
        if not source.is_file():
            raise ScriptError(f"There's no script {name}")
        if target.exists():
            raise ScriptError(f"There's a script {to} already")
        source.rename(target)
        self.relay.db.execute("UPDATE script_runs SET script = ? WHERE script = ?", (to, name))
        # The user's own tasks that run it follow it (not the Governor's: one made private stays out of its sight;
        # its task says it's gone, and the Governor sees to it)
        self.relay.db.execute("UPDATE schedules SET script = ? WHERE script = ? AND managed = 0", (to, name))

    def free(self, folder: str, wish: str) -> str:
        """A script's name in private/ or public/ that isn't taken: `wish` (x.py), else x_2.py, x_3.py, …"""
        if folder not in (PRIVATE, PUBLIC):
            raise ScriptError("A script goes into private or public")
        stem = Path(wish).name.removesuffix(".py")
        name, more = f"{folder}/{stem}.py", 2
        while self.path(name).exists():
            name, more = f"{folder}/{stem}_{more}.py", more + 1
        return name

    def copy(self, name: str, to: str = PRIVATE, wish: str | None = None) -> str:
        """A script (one of Relay's examples, say) as a new one of the user's, in private/ or public/: under
        `wish` or its own name, else the next one free (free); its name."""
        made = self.free(to, wish or name)
        self.create(made, self.read(name))
        return made

    def move(self, name: str, to: str) -> str:
        """A script into private/ or public/ (who may see it changes, and whether the Governor may run it): under
        its name, else the next one free; its name there."""
        self._yours(name)
        if name.split("/", 1)[0] == to:
            return name
        moved = self.free(to, name)
        self.rename(name, moved)
        return moved

    def delete(self, name: str) -> None:
        path = self._yours(name)
        # Its runs going now end with it: gone from the list, one couldn't be found or stopped any more, and went
        # on unseen
        with self.lock:
            going = [run for run in self.active.values() if run.script == name]
        for run in going:
            self._stop(run, "stopped")
        if path.is_file():
            path.unlink()
        self.relay.db.execute("DELETE FROM script_runs WHERE script = ?", (name,))

    def templates(self) -> list[dict[str, Any]]:
        """The skill's templates (relay-sdk-template-writer), to start a script from: name, what it does (its
        docstring's first line), its text."""
        found = []
        for path in sorted(TEMPLATES.glob("*.py")):
            text = path.read_text()
            about = re.match(r'\s*"""(.*?)(?:\n|""")', text)
            found.append({"name": path.name, "description": about.group(1).strip() if about else "", "text": text})
        return found

    def docs(self) -> str:
        """The page's documentation: how a script runs here, then the SDK, then RESL (built on it)."""
        return GUIDE + (SKILL / "reference.md").read_text() + "\n\n---\n\n" + (SKILL / "resl.md").read_text()

    # Runs

    def start(self, user: int, name: str, args: str = "") -> str:
        """Run a script; its run's id at once (it goes on in a thread of its own)."""
        self.read(name)  # (there, and a script's name)
        try:
            words = shlex.split(str(args or ""))
        except ValueError as exc:
            raise ScriptError(f"Its arguments don't parse: {exc}") from None
        run = ScriptRun(user, name, " ".join(shlex.quote(w) for w in words))  # (examples/…: an example)
        self.relay.db.execute("INSERT INTO script_runs (id, user_id, script, args, started, status) "
                              "VALUES (?, ?, ?, ?, ?, 'running')", (run.id, user, run.script, run.args, run.started))
        with self.lock:
            self.active[run.id] = run
        threading.Thread(target=self._run, args=(run,), name=f"script-{run.id[:8]}", daemon=True).start()
        return run.id

    def _run(self, run: ScriptRun) -> None:
        minutes = tunables.get("scripts.timeout")
        # Its time limit counts from here: its WorkBox may take a while to start (the first time)
        timer = threading.Timer(minutes * 60, self._stop, args=(run, "timed out"))
        timer.daemon = True
        timer.start()
        status = "failed"
        try:
            token = self.relay.accounts.start_session(run.user, (minutes + 5) / 1440)
            # (its key, for a restart of Relay during the run to end it: reconcile)
            self.relay.db.execute("UPDATE script_runs SET session = ? WHERE id = ?", (auth.token_hash(token), run.id))
            with self.lock:
                self.sessions[token] = run
            with run.lock:
                run.token = token
            # In private/'s or public/'s own WorkBox (started first, which may take a moment the first time)
            top, _, inside = run.script.partition("/")
            folder = self.folder() / top
            packages = needs((folder / inside).read_text(errors="replace"))
            shell = str(self.relay.containers.shell(folder)) if self.relay.containers is not None else "bash"
            # Not its folder first on its path (PYTHONSAFEPATH): a script there called random.py or email.py would
            # hide Python's own from every script (the SDK's urllib imports them), and from pip
            command = (f"PYTHONSAFEPATH=1 PYTHONPATH={shlex.quote(str(SOURCE.parent))} exec python3 -u "
                       f"{shlex.quote(inside)}" + (f" {run.args}" if run.args else ""))
            if packages:
                # First the packages it names, for its WorkBox's user: in that WorkBox's home, which stays, so
                # pip finds them there from then on
                failed = shlex.quote("[Relay] The packages it names couldn't be installed (pip, above)")
                command = (f"PYTHONSAFEPATH=1 python3 -m pip install --user --quiet --no-warn-script-location -- "
                           f"{' '.join(map(shlex.quote, packages))} || {{ echo {failed}; exit 1; }}; {command}")
            env = {**os.environ, "RELAY_TOKEN": token, "RELAY_URL": self.relay.runs.local_url or ""}
            with run.lock:  # (a Stop from here on finds its process; one before, none is started)
                if run.stopping:
                    raise ScriptError("its time was up first" if run.stopping == "timed out"
                                      else "it was stopped first")
                run.process = subprocess.Popen([shell, "-c", command], cwd=folder, env=env, stdout=subprocess.PIPE,
                                               stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                               start_new_session=True)
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            for chunk in iter(lambda: os.read(run.process.stdout.fileno(), 4096), b""):
                run.add(decoder.decode(chunk))
            run.add(decoder.decode(b"", final=True))
            run.exit_code = run.process.wait()
            status = run.stopping or ("succeeded" if run.exit_code == 0 else "failed")
        except Exception as exc:  # noqa: BLE001 - a run that couldn't start says why, in its output
            run.add(f"\n[Relay] It didn't run: {exc}\n")
            status = run.stopping or "failed"
        finally:
            timer.cancel()
            if run.stopping:
                run.settled.wait(SETTLE)  # (its Stop says what it stopped, first)
            with self.lock:
                self.sessions.pop(run.token or "", None)
            self.relay.accounts.end_session(run.token)
            if run.process is not None and run.process.stdout:
                run.process.stdout.close()
            with run.lock:  # (its end, once all of it is done: what follows its output ends there)
                run.finished, run.status = time.time(), status
            self._store(run)
            with self.lock:
                self.active.pop(run.id, None)

    def stop(self, run_id: str) -> bool:
        """Stop a run; whether one was going."""
        with self.lock:
            run = self.active.get(run_id)
        if run is None:
            return False
        self._stop(run, "stopped")
        return True

    def _stop(self, run: ScriptRun, why: str) -> None:
        with run.lock:
            run.stopping = run.stopping or why
            process, token = run.process, run.token
        try:
            # Its session first: it can't start anything more as you
            self.relay.accounts.end_session(token)
            if process is not None and process.poll() is None:
                # Its process group: the WorkBox's shell, whose watcher then ends what it started in the box
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    time.sleep(0.5)
                    os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            # Then the agents it started that still work: a script killed can't stop them
            said = "its script ran out of time" if why == "timed out" else "its script was stopped"
            stopped = [c for c in list(run.conversations) if self._stop_agent(c, said)]
            if stopped:
                run.add(f"\n[Relay] Stopped the agent{'s' if len(stopped) > 1 else ''} it started in "
                        f"{len(stopped)} conversation{'s' if len(stopped) > 1 else ''}\n")
        finally:
            run.settled.set()

    def _stop_agent(self, conversation_id: str, why: str) -> bool:
        going = self.relay.runs.active_run_for(conversation_id)
        return bool(going) and self.relay.runs.stop(going, why)

    def script_for(self, token: str | None) -> str | None:
        """The script a session is a going run's (a conversation it makes is started by that script), or None."""
        with self.lock:
            run = self.sessions.get(token or "")
        return run.script if run is not None else None

    def made(self, token: str | None, conversation_id: str) -> None:
        """A conversation made with a session (routes.conversations): a going run's, when it's that run's."""
        with self.lock:
            run = self.sessions.get(token or "")
        if run is not None:
            with run.lock:
                run.conversations.append(conversation_id)

    def _store(self, run: ScriptRun) -> None:
        found = run.public()
        self.relay.db.execute("UPDATE script_runs SET finished = ?, status = ?, exit_code = ?, output = ?, cut = ?, "
                              "conversations = ?, session = NULL WHERE id = ?",
                              (run.finished, run.status, run.exit_code, found["output"], found["cut"],
                               json.dumps(found["conversations"]), run.id))
        self.relay.db.execute("DELETE FROM script_runs WHERE script = ? AND id NOT IN (SELECT id FROM script_runs "
                              "WHERE script = ? ORDER BY started DESC LIMIT ?)", (run.script, run.script, KEPT))

    def run(self, run_id: str) -> dict[str, Any] | None:
        """A run as it is now (one going: its output so far). `cut`: the characters of its output's start left
        out (it keeps the last OUTPUT)."""
        with self.lock:
            going = self.active.get(run_id)
        if going is not None:
            return going.public()
        row = self.relay.db.one("SELECT * FROM script_runs WHERE id = ?", (run_id,))
        return None if row is None else {**{k: row[k] for k in ("id", "script", "args", "started", "finished",
                                                               "status", "exit_code")},
                                         "output": row["output"] or "", "cut": row["cut"] or 0,
                                         "conversations": json.loads(row["conversations"] or "[]")}

    def runs(self, name: str, user: int) -> list[dict[str, Any]]:
        """A script's runs of this user's, newest first (without their output)."""
        rows = self.relay.db.all("SELECT id, script, args, started, finished, status, exit_code FROM script_runs "
                                 "WHERE script = ? AND user_id = ? ORDER BY started DESC LIMIT ?",
                                 (str(name), user, KEPT))
        return [dict(r) for r in rows]

    def reconcile(self) -> None:
        """At Relay's start: runs a stop left going ended with it, and their sessions with them (a script still
        at work in its WorkBox can't go on as you)."""
        self.relay.db.execute("DELETE FROM auth_sessions WHERE token_hash IN (SELECT session FROM script_runs "
                              "WHERE status = 'running' AND session IS NOT NULL)")
        self.relay.db.execute("UPDATE script_runs SET status = 'failed', finished = ?, session = NULL, "
                              "output = COALESCE(output, '') || ? WHERE status = 'running'",
                              (time.time(), "\n[Relay restarted during this run]\n"))
