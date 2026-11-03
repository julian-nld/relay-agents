"""Agents that speak the Agent Client Protocol (ACP), and GitHub Copilot through it.

ACP (agentclientprotocol.com) is JSON-RPC over the agent's stdin and
stdout: the client starts a session in a folder, sends prompts, and gets
the agent's text, thinking and tool calls as `session/update`
notifications. The agent asks the client before it runs a tool
(`session/request_permission`), and Relay answers itself (nobody is asked):
its commands run in a container (relay.containers, through $SHELL), so they're
approved unless the guard refuses them (relay.guard); its own file changes
are approved in the project only.

Each run starts the agent afresh and continues its session with
`session/load` (which replays the session's history; that is skipped).
GitHub Copilot's CLI speaks it with `copilot --acp`; so do other agents,
e.g. `gemini --experimental-acp`, added as kind "acp" with their command.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from opencode_manager import paths
from opencode_manager.bridge.billing import API, SUBSCRIPTION
from opencode_manager.bridge.process import StderrTail, stop_process
from relay import guard, limits
from relay.containers import SCRATCH
from relay.providers.base import (
    Context,
    Event,
    Field,
    Provider,
    ProviderError,
    ProviderRun,
    TemplateError,
    inside,
    unreadable,
)

PROTOCOL = 1
START_TIMEOUT = 60  # seconds for the agent to answer initialize and open a session
# Tool kinds that change files (ACP's ToolKind).
CHANGING_KINDS = {"edit", "delete", "move"}


class AcpProvider(Provider):
    KIND = "acp"
    LABEL = "ACP agent"
    DESCRIPTION = (
        "Any coding agent that speaks the Agent Client Protocol over stdio, e.g. `gemini "
        "--experimental-acp`. Its commands run in a container; Relay answers its permission requests itself."
    )
    TOOLS = True
    FORKS = "none"
    FIELDS = (
        Field("command", "string-list", "The agent's program and arguments to speak ACP.", required=True),
        Field("models", "model-list", "Models to offer."),
        Field("model_argument", "string", "The option that picks a model, e.g. --model (default: none)."),
        Field("effort_argument", "string", "The option that sets the reasoning effort, if any."),
        Field("efforts", "string-list", "The efforts that option takes, e.g. [\"low\", \"medium\", \"high\"]."),
        Field("env", "object", "Extra environment variables."),
    )
    EXAMPLE = {
        "id": "gemini",
        "name": "Gemini CLI",
        "kind": "acp",
        "options": {"command": ["gemini", "--experimental-acp"], "model_argument": "--model",
                    "models": ["", "gemini-2.5-pro", "gemini-2.5-flash"]},
    }

    def validate(self) -> None:
        super().validate()
        if "command" in {f.name for f in self.FIELDS if f.required} and not self.options.get("command"):
            raise TemplateError("Option 'command' must name a program")
        env = self.options.get("env", {})
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            raise TemplateError("Option 'env' must map names to strings")

    # What the kind decides

    def argv(self, context: Context) -> list[str]:
        argv = list(self.options["command"])
        if context.model and self.options.get("model_argument"):
            argv += [self.options["model_argument"], context.model]
        if context.effort and self.options.get("effort_argument") and context.effort in self.efforts(context.model):
            argv += [self.options["effort_argument"], context.effort]
        return argv

    def env(self) -> dict[str, str]:
        return dict(self.options.get("env") or {})

    def efforts(self, model: str | None) -> list[str]:
        return list(self.options.get("efforts") or [])

    def billing(self, model: str | None) -> str | None:
        return None

    def program(self) -> str:
        return self.options["command"][0]

    def check(self) -> str:
        program = self.program()
        if shutil.which(program) is None and not os.path.isfile(program):
            raise ProviderError(f"{program!r} not found")
        started = time.monotonic()
        with _Agent([*self.argv(Context("check", paths.host_cache_dir(), "chat", None, None))], None,
                    self.env()) as agent:
            info = agent.request("initialize", {"protocolVersion": PROTOCOL, "clientCapabilities": {}},
                                 timeout=START_TIMEOUT)
            # Answering is one thing; a session is where a login or a plan that doesn't allow it shows.
            folder = paths.host_cache_dir() / "acp-check"
            folder.mkdir(parents=True, exist_ok=True)
            try:
                agent.request("session/new", {"cwd": str(folder), "mcpServers": []}, timeout=START_TIMEOUT)
            except ProviderError as exc:
                raise ProviderError(f"It answers, but can't start a session: {str(exc)[:300]}") from exc
        name = (info.get("agentInfo") or {}).get("title") or (info.get("agentInfo") or {}).get("name") or program
        version = (info.get("agentInfo") or {}).get("version")
        return f"{name}{f' {version}' if version else ''} works (answered in {time.monotonic() - started:.1f} s)"

    def start(self, context: Context, prompt: str) -> ProviderRun:
        program = self.program()
        if shutil.which(program) is None and not os.path.isfile(program):
            raise ProviderError(f"{program!r} not found")
        return _AcpRun(self, context, prompt)

    def judge(self, action: str, detail: Any, folder: Path | None,
              reads: list[Path] | None = None) -> tuple[str, str]:
        """Its permission requests ({"commands", "paths"}): commands run in a container
        (anything goes, but what the guard refuses); files change only in the project, and
        are read only in `reads` when that's given."""
        detail = detail if isinstance(detail, dict) else {}
        blocked = next(filter(None, map(guard.check, detail.get("commands") or [])), None)
        if blocked:
            return "deny", f"Relay blocked this command: {blocked}"
        paths = detail.get("paths") or []
        if action in CHANGING_KINDS and folder and any(not inside(Path(folder) / p, folder) for p in paths):
            return "deny", f"Only files in the project folder ({folder}) can be changed."
        refused = unreadable(paths, reads, Path(folder) if folder else None)
        return ("deny", refused) if refused else ("allow", "")


