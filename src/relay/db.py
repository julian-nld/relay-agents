"""SQLite storage for users, conversations, messages and runs."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from relay.handover import answer_text

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created REAL NOT NULL,
    expires REAL NOT NULL
);
-- A user's app passwords: for scripts and programs that use the API (the SDK), apart from the account's password
CREATE TABLE IF NOT EXISTS app_passwords (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created REAL NOT NULL,
    last_used REAL
);
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    model TEXT,
    mode TEXT NOT NULL CHECK (mode IN ('chat', 'code')),
    directory TEXT NOT NULL,
    provider_session TEXT,
    pinned INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS conversations_by_user ON conversations(user_id, updated DESC);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    run_id TEXT,
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_by_conversation ON messages(conversation_id, id);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    error TEXT,
    usage TEXT,
    created REAL NOT NULL,
    finished REAL
);
CREATE TABLE IF NOT EXISTS uploads (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    conversation_id TEXT,
    name TEXT NOT NULL,
    mime TEXT NOT NULL,
    size INTEGER NOT NULL,
    path TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS modes (
    id TEXT NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT,
    instructions TEXT,
    access TEXT NOT NULL,
    created REAL NOT NULL,
    PRIMARY KEY (user_id, id)
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS queued_messages (
    id INTEGER PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    attachments TEXT,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS services (  -- AgentOS's live services (relay.production)
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    command TEXT NOT NULL,  -- how it starts; it listens on $PORT, keeps data in $DATA_DIR
    public_port INTEGER NOT NULL UNIQUE,  -- where Relay listens for it
    health TEXT,  -- a path to ask (/health), or nothing: a connection is enough
    live_version TEXT,
    health_ok INTEGER,
    health_detail TEXT,
    health_at REAL,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS service_health (  -- a service's health as it changed (well <-> failing): its history
    service_id TEXT NOT NULL,
    at REAL NOT NULL,
    ok INTEGER NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS service_health_by_service ON service_health (service_id, at);
CREATE TABLE IF NOT EXISTS script_runs (  -- runs of the user's scripts, from the Scripts page (relay.scripts)
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    script TEXT NOT NULL,  -- its name in the scripts' folder: private/x.py, public/x.py, public/examples/x.py
    args TEXT,
    started REAL NOT NULL,
    finished REAL,
    status TEXT NOT NULL,  -- running, succeeded, failed, stopped, timed out
    exit_code INTEGER,
    output TEXT,  -- its last relay.scripts.OUTPUT characters
    cut INTEGER,  -- the characters of its output's start left out
    conversations TEXT,  -- JSON: the conversations it made (with its session), to show and stop with it
    session TEXT  -- while it runs: its session's key (auth_sessions), for a restart of Relay to end it
);
CREATE INDEX IF NOT EXISTS script_runs_by_script ON script_runs (script, started);
CREATE TABLE IF NOT EXISTS memories (  -- Relay's memory: what every agent knows next time (relay.memory)
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project TEXT,  -- NULL: general; else a project folder, service:<id> (its WorkBoxes), or governor (its own)
    text TEXT NOT NULL,
    by TEXT NOT NULL,  -- "you", or the agent that added it (provider · model)
    conversation_id TEXT,  -- the chat it was added in
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS memories_user ON memories (user_id, project);
CREATE TABLE IF NOT EXISTS service_data (  -- a service's data volume's size, once a day: how it grows
    service_id TEXT NOT NULL,
    day TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    PRIMARY KEY (service_id, day)
);
CREATE TABLE IF NOT EXISTS service_versions (
    id TEXT PRIMARY KEY,
    service_id TEXT NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    folder TEXT NOT NULL,  -- the frozen code
    source TEXT,  -- the WorkBox it came from
    port INTEGER,  -- its internal port, while it runs
    status TEXT NOT NULL,  -- building | starting | verifying | live | archived | failed
    note TEXT,
    data_snapshot TEXT,  -- the data as it was before this version took over
    created REAL NOT NULL,
    archived REAL
);
CREATE TABLE IF NOT EXISTS service_clones (  -- a WorkBox made from a service's live version (workbox.clone)
    conversation_id TEXT PRIMARY KEY,  -- its managed code task
    service_id TEXT NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    from_version TEXT NOT NULL,  -- the version that was live when it was made
    governor_run TEXT,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS agentos_events (  -- AgentOS's audit trail (relay.agentos: AgentOS.event)
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    at REAL NOT NULL,
    kind TEXT NOT NULL,  -- capability, refused, wake, dispatch, task, version, health, reconcile, …
    actor TEXT NOT NULL,  -- governor (what a model asked or claims) | relay (what Relay did or checked) | user
    ok INTEGER NOT NULL DEFAULT 1,
    run_id TEXT,
    conversation_id TEXT,
    task_id TEXT,
    service_id TEXT,
    version INTEGER,
    detail TEXT  -- JSON
);
CREATE INDEX IF NOT EXISTS agentos_events_by_user ON agentos_events (user_id, at);
CREATE TABLE IF NOT EXISTS agentos_interventions (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    conversation_id TEXT,  -- the Governor's run that asked, or the chat it's about
    created REAL NOT NULL,
    resolved REAL
);
CREATE TABLE IF NOT EXISTS preferences (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    value TEXT NOT NULL,  -- JSON
    PRIMARY KEY (user_id, key)
);
CREATE TABLE IF NOT EXISTS repairs (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    target_id TEXT NOT NULL,  -- the provider being repaired
    kind TEXT NOT NULL,
    folder TEXT NOT NULL,
    base_commit TEXT,
    conversation_id TEXT,
    status TEXT NOT NULL,  -- working | reviewed | applied | undone | discarded
    review TEXT,  -- JSON (relay.repair)
    created REAL NOT NULL,
    applied REAL
);
CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    prompt TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    model TEXT,
    agent_mode TEXT,
    effort TEXT,
    mode TEXT NOT NULL DEFAULT 'chat',  -- chat | code
    directory TEXT,  -- for code tasks
    isolate INTEGER NOT NULL DEFAULT 1,
    target TEXT NOT NULL DEFAULT 'new',  -- new: a chat of its own each time; same: on in conversation_id
    conversation_id TEXT,
    when_spec TEXT NOT NULL,  -- JSON (relay.schedules)
    timezone TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    next_run REAL,
    last_run REAL,
    last_conversation TEXT,
    last_error TEXT,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS computers (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    token TEXT NOT NULL UNIQUE,
    ask INTEGER NOT NULL DEFAULT 1,  -- ask the user before each command or file change
    created REAL NOT NULL,
    last_seen REAL,
    info TEXT,  -- JSON: os, hostname, user, home, shell, …
    UNIQUE (user_id, name)
);
CREATE TABLE IF NOT EXISTS shares (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    directory TEXT NOT NULL,
    name TEXT NOT NULL,
    read_only INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS model_status (
    provider_id TEXT NOT NULL,
    model TEXT NOT NULL,  -- '' for the provider's default
    ok INTEGER NOT NULL,
    message TEXT,
    seconds REAL,
    checked REAL NOT NULL,
    source TEXT NOT NULL,  -- 'run' or 'test'
    PRIMARY KEY (provider_id, model)
);
CREATE TABLE IF NOT EXISTS model_scores (  -- benchmark scores AgentOS's Governor found (relay.ranking)
    name TEXT PRIMARY KEY,  -- the model as the benchmark names it
    score REAL,  -- NULL: no benchmark knows it
    estimated INTEGER NOT NULL DEFAULT 0,  -- placed on the index's scale from other benchmarks
    note TEXT,  -- what else the Governor found worth saying (a tier, a setting, how it was placed)
    benchmark TEXT,
    source TEXT,
    noted REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS model_names (  -- which of those a provider's model is
    provider_id TEXT NOT NULL,
    model TEXT NOT NULL,
    name TEXT NOT NULL REFERENCES model_scores(name) ON DELETE CASCADE,
    noted REAL NOT NULL,
    PRIMARY KEY (provider_id, model)
);
CREATE TABLE IF NOT EXISTS run_events (
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    event TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
"""


