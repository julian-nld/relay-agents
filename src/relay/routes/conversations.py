"""Conversations and code tasks: messages, queue, retry, fork, rewind, moving, sharing."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import shutil
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from relay import checkpoints, export, fallback, forks, git, handover, relay_tools, tasks, timed, tunables, webdav
from relay.modes import Modes
from relay.providers import TemplateError, safely
from relay.runs import ProjectBusy
from relay.vpn import VpnError
from relay.web import (
    COOKIE,
    EFFORTS,
    ApiError,
    body,
    clones_of,
    conversation_row,
    has_tools,
    idle_task,
    last_runs,
    make_title,
    public_share,
    queued_messages,
    started_outside,
    summary,
    user_id,
    within_roots,
)

if TYPE_CHECKING:
    from relay.app import Relay


async def list_conversations(request: Request, relay: Relay):
    query = request.query_params.get("q", "").strip()
    sql = ("SELECT * FROM conversations WHERE user_id = ? AND parent_conversation IS NULL AND agentos_role IS NULL "
           "AND temporary = 0")
    params: list[Any] = [user_id(request)]
    if query:
        # Escape LIKE wildcards so "snake_case" or "100%" match literally.
        pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        sql += (" AND (title LIKE ? ESCAPE '\\' OR id IN (SELECT conversation_id FROM messages "
                "WHERE content LIKE ? ESCAPE '\\'))")
        params += [pattern, pattern]
    sql += " ORDER BY pinned DESC, updated DESC LIMIT 500"
    runs = last_runs(relay, user_id(request))
    rows = relay.db.all(sql, tuple(params))
    queued = queued_messages(relay, [r["id"] for r in rows])
    clones = clones_of(relay, rows)
    return [summary(relay, row, runs.get(row["id"]), queued[row["id"]], clones) for row in rows]


async def list_tasks(request: Request, relay: Relay):
    """Everything agents are doing or did: running chats and all code tasks,
    grouped for the tasks overview."""
    user = user_id(request)
    runs = last_runs(relay, user)
    rows = relay.db.all(
        "SELECT * FROM conversations WHERE user_id = ? AND parent_conversation IS NULL AND agentos_role IS NULL "
        "AND temporary = 0 ORDER BY updated DESC LIMIT 300", (user,))
    if not relay.agentos.settings(user)["included"]:  # (AgentOS out of this Relay: its tasks aren't shown)
        rows = [r for r in rows if not r["managed"]]
    groups: dict[str, list[dict[str, Any]]] = {"running": [], "review": [], "clean": [], "discarded": []}
    # Tasks working in the same place of a repository share its pending changes:
    # only the latest of them asks for a review.
    reviewed: set[tuple] = set()
    queued = queued_messages(relay, [r["id"] for r in rows])
    clones = clones_of(relay, rows)
    # A repair's or a new provider's agent (relay.repair, relay.creations): Relay takes its changes on its panel or
    # page (Apply, Add), not by Merge or Commit, so it's never one to review here
    repairing = {r["conversation_id"] for r in relay.db.all(
        "SELECT conversation_id FROM repairs WHERE user_id = ? AND conversation_id IS NOT NULL", (user,))}

    def describe(row: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
        item = summary(relay, row, runs.get(row["id"]), queued[row["id"]], clones)
        if item["running_run"]:
            group = "running"
        elif row["mode"] != "code":
            return None  # finished chats aren't tasks
        elif row["task_state"] == "discarded":
            group = "discarded"
        elif row["id"] in repairing:
            group = "clean"
        else:
            item["changes"] = tasks.summary(row)
            # Only git has something to merge or commit; in a folder without it
            # the changes are simply there (and can be undone from the chat).
            pending = item["changes"] and item["changes"]["files"] and row["git_mode"] != "none"
            place = (row["git_mode"], row["worktree"] or row["repo_root"], tasks.scope(row))
            if pending and row["git_mode"] == "direct" and place in reviewed:
                pending = False
                item["shared_changes"] = True  # the same changes as a newer task's
            if pending:
                reviewed.add(place)
            group = "review" if pending else "clean"
        if group == "running" and row["mode"] == "code" and row["id"] not in repairing:
            item["changes"] = tasks.summary(row)
        return group, item

    for found in await asyncio.to_thread(lambda: [describe(r) for r in rows]):
        if found:
            groups[found[0]].append(found[1])
    return groups


async def create_conversation(request: Request, relay: Relay):
    data = await body(request)
    tunnel = None
    if data.get("uses_vpn"):  # (its folder through a VPN tunnel: chosen first, so a wrong one makes nothing)
        try:
            tunnel = relay.vpn.choose(data["uses_vpn"])
        except VpnError as exc:
            raise ApiError(str(exc)) from exc
    row = await open_conversation(relay, user_id(request), data, external=started_outside(relay, request))
    if tunnel is not None:
        relay.vpn.take(tunnel, row["directory"])
    relay.scripts.made(request.cookies.get(COOKIE), row["id"])  # (a script's, from the Scripts page: its)
    # (a branch of its own asked for, and why it works in the folder itself instead)
    return {**summary(relay, row), "vpn": vpn_of(relay, row), "in_place": row.get("in_place")}


INSTRUCTIONS_LENGTH = 8000  # characters of a conversation's own instructions


def instructions_of(value: Any) -> str | None:
    """A conversation's own instructions (after its mode's, relay.modes.with_instructions); None: none."""
    text = str(value or "").strip()
    if len(text) > INSTRUCTIONS_LENGTH:
        raise ApiError(f"Instructions are at most {INSTRUCTIONS_LENGTH} characters")
    return text or None


def tools_of(row: dict[str, Any], wanted: Any) -> str | None:
    """Relay's own tools for a conversation after {kind: on} (relay.relay_tools), as kept."""
    try:
        kept = relay_tools.changed(row, wanted)
    except relay_tools.ToolsError as exc:
        raise ApiError(str(exc)) from exc
    if row.get("computer_id") and "computers" in relay_tools.off({"tools": kept}):
        raise ApiError("A computer task works on its computer: it keeps Relay's computer tools")
    return kept


def vpn_of(relay: Relay, row: dict[str, Any]) -> str | None:
    """The VPN tunnel a conversation's WorkBox goes through (by its folder), by name; None: the machine's network."""
    tunnel = relay.vpn.tunnel_for(row["directory"]) if row.get("directory") else None
    return tunnel["name"] if tunnel else None


