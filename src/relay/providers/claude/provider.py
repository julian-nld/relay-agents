"""Claude Code: `claude -p` with stream-json, through the bridge's ClaudeCodeBackend.

Its support layer is this folder and opencode_manager/bridge/claude_backend.py (which opencode-manager's
scripting uses too).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from opencode_manager import paths
from opencode_manager.bridge.claude_backend import ClaudeCodeBackend
from relay import guard, limits
from relay.providers.base import Context, Field, Provider, ProviderError, ProviderRun, inside, unreadable
from relay.providers.bridge_run import BridgeRun


class ClaudeCodeProvider(Provider):
    KIND = "claude-code"
    FORKS = "end"
    PROGRAMS = ("claude",)
    SIGN_IN = "claude auth login"
    NATIVE_SKILLS = True
    SUPPORT_FILES = ("opencode_manager/bridge/claude_backend.py",)
    RELAY_TOOLS_VIA_MCP = True  # from <dir>/.claude/skills of an --add-dir
    LABEL = "Claude Code"
    DESCRIPTION = (
        "Runs the Claude Code CLI (`claude -p`) with your Claude login. Its commands run "
        "in a container; Relay answers its permission requests itself."
    )
    TOOLS = True
    FIELDS = (
        Field("binary", "string", "Path to the claude executable (default: claude on PATH)."),
        Field("config_dir", "string", "Claude Code's config folder, with its login (default: Relay's own, in "
              "its data folder, once you logged in there with `relay claude`; else your own ~/.claude)."),
        Field("models", "model-list",
              "Models to offer, e.g. [\"sonnet\", \"opus\"]; \"\" is Claude Code's default."),
        Field("thinking_display", "string",
              "How the model's thinking comes along: \"summarized\" (default) or \"omitted\"."),
    )
    EXAMPLE = {
        "id": "claude-code",
        "name": "Claude Code",
        "kind": "claude-code",
        "options": {
            "models": [
                {"id": "", "name": "Default"},
                {"id": "sonnet", "name": "Sonnet"},
                {"id": "opus", "name": "Opus"},
                {"id": "haiku", "name": "Haiku"},
            ]
        },
    }

    @property
    def binary(self) -> str:
        return self.options.get("binary") or paths.program("claude")

    def model_id(self, model: str | None) -> str | None:
        """Claude Code's names are an alias (sonnet, opus, haiku) or a model's full name (claude-sonnet-5-5).
        With a provider in front, as opencode writes its models ("claude-code/sonnet", "anthropic/claude-…"),
        it's the name after it: Claude Code knows no such name, and failed on it for real."""
        prefix, slash, name = str(model or "").partition("/")
        if slash and name and prefix in (self.template.id, self.KIND, "anthropic", "claude"):
            return name
        return model

    @property
    def config_dir(self) -> Path | None:
        """Where Claude Code keeps its login and sessions; None: the user's own ~/.claude."""
        return claude_config_dir(self.options.get("config_dir"))

    def limits(self) -> dict[str, Any] | None:
        """The plan's 5-hour and weekly windows, as Claude Code's /usage asks for them, with its login
        (Claude Code renews that itself; Relay doesn't)."""
        folder = self.config_dir or Path.home() / ".claude"
        try:
            oauth = json.loads((folder / ".credentials.json").read_text())["claudeAiOauth"]
            token, expires = oauth["accessToken"], float(oauth.get("expiresAt") or 0) / 1000
        except (OSError, ValueError, KeyError, TypeError):
            return None  # (not signed in with a plan: an API key has no windows)
        account = f"claude:{folder}"
        if expires < time.time() + 30:
            return {"account": account, "error": "its login has expired: Claude Code renews it at its next run"}
        answer, why = limits.fetch_json(limits.CLAUDE_USAGE, {"Authorization": f"Bearer {token}",
                                                              "anthropic-beta": "oauth-2025-04-20"})
        if answer is None:
            return {"account": account, "error": why}
        return {"account": account, "service": "Claude", "plan": limits.plan_name(oauth.get("subscriptionType")),
                "windows": limits.claude_windows(answer), "source": "provider", "checked": time.time()}

    def env(self) -> dict[str, str]:
        return {"CLAUDE_CONFIG_DIR": str(self.config_dir)} if self.config_dir else {}

    def efforts(self, model: str | None) -> list[str]:
        return list(CLAUDE_EFFORTS)

    def box_key(self) -> str | None:
        return self.template.id

    def box_mounts(self) -> list[tuple[Path, bool]]:
        """Claude Code's program (as it's called: a link brings its target) and its login and
        settings: its config folder, and ~/.claude.json when that's the folder ~/.claude."""
        program = Path(shutil.which(self.binary) or self.binary)
        if self.config_dir:
            return [(program, False), (self.config_dir, True)]
        home = Path.home()
        return [(program, False), (home / ".claude", True), (home / ".claude.json", True)]

    def judge(self, action: str, detail: Any, folder: Path | None,
              reads: list[Path] | None = None) -> tuple[str, str]:
        """Claude Code's permission requests, answered by Relay: its commands run in a
        container (anything goes, but what the guard refuses); its own file tools
        change files only in the project, and read only `reads` when that's given."""
        detail = detail if isinstance(detail, dict) else {}
        if action in ("Bash", "PowerShell"):
            blocked = guard.check(str(detail.get("command") or ""))
            return ("deny", f"Relay blocked this command: {blocked}") if blocked else ("allow", "")
        if action in READING_TOOLS:
            target = detail.get("file_path") or detail.get("notebook_path") or detail.get("path")
            refused = unreadable([str(target)], reads, folder) if target else None
            return ("deny", refused) if refused else ("allow", "")
        if action not in CHANGING_TOOLS:
            return "allow", ""
        target = detail.get("file_path") or detail.get("notebook_path")
        if folder and target and not inside(Path(str(target)), folder):
            return "deny", f"Only files in the project folder ({folder}) can be changed."
        return "allow", ""

    def commands(self, directory: Path) -> list[dict[str, str]]:
        """Claude Code's built-in commands that make sense here, and your own
        (.claude/commands in the project and in Claude Code's config folder)."""
        found = dict(CLAUDE_COMMANDS)
        base = self.config_dir or Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
        for folder in (base / "commands", Path(directory) / ".claude" / "commands"):
            for file in sorted(folder.glob("*.md")) if folder.is_dir() else []:
                found[file.stem] = _first_line(file) or "your own command"
        return [{"name": name, "description": description} for name, description in found.items()]

    def check(self) -> str:
        if shutil.which(self.binary) is None:
            raise ProviderError(f"{self.binary!r} not found")
        result = subprocess.run([self.binary, "--version"], capture_output=True, text=True,
                                timeout=30, env={**os.environ, **self.env()})
        where = f"; login in {self.config_dir}" if self.config_dir else ""
        return (result.stdout.strip() or "Claude Code is installed") + where

    def start(self, context: Context, prompt: str) -> ProviderRun:
        if shutil.which(self.binary) is None:
            raise ProviderError(f"{self.binary!r} not found")
        if context.provider_session and not claude_session_exists(
                self.config_dir, context.directory, context.provider_session):
            # Gone: cleaned up by Claude Code (after 30 days), or the folder moved
            # (sessions are kept per folder). Start afresh, with the conversation handed over.
            context.provider_session = None
        new = context.provider_session is None
        session_id = context.provider_session or str(uuid.uuid4())
        # Claude Code keeps the system prompt a session started with: on --resume (or a
        # fork) a new --append-system-prompt goes unseen. When the mode's instructions or
        # Relay's note changed since, they come with the message instead.
        goes_on = not new or bool(context.fork_from)
        changed = goes_on and (context.tell_tools or context.mode_switched)
        # A fork carries the conversation itself; a fresh session gets it handed over.
        sent = context.agent_prompt(prompt, fresh=new and not context.fork_from, instructions=changed)
        # Attachments and a handed-over transcript live outside the project; let Claude read them.
        outside = {a.path.parent for a in context.attachments}
        if sent.file:
            outside.add(sent.file.parent)
        if context.skills_dir:
            outside.add(context.skills_dir)  # Relay's skills, loaded as Claude Code's own
        mode = context.agent_mode
        # Claude Code's default permission mode, with its requests going to Relay, which
        # answers them itself (judge()): nobody is asked. Without Relay's endpoint (tests,
        # a bare check) its permissions are skipped.
        prompt_tool = "mcp__relay__approve_permission" if context.mcp_url or context.approvals_url else None
        permission_mode = "default" if prompt_tool else None
        backend = ClaudeCodeBackend(
            context.directory,
            resume=not new,
            binary=self.binary,
            model=context.model or None,
            auto_approve=prompt_tool is None,
            add_dirs=sorted(outside),
            fork_from=context.fork_from,
            permission_mode=permission_mode,
            permission_prompt_tool=prompt_tool,
            effort=context.effort if context.effort in CLAUDE_EFFORTS else None,
            thinking_display=self.options.get("thinking_display") or "summarized",
            # Relay's tools come on top of Claude Code's own, via the system prompt.
            system_prompt="\n\n".join(filter(None, [mode.guidance if mode else None,
                                                     context.tools_note])) or None,
            **_relay_tools(context, asks=prompt_tool is not None,
                           # In its box its shell is Relay's anyway; it says which project the run is for.
                           env={**self.env(), **({"RELAY_FOLDER": str(context.directory)} if context.box
                                                 else _shell_env(context)),
                                # As root (a repair's test box: Relay there too, without its approvals) Claude Code
                                # skips its permissions only when told it's in a sandbox
                                **({"IS_SANDBOX": "1"} if prompt_tool is None and os.geteuid() == 0 else {})}),
        )
        if context.box is not None:
            backend.binary = str(Path(shutil.which(self.binary) or self.binary))  # the box has no PATH of ours
            backend.wrap = context.box.wrap
        return BridgeRun(backend, session_id, sent.prompt, new)


CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
# Claude Code's tools that change files (only in the project).
CHANGING_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
READING_TOOLS = {"Read", "Glob", "Grep", "LS", "NotebookRead"}
# Built-in commands that work without a terminal (claude -p).
CLAUDE_COMMANDS = (
    ("init", "write a CLAUDE.md for this project"),
    ("review", "review the changes"),
    ("security-review", "look for security problems in the changes"),
    ("compact", "summarise the conversation so far, to free up context"),
    ("context", "how full the context is"),
)


def _first_line(file: Path) -> str:
    """A command file's description: its frontmatter's, or its first line."""
    try:
        text = file.read_text(errors="replace")
    except OSError:
        return ""
    lines = [line.strip() for line in text.splitlines()]
    if lines[:1] == ["---"]:
        for line in lines[1:]:
            if line == "---":
                break
            if line.startswith("description:"):
                return line.split(":", 1)[1].strip().strip("'\"")[:100]
        lines = lines[lines.index("---", 1) + 1:] if "---" in lines[1:] else []
    return next((line for line in lines if line), "")[:100]


def claude_config_dir(option: str | None) -> Path | None:
    """Claude Code's config folder: the option ("host" for ~/.claude), else Relay's own
    (`relay claude`, in its data folder) once it has a login, else None (~/.claude)."""
    if option == "host":
        return None
    if option:
        return Path(option).expanduser()
    from relay.relocation import claude_dir
    own = claude_dir()
    return own if (own / ".credentials.json").exists() else None


