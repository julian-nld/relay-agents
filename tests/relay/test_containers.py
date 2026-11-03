"""Agents' containers (relay.containers): the shell that hands commands to podman."""

import json
import os
import shutil
import subprocess
import textwrap
import types
from pathlib import Path

import pytest

from relay.containers import ContainerError, Containers, _project


def test_a_folders_container_and_its_shell(tmp_path, podman):
    project = tmp_path / "project"
    (project / "sub").mkdir(parents=True)
    containers = Containers(state=tmp_path / "state")
    shell = containers.shell(project)
    run = [c for c in podman() if c[:1] == ["run"] and "-d" in c][0]
    name = containers.name(project)
    assert run[run.index("--name") + 1] == name and "--network=host" in run
    assert "--tz=local" in run  # (the machine's time zone: times in it are the user's)
    assert f"{project.resolve()}:{project.resolve()}" in run  # the project, at the same path
    assert "--userns=keep-id" not in run  # root inside: the user outside (rootless), and it may install things

    done = subprocess.run([str(shell), "-c", "pwd; echo $RELAY_EXEC | wc -c"], cwd=project / "sub",
                          capture_output=True, text=True)
    assert done.returncode == 0 and done.stdout.split()[0] == str((project / "sub").resolve())
    exec_call = [c for c in podman() if c[:1] == ["exec"]][-1]
    assert exec_call[exec_call.index("-w") + 1] == str(project / "sub") and name in exec_call
    # Outside the project its commands still run in the project
    elsewhere = subprocess.run([str(shell), "-c", "pwd"], cwd=tmp_path, capture_output=True, text=True)
    assert elsewhere.stdout.strip() == str(project.resolve())
    # A script's session (relay.scripts) goes in by its name only: its value is in no command line
    subprocess.run([str(shell), "-c", "true"], cwd=project, env={**os.environ, "RELAY_TOKEN": "s3cret"}, check=True)
    exec_call = [c for c in podman() if c[:1] == ["exec"]][-1]
    assert exec_call[exec_call.index("RELAY_TOKEN") - 1] == "-e" and not any("s3cret" in a for c in podman() for a in c)
    # Relay's SDK, read-only, for the scripts (and an agent that writes one)
    from relay.sdk import SOURCE
    assert f"{SOURCE.parent}:{SOURCE.parent}:ro" in run


def test_the_private_scripts_are_out_of_every_boxs_sight(tmp_path, podman):
    """An empty, read-only folder in place of the user's private scripts in a box whose project has them; their
    own box (where they run) has them; every WorkBox has the public ones, read-only."""
    root = tmp_path / "projects"
    private, public = root / "scripts" / "private", root / "scripts" / "public"
    private.mkdir(parents=True)
    public.mkdir()
    containers = Containers(state=tmp_path / "state")
    containers.scripts_folders(private, public)

    def volumes(directory):
        containers.ensure(directory)
        run = [c for c in podman() if c[:1] == ["run"] and containers.name(directory) in c][-1]
        return [run[i + 1] for i, a in enumerate(run) if a == "-v"]

    whole = volumes(root)  # (a code task on the whole workspace root)
    assert f"{root}:{root}" in whole and f"{containers.empty}:{private}:ro" in whole
    own = volumes(private)  # (the private scripts' own WorkBox)
    assert f"{private}:{private}" in own and not any(v.startswith(f"{containers.empty}:") for v in own)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    other = volumes(elsewhere)  # (a project with neither: the public ones, read-only, and no mask needed)
    assert f"{public}:{public}:ro" in other and not any(v.startswith(f"{containers.empty}:") for v in other)
    # Scripts not there yet (made at the Scripts page's first use): no mask, or podman would make the folder
    containers.scripts_folders(root / "later" / "private", root / "later" / "public")
    assert not any(v.startswith(f"{containers.empty}:") for v in volumes(root))


def test_the_shell_refuses_what_the_guard_refuses(tmp_path, podman):
    project = tmp_path / "project"
    project.mkdir()
    shell = Containers(state=tmp_path / "state").shell(project)
    before = len(podman())
    for args in (["-c", "rm -rf /"], ["-c", "-l", "rm -rf /"], ["-lc", "rm -rf ~"]):  # Claude Code: -c -l
        refused = subprocess.run([str(shell), *args], cwd=project, capture_output=True, text=True)
        assert refused.returncode == 126 and "Relay refused this command" in refused.stderr
    assert len(podman()) == before  # never reached the container
    assert subprocess.run([str(shell), "-c", "exit 3"], cwd=project).returncode == 3


