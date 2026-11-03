# How Relay is built

For developers, people and agents alike: the layers, what each module does, and a message end to end. How to work on
it (the agreements, the rules, the tests) is in [AGENTS.md](../AGENTS.md); what Relay does, in the
[guide](GUIDE.md).

Four layers, everything on the machine except the agent's commands:

```
browser ──HTTP/SSE──▶ Relay (app.py, routes/, runs.py, …)
                         │
                         ├─▶ standardised API        providers/base.py
                         │     │
                         │   support layer            providers/<name>/, opencode_manager/bridge/
                         │     │
                         │   provider, in its box     claude -p · opencode services · copilot --acp (boxes.py)
                         │     │                      (chat APIs: Relay's own tools)
                         │   the box's /bin/bash ──▶ exec socket ──▶ Relay (guard, then podman exec)
                         │     │
                         └─▶ containers.py ─────────▶ a WorkBox per project folder
```

**Providers** are the agents' own programs and services: Claude Code
(`claude -p`), opencode's service per opencode-manager profile, Copilot and
other ACP agents, OpenAI-style chat APIs.

**Support layer.** `opencode_manager/bridge/` speaks to them:
`claude_backend.py` drives `claude -p` in stream-json mode,
`opencode_backend.py` opencode's REST/SSE API, `events.py` turns what they
say into uniform events, `billing.py` tells who pays for a model.
`opencode_browser.py` stands in for opencode's desktop app, with a browser
in the container. `relay/providers/` has a folder per kind, its translator:
`claude/` and `opencode/` (through the bridge), `acp/` (Copilot and any ACP
agent), `chat/` (chat APIs, with Relay's own tool loop from `toolbox.py`),
`perplexity/`, `command/` (any program that prints uniform events) and
`example/` (the worked example of [ADDING-A-PROVIDER.md](ADDING-A-PROVIDER.md)).
Relay finds the folders when it starts; one that can't load is listed as
broken and the rest work. `conformance.py` checks a layer the way Relay runs it.

**Standardised API** (`providers/base.py`): a `Provider` lists models and
efforts, checks itself, starts runs and answers permission requests
(`judge()`); a `Context` is all a run gets (folder, mode, model, the shell,
notes, attachments); a `ProviderRun` yields uniform events — `text`,
`thinking`, `tool_call`, `tool_result`, `permission`, `usage`, `error`, and
finally `done`. Above this line Relay doesn't know one agent from another.

**Relay itself:**

| | |
|---|---|
| `app.py`, `routes/`, `web.py` | the `Relay` object and start-up; the HTTP API (conversations, runs, providers, settings, accounts, extras) |
| `runs.py` | runs in the background: the `Context`, the folder's container and shell, events stored and fanned out live (SSE), the queue |
| `db.py` | SQLite: conversations, messages, runs and their events, schedules, … |
| `delegation.py`, `tools_script.py` | Relay's tools (agents, delegate, wait, tell, the computer tools) as an MCP server or the `relay` script; `approve_permission` for Claude Code |
| `policy.py`, `health.py`, `limits.py`, `tunables.py` | which models subagents may use unasked, which failed lately, local-server room, Settings → Limits |
| `modes.py`, `skills.py`, `handover.py` | what an agent is told: modes, skills, the conversation handed to a fresh session |
| `git.py`, `tasks.py`, `checkpoints.py`, `forks.py`, `webdav.py` | code tasks: folder, branch or worktree; a record of every turn and `/rewind`; subagents' copies of the folder; the folder as a network drive |
| `forward.py` | servers on this machine's ports at `/port/<n>/` on Relay's own address (HTTP and WebSockets, signed in) |
| `boxes.py`, `containers.py`, `vpn.py`, `webbox.py`, `search.py`, `fetch.py`, `container/`, `guard.py` | ProviderBoxes and their exec gateway; the WorkBoxes, their images and shells, VPN tunnels (gluetun) for them, Relay's boxes for the web: its web search (SearXNG) and web fetch (`container/fetch.cjs`: a browser when it must), the browser for opencode (`container/browser.cjs`); the commands no agent may run |
| `agentos.py`, `production.py`, `ranking.py` | AgentOS: the Governor, its capabilities, the policy and audit trail; live services (ProductionBoxes), their versions, health checks and the proxy on their ports; the model ranking the Governor keeps up |
| `terminal.py`, `routes/terminal.py`, `static/js/terminal.js` | on-demand browser shells: authenticated WebSocket, PTY in the WorkBox (or a service's live ProductionBox), vendored xterm.js, resize and per-shell cleanup |
| `scheduler.py`, `schedules.py`, `fallback.py` | scheduled messages and tidying; going on after a usage limit |
| `computers.py`, `connector.py` | the user's own computers |
| `notify.py`, `uploads.py`, `usage.py`, `export.py`, `repair.py`, `creations.py` | notifications, attachments, cost, Markdown export, an agent repairing a provider, and the pages where a provider is added or repaired (a box with a shell, the agent's plan, Relay's check, a throwaway Relay) |
| `memory.py`, `timed.py`, `system.py` | Relay's memory for every agent; `/timed`, an agent kept at it for a while; Settings → System (the machine and Relay's boxes) |
| `sdk.py`, `sdk/python/relay_sdk/` | the Python SDK (its source), and the wheel Relay makes of it and serves for pip at `/sdk/` |
| `scripts.py`, `routes/scripts.py`, `builtin_skills/relay-sdk-template-writer/` | the Scripts page: your SDK scripts in `private/` (out of every box's sight) and `public/` (agents read them, the Governor runs them), their runs in their folder's WorkBox (a session of their own, their output as it comes, the conversations they start); the skill and its templates for an agent that writes one |
| `relay_tools.py` | Relay's own tools per conversation: which kinds (web, memory, subagents, computers) its agents get |
| `plugins.py`, `routes/plugins.py`, `static/js/plugins.js`, `plugins/notes/` | plugins ([WRITING-A-PLUGIN.md](WRITING-A-PLUGIN.md)): folders in `RELAY_HOME/plugins`, loaded at start (a broken one said); their routes at `/api/plugins/<name>/`, background tasks (the scheduler's tick), the hook for runs that end (`runs.py`), tools for agents (`delegation.py`, `relay plugin`); on the page, their buttons, pages (`/p/<name>/`) and Settings tabs; Notes, the example. The other plugins that come with Relay are in `plugins/`: see [FEATURES.md](FEATURES.md#plugins-that-come-with-relay) |
| `auth.py`, `config.py`, `relocation.py`, `doctor.py`, `backup.py`, `cli.py` | accounts (sessions, app passwords), settings, when the data or code moved, `relay doctor`, `relay backup`, the command line |

**The agent's environment**: the provider's program runs in its ProviderBox
(`boxes.py`), whose shells hand every command to Relay's exec gateway; Relay
checks the guard and that the folder is one the box has as a project, and runs it in the project's WorkBox
(`containers.py`): a warm podman container per project folder with the
folder (or its git repository and `.git`), `/tmp/opencode`, and Relay's
tools and skills read-only; the agent is root there. See
[Containers](GUIDE.md#containers).

**A message, end to end:** the browser posts it; `runs.py` builds the
`Context`, makes sure of the container and gives the agent Relay's shell and
note; the provider starts the agent through the support layer; the agent's
commands run in the container, and Relay answers its permission requests
(`judge()`); the uniform events go to the database and, live, to the browser;
afterwards a checkpoint commit, a notification, and the next queued message.

The front end (`src/relay/static`) is plain JavaScript modules without a build
step; markdown, sanitising and highlighting libraries are bundled in
`static/vendor/`, so Relay needs no internet access. `main.js` boots the app
and has the layout (the activity bar, the column's views), routing, header and new chats; `views.js` which
views there are and the column's width; `panels.js` the column's lists other than the chats'; `switcher.js` Ctrl+K;
`state.js` what
they all share (state, long-lived elements, loaded data); `conversation.js`
an open chat; `composer.js` the message box; `pickers.js` the pickers under
it; `commands.js` the slash commands; `render.js` the messages themselves;
`settings.js` the settings; `schedules.js` the AgentOS page; `service.js` a
live service's page (a shell in its box, Demote); `scripts.js` the Scripts
page; `terminal.js` the Shell panel (and the terminal both use); `ui.js` small building blocks. Smaller
ones are named for what they do: `api.js` (the API client), `markdown.js`, `changes.js` (a code task's changes
panel), `tasks.js`, `picker.js` (the folder picker), `attachments.js`, `forms.js` (an agent's questions),
`notifications.js`, `activity.js`, `tool-activity.js` and `outcomes.js` (how runs and their changes are shown),
`limits.js` (usage limits: the ring on the bar, the meters), `plugins.js` (plugins' parts of the page),
`thread-scroll.js`, `viewport.js` (a phone's keyboard), `theme.js`, `drive.js`, `search.js`, `vpn.js`,
`system.js`, `restart.js`, `base.js` (Relay under a path), `providerpage.js` (adding or repairing a provider) and
`error-page.js`.
