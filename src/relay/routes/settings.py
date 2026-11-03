"""Settings, limits, workspaces, notifications, usage and restarting."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from starlette.requests import Request

from relay import config, health, limits, notify, tunables, usage
from relay.web import SECRET_MASK, ApiError, body, user_id, within_roots

if TYPE_CHECKING:
    from relay.app import Relay


NOTIFY_FIELDS = ("ntfy_url", "ntfy_token", "webhook_url", "notify_min_seconds", "public_url", "notify_vpn")


async def get_settings(request: Request, relay: Relay):
    s = relay.settings
    return {
        "workspace_roots": s.workspace_roots,
        "chat_directory": str(s.chat_directory()),
        "notifications": {
            "ntfy_url": s.ntfy_url, "ntfy_token": SECRET_MASK if s.ntfy_token else "",
            "webhook_url": s.webhook_url, "notify_min_seconds": s.notify_min_seconds,
            "public_url": s.public_url, "notify_vpn": s.notify_vpn,
            # What's pushed, per category: as chosen, else its default
            "categories": [{**c, "push": relay.notifier.pushes(c["id"]), "priority": relay.notifier.priority(c["id"])}
                           for c in notify.CATEGORIES],
            "priorities": list(notify.PRIORITIES),
        },
    }


def _apply_notifications(settings: config.Settings, data: dict[str, Any]) -> None:
    """Validate everything first; change nothing if any field is invalid."""
    updates = {}
    for name in NOTIFY_FIELDS:
        if name not in data:
            continue
        value = data[name]
        if name == "notify_min_seconds":
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ApiError("notify_min_seconds must be a number of seconds")
        elif name == "notify_vpn":
            if not isinstance(value, bool):
                raise ApiError("notify_vpn must be true or false")
        else:
            if not isinstance(value, str):
                raise ApiError(f"{name} must be text")
            value = value.strip()
            if name == "ntfy_token" and value == SECRET_MASK:
                continue  # unchanged
            if value and name != "ntfy_token" and not value.startswith(("http://", "https://")):
                said = {"ntfy_url": "The ntfy topic's address", "webhook_url": "The webhook's address"}.get(name, name)
                raise ApiError(f"{said} must start with http:// or https:// (nothing was saved)")
        updates[name] = value
    categories = data.get("notify_categories")
    if categories is not None:
        if not isinstance(categories, dict):
            raise ApiError("notify_categories must be an object")
        chosen = dict(settings.notify_categories or {})
        for category, choice in categories.items():
            if category not in notify.CATEGORY or not isinstance(choice, dict):
                raise ApiError(f"There's no notification category {category!r}")
            if not isinstance(choice.get("push"), bool) or choice.get("priority") not in notify.PRIORITIES:
                raise ApiError(f"{category}: push is true or false, priority one of {', '.join(notify.PRIORITIES)}")
            chosen[category] = {"push": choice["push"], "priority": choice["priority"]}
        updates["notify_categories"] = chosen
    for name, value in updates.items():
        setattr(settings, name, value)


async def get_limits(request: Request, relay: Relay):
    """Settings → Limits: the tunables, and the server's own settings."""
    s = relay.settings
    return {
        "tunables": [{**t.public(), "value": tunables.get(t.key)} for t in tunables.TUNABLES],
        "server": {"host": s.host, "port": s.port, "public_url": s.public_url, "session_days": s.session_days,
                   "secure_cookies": s.secure_cookies, "auth": s.auth},
    }


async def put_limits(request: Request, relay: Relay):
    data = await body(request)
    if not {"values", "server"} & set(data):
        raise ApiError("Nothing to change: send values (and/or server)")
    values = data.get("values") or {}
    if not isinstance(values, dict):
        raise ApiError("values must be an object")
    try:
        merged = {**tunables.current(), **values}
        stored = tunables.changed(merged)
    except tunables.TunableError as exc:
        raise ApiError(str(exc)) from exc
    server = data.get("server") or {}
    if not isinstance(server, dict):
        raise ApiError("server must be an object")
    if "public_url" in server:
        url = str(server["public_url"] or "").strip()
        if url and not url.startswith(("http://", "https://")):
            raise ApiError("Relay's address must start with http:// or https://")
        relay.settings.public_url = url.rstrip("/")
    if "session_days" in server:
        try:
            days = int(server["session_days"])
        except (TypeError, ValueError) as exc:
            raise ApiError("Sign-ins last a whole number of days") from exc
        if not 1 <= days <= 365:
            raise ApiError("Sign-ins last between 1 and 365 days")
        relay.settings.session_days = days
    relay.settings.limits = stored
    tunables.use(stored)
    config.save_settings(relay.settings)
    return await get_limits(request, relay)


