"""Plugins (relay.plugins): the list and their on/off switch (Settings → Plugins), their routes
(/api/plugins/<name>/…), their static files (/plugins/<name>/static/…) and their pages (/p/<name>/…)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

from relay import config
from relay.plugins import Call, PluginError
from relay.web import COOKIE, ApiError, RelayJSON, app_credentials, body, proxy_sign_in, strict_json

if TYPE_CHECKING:
    from relay.app import Relay


async def list_plugins(request: Request, relay: Relay) -> dict[str, Any]:
    relay.plugins.notice()
    return {"plugins": relay.plugins.public(), "folder": str(relay.plugins.folder),
            "bundled": relay.plugins.bundled()}


async def add_plugin(request: Request, relay: Relay) -> dict[str, Any]:
    """One that comes with Relay: linked, and on from the next start ("restart": Restart Relay)."""
    name = request.path_params["name"]
    try:
        relay.plugins.link(name)
    except PluginError as exc:
        raise ApiError(str(exc), exc.status) from exc
    return {**next(p for p in relay.plugins.public() if p["name"] == name), "restart": True}


async def update_plugin(request: Request, relay: Relay) -> dict[str, Any]:
    """On or off. Off, it stops at once; on again, it's back at once if Relay loaded it at its start, or else from
    the next start ("restart": Restart Relay)."""
    name = request.path_params["name"]
    if name not in relay.plugins.found:
        raise ApiError(f"No plugin {name!r} in {relay.plugins.folder}", 404)
    data = await body(request)
    if not isinstance(data.get("enabled"), bool):
        raise ApiError("enabled is true or false")
    off = [n for n in relay.settings.plugins_off if n != name] + ([] if data["enabled"] else [name])
    relay.settings.plugins_off = off
    config.save_settings(relay.settings)
    found = relay.plugins.found[name]
    found.enabled = data["enabled"]
    restart = found.enabled and found.plugin is None and not found.problem
    return {**next(p for p in relay.plugins.public() if p["name"] == name), "restart": restart}


async def plugin_route(request: Request) -> Response:
    """A call of one of a plugin's routes: its handler runs in one of the plugin's threads; its answer is JSON."""
    relay: Relay = request.app.state.relay
    name, path = request.path_params["name"], request.path_params["path"]
    payload = None
    if request.method in ("POST", "PUT", "PATCH"):
        raw = await request.body()
        if raw.strip():
            try:
                payload = strict_json(raw)
            except (ValueError, UnicodeDecodeError) as exc:
                return JSONResponse({"error": f"Invalid JSON: {exc}"}, status_code=400)
    call = Call(request.method, path, {}, dict(request.query_params), payload, getattr(request.state, "user", None))
    try:
        answer = await relay.plugins.answer(name, call)
    except PluginError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)
    return RelayJSON(answer)


async def plugin_static(request: Request) -> Response:
    """A file of a plugin's static/ folder (its page's script, its style, its images), for a signed-in page (its
    import() and stylesheet send the session, as same-origin requests do): nobody else learns which plugins
    there are or reads their code."""
    relay: Relay = request.app.state.relay
    if relay.login_required:
        given = app_credentials(request)
        signed_in = relay.accounts.user_for_app_password(given[1], given[0]) if given else None
        if given is None or signed_in is None and proxy_sign_in(request):
            signed_in = relay.accounts.user_for_token(request.cookies.get(COOKIE))
        if signed_in is None:
            return Response("Not signed in", status_code=401, media_type="text/plain")
    found = relay.plugins.static(request.path_params["name"], request.path_params["path"])
    if found is None:
        raise HTTPException(404)
    return FileResponse(found)
