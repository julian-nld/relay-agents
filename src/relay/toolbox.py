"""Tools for models that have nothing but a response stream (chat APIs).

Agents like Claude Code and opencode bring their own tools. A chat API only
answers; for those, Relay runs the agent loop itself: it offers the model
tools (OpenAI-style function calling), runs the ones the model calls, sends
the results back, and asks again, until the model answers.

    everywhere     web_search (Relay's SearXNG, relay.search, when it's on), web_fetch (relay.fetch),
                   list_skills, read_skill, and (outside subagents)
                   relay_agents, delegate, wait, tell (merge in code tasks): hand work to other agents;
                   computers, computer_run, computer_read_file,
                   computer_write_file: the user's own computers (if any)
    code tasks     list_files, read_file, search, write_file, edit_file,
    and chats      run_command (in the folder's container: relay.containers); not in a
                   computer task, whose work is on that computer

Files stay inside the task's folder; nobody is asked. The loop itself is in
providers/chat.py.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from relay import guard, memory, skills, tunables
from relay.tools_script import focus_text

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".next", "target"}
READ, EDIT = "read", "edit"


class ToolError(Exception):
    """A tool that can't do what was asked; the message goes back to the model."""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    access: str
    run: Callable[[dict[str, Any]], str]


def _params(required: list[str], **properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required}


