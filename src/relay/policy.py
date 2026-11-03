"""Which subagents an agent may start without asking you.

Any agent can hand work to any provider and model (relay.delegation), and
some of those cost money: an API billed per token, or a big model that eats
a plan's limits. So subagents are allowed without asking only when they're

- on your list (Settings → Subagents): a provider with one model, or any of its models;
- free: a local model, or one that ran at no cost before (if you allow that);
- the calling agent's own provider and model (if you allow that).

Subagents on local models (a server on your network or machine, e.g. Ollama) can be
turned off altogether ("local": false), for when that server isn't running.

Any other is asked for (the answer can also be "use this allowed one
instead"); without an answer in time (ask_timeout()) the allowed one is used,
or else it's refused, so an agent doesn't wait long. Or it's refused right
away, if you set it so. The agent hears what happened and which subagents it
may use.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from opencode_manager.bridge.billing import FREE
from relay import tunables
from relay.db import Database
from relay.providers import TemplateError, safely

if TYPE_CHECKING:
    from relay.providers import Registry

KEY = "subagents"
DEFAULT = {"allowed": [], "free": True, "same": True, "others": "ask", "local": True}


def ask_timeout() -> float:
    """Seconds a question about a subagent waits for the user."""
    return tunables.get("subagents.ask_timeout")


class PolicyError(Exception):
    pass


def load(db: Database, user_id: int) -> dict[str, Any]:
    row = db.one("SELECT value FROM preferences WHERE user_id = ? AND key = ?", (user_id, KEY))
    stored = json.loads(row["value"]) if row else {}
    return json.loads(json.dumps({**DEFAULT, **(stored if isinstance(stored, dict) else {})}))  # a copy


def save(db: Database, user_id: int, data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise PolicyError("Expected an object")
    allowed = []
    if not isinstance(data.get("allowed") or [], list):
        raise PolicyError("allowed is a list of subagents: each a provider and a model")
    for entry in data.get("allowed") or []:
        if not isinstance(entry, dict) or not isinstance(entry.get("provider"), str) or not entry["provider"]:
            raise PolicyError("Each allowed subagent needs a provider")
        model = entry.get("model") or ""
        if not isinstance(model, str):
            raise PolicyError("A model is text; * means any model")
        if {"provider": entry["provider"], "model": model} not in allowed:
            allowed.append({"provider": entry["provider"], "model": model})
    others = data.get("others", "ask")
    if others not in ("ask", "refuse"):
        raise PolicyError("others is 'ask' or 'refuse'")
    policy = {"allowed": allowed, "free": bool(data.get("free", True)), "same": bool(data.get("same", True)),
              "others": others, "local": bool(data.get("local", True))}
    db.execute("INSERT INTO preferences (user_id, key, value) VALUES (?, ?, ?) "
               "ON CONFLICT (user_id, key) DO UPDATE SET value = excluded.value",
               (user_id, KEY, json.dumps(policy)))
    return policy


def allow_always(db: Database, user_id: int, provider_id: str, model: str | None) -> None:
    policy = load(db, user_id)
    policy["allowed"].append({"provider": provider_id, "model": model or ""})
    save(db, user_id, policy)


def billing(db: Database, registry: Registry, provider_id: str, model: str | None) -> str | None:
    """Who pays for a model: what its last runs said, or else what the provider can tell."""
    rows = db.all("SELECT usage FROM runs WHERE provider_id = ? AND COALESCE(model, '') = ? "
                  "AND usage IS NOT NULL ORDER BY finished DESC LIMIT 20", (provider_id, model or ""))
    for row in rows:
        found = (json.loads(row["usage"]) or {}).get("billing")
        if found:
            return found
    try:
        return safely(registry.provider(provider_id).billing, None, model)
    except TemplateError:
        return None


def verdict(policy: dict[str, Any], caller: dict[str, Any], provider_id: str, model: str | None,
            paid_by: str | None) -> str:
    """"allow", "ask" or "refuse" for a subagent on this provider and model."""
    model = model or ""
    for entry in policy["allowed"]:
        if entry["provider"] == provider_id and entry["model"] in ("*", model):
            return "allow"
    if policy["same"] and provider_id == caller["provider_id"] and model == (caller["model"] or ""):
        return "allow"
    if policy["free"] and paid_by == FREE:
        return "allow"
    return policy["others"]


def allowed(db: Database, registry: Registry, policy: dict[str, Any],
            caller: dict[str, Any]) -> list[tuple[str, str, str]]:
    """The subagents allowed without asking that are known: (provider, model, why); model "*" is any."""
    found: list[tuple[str, str, str]] = []
    enabled = {t.id for t in registry.templates() if t.enabled}

    def add(provider_id: str, model: str, why: str) -> None:
        if provider_id in enabled and not any(f[0] == provider_id and f[1] in ("*", model) for f in found) \
                and (policy["local"] or model == "*" or not local_server(registry, provider_id, model)):
            found.append((provider_id, model, why))

    for entry in policy["allowed"]:
        add(entry["provider"], entry["model"], "on the user's list")
    if policy["same"]:
        add(caller["provider_id"], caller["model"] or "", "the same as yours")
    if policy["free"]:
        for template in registry.templates():
            try:
                free = safely(registry.provider(template.id).free_models, []) if template.enabled else []
            except TemplateError:
                continue
            for model in free:
                add(template.id, model, "free")
        rows = db.all("SELECT provider_id, COALESCE(model, '') AS model, usage FROM runs "
                      "WHERE usage IS NOT NULL ORDER BY finished DESC LIMIT 500")
        seen = set()
        for row in rows:  # the latest run of each model says how it's paid for now
            if (row["provider_id"], row["model"]) not in seen:
                seen.add((row["provider_id"], row["model"]))
                if (json.loads(row["usage"]) or {}).get("billing") == FREE:
                    add(row["provider_id"], row["model"], "free")
    return found


def local_server(registry: Registry, provider_id: str, model: str | None) -> str | None:
    """The local server (e.g. Ollama) a model runs on, if it's on one."""
    try:
        return registry.provider(provider_id).local_server(model or None)
    except Exception:  # noqa: BLE001 - removed meanwhile, or its service is down: not known as local
        return None


def describe(entries: list[tuple[str, str, str]]) -> str:
    """For the agent: how to start each of them."""
    if not entries:
        return "none"
    parts = []
    for provider_id, model, why in entries:
        if model == "*":
            where = f"provider {provider_id}, any model"
        elif model:
            where = f"provider {provider_id}, model {model}"
        else:
            where = f"provider {provider_id}, its default model"
        parts.append(f"{where} ({why})")
    return "; ".join(parts)


def alternative(entries: list[tuple[str, str, str]], provider_id: str) -> tuple[str, str] | None:
    """An allowed subagent to offer instead: of the same provider if there is one, else
    another than the calling agent's own; "any model" offers the provider's default."""
    choices = [(p, "" if m == "*" else m, why) for p, m, why in entries]
    ranked = ([c for c in choices if c[0] == provider_id] + [c for c in choices if c[2] != "the same as yours"]
              + choices)
    return (ranked[0][0], ranked[0][1]) if ranked else None
