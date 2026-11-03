"""Relay AgentOS: a Governor that steers other agents while the user is away.

The Governor is an agent of its own (a code conversation, `agentos_role = "governor"`, in
the hidden `governor` mode) whose folder is its notebook: its WorkBox is the GovernorBox,
with no project in it, so it can't do the work itself. It works through capabilities,
Relay tools only its runs get (delegation.py dispatches them here):

    task.list, task.done                   the AgentOS tasks (schedules with `governed`)
    task.create, task.update, task.delete  tasks it makes ("managed"); the user's own it leaves alone
    chat.list, chat.create, chat.send,     chats and code tasks it makes: "managed", which it
    chat.read, chat.delete                 may see, change and delete
    chat.wait                              until managed chats are done (a short wait: it's woken when they are)
    chat.tell                              a working agent interrupted and told what to do instead (steering)
    chat.fork                              a user's own chat: with their permission, a managed fork
    agent.spawn                            chat.create with a task, in short
    agent.review                           a fresh agent (any model) in a managed code task's folder and
                                           WorkBox, read-only by default: to review it, run its tests
    notify.intervention                    "Intervention needed: …" for the user
    notify.update, notify.resolve          one that changed, or is settled (the user sees by whom, and why)
    notify.report                          what's done or found, for the user to know (not to act on)
    service.list, service.status,          the live services (relay.production): each in a line; one's box,
    service.logs                           processes, last exit and health in detail; its log
    service.restart, service.delete        its live version restarted (its box made anew when it's gone); the
                                           service gone, with its versions and data (irreversible)
    service.stop, service.start            not served (its versions and data kept); served again
    workbox.clone, workbox.delete          a service's live code and data snapshot as a managed code task
    workbox.look                           a command in a managed code task's folder, mounted read-only
    production.promote, production.rollback  a WorkBox as the new live version; back to an archived one
    provider.repair                        another agent repairs Relay's support layer for a provider that
                                           keeps failing, on its page (relay.creations): its plan, trying it and
                                           applying it are the user's (a Needs-you item with its page)
    provider.list, provider.add,           the providers and the kinds it may add: only those whose program runs
    provider.remove, provider.check        in a box of Relay's, or a chat API, never with a secret of the user's;
                                           removing only what it added; Relay's whole check of one
    provider.create                        a program Relay doesn't know: the user is sent to add it with an agent
                                           (relay.creations: installing it, logging in, the plan and the trial are
                                           theirs), a Needs-you item with its page
    models.list, models.score              the models available here and their scores Relay keeps; what it found
                                           of new ones, for AgentOS's model ranking (relay.ranking)
    memory.review, memory.change,          all of Relay's memory, with what Relay found (alike, place gone,
    memory.forget                          unused); a memory merged into another or forgotten (not the user's own)
    script.list, script.read,              the user's public scripts (relay.scripts: public/; private/ is out of
    script.run, script.output              its sight): one run as the user, its output; a task can run one too
                                           (task.create script=…): Relay runs it at its time, with no agent

Relay works out what's due itself (relay.scheduler): the Governor is woken only when an
AgentOS task is due, or for its regular round (every `round_hours`), with a briefing; and, so
it needn't wait for them, when agents it started are done, or one seems to go wrong (steps
failing in a row, a turn running long: wake_for_agents), to look and steer.
What it follows and its news are kept in the database (KEPT), so a restart of Relay loses
neither: an agent's turn the restart cut off goes on (resume_agents), and a wake-up of its
that was cut off is done again.
Its models come from a fallback loop the user approves; each wake-up has a budget
(agents started, cost).

What holds it to that (the Governor is a model: what it says is a claim, not a fact):
- Its token reaches only these tools (Delegation.allowed); its commands run in the
  GovernorBox, which has its notebook and no project, token or other conversation; its file
  tools read only its own things (read_boundary, held by the providers' judges).
- It sees, changes and deletes only what's managed (made by AgentOS); a user's chat takes
  the user's permission and becomes a managed fork; a user's task it may carry out, not change.
- A governed task's turn is a state Relay keeps (dispatch_state): Relay dispatches it with a
  wake-up and sees that wake-up end; task.done is the Governor's report, accepted only for a
  task dispatched to it, and kept apart (outcome_by governor) from what Relay saw itself.
  A wake-up that breaks off makes the task due again (a few times; then an intervention).
- Services change only through relay.production: a clone of the live version, promoted after
  Relay's own health check (that, not the Governor's word, is the validation recorded).
- Before an irreversible capability (IRREVERSIBLE), policy() decides: the user's "ask first"
  list asks them (as chat.fork does), and `policies` is where deterministic checks plug in.
- Everything it calls, and everything Relay does for it, is in agentos_events (event()).
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
import urllib.parse
import uuid
from collections.abc import Callable, Collection
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from relay import config, fallback, schedules, scripts
from relay.handover import answer_text

if TYPE_CHECKING:
    from relay.app import Relay
    from relay.runs import ActiveRun

KEY = "agentos"  # preferences
KEPT = "agentos_following"  # meta: what it follows and its news, over a restart of Relay
# included: AgentOS is part of this user's Relay (Settings → AgentOS). Out, it's gone from the UI (its page, services,
# /promote, its conversations) and the Governor isn't woken; what it made stays, and its services keep running.
DEFAULTS = {"included": True, "enabled": False, "round_hours": 16, "loop": None, "effort": "high", "max_agents": 4,
            "max_cost": 2.0, "last_wake": None, "ask_first": [], "own_model": True,
            "notes_moved": None}  # (when it was last asked to move notes.md into its own memory)
NOTES_TO_MOVE = 4096  # bytes of notes.md from which it's asked, once, to move what lasts into its memory
CAPABILITIES = ("task.list", "task.done", "task.create", "task.update", "task.delete", "chat.list", "chat.create",
                "chat.send", "chat.read", "chat.fork", "chat.wait", "chat.tell", "chat.delete", "agent.spawn",
                "agent.review", "notify.intervention", "notify.update", "notify.resolve", "notify.report",
                "service.list", "service.status", "service.logs", "service.restart", "service.delete", "service.stop",
                "service.start", "service.restore", "service.create", "workbox.clone", "workbox.delete", "workbox.look",
                "production.promote", "production.rollback", "provider.repair", "provider.list", "provider.add",
                "provider.remove", "provider.check", "provider.create", "models.list", "models.score", "memory.review",
                "memory.change", "memory.forget", "script.list", "script.read", "script.run", "script.output")
SEND_WAIT = 30  # seconds chat.send waits for an answer: a quick one comes back at once; it's woken for the rest
# What can't be undone by the Governor itself: policy() is asked first.
# The interventions the Governor may see: none about a service that's the user's own.
SEEN = "(service_id IS NULL OR service_id IN (SELECT id FROM services WHERE managed = 1))"
IRREVERSIBLE = ("production.promote", "production.rollback", "chat.delete", "workbox.delete", "task.delete",
                "service.delete", "service.restore", "service.create", "memory.forget")
# What starts an agent's turn, so uses the wake-up's budget (_spend); its answer says what's left.
SPENDING = ("chat.create", "agent.spawn", "agent.review", "chat.send", "workbox.clone", "provider.repair",
            "provider.check", "script.run")
ASK_TIMEOUT = 600  # seconds the user has to answer an "ask first" (else: not allowed)
RETRY_AFTER = 600  # seconds before a task whose wake-up broke off is due again
MAX_ATTEMPTS = 3  # wake-ups that broke off before a task's turn is given up (and the user told)
EVENTS_KEPT = 90 * 86400  # seconds the audit trail is kept
DONE_QUIET = 20  # seconds without another of its agents done before the Governor is woken for them (one batch)
STEPS_SHOWN = 8  # an agent's last steps in its progress
READ_PART = 8000  # characters of a message chat.read gives at a time (the rest with offset)
# What an agent whose turn a restart of Relay cut off is told when Relay goes on with it (resume_agents)
RESUMED = "Relay restarted while you were working, which cut your last turn off. Go on where you left off."


class AgentOSError(Exception):
    pass


def tool_name(capability: str) -> str:
    """As an MCP tool: agentos_chat_create."""
    return "agentos_" + capability.replace(".", "_")


def capability_of(name: str) -> str | None:
    """The capability an MCP tool name or a `relay agentos` word stands for."""
    plain = name.removeprefix("agentos_").replace("_", ".")
    return plain if plain in CAPABILITIES else None


class AgentOS:
    def __init__(self, relay: Relay) -> None:
        self.relay = relay
        self.budgets: dict[str, dict[str, Any]] = {}  # the Governor's run -> {started, agents}
        # Its agents' turns (what it started in managed chats): run -> {user, conversation, title, started}. One
        # that's done, or seems to go wrong, is news (per user) that wakes it, unless it saw the answer already.
        self.following: dict[str, dict[str, Any]] = {}
        self.news: dict[int, list[dict[str, Any]]] = {}
        self.signalled: set[tuple[str, str]] = set()  # (run, signal): told once
        self.nudged: set[str] = set()  # tasks it was woken once to report on (none of its agents working on them)
        # user -> when its next wake-up for news is due at agentos.wakes_per_hour (_news_wait: a few come sooner)
        self.news_due: dict[int, float] = {}
        self.held: set[int] = set()  # users whose Governor's news waits for that
        self.reports: dict[str, str] = {}  # its run -> its last report (notify.report): a task's note when it gave none
        self.unnoted: dict[str, list[str]] = {}  # its run -> tasks it reported without a note
        self.keeping = threading.Lock()  # (following and news are kept from run threads and the event loop)
        # Deterministic checks before an irreversible capability: f(user, capability, arguments) ->
        # why not, or None. Relay has none of its own yet; this is where they go (see policy()).
        self.policies: list[Callable[[int, str, dict[str, Any]], str | None]] = []

    # Settings

    def settings(self, user_id: int) -> dict[str, Any]:
        row = self.relay.db.one("SELECT value FROM preferences WHERE user_id = ? AND key = ?", (user_id, KEY))
        found = json.loads(row["value"]) if row else {}
        return {**DEFAULTS, **{k: v for k, v in found.items() if k in DEFAULTS}}

    def save_settings(self, user_id: int, data: dict[str, Any]) -> dict[str, Any]:
        current = self.settings(user_id)
        for key, value in data.items():
            if key not in DEFAULTS or key in ("last_wake", "notes_moved"):
                continue
            if key in ("included", "enabled", "own_model"):
                current[key] = bool(value)
            elif key in ("round_hours", "max_agents"):
                if not isinstance(value, (int, float)) or not 1 <= value <= 1000:
                    raise AgentOSError(f"{key} must be a number from 1 to 1000")
                current[key] = int(value)
            elif key == "max_cost":
                if not isinstance(value, (int, float)) or not 0 <= value <= 10000:
                    raise AgentOSError("max_cost must be a number from 0 to 10000")
                current[key] = float(value)
            elif key == "loop":
                if value is not None and fallback.find(fallback.load(self.relay.db, user_id), str(value)) is None:
                    raise AgentOSError("No such fallback loop")
                current[key] = value
            elif key == "effort":
                current[key] = str(value or "") or None
            elif key == "ask_first":
                if not isinstance(value, list) or any(v not in IRREVERSIBLE for v in value):
                    raise AgentOSError(f"ask_first lists capabilities of: {', '.join(IRREVERSIBLE)}")
                current[key] = sorted(set(value))
        self._store(user_id, current)
        if self.on(user_id, current):
            self.relay.schedules.ensure_system(user_id)  # Relay's own tasks, with AgentOS on
        return current

    def on(self, user_id: int, settings: dict[str, Any] | None = None) -> bool:
        """Whether the Governor works for the user: AgentOS is part of their Relay (included) and it's on."""
        settings = settings or self.settings(user_id)
        return bool(settings["included"] and settings["enabled"])

    def _store(self, user_id: int, values: dict[str, Any]) -> None:
        self.relay.db.execute("INSERT INTO preferences (user_id, key, value) VALUES (?, ?, ?) "
                              "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
                              (user_id, KEY, json.dumps(values)))

    def woken(self, user_id: int, now: float) -> None:
        """When it was last woken, on the settings as they are now: what the user saved while a wake-up
        started (AgentOS off, ask first) isn't put back."""
        self._store(user_id, {**self.settings(user_id), "last_wake": now})

    # The Governor

    def governor(self, user_id: int) -> dict[str, Any] | None:
        return self.relay.db.one("SELECT * FROM conversations WHERE user_id = ? AND agentos_role = 'governor'",
                                 (user_id,))

    async def _ensure_governor(self, user_id: int, settings: dict[str, Any]) -> dict[str, Any]:
        """The Governor's conversation, on the first usable agent of its approved loop."""
        from relay.routes.conversations import open_conversation  # it imports much of Relay
        loop = fallback.find(fallback.load(self.relay.db, user_id), settings["loop"] or "")
        if loop is None:
            raise AgentOSError("Choose the Governor's models first (AgentOS → Governor: an approved fallback loop)")
        entry = next((e for e in loop["entries"] if self._confined(e["provider"])
                      and fallback.usable(self.relay.db, self.relay.registry, e, needs_tools=True)), None)
        if entry is None:
            raise AgentOSError(f"None of the Governor's models (loop {loop['name']!r}) can work now")
        row = self.governor(user_id)
        if row is None:
            folder = notebook(user_id)
            folder.mkdir(parents=True, exist_ok=True)
            row = await open_conversation(self.relay, user_id, {
                "provider_id": entry["provider"], "model": entry.get("model") or None, "mode": "code",
                "directory": str(folder), "isolate": False, "agent_mode": "governor",
                "title": "AgentOS Governor"}, anywhere=True)
        if row["provider_id"] != entry["provider"]:
            # Another provider can't go on in this one's session (as when the user switches: relay.routes):
            # it starts one of its own, with the conversation handed over.
            self.relay.db.execute("UPDATE conversations SET provider_session = NULL, fork_session = NULL, "
                                  "fork_before = NULL, resume_at = NULL, limit_info = NULL WHERE id = ?", (row["id"],))
        self.relay.db.execute(
            "UPDATE conversations SET agentos_role = 'governor', agent_mode = 'governor', provider_id = ?, model = ?, "
            "effort = ?, fallback_loop = ? WHERE id = ?",
            (entry["provider"], entry.get("model") or None, settings.get("effort"), loop["id"], row["id"]))
        return self.relay.db.one("SELECT * FROM conversations WHERE id = ?", (row["id"],))

    async def wake(self, user_id: int, due: list[dict[str, Any]], now: float, reason: str,
                   move_on: bool = True) -> str | None:
        """Wake the Governor with a briefing; its run. None when it's still working (the tasks
        stay due); AgentOSError when it can't (no approved models)."""
        settings = self.settings(user_id)
        if not settings["included"]:
            raise AgentOSError("AgentOS is turned off (Settings → AgentOS)")
        governor = await self._ensure_governor(user_id, settings)
        if self.relay.runs.active_run_for(governor["id"]):
            return None
        with self.relay.runs.lock:  # budgets of wake-ups that are over
            going = set(self.relay.runs.active)
        self.budgets = {k: v for k, v in self.budgets.items() if k in going}
        # Each due task's turn is taken by this wake-up (dispatching) before it starts, so no other
        # takes it too; if it can't start, the tasks are as they were (still due).
        claimed = self._claim(due, now)
        if due and not claimed:
            return None  # their turns are taken already
        news = self.news.pop(user_id, [])  # (its agents' news go in this briefing)
        try:
            run_id = await asyncio.to_thread(self.relay.runs.start, governor,
                                             self.briefing(user_id, claimed, now, reason, settings, news))
        except Exception as exc:
            self._unclaim(claimed)
            self.news.setdefault(user_id, [])[:0] = news
            self.event(user_id, "wake", ok=False, reason=reason, why=str(exc)[:500])
            raise AgentOSError(f"The Governor couldn't start: {exc}") from exc
        finally:
            self._keep()
        self.budgets[run_id] = {"started": now, "agents": 0, "user": user_id, "reason": reason,
                                "max_agents": settings["max_agents"], "max_cost": settings["max_cost"]}
        with self.relay.db.transaction() as conn:
            for task in claimed:
                upcoming, enabled = task["next_run"], int(task["enabled"])
                if move_on:  # (run now, by hand: when it's next stays as it was)
                    upcoming = schedules.next_run(task["when"], task["timezone"], now, task["next_run"])
                    enabled = 1 if upcoming else 0
                conn.execute("UPDATE schedules SET dispatch_state = 'dispatched', dispatch_run = ?, last_run = ?, "
                             "last_conversation = ?, last_error = NULL, next_run = ?, enabled = ? WHERE id = ?",
                             (run_id, now, governor["id"], upcoming, enabled, task["id"]))
        self.event(user_id, "wake", run=run_id, conversation=governor["id"], reason=reason,
                   tasks=[t["id"] for t in claimed])
        for task in claimed:
            self.event(user_id, "task.dispatched", run=run_id, task=task["id"], name=task["name"],
                       attempt=task.get("attempts", 0) + 1)
        self.woken(user_id, now)
        return run_id

    def _claim(self, due: list[dict[str, Any]], now: float) -> list[dict[str, Any]]:
        """The due tasks whose turn nobody has taken yet, taken (dispatching)."""
        claimed = []
        with self.relay.db.transaction() as conn:
            for task in due:
                taken = conn.execute("UPDATE schedules SET dispatch_state = 'dispatching', dispatched_at = ? "
                                     "WHERE id = ? AND governed = 1 AND (dispatch_state IS NULL "
                                     "OR dispatch_state = 'awaiting')", (now, task["id"]))
                if taken.rowcount:
                    claimed.append(task)
        return claimed

    def _unclaim(self, tasks: list[dict[str, Any]]) -> None:
        for task in tasks:
            self.relay.db.execute("UPDATE schedules SET dispatch_state = ?, dispatched_at = NULL "
                                  "WHERE id = ? AND dispatch_state = 'dispatching'",
                                  (task.get("dispatch_state"), task["id"]))

    def run_ended(self, run_id: str, status: str, conversation_id: str | None = None, why: str | None = None) -> None:
        """A wake-up is over: its tasks' turns await the Governor's report (it may still come, on a
        later wake-up, when the agents it started are done); one that broke off is due again soon,
        or, after MAX_ATTEMPTS, given up with an intervention. A fallback agent that took over
        the Governor's conversation takes over its tasks and budget too. One that Relay's stop cut
        off is done again after the restart. `why`: who stopped it (relay.runs: stop), for the news."""
        followed = self.following.pop(run_id, None)
        if followed is not None:  # (one of its agents: news, unless it sees the answer while awake: _saw)
            going = self.relay.runs.active_run_for(conversation_id) if conversation_id else None
            if going and going != run_id:  # (a fallback agent took over, a queued message: it isn't done)
                self.following[going] = {**followed, "started": time.time()}
            else:
                # (cut off by Relay's stop: after the restart Relay goes on with it, resume_agents)
                cut_off = {"cut_off": True} if status != "succeeded" and self.relay.runs.shutting_down else {}
                self.news.setdefault(followed["user"], []).append({**followed, "kind": "done", "run": run_id,
                                                                   "status": status, "at": time.time(), **cut_off,
                                                                   **({"why": why} if why else {})})
            self._keep()
        self.signalled = {(r, kind) for r, kind in self.signalled if r != run_id}
        budget = self.budgets.pop(run_id, None)
        tasks = self.relay.db.all("SELECT * FROM schedules WHERE dispatch_run = ? AND dispatch_state = 'dispatched'",
                                  (run_id,))
        following = self.relay.runs.active_run_for(conversation_id) if conversation_id else None
        if status != "succeeded" and following and following != run_id:
            if budget is not None:
                self.budgets[following] = budget
            for task in tasks:
                self.relay.db.execute("UPDATE schedules SET dispatch_run = ? WHERE id = ?", (following, task["id"]))
            return
        now = time.time()
        self.reports.pop(run_id, None)
        self.unnoted.pop(run_id, None)
        if budget is not None and status != "succeeded" and self.relay.runs.shutting_down:
            self._restarted(budget["user"], conversation_id, run_id, budget.get("reason") or "", now)
        for task in tasks:
            if status == "succeeded":
                self.relay.db.execute("UPDATE schedules SET dispatch_state = 'awaiting', outcome = 'unreported', "
                                      "outcome_by = 'relay', outcome_note = NULL, outcome_at = ? WHERE id = ?",
                                      (now, task["id"]))
                self.event(task["user_id"], "task.unreported", run=run_id, task=task["id"], name=task["name"])
            else:
                self._broke_off(task, now, now + RETRY_AFTER, f"the Governor's wake-up {status}")
        # A wake-up of its that ended with tasks awaiting its report (this one's or an earlier's) while none of
        # its agents works: nothing would wake it before its round. Once a task, so it reports (one it leaves
        # again waits for its round).
        user = budget["user"] if budget is not None else None
        if status == "succeeded" and user is not None and not any(f["user"] == user for f in self.following.values()):
            # (a task reported meanwhile and awaiting again, a recurring one's next turn, is nudged anew)
            awaiting = self.relay.db.all("SELECT id, name, user_id FROM schedules WHERE dispatch_state = 'awaiting'")
            self.nudged &= {t["id"] for t in awaiting}
            waiting = [t for t in awaiting if t["user_id"] == user and t["id"] not in self.nudged]
            if waiting:
                self.nudged.update(t["id"] for t in waiting)
                self.news.setdefault(user, []).append(
                    {"kind": "awaiting", "conversation": conversation_id,
                     "title": ", ".join(t["name"] for t in waiting),
                     "run": run_id, "at": now})
                self._keep()

    def _broke_off(self, task: dict[str, Any], now: float, again: float, why: str) -> None:
        attempts = (task.get("attempts") or 0) + 1
        if attempts >= MAX_ATTEMPTS:
            self.relay.db.execute("UPDATE schedules SET dispatch_state = NULL, attempts = 0, outcome = 'interrupted', "
                                  "outcome_by = 'relay', outcome_note = ?, outcome_at = ?, last_error = ? WHERE id = ?",
                                  (why, now, f"Given up: {why}, {attempts} times", task["id"]))
            self.event(task["user_id"], "task.given_up", ok=False, task=task["id"], name=task["name"], why=why)
            self.intervene(task["user_id"], f"AgentOS gave up on task {task['name']!r} this time: {why}, "
                           f"{attempts} times.", task_id=task["id"])
            return
        upcoming = min(task["next_run"], again) if task["next_run"] else again
        self.relay.db.execute("UPDATE schedules SET dispatch_state = NULL, attempts = ?, outcome = 'interrupted', "
                              "outcome_by = 'relay', outcome_note = ?, outcome_at = ?, next_run = ?, enabled = 1 "
                              "WHERE id = ?", (attempts, why, now, upcoming, task["id"]))
        self.event(task["user_id"], "task.interrupted", ok=False, task=task["id"], name=task["name"], why=why,
                   again=upcoming)

    def reconcile(self, now: float, cut_off: Collection[str] = ()) -> None:
        """At Relay's start (no run is going, those a crash left are failed): turns a crash left half-way.
        One being dispatched never started (the task is still due); one dispatched broke off with its
        wake-up: due again now. What it followed and its news are as they were kept; an agent's turn
        or a wake-up of its that a crash cut off (the runs `cut_off`, failed now) is news like one
        Relay's stop cut off."""
        for task in self.relay.db.all("SELECT * FROM schedules WHERE dispatch_state IN ('dispatching', 'dispatched')"):
            if task["dispatch_state"] == "dispatching":
                self.relay.db.execute("UPDATE schedules SET dispatch_state = NULL, dispatched_at = NULL WHERE id = ?",
                                      (task["id"],))
                self.event(task["user_id"], "reconciled", task=task["id"], what="its wake-up never started")
            else:
                self._broke_off(task, now, now, "Relay stopped during its wake-up")
        row = self.relay.db.one("SELECT value FROM meta WHERE key = ?", (KEPT,))
        kept = json.loads(row["value"]) if row else {}
        self.news = {int(user): items for user, items in (kept.get("news") or {}).items()}
        self.following = {}
        for run_id, followed in (kept.get("following") or {}).items():  # (going when Relay stopped: a crash)
            ended = self.relay.db.one("SELECT status FROM runs WHERE id = ?", (run_id,))
            self.news.setdefault(followed["user"], []).append(
                {**followed, "kind": "done", "run": run_id, "status": ended["status"] if ended else "failed",
                 "at": now, **({"cut_off": True} if run_id in cut_off else {})})
        for governor in self.relay.db.all("SELECT r.id, c.id AS conversation, c.user_id FROM runs r "
                                          "JOIN conversations c ON c.id = r.conversation_id "
                                          "WHERE c.agentos_role = 'governor' AND r.id IN "
                                          f"({', '.join('?' * len(cut_off))})", tuple(cut_off)) if cut_off else []:
            woke = self.relay.db.one("SELECT detail FROM agentos_events WHERE kind = 'wake' AND run_id = ?",
                                     (governor["id"],))
            reason = json.loads(woke["detail"]).get("reason", "") if woke and woke["detail"] else ""
            self._restarted(governor["user_id"], governor["conversation"], governor["id"], reason, now)
        self._keep()

    def resume_agents(self, now: float) -> list[str]:
        """After a restart of Relay (it serves requests by now): its agents whose turn the restart cut off
        go on, followed again. Not one that's discarded, waiting for a usage limit's reset, or that went on
        meanwhile; nor while AgentOS is off. The Governor hears of it with its next wake-up."""
        went_on = []
        for user, items in list(self.news.items()):
            if not self.on(user):
                continue
            for item in [n for n in items if n.get("cut_off")]:
                row = self.relay.db.one("SELECT * FROM conversations WHERE id = ? AND managed = 1",
                                        (item["conversation"],))
                last = row and self.relay.db.one("SELECT id FROM runs WHERE conversation_id = ? ORDER BY created DESC "
                                                 "LIMIT 1", (row["id"],))
                if (row is None or row["task_state"] == "discarded" or row.get("resume_at") or last is None
                        or last["id"] != item["run"] or self.relay.runs.active_run_for(row["id"])):
                    continue  # (it stays news: the Governor sees to it)
                try:
                    run_id = self.relay.runs.start(row, RESUMED, relay_action={"kind": "resume"})
                except Exception as exc:  # noqa: BLE001 - e.g. its provider was removed meanwhile: news, as it is
                    print(f"relay: AgentOS couldn't go on with {row['id'][:8]}: {exc}", file=sys.stderr, flush=True)
                    continue
                items.remove(item)
                items.append({"kind": "resumed", "user": user, "conversation": row["id"], "title": row["title"],
                              "run": run_id, "at": now})
                self._follow(run_id, row)
                self.event(user, "agent.resumed", actor="relay", run=run_id, conversation=row["id"], title=row["title"])
                went_on.append(row["id"])
        self._keep()
        return went_on

    def goes_on(self, conversation_id: str) -> bool:
        """Whether a run in this conversation goes on by itself after a restart of Relay: one of AgentOS's (its
        agents' turns, resume_agents; its own wake-ups, done again), or a subagent of one, while AgentOS is on.
        Those needn't hold a restart up."""
        row = self.relay.db.one("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
        while row is not None and row.get("parent_conversation"):
            row = self.relay.db.one("SELECT * FROM conversations WHERE id = ?", (row["parent_conversation"],))
        return bool(row and (row["managed"] or row["agentos_role"] == "governor") and self.on(row["user_id"]))

    def _restarted(self, user: int, conversation_id: str | None, run_id: str, reason: str, now: float) -> None:
        """A wake-up of its that Relay's stop (or a crash) cut off: news that wakes it again after the restart."""
        self.news.setdefault(user, []).append({"kind": "restarted", "conversation": conversation_id, "title": reason,
                                               "run": run_id, "at": now})
        self._keep()

    def _keep(self) -> None:
        """What it follows and its news, kept (KEPT) for after a restart of Relay."""
        with self.keeping:
            kept = json.dumps({"following": dict(self.following),
                               "news": {str(user): list(items) for user, items in list(self.news.items()) if items}})
            self.relay.db.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET "
                                  "value = excluded.value", (KEPT, kept))

    def due_for_round(self, user_id: int, now: float) -> bool:
        settings = self.settings(user_id)
        last = settings.get("last_wake")
        return self.on(user_id, settings) and (last is None or now - last >= settings["round_hours"] * 3600)

    def enabled_users(self) -> list[int]:
        rows = self.relay.db.all("SELECT user_id, value FROM preferences WHERE key = ?", (KEY,))
        return [r["user_id"] for r in rows if json.loads(r["value"]).get("enabled")
                and json.loads(r["value"]).get("included", True)]

    def briefing(self, user_id: int, due: list[dict[str, Any]], now: float, reason: str,
                 settings: dict[str, Any], news: list[dict[str, Any]] | None = None) -> str:
        lines = [f"AgentOS wake-up, {_now(now)}: {reason}."]
        if due:
            lines.append("Due tasks:")
            for task in due:
                last = (time.strftime("%d %b %H:%M", time.localtime(task["last_run"])) if task.get("last_run")
                        else "never")
                where = f"; folder {task['directory']}" if task.get("directory") else ""
                lines.append(f"- [{task['id'][:8]}] {task['name']} ({schedules.describe(task['when'])}; last run "
                             f"{last}{where}): {task['prompt']}")
        elif news if news is not None else self.news.get(user_id):
            lines.append("No task is due: see to your agents below (and what else needs you), or nothing.")
        else:
            lines.append("No task is due: this is your regular round. Look over the managed chats and do what's "
                         "needed, or nothing.")
        awaiting = self.relay.db.all("SELECT id, name FROM schedules WHERE user_id = ? AND dispatch_state = 'awaiting'",
                                     (user_id,))
        if awaiting:
            lines.append("Tasks of earlier wake-ups still awaiting your report (task.done when you know how they "
                         "went): " + "; ".join(f"[{t['id'][:8]}] {t['name']}" for t in awaiting))
        managed = self.relay.db.one("SELECT count(*) AS n FROM conversations WHERE user_id = ? AND managed = 1 "
                                    "AND agentos_role IS NULL", (user_id,))["n"]
        open_ = self.relay.db.all(f"SELECT id, text, created FROM agentos_interventions WHERE user_id = ? AND {SEEN} "
                                  "AND resolved IS NULL ORDER BY created", (user_id,))
        lines.append(f"Managed chats: {managed} (chat.list).")
        services = self.relay.production.services(user_id, managed=True)
        if services:
            stopped = [s["name"] for s in services if s.get("stopped")]
            failing = [s["name"] for s in services if s["health_ok"] is False and not s.get("stopped")]
            lines.append(f"Services: {len(services)} (service.list)"
                         + (f"; failing now: {', '.join(failing)}" if failing else ", all well")
                         + (f"; stopped (by hand): {', '.join(stopped)}" if stopped else "") + ".")
        roots = ", ".join(str(r) for r in self.relay.settings.roots())
        lines.append(f"Project folders are in: {roots} (chat.create folder=… makes a new one there).")
        if open_:
            lines.append("Open interventions (the user hasn't settled them; notify.update id=… when one changed, "
                         "notify.resolve id=… note=… when it's fixed or no longer applies):")
            lines += [f"- [{i['id'][:8]}] {_stamp(i['created'])}: {i['text'][:300]}" for i in open_]
        lines += self._news_lines(self.news.get(user_id) or [] if news is None else news)
        notes = self._notes_to_move(user_id, settings, now)
        if notes:
            lines.append(f"First: you have a memory of your own now (Relay's memory, your part of it, which comes with "
                         f"every briefing). Your notes.md is {notes} KB: move what lasts in it (the user's decisions, "
                         "how their services and tasks are, what didn't work and why) into your memory, one short fact "
                         "each with its category (memory_add with project true: only you can write it, so an agent "
                         "may sort them, you save them), then trim notes.md to what's still a working record.")
        own = self._own(user_id, settings)
        if own:
            lines.append(f"Your own model ({own[0]} · {own[1] or 'its default'}) is always yours for agents: give "
                         "research, reviews and other judgment work to an agent on it (chat.create without a "
                         "provider) rather than doing it yourself.")
        lines.append("Start agents, then end your wake-up: you're woken when they're done, or when one seems to go "
                     "wrong (chat.wait only for a short wait).")
        lines.append(f"Budget, per wake-up (each has its own, also when a task spans several): at most "
                     f"{settings['max_agents']} agents started (chat.create, chat.send, agent.review and the like), "
                     f"about ${settings['max_cost']:.2f}. AgentOS spent about ${self.spent(user_id, now - 86400):.2f} "
                     "in the last 24 hours (your wake-ups and your agents).")
        return "\n".join(lines)

    def _notes_to_move(self, user_id: int, settings: dict[str, Any], now: float) -> int | None:
        """notes.md's size in KB when the Governor is to be asked to move it into its memory: while its
        memory is empty and notes.md is long enough to be worth it, at most once a day (a wake-up that
        failed at once doesn't lose it; one that did it has a memory)."""
        from relay import memory
        notes = notebook(user_id) / "notes.md"
        if now - (settings.get("notes_moved") or 0) < 86400 or not memory.enabled(self.relay.db, user_id) \
                or not memory.agents_add(self.relay.db, user_id):
            return None
        try:
            size = notes.stat().st_size
        except OSError:
            return None
        if size < NOTES_TO_MOVE or self.relay.db.one("SELECT 1 FROM memories WHERE user_id = ? AND project = ?",
                                                    (user_id, memory.GOVERNOR)):
            return None
        self._store(user_id, {**self.settings(user_id), "notes_moved": now})
        return max(1, round(size / 1024))

    def _news_lines(self, news: list[dict[str, Any]]) -> list[str]:
        """Its agents done since it was last woken, and those that seem to go wrong; what a restart of
        Relay cut off."""
        lines = []
        for item in [n for n in news if n["kind"] == "restarted"]:
            started = self.relay.db.one("SELECT created FROM runs WHERE id = ?", (item["run"],))
            when = f" of {_stamp(started['created'])}" if started else ""
            why = f" ({item['title']})" if item["title"] else ""
            lines.append(f"Relay restarted during your wake-up{when}{why}, which cut it off: see what you had started "
                         "then (chat.list), and go on where you left off.")
        resumed = [n for n in news if n["kind"] == "resumed"]
        if resumed:
            lines.append("Relay went on with these agents of yours, whose turn its restart cut off (you're woken when "
                         "they're done): " + "; ".join(f"[{n['conversation'][:8]}] {n['title']}" for n in resumed))
        done = [n for n in news if n["kind"] == "done"]
        if done:
            lines.append("Your agents that are done (chat.read id=… for all of it):")
            for item in done:
                if self.relay.db.one("SELECT 1 FROM conversations WHERE id = ?", (item["conversation"],)) is None:
                    continue
                answer = " ".join(self._last_answer(item["conversation"]).split())
                lines.append(f"- {answer[:400]}{'…' if len(answer) > 400 else ''}"
                             + (" (A restart of Relay cut this turn off: chat.send to have it go on.)"
                                if item.get("cut_off") else ""))
                if item.get("status") not in (None, "succeeded"):
                    ended = self._how_it_ended(item)
                    if ended:
                        lines.append(ended)
        signals = [n for n in news if n["kind"] == "signal"]
        if signals:
            lines.append("Your agents that may be going wrong (look: chat.read, workbox.look; steer: chat.tell id=… "
                         "message=…, which interrupts it):")
            for item in signals:
                progress = self._progress(item["conversation"])
                lines.append(f"- [{item['conversation'][:8]}] {item['title']}: {item['signal']}."
                             + (f"\n{progress}" if progress else " (It's done by now.)"))
        return lines

    def _own(self, user_id: int, settings: dict[str, Any] | None = None) -> tuple[str, str | None] | None:
        """The Governor's own provider and model, when its agents may use them (own_model): None otherwise."""
        settings = settings or self.settings(user_id)
        governor = self.governor(user_id) if settings.get("own_model", True) else None
        return (governor["provider_id"], governor["model"]) if governor else None

    def _provider(self, user: int, arguments: dict[str, Any], what: str) -> tuple[str, str | None]:
        """The agent's provider and model: those named, else the Governor's own (own_model). Only a
        confined one: its agent's program in a box, its commands in a WorkBox."""
        if arguments.get("provider"):
            chosen = str(arguments["provider"]), arguments.get("model") or None
        else:
            chosen = self._own(user)
            if chosen is None:
                raise AgentOSError(f"Say which provider {what} (relay agents lists them)")
        self._check_confined(chosen[0])
        return chosen

    def _confined(self, provider_id: str) -> bool:
        from relay.providers import KINDS
        template = next((t for t in self.relay.registry.templates() if t.id == provider_id), None)
        return template is None or KINDS[template.kind].CONFINED  # (one that isn't there: chat.create says so)

    def _check_confined(self, provider_id: str) -> None:
        if not self._confined(provider_id):
            raise AgentOSError(f"{provider_id} runs its program on this machine as it is, outside the boxes AgentOS's "
                               "agents work in: choose another provider")

    # Its agents: following them, and what's news

    def _follow_started(self, conversation: dict[str, Any]) -> None:
        """Follow the turn a managed chat was just started with (a repair's, a new kind's): its Governor is woken
        when it's done, as for chat.create's (it wasn't, a trial found: it heard at its next round, hours on)."""
        going = self.relay.runs.active_run_for(conversation["id"])
        if going:
            self._follow(going, conversation)

    def _follow(self, run_id: str, conversation: dict[str, Any]) -> None:
        self.following[run_id] = {"user": conversation["user_id"], "conversation": conversation["id"],
                                  "title": conversation["title"], "started": time.time()}
        if self.relay.runs.active.get(run_id) is None:  # (over already, maybe before run_ended could see it followed)
            followed = self.following.pop(run_id, None)  # (whichever of the two pops it says so, once)
            if followed is not None:
                row = self.relay.db.one("SELECT status FROM runs WHERE id = ?", (run_id,))
                self.news.setdefault(followed["user"], []).append(
                    {**followed, "kind": "done", "run": run_id, "status": row["status"] if row else "failed",
                     "at": time.time()})
        self._keep()

    def _saw(self, conversation_id: str) -> None:
        """The Governor got a chat's answer while awake: its being done is no news to wake it for."""
        for items in self.news.values():
            items[:] = [n for n in items if not (n["conversation"] == conversation_id and n["kind"] == "done")]
        self._keep()

    def watch(self, now: float) -> None:
        """Its agents at work: one that fails step after step, or works long on one turn, is news (once each)."""
        from relay import tunables
        failing_steps, long = tunables.get("agentos.failing_steps"), tunables.get("agentos.long_minutes") * 60
        for run_id, followed in list(self.following.items()):
            active = self.relay.runs.active.get(run_id)
            if active is None:
                continue
            with active.lock:
                results = [e for e in active.events if e.get("type") == "tool_result"]
                calls = [e for e in active.events if e.get("type") == "tool_call"]
            failed = 0
            for event in reversed(results):
                if event.get("ok", True):
                    break
                failed += 1
            same = 0  # (the same step again and again: a loop, failing or not)
            for event in reversed(calls):
                if (event.get("name"), event.get("input")) != (calls[-1].get("name"), calls[-1].get("input")):
                    break
                same += 1
            found = []
            if failed >= failing_steps:
                found.append(("failing", f"{failed} steps failed in a row"))
            if same >= failing_steps:
                found.append(("repeating", f"the same step {same} times in a row"))
            if now - followed["started"] >= long:
                found.append(("long", f"working for {round((now - followed['started']) / 60)} minutes on one turn"))
            for kind, signal in found:
                if (run_id, kind) not in self.signalled:
                    self.signalled.add((run_id, kind))
                    self.news.setdefault(followed["user"], []).append(
                        {**followed, "kind": "signal", "run": run_id, "signal": signal, "at": now})
                    self.event(followed["user"], "agent.signal", run=run_id, conversation=followed["conversation"],
                               signal=signal, title=followed["title"])
                    self._keep()

    async def wake_for_agents(self, now: float) -> None:
        """A wake-up for its agents' news: at once for a sign of going wrong, else once no other agent
        was done for DONE_QUIET seconds (one batch). Not while it works (it's in the next briefing), and
        not more often than agentos.wakes_per_hour: each wake-up has a budget of its own, so a Governor
        that starts agent after agent is held to that (the news waits, and comes along)."""
        for user, items in list(self.news.items()):
            if not items:
                continue
            if not self.on(user):
                self.news.pop(user, None)
                self._keep()
                continue
            signals = [n for n in items if n["kind"] == "signal"]
            waking = [n for n in items if n["kind"] != "resumed"]  # (agents gone on after a restart: it hears later)
            if not waking or not signals and now - max(n["at"] for n in waking) < DONE_QUIET:
                continue
            wait = self._news_wait(user, now)
            if wait > 0:
                if user not in self.held:  # (said once a time it's held)
                    self.held.add(user)
                    self.event(user, "wake.held", actor="relay", news=len(items), until=round(wait))
                continue
            self.held.discard(user)
            done = sum(1 for n in items if n["kind"] == "done")
            reason = "; ".join(filter(None, [
                "Relay restarted during your last wake-up" if any(n["kind"] == "restarted" for n in items) else "",
                f"{len(signals)} of your agents may be going wrong" if len(signals) > 1
                else "one of your agents may be going wrong" if signals else "",
                f"{done} of your agents are done" if done > 1 else "one of your agents is done" if done else "",
                "tasks await your report" if any(n["kind"] == "awaiting" for n in items) else ""]))
            try:
                if await self.wake(user, [], now, reason) is not None:
                    self._news_woken(user, now)
            except AgentOSError:
                self.news.pop(user, None)  # (it can't be woken: no models; the round says so)
                self._keep()

    def _news_wait(self, user: int, now: float) -> float:
        """Seconds before the Governor may be woken for news again (0: now). agentos.wakes_per_hour is a rate,
        not a window: half of them may come at once, then one every hour / that many. So news never waits for
        an hour to pass (6: at most 10 minutes), and an hour has at most half again as many."""
        from relay import tunables
        per_hour = tunables.get("agentos.wakes_per_hour")
        return max(0.0, self.news_due.get(user, now) - (max(1, per_hour // 2) - 1) * 3600 / per_hour - now)

    def _news_woken(self, user: int, now: float) -> None:
        """Woken for news: the next is due an hour / agentos.wakes_per_hour after it (or after those before it)."""
        from relay import tunables
        self.news_due[user] = max(self.news_due.get(user, now), now) + 3600 / tunables.get("agentos.wakes_per_hour")

    def _progress(self, conversation_id: str) -> str:
        """A working agent's turn so far: how long, its last steps (failed ones marked), its last words."""
        run_id = self.relay.runs.active_run_for(conversation_id)
        active = self.relay.runs.active.get(run_id) if run_id else None
        if active is None:
            return ""
        with active.lock:
            events = list(active.events)
        row = self.relay.db.one("SELECT created FROM runs WHERE id = ?", (run_id,))
        minutes = max(0, round((time.time() - row["created"]) / 60)) if row else None
        steps = _steps(events, "running")
        said = " ".join(answer_text(events).split())[-500:]
        return "\n".join([f"  Working{f' for {minutes} min' if minutes is not None else ''}; its last steps:"
                           if steps else f"  Working{f' for {minutes} min' if minutes is not None else ''}.",
                           *steps, *([f"  It says: …{said}"] if said else [])])

    def _how_it_ended(self, item: dict[str, Any]) -> str:
        """An agent's turn that didn't succeed, for the Governor to decide whether it goes on or starts afresh:
        who stopped it (or what went wrong), after how long, what it cost, its last steps."""
        row = self.relay.db.one("SELECT created, finished, error, usage FROM runs WHERE id = ?", (item["run"],))
        if row is None:
            return ""
        minutes = max(0, round(((row["finished"] or time.time()) - row["created"]) / 60))
        try:
            cost = float(json.loads(row["usage"]).get("cost") or 0) if row["usage"] else 0.0
        except (ValueError, TypeError, AttributeError):
            cost = 0.0
        why = item.get("why") or row["error"] or "nobody said why"
        steps = _steps(self.relay.db.run_events(item["run"]), "cut off")
        return "\n".join([f"  {item['status'].capitalize()} after {minutes} min ({why}); it cost about ${cost:.2f}"
                           + ("; its last steps:" if steps else ", and it took no steps yet."), *steps])

    # Capabilities

    def is_governor(self, conversation_id: str) -> bool:
        return self.relay.db.one("SELECT 1 FROM conversations WHERE id = ? AND agentos_role = 'governor'",
                                 (conversation_id,)) is not None

    async def handle(self, run: ActiveRun, capability: str, arguments: dict[str, Any]) -> str:
        """One capability call of the Governor's: past policy() when it's irreversible; in the audit
        trail either way, with what it asked and how it went."""
        user = self.relay.db.one("SELECT user_id FROM conversations WHERE id = ?", (run.conversation_id,))["user_id"]
        method = getattr(self, "_" + capability.replace(".", "_"))
        asked = {k: (v if not isinstance(v, str) else v[:300]) for k, v in arguments.items()}
        try:
            if capability in IRREVERSIBLE:
                refused = await self.policy(run, user, capability, arguments)
                if refused:
                    raise AgentOSError(refused)
            text = await method(run, user, arguments)
        except Exception as exc:
            self.event(user, "capability", actor="governor", ok=False, run=run.id, capability=capability,
                       arguments=asked, why=str(exc)[:500])
            raise
        self.event(user, "capability", actor="governor", run=run.id, capability=capability, arguments=asked,
                   answer=text[:300])
        budget = self.budgets.get(run.id)
        if capability in SPENDING and budget is not None:  # (what's left, as it goes)
            text += (f"\n(This wake-up: {budget['agents']} of {budget['max_agents']} agents, about "
                     f"${self.spent(user, budget['started']):.2f} of ${budget['max_cost']:.2f}.)")
        return text

    async def policy(self, run: ActiveRun, user: int, capability: str, arguments: dict[str, Any]) -> str | None:
        """Whether an irreversible capability may go ahead: why not, or None. The deterministic checks
        (`policies`) first; then, for what the user wants to be asked about first (settings
        ask_first), the user, the way chat.fork asks (no answer in ASK_TIMEOUT: no)."""
        for check in self.policies:
            refused = check(user, capability, arguments)
            if refused:
                self.event(user, "policy", ok=False, run=run.id, capability=capability, why=refused)
                return refused
        if capability not in self.settings(user)["ask_first"]:
            return None
        what = " ".join(f"{k}={v}" for k, v in arguments.items() if isinstance(v, (str, int, float)))[:300]
        event = {"type": "permission", "id": uuid.uuid4().hex, "action": f"AgentOS: {capability}",
                 "resources": [what or capability], "decision": None, "expires": time.time() + ASK_TIMEOUT,
                 "options": [{"id": "once", "label": "Allow", "outcome": "allowed"},
                             {"id": "denied", "label": "Deny", "outcome": "denied"}]}
        allowed = await self.relay.delegation._choose(run, event) == "once"
        self.event(user, "policy", actor="user", ok=allowed, run=run.id, capability=capability, asked=what)
        return None if allowed else f"The user didn't allow {capability} (they asked to be asked first)."

    # The audit trail

    def event(self, user: int, kind: str, actor: str = "relay", ok: bool = True, run: str | None = None,
              conversation: str | None = None, task: str | None = None, service: str | None = None,
              version: int | None = None, **detail: Any) -> None:
        """One entry of AgentOS's audit trail: what the Governor asked or claims (actor governor), what
        Relay did or checked itself (relay), what the user decided (user)."""
        try:
            self.relay.db.execute(
                "INSERT INTO agentos_events (user_id, at, kind, actor, ok, run_id, conversation_id, task_id, "
                "service_id, version, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user, time.time(), kind, actor, int(bool(ok)), run, conversation, task, service, version,
                 json.dumps(detail, default=str) if detail else None))
        except Exception as exc:  # noqa: BLE001 - the trail never stops the work
            print(f"relay: AgentOS couldn't note {kind}: {exc}", flush=True)

    def events(self, user: int, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.relay.db.all("SELECT * FROM agentos_events WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                                 (user, limit))
        return [{**r, "ok": bool(r["ok"]), "detail": json.loads(r["detail"]) if r["detail"] else {}} for r in rows]

    def tidy_events(self, now: float) -> None:
        self.relay.db.execute("DELETE FROM agentos_events WHERE at < ?", (now - EVENTS_KEPT,))

    def record_refusal(self, run: ActiveRun, tool: Any) -> None:
        """A tool its run wasn't given (Delegation.allowed): noted for AgentOS's own conversations."""
        row = self.relay.db.one("SELECT * FROM conversations WHERE id = ?", (run.conversation_id,))
        if row is not None and confined(self.relay.db, row):
            self.event(row["user_id"], "refused", actor="governor" if row["agentos_role"] else "agent", ok=False,
                       run=run.id, conversation=row["id"], tool=str(tool)[:100])

    async def _task_list(self, run: ActiveRun, user: int, arguments: dict[str, Any]) -> str:
        rows = [t for t in self.relay.schedules.all(user) if t.get("governed") or t.get("managed")]
        if not rows:
            return "No AgentOS tasks."
        return "\n".join(_describe_task(t) for t in rows)

    async def _task_done(self, run: ActiveRun, user: int, arguments: dict[str, Any]) -> str:
        """The Governor's report on a task's turn: only for one dispatched to it (Relay keeps whether
        it was); kept as its word (outcome_by governor), next to what Relay saw itself."""
        task = self._task(user, arguments.get("id"))
        if task.get("dispatch_state") not in ("dispatched", "awaiting"):
            raise AgentOSError(f"Task {task['name']!r} isn't dispatched to you now: report on a task when a wake-up's "
                               "briefing gave it to you (task.list says which await your report)")
        ok = str(arguments.get("ok", "true")).lower() not in ("false", "no", "0")
        note = (str(arguments.get("note") or arguments.get("message") or "").strip()[:2000]  # (the script's stdin)
                or self.reports.get(run.id, "")[:2000])
        if not note:
            self.unnoted.setdefault(run.id, []).append(task["id"])  # (its report, when one comes, is the note)
        done = self.relay.db.execute(
            "UPDATE schedules SET dispatch_state = NULL, attempts = 0, outcome = ?, outcome_by = 'governor', "
            "outcome_note = ?, outcome_at = ?, last_error = ? "
            "WHERE id = ? AND dispatch_state IN ('dispatched', 'awaiting')",
            ("done" if ok else "failed", note or None, time.time(), None if ok else (note or "failed"), task["id"]))
        if not done.rowcount:
            raise AgentOSError(f"Task {task['name']!r} was just reported on")
        self.nudged.discard(task["id"])
        self.event(user, "task.reported", actor="governor", ok=ok, run=run.id, task=task["id"], name=task["name"],
                   note=note)
        return f"Task {task['name']!r} marked {'done' if ok else 'failed'}."

    async def _task_create(self, run: ActiveRun, user: int, arguments: dict[str, Any]) -> str:
        """A task of its own: for itself (governed, the default), a prompt for a chosen agent, or one of the user's
        public scripts that Relay runs at its time (script=…)."""
        data = self._task_fields(arguments)
        data.setdefault("governed", not data.get("script"))
        if not data["governed"] and not data.get("provider_id") and not data.get("script"):
            raise AgentOSError("A task for a chosen agent needs provider (or leave it to yourself: governed=true; or "
                               "script=… for one of the user's public scripts)")
        data["managed"] = True
        try:
            self._check_folder(data.get("directory"))
            task = self.relay.schedules.save(user, data)
        except (schedules.ScheduleError, AgentOSError) as exc:
            raise AgentOSError(f"Couldn't make the task: {exc}") from exc
        return f"Made task [{task['id'][:8]}] {task['name']} ({task['describe']}; next {_stamp(task['next_run'])})."

    async def _task_update(self, run: ActiveRun, user: int, arguments: dict[str, Any]) -> str:
        task = self._task(user, arguments.get("id"), own=True)
        data = self._task_fields(arguments)
        try:
            self._check_folder(data.get("directory"))
            task = self.relay.schedules.save(user, data, task["id"])
        except (schedules.ScheduleError, AgentOSError) as exc:
            raise AgentOSError(f"Couldn't change the task: {exc}") from exc
        state = f"next {_stamp(task['next_run'])}" if task["enabled"] else "off"
        return f"Changed task [{task['id'][:8]}] {task['name']} ({task['describe']}; {state})."

    async def _task_delete(self, run: ActiveRun, user: int, arguments: dict[str, Any]) -> str:
        task = self._task(user, arguments.get("id"), own=True)
        self.relay.schedules.delete(user, task["id"])
        return f"Deleted task [{task['id'][:8]}] {task['name']}."

    def _task_fields(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """What task.create and task.update take, as Schedules.save wants it."""
        data: dict[str, Any] = {}
        for ours, theirs in (("name", "name"), ("provider", "provider_id"), ("model", "model"), ("mode", "agent_mode"),
                             ("effort", "effort")):
            if arguments.get(ours) not in (None, ""):
                data[theirs] = str(arguments[ours])
        if data.get("provider_id"):
            self._check_confined(data["provider_id"])
        text = arguments.get("message") or arguments.get("task") or arguments.get("prompt")
        if text:
            data["prompt"] = str(text)
        if arguments.get("when") not in (None, ""):
            try:
                data["when"] = schedules.parse_when(arguments["when"])
            except schedules.ScheduleError as exc:
                raise AgentOSError(str(exc)) from exc
        if "folder" in arguments:
            data["directory"] = str(arguments["folder"] or "") or None
            data["mode"] = "code" if data["directory"] else "chat"
        for flag in ("governed", "enabled"):
            if flag in arguments:
                data[flag] = str(arguments[flag]).lower() not in ("false", "no", "0", "off")
        if arguments.get("script") not in (None, ""):
            data["script"] = self._public_script(arguments["script"])  # (Relay runs it: no agent, no prompt)
            data["script_args"] = str(arguments.get("args") or "")
            data["governed"] = False
        return data

    # The user's public scripts (relay.scripts: public/; their private/ is out of the Governor's sight)

    def _public_script(self, name: Any) -> str:
        """A public script's full name (public/…) from what the Governor gave (report.py, public/report.py,
        examples/ask.py); never one of the user's private ones."""
        given = str(name or "").strip()
        if given.startswith(f"{scripts.PRIVATE}/"):
            raise AgentOSError("That's one of the user's private scripts: not yours to see or run")
        full = f"{scripts.PUBLIC}/{given.removeprefix(scripts.PUBLIC + '/')}"
        if not scripts.NAME.fullmatch(full):
            raise AgentOSError(f"No public script {given or '(no name)'}: script.list has them")
        return full

    def _script_told(self, found: dict[str, Any]) -> str:
        """A script's run for the Governor: how it went, what it started, its output's end."""
        took = f", {round((found['finished'] or time.time()) - found['started'])} s" if found.get("started") else ""
        code = f", exit {found['exit_code']}" if found.get("exit_code") not in (None, 0) else ""
        made = ", ".join(f"[{c['id'][:8]}] {c['title']}" for c in found.get("conversations_titled") or [])
        output = found["output"][-READ_PART:]
        cut = len(found["output"]) > READ_PART or found.get("cut")
        return "\n".join([f"Script run [{found['id'][:8]}] of {found['script']}: {found['status']}{code}{took}.",
                          *([f"It started: {made}"] if made else []),
                          f"Its output{' (its end)' if cut else ''}:" if output.strip() else "It printed nothing.",
                          *([output] if output.strip() else [])])

    def _titled(self, found: dict[str, Any]) -> dict[str, Any]:
        ids = found.get("conversations") or []
        rows = self.relay.db.all(f"SELECT id, title FROM conversations WHERE id IN ({', '.join('?' * len(ids))})",
                                 tuple(ids)) if ids else []
        return {**found, "conversations_titled": [dict(r) for r in rows]}

    async def _script_list(self, run: ActiveRun, user: int, arguments: dict[str, Any]) -> str:
        found = [s for s in self.relay.scripts.listed() if not s["private"]]
        if not found:
            return "The user has no public scripts (the Scripts page's public/)."
        lines = []
        for script in found:
            last = self.relay.scripts.runs(script["name"], user)[:1]
            ran = f"; last run {last[0]['status']} {_stamp(last[0]['started'])}" if last else "; never run"
            lines.append(f"- {script['name'].removeprefix(scripts.PUBLIC + '/')}"
                         + (" (one of Relay's examples)" if script["example"] else "") + ran)
        return "The user's public scripts (script.read name=…, script.run name=… args=…):\n" + "\n".join(lines)

    async def _script_read(self, run: ActiveRun, user: int, arguments: dict[str, Any]) -> str:
        name = self._public_script(arguments.get("name") or arguments.get("id"))
        try:
            text = self.relay.scripts.read(name)
        except scripts.ScriptError as exc:
            raise AgentOSError(str(exc)) from exc
        return f"{name}:\n{text}"

    async def _script_run(self, run: ActiveRun, user: int, arguments: dict[str, Any]) -> str:
        """Run a public script as the user; its end when it comes within SEND_WAIT, else how to see it later."""
        name = self._public_script(arguments.get("name") or arguments.get("id"))
        try:
            run_id = await asyncio.to_thread(self.relay.scripts.start, user, name, str(arguments.get("args") or ""))
        except scripts.ScriptError as exc:
            raise AgentOSError(str(exc)) from exc
        deadline = time.monotonic() + SEND_WAIT
        while time.monotonic() < deadline:
            found = self.relay.scripts.run(run_id)
            if found is not None and found["status"] != "running":
                return self._script_told(self._titled(found))
            await asyncio.sleep(0.5)
        return (f"Script run [{run_id[:8]}] of {name} goes on (more than {SEND_WAIT} s): "
                f"script.output run={run_id[:8]} later.")

    async def _script_output(self, run: ActiveRun, user: int, arguments: dict[str, Any]) -> str:
        wanted = str(arguments.get("run") or arguments.get("id") or "").strip()
        rows = self.relay.db.all("SELECT id FROM script_runs WHERE user_id = ? AND id LIKE ? AND script LIKE ?",
                                 (user, f"{wanted}%", f"{scripts.PUBLIC}/%")) if wanted else []
        if len(rows) != 1:
            raise AgentOSError(f"No run {wanted or '(no id)'} of a public script")
        found = self.relay.scripts.run(rows[0]["id"])
        return self._script_told(self._titled(found)) + (
            "\n(It's still running.)" if found["status"] == "running" else "")

    def _new_project(self, folder: str) -> None:
        """A code task's folder that isn't there yet: a new project, made when it's in a workspace root
        (the Governor's own box hasn't the projects, so it can't make one itself)."""
        from relay.web import within_roots
        path = Path(folder).expanduser()
        if path.is_absolute() and not path.exists() and within_roots(self.relay, path):
            path.mkdir(parents=True)

    def _check_folder(self, folder: str | None) -> None:
        """A task's project folder: one of the user's projects, as for a task they make themselves."""
        from relay.web import within_roots
        if folder and not (Path(folder).is_absolute() and Path(folder).is_dir()
                           and within_roots(self.relay, Path(folder))):
            raise AgentOSError(f"{folder} isn't a project folder in a workspace root")

    def _task(self, user: int, wanted: Any, own: bool = False) -> dict[str, Any]:
        wanted = str(wanted or "").strip()
        task = _one([t for t in self.relay.schedules.all(user) if (t.get("governed") or t.get("managed"))
                     and wanted and t["id"].startswith(wanted)], wanted, "AgentOS task", ": task.list lists them")
        if own and not task.get("managed"):
            raise AgentOSError(f"Task {task['name']!r} is the user's: you may carry it out, not change or delete it")
        return task

    async def _chat_list(self, run: ActiveRun, user: int, arguments: dict[str, Any]) -> str:
        rows = self.relay.db.all("SELECT * FROM conversations WHERE user_id = ? AND managed = 1 "
                                 "AND agentos_role IS NULL AND parent_conversation IS NULL ORDER BY updated "
                                 "DESC LIMIT 50", (user,))
        if not rows:
            return "No managed chats yet."
        out = []
        for r in rows:
            last = self.relay.db.one("SELECT status FROM runs WHERE conversation_id = ? ORDER BY created DESC LIMIT 1",
                                     (r["id"],))
            state = "working" if self.relay.runs.active_run_for(r["id"]) else (last["status"] if last else "new")
            where = f" in {r['directory']}" if r["mode"] == "code" else ""
            out.append(f"- [{r['id'][:8]}] {r['title']} ({r['mode']}{where}; {r['provider_id']}"
                       f"{' · ' + r['model'] if r['model'] else ''}; {state})")
        return "\n".join(out)

    async def _chat_create(self, run: ActiveRun, user: int, arguments: dict[str, Any]) -> str:
        from relay.routes.conversations import open_conversation
        message = str(arguments.get("message") or arguments.get("task") or "").strip()
        if message:
            self._spend(run)
        folder = arguments.get("folder")
        if folder:
            self._new_project(str(folder))
        provider, model = self._provider(user, arguments, "it's for")
        data = {"provider_id": provider, "model": model,
                "mode": "code" if arguments.get("folder") else "chat", "directory": arguments.get("folder"),
                "isolate": False, "agent_mode": arguments.get("mode") or None,
                "effort": arguments.get("effort") or None,
                "title": str(arguments.get("title") or "")[:200] or None}
        try:
            row = await open_conversation(self.relay, user, data)
        except Exception as exc:  # (the Governor hears why: ApiError and the like)
            task = self.relay.db.one("SELECT id FROM conversations WHERE user_id = ? AND managed = 1 AND directory = ?",
                                     (user, str(folder or "")))
            hint = (f" (to have an agent look at managed code task [{task['id'][:8]}]'s folder: agent.review "
                    f"id={task['id'][:8]})") if task else ""
            raise AgentOSError(f"Couldn't create it: {exc}{hint}") from exc
        self.relay.db.execute("UPDATE conversations SET managed = 1 WHERE id = ?", (row["id"],))
        text = f"Created managed {data['mode']} [{row['id'][:8]}] {row['title']}."
        if message:
            text += "\n\n" + await self._send(row["id"], message)
        return text

    async def _agent_spawn(self, run: ActiveRun, user: int, arguments: dict[str, Any]) -> str:
        if not str(arguments.get("message") or arguments.get("task") or "").strip():
            raise AgentOSError("agent.spawn needs the task (message)")
        return await self._chat_create(run, user, arguments)

    async def _agent_review(self, run: ActiveRun, user: int, arguments: dict[str, Any]) -> str:
        """A fresh agent on a managed code task's folder: the same files and WorkBox (its tests, its
        installed tools), none of its conversation, the model you choose; read-only by default (it may
        run tests and try things, not change the code). For a review by other eyes, of a clone too
        (whose folder isn't in a workspace root, so chat.create can't reach it)."""
        from relay.routes.conversations import open_conversation
        target = self._managed(user, arguments.get("id"))
        if target["mode"] != "code" or not target.get("directory"):
            raise AgentOSError(f"[{target['id'][:8]}] is a chat, not a code task: there are no files to review")
        message = str(arguments.get("message") or arguments.get("task") or "").strip()
        if not message:
            raise AgentOSError("Say what to review and how (message): it doesn't see the task's conversation")
        provider, model = self._provider(user, arguments, "reviews")
        self._spend(run)
        data = {"provider_id": provider, "model": model, "mode": "code",
                "directory": target["directory"], "isolate": False, "agent_mode": arguments.get("mode") or "readonly",
                "effort": arguments.get("effort") or None,
                "title": str(arguments.get("title") or f"Review: {target['title']}")[:200]}
        try:
            row = await open_conversation(self.relay, user, data, anywhere=True)
        except Exception as exc:  # (the Governor hears why: ApiError and the like)
            raise AgentOSError(f"Couldn't start the reviewer: {exc}") from exc
        self.relay.db.execute("UPDATE conversations SET managed = 1 WHERE id = ?", (row["id"],))
        text = (f"Reviewer [{row['id'][:8]}] {row['title']} works in [{target['id'][:8]}]'s folder "
                f"{target['directory']} ({data['agent_mode']} mode).")
        return text + "\n\n" + await self._send(row["id"], message)

    async def _chat_send(self, run: ActiveRun, user: int, arguments: dict[str, Any]) -> str:
        row = self._managed(user, arguments.get("id"))
        message = str(arguments.get("message") or "").strip()
        if not message:
            raise AgentOSError("The message is empty")
        if self.relay.runs.active_run_for(row["id"]):
            raise AgentOSError(f"It's still working: chat.wait id={row['id'][:8]} waits until it's done")
        self._spend(run)
        return await self._send(row["id"], message)

    async def _chat_wait(self, run: ActiveRun, user: int, arguments: dict[str, Any]) -> str:
        """Until the managed chats asked (all that work now, without ids) are done, up to subagents.wait
        seconds (Settings → Limits): their answers, and which still work."""
        from relay import tunables
        wanted = arguments.get("ids") or arguments.get("id") or []
        if isinstance(wanted, str):
            wanted = wanted.replace(",", " ").split()
        if not isinstance(wanted, list):
            raise AgentOSError("ids: the chats' ids (a list, or separated by commas)")
        rows = [self._managed(user, w) for w in wanted] if wanted else [
            r for r in self.relay.db.all("SELECT * FROM conversations WHERE user_id = ? AND managed = 1 AND "
                                         "agentos_role IS NULL", (user,)) if self.relay.runs.active_run_for(r["id"])]
        if not rows:
            return "No managed chat is working now."
        longest = tunables.get("subagents.wait")
        try:
            timeout = max(1.0, min(float(arguments.get("timeout") or longest), longest))
        except (TypeError, ValueError):
            timeout = longest
        deadline = time.monotonic() + timeout
        while any(self.relay.runs.active_run_for(r["id"]) for r in rows) and time.monotonic() < deadline:
            await asyncio.sleep(1)
        done = [r for r in rows if not self.relay.runs.active_run_for(r["id"])]
        parts = []
        for r in done:
            self._saw(r["id"])
            parts.append(f"=== {self._last_answer(r['id'])}")
        for r in rows:
            if r not in done:
                parts.append(f"=== [{r['id'][:8]}] {r['title']}: still working\n{self._progress(r['id'])}")
        if len(done) < len(rows):
            parts.append("You're woken when they're done (or when one seems to go wrong): end your wake-up rather "
                         "than wait long; chat.tell steers one that's going the wrong way.")
        return "\n\n".join(parts)

    async def _chat_tell(self, run: ActiveRun, user: int, arguments: dict[str, Any]) -> str:
        """Steer an agent: a working one is interrupted and gets the message as its next turn; an idle one
        gets it as chat.send would. Not counted in the budget: it's an agent already started."""
        row = self._managed(user, arguments.get("id"))
        message = str(arguments.get("message") or "").strip()
        if not message:
            raise AgentOSError("Say what it should do instead (message)")
        working = self.relay.runs.active_run_for(row["id"])
        if working:
            await asyncio.to_thread(self.relay.runs.stop_and_wait, working, 15,
                                    "the Governor interrupted it to steer it")
            if self.relay.runs.active_run_for(row["id"]):
                raise AgentOSError("It didn't stop in time: try again")
            self._saw(row["id"])  # (its interrupted turn is no news)
            message = f"A message from the Governor, which steers your work: {message}"
        return ("Interrupted it; " if working else "") + await self._send(row["id"], message)

    async def _send(self, conversation_id: str, message: str) -> str:
        row = self.relay.db.one("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
        run_id = await asyncio.to_thread(self.relay.runs.start, row, message)
        self._follow(run_id, row)
        deadline = time.monotonic() + SEND_WAIT
        while self.relay.runs.active_run_for(conversation_id) == run_id and time.monotonic() < deadline:
            await asyncio.sleep(0.5)
        if self.relay.runs.active_run_for(conversation_id) == run_id:
            return (f"[{conversation_id[:8]}] is still working: you're woken when it's done (or when it seems to go "
                    "wrong), so you may end your wake-up.")
        self._saw(conversation_id)
        return self._last_answer(conversation_id)

    def _last_answer(self, conversation_id: str) -> str:
        answer = self.relay.db.one("SELECT m.content, r.status, r.error FROM messages m JOIN runs r ON r.id = m.run_id "
                                   "WHERE m.conversation_id = ? ORDER BY m.id DESC LIMIT 1", (conversation_id,))
        if answer is None:
            return "No answer yet."
        head = f"[{conversation_id[:8]}] {answer['status']}" + (f" ({answer['error']})" if answer["error"] else "")
        text = (answer["content"] or "").strip()
        cut = (f"\n(… {len(text) - READ_PART} more characters: chat.read id={conversation_id[:8]} offset={READ_PART})"
               if len(text) > READ_PART else "")
        return f"{head}:\n{text[:READ_PART] or '(it said nothing)'}{cut}"

    async def _chat_read(self, run: ActiveRun, user: int, arguments: dict[str, Any]) -> str:
        row = self._managed(user, arguments.get("id"))
        if arguments.get("offset") not in (None, ""):  # (a long answer, a part at a time)
            return self._part(row, int(arguments["offset"]))
        count = max(1, min(int(arguments.get("last") or 4), 20))
        messages = self.relay.db.all("SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY id DESC "
                                     "LIMIT ?", (row["id"], count))
        working = self.relay.runs.active_run_for(row["id"])
        if not working:
            self._saw(row["id"])
        body = "\n\n".join(f"{m['role']}: {(m['content'] or '').strip()[:READ_PART]}"
                           + (f"\n(… {len(m['content'].strip()) - READ_PART} more characters: chat.read "
                              f"id={row['id'][:8]} offset={READ_PART})"
                              if len((m["content"] or "").strip()) > READ_PART else "")
                           for m in reversed(messages))
        progress = f"\n\nIts turn now:\n{self._progress(row['id'])}" if working else ""
        return (f"[{row['id'][:8]}] {row['title']}{' (working now)' if working else ''}\n\n"
                f"{body or '(nothing yet)'}{progress}")

    def _part(self, row: dict[str, Any], offset: int) -> str:
        """Its last answer from `offset` on, READ_PART characters at a time."""
        last = self.relay.db.one("SELECT content FROM messages WHERE conversation_id = ? AND role = 'assistant' "
                                 "ORDER BY id DESC LIMIT 1", (row["id"],))
        text = ((last or {}).get("content") or "").strip()
        part = text[max(0, offset):max(0, offset) + READ_PART]
        more = len(text) - max(0, offset) - len(part)
        return (f"[{row['id'][:8]}] its last answer from character {offset} ({len(text)} in "
                f"all):\n\n{part or '(nothing more)'}"
                + (f"\n\n(… {more} more: chat.read id={row['id'][:8]} offset={offset + len(part)})" if more > 0
                   else ""))

    async def _chat_delete(self, run: ActiveRun, user: int, arguments: dict[str, Any]) -> str:
        from relay.routes.conversations import remove_conversation
        row = self._managed(user, arguments.get("id"))
        for child in self.relay.db.all("SELECT * FROM conversations WHERE parent_conversation = ?", (row["id"],)):
            await remove_conversation(self.relay, child)
        await remove_conversation(self.relay, row)
        return f"Deleted [{row['id'][:8]}] {row['title']}."

    async def _chat_fork(self, run: ActiveRun, user: int, arguments: dict[str, Any]) -> str:
        """A managed fork of a chat. The user's own: only when they allow it."""
        from relay.routes.conversations import fork_row
        wanted = str(arguments.get("id") or "").strip()
        row = _one([r
                    for r in self.relay.db.all("SELECT * FROM conversations WHERE user_id = ? AND agentos_role IS NULL "
                                               "AND parent_conversation IS NULL AND temporary = 0", (user,))
                    if wanted and r["id"].startswith(wanted)], wanted, "chat")
        if not self._confined(row["provider_id"]):  # (the fork goes on with its provider: one AgentOS can use)
            raise AgentOSError(f"[{row['id'][:8]}] is on {row['provider_id']}, which runs its program on this machine "
                               "as it is, outside the boxes AgentOS's agents work in: it can't be forked for you")
        if not row["managed"]:
            event = {"type": "permission", "id": uuid.uuid4().hex, "action": "AgentOS: look into your chat",
                     "resources": [f"“{row['title']}” (a managed copy, forked; yours stays as it is)"],
                     "decision": None, "expires": time.time() + 300,
                     "options": [{"id": "once", "label": "Allow", "outcome": "allowed"},
                                 {"id": "denied", "label": "Deny", "outcome": "denied"}]}
            if await self.relay.delegation._choose(run, event) != "once":
                raise AgentOSError("The user didn't allow it. Don't look into it another way.")
        try:
            new = await fork_row(self.relay, row, managed=True)
        except Exception as exc:
            raise AgentOSError(f"Couldn't fork it: {exc}") from exc
        return f"Forked [{row['id'][:8]}] {row['title']} as managed [{new['id'][:8]}] {new['title']}."

    async def _notify_intervention(self, run: ActiveRun, user: int, arguments: dict[str, Any]) -> str:
        text = str(arguments.get("message") or arguments.get("text") or "").strip()
        if not text:
            raise AgentOSError("Say what's needed (message)")
        made = self.intervene(user, text, run.conversation_id)
        return (f"The user is told: Intervention needed [{made[:8]}]. When it changes: notify.update id={made[:8]}; "
                f"when it's settled: notify.resolve id={made[:8]}.")

    async def _notify_update(self, run: ActiveRun, user: int, arguments: dict[str, Any]) -> str:
        """An open intervention's text, anew (what's needed now): the user sees it was updated."""
        found = self._intervention(user, arguments.get("id"))
        text = str(arguments.get("message") or arguments.get("text") or "").strip()
        if not text:
            raise AgentOSError("Say what's needed now (message)")
        self.relay.db.execute("UPDATE agentos_interventions SET text = ?, updated = ? WHERE id = ?",
                              (text[:2000], time.time(), found["id"]))
        self.event(user, "intervention.updated", actor="governor", run=run.id, intervention=found["id"],
                   was=found["text"][:500], text=text[:500])
        return f"Intervention [{found['id'][:8]}] updated."

    async def _notify_resolve(self, run: ActiveRun, user: int, arguments: dict[str, Any]) -> str:
        """An open intervention is settled (the problem is fixed, or no longer applies): it leaves the
        user's list and the briefings; the user sees the Governor settled it, and why."""
        found = self._intervention(user, arguments.get("id"))
        why = str(arguments.get("note") or arguments.get("message") or "").strip()
        if not why:
            raise AgentOSError("Say why it's settled (note): what was done, or why it no longer applies")
        self.relay.db.execute("UPDATE agentos_interventions SET resolved = ?, resolved_by = 'governor', resolution = ? "
                              "WHERE id = ? AND resolved IS NULL", (time.time(), why[:2000], found["id"]))
        self.event(user, "intervention.resolved", actor="governor", run=run.id, intervention=found["id"],
                   text=found["text"][:500], note=why[:500])
        return f"Intervention [{found['id'][:8]}] is settled; the user sees why."

    async def _notify_report(self, run: ActiveRun, user: int, arguments: dict[str, Any]) -> str:
        """What's done or found, for the user to know: on the AgentOS page (From the Governor) and pushed
        if that's set up; not an intervention, which asks the user to act."""
        text = str(arguments.get("message") or arguments.get("text") or "").strip()
        if not text:
            raise AgentOSError("Say what to report (message)")
        self.event(user, "report", actor="governor", run=run.id, text=text[:2000])
        self.relay.notifier.report(text, run.conversation_id)
        self.reports[run.id] = text
        for task_id in self.unnoted.pop(run.id, []):  # (reported on without a note: this is what it found)
            self.relay.db.execute("UPDATE schedules SET outcome_note = ? WHERE id = ? AND outcome_note IS NULL",
                                  (text[:2000], task_id))
        return "The user is told (a report: nothing for them to do)."

    def _intervention(self, user: int, wanted: Any) -> dict[str, Any]:
        wanted = str(wanted or "").strip()
        return _one([i for i in self.relay.db.all(f"SELECT * FROM agentos_interventions WHERE user_id = ? AND {SEEN} "
                                                  "AND resolved IS NULL", (user,)) if wanted
                     and i["id"].startswith(wanted)],
                    wanted, "open intervention", ": the briefing lists the open ones, with their ids")

    # Services (relay.production)

    async def _service_list(self, run: ActiveRun, user: int, arguments: dict[str, Any]) -> str:
        from relay.production import _container, describe
        services = self.relay.production.services(user, managed=True)
        if not services:
            return "No services for AgentOS (the user adds them, and says which AgentOS looks after)."
        boxes = await asyncio.to_thread(self.relay.production.box_states)
        return "\n".join(describe(s, boxes.get(_container(s, s["live"]), "gone") if s["live"] else None)
                         for s in services) + "\nservice.status service=NAME: one in detail " \
            "(its box, processes, last exit, health)."

    async def _models_list(self, run: ActiveRun, user: int, arguments: dict[str, Any]) -> str:
        from relay import ranking
        models = await asyncio.to_thread(ranking.available, self.relay.registry, ranking.listed(self.relay.db)[1])
        if not models:
            return "No provider lists its models now: nothing to rank."
        ranking.note_listed(self.relay.db, models)  # (the ranking is of these from now on)
        return ranking.describe(self.relay.db, self.relay.registry, models) + (
            "\nmodels.score: what you found of new ones (scores=[{provider, model, name, score, estimated, source}]).")

    async def _models_score(self, run: ActiveRun, user: int, arguments: dict[str, Any]) -> str:
        from relay import ranking
        entries = arguments.get("scores")
        if entries is None and arguments.get("message"):  # (the script: JSON on stdin)
            entries = arguments["message"]
        if isinstance(entries, str):
            try:
                entries = json.loads(entries)
            except ValueError as exc:
                raise AgentOSError(f"scores is a JSON list of objects ({exc})") from None
        if entries is None:  # one, in fields
            entries = [{k: arguments[k] for k in ("provider", "model", "name", "score", "estimated", "source")
                        if k in arguments}]
        if isinstance(entries, dict):
            entries = [entries]
        if not isinstance(entries, list) or not entries:
            raise AgentOSError("Give scores: a list of {provider, model, name, score, estimated, source}")
        if not ranking.listed(self.relay.db)[1]:  # (scores before a models.list: the ranking is of what's here)
            ranking.note_listed(self.relay.db, await asyncio.to_thread(ranking.available, self.relay.registry))
        try:
            done = ranking.save(self.relay.db, self.relay.registry, entries, arguments.get("benchmark") or None)
        except ranking.RankingError as exc:
            raise AgentOSError(str(exc)) from None
        found = ranking.ranking(self.relay.db)
        left = len(found["unscored"])
        return (f"Saved {len(done)}:\n" + "\n".join(f"- {line}" for line in done)
                + f"\nThe ranking has {found['of']} models now"
                + (f"; {left} not looked up yet (models.list)." if left else "; every model has been looked up."))

    async def _memory_review(self, run: ActiveRun, user: int, arguments: dict[str, Any]) -> str:
        from relay import memory, tunables
        days = tunables.get("memory.idle_days")
        found = await asyncio.to_thread(memory.review, self.relay.db, user, time.time(), days)
        if not found["memories"]:
            return "Relay's memory holds nothing."

        def line(m: dict[str, Any], what: str = "") -> str:
            whose = "the user's own" if m["by"] == "you" else f"by {m['by']}"
            return f"- [{m['short']}] ({m['label']}, {m['category']}, {whose}{what}) {m['text']}"
        lines = [(f"Relay's memory: {len(found['memories'])} memories (at most {memory.MOST}). The user's own you "
                  "don't change: name them in your report.")]
        if found["same"]:
            lines.append("Likely the same (worded nearly alike: merge each into one, memory.change the clearest and "
                         "memory.forget the others; when they disagree, the newer usually holds):")
            for group in found["same"]:
                lines += [line(m, f", changed {_ago(m['updated'])}") for m in group] + [""]
        if found["gone"]:
            lines.append("Their place is gone (a folder or a service deleted):")
            lines += [line(m) for m in found["gone"]]
        if found["idle"]:
            lines.append(f"No agent got these for {days:g} days or more (their project not worked on, say):")
            lines += [line(m, f", unused for {m['idle_days']:.0f} days") for m in found["idle"]]
        lines.append("Every memory (used: when an agent last got it):")
        lines += [line(m, f", used {_ago(m['used'])}" if m["used"] else f", changed {_ago(m['updated'])}")
                  for m in found["memories"]]
        return "\n".join(lines)

    def _memory(self, user: int, arguments: dict[str, Any]) -> dict[str, Any]:
        """A memory the Governor may change or forget: any of the user's, but not their own word."""
        from relay import memory
        try:
            found = memory.find(self.relay.db, user, arguments.get("id"))
        except memory.MemoryError as exc:
            raise AgentOSError(f"{exc} (memory.review lists them)") from None
        if found["by"] == "you":
            raise AgentOSError(f"[{found['id'][:6]}] is the user's own: name it in your report, for them to decide "
                               "(Settings → Memory)")
        return found

    async def _memory_change(self, run: ActiveRun, user: int, arguments: dict[str, Any]) -> str:
        from relay import memory
        found = self._memory(user, arguments)
        text = str(arguments.get("text") or arguments.get("message") or "").strip() or None  # (the script's stdin)
        if text is None and not arguments.get("category"):
            raise AgentOSError("Say its new text, its new category, or both")
        try:
            changed = memory.change(self.relay.db, user, found["id"], text, category=arguments.get("category") or None)
        except memory.MemoryError as exc:
            raise AgentOSError(str(exc)) from None
        self.event(user, "memory.changed", actor="governor", run=run.id, memory=found["id"], was=found["text"],
                   text=changed["text"], place=memory.label(self.relay.db, changed["project"]))
        return f"Changed [{changed['id'][:6]}] ({changed['category']}): {changed['text']}"

    async def _memory_forget(self, run: ActiveRun, user: int, arguments: dict[str, Any]) -> str:
        from relay import memory
        found = self._memory(user, arguments)
        why = str(arguments.get("why") or arguments.get("message") or "").strip()  # (the script's stdin)
        if not why:
            raise AgentOSError("Say why it goes (why): the user sees it in the activity")
        memory.forget(self.relay.db, user, found["id"])
        self.event(user, "memory.forgotten", actor="governor", run=run.id, memory=found["id"], text=found["text"],
                   place=memory.label(self.relay.db, found["project"]), why=why[:500])
        return f"Forgot [{found['id'][:6]}]: {found['text']}"

    async def _service_status(self, run: ActiveRun, user: int, arguments: dict[str, Any]) -> str:
        from relay.production import describe_status
        service = self._service(user, arguments)
        return describe_status(await asyncio.to_thread(self.relay.production.status, service))

    async def _service_restart(self, run: ActiveRun, user: int, arguments: dict[str, Any]) -> str:
        from relay.production import ProductionError
        service = self._service(user, arguments)
        try:
            return await self.relay.production.restart(service)
        except ProductionError as exc:
            raise AgentOSError(f"Couldn't restart {service['name']}: {exc}") from exc

    async def _service_stop(self, run: ActiveRun, user: int, arguments: dict[str, Any]) -> str:
        from relay.production import ProductionError
        service = self._service(user, arguments)
        try:
            return await self.relay.production.stop_service(service)
        except ProductionError as exc:
            raise AgentOSError(f"Couldn't stop {service['name']}: {exc}") from exc

    async def _service_start(self, run: ActiveRun, user: int, arguments: dict[str, Any]) -> str:
        from relay.production import ProductionError
        service = self._service(user, arguments)
        try:
            return await self.relay.production.start_service(service)
        except ProductionError as exc:
            raise AgentOSError(f"Couldn't start {service['name']}: {exc}") from exc

    async def _service_delete(self, run: ActiveRun, user: int, arguments: dict[str, Any]) -> str:
        from relay.production import ProductionError
        service = self._service(user, arguments)
        try:
            await self.relay.production.delete(service)
        except ProductionError as exc:
            raise AgentOSError(f"Couldn't delete {service['name']}: {exc}") from exc
        return f"Deleted the service {service['name']}, with its versions and data."

    async def _service_logs(self, run: ActiveRun, user: int, arguments: dict[str, Any]) -> str:
        service = self._service(user, arguments)
        lines = max(10, min(int(arguments.get("lines") or 80), 400))
        return await asyncio.to_thread(self.relay.production.logs, service, None, lines)

    async def _workbox_clone(self, run: ActiveRun, user: int, arguments: dict[str, Any]) -> str:
        from relay.production import ProductionError
        service = self._service(user, arguments)
        provider, model = self._provider(user, arguments, "works in it")
        message = str(arguments.get("message") or "").strip()
        if message:
            self._spend(run)
        try:
            row = await self.relay.production.clone(user, service, provider, model, arguments.get("mode") or None,
                                                    governor_run=run.id)
        except ProductionError as exc:
            raise AgentOSError(str(exc)) from exc
        text = (f"Cloned {service['name']} into managed code task [{row['id'][:8]}] {row['title']}, folder "
                f"{row['directory']}: its live code, and its data as it is now in .data. To try it there: "
                f"PORT=18080 DATA_DIR=.data {service['command']}. When it's right: production.promote "
                f"service={service['name']} id={row['id'][:8]}.")
        if message:
            text += "\n\n" + await self._send(row["id"], message)
        return text

    async def _workbox_look(self, run: ActiveRun, user: int, arguments: dict[str, Any]) -> str:
        """A command in a managed code task's folder (a clone's too), mounted read-only in a box of its
        own: to see an agent's work without changing it (relay.containers.look)."""
        from relay import tunables
        from relay.containers import ContainerError
        target = self._managed(user, arguments.get("id"))
        if target["mode"] != "code" or not target.get("directory"):
            raise AgentOSError(f"[{target['id'][:8]}] is a chat, not a code task: there's no folder to look in")
        command = str(arguments.get("command") or arguments.get("message") or "").strip()
        if not command:
            raise AgentOSError("Say what to run (command): ls, cat, git diff, grep, a check that only reads")
        try:
            code, output = await asyncio.to_thread(self.relay.containers.look, Path(target["directory"]), command)
        except ContainerError as exc:
            raise AgentOSError(f"Couldn't look: {exc}") from exc
        limit = max(2000, tunables.get("chat.tool_output") - 500)
        cut = f"(… its first {len(output) - limit} characters left out)\n" if len(output) > limit else ""
        return (f"[{target['id'][:8]}] {target['directory']} (read-only), exit code {code}:\n"
                f"{cut}{output[-limit:] or '(no output)'}")

    async def _workbox_delete(self, run: ActiveRun, user: int, arguments: dict[str, Any]) -> str:
        return await self._chat_delete(run, user, arguments)  # (a clone's folder goes with its chat)

    async def _production_promote(self, run: ActiveRun, user: int, arguments: dict[str, Any]) -> str:
        from relay.production import ProductionError
        service = self._service(user, arguments)
        row = self._managed(user, arguments.get("id"))
        try:
            return await self.relay.production.promote_clone(service, row, by=f"governor:{run.id[:8]}")
        except ProductionError as exc:
            raise AgentOSError(str(exc)) from exc

    async def _production_rollback(self, run: ActiveRun, user: int, arguments: dict[str, Any]) -> str:
        from relay.production import ProductionError
        service = self._service(user, arguments)
        try:
            return await self.relay.production.rollback(service, arguments.get("version") or None,
                                                        by=f"governor:{run.id[:8]}")
        except ProductionError as exc:
            raise AgentOSError(str(exc)) from exc

    async def _service_create(self, run: ActiveRun, user: int, arguments: dict[str, Any]) -> str:
        """A new live service from a managed code task's folder (its agent made it run on $PORT, its data in
        $DATA_DIR): AgentOS looks after it, and the task goes on as its WorkBox (production.promote). Its start
        command and port are guessed when not said."""
        from relay.production import ProductionError, guess
        task = self._managed(user, arguments.get("id"))
        if task["mode"] != "code" or not task.get("directory"):
            raise AgentOSError(f"[{task['id'][:8]}] is a chat: a service is made from a code task's folder")
        found = await asyncio.to_thread(guess, self.relay.db, Path(task["directory"]), self.relay.settings.port)
        command = str(arguments.get("command") or found["command"] or "")
        if not command:
            raise AgentOSError("Say how it starts (command=…: it listens on $PORT); nothing in the folder says")
        name = str(arguments.get("name") or found["name"])
        try:
            made = await self.relay.production.create_from(
                user, task, name, command, arguments.get("port") or found["port"], arguments.get("health"),
                managed=True, by=f"governor:{run.id[:8]}")
        except ProductionError as exc:
            raise AgentOSError(f"Couldn't make the service: {exc}") from exc
        address = f"port {made['public_port']} on this machine, /port/{made['public_port']}/ through Relay"
        if made["live"]:
            return (f"Made service {made['name']}: version 1 is live at {address}. [{task['id'][:8]}] goes on as its "
                    f"WorkBox: what's changed there, production.promote service={made['name']} id={task['id'][:8]} "
                    "makes live. Tell the user its address (notify.report).")
        note = (made["versions"][0]["note"] or "") if made["versions"] else ""
        return (f"Made service {made['name']} ({address}), but its first version didn't come up: {note[:1500]} "
                f"Have the agent in [{task['id'][:8]}] fix it, then production.promote service={made['name']} "
                f"id={task['id'][:8]}.")

    async def _service_restore(self, run: ActiveRun, user: int, arguments: dict[str, Any]) -> str:
        """A service's data put back from one of its snapshots (service.status lists them); the data as it
        is now is kept first, so putting that back undoes it."""
        from relay.production import ProductionError
        service = self._service(user, arguments)
        try:
            return await self.relay.production.restore(service, str(arguments.get("snapshot") or ""),
                                                       by=f"governor:{run.id[:8]}")
        except ProductionError as exc:
            raise AgentOSError(str(exc)) from exc

    # Providers (relay.repair)

    async def _provider_repair(self, run: ActiveRun, user: int, arguments: dict[str, Any]) -> str:
        """Another agent repairs Relay's support layer for a provider that keeps failing, on the repair's page
        (relay.creations): it hands in a plan in a managed code task, to follow. Its plan, trying it and applying it
        are the user's: a Needs-you item takes them to its page."""
        from relay.providers import TemplateError
        from relay.repair import RepairError
        from relay.routes.providers import begin_page_repair
        from relay.web import ApiError
        target, helper = str(arguments.get("provider") or ""), str(arguments.get("helper") or "")
        if not target or not helper:
            raise AgentOSError("Say which provider to repair (provider) and whose agent repairs it (helper: another "
                               "provider with tools; relay agents lists them)")
        self._check_confined(helper)
        self._spend(run)
        note = str(arguments.get("message") or arguments.get("note") or "").strip()
        try:
            repair = await asyncio.to_thread(self.relay.creations.begin_repair, user, target)
            if repair["status"] != "setup":
                raise AgentOSError(f"{target} is being repaired already ({repair['id']}, {repair['status']}): its page "
                                   "is the user's")
            row = await begin_page_repair(self.relay, user, repair["id"], note, helper, arguments.get("model") or None,
                                          managed=True)
        except (ApiError, RepairError, TemplateError) as exc:
            raise AgentOSError(f"Couldn't start the repair: {exc}") from exc
        self._follow_started(row)
        name = self.relay.registry.get(target).name
        told = self.intervene(user, f"An agent is looking into Relay's support for {name}, to repair it: approve its "
                              "plan, try the repair and apply it, on its page.", conversation_id=row["id"],
                              repair_id=repair["id"], link=f"/providers/repair/{repair['id']}")
        self.event(user, "provider.repair", actor="governor", run=run.id, conversation=row["id"], target=target,
                   repair=repair["id"], intervention=told)
        return (f"Started repair {repair['id']} of {target}: its agent looks into it and hands in a plan, in managed "
                f"code task [{row['id'][:8]}] (chat.read and chat.send to follow it). Its plan, trying the repair and "
                f"applying it are the user's, on its page (intervention {told[:8]}): Relay checks it fully itself.")

    async def _provider_list(self, run: ActiveRun, user: int, arguments: dict[str, Any]) -> str:
        """The providers (which it added; which run on this machine outside a box), and the kinds it may add, with
        their options and an example."""
        from relay.providers import KINDS, parse_template
        lines = ["Providers (id, kind):"]
        for template in self.relay.registry.templates():
            try:
                boxed = self.relay.registry.provider(template.id).boxed()
            except Exception:  # noqa: BLE001 - a mistake in its folder: not one to vouch for
                boxed = False
            notes = [*(["you added it"] if template.managed else []), *(["off"] if not template.enabled else []),
                     *([] if boxed else ["runs on this machine outside a box: the user's"])]
            lines.append(f"- {template.id} ({template.kind}): {template.name}"
                         + (f"; {', '.join(notes)}" if notes else ""))
        lines += ["", "Kinds you may add (provider.add kind=… id=… name=… options={…}); a secret option is the user's "
                  "to fill in, so one that needs one is theirs to add:"]
        theirs = []
        for kind, cls in KINDS.items():
            try:
                boxed = cls(parse_template(dict(cls.EXAMPLE))).boxed()
            except Exception:  # noqa: BLE001 - its example doesn't stand: not offered
                continue
            if not cls.LISTED:
                continue
            if not boxed:
                theirs.append(f"{kind} ({cls.LABEL})")
                continue
            fields = ", ".join(f"{f.name} ({f.type}{', secret' if f.secret else ''}"
                               f"{', required' if f.required else ''})"
                               for f in cls.FIELDS)
            lines.append(f"- {kind}: {cls.LABEL}. {cls.DESCRIPTION} Options: {fields or 'none'}. Example: "
                         f"{json.dumps(cls.EXAMPLE)}")
        if theirs:
            # (said, or a Governor took the kind for missing and bent another kind's options to it, a trial found)
            lines += ["",
                      f"Kinds that are the user's to add, not yours: {', '.join(theirs)}. Their program runs on this "
                      "machine outside a box. When one of these is asked for, tell the user it's theirs to add "
                      "(Settings → Providers); don't make another kind run it instead."]
        lines.append("A program none of these runs: provider.create sends the user to add it with an agent (they "
                     "install it in a box of its own and log in, approve its agent's plan, and try it first).")
        return "\n".join(lines)

    async def _provider_add(self, run: ActiveRun, user: int, arguments: dict[str, Any]) -> str:
        """A provider of a kind Relay has, whose program runs in a box of Relay's (or a chat API), without the
        user's secrets, and whose box mounts nothing a test box may not (relay.boxes.refused_mount): marked as added
        by AgentOS (managed), which may remove it."""
        from relay.boxes import refused_mount
        from relay.providers import KINDS, TemplateError, parse_template
        kind = str(arguments.get("kind") or "")
        cls = KINDS.get(kind)
        if cls is None:
            raise AgentOSError(f"There's no kind {kind!r}: provider.list says which you may add")
        options = arguments.get("options") or {}
        if isinstance(options, str):  # (the script: JSON in a key=value)
            try:
                options = json.loads(options)
            except json.JSONDecodeError as exc:
                raise AgentOSError(f"options: JSON, e.g. {{\"base_url\": \"…\"}} ({exc})") from exc
        if not isinstance(options, dict):
            raise AgentOSError("options: an object, e.g. {\"models\": [\"…\"]}")
        secrets = [f for f in cls.FIELDS if f.secret]
        if [f for f in secrets if options.get(f.name)]:
            raise AgentOSError(f"{', '.join(f.name for f in secrets)} is a secret: the user's to fill in, not yours")
        if [f for f in secrets if f.required]:
            raise AgentOSError(f"{cls.LABEL} needs {', '.join(f.name for f in secrets if f.required)} (a secret): it's "
                               "the user's to add (Settings → Providers); tell them so")
        data = {"id": str(arguments.get("id") or kind), "name": str(arguments.get("name") or cls.LABEL), "kind": kind,
                "options": options, "managed": True}
        try:
            template = parse_template(data)
            provider = cls(template)
        except TemplateError as exc:
            raise AgentOSError(str(exc)) from exc
        if not provider.boxed():
            raise AgentOSError(f"{cls.LABEL} runs its program on this machine, outside a box of Relay's: "
                               "that's the user's to add (Settings → Providers), not yours. "
                               "A program that needs a kind of its own, in a box: provider.create, "
                               "which sends the user to add it with an agent")
        refused = [f"{path} ({why})" for path, writable in provider.box_mounts()
                   if Path(path).is_absolute() and (why := refused_mount(path, writable, self.relay.settings))]
        if refused:
            raise AgentOSError("Its box would have mounted what a provider you add may not: " + "; ".join(refused))
        try:
            added = self.relay.registry.add(data)
        except TemplateError as exc:
            raise AgentOSError(str(exc)) from exc
        self.event(user, "provider.add", actor="governor", run=run.id, provider=added.id, provider_kind=kind)
        keys = [f.name for f in secrets]
        return (f"Added provider {added.id} ({cls.LABEL}). provider.check id={added.id} tests it fully"
                + (f"; if it needs {' or '.join(keys)}, that's the user's to fill in (tell them)." if keys else "."))

    async def _provider_remove(self, run: ActiveRun, user: int, arguments: dict[str, Any]) -> str:
        from relay.providers import TemplateError
        wanted = str(arguments.get("id") or arguments.get("provider") or "")
        try:
            template = self.relay.registry.get(wanted)
        except TemplateError as exc:
            raise AgentOSError(str(exc)) from exc
        if not template.managed:
            raise AgentOSError(f"{template.name} is the user's: they remove it (Settings → Providers)")
        self.relay.registry.remove(template.id)
        self.event(user, "provider.remove", actor="governor", run=run.id, provider=template.id)
        return f"Removed provider {template.id}."

    async def _provider_check(self, run: ActiveRun, user: int, arguments: dict[str, Any]) -> str:
        """Relay's whole check of a provider (relay.providers.conformance), as Settings' shield runs it: in its box,
        its commands in a WorkBox, through a few short prompts to its model."""
        from relay.providers import TemplateError, conformance
        wanted = str(arguments.get("id") or arguments.get("provider") or "")
        try:
            provider = self.relay.registry.provider(wanted)
        except TemplateError as exc:
            raise AgentOSError(str(exc)) from exc
        self._check_confined(wanted)
        self._spend(run)
        discard = self.relay.containers.discard if self.relay.containers is not None else None
        results = await asyncio.to_thread(conformance.check, provider, str(arguments.get("model") or ""),
                                          self.relay.settings.chat_directory() / ".tests", self.relay.runs.place_check,
                                          discard)
        ok = all(r["ok"] is not False for r in results)
        marks = {True: "✓", False: "✗", None: "–"}
        return "\n".join([f"{wanted}: {'it does what Relay needs of it' if ok else 'not yet'}.",
                          *(f"{marks[r['ok']]} {r['name']}: {r['detail']}" for r in results)])

    async def _provider_create(self, run: ActiveRun, user: int, arguments: dict[str, Any]) -> str:
        """A program Relay doesn't know: the user is sent to add it with an agent (relay.creations), a Needs-you item
        that opens its page with what's known of it. Installing it, logging in, the plan and the trial are theirs."""
        about = " ".join(str(arguments.get(key) or "").strip() for key in ("program", "message", "note")).strip()
        if not about:
            raise AgentOSError("Say what the program is (program, and message: what you know of it), for the user")
        link = "/providers/new?" + urllib.parse.urlencode({"about": about[:1500]})
        program = str(arguments.get("program") or "").strip() or "a program Relay doesn't know"
        told = self.intervene(user, f"A provider for {program} is "
                              "wanted: open its page to install it in a box of its own and log in there; an agent of "
                              "yours then plans and builds its support for Relay, and you try it before it's added.",
                              link=link)
        self.event(user, "provider.create", actor="governor", run=run.id, about=about[:500], intervention=told)
        return (f"The user is asked to add it with an agent (intervention {told[:8]}, with its page): installing it, "
                "logging in, approving the plan and trying it are theirs. Once it's added it's a provider like any "
                "other (provider.list).")

    def _service(self, user: int, arguments: dict[str, Any]) -> dict[str, Any]:
        from relay.production import ProductionError
        try:
            return self.relay.production.get(user, str(arguments.get("service") or ""), managed=True)
        except ProductionError as exc:
            raise AgentOSError(f"{exc}: service.list lists them") from exc

    def intervene(self, user: int, text: str, conversation_id: str | None = None, task_id: str | None = None,
                  service_id: str | None = None, repair_id: str | None = None, link: str | None = None) -> str:
        """"Intervention needed: …" for the user (the AgentOS page, and a push if set up); its id. `link`: a page of
        Relay's it's about (its path), opened from it."""
        made = uuid.uuid4().hex
        self.relay.db.execute("INSERT INTO agentos_interventions (id, user_id, text, conversation_id, created, "
                              "task_id, service_id, repair_id, link) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                              (made, user, text[:2000], conversation_id, time.time(), task_id, service_id, repair_id,
                               link))
        self.event(user, "intervention", conversation=conversation_id, task=task_id, service=service_id,
                   intervention=made, text=text[:500])
        self.relay.notifier.intervention(text, conversation_id)
        return made

    def _managed(self, user: int, wanted: Any) -> dict[str, Any]:
        wanted = str(wanted or "").strip()
        return _one([r for r in self.relay.db.all("SELECT * FROM conversations WHERE user_id = ? AND managed = 1 "
                                                  "AND agentos_role IS NULL", (user,)) if wanted
                     and r["id"].startswith(wanted)],
                    wanted, "managed chat", ": chat.list lists them; a user's own chat takes chat.fork first")

    def _spend(self, run: ActiveRun) -> None:
        """One more agent started this wake-up: within the budget, or AgentOSError."""
        budget = self.budgets.get(run.id)
        if budget is None:
            return  # a run the user started with the Governor: their call
        if budget["agents"] >= budget["max_agents"]:
            raise AgentOSError(f"The budget of this wake-up is used up ({budget['max_agents']} agents). Finish; send "
                               "an intervention if more is needed.")
        spent = self.spent(budget["user"], budget["started"])
        if spent >= budget["max_cost"]:
            raise AgentOSError(f"The budget of this wake-up is used up (about ${spent:.2f} spent). Finish; send an "
                               "intervention if more is needed.")
        budget["agents"] += 1

    def spent(self, user: int, since: float) -> float:
        """What managed chats' runs cost since `since` (as the usage overview counts it)."""
        rows = self.relay.db.all("SELECT r.usage FROM runs r JOIN conversations c ON c.id = r.conversation_id "
                                 "WHERE c.user_id = ? AND (c.managed = 1 OR c.agentos_role = 'governor') "
                                 "AND r.created >= ? AND r.usage IS NOT NULL", (user, since))
        total = 0.0
        for r in rows:
            try:
                total += float(json.loads(r["usage"]).get("cost") or 0)
            except (ValueError, TypeError, AttributeError):
                continue
        return total

    # For the page

    def interventions(self, user: int) -> list[dict[str, Any]]:
        return self.relay.db.all("SELECT id, text, conversation_id, task_id, service_id, repair_id, link, created, "
                                 "updated, resolved, resolved_by, resolution FROM agentos_interventions "
                                 "WHERE user_id = ? ORDER BY created DESC LIMIT 100", (user,))

    def resolve_repair(self, user: int, repair_id: str) -> None:
        """A repair the Governor said was ready was applied: its intervention is settled."""
        for found in self.relay.db.all("SELECT id FROM agentos_interventions WHERE user_id = ? AND repair_id = ? "
                                       "AND resolved IS NULL", (user, repair_id)):
            self.resolve(user, found["id"])

    def resolve(self, user: int, intervention_id: str) -> None:
        done = self.relay.db.execute("UPDATE agentos_interventions SET resolved = ?, resolved_by = 'user' WHERE id = ? "
                                     "AND user_id = ? AND resolved IS NULL", (time.time(), intervention_id, user))
        if done.rowcount:
            self.event(user, "intervention.resolved", actor="user", intervention=intervention_id)


def _now(moment: float) -> str:
    """The server's time, with its zone: the Governor's task times are in it."""
    local = datetime.fromtimestamp(moment).astimezone()
    offset = local.strftime("%z")
    return f"{local.strftime('%a %d %b %Y %H:%M')} {local.tzname()} (UTC{offset[:3]}:{offset[3:]})"


def _steps(events: list[dict[str, Any]], unfinished: str) -> list[str]:
    """A turn's last steps, a line each, with how each went (failed ones marked; `unfinished`: one with no
    result yet)."""
    outcome = {e.get("id"): e.get("ok", True) for e in events if e.get("type") == "tool_result"}
    steps = []
    for event in [e for e in events if e.get("type") == "tool_call"][-STEPS_SHOWN:]:
        given = event.get("input") if isinstance(event.get("input"), dict) else {}
        what = next((str(given[k]) for k in ("description", "command", "file_path", "path", "pattern", "url")
                     if given.get(k)), "")
        state = unfinished if event.get("id") not in outcome else "ok" if outcome[event.get("id")] else "FAILED"
        steps.append(f"  {event.get('name') or 'tool'} {' '.join(what.split())[:120]}: {state}")
    return steps


def _ago(moment: float | None) -> str:
    """How long ago, in days, for the Governor: today, yesterday, 12 days ago."""
    days = int(max(0.0, time.time() - (moment or 0)) // 86400)
    return "today" if days == 0 else "yesterday" if days == 1 else f"{days} days ago"


def _one(found: list[dict[str, Any]], wanted: str, what: str, hint: str = "") -> dict[str, Any]:
    """The one of `found` (those whose id starts with `wanted`) that's meant: its whole id first. None, or more than
    one (a short id can fit two: "No managed chat" was said then), is an AgentOSError that says which."""
    exact = [f for f in found if f["id"] == wanted]
    if exact or len(found) == 1:
        return (exact or found)[0]
    if found:
        raise AgentOSError(f"More than one {what} starts with {wanted}: give more of its id")
    raise AgentOSError(f"No {what} {wanted or '(no id)'}{hint}")


def _stamp(moment: float | None, zone: str | None = None) -> str:
    """A moment for the Governor, in the task's time zone when it has one (else the server's)."""
    if not moment:
        return "never"
    try:
        from zoneinfo import ZoneInfo
        local = datetime.fromtimestamp(moment, ZoneInfo(zone)) if zone else datetime.fromtimestamp(moment)
    except (ValueError, KeyError, OSError):  # (an unknown zone: the server's)
        local = datetime.fromtimestamp(moment)
    return local.strftime("%a %d %b %H:%M")


def _describe_task(task: dict[str, Any]) -> str:
    """A task for task.list: what it is, when it runs next, how its last turn went, and whose turn now."""
    zone = task.get("timezone")
    by = ("Relay's own system task (carry it out, don't change it)" if task.get("kind") == "system"
          else "made by you" if task.get("managed") else "the user's (carry it out, don't change it)")
    who = ("yours to carry out" if task.get("governed")
           else f"Relay runs the script {task['script']}" if task.get("script")
           else f"sent to {task.get('provider_id')} by Relay")
    if not task["enabled"]:
        upcoming = "not planned: it's off" + (" (a one-off that ran)" if task["when"]["kind"] == "once"
                                              and task.get("last_run") else "")
    elif task.get("next_run"):
        upcoming = _stamp(task["next_run"], zone) + (" (due now)" if task["next_run"] <= time.time() else "")
    else:
        upcoming = "not planned"
    lines = [f"- [{task['id'][:8]}] {task['name']} ({by}; {who})",
             f"  when: {schedules.describe(task['when'])}{f' ({zone})' if zone else ''}; next: {upcoming}"]
    last = f"  last: {_stamp(task.get('last_run'), zone)}"
    if task.get("outcome"):
        said = "you reported" if task.get("outcome_by") == "governor" else "Relay saw"
        last += (f"; outcome {task['outcome'].upper()} ({said}, {_stamp(task.get('outcome_at'), zone)})"
                 + (f": {task['outcome_note']}" if task.get("outcome_note") else ""))
    elif task.get("last_error"):
        last += f"; FAILED: {task['last_error']}"
    elif task.get("last_run"):
        last += "; no outcome recorded"
    lines.append(last)
    if task.get("dispatch_state") in ("dispatched", "awaiting"):
        lines.append(f"  now: given to you (attempt {task.get('attempts', 0) + 1}), awaiting your task.done")
    elif task.get("dispatch_state") == "dispatching":
        lines.append("  now: being given to you")
    if task.get("directory"):
        lines.append(f"  folder: {task['directory']}")
    lines.append(f"  task: {task['prompt']}")
    return "\n".join(lines)


def confined(db, conversation: dict[str, Any]) -> bool:
    """Whether a conversation is AgentOS's: the Governor, one it manages, or a subagent of those."""
    row, seen = conversation, set()
    while row is not None:
        if row.get("agentos_role") or row.get("managed"):
            return True
        parent = row.get("parent_conversation")
        if not parent or parent in seen:
            return False
        seen.add(parent)
        row = db.one("SELECT * FROM conversations WHERE id = ?", (parent,))
    return False


def read_boundary(db, conversation: dict[str, Any], directory: Path | str) -> list[Path] | None:
    """What an AgentOS conversation's agent may read with its program's own file tools (the
    providers' judges hold it to this): its folder, what Relay handed it (its attachments and
    handed-over transcript), the skills and Relay's container tools, opencode's scratch folder;
    the Governor also its managed chats' folders (its agents' work) and the services' versions
    and clones. Not the user's own chats, other projects or the rest of Relay's data. None: a
    user's own conversation, which reads anywhere (the user's agents are the user's)."""
    if not confined(db, conversation):
        return None
    from relay import skills
    from relay.containers import CONTAINERFILE
    data = config.data_dir()
    uploads = [Path(r["path"]).parent for r in db.all("SELECT path FROM uploads WHERE conversation_id = ?",
                                                     (conversation["id"],)) if r["path"]]
    found = [Path(directory), data / "handover" / conversation["id"], *uploads, skills.BUILTIN, skills.user_dir(),
             CONTAINERFILE.parent, Path("/tmp/opencode")]
    if conversation.get("agentos_role") == "governor":
        found += [Path(r["directory"]) for r in db.all(
            "SELECT directory FROM conversations WHERE user_id = ? AND managed = 1 AND agentos_role IS NULL",
            (conversation["user_id"],)) if r["directory"]]
        found += [data / "production" / r["id"] for r in db.all(  # the services AgentOS looks after
            "SELECT id FROM services WHERE user_id = ? AND managed = 1", (conversation["user_id"],))]
    claude = skills.claude_folder()
    return [*found, claude] if claude else found


def notebook(user_id: int) -> Path:
    """The Governor's folder: its notes, and the only thing in its WorkBox (the GovernorBox)."""
    return config.data_dir() / "agentos" / str(user_id) / "governor"
