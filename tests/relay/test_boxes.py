"""ProviderBoxes (relay.boxes): a box's shell hands commands to Relay, which runs them in the
project's WorkBox."""


import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from relay.boxes import BOXFILE, Boxes, BoxLaunch, ExecGateway, _wanted, command_of
from relay.containers import ContainerError, Containers

SHELL = BOXFILE.parent / "box" / "relay-shell"


class Placed(Containers):
    """WorkBoxes without starting any: where a command would run."""

    def ensure(self, directory):
        return "work-" + Path(directory).name


def shell(socket, *args, cwd, folder=None, stdin=None):
    env = {**os.environ, "RELAY_EXEC_SOCKET": str(socket)}
    env.pop("RELAY_FOLDER", None)
    if folder:
        env["RELAY_FOLDER"] = str(folder)
    return subprocess.run([sys.executable, str(SHELL), *args], cwd=cwd, env=env, input=stdin,
                          capture_output=True, text=True, timeout=30)


def test_a_boxs_shell_runs_commands_through_relay(tmp_path, podman):
    project = tmp_path / "project"
    (project / "sub").mkdir(parents=True)
    containers = Placed(state=tmp_path / "state")
    gateway = ExecGateway(tmp_path / "box" / "exec.sock", containers, tmp_path / "commands.log", lambda: [project])
    gateway.start()
    try:
        done = shell(gateway.path, "-c", "pwd; echo oops >&2; exit 3", cwd=project / "sub", folder=project)
        assert done.returncode == 3 and done.stdout.strip() == str(project / "sub") and done.stderr == "oops\n"
        run = [c for c in podman() if c[:1] == ["exec"]][-1]
        assert "work-project" in run and run[run.index("-w") + 1] == str(project / "sub")
        assert any(a.startswith("RELAY_EXEC=") for a in run)  # its tag, to end what it starts
        # Input goes along; outside the project a command runs in the project
        assert shell(gateway.path, "-c", "cat", cwd=project, folder=project, stdin="piped").stdout == "piped"
        assert shell(gateway.path, "-c", "pwd", cwd=tmp_path, folder=project).stdout.strip() == str(project)
        # Claude Code calls it -c -l; the guard is Relay's, in one place
        refused = shell(gateway.path, "-c", "-l", "rm -rf /", cwd=project, folder=project)
        assert refused.returncode == 126 and "Relay refused this command" in refused.stderr
        assert "refused" in (tmp_path / "commands.log").read_text()
        # Without a run's folder: where Relay routes the folder it runs in, else that folder
        containers.route(project)
        assert shell(gateway.path, "-c", "true", cwd=project / "sub").returncode == 0
        assert "work-project" in [c for c in podman() if c[:1] == ["exec"]][-1]
        # Not a project folder: no
        nowhere = shell(gateway.path, "-c", "true", cwd=str(Path.home()))
        assert nowhere.returncode == 125 and "no project folder" in nowhere.stderr
    finally:
        gateway.stop()
    gone = shell(gateway.path, "-c", "true", cwd=project, folder=project)
    assert gone.returncode == 127 and "Relay isn't running" in gone.stderr


def test_a_command_runs_only_in_a_folder_the_box_has_as_a_project(tmp_path, podman):
    """What a box asks for comes from the box, where an agent may have got a program of its own running, and a WorkBox
    has its folder writable. A folder of the machine, or a link to one, isn't for it to name; nor are private scripts."""
    project, elsewhere = tmp_path / "project", tmp_path / "elsewhere"
    (project / "sub").mkdir(parents=True)
    elsewhere.mkdir()
    (project / "link").symlink_to(elsewhere)
    private = project / "scripts" / "private"
    (private / "deep").mkdir(parents=True)
    containers = Placed(state=tmp_path / "state")
    containers.scripts_folders(private, project / "scripts" / "public")
    containers.route(elsewhere)  # (a run of another provider: this box still doesn't have it)
    gateway = ExecGateway(tmp_path / "box" / "exec.sock", containers, tmp_path / "commands.log", lambda: [project])
    gateway.start()
    try:
        assert shell(gateway.path, "-c", "pwd", cwd=project / "sub").stdout.strip() == str(project / "sub")
        assert shell(gateway.path, "-c", "true", cwd=project, folder=project / "sub").returncode == 0
        before = len(podman())
        for folder in (elsewhere, project / "link", tmp_path, private, private / "deep"):
            named = shell(gateway.path, "-c", "true", cwd=project, folder=folder)
            within = shell(gateway.path, "-c", "true", cwd=folder)
            for refused in (named, within):
                assert refused.returncode == 125 and "no project folder" in refused.stderr, folder
        assert len(podman()) == before  # (nothing ran, and no container was asked for)
    finally:
        gateway.stop()


