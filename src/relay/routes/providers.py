"""Providers: templates, models and their health, tests, and repairs."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from starlette.requests import Request

from relay import health, ranking
from relay.providers import BROKEN, KINDS, ProviderError, TemplateError, draft_template, safely
from relay.routes.conversations import open_conversation
from relay.web import SECRET_MASK, ApiError, body, has_tools, masked, provider_detail, user_id, without_about

if TYPE_CHECKING:
    from relay.app import Relay


GUIDE = Path(__file__).resolve().parents[3] / "docs" / "ADDING-A-PROVIDER.md"


async def provider_guide(request: Request, relay: Relay):
    """How to add a provider Relay doesn't know (docs/ADDING-A-PROVIDER.md), for Settings → Add provider."""
    return {"markdown": GUIDE.read_text()}


async def provider_kinds(request: Request, relay: Relay):
    """Each kind as Settings shows it; one added to Relay (not one of its own) with its folder."""
    from relay.providers import ORDER
    return [{**described, "builtin": cls.KIND in ORDER, "folder": _folder(cls), "sign_in": cls.SIGN_IN}
            for cls in KINDS.values() if (described := safely(cls.describe, None))]


def _folder(cls) -> str:
    """A kind's folder, as providers/<name>."""
    files = safely(cls.support_files, ())
    return str(Path(files[0]).parent.relative_to("relay")) if files and files[0].startswith("relay/") else ""


async def list_providers(request: Request, relay: Relay):
    templates = relay.registry.templates()
    latest: dict[str, dict[str, Any]] = {}  # each provider's most recent model status
    for entry in health.statuses(relay.db).values():
        if entry["checked"] > latest.get(entry["provider_id"], {}).get("checked", 0):
            latest[entry["provider_id"]] = entry
    return {
        "providers": [
            {**masked(t), "label": KINDS[t.kind].LABEL, "tools": has_tools(t),
             "rewindable": KINDS[t.kind].REWINDABLE,
             "detail": provider_detail(t), "last_status": latest.get(t.id)}
            for t in templates
        ],
        "problems": relay.registry.problems(),
        # Provider folders that can't be loaded (a mistake in one): why, with its file and line
        "broken_kinds": dict(BROKEN),
        "suggestions": relay.registry.suggestions(),
    }


async def add_provider(request: Request, relay: Relay):
    return masked(relay.registry.add(without_about(await body(request))))


async def update_provider(request: Request, relay: Relay):
    provider_id = request.path_params["provider_id"]
    data = without_about(await body(request))
    current = relay.registry.get(provider_id)
    options = data.get("options")
    if isinstance(options, dict):
        # Masked secrets that weren't changed keep their stored value.
        data["options"] = {k: (current.options.get(k) if v == SECRET_MASK else v)
                           for k, v in options.items()}
    return masked(relay.registry.update(provider_id, data))


async def delete_provider(request: Request, relay: Relay):
    relay.registry.remove(request.path_params["provider_id"])
    # (its chats stay, without their provider: the log says where that came from)
    print(f"relay: provider {request.path_params['provider_id']} was removed, from Settings or the API", flush=True)
    return {"ok": True}


async def check_provider(request: Request, relay: Relay):
    provider = relay.registry.provider(request.path_params["provider_id"])
    try:
        message = await asyncio.to_thread(provider.check)
    except (ProviderError, OSError) as exc:
        return {"ok": False, "message": str(exc)}
    except Exception as exc:  # noqa: BLE001 - report anything the provider raised
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "message": message}


async def provider_models(request: Request, relay: Relay):
    """The provider's models, each with how it did last time (health)."""
    provider_id = request.path_params["provider_id"]
    provider = relay.registry.provider(provider_id)
    try:
        models = await asyncio.to_thread(provider.models)
    except Exception as exc:  # (a provider folder's mistake: said, not a server error)
        raise ApiError(f"{provider.template.name} couldn't list its models: {type(exc).__name__}: {exc}", 502) from exc
    known = health.statuses(relay.db, provider_id)
    ranked = ranking.ranking(relay.db)
    for model in models:
        model["status"] = known.get(f"{provider_id}/{model['id']}")
        model["rank"] = ranking.rank_of(ranked, provider_id, model["id"]) if model["id"] else None  # (AgentOS's)
    return {"models": models, "ranked": ranked["of"]}