async def test_notifications(request: Request, relay: Relay):
    if not relay.notifier.configured():
        raise ApiError("Set an ntfy topic URL or a webhook URL first")
    errors = await asyncio.to_thread(relay.notifier.test)
    return {"ok": not errors, "errors": errors}


async def send_notification(request: Request, relay: Relay):
    """A notification from a script (the SDK's notify()): {text, title, priority, url}. Not sent (not set up,
    turned off) isn't an error: {sent: false, why}, so the script goes on."""
    data = await body(request)
    text = str(data.get("text") or "").strip()
    if not text:
        raise ApiError("text: what the notification says")
    # (as the page names them too: "normal" is ntfy's default; any case)
    priority = {"normal": "default"}.get(str(data.get("priority") or "").lower(),
                                         str(data.get("priority") or "").lower())
    priority = priority or None
    if priority not in (None, *notify.PRIORITIES):
        raise ApiError(f"priority: one of {', '.join(notify.PRIORITIES)} (normal is default)")
    url = str(data.get("url") or "") or None
    if url and url.startswith("/") and not url.startswith("//"):
        # One of Relay's own pages (/c/<id>, a plugin's /p/<name>): at Relay's address, when it's known (Settings →
        # General), else the notification goes without a link
        url = relay.settings.public_url.rstrip("/") + url if relay.settings.public_url else None
    if url and not url.startswith(("http://", "https://")):
        raise ApiError("url: an http:// or https:// address, or a page of Relay's (/c/…), opened when it's tapped")
    why = await asyncio.to_thread(relay.notifier.script, text, str(data.get("title") or "") or None, priority, url)
    return {"sent": why is None, **({"why": why} if why else {})}


async def put_settings(request: Request, relay: Relay):
    data = await body(request)
    if isinstance(data.get("notifications"), dict):
        _apply_notifications(relay.settings, data["notifications"])
        config.save_settings(relay.settings)
    if "workspace_roots" not in data:
        return await get_settings(request, relay)
    roots = data.get("workspace_roots")
    if not isinstance(roots, list) or not all(isinstance(r, str) and r.strip() for r in roots):
        raise ApiError("workspace_roots must be a list of directories")
    resolved = [str(Path(r.strip()).expanduser()) for r in roots]
    for root in resolved:
        if not Path(root).is_absolute():
            raise ApiError(f"{root} is not an absolute path")
        if Path(root).resolve() == Path("/"):
            raise ApiError("The filesystem root can't be a workspace root")
    relay.settings.workspace_roots = resolved
    config.save_settings(relay.settings)
    return await get_settings(request, relay)


async def browse_workspaces(request: Request, relay: Relay):
    roots = relay.settings.roots()
    raw = request.query_params.get("path")
    if not raw:
        return {"path": None, "parent": None, "roots": [str(r) for r in roots],
                "entries": [{"name": str(r), "path": str(r), "exists": r.is_dir()} for r in roots]}
    if "\x00" in raw:
        raise ApiError("Not a directory inside a workspace root", 403)
    path = Path(raw).expanduser().resolve()
    if not within_roots(relay, path) or not path.is_dir():
        raise ApiError("Not a directory inside a workspace root", 403)
    entries = sorted(
        ({"name": p.name, "path": str(p), "exists": True} for p in path.iterdir()
         if p.is_dir() and not p.name.startswith(".") and p.name != "__pycache__"),  # (Python's, no project)
        key=lambda e: e["name"].lower(),
    )
    parent = path.parent if path not in roots and within_roots(relay, path.parent) else None
    return {"path": str(path), "parent": str(parent) if parent else None,
            "roots": [str(r) for r in roots], "entries": entries[:500]}


