"""VSCodium: Relay in VSCodium (or VS Code), as an extension of its own (extension/).
- **What it has:** your conversations in its side bar, a chat with Relay's agents in an editor tab, and a code task's
  changes in its diff editor.
- **How it talks to Relay:** through Relay's HTTP API, with an app password.
- **Where it runs:** with Remote - SSH it runs on Relay's own machine, where your folders are Relay's. A conversation
  in one of them is a code task there.

This plugin packs the extension as a .vsix itself, from extension/ (no npm), and gives it to the page (Download). It
also installs it into the VSCodium and VS Code servers that Remote - SSH put on this machine (Install). Its tab in
Settings (static/index.js) has the steps.

**Agents use VSCodium** (vscodium_definition, vscodium_references, …): a tool's call becomes a question for one of
the user's VSCodium windows, the one with the conversation's folder open. Each window, while it runs, says which
folders it has (`windows`) and asks for its questions (`tools`, held open until there's one); its language servers
answer them (`tools/<id>/result`), and the tool gives that answer to the agent, as plugins/chrome does for Chrome."""

import base64
import hashlib
import io
import json
import os
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from relay.plugins import PluginError

EXTENSION = Path(__file__).resolve().parent / "extension"
# The servers Remote - SSH leaves in the home folder: VSCodium's (Open Remote - SSH), then VS Code's (two layouts)
SERVERS = (("VSCodium", ".vscodium-server/bin/*/bin/codium-server"),
           ("VS Code", ".vscode-server/cli/servers/*/server/bin/code-server"),
           ("VS Code", ".vscode-server/bin/*/bin/code-server"))
INSTALLING = 120  # seconds one server may take to install it
STAMP = (1980, 1, 1, 0, 0, 0)  # (each file's time in the .vsix: the same files, the same bytes)
WAIT = 60     # seconds a tool waits for VSCodium's answer (a language server that's still starting takes a while)
POLL = 20     # seconds a window's ask for questions is held open when there's nothing new
GONE = 45     # seconds without a window asking, after which it counts as closed
AT_ONCE = 2   # questions that wait at once (each holds one of the plugin's four threads; each window's ask holds one)
NOT_CONNECTED = ("VSCodium isn't connected: these tools need VSCodium open on the folder, with Relay's VSCodium "
                 "extension signed in. Ask the user to open it there, or do without (read the files yourself).")
TYPES = {".json": "application/json", ".js": "application/javascript", ".css": "text/css", ".md": "text/markdown",
         ".svg": "image/svg+xml", ".png": "image/png", ".txt": "text/plain", ".vsixmanifest": "text/xml"}


def package():
    return json.loads((EXTENSION / "package.json").read_text())


def files():
    """The extension's files, as they go in the .vsix (no hidden ones)."""
    return sorted(p for p in EXTENSION.rglob("*") if p.is_file()
                  and not any(part.startswith(".") or part == "__pycache__" for part in p.relative_to(EXTENSION).parts))


def manifest(found):
    """extension.vsixmanifest, as vsce writes it: what VSCodium reads before it opens package.json."""
    def text(value):
        return escape(str(value or ""))

    kind = ",".join(found.get("extensionKind") or ["workspace"])
    icon = f"extension/{found['icon']}" if found.get("icon") else None
    return f"""<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011" \
xmlns:d="http://schemas.microsoft.com/developer/vsx-schema-design/2011">
  <Metadata>
    <Identity Language="en-US" Id="{text(found['name'])}" Version="{text(found['version'])}" \
Publisher="{text(found['publisher'])}" />
    <DisplayName>{text(found.get('displayName'))}</DisplayName>
    <Description xml:space="preserve">{text(found.get('description'))}</Description>
    <Tags></Tags>
    <Categories>{text(','.join(found.get('categories') or ['Other']))}</Categories>
    <GalleryFlags>Public</GalleryFlags>
    <Properties>
      <Property Id="Microsoft.VisualStudio.Code.Engine" Value="{text(found['engines']['vscode'])}" />
      <Property Id="Microsoft.VisualStudio.Code.ExtensionDependencies" Value="" />
      <Property Id="Microsoft.VisualStudio.Code.ExtensionPack" Value="" />
      <Property Id="Microsoft.VisualStudio.Code.ExtensionKind" Value="{text(kind)}" />
      <Property Id="Microsoft.VisualStudio.Code.LocalizedLanguages" Value="" />
      <Property Id="Microsoft.VisualStudio.Services.GitHubFlavoredMarkdown" Value="true" />
    </Properties>{f'''
    <Icon>{text(icon)}</Icon>''' if icon else ''}
  </Metadata>
  <Installation>
    <InstallationTarget Id="Microsoft.VisualStudio.Code" />
  </Installation>
  <Dependencies />
  <Assets>
    <Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true" />
    <Asset Type="Microsoft.VisualStudio.Services.Content.Details" Path="extension/README.md" Addressable="true" />{f'''
    <Asset Type="Microsoft.VisualStudio.Services.Icons.Default" Path="{text(icon)}" '''
'''Addressable="true" />''' if icon else ''}
  </Assets>
</PackageManifest>
"""


