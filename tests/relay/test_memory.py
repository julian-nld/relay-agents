"""Relay's memory (relay.memory): one place for every agent and chat, general and per project."""

import pytest

from relay import memory
from relay.db import Database


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_HOME", str(tmp_path / "data"))
    database = Database(tmp_path / "relay.db")
    database.execute("INSERT INTO users (id, username, password_hash, created) VALUES (1, 'me', 'x', 0)")
    return database


def test_general_and_a_projects_memories_and_what_an_agent_is_told(db):
    assert memory.note(db, 1, None).endswith("It holds nothing yet.")
    general = memory.add(db, 1, "  Talks   Dutch;\ncode in English ")
    assert general["text"] == "Talks Dutch; code in English" and general["by"] == "you"
    assert memory.add(db, 1, "talks dutch; code in english")["id"] == general["id"]  # (said twice)
    site = memory.add(db, 1, "Deploys with make release", "/p/site", by="Claude Code · sonnet", conversation_id="c1")
    memory.add(db, 1, "Uses Postgres 17", "/p/shop")
    told = memory.note(db, 1, "/p/site", "script")
    assert '`relay memory add --category C "…"` (--project: for this project only)' in told
    assert f"- [{general['id'][:6]}] (other) Talks Dutch" in told
    assert f"About this project (/p/site):\n- [{site['id'][:6]}] (other)" in told
    assert "Postgres" not in told  # (another project's)
    chat = memory.note(db, 1, None, "mcp")
    assert "memory_add (with its category)." in chat and "project true" not in chat and "Deploys" not in chat
    assert "remember (with its category) (project true" in memory.note(db, 1, "/p/site", "functions")
    assert "When the user tells you" not in memory.note(db, 1, None, "read") and "Talks Dutch" in memory.note(db, 1, None, "read")
    # Changed, moved, forgotten: by its id or its first characters, as the note shows it
    assert memory.change(db, 1, site["id"][:6], "Deploys with make ship")["text"] == "Deploys with make ship"
    assert memory.change(db, 1, site["id"], project=None)["project"] is None
    assert memory.forget(db, 1, f"[{general['id'][:6]}]")["text"].startswith("Talks Dutch")
    with pytest.raises(memory.MemoryError, match="No memory"):
        memory.find(db, 1, "zzzzzz")
    # Off: agents are told nothing
    memory.set_enabled(db, 1, False)
    assert memory.note(db, 1, "/p/site") is None and not memory.enabled(db, 1)
    memory.set_enabled(db, 1, True)
    assert memory.enabled(db, 1) and memory.agents_add(db, 1)
    # Agents only reading: they're told what it holds, and that the user keeps it
    memory.set_preferences(db, 1, agents_add=False)
    read = memory.note(db, 1, "/p/site", "mcp")
    assert memory.enabled(db, 1) and not memory.agents_add(db, 1)
    assert "The user keeps it themselves" in read and "memory_add" not in read and "Deploys" in read
    memory.set_preferences(db, 1, agents_add=True)


def test_each_memory_is_filed_in_a_category(db):
    """As Perplexity files them (Interests, Tools, Work, Preferences, Location, …): by its id or its label;
    none said, other; an unknown one says which there are."""
    assert memory.add(db, 1, "Uses pnpm")["category"] == "other"
    tools = memory.add(db, 1, "Writes TypeScript", category="tools")
    assert memory.add(db, 1, "Lives in Utrecht", category="Location")["category"] == "location"
    assert memory.add(db, 1, "Is a nurse", category="About you")["category"] == "about"
    with pytest.raises(memory.MemoryError, match="No category 'hobby': one of preferences, about"):
        memory.add(db, 1, "Plays chess", category="hobby")
    assert memory.change(db, 1, tools["id"], category="work")["category"] == "work"
    assert memory.change(db, 1, tools["id"], "Writes Go")["category"] == "work"  # (kept with a new text)
    assert f"- [{tools['id'][:6]}] (work) Writes Go" in memory.note(db, 1, None)
    assert "(preferences, about, work, tools, interests, location, setup, decisions, other)" in memory.note(db, 1, None)
    # Delete all, or one place's
    memory.add(db, 1, "Deploys with make", "/p/site", category="setup")
    assert memory.clear(db, 1, "/p/site") == 1 and memory.clear(db, 1) == 4 and memory.listed(db, 1) == []


