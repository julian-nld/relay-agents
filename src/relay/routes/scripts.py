"""The Scripts page's API (relay.scripts): the folder (also as a network drive), its scripts, their runs as they
go, the documentation."""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from relay import webdav
from relay.routes.conversations import open_conversation
from relay.scripts import PRIVATE, ScriptError
from relay.web import COOKIE, ApiError, body, public_share, started_outside, user_id

if TYPE_CHECKING:
    from relay.app import Relay

HEARTBEAT = 15  # seconds a run's output may say nothing before its stream says it's alive


def _name(relay: Relay, request: Request) -> str:
    """The script the address names, its folder put in front when it's left out (relay.scripts.named)."""
    return _safely(relay.scripts.named, request.path_params["name"])


def _safely(call, *args: Any) -> Any:
    try:
        return call(*args)
    except ScriptError as exc:
        raise ApiError(str(exc)) from exc


async def list_scripts(request: Request, relay: Relay):
    folder = str(relay.scripts.folder())
    shared = relay.db.one("SELECT 1 FROM shares WHERE user_id = ? AND directory = ?", (user_id(request), folder))
    return {"folder": folder, "scripts": relay.scripts.listed(), "shared": shared is not None}


async def set_scripts_folder(request: Request, relay: Relay):
    folder = _safely(relay.scripts.set_folder, (await body(request)).get("folder"))
    return {"folder": str(folder), "scripts": relay.scripts.listed()}


async def script_templates(request: Request, relay: Relay):
    """The templates, each with a name free for it in private/ (to start from)."""
    return {"templates": [{**template, "free": _safely(relay.scripts.free, PRIVATE, template["name"])}
                          for template in relay.scripts.templates()]}


async def script_docs(request: Request, relay: Relay):
    return {"markdown": relay.scripts.docs()}


async def share_scripts(request: Request, relay: Relay):
    """The scripts' folder as a network drive (relay.webdav), to edit them on your computer (VS Code, say)."""
    read_only = (await body(request)).get("read_only")
    share = webdav.create(relay.db, user_id(request), _safely(relay.scripts.folder), "Scripts",
                          None if read_only is None else bool(read_only))
    return public_share(share)


async def get_script(request: Request, relay: Relay):
    name = _name(relay, request)
    text = _safely(relay.scripts.read, name)
    tasks = [task["name"] for task in relay.schedules.all(user_id(request)) if task.get("script") == name]
    return {"name": name, "text": text, "runs": relay.scripts.runs(name, user_id(request)), "tasks": tasks}


async def save_script(request: Request, relay: Relay):
    data = await body(request)
    if not isinstance(data.get("text"), str):
        raise ApiError("text: the script")
    return _safely(relay.scripts.write, _name(relay, request), data["text"])


async def create_script(request: Request, relay: Relay):
    """A new script: not over one that's there (PUT saves over it)."""
    data = await body(request)
    if not isinstance(data.get("text"), str):
        raise ApiError("text: the script")
    return _safely(relay.scripts.create, _name(relay, request), data["text"])


async def rename_script(request: Request, relay: Relay):
    name, to = _name(relay, request), str((await body(request)).get("to") or "")
    to = to if "/" in to else f"{name.split('/')[0]}/{to}"  # (a name alone: in its folder)
    _safely(relay.scripts.rename, name, to)
    return {"name": to}


async def copy_script(request: Request, relay: Relay):
    """A copy as the user's own (Copy to my scripts): {to: private or public, name: the one wished for}; the next
    name free when that's taken."""
    data = await body(request)
    made = _safely(relay.scripts.copy, _name(relay, request), str(data.get("to") or PRIVATE),
                   str(data.get("name") or "") or None)
    return {"name": made}


async def move_script(request: Request, relay: Relay):
    """Into private/ or public/ (Make private, Make public): {to}; the next name free when its own is taken."""
    to = str((await body(request)).get("to") or "")
    return {"name": _safely(relay.scripts.move, _name(relay, request), to)}


