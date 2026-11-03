"""opencode, through an opencode-manager profile's service and the bridge's OpencodeBackend.

Its support layer is this folder and opencode_manager/bridge/opencode_backend.py (which opencode-manager's
scripting uses too).
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import shutil
import sqlite3
import sys
import time
import urllib.parse
from pathlib import Path

from opencode_manager import opencode, paths, ports
from opencode_manager import settings as manager_settings
from opencode_manager import state as manager_state
from opencode_manager.bridge import opencode_browser
from opencode_manager.bridge.billing import FREE, local_host
from opencode_manager.bridge.http import Client, HttpError
from opencode_manager.bridge.opencode_backend import OpencodeBackend, model_name
from opencode_manager.profiles import DEFAULT_PROFILE, ProfileError, ProfileStore
from relay import guard, limits
from relay.providers.base import (
    Context,
    Field,
    Provider,
    ProviderError,
    ProviderRun,
    TemplateError,
    inside,
    unreadable,
)
from relay.providers.bridge_run import BridgeRun


def opencode_profiles() -> list[str]:
    """Names of the opencode-manager profiles on this install."""
    settings = manager_settings.load()
    store = ProfileStore(paths.profiles_dir(), settings.base_port, own_default=paths.own_opencode())
    return [p.name for p in store.list()]


def active_opencode_profile() -> str:
    """The profile opencode-manager is using (the one chosen in its sidebar)."""
    settings = manager_settings.load()
    store = ProfileStore(paths.profiles_dir(), settings.base_port, own_default=paths.own_opencode())
    name = manager_state.load().active_profile
    return name if store.exists(name) else DEFAULT_PROFILE


def opencode_template(profile: str) -> dict:
    """A provider template pinned to one opencode-manager profile."""
    return {
        "id": "opencode-" + profile.replace("_", "-"),
        "name": f"opencode · {profile}",
        "kind": "opencode",
        "options": {"profile": profile},
    }


def _openai_login(folder: Path) -> dict | None:
    """opencode's OpenAI sign-in with a ChatGPT plan (an OAuth login, not an API key), in its data
    folder: opencode.db's credential table (opencode 2), else auth.json. {access, expires (Unix time),
    account_id, plan}; None when there's none."""
    found = None
    try:
        db = sqlite3.connect(f"file:{folder / 'opencode.db'}?mode=ro", uri=True)
        try:
            row = db.execute("SELECT value FROM credential WHERE integration_id = 'openai' AND active = 1 "
                             "ORDER BY time_updated DESC").fetchone()
        finally:
            db.close()
        if row:
            value = json.loads(row[0])
            found = {"access": value.get("access"), "expires": value.get("expires"),
                     "account_id": (value.get("metadata") or {}).get("accountID")}
    except (sqlite3.Error, ValueError, TypeError, AttributeError):
        pass
    if found is None:
        try:
            value = json.loads((folder / "auth.json").read_text()).get("openai") or {}
        except (OSError, ValueError, AttributeError):
            value = {}
        if value.get("type") == "oauth":
            found = {"access": value.get("access"), "expires": value.get("expires"),
                     "account_id": value.get("accountId")}
    if not found or not isinstance(found["access"], str) or not isinstance(found["expires"], (int, float)):
        return None
    claims = {}
    try:  # the access token's own claims: the account and plan, when the login didn't keep them
        claims = json.loads(base64.urlsafe_b64decode(found["access"].split(".")[1] + "==")).get(
            "https://api.openai.com/auth") or {}
    except (IndexError, ValueError, AttributeError):
        pass
    return {"access": found["access"], "expires": found["expires"] / (1000 if found["expires"] > 1e11 else 1),
            "account_id": found["account_id"] or claims.get("chatgpt_account_id"),
            "plan": claims.get("chatgpt_plan_type")}


def _choose_model(backend: OpencodeBackend, session_id: str, session: dict, context: Context) -> None:
    """Give the session the chosen model (picked halfway the conversation, too)
    and effort (a variant of the model, where it has them)."""
    current = session.get("model") or {}
    model = context.model or model_name(current)
    variant = context.effort or "default"
    if not model:
        return
    # Switching accepts unknown variants; opencode rejects them only when prompted.
    # Check in the session's location before carrying effort to another model.
    if variant != "default" and variant not in backend.variants(context.directory, model):
        variant = "default"
    if model_name(current) == model and (current.get("variant") or "default") == variant:
        return
    backend.set_model(session_id, model, variant)


