# Security

**Relay protects your system from accidental damage by agents; if an agent tries to escape its container on purpose, it will.**

Relay lets AI agents run commands on your machine. This page says what it's built for, what protects what, and what
it leaves to you. Read it before you run Relay anywhere but a network you trust.

## What Relay is built for

**One person, on their own machine, on a network they trust:** a home server on the local network, or reached
through a VPN such as Tailscale. Within that, Relay puts usability first:

- **Agents don't ask before they act.** They run commands and change files without a prompt per command. What
  holds them is where they run (containers), not questions to you.
- **No hardening you didn't ask for:** no roles, no per-command approvals, no audit for several users.
- **One hard rail:** commands that can't be undone (below) are refused, always.

Relay is **not** made to face the internet directly, nor to be shared by people who don't trust each other.

## What protects what

### Signing in

- **The first visitor creates the account.** Create yours right after Relay first starts: until then, anyone who
  can open it can claim it.
- **Passwords** are hashed with scrypt. The session is an HttpOnly, SameSite cookie, and failed sign-ins are
  throttled.
- **A forgotten password:** `./relay user password NAME` sets a new one and signs out all of that account's
  sessions.
- **App passwords** (Settings → Account) sign scripts and programs in, apart from the account's password:
  - each is shown once, kept only as a hash, and revoked on its own;
  - one can do what you can through the API, but not manage app passwords;
  - a new password leaves them as they are.

### Other sites in your browser

- **Every request that changes something** must carry the `X-Relay-Request` header, or an app password in the
  `Authorization` header. A page on another site can send neither to Relay without permission, and Relay grants
  none (no CORS).
- **Relay's pages** have a strict Content-Security-Policy: no inline scripts, nothing loaded from elsewhere, not to
  be framed.
- **Shells in the page** refuse a WebSocket from another site's page. That's checked by host, so it works behind a
  reverse proxy that ends HTTPS.

### Agents and their commands

Agents run in rootless podman containers.

- **The program:** each provider's program (Claude Code, opencode, …) runs in a ProviderBox. That box holds the
  program and its own login, e.g. `~/.claude` for Claude Code.
- **Its commands:** every shell command it runs goes through Relay's exec gateway into the project's WorkBox. The
  agent is root in there, which is your own user outside it. The gateway runs commands only in folders the box has as
  projects: never in its login's folder, or in your private scripts.
- **Your own Shell in that box** (Settings → Providers) has what the box has, its login too. What you type runs in the box,
  past the guard. What a program starts from it still goes to a WorkBox.

**What an agent can change** is what its WorkBox mounts:

- **Its project:** the project folder, or the git repository it's in, including `.git`.
- **Read-only:** Relay's tools and skills.
- **Claude Code's own file tools** run in its ProviderBox. Relay answers their permission requests: changes are
  allowed only inside the project.

**What that doesn't hold is an agent trying to get out.** The containers are there so that a mistake stays in its project.
They aren't a wall against an agent that works at it. Some ways out are known, and there will be others:

- **Relay runs git on your machine,** in a project's repository, and the agent can write that repository's `.git`. A
  command it puts there (`core.fsmonitor`, a filter, a diff driver) runs as you, at the end of its turn or when you look
  at its changes.
- **A provider's program reads settings from the project,** so an agent can get a program of its own started in the
  ProviderBox, next to the program's login.

**What this means for you:**

- **What an agent reads can talk it into trying.** A web page, a file or an issue can hold text written for that. Keep that
  in mind before you point an agent at something you don't trust.
- **If you need a wall,** run Relay in a virtual machine, or on a machine with nothing on it you can't lose.

**What an agent can reach on the network** is what your machine can reach, LAN and the internet alike. WorkBoxes
share the machine's network, so services listening on `localhost` are in reach too.

- **A VPN for a folder:** put the folder's WorkBoxes behind one (Settings → Workspaces → VPN) and their traffic goes
  through it instead.
- **Relay's own API** is in reach as well. Signed in, an agent holds only its conversation's tools token. That
  token gives it Relay's tools (subagents, memory, web search), not your session.

**Commands no agent may run.** Some commands can't be undone. Relay refuses them whatever the agent, mode or
setting. They are:

- wiping the file system, the home folder or a system folder (`rm -rf /`, `rm -rf ~`, `rm -rf $EMPTY/*`, where an
  empty variable makes it `/*`);
