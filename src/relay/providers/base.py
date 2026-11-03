"""The provider interface.

A provider is configured by a template: a small JSON document with an id,
a display name, a kind and kind-specific options. Each kind is a Provider
subclass that documents its options (FIELDS) and ships an EXAMPLE, which
the web UI offers when adding a provider.

Providers emit the bridge's uniform events (opencode_manager.bridge.events):
the first event of a run is {"type": "session", "session": ...} naming the
provider's own session (so the next turn can continue it), and the last is
{"type": "done", "status": ...}.

Relay adds optional `reasoning_ms` to events during a reasoning block, including
the event that ends it: elapsed milliseconds since its first non-empty delta.
It is measured once by the core and stored for the same live/replay presentation.
"""

from __future__ import annotations

import re
import shutil
import sys
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from opencode_manager.bridge.billing import FREE
from relay import guard, handover
from relay.handover import Handover
from relay.modes import instructions_prompt

Event = dict[str, Any]

ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


class TemplateError(Exception):
    """A provider template is invalid."""


class ProviderError(Exception):
    """A provider could not do what was asked (not installed, unreachable, …)."""


# Options providers no longer have, left alone in older templates (auto_approve: before
# agents worked in containers, relay.containers).
RETIRED_OPTIONS = {"auto_approve"}


@dataclass(frozen=True)
class Field:
    name: str
    type: str  # "string" | "boolean" | "string-list" | "model-list" | "object"
    help: str
    required: bool = False
    secret: bool = False


@dataclass
class Template:
    id: str
    name: str
    kind: str
    enabled: bool = True
    options: dict[str, Any] = field(default_factory=dict)
    managed: bool = False  # added by AgentOS's Governor (relay.agentos provider.add), which may remove it
    home: str = ""  # its own home in its box, where its program is installed and logged in (Provider.box_home)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "kind": self.kind, "enabled": self.enabled,
                "options": self.options, **({"managed": True} if self.managed else {}),
                **({"home": self.home} if self.home else {})}


# Where programs installed in a provider's own home are (npm's, pip's and most installers'), first on its PATH
HOME_BINS = (".local/bin", "bin")


