"""Relay's locations and settings.

Relay keeps its data in RELAY_HOME (default ~/.local/share/relay): the SQLite
database, provider templates, settings, chat and task folders; opencode-manager's
profiles are in opencode-manager/ there.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from opencode_manager import paths
from opencode_manager.jsonfile import read_json, write_json


def data_dir() -> Path:
    """Relay's data: RELAY_HOME, else ~/.local/share/relay (opencode-manager's is in it)."""
    return paths.relay_home()


def database_path() -> Path:
    return data_dir() / "relay.db"


def worktrees_dir() -> Path:
    """Where code tasks get their own git worktree."""
    return data_dir() / "worktrees"


def providers_dir() -> Path:
    return data_dir() / "providers"


def static_dir() -> Path:
    return Path(__file__).resolve().parent / "static"


def default_workspace_root() -> Path:
    return Path.home() / "projects"


@dataclass
class Settings:
    # All interfaces: Relay is meant for the local network (e.g. on a home server).
    host: str = "0.0.0.0"
    port: int = 8420
    # Directories under which Code-mode conversations may run agents.
    workspace_roots: list[str] = field(default_factory=lambda: [str(default_workspace_root())])
    # Where Chat-mode conversations get a scratch directory each.
    chat_root: str = ""
    # "password": sign in with an account. "none": no login at all, for a
    # trusted local network (everyone who can open Relay can run agents).
    auth: str = "password"
    # Mark the session cookie Secure (set when serving over HTTPS, e.g. behind a proxy).
    secure_cookies: bool = False
    session_days: int = 365  # personal use on a LAN: stay signed in
    # Push notifications when a run finishes (see notify.py).
    ntfy_url: str = ""  # e.g. https://ntfy.sh/some-secret-topic
    ntfy_token: str = ""  # access token for protected ntfy topics
    webhook_url: str = ""  # receives a JSON POST per finished run
    notify_min_seconds: int = 30  # quick answers don't need a push
    notify_vpn: bool = True  # a VPN tunnel's problems (relay.vpn): it fell back, nothing connects
    # Per category (relay.notify.CATEGORIES) what the user chose: {"push": bool, "priority": "high"}
    notify_categories: dict = field(default_factory=dict)
    public_url: str = ""  # Relay's address, for links in notifications
    # Limits and texts that differ from their defaults (relay.tunables; Settings → Limits).
    limits: dict = field(default_factory=dict)
    # The image agents' commands run in (relay.containers); "" is Relay's default.
    container_image: str = ""
    # A VPN for WorkBoxes (relay.vpn): {env: gluetun's settings, folders: [...] or all: true, ports: [...]}
    vpn: dict = field(default_factory=dict)
    # Relay's web search (relay.search): {enabled: true, tunnel: "" or a VPN tunnel's id}
    search: dict = field(default_factory=dict)
    # The Scripts page's folder (relay.scripts); "" is `scripts` in the first workspace root.
    scripts_folder: str = ""
    # The path Relay is served under on its site, e.g. "/relay": behind a proxy that serves it there and passes
    # requests on without that path (a new provider's trial Relay is at /trial/<id> of another). "" at the root.
    base_path: str = ""
    # A new provider's trial Relay (relay.creations): {name, back (the page of the Relay it's tried for)}
    trial: dict = field(default_factory=dict)
    # Plugins the user turned off (relay.plugins; Settings → Plugins): their folders stay, Relay doesn't load them
    plugins_off: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.auth not in ("password", "none"):
            raise ValueError(f"auth must be 'password' or 'none', got {self.auth!r}")
        self.base_path = self.base_path.rstrip("/")
        if self.base_path and not re.fullmatch(r"(/[A-Za-z0-9._~-]+)+", self.base_path):
            raise ValueError(f"base_path must be a path like /relay, got {self.base_path!r}")

    def chat_directory(self) -> Path:
        return Path(self.chat_root).expanduser() if self.chat_root else data_dir() / "chats"

    def roots(self) -> list[Path]:
        """The workspace roots; relative ones are relative to the code folder."""
        found = []
        for root in self.workspace_roots:
            path = Path(root).expanduser()
            found.append((path if path.is_absolute() else paths.root() / path).resolve())
        return found


def settings_path() -> Path:
    return data_dir() / "settings.json"


def load_settings(path: Path | None = None) -> Settings:
    raw = read_json(path or settings_path())
    known = {f.name for f in fields(Settings)}
    return Settings(**{k: v for k, v in raw.items() if k in known})


def save_settings(settings: Settings, path: Path | None = None) -> None:
    write_json(path or settings_path(), asdict(settings))


def save_port(port: int) -> None:
    """The port in settings.json, the rest of it as it is (`relay port N`)."""
    write_json(settings_path(), {**read_json(settings_path()), "port": port})
