"""AgentOS's model ranking: the models available here, from the smartest (#1) down.

AgentOS's Governor finds the scores (its weekly system task, `models-rank`): each model's place on
one intelligence index (the Artificial Analysis Intelligence Index), from benchmarks it searches.
Relay keeps what it found, so a model is looked up once:
    model_scores   a model as the benchmark names it: its score (NULL: no benchmark knows it), whether
                   it was estimated from other benchmarks, where it came from; or a router (Copilot's
                   auto: another model per request), which has no score and isn't ranked
    model_names    which of those a provider's model is ("opus" of Claude Code: Claude Opus 5.5)
A model id mapped under one provider counts under another with the same id (two opencode profiles).

The ranking is Relay's: the models available at the Governor's last look (LISTED, in meta), by
score; one model reached through two providers has one rank. It's shown where models are chosen
(the model picker, fallback loops) and told to agents choosing subagents. Without AgentOS, or
before its first ranking, there's none, and nothing else changes.
"""

from __future__ import annotations

import json
import math
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from relay.db import Database
    from relay.providers import Registry

LISTED = "model_ranking"  # meta: the models available at the Governor's last look, and when
FORGOTTEN = "model_ranking_forgotten"  # meta: when the user started the ranking over (Start over)
INDEX = "Artificial Analysis Intelligence Index"
NAME_LENGTH = 120
RECHECK = 30 * 86400  # seconds before a model no benchmark knew is looked up again (benchmarks add models)


class RankingError(Exception):
    pass


def available(registry: Registry, before: list[tuple[str, str]] | None = None) -> list[tuple[str, str]]:
    """(provider id, model id) of every enabled provider's models (not its default: that's one of them).
    A provider that can't list them now (its program starting, say) keeps those it had `before`."""
    found = []
    for template in registry.templates():
        if not template.enabled:
            continue
        try:
            models = [(template.id, m["id"]) for m in registry.provider(template.id).models() if m.get("id")]
        except Exception:  # noqa: BLE001 - one that can't list its models now: as it was
            models = []
        found += models or [m for m in before or [] if m[0] == template.id]
    return list(dict.fromkeys(found))


def listed(db: Database) -> tuple[float | None, list[tuple[str, str]]]:
    row = db.one("SELECT value FROM meta WHERE key = ?", (LISTED,))
    if row is None:
        return None, []
    data = json.loads(row["value"])
    return data.get("at"), [tuple(entry) for entry in data.get("models") or []]


def note_listed(db: Database, models: list[tuple[str, str]], now: float | None = None) -> None:
    value = json.dumps({"at": time.time() if now is None else now, "models": [list(m) for m in models]})
    db.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
               (LISTED, value))


def names(db: Database, models: list[tuple[str, str]]) -> dict[tuple[str, str], dict[str, Any]]:
    """Each model's benchmark entry ({name, score, estimated, …}), where one is known: its own, or that
    of the same model id under another provider."""
    scores = {r["name"]: r for r in db.all("SELECT * FROM model_scores")}
    own = {(r["provider_id"], r["model"]): r["name"] for r in db.all("SELECT * FROM model_names")}
    by_id: dict[str, str] = {}
    for (_, model), name in sorted(own.items()):
        by_id.setdefault(model, name)
    found = {}
    for provider_id, model in models:
        name = own.get((provider_id, model)) or by_id.get(model)
        if name in scores:
            found[(provider_id, model)] = scores[name]
    return found


def unscored(db: Database, models: list[tuple[str, str]], now: float | None = None) -> list[tuple[str, str]]:
    """What the Governor is to look up: models no benchmark entry is known for yet, and those no benchmark
    knew a while ago."""
    now = time.time() if now is None else now
    known = names(db, models)
    return [m for m in models if m not in known or (known[m]["score"] is None and not known[m].get("router")
                                                    and now - known[m]["noted"] > RECHECK)]