async def open_conversation(relay: Relay, user: int, data: dict[str, Any], anywhere: bool = False,
                            external: str | None = None) -> dict[str, Any]:
    """A new chat or code task (also for scheduled tasks and repairs); its row.
    `anywhere`: the folder needn't be in a workspace root (Relay's own, e.g. a repair's).
    `external`: who started it outside Relay's pages (web.started_outside)."""
    provider_id = str(data.get("provider_id", ""))
    relay.registry.get(provider_id)  # must exist
    model = data.get("model") or None
    data = {**data, "model": safely(relay.registry.provider(provider_id).model_id, model, model)}
    mode = data.get("mode", "chat")
    if mode not in ("chat", "code"):
        raise ApiError("mode must be 'chat' or 'code'")
    effort = data.get("effort") or None
    if effort not in (None, *EFFORTS):
        raise ApiError(f"Effort is one of {', '.join(EFFORTS)}")
    agent_mode = data.get("agent_mode") or None
    if agent_mode == "build":
        agent_mode = None  # the default
    elif agent_mode and Modes(relay.db).get(user, str(agent_mode)) is None:
        raise ApiError("That mode doesn't exist")
    loop = str(data.get("fallback_loop") or fallback.AUTO)
    if loop not in (fallback.AUTO, fallback.OFF) and fallback.find(fallback.load(relay.db, user), loop) is None:
        raise ApiError("There's no such fallback loop (Settings → Usage)")
    computer_id = data.get("computer_id") or None
    temporary = bool(data.get("temporary"))
    if temporary and (mode != "chat" or computer_id):
        raise ApiError("A temporary chat is a chat: without a project folder or a computer")
    if computer_id:
        if mode != "chat":
            raise ApiError("A computer task isn't a code task")
        relay.computers.get(user, str(computer_id))  # must be the user's
        if not has_tools(relay.registry.get(provider_id)):
            raise ApiError("Working on a computer needs a provider with tools")
    conversation_id = uuid.uuid4().hex
    if mode == "code":
        directory = Path(str(data.get("directory", ""))).expanduser()
        if not directory.is_absolute() or not directory.is_dir() or not (anywhere or within_roots(relay, directory)):
            raise ApiError("Choose a project folder inside a workspace root")
        setup = await asyncio.to_thread(tasks.setup, conversation_id, directory.resolve(),
                                        bool(data.get("isolate", False)))
        if setup.git_mode in ("none", "direct"):
            # Relay follows the changes with its checkpoints (no commits in the user's history).
            setup.base_commit = await asyncio.to_thread(
                checkpoints.take, conversation_id, setup.directory, "Start of the task")
    else:
        setup = tasks.Setup(relay.settings.chat_directory() / conversation_id)
    in_place = setup.in_place if mode == "code" else None
    now = time.time()
    relay.db.execute(
        "INSERT INTO conversations (id, user_id, title, title_auto, provider_id, model, mode, "
        "directory, git_mode, repo_root, base_branch, base_commit, branch, worktree, agent_mode, "
        "effort, computer_id, fallback_loop, created, updated, instructions, tools, temporary, external) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (conversation_id, user, str(data.get("title") or ("Temporary chat" if temporary else "New chat"))[:200],
         0 if data.get("title") or temporary else 1,  # (a temporary chat's title: no model is asked for one)
         provider_id, data.get("model") or None, mode, str(setup.directory), setup.git_mode,
         setup.repo_root, setup.base_branch, setup.base_commit, setup.branch, setup.worktree,
         agent_mode, effort, computer_id, None if loop == fallback.AUTO else loop, now, now,
         instructions_of(data.get("instructions")), tools_of({"computer_id": computer_id}, data.get("tools", {})),
         1 if temporary else 0, external),
    )
    row = relay.db.one("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
    return {**row, "in_place": in_place} if in_place else row


async def get_conversation(request: Request, relay: Relay):
    row = conversation_row(relay, request)
    messages = relay.db.all(
        "SELECT m.id, m.role, m.content, m.attachments, m.run_id, m.created, m.relay_action, r.status, r.error, "
        "r.changes, r.provider_id AS run_provider, r.model AS run_model, r.checkpoint, "
        "r.agent_mode AS run_mode, r.created AS run_started, r.finished AS run_finished "
        "FROM messages m LEFT JOIN runs r ON r.id = m.run_id "
        "WHERE m.conversation_id = ? ORDER BY m.id",
        (row["id"],),
    )
    for message in messages:
        message["changes"] = json.loads(message["changes"]) if message["changes"] else None
        message["attachments"] = json.loads(message["attachments"]) if message["attachments"] else []
        message["relay_action"] = json.loads(message["relay_action"]) if message["relay_action"] else None
    events = relay.db.runs_events([m["run_id"] for m in messages if m["run_id"]])
    for message in messages:
        if message["run_id"]:
            message["events"] = events[message["run_id"]]
    origin = relay.db.one("SELECT id, title FROM conversations WHERE id = ? AND user_id = ?",
                          (row["forked_from"], row["user_id"])) if row["forked_from"] else None
    parent = relay.db.one("SELECT id, title FROM conversations WHERE id = ?",
                          (row["parent_conversation"],)) if row["parent_conversation"] else None
    # A code task's changes as they are now (answers keep what they were after them); none for a repair's or a new
    # provider's agent: its panel or page shows them, and Relay takes them there
    repair = relay.repairs.for_conversation(row["id"])
    changes = await asyncio.to_thread(tasks.summary, row) if row["mode"] == "code" and not repair else None
    item = summary(relay, row)
    # Who answers now: as the run started, whatever the chat is set to since (that's for the next turn)
    going = relay.db.one("SELECT provider_id, model, agent_mode FROM runs WHERE id = ?",
                         (item["running_run"],)) if item["running_run"] else None
    return {**item, "messages": messages, "origin": origin, "parent": parent,
            "running_agent": {"provider": going["provider_id"], "model": going["model"], "mode": going["agent_mode"]}
            if going else None,
            "changes": changes, "repair": repair, "vpn": vpn_of(relay, row),
            "copy": await asyncio.to_thread(copy_status, row)}


def copy_status(row: dict[str, Any]) -> dict[str, Any] | None:
    """A code subagent's changes in its copy that its caller's folder hasn't got (relay.forks)."""
    if row["mode"] != "code" or not row["parent_conversation"]:
        return None
    fork = forks.load(row["id"])
    if fork is None or not fork.folder.is_dir():
        return None
    try:
        files = forks.changed(fork)
    except (forks.ForkError, git.GitError):
        return None
    return {"source": str(fork.source), "files": files} if files else None


async def subagent_copy(request: Request, relay: Relay):
    """Apply a subagent's changes that weren't handed over to its caller's folder (conflict
    markers where they conflict), or discard them; the copy goes either way."""
    row = idle_task(relay, request)
    fork = forks.load(row["id"])
    if fork is None:
        raise ApiError("This subagent has no copy of a folder", 404)
    action = (await body(request)).get("action")
    if action not in ("apply", "discard"):
        raise ApiError("action must be apply or discard")
    caller = relay.db.one("SELECT * FROM conversations WHERE id = ?", (fork.caller,))
    if caller is None and action == "apply":
        raise ApiError("The agent this subagent worked for is gone, and its folder with it", 409)
    if caller is not None:
        # Into the caller's folder as Relay knows it now: fork.json's copy of it may be from before a move.
        fork.source = Path(caller["directory"]).resolve()
    caller = caller or row

    def settle(_row: dict[str, Any]):
        # Applying and removing the copy as one: no run of the subagent starts in between.
        merged = forks.merge(fork, markers=True) if action == "apply" else None
        forks.remove(fork, relay.containers)
        return merged
    try:
        result = await asyncio.to_thread(relay.runs.project_operation, {**caller, "directory": str(fork.source)},
                                         settle)
    except (forks.ForkError, git.GitError) as exc:
        raise ApiError(f"Couldn't apply the changes: {exc}", 409) from exc
    relay.db.execute("UPDATE conversations SET updated = ? WHERE id = ?", (time.time(), row["id"]))
    if result is None:
        return {"ok": True}
    return {"ok": True, "files": result.files, "conflicts": result.conflicts}


async def export_conversation(request: Request) -> Response:
    relay: Relay = request.app.state.relay
    try:
        conversation = await get_conversation(request, relay)
    except ApiError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)
    run = request.query_params.get("run")
    if run:  # (one answer: Relay's SDK, a message's markdown)
        message = next((m for m in conversation["messages"] if m["run_id"] == run and m["role"] == "assistant"), None)
        if message is None:
            return JSONResponse({"error": "No answer of that run here"}, status_code=404)
        return Response(export.answer_markdown(message), media_type="text/markdown")
    try:
        provider_name = relay.registry.get(conversation["provider_id"]).name
    except TemplateError:
        provider_name = conversation["provider_id"]
    # (what a file's name can't have goes; a slash, as in add/done, becomes a dash)
    name = re.sub(r"[^\w\-. ]", "", re.sub(r"[/\\|]", "-", conversation["title"]))
    name = " ".join(name.split()).strip(" .-")[:60].strip() or "conversation"
    # Headers are Latin-1: an ASCII name for old browsers, the real one (UTF-8) for the rest.
    plain = re.sub(r"[^A-Za-z0-9\-. ]", "", name).strip(" .-") or "conversation"
    disposition = f'attachment; filename="{plain}.md"; filename*=UTF-8\'\'{quote(name + ".md")}'
    return Response(export.to_markdown(conversation, provider_name), media_type="text/markdown",
                    headers={"Content-Disposition": disposition})