def test_claude_codes_folder_file_comes_back(tmp_path, podman):
    project = tmp_path / "project"
    (project / "sub").mkdir(parents=True)
    shell = Containers(state=tmp_path / "state").shell(project)
    target = f"/tmp/claude-{os.urandom(4).hex()}-cwd"
    try:
        subprocess.run([str(shell), "-c", "-l", f"cd sub && pwd -P >| {target}"], cwd=project, check=True)
        with open(target) as found:
            assert found.read().strip() == str((project / "sub").resolve())
    finally:
        if os.path.exists(target):
            os.unlink(target)


@pytest.mark.parametrize("leave", ["ln -s {secret}", "mkfifo", "mkdir"])
def test_the_shell_here_takes_only_a_plain_file_from_the_box(tmp_path, podman, leave):
    """The folder file comes back through what the box wrote, and an agent is root there: a link would be followed on
    this machine, a pipe would hang the shell for ever."""
    project, secret = tmp_path / "project", tmp_path / "secret"
    project.mkdir()
    secret.write_text("a file of the machine")
    containers = Containers(state=tmp_path / "state")
    shell = containers.shell(project)
    own = containers.box_exchange / containers.name(project)
    target = f"/tmp/claude-{os.urandom(4).hex()}-cwd"
    # (its tag is in the command as the shell rewrote it)
    command = (f'tag=$(echo "$BASH_EXECUTION_STRING" | grep -o "[0-9a-f]*-cwd$"); {leave.format(secret=secret)} "{own}/$tag"; '
               f"pwd -P >| {target}")
    try:
        done = subprocess.run([str(shell), "-c", command], cwd=project, capture_output=True, text=True, timeout=30)
        assert done.returncode == 0, done.stderr
        assert not os.path.exists(target) or "a file of the machine" not in open(target).read()
        assert secret.read_text() == "a file of the machine"
    finally:
        if os.path.exists(target):
            os.unlink(target)


def test_the_shared_shell_goes_by_folder(tmp_path, podman):
    project, mine = tmp_path / "project", tmp_path / "mine"
    project.mkdir()
    mine.mkdir()
    containers = Containers(state=tmp_path / "state")
    containers.route(project)
    shell = containers.shared_shell()
    subprocess.run([str(shell), "-c", "true"], cwd=project, check=True)
    assert [c for c in podman() if c[:1] == ["exec"]]
    count = len(podman())
    # A folder Relay doesn't know (the user's own use of that opencode): on the machine
    assert subprocess.run([str(shell), "-c", "echo here"], cwd=mine, capture_output=True,
                          text=True).stdout == "here\n"
    assert len(podman()) == count


def test_a_folder_in_a_repository_gets_the_repository(tmp_path, podman):
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    containers = Containers(state=tmp_path / "state")
    containers.ensure(repo / "sub")
    run = [c for c in podman() if c[:1] == ["run"] and "-d" in c][-1]
    # git looks above the folder: the repository is there, but only the folder and .git can change
    assert f"{repo.resolve()}:{repo.resolve()}:ro" in run
    assert f"{(repo / 'sub').resolve()}:{(repo / 'sub').resolve()}" in run
    assert f"{(repo / '.git').resolve()}:{(repo / '.git').resolve()}" in run
    assert run[run.index("-w") + 1] == str((repo / "sub").resolve())
    # Containers in it: /proc unmasked and a volume for their images; no more capabilities than a box has
    key = containers.name(repo / "sub")[6:]
    assert "unmask=ALL" in run and f"relay-containers-{key}:/var/lib/containers" in run
    assert not [a for a in run if "privileged" in a or "cap-add" in a or "SYS_ADMIN" in a]


def _repository(folder, *git_args):
    folder.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(folder), *git_args], check=True)
    subprocess.run(["git", "-C", str(folder), "-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-q",
                    "--allow-empty", "-m", "first"], check=True)
    return folder.resolve()