def test_what_a_boxs_commands_may_have_as_their_folder(tmp_path, monkeypatch):
    """What it has writable of the workspace's and Relay's, and the folders its runs worked in: not a login, not
    other conversations' attachments, however they're mounted."""
    from relay import config
    monkeypatch.setattr(config, "data_dir", lambda: tmp_path / "data")
    roots, away = tmp_path / "projects", tmp_path / "away"
    roots.mkdir()
    away.mkdir()
    settings = type("Settings", (), {"workspace_roots": [str(roots)], "chat_directory": lambda self: tmp_path / "chats"})()
    boxes = Boxes(Containers(state=tmp_path / "state"), settings)
    boxes._extras("claude", away)  # (a run of this provider worked there)
    found = boxes.projects("claude")
    assert {roots, tmp_path / "chats", away, tmp_path / "data" / "repairs", tmp_path / "data" / "agentos"} <= set(found)
    assert tmp_path / "data" / "uploads" not in found and tmp_path / "data" / "handover" not in found
    assert away not in boxes.projects("other-provider")


def test_command_of():
    assert command_of(["-c", "ls"]) == "ls"
    assert command_of(["-c", "-l", "ls"]) == "ls"
    assert command_of(["--norc", "--noprofile", "-c", "ls"]) == "ls"
    assert command_of(["-lc", "ls", "name", "arg"]) == "ls"
    assert command_of(["script.sh"]) is None and command_of([]) is None


def test_what_a_box_has(tmp_path, monkeypatch):
    from relay import config
    monkeypatch.setattr(config, "data_dir", lambda: tmp_path / "data")
    real = tmp_path / "real-profiles"
    real.mkdir()
    roots = tmp_path / "projects"
    (roots / "inside").mkdir(parents=True)
    (roots / "link").symlink_to(real)  # a link inside a folder the box has: the link is there itself
    alone = tmp_path / "alone"
    alone.symlink_to(real)  # a link nowhere else: mounted at its path too
    settings = type("S", (), {"workspace_roots": [str(roots)], "chat_directory": lambda self: tmp_path / "chats"})()
    boxes = Boxes(Containers(state=tmp_path / "state"), settings)
    argv = boxes._argv("p", [(roots / "link", True), (alone, True), (tmp_path / "missing", True)],
                       boxes._extras("p", tmp_path / "elsewhere"))
    volumes = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    assert f"{roots}:{roots}" in volumes and f"{real}:{real}" in volumes and f"{real}:{alone}" in volumes
    assert not any(v.endswith(f":{roots / 'link'}") for v in volumes) and not any("missing" in v for v in volumes)
    assert "-e" in argv and "RELAY_BOX=1" in argv and argv[-3] == boxes.image
    # Repairs' and new kinds' folders from the start: a box in use isn't made anew for a new one
    assert f"{tmp_path / 'data' / 'repairs'}:{tmp_path / 'data' / 'repairs'}" in volumes
    # The user's private scripts: an empty, read-only folder in their place (the roots have them)
    private = roots / "scripts" / "private"
    boxes.containers.scripts_folders(private, roots / "scripts" / "public")
    assert f"{boxes.containers.empty}:{private}:ro" not in boxes._argv("p", [], [])  # (not there yet)
    private.mkdir(parents=True)
    volumes = [v for i, v in enumerate(boxes._argv("p", [], [])) if i and boxes._argv("p", [], [])[i - 1] == "-v"]
    assert f"{boxes.containers.empty}:{private}:ro" in volumes
    # A project outside the roots joins the box, and stays (so it isn't made anew each time)
    (tmp_path / "elsewhere").mkdir()
    assert boxes._extras("p", tmp_path / "elsewhere") == [tmp_path / "elsewhere"]
    assert boxes._extras("p", None) == [tmp_path / "elsewhere"]
    assert boxes._extras("p", roots / "inside") == [tmp_path / "elsewhere"]  # in the roots: there already