async def write_script(request: Request, relay: Relay):
    """A script an agent writes (With an agent): a code task in public/, where agents may see, with the skill
    relay-sdk-template-writer and what the script should do. {description, provider_id, model}."""
    data = await body(request)
    description = str(data.get("description") or "").strip()
    if not description:
        raise ApiError("description: what the script should do")
    first = description.splitlines()[0]  # (its title: a whole word last, not "…and prin")
    title = first if len(first) <= 60 else first[:60].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    row = await open_conversation(relay, user_id(request), {
        "provider_id": data.get("provider_id"), "model": data.get("model") or None, "mode": "code",
        "directory": str(_safely(relay.scripts.public_folder)), "title": f"Script: {title}"},
        external=started_outside(relay, request))
    relay.scripts.made(request.cookies.get(COOKIE), row["id"])  # (a script's, from the Scripts page: its)
    run_id = await asyncio.to_thread(relay.runs.start, row, f"/relay-sdk-template-writer {description}")
    return {"conversation": row["id"], "run": run_id}


async def delete_script(request: Request, relay: Relay):
    await asyncio.to_thread(_safely, relay.scripts.delete, _name(relay, request))  # (a run of it is stopped first)
    return {"ok": True}


async def run_script(request: Request, relay: Relay):
    data = await body(request)
    run_id = await asyncio.to_thread(_safely, relay.scripts.start, user_id(request), _name(relay, request),
                                     str(data.get("args") or ""))
    return {"run": run_id}


def _run(relay: Relay, request: Request) -> dict[str, Any]:
    found = relay.scripts.run(request.path_params["run_id"])
    owner = relay.db.one("SELECT user_id FROM script_runs WHERE id = ?", (request.path_params["run_id"],))
    if found is None or owner is None or owner["user_id"] != user_id(request):
        raise ApiError("No such run", 404)
    return {**found, "conversations": _titled(relay, found["conversations"])}


def _titled(relay: Relay, ids: list[str]) -> list[dict[str, Any]]:
    """The conversations a run made that are still there: id and title."""
    rows = relay.db.all(f"SELECT id, title FROM conversations WHERE id IN ({', '.join('?' * len(ids))})",
                        tuple(ids)) if ids else []
    titles = {row["id"]: row["title"] for row in rows}
    return [{"id": made, "title": titles[made]} for made in ids if made in titles]


async def get_script_run(request: Request, relay: Relay):
    return _run(relay, request)


async def stop_script_run(request: Request, relay: Relay):
    _run(relay, request)
    return {"stopped": await asyncio.to_thread(relay.scripts.stop, request.path_params["run_id"])}


async def script_output(request: Request) -> Response:
    """A run's output as it's written (server-sent): {"text", "at"} each time there's more, "conversations" when
    it made one (each's id and title), then "end" with how it ended. `after`: the characters seen already."""
    relay: Relay = request.app.state.relay
    try:
        _run(relay, request)
        after = int(request.query_params.get("after", "0"))
    except ApiError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)
    except ValueError:
        return JSONResponse({"error": "after must be a number"}, status_code=400)
    run_id = request.path_params["run_id"]

    async def stream():
        sent, made, said = after, 0, time.monotonic()
        while True:
            found = relay.scripts.run(run_id)
            if found is None:
                return
            total = found["cut"] + len(found["output"])
            if total > sent:
                yield f"data: {json.dumps({'text': found['output'][max(0, sent - found['cut']):], 'at': total})}\n\n"
                sent, said = total, time.monotonic()
            if len(found["conversations"]) > made:
                made = len(found["conversations"])
                yield f"event: conversations\ndata: {json.dumps(_titled(relay, found['conversations']))}\n\n"
            if found["status"] != "running":
                ended = {k: found[k] for k in ("status", "exit_code", "started", "finished")}
                yield f"event: end\ndata: {json.dumps(ended)}\n\n"
                return
            if await request.is_disconnected():
                return
            if time.monotonic() - said > HEARTBEAT:
                yield ": keep-alive\n\n"  # (a script that says nothing for a while: a reader's time-out, a proxy's)
                said = time.monotonic()
            await asyncio.sleep(0.2)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
