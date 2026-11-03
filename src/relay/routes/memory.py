"""Settings → Memory: Relay's memory (relay.memory), as the user sees and changes it."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from starlette.requests import Request

from relay import memory
from relay.web import ApiError, body, user_id

if TYPE_CHECKING:
    from relay.app import Relay


async def get_memory(request: Request, relay: Relay):
    user = user_id(request)
    return {"enabled": memory.enabled(relay.db, user), "agents_add": memory.agents_add(relay.db, user),
            "most": memory.MOST, "longest": memory.LONGEST,
            "categories": [{"id": kind, "label": name, "about": about} for kind, name, about in memory.CATEGORIES],
            "memories": [_public(m) for m in memory.listed(relay.db, user)],
            # (git looks at each code task's folder: off the event loop)
            "projects": await asyncio.to_thread(memory.projects, relay.db, user)}


async def put_memory(request: Request, relay: Relay):
    """On or off (off: agents get none and can't add; what's kept stays), and whether agents save what they
    learn (agents_add)."""
    data = await body(request)
    changes = {k: data[k] for k in ("enabled", "agents_add") if k in data}
    if not changes or not all(isinstance(v, bool) for v in changes.values()):
        raise ApiError("enabled and agents_add are true or false")
    memory.set_preferences(relay.db, user_id(request), **changes)
    return await get_memory(request, relay)


async def add_memory(request: Request, relay: Relay):
    data = await body(request)
    user = user_id(request)
    # restore: one forgotten a moment ago, put back as it was (Undo): who added it, in which chat, since when
    back = data.get("restore") if isinstance(data.get("restore"), dict) else {}
    try:
        memory.add(relay.db, user, data.get("text"),
                   await asyncio.to_thread(_project, relay, user, data.get("project")),
                   str(back.get("by") or "you"), back.get("conversation_id") or None, data.get("category"),
                   back.get("created") if isinstance(back.get("created"), (int, float)) else None)
    except memory.MemoryError as exc:
        raise ApiError(str(exc)) from exc
    return await get_memory(request, relay)


async def change_memory(request: Request, relay: Relay):
    data = await body(request)
    user = user_id(request)
    try:
        project = await asyncio.to_thread(_project, relay, user, data["project"]) if "project" in data else ...
        memory.change(relay.db, user, request.path_params["memory_id"], data.get("text"), project,
                      data.get("category"))
    except memory.MemoryError as exc:
        raise ApiError(str(exc), 404 if str(exc).startswith("No memory") else 400) from exc
    return await get_memory(request, relay)


async def forget_memory(request: Request, relay: Relay):
    try:
        memory.forget(relay.db, user_id(request), request.path_params["memory_id"])
    except memory.MemoryError as exc:
        raise ApiError(str(exc), 404) from exc
    return await get_memory(request, relay)


async def clear_memory(request: Request, relay: Relay):
    """Delete all memories (Settings → Memory), or one place's (?project=; "general" for the general ones)."""
    user = user_id(request)
    wanted = request.query_params.get("project")
    project = ... if wanted is None else None if wanted == "general" else await asyncio.to_thread(
        _project, relay, user, wanted)
    deleted = memory.clear(relay.db, user, project)
    return {**await get_memory(request, relay), "deleted": deleted}


def _project(relay: Relay, user: int, project: Any) -> str | None:
    """General (None, ""), or one of the user's projects."""
    if project in (None, ""):
        return None
    if not isinstance(project, str) or project not in {p["key"] for p in memory.projects(relay.db, user)}:
        raise ApiError("That isn't one of your projects")
    return project


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {k: row[k] for k in ("id", "short", "project", "label", "text", "category", "by", "conversation_id",
                                "conversation_title", "created", "updated")}