async def model_ranking(request: Request, relay: Relay):
    """AgentOS's model ranking (relay.ranking): empty before the Governor's first."""
    return ranking.ranking(relay.db)


async def forget_ranking(request: Request, relay: Relay):
    """Start over: what's kept goes, and the next ranking looks every model up anew."""
    ranking.forget(relay.db)
    relay.agentos.event(user_id(request), "ranking.forgotten", actor="user")  # (the Governor sees why)
    return ranking.ranking(relay.db)


# A provider's page (relay.creations): one Relay doesn't know, added with an agent (/providers/new/<id>), or one it
# has, repaired (/providers/repair/<id>)

async def creations(request: Request, relay: Relay):
    """The user's new providers not added or discarded yet."""
    from relay.creations import GOING
    rows = relay.db.all(f"SELECT id FROM repairs WHERE user_id = ? AND creates = 1 AND page = 1 AND status IN "
                        f"({','.join('?' * len(GOING))}) ORDER BY created DESC", (user_id(request), *GOING))
    return [relay.creations.view(user_id(request), row["id"]) for row in rows]


async def new_creation(request: Request, relay: Relay):
    """Step 1: its record and home; its setup box comes with its shell."""
    creation = await asyncio.to_thread(relay.creations.begin, user_id(request))
    return relay.creations.view(user_id(request), creation["id"])


async def new_repair(request: Request, relay: Relay):
    """A repair of a provider, on its page: its record (the one going already, when there is one). {provider_id}"""
    data = await body(request)
    repair = await asyncio.to_thread(relay.creations.begin_repair, user_id(request), str(data.get("provider_id") or ""))
    return relay.creations.view(user_id(request), repair["id"])


async def provider_trouble(request: Request, relay: Relay):
    """What's wrong with a provider, as Relay sees it (a repair's first step), and its repair going on, if any."""
    from relay.creations import GOING
    template = relay.registry.get(request.path_params["provider_id"])
    going = relay.db.one(f"SELECT id FROM repairs WHERE user_id = ? AND creates = 0 AND page = 1 AND target_id = ? "
                         f"AND status IN ({','.join('?' * len(GOING))})", (user_id(request), template.id, *GOING))
    return {**await asyncio.to_thread(relay.repairs.trouble, template), "provider": masked(template),
            "repair": going["id"] if going else None}


async def get_creation(request: Request, relay: Relay):
    return relay.creations.view(user_id(request), request.path_params["creation_id"])


