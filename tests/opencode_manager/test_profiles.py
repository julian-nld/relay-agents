import pytest

from opencode_manager.profiles import DEFAULT_PROFILE, ProfileError, ProfileStore


@pytest.fixture
def store(tmp_path):
    return ProfileStore(tmp_path / "profiles", base_port=50000)


def test_default_profile_always_listed_first(store):
    store.create("zeta")
    store.create("alpha")
    assert [p.name for p in store.list()] == [DEFAULT_PROFILE, "alpha", "zeta"]


def test_default_profile_has_no_overrides(store):
    profile = store.get(DEFAULT_PROFILE)
    assert profile.is_default
    assert profile.env() == {}


def test_create_makes_isolated_dirs_and_env(store):
    profile = store.create("work")
    assert profile.config_dir.is_dir()
    assert profile.data_home.is_dir()
    assert profile.state_home.is_dir()
    assert profile.env() == {
        "OPENCODE_CONFIG_DIR": str(profile.config_dir),
        "XDG_DATA_HOME": str(profile.data_home),
        "XDG_STATE_HOME": str(profile.state_home),
    }


def test_ports_are_unique_and_reused_after_delete(store):
    assert store.create("a").port == 50000
    assert store.create("b").port == 50001
    store.delete("a")
    assert store.create("c").port == 50000
    assert store.get("b").port == 50001


def test_copy_config_skips_service_file(store, tmp_path):
    source = tmp_path / "opencode"
    (source / "agent").mkdir(parents=True)
    (source / "opencode.json").write_text("{}")
    (source / "agent" / "review.md").write_text("x")
    (source / "service.json").write_text('{"password": "secret"}')

    profile = store.create("copy", copy_config_from=source)
    assert (profile.config_dir / "opencode.json").exists()
    assert (profile.config_dir / "agent" / "review.md").exists()
    assert not (profile.config_dir / "service.json").exists()


@pytest.mark.parametrize("name", ["", "Work", "-x", "a b", "../x", "x" * 33, DEFAULT_PROFILE])
def test_invalid_or_reserved_names_rejected(store, name):
    with pytest.raises(ProfileError):
        store.create(name)


def test_duplicate_and_default_delete_rejected(store):
    store.create("work")
    with pytest.raises(ProfileError):
        store.create("work")
    with pytest.raises(ProfileError):
        store.delete(DEFAULT_PROFILE)
    with pytest.raises(ProfileError):
        store.get("missing")


def test_allocation_skips_ports_in_use_on_this_machine(tmp_path):
    taken = {50000, 50002}
    store = ProfileStore(tmp_path / "p", base_port=50000, is_port_free=lambda port: port not in taken)
    assert store.create("a").port == 50001
    assert store.create("b").port == 50003


def test_reassign_port_persists_new_free_port(tmp_path):
    store = ProfileStore(tmp_path / "p", base_port=50000)
    store.create("a")
    store.create("b")
    assert store.reassign_port("a").port == 50002
    assert store.get("a").port == 50002


def test_a_relay_with_data_of_its_own_has_a_default_profile_of_its_own(tmp_path):
    """Not this machine's opencode, whose service another Relay may run (paths.own_opencode)."""
    store = ProfileStore(tmp_path / "profiles", base_port=50000, is_port_free=lambda port: port != 50000,
                         own_default=True)
    profile = store.get(DEFAULT_PROFILE)
    assert not profile.is_default and profile.root == tmp_path / "profiles" / DEFAULT_PROFILE and profile.port == 50001
    assert profile.env() == {
        "OPENCODE_CONFIG_DIR": str(profile.config_dir),
        "XDG_DATA_HOME": str(profile.data_home),
        "XDG_STATE_HOME": str(profile.state_home),
    }
    assert store.get(DEFAULT_PROFILE) == profile  # (made once)
    work = store.create("work")
    assert work.port not in (50000, 50001)
    assert [p.name for p in store.list()] == [DEFAULT_PROFILE, "work"] and store.exists(DEFAULT_PROFILE)
    with pytest.raises(ProfileError):
        store.create(DEFAULT_PROFILE)
    with pytest.raises(ProfileError):
        store.delete(DEFAULT_PROFILE)


def test_which_relay_has_an_opencode_of_its_own(tmp_path, monkeypatch):
    from opencode_manager import paths
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    monkeypatch.delenv("RELAY_HOME", raising=False)
    assert not paths.own_opencode()  # (the usual Relay: this machine's opencode)
    monkeypatch.setenv("RELAY_HOME", str(tmp_path / "share" / "relay"))
    assert not paths.own_opencode()
    monkeypatch.setenv("RELAY_HOME", str(tmp_path / "trial"))
    assert paths.own_opencode()
