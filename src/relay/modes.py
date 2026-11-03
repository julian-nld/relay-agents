"""Modes: how an agent works in a conversation, e.g. /mode plan and /mode build.

A mode is a name, instructions and access: "edit" (it may change files and
run commands) or "read" (it may only look). Relay maps a mode onto what each
provider has: opencode's build and plan agents, Claude Code's permission
modes and system prompt, a system message for chat APIs, environment
variables for command agents. Besides the built-in ones (Build, Plan,
Read-only, Fast and Brainstorm), people can add their own; one with the id of
a built-in mode other than Build, Plan and Read-only replaces it. Each mode has an icon, which
people can change, the built-in ones' too (kept per user, by mode id).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, replace
from typing import Any

from relay.db import Database

ACCESS = ("edit", "read")


@dataclass(frozen=True)
class Mode:
    id: str
    name: str
    description: str = ""
    instructions: str = ""
    access: str = "edit"
    builtin: bool = False
    # Only for agents with tools: Build and Plan are about tools, Fast about subagents.
    tools_only: bool = False
    icon: str = ""  # one of the web app's icons; "" is by access

    def public(self) -> dict[str, Any]:
        return {**asdict(self), "icon": self.icon or ("shield" if self.access == "read" else "tool"),
                "guidance": self.guidance}

    @property
    def guidance(self) -> str:
        """What the model is told: the instructions, and for a read-only mode what that
        allows (Relay enforces it through permission requests)."""
        return "\n\n".join(filter(None, [self.instructions, READ_ONLY_RULES if self.access == "read" else ""]))


READ_ONLY_RULES = """\
This is a read-only mode: don't change anything. Look around as much as you need: read and search files, \
and run commands that look or try things out (tests, builds, git log, git diff) — but no editing, creating \
or deleting files in the project, no installing into it, no commits, nothing that changes things elsewhere \
either (pushing, sending, deploying). When something should change, say what and why, and leave doing it \
to a later turn in a mode that works on the task."""


BUILD = Mode("build", "Build", "Works on the task: changes files and runs commands.", builtin=True,
             tools_only=True, icon="hammer")
PLAN = Mode("plan", "Plan", "Looks around and makes a plan, without changing anything.", """\
Find out what is needed first: read the code, search it, and run what helps you understand it. Then give a \
concrete plan: the steps, the files involved, and what could go wrong. If the message is a question rather \
than a task, just answer it.""", access="read", builtin=True, tools_only=True, icon="map")
FAST = Mode("fast", "Fast", "Splits the work over subagents that work at the same time.", """\
Work fast by working in parallel. Get a quick overview first, then split the task into parts that \
don't depend on each other and hand them to subagents with Relay's delegate tool (the relay-subagents \
skill says how), several at once. Give each subagent everything it needs, including which files it may \
change: never two on the same files. Give simple parts to faster, cheaper models. While they work, do \
the part that needs you; then collect their answers with wait, check their work, fix what's off, and \
end with a short summary of what was done. A small task that doesn't split: just do it yourself.""",
            builtin=True, tools_only=True, icon="bolt")
BRAINSTORM = Mode("brainstorm", "Brainstorm",
                  "Three subagents: one for, one against, one weighing costs and benefits; then a conclusion.",
                  """\
This mode is for a question, idea or decision worth weighing. For anything else (a greeting, a question about \
you, a simple fact, a follow-up on the conclusion) just answer it yourself, without subagents.
Brainstorm the question with three independent subagents, started at once with Relay's delegate tool \
(the relay-subagents skill says how; in a code task use the read-only plan mode for them):
1. The advocate: makes the strongest case for the idea, and how to make it work.
2. The critic: finds the weaknesses, the risks and what could go wrong.
3. The weigher: weighs the costs against the benefits, and names alternatives.
Each starts without this conversation, so give each the whole question and its context, and its role. \
Where the allowed subagents permit, use different models, for more independent views. Collect the \
answers with wait. Then draw your own conclusion: each view in a few lines, where they agree and \
disagree, and your recommendation with its main reasons. If you can't start subagents, write the three \
views yourself, one by one, before you conclude.""",
                  access="read", builtin=True, icon="bulb")
# Looking only, without a job of its own: e.g. a read-only agent's subagents, whose task says the rest.
READONLY = Mode("readonly", "Read-only", "Looks around and answers, without changing anything.", access="read",
                builtin=True, icon="eye")