class CopilotProvider(AcpProvider):
    KIND = "copilot"
    LABEL = "GitHub Copilot"
    DESCRIPTION = (
        "Runs the GitHub Copilot CLI (`copilot --acp`) with your Copilot subscription (sign in once with "
        "`copilot login`). Its commands run in a container; Relay answers its permission requests itself."
    )
    PROGRAMS = ("copilot",)
    SIGN_IN = "copilot login"
    FIELDS = (
        Field("binary", "string", "Path to the copilot executable (default: copilot on PATH)."),
        Field("models", "model-list", "Models to offer, e.g. [\"\", \"auto\", \"claude-sonnet-4.5\", \"gpt-5\"]; "
              "\"\" is Copilot's default."),
        Field("env", "object", "Extra environment variables, e.g. COPILOT_HOME, or COPILOT_PROVIDER_BASE_URL "
              "to use your own model provider."),
    )
    EXAMPLE = {
        "id": "copilot",
        "name": "GitHub Copilot",
        "kind": "copilot",
        "options": {"models": [{"id": "", "name": "Default"}, {"id": "auto", "name": "Auto"}]},
    }
    EFFORTS = ("low", "medium", "high", "xhigh", "max")

    def validate(self) -> None:
        Provider.validate(self)  # (no command to check: Copilot's own)
        env = self.options.get("env", {})
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            raise TemplateError("Option 'env' must map names to strings")

    def program(self) -> str:
        return self.options.get("binary") or paths.program("copilot")

    def detached(self, command: str) -> bool:
        return command.startswith("__copilot_pid_path=")  # (the wrapper of `detach: true`)

    def env(self) -> dict[str, str]:
        # Its detached commands (`detach: true`, what its own instructions say to use for a server)
        # run through a wrapper that writes their pid and exit code to files in its temporary folder
        # for it to read: one its commands' WorkBox sees too.
        SCRATCH.mkdir(exist_ok=True)
        return {"TMPDIR": str(SCRATCH), **super().env()}

    def argv(self, context: Context) -> list[str]:
        # The full path: in its box the machine's PATH isn't there.
        argv = [shutil.which(self.program()) or self.program(), "--acp", "--no-auto-update"]
        if context.model:
            argv += ["--model", context.model]
        if context.effort in self.EFFORTS:
            argv += ["--reasoning-effort", context.effort]
        return argv

    def efforts(self, model: str | None) -> list[str]:
        return list(self.EFFORTS)

    def box_key(self) -> str | None:
        return self.template.id

    def box_mounts(self) -> list[tuple[Path, bool]]:
        """Copilot's program (as it's called: a link brings its target), its login and settings
        (COPILOT_HOME, ~/.copilot) and the package it unpacks itself into (~/.cache/copilot)."""
        program = shutil.which(self.program()) or self.program()
        home = Path(self.env().get("COPILOT_HOME") or Path.home() / ".copilot")
        return [(Path(program), False), (home, True), (Path.home() / ".cache" / "copilot", True)]

    def limits(self) -> dict[str, Any] | None:
        """Its plan's monthly quotas (premium requests, chat messages), as Copilot asks for them, with
        the login `copilot login` keeps (COPILOT_HOME, ~/.copilot)."""
        if self.env().get("COPILOT_PROVIDER_BASE_URL"):
            return None  # (its own model provider: no Copilot quota)
        home = Path(self.env().get("COPILOT_HOME") or Path.home() / ".copilot")
        try:
            text = (home / "config.json").read_text()
            config = json.loads(re.sub(r"^\s*//.*$", "", text, flags=re.M))  # (it has comments)
            user = config.get("lastLoggedInUser") or {}
            token = config["authTokens"][f"{user['host']}:{user['login']}"]["token"]
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None
        account = f"copilot:{user['host']}:{user['login']}"
        if user["host"].rstrip("/") != "https://github.com":
            return {"account": account, "error": "only github.com tells its quotas this way"}
        answer, why = limits.fetch_json(limits.COPILOT_USER, {"Authorization": f"token {token}"})
        if answer is None:
            return {"account": account, "error": why}
        return {"account": account, "service": "Copilot",
                "plan": limits.plan_name(answer.get("access_type_sku") or answer.get("copilot_plan")),
                "windows": limits.copilot_windows(answer), "source": "provider", "checked": time.time()}

    def billing(self, model: str | None) -> str | None:
        # With its own model provider (BYOK) it's that provider's; else Copilot's plan.
        return API if self.env().get("COPILOT_PROVIDER_BASE_URL") else SUBSCRIPTION


