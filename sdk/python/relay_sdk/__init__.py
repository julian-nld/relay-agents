"""Relay's Python SDK: Relay's agents from your own Python code.

    from relay_sdk import *

    user = login("me", "my password", url="http://my-server:8420", provider="claude-code", model="sonnet")
    # or, without your password: an app password (Settings → Account), here or as $RELAY_APP_PASSWORD
    user = connect("http://my-server:8420", app_password="relay_…", provider="claude-code", model="sonnet")
    chat = convo.create(user, title="Site check", workdir="/home/me/projects/site",
                        instructions="Answer in Dutch.", tools={"web": False})
    message = chat.send("Run the tests and tell me what fails.")
    print(message.response, message.elapsed_time, message.tools_called)

    chat.switch(provider="opencode", model="opencode/big-pickle")   # the next turn is that agent's
    for chunk in chat.send("Fix the first one.", stream=True):       # the text as it comes
        print(chunk, end="", flush=True)

    chat.send("Make all the tests pass.", timed=60)                  # /timed: kept at it for up to an hour
    for turn in chat.follow():                                       # each turn, until it's quiet
        print(turn.response)
    print(chat.changes()["summary"])
    chat.commit("Fix the tests")

    run = scripts.run("public/report.py", ["--short"])               # your scripts (the Scripts page)
    for text in run:                                                 # its output as it's written
        print(text, end="")
    tasks.create("Daily report", "daily 08:00", script="public/report.py")   # at its times, as you
    print(services.get("site").status()["text"])                     # your live services (Production boxes)
    notify("The report is ready", title="Report")                    # to your phone (Settings → Notifications)

A Conversation is its id as a string too, so convo.send(convo_id, …) and the rest of 2.1 still work. A script
run from Relay's Scripts page is signed in already (RELAY_TOKEN): no login() there. Elsewhere, $RELAY_URL and
$RELAY_APP_PASSWORD sign a script in without login() either. Behind a proxy with a sign-in of its own (Caddy's
basic_auth), its name and password go in the address: https://name:password@my-relay.example. Only the standard
library; Python 3.9+. Relay serves this module for pip at http://<relay>/sdk/.
"""

from __future__ import annotations

import base64
import http.client
import json
import mimetypes
import os
import re
import shlex
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

__version__ = "4.2.0"
__all__ = ["Conversation", "Message", "Question", "RelayError", "Script", "ScriptRun", "Service", "Session", "Task",
           "connect", "conversation", "convo", "limits", "login", "models", "notify", "plugins", "providers",
           "scripts", "services", "tasks", "usage"]

DEFAULT_URL = "http://localhost:8420"
COOKIE = "relay_session"
AGENT = f"relay-sdk/{__version__}"  # (Relay says a conversation it starts was started by the Python SDK)
TIMEOUT = 60  # seconds a request (or a read of an answer as it comes: Relay sends a sign of life every 15 s) may take
QUIET = 1.5  # seconds follow() looks again before it calls a conversation quiet (a next turn starts at once)
SLACK = 0.15  # /timed's optimal by default: this much of max before it (as Relay's relay.timed)
TIME = re.compile(r"^(?:(\d+(?:\.\d+)?)h(?:(\d+)m?)?|(\d+(?:\.\d+)?)m?)$", re.I)

_default: Session | None = None


