"""Skills, modes, scheduled tasks, the subagent policy and the user's computers."""

from __future__ import annotations

import asyncio
import shlex
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from relay import fallback, policy, skills
from relay.agentos import IRREVERSIBLE, AgentOSError
from relay.computers import ComputerError
from relay.modes import Modes
from relay.production import ProductionError
from relay.schedules import ScheduleError
from relay.scripts import ScriptError
from relay.web import ApiError, body, has_tools, site_url, strict_json, summary, user_id, within_roots

if TYPE_CHECKING:
    from relay.app import Relay


async def computer_poll(request: Request) -> Response:
    """A computer's connector asks for work (relay.computers); its token is its key."""
    relay: Relay = request.app.state.relay
    try:
        data = strict_json(await request.body())
    except (ValueError, UnicodeDecodeError):
        data = {}
    jobs = await relay.computers.poll(request.path_params["token"], (data or {}).get("info"))
    if jobs is None:
        return JSONResponse({"error": "Unknown computer"}, status_code=404)
    return JSONResponse({"jobs": jobs})


async def computer_result(request: Request) -> Response:
    relay: Relay = request.app.state.relay
    try:
        data = strict_json(await request.body())
    except (ValueError, UnicodeDecodeError):
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    if not isinstance(data, dict) or not relay.computers.deliver(request.path_params["token"], str(data.get("id")),
                                                                 data):
        return JSONResponse({"error": "Unknown computer or job"}, status_code=404)
    return JSONResponse({"ok": True})


async def list_skills(request: Request, relay: Relay):
    return {"folder": str(skills.user_dir()), "skills": [s.public() for s in skills.all_skills()]}


async def add_skill(request: Request, relay: Relay):
    data = await body(request)
    skill = skills.create(str(data.get("name", "")), str(data.get("description", "")),
                          str(data.get("instructions", "")), str(data.get("access", "edit")))
    return skill.public()


async def get_skill(request: Request, relay: Relay):
    name = request.path_params["name"]
    return {**skills.get(name).public(), "files": skills.files(name)}


async def skill_file(request: Request, relay: Relay):
    name, path = request.path_params["name"], request.query_params.get("path", "SKILL.md")
    if request.method == "PUT":
        data = await body(request)
        return skills.write_file(name, path, str(data.get("content", ""))).public()
    return {"path": path, "content": skills.read_file(name, path)}


async def delete_skill(request: Request, relay: Relay):
    skills.delete(request.path_params["name"])
    return {"ok": True}


async def list_modes(request: Request, relay: Relay):
    return [m.public() for m in Modes(relay.db).all(user_id(request))]


async def add_mode(request: Request, relay: Relay):
    return Modes(relay.db).save(user_id(request), await body(request)).public()


async def update_mode(request: Request, relay: Relay):
    mode_id = request.path_params["mode_id"]
    return Modes(relay.db).save(user_id(request), await body(request), mode_id).public()


async def set_mode_icon(request: Request, relay: Relay):
    data = await body(request)
    return Modes(relay.db).set_icon(user_id(request), request.path_params["mode_id"], data.get("icon")).public()


async def delete_mode(request: Request, relay: Relay):
    Modes(relay.db).delete(user_id(request), request.path_params["mode_id"])
    return {"ok": True}


async def list_schedules(request: Request, relay: Relay):
    return [_running(relay, task) for task in relay.schedules.all(user_id(request))]


async def get_schedule(request: Request, relay: Relay):
    try:
        return _running(relay, relay.schedules.get(user_id(request), request.path_params["schedule_id"]))
    except ScheduleError as exc:
        raise ApiError(str(exc), 404) from exc


def _running(relay: Relay, task: dict[str, Any]) -> dict[str, Any]:
    """Whether its last turn goes on now: its script runs, or its chat's agent works (a simulated user saw "ran"
    while it did). The Governor's tasks say so themselves (dispatch_state)."""
    if task.get("script"):
        found = task.get("last_script_run") and relay.db.one("SELECT status FROM script_runs WHERE id = ?",
                                                             (task["last_script_run"],))
        running = bool(found) and found["status"] == "running"
    else:
        running = bool(task.get("last_conversation")) and bool(relay.runs.active_run_for(task["last_conversation"]))
    return {**task, "running": running}