class _Agent:
    """An ACP agent process: JSON-RPC requests out, messages in (on a reader thread)."""

    def __init__(self, argv: list[str], cwd, env: dict[str, str], wrap=None) -> None:
        if wrap is not None:  # in the provider's box (relay.boxes)
            argv, env = wrap(argv, cwd or Path.home(), env), {}
        try:
            self.process = subprocess.Popen(
                argv, cwd=cwd, env={**os.environ, **env}, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", start_new_session=True)
        except OSError as exc:
            raise ProviderError(f"Couldn't start {argv[0]}: {exc}") from exc
        self.stderr = StderrTail(self.process.stderr)
        self.inbox: queue.Queue = queue.Queue()
        self.lock = threading.Lock()
        self.next_id = 0
        threading.Thread(target=self._read, daemon=True).start()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _read(self) -> None:
        for line in self.process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self.inbox.put(json.loads(line))
            except json.JSONDecodeError:
                continue  # not protocol: a stray log line
        self.inbox.put(None)  # it ended

    def send(self, message: dict[str, Any]) -> None:
        with self.lock:
            try:
                self.process.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
                self.process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass  # it's gone; the reader notices

    def call(self, method: str, params: dict[str, Any]) -> int:
        with self.lock:
            self.next_id += 1
            request_id = self.next_id
        self.send({"id": request_id, "method": method, "params": params})
        return request_id

    def request(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        """Call and wait for the answer, dropping anything else that comes meanwhile."""
        request_id = self.call(method, params)
        deadline = time.monotonic() + timeout
        while True:
            try:
                message = self.inbox.get(timeout=max(0.1, deadline - time.monotonic()))
            except queue.Empty as exc:
                raise ProviderError(f"The agent didn't answer {method} in time") from exc
            if message is None:
                raise ProviderError(self._ended())
            if message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    raise ProviderError(_error_text(message["error"]))
                return message.get("result") or {}
            if "method" in message and "id" in message:  # a request of its own: nobody to ask yet
                self.send({"id": message["id"], "error": {"code": -32601, "message": "Not now"}})

    def _ended(self) -> str:
        self.stderr.thread.join(1)
        tail = " / ".join(list(self.stderr.lines)[-3:])
        return f"The agent stopped{f': {tail[-500:]}' if tail else ''}"

    def close(self) -> None:
        if self.process.poll() is None:
            try:
                self.process.stdin.close()
            except OSError:
                pass
            stop_process(self.process)


class _AcpRun(ProviderRun):
    def __init__(self, provider: AcpProvider, context: Context, prompt: str) -> None:
        self.provider = provider
        self.context = context
        self.prompt = prompt
        self.sent = ""  # what the agent was sent
        self.agent: _Agent | None = None
        self.session: str | None = None
        self.stopped = threading.Event()

    def _choose(self, rpc_id: int, options: list[dict[str, Any]], approve: bool) -> None:
        kinds = ("allow_once", "allow_always") if approve else ("reject_once", "reject_always")
        chosen = next((o for kind in kinds for o in options if o.get("kind") == kind), None)
        if chosen is None:
            outcome = {"outcome": "cancelled"}
        else:
            outcome = {"outcome": "selected", "optionId": chosen["optionId"]}
        self.agent.send({"id": rpc_id, "result": {"outcome": outcome}})

    def interrupt(self) -> None:
        self.stopped.set()
        if self.agent and self.session:
            self.agent.send({"method": "session/cancel", "params": {"sessionId": self.session}})
            threading.Timer(5, self._kill).start()
        elif self.agent:
            self._kill()

    def _kill(self) -> None:
        if self.agent:
            self.agent.close()

    # The run

    def events(self) -> Iterator[Event]:
        context = self.context
        try:
            # Its commands go to the folder's container through Relay's shell: as $SHELL, and as the
            # `bash` an agent finds first on its PATH. (Copilot runs /bin/bash itself: neither helps.)
            if context.box is not None:
                # In its box, whose shells are Relay's: commands go to the WorkBox anyway.
                self.agent = _Agent(self.provider.argv(context), str(context.directory),
                                    {**self.provider.env(), "RELAY_FOLDER": str(context.directory),
                                     **({"RELAY_TOOLS": str(context.tools_path)} if context.tools_path else {})},
                                    context.box.wrap)
            else:
                shell = {"SHELL": str(context.shell),
                         "PATH": f"{context.shell.parent}{os.pathsep}"
                                 f"{os.environ.get('PATH', '')}"} if context.shell else {}
                tools = {"RELAY_TOOLS": str(context.tools_path)} if context.tools_path else {}
                self.agent = _Agent(self.provider.argv(context), str(context.directory),
                                    {**self.provider.env(), **shell, **tools})
        except ProviderError as exc:
            yield {"type": "error", "message": str(exc)}
            yield {"type": "done", "status": "failed"}
            return
        try:
            yield from self._run()
        finally:
            self.agent.close()

    def _run(self) -> Iterator[Event]:
        context, agent = self.context, self.agent
        try:
            info = agent.request("initialize", {"protocolVersion": PROTOCOL, "clientCapabilities": {
                "fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}}, timeout=START_TIMEOUT)
            capabilities = info.get("agentCapabilities") or {}
            where = {"cwd": str(context.directory), "mcpServers": []}
            session, fresh = None, True
            if context.provider_session and capabilities.get("loadSession"):
                try:
                    # Its history is replayed as updates: request() drops them.
                    opened = agent.request("session/load", {"sessionId": context.provider_session, **where},
                                           timeout=START_TIMEOUT)
                    session, fresh = context.provider_session, False
                except ProviderError:
                    opened = None  # gone: a new session, with the conversation handed over
            if session is None:
                opened = agent.request("session/new", where, timeout=START_TIMEOUT)
                session = opened.get("sessionId")
            self.session = session
            yield {"type": "session", "session": session}
        except ProviderError as exc:
            yield {"type": "error", "message": str(exc)}
            yield {"type": "done", "status": "failed"}
            return
        if self.stopped.is_set():
            yield {"type": "done", "status": "interrupted"}
            return
        text = context.agent_prompt(self.prompt, fresh=fresh, instructions=True).prompt
        self.sent = text
        blocks = [{"type": "text", "text": text}]
        prompt_id = agent.call("session/prompt", {"sessionId": session, "prompt": blocks})
        yield {"type": "step", "agent": None, "model": context.model}
        tools: dict[str, str] = {}
        while True:
            message = agent.inbox.get()
            if message is None:
                if self.stopped.is_set():
                    yield {"type": "done", "status": "interrupted"}
                else:
                    yield {"type": "error", "message": agent._ended()}
                    yield {"type": "done", "status": "failed"}
                return
            method = message.get("method")
            if method == "session/update":
                yield from self._update((message.get("params") or {}).get("update") or {}, tools)
            elif method == "session/request_permission":
                yield from self._permission(message)
            elif method and "id" in message:
                agent.send({"id": message["id"], "error": {"code": -32601, "message": f"{method} isn't supported"}})
            elif message.get("id") == prompt_id:
                if "error" in message:
                    yield {"type": "error", "message": _error_text(message["error"])}
                    yield {"type": "done", "status": "interrupted" if self.stopped.is_set() else "failed"}
                    return
                result = message.get("result") or {}
                usage = result.get("usage") or {}
                yield {"type": "usage", "tokens": {"input_tokens": usage.get("inputTokens"),
                                                   "output_tokens": usage.get("outputTokens")} if usage else None,
                       "cost": None, "billing": self.provider.billing(context.model)}
                reason = result.get("stopReason")
                if reason == "cancelled":
                    yield {"type": "done", "status": "interrupted"}
                elif reason == "refusal":
                    yield {"type": "error", "message": "The agent refused to go on"}
                    yield {"type": "done", "status": "failed"}
                else:
                    yield {"type": "done", "status": "succeeded"}
                return

    def _update(self, update: dict[str, Any], tools: dict[str, str]) -> Iterator[Event]:
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            text = _text(update.get("content"))
            if text:
                yield {"type": "text", "delta": text}
        elif kind == "agent_thought_chunk":
            text = _text(update.get("content"))
            if text:
                yield {"type": "reasoning", "delta": text}
        elif kind == "tool_call":
            call_id = str(update.get("toolCallId"))
            name = update.get("kind") or "tool"
            name = {"execute": "bash", "edit": "edit", "read": "read", "search": "search", "fetch": "webfetch",
                    "delete": "delete", "move": "move", "think": "think"}.get(name, name)
            tools[call_id] = name
            detail = update.get("rawInput") if isinstance(update.get("rawInput"), dict) else {}
            yield {"type": "tool_call", "id": call_id, "name": name,
                   "input": {**detail, "title": update.get("title")} if update.get("title") else detail}
        elif kind == "tool_call_update" and update.get("status") in ("completed", "failed"):
            call_id = str(update.get("toolCallId"))
            output = "".join(_text(c.get("content")) for c in update.get("content") or [] if isinstance(c, dict))
            yield {"type": "tool_result", "id": call_id, "name": tools.get(call_id, "tool"),
                   "ok": update.get("status") == "completed", "output": output}
        elif kind == "session_info_update" and update.get("title") and not _starts(self.sent, update["title"]):
            # (Copilot's is the start of the message, Relay's note and all: Relay's own title is better)
            yield {"type": "title", "title": update["title"]}

    def _permission(self, message: dict[str, Any]) -> Iterator[Event]:
        params = message.get("params") or {}
        call = params.get("toolCall") or {}
        options = params.get("options") or []
        detail = call.get("rawInput") if isinstance(call.get("rawInput"), dict) else {}
        commands = [str(c) for c in detail.get("commands") or ([detail["command"]] if detail.get("command") else [])]
        action = call.get("kind") or call.get("title") or "tool"
        request_id = str(call.get("toolCallId") or message.get("id"))
        base = {"type": "permission", "id": request_id, "action": action,
                "resources": commands or [call.get("title") or json.dumps(detail)[:200]]}
        paths = [str(where.get("path")) for where in call.get("locations") or []
                 if isinstance(where, dict) and where.get("path")]
        verdict, why = self.provider.judge(action, {"commands": commands, "paths": paths}, self.context.directory,
                                           self.context.reads())
        self._choose(message["id"], options, verdict == "allow")
        yield {**base, "decision": "once"} if verdict == "allow" else {**base, "decision": "denied", "outcome": why}

def _starts(text: str, title: str) -> bool:
    """Whether a "title" is merely the start of the message (an ellipsis at its end aside)."""
    title = " ".join(title.split()).rstrip(".…").strip()
    return bool(title) and " ".join(text.split()).startswith(title)


def _text(content: Any) -> str:
    if isinstance(content, dict):
        return str(content.get("text") or "") if content.get("type", "text") == "text" else ""
    if isinstance(content, list):
        return "".join(_text(c) for c in content)
    return ""


def _error_text(error: Any) -> str:
    if isinstance(error, dict):
        detail = error.get("data")
        message = str(error.get("message") or "The agent failed")
        if isinstance(detail, (str, dict)) and detail:
            message += f": {detail if isinstance(detail, str) else json.dumps(detail)[:300]}"
        return message
    return str(error)
