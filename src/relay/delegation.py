"""Relay's own tools for agents: subagents and the user's computers, on top of the agent's own tools.

Relay doesn't replace what a provider has (its tools, its MCP servers, its
own subagents); it adds a small command-line tool next to them. Every agent
conversation gets a script of its own, `relay-tools/<token>/relay` in the
machine's cache (read-only), and the
agent is told about it: Claude Code in its system
prompt, opencode in the first message of a session, command agents through
RELAY_TOOLS. The agent runs it with its shell tool:

    relay agents            the providers, models and modes to hand work to
    relay delegate …        start a subagent (the task on stdin); prints its id
    relay wait [ID …]       wait a while (subagents.wait); prints finished answers
    relay computers         the user's own computers (relay.computers), and
    relay run/pull/push     commands and files there

Starting and waiting are separate so that no shell tool times out (Claude
Code's Bash stops after two minutes by default) and so several subagents can
run at once. A subagent runs as a hidden conversation of its own, starts
without the calling agent's context, and shows up in the calling agent's
answer as a card with its own progress, model and cost.

The script and what agents are told about it are in relay.tools_script.
It talks to Relay over HTTP (JSON-RPC at /mcp/<token>, the same
endpoint Claude Code's permission prompts use over MCP). The token is the
only credential: long, random, one per conversation, and only good while a
run of that conversation is going.

Which providers and models an agent may use without asking the user is
up to the user (relay.policy); others are asked for, or refused.

A free subagent whose model fails goes on with another free model (_free_fallback), so free
models coming and going doesn't leave its task undone; a paid one is its caller's to decide on.

Limits: subagents get no tools of Relay themselves (one level deep) but its web search, at most
a few run at once (each is an agent process and a container), a dozen per run, each for half an hour (Settings → Limits;
relay.tunables). They stop when the calling run ends.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import re
import secrets
import shlex
import stat
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opencode_manager.bridge.billing import FREE
from relay import (
    __version__,
    fallback,
    forks,
    git,
    guard,
    health,
    memory,
    policy,
    ranking,
    relay_tools,
    tasks,
    tunables,
)
from relay.computers import DEFAULT_TIMEOUT, MAX_TIMEOUT, ComputerError
from relay.fetch import FetchError
from relay.modes import Modes
from relay.plugins import PluginError, ToolCall
from relay.providers import TemplateError, safely
from relay.runs import RunError
from relay.search import SearchError
from relay.tools_script import SCRIPT, help_text, tools_dir

if TYPE_CHECKING:
    from relay.app import Relay
    from relay.runs import ActiveRun

MAX_ANSWER = 30_000  # characters of a subagent's answer passed back
FAILED_LATELY = 24 * 3600  # seconds a model's failed last run is mentioned in agents
EARLY = 8  # seconds delegate waits to see a subagent get going, so failing right away is reported at once
AGAIN = ("To still get this done, delegate it again with another model (agents lists them; not this one, "
         "it just failed), or do it yourself.")
PRUNE_AFTER = 7 * 86400  # seconds unused before an unknown tools script is removed
PAID_BY = {"api": "paid per use (API)", "subscription": "counts toward a plan's limits", "free": "free"}
# A free subagent whose model fails (an endpoint that's unavailable, a model not offered in the user's
# country, a limit) goes on with another free model, this many times at most, so one model's trouble
# doesn't leave its task undone; a model that failed this recently isn't one to go to.
FREE_FALLBACKS = 3
FAILED_RECENTLY = 3600

PROTOCOL = "2025-06-18"
MISSED = 5  # answers an agent stopped waiting for, kept for its next call (the latest)

# Claude Code calls this for every permission when Relay asks the user
# (--permission-prompt-tool); it isn't meant for the model itself.
APPROVE = {
    "name": "approve_permission",
    "description": "Asks the user for permission to use a tool. Only Claude Code's permission "
                   "prompt uses this; don't call it yourself.",
    "inputSchema": {"type": "object", "properties": {
        "tool_name": {"type": "string"}, "input": {"type": "object"}, "tool_use_id": {"type": "string"}},
        "required": ["tool_name", "input"]},
}


def _tool(tool_name: str, description: str, required: list[str], **properties: dict[str, Any]) -> dict[str, Any]:
    return {"name": tool_name, "description": description,
            "inputSchema": {"type": "object", "properties": properties, "required": required}}


_TEXT = {"type": "string"}
# Told to a subagent working in a copy of its caller's folder (relay.forks).
FORK_NOTE = ("\n\n(From Relay: you work in a copy of your caller's folder, without its git history; what you "
             "change goes into the original when you're done, except gitignored files. Installed software "
             "(node_modules, virtual environments) is the original's, shared; small ignored files such as .env "
             "are copied, ignored folders such as build output are not.)")

# Relay's tools for agents that take them as MCP tools (Provider.RELAY_TOOLS_VIA_MCP).
RELAY_TOOLS = [
    _tool("agents", "The AI agents you can hand work to: providers, their models and modes, and which "
          "are allowed without asking the user.", []),
    _tool("delegate", "Hand a task to another AI agent (another provider or model). It starts without this "
          "conversation, so give it everything it needs. With wait true, this waits for the answer (up to "
          "100 s; else collect it with wait); otherwise it returns an id at once, so several work at the same time.",
          ["task"], task={**_TEXT, "description": "What to do, with all the context needed."},
          provider={**_TEXT, "description": "Provider id from agents; default: yours."},
          model={**_TEXT, "description": "Model id for that provider."},
          mode={**_TEXT, "description": "Mode id, e.g. plan for work that only looks."},
          label={**_TEXT, "description": "A few words shown to the user, e.g. Critic."},
          effort={**_TEXT, "description": "How hard it thinks, one of the model's efforts (agents lists "
                  "them); default: its own, or in Fast mode its lowest."},
          wait={"type": "boolean", "description": "Wait for the answer."}),
    _tool("wait", "Collect the answers of subagents (waits up to 100 s for them).", [],
          ids={"type": "array", "items": _TEXT, "description": "Subagent ids; default: all not collected yet."}),
    _tool("tell", "Send one of your subagents a message: one still working is interrupted and gets it (e.g. "
          "\"Hurry up: answer with what you have now\"); a finished one gets it as a follow-up. Then wait "
          "for its answer again.", ["id", "message"], id={**_TEXT, "description": "The subagent's id."},
          message={**_TEXT, "description": "What to tell it."}),
]
# Relay's memory (relay.memory), when the user has it on: every run's but subagents', the Governor's too.
_CATEGORY = {"type": "string", "enum": memory.KINDS,
             "description": "What it's about: " + "; ".join(f"{k} ({about})" for k, _, about in memory.CATEGORIES)
             + "."}
MEMORY_TOOLS = [
    _tool("memory_add", "Save something lasting to Relay's memory, which every agent in every chat of the user's "
          "gets: about the user or their wishes, or (project true) about this project. One short fact each, filed "
          "in its category; not what only this conversation needs.", ["text", "category"],
          text={**_TEXT, "description": "The fact, short and on its own (it's read without this conversation)."},
          category=_CATEGORY,
          project={"type": "boolean", "description": "Only for this project (default: general). The Governor: your "
                                                     "own memory, which no other agent gets."}),
    _tool("memory_change", "Change one of Relay's memories, when it changed: its text, its category, or both.", ["id"],
          id={**_TEXT, "description": "Its id, as the memory's list shows it."},
          text={**_TEXT, "description": "Its new text."},
          category=_CATEGORY),
    _tool("memory_forget", "Forget one of Relay's memories: it's wrong, or no longer so.", ["id"],
          id={**_TEXT, "description": "Its id, as the memory's list shows it."}),
    _tool("memory_list", "Relay's memories for this conversation: the general ones and this project's.", []),
]
# Relay's web search (relay.search), when it's on: every run's, subagents' and the Governor's too.
SEARCH = _tool("search", "Search the web with Relay's own SearXNG (several search engines at once): titles, addresses "
               "and snippets. The user prefers it to a search tool of your own: search with this first. Read a "
               "result with your own fetch tool.", ["query"],
               query={**_TEXT, "description": "What to search for."},
               category={**_TEXT, "description": "general (default), news, it or science."},
               time_range={**_TEXT, "description": "Only this recent: day, week, month or year."},
               language={**_TEXT, "description": "A language code, e.g. en or nl; default: any."},
               page={"type": "integer", "description": "Which page of results; default 1."})
# Relay's web fetch (relay.fetch): every run's, as the search.
FETCH = _tool("fetch", "Read a web page or file as text, with Relay's own fetch: a page's main content as markdown, "
              "a PDF's text. When a plain fetch is refused (a bot check, 403) or comes back empty (a page its "
              "JavaScript builds), it opens the page in a real browser; a site that keeps refusing, through "
              "Startpage or from the Wayback Machine (it says so). Use it when your own fetch fails. Long ones come "
              "in parts.", ["url"],
              url={**_TEXT, "description": "The address (http or https)."},
              offset={"type": "integer", "description": "Where to go on in a long one (its last part says)."})
# A code task's own record of its turns (relay.tasks): no commits unless the user asks for one.
TASK_TOOLS = [
    _tool("changes", "This code task's turns as Relay recorded them (it keeps the files before and after "
          "each, outside git): what each changed, and all of it since the task started.", []),
    _tool("diff", "The changes of one turn of this task (a unified diff), or of all of them.", [],
          turn={**_TEXT, "description": "The turn's number from changes; default the last; \"all\": since the "
                "task started."}),
    _tool("restore", "Put this task's files back as they were before a turn (the user's edits since go back "
          "too; it can be undone with the point it returns). Only when the user asks for it.", ["turn"],
          turn={**_TEXT, "description": "The turn's number from changes, or a point restore returned."}),
    _tool("commit", "Commit this task's folder to the user's git repository (its current branch, as the "
          "user). Only when the user asks you to: Relay never commits by itself.", ["message"],
          message={**_TEXT, "description": "The commit message."}),
    _tool("merge", "In a code task a subagent works in a copy of your folder; its changes come into yours when "
          "it's done, unless they conflict with yours or it didn't finish. Then you decide: this applies them "
          "anyway, with conflict markers where they conflict.", ["id"],
          id={**_TEXT, "description": "The subagent's id."}),
]
SUBAGENT_TOOLS = {"agents", "delegate", "wait", "tell"}  # Relay's subagent tools (relay.relay_tools: subagents)
# A repair's agent (relay.repair; a new kind's too): what it can't do in its WorkBox, which hasn't the program, Relay
# does outside it (its agent couldn't run the program, nor test its version, a user found)
REPAIR_TOOLS = [
    _tool("repair_check", "Test your version of the provider's files (this folder's) in a copy of Relay, in a box of "
          "its own outside your container: a repair by the provider's check and a tiny prompt, a new provider by "
          "Relay's whole check of it. Run it until it passes.", []),
    _tool("repair_run", "Run a command where the provider's program runs: in a box with it and its login, as Relay "
          "runs it (its version, its --help, a tiny prompt to see what it outputs). Your own commands run in a "
          "container without it.", ["command"],
          command={**_TEXT, "description": "The command, e.g. claude --version."},
          timeout={"type": "integer", "description": "Seconds to wait, at most 600 (default 120)."}),
]
# A new provider's or a repair's agent, before it builds (relay.creations): its plan, for the user to approve
PLAN_TOOL = _tool("provider_plan", "Hand in your plan, for the user to approve (or to ask for another): a new "
                  "provider's support layer (with its name and program), or a repair. Then stop: you're asked to "
                  "carry it out once it's approved.", ["plan"],
                  name={**_TEXT, "description": "A new provider: its folder's name, a Python name: "
                        "relay/providers/<name>/ (gemini). Not for a repair."},
                  program={**_TEXT,
                           "description": "A new provider: how its program is started in its box, e.g. gemini. "
                           "Not for a repair."},
                  plan={**_TEXT, "description": "For the user, short and plain. A new provider: what it is, how Relay "
                        "will run it (the command, what it prints), sessions, models, permissions, what you couldn't "
                        "find out. A repair: what's wrong, and what you'll change."})
# AgentOS's Governor's capabilities (relay.agentos): only its runs get them.
_ID = {**_TEXT, "description": "The chat's or task's id (its first characters will do)."}
AGENTOS_TOOLS = [
    _tool("agentos_task_list", "The AgentOS tasks: what each is, when it runs next, when it ran last and how that "
          "went (who said so), and which await your report.", []),
    _tool("agentos_task_done", "Record how a task went this time.", ["id"], id=_ID,
          ok={"type": "boolean", "description": "Whether it was done (default true)."},
          note={**_TEXT, "description": "What happened, briefly (shown with the task when it failed)."}),
    _tool("agentos_task_create", "Make a task of your own: for you (default), a prompt for a chosen agent "
          "(governed false, with provider), or one of the user's public scripts that Relay runs at its time, as the "
          "user, without an agent (script). It's yours to change and delete; the user's tasks aren't.",
          ["name", "when"], name={**_TEXT, "description": "A short name."},
          message={**_TEXT, "description": "What's to be done each time (not for a script)."},
          script={**_TEXT, "description": "A public script's name (script.list), e.g. report.py: Relay runs it."},
          args={**_TEXT, "description": "The script's arguments (optional)."},
          when={**_TEXT, "description": "every 72 hours, daily 08:00, weekdays 09:00, weekly mon,wed 08:00, "
                "once 2026-10-05 09:00 (the server's time: the briefing says its zone), once 2026-10-05T07:00Z, or "
                "in 10 minutes."},
          folder={**_TEXT, "description": "A project folder it's about (optional)."},
          governed={"type": "boolean", "description": "Yours to carry out (default true)."},
          provider={**_TEXT, "description": "For governed false: the agent's provider."}, model=_TEXT, mode=_TEXT,
          effort=_TEXT),
    _tool("agentos_task_update", "Change a task you made (fields you leave out stay; enabled false turns it off).",
          ["id"], id=_ID, name=_TEXT, message=_TEXT, when=_TEXT, folder=_TEXT,
          enabled={"type": "boolean"}, governed={"type": "boolean"}, provider=_TEXT, model=_TEXT, mode=_TEXT,
          script=_TEXT, args=_TEXT),
    _tool("agentos_task_delete", "Delete a task you made.", ["id"], id=_ID),
    _tool("agentos_chat_list", "The managed chats and code tasks (made by AgentOS): ids, state, who works in them.",
          []),
    _tool("agentos_chat_create", "Create a managed chat (or, with folder, a code task in that project folder); with "
          "message, an agent starts on it at once and the answer comes back (or you're woken when it's done).",
          [], provider={**_TEXT, "description": "Provider id (agents lists them); default: your own model, when "
                        "the user lets your agents use it."},
          model={**_TEXT, "description": "Model id for that provider."},
          folder={**_TEXT, "description": "A project folder: a code task there (a new folder in a workspace root is "
                  "made); none: a chat."},
          mode={**_TEXT, "description": "Mode id, e.g. plan for looking only."},
          effort={**_TEXT, "description": "How hard it thinks (agents lists the efforts)."},
          title={**_TEXT, "description": "What the user sees it as."},
          message={**_TEXT,
                   "description": "The task, with everything the agent needs: it doesn't see your conversation."}),
    _tool("agentos_agent_spawn", "chat_create with a task: an agent starts on it at once.", ["message"],
          provider={**_TEXT, "description": "Provider id; default: your own model."}, model=_TEXT, folder=_TEXT,
          mode=_TEXT, effort=_TEXT,
          title=_TEXT, message={**_TEXT, "description": "The task, with everything the agent needs."}),
    _tool("agentos_agent_review", "A fresh agent (the model you choose, none of the task's conversation) in a managed "
          "code task's folder and WorkBox, read-only by default: it reads the code, runs its tests and tries things, "
          "without changing it. For a review by other eyes, of a service's clone too.", ["id", "message"],
          id=_ID, provider={**_TEXT, "description": "Provider id (agents lists them); default: your own model."},
          model=_TEXT,
          mode={**_TEXT, "description": "Mode id; default readonly (plan also only looks)."}, effort=_TEXT,
          title=_TEXT, message={**_TEXT, "description": "What to review and how: it doesn't see the task's "
                                "conversation."}),
    _tool("agentos_chat_send", "Send a managed chat a message: a quick answer (within 30 s) comes back at once, "
          "else you're woken when it's done.", ["id", "message"],
          id=_ID, message=_TEXT),
    _tool("agentos_chat_tell", "Steer an agent: one that's working is interrupted, and the message is its next "
          "turn (what to do instead); one that isn't gets it as chat_send would.", ["id", "message"], id=_ID,
          message={**_TEXT, "description": "What it should do now, with what it needs to know."}),
    _tool("agentos_chat_read", "The last messages of a managed chat (and, while it works, how far its turn got).",
          ["id"], id=_ID,
          last={"type": "integer", "description": "How many (default 4, at most 20)."},
          offset={"type": "integer", "description": "Its last answer from this character on (a long one comes in "
                  "parts: the reply says where the next starts)."}),
    _tool("agentos_chat_wait", "Wait a short while (up to 100 s) until managed chats are done: their answers, and "
          "how far those still working got. For longer work end your wake-up instead: you're woken when they're "
          "done.", [],
          ids={"type": "array", "items": _TEXT, "description": "Their ids; default: all that work now."}),
    _tool("agentos_chat_fork", "A managed fork of a chat: the user's own takes their permission (asked now).", ["id"],
          id=_ID),
    _tool("agentos_chat_delete", "Delete a managed chat or code task.", ["id"], id=_ID),
    _tool("agentos_service_list", "The live services: address, live version, health, its box's state, how each "
          "starts.", []),
    _tool("agentos_service_status", "One service in detail, to find out what's wrong: its box (running or not, since "
          "when, how often podman restarted it, last exit code, killed for memory), the processes in it, Relay's last "
          "health check and failures in a row, a check made just now, an operation going on, its versions, and its "
          "last 7 days: when it failed (how long, why) and how its data grew.",
          ["service"], service={**_TEXT, "description": "The service's name."}),
    _tool("agentos_service_logs", "A service's live version's log (its last lines).", ["service"],
          service={**_TEXT, "description": "The service's name."}, lines={"type": "integer"}),
    _tool("agentos_service_restart", "Restart a service's live version (its box is made anew when it's gone or "
          "broken), then Relay's health check: for a service that stopped or hangs.", ["service"],
          service={**_TEXT, "description": "The service's name."}),
    _tool("agentos_service_stop", "Stop a service: it isn't served (its address answers nothing) or checked any "
          "more; its versions and data stay, for service_start.", ["service"],
          service={**_TEXT, "description": "The service's name."}),
    _tool("agentos_service_start", "Start a stopped service again: its live version, checked, at its address.",
          ["service"], service={**_TEXT, "description": "The service's name."}),
    _tool("agentos_service_delete", "Delete a service, with its versions, archive and data: only when the user "
          "asks for it (to have it gone but keep its data, service_stop).", ["service"],
          service={**_TEXT, "description": "The service's name."}),
    _tool("agentos_workbox_clone", "A service's live code, and a snapshot of its data in .data, as a managed code task "
          "to work on; with message an agent starts at once.", ["service"],
          service=_TEXT, provider={**_TEXT, "description": "Who works in it (agents lists them); default: your own "
                                   "model."}, model=_TEXT,
          mode=_TEXT, message={**_TEXT, "description": "The task, with everything the agent needs."}),
    _tool("agentos_workbox_delete", "Delete a clone (a managed code task) and its folder.", ["id"], id=_ID),
    _tool("agentos_workbox_look", "Look at an agent's work yourself: a command (ls, cat, git diff, grep, a check that "
          "only reads) in a managed code task's folder (a clone's too), mounted read-only in a box of its own: "
          "nothing there can change.", ["id", "command"], id=_ID, command={**_TEXT, "description": "A shell command."}),
    _tool("agentos_production_promote", "Make a managed code task's folder the service's new live version: it's "
          "started and health-checked first; only then does it take over, and the old one is archived for 7 days.",
          ["service", "id"], service=_TEXT, id=_ID),
    _tool("agentos_production_rollback", "Back to an archived version of a service (the latest, or version).",
          ["service"], service=_TEXT, version={"type": "integer"}),
    _tool("agentos_service_create", "Make a new live service from a managed code task's folder, once its agent made it "
          "run on $PORT with its data in $DATA_DIR and it was reviewed: started and checked, at a public port of its "
          "own, looked after by AgentOS. The task goes on as its WorkBox (production.promote for its next versions).",
          ["id"], id=_ID, name={**_TEXT, "description": "a-z, 0-9 and - (default: the folder's name)."},
          command={**_TEXT, "description": "How it starts (default: guessed from the folder)."},
          port={"type": "integer", "description": "Its public port (default: a free one from 8500)."},
          health={**_TEXT, "description": "A path to check, e.g. /health (default: a connection is enough)."}),
    _tool("agentos_service_restore", "Put a service's data back as one of its snapshots has it (service.status "
          "lists them: before each version, daily, before a restore). Its box stops meanwhile; the data as it is now "
          "is kept first as a snapshot, so putting that back undoes it.", ["service", "snapshot"], service=_TEXT,
          snapshot={**_TEXT, "description": "The snapshot's name, e.g. daily-2026-10-06 or before-3."}),
    _tool("agentos_provider_repair", "Have another agent repair Relay's support layer for a provider that keeps "
          "failing (the program behind it changed its output or flags), on the repair's page: it hands in a plan in a "
          "managed code task to follow with chat.read and chat.send. Its plan, trying it and applying it are the "
          "user's (a Needs-you item takes them there); Relay checks it fully itself.", ["provider", "helper"],
          provider={**_TEXT, "description": "The provider to repair (its id)."},
          helper={**_TEXT, "description": "Another provider with tools whose agent repairs it."}, model=_TEXT,
          message={**_TEXT, "description": "What you know of what's wrong (its errors), for the agent."}),
    _tool("agentos_provider_list", "The providers here (which you added; which run outside a box, the user's), and "
          "the kinds you may add, with their options and an example template.", []),
    _tool("agentos_provider_add", "Add a provider of a kind Relay has, whose program runs in a box of Relay's (or a "
          "chat API): provider.list says which. A secret option (an API key) is the user's to fill in; a kind that "
          "needs one is theirs to add. Then provider.check it.", ["kind"],
          kind={**_TEXT, "description": "Its kind, e.g. opencode, openai-compatible."},
          id={**_TEXT, "description": "Its id: lowercase letters, digits and - (default: the kind's)."},
          name={**_TEXT, "description": "Its name in Relay."},
          options={"type": "object", "description": "Its options, as the kind's example has them."}),
    _tool("agentos_provider_remove", "Remove a provider you added (not one of the user's).", ["id"],
          id={**_TEXT, "description": "The provider's id."}),
    _tool("agentos_provider_check", "Relay's whole check of a provider, as the user's Check it fully: run as Relay "
          "runs it, through a few short prompts to its model (a turn, going on, stopping, a command). Takes a few "
          "minutes.", ["id"], id={**_TEXT, "description": "The provider's id."},
          model={**_TEXT, "description": "The model to check it with (default: its default)."}),
    _tool("agentos_provider_create", "A program Relay doesn't know is wanted as a provider (no kind runs it): send "
          "the user to add it with an agent, a Needs-you item that opens its page. They install it in a box of its "
          "own and log in, approve an agent's plan for it, and try it in a trial Relay before it's added.",
          ["program"], program={**_TEXT, "description": "The program, e.g. Gemini CLI (npm @google/gemini-cli)."},
          message={**_TEXT, "description": "What you know of it, for the user and their agent: what it is, how it's "
                   "installed and started, its login."}),
    _tool("agentos_notify_intervention", "Tell the user an intervention is needed: only when they must decide or do "
          "something, saying what's wrong and what you need. Its id comes back (and the briefings list the open "
          "ones).", ["message"], message=_TEXT),
    _tool("agentos_notify_report", "Tell the user what's done or found, for them to know (nothing to act on): it's "
          "on their AgentOS page and pushed to them, not on their list of what needs them.", ["message"],
          message=_TEXT),
    _tool("agentos_notify_update", "Change an open intervention's text: what's needed now (the user sees it was "
          "updated).", ["id", "message"], id={**_TEXT, "description": "The intervention's id (its first characters "
          "will do)."}, message=_TEXT),
    _tool("agentos_notify_resolve", "Settle an open intervention: the problem is fixed (you checked) or no longer "
          "applies. It leaves the user's list and your briefings; the user sees you settled it, and why.",
          ["id", "note"], id={**_TEXT, "description": "The intervention's id (its first characters will do)."},
          note={**_TEXT, "description": "Why it's settled: what was done, how you checked."}),
    _tool("agentos_memory_review", "All of Relay's memory (every place: general, each project, your own), each with "
          "when an agent last got it, and what Relay found: memories worded nearly alike, those whose place is gone, "
          "those no agent got for long. For the memory tidy-up.", []),
    _tool("agentos_memory_change", "Change a memory (any place; not the user's own): its text, its category, or both. "
          "To merge memories that say the same: the clearest changed, the others forgotten.", ["id"],
          id={**_TEXT, "description": "The memory's id (its first characters will do)."},
          text={**_TEXT, "description": "Its new text."},
          category={**_TEXT, "description": "Its new category (preferences, about, work, tools, interests, location, "
                    "setup, decisions, other)."}),
    _tool("agentos_memory_forget", "Forget a memory (any place; not the user's own: name those in your report): one "
          "that says what another does, whose place is gone, or that no longer holds. The user sees it, and why, in "
          "the activity.", ["id", "why"],
          id={**_TEXT, "description": "The memory's id (its first characters will do)."},
          why={**_TEXT, "description": "Why it goes."}),
    _tool("agentos_script_list", "The user's public scripts (Python with Relay's SDK, on their Scripts page): names, "
          "and how each last ran. Their private ones aren't yours.", []),
    _tool("agentos_script_read", "A public script's text.", ["name"],
          name={**_TEXT, "description": "Its name, e.g. report.py."}),
    _tool("agentos_script_run", "Run a public script as the user (it may start agents of its own): how it ended and "
          "its output, when it's done within 30 s; else its run's id, for script_output.", ["name"],
          name={**_TEXT, "description": "Its name, e.g. report.py."},
          args={**_TEXT, "description": "Its arguments, as on a command line (optional)."}),
    _tool("agentos_script_output", "A public script's run: how it went (or that it still runs), what it started, its "
          "output's end.", ["run"], run={**_TEXT, "description": "The run's id (its first characters will do)."}),
    _tool("agentos_models_list", "The models available here (every provider's), with the score Relay keeps for each "
          "on the intelligence index and its rank (#1 the smartest), and those not looked up yet.", []),
    _tool("agentos_models_score", "Save what you found of models for AgentOS's ranking: per provider model, which "
          "model it is (name, as the benchmark names it) and that name's score on the index (leave the score out "
          "when it's kept already under that name, or when no benchmark knows it). Relay keeps them and ranks.",
          ["scores"], scores={"type": "array", "description": "One per provider model.", "items": {
              "type": "object", "required": ["provider", "model", "name"], "properties": {
                  "provider": {**_TEXT, "description": "Provider id (models_list shows them)."},
                  "model": {**_TEXT, "description": "Its model id there."},
                  "name": {**_TEXT, "description": "The model exactly as the index names it, e.g. Claude Opus 5.5: "
                           "nothing added (the same model reached another way gets the same name; the rest goes "
                           "in note)."},
                  "note": {**_TEXT, "description": "Anything else worth saying: a tier, a setting, how it was "
                           "placed."},
                  "score": {"type": "number", "description": "Its score on the index."},
                  "estimated": {"type": "boolean", "description": "Placed on the index's scale from other "
                                "benchmarks (it isn't on the index itself)."},
                  "router": {"type": "boolean", "description": "It's a router (Copilot's auto, say): another model per "
                             "request, so no score of its own; it isn't ranked."},
                  "source": {**_TEXT, "description": "Where the score comes from (a URL)."}}}},
          benchmark={**_TEXT, "description": "The index, if not the Artificial Analysis Intelligence Index."}),
]
# What every wake-up of the Governor's uses: listed for it from the start, not behind its program's tool search
# (Claude Code defers MCP tools until searched; `_meta` anthropic/alwaysLoad, which other clients pass by).
CORE_CAPABILITIES = ("task.list", "task.done", "task.create", "chat.create", "chat.send", "chat.read", "chat.list",
                     "chat.wait", "chat.tell", "agent.spawn", "agent.review", "notify.intervention", "notify.report",
                     "notify.update", "notify.resolve", "workbox.look")
for _core in AGENTOS_TOOLS:
    if _core["name"].removeprefix("agentos_").replace("_", ".") in CORE_CAPABILITIES:
        _core["_meta"] = {"anthropic/alwaysLoad": True}
COMPUTER_TOOLS = [
    _tool("computers", "The user's own computers: online or not, their system, user and home folder.", []),
    _tool("computer_run", "Run a shell command on one of the user's computers (cmd.exe on Windows; use "
          "powershell -Command for PowerShell); returns the exit code and output.", ["command"],
          computer={**_TEXT, "description": "The computer's name; default: this task's, or the only one."},
          command={**_TEXT, "description": "The command."},
          cwd={**_TEXT, "description": "Folder to run it in; default the home folder."},
          timeout={"type": "integer", "description": "Seconds, at most 600; default 120."}),
    _tool("computer_read", "Read a text file on one of the user's computers.", ["path"],
          computer=_TEXT, path={**_TEXT, "description": "The file; ~ is the home folder."}),
    _tool("computer_write", "Create or overwrite a text file on one of the user's computers.", ["path", "content"],
          computer=_TEXT, path={**_TEXT, "description": "The file; ~ is the home folder."},
          content={**_TEXT, "description": "The whole new content."}),
]


class ToolError(Exception):
    """A tool call that can't be done; the message goes back to the agent."""