def ranking(db: Database) -> dict[str, Any]:
    """The ranking of the models available at the last look: {updated, of, benchmark, models (the ranked ones,
    best first: rank, name, score, …, and the provider models that are it), ranks ({provider: {model: rank}}),
    unknown (names no benchmark knows), routers (names of routers: a model per request) and routing ({provider:
    [its models that are]}), unscored (models not looked up yet)}."""
    at, models = listed(db)
    known = names(db, models)
    entries: dict[str, dict[str, Any]] = {}
    unknown, routers, routing = set(), set(), {}
    for (provider_id, model), row in known.items():
        if row.get("router"):
            routers.add(row["name"])
            routing.setdefault(provider_id, []).append(model)
            continue
        if row["score"] is None:
            unknown.add(row["name"])
            continue
        entry = entries.setdefault(row["name"], {k: row.get(k) for k in ("name", "score", "benchmark", "source",
                                                                         "noted", "note")}
                                   | {"estimated": bool(row["estimated"]), "models": []})
        entry["models"].append({"provider_id": provider_id, "model": model})
    ordered = sorted(entries.values(), key=lambda e: (-e["score"], e["name"].lower()))
    ranks: dict[str, dict[str, int]] = {}
    for entry in ordered:  # ties share a rank: 1, 2, 2, 4
        entry["rank"] = 1 + sum(1 for other in ordered if other["score"] > entry["score"])
        for m in entry["models"]:
            ranks.setdefault(m["provider_id"], {})[m["model"]] = entry["rank"]
    benchmarks = [e["benchmark"] for e in ordered if e.get("benchmark")]
    return {"updated": max([at or 0, *(e["noted"] for e in ordered)]) or None, "of": len(ordered),
            "benchmark": max(set(benchmarks), key=benchmarks.count) if benchmarks else None,
            "models": ordered, "ranks": ranks, "unknown": sorted(unknown), "routers": sorted(routers),
            "routing": routing,
            "unscored": [{"provider_id": p, "model": m} for p, m in unscored(db, models)]}


def rank_of(found: dict[str, Any], provider_id: str, model: str | None) -> int | None:
    return found["ranks"].get(provider_id, {}).get(model or "")


def save(db: Database, registry: Registry, entries: list[dict[str, Any]], benchmark: str | None = None,
         now: float | None = None) -> list[str]:
    """The Governor's findings: per provider model, which model it is (name), and that name's score on the
    index (left out: one kept already, or none found). What was saved, a line each."""
    now = time.time() if now is None else now
    providers = {t.id for t in registry.templates()}
    done = []
    with db.transaction():
        for entry in entries:
            if not isinstance(entry, dict):
                raise RankingError("Each score is an object: provider, model, name, score")
            provider_id = str(entry.get("provider") or entry.get("provider_id") or "").strip()
            model = str(entry.get("model") or "").strip()
            name = " ".join(str(entry.get("name") or "").split())[:NAME_LENGTH]
            if provider_id not in providers:
                raise RankingError(f"No provider {provider_id!r} (models.list shows their ids)")
            if not model:
                raise RankingError(f"{provider_id}: which model? (its id, as models.list shows it)")
            if not name:
                raise RankingError(f"{provider_id} {model}: which model is it? (name: as the benchmark names it)")
            router = str(entry.get("router", "false")).lower() in ("true", "yes", "1")
            score = None if router else _number(entry.get("score"), f"{provider_id} {model}")
            kept = db.one("SELECT * FROM model_scores WHERE name = ?", (name,))
            note = " ".join(str(entry.get("note") or "").split())[:300] or None
            if score is not None or router or kept is None:  # (a name without a score, new: no benchmark knows it)
                estimated = str(entry.get("estimated", "false")).lower() in ("true", "yes", "1")
                db.execute(
                    "INSERT INTO model_scores (name, score, estimated, note, benchmark, source, noted, router) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (name) DO UPDATE SET score = excluded.score, "
                    "estimated = excluded.estimated, note = excluded.note, benchmark = excluded.benchmark, "
                    "source = excluded.source, noted = excluded.noted, router = excluded.router",
                    (name, score, 1 if estimated and score is not None else 0, note,
                     (str(entry.get("benchmark") or benchmark or INDEX)[:NAME_LENGTH]) if score is not None else None,
                     str(entry.get("source") or "")[:500] or None, now, 1 if router else 0))
            elif note:
                db.execute("UPDATE model_scores SET note = ? WHERE name = ?", (note, name))
            db.execute("INSERT INTO model_names (provider_id, model, name, noted) VALUES (?, ?, ?, ?) "
                       "ON CONFLICT (provider_id, model) DO UPDATE SET name = excluded.name, noted = excluded.noted",
                       (provider_id, model, name, now))
            stored = db.one("SELECT score, router FROM model_scores WHERE name = ?", (name,))
            done.append(f"{provider_id} {model}: {name}, "
                        + ("a router (a model per request), not ranked" if stored["router"]
                           else f"{stored['score']:g}" if stored["score"] is not None else "no benchmark knows it"))
    return done