async def update_conversation(request: Request, relay: Relay):
    row = conversation_row(relay, request)
    data = await body(request)
    updates: dict[str, Any] = {}
    name_it = False
    if "title" in data:
        title = str(data["title"]).strip()
        if not title:
            raise ApiError("Title can't be empty")
        updates["title"] = title[:200]
        updates["title_auto"] = 0  # the user's own title is never replaced
    if "pinned" in data:
        updates["pinned"] = 1 if data["pinned"] else 0
    if "temporary" in data:
        if data["temporary"]:
            raise ApiError("A chat can't become temporary; a temporary one can be kept")
        updates["temporary"] = 0  # kept: a chat like any other from now on
        first = relay.db.one("SELECT content FROM messages WHERE conversation_id = ? AND role = 'user' ORDER BY id "
                             "LIMIT 1", (row["id"],))
        if row["temporary"] and row["title"] == "Temporary chat" and first and "title" not in data:
            # Named as any chat is: its first message, then the model's title for it (a simulated user's kept one
            # stayed "Temporary chat")
            updates.update(title=make_title(first["content"]), title_auto=1)
            name_it = True
    if "model" in data:
        updates["model"] = data["model"] or None
    if "effort" in data:
        effort = data["effort"] or None
        if effort not in (None, "default", *EFFORTS):
            raise ApiError(f"Effort is one of {', '.join(EFFORTS)}, or default")
        updates["effort"] = None if effort == "default" else effort
    if "fallback_loop" in data:
        choice = str(data["fallback_loop"] or fallback.AUTO)
        if choice not in (fallback.AUTO, fallback.OFF) and \
                fallback.find(fallback.load(relay.db, row["user_id"]), choice) is None:
            raise ApiError("There's no such fallback loop (Settings → Usage)")
        updates["fallback_loop"] = None if choice == fallback.AUTO else choice
        info = json.loads(row["limit_info"]) if row["limit_info"] else {}
        if info.get("restart"):  # waiting to go round its loop again: round the chosen one instead
            loop = fallback.loop_for(relay.db, row["user_id"], updates["fallback_loop"], row["provider_id"],
                                     row["model"])
            if loop and loop["entries"]:
                info["loop"] = {**(info.get("loop") or {}), "id": loop["id"], "name": loop["name"]}
                info["restart"] = dict(loop["entries"][0])
            else:
                info.pop("restart")
                info.pop("loop", None)
                updates["resume_at"] = None
            updates["limit_info"] = json.dumps(info)
    if "uses_vpn" in data:  # (its folder through a tunnel, or out of them again: relay.vpn)
        try:
            if data["uses_vpn"]:
                relay.vpn.take(relay.vpn.choose(data["uses_vpn"]), row["directory"])
            else:
                relay.vpn.leave(row["directory"])
        except VpnError as exc:
            raise ApiError(str(exc)) from exc
    if "instructions" in data:
        updates["instructions"] = instructions_of(data["instructions"])
    if "tools" in data:
        updates["tools"] = tools_of(row, data["tools"])
    if "agent_mode" in data:
        mode = data["agent_mode"] or None
        if mode and Modes(relay.db).get(row["user_id"], str(mode)) is None:
            raise ApiError("That mode doesn't exist")
        updates["agent_mode"] = None if mode == "build" else mode
    if "provider_id" in data and data["provider_id"] != row["provider_id"]:
        relay.registry.get(str(data["provider_id"]))
        # Another provider can't continue this provider's session; it gets the
        # conversation handed over instead.
        updates["provider_id"] = str(data["provider_id"])
        updates["provider_session"] = None
        updates["resume_at"] = None  # going on elsewhere: no waiting for the old one's limit
        updates["limit_info"] = None
        updates["fork_session"] = None
        updates["fork_before"] = None
    if updates:
        assignments = ", ".join(f"{k} = ?" for k in updates)
        relay.db.execute(f"UPDATE conversations SET {assignments} WHERE id = ?",
                         (*updates.values(), row["id"]))
    if name_it:  # (a model may take a while: the title follows)
        threading.Thread(target=relay.runs.title_now, args=(row["id"],), daemon=True).start()
    row = relay.db.one("SELECT * FROM conversations WHERE id = ?", (row["id"],))
    return {**summary(relay, row), "vpn": vpn_of(relay, row)}