def test_what_a_repository_really_is_gets_its_history(tmp_path):
    """A linked worktree and a submodule keep their history outside the folder, and need it."""
    main = _repository(tmp_path / "main")
    linked = (tmp_path / "linked").resolve()
    subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", str(linked)], check=True)
    assert _project(linked) == [(linked, True), (main / ".git", True)]
    (linked / "sub").mkdir()
    assert _project(linked / "sub") == [(linked, False), (linked / "sub", True), (main / ".git", True)]
    # (as newer git writes it: relative to the worktree's folder there)
    back = main / ".git" / "worktrees" / "linked" / "gitdir"
    back.write_text(os.path.relpath(linked / ".git", back.parent) + "\n")
    assert _project(linked) == [(linked, True), (main / ".git", True)]
    (main / "sub").mkdir()
    assert _project(main) == [(main, True)]
    assert _project(main / "sub") == [(main, False), (main / "sub", True), (main / ".git", True)]
    outer = _repository(tmp_path / "outer")
    subprocess.run(["git", "-C", str(outer), "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(main),
                    "inner"], check=True, capture_output=True)
    assert _project(outer / "inner") == [(outer / "inner", True), (outer / ".git" / "modules" / "inner", True)]
    # --separate-git-dir leaves nothing that names the folder back: its history can't be told from a made-up one
    separate = _repository(tmp_path / "separate", f"--separate-git-dir={tmp_path / 'apart.git'}")
    assert _project(separate) == [(separate, True)]


def test_what_an_agent_writes_in_git_gets_it_no_other_folder(tmp_path):
    """It's root in the WorkBox and its .git is writable there: none of this may bring a folder of the machine into its
    next command's WorkBox, read-only or not."""
    secret = (tmp_path / "secret").resolve()
    secret.mkdir()
    other = _repository(tmp_path / "other")
    other_linked = (tmp_path / "other-linked").resolve()
    subprocess.run(["git", "-C", str(other), "worktree", "add", "-q", str(other_linked)], check=True)
    alone = lambda folder: [(folder, True)]  # noqa: E731

    # core.worktree, far away or just above
    far = _repository(tmp_path / "far")
    subprocess.run(["git", "-C", str(far), "config", "core.worktree", str(secret)], check=True)
    assert _project(far) == alone(far)
    above = _repository(tmp_path / "wide" / "near")
    subprocess.run(["git", "-C", str(above), "config", "core.worktree", str(above.parent)], check=True)
    assert _project(above) == alone(above)
    # A repository inside another, whose own settings name the outer one
    outer = _repository(tmp_path / "outer")
    nested = _repository(outer / "nested")
    subprocess.run(["git", "-C", str(nested), "config", "core.worktree", str(outer)], check=True)
    assert _project(nested) == alone(nested)

    def forged(name, place, **files):
        folder = (tmp_path / name).resolve()
        folder.mkdir()
        (folder / ".git").write_text(f"gitdir: {place}\n")
        for file, text in files.items():
            where = folder / ".fake" / file
            where.parent.mkdir(exist_ok=True)
            where.write_text(text)
        return folder

    # A .git file: another repository's .git, or one of its worktrees (which names its own folder back)
    assert _project(made := forged("a", other / ".git")) == alone(made)
    assert _project(made := forged("b", other / ".git" / "worktrees" / "other-linked")) == alone(made)
    # A made-up one in its own folder, with its `commondir` on another repository or this one's on a real one
    assert _project(made := forged("c", ".fake", commondir=str(other / ".git"), gitdir=".git")) == alone(made)
    assert _project(made := forged("d", ".fake", config=f"[core]\n\tworktree = {secret}\n")) == alone(made)
    # Another repository's whose settings name its own work folder (a submodule's), not this one
    named = _repository(tmp_path / "named")
    subprocess.run(["git", "-C", str(named), "config", "core.worktree", str(secret)], check=True)
    assert _project(made := forged("f", named / ".git")) == alone(made)
    # Made up outside its folder, where it can write too (the scratch folder), naming this folder back all right
    made = forged("e", tmp_path / "scratch" / "fake")
    (tmp_path / "scratch" / "fake").mkdir(parents=True)
    (tmp_path / "scratch" / "fake" / "commondir").write_text(str(other / ".git") + "\n")
    (tmp_path / "scratch" / "fake" / "gitdir").write_text(f"{made}/.git\n")
    assert _project(made) == alone(made)
    # Itself as its gitdir, where its folder's place would make <common>/worktrees/<it> of Relay's own worktrees
    place = tmp_path / "data" / "worktrees"
    place.mkdir(parents=True)
    itself = (place / "task").resolve()
    itself.mkdir()
    (itself / ".git").write_text("gitdir: .\n")
    (itself / "commondir").write_text("../..\n")
    (itself / "gitdir").write_text(".git\n")
    assert _project(itself) == alone(itself)
    # Inside a real .git folder a `commondir` counts for nothing
    real = _repository(tmp_path / "plain")
    (real / ".git" / "commondir").write_text(str(other / ".git") + "\n")
    assert _project(real) == alone(real)

    # A link for .git, a pipe (which mustn't hang Relay), a loop, and junk: not a repository at all
    (link := (tmp_path / "link").resolve()).mkdir()
    (link / ".git").symlink_to(other / ".git")
    assert _project(link) == alone(link)
    (pipe := (tmp_path / "pipe").resolve()).mkdir()
    os.mkfifo(pipe / ".git")
    assert _project(pipe) == alone(pipe)
    assert _project(made := forged("loop", "cycle")) == alone(made)
    (made / "cycle").symlink_to(made / "cycle")
    assert _project(made) == alone(made)
    assert _project(made := forged("nul", "a\0b")) == alone(made)
    (junk := (tmp_path / "junk").resolve()).mkdir()
    (junk / ".git").write_text("not one\n")
    assert _project(junk) == alone(junk)
    (empty := (tmp_path / "empty").resolve()).mkdir()
    (empty / ".git").mkdir()
    assert _project(empty) == alone(empty)


