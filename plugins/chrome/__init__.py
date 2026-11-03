"""Chrome: Relay's agents in the user's own Chrome, through Relay's Chrome extension. They read the pages the user is
signed in to, click, type, open pages and take screenshots there (the tools chrome_tabs, chrome_read, …), each step
allowed by the user (on a card in the extension's side panel, or a window its notification opens).

A tool's call becomes a command. The extension, while Chrome runs, asks for the commands (`commands`, held open until
there's one), shows them to the user, carries out the ones they allow in the browser, and answers each
(`commands/<id>/result`). The tool waits for that answer and gives it to the agent.

The extension itself is extension/: this plugin zips it for the page (Download, in its tab in Settings), to unzip and
load in Chrome (Load unpacked)."""

import base64
import hashlib
import io
import json
import threading
import time
import uuid
import zipfile
from pathlib import Path

from relay.plugins import PluginError

WAIT = 300          # seconds a tool waits for the user to allow it and the browser to answer
POLL = 20           # seconds the extension's ask for commands is held open when there's nothing new
GONE = 45           # seconds without the extension asking, after which its Chrome counts as gone
AT_ONCE = 2         # commands that wait at once (each holds one of the plugin's four threads; the extension needs one)
SHOTS = 10          # screenshots kept in a conversation's folder
FOLDER = ".relay-chrome"
LARGEST_SHOT = 12 * 1024 * 1024  # bytes of a screenshot, at most

NOT_CONNECTED = ("The user's Chrome isn't connected: these tools need Chrome running with Relay's Chrome extension "
                 "signed in (and its “agents may use this browser” setting on). Ask the user to start Chrome (or open "
                 "the extension's side panel), then try again.")

EXTENSION = Path(__file__).resolve().parent / "extension"
ZIPPED = "relay-chrome"  # (the folder in the zip: unzipped over the last one, the extension is reloaded from it)
STAMP = (1980, 1, 1, 0, 0, 0)  # (each file's time in the zip: the same files, the same bytes)

TAB = {"type": "integer", "description": "the tab's id (chrome_tabs lists them); without it, the tab you used last, "
                                         "else the one the user has in front of them"}


def manifest():
    return json.loads((EXTENSION / "manifest.json").read_text())


def files():
    """The extension's files, as they go in the zip (no hidden ones)."""
    return sorted(p for p in EXTENSION.rglob("*")
                  if p.is_file() and not any(part.startswith(".") for part in p.relative_to(EXTENSION).parts))


