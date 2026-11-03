"""The example layer (kind example) for toy-agent: what docs/ADDING-A-PROVIDER.md builds step by step.

toy-agent (toy_agent.py, in this folder) is a program Relay doesn't know, with a JSON format of its own. This file
is its support layer: it says how to start it (in its ProviderBox), hands it the prompt, and turns what it prints
into Relay's uniform events (opencode_manager/bridge/events.py). Everything else (the conversation, modes,
handovers, fallback, limits, health, repairs) Relay does around it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

from opencode_manager.bridge.process import StderrTail, stop_process
from relay import config
from relay.providers.base import Context, Event, Field, Provider, ProviderError, ProviderRun

PROGRAM = Path(__file__).resolve().parent / "toy_agent.py"


# 1. The kind: what Settings shows, its options, an example template.

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

    # 2. Whether it can be used: the program is there.

    def check(self) -> str:
        if not PROGRAM.exists():
            raise ProviderError(f"{PROGRAM} isn't there")
        return f"toy-agent is in {PROGRAM.parent}"

    # 3. Its box: one of its own, with the program (read-only) and its sessions folder (its "login").

    def folder(self) -> Path:
        return config.data_dir() / "example" / self.template.id

    def env(self) -> dict[str, str]:
        return {"TOY_AGENT_HOME": str(self.folder())}

    def box_key(self) -> str | None:
        return self.template.id

    def box_mounts(self) -> list[tuple[Path, bool]]:
        self.folder().mkdir(parents=True, exist_ok=True)
        return [(PROGRAM.parent, False), (self.folder(), True)]

    # 4. A turn: the program started, the prompt on its stdin.

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


# 5. Its output as Relay's events.

class ToyRun(ProviderRun):
    def __init__(self, argv: list[str], directory: Path, env: dict[str, str], prompt: str) -> None:
        self.argv, self.directory, self.env, self.prompt = argv, directory, env, prompt
        self.process: subprocess.Popen[str] | None = None
        self.stopped = False

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

    def interrupt(self) -> None:
        self.stopped = True
        stop_process(self.process)


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