def check_schedule(relay: Relay, user: int, data: dict[str, Any]) -> None:
    """What only the app can check: the provider, the project folder, the chat; and a script's full name (one given
    without its folder: relay.scripts.named)."""
    if data.get("script"):  # (there, and its arguments as they're run: not found out at its time)
        data["script"] = relay.scripts.named(str(data["script"]))
        try:
            relay.scripts.read(data["script"])
        except ScriptError as exc:
            raise ApiError(str(exc)) from exc
    if data.get("script_args"):
        try:
            shlex.split(str(data["script_args"]))
        except ValueError as exc:
            raise ApiError(f"Its arguments don't parse: {exc}") from exc
    if "provider_id" in data:
        relay.registry.get(str(data.get("provider_id") or ""))
    if data.get("mode") == "code" and data.get("target", "new") == "new" and data.get("directory"):
        directory = Path(str(data["directory"])).expanduser()
        if not directory.is_absolute() or not directory.is_dir() or not within_roots(relay, directory):
            raise ApiError("Choose a project folder inside a workspace root")
    if data.get("target") == "same" and data.get("conversation_id") and not relay.db.one(
            "SELECT 1 FROM conversations WHERE id = ? AND user_id = ?", (str(data["conversation_id"]), user)):
        raise ApiError("That chat doesn't exist")


async def add_schedule(request: Request, relay: Relay):
    data = await body(request)
    if not data.get("governed") and not data.get("script"):  # (the Governor chooses who; a script runs itself)
        relay.registry.get(str(data.get("provider_id") or ""))
    check_schedule(relay, user_id(request), data)
    return relay.schedules.save(user_id(request), data)


async def update_schedule(request: Request, relay: Relay):
    data = await body(request)
    check_schedule(relay, user_id(request), data)
    return relay.schedules.save(user_id(request), data, request.path_params["schedule_id"])


async def delete_schedule(request: Request, relay: Relay):
    relay.schedules.delete(user_id(request), request.path_params["schedule_id"])
    return {"ok": True}


async def reset_schedule(request: Request, relay: Relay):
    """One of Relay's own tasks as it ships (its name, prompt and schedule, on)."""
    return relay.schedules.reset(user_id(request), request.path_params["schedule_id"])


async def run_schedule(request: Request, relay: Relay):
    """Run a scheduled task now (its schedule stays as it was); an AgentOS task wakes the Governor."""
    schedule = relay.schedules.get(user_id(request), request.path_params["schedule_id"])
    if schedule.get("governed"):
        try:
            run_id = await relay.agentos.wake(user_id(request), [schedule], time.time(), "the user ran it now",
                                              move_on=False)
        except AgentOSError as exc:
            raise ApiError(str(exc), 409) from exc
        if run_id is None:
            raise ApiError("The Governor is still working, or has this task already; try again when it's done", 409)
        return {"conversation_id": relay.agentos.governor(user_id(request))["id"]}
    if schedule.get("script"):  # (Relay runs it, with no agent: a script's run, on the Scripts page)
        run_id = await relay.scheduler.run_schedule(schedule, move_on=False)
        if run_id is None:
            why = relay.schedules.get(user_id(request), schedule["id"]).get("last_error")
            raise ApiError(why or "The script didn't start", 409)
        return {"script_run": run_id}
    conversation_id = await relay.scheduler.run_schedule(schedule, move_on=False)
    if conversation_id is None:  # (its provider removed, its chat deleted: why, not nothing at all)
        why = relay.schedules.get(user_id(request), schedule["id"]).get("last_error")
        raise ApiError(why or "The task didn't start", 409)
    return {"conversation_id": conversation_id}


# AgentOS (relay.agentos)

async def get_agentos(request: Request, relay: Relay):
    user = user_id(request)
    governor = relay.agentos.governor(user)
    return {"settings": relay.agentos.settings(user),
            "loops": [{"id": loop["id"], "name": loop["name"],
                       "entries": [f"{e['provider']}{' · ' + e['model'] if e.get('model') else ''}"
                                   for e in loop["entries"]]}
                      for loop in fallback.load(relay.db, user)],
            "governor": {"id": governor["id"], "running": bool(relay.runs.active_run_for(governor["id"])),
                         "provider_id": governor["provider_id"], "model": governor["model"]} if governor else None,
            "interventions": relay.agentos.interventions(user),
            # (its own memory, relay.memory: in Settings → Memory, the Governor's place)
            "memories": relay.db.one("SELECT count(*) AS n FROM memories WHERE user_id = ? AND project = 'governor'",
                                     (user,))["n"],
            "irreversible": list(IRREVERSIBLE),
            "spent_24h": relay.agentos.spent(user, time.time() - 86400),  # the Governor's and managed chats' runs
            "events": relay.agentos.events(user, 150)}


async def put_agentos(request: Request, relay: Relay):
    try:
        return relay.agentos.save_settings(user_id(request), await body(request))
    except AgentOSError as exc:
        raise ApiError(str(exc)) from exc


