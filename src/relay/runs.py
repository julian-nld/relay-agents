"""Background runs: a prompt sent to a provider, executed in a thread.

Runs keep going when the browser disconnects. Their events are kept in
memory while running (so any number of browsers can follow along live) and
stored in the database in batches while running, and flushed when the run ends.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sys
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from relay import fallback, forks, git, health, limits, memory, policy, relay_tools, skills, tasks, timed, tunables
from relay.agentos import read_boundary
from relay.containers import ContainerError
from relay.db import Database
from relay.handover import answer_text, tool_line
from relay.modes import Modes, governing, with_instructions
from relay.notify import Finished
from relay.providers import Context, ProviderError, Registry, TemplateError, safely
from relay.tools_script import note, search_note
from relay.uploads import Uploads

Event = dict[str, Any]
MAX_RESUMES = 5  # a usage limit hit again and again: stop going on by itself
EVENT_FLUSH_SECONDS = 0.5  # normal batch interval; not a durability guarantee on storage failure
MIN_RESUME_WAIT = 300  # seconds; also when a reset time is (or seems) in the past


class RunError(Exception):
    pass


class _Stopped(Exception):
    """A run stopped before its agent started."""


class ProjectBusy(RunError):
    status = 409


class ActiveRun:
    def __init__(self, run_id: str, conversation_id: str, provider_id: str = "",
                 message_id: int | None = None) -> None:
        self.id = run_id
        self.conversation_id = conversation_id
        self.provider_id = provider_id
        self.box_key: str | None = None  # the provider's box (relay.boxes), while it runs there
        self.directory: str | None = None  # the folder it works in
        self.message_id = message_id  # the user message this run answers
        # A code task's conversation: its files are committed around the run (relay.tasks).
        self.task: dict[str, Any] | None = None
        self.project: dict[str, Any] | None = None
        self.delegations = 0  # subagents started (relay.delegation)
        self.computers_allowed: set[str] = set()  # computers the user let it use freely for this answer
        self.go_on: str | None = None  # a fallback agent takes over with this message (relay.fallback)
        # A subagent's run: its caller's run and conversation, and its label (relay.delegation).
        self.parent_run: str | None = None
        self.parent_conversation: str | None = None
        self.label = ""
        self.tools = False  # whether Relay's tools were offered
        self.tools_path: str | None = None
        # Permission requests and forms waiting for the user: id -> {"event", "respond"}.
        self.pending: dict[str, dict[str, Any]] = {}
        self.mcp_token: str | None = None
        self.events: list[Event] = []
        self.reasoning_started: float | None = None
        self.stored_events = 0
        self.store_lock = threading.Lock()
        self.finished = False
        self.stop_requested = False
        self.stop_reason: str | None = None  # who stopped it and why, for those who weren't there (AgentOS)
        self.closing = False  # the run's own work is done; its subagents are being stopped
        self.provider_run = None
        self.lock = threading.Lock()
        self.listeners: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = set()

    def add(self, event: Event) -> None:
        with self.lock:
            kind = event.get("type")
            if kind == "reasoning" and event.get("delta") and self.reasoning_started is None:
                self.reasoning_started = time.monotonic()
            if self.reasoning_started is not None:
                # Browser replays and reconnects must not change the measured duration.
                event = {**event, "reasoning_ms": round((time.monotonic() - self.reasoning_started) * 1000)}
                if kind in ("tool_call", "subagent", "form", "permission", "error", "done") or (
                        kind == "text" and event.get("delta")):
                    self.reasoning_started = None
            self.events.append(event)
            listeners = list(self.listeners)
        for loop, flag in listeners:
            loop.call_soon_threadsafe(flag.set)

    def flush(self, db: Database) -> None:
        with self.store_lock:
            with self.lock:
                batch = self.events[self.stored_events:]
            if batch:
                db.append_run_events(self.id, self.stored_events, batch)
                self.stored_events += len(batch)

    def finish(self) -> None:
        with self.lock:
            self.finished = True
            listeners = list(self.listeners)
        for loop, flag in listeners:
            loop.call_soon_threadsafe(flag.set)


class RunManager:
    def __init__(self, db: Database, registry: Registry, notifier=None, containers=None, boxes=None) -> None:
        self.db = db
        self.containers = containers  # relay.containers.Containers; None: commands run here (tests)
        self.boxes = boxes  # relay.boxes.Boxes: providers' programs run in boxes; None: here
        self.registry = registry
        self.notifier = notifier
        self.active: dict[str, ActiveRun] = {}
        self.lock = threading.Lock()
        self.shutting_down = False  # (runs stopped by Relay's own exit: not by the user)
        # Subagents (relay.delegation), set up by the app: the MCP tools and
        # the address agents reach Relay at.
        self.delegation = None
        self.search = None  # relay.search.Search: Relay's web search, for every run (set by Relay)
        self.fetch = None  # relay.fetch.Fetcher: Relay's web fetch, for every run (set by Relay)
        self.local_url: str | None = None
        # Serialises start(): checking for a running turn and registering a new
        # one must be atomic, or two quick sends start two agents.
        self.start_lock = threading.Lock()
        self.event_flusher: threading.Thread | None = None
        self.project_operations: dict[str, tuple[list[Path], bool]] = {}

    def active_run_for(self, conversation_id: str) -> str | None:
        with self.lock:
            return next((r.id for r in self.active.values()
                         if r.conversation_id == conversation_id), None)

    def project_operation(self, row: dict[str, Any], operation: Callable, *args,
                          repository: bool = False, allow_run_id: str | None = None, **kwargs):
        """Reserve the affected files while restoring/committing them, not for ordinary runs.

        Registration is atomic with start(): checking for writers and starting an
        operation separately would still allow a run to slip in between them.
        """
        paths = [Path(row["directory"]).resolve()]
        if repository:
            paths += [Path(row[k]).resolve() for k in ("repo_root", "worktree") if row.get(k)]
        token = uuid.uuid4().hex
        with self.start_lock, self.lock:
            other = next((r for r in self.active.values()
                          if r.id != allow_run_id and r.project and _overlaps(paths, r.project, repository)), None)
            if other is not None:  # (who and where: an agent told this needn't go looking)
                raise ProjectBusy("Another agent is working in this project (“"
                                  f"{other.project.get('title') or 'a task'}”, "
                                  f"in {other.project['directory']}); wait for it to finish (or stop it) first")
            if any(_paths_overlap(paths, p) for p, _ in self.project_operations.values()):
                raise ProjectBusy("The project's files are already being updated; try again when that finishes")
            self.project_operations[token] = (paths, repository)
        try:
            return operation(row, *args, **kwargs)
        finally:
            with self.lock:
                self.project_operations.pop(token, None)

    def _own_copy(self, conversation: dict[str, Any]) -> dict[str, Any]:
        """A code subagent works in a copy of its caller's folder (relay.forks). Its copy goes
        once everything is handed over; going on with it (the user, in its conversation) gives
        it a new one, from the caller's files as they are now."""
        directory = conversation.get("directory") or ""
        if (conversation.get("mode") != "code" or not conversation.get("parent_conversation")
                or not _is_copy(directory) or Path(directory).is_dir()):
            return conversation
        parent = self.db.one("SELECT id, directory FROM conversations WHERE id = ?",
                             (conversation["parent_conversation"],))
        if parent is None:
            raise RunError("The agent this subagent worked for is gone, and its folder with it")
        try:
            fork = forks.make(parent["id"], Path(parent["directory"]), conversation["id"])
        except forks.ForkError as exc:
            raise RunError(f"Couldn't give the subagent a copy of {parent['directory']}: {exc}") from exc
        if self.containers is not None and fork.links:
            self.containers.mount_also(fork.folder, [fork.source / link for link in fork.links])
        self.db.execute("UPDATE conversations SET directory = ? WHERE id = ?", (str(fork.folder), conversation["id"]))
        return {**conversation, "directory": str(fork.folder)}

    def _containers_ready(self, run: ActiveRun) -> bool:
        """Before the first answer after Relay starts: podman is checked and, on a new machine (or
        after an upgrade that changed them), the agents' and the providers' images are built
        (minutes). The answer shows it, and can be stopped meanwhile; False when it was. A podman
        that doesn't work is the run's error (ContainerError)."""
        done, problem = threading.Event(), []

        def check() -> None:
            try:
                self.containers.check()
                if self.boxes is not None:
                    self.boxes.check()
            except ContainerError as exc:
                problem.append(exc)
            finally:
                done.set()

        threading.Thread(target=check, name="containers-check", daemon=True).start()
        if done.wait(1):  # usually it's long done
            if problem:
                raise problem[0]
            return True
        card = {"id": f"relay-containers-{run.id[:8]}", "name": "Relay"}
        run.add({"type": "tool_call", **card, "input": {
            "description": "Preparing the agents' container (on a new machine the first time takes a few minutes)"}})
        while not done.wait(0.5):
            if run.stop_requested:
                run.add({"type": "tool_result", **card, "ok": False, "output": "Stopped"})
                return False
        run.add({"type": "tool_result", **card, "ok": not problem, "output": str(problem[0]) if problem else "Ready"})
        if problem:
            raise problem[0]
        return True

    def tools_script_for(self, folder: str, within: Path | None = None) -> Path:
        """The tools script of the agent working in `folder` now (for `relay` in a WorkBox,
        container/bin/relay, when its program doesn't say whose it is: opencode's shared service).
        The most specific folder wins; RunError when there's none, or more than one. With `within`
        (a WorkBox's folder): only agents working in that box."""
        from relay.tools_script import tools_dir
        try:
            where = Path(folder).resolve()
        except (OSError, ValueError) as exc:
            raise RunError(f"Not a folder: {folder}") from exc
        with self.lock:
            ids = [r.conversation_id for r in self.active.values() if r.tools]
        found: list[tuple[int, Path]] = []
        for conversation_id in ids:
            row = self.db.one("SELECT directory, tools_token FROM conversations WHERE id = ?", (conversation_id,))
            if not row or not row["tools_token"] or not row["directory"]:
                continue
            directory = Path(row["directory"]).resolve()
            if within is not None and not (directory == within or within in directory.parents
                                           or directory in within.parents):
                continue
            if directory == where or directory in where.parents:
                found.append((len(directory.parts), tools_dir() / row["tools_token"] / "relay"))
        if not found:
            raise RunError("No agent of Relay is working in this folder now: Relay's tools work while one is")
        deepest = max(depth for depth, _ in found)
        scripts = {script for depth, script in found if depth == deepest}
        if len(scripts) > 1:
            raise RunError("More than one of Relay's agents works in this folder now, so `relay` can't tell "
                           "which you are; the user can tell you your tools' full path")
        return scripts.pop()

    def waiting_for_answer(self, conversation_id: str) -> bool:
        """Whether a run of the conversation (or of one of its subagents) waits for the user
        to approve something or answer questions."""
        with self.lock:
            return any(r.pending for r in self.active.values()
                       if conversation_id in (r.conversation_id, r.parent_conversation))

    def ask(self, run: ActiveRun, event: Event, respond) -> None:
        """Put a permission request or form to the user; respond passes the answer on.

        An event with "options" ([{"id", "label", "outcome"}]) offers those
        instead of Allow and Deny, and respond gets the chosen id. (What's left to ask:
        a paid subagent, a command on the user's own computer.)"""
        existing = run.pending.get(str(event["id"])) if event["type"] == "form" else None
        if existing is not None:
            with existing["lock"]:
                if run.pending.get(str(event["id"])) is existing:
                    self._request_event(run, existing["event"])
            return  # repeated delivery must not extend the deadline
        seconds = tunables.get("questions.dismiss_timeout") if event["type"] == "form" else 0
        if seconds:
            event = {**event, "expires": time.time() + seconds}
        entry = {"event": event, "respond": respond, "lock": threading.Lock()}
        run.pending[str(event["id"])] = entry
        self._request_event(run, event)
        if seconds:
            with entry["lock"]:
                if (run.pending.get(str(event["id"])) is entry
                        and not run.stop_requested and not run.closing and not run.finished):
                    self._form_timer(run, entry, seconds)
        if self.notifier is not None:
            row = self.db.one("SELECT title FROM conversations WHERE id = ?", (run.conversation_id,))
            what = event.get("resources")
            what = ", ".join(map(str, what)) if isinstance(what, list) else json.dumps(what)[:200]
            self.notifier.approval_needed(run.conversation_id, row["title"] if row else "Relay",
                                          event.get("title", "Questions") if event["type"] == "form" else
                                          f"{event.get('action')}: {what}")

    def _request_event(self, run: ActiveRun, event: Event) -> None:
        run.add(event)
        if event["type"] == "form" and run.parent_run:
            with self.lock:
                parent = self.active.get(run.parent_run)
            if parent is not None:
                parent.add({**event, "run": run.id, "subagent": run.label})

    def answer_form(self, run_id: str, request_id: str, answer: dict[str, Any] | None) -> None:
        with self.lock:
            run = self.active.get(run_id)
        if run is None or run.closing or run.stop_requested:
            raise RunError("This run has ended")
        entry = run.pending.get(request_id)
        if entry is None or entry["event"]["type"] != "form":
            raise RunError("That question was answered already")
        with entry["lock"]:
            self._reply_form(run, entry, answer)

    def _reply_form(self, run: ActiveRun, entry: dict, answer: dict | None, message: str | None = None) -> None:
        request_id = str(entry["event"]["id"])
        if run.pending.get(request_id) is not entry or run.stop_requested or run.closing or run.finished:
            raise RunError("That question was answered already")
        entry["respond"](answer)  # a failed reply leaves the question available for retry
        run.pending.pop(request_id, None)
        self._cancel_form_timer(entry)
        self._request_event(run, {**entry["event"], "status": "cancelled" if answer is None else "answered",
                                  **({"answer": answer} if answer is not None else {}),
                                  **({"message": message} if message else {})})

    def _form_timer(self, run: ActiveRun, entry: dict, seconds: float) -> None:
        timer = threading.Timer(seconds, self._expire_form, args=(run, entry))
        timer.daemon = True
        entry["timer"] = timer
        timer.start()

    @staticmethod
    def _cancel_form_timer(entry: dict) -> None:
        timer = entry.get("timer")
        if timer is not None:
            timer.cancel()

    def _expire_form(self, run: ActiveRun, entry: dict) -> None:
        with entry["lock"]:
            with self.lock:
                active = self.active.get(run.id) is run
            if (not active or run.pending.get(str(entry["event"]["id"])) is not entry
                    or run.stop_requested or run.closing or run.finished):
                return
            try:
                self._reply_form(run, entry, None, "Question dismissed automatically: no answer before the timeout")
            except Exception as exc:  # noqa: BLE001 - retry a failed provider reply, not a fictitious dismissal
                print(f"relay: couldn't dismiss question {entry['event']['id']}: {exc}", file=sys.stderr, flush=True)
                if not run.stop_requested and not run.closing and not run.finished:
                    self._form_timer(run, entry, 30)

    def answer(self, run_id: str, request_id: str, decision: str) -> None:
        """The user's answer to a permission request: "allow", "deny", or one of its options."""
        with self.lock:
            run = self.active.get(run_id)
        if run is None:
            raise RunError("This run has ended")
        entry = run.pending.get(str(request_id))
        if entry is None or entry["event"]["type"] != "permission":
            raise RunError("That request was answered already")
        options = entry["event"].get("options")
        if options:
            chosen = next((o for o in options if o["id"] == decision), None)
            if chosen is None:
                raise RunError("That isn't one of the choices")
            entry["respond"](decision)
            answered = {"decision": decision, "outcome": chosen.get("outcome") or chosen["label"]}
        else:
            if decision not in ("allow", "deny"):
                raise RunError("The answer is allow or deny")
            entry["respond"](decision == "allow")  # if this fails, the request stays open to answer again
            answered = {"decision": "once" if decision == "allow" else "denied"}
        run.pending.pop(str(request_id), None)
        run.add({**entry["event"], **answered})

    def withdraw(self, run: ActiveRun, request_id: str, decision: str, outcome: str) -> None:
        """Close a request nobody answered (e.g. in time), with what came of it."""
        entry = run.pending.pop(str(request_id), None)
        if entry is not None:
            run.add({**entry["event"], "decision": decision, "outcome": outcome})

    def start(self, conversation: dict[str, Any], prompt: str, attachments=(),
              reuse_message: int | None = None, relay_action: dict[str, Any] | None = None) -> str:
        """Record the user's message and start the provider in the background.

        With `reuse_message` (the id of the conversation's last user message)
        that message is answered again instead of adding a new one.
        `relay_action` records provenance without changing the provider's prompt.
        """
        with self.start_lock:
            return self._start(conversation, prompt, list(attachments), reuse_message, relay_action)

    def _start(self, conversation: dict[str, Any], prompt: str, attachments: list,
               reuse_message: int | None = None, relay_action: dict[str, Any] | None = None) -> str:
        if self.active_run_for(conversation["id"]):
            raise RunError("This conversation is still answering")
        project = dict(conversation)
        if conversation.get("parent_conversation"):
            parent = self.db.one("SELECT directory FROM conversations WHERE id = ?",
                                 (conversation["parent_conversation"],))
            if parent:
                project["source_directory"] = parent["directory"]  # a subagent's copy merges back here
        with self.lock:
            if any(_overlaps(paths, project, repo) for paths, repo in self.project_operations.values()):
                raise ProjectBusy("The project's files are being updated; send again when that finishes")
        conversation = self._own_copy(conversation)  # (after the check: not while its copy is applied)
        project["directory"] = conversation["directory"]
        try:
            provider = self.registry.provider(conversation["provider_id"])
        except TemplateError as exc:
            raise RunError(str(exc)) from exc
        if not provider.template.enabled:
            raise RunError(f"Provider {provider.template.name!r} is disabled")
        model = safely(provider.model_id, conversation["model"], conversation["model"])
        if model != conversation["model"]:  # (as its program names it; kept so, for the page and the next run)
            self.db.execute("UPDATE conversations SET model = ? WHERE id = ?", (model, conversation["id"]))
            conversation = {**conversation, "model": model}

        uploads = Uploads(self.db)
        rows = self.db.all(  # (since a /clear: what came before it isn't the agent's any more)
            "SELECT role, content, attachments, run_id FROM messages WHERE conversation_id = ? "
            "AND id < ? AND id > ? ORDER BY id",
            (conversation["id"], reuse_message if reuse_message is not None else 2**62,
             conversation.get("cleared_after") or 0),
        )
        # An agent starting a fresh session gets the conversation handed over,
        # tool calls included.
        handover = provider.TOOLS and not conversation["provider_session"]
        run_events = self.db.runs_events([m["run_id"] for m in rows if m["run_id"]]) if handover else {}
        history = []
        for m in rows:
            ids = [a["id"] for a in json.loads(m["attachments"])] if m["attachments"] else []
            earlier = uploads.in_conversation(conversation["id"], ids) if ids else []
            tools = [tool_line(e) for e in run_events.get(m["run_id"], []) if e.get("type") == "tool_call"]
            if m["content"] or earlier or tools:
                # Providers that get the whole history each time (chat APIs)
                # need earlier files too, or follow-up questions lose them.
                history.append({"role": m["role"], "content": m["content"], "attachments": earlier,
                                 "tools": tools})
        context = Context(
            conversation_id=conversation["id"],
            directory=_directory(conversation),
            mode=conversation["mode"],
            model=conversation["model"],
            provider_session=conversation["provider_session"],
            history=history,
            attachments=attachments,
            fork_from=conversation.get("fork_session"),
            fork_before=conversation.get("fork_before"),
            agent_mode=Modes(self.db).get(conversation["user_id"], conversation.get("agent_mode")),
            effort=conversation.get("effort"),
        )
        if conversation.get("agentos_role") == "governor":  # (in its own chat it stays the Governor)
            context.agent_mode = governing(context.agent_mode)
        context.agent_mode = with_instructions(context.agent_mode, conversation.get("instructions"))
        # (asked anew each time: a clone the Governor makes during this run is its to read)
        context.read_boundary = lambda: read_boundary(
            self.db, self.db.one("SELECT * FROM conversations WHERE id = ?", (conversation["id"],)) or conversation,
            context.directory)

        # Switched mode since the previous turn? A session that goes on still has the
        # old mode's instructions in it; the agent is told they no longer apply.
        previous = self.db.one("SELECT agent_mode FROM runs WHERE conversation_id = ? ORDER BY created DESC LIMIT 1",
                               (conversation["id"],))
        now_mode = _mode_id(context.agent_mode)
        if previous is not None and (previous["agent_mode"] or None) != now_mode:
            context.mode_switched = True
            context.mode_before = Modes(self.db).get(conversation["user_id"], previous["agent_mode"])
        run_id = uuid.uuid4().hex
        now = time.time()
        if relay_action is not None:
            relay_action = {**relay_action,
                            "to": {"provider_id": conversation["provider_id"], "model": conversation["model"]},
                            "handover": bool(handover and history)}
        with self.db.transaction() as conn:
            message_id = reuse_message
            if reuse_message is None:
                message_id = conn.execute(
                    "INSERT INTO messages (conversation_id, role, content, attachments, created, relay_action) "
                    "VALUES (?, 'user', ?, ?, ?, ?)",
                    (conversation["id"], prompt,
                     json.dumps([a.public() for a in attachments]) if attachments else None, now,
                     json.dumps(relay_action) if relay_action is not None else None),
                ).lastrowid
            conn.execute(
                "INSERT INTO runs (id, conversation_id, status, created, provider_id, model, agent_mode, effort) "
                "VALUES (?, ?, 'running', ?, ?, ?, ?, ?)",
                (run_id, conversation["id"], now, conversation["provider_id"], conversation["model"],
                 _mode_id(context.agent_mode), context.effort),
            )
            # Someone (or the scheduler) goes on now: no resume pending any more.
            conn.execute("UPDATE conversations SET updated = ?, resume_at = NULL WHERE id = ?",
                         (now, conversation["id"]))
        run = ActiveRun(run_id, conversation["id"], conversation["provider_id"], message_id)
        run.project = project
        run.parent_run = conversation.get("parent_run")
        run.parent_conversation = conversation.get("parent_conversation")
        run.label = conversation.get("title") or ""
        sent = self._expand_skill(provider, conversation, prompt) or prompt
        subagent = bool(conversation.get("parent_run"))
        if conversation["mode"] == "code" and not subagent:  # the calling agent's run covers them
            run.task = dict(conversation)
        if provider.has_tools() and self.delegation and self.local_url:
            run.mcp_token = self.delegation.token_for(run_id)
            if subagent:
                # Subagents get no Relay tools (one level deep) but the web search, and a way to ask permission.
                context.approvals_url = f"{self.local_url}/mcp/{run.mcp_token}"
            else:
                # Relay's own tools, next to the agent's.
                context.mcp_url = f"{self.local_url}/mcp/{run.mcp_token}"
        turned_off = relay_tools.off(conversation)  # (Relay's own tools the user turned off here)
        context.subagents = "subagents" not in turned_off
        if provider.has_tools() and self.search is not None and self.search.enabled() and "web" not in turned_off:
            context.search = self.search.text  # (subagents too: a search is only a look)
        if provider.has_tools() and self.fetch is not None and "web" not in turned_off:
            context.fetch = self.fetch.text  # (and so is a fetch)
        if context.mcp_url and "computers" not in turned_off:
            context.computers = [r["name"] for r in self.db.all(
                "SELECT name FROM computers WHERE user_id = ? ORDER BY name", (conversation["user_id"],))]
            if conversation.get("computer_id"):
                found = self.db.one("SELECT name FROM computers WHERE id = ?", (conversation["computer_id"],))
                context.computer = found["name"] if found else None
        if not subagent and context.mcp_url and "memory" not in turned_off:
            # Relay's memory: with Relay's tools (as the agent calls them), or in a chat model's system prompt
            style = ("mcp" if provider.RELAY_TOOLS_VIA_MCP else "script" if provider.TOOLS
                     else "functions" if provider.runs_tools() else "read")
            context.memory = memory.note(self.db, conversation["user_id"], memory.project_of(self.db, conversation),
                                         style, used=time.time())
        if provider.TOOLS and not subagent and context.mcp_url:
            # Agents with a shell run Relay's tools as a script; chat models get them as functions.
            try:
                context.tools_path = self.delegation.script_for(conversation, self.local_url)
            except OSError as exc:
                print(f"relay: no tools script: {exc}", file=sys.stderr, flush=True)
            else:
                offered = skills.all_skills()
                via_mcp = provider.RELAY_TOOLS_VIA_MCP
                # A project in a larger git repository: the agent is told to keep git to its folder.
                top = git.repo_root(Path(context.directory)) if context.directory else None
                repository = str(top) if top and top.resolve() != Path(context.directory).resolve() else None
                if provider.NATIVE_SKILLS:
                    context.skills_dir = skills.claude_folder()
                # Briefly; `relay help` has the rest (and the skills' descriptions, for those it doesn't load).
                # A service's WorkBox: how a version of it runs, so its data goes where it stays
                service = self.db.one("SELECT s.name, s.command FROM service_clones k JOIN services s ON s.id = "
                                      "k.service_id WHERE k.conversation_id = ?", (conversation["id"],))
                context.tools_note = note(context.tools_path,
                                          None if provider.NATIVE_SKILLS else [s.name for s in offered],
                                          context.computers, context.computer, via_mcp=via_mcp,
                                          repository=repository, code=conversation["mode"] == "code",
                                          fetch=context.fetch is not None, subagents=context.subagents,
                                          on_path=self.containers is not None, search=bool(context.search),
                                          service=service, scripts=self._public_scripts(),
                                          plugins=self._plugin_tools(conversation["id"]))
                if context.memory:
                    context.tools_note += f"\n\n{context.memory}"
                # A session is told once; forks, switches and fresh sessions again, and
                # again when the script lives elsewhere now (another machine) or skills changed.
                told = conversation.get("tools_session")
                # …and when the mode's instructions changed (another mode, or its text edited):
                # a session that goes on doesn't necessarily see a new system prompt.
                mode = context.agent_mode
                told_mode = hashlib.sha256(f"{mode.id}:{mode.guidance}".encode()).hexdigest()[:12] if mode else ""
                what = (f"{context.tools_path}|{skills.signature(offered)}|{','.join(context.computers)}"
                        f"|{context.computer or ''}|{told_mode}"
                        f"|{hashlib.sha256(context.tools_note.encode()).hexdigest()[:12]}")  # (its text changed)
                context.tell_tools = (not told or told != conversation["provider_session"]
                                      or conversation.get("tools_told_path") != what)
                run.tools = True
                run.tools_path = what
        elif provider.TOOLS and subagent and (context.search or context.fetch) and context.approvals_url \
                and not provider.RELAY_TOOLS_VIA_MCP:
            # A subagent with a shell: of Relay's tools only the web's, as `relay search` and `relay
            # fetch` (one that takes MCP tools has them as those: delegation)
            try:
                context.tools_path = self.delegation.script_for(conversation, self.local_url)
            except OSError as exc:
                print(f"relay: no tools script: {exc}", file=sys.stderr, flush=True)
            else:
                context.tools_note = search_note("relay" if self.containers is not None else str(context.tools_path),
                                                 search=bool(context.search))
                context.tell_tools = True
        run.directory = str(context.directory) if context.directory else None
        with self.lock:
            self.active[run_id] = run
            if self.event_flusher is None:
                self.event_flusher = threading.Thread(target=self._flush_events, name="run-events", daemon=True)
                self.event_flusher.start()
        threading.Thread(target=self._execute, args=(run, provider, context, prompt, sent),
                         name=f"run-{run_id[:8]}", daemon=True).start()
        return run_id

    def _plugin_tools(self, conversation_id: str) -> list[str]:
        """The names of the tools the user's plugins add (relay.plugins): the Governor's only those meant for it."""
        if self.delegation is None:
            return []
        relay = self.delegation.relay
        return [t["name"] for t in relay.plugins.tools(governor=relay.agentos.is_governor(conversation_id),
                                                       conversation_id=conversation_id)]

    def _public_scripts(self) -> str | None:
        """The user's public scripts' folder (relay.scripts), for agents to know of; when it's there."""
        public = getattr(self.containers, "scripts_public", None)
        return str(public) if public is not None and Path(public).is_dir() else None

    def _flush_events(self) -> None:
        wait = threading.Event()
        while True:
            wait.wait(EVENT_FLUSH_SECONDS)
            with self.lock:
                runs = list(self.active.values())
                if not runs:
                    self.event_flusher = None
                    return
            for run in runs:
                try:
                    run.flush(self.db)
                except Exception as exc:  # noqa: BLE001 - final storage retries; don't kill the flusher
                    print(f"relay: couldn't persist events for {run.id}: {exc}", file=sys.stderr, flush=True)

    def _expand_skill(self, provider, conversation: dict[str, Any], prompt: str) -> str | None:
        """A skill called as a command (`/code-review …`), unless the agent has
        a command of that name itself: then that one's meant."""
        name = skills.command_name(prompt)
        if name is None:
            return None
        if provider.TOOLS:
            try:
                if any(c["name"] == name for c in provider.commands(Path(conversation["directory"]))):
                    return None
            except Exception:  # noqa: BLE001 - a command list is a nicety
                pass
        context = ""
        if conversation["mode"] == "code":
            context = f"This is a code task in {conversation['directory']}."
            if conversation.get("git_mode") == "worktree" and conversation.get("base_commit"):
                context += (f" It started at commit {conversation['base_commit'][:12]}, so "
                            f"`git diff {conversation['base_commit'][:12]}` shows all of its changes.")
        return skills.expand(prompt, context)

    def shutdown(self, timeout: float = 10) -> None:
        """Stop every active run (e.g. when Relay exits) so no agent is left
        running unattended, and give them `timeout` seconds to finish."""
        self.shutting_down = True
        with self.lock:
            run_ids = list(self.active)
        for run_id in run_ids:
            self.stop(run_id, "Relay restarted")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                if not self.active:
                    return
            time.sleep(0.1)

    def stop_and_wait(self, run_id: str, timeout: float = 15, why: str | None = None) -> bool:
        """Stop a run and wait until it has ended; False if it didn't in time."""
        if not self.stop(run_id, why):
            return True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                if run_id not in self.active:
                    return True
            time.sleep(0.05)
        return False

    def busy_folders(self) -> set[Path]:
        """The folders agents work in now."""
        with self.lock:
            return {Path(run.directory).resolve() for run in self.active.values() if run.directory and not run.finished}

    def stop(self, run_id: str, why: str | None = None) -> bool:
        """Stop a run; `why` (the first one given) goes with its end: "the user stopped it", say."""
        with self.lock:
            run = self.active.get(run_id)
        if run is None:
            return False
        run.stop_reason = run.stop_reason or why
        run.stop_requested = True
        for entry in list(run.pending.values()):
            self._cancel_form_timer(entry)
        if run.provider_run is not None:
            threading.Thread(target=run.provider_run.interrupt, daemon=True).start()
        return True

    def place(self, provider, context: Context, run: ActiveRun | None = None) -> str | None:
        """Where a provider's run works, as every run does (and the conformance check's): its commands in the
        folder's WorkBox (a provider with tools), its program in its ProviderBox. That box's key, if any."""
        if provider.has_tools() and self.containers is not None:
            # The agent's commands run in the folder's container (relay.containers).
            context.shell = self.containers.shell(context.directory)
            self.containers.route(context.directory)
            context.shared_shell = self.containers.shared_shell()
            context.browser_argv = self.containers.browser_argv(context.directory)
            context.browser_log = self.containers.state / "browser.log"
            # Its attachments where its commands run too (a PDF, a zip, a spreadsheet are read with a
            # command: they weren't in the WorkBox, the user found)
            attached = [a.path for a in context.attachments] + [
                a.path for m in context.history for a in m.get("attachments") or []]
            if attached:
                try:
                    self.containers.provide(context.directory, attached)
                except Exception as exc:  # noqa: BLE001 - its file tools still read them
                    print(f"relay: attachments not in the WorkBox: {exc}", file=sys.stderr, flush=True)
        key = provider.box_key() if self.boxes is not None else None
        if key:
            # The provider's own program runs in its box (relay.boxes), with this folder in it.
            with self.lock:
                busy = any(r is not run and r.box_key == key for r in self.active.values())
            context.box = self.boxes.launch(key, provider.box_mounts(), context.directory, busy,
                                            detached=provider.detached, image=provider.box_image(),
                                            home=provider.box_home())
        return key

    def place_check(self, provider, context: Context):
        """A conformance check's turn (relay.providers.conformance) where a run's would be (place()), its permission
        requests answered as a run's are (an approvals-only token). What to call when the turn is over."""
        self.place(provider, context)
        if not (provider.has_tools() and self.delegation and self.local_url):
            return lambda: None
        token = self.delegation.check_token(provider, context.directory)
        context.approvals_url = f"{self.local_url}/mcp/{token}"
        return lambda: self.delegation.forget(token)

    def _execute(self, run: ActiveRun, provider, context: Context, prompt: str, sent: str) -> None:
        session = None
        status = "failed"
        started = time.monotonic()
        billing: list[str | None] = []  # (who pays, when the provider couldn't tell: asked once)
        try:
            if run.task is not None:
                # Where to rewind to: the files before this turn (your own edits in them).
                checkpoint = tasks.before_run(run.task, prompt)
                self.db.execute("UPDATE runs SET checkpoint = ? WHERE id = ?", (checkpoint, run.id))
                if checkpoint and not run.task.get("base_commit") and tasks.checkpointed(run.task):
                    self.db.execute("UPDATE conversations SET base_commit = ? WHERE id = ?",
                                    (checkpoint, run.conversation_id))
            unchecked = self.containers is not None and (
                self.containers.checked is None or (self.boxes is not None and self.boxes.checked is None))
            if provider.has_tools() and unchecked and not self._containers_ready(run):
                raise _Stopped()
            run.box_key = self.place(provider, context, run)
            run.provider_run = provider.start(context, sent)
            # Stopped before anything started: don't start the agent at all.
            events = () if run.stop_requested else run.provider_run.events()
            try:
                for event in events:
                    if event.get("type") == "session":
                        session = event.get("session") or session
                        self.db.execute("UPDATE runs SET session = ? WHERE id = ?", (session, run.id))
                        continue  # internal: Relay tracks sessions itself
                    if event.get("type") == "title":
                        self._auto_title(run.conversation_id, event.get("title"))
                        continue
                    if event.get("type") == "turn":  # internal: lets a fork or edit start before it
                        self.db.execute("UPDATE messages SET provider_ref = ? WHERE id = ?",
                                        (event.get("id"), run.message_id))
                        continue
                    if (event.get("type") == "permission" and event.get("decision") is None
                            and run.provider_run.waits_for_answers()):
                        request_id = event.get("id")
                        self.ask(run, event, lambda ok, rid=request_id: run.provider_run.answer_permission(rid, ok))
                        continue
                    if event.get("type") == "form":
                        request_id = str(event["id"])
                        if event.get("status") == "pending":
                            self.ask(run, event, lambda answer, rid=request_id:
                                     run.provider_run.answer_form(rid, answer))
                        else:
                            pending = run.pending.get(request_id)
                            if pending is None:
                                self._request_event(run, event)
                            else:
                                with pending["lock"]:
                                    run.pending.pop(request_id, None)
                                    self._cancel_form_timer(pending)
                                    self._request_event(run, {**pending["event"], **event})
                        continue
                    if run.stop_requested and event.get("type") in ("error", "done"):
                        continue  # the interruption's own fallout; reported below
                    if event.get("type") == "done":
                        status = event.get("status", "failed")
                        continue
                    if event.get("type") == "usage" and not event.get("billing"):
                        # Who pays, when the provider couldn't tell (opencode asked in a moment's hiccup): what
                        # this model's last runs said, or the provider says now
                        if not billing:
                            billing.append(policy.billing(self.db, self.registry, provider.template.id, context.model))
                        if billing[0]:
                            event = {**event, "billing": billing[0]}
                    run.add(event)
            finally:
                close = getattr(events, "close", None)
                if close is not None:
                    close()  # the provider's own cleanup (its process), also after a failure here
                if context.box is not None:
                    try:
                        context.box.end()  # what the run started in the box (podman exec leaves it)
                    except Exception as exc:  # noqa: BLE001 - never lose the run over it
                        print(f"relay: couldn't end the run in its box: {exc}", file=sys.stderr, flush=True)
        except _Stopped:
            pass  # stopped before the agent started
        except (ProviderError, TemplateError, OSError, ContainerError) as exc:
            run.add({"type": "error", "message": str(exc)})
        except Exception as exc:  # noqa: BLE001 - a provider bug must not kill the run silently
            run.add({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        if run.stop_requested:
            status = "interrupted"
        if self.delegation is not None:
            # Subagents stop with the run; wait for them so their cards are stored as they ended.
            run.closing = True
            self.delegation.settle(run.id)
        if run.task is not None:
            try:
                after = tasks.after_run(run.task, prompt)  # what the agent changed in this turn
                self.db.execute("UPDATE runs SET after_checkpoint = ? WHERE id = ?", (after, run.id))
            except Exception as exc:  # noqa: BLE001 - never lose the run over it
                print(f"relay: could not record the turn: {exc}", file=sys.stderr, flush=True)
        for request_id, entry in list(run.pending.items()):
            if entry["event"]["type"] == "form":
                with entry["lock"]:
                    if run.pending.pop(request_id, None) is not None:
                        self._cancel_form_timer(entry)
                        self._request_event(run, {**entry["event"], "status": "cancelled", "message": "The run ended"})
        run.add({"type": "done", "status": status, **({"why": run.stop_reason} if run.stop_reason else {})})
        try:
            self._after_stop(run, context, status)  # before listeners hear it's over
        except Exception as exc:  # noqa: BLE001 - never lose the run over it
            print(f"relay: could not note the usage limit: {exc}", file=sys.stderr, flush=True)
        try:
            self._store(run, session, status)
        except Exception as exc:  # noqa: BLE001 - e.g. the conversation was deleted meanwhile
            print(f"relay: could not store run {run.id}: {exc}", file=sys.stderr, flush=True)
        finally:
            run.pending.clear()
            with self.lock:
                self.active.pop(run.id, None)
            if run.mcp_token:
                self.delegation.forget(run.mcp_token)
                self.delegation.run_ended(run.id)
            if status == "succeeded":
                # Before telling listeners this run is over, so they find the next one going.
                self._send_queued(run.conversation_id)
            elif run.go_on:
                self._go_on(run.conversation_id, run.go_on)  # a fallback agent takes over
            try:
                self._timed_after(run, status)  # /timed: before its time, the agent goes on
            except Exception as exc:  # noqa: BLE001 - never lose the run over it
                print(f"relay: couldn't keep the timed request: {exc}", file=sys.stderr, flush=True)
            if self.delegation is not None:  # AgentOS: a wake-up's tasks (after a fallback took over)
                try:
                    self.delegation.relay.agentos.run_ended(run.id, status, run.conversation_id, run.stop_reason)
                except Exception as exc:  # noqa: BLE001 - never lose the run over it
                    print(f"relay: AgentOS couldn't note the run's end: {exc}", file=sys.stderr, flush=True)
                try:  # a new provider's agent done building it: Relay checks it, and starts its trial
                    self.delegation.relay.creations.run_ended(run.conversation_id)
                except Exception as exc:  # noqa: BLE001 - never lose the run over it
                    print(f"relay: couldn't follow the new provider: {exc}", file=sys.stderr, flush=True)
                try:  # plugins that want to know (relay.plugins: each in a thread of its own)
                    row = self.db.one("SELECT user_id, temporary, external FROM conversations WHERE id = ?",
                                      (run.conversation_id,)) or {}
                    errors = [e.get("message") for e in run.events if e.get("type") == "error"]
                    self.delegation.relay.plugins.run_ended({
                        "conversation_id": run.conversation_id, "run_id": run.id, "user_id": row.get("user_id"),
                        "provider_id": provider.template.id, "model": context.model or "", "status": status,
                        "error": (errors[-1] if errors else None) if status == "failed" else None,
                        "subagent": run.parent_run is not None, "temporary": bool(row.get("temporary")),
                        # (who started it outside Relay's pages: a script, the SDK, a plugin's own app password)
                        "started_by": row.get("external")})
                except Exception as exc:  # noqa: BLE001 - never lose the run over it
                    print(f"relay: couldn't tell the plugins: {exc}", file=sys.stderr, flush=True)
            run.finish()
        try:
            health.after_run(self.db, provider.template.id, context.model, status, run.events,
                             time.monotonic() - started)
        except Exception as exc:  # noqa: BLE001 - never fail a run over it
            print(f"relay: could not record the model's status: {exc}", file=sys.stderr, flush=True)
        named = self.db.one("SELECT cleared_after, title_auto FROM conversations WHERE id = ?",
                            (run.conversation_id,)) or {}
        # A title from the model for a first answer; not for a chat the user (or a script) named: its own stays,
        # and no model is asked for one
        if (status == "succeeded" and context.history == [] and named.get("title_auto")
                and not named.get("cleared_after")):
            answer = answer_text(run.events)
            try:
                self._auto_title(run.conversation_id,
                                 provider.suggest_title(prompt, answer, context.model or None))
            except Exception:  # noqa: BLE001 - a title is a nicety; never fail a run over it
                pass
        self._notify(run, status, time.monotonic() - started)

    def _after_stop(self, run: ActiveRun, context: Context, status: str) -> None:
        """When the model stopped working (a usage limit, or an answer that failed without
        doing anything): go on right away with the next agent of the conversation's fallback
        loop (relay.fallback); when the round is over, start the loop again at the first known
        reset (or in LIMITS_UNKNOWN hours), for a few rounds. Without a loop: go on when the
        limit resets (in LIMITS_UNKNOWN hours once when nobody says), else give up and say so.
        An answer that worked clears it all."""
        if status == "succeeded":
            self.db.execute("UPDATE conversations SET limit_info = NULL WHERE id = ?", (run.conversation_id,))
            return
        if status != "failed":
            return
        found = limits.reached(run.events)
        did_nothing = not any(e["type"] in ("text", "tool_call", "reasoning") for e in run.events)
        if found is None and not did_nothing:
            return  # it worked on it and then failed: that's for the user to look at
        row = self.db.one("SELECT * FROM conversations WHERE id = ?", (run.conversation_id,))
        if row is None:
            return
        errors = [e.get("message", "") for e in run.events if e["type"] == "error"]
        before = json.loads(row["limit_info"]) if row["limit_info"] else {}
        attempts = (before.get("attempts") or 0) + 1
        stopped = {"provider": run.provider_id, "model": context.model or ""}
        # A model that can't be used (it doesn't exist, isn't the login's, isn't served here): another agent of a
        # fallback loop may go on, but trying this one again later is no plan
        unusable = found is None and any(limits.unusable(message) for message in errors)
        info = {"run_id": run.id,
                "message": (found or {}).get("message") or (errors[-1] if errors else "It gave no answer"),
                "limit": found is not None, "resets_at": (found or {}).get("resets_at"),
                "provider_id": run.provider_id, "model": context.model, "attempts": attempts,
                "tried": [*before.get("tried", []), fallback.key(stopped)], "unusable": unusable}
        resume = None
        if not row["parent_run"] and tunables.get("limits.resume"):  # a subagent's is its caller's business
            going = (before.get("loop") or {}).get("id")
            loop = fallback.loop_for(self.db, row["user_id"], row.get("fallback_loop"), run.provider_id,
                                     context.model, going)
            if loop is not None:
                info["loop"] = {"id": loop["id"], "name": loop["name"],
                                "round": (before.get("loop") or {}).get("round") or 1}
                confined = _agentos(row)
                nxt = fallback.next_in(self.db, self.registry, loop, stopped, info["tried"],
                                       needs_tools=row["mode"] == "code" or bool(row.get("computer_id")),
                                       confined=confined)
                if nxt is not None:
                    info["fell_back"] = {"provider_id": nxt["provider"], "model": nxt["model"]}
                    self.switch_agent(row, nxt["provider"], nxt["model"], limit_info=info)
                    previous = self.display_name(run.provider_id, context.model)
                    why = "hit its usage limit" if found else "stopped without an answer"
                    run.go_on = (f"{previous} {why}, so you take over. Please continue where it left off; "
                                 "if the task was done, say so briefly.")
                    return
                # The round is over: none of the loop's agents can go on now.
                restart = next((e for e in loop["entries"] if not confined or fallback.confined(self.registry, e)),
                               None)
                if info["loop"]["round"] >= tunables.get("limits.loop_rounds") or restart is None or unusable:
                    info["gave_up"] = True
                else:
                    reset = fallback.first_reset(self.db, loop, info["resets_at"])
                    if reset:
                        resume = max(reset + tunables.get("limits.margin"), time.time() + MIN_RESUME_WAIT)
                    else:
                        resume = time.time() + tunables.get("limits.unknown_wait") * 3600
                        info["guessed"] = True
                    info["restart"] = dict(restart)  # the loop again, from its start
            else:
                resets = info["resets_at"]
                if resets and attempts <= MAX_RESUMES:
                    resume = max(resets + tunables.get("limits.margin"), time.time() + MIN_RESUME_WAIT)
                elif not resets and attempts == 1 and not unusable:
                    resume = time.time() + tunables.get("limits.unknown_wait") * 3600  # nobody said when: try once
                    info["guessed"] = True
                else:
                    info["gave_up"] = True
        self.db.execute("UPDATE conversations SET limit_info = ?, resume_at = ? WHERE id = ?",
                        (json.dumps(info), resume, row["id"]))

    def switch_agent(self, row: dict[str, Any], provider_id: str, model: str | None,
                     limit_info: dict[str, Any] | None = None) -> None:
        """Let another agent go on with the conversation (another provider gets it handed over)."""
        switch = {"provider_id": provider_id, "model": model or None}
        if provider_id != row["provider_id"]:
            switch.update(provider_session=None, fork_session=None, fork_before=None, effort=None)
        self.db.execute(f"UPDATE conversations SET {', '.join(f'{k} = ?' for k in switch)}, limit_info = ?, "
                        "resume_at = NULL WHERE id = ?",
                        (*switch.values(), json.dumps(limit_info) if limit_info else None, row["id"]))

    def restart_loop(self, row: dict[str, Any]) -> dict[str, Any]:
        """A fallback loop's next round: its first agent goes on (the scheduler, at the reset)."""
        info = json.loads(row["limit_info"]) if row["limit_info"] else {}
        start = info.get("restart")
        if not start:
            return row
        # Its first agent that can go on (one still limited is skipped, as within a round).
        loop = fallback.find(fallback.load(self.db, row["user_id"]), (info.get("loop") or {}).get("id"))
        needs_tools = row["mode"] == "code" or bool(row.get("computer_id"))
        start = next((e for e in (loop or {}).get("entries", [])
                      if fallback.usable(self.db, self.registry, e, needs_tools, _agentos(row))), start)
        loop = dict(info.get("loop") or {})
        loop["round"] = (loop.get("round") or 1) + 1
        self.switch_agent(row, start["provider"], start["model"],
                          limit_info={**{k: v for k, v in info.items() if k not in ("restart", "tried", "fell_back")},
                                      "loop": loop, "tried": []})
        return self.db.one("SELECT * FROM conversations WHERE id = ?", (row["id"],))

    def display_name(self, provider_id: str, model: str | None) -> str:
        try:
            name = self.registry.get(provider_id).name
        except TemplateError:
            name = provider_id
        return name + (f" · {model}" if model else "")

    def _timed_after(self, run: ActiveRun, status: str) -> None:
        """/timed (relay.timed): an agent that stopped before its time is told to go on (or, in the soft
        part, asked once); the user's Stop ends the timer. Not when a queued message or a fallback
        agent went on already."""
        row = self.db.one("SELECT * FROM conversations WHERE id = ?", (run.conversation_id,))
        timer = json.loads(row["timed"]) if row and row.get("timed") else None
        if not timer or timer.get("ended") or row["task_state"] == "discarded":
            return
        if status == "interrupted":
            if not self.shutting_down:
                self.db.execute("UPDATE conversations SET timed = ? WHERE id = ?",
                                (json.dumps({**timer, "ended": "you stopped the agent"}), row["id"]))
            return
        if status != "succeeded" or self.active_run_for(row["id"]):
            return
        prompt, timer = timed.next_prompt(timer)
        if prompt is None:
            return
        self.db.execute("UPDATE conversations SET timed = ? WHERE id = ?", (json.dumps(timer), row["id"]))
        self.start(row, prompt, relay_action={"kind": "timed"})

    def resume_timed(self) -> list[str]:
        """After a restart of Relay: a /timed agent that was at work before its MIN, its turn cut off by
        the restart (not by the user: that ends the timer), goes on. Not one waiting for a usage limit's
        reset. The conversations it went on in."""
        went_on = []
        for row in self.db.all("SELECT * FROM conversations WHERE timed IS NOT NULL "
                               "AND json_extract(timed, '$.ended') IS NULL"):
            timer = json.loads(row["timed"])
            last = self.db.one("SELECT status, error FROM runs WHERE conversation_id = ? ORDER BY created DESC LIMIT 1",
                               (row["id"],))
            cut_off = last is not None and (last["status"] == "interrupted"
                                            or last["error"] == "Relay restarted during this run")
            if (not cut_off or row["task_state"] == "discarded" or row.get("resume_at")
                    or self.active_run_for(row["id"]) or timed.phase(timer) != "hard"):
                continue
            prompt, timer = timed.next_prompt(timer)
            try:
                self.start(row, prompt, relay_action={"kind": "timed"})
                went_on.append(row["id"])
            except Exception as exc:  # noqa: BLE001 - e.g. its provider was removed meanwhile
                print(f"relay: couldn't go on with a timed request: {exc}", file=sys.stderr, flush=True)
        return went_on

    def _go_on(self, conversation_id: str, prompt: str) -> None:
        conversation = self.db.one("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
        if conversation is None or conversation["task_state"] == "discarded":
            return
        if timed.time_up(conversation.get("timed")):
            return  # a timed request's time is up: no fallback agent after it
        try:
            info = json.loads(conversation["limit_info"]) if conversation["limit_info"] else {}
            action = {"kind": "fallback", "from": {"provider_id": info.get("provider_id"), "model": info.get("model")}}
            self.start(conversation, prompt, relay_action=action)
        except Exception as exc:  # noqa: BLE001 - never break the run that just ended
            print(f"relay: couldn't go on with a fallback agent: {exc}", file=sys.stderr, flush=True)

    def _send_queued(self, conversation_id: str) -> None:
        """Start the next message the user queued while this run went on.

        Only after a run that succeeded: one that failed or was stopped needs
        the user's attention first, so the queue waits.
        """
        conversation = self.db.one("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
        queued = self.db.one("SELECT * FROM queued_messages WHERE conversation_id = ? ORDER BY id LIMIT 1",
                             (conversation_id,))
        if conversation is None or queued is None or conversation["task_state"] == "discarded":
            return
        ids = [a["id"] for a in json.loads(queued["attachments"])] if queued["attachments"] else []
        attachments = Uploads(self.db).in_conversation(conversation_id, ids) if ids else []
        try:
            self.start(conversation, queued["content"], attachments)
        except Exception as exc:  # noqa: BLE001 - never break the run that just ended
            print(f"relay: could not send a queued message: {exc}", file=sys.stderr, flush=True)
            return  # it stays queued, for the user to send or drop
        self.db.execute("DELETE FROM queued_messages WHERE id = ?", (queued["id"],))

    def title_now(self, conversation_id: str) -> None:
        """The model's title for a chat from its first question and answer, as a new chat gets one after its first
        answer: for a temporary chat that's kept (it had none)."""
        try:
            row = self.db.one("SELECT provider_id, model FROM conversations WHERE id = ?", (conversation_id,))
            said = self.db.all("SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY id LIMIT 2",
                               (conversation_id,))
            if row is None or [m["role"] for m in said] != ["user", "assistant"]:
                return
            provider = self.registry.provider(row["provider_id"])
            self._auto_title(conversation_id, provider.suggest_title(said[0]["content"], said[1]["content"] or "",
                                                                     row["model"] or None))
        except Exception:  # noqa: BLE001 - a title is a nicety
            pass

    def _auto_title(self, conversation_id: str, title: str | None) -> None:
        """Use a provider's or model's title, unless the user named the chat."""
        # Models sometimes name a chat after their own markup: "<tool_call>", or special tokens such as
        # "<|im_end|>" and Big Pickle's "<｜｜DSML｜｜ calls>" (those go wherever they are)
        title = re.sub(r"<[|｜][^<>]*>", " ", title or "")
        title = _unquoted(" ".join(title.split()))[:120]
        if re.fullmatch(r"<[^<>]*>|[^\w]*", title):
            return
        if title:
            self.db.execute("UPDATE conversations SET title = ? WHERE id = ? AND title_auto = 1",
                            (title, conversation_id))

    def _notify(self, run: ActiveRun, status: str, seconds: float) -> None:
        if self.notifier is None:
            return
        row = self.db.one("SELECT title, mode, parent_run, managed, agentos_role, temporary FROM conversations "
                          "WHERE id = ?", (run.conversation_id,))
        stored = self.db.one("SELECT changes FROM runs WHERE id = ?", (run.id,))
        if row is None or row["parent_run"]:
            return  # a subagent's run is part of the calling agent's, which is announced
        if row["temporary"]:
            return  # (a temporary chat: its user is there, or it's a script's one-shot)
        errors = [e["message"] for e in run.events if e["type"] == "error"]
        self.notifier.run_finished(Finished(
            conversation_id=run.conversation_id, title=row["title"], mode=row["mode"],
            status=status, seconds=seconds,
            text=answer_text(run.events),
            error=errors[-1] if errors else None,
            changes=json.loads(stored["changes"]) if stored and stored["changes"] else None,
            agentos=_agentos(row),
        ))

    def _store(self, run: ActiveRun, session: str | None, status: str) -> None:
        # Stored exactly as streamed, so event numbers seen live (and sent back
        # by reconnecting browsers) stay valid after the run ends.
        events = run.events
        if self.db.one("SELECT 1 FROM runs WHERE id = ?", (run.id,)) is None:
            return  # the conversation was deleted while the run was going
        changes = self._changes_summary(run.conversation_id, run.id)
        text = answer_text(events)
        errors = [e["message"] for e in events if e["type"] == "error"]
        usage = next((e for e in reversed(events) if e["type"] == "usage"), None)
        now = time.time()
        run.flush(self.db)
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO messages (conversation_id, role, content, run_id, created) "
                "VALUES (?, 'assistant', ?, ?, ?)",
                (run.conversation_id, text, run.id, now),
            )
            conn.execute(
                "UPDATE runs SET status = ?, error = ?, usage = ?, changes = ?, finished = ? "
                "WHERE id = ?",
                (status, "\n".join(errors) or None,
                 None if usage is None else json.dumps(usage),
                 None if changes is None else json.dumps(changes), now, run.id),
            )
            if session:
                # Only if the chat still uses this provider: switching providers
                # mid-run resets the session, which must not be undone here.
                # A fork's own session now exists; it no longer needs its origin.
                conn.execute(
                    "UPDATE conversations SET provider_session = ?, fork_session = NULL, "
                    "fork_before = NULL, updated = ? WHERE id = ? AND provider_id = ?",
                    (session, now, run.conversation_id, run.provider_id))
                if run.tools:  # the prompt told this session about them
                    conn.execute("UPDATE conversations SET tools_session = ?, tools_told_path = ? "
                                 "WHERE id = ? AND provider_id = ?",
                                 (session, run.tools_path, run.conversation_id, run.provider_id))

    def _changes_summary(self, conversation_id: str, run_id: str) -> dict | None:
        """What this answer changed (since its checkpoint), shown under it; the task's whole
        changes are in the header. None when there's no checkpoint to tell from."""
        row = self.db.one("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
        before = self.db.one("SELECT checkpoint FROM runs WHERE id = ?", (run_id,))
        if row is None or row["mode"] != "code" or not before or not before["checkpoint"]:
            return None
        return tasks.summary({**row, "base_commit": before["checkpoint"]})

    def event_count(self, run_id: str) -> int:
        """How many events a run has now: what a browser that opens it now catches up on."""
        with self.lock:
            run = self.active.get(run_id)
        if run is not None:
            with run.lock:
                return len(run.events)
        return self.db.one("SELECT count(*) AS n FROM run_events WHERE run_id = ?", (run_id,))["n"]

    async def follow(
        self, run_id: str, after: int = 0, heartbeat: float | None = None
    ) -> AsyncIterator[tuple[int, Event] | None]:
        """Yield (index, event) from `after` on; live while the run is active.

        With `heartbeat`, yields None after that many idle seconds, so a caller
        can keep a connection alive while an agent works silently.
        """
        with self.lock:
            run = self.active.get(run_id)
        if run is None:
            for index, event in enumerate(self.db.run_events(run_id)):
                if index >= after:
                    yield index, event
            return
        loop = asyncio.get_running_loop()
        flag = asyncio.Event()
        listener = (loop, flag)
        with run.lock:
            run.listeners.add(listener)
        try:
            index = after
            while True:
                flag.clear()
                with run.lock:
                    pending = run.events[index:]
                    finished = run.finished
                for event in pending:
                    yield index, event
                    index += 1
                if finished and index >= len(run.events):
                    return
                try:
                    await asyncio.wait_for(flag.wait(), timeout=heartbeat)
                except asyncio.TimeoutError:
                    yield None
        finally:
            with run.lock:
                run.listeners.discard(listener)


def _agentos(row: dict[str, Any]) -> bool:
    """AgentOS's conversation (the Governor's, or one it manages): its agents only on confined providers."""
    return bool(row.get("managed") or row.get("agentos_role"))


def _unquoted(title: str) -> str:
    """A title without the quotes and full stop a model put around it ("Fix the login." is Fix the login), but with
    those within it (Adding note "Buy milk")."""
    title = title.strip(" .")
    for quote in "\"'":
        if len(title) > 1 and title[0] == title[-1] == quote and (quote == "'" or title.count(quote) == 2):
            title = title[1:-1].strip(" .")  # (around all of it)
        elif (quote == '"' and title.count(quote) % 2) or (quote == "'" and title.startswith(quote)):
            # (one at an end that pairs with none: the other went with a cut)
            title = (title[1:] if title.startswith(quote) else title[:-1] if title.endswith(quote)
                     else title).strip(" .")
    return title


def _mode_id(mode: Any) -> str | None:
    """A run's mode as stored: None for Build, the default (a conversation's own instructions come as Build's)."""
    return mode.id if mode is not None and mode.id != "build" else None


def _is_copy(directory: str) -> bool:
    try:
        Path(directory).relative_to(forks.folders())
    except ValueError:
        return False
    return True


def _directory(conversation: dict[str, Any]) -> Path:
    """Where the agent works. A chat's scratch folder is made when needed; a code
    task's project folder must be there: an empty new one would only mislead."""
    path = Path(conversation["directory"])
    if conversation["mode"] == "code" and not path.is_dir():
        raise RunError(f"The task's folder {path} is gone (moved or deleted?)")
    path.mkdir(parents=True, exist_ok=True)
    if conversation["mode"] == "chat":
        _own_repository(path)
    return path


def _own_repository(path: Path) -> None:
    """A chat's folder inside someone else's repository (by default Relay's own data
    folder, in Relay's git checkout) gets an empty repository of its own, so git in
    the chat never reaches that one: no `git commit -a` of Relay's code from a chat,
    and agents don't take its history for the chat's."""
    try:
        root = git.repo_root(path)
        if root is not None and root.resolve() != path.resolve():
            git.run(path, "init", "-q")
    except git.GitError as exc:
        print(f"relay: couldn't give {path} a repository of its own: {exc}", file=sys.stderr, flush=True)


def _overlaps(paths: list[Path], row: dict[str, Any], repository: bool = False) -> bool:
    candidates = [Path(row["directory"]).resolve()]
    if row.get("source_directory"):
        candidates.append(Path(row["source_directory"]).resolve())
    if repository and row.get("repo_root"):
        candidates.append(Path(row["repo_root"]).resolve())
    return _paths_overlap(paths, candidates)


def _paths_overlap(first: list[Path], second: list[Path]) -> bool:
    return any(a == b or a in b.parents or b in a.parents for a in first for b in second)
