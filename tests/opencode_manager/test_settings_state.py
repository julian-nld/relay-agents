import pytest

from opencode_manager import settings, state


def test_settings_defaults_and_unknown_keys_ignored(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"sidebar_width": 40, "bogus": 1}')
    loaded = settings.load(path)
    assert loaded.sidebar_width == 40
    assert loaded.launch_mode == "service"


def test_settings_invalid_mode_rejected():
    with pytest.raises(ValueError):
        settings.Settings(launch_mode="nope")


def test_state_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    assert state.load(path).active_profile == "default"
    state.save(state.State(active_profile="work"), path)
    assert state.load(path).active_profile == "work"