# Columns added after the first release; created on start-up when missing.
MIGRATIONS = {
    "model_scores": {"note": "TEXT",
                     "router": "INTEGER NOT NULL DEFAULT 0"},  # a router: a model per request, so no score of its own
    "conversations": {
        # "none": not tracked; "direct": the agent works in the repository
        # itself; "worktree": it works on its own branch in a separate worktree.
        "git_mode": "TEXT NOT NULL DEFAULT 'none'",
        "repo_root": "TEXT",
        "base_branch": "TEXT",
        "base_commit": "TEXT",
        "branch": "TEXT",
        "worktree": "TEXT",
        "task_state": "TEXT NOT NULL DEFAULT 'open'",  # open | discarded
        # 1 while the title is Relay's own guess, which a better one (from the
        # provider or model) may replace; renaming a chat sets it to 0.
        "title_auto": "INTEGER NOT NULL DEFAULT 0",
        # A temporary chat: in no list, without Relay's memory; it goes when it's left (relay.scheduler tidies
        # one left behind)
        "temporary": "INTEGER NOT NULL DEFAULT 0",
        "forked_from": "TEXT",  # the conversation this one was forked from
        # Until its first run, a fork continues from a copy of this provider
        # session (up to the provider's message fork_before, if set).
        "fork_session": "TEXT",
        "fork_before": "TEXT",
        "agent_mode": "TEXT",  # relay.modes; NULL is the default (Build)
        "effort": "TEXT",  # low … max; NULL: the model's default
        # A subagent's conversation: the run that started it, and its conversation.
        "parent_run": "TEXT",
        "parent_conversation": "TEXT",
        # Relay's tools script (relay.delegation), and the provider session
        # that was told about it.
        "tools_token": "TEXT",
        "tools_session": "TEXT",
        "tools_told_path": "TEXT",  # where the script was then (it moves with the machine)
        # A usage limit stopped the last run: {"message", "resets_at", "provider_id", "model"} …
        "limit_info": "TEXT",
        "timed": "TEXT",  # JSON: /timed's timer (relay.timed): {started, min, optimal, max, asked, ended}
        "instructions": "TEXT",  # its own instructions, after its mode's (relay.modes.with_instructions)
        "tools": "TEXT",  # JSON: Relay's own tools its agents don't get, {kind: false} (relay.relay_tools)
        # … and when Relay tells the agent to go on (relay.scheduler); NULL: it doesn't.
        "resume_at": "REAL",
        # The fallback loop it follows (relay.fallback): a loop's id, "off", or NULL: the one its model is in.
        "fallback_loop": "TEXT",
        # A computer task: the user's computer (relay.computers) the work happens on.
        "computer_id": "TEXT",
        # /clear: the agent starts afresh after this message; the ones up to it are only the user's.
        "cleared_after": "INTEGER",
        # Was /permissions (before agents worked in containers); no longer used.
        "approve_mode": "TEXT",
        # AgentOS (relay.agentos): made by (or forked for) the Governor, which may see, change and
        # delete it; and the Governor's own conversation ("governor").
        "managed": "INTEGER NOT NULL DEFAULT 0",
        "agentos_role": "TEXT",
        # Started outside Relay's pages (a script or program, through the API): by whom, in words
        # (web.started_outside); NULL: in Relay's pages, or by Relay itself
        "external": "TEXT",
    },
    "repairs": {
        # A new provider kind being made (a "creation": relay.creations): its folder's name is target_id
        "creates": "INTEGER NOT NULL DEFAULT 0",
        "program": "TEXT",  # how its program is started
        "about": "TEXT",  # what the user said they installed
        "plan": "TEXT",  # its agent's plan, for the user to approve
        "home": "TEXT",  # the provider's home, where the user installed its program (Template.home)
        "trial": "TEXT",  # its trial Relay, while it's up: {port, pid, password, token}
        "page": "INTEGER NOT NULL DEFAULT 0",  # made on its page (relay.creations), not the old way
    },
    "schedules": {
        # An AgentOS task the Governor carries out (it chooses the agents), rather than a prompt
        # Relay sends a chosen agent by itself.
        "governed": "INTEGER NOT NULL DEFAULT 0",
        # A task that runs one of the user's scripts (relay.scripts) at its time, as the user, without an agent:
        # its name (private/… or public/…; the Governor's, public), its arguments, its last run
        "script": "TEXT",
        "script_args": "TEXT",
        "last_script_run": "TEXT",
        # One of Relay's own tasks (relay.schedules.SYSTEM_TASKS): its key. Reset gives its definition back.
        "system": "TEXT",
        # Made by AgentOS's Governor, which may change and delete it (not the user's own tasks).
        "managed": "INTEGER NOT NULL DEFAULT 0",
        # A governed task's turn (relay.agentos): NULL (idle) | dispatching (the Governor is being
        # woken for it) | dispatched (its wake-up runs: dispatch_run) | awaiting (that ended without
        # the Governor's report, which may still come); how it went, and by whose word.
        "dispatch_state": "TEXT",
        "dispatch_run": "TEXT",
        "dispatched_at": "REAL",
        "attempts": "INTEGER NOT NULL DEFAULT 0",  # wake-ups for this turn that broke off
        "outcome": "TEXT",  # done | failed (the Governor says) | interrupted | unreported (Relay saw)
        "outcome_by": "TEXT",  # governor | relay
        "outcome_note": "TEXT",
        "outcome_at": "REAL",
    },
    "services": {
        "stopped": "REAL",  # (stopped by hand, its versions and data kept: not served, not checked)
        # AgentOS looks after it: the Governor sees it and may change it (clone, promote, roll back).
        # Not: the user's own, which the Governor doesn't know of. (Those from before this: managed.)
        "managed": "INTEGER NOT NULL DEFAULT 1",
        # What's being done to it now (promote, rollback, restart, delete): one at a time.
        "operation": "TEXT",
        "operation_at": "REAL",
        # When its failing was told (the Governor woken, or the user): once until it's well, also
        # across Relay's restarts.
        "alerted_at": "REAL",
    },
    "service_versions": {
        # Where a version came from: the clone (managed code task) and the version it was made
        # from, who worked in it, who promoted it, which version it replaced (the one to roll
        # back to), and what Relay itself checked before it took over (JSON).
        "origin_conversation": "TEXT",
        "origin_title": "TEXT",
        "from_version": "INTEGER",
        "agents": "TEXT",  # JSON [{provider, model}]
        "promoted_by": "TEXT",  # governor:<run> | user
        "promoted_at": "REAL",
        "replaced_version": "INTEGER",
        "validation": "TEXT",
    },
    "agentos_interventions": {
        "task_id": "TEXT",
        "service_id": "TEXT",
        "updated": "REAL",  # its text changed (notify.update)
        "resolved_by": "TEXT",  # user | governor
        "resolution": "TEXT",  # why it's settled (the Governor's word)
        "repair_id": "TEXT",  # a provider's repair (or new provider) it's about: applied or discarded, it's settled
        "link": "TEXT",  # a page of Relay's it's about, opened from it (provider.create: adding a provider)
    },
    "model_status": {
        "resets_at": "REAL",  # a usage limit that stopped it, until then
        "usage": "TEXT",  # JSON: the provider's latest word on its usage windows (relay.limits)
    },
    "runs": {
        "changes": "TEXT",  # JSON summary of the task's changes after the run
        "provider_id": "TEXT",  # who answered, as the conversation can switch
        "model": "TEXT",
        "checkpoint": "TEXT",  # a code task's files before the run (relay.checkpoints)
        "after_checkpoint": "TEXT",  # … and after it
        "agent_mode": "TEXT",
        "effort": "TEXT",
        "session": "TEXT",  # the agent's session, as soon as it's known (to stop it after a crash)
    },
    "memories": {
        "category": "TEXT NOT NULL DEFAULT 'other'",  # what it's about (relay.memory.CATEGORIES)
        "used": "REAL",  # when an agent last got it (relay.memory.note); NULL: not since this was kept
    },
    "messages": {
        "attachments": "TEXT",  # JSON list of {id, name, mime, size}
        # The provider's own id for a user message (opencode), to fork before it.
        "provider_ref": "TEXT",
        "relay_action": "TEXT",  # JSON provenance for Relay's continuation instructions
    },
}


