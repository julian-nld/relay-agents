# Adding a provider Relay doesn't know

A **provider** is something that answers in Relay: an agent's program (Claude Code, opencode, Copilot), a chat API,
or a website (Perplexity). This guide is for one Relay doesn't support yet. It shows how to find out whether you need
to write anything, and how to write the part that connects it to Relay (its **compatibility layer**, which the code
calls its *support layer*) when you do.

The guide builds a complete layer step by step: **toy-agent**, a tiny coding agent with a format of its own, in
[`src/relay/providers/example/`](../src/relay/providers/example/). The code you see here is quoted from that folder,
word for word, and a test checks that it stays so. Its tests run with Relay's, so the example always works.

**The short version:**
1. Check whether a kind Relay has already fits (section 1). Often no code is needed at all.
2. If not, make a folder `src/relay/providers/<name>/`: everything for your provider goes in it, and nothing
   anywhere else (section 3).
3. Write the layer: a `Provider` class that starts the program, and a `ProviderRun` that turns what it prints into
   Relay's events (section 4).
4. Test it: its own tests, then **Check it fully** in Settings → Providers (section 5).
5. Restart Relay and add a provider of the new kind (section 6).

A mistake in your folder never keeps Relay from starting: Relay leaves that folder out and says why
(section 7).

---

## 1. First: maybe a kind fits already

