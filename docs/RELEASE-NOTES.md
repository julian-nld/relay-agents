# Relay 4.2.0 — release notes

Relay 4.2.0 (2026-11-03) brings two plugins, and code and docs ready for a public repository. Relay itself got what
the plugins needed, a way to sign a provider in again from its page, and fixes.

**Two plugins**, in `plugins/` next to the others. To turn one on, link it into Relay's plugins folder and restart
Relay. Each one's README says more.
- **VSCodium** (`plugins/vscodium`, extension 1.2.1): Relay in VSCodium and VS Code.
  - **The chat and review:** your conversations, a chat with any agent, the agent's changes in Source Control, and
    Keep/Undo per change in the editor.
  - **Inline editing** (Ctrl+I), with Accept and Reject.
  - **Agents use VSCodium:** its language servers answer `vscodium_problems`, `vscodium_definition`,
    `vscodium_references`, `vscodium_symbols` and `vscodium_hover`.
  - **Your computer:** VSCodium can be its connector, and asks before each command and change, a change shown as a diff
    (`relay.permissions`).
- **Chrome** (`plugins/chrome`, extension 1.4): Relay in Chrome's side panel. Agents in any chat use your own tabs while
  Chrome runs, each step allowed by you, and a text field can be rewritten with Relay.

**In Relay itself:**
- **The first start:** the page says while Relay builds its containers' images, as chats and models wait for them.
- **Commit** says what it takes beyond the task's own changes, and as whom, and after it the task leaves Needs review.
- **Adding Claude Code or Copilot:** the page has a shell running it to log in, checks the login with a test message,
  and asks before adding one whose login hasn't worked.
- **Settings → Plugins** lists the plugins that come with Relay, with **Turn on**, and shows one just added.
- **A stopped answer** says why when it wasn't you (a restart, say).
- **A Shell in a provider's box:** every provider with a box (Claude Code, Copilot, opencode's profiles, Perplexity) has a
  **Shell** in Settings → Providers and on its repair page, in that box. Sign in again there, or run `opencode` and connect
  OpenRouter or OpenAI to a profile with `/connect`. A lost login is now said as one, with no repair and no Governor.
- **Adding a provider takes two steps:** **Next step** checks its template and makes its box from it, with that Shell to log
  in and a test message; **Add provider** then adds it. A new opencode profile named in the template is made there.
- **Your own computers:** the connector takes a proxy's own sign-in (`--url`, `RELAY_URL`, or it asks), and it comes with
  the address your browser has. Its 401 no longer reads as an unknown connector.
- **Plugins:** `plugin.tool(…, only=)` offers a tool to some conversations only. A forwarded app keeps a cookie it sets for
  localhost. Plugin API 1, as before.
- **Fixes:** the launcher no longer needs `cmp`. The Server plugin names a WorkBox by its folder for any user. Relay
  stops within seconds while an answer streams. Checkpoints go on when a new repository appears in the
  folder.
- **What the containers are for, said plainly:** relay protects your system from accidental damage by agents; if an agent tries to escape its container on purpose, it will.
  [SECURITY.md](../SECURITY.md) says what that means, with the ways out that are known.
- **Ways out of a WorkBox closed:**
  - **Attachments:** to copy a chat's attachments in, Relay asked the box which ones it lacked and read what came back. An
    agent is root in its box, so since 2.9.1 it could have named any file your user can read, and been handed it. Relay now
    copies only the chat's attachments.
  - **The exec gateway** made a WorkBox for whatever folder a ProviderBox named, with that folder writable, so a program
    in the box could reach most of your files, and opencode's own `workdir` your private scripts. It now runs commands only
    in folders the box has as projects, and never in your private scripts.
  - **Relay's shell on the machine** (for chat APIs' and scripts' commands) opened a file the box left in its exchange
    folder, and followed it if that was a link. It takes only a plain file now.
  - **`.git`:** Relay asked git which folders a WorkBox needs, and an agent writes its own `.git`. A `core.worktree` setting
    brought any folder of yours into its WorkBox, read-only, and a made-up `.git` file another repository's `.git`,
    writable. Relay now reads this from the files, and takes a linked worktree's or submodule's history only when that
    repository names the folder back. **One thing stops working:** a repository made with `git init --separate-git-dir`
    names nothing back, so git doesn't work in its WorkBox.
- **The code:** a stricter lint (import order, current idioms, each `noqa` true), lines within 120 characters, and what
  nothing used is gone.

**New in the docs:** [FEATURES.md](FEATURES.md), [BUILDING.md](BUILDING.md), `CONTRIBUTING.md` and
`THIRD-PARTY-NOTICES.md`. **Scripts:** `make-bundle.sh` without tags bundles what's committed. **For developers:**
`requirements-dev.lock` pins the dev tools, because pytest 9.2 breaks pytest-xdist's groups.

**Upgrading from 4.1.0:** `git pull`, then restart Relay (`systemctl --user restart relay`). A Python SDK installed
elsewhere: install it again from your Relay (`/sdk/` has the command).

---

# Relay 4.1.0 — release notes

Relay 4.1.0 (2026-10-12) brings four plugins, in `plugins/` next to the Notes example. Relay itself only got the small
things they needed, and fixes for running Relay behind a proxy that asks a sign-in of its own.