def claude_project_folder(directory: Path) -> str:
    """The name of the folder Claude Code keeps a directory's sessions in."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(directory))


def claude_session_exists(config_dir: Path | None, directory: Path, session_id: str) -> bool:
    """Whether Claude Code still has a session (it keeps them per folder)."""
    base = config_dir or Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    return (base / "projects" / claude_project_folder(directory) / f"{session_id}.jsonl").is_file()


def _shell_env(context: Context) -> dict[str, str]:
    """Claude Code runs its Bash commands with this shell: Relay's, into the container."""
    return {"SHELL": str(context.shell), "CLAUDE_CODE_SHELL": str(context.shell)} if context.shell else {}



def guard_hook() -> dict:
    """Claude Code settings with a hook that refuses disastrous commands (relay.guard),
    even with permissions skipped; next to the user's own hooks."""
    command = f"{shlex.quote(sys.executable)} {shlex.quote(guard.__file__)} --hook"
    return {"hooks": {"PreToolUse": [{"matcher": "Bash|PowerShell",
                                      "hooks": [{"type": "command", "command": command, "timeout": 10}]}]}}


def _relay_tools(context: Context, asks: bool, env: dict[str, str] | None = None) -> dict:
    """What Claude Code needs for Relay's tools, and for asking the user through Relay."""
    # In its box Relay's shell holds the guard: the hook (a file of the machine's) isn't there.
    extra: dict = {"env": dict(env or {}), "settings": {} if context.box is not None else guard_hook()}
    if context.tools_path:
        # Allowed in every mode, without asking: it only talks to Relay. `relay` (container/bin/relay)
        # runs this conversation's script: RELAY_TOOLS says which.
        path = str(context.tools_path)
        extra["allowed_tools"] = sorted({f"Bash({path}:*)", f"Bash({shlex.quote(path)}:*)", "Bash(relay:*)",
                                         "mcp__relay"})
        extra["env"]["RELAY_TOOLS"] = path
    if context.mcp_url or (asks and context.approvals_url):
        # Relay's tools (and its permission prompt), next to Claude Code's own MCP servers.
        extra["mcp_servers"] = {"relay": {"type": "http", "url": context.mcp_url or context.approvals_url}}
        # A subagent's answer or the user's may take a while; by default Claude Code gives up after a minute.
        extra["env"]["MCP_TOOL_TIMEOUT"] = str(35 * 60 * 1000)
    return extra