def _text(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


def _number(description: str) -> dict[str, Any]:
    return {"type": "integer", "description": description}


class Toolbox:
    def __init__(self, directory: Path, code: bool, shell: Path | None = None,
                 relay_url: str | None = None, computers: list[str] | None = None, focus: str | None = None,
                 search: Callable[[dict[str, Any]], str] | None = None,
                 fetch: Callable[[dict[str, Any]], str] | None = None, memory: bool = False,
                 subagents: bool = True) -> None:
        self.directory = directory.resolve()
        self.code = code
        self.shell = shell  # runs commands in the folder's container (None: here, e.g. in tests)
        self.relay_url = relay_url  # Relay's JSON-RPC endpoint for this run (subagents)
        self.computers = computers or []  # the user's own computers (relay.computers)
        self.focus = focus  # a computer task's computer
        self.search = search  # Relay's web search (relay.search), if on
        self.fetch = fetch  # Relay's web fetch (relay.fetch), with its browser
        self.memory = memory  # Relay's memory (relay.memory) is on: the model may add to it
        self.subagents = subagents  # Relay's subagent tools, unless the user turned them off (relay.relay_tools)
        self.process: subprocess.Popen | None = None  # the command running now
        self.stopped = False
        self.tools = {tool.name: tool for tool in self._tools()}

    # What the model sees

    def definitions(self) -> list[dict[str, Any]]:
        return [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                  "parameters": t.parameters}} for t in self.tools.values()]

    def guidance(self) -> str:
        """A few lines for the system prompt, so the model knows where it is."""
        lines = ["You can use tools. Use them when they help, then answer the user."]
        # Where its files and commands are: its own WorkBox, on Relay's server (not the user's computer)
        where = (" run_command runs in its WorkBox: a Debian container on Relay's server "
                 "(not the user's own computer), as root: install what you need (apt-get, pip, npm)."
                 if self.shell else "")
        if self.code:
            lines.append(f"You work in the project folder {self.directory}; file paths are relative to it. "
                         "Start by looking around (list_files, read_file, search). Read a file before you "
                         "change it, and check your work, e.g. by running the tests." + where)
        elif not self.focus:
            lines.append(f"This conversation has a scratch folder of its own, {self.directory}: your files and "
                         "commands (list_files, read_file, write_file, run_command, …) are there, for whatever you "
                         "want to try, work out or make; file paths are relative to it." + where)
        if "delegate" in self.tools:
            lines.append("delegate hands work to another AI agent; relay_agents lists the ones there are.")
        if "computer_run" in self.tools and self.focus:
            lines.append(focus_text(self.focus, "computer_run, computer_read_file and computer_write_file"))
        elif "computer_run" in self.tools:
            lines.append(f"The user's own computers ({', '.join(self.computers)}) are something else: reach them "
                         "with computer_run and computer_read_file/computer_write_file only for what the user wants "
                         "done on one of them (Relay asks the user first); see computers for their systems. "
                         "Tell the user what you changed there.")
        lines.append("For some jobs there are skills with instructions (list_skills).")
        return " ".join(lines)

    def call(self, name: str, arguments: str | dict[str, Any]) -> tuple[bool, str]:
        """Run a tool the model called: (ok, result for the model)."""
        tool = self.tools.get(name)
        if tool is None:
            return False, f"There's no tool {name!r}."
        try:
            args = json.loads(arguments or "{}") if isinstance(arguments, str) else dict(arguments or {})
            if not isinstance(args, dict):
                raise ValueError("expected an object")
        except ValueError as exc:
            return False, f"The arguments aren't valid JSON ({exc}); send an object."
        try:
            return True, _cap(tool.run(args))
        except ToolError as exc:
            return False, str(exc)
        except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
            return False, f"{type(exc).__name__}: {exc}"

    # The tools

    def _tools(self) -> list[Tool]:
        return self._code_tools() + self._general_tools()

    def _general_tools(self) -> list[Tool]:
        tools = [
            *([Tool("web_search", "Search the web (Relay's own SearXNG, several search engines at once): titles, "
                    "addresses and snippets. Read a result with web_fetch.",
                    _params(["query"], query=_text("What to search for."),
                            category=_text("general (default), news, it or science."),
                            time_range=_text("Only this recent: day, week, month or year."),
                            language=_text("A language code, e.g. en or nl; default: any."),
                            page=_number("Which page of results; default 1.")), READ, self._web_search)]
              if self.search else []),
            Tool("web_fetch", "Fetch a web page or file (http or https) as text: a page's main content as markdown, a "
                 "PDF's text. Pages that refuse a plain fetch, or that their JavaScript builds, are opened in a "
                 "browser; a site that keeps refusing, through Startpage or from the Wayback Machine (it says so). "
                 "Long ones come in parts: the rest with offset.",
                 _params(["url"], url=_text("The address."),
                         offset=_number("Where to go on in a long one (the end of the last part says).")),
                 READ, self._web_fetch),
            Tool("list_skills", "List Relay's skills: instructions for particular jobs.",
                 _params([]), READ, self._list_skills),
            Tool("read_skill", "Read a skill's instructions (SKILL.md), or another file of it.",
                 _params(["name"], name=_text("The skill's name."),
                         file=_text("A file in the skill's folder; default SKILL.md.")), READ, self._read_skill),
        ]
        if self.relay_url and self.subagents:
            tools += [
                Tool("relay_agents", "List the AI agents (providers, models, modes) you can hand work to.",
                     _params([]), READ, lambda args: self._relay("agents", {})),
                Tool("delegate", "Hand a task to another AI agent. It doesn't see this conversation: put everything "
                     "it needs in the task. With wait true this returns its answer; otherwise an id at once, "
                     "and wait collects the answer.",
                     _params(["task"], task=_text("What to do, with all the context needed."),
                             provider=_text("Provider id from relay_agents; default: the same as yours."),
                             model=_text("Model id for that provider."),
                             mode=_text("Mode id, e.g. plan for read-only work."),
                             label=_text("A few words shown to the user."),
                             effort=_text("How hard it thinks: one of the model's efforts; default: its own "
                                          "(in Fast mode its lowest)."),
                             wait={"type": "boolean", "description": "Wait for the answer (up to 100 s)."}),
                     READ, lambda args: self._relay("delegate", args)),
                Tool("wait", "Wait (up to 100 s) for subagents and return the answers that are in.",
                     _params([], ids={"type": "array", "items": {"type": "string"},
                                      "description": "Subagent ids; default: all not collected yet."}),
                     READ, lambda args: self._relay("wait", args)),
                Tool("tell", "Send one of your subagents a message: one still working is interrupted and gets "
                     "it (e.g. \"Hurry up: answer with what you have\"); a finished one gets a follow-up. "
                     "Then wait for its answer again.",
                     _params(["id", "message"], id=_text("The subagent's id."), message=_text("What to tell it.")),
                     READ, lambda args: self._relay("tell", args)),
            ]
            if self.code:
                tools.append(Tool(
                    "merge", "A subagent works in a copy of your folder; its changes come into yours when it's "
                    "done, unless they conflict with yours or it didn't finish. Then you decide: this applies them "
                    "anyway, with conflict markers where they conflict.",
                    _params(["id"], id=_text("The subagent's id.")), EDIT, lambda args: self._relay("merge", args)))
        if self.relay_url and self.memory:
            which = _text("Its id, as the memory's list shows it.")
            category = {"type": "string", "enum": memory.KINDS, "description": "What it's about: " + "; ".join(
                f"{kind} ({about})" for kind, _, about in memory.CATEGORIES) + "."}
            tools += [
                Tool("remember", "Save something lasting to Relay's memory, which every agent in every chat of the "
                     "user's gets: about the user or their wishes, or (project true) about this project. One short "
                     "fact each, filed in its category; not what only this conversation needs.",
                     _params(["text", "category"], text=_text("The fact, short and on its own."), category=category,
                             project={"type": "boolean", "description": "Only for this project (default: general)."}),
                     READ, lambda args: self._relay("memory_add", args)),
                Tool("change_memory",
                     "Change one of Relay's memories, when it changed: its text, its category, or both.",
                     _params(["id"], id=which, text=_text("Its new text."), category=category), READ,
                     lambda args: self._relay("memory_change", args)),
                Tool("forget_memory", "Forget one of Relay's memories: it's wrong, or no longer so.",
                     _params(["id"], id=which), READ, lambda args: self._relay("memory_forget", args)),
                Tool("list_memories", "Relay's memories for this conversation: the general ones and this project's.",
                     _params([]), READ, lambda args: self._relay("memory_list", {})),
            ]
        if self.relay_url and self.computers:
            # Relay itself asks the user where a computer wants that, so no second ask here.
            which = _text(f"The computer: {', '.join(self.computers)}.")
            tools += [
                Tool("computers", "List the user's own computers: online or not, their system, user and home folder.",
                     _params([]), READ, lambda args: self._relay("computers", {})),
                Tool("computer_run", "Run a shell command on one of the user's computers (cmd.exe on Windows; "
                     "use powershell -Command for PowerShell). Returns the exit code and output.",
                     _params(["computer", "command"], computer=which, command=_text("The command."),
                             cwd=_text("Folder to run it in; default the user's home."),
                             timeout=_number("Seconds, at most 600; default 120.")),
                     READ, lambda args: self._relay("computer_run", args)),
                Tool("computer_read_file", "Read a text file on one of the user's computers.",
                     _params(["computer", "path"], computer=which, path=_text("The file; ~ is the home folder.")),
                     READ, lambda args: self._relay("computer_read", args)),
                Tool("computer_write_file", "Create or overwrite a text file on one of the user's computers.",
                     _params(["computer", "path", "content"], computer=which,
                             path=_text("The file; ~ is the home folder."),
                             content=_text("The whole new content.")),
                     READ, lambda args: self._relay("computer_write", args)),
            ]
        return tools

    def _code_tools(self) -> list[Tool]:
        """Files and commands in its folder: a code task's project, a chat's scratch folder (not in a
        computer task: that work is on the user's computer)."""
        tools: list[Tool] = []
        if self.code or not self.focus:
            tools += [
                Tool("list_files", "List the files and folders in the project (or a folder of it).",
                     _params([], path=_text("A folder; default: the project."),
                             depth=_number("How many levels deep; default 2.")), READ, self._list_files),
                Tool("read_file", "Read a text file, with line numbers.",
                     _params(["path"], path=_text("The file."), offset=_number("First line; default 1."),
                             limit=_number("How many lines; default 400.")), READ, self._read_file),
                Tool("search", "Search the project's text files for a regular expression.",
                     _params(["pattern"], pattern=_text("A regular expression."),
                             path=_text("A folder or file; default: the project."),
                             glob=_text("Only files matching this, e.g. *.py.")), READ, self._search),
                Tool("run_command", "Run a shell command in the project folder, e.g. to run tests or a build; "
                     "returns its output and exit code.",
                     _params(["command"], command=_text("The command (bash)."),
                             timeout=_number("Seconds before it's stopped; default 120, at most 600.")),
                     EDIT, self._run_command),
                Tool("write_file", "Create or overwrite a file with the given content.",
                     _params(["path", "content"], path=_text("The file."), content=_text("All of its text.")),
                     EDIT, self._write_file),
                Tool("edit_file", "Replace text in a file. old_string must occur exactly once, "
                     "unless replace_all is true; include enough surrounding text to make it unique.",
                     _params(["path", "old_string", "new_string"], path=_text("The file."),
                             old_string=_text("The exact text to replace."),
                             new_string=_text("What to put instead."),
                             replace_all={"type": "boolean", "description": "Replace every occurrence."}),
                     EDIT, self._edit_file),
            ]
        return tools

    def _path(self, value: Any, must_exist: bool = True, changes: bool = False) -> Path:
        # Relative to the project; absolute paths and ~ reach the rest of the machine too, to read
        # (Relay is for personal use: usability first). Changes stay in the project, like commands.
        target = (self.directory / Path(str(value or ".")).expanduser()).resolve()
        if changes and target != self.directory and self.directory not in target.parents:
            raise ToolError(f"Only files in the project folder ({self.directory}) can be changed.")
        if must_exist and not target.exists():
            raise ToolError(f"There's no {value!r} in the project.")
        return target

    def _relative(self, path: Path) -> str:
        if path == self.directory:
            return "."
        return str(path.relative_to(self.directory)) if self.directory in path.parents else str(path)

    def _web_fetch(self, args: dict[str, Any]) -> str:
        from relay.fetch import FetchError, text_here
        try:
            return (self.fetch or text_here)(args)  # (Relay's own, with its browser; here, in tests)
        except FetchError as exc:
            raise ToolError(str(exc)) from exc

    def _web_search(self, args: dict[str, Any]) -> str:
        from relay.search import SearchError
        try:
            return self.search(args)
        except SearchError as exc:
            raise ToolError(str(exc)) from exc

    def _list_skills(self, args: dict[str, Any]) -> str:
        found = skills.all_skills()
        return "\n".join(f"- {s.name}: {s.description}" for s in found) or "There are no skills."

    def _read_skill(self, args: dict[str, Any]) -> str:
        try:
            return skills.read_file(str(args.get("name", "")), str(args.get("file") or "SKILL.md"))
        except skills.SkillError as exc:
            raise ToolError(str(exc)) from exc

    def _relay(self, name: str, arguments: dict[str, Any]) -> str:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": name, "arguments": arguments}}).encode()
        request = urllib.request.Request(self.relay_url, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=700) as response:  # a computer's command: 600 s
                answer = json.loads(response.read())
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ToolError(f"Relay didn't answer: {exc}") from exc
        result = answer.get("result") or {}
        text = "".join(part.get("text", "") for part in result.get("content", []))
        if result.get("isError") or "error" in answer:
            raise ToolError(text or answer.get("error", {}).get("message", "failed"))
        return text

    def _list_files(self, args: dict[str, Any]) -> str:
        start = self._path(args.get("path"))
        depth = max(1, min(int(args.get("depth") or 2), 6))
        lines: list[str] = []

        def walk(folder: Path, level: int) -> None:
            for entry in sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                if len(lines) >= 500:
                    return
                if entry.name in SKIP_DIRS:
                    continue
                lines.append("  " * level + entry.name + ("/" if entry.is_dir() else ""))
                if entry.is_dir() and level + 1 < depth:
                    walk(entry, level + 1)

        if start.is_file():
            return self._relative(start)
        walk(start, 0)
        return "\n".join(lines) + ("\n… (cut off at 500)" if len(lines) >= 500 else "") if lines else "(empty)"

    def _read_file(self, args: dict[str, Any]) -> str:
        target = self._path(args.get("path"))
        if target.is_dir():
            raise ToolError("That's a folder; use list_files.")
        data = target.read_bytes()
        if b"\0" in data[:8192]:
            raise ToolError("That's a binary file.")
        lines = data.decode("utf-8", errors="replace").splitlines()
        offset = max(1, int(args.get("offset") or 1))
        limit = max(1, min(int(args.get("limit") or 400), 2000))
        shown = lines[offset - 1:offset - 1 + limit]
        body = "\n".join(f"{n:>6}\t{line}" for n, line in enumerate(shown, start=offset))
        more = len(lines) - (offset - 1 + len(shown))
        return body + (f"\n… {more} more lines (use offset)" if more > 0 else "") if shown else "(no lines there)"

    def _search(self, args: dict[str, Any]) -> str:
        try:
            pattern = re.compile(str(args.get("pattern", "")))
        except re.error as exc:
            raise ToolError(f"That isn't a valid regular expression: {exc}") from exc
        start = self._path(args.get("path"))
        glob = str(args.get("glob") or "*")
        files = [start] if start.is_file() else [
            p for p in start.rglob(glob) if p.is_file() and not SKIP_DIRS.intersection(p.relative_to(start).parts)]
        found = []
        for path in sorted(files):
            try:
                if path.stat().st_size > 1_000_000:
                    continue
                data = path.read_bytes()
            except OSError:
                continue
            if b"\0" in data[:8192]:
                continue
            for n, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), start=1):
                if pattern.search(line):
                    found.append(f"{self._relative(path)}:{n}: {line.strip()[:300]}")
                    if len(found) >= 200:
                        return "\n".join(found) + "\n… (cut off at 200 matches)"
        return "\n".join(found) or "No matches."

    def _write_file(self, args: dict[str, Any]) -> str:
        target = self._path(args.get("path"), must_exist=False, changes=True)
        if target.is_dir():
            raise ToolError("That's a folder.")
        content = str(args.get("content", ""))
        target.parent.mkdir(parents=True, exist_ok=True)
        existed = target.exists()
        target.write_text(content)
        return f"{'Overwrote' if existed else 'Created'} {self._relative(target)} ({len(content.splitlines())} lines)."

    def _edit_file(self, args: dict[str, Any]) -> str:
        target = self._path(args.get("path"), changes=True)
        text = target.read_text()
        old, new = str(args.get("old_string", "")), str(args.get("new_string", ""))
        if not old:
            raise ToolError("old_string is empty.")
        count = text.count(old)
        if count == 0:
            raise ToolError("old_string isn't in the file; read it again and copy the text exactly.")
        if count > 1 and not args.get("replace_all"):
            raise ToolError(f"old_string occurs {count} times; add surrounding text to make it unique, "
                            "or set replace_all.")
        target.write_text(text.replace(old, new) if args.get("replace_all") else text.replace(old, new, 1))
        return f"Edited {self._relative(target)} ({count if args.get('replace_all') else 1} replacement(s))."

    def _run_command(self, args: dict[str, Any]) -> str:
        command = str(args.get("command", "")).strip()
        if not command:
            raise ToolError("The command is empty.")
        refused = guard.check(command)
        if refused:
            raise ToolError(f"Relay blocked this command: {refused}")
        timeout = max(1, min(int(args.get("timeout") or tunables.get("chat.command_timeout")), 600))
        if self.stopped:
            raise ToolError("Stopped.")
        # Its own process group, so a time-out or a stop ends what it started too (a
        # server it launched would otherwise keep the output open, and Relay waiting).
        shell = str(self.shell) if self.shell else "bash"
        self.process = subprocess.Popen([shell, "-c", command], cwd=self.directory, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
                                        errors="replace", start_new_session=True)
        try:
            output, _ = self.process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._end_process()
            output, _ = self.process.communicate()
            return f"Stopped after {timeout} s.\n{output or ''}".rstrip()
        finally:
            code = self.process.returncode
            self.process = None
        if self.stopped:
            return f"Stopped.\n{output or ''}".rstrip()
        return f"Exit code {code}\n{output or ''}".rstrip()

    def _end_process(self) -> None:
        process = self.process
        if process is not None:  # the whole group: the shell may be done while what it started isn't
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    def stop(self) -> None:
        """End the command running now (the run is being stopped)."""
        self.stopped = True
        self._end_process()


def _cap(text: str) -> str:
    most = tunables.get("chat.tool_output")
    if len(text) <= most:
        return text
    return text[:most] + f"\n… ({len(text) - most} more characters cut off)"


