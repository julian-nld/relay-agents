"""Any program that prints the bridge's uniform events as JSON lines.

This is the escape hatch for adding agents Relay doesn't know: wrap them in a
small script that prints events like {"type": "text", "delta": "..."} (see
opencode_manager/bridge/events.py) and describe how to call it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Iterator

from opencode_manager.bridge.process import StderrTail, stop_process
from relay.providers.base import (
    Context,
    Event,
    Field,
    Provider,
    ProviderError,
    ProviderRun,
    TemplateError,
)


class CommandProvider(Provider):
    KIND = "command"
    LABEL = "Command (uniform events)"
    DESCRIPTION = (
        "Runs a program that prints uniform events as JSON lines, one per line. "
        "Arguments may use {prompt}, {session}, {model} and {directory}. Its first "
        "event should be {\"type\": \"session\", \"session\": \"…\"}; that id is passed "
        "as {session} to resume_command on the next turn."
    )
    TOOLS = True
    CONFINED = False  # (the program runs on this machine, as the user set it up: not for AgentOS's agents)
    FIELDS = (
        Field("command", "string-list", "Program and arguments for a new session.",
              required=True),
        Field("resume_command", "string-list",
              "Program and arguments to continue a session (default: command)."),
        Field("prompt_via_stdin", "boolean",
              "Send the prompt on stdin instead of as {prompt} (default: true)."),
        Field("env", "object", "Extra environment variables."),
        Field("models", "model-list", "Models to offer (passed as {model})."),
    )
    EXAMPLE = {
        "id": "claude-via-bridge",
        "name": "Claude Code (via opencode-manager bridge)",
        "kind": "command",
        "options": {
            "command": ["opencode-manager", "stream", "-b", "claude-code", "-d", "{directory}"],
            "resume_command": ["opencode-manager", "stream", "-b", "claude-code",
                               "-d", "{directory}", "-s", "{session}"],
        },
    }

    def validate(self) -> None:
        super().validate()
        if not self.options["command"]:
            raise TemplateError("Option 'command' must name a program")
        env = self.options.get("env", {})
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            raise TemplateError("Option 'env' must map names to strings")

    def check(self) -> str:
        program = self.options["command"][0]
        if shutil.which(program) is None:
            raise ProviderError(f"{program!r} not found")
        return f"{program} is available"

    def start(self, context: Context, prompt: str) -> ProviderRun:
        resume = context.provider_session is not None
        template = (self.options.get("resume_command") if resume else None) or self.options["command"]
        via_stdin = self.options.get("prompt_via_stdin", True)
        # Without a session to resume, the agent gets the conversation so far.
        prompt = context.agent_prompt(prompt, fresh=not resume).prompt
        values = {
            "{prompt}": prompt,
            "{session}": context.provider_session or "",
            "{model}": context.model or "",
            "{directory}": str(context.directory),
        }
        argv = [_fill(arg, values) for arg in template]
        if shutil.which(argv[0]) is None:
            raise ProviderError(f"{argv[0]!r} not found")
        mode = context.agent_mode
        env = {**os.environ, **self.options.get("env", {}),
               "RELAY_ATTACHMENTS": json.dumps([str(a.path) for a in context.attachments]),
               # The mode: how the agent should work (see relay.modes).
               "RELAY_MODE": mode.id if mode else "build",
               "RELAY_MODE_ACCESS": mode.access if mode else "edit",
               "RELAY_MODE_INSTRUCTIONS": mode.instructions if mode else "",
               "RELAY_EFFORT": context.effort or ""}
        if context.tools_path:
            env["RELAY_TOOLS"] = str(context.tools_path)  # Relay's tools script: subagents
        if context.mcp_url:
            env["RELAY_MCP_URL"] = context.mcp_url  # Relay's JSON-RPC endpoint
        if context.shell:
            env["SHELL"] = str(context.shell)  # its commands run in the folder's container
        return _CommandRun(argv, context, prompt if via_stdin else None, env)


def _fill(arg: str, values: dict[str, str]) -> str:
    # One pass, so placeholders inside substituted text (e.g. a prompt that
    # mentions "{directory}") are left alone.
    pattern = "|".join(re.escape(p) for p in values)
    return re.sub(pattern, lambda m: values[m.group(0)], arg)


class _CommandRun(ProviderRun):
    def __init__(self, argv: list[str], context: Context, stdin: str | None, env) -> None:
        self.argv = argv
        self.context = context
        self.stdin = stdin
        self.env = env
        self.process: subprocess.Popen[str] | None = None
        self.stopped = False

    def events(self) -> Iterator[Event]:
        if self.stopped:
            yield {"type": "done", "status": "interrupted"}
            return
        self.process = subprocess.Popen(
            self.argv, cwd=self.context.directory, env=self.env,
            stdin=subprocess.PIPE if self.stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
        )
        stderr = StderrTail(self.process.stderr)
        if self.stopped:  # interrupted while the process was starting
            stop_process(self.process)
        if self.stdin is not None:
            try:
                self.process.stdin.write(self.stdin)
                self.process.stdin.close()
            except BrokenPipeError:
                pass
        done = False
        for line in self.process.stdout:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and isinstance(event.get("type"), str):
                done = done or event["type"] == "done"
                yield event
        self.process.wait()
        if not done:
            if self.stopped:
                yield {"type": "done", "status": "interrupted"}
                return
            message = stderr.last() or f"exited with code {self.process.returncode}"
            yield {"type": "error", "message": message}
            yield {"type": "done", "status": "failed"}

    def interrupt(self) -> None:
        # If the process doesn't exist yet, events() sees the flag and stops it.
        self.stopped = True
        stop_process(self.process)