class RelayError(Exception):
    """What Relay refused or couldn't do, in its own words; `status`: the HTTP status, when there was one."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class Session:
    """A connection to one Relay: signed in (login), with an app password, or one without login (connect).
    `provider` and `model`: the defaults for new conversations."""

    def __init__(self, url: str | None = None, provider: str | None = None, model: str | None = None) -> None:
        self.url = (url or os.environ.get("RELAY_URL") or _url_file() or DEFAULT_URL).rstrip("/")
        if "://" not in self.url:  # (an address as a browser takes it: 192.168.1.2:8420)
            self.url = "http://" + self.url
        # A proxy's own sign-in in front of Relay (Caddy's basic_auth), in the address as https://name:password@host:
        # sent to the proxy with each call, and kept out of the address (and so out of errors and links)
        parts = urllib.parse.urlsplit(self.url)
        self._proxy: str | None = None
        if parts.username is not None:
            given = f"{urllib.parse.unquote(parts.username)}:{urllib.parse.unquote(parts.password or '')}"
            self._proxy = "Basic " + base64.b64encode(given.encode()).decode()
            self.url = urllib.parse.urlunsplit(parts._replace(netloc=parts.netloc.rpartition("@")[2]))
        self.username: str | None = None
        self.provider, self.model = provider, model
        self._cookie: str | None = None
        self._app_password: str | None = None

    def __repr__(self) -> str:
        return f"<relay_sdk.Session {self.username or '(no login)'} at {self.url}>"

    def request(self, method: str, path: str, data: Any = None, raw: bytes | None = None,
                headers: dict[str, str] | None = None) -> Any:
        """One call of Relay's API: its JSON (or text, for one that isn't), or RelayError."""
        body = raw if raw is not None else json.dumps(data).encode() if data is not None else None
        sent = {"x-relay-request": "1", "User-Agent": AGENT,
                **({"Content-Type": "application/json"} if data is not None else {}),
                **self._session_header(), **(headers or {})}
        try:
            with urllib.request.urlopen(urllib.request.Request(self.url + path, data=body, method=method,
                                                               headers=sent), timeout=TIMEOUT) as response:
                self._keep(response)
                payload = response.read()
                kind = response.headers.get_content_type()
        except urllib.error.HTTPError as exc:
            raise RelayError(_said(exc), exc.code) from None
        except urllib.error.URLError as exc:
            raise RelayError(f"Can't reach Relay at {self.url}: {exc.reason}") from None
        except (OSError, http.client.HTTPException) as exc:  # (a time-out, or the connection lost meanwhile)
            raise RelayError(f"Relay at {self.url} didn't answer: {exc or type(exc).__name__}") from None
        return json.loads(payload or b"null") if kind == "application/json" else payload.decode()

    def events(self, run_id: str) -> Iterator[dict[str, Any]]:
        """A run's events as they come (Relay's server-sent stream), until it ends."""
        for kind, data in self._stream(f"/api/runs/{run_id}/events", "an answer"):
            if kind is None and data:  # (named ones, such as caught-up, are Relay's page's)
                yield json.loads(data)

    def _stream(self, path: str, what: str) -> Iterator[tuple[str | None, str]]:
        """One of Relay's server-sent streams: each event's name (None for a plain one) and data, until "end"
        (that one too). `what` it brings, for an error."""
        request = urllib.request.Request(self.url + path,
                                         headers={"Accept": "text/event-stream", "User-Agent": AGENT,
                                                  **self._session_header()})
        try:
            response = urllib.request.urlopen(request, timeout=TIMEOUT)
        except urllib.error.HTTPError as exc:
            raise RelayError(_said(exc), exc.code) from None
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            raise RelayError(f"Can't reach Relay at {self.url}: {getattr(exc, 'reason', exc)}") from None
        with response:
            kind, data = None, []
            try:
                for raw in response:
                    line = raw.decode("utf-8").rstrip("\r\n")
                    if line:  # (id: and a sign of life, ": keep-alive", are passed by)
                        if line.startswith("event:"):
                            kind = line[6:].strip()
                        elif line.startswith("data:"):
                            data.append(line[5:].lstrip())
                        continue
                    yield kind, "\n".join(data)
                    if kind == "end":
                        return
                    kind, data = None, []
            except (OSError, http.client.HTTPException) as exc:  # (a time-out, or the connection lost)
                lost = f"Lost Relay at {self.url} while {what} came: {exc or type(exc).__name__}"
                raise RelayError(lost) from None

    def _session_header(self) -> dict[str, str]:
        if self._proxy:  # (Authorization is the proxy's: an app password goes in a header of Relay's own)
            own = {"X-Relay-App-Password": self._app_password} if self._app_password else \
                {"Cookie": f"{COOKIE}={self._cookie}"} if self._cookie else {}
            return {"Authorization": self._proxy, **own}
        if self._app_password:
            return {"Authorization": f"Bearer {self._app_password}"}
        return {"Cookie": f"{COOKIE}={self._cookie}"} if self._cookie else {}

    def _keep(self, response: Any) -> None:
        """The session's cookie, kept by hand: Relay may mark it Secure (secure_cookies), and it's sent anyway."""
        for header in response.headers.get_all("Set-Cookie") or []:
            name, _, rest = header.partition("=")
            if name.strip() == COOKIE:
                self._cookie = rest.split(";", 1)[0]


def login(username: str, password: str, url: str | None = None, provider: str | None = None,
          model: str | None = None) -> Session:
    """Signed in to Relay (at `url`, else $RELAY_URL, else a WorkBox's $RELAY_URL_FILE, else http://localhost:8420);
    the default session from now on.
    `provider`, `model`: the agent new conversations get when they don't say (else your latest conversation's)."""
    global _default
    session = Session(url, provider, model)
    session.request("POST", "/api/login", {"username": username, "password": password})
    session.username = username
    _default = session
    return session


def connect(url: str | None = None, provider: str | None = None, model: str | None = None,
            app_password: str | None = None) -> Session:
    """Signed in with an app password (`app_password`, else $RELAY_APP_PASSWORD: made in Relay's Settings →
    Account, for this script), or a Relay that runs without login (auth: none); the default session from now on."""
    global _default
    session = Session(url, provider, model)
    session._app_password = app_password or os.environ.get("RELAY_APP_PASSWORD") or None
    state = session.request("GET", "/api/state")
    if session._app_password and not state.get("user") and state.get("login_required"):
        raise RelayError(f"Relay at {session.url} doesn't know that app password (revoked? Settings → Account)", 401)
    if state.get("login_required") and not state.get("user"):
        raise RelayError(f"Relay at {session.url} asks for a login: login(username, password), or connect() with an "
                         "app password", 401)
    session.username = (state.get("user") or {}).get("username")
    _default = session
    return session


def _session(user: Session | None) -> Session:
    global _default
    if user is not None:
        return user
    if _default is None and os.environ.get("RELAY_TOKEN"):  # (a script on Relay's Scripts page: signed in)
        _default = Session()
        _default._cookie = os.environ["RELAY_TOKEN"]
    if _default is None and os.environ.get("RELAY_APP_PASSWORD"):  # (a script elsewhere, with an app password)
        connect()
    if _default is None:
        raise RelayError("Not signed in: login(username, password), or connect() with an app password, first")
    return _default


def _url_file() -> str | None:
    """Relay's address as a WorkBox has it (RELAY_URL_FILE: behind a VPN it's another than this machine's)."""
    try:
        return Path(os.environ["RELAY_URL_FILE"]).read_text().strip() or None
    except (KeyError, OSError):
        return None


def conversation(convo_id: str, user: Session | None = None) -> Conversation:
    """An existing conversation by its id, to work with."""
    return Conversation(convo_id, _session(user))


def providers(user: Session | None = None) -> list[dict[str, Any]]:
    """Relay's providers: id, name, enabled, tools (whether it works in folders), …"""
    return _session(user).request("GET", "/api/providers")["providers"]


def models(provider: str, user: Session | None = None) -> list[dict[str, Any]]:
    """A provider's models: id and name ("" is its default)."""
    return _session(user).request("GET", f"/api/providers/{urllib.parse.quote(provider)}/models")["models"]


def usage(days: int = 30, user: Session | None = None) -> dict[str, Any]:
    """What your agents cost over the last `days` (as Settings → Usage adds it up)."""
    return _session(user).request("GET", f"/api/usage?days={int(days)}")


def limits(fresh: bool = False, user: Session | None = None) -> list[dict[str, Any]]:
    """Per provider, how much is left of its usage windows (Settings → Usage), to pick a model with room;
    `fresh`: ask them now rather than within the last two minutes."""
    return _session(user).request("GET", "/api/usage/limits" + ("?fresh=1" if fresh else ""))