def content_types(paths):
    kinds = sorted({p.suffix.lower() for p in paths if p.suffix} | {".vsixmanifest"})
    rows = "".join(f'<Default Extension="{escape(k)}" ContentType="{TYPES.get(k, "application/octet-stream")}"/>'
                   for k in kinds)
    return ('<?xml version="1.0" encoding="utf-8"?>\n'
            f'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">{rows}</Types>\n')


def pack():
    """The .vsix: a zip of the extension's files under extension/, with the manifest and content types vsce adds."""
    found, paths = package(), files()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as vsix:
        def put(name, data):
            vsix.writestr(zipfile.ZipInfo(name, STAMP), data, zipfile.ZIP_DEFLATED)

        put("extension.vsixmanifest", manifest(found))
        put("[Content_Types].xml", content_types(paths))
        for path in paths:
            put("extension/" + path.relative_to(EXTENSION).as_posix(), path.read_bytes())
    return buffer.getvalue()


def servers(home=None):
    """The newest server of each product on this machine: [(its name, its program)]."""
    home = Path(home or os.path.expanduser("~"))
    found = {}
    for product, pattern in SERVERS:
        for program in home.glob(pattern):
            if os.access(program, os.X_OK) and (product not in found
                                                or program.stat().st_mtime > found[product].stat().st_mtime):
                found[product] = program
    return list(found.items())


def setup(plugin):
    built = {}  # (the .vsix of the files as they are: made again when one changes)

    def vsix():
        key = hashlib.sha256(b"".join(p.as_posix().encode() + p.read_bytes() for p in files())).hexdigest()
        if built.get("key") != key:
            built.update(key=key, data=pack())
        return built["data"]

    def version(call):
        return {"version": package()["version"]}

    def extension(call):
        found = package()
        return {"version": found["version"], "name": f"{found['name']}-{found['version']}.vsix",
                "data": base64.b64encode(vsix()).decode()}

    def install(call):
        places = servers()
        if not places:
            raise PluginError("There's no VSCodium or VS Code server on this machine yet: connect to it once with "
                              "Remote - SSH (in VSCodium: Open Remote - SSH), then install. Or download the .vsix and "
                              "use Install from VSIX… in VSCodium's Extensions view.", 409)
        done = []
        with tempfile.TemporaryDirectory(prefix="relay-vscodium-") as folder:
            path = Path(folder) / f"{package()['name']}.vsix"
            path.write_bytes(vsix())
            for product, program in places:
                try:
                    answer = subprocess.run([str(program), "--install-extension", str(path), "--force"],
                                            capture_output=True, text=True, timeout=INSTALLING)
                    said = (answer.stdout + answer.stderr).strip().splitlines()
                    ok = answer.returncode == 0
                except (OSError, subprocess.TimeoutExpired) as exc:
                    said, ok = [str(exc) or type(exc).__name__], False
                done.append({"product": product, "ok": ok, "said": said[-1] if said else ""})
        if not any(d["ok"] for d in done):
            raise PluginError("; ".join(f"{d['product']}: {d['said'] or 'it failed'}" for d in done), 502)
        return {"installed": done, "version": package()["version"]}

    plugin.route("version", version)
    plugin.route("extension", extension)
    plugin.route("install", install, methods=["POST"])

    tools(plugin)


