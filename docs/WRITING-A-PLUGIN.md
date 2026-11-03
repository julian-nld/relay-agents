# Writing a plugin

Relay's core is complete: since 4.0 it only gets fixes and improvements. Anything new comes as a **plugin**: a folder
that adds to Relay without changing its code. A plugin can add:
- **to the page:** buttons on the activity bar, pages of its own, tabs in Settings;
- **to the server:** API routes, background tasks, and a hook for runs that end;
- **for agents:** tools they call, as they call Relay's own.

This guide builds a complete one step by step: **Notes**, in [`plugins/notes/`](../plugins/notes/). It has a
page for short notes, a `notes_add` tool for agents, a note for each run that fails, and an hourly tidy-up. The code
you see here is quoted from that folder, word for word, and a test checks that it stays so.

**The short version:**
1. Make a folder in Relay's plugins folder, `~/.local/share/relay/plugins/<name>/`, with a `plugin.json`
   (section 1).
2. For the server's part: an `__init__.py` with `def setup(plugin)` (section 2).
3. For the page's part: `static/index.js`, an ES module with `export default function setup(relay)` (section 3).
4. Check it with `./relay plugins check <folder>`, then restart Relay: Settings → Plugins lists it, loaded or with
   what's wrong (section 4).

A mistake in a plugin never keeps Relay from starting: Relay leaves that plugin out and says why. The same goes for
one that calls `sys.exit()` or hangs while it loads.

