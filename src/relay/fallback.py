"""Fallback loops: who goes on when a conversation's model stops working.

A loop is a named, ordered list of agents ("provider" with its default model, or
"provider/model"), e.g. Opus (Claude Code) → GPT-5.6 Sol (opencode) → GPT-6 Astra
(Copilot). A user keeps any number (Settings → Usage). A conversation follows the
first loop its model is in, unless it was given another one, or none (`/loop`).

When an answer stops on a usage limit, or fails without doing anything at all, the
next agent of the loop after the one that stopped takes over right away (round the
loop, skipping the ones tried this round, turned off, or limited themselves). When
none is left, the round is over: Relay waits until the first known reset of the
loop's agents (or `limits.unknown_wait` hours when none says), then starts the loop
again from its first agent, for at most `limits.loop_rounds` rounds (relay.runs).
A conversation whose model is in no loop waits for its own limit to reset.

A model id may contain slashes itself (opencode's "openai/gpt-5.6-sol").
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from relay import health
from relay.db import Database

KEY = "fallback_loops"
OLD_KEY = "fallback_agents"  # one list for every model, before loops
AUTO, OFF = "auto", "off"  # a conversation's choice, besides a loop's id


class FallbackError(Exception):
    pass


def parse(text: str) -> list[dict[str, str]]:
    """"claude-code/opus, opencode/openai/gpt-5.6-sol" (or → between them) as entries."""
    entries = []
    for part in text.replace("\n", ",").replace("→", ",").replace("->", ",").split(","):
        part = part.strip()
        if part:
            provider, _, model = part.partition("/")
            entries.append({"provider": provider.strip(), "model": model.strip()})
    return entries


def key(entry: dict[str, str]) -> str:
    return f"{entry['provider']}/{entry.get('model') or ''}"


def load(db: Database, user_id: int) -> list[dict[str, Any]]:
    row = db.one("SELECT value FROM preferences WHERE user_id = ? AND key = ?", (user_id, KEY))
    if row is None:
        # The list from before loops is the first loop.
        old = db.one("SELECT value FROM preferences WHERE user_id = ? AND key = ?", (user_id, OLD_KEY))
        entries = [e for e in (json.loads(old["value"]) if old else []) if isinstance(e, dict) and e.get("provider")]
        return [{"id": "fallback", "name": "Fallback agents", "entries": entries}] if entries else []
    found = json.loads(row["value"])
    return [loop for loop in found if isinstance(loop, dict) and loop.get("id")
            and isinstance(loop.get("entries"), list)]


def save(db: Database, registry, user_id: int, loops: Any) -> list[dict[str, Any]]:
    if not isinstance(loops, list):
        raise FallbackError("A list of loops: {name, entries: [{provider, model}]}")
    clean, names = [], set()
    for loop in loops:
        if not isinstance(loop, dict):
            raise FallbackError("A loop is {name, entries}")
        name = " ".join(str(loop.get("name") or "").split())[:60]
        if not name:
            raise FallbackError("Each loop needs a name")
        if name.lower() in names:
            raise FallbackError(f"There's a loop called {name!r} already")
        names.add(name.lower())
        entries = loop.get("entries")
        if isinstance(entries, str):
            entries = parse(entries)
        if not isinstance(entries, list):
            raise FallbackError(f"Loop {name!r}: a list of provider/model entries")
        items = []
        for entry in entries:
            if not isinstance(entry, dict) or not str(entry.get("provider") or "").strip():
                raise FallbackError(f"Loop {name!r}: each entry needs a provider")
            provider = str(entry["provider"]).strip()
            try:
                registry.get(provider)
            except Exception as exc:  # (TemplateError: say which one)
                raise FallbackError(f"No provider {provider!r}") from exc
            item = {"provider": provider, "model": str(entry.get("model") or "").strip()}
            if item not in items:
                items.append(item)
        loop_id = str(loop.get("id") or "").strip() or uuid.uuid4().hex[:8]
        clean.append({"id": loop_id, "name": name, "entries": items})
    db.execute("INSERT INTO preferences (user_id, key, value) VALUES (?, ?, ?) "
               "ON CONFLICT (user_id, key) DO UPDATE SET value = excluded.value", (user_id, KEY, json.dumps(clean)))
    return clean


def find(loops: list[dict[str, Any]], loop_id: str | None) -> dict[str, Any] | None:
    return next((loop for loop in loops if loop["id"] == loop_id), None)


def for_model(loops: list[dict[str, Any]], provider: str, model: str | None) -> dict[str, Any] | None:
    """The first loop with this agent in it."""
    wanted = key({"provider": provider, "model": model or ""})
    return next((loop for loop in loops if any(key(e) == wanted for e in loop["entries"])), None)


def loop_for(db: Database, user_id: int, choice: str | None, provider: str, model: str | None,
             going: str | None = None) -> dict[str, Any] | None:
    """The loop a conversation follows: the one it was given (`choice`), none (OFF), or the
    first its model is in; one it's going round already (`going`) stays, as its model changes."""
    if choice == OFF:
        return None
    loops = load(db, user_id)
    if choice and choice != AUTO:
        return find(loops, choice)
    return find(loops, going) or for_model(loops, provider, model)


def next_in(db: Database, registry, loop: dict[str, Any], stopped: dict[str, str], tried: list[str],
            needs_tools: bool, confined: bool = False) -> dict[str, str] | None:
    """The loop's next agent after the one that stopped (round the loop) that can go on now."""
    entries = loop["entries"]
    at = next((i for i, e in enumerate(entries) if key(e) == key(stopped)), -1)
    order = entries[at + 1:] + entries[:at + 1] if at >= 0 else entries
    for entry in order:
        if key(entry) == key(stopped) or key(entry) in tried or not usable(db, registry, entry, needs_tools, confined):
            continue
        return entry
    return None


def usable(db: Database, registry, entry: dict[str, str], needs_tools: bool, confined: bool = False) -> bool:
    """Turned on, with tools where they're needed, and not limited itself now. Confined: only a provider whose
    program runs in a box (Provider.CONFINED), for AgentOS's conversations."""
    try:
        provider = registry.provider(entry["provider"])
    except Exception:  # noqa: BLE001 - removed meanwhile
        return False
    if not provider.template.enabled or (needs_tools and not provider.has_tools()) \
            or (confined and not provider.CONFINED):
        return False
    status = health.statuses(db).get(key(entry))
    return not (status and not status["ok"] and (status.get("resets_at") or 0) > time.time())


def confined(registry, entry: dict[str, str]) -> bool:
    """Its provider's program runs in a box (Provider.CONFINED)."""
    try:
        return registry.provider(entry["provider"]).CONFINED
    except Exception:  # noqa: BLE001 - removed meanwhile
        return False


def first_reset(db: Database, loop: dict[str, Any], also: float | None = None) -> float | None:
    """The first time one of the loop's agents is known to be able to go on again."""
    statuses = health.statuses(db)
    now = time.time()
    times = [s.get("resets_at") for e in loop["entries"] if (s := statuses.get(key(e)))]
    times = [t for t in [*times, also] if t and t > now]
    return min(times) if times else None