def pack():
    """The zip: the extension's files in a folder of their own."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zipped:
        for path in files():
            name = f"{ZIPPED}/{path.relative_to(EXTENSION).as_posix()}"
            zipped.writestr(zipfile.ZipInfo(name, STAMP), path.read_bytes(), zipfile.ZIP_DEFLATED)
    return buffer.getvalue()


def setup(plugin):
    changed = threading.Condition()
    commands = {}  # id: the command, while its tool waits
    state = {"seq": 0}
    seen = {}      # user: when the extension last asked for commands
    versions = {}  # user: a number that goes up with each change to their commands

    def user_of(call):
        return str(call.user["id"])

    def bump(user):
        versions[user] = versions.get(user, 0) + 1
        changed.notify_all()

    def pending(user):
        return [{k: c[k] for k in ("id", "action", "args", "conversation_id", "run_id", "created", "claimed")}
                for c in sorted(commands.values(), key=lambda c: c["seq"])
                if c["user"] == user and c["state"] == "pending"]

    # The tools' common part: the command, and the extension's answer to it.
    def ask(call, action, args):
        user = str(call.user_id)
        with changed:
            if time.time() - seen.get(user, 0) > GONE:
                return NOT_CONNECTED
            if sum(c["state"] == "pending" for c in commands.values()) >= AT_ONCE:
                return (f"Chrome is busy with {AT_ONCE} other steps (this chat's or another's): wait for them, and use "
                        f"these tools one at a time.")
            state["seq"] += 1
            command = {"id": uuid.uuid4().hex[:12], "seq": state["seq"], "user": user, "action": action,
                       "args": args, "conversation_id": call.conversation_id, "run_id": call.run_id,
                       "created": time.time(), "claimed": None, "state": "pending", "result": None}
            commands[command["id"]] = command
            bump(user)
            deadline = time.time() + WAIT
            try:
                while command["state"] == "pending":
                    now = time.time()
                    if now >= deadline:
                        command["state"] = "timeout"
                    elif now - seen.get(user, 0) > GONE:
                        command["state"] = "closed"
                    else:
                        changed.wait(min(deadline - now, 5))
            finally:
                commands.pop(command["id"], None)
                if command["state"] != "done":
                    bump(user)
        if command["state"] == "timeout":
            return "The user didn't answer in Chrome within 5 minutes: nothing was done."
        if command["state"] == "closed":
            return NOT_CONNECTED
        if command["state"] == "cancelled":
            return "Cancelled: the run ended."
        return answer(call, action, command["result"] or {})

    def answer(call, action, result):
        if result.get("denied"):
            why = f" They said: {result['reason']}" if result.get("reason") else ""
            return f"The user didn't allow that in Chrome: nothing was done.{why}"
        if not result.get("ok"):
            raise PluginError(str(result.get("error") or "It didn't work in Chrome"))
        text = str(result.get("text") or "Done.")
        if action == "screenshot":
            return f"{save_shot(call, result)}\n{text}"
        return text

    def save_shot(call, result):
        try:
            data = base64.b64decode(result.get("image") or "", validate=True)
        except ValueError:
            raise PluginError("Chrome's screenshot didn't come through") from None
        if not data or len(data) > LARGEST_SHOT:
            raise PluginError("Chrome's screenshot didn't come through (empty, or too large)")
        if not call.directory:
            raise PluginError("This conversation has no folder to keep a screenshot in")
        folder = Path(call.within(FOLDER))
        folder.mkdir(parents=True, exist_ok=True)
        kind = "png" if result.get("mime") == "image/png" else "jpg"
        name = f"screenshot-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}.{kind}"
        (folder / name).write_bytes(data)
        for old in sorted(folder.glob("screenshot-*"))[:-SHOTS]:  # (the last few are kept)
            old.unlink(missing_ok=True)
        return (f"The screenshot is {FOLDER}/{name} in your working folder: open that file (your Read tool, say) "
                f"to see it.")

    # The extension's routes, at /api/plugins/chrome/…
    def status(call):
        with changed:
            last = seen.get(user_of(call), 0)
        return {"plugin": plugin.name, "version": plugin.version, "connected": time.time() - last <= GONE}

    def poll(call):
        """The user's commands waiting for an answer, once there's a change since `since` (or after `wait` seconds)."""
        user = user_of(call)
        try:
            since = int(call.query.get("since") or 0)
            wait = min(max(float(call.query.get("wait") or 0), 0), POLL)
        except ValueError:
            raise PluginError("since and wait are numbers") from None
        with changed:
            seen[user] = time.time()
            changed.wait_for(lambda: versions.get(user, 0) != since, timeout=wait)
            seen[user] = time.time()
            return {"version": versions.get(user, 0), "commands": pending(user)}

    def mine(call):
        command = commands.get(call.params["id"])
        if not command or command["user"] != user_of(call) or command["state"] != "pending":
            raise PluginError("That command isn't waiting any more (it was answered, or the agent stopped waiting)",
                              404)
        return command

    def claim(call):
        """One panel takes a command (another window's panel then leaves it)."""
        client = str((call.json or {}).get("client") or "")
        if not client:
            raise PluginError("Which panel claims it: client")
        with changed:
            command = mine(call)
            if command["claimed"] not in (None, client):
                raise PluginError("Another of your Chrome windows took it", 409)
            command["claimed"] = client
            bump(command["user"])
        return {"claimed": command["id"]}

    def result(call):
        data = call.json
        if not isinstance(data, dict):
            raise PluginError("The result is a JSON object")
        with changed:
            command = mine(call)
            client = str(data.get("client") or "")
            if command["claimed"] not in (None, client):
                raise PluginError("Another of your Chrome windows took it", 409)
            command["result"] = data
            command["state"] = "done"
            bump(command["user"])
        return {"answered": command["id"]}

    built = {}  # (the zip of the files as they are: made again when one changes)

    def zipped():
        key = hashlib.sha256(b"".join(p.as_posix().encode() + p.read_bytes() for p in files())).hexdigest()
        if built.get("key") != key:
            built.update(key=key, data=pack())
        return built["data"]

    def version(call):
        return {"version": manifest()["version"]}

    def extension(call):
        found = manifest()
        return {"version": found["version"], "name": f"{ZIPPED}-{found['version']}.zip",
                "data": base64.b64encode(zipped()).decode()}

    plugin.route("version", version)
    plugin.route("extension", extension)
    plugin.route("status", status)
    plugin.route("commands", poll)
    plugin.route("commands/{id}/claim", claim, methods=["POST"])
    plugin.route("commands/{id}/result", result, methods=["POST"])

    # A run that ends (stopped, say) takes its commands with it.
    def ended(run):
        with changed:
            for command in commands.values():
                if command["run_id"] == run["run_id"] and command["state"] == "pending":
                    command["state"] = "cancelled"
                    bump(command["user"])

    plugin.on_run_ended(ended)

    # The tools.
    def tool(name, description, properties, required, args):
        schema = {"type": "object", "properties": properties, "required": required}
        plugin.tool(name, description, schema, lambda call: ask(call, name, args(call.arguments)))

    def tab(arguments):
        return {"tab": arguments["tab"]} if arguments.get("tab") is not None else {}

    def number(arguments, key, least=0):
        value = arguments.get(key)
        if value is None:
            return None
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if not isinstance(value, int) or isinstance(value, bool) or value < least:
            raise PluginError(f"{key} is a whole number, at least {least}")
        return value

    def text(arguments, key, longest):
        value = str(arguments.get(key) or "")
        if len(value) > longest:
            raise PluginError(f"{key} is at most {longest} characters")
        return value

    reading = ("Use it for what needs the user's own sign-in (their mail, a web shop's messages, internal tools) or "
               "what they have open; for public pages your own web fetch is quicker. The user allows it in Relay's "
               "Chrome extension.")

    tool("tabs", "List the tabs open in the user's own Chrome: each tab's id, title and address, and which ones they "
         "have in front of them. Use it to find a page they have open, then read it with chrome_read. " + reading,
         {}, [], lambda a: {})
    tool("read", "Read a page in the user's own Chrome, where they're signed in: its text, with what you can use "
         "marked by a number, like [12 button “Send”], [13 textbox “Message”] or [14 link “Inbox” → /messages]. "
         "Give those numbers to chrome_click and chrome_type. A long page comes in parts: `offset` for the next. "
         + reading,
         {"tab": TAB,
          "offset": {"type": "integer", "description": "where to go on in a long page (the last part says)"}},
         [], lambda a: {**tab(a), "offset": number(a, "offset") or 0})
    tool("open", "Open an address in the user's own Chrome, in a new tab (or in `tab`), wait for it to load, and read "
         "it as chrome_read does. `url` “back” goes back a page in the tab. For pages that need the user's sign-in; "
         "the user allows it in Relay's Chrome extension.",
         {"url": {"type": "string", "description": "an http(s) address, or “back”"}, "tab": TAB},
         ["url"], lambda a: {**tab(a), "url": text(a, "url", 4000)})
    tool("click", "Click a link, button, checkbox or other element of a page in the user's own Chrome, by its number "
         "from chrome_read. Says where the page is after it: read it again to see what changed. The user is asked "
         "to allow it in Relay's Chrome extension (unless they always allow it on that site).",
         {"ref": {"type": "integer", "description": "the element's number from chrome_read"}, "tab": TAB},
         ["ref"], lambda a: {**tab(a), "ref": number(a, "ref", 1)})
    tool("type", "Fill in a field of a page in the user's own Chrome, by its number from chrome_read: its text is "
         "replaced by `text`, and a list (select) is set to the option with that text. With `submit` it then "
         "presses Enter, which sends a form or a chat message; without it nothing is sent. The user is asked to "
         "allow it in Relay's Chrome extension (unless they always allow it on that site).",
         {"ref": {"type": "integer", "description": "the field's number from chrome_read"},
          "text": {"type": "string", "description": "what goes in it"},
          "submit": {"type": "boolean", "description": "press Enter after it (send)"}, "tab": TAB},
         ["ref", "text"], lambda a: {**tab(a), "ref": number(a, "ref", 1), "text": text(a, "text", 20000),
                                     "submit": bool(a.get("submit"))})
    tool("scroll", "Scroll a page in the user's own Chrome: up, down, to its top or bottom (the part under the middle "
         "of the window: a chat's messages, say), to load more of a list or chat that loads as you scroll; or `ref` "
         "brings that element into view. Read it again after.",
         {"direction": {"type": "string", "enum": ["up", "down", "top", "bottom"]},
          "ref": {"type": "integer", "description": "an element's number from chrome_read, to bring into view"},
          "tab": TAB},
         [], lambda a: {**tab(a), "direction": a.get("direction") or "down", "ref": number(a, "ref", 1)})
    tool("screenshot", "Take a screenshot of a tab in the user's own Chrome (the tab is brought to the front): it's "
         "saved as an image file in your working folder, which you open to see it. For what chrome_read's text "
         "doesn't show: the layout, images, charts. The user allows it in Relay's Chrome extension.",
         {"tab": TAB}, [], tab)