def plugins(user: Session | None = None) -> list[dict[str, Any]]:
    """The plugins in Relay's plugins folder (Settings → Plugins): name, label, version, whether it's on and
    loaded, and why not; their tools. A plugin's own API is at /api/plugins/<name>/…: Session.request()."""
    return _session(user).request("GET", "/api/plugins")["plugins"]


class Question:
    """What an agent asks while it works: a form (kind "form": its `title` and `fields`), or a permission
    (kind "permission": its `action`, `resources` and `options`). answer() it; a form takes {key: value} (or None
    to skip it), a permission True, False, or one of its options' id."""

    def __init__(self, message: Message, event: dict[str, Any]) -> None:
        self.message, self.event = message, event
        self.kind, self.id = event.get("type"), str(event.get("id"))
        self.title = event.get("title") or event.get("action") or ""
        self.fields = event.get("fields") or []
        self.action, self.resources = event.get("action"), event.get("resources") or []
        self.options = event.get("options") or []
        self.answered = False

    def __repr__(self) -> str:
        return f"<relay_sdk.Question {self.kind} {self.title!r}{' answered' if self.answered else ''}>"

    def answer(self, value: Any) -> None:
        session, run = self.message.session, self.message.run_id
        if self.kind == "form":
            session.request("POST", f"/api/runs/{run}/form", {"id": self.id, "answer": value})
        else:
            if isinstance(value, bool):
                if self.options:  # (the first that allows, or the last that doesn't)
                    allowing = [o for o in self.options if not str(o.get("outcome", "")).startswith("denied")]
                    value = (allowing[0] if value and allowing else self.options[-1])["id"]
                else:
                    value = "allow" if value else "deny"
            session.request("POST", f"/api/runs/{run}/permission", {"id": self.id, "decision": str(value)})
        self.answered = True


class Message:
    """One answer of an agent. Still coming (stream=True, follow()): iterate it for its text as it comes, or
    events() for all of it; what's below waits for its end. `on_question(q)` is called for each Question the
    agent asks meanwhile; what it returns is the answer (skip it with ...: then Relay's time for questions
    decides, Settings → Limits → Questions)."""

    def __init__(self, session: Session, conversation_id: str, run_id: str, started: float | None = None,
                 stored: dict[str, Any] | None = None,
                 on_question: Callable[[Question], Any] | None = None) -> None:
        self.session, self.conversation_id, self.run_id = session, str(conversation_id), run_id
        self._started = started
        self._elapsed: float | None = None
        self._events: list[dict[str, Any]] = list((stored or {}).get("events") or [])
        self._message = stored
        self._done = stored is not None
        self._text: str | None = None
        self._on_question = on_question
        self._questions: dict[str, Question] = {}

    def __repr__(self) -> str:  # (done: how it ended, so a failed answer doesn't read as a good one)
        ended = (self._message or {}).get("status") or "done"
        return f"<relay_sdk.Message {self.run_id[:8]} {ended if self._done else 'coming'}>"

    def __iter__(self) -> Iterator[str]:
        """The answer's text as it comes."""
        for event in self.events():
            if event.get("type") == "text" and event.get("delta"):
                yield event["delta"]

    def events(self) -> Iterator[dict[str, Any]]:
        """Each event of the answer (text, reasoning, tool_call, tool_result, form, permission, usage, done, …)
        as it comes."""
        if self._done:
            yield from self._events
            return
        for event in self.session.events(self.run_id):
            self._events.append(event)
            self._asked(event)
            yield event
        self._finish()

    def wait(self) -> Message:
        """Until the answer is done; itself."""
        for _ in self.events():
            pass
        return self

    @property
    def questions(self) -> list[Question]:
        """What the agent asked so far (answered or not)."""
        return list(self._questions.values())

    def answer(self, question: Question | str, value: Any) -> None:
        """Answer one of its questions by hand (while it's coming)."""
        found = question if isinstance(question, Question) else self._questions[str(question)]
        found.answer(value)

    def _asked(self, event: dict[str, Any]) -> None:
        if event.get("type") not in ("form", "permission") or not event.get("id"):
            return
        pending = event.get("status") == "pending" if event["type"] == "form" else event.get("decision") is None
        known = self._questions.get(str(event["id"]))
        if known is not None:
            known.event, known.answered = event, known.answered or not pending
            return
        question = self._questions[str(event["id"])] = Question(self, event)
        question.answered = not pending
        if pending and self._on_question is not None:
            value = self._on_question(question)
            if value is not ...:
                question.answer(value)

    def _finish(self) -> None:
        if self._started is not None:
            self._elapsed = time.monotonic() - self._started
        found = self.session.request("GET", f"/api/conversations/{self.conversation_id}")
        self._message = next((m for m in found["messages"] if m.get("run_id") == self.run_id
                              and m["role"] == "assistant"), None)
        if self._message is not None:
            self._events = list(self._message.get("events") or self._events)
        self._done = True

    @property
    def json(self) -> dict[str, Any]:
        """The answer as Relay keeps it: role, content, events, status, error, run_model, run_started, …"""
        self.wait()
        return self._message or {"role": "assistant", "run_id": self.run_id, "events": self._events}

    @property
    def response(self) -> str:
        """The answer's text: what the agent said."""
        return (self.json.get("content") or "").strip()

    @property
    def text(self) -> str:
        """The whole answer as Markdown, as Relay exports it: its text and tool calls where they happened."""
        if self._text is None:
            self.wait()
            self._text = self.session.request(
                "GET", f"/api/conversations/{self.conversation_id}/export.md?run={urllib.parse.quote(self.run_id)}")
        return self._text

    @property
    def started(self) -> float | None:
        """When its run started (Unix time, Relay's clock)."""
        return self.json.get("run_started")

    @property
    def finished(self) -> float | None:
        return self.json.get("run_finished")

    @property
    def elapsed_time(self) -> float | None:
        """Seconds its run took (Relay's measure; else from sending to its end)."""
        found = self.json
        if found.get("run_started") and found.get("run_finished"):
            return found["run_finished"] - found["run_started"]
        return self._elapsed

    @property
    def tools_called(self) -> list[dict[str, Any]]:
        """The tools the agent called, in order: name, input, ok (None while it ran) and output."""
        self.wait()
        results = {e.get("id"): e for e in self._events if e.get("type") == "tool_result"}
        called = []
        for event in self._events:
            if event.get("type") == "tool_call":
                result = results.get(event.get("id")) or {}
                called.append({"name": event.get("name"), "input": event.get("input"),
                               "ok": result.get("ok") if result else None, "output": result.get("output")})
        return called

    @property
    def status(self) -> str | None:
        """succeeded, failed or interrupted."""
        return self.json.get("status")

    @property
    def error(self) -> str | None:
        return self.json.get("error")

    @property
    def usage(self) -> dict[str, Any] | None:
        """Its tokens and cost, as the agent said them (the last usage event)."""
        self.wait()
        return next(({k: v for k, v in e.items() if k != "type"} for e in reversed(self._events)
                     if e.get("type") == "usage"), None)

    @property
    def cost(self) -> float:
        return float((self.usage or {}).get("cost") or 0)

    @property
    def model(self) -> str | None:
        return self.json.get("run_model")

    @property
    def provider(self) -> str | None:
        return self.json.get("run_provider")


