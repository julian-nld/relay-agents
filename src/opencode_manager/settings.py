"""User settings, stored in ~/.config/opencode-manager/settings.json."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from pathlib import Path

from opencode_manager import paths
from opencode_manager.jsonfile import read_json, write_json

LAUNCH_MODES = ("service", "standalone")


@dataclass
class Settings:
    # "service": each profile gets its own background opencode service on its
    #   own port, so switching profiles does not kill running work.
    # "standalone": opencode runs with a private server that dies with the pane.
    launch_mode: str = "service"
    sidebar_width: int = 32
    # Empty: `opencode` on PATH. A relative path is resolved against the code folder.
    opencode_bin: str = ""
    # Empty: the built-in editor for opencode.json. Otherwise a command such as
    # "nvim" or "code --wait"; falls back to the built-in one if not installed.
    editor: str = ""
    # Claude Code executable for `stream --backend claude-code`; empty: `claude`.
    claude_bin: str = ""
    base_port: int = 49400
    session_name: str = "opencode-manager"

    def __post_init__(self) -> None:
        if self.launch_mode not in LAUNCH_MODES:
            raise ValueError(
                f"launch_mode must be one of {LAUNCH_MODES}, got {self.launch_mode!r}"
            )


def settings_path() -> Path:
    return paths.data_dir() / "settings.json"


def load(path: Path | None = None) -> Settings:
    raw = read_json(path or settings_path())
    known = {f.name for f in fields(Settings)}
    return Settings(**{k: v for k, v in raw.items() if k in known})


def save(settings: Settings, path: Path | None = None) -> None:
    write_json(path or settings_path(), asdict(settings))