class Database:
    """A thread-safe wrapper around one SQLite connection."""

    def __init__(self, path: Path | str) -> None:
        if isinstance(path, Path):
            path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.execute("PRAGMA foreign_keys = ON")
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        for table, columns in MIGRATIONS.items():
            existing = {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, definition in columns.items():
                if name not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        # Runs of scripts from before private/ and public/ (2.6): those scripts are private/'s now, whichever Relay
        # moved them (relay.scripts); the examples of then are gone
        self.conn.execute("DELETE FROM script_runs WHERE script LIKE 'examples/%'")
        self.conn.execute("UPDATE script_runs SET script = 'private/' || script WHERE script NOT LIKE '%/%'")
        # New providers made on their page before it had a column (a home is theirs alone)
        self.conn.execute("UPDATE repairs SET page = 1 WHERE creates = 1 AND home IS NOT NULL AND page = 0")

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, params)

    def one(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def all(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def transaction(self):
        return _Transaction(self)

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    # Runs

    def append_run_events(self, run_id: str, start: int, events: list[dict[str, Any]]) -> None:
        """Persist a new batch without renumbering the events clients already saw."""
        with self.transaction() as conn:
            if conn.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone() is None:
                return  # deleted while its worker was finishing
            conn.executemany("INSERT INTO run_events (run_id, seq, event) VALUES (?, ?, ?)",
                             [(run_id, start + i, json.dumps(e)) for i, e in enumerate(events)])

    def run_events(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.all("SELECT event FROM run_events WHERE run_id = ? ORDER BY seq", (run_id,))
        return [json.loads(r["event"]) for r in rows]

    def runs_events(self, run_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        """The events of several runs at once."""
        found: dict[str, list[dict[str, Any]]] = {run_id: [] for run_id in run_ids}
        if not run_ids:
            return found
        marks = ",".join("?" * len(run_ids))
        for row in self.all(f"SELECT run_id, event FROM run_events WHERE run_id IN ({marks}) "
                            "ORDER BY run_id, seq", tuple(run_ids)):
            found[row["run_id"]].append(json.loads(row["event"]))
        return found

    def fail_unfinished_runs(self) -> list[dict[str, Any]]:
        """Runs left 'running' by a previous process can't continue: failed, and returned
        (id, provider_id, session, directory) so their agents can be stopped too."""
        with self.transaction() as conn:
            left = [dict(r) for r in conn.execute(
                "SELECT r.id, r.conversation_id, r.provider_id, "
                "COALESCE(r.session, c.provider_session) AS session, c.directory "
                "FROM runs r JOIN conversations c ON c.id = r.conversation_id WHERE r.status = 'running'")]
            now = time.time()
            error = "Relay restarted during this run"
            for row in left:
                events = [json.loads(r["event"]) for r in conn.execute(
                    "SELECT event FROM run_events WHERE run_id = ? ORDER BY seq", (row["id"],))]
                text = answer_text(events)
                if not conn.execute("SELECT 1 FROM messages WHERE run_id = ? AND role = 'assistant'",
                                    (row["id"],)).fetchone():
                    conn.execute("INSERT INTO messages (conversation_id, role, content, run_id, created) "
                                 "VALUES (?, 'assistant', ?, ?, ?)",
                                 (row["conversation_id"], text, row["id"], now))
                # Live cards and approval requests also need a terminal state;
                # otherwise replay would leave a spinner or unanswered request forever.
                cards = {(e["type"], e["id"]): e for e in events
                         if e.get("type") in ("permission", "subagent", "form") and e.get("id")}
                end = []
                for (kind, _), card in cards.items():
                    if kind == "permission" and card.get("decision") is None:
                        end.append({**card, "decision": "denied", "outcome": error})
                    elif kind == "form" and card.get("status") == "pending":
                        end.append({**card, "status": "cancelled", "message": error})
                    elif kind == "subagent" and card.get("status") in ("running", "waiting"):
                        child = conn.execute("SELECT status, usage, error FROM runs WHERE id = ?",
                                             (card.get("run"),)).fetchone()
                        finished = child and child["status"] != "running"
                        end.append({**card, "status": child["status"] if finished else "failed",
                                    "usage": json.loads(child["usage"]) if child and child["usage"] else None,
                                    "error": child["error"] if finished else error})
                end += [{"type": "error", "message": error}, {"type": "done", "status": "failed"}]
                conn.executemany("INSERT INTO run_events (run_id, seq, event) VALUES (?, ?, ?)",
                                 [(row["id"], len(events) + i, json.dumps(e)) for i, e in enumerate(end)])
                usage = next((e for e in reversed(events) if e.get("type") == "usage"), None)
                conn.execute("UPDATE runs SET status = 'failed', error = ?, finished = ?, usage = ? WHERE id = ?",
                             (error, now, json.dumps(usage) if usage else None, row["id"]))
        return [{k: r[k] for k in ("id", "provider_id", "session", "directory")} for r in left]


class _Transaction:
    def __init__(self, db: Database) -> None:
        self.db = db

    def __enter__(self):
        self.db.lock.acquire()
        self.db.conn.execute("BEGIN")
        return self.db.conn

    def __exit__(self, exc_type, exc, tb):
        try:
            self.db.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.db.lock.release()
