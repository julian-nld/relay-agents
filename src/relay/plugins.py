"""Plugins (Relay 4.0): what's new after Relay's core was wrapped up comes as a plugin, a folder in
RELAY_HOME/plugins/<name>/ (docs/WRITING-A-PLUGIN.md).

- plugin.json says what it is (its label, description, version, and the plugin API it's written for: API).
- Its __init__.py's setup(plugin) adds API routes, background tasks, a hook for runs that end and tools for agents,
  all through the Plugin it gets: that, the page's `relay` (static/js/plugins.js) and Relay's HTTP API are the
  whole contract.
- Its static/index.js adds buttons, pages and Settings tabs to Relay's page.

A plugin's trouble is its own: one that can't be loaded (a mistake, sys.exit, a setup that hangs) is listed with
why (Settings → Plugins, `relay plugins`), and Relay starts as always; its routes, tools, tasks and hooks run in a
few threads of its own, with time limits, so a slow one never holds up Relay's. Plugins run in Relay's process, with
its rights, outside any box (SECURITY.md): only the user drops one in.
"""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import inspect
import json
import os
import re
import sys
import threading
import time
import types
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from relay import config
from relay.providers import _why

if TYPE_CHECKING:
    from relay.app import Relay

API = 1  # the plugin API this Relay has: a plugin.json asking for a later one isn't loaded
NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")  # a plugin's folder (a Python package), its tools' and routes' prefix
TOOL_NAME = re.compile(r"[a-z][a-z0-9_]*")
FORWARD = re.compile(r"[a-z][a-z0-9_-]{0,31}")  # a path of Relay's a plugin forwards to a server of its own
TOOL_LENGTH = 48  # <plugin>_<tool>, at most: models take 64 for a tool's name, mcp__relay__ included
# What a plugin's name can't begin with: its tools are <plugin>_<tool>, and these begin Relay's own (or are them)
RESERVED = {"agentos", "agents", "approve", "changes", "commit", "computer", "computers", "delegate", "diff",
            "fetch", "help", "memory", "merge", "provider", "relay", "repair", "restore", "search", "tell", "wait"}
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
MIN_EVERY = 60  # seconds: the least a background task waits between runs
THREADS = 4  # a plugin's own threads: its routes, tools, tasks and hooks share them, and nothing of Relay's does
SETUP_WAIT = 20  # seconds its import and setup() may take at Relay's start
ROUTE_WAIT = 120  # seconds a route may take before its caller is answered 504 (it goes on in its thread)
TOOL_WAIT = 600  # …and a tool, before the agent hears it took too long
PACKAGE = "relay_plugins"  # (each plugin is relay_plugins.<name>)


BUNDLED = Path(__file__).resolve().parents[2] / "plugins"  # (those that come with Relay, in its own folder)


def plugins_dir() -> Path:
    return config.data_dir() / "plugins"


def data_dir() -> Path:
    return config.data_dir() / "plugin-data"


def _said(exc: BaseException, folder: Path) -> str:
    """A mistake as the plugin's author needs it: what, and where in its folder (file and line)."""
    if isinstance(exc, SystemExit):
        return f"it called sys.exit({exc.code!r})" + _why(exc, folder).removeprefix(f"SystemExit: {exc}")
    return _why(exc, folder).removeprefix("PluginError: ")


class PluginError(Exception):
    """What a plugin's handler or tool says went wrong: the caller gets the message (an API call with `status`, 400
    to 599; a 401 is sent as 502, since Relay's page takes a 401 for signed out)."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        status = status if isinstance(status, int) and 400 <= status <= 599 else 500
        self.status = 502 if status == 401 else status


@dataclass
class Call:
    """A call of a plugin's route: what it asked."""
    method: str  # (a HEAD comes as GET)
    path: str  # (after /api/plugins/<name>/)
    params: dict[str, str]  # the route's {parts}
    query: dict[str, str]  # (a key given twice: its last value)
    json: Any  # the body, for POST, PUT and PATCH (None without one)
    user: dict[str, Any]  # who asked: {id, username}


