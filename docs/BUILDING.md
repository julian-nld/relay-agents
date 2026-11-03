# Building Relay from source

Relay has no compile step. It's Python and plain JavaScript modules that run from this folder, so "building" it comes
down to three things: its Python dependencies go into `vendor/`, its container images get built, and for working on it
you add the dev tools.

If you only want to run Relay, the [README](../README.md#install) is shorter.

## What you need

| | |
|---|---|
| **System** | 64-bit Linux (x86-64 or arm64): Debian 12/13, Ubuntu 22.04+, Fedora |
| **Python** | 3.10 or newer, with pip (`python3-pip` on Debian and Ubuntu) |
| **git** | 2.40 or newer. Older versions work, but `doctor` warns: merges use `git merge-tree --write-tree` |
| **podman** | rootless. Relay runs agents' commands in containers |
| **Disk** | about 6 GB for the images: the work image is about 3.3 GB, the box image about 430 MB, and the rest is the search and VPN boxes and what they pull |
| **systemd** | only to install it as a service. `./relay serve` runs without it |
| **Node.js** | only for the frontend's tests. Relay itself doesn't need it on the machine: the images have their own |

On Debian or Ubuntu:

```sh
sudo apt install podman git python3-pip
```

Check that rootless podman works, with the host networking Relay uses:

```sh
podman run --rm --network=host docker.io/library/python:3.13-trixie id
```

## Build and run

```sh
git clone https://github.com/julian-nld/relay-agents.git ~/relay && cd ~/relay
sh scripts/vendor.sh        # the Python dependencies, into vendor/
./relay doctor --build      # check the machine, and build the images (a few minutes, once)
./relay serve               # http://<this machine>:8420
```

Open the address and create your account right away: the first visitor gets it.

- **`scripts/vendor.sh`** installs what `requirements.lock` pins into `vendor/`. They're pure Python, so one `vendor/`
  works on every CPU. `PYTHON=` picks another interpreter. The launcher runs it again by itself when the lock file
  changed, after a `git pull`.
- **The images** are made from `src/relay/container/Containerfile` (where agents' commands run) and
  `src/relay/container/Boxfile` (where agents' own programs run). Their tags hold a hash of their files, so they're
  built again only when those change. Without `--build`, Relay builds them when they're first needed.
- **Your data** is in `~/.local/share/relay`. To try a build without touching an install, give it a folder of its own:

  ```sh
  RELAY_HOME=/tmp/relay-try ./relay serve --port 8500
  ```

  Keep that path short: long ones make a box's sockets fail with "AF_UNIX path too long". If the folder has no
  `settings.json` with `workspace_roots` of its own, `~/projects` is its workspace root.

## An agent to talk to

Relay needs at least one agent or model, from the machine's PATH:

- **opencode 2:** `curl -fsSL https://opencode.ai/v2/install | bash`, then a new shell. Its free Zen models need no
  account, so this is the quickest way to try a build. Relay needs version 2: opencode.ai's main installer still gives
  version 1, which has no `opencode service`.
- **Claude Code:** install it as Anthropic describes and run `claude` once to sign in.
- **GitHub Copilot:** `copilot login`.
- **Any OpenAI-compatible API:** add it in Settings → Providers.

## Install it as a service

```sh
sh scripts/install-relay.sh --user            # a user service (recommended: rootless podman runs as you)
sh scripts/install-relay.sh                   # or a system service running as you (asks for sudo)
sh scripts/install-relay.sh --user --port N   # another port
sh scripts/install-relay.sh --uninstall --user   # remove the service; your data stays
```

It bundles the dependencies if they aren't there yet, checks the machine with `./relay doctor`, installs and starts
the service, turns on lingering so it runs with nobody logged in, and prints the addresses. Logs are in
`journalctl --user -u relay`.

## Plugins

The plugins in `plugins/` need no build, and they're off until you link one and restart Relay:

```sh
ln -s ~/relay/plugins/server ~/.local/share/relay/plugins/server
./relay plugins              # what's there, and what's wrong with one that can't load
```

Two of them ship an extension, which they pack themselves when you ask for it:

- **VSCodium** packs `plugins/vscodium/extension/` as a `.vsix`, without npm (Settings → VSCodium).
- **Chrome** zips `plugins/chrome/extension/`, for Load unpacked (Settings → Chrome).

## An installer bundle

For a machine without a clone:

```sh
sh scripts/make-bundle.sh        # dist/relay-<version>.tar.gz: the newest tag (or HEAD, in a repository without tags)
sh scripts/make-bundle.sh HEAD   # what's committed now
sh scripts/make-bundle.sh v4.2.0 # a release
```

It holds what git has at that point, its Python dependencies and an `install.sh`. On the other machine, as the user
that will run Relay:

```sh
tar xzf relay-<version>.tar.gz && sh relay-<version>/install.sh
```

`install.sh` takes `--user` (the default), `--system`, `--port N`, `--dir DIR` and `--no-service`.

## The SDK

Relay makes the wheel of `sdk/python/` itself and serves it for pip at `/sdk/` on its own address, so there's nothing
to build: see [the SDK's README](../sdk/python/README.md).

## Tests and lint

Install the dev tools once, at the versions in `requirements-dev.lock`. They go in `vendor-dev/`, which git ignores:

```sh
python3 -m pip install --target vendor-dev -r requirements-dev.lock
PYTHONPATH=vendor-dev python3 -m playwright install chromium
sudo env PYTHONPATH=vendor-dev python3 -m playwright install-deps chromium   # once: Chromium's system libraries
```

Then:

```sh
PYTHONPATH=vendor-dev python3 -m pytest -q -n 6 --dist loadgroup   # all of it, in parallel: a few minutes
PYTHONPATH=vendor-dev python3 -m pytest -q tests/relay/test_forks.py   # one file
PYTHONPATH=vendor-dev python3 -m ruff check                        # lint
node --test tests/js/*.test.cjs                                    # the frontend's and the extensions' own tests
PYTHONPATH=vendor-dev python3 scripts/relay-ui-check.py /tmp/relay-shots   # click through the whole UI (about 17 minutes)
```

- **They don't touch your install:** the tests use a `RELAY_HOME` of their own (the root `conftest.py`), fake containers
  and fake agents. A few run on real podman, next to any Relay on the machine, and clean up only what they made.
- **Some skip themselves** without what they need (Node, Chromium, podman), and `tests/relay/test_docs.py` and the bundle's
  tests need a git repository.
- **Under load,** a timing test can fail now and then on a small machine. Run it on its own before you believe it.
- **Keep to the pinned tools:** with pytest 9.2, pytest-xdist 3.8's groups stop working, and the tests that share real
  podman collide (`Address already in use`).

The rules for working on the code are in [AGENTS.md](../AGENTS.md).

## Where things are

| | |
|---|---|
| `relay`, `opencode-manager` | the launchers: they set `PYTHONPATH` and run the code in this folder |
| `src/relay/` | the app: HTTP API, runs, containers, AgentOS, the scheduler ([ARCHITECTURE.md](ARCHITECTURE.md)) |
| `src/relay/static/` | the web interface: plain JavaScript modules and CSS, with its libraries in `vendor/` |
| `src/relay/providers/` | one folder per kind of agent ([ADDING-A-PROVIDER.md](ADDING-A-PROVIDER.md)) |
| `src/relay/container/` | the images: `Containerfile`, `Boxfile` and what goes in them |
| `src/relay/builtin_skills/` | the skills Relay ships |
| `src/opencode_manager/` | profiles and services for opencode, and the bridge to Claude Code and opencode ([opencode-manager.md](opencode-manager.md)) |
| `plugins/` | the plugins that come with Relay ([WRITING-A-PLUGIN.md](WRITING-A-PLUGIN.md)) |
| `sdk/python/` | the Python SDK and RESL |
| `scripts/` | installers, the bundle, the UI check, the icons |
| `tests/` | the tests (each provider folder has its own too) |
| `vendor/`, `vendor-dev/` | made by the commands above; git ignores them |