def forget(db: Database) -> None:
    """Everything kept, for a ranking from the start (the user's Start over): every model is looked up anew."""
    with db.transaction():
        db.execute("DELETE FROM model_names")
        db.execute("DELETE FROM model_scores")
        db.execute("DELETE FROM meta WHERE key = ?", (LISTED,))
        db.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                   (FORGOTTEN, str(time.time())))


def _number(value: Any, what: str) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise RankingError(f"{what}: the score is a number (or left out)") from None
    if not math.isfinite(number) or number < 0:
        raise RankingError(f"{what}: the score is a number of 0 or more")
    return number


def describe(db: Database, registry: Registry, models: list[tuple[str, str]]) -> str:
    """What the Governor sees with models.list: each provider's models with what's known of them, then
    what's new."""
    found = ranking(db) if models == listed(db)[1] else None
    known = names(db, models)
    ranks = found["ranks"] if found else {}
    of = found["of"] if found else 0
    by_provider: dict[str, list[str]] = {}
    for provider_id, model in models:
        row = known.get((provider_id, model))
        if row is None:
            text = f"{model}: not looked up yet"
        elif row.get("router"):
            text = f"{model}: {row['name']}, a router (a model per request), not ranked"
        elif row["score"] is None:
            text = f"{model}: {row['name']}, no benchmark knew it" + (
                "; look again" if time.time() - row["noted"] > RECHECK else "")
        else:
            rank = ranks.get(provider_id, {}).get(model)
            text = (f"{model}: {row['name']}, {row['score']:g}{' (estimated)' if row['estimated'] else ''}"
                    + (f", #{rank} of {of}" if rank else "") + (f" [{row['note']}]" if row.get("note") else ""))
        by_provider.setdefault(provider_id, []).append(text)
    names_of = {t.id: t.name for t in registry.templates()}
    lines = [(f"The models available here, with the score Relay keeps for each ({INDEX}, unless it says otherwise; "
              "#1 is the smartest):")]
    forgotten = db.one("SELECT value FROM meta WHERE key = ?", (FORGOTTEN,))
    if forgotten and not known:
        when = time.strftime("%a %d %b %H:%M", time.localtime(float(forgotten["value"])))
        lines.insert(0, f"The user started the ranking over on {when} (Start over): the scores kept before were "
                        "cleared on purpose, so look every model up anew, with the rules of the task.")
    for provider_id, texts in by_provider.items():
        lines.append(f"- {provider_id} ({names_of.get(provider_id, provider_id)}):")
        lines += [f"  {text}" for text in texts]
    new = unscored(db, models)
    lines.append("")
    lines.append(f"Not looked up yet ({len(new)}): " + ", ".join(f"{p} {m}" for p, m in new) if new
                 else "Nothing new: every model has been looked up.")
    lines.append("A router (Copilot's auto and the like: another model per request) has no score of its own: save it "
                 "with router true, a name of its own and no score; it isn't ranked.")
    return "\n".join(lines)