@dataclass
class ToolCall:
    """A call of a plugin's tool by an agent: its conversation and run, and the arguments the agent gave (checked
    against the tool's schema: its types, what's required)."""
    conversation_id: str
    run_id: str
    user_id: int
    directory: str  # the conversation's folder, on this machine
    arguments: dict[str, Any]

    def within(self, path: str) -> Path:
        """`path` as the agent gave it (relative to its folder, or absolute) as a path in the conversation's
        folder; PluginError when it's anywhere else. A tool runs on this machine with Relay's rights, so a path
        from an agent goes through this before it's opened."""
        base = Path(self.directory).resolve()
        target = (base / str(path)).resolve()
        if not target.is_relative_to(base):
            raise PluginError(f"{path} isn't in this conversation's folder")
        return target


class Store:
    """A plugin's own small JSON store (plugin-data/<name>/store.json): each change written whole and at once (a
    crash never leaves half a file), one at a time, so routes, tools, tasks and hooks needn't lock for it."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.RLock()
        self._data: dict[str, Any] | None = None

    def _all(self) -> dict[str, Any]:
        if self._data is None:
            try:
                self._data = json.loads(self.path.read_text()) if self.path.exists() else {}
            except ValueError as exc:
                raise PluginError(f"its {self.path} isn't JSON any more ({exc}): mend or move it") from None
        return self._data

    def get(self, key: str, default: Any = None) -> Any:
        """The value kept under `key` (a copy), or `default`."""
        with self.lock:
            return copy.deepcopy(self._all().get(key, default))

    def set(self, key: str, value: Any) -> None:
        """Keep `value` (anything JSON has) under `key`."""
        with self.lock:
            data = self._all()
            data[key] = json.loads(json.dumps(value))  # (a copy, and what isn't JSON is said here)
            self._write(data)

    def delete(self, key: str) -> None:
        with self.lock:
            data = self._all()
            if data.pop(key, None) is not None:
                self._write(data)

    def update(self, key: str, change: Callable[[Any], Any], default: Any = None) -> Any:
        """`change(value)` (the value kept under `key`, or `default`) kept as the new value, with no other change
        in between; the new value."""
        with self.lock:
            value = change(copy.deepcopy(self._all().get(key, default)))
            self.set(key, value)
            return copy.deepcopy(value)

    def keys(self) -> list[str]:
        with self.lock:
            return list(self._all())

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        part = self.path.with_name(f".{self.path.name}.part")
        part.write_text(json.dumps(data, ensure_ascii=False, indent=1))
        os.replace(part, self.path)


@dataclass
class _Route:
    path: str
    pattern: re.Pattern[str]
    methods: tuple[str, ...]
    handler: Callable[[Call], Any]


@dataclass
class _Task:
    every: float
    run: Callable[[], Any]
    due: float = 0.0  # (the first tick after Relay started: then every `every` seconds from the last start)
    running: bool = False


@dataclass
class _Tool:
    name: str  # <plugin>_<tool>
    description: str
    schema: dict[str, Any]
    handler: Callable[[ToolCall], Any]
    governor: bool
    only: Callable[[str], Any] | None = None  # the conversations it's for (None: all)


def _plain(what: str, function: Any) -> None:
    if not callable(function):
        raise PluginError(f"{what}: a function")
    if inspect.iscoroutinefunction(function) or inspect.iscoroutinefunction(type(function).__call__):
        raise PluginError(f"{what}: a plain function (def, not async def): Relay runs it in a thread of its own")


class Plugin:
    """What a plugin's setup() gets: everything it adds to Relay goes through it (docs/WRITING-A-PLUGIN.md)."""

    def __init__(self, name: str, folder: Path, manifest: dict[str, Any], sdk: Callable[[Plugin, Any], Any]) -> None:
        self.name = name
        self.folder = folder
        self.label = str(manifest.get("label") or name)
        self.version = str(manifest.get("version") or "")
        self._sdk_for = sdk
        self._sdk: Any = None
        self._sdk_lock = threading.Lock()
        self._store: Store | None = None
        self.routes: list[_Route] = []
        self.tasks: list[_Task] = []
        self.hooks: list[Callable[[dict[str, Any]], Any]] = []
        self.tools: list[_Tool] = []
        self.forwards: dict[str, int | Callable[[], int]] = {}
        self.pool = ThreadPoolExecutor(THREADS, thread_name_prefix=f"relay-plugin-{name}")

    @property
    def data(self) -> Path:
        """A folder of the plugin's own for what it keeps (RELAY_HOME/plugin-data/<name>), made when first asked."""
        folder = data_dir() / self.name
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    @property
    def store(self) -> Store:
        """Its own small JSON store, in its data folder: get, set, delete, update, keys."""
        if self._store is None:
            self._store = Store(self.data / "store.json")
        return self._store

    @property
    def sdk(self) -> Any:
        """Relay's own API as a script has it: a relay_sdk Session, signed in with an app password of this
        plugin's own ("plugin <name>" in Settings → Account; a new one when the user revoked it). Pass it as
        `user=` to the SDK's functions. It's synchronous: use it in a route, a task, a hook or a tool."""
        with self._sdk_lock:
            self._sdk = self._sdk_for(self, self._sdk)
            return self._sdk

    def route(self, path: str, handler: Callable[[Call], Any], methods: tuple[str, ...] | list[str] = ("GET",)) -> None:
        """Serve `handler` at /api/plugins/<name>/<path> (signed in, as Relay's own API). `{part}`s of the path
        come in call.params. It returns what's answered as JSON, or raises PluginError."""
        _plain(f"route {path!r}", handler)
        path = path.strip("/")
        wanted = tuple(m.upper() for m in methods)
        if not wanted or any(m not in METHODS for m in wanted):
            raise PluginError(f"route {path!r}: methods are some of {', '.join(METHODS)}")
        twice = [m for r in self.routes if r.path == path for m in r.methods if m in wanted]
        if twice:
            raise PluginError(f"route {path!r} takes {', '.join(twice)} twice")
        pattern = re.sub(r"\\\{([A-Za-z_][A-Za-z0-9_]*)\\\}", r"(?P<\1>[^/]+)", re.escape(path))
        self.routes.append(_Route(path, re.compile(pattern), wanted, handler))

    def every(self, seconds: float, run: Callable[[], Any]) -> None:
        """Run `run()` in the background: about 20 seconds after Relay starts, then every `seconds` (at least 60)
        from the start of the run before, never twice at once."""
        _plain("every()", run)
        if not isinstance(seconds, (int, float)) or seconds < MIN_EVERY:
            raise PluginError(f"every(): at least {MIN_EVERY} seconds between runs")
        self.tasks.append(_Task(float(seconds), run))

    def on_run_ended(self, hook: Callable[[dict[str, Any]], Any]) -> None:
        """Call `hook(run)` when an agent's run ends: {conversation_id, run_id, user_id, provider_id, model, status
        (succeeded, failed or interrupted), error, subagent, temporary, started_by}."""
        _plain("on_run_ended()", hook)
        self.hooks.append(hook)

    def tool(self, name: str, description: str, schema: dict[str, Any] | None,
             handler: Callable[[ToolCall], Any], governor: bool = False,
             only: Callable[[str], Any] | None = None) -> None:
        """A tool for agents, <plugin>_<name>: in every chat and code task (not subagents; the Governor's too
        with `governor`), as an MCP tool or `relay plugin <plugin>_<name>`; with `only`, in those conversations
        `only(conversation_id)` says yes to. `schema`: its arguments' JSON Schema (an object). The handler answers
        with text (anything else is sent as JSON) or raises PluginError."""
        _plain(f"tool {name!r}", handler)
        if only is not None:
            _plain(f"tool {name!r}'s only", only)
        full = f"{self.name}_{name}"
        if not TOOL_NAME.fullmatch(name or ""):
            raise PluginError(f"tool {name!r}: lowercase letters, digits and _, starting with a letter")
        if len(full) > TOOL_LENGTH:
            raise PluginError(f"tool {name!r}: {full} is over {TOOL_LENGTH} characters (models take 64, with "
                              "Relay's prefix)")
        if not description:
            raise PluginError(f"tool {name!r}: say what it does (its description is what an agent goes by)")
        if any(t.name == full for t in self.tools):
            raise PluginError(f"tool {name!r} is there twice")
        schema = schema or {"type": "object", "properties": {}}
        if not isinstance(schema, dict) or schema.get("type") != "object" \
                or not isinstance(schema.get("properties", {}), dict) \
                or not all(isinstance(s, dict) for s in schema.get("properties", {}).values()):
            raise PluginError(f"tool {name!r}: its schema is a JSON Schema of type object, its properties objects")
        self.tools.append(_Tool(full, description, schema, handler, bool(governor), only))


    def forward(self, path: str, port: int | Callable[[], int]) -> None:
        """Relay's /<path> and /<path>/… (HTTP and WebSockets, the path as it is) to a server of the plugin's on this
        machine's `port` (or a function that says it then), for someone signed in to Relay, as /port/<n>/ is: a web
        app of its own at Relay's address, which its page can frame. `path` is one segment no page of Relay's has."""
        path = path.strip("/")
        if not FORWARD.fullmatch(path):
            raise PluginError(f"forward {path!r}: one segment of lowercase letters, digits, - and _")
        if not callable(port) and (not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536):
            raise PluginError(f"forward {path!r}: its port is a number from 1 to 65535, or a function giving one")
        if path in self.forwards:
            raise PluginError(f"forward {path!r} is there twice")
        self.forwards[path] = port


