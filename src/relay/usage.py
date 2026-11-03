"""The usage overview: what runs cost, pay-per-use and plan use apart."""

from __future__ import annotations

import json
import time
from typing import Any

from relay.db import Database

KINDS = ("api", "subscription", "free", "unknown")


def tokens(usage: dict[str, Any] | None) -> tuple[int, int]:
    """(input, output) tokens from the different shapes providers report."""
    found = (usage or {}).get("tokens") or {}
    if not isinstance(found, dict):
        return 0, 0

    def first(*keys: str) -> int:
        for key in keys:
            if isinstance(found.get(key), (int, float)):
                return int(found[key])
        return 0

    # Tokens read from or written to a prompt cache are input too; Claude and opencode count
    # them apart (OpenAI's prompt_tokens includes them already).
    cache = found.get("cache") if isinstance(found.get("cache"), dict) else {}
    cached = sum(int(v) for v in (found.get("cache_read_input_tokens"), found.get("cache_creation_input_tokens"),
                                  cache.get("read"), cache.get("write")) if isinstance(v, (int, float)))
    return (first("input_tokens", "input", "prompt_tokens") + cached,
            first("output_tokens", "output", "completion_tokens"))


def overview(db: Database, user_id: int, days: int) -> dict[str, Any]:
    since = time.time() - days * 86400
    rows = db.all(
        "SELECT r.provider_id, r.model, r.usage, r.finished FROM runs r "
        "JOIN conversations c ON c.id = r.conversation_id "
        "WHERE c.user_id = ? AND r.finished >= ? ORDER BY r.finished", (user_id, since))
    totals = {kind: {"runs": 0, "cost": 0.0, "input": 0, "output": 0} for kind in KINDS}
    groups: dict[tuple, dict[str, Any]] = {}
    per_day: dict[str, dict[str, float]] = {}
    for row in rows:
        usage = json.loads(row["usage"]) if row["usage"] else None
        kind = (usage or {}).get("billing") or "unknown"
        kind = kind if kind in KINDS else "unknown"
        cost = float((usage or {}).get("cost") or 0)
        tokens_in, tokens_out = tokens(usage)
        for bucket in (totals[kind], groups.setdefault(
                (row["provider_id"] or "?", row["model"] or "", kind),
                {"provider_id": row["provider_id"], "model": row["model"], "billing": kind,
                 "runs": 0, "cost": 0.0, "input": 0, "output": 0})):
            bucket["runs"] += 1
            bucket["cost"] += cost
            bucket["input"] += tokens_in
            bucket["output"] += tokens_out
        day = time.strftime("%Y-%m-%d", time.localtime(row["finished"]))
        slot = per_day.setdefault(day, {"api": 0.0, "runs": 0})
        slot["runs"] += 1
        if kind == "api":
            slot["api"] += cost
    return {
        "days": days,
        "totals": totals,
        "groups": sorted(groups.values(), key=lambda g: (-g["cost"], -g["runs"])),
        "per_day": [{"day": day, **values} for day, values in sorted(per_day.items())],
    }
