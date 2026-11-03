from pathlib import Path

from opencode_manager import opencode, paths
from opencode_manager.profiles import Profile, ProfileStore
from opencode_manager.settings import Settings

ROOT = Path(__file__).resolve().parents[2]


def test_data_lives_in_relays_data_folder_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv(paths.HOME_ENV, raising=False)
    monkeypatch.delenv(paths.RELAY_HOME_ENV, raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    assert paths.root() == ROOT
    assert paths.relay_home() == tmp_path / "share" / "relay"
    assert paths.data_dir() == tmp_path / "share" / "relay" / "opencode-manager"
    assert paths.profiles_dir() == paths.data_dir() / "profiles"
    monkeypatch.setenv(paths.RELAY_HOME_ENV, str(tmp_path / "mine"))
    assert paths.data_dir() == tmp_path / "mine" / "opencode-manager"


def test_data_dir_can_be_overridden(monkeypatch, tmp_path):
    monkeypatch.setenv(paths.HOME_ENV, str(tmp_path))
    assert paths.data_dir() == tmp_path


def test_python_path_includes_src_and_vendor():
    assert paths.python_path() == [ROOT / "src", ROOT / "vendor"]


def test_binary_setting_relative_to_the_code_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "root", lambda: tmp_path)
    assert opencode.binary(Settings(opencode_bin="tools/opencode")) == str(tmp_path / "tools/opencode")
    assert opencode.binary(Settings(opencode_bin="/usr/bin/oc")) == "/usr/bin/oc"
    assert opencode.binary(Settings(opencode_bin="oc")) == "oc"


def test_profiles_store_no_absolute_paths(tmp_path):
    """Moving the folder (another mount point) must keep profiles working."""
    store = ProfileStore(tmp_path / "a" / "profiles", base_port=50000)
    store.create("work")
    (tmp_path / "a").rename(tmp_path / "b")

    moved = ProfileStore(tmp_path / "b" / "profiles", base_port=50000).get("work")
    assert moved.port == 50000
    assert moved.env()["XDG_DATA_HOME"] == str(tmp_path / "b" / "profiles" / "work" / "data")


def test_copy_config_follows_symlinks(tmp_path):
    source = tmp_path / "opencode"
    (tmp_path / "real-agents").mkdir()
    (tmp_path / "real-agents" / "a.md").write_text("x")
    source.mkdir()
    (source / "agent").symlink_to(tmp_path / "real-agents")

    profile = ProfileStore(tmp_path / "profiles", 50000).create("p", copy_config_from=source)
    copied = profile.config_dir / "agent"
    assert copied.is_dir() and not copied.is_symlink()
    assert (copied / "a.md").read_text() == "x"


def test_stop_services_skips_default(monkeypatch, tmp_path):
    stopped = []
    monkeypatch.setattr(opencode, "_run", lambda s, p, *args, **kw: stopped.append((p.name, args)))
    profiles = [Profile("default"), Profile("a", tmp_path / "a", 1), Profile("b", tmp_path / "b", 2)]
    opencode.stop_services(Settings(launch_mode="standalone"), profiles)
    assert stopped == [("a", ("service", "stop")), ("b", ("service", "stop"))]


def test_port_is_free_detects_listeners():
    import socket

    from opencode_manager import ports

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        assert ports.is_free(port) is False
    assert ports.is_free(port) is True
