# opencode-manager

> **Relay**, the web app for chat and coding agents that builds on this, is
> documented in [its README](../README.md) and [guide](GUIDE.md).

A tmux workspace for [opencode](https://opencode.ai): a sidebar on the
left, opencode on the right, and switchable **profiles** — each with its own
opencode config, auth, sessions and state.

The code and its Python dependencies live in this folder; profiles and
settings in `~/.local/share/relay/opencode-manager/` (`OPENCODE_MANAGER_HOME`
overrides it; `RELAY_HOME` moves it along with Relay's data).

```
┌─ opencode-manager ─┬──────────────────────────────────┐
│ PROFILES           │                                  │
│ ○ default          │                                  │
│ ● work             │            opencode              │
│ ○ personal         │                                  │
│                    │                                  │
│ PROJECT            │                                  │
│ ~/code/foo         │                                  │
└────────────────────┴──────────────────────────────────┘
```

## Requirements

- Linux or macOS (on Windows: inside WSL)
- Python 3.10+
- tmux 3.2+
- opencode 2.x on `PATH`

Textual and its dependencies are bundled in `vendor/`; nothing needs to be
installed with pip.

## Setup

```sh
sh scripts/vendor.sh        # once, to fill vendor/ (needs internet)
```

## Usage

```sh
/path/to/opencode-manager/opencode-manager [directory]   # start or attach
./opencode-manager -p work ~/code/foo                    # start with a profile
./opencode-manager profiles                              # list profiles
./opencode-manager edit work                             # edit a profile's opencode.json
./opencode-manager stop                                  # stop everything
```

### Sidebar keys

| Key     | Action                                                        |
|---------|---------------------------------------------------------------|
| `enter` | Switch opencode to the highlighted profile                    |
| `n`     | New profile (optionally copying this machine's opencode config) |
| `a`     | Log in (`opencode auth login`) for the highlighted profile    |
| `d`     | Delete the highlighted profile                                |
| `r`     | Restart opencode                                              |
| `o`     | Focus the opencode pane                                       |
| `e`     | Edit the highlighted profile's `opencode.json`                |
| `p`     | Open another project directory                                |
| `q`     | Quit: stops opencode and all profile services                 |

Switch between the panes with the usual tmux keys (`prefix` + arrow).

### Stopping

Quit with `q`, or run `./opencode-manager stop`. Both stop every profile's
opencode service.

## Scripting

The CLI doubles as the scripting interface. Commands that return data accept
`--json`; errors go to stderr.

```sh
./opencode-manager status --json                  # workspace, active profile, project, profiles
./opencode-manager profiles --json                # profiles with port and service state
./opencode-manager switch work                    # switch the running workspace to a profile
./opencode-manager open ~/code/foo                # open a project in the running workspace
./opencode-manager restart                        # restart opencode in the running workspace
./opencode-manager profile create client-x --copy-config
./opencode-manager profile delete client-x --yes  # --yes is required: auth and sessions are removed
./opencode-manager config path work               # print the path of a profile's opencode.json
./opencode-manager config check work              # validate it (exit 1 with line and column)
./opencode-manager oc -p work -- run "fix the failing tests"   # any opencode command, as profile "work"
./opencode-manager oc -- models                   # … as the active profile
```

| Exit code | Meaning                                                    |
|-----------|------------------------------------------------------------|
| 0         | Success                                                    |
| 1         | Error (message on stderr)                                  |
| 2         | Invalid usage                                              |
| 3         | The workspace is not running (`switch`, `open`, `restart`) |

`oc` runs opencode with the profile's config, auth and sessions, and takes
care of the profile's service port first, so it is safe to use while the
workspace runs. Changes made by scripts (active profile, project, new or
deleted profiles) show up in the sidebar within a second.

In `profiles` output, `service` is `running`, `stopped`, `port-taken` (another
program uses the profile's port; it moves on next start) or `null` (no own
service: the `default` profile, or `standalone` mode).

## Streaming agent sessions (bridge)

`stream` sends a prompt to an agent session and streams what happens as JSON
lines, in the same format for every backend:

- **opencode** (default): goes through the profile's own service, so every
  provider connected in that profile works (a ChatGPT account, OpenRouter, a
  local model, …).
- **Claude Code** (`-b claude-code`): runs `claude -p` in stream-json mode
  with your normal Claude Code login. Profiles don't apply.

Tools are enabled and permissions are approved automatically (`opencode
--auto`, `claude --dangerously-skip-permissions`) — **run it in an environment
where that is safe**.

```sh
./opencode-manager stream -p work -d ~/code/foo "add a test for the parser"   # new session
./opencode-manager stream -p work --continue "now run it"                       # same conversation
./opencode-manager stream -p work -s ses_123 "and commit"                       # a specific session
echo "long prompt" | ./opencode-manager stream -p work                          # prompt from stdin
./opencode-manager stream -p work --text "explain main.py"                      # readable output
./opencode-manager stream -b claude-code -d ~/code/foo "fix the build"          # Claude Code
./opencode-manager stream -b claude-code --continue "and add a test"
```

Claude Code only resumes a session from the directory it started in; the
bridge remembers that directory, so `--continue` and `--session` work from
anywhere.

Options: `-m MODEL` (`provider/model` for opencode, e.g. `sonnet` for Claude
Code) and `--agent NAME` for a new session, `--no-auto` to not auto-approve
(opencode leaves requests pending; Claude Code denies them). `ctrl+c` interrupts the run in
opencode as well (exit code 130).

Every line is one event; the first names the session and `done` is always
last:

| `type`        | Fields                                          |
|---------------|-------------------------------------------------|
| `session`     | `backend`, `session`, `profile`, `directory`, `new` |
| `step`        | `agent`, `model` — a model turn starts          |
| `text`        | `delta` — answer text                           |
| `reasoning`   | `delta` — thinking text                         |
| `tool_call`   | `id`, `name`, `input`                           |
| `tool_result` | `id`, `name`, `ok`, `output`                    |
| `permission`  | `id`, `action`, `resources`, `decision`: `once` (auto-approved), `denied` or `null` |
| `usage`       | `tokens`, `cost`, `billing`: `api` (pay per use), `subscription` (the cost is only what it would be at API prices), `free` or `null` (unknown) |
| `turn`        | `id` — opencode's id for this turn's message, to fork before it later |
| `error`       | `message`                                       |
| `done`        | `status`: `succeeded`, `failed` or `interrupted` |

Exit code: 0 succeeded, 1 failed, 130 interrupted.

## Switching projects

Press `p` to open another project without restarting the workspace. A popup
lets you type a path (`→` completes directory names; relative paths are
relative to the current project) or pick one of the recent projects with
`tab`. opencode restarts in the chosen directory with the same profile.

Recent projects are remembered in `state.json` in the data folder. Recent
projects that no longer exist are not shown.

## Editing opencode.json

Press `e` to edit the highlighted profile's `opencode.json` in a popup over the
workspace. The built-in editor ships with the folder, so it is the same on
every machine:

- `ctrl+s` saves. The file is checked first: JSON with comments and trailing
  commas (JSONC) is fine, like opencode accepts it. Invalid JSON is not saved;
  the error is shown and the cursor jumps to it. Saving is atomic, so an
  interrupted write can't leave half a file.
- `ctrl+q` closes, asking first if there are unsaved changes.
- A missing file starts from a template with opencode's `$schema`.
- Editing `default` changes *this machine's* opencode config; the title says so.

opencode reads its config at startup, so after a change the sidebar offers to
apply it by restarting the profile (and its service). To use your own editor,
set `"editor"` in `settings.json` in the data folder; if it isn't installed on a machine, the
built-in editor is used instead.

## Profiles

Profiles live in `profiles/<name>/` in the data folder and are fully
isolated:

| Directory | Passed to opencode as  | Contains                                   |
|-----------|------------------------|--------------------------------------------|
| `config/` | `OPENCODE_CONFIG_DIR`  | `opencode.json`, agents, `service.json`, … |
| `data/`   | `XDG_DATA_HOME`        | `opencode.db` (auth and sessions), logs    |
| `state/`  | `XDG_STATE_HOME`       | UI state                                   |

`XDG_CONFIG_HOME` is deliberately left alone, so tools opencode runs (git,
gh, …) keep using the machine's own config.

The built-in `default` profile is *this machine's* own opencode setup, without
any overrides; it isn't stored in the data folder.

> **A profile's `data/` contains your login credentials.**

## What is written outside the data folder

Only disposable caches: Python bytecode under `~/.cache/opencode-manager/`,
plus opencode's own cache in `~/.cache/opencode/`.

## Settings

`settings.json` in the data folder (all keys optional):

```json
{
  "launch_mode": "service",
  "sidebar_width": 32,
  "opencode_bin": "",
  "editor": "",
  "base_port": 49400,
  "session_name": "opencode-manager"
}
```

- `launch_mode`
  - `service` (default): each profile runs its own opencode background service
    on its own port (from `base_port` upward, skipping ports already in use
    on the machine; a profile whose port is taken moves to a free one). Switching profiles only replaces
    the client, so work in progress keeps running until you quit.
  - `standalone`: opencode runs with `--standalone`, a private server that
    stops when you switch profiles.
- `opencode_bin`: empty means `opencode` on `PATH`. A relative path is
  resolved against the code folder.
- `editor`: empty means the built-in editor; otherwise a command such as
  `"nvim"` or `"code --wait"`, which gets the file path as its last argument.

## Development

```sh
python3 -m pytest
```

Code layout (`src/opencode_manager/`):

| Module         | Responsibility                                          |
|----------------|---------------------------------------------------------|
| `cli.py`       | Command line entry point and output formatting          |
| `scripting.py` | Operations behind the scriptable commands               |
| `bridge/`      | Streaming agent sessions as uniform events (`stream`)   |
| `paths.py`     | Code, data and cache locations                          |
| `workspace.py` | Creating, controlling and stopping the tmux session     |
| `tmux.py`      | Thin wrapper around the tmux CLI                        |
| `opencode.py`  | Locating and invoking opencode, per-profile services    |
| `profiles.py`  | Profile model and on-disk store                         |
| `settings.py`  | User settings                                           |
| `state.py`     | Remembered state (last profile, recent projects)        |
| `configfile.py`| Locating, validating (JSONC) and saving opencode.json   |
| `sidebar/`     | The Textual sidebar app, dialogs and stylesheet         |
| `picker/`      | The project picker                                      |
| `editor/`      | The built-in opencode.json editor                       |

Dependencies are pinned in `requirements.lock`; update them there and rerun
`scripts/vendor.sh`.