def test_a_box_with_a_home_of_its_own(tmp_path, monkeypatch):
    """A provider whose program the user installed in a home of its own (Template.home): its box has that home,
    writable, as its HOME, npm's global installs go there, and what's installed there comes first on PATH."""
    from relay import config
    from relay.providers import KINDS, parse_template
    monkeypatch.setattr(config, "data_dir", lambda: tmp_path / "data")
    settings = type("S", (), {"workspace_roots": [], "chat_directory": lambda self: tmp_path / "chats"})()
    boxes = Boxes(Containers(state=tmp_path / "state"), settings)
    home = tmp_path / "data" / "provider-homes" / "abc"
    argv = boxes._argv("p", [], [], None, home)
    assert home.is_dir() and f"{home}:{home}" in [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    env = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
    assert f"HOME={home}" in env and f"NPM_CONFIG_PREFIX={home / '.local'}" in env
    assert f"PATH={home / '.local' / 'bin'}:{home / 'bin'}:" in " ".join(env)
    plain = boxes._argv("p", [], [])
    assert f"HOME={Path.home()}" in [plain[i + 1] for i, a in enumerate(plain) if a == "-e"]
    # The template's, and the program installed there
    template = parse_template({"id": "toy", "name": "Toy", "kind": "example", "home": str(home)})
    assert template.to_dict()["home"] == str(home)
    provider = KINDS["example"](template)
    assert provider.box_home() == home and provider.home_program("toy") is None
    (home / ".local" / "bin").mkdir(parents=True)
    (home / ".local" / "bin" / "toy").write_text("#!/bin/sh\n")
    (home / ".local" / "bin" / "toy").chmod(0o755)
    assert provider.home_program("toy") == home / ".local" / "bin" / "toy"
    with pytest.raises(Exception, match="absolute"):
        parse_template({"id": "toy", "name": "Toy", "kind": "example", "home": "relative/home"})
    assert "home" not in parse_template({"id": "toy", "name": "Toy", "kind": "example"}).to_dict()


def test_a_setup_box_has_real_shells_and_the_providers_home(tmp_path, monkeypatch):
    """Where the user installs a new provider's program and logs in (relay.creations): Relay's box image with its
    shells real ones (installers run bash and sh), the provider's home as its HOME as its own box will have it."""
    from relay import boxes as boxes_module
    from relay import config
    monkeypatch.setattr(config, "data_dir", lambda: tmp_path / "data")
    made = []
    monkeypatch.setattr(boxes_module, "up", lambda name, spec, argv, what, found, labels=(): made.append(
        (name, argv, labels)))
    monkeypatch.setattr(Containers, "_state", lambda self, name: (None, None))
    boxes = Boxes(Containers(state=tmp_path / "state"), None)
    monkeypatch.setattr(boxes, "check", lambda: None)
    home = tmp_path / "data" / "provider-homes" / "abc"
    name = boxes.setup("new-abc", home)
    [(made_name, argv, labels)] = made
    assert name == made_name == boxes.setup_name("new-abc") and name.startswith("relay-setup-new-abc")
    assert labels == (boxes_module.SETUP_LABEL,) and home.is_dir()
    assert '$HOME/.local/bin' in (home / ".profile").read_text()  # (a login shell's PATH has it too)
    assert f"{home}:{home}" in argv and f"HOME={home}" in argv and f"NPM_CONFIG_PREFIX={home / '.local'}" in argv
    assert f"{boxes.state / 'here-shell'}:/usr/lib/relay/relay-shell:ro" in argv
    assert argv[argv.index("-w") + 1] == str(home) and argv[-3:] == [boxes.image, "sleep", "infinity"]



def test_what_a_box_mounts_is_there_from_its_first_spec(tmp_path, monkeypatch):
    """A folder that came later would make a box anew, and end a shell open in it: on a new Relay, the first test of a
    provider (its chats' folder), with that shell just opened on Add provider's next step."""
    from relay import config, skills
    monkeypatch.setattr(config, "data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(skills, "user_dir", lambda: tmp_path / "data" / "skills")
    monkeypatch.setattr(config, "worktrees_dir", lambda: tmp_path / "data" / "worktrees")
    boxes = Boxes(Containers(state=tmp_path / "state"), None)
    first = _wanted(boxes._common())
    assert (tmp_path / "data" / "chats").is_dir() and (tmp_path / "data" / "uploads").is_dir()
    assert {tmp_path / "data" / name for name in ("chats", "uploads", "handover", "worktrees", "skills")} <= {
        real for real, _, _ in first}
    assert _wanted(boxes._common()) == first


def test_a_run_in_its_box():
    launch = BoxLaunch("relay-box-claude-code", tag="t1")
    argv = launch.wrap(["/bin/claude", "-p"], "/work/p", {"RELAY_FOLDER": "/work/p"})
    assert argv == ["podman", "exec", "-i", "-w", "/work/p", "-e", "RELAY_FOLDER=/work/p", "-e", "RELAY_RUN=t1",
                    "relay-box-claude-code", "/bin/claude", "-p"]


def test_which_providers_have_boxes(tmp_path):
    from relay.providers.acp import AcpProvider, CopilotProvider
    from relay.providers.base import Template
    from relay.providers.claude import ClaudeCodeProvider
    from relay.providers.opencode import OpencodeProvider
    claude = ClaudeCodeProvider(Template("cc", "Claude", "claude-code", True, {"config_dir": str(tmp_path / "c")}))
    assert claude.box_key() == "cc" and (tmp_path / "c", True) in claude.box_mounts()
    copilot = CopilotProvider(Template("copilot", "Copilot", "copilot", True, {"env": {"COPILOT_HOME": "/x"}}))
    assert copilot.box_key() == "copilot" and (Path("/x"), True) in copilot.box_mounts()
    # every opencode provider shares opencode-manager's box
    assert OpencodeProvider(Template("oc", "oc", "opencode", True, {})).box_key() == "opencode-manager"
    assert AcpProvider(Template("g", "Gemini", "acp", True, {"command": ["gemini"]})).box_key() is None


def test_without_a_box_image_there_are_no_boxes(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    boxes = Boxes(Containers(state=tmp_path / "state"))
    try:
        boxes.ensure("p", [])
    except ContainerError as exc:
        assert "podman isn't installed" in str(exc)
    else:
        raise AssertionError("no podman, and still a box")



def test_boxes_that_see_a_removed_copies_folder_are_made_anew(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from relay import boxes as boxes_module
    from relay import forks
    from relay.boxes import Boxes

    inodes = None
    removed = []

    from relay.containers import instance
    mark = instance()

    def run(argv, **_):
        if argv[:2] == ["podman", "ps"]:  # (and another Relay's, with a box it sees otherwise: not this one's)
            return SimpleNamespace(returncode=0, stdout=f"relay-box-old|{mark}\nrelay-box-fine|{mark}\n"
                                                        "relay-box-old-feedface|feedface\nrelay-box-legacy|\n")
        if argv[:2] == ["podman", "exec"]:  # what each box sees at the folder's place
            return SimpleNamespace(returncode=0, stdout="12345\n" + "".join(f"{i}\n" for i in inodes[1:])
                                   if argv[2].startswith("relay-box-old") else "".join(f"{i}\n" for i in inodes))
        if argv[:2] == ["podman", "rm"]:
            removed.append(argv[-1])
        return SimpleNamespace(returncode=0, stdout="")
    monkeypatch.setattr(boxes_module.subprocess, "run", run)
    from relay import config
    watched = [forks.folders(), config.data_dir() / "agentos", config.data_dir() / "production",
               config.data_dir() / "repairs"]
    for folder in watched:
        folder.mkdir(parents=True)
    inodes = [folder.stat().st_ino for folder in watched]
    containers = SimpleNamespace(state=tmp_path / "state")
    assert Boxes(containers).drop_stale() == ["relay-box-old"]
    assert removed == ["relay-box-old"]


def test_relay_is_on_the_commands_path_and_knows_whose_tools(tmp_path, podman, monkeypatch):
    from relay.containers import BIN
    project = tmp_path / "project"
    project.mkdir()
    containers = Placed(state=tmp_path / "state")
    gateway = ExecGateway(tmp_path / "box" / "exec.sock", containers, tmp_path / "commands.log", lambda: [project])
    gateway.start()
    try:
        containers.set_url("http://127.0.0.1:8420")
        monkeypatch.setenv("RELAY_TOOLS", "/cache/relay-tools/abc/relay")  # Claude Code's, say
        shell(gateway.path, "-c", "true", cwd=project, folder=project)
        run = [c for c in podman() if c[:1] == ["exec"]][-1]
        assert f"PATH={BIN}:" in " ".join(run)  # container/bin/relay first: plain `relay` works
        # Its address, not the script's path: the box has no file with a token in it
        assert "RELAY_MCP_URL=http://127.0.0.1:8420/mcp/abc" in run
        assert not any(a.startswith("RELAY_TOOLS=") for a in run)
        assert f"RELAY_URL_FILE={containers.url_file}" in run
        assert (containers.public / "relay-tools").is_file() and "abc" not in (containers.public / "relay-tools").read_text()
        assert any(a.startswith("BASH_FUNC_relay%%=") for a in run)  # for login shells, which reset PATH
        # The box's own folder, signed: /tools-for answers it about its own agents only
        assert f"RELAY_BOX={project.resolve()}" in run
        assert f"RELAY_BOX_KEY={containers.box_key(project)}" in run
        assert containers.box_key(project) != containers.box_key(tmp_path / "other")
        monkeypatch.delenv("RELAY_TOOLS")  # opencode's shared service: Relay finds it by the folder
        shell(gateway.path, "-c", "true", cwd=project, folder=project)
        run = [c for c in podman() if c[:1] == ["exec"]][-1]
        assert not any(a.startswith(("RELAY_TOOLS=", "RELAY_MCP_URL=")) for a in run)
    finally:
        gateway.stop()


def test_a_commands_cwd_file_comes_back_from_its_boxs_own_folder(tmp_path):
    """Each WorkBox writes the shells' cwd files in an exchange folder of its own; Relay moves the
    one a command named to the shared folder, which the ProviderBox reads."""
    containers = Placed(state=tmp_path / "state")
    own = containers.box_exchange / "relay-abc"
    own.mkdir(parents=True)
    containers.exchange.mkdir(parents=True)
    (own / "12-ab-cwd").write_text("/work/sub\n")
    (own / "other-cwd").write_text("not this one\n")
    containers.hand_back("relay-abc", f"cd sub && pwd -P >| {containers.exchange}/12-ab-cwd")
    assert (containers.exchange / "12-ab-cwd").read_text() == "/work/sub\n" and not (own / "12-ab-cwd").exists()
    for command in (None, "ls", f"pwd -P >| {containers.exchange}/../escape-cwd", "pwd -P >| /tmp/other-cwd"):
        containers.hand_back("relay-abc", command)
    assert sorted(p.name for p in containers.exchange.iterdir()) == ["12-ab-cwd"]
    assert (own / "other-cwd").exists()
    # A link stays where the WorkBox put it: the ProviderBox's shell would follow it, where the login is
    (own / "34-cd-cwd").symlink_to("/home/somewhere/.credentials.json")
    containers.hand_back("relay-abc", f"pwd -P >| {containers.exchange}/34-cd-cwd")
    assert not (containers.exchange / "34-cd-cwd").is_symlink()


def test_a_stopped_command_leaves_only_what_detached_itself(tmp_path):
    """A shell that goes while its command runs (a stop, the run's end): what it started goes too,
    but for a server it detached into a session of its own (setsid -f), for the user to go on using."""
    import time
    import uuid

    from relay.boxes import END
    tag = uuid.uuid4().hex
    started = subprocess.Popen(["bash", "-c", f"sleep 60 & setsid -f sh -c 'echo $$ > {tmp_path}/kept; exec sleep 61'; "
                                "exec sleep 62"], env={**os.environ, "RELAY_EXEC": tag})
    try:
        deadline = time.time() + 10
        while not (tmp_path / "kept").exists() and time.time() < deadline:
            time.sleep(.05)
        kept = int((tmp_path / "kept").read_text())
        subprocess.run(["sh", "-c", END.format(tag=tag)], check=True, timeout=30)
        assert started.wait(10) == -9  # the command itself, and its `&` child with it
        os.kill(kept, 0)  # the detached one is still there
        left = [p.name for p in Path("/proc").iterdir() if p.name.isdigit()
                and f"RELAY_EXEC={tag}".encode() in _environ(p)]
        assert left == [str(kept)]
    finally:
        started.kill()
        subprocess.run(["pkill", "-9", "-s", str(kept)] if "kept" in locals() else ["true"], capture_output=True)


def _environ(proc: Path) -> bytes:
    try:
        return (proc / "environ").read_bytes()
    except OSError:
        return b""


def test_agents_are_told_how_to_leave_a_server_running():
    from relay.tools_script import note
    told = note(Path("/x/relay"))  # (in a WorkBox, where `relay` is on the PATH)
    assert "`relay serve NAME CMD`" in told
    from relay.tools_script import container
    assert "keeps your turn from ending" in container() and "pkill -f" in container()  # (relay help container)
    assert "setsid -f CMD > server.log 2>&1" in note(Path("/x/relay"), on_path=False)  # (no WorkBoxes)


def test_a_command_the_provider_detached_runs_in_a_session_of_its_own(tmp_path, podman):
    """Provider.detached: such a command (Copilot's `detach: true` wrapper) runs under setsid -w in
    the WorkBox, so a stop or the run's end leaves it; its output goes straight to the shell's own
    files (its log, in the scratch folder every box has), as the shell is gone once the run ends."""
    import uuid

    from relay.containers import SCRATCH
    project = tmp_path / "project"
    project.mkdir()
    containers = Placed(state=tmp_path / "state")
    gateway = ExecGateway(tmp_path / "box" / "exec.sock", containers, tmp_path / "commands.log", lambda: [project])
    gateway.detached = lambda command: command.startswith("__wrapper=")
    gateway.start()
    SCRATCH.mkdir(exist_ok=True)
    log = SCRATCH / f"relay-test-{uuid.uuid4().hex}.log"
    try:
        with open(log, "w") as out:
            subprocess.run([sys.executable, str(SHELL), "-c", "__wrapper=1; serve"], cwd=project, stdout=out,
                           stderr=out, env={**os.environ, "RELAY_EXEC_SOCKET": str(gateway.path),
                                            "RELAY_FOLDER": str(project)}, timeout=30)
        run = [c for c in podman() if c[:1] == ["exec"]][-1]
        at = run.index("work-project")
        assert run[at + 1:at + 7] == ["setsid", "-w", "sh", "-c", 'exec "$@" >>"$RELAY_OUT" 2>>"$RELAY_ERR"', "sh"]
        assert f"RELAY_OUT={log}" in run and f"RELAY_ERR={log}" in run
        # Its output not in a file the WorkBox has: still in a session of its own, through the shell
        shell(gateway.path, "-c", "__wrapper=1; serve", cwd=project, folder=project)
        run = [c for c in podman() if c[:1] == ["exec"]][-1]
        at = run.index("work-project")
        assert run[at + 1:at + 4] == ["setsid", "-w", "bash"] and not any(a.startswith("RELAY_OUT=") for a in run)
        # Any other command: as ever
        shell(gateway.path, "-c", "ls", cwd=project, folder=project)
        run = [c for c in podman() if c[:1] == ["exec"]][-1]
        assert "setsid" not in run
    finally:
        gateway.stop()
        log.unlink(missing_ok=True)


def test_copilots_detached_commands_are_known_and_its_temporary_files_shared():
    from relay.containers import SCRATCH
    from relay.providers.acp import CopilotProvider
    from relay.providers.base import Template
    copilot = CopilotProvider(Template("copilot", "Copilot", "copilot", True, {"env": {"COPILOT_HOME": "/x"}}))
    assert copilot.env() == {"TMPDIR": str(SCRATCH), "COPILOT_HOME": "/x"}
    assert copilot.detached("__copilot_pid_path=$1; __copilot_exit_path=$2; …")
    assert not copilot.detached("npm run dev:web")


def test_relay_serve_starts_restarts_lists_and_stops_a_server(tmp_path):
    """`relay serve` in a WorkBox: a server that outlives the turn, by name (no pkill)."""
    import time

    from relay.containers import BIN
    env = {**os.environ, "RELAY_SERVE_DIR": str(tmp_path / "served"), "RELAY_SERVE_WAIT": "0"}

    def relay(*args):
        return subprocess.run([sys.executable, str(BIN / "relay"), "serve", *args], cwd=tmp_path, env=env,
                              capture_output=True, text=True, timeout=30)
    started = relay("web", "echo up; exec sleep 300")
    assert started.returncode == 0 and "web started" in started.stdout and "up" in started.stdout
    pid = int((tmp_path / "served" / "web.pid").read_text())
    assert os.getsid(pid) == pid  # a session of its own: Relay's ending of the turn's commands spares it
    again = relay("web", "--", "sleep", "301")
    assert "web restarted: sleep 301" in again.stdout
    time.sleep(.2)
    assert not os.path.exists(f"/proc/{pid}") or open(f"/proc/{pid}/stat").read().split()[2] == "Z"
    assert "web: running · sleep 301" in relay("--list").stdout
    assert relay("--stop", "web").stdout.strip() == "Stopped web."
    assert "web: stopped" in relay("--list").stdout
    assert relay("--stop", "web").stdout.strip() == "web wasn't running."
    assert relay("bad name!", "x").returncode == 2
    # A server's port, and where the user opens it (through Relay: relay.forward)
    served = relay("site", sys.executable, "-m", "http.server", "--bind", "127.0.0.1", "0")
    try:
        port = re.search(r"opens port (\d+) at /port/\1/", served.stdout)
        assert port, served.stdout
        assert f"site: running · {sys.executable} -m http.server --bind 127.0.0.1 0 · port {port.group(1)} · log" \
            in relay("--list").stdout
    finally:
        relay("--stop", "site")


def test_perplexity_runs_in_a_box_of_the_workbox_image_with_its_cookie_in_a_file(tmp_path, monkeypatch):
    """Perplexity (the user's subscription, no API key) asks the site from a headless browser: its box is
    of the WorkBox image (Chromium), its script and profile folder are in it, and the session cookie
    goes there as a file (0600), not on the command line."""
    import stat
    from types import SimpleNamespace

    from relay import config
    from relay.providers import KINDS
    from relay.providers.base import Context, ProviderError, Template
    from relay.providers.perplexity import provider as perplexity_module
    monkeypatch.setattr(config, "data_dir", lambda: tmp_path / "data")
    perplexity = KINDS["perplexity"](Template("pplx", "Perplexity", "perplexity", True, {"cookie": "secret-token"}))
    assert perplexity.box_key() == "pplx" and perplexity.box_image() == "workbox"
    assert perplexity.has_shell() and perplexity.box_shell() == "bash"  # (that image's shells are its own)
    assert perplexity.has_tools() and perplexity.runs_tools()  # (an agent with Relay's tools; tools=false: it only answers)
    folder = tmp_path / "data" / "perplexity" / "pplx"
    script = Path(perplexity_module.__file__).parent / "perplexity.cjs"  # (in its folder, with its provider)
    assert (script.parent, False) in perplexity.box_mounts() and (folder, True) in perplexity.box_mounts()
    assert perplexity.billing(None) == "subscription"
    context = Context(conversation_id="c1", directory=tmp_path, mode="chat", model="pplx_pro", provider_session=None)
    with pytest.raises(ProviderError, match="box of its own"):
        perplexity.start(context, "hi")
    wrapped = []
    context.box = SimpleNamespace(wrap=lambda argv, cwd, env: wrapped.append((argv, cwd, env)) or ["podman", "exec", *argv])
    run = perplexity.start(context, "Who designed it?")
    argv, cwd, env = wrapped[0]
    assert argv == ["node", str(script)] and cwd == folder
    assert env["PERPLEXITY_PROFILE"] == str(folder) and "secret-token" not in " ".join([*argv, *env.values()])
    assert (folder / "cookie").read_text() == "secret-token"
    assert stat.S_IMODE((folder / "cookie").stat().st_mode) == 0o600
    assert run.question.endswith("My message:\n\nWho designed it?") and "```tool" in run.question
    # A follow-up: the Perplexity thread is the session, and only the new question goes, with a reminder
    context.provider_session = '{"b": "x", "t": "y"}'
    run = perplexity.start(context, "And when?")
    assert run.session == '{"b": "x", "t": "y"}' and run.question.endswith("And when?") and "Actions:" not in run.question
    plain = KINDS["perplexity"](Template("p2", "P", "perplexity", True, {"tools": False}))
    assert not plain.has_tools() and plain.start(context, "And when?").question == "And when?"


# A stand-in for container/perplexity.cjs: ready, then per question an answer (an action first)
FAKE_BRIDGE = """import json, sys
print(json.dumps({"type": "ready", "signed_in": True, "cookie": True}), flush=True)
asked = 0
for line in sys.stdin:
    question = json.loads(line)
    asked += 1
    if asked == 1:
        print(json.dumps({"type": "tool_call", "id": "search-1", "name": "web_search", "input": {"queries": ["x"]}}), flush=True)
        print(json.dumps({"type": "text", "delta": "partial"}), flush=True)
        text = 'Let me look.\\n\\n```tool\\n{"name": "read_file", "arguments": {"path": "notes.txt"}}\\n```'
        print(json.dumps({"type": "answered", "text": text, "sources": [], "searched": "search-1",
                          "session": '{"b": "1"}', "model": "pplx_pro"}), flush=True)
    else:
        seen = "the secret is 42" in question["question"] and question["session"] == '{"b": "1"}'
        print(json.dumps({"type": "answered", "text": "It says 42." if seen else "I didn't get it.",
                          "sources": [{"name": "A", "url": "https://a.example"}], "session": '{"b": "2"}',
                          "model": "pplx_pro"}), flush=True)
"""


def test_perplexity_works_as_an_agent_through_action_blocks(tmp_path, monkeypatch):
    """The site does no function calling: it asks for Relay's tools with ```tool blocks, Relay runs them
    and asks again in the same thread with the results, until it answers without one."""
    from relay.providers.perplexity.provider import _PerplexityRun
    from relay.toolbox import Toolbox
    (tmp_path / "notes.txt").write_text("the secret is 42\n")
    script = tmp_path / "bridge.py"
    script.write_text(FAKE_BRIDGE)
    run = _PerplexityRun([sys.executable, str(script)], "What do the notes say?", "", "pplx_pro",
                         Toolbox(tmp_path, code=True), "subscription")
    events = list(run.events())
    kinds = [(e["type"], e.get("name")) for e in events if e["type"] in ("tool_call", "tool_result")]
    assert kinds == [("tool_call", "web_search"), ("tool_result", "web_search"), ("tool_call", "read_file"),
                     ("tool_result", "read_file"), ], kinds
    text = "".join(e["delta"] for e in events if e["type"] == "text")
    assert "partial" not in text and "```tool" not in text  # (an action round's text, without its blocks)
    assert text.startswith("Let me look.") and "It says 42." in text and "[A](https://a.example)" in text
    assert [e["session"] for e in events if e["type"] == "session"] == ['{"b": "1"}', '{"b": "2"}']
    assert events[-1] == {"type": "done", "status": "succeeded"} and {"type": "step", "agent": None, "model": "pplx_pro"} in events


def test_a_box_of_the_workbox_image_gets_relays_reaper(tmp_path):
    from types import SimpleNamespace

    from relay.containers import CONTAINERFILE
    containers = SimpleNamespace(state=tmp_path / "state", exchange=tmp_path / "exchange", image="localhost/relay-agent:x",
                                 hidden=[], empty=tmp_path / "empty",
                                 check=lambda: None)
    boxes = Boxes(containers)
    argv = boxes._argv("pplx", [], [], image="workbox")
    assert argv[-3:] == ["localhost/relay-agent:x", "sleep", "infinity"]
    assert f"{CONTAINERFILE.parent / 'box'}:/usr/lib/relay:ro" in argv  # (relay-reap ends a run's processes)
    assert boxes._argv("pplx", [], [])[-3] == boxes.image  # (Relay's box image otherwise)


def test_a_test_that_undoes_its_patches_still_isnt_on_the_users_data(tmp_path, monkeypatch):
    """monkeypatch.undo() takes the fixtures' RELAY_HOME with it; what's left is the tests' backstop
    (tests/conftest.py), never the user's own data folder. Nor their ~/projects, the default workspace root:
    a Relay's start puts the scripts' folder there in order (relay.scripts)."""
    from relay import config
    assert config.Settings().roots() == [(tmp_path / "projects").resolve()]
    monkeypatch.undo()
    assert "relay-tests-" in str(config.data_dir()) and str(Path.home() / ".local") not in str(config.data_dir())
    assert "relay-tests-" in str(config.Settings().roots()[0])


def test_a_relay_with_data_of_its_own_doesnt_mount_this_machines_opencode(tmp_path, monkeypatch):
    from opencode_manager import paths
    from relay.providers.base import Template
    from relay.providers.opencode import OpencodeProvider
    provider = OpencodeProvider(Template("opencode", "opencode", "opencode", options={}))
    monkeypatch.setattr(paths, "own_opencode", lambda: True)  # (as for a test, a trial: RELAY_HOME of its own)
    mounts = provider.box_mounts()
    assert (paths.profiles_dir(), True) in mounts and len(mounts) == 2
    assert not [m for m, _ in mounts if "opencode" in m.parts[-1:] and ".config" in m.parts]
    monkeypatch.setattr(paths, "own_opencode", lambda: False)  # (the usual Relay)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    assert (tmp_path / ".local" / "share" / "opencode", True) in provider.box_mounts()


def test_a_test_box_has_only_what_it_is_given_and_a_real_shell(tmp_path, monkeypatch):
    """Boxes.test (relay.repair's tests of a provider's code): a box of its own, ended with its command, with only
    the mounts given (no workspace roots, chats or Relay's tools) and the network; its shell is a real one, as
    there's no Relay to hand commands to."""
    from types import SimpleNamespace
    ran = []
    monkeypatch.setattr("relay.boxes.subprocess.run",
                        lambda argv, **kwargs: ran.append(argv) or SimpleNamespace(returncode=0, stdout="", stderr=""))
    containers = SimpleNamespace(state=tmp_path / "state", exchange=tmp_path / "exchange", image="localhost/relay-agent:x",
                                 hidden=[], empty=tmp_path / "empty", check=lambda: None)
    boxes = Boxes(containers)
    boxes.checked = "ok"
    program, login = tmp_path / "bin" / "prog", tmp_path / "login"
    program.parent.mkdir()
    program.write_text("#!/bin/sh\n")
    login.mkdir()
    boxes.test(["python3", "-c", "print(1)"], [(program, False), (login, True), (tmp_path / "missing", True)],
               {"A": "1"}, tmp_path)
    argv = ran[-1]
    volumes = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    here = boxes.state / "here-shell"
    assert argv[:4] == ["podman", "run", "--rm", "--init"] and "--network=host" in argv and "A=1" in argv
    assert sorted(volumes) == sorted([f"{program}:{program}:ro", f"{login}:{login}",
                                      f"{here}:/usr/lib/relay/relay-shell:ro"])
    assert "bash.real" in here.read_text() and argv[argv.index("-w") + 1] == str(tmp_path)
    assert argv[-4:] == [boxes.image, "python3", "-c", "print(1)"]
    # The WorkBox image's shells are its own
    boxes.test(["true"], [], {}, tmp_path, image="workbox")
    assert ran[-1][-2:] == ["localhost/relay-agent:x", "true"] and not any("relay-shell" in a for a in ran[-1])