async def delete_conversation(request: Request, relay: Relay):
    row = conversation_row(relay, request)
    # Its subagents' conversations go too (those are hidden and have no worktrees).
    for child in relay.db.all("SELECT * FROM conversations WHERE parent_conversation = ?", (row["id"],)):
        await remove_conversation(relay, child)
    await remove_conversation(relay, row)
    return {"ok": True}


async def remove_conversation(relay: Relay, row: dict[str, Any]) -> None:
    if relay.production.promoting(row):
        raise ApiError("It's being promoted right now: delete it when that's done", 409)
    running = relay.runs.active_run_for(row["id"])
    await relay.shells.end_for(row["id"])  # (its kept shell, if any)
    # The agent must be gone before its worktree and attachments are removed.
    if running and not await asyncio.to_thread(relay.runs.stop_and_wait, running, 15, "it was deleted"):
        raise ApiError("The agent didn't stop in time; try again", 409)
    if row["git_mode"] == "worktree" and row["task_state"] == "open":
        await asyncio.to_thread(relay.runs.project_operation, row, tasks.discard, repository=True)
    relay.db.execute("DELETE FROM conversations WHERE id = ?", (row["id"],))
    relay.uploads.delete_conversation(row["id"])
    handover.remove(row["id"])
    copy = forks.load(row["id"])  # a subagent's copy of its caller's folder
    if copy is not None:
        await asyncio.to_thread(forks.remove, copy, relay.containers)
    checkpoints.remove(row["id"])
    relay.delegation.remove_script(row.get("tools_token"))
    if not relay.db.one("SELECT 1 FROM conversations WHERE directory = ?", (row["directory"],)):
        relay.db.execute("DELETE FROM shares WHERE directory = ?", (row["directory"],))  # its network drive
    if row["mode"] == "chat":
        scratch = Path(row["directory"])
        shared = relay.db.one("SELECT 1 FROM conversations WHERE directory = ?", (row["directory"],))
        if scratch.parent == relay.settings.chat_directory() and not shared:  # forks share it
            shutil.rmtree(scratch, ignore_errors=True)
            if relay.containers is not None:  # its container and home go with it
                await asyncio.to_thread(relay.containers.discard, scratch)
    relay.production.forget_clone(row)  # a service's WorkBox: its folder too, once nobody works there


async def send_message(request: Request, relay: Relay):
    row = conversation_row(relay, request)
    if row["task_state"] == "discarded":
        raise ApiError("This task was discarded; start a new one to continue")
    data = await body(request)
    text = str(data.get("text", ""))
    ids = data.get("attachments") or []
    if not isinstance(ids, list):
        raise ApiError("attachments must be a list of upload ids")
    if not text.strip() and not ids:
        raise ApiError("Message is empty")
    attachments = relay.uploads.claim([str(i) for i in ids], user_id(request), row["id"])
    if not text.strip():
        text = "See the attached files."
    first = relay.db.one("SELECT 1 FROM messages WHERE conversation_id = ? LIMIT 1", (row["id"],))
    if first is None and row["title"] == "New chat":
        relay.db.execute("UPDATE conversations SET title = ? WHERE id = ?",
                         (make_title(text), row["id"]))
        row["title"] = make_title(text)
    run_id = await asyncio.to_thread(relay.runs.start, row, text, attachments)
    return {"run_id": run_id}


async def queue_message(request: Request, relay: Relay):
    """Send a message when the current answer is done (right away if nothing runs)."""
    row = conversation_row(relay, request)
    if row["task_state"] == "discarded":
        raise ApiError("This task was discarded; start a new one to continue")
    if not relay.runs.active_run_for(row["id"]):
        return await send_message(request, relay)
    data = await body(request)
    text = str(data.get("text", ""))
    ids = data.get("attachments") or []
    if not isinstance(ids, list):
        raise ApiError("attachments must be a list of upload ids")
    if not text.strip() and not ids:
        raise ApiError("Message is empty")
    attachments = relay.uploads.claim([str(i) for i in ids], user_id(request), row["id"])
    queued = relay.db.execute(
        "INSERT INTO queued_messages (conversation_id, content, attachments, created) VALUES (?, ?, ?, ?)",
        (row["id"], text if text.strip() else "See the attached files.",
         json.dumps([a.public() for a in attachments]) if attachments else None, time.time())).lastrowid
    if not relay.runs.active_run_for(row["id"]):
        # The run ended just now, before it could pick this up: send it ourselves.
        return await send_queued(relay, row, queued)
    return {"queued": queued}


async def send_queued_now(request: Request, relay: Relay):
    """Send a queued message now, e.g. after the run it waited for failed."""
    row = idle_task(relay, request)
    return await send_queued(relay, row, int(request.path_params["queued_id"]))


async def send_queued(relay: Relay, row: dict[str, Any], queued_id: int) -> dict[str, Any]:
    queued = relay.db.one("SELECT * FROM queued_messages WHERE id = ? AND conversation_id = ?",
                          (queued_id, row["id"]))
    if queued is None:
        raise ApiError("That message isn't waiting any more", 404)
    ids = [a["id"] for a in json.loads(queued["attachments"])] if queued["attachments"] else []
    attachments = relay.uploads.in_conversation(row["id"], ids) if ids else []
    run_id = await asyncio.to_thread(relay.runs.start, row, queued["content"], attachments)
    relay.db.execute("DELETE FROM queued_messages WHERE id = ?", (queued_id,))  # sent: not before
    return {"run_id": run_id}


async def unqueue_message(request: Request, relay: Relay):
    row = conversation_row(relay, request)
    relay.db.execute("DELETE FROM queued_messages WHERE id = ? AND conversation_id = ?",
                     (request.path_params["queued_id"], row["id"]))
    return {"ok": True}


