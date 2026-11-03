"""Limits and texts that used to be fixed in the code, now settings (Settings → Limits).

Each has a default, bounds and a short explanation; the values that differ
from the defaults live in settings.json under "limits". Code asks with
get("subagents.parallel") when it needs one, so a change applies right away.

What's here was chosen for being worth changing: how many subagents, how
long things may take, how much text goes back and forth, what Relay says to
an agent on its own. Safety rails (the command guard, path checks) are not
settings.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class Tunable:
    key: str
    group: str
    label: str
    help: str
    default: Any
    minimum: float | None = None
    maximum: float | None = None
    unit: str = ""

    @property
    def kind(self) -> str:
        return "bool" if isinstance(self.default, bool) else "int" if isinstance(self.default, int) else "text"

    def public(self) -> dict[str, Any]:
        return {**asdict(self), "kind": self.kind}


TUNABLES = [
    Tunable("subagents.parallel", "Subagents", "At once", "Subagents that work at the same time. Each is an agent "
            "process (a Claude Code one takes a few hundred MB) and a container: keep it low on a small machine.",
            4, 1, 10),
    Tunable("subagents.per_answer", "Subagents", "Per answer", "Subagents one answer may start in all.", 12, 1, 100),
    Tunable("subagents.timeout", "Subagents", "Time limit", "A subagent is stopped after this long.", 30, 1, 240,
            "minutes"),
    Tunable("agentos.failing_steps", "AgentOS", "Failing steps", "An agent of the Governor's that fails this many "
            "steps in a row, or takes the same step this many times in a row, wakes it, to look and steer.", 4, 2, 50),
    Tunable("agentos.long_minutes", "AgentOS", "A long turn", "An agent of the Governor's that works this long on "
            "one turn wakes it, to look.", 30, 5, 1440, "minutes"),
    Tunable("agentos.wakes_per_hour", "AgentOS", "Woken for its agents", "How often an hour the Governor is woken "
            "for its agents' news (done, going wrong, a report due), each wake-up with a budget of its own: half of "
            "them may come at once, then one every hour / that many (6: every 10 minutes). More news waits for the "
            "next and comes along.", 6, 1, 60, "times"),
    Tunable("memory.idle_days", "AgentOS", "Unused memories", "AgentOS's weekly memory tidy-up looks again at "
            "memories no agent got for this long (their project not worked on, say): the Governor forgets those that "
            "no longer hold.", 90, 7, 3650, "days"),
    Tunable("services.daily_snapshots", "Services", "Daily snapshots", "A copy of each live service's data is made "
            "once a day (after 04:00, its box paused for that moment); this many are kept, to put back from its page. "
            "0: none (it's still copied before each new version).", 7, 0, 60, "days"),
    Tunable("subagents.wait", "Subagents", "One wait", "How long `relay wait` waits for answers at most; keep it "
            "under what the agent's shell tool allows (Claude Code: 2 minutes).", 100, 5, 110, "seconds"),
    Tunable("subagents.ask_timeout", "Subagents", "Time to answer", "How long a question about a subagent waits "
            "for your answer. Without one, a subagent that's allowed without asking (e.g. a free one) takes its "
            "place, if there is one; else it's refused.", 10, 5, 110, "seconds"),
    Tunable("subagents.local_models", "Subagents", "Local models at once", "Different models one local server "
            "(e.g. Ollama on your network) runs at the same time for an agent and its subagents; more than its GPU "
            "holds makes everything very slow. A subagent then gets the model that's working there already.",
            1, 1, 8),
    Tunable("computers.ask_timeout", "Your computers", "Time to answer", "How long a question about a command or "
            "file change on one of your computers waits for your answer before it counts as no.", 90, 10, 110,
            "seconds"),
    Tunable("questions.dismiss_timeout", "Questions", "Dismiss after", "Unanswered agent questions are dismissed "
            "automatically after this long, even when the chat is closed. The provider decides whether its turn "
            "continues or stops. Applies to new questions; set 0 to wait indefinitely.", 300, 0, 86400, "seconds"),
    Tunable("chat.rounds", "Chat models' tools", "Tool rounds", "Rounds of tool calls a chat model may make in one "
            "answer.", 40, 1, 200),
    Tunable("chat.tool_output", "Chat models' tools", "Tool output", "Characters of a tool's result sent back to "
            "the model.", 20_000, 1_000, 200_000, "characters"),
    Tunable("chat.command_timeout", "Chat models' tools", "Command time limit", "How long a command may run when "
            "the model doesn't say.", 120, 5, 600, "seconds"),
    Tunable("chat.temporary_hours", "Temporary chats", "Kept unused for", "A temporary chat goes when you leave it "
            "in Relay. One left otherwise (its tab closed, its script stopped) goes after this long unused.",
            24, 1, 720, "hours"),
    Tunable("scripts.timeout", "Scripts", "Time limit", "A script run from the Scripts page is stopped after this "
            "long (one that follows a timed request may take a while).", 120, 1, 1440, "minutes"),
    Tunable("search.timeout", "Web search", "A search", "How long a search waits for the search engines at most; "
            "one that answers later is left out (the answer names it). Starting the search box after half an hour "
            "unused takes a few seconds more.", 10, 3, 60, "seconds"),
    Tunable("limits.resume", "Usage limits", "Go on by itself", "When a model stops (a usage limit, or no answer at "
            "all): go on with a fallback agent right away, or else when the limit resets.", True),
    Tunable("limits.unknown_wait", "Usage limits", "When it doesn't say", "How long to wait when the provider "
            "doesn't say when its limit resets: a model in no fallback loop is tried once more then; a loop "
            "starts its next round.", 5, 1, 48, "hours"),
    Tunable("limits.loop_rounds", "Usage limits", "Rounds of a fallback loop", "How often a fallback loop "
            "is gone round before Relay gives up (between rounds it waits for the first reset).", 5, 1, 50,
            "rounds"),
    Tunable("limits.margin", "Usage limits", "Margin", "How long after the reset Relay goes on.", 60, 0, 3600,
            "seconds"),
    Tunable("limits.resume_prompt", "Usage limits", "What Relay says", "The message that makes the agent go on.",
            "The usage limit has reset, so you can go on now. Please continue the task where you left off; "
            "if it was done, say so briefly."),
    Tunable("handover.chars", "Handing a conversation over", "Transcript", "Characters of the conversation so far "
            "that go along in the prompt when another agent takes over; the rest goes in a file.", 24_000, 2_000,
            200_000, "characters"),
    Tunable("handover.message_chars", "Handing a conversation over", "One message", "Characters of one message "
            "within it.", 6_000, 500, 50_000, "characters"),
    Tunable("uploads.max_mb", "Attachments", "File size", "The largest file you can attach.", 25, 1, 500, "MB"),
    Tunable("uploads.per_message", "Attachments", "Files per message", "", 10, 1, 50),
    Tunable("tests.timeout", "Model tests", "Time limit", "How long a model test waits for an answer.", 120, 10, 600,
            "seconds"),
    Tunable("tests.prompt", "Model tests", "Prompt", "What a model test sends.", "Reply with just the word OK."),
]
BY_KEY = {t.key: t for t in TUNABLES}

# The values that differ from the defaults (settings.json's "limits"); set by config.
_values: dict[str, Any] = {}


class TunableError(ValueError):
    pass


def get(key: str) -> Any:
    tunable = BY_KEY[key]
    return _values.get(key, tunable.default)


def current() -> dict[str, Any]:
    return {t.key: get(t.key) for t in TUNABLES}


def use(values: dict[str, Any] | None) -> None:
    """Take these values (from settings.json); unknown or broken ones are left out."""
    _values.clear()
    for key, value in (values or {}).items():
        try:
            _values[key] = check(key, value)
        except TunableError:
            continue


def check(key: str, value: Any) -> Any:
    tunable = BY_KEY.get(key)
    if tunable is None:
        raise TunableError(f"There's no setting {key!r}")
    if tunable.kind == "bool":
        if not isinstance(value, bool):
            raise TunableError(f"{tunable.label}: on or off")
        return value
    if tunable.kind == "int":
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise TunableError(f"{tunable.label}: a whole number") from exc
        if tunable.minimum is not None and number < tunable.minimum or \
                tunable.maximum is not None and number > tunable.maximum:
            raise TunableError(f"{tunable.label}: between {tunable.minimum:g} and {tunable.maximum:g}")
        return number
    text = str(value).strip()
    if not text:
        raise TunableError(f"{tunable.label} can't be empty")
    return text[:2000]


def changed(values: dict[str, Any]) -> dict[str, Any]:
    """The checked values that differ from the defaults, to store."""
    found = {}
    for key, value in values.items():
        value = check(key, value)
        if value != BY_KEY[key].default:
            found[key] = value
    return found
