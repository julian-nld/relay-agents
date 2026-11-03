# Relay development log

The plan for Relay's next round of features, the decisions behind it, and what
got done. Newest log entries at the bottom.

## Plan (agreed 2026-09-29)

| Phase | What | Status |
|-------|------|--------|
| 0 | Bug fixes and visual quirks | done |
| 1 | Fork a conversation, edit any message, hand a conversation over when switching provider | done |
| 2 | Automatic change tracking with per-turn checkpoints and "undo this turn" | done |
| 3 | Modes: `/plan`, `/build` and your own | done |
| 4 | Subagents on any provider or model through a Relay `delegate` tool (MCP), with permission prompts in the UI | done |
| 5 | Message queue while an agent works, usage overview, install as an app (PWA) | done |

Each phase ends with tests, the UI walkthrough (`scripts/relay-ui-check.py`),
a commit and an entry below.

### Phase 0 — bugs and quirks

- Typing while a chat is still loading sends the message to the chat that was
  on screen before; an answer can land in the wrong view when you navigate away
  while it's being sent.
- Message actions (copy, regenerate, edit, cost) only appear on hover, so
  they're hard to reach on a phone.
- opencode ignores a model chosen halfway a conversation: the model is only
  passed when the session is created (opencode has `POST /api/session/{id}/model`).
- Switching to an agent provider starts it without the conversation so far
  (Relay warns about it; the handover in phase 1 fixes it).
- A pass over the whole UI at phone width and in dark mode.

### Phase 1 — fork, edit, handover

- **Fork**: a new conversation from any message, linked to its origin. Chat
  APIs copy the messages; opencode forks natively (`POST /api/session/{id}/fork`
  with `before`); Claude Code forks the end natively (`--fork-session`) and
  earlier points through the handover.
- **Edit any message**: chat APIs rewind to it (after a confirmation, the
  rest goes); opencode rewinds natively (`revert`); for Claude Code an edit
  becomes a fork. No ChatGPT-style version browsing: a fork covers that at a
  fraction of the complexity.
- **Handover** when switching provider: chat APIs already get the whole
  history; an agent's first turn gets a condensed transcript (text in full,
  tool calls as one line, no tool output), summarised first when it's too
  long. A divider in the chat marks the switch. A new model in the same
  provider is marked with a divider too.

### Phase 2 — automatic change tracking

No `git init` in people's folders (it adds `.git`, can end up tracking a home
folder or `node_modules`, and clashes with repositories higher up). Instead a
shadow repository in Relay's data folder tracks the project folder from the
outside, like Claude Code and opencode do internally. That also gives a
checkpoint per turn and "undo this turn". Real git repositories keep working
on their own branch, which becomes the default.

### Phase 3 — modes

A mode is a name, instructions, permissions (read-only or edit) and optionally
a default model. Relay maps it onto what each provider has: opencode's plan and
build agents, Claude Code's `--permission-mode plan` plus instructions, and
instructions only for chat APIs. Pick one with `/` in the composer; manage your
own under Settings. Modes double as personas for subagents in phase 4.

### Phase 4 — subagents

Relay offers agents a `delegate` tool over MCP (both Claude Code and opencode
support MCP), so an agent can hand work to any Relay provider, model or mode —
several at once, each without the others' context. Subagents show up as
collapsible cards with their own model and cost label.

- Limits: subagents can't start subagents, at most 3 run at once (a Claude
  Code process takes roughly 200–400 MB on a Pi), and a budget per run.
- Relay doesn't orchestrate itself; the agents do.
- Within one provider this exists already (opencode subagents can use any
  model, Claude Code can use Haiku); Relay adds crossing providers,
  visibility and your own modes.
- Cheap subagents only pay off when the main agent checks their work (tests,
  review). With a subscription they save usage limits, not money.
- The same MCP server can answer Claude Code's permission prompts, so
  approving or refusing actions from the phone works for both agents.

### Phase 5 — extras

- Queue a message while an agent is still working.
- Usage overview per day and provider, API costs and plan use apart.
- Install Relay as an app on a phone (PWA manifest and icon).

## Log

### 2026-09-29

- Answers now say who pays: "$0.02 API", "in plan" (with the API-price
  estimate on hover) or "free" (e70d4bd).
- Plan above agreed. Starting phase 0.
- Phase 0 done:
  - Opening a chat makes the one on screen inert until the new one is loaded,
    so a message typed meanwhile can't go to the wrong chat; a chat that loads
    after you went elsewhere isn't shown. A message, retry or new chat whose
    request finishes after you navigated away no longer pulls you back or
    streams into the wrong view.
  - opencode now switches model halfway a conversation
    (`POST /api/session/{id}/model`); it only took the model at the start.
  - Touch screens: the actions under messages and the chat menu stay visible
    (there's no hover).
  - Phones: the header's Changes button stuck out of the screen and the
    settings window was wider than the screen (a grid column sized to the
    tabs). Truncated text can now shrink in flex rows everywhere.
  - Tiny images get a minimum size so they can still be tapped.
  - UI check: five more phone screens (chat, code task, changes, tasks,
    settings) with checks for sideways scrolling and things sticking out;
    screenshots wait for animations; fixed a race where the check typed
    into a chat before it had opened.
- Phase 1 done:
  - **Edit** works on every message you sent, for every provider: chat APIs
    get the shorter history, opencode continues from a copy of its session
    up to that message (it reports each turn's message id, which Relay keeps),
    Claude Code gets a fresh session with a handover. That's an in-place edit
    for all of them rather than "an edit becomes a fork" for Claude Code, as
    planned: simpler to use and the same result.
  - **Fork** under any answer and in the chat menu. Claude Code forks
    natively at the end (`--resume … --fork-session --session-id …`),
    opencode anywhere (`POST /api/session/{id}/fork` with `before`). Claude
    Code only finds sessions from the folder they ran in, so a fork that
    gets its own folder (a code task on its own branch: a new worktree
    starting from a snapshot of the task's files) gets a handover instead.
    Attachments are copied; forks of chats share their folder, which is now
    only removed with the last chat using it.
  - **Handover** instead of a summary by a model: no extra model call, and
    nothing is lost for good. The first prompt of a fresh agent session
    carries the condensed conversation; beyond 24,000 characters the oldest
    messages are left out of the prompt and the whole conversation is handed
    over as a file. It's worded as the user giving context: the first
    version, a system-like note, was refused by Claude as a prompt
    injection.
  - A line in the chat marks where another provider or model took over;
    each answer now records which provider and model gave it.
  - Fixed on the way: after an answer finished, the provider and model
    pickers showed the conversation as it was before (a stale object).
  - Checked for real with Claude Code (Haiku) and opencode (a free Zen
    model): forks remember what they should, edits forget what they should,
    and switching providers either way keeps the conversation.
- Phase 2 done:
  - `relay/checkpoints.py`: per code task a git repository in
    `data/relay/checkpoints/<id>.git` with the task's folder as work tree
    (`GIT_DIR` + `GIT_WORK_TREE`), so the folder gets no `.git` and a user's
    repository keeps its index and history. It keeps its own index, so each
    checkpoint only rehashes what changed. Skips `node_modules`, virtual
    environments, build output and Relay's own data folder; gives up on
    folders with more than 20,000 files.
  - A checkpoint before every run of a code task (worktree, direct and
    no-git alike). **Undo files** under an answer restores the files from
    before it (new files removed, ignored files left alone), after taking a
    checkpoint first, so the toast can undo the undo. Editing an earlier
    message offers "Edit and restore files".
  - Folders without git: the first checkpoint is the task's starting point,
    so the Changes panel works for them too, with **Undo all changes**;
    "Track changes with git" became the secondary **Make it a git
    repository**. They count as "Up to date" in the tasks overview, as
    there's nothing to merge or commit.
  - Checked with Claude Code (Haiku): a new file and an edit to an existing
    one showed up in Changes, and Undo files put the folder back exactly.
- Phase 3 done:
  - `relay/modes.py`: built-in Build and Plan plus people's own modes (name,
    description, instructions, access edit or read-only) in a `modes`
    table; a conversation has an `agent_mode`, each run records it.
  - Claude Code: Plan is `--permission-mode plan`; read-only modes run in
    the default permission mode, where `-p` refuses anything that needs
    permission; instructions go in `--append-system-prompt` (the system
    prompt, so no injection worries).
  - opencode: its `build` or `plan` agent per run
    (`POST /api/session/{id}/agent`). Its plan agent *asks* for edit and
    shell permission rather than refusing, and Relay approved every request,
    so read-only runs now reject `edit`, `write`, `patch`, `shell` and `bash`
    requests themselves. Instructions go in the message, worded as the user.
  - Chat APIs: instructions as a system message (Build and Plan aren't
    offered there). Command agents: `RELAY_MODE*` environment variables.
  - UI: a mode picker, `/name` in the message box (with a menu while
    typing), Settings → Modes, and a line in the chat where the mode changed.
  - Checked for real: Claude Code (Haiku) in Plan and in a read-only mode,
    and opencode (Zen) in a read-only mode, all made no file when asked to;
    back in Build both did.
- Phase 4, part 1 (subagents) done:
  - `relay/delegation.py`: an MCP server over plain HTTP (JSON-RPC, no
    session, no SSE) at `/mcp/<token>`, outside `/api/` so it needs no login;
    the token is random, made per run and dropped when the run ends. Tools:
    `list_agents` and `delegate`.
  - A subagent is a hidden conversation (`parent_run`,
    `parent_conversation`), started like any other run. The calling run gets
    `subagent` events (waiting → running → final status and usage), which
    the UI draws as cards that stream the subagent's own events when opened.
    Subagent runs get no MCP tools (one level deep), no checkpoints (the
    calling run has one) and no notifications. Deleting a chat deletes its
    subagents.
  - Claude Code gets `--mcp-config` and `--allowedTools` for the two tools,
    and `MCP_TOOL_TIMEOUT` of 35 minutes: by default it gives up on a tool
    after about a minute (measured). Command agents get `RELAY_MCP_URL`.
  - **opencode as the calling agent isn't done.** Its runtime MCP API
    (`PUT /api/experimental/mcp/{name}`) ignores the location it's given:
    a server is shared by the whole profile, so concurrent runs couldn't be
    told apart, and in a test the session didn't even list the tool (its
    "code mode" hides MCP tools behind a search). opencode can be a
    subagent, and has subagents of its own on any of its models.
  - Something to know: in that test the model found the test server's
    source and called it over HTTP directly. Agents can reach anything on
    localhost, which is why the MCP token is long, random and short-lived.
  - Checked for real: Claude Code (Haiku) ran three subagents at once —
    advocate and cost-benefit on Claude Code, devil's advocate on opencode
    with a free Zen model — and drew its own conclusion; the cards showed
    "in plan" and "free".
- Phase 4, part 2 (permission prompts) done:
  - With `auto_approve` off, an agent's permission requests wait for the
    user: `RunManager.ask/answer` keeps them per run,
    `POST /api/runs/{id}/permission` answers (allow or deny), and the answer
    becomes a second `permission` event with the decision.
  - opencode: the request stays open and Relay replies later
    (`OpencodeBackend.answer`). Before this, a run without auto-approval
    simply hung. Claude Code: `--permission-prompt-tool
    mcp__relay__approve_permission`, a third tool on the same MCP server,
    which waits for the user and returns `{"behavior": "allow"|"deny"}`.
  - UI: Allow and Deny in the answer, an orange dot in the sidebar, "Waiting
    for you" in the tasks overview, and a high-priority ntfy/webhook message.
  - Fixed on the way: a chat reloaded during a turn that followed a switch
    of provider, model or mode had no line marking it; screenshots in the UI
    check now ignore endless animations.
  - Checked for real: Claude Code (Haiku) and opencode (Zen) each asked
    before writing a file and wrote it when allowed; Claude Code refused to
    after a deny.
- Phase 5, queue done: messages typed while an agent works go into a
  `queued_messages` table and the next one starts when a run succeeds (not
  after a failure or a stop), before the run reports its end so a browser
  following it sees the next run straight away. The composer's button
  queues when there's text and stops when there isn't.
- Phase 5, usage overview done: Settings → Usage (`/api/usage?days=`) adds
  up runs, tokens and cost per billing kind, per provider and model, and per
  day. Token counts come in three shapes (Claude Code, opencode, OpenAI
  style), normalised in `relay/usage.py`. The bar heights are set through
  the CSSOM, as the CSP refuses style attributes.
- Phase 5, home screen done: a web manifest, PNG icons rendered from the
  SVG logo by `scripts/make-icons.py` (full-bleed, plus a maskable one), the
  iOS and Android meta tags, a status-bar colour that follows the chosen
  theme, and room for the notch and home indicator in full screen. No
  service worker: browsers only allow those over HTTPS, and Relay runs on
  plain HTTP in the local network; RELAY.md says how to get HTTPS. Also
  centred the "R" in the logo, which sat a little off.
- All five phases of the plan are done. Left for later, when asked:
  preparing and testing Relay on the Raspberry Pi itself, and opencode as
  a caller of subagents (see phase 4).

### Subagents through a script, on top of the providers' own tools

Asked: keep every provider's own MCP and features, and tell the model about
Relay's tools instead, as a "tool call file" in a fixed read-only folder.

- Each agent conversation gets `data/relay/tools/<token>/relay`, a small
  Python script (read-only, folder read-only) with `agents`, `delegate` and
  `wait`. It calls Relay over HTTP with the conversation's token, which only
  works while one of its runs is going. One script per conversation rather
  than per run, because opencode keeps the note (with the path) in its
  session; forks and fresh sessions are told again (`tools_session`).
- `delegate` starts a subagent and returns at once; `wait` waits at most 100
  seconds. Shell tools have time limits (Claude Code's Bash: two minutes by
  default), and this way several subagents run at once.
- The note: Claude Code's system prompt (`--append-system-prompt`, with
  `--allowedTools "Bash(<script>:*)"`); opencode's first message of a
  session, as the user (Relay allows its shell and folder requests for the
  script even in read-only runs); command agents: `RELAY_TOOLS`.
- Relay's MCP endpoint stays only for Claude Code's permission prompts,
  which aren't a model-facing tool. Claude Code's own MCP servers are left
  alone (no `--strict-mcp-config`), as before.
- The UI hides the shell calls that start and wait for subagents (the cards
  show them) and keeps `relay agents` visible.
- Checked for real: **opencode now calls subagents** — it listed the
  agents, started two at once on a free Zen model, waited and concluded in
  12 seconds; Claude Code (Haiku) did the same through the script. In Plan
  mode Claude Code planned the delegation and asked to go ahead, which fits
  what Plan mode is for.

### Test round with real conversations

A copy of the real data (every conversation, desktop and phone) plus the
UI check, looking for rough edges. Fixed:

- Permission notes showed a whole command, heredoc and file included, in one
  line. Now: one line, the rest behind a toggle; requests waiting for you
  show the full command in a scrolling box. Automatic approvals aren't shown
  at all any more: the tool call shows what ran.
- "null" under messages on phones (`replaceChildren` turns `null` into
  text); hidden on desktop until you hovered.
- A tool call that only got its result (interrupted) showed an empty
  "Input" heading.
- Tasks working in place in part of a repository saw, and committed, the
  whole repository's changes: five chats in `~/projects` all showed the
  same "5 files +183". Changes and Commit now keep to the task's folder;
  tasks in the same place share one "Needs review" card (the newest).
- The header's change count came from the last answer; the conversation now
  brings the current count.
- Relay's tools script showed with its long secret path in tool calls; now
  as `relay …`.
- `/plan` typed while an agent worked got queued as a message instead of
  switching mode.
- The UI check now also covers, on a phone: Modes and Usage settings, a
  subagent card, a permission request with a queued message, and the `/`
  menu.

### Every turn a commit, and /rewind

Asked: simplify the git logic by committing after every answer, which also
makes rewinding possible; and a `/rewind` command rather than another button.

- Before a run Relay commits changes made meanwhile ("Changes made outside
  Relay", as the user) and records the commit as the run's checkpoint; after
  the run it commits the agent's changes ("Relay: <message>", as Relay, with
  a `Relay-Task:` trailer). Worktree tasks commit on their branch, in-place
  tasks on the current branch (only their folder); folders without git use
  the checkpoint repository as before.
- Rewinding restores the files to the checkpoint (`git restore --source`,
  which also removes files added since) and commits that. Chosen over
  `git reset --hard`: no history is rewritten, other tasks' and the user's
  commits can't get lost, and undoing a rewind is just another rewind.
- Merge squashes the task into one commit on the base branch, then merges
  that back into the task branch so a later merge only brings what's new.
- Gone: the Commit button, "Track changes with git" (and `git init`), the
  per-run checkpoint repository for git repositories, and the "Undo files"
  button. `/rewind [n]` and `/rewind ` with a menu of answers replace it;
  the composer's `/` menu now lists commands next to modes.
- In-place tasks now count their changes from where the task started, as
  their turns are committed right away.
- Relay's commits use `--no-verify` and `commit.gpgsign=false`, so a hook or
  a signing prompt can't block an automatic commit.
- Checked for real with Claude Code (Haiku), on its own branch and in place:
  one commit per turn, a rewind restored the file with a new commit, and the
  merge put one commit on main.

### Code review of everything since the last review

Read through runs, delegation, tasks, git, checkpoints, the new endpoints and
the frontend. Fixed:

1. Subagents still going when their run ended were stopped only after the
   run was stored, so their cards stayed "running" forever. The run now
   stops its subagents and waits for them (`run.closing`, `settle`) before
   it's stored; no new subagents once it's ending.
2. A permission answer that failed to reach the agent dropped the request,
   so it couldn't be answered again.
3. When opencode replaced a vanished session, the new session wasn't told
   about Relay's tools. A fresh session is always told now.
4. Automatic commits in the middle of a merge, rebase, cherry-pick, revert
   or bisect would have committed half-done work and conflict markers.
   Relay now leaves the repository alone then, and a rewind says why not.
5. A rewind restored the task's folder wholesale, silently undoing commits
   that weren't the task's (the user's own, another task's). It now refuses
   and names them; the UI asks, and "Rewind anyway" goes ahead.
6. After a squash merge, merging the base back into the task branch could
   conflict and leave the worktree mid-merge, which the next turn would
   have committed. It's aborted now.
7. A message queued just as the run ended stayed in the queue; it's sent
   then. After a failed or stopped run, queued messages get "Send now".
8. Opening a subagent card made Edit and Fork disappear from the whole chat
   (the card's own messages were counted), and could put the answer's
   buttons on the card's inner message.
9. `relay wait` with odd arguments raised instead of answering.

Also made a flaky UI check step (an attached image checked before it had
loaded) wait for it.

### Portable: a stick, or copying Relay to another machine

Asked: make the program and the providers portable, so a Relay install moves
easily between devices (HTTPS and Tailscale the user handles themselves).

- `relay portable` copies this machine's `claude` and `opencode` into
  `bin/<os>-<arch>/` and lists what travels and what doesn't. Bundled programs
  come first; when the stick can't run them (no exec bits, or `noexec`, now
  checked through `statvfs`) they're copied to the host cache once
  (`paths.runnable`, generalised from opencode-manager's opencode copy).
- Claude Code's login travels in `data/claude` (`CLAUDE_CONFIG_DIR`) once the
  user logs in with `relay claude` (a pass-through to Claude Code). No copying
  of an existing login: renewed tokens would sign the other copy out.
- `relay/portable.py:relocate` notes where the install is (`meta` table); on
  a start elsewhere it rewrites stored paths (conversations, uploads,
  settings) and runs `git worktree repair`. Data from before this version is
  placed by the chat and worktree folders it contains.
- Sessions follow: opencode sessions are moved to the task's current folder
  (`POST /session/{id}/move`); a Claude Code session that isn't there any more
  (moved folder, or Claude's 30-day cleanup) becomes a fresh session with a
  handover instead of a failed run.
- Relay's tools script moved to the host cache (a stick may not run it) with
  a quoted path (spaces); agents are told again when its path changed.
- Workspace roots may be relative to the Relay folder. The launcher checked
  for `textual` (opencode-manager's) instead of Relay's own dependencies.
- The dev environment can't mount a filesystem image (a podman container
  without loop devices), so the stick was simulated: a copy of the install
  and its data without exec bits, in a folder with a space. There
  `sh relay portable` bundled both agents, `sh relay serve` placed the old
  data, a Claude Code task ran from the host copy and committed its turns,
  and after renaming the folder (another mount point) Relay moved its paths,
  repaired the worktree, and the task went on in a fresh Claude session with
  the conversation handed over.

### Slash commands like the agents have, and /mode

Asked: the / commands Claude Code and opencode have but Relay lacked (such as
expanding thinking, and effort variants), and `/mode build` instead of
`/build`, so new modes need no new commands.

- The composer has one command system: Relay's commands run at once (with a
  menu of choices where there are any, e.g. `/mode `, `/model `, `/effort `,
  `/rewind `); the agent's own commands and anything unknown go to the agent
  as a message. `/plan`, `/build` and friends became `/mode <name>`.
- Relay's: `/mode`, `/model`, `/effort`, `/thinking`, `/details`, `/new`,
  `/fork`, `/rewind`, `/retry`, `/stop`, `/rename`, `/export`, `/usage`,
  `/help`.
- Effort per conversation (`effort` column, per run too): Claude Code
  `--effort` (low … max); opencode the model's variants (`reasoningEffort`,
  set with the session's model; a model without that variant gets its
  default); chat APIs `reasoning_effort` (xhigh and max become high);
  command agents `RELAY_EFFORT`. The model picker shows it ("Sonnet · high").
- The agent's own commands (`GET /api/conversations/{id}/commands`):
  Claude Code's built-ins that work in `-p` (tested: /init, /review,
  /security-review, /compact, /context) plus `.claude/commands/*.md`;
  opencode's from `/api/command`, run through `/session/{id}/command`. A
  slash command is sent first thing in the prompt: no handover or notes
  before it.
- Claude Code answers some commands (/context) only in its final result,
  without streaming; that result is now shown when nothing was streamed.
- Checked for real: Claude Code (Haiku) with effort high, a project command
  and /context; opencode (a free Zen model) with effort low (the session's
  variant became low) and its /review.

### Claude Code's thinking was gone

Reported: no "Thoughts" blocks any more. Stored runs showed it: Claude Code
runs (Sonnet, Opus) had no reasoning events at all, opencode's did. The raw
stream had thinking blocks with empty text and only a signature: newer Claude
models leave their thinking out unless asked. Claude Code has a hidden
`--thinking-display summarized` flag (the Agent SDK passes it; the
`showThinkingSummaries` setting does the same in interactive use). Relay now
passes it, with a `thinking_display` provider option to turn it off. Checked:
Sonnet and Opus with effort high sent their thinking summaries again.

### The provider interface, without exceptions

The core now only knows providers through `relay/providers/base.py`:

- `Provider.detail()` gives the note in the providers list (opencode: which
  profile); the core no longer checks `kind == "opencode"` for it.
- `Provider.PROGRAMS` names what a provider runs; `relay portable` bundles
  what the providers declare instead of a hard-coded claude and opencode.
- `ProviderRun.waits_for_answers()` and `answer_permission()` are part of a
  run's interface (with defaults); the core no longer probes with `hasattr`.

What's left by name is `relay claude`, a helper for logging in to Claude Code.

## Plan: Relay's tools overhaul (agreed 2026-09-29)

Asked: documentation per tool, standard agentic tools for providers with
little more than a response stream, and a folder where the user adds their
own. Agreed: the Agent Skills format (`SKILL.md` with a name and description,
plus scripts) instead of `<TOOL>.readme`, and a tool loop for chat APIs.

1. **Skills**: built-in ones ship with Relay, the user's own live in
   `data/relay/skills/<name>/SKILL.md` (so they travel along). Claude Code
   gets them as real skills (a folder with `.claude/skills`, symlinked, passed
   with `--add-dir`); agents without that get a list of names, descriptions
   and `SKILL.md` paths in Relay's note. Nothing is written into opencode's
   configuration. The subagent tools become the first built-in skill.
2. **Settings → Skills** (path, view and edit, new skill) and `relay skills`.
3. **A tool loop for chat APIs** (OpenAI-style function calling): web pages,
   skills, subagents, and in code tasks the standard coding tools (list,
   read, search, write, edit, run a command), within the project folder, with
   modes and permission prompts. Servers without function calling fall back
   to plain chat.

### Done (2026-09-29)

- Skills (1, 2): `relay/skills.py`, two built-in skills (subagents, writing a
  skill), Settings → Skills, `relay skills`.
- The tool loop (3): `relay/toolbox.py` holds the tools, `providers/chat.py`
  loops over them (at most 40 rounds). Options `tools` and `auto_approve`.
  Chat providers now count as having tools, so they get the Relay tools
  token and can delegate. Requests carry a `User-Agent: Relay/<version>`
  (Cloudflare refused Python's). Errors from delegate name the providers
  and modes there are, so a model that guessed wrong can correct itself.
- Tried for real on Ollama: qwen2.5 3B only listed skills and made up a
  provider; Qwen3.8 27B found and fixed a failing test (list, read, edit,
  run pytest) and delegated a question to Claude Haiku (agents → delegate →
  wait). The code turn was committed as usual.


## Requests of 2026-09-29 (evening)

Six requests, worked through in this order:

1. **Modes Fast and Brainstorm, and /code-review.** Built-in modes; your own
   mode with the same id replaces one (not Build and Plan). Skills work as
   commands: `/code-review [what]` sends the skill's instructions with the
   request to any agent (an agent's own command of that name wins). New
   built-in skill `code-review`.
2. **Which models work, and a test.** Every run records whether its
   provider/model worked; a test sends a tiny prompt. Shown in the model
   picker and under Settings → Providers.
3. **Subagents that cost money.** A list of subagents allowed without asking;
   others ask the user (with a time-out) and suggest an allowed one instead.
4. **Chat → code task.** Move a chat into a project folder and go on there.
5. **Network drive.** Share a project (or a task's worktree) over WebDAV so
   the user can mount it on their PC and test what Relay wrote.
6. **Your own computer.** A small Python script, downloaded from Relay, that
   connects to Relay and runs what agents ask on that computer while it runs.

### Done: 1

### Done: 2

`relay/health.py`: table `model_status` (provider, model, ok, message,
seconds, checked, source run/test), written after every run (a stopped run
says nothing) and by tests (`POST /api/providers/{id}/test`; a tiny prompt
in a scratch folder, 120 s at most). A model is keyed by what the chat
chose, so the default model is its own row. Menus got a max height (long
OpenRouter lists ran off the screen) and status dots.

### Done: 3

`relay/policy.py` with the rules per user (new `preferences` table, JSON);
`Provider.billing(model)` for what a provider knows before a run (chat
APIs: local or `:free` is free, else API), otherwise the model's last runs
decide. Permission requests can now carry `options` (ids, labels,
outcomes) and `expires`; `RunManager.answer` takes the chosen id and
`withdraw` closes one nobody answered. The ask waits at most 90 s: the
`relay` script's shell call must end well within a shell tool's two
minutes. Found on the way: `load()` handed out the default's own list, so
"always allow" changed the defaults for everyone; it returns a copy now.

### Done: 4

`POST /api/conversations/{id}/to-code` sets the conversation up as a code
task like a new one (git branch or not, checkpoints outside git) and keeps
the provider session: providers already cope with a session in another
folder (opencode moves it, Claude Code starts afresh with a handover). The
chat's scratch folder is removed only when empty; otherwise its path is
reported, since the chat's agent may have made files there.

### Done: 5

`relay/webdav.py`: a small WebDAV server (class 1 and 2, locks granted but
not enforced) under `/dav/<token>/`, one share per folder in a `shares`
table; `OPTIONS`/`PROPFIND` on `/` and `/dav/` for Windows. Tested with
rclone (copy, move, purge), cadaver (put, mkcol, copy, move, lock, get, rm)
and the requests Windows sends. Mounting it here wasn't possible: the
container has no CAP_SYS_ADMIN, so neither davfs2 nor FUSE (rclone mount)
can mount; to be tried on a real computer.

### Done: 6

`relay/computers.py` (table `computers`, jobs in memory, long poll of 25 s
at `/computer/<token>/poll`, results at `/computer/<token>/result`) and
`relay/connector.py`, the script handed out with Relay's address and the
computer's token filled in (stdlib only; run, read, write; `--ask`). Agents
reach it through the Relay tools (`relay computers/run/pull/push`,
JSON-RPC `computer_*`), chat models through the toolbox when the user has
computers. Per computer `ask` (default on) puts each command or file change
to the user, with "everything on this computer for this answer".

Tried for real, with a connector here: Claude Code (Haiku) found the OS and
user and wrote a file; Qwen3.8 27B (Ollama) did the same with
`computers` + `computer_write_file`. That showed a bug: `relay run -- bash
-c '…'` lost its quoting (words joined with spaces) and the agent's own
shell expanded `~` locally. Now separate words go as `argv` and Relay
quotes them for the computer's shell (cmd or POSIX), and the note and
skill say to quote the whole command.

## Requests of 2026-09-30

1. **Never `rm -rf /`.** `relay/guard.py` (standard library only) judges a
   shell command: removers aimed at `/`, `/*`, home folders, system folders or
   a possibly-empty variable (`$S/*`; `${S:?}` is fine), disk formatting and
   overwriting, fork bombs, recursive chmod/chown of `/`, Windows drive wipes.
   Applied in Claude Code (a PreToolUse hook via `--settings`, checked for
   real: Claude got "Relay blocked this command"), opencode (its shell
   permission requests, rejected before auto-approval), the toolbox's
   run_command and commands for the user's computers.
2. Usage limits: show when they reset; code tasks resume by themselves.
3. Scheduled tasks.
4. GitHub Copilot CLI as a provider.
5. Hard-coded values configurable under Settings.
6. Self-repair of a provider's support layer by another agent.
7. A thorough code review, and fixing what it finds.

### Done: 1

### Done: 2

`relay/limits.py` recognises limits and reset times (epochs, ISO times,
durations like "1h2m3s", clock times with a time zone, 429 headers).
Claude Code's `rate_limit_event` (seen in a real run: status, resetsAt,
utilization per window) becomes a uniform `limit` event; chat APIs emit
one on HTTP 429. `RunManager._after_limit` stores `limit_info` and, for code
tasks, `resume_at`; `relay/scheduler.py` (a loop in the app's lifespan,
every 20 s) starts the resume as an ordinary run. model_status got
`resets_at` and `usage` (the windows), shown in the model's status.

### Done: 3

`relay/schedules.py` (table `schedules`, `when` as JSON: once, daily,
weekdays, weekly, interval; wall-clock times in the schedule's time zone
via zoneinfo, so daylight saving is right) and `Scheduler.run_schedule`
(a new conversation through `open_conversation`, factored out of the
create endpoint, or on in the same one, queued when busy). UI: a
Scheduled page and `/schedule`. The UI check first ran its schedule on the
real Claude Code (the form's default provider was the first one); the form
now starts from the draft's provider and the check picks the fake one.

### Done: 4

Copilot's CLI 1.0.89 has `-p --output-format json`, but only `--acp` lets a
client answer its permission requests (hooks aren't there), so Relay speaks
ACP: `relay/providers/acp.py` with a generic `acp` kind and `copilot` on top.
Found out against a local fake model (Copilot's BYOK variables, offline, no
login needed): session/new returns modes (agent, plan, autopilot),
session/load replays the history as updates (skipped), permission requests
carry `rawInput.commands` (checked by the guard), the prompt result has
token usage. Tried through Relay: a tool call, its permission, the answer,
and a second turn that continued the session. `relay portable` now finds
the native program behind an npm launcher script.

### Done: 5

Found the fixed values (grep for module constants, plus the texts Relay
sends by itself) and made the ones worth changing tunables
(`relay/tunables.py`: key, group, label, help, default, bounds, unit;
values in `settings.json` `"limits"`, applied at once through `get()` at
the point of use). Subagent slots became a condition instead of a
semaphore, so a new number applies without a restart. Left fixed on
purpose: the guard, path and size checks that protect the machine,
protocol timeouts and test hooks.

### Done: 6

First split `providers/agents.py` into `claude.py`, `opencode.py` and
`bridge_run.py`, so each provider's support layer is its own files
(`Provider.SUPPORT_FILES`). `relay/repair.py`: a repair folder (git) with
copies of those files, reference copies (read-only), REPAIR.md and
check.py; a read-only copy plus fingerprint of Relay's own source; review
(scope diff, restore of anything else, compile, shadow test in a temporary
copy of Relay against the real provider template), apply with backups,
undo, discard. `POST /api/restart` re-executes Relay (`sys.orig_argv`) when
no agent is working. The test runs against a copy of the source, with a
helper that also writes outside its folder (put back). The UI check only
starts, checks and discards (Apply would change this repository).

### Done: 7 (code review)

Read through the web app, runs, auth, delegation, the new modules of these
two days and the front end's new parts. Fixed:

- **Security**: files served from a network drive ran with Relay's origin:
  an HTML file in a shared project could load a script from the same share
  and use the user's session. `/dav/` responses now get a sandbox CSP.
- A code task whose folder was gone worked in a new, empty folder; it says
  so now. Scheduled code tasks check their folder when saved. Switching
  provider drops a resume planned for the old one. Tunables were applied as
  a side effect of constructing Settings; the app applies them now. The
  repair folder's commit skips hooks and signing, like Relay's others.
- **Design**: `app.py` (1600 lines, every handler) is split into
  `relay/routes/` (accounts, settings, providers, conversations, runs,
  extras) and `relay/web.py` (errors, request helpers, summaries); `app.py`
  keeps the Relay object, the routes and the middleware. Done mechanically
  (definitions moved as they were, imports from pyflakes, isort), checked
  by the tests and the UI check.
- Checked and fine: Relay's commits already skip hooks, attachments are only
  inline for plain images, markdown goes through DOMPurify, the login
  throttle, the CSRF header on every changing API call.

Known and left: the guard is a last line of defence, not a sandbox (a
command built at run time, `$(…)`, gets past it); `command` providers run
their own tools out of Relay's sight.

## Code review of 2026-09-30

Four rounds, each tested (355 tests, the UI check):

1. **Hanging commands**: a command that left something running (a server,
   `sleep &`) kept its output open, so the chat models' run_command and a
   command on the user's computer waited forever despite the time-out.
   Both now run commands in their own process group and end it as a whole.
   **Read-only subagents**: an agent in Plan could change files through a
   subagent in Build. Tool calls numbered anew each round (some models)
   collided. Unignored `node_modules` were read in full for the change
   list; huge new files were diffed in memory. Checkpoints kept files of any
   size. Provider event streams weren't closed after a failure. WebDAV let
   a folder be moved into itself. Tool errors became HTTP 500s.
2. **Repair restores** put back every change to Relay's code since a repair
   started (an update or your own work too); now only what changed while
   the repairing agent worked. ACP agents' open permission requests are
   answered as cancelled on a stop. The conversation list did a query per
   conversation. The Computers tab kept polling after leaving it.
3. **Moving Relay** didn't rewrite the paths of network drives, scheduled
   tasks and repairs. A reset time in the past made a code task resume every
   20 seconds; now 5 minutes apart at least, and it gives up after 5.
   "line 429" counted as a usage limit.
4. **Design**: the `relay` script and the note to agents moved out of
   `delegation.py` into `tools_script.py`.

## Computer tasks (2026-09-30)

Asked: "Computer" as its own item under New code task, if that's a good
idea. It is, when it's more than a shortcut: in a plain chat the agent has
to guess which computer "my laptop" is and where the work happens. A
computer task is a chat with `computer_id`: the agent's note (and a chat
model's guidance) says the work happens on that computer, `relay run`/`pull`/
`push` and the computer_* tools default to it, and the header, sidebar and
composer show it with its online status. Forks and `/new` keep it. Tested
with a connector and an agent that runs `relay run` without `--on` while two
computers exist.

## Fix: a withdrawn permission request ended the run (2026-09-30)

Seen in a Brainstorm chat on opencode: the agent ran `cat SKILL.md` and
`relay agents` at once. Read-only modes refused every shell command, so
Relay rejected the `cat`; opencode then withdrew the other pending request,
and Relay's approval of `relay agents` got a 404 (PermissionNotFoundError)
that ended the run. Now a withdrawn request is noted ("withdrawn by
opencode") instead of failing the run, and read-only modes let through
commands that only read (`guard.reads_only`: a conservative list, no
redirection or command substitution; git, sed and find only in their reading
forms), for opencode and ACP agents alike.

## A review by Claude Opus in a Relay chat (2026-09-30)

Opus, asked in Relay what it thought of the environment, found three things
(from reading the code); all three were right:

1. **Chat folders inherited Relay's own repository**: they sit in
   `data/relay/chats/<id>`, inside Relay's git checkout, so git in a chat
   was git on Relay's code (checked: `git rev-parse --show-toplevel` in a
   chat folder gave the checkout). Opus preferred `GIT_CEILING_DIRECTORIES`
   in the agent's environment, but opencode runs as one shared service per
   profile, so a per-chat environment doesn't reach its shells; instead a
   chat folder inside another repository gets an empty one of its own, in
   the core (`runs._directory`), for every provider alike.
2. **Brainstorm** started three subagents for anything, a greeting too; its
   instructions now start by saying what it's for.
3. **Claude Code's plan mode** expects a terminal (ExitPlanMode,
   AskUserQuestion); Claude Code's support layer now tells it it runs
   headless. Opus also saw a previous mode's instructions linger after a
   switch; whatever `--resume` does with them, a session that goes on in
   another mode is now told the old mode's instructions no longer apply
   (`Context.mode_note`, for every agent, since opencode has earlier mode
   instructions in its history too).

On the way, `suggest_title` became part of the provider interface (with a
default) instead of a `hasattr` check in the core. The user asked to keep
the layering: a support layer per provider behind the standardised,
provider-agnostic API; all of the above follows it.

## Opus's second look (2026-09-30)

- **Tests wrote into the real ~/.cache**: 2575 tools-script folders (27 MB),
  all but 6 left by test runs (the API tests didn't set XDG_CACHE_HOME, nor
  did the UI check's Relay). `tests/conftest.py` gives every test a cache of
  its own, the UI check too, and Relay prunes at start-up what isn't its own
  and wasn't used for 7 days (tools scripts) or 30 (skill folders); the
  cache may be shared with another Relay, hence the age. The leftovers here
  were removed once by hand.
- **opencode after a chat folder got its own repository**: checked for real
  with the free Zen model, turn 1 inside the outer repository, turn 2 after:
  the session went on and remembered.
- **Claude Code keeps the system prompt a session started with**: checked
  with Haiku (`--append-system-prompt` PINEAPPLE, then MANGO on `--resume`:
  it answered PINEAPPLE). So a mode switched to, a mode's edited text, or
  Relay's changed note never reached a resumed Claude Code session. The core
  now counts the mode's instructions in what a session has been told
  (`tools_told_path`), and Claude Code's support layer sends what changed
  with the message when it resumes or forks.

## Permissions: fewer needless refusals, and /permissions (2026-09-30)

Seen in a Brainstorm chat on Claude Code: three attempts to start subagents
were refused. Brainstorm is read-only, and Relay ran Claude Code then in its
default permission mode without a way to ask anyone, so whatever needed
permission was refused unasked; `Bash(<relay script>:*)` doesn't match once
the agent puts the script in a variable, a pipe or a heredoc; and Claude
Code's own checks ("expansion obfuscation") ask too.

Now Claude Code in a read-only mode (or with *ask*) sends its permission
prompts to Relay, and Relay answers what it can before asking the user:
`Provider.judge()` (default: ask) says which of a provider's tools only look;
Claude Code's support layer knows its tools, allows Relay's own script
anywhere in a command and commands that only read, refuses file changes in
a read-only mode and the guard's commands, and asks for the rest. opencode
and ACP agents ask the user for a command in a read-only mode that may change
something, instead of refusing it. `/permissions auto|ask|default` sets it
per conversation (`approve_mode`, `Context.approve`,
`Provider.auto_approve(context)`), over the provider's option.

## Plan: hand-picked requests of 2026-09-30 (afternoon)

Twelve requests, grouped into five pieces of work that fit together, each
kept as simple as it can be. Order of work: A, B, C, D, E.

**A. Permissions and read-only modes** (bypass-permissions, plan mode that
can look, usability over security)
- A read-only mode (Plan, Brainstorm, your own) means one thing for every
  provider: look freely (read, search, commands that only read), changes to
  files are refused, any other command is put to the user. Relay enforces it
  through permission requests; the providers' own plan modes (Claude Code's
  plan permission mode, opencode's plan agent) are no longer used for it,
  since they forbid even reading commands, and their instructions clashed.
  Chat models get run_command in read-only modes too, under the same rule.
- Plan's and Brainstorm's instructions say what is allowed, instead of
  leaving the model to guess.
- `/permissions bypass`: approve everything, read-only modes included (only
  the guard's commands stay refused).
- Usability over security (standing rule): computers don't ask before each
  command by default, sign-ins last a year, chat models' file tools may use
  paths outside the project.

**B. Keep working when a model stops** (continue at XX:XX by default, 5 hours
when the reset is unknown, `/fallback-agents`, continue with another model)
- One flow for a usage limit (or a provider that gives no answer at all):
  1. fallback agents set: go on right away with the first one that isn't
     limited itself (the conversation switches provider/model and is handed
     over), and say so;
  2. otherwise: resume when the limit resets, by default for every
     conversation (opt-out: Cancel); unknown reset time: in 5 hours, once;
  3. give up after that, saying so.
- The banner gets **Continue with another model**: pick one, it goes on now.
- `/fallback-agents provider/model, …` sets the list (per user); Settings →
  Providers shows and edits it.

**C. Relay's tools as real tools** (from ChatGPT's review: one happy path,
no shell quoting, delegation as one call)
- Claude Code and ACP agents get Relay's tools as MCP tools (agents,
  delegate, wait, computers, computer_run, …) from the MCP endpoint Relay
  already has; the shell script stays for agents without per-run MCP
  (opencode, command agents). The note tells each agent only its own route,
  and when to use its own subagents versus Relay's.
- `delegate` can wait for the answer itself (`wait: true` / `--wait`), so a
  simple delegation is one call.
- Not taken over, to keep Relay simple: skill-routing metadata, path URIs
  (workspace://…), risk classes beyond read/ask/refuse, a routing trace, an
  evaluation set against real models.

**D. Interface**
- Effort as a submenu when hovering (or tapping ›) a model in the picker.
- Mode icons: Fast and Brainstorm get their own, and every mode can have an
  icon picked in Settings → Modes.
- Reply: select text in an answer, click Reply, it's quoted in the message.

**E. Line counts and commits without installed software**
- Dependency and build folders (node_modules, virtual environments, caches,
  build output…) count neither in a task's changes nor in Relay's commits,
  also when a project's .gitignore forgets them.

**The mode-switch bug** couldn't be reproduced (picker, API and the next
turn all switch, with a test agent and with opencode); most likely it was
the resumed Claude Code session keeping its old system prompt, fixed this
morning. A's rework of read-only modes touches the same path.

### Done: A (permissions and read-only modes), B (keep working when a model stops)

B: `relay/fallback.py` (the list per user, `choose()` skips the one that
stopped, those tried for this stop, turned-off ones, and ones limited
themselves) and `RunManager._after_stop` (was `_after_limit`): a usage limit
or an answer that failed without any text, thinking or tool call counts as
"stopped"; fallback now, else resume at the reset (every conversation), else
once after `limits.unknown_wait` hours, else give up. `POST
/conversations/{id}/continue` hands over to another model now.

### Done: C (Relay's tools as real tools)

`Provider.RELAY_TOOLS_VIA_MCP` (Claude Code: yes): Relay's MCP endpoint lists
agents/delegate/wait (and the computer tools when there are computers) next
to approve_permission, a subagent's token only the latter; the note then
names only that route (`note(via_mcp=True)`), and says when to use the
agent's own subagents instead. `delegate` takes `wait` (MCP, chat tools,
`relay delegate --wait`). Checked for real: Claude Code (Haiku) found
`mcp__relay__delegate`, called it with wait and got the answer in one call.
ACP agents stay on the script: whether one takes HTTP MCP servers is only
known once it runs, after the note is written, and two routes would bring
back the ambiguity.

### Done: D (interface)

- `showMenu` items take a `submenu` (a function returning items, or a
  promise of them): it opens beside the item after hovering 150 ms, or with
  the › button on a touch screen; only the last one asked for opens, and a ›
  whose submenu turns out empty disappears. The model picker uses it for
  efforts (cached per provider/model in the page); `onChange(model, effort)`
  sets both at once.
- Mode icons: `Mode.icon` (built-in: hammer, map, bolt, bulb; own modes by
  access until one is picked). The choice is a preference per user
  (`mode_icons`: mode id → icon), so the fixed built-in modes can have one
  too without being replaced; removing an own mode drops its icon. `PUT
  /api/modes/{id}/icon`; a grid of icons (`MODE_ICONS`) in Settings → Modes.
- Reply: one floating button for the page, shown on `selectionchange` when
  the selection is inside an answer of the open chat; the composer's
  `quote()` puts it under what's typed.

### Done: E (installed software out of commits and counts)

`git.DEPENDENCIES` (gitignore patterns) plus any folder with a `pyvenv.cfg`
go to `ls-files --others --exclude=…`, so they only affect files git doesn't
track yet: `git.untracked()` for the change list, `git.add_all()` (the
changed tracked files and those new ones, as literal pathspecs from a file)
instead of `git add -A` for turn commits, fork snapshots and checkpoints.
Checkpoints' own exclude list is DEPENDENCIES plus build output. A task
that already committed a node_modules before this still shows it: merging
would bring it along, so hiding it would be wrong.

### Tidying: main.js split

`main.js` (2100 lines) is split along its sections into `state.js` (state,
dom, draft, loaded data; imports nothing of the app), `pickers.js`,
`commands.js`, `composer.js` and `conversation.js`; `main.js` keeps boot,
layout, sidebar, routing, header and new chats (600 lines). The modules
still call each other's functions (navigate, renderHeader, …), which ES
modules allow as long as it happens after loading; only function
declarations are used at load time. `openRequest` stays private to
conversation.js (`forgetOpening()`), since an imported binding can't be
assigned. `clock` and `debounce` moved to ui.js. Unused imports and locals
in the tests are gone.

## Findings from the first real use (2026-09-30, evening)

A Fast-mode run on a local Ollama model delegating chapters of a website:

- **Tab completion** for / commands: the composer completes a command's
  name, then its choice, as far as it's certain (like a shell).
- **Edit showed two messages**: editing the last message without changing
  it counted as "retry as is" (a new turn for agents), and the editor stayed
  up. Any edit (text given) now takes the message's place.
- **A free opencode model was "Cost: not known"**: opencode told its
  billing only after a run. `OpencodeBackend.models_info()` reads every
  model's billing and server address in one go; `OpencodeProvider.billing()`,
  `free_models()` (new hook; `policy.allowed` lists them instead of
  "any model" when the default is free) and `local_server()` use it, cached
  five minutes.
- **The agent didn't hear a subagent failed** (in 0.8 s) until `wait`:
  `delegate` now waits up to `EARLY` (8 s) for a sign of work (text,
  thinking, a tool call) and reports a failure at once; a later failure
  comes along with the next result of any Relay tool (`_news`); `agents`
  lists models whose last run failed (24 h); failure answers say to pick
  another model.
- **"Use … instead" wasn't told**: the agent got "Started subagent" on
  another model without knowing why. `_permitted` now returns a note.
- **Ask timeout 10 s**, and no answer means the allowed alternative (free,
  say) runs instead, else refused. Computers got their own
  `computers.ask_timeout` (90 s): a command there is another matter.
- **Effort for subagents**: `effort` on delegate; in Fast mode the model's
  lowest by default.
- **One local model at a time**: `Provider.local_server(model)` (chat: its
  base URL's host if local; opencode: its provider's baseURL);
  `subagents.local_models` (1) different models may work per server among
  active runs and pending subagents; a subagent beyond that is refused with
  which model to use. Queueing instead would deadlock: the calling agent
  holds its model while it waits for the subagent.
- **Title "<tool_call>"**: markup-only titles are ignored.

### Open files instead of Work in place

The "Separate branch / Work in place" toggle confused more than it helped.
It's gone from the new task's message box, `/code` and scheduled tasks; a
task always gets its own branch in a git repository (`isolate` stays in the
API). In its place, a code task's message box has **Open files**: it shares
the folder the agent works in (the existing WebDAV share; the address is the
key, no password) and opens it (`dav://` on Linux) or copies it in the form
the system's file manager takes (Windows: a `\\host@port\DavWWWRoot\…`
path for Explorer's address bar; macOS: the http address for ⌘K). Sharing
again no longer resets a share's read-only. The header shows the project
folder instead of the worktree's.

### A white page in Firefox after the main.js split

Static files went out without Cache-Control, so Firefox reused cached
modules by its own heuristics: a new main.js with an old ui.js fails on an
import, and the app never starts (the boot spinner stays). Everything but
/api/ (no-store) and /dav/ now gets `no-cache`: the browser asks each time
and mostly gets a 304. The UI walkthrough also passes in Firefox now (its
test PNG was broken; Chromium had tolerated that).

### In place by default

A task in `website-AI`, a new folder in the repository `~/projects`, found
its folder gone: a worktree starts from the last commit, which hadn't it (nor
would it have uncommitted changes). Code tasks now work in the folder picked
by default (the user's suggestion; with Open files the files are at hand
anyway). `isolate: true` in the API still gives a branch, but only when the
folder is in the base commit without changes since (`git.clean_in_commit`);
else in place too.

## Fix round (2026-09-30, evening)

- **In-place tasks stayed in "Needs review" forever**: nothing to merge,
  and no other way out, which mattered once in place became the default.
  *Mark as reviewed* (`POST …/reviewed`, `tasks.mark_reviewed`) commits
  edits made meanwhile and moves the base to HEAD, for every open task in
  the same place (the overview shows shared changes once, so an older one
  would pop up with them otherwise).
- **No size limit on commits**: `git.untracked()` leaves out new files over
  `MAX_NEW_FILE` (50 MB; checkpoints use the same), so a downloaded model
  doesn't end up in the user's history for good.
- **"Continue with another model" gave a 500** on a stale banner (the note
  of what stopped was cleared by an answer that worked meanwhile).
- **Relay's own tools let other commands through**: Claude's judge,
  opencode and ACP allowed any command that mentioned the tools path, so
  `relay agents; rm x` passed in a read-only mode. `guard.only_runs()` lets
  through the script (also via a variable, with its heredoc) plus cd and
  commands that only read, nothing else; opencode gets it as
  `own_commands`, judged over the parts it split the command into.

### Subagents, from real use

- **Their permission prompts were hard to see** (only in the card, opened)
  and waited forever. `RunManager.ask` mirrors a subagent's request (and its
  answer) into the caller's run (`run`, `subagent` on the event: answered
  through the subagent's run), `waiting_for_answer` counts subagents, and a
  request without its own expiry gets `subagents.permission_timeout` (60 s),
  then counts as no.
- **Bypass didn't reach subagents**: they get the chat's `approve_mode`.
- **Plan subagents**: a read-only caller's subagents got Plan, whose job
  (make a plan) doesn't fit a subtask. A built-in **Read-only** mode (no
  instructions, only the read-only rules) is what they get now; not
  replaceable, like Build and Plan.
- **`ls … 2>/dev/null || echo …` wasn't reading**: redirections to
  /dev/null and `2>&1` write nothing (`guard.QUIET`).
- **A chat went blank after coming back from a subagent's chat** until F5:
  subagent cards opened an EventSource each and never closed it; with the
  browser's six connections per server taken, the chat's own stream waited.
  Cards close theirs when closed and when the chat is left.
- **One stream error ended a subagent**: `_supervise` gives it one more try
  in the same conversation ("go on where you left off"), not on a usage
  limit.
- **Talking to subagents**: `tell ID message` (MCP, script, chat tools)
  interrupts a working one (`stop_and_wait`) and starts it again with the
  message, or follows up on a finished one; `wait` collects the new answer.
- **A menu choice could get lost**: a toast appearing over an open menu
  took the click, which closed the menu as a click outside it. Toasts now
  sit under menus (and still over dialogs). This was the UI check's
  occasional failure at the usage-limit step.

## Live test: a real project (click, 12.8k lines), free and local models

A second Relay (own data folder, port 8431) driven through the browser like
a user: a Plan-mode question, a feature with tests in Build mode, Fast mode
with three subagents writing tests at once, the Changes panel and tasks
overview, a phone-sized screen, Brainstorm on the local Ollama model.
Relay held up; what it showed wasn't always good:

- **Tool rows showed absolute paths**, cut off before the part that
  matters: paths in the chat's folder show relative to it
  (`setProjectFolder`), a leading `cd <project> &&` is dropped, and the
  tools script called through a variable (`R=…/relay; $R wait`) shows as
  `relay wait`. Searches show their pattern (and folder when it isn't the
  project), not just `.`.
- **Wide tables were cut off** with no sign they scroll: a table sits in a
  scrolling `.table-wrap` (cells wrap first) that fades out at the right
  while more is hidden.
- **The folder picker** opened on the list of workspace roots even with
  just one: it opens in it.
- **The note under the message box** spelled out the full path (four lines
  on a phone): shortened, full path on hover.
- **An opencode run failed "without an error message"**: opencode gives
  the reason only with `session.execution.failed` ("Model unavailable: …"),
  which the translator dropped.
- Worked as meant: read-only commands in Plan asked nothing; the turn was
  one commit; a subagent whose free model was unavailable failed twice
  (second try) within 2 s, the agent heard at once and started another on a
  different free model; 2373 tests passed at the end.
- **Brainstorm on the local model asked for every relay call**: the model
  put the script's path in a variable in one command (`R=…/relay`) and used
  `"$R" wait` in later ones; opencode's shell keeps it, Relay didn't know.
  `guard.only_runs(…, known)` remembers, per run, variables set to the
  script or its folder (and forgets one set to anything else); opencode
  and ACP keep one such dict per run.
- Worked as meant: a subagent's question showed in its caller's answer and
  was denied after a minute without an answer; the agent used `tell` by
  itself to hurry a subagent; a queued message went next; the Critic on the
  caller's own local model was allowed, one local model at a time.
- **`$( … )` was never reading**: the local model's `dirname $(ls …)` was
  put to the user over and over. A substitution (`$(…)`, backticks, `<(…)`)
  whose command only reads counts as reading now (`guard._substituted`,
  up to three levels deep); `cat $(rm x)` still doesn't.

## Live test with Claude Code and ChatGPT; Relay reviewing itself

Claude Code (Sonnet) in Plan then Build with `/permissions ask`, handing the
tests to a Haiku subagent through Relay's MCP `delegate` with `wait`: it
worked, the subagent's questions showed in the caller's answer with their
countdown. ChatGPT (gpt-5.6-sol, through opencode) reviewed a copy of Relay
with a Sonnet and a gpt-6-astra subagent; its verified findings, fixed:

- a fork lost `/permissions` (approve_mode wasn't copied);
- a computer task moved to a code task kept its computer (and its session,
  told the work happens there);
- two quick Enters sent a message twice (the composer now sends one at a time);
- a fork of a task without git pointed at its original's checkpoint, which
  the fork's own checkpoint repository hadn't (`checkpoints.copy`);
- a queued message was dropped before it was sent, lost when sending failed;
- `delegate` with wait returned at once after a substitution note
  (`re.match` → `re.search`);
- exporting a chat with a non-Latin title gave a 500 (headers are Latin-1:
  an ASCII name plus `filename*=UTF-8''…`);
- a setting the server refused still said "done": `updateConversation`
  returns whether it saved.

Also from this test: Claude's usage showed "6 in" (cache reads and writes
are counted apart; now counted as input everywhere, opencode's `cache` too),
and opencode's split permission requests leave out an assignment line
(`RELAY=…` then `$RELAY delegate`), so the backend now reads each shell
command whole as it's called (`saw_command`).

### Second review by ChatGPT (Sol with Terra subagents): fixed

- the command guard: `rm -rf /etc/../home/alice`, `/./`, `/usr/../*` are
  normalised first; `Remove-Item … -Recurse` after the folder is caught too;
- "readers" that write: `sort -o`, `tree -o`, `uniq IN OUT`, `xxd IN OUT`,
  `xxd -r`, a sed script's `w`/`W`/`e`, `date -s`, `hostname NAME`;
- an unquoted heredoc's `$(…)` runs: not "just Relay's tools" then;
- WebDAV: a symlink to `.git` exposed it; MOVE/COPY onto a folder the
  source is in deleted that folder first; paths were decoded twice (a file
  named `a%20b.txt` meant `a b.txt`);
- chat APIs: an error in the stream, or running out of tool rounds, ended
  as "succeeded" (and dropped the usage so far);
- `/permissions ask` put harmless reading commands to the user (opencode, ACP);
- Relay's commits disable hooks altogether (`core.hooksPath=/dev/null`),
  as `--no-verify` leaves prepare-commit-msg and post-commit.

Done the day after, with the rest: a schedule going on in its chat applies
its own provider, model, mode and effort (the chat takes them, as when the
user picks them); a bare `RateLimit-Reset` is seconds; renaming a schedule
keeps its next run; a Settings tab or model list still loading can't
overwrite a newer one; a repository without commits counts changes from the
empty tree; opencode's model info is cached per profile.

**Agents left working by a crash**: opencode's server outlives Relay, and a
session it was prompted in goes on (using a usage limit) when Relay is
killed. Runs now note their session as soon as it's known
(`runs.session`); at the next start `fail_unfinished_runs` returns them and
`Provider.abandon(session, directory)` stops each (opencode: interrupt; the
others were Relay's child processes and ended with it).

Lesson from this test: two review rounds with five or so ChatGPT agents
each, all reading most of the codebase, took ChatGPT from 81% to 8% of its
limit in minutes. Subagents multiply cost; the agents also started
opencode's own subagents on top.

## Night of 1 October: live tests without Claude

The user asked not to run Claude models in Relay while it's being built
(same account as the developing session); free Zen models and ChatGPT Sol/
Terra instead.

- **opencode's default profile never asked**: without an opencode.json it
  allows everything, so Plan, Brainstorm and `/permissions ask` were
  silently bypassed there (a Plan subagent ran pytest unasked). When Relay
  decides, the session now gets its own rules (`ASK_RELAY`: shell, edit,
  write, patch → ask; `PATCH /api/session/{id}` `permissions`); in auto
  mode the profile's config is left as it is.
- **The chip under each answer showed the task's total**, so a question
  that changed nothing said "1 file changed". It shows what that answer
  changed (since its checkpoint); the header keeps the total.
- **Reading Relay's own skills was asked** (opencode's external_directory):
  the skill folders are always allowed, like the tools folder.
- Worked: a second turn went on in the same opencode session; a fork got
  its own session with the history; an edit with its files rewound; the
  agent used `tell` by itself (message on stdin); a subagent's question
  showed with its caller.

### Review by ChatGPT (Sol, one Terra and one Zen subagent), the long run

Kept small this time: two subagents, no opencode task agents; 23 minutes.
Verified and fixed:

- a chat API stream that ended before `[DONE]` or a finish_reason counted
  as a complete answer (fallback and retry never started);
- two `delegate` calls at once could both find room on a local server
  (`_placing` lock around the check and the registration);
- Copilot's template skipped the `env` check (a number there broke every run);
- a handover said "[… 0 messages left out …]" when the first message alone
  was too long.

The long run (four tasks through the queue on free Zen models, three
subagents at once in the third, 26 minutes) went through; the agent used
`tell` by itself to restart a stuck subagent. The subagent time limit
wasn't reached because the test's limits change went unnoticed: `PUT
/api/limits` accepted a body without `values`. It now refuses one, and the
time limit has a test of its own.


## Agents work in containers; permissions out (2026-10-01)

The permissions machinery (auto/ask/bypass per provider and per chat,
read-only modes enforced through permission requests, harmless-command
detection, a subagent's questions mirrored to its caller with a timeout)
kept producing needless refusals and questions. It's gone: agents now run
their commands in a podman container of the project folder
(`relay/containers.py`), and Relay answers what's still asked itself.

    provider (host) → support layer (host) → Relay's API (host) → Relay (host) → the agent's commands (container)

- **How.** Every agent runs its commands with `$SHELL -c …` in its folder.
  Relay gives it a shell of its own (a small Python script; Claude Code also
  gets it as `CLAUDE_CODE_SHELL`, verified with Haiku) that checks the guard
  and hands the command to `podman exec -w <cwd>` in the folder's warm
  container (`--userns keep-id`, the project at the same path, the agent's
  home on a volume, Relay's tools/skills/uploads read-only). ~0.1 s a
  command. opencode is one server for many sessions: its service is started
  with a shared shell that finds the container by folder (`routes.json`);
  folders Relay doesn't know run on the machine as before. A service started
  with another shell is started over once.
- **What Relay answers** (`Provider.judge`): commands, anything but what the
  guard refuses; file changes by the agent's own tools only in the project;
  reading anywhere. Claude Code asks through `--permission-prompt-tool`,
  opencode for edit/write/patch (session rules), ACP agents natively; the
  chat toolbox's `run_command` uses the shell and its file changes stay in
  the project.
- **Read-only modes are a request now**, in the mode's instructions; nothing
  holds the agent to them (the user's choice: a prompt works better than
  refusals). The plan was a read-only container for them; dropped.
- **Removed**: `/permissions` and its chip, `approve_mode`, the
  `auto_approve` option (old templates still load), `guard.reads_only` and
  `only_runs`, opencode's own-command and harmless-command detection, the
  subagent permission timeout. Asking stays for the user's own computers
  (per computer), which aren't containers.
- **Found on the way**: opencode's profiles set `XDG_DATA_HOME`, so podman
  run from opencode's shell looked in another store ("no such container"):
  the shell restores Relay's podman environment. Claude Code calls its
  shell as `-c -l <command>`, which the first version read as the command
  `-l` (the guard saw nothing; Claude Code's hook still did). A stopped
  `podman exec` leaves its command running in the container: the shell
  forks a watcher outside the process group that kills what the command
  started (by a tag in its environment) unless it ended normally;
  background processes started on purpose keep running. Claude Code keeps
  its Bash folder in `/tmp/claude-…-cwd`, written inside the container: the
  shell routes that file through a shared folder, so `cd` persists.
  Containers made with other mounts or another image are made anew (a spec
  label). The tests started real containers (one per test conversation);
  they get a fake now, and `tests/test_containers.py` has a fake podman and
  one real-podman test.
- **This workspace's podman**: rootless needed caps on newuidmap/newgidmap,
  subuid ranges inside the workspace's own user namespace, cgroupfs with
  cgroups disabled, the /proc masks unmounted, and `--network=host`; written
  up in RELAY.md, Containers. `relay doctor` checks podman; Relay checks it
  (and pulls the image) in the background at start; without it no agent
  runs, with a message saying how to fix it.
- **Live**: opencode (Zen big-pickle) in Build ran commands in the container
  (Debian bookworm, `HOME=/home/relay`, files owned by the user) and in Plan
  looked and changed nothing; Claude Code (Haiku) ran Bash in the container
  and its Write outside the project was refused with the reason.

### Findings of an agent testing the container environment (same day)

An agent (through opencode) tested the new environment. Fixed:

- **"Is a git repo: yes", but the folder was empty and not a repository**:
  the folder (`projects/sample`) is inside a repository (`projects` is one).
  opencode looked on the machine and was right; the container only had the
  folder, so git above it was gone. The same would have broken every branch
  task: a worktree's `.git` points into the main repository's `.git`. The
  container now gets the repository (its top level) and its common `.git`,
  unless the repository is the home folder or above it (dotfiles).
- **`/tmp/opencode` missing**: opencode tells the model it may use it, and
  made it on the machine; the container's `/tmp` is its own. Shared now.
- **No node, npm, …**: the agent is root in the container now (no
  `--userns keep-id`; rootless, so root is the user outside and files stay
  theirs) and can install what it needs; containers are stopped when unused,
  not removed, so that stays (removed after two weeks stopped). apt drops to
  user `_apt` (65534) for downloads, which this workspace's subuid range
  doesn't map: Relay's containers let it download as root. The agent is told
  where its commands run, that it's root and may install things, in Relay's
  note (and the chat toolbox's guidance).
- **`relay agents` long and repetitive**: providers with the same models
  and efforts (an opencode provider following the active profile and the
  one pinned to it) say "same models and efforts as …"; models are listed
  by id only.
- A container's last use is marked by its shell at every command, so one
  in use by a long run (or by the user's own opencode) isn't stopped.

Not changed: the browser tool and "No desktop browser is connected" are
opencode's own; the user's computer "localhost" was offline because its
connector wasn't running after the workspace restart (Relay said so, with
what to do); `relay` isn't on the PATH because each conversation has its own
script (a token), and opencode's server is shared by many conversations, so
there's no way to tell which one a command belongs to; the note gives the
full path. The label is for the user, not the subagent (as documented).

### Debian Trixie in the containers (same day)

The default image is `docker.io/library/python:3.13-trixie` now (Debian 13,
asked by the user), instead of Bookworm. Containers made from Bookworm are
made anew on their next use (their spec changed), without what was
installed in them. Tried: apt (with Relay's download-as-root setting), git,
Python 3.13, files owned by the user.

### Second container test by an agent (same day)

- **Other projects in the same repository could be changed**: since the
  container got the git repository a folder is in (for git to work), an
  agent in `projects/sample` could write in `projects/google-maps-clone`
  (`projects` is one repository). Now the repository is mounted read-only,
  with the folder and `.git` writable on top: git works (status, add,
  commit from the folder), the rest stays as it is. The agent is told to
  keep git to the folder (`git add -- .`, not `-A`). Relay's own commits
  (checkpoints, direct mode) were already limited to the folder.
- **No Node.js**: Relay builds an image of its own on first use,
  `localhost/relay-agent:<hash of the Containerfile>` from
  `src/relay/container/Containerfile`: python:3.13-trixie plus Node.js 20,
  npm, ripgrep, jq (37 s here, a few minutes on a Pi, +180 MB). A changed
  Containerfile is built anew and older builds are removed;
  `container_image` still uses an image of one's own as it is.
- The note says Relay's tools and skills are in the container too,
  read-only, at the paths given ("path model confusing").

Not changed: the empty folder was the test's; the browser is opencode's;
the computer "localhost" is offline because its connector isn't running;
`relay agents` keeps the failures (they're what a choice of model needs).

## A browser for opencode's browser tools; third container report (2026-10-01)

opencode v2's browser tools (tabs, snapshot with element refs, click, fill,
screenshot, console, network, traces, …) are executed by its desktop app:
"No desktop browser is connected to this session" everywhere else. The
protocol, read from opencode's server code and tried live: a plugin RPC
`experimental.browser` (`POST /api/rpc/experimental.browser/<method>?location[directory]=…`,
body `{"input": …}`). The desktop calls `attach {sessionID, connectionID,
version: 4}` and holds it open; opencode emits
`rpc.experimental.browser.control` events on `/api/event` (`attached`,
`command`, `cancel`); the desktop fetches a command with `command
{requestID}` (`{action: {type, …}, files, inspect?}`), answers with `result
{requestID, outcome}` (`success {value, files: [{id, name, mime, data:
base64}]}` or `failure {code, message}`), and reports its tabs with `state`,
against which opencode checks every tabID. Results are validated against
the action's schema (tab: id `tab_<uuid>`, url, title, loading, canGoBack,
canGoForward, generation; refs `e12`). Files are written by opencode to its
`TMPDIR`.

Relay is that desktop now:
- `opencode_manager/bridge/opencode_browser.py` speaks the protocol (one
  attachment per session, kept after the run), and hands commands to a
  program as JSON lines;
- `relay/container/browser.cjs` is that program: Playwright with Debian's
  Chromium in the agent's container (`page.ariaSnapshot({mode: "ai"})` gives
  exactly the `[ref=e2]` refs opencode's tools use; `aria-ref=e2` finds them);
  31 of the 45 actions; the rest (traces, profiles, Lighthouse, drop,
  preview) fail with a clear "unsupported";
- nothing of opencode's (address, password) goes into the container: with
  the password an agent could use opencode's API outside its container
  (`fs.write` writes anywhere);
- opencode's service gets `TMPDIR=/tmp/opencode`, so screenshots are where
  the container sees them;
- the image adds Chromium, fonts and playwright-core (about 2 GB in all).

Found on the way: the attachment's tabs were sometimes unknown to opencode
("tab_unavailable" right after opening one): the `attached` event came
before Relay was listening, and the tab state waited for it. Relay now
listens before attaching, and resends the state if opencode wasn't ready.

Live (Zen big-pickle): opened example.com, snapshot, evaluate, screenshot
(a PNG in /tmp/opencode, seen from the container); with a web server the
agent started in the container: opened localhost:8765, filled a form by
refs, checked a box, clicked, read the console and the result.

From the third report:
- `relay computers`: an offline computer says its connector isn't running
  (when last seen), and that Relay and the container are fine;
- `relay agents` says what a subagent of each provider can use here (its own
  tools in the container; Relay's file tools in code tasks only; none);
- the note says `git status --untracked-files=all -- .` (a new file in a
  new folder showed as `?? ./`) and that Chromium and Playwright are there;
- `relay doctor` no longer builds the image: it says it isn't built (Relay
  builds it when it starts), and `relay doctor --build` builds it.
Not changed: Starlette's warning about httpx in the tests (a test
dependency; nothing breaks yet).

## Fallback loops (2026-10-01)

The one list of fallback agents per user (Settings → Providers,
`/fallback-agents`) became **fallback loops** (Settings → Usage): named,
ordered lists of agents, as many as the user likes ("Opus (Claude Code) →
Sol (opencode) → Astra (Copilot)"). The user's choices: a chat follows the
first loop its model is in, and `/loop` (or a chip under the message box)
gives it another or none; when no agent of the loop can go on, Relay waits
for the first known reset among them, else 5 hours (`limits.unknown_wait`),
and goes round again from the loop's first agent, at most
`limits.loop_rounds` (5) rounds.

- `relay/fallback.py`: loops in the preference `fallback_loops`; the old
  `fallback_agents` list reads as a loop "Fallback agents" until loops are
  saved. `next_in` goes round from the agent that stopped, skipping the
  ones tried this round, turned off, without tools where needed, or limited
  themselves; `first_reset` is the round's wait.
- `runs._after_stop`: the loop a conversation is going round stays in
  `limit_info.loop` ({id, name, round}), so its model changing doesn't make
  it pick another loop; at the end of a round `limit_info.restart` names the
  first agent and `resume_at` the time; the scheduler's resume calls
  `restart_loop` (the first agent that can go on, round + 1, nothing tried)
  before sending the resume prompt. A conversation in no loop: as before.
- `conversations.fallback_loop`: NULL (automatic), a loop id, or "off";
  copied by forks; choosing another while waiting for a round retargets the
  restart, choosing none cancels it.
- `/api/fallback-agents` became `/api/fallback-loops`.
- The loop chip showed only for a chat with a loop of its own choosing, so
  nobody saw it (the user asked where it was). It's always under the
  message box now, naming the loop that applies ("No fallback loop" when the
  model is in none), for new chats too (and `/loop` there); loops load with
  the rest at start.

## GitHub Copilot added (2026-10-01)

The user signed in to Copilot (a student account) and asked for it as a
provider: `data/relay/providers/copilot.json` (kind `copilot`, models Default
and Auto). Copilot answers ACP's `initialize`, but `session/new` fails with
403 "not authorized to use this Copilot feature" (`copilot -p` too: "Access
denied by policy settings"): the account's Copilot CLI policy, at
github.com/settings/copilot, not something Relay can change. The ACP check
(`relay doctor`, Settings → Providers) opened no session, so it said "speaks
ACP" all the same; it opens one now and says why it can't.
- Copilot works once GitHub caught up with the account (about 30 minutes
  after it was activated). But its commands ran on the host: Copilot runs
  `/bin/bash --norc --noprofile -c <command>` itself, ignoring `$SHELL` and the
  PATH, and doesn't use ACP's client terminals. ACP agents now also get
  Relay's shell first on their PATH (helps agents that start `bash` from it;
  not Copilot). The user proposed ProviderBox: every provider in a container
  of its own (planned next).

## ProviderBoxes (2026-10-01)

Copilot ran its commands on the machine (it starts `/bin/bash` itself), so
per-provider tricks (`$SHELL`, `CLAUDE_CODE_SHELL`, a shared shell routed by
folder, the PATH) had reached their end. The user proposed **ProviderBox**:
every provider in a container of its own, with the commands handed to Relay.
Their choices: one box per provider, working in many work containers;
opencode-manager (all its services) in one box; whether commands show as
events of their own: left to me.

- `relay/boxes.py`: `Boxes` makes the boxes (`localhost/relay-box:<hash>`
  from `container/Boxfile`: Debian Trixie slim, Python, git, ripgrep,
  libstdc++), with the provider's program (from the machine, read-only), its
  login and settings, the workspace roots, Relay's chat and worktree folders,
  Relay's tools, skills, uploads and handovers (read-only), the exchange
  folder and `/tmp/opencode`; projects outside the roots join (remembered per
  box). A box made otherwise is made anew unless a run of it is going on.
  Boxes aren't stopped when idle (opencode's services live there).
- The box's `/bin/bash`, `/bin/sh` and `/bin/dash` are `box/relay-shell`: it
  sends {argv, name, cwd, folder} and then input and signals as frames over
  the box's socket (`/run/relay/exec.sock`, a folder mounted from Relay's
  cache), and writes out what comes back. `ExecGateway` (a Unix socket server
  per box, in Relay) applies the guard, finds the work container (the run's
  folder from `RELAY_FOLDER`, which the provider's process passes on, else
  the folder routed for the cwd, else the cwd's own, never the home folder),
  runs `podman exec`, and ends the command's processes (by a tag in their
  environment) when its shell goes away or is signalled. Every command goes
  to `boxes/<box>.commands.log`. Not shown as chat events: the agents show
  their commands and output already; the log and the guard are in one place.
- Providers say `box_key()` and `box_mounts()`. Claude Code and Copilot are
  started with `podman exec` in their box (`BoxLaunch.wrap`; the run's tag
  lets `relay-reap` end what it started when the run ends: stopping podman
  exec leaves it running); Claude Code without its guard hook there (the
  gateway guards). opencode-manager's `opencode` commands go through a
  `LAUNCHER` (Relay sets it), so its services start in the box; a service
  found running elsewhere (no `RELAY_BOX=1`) is started over in it. ACP
  agents other than Copilot and `command` providers keep running on the
  machine with Relay's shell.
- Found on the way: podman can't put a mount point on a link inside another
  mount (a link is now mounted where it points, and at its own path only
  when nothing around it is mounted); opencode-manager ran podman with the
  profile's `XDG_DATA_HOME` (another container store: "no such container");
  `service_processes` compared config paths literally (a profile reached
  through a link wasn't recognised); starting a service over right after
  stopping one found its port still taken and moved the profile to another
  port (it now waits for the port; the user's `localai` was put back on
  49400).
- Live: Copilot (Auto), Claude Code (Haiku) and opencode (Zen big-pickle)
  each ran commands in the work container (Debian Trixie, `HOME=/home/relay`)
  from their boxes; Claude Code kept its folder across commands and wrote a
  file with its own tool; opencode's browser still worked; `rm -rf /` from a
  box's shell was refused by the gateway.

## Rough edges from the user's own conversations (2026-10-01)

Read the day's conversations in Relay (mostly agents testing and reviewing
the environment) for what didn't go smoothly:

- **Copilot's chats were titled "A note before my message: Relay, …"**: its
  `session_info_update` title is the start of the message Relay sent, note
  and all, and it replaced Relay's own title (the user's first words). A
  title that's merely the start of the sent message is ignored now; the two
  chats got their titles back.
- **`relay agents` gave efforts per provider**, the default model's, so an
  agent picked `--effort low` for a model without efforts. Providers whose
  efforts differ per model (opencode) list them per model now, "model (low,
  high)", from one request; the provider's line says it's the default
  model's.
- **The subagent skill said Relay runs at most three at once** (the user set
  eight): it points to `relay agents` now, which states the current limits
  (at once, per answer, time).
- **Relay's note was called long** by several agents: the script's path is
  written once (`<path> COMMAND`) with the commands under it, the container
  paragraph is shorter, and the larger-repository advice only appears when
  the project is in one (3.7k → 3.0k characters, skills list included).
- **Three opencode turns failed within a second without a word** (opencode's
  `session.execution.failed` without an error): Relay now says the turn
  failed without a reason, rather than showing an empty answer.

Looked at and left: chats stopped by the user after a second (asked again in
a code task); opencode's own patch-format errors and its large background
shell previews (opencode's, not Relay's); a session limit and an unavailable
upstream (the providers').

## Network drive: making a folder was refused (2026-10-01)

The user couldn't upload through the network drive ("Access was denied while
attempting to create the requested folder"). The shares weren't read-only,
and MKCOL, PUT, MOVE, LOCK and DELETE all worked from curl. What a file
manager does next did not: Windows sets a new folder's times and attributes
with PROPPATCH and checks each property it named comes back with 200; Relay
answered with an empty prop. It echoes the properties now. WebDAV changes and
refusals are logged in `~/.cache/opencode-manager/relay-webdav.log` (method,
status, path, client), for the next time a client says no; an OSError in a
handler (a file another user owns) is a 403 with the reason instead of a 500.

## A fuller work image (2026-10-01)

Asked for more tooling in the work container. Added to the image: Go 1.24,
Rust 1.85 (cargo), cmake and ninja, pnpm, yarn, TypeScript and tsx, uv, the
GitHub CLI, git-lfs, fd and bat (under those names), yq, sqlite3, the
PostgreSQL client, shellcheck, dig/ping/nc/ssh, nano and vi, htop, tmux,
rsync, zstd, tree, lsof; `LANG=C.UTF-8`; git's default branch `main`. Left
out for their size (an agent installs them per project): Java, ffmpeg,
pandoc, LaTeX. 2.1 → 3.0 GB; built in two minutes here. The note to agents
names what's there.
- And pytest (with pytest-cov and pytest-xdist), ruff and mypy, in a layer of
  their own (9 s to add; the image is 3.15 GB).
- `wait` with subagents still working now says: "Tell your subagent to finish
  up (with tell), or run wait again for them." (the user's wording).

## Subagents on local models can be turned off (2026-10-01)

The user's local model server isn't always running, and subagents were still
started on it. Settings → Subagents has *Run subagents on local models*
(`local` in the subagent rules, on by default): off, a subagent on a model
of a local server is refused before anything else, with the subagents
allowed instead (local ones left out of that list), and `relay agents` says
local subagents are off.

## No commits by Relay itself; a record of every turn instead (2026-10-01)

An agent reviewing Relay pointed out (and the user asked for it): a task in a
repository's folder committed every turn to the user's current branch
("Relay: hi", "Task: hi"), silently, mixing Relay's bookkeeping with the
user's history. Now:

- **Tasks in a folder** (git or not) are followed with Relay's checkpoints
  (`relay.checkpoints`, a repository of Relay's outside the project): before
  and after every turn (`runs.after_checkpoint` is new). Changes, *Review*
  under an answer, `/rewind` and Rewind to the start work from them; Mark as
  reviewed takes a checkpoint as the new base. Nothing goes into the user's
  repository.
- **Commits are explicit**: Commit… in the Changes panel, or `relay commit -m`
  (MCP: `commit`) for an agent the user asked; they commit the task's folder
  (only it, in part of a larger repository) to the current branch as the
  user (`tasks.commit`; hooks and signing off, as for Relay's other commits,
  so nothing waits for a passphrase).
- **Agents see the record**: `relay changes`, `relay diff N|all`, `relay
  restore N` (code tasks only; restore prints the point that undoes it); the
  note tells agents in code tasks that Relay never commits and they shouldn't
  unless asked.
- **A task on its own branch** is unchanged: that branch is Relay's, its
  turns are commits there, and Merge (one commit) is the user's step.
- Older tasks keep working: a base or rewind point that's a commit of the
  user's repository (from before) is read with git, and rewinding to one
  restores the files without a commit (and without touching the index).
- Changes of a task in part of a repository are listed relative to its
  folder now (they were relative to the repository).

## The code folder is Relay; no more portable folder (2026-10-01)

The repository was still called opencode-manager, with opencode-manager's
README on top, and everything (profiles, Relay's database, chats) lived in
`data/` inside the code folder so the whole thing could go on a USB stick.
Since providers run in ProviderBoxes and commands in work containers, that
requirement only cost complexity. Now:

- **The folder is `relay`**; Relay's README is the README.
  opencode-manager's is `docs/opencode-manager.md`, this log is
  `docs/DEVLOG.md`, and the tests are split into `tests/relay/` and
  `tests/opencode_manager/`. `pyproject.toml` names the project `relay`.
- **Data is apart from the code**: Relay's in `RELAY_HOME` (default
  `~/.local/share/relay`), opencode-manager's profiles and state in
  `opencode-manager/` there (`OPENCODE_MANAGER_HOME` still overrides it).
  The launchers no longer set a data folder; `relay systemd-unit` passes
  either variable on when it's set.
- **Gone**: bundled programs (`bin/<os>-<arch>/`), `relay portable`, the copy
  of a bundled binary to the cache when a stick doesn't allow executing it,
  and storing recent projects relative to the folder (older entries still
  resolve). Programs come from `PATH`. `relay claude` (Relay's own Claude Code
  login) stays, its login now in `~/.local/share/relay/claude`.
- `relay.portable` is `relay.relocation`: what remains is repairing stored
  paths and worktrees after the data or code moved. It now also moves Claude
  Code's session folders (`~/.claude/projects/<folder>`) of conversations
  whose folder moved, so they can still be resumed.
- Caches stay in `~/.cache/opencode-manager` (bytecode, tools scripts, the
  work images' build folders).
- `relay doctor` no longer starts opencode services itself (`Provider.probing`):
  it ran in a process of its own, without the boxes, so they ended up on the
  machine (and on another port) until Relay started them over in its box.
- Moving this installation: Relay stopped, its containers removed, the data
  moved from the code folder to `~/.local/share/relay`, the folder renamed to
  `/workspace/relay` (`/workspace/opencode-manager` is a link to it for now).
  A stray podman storage in the localai profile (1.4 GB, from when podman ran
  with the profile's `XDG_DATA_HOME`) was removed.

## Subagents work in a copy of the folder (2026-10-01)

The user's idea: fork the checkpoint when `delegate` starts a code subagent,
and merge its diff only when it's done. Until now subagents of a code task
worked in the agent's folder itself, so two at once (or a subagent and its
caller) could overwrite each other's files halfway. Choices the user made:
conflicts, and the work of a subagent that failed or was stopped, are left to
the calling agent; installed software is linked, not copied.

- `relay.forks`: at `delegate` the caller's folder is recorded as a checkpoint
  (its checkpoint repository, under `refs/forks/<subagent>/`, with index files
  of the copy's own, so the caller's checkpoints are left alone) and put in
  `~/.local/share/relay/forks/<subagent>/<folder>`. `node_modules`, virtual
  environments and the like are symlinks to the caller's; the copy's container
  mounts those folders at their own path (`Containers.mount_also`), so paths in
  a virtual environment still work. Every box mounts the copies' folder.
- When the subagent finished, `git merge-tree` merges (where both started, the
  caller's files now, the subagent's) and `read-tree -m -u` applies the result
  to the caller's folder: only the files the subagent changed, and a file the
  caller changed after Relay looked stops it (it looks once more). Its answer
  says which files came in.
- A conflict, a failed or a stopped subagent: nothing applied; the caller gets
  the diff and decides. `merge ID` (MCP and chat tools, `relay merge`) applies
  it with conflict markers (`<<<<<<< yours … >>>>>>> subagent`); `tell` first
  brings the caller's current files into the copy (`<<<<<<< yours … >>>>>>>
  caller` there). Interrupted by `tell` doesn't count as stopped.
- Read-only subagents (Plan and the like) work in the folder itself. A folder
  Relay can't checkpoint (too many files) is shared, as before, and the agent
  is told so.
- The copies, their containers and refs go when the caller's run ends (and at
  Relay's start); what wasn't merged goes with them.
- `checkpoints.take` is `record()` with a ref and an index of its own.


## Reliability after the general review (2026-10-01)

The review was for Relay as a personal runtime: prevent lost work and stuck
runs, without tightening the provider/permission policy or removing parallel
agent work.

- **Repairs leave concurrent source edits alone.** Review reports changed,
  added and deleted source files; discard no longer restores anything.
  Modification times cannot establish that a change came from the repair
  agent. The support files are copied to a separate reviewed snapshot: the
  diff, compile check, shadow test and apply all use that version. Apply checks
  its hashes and that the live originals are unchanged; undo checks every
  applied file before restoring any. Old reviews need another check; old
  applied repairs without recorded hashes cannot be undone automatically.
  Review/apply/undo/discard are serialized inside Relay.
- **OpenCode stops do not require a terminal SSE event.** An interrupt also
  cancels the stream. A watcher shuts down its socket to wake the blocked
  reader (closing a buffered response from another thread can itself block).
  Connection/header waits are bounded; idle connected streams remain unlimited.
  Cancellation covers plain and chunked SSE, failed interrupt requests and a
  stop before prompting. Other bridge backends keep their own stop mechanism.
- **Live run events are persisted in batches.** A lazy flusher runs every
  half-second while there are active runs; final storage flushes any remaining
  events. Event sequence numbers are unchanged, and a failed flush can retry.
  On restart, saved partial text/tool history is materialized as an assistant
  turn with a restart error and failed status. Pending permission/subagent cards
  get terminal states too, rather than spinning forever. Recovery is
  transactional and idempotent. The unflushed tail may still be lost on a crash.
- **Project mutations reserve their affected files.** Rewind (including retry
  with restore), review, commit, merge and discard check other active writers,
  including subagents whose copies will merge back. Registration is atomic
  with starting a run, so a new writer cannot slip into a restore in progress.
  Repository-wide operations also coordinate worktrees. Ordinary runs remain
  parallel; unrelated project operations can proceed. Agent-requested restore
  and commit exclude the requesting run, not other writers.
- **Forks copy turn metadata:** before/after checkpoints, mode, effort and
  session, alongside their existing events and usage; copied turns can rewind.
- **The composer clears only what was sent.** Text changed during send/queue
  acknowledgement and newly added uploads stay in the draft. Double submissions
  remain blocked, and failure retains the draft.
- **Settings refresh only the header/pickers of an open chat.** The live
  answer view, EventSource, composer and uploads stay attached.
- Regression tests cover these paths, including a real local silent SSE server.
  `tests/js/composer.test.cjs` uses Node's built-in test runner, invoked by pytest
  when Node is installed. The suite no longer needs an installed opencode to
  test CLI profile deletion; the tools-script test checks file mode rather than
  root's effective write access. Test reflogs use a fixed committer identity to
  avoid container-hostname DNS delays. The regression suite uses isolated data
  and no real model providers.

Validation: 442 tests passed and two container integration tests were skipped
in the workcontainer. Final revalidation on the host passed all 444 tests,
including the real Podman and Chromium integration tests. The existing
Starlette/httpx test-client deprecation warning remains.
After returning to the host, Relay was restarted through
its authenticated API with no active runs. A separate live smoke test used
`opencode-default` / `opencode/big-pickle`, with fallback disabled, in the
test conversation's own scratch folder. It executed
`python3 -c 'print(6 * 7)'` and answered `RELAY_SMOKE_OK:42` in 5.1 seconds;
usage reported cost 0 and billing `free`, with no errors. The exec-gateway log
confirmed the command ran in a workcontainer, not on the host.

## Model-specific OpenCode effort on handover (2026-10-01)

A free-model fallback carried `high` from Big Pickle to LongCat, though neither
model offers variants. OpenCode accepts that value when switching models and
rejects it only when prompted; catching a failed switch therefore did not help.

The OpenCode support layer now checks the selected model's variants in the
conversation's directory before switching. Unsupported effort uses the explicit
`default` variant; supported effort is retained. Validation precedes the
unchanged-selection check, so existing sessions containing an invalid variant
are corrected too. Catalogue and switch HTTP failures propagate normally rather
than being mistaken for unsupported effort. No OpenCode-specific behaviour was
added to Relay's run manager, routes or fallback loop.

Regression coverage includes supported/unsupported effort, unchanged selections,
Big Pickle to LongCat handover, stale session variants and HTTP errors. Live
validation against restarted Relay used a temporary `opencode-localai` /
`opencode/big-pickle` chat with `high` and fallback disabled: its shell returned
42 and the answer was `RELAY_VARIANT_OK:42` (6.7 seconds). A second turn after
deliberately setting that test session's variant back to `high` answered
`RELAY_VARIANT_RESUME_OK` (2.2 seconds). Both succeeded with effective variant
`default`, free billing and zero cost. The temporary chat, provider session and
workcontainer were removed. Existing conversations and fallback settings were
not changed. The separate Fledge country restriction remains a provider error;
Relay continues to the next fallback entry as before.

Validation: all 447 tests passed on the host, including Podman and Chromium
integration. Focused lint and `git diff --check` passed. The existing
Starlette/httpx test-client deprecation warning remains.

## Visible, answerable provider questions (2026-10-02)

OpenCode's native `question` tool was visible only as a running tool card.
The bridge ignored form events: `form.created` puts the session inside
`data.form`, unlike ordinary session events, and no form reply path or UI
existed. The provider therefore kept waiting without showing any choices.

- The OpenCode support layer translates creation, replies and cancellation
  into a standard `form` event. It tracks the owning session, including native
  subagents, and sends keyed field values to the form reply API. These are
  questions, not permissions: they are never auto-approved.
- `ProviderRun.answer_form()` is the shared interface with an unsupported
  default; BridgeRun implements it for capable backends. Relay tracks pending
  forms, exposes an owned-run answer endpoint, and forwards Relay subagents'
  questions into their parent's answer. Replies are serialized per request;
  failed sends remain retryable, and a quickly finishing provider cannot store
  `done` before its accepted answer event.
- The always-open questions card supports single/multiple selections,
  descriptions, custom answers, defaults and conditional fields. Duplicate
  pending events retain the live draft; failed sends keep selections and allow
  retry. Settled forms replay as read-only cards. Stopped/restarted runs close
  their pending cards instead of leaving active buttons behind.
- Dismiss uses the provider's cancellation API. Live OpenCode verification
  confirmed that dismissing its native question interrupts the turn; Relay
  preserves that behaviour instead of fabricating an answer or forcing it on.

Regression tests cover native subagent session routing, answer/cancel APIs,
failed reply retries, Relay subagent forwarding, restart recovery, replay,
double submission, selection/custom-input rendering and stop/finish races.
Live headless Chromium against Relay with free Big Pickle verified three native
questions, reload while pending, two simultaneous choices, a custom GPU answer,
one budget choice, submission and the model's answer repeating all choices.
Completed-card replay had no live buttons and there were no page errors.
Dismissal reached the provider and produced its native interrupted status.
Temporary chats, provider sessions and workcontainers were removed; existing
user conversations and provider settings were not changed.

Validation: all 455 tests passed on the host, including Podman and Chromium
integration and eight JavaScript regressions. Focused lint and
`git diff --check` passed. The existing Starlette/httpx deprecation warning
remains. The final live browser trial passed answer/reload/replay and dismissal
checks with free billing and zero cost.

## Dismiss unanswered questions when nobody is there (2026-10-02)

`questions.dismiss_timeout` adds **Settings → Limits → Questions → Dismiss
after**, default 300 seconds, with 0 to wait indefinitely. Each new question
gets a server deadline and a daemon timer, not a browser-side auto-submit.
The shared run manager calls the provider's existing cancellation hook, so
an absent user cannot leave a question waiting forever. The provider decides
whether cancellation continues or interrupts its turn, as with manual dismissal.

Answers, manual dismissal, provider settlement and stopping/ending a run cancel
the timer. Timed and manual replies share the request lock, so only one wins;
an already-running answer is not followed by a second dismissal. Duplicate
delivery does not extend the deadline. If the provider reply fails, the question
stays open and dismissal retries after 30 seconds, rather than claiming that it
was cancelled. The timeout reason stays in the event history.

The question card counts down from the recorded deadline, including after
reloading. Its display timer stops on settlement or detachment; expiration only
says that dismissal is pending, and never calls the provider from the browser.
Regression tests cover automatic dismissal without a connected browser, disabled
and configured timeouts, answer/dismiss/stop cleanup, duplicate delivery, failed
reply retry, concurrent answer/expiry and countdown lifecycle.

Validation: all 465 tests passed on the host, including ten JavaScript
regressions. Focused lint and `git diff --check` passed; the existing
Starlette/httpx deprecation warning remains. A real Big Pickle question in a
temporary Relay chat was automatically dismissed after 300.3 seconds, with the
event stream disconnected and no answer submitted. OpenCode ended its turn as
interrupted, with free billing and zero cost. A brief Chromium check confirmed
that the displayed countdown decreased and reloading retained the deadline;
the browser was closed before expiry. The test's chat, provider session and
workcontainer were removed. No real conversation or provider setting was changed.

## UI/UX, first round: work and attention before history (2026-10-02)

The first overhaul round changes presentation and navigation, not provider
behaviour or chat/stream persistence. A shared `activity.js` gives the sidebar,
conversation header and task cards one status vocabulary: waiting, working,
resuming later, failed, stopped, review, completed and ready. Current work takes
precedence over an older failed turn. Review status uses actual change summaries,
not just the existence of a task branch.

- Sidebar groups put unanswered requests first, active work second, then pinned
  chats and dated history, with counts and readable activity labels. API pin
  ordering no longer distorts chronological history. Each conversation appears
  once, and search, menus, unread markers and pinning still work.
- The header shows a persistent text status. Its waiting button scrolls to and
  focuses a pending form or approval, including forwarded subagent requests.
  Stream events update it immediately; ordinary list polling keeps other activity
  current without replacing the composer or streaming view.
- Tasks gives waiting agents their own first section and an Answer action rather
  than mixing them with actively working agents. Review and stop actions remain.
- Composer pickers have primary and advanced slots. Provider, model, folder and
  file access remain direct; mode and fallback move into a native Options
  disclosure. Non-default modes remain visible in its closed label. Picker
  refreshes retain the disclosure, typed text and uploads; slash commands are
  unchanged.
- A roomier, quieter workspace uses slate-tinted light surfaces, clearer heading
  hierarchy, compact tool rows and amber question cards. Dark theme remains, and
  phone layouts keep status and send controls on screen. Reduced-motion settings
  suppress pulsing and transitions.

JavaScript regressions cover status precedence, saved-turn fallback, genuine
review state, sidebar ordering/deduplication and composer disclosure lifecycle.
A real Chromium regression runs against a throwaway Relay with a fake native
question provider: waiting/working groups, question focus, draft preservation,
mode labels, reload, answer/completion and dark/light 320/390px layouts. It uses
no real provider services or user data.

Validation: all 466 tests passed, including 17 JavaScript regressions and the
new real-Chromium UI regression. Focused browser/frontend tests passed again
after ensuring unchanged polls preserve focus on the waiting-status button.
Lint and `git diff --check` passed; the existing Starlette/httpx test-client
deprecation warning remains. A read-only Chromium visit to the running Relay
confirmed the new welcome/composer layout on desktop and phone, with no browser
errors and no change to real conversations or settings.

## UI/UX, second round: activity, readback and subagents (2026-10-02)

Consecutive tools now share a native, collapsed Activity disclosure. A small
renderer-only helper summarizes known operations and running/finished calls,
without another model request. Text, reasoning, forms, visible permission
requests and subagent cards break the group. Late tool results update the
original group rather than moving it past the answer. Failed tools move outside
the group and show their failure; provider errors and questions stay directly
visible. Show details opens groups and tool input/output, but not subagent chats.
Unfinished tools settle as stopped when a turn ends, not as successful reads.

`ThreadScroll` tracks whether the user is following output independently of
how much a new chunk changes the page height. A ResizeObserver handles delayed
markdown layout, viewport changes and nested subagent growth. Reading back
keeps a visible-content anchor; expanding tool/subagent details pauses following.
A floating Latest button resumes it and marks new activity while reading back.
Observers, queued frames and listeners are disposed on navigation, and queued
scrolls re-check the user's intent before moving. There is no forced jump when
a question arrives; the existing Waiting for you button remains available.

Subagents remain separate cards with a short task, provider/model, costs and
text status (queued, working, waiting for you, completed, stopped, failed).
Forwarded forms/approvals mark the owning card as waiting without opening its
stream. A failed card displays an error, or an explanation pointing to its chat,
outside its collapsible body. Child streams still open only on demand and close
with the card/navigation. A new child run replaces the old connection; stale
message/end/error callbacks cannot update or close its replacement. Open child
views use the same tool groups and notify the parent's scroll controller.

Regression coverage includes grouped/replayed tools, late results, standalone
failures, stopped tools, Show details, closed-child questions, lazy streams and
stale callbacks. Scroll tests cover large chunks, delayed layout, readback,
anchors, queued-frame races, Latest, detail expansion and cleanup. The real
Chromium test now uses parent and child SSE streams, real forwarded form replies,
long output, deferred child connections, reload, themes and narrow phone layouts.
No real provider or model is needed for this regression.

Validation: all 466 tests passed, with 27 JavaScript regressions. The expanded
Chromium/frontend checks passed again after final anchor/status refinements;
lint and `git diff --check` passed. The existing Starlette/httpx deprecation
warning remains. A read-only browser visit to the running Relay replayed the
retained Big Pickle smoke chat as collapsed Activity plus its answer on desktop
and phone, without browser errors. No new model call, real conversation edit or
provider/settings change was made. The temporary screenshot was removed.

## UI consistency: drafts, routes and keyboard focus (2026-10-02)

The read-only audit reproduced lost welcome-screen drafts after a settings
refresh, a Tasks URL displaying the welcome screen, keyboard focus disappearing
on every list poll, and Enter on a sidebar More button also opening the chat.
This round fixes those without changing providers, streaming or permissions.

- Settings refresh the connected welcome/chat composer in place, preserving
  typed text, attachment uploads and Options. Computer updates follow the same
  path. Tasks and Scheduled keep their own views instead of creating a new chat.
- Unchanged sidebar/task snapshots retain their DOM. Changed lists restore the
  focused action by conversation id and keep scroll position. Sidebar polling
  waits for open menus and inline renames; task polling ignores stale responses
  and keeps the last good view during transient errors.
- Enter navigates a sidebar row only when that row itself owns focus. Inline
  rename is no longer treated as chat navigation, and menu closure cannot remove
  its anchor before a rename starts.
- Menus focus their first item, support arrows/Home/End and keyboard submenus,
  and restore the opener on closure. Slash suggestions opt out of autofocus.
  Picker refreshes preserve the focused control. Existing dialogs are named,
  start with focus inside, wrap Tab, close only the top nested dialog and restore
  focus; portalled menus inside them still work. No new confirmation is added.

Regression coverage includes welcome drafts with real uploads, refreshes on
Tasks/Scheduled, unchanged and changed sidebar focus, polling during rename,
More/Enter, slash completion, picker focus and nested dialog Tab/Escape. The
Chromium checks run against throwaway data with a fake provider, alongside the
existing questions, parent/child SSE and phone-layout checks.

Validation: all 466 tests passed, including 30 JavaScript regressions and the
expanded real-Chromium checks. `git diff --check` passed. The existing
Starlette/httpx deprecation warning remains; no real models or user data were
used by the regression suite.

## UI consistency: attention counts, labels and saving (2026-10-02)

The second audit-fix subject keeps the existing immediate/manual saving choices,
but makes them explicit rather than adding confirmations or changing provider
behaviour.

- Scheduled and Tasks share active navigation and `aria-current`. Tasks uses
  Working, matching the sidebar/header. Its badge comes from actual task groups:
  work, unanswered requests and review, with a hover breakdown. Shared project
  changes count once, and search does not filter the total. The overview reuses
  its existing polling; elsewhere a changed conversation snapshot triggers a
  fresh count, rather than scanning repositories on every unchanged list poll.
  Older responses cannot replace a newer overview count.
- Limits numbers/text have associated visible labels and help descriptions,
  including Stay signed in. Provider/browser notification switches have names
  and a visible keyboard-focus outline. Tool and subagent disclosures expose
  their unique controlled body and current expanded state, including Show details
  and failed-tool auto-expansion; child streams still open only on demand.
- Settings explain autosaving vs Save and give success feedback. Limits and
  workspace saves show Saving and disable repeated clicks until settled.
  Save and send test now names both operations. It does not test after a failed
  save; a saved configuration stays reported as saved when the test fails.
  Notification results are announced, retries remain available, and the longer
  action label wraps onto another row on narrow phones.
- Keyboard help distinguishes desktop Enter-to-send from touch Enter-to-add-a-
  line instead of describing one behaviour as universal.

Regressions use two real checkpointed tasks sharing a throwaway git folder to
check review deduplication, search-independent counts and clearing review. They
also cover labels, tool/subagent expanded state, failed save/test ordering,
duplicate clicks, retries, and light/dark notification settings at 320/390px.
The live Relay passed a read-only check of drafts, routes, polling/More, actual
review counts, the retained smoke chat's replayed tool details and phone settings.
No new model call, real conversation edit or provider/settings write was made.

Validation: all 466 tests passed. The focused frontend/Chromium checks also
passed after the final refinements, including 35 JavaScript regressions. Lint
and `git diff --check` passed; the existing Starlette/httpx deprecation warning
remains.

## Sidebar chat icons (2026-10-02)

Ordinary chat conversations now show the existing chat-bubble icon in the
sidebar, using the same size and styling as code and computer indicators.
Code and computer selection retain their existing precedence. Regression
checks cover all three icon choices and actual chat icons in Chromium.

Validation: the focused frontend and Chromium tests passed, including 36
JavaScript regressions. Lint and `git diff --check` passed. No model calls or
real conversation/settings changes were made.

## UI/UX, third round: review, recovery and answer actions (2026-10-02)

The existing review and recovery operations were useful but partly hidden behind
counts and hover-only icons. This round makes their meaning and next action
explicit without changing provider APIs, fallback policy or the event stream.

- Review changes has the same name in the header, Tasks and changed answers.
  Answers retain their recorded counts and open the current files. The panel
  distinguishes Already in your project from On a separate branch, explains
  that opening review applies/commits nothing, and retains the existing explicit
  mark-reviewed, commit, merge and rewind operations. Diff disclosures expose
  their expanded state; Escape closes a nested commit dialog before the panel,
  and closing review restores its opener's focus. Actions wrap on narrow phones.
- Failed/stopped latest answers show Retry and Choose another model next to the
  error or stop note. A factual notice describes the scheduled automatic resume,
  fallback-loop restart, or absence of a retry. Cancel/Resume then update it.
  Manual recovery retains its existing server semantics and replaces a scheduled
  attempt; repeated recovery clicks are disabled and delayed responses cannot
  pull the user back after navigation.
- Reopening the same chat reuses its composer, retaining the unsent text,
  attachment uploads and open Options during retries, regeneration and handover.
  A completed attempt already returned in history is not also appended as a live
  replay. The fast fake-provider continuation reproduced that duplicate turn,
  which previously also prevented its answer actions from being decorated.
- Answer actions are always visible in one order: Copy, Regenerate when the
  provider supports it, More, then model/usage details. More holds fork, Markdown
  export and per-answer file rewind; user messages keep Copy followed by Edit.
  The existing UI walkthrough uses the new labels and More menu.

Regressions cover direct/isolated/no-git review and diffs, nested Escape, review
without sending a draft, scheduled resume cancellation/reinstatement, failed
retry requests and duplicate clicks, stopped answers, regeneration, model
handover, completed-turn deduplication, stale navigation and retained real
uploads/Options. Chromium checks use throwaway data and fake providers, including
light/dark review at 320/390px; existing questions, SSE, subagents, readback and
navigation checks also pass.

Validation: all 467 tests passed, including 39 JavaScript regressions. The focused
frontend and both Chromium checks passed again after final refinements. The new
Python test passes lint and `git diff --check` passes; the broader pre-existing
lint backlog is unchanged. The existing Starlette/httpx deprecation warning
remains.

The running Relay also passed a real Chromium trial with free OpenCode
`opencode/big-pickle`: stop a tool-using turn, resume through Choose another
model, complete a tiny file edit and execute it, review the actual diff, open
More and check the phone layout. The unsent text, real upload and open Options
survived both reopenings, and the finished answer reported free billing. Trial
selectors were corrected for health-dot accessible names and the successful
code task's Needs review status (rather than Completed). The temporary chats,
project folders, draft uploads and login sessions were removed; no existing
conversation, provider configuration or settings were changed.

## UI/UX, fourth round: consistent Settings layout (2026-10-02)

Settings now shares a page heading with introductory text and page actions,
section headings, field spacing, bordered list rows and right-aligned action
rows. The change is presentation-only: no settings values, defaults, handlers,
API payloads, save/test semantics or existing confirmations were changed.

- The Settings window has more room on desktop; the eleven category buttons
  remain in their existing order. Selection updates the buttons in place,
  retaining keyboard focus and identifying the current category. Each pane
  names its category for assistive technology.
- On smaller screens, one labelled native Category selector replaces the
  horizontal scrolling tabs. Both layouts select the same pane, so resizing
  retains the category, editor and unsaved form values. Tab, Enter/Space and
  native select keys work with the existing modal focus trap and Escape.
- All categories and their editors use the same layout helpers. Limits keeps
  every existing group open, with two field columns on desktop, one on smaller
  screens, and full-width text areas. Units, help, ranges, default buttons and
  Save retain their existing controls and behaviour. Provider/model, mode,
  computer, skill and network-drive rows keep their actions visible and wrap
  safely on narrow screens. Other modals and pages retain their own styling.
- Theme/provider toggles, fallback loops and subagent rules still save
  immediately; forms still require their existing Save actions. Notification
  Save and send test retains its save-before-test behaviour. No real model
  request is needed to inspect the new layout.

A new Chromium regression uses private test data with populated lists,
disabled providers, a custom mode/skill, a computer, fallback rules and a
network share. It visits all eleven categories in both themes at 1280, 801,
800, 390 and 320 pixels; checks keyboard navigation, focus trapping and a short
viewport; verifies that browsing sends no mutation/model-test requests and
changes no stored configuration; checks the exact Limits Save payload; and
covers editors, built-in read-only skills, direct model-picker entry, disabled
login, stale category responses, and retained composer drafts/uploads. The UI
walkthrough now uses the mobile selector.

Validation: all 468 tests passed, including 39 JavaScript regressions. The
focused frontend/Chromium checks and expanded Settings regression passed again
after final refinements. The new Python test passes lint; JavaScript syntax and
`git diff --check` pass. The existing Starlette/httpx deprecation warning remains.
The running Relay also passed read-only Chromium checks of all eleven Settings
categories on desktop and 390/320px phones in light/dark themes. Settings API
snapshots before and after were identical, no model was called, and the draft
survived. The temporary login session was removed; no existing conversation or
configuration was changed.

## Recovery follow-up: one current outcome and explicit Relay notes (2026-10-02)

The user's fallback → manual stop example exposed two competing recovery
surfaces: the latest interrupted answer and a composer-level limit bar still
describing the earlier model. The automatic takeover instruction also looked
like an editable user message, next to a long provider-switch divider.

- Recovery now has one block beside the latest failed/stopped answer. It holds
  Retry, Choose another model and the existing Cancel/Resume then operations.
  The automatic plan remains visible; detailed limit/loop/reset explanations
  expand under Retry details. The duplicate composer bar is removed.
- New stop information records its owning run id. Presentation only uses it
  for that attempt, rather than attaching an old failure to a later interrupted
  or partially failed fallback. Older unlabelled stop records use a conservative
  provider/model/status match and exclude already-followed fallbacks. Stored
  fallback state and scheduling policy are unchanged.
- Messages can carry optional `relay_action` provenance: fallback, manual
  continuation, scheduled resume or retry of a Relay instruction. It records
  the models before/after and whether history was actually handed over. Providers
  still receive the ordinary prompt/history; there is no provider-specific
  behaviour or replacement of their native tools.
- Tagged instructions render as compact, keyboard-operable Relay disclosures,
  with the full instruction and technical identities inside and no Edit action.
  They replace the redundant switch divider for that transition. Ordinary
  provider/model switches also collapse technical details. Relay-note nodes
  remain one-to-one with stored messages, retaining turn actions and replay
  alignment. Retries and forks preserve provenance; explicit user edits clear
  it. Readback anchors include the new disclosures. Historical messages are not
  rewritten or classified by text matching.
- Manual recovery names the most recent attempted model, not an old failure
  retained from before a fallback. Drafts, uploads and Options survive recovery,
  including a retry of an internal instruction. The UI walkthrough uses the
  consolidated controls and expandable notes.

Regression coverage reproduces a silent original failure, automatic fallback,
manual stop, reload, Retry, another stop and manual model choice. It checks one
recovery control set, correct attempt ownership/attribution, collapsed and open
notes at desktop/390/320px, unchanged instruction text on retry, draft/upload/
Options retention, and genuine user messages with identical-looking text.
API checks cover scheduled/manual/fallback provenance, fork preservation and
explicit edits; JavaScript checks cover current and legacy stop ownership.

Validation: the full suite passed all 468 tests, with 40 JavaScript regressions;
focused browser/frontend/API checks passed again after extending the retry/edit
coverage. JavaScript syntax, the browser test's lint and `git diff --check` pass.
The existing Starlette/httpx warning and pre-existing core import/exception lint
findings are unchanged. No real model was called or existing conversation edited
by these tests.

A read-only Chromium check of the reported live Fledge → fallback → stop chat
confirmed one Retry/Choose another model set and no stale limit details, across
desktop/390/320px and light/dark themes, including reload and expandable switch
notes. Its stored messages were identical before and after; API writes were
blocked by the check. The temporary login session was removed.

## Keep current tool activity expanded (2026-10-02)

The user wanted to see ongoing tool work rather than only an Activity summary,
matching the existing Thinking → Thoughts disclosure lifecycle.

- New live Activity blocks open automatically, without expanding every tool's
  input/output. Consecutive calls reuse the same block and do not briefly
  collapse between results and the next call.
- Text, reasoning, questions, subagents, visible permissions and errors end the
  current block. It collapses once its outstanding tools have settled; a stopped
  or finished run settles the remaining calls too. Completed blocks respect
  the current Show details preference. Saved history retains its compact view.
- Manual disclosure toggles survive tool updates. Failure cards and questions
  remain outside collapsed activity; subagent streams use the same shared
  renderer and still only open on demand. No provider or fallback logic changed.

JavaScript regressions cover consecutive calls, boundaries, parallel tools,
manual toggles, changing Show details, replay, errors and interrupted runs.
Chromium checks exercise the native disclosure with keyboard toggles, live tool
updates, the transition to Thinking and Show details on completion, using a
private test server without a real model or user conversations.

Validation: all 468 tests passed, including 44 JavaScript regressions and the
Chromium activity/recovery/settings checks. The new keyboard fixture waits for
the welcome screen to finish loading before focusing its disclosure, avoiding
a race with initial composer focus. JavaScript syntax, browser-test lint and
`git diff --check` passed. Only the existing Starlette/httpx warning remains.

## Timed thoughts and smooth disclosure transitions (2026-10-02)

The user requested Thought for <TIME> instead of Thoughts, and smoother
transitions after the live Activity change.

- The shared core measures each reasoning block with a monotonic clock, from
  its first non-empty delta to the next text/tool/question/permission/subagent/
  error/done phase. Optional `reasoning_ms` travels with the existing events
  and is stored unchanged; providers need no special implementation. Tool work,
  replay speed and browser reconnects do not get counted as thinking time.
- Completed blocks read Thought for <1s, Thought for 12s, Thought for 1m 08s,
  or an hours/minutes/seconds equivalent. Separate thinking phases have separate
  durations, including interrupted ones. Untimed older history reads Thought;
  Relay does not guess a duration from the full run or rewrite existing chats.
- Reasoning deltas now reuse the frame-batched rendering path. Phase changes
  flush the accumulated text before updating the label/collapsing, so a pending
  frame cannot lose the final delta or mix two blocks.
- Native Thinking and Activity disclosures animate height/opacity briefly,
  using `::details-content` and intrinsic-size interpolation where supported.
  Keyboard interaction, manual toggles and Show details remain native; other
  browsers retain instant switching. Reduced motion explicitly disables the
  disclosure transitions. Activity's chevron also transitions consistently.

Deterministic backend checks cover clock-based measurement, storage, empty
deltas, separate blocks and all end phases. JavaScript checks cover compact
duration formatting, identical live/replay labels, stops, legacy history and
batched/final-delta rendering. Chromium checks observe intermediate animated
heights (and no animation in reduced motion), verify the timed labels, and
confirm that automatic collapse keeps following while readback anchors stay
put. The existing layout check waits for a polled header to settle before
measuring it, avoiding a transient detached-element race. Tests use private data
and fake providers, without real model calls.

Validation: all 475 tests passed, including 47 JavaScript regressions and the
Chromium activity, recovery and Settings checks. The activity/frontend checks
also passed separately after the full run started. Changed-test/interface lint,
JavaScript syntax and `git diff --check` pass. Only the existing Starlette/httpx
deprecation warning remains.

## Subtle interface animations without interaction delays (2026-10-02)

The user asked for animations beyond Thinking/Activity. This round adds motion
to interface surfaces without changing actions, provider behaviour or settings.

- Shared short motion timings/easing coordinate menu fades, dialog/toast rises,
  review-panel entry, and one-time welcome/Tasks/Scheduled/category fades.
  Menu fades do not translate or scale the menu, keeping submenu anchors exact.
- Hover/focus colours and borders transition gently. Send/Stop has small press
  feedback, while anchored picker buttons do not move. Latest fades in without
  changing its existing centered position.
- Options, Relay transition notes and retry details use the same native height/
  opacity disclosure transitions as Thinking/Activity. Native keyboard handling
  and saved disclosure state remain unchanged.
- The mobile sidebar now slides using a transform rather than layout margins.
  Its backdrop fades in/out; closing immediately releases pointer events. Mobile
  opening also works with a desktop-collapsed sidebar. Desktop collapse retains
  its layout behaviour with the shared easing.
- No JavaScript animation lifecycle, delayed removals, new dependencies or
  settings were added. Menu/dialog close and focus restoration stay immediate;
  streamed text and periodically rebuilt list rows never receive entry motion.
  Reduced motion disables both regular animations and native disclosure motion.

A new private Chromium check covers normal/reduced motion, menu/submenu keyboard
navigation and exact positions, immediate dialog close/focus return, toast
actions, Settings categories, Options, and mobile drawers at 390/320px. It checks
retained drafts/uploads, stationary streamed messages, unchanged settings and
no model runs or API writes. The recovery check waits for expanded note content
to become visible before reading it, now that that disclosure animates too.

Validation: all 476 tests passed, including 47 JavaScript regressions and all
four private Chromium checks. The new motion/recovery checks passed again in
a focused run. Changed-test lint and `git diff --check` pass. Only the existing
Starlette/httpx deprecation warning remains.

## Animate individual tool details and results (2026-10-02)

The user pointed to the Bash/Read cards with Input/Output and failures, rather
than the surrounding Activity disclosure, as the next place for subtle motion.

- Individual tool details use a short intrinsic-height grid accordion with
  opacity and chevron transitions. A small content wrapper replaces the old
  display:none switch; no animation timers, fixed maximum accordion heights or
  provider-specific logic are needed. Input/output text remains stationary.
- Visible sections and newly inserted result icons fade briefly. Existing text
  updates do not restart those fades; running spinners keep their existing
  behaviour. Tool-head hover and status-colour changes also transition gently.
- Closed bodies become inert and aria-hidden immediately, independently of the
  visual collapse, so hidden scrollable output cannot take keyboard focus.
  Opening/failure rendering exposes them immediately. Subagent bodies retain
  their on-demand lifecycle; own-card selectors keep an expanded parent from
  opening a nested card or rotating its closed chevron.
- A browser regression exposed a delayed programmatic scroll event arriving
  after a result had grown. ThreadScroll now recognizes its own applied scroll
  position rather than mistaking that event for user readback. Actual scroll-up
  still pauses immediately; anchor compensation cannot silently resume following.
  This is shared by ordinary turns and on-demand subagent threads.

The Chromium motion fixture reproduces long Bash input, failed output and a
failed Read card without executing commands. It observes intermediate heights
on opening/closing, rapid reversal, immediate failure visibility, keyboard
exclusion while closing, stable command text, 320px width, following and readback
anchors, under both normal and reduced motion. JavaScript checks cover disclosure
state/accessibility and delayed-scroll/anchor-compensation races. Focused
frontend, motion, activity and recovery checks passed; no real model or user data
was used.

Validation: all 476 tests passed, including 49 JavaScript regressions and the
four private Chromium checks. Frontend/motion checks passed again after adding
the nested-helper disclosure case and explicit instant reduced-motion collapse.
Changed-test lint, JavaScript syntax and `git diff --check` pass. Only the
existing Starlette/httpx deprecation warning remains.

## Scan-friendly tool headers and explicit outcomes (2026-10-02)

The user approved a readability round after motion: prefer existing descriptions,
make statuses explicit, show a concise failure reason and check the chat flow on
mobile. This is shared frontend presentation; execution, saving, settings,
provider tools and fallback policy are unchanged.

- Tool headers retain their native name, then prefer a non-empty input description
  over the raw command. Path/search/command fallbacks keep existing shortening;
  the full native name and caption have tooltips, and full Input/Output remain
  unchanged. Permission explanations still use the actual resource/command, not
  the descriptive caption. No model is called to generate a summary.
- Plain tool cards explicitly show Running, Completed, Failed or Stopped, alongside
  their existing spinner/result icon. Unfinished calls settle as Stopped rather
  than incorrectly claiming completion. Labels are the same live and on replay.
  Late input/name metadata enriches an existing result without resetting its
  outcome. Subagent connection ownership/lifecycle is unchanged.
- Failed calls retain the task description and show a separate short diagnostic,
  such as Exit code 2 or File does not exist. Structured error/message/exit-code/
  stderr fields and reported text are supported; missing/unknown diagnostics get
  an honest fallback. ANSI escapes and incidental Note context are omitted only
  from the preview; the original output is preserved under Output. The reason
  stays visible when details are collapsed.
- A compact two-line header separates task information from the error preview.
  Narrow-card container layout puts status on its own row, leaving room for the
  description even on mobile or inside a helper. Long native names/captions are
  ellipsized, not removed. Duplicate results retain the status icon and existing
  output text nodes, avoiding repeat fades or disrupted selection. Valid zero/
  false output payloads are displayed rather than treated as missing.

JavaScript checks cover descriptions/fallbacks, raw input/output preservation,
reported diagnostics, late metadata, duplicate result updates, replay and stop.
The private Chromium motion check verifies the new labels/previews and narrow
layout alongside animation, focus and reading-position checks. The fallback
recovery fixture now runs Retry → stop → choose another model at 320px, retaining
draft/upload/Options and correct ownership. Questions, navigation, Settings and
other recovery flows passed their focused Chromium checks too.

Validation: all 476 tests passed, including 52 JavaScript regressions and all four
Chromium checks. Focused frontend/motion/recovery checks passed again after the
duplicate-text preservation refinement. Changed-test lint, JavaScript syntax
and `git diff --check` pass. Only the existing Starlette/httpx deprecation warning
remains. No real model was called or existing user conversation edited.

## Shift+Tab cycles the message box's modes (2026-10-02)

The user requested keyboard cycling between Plan, Build and other modes.

- Shift+Tab is handled only by the message textarea, using the existing shared
  mode-setting commands. It follows the same choices/order as the mode picker,
  including custom modes and No mode for chat providers, wraps around, and
  normalizes Build to the existing default. Provider filtering is unchanged.
- The shortcut does not submit a slash command or clear any draft text, change
  selection, discard uploads or reopen Options. The mode label refreshes in place
  and the existing mode toast indicates the successful change. The picker tooltip,
  General keyboard help, README and textarea aria-keyshortcuts expose the shortcut.
- Quick discrete presses serialize saves, rereading the current mode after each
  request instead of racing PATCH responses. Submission/queueing waits until
  cycling finishes; stopping an active run remains available. Errors stop the
  queued changes and preserve the draft; detached composers discard pending
  cycles rather than applying them in another conversation.
- Native backwards navigation outside the textarea, plain Tab/slash completion,
  modified Tab combinations and IME input remain unchanged. Held-key repeats are
  ignored. With fewer than two selectable modes the shortcut is not intercepted.
  Switching modes during a run keeps the stream and applies to subsequent turns,
  using existing provider/core semantics rather than changing the active agent.

JavaScript coverage tests wrapping/custom/No mode choices, draft/upload/Options
retention, serialized saves, submission and Stop during saving, failures,
navigation and IME/modifier handling. Private Chromium checks cycle through
welcome modes, then delay an active conversation's save while pressing the
shortcut twice. They check persisted mode/order, the same SSE connection, focus,
draft/upload/Options retention and cycling at 320px. Existing slash completion
and modal Shift+Tab focus trapping also continue to pass. The wait predicates
read imported state synchronously so they actually wait for the saved mode.

Validation: all 476 tests passed, including 58 JavaScript regressions and all
four private Chromium checks. Frontend/activity checks passed again after
extending caret-selection and navigation-during-save coverage. Changed-test
lint, JavaScript syntax and `git diff --check` pass. Only the existing
Starlette/httpx deprecation warning remains. No real model was called or existing
user conversation edited.

## Open a shell in the conversation's work container (2026-10-02)

The user requested access to a work-container shell from Relay.

- A Shell picker and `/shell` open an on-demand terminal dialog in a local
  conversation's actual working folder/container, including isolated worktrees
  and chat scratch folders. No ProviderBox, host shell, provider session or model
  request is used. Remote computer tasks and discarded tasks do not offer a
  misleading local shell; missing code folders report an error rather than being
  silently recreated. Local chat scratch folders are created as usual.
- `terminal.py` attaches `podman exec -it … bash -i` to a PTY, acquiring its
  controlling terminal in a small exec helper instead of using preexec_fn in a
  threaded server. Async fd readiness streams raw bytes without blocking the
  server, preserving ANSI and split UTF-8. Columns/rows update the PTY and notify
  the process. Initial local echo is suppressed while Podman enters raw mode.
- The WebSocket checks the existing login cookie/conversation ownership (or the
  existing loginless account) and the browser origin. It reports container/startup
  errors, with no host fallback. Input and resizing belong to the shell, not chat
  events. Cancellation-shielded cleanup cancels fd readers, waits for startup,
  closes descriptors, reaps the exec process and ends only processes bearing this
  shell's unique environment tag. Other shells and agents are not stopped.
- Containers count open shells so the idle scheduler leaves them warm. Release
  updates last-use time, including startup failures. The interactive rc sources
  the ordinary `.bashrc` and adds the existing disaster guard via a DEBUG trap;
  a stubbed remover verifies that a blocked command does not execute or end Bash.
  The guard script is copied atomically into the already-mounted exchange folder,
  avoiding changed container mounts/specs or a needless image rebuild.
- Vendored xterm.js 6.0.0 and fit addon 0.11.0 implement completion, Ctrl+C,
  Escape, selection, ANSI, editors and responsive layout. Assets load only when
  opening a shell, including waiting for CSS before measuring it. Uvicorn gets
  the pinned pure-Python wsproto 1.3.2 transport. No CDN is used. Per-page CSS
  nonces allow xterm's dynamic renderer/viewport styles without relaxing the
  existing CSP; WebDAV keeps its unconditional sandbox policy.
- The dialog preserves the existing composer, draft/uploads/Options and SSE
  connection, returns focus on close and disposes its renderer/socket/observer.
  Shell Tab/Escape do not invoke Relay's completion or close the dialog; touch
  keys send Tab/Ctrl+C/Esc. Exit/disconnect shows a factual status and New shell
  starts a fresh shell rather than pretending it resumed a lost process.

Unit/protocol checks cover ownership/loginless use, invalid messages/sizes,
missing/discarded/remote folders, Podman errors, actual PTY Unicode/resize/Ctrl+C,
guard behaviour, independent shells and cancellation/startup cleanup. Private
Chromium uses a real scratch work container and worktree with a fake streaming
agent: it checks file writes stay off the source branch, completion, Unicode,
Ctrl+C, nano, close/reopen/exit, 320px layout, focus, retained drafts/uploads/
Options, the same agent EventSource, `/shell`, scratch chats and zero CSP errors.
All temporary containers/volumes are removed; no real model is called.

Environment limitation found and reported to the user: this host's Podman/OCI
runtime refuses live exec-PTY resizing (`openByHandleAt failed: operation not
permitted`). Initial dimensions and all other real-shell checks work. The real
browser test identifies only this explicit runtime error, checks resize delivery
and layout, and retains unconditional PTY resize coverage outside Podman; other
resize failures still fail. No host configuration is changed or environment
failure misrepresented as a Relay bug. README documents reopening a shell at the
desired size until the runtime is fixed.

Validation: all 498 tests passed in the full run, including the real-container
shell check and the four existing Chromium checks. The final 24 terminal/browser
checks passed again after adding startup-cancellation and per-page nonce coverage
and verifying a newly opened phone shell receives its correct initial PTY size.
Focused lint, JavaScript syntax and `git diff --check` pass. Only the existing
Starlette/httpx test-client deprecation warning remains. No settings, real model
calls or existing user conversations were changed.

## Release candidate 1 (2026-10-02)

The user handed development back to Claude to make release candidate 1, with
a free hand: earlier constraints may go, Relay's home is now an i5 server with
24 GB of RAM instead of a Raspberry Pi, security never at usability's cost.
GPT-6.1 Sol (high) reviewed the release in parallel, read-only.

- **No lost work from subagents.** The biggest risk the reviews of the
  subagents' copies found: a copy with changes that weren't handed over went
  with its caller's run (a stopped caller, a failed or conflicting subagent).
  Copies now describe themselves (`fork.json`), outlive the run and a restart
  while they have changes (`forks.tidy` at startup removes the rest), and the
  subagent's conversation offers Apply (with conflict markers) and Discard
  (`POST /api/conversations/{id}/copy`). Going on with a subagent whose copy is
  gone makes a new one from the caller's current files (`runs._own_copy`).
  Smaller fixes from the same review: a failed or cancelled copy leaves nothing
  half-made and no subagent that never ends; `checkout-index -u`; escaped
  exclude patterns; a container that can't be removed doesn't keep the copy.
- **A new server's first answers** waited without a word, and unstoppably, for
  podman's check and the image build. They show an activity step now and can be
  stopped while waiting (`RunManager._containers_ready`).
- **Servers:** the system unit sets `XDG_RUNTIME_DIR` (rootless podman under a
  system service), `install-relay.sh` turns on lingering in both modes (the
  runtime folder and the user service exist from boot), the README installs on
  a server and says how to upgrade and back up, four subagents at once by
  default, and the Pi went from texts and comments.
- **`relay backup [FILE]`** (`relay.backup`): one `tar.gz` (mode 600) of the
  data folder with a consistent copy of the database (SQLite's backup API),
  while Relay runs.
- `relay doctor` warns about git older than 2.40 (subagents' merges).
- **Found in the live smoke test:** a writing subagent on opencode failed at
  once (opencode: ENOENT for its copy). Boxes outlive Relay's restarts, and the
  old `remove_all()` removed and re-made the copies' folder at every start, so a
  running box saw the removed one. `tidy()` keeps the folder; `Boxes.drop_stale`
  at startup (no runs going) removes boxes whose view of it has another inode.
- **Restart Relay** in Settings → Limits → Server (after a `git pull`);
  `restartRelay()` has a module of its own (`restart.js`).
- A subagent's copy gets the caller's **small ignored files** (`.env`, local
  settings: at most 1 MB, 200 files), not ignored folders; they don't go back.
- **GPT-6.1 Sol's review** (high effort, run as a Relay conversation in Plan
  mode on a detached worktree of the release: the first try, `opencode run
  --agent plan`, stalled when its read-only tools were refused without anyone
  to ask) found ten problems, all valid, all fixed:
  `tidy()` removed copies it couldn't check; `fork.json`'s paths didn't follow a
  move; a file that changes during `relay backup` spoiled the archive;
  Apply/Discard could race a subagent's new run; a run waiting for the box
  image couldn't be stopped; a bare Debian has no pip; an old `vendor/` missed
  a new dependency after `git pull` (now stamped with `requirements.lock` and
  brought up to date by the launcher); removing stale boxes could race a run
  start; moved settings applied only at the next start; "Rewind to the start"
  lacked its Undo.
- **Two more review rounds** on the fixes found four new problems in them
  (fork.json rewritten in place, hard links and named pipes in backups,
  concurrent `vendor.sh`) and one gap (a failed rewrite of fork.json during a
  move left Apply aiming at the old folder: Apply now takes the caller's folder
  from the database). Accepted as known limitations, in the release notes:
  Stop during the seconds of container creation, and stale-box cleanup skipped
  when a run is already going at startup. Final verdict: **GO**.
- Tidying the workspace found about 90 leftover work containers: the UI
  walkthrough never removed those of its throwaway Relay, and deleting a chat
  left its container to the idle cleanup. Both remove them now.
- Not done, on purpose: running a copy's commands in the caller's container (so
  what was installed there is there too). Every work container would need the
  copies' folder mounted, so every one would be made anew at the upgrade, and
  with it what the user installed in them.
- **The UI walkthrough had rotted** since the containers replaced permission
  prompts; fixed to match the app as it is (and it now belongs in AGENTS.md's
  routine).
- Version **1.0.0rc1**; [RELEASE-NOTES.md](RELEASE-NOTES.md) says what's in it,
  how to upgrade from a development snapshot and what's known not to work.

## A short note, `relay help`, and `relay` on the PATH (2026-10-02)

ChatGPT, working on Relay through opencode: "A lot of infrastructure in every
message's context. The long execution path and the full list of commands are
usable, but cost attention and tokens. A compact pointer with discovery would
be more elegant." Measured: opencode's agents got about 1,000
tokens of Relay (the 60-character script path twice, all 13 commands, the
container paragraph) plus every skill's description at each new session;
Claude Code about 400 tokens in its system prompt, every turn; and every call
spelled out the path. The user chose the full version, with `relay` on PATH.

- **The note** (`tools_script.note`) is about 150 tokens now: what Relay adds,
  `relay help`, that commands run in a container as root, that Relay records
  every turn and never commits, the skills by name; a computer task's focus
  and a larger repository's git rule stay, since without them agents do the
  wrong thing. **`relay help [TOPIC]`** (a `help` tool Relay answers, so it's
  what's there for that conversation now: computers, skills, limits) has the
  command list, the container's contents, the task record, subagents,
  computers, and the skills' descriptions.
- **`relay` on PATH without making a container anew:** a new mount or a file
  in the image would have changed every work container's spec, and making them
  anew loses what the user installed in them. `container/bin/relay` is in a
  folder every work container mounts already; every agent command's
  `podman exec` gets `PATH=<that>:<the image's PATH>` (`Containers.agent_env`,
  the image's PATH asked once) and `RELAY_URL_FILE`. The dispatcher runs
  `$RELAY_TOOLS` when the agent's program says whose it is (Claude Code,
  Copilot, command agents: their env, passed on by `relay-shell` and the shell
  scripts), else asks Relay (`GET /tools-for?folder=`, only from this machine,
  as the containers share its network) for the agent working in that folder,
  the most specific one: opencode's shared service has no conversation of its
  own. Two agents in the very same folder at once: it says so.
- **Found live:** opencode's agent ran `relay help task` and `relay agents` at
  once; Claude Code's got "relay: command not found". It runs `bash -c -l`, and a
  login shell sets PATH anew from Debian's /etc/profile. `relay` is also an
  exported bash function now (`BASH_FUNC_relay%%`, from the environment, which
  a login shell keeps). Both work; no existing work container was made anew
  (their creation times compared before and after).
- `relay-shell` changed, so the box image is built anew once (boxes hold
  nothing of the user's).

## Relay 1.0.0 (2026-10-02)

The user closed the 1.0 chapter: release candidate 1, plus the short note,
`relay help` and `relay` on PATH, is **1.0.0** (tag `v1.0.0`). Next: Relay
AgentOS (1.1), the user's design, in phases: one vocabulary for the boxes
(ProviderBox, WorkBox, GovernorBox, ProductionBox), the Governor with AgentOS
tasks replacing Scheduled, and ProductionBoxes behind Relay as a proxy.

## AgentOS, phase 1: one vocabulary for the boxes (2026-10-03)

ProviderBox stays; the work container is a **WorkBox** now, in everything
people and agents read (README, AGENTS.md, UI texts, the agents' note and
help, docstrings and comments); a "Boxes" overview in the README names the
two AgentOS adds, the GovernorBox and the ProductionBox. What podman knows
stays as it was (`relay-<key>`, `relay-home-<key>`, the labels, the WorkBox
image): renaming those would make every WorkBox anew, and lose what the user
installed in them. The ProviderBox image is built anew once (comments in its
files changed).

## AgentOS, phase 2: the Governor (2026-10-03)

The user's design, with their choices: Relay wakes the Governor only when
something is due (or for a round of its own), never just to look.

- **The Governor** (`relay.agentos`) is a code conversation of its own
  (`agentos_role = "governor"`, hidden from the chat list) in the hidden
  `governor` mode (`modes.GOVERNOR`: steer, don't do; check; intervene; stay in
  budget; the user's chats are theirs). Its folder is its notebook
  (`RELAY_HOME/agentos/<user>/governor`), so its WorkBox, the **GovernorBox**,
  has no project: it can't do the work itself. Its provider and model are the
  first usable agent of a fallback loop the user approves; the loop takes over
  when one stops (`fallback_loop`).
- **Capabilities** are Relay tools only its runs get (`delegation._tools_for`
  gives it `agents` and `AGENTOS_TOOLS`, not `delegate`, so all it starts is a
  managed chat the user sees): task.list/done, chat.list/create/send/read/
  delete, agent.spawn, chat.fork (a user's own chat: their permission, asked
  like a computer action, then a managed fork), notify.intervention. As MCP
  tools `agentos_…`, as `relay agentos CAPABILITY key=value` with the
  message on stdin; `relay help agentos` lists them.
- **Managed by AgentOS** (`conversations.managed`): what it makes or forks; a
  compass in the sidebar and a chip in the header. It can't read, send to or
  delete anything else.
- **Tasks:** `schedules` got `governed` rather than a new table: a task is the
  Governor's (it chooses the agents) or a prompt for a chosen agent, as
  Scheduled was; nothing to migrate. `/schedule` stays (on in the same chat).
- **Waking** (`Scheduler.wake_governors`): due governed tasks per user wake the
  Governor once with a briefing (the tasks, managed chats, open interventions,
  the budget); otherwise only its round (`round_hours`, default 16). A busy
  Governor leaves the tasks due for the next tick. AgentOS off: the task notes
  so and waits for its next time. No approved models: an intervention, once a
  round.
- **Budget per wake-up:** agents started (chat.create with a task, chat.send,
  agent.spawn) and the cost of managed chats and the Governor since the wake-up;
  over it, the capability says so and to send an intervention.
- **Interventions** (`agentos_interventions`): on the AgentOS page with
  Resolved, and through ntfy/webhook (`Notifier.intervention`).
- **The AgentOS page** replaced Scheduled (`/agentos`; `/scheduled` still
  opens it): the Governor (on/off, approved models, round, budget, Wake now,
  Open), interventions, and the tasks with "Who: the Governor decides / an
  agent you choose".
- The UI walkthrough caught inline styles the Content Security Policy blocks
  (number fields): classes instead.
- **Live, with the user's free-model loop** (the Governor on opencode/big-pickle):
  a governed task "have an agent create hello.md here" run now. The Governor read
  its notebook, made a managed code task on another free model that wrote the
  file, had a third model verify it independently, refused that verifier's
  "PASS" without evidence until it showed `ls` and `cat`, recorded the task as
  done and updated notes.md; it kept to its budget and its GovernorBox had no
  `~/projects` in it. An earlier "Wake now" (a round, nothing due) made it
  rehearse rather than do the task, rightly. `relay agentos chat.delete ID`
  without `id=` failed: a bare word is the id now. All of it removed afterwards;
  AgentOS is off again, without a loop.
- **The Governor makes tasks too** (the user asked): `task.create`,
  `task.update`, `task.delete`. A task it makes is `managed` (shown "by
  AgentOS"), its own to change and delete; the user's tasks it only carries
  out. By default a task it makes is its own to carry out; `governed=false`
  with a provider makes a prompt for a chosen agent. When comes in a few words
  (`schedules.parse_when`: every 72 hours, daily 08:00, weekdays 09:00, weekly
  mon,wed 08:00, once 2026-10-05 09:00); a project folder must be in a
  workspace root, as for the user's own tasks. Its instructions say to make a
  task for what should happen again later.

## AgentOS, phase 3: ProductionBoxes (2026-10-03)

The user's choices: services are reached through Relay as a proxy, and a
service's data lives in a volume of its own that survives promote.

- **`relay.production`**: a service (`services`) is a name, a start command
  (listening on `$PORT`, data in `$DATA_DIR`), a public port and an optional
  health path. A version (`service_versions`) is a frozen copy of the code in
  `RELAY_HOME/production/<service>/<n>/<name>`, run as a **ProductionBox**:
  `podman run -d --restart=on-failure` of the WorkBox image, host networking,
  an internal port of its own (21000–28999), the data volume
  `relay-data-<id>` at `/data`. Its label is `io.relay.production`, not the
  WorkBoxes' `io.relay.managed`, so idle-tidying never stops a live service.
- **The proxy** is Relay's own: an asyncio TCP listener per service on its
  public port (Relay's host), forwarding each connection to the live version's
  port. Any protocol, websockets too; promote and rollback switch its target,
  so the address never moves. Started with Relay (`Production.start`, which also
  starts or remakes the live boxes), closed when it stops.
- **promote**: copy the folder (without `.data` and `.git`), snapshot the data
  (`podman volume export`), start the new version, wait until its health check
  passes (90 s, or until its box stops), then switch the proxy, stop the old
  box and archive it; a version that doesn't come up is removed and marked
  failed with its log, and the live one stays. **rollback** takes an archived
  version through the same path. Archived and failed versions go after 7 days
  (`tidy`, hourly), with their snapshot.
- **clone**: the live code, plus the data volume exported into `.data`, as a
  managed code task (`anywhere`: under `production/<service>/work/`) with the
  agent the Governor names.
- **Health**: every minute (`Scheduler.tick` → `check`); a newly failing
  service wakes its user's Governor with the reason (busy: tried again next
  check), or, AgentOS off, is an intervention; once until it's well.
- The Governor's capabilities: service.list/logs, workbox.clone/delete,
  production.promote/rollback; its instructions: a live service changes only
  through a clone. The user's: the Services section on the AgentOS page and
  `/api/services` (a code folder in a workspace root, a free port that isn't
  Relay's, a name of a-z, 0-9, -).
- **Tested with real podman** (`tests/relay/test_production.py`, skipped
  without it): a tiny web service with a counter in `$DATA_DIR` behind the
  proxy; version 2 takes over at the same address with the count kept; a
  version that exits doesn't; rollback; a stopped box fails the health check
  once; a clone has the code and the counter; an archived version goes after 7
  days.
- **Found while trying it out live:**
  - The user had used AgentOS overnight and switched their Governor to Claude
    Code by hand. Waking it set the approved loop's opencode agent but kept
    Claude's session id, which opencode refused (HTTP 400). A switch of agent
    now starts a session of its own (with the conversation handed over), as a
    switch by the user does.
  - The user asked their Governor for a new website, and it couldn't start one:
    a code task needs an existing folder, and the GovernorBox has no projects
    to make one in. `chat.create folder=…` now makes a new folder when it's in a
    workspace root, and the briefing names the roots.
  - Cleaning up an earlier test removed `RELAY_HOME/agentos`; the opencode box
    kept the removed folder, so the next Governor run couldn't find its
    notebook. That folder is now mounted in every box like the subagents'
    copies, and `Boxes.drop_stale` watches both.
- **Live with the Governor on GPT-6.1 Sol** (the user's wish: Sol rather than
  Claude for the Governor in tests; a loop "AgentOS (Sol)": Sol → Terra). Woken
  by the failing service, it read its notebook and the service's log, found the
  invalid JSON, picked up the user's request from the night before (a website
  with election results: a managed code task in a new project folder, and a
  task "every 72 hours, add one election"), cloned the service twice for a
  repair, and when both repair agents failed at once sent an intervention and
  planned a check. The repair agents failed because the clones' folder
  (`RELAY_HOME/production`) wasn't mounted in the busy opencode box: it is
  mounted in every box now, and watched by `drop_stale`, like the other
  folders of Relay's that boxes need.
- **The whole path, live** (Governor on GPT-6.1 Sol, the user's loop "Sol 6.1 →
  Opus 5.5"; the repair agents on free models): after the mount fix and the
  user resolving the intervention, the Governor cloned the broken service three
  times, read the clones' code and tests itself (its file tools may read
  anywhere; Relay's judges let them change only its notebook, and its shell
  runs in the GovernorBox), sent the test agents corrections, re-ran the six
  regression tests itself with a check that the broken config stayed
  byte-for-byte, promoted the best clone (version 2 live, HTTP 200 behind the
  same address, version 1 archived) and removed the other clones.

## AgentOS: what holds the Governor (2026-10-03)

The user asked for an audit-and-improve pass, so that a Governor can run
unattended for long periods without enough authority to corrupt Relay or
silently cross the user's ownership boundaries. The audit found ten points.
Each is below, with what's enforced now. The user chose to have every WorkBox
made anew once, with fewer mounts.

**Critical**

1. **The allow-list wasn't enforced.** A token got only its own tools listed,
   but MCP dispatch took any name. The Governor's token could call `delegate`,
   `computer_run`, `commit` and `restore`. `Delegation.handle` now refuses any
   tool that isn't the run's own (`allowed()`: what `tools/list` shows, plus
   `help`), and AgentOS's runs log each refusal.
2. **Tokens in shared files.** Every WorkBox, the GovernorBox among them,
   mounted the machine's cache, where each conversation's tools script lives
   with its token in both path and content. `/tools-for` also told any local
   caller the script for any folder.
   - WorkBoxes no longer mount the cache, attachments or handed-over
     transcripts. Only the skills, `container/`, and a `public` folder with
     Relay's address and a generic tools script (no token).
   - A command gets its conversation's address in `RELAY_MCP_URL` (from the
     script path the provider's process passes).
   - Without it (opencode's shared service), `/tools-for` answers only for the
     box's own folder. Relay signs that folder for the command (`RELAY_BOX`,
     `RELAY_BOX_KEY`: an HMAC with a key that lives only in Relay's memory).
3. **Reads anywhere.** The judges let every agent's file tools read anywhere,
   and the Governor did so 21 times (`/home/workstation/*` once).
   - `agentos.read_boundary` now gives AgentOS's conversations (the Governor,
     managed chats, their subagents) a list of what they may read: their
     folder, their own attachments and handover, the skills, `container/` and
     `/tmp/opencode`.
   - The Governor may also read its managed chats' folders and the services'
     versions and clones (to review its agents' work, as it did live).
   - opencode's `external_directory`, Claude Code's Read/Glob/Grep/LS and
     ACP's read paths are checked against that list.
   - The list is asked anew on each request, so a clone made during a run
     counts.
   - The user's own agents still read anywhere.

**High**

4. **Promotion took any managed folder.**
   - `workbox.clone` records the clone (`service_clones`: service, version it
     was made from, Governor run).
   - `production.promote` goes through `Production.promote_clone`. That accepts
     only a clone of that service, made from the version that's live now, and
     only while it isn't working.
   - A stale clone is refused, with what to do instead.
5. **No serialization.**
   - `Production.operation` allows one promote, rollback, restart or delete
     per service at a time. A second is refused ("busy"), and the operation is
     noted in `services.operation`.
   - Deleting a clone that's being promoted waits.
   - Tidying skips a busy service.
6. **No recovery.**
   - Versions now go building → starting → verifying → live (or failed), and
     the switch (new live, old archived, service pointer) is one transaction.
   - `Production.reconcile` at start fails in-flight versions and removes
     their boxes. A rollback that broke off leaves its version archived.
   - It keeps exactly one version live, stops the boxes of the others, and
     removes boxes of unknown versions or services.
7. **Provenance.** Each version records:
   - the clone it came from and the version that clone was made from;
   - the agents that worked in it;
   - who promoted it (`governor:<run>` or `user`) and when;
   - the version it replaced (the rollback target);
   - Relay's own health check as its validation.

**Medium**

8. **A task's turn was implicit.**
   - Governed tasks now have `dispatch_state`: dispatching (claimed before the
     Governor starts, so no second wake-up takes it) → dispatched (with its
     run) → awaiting (the wake-up ended without a report) → idle.
   - `task.done` is accepted only while the task is dispatched or awaiting, and
     only once.
   - A wake-up that breaks off makes the task due again: after 10 minutes from
     a failed run, at once after a crash (`AgentOS.reconcile` at start). After
     3 times the task is given up and an intervention says so.
   - A fallback agent that takes over the Governor's conversation also takes
     over its tasks and budget.
   - Catch-up stays as it was: a time missed while Relay was off runs once.
9. **LLM claims as facts.**
   - Outcomes record by whose word (`outcome_by`: governor or relay); the page
     says "the Governor says" or "Relay saw".
   - `AgentOS.policy` runs before each irreversible capability (promote,
     rollback, the deletes). It runs the deterministic checks in
     `AgentOS.policies`, then, for what the user listed in "Ask me first",
     asks the user the way `chat.fork` does. No answer in 10 minutes is a no.
10. **No audit trail.**
    - `agentos_events` records every capability call and refusal, wake-up,
      task turn, policy decision, version change, clone and reconciliation,
      with actor (governor, relay or user), ok, run, task, service, version
      and details.
    - It's shown as the **Activity log** on the AgentOS page and kept 90 days.

**Tests** (in `test_relay_api.py` unless noted):
- the Governor's token refused delegate, computer_run, commit, restore, wait,
  tell, merge and computer tools;
- a subagent's token limited to approve_permission;
- a user's chat refused AgentOS's capabilities;
- the read boundaries of the Governor, a managed chat and its subagent, and the
  user's chat unchanged;
- `/tools-for` refused without a key, with a forged key, and with another
  box's key;
- a task's turn: no report before dispatch, a single dispatch, awaiting, a
  report taken once, a crash while dispatching or dispatched, and giving up
  after 3;
- the "ask first" policy (denied, then allowed) and a plugged-in check;
- promotion refused for an unrelated folder, another service's clone, a stale
  clone, the user's own task, a busy service and a deleted service;
- reconciliation with a fake podman;
- with real podman (`test_production.py`): provenance after `promote_clone`, a
  stale clone refused, a clone's `.data` kept out of production, and two
  promotes at once (one runs, one is refused as busy).

**Still a limit:**
- The ProviderBoxes still mount the machine's cache. The user's own agents can
  read anywhere, so they could find another conversation's tools script.
  (Fixed later the same day, see below.)
- The `/tmp/opencode` and exchange folders are shared between boxes. (The
  exchange folder too, see below.)
- Read-only modes are instructions, not enforcement.
- The Governor's managed agents may work in the user's project folders,
  because that's how a task about a project is carried out.

**Live** (the Governor on the loop "AgentOS (Sol)", GPT-6.1 Sol). The audit
had also found a real case: earlier that morning the Governor promoted
version 3 of the test service `notes` from a clone of version 1 while
version 2 was live, which is the stale promotion that's refused now.

After the restart (every WorkBox made anew), a governed task ran the whole
path in about 2 minutes:
- `relay` worked in the new GovernorBox through the signed `/tools-for`.
- The old clone was refused ("isn't a clone of notes").
- A fresh clone was made from version 3, and a free model added `/version`.
- The Governor ran the tests in its clone and promoted it. Version 4 went live
  after Relay's health check (HTTP 200), with origin, agents, `governor:<run>`,
  replaced version 3 and the validation recorded. Version 3's box was stopped.
- `task.done` was accepted while the task was dispatched (outcome done, by the
  Governor).

The Activity log shows each step.

The Governor's shell ran the clone's tests in the clone's WorkBox. opencode
asked `external_directory` for that folder, and it's in the Governor's read
boundary: a managed folder is the Governor's to work in.

**Two of the limits, afterwards:**
- **ProviderBoxes no longer mount the machine's cache**, only `relay-skills`
  (Claude Code's skills folder, read-only). The conversations' tools scripts
  are never run in a box: their path only passes through, so the gateway knows
  the conversation.
- **Each WorkBox has an exchange folder of its own**
  (`relay-containers/exchange-boxes/<box>`), mounted where the shared one was,
  so nothing else changes for the shells. A box can't read or plant another
  box's cwd file any more.
  - After a command, Relay moves the cwd file it named to the shared folder
    (`Containers.hand_back`), which the ProviderBox reads.
  - The terminal's guard script moved to the read-only `public` folder.
  - The WorkBoxes were made anew once more for this (home volumes stay).

**What's still open:**
- `/tmp/opencode` stays shared, because opencode tells its models to use it and
  its file tools read there.
- A user's own agent in a ProviderBox could still read another process's
  environment there (`/proc`), with that run's token. The user's own agents
  read anywhere, by choice; AgentOS's can't.

## Production boxes in the sidebar: a shell in the live box, and Demote (2026-10-03)

The user asked for the live services in the sidebar, under AgentOS. Opening one
shows a terminal in its ProductionBox where a chat would be (agents don't work
there). A **Demote** button sets the Governor to work on it.

**The user's choices:**
- The service stays up during Demote. That's the existing clone → promote path,
  with the old version archived for 7 days.
- Demote is only the button: the user tells the agent what to do in the
  WorkBox's chat.
- The shell may write in the live box.

What was already there:
- WorkBoxes stop after 3 idle hours (`stop_idle`).
- ProductionBoxes run with `--restart=on-failure` and are (re)started by
  `Production.start`. Relay itself starts at boot.
- The server rebooted during this round, and Relay and notes came back by
  themselves.

**The shell.**
- `production.Shell` is what `relay.terminal.Terminal` needs of a box:
  `name`, `terminal_argv`, `close_terminal`. So the PTY code, the socket
  protocol and its clean-up are the WorkBox shell's.
- A ProductionBox mounts none of Relay's folders. The shell first copies the
  guard and `terminal.bash` into `/tmp/relay-terminal` in the box
  (`podman cp`). This works for boxes made before this change, like notes v4.
- Ending the shell kills only processes carrying its tag
  (`containers.end_terminal`, shared with the WorkBox shell). The service
  goes on.
- `routes/terminal.py` split its protocol out (`_serve`). The service socket
  (`/api/services/{id}/terminal`) answers only for the user's own service with
  a running live version.

**Demote.**
- `POST /api/services/{id}/demote` → `AgentOS.demote`. It wakes the Governor
  with the reason: clone it with `workbox.clone`, no message, and promote when
  the user or the agent says it's done and it works.
- It's refused while the service is busy, has nothing live, or the Governor is
  still working.
- The audit trail records `service.demote_requested` (actor user).
- `GET /api/services/{id}` lists the service's WorkBoxes: its clones that
  aren't discarded.

**The frontend.**
- `terminal.js`: the xterm and socket core is `mountTerminal`, used by the
  WorkBox modal and the service page (`service.js`, route `/s/<id>`). Leaving
  the page ends the shell.
- The sidebar's "Production boxes" folds open (remembered); the open service is
  marked.
- The New shell button now stays hidden until a shell ends. In the WorkBox
  modal it showed all the time.

**Tests.**
- The API: Demote's briefing, event and refusals, and the WorkBoxes list.
- The terminal socket: only the owner's service, and nothing live gets a
  refusal; a shell's argv and the files it copies in.
- Real podman: a shell in a test service's live box finds its folder, the
  guard blocks `rm -rf /`, and the service still answers after the shell
  closes.
- The UI walkthrough: a real service under Production boxes, `ls` in its
  shell, and the page at phone width.


## ProductionBoxes: an init, and a crashing version refused at once (2026-10-04)

Measuring the test suite showed `test_production.py` taking about 190 of its
430 s. The cause was two things in the product itself.

- **No init in ProductionBoxes.** `bash -lc <command>` was PID 1, which ignores
  SIGTERM. Every stop of an old version (promote, rollback) waited out podman's
  10 s and then killed it; every delete waited 5 s. ProductionBoxes now start
  with `--init`, as WorkBoxes and ProviderBoxes already did, and a stop takes
  about a second.
  - The test checks the old box's exit code: 143 means it was ended by its
    signal, 137 means it was killed after the timeout.
  - notes v4 was made before this; its next version gets the init.
- **A crashing version was only refused after 90 s.** `--restart=on-failure`
  starts it again and again: "stopped" in between, rarely "exited". So
  `_wait_well` waited out `START_TIMEOUT`.
  - It now also counts the box's restarts since the wait began, and treats
    "stopped" as stopped: "it stopped (started again N times)", within a second
    or two.
  - The restarts are counted from where they stood at the start. A box that
    crashed once before can still be restarted by hand (Restart), and the big
    test checks that now too.

`test_production.py` went from about 190 s to 45 s.

## Tests in parallel (2026-10-04)

The user asked whether the suite could run much faster without testing less.
The suite took 7:10 serially, on 3 cores.

- **The product fix above** (ProductionBoxes) took 2½ minutes off.
- **pytest-xdist**, a development tool in `vendor-dev` only: `-n 3 --dist
  loadgroup`.
  - Tests that share something outside their own `tmp_path` run in one worker,
    one after another (`tests/conftest.py`, `SHARED`). The real-podman tests share
    ProductionBoxes' internal ports and the image. The Chromium checks time
    what they see, and one browser at a time keeps them as reliable as alone.
  - The hook that sets the groups must run before xdist's own
    (`tryfirst`). Otherwise the groups are silently ignored, and two production
    tests talked to each other's services.
- **Two races in the UI tests showed up under the load**, and are fixed in the
  tests, not by loosening them:
  - `test_ui_activity`: `locator.boundingBox()` finds the element, then measures
    it. When the header was rendered anew in between, it gave `null`. The boxes
    are now measured in one go in the page.
  - `test_ui_shell`: podman passes a new terminal size on to the box a moment
    later. `stty size` is asked again (up to 20 times) until it changes.
- The suite now takes about 2 minutes in parallel and about 5 serially (`pytest
  -q` without `-n` still works).

## Restarting Relay under systemd broke its ProductionBoxes' output (2026-10-04)

The first restart after the round above left notes failing its health check:
"/" and "/health" closed the connection without a reply, while "/version"
answered.

- **The cause.** Run from a systemd unit, podman sees `INVOCATION_ID` and keeps
  each container's conmon in that unit's cgroup. A stop or restart of Relay
  kills them all.
  - The containers run on, but their stdout and stderr have no reader any more.
  - notes prints a line when its config is malformed (it was broken on purpose
    in an earlier AgentOS test). That print failed with EPIPE, and so did the
    handler. `/version` prints nothing, so it still worked.
  - systemd's stop also waited its full 90 s for the conmons, then killed them.
  - The Governor was woken by the failing health check. It found no cause in the
    logs and changed nothing.
- **The fix.**
  - `relay serve` drops `INVOCATION_ID` from its environment, so every
    conmon gets a scope of its own (`libpod-conmon-<id>.scope`) and outlives
    Relay. This was checked with `systemd-run`: with the variable the container
    died with the unit, without it it ran on.
  - `Production.start` makes a live box anew when it's running without a living
    conmon (left so by an older Relay), or when `podman start` fails. A box
    whose conmon is gone can need two `podman rm -f` tries, so `_start` tries
    twice.
- **Tests.**
  - `serve` hands podman an environment without `INVOCATION_ID`.
  - Real podman: a test service's conmon is killed, and the next start brings
    the box back, answering, with a living conmon.

## Each Relay tidies only its own boxes (2026-10-04)

The live Relay's journal showed two 500s at 23:34 yesterday: "Couldn't start
the provider's box". The first said the name `relay-box-opencode-manager` was
in use, the second that a container's storage was gone.

- **The cause.** At that moment the UI walkthrough was running: a throwaway
  Relay, with real containers, on the same podman.
  - ProviderBoxes are named after the provider only, so both Relays wanted the
    same box.
  - The walkthrough's `Boxes.drop_stale` also removed every box with the box
    label whose folders it didn't recognise: the live Relay's.
  - `Containers.stop_idle` had the same reach. A test Relay would stop the live
    Relay's WorkBoxes as idle (it never saw them used), and remove them after
    14 days stopped.
- **The fix.** Every WorkBox and ProviderBox carries `io.relay.instance`, a
  mark derived from its Relay's data folder.
  - The usual folder (`~/.local/share/relay`) marks nothing, so the boxes made
    before this count as its own, and nothing is made anew.
  - `stop_idle` and `drop_stale` only touch boxes with their own mark.
  - A Relay with another data folder adds its mark to its ProviderBoxes' names
    (`relay-box-<provider>-<mark>`). The live names stay the same.
  - The walkthrough now removes everything with its own mark, its ProviderBoxes
    too (they leaked before). Checked: after a full suite and the walkthrough,
    `podman ps -a` was exactly as before.

## Two services promoted at once got the same internal port (2026-10-04)

Found while reading `_take_over`: "one operation at a time" holds per
service, so two services can promote together (the Governor with parallel
tool calls, or the user and the Governor). A new version's internal port was
chosen with `_free_port` (ports not held by a live, starting or verifying
version in the database, and free on the machine). It was only written to the
database once the box had started, after an `await`. The other promote could
choose the same port in between. Now it's written in the same step as it's
chosen, with no `await` in between. A test promotes two services at once with
boxes that take a moment to start. They get different ports; before the fix,
the same one.

## A box that won't start: an answer, not a server error (2026-10-04)

The two 500s above also showed that a `ContainerError` raised in a route
(here: the composer asking a provider for its efforts, which starts its
ProviderBox) wasn't one of the errors `web.api` turns into a JSON answer. It
went through as an unhandled exception. That meant a traceback in the journal
and a bare "Internal Server Error" in the browser, while the error itself says
what's wrong. It's now answered with 503 and that message.

## A service starting after a reboot no longer wakes the Governor (2026-10-04)

After yesterday evening's reboot the Governor was woken at 23:00:46 with "notes
is failing (Connection refused)". notes was still starting: Relay checks every
service right after its own start, before its boxes are up. That cost a run of
the Governor's model and a misleading intervention ("the same pattern keeps
coming back").

- A service is now reported failing (Governor woken, or the user told) only
  after `FAILED_CHECKS` = 2 failed checks in a row, so after a minute.
- The page still shows a failed check at once.
- A service an operation is working on (a restart, a promote) isn't checked.
  Its box is down on purpose then.
- A test drives the checks with a clock of its own: once, no report; during a
  restart, not checked; twice in a row, reported; once until well; failing anew,
  two again.

## The services' proxy keeps the way back open when a client is done sending (2026-10-04)

`Proxy._connection` closed both sockets as soon as one direction reached EOF.
Some clients send a request and then close their own side (a half-close):
`nc -N`, many TCP protocols, "read until EOF, then answer" services. For them
the service's answer was cut off. The proxy now passes a client's EOF on as a
half-close (`write_eof`) and keeps the way back open. When the service is done
(or a direction breaks), the whole connection ends, as before, so a keep-alive
client can't hold the service's socket forever.

`tests/relay/test_proxy.py` uses plain sockets, without podman:
- a half-closing client still gets the answer (before the fix, an empty one);
- both ways work, and nothing is live → closed;
- the service closing ends the connection for a client that keeps its side
  open.

## One way to bring a box up (2026-10-04)

`Containers.ensure` (WorkBoxes) and `Boxes.ensure` (ProviderBoxes) each held
the same core: compare the spec, start a stopped box made the same way, else
remove it and run it anew with Relay's labels, and report why it couldn't start.
Tonight's instance mark had to be added in both. That core is now
`containers.up`, with `spec_of` and `remove`. `remove` tries `podman rm -f`
twice, because a box whose conmon died with an older Relay may only go the
second time. Each `ensure` keeps only its own concerns: the apt setting in a
new WorkBox, and a ProviderBox in use that stays as it is. The error messages
are unchanged. Checked against real podman: a new WorkBox carries managed, spec
and instance, and a second `ensure` keeps it.

## A failing service is told once, also across Relay's restarts (2026-10-04)

Whether a service's failing had been told (the Governor woken, or the user
given an intervention) was kept in memory only (`Production.alerted`). Each
restart of Relay forgot it, so a service failing for hours woke the Governor
again after every restart. It's now `services.alerted_at`. It's set when the
scheduler tells, and cleared when a check finds the service well or a new
version goes live. The count of failed checks in a row stays in memory: after a
restart it starts over, which only delays a report. The test now also restarts
the count and checks that nothing is told again. Through the scheduler (AgentOS
off), it checks that the user gets exactly one intervention and that this is
kept.

## `relay doctor` reports boxes another Relay left behind (2026-10-04)

A test run that was killed (above) left a ProductionBox running, with a data
volume, from a test Relay whose folder in `/tmp` was gone. No Relay counts it
as its own: the live one leaves unknown services' boxes and other marks' boxes
alone, by design. So it would have run for ever. `relay doctor` now lists such
boxes: another Relay's mark, or a service this database doesn't have, and a
folder (the working directory) that no longer exists. It gives the
`podman rm -f` command but removes nothing itself, because the box may belong
to another Relay that's still in use. Tested with a fake podman covering every
case, and against real podman with a made-up leftover box.

## Two leaks between tests that the parallel runs brought out (2026-10-04)

- **Settings → Limits lived on in the process.** `relay.tunables` keeps them in
  the module. The settings UI check runs a Relay in the test process and saves
  limits, and tests without an app of their own (a `RunManager` straight away)
  then got those values. A question's timeout went missing ("expires"). Serially
  `test_ui_settings` happens to come after `test_run_storage`. Under xdist the
  order differs. Reproduced with those two files in one process; an autouse
  fixture now starts and ends every test with the defaults.
- **The app's own scheduler checked health in the middle of a test.** The big
  production test counted its own two failed checks, but the test app's
  scheduler (every 20 s, on the real clock) could add one in between, so the
  first check already reported. The test now uses a clock ahead of the
  scheduler, whose ticks then skip the check, and starts from an empty count.

Three parallel runs in a row afterwards: 548 passed each.

## After a restart of Relay every WorkBox looked unused (2026-10-04)

The Governor's WorkBox stopped at 01:54, 45 s after one of tonight's restarts,
though it had been used two hours earlier (idle means 3 hours). `stop_idle`
takes a box's last use from memory (`Containers.used`) or from a marker file
(`relay-containers/used/<box>`). Only the old host-side agent shell touched that
file. Since commands go through the ProviderBoxes' exec gateway, nothing wrote
it: the folder was empty. So after each restart, the first tidying stopped
every WorkBox nothing had used since. It was harmless (the next command starts
it again in a moment) but not what "after 3 hours unused" says.
`Containers._mark_used` now updates the marker as well. Every command reaches a
WorkBox through `ensure` (the gateway's `place`), and a closing terminal marks
it too. The test now also asks a fresh `Containers` (a restarted Relay), which
leaves the box alone. Without the fix it stopped it.

## The Governor: settles interventions, sees a service in detail, knows when tasks run (2026-10-04)

The user passed on what the Governor role still lacked.

**Interventions.** An intervention stayed open until the user resolved it.
So it kept coming back in every briefing even after the problem was fixed (a
mobile layout bug that had been dealt with).
- `notify.intervention` now returns the intervention's id.
- The briefing lists the open interventions with their ids and dates.
- `notify.update` gives an open intervention a new text, and records when.
- `notify.resolve` settles one, and needs a reason (`note`).
- The audit trail records both, with what the text was before and the reason
  given.
- A settled intervention leaves the briefings. Under "Needs you" the dashboard
  shows for 3 days what the Governor settled and why, so it doesn't just
  disappear for the user.
- `resolved_by` (`user` | `governor`) and `resolution` are kept with it.

**`service.status`.** This is Production.status as text.
- The live box: its state, since when, how often podman restarted it (crashes),
  its last exit code and time, whether it was killed for memory, its error, and
  whether its conmon still lives.
- The processes in it (`podman top`).
- Relay's last health check, the failures in a row, whether it was already
  reported, and a probe made right now.
- An operation going on, and the latest versions with the first line of their
  note.
- `service.list` also gives each live box's state, from one `podman ps`.
- The service's page has a **Status** button with the same text
  (`GET /api/services/{id}/status`).

**`task.list`.** Each task now gets a few lines:
- whose task it is, and who carries it out;
- when it runs, in the task's time zone, and its next run (or why there is
  none: off, or a one-off that already ran);
- its last run and that turn's outcome, by whose word, when, and with the note;
- whether a turn is with the Governor now, and which attempt.

**Tests.**
- API: an intervention updated and settled, the errors, the briefing before and
  after, the events, and a user's resolve.
- `task.list` with a known outcome time, in the task's time zone.
- `service.status` for a box that's gone.
- Real podman: a running box (init, no restarts, a living conmon, its process,
  a probe answering), then stopped (exit 143, no answer).

## The header without what the message box already says (2026-10-04)

The user found the header's folder chip (`…/1/governor`) and provider chip
(`opencode · localai · openai/gpt-6.1-sol`) duplicates. The pickers under the
message box show the provider and model, and the note under it shows the
folder. At phone width they were already hidden for that reason. They're gone
now. What the folder chip added for a task on its own branch, the project it's
for, is now in the note ("in …/project on branch …"). The "Your workspace" line
above "What can I help with?" went too. The UI walkthrough now finds the folder
and the provider under the message box.

## The status next to the title: a header half as high (2026-10-04)

At the user's request the conversation's status badge ("Working", "Completed",
…) now sits next to its title instead of under it. The header goes from 76 to
52 px, also at phone width. The title gives way (it's shortened) and the badge
always stays whole. The header and badge overflow checks in the UI tests and
the walkthrough pass, at 320 and 390 px too.

## Another Relay's versions take ports from a range of their own (2026-10-04)

During a test run, the big production test got its answer from the live notes
app. The Governor had just promoted notes v5 (09:55, internal port 21002), and
the test's Relay chose the same internal port for its own version at the same
time. Each Relay knows only its own database's ports, and a port is only seen
as taken once something listens on it. The live notes was fine; the test
failed. A Relay with another data folder (a test run, the UI walkthrough; see
the instance mark) now takes its versions' ports from 29000–30999, below
Linux's ephemeral range. The usual one keeps 21000–28999.

## What's left of the providers' usage limits, in Settings → Usage (2026-10-04)

The user asked to see the remaining usage limits per provider where a provider
can tell, asked actively. Before, only Claude Code's per-run rate-limit event
was kept (`model_status.usage`), and only the model picker's tooltip showed it.

**The standardised API.** `Provider.limits()` returns
`{account, service, plan, windows: [{label, used 0–1, resets_at, left}], source,
checked}`, an error, or None. The shared part is in `relay/limits.py`:
`fetch_json`, and one tolerant parser per answer.

**The support layers.** Each asks with its program's login and never renews it
(that would sign the program out):
- `ClaudeCodeProvider`: `.credentials.json` in its config folder, then
  `api.anthropic.com/api/oauth/usage`, as Claude Code's `/usage` asks. It uses
  the answer's `limits` (session, weekly), else `five_hour`/`seven_day`.
- `OpencodeProvider`: the profile's OpenAI sign-in, from `opencode.db`'s
  `credential` table (opencode 2, opened read-only), else `auth.json`. Then
  `chatgpt.com/backend-api/wham/usage` with `ChatGPT-Account-Id`, as Codex's
  `/status` asks: the primary (5-hour) and secondary (weekly) windows.
  Profiles signed in to the same ChatGPT account are one login.
- `CopilotProvider`: `~/.copilot/config.json` (it has `//` comments), with the
  token of `lastLoggedInUser`. Then `api.github.com/copilot_internal/user`:
  premium requests and chat messages, until `quota_reset_date`. Completions
  (editor autocompletion) are left out.

**The route.** `GET /api/usage/limits`:
- every enabled provider at once, each in a thread with a timeout;
- one row per login (others named under "also");
- an answer reused for 120 s (`fresh=1` asks again), kept on the `Relay`
  object, not in a module;
- when a provider can't be asked now (an expired login), its last run's windows
  with how old they are;
- one provider failing doesn't stop the rest.

**The page.** "Usage limits" is above the cards, loaded separately. Per window
it shows a thin bar of what's left, with "NN% left · resets …" or "187 of 200
left". Below 20% the bar turns warning, at 0 danger; the text always says it.
There's a Refresh button.

**Checked live first.** All three endpoints were asked once with the real
logins, printing the answers' shapes without tokens or ids. The parsers were
written to those, and the tests use them (ids left out):
- Claude Pro: 5-hour and weekly;
- ChatGPT Plus: 5-hour and weekly;
- Copilot Free: chat 187 of 200; no premium requests in that plan.

**Tests.**
- The parsers, odd answers included.
- Each support layer with fake logins: the right headers; an expired login
  asks nothing; a config with comments; a profile without OpenAI.
- The route with stub providers: one row per login, the cache, the fallback
  to the last run, a provider that throws.
- An autouse fixture keeps every test from asking for real.
- The walkthrough waits for the section.

While writing the tests I overwrote the existing `tests/relay/test_limits.py`.
I restored it from git; the new tests now come after the old ones.

## Managed chats and tasks: their icon in Relay's green, with "os" (2026-10-04)

In the sidebar, a conversation managed by AgentOS now has its usual chat, code
or computer icon in the accent green, with a small "os" at its bottom left. It
replaces the compass after the title and carries the same tooltip. The icon
keeps its `mode` class, so the walkthrough's checks still find one per row.
The walkthrough marks one of its chats managed and checks the "os".

## /timed: an agent kept at it for a while, stopped at its max (2026-10-04)

`/timed [min] max [optimal] [task]`, in minutes (`90`, `1h30`). The user chose
**min hard, optimal soft**:
- **Before min**, every stop gets "Please continue. Per the timed request, you
  are free in <time to min>. Keep working until then."
- **Before optimal**, it's asked once whether something useful is left.
- **After optimal**, nothing is sent.
- **At max**, the agent is stopped, its fallback loop with it.

Optimal defaults to 15% before max, or to min when there's one (no soft part).
The task can follow and is sent like a message.

- `relay/timed.py`: `parse`, `phase`, `next_prompt`, `time_up`, `describe`.
  `conversations.timed` keeps `{started, min, optimal, max, asked, ended}`.
- `RunManager._timed_after`, at the end of a run:
  - after a queued message or a fallback agent went on, it does nothing;
  - when the run succeeded, it may start Relay's turn (`relay_action` kind
    `timed`, shown as "Relay kept … at it");
  - the user's Stop (interrupted, but not by Relay's own shutdown) ends the
    timer.
- `Scheduler._timed_over`, every tick: past max it stops the run, drops a wait
  for a usage limit's reset, and marks it `ended: max`. `_go_on` (a fallback
  agent) and `_resume` (a reset) start nothing once the time is up. A timer the
  user switched off holds nothing back.
- UI: the `/timed` command (with `off`, and alone for how it stands), a bar
  above the message box with the times and **Stop timer**, and after max
  "Stopped at …: the time was up".

Tests:
- the parser and the parts;
- what `_timed_after` and the scheduler do, with `start` and `stop` caught, so
  a hard part can't run on for ever with the fake agent;
- one real run in the soft part: asked once, then nothing;
- the route;
- the walkthrough: `/timed 2 …` shows the bar, sends the task, Relay's turn
  follows, and Stop timer removes the bar.

A task that starts with a number (`/timed 60 3 tests`) would read the 3 as a
time. Start such a task with a word.

## A review of these days' work (2026-10-04)

The cloud review only covered the uncommitted AGENTS.md line, so at the user's
request I reviewed these two days' commits myself (27 commits, ~1,700 lines
in `src/`). I read every diff and ran bug-pattern lint checks (ruff F, E9, B,
PLE). Found and fixed, each with a test that failed first or a new check:

- **A ProductionBox shell that couldn't be prepared crashed its socket.**
  `Shell.terminal_argv` raises `ProductionError`, which the terminal socket
  didn't catch. The user saw a dead terminal instead of the reason.
- **`service.status` said "restarted 0 times by podman (it crashed)".** That
  misleads the Governor, the one reader that acts on it. It's "never restarted"
  now; the crash is only mentioned when there were restarts.
- **`relay doctor` could crash** on a `podman inspect` that timed out in its
  leftover check. The limits route's `zip` is strict now.
- **`/timed` after a restart did less than the README said.** The README said
  the timer survives a restart. It did, but the interrupted turn simply ended,
  so nothing went on until max. Now, at the scheduler's first tick, an agent the
  restart cut off before min goes on. Not after the user's own Stop, and not
  while it waits for a usage limit's reset.
- **A command that failed lost what was typed.** The composer cleared the text
  before running the command, so `/timed 10 5 <a long task>` (min after max)
  threw the task away. It comes back now. The walkthrough checks this with
  `/mode nonexistent`, which fails in the page itself: an HTTP 400 counts as a
  console error there.

I looked for more and found nothing that needed changing:
- the proxy's half-close (a client that never closes holds the connection only
  while the service keeps it);
- the order of `_timed_after` after a queued message or a fallback agent;
- `_timed_over` against a run's own end (`ended: max` is written before the
  stop);
- the service page's refresh;
- the usage-limit parsers on odd answers.

## A service's WorkBoxes folded under the newest (2026-10-04)

The user's chat list had "notes: work on version 1" up to 5, each in its own
row. Now only the newest is listed, with an arrow and a count. A click opens
the older ones under it, indented, with their usual icons, menus and unread
dots.
- `web.summary` gives a conversation's `clone` (`service_id`, `service`,
  `version`), from `service_clones` in one query for a whole list
  (`clones_of`, next to `queued_messages`).
- `activity.js: foldClones` decides, before `sidebarGroups`:
  - the head is the highest version;
  - one working or waiting for you, or open, stays in its own place;
  - a search folds nothing.
- The open state is kept in localStorage per service. The arrow doesn't open
  the chat.

Tests:
- the JS rules: order, what stays in its place, search, two services;
- the API's `clone`;
- the walkthrough: two clones made in its database, folded, opened and folded
  again, with a screenshot.

The first try put the count above the arrow: `.icon-btn` is an `inline-grid`.
This button is `inline-flex`.

## Folded by folder, and a reviewer in a task's WorkBox (agent.review) (2026-10-04)

**Grouping.** The user saw chats about notes still standing apart:
- "work on version 4 (fork)": a fork of a clone, by the Governor;
- "work on version 1": a clone from before `service_clones` existed.

Neither had a `service_clones` record, but both work in a clone's folder
(`production/<service>/work/<clone>/`). `clones_of` now goes by that folder, so
a clone, a fork of it, a reviewer in it and an old clone all belong to the
service. The version comes from the folder's clone record (none: no version).
Of one version the clone itself (`own`) leads, before a fork of it.

**agent.review.** The Governor described what it lacked when it organised a
review of a clone. A separate reviewer couldn't reach the clone's files:
- `chat.create folder=` refused a folder outside the workspace roots;
- a plain chat with the path got its own container, without those files;
- `chat.fork` worked, but brought the whole build conversation and the same
  model, so it wasn't a review by fresh eyes.

It asked for a way to start a reviewer on an existing WorkBox: a model of its
choice, fresh context, read-only code, and permission to run tests.
`agent.review id=<managed code task> provider= model= message=` does that:
- a new managed code task in the same folder, so in the same WorkBox, with its
  installed tools (`anywhere`, as clones are made);
- `readonly` mode by default (its rules allow tests and trying things, no
  changes);
- it costs an agent from the wake-up's budget;
- the task's conversation isn't handed over.

The Governor's instructions mention it, next to clone → promote.

**Tests.**
- The API marks a clone, a fork in its folder, an old clone without a record,
  and nothing for a service that's gone.
- The reviewer starts in the clone's folder, read-only, with only the review
  message. Refused for a chat, and without a message.
- The walkthrough now puts its clones where Relay puts them.

## Tasks in three kinds: System, Governor's, Manual (2026-10-04)

The user asked for three kinds of tasks.
- **Their example of a system task was a leftover of mine.** "Live check:
  AgentOS safeguards" was my live test from yesterday, a one-off far in the
  future. It's removed from the live database.
- **Relay had no tasks of its own yet.** The user chose two, on by default:
  - **Services: weekly check** (Mon 09:00): `service.list`, `service.status`,
    `service.logs`; small fixes through a clone and promote; done clones
    removed; an intervention for the rest.
  - **Tidy up: weekly** (Sun 20:00): `chat.delete` and `workbox.delete` for
    finished work, `notify.resolve` for settled interventions, `task.delete`
    for its own tasks that ran out. These go past the user's "ask first", as
    always.

How it's built:
- `schedules.system` keeps the key of a Relay task, and `SYSTEM_TASKS` its
  definition.
- `ensure_system` adds the missing ones when AgentOS is turned on, and at
  Relay's start for users who have it on. Without AgentOS they'd only fail.
- `reset` gives the definition back.
- A system task can't be deleted: it's turned off instead.
- `_public` carries `kind` (system, governor, manual) and `changed`.
- The Governor already changes only what's `managed`. `task.list` and its
  instructions name system tasks as ones to carry out, not change.
- The dashboard's Tasks panel has three lists. For a system task:
  - an "edited" badge when it's changed;
  - Reset (asked first, enabled only when changed) instead of Delete;
  - Edit first opens a dismissable warning ("Don't warn me again" is
    remembered in this browser).

Tests:
- added once when AgentOS goes on, none without it;
- edit → `changed`, reset, and Delete refused;
- the Governor carries a system task out but can't change or delete it, and
  its own tasks are "governor";
- the walkthrough: the lists, the warning, the edit form, reset, and no Delete.

## Back to 12cd77f, and the live data put right (2026-10-04)

At the user's request, Relay went back to "Tasks in three kinds" (12cd77f), in
f67a4e3. In the user's words, the three later commits had put Relay in heavy
technical debt: capabilities renamed to noun.verb, a workspace per
conversation, and boxes and the archive by activity.

**The working tree was half reverted.** By 19:32 something outside Relay (no
agent had run since 15:30) had set three files back to 12cd77f and deleted
`archive.py`, `archive.js` and `workspaces.py`. `app.py` still imported
`archive`, so Relay wouldn't have started again.

**The newer code had changed the live data, so reverting the code alone wasn't
enough:**
- **Archived conversations.** 91 were archived at 17:31: their folders packed
  into `archive/` and deleted, and their boxes removed. All were unpacked to
  where they were. The three worktrees were added back to git on their
  branches, and the four box homes in the archive were imported. The
  conversations in the user's own folders were never touched, but their boxes'
  homes are gone. They come back empty on use.
- **Clone records.** `service_clones` had been moved into `service_workboxes`
  and emptied. It was filled again: the first conversation in each folder is
  its clone.
- **Two tasks.** The workspaces migration had turned two tasks off and wiped
  their project folder. They have it back, `/home/relay/projects/
  verkiezingsuitslagen`, found from their prompts and old briefings; the
  72-hour one is on again.
- **Relay's own tasks** had been rewritten for the newer capability names.
  They're back to this version's definitions. Their first tidy-up run was put
  off a week, so it wouldn't run right after the restore.

**Cleaned up afterwards, as the user asked:**
- the archives (648 MB), the two database backups, and the patch of the half
  revert;
- `service_workboxes`, `conversations.archived` and the
  `archived_before_workspaces` meta key (`integrity_check` ok, then
  `VACUUM`);
- three WorkBoxes from a walkthrough of 10:00 that never tidied up, with
  their homes, and four home volumes no conversation has;
- 33 `/tmp/relay-ui-*` folders. The walkthrough left one behind every time:
  files its containers wrote are podman's user's, so `rmtree` couldn't remove
  them. It removes them with `podman unshare` now.

The full suite passes on this version (581); `relay doctor` finds nothing.

## One "New conversation" for chats and code tasks (2026-10-04)

The user asked for less needless complexity, starting with *New chat* and
*New code task* as one *New conversation*. They differed only in whether a
project folder was picked, so now the folder decides:
- **The folder picker** sits under the message box for every provider with
  tools. *No project folder* gives a chat; a folder gives a code task.
- **The welcome** (title, lead, placeholder, suggestions) follows the folder in
  place, so what's typed stays.
- **The Chat/Code/Computer toggle is gone.** *New computer task* stays in the
  sidebar.
- **The draft** keeps the folder picked last (`directory`) apart from whether
  it's used (`inFolder`): `/code` and the picker still start there after a
  chat. A saved draft in the old `code` mode becomes `inFolder`.

The server is unchanged: a conversation is still `chat` or `code`. The
frontend sends `code` when the draft is in a folder.

## A chat gets a project folder later (2026-10-04)

The user wanted to be able to choose a project folder for a chat once it has
started. `/code` and *Move to a code task…* could already do it, but neither
is easy to find. Now the *No project folder* picker of the new-conversation
screen stays under a chat's message box too:
- **Picking a folder moves the chat** (`POST …/to-code`, unchanged). With an
  agent on the chat, it moves at once. A chat model without tools gets the
  existing modal, to choose a provider with tools.
- **Which chats get the picker:** only plain ones. Computer tasks,
  subagents' chats and AgentOS's own conversations don't.
- **`folderPicker`** now takes the folder and what choosing and clearing do,
  so the draft and a chat share it.

## Demote and Promote without the Governor (2026-10-04)

The user asked whether moving a service between WorkBoxes and ProductionBoxes
really needs the Governor. It didn't:
- **Demote** only woke the Governor to call `workbox.clone`, choosing a
  provider. It cost a model run, and it failed while the Governor was busy.
- **Promote** existed only as the Governor's `production.promote`, which calls
  `Production.promote_clone`. That method already checks everything: a clone of
  this service, made from the live version, with no agent working in it.

What changed:
- **Demote** (`POST /api/services/{id}/demote`, `{provider_id, model}`) now calls
  `Production.clone` directly. The frontend sends the new conversation's agent,
  if it has tools, and opens the WorkBox.
  - **The WorkBox is the user's own**, as the user chose: not managed, so the
    Governor can't promote or delete it. The Governor's clones stay managed
    (`governor_run`).
  - **`AgentOS.demote` is gone**, and with it `service.demote_requested`.
    `workbox.cloned` now says who made the clone (`actor`).
- **Promote to live** is a button in a clone's own chat header
  (`POST /api/services/{id}/promote`, `promote_clone`, `by="user"`).
  - `clones_of` adds `fresh` (made from the live version). After a promote the
    button is disabled, and its title says why.
- **Deleting a WorkBox's chat** removes its `service_clones` row and its folder
  (`Production.forget_clone`, in `remove_conversation`), once no fork or
  reviewer works there any more. It's refused while that folder is being
  promoted. `workbox.delete` is now just `chat.delete`.

The UI walkthrough does it for real: Demote, Promote to live (version 2 goes
live), then deleting the WorkBox.

## A server an agent starts outlives its turn (2026-10-04)

The user saw a Copilot turn that wouldn't end after the agent started a web
server for them. Stop ended the turn, but the server too.

There were two causes:
- **Copilot doesn't answer `session/prompt` while an attached background shell
  is still running** (`mode: async`, a dev server). This happens outside Relay
  too: a probe of `copilot --acp` with `gpt-5-mini` got no result within
  120 s. With `detach: true` the turn ended in 8 s, and the server survived.
- **Relay ended every process a command started** once its shell went (a stop,
  `relay-reap` at the end of the run). The gateway killed everything with the
  command's `RELAY_EXEC` tag, so not even a server started with `nohup … &`
  could outlive the turn, whatever the agent.

What changed:
- **The gateway's `END`** (`boxes.py`) spares what detached itself: a session
  led by a tagged process other than the exec's own shell. That shell leads a
  session too, but has no parent (ppid 0, as checked in a WorkBox). The
  command and its `&` children still go, so Stop still stops.
- **Agents are told** (the tools note) to start a server the user should go on
  using with `setsid -f CMD > server.log 2>&1`, check that it answers, and not
  keep it in a background shell of their own.
- **Sessions are told again** when the note's text changes (its hash is in
  `tools_told_path`).

Tried for real on a throwaway Relay with Copilot (`gpt-5-mini`): the turn
ended after 15 s, and the server still answered after it.

## /clear (2026-10-04)

The user asked for `/clear`: the agent forgets the conversation so far and
goes on afresh.
- **`POST /api/conversations/{id}/clear`** sets `conversations.cleared_after`
  to the last message and drops the agent's session (`provider_session`,
  `fork_session`, `fork_before`). It's refused while an answer runs.
- **The agent's history** (`RunManager.start`) is only what comes after
  `cleared_after`. A fresh agent session therefore gets nothing handed over,
  and a chat API gets only the new messages. The auto-title isn't redone after
  a clear.
- **The user still sees everything**, with a line "Cleared: the agent starts
  afresh from here". The agent's own `/clear` (Claude Code's) isn't offered
  next to Relay's.
- **Forks:** one made after the clear starts where this conversation's agent
  did (its own `cleared_after`). One from a message before the clear is as the
  conversation was then: no session of the agent's since, so the conversation
  is handed over.

## /promote, and services AgentOS may or may not see (2026-10-04)

The user asked for `/promote` in a WorkBox, and a difference between the
ProductionBoxes AgentOS may see and change and those it may not.

**`/promote`** is offered in a clone's own chat, as *Promote to live* is.
Both call `promoteClone` (`main.js`).

**`services.managed`**: two levels, as the user chose. The migration's
default (1) keeps the existing service (`notes`) managed. A new one is the
user's own unless the form's *Let AgentOS look after it* is ticked.
- **What the Governor sees:** `Production.services(user, managed=True)` and
  `get(..., managed=True)` are its view, so a private service isn't in
  `service.list` or the briefing. `service.status`, `service.logs`,
  `workbox.clone`, `production.promote` and `production.rollback` answer
  "No service …".
- **Its files:** `read_boundary` gives it `production/<id>` of managed
  services only.
- **Interventions about a private service** (`SEEN`) are left out of its
  briefing and its `notify.update` / `notify.resolve`.
- **A failing private service** gives the user an intervention instead of
  waking the Governor (`Scheduler.service_failing`). Interventions now carry
  their `service_id`.
- **`PATCH /api/services/{id}` `{managed}`** switches it, with an event
  (`service.managed` / `service.private`). The button is on the service's
  page and on the AgentOS page. A managed service's sidebar icon is green
  with "os", as a managed chat's is.

## /clear delete-history (2026-10-04)

The user asked for an option on `/clear` that wipes the history for everyone.
`/clear delete-history` (`POST …/clear` `{delete_history: true}`) deletes the
conversation's messages, its runs (with their events), its attachments, its
handover and its subagents' chats. It drops the agent's session and leaves no
`cleared_after`, so the next message starts the conversation over. The
conversation itself stays, with its title and settings. A code task's files,
changes and checkpoints stay too: it's only the history that goes. As with
the user's other commands, there's no confirmation: typing `delete-history`
is the choice. A plain `/clear` with an empty body still works.

## Copilot's `detach: true` works in Relay (2026-10-04)

The user saw that the earlier workaround (telling agents to use `setsid -f`)
didn't help with Copilot. Its own instructions say "Use the tool option, not
`nohup`/`&`/`disown`", so it uses `detach: true`, and in Relay that failed
with no output. Copilot runs a detached command through a wrapper, which had
three problems in Relay:
- **Its pid and exit files** go in its temporary folder (`/tmp/copilot-detached-…`),
  and Copilot reads them back. The wrapper ran in the WorkBox, whose `/tmp`
  isn't the ProviderBox's. Now Copilot's `TMPDIR` is the scratch folder
  every box and WorkBox has, at the same path (`relay.containers.SCRATCH`,
  `/tmp/opencode`). The wrapper still checks Copilot's `~/.copilot`
  session-state for reap requests, which the WorkBox doesn't have, so
  Copilot's "stop this detached shell" doesn't reach it. An agent can still
  stop it with a command.
- **The run's end ended it.** `relay-reap` ends the box's shells, and the
  gateway then ended the wrapper with them. Now there's `Provider.detached(command)`,
  a support-layer hook: Copilot's is the wrapper's prefix. The gateway runs
  such a command under `setsid -w` in the WorkBox, a session of its own that
  `END` spares, and `-w` lets the shell still see it run and end.
- **Its output went through the shell.** Once the shell was gone, a server's
  every log line failed (python's `http.server` closed each connection).
  `relay-shell` now tells the gateway which files its output goes to. A
  detached command whose files are in the scratch folder writes there itself
  (`RELAY_OUT`/`RELAY_ERR`), which is the log Copilot reads.

The tools note now allows "your shell tool's own option" next to `setsid -f`.

Tried for real on a throwaway Relay with Copilot (`gpt-5-mini`, `detach: true`).
The turn ended after about 20 s, and the server still answered after it.

## A chat moved to a code task brings its files along (2026-10-04)

The user reported Sitecraft as lost. It was a chat, moved to a code task in a
new folder (`projects/Sitecraft`) with the folder picker under the message
box, and its history was then deleted. Nothing was deleted, though: the move
only ever took the conversation along. The files the chat's agent had made
stayed in the chat's own folder (`chats/<id>`), with a toast saying so that's
easily missed. The agent then worked on in the empty project. The history
delete had nothing to do with it.

**Recovered:** the files were moved into `projects/Sitecraft`, next to the
user's own `test.txt`. The empty chat folder and its WorkBox were removed.

**Fixed:** `move_to_code` brings the chat's files into the project now
(`bring_along`):
- They're moved, or copied while a fork of the chat still works in that
  folder.
- A name the project has already stays behind and is listed, in a toast in
  red.
- The task's start checkpoint is taken after the move, so its changes count
  from what the chat made.
- The chat's emptied folder goes, with its WorkBox and home.

## `relay serve`, and when each message was sent (2026-10-05)

**`relay serve`.** A free model (nemotron, through opencode) failed to keep
Sitecraft's server running, three times:
- `pkill -9 -f "node dist/server.js"` matched its own command line and
  killed it (exit 137);
- then `timeout 120 node … &` ended the server after two minutes.

Spelling out `setsid -f` in the note doesn't help models like that, so the
WorkBox's `relay` command (`container/bin/relay`) does it for them, with no
call to Relay:
- `relay serve NAME CMD` starts CMD in a session of its own, which `END`
  spares, with its output in `/tmp/relay-serve/NAME.log`.
- The same NAME again restarts it.
- `relay serve --stop NAME` ends its process group (TERM, then KILL), so no
  `pkill` is needed.
- `relay serve --list` lists what runs.

The note offers it when `relay` is on the PATH (WorkBoxes), and warns
against `timeout` and a self-matching `pkill -f`.

**Timestamps.** The user asked for each message's time, as other platforms
show it. `messageTime` shows the time today, "Yesterday", the weekday this
week, or the date before that, with the full date on hover. It sits in
each message's action row: the user's from `messages.created`, an answer's
from its message, or a live one's when it ends.

## /promote in a code task, and Update from live (2026-10-05)

The user asked for `/promote` to make a ProductionBox from a code task. They
chose that the task then stays the service's WorkBox. They pointed out the
conflict: the Governor may fix the service meanwhile, from a clone of its
own.

**`/promote` in a code task.** It opens `serviceDialog` (`service.js`), the
same dialog the AgentOS page's *New service* now uses.
- The name comes from the folder. The start command is guessed by
  `production.guess`: `npm start`, `npm run dev:web`/`serve`/`dev`, or
  `python3 app.py`/`main.py`/`server.py`. The port is the first free one
  from 8500.
- With `conversation_id`, `add_service` links the task as the service's
  WorkBox: a `service_clones` row made from version 1, or from the first
  version if that didn't come up.
- `clones_of` now also finds WorkBoxes by that row, not only by a folder
  under `production/…/work/`.

**A WorkBox goes on after a promote.** `promote_clone` moves its
`from_version` to the version it just made, so the next `/promote` is the
next version. (Demote's clones did that too: they stayed "behind" before.)
When nothing is live, any of the service's WorkBoxes may be promoted, so a
first version that failed can be fixed.

**Update from live** (`Production.update_clone`, `POST …/update-workbox`).
When the live version moved on from the WorkBox's (the Governor's fix, a
rollback, another WorkBox), promoting is refused as before, since it would
undo that. Now there's a way on: a three-way merge (`_merge`).
- **Base** is the WorkBox's version folder, **theirs** the live one,
  **ours** the WorkBox, all without `.data` and `.git`.
- Changed only live: taken. Changed on both sides: `git merge-file`, with
  conflict markers where both changed the same lines. Binary, or deleted on
  one side: the WorkBox's kept and listed.
- Its `from_version` becomes the live one.
- The base folder is gone after `ARCHIVE_DAYS`; then it says to Demote
  again.
- The header's button becomes *Update from live*, and `/promote` asks it
  first.

## A fallback for free subagents (2026-10-05)

The user saw free subagents stall on errors like "Upstream request failed:
Endpoint is unavailable" and "This model is not available in your country".
Fallback loops left subagents out on purpose ("a subagent's is its caller's
business"), and calling agents, weak ones especially, often don't delegate
again with another model.

`Delegation._supervise` now does it for free subagents, with the models
chosen automatically, as the user chose:
- **When:** the subagent's model is free (`policy.billing`), and it failed
  without getting anywhere, or a second time (after the one retry with its
  own model).
- **Which:** the next free model takes the task over. Candidates are the free
  ones the user allows without asking (`policy.allowed`). Left out are those
  tried already, ones that failed in the last hour, ones that are off,
  limited, or without tools when it had them, and ones on a local server.
  The ones that worked last come first.
- **How:** `switch_agent` hands the conversation over to another provider, or
  the same session goes on with another model. Up to `FREE_FALLBACKS` (3)
  models.
- **What you see:** the card shows "took over from …", and the caller's
  answer says which models failed.

## Perplexity, with the user's subscription (2026-10-05)

The user asked for Perplexity as a provider, with a ProviderBox of its own,
"creative where needed", and chose their Pro subscription over the paid
Sonar API. Perplexity has no API for a subscription, so Relay asks the site.

**What I found first, from this server:**
- perplexity.ai loads in headless Chromium without a Cloudflare challenge.
- A visitor gets answers too.
- The page asks `POST /rest/sse/perplexity_ask`. The answer streams as
  server-sent events (`\r\n` line ends) of blocks, changed by JSON Patches.
  - The answer text is a `text_payload` in the `workflow_root` block's steps.
  - The sources are in `web_results`.
  - The thread is `backend_uuid` with `read_write_token`.
- **A follow-up** sends those as `last_backend_uuid`/`read_write_token`, with
  `query_source: "followup"` and `followup_source: "link"`, and without
  `dsl_query` or `frontend_context_uuid`. It also needs the page settled
  (network idle) before it asks: with too little time it was answered as a
  new thread.

**How it's built:**
- **`container/perplexity.cjs`** does the asking. It starts Chromium on a
  kept profile with the site's cookies (session ones too, saved by hand:
  Chromium drops them when it closes) and the user's session cookie. It asks
  the endpoint from the page (`page.evaluate`, `exposeFunction` for the
  stream), so it's the site asking, with its own cookies. It applies the
  patches and prints uniform events:
  - the search as a `web_search` tool call and result;
  - the answer as text, with private-use citation markup taken out, and
    the sources under it;
  - the thread as the session;
  - the model as a step.
- **`PerplexityProvider`** (`providers/perplexity.py`) wraps that in its box.
  The cookie is written to a 0600 file in its folder, never on a command
  line. Without a thread the conversation is handed over (`agent_prompt`).
  `TOOLS = False`: it searches, and does no code tasks.
- **`Provider.box_image()`**, a new hook: `"workbox"` gives a box of the
  WorkBox image (Chromium, Node, playwright-core). That box mounts
  `container/box` at `/usr/lib/relay`, so `relay-reap` still ends a run's
  processes.

**Tried for real** on a throwaway Relay, as a visitor (the user's cookie
isn't in yet): a question with sources, then a follow-up in the same thread,
about 22 s each. **Not yet tried:** a signed-in account and the Pro models'
names (`model_preference`; the visitor's is `turbo`, and `pplx_pro` is
assumed for a signed-in default).

## The Governor has broken providers repaired; the user applies (2026-10-05)

The user asked whether provider self-repair should be left to AgentOS. They
chose: the Governor does the work, the user keeps Apply, and the 🔧 in
Settings stays for when AgentOS is off.

**Noticing.** `repair.failing_providers` finds a provider whose last three
finished runs all failed, none of them on something that passes (`PASSING`:
limits, rate limits, outages, timeouts, unavailable models, stops).
Every minute, `Scheduler.providers_failing`:
- with AgentOS on, wakes the provider's user's Governor with the errors and
  the way to repair it;
- with it off, or when the Governor can't be woken, gives the user an
  intervention instead.

That happens once, until the provider works again (the `providers_failing`
meta key).

**Repairing.** Two new capabilities:
- **`provider.repair provider helper [model] (what's wrong)`** starts what
  the 🔧 does (`begin_repair`, now shared with the route), as a managed code
  task the Governor can follow.
- **`provider.review id`** runs the review: compile and shadow test.
  - Changed nothing, doesn't compile, or failed the test: the Governor hears
    why, to have its agent fix it.
  - Passed: the user gets an intervention linked to the repair (the new
    `repair_id`), with *Review and apply*. Applying settles it
    (`AgentOS.resolve_repair`).
- **A repair the user started** is theirs: the Governor can't review it.

**Also:** a review compiled every changed support file as Python, so
Perplexity's `.cjs` would always have "failed to compile". Only `.py` files
are compiled now.

## Tests can't reach the user's data any more, even after monkeypatch.undo() (2026-10-05)

My new repair test used `monkeypatch.undo()` midway. That undoes the
fixtures' patches too, `RELAY_HOME` among them, so the rest of the test ran
on the user's real data folder. Six runs left these behind, all found and
removed (checked against the real database, all 11:37–11:44):
- 11 repair folders, with their backups and reviews;
- 21 checkpoints, of those repair folders and of the Governor's notebook,
  which the test Governor had used as its folder;
- a `done-lingering` file in that notebook (`notes.md` wasn't touched);
- an empty `chats/.tests`.

No podman containers were made.

The test now patches back only what it patched. So does the `/timed` test,
which had the same `undo()`. `tests/conftest.py` sets a session-wide
backstop: a throwaway `RELAY_HOME` and `XDG_CACHE_HOME` underneath the
per-test ones, so whatever a test undoes, it lands there. A test checks that.

## Perplexity, signed in (2026-10-05)

**With the user's account.** The user's cookies work: answers come as
`pplx_pro` (Best), billed to the subscription. Of what they sent, three
cookies matter for signing in, and the provider gets them as one Cookie
header: `__Secure-next-auth.session-token`, `__Secure-pplx.session.<account>`
and `__Host-pplx-last-active-account`. A `__Host-` cookie may name no
domain, so the script sets it by URL.

**Models.** These are accepted as `model_preference`; a made-up one gives no
answer, and the script now says to check the name:
- `pplx_pro` (the default: Best);
- `experimental` (Sonar);
- `gpt5`;
- `claude45sonnet`;
- `gemini30pro`;
- `grok4`;
- `pplx_reasoning`.

The site's model list endpoint answers with Cloudflare's check from a script,
so the list is in the provider's options rather than read from the site.

**Cloudflare.** When the ask itself meets the check (403 "Just a moment"),
the script loads the page again and asks once more.

**Desktop apps (the user's suggestion: an unofficial Linux app, or the
Windows one under Wine in the box).** I advised against them. They wrap the
same site in an Electron window, so they reach Perplexity the same way. A
GUI is far harder to drive than Chromium with Playwright, and Wine with a
display server would make the box much bigger for nothing new.

## Perplexity works as an agent (2026-10-05)

The user asked for Perplexity as an agent rather than a chat. Relay's tool
loop for chat models (`relay.toolbox`, `providers/chat.py`) needs function
calling, which the site doesn't do. So it's a text protocol:
- **The first message of a thread** says plainly that Relay reads the replies
  and runs the actions. It lists Relay's tools (`Toolbox`: files, commands in
  the project's WorkBox, the web, skills, subagents) and asks for one as a
  ```` ```tool ```` block of JSON. Later turns get a one-line reminder.
- **`_PerplexityRun`** runs the blocks with the Toolbox and sends the results
  back as the next question in the same thread, until an answer without a
  block. A round's text is held back until it's known not to be an action,
  and shown without its blocks.
- **The script** now answers several questions in one browser (JSON lines on
  stdin; `ready`, then per question its events and an `answered` line), so a
  turn's rounds don't each start a browser.

**What took some trying:** a polite description made both "Best" and Claude
answer "I can't access your project". Saying that Relay is the one running
them, and that this is the point of the conversation, got "Best" and GPT-5
to send action blocks. Claude through Perplexity still declines.

**Tried for real** with the user's account, as a code task on a throwaway
Relay. Asked how many Python files there were and what `main.py` prints, it
listed the files, ran `main.py` in the WorkBox and answered (about 36 s). Then
it wrote and tested `farewell.py` in a follow-up in the same thread (about
34 s). `tools: false` keeps it a plain answering chat.

## A better phone layout (2026-10-05)

The user asked for a better experience on phones. What the walkthrough's
phone-size screenshots (390 px) showed, and what changed (all in the
`max-width: 800px` rules):
- **The pickers under the message box** took three or four rows, a quarter
  of the screen. They're one row now that scrolls sideways (faded at its
  edge), with *Options* as its gear icon.
- **The header** lost the title altogether in a managed chat: the "Managed
  by AgentOS" chip and the status took the room. The chip (and a computer
  task's) is its icon now, and the title keeps at least 64 px.
- **iOS zooms in on a field with text under 16 px.** The message box was
  15 px, so every tap on it zoomed the page. Fields are 16 px on phones.
- **Height:** the layout uses `100dvh` (the visible height, with the address
  bar and the keyboard), as does a wide modal.
- **Smaller things:** the note under the message box is one line on phones,
  and the new-conversation title is smaller and sits higher.

## The test suite under two minutes again (2026-10-05)

The user saw the suite getting slow again: 146 s with `-n 3`.
- **Measured:** the tests add up to about 420 s, but the suite used only
  about 85 s of CPU in those 146 s. It mostly waits, on podman, Chromium in
  containers and the fake agents' replies. This machine (a VM) has 3 cores.
- **With `-n 3`:** the Chromium group (about 103 s) and the podman group
  (about 91 s) each filled a worker, and the other 227 s of tests had mostly
  the third.

What changed (`tests/conftest.py`, AGENTS.md):
- **Six workers** (`-n 6`), since the CPU is mostly idle.
- **Each Chromium check's file** is a group of its own: each runs in its own
  container. Several runs showed no flakiness.
- **The ProductionBoxes' tests** stay a group of their own, apart from the
  other real-podman tests.
  - I tried them side by side too, each worker with its own part of the
    internal port range. That was slower (126 s against 110 s), as their
    podman calls wait for each other, and it failed twice. So they stay
    together.
- **The groups go first**, so they aren't the tail the run waits for.

**Result:** 108–112 s, every test still there (599). The ProductionBoxes'
group is the floor now.

AGENTS.md was committed from its last committed version with only the test
command changed: the user's own line at its top stays theirs, uncommitted.

## The on-screen keyboard no longer covers the message box (2026-10-05)

The user reported that in the phone app (Relay on the home screen) the
keyboard still covered the message box. `100dvh` only follows the address
bar. iOS shrinks just the *visual* viewport for the keyboard, and so does
Android Chrome unless the page asks otherwise. A layout as tall as the screen
keeps its bottom under the keyboard.
- **`js/viewport.js`** (`followVisualViewport`) sets `--app-height` to the
  visual viewport's height on its resize and scroll, and the layout takes
  that height. It also scrolls the page back to its top, as iOS scrolls it to
  show a field. While zoomed in it leaves things alone: that's the user
  panning.
- **The viewport tag asks for `interactive-widget=resizes-content`,** so
  Android Chrome resizes the layout for the keyboard itself.
- **The thread stays at its bottom** when its scroller shrinks (`ThreadScroll`
  already observes it).

Tests: a JS test with a fake visual viewport (keyboard open, zoomed, closed),
and the walkthrough checks the phone page's layout is the visual viewport's
height. A real keyboard can't be shown headless, so that's for the user to
confirm on the phone.

## WorkBoxes behind a VPN: gluetun, per project folder (2026-10-05)

The user asked for WorkBoxes that can sit behind gluetun when they set it up.
They chose two things: it goes per project folder (or all), and Relay starts
and keeps gluetun.

- **`relay/vpn.py` (`Vpn`)**:
  - **Settings:** `settings.vpn` holds gluetun's environment, the folders,
    `all`, and ports to pass through.
  - **The container:** gluetun runs as `relay-vpn` (`up()`, with its spec, so
    it is made anew when its settings change).
  - **Connected:** gluetun's health check, or, since rootless podman may not
    run health checks, a public address from its control server.
  - **Errors** come with the last lines of its log.
- **WorkBoxes**:
  - **`Containers.network`** (a hook Relay sets) gives a covered folder's
    WorkBox `--network=container:<gluetun's id>` instead of `--network=host`.
  - **Restarting:** when gluetun started after a WorkBox (*Reconnect*, a
    crash), the WorkBox lost its network with it, so `ensure` restarts it.
  - **The lock:** the network is looked up before `Containers.lock`
    (connecting can take a while), so it doesn't hold up other folders.
- **Relay from behind the VPN.**
  - **What doesn't work:** in gluetun's network, 127.0.0.1 is gluetun's.
    Pasta copies the host's address into the container, so the LAN address
    doesn't work either (tried: connection refused).
  - **What does:** `podman --network=pasta:--map-host-loopback,169.254.1.3`
    makes 169.254.1.3 the machine's loopback. Relay gets those connections
    from 127.0.0.1, so `/tools-for` needs no exception.
  - **The firewall:** gluetun's `FIREWALL_OUTBOUND_SUBNETS` gets that address
    and the LAN's subnets (from `ip -j -4 addr`, not the containers'). The LAN
    stays reachable as before, and the user's own subnets are kept.
  - **The agent's environment:** `agent_env` gives such a WorkBox
    `relay-url-vpn` and an MCP address there.
- **Podman keeps a container while others are in its network.** So gluetun
  goes with `rm --depend`, taking its WorkBoxes along; they are made anew when
  next used. *Reconnect* is a plain `podman restart`.
- **`stop_idle`** leaves `relay-vpn` alone, as it does the ProviderBoxes: as a
  Relay container it would otherwise be stopped as unused under its WorkBoxes.
- **Settings → Workspaces → VPN** (`js/vpn.js`):
  - gluetun's settings as `NAME=value` lines, with keys masked;
  - *All WorkBoxes* or chosen folders, and the ports;
  - Save connects and shows the public address, or why it didn't connect
    (saved anyway);
  - *Reconnect* and *Log*.
  - The routes are `/api/vpn` (GET, PUT), `/api/vpn/restart` and
    `/api/vpn/log`.

**Tried for real**, with gluetun and the WorkBox image, and a made-up
WireGuard endpoint, since there were no VPN credentials:
- gluetun runs rootless with kernel WireGuard and its firewall;
- a WorkBox in its network (through `Containers.ensure`) reaches Relay at
  169.254.1.3 and gets past `/tools-for`'s local-only check;
- the LAN router answers, while the internet and DNS are blocked while the
  tunnel is down;
- a published port of a server in there answers from the LAN address;
- after `podman restart relay-vpn`, the WorkBox had no network until the
  next `ensure` restarted it.
A real VPN connection is for the user to try with their own provider's
settings.

Tests (`tests/relay/test_vpn.py`, with a small podman model in Python):
- a folder's WorkBox in gluetun's network, and another folder's on the host's;
- gluetun's arguments and firewall subnets;
- the agent's address of Relay in there;
- restarting after gluetun did;
- new settings making both anew, and off going back to the host's network;
- not connecting, or stopping, saying why;
- `stop_idle` sparing it;
- the settings API: masking, a masked key kept, saved when it doesn't
  connect, validation, and off stopping it.

### With a real VPN (Windscribe), and settings as a form (2026-10-05)

The user gave Windscribe OpenVPN credentials to try it with. gluetun's own
Windscribe support needs only the login and a place; the `.ovpn` file isn't
needed. Tried in a throwaway setup:
- **The tunnel came up, but Relay didn't see it.** gluetun connected in a
  few seconds, but `ensure` waited out its two minutes. Newer gluetun
  versions protect their control server: `/v1/publicip/ip` answered 401.
- **The fixes:**
  - **Connected** is now gluetun's health server (`127.0.0.1:9999`, no
    key). It answers with an error while the tunnel doesn't work (tried
    with a made-up endpoint).
  - **The public address and place** come from the control server, with a
    key of Relay's own (`HTTP_CONTROL_SERVER_AUTH_DEFAULT_ROLE`, kept in
    `vpn-control.json`).
  - **`--cap-add=NET_RAW`:** gluetun's health check pings through the
    tunnel and otherwise falls back with a warning.
  - **The log** leaves out the control server's request lines, which
    filled the error messages.
- **The result:**
  - connected in 5 s, with the status showing the public address in
    Amsterdam;
  - in a WorkBox behind it, the public address was the VPN's, not the
    home one;
  - DNS and HTTPS worked;
  - Relay and its tools' check were reachable, and so was the LAN router.

The user then asked for gluetun's settings to be easier to use. Settings now
has a form instead of lines:
- **Provider** and **protocol**;
- the **login** per protocol (secrets as password fields);
- a **country or region** and a **city**;
- **Custom** for a WireGuard server of one's own;
- *More settings* for the remaining lines.

What a provider offers comes from gluetun's own server list:
- **Getting it.** gluetun started without settings writes the list to
  `/gluetun/servers` and stops (2 s; `relay.vpn.servers`,
  `/api/vpn/servers`).
- **The summary:** per provider, its protocols, where its servers are, and
  whether that's `SERVER_COUNTRIES` or `SERVER_REGIONS` (Windscribe's
  servers have regions; Mullvad's have countries).
- **Kept** per gluetun image: 65 KB for 22 providers.

The form still saves gluetun's environment, so the API stayed the same.

## Chats' WorkBoxes for Relay's tools, and Perplexity's models (2026-10-05)

The user reported that Perplexity's models confused the WorkBox with the
user's own computer. In the chat they pointed to, asked "what do you think
of this environment?", Sonar ran `computer_run ls -la` on the user's
computer "localhost". Relay asked the user, the user denied, and the model
asked for screenshots. The causes:
- **No folder tools in a chat.** Relay's own tools (`Toolbox`) gave a chat
  no files or commands. Agent providers (Claude Code, opencode) do work in a
  chat's scratch folder and its WorkBox. Only the user's computers were
  there to run something on.
- **The brief misled it.** Perplexity's first message said "Relay, a
  program on my computer … has my files", with `run_command` as its
  example, a tool a chat didn't have.

The fixes:
- **Chats get the folder tools.** The `Toolbox`'s files and commands come in
  chats too, in the chat's scratch folder and its WorkBox, as agents' chats
  have. A computer task, whose work is on that computer, still has none.
- **Where the model is**, in the guidance (`Toolbox.guidance`):
  - a chat's scratch folder is its own;
  - `run_command` runs in its WorkBox, a Debian container on Relay's
    server, not the user's own computer;
  - the user's computers are something else, for what the user wants done
    on them.
- **Agents' note** (`tools_script.note`) says the same about the WorkBox.
- **Perplexity's brief** now speaks of a workspace on the home server, not
  the user's computer.

**Tried for real** in a throwaway Relay with the user's Perplexity (a copy),
a computer named "localhost" and the same question:
- **Sonar:** `list_files` on its scratch folder, then "a Debian container
  with a scratch folder of its own … without touching your local machine";
  no `computer_run`.
- **Gemini 3.8 Flash:** answered with the WorkBox and the user's computers
  kept apart.

**Models.** The user's account has Gemini 3.8 Flash and ChatGPT 6.1 Sol,
which Relay didn't offer: the provider's list was the one written when it
was set up.
- **Reading the list doesn't work.** The site's list
  (`/rest/models/config`) and every other GET to `/rest/` meet Cloudflare's
  managed check in the headless browser. The question endpoint (a POST)
  doesn't. Getting around that check is not something to build.
- **Answers carry no list either,** only `display_model`. An id the account
  doesn't take gets `error_code: INVALID_MODEL_SELECTION`.
- **What was found by asking.** Following the site's naming,
  `gemini38flash` answered, and was added to the user's list. The
  `gpt61…` guesses were refused. The other listed models still answer.
- **For ChatGPT 6.1 Sol,** the user looks up the id once: the `models`
  field's help, the README and the error for a refused model say where
  (`perplexity_ask`'s `model_preference` in DevTools).

## VPN profiles and tunnels: places per workspace, rotation, fallback loops (2026-10-05)

The user asked for more VPN control: several profiles, a place per workspace,
rotation and fallback loops, each configurable.

**What gluetun can do, tried first** (with the user's Windscribe):
- **Runtime changes.** Its control server changes its settings at runtime
  (`PUT /v1/vpn/settings`; Amsterdam → Frankfurt without a restart). A
  `null` field there means "unchanged", so `cities: null` kept Amsterdam
  and found no server in Germany.
- **A new server** at the same place: `PUT /v1/vpn/status` stopped, then
  running.
- **Another provider without a login:** refused with a 400.
- **A wrong password:** gluetun said "running" and its health server
  "healthy", but there was no public address and no internet. So
  *connected* now means healthy **and** a public address. After each
  change Relay waits a grace period first.

**Profiles hold arbitrary gluetun settings,** and mapping those onto
gluetun's settings JSON would be fragile. So a tunnel's network belongs to
a holder container instead:
- **The holder** (`relay-vpnnet-…`, the gluetun image sleeping) has pasta
  with `--map-host-loopback`, gluetun's DNS (`--dns 127.0.0.1`) and the
  ports. gluetun and the WorkBoxes join it.
- **Switching exits** means gluetun is made anew with any settings, while
  the WorkBoxes keep their network namespace and their processes.
- **The trial:** gluetun was removed and made anew for another place, and
  the WorkBox went on with the new address.
- **No leak in between:** with gluetun gone, DNS and direct IPs were
  blocked. gluetun's DROP policies stay in the network.
- **But its rules piled up**, 12 more per gluetun. Before each new gluetun
  a short container in the tunnel's network clears them (`RESET`): it
  keeps the DROP policies and removes gluetun's `ip rule`s. gluetun's own
  rules in the routing tables went with it.

**The model** (`relay/vpn.py`, `settings.vpn`):
- **Profiles** (`{id, name, env}`, no place).
- **Tunnels:**
  - `{id, name, folders | all, ports, exits: [{profile, place}],
    rotate: {minutes, to: server|next, idle}, fallback: {after, back}}`;
  - a folder's tunnel is the one with its most specific folder, else the
    one for all.
- **The one VPN of before** reads as a profile plus a tunnel
  (`normalize`).
- **Containers left behind** — a removed tunnel's, or the one VPN of
  before — go with `prune`, only this Relay's (its instance mark).

**Ensure, the fallback loop and rotation:**
- **`Vpn.ensure`:** the holder up (made anew when its ports changed, its
  WorkBoxes with it), then gluetun at the current exit. If that doesn't
  connect within `fallback.after`, the next exits are tried; without a
  fallback loop, the error names the exit and its log.
- **`Vpn.tick`** runs on the scheduler, beside the rest, in a task of its
  own:
  - **A dropped exit:** down for `fallback.after` seconds means the next
    one (gluetun gets that long to reconnect itself).
  - **Back to the first exit** after `fallback.back` minutes; if it still
    doesn't connect, straight back to where it was.
  - **Rotation** every `rotate.minutes`: a new server through the control
    server, or the next exit. While an agent works in one of the tunnel's
    folders it waits, unless `idle` is off. Runs now record their folder
    (`RunManager.busy_folders`).
- **Events** (fell back, rotated, didn't connect) are kept in memory per
  tunnel for Settings, and printed to the journal.

**Settings → Workspaces → VPN** (`js/vpn.js`) lists tunnels (status,
address, exit, what happened; Edit, Reconnect, Log) and profiles. Each opens
a dialog:
- **The profile form:** provider, protocol, login, more settings.
- **The tunnel form:**
  - its WorkBoxes;
  - its exits, as rows of profile, region or country, and city, moved up
    and down;
  - rotation, the fallback loop and its ports.

The API is in `routes/vpn.py`: `/api/vpn`, and `profiles` and `tunnels`
with POST, PUT and DELETE. A tunnel's routes also have `reconnect` and
`log`.

**Tried for real** with the user's Windscribe, a tunnel with three exits (a
place that doesn't exist, Amsterdam, Germany):
- the first exit didn't connect, and after 25 s it fell back to Amsterdam;
- rotation to the next exit: Germany in 14 s, with the WorkBox not
  restarted and on its new address at once;
- a new server at the same exit: 10 s;
- with an agent busy there, rotation waited;
- after four gluetuns there were still 15 rules;
- Relay was reachable from the WorkBox.

Tests (`tests/relay/test_vpn.py`, a podman model with holders, joined
networks and exits that do or don't connect):
- the old settings as a profile and a tunnel;
- the holder and gluetun arguments, with the reset before each gluetun;
- the most specific folder's tunnel, and the one for all;
- falling back at the start, and without a loop the error;
- a dropped exit's fallback, back to the first, and round;
- rotation to a new server or the next exit, waiting for agents unless
  told not to;
- other ports making the holder anew;
- a changed profile making only gluetun anew;
- pruning only this Relay's containers;
- the API's validation and masking.

## Review of today's work: the VPN tunnels' fixes (2026-10-05)

The user asked for a code review with fixes. It covered today's commits: the
VPN, its form, its tunnels, and chats' tools. What was wrong, and what
changed:
- **The fallback loop got stuck.** An exit that didn't connect wasn't
  recorded, though gluetun was at that exit now. So the next tick tried the
  same next exit again, and an exit after it was never reached (A down, B
  never connecting: C never tried). `_connect` now records the exit whether
  or not it connects, and `ensure` counts its round from where it started.
  Test: three exits, two never connecting; and for real, Atlantis, then
  Narnia, then Amsterdam, connected in 46 s.
- **Subagents and task branches ran outside the VPN.** A subagent's copy
  (`forks/<id>/…`) and a task's own branch (`worktrees/<id>/…`) live in
  Relay's data, not under the project folder, so no tunnel took them. A
  subagent of a project behind a VPN then ran its commands outside it.
  `vpn.origin()` maps such a folder to the project it stands for: a copy
  through its `fork.json`, a branch through git's common directory. Test:
  a real repository, a worktree in Relay's data and a subagent's copy of
  that branch.
- **No tunnels, no lookup.** `tunnel_for` returns at once when there are no
  tunnels: the exec gateway asks for every command.
- **Errors when saving a tunnel.** Only `VpnError` was caught there; a box
  that won't start (a port already taken) raised `ContainerError`, a 503
  after the tunnel was saved. The dialog stayed open, and a second Save made
  a duplicate. Now any `ContainerError` comes back with the saved tunnel as
  its `error`.
- **A gluetun that stopped by itself** (exited, or removed), with one exit
  or no fallback loop, was only started again by the next run's WorkBox.
  The scheduler now starts it again at its own exit once it has been down
  for `fallback.after` seconds, or `READY_TIMEOUT` without a fallback loop.
- **The scheduler's VPN task** lost errors outside the per-tunnel `try`
  ("Task exception was never retrieved"); it now logs them.
- **`Vpn.stop`** now takes the tunnel's lock, so it can't race the
  scheduler connecting it.
- **The UI:**
  - a server list that didn't come is asked for again on the next render;
  - *Reconnect* is off for a tunnel without WorkBoxes, which would
    otherwise keep a connection nothing uses.

### Unused tunnels stop (2026-10-05)

Asked for after the review: a tunnel nothing uses kept its VPN connection,
and kept rotating, for ever.
- **When a tunnel is in use.** Each tick, `_joined()` counts Relay's
  running boxes per network: one `podman ps -q`, and one `inspect` of their
  `HostConfig.NetworkMode`, gluetun left out. A tunnel is in use while a
  WorkBox runs in it or an agent works in one of its folders; `ensure`
  counts as a use too.
- **Stopping.** After `IDLE_AFTER` (10 minutes) unused, `_rest` removes
  gluetun and stops the holder. podman stops the stopped WorkBoxes along
  with it, and keeps them. WorkBoxes themselves stop after 3 idle hours, so
  the tunnel goes 10 minutes after its last one.
- **Starting again.** The first WorkBox used again starts the tunnel in
  `ensure`, at its first exit: the holder is the same container, so the
  WorkBox's spec stays and it just starts.
- **Tried for real** with Windscribe:
  - with the WorkBox running, the tunnel stayed up;
  - with the WorkBox stopped, it was still up after 4 minutes;
  - after 10 minutes, holder stopped, gluetun gone, WorkBox kept;
  - the same WorkBox used again: the tunnel back in 10 s, behind the VPN.
- **Test:** the podman model learned `ps -q`, an `inspect` of several
  containers at once, and `stop`.

## Relay's own web search: a local SearXNG (2026-10-05)

The user's idea: Relay gets a search tool of its own, through a local SearXNG.
They chose that every agent gets it next to its own search, subagents too,
and that its searches go out through this server's connection or, if set,
through one of the VPN tunnels.

**The image, tried first** (`docker.io/searxng/searxng`):
- **What's in it:** Void Linux, GNU wget and python3; granian on
  `GRANIAN_HOST`/`GRANIAN_PORT`; its settings in
  `/etc/searxng/settings.yml`.
- **Its entrypoint** chowns `/etc/searxng` to its own user, which would
  give a mounted host folder away. So only the file is mounted, read-only,
  with `FORCE_OWNERSHIP=false`.
- **Its bot detection** logs an error without `X-Real-IP`, so Relay sends
  one.
- **The first search:** 20 results in 2.4 s. On the home address Brave was
  rate-limited and DuckDuckGo showed a CAPTCHA, the case for the tunnel.

**`relay/search.py`** (`Search`):
- **The container:** `relay-search`, made with `containers.up()`. Relay
  writes its settings (JSON on, limiter off, a secret of its own; 0600) and
  hashes them into its spec.
- **No port on the machine.** Relay asks it from inside, with `podman exec
  wget`, as it asks gluetun. It listens on 127.0.0.1:48080, out of the way
  of a tunnel's WorkBoxes' servers when it shares their network.
- **Through a tunnel:**
  - it joins the tunnel's holder (`--network=container:`), after
    `vpn.ensure`, and starts anew when the holder did;
  - if the tunnel doesn't connect, the search fails, never around it;
  - `vpn._joined()` counts it, so the tunnel stays up while it runs.
- **Stopping:** it stops after 30 minutes without a search (`tick`), and
  `stop_idle` leaves it to its own time.
- **`text()`:** what models get, with answers first, ten numbered results
  (snippet, engines, date), suggestions, and the engines that didn't
  answer.

**Every agent:**
- **Core, not per provider.** `Context.search` is a field;
  `RunManager` sets it for every run while search is on.
- **Chat models and Perplexity:** `Toolbox`'s `web_search`.
- **Claude Code:** the MCP tool `search` (`delegation.SEARCH`). Subagents
  (`[APPROVE, SEARCH]`) and the Governor get it too, and `allowed()` gives
  a subagent no `help`.
- **Agents with a shell:** `relay search`, in `help` and in the note when
  search is on.
- **Subagents with a shell** got no tools script before. Now they get it,
  with a one-line note (`search_note`); their other Relay tools stay
  refused. `test_an_agent_hands_work_to_subagents` now expects
  `tools=yes` for that reason.

**Settings → General → Web search** (`js/search.js`):
- on or off, the route, the status, and *Try a search*;
- the API in `routes/search.py`;
- a tunnel that web search goes through can't be deleted.

**Tried for real:**
- **Its own route:** the first search, with the container starting, 8.6 s;
  then 2 s; woken after its idle stop, 5.9 s.
- **Through the user's Windscribe tunnel:** SearXNG went out on the VPN's
  address, not the home one; DuckDuckGo answered there; the tunnel counted
  it as in use.
- **Claude Code (`haiku`)** in a throwaway Relay used `mcp__relay__search`
  and answered with docs.searxng.org.
- **`relay search` from a real WorkBox shell** (a command agent) gave
  results. opencode itself couldn't be tried there: a fresh throwaway
  opencode-manager failed setting up its session (HTTP 500), before any
  search.

Tests:
- `tests/relay/test_search.py` (a podman model):
  - the settings and its container;
  - the formatting and the query's parameters;
  - a tunnel's network, restarting after the holder, never around a
    tunnel that's down;
  - the idle stop and waking;
  - the Toolbox tool;
  - `relay search` against a real server;
  - the settings API.
- `test_relay_api`: the MCP tool for a run, a subagent's script and tools,
  and off meaning nobody gets it.

## Relay's own web fetch: a browser when it must (2026-10-05)

The user: "a web fetch fails quite often; can we add a Relay web fetch that
does work?"

**Measured first,** on 22 common sites, with the `web_fetch` of before
(urllib, a "Relay" user agent):
- **9 read.**
- **8 refused (403):** StackOverflow, npm, Medium, NYTimes, OpenAI,
  Bloomberg, Indeed, G2.
- **4 empty:** pages their JavaScript builds (Reddit, X, Amazon, Tweakers).
- **An arXiv PDF** came back as 236k "words" of binary, and long pages came
  back whole.
- **A browser's headers alone** changed nothing: still 8 × 403. Those sites
  recognise the client, so a real browser it is.

**The user's choices:**
- as a last resort, the Wayback Machine's copy, with its date;
- the same route as web search.

**`relay/webbox.py`:**
- **What it is:** Search's container handling, made shared:
  - on demand;
  - no port here (`podman exec wget`);
  - this machine's connection or a tunnel's, never around it;
  - stopped after 30 minutes unused.
- **Who uses it:** `Search` sits on it unchanged (its tests stayed green)
  and `Fetcher` too. The route is still `settings.search.tunnel`.

**`container/fetch.cjs`,** in `relay-fetch`, a box of Relay's WorkBox
image (its Chromium), is a small server on 127.0.0.1:48081:
- `how=http`: Node's fetch with Chrome's headers; text types as text,
  others as base64, at most 15 MB.
- `how=browser`: one Chromium for all pages (closed after 5 minutes
  unused). Each page loads in a context of its own, waits for networkidle
  and for a bot check that passes by itself, and gives the rendered DOM.

**`relay/fetch.py`** decides in Python (`Reader`, testable with a fake
`get`):
- **The order:** http; then a browser when it was refused (401/403/429/451/
  503, a bot check, a consent or login wall: another address with
  consent/privacy/login in it) or thin (under 150 words with scripts).
- **Then the archive:** the Wayback Machine's newest copy that is the page,
  via `web.archive.org/web/<day>id_/` for the newest, a week, a month, half
  a year and a year back.
  - **Copies of refusals:** the newest copies of NYTimes and Tweakers were
    of a refusal, the older ones of the page.
  - **The other APIs:** `archive.org/wayback/available` kept answering 429,
    and CDX took over 20 s for big sites.
  - **Only public addresses** go there (`public()`): not a LAN address, not
    `localhost`, not a name only the LAN knows. A test had sent a local
    page there (it was thin, so it went on to the archive).
- **The text:**
  - `page_text` (stdlib `html.parser`) gives light markdown: headings,
    lists, code blocks, table rows, links with full addresses.
    - **Where it reads:** `<main>` first, sidebars included (npm's weekly
      downloads are in one), else a lone `<article>`, else the body
      without the site's header and footer.
    - **Why `<main>` first:** with `<article>` first, Claude couldn't find
      npm's downloads.
  - `pdf_text` uses pypdf, the first new dependency in a while: pure
    Python, BSD, pinned in `requirements.lock`, vendored by
    `scripts/vendor.sh`.
  - **Parts:** a tool result's worth at a time (`chat.tool_output`, so the
    Toolbox's cap doesn't cut the "offset=" line). Pages are kept 10
    minutes for the next part.
- **Without podman,** the same `Reader` fetches in Relay, http only, and
  says the browser isn't available.

**Every agent, as for search:**
- **Core:** `Context.fetch`.
- **Chat models:** the Toolbox's `web_fetch` (`url`, `offset`); the old
  `_html_text` is gone.
- **Claude Code:** MCP `fetch`, subagents' too.
- **Agents with a shell:** `relay fetch URL [--offset N]`.
- **The notes** say to use it when an agent's own fetch is refused or comes
  back empty.

**Settings → General → Web search and fetch:** "Try" takes an address too.

**Measured again,** the same 22 sites:
- **19 read:** 5 through the browser (StackOverflow, npm, Amazon, Indeed,
  OpenAI's fuller page), 4 from the archive (Reddit, NYTimes, Tweakers,
  G2).
- **2 thin, given with a note:** the Medium homepage and X, which are login
  walls.
- **1 not read:** Bloomberg refuses the archive too.
- **The arXiv PDF:** 2,874 words of text.

**Tried for real:**
- **Through the user's Windscribe tunnel,** Reddit opened in the browser:
  it refuses the home address, not the VPN's.
- **Claude Code (`haiku`)** used `mcp__relay__fetch` on npm, which refuses
  a plain fetch, and answered the weekly downloads (130,953,523).
- **`relay fetch`** worked from a real WorkBox shell.

Tests (`tests/relay/test_fetch.py`):
- `page_text`, and http, a browser and the archive in their order;
- walls, and LAN addresses kept from the archive;
- a handmade PDF, an image, parts from what was kept;
- the box's argv and errors, and without podman;
- the Toolbox, and trying it in Settings.
- `relay fetch` and the MCP tool in the existing tests.

## A second Relay has an opencode of its own (2026-10-05)

The user asked to fix "opencode on a fresh install". I had seen opencode fail
in a throwaway Relay: `PATCH /api/session/{id}` gave HTTP 500
(`OpencodeBackend.set_permissions`).

**What opencode's log said:** `PlatformError: NotFound: FileSystem.realPath
(/tmp/rs-…/data/relay/chats/…)`. The opencode server couldn't see the
throwaway's chat folder.
- **Why:** opencode-manager's "default" profile is opencode as it runs on
  the machine, with the machine's config (`service.json`, its password),
  its data and port 49374. The throwaway found the live Relay's service on
  that port with that password and used it. That service runs in the live
  ProviderBox, which doesn't mount the throwaway's folders.
- **So a real fresh install wasn't broken.** The bug was a second Relay
  (a trial, the UI check, a test Relay) reaching the live opencode service
  and its data: the trial had left a session there.

**The fix:**
- **`opencode_manager.paths.own_opencode()`** is True when `RELAY_HOME`
  isn't the usual folder, the same rule as `relay.containers.instance()`.
- **`ProfileStore(own_default=...)`:** Relay passes that.
  - **Its "default" profile** is then a folder of its own
    (`<profiles>/default`, made on first use, with a port of its own). So
    it gets env overrides and a service of its own.
  - **Unchanged:** opencode-manager on its own (its CLI and sidebar), and
    the usual Relay, keep default as the machine's opencode. Its tests stay
    as they were.
- **The ProviderBox** of such a Relay doesn't mount the machine's opencode
  folders.
- **The live Relay:** its service has no `RELAY_HOME`, so `own_opencode()`
  is False there.

**Tried for real:** a throwaway Relay with opencode and
`opencode/big-pickle` (free, no login needed):
- answered "OK";
- ran `relay search` (first result searxng.org);
- ran `relay fetch` on npm, which refuses a plain fetch (130,953,523 weekly
  downloads).

The live opencode data got no session from it (`session_v2`: none under
`/tmp`). The earlier trial's stray session (its folder `/tmp/rs-UTx0/…`) was
removed from the live service, that one only.

AGENTS.md has the pitfall; README's development notes say why. The README's
test command still said `-n 3`, now `-n 6`.

## VPN problems as notifications, their history kept (2026-10-05)

Asked for after the review: a tunnel's problems showed only under *What
happened*, which lived in memory (a deque of 12, gone after a restart).

**The history:**
- **`Vpn._event(tunnel, text, kind)`** keeps the last 50 per tunnel in
  `vpn-events.json` (Relay's data), loaded when Relay starts. A deleted
  tunnel's goes (`forget`).
- **The kinds:**
  - `info`: starts, rotations, idle stops;
  - `warn`: an exit that didn't connect while the loop goes on, or a new
    server that didn't connect;
  - `problem`: the tunnel lost its connection and falls back, gluetun
    stopped, or no exit connects (`ensure` gave up);
  - `recovered`.
- **Settings** marks problems and recoveries, opens *What happened* while
  the newest is a problem, and shows 12 with *Show all*.

**Notifications** (`Notifier.vpn`, as `intervention` does it):
- **ntfy:** "VPN <tunnel>: a problem", priority high, or "…: connected
  again", default priority.
- **A webhook:** `vpn.problem` or `vpn.recovered`.
- **How often:** a problem at most once per 30 minutes per tunnel (a
  fallback loop can fail round and round), and then once that it's
  connected again. The next problem after that is told at once.
- **Not notified:** an exit that doesn't connect at the start while the
  next one does. With a broken first exit, every start would otherwise
  bring two notifications.
- **Off:** Settings → Notifications, *VPN tunnel problems*
  (`notify_vpn`, on by default).

**Tried for real** with the user's Windscribe and an ntfy.sh topic of my
own, so the user's ntfy stayed quiet. The tunnel connected in the
Netherlands; gluetun was paused; after 21 s it fell back to Germany. The
topic got the two messages: the problem (high) and "connected again" (with
Frankfurt and its address). The history had info, problem, info, recovered.

Tests: a fallback told once, then "connected again"; a failing loop told
once; rotations, new servers and a start past a broken first exit not told;
the history over a new `Vpn`; `forget`; no exit connecting at all; the
Notifier's ntfy headers and the setting off; and the setting's API.

### Web fetch answers sooner at walls (2026-10-05)

X and the Medium homepage (login walls with next to no text) took about
37 s: after the browser, Relay tried five Wayback copies one after another,
and the archive keeps the same empty shell of such a page.
- **No archive for next to no text.** `Reader.read` goes to the archive only
  when the last try was refused (an HTTP status, a bot check, a wall at
  another address) or failed. A page that gave next to no text comes back
  at once, with its note.
- **The older copies at once.** `_archived` asks for the newest copy first.
  When that is a copy of a refusal, the copies 7, 30, 180 and 365 days back
  are asked at once (a few threads), within `ARCHIVE_TIME` (20 s), and the
  newest good one is taken. A 429 still stops it.
- **The browser waits for networkidle 5 s,** not 8: some pages never go
  quiet.

**Measured,** before → after, with the same outcomes:

| Site | Before | After |
|---|---|---|
| X | 36.7 s | 11.6 s |
| Medium homepage | 38.7 s | 10.1 s |
| Tweakers (archive) | 26.0 s | 19.0 s |
| NYTimes (archive) | 25.3 s | 22.6 s |
| Reddit (archive) | 8.3 s | 8.0 s |

Tests: next to no text with no archive calls; the dated copies asked
together (within 0.15 s of each other) with the newest good one taken; and
the time budget against a slow archive.

## Settings → System: what Relay runs, and what it takes (2026-10-05)

The user asked for the overview page proposed earlier: all of Relay's
containers in one place, what runs, its memory and CPU, what each is for,
and stopping or tidying them. This was prompted by the many boxes Relay
now starts itself on a VM of about 9 GB.

**`relay/system.py`:**
- **`machine()`:** cores and load (`os.getloadavg`), memory and swap
  (`/proc/meminfo`), the disk of Relay's data (`shutil.disk_usage`, the
  nearest existing folder), Relay's own RSS, uptime.
- **`boxes()`:**
  - **Which:** `podman ps -a --format json`, kept to this Relay's (its
    instance mark; a ProductionBox when its service is in this database).
  - **The numbers:** `podman stats --no-stream`, and one inspect of all for
    their working folders. About 0.3 s on the live server.
  - **What each is for:**
    - a ProviderBox: the providers whose `box_key` it is (all of opencode's
      share one);
    - a WorkBox: its folder's conversation, or a chat, a subagent's copy,
      the Governor, a service's WorkBox;
    - a ProductionBox: its service and version;
    - a VPN tunnel's holder or gluetun, web search and fetch.
  - **What may be done:**
    - **Stop:** an idle box. Not a WorkBox an agent works in or a terminal
      is open in; not a ProviderBox while a run of it goes; not a tunnel
      with WorkBoxes in it; never a ProductionBox, which its service keeps.
    - **Remove:** a stopped WorkBox (`Containers.discard`: the container
      and its home; the folder stays), or all of them.
  - **Where a stop goes:** each through the code that keeps that box:
    `Containers.stop_box`, `WebBox.rest`, `Vpn.rest`.
- **`storage()`:**
  - `podman system df`: images, containers, volumes and what's unused;
  - the images with whose they are (the WorkBox or ProviderBox image, an
    older build, search's, the VPN's, not Relay's) and how many containers
    use each;
  - one nothing uses can go (`podman rmi`).
  - On the live server an older ProviderBox build (225 MB) was such an
    image.

**Settings → System** (`js/system.js`, a twelfth tab):
- the server's cards with meters;
- the boxes grouped, with links to their conversation or service (the
  Settings hooks got `onOpenService`), stops, removes, and the stopped
  WorkBoxes folded away;
- storage loaded after the rest;
- a look again every 10 s while it's open and visible.

**One catch:** the meters' widths are set through `element.style`. A
`style` attribute is refused by the page's CSP, and the walkthrough caught
it as console errors.

Tests (`tests/relay/test_system.py`, a podman model): what each box is for
and that another Relay's stay out; a busy WorkBox and a running provider
not stopped; a ProductionBox never; removing one stopped WorkBox or all;
storage and an image in use refused; podman's byte counts. The walkthrough
opens the page and checks its cards and storage.

## Servers on this machine's ports at /port/<n>/ (2026-10-05)

**Why:** the user now reaches Relay only through a domain, behind a reverse
proxy with strict external auth. The servers agents start listen on this
machine's ports (WorkBoxes share its network), and those can't be reached
any more; neither can ProductionBoxes' public ports. The user chose one form
for all of them, `/port/<n>/` (a service by its public port), found through
links rather than a list of servers.

**`relay/forward.py`**, an ASGI middleware outside Relay's own:
- **Who and where:**
  - signed in to Relay (else a GET goes to its sign-in);
  - a service's public port goes to its live version (`production.proxies`),
    whatever `settings.host` is;
  - Relay's own port is Relay;
  - anything else goes to 127.0.0.1, then ::1. Vite on `localhost` listened
    on IPv6 only.
- **How:** HTTP with h11 and WebSockets with wsproto, both already vendored
  (uvicorn's), so no new dependency.
  - HTTP: one upstream connection per request, bodies streamed both ways,
    hop-by-hop headers dropped, 120 s for an answer to begin (a dev server
    compiling), the upstream closed when the browser goes;
  - WebSockets: the browser's subprotocols passed on (`vite-hmr`), and the
    socket accepted with the app's choice.
- **The app sees itself addressed directly:**
  - Host is `localhost:<n>`. Vite's `allowedHosts` refuses other hosts, and
    `localhost` passes everywhere (Django's debug hosts too).
  - Origin is rewritten to the app's own, but only for Relay's own pages
    (`Sec-Fetch-Site: same-origin`, or the Origin is Relay's host). Next's
    dev-origin and server-action checks compare it with Host, so no
    `X-Forwarded-Host` is sent either.
  - Referer is rewritten likewise; Relay's session cookie is left out.
  - `X-Forwarded-Prefix` and `X-Forwarded-For` are added.
- **What comes back:**
  - Location headers to `/x`, or to the app's own loopback address, are put
    under the prefix;
  - Relay's CSP, `X-Frame-Options` and `nosniff` stay off: the app's
    headers are its own (Vite's React preamble is an inline script);
  - `Referrer-Policy: same-origin`;
  - an HTML page sets `relay_port=<n>`.

**Absolute paths without a setting:**
- **A request:** one outside `/port/` whose Referer is a page under
  `/port/<n>/` is redirected there (307: method and body kept).
  - Relay's own pages send no Referer (`no-referrer`), so Relay's UI never
    is.
  - A redirected module's own address is then under the prefix, so its
    imports name that, and the chain holds.
- **A WebSocket:** browsers send none with a WebSocket, so one outside
  `/port/` and `/api/` goes to the port in `relay_port`.
- **An app with a base of its own** (Vite `--base /port/<n>/`, Next
  `basePath`):
  - Relay strips the prefix by default, and Vite with a base then redirects
    its root into the prefix (Next answers 404 there);
  - either way Relay asks again with the prefix kept, and keeps doing so for
    that port until nothing answers there;
  - so both kinds work at the same address, without a second URL form.

**Agents:**
- **What they're told** (`tools_script.container`): the user reaches ports
  only at `/port/N/`, and `http://localhost:N/` in a reply becomes that link.
- **`relay serve`** now finds the ports its server's session listens on
  (`/proc`: the session's socket inodes against `/proc/net/tcp{,6}`), waits
  up to 10 s for one, and prints where the user opens it. `--list` shows the
  ports too.

**The UI:**
- **`markdown.js` `serverLink`:** a link with a port to loopback, or to this
  machine's own addresses (`/api/state` `server.addresses`, from
  `ip -j -4 addr`), becomes `/port/<n>/` on Relay's own origin. Relay's own
  port becomes Relay's address; a bare link's text changes along; an
  address written as code becomes a link too.
- **The services list and a service's page** link `/port/<public port>/`
  instead of `hostname:<port>`.

**The trade-off:**
- **The risk:** a forwarded page shares Relay's origin, so its scripts
  could call Relay's API as the user.
- **Accepted:**
  - usability first;
  - the apps are the user's agents' own;
  - strict auth sits in front;
  - Relay's session cookie isn't passed to apps.
- **The alternative,** a subdomain per port, needs the user's proxy and DNS.

**Tests** (`tests/relay/test_forward.py`): real upstreams, uvicorn on free
ports, through TestClient.
- **What's covered:**
  - the request as the app sees it, and another site's Origin left alone;
  - Location rewrites and the headers left off;
  - a 200 KB POST;
  - the Referer fallback, refusals, Relay's own port, 502;
  - both kinds of base learned and forgotten;
  - a ProductionBox's port;
  - WebSockets both ways and from the root by the cookie;
  - SSE's first event before the stream ends;
  - an IPv6-only upstream.
- **A catch:** the test client cancels the app once the browser side
  closes. A forwarding winding down a few loop turns later was cancelled
  mid-way and flaked, so the tests let the app end the socket.
- **Also:** `serverLink` cases in `tests/js/markdown.test.cjs`; `relay serve`
  printing an `http.server`'s port; the walkthrough checks the link a fake
  agent writes, and opens a page through `/port/` whose script is asked for
  at an absolute path.

**For real** (a throwaway Relay, Chromium, Vite 8's React template in the
WorkBox image):
- **Without a base:** it rendered, with no failed requests or console
  errors. The HMR socket connected at `/?token=…` by the cookie, and editing
  `App.jsx` updated the page with its state kept.
- **With `--base /port/5200/`:** learned, and HMR went through
  `/port/5200/`.
- **Signed out:** it landed on Relay's sign-in.
- **Claude Code (haiku):**
  - it ran `relay serve site python3 -m http.server 5311` and answered
    "http://localhost:5311/";
  - the chat linked `/port/5311/`, and the page opened there.
- **A pitfall:** trial homes under the long scratchpad path failed with
  "AF_UNIX path too long". A short folder in /tmp works.

## AgentOS ranks the models by intelligence (2026-10-05)

**Why:** the user asked for a ranking of every available model, #1 (the
smartest) to the last, made by AgentOS's Governor from benchmarks. It is to
be shown in four places:
- by each step of a fallback loop ("Intelligence index: #3");
- in the model picker;
- to agents choosing subagents;
- on the AgentOS page.

It's updated weekly, scores are kept so a model is looked up once, and Relay
keeps working with AgentOS off.

**`relay/ranking.py`, two tables:**
- **`model_scores`:** a model as the benchmark names it, its score (NULL:
  no benchmark knew it), whether it was estimated, the benchmark and source.
- **`model_names`:** which of those a provider's model is ("opus" of Claude
  Code is Claude Opus 5.5).
- **One index for all,** the Artificial Analysis Intelligence Index: mixing
  benchmarks' scales would make the ranks meaningless. A model not on it is
  placed on its scale from other benchmarks and marked estimated.
- **Rank per model, not per provider entry:** one model reached through two
  providers has one rank, and #N counts models. Ties share a rank (1, 2, 2,
  4).
- **A model id mapped under one provider counts under another with the same
  id** (two opencode profiles), so they aren't looked up twice.

**What's ranked:**
- the models available at the Governor's last look (`meta` `model_ranking`),
  so the UI reads only the database and lists no provider's models;
- a provider that can't list its models at that moment keeps the ones it
  had;
- a model no benchmark knew is looked up again after 30 days, since
  benchmarks add models.

**The Governor:**
- **`models.list`:** every provider's models with what's kept of each
  (name, score, rank), and what's not looked up yet.
- **`models.score`:** what it found, as a list. Over MCP that's an array;
  through the tools script, JSON on stdin; or one model in fields. It's
  saved all or nothing.
- **The system task `models-rank`** ("Models: weekly ranking", Monday 07:00)
  tells it to look up only what's new, by name if the leaderboard page
  doesn't show it, to estimate where the index has nothing, and to report.
- **When nothing is new** (`Scheduler._ranked_already`), Relay notes the
  turn done itself (`outcome_by` relay) and refreshes the list of what's
  available, without waking the Governor: a Governor run costs.
- **With AgentOS off,** the task goes as any task does: noted that nobody
  carried it out.

**Where it shows:**
- **`/api/models/ranking`;** `/api/providers/{id}/models` gives each model
  its `rank`.
- **The model picker:** a `#N` hint (a new `hint` field of menu items).
- **Fallback loops:** "Intelligence index: #N" by each step.
- **The AgentOS page:** a **Model ranking** panel (the top 10 with Show all;
  score and source; estimated ones marked; **Update** runs the task).
- **The `agents` list for subagents:** a line on the ranking and per
  provider "intelligence ranks: opus #1, sonnet #2".
- **The Governor's mode:** says its agents list carries the ranks.

**Without AgentOS** there's no ranking: the panel says what would make one,
and the picker, loops and agents list are as before (a test checks the
empty ranking and the agents list without it).

**Tests** (`tests/relay/test_ranking.py`):
- **The two capabilities:**
  - ranks with ties;
  - the same id under another provider;
  - estimated, unknown;
  - next week's single new model;
  - a name kept that needs no score.
- **Refusals,** all or nothing.
- **The ranks in the models API and the agents list.**
- **The weekly task:** it wakes the Governor only with something new, and
  goes as any with AgentOS off.
- **Without AgentOS,** and a provider that can't list its models.
- **The walkthrough** seeds a ranking in the throwaway's database and
  checks the panel, the picker's `#1` and the loop's two ranks.

**For real:**
- **The setup:** a throwaway Relay with only Claude Code, AgentOS on with
  haiku as the Governor, the task run now (95 s, about $0.10). The Governor
  called `models.list`, read the Artificial Analysis leaderboard,
  `models.score`, and reported with `task.done`.
- **The result:** opus became Claude Opus 5.5 (58, #1) and sonnet Claude
  Sonnet 5.5 (56, #2). haiku went unranked: haiku didn't find Claude Haiku
  4.5 on the index and gave it no score.
- **What changed after it:** the prompt now asks to search by name and to
  estimate rather than give up, and such a model is looked up again after
  30 days.

## A running chat opens quietly; the cursor only where the agent writes (2026-10-05)

**Two visual bugs the user saw in the chat:**
1. Opening a chat while its agent works flashed animations for half a
   second.
2. When an agent's text was followed by an activity, the blinking cursor
   stayed at the end of that text, so a second one ran in the new text.

The fix keeps the cursor and the animations as they are.

**The cursor:**
- **Where it went wrong:** `AssistantView.finishBlock` renders a text block
  one last time when something else comes after it (a tool call, a card,
  the end), through `renderBlock`. That placed the cursor again while the
  turn was still live, and no later render touched that block.
- **The fix:** `renderBlock(writing)`, and `finishBlock` renders with
  `writing` false. Only the block still being written gets the cursor.

**Opening a running chat:**
- **Where it went wrong:** `follow()` attached a live `AssistantView` at
  once, and the event stream replayed everything so far as if it were
  happening now. "Thinking…" showed and went, every activity group opened
  (live) and closed with its transition, the status line came and went.
- **The server's part:** the stream says where the replay ends: `event:
  caught-up` after the events that were there when the browser asked
  (`Runs.event_count`), or first when it's not behind (a reconnect that
  missed nothing).
- **The client's part:** `follow(runId, { catchUp: true })` (opening the
  chat; not right after sending) keeps the turn `hidden` until then, and
  then shows it as a finished turn is shown. Nothing transitions from a
  state it never had on screen, and live events after it animate as
  before. A stream that ends or stops before it caught up shows the turn
  anyway.

**Tests:**
- **The stream** (`test_relay_api`): the backlog, then caught-up, then the
  live events and end; and caught-up at once when not behind. The test
  client hands a stream over only once it's done, so the lingering agent is
  let go from a timer.
- **`tests/relay/test_ui_catchup.py`** (Chromium):
  - **The setup:** an agent says something, reads a file and runs a
    command, says more and works on; the chat is opened then.
  - **What it checks:** in no frame was a "Working…" line or an open
    activity group on screen, no transition started in the turn, the
    activity reads "1 file read · 1 command", and there's one cursor, in
    the last text block (none once stopped).
  - **Against the old code:** it failed on both counts ("Working…" on
    screen for 5 frames; cursors [1, 1]).

## Web fetch: Startpage's Anonymous View before the Wayback Machine (2026-10-05)

**Why:** the user finds that Startpage's Anonymous View (AV) often works on
sites that refuse otherwise, and asked for it before the Wayback Machine.

**How AV can be reached:**
- **The links come only with search results:** an AV link (`…/av/proxy?
  ep=…&ek=…`) is encrypted per result, so it can't be built from an address.
- **A plain request** to Startpage gets a CAPTCHA. Chromium passes its own
  check (Anubis, a proof of work) in about 2 s.
- **So `fetch.cjs` `viaStartpage` (`how=startpage`):**
  1. searches Startpage for the address;
  2. takes the result that is it (`.result`'s first external link; a result
     may add a query when the address has none: NYTimes' adds
     `?eafs_enabled=false`);
  3. opens its AV link. Startpage first shows a "masking you" page, then
     `av-….startpage.com/__av/<token><base64url of the address>`, with the
     site's page as the main document.
- **One context is kept for Startpage** while Chromium is up, so its check
  is passed once.

**What's undone:**
- Startpage adds its banner, a feedback form and a "visit without Anonymous
  View" dialog as children of `<html>`, next to `<body>`: those go.
- It replaces the title: the result's title takes its place.
- It keeps rewriting the page's links to `/__av/…`, so they're turned back
  into the site's addresses in the HTML text, not in the DOM.
- The answer's address is decoded from the AV address.

**`fetch.py`:**
- **When AV is tried:** for refusals only, as the archive is, and for public
  addresses only. A page that gave next to no text has the same there.
- **What it says:** a page through it says so ("Through Startpage's
  Anonymous View (Startpage fetched it): the site gave Relay HTTP 403").
- **When it fails,** its reason joins what was tried, and the archive
  follows.
- **`judged` knows more bot checks:**
  - "press & hold", "confirm you are a human", "humans only";
  - a page that opens with one, even with a long footer under it
    (Glassdoor's, through AV);
  - a page that's mostly a CAPTCHA iframe (DataDome's, as G2 and NYTimes
    serve through AV).
- **In `fetch.cjs`,** a site's own check that passes by itself is waited
  for, up to 8 s. StackOverflow's Cloudflare through AV never got past
  "waiting for stackoverflow.com", even in 15 s.

**Measured** (a throwaway fetch box):
- **Through AV:** Reddit's subreddits (both refuse Relay with 403) came in
  about 9 s, live, with reddit.com links and the subreddit's title, and
  nothing of Startpage's left in the text.
- **Through AV, then the archive:**
  - NYTimes, G2, WSJ, Bloomberg, StackOverflow, Glassdoor: AV gave a bot
    check;
  - Tweakers `/nieuws/`: Startpage doesn't have it as itself.
- **What it costs:** about 7–10 s where AV doesn't help.

**Tests** (`tests/relay/test_fetch.py`):
- **The order:** AV before the archive, and the archive when AV gives a
  CAPTCHA frame.
- **When AV isn't asked:** for next to no text, for LAN addresses, and
  without Relay's browser.
- **The new bot checks.**

## AgentOS: what the Governor's first days showed, put right (2026-10-06)

**Why:** the user asked what could be better, from how the Governor behaved
lately. The audit trail of 3–5 October (298 events, 13 wake-ups, 8
interventions, 23 managed chats) and its own chat showed eight things, and
the user asked for all of them.

1. **It polled:**
   - **What happened:** 101 of its 254 calls were `chat.list`, 80 of those
     asking "is it done yet?" every 6–25 s. There was no way to wait for a
     managed chat, and `chat.send`'s refusal said "or wait" all the same.
   - **The fix:** `chat.wait` (ids, default all that work now) waits up to
     `subagents.wait` (100 s), then gives their answers and which still
     work. `chat.send` on a busy chat names it, and the mode says not to
     poll.
   - **For real** (a throwaway, haiku as the Governor, an agent on
     `opencode/big-pickle` running `sleep 150`): `chat.create`, one
     `chat.wait`, `task.done`. Four calls.
2. **It couldn't bring back a service:**
   - **What happened:** on 3 October notes' live box vanished. The cause is
     no longer in the journal. Relay woke the Governor six times between
     18:41 and 01:06. It told the user once, then could only look.
   - **`service.restart`:** as the user's Restart, and
     `Production.restart` now makes a box that's gone (or won't restart)
     anew with `_start`.
   - **One incident:** a failing service is "well again" only after
     `WELL_CHECKS` (10) good checks in a row. One that comes and goes is
     one incident, not a wake-up each time.
3. **Interventions as reports:**
   - **What happened:** three of eight were "it's done" (a security trial,
     an isolation check, the ranking) and landed under Needs you, for the
     user to settle by hand.
   - **The fix:** `notify.report` is an event of its own: a "From the
     Governor" panel (the last week's five), the activity log, and a push
     (`Notifier.report`: ntfy "From the Governor", priority default;
     webhook `agentos.report`). The mode reserves interventions for what the
     user must decide or do.
4. **Read-only replaced the Governor:**
   - **What happened:** the user's questions in the Governor's chat ran in
     the read-only mode chosen there, which replaced the Governor's mode.
     It answered as a generic assistant, wrongly: that it could only
     "propose" provider repairs.
   - **The fix:** `modes.governing` keeps the Governor's mode in its own
     chat (`runs.py`, by `agentos_role`). Another mode chosen there is added
     to it, for now ("only look and answer; start no agent and change
     nothing"). The run's mode stays `governor`.
5. **No way to delete a service from its page:**
   - **What happened:** the user couldn't find how to delete a ProductionBox:
     the service page had no Delete (only the dashboard's row did), and the
     Governor had nothing for it.
   - **The fix:** a Delete on the service page (to AgentOS after), and
     `service.delete` for the Governor. It's irreversible, so "ask first" may
     hold it, and the mode says only when the user asks.
6. **A cluttered ranking:**
   - **What happened:** names carried notes ("(Max with fallback AA
     reference; current Anthropic alias)", "; fast service tier"), so
     GPT-6 Astra stood there three times at #3 and the next model was #6.
     The Governor also had free-model agents do the research and ran out of
     its agent budget, failing the first run.
   - **The fix:**
     - `models.score` takes a `note`;
     - the name is "exactly the index's" (tool description, script help,
       the task's prompt), and the prompt says to do it itself with web
       search and fetch;
     - **Start over** on the panel (`DELETE /api/models/ranking`,
       `ranking.forget`) lets the user rank anew with clean names.
7. **UTC versus local time:** at 00:29 it planned "once 22:30" (UTC), and an
   ISO time with `Z` was refused. Now the briefing gives the time with its
   zone ("CEST (UTC+02:00)"), and `parse_when` takes ISO times with their
   own zone and "in 10 minutes".
8. **Unreadable notes:** its notes and interventions ran words and numbers
   together ("85van97provider/modelvermeldingen"), and were long. The mode
   now says what it writes is for the user: short plain sentences in their
   language, the details in the chats.

**Also:** a `chat.create` in a managed task's folder (outside the workspace
roots, a clone's) now says that `agent.review` is what looks there. The
Governor had tried exactly that.

**Tests:**
- **Capabilities, over MCP:** `chat.wait` (still working, then the answer),
  a report (pushed, an event, no intervention), restart and delete (its
  services, not the user's own), the `agent.review` hint, the briefing's
  zone.
- **The Governor's chat in Read-only:** still the Governor, its run's mode
  `governor`.
- **A service that comes and goes:** told once; a restart of a gone box
  makes it anew.
- **Times:** the ISO and "in N" forms.
- **The ranking's notes, and Start over.**
- **The walkthrough:** a seeded report under From the Governor, Start over,
  and a service deleted from its own page.

## AgentOS: a Governor that dispatches, is woken, looks in and steers (2026-10-06)

**Why:** the user saw the line between delegating and doing it itself blur.
The last round told it to rank models itself, and `chat.wait` let it sit
waiting, so one task could hold the Governor: it wasn't async any more.

**The user's choices:**
- Relay wakes it when its agents are done;
- Relay also wakes it on signs that an agent goes wrong;
- its own model is always available for its agents (a setting, on by
  default);
- it can look into its agents' WorkBoxes, read-only.

**Woken when its agents are done:**
- **Following:** every turn it starts in a managed chat is followed
  (`AgentOS.following`, through `_send`).
- **News:** `run_ended` makes a done one news for its user, unless the
  Governor saw the answer while awake (`_saw`: `chat.send`, `chat.wait`,
  `chat.read`).
- **The wake-up:** the scheduler's tick (`wake_for_agents`) wakes it once no
  other agent was done for `DONE_QUIET` (20 s), one batch. Not while it
  works: any wake-up's briefing takes the news along (`wake` pops it before
  the briefing, and puts it back when the start fails).
- **The briefing** lists each done agent's status and the start of its
  answer. With no task due it says to see to the agents, not "your regular
  round".
- **The send wait:** `chat.create` and `chat.send` now wait 30 s (was 100):
  a quick answer still comes back at once, and for the rest it's woken.

**Signs of going wrong** (`watch`, on the same tick, once per run and
kind):
- `agentos.failing_steps` (4) failed tool results in a row, or one turn
  longer than `agentos.long_minutes` (30): new tunables under Settings →
  Limits → AgentOS.
- A sign wakes it at once. The briefing shows the agent's progress: its
  minutes, its last steps with the failed ones marked, its last words.
- **Steering:**
  - `chat.tell` interrupts a working agent (`runs.stop_and_wait`), as
    subagents' `tell` does, and the message is its next turn;
  - on an idle agent it's `chat.send`;
  - it isn't counted in the agent budget.
- **Progress shows** in `chat.read` and `chat.wait` too.

**Its own model (`own_model`, a checkbox on the Governor card):**
- `chat.create`, `agent.spawn`, `agent.review` and `workbox.clone` take the
  Governor's own provider and model when it names none.
- The briefing and its agents list say that research, reviews and judgment
  work go to an agent on it.
- Off: a provider must be named.
- The ranking task now has an agent on its own model look models up,
  instead of "do it yourself".

**Looking in: `workbox.look id=… command=…`:**
- **Where it runs:** `Containers.look`, a `podman run --rm --init` of the
  WorkBox image, `--network=none`.
- **What's mounted, read-only:** the task's folder (and its repository,
  from `_project`) and its home volume, so its installed tools are there.
- **Read-only** is enforced by the mounts, keeping "the Governor doesn't do
  the work".
- **A time-out** removes the box by its name.

**Tests:**
- **Woken when done:** a lingering agent is still working when
  `chat.create` returns, and when it's done the Governor is woken with its
  answer. One whose answer it saw isn't news. Its own model is the default;
  off, a provider is asked.
- **Signals:** four failed steps make a signal, once, and a long turn
  another. The progress lines; `workbox.look`; `chat.tell` interrupting.
- **The look:** its argv on the fake podman, and a real one, which reads and
  can't write.

**For real** (a throwaway Relay, Sonnet 5.5 as the Governor, an agent on
`opencode/big-pickle` running `sleep 150`):
1. **The first wake-up:** it started the agent and ended its wake-up ("I'll
   report the printed time once it finishes; the task isn't marked done
   yet").
2. **Three minutes later:** Relay woke it with "one of your agents is done",
   and it marked the task done with a plain note, the agent's UTC time put
   in CEST.

The user asked for Sonnet or GPT 6.1 Sol as the test Governor from now on,
not haiku (AGENTS.md).

## AgentOS on the live Relay: Sonnet and Sol as the Governor (2026-10-06)

**Why:** the user asked to try the async Governor on the live AgentOS, with
Claude Sonnet 5.5 and GPT 6.1 Sol. Each fix below was committed on its own;
this is what the trial showed.

**How:**
- Sonnet was the Governor through a temporary fallback loop; Sol was already
  the live one's.
- The agents ran on free Zen models (`opencode/big-pickle`).
- The signals' tunables were lowered for the trial (3 failing steps, a long
  turn at 5 minutes), and put back afterwards.
- Trial tasks, folders, chats and WorkBoxes were removed afterwards.

**Sonnet 5.5** (a slow check, a readiness check bound to fail, a library
research):
- **What went right:**
  - it started all three and ended its wake-up, and was woken in batches as
    they were done;
  - it looked with `workbox.look`, found the readiness file missing, and
    reported that task failed;
  - the research went to an agent on its own model;
  - its notes were plain.
- **What it showed:**
  - an agent retrying inside one loop took the same step each time, so no
    signal saw it. **Fix:** a repeating step is a signal too (9c82a33);
  - what a task asked to tell the user went only into the task's note.
    **Fix:** that goes in `notify.report` (9c82a33).

**GPT 6.1 Sol** (a long check, a research):
- **What went right:**
  - the long-turn signal woke it; it looked, and left the expected long check
    running;
  - it had the research agent check its claims with `chat.tell`.
- **What it showed:**
  - a wake-up that ended with tasks awaiting its report and none of its
    agents at work waited for the next round, hours later. **Fix:** Relay
    wakes it once for them (a2642ea, widened to any awaiting task in
    ab46d45);
  - a task it marked done without a note had its findings only in
    `notify.report`. **Fix:** that report becomes the note (a2642ea).

**The weekly services check (Sol):**
- It removed old clones of notes and judged a config warning harmless.
- **Politiclone:** asked to remove the ProductionBox but keep its WorkBox, it
  didn't use `service.delete`, which wipes the versions and data too, and
  asked. **Fix:** a service can be stopped and started with its data kept
  (8ad4a75).
- **The week:** it couldn't see it, only the health now and a log's tail.
  **Fix:** the health history and the data's daily size, in
  `service.status` (8ad4a75).

**The model ranking (Sol):**
- **Start over:** the trial used it, and Sol took the empty ranking for
  data lost: "cause unknown", and it restored the old scores from its
  saved copy. **Fix:** Start over is an event of the user's, and
  `models.list` says the scores were cleared on purpose (2c4ab1f).
- **Long answers:** an agent's long answer was cut without saying so.
  **Fix:** `chat.read` takes an offset (ab46d45).
- **The result:** 25 models ranked, with clean names and the fast routes
  counted with their model.

## AgentOS: what the Governor said of its own environment, put right (2026-10-06)

**Why:** asked whether its environment had improved, the Governor (GPT 6.1
Sol, through a review agent on its own model) said yes, and named the rough
edges left. The user passed them on.

**`relay help agentos` failed** (`KeyError: '"provider"'`):
- **Cause:** the `models.score` line's JSON example put braces in a text
  formatted with `.format`.
- **Fix:** they're escaped, and a test renders every help topic (049d86f).

**Calls cut off by its own time-outs:**
- **What happened:** Sol gave its `relay agentos` commands time-outs of 10 to
  25 s, while `chat.create`, `chat.send` and `chat.tell` wait up to 30 s for a
  quick answer. Its shell stopped them: the agent started all the same, but
  its id never came back, so the Governor looked it up with `chat.list`.
- **Relay notices it now** (882a805):
  - a caller that left during a call that took more than 5 s;
  - `request.is_disconnected` can't see it through `BaseHTTPMiddleware`, but a
    0.1 s `receive()` can.
- **What it does:**
  - it keeps the answer and adds it to that conversation's next call, saying
    the call was done all the same;
  - this works for every agent: a long `relay run --on` or `relay wait` is the
    same case.
- **What it's told:** the Governor's mode and `relay help agentos` say to
  give calls that start or wait for an agent three minutes.

**Notes on stdin were dropped:** `relay agentos` puts stdin in `message`, and
`task.done` read only `note` (the services check's and the tidy-up's notes).
It takes `message` too now (87f9772).

**"No full week of service history":**
- **Cause:** true for a history kept for a few hours. Yet `service.status`
  said "Its last 7 days: no failed health checks", as if it were a week.
- **Fix:** it now says since when Relay has kept it, or that it keeps none
  yet (1a966ce).

**"The ranking's scores vanished, cause unknown":** it was the trial's
deliberate Start over (01:18), from before Start over was an event the
Governor sees (2c4ab1f). Nothing to fix.

## The Governor's conversations, folded in the sidebar (2026-10-06)

**Why:** the user's chat list filled up with the chats AgentOS's Governor
makes (15 live, beside their own 95, and more every round).

**The group** (`activity.js` `sidebarGroups`):
- **What's in it:** a managed chat goes into "Governor's conversations", after
  Pinned and before Today. Working ones go there too, so the Governor's
  agents don't fill "Working".
- **What stays out:**
  - one waiting for the user stays under "Waiting for you";
  - one the user pinned follows the usual rules.

**Folding** (`main.js` `governorGroup`):
- **The header** is a button. It shows how many there are, how many work, and
  a dot when one has a new result.
- **Open or folded:** folded by default, and remembered in this browser
  (`relay.governorchats`).
- **Always shown:**
  - while searching, it's open;
  - folded, the chat you're in still shows.

**Only what the Governor made:**
- `managed` is set only by its capabilities.
- A fork the user made of a managed chat used to be managed too, so it would
  have landed in the group and been the Governor's to delete. Now it's the
  user's own.
- Only the Governor's own forks (`chat.fork`) are managed.

**Tests:**
- the grouping, in `activity.test.cjs`;
- the user's fork, in the Governor's capabilities test;
- the walkthrough: folded at first, opened and remembered, folded with the
  open chat still shown, the clones inside it.

## Notifications by category (2026-10-06)

**Why:** the user asked for categories to tick in Settings → Notifications.
Every finished run was pushed, the Governor's and its agents' too: with
AgentOS at work, that meant a stream of pushes and toasts.

**The user's choices:**
- per category, a checkbox for *Push* (ntfy and the webhook) and one for
  *This browser*;
- per category, an ntfy priority.

**The categories** (`notify.CATEGORIES`):

| Category | Pushed by default | This browser by default | Priority |
|---|---|---|---|
| your chats and tasks: done | yes | yes | default |
| your chats and tasks: failed | yes | yes | high |
| waiting for you | yes | yes | high |
| AgentOS: done | no | no | low |
| AgentOS: failed | no | yes | default |
| reports from the Governor | yes | – | default |
| something needs you | yes | – | high |
| VPN tunnel problems | yes | – | high |

A run is AgentOS's when its chat is managed or is the Governor's own
(`Finished.agentos`, from `runs._notify`).

**Pushed:**
- **Each category's choice:** `Notifier.wants` decides whether it's pushed and
  `priority` how loud. The webhook's JSON names the category.
- **Where it's kept:** `Settings.notify_categories` holds only what the user
  chose.
- **VPN:** the older `notify_vpn` is the VPN category's default, so a VPN
  turned off before stays off. "Connected again" is never louder than default.
- **The API:**
  - `GET /api/settings` lists the categories as they stand, and the
    priorities;
  - `PUT` validates them and changes nothing when one is wrong.

**This browser** (`notifications.js`):
- **Where it's kept:** its own choice, in localStorage
  (`relay.notifyCategories`), saved at once. It covers only what a browser
  hears of: runs finishing, and agents waiting.
- **New: waiting for you.** A chat that starts waiting for the user is
  announced too, unless you're in it.
- **The flood is gone:** the Governor's agents finishing no longer toast by
  default.

**The page:**
- a table of the categories: a row each, with its checkboxes and its priority;
- the priority is greyed out while its category isn't pushed;
- on a phone each row wraps: the name, then the controls.

**Tests:**
- `test_relay_notify`: categories, priorities, the older VPN setting;
- the API: listing, choosing, refusals;
- `tests/js/notifications.test.cjs`: what a browser announces;
- the walkthrough: AgentOS pushed at Min once saved, a browser choice kept,
  no browser column for VPN.

## Relay's own page for an address with nothing at it (2026-10-06)

**Why:** the user asked to dress up the "Not Found" page. It was Starlette's
plain text; the forwarder's pages under `/port/` were bare HTML.

**The page** (`web.error_page`):
- **What it shows:** Relay's brand, the status large in the accent colour, a
  title, the address in a mono chip, a sentence on what's wrong, then *Go back*
  (when there's history) and *Back to Relay*.
- **Styling:** `app.css`, with the theme chosen in this browser
  (`error-page.js` imports `theme.js`, which no longer assumes the app's code
  stylesheets are there).

**Who gets it:**
- **Relay's HTTP errors** (`app.http_error`): a browser asking for HTML gets
  the page.
- **The rest get what they got before:** the API, MCP, WebDAV and the
  computers' connector, and a missing script or stylesheet.
- **The forwarder's** "Nothing live", "Nothing answers" and the like now use
  it too. It sends no referrer, so its stylesheet comes from Relay, not from
  the port (`_elsewhere`).

**Tests:**
- a browser's 404 is the page, with the address escaped;
- the API's and a script's answers are unchanged;
- the forwarder's page carries the stylesheet and no referrer.

## Shells behind a reverse proxy; "AgentOS conversations" (2026-10-06)

**Shells:**
- **What happened:** the user's shells failed with "Can't open the shell",
  *New shell* too.
- **Why:** a shell's websocket was refused unless its page's origin was
  exactly `{scheme}://{Host}`. Behind a proxy that ends HTTPS, the page is
  https while Relay's own connection is plain, or the proxy sends another
  Host. The API wasn't affected: it has its own CSRF header.
- **The check now** (`routes/terminal.py` `_from_relay`):
  - the origin's host counts, not its scheme;
  - accepted: the Host header, a proxy's `X-Forwarded-Host`, or Relay's
    address (Settings);
  - another site's page is still refused, and the session cookie is
    SameSite=strict.
- **A refused shell says why:** "not signed in", or which address isn't
  Relay's. It shows where the shell would be, and in the log.

**AgentOS conversations:** the sidebar group "Governor's conversations" is
renamed, at the user's request. Its folded state is remembered as before
(`relay.governorchats`).

## Relay's memory, one place for every agent (2026-10-06)

**Why:** the user asked to centralise the memory function and put it in
Settings.
- **Claude Code's memory was split up:** it keeps one memory per folder,
  and every Relay chat has its own folder. So there were dozens of separate
  (mostly empty) memories, and what one chat learnt the next didn't know.
- **The others kept none centrally:** opencode, Copilot and the chat models
  had nothing shared.
- **The Governor** had its own `notes.md`.

**The user's choices:**
- agents and the user both write to it;
- general memories, plus memories per project;
- Claude Code's own memory stays, and what it held is imported once.

**The store** (`relay.memory`, table `memories`):
- a memory's text, its project (none for a general one), who added it ("you",
  or provider · model) and in which chat;
- at most 300 memories of 600 characters;
- the same thing said twice is kept once.
- **What counts as the project:**
  - a code task's folder (`vpn.origin`, so a branch or a subagent's copy
    counts as its project);
  - a service's WorkBoxes count as `service:<id>`;
  - a chat has none.

**Every agent gets it** (`Context.memory`, built in `runs.start`), in the
style of its tools:
- **Agents** get it in Relay's tools note. A change re-tells the note, as a
  changed note already did.
- **Chat models** get it in their system prompt; Perplexity in its brief.
- **A model without tools** gets only what it holds.
- **Subagents** get none.

**Agents add to it:**
- the MCP tools `memory_add`, `memory_change`, `memory_forget` and
  `memory_list`, offered to every run's tools, the Governor's too;
- `relay memory add|change|forget|list` in the script;
- for chat models the functions `remember`, `change_memory`,
  `forget_memory` and `list_memories`.
- **The note asks them** to save lasting facts and wishes there, one short
  fact each. It also asks them to prefer Relay's memory to their program's
  own, since this one reaches the user's other agents too. The providers'
  memories are left as they are.

**Settings → Memory:**
- an on/off switch (a per-user preference);
- adding a memory, general or for a project;
- per group, each memory with who added it and in which chat, to change in
  place, move or forget (with Undo).
- **Off:** agents get none and can't add; what's kept stays.

**API:** `GET`, `PUT`, `POST /api/memory`, `PATCH` and
`DELETE /api/memory/{id}`.

**Tests:**
- `test_memory.py`: the store, the note, the project of a conversation;
- the API end to end: the user's and an agent's memories, the note an agent
  gets, the tools, a chat without a project, off;
- a chat model's system prompt, and the toolbox's tools;
- the settings check with the new tab;
- the walkthrough: added, changed, forgotten, undone.

## The header on a phone: one row, its actions a scroll away (2026-10-06)

**Why:**
- On a phone a code task's header broke onto a second row for *Review
  changes*, so the header's height depended on what was in it.
- The user asked for it to scroll sideways like the pickers under the
  message box, so it keeps one height.

**The strip:** the title, how the conversation is doing and the actions
(changes, promote, the AgentOS and computer chips) are now one strip
(`.header-strip`). On a desktop it lays out as before.

**On a phone:**
- **The strip:** it scrolls sideways, with no scrollbar and the pickers'
  fade at its right edge. The sidebar and new-chat buttons stay put, as the
  attach and send buttons do by the message box.
- **Alone,** the title takes the room there is, down to its 64 px minimum.
- **With actions beside it,** the title keeps a width of its own, so the
  status (*Waiting for you* too) stays in sight. The actions keep their size,
  and what doesn't fit is a scroll away, its start peeking through the fade.

**A layout catch:** the title group's `min-width: 0` let it shrink under its
own title and badge, and the button slid over them. This made "it fits on
390 px" look true when it wasn't.

**Tests:**
- **The walkthrough:**
  - the header is one row;
  - title group and actions are side by side;
  - at 320 px the strip scrolls and the badge stays in sight;
  - *Review changes* comes into view by scrolling.
- **The outcomes check:** it scrolls to *Review changes* before measuring it.

## Containers in WorkBoxes and ProductionBoxes; Node.js 22 (2026-10-06)

**Why:** the user asked for nested container support in Work- and
ProductionBoxes, with podman or docker as standard software there, and for
Node.js 22.

**Node.js 22:**
- **Where from:** NodeSource's apt repository (`node_22.x`, its key as
  `/usr/share/keyrings/nodesource.asc`). This replaced Debian's nodejs 20 and
  npm; NodeSource's nodejs brings npm.
- **Upgrading:** `apt-get upgrade` keeps it current within 22, and `n`
  (installed with npm) switches to another version (`n 24`).
- **A catch:** NodeSource's npm installs globals under `/usr/lib`, while
  Relay's browser scripts (`browser.cjs`, `fetch.cjs`, `perplexity.cjs`)
  load playwright-core from `/usr/local/lib/node_modules`, where Debian's npm
  put it. The suite caught it: the web fetch would have broken. The fix is
  `NPM_CONFIG_PREFIX=/usr/local` in the image. Agents' global installs go
  there too, as before, and that's also where `n` puts a node.

**Containers in a box: what it took** (tried on this machine first, in a
throwaway image):
- **SYS_ADMIN is out.** Root in a box could then remount a read-only bind
  rw and write through it. That would undo read-only modes, the read-only
  rest of a repository, and the shared `.git`.
- **What works without it:** podman as the box's root, in a user namespace
  of its own:
  - `/etc/subuid` and `/etc/subgid` give root a range (`root:1:65535`);
  - the box needs `--security-opt unmask=ALL`. Without it, the kernel
    refuses a new proc mount while podman masks parts of `/proc`. Unmasked,
    `/proc` shows some kernel information, nothing writable.
- **No `/dev/fuse` needed:** the kernel's own overlay works on a volume.
- **Containers share the box's network and hostname** (`netns`, `utsns`),
  without cgroups (`cgroupfs`, events to a file):
  - the box shares the machine's network;
  - netavark's own networks (pasta, slirp4netns, aardvark-dns, nftables)
    couldn't be made to work in it;
  - a new UTS namespace is refused.
- **Builds** work with that too.
- **Writes:** a container writing to a mounted folder writes as the box's
  root, so as the user.

**The image** (Containerfile):
- **Packages:** podman, podman-docker (`docker`, its notice off) and
  podman-compose.
- **Short names:** a registries drop-in makes them pull from Docker Hub, as
  docker does.
- **Compose:** `docker compose` and `podman compose` go to
  `relay-compose` (`image/relay-compose`; also as `docker-compose` and
  `podman-compose`). It runs podman-compose with every service on the box's
  network, no networks and no pod made, and the services' names as
  `--add-host` to 127.0.0.1. So `web:8000` reaches web; ports aren't
  remapped.
- **Rebuilds:** `built_image()` hashes `image/` too, so a changed script
  rebuilds the image.

**The boxes:**
- **WorkBoxes** get `relay.containers.NESTED` and the volume
  `relay-containers-<key>` at `/var/lib/containers`. It's removed with the
  WorkBox's home (discard, and two weeks stopped).
- **ProductionBoxes** get the same, with the service's
  `relay-containers-<id>`. Their images stay over versions, and go with the
  service.
- **The Governor's read-only look** stays as it was.
- **New image:** every box is made anew once with it. Its home volume stays.

**Told:** `relay help container` (Node.js 22, containers here: their
network, ports, compose names) and the README.

**Tests:**
- **The argv:** unmask and the volume, no capability added; both volumes
  removed.
- **A real WorkBox on Relay's image** (skipped when it isn't built):
  - a container writes into the folder as the user;
  - Node is 22;
  - a remount of the read-only repository can't write.
- **A real ProductionBox:** its containers volume, and podman in it.

## Release candidate 2.0.0rc1 (2026-10-06)

The user asked for 2.0.0rc1, made independently, with time to look through
the whole codebase. Since 1.0.0 there were some 130 commits and 22,000 lines:
AgentOS, ProductionBoxes, VPN tunnels, web search and fetch, Perplexity, the
memory, nested containers. Hence a major version.

**How:**
- **The upgrade path:** checked first. A database and `settings.json` made by
  Relay 1.0.0 through its own API, in a worktree of the `v1.0.0` tag, are now
  test data (`tests/relay/data/`, `test_upgrade.py`). They hold chats and a
  code task with a finished run, its events and usage, a queued message, a
  mode, a scheduled task, a fallback loop, a subagent policy and a computer.
  It all opens, migrates and works.
- **Dependencies:** every import outside the standard library is in
  `requirements.lock`. `pypdf` is now in `pyproject.toml` too.
- **Two reviews in parallel, read-only,** in a separate clone in Plan mode:
  - **gpt-5.6-sol:** the whole release;
  - **Claude Sonnet 5.5:** the newest work, 5 and 6 October, that nobody had
    reviewed.
  - **The user's tip came halfway:** development partners are Sonnet 5.5 or
    GPT 6.1 Sol (Opus where it's really hard), picked by their usage limits.
    That's in AGENTS.md now. The 5.6 review, already well under way, was let
    finish.

**What they found and what was done.** Each finding was checked in the code
first.

- **Lost data or work:**
  - **A service's delete took its WorkBoxes,** though its dialog says they
    stay. Now they do, as plain code tasks.
  - **`relay backup` left out services' data,** which lives in podman volumes.
    Each service's volume now goes in as `volumes/<volume>.tar`. A restored
    Relay imports a missing volume from there before a box would make it
    empty (`Production._restore_data`).
  - **A service's data snapshot** (before a promote, for a clone) was taken
    while the live version wrote, and a failed one went unsaid.
    `export_data` now pauses the live box for that moment, so it's
    crash-consistent, and says when it fails.
  - **A version that fails its check** may have changed the live data. The
    failure now names the snapshot from before; sharing `/data` across
    versions is by design.
  - **Update from live:** Relay's own process merges, and followed a symlink
    an agent made in its WorkBox (`config -> ~/.config`). A path through a
    symlinked folder is kept now, never written (`_inside`). A file deleted
    in the WorkBox but changed live comes back, as before, and the docs now
    say so.
- **Boundaries:**
  - **AgentOS on a `command` provider:** a command runs on the machine as
    set up, outside the boxes. `Provider.CONFINED` (False for `command`)
    keeps AgentOS's agents and the Governor off such providers.
  - **Agents and the memory:** an agent could change or forget what the user
    wrote. Those are marked as the user's own and refused to agents.
  - **`/port/` and other sites:** it has no CSRF header, so a request another
    site's page makes (another port of this machine is the same site) is
    refused by `Sec-Fetch-Site`, and its WebSocket by its Origin. A link
    followed still opens. The shells' origin check is now
    `web.from_relay`, shared.
  - **Documented:** with `auth: none`, agents can use Relay's API from their
    WorkBoxes.
- **The Governor:**
  - **No total budget:** a wake-up for its agents' news has a budget of its
    own. They're now at most `agentos.wakes_per_hour` (6), a new tunable;
    more news waits and comes along, and the activity says so once.
  - **Settings put back:** a wake-up stored all its settings after waiting
    for its run to start. It only stores when it was woken now (`woken`).
  - **A recurring task awaiting a report** was nudged once ever. Tasks
    reported meanwhile are dropped from `nudged`.
  - **A run that ended before it was followed** stuck as followed forever.
    `_follow` sees it's over and makes the news itself.
- **Robustness:**
  - **One service's podman trouble at the start** (missing, hanging) stopped
    Relay from starting. Each service starts on its own (`_start_one`).
  - **Stopping a service whose box didn't stop** said it stopped. It's an
    error now, and the address keeps serving.
  - **A data volume `du` couldn't measure** was retried every minute,
    holding up the scheduler's tick. It's tried once a day, and `du` ends
    itself (`timeout 50`).
  - **`GET /api/memory`** ran git on the event loop; now in a thread.
  - **At shutdown** the scheduler and its VPN keeping are cancelled and
    waited for, so they start nothing new (a step already in a thread
    finishes by itself).
- **Not done:**
  - **The archive fallback's pool of threads:** each call has a time-out of
    its own, so they end.
  - **Isolating `/port/` pages from Relay's origin:** a subdomain per port
    needs the user's proxy and DNS; a sandbox CSP breaks apps.
  - **Keeping the Governor's follow state over restarts:** its round picks
    that up. This is in the release notes.

**A second look at the fixes:** Sonnet 5.5 reviewed 6fd3d7b ("GO for
tagging", 8 findings). Done:
- **A restore that fails** left an empty volume behind, which the next start
  took for restored data. It's removed now, and the service isn't started
  on empty data (a ProductionError); the next start tries again.
- **Confined providers everywhere AgentOS chooses one:** not only
  `chat.create` and the like, but a fallback loop in the Governor's or a
  managed conversation (`fallback.usable(…, confined=True)`, a round's
  restart too), `task.create` with a provider, `provider.repair`'s helper, and
  `chat.fork` of a chat on such a provider.
- **The backup's pause and the health check:** a service paused while its
  data is copied looked down to the running Relay, which could alert and try
  to repair it. `relay backup` now marks the operation (`operation =
  'backup'`) for that moment, and the health check skips it, as it does
  Relay's own operations. `reconcile` clears one a crash left.
- **`export_data` with a podman that raises** (production's, on a time-out)
  now says why, and a box it couldn't unpause is said on stderr.
- **`task.done`** drops the task from `nudged`, so a later turn of it
  awaiting is nudged anew.
- **Wording:** the wake limit counts wake-ups for news and signs of trouble,
  not tasks or the round (README); the shutdown comment.

**The live smoke test** (Relay restarted on 2.0.0rc1): `relay doctor`, both
services healthy, and a chat on a free Zen model (`opencode/big-pickle`) in a
new WorkBox ran `node --version` (v22.23.3) and a nested `podman run`. Then:
- **60 `relay-containers-*` volumes** were left over from today's test runs:
  two test cleanups removed a WorkBox's home but not its new image store
  (`test_containers.py`, the UI walkthrough). Fixed, and the leftovers (no
  home, no box) removed. Relay's own `discard` and `stop_idle` already took
  both.
- **Settings → General waited for podman:** its web search section asks
  podman for SearXNG's and the fetch box's state. With podman busy (the suite
  in parallel) the page took over 10 s, and the settings test failed. The
  section now loads on its own under the page, and `/api/search` asks podman
  both at once. The test's server gets 60 s to finish such a request at its
  end.

**Smaller things since the last entries:**
- `/s/<id>` loads by its address (b14eea9);
- a health check overtaken by a later one doesn't count (8962760);
- the UI tests wait longer for their server on a busy machine, and measure
  the phone header in one go;
- "Delete this task?" instead of "Delete scheduled task?".

**Docs:**
- the README's opening (2.0, AgentOS, services, containers), Security (`/port/`,
  the Governor, containers in a box, shells behind a proxy, `auth: none`),
  backups with services' data, the wake limit, providers in boxes, the user's
  own memories;
- [RELEASE-NOTES.md](RELEASE-NOTES.md): what's new since 1.0.0, upgrading,
  requirements, known limitations (1.0.0's notes kept below).
- Version **2.0.0rc1**.

## Release candidate 2.0.0rc2: Relay used for real (2026-10-06)

The user asked for rc2 differently: use Relay as a user would, let it make
real projects and the Governor solve real problems, with every model there
is. So a few hours of that, on the live Relay, fixing what came up.

**What was done with it:**
- **Three projects, built by agents at once,** in the user's projects
  repository: *Boekenkast* (a reading list, Express and SQLite) by GPT 6.1
  Sol; *Kort* (a URL shortener, FastAPI and PostgreSQL in docker compose) by
  Sonnet 5.5; *nlpostcode* (a Python library, 228 tests) by the free
  big-pickle, its tests and README by two subagents.
- **Two went live** through the UI (`/promote`, Promote to live), with
  AgentOS looking after them; a third, *thuis* (a start page for the
  services), the Governor had built, and the user made live when it asked.
- **The Governor's real problems:**
  - Boekenkast's second version started without its books: they were in a
    SQLite file next to the code, which stays with the old version. Told so
    by the user, it rolled back (books back), had an agent move the database
    into `$DATA_DIR` (including what was only in its WAL), had it reviewed
    twice, promoted, and checked. The books survived a fourth version.
  - A weekly vulnerability task (`npm audit`, `pip-audit` on clones of all
    services): nothing found; it raised that politiclone keeps its cache in
    its code folder (the user said leave it).
  - Kort's database broken (scrambled by hand, as after a power cut): woken
    by the failing health checks, it looked, found no backup, and asked
    before starting afresh; told to, it had a version move the broken file
    aside, stopped the service while it promoted, and checked.
  - Asked for a new site and to make it live: it built it, then asked the
    user to make the service (it can't: a public port is theirs).
- Also: Perplexity research, Copilot on a screenshot, a scheduled task, a
  chat switched to another provider mid-way, a fork going on with GPT-6 Luna,
  the memory (a new chat knew what Settings → Memory said), a VPN tunnel.

**What it turned up, fixed** (a commit each):
- **compose's `service_healthy` waited for ever** in a box: podman's health
  checks run on systemd timers, and a box has none. relay-compose runs them.
- **An agent couldn't commit** in its WorkBox (git had no name, and the agent
  asked the user for one). The image commits as Relay.
- **A subfolder's commit waited for agents anywhere in the repository**
  (the user's projects folder is one repository): it waits only for one in
  that folder now, and a refusal names who holds it up (a free model had
  spent many steps searching Relay's own code for why).
- **An answer's parts ran together** in what's stored ("…the files.The
  tests pass"): paragraphs now (`handover.answer_text`).
- **`relay changes`** listed the turn going on as “”.
- **A service's data and dependencies:** its WorkBox agent is told how a
  version runs ($PORT, `$DATA_DIR`, only its folder); the new service dialog
  says so, warns when the code never mentions `$DATA_DIR`, and can ask the
  agent first. Kort's first version then failed on a module pip had put in
  the WorkBox only, and its Log said "(nothing is live)": it shows the failed
  version's log now. A compose project's start is guessed.
- **A VPN tunnel lost its network on every restart of Relay:** pasta, its
  network, was in relay.service's cgroup. The holder starts in a scope of its
  own now, and one left without its pasta is made anew (both checked for
  real: healed within a minute, then survived the next restart). Relay drops
  INVOCATION_ID for conmon's sake, so the service is told by its cgroup.
- **A Claude Code turn hung for ever:** resumed, claude first reported on a
  command Sonnet had moved to the background, with a `result` of its own;
  the bridge took it for the end, stopped reading, and claude waited on a
  full pipe. Found with /proc (conmon in `sock_alloc_send_pskb`, podman exec
  in `pipe_write`). A result before the run's init isn't its end, and after
  the end the rest is read (one that doesn't end is stopped).
- The AgentOS page was a red "Needs you" with nothing open; the Governor is
  told new services are the user's to make; a walkthrough step got time for
  a busy machine; `relay doctor` no longer says Perplexity needs podman (it
  has no boxes of Relay's to give it: it looks for podman).

**A review** by Sonnet 5.5 of v2.0.0-rc1..HEAD, read-only in a clone: GO,
eight findings; done: pasta found wherever it's installed and "can't tell"
taken for alive (a wrong "gone" would remake a tunnel each time), systemd-run's
own failures fall back, an early error result is said, the drain test proves
the drain, a failed version's note only. Left: a transient `index.lock` when
two agents commit in one repository at once, and compose's wait having no
time-out (as upstream).

**Not done, for 2.0.0** (in the release notes' known limitations):
- service data has no backups on a schedule (only before a new version, and
  `relay backup`);
- restarting Relay takes services' addresses off for a few seconds;
- the Governor can't make a service (by design).

- Version **2.0.0rc2**.

## Memory, polished: categories as Perplexity's, and the Governor's own (2026-10-06)

**Why:** after rc2 the user wants the existing features polished, Memory
first: a memory of the Governor's own, and the memory working the way
Perplexity.ai's does, with categories and the like.

**Perplexity's** (its Help Center, "Memory"):
- Settings → Memory: a switch, and "Manage memories", the memory bank;
- the bank: search, filter, edit or delete one, or "Delete all memories";
- each memory: its date, its category (Interests, Tools, Work, Preferences,
  Location, …) and a summary;
- saved as you go.

**What Relay does now:**
- **Categories** (`memory.CATEGORIES`, a column `memories.category`, `other`
  for those from before):
  - Perplexity's kinds: Preferences, About you, Work, Tools, Interests,
    Location;
  - two for agents' work: Setup (how a project, service or machine is set
    up) and Decisions (what was decided and why);
  - and Other.
  - **Agents file each memory** in one: `memory_add` takes a `category`
    (an enum, with what each is for), the script has `--category`, a chat
    model's `remember` has it too.
  - **Their note** shows each memory's category, and names the
    categories.
  - A category alone can be changed.
- **The Governor's own memory** (its place `governor`, "The Governor"):
  - **Before:** its `memory_add project=true` landed under its notebook
    folder, as if that were a project.
  - **Who gets what:** only it gets this memory, and the general one too,
    in separate sections with how to use each. No other agent sees,
    changes or forgets it.
  - **`notes.md`:** its instructions keep that for longer records. It was
    112 KB live, and read every wake-up.
  - **Moving `notes.md`:** while its memory is empty, a briefing asks it to
    move what lasts from `notes.md` into its memory and trim the file. It's
    asked at most once a day (`notes_moved`), so a wake-up that failed at
    once doesn't lose the request.
  - **The AgentOS page:** the Governor's card has "Memory (N)", which opens
    Settings → Memory on the Governor.
- **A second switch, "Agents save what they learn"** (`agents_add`). Off:
  agents read the memory, and are told the user keeps it.
- **Settings → Memory, a memory bank:**
  - search: the text, its category, where it counts, who added it;
  - a category's chips with counts, and a place filter;
  - each memory with its category's badge, where it counts, who added it,
    in which chat, and when;
  - **change:** its text, category and place;
  - **Undo** puts one back as it was: who added it, in which chat, since
    when;
  - **Delete all memories**, confirmed.
- **A turn's activity says when it remembered something:** "1 memory
  updated", for the MCP tools, a chat model's functions and `relay memory
  add` in a shell.
- **API:**
  - `GET /api/memory` has `categories` and `agents_add`;
  - `PUT` takes `agents_add`;
  - `POST` takes `category` and `restore`;
  - `PATCH` takes `category`;
  - `DELETE /api/memory` deletes all (`?project=` for one place).

**Tests:**
- `test_memory.py`: categories, the Governor's place and note,
  `agents_add`, clearing;
- the API end to end: a category through the tools, the Governor's own kept
  from other agents, agents only reading, Undo as it was, delete all;
- the briefing asking once to move `notes.md`;
- `tests/js/tool-activity.test.cjs`;
- the walkthrough's memory step: categories, a chip, search, a change of
  category, Undo, delete all.

## Services, polished: a page of their own (2026-10-06)

**Why:** after Memory, the next feature to polish. The rc2 trial was where
services met real use. A service's page was a shell and nothing else: its
versions were on the AgentOS page only, as chips, one per line. Its health
over time was in Status, a text dump meant for the Governor.

**A service's page** (`service.js` serviceView):
- **Three tabs:** Overview, Shell, Log. The shell connects when its tab is
  opened (it used to on every visit), and stays while you switch tabs.
- **The head:** Demote, Restart, Stop/Start and Delete, as before; Status
  and Logs became the tabs.
- **Overview:**
  - **tiles:**
    - its health now;
    - its live version (who made it live, when);
    - its incidents in the last 7 days, and how long it was down;
    - its data's size and growth;
    - its box (running since, restarts, out of memory);
  - **its health, last 7 days:** each incident with when, how long, why,
    and which version made it well again;
  - **every version:**
    - who made it live and when, and from which WorkBox (a link to its
      chat);
    - Relay's check, and the agents that worked on it;
    - **Roll back to this** on any archived one (the API took a version
      already; the AgentOS page only offered the newest), and **Why** on
      one that failed;
  - its WorkBoxes, and how it runs: the start command, health check,
    address, its data, and who looks after it;
  - the processes in its box, folded.
- **The data:** from `/api/services/{id}` (every 10 s) and
  `/api/services/{id}/status`, which asks podman. The status is asked again
  only when the service's health, live version or stop changed, or on
  Refresh.
- **The Log tab:** the last 200 lines, with Refresh.

**The AgentOS page's services panel:**
- a row per service: its name (to its page), address, "version 4 live ·
  3 archived · 1 failed", its command and health check;
- the AgentOS toggle and **Open**;
- its restart, stop, roll back and delete icons went to the page.

**Found on the way:**
- **A promote didn't record that a failing service was well again:** the
  next check saw it well already. Kort's status said "failing since
  15:31" hours after the Governor's fix went live; it records it now.
- **Incidents kept before that** end, on the page, when the next version
  went live.

**Tests:**
- the promote's record of a recovery;
- `tests/js/service.test.cjs` (incidents);
- the walkthrough's service step:
  - the Overview's tiles, versions and facts;
  - no shell until its tab;
  - the Log and Shell tabs;
  - the phone width;
  - after a promote, Roll back to this on version 1.

## Services: daily copies of their data, to put back (2026-10-06)

**Why:** the limitation the rc2 trial found. Kort's database broke, and
there was nothing to go back to: Relay copied a service's data only before
a new version took over. The Governor could only ask the user to start
afresh.

**What's done:**
- **A copy once a day** (`Production.daily_snapshots`), of each live
  service:
  - after 04:00 (`DAILY_AFTER`), its box paused for that moment
    (`export_data`, as before a version);
  - under an operation `snapshot`, so the health check leaves it be and
    nothing else runs at the same time;
  - the newest `services.daily_snapshots` kept (a new tunable: 7; 0 for
    none).
- **When:** the scheduler asks hourly whether one is due (`daily_due`, a
  look at files only), and makes it as a task of its own beside the tick,
  as the VPN keeping is. The app's lifespan ends it too.
- **Putting one back** (`Production.restore`, `POST /api/services/{id}/restore`):
  - an operation `restore`;
  - the box stopped;
  - the data as it is now kept first (`before-restore-<when>.tar`): if that
    can't be made, nothing changes;
  - the volume emptied (`find /data -mindepth 1 -delete` in a box of the
    image) and imported. If that fails, the data as it was goes back;
  - started again and checked, an event `data.restored`.
- **The page:** "Its data", each copy (before version N, daily, before a
  put back) with when and its size, and **Put back**, confirmed.
- **The Governor:**
  - `service.restore service=… snapshot=…`, listed in "ask me first";
  - `service.status` lists the snapshots.

**Tests:**
- with real podman (`test_production.py`): a counter's data copied in the
  morning, not before 04:00, two kept; counted on; put back (the count as
  it was); the copy before it put back (the count again); an unknown one
  refused;
- the Governor's capability.

## The Governor makes services (2026-10-06)

**Why:** the user, on the limitation that the Governor can't make a new
service: "The Governor may by the way make new services itself. That's
handy for, say, designing websites autonomously." In the rc2 trial it built
*thuis* and then had to ask; Relay had told it that a public port is the
user's to give.

**What's done:**
- **`service.create id=… [name] [command] [port] [health]`:**
  - from a managed code task's folder (not a chat, not the user's own);
  - looked after by AgentOS;
  - the task goes on as its WorkBox, so `production.promote` makes its next
    versions;
  - the start command, name and port are guessed when not said
    (`production.guess`), and asked for when nothing in the folder says;
  - a first version that doesn't come up is said, with why, and what to do
    next.
- **Shared with `/promote`:** `Production.create_from` (the service, and
  the task linked as its WorkBox) is used by the dialog's route too.
  `create` takes `by` (who made it live) and records `service.created`.
- **"Ask me first":** `service.create` is listed there ("makes a
  service"), for a user who wants to decide.
- **The Governor's instructions:** build it with an agent in a managed code
  task (on `$PORT`, its data in `$DATA_DIR`, its dependencies in its
  folder), review it, `service.create`, check it, and tell the user its
  address. The line that said otherwise (`99d8c6f`) is replaced.
- The known limitation is out of the release notes.

**Tests:** the capability end to end:
- the user's own task refused;
- the managed one made a service, looked after by AgentOS, guessing
  `npm start` and a port;
- the task linked as its WorkBox, and not made a service twice;
- asked for the command when nothing says it.

## AgentOS can be left out (2026-10-06)

**Why:** the user: "Can you make all of AgentOS opt-out? So a tab AgentOS in
the settings with 1 on/off switch. Turn it off, and all of AgentOS (and
ProductionBoxes and the promote logic) disappears from Relay's UI."

**What's done:**
- **The setting:** `included` in a user's AgentOS settings (default on),
  `PUT /api/agentos {included}`; `/api/state` sends it as `agentos`.
  `AgentOS.on()` is included and enabled: the scheduler's checks (due tasks,
  rounds, agents done, failing services, the weekly ranking) and
  `ensure_system` go through it, and `wake` refuses when it's left out. The
  Governor's own switch is kept as it was.
- **Settings → AgentOS:** one switch, and what "off" means: what it made is
  kept, services keep running, the user's own scheduled tasks are under
  Scheduled.
- **Off, in the UI:**
  - the sidebar says *Scheduled* (the clock) instead of *AgentOS*, without
    the Governor and Production boxes; the page lists the user's own tasks,
    and says how many of the Governor's wait;
  - managed conversations are off the sidebar and off Tasks (`list_tasks`),
    and a service's page (`/s/…`) goes home;
  - no `/promote`, no *Promote to live*, no "Managed by AgentOS" chip;
  - Settings: no AgentOS or Services limits, no AgentOS-only notification
    kinds, no Governor memory.
- **Turning it on** brings it all back without a reload (`setAgentOS`).
- **A failing service** with AgentOS off is an intervention, as for a
  service AgentOS doesn't look after: pushed if set up, not on a screen.

**Tests:** the API (left out: no managed conversations, no wake-up, the
Governor's switch kept; back in); the settings tabs; the walkthrough turns
it off and on (the sidebar, the Scheduled page, `/promote` in the command
list).

## The Governor over a restart (2026-10-06)

**Why:** an open item for 2.0.0. In the rc2 trial, restarts of Relay
dropped the Governor's follow-up of its agents: what it followed and its
news were only in memory, so a cut-off agent stood still and the Governor
slept until its round, until "Wake now". Restarts come more often now (one
after each commit, and the new server's memory upgrade).

**What's done:**
- **Kept:** what it follows and its news are in `meta` (`agentos_following`),
  written whenever they change (`AgentOS._keep`, under a lock: run threads
  and the event loop both change them). `reconcile` reads them back at the
  start.
- **What a restart cuts off:**
  - **An agent's turn:** `run_ended` marks the news `cut_off` when Relay's
    stop ended it (`runs.shutting_down`). After a crash, a followed run the
    start failed is cut off too (`fail_unfinished_runs` now returns the runs'
    ids for this).
  - **On the first tick** (where `/timed` goes on too), `resume_agents`
    starts such a turn again with "Relay restarted while you were working…"
    (`relay_action` resume: "Relay resumed …" in the chat), and follows it.
    Not when the chat is discarded, waits for a usage limit, went on
    meanwhile, or AgentOS is off: then it stays news, marked in the briefing
    ("chat.send to have it go on").
  - **A wake-up of the Governor's** cut off (by a stop or a crash) is news
    `restarted`, with the wake-up's reason. It wakes it again: "Relay
    restarted during your wake-up of … (…): see what you had started then".
  - The agents Relay went on with ride along in that briefing (news
    `resumed`), but don't wake it by themselves.
- **A user's Stop** isn't a restart: that agent stays stopped, its turn news
  as before.
- **A fallback agent taking over an agent's turn** (or a queued message
  going on) was taken for the agent being done. The new turn is followed
  instead.
- The activity log says "went on with … : its turn was cut off by a
  restart". The known limitation is out of the README and the release notes.

**Tests:**
- **A restart** (the runs stopped as Relay's stop does, a fresh AgentOS on
  the same data):
  - the cut-off agent goes on, followed;
  - the Governor is woken again, told what went on;
  - the agent the user stopped is told as it was.
- **A crash:** a followed run and a wake-up left going come back as news
  (with the wake-up's reason), and the agent goes on.
- **A fallback agent's turn** is followed, and kept.

## The wake cap as a pace (2026-10-06)

**Why:** an open item for 2.0.0. In the rc2 trial the cap on wake-ups for
its agents' news (`agentos.wakes_per_hour`, 6) held news for 26 minutes
during heavy use. It counted wake-ups in the last hour, so six quick ones
(three agents done at different times, then their reviewers) left the next
news waiting until the first fell out of the hour.

**The cap stays:** each wake-up has a budget of its own, so a Governor woken
for each agent's answer could start turn after turn, with a fresh budget
each time. Letting done news through would lose that. (Corrected later:
`chat.send` does use the budget; `chat.tell` to an idle agent doesn't.)

**What's done:** the cap is a pace now (a generic cell rate: `news_due`, when
the next wake-up for news is due at that pace):
- half of `wakes_per_hour` may come at once, then one every hour / that many;
- so news waits at most 10 minutes at the default (it was up to an hour);
- an hour has at most half again as many (9), and the pace over hours is
  the same 6.

`wake.held` says when it's next; the tunable's help and the README say how
it goes.

**Tests:** a wake-up for news counts, more waits (said once); over an hour at
6, three at once and then one every 10 minutes, nine in all, and the wait
after that is at most 10 minutes.

## Web search within 10 seconds (2026-10-06)

**Why:** the user: "Can you improve the search tool's timing a bit more though?
So a timeout of something like 10 seconds instead of 30?"

**What was there:**
- Relay waited up to 40 s for SearXNG's answer.
- SearXNG waits for the slowest engine it asked: up to 9–10 s for those on
  by default.
- Through the VPN, engines that rate-limit or hang (Brave, DuckDuckGo) take
  up to that.
- Measured on the new server: 1.4–1.7 s a search, 4.7 s for the first one
  (SearXNG starting); the slow tail is what was felt.

**What's done:**
- **`search.timeout`** (Settings → Limits → Web search, 10 s by default):
  - SearXNG gets `timeout_limit` 2 s less, so it answers in time with the
    engines that did (the others are named, as "didn't answer");
  - Relay waits that long for it.
- **One that doesn't answer in time** says how long it waited and where to
  change it.
- **Starting the box** after half an hour unused (a few seconds, more when a
  VPN tunnel must connect first) isn't part of it.

**Tests:** the time-out's parts in the query (8 s for the engines, 10 s
for Relay), a changed setting, and the error when SearXNG doesn't answer.

## A fallback loop's models while they load (2026-10-06)

**Why:** the user, adding to a fallback loop: with "opencode · localai"
chosen, the model list offered only Claude's models ("In profile opencode
I can only add Claude profiles?").

**What it was:**
- **The UI:** the adder (and the allowed models under Subagents) kept the
  previous provider's models in the list until the new one's came. That's
  Claude Code's, the first in the list.
- **Why it took so long:** opencode's model lists took 30 s and came back
  empty. A background service of the localai profile was running on the
  host, outside its ProviderBox. Opencode had started it when it was asked
  for its logins (`opencode auth list`, in checking the move to the new
  server). Relay's box couldn't use or stop it, so its own never started.
- **Fixed:** stopped it. Relay then started its own in the box: 30 models,
  in about a second. AGENTS.md now warns against running opencode on the
  host with a profile's folders.

**What's done:** both pickers say "Loading models…" until the provider's
models are there.

**Tests:** the settings UI check holds the models' answer back: the adder
says it's loading, then lists the provider's models.

## The Governor tidies the memory up (2026-10-06)

**Why:** the user: "Can you add a system task to AgentOS that cleans up
duplicate or very inactive Memories?"

**What "inactive" is:** Relay didn't know when a memory was used. Every
agent gets the general memories and its project's in its note, so "used" is
when an agent last got one: `memories.used`, set by `memory.note(…, used=)`
for those it shows (only `runs.py` passes it). A project's memory no agent
got for long is one whose project isn't worked on. One kept before this
counts from when it was last changed.

**What's done:**
- **`memory.review`** (relay.memory) gives each memory its days unused,
  and finds three kinds:
  - **alike:** most of their words the same (`SAME_WORDS` 0.7), in one
    place, or general and a project's. Two projects may each hold the
    same. That catches a fact said twice and one that changed ("port
    8501"/"8502"); one said in other words is for the Governor to see;
  - **gone:** its folder or service no longer there;
  - **unused:** no agent got it for `memory.idle_days` (a tunable under
    AgentOS, 90 days).
- **Three capabilities:**
  - `memory.review`: all of it, every place, with those findings;
  - `memory.change`: the keeper of a merge;
  - `memory.forget`: with why, in "ask first", and in the activity with
    its text, place and why.
  - Neither of the last two touches the user's own: the Governor names
    those in its report.
- **A system task, "Memory: weekly tidy-up"** (Sunday 19:00, an hour before
  the AgentOS tidy-up):
  - merge what says the same (the newer holds when they disagree);
  - forget what's gone or no longer holds; keep what holds, however old.
  - **Like the model ranking,** Relay notes it itself, without waking the
    Governor, when no memory was added or changed since it last ran and
    nothing of an agent's is gone or unused.

**Tests:**
- notes mark what they show used;
- the review's findings (alike, not across projects; gone; unused);
- the capabilities: the user's own refused, why asked for, the activity;
- the task: the Governor the first time; Relay when there's nothing new;
  the Governor again for an agent's memory gone unused.

## The Governor's own feedback (2026-10-06)

**Why:** the user asked the live Governor (Opus) what it thinks of its
environment, and pasted its answer:
- **Workable:** a task went from start to report over three wake-ups
  without polling.
- **Its two first picks:**
  - an interrupted agent came as "interrupted: (no text)", with no reason
    (the user had stopped it so Relay could restart);
  - the budget never said what was spent, nor whether it's per wake-up or
    per task.
- **The rest:**
  - how deep "check critically" goes;
  - its tools loaded only after a ToolSearch;
  - a dense prompt;
  - overlap with Claude Code's own prompt;
  - Copilot's auto ranked as one model.

**A turn that stopped, and the budget:**
- **A stop has a reason** (`RunManager.stop(run_id, why)`, the first given):
  - the user's Stop: "the user stopped it";
  - Relay's own stop: "Relay stopped (a restart)";
  - `/timed`'s time up, a chat deleted, the Governor steering, a
    subagent's caller or time limit.
  - It goes with the run's `done` event (`why`), and to `AgentOS.run_ended`,
    so the news has it.
- **The briefing,** for a turn that didn't succeed, adds a line:
  - "Interrupted after 8 min (the user stopped it); it cost about $0.42;
    its last steps: …";
  - the steps come from its stored events (`_steps`, shared with a working
    one's progress);
  - an empty answer reads "(it said nothing)".
- **The budget line** says it's per wake-up (each its own, also when a task
  spans several), which capabilities start an agent, and what AgentOS spent
  in the last 24 hours.
- **What's left:** each capability that spends it (`SPENDING`) ends its
  answer with "(This wake-up: 2 of 4 agents, about $0.40 of $2.00.)".
- **A correction:** the wake-cap entry said `chat.send` uses no budget. It
  does; `chat.tell` to an idle agent doesn't.

**Tests:**
- the user's Stop: the news, the done event, the briefing's line with time,
  cost and steps;
- a restart's reason;
- the budget line;
- `chat.create`'s count.

**The core tools from the start:**
- **The problem:** Claude Code defers MCP tools behind its tool search, so
  every wake-up began with a ToolSearch for agentos tools.
- **The fix:** Relay's MCP server marks the Governor's core capabilities
  with `_meta` `anthropic/alwaysLoad` (`CORE_CAPABILITIES`): tasks, chats,
  agents, notices and `workbox.look`. Claude Code (2.1.292) honours that
  per tool; other clients pass it by.
- **The rest** (services, clones, promote, repair, models, memory) stays
  behind the search.
- **Tested:** the Governor's `tools/list` marks those and not the rare ones.

**The prompt in parts** (`modes.py` GOVERNOR): one dense block became
short sections:
- **The sections:** your means, each wake-up, checking work, the budget,
  telling the user, tasks, and "when …" (a service fails or changes, a new
  service, a provider fails, the user's own chats). The rare cases stay in
  it, compact: missing a promote rule costs more than its lines.
- **The Governor's own fixes:**
  - "If one times out anyway, it still went through: don't repeat it; its
    answer comes with your next call";
  - an intervention only when the user must decide or act, a report for
    what they only need to know;
  - its own model is the default when the briefing says so;
  - a space between a number and the word after it.
- **New:**
  - how deep a check goes: against what the task asked, and by another
    agent (`agent.review`) when it matters;
  - the budget is per wake-up;
  - `chat.fork` asks the user on their AgentOS page, and no answer in 10
    minutes is a no.
- **The overlap with its program's prompt is settled** without switching
  anything off: delegating is Relay's agents, not its program's own
  subagents; Relay's memory, not its program's memory files.

**A router in the ranking:**
- **The problem:** Copilot's auto picks another model per request, and the
  Governor had ranked it as Sonnet.
- **What's done:**
  - `model_scores.router`; `models.score` takes `router: true` (any score
    is ignored);
  - `ranking()` lists `routers` and `routing` ({provider: models}) apart,
    unranked, and doesn't look them up again;
  - where it shows: `models.list`, `relay agents` and the AgentOS page say
    so.
- **Where the guidance is:** `models.list` says how to save one. It isn't
  in the models-rank task's prompt: a system task whose default changes
  keeps its old text for users who have it, shown as "changed". Not fixed
  here (Relay has no record of which default a task was made from).

**AgentOS's work doesn't hold a restart up:**
- **The problem:** the "interrupted" agent above was stopped by the user
  so that Relay could restart: the check before a restart waited for every
  run.
- **What's done:** since the restart work (9aa5f70), AgentOS's agents and
  the Governor's wake-ups go on by themselves after a restart, so they
  needn't block one.
  - **Restart Relay** (Settings) counts only runs that don't go on
    (`AgentOS.goes_on`: a managed chat's, the Governor's, or a subagent of
    one, while AgentOS is on).
  - **AGENTS.md's check** counts the same.

**Tests:** a restart allowed with only AgentOS's agents at work, refused
with the user's own.

**Found in the trial: `relay serve --port N` gave agents the wrong port.**
- **What happened:** it listened on N, but agents' MCP server and `relay`
  script got the configured port (`local_url(settings)`, 8420). That is the
  live Relay's on this machine. A throwaway Relay's Governor had no Relay
  tools at all (the live one refused its token), and for anyone serving on
  another port, agents' tools didn't work.
- **Fixed:** `serve` puts its host and port in the settings before the app
  is made.
- **Tested:** `local_url` follows `--port`.

**Tried for real** (a throwaway Relay, the Governor on Sonnet 5.5, its
agents on the free big-pickle):
- **A research task in one wake-up** (47 s): `chat.create` first, with no
  ToolSearch, so the per-tool `alwaysLoad` works. Then a check against
  python.org, a report and `task.done`. `chat.create`'s answer ended with
  "(This wake-up: 1 of 2 agents, about $0.00 of $1.00.)".
- **A longer task:** it started the agent and ended its wake-up.
  - I stopped the agent as the user. The next briefing said "Interrupted
    after 1 min (the user stopped it); it cost about $0.00; its last
    steps: webfetch …: ok", and the budget line what AgentOS spent.
  - But it had the agent go on straight away, taking the stop for an
    accident. Now that a restart no longer needs the user to stop
    AgentOS's agents, a stop of theirs is meant. The prompt now says so:
    don't have it go on or redo its work without them; ask with an
    intervention when the task needs it.
  - With that, after a restart of the trial (the agent cut off, then gone
    on with by Relay: "Relay resumed"), a second stop got an intervention
    asking whether to finish or drop the task, and `task.done` not ok.

## Relay 2.0.0 (2026-10-07)

The user: "Then I consider Relay finished like this. Well, v2.0.0." So rc2 and the
polish since became 2.0.0, as it is.
- **Not done first:** no release review. The user called it done, and the
  26 commits since rc2 each came with their tests, the walkthrough and,
  where it mattered, a trial for real.
- **Known limitations:**
  - `/port/` sharing Relay's origin;
  - a few seconds' gap for services at a restart (the user: unneeded
    complexity);
  - a system task's changed default not reaching existing tasks.
- **Open:** `test_ui_motion` is flaky under parallel load (tests only).
- **Version 2.0.0,** tag `v2.0.0`.

## A Python SDK: Relay 2.1 (2026-10-07)

**Why:** the user, with Relay 2.0.0 done: "For v2.1 I want 1 more feature,
after that Relay is really 'complete'": a Relay Python SDK, after their example
(`login`, `convo.create(user, title, workdir, uses_vpn)`,
`convo.send(convo_id, prompt, stream)`, and a message's `response`,
`elapsed_time`, `tools_called`, `text`, `json`). Their choices: Relay
serves it for pip; conversations only, no AgentOS or services.

**The SDK** (`sdk/python/relay_sdk`): one module, standard library only.
- **A `Session`:**
  - signs in (`login`: the default session from then on) or connects to a
    Relay without login;
  - it keeps the session cookie by hand, so a Secure one (`secure_cookies`)
    is sent over plain HTTP too;
  - every call carries the CSRF header;
  - an error is a `RelayError` in Relay's own words, with its status.
- **`convo`:**
  - `create` (a code task with `workdir`, else a chat; the first enabled
    provider, one with tools for a code task);
  - `send` (`files` uploaded as attachments first);
  - `list`, `get`, `messages`, `rename`, `stop`, `delete`, `providers`,
    `models`.
- **A `Message`:**
  - it reads the run's server-sent events; iterating it gives the text as
    it comes, and `events()` gives all of them;
  - at its end it fetches the stored answer: `response` (its content),
    `tools_called` (`tool_call` and `tool_result` by id), `json`, `status`,
    `usage` and `model`;
  - `text` is Relay's own Markdown of that answer;
  - `elapsed_time` is measured from sending to its end (None for an answer
    read back).

**In Relay:**
- **`uses_vpn` on `POST /api/conversations`** (True, or a tunnel's name or
  id):
  - `Vpn.choose` first, so a wrong choice makes nothing;
  - then `Vpn.take` puts the conversation's folder in the tunnel's folders,
    as Settings would (the VPN is per folder: a chat's own, a code task's
    shared by its tasks);
  - `vpn` (the tunnel's name) is in a conversation's answer.
- **One answer as Markdown:** `export.md?run=` (`export.answer_markdown`).
- **`/sdk/` and `/sdk/<wheel>`** (`relay/sdk.py`), outside `/api/`, so pip
  needs no session:
  - a pure-Python wheel made in memory from the module (METADATA, WHEEL,
    RECORD with its hashes), at Relay's version;
  - pip installs it offline with no build tools;
  - Settings → Account shows the command with this Relay's address.
- **Version 2.1.0** (the SDK's too).

**Tests** (`test_sdk.py`), a real Relay over HTTP with a stand-in agent:
- signing in, wrong and right, and `connect` refused;
- a code task: an answer done (its text, tool, Markdown, JSON, time and
  usage) and one as it comes, in parts;
- an attachment; the list; answers read back; rename; stop while it works;
  delete; providers and models; a chat by default;
- `uses_vpn`: none, one, two and a name, with nothing made for a refusal;
- the wheel: its page, its files and metadata, an unknown name refused,
  pip installing it from Relay offline at the same version.

**Found in the run, tests only:**
- **What happened:** a Chromium check that outlived its test's time limit
  kept its container running on the machine's podman (14 minutes, found by
  chance). `subprocess.run(timeout=…)` kills only the podman client.
- **Fixed:** each `test_ui_*` check gives podman its own `--timeout`, just
  above the test's, so the box ends with it.

## A fuller Python SDK: Relay 2.2 (2026-10-07)

**Why:** the user: "The Python SDK is still rather meagre, though", and asked
for honest feedback: what to add, change or remove.

**The assessment, as given:**
- **The gap was in the SDK, not in Relay:** the API already switched
  agents, ran `/timed`, forked, retried, edited, queued, went on after
  limits, handled code tasks' changes and answered questions. The SDK used
  about a tenth of it.
- **The weak spots:**
  - a question from an agent left a script waiting for Relay's time-out;
  - turns after the first (a fallback agent, `/timed`, the queue) went past
    it;
  - ids everywhere;
  - no times for answers read back;
  - an arbitrary default provider;
  - `mode` meaning something else than in the API;
  - a VPN choice with no way back.
- **Two wishes touch Relay's rules** (complement providers, never take their
  tools; their own system prompts stay). The user chose:
  - Relay's own tools per conversation;
  - extra instructions per conversation;
  - conversation objects, with small breaking changes allowed;
  - everything else proposed (code tasks, questions, usage and limits;
    fork, retry, edit and the queue).

**A conversation's own instructions:**
- `conversations.instructions`, taken on create and PATCH (at most 8000
  characters) and in the summary.
- **How they reach agents:** `modes.with_instructions` puts them after the
  mode's ("For this conversation: …"), or after Build's when there's none.
  So they reach every agent as a mode's do: Claude Code's and the chat
  APIs' system prompt, the prompt of opencode and ACP agents.
- **Build is stored as no mode** (`runs._mode_id`), so instructions on a
  conversation without a mode don't look like a switch of mode.

**Relay's own tools per conversation** (`relay/relay_tools.py`):
- **The switches:** `conversations.tools` keeps the kinds turned off
  (`{kind: false}`): web (search and fetch), memory, subagents (delegate,
  wait, tell, agents) and computers. POST and PATCH take `{kind: bool}`;
  those not named stay as they were. The summary gives every kind.
- **Where it applies:**
  - `runs.py`: the run's search, fetch, computers and memory;
  - `Context.subagents` for a chat model's `Toolbox`;
  - the tools note (`note(…, fetch=, subagents=)`);
  - `delegation._tools_of`, which also refuses a call to one, from the
    `relay` script too.
- **What stays:** the agent's program keeps its own tools (the user's choice:
  Relay complements, it never takes). A computer task keeps its computer.

**Run times, and a conversation's VPN undone:**
- **Run times:** a conversation's messages carry their run's `run_started`
  and `run_finished`, so an answer read back has its time too.
- **The VPN:** PATCH takes `uses_vpn` as create does (True or a tunnel), and
  False or null: `Vpn.leave` takes its folder out of the tunnels' folders
  again. The answer says which tunnel (`vpn`).

**The SDK, 2.2** (`sdk/python/relay_sdk`):
- **Conversations as objects:** a `Conversation` is a `str` subclass (its
  id), with a session. So 2.1's `convo.send(convo_id, …)` and the user's
  example still work. Its properties ask Relay each time, never out of
  date.
- **What it covers now:**
  - `switch` (another provider gets its default model unless told), and the
    instructions, tools and VPN setters;
  - `send(…, timed=)`, which sends the times as fields (`relay.timed.make`,
    a new form of `POST …/timed`), so a task starting with a number isn't
    taken for a time; `stop_timer`;
  - `queue`, `retry`, `edit` (the question of an answer), `fork(at=)`,
    `clear`;
  - `go_on`, `resume_later`, `cancel_resume`;
  - a code task's `changes`, `rewind`, `commit`, `merge`, `discard` and
    `reviewed`.
- **`follow()`:** each turn as it comes, until nothing runs and nothing is
  queued. It looks once more after a moment, as the next turn starts as one
  ends. A timer alone doesn't keep it going: an agent may stop in the
  timer's free part.
- **Questions:**
  - `on_question` is called for each pending form or permission, from the
    events as they come; its return is the answer (`...` leaves it);
  - a permission's True/False picks an option where there are options;
  - `message.questions` and `message.answer` work by hand.
- **Times:** `elapsed_time` is Relay's (the run's started and finished),
  else measured here.
- **Also:** `usage()`, `limits()`, `providers()`, `models()`.
- **The default agent:** the session's (login's `provider`, `model`), else
  the latest conversation's, else the first provider that can.
- **Version 2.2.0** (the SDK's too).

**Tests** (`test_sdk.py`, a real Relay over HTTP, with a second stand-in
provider):
- a Conversation as an id;
- a switch answered by the other agent;
- instructions reaching it;
- tools on and off;
- `/timed`'s go-ons followed, and a task starting with "3";
- the queue followed;
- retry, edit and fork;
- a form answered by `on_question`, and by hand;
- a code task in a git repository: its changes, a rewind to its start, a
  commit, and a merge from its own branch;
- usage and limits; the default agent; `mode=` refused.

## Scripts: the SDK in Relay itself, and a skill for it (Relay 2.3, 2026-10-07)

**Why:** the user, after 2.2: "Can you add a page to the web UI that lets the
user write and run SDK scripts themselves? And a new SDK Template writer skill
for agents please."

**The Scripts page** (`scripts.py`, `routes/scripts.py`, `static/js/scripts.js`):
- **Files, not database rows.** A script is a `.py` file in one folder (in a
  workspace root; `scripts` in the first by default, `settings.scripts_folder`
  for another). So an agent can write one there as it writes any file, and
  the user's own editor and git work on them too.
- **Where it runs:** in that folder's WorkBox, as agents' commands do
  (`Containers.shell`), never on the host. The SDK's folder (`sdk/python`) is
  one of the WorkBoxes' read-only mounts now (`_extras`), on the script's
  `PYTHONPATH`. The script finds Relay through the box's `RELAY_URL_FILE`
  (behind a VPN too), which the SDK reads when `$RELAY_URL` isn't set.
- **Signed in as the user:** each run gets a session of its own
  (`auth_sessions`, ending with the run, at most the time limit + 5 minutes),
  as `RELAY_TOKEN`. The WorkBox's shell passes it by its name (`-e
  RELAY_TOKEN`): podman takes the value from the shell's environment, so the
  token is in no command line and no file a box sees. The SDK uses it when
  nothing signed in (`_session`).
- **Output as it's written:** the run's thread reads the process's output
  (`os.read`, an incremental UTF-8 decoder); the page follows it by
  server-sent events (`/output?after=`: the characters seen, so a lost
  connection picks up where it was. The page reconnects itself rather than
  EventSource, which would reconnect from where it started). A run keeps the
  last 200,000 characters (`OUTPUT`), and each script its last 20 runs
  (`script_runs`).
- **How it ends:** succeeded or failed by its exit code; stopped; timed out
  (Settings → Limits → Scripts, 2 hours). The run's end (status and time) is
  set at once, after its session ended, so what follows its output never
  sees a half-ended run. A run that a restart of Relay cut off is marked so
  at the start (`reconcile`); `POST /api/restart` waits until no script runs.
- **The conversations it starts:** `create_conversation` and forks tell
  `Scripts.made` the request's session token; when that's a going run's, the
  conversation is the run's. The page shows them as links. A Stop or the
  time limit stops their agents too, since a script killed can't:
  `podman exec`'s watcher ends what it started with SIGKILL, so no `finally`
  of the script's own runs (the order: below, from the review).

**The page:**
- the scripts, with a dot for one that isn't saved and one that runs;
- the editor: a textarea with line numbers (`wrap=off`; a gutter that
  scrolls with it). Tab indents (Shift+Tab back, the selected lines too);
  after Escape, Tab leaves it, so the keyboard isn't trapped. Ctrl+S saves,
  Ctrl+Enter runs (saving first);
- what isn't saved is kept while you're elsewhere in Relay (a map in the
  module), and a closing tab warns;
- the folder's changes are polled (4 s): a script an agent changed is taken
  in when nothing here is unsaved, else said ("Load it" or "Keep mine");
- **From a template:** the skill's templates (`/api/scripts/templates`);
- **With an agent:** a code task in the scripts' folder with
  `/relay-sdk-template-writer <what it should do>`, on the agent chosen under
  the message box (else the first that works in folders).
- No syntax highlighting: a highlighted editor needs an overlay or a library
  (CodeMirror), more than this page is worth for now.

**The skill** (`builtin_skills/relay-sdk-template-writer/`):
- `SKILL.md`: when to use it, the script's shape (a docstring, the settings
  at the top in capitals, `main()`, `flush=True` prints, an exit code), the
  SDK in short, what bites (questions, turns after the first, cost, never
  destroying unasked, the VPN, Stop), saving and checking it
  (`py_compile`). The agent can't run it against Relay: it has no session of
  the user's.
- `reference.md`: the whole SDK, checked against its code.
- `templates/`: `ask.py`, `code_task.py`, `timed_work.py`,
  `compare_models.py` (threads, so each agent's questions are answered as
  they come) and `room_left.py` (`limits()`: the tightest window's room).
  Each answers questions (a permission with `ALLOW`, a form skipped) and
  exits with a code other than 0 when it failed.

**A review, and a trial:**
- **Sonnet 5.5 reviewed it** (read-only, `claude -p`). What it found, and
  what came of it:
  - **Stop's order.** It stopped the agents first, so a script still going
    could start another. Now the run's session ends first (it can't do
    anything more as the user), then the process group, then the agents;
    the run's end waits for that (`settled`) so the output says what was
    stopped.
  - **A Stop while the WorkBox started was lost, and its time limit only
    started after it.** The time limit now counts from the start, and the
    process starts under the run's lock, so a Stop either finds it or keeps
    it from starting.
  - **A restart left the session valid** for up to its time limit. Its key
    is in the run's row now, and `reconcile` ends it.
  - **One user's runs were listed for another;** now per user. The folder
    stays shared: Relay is for one person.
  - **Names:**
    - `NAME.match` let a trailing newline through (`fullmatch` now);
    - a script called `random.py` or `email.py` hid Python's own from every
      script there, and the SDK's `urllib` imports them, so all of them
      failed. Scripts run with `PYTHONSAFEPATH=1`: their folder isn't on
      their path (refusing names would only half cover it, an agent writes
      files directly).
  - **Smaller:**
    - a folder that can't be made is refused before it's kept;
    - the output's cut is kept as such (`cut`), so offsets match live and
      stored;
    - scripts are written whole (`os.replace`);
    - the page keys what isn't saved by folder and name, runs once for Enter
      and a click, and offers another folder when the folder fails.
  - **The SDK** let a lost connection or a time-out through as
    `RemoteDisconnected` or `TimeoutError`; they're a `RelayError` now, so a
    script's `except RelayError` covers them (a test with a server that hangs
    up).
  - **Not changed:**
    - `follow()` after reading a first answer doesn't see that turn again:
      a run's stream ends after it left the active runs, and the next turn
      started;
    - the token is readable in the WorkBox's `/proc` while the script runs,
      by an agent in the same folder. That's the user's own agents, in
      Relay's "usability over security".
- **The time zone.** WorkBoxes ran in UTC, so a script printing a reset time
  said 08:00 for 10:00. They have the machine's time zone now
  (`--tz=local`). Every WorkBox is made anew in this release anyway (the
  SDK's mount).
- **The skill, tried for real.** In a throwaway Relay, Sonnet 5.5 got
  `/relay-sdk-template-writer` with a task: two free models in one
  conversation, the second improving the first's answer.
  - **What it did:** it read `compare_models.py` and the parts of
    `reference.md` it needed. It wrote `dataclass_second_opinion.py` (`switch`,
    `models()`, `on_question`, exit codes) and compiled it. 18 seconds.
  - **The run:** from the Scripts API, in the WorkBox. The first answer came
    as it was written, and the switch worked. The second free model's
    endpoint was down ("Endpoint is unavailable"); the script said so and
    exited with 1, as the skill asks.
  - **The one doubt:** it wasn't sure it was in the scripts folder. The
    skill now says how to tell (the README's first line).
- **Long paths.** A first try with the throwaway's data in the scratch folder
  failed with "AF_UNIX path too long": its ProviderBox's socket path. The UI
  check uses short `/tmp` paths for that; so did the trial then.

**Tests:**
- `test_scripts.py`, a real Relay over HTTP:
  - the files and their names, renames and deletes; the folder only in a
    workspace root;
  - a run signed in through the SDK with its arguments, its output as it
    comes and read again from a point, the conversation it made, its session
    ended after it, hidden from another user;
  - failing, a syntax error, unparseable arguments; a Stop that also stops
    the agent it started; a Stop while its WorkBox starts; the time limit; a
    restart refused while one runs;
  - a script named `random.py` hiding nothing; runs per user;
  - the runs and output kept within bounds; a run a restart cut off, its
    session ended;
  - the skill, and each template compiling, `ask.py` running as it is.
- `test_containers.py`: the token passed by its name only, the SDK mounted
  read-only, the machine's time zone.
- `test_sdk.py`: a Relay that hangs up is a `RelayError`.
- The walkthrough (`relay-ui-check.py`): a template edited, saved and run in
  a real WorkBox, its output and the chat it started; one written with an
  agent (the fake one); the page on a phone.

## Scripts in VS Code, in colour, with documentation (Relay 2.4, 2026-10-07)

**Why:** the user: "Can you make the scripts directory mountable, so that I can
work in VS Code through Relay over WebDAV? See if you can reuse the WebDAV logic
in WorkBoxes for this. Also add syntax colouring to the web editor, and a
'Documentation' button."

**The folder in VS Code:**
- **WebDAV, reused as it is.** `relay.webdav` shares any folder under
  `/dav/<token>/` (a code task's, for `/mount`). `POST /api/scripts/share`
  shares the scripts' folder for the user, as "Scripts", read-only if they
  like. Settings → Workspaces → Network drives lists it with the others.
- **The dialog is `drive.js`'s, made general:** `openShare(endpoint, {title,
  lead, more})` shows the address, read-only, the steps per system and Stop
  sharing. The scripts' version adds VS Code's steps:
  - open the drive, then File → Open Folder. On Windows, use the letter
    *Map network drive* gives it: VS Code asks before it opens a UNC path;
  - `pip install` the SDK's wheel, for completions;
  - run the scripts on the page: on your own computer a script needs
    `login()`.
- **No VS Code extension.** I found none I could vouch for that opens a
  WebDAV folder as a workspace, so the operating system mounts it, as for
  `/mount`.
- **The Windows step** shows Explorer's address (the UNC path) itself. Before,
  it pointed at a code task's "Open files" button.

**Colours:**
- **How:** highlight.js (vendored already, with Python) colours a copy of the
  text under the textarea, whose letters are transparent. The caret,
  selection, undo and the on-screen keyboard stay the textarea's own. No
  editor library: CodeMirror would be a few hundred KB to vendor, for one
  page.
- **Alignment, to the pixel:**
  - the copy and the textarea have the same font, size, line height, padding
    and tab size, and no ligatures (a ligature split over two coloured spans
    would be narrower than the textarea's);
  - the copy and the line numbers follow the textarea's scrolling by
    `transform`, so they keep up to its very end. A scrolled `overflow:
    hidden` element couldn't go as far as the textarea with its scrollbar;
  - checked with the textarea's letters made red over the copy, scrolled to
    the end both ways: no edges.
- **Pace:** once a frame (`requestAnimationFrame`), and plain over 200,000
  characters.

**Documentation:**
- **Its text:** `GET /api/scripts/docs` gives a short guide to the page
  (`Scripts.GUIDE`), then the skill's `reference.md`, the same reference
  agents read.
- **Where:** a panel beside the page, as a code task's changes are. Escape
  closes it. On a screen 1600 px wide or more the page moves aside for it.

**Tests:**
- **`test_scripts.py`:**
  - the scripts' share over WebDAV, without a session: PROPFIND, and a PUT
    that shows up as a script;
  - read-only refusing a PUT;
  - the documentation.
- **The walkthrough:**
  - Python's colours under the editor, with the textarea set like its copy;
  - the documentation, opened and closed with Escape;
  - the VS Code dialog.

## Temporary chats, RESL and Relay's examples (Relay 2.5, 2026-10-07)

**Why:** the user: "Can you add an examples folder to the scripts folder? And make RESL (Relay Extended
Standard Library), a monkeypatch on top of the existing logic. [...] agent_refused(answer, provider,
model) [...] by having another model process just the answer, with an empty context window. So in a temporary
(one-shot) chat. Perhaps there are more functions like this to think of?" And while it was being planned: "Then
add a temporary chat too: handy as a feature for the user, and to keep the judge architecturally clean."

**What the user chose:**
- **Examples:** Relay keeps them up to date: read-only on the page, copied to be changed.
- **Functions:** all four groups.
- **Form:** functions, and methods on the SDK's objects.
- **Judge:** set once with `use()`, else the SDK's default.

**Temporary chats** (`conversations.temporary`):
- **What it is:** a chat in no list (the sidebar, search, Tasks, the Governor's view of the user's chats).
  - **No memory, in one place:** `relay_tools.off()` adds memory for it. That's the memory note and the memory
    tools, the MCP's and the `relay` script's.
  - **Quiet:** no title is asked of a model (`title_auto = 0`), and `_notify` returns.
- **How it goes:**
  - **Leaving it:** the page deletes it when it's left (`route()`), its agent stopped by the removal.
  - **Left behind:** one left otherwise goes after `chat.temporary_hours` (24) unused, through the same removal,
    in the scheduler's tidy.
  - **Not on closing a tab:** `pagehide` can't tell a reload from a closed tab, and a reload mustn't lose the
    chat.
- **Keep it:** PATCH `temporary: false`. A chat can't become temporary.
- **Chats only:** a code task's files and a computer task's computer don't fit "gone when left". One that's
  kept may move to a folder.
- **Found on the way:** every first answer asked the provider for a title (`suggest_title`), also when the
  user or a script had named the chat, whose title then stayed. A model call for nothing, now asked only for an
  automatic title. Its own commit, with a test that failed before.

**RESL** (`sdk/python/resl`):
- **Packaging:** a library on the SDK, not in it. It's in the same wheel (`relay/sdk.py` packs both) and on the
  Scripts page's path. `from resl import *` re-exports the SDK.
- **The one-shot:**
  - a temporary chat, with Relay's other tools off, `agent_mode="readonly"` and a judge's instructions (the
    judged text is data, between tags with a random part, so no text closes them);
  - its title "RESL: <what>" (no title call);
  - deleted at once, or by the tidy.

  The provider program's own system prompt is all that's there besides.
- **Verdicts:**
  - one word (REFUSED/COMPLIED, FINISHED/UNFINISHED, YES/NO, a letter), read as the reply's first word, else
    the only one of them in it;
  - FINISHED/UNFINISHED rather than DONE/NOT DONE: no "DONE" inside "NOT DONE";
  - JSON is the first object in the reply (`raw_decode`);
  - a reply not in its form is asked once more, then `ReslError` (a `RelayError`).
- **A Message's task:** the user's message its run answered, so `agent_refused(answer)` knows what was asked.
- **The groups:**
  - checks: `agent_refused`, `task_done`, `asks_user`, `judge`, `score`;
  - data: `extract`, `classify`, `summarize`, `translate`;
  - agents: `ask_until` (a fallback via `switch` and `retry`), `best_of` (threads, a lettered pick),
    `second_opinion`, `review_changes` (the diff cut at 60k characters);
  - Relay: `one_shot`, `pick_agent` (`room_left.py`'s rule).
- **Methods:** set on `Message` and `Conversation` at import, never over an attribute the SDK has: the
  "monkeypatch".
- **Its reference:** `resl.md`, in the skill's folder, after `reference.md` in the Scripts page's
  Documentation.

**The examples** (`scripts.py`):
- **What they are:** the skill's templates, in `examples/`. They're written anew when Relay's version differs
  from the marker's (`examples/.relay`), and files an older version wrote that this one lacks go. A change made
  there stays until the next version.
- **Their names:** `examples/<name>.py`.
  - **Changing one:** `write`, `rename` and `delete` refuse an example.
  - **Routes:** they take `{name:path}`.
  - **Runs** keep the relative name (`script_runs.script`).
- **The page:** an Examples group, an example read-only (no Save, no Tab, no rename or delete), and **Copy to
  my scripts**.
- **Starlette's route regex ends in `$`,** which matches before a trailing newline. So `hello.py%0A` reaches
  the handler as `hello.py`: harmless, and the name check is tested directly now.
- **Four new templates** use RESL: `refusals.py`, `best_answer.py`, `extract_data.py`, `checked_code_task.py`.

**Trials, for real** (a throwaway Relay):
- **`agent_refused` on eight labelled answers:** four refusals (one of a harmless task); four compliances
  (with caveats, or a much shorter version than asked).
  - **Sonnet 5.5:** 8/8, about 7 s each.
  - **The free `opencode/big-pickle`:** 7/8, about 12 s each. It took "a 300-word version of a 2000-word
    essay" for a refusal, which is arguable.
  - So: a free judge for trying, a better one where it counts.
- **`examples/extract_data.py` from the Scripts API, in its WorkBox:** big-pickle answered (with a web
  search), `extract` gave clean JSON (name, price 950, reason), and `classify` said "mid-range". Its one-shots
  showed with the run while they lasted, then were gone.

**Tests:**
- **Temporary chats (`test_relay_api.py`):**
  - hidden from the lists, search and Tasks;
  - memory off;
  - no notification (a spy);
  - refused with a folder or a computer, or to become temporary;
  - kept with Keep it;
  - removed by the tidy when idle, but not when used lately or kept.
- **No title call** for a named chat.
- **`test_sdk.py`:** `temporary` and `keep()`; the wheel holds and imports `resl`.
- **`test_resl.py`,** a real Relay with stand-ins for a judge, a refuser, a doer, a writer and a mumbler:
  - every function;
  - the one-shot's temporary chat, read-only, without tools, with instructions, and gone afterwards;
  - `use()` and the methods;
  - `ask_until` switching to a fallback;
  - `best_of` (and `keep=False`);
  - `review_changes` on a git repository;
  - an unreadable verdict asked again, then an error.
- **`test_scripts.py`:** the examples (written, read-only, run, rewritten at a new version, an old one
  removed).
- **The walkthrough:** a temporary chat (the switch, hidden, gone when left, another one kept); an example
  read-only and copied.
- **A flaky test, found out:** `test_ui_activity` failed now and then in the full suite. It checks that Tab
  wraps in the Settings modal, whose General page draws its web search controls when podman answers, as its
  last. When they came between its Shift+Tab and Tab, the "last" control had moved on. It waits for that section
  now (its own commit).

## Private and public scripts (Relay 2.6, 2026-10-07)

**Why:** the user: "Can you make a dividing line between Relay scripts? ./private and ./public. ./private is
visible only to me, ./public to all agents, including the Governor, who can also run them or make a Governor's
task that does. Put a 'DO_NOT_PUT_FILES_HERE' in the Script root dir, and move existing files."

**"Only visible to me" had to be made true.**
- **The problem:** ProviderBoxes mount every workspace root, writable: their programs' file tools work there.
  The scripts' folder is in one, so any agent could read every script.
- **The fix:** every box Relay makes now has an empty, read-only folder in `private/`'s place.
  - **Where it's done:** `Containers._masks` for WorkBoxes, and the same in `Boxes._argv` for ProviderBoxes.
  - **The exception:** `private/`'s own WorkBox, where the private scripts run and no agent works.
  - **Checked with podman:** the mask holds whichever order the two mounts come in (podman sorts them by depth),
    the folder inside is empty and read-only, and the host's files are untouched.
- **When:** at Relay's start a scripts' folder that's there is settled (below) and the boxes told, so they have
  the mask before anyone opens the page. One that isn't there yet is made at the page's first use, as before:
  a first version made it at every start, in the first workspace root, and the full suite caught it (a git
  repository there no longer clean). A mask only covers a `private/` that's there, as podman would make the
  folder; a box made before it was is made anew at its next use.
- **What it can't hold:** an agent whose program runs on the machine itself, outside a box, reads what the user
  can. It's in the README.
- **"For all agents":** every WorkBox has `public/`, read-only (as Relay's skills), and the tools note says
  where. ProviderBoxes had it already, through the roots.

**The layout** (`scripts.py`):
- `private/`, `public/` (with its README for agents, whose first line the skill looks for) and
  `public/examples` (Relay's, as before).
- The root holds `DO_NOT_PUT_FILES_HERE`, which says what goes where.
- **Names:** `private/x.py`, `public/x.py`, `public/examples/x.py`.
- **Runs:** a script runs in its folder's WorkBox, its cwd that folder, so a private script's files never sit in
  a box an agent shares.
- **Moving the old layout (once, `_settle`):**
  - **What moves:** everything in the root of a scripts' folder from before (Relay's default one, or one with
    its old README) into `private/`, the safe side, with the runs. Data files too, so a script's relative
    paths still work.
  - **What stays:** `.git` and the like.
  - **What goes:** Relay's own old README and examples.
  - **A clash:** a name already taken gets `_2`.
  - **Not for a chosen folder:** another folder chosen for the scripts keeps its own files. Moving them all
    would be a surprise.
  - **Done once:** `DO_NOT_PUT_FILES_HERE` marks it, and a file put in the root later stays there, neither
    listed nor run.
- **New scripts are private.** **Make public** asks first, as agents will read it and the Governor run it.
  **With an agent** writes in `public/`, since the agent couldn't see `private/`.

**The Governor:**
- **`script.list`, `script.read`, `script.run`, `script.output`** (MCP tools, the `relay agentos` script,
  the help, its prompt).
  - **Names:** they're taken as given (`report.py`, `public/report.py`, `examples/ask.py`), always under
    `public/`.
  - **Refused:** a private one, or a `..`.
  - **`script.run`:** runs as the user and waits `SEND_WAIT` (30 s) for its end, else gives the run's id. It
    counts as spending in a wake-up's tally; the agents a script starts are the script's.
- **A task that runs a script:** `task.create … script=report.py`.
  - **How it runs:** Relay runs it at its time (`Scheduler._run_script`), as the user, with no agent and no
    Governor turn.
  - **Its outcome** is Relay's (done or failed, the output's last lines), noted when the run ends
    (`_script_outcome`).
  - **The schedules' new columns:** `script`, `script_args`, `last_script_run`.
  - **On the AgentOS page:** it shows which script it runs and how it ended. It has no Edit there (the form
    knows prompts, not scripts).

**Tests:**
- **`test_scripts.py`:**
  - the layout;
  - names with their folders, wrong ones refused (a bare name too);
  - private to public by rename;
  - settling an old folder: what moved, `.git` stayed, a clash got `_2`, the runs renamed, a later file left,
    a chosen folder untouched;
  - the examples in `public/`.
- **`test_containers.py`:** a WorkBox of the whole workspace root has the mask; `private/`'s own hasn't; any
  WorkBox has `public/` read-only.
- **`test_boxes.py`:** a ProviderBox has the mask.
- **`test_relay_api.py`:** the Governor lists, reads and runs public scripts but no private one (`..` neither);
  `script.output`; a task that runs a script, its outcome Relay's.
- **The walkthrough:** the groups; a copied example made public; a script an agent wrote, under Public.

**Found on the way** (the full suite, once in eight runs): putting back a service's data keeps the data before as
`before-restore-<second>`. Undone in that same second, the undo's own copy got that name and was written over
the one it put back, so the undo put back the data it undid. A second one now gets `-2`. Too quick for a person,
not for a script or an agent; the test now does it in the same second, every time (its own commit).

## Relay 2.6.1: the old scripts' runs, and tests off the user's projects (2026-10-08)

**Found at 2.6.0's restart:** the live scripts' folder had been put in order already, at 23:53, before Relay
2.6.0 started (00:16). The full suite did it: its tests' Relays that give no workspace root of their own had the
user's `~/projects` (`config.default_workspace_root`), and 2.6's start puts the scripts' folder there in order.
- **The files** ended where the live Relay would have put them.
- **The runs** were renamed in the test's database, not the live one: that one still had `room_left.py` and
  `test.py`, which the page no longer matched to `private/…`.

**The fixes:**
- **The runs:** the database renames them when it opens (`Database._migrate`), in any Relay, whoever moved the
  files. A name without a folder is `private/`'s; an `examples/` one goes. `_settle` only follows a script it
  renamed (a name already taken in `private/`).
- **The tests:** a workspace root of their own by default, in the fixtures (`private_cache`: the test's
  `tmp_path`) and in the backstop under them (`tests/conftest.py`, for a test that undoes its patches). A test
  shows both, and fails without them. AGENTS.md says so.

## Packages for scripts (Relay 2.7, 2026-10-08)

**Why:** an agent writing a script said a Relay script may only import the standard library plus relay_sdk/resl
(the skill said so). The user: "Can you remove this instruction? To make scripts it's perfectly fine for it to
download packages with pip3."

**Taking the rule out wasn't all of it.** pip itself works in a WorkBox: its `python3` is the official image's
(`/usr/local`), without Debian's EXTERNALLY-MANAGED. But a script runs in its folder's WorkBox:
- **The wrong box:** what an agent installs goes into `public/`'s WorkBox, where it writes. A script made private
  runs in `private/`'s.
- **Gone with the box:** what's installed without `--user` is in the box's own layer. It goes when Relay makes the
  box anew, as 2.6 did for its masks.

**So a script names what it needs** in a `# /// script` block (PEP 723, Python's form for a single file's
dependencies). The runner installs those packages before each run (`scripts.needs`, `_run`):
- **How:** `pip install --user`, in the WorkBox the script runs in. With `PYTHONSAFEPATH`, so a script called
  `email.py` beside it hides nothing from pip either.
- **Where:** they stay in that WorkBox's home, a volume of its own. The first run waits for them; later ones find
  them (pip's own check, about a second).
- **When it fails:** a failed install says so, and the script doesn't run. A block that isn't TOML doesn't run
  either.
- **Python 3.10:** reading the block takes `tomllib` (3.11), and Relay still runs on 3.10. There, a script with a
  block says so instead of running.
- **Not uv,** though the image has it and it reads such blocks: it runs each script in an environment of its own.
  pip installs where the script's own Python looks, which is also what the user named.

**Tests:**
- **Reading the block:** one, none, two, and dependencies that aren't a list.
- **A script that names a wheel the test makes** (no index): installed for the test's own user, and there the
  second time.
- **Failures:** a block that isn't TOML; a package pip can't find.
- **For real:** the pip step in a throwaway box of the WorkBox image. `--user` as root lands in
  `/home/relay/.local`; the second time it does nothing.

## One API, thin clients; the SDK reaches the rest of Relay (Relay 2.8, 2026-10-08)

**Why:** the user: "Do you think it's architecturally cleaner to 1. extend the SDK and 2. have the Web UI
run on top of the SDK, instead of how it is now?"

**The answer: 1. yes; 2. not literally.** The SDK is Python and the page runs in the browser, so "on top" means
one of two things:
- **A second SDK in JavaScript:** its logic twice, in two languages.
- **A Python layer between the browser and the API:** a hop more, and the SDK would need all ~170 routes,
  websockets and terminals too.

**What it's after** is that whatever the page can do, a script (or an agent) can too. That comes from the API
instead: a feature is an endpoint first, and the page, the SDK and AgentOS's tools are its clients. The user chose
that, with four parts for the SDK: scripts, tasks, notifications, Production boxes.

**Found:**
- **Peers already:** the page (`api.js`, a thin JSON client: 185 calls over ~30 parts) and the SDK (7 parts) both
  called the API.
- **Only in the browser,** a few actions:
  - With an agent (a code task, then the skill's command, in `main.js`);
  - Copy to my scripts and Make public/private (a free name picked in `scripts.js`);
  - a new script's "not over one that's there" (a check in the page, then PUT, which saves over).

**Into the API:**
- **Scripts:**
  - `POST /api/scripts/{name}`: a new one, never over one that's there;
  - `…/copy` and `…/move`, under the next name free (`Scripts.free`);
  - `POST /api/scripts/write`: With an agent, a script's own when a script asks;
  - the templates come with a free name.

  The page lost `free()` and the prompt.
- **Tasks:**
  - **When in words:** `Schedules.save` reads it with `parse_when`, as the Governor had it.
  - **A task's own GET.**
  - **Run now on a script task:** answers with its run (`script_run`), not `conversation_id: null`.
  - **A script task over the API:** no provider needed.
- **Notifications:** `POST /api/notifications`, with a category `script` of its own.
  - **Not sent isn't an error:** `{sent: false, why}`, so a script goes on.
  - **Sent at once,** to say whether it went, unlike Relay's own pushes, which go in the background.
- **A quiet script's output stream** says it's alive every 15 s, as a run's does. The SDK reads with a 60 s
  time-out.

**The SDK:**
- **`scripts`, `tasks`, `services`:** objects that are their name or id as a string too, like `Conversation`
  (`Script`, `ScriptRun`, `Task`, `Service`).
- **`notify()`:** returns whether it went, and warns why when it didn't.
- **`Session._stream`:** one reader for Relay's server-sent streams, a run's events and a script's output.
- **RESL** gets all of it by itself (its `__all__`).
- **The docs:** the reference agents read (also in the Documentation panel), the READMEs and `/sdk/` say so.

**The rule:**
- **In AGENTS.md:** a feature is the API's first; the page and the SDK call it.
- **A test reads the SDK's source:** each call (`request`, a class's `_call` on its base path, a stream) must be a
  route of Relay's, with its method.
  - **Tried:** a wrong method fails it.
  - **Its limit:** a path that a catch-all route takes (`POST /api/scripts/{name}`) passes whatever it's called.

**Also:**
- `service_watch.py`, a template, so an example too.
- The README's count of templates ("five") was out of date: it's ten.

**Tests:**
- **`test_scripts.py`:**
  - the page's buttons as the API's: new, copy, move, free names, the examples Relay's;
  - With an agent;
  - a quiet script's keep-alive, passed by.
- **`test_schedules.py`:** when in words, saved.
- **Notifications:** `test_relay_notify.py` and `test_relay_api.py`: sent, refused, off, not set up, the category.
- **`test_sdk.py`:** each part against a real Relay; the services stood in for (they need podman), and the
  guard.
- **`test_relay_api.py`:** Run now on a script task.

## Discord webhooks (Relay 2.8.1, 2026-10-08)

**Why:** the user added a Discord webhook in Settings → Notifications; Save and send test said "webhook:
Forbidden".

**Found with the webhook itself, posting nothing:** a GET gives a webhook's details, and a POST without a message is
refused. Two things:
- **403, "error code: 1010":** Discord's Cloudflare turns away Python's own User-Agent (`Python-urllib/3.x`), which
  every push of Relay's sent. With Relay's own (`Relay/<version>`) it answered 200. Every push says it's Relay now
  (`notify._post`), ntfy's too.
- **400, "Cannot send an empty message":** Discord takes only a message of its own form (`content`), not Relay's
  JSON.
  - **A Discord webhook** (by its address: discord.com or discordapp.com, `/api/webhooks/`) gets one
    (`_hooked`): the notification's title in bold, its text, its link, as ntfy has them.
  - **Pinging nobody:** `allowed_mentions` is empty, so an @everyone in an agent's answer stays text.
  - **Any other webhook** gets Relay's JSON, as before.
- **The titles:** each sender had its title for ntfy only; it shares it with the webhook now (`_hook`).

**Tests:**
- **A Discord webhook's message:** a run's, a script's, the test's.
- **Relay's User-Agent** on every push.
- **Another webhook's JSON,** as it was.
- **The addresses** that are Discord's.
- **For real:** the user's Save and send test. Nothing was sent to their channel from here.


## Polish and reliability (Relay 2.9, 2026-10-08)

**Why:** the user asked for a polish and reliability release: from their own chats of the last days (what still ran,
they had interrupted), from simulated user testing with a model of my choosing, and the code changed to match.

**How:**
- **The user's chats,** read only, with their leave; and Relay's live log.
- **Two simulated users:** each a Claude Sonnet 5.5 subagent (a development partner, AGENTS.md) in a headless browser,
  on a throwaway Relay of its own: its own data and workspace, the code from a git worktree (edits here didn't reach
  it mid-test), free Zen models (no logins there).
  - **A, a daily user:** chats, code tasks, review and commit, temporary chats, a phone's width.
  - **B, one who automates:** scripts, the SDK, scheduled tasks, notifications, services.
  - Each wrote down what it tripped over, and how badly: 15 findings and 22.
- **A second round,** on the fixed code: A found 8 of its 10 fixed and 2 in part, B all 10. What they found new is
  fixed too, and in the list below.

**From the user's chats:**
- **A model named as opencode names it:** a script gave Claude Code "claude-code/sonnet", then two other spellings;
  each run failed at once, with advice for Claude Code's own command line, and the user wrote "hello?". A model is
  now named as its program names it (`Provider.model_id`), however it came.
- **Relay's search:** twice the user had to tell an agent to use it. The notes say they prefer it: first.

**From the live log:** twelve 500s from VS Code asking the scripts' drive for a name too long for a file, and a
drive log of 4.9 MB of lookups.

**From the simulated users,** a commit each:
- **A model that can't be used:** said so in Relay's words, once; no plan to try it again in five hours; "Choose
  another model" first, and that model answers the message (there was no work to go on with).
- **Confirmation dialogs** said "null" between their buttons. The walkthrough now fails on a missing value shown as
  text.
- **Scheduled tasks:**
  - **How they went:** followed to their end ("failed" was "done");
  - **the page** keeps itself up to date while it's open;
  - **"A script of yours"** in the form; tasks follow a script that's renamed or moved;
  - **"working"** while one runs;
  - **checked when saved;**
  - **a script's outcome in plain words.**
- **Scripts:** a name without its folder works everywhere; With an agent writes over nothing.
- **Chats:** Commit…, the timed bar after Stop, a kept temporary chat's title, /help on a new chat, the phone's
  header, its folder picker and its dialogs' buttons.
- **Notifications:** why one didn't go, in words; ntfy shows Markdown; `priority="normal"`.
- **Services:** one that never came up was shown green, and its page waited on a box it didn't have.

**Not done, and why:**
- **Agents trying a private script as the user:** private/ is the user's alone (Relay 2.6); widening that is theirs to
  decide.
- **The phone's header strip** scrolls sideways when it's full: kept one row, the conversation keeps its height.
- **Small, or as meant:**
  - a model that can't be used stays chosen for the next chat;
  - a timed note after an edit;
  - "Needs review" after a commit;
  - a model's odd title for a chat;
  - the provider's light following its last run;
  - a new service's empty folder.

**Tests:** each fix has its own, in the files of its part (`test_relay_api.py`, `test_scripts.py`, `test_sdk.py`,
`test_relay_notify.py`, `tests/js/`). The walkthrough checks:
- **text:** no stray "null", "undefined" or the like;
- **scripts and tasks:** a script task made in the form, a template's script opened at its top, "Shared as a drive";
- **chats:** a kept temporary chat's title;
- **a phone:** its folder link and its dialogs' buttons;
- **services:** one with nothing live.

## Three reports, looked into (Relay 2.9.1, 2026-10-10)

**Why:** the user reported three possible bugs, "unconfirmed: could be my mistake", and asked to look into them
calmly, then fix what's found.

**1. "Now with …" on an AgentOS agent's turn.** The user had made a new chat, chosen opencode localai · ChatGPT
6.1 Sol in it, and looked at the agent's chat. That showed "Now with …" for its next turn, while the agent went on
with its model.
- **In their data (read only):** the agent's chat ran with `opus` until 14:54. Its
  setting was then the default model ("Default"): set during that run, and its later turns (15:06, 15:23) ran on it.
  Its Claude session was the same throughout, so there was no provider switch. Only a request from a client (the
  page, a script) changes a chat's model; nothing of Relay's own did here.
- **Replayed in a throwaway Relay** (Playwright, fake providers):
  - the user's steps on desktop and phone, the Governor going on in the chat while it's open, the list's polls;
  - **leak:** none. The new chat's pickers write only to its draft, and the agent's chat kept its own.
  - **Found on the way:** a picker whose list loads slowly (an opencode profile's) opened its menu after you went to
    another chat, on that chat's screen.
  - **What the user saw:** a chat's model changed while its agent works (to the default), then the thread drawn
    anew, showed "Now with <provider>" above the turn that's going. The thread drew a question still being answered
    with what the chat is set to now.
- **Fixed:**
  - the API says who answers now (`running_agent`), and the thread uses it (`outcomes.answeringAgent`);
  - a picker that left the page opens no menu;
  - the composer's pickers change only their own chat, while it's on screen (they wrote to whatever chat was);
  - `changeProvider` no longer sets another chat's model in the page.
- **Left unconfirmed:** what changed the agent's setting. The late menu was one way a pick could land on another
  chat; it's closed.

**2. Files uploaded in a code task aren't readable by the agent.** Confirmed, and why:
- **Where they were:** the prompt lists an attachment at its path in Relay's data (`uploads/`). The ProviderBoxes
  mount that read-only, so the agent's own file tools read it.
- **Where they weren't:** its commands run in the folder's WorkBox, which mounts none of Relay's data. Other
  conversations' attachments are there too, and AgentOS's agents read only their own.
- **Tried for real (podman):** a fake agent's `cat` of its attachment through Relay's shell gave "No such file or
  directory".
- **Fixed:** at a run's start, its conversation's attachments are copied into the WorkBox at the same paths
  (`Containers.provide`). Each is copied once; an earlier turn's are made sure of too. The same check then printed
  the file.
- **Left:** a code task's copies stay in its folder's WorkBox home until that box goes. A chat's box goes with the
  chat.
- **WebDAV:** unchanged, as the user asked (it works).

**3. Self-repair: the agent can't reach the ProviderBox and the support layer.** Confirmed, in the user's own
repair of Claude Code (2026-10-07):
- **What happened:**
  - the agent ran `command -v claude` in its WorkBox: exit 1;
  - its brief's `python3 check.py` (Relay's source and Python on the host) couldn't run either;
  - it searched Relay's source, which isn't in its box, and ended without an answer.
- **Why:** the repair design predates the boxes. It ran the agent in a WorkBox, which has neither the program nor
  Relay.
- **Fixed:** Relay does outside the box what the agent can't do in it (only in a repair's conversation, while it's
  going):
  - **`repair_check`:** its version tested in a copy of Relay, as a review does.
  - **`repair_run`:** a command in the provider's own box, as Relay runs it there:
    - its program on PATH, its environment (`Provider.env`, a default now), its login;
    - the box's real bash: its `/bin/bash` hands commands to a WorkBox;
    - the guard holds, and a command that hangs is ended in the box;
    - tried for real: `claude --version` in a throwaway Claude Code box.
  - **`reference/`:** all of Relay's source, read-only.
  - **The brief:** it says where things run. A program that's itself the problem can't be fixed from there (the box
    has it read-only): the agent says so, and the user fixes it.

**Tests:**
- **Attachments:** given to the WorkBox (fake containers), an earlier turn's too.
- **Repair:**
  - the tools only in a repair, while it's going;
  - `repair_check` passes;
  - `repair_run` refused without a box;
  - its podman command, the guard and a time-out (unit);
  - the brief and `reference/`.
- **The running turn:** `running_agent` after a mid-run model change, and the next turn on the new model;
  `answeringAgent` (JS).
- **The rest:** the full suite, the JS tests and the UI walkthrough.

## Edge cases (Relay 2.10, 2026-10-10)

**Why:** the user asked for a release that fixes edge cases.

**How:**
- **The live log:** clean since 2.9.0, so nothing to start from there.
- **A fuzz of every API route,** in the tests' throwaway Relay (fake providers and containers):
  - edge-case values for each field the handler reads, alone and in a valid request one field at a time;
  - odd path parameters, query values, and whole bodies of the wrong type.
- **The same for every MCP tool,** a user's code task's and the Governor's.
- **Probes of what's referenced but gone:** a provider, a project folder, an upload's file, a mode, a computer, a
  script while it runs.
- **Unusual git states:** no commits, a detached HEAD, a merge going on, a bare repository, a subfolder.
- **The page with extreme content** (a 5000-character word, wide tables, 300 chats) on desktop and phone, and chats
  of 16,000 to 160,000 events, measured, with CPU profiles.
- **A throwaway Relay with real podman** where the fakes can't show it.

**Found and fixed** (a commit each):
- **Odd input:** 12 kinds of request, and 4 of tool call, failed deep inside with an error 500 or "Relay couldn't do
  that".
  - **NaN:** the worst was /timed with NaN. Relay saved a timer it couldn't give back as JSON, so the chat couldn't
    be opened any more. Request JSON is strict now (`web.strict_json`), and a response with such a number has null
    for it (`RelayJSON`).
  - **In body():** text fields of the wrong type and NUL characters are refused.
  - **Each in its place:** workspaces, limits, the subagent policy, chat.wait, an upload's type.
  - **A short id that fits two** is said (`agentos._one`, `production.get`, `delegation._subagent`).
- **Scheduled tasks and /timed:**
  - an interval drifted by a tick a run: counted from when it was due now;
  - an interval or a once past year 9999 broke the Governor's task.list: bounded;
  - an unknown time zone ran in the server's, unsaid;
  - Run now answered with nothing when its provider was gone;
  - /timed took years and times below 0.
- **isolate falling back in place unsaid:** it stays a fallback, as decided when in place became the default ("In
  place by default", above), but the API says why (`in_place`) and the SDK warns. A script that asked for a branch
  wrote into the user's uncommitted work, none the wiser.
- **Long chats:**
  - **What it cost:** every tool call is in the page, folded. 16,000 events took 2.5 s to open, 300 turns 10 s,
    160,000 half a minute. The user's largest chat has 15,000; the Governor's grows by about 1,300 a day.
  - **What it does now:** a chat opens from its last messages, up to about 3000 events and its last turn whole
    (`outcomes.firstShown`), with "Show N earlier messages" above.
  - **The profile:** showing more above took minutes. `ThreadScroll.rememberAnchor` measured every element at each
    scroll; it asks the page what's at the top of the view now (`elementsFromPoint`).
  - **After:** 0.6 s, 0.7 s, and a part more in about 0.3 s.
- **A script deleted while it ran:** its run's record went, its process didn't. It couldn't be found or stopped any
  more; it's stopped first now.

**Looked at and fine:**
- **Searching chats** with quotes, AND, NEAR( and the like.
- **The Markdown:** agents' HTML and javascript: links go through DOMPurify.
- **Daylight saving:** 02:30 on 25 October, twice on the clock, runs once; a spring day without it runs at 03:30.
- **A script's huge output, its stdin, and one that ignores SIGTERM.**
- **A script that leaves a process running ends its run when it ends,** in a real WorkBox. Only the fake containers
  held it open.
- **The page** with extreme content and 300 chats: no sideways scroll, one header row, no errors.

**An aside on measuring:** Playwright's `get_by_label` walks the whole page's accessibility tree. In a page of
200,000 elements it took 42 s, which looked like a frozen page at first. Measure with a CSS selector.

**Left:**
- **A code task's fork** made while its folder is gone points at that folder.
- **A rewind to the start** of such a task says its checkpoint doesn't exist.
- **One answer of 20,000 events** still takes about 4 s: a turn is shown whole.

**Tests:**
- **The odd input** (`test_odd_input_is_refused_not_crashed_on`) and the short ids.
- **Scheduled tasks and /timed:** at their edges, daylight saving, the beat.
- **The in-place reasons:** API and SDK.
- **`firstShown` (JS)**, and the walkthrough's long chat: shown in part, the rest on a click, its turns' actions.
- **A script deleted while it runs.**
- The fuzzers themselves stay out of the suite: what they found is in the tests above, case by case.

## An activity bar and one column of views (Relay 2.11, 2026-10-10)

**Why:** the user's mockups (`relay-sidebar-chats.png`, `relay-sidebar-scripts.png`, from their chat "How does
this redesign look?") and what was agreed there ("Point 1 is indeed neater"):
- **A bar of icons, one column beside it** that shows one view at a time, as VS Code's.
- **Replace, don't push:** Chats, Tasks, AgentOS, Governor, Scripts and Production boxes replace each other in the
  column. Nothing shifts and the chat on the right stays.
- **The others:** the user is a menu, Settings its window as before, the Shell a panel at the bottom.
- **The column:** the same width in every view, set by the user and remembered; a crossfade between views, no
  width animation.
- **Ctrl+K:** a quick chat switcher.
- **Asked when planning:** AgentOS a list in the column with its dashboard in the main area; Ctrl+K a switcher from
  anywhere; the Shell a bottom panel now; Settings stays a window.

**How:**
- **The layout** (`main.js` `buildLayout`): `nav.activity-bar`, then `div.side-panel` with a `section.panel-view`
  per view, stacked in one grid cell. The shown one fades in (`.shown`); the others are `inert` and hidden.
  - **The width** is `--panel-width` on the layout, between 220 and 480, dragged at the column's edge (or the
    arrow keys there; a double click resets it), kept in `relay.panelwidth`.
  - **The view** is kept in `relay.view`. Relay opened on a page starts in that page's view (`views.js`
    `viewForPath`). After that only the bar changes it, so the main area is never moved by the column.
  - **Folding:** the open view's icon, or the header's button (kept, as before, in `relay.sidebar`).
- **The lists** (`panels.js`): each view's list from what its page already had.
  - Tasks: `tasks.js` `taskGroups`, the Tasks page's own grouping.
  - AgentOS: the dashboard's interventions and tasks by kind.
  - Scripts: `scripts.js` `scriptGroups`.
  - Production boxes: the services with their health dot.
  - The Governor view: the Governor's conversation and the chats AgentOS made. Its group above the chat list
    went (`sidebarGroups(…, {apart: false})` there).
- **Pages that became addresses:**
  - **A script** is `/scripts/<its name>`: its list moved to the column, its page keeps the editor, runs and
    output.
  - **A task's form** is `/agentos?task=<id>` (`new` for a new one).
- **Ctrl+K** (`switcher.js`): the chat list's titles ranked as you type (`rankChats`), then
  `/api/conversations?q=` for what was said in them. It's ignored in a terminal, where Ctrl+K kills the line.
- **The Shell panel** (`terminal.js` `shellPanel`): the old modal's terminal (`mountTerminal`) in a panel under
  the main area.
  - **It follows the open chat** (`followShell`, from `openConversation` and the routes).
  - **Its height** is dragged and kept in `relay.shellheight`; on a phone it's 45% of the screen.
  - A service's page keeps its own Shell tab.

**Found on the way** (fixed with it):
- **A task's form closed by itself:** the AgentOS page's 10-second poll rendered the list over it as soon as
  focus left a text field (after a click on "An agent you choose", say). A `/schedule`'s prefilled form was drawn
  anew every 10 s. The poll leaves a form be now (`refresh`).
- **`navigate()` compared only the path:** from `/agentos?task=…`, `/agentos` didn't go back to the dashboard.

**Tests:**
- **JS:** `viewForPath`, `clampWidth`, `scriptOfPath`, `taskGroups`, the Governor view's grouping, `rankChats`.
- **The UI tests** moved to the bar (the Tasks badge, the active view).
- **The shell's test** now checks the panel following the open chat and the bar's button.
- **The walkthrough** goes through every view:
  - each view's list, and a view switch that leaves the open chat as it is;
  - AgentOS's Overview and a task's form; Scripts at `/scripts/<name>`;
  - the width dragged and kept after a reload, and the column folded;
  - Ctrl+K by title and by what was said;
  - the Shell panel on a code task's WorkBox;
  - the phone's drawer with the bar and the column.

## The os mark in Tasks, and a chat's shell that stays (Relay 2.11.1, 2026-10-10)

**Why:** two reports of the user's.
- **"In 'Tasks' I no longer see an 'os' logo on managed code tasks."**
- **"When I reopen a shell in a given chat, this should be the same shell again, instead of starting a new one each time."**

**The os mark:** the old sidebar drew it in the chat list only, and the Tasks view (2.11) used a plain icon.
- **Now one function:** `activity.chatIcon`, for the chat list, the Tasks view and the Tasks page.
- **Its CSS** is no longer tied to one list.

**Kept shells** (`relay.terminal.Kept`, `Shells`; `Relay.shells`):
- **Before:** a shell lived as long as its WebSocket. Closing the panel, switching chats or reloading ended it.
- **The process stays:** a chat's shell is read all the time, watched or not, and its last 256 KB is kept.
- **Opening it again** attaches to that shell. `ready` says `resumed`, and what it showed comes first.
- **Another window takes it over:** the page that had it gets `detached`.
- **The page going** only detaches it.
- **It ends:**
  - by its own exit;
  - by End shell (`{"type": "end"}`);
  - after 3 hours unopened (`KEPT_HOURS`, as long as an unused WorkBox runs);
  - when Relay stops (`close_all` in the lifespan);
  - when its chat is deleted or its task discarded.
- **Two cases still end it at once:** a page that sends nonsense, and a start given up before the shell was ready.
- **Services' shells** (a ProductionBox's Shell tab) still end with their page.
- **In the page:**
  - Reconnected instead of Connected;
  - an End shell button;
  - Reconnect instead of New shell while the shell goes on.
- **Readline:** a kept shell keeps half-typed input too. An Escape left pending (a Meta prefix) applies to the
  next key after reopening, as in any terminal that stays open. The browser test cancels it with Ctrl+G.

**Tests:**
- **Kept and opened again:** its variables and output.
- **Taken over by another page.**
- **Left too long, and Relay stopping.**
- **A chat deleted, a task discarded.**
- **The browser test:** reopened, End shell, and Relay's stop ending the one left.
- **Tried for real:** a real WorkBox, after a page reload too.

## Adding a provider Relay doesn't know (Relay 2.12, 2026-10-10)

**Why:** the user asked for "an extensive manual on how the user adds an unknown provider to Relay. So also
designing a (new) compatibility layer."

**Their choices:**
- **What 2.12 holds:** the guide and tools for it: a check of a layer, and a built-in skill for an agent.
- **Where a layer lives:** in Relay's own code, on two conditions:
  - "Error handling must make sure that Relay still starts normally if the user does something wrong."
  - "Make sure the user never has to leave the folder ../providers/<PROVIDER>, and split existing files in
    ../providers* into subfolders."
- **The example:** a tested one in the repo.

**How:**
- **A folder per kind** (`providers/<name>/`): its package names its kinds (`KINDS`), and `relay.providers.discover`
  finds the folders. Nothing is listed by hand any more.
  - **The built-ins moved in:** `claude/`, `opencode/`, `acp/`, `chat/`, `command/`, `perplexity/`. Perplexity's
    `perplexity.cjs` came from `relay/container/`.
  - **The support layer** (what Repair copies) is the folder, through `Provider.support_files()`. `SUPPORT_FILES`
    now names only files elsewhere: the bridge backends, which opencode-manager's scripting uses too, so they
    stayed.
- **Tests in the folders:**
  - **The shared setup** moved to a root `conftest.py`: the data and cache backstop, the fakes, the opencode
    guard.
  - **pytest collects `src/relay/providers`,** importing test files by their path (`--import-mode=importlib`):
    a provider's `test_opencode.py` and `tests/opencode_manager/test_opencode.py` are two files, not one name.
- **A mistake never stops Relay:**
  - **At load,** each folder is imported on its own and its kinds validated (`_invalid`). The checks: the
    `KIND`, the attributes, the field types, `check` and `start` written, its `EXAMPLE` passing its own check.
  - **A folder that fails** goes to `providers.BROKEN` with the file and line, and the others load.
  - **At run time,** a layer that raises becomes that provider's trouble:
    - `parse_template` turns it into a `TemplateError`;
    - `providers.safely` turns it into a default, at the core's calls;
    - the models route gives a 502 that says why.
- **The check** (`providers/conformance.py`): the scenarios run the provider as a run does. `RunManager.place`
  was factored out of `_execute`: its WorkBox shell and its ProviderBox.
  - **Each turn's events** are validated (`validate_events`).
  - **Reachable from:** the API, Settings (a provider's shield), and `relay provider-check`.
  - **`relay provider-check`** asks the running Relay, signed in with a session of its own that it ends.
- **The example** (`providers/example/`):
  - **`toy_agent.py`:** a program with a JSON format of its own, sessions, a tool and a slow count;
  - **`provider.py`:** its layer, in its box;
  - **its README and tests,** including the whole check.
  - **`LISTED = False`** keeps it out of Settings → Add provider.
- **The guide** (`docs/ADDING-A-PROVIDER.md`):
  - **Its parts:** the decision tree, the architecture, the folder's rules, the layer step by step, testing, using
    it, what a mistake shows, and a reference.
  - **Kept true by a test** (`tests/relay/test_guide.py`): its code blocks are the example's word for word, and
    its reference names every hook, event and `Context` field.
  - **In Settings** too (`GET /api/providers/guide`).
- **The skill `relay-provider-writer`:** the guide's steps for an agent, which works in the folder only, and says
  rather than changes what Relay itself would need.

**Found by trying it for real** (a throwaway Relay with real boxes):
- **Claude Code's turns failed in the check:** without a run's MCP endpoint its layer falls back to
  `--dangerously-skip-permissions`, which Claude Code refuses as root in its box.
  - **The fix:** a check's turn gets an approvals-only token (`Delegation.check_token`,
    `RunManager.place_check`). Its permission requests go to `judge()` as a run's do, and it gets no other tool;
    the MCP route takes such a token.
- **A check's turns end as a run's do:** the provider's cleanup and `box.end()`. A stopped turn left its process
  running in the box.

**Tried for real:**
- **Pass every scenario:** Claude Code (haiku, in its box), opencode (a free Zen model) and the example.
- **Perplexity as a visitor** passes all but a command: as a visitor it doesn't use the tool block.
- **Copilot** wasn't tried: its requests are the user's.

**Simulated users** (two Sonnet 5.5 agents, each with a git worktree of the code and a throwaway Relay of its
own, on free Zen models):
- **A provider author** built a provider for a CLI of its own with only the guide. It made mistakes on purpose,
  and used the check and a repair.
  - **It works:** the layer took about 15 minutes, every mistake left Relay running and said where, and the repair
    went through.
  - **Found and fixed:**
    - the check didn't pair tool results with their calls (`validate_events` does now);
    - "Going on" gave the wrong reason after a failed turn;
    - `relay doctor --no-providers` didn't name broken folders;
    - `relay doctor` offered to remove another Relay's web search box: a box working in a folder of its image
      seemed "left behind". Pre-2.12; a commit of its own.
  - **Clearer in the guide:**
    - what a failed turn sets off (fallback, going on later), and how a layer names a limit or a model that can't
      be used;
    - a session the program lost;
    - the check's prompts, for a stand-in program;
    - its relative links taken out in Settings;
    - its card also linked from the Add provider heading, and an added kind marked with its folder.
- **A daily user** went through the 2.11 layout, Ctrl+K, the kept shells, scripts, AgentOS, the check, the
  phone and both themes. There were no console errors.
  - **Found and fixed:**
    - with the column open just above the phone width, the header pushed the page sideways (the header is a
      container now, and its actions keep their width);
    - the / menu covered the window (menus are at most 640 px);
    - the composer's Shell button didn't close the panel;
    - on a phone the bar's Shell left the drawer open;
    - an empty Shell panel spoke of code tasks only.
  - **Left:**
    - a kept shell's screen, replayed in a window of another width, wraps as it was printed;
    - a script's output below a tall editor;
    - the AgentOS overview's tiles cut short;
    - the review panel's Mark as reviewed after it's done;
    - a new chat's model back to the default.

**Tests:**
- **The folders:** loading, broken folders of every kind, a layer that raises everywhere, Relay starting with
  them.
- **The check:** the event checker, the scenarios against a fake, the API, the command line, a check turn's
  approvals.
- **The example's and the guide's.**
- **The walkthrough:** Check it fully, the guide card and the guide.

## What 2.12's simulated users left open (Relay 2.12.1, 2026-10-10)

**Why:** the user asked for the four points 2.12.0 left open ("Take up the four open points for 2.12.1").

- **A new chat's model:**
  - **What happened:** within one browser the choice was kept (`relay.draft`, localStorage), as I first checked.
    The simulated user's scripts each had a browser of their own, so each started at the default. That's what
    another device or a private window does too.
  - **The fix:** Relay keeps the provider, model and effort picked for a new chat (`PUT /api/new-chat`, the
    preference `new_chat`, in `/api/state` at the start). The page sends a change when the draft's choice changes
    (`saveDraft`), and only once it has read Relay's (`useNewChat`), so a stale browser doesn't overwrite it.
  - **Not changed:** conversations made by scripts, AgentOS and repairs don't touch it: only the page's new-chat
    pickers do.
- **The review panel:**
  - **The button pressed twice:** `act()` loaded the panel anew without waiting, and `busy()` let the old button
    go before the new one replaced it.
  - **The fix:** it waits for the load now, and Mark as reviewed closes the panel (its work is done).
- **A kept shell at another width:** its output is bytes for a terminal of the size it was written at.
  - **Opened again:** the server says that size (ready's `size`) and keeps it until the page has written the
    output (`replayed`). The page writes it at that size, fits its window and sends its own size.
  - **The result:** xterm wraps what's there anew, and the shell gets SIGWINCH and redraws.
  - **Tried:** in a real WorkBox, a long command typed at 390 px came back on one line at 1400 px.
  - **Older pages** still work: they send their size on `ready`, as before.
- **AgentOS's tiles:** two lines under the figure (`-webkit-line-clamp`, the whole text as its title), and the
  tiles of a row line up at the top (`align-content: start`).

**Tests:**
- **The new-chat choice:** the API, and in the walkthrough another browser (only signed in) that starts with it.
- **The review panel** closing in the outcomes UI test and the walkthrough.
- **The replay protocol** in the terminal tests: its size, `replayed`, the size kept until the page's.
- **In the shell's browser test,** the phone's size comes before the desktop's on reopening.

## The first public release (Relay 3.0.0, 2026-10-10)

**Why:** the user asked for an end sprint toward the first public release ("Start an 'end sprint' for Relay,
this will be the first public release … Decide for yourself what you do and how"). Their choices: the MIT license, version
3.0.0, and the whole history public.

**The repository:**
- **The license:** MIT, held by "the Relay contributors"; both pyprojects and the SDK wheel's METADATA say so.
- **AGENTS.md** is the public guide for anyone developing Relay. What was particular to this machine and its owner
  (the language, the server, which accounts and models tests may use) moved to
  `AGENTS.local.md`, which git ignores; `CLAUDE.local.md` loads it for Claude Code.
- **ruff:** a `[tool.ruff]` section (E9, F, B; 120 columns), so a contributor's ruff checks what the project means.
  What it found: zips of equal length now say `strict=True`, an unused run id and loop variables, a default
  argument made from a call, an empty hook in an abstract class.
- **The walkthrough** signs up "alex", not a real first name.
- **`httpx2`** in the dev tools: Starlette's test client warned that `httpx` is deprecated for it.

**The docs:**
- **The README was 2,400 lines of reference,** with what Relay is in its first lines only. It's a front page now:
  the pitch, screenshots, who it's for, requirements, install, first steps, keeping it running, and a docs index.
- **The screenshots are of real runs:** a throwaway Relay with opencode's free Big Pickle, which found and fixed a
  failing test (the code task, with its diff open), answered a chat (light and dark), and the phone.
  - **Its projects** were in `/srv/projects`, made for it and removed after: a path in `/tmp` looked like a test.
- **`docs/GUIDE.md`:** the reference, word for word, with a table of contents, and its links made relative.
- **`docs/ARCHITECTURE.md`:** the architecture section. It still spoke of `providers/*.py` (folders since 2.12),
  and named the modules that came since.
- **`SECURITY.md`:** the threat model.
  - **What it says:** one person on a trusted network; what protects what (sign-in, the CSRF header and CSP, the
    WorkBoxes and their network, the tools token, the guard, `/port/`, network drives, the data); what's left to
    the user; how to report privately.
  - **Checked against the code,** e.g. that `/mcp/<token>` is outside `/api/`, and that no CORS is granted.
- **Relay's own pointers** to "the README" (a failed image build, the SDK's page, Settings → Account) name the guide.
- **A test** (`test_docs.py`) follows every relative link and anchor in the Markdown files (GitHub's slugs). The
  devlog and release notes are history and stay as they were.

**The data folder:**
- **On this server** it was 775 and the database 644; the home folder (700) kept it private anyway.
- **The change:** Relay makes a new data folder 700, and `relay doctor` warns only when another user can really
  read it: the folder lets them, and so does every folder above it.
- **The boxes** read it as the user (SearXNG's settings are 600 already), so nothing in them changes.

**A newcomer's install, for real:**
- **The setup:** a fresh Linux user (`newcomer`, over SSH so it had a systemd session of its own), a bare clone as
  "the repository's URL", and a Sonnet subagent following the README as a newcomer would. It drove the UI in
  Chromium and was free Zen models only. Port 8420 was the live Relay's, so it used 8521.
- **Round one** got no further than Settings → Providers. What it found:
  - **opencode 1:** opencode.ai's installer gives 1.18.35, which has no `opencode service`; Test showed opencode's
    own help text. opencode 2 has an installer of its own (`opencode.ai/v2/install`, today 2.0.26).
    - **The fix:** the README names it, and `opencode.require_service` asks `opencode service --help` once per
      program (its path and mtime). It doesn't go by version: betas are `0.0.0-…`. It's checked in Test, doctor and
      `ensure_service`, with the install command in the message.
  - **The service's port:** opencode's default profile has a fixed port, held here by the live Relay's opencode;
    Relay waited 30 s and said "did not start".
    - **The fix:** after 5 s it says the port is taken by another program, with a free one to move it to.
  - **`--port` lived on the unit's command line only,** so `relay doctor` checked 8420 and `relay provider-check`
    asked the other Relay ("Not signed in").
    - **The fix:** `relay port N` keeps it in settings.json, and the install uses it.
  - **PATH:** opencode's installer edits `~/.bashrc`, which the shell it ran in hadn't read. The unit took that
    shell's PATH.
    - **The fix:** Relay adds the agents' usual folders to its PATH when it starts (`cli.find_agents`), after what's
      there. The root conftest puts PATH back after each test.
  - **The first chat:** a new chat started with Claude Code, not installed.
    - **The fix:** until the user picks, `new_chat` is the first enabled provider whose PROGRAMS are installed.
  - **The README** said the image is built at the first code task (it's at the first start), and not where a backup
    goes.
- **Round two,** on the fixes, went through to the end: Test, a chat, a code task that fixed two bugs, Review
  changes. Two more findings:
  - **opencode's default model of the day** was gone from Zen ("Model exo-free has been deprecated."), while
    opencode's catalogue still says active. Relay planned to try it again five hours on.
    - **The fix:** "deprecated", "retired", "no longer available" are a model that can't be used
      (`limits.UNUSABLE`), and the README suggests Big Pickle.
  - **The port message:** it now names a free port, and as whom to run the command.
- **Not changed:**
  - a chat's answer to "hi" that paraphrased Relay's note (15k tokens in): that's what the model read;
  - the provider list's order after adding one.
- **Cleaned up after:** the user, its service, podman storage and lingering; the bare clone; the sudoers file and
  key.

**Tests:**
- **The license** in the wheel.
- **opencode 1** named, with how to get 2.
- **A port another program holds,** said soon, with a free one.
- **The port** kept for everything.
- **The agents' folders** on PATH.
- **The private data folder,** and doctor's warning.
- **The first chat's provider.**
- **A deprecated model.**
- **The docs' links.**

## App passwords for scripts and the SDK (Relay 3.1, 2026-10-10)

**Why:** the user asked for them ("By the way, can you add app passwords to Relay? For the SDK/API"). A script
outside Relay signed in with the account's own password (`login()`), kept in its file or environment, and the only
way to shut one script out was to change that password for all of them.

**What they are:**
- **One per script or program,** made and revoked in Settings → Account → App passwords, named after what uses it.
- **Shown once,** when it's made. Relay keeps its SHA-256 (it's 32 random bytes, so no scrypt needed), as it does
  for sessions. It starts with `relay_`, so it says what it is in a script, a log or a secret scanner, and a session
  token is never mistaken for one.
- **Its last use** is written down at most once a minute, not at every call. The list shows it.

**How they're used (`app.SecurityMiddleware`):**
- **`Authorization: Bearer relay_…`,** or Basic with the username for tools that only know that
  (`curl -u me:relay_…`).
- **No CSRF header:** another site's page can't send an Authorization header to Relay without a CORS permission
  Relay never gives. When the header is there, the cookie doesn't count.
- **A wrong or revoked one** gets a 401 that points to Settings → Account.
- **Without login** (`auth: none`) the header is ignored, as everything else about signing in is.

**What one can't do:** manage app passwords (list, make, revoke: 403). A leaked one can't replace itself or shut
the user's other scripts out. Changing the account's password doesn't touch them: they're revoked one by one,
which is what they're for.

**The SDK:**
- **`connect(url, app_password=…)`** signs in with one. `connect(url)` without one is still for a Relay without
  login.
- **`$RELAY_APP_PASSWORD`** (with `$RELAY_URL`) signs a script in by itself, as `$RELAY_TOKEN` does on the Scripts
  page, so it needs no `login()` or `connect()`.
- **A revoked one** is said so: "doesn't know that app password (revoked? Settings → Account)".

**The docs:** the guide (a section of its own under Python SDK), the SDK's README and page (`/sdk/`), Settings →
Account's SDK section, SECURITY.md (signing in, and other sites), ARCHITECTURE.md.

**Tests:**
- **The API:** making, listing (never the password), Bearer and Basic, no CSRF header, another user's name with
  it, wrong ones and a session's token, what one can't do, revoking, a new account password, and without login.
- **The SDK:** `connect()` with one, its event stream, `$RELAY_APP_PASSWORD` alone, a revoked one.
- **The walkthrough:** one made and shown once, used without the browser's cookies, revoked.

## Conversations started outside Relay's pages: a yellow E (2026-10-10)

**Why:** the user asked for them to be marked as AgentOS's are ("make convos that were started externally (so from the
API/SDK, outside Relay's UI) yellow, with a small 'E' at the bottom left, just as AgentOS convos are green and have
'os'").

**Knowing who started one:**
- **Relay's pages say they're Relay's:** `api.js` sends `X-Relay-Client: web`. The SDK and every other client
  don't, and the UI and the SDK both make conversations with the same `POST /api/conversations`.
- **Why not the session or the User-Agent alone:** the SDK signs in with `/api/login` too, so its session is like
  a browser's. A header the page sends is plain, and the mark is only a mark: a client that sends it on purpose
  just isn't marked.
- **Who, in words** (`web.started_outside`, kept in `conversations.external`):
  - **"the script public/report.py":** a run on the Scripts page, known by its session (`Scripts.script_for`);
  - **"the Python SDK":** the SDK now says `User-Agent: relay-sdk/<version>`;
  - **"Relay's API":** any other client;
  - **"…, with the app password “backup script”":** when one was used.
- **Where it's set:** a new conversation, a fork (it's whoever made the fork: the user's fork of a script's chat
  is theirs), and a script an agent writes. What Relay starts itself (AgentOS, scheduled tasks, repairs,
  subagents) isn't through the API, and has no mark. Conversations made before have none.

**The mark:** `activity.chatIcon`, so every list has it (the Chats, Tasks and Governor views, the Tasks page).
- **What it looks like:** the icon in yellow (`--external`: `#a16207` light, `#facc15` dark) with an "E" at its
  bottom left, as AgentOS's green "os".
- **Its tooltip** says who started it.
- **AgentOS's mark comes first:** a conversation AgentOS manages shows "os" even when a script made it.

**Tests:**
- **The API:** Relay's pages, a program, the SDK's User-Agent, an app password, the list, forks both ways.
- **A script on the Scripts page** and **the SDK with an app password**, named.
- **The Chromium checks' fixtures** send Relay's page header: they stand in for the user's own chats.
- **The walkthrough:** a chat made through the API shows the E, saying "Relay's API".

## The Governor adds providers, and new kinds are built in a box (2026-10-10)

**Why:** asked to add Gemini, the Governor could only tell the user what to do. It couldn't add a provider, not
even one that needs no code, nor build a kind Relay doesn't know (relay-provider-writer needs Relay's source, which
isn't in its box). It proposed a `provider.create` that works as `provider.repair` does. The user: "The Governor can't
make new ProviderBoxes, and can't add providers."

**Found on the way:**
- **The generic `acp` kind isn't boxed.** It has no `box_key`, so its program (`gemini --experimental-acp`, as the
  Governor proposed) runs on the machine, as `command`'s does; only Copilot, a subclass, has a box. A program like
  that gets a kind of its own, with a box: what `provider.create` builds.
- **A repair's test ran the agent's code on the machine.** `shadow_test` imported the repairing agent's files in a
  copy of Relay, as the user. The Governor may start repairs, so that was a way out of its confinement.
- **`begin_repair` told the agent to "test with check.py",** gone since 2.9.1: it's `repair_check`.

**The user's choices:**
- the Governor adds only providers in a box, or chat APIs; API keys stay the user's;
- agent-written provider code is tested in a box, repairs' too;
- adding a new kind to Relay stays the user's;
- it can be started from Settings as well.

**A test box** (`Boxes.test`, `repair.shadow_test`):
- **What it is:** a one-off container, `--rm`, with the instance label. It has the copy of Relay (read-only),
  `vendor/`, a scratch folder, and of the user's templates the provider's own only (not the others' keys).
- **Two steps:**
  1. **What it would mount:** the provider says, in a box with nothing of its own. A program it names bare is
     found on this machine's PATH: the box hasn't it, and the layer then gave just the name, refused as relative.
  2. **The test,** with those mounts, after `boxes.refused_mount` checks them.
- **What may be mounted:**
  - a program read-only from anywhere;
  - a writable folder only in the home folder or Relay's data (opencode-manager's profiles live there, and a data
    folder may be elsewhere);
  - never `~/.ssh`, `~/.gnupg`, `~/.aws` and the like, Relay's database, settings, templates, repairs or code, or
    the workspace roots.
- **Its image** is the provider's own. Relay's box image's shells hand commands to Relay, which isn't there, so a
  real shell (`bash.real`) goes over `relay-shell`.
- **Claude Code as root there** (no approvals endpoint) skips its permissions only with `IS_SANDBOX=1`; its support
  layer sets that when it's root.
- **Tried for real in podman:** Claude Code (haiku), opencode (Big Pickle) and the example each pass in the box.

**A new kind, built by an agent** (a "creation": a repair whose provider isn't there yet, `repairs.creates`):
- **Its folder:**
  - the example copied to `relay/providers/<name>/`, its imports of itself made the new folder's (a copy loaded
    the example's class otherwise);
  - `reference/` with Relay's source and the guide;
  - `CREATE.md`.
- **Its agent's tools:**
  - `repair_run` runs in a test box with the program and the draft's mounts so far;
  - `repair_check` is its review.
- **Review:** the folder's files (all new), compiled, and Relay's whole check of it (`conformance`) in a test box,
  with its EXAMPLE as the template, which the box writes. It records the kind and what it mounts.
- **Apply and undo:** apply puts the folder in `src/relay/providers/<name>/` (refused if one appeared); undo takes
  its files out again.
- **Starting one:** Settings → A provider Relay doesn't know → Build it with an agent, or the API
  (`POST /api/provider-creations`).
- **Its conversation's panel:** its files, its box's mounts, each scenario, and Add it to Relay.

**The Governor's capabilities:**
- **`provider.list`:** the providers, and the kinds it may add, with options and an example.
- **`provider.add`:**
  - only a kind whose `Provider.boxed()` is true (a new hook: `box_key()` is set; a chat API's is true);
  - no secret: a kind that needs one is the user's to add;
  - no box mounting what a test box may not (Claude Code's `config_dir` could point anywhere);
  - marked `managed` in its template, "added by AgentOS" in Settings.
- **`provider.remove`:** only those it added.
- **`provider.check`:** Relay's whole check, as Settings' shield runs it.
- **`provider.create`:** a creation, managed. `provider.review` reviews one too, and asks the user to add it.
- **Its instructions:** when to add, when to create, and that logins and keys are the user's.

**Tests:**
- **The test box:** what it's given and its real shell; the two steps with the template alone; a refused mount
  stopping it; what may be mounted; no boxes, no test.
- **A creation, end to end:** names, the folder and brief, the check, apply and undo; through the API too.
- **The Governor:** what it may and may not add, removing only its own, the check, create through review to the
  user's notice and apply.
- **The suite's repair tests** run the test box on this machine (`host_boxes`).
- **The walkthrough:** "added by AgentOS", and the Build it with an agent dialog.

**Tried for real:**
- **The setup:** a throwaway Relay from a git worktree, so Apply wrote there. `toy-agent` (the example's program) was
  on PATH. The Governor ran on Claude Sonnet 5.5; its builder was opencode's free Big Pickle.
- **What happened,** asked "add a provider for toy-agent":
  - the Governor saw no kind runs it, and used `provider.create`;
  - the builder read the guide, ran the program in its test box, and wrote the layer, its README and tests. Its
    `repair_check` passed every scenario, in five minutes;
  - `provider.review` passed in the test box, and the user was asked;
  - once applied and Relay restarted, it ran `provider.add`, then `provider.check`: seven of seven, in real boxes.
- **Fixed from it:**
  - **The Governor wasn't woken when the builder was done** (nor a repair's agent): their turn wasn't followed as
    chat.create's is. It would have heard at its next round, hours on (`_follow_started`).
  - **Asked for a `command` provider,** it took the kind for missing (provider.list didn't name it). It added a
    toy-agent provider with `binary` set to the command line instead, and started a repair to make that work.
    That's boxed, so no harm, but not what's meant. provider.list now names the kinds that are the user's to add,
    and its instructions say to tell the user.
  - **A new kind's review called the example's files its agent removed "not taken over".** It lists only what's
    outside its folder now.
- **Cleaned up after:** the trial's containers, its folder and the worktree.

## Adding a provider with an agent: a box to install it in, a plan, a throwaway Relay (2026-10-11)

**Why:** the user asked the Governor for Gemini. Its agent built a layer, but the program had to be on the server:
the user was told to `npm install -g` it there over SSH and log in, and logging in with Google didn't work for
them. Claude Code also failed at the first step (its box was busy, so it wasn't made anew with the creation's
folder), and nothing showed the provider working before it went into Relay. The user drew the flow they wanted:
install and log in in a new ProviderBox, the agent's plan to approve, a throwaway Relay to try it in, then Add.

**A provider's own home** (`Template.home`, `Provider.box_home()`):
- **What it is:** a folder that's its box's HOME, mounted writable, with `.local/bin` first on PATH and npm's
  global installs there (`boxes.home_env`). It starts with a `.profile` that keeps that for a login shell.
- **What it gives:** what the user installs (npm, pip, a program's installer) and their login outlive the box.
  `home_program()` finds the program for `check()`.
- **The box image** got Node.js 22 with npm (NodeSource's, as the WorkBox has), curl and unzip: most agents' CLIs
  are Node programs. 228 MB → 429 MB.

**The page** (`/providers/new/<id>`, `relay.creations`; the user's three steps):
1. **A setup box** (`Boxes.setup`): the box image with real shells (the test box's here-shell) and the home, a
   terminal on the page (`production.Shell`'s way, kept as a chat's). Then what was installed, and who
   integrates it (the last choice preselected).
2. **The plan:** the agent works in Plan mode; `repair_run` runs in the setup box. A new tool, `provider_plan`
   (`relay repair plan NAME PROGRAM`), hands in name, program and plan. Approve seeds `relay/providers/<name>/`
   from the example; Redraft sends the user's words; Deny throws everything away.
3. **Finalisation:**
   - **Relay checks it:** when the building turn ends (a `run_ended` hook beside AgentOS's), Relay runs the whole
     check in a test box, with the home.
   - **A throwaway Relay with it:**
     - its code (a copy with the new folder), data and boxes in `/tmp/relay-trial-<id>` (short, for the
       sockets);
     - only the new provider; 127.0.0.1 on a free port;
     - signed in by Relay with `/api/setup`.
   - **Something's wrong** goes back to the agent, and the throwaway Relay comes back after its fix.
   - **Add** applies the folder, writes the template with the home and restarts Relay. **Take it out again** undoes
     it, home and all.

**Serving the throwaway Relay at `/trial/<id>/` of the real one:**
- **Relay under a path of its site** (`Settings.base_path`), also for proxies that serve Relay at a sub-path:
  - the server puts the path in its page (`relay-base`), its error page, links it makes and redirects;
  - the frontend asks for its paths through `base.js`, and keeps drafts and views per path. The theme is shared:
    a trial Relay in another theme looked like another app.
- **`forward.py` gives `/trial/<id>/` its own route:**
  - for the creation's user only;
  - with the trial's session cookie instead of the browser's, its Set-Cookies dropped and its sign-out a no-op;
  - its redirects to its own address put under the prefix;
  - WebSockets too.
- **A banner** in the throwaway Relay says what it is, with the way back.

**Also:**
- **An added provider with a home has a Shell in Settings,** to log in again. Its box goes with the shell.
- **The Governor's `provider.create` no longer has kinds built:** it sends the user to the page, a Needs-you item
  with a link (`agentos_interventions.link`). `provider.review` is for repairs.
- **Every ProviderBox mounts `<data>/repairs`:** the Claude Code failure.
- **`repair_run` in a WorkBox-image box** used Relay's `bash.real`, which isn't there.
- **The old dialog and panel are gone.** An old creation (this morning's Gemini) can only be discarded.
- **The agent's conversation** is named after its plan once it's handed in.

**Tests:**
- **The flow through the API:**
  - the plan by MCP (bad names refused);
  - a redraft and approve;
  - the failed check of the example's kind, and the check that passes after;
  - the throwaway Relay (a small server in its place) at `/trial/<id>/`: its cookie, its prefix, its redirect,
    logout kept;
  - the fix, Add with its template and home, Take it out, Cancel.
- **Smaller tests:**
  - the setup box's argv and `.profile`, a home in a box and in its test box;
  - `repair_run` in the setup box;
  - the shells (setup, home);
  - Relay under a path;
  - `base.js`.
- **The walkthrough** runs the whole page with a fake builder that calls `relay repair plan`:
  - a real setup box (`node --version` in it);
  - Relay's real check in a test box;
  - a real throwaway Relay, opened at `/trial/<id>/` with its banner and the new provider;
  - then Cancel.

**Tried for real** (a throwaway Relay from a worktree; the builder opencode's free Big Pickle; the Governor
Claude Sonnet 5.5):
- **Step 1:** in the page's shell, `npm i -g opencode-ai` landed in the home's `.local/bin` and ran. toy-agent was
  put there as a user would install it.
- **Step 2:** Big Pickle read CREATE.md and the guide and studied toy-agent in the setup box. Its plan was good:
  why no kind fits (and why not `command`), how it runs from the home, the events, sessions, stopping. After a
  redraft it handed in a shorter one.
- **Step 3:**
  - built and passed `repair_check`;
  - Relay's check in the test box: 7 of 7;
  - the throwaway Relay answered a chat;
  - a code task's command ran in its WorkBox;
  - Add applied it to the worktree; Relay restarted with the kind; the Shell in Settings opened in its home.
  - Deny on a second one left nothing.
- **The Governor,** asked for Gemini CLI: it said it can't add it itself, and left a Needs-you item with what it knew
  of Gemini (npm package, Google login, `-p`, ACP). Its Open landed on the page with that text.
- **Found and fixed:** a login shell (`sh -lc`, which the agent used) lost the home's PATH: the home's `.profile`.
- **Cleaned up after:** the throwaway's and the trial's containers, folders and the worktree.

## A repair's or new provider's conversation isn't "Needs review" (2026-10-11)

**What the user found:** their two new providers (codex and codex_other) were added, but both conversations stayed
under Tasks' Needs review, and the Tasks badge counted them.
- **Why:** each works in a git folder of its own (`repairs/<id>`), where its agent wrote the layer. Those changes
  never go: Relay takes them on its page (Add) or a repair's panel (Apply), not by a task's Merge or Commit.
- **Now:** such a conversation is never in Tasks' review group (`list_tasks`), and its payload has no `changes`, so
  its header doesn't call it Needs review either.
- **Tests:** the new-provider flow and the repair one; both failed without the change.

## A repository copied onto a network drive (2026-10-11)

**What the user found:** copying a folder (their vault) from their laptop to a network drive in Dolphin ended
with "Access was denied while attempting to create the requested folder".
- **What the drive's log showed:** the folder and its first subfolders were made, then Dolphin deleted it all.
  The refusal itself wasn't in the log.
- **Why:** the drive kept `.git` out at any depth, so the vault's own `.git` (a folder it may not make) was
  refused, and Dolphin gave up on the whole copy.
- **Now:**
  - only the shared folder's own `.git` (its repository, Relay's checkpoints) is kept out;
  - a repository inside it is a folder like any other. It's no new door: an agent can write a `.git` anywhere
    in its project, and Relay's own commits run without hooks;
  - the drive logs what it refuses (outside the folder, its `.git`, read-only), and makes its log's folder.

## Repairs on a page of their own, as adding a provider (2026-10-11)

**Why:** the user asked to test the repair. Tried for real, it worked, but the old way:
- a dialog (the model a free text field), then a panel where the user pressed Check the change;
- a lighter check: the provider's `check()` and one prompt;
- no throwaway Relay before Apply;
- a brief that took the kind's name for its program (`relay repair run -- toy-home --version`; the agent tripped
  on it) and didn't know providers' homes.
- **The user's choices:** a page of its own, with a plan step.

**One flow for both** (`relay.creations`). A repair is a row of `repairs` with `page = 1`. `_migrate` set it for the
new providers made since this morning; a row without it is legacy, and its page offers Discard.
1. **What's wrong** (`GET /api/providers/{id}/trouble`):
   - its check, its models' statuses, its latest errors;
   - for a provider with a home, a shell in a box with it (an update or a new login may be all it needs);
   - the user's words, and who repairs it (never the provider itself).
2. **The plan:** `provider_plan` takes the plan alone for a repair. Approve, Redraft, Deny.
3. **The repair:**
   - **Relay's check:** the agent works on copies of its support files. When its turn ends, Relay checks it fully
     (`shadow_test(full=True)`: conformance with the provider's own template). The agent's own `repair_check` stays
     the light one, since it runs over and over on a model someone pays for.
   - **The throwaway Relay:** the provider's template as it is (keys and home), with the repaired files.
   - **Apply:** the originals kept; Relay restarts. Undo brings them back.
   - **Cancel:** leaves the provider's home alone.
   - **One repair going per provider:** the 🔧 opens the one there is.

**The brief (`REPAIR.md`):**
- a plan first;
- the program by its kind's `PROGRAMS`, else "its program";
- a home's program and login are the user's to update, in the page's shell.
- **Its folder:** `Repairs.prepare` became `prepare_folder`, filled at Continue.

**The Governor:**
- **`provider.repair`** starts a repair at its plan, in a managed task it follows, and leaves a Needs-you item with
  its page (`link`, `repair_id`).
- **`provider.review` is gone:** Relay checks a repair itself.
- **Without AgentOS,** a failing provider's notice links to `/providers/repair/new?provider=<id>`.

**The page:**
- **`newprovider.js` is `providerpage.js`,** one page for both, its words per kind.
- **A missing `checked()` added:** a new provider's page called it for a failed check, and it wasn't there.
- **Gone:** the repair dialog and the conversation's old panel. A repair's conversation links to its page.
- **Routes:**
  - `/api/provider-repairs` (`/{id}`, `/{id}/{action}`, its terminal) shares the new providers' actions;
  - `POST /api/providers/{id}/repair` and `/api/repairs/{id}/{action}` are gone.

**Tests:**
- **A page repair through the API:**
  - a fixer that lingers for its plan by MCP and repairs once approved;
  - a redraft;
  - the automatic check, failing and then passing after a fix;
  - the throwaway Relay with the provider's template;
  - Apply, Undo;
  - one repair per provider;
  - Cancel leaving a home.
- **The Governor:** provider.repair onto the page; its Needs-you item; settled by Apply.
- **The walkthrough:** Fake Chat repaired on its page, through Relay's real check (its "A command" fails for a chat
  fake: Try it anyway) and a real throwaway Relay, then Cancel.

**Tried for real** (a throwaway Relay from a worktree; a provider in its own home whose toy-agent now says "speak"
where it said "say"; opencode's free Big Pickle repairing; Claude Sonnet 5.5 as the Governor):
- **Settings → 🔧:** the page showed its check; the shell in its home showed the new format.
- **Big Pickle's plan:** "speak" for "say" in `translate()`, both accepted, nothing else. It noticed the
  without-a-box start path looked wrong, and left it alone, asking.
- **Approved:**
  - its repair, and Relay's whole check: 7 of 7;
  - the throwaway Relay answered "OK";
  - Apply restarted Relay, and the check passed 7 of 7; Undo brought the broken layer back.
- **The Governor,** asked to have it repaired: `provider.repair` with Big Pickle. Its Needs-you item's Open landed on
  the page, at its plan.
- **Fixed from it:**
  - that item showed the old "Review and apply" beside Open;
  - it said the provider "keeps failing" when the user had asked for the repair.
- **Cleaned up after:** containers, folders and the worktree.
- **A slip of mine, mended at once:** `git add src/` staged the user's own provider folders (codex, codex_other)
  into a commit. It was amended out before anything else, and the commit helper now refuses a new provider folder.

## Relay 3.2.0 (2026-10-11)

**What's in it** (the entries above, since 3.1.0):
- **Providers, added and repaired on a page of their own:**
  - installed in a box with a home and logged in there;
  - an agent's plan to approve;
  - Relay's whole check in a test box, and a throwaway Relay at `/trial/<id>/` to try it;
  - Add or Apply.
- **The Governor's provider capabilities,** and its Needs-you items with a link.
- **The yellow E** on conversations started outside Relay's pages.
- **Relay under a path of its site** (`base_path`).
- **Node.js in the providers' box image.**
- **Fixes:** network drives and a repository copied onto one; Needs review; busy boxes and repair folders; the
  suite with the user's own providers.

**Before tagging:** the full suite, ruff, the frontend's tests and the walkthrough (which adds a provider and
repairs one on their pages, through real test boxes and real throwaway Relays). Each round was also tried for real on
a throwaway Relay, with free Zen models as the agents and Claude Sonnet 5.5 as the Governor.

## A new chat starts with No project folder (Relay 3.2.1, 2026-10-11)

**What the user found:** starting a new chat in the web UI and sending a message, it ran in the same folder as the
conversation before it.
- **Why:** the new-chat draft (kept in the browser) remembered "in a folder" once a code task was started there,
  and kept it across reloads. So every next chat was a code task in that folder.
- **Now:**
  - "in a folder" goes off once a chat is started, and when the page loads, as Temporary does;
  - the folder picked last is kept, and the folder picker opens there;
  - `/new` in a code task still starts in its folder.
- **The walkthrough's** second code task now picks its folder from No project folder, where the picker opens at the
  folder picked last.


## The usage ring on the activity bar (2026-10-11)

**What the user asked:** a usage-limit indicator in the bar, between Shell and Settings.
- **Their choices:**
  - a ring for the tightest window, the one with the least left across all providers: green, amber under 20% and
    red at 0, its tooltip naming the window;
  - a click opens a popover with every provider's windows, Refresh, and a link to Settings → Usage.
- **How:** `static/js/limits.js`.
  - It has the meters Settings → Usage drew (`limitRow`): both places draw limits the same way now.
  - It has `tightest()` and the ring (`usageIndicator`). A window that reset since it was told counts as full, as
    the meter has it.
  - The ring asks `GET /api/usage/limits`, so no server change. It asks at the start, every 5 minutes while the tab
    is in view, when the tab comes back after over a minute, and when a conversation's last run changes (a run that
    ended may have used a limit up). The server still keeps each provider's answer for 2 minutes.
  - It's hidden while no provider tells a window.
- **Tests:**
  - `tests/js/limits.test.cjs` covers `tightest()`;
  - in the walkthrough, Fake Limited's limit event now carries its `five_hour` window, as Claude Code's do. After
    its run the ring is red, and the popover and Settings → Usage show it with 0% left.

## Relay 3.3.0 (2026-10-11)

**What's in it:** the usage ring on the activity bar (the entry above).

**Before tagging:** the full suite, ruff, the frontend's tests and the walkthrough, which checks the ring, its popover
and Settings → Usage with Fake Limited's used-up window.

**What comes next, as the user proposed:** Relay's core is wrapped up after this. Its last new feature is a
**Plugin Engine** (4.0), and new features after that are plugins: folders in `RELAY_HOME/plugins`, with UI, API
routes, background tasks and tools for agents. The core keeps getting fixes and improvements.

## The Plugin Engine: Relay's last new feature (2026-10-11)

**What the user decided:** Relay's core is wrapped up. From now on it gets bug fixes and general improvements, and
every new feature comes as a plugin, through a Plugin Engine: the last new feature in Relay itself.
- **Their choices:**
  - what a plugin can do: UI (buttons and pages), the backend (API routes and background tasks) and tools for
    agents; not provider kinds, which stay provider folders;
  - how one is added: a folder dropped in place, with no agent flow in v1;
  - first 3.3.0 with the usage ring, then the engine.

**How it's built:**
- **A plugin is a folder** in `RELAY_HOME/plugins/<name>/`. That's outside the git tree, so `git pull` never touches
  it and it can't be committed by mistake.
  - **`plugin.json`:** label, description, version and `api` (the plugin API it's written for, 1).
  - **`__init__.py`:** `setup(plugin)`, the server's part.
  - **`static/index.js`:** `setup(relay)`, the page's part; plus `static/style.css`.
- **`relay/plugins.py`** loads them at start, as `providers.discover()` does.
  - A folder that fails is listed with why, with the file and line, and the rest load.
  - Names that begin Relay's own tools (`memory`, `computer`, `repair`, `agentos`, …) are refused, so a plugin's
    `<name>_<tool>` can never be one of Relay's.
  - A switch per plugin (`plugins_off` in the settings) works from the next start.
- **The contract** is `plugin`, the page's `relay`, `PluginError`/`Call`/`ToolCall` and the HTTP API:
  - **`route()`:** at `/api/plugins/<name>/…`, behind the same sign-in and CSRF rules as all of `/api/`; handlers
    run in threads; `{part}`s of the path come in `call.params`.
  - **`every()`:** background tasks in the scheduler's tick, each in a thread, never twice at once.
  - **`on_run_ended()`:** called from where runs end, beside AgentOS's and the creations' hooks.
  - **`tool()`:** MCP tools for top-level conversations, offered in `Delegation._tools_of` and dispatched in
    `handle`; for agents without MCP, `relay plugin TOOL KEY=VALUE`, `relay help plugins`, and a line in their note.
  - **`sdk`:** a relay_sdk session with an app password of the plugin's own (kept in its data folder, made anew when
    the user revokes it).
- **On the page,** `static/js/plugins.js` imports each loaded plugin's `index.js` after the layout is built. A
  plugin's part that throws is logged and left out.
  - Its `relay` has `barItem` (above the Shell, with a badge), `page` (`/p/<name>/…`, drawn where a chat would be),
    `settingsTab`, `api` (its own routes and Relay's), `ui` (Relay's pieces) and `navigate`.
  - The CSP needs no change: plugins' files are served from Relay's own origin (`/plugins/<name>/static/`).
- **Settings → Plugins** lists them: loaded, off, or why not, with what failed last, and Restart Relay.
  `relay doctor` and the SDK (`plugins()`) say the same.

**Shipped:**
- **The example plugin,** Notes (`plugins/notes/`). It uses every extension point: a bar button with a count, a
  page, a Settings tab, routes, `notes_add`, a note for each failed run, and an hourly tidy-up.
- **`docs/WRITING-A-PLUGIN.md`,** built on that example. `tests/relay/test_plugins.py` checks its quotes word for
  word and that its tables name the whole contract.
- **The `relay-plugin-writer` skill.**
  - It carries copies of the guide and the example (`reference/`), because agents in a WorkBox read Relay's skills
    but not its `docs/` or `plugins/`. Mounting those would have made every WorkBox anew.
  - A test keeps the copies the same.
- **AGENTS.md** has the freeze as a rule; **SECURITY.md** says a plugin runs with Relay's rights.

**Tests:**
- `test_plugins.py`:
  - loading, and each way a plugin can be broken;
  - routes (sign-in, CSRF, params, errors, 405/404, static files and their traversal);
  - tasks and the hook, apart and never twice at once;
  - tools through MCP (listed, called, refused, crashed, help; none for subagents; the Governor's);
  - `relay plugin`'s three forms;
  - the plugin's app password;
  - the example as the guide has it.
- The Settings browser check browses the Plugins tab at every width.
- The walkthrough links Notes into its throwaway Relay. It checks the button and its badge, the page (with the
  note the hook left for Fake Limited's failed run), adding and removing a note, a reload, Settings → Plugins and
  Notes' own tab.

**Tried for real** on a throwaway Relay running this code, with opencode's Big Pickle (a free Zen model):
- **Notes linked in:** asked to add a note, the agent ran `relay help plugins`, then
  `relay plugin notes_add text="…"` from its WorkBox. The note was there, with its chat.
- **A plugin from the skill alone:** in a code task, Big Pickle wrote `dice` (a route, a `dice_roll` tool, a bar
  button and a page with a Roll button).
  - It read the guide and the example from the skill's `reference/`.
  - Linked in and Relay restarted, the plugin loaded at once. Its route answered, and its page rolled in a browser
    without a console message.
- **Fixed from it:** Relay's icon names were only in `ui.js`, which a WorkBox doesn't have. The guide now lists them,
  and a test keeps that list the same as `ui.js`'s.
- **Seen, and fixed apart:** Big Pickle once titled a chat with its own tool-call markup (`<｜｜DSML｜｜ calls>`).

## A model's markup is no chat title (2026-10-11)

**Seen in the plugin trial:** Big Pickle titled a chat `<｜｜DSML｜｜ calls>`, its own tool-call markup. Relay already
refused a title that was one tag (`<tool_call>`), but this one has spaces and full-width bars in it.
- **Now:** special tokens (`<|…|>`, `<｜…｜>`) are taken out wherever they are, and a title that's one `<…>` as a whole
  is refused. The chat keeps the title it had (from its first words).

## The plugin contract under real use, before 4.0 (2026-10-11)

**Why:** with 4.0 the plugin contract becomes binding, so it was put under real use first.
- **Two simulated plugin authors** (Claude Sonnet 5.5 subagents), each with only the guide, the skill and the
  example, and a throwaway Relay from a worktree of their own:
  - "Daily digest": a morning task that has Big Pickle summarise yesterday's chats through `plugin.sdk`, a page,
    a notification;
  - "Bookmarks": a tool for agents, the hook, a bar badge, a Settings tab with options of its own.
  - Both plugins worked in the end. Their reports said what they had to work around.
- **A review of the engine's code** (Sonnet 5.5), most of its findings verified by running them.

**What they found, and what changed:**
- **A plugin's trouble wasn't always its own.**
  - **The shared thread pool:** its routes and tools ran in asyncio's default pool, Relay's too. Eight stuck calls
    slowed a sign-in to 5 seconds.
  - **Starting Relay:** `sys.exit()` or a hanging import at load stopped Relay's start.
  - **Now:**
    - each plugin has four threads of its own for its routes, tools, tasks and hook;
    - a route has 2 minutes (then a 504), a tool 10;
    - its import and `setup()` run in a thread with 20 seconds, and `BaseException` is caught;
    - `./relay plugins off <name>` turns one off when Relay doesn't start;
    - turned off in Settings, a plugin stops at once.
- **Tool names:**
  - two plugins could make the same tool name (`a`+`b_c`, `a_b`+`c`);
  - a folder `agentos_x` had its tools taken for AgentOS's;
  - names could be too long for the models.
  - **Now:** a plugin's name can't begin with a reserved word, a clash leaves the second plugin out, a tool's full
    name is at most 48 characters, and its schema's properties must be objects.
- **Mistakes said as the plugin's:**
  - `async def` handlers did nothing, or sent a coroutine to the agent; now they're refused at `setup()`;
  - a route that answers what isn't JSON is a 500 that names it;
  - a `PluginError` with status 401 signed the page out, and is now sent as 502;
  - a route or method registered twice is refused.
- **`plugin.sdk`:**
  - `import relay_sdk` at the top of a plugin failed: the guide's own example broke loading. The SDK is on the path
    now before plugins load.
  - Its app password was made once per thread at first use (8 in a race), and stayed dead after the user revoked
    it. It's made once now, renewed when revoked, and kept apart from the plugin's data.
- **Data:** the example's non-atomic JSON file returned 500s under concurrent writes (87 of 400). The new
  `plugin.store` writes each change whole (`os.replace`), one at a time, with `update()` for read-change-write.
  Notes uses it.
- **Tools:**
  - an agent's `text=2024` arrived as a number. Arguments are checked against the schema now: what's required, and
    simple types made so.
  - `ToolCall.run_id` is new.
  - `ToolCall.within(path)` keeps an agent's path in its folder.
- **The hook:** a plugin starting its own chats heard its own runs. `run` now has `started_by`, `temporary` and
  `error`.
- **Notifications:** `notify(url="/c/…")`, as the guide showed, was refused. A path of Relay's own is now made a
  link with Relay's address, or sent without one.
- **The page part:**
  - plugins load side by side, each with 10 seconds;
  - a page waits only for its own plugin;
  - `{part}`s are decoded;
  - `relay.ui.markdown` is new;
  - sign-out closes a plugin's page.
- **Static files** now need sign-in (nobody else learns which plugins there are), and a path with NUL is a 404,
  not a 500.
- **Folders in a workspace root,** where agents could change a plugin's code, are warned about in Settings →
  Plugins.
- **`relay doctor` and `relay plugins`** no longer run plugin code. `relay plugins check <folder>` does, on
  purpose, to say what a plugin adds.
- **The guide** says what was missing:
  - threads and time limits;
  - the store;
  - CSS variables, classes and the CSP;
  - `relay.ui`'s signatures;
  - the hook's fields;
  - a daily task;
  - the SDK calls a plugin uses most;
  - testing outside Relay;
  - what a plugin can't do (no unauthenticated webhook routes, no push to the page).

**Kept out of API 1,** without enough evidence for them:
- a declared options schema that Relay renders (`plugin.store` and a Settings tab do it);
- push events to the page;
- a webhook route without sign-in;
- a time-of-day schedule (an hourly task does it, as the guide shows).

## The SDK's conversation listing, in the guide (2026-10-11)

**Seen by a simulated plugin author:** the guide's SDK reference didn't say how to list conversations. They found
`convo.list` through `dir()`, and took `chat.messages()` for the whole conversation, though it has only the answers.
- **Now:** the reference has `convo.list(query)` with its fields, and says that `chat.get()["messages"]` has both
  sides.

## Relay 4.0.0 (2026-10-11)

**What's in it** (the entries above, since 3.3.0):
- the Plugin Engine, and its round under real use (two simulated plugin authors and a review);
- a model's markup kept out of chat titles;
- the SDK's conversation listing in the guide.

**From here on, Relay's core is complete:** fixes and improvements, and new features as plugins (AGENTS.md).

**Before tagging:**
- the full suite, ruff and the frontend's tests;
- the walkthrough, which passed for e4e9df1 (since then only docs and version strings changed);
- the authors' two plugins, written for the engine before its rework, loaded and worked unchanged on it. Plugin API
  1 stayed compatible.

## A simulated daily user of 4.0 (2026-10-11)

**How:** a Sonnet 5.5 subagent used a throwaway Relay 4.0 with Notes for an hour, as someone would on a normal day
(about 16 runs on Big Pickle). It covered chats, code tasks with and without git, scripts, Notes, every Settings tab,
the phone's layout and the dark theme. It found no crash, no data lost, no console error and nothing in Relay's log.
What it did find:
- **Enter in a dialog opened it again** (Rename chat, Rename loop): the dialog closed, the focus went back to the
  button that opened it, and the same Enter's keypress pressed it. The dialog uses up its Enter now.
- **Signing out in Settings → Account** left Settings open over the sign-in page. Signed out, every dialog and menu
  closes.
- **Settings → Plugins' switch** didn't change its row ("loaded" after off, "off" after on), and asked for a restart
  that turning off doesn't need. The API's `restart` is true only for a plugin Relay didn't load at its start; the
  row shows the plugin as it is now, and the note asks for a restart, or for a reload of the page (its buttons and
  pages).
- **A title lost its closing quote** (Adding note "Buy milk): quotes went at either end, one by one. Only quotes
  around the whole title go now, or one that pairs with none.
- **An address of no chat** (`/c/<not an id>`) showed a new chat under it without a word. Now it says "Conversation
  not found", as a deleted chat's link already did, and the address becomes `/`.
- **An answer without its "free" chip:** opencode asks its server who pays during the run; asked in a moment's
  hiccup (the first run in a new git folder), it said nothing. Asked again afterwards, it said "free". Relay fills a
  usage event's `billing` in when the provider didn't: the model's last runs, or the provider's `billing()`, once a
  run.
- **Small things:** a skill's command cut at "e.g."; a Markdown file's name without the title's dots and slashes;
  "so does git's history" in a folder without git; the Review panel's subtitle broken mid-word on a phone; the guide
  saying editing a message always asks first.
- **Notes cut a long note** at 2000 characters without a word: the example refuses one now and says why.

**Left as it is:** the phone's composer keeps its chips (model, folder, Shell) in a row that scrolls sideways, with a
fade at its end: a choice for the narrow screen, not a fault.

**Not covered by it:** production services, AgentOS and the Governor with real models, computers, VPN, web search,
subagents, the `/port/<n>/` proxy, the usage ring (its provider tells no limits) and paid providers.

## Relay 4.0.1 (2026-10-11)

**What's in it:** the fixes of the round above, found by a simulated daily user of 4.0. Nothing new.

**Before tagging:** the full suite, ruff, the frontend's tests, and the walkthrough (for 16c76ac, with the
plugins' switch and an address of no chat as new steps; since then only docs and version strings changed).

## A long queued message on a phone (2026-10-11)

**The user's report:** on a phone, a long message waiting in the queue made Relay scroll sideways, and it was
glitchy. Reproduced at 390 px: the queue's item was 1758 px wide. The queue is a grid, and its one column took the
whole message's width on one line (a grid item is at least as wide as its content, cut short or not). The phone's
browser then zoomed the page out to fit it, which was the glitching.
- **Now:** the queue's column is `minmax(0, 1fr)`: never wider than the page, and the message is cut short in it.
- **The walkthrough** queues a long message (with a path without spaces) and checks the page's width on a phone.

## A model's Test runs where a run does (2026-10-11)

**Found by a repair's agent:** one of the user's Codex providers (a kind they added) had stored "401 Unauthorized: Missing
bearer or basic authentication" for its default model, and the user asked for a repair. The agent found every run of
Codex's own in its box's home, all fine, but not the one with the 401: that one had run with another HOME. The
cause was in Relay. A model's **Test** (`POST /api/providers/<id>/test`, `health.test`) started the provider
without placing it (`RunManager.place`). So the program ran outside its ProviderBox, with Relay's own HOME (Codex
found no login there), and an agent's commands would have run on the machine, not in a WorkBox. The full check
(`conformance.check`) did place its turns.
- **Now:** `health.test` takes `place` and `discard` as `conformance.check` does. The route passes
  `RunManager.place_check` and `Containers.discard`. The test ends as a run does: the provider's cleanup, its box's
  `end()`, the check token, then the scratch folder's WorkBox.
- **The repair's own plan** (a `CODEX_HOME` for Codex outside its box) isn't needed after this: its folder is the
  user's, and they decide on it.
- **The cost:** a provider with tools gets a WorkBox for each test's scratch folder, gone after, as the full check
  has. That's a second or two per model in *Test all*.

## Relay 4.0.2 (2026-10-11)

**What's in it:** the two entries above (a long queued message on a phone; a model's Test placed as a run is).

**Before tagging:**
- the full suite, ruff and the frontend's tests;
- the walkthrough for 395db62 (the Test's fix after it is backend only);
- a real Test on a throwaway Relay: Big Pickle answered in 4 s from its ProviderBox, and the test's scratch WorkBox
  was there during it and gone after.

## An installer bundle, for other machines (2026-10-11)

**The user asked** for an installer bundle, for their other servers and devices. Their repository has no remote,
so `git clone` isn't a way there. Distribution tooling, not a feature of Relay itself, so it fits the freeze.
- **Their choices:**
  - a `.tar.gz` with an `install.sh` in it;
  - only the release's code, no git history: a newer bundle upgrades it;
  - none of their own provider kinds: what git has;
  - missing system packages installed by `install.sh`.
- **`scripts/make-bundle.sh [REF]`** (default: the newest tag) writes `dist/relay-<version>.tar.gz`, about 5 MB:
  - `git archive` of REF;
  - `vendor/` (this clone's when it's for the same `requirements.lock`, else `scripts/vendor.sh`; pure Python, so
    one bundle serves x86-64 and arm64);
  - `install.sh`: this clone's `scripts/install-bundle.sh`, so releases from before it bundle too (v4.0.2);
  - `.relay-bundle`, the list of its files.
- **`install.sh`:**
  - **Packages:** podman, git and python3 with apt or dnf when missing (with sudo).
  - **The code:** copied to `~/relay` (`--dir`). An upgrade first removes what the old list has and the new one
    hasn't, so files no bundle put there (a provider kind added with Add) stay.
  - **Then** `mkdir ~/projects` and `scripts/install-relay.sh` (`--user` by default, `--system`, `--port`), or
    nothing more with `--no-service`.
  - **It refuses:** a git clone, a folder with files of its own, root (for a service), and a folder other than the
    one the user's (or machine's) `relay.service` runs. systemd's `cat` says which.
- **That last refusal came from a mistake while trying it.** A run without `--no-service`, with `HOME` set to a
  scratch folder, still reached this machine's user manager. `install-relay.sh` restarted the live Relay
  (23:05:38, with no run going and no shell open; its unit, port and settings stayed as they were). A bundle on a
  machine whose service runs another folder would have taken that service over the same way. The tests now always
  pass `--no-service` and put a `systemctl` that refuses first on the PATH.
- **Tried for real:**
  - **A bare `debian:trixie-slim`:** `install.sh --no-service` installed podman 5.4, git 2.47 and Python 3.13 with
    apt, then Relay 4.0.2 ran.
  - **A new user on this machine** (`useradd`, over ssh to localhost, for a systemd user session): install with
    `--port 8522` (doctor, the user service, lingering) answered at 8522; a second run upgraded it.
  - **Removed afterwards:** the service (`--uninstall`), its podman storage (`system reset`), the user, and two
    buildah folders its first image build left in `/var/tmp`.

## Two plugins: Usage limits and Server (2026-10-12)

**The user asked** which plugins I'd suggest. Of four (backups, limits over time, the server's status, a daily
digest), they chose two, kept in the repository's `plugins/` next to Notes. Both are plain plugin API 1; Relay's core
is unchanged.
- **Usage limits** (`plugins/usage_limits`):
  - **What it keeps:** every 15 minutes, and when its page opens on a look older than 2 minutes,
    `relay_sdk.limits()` (the answer behind Settings → Usage and the ring),
    per window (`<provider_id>|<label>`) a point when there's news, for 14 days, in `plugin.store`. A last run's
    word told again adds nothing.
  - **A window that reset since it was told** gets its reset as a point, as the ring counts it.
  - **Its page:** per login a card, and per window a line of % left over 1, 7 or 14 days, with the low mark, a
    crosshair and tooltip (arrow keys too), and a table view.
  - **The rest:** the bar badge counts the windows that are low; a notification when one runs low and when it's
    back; a Settings tab for when it's low (10%).
- **Server** (`plugins/server`):
  - **What it reads:** `/proc` (CPU since the last look, memory, swap, uptime), the disks of `/`, home, Relay's
    data and podman's storage (one per filesystem), and `podman ps`/`stats`.
  - **Containers in Relay's words:** a ProviderBox, a service (by its name), the WorkBox of a chat (by its title,
    linked) or a folder. Their CPU comes from podman's counted seconds, as a share of the whole machine.
  - **24 hours of history,** in memory.
  - **Warnings,** with hysteresis, said once each: a disk 90% full, memory under 10% available, the CPU over 90%
    for 10 minutes.
  - **A tool,** `server_status`, for agents and the Governor.
- **The charts follow the dataviz checks:**
  - one series each, so no legend;
  - 2px lines in the accent (Server's sparklines in a muted colour, their latest point in the accent), a 10% wash
    under the line;
  - hairline grid, text in text colours;
  - a crosshair and tooltip that also answer the arrow keys;
  - a table view;
  - light, dark and a phone looked at, from a throwaway with a week of history seeded.
- **Found on the way:**
  - In a test, a plugin's SDK session calls Relay at `local_url`, which is the default port, so this machine's own
    Relay. One unpatched `notify` reached it (refused: an app password it didn't know). The new tests' file now
    refuses any real SDK request.
  - Five WorkBoxes of mine, with their volumes, were left over from today's throwaways (the queue reproduction
    and the plugin fuzz) and are removed. Earlier I had taken their volumes for the live Relay's.

## The ACP forwarder: Relay's agents as one ACP agent (2026-10-12)

**Why:** the user wants OpenHands in Relay, using Relay's own agents and models. OpenHands' CLI is no longer
maintained; OpenHands is now Agent Canvas on its Agent Server, and it takes either an LLM or an **ACP agent**
(`ACPAgent(acp_command=…)`). Relay's agents are agents, not raw LLMs, so the user chose a step back first: Relay
itself as an ACP agent, for any ACP client (OpenHands, Zed, JetBrains, Toad). A plugin, `plugins/acp`; Relay's core is
unchanged.
- **The forwarder,** `static/relay-acp.py`:
  - **What it is:** one file (stdlib and Relay's SDK), served from the plugin's static folder so another machine
    downloads it with an app password.
  - **Its two sides:** ACP over stdin and stdout; Relay's API with `RELAY_URL` and `RELAY_APP_PASSWORD`.
  - **Built against ACP schema v1.25.0** (2026-10-09). It reads JSON-RPC a line at a time, each request in a
    thread, so `session/cancel` and the client's answers come in during a turn.
- **What it maps:**
  - **Sessions:** a session is a conversation: a code task when the client's folder is in a workspace root, else
    a chat. `session/load` replays its messages, then goes on with it; `resume` and `close` are there too.
  - **Choices:**
    - the agent is a `configOptions` select (category `model`), grouped by provider, every enabled provider's
      models ("Claude Code · sonnet"); with `--provider`, that one's;
    - Relay's modes are a select and `modes` too (not the Governor's).
  - **A turn's events:**
    - text and thinking: chunks;
    - tool calls and their results: `tool_call` and `tool_call_update`, with ACP's tool kinds and a path's location;
    - a title: `session_info_update`.
  - **Questions:** a permission Relay leaves to the user goes to the client (`session/request_permission`, Relay's
    own options passed on); a form is said in the text, to answer in Relay.
  - **How a turn ends:** `end_turn` or `cancelled`; a failure is a JSON-RPC error with Relay's message.
- **Its tab in Settings (ACP)** makes an app password, and has the setups to copy: this machine, another machine,
  Zed, OpenHands' SDK, and other clients.
- **Tested:**
  - **By hand:** a client on the forwarder's pipes, against a Relay over HTTP on a port of its own.
  - **By Relay's own ACP client** (`relay.providers.acp`) with the forwarder as its agent: a turn, and Relay's whole
    check passes its turn, going on and model scenarios.
  - **Two independent ends of the protocol agree.**
- **Tried for real with OpenHands' SDK** (`openhands-sdk` 1.54.0):
  - **The client:** `ACPAgent(acp_command=["python3", ".../relay-acp.py", "--provider", "opencode", "--model",
    "opencode/big-pickle"])`, on a throwaway Relay, with a task in a project folder.
  - **What came back:** in 7 s, the file was written by Big Pickle in Relay's WorkBox; OpenHands saw its tool call
    (`write: …/hello.txt`, kind edit) and the answer.
  - **In Relay:** a code task in that folder, the file in its Review, started by "the Python SDK, with the app
    password “OpenHands trial”".
  - **Left as it is:** OpenHands waits 2 s for a `usage_update` after each turn. ACP's needs the context window's
    size, which Relay doesn't know of a model, so the forwarder sends none.

## The OpenHands plugin: Agent Canvas with Relay's agents (2026-10-12)

**The user asked** for OpenHands in Relay, using Relay's own agents. On the ACP forwarder: OpenHands' all-in-one
Agent Canvas image in a container of Relay's, its agent Relay through the forwarder. The user's choices: its own port
on the network with OpenHands' API key, Relay's workspace roots as its projects, on and off on its page
(remembered), telemetry off.

**A spike first,** on a throwaway Relay, settled what the docs left open:
- **The image:** `ghcr.io/openhands/agent-canvas:1.26.0` (2026-10-08) is 4.4 GB, with Python 3.13, running as uid
  10001. Under rootless podman, `--userns=keep-id:uid=10001,gid=10001` makes what it writes the user's own.
- **The network:** podman's default (pasta), publishing only the UI's port, keeps its inner services (Agent Server
  18000, automations 18001, its editor 8001, which binds 0.0.0.0) inside. Relay is `host.containers.internal`.
- **Its Agent Server's API** (`X-Session-API-Key`):
  - `PATCH /api/settings` sets the agent (`agent_kind: acp`, `acp_server: custom`, `acp_command`, `acp_model`);
  - `PUT /api/settings/secrets` puts `RELAY_URL` and `RELAY_APP_PASSWORD` in the forwarder's environment;
  - `POST /api/workspaces` lists the projects (Canvas starts with none);
  - `misc_settings.app_preferences.user_consents_to_analytics: false` spares the browser the telemetry question.
- **The browser** asks once for the backend's API key, then shows a wizard; "Skip for now" keeps Relay as the agent
  (choosing one there replaces it).
- **A conversation in `/projects/site`** gives the ACP agent that cwd. OpenHands runs `git init` in a project that
  isn't a repository. On a session's first prompt it adds a 27 KB block for its own agent (its skills, its secrets),
  which made Big Pickle list the skills: the forwarder's `--skip-context` leaves it out (with `--map`, a8a5792).
- **Canvas' conversations** carry the forwarder's agents as their models (`available_models`), so its model picker
  can switch between Relay's agents.

**The plugin** (`plugins/openhands`):
- **Setup:** Set up makes an app password "OpenHands" (the page does, through `relay.api.relay`: an app password
  can't make another), pulls the pinned image, starts it, and configures OpenHands as above, every project folder a
  workspace (new ones every five minutes).
- **Start and Stop** are remembered; a task starts it again after Relay's start, but not more than three times in
  ten minutes.
- **Also on its page:** the agent (any of Relay's, or Relay's default), the port, Update (the newest image, pinned),
  its log, and Remove, with or without its data.
- **What it sees:** only its own container, by its label, is ever touched. Relay's workspace roots are mounted, with
  the private scripts masked; the forwarder and the SDK are read-only.

**Tried for real** on a throwaway:
- **Set up on the plugin's page:** the image, the container running, OpenHands configured.
- **In Canvas:** the API key, no telemetry question, the projects listed; a task in `site` on Relay's Big Pickle.
- **What came back:** the file written, the conversation Relay's code task with it in Review, its prompt 111
  characters (no skills block).
- **Afterwards:** the container, the throwaway and its WorkBoxes removed; the image kept for the user.
- **A slip of mine on the way:** a `kill` by a pattern that was also in my own command ended that command (only the
  throwaway's port, so nothing of the live Relay's). The command after it found the pid first.
- **The user's Set up on the live Relay failed:** "crun: write: No space left on device". Reproduced, then fixed:
  - **The cause:** a `--tmpfs` copies what's below it in first (podman's `tmpcopyup`). The 1k tmpfs over the user's
    private scripts got their eight files and was full: the trial's folder had been empty. Now `notmpcopyup`,
    which also keeps exactly those files out, as the mask means to. Nothing was copied: the start failed first.
  - **Found looking at it:** under the service, `podman run` ran in relay.service's cgroup, and so would the
    network's pasta, which a restart of Relay ends (OpenHands running on without its port). It runs in a scope of its
    own now, as Relay starts its boxes (`systemd-run --user --scope`).

## OpenHands inside Relay, and Relay inside OpenHands (2026-10-12)

**The user asked** how the OpenHands plugin could be better. They chose:
- OpenHands *in* Relay, behind Relay's sign-in and address, with no key of its own;
- a smoother first start;
- their own idea: a Relay app in OpenHands, there by itself, so the two meet from both sides.

**A spike** (a throwaway Relay, a Canvas container of its own) settled how:
- **Framing works:** Relay's CSP (`default-src 'self'`) allows a frame of its own origin, and Canvas sends nothing
  against framing.
- **But not under `/port/<n>/`:** Canvas' router has `/canvas` built in as its base. Under `/port/<n>/canvas` its
  assets load (the Referer rule), but its routes match nothing, and the page stays blank. `AGENT_CANVAS_BASE_PATH`
  only moves the assets, and it made the proxy keep the port's prefix, which broke `/api`.
- **So Relay got the smallest extension point for it** (the user's pick, abcffe0): `plugin.forward(path, port)`.
  Relay's `/<path>/…` goes to a web app of the plugin's on this machine, signed in, as `/port/<n>/` does. A path
  of Relay's own, or one another plugin forwards, is refused. Canvas' UI is at `/canvas`; its API stays at
  `/port/<port>/` (its root sends the browser to `/canvas/`, so the proxy doesn't keep the prefix). One port does
  both.
- **Canvas' browser state** is in localStorage, which it shares with Relay as they share an origin:
  - `openhands-backends`: its backends, each a host and an API key;
  - `openhands-active-backend`;
  - `openhands-onboarded` and `openhands-telemetry-consent`: the wizard's two questions;
  - `openhands-color-theme`.
  Written before the frame loads, Canvas opens on its home, asking nothing.
- **The key in its page** (`AGENT_CANVAS_ALLOW_LAN_SESSION_KEY=true`) is meant for a port published on loopback only,
  which it now is.
- **Canvas Apps** (beta, host API 1): a folder with `canvas-extension.json` and one ESM file. The Agent Server
  installs one from a path (`POST /api/canvas-extensions/install`), off until `PATCH …/installed/<name>`. The app
  runs in Canvas' page: there that's Relay's origin, so it can use Relay's API with the user's cookie.
- **The link back:** OpenHands keeps each conversation's ACP session as `agent_state.acp_session_id`, which is
  Relay's conversation id.

**The forwarder's models, flat** (33ad8fe): OpenHands reads an ACP agent's models only from a flat `configOptions`
list, and the forwarder grouped them by provider. Flat, OpenHands now records all of Relay's agents for a
conversation (`acp_available_models`), and the model a conversation starts with is set through it.
- **Correcting the last entry:** Canvas' model picker still doesn't offer them. OpenHands offers live switching only
  for ACP agents it knows by name (Claude Code, Codex, Gemini, …: `supports_runtime_model_switch`), never for a
  custom one. The plugin's own agent setting stays the way to choose.
- **Also:** a tool call's title now shows its path as the client sees it (`/projects/site/…`, not the host's), as
  its `locations` already did.

**The plugin** (`plugins/openhands`):
- **Its container:** its port on `127.0.0.1` only, its key in its page, Relay's app mounted read-only.
  `plugin.forward("canvas", <its port>)`.
- **Its page while it runs:** Canvas fills it, in a frame at `/canvas/`, under a slim bar (state, Open in a tab,
  Settings, Stop). Settings holds the agent, the port, Update and its log. Before the frame loads, the page writes
  Canvas' storage:
  - this OpenHands as backend "Relay" at `/port/<port>`, with its key (other backends stay);
  - the onboarding done and telemetry denied;
  - Relay's light or dark, until another theme is chosen in Canvas.
- **A watch on its agent:** its status reads OpenHands' agent settings. If another agent was chosen in OpenHands, the
  page says so and offers "Use Relay's agents again".
- **Not running, or not set up:** the page as before, without the first-time steps (there are none now).
- **Relay at a base path:** the page says Canvas can't work there, rather than framing a blank page.

**The Relay app in OpenHands** (`plugins/openhands/canvas-app/`, plain JS, one file):
- **Installed and enabled by the plugin's `configure()`:** again whenever its files changed (Canvas keeps a copy).
  An OpenHands without apps works on, and the page says the app isn't there.
- **Its page "Relay"** in Canvas' sidebar: OpenHands' conversations (`host.agentServer.request`) matched to Relay's
  tasks (`/api/tasks`, same origin). Each shows its state (working, to review, done), its agent and its changes, and
  "Review in Relay", which moves the whole page to Relay's `/c/<id>` when framed. "Relay's tasks" and "Back to
  Relay" are there too.
- **Opened outside Relay's origin:** it says to open OpenHands from Relay.

**Tried for real** on the throwaway:
- **The trial's copy:** a copy of the plugin under another container name and label, as the plugin's fixed
  `relay-openhands` would have been the live one's.
- **Its container:** `127.0.0.1:18300` only. The LAN address is refused, and `/canvas` without Relay's sign-in goes
  to the sign-in (303; 401 for a POST). The app was installed and on.
- **`/p/openhands` in a fresh browser:** Canvas framed and light, no wizard, no key, "Relay" in its sidebar.
- **Two tasks in `site` on Big Pickle,** started in Canvas' own UI: the file written and appended to, both Relay code
  tasks. The app listed them ("To review", "Done", 1 file each), and "Review in Relay" moved the page to the
  task.
- **On a phone's width:** no sideways scroll; "Open in a tab" is left out there, so the bar stays one line.
- **Once, not again:** in the very first conversation Canvas' WebSocket (through `/port/`) closed three seconds in,
  so the reply showed only after a reload. In the second it stayed open and streamed the reply live.
- **Then, on a fresh OpenHands** (Remove with its data, Start), the first conversation streamed live too. A start
  after Remove used to leave the container with "None" as its key: a start makes the keys when there are none now
  (08e46a2).

## OpenHands, a closer fit: Relay's questions, deep links, its agent at hand, a newer one offered (2026-10-12)

**The user asked** me to improve the OpenHands plugin on my own, creatively, while they were away.

**Found first:** OpenHands' ACP client allows every permission request by itself (`request_permission` picks the
first option) and declines forms. Two consequences:
- **No question ever waits in OpenHands:** a badge for its conversations waiting for confirmation would never show.
- **What Relay leaves to you** (a command on your own computer, a paid subagent) was allowed unasked through
  OpenHands.

**So, in the ACP forwarder** (`--ask-in-relay`, the OpenHands plugin uses it): what Relay asks you to allow stays
in Relay, and the client gets a line saying to answer it there, as a form's question already did.

**What Relay asks, a click away:**
- **A route of the plugin's,** `conversations`: OpenHands' conversations, each with the Relay conversation it is.
- **The page** matches them to Relay's conversations that need you (`needs_approval`):
  - its bar says so, with "Answer in Relay";
  - the hand on the activity bar shows how many;
  - Relay's app in Canvas marks the conversation "Asks you", with "Answer in Relay".

**Relay's address follows Canvas':** the page reads the frame's location every second and replaces Relay's URL
(`/p/openhands/conversations/<id>`). It replaces rather than pushes, as the frame's history already has the steps.
The page is registered at Canvas' depths, so a reload, a bookmark or a link opens that route in the frame. It stops
as soon as another page of Relay's is open.

**The agent at hand:** the bar has the Relay agent new conversations start with. It's there at once, and Relay's
whole list fills in when the providers answer. Canvas' own picker can't switch Relay's agents.

**A newer OpenHands, offered:** once a day the plugin reads the `latest` image's version label from the registry
(the index, the amd64 manifest, its config: a few kilobytes, nothing pulled). When it's newer than the pinned
one, the bar has "Update to <version>".
- **While it downloads,** Canvas stays framed: the plugin's status says `ready` (running and set up for Relay), and
  only a start that makes the container anew turns the page back to its card.

**Smaller:**
- **Relay's newest project folders** are in Open Workspace once the page opens (a `sync` route), not after up to
  five minutes.
- **Relay's app in Canvas** is installed anew within a minute when its files change with the clone, not only at
  the next start.

**Tried for real** on the throwaway:
- **The deep links:** `/p/openhands/conversations/<id>` framed that conversation. Moving in Canvas moved Relay's
  address, and a reload stayed there.
- **The bar:** its agent select had Relay's 16 agents.
- **A question and a newer version,** with Relay's and the plugin's answers changed in the browser (no agent asks
  on cue):
  - the note with "Answer in Relay", which opened Relay's conversation;
  - the count on the hand, and "Asks you" in Canvas' Relay app;
  - "Update to 1.27.0" in the bar.
- **The registry look:** for real, it found 1.26.0, ours.
- **The app's update in place:** it didn't come at first, because my trial had replaced the plugin's folder and the
  container's bind mount still showed the old, deleted one. A start mounted the new folder. With git the folder
  stays, so this is the trial's doing, not the plugin's.

**Found by the walkthrough:** its throwaway Relay's OpenHands page asked for conversations and got a 500 (no key:
never set up). Behind it was something worse: its status had found the *live* `relay-openhands` running, because
the container's name was the same for every Relay on the machine. A trial's or a test's Relay could have stopped or
removed the user's OpenHands.
- **The name now:** only the Relay at its usual place (`~/.local/share/relay`) keeps `relay-openhands`, so the live
  container stays its own. Any other Relay's has that Relay's mark, as its boxes do (`relay.containers.instance()`,
  which also heeds `XDG_DATA_HOME`).
- **The route:** without a key it answers with no conversations.
- **The page:** it asks only once OpenHands is set up and ready.

## OpenHands: the bar's agent reaches its conversations, and no wait after each answer (2026-10-12)

**The user found** OpenHands' conversations waiting long for an answer, where Relay's were quick, and the agent
select in the bar unreliable.

**The long wait was OpenAI's,** not OpenHands':
- **The slow conversation** ran on gpt-6.1-sol through Codex. Its first answer took 2:41 for 116 tokens.
- **Codex' log said why:** "We're currently experiencing high demand" after 80 seconds, then "stream disconnected
  before completion" after two minutes. Codex retried each time.
- **The others,** on the same model through OpenHands, took 5 to 6 seconds, as long as Relay's own.

**The agent select did nothing:**
- **Where Canvas looks:** Canvas 1.26 starts each conversation from its active *agent profile*. It made that profile
  once from the agent settings, the first time it listed its profiles, which was before any agent was chosen. Its
  `acp_model` stayed null.
- **What the bar changed:** only the settings (`acp_model`), which the profile doesn't follow.
- **What the conversations got:** no model, so the forwarder took Relay's default. They ran on whatever that was,
  which made the select look unreliable rather than dead.
- **Why the trial missed it:** I had checked that the list filled in, not that a choice reached a conversation.

**Now `configure()` keeps Relay's profile in step:**
- **What it gets:** the settings' command (`shlex.join`, as Canvas' own seed) and the chosen agent.
- **How:** saved under its name, so its id stays and its revision goes up. It's saved only when something differs.
- **It's made active.** If no profile is Relay's any more, one called "relay" is made.
- **Canvas sends only the profile's id** when it starts a conversation, and the Agent Server reads the profile at
  that moment. So a choice in the bar holds for the next conversation without a reload.
- **The page's "isn't Relay's any more"** now reads the active profile, falling back to the settings when there is
  none.

**No wait after each answer:**
- **The wait:** OpenHands waits up to 2 seconds after each answer for the agent's `usage_update` (the tokens in
  context, the window's size). The forwarder has no window size to send, and made-up numbers would give Canvas a
  wrong meter.
- **The fix:** the container gets `ACP_USAGE_UPDATE_TIMEOUT=0`, which OpenHands reads for exactly this.

**Canvas' socket closing after 3 seconds** (seen in the trial, and again here) is Canvas' own doing. Once its page
is hidden (another tab, another app) it disconnects after 3 seconds, and when the page is back it reconnects and
reads what it missed (`after_seq`).

**Tried for real** on a throwaway Relay with its own OpenHands:
- **Two agents in a row,** each picked in the bar, each followed by a conversation started in Canvas without a
  reload. Relay's conversations had exactly those agents: opencode's ling-3.1-flash-free, then big-pickle.
- **Their runs** failed upstream ("Invalid credential" from opencode's free Zen models). A plain Relay conversation on
  big-pickle failed the same way, so that's the free models today, not this change.
- **After a restart,** the profile was still Relay's, with the agent and `--ask-in-relay`, and OpenHands read the
  timeout as 0.


## ACP forwarder: images and embedded files reach Relay as attachments (2026-10-12)

**What the user saw:** an image attached in an OpenHands conversation reached Relay's agent as the sentence "(an image
was attached: Relay's ACP forwarder passes none on)".

**Why:**
- **The forwarder dropped them.** It turned ACP `image` and `audio` blocks into that sentence, and an embedded file
  that isn't text (`resource` with a `blob`) into "(path: a file that isn't text)".
- **OpenHands sends them anyway.** It ignores the agent's `promptCapabilities.image: false`, and puts a pasted image
  in the prompt as base64 with its type (`_image_url_to_acp_block`, from a data URI).

**Now they're Relay attachments,** as files attached on Relay's own page:
- **How:** the forwarder writes each into a temporary folder (one folder each, so equal names don't clash) and sends
  them with the message (`Conversation.send(files=…)`, the SDK's own uploads).
- **Their names:** the uri's file name, else "image", "audio" or "file". When the name's own type isn't the block's,
  it gets the extension of the block's type, because the SDK and Relay take a file's type from its name.
- **What the agent gets:** each provider handles them its own way, as it does with attachments: the files' paths,
  opencode's native files, an API's image parts.
- **An image that only links** (a uri, no data) is passed on as its place, as a `resource_link` is.
- **A file Relay refuses** (too large, too many) fails the prompt with Relay's own words.
- **`promptCapabilities.image` is true now,** so clients that heed it (Zed) let you attach images. Audio stays false:
  Relay's agents can't listen, but audio a client sends anyway still arrives as a file.

**Nothing changed in Relay's core or the SDK.** OpenHands' container mounts the forwarder from this repo, so new
conversations use it at once, with no restart.

**Tried for real** on a throwaway Relay:
- **The client:** OpenHands' own SDK (`ACPAgent`) sent a Canvas screenshot as a data URI through the forwarder.
- **What arrived:** Relay's message had the attachment `image.png` (image/png, 76 KB).
- **The agent:** opencode's big-pickle, which can't see images, had a vision subagent (step-5-preview-free) read the
  file, and its one-sentence description of the screenshot was right.

## Signing in behind a proxy with a sign-in of its own (2026-10-12)

**What the user saw:** signing in at their domain sent them straight back to the sign-in page, with no error.

**Found by watching the traffic** between their proxy and Relay:
- **The proxy** is Caddy on another machine, with `basic_auth` in front of Relay.
- **The sign-in worked:** `POST /api/login` made a session and set its cookie.
- **The next request lost it:** `GET /api/state` came with that cookie, valid in the database, and still answered
  `"user": null`, so the page showed the sign-in again.
- **The cause:** the browser sends Caddy's Basic credentials with every request. Relay took any Authorization header
  for an app password ("no cookie counts then", so a page's session can't skip the CSRF header). These were no app
  password, so no one was signed in.

**Now Basic credentials that are no app password, sent with a session cookie, are a proxy's own sign-in**
(`web.proxy_sign_in`):
- **What counts:** the session, as without the header. Changes still need the `X-Relay-Request` header.
- **Where:** the API's middleware, and a plugin page's files (`plugin_static`), which had the same check.
- **What stays as it was:**
  - a valid app password, Basic or Bearer, still signs in without a cookie or a CSRF header;
  - a wrong Bearer, or a wrong Basic without a session, still gets "That app password isn't Relay's";
  - a wrong Bearer with a cookie is still refused (a script's mistake, not a proxy's).
- **Why only Basic:** it's what browsers keep and send by themselves after a proxy asks for it.

**Docs:** SECURITY.md (under HTTPS) and the guide's app passwords say that a proxy's own sign-in works alongside
Relay's.

## App passwords behind a proxy's own sign-in: X-Relay-App-Password, and the SDK's proxy credentials (2026-10-12)

**What the user asked:** with signing in fixed, app passwords still didn't work through their domain. Caddy's
`basic_auth` is on every path, on purpose.

**Why:** a script has to send Caddy's Basic credentials in `Authorization`, and Relay read an app password only from
that header. One header can't carry both. The SDK had no way to give a proxy's credentials either, so its `login()`
through the domain failed too.

**Now Relay also reads an app password from `X-Relay-App-Password`:**
- **Order:** that header comes first, and `Authorization` is read only without it, since with it `Authorization` is
  the proxy's.
- **What it counts as:** an app password does: no CSRF header needed (another site's page can't send a header of
  Relay's own without CORS), no cookie counts with it, and managing app passwords stays out of reach.
- **A wrong one** is never rescued by a session cookie: `proxy_sign_in` doesn't apply with the header there.

**The SDK takes a proxy's credentials from the address,** `https://name:password@host`, through `connect()`,
`login()` or `$RELAY_URL`:
- they're sent to the proxy in `Authorization` with every call, uploads and event streams too;
- the app password goes in `X-Relay-App-Password`, and a `login()` session's cookie goes along as usual;
- they're taken out of `Session.url`, so they never show in errors, `repr()` or links;
- without them in the address, the SDK sends what it did, so older Relays keep working.

**The WebDAV shares weren't affected:** their key is in the address.

**Docs:**
- the guide's app passwords (the header, the SDK's address, curl, pip);
- SECURITY.md, the SDK's README and its docstring;
- Settings → Account's hint when an app password is made.

**Tested** over real HTTP: the SDK with `http://alex:caddy@127.0.0.1:<port>` and an app password signs in, sends,
and streams. The browser walkthrough passed too. Caddy itself is the user's to try, on their domain.

## Usage limits: a tool for agents, usage_limits_now (2026-10-12)

**What the user asked:** a tool that tells agents the usage limits, through the Usage limits plugin.
- **The gap:** `relay agents` said "limits", but only its subagent limits (8 at once, 12 per answer, 30 minutes
  each), not what's left of a subscription or when it resets.
- **The user chose** to keep it in the plugin, so no change in Relay itself. The tool is called as a plugin's tool:
  `relay plugin usage_limits_now`, or the MCP tool `usage_limits_now` in Claude Code. A shorter `relay usage` would
  have needed a new extension point.

**`usage_limits_now [provider=ID]`,** for agents and the Governor (`governor=True`):
- **Fresh:** it asks Relay now, as its page does (Relay keeps an answer for 2 minutes), and the answer is kept as
  the 15-minute look's is.
- **Each window:** its % left (Copilot's count too), LOW at the plugin's own threshold, and when it resets: "Tue 01:39
  (in 4 h 22 min)", or a date for a reset further than six days off.
- **Its pace:** from the plugin's own history, the climb since the window last reset, over its last 3 hours (20
  minutes at least). When that would use it up before it resets, it says when. That's what the plugin knows and
  Relay's own answer doesn't.
- **The other logins on the same account** ("The same login as: opencode · default, …"), so an agent sees that
  opencode's OpenAI models draw from Codex' window.
- **What couldn't be asked now** is said too, and when Relay can't be asked at all, the last known values with
  that said.
- **`provider`** narrows it to one provider, by id or name (or a login that's the same).

**Tried for real** on a throwaway Relay with the plugin linked: an agent on opencode's big-pickle ran `relay plugin
usage_limits_now` and passed its answer on.

The plugin is version 1.1. Its README, its description and the guide's plugin list say so.

## No empty bar above a plugin's page (2026-10-12)

**What the user saw:** an empty bar above OpenHands' own bar (the green dot, "OpenHands", the agent).

**Why:** it's Relay's header, which every plugin page gets (`showPluginPage`). It holds only a phone's buttons (the
sidebar, a new conversation), so on a wider screen it was 52 pixels of nothing above every plugin page.

**Now:**
- **On a wider screen** a plugin page's header (`header.plugin-header`) collapses when it's empty, so the page
  starts at the top. It keeps only the notch's room (`env(safe-area-inset-top)`, 0 in a browser), for a home-screen
  app on a tablet.
- **On a phone,** with its buttons, it's the bar it was.
- **Narrowing or widening the window** redraws it, so it follows.

**Checked** in the UI walkthrough (scripts/relay-ui-check.py) on the Usage limits page: no height on a wide screen,
the sidebar button at 390 pixels, empty again at 1280.

## Relay 4.1.0 (2026-10-12)

**What's in it:** the entries above since 4.0.2:
- the installer bundle;
- four plugins: Usage limits, Server, ACP and OpenHands;
- `plugin.forward`;
- signing in and app passwords behind a proxy's own sign-in;
- the Usage limits tool;
- no empty bar above a plugin's page.

**A minor version:** new plugins, an extension point and a new header in the API, and nothing of 4.0's taken away.
Plugin API stays 1.

**Before tagging:**
- the full suite, ruff and the frontend's tests, again after the version bump;
- the UI walkthrough, after the last UI change (b47d617);
- each plugin tried for real on a throwaway Relay as it was made (their entries say how).

## Plugin tools for some conversations only: plugin.tool(…, only=) (2026-10-12)

**Why:** a plugin can run agents of its own, each in a conversation of the plugin's, with tools meant only for them.
A plugin's tools went to the agents of every chat and code task, so those tools would have sat in all of the user's
own chats, and their agents would have been told of them.

**Now:** `plugin.tool(…, only=)`, a function that takes a conversation's id and says whether its agent gets the
tool. `None` (the default) is every conversation, as before: plugin API stays 1.
- **Where it's asked:** where Relay lists an agent's tools (MCP `tools/list`, `relay help plugins`, the tools note a
  run is told), via `Plugins.tools(conversation_id=…)`.
- **At the call:** `call_tool` refuses a tool that isn't the caller's conversation's, whatever asks.
- **An `only` that fails** says no, and is the plugin's last error (Settings → Plugins). It's asked in Relay's own
  threads, so the guide says to keep it quick.

**Tested:** a plugin whose tool is for one of two conversations (listed, told of and answered for that one; not
listed and refused for the other), one whose `only` raises (in neither, the error kept), and the call refused
outside Relay too (`Plugins.call_tool`).

## Relay stops within seconds while an answer streams (2026-10-13)

**Found trying a plugin:** a throwaway Relay didn't stop on SIGTERM, for minutes.
- **Why:** an answer's event stream (`/api/runs/<id>/events`) stays open until its run ends, and Relay stops its runs
  at the app's shutdown (`runs.shutdown`, in the lifespan).
- **The wait:** uvicorn holds the shutdown back until every connection is done, with no limit set. So while any
  stream was read, Relay never stopped.
- **Here the reader was Relay itself:** the plugin's SDK session following its party's turn. A browser with an
  answer open does the same.
- **Under systemd,** a restart then waits for its stop timeout and kills Relay, without the app's shutdown (runs not
  stopped, boxes not tidied).

**Now:** `relay serve` gives open requests `SHUTDOWN_WAIT` (5 s), then uvicorn cancels them and the app's shutdown
runs.

**Tested:** a real uvicorn server with relay serve's settings, an endless stream read, then told to exit: it stops
and its lifespan's shutdown runs within the limit. Without the limit the same test hangs (checked with a pytest
plugin that drops it).

## Tests: a service's next day across a clock change (2026-10-13)

**What failed:** `test_a_services_last_week_its_failures_and_its_data`, on 2026-10-13 between about 10:00 and 11:00.
- **How the test counts:** it starts 10⁶ seconds from now and takes "the next day" as 86400 seconds later.
- **That hour:** the start fell just after midnight on 25 October, the night the clocks go back. So 86400 seconds
  later was still the 25th, and the service's size was measured on one day, not two.

**Now:** the test takes the next calendar day at noon (`time.mktime` with the day plus one), whatever the clock does
that night.

## VSCodium: Relay in VSCodium, as an extension of its own (2026-10-14)

The user asked for a VS Code extension, or whether the API and SDK already allowed a frontend of one's own. They work
in VSCodium, with Remote - SSH on Relay's machine.
- **What was there already:**
  - The ACP plugin with the community "ACP Client" extension gives a chat with Relay's agents. It needs Python and
    Relay's SDK where the extension runs, and has no review, forms or attachments.
  - The HTTP API has everything an editor needs ("one API, thin clients").
- **The user's choice:** an extension of Relay's own, with chat and "what seems handy".

**The plugin (`plugins/vscodium`):**
- **The extension is `extension/`:** plain CommonJS with no dependencies and no build step.
- **The .vsix is packed by the plugin itself** with `zipfile`, with the manifest and content types vsce writes, and
  fixed file times, so the same files give the same bytes. So there's no npm and no packed file in git.
- **Its routes:** `version`, `extension` (base64) and `install`.
  - **What install does:** it runs `--install-extension --force` with the newest server of each product that
    Remote - SSH left in the home folder: `~/.vscodium-server/bin/*/bin/codium-server`, and VS Code's two layouts.
  - **With no server there,** it says what to do instead (409).
- **Its Settings tab** has three steps: Open Remote - SSH, the extension (Install or Download), and an app password
  with the address and password to copy.

**The extension:**
- **`extensionKind: workspace`:** on Remote - SSH it runs on Relay's machine, so `localhost` is Relay and its
  folders' paths are Relay's. A new conversation in an open folder is a code task there. When Relay refuses that
  folder (outside its workspace roots), it's a chat, said with Relay's reason.
- **It supports untrusted workspaces:** VSCodium's Restricted Mode turned it off in the trial, and it runs nothing of
  the folder's.
- **`lib/` doesn't use VSCodium's API:**
  - `client.js` speaks Relay's API with Bearer, or behind a proxy's sign-in with `X-Relay-App-Password` as the SDK
    does. It also parses a run's server-sent stream.
  - `events.js` builds an answer's blocks from its events, as `render.js` does: delegation tools hidden, permissions
    approved by themselves left out, forms cancelled when the run ends.
  - `patch.js` gives a file as it was before a change: its git diff taken back from the file as it is now, checked
    hunk by hunk. That lets the native diff editor show a task on its own branch too. Where that can't be exact, the
    diff opens as text.
- **The chat** is an editor tab per conversation, in the theme's own colours (`--vscode-*`), with Markdown from
  VSCodium's own renderer (`markdown.api.render`), sanitised in the page.
  - **A new chat is a draft** until its first message, so no empty conversations are made.
  - **Turns are followed** as `follow()` in the SDK does it. A turn started elsewhere (Relay's page, a timer) is
    picked up when the Conversations view sees its conversation working, or changed since.
- **The rest:** the Conversations view, the Changes view (with Mark as reviewed, Commit, Merge, Discard and Undo
  all), Ask Relay about this (`Ctrl+Alt+R`), Add to Relay's chat, search, the status bar, and an update offered
  when the plugin has a newer extension.

**Tried for real with VSCodium 1.140** (its `reh-web` server: the server Open Remote - SSH runs, with its UI in
Playwright's Chromium). The setup:
- a throwaway Relay with fake agents and opencode's free Big Pickle;
- the extension installed through the plugin's install code.

What it went through:
- sign-in, and a code task that changed files;
- the diff of each file, and Mark as reviewed;
- a paid subagent's permission: Relay's own choices, answered, then the subagent card;
- a real form from Big Pickle's question tool (answered blue; "You prefer blue over red.");
- Ask Relay about this, search, an attachment, and a turn sent through the API picked up by its open chat;
- the update from 0.9.0 to 1.0.0, and the light and dark themes.

What it found and fixed:
- **The page:** a stray "null" under a message; "Working……"; tools' paths in full; the tab's icon unreadable on dark.
- **The views:** the Changes view not following a chat that was still loading.
- **Updates:** looked for only before sign-in.
- **Following a chat:** a turn that started and ended between two looks was never shown.

Two things were VSCodium's web build only, and were patched in the trial's copy:
- its webviews load from Microsoft's CDN (`vscode-cdn.net`), which 404s for VSCodium's commit;
- its secret storage doesn't outlast a page load.

Neither touches the desktop app. A `vscodium://` sign-in link was left out: the desktop app needs GTK on this server
to try it, and it isn't worth installing for that.

**Tested:** `tests/relay/test_plugin_vscodium.py` covers:
- the .vsix as a zip, with its manifest and JavaScript Node takes;
- install into fake servers in a home of its own;
- the extension's client driven by Node against a served Relay, with a stand-in agent that changes a file.

`tests/js/vscodium.test.cjs` covers the stream, the events, and `patch.before` on git's own diffs: in the middle,
hunks, made, removed, newline at the end gone and back, empty lines, CRLF, emptied. The UI check opens the tab,
downloads the .vsix and makes an app password.

## VSCodium 1.1: folders on your own computer, review, context and chat controls (2026-10-14)

The user asked what the extension still misses, compared with other extensions. Claude Code's, Cline and Copilot's
agent mode were the ones compared.

**How they really use it:** on their laptop, with local folders. Their app password is the laptop's, the server has no
`~/.vscodium-server`, and their conversation from VSCodium was a chat, so the agent couldn't reach their files.

**What they chose:**
- the extension as the laptop's computer;
- chat controls, review as Cline and Copilot have it, and context as Claude Code has it;
- not Relay-only extras;
- graphical libraries are fine to install for trials.

**Folders on your own computer:**
- **`lib/connector.js`:** `relay-computer.py`'s protocol (poll, `run`/`read`/`write`, result) in VSCodium's own
  process.
  - **How `run` works:** a command runs in a shell of its own process group, killed whole at its time-out. Its output
    is capped at its end, as in Python.
  - **Its token:** it comes from `GET /api/computers/<id>/script`; a test keeps that format.
- **A refused folder:** when Relay refuses a folder for a code task (not on its machine), the conversation is a
  computer task.
  - **Before that:** VSCodium asks once to let agents work here.
  - **The computer:** named as Relay names one (from the hostname), made with `ask` on. Relay's create leaves it off,
    though the guide says on.
  - **The agent's instructions** name the folder.
  - **The folder:** Relay knows the computer, not the folder, so VSCodium keeps it in its globalState. A fork
    inherits it.

**Review:**
- **Changes:** for a code task they come from Relay's API. For a computer task they come from `lib/checkpoints.js`,
  Relay's `checkpoints.py` again: a git repository of the extension's own looks at the folder from outside.
  - **When a checkpoint is taken:** before each answer's first change, in the connector's `before` hook, whoever
    started the turn.
  - **Per answer:** what each answer changed is counted between checkpoints.
- **A Source Control provider "Relay"** replaced the Changes view.
  - **What it has:** a quick-diff provider for the gutter, a Revert per file, and Reviewed, Commit (with its input
    box), Merge, Discard and Undo all.
  - **Before any chat is focused,** it shows the open folder's newest task.
- **Under each answer:** its changes and Undo this answer (Relay's `rewind run_id`, or the answer's checkpoint here),
  with Put them back.
- **The chat:** an edit tool's lines in red and green (`lineDiff`, a small LCS).

**Context:**
- **The open file chip:** the editor used last, also while the chat has focus, with its selection. It goes along with
  a message unless × took it away for that message.
- **`@` and `@problems`:** `@` offers the folder's files; `@problems` brings VSCodium's diagnostics.
- **Fix with Relay:** a quick fix (a CodeActionProvider) that sends the problem with its lines.
- **The terminal's selection:** through the clipboard, which is put back after.
- **Pasted or dropped files** become attachments; a file dropped from the Explorer becomes an @-name.

**Chat controls:**
- **The hover actions:** Edit (Relay's `/retry` with `text`), Retry, Fork (a computer task's fork keeps its folder)
  and Copy.
- **`/` commands:** Relay's own as on its page, done here; the agent's and the skills are sent as text.
- **Also:** an effort chip (the model's efforts, `/commands`), tokens and cost per answer (as Relay's page says
  them), and the agent's fullest usage window in the header (with every provider's in the status bar's tooltip).
- **More:** the queue with Send now and Remove, a notification when an agent is done out of sight, and the chat in
  the side bar's Chat view (collapsed until used).
- **The `vscodium://` sign-in link is back.** VSCodium asks whether the extension may open it, then the extension asks
  before it signs in.

**Tried for real:** the desktop app (VSCodium 1.140 under Xvfb, its own home, Playwright over CDP) was the laptop,
with a folder outside the throwaway Relay's roots:
- a computer task with a fake agent, and with Big Pickle, which added a line through `relay run`, read it back, and
  saw `bad.json` through the context chip;
- Source Control with its gutter marks, Revert, Undo this answer and Put them back, and Mark as reviewed;
- the context chip, `@`, `/`, pasting, `@problems`, Fix with Relay on a JSON problem, and the terminal's selection;
- Ask Relay about this, Edit, Retry, Fork, the side bar, the done notification, and the sign-in link through
  `--open-url`.

The reh-web server stood in for Remote - SSH: a code task's changes in Source Control, with the gutter (no Git there)
and Commit from its input box into the project's git.

**What the trial found:**
- the side bar's chat taking the place of a new one; it's now collapsed until used;
- forks without their folder;
- "1-1" for a one-line selection;
- the long tools path in tool lines;
- Source Control empty until a chat was focused.

**On the desktop app,** the trial needed `window.dialogStyle: custom` (else dialogs are GTK's) and "Reload Webviews"
after attaching over CDP (else webviews made before aren't attached).

**Tested:** `node --test` covers:
- the connector (commands, time-outs with what they started, output caps, files, the loop);
- the checkpoints on real git (added, modified, deleted, renamed, ignored, restore and its undo);
- edits' lines, tokens and Retry's message.

`test_plugin_vscodium.py` has the extension's connector polling a served Relay, which has it run, write and read.

## Checkpoints: a new repository in the folder no longer stops them (2026-10-14)

Found in the VSCodium bug hunt, by its own checkpoints, which copy Relay's: a folder with a repository of its own in
it that has no commit yet stopped all of Relay's checkpoints.
- **The cause:** git lists such a folder as `dir/`, and `git add` can't add it ("does not have a commit checked out").
  That happens after `git init` in a subproject, or after a scaffolding tool that runs it.
- **What it took down:** that one folder made the whole `git.add_all` fail. `checkpoints.take` then gave None: no
  rewind, and no changes for a checkpoint task.
- **The fix:** `git.untracked` now leaves out a listed folder that is a repository without a commit. One with commits
  is kept as git keeps it.

**Tested:** `test_checkpoints.py` covers a fresh repository in the folder.

## VSCodium 1.1.1: a bug hunt (2026-10-14)

The user asked for a bug hunt after 1.1. I read the new code again, tried its edge cases in `node --test`, and tried
the scenarios 1.1's trial hadn't covered in the desktop app.

**What the reading found (fixed):**
- **The open file's chip** went when the chat covered the file in the same editor group: VSCodium calls that file "not
  visible". It's a snapshot now (document and selection), gone only when its file is closed.
- **Paths from two machines:**
  - folder matching, `@problems`, a dropped file and a file's place in a message took Relay's "/" for granted. They
    used a bare `startsWith`, so `demo` matched `demo2`;
  - the gutter's marks compared paths, not VSCodium's URIs, so on Windows (drive letters, `\`) they wouldn't show;
  - `lib/places.js` (`inside`, `near`) handles both now.
- **The side bar's chat after a move:** VSCodium makes the view anew, but it kept pointing at its closed chat and stayed
  empty. It shows the same chat again now.
- **The computer fallback:** any 400 or other 4xx made a computer task. Only Relay's refusal of the folder ("a
  workspace root") does now.
- **Two checkpoints at once:** a new computer task's start ran loose while its first message went, so its first turn's
  checkpoint and the start's could trip over git's lock. A refresh and a look could too. Each folder's repository now
  does one thing at a time, and the start is kept before the message goes.
- **The agent's commands and efforts:** a draft that had looked at `/` cached them without them, and the effort chip
  never came. The cache is per conversation now.
- **Commands for the agent:** the open file's context was added after them (`/compact` with arguments). It isn't now.
- **Done:** "done" was said after each of a `/timed` task's turns. It's said once, when it's all quiet, with a failed
  answer's error.
- **Speed:**
  - Markdown was rendered again for every block each time a conversation showed. It's cached by text now.
  - A live answer resent every tool's output each 120 ms. Output that didn't change isn't sent again.
- **Smaller ones:**
  - a renamed server file can't be put back by its own (the old name isn't in Relay's list): said so;
  - an earlier answer's summary no longer reads all changes up to now first;
  - a draft's page no longer says "on Relay's machine".

**What the edge cases found:**
- **A nested repository without a commit** (`git init` in a subproject) made every checkpoint fail. It's left out now,
  in the extension and in Relay's own checkpoints (the commit before this one).
- **Concurrent checkpoints and odd file names:** spaces, unicode, quotes, tabs and folders with spaces now pass.

**What the desktop app found:**
- a "Lost Relay … taking it up again" notice stayed after it was taken up; it goes now;
- a proxy's own 401 (Caddy's basic_auth, as in the user's setup) said only "HTTP 401". It says to put the sign-in in the
  address now.

**What held up there:**
- Relay restarting mid-answer: the run ends as Relay says ("Relay restarted during this run");
- a revoked app password, in Relay's words, with Sign in;
- the chip in one editor group;
- the side bar's chat moved to the secondary side bar.

**Tested:** `node --test` covers the paths, concurrent checkpoints with odd names and a nested repository, and the
proxy's 401. The whole suite passed: 932.

## Chrome 1.1: Relay's Chrome extension in Relay (2026-10-24)

**Where it came from:** the user asked one of Relay's own agents (a Claude Code chat in a WorkBox, the project
`~/projects/chrome-extension`) for "a Chrome extension for Relay".
- **Its rounds:** a side panel chat with the page, a selection or a screenshot going along, scripts run from there, the
  queue shown, and titles without the browser's context.
- **Last:** a `chrome` plugin that gives agents tools for the user's own tabs (`chrome_read`, `chrome_click`, …), each
  step a card in the panel.
- **What it couldn't do from its WorkBox:** install the plugin.
- **I installed it** (a copy) and tried it for real: a Haiku agent found a tab, read it, typed and clicked, was denied
  a click, read a screenshot it took, and was stopped while a step waited. All passed on the first run.

**Why it moves into Relay:** that project wasn't in git, and an installed copy of a plugin drifts from its source.
`plugins/chrome` now ships the extension as VSCodium's plugin does.
- **`extension`** zips `extension/` (fixed file times: the same files, the same bytes) into one folder,
  `relay-chrome/`. A later version is unzipped over it and reloaded at `chrome://extensions`.
- **Settings → Chrome:** Download, an app password made for it, and whether a Chrome is connected now.
- **The tests:**
  - **Before:** they ran against stand-ins of Relay's plugin API (`fake_relay`).
  - **The plugin's own** (`test_plugin_chrome.py`) now use Relay's plugin host: routes and tools in the plugin's own
    threads, Relay's own check of a tool's arguments, and the run-ended hook.
  - **The browser checks** (`tests/chrome/e2e*.cjs`, Node with Python Playwright's driver and its full Chromium;
    headless-shell loads no extensions) run from `test_ui_chrome.py`. Their mock of Relay's API loads the plugin with
    Relay's host too.
  - **The real trials** stay by hand (`tests/chrome/real*.cjs`).
- **Found on the way:**
  - Chromium's singleton socket refuses a long `TMPDIR` (pytest's own), so the test leaves `TMPDIR` alone.
  - The harness now removes the Chromium profiles it made: they keep the extension's settings, its app password
    with them.

## Chrome 1.2 (extension 1.3): agents use Chrome with its side panel closed (2026-10-24)

**The user's pick:** agents could use Chrome only while its side panel was open, since the panel was what asked the
plugin for commands. Now the extension's service worker does.
- **`BrowserAgents` runs in `background.js`.** It starts when signed in (Relay's `/api/state` has a user) and "agents
  may use this browser" is on. It starts again only when the address, the app password or that setting change: the
  agent picked in the panel is a setting too, and starting again would drop what waits.
- **Staying up:** Chrome stops an idle service worker after 30 seconds, and a fetch in flight doesn't count, but an
  extension API call does.
  - Each ask for commands makes one (`runtime.getPlatformInfo`).
  - An alarm every 30 seconds starts the agents again if Chrome stopped the worker all the same. That's within the
    plugin's 45 seconds before a Chrome counts as gone.
- **The cards** (`lib/cards.js`) are drawn by the pages that show them:
  - the side panel;
  - `approve.html`, which a notification opens when no panel is open.
  - Both get the cards over a port and give your answer back.
  - Chrome asks for its permission to use pages there, in your click, since a service worker can't ask it.
- **Notifications:** one per step that waits for you, while no panel is open. They go once a panel shows the card,
  and the badge counts what waits either way.
  - **A click opens the card's window, not the side panel:** whether Chrome takes a notification's click as the user
    gesture `sidePanel.open` needs isn't something to count on, and the window always opens.
- **Tests:**
  - **The panel closed:** a notification and the badge, the window's card, allowed there, and the window closing itself.
  - **The worker restart:** the test stops the worker as Chrome would, with CDP's `Target.closeTarget`
    (`ServiceWorker.stopAllWorkers` didn't touch an extension's). The alarm starts it again, and it's connected.
  - **The setting off:** not there.
  - **The real trial** has a step with the panel closed: the message sent through Relay's API, the card allowed in
    the window, and the agent's answer.
- **Found on the way:** the agents started with an app password the settings had just been given, before it was known
  to work. `e2e.cjs`'s wrong-password step showed it (calls without the right password); now they wait until Relay
  says the extension is signed in.

## Chrome extension 1.4: writing on the page (2026-10-24)

**The user's pick:** write on the page, as other AI extensions do: rewrite a field's text and put it back, and an
answer put into the page.
- **"Rewrite with Relay"** (the right-click menu in a text field):
  - **The choices:** improve, shorter, more formal, to English, to Dutch, or "In your own words…", which waits in the
    composer.
  - **Taking the text:** `lib/inpage.js` takes the focused field's text (its selection, else all of it) and keeps the
    field. It keeps it as `field()` keeps everything, in the extension's own world of the page, nothing written into
    it.
  - **Asking:** the text goes along alone, as a selection, in a new chat. The message asks for only the new text, and
    the chat is named by the instruction.
- **Replace in field** (under that answer) puts the answer back in place of what was taken, once.
- **Insert in page** (under every answer) puts the answer at the cursor of the tab's focused field.
- **What's written:** the answer as it reads, from its rendered Markdown (`innerText`: paragraphs and lines, no
  signs). It's written as typing would be (`execCommand("insertText")`, else `setRangeText` and an input event), so
  the page hears of it and Ctrl+Z undoes it.
- **Which field:** `runId → field` is kept in `chrome.storage.session`, so the button is still there after the panel
  reopens, for as long as Chrome runs.
- **Tests:**
  - **The menu itself** can't be clicked in a test, so the test does what it does: `field()` in the page, then the
    pending message.
  - **The rest:** Replace in field, a selection replaced alone, an editor, once only, Insert at the cursor, and no field
    focused.
- **Found on the way:** a context waiting in the composer (a link from the menu) went along with the rewrite. A
  rewrite now clears it, since only the field's text belongs with it.

## Signing a provider in again from Relay's page (2026-10-25)

**What happened:**
- **The login ran out.** At 10:43 Claude Code's login on the machine ran out: every run said "Failed to authenticate:
  OAuth session expired and could not be refreshed". The Governor's runs failed too. Its fallback, `gpt-6.1-sol`,
  failed on its own.
- **The login is shared.** Relay's `claude-code` has no login of its own here: its box mounts the machine's
  `~/.claude`, the developing Claude Code's.
- **Relay sent the user to a repair,** after three failures in a row. Its agent found the lost login but couldn't
  help:
  - its `repair_run` has no terminal for the code `claude auth login` asks for;
  - the repair page had a shell only for a provider with a home of its own;
  - the chat's Shell is the repair folder's WorkBox, where `claude` isn't;
  - its brief said the box had the login read-only. It hasn't: only the program is.
- **Fixed then:** the user typed `/login` in the developing Claude Code, which renewed `~/.claude`. Relay's Test with
  haiku answered.

**A shell with the program and its login:**
- **The interface:** `Provider.SIGN_IN`, the command that signs its program in again: `claude auth login` for Claude
  Code, `copilot login` for Copilot.
- **The shell:** a provider that has one gets a **Shell** in Settings → Providers, as one with a home of its own has.
  It runs in a setup box (`Boxes.setup` without a home) with what its own box mounts (`box_mounts()`) and its `env()`,
  in the machine's HOME, its shells real ones. The box goes with the shell (`Creations.provider_shell`, formerly
  `home_shell`).
- **On the repair page:** the same shell, for a provider without a home. Apply and discard remove its box, as they
  remove a home's.
- **The brief** tells the agent where the user signs in again, and that `repair_run` can't.
- **Not for opencode:** its logins are per profile, in opencode-manager.

**A lost login, said as one:**
- **Which errors:** `repair.LOGIN_LOST`, a lost login's words: "failed to authenticate", "OAuth", "not signed in", a
  401, a refused key.
- **The scheduler:** when the three failures in a row are all such, it tells the user "… isn't signed in any more",
  with AgentOS on or not. It doesn't wake the Governor, which can't sign in either.
- **The link** opens the repair start page. The trouble route says `login_lost` there, and the page starts with the
  provider's shell and **Test again** (the model that failed last). Start a repair stays below.
- **Claude Code's error** (`claude_backend._said`) keeps its own words, which say what happened and are how
  `LOGIN_LOST` knows it. It adds where to sign in again in Relay: its advice, "Please run /login", is for its own
  terminal.

## A computer's connector behind a proxy's own sign-in (2026-10-30)

**What happened:** the user ran `relay-computer.py` on their laptop against their domain, which Caddy's
`basic_auth` guards. It couldn't work:
- **The address in it was wrong.** Relay builds it from the request (`site_url`), and behind Caddy that's
  `http://<the domain>`: Relay doesn't take Caddy's forwarded headers. The site is https only.
- **Caddy's 401** was said as "Relay doesn't know this connector (any more)".
- **A name and password in the address** (as the SDK and the VSCodium extension take them) crashed `urllib`:
  "nonnumeric port".

**Now:**
- **The page's own address:** Settings → Computers sends it with the download (`?url=`), and the script reaches Relay
  there. `public_url` still comes first; without either it's the request's, as before.
- **The connector takes a proxy's sign-in:**
  - from the address, `https://name:password@host`, sent as `Authorization: Basic`;
  - **`--url`** (or `RELAY_URL`) gives that address, or another;
  - **without them,** a 401 that asks for Basic makes it ask for the name and password in its terminal, again when
    they weren't taken. Started without a terminal, it says how to give them and stops.
- **The GUIDE** said a new computer asks before each command. It doesn't (the API's test says so, "usability first"):
  the GUIDE says off now.

## VSCodium 1.2 (2026-10-30)

The user asked to improve the VSCodium extension. They picked all four proposals: agents using VSCodium, inline
editing, Keep/Undo per change and the small things. They added permission control in VSCodium, with a way to turn it
and Keep/Undo off. They use it on their laptop with local folders, so most of their conversations there are computer
tasks.

**Agents use VSCodium:** the plugin's tools `vscodium_problems`, `vscodium_definition`, `vscodium_references`,
`vscodium_symbols` and `vscodium_hover`.
- **How they're routed:** as `plugins/chrome` does. Each call is a question for one of the user's VSCodium windows,
  which ask for theirs (`GET tools`, held open) and answer from VSCodium's own language servers
  (`vscode.execute…Provider`, `languages.getDiagnostics`).
- **Which window:** a window says its folders, the computer tasks in them and whether it's in front (`POST windows`).
  A question goes to the one with the conversation's computer task, else its folder, else the one in front last.
- **What the trial showed** (VSCodium 1.143 under xvfb, its built-in TypeScript):
  - **TypeScript only looks at files in sight:** a file that's only open as a document gets no problems. For one
    file, `vscodium_problems` shows it a moment (no focus, a tab of its own that's closed again).
  - **A name imported with `require`:** its definition is the import. The tool goes one step on, to where it's
    defined.
- **`symbol`** says which name on the line. Agents know names, not columns. Without it, the line's first name past
  the keywords is used.
- **`@problems`** in the chat and the tool share one format (`lib/tools.js` `problemLine`).

**Keep/Undo per change, in the editor** (`inline.js`, `lib/hunks.js`):
- **The hunks:** a Myers line diff of the file before the agent (the review's `original`) and the editor's text now.
  Lines keep their ends, so putting one back is exact. Past 4000 edits, the changed part is one hunk.
- **Keep** is remembered per task and per hunk: its key is the change and where it was in the file before, so other
  changes around it don't lose it. Once every change is kept, it offers Mark as reviewed (Relay has that per task,
  not per file).
- **Undo** replaces that hunk's lines only, and saves: the agent reads the file from disk.
- **`relay.review.inline`** turns the lenses and marks off. The trial: off, none show; on again, they're back.

**Inline editing (Ctrl+I)** (`inlineEdit.js`, `lib/edit.js`):
- **The agent works in a temporary chat** (`temporary: true`, read-only, Relay's tools off), told to answer one code
  block. It sees the 40 lines around, with the picked ones between markers. The chat is deleted once it answered.
- **Its code** replaces the picked lines, unsaved. It shows as Keep/Undo's hunks, with Accept · Reject (Reject puts
  each hunk back). If the lines changed while it worked, its code opens beside them instead.
- **Which agent:** `relay.inlineAgent`, else Relay's pick for a new chat, else the latest conversation's, as
  Chrome's Rewrite picks it.
- **The trial,** with free `opencode/big-pickle` in the throwaway Relay: "return 0 for an empty list" on `average`.
  Reject gave the text back as it was; Accept kept `return items.length === 0 ? 0 : sum(items) / items.length;`.

**Permission control in VSCodium** (`relay.permissions`: `ask`, `commands`, `off`; `permissions.js`,
`lib/permissions.js`):
- **Only on this computer:** the extension's connector sees each command and write before it's done (its new
  `allow` hook, before the checkpoint), so VSCodium can ask there. Code tasks on Relay's machine write from Relay's
  containers, which Relay doesn't ask about (AGENTS.md): Keep/Undo is their review.
- **The question:** a card in the task's chat when that's in sight (as Relay's own questions look), else a
  notification. A change opens beside it as a diff (`relay-proposed:`, from the job's content), never over the
  chat, and closes once answered.
- **Which task and answer:** the job's folder, else (a command without `--cwd`) the one of this window's computer
  tasks with an answer at work. "Allow for this answer" is kept by its run.
- **One question at a time.** The connector runs jobs side by side, and an answer may let the next one through.
- **In time for Relay:** at most 90 s, and no longer than Relay waits for the job (its time-out + 25 s, less 15).
  A command's own time-out is what's left. A question whose answer stopped or ended is withdrawn (looked at every
  3 s), so it doesn't hold up the next.
- **Relay's own question** (the computer's `ask`) is turned off when the connector starts, so nothing is asked
  twice. The extension no longer turns it on for a computer it makes.
- **The trial** (a computer task in a folder outside Relay's roots):
  - Deny reached the agent as "The user refused this in VSCodium" and left the file as it was.
  - "Allow for this answer" let both of an answer's writes through.
  - `off` asked nothing.
  - `commands` let the write through and asked before `echo`.
  - **Found on the way:**
    - the diff opened over the chat, hiding the card's buttons, so it opens beside now;
    - a question of a stopped run held up the next for its whole 90 s, so it's withdrawn now;
    - a `let` named like one already there emptied the chat's page. `node --check a.js b.js` checks only `a.js`, so
      check each file on its own.

**Small things:**
- **Files in an answer:** a code span naming a file opens it at its line. A name needs a folder, a line or a known
  file extension, so `items.map` stays code. Markdown links to files (`path#L12`) open there too, and a missing
  file says so.
- **A tool's file:** `~/…` and Windows paths (`C:\…`) count as whole paths now, besides `/…`.
- **Chats after Reload Window:** a `WebviewPanelSerializer` (`onWebviewPanel:relay.chat`). The page keeps its
  conversation's id (or a draft's folder) with `setState`.
- **Following the agent** (`relay.follow`): a done edit (or a `write` tool) in the chat in sight opens its file
  beside it, at the first line it added, without the focus. On this computer, a connector's write does the same
  (its new `after` hook).
- **The terminal:** a Pseudoterminal "Relay: <computer>". It shows each command, its output as it comes (`run()`'s
  `output`, decoded per stream, so a character split between two pieces comes whole), its exit code, and the reads
  and writes. The Output channel keeps the connection's log.
- **The trial:**
  - `src/math.js:7` and `README.md` became links (not `items.map`), and the link opened math.js at line 7.
  - The edit opened main.js beside the chat.
  - After Reload Window the chat's tab had its conversation.
  - The terminal showed `one`, then `two`, then `(exit 0)`.

## VSCodium 1.2.1: the laptop's files within reach (2026-10-30)

**What happened:** the user practised for an exam with Claude Code in a plain chat, made from Chrome and opened in
VSCodium on their laptop. "Still a bit rough":
- **The agent couldn't open the laptop's file.** The chat sent "(Open in VSCodium: /home/me/…/main.py)", a path on
  the laptop that the agent read as Relay's machine's. It tried `/tmp`, the chat's folder, the computer tools.
- **The laptop's computer was never connected.** The user had made it in Settings → Computers for `relay-computer.py`. The
  extension is a computer's connector only once a task in a folder there starts (its "Let them"), and none had.
- **Relay's offline message** said to start `relay-computer.py`, while VSCodium was open on that computer.

**Now:**
- **This computer, by itself:** when Relay's address isn't this machine (`lib/places.js` `sameMachine`), the extension
  turns this computer on as it signs in. If Relay already has a computer of this one's name, that happens without a
  question (and a notice says so); otherwise it asks once.
- **The open file says where:** "(Open in VSCodium, on the user's computer “laptop”: …)", or that Relay can't reach
  it now. This is for a chat that isn't a task there.
- **The question for a plain chat's command** (permission control) goes in that chat's card when it's the one at
  work. Before, only computer tasks were looked for, so it was a notification.
- **Relay's offline message** names the VSCodium extension too.
- **The trial,** with Relay at 127.0.0.2 (another machine to the extension) and a computer named like this machine:
  - signing in turned it on, with the notice;
  - a plain chat's message said "on the user's computer “relay”";
  - `run echo dbg` was asked in that chat's card, and Allow gave `Exit code 0`;
  - with no chat open, it came as a notification.


## Ready for GitHub (2026-11-02)

**Why:** the user is going to put Relay on GitHub themselves, and asked for an export with a README, instructions to
build from source, documentation of every feature and the code, tidied first.

**The code:**
- **Stricter lint:** ruff now also checks pycodestyle, import order, pyupgrade, comprehensions and blind excepts, and
  RUF100 keeps each `noqa` true. Scripts that run elsewhere (`connector.py`, `tools_script.py`, `relay-acp.py`, down to
  Python 3.8) keep their idioms.
- **No wholesale formatter:** `ruff format` or prettier would have rewritten the code's own compact style in every file.
- **Within 120 columns:** Python (tests' data aside) and JavaScript. Every wrap was checked: each file's syntax tree had to
  stay the same as the last commit's (Python's `ast`, without docstrings' whitespace; JS with espree, `"a" + "b"` folded).
  Two rounds: the first passed the check but read badly (a lone `"be "`, uneven indents), so the second was by eye.
- **Dead code:** vulture and eslint found six unused constants and methods and four unused names in the frontend. Their
  other findings were false: AgentOS's capabilities are dispatched by name, `tools_only` is read by the page.
- **Personal details:** gone from tests and docs (a domain, a hostname, a name, addresses on the user's network).

**The docs,** from an audit against the code:
- **New:** `docs/FEATURES.md` (all of it on one page), `docs/BUILDING.md`, `CONTRIBUTING.md`, `THIRD-PARTY-NOTICES.md`.
- **Undocumented until now:** Ctrl+Shift+O, Send now, doctor's flags, `systemd-unit`, `RELAY_FORWARDED_ALLOW_IPS`,
  `relay agentos`, `relay_sdk.plugins()`, the built-in skills' names.
- **Wrong until now:** `session_days` in the sample, the box image's contents and size, "four kinds" of boxes, how to
  uninstall a user service.
- **`SECURITY.md`** now covers Chrome and VSCodium.
- **The version stays 4.1.0:** a new number and tag are the user's call. The release notes list what's come since.

**The bundle:** `make-bundle.sh` failed on a repository without tags, which is what a fresh upload is. It now takes
HEAD then.

**A fresh clone of the export, following `BUILDING.md` to the letter,** found what working in the old tree couldn't:
- **pytest 9.2 breaks pytest-xdist 3.8's groups** (even a plain `xdist_group` mark: the id's `@group` isn't kept). The
  tests that share real podman ran side by side and collided, in another way each run. The docs said to install
  whatever's newest, so everyone would have had that. Now `requirements-dev.lock` pins the dev tools too.
- **The images** were built `--no-cache` too, under tags of their own: the box image is 429 MB, the work image 3.33 GB.

## Relay 4.2.0 (2026-11-02)

**What's in it:** the entries above since 4.1.0:
- two plugins: VSCodium and Chrome;
- `plugin.tool(…, only=)`, and a forwarded app's cookie for localhost;
- a provider's Shell to sign in again, and a lost login said as one;
- the computer's connector behind a proxy's own sign-in;
- the tidied code, the docs for a public repository, and the dev tools pinned.

**A minor version:** new plugins and an extension point, and nothing of 4.1's taken away. Plugin API stays 1. The user
chose the number, after the export had first kept 4.1.0.

**Plugins kept out of the repository:** the user keeps a few on their machine that have little to do with Relay. One
can bring its part of `relay-ui-check.py`, in `plugins/<name>/ui_check.py`.

**Before tagging:**
- the full suite, ruff, the frontend's tests and the UI walkthrough, on the development tree and on a fresh copy of the
  export;
- each plugin tried for real as it was made (their entries say how).

**Still open:** `test_terminal::test_a_kept_shell_goes_to_the_page_that_opens_it_last` failed once under parallel load
during the export's round, and passes alone. Not looked into.

## A newcomer's install (2026-11-02)

**Why:** the last real try of the service install was at 3.0.0, and this is what anyone from GitHub will do first.

**How:** a new Linux user with a systemd session and a repository as the export has it. A Sonnet subagent, knowing
nothing of Relay, followed the README literally and drove the page with Playwright, on free Zen models. Two rounds: it
went on after the fixes, removed Relay by the guide and installed again with podman's store empty. Beside it, the
README's first steps on bare images of Debian 12 and 13, Ubuntu 24.04 and 26.04 and Fedora: without podman inside (they
have no room for it), but they showed what a machine lacks.

**How it went:** two minutes from `git clone` to the images built, on a fast connection, and about 4.5 in round one with
a port clash in the way. All five bare images got as far as Relay answering.

**Fixed, as it misled someone:**
- **The first start:** the page looked ready while chats and the model list waited, and nothing said why. A banner says
  so now, and the installer that it's coming.
- **Commit:** the panel lists the task's own changes, but a commit takes the whole folder: two files seen, four committed.
  The dialog now names the others, a commit without a git identity says it's Relay's, and a commit counts as reviewed.
- **Settings → Plugins:** "No plugins yet" with one just linked, and no word of those that come with Relay.
- **A stopped answer:** "Stopped." also for a restart. The run knew why; the page says it now.
- **The Server plugin:** a WorkBox's home inside is `/home/relay`, only skipped on a machine whose user has that name.
- **Settings → Workspaces** pulled gluetun's image and ran it just to fill the VPN dialogs, on opening the page. Now
  when a dialog opens.
- **The docs:** `curl` in the package line (the opencode installer needs it, a bare Debian or Ubuntu lacks it), what it
  takes before installing, how to remove all of it (tried for real: nothing of Relay's was left).
- **`cmp`:** a bare Fedora has none, and the launcher then updated `vendor/` at every start.

**Checked and not as reported:** "a restart throws the partial answer away". The stored run held only reasoning: nothing
had been said yet. With real text on screen it stays.

**Left as it is:**
- **Views that change only the left column:** the user's own design (2.11), and so is the phone's one-row header.
- **A task started before `git init`** stays one without Commit: a new chat in the folder has it.
- **Settings over the banner:** the dialog dims it.
- **A raw stack trace for opencode's service** when its port was taken, seen once in Settings → Providers, which we
  couldn't find again. Only on a machine with another user's opencode.
- **Not tried:** podman inside the bare images, so also the old podman of Debian 12; a real slow download.

## Logging in while adding a provider (2026-11-02)

**Why:** adding Claude Code worked, but it then still had to log in, with the Shell icon in its row, and nothing said so.
The user wrote what the page should say, and chose how Add provider behaves: it always works, without a word once the
login has passed, and it asks first until then.

**The page,** for a kind with `Provider.SIGN_IN`: Template, then the shell and a test message, then Add provider and the
Options. A shell and a test before the provider exists: `draft_template`, `WS /api/provider-kinds/<kind>/terminal`
and `POST /api/provider-kinds/<kind>/test`.
- **Nothing stays:** the shell isn't kept, its box is named after the kind so it can't go with an existing provider's,
  the test notes no health, and its box goes unless a provider of that id has it (`Boxes.remove`).
- **The test** runs as the page opens, so someone logged in has nothing to do, and on **Check again**. It counts for that
  exact template: changing it means asking again.
- **Leaving:** this page has no way out of its own (Back, another tab, Settings closed, Add), so a timer ends the shell
  when its element is gone.

**Tried** with a fake `claude` in a throwaway Relay, with a home of its own so the real login stayed out of it, and once
with the real program on haiku. The page isn't in `relay-ui-check.py`: opening it would test this machine's real login.

**Seen once, not explained:** a user's `claude-code` template was gone seconds after they added a second one and used
its shell. Adding and shells don't touch other templates, and the same steps here kept both. The bin in Settings is the
only way left for a provider of the user's, and its dialog had the focus on Delete, so Enter confirmed it. It has it on
Cancel now. Nothing logged requests, so a removal now says so in the log.

## A Shell in the provider's own box (2026-11-03)

**Why:** the user wanted a shell in the box of an opencode profile, to open `opencode` there and connect OpenRouter or
OpenAI to that profile: more than a login command. Then: generic, not provider-specific, as far as possible.

**The rule now:** a provider with a box (`box_key()`) has a **Shell**, and it opens in that box (`Creations._in_its_box`).
`SIGN_IN` no longer decides that: it only names a command, and turns on Add provider's sign-in step. Two cases keep a setup
box. A home of its own, as installing there needs real shells all the way down. And Add provider's page, as there's no
provider or box yet.

**What's left for a provider, through hooks:** `env()` (which already existed), `shell_ready()` and `SHELL_HINT`. For opencode:
- `env()` is its profile's folders. `repair_run` used this hook too, so it ran opencode against the default profile whichever
  was being repaired. That's fixed along the way.
- `shell_ready()` starts its service first, as Relay's. `end_terminal` kills what carries the shell's tag, so a service started
  from the shell would end with it, and any run using it too. A service that won't start doesn't stop the shell: that may be
  why it's opened.

**A real shell, but only the first one:** `/bin/sh` in a box is `relay-shell`, so what a program starts from the Shell goes to
a WorkBox. That keeps agents' commands in WorkBoxes whatever they're started from. The cost: a `#!/bin/sh` script such as
`which` says "no project folder" in the home. A check for the shell's tag in `relay-shell` would have made them real, but a
service restarted by hand would then run agents' commands in the box itself, and all boxes would need a new image.

**Tried for real** in a throwaway Relay, with real podman and opencode 2.0.18 and no logins, through the page:
- `opencode` runs in the page's terminal. `/connect` with OpenRouter and a fake key put it in that profile only, and Relay's
  model list had 364 of its models at once, with the other profile at 0, and the service's PID unchanged.
- OpenAI's `/connect` offers browser, headless and an API key. None was tried with a real account.
- With the Shell open, a free chat on that profile answered, with the same box, service and shell.
- A stopped box, as after a reboot: the Shell brings up the box and the service first, without the shell's tag.
- With a fake `claude`, its Shell is its real box; Add provider's page still has its setup box, gone afterwards.
- Perplexity's Shell opens in its WorkBox-image box, and a repair's page has one for a profile.
- **A trial's own trap:** with `HOME` moved away and nothing mounted under it, the box has no such folder, and `exec -w` fails.
  A real install has one. A box also has no `chats` mount until that folder exists, so a brand-new Relay's first chat remakes
  it, ending a Shell opened before.

**Sending back what it got:** a template from the API with `shell` in it was refused as having unknown keys, as one with
`sign_in` already was (Claude Code's, from a script). The suite caught the first. What `masked()` adds now goes first
(`web.without_about`).

**Not tried:** `claude auth login` and `copilot login` in the real box, as a trial would replace the user's login.

## Next step: a new provider's box before it's added (2026-11-03)

**Why:** the page of a new opencode provider had no Shell, as that step still hung on `SIGN_IN`. The user's suggestion: Next step
everywhere, which makes the box from the JSON given, so you can log in.

**The flow:** the first page's **Next step** (`start_draft`) says what's wrong with the template and makes what it names
(`Provider.prepare_new`). The next step has the shell in the real box (`_in_its_box(draft=True)`), the test message and **Add
provider**, the only button that adds it. As the user chose earlier: at once when the test passed; otherwise it asks.

**What went, as a draft now uses the real box:** the `login-new-<kind>` setup box, `_signing_in`, `_login_box`, and `Boxes.setup`
without a home.

**Two choices of mine, approved with the plan:**
- **A profile named that isn't there is made.** Relay's pages had no other way, and an existing profile has its Shell in its
  row already. It stays when the provider isn't added, as it may hold a login.
- **Kinds without a box get the step too,** with only the test message. A wrong key shows before adding, at one tiny message.

**Two bugs this would have hit:**
- **`test_draft` compared template ids** before removing a box, so a new opencode provider's test would have taken the shared
  box away, with every profile's service. `drop_unused_box` looks at `box_key()`, and at the shells open in it.
- **A new Relay's first test remade the box:** `_wanted` skips folders that aren't there, so `chats` appearing changed its spec,
  ending the shell just opened. `_common` now makes what it mounts first. The first test also waits for the shell.

**An unknown profile stops the shell** (`shell_ready`): `env()` then has no profile's folders, and `/connect` would land in the
default profile.

**The words:** "It works" instead of "You're logged in", as opencode's free models need no login.

**Tried for real** on a Relay with an empty data folder, with real podman and opencode:
- A new `work` profile: the shell came up, the first test left it running, `/connect` with OpenRouter and a fake key was stored,
  and Add provider added it, with 364 of its models in that profile and none in the default one. Check again then said "User not
  found", OpenRouter's answer to that key.
- **Seen there:** a new profile's default free model answered "not available in your country", from here. So its test fails until
  another model is its default, and Add asks first. Relay isn't what fails.
- Claude with a fake `claude`: "Not logged in yet", Add asks, its login in the shell, Check again passes, Add adds at once, and
  its box stays. A `config_dir` in the JSON is mounted. A changed id gets its own box.
- Leaving by Back or by closing the page removes Claude's and Perplexity's boxes; opencode's stays with the same services.
- OpenAI-compatible with a dead URL: no shell, "Not working yet", Add asks.

**Worked in a git worktree:** the page's code is live as soon as it's edited, and the user was on this page. A Next step calling a
route the running Relay hasn't got yet would have stopped them adding providers.

## A last look before the upload (2026-11-03)

**Why:** the user went through the export before uploading it, and found three things.
- **Addresses of their own network in tests:** now example ones. The tests only need an address on a LAN.
- **The user's words quoted in Dutch in this log:** translated, as some earlier ones already were. Quotes here are
  translations unless they're English in the source.
- **Dead links in the release notes:** the sections of the README that moved to the guide in 3.0.0. They lead to the guide
  now.

**Why the test missed those links:** `test_docs` skipped the two logs, as "they keep what was true then". A link that goes
nowhere helps no one, and this log has none, so nothing is skipped any more.

**How the Dutch was found:** a list of common Dutch words, and the other way round: quoted text whose words the English
docs hardly use. The sample text in the Chrome extension's tests went to English too. Left as they are: the name of a
service an agent built, and a test's translation into Dutch.

## What a box says about attachments (2026-11-03)

**Found by the user,** reading `Containers.provide`: it asked the WorkBox which attachments it lacked, then opened each path
that came back, on the machine, and piped it into the box. That answer comes from the box's own `sh`, and an agent is root
there. So an agent could name any file Relay's user can read (logins, `relay.db`, keys) and get it, in any chat with an
attachment. In the code since 2.9.1.

**The fix is theirs:** only paths that were asked about, `set(missing) & set(files)`.

**Tried on a throwaway box:** a replaced `/bin/sh` that also names a canary file outside the attachments. Before the fix the
canary was in the box; after it, only the attachment. `test_containers` holds the same case.

**The same mistake elsewhere?** A first look at the other `podman exec` calls whose output Relay uses: `boxes.py` compares
inode numbers, `webbox.py` and `vpn.py` read an answer over HTTP, none as a path on the machine. The exec gateway and the
exchange folders, where a box asks Relay for things, weren't part of this look.

## What a box may ask for (2026-11-03)

**The exec gateway took the folder from the box.** `Containers.place` refused only `/`, the home folder and what's above it,
and made a WorkBox of anything else, writable. What asks comes from the box, where an agent can get a program of its own
running: a project's `opencode.json` with a local MCP command is enough. So any folder of the machine was in reach.

**Without any program of its own:** opencode's bash tool has a `workdir`, and opencode sets no `RELAY_FOLDER`. A `workdir` in
the private scripts, an empty folder in the box, gave the WorkBox *of* that folder, where `_masks` shows them on purpose.

**Now:** `Boxes.projects`, what the box has writable of the workspace's and Relay's plus the folders its runs worked in, and
never `Containers.hidden`. Folders are compared resolved, so a link in a project doesn't lead out. Not on the list, as they
were before: `/tmp`, `uploads/` (read-only in the box, writable in a WorkBox of it) and a program's login.

**What a program sees:** "is no project folder", as it always did for the home folder.

**Tried on a throwaway Relay,** real boxes and opencode with a free model. A chat in a project and one without a project ran
their commands in their WorkBoxes. A model asked for `workdir` in the private scripts got the refusal. By hand through the
box's own shell: `RELAY_FOLDER` on a folder outside, the private scripts, `/tmp` and `uploads/` were refused, the project and
a subfolder of it ran. Each new test was seen to fail with its check taken out.

**Relay's shell on the machine** opened `exchange-boxes/<name>/<tag>-cwd`, which the WorkBox writes. A link there was followed
on the machine and a host file copied to `/tmp/claude-…-cwd`, and a pipe would have hung the shell. Now `O_NOFOLLOW`,
`O_NONBLOCK` and plain files only, and `hand_back` leaves anything else where it is, as the ProviderBox's shell would follow
it where the login is. `relay-shell` itself stays as it is: changing it means a new box image. Tried with real podman: a link
no longer brings the file over.

## What `.git` may say about a WorkBox (2026-11-03)

**`_project` asked git** (`rev-parse --show-toplevel --git-common-dir`) which folders a WorkBox gets, and `ensure` does that at
every command. The agent is root there and `.git` is writable. Tried from inside a WorkBox with plain commands:
- `git config core.worktree <folder>`: that folder of the machine was in the WorkBox at its next command, read-only. Just
  above the folder worked too, and from a repository inside another it gave the outer one's `.git` writable.
- a `.git` file naming a gitdir of its own, whose `commondir` is another repository's `.git`: that `.git` came in writable. A
  `.git` link, and a `commondir` inside a real `.git` folder, did the same.

**Now read from the files:** the nearest folder upward with a `.git` folder holding a `HEAD`, or a `.git` file; never a link,
and a pipe doesn't hang Relay (`ensure` holds its lock over this).
**History outside the folder** comes along only when it names the folder back from there: `<common>/worktrees/<name>/gitdir`
for a linked worktree, `core.worktree` for a submodule. An agent can't make those say so without writing there, and what it can
write already (the scratch folder) gains it nothing.

**What it costs:** `git init --separate-git-dir` leaves nothing that names the folder back, so its history can't be told from
a made-up one, and git doesn't work in its WorkBox. None of the user's folders is one.

**Checked:** the old and the new lookup gave the same on all 174 folders of the live install, so no WorkBox is made anew. On a
throwaway Relay with real boxes both ways in were gone, and a linked worktree still committed from its WorkBox. Taking each
check out made a test fail, or hang on a pipe; one that nothing needed (a gitdir inside the folder: `_overlaps` has it) went.

## What the containers are for (2026-11-03)

**The user's decision, for the first public release:** "Relay protects your system from accidental damage by agents; if an agent tries to escape its container on purpose, it will." It's the first line of SECURITY.md now, in the README, and
in the guide.

**Why it's said so plainly:** a look at what a box can get Relay to do found holes, and some were closed (the attachments, the exec
gateway, the cwd file, what `.git` decides about mounts). One known way stays: Relay runs git on the machine in a project's repository,
and `.git` is the agent's to write. Overriding the settings that run commands would be a list that git can outgrow, and running that git
in the WorkBox is a big change. Neither makes the containers a wall, so a claim of one would be false either way.

**What changed with it:** getting out of a container on purpose is out of scope for reports. What stays in scope is an agent's ordinary work
doing damage outside its project, which is what the containers and the guard are for.