async def retry_message(request: Request, relay: Relay):
    """Answer a message again: the last one, or with `message_id` an earlier one,
    optionally with new `text`. Answering an earlier or edited message rewinds
    the conversation to it; everything after it goes.

    Chat APIs simply get the shorter history. opencode continues from a copy of
    its session up to that message; other agents start a fresh session that
    gets the conversation so far handed over. Retrying the last message (without
    new text) gives agents a new turn instead, as their sessions keep the failed one.
    """
    row = idle_task(relay, request)
    if row["task_state"] == "discarded":
        raise ApiError("This task was discarded; start a new one to continue")
    data = await body(request)
    users = relay.db.all("SELECT * FROM messages WHERE conversation_id = ? AND role = 'user' "
                         "ORDER BY id", (row["id"],))
    if not users:
        raise ApiError("There is no message to answer again")
    target = users[-1]
    if data.get("message_id") is not None:
        target = next((m for m in users if str(m["id"]) == str(data["message_id"])), None)
        if target is None:
            raise ApiError("That message isn't in this conversation", 404)
    text = target["content"]
    if data.get("text") is not None:
        text = str(data["text"])
        if not text.strip():
            raise ApiError("Message is empty")
    ids = [a["id"] for a in json.loads(target["attachments"])] if target["attachments"] else []
    attachments = relay.uploads.in_conversation(row["id"], ids)
    provider = relay.registry.provider(row["provider_id"])
    # An edit (text given) always takes the place of the message, even unchanged.
    rewind = provider.REWINDABLE or data.get("text") is not None or target["id"] != users[-1]["id"]
    if not rewind:
        action = None
        if target["relay_action"]:
            action = {"kind": "retry", "from": {"provider_id": row["provider_id"], "model": row["model"]}}
        run_id = await asyncio.to_thread(relay.runs.start, row, text, attachments, relay_action=action)
        return {"run_id": run_id, "rewound": False}
    if data.get("restore_files"):
        # Put the files back as they were before the turn being redone.
        answer = relay.db.one(
            "SELECT r.checkpoint FROM messages m JOIN runs r ON r.id = m.run_id "
            "WHERE m.conversation_id = ? AND m.id > ? ORDER BY m.id LIMIT 1", (row["id"], target["id"]))
        if not answer or not answer["checkpoint"]:
            raise ApiError("There's no checkpoint of the files before that message")
        try:
            await asyncio.to_thread(relay.runs.project_operation, row, tasks.rewind,
                                    answer["checkpoint"], target["content"][:60], bool(data.get("force")))
        except tasks.OthersChanges as exc:
            return JSONResponse({"error": str(exc), "others": exc.subjects}, status_code=409)
    session: dict[str, Any] = {}
    if not provider.REWINDABLE:
        # The agent's session holds the turns being taken back.
        fork = provider.FORKS == "any" and row["provider_session"] and target["provider_ref"]
        session = {"provider_session": None,
                   "fork_session": row["provider_session"] if fork else None,
                   "fork_before": target["provider_ref"] if fork else None}
    with relay.db.transaction() as conn:
        conn.execute("DELETE FROM runs WHERE id IN (SELECT run_id FROM messages "
                     "WHERE conversation_id = ? AND id > ? AND run_id IS NOT NULL)",
                     (row["id"], target["id"]))
        conn.execute("DELETE FROM messages WHERE conversation_id = ? AND id > ?",
                     (row["id"], target["id"]))
        conn.execute("UPDATE messages SET content = ?, provider_ref = NULL, relay_action = ? WHERE id = ?",
                     (text, None if data.get("text") is not None else target["relay_action"], target["id"]))
        if session:
            conn.execute("UPDATE conversations SET provider_session = ?, fork_session = ?, "
                         "fork_before = ? WHERE id = ?",
                         (session["provider_session"], session["fork_session"],
                          session["fork_before"], row["id"]))
    row = relay.db.one("SELECT * FROM conversations WHERE id = ?", (row["id"],))
    run_id = await asyncio.to_thread(relay.runs.start, row, text, attachments, target["id"])
    return {"run_id": run_id, "rewound": True}


async def conversation_commands(request: Request, relay: Relay):
    """The agent's own slash commands here, and the efforts its model offers."""
    row = conversation_row(relay, request)
    provider = relay.registry.provider(row["provider_id"])
    commands = await asyncio.to_thread(provider.commands, Path(row["directory"])) if provider.TOOLS else []
    efforts = await asyncio.to_thread(provider.efforts, row["model"])
    return {"commands": commands, "efforts": efforts}


async def rewind_files(request: Request, relay: Relay):
    """Make a code task's files what they were before a run (`run_id`), at a
    `checkpoint` (e.g. the `undo` returned here), or at the start (`to_start`).
    From Relay's record (a task on its own branch: a new commit there); nothing is rewritten."""
    row = idle_task(relay, request)
    if row["mode"] != "code" or row["task_state"] != "open":
        raise ApiError("Only an open code task has files to rewind")
    data = await body(request)
    target, label = data.get("checkpoint"), ""
    if data.get("to_start"):
        if not row["base_commit"]:
            raise ApiError("This task has no starting point to go back to")
        target, label = row["base_commit"], "the task started"
    if data.get("run_id"):
        run = relay.db.one(
            "SELECT r.checkpoint, (SELECT content FROM messages WHERE conversation_id = r.conversation_id "
            "AND role = 'user' AND id < (SELECT id FROM messages WHERE run_id = r.id) ORDER BY id DESC LIMIT 1) "
            "AS prompt FROM runs r WHERE r.id = ? AND r.conversation_id = ?", (str(data["run_id"]), row["id"]))
        if run is None or not run["checkpoint"]:
            raise ApiError("There's no record of the files before that answer")
        target, label = run["checkpoint"], (run["prompt"] or "")[:60]
    if not target:
        raise ApiError("Say which answer to rewind to")
    try:
        undo = await asyncio.to_thread(relay.runs.project_operation, row, tasks.rewind,
                                       str(target), label, bool(data.get("force")))
    except tasks.OthersChanges as exc:
        return JSONResponse({"error": str(exc), "others": exc.subjects}, status_code=409)
    return {"ok": True, "undo": undo, "changes": await asyncio.to_thread(tasks.summary, row)}


