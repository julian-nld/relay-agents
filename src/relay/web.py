"""What the route modules share: errors, request helpers, and how conversations are summarised."""

from __future__ import annotations

import base64
import binascii
import html
import json
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from relay import checkpoints, config, fallback, policy, relay_tools, skills, tasks
from relay.auth import AuthError
from relay.computers import ComputerError
from relay.containers import ContainerError
from relay.modes import ModeError
from relay.providers import KINDS, ProviderError, TemplateError, has_shell
from relay.repair import RepairError
from relay.runs import ProjectBusy, RunError
from relay.schedules import ScheduleError
from relay.uploads import UploadError

if TYPE_CHECKING:
    from relay.app import Relay


COOKIE = "relay_session"


CSRF_HEADER = "x-relay-request"

# Relay's own pages say so (static/js/api.js): a conversation made without it was started outside them
CLIENT_HEADER = "x-relay-client"

# An app password where Authorization is taken: behind a proxy with a sign-in of its own (Caddy's basic_auth)
APP_PASSWORD_HEADER = "x-relay-app-password"


SECRET_MASK = "********"


PUBLIC_API = {"/api/state", "/api/login", "/api/setup"}


TITLE_LENGTH = 60


CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
    "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
    "frame-ancestors 'none'; form-action 'self'"
)


DAV_CSP = "sandbox; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'"

# Relay's own page for an address with nothing at it (a 404), or a forwarded port with nothing behind it
# (relay.forward): the app's look and the theme chosen in this browser (theme.js), and a way back.
ERROR_PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="color-scheme" content="light dark">
  <meta name="relay-base" content="{base}">
  <title>{title} · Relay</title>
  <link rel="icon" href="{base}/static/icon.svg" type="image/svg+xml">
  <link rel="stylesheet" href="{base}/static/css/app.css">
  <script src="{base}/static/js/error-page.js" type="module"></script>
</head>
<body class="error-page">
  <main class="error-card">
    <a class="brand" href="{base}/"><img src="{base}/static/icon.svg" alt="">Relay</a>
    <p class="error-code">{status}</p>
    <h1>{title}</h1>
    {where}<p class="error-lead">{text}</p>
    <p class="error-actions">
      <button class="btn" type="button" data-back hidden>Go back</button>
      <a class="btn primary" href="{base}/">Back to Relay</a>
    </p>
  </main>
</body>
</html>
"""


def from_relay(headers: Any, public_url: str) -> bool:
    """Whether a browser's request comes from one of Relay's own pages (its Origin, when it says one): the
    origin's host is the one asked for (Host, or a reverse proxy's X-Forwarded-Host) or Relay's address
    (Settings). Not its scheme: behind a proxy that ends HTTPS the page is https, and Relay's own
    connection plain. No Origin: not a page's (a tool, curl). For websockets, which no CORS guards."""
    origin = headers.get("origin")
    if not origin:
        return True
    ours = [headers.get("host", ""), *headers.get("x-forwarded-host", "").split(","),
            urllib.parse.urlsplit(public_url).netloc]
    return _host(urllib.parse.urlsplit(origin).netloc) in {_host(h) for h in ours if h.strip()}


def _host(netloc: str) -> str:
    """A host as an origin writes it: lower case, without the scheme's own port."""
    netloc = netloc.strip().lower()
    for default in (":443", ":80"):
        if netloc.endswith(default):
            return netloc[:-len(default)]
    return netloc


def error_page(status: int, title: str, text: str, where: str = "", base: str = "") -> str:
    """The page as HTML: the status, a title, a sentence or two, and the address it's about (plain text). `base`:
    the path Relay is served under (Settings.base_path)."""
    shown = f'<p class="error-where">{html.escape(where)}</p>' if where else ""
    return ERROR_PAGE.format(status=status, title=html.escape(title), text=html.escape(text), where=shown,
                             base=html.escape(base))


def site_url(request: Any, settings: config.Settings) -> str:
    """Relay's address as this request reached it, with the path it's served under: http://host:port/relay/."""
    return f"{str(request.base_url).rstrip('/')}{settings.base_path}/"


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def local_url(settings: config.Settings) -> str:
    """Where agents on this machine reach Relay."""
    host = settings.host
    if host in ("", "0.0.0.0", "::", "localhost"):
        host = "127.0.0.1"
    elif ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{settings.port}"