async def wake_agentos(request: Request, relay: Relay):
    """Wake the Governor now, for a round (its tasks wake it when they're due)."""
    user = user_id(request)
    try:
        run_id = await relay.agentos.wake(user, [], time.time(), "the user woke you for a round")
    except AgentOSError as exc:
        raise ApiError(str(exc), 409) from exc
    if run_id is None:
        raise ApiError("The Governor is still working", 409)
    return {"conversation_id": relay.agentos.governor(user)["id"], "run_id": run_id}


async def list_services(request: Request, relay: Relay):
    return relay.production.services(user_id(request))


async def add_service(request: Request, relay: Relay):
    """A live service: its first version from a project folder, started behind Relay's proxy. With
    `conversation_id` (/promote in a code task), that task in the same folder goes on as its WorkBox."""
    data = await body(request)
    user = user_id(request)
    folder = Path(str(data.get("folder") or "")).expanduser()
    if not folder.is_absolute() or not folder.is_dir() or not within_roots(relay, folder):
        raise ApiError("Choose a project folder in one of the workspace roots")
    task = None
    if data.get("conversation_id"):
        task = relay.db.one("SELECT * FROM conversations WHERE id = ? AND user_id = ? AND mode = 'code'",
                            (str(data["conversation_id"]), user))
        if task is None or Path(task["directory"]).resolve() != folder.resolve():
            raise ApiError("That isn't a code task in this folder")
        if relay.db.one("SELECT 1 FROM service_clones WHERE conversation_id = ?", (task["id"],)):
            raise ApiError("This is a service's WorkBox already: /promote makes it its live version")
    fields = (str(data.get("name") or ""), str(data.get("command") or ""), data.get("port"), data.get("health"))
    try:
        if task is not None:  # (/promote: the task goes on as its WorkBox)
            return await relay.production.create_from(user, task, *fields, managed=bool(data.get("managed")))
        return await relay.production.create(user, fields[0], folder, *fields[1:], managed=bool(data.get("managed")))
    except ProductionError as exc:
        raise ApiError(str(exc)) from exc


async def guess_service(request: Request, relay: Relay):
    """What a new service from this folder is likely called and started with, and a free port."""
    folder = Path(request.query_params.get("folder") or "").expanduser()
    if not folder.is_absolute() or not folder.is_dir() or not within_roots(relay, folder):
        raise ApiError("Choose a project folder in one of the workspace roots")
    from relay.production import guess
    return await asyncio.to_thread(guess, relay.db, folder, relay.settings.port)  # (it reads the code)


async def update_workbox(request: Request, relay: Relay):
    """A WorkBox the live version moved on from, brought up to date with it (a three-way merge)."""
    service = _service(relay, request)
    row = relay.db.one("SELECT * FROM conversations WHERE id = ? AND user_id = ?",
                       (str((await body(request)).get("conversation_id") or ""), user_id(request)))
    if row is None:
        raise ApiError("No such WorkBox", 404)
    try:
        return await relay.production.update_clone(service, row)
    except ProductionError as exc:
        raise ApiError(str(exc), 409) from exc


def _service(relay: Relay, request: Request) -> dict[str, Any]:
    try:
        return relay.production.get(user_id(request), request.path_params["service_id"])
    except ProductionError as exc:
        raise ApiError(str(exc), 404) from exc


async def get_service(request: Request, relay: Relay):
    try:
        return relay.production.one(user_id(request), request.path_params["service_id"])
    except ProductionError as exc:
        raise ApiError(str(exc), 404) from exc


async def update_service(request: Request, relay: Relay):
    """Whether AgentOS looks after it (the Governor sees it and may change it), or it's the user's own."""
    service = _service(relay, request)
    data = await body(request)
    if "managed" in data:
        relay.db.execute("UPDATE services SET managed = ? WHERE id = ?", (int(bool(data["managed"])), service["id"]))
        relay.agentos.event(service["user_id"], "service.managed" if data["managed"] else "service.private",
                            actor="user", service=service["id"])
    return relay.production.one(user_id(request), service["id"])


async def delete_service(request: Request, relay: Relay):
    try:
        await relay.production.delete(_service(relay, request))
    except ProductionError as exc:
        raise ApiError(str(exc), 409) from exc
    return {"ok": True}


async def restore_service(request: Request, relay: Relay):
    """Its data put back as one of its snapshots has it (the data as it is now kept first)."""
    data = await body(request)
    try:
        return {"message": await relay.production.restore(_service(relay, request), str(data.get("snapshot") or ""))}
    except ProductionError as exc:
        raise ApiError(str(exc), 409) from exc


async def rollback_service(request: Request, relay: Relay):
    data = await body(request)
    try:
        return {"message": await relay.production.rollback(_service(relay, request), data.get("version"))}
    except ProductionError as exc:
        raise ApiError(str(exc), 409) from exc


async def restart_service(request: Request, relay: Relay):
    try:
        return {"message": await relay.production.restart(_service(relay, request))}
    except ProductionError as exc:
        raise ApiError(str(exc), 409) from exc


