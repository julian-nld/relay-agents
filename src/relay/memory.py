"""Relay's memory: what every agent should know next time, in every chat (Settings → Memory).

The providers keep memories of their own: Claude Code one per folder, so one per chat in Relay, and
what it learns in one chat the next doesn't know. Relay's is one place for all of them, beside
theirs: what's lasting about the user and their wishes (general), and per project what's worth
knowing there. Agents get it with Relay's note (relay.runs: the tools note; a chat model in its
system prompt) and add to it with Relay's tools (`relay memory add`, the MCP tool memory_add, the
function remember); the user sees and changes all of it in Settings → Memory, a memory bank as
Perplexity's: each memory filed in a category (CATEGORIES), to search and filter.

The Governor (AgentOS) has a memory of its own (its place GOVERNOR): what it learns as the Governor,
which it gets with every briefing and no other agent gets; the general one it gets too.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from relay.db import Database

KEY = "memory"  # preferences: {"enabled": false}: agents get none and can't add (what's kept stays);
# {"agents_add": false}: they get it, but only the user adds, changes and forgets
GOVERNOR = "governor"  # the place of the Governor's own memory (a memory's project)
# What a memory is about, as Perplexity files them (Interests, Tools, Work, Preferences, Location, …), and two
# for agents' work: (id, label, what goes there)
CATEGORIES = (
    ("preferences", "Preferences", "how they like answers, code and work done"),
    ("about", "About you", "who they are: name, language, background"),
    ("work", "Work", "their job, team, and how they work"),
    ("tools", "Tools", "languages, frameworks, editors and services they use"),
    ("interests", "Interests", "topics they follow"),
    ("location", "Location", "where they are, their time zone"),
    ("setup", "Setup", "how a project, service or machine is set up: commands, ports, paths"),
    ("decisions", "Decisions", "what was decided, and why, so it isn't asked again"),
    ("other", "Other", "the rest"),
)
KINDS = [c[0] for c in CATEGORIES]
LONGEST = 600  # characters of one memory: a fact or a wish, not a document
MOST = 300  # memories kept per user
SHOWN = 8000  # characters of them in an agent's note; the rest with `relay memory list`
SAME_WORDS = 0.7  # of two memories' words, the share they have in common from which they likely say the same
# How an agent adds, changes, forgets and lists, as it calls Relay's tools: the `relay` command, MCP
# tools (Claude Code), a chat model's functions (relay.toolbox): (add, its project flag, change, forget, list)
HOW = {"script": ('`relay memory add --category C "…"`', "--project", '`relay memory change ID [--category C] ["…"]`',
                  "`relay memory forget ID`", "`relay memory list`"),
       "mcp": ("memory_add (with its category)", "project true", "memory_change", "memory_forget", "memory_list"),
       "functions": ("remember (with its category)", "project true", "change_memory", "forget_memory",
                     "list_memories")}


class MemoryError(Exception):
    pass


def _preferences(db: Database, user: int) -> dict[str, Any]:
    row = db.one("SELECT value FROM preferences WHERE user_id = ? AND key = ?", (user, KEY))
    return json.loads(row["value"]) if row else {}


def enabled(db: Database, user: int) -> bool:
    return bool(_preferences(db, user).get("enabled", True))


def agents_add(db: Database, user: int) -> bool:
    """Whether agents save what they learn (add, change, forget), or only read what the user keeps."""
    return bool(_preferences(db, user).get("agents_add", True))


def set_preferences(db: Database, user: int, **changes: bool) -> None:
    """enabled, agents_add: those given changed, the others as they are."""
    values = {"enabled": enabled(db, user), "agents_add": agents_add(db, user)}
    values.update({k: bool(v) for k, v in changes.items() if k in values})
    db.execute("INSERT INTO preferences (user_id, key, value) VALUES (?, ?, ?) ON CONFLICT (user_id, key) "
               "DO UPDATE SET value = excluded.value", (user, KEY, json.dumps(values)))


def set_enabled(db: Database, user: int, on: bool) -> None:
    set_preferences(db, user, enabled=on)


def project_of(db: Database, conversation: dict[str, Any]) -> str | None:
    """The project a conversation works on: a code task's folder (a task's own branch or a subagent's
    copy stands for its project's: vpn.origin), a service's WorkBox the service; a chat none."""
    from relay.vpn import origin
    if conversation.get("agentos_role") == "governor":
        return GOVERNOR  # (its own memory, not its notebook folder's)
    if conversation.get("mode") != "code" or not conversation.get("directory"):
        return None
    clone = db.one("SELECT service_id FROM service_clones WHERE conversation_id = ?", (conversation["id"],))
    return f"service:{clone['service_id']}" if clone else str(origin(conversation["directory"]))


def label(db: Database, project: str | None) -> str:
    if project is None:
        return "General"
    if project == GOVERNOR:
        return "The Governor"
    if project.startswith("service:"):
        service = db.one("SELECT name FROM services WHERE id = ?", (project.removeprefix("service:"),))
        return f"The service {service['name']}" if service else "A service that's gone"
    return project


def projects(db: Database, user: int) -> list[dict[str, str]]:
    """The projects a memory can be about: those of the user's code tasks, their services, the Governor
    (when there's one), and those memories are about already. The Governor first."""
    from relay.vpn import origin
    found: set[str] = set()
    for row in db.all("SELECT DISTINCT directory FROM conversations WHERE user_id = ? AND mode = 'code' AND "
                      "parent_conversation IS NULL AND directory IS NOT NULL AND agentos_role IS NOT 'governor'",
                      (user,)):
        if row["directory"] and Path(row["directory"]).is_dir():
            found.add(str(origin(row["directory"])))
    found -= {p for p in found if "/production/" in p}  # (a service's WorkBoxes: the service)
    found |= {f"service:{r['id']}" for r in db.all("SELECT id FROM services WHERE user_id = ?", (user,))}
    found |= {r["project"] for r in db.all("SELECT DISTINCT project FROM memories WHERE user_id = ? AND "
                                           "project IS NOT NULL", (user,))}
    if db.one("SELECT 1 FROM conversations WHERE user_id = ? AND agentos_role = 'governor'", (user,)):
        found.add(GOVERNOR)
    return sorted(({"key": p, "label": label(db, p)} for p in found),
                  key=lambda p: (p["key"] != GOVERNOR, p["label"].lower()))


def listed(db: Database, user: int) -> list[dict[str, Any]]:
    """All of a user's memories, general first, then per project; newest last within each."""
    rows = db.all("SELECT m.*, c.title AS conversation_title FROM memories m LEFT JOIN conversations c "
                  "ON c.id = m.conversation_id WHERE m.user_id = ? ORDER BY m.project IS NOT NULL, m.project, "
                  "m.created", (user,))
    return [{**r, "short": r["id"][:6], "label": label(db, r["project"])} for r in rows]


def add(db: Database, user: int, text: str, project: str | None = None, by: str = "you",
        conversation_id: str | None = None, category: str | None = None,
        created: float | None = None) -> dict[str, Any]:
    """A memory (`created`: when it was first, for one put back after it was forgotten)."""
    text = _text(text)
    kind = _category(category)
    same = db.one("SELECT * FROM memories WHERE user_id = ? AND project IS ? AND lower(text) = lower(?)",
                  (user, project, text))
    if same:
        return same  # (said twice)
    if db.one("SELECT count(*) AS n FROM memories WHERE user_id = ?", (user,))["n"] >= MOST:
        raise MemoryError(f"The memory is full ({MOST}): forget what's no longer true first")
    now = time.time()
    memory_id = uuid.uuid4().hex
    db.execute("INSERT INTO memories (id, user_id, project, text, by, conversation_id, created, updated, category) "
               "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
               (memory_id, user, project, text, by, conversation_id, created or now, now, kind))
    return find(db, user, memory_id)


def change(db: Database, user: int, memory_id: str, text: str | None = None, project: Any = ...,
           category: str | None = None) -> dict[str, Any]:
    """A memory's new text, category, or (project: None for general) its new place."""
    found = find(db, user, memory_id)
    fields = {"text": _text(text) if text is not None else found["text"],
              "project": found["project"] if project is ... else project,
              "category": _category(category) if category is not None else found["category"]}
    db.execute("UPDATE memories SET text = ?, project = ?, category = ?, updated = ? WHERE id = ?",
               (fields["text"], fields["project"], fields["category"], time.time(), found["id"]))
    return find(db, user, found["id"])


def clear(db: Database, user: int, project: Any = ...) -> int:
    """All of a user's memories forgotten (or one place's: None for the general ones); how many."""
    if project is ...:
        return db.execute("DELETE FROM memories WHERE user_id = ?", (user,)).rowcount
    return db.execute("DELETE FROM memories WHERE user_id = ? AND project IS ?", (user, project)).rowcount


def forget(db: Database, user: int, memory_id: str) -> dict[str, Any]:
    found = find(db, user, memory_id)
    db.execute("DELETE FROM memories WHERE id = ?", (found["id"],))
    return found


def find(db: Database, user: int, memory_id: str) -> dict[str, Any]:
    """A memory by its id, or its first characters (as the note shows it)."""
    wanted = str(memory_id or "").strip().strip("[]").lower()
    rows = db.all("SELECT * FROM memories WHERE user_id = ? AND id LIKE ?", (user, wanted + "%")) if wanted else []
    if len(rows) != 1:
        raise MemoryError(f"No memory {memory_id!r}" if not rows else f"More than one memory starts with {memory_id!r}")
    return rows[0]


def note(db: Database, user: int, project: str | None, style: str = "script", used: float | None = None) -> str | None:
    """What an agent is told: how to add to the memory, and what it holds for it (general, and its
    project's; the Governor's own for the Governor). `style`: script (`relay memory …`), mcp (its MCP
    tools), functions (a chat model's) or read (a model without tools: only what it holds). `used`: when
    an agent gets it (relay.runs), the memories it shows are marked used then (review). None when the
    user turned it off."""
    if not enabled(db, user):
        return None
    governor = project == GOVERNOR
    text = ("Relay's memory (Settings → Memory): what every agent knows of the user, and your own, the Governor's, "
            "which no other agent gets." if governor else
            "Relay's memory, which every agent in every chat of the user's gets (Settings → Memory).")
    if style == "read":
        listing = "Settings → Memory"
    elif not agents_add(db, user):
        listing = HOW[style][4]
        text += " The user keeps it themselves: when something should be remembered, tell them."
    else:
        add, flag, change, forget, listing = HOW[style]
        if project is not None:
            add += f" ({flag}: {'your own memory' if governor else 'for this project only'})"
        about = "their wishes or this project" if project and not governor else "their wishes"
        kinds = ", ".join(KINDS)
        text += (f" When the user tells you something lasting about themselves, {about}, or asks you to remember "
                 f"something, save it there, one short fact each, filed in its category ({kinds}): {add}. When one "
                 f"changed: {change}; when it's wrong or no longer so: {forget} (but not the user's own, marked so: "
                 "theirs to change). Not what only this conversation needs, and not what's "
                 "below already. Prefer it to a memory of your own program's: this one reaches the user's other "
                 "agents too.")
        if governor:
            text += (" What you learn as the Governor that lasts (the user's decisions, how their services and tasks "
                     "are, what didn't work and why) goes in your own memory: you get it with every briefing. What "
                     "every agent should know of the user goes in the general one.")
    general = db.all("SELECT * FROM memories WHERE user_id = ? AND project IS NULL ORDER BY created", (user,))
    own = db.all("SELECT * FROM memories WHERE user_id = ? AND project = ? ORDER BY created",
                 (user, project)) if project else []
    titles = (("What every agent knows of the user:", "Your own memory (the Governor's):") if governor
              else ("What it holds:", f"About this project ({label(db, project)}):"))
    parts, size, left, shown = [], 0, 0, []
    for title, rows in zip(titles, (general, own), strict=True):
        lines = []
        for row in rows:
            line = (f"- [{row['id'][:6]}] ({row['category']}) {row['text']}"
                    + (" (the user's own)" if row["by"] == "you" else ""))
            if size + len(line) > SHOWN:
                left += 1
                continue
            size += len(line)
            lines.append(line)
            shown.append(row["id"])
        if lines:
            parts.append("\n".join([title, *lines]))
    if used is not None and shown:
        db.execute(f"UPDATE memories SET used = ? WHERE id IN ({', '.join('?' * len(shown))})", (used, *shown))
    if not parts:
        parts.append("It holds nothing yet.")
    if left:
        parts.append(f"(and {left} more: {listing})")
    return "\n\n".join([text, *parts])


def review(db: Database, user: int, now: float, idle_days: float) -> dict[str, Any]:
    """The memory as AgentOS's tidy-up sees it (its system task memory-tidy): every memory with the days no
    agent got it (`idle`: since it was used, else changed), and what Relay found: memories that likely say the
    same (`same`: groups worded nearly alike; one said in other words only a reader sees), those whose place is
    gone (`gone`: its folder or service deleted), and those no agent got for idle_days or more (`idle`)."""
    rows = listed(db, user)
    for row in rows:
        row["idle_days"] = max(0.0, (now - (row["used"] or row["updated"])) / 86400)
        row["gone"] = _gone(db, row["project"])
    return {"memories": rows, "same": _alike(rows), "gone": [r for r in rows if r["gone"]],
            "idle": [r for r in rows if not r["gone"] and r["idle_days"] >= idle_days]}


def _gone(db: Database, project: str | None) -> bool:
    """A memory's place that's no longer there: its folder, its service (general and the Governor's never)."""
    if project is None or project == GOVERNOR:
        return False
    if project.startswith("service:"):
        return db.one("SELECT 1 FROM services WHERE id = ?", (project.removeprefix("service:"),)) is None
    return not Path(project).is_dir()


def _alike(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Memories worded nearly alike (most of their words the same), in one place, or one general and one a
    project's (two projects may each hold the same, for themselves): groups of two or more."""
    words = [set(re.findall(r"\w+", row["text"].lower())) for row in rows]
    group = list(range(len(rows)))

    def root(i: int) -> int:
        while group[i] != i:
            i = group[i]
        return i
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            if rows[i]["project"] != rows[j]["project"] and None not in (rows[i]["project"], rows[j]["project"]):
                continue
            both = words[i] | words[j]
            if both and len(words[i] & words[j]) / len(both) >= SAME_WORDS:
                group[root(j)] = root(i)
    found: dict[int, list[dict[str, Any]]] = {}
    for i, row in enumerate(rows):
        found.setdefault(root(i), []).append(row)
    return [g for g in found.values() if len(g) > 1]


def _category(value: Any) -> str:
    """One of CATEGORIES, by its id or its label ("other" when none is said)."""
    wanted = str(value or "").strip().lower()
    if not wanted:
        return "other"
    for kind, name, _ in CATEGORIES:
        if wanted in (kind, name.lower()):
            return kind
    raise MemoryError(f"No category {value!r}: one of {', '.join(KINDS)}")


def _text(text: Any) -> str:
    text = " ".join(str(text or "").split())
    if not text:
        raise MemoryError("Say what to remember (text)")
    if len(text) > LONGEST:
        raise MemoryError(f"A memory is one short fact (at most {LONGEST} characters): split it, or keep the gist")
    return text
