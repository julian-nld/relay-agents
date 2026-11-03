BEGIN TRANSACTION;
CREATE TABLE auth_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created REAL NOT NULL,
    expires REAL NOT NULL
);
INSERT INTO "auth_sessions" VALUES('52bbeabb24d898053c566de625b67b505129e2df465c8f6bdbae04333e1a39f7',1,1.79128494549097347e+09,1.82282094549097347e+09);
CREATE TABLE computers (
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
INSERT INTO "computers" VALUES('8472b57ed31c45aca215bb95fbe32b80',1,'laptop','y-fwwxLMACOh4O8KrPn5J4A_X8nDG6YC9741bdfEGKQ',0,1.7912849455500245e+09,NULL,NULL);
CREATE TABLE conversations (
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
, git_mode TEXT NOT NULL DEFAULT 'none', repo_root TEXT, base_branch TEXT, base_commit TEXT, branch TEXT, worktree TEXT, task_state TEXT NOT NULL DEFAULT 'open', title_auto INTEGER NOT NULL DEFAULT 0, forked_from TEXT, fork_session TEXT, fork_before TEXT, agent_mode TEXT, effort TEXT, parent_run TEXT, parent_conversation TEXT, tools_token TEXT, tools_session TEXT, tools_told_path TEXT, limit_info TEXT, resume_at REAL, fallback_loop TEXT, computer_id TEXT, approve_mode TEXT);
INSERT INTO "conversations" VALUES('0ca6398fce2743879cfc7a31f14cc51e',1,'A chat from 1.0','fake',NULL,'chat','@HOME@/chats/0ca6398fce2743879cfc7a31f14cc51e',NULL,1,1.7912849454954102e+09,1.7912849454954102e+09,'none',NULL,NULL,NULL,NULL,NULL,'open',0,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL);
INSERT INTO "conversations" VALUES('021ba703ddff44d0a8dcd785f5bcebdd',1,'A code task from 1.0','fake',NULL,'code','@HOME@/ws/site',NULL,0,1.79128494552783775e+09,1.79128494552783775e+09,'none',NULL,NULL,'67f26f298d0908820772f6e31efac68f7e9f731f',NULL,NULL,'open',0,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    run_id TEXT,
    created REAL NOT NULL
, attachments TEXT, provider_ref TEXT, relay_action TEXT);
INSERT INTO "messages" VALUES(1,'0ca6398fce2743879cfc7a31f14cc51e','user','hello from 1.0',NULL,0.0,NULL,NULL,NULL);
INSERT INTO "messages" VALUES(2,'021ba703ddff44d0a8dcd785f5bcebdd','user','list the files',NULL,1.0,NULL,NULL,NULL);
INSERT INTO "messages" VALUES(3,'021ba703ddff44d0a8dcd785f5bcebdd','assistant','Done from 1.0.','run10',2.0,NULL,NULL,NULL);
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE model_status (
    provider_id TEXT NOT NULL,
    model TEXT NOT NULL,  -- '' for the provider's default
    ok INTEGER NOT NULL,
    message TEXT,
    seconds REAL,
    checked REAL NOT NULL,
    source TEXT NOT NULL, resets_at REAL, usage TEXT,  -- 'run' or 'test'
    PRIMARY KEY (provider_id, model)
);
CREATE TABLE modes (
    id TEXT NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT,
    instructions TEXT,
    access TEXT NOT NULL,
    created REAL NOT NULL,
    PRIMARY KEY (user_id, id)
);
INSERT INTO "modes" VALUES('reviewer',1,'Reviewer','Reviews','Review it.','read',1.79128494553470659e+09);
CREATE TABLE preferences (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    value TEXT NOT NULL,  -- JSON
    PRIMARY KEY (user_id, key)
);
INSERT INTO "preferences" VALUES(1,'fallback_loops','[{"id": "1d3713ff", "name": "My loop", "entries": [{"provider": "fake", "model": "m"}]}]');
INSERT INTO "preferences" VALUES(1,'subagents','{"allowed": [{"provider": "fake", "model": "m"}], "free": true, "same": true, "others": "ask", "local": false}');
CREATE TABLE queued_messages (
    id INTEGER PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    attachments TEXT,
    created REAL NOT NULL
);
INSERT INTO "queued_messages" VALUES(1,'021ba703ddff44d0a8dcd785f5bcebdd','and then the tests','[]',3.0);
CREATE TABLE repairs (
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
CREATE TABLE run_events (
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    event TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
INSERT INTO "run_events" VALUES('run10',0,'{"type": "session", "session": "s1"}');
INSERT INTO "run_events" VALUES('run10',1,'{"type": "tool_call", "id": "t1", "name": "shell", "input": {"command": "ls"}}');
INSERT INTO "run_events" VALUES('run10',2,'{"type": "tool_result", "id": "t1", "name": "shell", "ok": true, "output": "app.py"}');
INSERT INTO "run_events" VALUES('run10',3,'{"type": "text", "delta": "Done from 1.0."}');
INSERT INTO "run_events" VALUES('run10',4,'{"type": "done", "status": "succeeded"}');
CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    error TEXT,
    usage TEXT,
    created REAL NOT NULL,
    finished REAL
, changes TEXT, provider_id TEXT, model TEXT, checkpoint TEXT, after_checkpoint TEXT, agent_mode TEXT, effort TEXT, session TEXT);
INSERT INTO "runs" VALUES('run10','021ba703ddff44d0a8dcd785f5bcebdd','succeeded',NULL,'{"input": 1200, "output": 300, "cost": 0}',1.0,2.0,'{"files": 1, "additions": 2, "deletions": 0}','fake','m',NULL,NULL,'build',NULL,NULL);
CREATE TABLE schedules (
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
INSERT INTO "schedules" VALUES('7edcc6379f4d482eb6fb039cfa9e2466',1,'Morning summary','Summarise the news','fake',NULL,NULL,NULL,'chat',NULL,0,'new',NULL,'{"kind": "daily", "time": "08:00"}',NULL,1,1791352800.0,NULL,NULL,NULL,1.79128494553784298e+09);
CREATE TABLE shares (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    directory TEXT NOT NULL,
    name TEXT NOT NULL,
    read_only INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL
);
CREATE TABLE uploads (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    conversation_id TEXT,
    name TEXT NOT NULL,
    mime TEXT NOT NULL,
    size INTEGER NOT NULL,
    path TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created REAL NOT NULL
);
INSERT INTO "users" VALUES(1,'me','scrypt$16384$8$1$57ee8c71024a215f421c5e194a00ce4e$48ec5dc9b95bc8c33b4cf62341af9da8fa331927a4f845b9dca13ef8397b5b4e',1.79128494549078679e+09);
CREATE INDEX conversations_by_user ON conversations(user_id, updated DESC);
CREATE INDEX messages_by_conversation ON messages(conversation_id, id);
COMMIT;