def api(handler):
    """Wrap a handler: JSON errors, and pass the Relay object along."""

    async def endpoint(request: Request) -> Response:
        try:
            result = await handler(request, request.app.state.relay)
        except (ApiError, ProjectBusy) as exc:
            return JSONResponse({"error": str(exc)}, status_code=exc.status)
        except (AuthError, TemplateError, ProviderError, RunError, tasks.TaskError,
                UploadError, checkpoints.CheckpointError, ModeError, skills.SkillError,
                policy.PolicyError, fallback.FallbackError, ComputerError, ScheduleError, RepairError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except ContainerError as exc:  # (podman, a box that won't start: it says why and what to do)
            return JSONResponse({"error": str(exc)}, status_code=503)
        if isinstance(result, Response):
            return result
        return RelayJSON(result)

    return endpoint


# Fields that are a name or a path wherever they come: anything else (a list, an object) failed deep inside, with
# an error 500 (a fuzz of every route found it)
TEXT_FIELDS = ("model", "provider_id", "kind", "directory", "parent", "checkpoint", "effort", "agent_mode")


def strict_json(raw: bytes | str) -> Any:
    """JSON as the standard has it: NaN and Infinity (Python's json takes them) aren't numbers Relay can give back
    as JSON. Kept, a /timed with NaN made a chat that couldn't be opened any more (an error 500 for good)."""
    def refuse(name: str) -> Any:
        raise ValueError(f"{name} isn't a number JSON has")
    return json.loads(raw, parse_constant=refuse)


class RelayJSON(JSONResponse):
    """A response with a number JSON hasn't got (NaN, Infinity: a provider's output may say so) has null for it,
    rather than failing as a whole."""

    def render(self, content: Any) -> bytes:
        try:
            return super().render(content)
        except ValueError:
            return super().render(_finite(content))


def _finite(value: Any) -> Any:
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else None
    if isinstance(value, dict):
        return {k: _finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(v) for v in value]
    return value


async def body(request: Request) -> dict[str, Any]:
    try:
        data = strict_json(await request.body())
    except (ValueError, UnicodeDecodeError) as exc:  # (a JSONDecodeError is a ValueError)
        raise ApiError("Invalid JSON" if isinstance(exc, json.JSONDecodeError) else f"Invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ApiError("Expected a JSON object")
    for key in TEXT_FIELDS:
        if data.get(key) is not None and not isinstance(data[key], str):
            raise ApiError(f"{key} is text: a name or a path")
    if _nul(data):  # (no path, name or program's argument takes one: it failed where it went)
        raise ApiError("Text can't contain a NUL character (\\u0000)")
    return data


def _nul(value: Any) -> bool:
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(_nul(k) or _nul(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_nul(v) for v in value)
    return False


def user_id(request: Request) -> int:
    return request.state.user["id"]


def app_credentials(request: Request) -> tuple[str | None, str] | None:
    """An app password, as (the username, or None; the password): in X-Relay-App-Password (when a proxy's own sign-in
    has the Authorization header), else in Authorization: Bearer <password>, or Basic for the tools that only know
    that (curl -u NAME:PASSWORD). None when there's no such header."""
    if APP_PASSWORD_HEADER in request.headers:
        return None, request.headers[APP_PASSWORD_HEADER].strip()
    scheme, _, value = request.headers.get("authorization", "").strip().partition(" ")
    if scheme.lower() == "bearer":
        return None, value.strip()
    if scheme.lower() == "basic":
        try:
            username, _, password = base64.b64decode(value.strip(), validate=True).decode().partition(":")
        except (binascii.Error, UnicodeDecodeError):
            return None, ""
        return username, password
    return None


def proxy_sign_in(request: Request) -> bool:
    """Whether an Authorization header that is no app password of Relay's is a reverse proxy's own sign-in in front of
    it (Caddy's basic_auth, nginx's auth_basic): Basic, which the browser then sends with every request, and Relay's
    session alongside. Taken for an app password, it would leave Relay's sign-in going round in circles. (Not with an
    app password in X-Relay-App-Password: a wrong one there is a script's mistake.)"""
    return request.headers.get("authorization", "").strip().lower().startswith("basic ") and \
        APP_PASSWORD_HEADER not in request.headers and bool(request.cookies.get(COOKIE))


def started_outside(relay: Relay, request: Request) -> str | None:
    """Who started a conversation this request makes, when it's outside Relay's pages, in words (its mark in the
    lists says so); None for Relay's pages."""
    if request.headers.get(CLIENT_HEADER) == "web":
        return None
    script = relay.scripts.script_for(request.cookies.get(COOKIE))  # (a run on the Scripts page: its session)
    if script:
        return f"the script {script}"
    how = "the Python SDK" if request.headers.get("user-agent", "").startswith("relay-sdk/") else "Relay's API"
    app = getattr(request.state, "app_password", None)
    named = relay.db.one("SELECT name FROM app_passwords WHERE id = ?", (app,)) if app else None
    return f"{how}, with the app password “{named['name']}”" if named else how


def client_id(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def session_cookie(response: Response, relay: Relay, token: str) -> None:
    response.set_cookie(
        COOKIE, token, max_age=relay.settings.session_days * 86400, httponly=True,
        samesite="strict", secure=relay.settings.secure_cookies, path="/",
    )


def within_roots(relay: Relay, path: Path) -> bool:
    path = path.resolve()
    return any(path == root or path.is_relative_to(root) for root in relay.settings.roots())


# What masked() says about a template besides the template itself: a client may send it back as it got it
ABOUT_A_TEMPLATE = ("sign_in", "shell_hint", "shell")


def without_about(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if key not in ABOUT_A_TEMPLATE}


def masked(template) -> dict[str, Any]:
    data = template.to_dict()
    secrets_ = {f.name for f in KINDS[template.kind].FIELDS if f.secret}
    data["options"] = {k: (SECRET_MASK if k in secrets_ and v else v)
                       for k, v in data["options"].items()}
    if KINDS[template.kind].SIGN_IN:  # (it signs in again in its Shell: relay.creations provider_shell)
        data["sign_in"] = KINDS[template.kind].SIGN_IN
    if KINDS[template.kind].SHELL_HINT:
        data["shell_hint"] = KINDS[template.kind].SHELL_HINT
    data["shell"] = has_shell(template)
    return data


def has_tools(template) -> bool:
    """Whether the provider can work with tools (and so take on code tasks)."""
    try:
        return KINDS[template.kind](template).has_tools()
    except Exception:  # noqa: BLE001 - its kind gone, or a mistake in its folder: no tools known
        return False


def provider_detail(template) -> str | None:
    """A short note on what a provider runs against, for the settings list."""
    try:
        return KINDS[template.kind](template).detail()
    except Exception:  # noqa: BLE001 - its kind gone, or a mistake in its folder: nothing to note
        return None


def conversation_row(relay: Relay, request: Request) -> dict[str, Any]:
    row = relay.db.one("SELECT * FROM conversations WHERE id = ? AND user_id = ?",
                       (request.path_params["conversation_id"], user_id(request)))
    if row is None:
        raise ApiError("Conversation not found", 404)
    return row


def last_runs(relay: Relay, user: int) -> dict[str, dict[str, Any]]:
    """The latest finished run of each of a user's conversations."""
    rows = relay.db.all(
        "SELECT r.conversation_id, r.id, r.status, MAX(r.finished) AS finished FROM runs r "
        "JOIN conversations c ON c.id = r.conversation_id "
        "WHERE c.user_id = ? AND r.finished IS NOT NULL GROUP BY r.conversation_id",
        (user,),
    )
    return {r["conversation_id"]: {"id": r["id"], "status": r["status"], "finished": r["finished"]}
            for r in rows}


def queued_messages(relay: Relay, ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    """The messages waiting in each of these conversations, in one query."""
    found: dict[str, list[dict[str, Any]]] = {i: [] for i in ids}
    for start in range(0, len(ids), 500):  # SQLite's limit on parameters
        chunk = ids[start:start + 500]
        for q in relay.db.all(f"SELECT id, conversation_id, content FROM queued_messages WHERE conversation_id IN "
                              f"({','.join('?' * len(chunk))}) ORDER BY id", tuple(chunk)):
            found[q["conversation_id"]].append({"id": q["id"], "content": q["content"]})
    return found


def clones_of(relay: Relay, rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Which of these conversations work in a service's WorkBox: a clone's folder
    (production/<service>/work/<clone>/, relay.production), so the clone itself, a fork of it, a reviewer
    in it (agent.review) or a clone from before service_clones; and a code task made into a service
    (/promote), whose WorkBox is its own project folder. {conversation: {service_id, service, version,
    own, fresh}}: the version it was made from when that's known; `fresh`, a WorkBox made from the live
    version (it can be promoted); in a few queries for a whole list."""
    from relay.production import folders
    root = folders()
    found: dict[str, tuple[str, str | None]] = {}  # conversation -> (service, its clone's folder)
    for row in rows:
        try:
            parts = Path(row.get("directory") or "").relative_to(root).parts
        except ValueError:
            continue
        if len(parts) >= 3 and parts[1] == "work":
            found[row["id"]] = (parts[0], "/".join(parts[:3]))
    asked = [row["id"] for row in rows]
    linked = relay.db.all(f"SELECT conversation_id, service_id FROM service_clones WHERE conversation_id IN "
                          f"({','.join('?' * len(asked))})", tuple(asked)) if asked else []
    for link in linked:
        found.setdefault(link["conversation_id"], (link["service_id"], None))
    if not found:
        return {}
    ids = sorted({service for service, _ in found.values()})
    marks = ",".join("?" * len(ids))
    names = {s["id"]: s["name"]
             for s in relay.db.all(f"SELECT id, name FROM services WHERE id IN ({marks})", tuple(ids))}
    versions, own, fresh = {}, {}, set()  # a clone's folder -> the version it was made from; the WorkBoxes' own
    for c in relay.db.all("SELECT c.id, c.directory, v.number, k.from_version = s.live_version AS fresh "
                          "FROM service_clones k JOIN conversations c ON c.id = k.conversation_id "
                          "JOIN services s ON s.id = k.service_id "
                          f"LEFT JOIN service_versions v ON v.id = k.from_version WHERE k.service_id IN ({marks})",
                          tuple(ids)):
        own[c["id"]] = c["number"]
        if c["fresh"]:
            fresh.add(c["id"])
        try:
            versions["/".join(Path(c["directory"]).relative_to(root).parts[:3])] = c["number"]
        except ValueError:
            continue
    return {conversation: {"service_id": service, "service": names[service],
                           "version": own[conversation] if conversation in own else versions.get(folder),
                           "own": conversation in own,  # (else a fork of it, a reviewer in it)
                           "fresh": conversation in fresh}
            for conversation, (service, folder) in found.items() if service in names}


def computer_of(relay: Relay, row: dict[str, Any]) -> dict[str, Any] | None:
    """A computer task's computer: {id, name, online}; name None when it was removed."""
    if not row.get("computer_id"):
        return None
    found = relay.db.one("SELECT * FROM computers WHERE id = ?", (row["computer_id"],))
    if found is None:
        return {"id": row["computer_id"], "name": None, "online": False}
    public = relay.computers.get(row["user_id"], found["id"])
    return {"id": public["id"], "name": public["name"], "online": public["online"]}


def summary(relay: Relay, row: dict[str, Any], last_run: dict[str, Any] | None = None,
            queued: list[dict[str, Any]] | None = None, clones: dict[str, dict[str, Any]] | None = None) -> dict[str,
                                                                                                                 Any]:
    if queued is None:
        queued = queued_messages(relay, [row["id"]])[row["id"]]
    if clones is None:  # (a list passes them for all its conversations at once)
        clones = clones_of(relay, [row])
    return {
        "id": row["id"], "title": row["title"], "provider_id": row["provider_id"],
        "model": row["model"], "mode": row["mode"], "directory": row["directory"],
        "pinned": bool(row["pinned"]), "created": row["created"], "updated": row["updated"],
        "temporary": bool(row.get("temporary")),  # in no list, without memory; it goes when it's left
        "running_run": relay.runs.active_run_for(row["id"]),
        "git": {"mode": row["git_mode"], "branch": row["branch"], "base_branch": row["base_branch"],
                "repo_root": row["repo_root"], "state": row["task_state"]},
        "last_run": last_run,
        "forked_from": row.get("forked_from"),
        "agent_mode": row.get("agent_mode"),
        "effort": row.get("effort"),
        "instructions": row.get("instructions"),  # its own, after its mode's
        "tools": relay_tools.public(row),  # Relay's own tools its agents get: {kind: on}
        "needs_approval": relay.runs.waiting_for_answer(row["id"]),
        "limit": json.loads(row["limit_info"]) if row.get("limit_info") else None,
        "resume_at": row.get("resume_at"),
        "timed": json.loads(row["timed"]) if row.get("timed") else None,
        "fallback_loop": row.get("fallback_loop") or "auto",
        "computer": computer_of(relay, row),
        "cleared_after": row.get("cleared_after"),  # /clear: the agent's history starts after this message
        "queued": queued,
        # AgentOS (relay.agentos): made by the Governor, which may see, change and delete it; or the Governor.
        "managed": bool(row.get("managed")),
        "agentos_role": row.get("agentos_role"),
        # Started outside Relay's pages, by whom (a script or program through the API): its mark in the lists
        "external": row.get("external"),
        # A service's WorkBox (relay.production: clone): the sidebar folds a service's under its newest.
        "clone": clones.get(row["id"]),
    }


EFFORTS = ("low", "medium", "high", "xhigh", "max")


def public_share(share: dict[str, Any]) -> dict[str, Any]:
    return {"token": share["token"], "path": f"/dav/{share['token']}/", "directory": share["directory"],
            "name": share["name"], "read_only": share["read_only"], "created": share["created"]}


def make_title(text: str) -> str:
    line = " ".join(text.strip().split())
    return line if len(line) <= TITLE_LENGTH else line[: TITLE_LENGTH - 1].rstrip() + "…"


def owned_run(relay: Relay, request: Request) -> dict[str, Any]:
    run = relay.db.one(
        "SELECT r.* FROM runs r JOIN conversations c ON c.id = r.conversation_id "
        "WHERE r.id = ? AND c.user_id = ?",
        (request.path_params["run_id"], user_id(request)),
    )
    if run is None:
        raise ApiError("Run not found", 404)
    return run


def idle_task(relay: Relay, request: Request) -> dict[str, Any]:
    row = conversation_row(relay, request)
    if relay.runs.active_run_for(row["id"]):
        raise ApiError("Wait for the agent to finish (or stop it) first", 409)
    return row