async def creation_action(request: Request, relay: Relay):
    """What its page does: start (what was installed or what's wrong, and whose agent works on it), approve,
    redraft, fix (something's wrong), check (again; anyway: try it even so), trial (up again), add (a new provider),
    apply (a repair), undo, discard."""
    user, creation_id, action = user_id(request), request.path_params["creation_id"], request.path_params["action"]
    data = await body(request)
    creations = relay.creations
    creation = creations.get(user, creation_id)
    going = creation["conversation_id"] and relay.runs.active_run_for(creation["conversation_id"])
    if action == "start":
        if creation.get("creates"):
            await begin_creation(relay, user, creation_id, str(data.get("about") or ""),
                                 str(data.get("provider_id") or ""), data.get("model") or None)
        else:
            await begin_page_repair(relay, user, creation_id, str(data.get("about") or ""),
                                    str(data.get("provider_id") or ""), data.get("model") or None)
    elif action in ("approve", "redraft", "fix"):
        if going:
            raise ApiError("Wait until the agent is done", 409)
        if action == "approve":
            prompt = await asyncio.to_thread(creations.approve, user, creation_id)
        elif action == "redraft":
            prompt = creations.redraft(user, creation_id, str(data.get("text") or ""))
        else:
            prompt = await asyncio.to_thread(creations.fix, user, creation_id, str(data.get("text") or ""))
        row = relay.db.one("SELECT * FROM conversations WHERE id = ?", (creation["conversation_id"],))
        if row is None:
            raise ApiError("Its agent's conversation is gone")
        await asyncio.to_thread(relay.runs.start, row, prompt)
    elif action == "check":
        if going:
            raise ApiError("Wait until the agent is done", 409)
        creations.check(user, creation_id, bool(data.get("anyway")))
    elif action == "trial":
        await asyncio.to_thread(creations.ensure_trial, user, creation_id)
    elif action in ("add", "apply"):
        await asyncio.to_thread(creations.add if action == "add" else creations.apply, user, creation_id)
        await relay.shells.end_for(f"creation:{creation_id}")  # (its setup box is gone)
        relay.agentos.resolve_repair(user, creation_id)  # (the Governor's Needs-you item: settled)
    elif action == "undo":
        await asyncio.to_thread(creations.undo, user, creation_id)
    elif action == "discard":
        if going:
            await asyncio.to_thread(relay.runs.stop_and_wait, going, 15, "the user cancelled it")
        await relay.shells.end_for(f"creation:{creation_id}")
        if creation.get("page"):
            await asyncio.to_thread(creations.discard, user, creation_id)
        else:  # (one made the old way)
            await asyncio.to_thread(relay.repairs.discard, user, creation_id)
        relay.agentos.resolve_repair(user, creation_id)
    else:
        raise ApiError("Unknown action", 404)
    return creations.view(user, creation_id)


async def begin_creation(relay: Relay, user: int, creation_id: str, about: str, helper_id: str,
                         model: str | None) -> dict[str, Any]:
    """Step 2: what the user installed, to the agent they chose (in Plan mode): it studies the program and hands
    in a plan. Its conversation."""
    helper = relay.registry.get(helper_id)
    if not has_tools(helper):
        raise ApiError(f"{helper.name} can't work on files: choose a provider with tools")
    creation = await asyncio.to_thread(relay.creations.prepare, user, creation_id, about)
    row = await open_conversation(relay, user, {
        "provider_id": helper.id, "model": model, "mode": "code", "isolate": False, "agent_mode": "plan",
        "directory": creation["folder"], "title": "New provider"}, anywhere=True)
    relay.repairs.attach(creation["id"], row["id"])
    prompt = ("The user installed a program for Relay to use as a provider, and wants your plan for it first. "
              "Everything you need is in CREATE.md in this folder: read it, then the guide it names. Study the "
              "program with repair_run, hand in your plan with provider_plan, and stop there.")
    await asyncio.to_thread(relay.runs.start, row, prompt)
    return row


async def begin_page_repair(relay: Relay, user: int, repair_id: str, about: str, helper_id: str, model: str | None,
                            managed: bool = False) -> dict[str, Any]:
    """A repair's step 2: what's wrong, to the agent chosen (in Plan mode): it looks into it and hands in a plan.
    Its conversation; AgentOS's Governor's is `managed`, for it to follow."""
    repair = relay.creations.get(user, repair_id)
    target = relay.registry.get(repair["target_id"])
    helper = relay.registry.get(helper_id)
    if helper.id == target.id:
        raise ApiError("Choose another provider to do the repair")
    if not has_tools(helper):
        raise ApiError(f"{helper.name} can't work on files: choose a provider with tools")
    repair = await asyncio.to_thread(relay.creations.prepare, user, repair_id, about)
    row = await open_conversation(relay, user, {
        "provider_id": helper.id, "model": model, "mode": "code", "isolate": False, "agent_mode": "plan",
        "directory": repair["folder"], "title": f"Repair: {target.name}"}, anywhere=True)
    if managed:
        relay.db.execute("UPDATE conversations SET managed = 1 WHERE id = ?", (row["id"],))
    relay.repairs.attach(repair["id"], row["id"])
    prompt = (f"Relay's support layer for {target.name} seems broken, and the user wants your plan to repair it "
              "first. Everything you need is in REPAIR.md in this folder: read it. Look into it with repair_run, hand "
              "in your plan with provider_plan, and stop there.")
    await asyncio.to_thread(relay.runs.start, row, prompt)
    return row