class Conversation(str):
    """A chat or code task in Relay. It's its id as a string too (str(), and wherever an id goes); what's below
    asks Relay each time, so it's never out of date."""

    session: Session

    def __new__(cls, convo_id: str, session: Session | None = None) -> Conversation:
        made = super().__new__(cls, str(convo_id))
        made.session = _session(session)
        return made

    def __repr__(self) -> str:
        return f"<relay_sdk.Conversation {str(self)[:8]}>"

    @property
    def id(self) -> str:
        return str(self)

    def _call(self, method: str, path: str = "", data: Any = None) -> Any:
        return self.session.request(method, f"/api/conversations/{self}{path}", data)

    # What it is

    def get(self) -> dict[str, Any]:
        """As Relay gives it: its title, agent, folder, mode, instructions, tools, VPN, timer, queue, messages."""
        return self._call("GET")

    title = property(lambda self: self.get()["title"], lambda self, value: self.rename(value))
    provider = property(lambda self: self.get()["provider_id"])
    model = property(lambda self: self.get()["model"])
    agent_mode = property(lambda self: self.get()["agent_mode"])
    effort = property(lambda self: self.get()["effort"])
    mode = property(lambda self: self.get()["mode"], doc="chat, or code (a code task)")
    directory = property(lambda self: self.get()["directory"])
    running = property(lambda self: bool(self.get()["running_run"]))
    timer = property(lambda self: self.get()["timed"], doc="/timed's timer, if one was set")
    queued = property(lambda self: self.get()["queued"])
    temporary = property(lambda self: self.get()["temporary"], doc="a temporary chat: in no list, without memory")

    @property
    def instructions(self) -> str | None:
        """Its own instructions, after its mode's, for every agent in it (None: none)."""
        return self.get()["instructions"]

    @instructions.setter
    def instructions(self, text: str | None) -> None:
        self._call("PATCH", data={"instructions": text or ""})

    @property
    def tools(self) -> dict[str, bool]:
        """Relay's own tools its agents get: {"web", "memory", "subagents", "computers": on}. The agent's
        program keeps its own."""
        return self.get()["tools"]

    @tools.setter
    def tools(self, kinds: dict[str, bool]) -> None:
        self._call("PATCH", data={"tools": kinds})

    @property
    def vpn(self) -> str | None:
        """The VPN tunnel its folder goes through, by name (None: the machine's network)."""
        return self.get()["vpn"]

    @vpn.setter
    def vpn(self, tunnel: bool | str | None) -> None:
        """True (the only tunnel), a tunnel's name, or None to take its folder out again."""
        self._call("PATCH", data={"uses_vpn": tunnel or None})

    def messages(self) -> list[Message]:
        """Its answers so far, as Messages (each done)."""
        found = self.get()
        return [Message(self.session, self, m["run_id"], stored=m) for m in found["messages"]
                if m["role"] == "assistant" and m.get("run_id")]

    # Talking

    def send(self, prompt: str, stream: bool = False, files: list[str | Path] | None = None,
             timed: float | str | tuple | None = None,
             on_question: Callable[[Question], Any] | None = None) -> Message:
        """Send a message; the agent's answer: done (stream=False), or still coming (stream=True: iterate it).
        `files` (paths here) go along as attachments. `timed`: /timed, the agent kept at it — minutes or "1h30"
        (its most), or (min, max) or (min, max, optimal); follow() shows the turns that come of it.
        `on_question(q)`: answers what the agent asks meanwhile."""
        attachments = [_upload(self.session, Path(f)) for f in files or []]
        if timed is not None:
            low, high, best = _timer(timed)
            prompt = self._call("POST", "/timed", {"min": low, "max": high, "optimal": best, "task": prompt})["task"]
        started = time.monotonic()
        run = self._call("POST", "/messages", {"text": prompt, "attachments": attachments})
        message = Message(self.session, self, run["run_id"], started, on_question=on_question)
        return message if stream else message.wait()

    def queue(self, prompt: str, files: list[str | Path] | None = None) -> None:
        """Send it once the answer going now is done (at once when none is); follow() shows its answer."""
        attachments = [_upload(self.session, Path(f)) for f in files or []]
        self._call("POST", "/queue", {"text": prompt, "attachments": attachments})

    def follow(self, on_question: Callable[[Question], Any] | None = None) -> Iterator[Message]:
        """Each turn as it comes, the one going now first, until it's quiet: nothing running and nothing queued.
        Those are a timed request's go-ons, a fallback agent taking over at a limit, the queue's messages. A wait
        for a usage limit's reset ends it (get()["resume_at"] says until when)."""
        seen: set[str] = set()
        looked_again = False
        while True:
            found = self.get()
            run = found.get("running_run")
            if run and run not in seen:
                seen.add(run)
                looked_again = False
                message = Message(self.session, self, run, on_question=on_question)
                yield message
                message.wait()  # (in case it wasn't read)
                continue
            if run or found.get("queued"):
                time.sleep(0.3)
                continue
            if not looked_again:  # (the next turn starts as one ends: once more, a moment later)
                looked_again = True
                time.sleep(QUIET)
                continue
            return

    def wait(self, on_question: Callable[[Question], Any] | None = None) -> list[Message]:
        """Until it's quiet (see follow()); the answers that came meanwhile."""
        return list(self.follow(on_question))

    def retry(self, message: Message | None = None) -> Message:
        """Answer again: the last message, or the one `message` answered (what came after it goes). Done."""
        data = {} if message is None else {"message_id": self._prompt_of(message)}
        return self._answer(self._call("POST", "/retry", data))

    def edit(self, message: Message | int, text: str) -> Message:
        """Change what was asked (the message an answer answered, or a message's id) and answer it anew: what
        came after it goes. Done."""
        prompt = self._prompt_of(message) if isinstance(message, Message) else int(message)
        return self._answer(self._call("POST", "/retry", {"message_id": prompt, "text": text}))

    def fork(self, at: Message | None = None) -> Conversation:
        """A new conversation with this one's messages (up to and with the answer `at`), to go on separately."""
        made = self._call("POST", "/fork", {} if at is None else {"message_id": at.json["id"]})
        return Conversation(made["id"], self.session)

    def go_on(self, provider: str | None = None, model: str | None = None) -> Message:
        """After a model stopped (a usage limit): go on now, with the same agent or another. Done."""
        data = {**({"provider_id": provider} if provider else {}), **({"model": model} if model is not None else {})}
        return self._answer(self._call("POST", "/continue", data))

    def resume_later(self) -> None:
        """Go on by itself when the usage limit resets."""
        self._call("POST", "/resume", {"when": "reset"})

    def cancel_resume(self) -> None:
        self._call("POST", "/resume", {"cancel": True})

    def stop_timer(self) -> None:
        """End /timed's timer: the agent may stop when it likes."""
        self._call("POST", "/timed", {"off": True})

    def stop(self) -> bool:
        """Stop its agent, when one works; whether one did."""
        running = self.get().get("running_run")
        return bool(running) and bool(self.session.request("POST", f"/api/runs/{running}/stop").get("stopped"))

    # Changing it

    def switch(self, provider: str | None = None, model: str | None = None, effort: str | None = None,
               agent_mode: str | None = None, fallback_loop: str | None = None) -> None:
        """Another agent from the next turn on: provider (the conversation is handed over; its model is that
        provider's default unless `model` says), model, effort ("default", "low" … "max"), mode (its id, or
        "build"), fallback loop (its id, "auto" or "off")."""
        data: dict[str, Any] = {}
        if provider is not None:
            data["provider_id"], data["model"] = provider, model or ""
        elif model is not None:
            data["model"] = model
        for key, value in (("effort", effort), ("agent_mode", agent_mode), ("fallback_loop", fallback_loop)):
            if value is not None:
                data[key] = value
        if data:
            self._call("PATCH", data=data)

    def rename(self, title: str) -> None:
        self._call("PATCH", data={"title": title})

    def pin(self, pinned: bool = True) -> None:
        self._call("PATCH", data={"pinned": pinned})

    def keep(self) -> None:
        """A temporary chat kept: a chat like any other from now on (in the list, with memory)."""
        self._call("PATCH", data={"temporary": False})

    def clear(self) -> None:
        """/clear: the agent starts afresh (it forgets the conversation so far); the messages stay."""
        self._call("POST", "/clear")

    def delete(self) -> None:
        self._call("DELETE")

    # A code task's work

    def changes(self) -> dict[str, Any]:
        """Its changes: files (each with its diff), a summary (files, additions, deletions), its branch."""
        return self._call("GET", "/changes")

    def commit(self, message: str) -> str:
        """Commit its folder to your repository (its current branch); the commit."""
        return self._call("POST", "/commit", {"message": message})["commit"]

    def merge(self) -> str:
        """A task on a branch of its own (isolate=True): merge it into the branch it came from; that branch."""
        return self._call("POST", "/merge")["merged_into"]

    def discard(self) -> None:
        """Throw its changes away (its branch, or its files as they were)."""
        self._call("POST", "/discard")

    def rewind(self, to: Message | str = "start") -> dict[str, Any]:
        """Its files as they were before an answer (a Message), or at its start ("start"). Relay's record keeps
        the files of now: what it gives back has `undo`, a checkpoint to go back to."""
        data = {"to_start": True} if to == "start" else {"run_id": to.run_id}
        return self._call("POST", "/rewind", data)

    def reviewed(self) -> None:
        """Its changes are seen (the tasks overview counts from here)."""
        self._call("POST", "/reviewed")

    # Within

    def _answer(self, run: dict[str, Any]) -> Message:
        return Message(self.session, self, run["run_id"], time.monotonic()).wait()

    def _prompt_of(self, answer: Message) -> int:
        """The id of the message an answer answered (the user's message before it)."""
        messages = self.get()["messages"]
        mine = next((i for i, m in enumerate(messages) if m.get("run_id") == answer.run_id), None)
        asked = [m for m in messages[:mine] if m["role"] == "user"] if mine is not None else []
        if not asked:
            raise RelayError("That answer isn't in this conversation")
        return asked[-1]["id"]