async def stop_service(request: Request, relay: Relay):
    """Not served or checked any more; its versions and data stay (start_service)."""
    try:
        return {"message": await relay.production.stop_service(_service(relay, request))}
    except ProductionError as exc:
        raise ApiError(str(exc), 409) from exc


async def start_service(request: Request, relay: Relay):
    try:
        return {"message": await relay.production.start_service(_service(relay, request))}
    except ProductionError as exc:
        raise ApiError(str(exc), 409) from exc


async def demote_service(request: Request, relay: Relay):
    """An agent to work on the service: its live version cloned into a WorkBox of the user's own
    (it stays up), with `provider_id` (`model`) as its agent."""
    service = _service(relay, request)
    data = await body(request)
    provider_id = str(data.get("provider_id") or "")
    if not has_tools(relay.registry.get(provider_id)):
        raise ApiError("Choose a provider with tools to work in the WorkBox")
    try:
        row = await relay.production.clone(user_id(request), service, provider_id, data.get("model") or None)
    except ProductionError as exc:
        raise ApiError(str(exc), 409) from exc
    return summary(relay, row)


async def promote_service(request: Request, relay: Relay):
    """One of the service's WorkBoxes as its new live version (relay.production: promote_clone)."""
    service = _service(relay, request)
    row = relay.db.one("SELECT * FROM conversations WHERE id = ? AND user_id = ?",
                       (str((await body(request)).get("conversation_id") or ""), user_id(request)))
    if row is None:
        raise ApiError("No such WorkBox", 404)
    try:
        return {"message": await relay.production.promote_clone(service, row, by="user")}
    except ProductionError as exc:
        raise ApiError(str(exc), 409) from exc


async def service_status(request: Request, relay: Relay):
    """Its box, processes, health checks and versions in detail (as the Governor's service.status)."""
    from relay.production import describe_status
    found = await asyncio.to_thread(relay.production.status, _service(relay, request))
    return {**found, "text": describe_status(found)}


async def service_logs(request: Request, relay: Relay):
    return {"logs": await asyncio.to_thread(relay.production.logs, _service(relay, request), None, 200)}


async def resolve_intervention(request: Request, relay: Relay):
    relay.agentos.resolve(user_id(request), request.path_params["intervention_id"])
    return {"ok": True}


async def get_fallback_loops(request: Request, relay: Relay):
    return fallback.load(relay.db, user_id(request))


async def put_fallback_loops(request: Request, relay: Relay):
    data = await body(request)
    return fallback.save(relay.db, relay.registry, user_id(request), data.get("loops", []))


async def get_subagent_policy(request: Request, relay: Relay):
    return policy.load(relay.db, user_id(request))


async def put_subagent_policy(request: Request, relay: Relay):
    return policy.save(relay.db, user_id(request), await body(request))


async def list_computers(request: Request, relay: Relay):
    return relay.computers.for_user(user_id(request))


async def add_computer(request: Request, relay: Relay):
    return relay.computers.create(user_id(request), str((await body(request)).get("name", "")))


async def update_computer(request: Request, relay: Relay):
    return relay.computers.update(user_id(request), request.path_params["computer_id"], await body(request))


async def delete_computer(request: Request, relay: Relay):
    relay.computers.remove(user_id(request), request.path_params["computer_id"])
    return {"ok": True}


async def computer_script(request: Request) -> Response:
    """The connector for a computer, set to reach Relay the way this browser does: its page says (`url`) where it has
    Relay, which behind a proxy isn't what Relay sees (https there, http here)."""
    relay: Relay = request.app.state.relay
    given = urlsplit(request.query_params.get("url", ""))
    browsers = given.geturl() if given.scheme in ("http", "https") and given.hostname else ""
    try:
        url = relay.settings.public_url or browsers or site_url(request, relay.settings)
        text = relay.computers.script(user_id(request), request.path_params["computer_id"], url)
    except ComputerError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return Response(text, media_type="text/x-python",
                    headers={"Content-Disposition": 'attachment; filename="relay-computer.py"'})


async def sdk_page(request: Request) -> Response:
    """/sdk/: how to install Relay's Python SDK from this server (relay.sdk)."""
    from relay import sdk
    return Response(sdk.page(site_url(request, request.app.state.relay.settings)),
                    media_type="text/plain; charset=utf-8")


async def sdk_wheel(request: Request) -> Response:
    """The SDK's wheel, for pip; under its own name only (pip reads the version from it)."""
    from relay import sdk
    if request.path_params["name"] != sdk.wheel_name():
        return JSONResponse({"error": f"Not here: the SDK is /sdk/{sdk.wheel_name()}"}, status_code=404)
    return Response(sdk.wheel(), media_type="application/zip")