async def continue_now(request: Request, relay: Relay):
    """Go on now after a model stopped: with the same one, or with another one
    ({"provider_id", "model"}), which then takes over the conversation."""
    row = idle_task(relay, request)
    data = await body(request)
    info = json.loads(row["limit_info"]) if row["limit_info"] else {}
    previous = relay.db.one("SELECT id, provider_id, model, status FROM runs WHERE conversation_id = ? "
                            "ORDER BY created DESC, rowid DESC LIMIT 1", (row["id"],))
    source = previous or info
    stopped_by = {"provider_id": source.get("provider_id"), "model": source.get("model")}
    stopped = previous["status"] in ("failed", "interrupted") if previous else bool(info.get("provider_id"))
    updates: dict[str, Any] = {"resume_at": None}
    provider_id = str(data.get("provider_id") or row["provider_id"])
    model = data.get("model") if "model" in data else row["model"]
    if provider_id != row["provider_id"] or (model or None) != (row["model"] or None):
        relay.registry.get(provider_id)
        updates.update(provider_id=provider_id, model=model or None)
        if provider_id != row["provider_id"]:
            updates.update(provider_session=None, fork_session=None, fork_before=None, effort=None)
    relay.db.execute(f"UPDATE conversations SET {', '.join(f'{k} = ?' for k in updates)} WHERE id = ?",
                     (*updates.values(), row["id"]))
    row = relay.db.one("SELECT * FROM conversations WHERE id = ?", (row["id"],))
    # Describe the latest attempt, not a stop left behind by an earlier fallback.
    if stopped and stopped_by.get("provider_id") and (provider_id != stopped_by["provider_id"]
                                                     or (model or None) != (stopped_by.get("model") or None)):
        name = relay.runs.display_name(stopped_by["provider_id"], stopped_by.get("model"))
        events = relay.db.runs_events([previous["id"]]).get(previous["id"], []) if previous else []
        asked = relay.db.one("SELECT content FROM messages WHERE conversation_id = ? AND role = 'user' "
                             "ORDER BY id DESC LIMIT 1", (row["id"],))
        if asked and not any(e.get("type") in ("text", "tool_call", "reasoning") for e in events):
            # It did nothing (a model that can't be used, say): no work to go on with, the message to answer.
            # ("Continue where it left off" had a simulated user's next model look for work that wasn't there.)
            prompt = (f"{name} stopped before it answered, so you answer instead. "
                      f"The user's message:\n\n{asked['content']}")
        else:
            prompt = (f"{name} stopped, so you take over. Please continue where it left off; "
                      "if the task was done, say so briefly.")
    else:
        prompt = tunables.get("limits.resume_prompt")
    run_id = await asyncio.to_thread(relay.runs.start, row, prompt,
                                    relay_action={"kind": "continue", "from": stopped_by})
    return {"run_id": run_id}


async def resume_later(request: Request, relay: Relay):
    """Go on when the usage limit resets ({"when": "reset"}), or not ({"cancel": true})."""
    row = conversation_row(relay, request)
    data = await body(request)
    if data.get("cancel"):
        relay.db.execute("UPDATE conversations SET resume_at = NULL WHERE id = ?", (row["id"],))
    else:
        info = json.loads(row["limit_info"]) if row["limit_info"] else {}
        if not info.get("resets_at"):
            raise ApiError("There's no usage limit with a known reset time")
        relay.db.execute("UPDATE conversations SET resume_at = ? WHERE id = ?",
                         (max(info["resets_at"], time.time()) + tunables.get("limits.margin"), row["id"]))
    return summary(relay, relay.db.one("SELECT * FROM conversations WHERE id = ?", (row["id"],)))


async def set_timed(request: Request, relay: Relay):
    """/timed: {"text": "[min] max [optimal] [task]"} sets the timer (relay.timed) and gives back the task
    to send, or the same as fields ({"max", "min", "optimal" in minutes, "task"}: Relay's SDK, which a task
    starting with a number can't be mistaken in); {"off": true} ends it. The conversation, with the task as "task"."""
    row = conversation_row(relay, request)
    data = await body(request)
    task = ""
    if data.get("off"):
        found = json.loads(row["timed"]) if row.get("timed") else None
        # (a running timer ends; one that ended already, at its max, is put away)
        value = json.dumps({**found, "ended": "you stopped the timer"}) if found and not found.get("ended") else None
    else:
        try:
            if data.get("max") is not None:
                timer = timed.make(*(None if data.get(k) is None else float(data[k])
                                     for k in ("min", "max", "optimal")))
                task = str(data.get("task") or "")
            else:
                timer, task = timed.parse(str(data.get("text") or ""))
        except (timed.TimedError, TypeError, ValueError) as exc:
            raise ApiError(str(exc) if isinstance(exc, timed.TimedError)
                           else "min, max and optimal are minutes") from exc
        value = json.dumps(timer)
    relay.db.execute("UPDATE conversations SET timed = ? WHERE id = ?", (value, row["id"]))
    return {**summary(relay, relay.db.one("SELECT * FROM conversations WHERE id = ?", (row["id"],))), "task": task}


async def move_to_code(request: Request, relay: Relay):
    """Turn a chat into a code task in a project folder, and go on there.

    The agent keeps its session where it can (opencode moves it along); one
    that keeps sessions per folder (Claude Code) or another provider gets the
    conversation handed over. The chat's scratch folder goes when it's empty.
    """
    row = conversation_row(relay, request)
    if row["mode"] != "chat":
        raise ApiError("This is a code task already")
    if row["temporary"]:
        raise ApiError("Keep this temporary chat first; then it can go on in a project folder")
    if row["parent_conversation"]:
        raise ApiError("A subagent's chat can't be moved")
    if relay.runs.active_run_for(row["id"]):
        raise ApiError("Wait until the answer is done", 409)
    data = await body(request)
    provider_id = str(data.get("provider_id") or row["provider_id"])
    template = relay.registry.get(provider_id)
    if not has_tools(template):
        raise ApiError(f"{template.name} can't work on files; choose a provider with tools")
    directory = Path(str(data.get("directory", ""))).expanduser()
    if not directory.is_absolute() or not directory.is_dir() or not within_roots(relay, directory):
        raise ApiError("Choose a project folder inside a workspace root")
    setup = await asyncio.to_thread(tasks.setup, row["id"], directory.resolve(), bool(data.get("isolate", False)))
    # What the chat's agent made goes along: moved (copied while a fork of the chat still works in
    # its folder). A name the project has already stays behind, and is said.
    scratch = Path(row["directory"])
    shared = relay.db.one("SELECT 1 FROM conversations WHERE directory = ? AND id != ?", (row["directory"], row["id"]))
    left_behind: list[str] = []
    own_scratch = scratch.parent == relay.settings.chat_directory() and scratch.is_dir()
    if own_scratch:
        left_behind = await asyncio.to_thread(bring_along, scratch, setup.directory, bool(shared))
    if setup.git_mode in ("none", "direct"):  # (the task starts with what the chat made)
        setup.base_commit = await asyncio.to_thread(
            checkpoints.take, row["id"], setup.directory, "Start of the task")
    updates: dict[str, Any] = {
        "mode": "code", "directory": str(setup.directory), "git_mode": setup.git_mode,
        "repo_root": setup.repo_root, "base_branch": setup.base_branch, "base_commit": setup.base_commit,
        "branch": setup.branch, "worktree": setup.worktree, "task_state": "open", "updated": time.time(),
        "computer_id": None,  # a computer task moved here works in the project, not on that computer
    }
    if provider_id != row["provider_id"] or row.get("computer_id"):
        # (A computer task's session was told the work happens elsewhere: a new one, handed over.)
        updates.update(provider_id=provider_id, provider_session=None, fork_session=None, fork_before=None)
        if provider_id != row["provider_id"]:
            updates.update(model=data.get("model") or None, effort=None)
    if "model" in data and provider_id == row["provider_id"]:
        updates["model"] = data["model"] or None
    assignments = ", ".join(f"{k} = ?" for k in updates)
    relay.db.execute(f"UPDATE conversations SET {assignments} WHERE id = ?", (*updates.values(), row["id"]))
    if own_scratch and not shared and not left_behind:
        with contextlib.suppress(OSError):
            scratch.rmdir()
        if relay.containers is not None:  # its WorkBox and home: the chat works in the project now
            await asyncio.to_thread(relay.containers.discard, scratch)
    return {**summary(relay, relay.db.one("SELECT * FROM conversations WHERE id = ?", (row["id"],))),
            "left_behind": str(scratch) if left_behind else None, "not_moved": left_behind}


