"""The `relay` command agents run with their shell, and what they're told about it.

Every agent conversation gets a small read-only script of its own
(tools_dir()/<token>/relay, see Delegation.script_for) that talks to Relay's
JSON-RPC endpoint; note() is what an agent is told about it, the skills it
doesn't load itself, and the user's computers.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from opencode_manager import paths
from relay import tunables


def note(path: Path, skills: list[str] | None = None, computers: list[str] | None = None, focus: str | None = None,
         via_mcp: bool = False, repository: str | None = None, code: bool = False, on_path: bool = True,
         search: bool = False, service: dict[str, Any] | None = None, fetch: bool = True,
         subagents: bool = True, scripts: str | None = None, plugins: list[str] | None = None) -> str:
    """What an agent is told about Relay, briefly: what there is and how to find out more
    (`relay help`); the details are help topics (help()). Relay's tools come as MCP tools of the
    server "relay" (via_mcp), or as the `relay` command (on the WorkBox's PATH, container/
    bin/relay; at `path` where there are no containers). `skills`: the names of the skills it
    doesn't load itself; `repository`: the larger git repository the project is in, if so; `service`: the
    live service (relay.production) whose WorkBox this folder is, if so. `fetch`, `subagents`: off when the user
    turned Relay's web or subagent tools off for this conversation (relay.relay_tools). `scripts`: the user's public
    scripts' folder (relay.scripts), which every agent may read. `plugins`: the names of the tools the user's
    plugins add (relay.plugins)."""
    command = "relay" if on_path else shlex.quote(str(path))  # a home folder may have spaces in it
    what = ", ".join(filter(None, ["web search" if search else "", "web fetch with a real browser" if fetch else "",
                                   "hand work to other AI agents and models" if subagents else "",
                                   "this task's record of its turns"
                                   if code else "", f"the user's own computers: {', '.join(computers)}"
                                   if computers else ""]))
    if via_mcp:
        text = (f"Relay, the app this conversation runs in, adds tools of its own next to yours: those of its MCP "
                f"server \"relay\" ({what}); their descriptions say how."
                + (" Use your own subagents for more work on your own model, Relay's delegate for another provider "
                   "or model." if subagents else ""))
    else:
        text = (f"Relay, the app this conversation runs in, adds tools of its own next to yours ({what}): run "
                f"`{command} help` with your shell tool for them.")
    if plugins:
        text += (f" The user's plugins add tools too: {', '.join(plugins)}"
                 + ("." if via_mcp else f" (`{command} help plugins` says what they do and how to call them)."))
    if search:
        text += (" Search the web with Relay's web search first: it's the user's own (SearXNG), which they prefer to "
                 "yours.")
    if fetch:
        text += (" When your own web fetch is refused or comes back empty, Relay's fetch opens the page in a browser "
                 "(and as a last resort has Startpage fetch it, or reads the Wayback Machine's copy).")
    text += (" Your shell commands run in this project's WorkBox, a Debian container on Relay's server (not the user's "
             "own computer), as root: install what you need "
             f"(`{command} help container` says what's there). A server the user should go on using: "
             + ("`relay serve NAME CMD` (`relay help container` says more)." if on_path else
                "start it detached (your shell tool's option, or `setsid -f CMD > server.log 2>&1`); not in a "
                "background shell of your own, which keeps your turn from ending."))
    if repository:
        text += (f" The project is part of a larger git repository ({repository}), which is read-only but for this "
                 "folder: keep git to it (git status --untracked-files=all -- ., git add -- ., not -A).")
    if service:
        text += (f" This folder is the WorkBox of the live service {service['name']}: Promote to live (the user's "
                 "/promote) makes it the service's next version, a copy of the folder in a box of its own. A version "
                 "listens on $PORT and keeps its data in $DATA_DIR, which goes on from version to version; what it "
                 "writes in its own folder (a database file next to the code, say) stays behind with the old "
                 "version. And it has only what's in the folder: what's installed in this box (pip install, "
                 "apt-get) isn't there, so keep dependencies in the folder (node_modules, a .venv) or have the start "
                 f"command install them. To try it here: PORT=18080 DATA_DIR=.data {service['command']}.")
    if code:
        text += (f" Relay records every turn and never commits: don't commit unless the user asks "
                 f"(`{command} help task`).")
    if scripts:
        text += (f" The user's public scripts (Python with Relay's SDK) are in {scripts}: read them as you like; they "
                 "run on the user's Scripts page.")
    if skills:
        text += (f" Skills for particular jobs: {', '.join(skills)}. When one fits, read its SKILL.md first "
                 f"(`{command} help skills` says what each is for and where it is).")
    if focus:
        text += "\n\n" + focus_text(focus, "computer_run (it goes to that computer without a computer name), "
                                    "computer_read and computer_write" if via_mcp else
                                    f"`{command} run` (it goes to {focus} without --on) and `{command} pull`/`push`")
    return text


def search_note(command: str = "relay", search: bool = True) -> str:
    """What a subagent with a shell is told: of Relay's tools it has the web's only."""
    return ("Relay adds tools for the web next to your own, for your shell tool: "
            + (f"`{command} search QUERY` searches it (its own SearXNG, which the user prefers to yours: search "
               f"with it first; --category news|it|science, --time day|week|month|year, --lang CODE, --page N); "
               if search else "")
            + f"`{command} fetch URL` reads a page, opening it in a browser when a plain fetch is refused or "
              f"comes back empty (long ones in parts: --offset N).")


HELP_TOPICS = ("container", "task", "subagents", "computers", "skills", "plugins", "agentos")

AGENTOS_HELP = """\
AgentOS's capabilities (only the Governor has them): `{command} agentos CAPABILITY key=value …`, a message \
(a task, say) on stdin. As MCP tools: agentos_task_list, agentos_chat_create, …. Those that start or wait for \
an agent take up to two minutes: give the command a time-out of three; one stopped sooner was done all the \
same, and your next call brings its answer.
  task.list                                   the AgentOS tasks: what, when next, when last and how it went
  task.done id=ID [ok=false] [note=…]         report how a task given to you went (a briefing's, or one
                                              task.list says awaits your report); the note on stdin will do
  task.create name=… when=… [folder=DIR] [governed=false provider=P …]  (what's to be done on stdin)
                                              a task of your own; when: every 72 hours, daily 08:00,
                                              weekdays 09:00, weekly mon,wed 08:00, once 2026-10-05 09:00
                                              (the briefing's zone), once 2026-10-05T07:00Z, in 10 minutes
  task.create name=… when=… script=NAME [args=…]   a task that runs one of the user's public scripts at its
                                              time, as the user, with no agent: Relay runs it
  task.update id=ID [name=…] [when=…] [enabled=false] …  (new text on stdin)   one you made
  task.delete id=ID                           one you made (the user's own tasks aren't yours to change)
  chat.list                                   the managed chats and code tasks
  chat.create [provider=P] [model=M] [folder=DIR] [mode=…] [effort=…] [title=…]  (message on stdin: an agent
                                              starts on it at once; with folder, a code task there: a new
                                              folder in a workspace root is made, for a new project)
  agent.spawn [provider=P] … (message on stdin) chat.create with a task
  agent.review id=ID [provider=P] [model=M] [mode=…] (what to review on stdin)
                                              a fresh agent in that code task's folder and WorkBox (a
                                              clone's too), read-only by default: reads, runs its tests
  chat.send id=ID (message on stdin)          message a managed chat; a quick answer (30 s) comes back at once,
                                              else you're woken when it's done
  chat.read id=ID [last=N] [offset=N]         its last messages (`chat.read ID` will do too); offset: its
                                              last answer from that character on (a long one, in parts)
  chat.wait [ids=ID,ID]                       a short wait until they're done (default: all that work now; up
                                              to 100 s): answers, and how far the others got; for longer work
                                              end your wake-up: you're woken when they're done
  chat.tell id=ID (message on stdin)          steer an agent: a working one is interrupted, the message is its
                                              next turn
  chat.fork id=ID                             a managed fork; a user's own chat asks them first
  chat.delete id=ID                           delete a managed chat or code task
  notify.intervention (message on stdin)      "Intervention needed: …" for the user (its id comes back)
  notify.update id=ID (message on stdin)      an open one's text, anew: what's needed now
  notify.resolve id=ID note=…                 an open one is settled (fixed, checked; or no longer applies):
                                              off the user's list and your briefings, with your why
  notify.report (message on stdin)            what's done or found, for the user to know: nothing to act on
  service.list                                the live services: address, live version, health, its box
  service.status service=NAME                 one in detail: its box (since when, restarts, last exit,
                                              OOM), processes, health checks, a check just now, versions,
                                              its last 7 days (failures, data growth)
  service.logs service=NAME [lines=N]         its live version's log
  service.restart service=NAME                its live version restarted (its box made anew when it's gone)
  service.stop service=NAME                   not served or checked any more; its versions and data stay
  service.start service=NAME                  a stopped one served again
  service.delete service=NAME                 the service gone, with its versions and data: when the user asks
  workbox.clone service=NAME [provider=P] [model=M]  (a task on stdin: an agent starts at once)
                                              its live code and a data snapshot (.data) as a managed code task
  workbox.delete id=ID                        a clone, and its folder
  workbox.look id=ID command=…                a command in a managed code task's folder, read-only (ls, cat,
                                              git diff, grep): to see an agent's work yourself
  production.promote service=NAME id=ID       a clone of that service (made from the version live now) as
                                              its new live version (started and checked first; the old one
                                              is archived for 7 days)
  production.rollback service=NAME [version=N]  back to an archived version
  service.create id=ID [name=N] [command=C] [port=P] [health=/path]  a new live service from a managed code
                                              task's folder (it runs on $PORT, its data in $DATA_DIR), looked after
                                              by AgentOS; the task goes on as its WorkBox (production.promote)
  service.restore service=NAME snapshot=NAME  its data as a snapshot has it (service.status lists them; the data
                                              now is kept first, to undo it): its box stops meanwhile
  provider.repair provider=ID helper=P [model=M] (what's wrong on stdin)
                                              another agent repairs Relay's support layer for a provider
                                              that keeps failing, on copies of its files (a managed code task):
                                              its plan, trying it and applying it are the user's, on its page
  models.list                                 the models available here, the scores Relay keeps for them on
                                              the intelligence index and their ranks; those not looked up yet
  models.score (a JSON list on stdin: [{{"provider": P, "model": M, "name": "Claude Opus 5.5", "score": 70,
                "estimated": false, "source": URL, "note": "…"}}, …; a router: "router": true, no score])
                                              what you found of new ones; Relay ranks
                                              them (name: exactly as the index names it; the rest in note)
  memory.review                               all of Relay's memory (every place), when an agent last got each,
                                              and what Relay found: alike, place gone, unused for long
  memory.change id=ID [category=C] (its new text on stdin)   a memory (not the user's own): merging, say
  memory.forget id=ID (why on stdin)          a memory (not the user's own) gone; the user sees why
  script.list                                 the user's public scripts (Python with Relay's SDK), how each
                                              last ran; their private ones aren't yours
  script.read name=NAME                       a public script's text
  script.run name=NAME [args=…]               run one as the user (it may start agents): its end and output
                                              within 30 s, else its run's id
  script.output run=ID                        a run's state, what it started, its output's end
`{command} agents` lists the providers and models you may use (without provider=: your own model). The
user may want to be asked before promote, rollback or a delete: then the call waits for their answer.
Relay keeps a log of all of it."""


def help_text(topic: str | None, command: str = "relay", computers: list[str] | None = None, code: bool = False,
              repository: str | None = None, skills_text: str = "", search: bool = False,
              plugins: list[dict[str, Any]] | None = None) -> str:
    """`relay help [TOPIC]`: what the note leaves out (Relay answers it: what's there now)."""
    from relay import skills

    topic = (topic or "").strip().lower()
    if topic in ("", "commands"):
        searching = ("  search QUERY             search the web (Relay's SearXNG); --category news|it|science, "
                     "--time day|week|month|year, --lang, --page\n" if search else "")
        searching += ("  fetch URL                a page as text (in a browser when it must; a PDF's text too); "
                      "--offset N for the rest of a long one\n")
        text = f"""\
Relay's tools: run `{command} COMMAND` with your shell tool.
{searching}  agents                   the AI agents you can hand work to: providers, models, modes, limits
  delegate …               start a subagent: the task on stdin; --provider, --model, --mode, --label, --effort
  wait [ID ...]            collect subagents' answers (waits up to {tunables.get("subagents.wait")} seconds)
  tell ID …                a message to a subagent (on stdin); one still working is interrupted and gets it
  memory add|change|forget|list   Relay's memory, which every agent in every chat gets (its note says more)
"""
        if code:
            text += """\
  changes                  this task's turns as Relay recorded them (the files before and after each)
  diff [TURN|all]          the changes of one turn (default the last), or all since the task started
  restore TURN             the files back as they were before that turn (only when the user asks)
  commit -m MESSAGE        commit the task's folder to the user's repository (only when the user asks)
  merge ID                 apply a subagent's changes that weren't (they conflicted, or it didn't finish)
"""
        if computers:
            text += f"""\
  computers                the user's own computers ({', '.join(computers)}): their system, user and home
  run --on NAME 'COMMAND'  run a command there: quote it whole (or give it on stdin); --cwd, --timeout
  pull --on NAME REMOTE [LOCAL] / push --on NAME LOCAL REMOTE   copy a file from or to it
"""
        if plugins:
            text += ("  plugin TOOL [KEY=VALUE …]   a tool of the user's plugins: "
                     f"{', '.join(t['name'] for t in plugins)}\n")
        topics = [t for t in HELP_TOPICS if (t != "task" or code) and (t != "computers" or computers)
                  and t != "agentos" and (t != "plugins" or plugins)]
        return text + f"More: `{command} help TOPIC`, TOPIC one of {', '.join(topics)}."
    if topic == "container":
        return container(repository)
    if topic == "task":
        return TASK_NOTE.format(changes=f"{command} changes", diff=f"{command} diff", restore=f"{command} restore",
                                commit=f"{command} commit") if code else "This isn't a code task."
    if topic == "subagents":
        return ("Hand a task to another AI agent or model with `delegate` (it starts without this conversation: "
                "give it everything it needs); several work at once; `wait` collects their answers, `tell` sends one "
                "a message. `agents` lists who's there, their models, modes and costs, and what you may use without "
                "asking the user. Use your own subagents for more work on your own model, Relay's for another "
                "provider or model. The relay-subagents skill says when and how.")
    if topic == "computers":
        if not computers:
            return "The user has no computers of their own in Relay."
        return (f"The user's own computers: {', '.join(computers)}. `computers` shows their system, user and home; "
                f"`run --on NAME` runs a command there, `pull`/`push` copy files. The relay-computers skill says "
                "how to work there.")
    if topic == "agentos":
        return AGENTOS_HELP.format(command=command)
    if topic == "plugins":
        return plugins_help(plugins or [], command)
    if topic == "skills":
        return (skills_text or "No skills besides the ones your program loads itself.") + (
            f"\n\nSkills you write for Relay go in {shlex.quote(str(skills.user_dir()))} (relay-skill-writing "
            "explains the format).")
    return f"No help on {topic!r}: one of {', '.join(HELP_TOPICS)}, or nothing for the commands."


def plugins_help(tools: list[dict[str, Any]], command: str = "relay") -> str:
    """`relay help plugins`: the tools the user's plugins add (relay.plugins), with their arguments."""
    if not tools:
        return "The user's plugins add no tools."
    lines = [f"Tools the user's plugins add: `{command} plugin TOOL KEY=VALUE …` (a value that's JSON is taken as "
             f"such: a number, true, a list), or one JSON object as its only argument, or on stdin."]
    for tool in tools:
        lines.append(f"\n{tool['name']}: {tool['description']}")
        schema = tool.get("inputSchema") or {}
        needed = set(schema.get("required") or [])
        for key, spec in (schema.get("properties") or {}).items():
            spec = spec if isinstance(spec, dict) else {}
            what = spec.get("type") or "any"
            lines.append(f"  {key} ({what}{', required' if key in needed else ''})"
                         + (f": {spec['description']}" if spec.get("description") else ""))
    return "\n".join(lines)


# What an agent in a code task is told about Relay's record of its turns (relay.tasks).
TASK_NOTE = ("Relay records every turn of this task itself (the files before and after, outside git: "
             "{changes}, {diff}); it never commits, so don't commit unless the user asks (then {commit}, "
             "which commits as the user). Subagents that may change files work in a copy of your folder; their "
             "changes come into yours when they're done, unless they conflict or the subagent didn't finish: then "
             "you decide (merge).")


def container(repository: str | None = None) -> str:
    """Where the agent's commands run (relay.containers)."""
    text = ("Your shell commands run in this project's WorkBox, a Debian container (not on the machine your program "
            "runs on), with the project folder at the same path; Relay's tools and skills are there too, read-only. "
            "You're root there: install what you need (apt-get, pip, npm); it's kept for this folder. "
            "Python 3.13 (pip, uv, pytest, ruff, mypy), Node.js 22 (npm, pnpm, yarn, TypeScript; "
            "`n 24` for another version), Go, Rust, gcc and cmake, the GitHub CLI, ripgrep, fd, "
            "jq and sqlite are there, and Chromium with Playwright (playwright-core, "
            "/usr/bin/chromium) for work in a browser. Containers work here too: podman, also as docker (run, build, "
            "`docker compose`). They share the WorkBox's network: a container's server listens on its own port here "
            "(-p doesn't remap it; not under 1024), and compose services reach each other by name. "
            "Their images are kept for this folder. Commands still running when your turn ends stop with it, "
            "so a server the user should go on using (a dev server, say): `relay serve NAME CMD` starts it detached, "
            "its log in /tmp/relay-serve/NAME.log; again with the same NAME restarts it; `relay serve --stop NAME`, "
            "`relay serve --list`. (Or your shell tool's own option to detach.) Then check that it answers. "
            "Not in a background shell of your own (that keeps your turn from ending), not with `timeout`, "
            "and not stopped with `pkill -f` on a pattern that's in your own command too. "
            "The user reaches this machine's ports only through Relay, port N at /port/N/ on Relay's address: "
            "give them http://localhost:N/, which Relay's chat turns into that link. "
            "Absolute paths (/assets/…) work there as they are; an app whose own routes show in the address bar does "
            "better with the base /port/N/ (Vite --base, Next basePath), which Relay notices.")
    if repository:
        text += (f" The project is part of a larger git repository ({repository}), which is read-only but for this "
                 "folder: keep git to it (git status --untracked-files=all -- ., git add -- ., not -A).")
    return text


def focus_text(computer: str, how: str) -> str:
    """What an agent in a computer task is told: the work is on that computer, not here."""
    return (f"This conversation is about the user's own computer \"{computer}\": the work happens there, not on the "
            f"machine you run on. Use {how}; start with a look at its system (`computers`), and tell the user what "
            f"you changed. If it's offline, say so and ask the user to start Relay's connector there.")


def tools_dir() -> Path:
    """In the machine's cache: the script is written anew for every run anyway."""
    return paths.host_cache_dir() / "relay-tools"


# The script each conversation gets; @PYTHON@ and @URL@ are filled in.
SCRIPT = '''#!@PYTHON@
"""Relay's tools for this conversation. `relay help` says what they are."""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

# In a WorkBox the conversation's address comes with the command (RELAY_MCP_URL): the script is the
# same for every conversation there, and holds no token.
URL = os.environ.get("RELAY_MCP_URL") or "@URL@"


def call(name, arguments, timeout=300, quiet=False):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}}).encode()
    request = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            answer = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        sys.exit(f"relay: {exc.code}: no run of this conversation is going" if exc.code == 404 else f"relay: {exc}")
    except OSError as exc:
        sys.exit(f"relay: can't reach Relay ({exc})")
    result = answer.get("result") or {}
    text = "".join(part.get("text", "") for part in result.get("content", []))
    failed = result.get("isError") or "error" in answer
    if quiet and not failed:
        return text  # e.g. a file's content, for the caller to handle
    print(text or answer.get("error", {}).get("message", ""))
    sys.exit(1 if failed else 0)


parser = argparse.ArgumentParser(prog="relay", description="Relay's tools: search and read the web, hand work to "
                                 "other AI agents, and work on the user's own computers.")
commands = parser.add_subparsers(dest="command")
help_command = commands.add_parser("help", help="what Relay's tools are, or more on a topic (Relay says which)")
help_command.add_argument("topic", nargs="?")
search = commands.add_parser("search", help="search the web (Relay's own SearXNG)")
search.add_argument("--category", help="general (default), news, it or science")
search.add_argument("--time", help="only this recent: day, week, month or year")
search.add_argument("--lang", help="a language code, e.g. en or nl")
search.add_argument("--page", type=int, help="which page of results (default 1)")
search.add_argument("query", nargs="+", help="what to search for")
fetch = commands.add_parser("fetch", help="a web page as text (in a browser when a plain fetch is refused)")
fetch.add_argument("--offset", type=int, help="where to go on in a long one (its last part says)")
fetch.add_argument("url", help="the address")
commands.add_parser("agents", help="list the providers, models and modes to hand work to")
delegate = commands.add_parser("delegate", help="start a subagent; the task comes on stdin (or as arguments)")
delegate.add_argument("--provider", help="provider id (default: the same as yours)")
delegate.add_argument("--model", help="model id (default: the provider's default)")
delegate.add_argument("--mode", help="mode id, e.g. plan for read-only work")
delegate.add_argument("--label", help="a few words shown to the user, e.g. \\"Devil's advocate\\"")
delegate.add_argument("--effort", help="how hard it thinks: one of the model's efforts (relay agents lists them)")
delegate.add_argument("--wait", action="store_true", help="wait for the answer (up to 100 s) instead of printing an id")
delegate.add_argument("task", nargs="*", help="the task (instead of stdin)")
wait = commands.add_parser("wait", help="wait for subagents and print their answers")
wait.add_argument("ids", nargs="*", help="subagent ids (default: all not yet collected)")
tell = commands.add_parser("tell", help="send a subagent a message (interrupting it if it's still working)")
tell.add_argument("id", help="the subagent's id")
tell.add_argument("message", nargs="*", help="the message (instead of stdin)")
commands.add_parser("changes", help="this code task's turns, as Relay recorded them, and what each changed")
diff = commands.add_parser("diff", help="the changes of one turn (default the last), or all since the task started")
diff.add_argument("turn", nargs="?", help="a turn's number from changes, or all")
restore = commands.add_parser("restore", help="put the task's files back as they were before a turn")
restore.add_argument("turn", help="a turn's number from changes, or a point restore printed")
commit = commands.add_parser("commit", help="commit the task's folder to the user's repository (only when asked)")
commit.add_argument("-m", "--message", required=True, help="the commit message")
merge = commands.add_parser("merge", help="apply a subagent's changes to your folder, with conflict markers if needed")
merge.add_argument("id", help="the subagent's id")
agentos = commands.add_parser("agentos", help="AgentOS's capabilities (the Governor's): relay help agentos")
agentos.add_argument("capability", help="e.g. task.list, chat.create")
agentos.add_argument("fields", nargs="*", help="key=value, e.g. provider=opencode folder=/home/me/site; the "
                     "message (a task, say) on stdin")
commands.add_parser("computers", help="list the user's own computers")
repairing = commands.add_parser("repair", help="in a repair (Relay's self-repair): check your version of the files, "
                                "or run a command where the provider's program runs").add_subparsers(dest="action")
repairing.add_parser("check", help="test your version of the support files in a copy of Relay")
repair_run = repairing.add_parser("run", help="a command in the provider's own box (its program, its login)")
repair_run.add_argument("--timeout", type=int, help="seconds, at most 600 (default 120)")
repair_run.add_argument("words", nargs=argparse.REMAINDER, metavar="command", help="the command, after --")
repair_plan = repairing.add_parser("plan", help="hand in your plan (on stdin) for the user: a repair's, or a new "
                                   "provider's (with its name and program)")
repair_plan.add_argument("name", nargs="?", default="", help="a new provider: its folder's name, e.g. gemini")
repair_plan.add_argument("program", nargs="?", default="", help="a new provider: how its program is started in its box")
remember = commands.add_parser("memory", help="Relay's memory, which every agent in every chat gets")
remember.add_argument("action", choices=["add", "change", "forget", "list"])
remember.add_argument("words", nargs="*", help="add: the fact (or on stdin); change: ID, then its new text; forget: ID")
remember.add_argument("--project", action="store_true", help="add: only for this project (default: general; the "
                      "Governor: its own memory)")
remember.add_argument("--category", help="add, change: what it's about (preferences, about, work, tools, interests, "
                      "location, setup, decisions, other)")
run = commands.add_parser("run", help="run a command on one of the user's computers (or the command on stdin)")
run.add_argument("--on", dest="computer", help="the computer's name (default: the only one)")
run.add_argument("--cwd", help="folder to run it in (default: the home folder there)")
run.add_argument("--timeout", type=int, help="seconds, at most 600 (default 120)")
run.add_argument("words", nargs=argparse.REMAINDER, metavar="command", help="the command, after --")
pull = commands.add_parser("pull", help="copy a file from one of the user's computers to here")
pull.add_argument("--on", dest="computer")
pull.add_argument("remote")
pull.add_argument("local", nargs="?")
push = commands.add_parser("push", help="copy a file from here to one of the user's computers")
push.add_argument("--on", dest="computer")
push.add_argument("local")
push.add_argument("remote")
plugin = commands.add_parser("plugin", help="a tool of the user's plugins (relay help plugins says which)")
plugin.add_argument("tool", help="its name, e.g. notes_add")
plugin.add_argument("fields", nargs="*", help="its arguments: KEY=VALUE …, or one JSON object (or that on stdin)")
options = parser.parse_args()

if options.command == "fetch":
    call("fetch", {"url": options.url, "offset": options.offset}, timeout=300)
elif options.command == "search":
    call("search", {"query": " ".join(options.query), "category": options.category, "time_range": options.time,
                    "language": options.lang, "page": options.page})
elif options.command == "agents":
    call("agents", {})
elif options.command == "delegate":
    task = " ".join(options.task) if options.task else ("" if sys.stdin.isatty() else sys.stdin.read())
    call("delegate", {"task": task, "provider": options.provider, "model": options.model,
                      "mode": options.mode, "label": options.label, "effort": options.effort,
                      "wait": options.wait})
elif options.command == "wait":
    call("wait", {"ids": options.ids})
elif options.command == "tell":
    message = " ".join(options.message) if options.message else ("" if sys.stdin.isatty() else sys.stdin.read())
    call("tell", {"id": options.id, "message": message})
elif options.command == "changes":
    call("changes", {})
elif options.command == "diff":
    call("diff", {"turn": options.turn})
elif options.command == "restore":
    call("restore", {"turn": options.turn})
elif options.command == "commit":
    call("commit", {"message": options.message})
elif options.command == "merge":
    call("merge", {"id": options.id})
elif options.command == "agentos":
    fields = dict(field.split("=", 1) for field in options.fields if "=" in field)
    bare = [field for field in options.fields if "=" not in field]
    if bare and "id" not in fields:  # chat.read 00f6443a: the id
        fields["id"] = bare.pop(0)
    if "message" not in fields and not sys.stdin.isatty():
        piped = sys.stdin.read().strip()
        if piped:
            fields["message"] = piped
    call("agentos_" + options.capability.replace(".", "_").replace("-", "_"), fields)
elif options.command == "memory":
    words = list(options.words)
    if options.action == "add":
        text = " ".join(words) or ("" if sys.stdin.isatty() else sys.stdin.read())
        call("memory_add", {"text": text, "project": options.project, "category": options.category})
    elif options.action == "change":
        text = " ".join(words[1:]) or ("" if sys.stdin.isatty() or options.category else sys.stdin.read())
        call("memory_change", {"id": words[0] if words else "", "text": text, "category": options.category})
    elif options.action == "forget":
        call("memory_forget", {"id": words[0] if words else ""})
    else:
        call("memory_list", {})
elif options.command == "computers":
    call("computers", {})
elif options.command == "plugin":
    if len(options.fields) == 1 and options.fields[0].lstrip().startswith("{"):
        arguments = json.loads(options.fields[0])
    elif not options.fields and not sys.stdin.isatty():
        piped = sys.stdin.read().strip()
        arguments = json.loads(piped) if piped else {}
    else:
        arguments = {}
        for field in options.fields:
            key, _, value = field.partition("=")
            try:
                arguments[key] = json.loads(value)  # (a number, true, a list)
            except ValueError:
                arguments[key] = value
    call(options.tool, arguments, timeout=700)
elif options.command == "repair":
    if options.action == "check":
        call("repair_check", {}, timeout=1000)
    elif options.action == "plan":
        call("provider_plan", {"name": options.name, "program": options.program,
                               "plan": "" if sys.stdin.isatty() else sys.stdin.read()})
    elif options.action is None:
        sys.exit("relay repair check, or relay repair run -- COMMAND")
    else:
        import shlex
        words = options.words[1:] if options.words[:1] == ["--"] else options.words
        command = shlex.join(words) if words else ("" if sys.stdin.isatty() else sys.stdin.read())
        call("repair_run", {"command": command, "timeout": options.timeout}, timeout=700)
elif options.command == "run":
    words = options.words[1:] if options.words[:1] == ["--"] else options.words
    command = None if words else ("" if sys.stdin.isatty() else sys.stdin.read())
    # Separate words are quoted for the computer's own shell by Relay, which knows what that is.
    call("computer_run", {"computer": options.computer, "command": command, "argv": words or None,
                          "cwd": options.cwd, "timeout": options.timeout}, timeout=700)
elif options.command == "pull":
    import base64, os
    data = base64.b64decode(call("computer_read", {"computer": options.computer, "path": options.remote,
                                                   "encoding": "base64"}, quiet=True))
    target = options.local or os.path.basename(options.remote.replace("\\\\", "/")) or "pulled"
    with open(target, "wb") as out:
        out.write(data)
    print(f"{options.remote} -> {target} ({len(data)} bytes)")
elif options.command == "push":
    import base64
    with open(options.local, "rb") as source:
        content = base64.b64encode(source.read()).decode()
    call("computer_write", {"computer": options.computer, "path": options.remote, "content_b64": content})
else:  # help, or nothing: Relay says what's there for this conversation
    call("help", {"topic": getattr(options, "topic", None)})
'''
