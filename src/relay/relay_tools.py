"""Relay's own tools, per conversation: which of them its agents get (the user's choice, e.g. with Relay's SDK).

Kinds: web (search and fetch), memory, subagents (delegate, wait, tell, agents) and computers (the user's own).
All are on unless turned off; what's off is kept with the conversation (conversations.tools: {kind: false}),
and Relay leaves it out where its tools are made (relay.runs: the run's context; relay.delegation: the MCP
tools, which also refuses a call to one; the tools note; a chat model's toolbox). The agent's program keeps
its own tools: Relay complements providers, it never takes theirs.
"""

from __future__ import annotations

import json
from typing import Any

KINDS = ("web", "memory", "subagents", "computers")


class ToolsError(ValueError):
    pass


def off(row: dict[str, Any] | None) -> set[str]:
    """The kinds a conversation's agents don't get. A temporary chat never has Relay's memory: it uses none
    and adds none."""
    kept = {"memory"} if (row or {}).get("temporary") else set()
    try:
        found = json.loads((row or {}).get("tools") or "{}")
    except ValueError:
        return kept
    return kept | ({kind for kind, value in found.items() if kind in KINDS and value is False}
                   if isinstance(found, dict) else set())


def public(row: dict[str, Any]) -> dict[str, bool]:
    """{kind: on} for every kind."""
    gone = off(row)
    return {kind: kind not in gone for kind in KINDS}


def changed(row: dict[str, Any] | None, wanted: Any) -> str | None:
    """What's kept after {kind: on} (those not named stay as they were): the JSON of those off, None for all on."""
    if not isinstance(wanted, dict) or any(not isinstance(v, bool) for v in wanted.values()):
        raise ToolsError(f"tools: {{kind: true or false}}, kinds: {', '.join(KINDS)}")
    unknown = sorted(set(wanted) - set(KINDS))
    if unknown:
        raise ToolsError(f"No such kind of Relay's tools: {', '.join(unknown)} (there are {', '.join(KINDS)})")
    gone = (off(row) | {k for k, on in wanted.items() if not on}) - {k for k, on in wanted.items() if on}
    return json.dumps(dict.fromkeys(sorted(gone), False)) if gone else None