class _Conversations:
    """convo: Relay's chats and code tasks. Each takes user= (a Session), else the default one."""

    def create(self, user: Session | None = None, title: str | None = None, workdir: str | None = None,
               uses_vpn: bool | str = False, provider: str | None = None, model: str | None = None,
               effort: str | None = None, agent_mode: str | None = None, instructions: str | None = None,
               tools: dict[str, bool] | None = None, isolate: bool = False,
               fallback_loop: str | None = None, temporary: bool = False) -> Conversation:
        """A new conversation (it's its id as a string too). With `workdir` (a folder on Relay's machine, in a
        workspace root) a code task there, `isolate`: on a branch of its own (when it can't be, in the folder
        itself, with a warning why); else a chat.
        - provider, model: the agent; by default the session's (login), else your latest conversation's.
        - effort ("low" … "max"), agent_mode (a mode's id), fallback_loop: else their defaults.
        - instructions: its own, after its mode's. tools: Relay's own tools on or off ({"web": False}).
        - uses_vpn: True (the only VPN tunnel) or a tunnel's name: its folder goes through it.
        - temporary: a temporary chat (no folder): in no list, without Relay's memory, no title asked for; delete()
          it when done (Relay removes one left behind after a while)."""
        session = _session(user)
        if provider is None:
            provider, default_model = _default_agent(session, code=bool(workdir))
            model = default_model if model is None else model
        data: dict[str, Any] = {"provider_id": provider, "mode": "code" if workdir else "chat"}
        if workdir:
            data["directory"], data["isolate"] = str(workdir), bool(isolate)
        for key, value in (("title", title), ("model", model), ("effort", effort), ("agent_mode", agent_mode),
                           ("instructions", instructions), ("tools", tools), ("uses_vpn", uses_vpn),
                           ("fallback_loop", fallback_loop), ("temporary", temporary)):
            if value:
                data[key] = value
        made = session.request("POST", "/api/conversations", data)
        if made.get("in_place"):  # (a branch of its own asked for: why it works in the folder itself)
            warnings.warn(f"Not on a branch of its own, in the folder itself: {made['in_place']}", RuntimeWarning,
                          stacklevel=2)
        return Conversation(made["id"], session)

    def send(self, convo_id: str, prompt: str, stream: bool = False, files: list[str | Path] | None = None,
             user: Session | None = None, **more: Any) -> Message:
        """Conversation.send, for an id."""
        return Conversation(convo_id, user).send(prompt, stream=stream, files=files, **more)

    def list(self, user: Session | None = None, query: str | None = None) -> list[dict[str, Any]]:
        """The conversations, newest first (as Relay's sidebar has them); `query` searches titles and messages."""
        path = "/api/conversations" + (f"?q={urllib.parse.quote(query)}" if query else "")
        return _session(user).request("GET", path)

    def get(self, convo_id: str, user: Session | None = None) -> dict[str, Any]:
        return Conversation(convo_id, user).get()

    def messages(self, convo_id: str, user: Session | None = None) -> list[Message]:
        return Conversation(convo_id, user).messages()

    def rename(self, convo_id: str, title: str, user: Session | None = None) -> None:
        Conversation(convo_id, user).rename(title)

    def stop(self, convo_id: str, user: Session | None = None) -> bool:
        return Conversation(convo_id, user).stop()

    def delete(self, convo_id: str, user: Session | None = None) -> None:
        Conversation(convo_id, user).delete()

    def providers(self, user: Session | None = None) -> list[dict[str, Any]]:
        return providers(user)

    def models(self, provider: str, user: Session | None = None) -> list[dict[str, Any]]:
        return models(provider, user)