@dataclass
class Context:
    """What a provider needs to know about the conversation it runs for."""

    conversation_id: str
    directory: Path
    mode: str  # "chat" | "code"
    model: str | None
    provider_session: str | None
    # Previous turns, oldest first: {"role", "content", "attachments", "tools"}.
    # Chat APIs get them every time; agents only when they start a fresh
    # session in a conversation that already has messages (a handover).
    history: list[dict[str, Any]] = field(default_factory=list)
    # Files attached to this message (relay.uploads.Attachment).
    attachments: list = field(default_factory=list)
    # A fork: continue from a copy of this provider session, up to the
    # provider's message `fork_before` if given (opencode only).
    fork_from: str | None = None
    fork_before: str | None = None
    # How the agent works (relay.modes.Mode); None is the default, Build.
    agent_mode: Any = None
    # The agent's commands run in a container (relay.containers): `shell` is a $SHELL that
    # sends them to this run's (the folder's, read-only in a read-only mode); `shared_shell`
    # one for a server that serves many folders (opencode), which finds the container by
    # the command's folder.
    shell: Path | None = None
    shared_shell: Path | None = None
    # How to start a browser in that container (Relay's, for opencode's browser tools).
    browser_argv: list[str] | None = None
    browser_log: Path | None = None
    # The provider's own program runs in its ProviderBox (relay.boxes): BoxLaunch.wrap(argv, cwd, env).
    box: Any = None
    # The mode of the previous turn when it was another (relay.modes.Mode, None for
    # Build): a session that goes on has that one's instructions in it still.
    mode_before: Any = None
    mode_switched: bool = False
    # How hard the model thinks: low, medium, high, xhigh or max; None: its default.
    effort: str | None = None
    # Relay's MCP endpoint for this run (relay.delegation): Claude Code's
    # permission prompts go there.
    mcp_url: str | None = None
    # Where the agent's permission prompts go when it has no Relay tools (a subagent):
    # approvals only.
    approvals_url: str | None = None
    # What its program's own file tools may read (relay.agentos.read_boundary), a function asked
    # each time (reads()): AgentOS's Governor and the agents it manages, their own things only;
    # None: anywhere (the user's own agents).
    read_boundary: Any = None
    # Relay's own tools (relay.delegation): the script, what to tell the
    # agent about it, and whether this session still has to be told.
    tools_path: Path | None = None
    tools_note: str | None = None
    tell_tools: bool = False
    # For agents that load skills themselves: a folder with .claude/skills (relay.skills).
    skills_dir: Path | None = None
    # The user's own computers that agents can work on (relay.computers), and in a
    # computer task the one the work is on.
    computers: list[str] = field(default_factory=list)
    computer: str | None = None
    # Relay's web search (relay.search): a search's arguments ({query, category, time_range,
    # language, page}) to what it found, as text; raises relay.search.SearchError. None: off.
    search: Any = None
    # Relay's web fetch (relay.fetch): {url, offset} to the page as text; raises relay.fetch.FetchError.
    fetch: Any = None
    # Relay's memory (relay.memory) for this conversation, and how to add to it: agents get it in Relay's
    # note (tools_note), chat models in their system prompt. None: the user turned it off, or a subagent.
    memory: str | None = None
    # Relay's subagent tools (delegate, wait, tell, its agents): off when the user turned them off for this
    # conversation (relay.relay_tools); a chat model's toolbox leaves them out then.
    subagents: bool = True

    def reads(self) -> list[Path] | None:
        return self.read_boundary() if callable(self.read_boundary) else self.read_boundary

    def prompt_with_attachments(self, prompt: str) -> str:
        """The prompt plus a list of the attached files, for agents that read
        files themselves."""
        if not self.attachments:
            return prompt
        lines = [f"- {a.path} ({a.name}, {a.mime})" for a in self.attachments]
        return (f"{prompt}\n\nAttached files (read them with your tools):\n" + "\n".join(lines))


    def mode_note(self) -> str:
        """For a session that goes on in another mode than the previous turn."""
        now = self.agent_mode.name if self.agent_mode else "Build"
        before = self.mode_before.name if self.mode_before else "Build"
        return (f"(I switched from the {before} mode to the {now} mode: the {before} mode's instructions from "
                f"earlier in this conversation no longer apply; work in the {now} mode now.)")

    def agent_prompt(self, prompt: str, fresh: bool, instructions: bool = False) -> Handover:
        """What an agent is sent: the prompt with its attachments, the mode's
        instructions if asked (for agents without a system prompt) and, in a
        fresh session of a conversation that has messages already, the
        conversation so far."""
        text = self.prompt_with_attachments(prompt)
        if prompt.startswith("/"):
            return Handover(text)  # the agent's own slash command: it must come first, as typed
        if self.mode_switched and not fresh:
            text = f"{self.mode_note()}\n\n{text}"
        if instructions:
            text = instructions_prompt(self.agent_mode, text)
            # A fresh session never heard of Relay's tools, whatever was told before.
            if (self.tell_tools or fresh) and self.tools_note:
                text = f"A note before my message:\n\n{self.tools_note}\n\n---\n\n{text}"
        return handover.prepare(self.conversation_id, self.history, text) if fresh else Handover(text)


class ProviderRun(ABC):
    @abstractmethod
    def events(self) -> Iterator[Event]:
        """Uniform events until (and including) the final "done" event."""

    @abstractmethod
    def interrupt(self) -> None:
        """Stop the run as soon as possible; may be called from another thread."""

    def waits_for_answers(self) -> bool:
        """Whether a permission event without a decision waits for the user,
        who answers with answer_permission(). Otherwise such events are only
        shown (e.g. Claude Code asks through Relay's MCP tool instead)."""
        return False

    def answer_permission(self, request_id: str, approve: bool) -> None:
        """Pass the user's answer to a permission request on to the agent."""
        raise ProviderError("This agent doesn't wait for answers to permission requests")

    def answer_form(self, request_id: str, answer: dict[str, Any] | None) -> None:
        """Answer keyed form fields, or cancel the question with None."""
        raise ProviderError("This agent doesn't wait for answers to forms")


