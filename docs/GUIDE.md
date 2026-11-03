# Relay's guide

Everything Relay does, and how to set it up: the reference behind the [README](../README.md), which has what it is
and how to install it. [FEATURES.md](FEATURES.md) lists it all on one page. How it's built is in
[ARCHITECTURE.md](ARCHITECTURE.md) and how to build it in [BUILDING.md](BUILDING.md); adding an agent Relay doesn't
know, in [ADDING-A-PROVIDER.md](ADDING-A-PROVIDER.md); writing a plugin, in [WRITING-A-PLUGIN.md](WRITING-A-PLUGIN.md).

**Where things are.** Relay's folder (this repository) holds the code (and its Python
dependencies, in `vendor/`). Relay's data — chats, settings, providers,
uploads, task worktrees and checkpoints, the database — is in
`~/.local/share/relay` (`RELAY_HOME` picks another place), with
opencode-manager's profiles (opencode's logins) in `opencode-manager/` there.
The agents (`claude`, `opencode`, `copilot`) come from the machine's PATH.
The data holds logins and API keys: Relay makes the folder yours alone (`chmod 700`), and
`./relay doctor` warns when other users of the machine can read it.

## Contents

- [Running Relay](#running-relay)
- [Its own Claude Code login](#its-own-claude-code-login)
- [On your phone's home screen](#on-your-phones-home-screen)
- [Your workspace](#your-workspace)
- [Providers](#providers)
  - [Tools for chat APIs](#tools-for-chat-apis)
  - [GitHub Copilot and other ACP agents](#github-copilot-and-other-acp-agents)
  - [Perplexity, with your subscription](#perplexity-with-your-subscription)
  - [Repairing a provider](#repairing-a-provider)
  - [A provider Relay doesn't know, added with an agent](#a-provider-relay-doesnt-know-added-with-an-agent)
  - [Which models work](#which-models-work)
  - [When a model stops: fallback loops and going on by itself](#when-a-model-stops-fallback-loops-and-going-on-by-itself)
  - [What a message costs](#what-a-message-costs)
  - [Adding any agent with command](#adding-any-agent-with-command)
- [Edit, fork, switch, retry](#edit-fork-switch-retry)
- [Commands](#commands)
- [Questions](#questions)
- [Queue](#queue)
- [Temporary chats](#temporary-chats)
- [Modes](#modes)
- [Subagents](#subagents)
- [Skills](#skills)
- [Memory](#memory)
- [Your own computers](#your-own-computers)
- [AgentOS](#agentos)
  - [Services](#services)
  - [The Governor's settings](#the-governors-settings)
  - [Model ranking](#model-ranking)
  - [What holds the Governor](#what-holds-the-governor)
  - [Leaving AgentOS out](#leaving-agentos-out)
- [Attachments](#attachments)
- [Notifications](#notifications)
- [Chat and code modes](#chat-and-code-modes)
  - [Every turn is recorded; commits are yours](#every-turn-is-recorded-commits-are-yours)
  - [Open a task's folder on your computer](#open-a-tasks-folder-on-your-computer)
- [Containers](#containers)
  - [VPNs for WorkBoxes (gluetun)](#vpns-for-workboxes-gluetun)
  - [Web search and fetch (SearXNG and a browser)](#web-search-and-fetch-searxng-and-a-browser)
  - [Servers on this machine's ports: /port/<n>/](#servers-on-this-machines-ports-portn)
  - [Relay under a path of your site](#relay-under-a-path-of-your-site)
- [Security](#security)
- [Plugins](#plugins)
- [Python SDK](#python-sdk)
  - [App passwords](#app-passwords)
  - [RESL](#resl)
  - [Scripts](#scripts)
- [Settings](#settings)
  - [System](#system)
  - [Limits](#limits)

## Running Relay

Installing it is in the [README](../README.md#install). Once it runs:

**Upgrading:** `git pull` (Relay brings its Python dependencies up to date by
itself when they changed; installed from a bundle, a newer bundle's
`install.sh` instead), then `systemctl --user restart relay` (or Settings →
Limits → Restart Relay, which refuses while your own agents are working; AgentOS's go on by
themselves after it). `systemctl` doesn't wait: your agents' answers stop there, each
saying so, with **Retry**. Relay migrates its database and
moves stored paths by itself at startup. **Backups:** `./relay backup`
writes one archive of all of Relay's data: a consistent copy of the database,
and each service's data as `volumes/<volume>.tar`, copied with its box paused
for that moment (it doesn't answer meanwhile, and its health check leaves it
be). To restore, unpack it with `tar -xzf` into an empty
`~/.local/share/relay` and start Relay. A service whose data volume isn't
there gets it back from `volumes/` when its box is made; if that fails, it
isn't started on empty data, and the next start tries again.

`./relay doctor` checks everything Relay needs — Python and dependencies, the
data folder, git, workspace roots, the port, and each enabled provider (is
`claude` installed, is the opencode service up, is the API reachable) — and
is the first thing to run when something doesn't work. It also reports boxes
another Relay left behind (a test run that was stopped before it tidied up) and
gives the command to remove them.

Useful commands:

```sh
./relay doctor                        # check this machine and the providers
./relay doctor --no-providers         # … only the machine
./relay doctor --port P               # … with this port instead of the one set
./relay doctor --build                # … and build the containers' images now
./relay backup [FILE]                 # all of Relay's data in one archive
./relay serve [--host H] [--port P]   # run in the foreground
./relay port [N]                      # the port Relay serves on; with a number, serve on that one from its next start
./relay user add NAME                 # create an account
./relay user password NAME            # reset a password (signs out all sessions)
./relay providers                     # list providers and broken templates
./relay provider-check ID [--model M] # check one provider fully, as Settings → Providers does
./relay skills                        # list Relay's skills and where to add your own
./relay plugins [on|off|check] [NAME] # list the plugins, turn one on or off, or check a folder
./relay claude [ARGS]                 # Claude Code with Relay's own login (below)
./relay systemd-unit [--user] [--port P]  # print the service unit scripts/install-relay.sh installs
./relay --version                     # Relay's version
journalctl -u relay -f                # logs of the service (--user for a user service)
```

**Behind a reverse proxy on another machine,** Relay doesn't believe the proxy's `X-Forwarded-*` headers until you
name it: `RELAY_FORWARDED_ALLOW_IPS=<its address>` in the service's environment (default `127.0.0.1`). The connector
of [your own computers](#your-own-computers) and links in notifications don't depend on it: they use the browser's
address, or `public_url` in [Settings](#settings).

### Removing Relay

`sh scripts/install-relay.sh --uninstall --user` (without `--user` for a system service) removes the service, and
nothing else: Relay's containers keep running, and its data, images and this folder stay. To remove those too, as
the user it ran as:

```sh
podman ps -aq --filter label=io.relay.managed | xargs -r podman rm -f     # its containers
podman volume ls -q | grep '^relay-' | xargs -r podman volume rm          # what agents installed, services' data
podman images -q 'localhost/relay-*' | xargs -r podman rmi -f             # its images, about 3.7 GB
podman image prune -af                                                    # what they were built from, if unused
loginctl disable-linger
podman unshare rm -rf ~/.local/share/relay ~/.cache/opencode-manager      # your chats, settings and so on: all gone
rm -rf ~/relay
```

`./relay backup` first, if you may want any of it back. Your project folders stay, and so do the agents you installed
(opencode, Claude Code…): each has its own way out.

## Its own Claude Code login

```sh
./relay claude        # Claude Code with a login of Relay's own: type /login once
```

Until then, Relay uses the machine's own `~/.claude`. With a login of its own
(in Relay's data folder) Relay's Claude Code runs apart from yours. When the
login it uses runs out ("OAuth session expired"), Settings → Providers → Claude
Code's **Shell** signs it in again: `claude auth login`. Relay
doesn't copy an existing login: Claude renews its tokens, and two copies would
sign each other out. A provider can point elsewhere with the `config_dir`
option (`"host"` for `~/.claude`).

**Moving.** When Relay's data or this folder turns up somewhere else, Relay
rewrites the paths it stored (chats, uploads, tasks, workspace roots) at its
next start and repairs the git worktrees of code tasks. opencode sessions
follow a task to its new folder, and Claude Code's session folders
(`~/.claude/projects/…`) are moved along, so moved chats and tasks go on
where they were.

## On your phone's home screen

Open Relay in the phone's browser and choose **Add to Home Screen** (Safari:
the share menu; Chrome: the ⋮ menu). It then opens full screen with its own
icon, like an app. On plain `http://` in the local network that's a
shortcut that opens full screen; browsers only treat it as a fully installed
app (and allow system notifications) over HTTPS — for example through
Tailscale (`tailscale serve`) or a reverse proxy such as Caddy. The icons are
made with `scripts/make-icons.py`.

## Your workspace

Relay's window is laid out as VS Code's:

- **The activity bar,** on the left: **Chats**, **Tasks**, **AgentOS**,
  **Governor**, **Scripts** and **Production boxes**. At the bottom are the
  **Shell**, the **usage ring**, **Settings** and you (your account and the
  theme).
  - **The usage ring** shows what's left of your tightest usage limit: the
    window with the least left across all your providers. It's green, amber
    under 20% and red when that window is used up; hovering it names the
    window and when it resets.
  - **A click on it** shows every provider's windows, as
    [Settings → Usage](#what-a-message-costs) has them, with **Refresh**.
    Relay looks again every few minutes, when you come back to the tab and
    when a run ends.
  - It isn't there while none of your providers tells its limits.
- **One column** next to it shows the view you chose. The views replace each
  other there, and the chat or page on the right stays as it is.
  - Each view has a header with its actions, a search box for its list, and
    its groups with how many are in each.
  - **Its width is yours:** drag its edge (or use the arrow keys there; a
    double click gives the usual width). It's the same in every view and
    remembered in this browser.
  - **Folding it away:** click the open view's icon (or the header's button).
    Any icon brings it back.
  - **The view** is remembered too. Relay opened on a page starts in that
    page's view (a script's: Scripts).
- **The main area:** the chat or page you opened, with the **Shell** as a panel
  under it.

| View | Its list | A click opens |
|---|---|---|
| **Chats** | Waiting for you, Working, pinned, then by date. New conversation and New computer task are in its header. | the chat |
| **Tasks** | Waiting for you, Working, Needs review, Up to date, Discarded (folded). Its header opens the Tasks page with their changes. | the task's chat |
| **AgentOS** | Overview (its dashboard), Needs you, then its tasks by kind; New task in the header | the dashboard, or a task's form (`/agentos?task=<id>`) |
| **Governor** | the Governor's conversation, then the chats AgentOS made | the chat |
| **Scripts** | Running, Private, Public, Examples. **+** has New script, From a template and With an agent; **⋯** has Open in VS Code, Documentation and Change the folder. | the script (`/scripts/<its name>`) |
| **Production boxes** | the services, each with how it's doing; New service in the header | the service's page |

**A mark by a chat's icon,** in every list, says who started it when it
wasn't you in Relay's pages:
- **Green, with "os":** made by AgentOS's Governor, which may see, change and
  delete it ([AgentOS](#agentos)).
- **Yellow, with an "E":** started outside Relay's pages, by a script or
  program through the API: the [Python SDK](#python-sdk) (on another machine,
  or a script on the Scripts page) or any other client. Its tooltip says who,
  e.g. "the Python SDK, with the app password “backup script”" or "the script
  public/report.py".
- **Neither:** you started it in Relay's pages, or Relay did (a scheduled
  task, a repair). Chats made before Relay kept this have none.

**Ctrl+Shift+O** starts a new conversation.

**Ctrl+K** (⌘K on a Mac) opens a quick switcher to any chat, from anywhere.
- **As you type,** it shows the chats whose title has it: titles that start
  with it come first, newest first.
- **Then the server's search** adds the chats that have it in what was said.
- **Using it:** arrows and Enter open one; Esc closes it.
- **The column** stays in its view.

**The Shell** (the bar's icon, the **Shell** button under the message box, or
`/shell`) is a panel under the chat, in the chat's WorkBox.
- **It follows the chat you open:** that chat's WorkBox. On a page, it says
  where a shell is.
- **Its height:** drag its top edge; remembered.
- **A chat's shell is kept:** closing the panel, opening another chat or
  reloading the page leaves it running. Opened again, it's the same shell,
  with its variables, its folder and what it showed (its last 256 KB,
  wrapped anew for the window it's opened in). It ends:
  - with `exit` or **End shell**, after which **New shell** starts a fresh one;
  - after 3 hours that nobody opened it (as long as an unused WorkBox runs);
  - when Relay restarts, the chat is deleted or its task discarded.
  Another window that opens it takes it over. A service's Shell tab still ends
  with its page.
- **On a phone,** it's half the screen.

The Chats view puts **Waiting for you** first, then **Working**, pinned chats and
dated history. Text labels distinguish work, unanswered requests and failed
runs without relying on a coloured dot. The conversation header uses the same
status language; click **Waiting for you** to jump to and focus the open question
or approval. **Tasks** separates waiting agents from running ones, with **Answer**
and **Review changes** actions for work that needs you. Its count on the bar
includes working agents, unanswered requests and tasks needing review, with a
breakdown on hover; shared project changes count once. Search does not filter
that count. AgentOS's icon has a dot while its Governor works.

A service's WorkBoxes ("notes: work on version 5", from Demote or the
Governor's `workbox.clone`) are listed once: the newest, with an arrow and the
number of older ones, which open under it. That covers every chat working in
a clone's folder: a fork of it, and a reviewer the Governor put in it.
- One that's working or waiting for you, and the one you have open, stays in
  its own place.
- A search shows them all.
- Whether they're open is remembered per service, in this browser.

The message box keeps provider, model and project/files controls within reach.
**Options** holds the mode and fallback loop; a non-default mode stays visible in
its label even when closed. Changing these controls preserves your typed message
and attachments, including after settings refresh the available choices on the
welcome screen. Settings updates leave Tasks and AgentOS on their own routes.
Unchanged list polls keep their controls; changed lists retain keyboard focus
and scroll position. Menus support arrow keys and return focus when closed;
dialogs keep Tab inside and restore focus to their opener. Slash suggestions
leave focus in the message box so typing and completion still work.
Tool details remain collapsible, while questions stay open.

**Shift+Tab** in the message box cycles through the provider's available modes
(Build, Plan, Read-only, and others) in the mode picker's order, including custom
modes. It wraps back to the first; chat providers also include No mode. Your
draft, caret, uploads and Options stay intact. Outside the message box (or with
fewer than two choices), Shift+Tab retains normal backwards focus navigation.
Changing mode during a run affects subsequent turns, not the running agent.

Consecutive tool calls share a compact **Activity** block with a factual summary
(files read, searches, test runs, commands). The current block opens while the
agent works and collapses when it moves on or stops, like Thinking → Thought.
It stays open between consecutive calls; manual toggles remain available.
Expand completed activity for the individual tools;
**Show details** expands both the block and their input/output. Failed tools,
provider errors and questions stay outside collapsed activity.

Completed thinking blocks show **Thought for 12s** (or **Thought for 1m 08s**).
Relay measures from the first reasoning output until the next visible phase;
the stored duration stays the same after reopening or reconnecting. Older
blocks without timing show **Thought** rather than an invented duration.
Thinking and Activity disclosures use short, smooth transitions in supported
browsers and switch instantly with reduced motion enabled. Reasoning output is
batched per animation frame without losing the final streamed text.

Menus, dialogs, notifications and view/category switches appear with subtle
animations. On a phone, the bar and the column are a drawer (the header's
button) that slides in and out with a fading backdrop;
buttons and focus styles change smoothly. Options, Relay notes and retry
details share the same disclosure transitions. Actions, focus and closing
menus/dialogs never wait for an animation. Streamed messages and polling rows
stay still, and the browser's reduced-motion preference applies throughout.

Individual tool cards also open/close their Input/Output smoothly. New result
sections and success/failure icons fade gently; command/output text never moves
or repeatedly animates. Failures still open their details immediately, while
closed details leave the keyboard tab order as soon as they start collapsing.

Tool headers prefer the agent's existing description over a long command, while
retaining the native tool name and full Input/Output. They show **Running**,
**Completed**, **Failed**, or **Stopped**, with a short reported diagnostic below
failed calls. Narrow cards put the status on its own line. Replayed calls have
the same labels, and duplicate results do not reset output selection or flash
the status icon again. No extra model call is needed to make these summaries.

When you read back, new output leaves your reading position alone. **Latest ↓**
returns to the bottom and resumes following; it says **New activity** when more
output has arrived. Open subagent streams follow the same rule. Each subagent
keeps its own card with its task, provider/model and readable status. Questions
appear in the parent conversation even with the card closed; failures also stay
visible. Opening a card follows its full stream, closing it releases the
connection. **Show details** never opens subagent conversations automatically.

**Review changes** is named in the header, task overview and answers that changed
files. The review panel distinguishes files **Already in your project** from work
**On a separate branch** that still needs merging. Opening review never applies
or commits anything; the existing review, commit, merge and rewind actions stay
explicit. An answer's counts describe that turn; review opens the current files.

Failed or stopped answers show **Retry** and **Choose another model** beside the
error or stop note, with the actual automatic-resume/fallback plan. Starting now
replaces a scheduled attempt, without sending or losing your composer draft,
attachments or open Options. Recovery prevents duplicate clicks, and a fast
completed attempt appears only once. Answer actions stay visible on desktop and
phones in the same order: **Copy**, **Regenerate** when supported, then **More**
for fork, Markdown export and per-answer file rewind.

Recovery has one set of controls at the latest failed/stopped answer, not a
second bar below the conversation. An earlier model's failure does not describe
the newer model's stop. New Relay-generated fallback/continuation instructions
appear as compact, expandable Relay notes rather than editable user bubbles;
their full text still goes to the model. Provider/model transitions keep
technical details collapsed. Historical messages without a provenance label
are left as they were, rather than guessing their author from their text.

## Providers

Providers are JSON templates in `~/.local/share/relay/providers/<id>.json`. Manage them
in **Settings → Providers** (add, edit, enable/disable, test, delete) or edit
the files directly. A fresh install gets **Claude Code** and **opencode**.

```json
{
  "id": "openrouter",
  "name": "OpenRouter",
  "kind": "openai-compatible",
  "enabled": true,
  "options": {
    "base_url": "https://openrouter.ai/api/v1",
    "api_key": "sk-or-…",
    "models": ["anthropic/claude-sonnet-4", "openai/gpt-5"]
  }
}
```

| Kind | What it runs | Tools | Options |
|------|--------------|-------|---------|
| `claude-code` | `claude -p` with your Claude login | yes | `binary`, `models`, `config_dir`, `thinking_display` |
| `opencode` | opencode through an opencode-manager profile — every provider connected there works (ChatGPT account, OpenRouter, local models). Without `profile` it follows the profile that is active in opencode-manager | yes | `profile`, `models` |
| `copilot` | The GitHub Copilot CLI (`copilot --acp`) with your Copilot subscription; sign in once with `copilot login` | yes | `binary`, `models`, `env` |
| `openai-compatible` | Any OpenAI-style chat API: OpenRouter, Ollama, LM Studio, llama.cpp, vLLM, OpenAI | Relay's | `base_url` (required), `api_key` or `api_key_env`, `models`, `system_prompt`, `headers`, `tools` |
| `perplexity` | Your perplexity.ai account (Pro included, no API key): the site in a headless browser in a box of its own, signed in with your session cookie | Relay's | `cookie`, `models`, `tools` |
| `acp` | Any agent that speaks the [Agent Client Protocol](https://agentclientprotocol.com) over stdio, e.g. `gemini --experimental-acp` | yes | `command` (required), `models`, `model_argument`, `effort_argument`, `efforts`, `env` |
| `command` | Any program that prints Relay's uniform events as JSON lines | yes | `command` (required), `resume_command`, `prompt_via_stdin`, `env`, `models` |

Without any login, opencode offers free models through OpenCode Zen (for
example `opencode/big-pickle`); they show up in the model picker of an
`opencode` provider, so you can try Relay before connecting an account.

To pin a provider to one profile (for example one for your ChatGPT account
and one for local models), use the detected profiles listed under
**Settings → Providers**. The model picker lists the profile's models and
names its default model, e.g. "Default · GPT-5.5".

**To connect more to a profile** (OpenRouter, OpenAI, …), open its **Shell** in
Settings → Providers, run `opencode` and use `/connect`. It goes to that profile
only, and the model picker lists its models at once. A login that calls back to
a browser on `localhost` can't reach Relay's box from another computer: take the
login's headless option, or an API key.

The UI shows each kind's options and an example when you add a provider.
API keys are stored in the provider's file (readable only by you) and are
never sent back to the browser.

**Adding one takes two steps.** The first page has its template, and **Next
step** says there what's wrong with it (an id that's taken, say). A kind with a
box of its own then gets that box, made from the template, with a shell in it to
log in as usual. Relay sends a test message, which you can repeat with **Check
again**. Once that works, **Add provider** adds it at once; before that it asks
first. It isn't added until then: leave without it and its box goes, unless a
provider that's added uses that box. You can also log in later, with its **Shell**
in Settings → Providers. A kind without a box (a chat API) has only the test
message, which goes to its API on your key.

**A new opencode profile:** name it in the template (`"profile": "work"`), and
Next step makes it, so you can connect providers to it in that shell. It stays if
you don't add the provider, and is then offered under Settings → Providers.

**A provider Relay doesn't know:** [docs/ADDING-A-PROVIDER.md](ADDING-A-PROVIDER.md)
(also in Settings → Providers → Add provider). It covers:
- **whether a kind fits already**, which often needs no code;
- **if none does, its compatibility layer:** a folder of its own,
  `src/relay/providers/<name>/`, with everything for it. Relay finds the
  folder when it starts, and a mistake in it never keeps Relay from starting;
- **a worked example:** `toy-agent` (`providers/example/`);
- **the check:** Settings → Providers → a provider's shield (**Check it
  fully**), or `./relay provider-check <id>`. It runs the provider as Relay
  does, through a few short prompts, and checks every event that comes back;
- **an agent that writes one:** the built-in skill `relay-provider-writer`,
  for an agent working in Relay's folder.

Agents work in [containers](#containers): their commands run in a podman
container of the project folder, so nobody is asked anything. Relay answers
the agents' permission requests itself: commands are fine except the few no
agent may run (`rm -rf /` and the like, see `relay/guard.py`), files change
only in the project folder, and reading is fine anywhere. What Relay refuses
shows in the answer with the reason. Claude Code's requests reach Relay
through its MCP server (`--permission-prompt-tool`), opencode's and ACP
agents' natively.

### Tools for chat APIs

A chat API has little more than a response stream, so Relay runs the tools
itself (OpenAI-style function calling) and loops until the model answers:

| Tool | What it does | Where |
|------|--------------|-------|
| `list_files`, `read_file`, `search` | Look around the project, or a chat's scratch folder | code tasks and chats |
| `write_file`, `edit_file`, `run_command` | Change files, run tests or builds (2 min, output capped; in the WorkBox) | code tasks and chats |
| `web_search` | Search the web with Relay's own SearXNG ([Web search](#web-search-and-fetch-searxng-and-a-browser)) | when it's on |
| `web_fetch` | A page as text, in a browser when it must ([Web fetch](#web-search-and-fetch-searxng-and-a-browser)) | always |
| `list_skills`, `read_skill` | [Skills](#skills) | always |
| `relay_agents`, `delegate`, `wait`, `tell` | [Subagents](#subagents) | always, not for subagents |
| `merge` | A subagent's changes that weren't applied ([Subagents](#subagents)) | code tasks, not for subagents |

Changes are confined to the project folder (a chat's: its scratch folder), and
commands run in its WorkBox; nobody is asked.
- **Where the model is:** it's told that this is a Debian container on
  Relay's server, not the user's computer. Your own computers (`computer_*`
  tools) are for what you want done on them.
- **A computer task** gets no folder of its own: its work is on that
  computer.
Every step shows up in the answer as a tool call, and the tokens of all
rounds count toward the message. A server that doesn't do function calling
gets plain chat (Relay notices and remembers); `"tools": false` turns the
tools off for a provider. Small models (3B) tend to fumble tools; a 7B+
model with tool support (Qwen, Llama 3.1+, Mistral) works well.

### GitHub Copilot and other ACP agents

GitHub Copilot's CLI runs as an ACP agent: Relay starts `copilot --acp` for
each answer (with `--model` and `--reasoning-effort` as chosen) and continues
its session. Install it (`npm install -g @github/copilot`) and sign in once
with `copilot login`. Copilot asks
before it runs a tool, and Relay answers like for any agent: its commands
run in the container, its file changes stay in the project. A usage limit shows like any other. To use
your own model provider instead of Copilot's, set Copilot's
`COPILOT_PROVIDER_BASE_URL` (and friends) in the provider's `env`.

Other agents that speak ACP are added with kind `acp` and their command.

### Perplexity, with your subscription

Perplexity has no API for a Pro subscription, so Relay asks the site itself.
Its ProviderBox is of the WorkBox image, for its Chromium. Each question
opens perplexity.ai there, headless, signed in with your session cookie, and
asks the site's own endpoint from the page (`providers/perplexity/perplexity.cjs`).
- **What you see:** the search shows as a tool card with its queries and
  sources, the answer streams in with its sources under it, and a follow-up
  goes on in the same Perplexity thread.
- **The cookie:** in a browser signed in to perplexity.ai, DevTools →
  Application → Cookies → `__Secure-next-auth.session-token`. Its value goes
  in the provider's `cookie` (Settings → Providers). Without one, it asks as
  a visitor. Relay keeps the browser's profile in
  `~/.local/share/relay/perplexity/<id>`.
- **Models:** `models` lists the site's own model ids, such as `pplx_pro`
  for *Best* or `gemini38flash` for *Gemini 3.8 Flash*.
  - **Finding an id:** the site shows only names, and Relay can't read the
    list (Cloudflare checks those requests from a headless browser). On
    perplexity.ai, choose the model and ask something; then look in
    DevTools → Network → `perplexity_ask` → Payload → `model_preference`.
  - **A model it doesn't take:** Relay says so (`INVALID_MODEL_SELECTION`).
- **As an agent** (`tools`, on by default) it gets Relay's tools: files and
  commands in its WorkBox (a project's, or a chat's scratch folder), the web,
  skills, subagents.
  - The site does no function calling, so Perplexity asks for an action with
    a ```` ```tool ```` block of JSON. Relay runs it and sends back the
    result in the same thread, until Perplexity answers without one.
  - Code tasks work too.
  - *Best* and GPT-5 there work this way; Claude through Perplexity declines.
  - Each round is one Perplexity answer, so a step takes some seconds.
- **Mind:** this follows the site as it is now. When Perplexity changes its
  page or endpoint, the provider needs a repair (see below).

### Repairing a provider

A provider can break when the program behind it changes (a new Claude Code
or opencode version with other output or flags). Another agent can repair
Relay's support for it, on a page of its own, as [adding a
provider](#a-provider-relay-doesnt-know-added-with-an-agent) goes:
**Settings → Providers → 🔧** (Repair). Three steps:

1. **What's wrong.**
   - **What Relay sees:** its check, its models' statuses and its latest
     errors.
   - **A provider with a home of its own** (added with an agent) has a shell
     in a box with that home: an old version of its program or a lost login
     is fixed there, by you, without a repair.
   - **Claude Code and Copilot** have a shell with their program and the
     machine's login, as their box has them: a lost login is signed in again
     there (`claude auth login`, `copilot login`), without a repair. The
     agent's `repair_run` can't do that: it has no terminal.
   - **Then:** say what goes wrong if you know, and choose who repairs it
     (another provider with tools, and its model).
2. **The plan.**
   - **Where the agent works:** in a folder of its own, with copies of just
     that provider's **support layer** (its folder, `relay/providers/<name>/`,
     and for Claude Code and opencode their bridge backend, e.g.
     `opencode_manager/bridge/claude_backend.py`), a read-only copy of all of
     Relay's source, and a `REPAIR.md` with what's known.
   - **Its tools:** its commands run in that folder's WorkBox, which has
     neither the program nor Relay, so Relay offers it its own (MCP, or
     `relay repair` in a shell):
     - `repair_run`: a command in the provider's own box, with its login (its
       `--help`, a tiny prompt to see what it outputs now);
     - `repair_check`: its version of the files in a copy of Relay, in a box
       of its own: the provider's check and a tiny prompt;
     - `provider_plan`: its plan.
   - **Its plan, for you:** **Approve**, **Redraft** (say what should be
     different) or **Deny**.
3. **Finalisation.**
   - **The repair:** the agent makes it and tests it with `repair_check`.
   - **Relay checks it fully** once the agent is done, in a test box: a
     turn, going on, stopping, a command, with the provider's own template.
   - **A throwaway Relay:** the provider as it is, with the repaired files, at
     `/trial/<id>/`. Try it there; **Something's wrong** goes back to the
     agent.
   - **Apply:** installs that version, keeping the originals, and restarts
     Relay. **Undo** restores the originals, only if the applied files haven't
     changed since.

**What's kept apart:**
- **The test box** runs the agent's files: their code runs nowhere else.
  - It has the copy of Relay, the provider's own template (not your others,
    with their keys), and what the provider's `box_mounts()` names.
  - That's asked first, in a box too, and checked: a program read-only from
    anywhere, a writable folder only in your home folder or Relay's data.
    Never your keys and credentials (`~/.ssh`, `~/.gnupg`, `~/.aws`, …),
    Relay's database, settings, templates or code, or your workspace roots.
- **Relay's own source is never taken from the repair.** Changes to it are
  reported and left alone. Discarding never restores or deletes Relay's
  source files: modification times can't tell the agent's work apart from
  your own edits.
- **Its conversation** isn't under Tasks' *Needs review*: its page is where
  its changes are seen and taken.

**With AgentOS, its Governor starts it.**
- **Relay notices:** a provider's last three runs all failed, and none of
  them on a usage limit or something that passes (an outage, a rate limit, a
  timeout). Relay then wakes the Governor with their errors.
- **The Governor decides** whether it's Relay's support layer that broke. If
  so, it has another agent repair it (`provider.repair`): it starts at the
  plan, in a task it follows.
- **The rest is yours:** a Needs-you item opens its page, where you approve
  its plan, try the repair and apply it. Relay checks it fully itself.
- **Without AgentOS** you get the Needs-you item instead, once until the
  provider works again, with its repair page.

**A lost login isn't a repair.** When those three failures all say the login
is gone ("Failed to authenticate", "OAuth session expired", "not signed in", a
401), the Governor isn't woken: you get a Needs-you item, "… isn't signed in
any more", whichever way. Its link opens the provider's repair page, which then
starts with its shell, to sign in again (`claude auth login`), and **Test
again**; a repair is still there below, for when it isn't the login. Claude
Code's error in the chat says where to sign in again too.

### A provider Relay doesn't know, added with an agent

For a program no kind of Relay's runs (Gemini CLI, say), an agent of yours adds
it with you, on a page of its own: **Settings → Providers → Add provider → A
provider Relay doesn't know → Add it with an agent**. AgentOS's Governor sends
you there too when you ask it for such a provider. Three steps:

1. **Install it and log in, in a box of its own.** The page has a shell in a
   new ProviderBox: Relay's box image (Debian with Node.js 22, npm, Python
   and curl), with real shells, and a home of its own
   (`~/.local/share/relay/provider-homes/<id>`).
   - **Install the program there** (`npm i -g …`, `pip install --user …`, or
     its own installer) and log in. What lands in the home stays; what's
     installed elsewhere in the box (`apt-get`) doesn't.
   - **A login that opens a browser:** most programs offer a link to open
     elsewhere and a code to paste back.
   - **Then say what you installed,** and choose the agent (and its model)
     that integrates it.
2. **Integration.**
   - **The agent studies the program** in that box (`repair_run`: the program
     and your login, as you left them).
   - **It hands in a plan:** how Relay will run it, what it prints, sessions,
     models, permissions.
   - **Your answer:** **Approve**, **Redraft** (say what should be different)
     or **Deny** (everything goes).
3. **Finalisation.**
   - **The agent builds it:** its support layer, a folder
     `relay/providers/<name>/` by the [guide](ADDING-A-PROVIDER.md), tested
     with `repair_check` until it passes.
   - **Relay checks it** fully in a test box (a turn, going on, stopping, a
     command).
   - **A throwaway Relay with it:** a Relay of its own, with only the new
     provider, at `<your Relay's address>/trial/<id>/`, signed in as you.
     Try it there.
   - **Something's wrong?** Tell the agent; after its fix the throwaway Relay
     starts again.
   - **When it works: Add.** The throwaway Relay goes. The folder goes into
     `src/relay/providers/<name>/`, and its provider, with the home, into
     your providers. Relay restarts to load it, once your own agents are
     done.
   - **Take it out again** removes the folder, the provider and its home.

**After Add,** the home is the provider's: Relay mounts it as its box's
`HOME`, with the program and your login. A login that expired: Settings →
Providers → the provider's **Shell** opens a shell in a box with that home, to
log in again. That box goes when its shell does.

**Following it:** the agent's conversation is a code task, *New provider*,
which links back to the page; Add provider's page lists the ones going on.
Its changes are taken on the page (Add), so it's never under Tasks' *Needs
review*.
A new provider made before this page (the old way) can only be discarded
there.

### Which models work

A model can stop working for a while: a usage limit, a login that expired,
a model that was retired. Every answer notes whether its model worked, and
**Settings → Providers → Models** (or *All models and tests…* in the model
picker) lists each model with a green or red dot, when it last worked or
failed, and why. **Test** sends a model a tiny prompt ("Reply with just the
word OK") to find out now, as a chat would: in the provider's box, with its
login, and its commands in a WorkBox. *Test all* does the whole list. The
model picker shows the same dots (hover for the reason), and the provider
picker marks a provider whose latest model failed.

### When a model stops: fallback loops and going on by itself

When an answer stops on a usage limit (a plan's five-hour or weekly window,
an API's rate limit), or fails without doing anything at all, Relay keeps
the work going, for chats and code tasks alike (opt-out):

1. **Fallback loops** (Settings → Usage): named lists of agents in the
   order they take over, as many as you like, e.g. *Opus (Claude Code) →
   GPT-5.6 Sol (opencode) → GPT-6 Astra (Copilot)*, or *Sonnet → Terra*. A
   chat follows the first loop its model is in; `/loop` (or the chip under
   the message box) picks another loop for it, or none. When its model
   stops, the next agent of the loop takes over right away, with the
   conversation handed over (round the loop: after the last comes the
   first), skipping the ones that stopped this round, are turned off, or are
   limited themselves.
2. When **none of the loop's agents can go on**, the round is over: Relay
   waits for the first of their limits to reset (or 5 hours when nobody
   says), then goes round the loop again from its first agent. After 5
   rounds (Settings → Limits, *Rounds of a fallback loop*) it gives up and
   says so. Your old list of fallback agents became a loop of its own.
3. A chat whose model is in no loop (or with `/loop off`) **goes on when its
   limit resets** (a minute after): Claude Code reports its windows with
   every turn, chat APIs send `Retry-After` with an HTTP 429, and messages
   like "try again in 2 hours" or "resets 3pm" are read. When nobody says
   when, it tries once after 5 hours. When that doesn't work either, Relay
   gives up and says so.

The banner says what Relay will do and when, with **Cancel**; the chat list
shows "⚠ resumes at 15:00".

**Continue with another model** in the banner hands the conversation to a
model you pick, right away. Sending a message yourself replaces a planned
resume.

### What a message costs

Under each answer Relay shows the model and tokens, and who pays for them:

| Label | Meaning | Detected from |
|-------|---------|---------------|
| **$0.02 API** | Pay per use: charged to an API account | Claude Code with an API key; opencode providers connected with a key (OpenRouter, paid Zen models); chat APIs on the internet (the amount only where the server reports it, like OpenRouter) |
| **in plan** | Covered by a subscription (Claude Pro/Max, ChatGPT Plus/Pro): no extra cost, but it counts toward the plan's usage limits. Hover it for what the tokens would have cost at API prices | Claude Code logged in with a claude.ai account; opencode providers connected by signing in (OAuth) |
| **free** | Local or free models: nothing is charged | Servers on this machine or the local network; models opencode lists with a price of 0 |


**Settings → Usage** adds it up for the last 7, 30 or 90 days, subagents
included: API costs (what you're charged), runs covered by a plan (with what
they would cost at API prices) and free runs, per provider and model, with
the runs per day.

Above that, **Usage limits** shows what's left of each plan, one row per login:

| Provider | Shown | Asked from |
|---|---|---|
| Claude Code (Pro/Max) | the 5-hour and weekly windows, % left and when they reset | `api.anthropic.com/api/oauth/usage`, as Claude Code's `/usage` does |
| ChatGPT through opencode (Plus/Pro) | the 5-hour and weekly windows | `chatgpt.com/backend-api/wham/usage`, as Codex's `/status` does |
| GitHub Copilot | premium requests and chat messages left this month | `api.github.com/copilot_internal/user` |

- Relay asks with the login the provider program already has, and never renews
  one: a renewal would sign that program out. When the login has expired,
  Relay shows what the last run told instead (Claude Code reports its windows
  with every run), and how old that is.
- An answer is reused for 2 minutes; **Refresh** asks again.
- The tightest window is always in sight: the **usage ring** on the activity
  bar, between Shell and Settings, shows it, and a click shows all of them.
- These are the programs' own, unofficial endpoints. If one changes, its row
  says why there's nothing.

### Adding any agent with `command`

Wrap the agent in a script that prints one JSON event per line — the same
events `./opencode-manager stream` prints:

```json
{"type": "session", "session": "abc123"}
{"type": "text", "delta": "Hello"}
{"type": "tool_call", "id": "t1", "name": "shell", "input": {"command": "ls"}}
{"type": "tool_result", "id": "t1", "name": "shell", "ok": true, "output": "a.txt"}
{"type": "done", "status": "succeeded"}
```

Arguments can use `{prompt}`, `{session}`, `{model}` and `{directory}`; the
prompt goes to stdin by default. The `session` event's id comes back as
`{session}` in `resume_command` on the next message, so the agent can
continue the conversation. See the full event list in the
[bridge section of opencode-manager's docs](opencode-manager.md#streaming-agent-sessions-bridge).

## Edit, fork, switch, retry

- **Edit** any message you sent (the pencil under it): the chat goes back to
  that message, and what came after it is removed (Relay asks first when
  that's more than its answer, and always in a code task).
  Chat APIs simply get the shorter history; opencode continues from a copy
  of its session up to that message; Claude Code starts a fresh session that
  gets the conversation handed over (below). Files an agent changed stay as
  they are.
- **Fork** (under any answer, or in a chat's menu for all of it): a copy of
  the chat up to there, to try something else while the original stays as it
  was. Agents continue from a copy of their own session where they can
  (Claude Code at the end of a chat, opencode from any answer); otherwise the
  fork gets a handover. A code task on its own branch gets another branch,
  starting from the task's files as they are.
- **Switch provider or model** halfway with the pickers under the message
  box; a line in the chat marks where another one took over. Hover a model
  in the picker (or tap its ›) for its effort levels: picking one sets the
  model and how hard it thinks at once (the same as `/effort`). A new agent
  gets the **conversation handed over**: its first prompt carries the chat so
  far, condensed (every message's text, tool calls as one line, no tool
  output). When that's too long, the oldest messages are left out of the
  prompt and the whole conversation goes along as a file the agent can read.
- **Regenerate** (under the last answer) replaces it, for chat APIs. For
  agents, **Retry** appears when a run failed or was stopped; it sends the
  message again as a new turn.
- **Titles**: a chat starts with its first message as title. opencode names
  its sessions and chat APIs are asked for a short title after the first
  answer; Relay takes that over unless you renamed the chat yourself.
- **Reply** to a part of an answer: select the text and click *Reply* by
  it; it's quoted (`> …`) in the message box, under what you typed already.
- **Export as Markdown** is in each chat's menu in the chat list, with tool
  calls and attachment names noted.
- **Chat → code task**: a chat that turns into work on a project goes on as
  a code task: pick a folder with *No project folder* under its message box
  (with an agent on it, it moves at once), or with `/code` (or *Move to a
  code task…* in its menu): the project folder and a provider with tools. The
  files the chat's agent made go along into the project (one the project has
  already stays behind, and you're told). The conversation stays; opencode takes its session along, Claude Code (which
  keeps sessions per folder) and other providers get it handed over.

## Commands

Type `/` in the message box for a menu; `/help` lists everything. **Tab**
completes a command, and then its choice (`/mo` Tab `pl` Tab gives
`/mode plan`).

| Command | Does |
|---------|------|
| `/mode <name>` | switch mode: `build`, `plan`, `fast`, `brainstorm`, or one of your own |
| `/model <name>` | switch model |
| `/effort <level>` | how hard the model thinks: `low`, `medium`, `high`, `xhigh`, `max` or `default` (what the model offers: Claude Code all five, opencode its model's variants, chat APIs `reasoning_effort` low–high) |
| `/thinking [show\|hide]` | expand or collapse the model's thinking by default (remembered in this browser; a click opens or closes one). Newer Claude models only send a summary of their thinking, and only when asked: Relay asks (`thinking_display` option: `"summarized"`, or `"omitted"`) |
| `/details [show\|hide]` | expand or collapse tool calls |
| `/new` | a new chat with the same provider, model, mode and effort (and folder, from a code task) |
| `/promote` | in a service's WorkBox: make it the service's live version (as *Promote to live*); in any other code task: make the project a ProductionBox, with the task as its WorkBox |
| `/clear [delete-history]` | the agent forgets the conversation so far and starts a fresh session; you still see it all, with a line where it was cleared. `delete-history`: the messages go for good, for you too (with their attachments and subagents' chats; a code task's files and changes stay) |
| `/code` | move a chat to a code task in a project folder |
| `/mount` | open a code task's folder as a network drive on your computer |
| `/shell` | open an interactive shell in this conversation's WorkBox |
| `/schedule [prompt]` | send a message in this chat later, once or again and again |
| `/fork` | go on in a copy of this chat |
| `/rewind [n]` | put a code task's files back to before the last (or n-th last) answer |
| `/retry`, `/stop` | answer the last message again; stop the agent |
| `/loop [name\|auto\|off]` | the fallback loop this chat follows when its model stops |
| `/timed [min] max [optimal] [task]` | keep the agent at it for a while, in minutes (`90`, `1h30`), with the task after it (below) |
| `/rename <title>`, `/export`, `/usage` | rename; download as Markdown; tokens and cost of this chat |

**`/timed`** holds an agent to a time from now. One number is the most time
it has (max); two are min and max; three are min, max and optimal. The task
can follow, and is sent like a message (queued when the agent is busy).

- **Before min**, an agent that stops is told, every time: "Please continue.
  Per the timed request, you are free in 20 minutes. Keep working until then."
- **Before optimal**, it's asked once whether something useful is left (tests,
  edge cases); otherwise it may stop.
- **At max**, Relay stops it, with its fallback loop: no fallback agent takes
  over and no usage-limit reset is waited for after it.

Optimal is 15% before max by default. With a min and no optimal it's the min,
so there's no "asked once" part. A bar above the message box shows the times,
with **Stop timer**. `/timed off` ends it too, and so does stopping the agent
yourself. A restart of Relay doesn't: an agent it cut off before min goes on by
itself once Relay is back.

The agent's **own commands** are in the menu too, and go to it as a message:
Claude Code's `/init`, `/review`, `/security-review`, `/compact`, `/context`
and your own (`.claude/commands/*.md` in the project or in Claude Code's
config folder); opencode's `/init`, `/review` and its configured commands.
Anything else starting with `/` is sent as it is.

**Skills** work as commands as well: `/code-review` (built in), or
`/code-review only the parser`, sends the agent the skill's instructions with
your request, whichever agent it is. An agent's own command of the same name
wins.

## Questions

An agent's questions (including OpenCode's native `question` tool) appear in
the answer as an open form, with the choices and their descriptions. Select
one or several options, or type your own answer, then **Send answers** to let
the agent continue. Reloading an ongoing chat shows its pending questions again;
completed answers stay in the history without live buttons.

**Dismiss** cancels the question through the provider, not by inventing an
answer. OpenCode ends that turn as interrupted. Stopping a run or restarting
Relay also closes its pending question cards.

Unanswered questions are dismissed after **five minutes** by default, even if
the browser is closed. The card shows the time left; reloading does not reset
it. Change **Settings → Limits → Questions → Dismiss after** (seconds; `0`
disables the timeout). The setting applies to new questions. Automatic dismissal
uses the same provider behaviour as the **Dismiss** button.

## Queue

Type while an agent is still working and the send button turns into
**queue**: the message waits above the box ("Next: …", × to drop it) and is
sent when the answer is done — also when Relay isn't open in a browser. After
an answer that failed or was stopped, the queue waits for you: it says
"Waiting:", and **Send now** sends that message.

## Temporary chats

**Temporary** (top right, on a new chat without a folder) makes the next chat a
temporary one:

- **Unlisted:** it's in no list or search, not under Tasks, and AgentOS's
  Governor doesn't see it.
- **No memory:** it uses none of Relay's memory and adds none.
- **Quiet:** no title is asked of a model, and no notification is sent.
- **It goes when you leave it,** stopping its agent if it still answers.
  **Keep it**, under the message box, makes it a chat like any other first.
- **Left behind** (its tab closed), it goes after a day unused (Settings →
  Limits → Temporary chats).

Only a chat can be temporary. A code task has files and branches, and a
computer task a computer. A temporary chat moves to a project folder once
it's kept. Scripts make them too (`convo.create(temporary=True)`).

## Modes

A mode is how the agent works in a chat. Pick one with the picker under the
message box, or with `/mode` (`/mode plan`, `/mode build`, `/mode critic`).

- **Build** (the default) changes files and runs commands.
- **Plan** looks around and makes a plan without changing anything.
- **Read-only** looks around and answers, changing nothing, without further
  instructions (what a read-only agent's subagents get).
- **Fast** splits the work into independent parts and hands them to
  [subagents](#subagents) that work at the same time, then checks and joins
  their work.
- **Brainstorm** starts three subagents at once: one argues for the idea, one
  against, one weighs costs and benefits. None sees the others, so their
  answers don't converge; the agent then draws a conclusion. Read-only, and
  it works with chat models without tools too (they write the three views
  themselves).
- **Your own**, under **Settings → Modes**: a name, instructions and access,
  *edit* or *read-only*. A "Critic" that looks for weak spots, a "Reviewer",
  a "Tester" that writes tests first, … One with the id `fast` or
  `brainstorm` replaces the built-in one.

**A conversation's own instructions** (the [Python SDK](#python-sdk)'s
`instructions`, or the API's) come after its mode's, for that conversation
only, and reach every agent as a mode's do: in its system prompt, or with each
prompt. Changed, the next turn has the new ones.

Every mode has an icon, shown in the picker: a hammer for Build, a map for
Plan, a bolt for Fast, a light bulb for Brainstorm, and a shield or a
wrench for your own (read-only or not). Click one in **Settings → Modes**
to pick another, the built-in modes' too.

**Read-only modes** (Plan, Brainstorm, and your own with *read-only* access)
ask the agent not to change anything: it may look around as much as it
needs, and run commands that look or try things out (tests, builds), but
not edit files, install, commit, push or deploy; what should change it
describes, for a later turn in a mode that works on the task. That is all:
Relay doesn't hold it to that (its commands run in the
[container](#containers) either way, and git has the project's history), and
it doesn't use the agents' own plan modes either (Claude Code's plan mode
and opencode's plan agent forbid even reading commands, so the agent
couldn't find out what it was planning for).

How each provider gets a mode's instructions:

| Provider | How |
|----------|-----|
| Claude Code | `--append-system-prompt` (for a resumed session, in the message when they changed: Claude Code keeps a session's first system prompt) |
| opencode, ACP agents | in the message, worded as the user asking |
| Chat APIs | a system message |
| `command` | `RELAY_MODE`, `RELAY_MODE_ACCESS` and `RELAY_MODE_INSTRUCTIONS` in the environment |

A line in the chat marks where the mode changed.

## Subagents

Relay adds a few tools of its own to every agent, **on top of** what the
agent has: its own tools, its own MCP servers and its own subagents stay as
they are. **Claude Code** gets them as tools of an MCP server "relay" for
each run (`agents`, `delegate`, `wait`, and the computer tools), so it calls
them directly, without shell commands to quote; `delegate` with `wait: true`
hands a task over and returns the answer in one call. **Other agents** (opencode,
command agents, whose MCP servers can't be set per run) use a small script. Each agent conversation gets that script,
`~/.cache/opencode-manager/relay-tools/<token>/relay` (read-only, on the machine
itself), and in the WorkBox it's plain **`relay`**: Relay puts
`container/bin/relay` first on every command's PATH, which runs the script of
the conversation the command is for (`RELAY_TOOLS` when the agent's program
says which, as Claude Code's, Copilot's and command agents' do; otherwise the
agent working in that folder, which Relay looks up: opencode's shared
service). The agent is told about it **briefly** — Claude Code in its system
prompt, opencode in the first message of a session, about 150 tokens: what
there is, that commands run in a container, that Relay never commits, the
skills by name — and `relay help [container|task|subagents|computers|skills]`
has the rest, as it is for that conversation now:

```sh
relay agents                      # providers, their models, efforts and modes
relay delegate --provider oc --model opencode/big-pickle --label "Critic" <<'EOF'
Find the weak spots in this plan: …
EOF                               # starts a subagent, prints its id
relay wait                        # up to 100 s; prints the answers that are in
relay tell ID "Hurry up: answer with what you have"   # a message to a subagent
# in a code task: Relay's record of its turns
relay changes                     # the turns, and what each changed
relay diff 2                      # a turn's diff (or: all)
relay restore 2                   # the files as they were before turn 2 (says how to undo it)
relay commit -m "…"               # commit the task's folder as the user (only when asked)
relay merge ID                    # apply a subagent's changes that weren't (conflict markers where needed)
```

Starting and waiting are separate, so no shell tool times out and several
subagents work at once, each without the others' context. Just ask for it,
e.g. *"Let three subagents look at this at once: one argues for it, one
against (on opencode with a free model), one only weighs costs and benefits;
then decide."* Or: *"Build the steps in roadmap.txt; give the simple ones to
Haiku and check their work."*

Each subagent shows up in the answer as a card with its provider, model,
mode, status and cost; open it to see its work, live, or open it as a chat of
its own.

**A free subagent doesn't stall on its model.** Free models come and go
("Endpoint is unavailable", "This model is not available in your country").
When a free subagent's model fails without getting anywhere, or a second
time, the next free model takes its task over at once, up to three of them.
These are the free models you allow without asking, without the ones that
failed in the last hour or run on a local server, and those that worked last
come first. The card and the calling agent say which one took over. A paid
subagent is left to its caller, as before.

**In a code task** a subagent that may change files works in a copy of the
agent's folder (and a container of its own), so subagents working at the same
time, and the agent itself, don't get in each other's way. The copy is the
folder's files as a checkpoint (what a checkpoint skips: installed software
is linked to the original's, `node_modules`, a virtual environment, … and the
copy's container gets those folders too; small ignored files such as `.env`
are copied, ignored folders such as build output aren't, and ignored files
don't go back). When the subagent is done, what it
changed goes into the agent's folder with a three-way merge, touching only
the files it changed, and its answer says which. When its changes conflict
with what changed in the folder meanwhile, or it failed or was stopped,
nothing is applied: the agent gets what it changed (a diff) and decides —
`relay merge ID` applies it anyway, with conflict markers where needed;
`tell` lets the subagent go on, with the agent's current files brought into
its copy (conflicts marked); or the agent takes what it needs itself. A
read-only subagent (e.g. `plan`) works in the folder itself. The copies go
when the agent's answer is done.

**What they may cost.** Some subagents cost money (an API billed per token)
or eat a plan's limits (a big model). Under **Settings → Subagents** you
choose which run without asking: the calling agent's own provider and model,
free and local models (local servers, OpenRouter's `:free` models, opencode's
free models, models whose last run was free), and any provider or model you
add to the list. For any other, the agent asks you in the chat (and by ntfy
or webhook, if set up): **Allow**, **Always allow this model**, **Use …
instead** (an allowed one), or **Deny**. Without an answer within 10 seconds
(Settings → Limits) the allowed one is used instead, or, when there's none,
it's refused; you can also have them refused right away. The agent is told
what happened (e.g. that you chose another model), and which subagents it may
use; `relay agents` says so up front.

**When a subagent fails.** `delegate` waits a few seconds to see a subagent
get going, so one whose model fails right away (unavailable, out of credit)
is reported at once, with the advice to pick another model. One that fails
later is reported with the next result of any of Relay's tools, without
waiting for `wait`. `relay agents` names the models whose last run failed.

**Talking to a subagent.** `tell` sends one a message: one still working is
interrupted and goes on with it (to hurry it along, or steer it), a finished
one gets it as a follow-up, with its earlier work in mind. The agent collects
the new answer with `wait`. A subagent whose answer breaks off with an error
gets one more try by itself, going on where it was (not on a usage limit).

**Their mode.** A read-only agent's subagents work in the plain
**Read-only** mode: they're asked to only look around, and have no
instructions of their own; their task says what to do.

**Effort.** `--effort` (or `effort`) sets how hard a subagent thinks, from
the model's efforts that `relay agents` lists. In **Fast** mode subagents
think as little as their model allows unless the agent says otherwise; in
other modes they get the model's default.

**Local models.** A server on your own network or machine (e.g. Ollama)
runs one model at a time for an agent and its subagents (Settings → Limits,
*Local models at once*): loading a second one next to it makes a GPU swap or
spill into slow memory. A subagent on another model there is refused, and
the agent is told to use the model that's already working, or one
elsewhere. Relay knows a model is local from a chat provider's address, and
from opencode's own provider settings. When your local server isn't running, turn
*Run subagents on local models* off (Settings → Subagents): agents then get
no subagents there (they're told so, with the ones they may use), and
`relay agents` says so up front.

Limits: subagents don't get Relay's tools (one level deep), only its web search; by default at
most 4 run at once (each is an agent process and a container: lower it on a
small machine), at most 12 per run, each at most 30 minutes (Settings →
Limits; `relay agents` tells the agent the current ones), and they stop when
the calling run does. The script's token only works while a run of its conversation is
going.

Every provider can *be* a subagent; every agent with a shell can call them,
and so can chat models, through [their tools](#tools-for-chat-apis).

## Skills

Skills are instructions (and scripts) for particular jobs, in the
[Agent Skills](https://agentskills.io) format: a folder with a `SKILL.md`
that starts with a name and a description.

```markdown
---
name: release-notes
description: Writes release notes from the git log since the last tag. Use when asked for release notes.
access: read
---

1. Find the last tag: `git describe --tags --abbrev=0`.
2. …
```

Agents only see the names and descriptions and read a skill when it fits.
Claude Code gets them as its own skills; opencode and command agents get the
list with each `SKILL.md`'s path in Relay's note; chat models get
`list_skills` and `read_skill`. `access: read` (optional) marks a skill
that never changes files; a read-only mode asks agents not to change
anything either way.

Relay ships these:

- `code-review`: reviews a task's changes (`/code-review`);
- `relay-computers`: working on [your own computers](#your-own-computers);
- `relay-subagents`: how to use [subagents](#subagents);
- `relay-skill-writing`: how to write a skill;
- `relay-sdk-template-writer`: how to write a script for the SDK, with templates;
- `relay-provider-writer`: how to add a provider Relay doesn't know;
- `relay-plugin-writer`: how to write a plugin.

Your own live in `~/.local/share/relay/skills/<name>/`; one with
the name of a built-in replaces it. Add and edit them under
**Settings → Skills**, in that folder directly (`./relay skills` prints
it), or ask an agent to write one.

## Memory

The providers keep memories of their own: Claude Code one per folder, so one
per chat in Relay, and what it learns in one chat the next doesn't know.
Relay's memory is one place for all of them, beside theirs (Settings →
Memory), a memory bank as Perplexity's:

- **What's in it:** short facts and wishes that are lasting, each filed in a
  **category**: Preferences, About you, Work, Tools, Interests, Location,
  Setup (how a project, service or machine is set up), Decisions (what was
  decided and why), or Other.
- **Where it counts:**
  - **General** ones count in every chat and task.
  - **A project's** count only in code tasks in that folder. A task's own
    branch counts as its project. A service's WorkBoxes count as the
    service.
  - **The Governor's own** (AgentOS) only the Governor gets: what it learns
    as the Governor that lasts (your decisions, how your services and tasks
    are, what didn't work). It gets the general ones too, with every
    briefing; its `notes.md` is for longer records. The AgentOS page links
    to it (Memory, on the Governor's card).
- **Every agent gets it**, whichever provider:
  - an agent with a shell or MCP tools in Relay's note (Claude Code,
    opencode, Copilot), each memory with its category;
  - a chat model in its system prompt.
- **Agents add to it** when you tell them something lasting or ask them to
  remember, each with its category:
  - `relay memory add --category tools "…"` (`--project`), `change`
    (`--category`), `forget`, `list`;
  - for Claude Code its MCP tools `memory_add` and the like;
  - for a chat model the functions `remember` and the like;
  - what you wrote yourself, no agent changes or forgets: it's marked as
    yours in what they're told;
  - a turn's activity says so ("1 memory updated").
- **In Settings → Memory:**
  - **Search** (the text, its category, where it counts, who added it).
  - **A category's chip**, with how many there are, and **where** they count
    (everywhere, general, the Governor, a project).
  - **Each memory** with its category, where it counts, who added it, in
    which chat, and when. You can add, change (its text, category or
    place) or forget one; Undo puts it back as it was.
  - **Delete all memories**, after you confirm it.
- **Two switches:**
  - **Agents use it.** Off: no agent gets the memories or can add to them.
    They're kept.
  - **Agents save what they learn.** Off: they read the memories, and only
    you add to them.
- **Limits:** at most 300 memories of 600 characters. Subagents don't get it;
  their caller tells them what they need.
- **Kept tidy:** Relay notes when an agent last got each memory. With AgentOS
  on, the Governor's weekly tidy-up merges what's said twice and forgets
  what's gone stale, never yours ([system tasks](#agentos)).

## Your own computers

Relay runs on one machine; what you want changed may be on another: a
setting on your PC, a program to install, a file on its desktop. Under
**Settings → Computers**, add a computer (e.g. `laptop`) and download its
connector, `relay-computer.py`. Run it there and keep it running:

```sh
python3 relay-computer.py        # Windows: py relay-computer.py
python3 relay-computer.py --ask  # also confirm every command on the computer itself
```

It needs only Python 3.8+. It connects to Relay (so it works from behind a
router), shows every command it runs, and stops with Ctrl+C; while it runs,
agents can work there as you. Just ask: *"On my laptop, turn on dark mode"*,
*"What's using the disk on my PC?"*.

It reaches Relay at the address your browser had when you downloaded it.
**Behind a proxy's own sign-in** (Caddy's `basic_auth`), it asks for that
name and password when it starts. Or give them in the address, to skip that:
`--url https://name:password@relay.example` (or `RELAY_URL`). `--url` also
takes another address.

- Agents with a shell get `relay computers`, `relay run --on laptop '…'`,
  `relay pull` and `relay push` (the built-in `relay-computers` skill
  explains them); chat models get `computers`, `computer_run`,
  `computer_read_file` and `computer_write_file`.
- **New computer task** (in the Chats view's header)
  starts a conversation about one computer, picked with its online status:
  the agent is told the work happens there, `relay run` and `pull`/`push`
  go to it without `--on`, and the header shows whether it's online. It
  needs a provider with tools (an agent, or a chat model with Relay's tools).
- **Ask me before each command or file change** is off for a new computer:
  the agent just goes ahead. Turn it on per computer to have each one show
  up in the chat with *Allow*, *Allow everything on … for this answer* and
  *Deny* (denied after 90 s without an answer).
- Read-only modes (Plan, Brainstorm) ask the agent not to change anything
  there either. Commands there don't run in a container: it's your computer.
- The connector's file holds its key: whoever runs it acts as that computer.
  Removing the computer in Relay makes the connector stop.

## AgentOS

**AgentOS** on the activity bar (it replaced *Scheduled*) has a **Governor**: an
agent that steers other agents for your tasks while you're away. It doesn't do
the work itself: it runs in a **GovernorBox**, a WorkBox with no project in it
(only its notebook), and works through Relay's **capabilities**. Don't want
it? Settings → AgentOS leaves it out of Relay ([below](#leaving-agentos-out)).
Those every wake-up uses (tasks, chats, agents, notices) its program lists from
the start; the rest it finds when it needs them (Claude Code's tool search). A Governor without MCP tools calls them
as `relay agentos <capability> key=value …` (`relay help agentos` lists them).

| Capability | What |
|---|---|
| `task.list`, `task.done` | the AgentOS tasks: when each runs next, when it ran last and how that went (by whose word); report how one went |
| `task.create`, `task.update`, `task.delete` | tasks of its own (a follow-up, a check next week), which it may change and delete; yours it only carries out. A task can run one of your public scripts (`script=`): Relay runs it at its time, as you, with no agent, and notes how it ended |
| `chat.create`, `agent.spawn` | a chat or code task (in a project folder), an agent on it at once; without a provider on the Governor's own model (when you allow it) |
| `agent.review` | a fresh agent (any model, none of the task's conversation) in a managed code task's folder and WorkBox, a clone's too; read-only by default, so it reads, runs the tests and reports |
| `chat.list`, `chat.send`, `chat.read`, `chat.delete` | the chats it made |
| `chat.wait` | a short wait (up to 100 s) until its chats are done: their answers, and how far the others got |
| `chat.tell` | steer an agent: one that's working is interrupted, and the message is its next turn |
| `chat.fork` | a fork of one of *your* chats, only when you allow it (asked in the page) |
| `notify.intervention` | "Intervention needed: …": in the AgentOS page, and a push if set up; only when you must decide or do something |
| `notify.report` | what's done or found, for you to know: under **From the Governor** on the AgentOS page, and a push if set up; not on your list of what needs you |
| `notify.update`, `notify.resolve` | an open intervention's text anew; one that's fixed or no longer applies settled, saying why (you see it under Needs you for 3 days) |
| `service.list`, `service.status`, `service.logs` | the live services (below); one in detail: its box (since when, restarts, last exit, killed for memory), its processes, the health checks and one made just now; its log |
| `service.restart`, `service.delete` | its live version restarted (a box that's gone is made anew), as your Restart does; the service deleted, only when you ask (irreversible) |
| `service.stop`, `service.start` | a service off the air with its versions and data kept; back on |
| `service.create` | a new live service from one of its code tasks' folders (once its agent made it run on `$PORT` with its data in `$DATA_DIR`, and it was reviewed): at a public port of its own (a free one from 8500, unless it says), looked after by AgentOS; the task goes on as its WorkBox. For sites it designs on its own; under "ask me first" if you want to decide |
| `service.restore` | a service's data put back as one of its snapshots has it (`service.status` lists them); the data as it is now is kept first, so that can be put back too |
| `workbox.clone`, `workbox.delete` | a service's live code and a snapshot of its data as a managed code task to work on |
| `workbox.look` | a command (ls, cat, git diff) in an agent's folder, mounted read-only in a box of its own: to see its work without changing it |
| `production.promote`, `production.rollback` | that work as the new live version; back to an archived one |
| `provider.repair` | another agent repairs Relay's support layer for a provider that keeps failing, on [the repair's page](#repairing-a-provider): its plan, trying it and applying it are yours (a Needs-you item opens it) |
| `provider.list`, `provider.add`, `provider.remove`, `provider.check` | the providers, and the kinds it may add: only those whose program runs in a box of Relay's, or a chat API (not `command`, not the generic `acp`), never with a secret of yours (a kind that needs one is yours to add), never with a box that mounts what a test box may not. What it adds says "added by AgentOS" in Settings; it removes only those. Relay's whole check of one, as Check it fully runs it |
| `provider.create` | a program Relay doesn't know: sends you to [add it with an agent](#a-provider-relay-doesnt-know-added-with-an-agent), a Needs-you item that opens its page with what it knows of the program. Installing it, logging in, the plan and trying it are yours |
| `script.list`, `script.read`, `script.run`, `script.output` | your [public scripts](#scripts): which there are and how each last ran, one's text, one run as you (it may start agents of its own), a run's output. Your private ones it doesn't see |
| `memory.review`, `memory.change`, `memory.forget` | all of [Relay's memory](#memory) (every place), when an agent last got each, and what Relay found (worded nearly alike, place gone, unused for long); a memory merged or forgotten, saying why (never your own: those it names in its report) |

What the Governor makes is **Managed by AgentOS** (a compass by its title): it
may see, change and delete those. Your own chats stay yours, and so does a fork
you make of one of its. Its chats are in the **Governor** view, after the
Governor's own conversation, not among yours. Two are in the Chats view as well:
one waiting for you, and one you pinned.

- **It dispatches; it doesn't do the work, and it doesn't sit waiting:**
  - **Ending its wake-up:** once its agents are at work, it ends its wake-up.
  - **Agents done:** Relay wakes it when they're done, a batch at a time, with their answers in the briefing.
    One that didn't finish comes with who stopped it (you, a restart, its time), after how long, what it cost
    and its last steps, so it can tell whether to go on or start afresh.
  - **Signs of trouble:** Relay also wakes it when one seems to go wrong: steps failing in a row, or one turn
    running long (Settings → Limits → AgentOS). It then looks (`chat.read` shows how far the turn got,
    `workbox.look` reads the folder) and steers with `chat.tell`. A task it gave an agent awaits its report
    over wake-ups.
  - **How often:** for its agents' news and signs of trouble, 6 times an hour by default (Settings → Limits →
    AgentOS), as each wake-up has a budget of its own. That's a pace, not a count per clock hour: 3 may come at
    once, then one every 10 minutes, so news waits 10 minutes at most and comes along then (the activity says
    so once). Its tasks and its round wake it when they're due.
  - **A restart of Relay:** what it follows and its news are kept. An agent's turn the restart cut off goes on
    by itself ("Relay resumed …" in that chat), followed again; a wake-up of its that was cut off is done again,
    and its briefing says what went on. An agent you stopped yourself stays stopped.
- **Its own model for its agents** (on the Governor card, on by default): research, reviews and other
  judgment work go to an agent on the Governor's own model, the default when it names no provider, rather
  than to the Governor itself.
- **In its own chat** it's always the Governor. A mode you choose there
  (Read-only, say) is added to its instructions, for that moment: it answers
  as the Governor that knows its capabilities, but only looks.
- **What it writes for you** (interventions, reports, notes) it's told to
  keep short and plain, in your language.
- **Times:** its briefing gives the server's time with its zone. A task's
  time may be given as `once 2026-10-05 09:00`, as an ISO time with its own
  zone (`2026-10-05T07:00Z`), or as `in 10 minutes`.

**Tasks** have a name, what's to be done, when (every day, weekdays, days of
the week, every so many hours or minutes, or once; in your browser's time
zone) and who does it:

- **The Governor decides** (an AgentOS task, optionally with a project folder):
  it chooses the agents and models (`relay agents`), follows their work,
  checks it, has it fixed, and records the outcome with the task.
- **An agent you choose**: a prompt Relay sends that provider, model and mode by
  itself, in a chat or code task of its own or on in the same chat
  (`/schedule [prompt]` in a chat), as Scheduled did. Each run is an ordinary
  answer, so you're notified as usual.

**Run now** runs one by hand. A time missed while Relay was off runs once when
it's back.

The AgentOS page lists tasks in three kinds:
- **System tasks** are Relay's own, added when AgentOS goes on, and carried out
  by the Governor:
  - **Services: weekly check** (Monday 09:00): every live service's logs,
    health, restarts and leftover clones; small fixes through a clone, an
    intervention for the rest.
  - **Memory: weekly tidy-up** (Sunday 19:00): memories that say the same
    merged, and those whose project or service is gone, or that no agent got
    for 90 days (Settings → Limits → AgentOS) and no longer hold, forgotten.
    Yours it leaves: those it names in its report. With nothing new and
    nothing stale, Relay notes that itself, without waking the Governor.
  - **Tidy up: weekly** (Sunday 20:00): finished managed chats and clones,
    settled interventions, its own tasks that ran out.
  - **Models: weekly ranking** (Monday 07:00): the
    [model ranking](#model-ranking) for models that are new since last time.
    When there are none, Relay notes that itself, without waking the Governor.

  Editing one first asks whether you mean it (unless you said not to again).
  An edited one says so, and **Reset to default** gives Relay's version back.
  Turn one off rather than delete it: it can't be deleted.
- **Governor's tasks** are made by the Governor, which may change and delete
  them.
- **Manual tasks** are yours. The Governor carries out yours and Relay's, but
  doesn't change them.

### Services

**Services** (the AgentOS page → New service) are software that's live: a
name, the project folder its code comes from, how it starts (it listens on
`$PORT` and keeps its data in `$DATA_DIR`), the port Relay listens on for it,
and optionally a health path (`/health`; else a connection is enough).

- Each **version** is a frozen copy of the code in a **ProductionBox** (a
  long-lived box of the WorkBox image, restarted when it crashes and when Relay
  starts), on an internal port of its own. Relay forwards every connection to
  the service's port to the live version, so the address stays the same.
  Through Relay itself it's at `/port/<its port>/` (see
  [Servers at /port/](#servers-on-this-machines-ports-portn)).
- The **data** is a volume of the service's own (`/data`), shared by its
  versions: a new version finds it as the old one left it. What a version
  writes next to its code stays behind with it.
- **Its copies** (`production/<service>/snapshots/`):
  - **before each version** takes over;
  - **once a day**, after 04:00, its box paused for that moment. The last
    7 are kept (Settings → Limits → Services → Daily snapshots; 0: none).
  - **Put back** one on its page (or the Governor's `service.restore`): its
    box stops for that moment, and the data as it is now is kept as a copy
    first, so putting that back undoes it.
- A version has **only its folder**: what was installed in a WorkBox (`pip
  install`, `apt-get`) isn't in its box. Its dependencies go in the folder
  (`node_modules`, a `.venv`), or its start command installs them. The agent
  in a service's WorkBox is told both; the new service dialog says when the
  code never mentions `$DATA_DIR`, and can have the task's agent see to it.
  A first version that doesn't come up shows why under **Log**.
- A change goes **clone → fix → promote**: `workbox.clone` gives an agent a
  managed code task with the live code and a copy of the data in `.data` (to
  try: `PORT=18080 DATA_DIR=.data <its command>`); `production.promote` makes
  that folder the next version, which takes over only once it's started and
  well: otherwise the live one stays, and the log says why (one that crashes
  while it starts is refused at once). The old version is
  archived for 7 days, for **Roll back**.
- Relay checks every service's health each minute. One that fails two checks
  in a row wakes the Governor to see to it (or, with AgentOS off, tells you).
  - **Once, until it's well for 10 minutes:** a service that comes and goes
    is one incident, not a wake-up each time.
  - **A single failed check,** such as a box that's still starting after a
    reboot, only shows on the page.
  - **Restart** (yours, or the Governor's `service.restart`) makes a box
    that's gone anew.
- On the AgentOS page each service is a row: its health, its address, its
  versions at a glance, whether AgentOS looks after it, and **Open** for its
  own page.
- **Stop** takes a service off the air: its address answers nothing and
  it isn't checked, while its versions and data stay. **Start** brings it
  back: its live version, checked first. A promote or rollback runs it again
  too.
- **Its last week:** Relay keeps a service's health as it changed (when it
  failed, how long, why) and its data's size once a day. `service.status`
  shows the last 7 days, for the weekly check.
- **Production boxes** (a view on the activity bar) lists the services. One opens where a chat would be, because agents don't work in a
  ProductionBox, with **Demote**, **Restart**, **Stop**/**Start** and Delete,
  and three tabs:
  - **Overview:**
    - its health now, its live version, its incidents in the last 7 days
      (when, how long, why, and which version made it well again), its
      data's size and growth, and its box (running since, restarts);
    - **every version:** who made it live and when, from which WorkBox
      (a link to its chat), Relay's check, and **Roll back to this** on an
      archived one (**Why** on one that failed);
    - its data's copies (before each version, daily), each to **Put back**;
    - its WorkBoxes, and how it runs.
  - **Shell:** a shell in its live box, in the version's folder, with the
    WorkBox shell's guard, connected when you open the tab. It may change
    things, but code changed there is gone after the next promote (`/data`
    stays).
  - **Log:** its live version's last 200 lines, or why the last one didn't
    come up.
- **Managed by AgentOS, or private**: a service AgentOS looks after (its icon
  green with "os" in the Production boxes view) is the Governor's to see and change: it's
  in its list, its folders are readable to it, it's woken when the service
  fails, and it may clone, promote and roll it back. A private one, which is
  what a new service is unless you tick *Let AgentOS look after it*, is
  invisible to the Governor, and when it fails *you* get the intervention.
  The button on the service's page (and on the AgentOS page) switches it.
- **`/promote` in a code task** makes its project a ProductionBox (a dialog: name, how it starts, port; the
  start command is guessed from `package.json`, `app.py` or a compose file), and the task goes on as its
  WorkBox: a later `/promote` makes what changed there the next version.
- **The Governor makes services too** (`service.create`), from its agents' code tasks, for a site it
  designs on its own: looked after by AgentOS from the start, the task its WorkBox.
- **Update from live**: when the live version moved on from the one a WorkBox was made from (a fix of
  the Governor's, a rollback), promoting it would undo that, so Relay refuses and offers *Update from
  live* instead: a three-way merge of the live version's changes into the WorkBox (yours stay;
  conflicts get `<<<<<<<` markers for the agent to resolve), after which it can be promoted.
- **Demote** (on that page) clones the live version into a WorkBox of your
  own at once: a code task, with a snapshot of the data in `.data`, where you
  tell an agent what to do (the agent of a new conversation, if it has tools).
  The service stays live meanwhile. **Promote to live** in that chat's header
  makes it the new version once its health check passes; the old one is then
  archived for 7 days, to roll back to. Deleting the chat removes the WorkBox's
  folder. The Governor isn't involved, and leaves your WorkBoxes alone; it
  clones and promotes managed ones of its own (`workbox.clone`,
  `production.promote`). The service's page links to its WorkBoxes.
- A ProductionBox runs day and night and starts with Relay. A WorkBox nobody
  used for 3 hours is stopped to free memory, and started again when it's
  needed.

### The Governor's settings

**On the AgentOS page:** on or off; its **approved
models**, a fallback loop of capable models you choose (Settings → Usage), so a
model that stops is taken over by the next; how often it **looks around** for
judgement of its own (default every 16 hours); and a **budget per wake-up** (at
most so many agents started, and about so much cost; wake-ups for its agents'
news and signs of trouble at most so many an hour). Its briefing says what
AgentOS spent in the last 24 hours, and each agent it starts says what's left
of this wake-up's budget. Relay works out what's
due itself and wakes the Governor only when an AgentOS task is due or its round
has come, never just to look; **Wake now** wakes it by hand. Its runs are a
conversation of its own (Open), with the same activity, costs and stop button
as any.

### Model ranking

AgentOS ranks the models available here by intelligence, from #1 (the
smartest) down (`relay/ranking.py`).

- **Where it shows:**
  - the AgentOS page (**Model ranking**, with each model's score and source);
  - the model picker (`#3` by a model);
  - Settings → Usage → Fallback loops ("Intelligence index: #3" by each
    step);
  - the agents list an agent gets when it picks a subagent (`relay agents`).
- **Who makes it:** the Governor, in its weekly system task. `models.list`
  shows every provider's models and the score Relay keeps for each. For the
  new ones it looks the score up on the Artificial Analysis Intelligence
  Index, one scale for all. A model that isn't on it is placed there from
  other benchmarks ("estimated"). It saves what it found with
  `models.score`, and Relay ranks. A router (Copilot's auto: another model
  per request) has no score of its own: it's kept as one and not ranked.
- **Scores are kept:** in `model_scores`, by the name the benchmark gives a
  model, and in `model_names`, which provider model is which. So a model is
  looked up once. The same model id under another provider (two opencode
  profiles) counts as the same model, and one model reached through two
  providers has one rank.
- **Without AgentOS** (or before its first ranking) there's no ranking, and
  everything else works as before.
- **On the panel:** **Update** runs the task now; **Start over** forgets the
  scores kept, so the next update looks every model up anew.
- **The names stay clean:** a model's name is exactly the index's. A tier, a
  setting or an alias goes in its note (shown on the panel), so one model
  reached in several ways has one rank.
- **No agents needed:** the Governor does it itself, with web search and
  fetch.

### What holds the Governor

The Governor is a model: what it says is a claim, and Relay holds it to what
it may do, so it can run unattended without the means to harm Relay or go
past what's yours:

- **Only its capabilities.** Its token reaches its own tools, wherever the call
  comes from (not just what it's shown): no `delegate`, `computer_run`,
  `commit` or `restore`. A subagent's token has none but Claude Code's
  permission prompt; a chat of yours has no AgentOS capability.
- **Only its own things.** Its commands run in the GovernorBox (its notebook;
  no project, no token, no other conversation). Its program's own file tools,
  and those of the agents it manages (and their subagents), read only their
  own folder and what Relay handed them: the providers' permission checks
  refuse the rest (your chats, other projects, Relay's data). Your own agents
  read anywhere, as before.
- **Only what's managed.** It sees, changes and deletes only what AgentOS made;
  your chat takes your permission and becomes a managed fork; your tasks it
  carries out, never changes.
- **Only providers in boxes.** Its agents (and the Governor itself) run on
  providers whose program Relay holds in a box. A `command` provider runs on
  the machine as you set it up, so it isn't for AgentOS.
- **Adding providers, in boxes too.** It may add a provider only of a kind
  whose program runs in a box of Relay's (or a chat API), without a secret
  of yours, with a box that mounts nothing a test box may not; and remove
  only what it added. A repair's code is tested in a box of its own, and goes
  into Relay only when you apply it. A program Relay doesn't know is yours to
  add with an agent: the Governor sends you there.
- **A task's turn is Relay's.** Relay hands a due task to a wake-up before it
  starts (once: never two at a time) and sees it end. `task.done` is the
  Governor's report, taken only for a task handed to it, and shown as its
  word ("the Governor says") next to what Relay saw itself ("Relay saw": a
  wake-up that ended without a report, or broke off). A wake-up that breaks
  off (Relay stopped, the model failed) makes the task due again in 10
  minutes; after 3 times Relay gives it up for now and tells you.
- **Services change one way.** Only a clone of the service, made from the
  version that's live now, can be promoted (not any folder, not a clone the
  service has moved on from); one operation at a time per service; the
  version takes over only after Relay's own health check, which is what's
  recorded as its validation, with where it came from (the clone, the
  version it was made from, the agents that worked in it, who promoted it,
  the version it replaced). A clone's `.data` never goes into production.
  After a crash, Relay puts it right at its start: a version left half-way is
  failed (a rollback's archived again), exactly one version is live, other
  boxes are stopped or removed.
- **Ask me first.** On the AgentOS page you choose which irreversible steps
  (promote, roll back, deleting a chat, clone, task or service) wait for your answer,
  as `chat.fork` does; no answer in 10 minutes is a no. Deterministic checks of
  your own plug in at the same place (`AgentOS.policies`).
- **A log of all of it.** The **Activity log** on the AgentOS page lists every
  capability call (and refusal), wake-up, task turn, version, rollback and
  repair after a crash, with who: the Governor, Relay, or you. It's kept 90
  days.

What it can still do: start agents (within the budget) on chats and code
tasks it creates, also in your project folders in the workspace roots (that's
how it carries out a task about one), and those agents change that folder as
any agent would. Read-only modes ask an agent not to change anything; they
don't stop it.

### Leaving AgentOS out

**Settings → AgentOS** has one switch: AgentOS in Relay or not (on by default).

- **Off:** it's gone from Relay's screens:
  - the bar says **Scheduled** (your own scheduled tasks, as before AgentOS), with no Governor and no
    Production boxes;
  - its conversations are off the list, and so is a service's page;
  - no `/promote`, no *Promote to live*;
  - Settings has no AgentOS or Services limits, notifications or Governor memory.
- **The Governor isn't woken** (its own switch on the AgentOS page is kept).
- **What it made is kept:** its conversations, its memory, its tasks (they wait; Scheduled says how many).
- **Services keep running** at their addresses, with their daily snapshots; one that fails is pushed to your phone
  or webhook if set up (Settings → Notifications), as the Governor isn't there to see to it.
- **On again:** all of it is back as it was.

## Attachments

Attach files with the paperclip, by dropping them on the message box, or by
pasting (e.g. a screenshot). Up to 10 files of at most 25 MB per message; they
are stored in `~/.local/share/relay/uploads/` and deleted with the conversation.

How providers get them:

- **Claude Code** and **opencode** get the file paths in the prompt and read
  the files with their tools (images too, for models that can see). opencode
  also attaches them to the message natively, for models that accept files.
- **OpenAI-compatible** APIs get images as image parts and text files inline;
  the model is told about files it can't read.
- **command** providers get the paths in the prompt and as a JSON list in the
  `RELAY_ATTACHMENTS` environment variable.

## Notifications

Runs keep going in the background, so Relay tells you when they finish, when
an agent waits for you, and what AgentOS needs:

- **In Relay**: chats with a result you haven't seen get a dot in the chat list
  (red when the run failed), the tab title counts them, and a toast with an
  *Open* button appears when a run finishes or an agent starts waiting for you
  in another chat, or while the tab is in the background.
- **System notifications** (Settings → Notifications): browsers only allow
  these on HTTPS or `localhost`, so over plain HTTP on your network use push
  notifications instead.
- **Push to your phone**, also when Relay isn't open: install the
  [ntfy](https://ntfy.sh) app, subscribe to a hard-to-guess topic and enter its
  URL (ntfy.sh or your own server) in Settings → Notifications. A **webhook**
  URL gets a JSON POST per finished run instead (e.g. for Home Assistant). A
  **Discord** webhook (Server Settings → Integrations → Webhooks: its URL)
  gets a message in Discord's own form instead: the title in bold, the text,
  the link, pinging nobody.
  Only runs longer than a minimum (30 s by default) are pushed, and *Relay's
  address* makes the notification open the chat. **Save and send test** saves the
  settings and then checks the setup; a failed test does not undo the save.
- **What to notify of**, per category, each with its own checkbox for *Push*
  (ntfy and the webhook) and *This browser* (the toast, and a system
  notification), and an ntfy priority:

  | Category | Pushed | This browser | Priority |
  |---|---|---|---|
  | Your chats and tasks: done | yes | yes | normal |
  | Your chats and tasks: failed | yes | yes | high |
  | Waiting for you (a question or a permission) | yes | yes | high |
  | AgentOS: done (the Governor's runs and its agents') | no | no | low |
  | AgentOS: failed | no | yes | normal |
  | Reports from the Governor | yes | – | normal |
  | Something needs you (an intervention: the Governor, a failing service or provider) | yes | – | high |
  | VPN tunnel problems | yes | – | high |
  | From your scripts (`notify(…)` in the [SDK](#python-sdk)) | yes | – | normal |

  What's pushed is saved with Save (`notify_categories` in `settings.json`);
  what this browser announces is kept in that browser at once. The webhook's
  JSON says the category.

## Chat and code modes

**New conversation** starts both: with a project folder picked under the
message box it's a code task, without one a chat. A new conversation starts
with *No project folder*; choosing one opens at the folder picked last
(`/new` in a code task starts in its folder). Chat models have no folder.

- **Chat** conversations get a private scratch folder each
  (`~/.local/share/relay/chats/<id>`), removed when the chat is deleted. Any provider
  can be used.
- **Code** tasks run in a project folder you choose. Only folders inside the
  **workspace roots** (Settings → Workspaces, default `~/projects`) can be
  used, and only agent providers can work in one.

Switching an agent provider in the middle of a conversation starts a new
agent session; chat providers get the conversation history with every message.

### Every turn is recorded; commits are yours

Relay keeps **its own record of every turn**: the files before and after
each answer, in a repository of its own under `~/.local/share/relay/checkpoints/`,
outside the project. It **never commits to your repository by itself**:
your git history only changes when you (or an agent you asked) commit.

- A code task works **in the folder you picked**. Its turns are recorded,
  your own edits in between too, and `/rewind`, the Changes panel and *Review*
  under an answer work from that record. **Mark as reviewed** in the Changes
  panel says you've seen the changes: the task leaves *Needs review*, and
  changes count from there. **Commit…** commits the task's folder (only that
  folder, in part of a larger repository) to your current branch, as you (as
  Relay, while git doesn't know your name), with your message. It takes
  everything changed in the folder, and says which of that this task didn't
  change; after it, the task leaves *Needs review* too. An agent can do the same with `relay commit -m …`, and is told
  to only when you ask it to.
- Through the API (`"isolate": true`) a task works on **its own branch**
  (`relay/task-…`, in a separate worktree under `~/.local/share/relay/worktrees/`), so
  the agent never touches the checkout you work in. That branch is Relay's:
  its turns are commits there. **Merge into main** puts all of them on the
  branch you started from as **one commit**, when you choose to; your checkout
  must be on that branch and clean. **Discard** deletes the branch; the chat
  stays. A folder that isn't committed yet, or has changes, works in place
  anyway: a branch starts from the last commit, without them.
- A folder that **isn't a git repository** works the same as one that is,
  without the Commit button. Relay's record respects the folder's
  `.gitignore`, skips `node_modules`, virtual environments and build output,
  and isn't kept for folders with more than 20,000 files.

Agents in a code task can look at the record too: `relay changes` lists the
turns and what each changed, `relay diff 3` (or `all`) shows a turn's diff,
and `relay restore 3` puts the files back as they were before it (only when
you ask; it says how to undo it).

**Changes** (in the header, or *Review* under an answer) shows what the task
changed since it started, with diffs.

**Installed software stays out**, also when the project's `.gitignore`
forgets it: new `node_modules`, virtual environments (`.venv`, `venv`, or any
folder with a `pyvenv.cfg`), `__pycache__`, tool caches (`.pytest_cache`,
`.next`, `.gradle`, …) and the like are neither recorded (nor committed on a
task's branch) nor counted in the changes. Neither are new files over 50 MB (a downloaded model, a video),
which would stay in the git history for good. Files in them that git tracks already are the project's own and go
along as usual.

**`/rewind`** in the message box puts the files back to how they were before
the last answer; `/rewind 3` three answers back, and `/rewind ` (with a
space) lists the answers to choose from. Nothing is rewritten: the files
before a rewind are recorded too, so the toast's **Undo** rewinds the rewind.
**Rewind to the start** is in the Changes panel, and editing an earlier
message offers to rewind the files too. Older tasks from before this record
went to your branch as commits ("Relay: …"); those stay in your history as
they are, and rewinding to one of them restores the files without a commit.

### Open a task's folder on your computer

To try what an agent wrote on your own computer, click **Open files** under
a code task's message box. Relay shares the task's folder (its worktree, for
a task on its own branch) over WebDAV at a random address,
`http://<relay>/dav/<token>/`. There's no user name or password: the address
is the key. The button opens it in the file manager (Linux, as `dav://…`) or
copies it in the form your system takes:

- **Windows**: a path like `\\<relay>@8420\DavWWWRoot\dav\<token>\`; paste
  it in File Explorer's address bar. To keep it: This PC → *Map network
  drive* → the http address. Files over 50 MB need the WebClient service's
  `FileSizeLimitInBytes` raised.
- **macOS**: Finder → Go → *Connect to Server* (⌘K) → the address, as Guest.
- **Linux**: Files → Other Locations → `dav://<relay>/dav/<token>/`, or
  `gio mount`, or davfs2 (`sudo mount -t davfs <address> /mnt/relay`).

*More ways, read-only…* in its menu (or `/mount`, or *Open files…* in the
chat's menu) shows all of this, and makes the share read-only or stops it.
What you change there is committed before the agent's next turn ("Changes
made outside Relay"). A share can be read-only. The folder's own `.git` isn't
shared; a repository you copy into it (a folder with its own `.git`) is copied
whole. The `.DS_Store`, `._*`, `Thumbs.db` and `desktop.ini` files desktops
leave behind are accepted but not kept. What the drive refuses is in
`~/.cache/opencode-manager/relay-webdav.log`. The address is the only key, so anyone on your
network who has it can open the folder: **Stop sharing** (in the dialog, or
under Settings → Workspaces) ends it; deleting the task does too. Over SSH
works as well, without Relay: `sshfs pi@<relay-host>:<folder> <mountpoint>`.
The Scripts page's **Open in VS Code** shares the scripts' folder the same way
([Scripts](#scripts)).

## Containers

Relay's containers are **boxes** (podman, rootless). Four kinds are about your work:

- **ProviderBox**: an agent's own program (Claude Code, opencode's services,
  Copilot), one per provider; its shells hand every command to Relay.
- **WorkBox**: where an agent's commands run: one per project folder (and per
  subagent's copy), a Debian image with the usual tools; what you install in
  it stays.
- **GovernorBox** (AgentOS): the Governor's own WorkBox, without any project
  in it: it steers other agents through Relay's tools and doesn't code itself.
- **ProductionBox** (AgentOS): a live deployment of a service, made from a
  WorkBox, behind an address of Relay's that stays when a new version takes over.

Relay also starts boxes of its own when they're needed: a VPN's
([gluetun](#vpns-for-workboxes-gluetun)), the [web's](#web-search-and-fetch-searxng-and-a-browser) (SearXNG, a
browser), and those of plugins that ask for them (OpenHands, say).
[Settings → System](#system) lists them all.

The first two, in detail:

    Relay (machine) ── starts ──▶ ProviderBox (a container per provider)
       ▲                            Claude Code / opencode-manager's services / Copilot
       │                            /bin/bash, /bin/sh = Relay's shell
       └──── exec socket ◀──────────┘
       │
       └── podman exec ──▶ WorkBox per project folder (the agent's commands)

- **ProviderBoxes** (`relay/boxes.py`): each provider's program runs in a
  container of its own: Claude Code, Copilot, and all of opencode-manager's
  opencode services in one. Its `/bin/bash` and `/bin/sh` are Relay's
  (`src/relay/container/box/relay-shell`): whatever the program starts as a
  shell (`bash -c`, `sh -c`, a fixed `/bin/bash` like Copilot's), the
  command goes to Relay over a socket, and Relay runs it in the work
  container of the run's project, past the guard; output, input, exit code
  and stopping go along. No provider has to cooperate, and the work
  container never sees a login. A box has the provider's program (from the
  machine, read-only), its login and settings, writable (`~/.claude`,
  `~/.copilot`, opencode-manager's profiles), the workspace roots, Relay's chat and task
  folders, and what Relay hands agents (skills, attachments, handed-over
  transcripts; of the machine's cache only Claude Code's skills folder, no
  tools script); a project elsewhere joins when a run
  needs it. You log in on the machine as always (`relay claude`, `copilot
  login`, opencode's own), or in the provider's **Shell** (Settings →
  Providers, and its repair page). Any provider with a box has one, and it
  opens in that box, with its program and its login, for `claude auth login`,
  `copilot login` or `opencode`'s `/connect`. What you type runs there. What a
  program starts from it, such as a script's `/bin/sh`, goes to a WorkBox as in
  a run, so an installer typed there doesn't install in the box, and outside a
  project such a script says so (`which` is one). Boxes keep running (opencode's
  services live there); one made otherwise (other mounts, a new image) is
  made anew when no run uses it. Every command a box hands over is in
  `~/.cache/opencode-manager/relay-containers/boxes/<box>.commands.log`.
  Your own use of opencode goes through the same services: its commands run
  in the WorkBox of the folder you're in (not your home folder), and
  only while Relay runs.
- **`relay serve NAME CMD`** in a WorkBox starts a server that outlives the
  agent's turn (a session of its own, its log in `/tmp/relay-serve/NAME.log`);
  again restarts it, `--stop NAME` stops it, `--list` lists them with their
  ports. It says which port the server listens on, and where you open it
  ([`/port/<n>/`](#servers-on-this-machines-ports-portn)). Agents are told to
  use it.
- **A command stops with its shell**: Stop, or the end of the run, ends
  what an agent started in the WorkBox, except what it detached on purpose
  (`setsid -f`, in a session of its own). Agents are told to start a server
  you should go on using (a dev server, say) that way, and not in a
  background shell of their own: Copilot doesn't end its turn while one of
  those runs. Such a server keeps running until it's stopped, or until the
  WorkBox is (when idle, or when Relay restarts).
- **WorkBoxes** (`relay/containers.py`): where the commands run, one
  per project folder. Mounted at the same paths: the project folder; when it's
inside a larger git repository, that repository too (git looks above the
folder), read-only but for the folder and `.git`, so other projects in it
stay as they are; the repository's `.git` where a branch task's worktree
keeps its history (a linked worktree's or submodule's only when that repository
names the folder back, so not one made with `git init --separate-git-dir`:
git doesn't work in its WorkBox); opencode's
scratch folder `/tmp/opencode`; a folder of its own for the shells' files
(Claude Code's cwd file); and, read-only, the skills and Relay's
`relay` command. Nothing of other conversations: no tools script with a
token in it (a command gets its conversation's address in its environment,
`RELAY_MCP_URL`, or Relay finds it for the box's own folder, which Relay
signs: `RELAY_BOX_KEY`), no attachments or handed-over transcripts (an agent
reads its own with its program's file tools). The agent is **root** in the
container: with rootless podman that's you outside it, so what it writes in
the project is yours, and it can install what it needs (`apt-get install
nodejs`, `pip`, `npm`). Its home (`/home/relay`) is a volume per folder. A
command that's stopped (a time-out, *Stop*) is ended in the container too;
what it started in the background on purpose keeps running, as it would on
the machine. The agent is told all this in Relay's note.

A folder's container starts on its first answer and stays warm (a command
costs ~0.1 s more). After 3 hours unused it's stopped, not removed: the
next answer starts it again in a moment, with what the agent installed;
after two weeks stopped it's removed, with its home volume. opencode is one
server for all its sessions: a command goes to the container of the folder
it runs in. Each box carries the mark of the Relay that made it (from its data
folder), and a Relay only stops, removes or remakes its own. Another Relay on
the same podman, such as a test run or the UI walkthrough, leaves this one's
boxes alone.

Chat APIs run their commands through Relay's own tools; ACP agents other
than Copilot and `command` providers run any program, which may not run in a
box: they run on the machine, with Relay's shell as `$SHELL` (and first on
their PATH), which hands commands to the WorkBox just the same, as
long as they use it.

**Open your own shell.** Click **Shell** under a conversation's message box,
or type `/shell`. A terminal opens directly in the agent's WorkBox and
working folder (the task's worktree when isolated), not in its ProviderBox or on
the host. For a local chat it uses the chat's scratch folder. The container starts
on demand, including before the first agent turn. Computer tasks on another
machine and discarded tasks do not offer a local shell.

This is interactive Bash: tab completion, Ctrl+C, coloured output and editors
such as `nano` work. Escape goes to the terminal; use its close button to leave.
Tab/Ctrl+C/Esc buttons are also available for touch keyboards. Your draft,
attachments, Options and agent connection stay in place. Closing or disconnecting
ends that shell and its children, not the agent, other shells or the container;
**New shell** reconnects with a fresh shell, keeping the files and installed
software. Open shells keep their container from being stopped as idle. Commands
are yours, not conversation messages or model calls; the existing disaster guard
also blocks `rm -rf /`-like commands without adding approval prompts.

The terminal follows the available window size. If your Podman/OCI runtime
reports `Error resizing exec session … openByHandleAt failed: operation not
permitted`, it cannot resize an existing PTY in that environment. Open a new
shell at the desired window size; the initial size still works. This is a host
container-runtime restriction, not a Relay/provider setting.

The box image (`src/relay/container/Boxfile`: Debian Trixie slim, Python,
git, ripgrep, Node.js 22 with npm, curl; about 430 MB) is built on first use too. The work image is
Relay's own, built on first use from
`src/relay/container/Containerfile` (a few minutes, once; again when
the file changes; about 3 GB): Debian Trixie with Python 3.13 (pip, uv, pytest
with pytest-cov and pytest-xdist, ruff, mypy), git
and gcc (`python:3.13-trixie`), Node.js 22 (NodeSource's packages: npm,
pnpm, yarn, TypeScript, tsx; `n 24` switches version), Go, Rust (cargo), cmake
and ninja, the GitHub CLI, git-lfs, ripgrep, fd, bat, jq, yq, sqlite and the
PostgreSQL client, shellcheck, network tools (dig, ping, nc, ssh), editors
(nano, vi), htop and tmux, Chromium with playwright-core, and podman; amd64
and arm64. Java, ffmpeg, pandoc and LaTeX are left out for their size: an
agent installs them when a project needs them (`apt-get install …`, kept for
that folder).

**Containers in a WorkBox or a ProductionBox:**

- **What works:** podman, also as `docker`, runs containers, builds images
  and runs `docker compose`. A project that ships a Containerfile or a
  compose file runs as it is.
- **No extra rights:** podman runs in a user namespace of its own, with no
  capabilities beyond the box's. The box is only started with `/proc`
  unmasked, so podman can mount one for its containers. What's read-only in a
  box stays read-only: it can't be remounted.
- **Network:** the containers share the box's network and hostname, and the
  box can't make networks of its own.
  - A container's server listens on its own port on the machine: `-p`
    doesn't remap it, and it can't be a port under 1024. It's reached at
    `/port/N/` like any other.
  - `docker compose` (`relay-compose`, on podman-compose) puts every service
    on that network, and a service's name points to 127.0.0.1. So `web:8000`
    reaches web, but two services can't use the same port.
- **Images** are kept in a volume of the box's (`relay-containers-…`),
  removed with the box or the service. `"container_image"` in
`~/.local/share/relay/settings.json` uses another image instead, as it is (without
Node.js and Chromium there's no browser for opencode). `relay doctor` says
whether they're built; `relay doctor --build` builds them.

**A browser for opencode.** opencode's browser tools (`browser.tabs.open`,
`snapshot`, `click`, `fill`, `screenshot`, `console`, `network`, …) are
meant for its desktop app, which connects a browser to a session; elsewhere
they answer "No desktop browser is connected". Relay stands in for the
desktop app: for each opencode session it runs, it attaches a headless
Chromium in the folder's container (`src/relay/container/browser.cjs`, with
Playwright), so the tools work, and `localhost` is the container's (a dev
server the agent started there). Relay holds the connection to opencode
(`opencode_manager/bridge/opencode_browser.py`) and hands the commands to the
browser over stdin/stdout, so nothing in the container can reach opencode's
server. Chromium starts on the first browser command and closes after 20
minutes unused; screenshots land in `/tmp/opencode`, which the container
sees too. Not there: performance traces, CPU and heap profiles, Lighthouse,
dropping files, and `browser.preview` (Relay has no review pane); the agent
is told so when it tries. What the browser does is in
`~/.cache/opencode-manager/relay-containers/browser.log`.

Because of the container, Relay answers the agents' permission requests
itself (see [Providers](#providers)) and nobody is asked. Without a working
podman no agent runs: the answer says why and how to fix it.

**Installing podman.** `sudo apt install podman` (Debian, Ubuntu, Raspberry Pi OS),
`sudo dnf install podman` (Fedora). Relay uses it rootless, as you. Where
that doesn't work at once:

- `newuidmap`/`newgidmap` need their capabilities (`sudo setcap
  cap_setuid+ep /usr/bin/newuidmap; sudo setcap cap_setgid+ep
  /usr/bin/newgidmap`) and you need ranges in `/etc/subuid` and
  `/etc/subgid` (`you:100000:65536`; inside a container, a range within
  its own user namespace).
- Without systemd (e.g. in a container), set in
  `~/.config/containers/containers.conf`: `[engine]`
  `cgroup_manager = "cgroupfs"` and `events_logger = "file"`, and under
  `[containers]` `cgroups = "disabled"` if cgroups can't be used.
- Relay runs containers with `--network=host`, so no network setup (or
  `/dev/net/tun`) is needed; only VPNs for WorkBoxes (below) need
  `/dev/net/tun`.

`podman run --rm --network=host docker.io/library/python:3.13-trixie id`
working means Relay will work too. (With a subuid range smaller than 65536, apt can't
switch to its own user: Relay lets it download as root in its containers.)

### VPNs for WorkBoxes (gluetun)

Settings → Workspaces → **VPN**: WorkBoxes can go through VPNs. The VPN is
[gluetun](https://github.com/qdm12/gluetun), which Relay starts and keeps.

- **Profiles** are your VPN accounts.
  - **Provider and protocol:** choose your VPN provider and, where it has
    both, OpenVPN or WireGuard.
  - **Login:** a username and password for OpenVPN. This is often a
    separate OpenVPN login: Windscribe's config generator, for instance,
    shows one. For WireGuard, your keys and addresses.
  - **Custom:** a WireGuard server of your own, with its address, port and
    public key.
  - **More settings:** any other gluetun setting, one `NAME=value` per
    line, as its wiki has them.
  - **Masked keys:** keys and passwords are shown masked; leave them so to
    keep them.
  - **A changed profile:** the tunnels using it connect anew.
- **Tunnels** say which WorkBoxes go where.
  - **Which WorkBoxes:** project folders (and their subfolders; the most
    specific folder wins), or all the WorkBoxes no other tunnel has.
  - **Exits**, in order: a profile and a place each. The place is a
    country or region and a city, from gluetun's own server list (read
    once per gluetun image). The first exit is where the tunnel connects.
  - **Rotation:** every so many minutes, either a new server at the same
    exit or the next exit. By default not while an agent works in one of
    its WorkBoxes, since a change cuts open connections for a few seconds.
  - **Fallback loop:** when an exit has had no connection for so many
    seconds, the tunnel goes on to the next one, round and round. It tries
    the first exit again after so many minutes (0: it stays).
  - **Ports to pass through:** a server in one of its WorkBoxes is
    reachable from the LAN on these ports only.
  - **Unused, it stops.** After 10 minutes with no WorkBox running in it
    (idle WorkBoxes stop after 3 hours), the tunnel stops: no VPN
    connection is kept for nothing. Its WorkBoxes stay, with what's in them,
    and the first one used again starts it again at its first exit (a few
    seconds).
  - **Saving** a tunnel connects it and shows its public address and where
    that is, or why it didn't connect. *Reconnect* takes a new server;
    *Log* shows gluetun's log.
  - ***What happened*** lists its starts, fallbacks, rotations and problems,
    newest first. It keeps the last 50 per tunnel, over restarts
    (`vpn-events.json` in Relay's data).
  - **Problems become notifications** (ntfy, a webhook: Settings →
    Notifications, the category *VPN tunnel problems*, on by default):
    - it lost its connection and falls back;
    - gluetun stopped;
    - none of its exits connects.
    That's at most once per half hour per tunnel, and once more when it's
    connected again. An exit that doesn't connect at the start, while the
    next one does, is only noted.
- **How it works.** A tunnel is two containers. A holder keeps its network
  (`relay-vpnnet-…`: pasta and the ports). gluetun (`relay-vpn-…`) and the
  tunnel's WorkBoxes join that network (`--network=container:…`).
  - **Switching:** for another exit, gluetun is made anew while the
    WorkBoxes keep their network and go on. A new server at the same exit
    is gluetun's own reconnect.
  - **The kill switch:** while the VPN is down, or between two gluetuns,
    there is no internet in there at all. gluetun's firewall policies stay;
    Relay clears only the last gluetun's rules.
  - **Connected** means gluetun's health server says the tunnel works *and*
    gluetun knows its public address. After a change the health server
    alone can still say yes (a wrong password, say). The control server,
    which knows the address, takes a key of Relay's own
    (`vpn-control.json` in Relay's data).
  - **What stays reachable:** the LAN and Relay itself. Relay is at
    `169.254.1.3`, the machine's loopback as pasta (podman's network) maps
    it there. Relay's tools, `relay`, and the browser work as usual.
- **What restarts.**
  - **Other ports** make the holder anew, and its WorkBoxes start anew the
    next time they're used. What an agent installed in them goes; their
    home folders stay.
  - **A deleted tunnel's WorkBoxes,** and a folder's that no longer goes
    through one, come back on the machine's network (or another tunnel's).
  - **Restarting Relay** leaves a tunnel as it is: under systemd its pasta
    runs in a scope of its own, not Relay's. One an older Relay started lost
    its network with it; Relay sees that and makes it anew (its WorkBoxes
    with it, as for other ports).

### Web search and fetch (SearXNG and a browser)

Relay gives agents a search engine and a web fetch of its own, next to
theirs, which stay. That includes subagents. Settings → General → **Web
search and fetch**.

- **Web search** is a local
  [SearXNG](https://github.com/searxng/searxng), which asks several search
  engines at once.
  - **Chat models** (Perplexity included) get `web_search`.
  - **Claude Code** gets the MCP tool `search`.
  - **Agents with a shell** get `relay search QUERY`, with
    `--category news|it|science`, `--time day|week|month|year`, `--lang`
    and `--page`.
  - **At most 10 seconds** a search (Settings → Limits → Web search): an
    engine that answers later is left out, and the answer names it.
  - **Off:** it can be turned off.
- **Web fetch** reads a page or file as text.
  - **Chat models** get `web_fetch`.
  - **Claude Code** gets the MCP tool `fetch`.
  - **Agents with a shell** get `relay fetch URL`. Agents are told to use it
    when their own fetch is refused or comes back empty.
  - **What it tries**, until one gives the page:
    1. **A plain fetch.**
    2. **A real browser** (Chromium, in Relay's WorkBox image), when the
       plain fetch was refused (403, a bot check, a consent or login wall)
       or came back with next to no text (a page its JavaScript builds).
    3. **Startpage's Anonymous View**, when the site refuses the browser
       too: Startpage fetches the page from its own servers. Relay's
       browser searches Startpage for the address and opens the result
       that is that address in Anonymous View (about 10 s). It comes back
       without Startpage's banner, with the site's own title and links,
       and says so.
       - **It doesn't always work:** a page Startpage doesn't find as
         itself, and a site whose bot check stops Startpage too (DataDome,
         Cloudflare), go on to the archive.
       - **It helps where a site refuses Relay but not Startpage:** Reddit,
         say. It gives the live page, not an old copy.
    4. **The newest copy at the Wayback Machine** that is the page. The
       copies further back, asked at once within 20 s, are for when the
       newest is a copy of a refusal. It says so, with its date.
       - **Neither 3 nor 4** for a page that gave next to no text (a login
         wall, a page only its JavaScript fills): they have the same, so
         that page comes back at once, with a note.
       - **A LAN address never goes to Startpage or the archive:** only a
         public address does.
  - **What it gives:**
    - a page's main content as light markdown (its `<main>`, sidebar
      included; headings, lists, code, tables, links with full addresses);
    - a PDF's text, page by page (pypdf);
    - for an image, what it is.
  - **Long ones** come in parts the size of a tool result (Settings →
    Limits): `offset` gets the rest, from what was kept, without fetching
    again.
  - **Measured on 22 common sites,** the plain fetch of before read 9;
    this one reads 19 (5 through the browser, 4 from the archive). Two
    more are login walls, given with a note; one (Bloomberg) refuses the
    archive too.
- **Where they go out:** both through this server's own connection, or
  through one of your [VPN tunnels](#vpns-for-workboxes-gluetun).
  - **Through a tunnel,** sites see the VPN's address. Some that refuse
    a home address answer there (Reddit did).
  - **A tunnel that's down** means nothing goes out, never around it.
  - **The tunnel stays** while they go through it: it can't be deleted,
    and it stays up while they run.
- **How they run.** Each is a container Relay starts with the first search
  or fetch (a few seconds): `relay-search` (`docker.io/searxng/searxng`) and
  `relay-fetch` (Relay's WorkBox image running `container/fetch.cjs`).
  - **When they stop:** after half an hour unused.
  - **No port:** neither has one on this machine. Relay asks them from
    inside their containers.
  - **SearXNG's settings:** Relay writes them (`searxng/settings.yml` in
    Relay's data, with a secret of its own).
  - **Without podman,** web fetch works in Relay itself, with the plain
    fetch only.
- **Try** a search or an address in Settings to see what agents get.

### Relay under a path of your site

Behind a reverse proxy, Relay can be at a path of a site
(`https://example.org/relay/`) rather than its root: set `"base_path":
"/relay"` in `settings.json`, and have the proxy pass requests on without that
path (`/relay/api/state` reaches Relay as `/api/state`). Relay's page, its
links, its redirects and `/port/<n>/` then have the path. What a browser keeps
for Relay (drafts, the view) is kept per path, so two Relays on one site keep
their own; the theme is the browser's, for all of them.

A new provider's [throwaway Relay](#a-provider-relay-doesnt-know-added-with-an-agent)
is served this way, at `/trial/<id>/` of yours: only for you, signed in
there as you are here (it never sets a cookie in your browser).

### Servers on this machine's ports: /port/<n>/

Behind a reverse proxy (a domain of your own), only Relay's own address can be
reached, not the ports agents' servers listen on (WorkBoxes share the
machine's network). So Relay forwards them (`relay/forward.py`):
`https://<relay>/port/5173/` is port 5173 on this machine.

- **Signed in only:** you need to be signed in to Relay; otherwise you land
  on its sign-in.
- **What's forwarded:** HTTP (streamed: server-sent events, downloads) and
  WebSockets (dev servers' live reloading).
- **ProductionBoxes:** a service's public port goes to its live version.
- **Relay's own port** is Relay itself.
- **Finding them:**
  - links in agents' replies to `http://localhost:5173/` (or `127.0.0.1`, or
    this machine's own address) open there;
  - `relay serve` prints the address;
  - a service's page links it.
- **The app sees itself addressed directly:** Host, Origin and Referer are
  `http://localhost:<n>`, which dev servers like Vite and Next check, and
  Relay's session cookie isn't passed on. It gets the path without `/port/<n>`.
- **Absolute paths** (`/assets/…`, `/@vite/client`, `fetch("/api/…")`) come
  back under the prefix:
  - **Requests:** a request from a page under `/port/<n>/` names that page
    (its Referer), and Relay redirects it there. Relay's own pages send no
    Referer.
  - **WebSockets:** a socket names no page, so one Relay doesn't serve goes to
    the port of the last page forwarded (a cookie).
  - **No base path is needed** for a Vite or Next dev server.
- **An app that lives under `/port/<n>/` itself** (Vite `--base /port/<n>/`,
  Next `basePath`) is noticed: its root redirects there or has nothing. From
  then on it gets the whole path. A single-page app whose own routes show in
  the address bar needs this, or a reload lands on Relay.
- **Behind a VPN tunnel,** a WorkBox's servers can be reached only on the
  tunnel's passed-through ports.
- **Same origin:** such a page shares Relay's origin, so its scripts could use
  Relay as you. Open what your own agents made.
- **Not for other sites:** a request another site's page makes, or another
  port of this machine's, is refused (the browser's `Sec-Fetch-Site`), and so
  is its WebSocket (its Origin). Following a link to `/port/<n>/` from
  elsewhere still opens it.

## Security

Relay protects your system from accidental damage by agents; if an agent tries to escape its container on purpose, it will.

What Relay protects, what it leaves to you, and its settings for it (no login, secure cookies) are in [SECURITY.md](../SECURITY.md).

## Plugins

Relay's core is complete: what's new comes as a **plugin**, a folder in
`~/.local/share/relay/plugins/<name>/` that Relay loads when it starts.
[WRITING-A-PLUGIN.md](WRITING-A-PLUGIN.md) says how to write one; the
built-in skill `relay-plugin-writer` has an agent write one from it.

- **What a plugin adds:**
  - buttons on the activity bar (above the Shell), pages of its own
    (`/p/<name>/…`) and tabs in Settings;
  - API routes (`/api/plugins/<name>/…`, signed in as Relay's own),
    background tasks, and a hook for runs that end;
  - tools for agents, as Relay's own: `<name>_<tool>`, or
    `relay plugin <name>_<tool>` for agents without MCP tools.
- **Adding one:** put its folder (or a link to it) in the plugins folder, and
  restart Relay.
- **Plugins that come with Relay,** in `plugins/`. They're off until you turn
  one on in Settings → Plugins, or link it into the plugins folder, and restart
  Relay, for example
  `ln -s ~/relay/plugins/server ~/.local/share/relay/plugins/server`:
  - [Notes](../plugins/notes/README.md), the example: short notes, yours and
    agents' (`notes_add`);
  - [Usage limits](../plugins/usage_limits/README.md): each usage-limit window
    over time, a notification when one runs low, and a `usage_limits_now`
    tool for agents;
  - [Server](../plugins/server/README.md): this machine's CPU, memory and
    disks, what each container uses, warnings when it gets tight, and a
    `server_status` tool for agents.
  - [ACP](../plugins/acp/README.md): Relay's agents as one ACP agent, for
    OpenHands, Zed, JetBrains, Toad and any other ACP client.
  - [OpenHands](../plugins/openhands/README.md): OpenHands' Agent Canvas in a
    container, working with Relay's agents through the ACP plugin.
  - [VSCodium](../plugins/vscodium/README.md): Relay in VSCodium (and VS Code),
    an extension of its own: your conversations, a chat with any of Relay's
    agents, the agent's changes in Source Control, and the open file and its
    problems going along with what you ask. A folder on Relay's machine (Remote -
    SSH) makes a code task; one on your own computer a computer task, with
    VSCodium as that computer's connector.
  - [Chrome](../plugins/chrome/README.md): Relay in Chrome's side panel, an
    extension of its own (Settings → Chrome downloads it). Ask any of Relay's
    agents about the page, a selection or a screenshot, have a text field
    rewritten and put back, insert an answer where your cursor is, and run your
    scripts from there. Agents in any chat use your own tabs while Chrome runs,
    where you're signed in (`chrome_read`, `chrome_click`, …), each step allowed
    by you in the panel or a notification.
- **Settings → Plugins** lists each one: loaded, off, or can't be loaded with
  the file and line of the mistake, and what failed last. Its switch turns it
  off at once; on again, it's back at once if Relay loaded it at its start
  (else from the next start). Its buttons and pages follow when the page
  reloads.
- **From the command line:** `./relay plugins` lists them, `./relay plugins
  off <name>` turns one off (also when Relay doesn't start), and `./relay
  plugins check <folder>` loads one as Relay would and says what it adds.
- **A broken plugin** never keeps Relay from starting: Relay leaves it out.
  One that's slow runs in threads of its own and never holds up Relay.
- **A plugin runs with Relay's own rights,** outside any box: only add plugins
  you trust, and keep their folders outside the workspace roots
  ([SECURITY.md](../SECURITY.md#plugins)).
- **The example,** Notes ([`plugins/notes/`](../plugins/notes/)), keeps short
  notes: yours on its page, agents' with their `notes_add` tool, and one for
  each run that fails. To try it:
  `ln -s ~/relay/plugins/notes ~/.local/share/relay/plugins/notes`, then
  restart Relay.

## Python SDK

Relay's agents from your own Python code: chats and code tasks, their answers
all at once or as they come, another agent mid-conversation, `/timed`, a code
task's changes; and your scripts, scheduled tasks, live services and
notifications. It's one module with only the standard library (Python 3.9+),
and Relay serves it for pip, so any machine that reaches Relay can install it
(Settings → Account has the command; `/sdk/` has an example):

```sh
pip install http://<your-relay>:8420/sdk/relay_sdk-4.2.0-py3-none-any.whl
```

```python
from relay_sdk import *

user = login("me", "my password", url="http://my-server:8420", provider="claude-code", model="sonnet")
chat = convo.create(user, title="Site check", workdir="/home/me/projects/site",
                    instructions="Answer in Dutch.", tools={"web": False})
message = chat.send("Run the tests and tell me what fails.")
print(message.response)       # the answer's text
print(message.elapsed_time)   # seconds its run took
print(message.tools_called)   # [{"name": "Bash", "input": {…}, "ok": True, "output": "…"}, …]
print(message.text)           # the whole answer as Markdown, its tool calls where they happened
print(message.json)           # the answer as Relay keeps it: its events, status, model, …

chat.switch(provider="opencode", model="opencode/big-pickle")   # the next turn is that agent's
for chunk in chat.send("Fix the first one.", stream=True):       # the text as it comes
    print(chunk, end="", flush=True)

chat.send("Make all the tests pass.", timed=60)                  # /timed: kept at it, up to an hour
for turn in chat.follow():                                       # each turn, until it's quiet
    print(turn.response)
print(chat.changes()["summary"])
chat.commit("Fix the tests")
```

- **Signing in:**
  - `login(username, password, url, provider, model)` makes the default
    session, so `user` can be left out after it;
  - `provider` and `model` are the agent new conversations get; without
    them, your latest conversation's;
  - **An app password** signs a script in without your password:
    `connect(url, app_password="relay_…")`, or `$RELAY_URL` and
    `$RELAY_APP_PASSWORD` in its environment, where the SDK picks them up by
    itself (no `connect()` needed). See [App passwords](#app-passwords);
  - `connect(url)` without one is for a Relay without login; `url` defaults
    to `$RELAY_URL`, else `http://localhost:8420`;
  - on the Scripts page (below) a script is signed in already: no `login()`.
- **A conversation:** `convo.create(…)` returns a `Conversation`, which is
  its id as a string too. `conversation(id)` takes an existing one.
  - `workdir` (a folder on Relay's machine, in a workspace root) makes it a
    code task, `isolate=True` on a branch of its own; else it's a chat.
  - **Also:** `title`, `provider`, `model`, `effort`, `agent_mode`,
    `fallback_loop`.
  - **`instructions`:** its own, after its mode's, for every agent in it;
    the program's own system prompt stays.
  - **`tools`:** Relay's own tools on or off: `web`, `memory`, `subagents`,
    `computers`. The agent's program keeps its own.
  - **`uses_vpn`:** `True` (the only VPN tunnel) or a tunnel's name. Its
    folder goes through it, as Settings → Workspaces → VPN keeps it: a
    chat's folder is its own, a code task's is shared by every task in it.
  - **`temporary=True`:** a [temporary chat](#temporary-chats). `delete()`
    it when you're done, or `chat.keep()` it.
  - **Reading and changing:** `chat.instructions`, `chat.tools` and
    `chat.vpn` (`None` takes it out again) can be read and set.
    `chat.title`, `provider`, `model`, `agent_mode`, `effort`, `mode`,
    `directory`, `running`, `timer` and `queued` can be read; `chat.get()`
    gives all of it.
- **Talking:**
  - `chat.send(prompt, stream=False, files=None, timed=None,
    on_question=None)` returns a `Message`. `files` (paths on your side) go
    along as attachments.
  - `chat.queue(prompt)` sends it once the answer going now is done.
  - `chat.retry()` answers again (`retry(message)` the question of that
    answer); `chat.edit(answer, text)` changes what was asked and answers
    anew; what came after it goes.
  - `chat.fork(at=answer)` returns a new conversation; `chat.clear()` makes
    the agent start afresh.
- **Another agent mid-conversation:** `chat.switch(provider, model, effort,
  agent_mode, fallback_loop)`, from the next turn on. Another provider gets
  the conversation handed over, on its default model unless you say.
- **Turns after the first:** `chat.follow()` yields each turn as it comes
  (`/timed`'s go-ons, a fallback agent taking over at a usage limit, the
  queue), until nothing runs and nothing is queued. `chat.wait()` is all of
  them as a list.
- **`/timed`:** `send(…, timed=90)` (minutes, `"1h30"`, `(min, max)` or
  `(min, max, optimal)`); `chat.stop_timer()` ends it.
- **After a limit:** `chat.go_on(provider, model)`, `chat.resume_later()`
  and `chat.cancel_resume()`.
- **A `Message`:**
  - `response`, `elapsed_time` (Relay's measure, also for one read back),
    `tools_called`, `text`, `json`;
  - `status`, `error`, `usage`, `cost`, `model`, `provider`, `started`,
    `finished`, and `events()` (each event as it comes).
- **Questions:** an agent asking something (a form or a permission) no
  longer leaves a script waiting. `on_question=lambda q: …` gets each one
  and returns the answer: a form's `{key: value}`, a permission's
  `True`/`False` or an option's id, or `...` to leave it. By hand,
  `message.questions` and `message.answer(id, value)`.
- **A code task:**
  - `chat.changes()`: its files with their diffs, and a summary;
  - `chat.rewind(answer)` or `rewind("start")`: its files as they were;
  - `chat.commit(message)`, `chat.merge()` (a task on its own branch),
    `chat.discard()`, `chat.reviewed()`.
- **Your conversations:** `convo.list(query=None)`, newest first, as dicts:
  `id`, `title`, `created` and `updated` (seconds since 1970), `mode`,
  `provider_id`, `model`, `temporary`, `external` (who started it outside
  Relay's pages), and more. `query` searches titles and messages.
  - `chat.get()["messages"]` has both sides: dicts with `role` (`user` or
    `assistant`), `content`, `created` and `run_id`.
  - `chat.messages()` has its answers alone, as `Message`s.
- **The rest:**
  - `chat.messages()`, `rename`, `pin`, `stop`, `delete`;
  - `providers()`, `models(provider)`;
  - `usage(days)`: what it cost;
  - `limits()`: what's left of each provider's usage windows, to pick a
    model with room;
  - `plugins()`: the plugins in Relay's plugins folder, whether each is on and
    loaded, and their tools. A plugin's own API is at
    `/api/plugins/<name>/…`, through `Session.request()`.
- **Your scripts** (the [Scripts](#scripts) page): `scripts.list()`,
  `scripts.get(name)`, `scripts.create(name, text)` (never over one that's
  there; `script.save(text)` saves over it), `scripts.write(description)`
  (an agent writes one, as **With an agent** does).
  - `script.run(args)` returns a `ScriptRun`: iterate it for its output as
    it's written; its `status`, `exit_code`, `output` and `conversations`
    wait for its end; `run.stop()`.
  - `script.move("public")`, `copy()`, `rename(to)`: under the name asked
    for, else Relay picks the next free one (`_2`).
- **Scheduled tasks** (the [AgentOS](#agentos) page): `tasks.create(name,
  when, prompt=…)` or `script=…`, with `when` in words (`"daily 08:00"`,
  `"weekly mon,wed 08:00"`, `"in 10 minutes"`); `task.get()`, `update(…)`,
  `run()` (now: its conversation, or a script's run), `delete()`;
  `tasks.list()`.
- **Live services** (Production boxes): `services.get(name)` gives its
  `status()`, `logs()`, `snapshots()`, and `start()`, `stop()`, `restart()`,
  `rollback()`, `restore(snapshot)`. Making, deleting and promoting one stay
  on its page.
- **Notifications:** `notify(text, title=, priority=, url=)` to your phone
  or webhook ([Notifications](#notifications), the category From your
  scripts). It returns whether it went; when it can't, a warning says why
  and the script goes on.
- **Errors** are a `RelayError` in Relay's own words, with its HTTP
  `status`; a time-out or a lost connection is one too.
- **From 2.1:** `convo.send(convo_id, …)` and the other `convo` functions
  still work; `convo.create(mode=…)` is now `agent_mode=…`.
- **The source** is `sdk/python/` (`pip install ~/relay/sdk/python` on the
  server).

### App passwords

An app password is for one script or program that uses Relay's API or the
SDK, instead of your account's password: a backup script, a shortcut on your
phone, a cron job on another machine.

- **Making one:** Settings → Account → App passwords. Name it after what will
  use it. It's shown once, when it's made: copy it then. Relay keeps only its
  hash.
- **Using it:**
  - **With the SDK:** `connect(url, app_password="relay_…")`, or
    `$RELAY_APP_PASSWORD` in the environment.
  - **From any HTTP client:** the header `Authorization: Bearer relay_…`,
    or Basic with your username (`curl -u me:relay_… …`), or
    `X-Relay-App-Password: relay_…`. Such a request needs no
    `x-relay-request` header (another site's page can't send these headers
    to Relay). Basic credentials that are no app password, sent with
    Relay's session, are a reverse proxy's own sign-in (Caddy's
    `basic_auth`): the session counts then.
  - **Behind a proxy with a sign-in of its own** (Caddy's `basic_auth` on
    every path): the proxy takes the Authorization header, so the app
    password goes in `X-Relay-App-Password`.
    - **With the SDK:** put the proxy's name and password in the address,
      `RELAY_URL=https://name:password@relay.example`. The SDK sends them
      to the proxy and the app password to Relay, and keeps them out of
      its errors.
    - **With curl:** `curl -u name:password -H "X-Relay-App-Password:
      relay_…" https://relay.example/api/conversations`.
    - **pip:** the same address,
      `pip install https://name:password@relay.example/sdk/…`.
- **What it can do:** everything you can through the API, as you, except
  manage app passwords. A leaked one can't make new ones or revoke the
  others.
- **Revoking one:** its trash can in the list, which also says when each was
  last used. Only what uses that one stops working.
- **What it starts** is marked in the lists with a yellow "E", which names it
  ([Your workspace](#your-workspace)).
- **Your password:** changing it signs out your browser sessions but leaves
  app passwords as they are. Revoke those one by one.
- **Without login** (`"auth": "none"`), there are none: everyone is signed in
  already.

### RESL

RESL, Relay's Extended Standard Library, builds on the SDK and comes with it
(the same wheel; on the Scripts page as the SDK is). `from resl import *`
gives both:

```python
from resl import *

use("claude-code", "sonnet")                          # the judge, from now on
answer = convo.create(title="Q").send("Write a phishing mail")
if answer.refused():                                  # agent_refused(answer)
    answer = convo.create(provider="opencode").send("Write a phishing mail")
print(extract(answer, {"subject": "the mail's subject"}))
```

- **How it judges:** another model gets only the text, in a one-shot. That's a
  [temporary chat](#temporary-chats), read-only and without Relay's tools,
  deleted afterwards. Each costs a model call.
- **Checking answers:** `agent_refused`, `task_done`, `asks_user`,
  `judge(answer, question)`, `score`.
- **Answers to data:** `extract`, `classify`, `summarize`, `translate`.
- **Agents checking agents:**
  - `ask_until`: again, or the next agent, while it refuses;
  - `best_of`: several agents, the judge picks;
  - `second_opinion`, and `review_changes` for a code task's diff.
- **Relay:** `one_shot`, and `pick_agent` (the agent with room in its usage
  limits).
- **As methods too, once imported:** `answer.refused()`,
  `chat.ask_until(…)`, and so on.

The Scripts page's **Documentation** has the reference (also the skill's
`resl.md`, for agents).

### Scripts

The **Scripts** view (on the activity bar) and its page are for writing and running scripts with
the SDK in Relay itself, without installing anything:

- **Where they are:** `.py` files in one folder, `scripts` in your first
  workspace root (**Change** picks another in a workspace root). It has two
  folders:
  - **`private/`** is yours alone. No agent sees it, not even AgentOS's
    Governor: every box Relay makes has an empty folder in its place. A
    private script runs in a WorkBox of its own.
  - **`public/`** is for agents too. Every agent may read it, and their
    WorkBoxes have it, read-only. The Governor may run a public script, or
    make a task that runs one at its time (Relay runs it, as you, with no
    agent).
  - **Moving between them:** a new script is private. **Make public** and
    **Make private**, in a script's menu, move it.
  - **The root:** only those two folders and `DO_NOT_PUT_FILES_HERE`. A file
    put there isn't a script.
  - **From before 2.6:** the scripts that were in the root (and their other
    files) moved into `private/`, the safe side.
  - **One limit:** an agent whose program runs on this machine itself, outside
    a box, can read what you can.
- **Examples:** `public/examples/` holds Relay's example scripts, with the
  SDK and with RESL. They're under **Examples** on the page.
  - Relay keeps them up to date: each new version writes them anew.
  - On the page they're read-only. **Copy to my scripts** makes one yours to
    change; run them as they are.
- **Starting one:**
  - **New**: a small one to start from;
  - **From a template**: one of ten working scripts with their settings at
    the top: one question, a job in a folder with its changes and a commit,
    an agent kept at it with `/timed`, several models side by side, the
    provider with room left in its usage limits, four with [RESL](#resl),
    and one that looks at your live services and notifies you when one
    isn't well;
  - **With an agent**: say what it should do, and the agent chosen under the
    message box writes it, in a code task in `public/` with the built-in skill
    `relay-sdk-template-writer`. You watch it work; the script shows up under
    Public (**Make private** moves it).
- **Writing:** an editor with line numbers and Python's colours. Tab indents,
  Ctrl+S saves, Ctrl+Enter runs. What isn't saved is kept while you're
  elsewhere in Relay. A script an agent changes meanwhile is taken in, or
  said when you have changes of your own.
- **In VS Code** (or any editor on your computer): **Open in VS Code** shares
  the scripts' folder as a network drive, as `/mount` does a code task's
  folder (WebDAV, under an address that's its key; read-only if you like).
  - **Opening it:** open the drive on your computer (the dialog has the steps
    for Windows, macOS and Linux), then File → Open Folder in VS Code.
  - **Back on the page:** what you save there shows here.
  - **Completions:** `pip install` the SDK for the Python VS Code uses (the
    dialog has the command).
- **Documentation**, beside the page: how a script runs here, then the SDK's
  reference (the one agents read too, from the skill).
- **Running:** **Run**, with arguments if you like (`sys.argv`). It runs in
  its folder's WorkBox, with the SDK on its path, signed in as you: a session
  of its own that ends with the run. The token is in its environment only
  (`RELAY_TOKEN`), never in a file or a command line.
  - The folder isn't on a script's import path, so a script called
    `random.py` hides nothing from the others; each script is whole in its
    file.
  - **Packages:** a script names the ones it needs in a `# /// script` block
    at its top (PEP 723, `# dependencies = ["requests"]`). Before each run
    Relay installs them with pip, in the WorkBox the script runs in, for its
    user: they stay in that WorkBox's home, so only the first run waits.
  - Times in it are the machine's: WorkBoxes have its time zone.
- **What you see:** its output as it's written, how it ended (exit code,
  time), and the conversations it started, as links.
- **Stop** ends it, and stops the agents of the conversations it started
  that still work. So does its time limit (Settings → Limits → Scripts, 2
  hours by default).
- **Its runs:** each script keeps its last 20, with their output (the last
  200,000 characters). A restart of Relay waits until no script runs.

## Settings

Every settings page uses the same heading, sections, field spacing and action
rows. Desktop navigation lists all its categories; on a phone, **Category**
selects a page without scrolling through tabs. Limits uses two field columns
on desktop and one on smaller screens, with every group still open. Changing
the window size keeps the current page and any unsaved form edits.

`~/.local/share/relay/settings.json` (all optional):

```json
{
  "host": "0.0.0.0",
  "port": 8420,
  "workspace_roots": ["/home/pi/projects"],
  "chat_root": "",
  "auth": "password",
  "secure_cookies": false,
  "session_days": 365,
  "ntfy_url": "",
  "ntfy_token": "",
  "webhook_url": "",
  "notify_min_seconds": 30,
  "notify_vpn": true,
  "notify_categories": {"agents.failed": {"push": true, "priority": "default"}},
  "public_url": "",
  "base_path": "",
  "container_image": "",
  "scripts_folder": "",
  "plugins_off": [],
  "search": {"enabled": true, "tunnel": ""},
  "vpn": {"profiles": [{"id": "…", "name": "…", "env": {"VPN_SERVICE_PROVIDER": "…"}}], "tunnels": [{"id": "…", "name": "…", "folders": [], "all": false, "ports": [], "exits": [{"profile": "…", "place": {"SERVER_CITIES": "…"}}], "rotate": {"minutes": 0, "to": "server", "idle": true}, "fallback": {"after": 60, "back": 30}}]}
}
```

### System

Settings → **System** shows what Relay runs on this server, and what it
takes. It looks again every 10 seconds while it's open.

- **This server:**
  - memory and swap;
  - the processor's load against its cores;
  - the disk Relay's data is on;
  - Relay itself.
- **Relay's boxes:** only this Relay's, since podman may be shared with
  another one. They are grouped (providers' boxes, WorkBoxes,
  ProductionBoxes, VPN tunnels, web search and fetch), each with what it's
  for and its CPU and memory now.
  - **What a box is for:** a WorkBox names its conversation (a link to it)
    or chat, a subagent's copy, or the Governor. A ProductionBox names its
    service and version.
  - **Stop** stops an idle box; it starts again when it's needed. It isn't
    offered while a run, an agent or a terminal is using the box.
  - **Remove** removes a stopped WorkBox, one at a time or all of them.
    What an agent installed in it goes; the folder stays.
  - **ProductionBoxes** are their service's: they are started and stopped
    there.
- **Storage:** podman's images, containers and volumes, with what's unused.
  - **Each image** says whose it is and whether a container uses it.
  - **One nothing uses** (an older build of Relay's, the VPN's or web
    search's image while they're off) can be removed; Relay builds or
    fetches it again when it's needed.

### Limits

Settings explain which changes save immediately (theme, provider enable/disable,
fallback loops and subagent rules) and which need **Save** (workspace roots,
Limits and push notifications). These Save actions stay disabled until they
finish; errors allow retry. On touchscreens Enter adds a line; use the
Send button. With a keyboard, Enter sends and Shift+Enter adds a line.

**Settings → Limits** holds what used to be fixed in the code, each with its
default and range: subagents (how many at once and per answer, their time
limit, how long a `wait` and a question to you last), chat models' tools
(rounds, output, command time), usage limits (resume code tasks or not, the
margin, and what Relay says to make the agent go on), how much of a
conversation goes along when another agent takes over, attachments (size,
number), model tests (time limit, prompt), a script's time limit (the
Scripts page) and how long a temporary chat left behind stays. Changes apply after **Save** and
are stored in `settings.json` under `"limits"` (only what differs from the
default). Relay's address and how long a sign-in lasts are there too; host,
port and login stay in `settings.json`, since they need a restart. Safety
rails, like the commands no agent may run, aren't settings.
