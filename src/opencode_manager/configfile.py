"""Locating, validating and saving a profile's opencode.json."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from opencode_manager import paths
from opencode_manager.profiles import Profile

TEMPLATE = """\
{
  "$schema": "https://opencode.ai/config.json"
}
"""


@dataclass(frozen=True)
class ConfigError:
    line: int  # 1-based
    column: int  # 1-based
    message: str

    def __str__(self) -> str:
        return f"Line {self.line}, column {self.column}: {self.message}"


def config_dir(profile: Profile) -> Path:
    if profile.is_default:
        return paths.default_opencode_config_dir()
    return profile.config_dir


def config_file(profile: Profile) -> Path:
    """The profile's opencode config file (opencode.jsonc if that is what exists)."""
    directory = config_dir(profile)
    jsonc = directory / "opencode.jsonc"
    json_file = directory / "opencode.json"
    return jsonc if jsonc.is_file() and not json_file.is_file() else json_file


def read(path: Path) -> str | None:
    try:
        return path.read_text()
    except FileNotFoundError:
        return None


def validate(text: str) -> ConfigError | None:
    """Check that `text` is a JSONC object, as opencode accepts it."""
    try:
        data = json.loads(_strip_jsonc(text))
    except json.JSONDecodeError as exc:
        return ConfigError(exc.lineno, exc.colno, exc.msg)
    if not isinstance(data, dict):
        return ConfigError(1, 1, "The config must be a JSON object ({ ... })")
    return None


def save(path: Path, text: str) -> None:
    """Write `text` atomically, so an interrupted write never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        if path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _strip_jsonc(text: str) -> str:
    """Turn JSONC into JSON: blank out comments and drop trailing commas.

    Removed characters are replaced by spaces (newlines are kept), so error
    positions reported by the JSON parser still match the original text.
    """
    out = list(text)
    i, n = 0, len(text)
    last_comma = None  # index of a comma that may turn out to be trailing
    while i < n:
        ch = text[i]
        if ch == '"':
            last_comma = None
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
            i += 1
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                out[i] = " "
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = n if end == -1 else end + 2
            for j in range(i, end):
                if text[j] != "\n":
                    out[j] = " "
            i = end
        elif ch == ",":
            last_comma = i
            i += 1
        elif ch in "}]":
            if last_comma is not None:
                out[last_comma] = " "
            last_comma = None
            i += 1
        else:
            if not ch.isspace():
                last_comma = None
            i += 1
    return "".join(out)
