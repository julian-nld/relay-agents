"""OpenHands: its Agent Canvas (UI, Agent Server, automations) in a container of Relay's, working with Relay's agents.

The container (relay-openhands, or with a suffix for a Relay elsewhere; labelled as this plugin's) runs OpenHands'
all-in-one image under rootless podman as the user (`--userns=keep-id`), its port on 127.0.0.1 only: it's reached
through Relay, after Relay's sign-in, its UI at Relay's /canvas (plugin.forward: Canvas knows no other path) and its
API at Relay's /port/<port>/, so its key may be in its page. Its agent is Relay: the ACP forwarder (plugins/acp)
mounted read-only, with Relay's workspace roots as /projects and `--map` back, so a conversation in a project is
Relay's code task in the real folder, with any of Relay's agents. Relay's app in it (canvas-app/) shows those tasks
and leads back to Relay. Its page (static/index.js) sets it up (an app password for it, the image), starts and stops
it (remembered), and has Canvas framed while it runs."""

import hashlib
import json
import re
import secrets
import shlex
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlparse

import relay_sdk
from relay import containers
from relay.plugins import PluginError

IMAGE = "ghcr.io/openhands/agent-canvas"
REGISTRY = "https://ghcr.io/v2/openhands/agent-canvas"  # (its `latest`'s version, read without pulling it)
PINNED = "1.26.0"  # (tried with Relay: Update moves it to the newest)
NAME = "relay-openhands"  # (of the Relay at its usual place: container_name())
LABEL = "io.relay.plugin=openhands"
USER = "10001"  # the image's user (openhands), mapped to Relay's own
PORT = 8000
STARTING = 180  # seconds the Agent Server may take to answer after a start
SYNC_EVERY = 5  # minutes between two looks for new project folders
NEWER_EVERY = 24 * 3600  # seconds between two looks for a newer OpenHands
ROOT = Path(__file__).resolve().parents[2]  # (the clone this plugin is part of)
FORWARDER = ROOT / "plugins" / "acp" / "static"
SDK = ROOT / "sdk" / "python"
APP = Path(__file__).resolve().parent / "canvas-app"  # Relay's app in Canvas
APP_PLACE = "/opt/relay/plugins/openhands/canvas-app"  # (where the container has it)


def podman(*args, timeout=60):
    """podman's answer (the tests give their own)."""
    argv = ["podman", *args]
    if args[:1] == ("run",) and in_a_service() and shutil.which("systemd-run"):
        # (its network's pasta, which podman leaves running, in a scope of its own, as Relay starts its boxes: in
        # Relay's cgroup a restart of Relay would end it, and OpenHands would run on without its port)
        argv = ["systemd-run", "--user", "--scope", "--quiet", "--collect", *argv]
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(argv, 1, "", str(exc))


def in_a_service():
    """Whether Relay runs as a systemd service (its cgroup a unit's: relay.service)."""
    try:
        return any(line.endswith(".service") for line in Path("/proc/self/cgroup").read_text().splitlines())
    except OSError:
        return False


def canvas(port, key, method, path, body=None, timeout=30):
    """A call of the Agent Server's API in the container (the tests give their own): its JSON."""
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"X-Session-API-Key": key, "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as answer:
        return json.loads(answer.read() or b"null")