**Four plugins.** Each is a folder in `plugins/`. To turn one on, link it into Relay's plugins folder and restart
Relay, for example `ln -s ~/relay/plugins/usage_limits ~/.local/share/relay/plugins/usage_limits`. Each one's README
says more.
- **Usage limits** (`plugins/usage_limits`):
  - **Its page:** each usage-limit window over 1, 7 or 14 days (Claude Code's 5-hour and weekly, Codex's, Copilot's),
    with a table view.
  - **Low windows:** a badge on its button counts them, and a notification says when one runs low and when it's
    back.
  - **For agents and the Governor,** `usage_limits_now`: what's left of each window, when it resets, and when it would
    run out before that at its recent pace.
- **Server** (`plugins/server`):
  - **Its page:** this machine's CPU, memory, swap and disks, each with 24 hours of history, and what each container
    uses, in Relay's words (a provider's box, a service, a chat's WorkBox).
  - **Warnings** when a disk, memory or the CPU gets tight.
  - **For agents and the Governor,** `server_status`.
- **ACP** (`plugins/acp`): Relay's agents as one agent of the
  [Agent Client Protocol](https://agentclientprotocol.com), for OpenHands, Zed, JetBrains, Toad and any other ACP
  client.
  - **Its forwarder** (`relay-acp.py`) lists every Relay agent in the client's model picker, and each session is a
    Relay conversation.
  - **Images and files** attached in the client reach the agent as Relay attachments.
  - **For a client in a container,** `--map` gives its folders' real paths.
  - **For a client that would allow anything itself,** `--ask-in-relay` keeps what Relay asks you in Relay.
  - **Settings → ACP** has the setups to copy.
- **OpenHands** (`plugins/openhands`): OpenHands' Agent Canvas in a container, with Relay's agents.
  - **Where it opens:** at `/canvas` on Relay's own address, behind Relay's sign-in; its port is on 127.0.0.1 only.
  - **Its page,** under the hand on the activity bar, starts it, updates it and frames it. A bar above it picks
    which of Relay's agents its new conversations get.
  - **A Relay app inside Canvas** links each OpenHands conversation to its task in Relay.
  - **What Relay's agent asks** (a permission, a question) is answered in Relay, a click away.
  - **What it needs:** podman, and about 4.4 GB to download, once.

**For plugin authors:** `plugin.forward(path, port)` serves a web app of a plugin's own at Relay's address
(`/<path>`, HTTP and WebSockets), for whoever is signed in to Relay
([Writing a plugin](WRITING-A-PLUGIN.md)). Plugin API 1, as before.

**Behind a proxy that asks a sign-in of its own** (Caddy's `basic_auth`, nginx's `auth_basic`):
- **Signing in works.** The browser sends the proxy's credentials with every request, and Relay took them for a
  wrong app password: you were sent back to the sign-in page without a word.
- **App passwords work.** A script sends the proxy's credentials as usual and its app password in
  `X-Relay-App-Password`.
  - **With the Python SDK,** the proxy's name and password go in the address:
    `RELAY_URL=https://name:password@relay.example`.
  - **With curl:** `curl -u name:password -H "X-Relay-App-Password: relay_…" https://relay.example/api/…`.

**Also:**
- **An installer bundle,** for machines without git:
  - `sh scripts/make-bundle.sh` writes `dist/relay-<version>.tar.gz`;
  - its `install.sh` installs or upgrades Relay there, with the packages it needs.
- **A plugin's page** no longer has an empty bar above it on a wider screen.

**Upgrading from 4.0.x:** `git pull`, then restart Relay (`systemctl --user restart relay`). A Python SDK installed
elsewhere: install it again from your Relay (`/sdk/` has the command).

---

# Relay 4.0.2 — release notes

Relay 4.0.2 (2026-10-11) fixes two things. Nothing new.

- **A model's Test** (Settings → Models, and the model picker) runs as a chat would: in the provider's box, with its
  login, and an agent's commands in a WorkBox. It ran outside the box, with Relay's own home folder: a program that
  keeps its login there (Codex, say) failed its test with "401 Unauthorized" while its chats worked. A repair's
  agent traced this back to Relay.
- **A long message waiting in the queue** no longer makes the page scroll sideways on a phone: it's cut short.

**Upgrading from 4.0.x:** `git pull`, then restart Relay (`systemctl --user restart relay`).

---

# Relay 4.0.1 — release notes

Relay 4.0.1 (2026-10-11) fixes what a day of ordinary use of 4.0 turned up. Nothing new: fixes only.

- **Enter in a dialog** (Rename chat, Rename loop) no longer opens the dialog again right after it saved.
- **Signing out** in Settings → Account closes Settings: the sign-in page is all that's left.
- **Settings → Plugins:** the switch shows the plugin as it is now. Turned off, it stops at once; on again, it's back
  at once when Relay loaded it at its start. The note asks for a restart only when one is needed, or else for a
  reload of the page, where the plugin's buttons and pages follow.
- **Chat titles** keep the quotes within them: Adding note "Buy milk" no longer loses its closing quote.
- **A link to no chat** says "Conversation not found" and opens a new chat.
- **The "free" chip** under an answer is there also when opencode couldn't tell who pays during the run.
- **Notes,** the example plugin, refuses a note longer than 2000 characters instead of cutting it without a word.
- **Small things:** a skill's command shows its whole first sentence (it was cut at "e.g."); a chat's Markdown file
  keeps the dots of its title, and a slash becomes a dash; rewinding a code task in a folder without git no longer
  mentions git's history; the Review panel's subtitle no longer breaks mid-word on a phone.

**Upgrading from 4.0.0:** `git pull`, then restart Relay (`systemctl --user restart relay`).

---

# Relay 4.0.0 — release notes

Relay 4.0.0 (2026-10-11) is the last version with new features in Relay itself. **Relay's core is complete:** from
now on it gets fixes and improvements, and what's new comes as a **plugin**.

**Plugins** are folders in `~/.local/share/relay/plugins/<name>/` that Relay loads when it starts.
[Writing a plugin](WRITING-A-PLUGIN.md) explains how to write one, built on an example.
- **What a plugin adds:**
  - buttons on the activity bar, pages of its own (`/p/<name>/…`) and tabs in Settings;
  - API routes (`/api/plugins/<name>/…`, signed in as Relay's own), background tasks, a hook for runs that end, and
    a small store of its own;
  - tools for agents, as Relay's own: MCP tools, or `relay plugin <tool>` for agents without them;
  - Relay's own API, through the Python SDK, with an app password of its own.
- **Settings → Plugins** lists them: loaded, off, or why not (the file and line of the mistake), and what failed
  last. Its switch turns one off at once.
- **On the command line:**
  - `./relay plugins` lists them;
  - `./relay plugins off <name>` turns one off, also when Relay doesn't start;
  - `./relay plugins check <folder>` loads one and says what it adds.
- **A plugin's trouble is its own:**
  - one that can't be loaded, calls `sys.exit()` or hangs at its start is left out, and Relay starts as always;
  - its routes, tools, tasks and hook run in a few threads of its own, with time limits, so a slow one never holds
    up Relay.
- **A plugin runs with Relay's own rights,** outside any box: only add plugins you trust, and keep their folders
  outside the workspace roots, where agents work. [SECURITY.md](../SECURITY.md#plugins) has the details.
- **The example, Notes** (`plugins/notes/`): short notes on a page of their own. Agents add them with a
  `notes_add` tool, a run that fails leaves one, and old ones go by themselves. To try it:
  `ln -s ~/relay/plugins/notes ~/.local/share/relay/plugins/notes`, then restart Relay.
- **An agent can write one:** the built-in skill `relay-plugin-writer` has the guide and the example with it.
  In a trial, Big Pickle (a free model) wrote a working plugin from it alone.
- **How it was tested before this release:** two simulated plugin authors each wrote a real plugin from the guide
  alone (a daily digest of your chats, and bookmarks agents save), and a review went through the engine. What they
  found is fixed, and the guide says what they had to find out.

**Also in 4.0:**
- A model's markup is no chat title any more: Big Pickle once named a chat `<｜｜DSML｜｜ calls>`.
- A notification from a script or plugin may link to one of Relay's own pages (`url="/c/…"`).
- The SDK's reference in the guide lists conversations (`convo.list`) and says what `chat.messages()` has.

**For contributors:** [AGENTS.md](../AGENTS.md) has the rule. Relay's own code gets bug fixes, reliability and speed,
docs, polish of what's there, and the extension points plugins need; a new feature is a plugin.

**Upgrading from 3.x:** `git pull`, then restart Relay (`systemctl --user restart relay`, or Settings → Limits →
Restart Relay). Relay makes the plugins folder at its start.

---

# Relay 3.3.0 — release notes

Relay 3.3.0 (2026-10-11) keeps your usage limits in sight.

**The usage ring,** on the activity bar between Shell and Settings, shows what's left of your tightest usage limit:
the window with the least left across all your providers (Claude Code's 5-hour and weekly windows, ChatGPT's,
Copilot's requests).
- **Its colours:** green; amber under 20%; red when that window is used up.
- **Hovering it** names the window and when it resets.
- **A click** shows every provider's windows as Settings → Usage has them, with **Refresh** and a link there.
- **When it looks again:** every few minutes while the tab is in view, when you come back to the tab, and when a run
  ends.
- **Hidden** while none of your providers tells its limits.

Settings → Usage draws its limits with the same code, so both look alike.

**Upgrading from 3.2.x:** `git pull`, then restart Relay (`systemctl --user restart relay`, or Settings → Limits →
Restart Relay) so it reports 3.3.0. The ring itself is in the page, and a reload shows it.

---

# Relay 3.2.1 — release notes

Relay 3.2.1 (2026-10-11) fixes one thing.

**A new chat starts with No project folder.** It started in the folder of the code task before it: the page kept
"in a folder" after a task was started there, and after a reload. Now every new conversation starts as a chat, and
the folder picker opens at the folder you picked last. `/new` in a code task still starts in its folder.

**Upgrading from 3.2.0:** `git pull`, then reload Relay's page (no restart needed: it's only the page).

---

# Relay 3.2.0 — release notes

Relay 3.2.0 (2026-10-11) adds a provider Relay doesn't know, and repairs one it has, on a page of its own: you
install it in a box and log in there, an agent of yours plans the work, and you try the result in a throwaway Relay
before it goes into yours.

**Adding a provider with an agent** (Settings → Providers → Add provider → A provider Relay doesn't know → Add it
with an agent). [The guide](GUIDE.md#a-provider-relay-doesnt-know-added-with-an-agent) has the details.
1. **Install it and log in,** in a shell on the page, in a new box with a home of its own. What you install there
   (`npm i -g`, `pip install --user`, its own installer) and your login stay. Then say what you installed, and
   choose the agent and model that integrates it.
2. **Its plan:** the agent studies the program in that box and hands in a plan. **Approve**, **Redraft** (say what
   should be different) or **Deny**.
3. **Try it, then Add:**
   - the agent builds the layer;
   - Relay checks it fully in a box of its own;
   - Relay starts a **throwaway Relay** with only the new provider, at `<your Relay>/trial/<id>/`, signed in as you.
   - Something's wrong goes back to the agent. **Add** puts it in Relay and restarts it; **Take it out again** undoes
     that.
- **A provider added this way** has a **Shell** in Settings, in a box with its home: to log in again when a login
  expires.

**Repairing a provider, the same way** (Settings → Providers → 🔧). [The guide](GUIDE.md#repairing-a-provider)
has the details.
1. **What's wrong:** Relay's check, its models and its latest errors. A provider with a home has a shell there too
   (an update or a new login may be all it needs). Then your words, and who repairs it.
2. **Its plan,** to approve.
3. **Try it, then Apply:**
   - Relay checks the repair fully (a turn, going on, stopping, a command), where it used to be its check and one
     prompt;
   - a throwaway Relay has the provider with the repaired files;
   - **Apply** keeps the originals and restarts Relay; **Undo** brings them back.

**AgentOS's Governor and providers**
- **It looks after providers:** `provider.list`, `provider.add` (only kinds whose program runs in a box of Relay's,
  or a chat API, never with a key of yours), `provider.remove` (only what it added: "added by AgentOS" in Settings)
  and `provider.check`.
- **A program Relay doesn't know:** `provider.create` sends you to add it with an agent, with what it knows of it.
- **A provider that keeps failing:** `provider.repair` starts its repair.
- **Both leave a Needs-you item with Open,** to the page. Installing, logging in, plans, trying and adding or
  applying stay yours. `provider.review` is gone: Relay checks a repair itself.

**Also new**
- **A yellow "E"** marks conversations started outside Relay's pages: by a script, the Python SDK or another client
  of the API. Its tooltip says who ([the guide](GUIDE.md#your-workspace)).
- **Relay under a path of your site:** `"base_path": "/relay"` in `settings.json`, behind a proxy that serves it
  there ([the guide](GUIDE.md#relay-under-a-path-of-your-site)).
- **Providers' boxes have Node.js 22,** npm, curl and unzip, for programs written for Node (the image grew from
  228 to 429 MB).
- **Code an agent writes for a provider** (a repair, a new one) is tested in a box of its own, never on the machine;
  [SECURITY.md](../SECURITY.md) says what that box has, and what a throwaway Relay runs.

**Fixed**
- **Copying a folder with its own `.git` onto a network drive** (Dolphin: "Access was denied while attempting to
  create the requested folder") works: only the shared folder's own `.git` is kept out. The drive's log now has what
  it refuses.
- **A repair's or new provider's conversation** isn't under Tasks' Needs review any more: its page is where its
  changes are taken.
- **An agent on a busy provider's box** (Claude Code working elsewhere) couldn't start in a repair's folder: every
  box has those folders now.
- **The test suite** passes on a Relay where you added providers of your own.

**Upgrading from 3.1.0:**
- **`git pull` and restart.**
- **The first start builds the providers' box image anew** (a few minutes). Each provider's box is made again once,
  when it's next free.
- **The Python SDK:** your 3.1.0 SDK keeps working. Install the 3.2.0 one (Settings → Account has the command) and
  Relay can say a conversation came from the SDK.
- **For scripts that called Relay's own API:** `POST /api/providers/{id}/repair` and `/api/repairs/{id}/{action}`
  are gone; a repair is `/api/provider-repairs` now. The SDK never used them.

---

# Relay 3.1.0 — release notes

Relay 3.1.0 (2026-10-10) brings app passwords: scripts and programs sign in to Relay's API and the Python SDK
without your account's password.

**App passwords**
- **One per script or program.** Make them in Settings → Account → App passwords, named after what uses each one.
- **Shown once,** when it's made (`relay_…`). Relay keeps only a hash. The list says when each was last used.
- **Revoked one by one:** only what uses that one stops working. Changing your account's password signs out your
  browser sessions but leaves app passwords as they are.
- **What one can do:** everything you can through the API, as you, except manage app passwords. A leaked one can't
  make new ones or revoke the others.

**Using one**
- **With the SDK:** `connect("http://my-server:8420", app_password="relay_…")`. Or set `RELAY_URL` and
  `RELAY_APP_PASSWORD` in a script's environment, and it signs in by itself, with no `login()` or `connect()`.
- **From any HTTP client:** the header `Authorization: Bearer relay_…`, or Basic with your username
  (`curl -u me:relay_… …`). Such a request needs no `x-relay-request` header: another site's page can't send an
  Authorization header to Relay.

The guide has the details: [App passwords](GUIDE.md#app-passwords).

**Upgrading from 3.0.0:** `git pull` and restart. The SDK's new `connect(…, app_password=…)` needs the SDK of 3.1.0:
install it again from your Relay (Settings → Account has the command).

---

# Relay 3.0.0 — release notes

Relay 3.0.0 (2026-10-10) is the first public release. It works as 2.12.1 did: what's new is for whoever meets Relay
for the first time, and what one such newcomer ran into, installing it from the README.

**Open source, under the [MIT license](../LICENSE).** The whole history comes along.

**Documentation for someone new to it**
- **[The README](../README.md)** is a front page now:
  - what Relay is and does, with screenshots of real runs;
  - who it's for, what it needs, how to install it, and the first steps.
- **[The guide](GUIDE.md)** has everything the README used to hold, with a table of contents.
- **[SECURITY.md](../SECURITY.md)** says what Relay is built for and what protects what:
  - signing in, other sites, the agents' containers and network, the commands no agent may run, `/port/`,
    network drives, the data;
  - what it leaves to you, and how to report a problem privately.
- **[How it's built](ARCHITECTURE.md)** is a page of its own, brought up to date.
- **[AGENTS.md](../AGENTS.md)** is the guide for anyone who develops Relay, people and agents alike. A machine's own
  notes go in `AGENTS.local.md`, which isn't in git.

**Installing, as a newcomer did it.** A fresh Linux user, with no agents and no Relay data, followed the README to
the end of its first steps: a chat and a code task with Review changes. What stopped them or misled them is fixed:
- **opencode 2.** Relay uses opencode's background service, which came with opencode 2. opencode.ai's main installer
  still gives opencode 1, so:
  - the README names opencode 2's installer: `curl -fsSL https://opencode.ai/v2/install | bash`;
  - Relay itself says when it finds opencode 1, and how to get 2, in Test, in `relay doctor`, and when it would
    start the service.
- **opencode's service port in use** by another program, such as another user's opencode: Relay says so after 5
  seconds, and names a free port to move it to. It used to wait 30 seconds and say the service "did not start".
- **The port you install Relay on** (`install-relay.sh --port N`) is kept in Relay's settings. `relay doctor`,
  `relay provider-check` and the addresses it prints use it too. `./relay port N` changes it later.
- **Agents installed after Relay** are found, in the folders their installers use (`~/.opencode/bin`,
  `~/.local/bin`, a user's npm and bun): in the service, in the foreground, and in services installed before.
- **A new Relay's first chat** starts with a provider that's installed. It used to be Claude Code, whether it was
  there or not.
- **A model that's been deprecated** gets no plan to try it again five hours on: the chat says so. This was
  opencode's default model of the day, for that newcomer.
- **The README** says when the containers' images are built (at the first start), where a backup goes, and to pick
  one of opencode's free models without an account.

**Relay's data folder** is yours alone (`700`) when Relay makes it. `relay doctor` warns when other users of the
machine can read it.

**For developers**
- **`ruff check` is clean,** with the project's own rules in `pyproject.toml`.
- **Starlette's test client** uses `httpx2` in the dev tools.
- **A test** keeps the docs' links and anchors working.

**Upgrading from 2.12.1:** `git pull` and restart. A service installed with `--port` keeps working: its port is still
on its command line, which wins over the settings.

---

# Relay 2.12.1 — release notes

Relay 2.12.1 (2026-10-10) fixes the four things the simulated users found that 2.12.0 left open.

- **New chats start with the provider and model you picked last, on any device.** It used to be kept by the
  browser alone, so another browser, your phone or a private window started at the default model each time.
- **The review panel closes once you mark its changes as reviewed.** It used to stay open on "No changes to
  review", and the button could be pressed twice.
- **A kept shell opened in a window of another width** wraps its earlier lines anew, instead of showing them broken
  in odd places (typed on a phone, opened on a desktop, say).
- **AgentOS's tiles say all of it:** two lines under each figure instead of one cut short ("next Sun, Oct 11
  08:00 …").

**Upgrading from 2.12.0:** `git pull` and restart.

---

# Relay 2.12.0 — release notes

Relay 2.12 (2026-10-10) is about adding a provider Relay doesn't know: a guide, the tools for it, and a home for each
provider's code.

**A guide: [docs/ADDING-A-PROVIDER.md](ADDING-A-PROVIDER.md)**
- **Where to find it:** in Relay's folder, and in Settings → Providers → Add provider → **A provider Relay doesn't
  know**.
- **First it asks whether a kind fits already,** which often needs no code: an OpenAI-style API, an ACP agent, a
  model opencode supports.
- **If none does, it builds the part that connects one to Relay,** its compatibility layer, step by step:
  - how to study the program;
  - how to start it in its box;
  - how to turn what it prints into Relay's events;
  - stopping, permissions, Relay's tools and skills, models, efforts, usage limits.
- **A worked example runs along:** toy-agent, a tiny coding agent with a format of its own, and its layer.
  - **Always up to date:** the guide's code is quoted from it, and a test keeps it so.
  - **It ends with a reference:** every event, hook and field.

**Each provider in a folder of its own** (`src/relay/providers/<name>/`)
- **Everything for a provider goes in its folder,** its tests too. Relay finds the folders when it starts, so
  adding a provider never means changing a file outside its folder. Relay's own providers moved into folders too.
- **A mistake in a folder never keeps Relay from starting.** Relay leaves that folder out and says why, with the
  file and line, in Settings → Providers and in `relay doctor`. A provider whose code raises somewhere is that
  provider's trouble, not the page's.
- **A repair** works on the provider's folder.

**Check it fully**
- **Where:** the shield next to a provider in Settings → Providers, or `./relay provider-check <id>`.
- **What it does:** it runs the provider as Relay does (in its box, its commands in a WorkBox), through a few short
  prompts, and checks every event that comes back.
- **The scenarios:** a turn, a turn that goes on in its session, a turn stopped halfway and, for an agent, a
  command.
- **Tried:** Claude Code, opencode and the example pass every scenario.

**An agent can write one:** the built-in skill `relay-provider-writer` has an agent build a layer by the guide, in
Relay's folder.

**Fixed, found by simulated users** (testing this release before it went out):
- **With the column open on a window just wider than a phone,** the header no longer pushes the page sideways.
- **The / menu** no longer covers the window.
- **The Shell panel:**
  - the Shell button under the message box closes the panel too;
  - on a phone, the bar's Shell closes the drawer.
- **`relay doctor`** no longer offers to remove another Relay's web search box.

**Upgrading from 2.11.1:** `git pull` and restart.

---

# Relay 2.11.1 — release notes

Relay 2.11.1 (2026-10-10) fixes one thing you reported and does one you asked for.

- **The os mark in Tasks is back:** AgentOS's code tasks have their green icon with "os" again in the Tasks view.
  The Tasks page has it now too.
- **A chat's shell stays:**
  - **It keeps running** when you close the Shell panel, open another chat or reload the page.
  - **Opened again, it's the same shell:** its variables, its folder, what's running in it, and what it showed.
    The status says Reconnected.
  - **It ends** with `exit` or the new **End shell** button (then **New shell** starts a fresh one). It also ends
    after 3 hours that nobody opened it, when Relay restarts, and when you delete the chat or discard its task.
  - **Another window** that opens it takes it over.
  - **A service's Shell tab** still ends with its page.

**Upgrading from 2.11.0:** `git pull` and restart.

---

# Relay 2.11.0 — release notes

Relay 2.11 (2026-10-10) lays out its window as your mockups have it: an activity bar and one column beside it.

**The activity bar and its views**
- **On the left, a bar of icons:** Chats, Tasks, AgentOS, Governor, Scripts and Production boxes. At the bottom are
  the Shell, Settings and you (your account and the theme).
- **One column** shows the view you chose. The views replace each other there: the chat or page on the right
  stays as it is.
- **Each view** has a header with its actions, a search for its list, and its groups with how many are in each:

  | View | Lists | Opens |
  |---|---|---|
  | Chats | your chats, as before | the chat |
  | Tasks | Waiting for you, Working, Needs review, Up to date, Discarded | the task |
  | AgentOS | Overview, Needs you, its tasks by kind | its dashboard, or a task's form |
  | Governor | the Governor's conversation, then the chats AgentOS made | the chat |
  | Scripts | Running, Private, Public, Examples | the script, at `/scripts/<its name>` |
  | Production boxes | the services and how each is doing | the service's page |

- **The AgentOS group** above your chats is gone: those chats are in the Governor view.
- **The column's width is yours:** drag its edge. It's the same in every view and remembered.
- **Folding it away:** click the open view's icon; any icon brings it back.
- **On a phone,** the bar and the column are the drawer.

**Ctrl+K** (⌘K) opens a quick switcher to any chat, from anywhere.
- **As you type,** it shows chats by title.
- **Then** it adds the ones that have it in what was said.
- **Using it:** arrows and Enter open one.

**The Shell** is a panel under the chat now, instead of a window.
- **It follows the chat you open.**
- **Its height:** drag its top edge; it's remembered.
- **Opened by** the bar's Shell, the Shell button under the message box, or `/shell`.

**Fixed on the way:** a scheduled task's form closed by itself after 10 seconds when you had clicked a button in
it (the AgentOS page's refresh drew the list over it).

**Upgrading from 2.10.0:** `git pull` and restart.

---

# Relay 2.10.0 — release notes

Relay 2.10 (2026-10-10) fixes edge cases. They were found by sending every API
route and every tool of Relay's edge-case input, by taking things away that
others still point to, by trying unusual git states, extreme content and very
long chats in a throwaway Relay, and by measuring.

**Long chats**
- **A long chat opens from its last messages** (up to about 3000 events, at
  least its last turn). **Show N earlier messages** above them brings the part
  before, where you're reading.
  - 16,000 events open in 0.6 s instead of 2.5 s, 300 turns in 0.7 s instead
    of 10.
  - AgentOS's Governor's chat only grows.
- **Keeping your place while reading back** no longer measures every element:
  showing more above took minutes with tens of thousands of tool calls.

**Odd input, refused and said** (it failed with an error 500 before)
- **NaN or Infinity** in a request, as Python's json sends them: a /timed with
  NaN made a chat that couldn't be opened any more. A response with such a
  number from a provider's output has null for it.
- **A NUL character,** or a name or path given as a list or an object.
- **A folder name too long, or a NUL** in a workspace path.
- **Settings and the subagent policy** with a value of the wrong type.
- **An upload's type:** kept only when it's a type.
- **A short id that fits two** (a chat, task, intervention, repair, service,
  subagent) is said. AgentOS said "No managed chat", and a subagent's merge or
  tell took the first one.

**Scheduled tasks and /timed**
- **"Every hour" keeps its beat:** it drifted by up to 20 s a run.
- **Far-off times are refused:** an interval of at most a year, a once that's a
  date there is. One past year 9999 broke the Governor's task list.
- **An unknown time zone** is refused, instead of using the server's.
- **Run now** says why a task didn't start (its provider removed, say).
- **/timed** is at most a week, from 0 minutes.
- **Checked:** 02:30 on 25 October (twice on the clock) runs once.

**Code tasks:** a task asked onto a branch of its own (`isolate`, the API's
and the SDK's) that works in the folder itself says why (`in_place`; the SDK
warns).
- **Why it can't be:** no commits, a detached HEAD, uncommitted changes.
- **Before,** it wrote into your uncommitted work unsaid.

**Scripts:** one deleted while it runs ends its run first. The run went on
unseen and couldn't be stopped any more.

**Upgrading from 2.9.1:** `git pull` and restart.

---

# Relay 2.9.1 — release notes

Relay 2.9.1 (2026-10-10) fixes three things you reported.

- **Attachments in a code task:** a file you attach is now in the agent's
  WorkBox too, at the path the agent is told. Before, only the agent's own
  file tools could read it. A command (a PDF, a zip or a spreadsheet opened
  with a program) found nothing there.
- **Self-repair** works where its agent works:
  - **Before:** in its container it had neither the program behind the
    provider nor Relay's source to test in.
  - **Now, Relay runs for it:**
    - `repair_check` tests its version in a copy of Relay;
    - `repair_run` runs a command in the provider's own box, with its login
      (`claude --version`, a tiny prompt).
  - **Its reference** is all of Relay's source.
  - **A program that's itself the problem** (an old version, a lost login)
    it tells you about.
- **"Now with …" above a turn that's going:** the turn shows the agent that's
  answering, not a model chosen for the chat meanwhile (that one is for the next
  turn).
  - **A model menu that loaded slowly** no longer opens on the next screen.
  - **The pickers under the message box** change only their own chat.

**Upgrading from 2.9.0:** `git pull` and restart.

---

# Relay 2.9.0 — release notes

Relay 2.9 (2026-10-08) is a polish and reliability release. It comes from your
own chats of the last days, and from two simulated users: Claude Sonnet 5.5,
in a browser on a throwaway Relay, on free models, as a daily user and as one
who automates. A second round of them checked the fixes.

**Models**
- **The name with its provider in front:** "claude-code/sonnet", as opencode
  writes its models, is "sonnet"; for opencode, "big-pickle" is
  "opencode/big-pickle". Both failed at once before.
- **A model that can't be used** (it doesn't exist, the login can't use it, it
  isn't offered where you are):
  - its error says so in Relay's words, once;
  - "Choose another model" is the first button, and the model you choose
    answers your message (there was no work to go on with);
  - Relay no longer plans to try it again in five hours, as if it were a
    usage limit.
- **A name mistyped once** isn't warned of to agents for good.
- **"Thinking…"** counts the seconds after 10, while an agent says nothing.
- **Search:** agents search with Relay's web search first, your own SearXNG.

**Scheduled tasks**
- **How it went:** a task whose agent failed says "failed", and why, not
  "done". A script's says how long it took ("took 3 s"), and why when it
  failed ("exit 1 after 3 s:" and its last line).
- **"Working"** while its script runs or its agent works, not "ran".
- **Checked when saved:** a script that isn't there, or arguments that don't
  parse, are refused then, not found out at the task's time. One whose script
  went keeps it in the form, marked "(gone)".
- **The AgentOS page** keeps itself up to date while it's open.
- **The form** has "A script of yours"; Run now on one says that it runs.
- **Your tasks follow a script** you rename or make public or private.
  Deleting one says which tasks run it.

**Scripts**
- **A name without its folder** ("report.py") works everywhere: private/'s,
  else public/'s.
- **With an agent** never writes over a script that's there, and leaves no
  `__pycache__` behind.
- **The page:** a script made from a template opens at its top. The page says
  when its folder is shared as a drive.

**Chats**
- **Confirmation dialogs** no longer show "null" between their buttons.
- **Commit…** starts from the task's title, and says when a message is
  missing.
- **What stood still:** the timed bar goes with Stop, and its times stay true.
  Cancel takes "Resuming later" away.
- **A kept temporary chat** gets a title.
- **On a phone:** "Choose a project folder" in the welcome is a link to it;
  Review changes is its icon and count; a dialog's buttons wrap instead of
  sticking out.

**Notifications:** why one didn't go, in words. ntfy shows Markdown, and
`notify(priority="normal")` works.

**Services:** one that never came up shows red, not green, and its page no
longer waits on a box it doesn't have.

**Network drives:** a name too long for a file is no error 500 any more, and
the drive's log stays small.

**Upgrading from 2.8.1:** `git pull` and restart.

---

# Relay 2.8.1 — release notes

Relay 2.8.1 (2026-10-08): Discord webhooks work.

- **A Discord webhook** in Settings → Notifications gets messages in Discord's
  own form: the title in bold, the text, the link, pinging nobody. Before,
  Discord refused them ("webhook: Forbidden").
- **Every push says it's Relay** (its User-Agent). Discord turned away
  Python's own.

**Upgrading from 2.8.0:** `git pull` and restart; then **Save and send test**
in Settings → Notifications.

---

# Relay 2.8.0 — release notes

Relay 2.8 (2026-10-08): one API, thin clients. What the page's buttons do is
Relay's API now, and the Python SDK reaches the rest of Relay. The guide has
the manual: [Python SDK](GUIDE.md#python-sdk).

- **The SDK:**
  - **`scripts`:** list, create, run (its output as it's written, its end),
    move, copy and rename, and `write`, where an agent writes one;
  - **`tasks`:** scheduled ones, with when in words (`"daily 08:00"`), a
    prompt or a script; run one now;
  - **`services`:** your live services' status, logs and snapshots; start,
    stop, restart, roll back, restore;
  - **`notify(text)`:** to your phone, in a new category From your scripts
    (Settings → Notifications).
- **The API:** what the page did in steps of its own is an endpoint, for the
  SDK as for the page:
  - a new script, never over one that's there;
  - Copy and Make public/private, under the next name free;
  - With an agent;
  - a scheduled task's when in words;
  - a notification from a script.
- **A template:** `service_watch.py` looks at your live services and tells
  your phone when one isn't well.
- **Agents working on Relay** (AGENTS.md): a feature is the API's first, and
  the page and the SDK call it. A test keeps the SDK to Relay's routes.

**Upgrading from 2.7.0:** `git pull` and restart. On other machines, install
the SDK again from your Relay (`/sdk/`).

---

# Relay 2.7.0 — release notes

Relay 2.7 (2026-10-08): your scripts may use packages from PyPI. The guide
has the manual: [Scripts](GUIDE.md#scripts).

- **A script names the packages it needs** in a `# /// script` block at its
  top (PEP 723, `# dependencies = ["requests"]`).
  - Relay installs them with pip before each run, in the WorkBox the script
    runs in.
  - They stay in that WorkBox's home, so only the first run waits.
  - A script made private or public gets them in its new WorkBox too.
- **Agents may use them:** the `relay-sdk-template-writer` skill no longer
  keeps a script to the standard library. An agent names what its script
  needs, and may install it to try the script as it writes it.

**Upgrading from 2.6.1:** `git pull` and restart.

---

# Relay 2.6.1 — release notes

Relay 2.6.1 (2026-10-08) fixes how 2.6 moves your scripts.

- **Your scripts' runs from before 2.6** are named anew (`private/…`) when
  Relay opens its database, so they stay with their scripts whichever Relay
  moved those. In 2.6.0 only the Relay that moved them renamed their runs:
  when another one with the same workspace root came first, the runs kept
  their old names, and the page no longer showed them.

**Upgrading from 2.6.0:** `git pull` and restart.

---

# Relay 2.6.0 — release notes

Relay 2.6 (2026-10-07) splits your scripts into private and public. The
guide has the manual: [Scripts](GUIDE.md#scripts).

- **`private/`:** yours alone.
  - No agent sees it, not even AgentOS's Governor: every box Relay makes has an
    empty folder in its place.
  - A private script runs in a WorkBox of its own.
- **`public/`:** for agents too.
  - Every agent may read it: their WorkBoxes have it read-only, and their note
    says where.
  - AgentOS's Governor may run a public script (`script.list`, `script.read`,
    `script.run`, `script.output`), or make a task that runs one at its time.
    Relay runs it, as you, with no agent, and notes how it ended.
- **The root** holds only the two folders and `DO_NOT_PUT_FILES_HERE`.
  - Your existing scripts, and the other files beside them, moved into
    `private/` (the safe side), their runs with them.
  - Relay's examples are in `public/examples/` now.
- **On the page:**
  - the groups Private, Public and Examples;
  - **Make public** and **Make private** in a script's menu;
  - a new script is private; **With an agent** writes in `public/`.
- **On the AgentOS page:** a task that runs a script shows which, and how it
  ended.
- **Fixed:** a service's data put back and undone within the same second (a
  script or an agent can) lost its undo: the copy kept first was written over.

**Upgrading from 2.5.0:** `git pull` and restart.
- **The scripts' folder** is put in order when Relay starts: the scripts that
  were in its root, and the other files beside them, move into `private/`.
- **Boxes:** each WorkBox and ProviderBox is made anew once, at its next use.
  WorkBoxes get the public scripts read-only; every box gets the private ones
  masked.

---

# Relay 2.5.0 — release notes

Relay 2.5 (2026-10-07) adds temporary chats and RESL, Relay's Extended
Standard Library, and the Scripts page gets Relay's examples. The guide has
the manual: [Temporary chats](GUIDE.md#temporary-chats),
[RESL](GUIDE.md#resl), [Scripts](GUIDE.md#scripts).

- **Temporary chats:** **Temporary**, on a new chat, makes one.
  - In no list, search or Tasks, and not seen by AgentOS's Governor.
  - It uses and adds none of Relay's memory, and sends no notification.
  - It goes when you leave it. **Keep it** makes it a chat like any other.
  - One left behind (a closed tab) goes after a day (Settings → Limits).
  - Scripts make them with `convo.create(temporary=True)`.
- **RESL**, built on the SDK and in the same wheel: `from resl import *`.
  - **How it judges:** another model gets only the text, in a temporary one-shot
    chat.
  - **Checking answers:** did the agent refuse (`agent_refused`), finish
    (`task_done`), or wait for you (`asks_user`)? Also `judge` and `score`.
  - **Answers to data:** `extract`, `classify`, `summarize`, `translate`.
  - **Agents checking agents:** `ask_until` (the next agent while one refuses),
    `best_of`, `second_opinion`, `review_changes`.
  - **Relay helpers:** `one_shot`, `pick_agent`.
  - **As methods:** `answer.refused()` and the like.
- **Relay's examples in the scripts' folder** (`examples/`): runnable, kept up
  to date with each version, read-only on the page. **Copy to my scripts**
  makes one yours. Four use RESL.
- **Smaller:** a chat with a title of its own (the user's, or a script's)
  no longer has a model asked for one.

**Upgrading from 2.4.0:** `git pull` and restart. The database gets the
temporary chats' column. Install the SDK anew from `/sdk/` for RESL.

---

# Relay 2.4.0 — release notes

Relay 2.4 (2026-10-07) makes the Scripts page a better place to write. The
[guide](GUIDE.md#scripts) has the manual.

- **Your scripts in VS Code:** **Open in VS Code** shares the scripts' folder
  as a network drive (WebDAV), as `/mount` does a code task's folder.
  - Open the drive on your computer, then open it as a folder in VS Code (or
    any editor). What you save there shows on the page.
  - The dialog has the steps for Windows, macOS and Linux, and the
    `pip install` for completions.
- **Colours:** the editor colours Python as you type.
- **Documentation:** a button that opens it beside the page: how scripts run
  here, then the SDK's reference.
- **Smaller:** the network drive dialog's Windows step shows Explorer's
  address itself.

**Upgrading from 2.3.0:** `git pull` and restart. The SDK is 2.4.0 too,
unchanged.

---

# Relay 2.3.0 — release notes

Relay 2.3 (2026-10-07) brings the SDK into Relay itself: a **Scripts** page to
write and run your own scripts with it, and a skill for agents that write
them. The [guide](GUIDE.md#scripts) has the manual.

- **The Scripts page** (in the sidebar):
  - **What they are:** `.py` files in one folder, `scripts` in your first
    workspace root (another one can be chosen).
  - **Writing:** an editor with line numbers, Tab, Ctrl+S and Ctrl+Enter.
    What isn't saved is kept while you're elsewhere in Relay.
  - **Running:** in the folder's WorkBox, signed in as you, with its
    arguments. Its output comes as it's written, with how it ended and the
    conversations it started, as links. Each script keeps its last 20 runs.
  - **Stop** (and its time limit, Settings → Limits → Scripts) also stops
    the agents of the conversations it started.
- **Three ways to start one:**
  - **New**: a small one to start from;
  - **From a template**: five working scripts: one question, a job in a
    folder with its changes and a commit, `/timed` work, models side by side,
    the provider with room in its usage limits;
  - **With an agent**: say what it should do; the agent chosen under the
    message box writes it there.
- **The skill `relay-sdk-template-writer`** (built in): how an agent writes
  such a script, with the SDK's reference and the templates. The page's
  **With an agent** uses it; `/relay-sdk-template-writer <what it should do>`
  calls it in any code task.
- **The SDK, 2.3:**
  - signed in by itself on the Scripts page (`RELAY_TOKEN`);
  - in a WorkBox it finds Relay by itself, behind a VPN too;
  - a time-out or a lost connection is a `RelayError` too.
- **WorkBoxes have the machine's time zone,** so times in them (`date`, a
  script's output) are yours.
- **A restart** (`POST /api/restart`) waits until no script runs, as it does
  for agents.

**Upgrading from 2.2.0:** `git pull` and restart. Each WorkBox is made anew
at its next use (it now has the SDK, read-only, and the machine's time zone):
its home folder is kept, but what an agent installed with `apt-get` is gone.
The database gets the scripts' runs.

---

# Relay 2.2.0 — release notes

Relay 2.2 (2026-10-07) makes the Python SDK a full one: most of what Relay can
do with a conversation, from your own code. The [guide](GUIDE.md#python-sdk)
has the manual.

- **Conversations as objects.** `convo.create` returns a `Conversation`,
  which is its id as a string too, so 2.1's code keeps working.
- **What it adds:**
  - another agent mid-conversation (`switch`);
  - `/timed` (`send(…, timed=90)`);
  - every turn as it comes (`follow`): `/timed`'s go-ons, a fallback agent,
    the queue;
  - `queue`, `retry`, `edit`, `fork`, `clear`;
  - going on after a usage limit;
  - a code task's changes, rewind, commit, merge and discard;
  - usage and the providers' limits.
- **Questions:** an agent asking something (a form or a permission) no
  longer leaves a script waiting. `on_question` answers it, or
  `message.answer` by hand.
- **New in Relay itself, and the SDK uses them:**
  - **A conversation's own instructions**, after its mode's, for every
    agent: Claude Code's and the chat APIs' system prompt, the prompt of
    opencode and ACP agents. The program's own system prompt stays.
  - **Relay's own tools per conversation:** web, memory, subagents and
    computers can be turned off. The agent's program keeps its own tools.
  - **Messages carry their run's times,** so `elapsed_time` works for an
    answer read back too.
  - **A conversation's VPN can be undone.**
  - **`/timed`'s times** can be given as fields.
- **Changed from 2.1:**
  - `convo.create(mode=…)` is `agent_mode=…`;
  - without a provider, a new conversation gets your latest conversation's
    agent (or the one `login` names), not the first provider enabled.

**Upgrading from 2.1.0:** `git pull` and restart. The database is migrated
(a conversation's instructions and tools). Install the SDK anew from
`/sdk/`.

---

# Relay 2.1.0 — release notes

Relay 2.1 (2026-10-07) adds one thing: a **Python SDK**. Relay's agents can
now be used from your own Python code. The [guide](GUIDE.md#python-sdk)
has the manual.

- **The SDK:** `login`, then `convo.create` (a chat, or a code task in a
  folder) and `convo.send`. The answer is a `Message`: its `response`,
  `elapsed_time`, `tools_called`, `text` (Markdown) and `json`, either all
  at once or as it comes (`stream=True`).
  - **Also:** list, get, read back, rename, stop and delete conversations;
    attachments; providers and their models.
  - **The VPN:** `uses_vpn` sends a conversation's folder through a VPN
    tunnel.
- **Installing it:** Relay serves it for pip, so any machine that reaches
  Relay installs it from there: `pip install
  http://<relay>/sdk/relay_sdk-2.1.0-py3-none-any.whl`. Settings → Account
  has the command.
  - It's one module with only the standard library, for Python 3.9+.
- **In Relay's API:** `uses_vpn` and `vpn` on a conversation, and one answer
  as Markdown (`export.md?run=`).

**Upgrading from 2.0.0:** `git pull` and restart. Nothing is migrated.

---

# Relay 2.0.0 — release notes

Relay 2.0 (2026-10-07). Relay is a self-hosted
web app for chatting with AI models and running coding agents (Claude Code,
opencode, GitHub Copilot, Perplexity, any OpenAI-compatible API, or a command
of your own) on your own server, for one person. 2.0 adds **AgentOS**: an
agent of its own that carries out your tasks and looks after your services
while you're away. The [README](../README.md) is the manual;
[DEVLOG.md](DEVLOG.md) has the decisions behind it.

## Since 2.0.0rc2

After rc2, the features already there were polished, and the Governor itself
was asked what got in its way.

- **Memory, like Perplexity's.**
  - Every memory is filed in a category: preferences, about you, work,
    tools, interests, location, setup, decisions or other.
  - Settings → Memory is a memory bank: search, category chips, where each
    one counts, edit in place, undo, and delete all.
  - A second switch decides whether agents save what they learn.
- **The Governor's memory.**
  - It has a memory of its own, which comes with every briefing; its long
    notes.md was moved into it once.
  - A weekly system task tidies Relay's memory up: what's said twice is
    merged, and what's gone stale is forgotten. Yours it leaves alone and
    only names in its report.
  - Relay now notes when an agent last got each memory.
- **Services.**
  - Each has a page: an overview, a shell in its box, and its log.
  - Its data is copied every night, and 7 copies are kept. One can be put
    back from the page, or by the Governor (`service.restore`).
  - The Governor can make a new service itself (`service.create`), for a
    site it builds on its own, say.
- **AgentOS can be left out:** Settings → AgentOS. Off, it's gone from
  Relay's screens: its page, its conversations, Production boxes and
  `/promote`.
- **The Governor over a restart.**
  - What it follows is kept.
  - An agent's turn a restart cut off goes on by itself, and a wake-up that
    was cut off is done again.
  - Its work no longer holds a restart up; only your own agents do.
- **The Governor's own feedback.**
  - A turn that stopped comes with why, how long, what it cost and its last
    steps.
  - The budget says it's per wake-up and what was spent.
  - Its core tools are loaded from the start, and its instructions are in
    short parts.
  - A router such as Copilot's auto isn't ranked as one model.
  - An agent you stopped stays stopped unless you say otherwise.
- **News reaches the Governor within 10 minutes** under heavy use: its wake
  cap is a pace now, not a count per hour.
- **A web search takes at most 10 seconds** (Settings → Limits → Web
  search). Engines that answer later are left out, and the answer names
  them.
- **Fixes:**
  - `relay serve --port N` gave agents the default port;
  - the fallback loop editor showed the previous provider's models while
    the new one's loaded.

**Upgrading from 2.0.0rc2:** `git pull` and restart. The database is
migrated (memories' categories and use, routers in the model ranking). With
AgentOS on, the new system task "Memory: weekly tidy-up" is added.

## Since 2.0.0rc1

rc2 is rc1 used for real: agents built three projects (a web app with SQLite,
a URL shortener with PostgreSQL in docker compose, a Python library with
subagents), two went live as services, and the Governor solved real problems
(a new version that lost the app's data, a weekly vulnerability check, a
service whose database broke). What that turned up is fixed:

- **Services keep their data and dependencies.** The agent in a service's
  WorkBox is told how a version runs: on `$PORT`, its data in `$DATA_DIR`
  (shared by every version), its dependencies in its folder. The new service
  dialog says so, warns when the code never mentions `$DATA_DIR`, and can
  ask the task's agent to see to it first. A first version that doesn't come
  up shows why under **Log**. A compose project's start is guessed.
- **`docker compose` in a box:** `depends_on: condition: service_healthy` no
  longer waits for ever (Relay runs the health checks itself; podman's need
  systemd).
- **Agents can commit** in their WorkBox (as Relay, unless the project says
  who), and a task in one folder of a repository that holds several projects
  commits even while another agent works elsewhere in it. A refusal names
  who holds the project up.
- **VPN tunnels survive a restart of Relay;** one an older Relay left without
  its network is made anew by itself.
- **Claude Code** turns no longer hang on a resumed session's leftovers (a
  command it had moved to the background).
- **Answers** keep what an agent said before and after its tool calls as
  separate paragraphs in exports, notifications and handovers.
- Smaller: `relay changes` names the turn going on; the AgentOS page is calm
  when nothing needs you.

**Upgrading from 2.0.0rc1:** `git pull` and restart. The WorkBox image is
built anew (a few minutes, mostly cached), and each WorkBox is made anew at its
next use. A VPN tunnel that's running when you restart is made anew once.

## What's new since 1.0.0

### AgentOS: a Governor that works for you

- **The Governor** is an agent in a conversation of its own, on the model you
  choose (a fallback loop). It's woken for your tasks, on a regular round, when
  a service fails, and when the agents it started are done or seem to go wrong.
  It doesn't do the work itself:
  - it starts agents in managed chats and code tasks, on the models you allow
    (its own model included, for research and reviews);
  - it reviews their work, has a fresh reviewer look (`agent.review`), and
    looks into their WorkBoxes read-only (`workbox.look`);
  - it steers an agent that goes the wrong way (`chat.tell`);
  - it reports to you (`notify.report`), and asks you when it must
    (interventions, on the AgentOS page and as notifications).
- **It's held to its means:**
  - its capabilities only (`relay agentos …`, or MCP tools);
  - only what AgentOS made: your own chats are yours, and a fork of yours takes
    your permission;
  - only providers whose program Relay holds in a box, for its agents and
    when a fallback loop takes over in its conversations;
  - a budget of agents and money per wake-up, and at most a few wake-ups an
    hour for its agents' news and signs of trouble;
  - a log of everything it does.
- **Tasks in three kinds:** system tasks (a weekly services check, a weekly
  tidy-up, a weekly model ranking), the Governor's own, and your manual ones.
  Scheduled tasks of 1.0 are your manual tasks now.
- **The AgentOS page** is a dashboard: the Governor's state and log, its tasks,
  open interventions, the services, and its settings (model, round, budget,
  its own model for its agents).
- **A model ranking:** the Governor looks the models up on a benchmark, and
  Relay ranks them in the model picker, the fallback loops and for subagents.
- **Its conversations** are folded under "AgentOS conversations" in the
  sidebar.
- **It repairs providers:** when one keeps failing, the Governor has another
  agent repair Relay's support layer for it, tested in a shadow copy, for you
  to apply.

### Services (ProductionBoxes)

- **A project becomes a live service** behind Relay (`/promote`, or the New
  service dialog). It gets its own box, data volume, internal port and health
  check, at a public port of its own.
- **Versions:** Demote clones the live version into a WorkBox, with a copy of
  its data. Promote to live makes it the next version, health-checked first,
  and Roll back returns to an archived one.
- **Stop and Start** with the data kept. **Restart**, **Status** and **Logs**
  are there too, and a **shell** in the live box is on the service's page.
- **Health checks:** a failing service is told once, and the Governor sees to
  it when AgentOS looks after it. Its history (failures, data size per day) is
  kept for `service.status`.
- **`/port/<n>/`:** any server on the machine, and every service, through
  Relay's own address. That's for a Relay behind a reverse proxy.
- **`relay serve NAME CMD`:** a server an agent starts outlives its turn.
- **Backups include services' data:** `relay backup` has each service's
  volume, copied with its box paused for that moment (it doesn't answer
  meanwhile). A restored Relay puts it back when the service's box is made,
  and doesn't start the service on empty data when that fails.

### Agents' environment

- **Containers in a box:** podman, and docker as its other name, in
  WorkBoxes and ProductionBoxes. They run containers, build images and run
  `docker compose`, with no capabilities beyond the box's.
- **Node.js 22** in the image (was 20), with `n` to switch version.
- **VPN tunnels** (gluetun): a project folder's WorkBox can go out through a
  VPN. You set profiles and tunnels per workspace, with rotation, a fallback
  loop of exits, and notifications when one fails.
- **Relay's own web search** (a local SearXNG) and **web fetch** (a real
  browser when a plain fetch is refused, then Startpage's Anonymous View, then
  the Wayback Machine) for every agent.
- **Chats have a WorkBox too:** a chat's scratch folder, with Relay's tools.

### Agents and models

- **One memory for every agent** (Settings → Memory): what's lasting about
  you, and per project. Every agent gets it, whichever provider. Agents add
  to it when you tell them something lasting, and you see and change all of it.
- **Perplexity** as a provider, with your subscription (a browser in a box of
  its own), working as an agent with Relay's tools.
- **Commands:**
  - `/timed` keeps an agent at a task for a while;
  - `/clear` starts the agent afresh, with the conversation kept on screen
    (`delete-history` drops it);
  - `/promote` makes a project a service.
- **A free subagent** goes on with another free model when its own fails.
- **An agent's cut-off call:** when its own time-out stopped a call to
  Relay's tools, the answer comes with its next call.
- **Settings → Usage** shows what's left of each provider's usage limits.

### The app

- **One New conversation:** a project folder, chosen at the start or later,
  makes it a code task. A chat moved to a code task brings its files along.
- **The sidebar:**
  - AgentOS and the Governor;
  - Production boxes;
  - a service's WorkBoxes folded under the newest.
- **A conversation's status** next to its title, in a header half as high.
  On a phone the header is one row that scrolls sideways.
- **A better phone layout:** the keyboard no longer covers the message box.
- **Notifications by category:** your chats and tasks, waiting for you,
  AgentOS, reports, interventions and VPN problems. Each can be pushed (ntfy,
  a webhook), shown in the browser, and given an ntfy priority.
- **Settings → System:** what Relay runs on the server, and what it takes.
- **A running chat opens quietly**, and a 404 is Relay's own page.

## Upgrading from 1.0.0

- **Take a `relay backup` first.** Then `git pull` and restart Relay
  (Settings → Limits → Restart, or `systemctl --user restart relay`).
- **Updated at the first start:** the database is migrated, `vendor/` is
  brought up to date (pypdf is new), and `settings.json` keeps what you set.
  This is checked against a database and settings made by 1.0.0
  (`tests/relay/test_upgrade.py`).
- **The images:**
  - **The WorkBox image** (podman, Node.js 22) is built once, which takes
    minutes. Every WorkBox is made anew with it at its next use. Its home
    folder is kept, but what an agent installed with `apt-get` is gone.
  - **The ProviderBoxes' image** is built anew too.
  - **The old WorkBox image** stays until you remove it (Settings → System).
- **Your scheduled tasks** are your manual AgentOS tasks now, unchanged.
- **AgentOS is off** until you turn it on and choose the Governor's model on
  the AgentOS page. Choose a capable one (a Sonnet or a GPT-6 class model): a
  small model follows its instructions too loosely.

## Requirements

64-bit Linux, Python 3.10+ with pip (to install the bundled dependencies
once), git 2.40+, rootless podman, and the agent CLIs you use. Count on about
6 GB of disk for the container images (more for what agents pull in their
containers), and `/dev/net/tun` for VPN tunnels. See
[Requirements](../README.md#requirements).

## Known limitations

- **Pages under `/port/<n>/`** share Relay's address: a page there that hides
  its referrer can use Relay with your session. Open only servers you trust
  there. A subdomain per port would isolate them, but needs your proxy and
  DNS.
- **Containers in a box** share its network:
  - they make no networks of their own;
  - `-p` doesn't remap a port, and no port under 1024 works;
  - compose services reach each other by name, but can't share a port.
- **The Governor** is as good as its model. It can't reach your own chats,
  but it acts within its budget without asking, unless you list actions under
  "ask first".
- **A service's new version** runs on the live data while it's checked, so a
  version that fails may have changed the data (a migration). The data as it
  was before is kept (`production/<service>/snapshots/`), and the failure
  names it.
- **Restarting Relay** takes the services' addresses offline for a few
  seconds: Relay itself forwards them to the live versions.
- **A system task's default** (its prompt or schedule) changed by a new
  version doesn't reach a task you already have: **Reset to default** takes
  it over.
- **Perplexity** drives the website in a browser, so a change to the site can
  break it until the provider is repaired.
- One user per server in practice. Read-only modes only instruct the model
  (the Governor's look is the exception: it's read-only by its mounts).
- What 1.0.0 listed still holds: a subagent's copy has no git history or
  ignored folders, resizing the in-page shell needs a new shell on some
  podman setups, and starting a container (seconds) can't be stopped.

---

# Relay 1.0.0 — release notes

The first release (2026-10-02), after one release candidate (1.0.0rc1). Relay
is a self-hosted web app for chatting with AI models and running coding agents
(Claude Code, opencode, GitHub Copilot, any OpenAI-compatible API, or a
command of your own) on your own server, for one person on a local network.
The [README](../README.md) is the manual; [DEVLOG.md](DEVLOG.md) has the
decisions behind it.

## What's in it

- **Chat and code tasks** with any provider, side by side; answers stream
  in, runs keep going when the browser closes, and a conversation can switch
  provider or model halfway (the conversation so far is handed over).
- **Agents work in containers.** Each provider runs in a ProviderBox of its
  own; the commands its agent runs go, through `relay-shell`, to a Debian work
  container of the project (rootless podman). Nobody is asked per command;
  `relay.guard` still refuses machine-wrecking ones (`rm -rf /` and the like).
- **Every turn is recorded** outside your git history (Relay's checkpoints):
  review what an answer changed, rewind a turn, undo the rewind. Relay never
  commits by itself; Commit (or `relay commit` for an agent you ask) does, as
  you. A task can also work on a branch of its own and be merged as one commit.
- **Subagents on any provider or model** (`relay delegate`, `wait`, `tell`),
  with a policy for what may cost money, fallback when a model fails, and a
  copy of the folder per writing subagent, merged back when it's done.
- **Modes** (Plan, Build, Brainstorm, Read-only and your own; Shift+Tab in the
  message box), **skills** (the Agent Skills format), **scheduled tasks**,
  **your own computers** as remote shells, a **message queue**, **fallback
  loops** when a model hits its limit, **questions** from agents answered in
  the page, a **usage overview**, push notifications (ntfy or a webhook), the
  task folder as a **network drive** (WebDAV), an **interactive shell** in a
  task's WorkBox, and self-repair of a broken provider.
- Phones: responsive, installable as an app (over HTTPS), light and dark.

## Since 1.0.0rc1

- **Agents are told about Relay briefly** (about 150 tokens instead of about
  1,000 plus every skill's description): what Relay adds, that commands run in a
  container, that Relay never commits, the skills by name.
  **`relay help [container|task|subagents|computers|skills]`** has the rest, as it
  is for that conversation now.
- **Plain `relay`** in the agents' WorkBoxes, instead of a long path to the
  conversation's tools script: on every command's PATH and as a bash function
  (login shells), running the script of the conversation the command is for.
  No WorkBox is made anew for it.

## Since the last development snapshot (in 1.0.0rc1)

- **Subagents' unapplied work is kept.** A code subagent whose changes
  weren't handed over (it was stopped or failed, they conflicted, or you went
  on with it yourself) keeps its copy after its caller's run, and after a
  restart; its conversation shows the files and offers **Apply** (conflict
  markers where needed) and **Discard**. Going on with a subagent whose copy is
  gone gives it a new one. Before, that work was thrown away with the copy.
- **Restart Relay** in Settings (Limits → Server), e.g. after a `git pull`;
  it refuses while agents are working.
- **`relay backup`** writes all of Relay's data to one archive, with a
  consistent copy of the database, also while Relay runs.
- **Servers instead of a Raspberry Pi:** the install docs, `relay
  systemd-unit` (the user's runtime folder for rootless podman) and
  `install-relay.sh` (lingering in both modes, so containers work right after a
  reboot) are made for a regular Linux server; four subagents work at once by
  default (was three).
- **A new server's first answers** show that the agents' container is being
  prepared (podman's check and, once, the image build: minutes) and can be
  stopped meanwhile; before, they waited silently and couldn't be stopped.
- The UI walkthrough (`scripts/relay-ui-check.py`) passes again; it had
  fallen behind the containers, fallback loops and the UI overhaul.
- **Upgrading from a snapshot with subagents' copies:** boxes that still see
  the old, removed folder of copies (an older Relay re-made it at every start)
  are made anew when Relay starts; before, a writing subagent's first run after
  an upgrade failed with "no such file or directory".
- A subagent's copy gets the caller's small ignored files (`.env`, local
  settings), so its tests see what the caller's do.
- **After `git pull`, Relay brings `vendor/` up to date itself** when the
  dependencies changed (it starts anyway when that fails, e.g. offline);
  `vendor.sh` installs into a new folder and swaps it in when done, and says
  so when pip is missing.
- **`relay backup`** reads every file whole before it goes in, so a file a run
  writes meanwhile can't spoil the archive; the archive appears when complete.
- Subagents' copies stay when Relay can't tell whether they hold work (a full
  disk), follow a move of Relay or the project, and Apply/Discard can't race a
  subagent's new run. A run waiting for the providers' box image can be
  stopped too. After a move, the new workspace roots apply at once.
- "Rewind to the start" in the Changes panel has the Undo it promises.
- Deleting a chat removes its WorkBox and home volume at once (they
  used to wait for the idle cleanup, days later); the UI walkthrough removes
  the containers of its throwaway Relay.
- `relay doctor` warns about git older than 2.40 (needed to merge subagents'
  changes).
- A subagent whose copy couldn't be made, or whose start was cancelled, no
  longer hangs `wait` and the end of its caller's run.

## Requirements

64-bit Linux, Python 3.10+ with pip (to install the bundled dependencies once),
git 2.40+, rootless podman, and the agent CLIs you use. About 6 GB of disk for the container images. See
[Requirements](../README.md#requirements).

## Upgrading from a development snapshot

- Relay's data moved out of the code folder to `~/.local/share/relay`
  (`RELAY_HOME`) on 2026-10-01; a snapshot from before keeps its data in
  `data/` next to the code: move `data/relay/*` to `~/.local/share/relay/` and
  `data/profiles`, `data/state.json` to `~/.local/share/relay/opencode-manager/`
  while Relay is stopped. Stored paths (and Claude Code's session folders) are
  rewritten at the next start.
- The database is migrated at startup (checked from the very first schema of
  2026-09-28), and `vendor/` is brought up to date at the first start after the
  pull; nothing else to do. Take a `relay backup` first all the same.
- Agents' containers made by an older Relay are made anew when their
  definition changed (what an agent installed in one is then gone).

## Known limitations

- One user per server in practice: accounts exist, but everything Relay runs
  runs as the one system user, and agents can see that user's files.
- Read-only modes only instruct the model; they don't prevent changes.
- A subagent's copy has no git history and no ignored folders (build output);
  small ignored files (`.env`) are copied but don't go back; installed software
  (`node_modules`, virtual environments) is linked to the original's, so an
  editable install there still points at the original's sources. It works in a
  container of its own: what was installed in the caller's container isn't there.
- Resizing the in-page shell needs a new shell on some podman setups.
- Stop takes effect once a run's container and box are up: waiting for the
  images (minutes on a new server) can be stopped, starting a container
  (seconds) can't.