class Provider(ABC):
    KIND: ClassVar[str]
    LABEL: ClassVar[str]
    # Its program is held where Relay puts it (a ProviderBox, or Relay runs its tools in a WorkBox):
    # AgentOS's agents run only on such providers. False: it runs on this machine as it is.
    CONFINED: ClassVar[bool] = True
    DESCRIPTION: ClassVar[str]
    # Whether the provider runs an agent with tools (shell, files, …).
    TOOLS: ClassVar[bool]
    # Offered in Settings → Add provider (the guide's example isn't: a template of it still works).
    LISTED: ClassVar[bool] = True
    FIELDS: ClassVar[tuple[Field, ...]] = ()
    # Whether a turn can be taken back and redone: true when Relay holds the
    # whole history (chat APIs); agents keep their own sessions, which can't
    # be rewound.
    REWINDABLE: ClassVar[bool] = False
    # How an agent can copy its session for a fork or edit: "end" (all of it),
    # "any" (up to any of its messages too) or "none" (it gets a handover).
    FORKS: ClassVar[str] = "none"
    # Programs the provider runs.
    PROGRAMS: ClassVar[tuple[str, ...]] = ()
    # The command that signs its program in again, typed in its Shell (Settings → Providers, Add provider's next step, a
    # repair's page: relay.creations provider_shell), e.g. "claude auth login". None: it has no such command, which
    # doesn't take its Shell away (has_shell).
    SIGN_IN: ClassVar[str | None] = None
    # What else its Shell is for, in a sentence, said where it opens.
    SHELL_HINT: ClassVar[str | None] = None
    # Whether the agent loads Relay's skills itself (from Context.skills_dir);
    # otherwise they're listed in Relay's note.
    NATIVE_SKILLS: ClassVar[bool] = False
    # Its support layer is its folder (support_files()); these are files of it elsewhere, relative to src/
    # (a bridge backend opencode-manager uses too).
    SUPPORT_FILES: ClassVar[tuple[str, ...]] = ()
    # Whether the agent takes Relay's tools as MCP tools for each run (Context.mcp_url)
    # instead of the `relay` command in its shell: no shell quoting, one route.
    RELAY_TOOLS_VIA_MCP: ClassVar[bool] = False
    EXAMPLE: ClassVar[dict[str, Any]]

    def __init__(self, template: Template) -> None:
        self.template = template
        self.validate()

    @property
    def options(self) -> dict[str, Any]:
        return self.template.options

    def validate(self) -> None:
        """Check the options against FIELDS; subclasses add their own checks."""
        known = {f.name: f for f in self.FIELDS}
        for name in self.options:
            if name not in known and name not in RETIRED_OPTIONS:
                raise TemplateError(f"Unknown option {name!r} for kind {self.KIND!r}")
        for f in self.FIELDS:
            value = self.options.get(f.name)
            if value is None:
                if f.required:
                    raise TemplateError(f"Option {f.name!r} is required")
                continue
            if not _matches(f.type, value):
                raise TemplateError(f"Option {f.name!r} must be a {f.type}")

    def detail(self) -> str | None:
        """A short note for the list of providers, e.g. what it runs against."""
        return None

    def efforts(self, model: str | None) -> list[str]:
        """The reasoning efforts `model` can be run with ([] if none)."""
        return []

    def model_efforts(self) -> dict[str, list[str]] | None:
        """Every model's efforts, by model id, when they differ per model; None: efforts() of the
        default model goes for all."""
        return None

    def billing(self, model: str | None) -> str | None:
        """Who pays for `model` ("api", "subscription", "free"; see
        opencode_manager.bridge.billing), if the provider can tell before a run."""
        return None

    def limits(self) -> dict[str, Any] | None:
        """What's left of its plan's usage limits, asked now (Settings → Usage): {"account" (which
        login: providers sharing one are shown once), "service" (Claude, ChatGPT, …), "plan",
        "windows": [relay.limits' window],
        "source": "provider", "checked"}, or {"account", "error": why not}; None when it doesn't tell.
        It uses the program's login as it is, and never renews it."""
        return None

    def free_models(self) -> list[str]:
        """The models that cost nothing (billing() "free"); ["*"] for all of them."""
        return ["*"] if self.billing(None) == FREE else []

    def abandon(self, session: str, directory: Path) -> None:
        """A run in `session` was cut off by Relay stopping (a crash, a restart): stop what the
        agent may still be doing there. By default nothing: the agent was Relay's own child
        process and ended with it."""
        return None

    def local_server(self, model: str | None) -> str | None:
        """The server on the local network or this machine that `model` runs on (its
        host, e.g. an Ollama box), else None. Relay has one such model at a time
        work per server, so a GPU doesn't have to hold several (subagents.local_models)."""
        return None

    def runs_tools(self) -> bool:
        """Whether Relay runs the tool loop for this provider (relay.toolbox):
        for models that only stream answers but can call functions."""
        return False

    def has_tools(self) -> bool:
        """An agent with its own tools, or a model with Relay's: it can do code tasks."""
        return self.TOOLS or self.runs_tools()

    def commands(self, directory: Path) -> list[dict[str, str]]:
        """The agent's own slash commands ({"name", "description"}) in a folder;
        Relay passes those on instead of treating them as text."""
        return []

    def models(self) -> list[dict[str, str]]:
        """Models to offer; [{"id", "name"}]. An empty id means the default."""
        listed = self.options.get("models")
        if listed:
            return [_model_entry(m) for m in listed]
        return [{"id": "", "name": "Default"}]

    def model_id(self, model: str | None) -> str | None:
        """A model as the program names it, from how a user or a script may give it (with the provider's id in
        front, say: "claude-code/sonnet"); as it is, by default. Relay asks it as a run starts."""
        return model

    # ProviderBoxes (relay.boxes): Relay's, when providers run in boxes; set by Relay.
    boxes: ClassVar[Any] = None
    # Set by `relay doctor`: check() looks at what runs and starts nothing that keeps
    # running (that's Relay's, in the provider's box).
    probing: ClassVar[bool] = False

    def box_key(self) -> str | None:
        """The box this provider's program runs in (one per provider); None: on the machine
        (chat APIs have no program; command and ACP agents run any program, which may not run in
        a box: they get Relay's shell as $SHELL instead)."""
        return None

    def box_mounts(self) -> list[tuple[Path, bool]]:
        """What of the machine its program needs in the box: (path, writable)."""
        return []

    def box_image(self) -> str | None:
        """Its box's image: None, Relay's box image; "workbox", the WorkBox image (Chromium, Node and
        Playwright), for a program that needs a browser."""
        return None

    def box_home(self) -> Path | None:
        """A home of its own in its box: the template's (Template.home), where the user installed its program and
        logged in, in a shell in it (a provider added with an agent, Settings → Add provider). Relay mounts it, and
        makes it the box's HOME with its programs (HOME_BINS) first on PATH and npm's global installs in it
        (relay.boxes.home_env). None: the machine's home, as the box has it."""
        return Path(self.template.home) if self.template.home else None

    def home_program(self, name: str) -> Path | None:
        """Its program `name` as installed in its home (box_home), e.g. for check(): None when it isn't there."""
        home = self.box_home()
        if home is None:
            return None
        found = shutil.which(name, path=":".join(str(home / folder) for folder in HOME_BINS))
        return Path(found) if found else None

    def has_shell(self) -> bool:
        """Whether it has a Shell (Settings → Providers, a repair's page): a provider with a box of its own, which it
        opens in (relay.creations provider_shell)."""
        return bool(self.box_home() or self.box_key())

    def box_shell(self) -> str:
        """A real shell in its box: /bin/bash there is Relay's, which hands commands to a WorkBox. A box of the WorkBox
        image has its own."""
        return "bash" if self.box_image() == "workbox" else "/usr/lib/relay/bash.real"

    def shell_ready(self) -> None:
        """Before a shell opens in its box: what its program attaches to there, running."""
        return None

    def prepare_new(self) -> None:
        """Add provider's next step: what a new provider's template names that isn't there yet, made (a profile, say).
        It isn't undone when the provider isn't added: it may hold a login by then."""
        return None

    def boxed(self) -> bool:
        """Whether nothing of it runs on the machine outside a box of Relay's: its program in its ProviderBox, or no
        program at all (a chat API). AgentOS's Governor may add only such providers (relay.agentos provider.add)."""
        return self.box_key() is not None

    def detached(self, command: str) -> bool:
        """Whether a command its program hands to its box's shell is one it detached on purpose, to
        outlive the run (a server it started with its own tool option): then it runs in a session of
        its own in the WorkBox, which a stop or the run's end leaves alone (relay.boxes)."""
        return False

    def env(self) -> dict[str, str]:
        """What its program needs in its environment besides the machine's (its config folder, say): also when
        it's run by hand, in its box (a repair's repair_run)."""
        return {}

    def judge(self, action: str, detail: Any, folder: Path | None,
              reads: list[Path] | None = None) -> tuple[str, str]:
        """What Relay answers when the agent asks to use a tool (e.g. Claude Code's
        permission prompt): ("allow" | "deny", why). Nobody is asked: commands run in a
        container; only a provider knows which of its own tools change files, which
        may happen in `folder` (the project), and which read files, which may happen
        in `reads` when that's given (else anywhere). Read-only modes (Plan, Brainstorm)
        only ask the agent, in their instructions, not to change anything. By default:
        any command in it must pass the guard."""
        command = detail.get("command") if isinstance(detail, dict) else None
        blocked = guard.check(command) if isinstance(command, str) else None
        return ("deny", f"Relay blocked this command: {blocked}") if blocked else ("allow", "")

    def suggest_title(self, prompt: str, answer: str, model: str | None = None) -> str | None:
        """A short title for a new chat after its first answer, if the provider can
        make one cheaply (a chat API asks its model); None: it keeps the first message."""
        return None

    @abstractmethod
    def check(self) -> str:
        """Verify the provider can be used; return a short status message."""

    @abstractmethod
    def start(self, context: Context, prompt: str) -> ProviderRun:
        """Start a run of `prompt`; events come from the returned run."""

    @classmethod
    def support_files(cls) -> tuple[str, ...]:
        """The support layer, relative to src/: the files that turn the program into uniform events, which
        another agent may repair (relay.repair). Its folder's (providers/<name>/, but its tests), then
        SUPPORT_FILES."""
        source = Path(sys.modules[cls.__module__].__file__).resolve()
        src = Path(__file__).resolve().parents[2]
        folder = source.parent
        files = sorted(str(path.relative_to(src)) for path in folder.rglob("*")
                       if path.is_file() and not {"__pycache__", "tests"} & set(path.relative_to(folder).parts))
        return tuple(files) + tuple(f for f in cls.SUPPORT_FILES if f not in files)

    @classmethod
    def describe(cls) -> dict[str, Any]:
        return {
            "kind": cls.KIND,
            "label": cls.LABEL,
            "description": cls.DESCRIPTION,
            "tools": cls.TOOLS,
            "listed": cls.LISTED,
            "rewindable": cls.REWINDABLE,
            "fields": [f.__dict__ for f in cls.FIELDS],
            "example": cls.EXAMPLE,
        }