def near(a, b):
    """Whether one of two folders is in the other (a task's worktree in the open folder, or the other way round)."""
    if not a or not b:
        return False
    a, b = a.rstrip("/") + "/", b.rstrip("/") + "/"
    return a.startswith(b) or b.startswith(a)


def tools(plugin):
    """The agents' VSCodium tools, each a question for one of the user's VSCodium windows."""
    changed = threading.Condition()
    questions = {}  # id: the question, while its tool waits
    windows = {}    # client: {user, folders, tasks, focused, seen}
    versions = {}   # client: a number that goes up with each change to its questions
    state = {"seq": 0}

    def user_of(call):
        return str(call.user["id"])

    def bump(client):
        versions[client] = versions.get(client, 0) + 1
        changed.notify_all()

    def alive(user):
        return [(client, w) for client, w in windows.items()
                if w["user"] == user and time.time() - w["seen"] <= GONE]

    def which(call, user):
        """The window that answers: the one with the computer task, else with the conversation's folder, else the one
        the user had in front of them last."""
        found = alive(user)
        for test in (lambda w: call.conversation_id in w["tasks"],
                     lambda w: any(near(call.directory, f) for f in w["folders"])):
            matching = [(client, w) for client, w in found if test(w)]
            if matching:
                found = matching
                break
        return max(found, key=lambda cw: (cw[1]["focused"], cw[1]["seen"]))[0] if found else None

    def ask(call, action, args):
        user = str(call.user_id)
        with changed:
            client = which(call, user)
            if client is None:
                return NOT_CONNECTED
            if sum(q["state"] == "pending" for q in questions.values()) >= AT_ONCE:
                return f"VSCodium is busy with {AT_ONCE} other questions: ask again, one at a time."
            state["seq"] += 1
            question = {"id": uuid.uuid4().hex[:12], "seq": state["seq"], "client": client, "action": action,
                        "args": args, "conversation_id": call.conversation_id, "directory": call.directory,
                        "run_id": call.run_id, "state": "pending", "result": None}
            questions[question["id"]] = question
            bump(client)
            deadline = time.time() + WAIT
            try:
                while question["state"] == "pending":
                    now = time.time()
                    if now >= deadline:
                        question["state"] = "timeout"
                    elif client not in dict(alive(user)):
                        question["state"] = "closed"
                    else:
                        changed.wait(min(deadline - now, 5))
            finally:
                questions.pop(question["id"], None)
                if question["state"] != "done":
                    bump(client)
        if question["state"] == "timeout":
            return f"VSCodium didn't answer within {WAIT} seconds (its language server may still be starting)."
        if question["state"] == "closed":
            return NOT_CONNECTED
        if question["state"] == "cancelled":
            return "Cancelled: the run ended."
        result = question["result"] or {}
        if not result.get("ok"):
            raise PluginError(str(result.get("error") or "VSCodium couldn't answer that"))
        return str(result.get("text") or "(nothing)")

    # The windows' routes, at /api/plugins/vscodium/…
    def window(call):
        """A window says what it has open: its folders, its computer tasks' conversations, whether it's in front."""
        data = call.json if isinstance(call.json, dict) else {}
        client = str(data.get("client") or "")
        if not client:
            raise PluginError("Which window: client")
        listed = lambda key: [str(x) for x in (data.get(key) or []) if isinstance(x, str)][:200]  # noqa: E731
        with changed:
            known = windows.get(client)
            if known and known["user"] != user_of(call):
                raise PluginError("That window is another user's", 409)
            windows[client] = {"user": user_of(call), "folders": listed("folders"), "tasks": listed("tasks"),
                               "focused": time.time() if data.get("focused") else (known or {}).get("focused", 0),
                               "seen": time.time()}
        return {"window": client}

    def poll(call):
        """A window's questions waiting for an answer, once there's a change since `since` (or after `wait` s)."""
        client = str(call.query.get("client") or "")
        try:
            since = int(call.query.get("since") or 0)
            wait = min(max(float(call.query.get("wait") or 0), 0), POLL)
        except ValueError:
            raise PluginError("since and wait are numbers") from None
        with changed:
            known = windows.get(client)
            if not known or known["user"] != user_of(call):
                raise PluginError("Unknown window: say which folders it has first (windows)", 404)
            known["seen"] = time.time()
            changed.wait_for(lambda: versions.get(client, 0) != since, timeout=wait)
            known["seen"] = time.time()
            waiting = [{k: q[k] for k in ("id", "action", "args", "conversation_id", "directory")}
                       for q in sorted(questions.values(), key=lambda q: q["seq"])
                       if q["client"] == client and q["state"] == "pending"]
            return {"version": versions.get(client, 0), "questions": waiting}

    def result(call):
        data = call.json
        if not isinstance(data, dict):
            raise PluginError("The result is a JSON object")
        with changed:
            question = questions.get(call.params["id"])
            if (not question or question["state"] != "pending" or question["client"] != str(data.get("client") or "")
                    or windows.get(question["client"], {}).get("user") != user_of(call)):
                raise PluginError("That question isn't waiting any more (the agent stopped waiting)", 404)
            question["result"] = data
            question["state"] = "done"
            bump(question["client"])
        return {"answered": question["id"]}

    plugin.route("windows", window, methods=["POST"])
    plugin.route("tools", poll)
    plugin.route("tools/{id}/result", result, methods=["POST"])

    def ended(run):
        with changed:
            for question in questions.values():
                if question["run_id"] == run["run_id"] and question["state"] == "pending":
                    question["state"] = "cancelled"
                    bump(question["client"])

    plugin.on_run_ended(ended)

    def text(arguments, key, longest, needed=False):
        value = arguments.get(key)
        if value is None or value == "":
            if needed:
                raise PluginError(f"{key} is needed")
            return None
        if not isinstance(value, str) or len(value) > longest:
            raise PluginError(f"{key} is text, at most {longest} characters")
        return value

    def line(arguments):
        value = arguments.get("line")
        if isinstance(value, str) and value.strip().isdigit():
            value = int(value)
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise PluginError("line is a line's number, from 1")
        return value

    def place(arguments):
        return {"path": text(arguments, "path", 4000, True), "line": line(arguments),
                "symbol": text(arguments, "symbol", 200)}

    def tool(name, description, properties, required, args):
        schema = {"type": "object", "properties": properties, "required": required}
        plugin.tool(name, description, schema, lambda call: ask(call, name, args(call.arguments)))

    where = ("Paths are the folder's (relative to it, or absolute). It needs VSCodium open on the folder with Relay's "
             "extension signed in; it answers from VSCodium's language servers, as the user's editor sees the code.")
    at = {"path": {"type": "string", "description": "the file"},
          "line": {"type": "integer", "description": "the line's number, from 1"},
          "symbol": {"type": "string", "description": "the name on that line it's about (without it, the line's "
                                                      "first name)"}}

    def problems(arguments):
        return {"path": text(arguments, "path", 4000)}

    def symbols(arguments):
        found = {"query": text(arguments, "query", 200), "path": text(arguments, "path", 4000)}
        if not found["query"] and not found["path"]:
            raise PluginError("Give query (names across the folder) or path (one file's outline)")
        return found

    tool("problems", "The errors and warnings VSCodium sees now (its Problems view): in the whole folder, or in one "
         "file (`path`), as path:line:col: error|warning: message (source). Quicker and fuller than a build to find "
         "type errors and lint warnings, and the way to check your change compiles. " + where,
         {"path": {"type": "string", "description": "one file (else the whole folder)"}}, [], problems)
    tool("definition", "Where a name used in a file is defined (go to definition), with its lines: its function, "
         "class, type or variable, also in libraries. " + where, at, ["path", "line"], place)
    tool("references", "Every place a name is used (find all references), as path:line: the line. Before you change "
         "or rename a function, a type or a field, to see what depends on it. " + where, at, ["path", "line"], place)
    tool("symbols", "Names in the code: with `query`, the functions, classes, types and so on across the folder whose "
         "names match it (where each is); with `path`, one file's outline. " + where,
         {"query": {"type": "string", "description": "part of the name to look for"},
          "path": {"type": "string", "description": "a file, for its outline"}}, [], symbols)
    tool("hover", "What VSCodium shows when you hover over a name: its type, its signature and its docs. " + where,
         at, ["path", "line"], place)