| The provider… | Use kind | Code to write |
|---|---|---|
| has an OpenAI-style chat API (`/v1/chat/completions`): OpenRouter, Ollama, LM Studio, llama.cpp, vLLM, many hosted models | `openai-compatible` | none: fill in `base_url` and maybe `api_key` |
| is an agent that speaks the [Agent Client Protocol](https://agentclientprotocol.com) (ACP), e.g. `gemini --experimental-acp` | `acp` | none: its `command` |
| is a model or service opencode supports (it connects to dozens) | `opencode` | none: connect it in opencode-manager's profile |
| is any program you can wrap in a small script that prints Relay's events | `command` | a wrapper script, outside Relay |
| is none of these | a new kind | a compatibility layer: this guide |

**About `command`:** it's the quickest way to try something. Its program runs on this machine as you set it up,
not in a box of its own. So AgentOS's agents don't use it, and Relay can't repair it. A layer in a folder of its
own gets all of that.

**About `acp`:** its program runs on this machine too, not in a box (Relay can't know what an arbitrary ACP agent
needs mounted). Its commands still go to the WorkBox, through Relay's shell as its `$SHELL`. A kind of its own puts
the program in a ProviderBox: Copilot's (`acp/provider.py`, `CopilotProvider`) subclasses `acp`'s for that, with
`box_key` and `box_mounts` (its program, its login folder). AgentOS's Governor may add only providers whose program
runs in a box.

---

## 2. How Relay talks to a provider

```
 Relay              your layer (your folder)          the program
   │  a turn ────▶  Provider.start()  ───── starts ──▶  (in its box)
   │                                                      │
   │  ◀── events ──  ProviderRun.events()  ◀── prints ────┘
```

**The uniform events** are small JSON objects with a `"type"`: `session`, `text`, `reasoning`, `tool_call`,
`tool_result`, `usage`, `error`, `done` and a few more ([the reference](#uniform-events) lists them all). Every
provider speaks these to Relay, so everything Relay does works the same for each.

**Where it runs:**
- **The program runs in its ProviderBox:** a podman container of its own (`relay/boxes.py`), with the program,
  its login and the folders it needs mounted in. The box's `/bin/bash` and `/bin/sh` are Relay's: each command
  the program runs is handed to Relay, which runs it in the project's **WorkBox**, past Relay's guard against
  `rm -rf /` and the like. So the program needn't cooperate, and the WorkBox never sees its login.
- **Your layer runs in Relay** (Python). It starts the program in the box (`context.box.wrap(…)`) and reads what
  it prints.

**What Relay does around your layer** (you don't write any of this):
- **The conversation:** messages, titles, attachments, forks, rewinding where the provider can.
- **Handovers:** a session that starts fresh in a conversation with messages gets the conversation so far
  (`context.agent_prompt`).
- **Modes** (Build, Plan, Read-only, …): their instructions reach the agent through your layer.
- **Relay's tools and skills:** the `relay` command in the WorkBox, or MCP.
- **Health and models:** which models work, and the model tests.
- **Usage limits and fallback loops:** another agent takes over when one stops.
- **Repairs:** another agent repairs your folder when the program changes under it.
- **AgentOS:** its Governor can use your provider for its agents.

---

## 3. The folder

Everything for your provider goes in one folder. You never need to change a file outside it.

```
src/relay/providers/<name>/
├── __init__.py        KINDS = (MyProvider,): what it adds; Relay finds it by this
├── provider.py        the layer: your Provider and its ProviderRun
├── …                  what else it needs: a backend, a script for its box, …
├── README.md          what it is, its options, what you learned of the program
└── tests/
    └── test_<name>.py its tests (collected with Relay's own)
```

**The rules:**
- **The folder's name** must be a Python name: lowercase letters, digits and `_` (`my_agent`, not `my-agent`).
- **The kind's name** (`KIND`) is lowercase letters, digits and `-` (`my-agent`). It must not be one Relay
  already has.
- **`__init__.py` names the kinds** the folder adds, as `KINDS = (MyProvider,)`. One folder may add more than one
  (`acp/` adds `acp` and `copilot`).
- **Its tests** go in `tests/test_<name>.py`. The suite collects `src/relay/providers/*/tests` and gives them the
  same protections as Relay's own tests: a throwaway data folder, no real podman, no real opencode service.
- **The support layer** (what a Repair copies) is the whole folder, except `tests/`.

Relay's own kinds live the same way: `claude/`, `opencode/`, `acp/`, `chat/` (openai-compatible), `command/`,
`perplexity/`. They're worth reading once yours does something theirs do.

---

## 4. Step by step, with toy-agent

### 4.1 Study the program first

Before writing anything, find out how the program behaves: run it by hand, read its `--help`, and try a few
prompts. Write down the answers to these questions (in your folder's `README.md`):

| Question | toy-agent's answer |
|---|---|
| How is it started? Where does the prompt go: an argument, or its stdin? | `python3 toy_agent.py`, with the prompt on stdin |
| What does it print? Something a program can read (JSON lines, a stream, a protocol)? | JSON lines, each with a `"kind"` |
| Does it keep sessions? How do you go on in one? | yes: `--resume ID`; the first line says the ID |
| How do you choose a model? An effort or thinking level? | `--model toy-small` or `toy-large`; no effort |
| How do you stop it? | it stops on SIGINT, like most |
| Does it run commands or change files? How? | it runs commands in `$SHELL` |
| Does it ask permission first? How, and how is it answered? | no |
| Where are its login, settings and sessions? | in `$TOY_AGENT_HOME` (default `~/.toy-agent`) |
| Does it tell its usage, cost or limits? | no |

toy-agent's output for "Reply with just the word OK.":

```
{"kind": "hello", "conversation": "3f9c2a1b7d10", "model": "toy-small"}
{"kind": "think", "words": "Let me see what's asked."}
{"kind": "say", "words": "OK"}
{"kind": "bye", "outcome": "ok"}
```

A real program's output is longer, but the job is the same: you map each kind of line onto Relay's events.

### 4.2 The kind

The class says what Settings shows (`LABEL`, `DESCRIPTION`), whether the provider has tools (`TOOLS`), which
options a template of it may have (`FIELDS`), and an example template (`EXAMPLE`, offered when you add one):

<!-- example/provider.py -->
```python
class ToyAgentProvider(Provider):
    KIND = "example"
    LABEL = "Example: toy-agent"
    DESCRIPTION = "The guide's worked example (docs/ADDING-A-PROVIDER.md): toy-agent, a tiny coding agent."
    TOOLS = True  # it runs commands itself: its shell is Relay's, so they run in the WorkBox
    LISTED = False  # (not offered in Settings: a template of it still works, to try it)
    FIELDS = (
        Field("models", "model-list", "Models to offer: toy-small, toy-large; \"\" is its default."),
    )
    EXAMPLE = {
        "id": "toy",
        "name": "Toy agent",
        "kind": "example",
        "options": {"models": [{"id": "", "name": "Default"}, "toy-small", "toy-large"]},
    }
```

**Fields:**
- **Their types:** `string`, `boolean`, `string-list`, `model-list` (ids, or `{id, name}`) and `object`.
- **Secrets:** a `Field(…, secret=True)` (an API key, a cookie) is never sent back to the browser.
- **Checking:** Relay refuses unknown options and wrong types itself. For more, override `validate()`, call
  `super().validate()`, and raise `TemplateError` with what's wrong.

**`LISTED = False`** keeps the example out of Settings → Add provider. Leave it out for yours.

### 4.3 Whether it can be used: `check()`

Settings → Providers → **Test**, `relay doctor` and the full check call `check()`. It returns a short status, or
raises `ProviderError` saying what's wrong: not installed, not signed in, too old.

<!-- example/provider.py -->
```python
    def check(self) -> str:
        if not PROGRAM.exists():
            raise ProviderError(f"{PROGRAM} isn't there")
        return f"toy-agent is in {PROGRAM.parent}"
```

**Keep it quick,** with nothing left running: `relay doctor` sets `Provider.probing` while it asks.

### 4.4 Its box

Your program runs in a ProviderBox of its own. You say which box and what goes in it:

<!-- example/provider.py -->
```python
    def folder(self) -> Path:
        return config.data_dir() / "example" / self.template.id

    def env(self) -> dict[str, str]:
        return {"TOY_AGENT_HOME": str(self.folder())}

    def box_key(self) -> str | None:
        return self.template.id

    def box_mounts(self) -> list[tuple[Path, bool]]:
        self.folder().mkdir(parents=True, exist_ok=True)
        return [(PROGRAM.parent, False), (self.folder(), True)]
```

- **`box_key()`:** the box's name. One per provider (its template's id) is usual. `None` means no box, which is
  only for chat APIs (nothing to run).
- **`box_mounts()`:** what of this machine the program needs in its box, as `(path, writable)`. That's the
  program itself (read-only; a link brings its target) and its login and settings (writable: it renews its own
  login). The workspace roots, Relay's chat folders and what Relay hands agents are mounted already.
- **`env()`:** what its environment needs besides that: its config folder, say. A repair's `repair_run` uses it
  too.
- **`box_image()`:** `None` gives Relay's box image (Debian with `python3`, Node.js 22 with `npm`, `git`, `curl`,
  `ripgrep`). `"workbox"` gives the WorkBox image, with Chromium and Playwright, for a program that needs a
  browser (as Perplexity does).
- **`box_home()`:** a home of its own in its box, its template's `home`. A provider added with an agent (§8) has
  one: the user installed its program in it and logged in there, in a shell in its box. Relay mounts it as the
  box's `HOME`, with what's installed there first on `PATH` (`.local/bin`, where `npm i -g` puts it too).
  `home_program(name)` finds the program there, for `check()`; the layer mounts nothing else for it.
- **`CONFINED`** (true by default): the program runs where Relay puts it. AgentOS's agents only run on such
  providers.

The box keeps running between turns. Relay makes it anew when its mounts change and no run is using it.

### 4.5 A turn: `start()`

`start(context, prompt)` builds the command line and returns a `ProviderRun`. `context` tells you about the
conversation ([all its fields](#context)): its folder (`directory`), `model`, `effort`, and the program's session
to go on in (`provider_session`, `None` for a fresh one).

<!-- example/provider.py -->
```python
    def start(self, context: Context, prompt: str) -> ProviderRun:
        resume = context.provider_session is not None
        argv = ["python3", str(PROGRAM)]
        if resume:
            argv += ["--resume", context.provider_session]
        if context.model:
            argv += ["--model", context.model]
        # A fresh session gets the conversation so far (a handover); toy-agent has no system prompt, so the
        # mode's instructions and Relay's note about its tools come in the message.
        text = context.agent_prompt(prompt, fresh=not resume, instructions=True).prompt
        env = self.env()
        if context.box is not None:
            # In its box /bin/sh is Relay's: each command goes to the WorkBox of this folder.
            argv = context.box.wrap(argv, context.directory, {**env, "RELAY_FOLDER": str(context.directory)})
            env = dict(os.environ)
        else:  # (without boxes: on this machine, its commands through the run's shell, in the WorkBox)
            argv = [sys.executable, *argv[1:]]
            env = {**os.environ, **env, **({"SHELL": str(context.shell)} if context.shell else {})}
        self.folder().mkdir(parents=True, exist_ok=True)
        return ToyRun(argv, context.directory, env, text)
```

**`context.agent_prompt(prompt, fresh, instructions)`** gives what to send the program:
- the message, with a list of its attachments (the program reads them with its own tools);
- with `fresh=True` in a conversation that has messages already, a **handover**: the conversation so far, so a
  fresh session (after a fallback, a provider switch or a fork) knows what happened;
- with `instructions=True`, the **mode's instructions** and **Relay's note about its tools**, in the message. Use
  it when the program has no system prompt. A program that has one (Claude Code's `--append-system-prompt`) gets
  `context.agent_mode.guidance` and `context.tools_note` there instead, and `instructions=False`.

**Commands land in the WorkBox:**
- **In its box,** pass `RELAY_FOLDER` (the run's folder) in the environment. The box's shell sends each command
  to that folder's WorkBox.
- **Without boxes** (a machine without podman, or a test), the run's own shell is `context.shell`: set it as the
  program's `$SHELL`.

**Start the program in `events()`, not in `start()`,** so a run stopped before it began never starts it.

### 4.6 Its output as Relay's events

`ProviderRun.events()` yields Relay's events until, and including, one `done`. This is the heart of the layer:

<!-- example/provider.py -->
```python
    def events(self) -> Iterator[Event]:
        if self.stopped:  # (stopped before it started)
            yield {"type": "done", "status": "interrupted"}
            return
        if shutil.which(self.argv[0]) is None:
            yield {"type": "error", "message": f"{self.argv[0]} isn't there"}
            yield {"type": "done", "status": "failed"}
            return
        self.process = subprocess.Popen(self.argv, cwd=self.directory, env=self.env, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                        start_new_session=True)
        stderr = StderrTail(self.process.stderr)  # (drained, or a chatty program blocks)
        if self.stopped:  # (stopped while it started)
            stop_process(self.process)
        try:
            self.process.stdin.write(self.prompt)
            self.process.stdin.close()
        except BrokenPipeError:
            pass
        ended = False
        for line in self.process.stdout:
            try:
                said = json.loads(line)
            except json.JSONDecodeError:
                continue  # (not one of its events: a stray print)
            for event in translate(said):
                ended = ended or event["type"] == "done"
                yield event
        self.process.wait()
        if not ended:  # (it stopped without its bye: stopped by us, or it fell over)
            if self.stopped:
                yield {"type": "done", "status": "interrupted"}
                return
            yield {"type": "error", "message": stderr.last() or f"toy-agent exited with code {self.process.returncode}"}
            yield {"type": "done", "status": "failed"}
```

The translation of one line:

<!-- example/provider.py -->
```python
def translate(said: dict) -> list[Event]:
    """One of toy-agent's lines as Relay's events (none for what Relay needn't know)."""
    kind = said.get("kind")
    if kind == "hello":
        return [{"type": "session", "session": said["conversation"]},
                {"type": "step", "agent": "toy-agent", "model": said.get("model")}]
    if kind == "think":
        return [{"type": "reasoning", "delta": said["words"]}]
    if kind == "say":
        return [{"type": "text", "delta": said["words"]}]
    if kind == "use":
        return [{"type": "tool_call", "id": said["call"], "name": said["tool"], "input": said.get("args") or {}}]
    if kind == "used":
        return [{"type": "tool_result", "id": said["call"], "name": "run", "ok": not said.get("failed"),
                 "output": said.get("result") or ""}]
    if kind == "bye":
        if said.get("outcome") == "ok":
            return [{"type": "done", "status": "succeeded"}]
        return [{"type": "error", "message": said.get("why") or "toy-agent failed"},
                {"type": "done", "status": "failed"}]
    return []
```

| toy-agent says | Relay's events |
|---|---|
| `hello` (its session, its model) | `session`, then `step` |
| `think` | `reasoning` |
| `say` | `text` |
| `use` (a tool call) | `tool_call` |
| `used` (its result) | `tool_result`, with the same `id` |
| `bye` ok / error | `done` succeeded, or `error` then `done` failed |
| anything else | nothing: what Relay needn't know, it isn't told |

**The rules Relay relies on** (the check tests those it can see in the events):
- **A `session` event** names the program's session, so the next turn goes on in it (`context.provider_session`).
  Send it as soon as you know it.
- **Exactly one `done`, last,** with `succeeded`, `failed` or `interrupted`. Whatever happens (the program crashes,
  prints garbage, is stopped), `events()` ends with one.
- **An `error` event before `done: failed`** says why, in words a person understands: the program's last stderr
  line is often good.
- **Text comes in pieces:** `text` and `reasoning` are deltas, appended as they come. Send them as soon as they
  come; Relay shows them live.
- **Tool calls and results share an `id`.** A result's `ok` is false for a failed command; its `output` is text.
- **Usage, cost and limits**, if the program tells them:
  - `usage` is `{"tokens": {"input_tokens", "output_tokens", …}, "cost", "billing"}`; its billing is `api`,
    `subscription` or `free`;
  - `limit` is `{"status": "allowed" | "warning" | "rejected", "resets_at", "window"}`: a plan's usage window.
    Relay's fallback and its "resumes at" use it.
- **Don't buffer:** yield as you read. Read stdout line by line, and drain stderr in the background
  (`StderrTail`), or a program that prints a lot to stderr blocks.

**When a turn fails,** Relay reads it as the model stopping:
- **What it does:** an agent of the conversation's fallback loop takes over (Settings → Usage). Without one, the
  chat goes on by itself when the limit resets. When nothing says when, it tries once more after a few hours
  ("Resumes at …").
- **A usage limit:** a `limit` event (`rejected`, with `resets_at`) tells Relay best. An `error` in the
  program's words works too: Relay reads "rate limit", "429", "quota", and a time such as "resets in 3 hours".
- **A model that can't be used** (not found, not on this plan): say so in the error ("model not found"). Relay
  then doesn't wait for a reset that won't help.
- **A session the program no longer knows** (it lost it after a stop, or it expired): don't fail every turn
  after. Start a fresh session instead, with `context.agent_prompt(prompt, fresh=True)` for the handover. Or fail
  once with a clear error; the user can then fork or start anew.

### 4.7 Stopping: `interrupt()`

Relay calls `interrupt()` from another thread when the user presses Stop, or a run is cut off. Stop the program
soon; `events()` then ends with `done: interrupted`:

<!-- example/provider.py -->
```python
    def interrupt(self) -> None:
        self.stopped = True
        stop_process(self.process)
```

`stop_process` (from `opencode_manager.bridge.process`) sends SIGINT to the program's process group, and SIGKILL
when it doesn't stop in 5 seconds. Start the program with `start_new_session=True` so its children are in its
group. In its box, Relay also ends what the run started there when the run ends (`relay-reap`), so a program
that leaves a process behind doesn't keep it running.

### 4.8 Permissions

Many agents ask before they run a command or change a file. Relay answers itself: nobody is asked, because the
commands run in a WorkBox. Your layer decides how the question reaches Relay:
- **The program asks through a protocol** (ACP's `session/request_permission`, an HTTP callback): call
  `self.provider.judge(action, detail, folder)` with what it wants to do. It returns `("allow" | "deny", why)`.
  Answer the program with that, and yield a `permission` event with the decision (`once` when allowed, `denied`
  when refused) so the page shows it.
- **The program asks through an MCP tool** (Claude Code's `--permission-prompt-tool`): point it at
  `context.mcp_url`, or `context.approvals_url` when that's all there is (a subagent's run, the check). Relay
  calls your `judge()` from there.
- **It leaves the question open until it's answered** (opencode's): yield `permission` with `"decision": null`,
  return `True` from `ProviderRun.waits_for_answers()`, and pass the answer on in `answer_permission(id, approve)`.
- **It never asks:** nothing to do (toy-agent).

**`judge()` by default** allows any command that passes Relay's guard. Override it when only your layer knows
which of the program's tools change files (allowed only in the project) or read them (`reads`: AgentOS's agents
read only their own folder; `unreadable()` in `base.py` helps).

**Forms:** a program that asks the user questions (with choices) yields a `form` event with `status: pending`.
It then answers with `answer_form(id, answer)`, or `None` when the user cancels.

### 4.9 Relay's tools and skills

Relay offers every agent its own tools: subagents, the user's computers, web search and fetch, memory, skills.
- **An agent with a shell** (`TOOLS = True`) gets them as the `relay` command in its WorkBox.
  - **Telling it:** `context.tools_note` says how; `context.agent_prompt(…, instructions=True)` puts that note in
    the message when the session hasn't heard it yet (`context.tell_tools`).
  - **A program with a system prompt** gets the note there instead.
- **An agent that takes MCP servers** can get the tools that way instead: set `RELAY_TOOLS_VIA_MCP = True` and
  give the program `context.mcp_url` as an MCP server. There's no shell quoting that way.
- **Skills:**
  - **Listed:** Relay names its skills in the note; `relay help skills` tells the agent what each is for and
    where its `SKILL.md` is.
  - **Loaded by the agent itself:** set `NATIVE_SKILLS = True` when the program loads skills from a folder of
    Claude Code's shape. Relay then gives it `context.skills_dir`.
- **A chat model without tools of its own** can get Relay's as functions instead: Relay then runs the tool loop
  for it (`runs_tools()`, `relay/toolbox.py`; see `chat/`).

### 4.10 Models and efforts

- **`models()`** gives the models to offer, as `[{"id", "name"}]`. By default these are the template's `models`
  option, and `""` is the program's own default. Ask the program when it can list them (opencode's does).
- **`model_id(model)`** turns a model as a user or a script may write it ("claude-code/sonnet") into the
  program's name for it.
- **`efforts(model)`** gives the reasoning efforts a model takes, of `low`, `medium`, `high`, `xhigh` and `max`.
  Use **`model_efforts()`** when they differ per model.
- **`billing(model)`** says who pays (`api`, `subscription` or `free`), when known before a run.
  **`free_models()`** follows from it.
- **`local_server(model)`:** a model that runs on a machine of yours (an Ollama box) names it here, so Relay
  doesn't load two models on one GPU at once.

### 4.11 The rest, when your program has it

| Hook | What for |
|---|---|
| `limits()` | What's left of its plan's usage windows, for Settings → Usage, asked with its own login (never renewing it) |
| `commands(directory)` | Its own slash commands: Relay passes them on instead of treating them as text |
| `suggest_title(prompt, answer, model)` | A title for a new chat, when the provider can make one cheaply |
| `abandon(session, directory)` | Stop what a session may still be doing after Relay was cut off (its server kept running) |
| `FORKS` | Whether it can copy a session for a fork (`end`, or `any` with `fork_before`); else a fork gets a handover |
| `REWINDABLE` | Whether a turn can be taken back (true for chat APIs: Relay holds their history) |
| `detached(command)` | A command its program detaches on purpose, to outlive the run (a dev server): it's left running |
| `PROGRAMS` | The programs it runs (by name: `claude`, `opencode`), for whoever reads the layer |
| `SIGN_IN` | The command that signs its program in again (`claude auth login`), named by its **Shell** and at Add provider's next step |
| `SHELL_HINT` | What else its **Shell** is for, in a sentence |
| `shell_ready()` | Before a shell opens in its box: what its program attaches to there, running (a service of its own), so that it isn't started from the shell and doesn't end with it |
| `prepare_new()` | At Add provider's next step: what a new provider's template names that isn't there yet, made (a profile, say). It isn't undone when the provider isn't added |
| `detail()` | A short note in the list of providers (what it runs against) |

---

## 5. Testing

### Its own tests

Write them in `tests/test_<name>.py` in your folder. The example's are a good start
([`example/tests/test_example.py`](../src/relay/providers/example/tests/test_example.py)):
- the translation: lines the program prints → Relay's events, checked with `validate_events` (below);
- how a turn starts: the command line in its box (a fake `context.box` records what `wrap` got), resuming, the
  model;
- a fake of the program, when the real one can't run in a test: a small script that prints what the program would
  (`tests/relay/test_conformance.py` has one).

Run them:

```sh
PYTHONPATH=vendor-dev python3 -m pytest -q src/relay/providers/<name>
```

### The check

`relay.providers.conformance` checks a layer the way Relay uses it. It runs the provider as a run does (its
program in its box, its commands in a WorkBox, its permission requests answered by `judge()`), in a scratch folder,
through a few short prompts to a real model. It checks every event that comes back:

| Scenario | It passes when |
|---|---|
| Loads | the kind loads and is valid |
| check() | it gives a status, or a `ProviderError` that says why |
| Models | `models()` gives `{id, name}`, and `efforts()` only known efforts |
| A turn | "Reply with just the word OK": every event as described, a `session`, one `done` last, some text |
| Going on | a second turn in that session works |
| Stopping | a slow count, stopped after its first text: `done: interrupted` within 15 seconds |
| A command (`TOOLS` only) | "run `echo relay-check-…`": a `tool_call`, and the output comes back |

Ask for it in one of three ways:
- **Settings → Providers →** the provider's shield (**Check it fully**). You choose the model.
- **On the command line,** with Relay running: `./relay provider-check <id> [--model m]`.
- **In your tests:** `conformance.check(provider, model, scratch_folder)` runs it without boxes, and
  `conformance.validate_events(events)` checks a list of events.

**Its prompts,** for a fake or stand-in program that has to answer them:
- "Reply with just the word OK." (the turn; then "Reply with just the word OK again." in its session);
- "Count from 1 to 300, one number per line, and nothing else." (stopped after its first text);
- "Run this shell command and tell me what it printed: echo relay-check-…" (an agent with tools, in a code task).

A scenario marked – couldn't be shown (it finished before it could be stopped, say). It doesn't count against
the layer.

**What its failures usually mean:**
- **"no session event":** the first line naming the session wasn't translated.
- **"one done, not 0":** `events()` ended without `done`, after an exception or when the program crashed.
- **Stopping is still going:** `interrupt()` didn't stop the program, or `events()` didn't notice.
- **A command, "no tool_call event":** the program's tool use isn't translated, or it ran no command.

### For real

Restart Relay (it finds new folders when it starts), add a provider of your kind, and use it: a chat, a code task
with commands, a follow-up, Stop. Look at the run in **Settings → Providers → Models** and at `journalctl --user -u
relay` (where Relay says what a layer raised).

---

## 6. Using it

1. **Restart Relay:** `systemctl --user restart relay`, or Settings → Limits → **Restart Relay**. A new or changed
   folder is read when Relay starts.
2. **Add a provider:** Settings → Providers → Add provider → your kind. You get its `EXAMPLE` to fill in. Or write
   the template yourself: `~/.local/share/relay/providers/<id>.json`, as in the [guide](GUIDE.md#providers).
3. **Try it:** Test, Models, Check it fully.

---

## 7. When something's wrong

**A folder that can't be loaded:** a syntax error, a failing import, no `KINDS`, or a kind that isn't one (a
`KIND` that's taken or malformed, `start()` not written, an `EXAMPLE` its own check refuses).
- **Relay starts anyway,** without that folder, and the other providers work.
- **It says why,** with the file and line, in three places:
  - Settings → Providers → **Provider folders that can't be loaded**;
  - `./relay doctor` (also with `--no-providers`);
  - the log.
- **A provider of that kind** is listed under Broken templates until the folder loads again.

**A layer that raises** where Relay asks it something (its options check, its models, efforts, billing): that
provider's trouble, said, never the page's.
- **Its options check:** the template can't be saved, and says why.
- **Its models:** the model list says why.
- **Its efforts, billing and the like:** none are known, and the reason is in the log.
- **A run whose layer raises** fails with the message.

**When the program changes** (a new version with other output), Relay can have another agent **repair** your
layer: Settings → Providers → 🔧 opens its page. The agent works on a copy of your folder and hands in a plan
first. Relay checks its change fully and starts a throwaway Relay with it to try, and you apply it
([the guide: Repairing a provider](GUIDE.md#repairing-a-provider)). Your folder's `README.md` and tests help that
agent as much as they help you.

---

## 8. With an agent

The built-in skill **relay-provider-writer** has an agent build a layer by this guide. Three ways to start one:
- **Settings → Providers → Add provider → A provider Relay doesn't know → Add it with an agent** (AgentOS's
  `provider.create` sends you there too). Its page has three steps ([the guide: added with an
  agent](GUIDE.md#a-provider-relay-doesnt-know-added-with-an-agent)):
  1. You install the program in a box of its own and log in there, in a shell on the page. The box's home
     (`box_home()`) keeps both, and the provider gets it.
  2. Your agent studies the program there (`repair_run`) and hands in a plan (`provider_plan`). You approve it, or
     ask for another.
  3. It builds the folder from this guide's example, in a folder Relay made for it (with Relay's source and this
     guide to read, and `CREATE.md`), and tests it with `repair_check`: this guide's whole check, in a box of its
     own. Relay checks it once more and starts a throwaway Relay with it for you to try; **Add** puts it in
     `src/relay/providers/<name>/`, with its provider, and restarts Relay.
- **In Relay, in Relay's folder:** a code task there (when it's in one of your workspace roots). Ask
  `/relay-provider-writer <the program, and where it's installed>`. It works on Relay's source itself.
- **In a terminal:** an agent working in `~/relay` (Claude Code, opencode `--standalone`). Ask it to follow
  `src/relay/builtin_skills/relay-provider-writer/SKILL.md`.

The last two study the program and write the folder and its tests; you then restart Relay, add the provider and check it.

---

## Reference

### Uniform events

Every event is a JSON object with a `type`; the fields below, and more where a provider has them.

| Type | Fields | Meaning |
|---|---|---|
| `session` | `session`; `new` | the program's session, to go on in next turn |
| `step` | `agent`, `model` | a model turn starts (which agent and model) |
| `text` | `delta` | answer text, a piece at a time |
| `reasoning` | `delta` | thinking text, a piece at a time |
| `tool_call` | `id`, `name`, `input` | the agent uses a tool |
| `tool_result` | `id`, `name`, `ok`, `output` | its result (same `id`) |
| `permission` | `id`, `action`, `resources`, `decision` | a permission asked: `once` (allowed), `denied`, or `null` (open) |
| `form` | `id`, `title`, `fields`, `status`, `answer`, `message`, `expires` | questions for the user: `pending`, `answered`, `cancelled` |
| `usage` | `tokens`, `cost`, `billing` | what the turn used: `api`, `subscription` or `free` |
| `limit` | `status`, `resets_at`, `window`, `windows` | its plan's usage limit: `allowed`, `warning`, `rejected` |
| `title` | `title` | a title for the conversation |
| `turn` | `id` | the program's id for this turn's message (to fork before it) |
| `error` | `message` | what went wrong, for a person |
| `done` | `status` | always last: `succeeded`, `failed` or `interrupted` |

### Provider hooks

The class attributes and methods of `Provider` (`src/relay/providers/base.py`), and of the run it returns.

| Hook | Default | What it's for |
|---|---|---|
| `KIND` | (required) | the kind's name in templates |
| `LABEL` | (required) | its name in Settings |
| `DESCRIPTION` | (required) | a sentence in Settings |
| `TOOLS` | (required) | it runs an agent with tools of its own |
| `EXAMPLE` | (required) | the template offered when one is added |
| `FIELDS` | none | its options |
| `LISTED` | true | offered in Settings → Add provider |
| `CONFINED` | true | its program runs where Relay puts it (AgentOS's agents only use such) |
| `REWINDABLE` | false | a turn can be taken back |
| `FORKS` | `none` | how it copies a session: `end`, `any` or `none` |
| `PROGRAMS` | none | the programs it runs, by name |
| `SIGN_IN` | none | the command that signs it in again, in its Shell |
| `SHELL_HINT` | none | what else its Shell is for |
| `NATIVE_SKILLS` | false | it loads skills itself from `context.skills_dir` |
| `RELAY_TOOLS_VIA_MCP` | false | it takes Relay's tools as MCP tools |
| `SUPPORT_FILES` | none | files of its support layer outside its folder (Relay's own only) |
| `boxes` | set by Relay | Relay's ProviderBoxes |
| `probing` | false | set while `relay doctor` asks: start nothing |
| `options` | the template's | its options |
| `validate()` | checks `FIELDS` | its own options check: raise `TemplateError` |
| `check()` | (required) | can it be used: a status, or `ProviderError` |
| `start(context, prompt)` | (required) | start a turn: a `ProviderRun` |
| `models()` | the `models` option | the models to offer |
| `model_id(model)` | as given | a model as the program names it |
| `efforts(model)` | none | a model's reasoning efforts |
| `model_efforts()` | none | efforts per model, when they differ |
| `billing(model)` | unknown | who pays: `api`, `subscription`, `free` |
| `free_models()` | from `billing` | the models that cost nothing |
| `limits()` | none | its plan's usage windows, asked now |
| `local_server(model)` | none | the machine a local model runs on |
| `runs_tools()` | false | Relay runs the tool loop for it (a chat model) |
| `has_tools()` | `TOOLS` or `runs_tools()` | it can do code tasks |
| `commands(directory)` | none | its own slash commands |
| `suggest_title(prompt, answer, model)` | none | a title for a new chat |
| `abandon(session, directory)` | nothing | stop a session cut off by Relay stopping |
| `detail()` | none | a note in the list of providers |
| `box_key()` | none | its ProviderBox |
| `box_mounts()` | none | what its program needs in its box |
| `box_image()` | Relay's box image | `"workbox"` for one with a browser |
| `box_home()` | the template's `home` | a home of its own in its box, where its program was installed and logged in |
| `home_program(name)` | (from `box_home()`) | where its program is in that home, e.g. for `check()` |
| `has_shell()` | it has a box or a home | it has a **Shell** |
| `box_shell()` | the box's real bash | the real shell in its box (`/bin/bash` there is Relay's) |
| `shell_ready()` | nothing | what its program attaches to in its box, running before a shell opens there |
| `prepare_new()` | nothing | what a new provider's template names that isn't there yet, made at Add provider's next step |
| `boxed()` | `box_key()` is set | nothing of it runs outside a box of Relay's (a chat API: true); AgentOS's Governor may add only such |
| `detached(command)` | false | a command it detaches on purpose |
| `env()` | none | its program's environment |
| `judge(action, detail, folder, reads)` | the guard | Relay's answer to a permission request |
| `support_files()` | its folder | its support layer, for repairs |
| `describe()` | from the above | what Settings is told about the kind |
| `events()` | (required, on the run) | the turn's events, ending with `done` |
| `interrupt()` | (required, on the run) | stop the turn soon |
| `waits_for_answers()` | false (on the run) | it waits for answers to open permissions |
| `answer_permission(id, approve)` | refuses (on the run) | pass an answer on |
| `answer_form(id, answer)` | refuses (on the run) | pass a form's answer on |

### Context

What `start()` gets about the conversation (`Context` in `src/relay/providers/base.py`).

| Field | What it is |
|---|---|
| `conversation_id` | Relay's id of the conversation |
| `directory` | the folder the agent works in (a project's, or a chat's own) |
| `mode` | `chat` or `code` |
| `model` | the model chosen (`None`: its default) |
| `provider_session` | the session to go on in (`None`: a fresh one) |
| `history` | the earlier messages (chat APIs get them every turn; agents in a handover) |
| `attachments` | this message's files (`path`, `name`, `mime`) |
| `fork_from` | a session to copy, for a fork |
| `fork_before` | up to which of its messages (`FORKS = "any"`) |
| `agent_mode` | the mode: its `instructions`, `guidance` and `access` |
| `mode_before` | the previous turn's mode, when it changed |
| `mode_switched` | the mode changed since the last turn |
| `effort` | the reasoning effort chosen |
| `shell` | a `$SHELL` that runs commands in this folder's WorkBox |
| `shared_shell` | one for a server that serves many folders |
| `browser_argv` | how to start Relay's browser in the WorkBox |
| `browser_log` | where that browser logs |
| `box` | its ProviderBox launch: `box.wrap(argv, cwd, env)` |
| `mcp_url` | Relay's MCP endpoint for this run |
| `approvals_url` | where a subagent's permission requests go |
| `read_boundary` | what its file tools may read (AgentOS's agents); `context.reads()` |
| `tools_path` | Relay's `relay` command for this run |
| `tools_note` | what to tell the agent about Relay's tools |
| `tell_tools` | the session hasn't been told yet |
| `skills_dir` | Relay's skills, for an agent that loads them itself |
| `computers` | the user's computers agents can work on |
| `computer` | the one a computer task works on |
| `search` | Relay's web search (a function), or `None` |
| `fetch` | Relay's web fetch (a function), or `None` |
| `memory` | Relay's memory for this conversation, or `None` |
| `subagents` | Relay's subagent tools are on |