convo = _Conversations()


class Script(str):
    """One of your scripts on Relay's Scripts page, by its name: private/x.py, public/x.py, or one of Relay's
    examples (public/examples/x.py); x.py alone is private/'s, else public/'s. It's its name as a string too; what's
    below asks Relay each time."""

    session: Session

    def __new__(cls, name: str, session: Session | None = None) -> Script:
        made = super().__new__(cls, str(name))
        made.session = _session(session)
        return made

    def __repr__(self) -> str:
        return f"<relay_sdk.Script {self!s}>"

    @property
    def name(self) -> str:
        return str(self)

    def _call(self, method: str, path: str = "", data: Any = None) -> Any:
        return self.session.request(method, f"/api/scripts/{urllib.parse.quote(str(self))}{path}", data)

    @property
    def text(self) -> str:
        """Its code."""
        return self._call("GET")["text"]

    def save(self, text: str) -> None:
        """Its code, saved over what it was (scripts.create makes a new one, never over one that's there)."""
        self._call("PUT", data={"text": text})

    def run(self, args: str | list[str] = "") -> ScriptRun:
        """Run it, as the page's Run does (in its folder's WorkBox, signed in as you): its run at once, going on.
        `args`: as typed in the page's field, or a list of them (each quoted)."""
        typed = " ".join(shlex.quote(str(a)) for a in args) if isinstance(args, (list, tuple)) else str(args or "")
        return ScriptRun(self._call("POST", "/run", {"args": typed})["run"], self.session)

    def runs(self) -> list[ScriptRun]:
        """Its last runs, newest first."""
        return [ScriptRun(found["id"], self.session) for found in self._call("GET")["runs"]]

    def rename(self, to: str) -> Script:
        """Called otherwise, in its folder (or with one: private/y.py); the script as it's called now."""
        return Script(self._call("POST", "/rename", {"to": to})["name"], self.session)

    def move(self, to: str) -> Script:
        """Into "private" (yours alone) or "public" (agents may read it, AgentOS's Governor may run it): the script
        there, under its name, else the next one free (x_2.py)."""
        return Script(self._call("POST", "/move", {"to": to})["name"], self.session)

    def copy(self, to: str = "private", name: str | None = None) -> Script:
        """A copy, as a new script of yours in "private" or "public", called `name` or as this one (else the next
        name free): the copy."""
        return Script(self._call("POST", "/copy", {"to": to, **({"name": name} if name else {})})["name"],
                      self.session)

    def delete(self) -> None:
        """It and its runs (the conversations it started stay)."""
        self._call("DELETE")


