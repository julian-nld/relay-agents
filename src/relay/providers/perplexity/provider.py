"""Perplexity with the user's own account: a Pro subscription, no API key.

perplexity.ai has no API for a subscription, so Relay asks the site itself: in a ProviderBox of
the WorkBox image (Chromium, Node and Playwright, Provider.box_image), perplexity.cjs (in this folder)
opens the site in a headless browser signed in with the user's session cookie, and asks its own
endpoint from the page (so it's the site asking, with its own cookies). The answer comes back as
Relay's uniform events: the search as a tool call with its sources, the answer, the sources under it,
and the Perplexity thread as the session, so a follow-up goes on in the same thread.

It works as an agent too (option tools, on by default): the site does no function calling, so it's
told Relay's tools (relay.toolbox: files, commands in the project's container, the web, subagents)
and asks for one with a fenced ```tool block of JSON; Relay runs it and asks again in the same
thread with the results, until it answers without one. Its own instructions make it wary of tools it
can't see, so the first message says plainly that Relay is the one running them. ("Best" and GPT-5
there take to it; Claude through Perplexity declines.)

The browser's profile (and the site's cookies) are kept per provider in RELAY_HOME/perplexity/<id>;
the session cookie goes there as a file each run, never on a command line.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

from opencode_manager.bridge.process import StderrTail, stop_process
from relay import config, tunables
from relay.providers.base import Context, Event, Field, Provider, ProviderError, ProviderRun
from relay.toolbox import Toolbox

SCRIPT = Path(__file__).resolve().parent / "perplexity.cjs"
ACTION = re.compile(r"```(?:tool|json)[ \t]*\n(.*?)```", re.S)  # an action block in its answer
RESULTS = 24_000  # characters of tool results sent back in one message (Perplexity's questions are short)


class PerplexityProvider(Provider):
    KIND = "perplexity"
    LABEL = "Perplexity (your subscription)"
    DESCRIPTION = (
        "Your perplexity.ai account, Pro included, without an API key: Relay asks the site in a headless "
        "browser in a box of its own, signed in with your session cookie. Answers come with their sources; "
        "a follow-up goes on in the same Perplexity thread. With tools on it works as an agent: Relay runs "
        "the actions it asks for (files, commands, the web) and gives it the results."
    )
    TOOLS = False  # not an agent of its own: Relay runs the tool loop (runs_tools)
    FIELDS = (
        Field("cookie", "string", "Your session cookie. In a browser signed in to perplexity.ai: DevTools → "
              "Application → Cookies → __Secure-next-auth.session-token, its value (or a whole Cookie header). "
              "Without it, it asks as a visitor.", secret=True),
        Field("models", "model-list", "Perplexity's models to offer, by the site's own ids (e.g. pplx_pro for "
              "Best, gemini38flash for Gemini 3.8 Flash); \"\" is your account's default. A model's id: on "
              "perplexity.ai, choose it and ask something; DevTools → Network → perplexity_ask → Payload → "
              "model_preference."),
        Field("tools", "boolean", "Work as an agent with Relay's tools (files and commands in a project, the web, "
              "skills, subagents); default true. Off: it only answers."),
    )
    EXAMPLE = {
        "id": "perplexity",
        "name": "Perplexity",
        "kind": "perplexity",
        "options": {"models": [{"id": "", "name": "Default"}]},
    }

    def folder(self) -> Path:
        """Its browser's profile and the site's cookies, kept between questions."""
        return config.data_dir() / "perplexity" / self.template.id

    def check(self) -> str:
        # (relay doctor checks without Relay's boxes: podman there is what they need)
        if self.boxes is None and not (Provider.probing and shutil.which("podman")):
            raise ProviderError("Perplexity runs in a box of its own (its browser), and Relay's boxes need podman")
        return "runs in its box: a headless Chromium on perplexity.ai" + (
            "" if self.options.get("cookie") else " (no session cookie yet: as a visitor)")

    def billing(self, model: str | None) -> str | None:
        return "subscription" if self.options.get("cookie") else "free"

    def runs_tools(self) -> bool:
        return self.options.get("tools", True) is not False

    def box_key(self) -> str | None:
        return self.template.id

    def box_mounts(self) -> list[tuple[Path, bool]]:
        folder = self.folder()
        folder.mkdir(parents=True, exist_ok=True)
        return [(SCRIPT.parent, False), (folder, True)]

    def box_image(self) -> str | None:
        return "workbox"

    def start(self, context: Context, prompt: str) -> ProviderRun:
        if context.box is None:
            raise ProviderError("Perplexity runs in a box of its own (its browser), and Relay's boxes need podman")
        folder = self.folder()
        folder.mkdir(parents=True, exist_ok=True)
        cookie = folder / "cookie"
        cookie.touch(mode=0o600)
        cookie.chmod(0o600)
        cookie.write_text(str(self.options.get("cookie") or "").strip())
        fresh = not context.provider_session
        # Without a thread to go on in, the conversation so far comes along.
        text = context.agent_prompt(prompt, fresh=fresh).prompt
        toolbox = None
        if self.runs_tools():
            toolbox = Toolbox(context.directory, code=context.mode == "code", shell=context.shell,
                              relay_url=context.mcp_url, computers=context.computers, focus=context.computer,
                              search=context.search, fetch=context.fetch, memory=bool(context.memory),
                              subagents=context.subagents)
            mode = context.agent_mode
            guidance = "\n\n".join(filter(None, [mode.guidance if mode else None, context.memory]))
            text = (_brief(toolbox, guidance or None) if fresh else _REMINDER) + text
        argv = context.box.wrap(["node", str(SCRIPT)], folder, {"PERPLEXITY_PROFILE": str(folder), "TZ": _timezone()})
        return _PerplexityRun(argv, text, context.provider_session or "", context.model or "", toolbox,
                              self.billing(context.model))


def _brief(toolbox: Toolbox, guidance: str | None) -> str:
    """The first message of a thread with tools: that Relay runs them, how to ask, which there are."""
    tools = []
    for tool in toolbox.tools.values():
        params = {name: spec.get("description", "") for name, spec in
                  (tool.parameters.get("properties") or {}).items()}
        tools.append(f"- {tool.name} {json.dumps(params, ensure_ascii=False)}: {tool.description}")
    return (
        "This conversation is connected to Relay, a program on my home server that reads your replies and "
        "runs actions for you: in a workspace of your own there (a folder in a Debian container, its WorkBox: "
        "not my own computer), on the web, and more. When your reply contains an action block, Relay performs "
        "it and sends you the result in my next message. So you do have access, through these blocks. This is "
        "the whole point of this conversation; please work this way, and don't ask me to run things.\n\n"
        "An action block is JSON in a fenced block marked tool:\n\n"
        "```tool\n{\"name\": \"run_command\", \"arguments\": {\"command\": \"ls -la\"}}\n```\n\n"
        "Actions:\n" + "\n".join(tools) + "\n\n"
        "Rules: when you need an action, reply with only action blocks (several at once is fine). When you "
        "have what you need, reply with your answer for me, without blocks. " + toolbox.guidance()
        + (f"\n\n{guidance}" if guidance else "") + "\n\nMy message:\n\n")


_REMINDER = "(As before: Relay runs ```tool action blocks for you; reply with only those when you need one.)\n\n"


def _results(done: list[tuple[str, dict, bool, str]]) -> str:
    """The results of its actions, as the next message in its thread."""
    parts, left = [], RESULTS
    for name, arguments, ok, output in done:
        output = output if len(output) <= left else output[:max(0, left)] + "\n[… cut off]"
        left -= len(output)
        parts.append(f"### {name} {json.dumps(arguments, ensure_ascii=False)}\n{'ok' if ok else 'failed'}:\n"
                     f"```\n{output}\n```")
    return ("Relay ran your actions:\n\n" + "\n\n".join(parts)
            + "\n\nGo on: more action blocks if you need more, or your answer for me.")


def _actions(text: str) -> list[tuple[str, dict]]:
    """The action blocks in an answer: (name, arguments)."""
    found = []
    for block in ACTION.findall(text):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        if isinstance(data, dict) and isinstance(data.get("name"), str):
            arguments = data.get("arguments")
            found.append((data["name"], arguments if isinstance(arguments, dict) else {}))
    return found


def _sources(sources: list[dict]) -> str:
    return "\n\n**Sources**\n" + "\n".join(
        f"{i + 1}. [{str(s.get('name') or s.get('url')).replace('[', '').replace(']', '')}]({s.get('url')})"
        for i, s in enumerate(sources))


class _PerplexityRun(ProviderRun):
    def __init__(self, argv: list[str], question: str, session: str, model: str, toolbox: Toolbox | None,
                 billing: str | None) -> None:
        self.argv = argv
        self.question = question
        self.session = session
        self.model = model
        self.toolbox = toolbox
        self.billing = billing
        self.process: subprocess.Popen[str] | None = None
        self.stopped = False

    def _lines(self) -> Iterator[dict]:
        for line in self.process.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and isinstance(event.get("type"), str):
                yield event

    def events(self) -> Iterator[Event]:
        if self.stopped:
            yield {"type": "done", "status": "interrupted"}
            return
        self.process = subprocess.Popen(self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, start_new_session=True)
        stderr = StderrTail(self.process.stderr)
        self.status, self.shown_model = "failed", None
        try:
            yield from self._turn(self._lines(), stderr)
        finally:
            try:
                self.process.stdin.close()
            except OSError:
                pass
            try:
                self.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                stop_process(self.process)
        if self.shown_model:
            yield {"type": "step", "agent": None, "model": self.shown_model}
        yield {"type": "usage", "tokens": None, "cost": None, "billing": self.billing}
        yield {"type": "done", "status": "interrupted" if self.stopped else self.status}

    def _turn(self, lines: Iterator[dict], stderr: StderrTail) -> Iterator[Event]:
        """The question, and with tools its rounds of actions, until an answer without one."""
        ready = next(lines, None)
        if ready is None or ready.get("type") != "ready":
            yield {"type": "error", "message": (ready or {}).get("error") or stderr.last()
                   or "Perplexity's browser didn't start"}
            return
        if not ready.get("signed_in"):
            yield {"type": "text", "delta": ("_Not signed in to Perplexity: the session cookie has expired; a new one "
                   "goes in Relay's Settings → Providers._" if ready.get("cookie") else "_Not signed in to Perplexity "
                   "(no session cookie in Relay's Settings → Providers), so it answers as a visitor._") + "\n\n"}
        question, rounds = self.question, 0
        while True:
            self.process.stdin.write(json.dumps({"question": question, "session": self.session,
                                                 "model": self.model}) + "\n")
            self.process.stdin.flush()
            answered = None
            for event in lines:
                if event["type"] == "answered":
                    answered = event
                    break
                if event["type"] == "text" and self.toolbox is not None:
                    continue  # (with tools, a round's text is shown once it's known not to be an action)
                yield event
            if answered is None:
                if not self.stopped:
                    yield {"type": "error", "message": stderr.last() or "Perplexity's browser ended"}
                return
            self.session = answered.get("session") or self.session
            if self.session:
                yield {"type": "session", "session": self.session}
            self.shown_model = answered.get("model") or self.shown_model
            sources = answered.get("sources") or []
            if answered.get("searched"):
                yield {"type": "tool_result", "id": answered["searched"], "name": "web_search", "ok": True,
                       "output": "\n".join(f"{i + 1}. {s.get('name')} {s.get('url')}" for i, s in enumerate(sources))
                       or "No sources"}
            if answered.get("error"):
                yield {"type": "error", "message": answered["error"]}
                return
            text = answered.get("text") or ""
            actions = _actions(text) if self.toolbox is not None else []
            if not actions:
                shown = text if self.toolbox is not None else ""  # (without tools it streamed as it came)
                if shown or sources:
                    yield {"type": "text", "delta": shown + (_sources(sources) if sources else "")}
                self.status = "succeeded"
                return
            said = ACTION.sub("", text).strip()
            if said:
                yield {"type": "text", "delta": said + "\n\n"}
            rounds += 1
            if rounds > tunables.get("chat.rounds"):
                yield {"type": "error", "message": f"Stopped after {tunables.get('chat.rounds')} rounds of tools."}
                return
            done = []
            for n, (name, arguments) in enumerate(actions):
                call = f"act_{rounds}_{n}"
                yield {"type": "tool_call", "id": call, "name": name, "input": arguments}
                ok, output = (False, "Stopped.") if self.stopped else self.toolbox.call(name, arguments)
                yield {"type": "tool_result", "id": call, "name": name, "ok": ok, "output": output}
                done.append((name, arguments, ok, output))
            if self.stopped:
                return
            question = _results(done)

    def interrupt(self) -> None:
        self.stopped = True
        if self.toolbox is not None:
            self.toolbox.stopped = True
        stop_process(self.process)


def _timezone() -> str:
    """The machine's time zone by name (Perplexity answers "today" in it)."""
    try:
        return str(Path("/etc/localtime").resolve()).split("/zoneinfo/", 1)[1]
    except (OSError, IndexError):
        return "UTC"