# File changes are put to Relay (OpencodeBackend.set_permissions), which keeps them to the
# project; its commands run in a container (relay/containers.py).
CHANGING = ("edit", "write", "patch")
ASK_RELAY = [{"action": action, "resource": "*", "effect": "ask"} for action in CHANGING]

# What opencode says of its models (who pays, which server), per provider.
_MODELS_INFO: dict[str, tuple[float, dict]] = {}
MODELS_INFO_FOR = 300.0  # seconds


class OpencodeProvider(Provider):
    KIND = "opencode"
    FORKS = "any"
    PROGRAMS = ("opencode",)
    SUPPORT_FILES = ("opencode_manager/bridge/opencode_backend.py",)
    LABEL = "opencode"
    DESCRIPTION = (
        "Runs opencode through an opencode-manager profile, so every provider "
        "connected in that profile works (ChatGPT account, OpenRouter, local models, …). "
        "Its commands run in a container; Relay answers its permission requests itself."
    )
    TOOLS = True
    SHELL_HINT = "Run opencode, and use /connect in it to add OpenRouter, OpenAI and others to this profile."
    FIELDS = (
        Field("profile", "string",
              "opencode-manager profile to use; leave it out to follow the active profile "
              "(the one selected in opencode-manager's sidebar)."),
        Field("models", "model-list",
              "Models to offer as provider/model ids; default: those opencode lists."),
    )
    EXAMPLE = {
        "id": "opencode",
        "name": "opencode",
        "kind": "opencode",
        "options": {},
    }

    def detail(self) -> str | None:
        if self.options.get("profile"):
            return f"profile {self.options['profile']}"
        return f"follows the active profile (now: {active_opencode_profile()})"

    def profile_name(self) -> str:
        """The profile this provider uses right now."""
        return self.options.get("profile") or active_opencode_profile()

    def _manager(self):
        settings = manager_settings.load()
        store = ProfileStore(paths.profiles_dir(), settings.base_port, is_port_free=ports.is_free,
                             own_default=paths.own_opencode())
        try:
            profile = store.get(self.profile_name())
        except ProfileError as exc:
            raise ProviderError(str(exc)) from exc
        return settings, store, profile

    def box_key(self) -> str | None:
        return "opencode-manager"  # one box for all of opencode-manager's services

    def box_mounts(self) -> list[tuple[Path, bool]]:
        """opencode's program, opencode-manager's profiles (each a config and data folder of
        its own), and the default profile's folders (opencode's usual ones) unless this Relay has
        an opencode of its own (paths.own_opencode: then its default is one of those profiles)."""
        settings = manager_settings.load()
        program = Path(shutil.which(opencode.binary(settings)) or opencode.binary(settings))
        home = Path.home()
        mounts = [(program, False), (paths.profiles_dir(), True)]
        if paths.own_opencode():
            return mounts
        for folder in (home / ".config" / "opencode", home / ".local" / "share" / "opencode",
                       home / ".local" / "state" / "opencode", home / ".cache" / "opencode"):
            folder.mkdir(parents=True, exist_ok=True)
            mounts.append((folder, True))
        return mounts

    def env(self) -> dict[str, str]:
        """Its profile's folders, and temporary files where its service has them."""
        try:
            return {**self._manager()[2].env(), "TMPDIR": SHARED_TMP}
        except ProviderError:
            return {"TMPDIR": SHARED_TMP}

    def shell_ready(self) -> None:
        """Its service running as Relay's, for opencode in the shell to attach to: one started from the shell would
        end with it, and a run using it too. One that won't start may be why the shell's opened. A profile that isn't
        there stops it: opencode in the shell would be another profile's."""
        self._manager()
        with contextlib.suppress(ProviderError):
            self._backend()

    def prepare_new(self) -> None:
        """A profile its template names that isn't there yet: Relay's pages have no other way to make one."""
        name = self.options.get("profile")
        settings = manager_settings.load()
        store = ProfileStore(paths.profiles_dir(), settings.base_port, is_port_free=ports.is_free,
                             own_default=paths.own_opencode())
        try:
            if name and not store.exists(name):
                store.create(name)
        except ProfileError as exc:
            raise ProviderError(f"The profile {name!r}: {exc}") from exc

    def _launcher(self, cmd: list[str], env: dict[str, str]) -> list[str]:
        """opencode-manager's opencode commands, and so the services they start, run in the box."""
        from relay.boxes import BoxLaunch
        launch = BoxLaunch(self.boxes.ensure(self.box_key(), self.box_mounts()))
        return launch.wrap([shutil.which(cmd[0]) or cmd[0], *cmd[1:]], Path.home(), env)

    def _backend(self, judge=None) -> OpencodeBackend:
        settings, store, profile = self._manager()
        Path(SHARED_TMP).mkdir(exist_ok=True)
        if self.boxes is not None:
            # In the box (its shells are Relay's), with temporary files (e.g. the browser's
            # screenshots) where the WorkBoxes see them too.
            opencode.LAUNCHER = self._launcher
            env, marks = {"TMPDIR": SHARED_TMP}, {"TMPDIR": SHARED_TMP, "RELAY_BOX": "1"}
        else:
            shell = _SHELL.get("shared")
            # Its commands run with Relay's shell (in the folder's container), and its temporary
            # files go where the containers see them too.
            env = marks = {"SHELL": str(shell), "TMPDIR": SHARED_TMP} if shell else None
        try:
            url, password = opencode.ensure_service(settings, store, profile, env=env)
            if marks and any(any(found.get(k) != v for k, v in marks.items())
                             for _, found in opencode.service_processes(profile)):
                # Started otherwise (before Relay, or by someone else): it starts over, as Relay's.
                url, password = opencode.start_service_over(settings, store, profile, env)
        except opencode.OpencodeError as exc:
            raise ProviderError(str(exc)) from exc
        return OpencodeBackend(Client(url, "opencode", password), judge=judge)

    # opencode loads its model catalogue a few seconds after the service starts.
    MODELS_WAIT = 15.0

    def models(self) -> list[dict[str, str]]:
        if self.options.get("models"):
            return super().models()
        default = [{"id": "", "name": "Default model"}]
        try:
            backend = self._backend()
        except ProviderError:
            return default
        # Without a location the API answers with empty results.
        location = "?directory=" + urllib.parse.quote(str(Path.home()))
        path = "/api/model" + location
        deadline = time.monotonic() + self.MODELS_WAIT
        while True:
            try:
                response = backend.client.request("GET", path, timeout=5)
            except HttpError as exc:
                if exc.status is not None:
                    return default  # a real answer (401, 404, …): waiting won't help
                response = None  # not reachable yet
            data = response.get("data") if isinstance(response, dict) else None
            if (isinstance(data, list) and data) or time.monotonic() > deadline:
                break
            time.sleep(0.5)
        if not isinstance(data, list):
            return default
        default = [{"id": "", "name": self._default_label(backend, location)}]
        listed = {}
        for model in data:
            # A model is addressed as providerID/id; modelID is the upstream
            # name, which variants such as "GPT-5.5 Fast" share with the base model.
            provider, model_id = model.get("providerID"), model.get("id")
            if provider and model_id and model.get("enabled", True) is not False:
                full = f"{provider}/{model_id}"
                listed[full] = {"id": full, "name": model.get("name") or full}
        return default + sorted(listed.values(), key=lambda m: m["name"].lower())

    def model_id(self, model: str | None) -> str | None:
        """opencode names a model with its provider ("opencode/big-pickle"). One given without it ("big-pickle", as
        a simulated user did: opencode said "Model unavailable: big-pickle/") is the one model of that name, if one."""
        if not model or "/" in model:
            return model
        named = [m["id"] for m in self.models() if m["id"].rsplit("/", 1)[-1] == model]
        return named[0] if len(named) == 1 else model

    @staticmethod
    def _default_label(backend: OpencodeBackend, location: str) -> str:
        """"Default · <model>" naming the model the profile's config selects."""
        try:
            response = backend.client.request("GET", "/api/model/default" + location, timeout=5)
        except HttpError:
            return "Default model"
        model = response.get("data") if isinstance(response, dict) else None
        name = model.get("name") if isinstance(model, dict) else None
        return f"Default · {name}" if name else "Default model"

    def check(self) -> str:
        if self.probing:
            settings, _, profile = self._manager()
            if not shutil.which(opencode.binary(settings)):
                raise ProviderError(f"{opencode.binary(settings)} isn't installed")
            try:
                opencode.require_service(settings)
            except opencode.OpencodeError as exc:
                raise ProviderError(str(exc)) from exc
            if opencode.serves(settings, profile):
                return f"opencode service for profile {profile.name!r} is running"
            return f"opencode service for profile {profile.name!r} isn't running; Relay starts it when needed"
        backend = self._backend()
        try:
            backend.client.request("GET", "/api/session?limit=1")
        except HttpError as exc:
            raise ProviderError(str(exc)) from exc
        return f"opencode service for profile {self._manager()[2].name!r} is running"

    def judge(self, action: str, detail, folder: Path | None, reads: list[Path] | None = None) -> tuple[str, str]:
        """opencode's permission requests, answered by Relay: its commands run in a
        container (anything goes, but what the guard refuses); it changes files only in
        the project. Reading elsewhere (external_directory) is fine, unless only `reads` is."""
        resources = [str(r) for r in detail] if isinstance(detail, list) else []
        if action in ("shell", "bash"):
            blocked = next(filter(None, map(guard.check, resources)), None)
            return ("deny", f"Relay blocked this command: {blocked}") if blocked else ("allow", "")
        if action in ("external_directory", "read"):
            refused = unreadable(resources, reads, folder)
            return ("deny", refused) if refused else ("allow", "")
        if action not in CHANGING:
            return "allow", ""
        # (paths come relative to the project)
        if folder and any(not inside(folder / r, folder) for r in resources):
            return "deny", f"Only files in the project folder ({folder}) can be changed."
        return "allow", ""

    def start(self, context: Context, prompt: str) -> ProviderRun:
        if context.shared_shell:
            _SHELL["shared"] = context.shared_shell
        folder = Path(context.directory)

        def judge(action: str, resources: list[str]) -> str | None:
            verdict, why = self.judge(action, resources, folder, context.reads())
            return why if verdict == "deny" else None

        backend = self._backend(judge)
        session_id = context.provider_session
        session = backend.get_session(session_id) if session_id else None
        if session is None:
            session_id = None  # new, or e.g. the profile's data was reset; start over
        forked = False
        try:
            if session_id is None and context.fork_from and backend.get_session(context.fork_from):
                session_id = backend.fork(context.fork_from, context.fork_before)
                session = backend.get_session(session_id) or {}
                forked = True
            new = session_id is None
            # Its build agent, also in read-only modes (whose instructions ask it not to change
            # anything): opencode's plan agent refuses even commands that only look.
            agent = "build"
            if new:
                session_id = backend.create_session(context.directory, model=context.model or None,
                                                    agent=agent)
                session = backend.get_session(session_id) or {}
            else:
                backend.set_agent(session_id, agent)
                where = (session.get("location") or {}).get("directory")
                if where and where != str(context.directory):
                    backend.move(session_id, context.directory)  # e.g. Relay moved to another machine
            _choose_model(backend, session_id, session, context)
            # opencode asks Relay before changing a file, whatever the profile's config says
            # (without one it allows everything unasked); Relay answers (judge()).
            backend.set_permissions(session_id, ASK_RELAY)
        except HttpError as exc:
            raise ProviderError(str(exc)) from exc
        if context.browser_argv:
            # opencode's browser tools want its desktop app: Relay's browser (Chromium in the
            # container) stands in for it. It stays attached to the session after this run.
            try:
                opencode_browser.attach(backend.client, session_id, Path(context.directory), context.browser_argv,
                                        context.browser_log)
            except OSError as exc:
                print(f"relay: no browser for opencode: {exc}", file=sys.stderr, flush=True)
        # opencode attaches the files to the message, but not every model reads
        # attached files; the paths in the prompt let the agent open them itself.
        files = [{"uri": a.path.as_uri(), "name": a.name} for a in context.attachments]
        options: dict = {"files": files} if files else {}
        name, _, arguments = prompt[1:].partition(" ") if prompt.startswith("/") else ("", "", "")
        if name and any(c["name"] == name for c in self._commands(backend, context.directory)):
            # One of opencode's commands (init, review, your own): run it as such.
            return BridgeRun(backend, session_id, arguments.strip(), new or forked, command=name, **options)
        # A fork carries the conversation itself; a fresh session gets it handed over.
        sent = context.agent_prompt(prompt, fresh=new, instructions=True)
        return BridgeRun(backend, session_id, sent.prompt, new or forked, **options)

    def abandon(self, session: str, directory: Path) -> None:
        # opencode's server outlives Relay, and a session it was prompted in keeps working.
        self._backend().interrupt(session)

    def limits(self) -> dict | None:
        """A ChatGPT plan's 5-hour and weekly windows, when its profile is signed in to OpenAI with
        one: asked as Codex's /status asks, with opencode's login (opencode renews it; Relay doesn't)."""
        try:
            _, _, profile = self._manager()
        except ProviderError:
            return None
        data = profile.data_home or Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
        login = _openai_login(data / "opencode")
        if login is None:
            return None
        account = f"chatgpt:{login['account_id'] or data}"  # (profiles signed in to the same account: one)
        if login["expires"] < time.time() + 30:
            return {"account": account, "error": "its OpenAI login has expired: opencode renews it at its next run"}
        headers = {"Authorization": f"Bearer {login['access']}"}
        if login["account_id"]:
            headers["ChatGPT-Account-Id"] = login["account_id"]
        answer, why = limits.fetch_json(limits.CHATGPT_USAGE, headers)
        if answer is None:
            return {"account": account, "error": why}
        return {"account": account, "service": "ChatGPT",
                "plan": limits.plan_name(answer.get("plan_type") or login["plan"]),
                "windows": limits.chatgpt_windows(answer), "source": "provider", "checked": time.time()}

    def billing(self, model: str | None) -> str | None:
        return self._model_info(model).get("billing")

    def local_server(self, model: str | None) -> str | None:
        return local_host(self._model_info(model).get("base_url"))

    def free_models(self) -> list[str]:
        return sorted(m for m, info in self._models_info().get("models", {}).items() if info["billing"] == FREE)

    def _model_info(self, model: str | None) -> dict:
        found = self._models_info()
        return found.get("models", {}).get(model or found.get("default") or "", {})

    def _models_info(self) -> dict:
        """opencode's word on its models (OpencodeBackend.models_info), kept for a few minutes."""
        key = f"{self.template.id}|{self.profile_name()}"  # (a provider can be set to another profile)
        found = _MODELS_INFO.get(key)
        if found and time.monotonic() - found[0] < MODELS_INFO_FOR:
            return found[1]
        try:
            info = self._backend().models_info(Path.home())
        except (ProviderError, HttpError, OSError, KeyError, TypeError, ValueError):
            return {}
        _MODELS_INFO[key] = (time.monotonic(), info)
        return info

    def efforts(self, model: str | None) -> list[str]:
        try:
            return self._backend().variants(Path.home(), model or None)
        except (ProviderError, HttpError, OSError):
            return []

    def model_efforts(self) -> dict[str, list[str]] | None:
        try:
            return self._backend().all_variants(Path.home())  # (each model has its own variants)
        except (ProviderError, HttpError, OSError, KeyError, TypeError):
            return None

    def commands(self, directory: Path) -> list[dict[str, str]]:
        try:
            return self._commands(self._backend(), directory)
        except (ProviderError, OSError):
            return []

    @staticmethod
    def _commands(backend: OpencodeBackend, directory: Path) -> list[dict[str, str]]:
        try:
            return backend.commands(directory)
        except (HttpError, KeyError, TypeError):
            return []

    def validate(self) -> None:
        super().validate()
        profile = self.options.get("profile")
        if profile is not None and not profile:
            raise TemplateError("Option 'profile' must not be empty")


# opencode's scratch folder, which relay/containers.py shares with the containers.
SHARED_TMP = "/tmp/opencode"

# Relay's shell for opencode's server (Containers.shared_shell), once a run gave it.
_SHELL: dict[str, Path] = {}