def test_a_repository_at_home_or_above_gives_the_folder_alone(tmp_path, monkeypatch):
    home = _repository(tmp_path / "home")
    monkeypatch.setattr(Path, "home", lambda: home)
    (home / "projects").mkdir()
    assert _project(home / "projects") == [(home / "projects", True)]


def test_unused_containers_are_stopped_then_removed(tmp_path, podman, monkeypatch):
    import time
    containers = Containers(state=tmp_path / "state")
    containers.route(tmp_path)
    name = containers.name(tmp_path)
    from relay.containers import instance
    mark = instance()
    # Another Relay's on the same podman (a test run's, the user's own), idle and long stopped: its own
    others = f"relay-0000other000 running 0|feedface\nrelay-0000gone0000 exited {int(time.time()) - 30 * 86400}|feedface\n"
    listing = {"rows": f"{name} running 0|{mark}\n" + others}
    real = subprocess.run

    def run(argv, **kwargs):
        if argv[:2] == ["podman", "ps"]:
            return subprocess.CompletedProcess(argv, 0, listing["rows"], "")
        return real(argv, **kwargs)
    monkeypatch.setattr(subprocess, "run", run)
    assert containers.stop_idle(3) == 0  # just used
    # …also after a restart of Relay (a new Containers, nothing in memory): a command ran in it
    containers.ensure(tmp_path)
    assert Containers(state=tmp_path / "state").stop_idle(3) == 0
    containers.used[name] -= 4 * 3600
    marker = tmp_path / "state" / "used" / name
    os.utime(marker, (marker.stat().st_atime, time.time() - 4 * 3600))
    assert containers.stop_idle(3) == 1
    assert ["stop", "-t", "5", name] in podman()  # stopped, with what was installed in it
    assert json.loads((tmp_path / "state" / "routes.json").read_text()) == {}
    listing["rows"] = f"{name} exited {int(time.time()) - 15 * 86400}|{mark}\n" + others
    containers.stop_idle(3)
    assert ["rm", "-f", name] in podman()
    assert ["volume", "rm", "-f", f"relay-home-{name[6:]}", f"relay-containers-{name[6:]}"] in podman()
    assert not [c for c in podman() if "relay-0000other000" in c or "relay-0000gone0000" in c]


def test_relays_image_is_built_once(tmp_path, podman, monkeypatch):
    from relay.containers import CONTAINERFILE, built_image
    monkeypatch.setenv("FAKE_NO_IMAGE", "1")
    containers = Containers(state=tmp_path / "state")
    assert containers.image == built_image() and containers.image.startswith("localhost/relay-agent:")
    containers.check()
    build = [c for c in podman() if c[:1] == ["build"]]
    assert build and build[0][build[0].index("-t") + 1] == containers.image and str(CONTAINERFILE) in build[0]
    assert "nodejs" in CONTAINERFILE.read_text()
    # An image of your own is used as it is
    assert not Containers(image="docker.io/library/debian:trixie", state=tmp_path / "s2").build