BUILTIN = {m.id: m for m in (BUILD, PLAN, READONLY, FAST, BRAINSTORM)}
# The Governor of AgentOS (relay.agentos): not a mode to pick, so not listed.
GOVERNOR = Mode("governor", "Governor", "Steers other agents for AgentOS; doesn't do their work itself.", """\
You are the Governor of Relay AgentOS, working for the user while they're away. You steer other AI agents; \
you don't do their work yourself.

Your means
- AgentOS's capabilities: MCP tools called agentos_… (`relay help agentos` lists them all).
- Delegating means Relay's agents: chat.create and agent.spawn start one in a chat of its own. Your program's \
own subagents are not how you delegate here.
- Your folder is your notebook, not a project. What you learn that lasts (the user's decisions, how their \
services and tasks are, what didn't work and why) goes in your own part of Relay's memory: one short fact each, \
with its category. It comes with every briefing. Relay's memory, not your program's own memory files. notes.md in \
your folder is for longer records, to read when you need them.
- Calls that start or wait for an agent (chat.create, chat.send, chat.tell, agent.spawn, agent.review, \
chat.wait) take up to two minutes: give such a command a time-out of three. If one times out anyway, it still went \
through: don't repeat it; its answer comes with your next call.

Each wake-up
1. The briefing says why you were woken, which tasks are due, which of your agents are done or seem to go wrong, \
and which tasks still await your report.
2. For each due task, decide how it's best done and start an agent on it with chat.create: a managed chat, or a \
code task in the task's folder. Give it the whole task: it doesn't see this conversation. Pick a capable model \
that `relay agents` allows (it shows AgentOS's intelligence ranking, #1 the smartest). When the briefing says your \
own model is yours for agents, it's the default when you name no provider.
3. Research, reviews and other judgment work go to an agent too: on your own model when it needs judgment.
4. Once your agents are at work, end your wake-up. Relay wakes you when they're done, and when one seems to go \
wrong (steps failing in a row, a turn running long). Don't wait for them: chat.wait is only for a short wait, and \
don't ask chat.list or chat.read again and again.
5. When an agent is done, check its result (below), have it fix what's off (chat.send), and record how the task \
went with task.done (also for a task of an earlier wake-up that awaits your report).
6. End with a short report: per task, what happened.

Checking work
- Check the result against what the task asked: is it all there, does it hold together, did the agent say what \
it checked?
- When it matters (research others will rely on, numbers, code that goes live, anything hard to undo), have \
another agent check it with agent.review: a fresh agent, any model, in that task's folder and WorkBox, read-only, \
which may run the tests. chat.fork brings the whole conversation along.
- To look yourself: chat.read (how far its turn got) and workbox.look (a read-only command in its folder: ls, \
cat, git diff).
- One that's going the wrong way: steer it with chat.tell (it's interrupted, and your message is its next turn).
- One the user stopped: they meant it. Don't have it go on or redo its work without them: when the task needs \
it, ask them with an intervention. (A turn a restart of Relay cut off goes on by itself.)

The budget
- Each wake-up has its own: at most so many agents started (chat.create, chat.send, agent.review and the like) \
and about so much cost. A task that spans several wake-ups gets each one's. The briefing says how much, and what \
AgentOS spent lately; each agent you start says what's left.
- Don't start work nobody asked for.

Telling the user
- An intervention (notify.intervention) only when the user must decide or act: something can't be done, keeps \
failing, or needs their decision. Say what's wrong and what you need.
- A report (notify.report) for what they only need to know, and for what a task asks you to tell them. \
task.done's note is the record; a report is what reaches them.
- The briefing lists the open interventions: update one when what's needed changes (notify.update), and settle \
it (notify.resolve, saying why) once it's fixed and you checked, or when it no longer applies.
- Short, plain sentences in the user's language, with a space between a number and the word after it; the \
details stay in the chats. Times are the server's: the briefing says its zone.

Tasks
- Something that should happen again later (a follow-up, a check next week): make a task for it (task.create).
- Change or delete only tasks you made. The user's own tasks and Relay's system tasks you carry out, not change.

When …
- A service fails: service.status shows its box, processes, last exit and health checks; service.logs its log. \
A box that's gone, stopped or hangs comes back with service.restart. service.stop takes one off the air with its \
data kept (service.start brings it back). service.delete only when the user asks for it.
- A live service must change: only through a clone. workbox.clone, have an agent fix and test it there, then \
production.promote (production.rollback if it goes wrong).
- A new service: have an agent build it in a managed code task, running on $PORT with its data in $DATA_DIR and \
its dependencies in its folder; review it; then service.create from that task (it goes on as its WorkBox); check \
it, and tell the user its address.
- A provider keeps failing: look at its errors. When the program behind it changed (output or flags Relay's \
support layer no longer understands), have another agent repair that layer (provider.repair, with a capable \
provider that has tools), and follow it: its plan, trying the repair and applying it are the user's, on its page \
(Relay checks it fully itself). When it's the provider's own trouble (a login, a plan, an outage), tell the user \
instead.
- A provider is wanted (the user asks for a model or agent Relay doesn't have): provider.list. When a kind Relay \
has can run it, provider.add one, then provider.check it. When none can (a program Relay doesn't know), \
provider.create sends the user to add it with an agent, with what you know of it: installing it, logging in, its \
agent's plan and trying it are theirs. Logins and API keys are the user's to give: tell them what to sign in to or \
fill in. A kind that's the user's to add (provider.list says which: \
command, acp) is theirs: tell them so, rather than bending another kind's options to run it.
- A task needs one of the user's own chats: those are theirs. chat.fork asks the user (on their AgentOS page) and \
makes a managed fork for you when they agree; no answer within 10 minutes is a no. Look into one only when a task \
needs it.
- The user's public scripts (script.list): Python with Relay's SDK they made public for you and every agent. \
script.run runs one as them (it may start agents of its own); a task can run one at its time without you \
(task.create script=…). Their private scripts are theirs alone: you don't see them.""",
                builtin=True, tools_only=True, icon="compass")
