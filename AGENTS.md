# Working on Relay

For anyone developing this repository, people and agents alike (Claude Code, opencode, Codex, …).
What Relay is: [README.md](README.md); all it does: [docs/GUIDE.md](docs/GUIDE.md); how it's built:
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Why things are as they are, round by round:
[docs/DEVLOG.md](docs/DEVLOG.md); add an entry there for each round of work.

**Your own machine:** if `AGENTS.local.md` exists (it's not in git), read it too. It holds what's particular to
this machine and its owner: their language, their server, which of their accounts and models tests may use.
Claude Code loads it through `CLAUDE.local.md`.

## Where things are

- **Code:** this folder; git, branch `main`. Installed as a service, the running Relay uses this code, and changes
  take effect when it restarts.
- **Relay's data:** `~/.local/share/relay` (`RELAY_HOME`): `relay.db`, chats, worktrees, checkpoints, subagents'
  copies (`forks/`), settings; opencode-manager's profiles in `opencode-manager/` there. A user's conversations
  are theirs: read them only when they say so, never change them.
- **Caches:** `~/.cache/opencode-manager` (tools scripts, container state, bytecode).

## Working agreements

- Code, comments, commit messages and docs are in English.
- **Commit only when asked**, one commit per subject, not one for a whole session (Relay itself never commits
  either).
- **Usability over security.** Relay is for one person on their own machine or LAN (a home server). Don't add
  prompts, refusals, confirmations or hardening nobody asked for. The one hard rail: `relay.guard` must never let
  `rm -rf /`-like commands through. [SECURITY.md](SECURITY.md) tells users what protects what: keep it true when
  you change any of it. The containers guard against accidents, not against an agent trying to get out: don't present
  them as more than that.
- HTTPS and remote access are left to the user's own setup (a reverse proxy, Tailscale): Relay doesn't do them.
- **Complement providers, never replace them:** each keeps its own tools, MCP servers and subagents; Relay adds
  alongside (a note, a script, an extra tool), never flags that take something away.
- Read-only modes (Plan, Brainstorm, Read-only) only instruct the model; don't bring back enforcement or
  per-command permission prompts. Agents' commands run in containers instead.
- When the environment gets in the way (podman broken, thread limits, a missing tool), say so plainly and early:
  that's for the machine's owner to fix. Don't take it for a bug in Relay.

## Relay's core is complete: new features are plugins

Since 4.0, Relay's own code gets **bug fixes, reliability and speed, docs, polish of what's there, and the extension
points plugins need**. A new feature is a **plugin** ([docs/WRITING-A-PLUGIN.md](docs/WRITING-A-PLUGIN.md)): a
folder in `RELAY_HOME/plugins`, with buttons, pages and Settings tabs, API routes, background tasks, a hook for runs
that end, and tools for agents.
- **When a plugin can't do it,** add the smallest extension point that lets it (a new part of `plugin`, the page's
  `relay`, or a hook), keep the old ones working, and raise `plugins.API` only when a plugin must know.
- **New provider kinds** stay provider folders ([docs/ADDING-A-PROVIDER.md](docs/ADDING-A-PROVIDER.md)).
- **The contract** is `plugin`, the page's `relay`, `relay.plugins`' `PluginError`, `Call` and `ToolCall`, and the
  HTTP API: changing one breaks the user's plugins. The guide's quotes and tables are checked by
  `tests/relay/test_plugins.py`.

## Architecture rules

- **Provider → support layer → standardised API → Relay.** Behaviour for all agents goes into the core or the
  interface (`providers/base.py`: a field on `Context`, a hook with a default on `Provider`), never a special case
  for one provider in `runs.py`, the routes or the frontend. What one program needs goes into its support layer:
  its folder `providers/<name>/` (e.g. `providers/claude/`, + `bridge/claude_backend.py`). Core code doesn't import
  a specific provider.
- **A new provider kind** is a folder `src/relay/providers/<name>/` and nothing outside it:
  [docs/ADDING-A-PROVIDER.md](docs/ADDING-A-PROVIDER.md) says how (its worked example is `providers/example/`).
  What the guide quotes from the example is checked by a test: change both together.
- **ProviderBoxes:** every provider's program runs in a box of its own (`relay.boxes`; all opencode-manager
  services share one). The box's `/bin/bash` and `/bin/sh` are `relay-shell`, which hands commands to Relay's exec
  gateway; they run in the project's WorkBox (`relay.containers`). New providers fit this, without per-provider
  shell tricks.
- **One API, thin clients.** A feature lives in Relay's HTTP API first. The web UI, the Python SDK (`sdk/python`)
  and AgentOS's tools are its clients.
  - **Into the API:** what a button does in more than one call, or decides itself (a name, a prompt, a default).
    Each client then calls that.
  - **The SDK** covers what a script needs, not settings, accounts or terminals. A test
    (`tests/relay/test_sdk.py`) keeps its calls to Relay's own routes.