class ScriptRun(str):
    """A run of a script (Script.run, Script.runs): its id as a string too. Iterate it for its output as it's
    written; what's below waits for its end, but get() (as it is now)."""

    session: Session

    def __new__(cls, run_id: str, session: Session | None = None) -> ScriptRun:
        made = super().__new__(cls, str(run_id))
        made.session = _session(session)
        made._ended = False
        return made

    def __repr__(self) -> str:
        return f"<relay_sdk.ScriptRun {str(self)[:8]}>"

    @property
    def id(self) -> str:
        return str(self)

    def __iter__(self) -> Iterator[str]:  # type: ignore[override]
        """Its output as it's written, from its start, until it ends."""
        for kind, data in self.session._stream(f"/api/scripts/runs/{self}/output", "a script's output"):
            if kind is None and data:
                yield json.loads(data)["text"]
        self._ended = True

    def get(self) -> dict[str, Any]:
        """As Relay has it now: script, args, started, finished, status (running, succeeded, failed, stopped,
        timed out), exit_code, output, conversations (id and title)."""
        return self.session.request("GET", f"/api/scripts/runs/{self}")

    def wait(self) -> ScriptRun:
        """Until it has ended; itself."""
        if not self._ended:
            for _ in self:
                pass
        return self

    @property
    def status(self) -> str:
        return self.wait().get()["status"]

    @property
    def exit_code(self) -> int | None:
        return self.wait().get()["exit_code"]

    @property
    def output(self) -> str:
        """What it printed (its last 200,000 characters)."""
        return self.wait().get()["output"]

    @property
    def conversations(self) -> list[Conversation]:
        """The conversations it started."""
        return [Conversation(found["id"], self.session) for found in self.wait().get()["conversations"]]

    @property
    def script(self) -> Script:
        return Script(self.get()["script"], self.session)

    def stop(self) -> bool:
        """Stop it, and the agents of the conversations it started; whether it was going."""
        return self.session.request("POST", f"/api/scripts/runs/{self}/stop")["stopped"]


class _Scripts:
    """scripts: yours on Relay's Scripts page. Each takes user= (a Session), else the default one."""

    def list(self, user: Session | None = None) -> list[dict[str, Any]]:
        """Each: name, private, example, size, modified, running."""
        return _session(user).request("GET", "/api/scripts")["scripts"]

    def get(self, name: str, user: Session | None = None) -> Script:
        """One by its name: private/x.py, public/x.py, public/examples/x.py."""
        return Script(name, _session(user))

    def create(self, name: str, text: str, user: Session | None = None) -> Script:
        """A new script (never over one that's there: Script.save saves over it)."""
        session = _session(user)
        made = session.request("POST", f"/api/scripts/{urllib.parse.quote(name)}", {"text": text})
        return Script(made["name"], session)

    def run(self, name: str, args: str | list[str] = "", user: Session | None = None) -> ScriptRun:
        """Run one: Script.run."""
        return Script(name, _session(user)).run(args)

    def write(self, description: str, provider: str | None = None, model: str | None = None,
              user: Session | None = None) -> Conversation:
        """One an agent writes (the page's With an agent): a code task in public/ with Relay's skill for it, going
        on; its conversation (wait() for it). provider, model: the session's, else your latest code task's."""
        session = _session(user)
        if provider is None:
            provider, default_model = _default_agent(session, code=True)
            model = default_model if model is None else model
        made = session.request("POST", "/api/scripts/write",
                               {"description": description, "provider_id": provider, "model": model or ""})
        return Conversation(made["conversation"], session)


scripts = _Scripts()


class Task(str):
    """A scheduled task (on the AgentOS page): its id as a string too; what's below asks Relay each time."""

    session: Session

    def __new__(cls, task_id: str, session: Session | None = None) -> Task:
        made = super().__new__(cls, str(task_id))
        made.session = _session(session)
        return made

    def __repr__(self) -> str:
        return f"<relay_sdk.Task {str(self)[:8]}>"

    @property
    def id(self) -> str:
        return str(self)

    def _call(self, method: str, path: str = "", data: Any = None) -> Any:
        return self.session.request(method, f"/api/schedules/{self}{path}", data)

    def get(self) -> dict[str, Any]:
        """As Relay has it: name, when, next_run, prompt or script, provider_id, model, enabled, last_run,
        last_error, outcome (an AgentOS task's, or a script's: done or failed) and outcome_note."""
        return self._call("GET")

    def update(self, **fields: Any) -> Task:
        """Change it: name=, when=, prompt=, script=, args=, provider=, model=, workdir=, enabled=, … (as
        tasks.create takes them); the rest stays."""
        self._call("PUT", data=_task_fields(fields))
        return self

    def run(self) -> Conversation | ScriptRun | None:
        """Run it now (its schedule stays): the conversation it went to, or a script task's run."""
        ran = self._call("POST", "/run")
        if ran.get("script_run"):
            return ScriptRun(ran["script_run"], self.session)
        return Conversation(ran["conversation_id"], self.session) if ran.get("conversation_id") else None

    def delete(self) -> None:
        self._call("DELETE")


class _Tasks:
    """tasks: scheduled tasks, as on the AgentOS page: a prompt for an agent, or one of your scripts, at their
    times. Each takes user= (a Session), else the default one."""

    def list(self, user: Session | None = None) -> list[dict[str, Any]]:
        return _session(user).request("GET", "/api/schedules")

    def get(self, task_id: str, user: Session | None = None) -> Task:
        return Task(task_id, _session(user))

    def create(self, name: str, when: str | dict[str, Any], prompt: str | None = None, script: str | None = None,
               args: str | None = None, provider: str | None = None, model: str | None = None,
               workdir: str | None = None, governed: bool = False, enabled: bool = True,
               user: Session | None = None, **more: Any) -> Task:
        """A new scheduled task.
        - when: in words ("daily 08:00", "weekdays 09:00", "weekly mon,wed 08:00", "every 6 hours",
          "once 2026-10-05 09:00", "in 10 minutes") or as Relay keeps it ({"kind": "daily", "time": "08:00"}).
        - prompt: what an agent is asked each time; provider, model: the session's, else your latest
          conversation's; workdir: a code task in that folder.
        - script: one of your scripts (private/x.py, public/x.py), run as you with no agent; args: its arguments.
        - governed: an AgentOS task, the Governor picks who does it.
        - more: other fields as Relay takes them (agent_mode, effort, timezone, isolate, …)."""
        session = _session(user)
        fields: dict[str, Any] = {"name": name, "when": when, "enabled": enabled, "script": script, "args": args,
                                  "prompt": prompt, "workdir": workdir, **more}
        if not script and governed:
            fields["governed"] = True
        elif not script:
            if provider is None:
                provider, default_model = _default_agent(session, code=bool(workdir))
                model = default_model if model is None else model
            fields.update(provider=provider, model=model)
        return Task(session.request("POST", "/api/schedules", _task_fields(fields))["id"], session)