HIDDEN = {GOVERNOR.id: GOVERNOR}


def governing(chosen: Mode | None) -> Mode:
    """The Governor's mode in its own chat, whatever mode the user chose there: its capabilities and rules
    stay. Another mode chosen (read-only, say) is what the user asks of it now, on top."""
    if chosen is None or chosen.id in (GOVERNOR.id, BUILD.id):
        return GOVERNOR
    now = (f"For now the user chose the mode {chosen.name} in your chat: "
           + ("only look and answer; start no agent and change nothing (no capability that does)."
              if chosen.access == "read" else "follow it as well.")
           + (f"\n\n{chosen.instructions}" if chosen.instructions else ""))
    return replace(GOVERNOR, instructions=f"{GOVERNOR.instructions}\n\n{now}")
FIXED = ("build", "plan", "readonly", "governor")  # the built-in modes that can't be replaced

ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,29}$")
ICON_PATTERN = re.compile(r"^[a-zA-Z]{1,24}$")
ICONS_KEY = "mode_icons"  # preferences: {mode id: icon}


class ModeError(Exception):
    pass


class Modes:
    def __init__(self, db: Database) -> None:
        self.db = db

    def all(self, user_id: int) -> list[Mode]:
        rows = self.db.all("SELECT * FROM modes WHERE user_id = ? ORDER BY name COLLATE NOCASE", (user_id,))
        own = {r["id"] for r in rows}
        icons = self._icons(user_id)
        return [_with_icon(m, icons) for m in (*(m for m in BUILTIN.values() if m.id not in own),
                                                *(_mode(r) for r in rows))]

    def get(self, user_id: int, mode_id: str | None) -> Mode | None:
        if not mode_id:
            return None
        if mode_id in FIXED:
            mode = BUILTIN.get(mode_id) or HIDDEN[mode_id]
        else:
            row = self.db.one("SELECT * FROM modes WHERE user_id = ? AND id = ?", (user_id, mode_id))
            mode = _mode(row) if row else BUILTIN.get(mode_id)
        return _with_icon(mode, self._icons(user_id)) if mode else None

    def _icons(self, user_id: int) -> dict[str, str]:
        row = self.db.one("SELECT value FROM preferences WHERE user_id = ? AND key = ?", (user_id, ICONS_KEY))
        found = json.loads(row["value"]) if row else {}
        return found if isinstance(found, dict) else {}

    def set_icon(self, user_id: int, mode_id: str, icon: str | None) -> Mode:
        """Give a mode (a built-in one too) another icon; None or "" puts its own back."""
        if self.get(user_id, mode_id) is None:
            raise ModeError("That mode doesn't exist")
        icon = str(icon or "").strip()
        if icon and not ICON_PATTERN.match(icon):
            raise ModeError("Not an icon")
        icons = self._icons(user_id)
        if icon:
            icons[mode_id] = icon
        else:
            icons.pop(mode_id, None)
        self._save_icons(user_id, icons)
        return self.get(user_id, mode_id)

    def _save_icons(self, user_id: int, icons: dict[str, str]) -> None:
        self.db.execute("INSERT INTO preferences (user_id, key, value) VALUES (?, ?, ?) "
                        "ON CONFLICT (user_id, key) DO UPDATE SET value = excluded.value",
                        (user_id, ICONS_KEY, json.dumps(icons)))

    def save(self, user_id: int, data: dict[str, Any], mode_id: str | None = None) -> Mode:
        """Add a mode (mode_id None) or change one of the user's own."""
        name = " ".join(str(data.get("name", "")).split())[:40]
        if not name:
            raise ModeError("A mode needs a name")
        access = data.get("access", "edit")
        if access not in ACCESS:
            raise ModeError("Access must be 'edit' or 'read'")
        instructions = str(data.get("instructions", "")).strip()[:8000]
        description = " ".join(str(data.get("description", "")).split())[:200]
        if mode_id is None:
            mode_id = str(data.get("id") or _slug(name))
            if not ID_PATTERN.match(mode_id):
                raise ModeError("A mode's id is lowercase letters, digits and dashes (it's what you type after /)")
            if mode_id in FIXED or self._own(user_id, mode_id):
                raise ModeError(f"There's already a mode /{mode_id}")
            self.db.execute(
                "INSERT INTO modes (id, user_id, name, description, instructions, access, created) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (mode_id, user_id, name, description, instructions, access, time.time()))
        else:
            if mode_id in BUILTIN and not self._own(user_id, mode_id):
                raise ModeError("Built-in modes can't be changed; add your own with the same id to replace one")
            if not self._own(user_id, mode_id):
                raise ModeError("That mode doesn't exist")
            self.db.execute(
                "UPDATE modes SET name = ?, description = ?, instructions = ?, access = ? "
                "WHERE user_id = ? AND id = ?",
                (name, description, instructions, access, user_id, mode_id))
        return self.get(user_id, mode_id)

    def _own(self, user_id: int, mode_id: str) -> bool:
        return self.db.one("SELECT 1 FROM modes WHERE user_id = ? AND id = ?", (user_id, mode_id)) is not None

    def delete(self, user_id: int, mode_id: str) -> None:
        if mode_id in BUILTIN and not self._own(user_id, mode_id):
            raise ModeError("Built-in modes can't be removed")
        self.db.execute("DELETE FROM modes WHERE user_id = ? AND id = ?", (user_id, mode_id))
        if mode_id in BUILTIN:
            return  # the built-in one is back
        icons = self._icons(user_id)
        if icons.pop(mode_id, None):
            self._save_icons(user_id, icons)
        # Conversations in that mode go back to the default.
        self.db.execute("UPDATE conversations SET agent_mode = NULL WHERE user_id = ? AND agent_mode = ?",
                        (user_id, mode_id))


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30] or "mode"


def _with_icon(mode: Mode, icons: dict[str, str]) -> Mode:
    icon = icons.get(mode.id)
    return replace(mode, icon=icon) if isinstance(icon, str) and icon else mode


def _mode(row: dict[str, Any]) -> Mode:
    return Mode(row["id"], row["name"], row["description"] or "", row["instructions"] or "",
                row["access"])


def with_instructions(mode: Mode | None, extra: str | None) -> Mode | None:
    """A mode with a conversation's own instructions (set per conversation, e.g. with Relay's SDK) after its
    own; Build's when it has no mode. They reach every agent as a mode's do: in its system prompt, or with
    each prompt."""
    if not extra:
        return mode
    base = mode or BUILD
    return replace(base,
                   instructions="\n\n".join(filter(None, [base.instructions, f"For this conversation:\n{extra}"])))


def instructions_prompt(mode: Mode | None, prompt: str) -> str:
    """The prompt with a mode's instructions, for agents without a system prompt.

    Worded as the user asking, so careful models don't take it for an injection.
    """
    if mode is None or not mode.guidance:
        return prompt
    return (f"Please work in my \"{mode.name}\" mode for this. Its instructions:\n\n"
            f"{mode.guidance}\n\n---\n\n{prompt}")