def _model_entry(model: Any) -> dict[str, str]:
    if isinstance(model, str):
        return {"id": model, "name": model or "Default"}
    return {"id": str(model.get("id", "")), "name": str(model.get("name") or model.get("id") or "Default")}


def _matches(kind: str, value: Any) -> bool:
    if kind == "string":
        return isinstance(value, str)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "string-list":
        return isinstance(value, list) and all(isinstance(v, str) for v in value)
    if kind == "model-list":
        return isinstance(value, list) and all(
            isinstance(v, str) or (isinstance(v, dict) and isinstance(v.get("id"), str))
            for v in value
        )
    if kind == "object":
        return isinstance(value, dict)
    return True


def unreadable(paths: list[str], reads: list[Path] | None, base: Path | None = None) -> str | None:
    """Why reading one of `paths` (globs end in /*; relative ones are in `base`) is refused, when
    only `reads` may be read; None when it's fine."""
    if reads is None:
        return None
    for text in paths:
        text = str(text).rstrip("*").rstrip("/") or "/"
        path = Path(text) if Path(text).is_absolute() or base is None else Path(base) / text
        if not any(inside(path, allowed) for allowed in reads):
            return (f"{text} isn't this conversation's: AgentOS's agents read only their own folder "
                    "(and what Relay handed them)")
    return None


def inside(path: Path, folder: Path) -> bool:
    """Whether `path` is `folder` or in it (links followed)."""
    try:
        path, folder = Path(path).resolve(), Path(folder).resolve()
    except OSError:
        return False
    return path == folder or folder in path.parents