- Match the surrounding code: its comment density (short comments that say why), naming and idiom. No new
  dependencies without a good reason (they're pinned in `requirements.lock` and installed into `vendor/`).

## Tests

```sh
PYTHONPATH=vendor-dev python3 -m pytest -q -n 6 --dist loadgroup   # all of it, in parallel: under 2 minutes
PYTHONPATH=vendor-dev python3 -m pytest -q tests/relay/test_forks.py
PYTHONPATH=vendor-dev python3 -m ruff check                        # clean
node --test tests/js/*.test.cjs                                     # the frontend's own tests
```

In parallel (pytest-xdist), tests that share something outside their own `tmp_path` run in one process, one after
another: the ProductionBoxes' tests on real podman, the other real-podman tests, and each Chromium check's file
(`conftest.py`, `SHARED`). A new test file of that kind goes there too. The suite mostly waits (on podman, browsers,
agents' replies), so it runs with more processes than a small machine has cores. Without `-n` everything runs
serially, in about 5 minutes.

`vendor-dev/` (ignored by git) holds the dev tools: `python3 -m pip install --target vendor-dev -r
requirements-dev.lock`, then `PYTHONPATH=vendor-dev python3 -m playwright install chromium` (and once, with sudo,
`install-deps chromium`). Keep to those versions: pytest 9.2 breaks pytest-xdist 3.8's groups.

Before a release, and after UI changes, also click through the whole UI in a headless browser (fake providers, a
throwaway Relay; fails on any console error):

```sh
PYTHONPATH=vendor-dev python3 scripts/relay-ui-check.py /tmp/relay-shots
```

Tests never touch the real `~/.local/share/relay` (the root `conftest.py` sets `RELAY_HOME`) or `~/projects` (a
default workspace root of their own: a Relay's start puts the scripts' folder there in order), never start the
machine's opencode services and use fake containers. That setup is in the root `conftest.py`, so a provider
folder's own tests (`src/relay/providers/<name>/tests`, collected with `tests/`) get it too. For changes the fakes
can't show (containers, boxes, a real provider), also try it for real against a throwaway Relay (a `RELAY_HOME` of
its own), in a scratch folder, and clean up afterwards.

Test Relays share podman with a live one on the same machine (its ProductionBoxes, WorkBoxes, ProviderBoxes): code
that cleans up containers must only touch those its own database knows, and never run a known-destructive version
against real podman, not even to show that a test catches it.

**Real models in tests and trial runs** cost someone's money or quota: prefer **free opencode Zen models** (e.g.
`opencode/big-pickle`), keep runs on a paid provider few and short on its cheapest model, and keep subagent fan-out
small (a few at a time). `AGENTS.local.md` may say which accounts and models may be used on a machine.

## Running and restarting Relay

Installed with `scripts/install-relay.sh --user`, Relay runs as the systemd user service `relay` (lingering on, so
it starts at boot; logs: `journalctl --user -u relay`). After a round of work, restart it, but first check that
none of the user's own agents is working and none of their scripts runs (AgentOS's agents and the Governor go on by
themselves after a restart, while AgentOS is on; a script would end):

```sh
python3 -c "import sqlite3,os; db = sqlite3.connect(os.path.expanduser('~/.local/share/relay/relay.db')); print(db.execute(\"SELECT count(*) FROM runs r JOIN conversations c ON c.id = r.conversation_id WHERE r.status NOT IN ('failed','interrupted','succeeded') AND c.managed = 0 AND c.agentos_role IS NULL AND c.parent_conversation IS NULL\").fetchone()[0] + db.execute(\"SELECT count(*) FROM script_runs WHERE status = 'running'\").fetchone()[0])"
```

If that's 0, and no one has a shell open (a restart ends the kept shells:
`ps -eo args | grep "[p]odman exec -it" | grep -c RELAY_TERMINAL=`), `systemctl --user restart relay`. Signed in,
`POST /api/restart` restarts it too, and refuses while the user's own agents work or their scripts run. Never stop
it with `pkill -f` on a pattern that's also in your own command. `./relay doctor` checks the machine, the
containers and the providers without starting anything.

## Pitfalls

- **Run opencode with `--standalone`** when you develop Relay with it. A plain `opencode` attaches to the
  background service, which Relay runs in its ProviderBox: your commands then run in a Relay WorkBox (no podman, no
  opencode, no `~/.local/share/relay`, no host processes), and your file tools see only what the box mounts.
- **Don't run opencode on the host with a profile's folders** (its `XDG_*_HOME` from `opencode-manager/profiles/…`),
  not even `opencode auth list`: it starts that profile's background service on the host, outside the ProviderBox.
  Relay can't use or stop that one, so the profile's model lists wait 30 seconds and come back empty, and its chats
  don't start. Read the profile's files instead; stop such a service if you started one.
- Agents that Relay itself starts work in containers on purpose: inside one you can't restart Relay or reach the
  host.
- Rootless podman: a workspace restart can break it. Check with
  `podman run --rm --network=host docker.io/library/python:3.13-trixie id` (host networking, as Relay uses: the
  default needs /dev/net/tun, which may not be there); if that fails, the machine needs fixing.
- Files a container wrote that you can't remove: `podman unshare rm -rf …`.
- A Relay with a `RELAY_HOME` of its own (a trial, the UI check) has an opencode of its own (its "default" profile
  is in its own data, no logins): use free Zen models there; it never touches the machine's opencode service or
  data.