- formatting or overwriting a disk, and fork bombs;
- the Windows counterparts (`format c:`, `rd /s /q C:\`, `Remove-Item -Recurse C:\`).

The check (`relay.guard`) sits on every path a command takes:

- the shell every agent's commands go through;
- Claude Code's PreToolUse hook, next to your own hooks;
- opencode's and ACP agents' requests to run a command;
- `run_command` for chat models;
- your own computers, before a command is sent.

The agent is told why.

**Provider code an agent writes** (a repair, a provider kind built with an agent) is tested in a box of its own,
never on the machine. The box has only:
- a copy of Relay with the agent's files;
- the provider's own template (not your others, with their keys);
- what the provider asks to have mounted, checked first: a program read-only, a writable folder only in your home
  folder or Relay's data; never your keys and credentials (`~/.ssh`, `~/.gnupg`, `~/.aws`, …), Relay's database,
  settings, templates or code, or your workspace roots.

Putting a repair into the running Relay (Apply) is always yours, after you've tried it. A repair has a throwaway
Relay too, as a new provider has (below): the repaired layer, with the provider as you set it up.

**A provider added with an agent** (Settings → Add provider → Add it with an agent) is yours from start to end:
- **You install it** in a box of its own, in a shell with real shells and its own home
  (`~/.local/share/relay/provider-homes/<id>`): only that home of your machine's is in it.
- **You approve its agent's plan.**
- **A throwaway Relay with the new layer:** once the layer passed the whole check in a test box, Relay starts one for
  you to try it in. Like Relay itself once the provider is added, that Relay runs the layer's code as you, on the
  machine, not in a box: a Relay needs podman, which a box hasn't. Its files are on the page.
  - It has only the new provider, its own data and its own boxes.
  - It listens on `127.0.0.1`, and is reached at `/trial/<id>/` of yours, by you alone, signed in there by Relay.
  - It goes when you add the provider or cancel.
- **Add** (its folder into Relay's code) is yours.

**AgentOS's Governor** may add providers only of kinds whose program runs in a box of Relay's (or a chat API),
never with a secret of yours, and removes only what it added. A program Relay doesn't know it can't have built:
it sends you to add it with an agent. It may start a repair, whose plan, trial and Apply are yours. It sees and changes only what AgentOS made. Details are in the guide:
[What holds the Governor](docs/GUIDE.md#what-holds-the-governor).

### Your own computers

**The connector** (`relay-computer.py`) lets agents run commands and read and write files on another computer, as
the user who started it.

- **On that computer:** every command shows in its window, and `--ask` confirms each one.
- **To stop it:** close the window, and agents can't reach that computer any more.

### Servers at `/port/<n>/`

A server on the machine's ports is reachable through Relay at `/port/<n>/`, for someone signed in.

- **Same address as Relay:** these pages are served from Relay's own address, without Relay's protections. A
  server's page could therefore act with your Relay session.
- **What Relay does about it:** a page's calls to Relay's API go to its own server instead. A page that hides its
  referrer still reaches Relay itself.
- **Open there only servers you trust:** your agents' dev servers and your own services.

### Network drives

A code task's folder (or the scripts' folder) can be opened as a network drive (WebDAV), at `/dav/<token>/`.

- **The address is the key:** anyone who has it can read the folder, and write it unless the drive is read-only.
- **Revoking it:** remove the drive in Settings, and its address stops working.
- **Its pages** are served sandboxed.

### Relay's data

**`~/.local/share/relay`** holds the database, chats, uploads and the providers' API keys. opencode's logins are
there too.

- **Its permissions:** Relay makes the folder yours alone (`700`). `./relay doctor` warns when other users of the
  machine can read it.
- **Backups:** `./relay backup` archives contain the same secrets.

### Plugins

**A plugin** ([WRITING-A-PLUGIN.md](docs/WRITING-A-PLUGIN.md)) is code that runs in Relay's own process, with
Relay's rights, outside any box. It can read and change everything Relay can: your data, your logins, the
machine as your user.
- **Only you add one:** a folder you put in `~/.local/share/relay/plugins/`. Relay never adds one by itself, and
  agents can't reach that folder from their WorkBoxes.
- **A link is as safe as where it points.** A plugin linked from a workspace root is one agents could change.
  Settings → Plugins warns about such a plugin: keep plugins outside the workspace roots.
- **Only add plugins you trust,** as you would any program you run. Settings → Plugins turns one off at once, and
  `./relay plugins off <name>` does too when Relay doesn't start.
- **Its API** (`/api/plugins/<name>/…`) and its page's files (`/plugins/<name>/static/…`) need the same sign-in as
  Relay's own.
- **Its tools** run on the machine with Relay's rights, with arguments an agent gave. The guide tells plugin
  authors to treat those as untrusted and gives them `within()` for paths.
- **A web app it forwards** (`plugin.forward`) is at a path of Relay's, behind Relay's sign-in, as `/port/<n>/`
  is. It shares Relay's origin, so its scripts can use Relay's API as you.
- **Its own app password** ("plugin <name>" in Settings → Account) is made when it first uses Relay's API through
  the SDK. It's kept outside the plugin's data. Revoking it is safe: the plugin gets a new one the next time.
- **The plugins that come with Relay** in `plugins/` are off until you turn one on. These open a way in, or act for
  you outside Relay's boxes:
  - **ACP** (`plugins/acp`): its forwarder lets an ACP client use Relay's agents with an app password you make for
    it. Whoever has that password can run your agents, as with any app password.
  - **OpenHands** (`plugins/openhands`): OpenHands' UI and API are reached only through Relay, after Relay's
    sign-in (`/canvas`, `/port/<port>/`): its port is on 127.0.0.1, so its API key is in its page. Like any page
    under Relay's address, it can use Relay's API as you (its Relay app does). Its container sees only your
    workspace roots (your private scripts masked), and works with Relay through an app password "OpenHands".
    OpenHands would allow whatever an agent asks by itself, so what Relay asks you to allow stays in Relay
    (`--ask-in-relay`).
  - **Chrome** (`plugins/chrome`): while Chrome runs with its extension signed in, agents read the pages you allow
    in your own browser, where you're signed in. Clicking, typing and opening ask each time, unless you chose
    **Always** for a site. A page can hold text written to talk an agent into something, so keep **Always** for
    sites you trust. The extension keeps an app password.
  - **VSCodium** (`plugins/vscodium`): the extension can be your computer's connector, so agents run commands and
    change files there as you (see [Your own computers](docs/GUIDE.md#your-own-computers)). VSCodium asks before each
    command and change unless you set `relay.permissions` otherwise. It turns itself on as you sign in when Relay
    already has a computer of this one's name, else it asks once. It keeps an app password in VSCodium's secret
    storage.
- **A misbehaving plugin** (a slow route, a task that never ends, a setup that hangs) runs in threads of its own,
  with time limits. It never holds up Relay's own work.

## What's left to you

- **HTTPS.** Relay serves plain HTTP. For anything beyond your LAN, put it behind a VPN (e.g. `tailscale serve`) or
  an authenticating reverse proxy that ends HTTPS. Then set `"secure_cookies": true` in
  `~/.local/share/relay/settings.json`.
  - **A proxy on another machine:** `RELAY_FORWARDED_ALLOW_IPS=<its address>` lets Relay believe its `X-Forwarded-*`
    headers.
  - **A proxy's own sign-in** (Caddy's `basic_auth`, nginx's `auth_basic`) works alongside Relay's. The browser
    sends its Basic credentials with every request; they're no app password, so Relay's session counts as without
    them, and changes still need the `X-Relay-Request` header. Scripts behind it send their app password in
    `X-Relay-App-Password`, which another site's page can't send either.
- **Who's on your network:** anyone who can reach Relay's port (8420, on all interfaces by default) can try to sign
  in. `"host": "127.0.0.1"` in `settings.json` limits it to the machine itself, e.g. behind a proxy on the same
  machine.
- **The agents' providers:** their accounts, keys and what they cost are between you and them.

## Settings that lower the bar

**`"auth": "none"`** in `settings.json` turns signing in off. Then everyone who can open Relay can run agents on the
machine. So can the agents themselves: they reach Relay's API from their WorkBoxes, outside their tools and
AgentOS's limits. Use it only on a machine nobody else can reach.

## Reporting a vulnerability

Please don't open a public issue for a security problem.

- **Where:** report it privately, through the hosting site's private vulnerability reporting. On GitHub that's the
  repository's **Security** tab → **Report a vulnerability**.
- **What to send:** what an attacker needs (network access? an account? a malicious page or repository?), what
  they can do with it, and how to reproduce it.

Relay is a personal project kept up in spare time. There's no bounty, and fixes come with the next release.

**In scope:** ways around the protections above. Some examples:

- a command an agent runs in the ordinary way doing damage outside its project, such as one that gets past the guard;
- another site acting with your session;
- a way in without the password.

**Not in scope:** what this page says Relay doesn't do. For example, an agent getting out of its container on purpose, an
agent using the network it's given, or `"auth": "none"` letting people in.