def checked(schema: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    """An agent's arguments as the tool's schema has them: what's required there, and its simple types made so
    (an agent's `relay plugin … text=2024` gives a number for a string: "2024"). What can't be, PluginError."""
    found = dict(arguments)
    missing = [k for k in schema.get("required") or [] if found.get(k) is None]
    if missing:
        raise PluginError(f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} needed")
    for key, spec in (schema.get("properties") or {}).items():
        value, kind = found.get(key), spec.get("type")
        if value is None:
            continue
        if kind == "string" and not isinstance(value, str):
            found[key] = json.dumps(value) if isinstance(value, (bool, list, dict)) else str(value)
        elif kind in ("integer", "number") and (isinstance(value, bool) or not isinstance(value, (int, float))):
            try:
                number = float(str(value).strip())
            except ValueError:
                raise PluginError(f"{key} is a number") from None
            if kind == "integer" and not number.is_integer():
                raise PluginError(f"{key} is a whole number")
            found[key] = int(number) if kind == "integer" or number.is_integer() else number
        elif kind == "integer" and isinstance(value, float):
            if not value.is_integer():
                raise PluginError(f"{key} is a whole number")
            found[key] = int(value)
        elif kind == "boolean" and not isinstance(value, bool):
            said = str(value).strip().lower()
            if said not in ("true", "false", "yes", "no", "1", "0"):
                raise PluginError(f"{key} is true or false")
            found[key] = said in ("true", "yes", "1")
    return found


@dataclass
class Found:
    """A folder in the plugins folder, as Settings → Plugins shows it."""
    name: str
    folder: Path
    manifest: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    plugin: Plugin | None = None  # (loaded)
    problem: str | None = None  # why it isn't loaded
    errors: dict[str, str] = field(default_factory=dict)  # what failed last: {"route"/"task"/"hook"/"tool": why}

    def frontend(self) -> dict[str, bool]:
        static = self.folder / "static"
        return {"script": (static / "index.js").is_file(), "style": (static / "style.css").is_file()}

    def working(self) -> Plugin | None:
        """The plugin, while it's loaded and on (turned off, it stops at once; on again, at the next start)."""
        return self.plugin if self.enabled else None


class Plugins:
    """The plugins of this Relay: found and loaded at its start, then called by its routes, its scheduler, its
    runs' ends and its agents' tool calls."""

    def __init__(self, relay: Relay | None = None, folder: Path | None = None) -> None:
        self.relay = relay
        self.folder = folder or plugins_dir()
        self.found: dict[str, Found] = {}
        self.lock = threading.Lock()
        self.taken: set[str] = set()  # (the first segments of Relay's own paths: no plugin forwards those)

    # Loading

    def load(self, off: list[str] | tuple[str, ...] = (), run: bool = True) -> None:
        """Find the plugins and load those that are on; a mistake in one is its own (it's said why). Without
        `run`, only what can be known without running a plugin's code (`relay doctor`): its plugin.json, and
        whether its Python parses."""
        self.found = {}
        _prepare()
        if not self.folder.is_dir():
            return
        for folder in sorted(p for p in self.folder.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))):
            found = Found(folder.name, folder, enabled=folder.name not in off)
            self.found[folder.name] = found
            try:
                found.manifest = self._manifest(folder)
            except PluginError as exc:
                found.problem = str(exc)
            else:
                if found.enabled and run:
                    self._load(found)
                elif not run:
                    found.problem = _parses(found)
            if found.problem and run:
                print(f"relay: plugins/{found.name} can't be loaded: {found.problem}", file=sys.stderr, flush=True)

    def check(self, folder: Path) -> Found:
        """One plugin's folder loaded as Relay would (`relay plugins check`): what it adds, or why it can't be."""
        _prepare()
        found = Found(folder.name, folder)
        try:
            found.manifest = self._manifest(folder)
        except PluginError as exc:
            found.problem = str(exc)
            return found
        self._load(found)
        return found

    def notice(self) -> None:
        """A plugin put in the folder since Relay started: known, and loaded at the next start."""
        if not self.folder.is_dir():
            return
        off = self.relay.settings.plugins_off if self.relay is not None else ()
        for folder in sorted(p for p in self.folder.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))):
            if folder.name not in self.found:
                found = Found(folder.name, folder, enabled=folder.name not in off)
                try:
                    found.manifest = self._manifest(folder)
                except PluginError as exc:
                    found.problem = str(exc)
                self.found = {**self.found, folder.name: found}  # (others go through it meanwhile)

    def bundled(self) -> list[dict[str, str]]:
        """The plugins that come with Relay and aren't in its plugins folder."""
        rows = []
        for folder in sorted(BUNDLED.glob("*/plugin.json")) if BUNDLED.is_dir() else []:
            if folder.parent.name not in self.found:
                try:
                    manifest = self._manifest(folder.parent)
                except PluginError:
                    continue
                rows.append({"name": folder.parent.name, "label": str(manifest.get("label") or folder.parent.name),
                             "description": str(manifest.get("description") or "")})
        return rows

    def link(self, name: str) -> None:
        """One that comes with Relay into its plugins folder, as the README says: on from the next start."""
        if name not in {p["name"] for p in self.bundled()}:
            raise PluginError(f"{name} isn't a plugin that comes with Relay", 404)
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / name).symlink_to(BUNDLED / name)
        self.notice()

    def _manifest(self, folder: Path) -> dict[str, Any]:
        if not NAME.fullmatch(folder.name):
            raise PluginError("its folder's name is lowercase letters, digits and _, starting with a letter")
        if folder.name.split("_")[0] in RESERVED:
            raise PluginError(f"it begins with {folder.name.split('_')[0]}, a name Relay's own tools have: name it "
                              "otherwise")
        try:
            manifest = json.loads((folder / "plugin.json").read_text())
        except FileNotFoundError:
            raise PluginError("it has no plugin.json") from None
        except (OSError, ValueError) as exc:
            raise PluginError(f"its plugin.json isn't JSON: {exc}") from None
        if not isinstance(manifest, dict):
            raise PluginError("its plugin.json is a JSON object")
        wanted = manifest.get("api", 1)
        if not isinstance(wanted, int) or isinstance(wanted, bool):
            raise PluginError('"api" in its plugin.json is a number: the plugin API it\'s written for')
        if wanted > API:
            raise PluginError(f"it's written for plugin API {wanted}; this Relay has {API}: update Relay")
        return manifest

    def _load(self, found: Found) -> None:
        """Its import and setup(), in a thread of their own: one that hangs (a network call, say) or calls
        sys.exit() leaves the plugin out, and Relay goes on."""
        done, result = threading.Event(), {}

        def load() -> None:
            try:
                result["plugin"] = self._import(found)
            except BaseException as exc:  # noqa: BLE001 - sys.exit() too: whatever a plugin does wrong is its own
                result["problem"] = _said(exc, found.folder)
            finally:
                done.set()

        threading.Thread(target=load, daemon=True, name=f"relay-plugin-{found.name}-setup").start()
        if not done.wait(SETUP_WAIT):
            found.problem = (f"its import and setup() didn't end within {SETUP_WAIT} seconds (something there "
                             "waits: a network call?), so Relay went on without it")
            return
        plugin = result.get("plugin")
        clash = plugin and next((f"its tool {t.name} is the {other.name} plugin's too" for t in plugin.tools
                                 for other in self.loaded() for o in other.tools if o.name == t.name), None)
        for path in plugin.forwards if plugin and not clash else ():
            other = next((o for o in self.loaded() if path in o.forwards), None)
            if path in self.taken:
                clash = f"it forwards /{path}, a path of Relay's own"
            elif other is not None:
                clash = f"it forwards /{path}, which the {other.name} plugin does"
            if clash:
                break
        found.problem = result.get("problem") or clash or None
        if found.problem:
            sys.modules.pop(f"{PACKAGE}.{found.name}", None)
            if plugin is not None:
                plugin.pool.shutdown(wait=False)
            return
        found.plugin = plugin

    def _import(self, found: Found) -> Plugin:
        plugin = Plugin(found.name, found.folder, found.manifest, self._sdk)
        code = found.folder / "__init__.py"
        if code.is_file():
            module_name = f"{PACKAGE}.{found.name}"
            spec = importlib.util.spec_from_file_location(module_name, code,
                                                          submodule_search_locations=[str(found.folder)])
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            setup = getattr(module, "setup", None)
            if setup is not None:
                if not callable(setup):
                    raise PluginError("setup in its __init__.py is a function: def setup(plugin)")
                setup(plugin)
        return plugin

    def forwarded(self, path: str) -> int | None:
        """The port a working plugin forwards this path of Relay's to (plugin.forward), or None."""
        segment = path.lstrip("/").split("/", 1)[0]
        if not segment or segment in self.taken:
            return None
        for plugin in self.loaded():
            port = plugin.forwards.get(segment)
            if port is not None:
                try:
                    port = port() if callable(port) else port
                except Exception as exc:  # noqa: BLE001 - its own mistake: nothing forwarded
                    self._failed(plugin, "forward", exc)
                    return None
                return port if isinstance(port, int) and 0 < port < 65536 else None
        return None

    def loaded(self) -> list[Plugin]:
        """The plugins at work: loaded, and on."""
        return [p for f in self.found.values() if (p := f.working()) is not None]

    def public(self) -> list[dict[str, Any]]:
        """Every plugin found, as Settings → Plugins and the page show them."""
        roots = self.relay.settings.roots() if self.relay is not None else []
        return [{"name": f.name, "label": str(f.manifest.get("label") or f.name),
                 "description": str(f.manifest.get("description") or ""),
                 "version": str(f.manifest.get("version") or ""),
                 "folder": str(f.folder), "enabled": f.enabled, "loaded": f.working() is not None, "problem": f.problem,
                 "errors": dict(f.errors), "tools": [t.name for t in (f.plugin.tools if f.plugin else [])],
                 # (in a workspace root, where agents work: they could change its code)
                 "exposed": any(f.folder.resolve().is_relative_to(root) for root in roots),
                 **(f.frontend() if f.working() is not None else {"script": False, "style": False})}
                for f in self.found.values()]

    def _failed(self, plugin: Plugin, where: str, exc: BaseException | str) -> str:
        why = exc if isinstance(exc, str) else _said(exc, plugin.folder)
        found = self.found.get(plugin.name)
        if found is not None:
            found.errors[where] = f"{time.strftime('%Y-%m-%d %H:%M')}: {why}"
        print(f"relay: plugin {plugin.name}: its {where} failed: {why}", file=sys.stderr, flush=True)
        return why

    def _working(self, name: str) -> Plugin:
        found = self.found.get(name)
        if found is None or found.working() is None:
            raise PluginError(f"No plugin {name!r} is loaded", 404)
        return found.plugin

    # Routes (/api/plugins/<name>/<path>)

    async def answer(self, name: str, call: Call) -> Any:
        """A call of a plugin's route, answered in one of its threads within ROUTE_WAIT seconds: its handler's
        answer (JSON), or PluginError."""
        plugin = self._working(name)
        try:
            return await asyncio.wait_for(asyncio.get_running_loop().run_in_executor(plugin.pool, self.call, name,
                                                                                     call),
                                          ROUTE_WAIT)
        except (asyncio.TimeoutError, TimeoutError):
            self._failed(plugin, "route", f"/{call.path} didn't answer within {ROUTE_WAIT} seconds")
            raise PluginError(f"The {name} plugin didn't answer within {ROUTE_WAIT} seconds", 504) from None

    def call(self, name: str, call: Call) -> Any:
        """Answer a call of a plugin's route (in a thread): its handler's answer, or PluginError."""
        plugin = self._working(name)
        method = "GET" if call.method == "HEAD" else call.method
        allowed: set[str] = set()
        for route in plugin.routes:
            matched = route.pattern.fullmatch(call.path.strip("/"))
            if matched is None:
                continue
            if method not in route.methods:
                allowed.update(route.methods)
                continue
            call.params = matched.groupdict()
            try:
                answer = route.handler(call)
            except PluginError:
                raise
            except BaseException as exc:  # noqa: BLE001 - the caller hears about it, instead of an HTTP 500
                why = self._failed(plugin, "route", f"/{route.path}: {_said(exc, plugin.folder)}")
                raise PluginError(f"The {name} plugin failed: {why}", 500) from None
            try:
                json.dumps(answer)
            except (TypeError, ValueError) as exc:  # (its mistake, said as one, not Relay's HTTP 500)
                why = self._failed(plugin, "route", f"/{route.path} answered with what isn't JSON: {exc}")
                raise PluginError(f"The {name} plugin failed: {why}", 500) from None
            return answer
        if allowed:
            raise PluginError(f"/{call.path} takes {', '.join(sorted(allowed))}", 405)
        raise PluginError(f"The {name} plugin has nothing at /{call.path}", 404)

    def static(self, name: str, path: str) -> Path | None:
        """A file of a working plugin's static/ folder, or None (nothing outside it)."""
        found = self.found.get(name)
        if found is None or found.working() is None:
            return None
        try:
            root = (found.folder / "static").resolve()
            target = (root / path).resolve()
            return target if target.is_relative_to(root) and target.is_file() else None
        except (OSError, ValueError):  # (a NUL in the path, say)
            return None

    # Background tasks, and runs that ended

    def tick(self, now: float) -> None:
        """Start the tasks that are due, each in one of its plugin's threads (Relay's scheduler, every few
        seconds)."""
        for plugin in self.loaded():
            for task in plugin.tasks:
                with self.lock:
                    if task.running or now < task.due:
                        continue
                    task.running = True
                    task.due = now + task.every
                try:
                    plugin.pool.submit(self._task, plugin, task)
                except RuntimeError as exc:  # (no thread to be had: the next tick tries again)
                    task.running = False
                    self._failed(plugin, "task", exc)

    def _task(self, plugin: Plugin, task: _Task) -> None:
        try:
            task.run()
        except BaseException as exc:  # noqa: BLE001 - one plugin's task never stops Relay's loop
            self._failed(plugin, "task", exc)
        finally:
            task.running = False

    def run_ended(self, run: dict[str, Any]) -> None:
        """Tell the plugins that want to know that a run ended (in their own threads)."""
        for plugin in self.loaded():
            for hook in plugin.hooks:
                try:
                    plugin.pool.submit(self._hook, plugin, hook, dict(run))
                except RuntimeError as exc:
                    self._failed(plugin, "hook", exc)

    def _hook(self, plugin: Plugin, hook: Callable[[dict[str, Any]], Any], run: dict[str, Any]) -> None:
        try:
            hook(run)
        except BaseException as exc:  # noqa: BLE001 - one plugin's hook never touches the run
            self._failed(plugin, "hook", exc)

    # Tools for agents (relay.delegation)

    def tools(self, governor: bool = False, conversation_id: str | None = None) -> list[dict[str, Any]]:
        """The plugins' tools as MCP tools: all of them, or (the Governor's) those meant for it too; with
        `conversation_id`, those that are that conversation's (a tool's `only`)."""
        return [{"name": t.name, "description": f"{t.description} (from the {p.label} plugin)",
                 "inputSchema": t.schema}
                for p in self.loaded() for t in p.tools if (t.governor or not governor)
                and (conversation_id is None or self._for(p, t, conversation_id))]

    def _for(self, plugin: Plugin, tool: _Tool, conversation_id: str) -> bool:
        """Whether `tool` is the conversation's: its `only` says so (one that fails says no, and is the plugin's
        last error)."""
        if tool.only is None:
            return True
        try:
            return bool(tool.only(conversation_id))
        except BaseException as exc:  # noqa: BLE001 - a plugin's mistake never touches the run
            self._failed(plugin, "tool", f"{tool.name}'s only: {_said(exc, plugin.folder)}")
            return False

    async def use_tool(self, name: str, call: ToolCall) -> str:
        """A plugin's tool, run in one of its threads within TOOL_WAIT seconds: its answer as text, or
        PluginError with what the agent hears."""
        plugin = next((p for p in self.loaded() for t in p.tools if t.name == name), None)
        if plugin is None:
            raise PluginError(f"No plugin has a tool {name!r}")
        try:
            return await asyncio.wait_for(asyncio.get_running_loop().run_in_executor(
                plugin.pool, self.call_tool, name, call), TOOL_WAIT)
        except (asyncio.TimeoutError, TimeoutError):
            self._failed(plugin, "tool", f"{name} didn't answer within {TOOL_WAIT} seconds")
            raise PluginError(f"The {plugin.name} plugin's {name} didn't answer within {TOOL_WAIT} seconds") from None

    def call_tool(self, name: str, call: ToolCall) -> str:
        """Run a plugin's tool (in a thread): its answer as text, or PluginError with what the agent hears."""
        for plugin in self.loaded():
            for tool in plugin.tools:
                if tool.name != name:
                    continue
                if not self._for(plugin, tool, call.conversation_id):
                    raise PluginError(f"{name} isn't for this conversation (the {plugin.name} plugin gives it to "
                                      "some only)")
                try:
                    call.arguments = checked(tool.schema, call.arguments)
                except PluginError as exc:
                    raise PluginError(f"{name}: {exc}") from None
                try:
                    answer = tool.handler(call)
                except PluginError:
                    raise
                except BaseException as exc:  # noqa: BLE001 - the agent hears about it
                    raise PluginError(f"The {plugin.name} plugin failed: {self._failed(plugin, 'tool', exc)}") from None
                return answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False, default=str)
        raise PluginError(f"No plugin has a tool {name!r}")

    # Relay's own API, for plugin.sdk

    def _sdk(self, plugin: Plugin, session: Any) -> Any:
        """A relay_sdk Session for `plugin` (`session`, its last one, while it still works): signed in with its
        own app password (kept apart from its data, made anew when the user revoked it), or none needed (a Relay
        without login)."""
        if self.relay is None:
            raise PluginError("plugin.sdk needs a running Relay")
        import relay_sdk
        if not self.relay.login_required:
            return session or relay_sdk.Session(self.relay.runs.local_url)
        if session is not None and self.relay.accounts.user_for_app_password(session._app_password) is not None:
            return session
        kept = data_dir() / ".app-passwords" / plugin.name
        password = kept.read_text().strip() if kept.is_file() else ""
        if not password or self.relay.accounts.user_for_app_password(password) is None:
            owner = self.relay.db.one("SELECT id FROM users ORDER BY id LIMIT 1")
            if owner is None:
                raise PluginError("Relay has no account yet")
            password = self.relay.accounts.create_app_password(owner["id"], f"plugin {plugin.name}")["password"]
            kept.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            kept.write_text(password)
            kept.chmod(0o600)
        session = relay_sdk.Session(self.relay.runs.local_url)
        session._app_password = password
        return session


def _prepare() -> None:
    """The package plugins are loaded in, and Relay's SDK on the path (`import relay_sdk` in a plugin works)."""
    if PACKAGE not in sys.modules:
        package = types.ModuleType(PACKAGE)
        package.__path__ = []
        sys.modules[PACKAGE] = package
    from relay.sdk import SOURCE  # (sdk/python/relay_sdk)
    if str(SOURCE.parent) not in sys.path:
        sys.path.append(str(SOURCE.parent))


def _parses(found: Found) -> str | None:
    """Whether its Python parses (without running it): what's wrong where, or None."""
    for path in sorted(found.folder.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            compile(path.read_text(), str(path), "exec")
        except (SyntaxError, ValueError, OSError) as exc:
            return _said(exc, found.folder)
    return None