def test_without_podman_agents_dont_run(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(ContainerError, match="podman isn't installed"):
        Containers(state=tmp_path / "state").check()


def test_the_governor_looks_in_an_agents_folder_read_only(tmp_path, podman):
    """workbox.look (relay.agentos): a box of its own, no network, the folder and the WorkBox's home read-only."""
    project = tmp_path / "project"
    project.mkdir()
    containers = Containers(state=tmp_path / "state")
    containers.look(project, "git diff")
    run = [c for c in podman() if c[:1] == ["run"]][-1]
    assert "--rm" in run and "--network=none" in run and run[-3:] == ["bash", "-lc", "git diff"]
    assert f"{project.resolve()}:{project.resolve()}:ro" in run
    assert f"relay-home-{containers.name(project)[6:]}:/home/relay:ro" in run
    assert run[run.index("-w") + 1] == str(project.resolve())


def _podman_works():
    if shutil.which("podman") is None:
        return False
    try:
        return subprocess.run(["podman", "image", "exists", "docker.io/library/python:3.13-trixie"],
                              capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.mark.skipif(not _podman_works(), reason="no podman with Relay's image here")
def test_with_real_podman(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    containers = Containers(image="docker.io/library/python:3.13-trixie", state=tmp_path / "state")
    containers.set_url("http://127.0.0.1:1")  # (as Relay does at its start)
    try:
        shell = containers.shell(project)
        done = subprocess.run([str(shell), "-c", "echo $HOME; touch made.txt; grep -c trixie /etc/os-release"],
                              cwd=project, capture_output=True, text=True, timeout=120)
        assert done.returncode == 0, done.stderr
        assert done.stdout.split()[0] == "/home/relay"
        assert (project / "made.txt").stat().st_uid == os.getuid()  # what it writes stays the user's
        # Claude Code's cwd file comes back, through the box's own exchange folder
        target = f"/tmp/claude-{os.urandom(4).hex()}-cwd"
        try:
            subprocess.run([str(shell), "-c", "-l", f"pwd -P >| {target}"], cwd=project, check=True, timeout=120)
            assert open(target).read().strip() == str(project.resolve())
        finally:
            if os.path.exists(target):
                os.unlink(target)
        # …which is the box's own: what it writes there no other box (or the ProviderBoxes) sees
        name = containers.name(project)
        subprocess.run(["podman", "exec", name, "touch", str(containers.exchange / "planted")], check=True)
        assert (containers.box_exchange / name / "planted").exists() and not (containers.exchange / "planted").exists()
        # A command finds Relay's address and the generic tools script (read-only)
        seen = subprocess.run([str(shell), "-c", f"cat {containers.url_file}; ls {containers.public}; "
                               f"touch {containers.public}/x 2>&1"], cwd=project, capture_output=True,
                              text=True, timeout=120)
        assert "http://127.0.0.1:1" in seen.stdout and "relay-tools" in seen.stdout and "Read-only" in seen.stdout
        # The Governor's look: it reads the folder, and can't change it
        code, output = containers.look(project, "ls; echo hi > other.txt")
        assert code != 0 and "made.txt" in output and "Read-only file system" in output
        assert not (project / "other.txt").exists()
    finally:
        subprocess.run(["podman", "rm", "-f", "-t", "1", containers.name(project)], capture_output=True)
        subprocess.run(["podman", "volume", "rm", "-f", f"relay-home-{containers.name(project)[6:]}",
                        f"relay-containers-{containers.name(project)[6:]}"], capture_output=True)


def _relays_image():
    from relay.containers import built_image
    try:
        return shutil.which("podman") and subprocess.run(["podman", "image", "exists", built_image()],
                                                         capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.mark.skipif(not _relays_image(), reason="Relay's image isn't built here (it takes a while: not in a test)")
def test_containers_in_a_workbox_and_what_stays_read_only(tmp_path):
    """podman (and docker) in a WorkBox: a container runs, its image kept in the box's volume; with no
    more capabilities than the box, what's read-only there (the rest of a repository) stays so."""
    from relay.containers import built_image
    repo = tmp_path / "repo"
    (repo / "site").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    containers = Containers(image=built_image(), state=tmp_path / "state")
    containers.set_url("http://127.0.0.1:1")
    folder = repo / "site"
    try:
        shell = containers.shell(folder)

        def run(command, timeout=300):
            return subprocess.run([str(shell), "-c", command], cwd=folder, capture_output=True, text=True,
                                  timeout=timeout)
        pulled = run("podman pull -q docker.io/library/alpine:3")
        if pulled.returncode != 0 and "registry" in pulled.stderr.lower():
            pytest.skip(f"no registry reachable: {pulled.stderr.strip()[-200:]}")
        nested = run("docker run --rm -v $PWD:/app alpine:3 sh -c 'echo nested > /app/made.txt; cat /app/made.txt'")
        assert nested.returncode == 0 and nested.stdout.strip() == "nested", nested.stderr
        assert (folder / "made.txt").stat().st_uid == os.getuid()  # (written as the box's root: the user's)
        node = run("node --version")
        assert node.stdout.startswith("v22."), node.stdout
        # compose: a service that waits for another's health check (podman's timers need systemd, not here)
        (folder / "compose.yml").write_text(textwrap.dedent("""\
            services:
              db:
                image: docker.io/library/alpine:3
                command: sh -c "sleep 2; touch /tmp/ready; sleep 300"
                healthcheck: {test: ["CMD", "test", "-f", "/tmp/ready"], interval: 1s}
              app:
                image: docker.io/library/alpine:3
                command: sleep 300
                depends_on: {db: {condition: service_healthy}}
            """))
        up = run("docker compose up -d && podman ps --format '{{.Names}} {{.Status}}'", timeout=120)
        assert up.returncode == 0 and "app" in up.stdout and "(healthy)" in up.stdout, up.stdout + up.stderr
        run("docker compose down -t 1", timeout=120)
        # git commits with no one configured: as Relay
        made = run("git init -q made && cd made && git commit -q --allow-empty -m first && git log --format=%an")
        assert made.stdout.strip() == "Relay", made.stdout + made.stderr
        locked = run(f"mount -o remount,rw,bind {repo} 2>&1; touch {repo}/escaped 2>&1")
        assert not (repo / "escaped").exists(), locked.stdout
    finally:
        name = containers.name(folder)
        subprocess.run(["podman", "rm", "-f", "-t", "1", name], capture_output=True)
        subprocess.run(["podman", "volume", "rm", "-f", f"relay-home-{name[6:]}", f"relay-containers-{name[6:]}"],
                       capture_output=True)


def test_each_relay_marks_its_boxes(tmp_path, monkeypatch):
    """The usual data folder marks nothing (its boxes had no mark before); another one (a test
    run's, a second install) marks its boxes, and its ProviderBoxes' names, with its own."""
    from relay.boxes import Boxes
    from relay.containers import instance, mine
    monkeypatch.delenv("RELAY_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert instance() == "" and mine("") and not mine("feedface")
    assert Boxes.name(None, "opencode-manager") == "relay-box-opencode-manager"
    monkeypatch.setenv("RELAY_HOME", str(tmp_path / "elsewhere"))
    mark = instance()
    assert len(mark) == 8 and mine(mark) and not mine("") and not mine("feedface")
    assert Boxes.name(None, "opencode-manager") == f"relay-box-opencode-manager-{mark}"


def test_a_box_cant_name_a_file_of_the_machine_to_be_copied_in(tmp_path, monkeypatch):
    """The box says which attachments it lacks, and an agent is root in it, so its `sh` may say anything."""
    secret, attached = tmp_path / "secret", tmp_path / "attached"
    secret.write_text("a file of the machine")
    attached.write_text("an attachment")
    copied = {}

    def run(argv, stdin=None, **_):
        if stdin is None:
            return types.SimpleNamespace(stdout=f"{secret}\n{attached}\n{tmp_path / 'gone'}\n")
        copied[argv[-1]] = stdin.read()
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr("relay.containers.subprocess.run", run)
    Containers.provide(types.SimpleNamespace(ensure=lambda directory: "box"), tmp_path, [attached])
    assert copied == {str(attached): b"an attachment"}
