"""Runs as they go: their events, stopping them, permission answers, MCP, and attachments."""

from __future__ import annotations

import asyncio
import hmac
import json
import time
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse

from relay.runs import RunError
from relay.uploads import UploadError, max_bytes
from relay.web import ApiError, body, owned_run, strict_json, user_id

if TYPE_CHECKING:
    from relay.app import Relay

SLOW = 5  # seconds: a call that took longer may have been given up by its caller (a time-out of its command)


async def mcp_endpoint(request: Request) -> Response:
    """Relay's MCP server for a running agent (relay.delegation); the token is the credential."""
    relay: Relay = request.app.state.relay
    token = request.path_params["token"]
    if relay.delegation.run_for(token) is None and token not in relay.delegation.checks:  # (a run's, or a check's)
        return JSONResponse({"error": "Unknown or expired token"}, status_code=404)
    if request.method != "POST":
        return Response(status_code=405, headers={"Allow": "POST"})
    try:
        message = strict_json(await request.body())
    except (ValueError, UnicodeDecodeError):
        return JSONResponse({"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32700, "message": "Parse error"}}, status_code=400)
    messages = message if isinstance(message, list) else [message]
    started = time.monotonic()
    answers = [a for a in [await relay.delegation.handle(token, m) for m in messages] if a]
    if answers and time.monotonic() - started > SLOW and await _gone(request):
        relay.delegation.left(token, messages, answers)  # (its next call has them)
    if not answers:
        return Response(status_code=202)
    return JSONResponse(answers if isinstance(message, list) else answers[0])


async def _gone(request: Request) -> bool:
    """Whether the caller left while Relay worked (its command was stopped). request.is_disconnected
    doesn't see it through BaseHTTPMiddleware; a short receive does: at once when it's gone, after a
    tenth of a second when it waits."""
    with anyio.move_on_after(0.1):
        return (await request.receive())["type"] == "http.disconnect"
    return False


async def tools_for(request: Request) -> Response:
    """For `relay` in a WorkBox (container/bin/relay): the MCP address of the agent working in a
    folder. Only for this machine (the containers share its network), and only for a box's own
    folder: the command's environment has it signed by Relay (Containers.agent_env), so one box
    can't ask for another's agents (the Governor's, say)."""
    relay: Relay = request.app.state.relay
    if not request.client or request.client.host not in ("127.0.0.1", "::1"):
        return JSONResponse({"error": "Only for Relay's own containers"}, status_code=403)
    folder, box = request.query_params.get("folder") or "", request.query_params.get("box") or ""
    key = request.query_params.get("key") or ""
    signer = getattr(relay.containers, "box_key", None)
    if not box or signer is None or not hmac.compare_digest(signer(box), key):
        return JSONResponse({"error": "Relay's tools work in a WorkBox, while one of Relay's agents works there"},
                            status_code=403)
    try:
        inside = Path(folder).resolve()
        box_folder = Path(box).resolve()
    except (OSError, ValueError):
        return JSONResponse({"error": f"Not a folder: {folder}"}, status_code=404)
    if inside != box_folder and box_folder not in inside.parents:
        inside = box_folder  # (a command outside its box's folder runs in it)
    try:
        script = relay.runs.tools_script_for(str(inside), within=box_folder)
    except RunError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409 if "More than one" in str(exc) else 404)
    return JSONResponse({"url": f"{relay.runs.local_url}/mcp/{script.parent.name}"})


async def answer_permission(request: Request, relay: Relay):
    """Allow or refuse what an agent asked permission for."""
    row = relay.db.one("SELECT c.user_id FROM runs r JOIN conversations c ON c.id = r.conversation_id "
                       "WHERE r.id = ?", (request.path_params["run_id"],))
    if row is None or row["user_id"] != user_id(request):
        raise ApiError("Run not found", 404)
    data = await body(request)
    await asyncio.to_thread(relay.runs.answer, request.path_params["run_id"], str(data.get("id")),
                            str(data.get("decision") or ""))
    return {"ok": True}


async def answer_form(request: Request, relay: Relay):
    owned_run(relay, request)
    data = await body(request)
    if "answer" not in data or not (data["answer"] is None or isinstance(data["answer"], dict)):
        raise ApiError("answer must contain keyed field values, or null to skip the question")
    await asyncio.to_thread(relay.runs.answer_form, request.path_params["run_id"], str(data.get("id")),
                            data["answer"])
    return {"ok": True}


async def run_events(request: Request) -> Response:
    relay: Relay = request.app.state.relay
    try:
        run = owned_run(relay, request)
        after = int(request.query_params.get("after", "0"))
        # A reconnecting EventSource says which event it saw last.
        last_seen = request.headers.get("last-event-id")
        if last_seen:
            after = max(after, int(last_seen) + 1)
    except ApiError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)
    except ValueError:
        return JSONResponse({"error": "after must be a number"}, status_code=400)
    # What happened before the browser asked comes first, then a "caught-up": the browser shows that part at
    # once (as a finished turn), not as a burst of live changes, each animated; from there on it's live.
    backlog = relay.runs.event_count(run["id"])
    caught_up = "event: caught-up\ndata: {}\n\n"

    async def stream():
        behind = after < backlog
        if not behind:
            yield caught_up
        async for item in relay.runs.follow(run["id"], after, heartbeat=15):
            if item is None:
                yield ": keep-alive\n\n"  # keeps proxies from closing an idle stream
            else:
                index, event = item
                yield f"id: {index}\ndata: {json.dumps(event)}\n\n"
                if behind and index >= backlog - 1:
                    behind = False
                    yield caught_up
            if await request.is_disconnected():
                return
        yield "event: end\ndata: {}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
    })


async def upload(request: Request, relay: Relay):
    """Store a file sent as the raw request body (name in X-Filename)."""
    name = urllib.parse.unquote(request.headers.get("x-filename", "")) or "file"
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError as exc:
        raise UploadError("Invalid Content-Length") from exc
    if declared > max_bytes():
        raise UploadError(f"Files can be at most {max_bytes() // (1024 * 1024)} MB")
    writer = relay.uploads.writer(name)
    try:
        async for chunk in request.stream():
            writer.write(chunk)  # streamed to disk: nothing large is held in memory
    except BaseException:
        writer.discard()
        raise
    attachment = relay.uploads.register(user_id(request), writer, request.headers.get("content-type"))
    return attachment.public()


async def get_upload(request: Request) -> Response:
    relay: Relay = request.app.state.relay
    attachment = relay.uploads.get(request.path_params["upload_id"], user_id(request))
    if attachment is None or not attachment.path.is_file():
        return JSONResponse({"error": "Not found"}, status_code=404)
    # Only plain images are shown inline; anything else (including SVG, which
    # can carry scripts) is downloaded.
    inline = attachment.is_image and attachment.mime != "image/svg+xml"
    return FileResponse(attachment.path, media_type=attachment.mime, filename=attachment.name,
                        content_disposition_type="inline" if inline else "attachment",
                        headers={"Cache-Control": "private, max-age=86400"})


async def stop_run(request: Request, relay: Relay):
    run = owned_run(relay, request)
    return {"stopped": relay.runs.stop(run["id"], "the user stopped it")}
