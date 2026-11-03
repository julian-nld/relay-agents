"""Claude Code backend: runs `claude -p` in stream-json mode.

Each run is one `claude` process. New sessions get their ID up front
(--session-id), so the caller knows it before any output; later turns use
--resume, which Claude Code only finds from the same project directory.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from opencode_manager.bridge.billing import claude_billing
from opencode_manager.bridge.events import Event, event
from opencode_manager.bridge.process import StderrTail, stop_process

AFTER_RESULT = 30  # seconds a claude process may take to end after its result
# Claude Code's word for a model it doesn't know, or the login can't use (its advice is for its command line)
MODEL_ISSUE = re.compile(r"There's an issue with the selected model \((.*?)\)")
# Its word for a login that's gone (expired and not renewed, signed out): its advice ("Please run /login") is for its
# own terminal
LOGIN_ISSUE = re.compile(r"Failed to authenticate|OAuth (session|token)|Invalid API key|Please run /login", re.I)


class Translator:
    """Turns Claude Code stream-json messages into uniform events."""

    def __init__(self, agent: str | None = None) -> None:
        self.agent = agent
        self.tool_names: dict[str, str] = {}
        self.billing: str | None = None
        self.said_something = False  # any answer text streamed
        self.started = False  # this run's init seen
        self.early: dict[str, Any] | None = None  # a result before it (said if no other comes)
        self.done = False

    def translate(self, raw: dict[str, Any]) -> list[Event]:
        # Messages from inside a subagent (the Task tool) are its own work;
        # its outcome arrives as the Task tool's result.
        if raw.get("parent_tool_use_id"):
            return []
        kind = raw.get("type")
        if kind == "system" and raw.get("subtype") == "init":
            self.started = True
            self.billing = claude_billing(raw.get("apiKeySource"))
            return []
        if kind == "stream_event":
            return self._stream_event(raw.get("event", {}))
        if kind == "assistant":
            return self._tool_calls(raw.get("message", {}))
        if kind == "user":
            return self._tool_results(raw.get("message", {}))
        if kind == "result":
            # One before this run's init isn't its end: a resumed session reports what was left (a command it
            # had moved to the background, say) before it starts. Taken for the end, the run stopped reading.
            if not self.started:
                self.early = raw
                return []
            return self._result(raw)
        if kind == "rate_limit_event":
            return self._limit(raw.get("rate_limit_info") or {})
        return []

    @staticmethod
    def _limit(info: dict[str, Any]) -> list[Event]:
        """How much of the plan's usage window is used, and when it resets."""
        status = {"allowed": "allowed", "allowed_warning": "warning", "rejected": "rejected"}.get(
            info.get("status"), info.get("status"))
        windows = {name: {"utilization": w.get("utilization"), "resets_at": w.get("resetsAt")}
                   for name, w in (info.get("unifiedWindows") or {}).items() if isinstance(w, dict)}
        return [event("limit", status=status, resets_at=info.get("resetsAt"), window=info.get("rateLimitType"),
                      windows=windows or None)]

    def _stream_event(self, ev: dict[str, Any]) -> list[Event]:
        if ev.get("type") == "message_start":
            return [event("step", agent=self.agent, model=ev.get("message", {}).get("model"))]
        if ev.get("type") == "content_block_delta":
            delta = ev.get("delta", {})
            if delta.get("type") == "text_delta":
                self.said_something = True
                return [event("text", delta=delta.get("text", ""))]
            if delta.get("type") == "thinking_delta" and delta.get("thinking"):
                return [event("reasoning", delta=delta["thinking"])]
        return []

    def _tool_calls(self, message: dict[str, Any]) -> list[Event]:
        out = []
        for block in message.get("content", []):
            if block.get("type") == "tool_use":
                self.tool_names[block.get("id")] = block.get("name")
                out.append(event("tool_call", id=block.get("id"), name=block.get("name"),
                                 input=block.get("input")))
        return out

    def _tool_results(self, message: dict[str, Any]) -> list[Event]:
        content = message.get("content")
        if not isinstance(content, list):
            return []
        out = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tool_id = block.get("tool_use_id")
                out.append(event("tool_result", id=tool_id, name=self.tool_names.get(tool_id),
                                 ok=not block.get("is_error", False),
                                 output=_content_text(block.get("content"))))
        return out

    def _result(self, raw: dict[str, Any]) -> list[Event]:
        self.done = True
        out = []
        failed = raw.get("is_error") or raw.get("subtype") != "success"
        if not failed and not self.said_something and isinstance(raw.get("result"), str) and raw["result"].strip():
            # Slash commands like /context answer without streaming: only in the result.
            out.append(event("text", delta=raw["result"]))
        # With a subscription, total_cost_usd is what the run would have cost at API prices.
        out.append(event("usage", tokens=raw.get("usage"), cost=raw.get("total_cost_usd"),
                         billing=self.billing))
        for denial in raw.get("permission_denials") or []:
            out.append(event("permission", id=denial.get("tool_use_id"),
                             action=denial.get("tool_name"), resources=denial.get("tool_input"),
                             decision="denied"))
        if failed:
            message = raw.get("result") or raw.get("subtype") or "Claude Code failed"
            out.append(event("error", message=_said(str(message))))
        out.append(event("done", status="failed" if failed else "succeeded"))
        return out