tasks = _Tasks()


def _task_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """A task's fields as the SDK names them, as Relay's API does (provider_id, script_args, a code task's
    directory); those left None out."""
    names = {"provider": "provider_id", "args": "script_args"}
    data = {names.get(key, key): value for key, value in fields.items() if value is not None and key != "workdir"}
    if fields.get("workdir"):
        data.update(mode="code", directory=str(fields["workdir"]))
    return data


class Service(str):
    """One of your live services (Production boxes), by its id: its id as a string too."""

    session: Session

    def __new__(cls, service_id: str, session: Session | None = None) -> Service:
        made = super().__new__(cls, str(service_id))
        made.session = _session(session)
        return made

    def __repr__(self) -> str:
        return f"<relay_sdk.Service {str(self)[:8]}>"

    @property
    def id(self) -> str:
        return str(self)

    def _call(self, method: str, path: str = "", data: Any = None) -> Any:
        return self.session.request(method, f"/api/services/{self}{path}", data)

    def get(self) -> dict[str, Any]:
        """As Relay has it: name, its live version, versions, snapshots (of its data), address, health, …"""
        return self._call("GET")

    def status(self) -> dict[str, Any]:
        """How it is now: running, well or not, …; `text` says it in words."""
        return self._call("GET", "/status")

    def logs(self) -> str:
        """Its log's last lines."""
        return self._call("GET", "/logs")["logs"]

    def snapshots(self) -> list[dict[str, Any]]:
        """Its data's snapshots, newest first: name, kind (version, daily, restore), at, bytes."""
        return self.get()["snapshots"]

    def start(self) -> str:
        """What Relay did, in words (each of these)."""
        return self._call("POST", "/start")["message"]

    def stop(self) -> str:
        return self._call("POST", "/stop")["message"]

    def restart(self) -> str:
        return self._call("POST", "/restart")["message"]

    def rollback(self, version: int | None = None) -> str:
        """Back to its version before the live one, or to `version` (its number)."""
        return self._call("POST", "/rollback", {} if version is None else {"version": version})["message"]

    def restore(self, snapshot: str) -> str:
        """Its data put back as a snapshot (snapshots()) has it; the data before is kept as one too."""
        return self._call("POST", "/restore", {"snapshot": snapshot})["message"]


class _Services:
    """services: your live services (Production boxes). Each takes user= (a Session), else the default one.
    Making, deleting and promoting them stays on their page."""

    def list(self, user: Session | None = None) -> list[dict[str, Any]]:
        return _session(user).request("GET", "/api/services")

    def get(self, service: str, user: Session | None = None) -> Service:
        """One by its name or id."""
        session = _session(user)
        return Service(session.request("GET", f"/api/services/{urllib.parse.quote(service)}")["id"], session)


services = _Services()


def notify(text: str, title: str | None = None, priority: str | None = None, url: str | None = None,
           user: Session | None = None) -> bool:
    """A notification to your phone or a webhook (Settings → Notifications: ntfy or a webhook, the category From
    your scripts); whether it went. Not set up or turned off doesn't stop a script: a warning says why.
    priority: min, low, default (normal), high or urgent; url: opened when it's tapped."""
    said = _session(user).request("POST", "/api/notifications",
                                  {"text": text, "title": title, "priority": priority, "url": url})
    if not said.get("sent"):
        warnings.warn(f"Not notified: {said.get('why')}", RuntimeWarning, stacklevel=2)
    return bool(said.get("sent"))


def _default_agent(session: Session, code: bool) -> tuple[str, str | None]:
    """The agent for a new conversation that doesn't say: the session's; else your latest conversation's (one
    whose provider works in folders, for a code task); else the first provider that can."""
    enabled = {p["id"]: p for p in providers(session) if p.get("enabled") and (p.get("tools") or not code)}
    if session.provider:
        return session.provider, session.model
    for row in convo.list(session):
        if row.get("provider_id") in enabled:
            return row["provider_id"], row.get("model")
    if not enabled:
        raise RelayError("No provider in Relay can do this (Settings → Providers)")
    return next(iter(enabled)), None


def _timer(timed: float | str | tuple | list) -> tuple[float | None, float, float]:
    """/timed's minutes: (min, max, optimal), as Relay's relay.timed reads them."""
    if isinstance(timed, (tuple, list)):
        if not 2 <= len(timed) <= 3:
            raise RelayError("timed: minutes, or (min, max), or (min, max, optimal)")
        low, high = _minutes(timed[0]), _minutes(timed[1])
        return low, high, _minutes(timed[2]) if len(timed) == 3 else low
    high = _minutes(timed)
    return None, high, high * (1 - SLACK)


def _minutes(value: float | str) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    match = TIME.match(str(value).strip())
    if not match:
        raise RelayError(f"Not a time: {value!r} (minutes, or as 1h30)")
    hours, rest, plain = match.groups()
    return float(hours) * 60 + float(rest or 0) if hours is not None else float(plain)


def _upload(session: Session, path: Path) -> str:
    kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    made = session.request("POST", "/api/uploads", raw=path.read_bytes(),
                           headers={"X-Filename": urllib.parse.quote(path.name), "Content-Type": kind})
    return made["id"]


def _said(exc: urllib.error.HTTPError) -> str:
    """Relay's own words for an error (its JSON's error), else the HTTP reason."""
    try:
        return json.loads(exc.read() or b"{}").get("error") or f"HTTP {exc.code} {exc.reason}"
    except ValueError:
        return f"HTTP {exc.code} {exc.reason}"