def latest_version():
    """The newest OpenHands' version: the label of its `latest` image, from its registry (its index, the amd64
    manifest, its config: a few kilobytes, nothing pulled). None when it can't say. (The tests give their own.)"""
    try:
        with urllib.request.urlopen("https://ghcr.io/token?scope=repository:openhands/agent-canvas:pull",
                                    timeout=15) as answer:
            token = json.load(answer)["token"]

        def get(path, accept):
            request = urllib.request.Request(f"{REGISTRY}/{path}",
                                             headers={"Authorization": f"Bearer {token}", "Accept": accept})
            with urllib.request.urlopen(request, timeout=15) as answer:
                return json.load(answer)

        manifests = ("application/vnd.oci.image.manifest.v1+json, "
                     "application/vnd.docker.distribution.manifest.v2+json")
        index = get("manifests/latest", "application/vnd.oci.image.index.v1+json, "
                    "application/vnd.docker.distribution.manifest.list.v2+json, " + manifests)
        manifest = next((m for m in index.get("manifests", [])
                         if m.get("platform", {}).get("architecture") == "amd64"), None)
        if manifest is not None:
            index = get(f"manifests/{manifest['digest']}", manifests)
        config = get(f"blobs/{index['config']['digest']}", "application/json")
        return config["config"]["Labels"].get("org.opencontainers.image.version") or None
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError, StopIteration):
        return None


def relays(profile):
    """Whether an agent profile of OpenHands' is Relay's: the forwarder as its ACP agent."""
    return profile.get("agent_kind") == "acp" and "/relay-acp.py" in (profile.get("acp_command") or "")


def container_name():
    """Its container's name: relay-openhands for the Relay at its usual place (~/.local/share/relay), else with that
    Relay's mark, as its boxes have it, so a trial's or a test's Relay on the same machine never touches the user's
    OpenHands."""
    mark = containers.instance()
    return f"{NAME}-{mark}" if mark else NAME


def version(text):
    """A version as numbers ("1.26.0" → (1, 26, 0)), None when it isn't one."""
    try:
        return tuple(int(part) for part in str(text).split("."))
    except ValueError:
        return None


