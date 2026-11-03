# Relay's SDK (`relay_sdk`): reference

`from relay_sdk import *` gives: `convo`, `conversation`, `login`, `connect`,
`providers`, `models`, `usage`, `limits`, `scripts`, `tasks`, `services`,
`notify`, `Conversation`, `Message`, `Question`, `Script`, `ScriptRun`,
`Task`, `Service` and `RelayError`.

## Signing in

- **On the Scripts page:** a script is signed in already (`RELAY_TOKEN`).
- **Run elsewhere:** call `login(username, password, url=…)` once. Read the
  password from the environment, never from the script.
- **Defaults:** `login(…, provider=, model=)` sets the agent new
  conversations get. Without it, they get the agent of the user's latest
  conversation.

## Conversations

**Creating one:** `convo.create(…)` returns a `Conversation`. It's its id as a
string too, so wherever an id goes, it does.
- **`title`**
- **`workdir`:** a folder on Relay's machine, in a workspace root. It makes a
  code task; `isolate=True` puts it on a branch of its own.
- **The agent:** `provider` (an id, e.g. `"claude-code"`, `"opencode"`),
  `model` (e.g. `"sonnet"`, `"opencode/big-pickle"`), `effort` (`"low"` …
  `"max"`), `agent_mode` (`"plan"`, `"readonly"`, a mode's id),
  `fallback_loop`.
- **`instructions`:** its own, after its mode's, for every agent in it.
- **`tools`:** Relay's own tools on or off, e.g. `{"web": False,
  "subagents": False}` (kinds: web, memory, subagents, computers). The
  agent's program keeps its own.
- **`uses_vpn`:** `True` (the only tunnel) or a tunnel's name.
- **`temporary=True`:** a temporary chat: in no list, without Relay's memory,
  no title asked of a model. `chat.delete()` it when done (Relay removes one
  left behind after a day); `chat.keep()` makes it a chat like any other.

**An existing one:** `conversation(id)`.

**Reading it:** each read asks Relay anew.
- `chat.get()` gives all of it: title, provider_id, model, mode, directory,
  instructions, tools, vpn, timed, queued, running_run, messages.
- The properties `title`, `provider`, `model`, `agent_mode`, `effort`,
  `mode` (`"chat"`/`"code"`), `directory`, `running`, `timer`, `queued`,
  `instructions`, `tools` and `vpn`.
- `instructions`, `tools`, `vpn` and `title` can be set too.
- `chat.messages()`: its answers so far, as Messages.

**Talking:**
- **`chat.send(prompt, stream=False, files=None, timed=None,
  on_question=None)`** returns a Message.
  - `stream=False` waits until the answer is done.
  - `files` are paths on the script's side, sent as attachments.
  - `timed` keeps the agent at it: minutes, `"1h30"`, `(min, max)` or `(min,
    max, optimal)`.
- **`chat.queue(prompt)`:** sends it once the answer going now is done.
- **`chat.follow(on_question=None)`:** each turn as it comes, the one going
  now first, until nothing runs and nothing is queued. `chat.wait()` gives
  them as a list.
- **`chat.retry()`, `chat.retry(answer)`, `chat.edit(answer, new_text)`:**
  each returns a Message, and what came after goes.
- **`chat.fork(at=answer)`:** returns a new Conversation.
- **`chat.clear()`:** the agent starts afresh.
- **`chat.stop()`, `chat.stop_timer()`**
- **After a usage limit:** `chat.go_on(provider=, model=)`,
  `chat.resume_later()`, `chat.cancel_resume()`.

**Changing it:**
- `chat.switch(provider=, model=, effort=, agent_mode=, fallback_loop=)`:
  from the next turn on. Another provider gets the conversation handed over.
- `chat.rename(title)`, `chat.pin()`, `chat.delete()`.

**A code task:**
- **`chat.changes()`:** `{"files": [{"path", "diff", …}], "summary": {"files",
  "additions", "deletions"}, …}`.
- **`chat.rewind("start")` or `chat.rewind(answer)`:** its files as they were.
- **`chat.commit(message)`:** returns the commit.
- **`chat.merge()`:** a task with `isolate=True`; returns the branch merged
  into.
- **`chat.discard()`**, **`chat.reviewed()`**

## A Message

**Its answer:**
- **`response`:** the answer's text.
- **`text`:** the whole answer as Markdown, its tool calls in it.
- **`json`:** the answer as Relay keeps it.

**What it did:**
- **`tools_called`:** `[{"name", "input", "ok", "output"}]`.
- **`elapsed_time`:** seconds.
- **`usage`:** `{"tokens", "cost"}`; `cost` is a float.

**Its state:**
- **`status`:** `"succeeded"`, `"failed"` or `"interrupted"`, with `error`.
- **Which agent:** `model`, `provider`.
- **Its times:** `started`, `finished`.

**As it comes:** iterating a Message gives its text as it comes;
`events()` gives every event (`text`, `tool_call`, `tool_result`, `form`,
`permission`, `usage`, `done`, …).

**Questions:**
- `on_question=lambda q: …` is called for each one. `q.kind` is `"form"` (it
  has `q.title` and `q.fields`, each with a `key`) or `"permission"` (it has
  `q.action` and `q.options`).
- Return the answer: a form's `{key: value}` (or `None` to skip it), a
  permission's `True`/`False` or an option's id, or `...` to leave it.
- By hand: `message.questions`, `message.answer(id, value)`.

## Relay

- **`providers()`:** `[{"id", "name", "enabled", "tools", …}]`.
- **`models(provider)`:** `[{"id", "name"}]`, where `""` is its default.
- **`usage(days=30)`:** `{"days", "totals": {kind: {"runs", "cost", …}},
  …}`.
- **`limits()`:** per provider `{"provider_id", "name", "windows": [{"label",
  "used" (0–1), "resets_at", "left"}], "error"}`.

## Scripts

The user's scripts on Relay's Scripts page. A `Script` is its name as a string
too: `private/x.py` (the user's alone), `public/x.py` (agents may read it), or
one of Relay's examples, `public/examples/x.py` (read-only). A name without its
folder (`x.py`) is private/'s, else public/'s.
- **`scripts.list()`:** `[{"name", "private", "example", "size", "modified",
  "running"}]`.
- **`scripts.get(name)`**, **`scripts.create(name, text)`:** a Script. Create
  never saves over one that's there; `script.save(text)` does.
- **`script.text`**, **`script.runs()`** (its last runs), **`script.delete()`**.
- **`script.run(args)`** or **`scripts.run(name, args)`:** a `ScriptRun`, going
  on. `args`: as typed on the page, or a list (each quoted).
- **Where it is:** `script.move("public")` or `("private")`, `script.copy()`
  (a new one of the user's), `script.rename("y.py")`. Each returns the script
  as Relay named it: a name that's taken gets `_2`, `_3`, ….
- **`scripts.write(description, provider=, model=)`:** one an agent writes,
  as the page's With an agent: a code task in `public/` with this skill;
  returns its Conversation (`.wait()` for it).

**A `ScriptRun`** is its id as a string too.
- **Its output as it's written:** iterate it (`for text in run: …`).
- **Its end:** `run.wait()`; `status` (`"succeeded"`, `"failed"`, `"stopped"`,
  `"timed out"`), `exit_code`, `output` and `conversations` (the ones it
  started) wait for it.
- **Now:** `run.get()`; **`run.stop()`** ends it and the agents it started.

## Scheduled tasks

As on the AgentOS page: a prompt for an agent, or one of the user's scripts, at
their times. A `Task` is its id as a string too.
- **`tasks.create(name, when, prompt=, script=, args=, provider=, model=,
  workdir=, governed=False, enabled=True)`:**
  - `when` in words: `"daily 08:00"`, `"weekdays 09:00"`, `"weekly mon,wed
    08:00"`, `"every 6 hours"`, `"once 2026-10-05 09:00"`, `"in 10 minutes"`;
  - `prompt` with an agent (by default the session's, else the user's latest
    conversation's), `workdir` for a code task in that folder;
  - or `script` (with `args`): Relay runs it as the user, with no agent;
  - `governed=True`: AgentOS's Governor picks who does it.
- **`tasks.list()`**, **`tasks.get(id)`**.
- **`task.get()`:** its name, when, next_run, last_run, last_error, and its
  outcome (`"done"`/`"failed"`) with outcome_note.
- **`task.update(name=, when=, prompt=, …)`**, **`task.delete()`**.
- **`task.run()`:** now (its schedule stays); returns the Conversation it went
  to, or a script task's ScriptRun.

## Live services

The user's Production boxes. A `Service` is its id as a string too.
- **`services.list()`**, **`services.get(name or id)`**.
- **Reading it:** `service.get()` (its versions, snapshots, address, health),
  `service.status()` (with `text`: how it is, in words), `service.logs()`,
  `service.snapshots()`.
- **Acting on it:** `start()`, `stop()`, `restart()`, `rollback()` (or
  `rollback(version)`), `restore(snapshot)`. Each returns what Relay did, in
  words. Making, deleting and promoting one stay on its page.

## Notifications

**`notify(text, title=None, priority=None, url=None)`:** to the user's phone or
webhook (Settings → Notifications, the category From your scripts).
- `priority`: `"min"`, `"low"`, `"default"`, `"high"` or `"urgent"`; `url`
  opens when it's tapped.
- Returns whether it went. When it can't (not set up, turned off), the script
  goes on: a warning says why.

## Errors

A `RelayError` has Relay's own words and `.status` (HTTP). It's also what a
time-out or a lost connection raises. Catch it where the script can go on
without that step.