class ClaudeCodeBackend:
    name = "claude-code"

    def __init__(
        self,
        directory: Path,
        resume: bool,
        binary: str = "claude",
        model: str | None = None,
        agent: str | None = None,
        auto_approve: bool = True,
        add_dirs: list[Path] | None = None,
        fork_from: str | None = None,
        permission_mode: str | None = None,
        system_prompt: str | None = None,
        permission_prompt_tool: str | None = None,
        effort: str | None = None,
        thinking_display: str | None = "summarized",
        mcp_servers: dict[str, Any] | None = None,
        allowed_tools: list[str] | None = None,
        env: dict[str, str] | None = None,
        settings: dict[str, Any] | None = None,
        wrap: Any = None,
    ) -> None:
        # (argv, folder, env) -> argv that runs claude elsewhere (e.g. in a container), with that env
        self.wrap = wrap
        self.settings = settings or {}  # extra settings (--settings), e.g. hooks
        self.permission_prompt_tool = permission_prompt_tool  # an MCP tool that asks someone
        self.effort = effort  # low, medium, high, xhigh or max; None: Claude Code's default
        # Newer models don't send their thinking unless asked; "summarized" gets
        # a summary of it (the flag the Agent SDK uses), None leaves it out.
        self.thinking_display = thinking_display
        self.mcp_servers = mcp_servers or {}  # added to the user's own MCP servers
        self.allowed_tools = allowed_tools or []  # allowed without asking, in any mode
        self.env = env or {}
        self.permission_mode = permission_mode  # e.g. "plan"; overrides auto_approve
        self.system_prompt = system_prompt  # appended to Claude Code's own
        self.add_dirs = add_dirs or []
        # A session to copy: the run continues from it under its own new id.
        self.fork_from = fork_from
        self.directory = directory
        self.resume = resume
        self.binary = binary
        self.model = model
        self.agent = agent
        self.auto_approve = auto_approve
        self.process: subprocess.Popen[str] | None = None
        self.stopped = False

    def command(self, session_id: str) -> list[str]:
        argv = [self.binary, "-p", "--output-format", "stream-json",
                "--include-partial-messages", "--verbose"]
        if self.fork_from:
            argv += ["--resume", self.fork_from, "--fork-session", "--session-id", session_id]
        else:
            argv += ["--resume", session_id] if self.resume else ["--session-id", session_id]
        if self.model:
            argv += ["--model", self.model]
        if self.effort:
            argv += ["--effort", self.effort]
        if self.thinking_display:
            argv += ["--thinking-display", self.thinking_display]
        if self.agent:
            argv += ["--agent", self.agent]
        for directory in self.add_dirs:
            argv += ["--add-dir", str(directory)]
        if self.system_prompt:
            argv += ["--append-system-prompt", self.system_prompt]
        if self.mcp_servers:
            argv += ["--mcp-config", json.dumps({"mcpServers": self.mcp_servers})]
        if self.allowed_tools:
            argv += ["--allowedTools", ",".join(self.allowed_tools)]
        if self.settings:
            argv += ["--settings", json.dumps(self.settings)]
        if self.permission_prompt_tool:
            argv += ["--permission-prompt-tool", self.permission_prompt_tool]
        if self.permission_mode:
            # Without a terminal, anything that needs permission is refused.
            argv += ["--permission-mode", self.permission_mode]
        elif self.auto_approve:
            argv.append("--dangerously-skip-permissions")
        return argv

    def run(self, session_id: str, text: str) -> Iterator[Event]:
        """Run one turn and yield uniform events until it is done.

        On KeyboardInterrupt the caller should call interrupt().
        """
        translator = Translator(self.agent)
        if self.stopped:
            yield event("done", status="interrupted")
            return
        command = self.command(session_id)
        if self.wrap is not None:
            command = self.wrap(command, self.directory, self.env)
        self.process = subprocess.Popen(
            command,
            cwd=self.directory,
            env={**os.environ, **self.env} if self.env and self.wrap is None else None,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            # Own process group: a terminal ctrl+c reaches only us, and
            # interrupt() decides how claude is stopped.
            start_new_session=True,
        )
        stderr = StderrTail(self.process.stderr)
        if self.stopped:  # interrupted while the process was starting
            stop_process(self.process)
        # The prompt goes via stdin: no length limit, and a leading "-" is fine.
        try:
            self.process.stdin.write(text)
            self.process.stdin.close()
        except BrokenPipeError:
            pass
        for line in self.process.stdout:
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            yield from translator.translate(raw)
            if translator.done:
                break
        if translator.done:
            # (anything it still says is read, or a process with more to say waits to say it for ever;
            # one that doesn't end after its result is stopped)
            threading.Thread(target=lambda: self.process.stdout.read(), daemon=True).start()
            try:
                self.process.wait(timeout=AFTER_RESULT)
            except subprocess.TimeoutExpired:
                stop_process(self.process)
        self.process.wait()
        if not translator.done:
            if self.stopped:
                yield event("done", status="interrupted")
                return
            early = translator.early or {}  # (an error before its init, say a session it couldn't resume)
            said = early.get("result") if early.get("is_error") and isinstance(early.get("result"), str) else None
            detail = said or stderr.last() or f"claude exited with code {self.process.returncode}"
            yield event("error", message=_said(detail))
            yield event("done", status="failed")

    def interrupt(self, session_id: str) -> None:
        # If the process doesn't exist yet, run() sees the flag and stops it.
        self.stopped = True
        stop_process(self.process)


def _said(message: str) -> str:
    """Claude Code's error in Relay's words where its own are for its command line ("Run --model to pick a
    different model", "Please run /login"): what to do in Relay instead."""
    found = MODEL_ISSUE.search(message)
    if found:
        return (f"Claude Code doesn't know the model {found.group(1)!r}, or this login can't use it. Choose another "
                "in the model menu: sonnet, opus or haiku (or a full name, such as claude-sonnet-5-5).")
    if LOGIN_ISSUE.search(message):
        return (f"{message.rstrip('. ')}. Claude Code isn't signed in any more: sign it in again in its Shell "
                "(Settings → Providers → Claude Code), with `claude auth login`.")
    return message


def _content_text(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
        return "".join(parts) or None
    return None