def setup(plugin):
    store = plugin.store
    container = container_name()
    # (ready: Canvas to use, set up for Relay; not while a start makes it anew)
    work = {"doing": None, "lock": threading.Lock(), "minute": 0, "starts": [], "app": None, "ready": True}

    def state():
        """The container: "running", "exited", … or None when there's none."""
        found = podman("ps", "-a", "--filter", f"label={LABEL}", "--filter", f"name=^{container}$", "--format",
                       "{{.State}}")
        if found.returncode != 0:
            return None
        return found.stdout.strip().split("\n")[0] or None

    def image():
        return f"{IMAGE}:{store.get('image', PINNED)}"

    def relay_port():
        return urlparse(plugin.sdk.url).port or 8420

    def roots():
        """Relay's workspace roots, and where each is in the container."""
        found = [r for r in plugin.sdk.request("GET", "/api/settings")["workspace_roots"]
                 if Path(r).is_dir()]
        if len(found) == 1:
            return [(found[0], "/projects")]
        named, places = {}, []
        for root in found:
            name = re.sub(r"[^A-Za-z0-9._-]", "-", Path(root).name) or "root"
            named[name] = named.get(name, 0) + 1
            places.append((root, f"/projects/{name}" + (f"-{named[name]}" if named[name] > 1 else "")))
        return places

    def private_scripts():
        """The user's private scripts: not for OpenHands, as for Relay's agents (an empty folder in their place)."""
        try:
            folder = plugin.sdk.request("GET", "/api/scripts")["folder"]
        except relay_sdk.RelayError:
            return None
        private = Path(folder) / "private"
        return private if private.is_dir() else None

    def say(doing):
        work["doing"] = doing

    def failed(why):
        store.set("error", f"{time.strftime('%Y-%m-%d %H:%M')}: {why}")
        say(None)

    def start():
        """The container made anew (its state stays) and OpenHands set up to work with Relay."""
        if not (FORWARDER / "relay-acp.py").is_file() or not (SDK / "relay_sdk").is_dir():
            raise PluginError("OpenHands works with Relay through the ACP plugin (plugins/acp): it isn't in this clone")
        port = store.get("port", PORT)
        work["ready"] = False
        if not store.get("api_key"):  # (the first start, or the first after Remove with its data)
            store.set("api_key", secrets.token_hex(24))
            store.set("secret_key", secrets.token_hex(32))
        if state() is not None:
            podman("rm", "-f", "-t", "10", container)
        places = roots()
        state_dir = plugin.data / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        mounts = [f"{state_dir}:/home/openhands/.openhands", f"{FORWARDER}:/opt/relay/plugins/acp/static:ro",
                  f"{SDK}:/opt/relay/sdk/python:ro", f"{APP}:{APP_PLACE}:ro"]
        mounts += [f"{root}:{place}" for root, place in places]
        masks = []
        private = private_scripts()
        for root, place in places:
            if private and private.resolve().is_relative_to(Path(root).resolve()):
                # (notmpcopyup: podman would copy what's there into it first, the very files it hides)
                masks += ["--tmpfs", f"{place}/{private.resolve().relative_to(Path(root).resolve())}:ro,size=1k,"
                                     "notmpcopyup"]
        # (its key in its page: only this machine reaches its port, and the browser only through Relay's sign-in)
        env = {"PORT": "8000", "LOCAL_BACKEND_API_KEY": store.get("api_key"), "OH_SECRET_KEY": store.get("secret_key"),
               "AGENT_CANVAS_ALLOW_LAN_SESSION_KEY": "true",
               "AGENT_CANVAS_DISABLE_TELEMETRY": "1", "VITE_DO_NOT_TRACK": "1", "DO_NOT_TRACK": "1",
               # (after each answer it waits for the agent's token count, 2 s by default: the forwarder has none)
               "ACP_USAGE_UPDATE_TIMEOUT": "0"}
        say("starting OpenHands")
        made = podman("run", "-d", "--name", container, "--label", LABEL, f"--userns=keep-id:uid={USER},gid={USER}",
                      "-p", f"127.0.0.1:{port}:8000", *[a for e in env.items() for a in ("-e", f"{e[0]}={e[1]}")],
                      *[a for m in mounts for a in ("-v", m)], *masks, image(), timeout=120)
        if made.returncode != 0:
            raise PluginError(f"podman couldn't start it: {made.stderr.strip()[-300:]}")
        deadline = time.monotonic() + STARTING
        while True:
            try:
                canvas(port, store.get("api_key"), "GET", "/health", timeout=5)
                break
            except (urllib.error.URLError, OSError, ValueError):
                if time.monotonic() > deadline or state() != "running":
                    raise PluginError("OpenHands didn't answer: " + logs(20)[-400:]) from None
                time.sleep(2)
        configure(places)
        work["ready"] = True
        work["starts"] = [t for t in work["starts"] if t > time.time() - 600] + [time.time()]
        store.set("error", None)
        say(None)

    def configure(places=None):
        """OpenHands' agent: Relay through the forwarder; no telemetry; Relay's projects as its workspaces; Relay's
        app in it."""
        places = places or roots()
        port, key = store.get("port", PORT), store.get("api_key")
        # (what Relay asks you to allow stays in Relay: OpenHands would allow anything unasked)
        command = ["python3", "/opt/relay/plugins/acp/static/relay-acp.py", "--skip-context", "--ask-in-relay",
                   *[a for root, place in places for a in ("--map", f"{place}={root}")]]
        canvas(port, key, "PATCH", "/api/settings", {
            "agent_settings_diff": {"agent_kind": "acp", "acp_server": "custom", "acp_command": command,
                                    "acp_model": store.get("agent") or None,
                                    "agent_context": {"load_public_skills": False, "load_user_skills": False,
                                                      "load_project_skills": False}},
            "misc_settings_diff": {"app_preferences": {"user_consents_to_analytics": False}}})
        for name, value in (("RELAY_URL", f"http://host.containers.internal:{relay_port()}"),
                            ("RELAY_APP_PASSWORD", store.get("app_password"))):
            canvas(port, key, "PUT", "/api/settings/secrets", {"name": name, "value": value})
        profile(command)
        workspaces(places)
        try:
            relay_app()
            work["app"] = None
        except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as exc:
            work["app"] = f"Relay's app in OpenHands isn't there: {exc}"  # (OpenHands works without it)

    def relay_app():
        """Relay's app in Canvas (canvas-app/): installed from its folder in the container when it isn't there or
        has changed since (Canvas keeps a copy), and on."""
        port, key = store.get("port", PORT), store.get("api_key")
        made = app_hash()
        found = next((a for a in canvas(port, key, "GET", "/api/canvas-extensions/installed")["canvas_extensions"]
                      if a.get("name") == "relay"), None)
        if found is None or store.get("app") != made:
            found = canvas(port, key, "POST", "/api/canvas-extensions/install", {"source": APP_PLACE, "force": True})
            store.set("app", made)
        if not found.get("enabled"):
            canvas(port, key, "PATCH", "/api/canvas-extensions/installed/relay", {"enabled": True})

    def app_hash():
        """Relay's app in Canvas as it is in this clone: a hash of its files."""
        return hashlib.sha256(b"".join(f.read_bytes() for f in sorted(APP.iterdir()) if f.is_file())).hexdigest()

    def profile(command):
        """Canvas starts each conversation from its active agent profile, which it made once from the settings (when
        it first listed its profiles) and which doesn't follow them: Relay's profile gets their command and agent,
        and is the active one."""
        port, key = store.get("port", PORT), store.get("api_key")
        want = {"agent_kind": "acp", "acp_server": "custom", "acp_command": shlex.join(command),
                "acp_model": store.get("agent") or None}
        listed = canvas(port, key, "GET", "/api/agent-profiles")  # (the first look makes "default" of the settings)
        ours = [found for found in (canvas(port, key, "GET", f"/api/agent-profiles/{quote(p['name'])}")["profile"]
                                    for p in listed.get("profiles") or ()) if relays(found)]
        for found in ours or [{"name": "relay"}]:
            if any(found.get(name) != value for name, value in want.items()):
                canvas(port, key, "POST", f"/api/agent-profiles/{quote(found['name'])}", {**found, **want})
        if listed.get("active_agent_profile_id") not in {found.get("id") for found in ours}:
            names = {found["name"] for found in ours} or {"relay"}
            made = canvas(port, key, "GET", "/api/agent-profiles")["profiles"]  # (a new one's id)
            chosen = next(p["id"] for p in made if p["name"] in names)
            canvas(port, key, "POST", f"/api/agent-profiles/{quote(chosen)}/activate")

    def relays_agent():
        """Whether OpenHands' conversations start with Relay's agent (another may be chosen in OpenHands): its active
        agent profile's, or without one its settings'; None when it can't say."""
        port, key = store.get("port", PORT), store.get("api_key")
        try:
            listed = canvas(port, key, "GET", "/api/agent-profiles", timeout=5)
            active = next((p for p in listed.get("profiles") or ()
                           if p.get("id") == listed.get("active_agent_profile_id")), None)
            if active is not None:
                return relays(canvas(port, key, "GET", f"/api/agent-profiles/{quote(active['name'])}",
                                     timeout=5)["profile"])
            agent = canvas(port, key, "GET", "/api/settings", timeout=5)["agent_settings"]
        except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError):
            return None
        return agent.get("agent_kind") == "acp" and any(str(a).endswith("/relay-acp.py")
                                                         for a in agent.get("acp_command") or ())

    def workspaces(places=None):
        """Each project folder in Relay's workspace roots, a workspace in OpenHands (new ones as they come)."""
        places = places or roots()
        port, key = store.get("port", PORT), store.get("api_key")
        canvas(port, key, "POST", "/api/workspaces/parents", {"parents": [
            {"id": f"relay-{i}", "name": f"{Path(root).name} (Relay)", "path": place}
            for i, (root, place) in enumerate(places)]})
        found = []
        for root, place in places:
            for folder in sorted(Path(root).iterdir()):
                if folder.is_dir() and not folder.name.startswith("."):
                    found.append({"id": f"relay-{place.strip('/').replace('/', '-')}-{folder.name}"[:120],
                                  "name": folder.name, "path": f"{place}/{folder.name}", "parentPath": place})
        if found:
            canvas(port, key, "POST", "/api/workspaces", {"workspaces": found})

    def logs(lines=200):
        found = podman("logs", "--tail", str(lines), container)
        return (found.stdout + found.stderr).strip()

    def in_background(job, doing):
        """Long work (a pull of gigabytes, a start) in a thread of its own: the page asks how far it got."""
        if not work["lock"].acquire(blocking=False):
            raise PluginError(f"OpenHands is busy: {work['doing'] or 'working'}", 409)
        say(doing)

        def run():
            try:
                job()
            except PluginError as exc:
                failed(str(exc))
            except Exception as exc:  # noqa: BLE001 - said on the page
                failed(f"{type(exc).__name__}: {exc}")
            finally:
                work["lock"].release()

        threading.Thread(target=run, daemon=True).start()

    def pull():
        say(f"downloading OpenHands ({image()}, about 4.4 GB)")
        pulled = podman("pull", image(), timeout=3600)
        if pulled.returncode != 0:
            raise PluginError(f"The download failed: {pulled.stderr.strip()[-300:]}")

    # Its page's API, at /api/plugins/openhands/…
    def newer():
        """A newer OpenHands than this one, when the last look found one."""
        latest, ours = store.get("latest"), store.get("image", PINNED)
        return latest if version(latest) and version(ours) and version(latest) > version(ours) else None

    def status(call):
        now = state()
        present = podman("image", "exists", image()).returncode == 0
        return {"set_up": bool(store.get("app_password")) and present, "image": image(), "present": present,
                "has_password": bool(store.get("app_password")),
                "state": now, "port": store.get("port", PORT), "api_key": store.get("api_key"),
                "agent": store.get("agent") or "", "wanted": store.get("running", False), "doing": work["doing"],
                "container": container,
                "ready": now == "running" and work["ready"],
                "error": store.get("error"), "app_error": work["app"], "newer": newer(),
                "relay_agent": relays_agent() if now == "running" and not work["doing"] else None}

    def set_up(call):
        password = (call.json or {}).get("app_password")
        if not isinstance(password, str) or not password.startswith("relay_"):
            raise PluginError("An app password for OpenHands is needed (Settings → Account makes one)")
        store.set("app_password", password)

        def job():
            pull()
            start()
            store.set("running", True)

        in_background(job, "setting up")
        return status(call)

    def turn_on(call):
        if not store.get("app_password"):
            raise PluginError("Set OpenHands up first")
        work["starts"] = []  # (started by hand: its count of starts begins anew)

        def job():
            if podman("image", "exists", image()).returncode != 0:
                pull()
            start()
            store.set("running", True)

        in_background(job, "starting OpenHands")
        return status(call)

    def turn_off(call):
        store.set("running", False)
        if state() is not None:
            podman("stop", "-t", "10", container, timeout=60)
        return status(call)

    def update(call):
        def job():
            say("looking for a newer OpenHands")
            pulled = podman("pull", f"{IMAGE}:latest", timeout=3600)
            if pulled.returncode != 0:
                raise PluginError(f"The download failed: {pulled.stderr.strip()[-300:]}")
            version = podman("image", "inspect", f"{IMAGE}:latest", "--format",
                             "{{index .Config.Labels \"org.opencontainers.image.version\"}}").stdout.strip()
            if version and version != store.get("image", PINNED):
                podman("tag", f"{IMAGE}:latest", f"{IMAGE}:{version}")
                store.set("image", version)
                if store.get("running"):
                    start()
            say(None)

        in_background(job, "updating OpenHands")
        return status(call)

    def change(call):
        given = call.json or {}
        port, agent = given.get("port", store.get("port", PORT)), given.get("agent", store.get("agent") or "")
        if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535 or port == relay_port():
            raise PluginError("The port is a number from 1024 to 65535, not Relay's own")
        if not isinstance(agent, str) or (agent and "|" not in agent):
            raise PluginError("The agent is a provider and its model, as \"provider|model\"")
        moved = port != store.get("port", PORT)
        store.set("port", port)
        store.set("agent", agent)
        if state() == "running":
            if moved:
                in_background(start, "moving OpenHands to its new port")
            else:
                configure()
        return status(call)

    def conversations(call):
        """OpenHands' conversations, newest first, each with the Relay conversation it is (its ACP session)."""
        if not store.get("api_key"):
            return {"conversations": []}  # (never set up, or removed)
        try:
            found = canvas(store.get("port", PORT), store.get("api_key"), "GET",
                           "/api/conversations/search?limit=100&sort_order=UPDATED_AT_DESC", timeout=10)
        except (urllib.error.URLError, OSError, ValueError):
            return {"conversations": []}  # (not running, or not answering: none to show)
        return {"conversations": [{"id": c.get("id"), "title": c.get("title") or "",
                                   "status": c.get("execution_status"),
                                   "relay": (c.get("agent_state") or {}).get("acp_session_id")}
                                  for c in (found or {}).get("items") or [] if isinstance(c, dict)]}

    def sync(call):
        """Relay's project folders in OpenHands now (its page asks when it opens), not at the next look."""
        if state() != "running":
            raise PluginError("OpenHands isn't running", 409)
        workspaces()
        return {"ok": True}

    def use_relay(call):
        """OpenHands' agent Relay's again (after another was chosen in OpenHands)."""
        if state() != "running":
            raise PluginError("OpenHands isn't running")
        configure()
        return status(call)

    def remove(call):
        store.set("running", False)
        if state() is not None:
            podman("rm", "-f", "-t", "10", container, timeout=60)
        if (call.json or {}).get("data"):  # (its conversations and settings too)
            shutil.rmtree(plugin.data / "state", ignore_errors=True)
            for key in ("api_key", "secret_key", "app"):
                store.delete(key)
        return status(call)

    plugin.route("status", status)
    plugin.route("setup", set_up, methods=["POST"])
    plugin.route("start", turn_on, methods=["POST"])
    plugin.route("stop", turn_off, methods=["POST"])
    plugin.route("update", update, methods=["POST"])
    plugin.route("settings", change, methods=["PUT"])
    plugin.route("use-relay", use_relay, methods=["POST"])
    plugin.route("conversations", conversations)
    plugin.route("sync", sync, methods=["POST"])
    plugin.route("remove", remove, methods=["POST"])
    plugin.route("logs", lambda call: {"logs": logs()})
    # Canvas at Relay's /canvas, after Relay's sign-in (its page frames it there)
    plugin.forward("canvas", lambda: store.get("port", PORT))

    # Every minute: on when it was left on (after Relay's start, after a crash: not again and again), Relay's app in
    # it when it changed, the project folders that came since, every few minutes, and a newer OpenHands, once a day
    def minute():
        if store.get("app_password") and time.time() - store.get("looked", 0) > NEWER_EVERY:
            store.set("looked", time.time())
            found = latest_version()
            if found:
                store.set("latest", found)
        if work["lock"].locked() or not store.get("running"):
            return
        now = state()
        if now != "running":
            if len([t for t in work["starts"] if t > time.time() - 600]) >= 3:
                if not store.get("error"):
                    failed("It stopped again and again; Start tries anew. Its log: " + logs(20)[-400:])
                return
            in_background(start, "starting OpenHands again")
            return
        work["minute"] += 1
        try:
            if work["ready"] and store.get("app") != app_hash():  # (Relay's app changed with this clone: anew)
                relay_app()
                work["app"] = None
            if work["minute"] % SYNC_EVERY == 0:
                workspaces()
        except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError, relay_sdk.RelayError):
            pass  # (next time)

    plugin.every(60, minute)
