# What Relay does

Every feature on one page, each with a link to where it's explained. For what Relay is and how to install it, see the
[README](../README.md); [the guide](GUIDE.md) is the full reference.

## Contents

- [Conversations](#conversations)
- [Code tasks](#code-tasks)
- [Providers](#providers)
- [Containers](#containers)
- [What every agent shares](#what-every-agent-shares)
- [AgentOS](#agentos)
- [Services and ports](#services-and-ports)
- [Your own computers](#your-own-computers)
- [Scripts and the SDK](#scripts-and-the-sdk)
- [Plugins](#plugins)
- [The web interface](#the-web-interface)
- [Settings](#settings)
- [Running it](#running-it)

## Conversations

- **Chats with any agent or model,** their text, thinking and tool calls as they come. Runs go on when you close the
  tab, and the conversation catches up when you're back. ([Chat and code modes](GUIDE.md#chat-and-code-modes))
- **[Edit, fork, switch and retry](GUIDE.md#edit-fork-switch-retry):** edit a message and have it answered anew, fork at
  any answer, switch provider or model mid-chat (the conversation is handed over), reply to a selection, export as
  Markdown, turn a chat into a code task.
- **[Commands](GUIDE.md#commands):** `/mode`, `/model`, `/effort`, `/thinking`, `/details`, `/help`, `/new`, `/fork`,
  `/clear`, `/code`, `/mount`, `/shell`, `/rewind`, `/schedule`, `/retry`, `/stop`, `/loop`, `/timed`, `/rename`,
  `/export`, `/usage` and `/promote`, and each skill or agent's own command as `/name`.
- **[`/timed`](GUIDE.md#commands):** keep an agent at a task for a set time.
- **[Modes](GUIDE.md#modes):** Build, Plan, Brainstorm, Read-only, Fast and your own, per conversation.
- **[The queue](GUIDE.md#queue):** messages typed while an agent works are sent when it's done.
- **[Questions](GUIDE.md#questions):** an agent can ask you with choices or a form.
- **[Temporary chats](GUIDE.md#temporary-chats)** leave no trace unless you keep them.
- **[Attachments](GUIDE.md#attachments):** images, PDFs and other files, pasted or dropped.
- **[Notifications](GUIDE.md#notifications):** ntfy, a webhook or Discord, per category, when an agent is done or needs you.
- **[What a message costs](GUIDE.md#what-a-message-costs):** tokens and cost per answer, per conversation and over time.

## Code tasks

- **Point an agent at a project folder.** It works in the folder itself or, through the API, on its own branch in a
  worktree.
  ([Chat and code modes](GUIDE.md#chat-and-code-modes))
- **[Every turn is recorded](GUIDE.md#every-turn-is-recorded-commits-are-yours):** see what each answer changed, in a
  diff, and rewind to any point. Relay never commits by itself: commit, merge or discard is your click.
- **[The folder on your computer](GUIDE.md#open-a-tasks-folder-on-your-computer):** a task's folder as a network drive
  (WebDAV), or with sshfs.
- **A shell** in the task's container, in the browser. ([Your workspace](GUIDE.md#your-workspace))

## Providers

- **[Which agents](GUIDE.md#providers):** Claude Code, opencode (its profiles and many model providers),
  [GitHub Copilot and other ACP agents](GUIDE.md#github-copilot-and-other-acp-agents), any OpenAI-compatible API with
  [Relay's tools](GUIDE.md#tools-for-chat-apis), [Perplexity](GUIDE.md#perplexity-with-your-subscription), and
  [any program that prints Relay's events](GUIDE.md#adding-any-agent-with-command).
- **[One Relay doesn't know](GUIDE.md#a-provider-relay-doesnt-know-added-with-an-agent):** an agent builds its support
  layer on a page of its own, in a box where you install and sign in, checked by Relay and tried in a throwaway Relay.
  By hand: [Adding a provider](ADDING-A-PROVIDER.md).
- **[Repairing one](GUIDE.md#repairing-a-provider)** that keeps failing, with an agent and a test before you apply it. A lost
  login is said as one, with a shell to sign in again.
- **[Its own Claude Code login](GUIDE.md#its-own-claude-code-login),** apart from the machine's.
- **[Which models work](GUIDE.md#which-models-work):** each model's test results and recent failures.
- **[Fallback loops](GUIDE.md#when-a-model-stops-fallback-loops-and-going-on-by-itself):** when a model hits its usage limit,
  the next one takes over, or the same one goes on when its limit resets.

## Containers

- **[Boxes](GUIDE.md#containers):** each agent's program in a ProviderBox, its commands in a WorkBox per project folder
  (rootless podman, root inside), with a guard that refuses commands like `rm -rf /`.
- **[A VPN per folder](GUIDE.md#vpns-for-workboxes-gluetun)** (gluetun): profiles, exits, rotating, falling back.
- **[Web search and fetch](GUIDE.md#web-search-and-fetch-searxng-and-a-browser)** of Relay's own: SearXNG, and a browser for
  pages that need one.
- **[What protects what](../SECURITY.md).**

## What every agent shares

- **[Memory](GUIDE.md#memory):** one place for what agents should remember, general or per project, whichever provider.
- **[Skills](GUIDE.md#skills)** in the Agent Skills format: Relay's own and yours.
- **[Subagents](GUIDE.md#subagents):** any agent can start others, on any provider or model, each in its own copy of the
  folder, with a policy for what may start unasked.
- **Web search and fetch,** the computer tools and plugins' tools. Per conversation, you choose which it gets.

## AgentOS

- **[The Governor](GUIDE.md#agentos):** an agent that carries out tasks while you're away, on a schedule, standing, or when
  you ask. It starts and steers other agents, reviews their work and asks you when it must, as a Needs-you item.
- **Tasks:** on a schedule or standing, yours, Relay's or the Governor's own.
- **[Its capabilities](GUIDE.md#agentos):** what it may do, one by one, with an audit trail and a policy for what can't be
  undone.
- **[Its settings](GUIDE.md#the-governors-settings):** approved models, how often it looks around, a budget per wake-up.
- **[What holds it](GUIDE.md#what-holds-the-governor):** the limits on what it can reach.
- **[Model ranking](GUIDE.md#model-ranking):** the Governor keeps a ranking of which models suit which work.
- **[Leaving it out](GUIDE.md#leaving-agentos-out):** one switch.

## Services and ports

- **[Services](GUIDE.md#services):** a project becomes a live service behind Relay, in a ProductionBox: versions, health
  checks, rollback, daily snapshots of its data, promote and demote.
- **[Servers on this machine's ports](GUIDE.md#servers-on-this-machines-ports-portn):** any dev server at `/port/<n>/` on Relay's
  address, signed in.
- **[Relay under a path of your site](GUIDE.md#relay-under-a-path-of-your-site),** behind a proxy.

## Your own computers

- **[Agents on your other machines](GUIDE.md#your-own-computers):** a small connector (`relay-computer.py`, or the VSCodium
  extension) lets agents run commands and read and write files there, as you, while it runs. It works from behind a
  router and behind a proxy's own sign-in.
- **Computer tasks:** a conversation about one computer.
- **Ask first:** per computer, or in VSCodium with a diff of each change.

## Scripts and the SDK

- **[The Python SDK](GUIDE.md#python-sdk):** conversations, scripts, tasks, services, notifications, usage and limits, from
  your own code, on any machine ([its README](../sdk/python/README.md)).
- **[App passwords](GUIDE.md#app-passwords)** for scripts and clients, also behind a proxy's own sign-in.
- **[RESL](GUIDE.md#resl):** one-shot judges and helpers on top of the SDK.
- **[The Scripts page](GUIDE.md#scripts):** write and run SDK scripts in Relay, private or public, with templates, packages
  from PyPI, run history and a schedule. The folder opens in VS Code as a network drive.

## Plugins

Anything new comes as a [plugin](GUIDE.md#plugins): a folder Relay loads, with buttons, pages, Settings tabs, API routes,
background tasks, hooks and tools for agents ([Writing a plugin](WRITING-A-PLUGIN.md)).

### Plugins that come with Relay

They're in `plugins/` and off until you turn one on in **Settings → Plugins** and restart Relay.

| Plugin | What it does | Agents' tools |
|---|---|---|
| [Notes](../plugins/notes/README.md) | the worked example: short notes, yours and agents' | `notes_add` |
| [Usage limits](../plugins/usage_limits/README.md) | each usage-limit window over time, a notification when one runs low | `usage_limits_now` |
| [Server](../plugins/server/README.md) | the machine's CPU, memory and disks, what each container uses, warnings | `server_status` |
| [ACP](../plugins/acp/README.md) | Relay's agents as one ACP agent, for Zed, JetBrains, OpenHands, Toad and others | |
| [OpenHands](../plugins/openhands/README.md) | OpenHands' Agent Canvas in a container, working with Relay's agents | |
| [VSCodium](../plugins/vscodium/README.md) | Relay in VSCodium and VS Code: chat, review, Keep/Undo per change, inline editing (Ctrl+I), permission control, your computer as a connector | `vscodium_problems`, `vscodium_definition`, `vscodium_references`, `vscodium_symbols`, `vscodium_hover` |
| [Chrome](../plugins/chrome/README.md) | Relay in Chrome's side panel: ask about a page, rewrite a text field; agents use your own tabs, each step allowed by you | `chrome_tabs`, `chrome_read`, `chrome_open`, `chrome_click`, `chrome_type`, `chrome_scroll`, `chrome_screenshot` |

## The web interface

- **[Your workspace](GUIDE.md#your-workspace):** an activity bar with Chats, Tasks, AgentOS, Governor, Scripts and Production
  boxes, plugins' pages, a usage ring, and light and dark themes.
- **Ctrl+K** switches to any chat by its title or what was said in it. **Ctrl+Shift+O** starts a new one. **Shift+Tab**
  cycles the modes.
- **[On your phone](GUIDE.md#on-your-phones-home-screen):** add it to your home screen. It needs no internet: its libraries are
  in the repository.

## Settings

[General](GUIDE.md#settings), [Providers](GUIDE.md#providers), [Modes](GUIDE.md#modes), [Subagents](GUIDE.md#subagents),
[Computers](GUIDE.md#your-own-computers), [Skills](GUIDE.md#skills), [Memory](GUIDE.md#memory),
[Usage](GUIDE.md#what-a-message-costs), [AgentOS](GUIDE.md#leaving-agentos-out), [System](GUIDE.md#system) (the machine and
Relay's boxes), [Plugins](GUIDE.md#plugins), [Workspaces](GUIDE.md#your-workspace) (workspace roots, network drives, VPNs),
[Limits](GUIDE.md#limits) (Relay's limits and texts), [Notifications](GUIDE.md#notifications) and
[Account](GUIDE.md#app-passwords), plus a tab for each plugin that has one.

## Running it

- **[Installing](../README.md#install),** as a service, in the foreground, or from a bundle on a machine without git.
  [From source](BUILDING.md).
- **[`./relay` on the command line](GUIDE.md#running-relay):** `serve`, `doctor`, `backup`, `port`, `user`, `providers`,
  `provider-check`, `skills`, `plugins`, `claude`, `systemd-unit`.
- **Backups:** one archive of all of Relay's data, while it runs. ([Running Relay](GUIDE.md#running-relay))
- **[opencode-manager](opencode-manager.md):** the tmux workspace for opencode that Relay builds on, usable on its own.