def bring_along(scratch: Path, project: Path, copy: bool) -> list[str]:
    """A chat's files into the project it moves to; the names that stayed behind (the project has
    them already, or they couldn't be moved)."""
    stayed = []
    for entry in sorted(scratch.iterdir()):
        target = project / entry.name
        if target.exists() or target.is_symlink():
            stayed.append(entry.name)
            continue
        try:
            if not copy:
                shutil.move(str(entry), str(target))
            elif entry.is_dir() and not entry.is_symlink():
                shutil.copytree(entry, target, symlinks=True)
            else:
                shutil.copy2(entry, target, follow_symlinks=False)
        except OSError:
            stayed.append(entry.name)
    return stayed


async def share_conversation(request: Request, relay: Relay):
    """A code task's folder as a network drive (relay.webdav), e.g. to try its code on your computer."""
    row = conversation_row(relay, request)
    if row["mode"] != "code":
        raise ApiError("Only a code task has a project folder to share")
    data = await body(request)
    read_only = data.get("read_only")
    share = webdav.create(relay.db, user_id(request), Path(row["directory"]), row["title"],
                          None if read_only is None else bool(read_only))
    return public_share(share)


async def list_shares(request: Request, relay: Relay):
    return [public_share(s) for s in webdav.for_user(relay.db, user_id(request))]


async def delete_share(request: Request, relay: Relay):
    webdav.remove(relay.db, user_id(request), request.path_params["token"])
    return {"ok": True}


async def clear_conversation(request: Request, relay: Relay):
    """/clear: the agent forgets the conversation so far and starts a fresh session; the user still
    sees it all. What comes next is its whole history (nothing is handed over). With
    `delete_history` (/clear delete-history) it's gone for everyone: the messages, their runs and
    attachments, and the subagents' chats. A code task's files and its changes stay."""
    row = conversation_row(relay, request)
    if relay.runs.active_run_for(row["id"]):
        raise ApiError("Wait until the answer is done, or stop it", 409)
    last = relay.db.one("SELECT max(id) AS id FROM messages WHERE conversation_id = ?", (row["id"],))["id"]
    if last is None:
        raise ApiError("There's nothing to clear yet")
    wipe = bool((await body(request) if await request.body() else {}).get("delete_history"))
    if wipe:
        for child in relay.db.all("SELECT * FROM conversations WHERE parent_conversation = ?", (row["id"],)):
            await remove_conversation(relay, child)
        with relay.db.transaction() as conn:
            conn.execute("DELETE FROM runs WHERE conversation_id = ?", (row["id"],))  # (their events with them)
            conn.execute("DELETE FROM messages WHERE conversation_id = ?", (row["id"],))
        relay.uploads.delete_conversation(row["id"])
        handover.remove(row["id"])
    relay.db.execute("UPDATE conversations SET cleared_after = ?, provider_session = NULL, fork_session = NULL, "
                     "fork_before = NULL, updated = ? WHERE id = ?",
                     (None if wipe else last, time.time(), row["id"]))
    return summary(relay, relay.db.one("SELECT * FROM conversations WHERE id = ?", (row["id"],)))


async def fork_conversation(request: Request, relay: Relay):
    """A new conversation with this one's messages up to `message_id` (default:
    all of them), to go on from there separately."""
    row = conversation_row(relay, request)
    data = await body(request)
    made = await fork_row(relay, row, data.get("message_id"), external=started_outside(relay, request))
    relay.scripts.made(request.cookies.get(COOKIE), made["id"])
    return summary(relay, made)


