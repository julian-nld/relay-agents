"""Signing in and out, and the page itself."""

from __future__ import annotations

import asyncio
import json
import secrets
import shutil
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from relay import __version__, config, containers, forward
from relay.providers import KINDS
from relay.web import COOKIE, CSP, ApiError, body, client_id, session_cookie, user_id

if TYPE_CHECKING:
    from relay.app import Relay


async def index(request: Request) -> Response:
    # xterm creates per-terminal styles; a nonce permits those without allowing arbitrary inline CSS.
    nonce = secrets.token_urlsafe(24)
    html = await asyncio.to_thread((config.static_dir() / "index.html").read_text)
    # (Relay under a path of its site: its own links have it, static/js/base.js)
    base = request.app.state.relay.settings.base_path
    return HTMLResponse(html.replace("@STYLE_NONCE@", nonce).replace("@BASE@", base), headers={
        "Content-Security-Policy": CSP.replace("style-src 'self'", f"style-src 'self' 'nonce-{nonce}'"),
        "Cache-Control": "no-cache",
    })


async def state(request: Request, relay: Relay):
    return {
        "user": request.state.user,
        "login_required": relay.login_required,
        "needs_setup": relay.needs_setup(),
        "version": __version__,
        "trial": relay.settings.trial or None,  # (a new provider's trial Relay: its banner)
        # (the page's banner while Relay's images are built: at the first start, a few minutes)
        "setting_up": relay.setting_up,
        "building": list(containers.BUILDING.values()),
        # (links in agents' replies to a server here become /port/<n>/: static/js/markdown.js)
        "server": {"port": relay.settings.port, "addresses": list(await asyncio.to_thread(forward.addresses))}
        if request.state.user else None,
        # AgentOS part of this Relay (Settings → AgentOS): out, the UI has none of it
        "agentos": relay.agentos.settings(request.state.user["id"])["included"] if request.state.user else None,
        # What a new chat starts with: the provider, model and effort picked for one last, on any device
        "new_chat": new_chat(relay, request.state.user["id"]) if request.state.user else None,
    }


NEW_CHAT = "new_chat"  # (preferences: what a user's new chats start with)


def new_chat(relay: Relay, user: int) -> dict[str, Any] | None:
    row = relay.db.one("SELECT value FROM preferences WHERE user_id = ? AND key = ?", (user, NEW_CHAT))
    if row:
        return json.loads(row["value"])
    # Nothing picked yet: the first provider whose programs are installed (a new Relay has Claude Code's template
    # whether Claude Code is there or not)
    for template in relay.registry.templates():
        if template.enabled and all(shutil.which(program) for program in KINDS[template.kind].PROGRAMS):
            return {"provider_id": template.id, "model": None, "effort": None}
    return None


async def set_new_chat(request: Request, relay: Relay):
    """The provider, model and effort a user picked for a new chat: their next new chats start with them, in every
    browser (a browser kept them for itself only)."""
    data = await body(request)
    choice = {}
    for key in ("provider_id", "model", "effort"):
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            raise ApiError(f"{key}: a string, or null")
        choice[key] = value or None
    if choice["provider_id"] is None:
        raise ApiError("provider_id: the provider picked")
    relay.db.execute("INSERT INTO preferences (user_id, key, value) VALUES (?, ?, ?) ON CONFLICT (user_id, key) "
                     "DO UPDATE SET value = excluded.value", (user_id(request), NEW_CHAT, json.dumps(choice)))
    return choice


async def setup(request: Request, relay: Relay):
    """Create the first account (Relay runs on a trusted local network)."""
    data = await body(request)
    if not relay.needs_setup():
        raise ApiError("Relay is already set up", 409)
    username, password = str(data.get("username", "")), str(data.get("password", ""))
    # After running without login, the first real account takes over the
    # 'local' account's conversations.
    user = relay.accounts.claim_local_user(username, password)
    if user is None:
        user = relay.accounts.create_user(username, password)
    response = JSONResponse({"ok": True})
    session_cookie(response, relay, relay.accounts.start_session(user, relay.settings.session_days))
    return response


async def login(request: Request, relay: Relay):
    data = await body(request)
    client = client_id(request)
    wait = relay.throttle.begin(client)
    if wait > 0:
        raise ApiError(f"Too many attempts; try again in {int(wait) + 1} seconds", 429)
    try:
        user = await asyncio.to_thread(
            relay.accounts.authenticate, str(data.get("username", "")), str(data.get("password", ""))
        )
    except BaseException:
        relay.throttle.failed(client)
        raise
    if user is None:
        relay.throttle.failed(client)
        raise ApiError("Wrong username or password", 401)
    relay.throttle.succeeded(client)
    response = JSONResponse({"ok": True})
    session_cookie(response, relay, relay.accounts.start_session(user["id"], relay.settings.session_days))
    return response


async def logout(request: Request, relay: Relay):
    relay.accounts.end_session(request.cookies.get(COOKIE))
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE, path="/")
    return response


def _managing_app_passwords(request: Request, relay: Relay) -> None:
    """App passwords are managed signed in: one of them can't make, see or revoke others (a leaked one can't
    replace itself or shut the user's other scripts out)."""
    if not relay.login_required:
        raise ApiError("Login is disabled (auth is 'none' in settings.json): there's nothing to sign in with")
    if request.state.app_password:
        raise ApiError("An app password can't manage app passwords: sign in to Relay for that", 403)


async def app_passwords(request: Request, relay: Relay):
    _managing_app_passwords(request, relay)
    return relay.accounts.app_passwords(user_id(request))


async def add_app_password(request: Request, relay: Relay):
    """A new app password, named after what will use it: the password is in this answer only."""
    _managing_app_passwords(request, relay)
    name = (await body(request)).get("name")
    if not isinstance(name, str):
        raise ApiError("name: what will use it, e.g. backup script")
    return relay.accounts.create_app_password(user_id(request), name)


async def remove_app_password(request: Request, relay: Relay):
    _managing_app_passwords(request, relay)
    if not relay.accounts.remove_app_password(user_id(request), request.path_params["password_id"]):
        raise ApiError("No such app password", 404)
    return {"ok": True}


async def change_password(request: Request, relay: Relay):
    if not relay.login_required:
        raise ApiError("Login is disabled (auth is 'none' in settings.json)")
    data = await body(request)
    username = request.state.user["username"]
    user = await asyncio.to_thread(relay.accounts.authenticate, username, str(data.get("current", "")))
    if user is None:
        raise ApiError("Current password is wrong", 403)
    relay.accounts.set_password(username, str(data.get("new", "")))
    response = JSONResponse({"ok": True})
    session_cookie(response, relay, relay.accounts.start_session(user["id"], relay.settings.session_days))
    return response