async def get_repair(request: Request, relay: Relay):
    return relay.repairs.get(user_id(request), request.path_params["repair_id"])


async def model_statuses(request: Request, relay: Relay):
    return health.statuses(relay.db)


async def test_model(request: Request, relay: Relay):
    """Send a model a tiny prompt to see whether it works now: where a run would be (its box, with its login; the
    WorkBox, for its commands), as the full check does."""
    provider_id = request.path_params["provider_id"]
    provider = relay.registry.provider(provider_id)
    model = str((await body(request)).get("model") or "")
    discard = relay.containers.discard if relay.containers is not None else None
    result = await asyncio.to_thread(health.test, provider, model, relay.settings.chat_directory() / ".tests",
                                     relay.runs.place_check, discard)
    health.record(relay.db, provider_id, model, result["ok"], result["message"], result["seconds"], "test")
    return health.statuses(relay.db, provider_id)[f"{provider_id}/{model}"]


async def start_draft(request: Request, relay: Relay):
    """Add provider's next step, for a template as its page has it: said here when it can't be one, and what it names
    that isn't there yet made. What that step has for it: {shell, sign_in, shell_hint}. It isn't added until it asks."""
    template = draft_template(request.path_params["kind"], (await body(request)).get("template"))
    if any(known.id == template.id for known in relay.registry.templates()):
        raise TemplateError(f"A provider with id {template.id!r} already exists: change its id")
    provider = KINDS[template.kind](template)
    await asyncio.to_thread(provider.prepare_new)
    return {"shell": provider.has_shell(), "sign_in": provider.SIGN_IN, "shell_hint": provider.SHELL_HINT}


async def test_draft(request: Request, relay: Relay):
    """A provider not added yet (Add provider's next step): a tiny prompt, as test_model sends it, to see whether it
    works. Nothing of it stays: no health is noted, and its box goes when nothing has a use for it."""
    from relay.repair import LOGIN_LOST
    data = await body(request)
    template = draft_template(request.path_params["kind"], data.get("template"))
    provider = KINDS[template.kind](template)
    discard = relay.containers.discard if relay.containers is not None else None
    result = await asyncio.to_thread(health.test, provider, str(data.get("model") or ""),
                                     relay.settings.chat_directory() / ".tests", relay.runs.place_check, discard)
    await asyncio.to_thread(relay.creations.drop_unused_box, provider)
    return {**result, "login_lost": not result["ok"] and bool(LOGIN_LOST.search(result["message"]))}


async def check_conformance(request: Request, relay: Relay):
    """Check it fully (relay.providers.conformance): the provider run as Relay runs it, in a scratch folder, through
    a few short prompts to its model. [{name, ok, detail}]; it takes a while (each turn is a real answer)."""
    from relay.providers import conformance
    provider = relay.registry.provider(request.path_params["provider_id"])
    model = str((await body(request)).get("model") or "")
    discard = relay.containers.discard if relay.containers is not None else None
    results = await asyncio.to_thread(conformance.check, provider, model, relay.settings.chat_directory() / ".tests",
                                      relay.runs.place_check, discard)
    return {"results": results, "ok": all(r["ok"] is not False for r in results)}


async def provider_efforts(request: Request, relay: Relay):
    """The efforts a provider's model offers (for a new chat)."""
    provider = relay.registry.provider(request.path_params["provider_id"])
    return await asyncio.to_thread(provider.efforts, request.query_params.get("model") or None)