async def fork_row(relay: Relay, row: dict[str, Any], message_id: Any = None, managed: bool = False,
                   external: str | None = None) -> dict[str, Any]:
    """Fork the conversation `row` (also for AgentOS, `managed`); the fork's row. The user's fork of a
    managed chat is theirs. `external`: who made the fork outside Relay's pages (web.started_outside)."""
    if row["task_state"] == "discarded":
        raise ApiError("This task was discarded; there's nothing to fork")
    if relay.runs.active_run_for(row["id"]):
        raise ApiError("Wait until the answer is done", 409)
    messages = relay.db.all("SELECT * FROM messages WHERE conversation_id = ? ORDER BY id", (row["id"],))
    kept = messages
    if message_id is not None:
        cut = next((i for i, m in enumerate(messages) if str(m["id"]) == str(message_id)), None)
        if cut is None:
            raise ApiError("That message isn't in this conversation", 404)
        kept = messages[:cut + 1]
    while kept and kept[-1]["role"] == "user":
        kept = kept[:-1]  # a question without its answer: the fork starts before it
    if not kept:
        raise ApiError("There's nothing to fork yet")
    new_id = uuid.uuid4().hex
    if row["mode"] == "code":
        setup = await asyncio.to_thread(tasks.fork, row, new_id)
        if setup.git_mode in ("none", "direct") and row["base_commit"]:
            # The fork shares the folder; its changes count from the original's start, which is
            # in the original's checkpoints: the fork's own start from a copy of them.
            await asyncio.to_thread(checkpoints.copy, row["id"], new_id)
            await asyncio.to_thread(checkpoints.take, new_id, setup.directory, "Fork of a task")
            setup.base_commit = row["base_commit"]
    else:
        setup = tasks.Setup(Path(row["directory"]))  # a chat's session lives in its folder
    provider = relay.registry.provider(row["provider_id"])
    # Agents continue from a copy of their session where they can: in the same
    # folder (Claude Code finds sessions per folder), at the end or, for
    # opencode, before the provider's own message. Otherwise: a handover.
    after = messages[len(kept)] if len(kept) < len(messages) else None
    fork_session = fork_before = None
    cleared = row.get("cleared_after")
    before_clear = bool(cleared) and kept[-1]["id"] <= cleared  # (the session since isn't of then: a handover)
    if (provider.FORKS != "none" and row["provider_session"] and not before_clear
            and str(setup.directory) == row["directory"]):
        if after is None:
            fork_session = row["provider_session"]
        elif provider.FORKS == "any" and after["provider_ref"]:
            fork_session, fork_before = row["provider_session"], after["provider_ref"]
    now = time.time()
    title = f"{row['title']} (fork)"[:200]
    ids = [a["id"] for m in kept if m["attachments"] for a in json.loads(m["attachments"])]
    relay.db.execute(
        "INSERT INTO conversations (id, user_id, title, title_auto, provider_id, model, mode, "
        "directory, git_mode, repo_root, base_branch, base_commit, branch, worktree, forked_from, "
        "fork_session, fork_before, agent_mode, effort, computer_id, fallback_loop, managed, external, created, "
        "updated) VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (new_id, row["user_id"], title, row["provider_id"], row["model"], row["mode"],
         str(setup.directory), setup.git_mode, setup.repo_root, setup.base_branch, setup.base_commit,
         setup.branch, setup.worktree, row["id"], fork_session, fork_before, row["agent_mode"], row["effort"],
         row.get("computer_id"), row.get("fallback_loop"), int(managed), external, now, now),
    )
    copies = await asyncio.to_thread(relay.uploads.copy, ids, row["id"], new_id)
    with relay.db.transaction() as conn:
        for m in kept:
            run_id = None
            if m["run_id"]:
                run_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO runs (id, conversation_id, status, error, usage, created, finished, "
                    "changes, provider_id, model, checkpoint, after_checkpoint, agent_mode, effort, session) "
                    "SELECT ?, ?, status, error, usage, created, finished, changes, provider_id, model, "
                    "checkpoint, after_checkpoint, agent_mode, effort, session FROM runs WHERE id = ?",
                    (run_id, new_id, m["run_id"]))
                conn.execute("INSERT INTO run_events (run_id, seq, event) "
                             "SELECT ?, seq, event FROM run_events WHERE run_id = ?", (run_id, m["run_id"]))
            attachments = None
            if m["attachments"]:
                listed = [copies.get(a["id"]) for a in json.loads(m["attachments"])]
                attachments = json.dumps([a for a in listed if a]) if any(listed) else None
            copied = conn.execute(
                "INSERT INTO messages (conversation_id, role, content, attachments, run_id, created, "
                "provider_ref, relay_action) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (new_id, m["role"], m["content"], attachments, run_id, m["created"], m["provider_ref"],
                 m["relay_action"])
            ).lastrowid
            if cleared and not before_clear and m["id"] <= cleared:  # the fork's agent starts where this one's did
                conn.execute("UPDATE conversations SET cleared_after = ? WHERE id = ?", (copied, new_id))
    return relay.db.one("SELECT * FROM conversations WHERE id = ?", (new_id,))


async def task_changes(request: Request, relay: Relay):
    row = conversation_row(relay, request)
    return await asyncio.to_thread(tasks.describe, row)


async def merge_task(request: Request, relay: Relay):
    row = idle_task(relay, request)
    new_base = await asyncio.to_thread(relay.runs.project_operation, row, tasks.merge, repository=True)
    relay.db.execute("UPDATE conversations SET base_commit = ?, updated = ? WHERE id = ?",
                     (new_base, time.time(), row["id"]))
    return {"ok": True, "merged_into": row["base_branch"]}


async def mark_reviewed(request: Request, relay: Relay):
    """A task working in its folder: its changes are seen. Other open tasks in the same folder
    share those changes (the tasks overview shows them once), so they count from here too."""
    row = idle_task(relay, request)
    await asyncio.to_thread(relay.runs.project_operation, row, lambda r: _mark_reviewed(relay, r))
    await _others_reviewed(relay, row)
    relay.db.execute("UPDATE conversations SET updated = ? WHERE id = ?", (time.time(), row["id"]))
    return {"ok": True}


async def _others_reviewed(relay: Relay, row: dict[str, Any]) -> None:
    for other in relay.db.all("SELECT * FROM conversations WHERE user_id = ? AND id != ? AND task_state = 'open' "
                              "AND git_mode IN ('none', 'direct') AND directory = ?",
                              (row["user_id"], row["id"], row["directory"])):
        if not relay.runs.active_run_for(other["id"]):
            try:
                await asyncio.to_thread(relay.runs.project_operation, other, lambda r: _mark_reviewed(relay, r))
            except (tasks.TaskError, ProjectBusy):
                pass


def _mark_reviewed(relay: Relay, row: dict[str, Any]) -> None:
    relay.db.execute("UPDATE conversations SET base_commit = ? WHERE id = ?", (tasks.mark_reviewed(row), row["id"]))


async def commit_task(request: Request, relay: Relay):
    """Commit the task's folder to the user's repository, with their message: the one way
    Relay writes to their git history (for a task in a repository's folder)."""
    row = idle_task(relay, request)
    data = await body(request)
    # (a task in a subfolder commits only that: agents elsewhere in the repository don't hold it up)
    made = await asyncio.to_thread(relay.runs.project_operation, row, tasks.commit,
                                  str(data.get("message") or ""), repository=tasks.scope(row) is None)
    # (committed is more than seen: the folder's changes count from here, as after Mark as reviewed)
    try:
        await asyncio.to_thread(relay.runs.project_operation, row, lambda r: _mark_reviewed(relay, r))
        await _others_reviewed(relay, row)
    except (tasks.TaskError, ProjectBusy) as exc:
        print(f"relay: committed, but couldn't mark it as reviewed: {exc}", file=sys.stderr, flush=True)
    relay.db.execute("UPDATE conversations SET updated = ? WHERE id = ?", (time.time(), row["id"]))
    return {"commit": made, "as_relay": not git.has_identity(Path(row["repo_root"]))}


async def discard_task(request: Request, relay: Relay):
    row = idle_task(relay, request)
    await relay.shells.end_for(row["id"])  # (its kept shell works in the branch that goes)
    await asyncio.to_thread(relay.runs.project_operation, row, tasks.discard, repository=True)
    relay.db.execute("UPDATE conversations SET task_state = 'discarded', updated = ? WHERE id = ?",
                     (time.time(), row["id"]))
    return {"ok": True}
