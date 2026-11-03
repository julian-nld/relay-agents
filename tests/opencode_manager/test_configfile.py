import pytest

from opencode_manager import configfile, paths
from opencode_manager.profiles import Profile


def test_valid_json_and_jsonc():
    assert configfile.validate(configfile.TEMPLATE) is None
    jsonc = """{
      // model to use
      "model": "anthropic/claude-sonnet-5", /* inline */
      "url": "http://x.test/a//b",
      "list": [1, 2, 3,],
    }"""
    assert configfile.validate(jsonc) is None


def test_error_position_matches_original_text():
    text = '{\n  // comment\n  "a": 1\n  "b": 2\n}'
    error = configfile.validate(text)
    assert error is not None
    assert (error.line, error.column) == (4, 3)
    assert str(error).startswith("Line 4, column 3:")


@pytest.mark.parametrize("text", ["[]", '"x"', "1", "null"])
def test_top_level_must_be_object(text):
    assert "JSON object" in configfile.validate(text).message


def test_comment_markers_inside_strings_are_kept():
    assert configfile.validate('{"a": "/* not a comment */", "b": "// nor this",}') is None
    assert configfile.validate('{"a": "quote \\" // still string"}') is None


def test_unterminated_block_comment_is_an_error():
    assert configfile.validate('{"a": 1 /* oops') is not None


def test_config_file_location(tmp_path, monkeypatch):
    profile = Profile("work", root=tmp_path, port=1)
    (tmp_path / "config").mkdir()
    assert configfile.config_file(profile) == tmp_path / "config" / "opencode.json"

    (tmp_path / "config" / "opencode.jsonc").write_text("{}")
    assert configfile.config_file(profile) == tmp_path / "config" / "opencode.jsonc"

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "host"))
    assert configfile.config_file(Profile("default")) == tmp_path / "host" / "opencode" / "opencode.json"
    assert paths.default_opencode_config_dir() == tmp_path / "host" / "opencode"


def test_save_is_atomic_and_keeps_permissions(tmp_path):
    path = tmp_path / "new" / "opencode.json"
    configfile.save(path, "{}\n")
    assert path.read_text() == "{}\n"
    path.chmod(0o600)
    configfile.save(path, '{"a": 1}\n')
    assert path.read_text() == '{"a": 1}\n'
    assert path.stat().st_mode & 0o777 == 0o600
    assert [p.name for p in path.parent.iterdir()] == ["opencode.json"]