async def create_workspace(request: Request, relay: Relay):
    data = await body(request)
    parent = Path(str(data.get("parent", ""))).expanduser().resolve()
    name = str(data.get("name", "")).strip()
    if not name or "/" in name or name in (".", ".."):
        raise ApiError("Invalid folder name")
    if parent not in relay.settings.roots() and not within_roots(relay, parent):
        raise ApiError("Folders can only be created inside a workspace root", 403)
    target = parent / name
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # (a name too long for a file, say: no error 500)
        raise ApiError(f"Couldn't make that folder: {exc.strerror or exc}") from exc
    return {"path": str(target)}


async def restart(request: Request, relay: Relay):
    """Restart Relay, e.g. to use a repaired provider; not while agents are working, except AgentOS's, which go
    on by themselves after it (AgentOS.goes_on), nor while a script runs (the Scripts page: it would end)."""
    with relay.runs.lock:
        going = [run.conversation_id for run in relay.runs.active.values()]
    busy = sum(1 for conversation_id in going if not relay.agentos.goes_on(conversation_id))
    if busy:
        raise ApiError(f"{busy} agent{'s are' if busy > 1 else ' is'} still working; restart when they're done", 409)
    with relay.scripts.lock:
        scripts = len(relay.scripts.active)
    if scripts:
        raise ApiError(f"{scripts} script{'s are' if scripts > 1 else ' is'} still running (Scripts); restart when "
                       f"{'they are' if scripts > 1 else 'it is'} done", 409)
    asyncio.get_running_loop().call_later(0.5, restart_process)
    return {"ok": True}


def restart_process() -> None:
    """Start Relay anew in this process, as it was started (sys.orig_argv)."""
    os.execv(sys.executable, getattr(sys, "orig_argv", [sys.executable, *sys.argv]))


LIMITS_KEPT = 120  # seconds a provider's answer about its limits is shown again before it's asked anew
LIMITS_WAIT = 15  # seconds one provider may take to answer


async def usage_limits(request: Request, relay: Relay):
    """What's left of each provider's usage limits (Provider.limits), one row per login; when a
    provider can't be asked now, what its last run told (model_status.usage), with how old."""
    fresh = request.query_params.get("fresh") == "1"
    now = time.time()

    async def ask(template) -> dict | None:
        kept = relay.limits_seen.get(template.id)
        if kept and not fresh and now - kept[0] < LIMITS_KEPT:
            return kept[1]
        try:
            found = await asyncio.wait_for(asyncio.to_thread(relay.registry.provider(template.id).limits), LIMITS_WAIT)
        except Exception as exc:  # noqa: BLE001 - one provider's trouble isn't the page's
            found = {"account": f"provider:{template.id}", "error": f"couldn't ask it ({type(exc).__name__})"}
        relay.limits_seen[template.id] = (now, found)
        return found

    templates = [t for t in relay.registry.templates() if t.enabled]
    answers = await asyncio.gather(*(ask(t) for t in templates))
    rows, by_account = [], {}
    for template, found in zip(templates, answers, strict=True):
        if found is None or found.get("error"):  # what its runs said last, if anything
            last = max((s for s in health.statuses(relay.db, template.id).values()
                        if limits.run_windows(s.get("usage"))),
                       key=lambda s: s["checked"] or 0, default=None)
            if last is not None:
                found = {"account": (found or {}).get("account") or f"provider:{template.id}", "plan": None,
                         "windows": limits.run_windows(last["usage"]), "source": "last run", "checked": last["checked"],
                         "note": (found or {}).get("error")}
        if found is None:
            continue
        account = found.get("account") or f"provider:{template.id}"
        if account in by_account:
            by_account[account]["also"].append(template.name)
            continue
        row = {"provider_id": template.id, "name": template.name, "also": [], "service": found.get("service"),
               "plan": found.get("plan"),
               "windows": found.get("windows") or [], "source": found.get("source"), "checked": found.get("checked"),
               "error": found.get("error"), "note": found.get("note")}
        by_account[account] = row
        rows.append(row)
    return rows


async def usage_overview(request: Request, relay: Relay):
    try:
        days = max(1, min(366, int(request.query_params.get("days", "30"))))
    except ValueError as exc:
        raise ApiError("days must be a number") from exc
    return await asyncio.to_thread(usage.overview, relay.db, user_id(request), days)
