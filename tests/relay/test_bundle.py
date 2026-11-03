"""The installer bundle (scripts/make-bundle.sh): what git has, its Python dependencies and install.sh, which installs
it and upgrades an install from an earlier bundle. Only ever with --no-service, and with a systemctl that refuses,
so no test touches this machine's own Relay service."""

import io
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def bundle(tmp_path):
    lock = ROOT / "vendor" / ".requirements.lock"
    if not lock.is_file() or lock.read_text() != (ROOT / "requirements.lock").read_text():
        pytest.skip("vendor/ isn't for requirements.lock: the bundle would need pip and the internet")
    if not all(shutil.which(tool) for tool in ("podman", "git", "python3")):
        pytest.skip("install.sh would install system packages")
    # (what's to be committed: the index's tree)
    tree = subprocess.run(["git", "write-tree"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    made = subprocess.run(["sh", "scripts/make-bundle.sh", tree], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "DIST": str(tmp_path / "dist")})
    assert made.returncode == 0, made.stderr
    [archive] = (tmp_path / "dist").glob("relay-*.tar.gz")
    with tarfile.open(archive) as tar:
        tar.extractall(tmp_path / "unpacked", filter="tar")
    [unpacked] = (tmp_path / "unpacked").iterdir()
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "systemctl").write_text('#!/bin/sh\necho "no systemctl in tests" >&2\nexit 1\n')
    (bin_ / "systemctl").chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    env = {"PATH": f"{bin_}:/usr/local/bin:/usr/bin:/bin", "HOME": str(home), "RELAY_HOME": str(tmp_path / "data")}

    def install(folder: Path, *options: str) -> subprocess.CompletedProcess:
        return subprocess.run(["sh", str(unpacked / "install.sh"), "--dir", str(folder), "--no-service", *options],
                              capture_output=True, text=True, env=env)

    return tree, unpacked, install, home


def test_a_bundle_is_what_git_has_with_its_dependencies(bundle):
    tree, unpacked, _, _ = bundle
    tracked = set(subprocess.run(["git", "ls-tree", "-r", "--name-only", tree], cwd=ROOT, capture_output=True,
                                 text=True, check=True).stdout.split("\n")) - {""}
    files = {str(p.relative_to(unpacked)) for p in unpacked.rglob("*") if p.is_file() or p.is_symlink()}
    own = {f for f in files if not f.startswith("vendor/")} - {"install.sh", ".relay-bundle"}
    archive = subprocess.run(["git", "archive", tree], cwd=ROOT, capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        archived = {member.name for member in tar.getmembers() if not member.isdir()}
    # (no one's own provider kinds, local notes or caches, and what .gitattributes leaves out stays out)
    assert own == archived and archived <= tracked
    assert (unpacked / "vendor" / ".requirements.lock").read_text() == (unpacked / "requirements.lock").read_text()
    assert not list(unpacked.rglob("__pycache__"))
    assert (unpacked / ".relay-bundle").read_text().split("\n")[:-1] == sorted(files, key=lambda f: f.encode())
    assert (unpacked / "install.sh").read_text() == (ROOT / "scripts" / "install-bundle.sh").read_text()


def test_install_sh_installs_and_upgrades(bundle, tmp_path):
    _, unpacked, install, home = bundle
    folder = tmp_path / "relay"
    done = install(folder)
    assert done.returncode == 0, done.stderr
    assert f"Installing Relay {version(unpacked)} in {folder}" in done.stdout
    assert subprocess.run([str(folder / "relay"), "--version"], capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", "HOME": str(home)}).stdout.strip() == version(unpacked)
    assert (home / "projects").is_dir()  # (the default workspace root)
    # An upgrade: what the earlier bundle had and this one hasn't goes; what's yours stays
    (folder / "src" / "relay" / "gone.py").write_text("")
    with (folder / ".relay-bundle").open("a") as manifest:
        manifest.write("src/relay/gone.py\n")
    (folder / "src" / "relay" / "providers" / "mine").mkdir()
    (folder / "src" / "relay" / "providers" / "mine" / "__init__.py").write_text("KINDS = {}\n")
    done = install(folder)
    assert done.returncode == 0, done.stderr
    assert f"Upgrading Relay {version(unpacked)} to {version(unpacked)}" in done.stdout
    assert not (folder / "src" / "relay" / "gone.py").exists()
    assert (folder / "src" / "relay" / "providers" / "mine" / "__init__.py").exists()
    assert (folder / ".relay-bundle").read_text() == (unpacked / ".relay-bundle").read_text()


def test_install_sh_leaves_what_isnt_a_bundles(bundle, tmp_path):
    _, _, install, _ = bundle
    (tmp_path / "clone" / ".git").mkdir(parents=True)
    done = install(tmp_path / "clone")
    assert done.returncode == 1 and "is a clone of Relay's repository" in done.stderr
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "notes.txt").write_text("mine")
    done = install(tmp_path / "other")
    assert done.returncode == 1 and "has files of its own" in done.stderr
    assert [p.name for p in (tmp_path / "other").iterdir()] == ["notes.txt"]


def test_install_sh_takes_no_other_folders_service_over(bundle, tmp_path):
    """A Relay that's this user's service already, from another folder (systemd says which), isn't replaced."""
    _, unpacked, _, home = bundle
    bin_ = tmp_path / "bin"
    (bin_ / "systemctl").write_text("#!/bin/sh\ncase \"$*\" in *'cat relay.service'*) "
                                    "printf 'ExecStart=/bin/sh /srv/relay/relay serve\\n'; exit 0 ;; esac\nexit 1\n")
    done = subprocess.run(["sh", str(unpacked / "install.sh"), "--dir", str(tmp_path / "relay")], capture_output=True,
                          text=True, env={"PATH": f"{bin_}:/usr/bin:/bin", "HOME": str(home)})
    assert done.returncode == 1 and "Relay in /srv/relay is this user's service already" in done.stderr
    assert not (tmp_path / "relay").exists()


def version(unpacked: Path) -> str:
    return next(line.split('"')[1] for line in (unpacked / "src/relay/__init__.py").read_text().split("\n")
                if line.startswith("__version__"))