**A plugin runs with Relay's own rights,** in Relay's process, outside any box. It can do anything Relay can, so
only add plugins you trust ([SECURITY.md](../SECURITY.md#plugins)).

---

## 1. The folder

```
~/.local/share/relay/plugins/notes/
├── plugin.json        what it is
├── __init__.py        the server's part (optional)
├── static/
│   ├── index.js       the page's part (optional)
│   └── style.css      its style (optional)
└── README.md
```

**The folder's name is the plugin's name.**
- **What's allowed:** lowercase letters, digits and `_`, starting with a letter.
- **Where it shows:** it's the prefix of its tools (`notes_add`), its API (`/api/plugins/notes/…`) and its pages
  (`/p/notes/…`).
- **What's taken:** a name can't begin with one of Relay's own tools' names (`memory`, `computer`, `repair`,
  `agentos`, `search`, …).

**The folder may be a link.** The example is used that way:
`ln -s ~/relay/plugins/notes ~/.local/share/relay/plugins/notes`.
- **Keep it outside the workspace roots,** where agents work. An agent there could change the plugin's code, which
  runs with Relay's rights.
- **Settings → Plugins warns** about a plugin whose folder is in one.

**`plugin.json`:**

<!-- notes/plugin.json -->
```json
{
  "label": "Notes",
  "description": "Short notes, yours and your agents': a page to keep them, and a tool agents add them with.",
  "version": "1.0",
  "api": 1
}
```

| Field | What it is |
|---|---|
| `label` | its name on the page (default: the folder's name) |
| `description` | one line for Settings → Plugins |
| `version` | yours, shown in Settings → Plugins |
| `api` | the plugin API it's written for: `1` (this guide). A Relay with an older API leaves it out and says to update Relay. |

**Where its data goes:** `plugin.data` is a folder of its own, `~/.local/share/relay/plugin-data/<name>/`, and
`plugin.store` a small JSON store in it (section 2). They stay when the plugin's folder is replaced by a newer
version.

---

## 2. The server's part: `setup(plugin)`

Relay imports `__init__.py` when it starts and calls its `setup(plugin)` once. `plugin` is everything the plugin
adds through. That, the page's `relay` (section 3) and Relay's HTTP API are the whole contract: anything else in
Relay's code may change from one version to the next.

**`setup()` only registers.** Its import and `setup()` have 20 seconds; a plugin that takes longer (a network call,
say) is left out. Do slow work in a task or a route.

**Threads:** each plugin has four threads of its own. Its routes, tools, hook and tasks run in them, so they can run
at the same time as each other, but never hold up Relay's own work.
- **Plain functions:** they're `def`, not `async def`.
- **Time limits:** a route has 2 minutes before its caller gets a 504, and a tool has 10 minutes.
- **What runs over a limit** goes on in its thread. For longer work, start a `threading.Thread(daemon=True)` of
  your own, answer at once, and let a route say how far it got.

**Its store:** `plugin.store` keeps what the plugin needs (anything JSON has) under keys.
- **Writing:** each change is written whole and at once, so a crash or a restart never leaves half a file.
- **Updating:** `update()` reads, changes and writes with nothing in between. Threads need no lock of their own.

Notes keeps its notes there:

<!-- notes/__init__.py -->
```python
def setup(plugin):
    store = plugin.store  # (its own: each change written whole, one at a time, whichever thread makes it)

    def add(text, by, conversation=None):
        text = (text or "").strip()
        if not text:
            raise PluginError("A note needs some text")
        if len(text) > LONGEST:
            raise PluginError(f"A note is at most {LONGEST} characters: this one has {len(text)}")
        note = {"id": uuid.uuid4().hex[:8], "text": text, "by": by, "at": time.time(), "conversation": conversation}
        store.update("notes", lambda notes: [*notes, note], [])
        return note
```

| `plugin.store` | |
|---|---|
| `get(key, default=None)` | the value kept under `key` (a copy), or `default` |
| `set(key, value)` | keep `value` under `key` |
| `update(key, change, default=None)` | `change(value)` kept as the new value, nothing in between; the new value |
| `delete(key)` | |
| `keys()` | |

For files of your own (an export, a cache), use `plugin.data`.

### Routes

`plugin.route(path, handler, methods=["GET"])` answers at `/api/plugins/<name>/<path>`.
- **Sign-in:** it needs the same sign-in as the rest of Relay's API: a browser that's signed in, or an app password.
  A POST, PUT, PATCH or DELETE from a browser needs the header `X-Relay-Request: 1`; the page's `relay.api` sends it.
- **Parts:** `{part}`s of the path come in `call.params`.
- **The answer:** the handler gets a `Call` and returns what's answered, as JSON. Anything that isn't JSON (a
  `datetime`, say) is the plugin's mistake: a 500 that names it.
- **An error:** raise `PluginError(message, status)` for an answer like `{"error": message}` with that status
  (400–599).
  - A 401 is sent as 502, since the page takes a 401 for "signed out".
  - Anything else it raises is a 500 that names the plugin, and Settings → Plugins shows it as the plugin's last
    error.

<!-- notes/__init__.py -->
```python
    plugin.route("notes", lambda call: store.get("notes", []))
    plugin.route("notes", lambda call: add((call.json or {}).get("text"), call.user["username"]), methods=["POST"])
    plugin.route("notes", remove_all, methods=["DELETE"])
    plugin.route("notes/{id}", remove, methods=["DELETE"])
```

<!-- notes/__init__.py -->
```python
    def remove(call):
        gone = []

        def without(notes):
            gone.extend(n for n in notes if n["id"] == call.params["id"])
            return [n for n in notes if n["id"] != call.params["id"]]

        store.update("notes", without, [])
        if not gone:
            raise PluginError("No such note", 404)
        return {"removed": call.params["id"]}
```

| `Call` | |
|---|---|
| `method` | `GET`, `POST`, `PUT`, `PATCH` or `DELETE` (a `HEAD` comes as `GET`) |
| `path` | the path after `/api/plugins/<name>/` |
| `params` | the route's `{part}`s |
| `query` | the query string's parameters (a key given twice: its last value) |
| `json` | the body (POST, PUT, PATCH), or `None` |
| `user` | who asked: `{id, username}` |

### Tools for agents

`plugin.tool(name, description, schema, handler)` gives agents a tool called `<plugin>_<name>`, of at most 48
characters.
- **Who gets it:** the agents of every chat and code task, not subagents. With `governor=True`, AgentOS's Governor
  gets it too. Chat APIs' own tool loop (providers of the kind `openai-compatible`) doesn't get it.
  - **Only some conversations:** `only=` a function that takes a conversation's id and says whether its agent gets
    the tool, for a plugin's own conversations, say. Other agents don't see it, and a call from one is refused.
    Relay asks it each time an agent's tools are listed, so keep it quick (no network); one that fails counts as no.
- **How they call it:**
  - agents that take MCP tools (Claude Code) have it as one of Relay's;
  - the others call `relay plugin <plugin>_<name> KEY=VALUE …`;
  - `relay help plugins` tells them what each tool does and takes.
- **What it says:** the description is all an agent goes by, so say what it's for and when to use it.
- **Its arguments:** `schema` is their JSON Schema, an object with a `properties` object.
  - **Before the handler runs,** Relay checks the arguments against it: what's `required` is there, and simple types
    are made so. An agent's `text=2024` arrives as the string `"2024"`, and `count="3"` as the number 3.
  - **What can't be made so** is said to the agent, and the handler isn't called.
- **The answer:** the handler gets a `ToolCall` and answers with text. Anything else is sent as JSON.
- **An error:** a `PluginError` is an error to the agent, and an agent's shell sees it as a failed command. For a
  polite "no" (something turned off, say), answer with text instead.
- **A tool runs on this machine, with Relay's rights,** and its arguments come from an agent, which a web page could
  have talked into anything. Treat them as untrusted. Before a path from an agent is opened, `call.within(path)`
  makes sure it's in the conversation's folder.

<!-- notes/__init__.py -->
```python
    def noted(call):
        note = add(call.arguments["text"], "an agent", call.conversation_id)
        return f"Noted ({note['id']}): the user sees it on their Notes page."

    plugin.tool("add", "Add a short note to the user's Notes: a reminder, or a finding worth keeping.",
                {"type": "object", "properties": {"text": {"type": "string", "description": "the note"}},
                 "required": ["text"]}, noted)
```

| `ToolCall` | |
|---|---|
| `conversation_id` | the agent's conversation |
| `run_id` | the run (the agent's turn) that called it |
| `user_id` | whose it is |
| `directory` | its folder, on this machine |
| `arguments` | what the agent gave, checked against the schema |
| `within(path)` | `path` (relative to the folder, or absolute) as a path in the folder; `PluginError` when it's anywhere else |

### Runs that end

`plugin.on_run_ended(hook)` calls `hook(run)` when an agent's run ends, subagents' too.

| `run` | |
|---|---|
| `conversation_id`, `run_id` | the conversation and the run |
| `user_id` | whose it is |
| `provider_id`, `model` | the agent |
| `status` | `succeeded`, `failed` or `interrupted` |
| `error` | why it failed (the last error it said), or `None` |
| `subagent` | a subagent's run |
| `temporary` | a temporary chat |
| `started_by` | who started the conversation outside Relay's pages, in words, or `None` for Relay's pages (e.g. "the Python SDK, with the app password “plugin digest”": a plugin's own chats say so) |

<!-- notes/__init__.py -->
```python
    def ended(run):
        if run["status"] == "failed" and not run["subagent"] and not run["started_by"]:
            why = (run["error"] or "no reason given")[:1000]  # (a long one cut short)
            add(f"A run of {run['provider_id']} failed: {why}", "Relay", run["conversation_id"])

    plugin.on_run_ended(ended)
```

A plugin that starts chats of its own and listens to runs that end skips its own with `started_by`, or it hears
itself.

### Background tasks

`plugin.every(seconds, task)` runs `task()` in the background.
- **When:** about 20 seconds after Relay starts, then every `seconds` (at least 60) from the start of the run
  before.
- **Never twice at once:** a run that's still going when the next one is due is waited for.
- **Errors:** an error is printed and kept as the plugin's last error; the next run goes on as planned.

<!-- notes/__init__.py -->
```python
    def tidy():
        cutoff = time.time() - KEEP_DAYS * 86400
        store.update("notes", lambda notes: [n for n in notes if n["at"] >= cutoff], [])

    plugin.every(3600, tidy)
```

**Once a day,** at a time of day: an hourly task that keeps the day it last ran. It also runs after a restart, so
make it safe to run again:

```python
def daily():
    today = time.strftime("%Y-%m-%d")
    if time.localtime().tm_hour >= 7 and plugin.store.get("made") != today:
        make_the_digest()
        plugin.store.set("made", today)

plugin.every(3600, daily)
```

### Relay's own API

`plugin.sdk` is a session of the [Relay SDK](GUIDE.md#python-sdk) for Relay's own API: chats and code tasks,
scripts, scheduled tasks, notifications.
- **Its app password:** it's signed in with one of the plugin's own, "plugin <name>" in Settings → Account. When the
  user revokes it, the next use makes a new one.
- **`import relay_sdk`** works anywhere in the plugin's code.
- **Pass it as `user=`.**
- **It's synchronous:** use it in a route, a tool, the hook or a task, never in `setup()` itself.

```python
import relay_sdk

def ended(run):
    if run["status"] == "failed":
        relay_sdk.notify("A run failed", url=f"/c/{run['conversation_id']}", user=plugin.sdk)
```

- **A link in a notification** may be one of Relay's own pages (`/c/<id>`, a plugin's `/p/<name>`). Relay puts its
  own address in front (Settings → General). Without one, the notification goes without a link.
- **What a plugin uses most:**
  - **Conversations:** `relay_sdk.convo.list(user=…)`, newest first. Each is a dict with `id`, `title`,
    `created`, `updated` (seconds since 1970), `mode`, `provider_id`, `model`, `temporary` and `external`.
  - **One conversation:** `relay_sdk.conversation(id, user=…).get()`, with its `messages`, each a dict with
    `role`, `content`, `created` and `run_id`.
  - **A short chat:** `relay_sdk.convo.create(user=…, provider=…, model=…, temporary=True).send(prompt).text`.
    Then `delete()` it.
  - **A notification:** `relay_sdk.notify(text, title=, url=, user=…)`.
- **[The SDK's reference](GUIDE.md#python-sdk)** has the rest.

### A web app of its own

A plugin that runs a server of its own on this machine (a web app in a container, say) can put it at Relay's
address, behind Relay's sign-in: `plugin.forward(path, port)`.
- **What it forwards:** Relay's `/<path>` and `/<path>/…`, HTTP and WebSockets, to `127.0.0.1:<port>`, the path as
  it is (the app serves under `/<path>` itself).
- **The way in:** only for someone signed in to Relay, as `/port/<n>/` is. Relay's session cookie isn't passed on.
- **`path`** is one segment of lowercase letters, digits, `-` and `_` that no page of Relay's has, and no other
  plugin forwards. **`port`** is a number, or a function that says it when asked (a port the user can change).
- **The app shares Relay's origin:** its page can be framed in the plugin's own (`<iframe src="/<path>/…">`), and
  its scripts can use Relay's API as the user. Only forward to apps you trust as much as the plugin.

### The reference

| `plugin` | |
|---|---|
| `name` | the folder's name |
| `label` | its label (`plugin.json`) |
| `version` | its version (`plugin.json`) |
| `folder` | its folder |
| `data` | a folder for what it keeps (made when first asked) |
| `store` | its own small JSON store, in `data` |
| `sdk` | a Relay SDK session for Relay's own API |
| `route(path, handler, methods)` | an API route |
| `tool(name, description, schema, handler, governor, only)` | a tool for agents (`only`: for some conversations) |
| `on_run_ended(hook)` | a hook for runs that end |
| `every(seconds, task)` | a background task |
| `forward(path, port)` | Relay's `/<path>/…` to a web app of its own on this machine (signed in) |

`PluginError`, `Call` and `ToolCall` come from `relay.plugins`.

---

## 3. The page's part: `setup(relay)`

When Relay's page loads, it imports each plugin's `static/index.js` and calls its default export with `relay`: what
the plugin may add to the page.
- **`setup(relay)` has 10 seconds.** Plugins load side by side, and one that fails or takes longer is left out.
- **`static/style.css`** is added to the page too.
- **Serving:** both are served from `/plugins/<name>/static/` to a signed-in page.
- **Changes show** at the next reload of the page, without a restart.

**Relay's look:**
- **Its CSS variables:** `--bg`, `--bg-elevated`, `--bg-hover`, `--bg-active`, `--bg-input`, `--bg-code`, `--fg`,
  `--fg-muted`, `--fg-faint`, `--border`, `--border-strong`, `--accent`, `--accent-fg`, `--success`, `--warning`,
  `--danger`, `--radius`, `--radius-sm`, `--shadow`, `--shadow-lg`. With these, a plugin looks right in the light and
  the dark theme.
- **Its classes:**
  - `btn`, with `primary`, `small`, `ghost` and `danger`;
  - `icon-btn`, with `sm`;
  - `badge`, `muted`, `lead` and `error-text`;
  - `md` for Markdown (`relay.ui.markdown` sets it).
- **The page's rules (its CSP):**
  - scripts only from Relay;
  - no inline `style="…"`, though `el.style.width = …` works;
  - requests only to Relay. Reach another site through a route of the plugin's own.
  - images from Relay or `data:`.

Notes puts a button on the activity bar, with how many notes there are:

<!-- notes/static/index.js -->
```js
export default function setup(relay) {
  const { h, icon, clear, toast, timeAgo, confirmDialog } = relay.ui;
  const button = relay.barItem({ title: "Notes", icon: "book", onClick: () => relay.navigate("") });

  async function notes() {
    const all = await relay.api.get("notes");
    button.setBadge(all.length);
    return all;
  }
```

**Its page,** at `/p/notes`: `relay.page(path, render)` draws `render(el, {params, query})` into the main area,
where a chat would be. Like the backend's, `{part}`s of the path come in `params`. What `render` returns, if it's a
function, is called when another page opens:

<!-- notes/static/index.js -->
```js
    clear(el, h("div.notes", h("h1", "Notes"), h("div.notes-add", text, add), list));
    await draw();
    const timer = setInterval(draw, 30000);
    return () => clearInterval(timer);  // (another page opened)
```

**Nothing is pushed to the page.** A badge or a page that should follow changes asks again now and then, as Notes
does every 30 seconds.

**Its tab in Settings,** after Relay's own:

<!-- notes/static/index.js -->
```js
  relay.settingsTab({ label: "Notes", icon: "book", render: async (pane) => {
```

| `relay` | |
|---|---|
| `name`, `label` | the plugin's |
| `api` | `get`, `post`, `put`, `patch` and `del` of its own routes (`relay.api.get("notes")`); `api.relay`: Relay's whole API (`relay.api.relay.get("/api/conversations")`) |
| `ui` | Relay's pieces (below) |
| `url(path)` | a file of its `static/` folder |
| `navigate(path)` | one of its pages (`""` its first, `"notes/12"`), or Relay's own (`"/c/<id>"`, `"/tasks"`) |
| `barItem({title, icon, onClick})` | a button on the activity bar, above the Shell; it gives `{el, setBadge(n)}` |
| `page(path, render)` | a page at `/p/<name>/<path>` |
| `settingsTab({label, icon, render})` | a tab in Settings: `render(pane)` draws it |

| `relay.ui` | |
|---|---|
| `h(spec, attributes, …children)` | an element: `h("div.notes", {onclick: …}, "text", child)`; the attributes are optional |
| `icon(name, cls)` | an icon (below), `cls` `"sm"` for a small one |
| `clear(el, …children)` | `el` emptied, and the children put in |
| `modal(title, {wide})` | a dialog: `{el, body, foot, close()}` |
| `toast(message, kind)` | a message for a moment; `kind` `"error"` for a red one |
| `confirmDialog(title, message, {confirm, danger})` | `true` when confirmed |
| `promptDialog(title, {label, value, placeholder, confirm})` | the text typed, or `null` |
| `timeAgo(seconds)`, `clock(seconds)` | "5 minutes ago", "14:05", for a time in seconds since 1970 |
| `markdown(text)` | Markdown as Relay shows it: an element, sanitised, its code highlighted |

**Icons:** one of Relay's by its name, or an SVG's paths on a 24×24 grid of your own (`'<path d="…"/>'`). Relay's:
`plus`, `list`, `bell`, `paperclip`, `search`, `menu`, `sidebar`, `settings`, `logout`, `send`, `stop`, `play`,
`copy`, `check`, `x`, `chevronDown`, `chevronRight`, `folder`, `folderPlus`, `terminal`, `file`, `undo`, `fork`,
`edit`, `trash`, `pin`, `more`, `sun`, `moon`, `monitor`, `code`, `chat`, `temporary`, `alert`, `refresh`,
`arrowLeft`, `plug`, `puzzle`, `user`, `info`, `tool`, `globe`, `shield`, `brain`, `hammer`, `map`, `bolt`, `bulb`,
`flask`, `book`, `bug`, `rocket`, `star`, `eye`, `target`, `clock`, `box`, `compass`, `bot`, `fileCode`, `sparkles`,
`reply`.

**When its part fails,** it's said in the browser's console and in Settings → Plugins, and the rest of the page goes
on.

---

## 4. Checking, installing, and when something's wrong

1. **Check it:** `./relay plugins check <folder>`, in Relay's folder, loads it as Relay would and lists its routes,
   tools, tasks, hook and page. A mistake is said with its file and line.
   - **It runs the plugin's `setup()`,** as Relay will.
   - **Its code outside Relay:** `from relay.plugins import Plugins` with `PYTHONPATH=~/relay/src:~/relay/vendor`.
     `Plugins(folder=…).check(path)` gives what `relay plugins check` prints; call a route's or a tool's handler
     with a `Call` or a `ToolCall` of your own.
2. **Install it:** put the folder in `~/.local/share/relay/plugins/` (or a link to it, outside the workspace roots).
3. **Restart Relay:** Settings → Plugins → **Restart Relay**, or `systemctl --user restart relay`.
4. **Settings → Plugins** lists every plugin:
   - its label, version, description, folder and tools;
   - **loaded**, **off**, or **can't be loaded** with why (the file and line of the mistake);
   - what failed last: a route, a task, the hook, a tool, or the page's part.
5. **Its switch** turns it off at once: its routes, tools, tasks and hook stop, and its page's part goes at the next
   reload. On again, it's back at once if Relay loaded it at its start, or else from the next start.

**From the command line:**
- `./relay plugins` lists them without running their code. So does `./relay doctor`.
- `./relay plugins off <name>` turns one off, also when Relay doesn't start or answer.

**What a plugin can't do** (in plugin API 1):
- add a provider (that's [a provider folder](ADDING-A-PROVIDER.md));
- change what Relay's own screens show, or replace Relay's own tools;
- answer without sign-in: a webhook from another site has no route to reach;
- push to the page.