@dataclass
class Subagent:
    id: str
    label: str
    who: str
    provider_id: str = ""
    model: str = ""
    failed: bool = False
    task: asyncio.Task | None = None
    answer: str | None = None  # set when it's done
    delivered: bool = False  # its answer went back to the agent
    done: asyncio.Event = field(default_factory=asyncio.Event)
    fork: forks.Fork | None = None  # its copy of the caller's folder (a code task)
    told: bool = False  # interrupted by tell: it goes on, its changes aren't handed over yet
    tried: list[tuple[str, str]] = field(default_factory=list)  # free models that failed it (FREE_FALLBACKS)


class Delegation:
    def __init__(self, relay: Relay) -> None:
        self.relay = relay
        self.run_tokens: dict[str, str] = {}  # token -> run id (Claude Code's permission prompts)
        # token -> (provider, folder): a conformance check's turn (relay.providers.conformance), approvals only
        self.checks: dict[str, tuple[Any, Path]] = {}
        self.subagents: dict[str, dict[str, Subagent]] = {}  # run id -> its subagents
        # conversation id -> answers its agent stopped waiting for (when, tool, text): its next call has them
        self.missed: dict[str, list[tuple[float, str, str]]] = {}
        self._busy = 0  # subagents working now
        self._room: asyncio.Condition | None = None
        self._placing = asyncio.Lock()  # (see start: _local_room)

    # Tokens and the script

    def token_for(self, run_id: str) -> str:
        token = secrets.token_urlsafe(32)
        self.run_tokens[token] = run_id
        return token

    def forget(self, token: str) -> None:
        self.run_tokens.pop(token, None)
        self.checks.pop(token, None)

    def check_token(self, provider: Any, folder: Path) -> str:
        """A conformance check's turn: its permission requests answered as a run's are (the provider's judge()),
        and nothing else (no conversation: no Relay tools). forget() it after."""
        token = secrets.token_urlsafe(32)
        self.checks[token] = (provider, Path(folder))
        return token

    def run_for(self, token: str) -> ActiveRun | None:
        """The run a token belongs to: a run's own, or its conversation's tools token."""
        run_id = self.run_tokens.get(token)
        if run_id is None:
            row = self.relay.db.one("SELECT id FROM conversations WHERE tools_token = ?", (token,))
            run_id = self.relay.runs.active_run_for(row["id"]) if row else None
        with self.relay.runs.lock:
            return self.relay.runs.active.get(run_id) if run_id else None

    def script_for(self, conversation: dict[str, Any], url: str) -> Path:
        """The conversation's `relay` script, (re)written to reach Relay at `url`."""
        token = conversation.get("tools_token")
        if not token:
            token = secrets.token_urlsafe(32)
            self.relay.db.execute("UPDATE conversations SET tools_token = ? WHERE id = ?",
                                  (token, conversation["id"]))
            conversation["tools_token"] = token
        folder = tools_dir() / token
        path = folder / "relay"
        text = SCRIPT.replace("@PYTHON@", sys.executable).replace("@URL@", f"{url}/mcp/{token}")
        if not path.exists() or path.read_text() != text:
            folder.mkdir(parents=True, exist_ok=True)
            folder.chmod(0o755)
            if path.exists():
                path.chmod(0o755)
            path.write_text(text)
            path.chmod(stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP)  # read-only
            folder.chmod(0o555)
        os.utime(folder)  # in use: prune_scripts() leaves it alone
        return path

    def prune_scripts(self, older_than: float = PRUNE_AFTER) -> int:
        """Remove tools scripts of conversations that are gone. The machine's cache may
        be shared with other Relays (another RELAY_HOME, a test run), so only folders
        this Relay doesn't know and nobody used for `older_than` seconds go."""
        folder = tools_dir()
        if not folder.is_dir():
            return 0
        known = {r["tools_token"] for r in self.relay.db.all(
            "SELECT tools_token FROM conversations WHERE tools_token IS NOT NULL")}
        cutoff = time.time() - older_than
        removed = 0
        for entry in folder.iterdir():
            try:
                if entry.is_dir() and entry.name not in known and entry.stat().st_mtime < cutoff:
                    self.remove_script(entry.name)
                    removed += 1
            except OSError:
                continue
        return removed

    def remove_script(self, token: str | None) -> None:
        if not token:
            return
        folder = tools_dir() / token
        if folder.exists():
            folder.chmod(0o755)
            for child in folder.iterdir():
                child.chmod(0o644)
                child.unlink()
            folder.rmdir()

    # JSON-RPC (the script, and MCP for Claude Code's permission prompts)

    async def handle(self, token: str, message: Any) -> dict[str, Any] | None:
        """Answer one JSON-RPC message; None for notifications."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, -32600, "Invalid request")
        request_id, method = message.get("id"), message.get("method")
        if request_id is None:
            return None  # a notification, e.g. notifications/initialized
        params = message.get("params") or {}
        if method == "initialize":
            return _result(request_id, {
                "protocolVersion": params.get("protocolVersion") or PROTOCOL,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "relay", "version": __version__},
            })
        if method == "ping":
            return _result(request_id, {})
        if token in self.checks:  # (a conformance check's turn: approvals only)
            if method == "tools/list":
                return _result(request_id, {"tools": [APPROVE]})
            arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            if method == "tools/call" and params.get("name") == "approve_permission":
                provider, folder = self.checks[token]
                tool_input = arguments.get("input") if isinstance(arguments.get("input"), dict) else {}
                verdict, why = await asyncio.to_thread(provider.judge, str(arguments.get("tool_name") or "tool"),
                                                       tool_input, folder)
                return _result(request_id, _text(json.dumps(
                    {"behavior": "allow", "updatedInput": tool_input} if verdict == "allow"
                    else {"behavior": "deny", "message": why or "Relay didn't allow this."})))
            if method == "tools/call":
                return _result(request_id, _text("A check's turn has no Relay tools.", error=True))
        if method == "tools/list":
            return _result(request_id, {"tools": self._tools_for(token)})
        if method == "tools/call":
            run = self.run_for(token)
            if run is None:
                return _result(request_id, _text("No run of this conversation is going right now.", error=True))
            name, arguments = params.get("name"), params.get("arguments") or {}
            if not isinstance(arguments, dict) or _nul(arguments):
                # (no path, name or command takes a NUL: it failed where it went, a fuzz of the tools found)
                return _result(request_id, _text("Its arguments are an object, without NUL characters (\\u0000).",
                                                 error=True))
            if name not in self.allowed(run):
                # Only what this run was offered: a token never reaches a tool its run doesn't have
                # (a Governor's no delegate, computer_run or commit; a subagent's only approve_permission).
                self.relay.agentos.record_refusal(run, name)
                if name != "approve_permission" and self.relay.db.one(
                        "SELECT 1 FROM conversations WHERE id = ? AND parent_run IS NOT NULL", (run.conversation_id,)):
                    return _result(request_id,
                                   _text("A subagent gets no Relay tools of its own (but search and fetch).",
                                                     error=True))
                return _result(request_id, _text(f"Tool {name!r} isn't one of this conversation's.", error=True))
            try:
                if isinstance(name, str) and name.startswith("agentos_"):
                    text = await self._agentos(run, name, arguments)
                elif name == "help":
                    text = await asyncio.to_thread(self.help, run, arguments.get("topic"))
                elif name == "search":
                    text = await asyncio.to_thread(self.relay.search.text, arguments)
                elif name == "fetch":
                    text = await asyncio.to_thread(self.relay.fetch.text, arguments)
                elif name == "agents":
                    text = await asyncio.to_thread(self.list_agents, run)
                elif name == "delegate":
                    text = await self.start(run, arguments)
                    started = re.search(r"Started subagent (\w+)", text)
                    if arguments.get("wait") and started:  # one call: start it and wait for the answer
                        text = await self.wait(run, {"ids": [started.group(1)]})
                elif name == "wait":
                    text = await self.wait(run, arguments)
                elif name == "tell":
                    text = await self.tell(run, arguments)
                elif name == "merge":
                    text = await self.merge(run, arguments)
                elif name == "approve_permission":
                    text = await self.approve(run, arguments)
                elif name in ("changes", "diff", "restore", "commit"):
                    text = await asyncio.to_thread(self.on_task, run, name, arguments)
                elif name == "computers":
                    text = self.list_computers(run)
                elif name in ("computer_run", "computer_read", "computer_write"):
                    text = await self.on_computer(run, name.removeprefix("computer_"), arguments)
                elif name in ("memory_add", "memory_change", "memory_forget", "memory_list"):
                    text = await asyncio.to_thread(self.on_memory, run, name, arguments)
                elif name in ("repair_check", "repair_run", "provider_plan"):
                    text = await asyncio.to_thread(self.on_repair, run, name, arguments)
                else:  # a plugin's (relay.plugins: in its own threads)
                    conversation = self._conversation(run)
                    text = await self.relay.plugins.use_tool(name, ToolCall(
                        run.conversation_id, run.id, conversation["user_id"], conversation["directory"], arguments))
            except (ToolError, ComputerError, SearchError, FetchError, PluginError) as exc:
                return _result(request_id, _text(str(exc) + self._news(run, name), error=True))
            except Exception as exc:  # noqa: BLE001 - the agent hears about it, instead of an HTTP 500
                print(f"relay: tool {name!r} failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
                return _result(request_id, _text(f"Relay couldn't do that: {type(exc).__name__}: {exc}", error=True))
            return _result(request_id, _text(text + self._news(run, name)))
        return _error(request_id, -32601, f"Method not found: {method}")

    def _news(self, run: ActiveRun, tool: str) -> str:
        """Subagents that failed since the agent last heard, so it needn't wait to find out; and answers
        it stopped waiting for."""
        if tool == "approve_permission":
            return ""
        failed = [s for s in self.subagents.get(run.id, {}).values() if s.done.is_set() and s.failed
                  and not s.delivered]
        for s in failed:
            s.delivered = True
        missed = self.missed.pop(run.conversation_id, [])
        return "".join(f"\n\nMeanwhile, subagent {s.id[:8]}{f' ({s.label})' if s.label else ''} ended: {s.answer}"
                       for s in failed) + "".join(
            f"\n\nYour {_called(name)} call of "
            f"{time.strftime('%H:%M:%S', time.localtime(at))} was stopped before Relay "
            "answered (a time-out of your command, say: give such a call a minute or "
            f"more). It was done all the same; Relay's answer:\n{text}" for at, name, text in missed)

    def left(self, token: str, messages: list[Any], answers: list[dict[str, Any]]) -> None:
        """The caller was gone before these answers came (its shell's time-out stopped `relay`, say): what
        was done (an agent started, a command run) isn't lost, its next call has them."""
        run = self.run_for(token)
        if run is None:
            return
        called = {m.get("id"): (m.get("params") or {}).get("name") for m in messages
                  if isinstance(m, dict) and m.get("method") == "tools/call"}
        for answer in answers:
            name = called.get(answer.get("id"))
            text = "".join(part.get("text", "") for part in (answer.get("result") or {}).get("content") or [])
            if isinstance(name, str) and name != "approve_permission" and text:
                kept = self.missed.setdefault(run.conversation_id, [])
                kept[:] = [*kept, (time.time(), name, text)][-MISSED:]

    # Tools

    async def _agentos(self, run: ActiveRun, name: str, arguments: dict[str, Any]) -> str:
        """A capability of AgentOS's Governor (relay.agentos)."""
        from relay.agentos import AgentOSError, capability_of
        capability = capability_of(name)
        if capability is None:
            raise ToolError(f"Unknown tool {name!r}")
        if not self.relay.agentos.is_governor(run.conversation_id):
            raise ToolError("Only AgentOS's Governor has AgentOS's capabilities")
        try:
            return await self.relay.agentos.handle(run, capability, arguments)
        except AgentOSError as exc:
            raise ToolError(str(exc)) from exc

    def help(self, run: ActiveRun, topic: str | None) -> str:
        """`relay help [TOPIC]`: what the agent's note leaves out, as it is for this conversation now."""
        from relay import skills
        conversation = self._conversation(run)
        directory = Path(conversation["directory"])
        top = git.repo_root(directory) if directory.is_dir() else None
        repository = str(top) if top and top.resolve() != directory.resolve() else None
        computers = [r["name"] for r in self.relay.db.all(
            "SELECT name FROM computers WHERE user_id = ? ORDER BY name", (conversation["user_id"],))]
        plugins = {t["name"] for t in self.relay.plugins.tools()}  # (those this run has: below)
        return help_text(topic if isinstance(topic, str) else None, computers=computers,
                         code=conversation["mode"] == "code", repository=repository,
                         skills_text=skills.note(skills.all_skills()), search=self.relay.search.enabled(),
                         plugins=[t for t in self._tools_of(run) if t["name"] in plugins])

    def list_agents(self, run: ActiveRun) -> str:
        conversation = self._conversation(run)
        rules = policy.load(self.relay.db, conversation["user_id"])
        free = policy.describe(policy.allowed(self.relay.db, self.relay.registry, rules, conversation))
        others = ("asked of the user, who may refuse or choose another; without an answer in "
                  f"{policy.ask_timeout()} s an allowed one is used instead" if rules["others"] == "ask"
                  else "refused")
        failing = {k: v for k, v in health.statuses(self.relay.db).items()
                   if not v["ok"] and time.time() - (v["checked"] or 0) < FAILED_LATELY}
        ranked = ranking.ranking(self.relay.db)
        lines = [f"Allowed without asking the user: {free}. Any other provider or model is {others}."
                 + ("" if rules["local"] else " Subagents on local models (a local server, e.g. Ollama) are "
                    "turned off by the user."), "",
                 *([f"Intelligence ranks: AgentOS's ranking of the models here, from benchmarks ({ranked['benchmark']}"
                    f"): #1 is the smartest of {ranked['of']}. A hard task wants a model near the top; an easy one "
                    "does fine lower down.", ""] if ranked["of"] else []),
                 "Providers (use the id with --provider):"]
        listed: dict[tuple, str] = {}  # models and efforts -> the provider that showed them first
        for template in self.relay.registry.templates():
            if not template.enabled:
                continue
            try:
                provider = self.relay.registry.provider(template.id)
            except TemplateError:
                continue
            code = conversation["mode"] == "code"
            # What a subagent there can do (none get Relay's own tools: one level deep).
            kind = ("agent with its own tools: shell, files, web (commands in the folder's container)"
                    if provider.TOOLS else
                    ("chat model with Relay's tools: files, run_command (in the container), web_fetch" if code
                     else "chat model with Relay's tools: web_fetch, skills; no files outside a code task")
                    if safely(provider.runs_tools, False)
                    else "chat model without tools: it only answers from the task")
            you = " (you)" if template.id == conversation["provider_id"] else ""
            lines.append(f"- {template.id}{you}: {template.name}, {kind}")
            # (a provider folder's mistake leaves its line short, not the list)
            models = [m["id"] for m in safely(provider.models, []) if m["id"]]
            per_model = safely(provider.model_efforts, None)
            levels = safely(provider.efforts, [], None)
            # Efforts per model where they differ by model: "a (low, high)"; without, it has none.
            shown = [f"{m} ({', '.join(per_model[m])})" if per_model and per_model.get(m) else m for m in models]
            same = listed.setdefault((tuple(shown), tuple(levels)), template.id)
            if same != template.id and models:
                lines.append(f"  same models and efforts as {same}")
            else:
                if models:
                    lines.append(f"  models (--model{'; its --effort levels in brackets' if per_model else ''}): "
                                 f"{', '.join(shown[:40])}"
                                 + (" …" if len(models) > 40 else ""))
                if levels:
                    lines.append(f"  efforts (--effort){' of its default model' if per_model else ''}: "
                                 f"{', '.join(levels)}")
                ranks = sorted((rank, m) for m in models if (rank := ranking.rank_of(ranked, template.id, m)))
                if ranks:
                    lines.append(f"  intelligence ranks: {', '.join(f'{m} #{rank}' for rank, m in ranks)}")
                routing = ranked["routing"].get(template.id)
                if routing:
                    lines.append(f"  routers (another model per request, so not ranked): {', '.join(routing)}")
            # (a model it doesn't offer, say a name mistyped once, isn't one to warn of for good)
            broken = [f"{v['model'] or 'its default model'} ({v['message']})"
                      for k, v in failing.items() if v["provider_id"] == template.id
                      and (not v["model"] or v["model"] in models)]
            if broken:
                lines.append(f"  failed on their last run, better not use: {'; '.join(broken)}")
        if conversation.get("agentos_role") == "governor":
            own = self.relay.agentos._own(conversation["user_id"])
            if own:
                lines.append(f"\nYour own model ({own[0]} · {own[1] or 'its default'}) is always yours for agents "
                             "(chat.create without a provider): research, reviews and other judgment work go to an "
                             "agent on it, not to you.")
        lines.append("\nModes (use the id with --mode):")
        for mode in Modes(self.relay.db).all(conversation["user_id"]):
            access = "read-only" if mode.access == "read" else "may change files"
            lines.append(f"- {mode.id}: {mode.name}, {access}" + (f". {mode.description}" if mode.description else ""))
        lines.append(f"\nAt most {tunables.get('subagents.parallel')} subagents work at once, "
                     f"{tunables.get('subagents.per_answer')} per answer, each for at most "
                     f"{tunables.get('subagents.timeout')} minutes.")
        limit = tunables.get("subagents.local_models")
        lines.append(f"Models on a local server (e.g. Ollama) run {limit} at a time per server: a subagent there "
                     "gets the model that's working there already, or one elsewhere.")
        return "\n".join(lines)

    async def start(self, run: ActiveRun, arguments: dict[str, Any]) -> str:
        """Start a subagent; its answer comes with wait()."""
        db = self.relay.db
        parent = self._conversation(run)
        task = str(arguments.get("task") or "").strip()
        if not task:
            raise ToolError("The task is empty: say what the subagent should do (on stdin)")
        provider_id = str(arguments.get("provider") or parent["provider_id"])
        try:
            template = self.relay.registry.get(provider_id)
        except TemplateError as exc:
            there = ", ".join(t.id for t in self.relay.registry.templates() if t.enabled)
            raise ToolError(f"No provider {provider_id!r}. The providers are: {there}") from exc
        if not template.enabled:
            raise ToolError(f"Provider {provider_id!r} is turned off")
        model = arguments.get("model")
        if model is None and provider_id == parent["provider_id"]:
            model = parent["model"]
        mode_id = arguments.get("mode") or None
        modes = Modes(db)
        if mode_id and modes.get(parent["user_id"], str(mode_id)) is None:
            there = ", ".join(m.id for m in modes.all(parent["user_id"]))
            raise ToolError(f"No mode {mode_id!r}. The modes are: {there}")
        # A read-only caller's subagents are read-only too, else Plan could change files through
        # them; plainly so, without Plan's job of making a plan: their task says what to do.
        wanted = modes.get(parent["user_id"], mode_id) if mode_id else None
        if self._read_only(parent) and (wanted is None or wanted.access != "read"):
            mode_id = "readonly"
        if run.closing or run.stop_requested:
            raise ToolError("The run is ending; no new subagents")
        if run.delegations >= tunables.get("subagents.per_answer"):
            raise ToolError(f"This run has used its {tunables.get('subagents.per_answer')} subagents; "
                            "finish with what you have")
        provider_id, model, note = await self._permitted(run, parent, provider_id, model, mode_id, task)
        template = self.relay.registry.get(provider_id)
        # Checking the local servers' room and taking it go together, so two delegates at once
        # can't both find room for one more model.
        async with self._placing:
            await self._local_room(run, provider_id, model)
            effort, effort_note = await asyncio.to_thread(self._effort, parent, provider_id, model,
                                                          arguments.get("effort"))
            if run.closing or run.stop_requested:
                raise ToolError("The run is ending; no new subagents")
            if run.delegations >= tunables.get("subagents.per_answer"):
                raise ToolError(f"This run has used its {tunables.get('subagents.per_answer')} subagents; "
                                "finish with what you have")
            run.delegations += 1

            child_id = uuid.uuid4().hex
            label = " ".join(str(arguments.get("label") or "").split())[:60]
            directory = (parent["directory"] if parent["mode"] == "code"
                         else str(self.relay.settings.chat_directory() / child_id))
            now = time.time()
            db.execute(
                "INSERT INTO conversations (id, user_id, title, title_auto, provider_id, model, mode, "
                "directory, agent_mode, effort, parent_run, parent_conversation, created, updated) "
                "VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (child_id, parent["user_id"], (label or f"Subagent: {task}")[:200], provider_id,
                 model or None, parent["mode"], directory, None if mode_id == "build" else mode_id,
                 effort, run.id, parent["id"], now, now))
            card = {"type": "subagent", "id": child_id, "label": label, "task": task[:2000],
                    "provider": provider_id, "model": model or None, "mode": mode_id, "run": None,
                    "status": "waiting"}
            run.add(dict(card))
            detail = ", ".join(filter(None,
                                      [f"{mode_id} mode" if mode_id else "", f"effort {effort}" if effort else ""]))
            who = template.name + (f" · {model}" if model else "") + (f" ({detail})" if detail else "")
            subagent = Subagent(child_id, label, who, provider_id, model or "")
            self.subagents.setdefault(run.id, {})[child_id] = subagent
        where = ""
        if parent["mode"] == "code" and not self._read_only({"user_id": parent["user_id"],
                                                             "agent_mode": None if mode_id == "build" else mode_id}):
            # It may change files: in a copy of the caller's folder, so neither gets in the other's way.
            try:
                subagent.fork = await asyncio.to_thread(forks.make, parent["id"], Path(parent["directory"]), child_id)
            except forks.ForkError as exc:
                where = f" It works in your folder itself ({exc})."
            except BaseException:  # the call was cancelled: it never starts, and nobody waits for it
                subagent.answer, subagent.delivered = "It didn't start.", True
                subagent.done.set()
                raise
            else:
                fork = subagent.fork
                if self.relay.containers is not None and fork.links:
                    self.relay.containers.mount_also(fork.folder, [fork.source / link for link in fork.links])
                db.execute("UPDATE conversations SET directory = ? WHERE id = ?", (str(fork.folder), child_id))
                task += FORK_NOTE
                where = " It works in a copy of your folder; its changes come into yours when it's done."
        subagent.task = asyncio.create_task(self._supervise(run, card, task, subagent))
        short = child_id[:8]
        named = f"{short}{f' ({label})' if label else ''}"
        if not await self._gets_going(card, subagent) and subagent.failed:
            subagent.delivered = True
            return f"{note}Subagent {named} failed right away: {subagent.answer}"
        return f"{note}Started subagent {named}: {who}.{effort_note}{where} Get its answer with: wait {short}"

    async def _gets_going(self, card: dict[str, Any], subagent: Subagent) -> bool:
        """Wait until the subagent works (thinks, writes or uses a tool), up to EARLY seconds;
        False when it ended before that (done already, or failed: its model is unavailable, say)."""
        started = time.monotonic()
        while time.monotonic() - started < EARLY:
            if subagent.done.is_set():
                return False
            if card.get("run") is None:
                if time.monotonic() - started > 1:
                    return True  # waiting for its turn (subagents.parallel)
            else:
                with self.relay.runs.lock:
                    child = self.relay.runs.active.get(card["run"])
                if child and any(e.get("type") in ("text", "reasoning", "tool_call") for e in list(child.events)):
                    return True
            await asyncio.sleep(0.2)
        return not subagent.done.is_set()

    def _effort(self, parent: dict[str, Any], provider_id: str, model: str | None,
                wanted: Any) -> tuple[str | None, str]:
        """A subagent's effort, and a note for the agent when its choice can't be used."""
        try:
            levels = self.relay.registry.provider(provider_id).efforts(model)
        except Exception:  # noqa: BLE001 - removed meanwhile, or its service is down: no efforts
            levels = []
        wanted = str(wanted or "").strip().lower()
        if wanted and wanted != "default":
            if wanted in levels:
                return wanted, ""
            return None, (f" It has no effort {wanted!r} ({', '.join(levels) or 'no efforts at all'}), "
                          "so it uses its default.")
        if not wanted and parent.get("agent_mode") == "fast" and levels:
            return levels[0], ""  # Fast: subagents think as little as their model allows
        return None, ""

    async def _local_room(self, run: ActiveRun, provider_id: str, model: str | None) -> None:
        """Refuse a subagent on a local server (e.g. Ollama) that already has as many models
        working as the user allows (subagents.local_models), with which ones to use instead."""
        server = await asyncio.to_thread(self._server, provider_id, model)
        if not server:
            return
        with self.relay.runs.lock:
            ids = list(self.relay.runs.active)
        working = {(r["provider_id"], r["model"]) for r in self.relay.db.all(
            f"SELECT provider_id, COALESCE(model, '') AS model FROM runs WHERE id IN ({', '.join('?' * len(ids))})",
            tuple(ids))} if ids else set()
        working |= {(s.provider_id, s.model) for mine in self.subagents.values() for s in mine.values()
                    if not s.done.is_set()}
        there = set()
        for other in working:
            if await asyncio.to_thread(self._server, *other) == server:
                there.add(other)
        limit = tunables.get("subagents.local_models")
        if (provider_id, model or "") in there or len(there) < limit:
            return
        names = ", ".join(f"provider {p}, model {m or 'its default'}" for p, m in sorted(there))
        raise ToolError(f"{server} runs local models, and the user lets it run {limit} at a time "
                        f"(more makes everything very slow). Working there now: {names}. Give this subagent "
                        f"{'that' if len(there) == 1 else 'one of those'}, or a model that doesn't run on {server}.")

    def _server(self, provider_id: str, model: str | None) -> str | None:
        try:
            return self.relay.registry.provider(provider_id).local_server(model or None)
        except Exception:  # noqa: BLE001 - removed meanwhile, or its service is down: not known as local
            return None

    # A code task's record of its turns (relay.tasks)

    def on_task(self, run: ActiveRun, name: str, arguments: dict[str, Any]) -> str:
        row = self._conversation(run)
        if row["mode"] != "code" or row.get("task_state") != "open":
            raise ToolError("This isn't an open code task: there are no files of its own to follow")
        turns = self.relay.db.all(
            "SELECT r.id, r.created, r.status, r.checkpoint, r.after_checkpoint, r.changes, "
            "(SELECT content FROM messages WHERE conversation_id = r.conversation_id AND role = 'user' "
            # (the turn going on has no answer yet: the last message is its prompt)
            "AND id < COALESCE((SELECT id FROM messages WHERE run_id = r.id), 9e18) "
            "ORDER BY id DESC LIMIT 1) AS prompt FROM runs r "
            "WHERE r.conversation_id = ? AND r.checkpoint IS NOT NULL ORDER BY r.created",
            (row["id"],))

        def pick(value: Any) -> dict[str, Any]:
            text = str(value or "").strip()
            if not turns:
                raise ToolError("No turns are recorded for this task yet")
            if not text:
                return dict(turns[-1])
            if not text.isdigit() or not 1 <= int(text) <= len(turns):
                raise ToolError(f"There are {len(turns)} turns; say one of 1–{len(turns)} (see changes)")
            return dict(turns[int(text) - 1])

        try:
            if name == "changes":
                if not turns:
                    return "No turns are recorded for this task yet."
                lines = []
                for number, turn in enumerate(turns, 1):
                    summary = json.loads(turn["changes"]) if turn["changes"] else None
                    what = (f"{summary['files']} files, +{summary['additions']} −{summary['deletions']}"
                            if summary else ("going on now" if turn["id"] == run.id else "no record"))
                    asked = " ".join((turn["prompt"] or "").split())[:70]
                    lines.append(f"{number}. {time.strftime('%Y-%m-%d %H:%M', time.localtime(turn['created']))} "
                                 f"“{asked}”: {what}")
                total = tasks.summary(row)
                if total:
                    lines.append(f"Since the task started: {total['files']} files, +{total['additions']} "
                                 f"−{total['deletions']} (relay diff all).")
                return "\n".join(lines)
            if name == "diff":
                if str(arguments.get("turn") or "").strip() == "all":
                    if not row["base_commit"]:
                        raise ToolError("This task has no starting point recorded")
                    return tasks.turn_diff(row, row["base_commit"], None)
                turn = pick(arguments.get("turn"))
                after = turn["after_checkpoint"] if turn["id"] != run.id else None
                return tasks.turn_diff(row, turn["checkpoint"], after)
            if name == "restore":
                wanted = str(arguments.get("turn") or "").strip()
                target = pick(wanted)["checkpoint"] if wanted.isdigit() else wanted
                if not target:
                    raise ToolError("Say which turn to restore the files to (the files before it)")
                undo = self.relay.runs.project_operation(row, tasks.rewind, target, f"turn {wanted}",
                                                         force=True, allow_run_id=run.id)
                return (f"The files are as they were before turn {wanted}." if wanted.isdigit() else
                        "The files are back at that point.") + (f" To undo this: restore {undo}." if undo else "")
            made = self.relay.runs.project_operation(row, tasks.commit, str(arguments.get("message") or ""),
                                                     repository=tasks.scope(row) is None, allow_run_id=run.id)
            return f"Committed {made[:10]} to the user's repository."
        except (tasks.TaskError, RunError) as exc:
            raise ToolError(str(exc)) from exc

    # The user's own computers (relay.computers)

    def list_computers(self, run: ActiveRun) -> str:
        conversation = self._conversation(run)
        mine = self.relay.computers.for_user(conversation["user_id"])
        if not mine:
            return "The user has no computers connected to Relay."
        lines = []
        for c in mine:
            info = c["info"] or {}
            system = " ".join(filter(None,
                                     [info.get("os"), info.get("release"), info.get("machine")])) or "system unknown"
            seen = c.get("last_seen")
            state = "online" if c["online"] else (
                "OFFLINE: Relay's connector there isn't running"
                + (f" (last seen {time.strftime('%Y-%m-%d %H:%M', time.localtime(seen))})" if seen
                   else " (never connected)"))
            lines.append(f"- {c['name']}: {state}; {system}; user {info.get('user', '?')}, "
                         f"home {info.get('home', '?')}, shell {info.get('shell', '?')}"
                         + ("; the user is asked before each command or file change" if c["ask"] else ""))
        offline = [c["name"] for c in mine if not c["online"]]
        if offline:
            lines.append("(Only those computers' connectors are offline: Relay itself and your own container work "
                         "as usual. Ask the user to start the connector there if you need it.)")
        return "\n".join(lines)

    async def on_computer(self, run: ActiveRun, kind: str, arguments: dict[str, Any]) -> str:
        """A command, or a file read or written, on one of the user's computers."""
        conversation = self._conversation(run)
        wanted = arguments.get("computer")
        if not wanted and conversation.get("computer_id"):  # a computer task: its own computer
            own = self.relay.db.one("SELECT name FROM computers WHERE id = ?", (conversation["computer_id"],))
            if own is None:
                raise ToolError("This task's computer was removed from Relay")
            wanted = own["name"]
        computer = self.relay.computers.by_name(conversation["user_id"], wanted)
        if kind == "run":
            command = str(arguments.get("command") or "").strip()
            argv = arguments.get("argv")
            if isinstance(argv, list) and argv:
                # One word is a whole command line; several are quoted for the computer's shell.
                windows = (computer["info"] or {}).get("os") == "Windows"
                command = (str(argv[0]) if len(argv) == 1 else
                           subprocess.list2cmdline([str(a) for a in argv]) if windows
                           else shlex.join(str(a) for a in argv))
            if not command:
                raise ToolError("Say which command to run")
            refused = guard.check(command)
            if refused:
                raise ToolError(f"Relay blocked this command on {computer['name']}: {refused}")
            try:
                timeout = max(1, min(int(arguments.get("timeout") or DEFAULT_TIMEOUT), MAX_TIMEOUT))
            except (TypeError, ValueError):
                timeout = DEFAULT_TIMEOUT
            job = {"command": command, "cwd": arguments.get("cwd") or None, "timeout": timeout}
            what = command
        else:
            path = str(arguments.get("path") or "").strip()
            if not path:
                raise ToolError("Say which file")
            job, timeout, what = {"path": path}, 120, path
            if kind == "write":
                if arguments.get("content_b64") is not None:
                    job["content_b64"] = str(arguments["content_b64"])
                else:
                    job["content_b64"] = base64.b64encode(str(arguments.get("content") or "").encode()).decode()
                what = f"write {path} ({len(base64.b64decode(job['content_b64']))} bytes)"
        if computer["ask"] and kind != "read" and computer["name"] not in run.computers_allowed:
            await self._ask_computer(run, computer, kind, what)
        result = await self.relay.computers.call(computer, kind, job, timeout)
        if not result.get("ok"):
            output = result.get("output")
            raise ToolError(f"On {computer['name']}: {result.get('error') or 'it failed'}"
                            + (f"\n{output}" if output else ""))
        if kind == "run":
            return f"Exit code {result.get('exit')}\n{result.get('output', '')}".rstrip()
        if kind == "write":
            return f"Wrote {result.get('path')} ({result.get('size')} bytes) on {computer['name']}"
        data = base64.b64decode(result.get("content_b64") or "")
        if arguments.get("encoding") == "base64":
            return result.get("content_b64") or ""
        text = data.decode("utf-8", "replace")
        return text if len(text) <= MAX_ANSWER else text[:MAX_ANSWER] + "\n[… cut off]"

    async def _ask_computer(self, run: ActiveRun, computer: dict[str, Any], kind: str, what: str) -> None:
        name = computer["name"]
        event = {"type": "permission", "id": uuid.uuid4().hex,
                 "action": f"{'Command' if kind == 'run' else 'File change'} on {name}", "resources": [what],
                 "decision": None, "expires": time.time() + tunables.get("computers.ask_timeout"),
                 "options": [{"id": "once", "label": "Allow", "outcome": "allowed"},
                             {"id": "answer", "label": f"Allow everything on {name} for this answer",
                              "outcome": f"allowed, and everything on {name} for this answer"},
                             {"id": "denied", "label": "Deny", "outcome": "denied"}]}
        choice = await self._choose(run, event)
        if choice == "answer":
            run.computers_allowed.add(name)
        elif choice != "once":
            late = " in time" if choice == "timeout" else ""
            raise ToolError(f"The user didn't allow this on {name}{late}. Don't try it another way; "
                            "ask the user in your answer if it's needed.")

    async def _permitted(self, run: ActiveRun, parent: dict[str, Any], provider_id: str, model: str | None,
                         mode_id: str | None, task: str) -> tuple[str, str | None, str]:
        """The provider and model to use, as the user's rules allow (relay.policy): the ones
        asked for, or the ones chosen instead, with a note saying so. ToolError if refused."""
        db, registry = self.relay.db, self.relay.registry
        rules = policy.load(db, parent["user_id"])
        if not rules["local"]:
            server = await asyncio.to_thread(self._server, provider_id, model)
            if server:
                entries = await asyncio.to_thread(policy.allowed, db, registry, rules, parent)
                raise ToolError(f"The user has turned subagents on local models off (Settings → Subagents), and "
                                f"this one runs on {server}. Choose a model elsewhere"
                                + (f"; allowed without asking: {policy.describe(entries)}." if entries else "."))
        paid_by = await asyncio.to_thread(policy.billing, db, registry, provider_id, model)
        judged = policy.verdict(rules, parent, provider_id, model, paid_by)
        if judged == "allow":
            return provider_id, model, ""
        entries = await asyncio.to_thread(policy.allowed, db, registry, rules, parent)
        who = registry.get(provider_id).name + (f" · {model}" if model else "")
        allowed_text = (f"Subagents allowed without asking: {policy.describe(entries)}." if entries
                        else "No subagents are allowed without asking; do the work yourself.")
        if judged == "refuse":
            raise ToolError(f"A subagent on {who} isn't allowed: the user only allows some subagents "
                            f"without asking. {allowed_text}")
        other = policy.alternative(entries, provider_id)
        other_name = None
        if other:
            try:
                other_name = registry.get(other[0]).name + (f" · {other[1]}" if other[1] else "")
            except Exception:  # noqa: BLE001 - removed meanwhile: offer none
                other = None
        options = [{"id": "once", "label": "Allow", "outcome": "allowed"},
                   {"id": "always", "label": "Always allow this model",
                    "outcome": "allowed, from now on without asking"}]
        if other:
            options.append({"id": "alt", "label": f"Use {other_name} instead", "outcome": f"{other_name} used instead"})
        options.append({"id": "denied", "label": "Deny", "outcome": "denied"})
        why = (f"Cost: {PAID_BY.get(paid_by or '', 'not known')}. "
               "It's not on your list of subagents allowed without asking.")
        event = {"type": "permission", "id": uuid.uuid4().hex, "action": f"Subagent on {who}",
                 "resources": [f"Task: {task[:600]}" + ("…" if len(task) > 600 else ""),
                               *( [f"Mode: {mode_id}"] if mode_id else []), why],
                 "decision": None, "options": options, "expires": time.time() + policy.ask_timeout()}
        choice = await self._choose(run, event)
        if choice == "always":
            policy.allow_always(db, parent["user_id"], provider_id, model)
        elif choice == "alt" and other:
            return other[0], other[1] or None, (f"The user didn't allow {who}; they chose {other_name} "
                                                f"instead, so that's what this subagent runs on. ")
        elif choice == "timeout" and other:
            return other[0], other[1] or None, (f"The user didn't answer about {who} in time, so this subagent "
                                                f"runs on {other_name} instead (allowed without asking). ")
        elif choice != "once":
            late = " (no answer in time)" if choice == "timeout" else ""
            raise ToolError(f"The user didn't allow a subagent on {who}{late}. {allowed_text}")
        return provider_id, model, ""

    async def _choose(self, run: ActiveRun, event: dict[str, Any]) -> str:
        """Put a question with options to the user; the chosen id, or "timeout"."""
        loop = asyncio.get_running_loop()
        answer: asyncio.Future = loop.create_future()

        def respond(choice: str) -> None:
            loop.call_soon_threadsafe(lambda: answer.done() or answer.set_result(choice))

        self.relay.runs.ask(run, event, respond)
        while not answer.done():
            if run.stop_requested or run.finished or run.closing:
                self.relay.runs.withdraw(run, event["id"], "denied", "the run ended")
                return "denied"
            if time.time() > event["expires"]:
                self.relay.runs.withdraw(run, event["id"], "timeout", "no answer in time, so denied")
                return "timeout"
            await asyncio.wait([answer], timeout=0.5)
        return answer.result()

    async def wait(self, run: ActiveRun, arguments: dict[str, Any]) -> str:
        """The answers of finished subagents, waiting a while (subagents.wait) for them."""
        mine = self.subagents.get(run.id, {})
        ids = arguments.get("ids") or []
        wanted = [str(i) for i in (ids if isinstance(ids, list) else [ids])]
        if wanted:
            chosen = [s for s in mine.values() if any(s.id.startswith(w) for w in wanted if w)]
            if not chosen:
                raise ToolError(f"No subagent {', '.join(wanted)} in this run")
        else:
            chosen = [s for s in mine.values() if not s.delivered]
            if not chosen:
                return "No subagents to wait for."
        try:
            longest = tunables.get("subagents.wait")
            timeout = max(0.0, min(float(arguments.get("timeout") or longest), longest))
        except (TypeError, ValueError):
            timeout = tunables.get("subagents.wait")
        pending = [s.done.wait() for s in chosen if not s.done.is_set()]
        if pending:
            await asyncio.wait([asyncio.ensure_future(p) for p in pending], timeout=timeout)
        parts, running = [], []
        for s in chosen:
            if s.done.is_set():
                s.delivered = True
                parts.append(f"=== Subagent {s.id[:8]}{f' ({s.label})' if s.label else ''} ===\n{s.answer}")
            else:
                running.append(s.id[:8])
        if running:
            them = "your subagent" if len(running) == 1 else "your subagents"
            parts.append(f"Still working: {', '.join(running)}. Tell {them} to finish up (with tell), "
                         "or run wait again for them.")
        return "\n\n".join(parts)

    async def _supervise(self, run: ActiveRun, card: dict[str, Any], prompt: str, subagent: Subagent) -> None:
        """Run the subagent on `prompt` (its task, or a message from its caller) and keep its answer.
        One error in its stream doesn't end it: it gets one more try, going on where it was."""
        db = self.relay.db
        retried, stopped = False, []  # (it went on once with its own model; the free models that failed)
        try:
            while True:
                async with self._slot():
                    if run.stop_requested or run.finished or run.closing:
                        run.add({**card, "status": "interrupted"})
                        subagent.answer = "It didn't start: the run was stopped."
                        return
                    child = db.one("SELECT * FROM conversations WHERE id = ?", (card["id"],))
                    try:
                        child_run = await asyncio.to_thread(self.relay.runs.start, child, prompt)
                    except Exception as exc:  # noqa: BLE001 - reported to the agent and the card
                        run.add({**card, "status": "failed", "error": str(exc)})
                        subagent.answer = f"It couldn't start: {exc}. {AGAIN}"
                        subagent.failed = True
                        return
                    card["run"] = child_run
                    run.add({**card, "status": "running"})
                    await self._wait_for(run, child_run)
                answer = db.one("SELECT m.content, r.status, r.usage, r.error FROM messages m "
                                "JOIN runs r ON r.id = m.run_id WHERE m.run_id = ?", (child_run,))
                status = answer["status"] if answer else "failed"
                if status != "failed" or run.stop_requested or run.finished or run.closing:
                    break
                error = (answer["error"] if answer else None) or "an error"
                events = db.runs_events([child_run]).get(child_run, [])
                did_nothing = not any(e.get("type") in ("text", "tool_call", "reasoning") for e in events)
                # A free model that failed without getting anywhere (or again): another free one goes on
                if (did_nothing or retried) and len(stopped) < FREE_FALLBACKS:
                    taken = await asyncio.to_thread(self._free_fallback, run, card, subagent, stopped)
                    if taken:
                        prompt = (f"{taken} stopped ({error[:300]}), so you take over. Please do the task above, "
                                  "or continue where it left off; if it was done, say so briefly.")
                        retried = False
                        continue
                if not retried and self._worth_retrying(run, card["id"]):
                    retried = True
                    prompt = (f"Your answer stopped with an error ({error[:300]}). Please go on where you "
                              "left off.")
                    continue
                break
            usage = json.loads(answer["usage"]) if answer and answer["usage"] else None
            run.add({**card, "status": status, "usage": usage})
            text = (answer["content"] if answer else "") or ""
            if len(text) > MAX_ANSWER:
                text = text[:MAX_ANSWER] + "\n[… cut off]"
            head = f"{subagent.who}: {'finished' if status == 'succeeded' else status}."
            if stopped:
                head += f" It took over from {', '.join(stopped)}, which failed."
            if answer and answer["error"]:
                head += f" Error: {answer['error']}"
            subagent.failed = status == "failed" and not text.strip()
            subagent.answer = (f"{head}\n\n{text}" if text
                               else f"{head} It gave no answer." + (f" {AGAIN}" if subagent.failed else ""))
            if subagent.fork is not None and not subagent.told:
                subagent.answer += "\n\n" + await asyncio.to_thread(self._hand_over, subagent, status)
        finally:
            if subagent.answer is None:
                subagent.answer = "It ended without an answer."
            subagent.done.set()

    def _hand_over(self, subagent: Subagent, status: str) -> str:
        """A subagent in a copy is done: its changes go into the caller's folder when it finished
        and they don't conflict; otherwise the caller hears what they are, and decides."""
        short = subagent.id[:8]
        try:
            if status == "succeeded":
                return forks.merge(subagent.fork).describe(short)
            changes = forks.pending(subagent.fork)
        except (forks.ForkError, git.GitError) as exc:
            return f"Relay couldn't hand over its changes ({exc}); `merge {short}` tries again."
        if not changes:
            return "It changed no files."
        return (f"It {status}, so its changes weren't applied. What it changed:\n\n{changes}\n\nYou decide: "
                f"`merge {short}` applies them (with conflict markers where they conflict with yours); tell "
                f"{short} lets it go on; or take what you need from the diff yourself.")

    async def merge(self, run: ActiveRun, arguments: dict[str, Any]) -> str:
        """Apply a subagent's changes that weren't (they conflicted, or it didn't finish)."""
        wanted = str(arguments.get("id") or "").strip()
        mine = self.subagents.get(run.id, {})
        subagent = _subagent(mine, wanted)
        if subagent is None:
            raise ToolError(f"No subagent {wanted or '(no id)'} in this run")
        short = subagent.id[:8]
        if subagent.fork is None:
            raise ToolError(f"Subagent {short} works in your folder itself: its changes are there already")
        if not subagent.done.is_set():
            raise ToolError(f"Subagent {short} is still working: wait for it (or tell it to finish up) first")
        try:
            result = await asyncio.to_thread(forks.merge, subagent.fork, True)
        except (forks.ForkError, git.GitError) as exc:
            raise ToolError(f"Couldn't apply its changes: {exc}") from exc
        if result.status == "nothing":
            return f"Subagent {short} has no changes that aren't in your folder yet."
        return result.describe(short)

    def _free_fallback(self, run: ActiveRun, card: dict[str, Any], subagent: Subagent,
                       stopped: list[str]) -> str | None:
        """A free subagent's model failed: the next free model goes on with its task (its card and
        the caller see which). The free models the user allows without asking (relay.policy), not
        tried yet, usable (on, with tools when it had them, not limited), not failed in the last
        FAILED_RECENTLY seconds, not on a local server; the ones that worked last first. The name of
        the one that stopped, or None: not free, or no other free model to go to."""
        db, registry = self.relay.db, self.relay.registry
        child = db.one("SELECT * FROM conversations WHERE id = ?", (card["id"],))
        caller = db.one("SELECT * FROM conversations WHERE id = ?", (child["parent_conversation"],)) if child else None
        if child is None or caller is None:
            return None
        current = {"provider": child["provider_id"], "model": child["model"] or ""}
        if policy.billing(db, registry, current["provider"], current["model"]) != FREE:
            return None
        rules = policy.load(db, child["user_id"])
        if not rules["free"]:
            return None
        try:
            needs_tools = registry.provider(current["provider"]).has_tools()
        except Exception:  # noqa: BLE001 - removed meanwhile
            needs_tools = True
        tried = {fallback.key(current), *(fallback.key({"provider": p, "model": m}) for p, m in subagent.tried)}
        statuses = health.statuses(db)
        now = time.time()
        found = []
        for provider_id, model, why in policy.allowed(db, registry, rules, caller):
            entry = {"provider": provider_id, "model": model}
            status = statuses.get(fallback.key(entry))
            if why != "free" or model == "*" or fallback.key(entry) in tried \
                    or (status and not status["ok"] and now - status["checked"] < FAILED_RECENTLY) \
                    or not fallback.usable(db, registry, entry, needs_tools) \
                    or policy.local_server(registry, provider_id, model):
                continue
            found.append((0 if status and status["ok"] else 1, -(status["checked"] if status else 0), provider_id,
                          model))
        if not found:
            return None
        _, _, provider_id, model = min(found)
        name = self.relay.runs.display_name(current["provider"], current["model"])
        subagent.tried.append((current["provider"], current["model"]))
        stopped.append(name)
        self.relay.runs.switch_agent(child, provider_id, model)
        template = registry.get(provider_id)
        subagent.provider_id, subagent.model = provider_id, model
        subagent.who = template.name + (f" · {model}" if model else "")
        card.update(provider=provider_id, model=model or None, fell_back=name)
        run.add({**card, "status": "running"})
        return name

    def _worth_retrying(self, run: ActiveRun, conversation_id: str) -> bool:
        """After a failed answer: not when the caller is ending, nor on a usage limit (it won't
        have reset by now)."""
        if run.stop_requested or run.finished or run.closing:
            return False
        row = self.relay.db.one("SELECT limit_info FROM conversations WHERE id = ?", (conversation_id,))
        info = json.loads(row["limit_info"]) if row and row["limit_info"] else {}
        return not info.get("limit")

    async def tell(self, run: ActiveRun, arguments: dict[str, Any]) -> str:
        """A message to one of this run's subagents: one still working is interrupted and gets it
        (e.g. to hurry up and answer with what it has); a finished one gets it as a follow-up."""
        wanted = str(arguments.get("id") or "").strip()
        message = str(arguments.get("message") or "").strip()
        mine = self.subagents.get(run.id, {})
        subagent = _subagent(mine, wanted)
        if subagent is None:
            raise ToolError(f"No subagent {wanted or '(no id)'} in this run"
                            + (f"; there's {', '.join(s.id[:8] for s in mine.values())}" if mine else ""))
        if not message:
            raise ToolError("The message is empty")
        if run.closing or run.stop_requested:
            raise ToolError("The run is ending")
        card = next((dict(e) for e in reversed(run.events) if e.get("type") == "subagent"
                     and e.get("id") == subagent.id), None)
        if card is None:
            raise ToolError("That subagent hasn't started")
        working = not subagent.done.is_set()
        if working and card.get("run"):
            subagent.told = True
            await asyncio.to_thread(self.relay.runs.stop_and_wait, card["run"], 15,
                                    "its caller interrupted it to tell it something")
        try:
            await asyncio.wait_for(subagent.done.wait(), timeout=30)
        except asyncio.TimeoutError as exc:
            raise ToolError("The subagent didn't stop in time; try again") from exc
        finally:
            subagent.told = False
        if subagent.fork is not None:
            # It goes on from your files as they are now (and what it hasn't handed over yet).
            try:
                conflicts = await asyncio.to_thread(forks.refresh, subagent.fork)
            except (forks.ForkError, git.GitError):
                conflicts = None
            if conflicts is not None:
                message += ("\n\n(From Relay: your folder now has your caller's latest files too"
                            + (f"; they conflicted with your changes in {', '.join(conflicts)}, which have conflict "
                               "markers (<<<<<<< yours … ======= … >>>>>>> caller) to sort out" if conflicts else "")
                            + ".)")
        # Its conversation answers to this run now (its questions show here).
        self.relay.db.execute("UPDATE conversations SET parent_run = ? WHERE id = ?", (run.id, subagent.id))
        subagent.done.clear()
        subagent.answer, subagent.delivered, subagent.failed = None, False, False
        prompt = f"A message from the agent that gave you this task: {message}"
        subagent.task = asyncio.create_task(self._supervise(run, card, prompt, subagent))
        short = subagent.id[:8]
        return (f"Told subagent {short}{' (interrupted it first)' if working else ''}. It goes on with your "
                f"message; get its answer with: wait {short}")

    async def _wait_for(self, run: ActiveRun, child_run: str) -> None:
        deadline = time.monotonic() + tunables.get("subagents.timeout") * 60
        runs = self.relay.runs
        while True:
            with runs.lock:
                going = child_run in runs.active
            if not going:
                return
            if run.stop_requested or run.finished or run.closing:
                runs.stop(child_run, "its caller stopped")
            elif time.monotonic() > deadline:
                runs.stop(child_run, "its time limit was up")
                deadline = float("inf")  # stopping; wait for it to end
            await asyncio.sleep(0.3)

    def settle(self, run_id: str, timeout: float = 60) -> None:
        """Wait (in the run's thread) until the run's subagents have ended and
        reported; they're stopped as soon as run.closing is set."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if all(s.done.is_set() for s in self.subagents.get(run_id, {}).values()):
                return
            time.sleep(0.1)

    def run_ended(self, run_id: str) -> None:
        """Forget a finished run's subagents once they've stopped too. Their copies go, unless
        they have changes that weren't handed over: those wait for the user (Apply or Discard
        in the subagent's conversation; relay.forks)."""
        mine = self.subagents.get(run_id, {})
        for subagent in mine.values():
            if subagent.fork is None or not subagent.done.is_set():
                continue
            fork, subagent.fork = subagent.fork, None
            try:
                if not forks.changed(fork):
                    forks.remove(fork, self.relay.containers)
            except Exception as exc:  # noqa: BLE001 - never lose the run over it (the copy stays)
                print(f"relay: couldn't tidy a subagent's copy: {exc}", file=sys.stderr, flush=True)
        if all(s.done.is_set() for s in mine.values()):
            self.subagents.pop(run_id, None)

    async def approve(self, run: ActiveRun, arguments: dict[str, Any]) -> str:
        """Claude Code's permission prompt: Relay answers it (the provider's judge());
        nobody is asked, as its commands run in a container."""
        tool_input = arguments.get("input") if isinstance(arguments.get("input"), dict) else {}
        action = str(arguments.get("tool_name") or "tool")
        verdict, why = await asyncio.to_thread(self._judge, run, action, tool_input)
        if verdict == "allow":
            return json.dumps({"behavior": "allow", "updatedInput": tool_input})
        run.add({"type": "permission", "id": str(arguments.get("tool_use_id") or uuid.uuid4().hex),
                 "action": action, "resources": tool_input, "decision": "denied", "outcome": why or "denied"})
        return json.dumps({"behavior": "deny", "message": why or "Relay didn't allow this."})

    def allowed(self, run: ActiveRun) -> set[str]:
        """The tool names a run may call: those it's offered, and help (the script's)."""
        names = {t["name"] for t in self._tools_of(run)}
        # (a subagent: no help)
        return names | {"help"} if not names <= {"approve_permission", "search", "fetch"} else names

    def _tools_for(self, token: str) -> list[dict[str, Any]]:
        return self._tools_of(self.run_for(token))

    def _tools_of(self, run: ActiveRun | None) -> list[dict[str, Any]]:
        """The MCP tools of a run: a subagent's only ask permission, search and fetch (one level deep). Without
        the kinds of Relay's tools the user turned off for its conversation (relay.relay_tools)."""
        row = self.relay.db.one("SELECT user_id, parent_run, tools FROM conversations WHERE id = ?",
                                (run.conversation_id,)) if run else None
        gone = relay_tools.off(row)
        # (a search, a fetch: only a look)
        search = [*([SEARCH] if self.relay.search.enabled() else []),
                  FETCH] if row is not None and "web" not in gone else []
        if row is None or row["parent_run"]:
            return [APPROVE, *search]
        remembering = MEMORY_TOOLS if memory.enabled(self.relay.db, row["user_id"]) and "memory" not in gone else []
        if self.relay.agentos.is_governor(run.conversation_id):
            # It steers: agents to choose from and AgentOS's capabilities; what it starts is managed.
            return [APPROVE, *search, *(t for t in RELAY_TOOLS if t["name"] == "agents"), *AGENTOS_TOOLS, *remembering,
                    *self.relay.plugins.tools(governor=True, conversation_id=run.conversation_id)]
        has_computers = self.relay.db.one("SELECT 1 FROM computers WHERE user_id = ?", (row["user_id"],))
        code = self.relay.db.one("SELECT 1 FROM conversations WHERE id = ? AND mode = 'code'", (run.conversation_id,))
        relay_own = [t for t in RELAY_TOOLS if "subagents" not in gone or t["name"] not in SUBAGENT_TOOLS]
        repair = self.relay.repairs.working(run.conversation_id)
        repairing = [*REPAIR_TOOLS, *([PLAN_TOOL] if repair["status"] in ("planning", "planned") else [])] \
            if repair else []
        return [APPROVE, *search, *relay_own, *(TASK_TOOLS if code else []),
                *(COMPUTER_TOOLS if has_computers and "computers" not in gone else []), *remembering, *repairing,
                *self.relay.plugins.tools(conversation_id=run.conversation_id)]  # (the user's plugins': relay.plugins)

    def _judge(self, run: ActiveRun, action: str, detail: dict[str, Any]) -> tuple[str, str]:
        conversation = self._conversation(run)
        try:
            provider = self.relay.registry.provider(run.provider_id)
        except TemplateError:
            return "deny", "The provider was removed"
        from relay.agentos import read_boundary
        folder = Path(conversation["directory"])
        return provider.judge(action, detail, folder, read_boundary(self.relay.db, conversation, folder))

    def _read_only(self, conversation: dict[str, Any]) -> bool:
        mode = Modes(self.relay.db).get(conversation["user_id"], conversation.get("agent_mode"))
        return bool(mode and mode.access == "read")

    @contextlib.asynccontextmanager
    async def _slot(self):
        """Wait for room among the subagents working at once (subagents.parallel)."""
        if self._room is None:
            self._room = asyncio.Condition()
        async with self._room:
            await self._room.wait_for(lambda: self._busy < tunables.get("subagents.parallel"))
            self._busy += 1
        try:
            yield
        finally:
            async with self._room:
                self._busy -= 1
                self._room.notify_all()

    def on_repair(self, run: ActiveRun, name: str, arguments: dict[str, Any]) -> str:
        """A repair's agent's test of its version, or a command in the provider's box (relay.repair); a new
        provider's plan (relay.creations)."""
        from relay.repair import RepairError
        repair = self.relay.repairs.working(run.conversation_id)
        if repair is None:
            raise ToolError("This conversation's repair is done (applied or discarded)")
        try:
            if name == "provider_plan":
                return self.relay.creations.plan(repair, arguments.get("name"), arguments.get("program"),
                                                 arguments.get("plan"))
            if name == "repair_check":
                return json.dumps(self.relay.repairs.check(repair), indent=2)
            return self.relay.repairs.run(repair, arguments.get("command"), self.relay.boxes, arguments.get("timeout"))
        except RepairError as exc:
            raise ToolError(str(exc)) from exc

    def on_memory(self, run: ActiveRun, name: str, arguments: dict[str, Any]) -> str:
        """Relay's memory (relay.memory), as an agent adds to it, changes it or reads it."""
        db, row = self.relay.db, self._conversation(run)
        user = row["user_id"]
        if not memory.enabled(db, user):
            raise ToolError("The user turned Relay's memory off (Settings → Memory)")
        project = memory.project_of(db, row)
        own = "your own" if project == memory.GOVERNOR else "this project"
        if name != "memory_list" and not memory.agents_add(db, user):
            raise ToolError("The user keeps Relay's memory themselves (Settings → Memory): tell them what to remember")
        try:
            if name == "memory_add":
                mine = str(arguments.get("project", "")).lower() in ("true", "1", "yes")
                if mine and project is None:
                    raise ToolError("This conversation has no project (it's a chat): save it as a general one")
                names = {t.id: t.name for t in self.relay.registry.templates()}
                provider = run.provider_id or row["provider_id"]
                by = names.get(provider, provider) + (f" · {row['model']}" if row.get("model") else "")
                made = memory.add(db, user, arguments.get("text"), project if mine else None, by, row["id"],
                                  arguments.get("category"))
                return (f"Remembered [{made['id'][:6]}] ({own if mine else 'general'}, {made['category']}): "
                        f"{made['text']}")
            if name in ("memory_change", "memory_forget"):
                found = memory.find(db, user, arguments.get("id"))
                if found["project"] not in (None, project):  # (another project's, the Governor's: not this one's)
                    raise ToolError(f"No memory {arguments.get('id')!r} for this conversation (memory_list)")
                if found["by"] == "you":  # (the user's own word: theirs to change, not an agent's)
                    raise ToolError(f"[{found['id'][:6]}] is the user's own: ask them to change it (Settings → Memory)")
                if name == "memory_change":
                    if arguments.get("text") in (None, "") and arguments.get("category") in (None, ""):
                        raise ToolError("Say its new text, its new category, or both")
                    changed = memory.change(db, user, found["id"], arguments.get("text") or None,
                                            category=arguments.get("category") or None)
                    return f"Changed [{changed['id'][:6]}] ({changed['category']}): {changed['text']}"
                gone = memory.forget(db, user, found["id"])
                return f"Forgot [{gone['id'][:6]}]: {gone['text']}"
            lines = []
            for m in memory.listed(db, user):
                if m["project"] in (None, project):
                    where = own if m["project"] else "general"
                    whose = ", the user's own" if m["by"] == "you" else ""
                    lines.append(f"- [{m['short']}] ({where}, {m['category']}{whose}) {m['text']}")
            return "\n".join(lines) or "Relay's memory holds nothing for this conversation yet."
        except memory.MemoryError as exc:
            raise ToolError(str(exc)) from exc

    def _conversation(self, run: ActiveRun) -> dict[str, Any]:
        row = self.relay.db.one("SELECT * FROM conversations WHERE id = ?", (run.conversation_id,))
        if row is None:
            raise ToolError("The conversation is gone")
        return row


def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _called(name: str) -> str:
    """A tool's name as the agent knows it (chat.create for AgentOS's agentos_chat_create)."""
    return name.removeprefix("agentos_").replace("_", ".") if name.startswith("agentos_") else name


def _text(text: str, error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": error}


def _subagent(mine: dict[str, Any], wanted: str) -> Any:
    """The one of a run's subagents its id (or its first characters) means; None when none does. More than one is a
    ToolError: the first was taken, which could be the other one."""
    found = [s for s in mine.values() if wanted and s.id.startswith(wanted)]
    exact = [s for s in found if s.id == wanted]
    if len(found) > 1 and not exact:
        raise ToolError(f"More than one subagent starts with {wanted}: give more of its id")
    return (exact or found or [None])[0]


def _nul(value: Any) -> bool:
    """A NUL character anywhere in a tool's arguments (as relay.web.body checks a request's)."""
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(_nul(k) or _nul(v) for k, v in value.items())
    return isinstance(value, list) and any(_nul(v) for v in value)