def test_the_governor_has_a_memory_of_its_own(db):
    """Its place is the Governor (not its notebook folder): it gets that and the general one, with how to use
    both; no other agent gets it."""
    db.execute("INSERT INTO conversations (id, user_id, title, provider_id, mode, directory, agentos_role, created, "
               "updated) VALUES ('g', 1, 'AgentOS Governor', 'fake', 'code', '/data/agentos/1/governor', 'governor', 0, 0)")
    governor = db.one("SELECT * FROM conversations WHERE id = 'g'")
    assert memory.project_of(db, governor) == memory.GOVERNOR and memory.label(db, memory.GOVERNOR) == "The Governor"
    assert memory.projects(db, 1)[0] == {"key": "governor", "label": "The Governor"}
    assert "/data/agentos/1/governor" not in [p["key"] for p in memory.projects(db, 1)]
    memory.add(db, 1, "Talks Dutch", category="about")
    own = memory.add(db, 1, "The user wants politiclone's cache left as it is", memory.GOVERNOR, "GPT · sol",
                     category="decisions")
    told = memory.note(db, 1, memory.GOVERNOR, "mcp")
    assert "What every agent knows of the user:\n- [" in told and "Talks Dutch" in told
    assert f"Your own memory (the Governor's):\n- [{own['id'][:6]}] (decisions) The user wants" in told
    assert "(project true: your own memory)" in told and "goes in your own memory" in told
    for elsewhere in (None, "/p/site"):
        assert "politiclone" not in memory.note(db, 1, elsewhere, "mcp")


def test_a_memory_is_one_short_fact_and_there_are_so_many(db, monkeypatch):
    with pytest.raises(memory.MemoryError, match="Say what"):
        memory.add(db, 1, "   ")
    with pytest.raises(memory.MemoryError, match="one short fact"):
        memory.add(db, 1, "x" * (memory.LONGEST + 1))
    monkeypatch.setattr(memory, "MOST", 2)
    memory.add(db, 1, "one")
    memory.add(db, 1, "two")
    with pytest.raises(memory.MemoryError, match="full"):
        memory.add(db, 1, "three")
    monkeypatch.setattr(memory, "SHOWN", 48)  # (room for one of them: "- [id] (other) one (the user's own)")
    told = memory.note(db, 1, None)
    assert "] (other) one" in told and "] (other) two" not in told and told.endswith("(and 1 more: `relay memory list`)")


def test_the_project_a_conversation_works_on(db, tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    assert memory.project_of(db, {"id": "c", "mode": "chat", "directory": str(tmp_path / "chats" / "c")}) is None
    assert memory.project_of(db, {"id": "c", "mode": "code", "directory": str(site)}) == str(site.resolve())
    # A service's WorkBox: the service
    db.execute("INSERT INTO services (id, user_id, name, command, public_port, created) VALUES ('s1', 1, 'notes', 'x', 1, 0)")
    db.execute("INSERT INTO service_clones (conversation_id, service_id, from_version, created) VALUES ('c3', 's1', 'v', 0)")
    assert memory.project_of(db, {"id": "c3", "mode": "code", "directory": "/somewhere/notes"}) == "service:s1"
    assert memory.label(db, "service:s1") == "The service notes" and memory.label(db, None) == "General"
    keys = [p["key"] for p in memory.projects(db, 1)]
    assert "service:s1" in keys


def test_what_agents_get_is_marked_used_and_the_review_finds_what_to_tidy(db, tmp_path):
    """An agent's note marks the memories it shows used (then). The review (AgentOS's tidy-up) gives each
    memory the days no agent got it, and finds memories worded nearly alike (in one place, or general and a
    project's), those whose place is gone, and those unused for long."""
    project, old, gone = tmp_path / "project", tmp_path / "old", tmp_path / "gone"
    project.mkdir()
    old.mkdir()
    mine = memory.add(db, 1, "The user prefers answers in Dutch")
    said = memory.add(db, 1, "User prefers answers in Dutch.", by="Claude Code · opus")
    pnpm = memory.add(db, 1, "Uses pnpm", str(project), by="opencode")
    npm = memory.add(db, 1, "Uses npm", str(project), by="opencode")
    elsewhere = memory.add(db, 1, "Uses pnpm", str(old), by="opencode")  # (another project's: its own)
    left = memory.add(db, 1, "Runs on port 8501", str(gone), by="opencode")
    service = memory.add(db, 1, "Its health check is /health", "service:deleted", by="opencode")
    now = 1_000_000_000.0
    db.execute("UPDATE memories SET updated = ?", (now - 200 * 86400,))
    memory.note(db, 1, str(project), used=now - 86400)  # (an agent in the project, yesterday)
    assert {r["id"] for r in db.all("SELECT id FROM memories WHERE used = ?", (now - 86400,))} == {
        mine["id"], said["id"], pnpm["id"], npm["id"]}
    found = memory.review(db, 1, now, 90)
    days = {m["id"]: round(m["idle_days"]) for m in found["memories"]}
    assert days[mine["id"]] == 1 and days[elsewhere["id"]] == 200
    assert [{m["id"] for m in group} for group in found["same"]] == [{mine["id"], said["id"]}]
    assert {m["id"] for m in found["gone"]} == {left["id"], service["id"]}
    assert [m["id"] for m in found["idle"]] == [elsewhere["id"]]  # (the gone ones are said once, as gone)
    assert memory.review(db, 1, now, 365)["idle"] == []
