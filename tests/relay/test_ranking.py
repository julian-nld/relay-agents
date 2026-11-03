"""AgentOS's model ranking (relay.ranking): the Governor's findings kept, the models here ranked from
them, and the ranks where models are chosen; nothing of it without AgentOS."""

import asyncio
import json
import sys
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from relay import config, ranking, schedules
from relay.agentos import AgentOSError
from relay.app import create_app
from relay.db import Database
from relay.providers import Registry

H = {"x-relay-request": "1"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_HOME", str(tmp_path / "data"))
    settings = config.Settings(workspace_roots=[str(tmp_path / "ws")], chat_root=str(tmp_path / "chats"))
    registry = Registry(tmp_path / "providers")
    for provider_id, models in (("alpha", ["big", "small"]), ("beta", ["big", "other", "mystery"])):
        registry.add({"id": provider_id, "name": provider_id.title(), "kind": "command",
                      "options": {"command": [sys.executable, "-c", "pass"], "models": models}})
    app = create_app(settings, Database(tmp_path / "relay.db"), registry)
    with TestClient(app) as client:
        assert client.post("/api/setup", json={"username": "me", "password": "correct horse"}, headers=H).status_code == 200
        client.relay = app.state.relay
        client.user = client.relay.db.one("SELECT id FROM users")["id"]
        yield client


def capability(env, which, **arguments):
    method = getattr(env.relay.agentos, "_" + which.replace(".", "_"))
    return asyncio.run(method(None, env.user, arguments))


def test_the_governor_finds_scores_once_and_relay_ranks_the_models_here(env):
    relay = env.relay
    listed = capability(env, "models.list")
    assert "- alpha (Alpha):" in listed and "big: not looked up yet" in listed
    assert "Not looked up yet (5): alpha big, alpha small, beta big, beta other, beta mystery" in listed
    saved = capability(env, "models.score", scores=[
        {"provider": "alpha", "model": "big", "name": "Big Model 2", "score": 70, "source": "https://example.org/aa"},
        {"provider": "alpha", "model": "small", "name": "Small Model", "score": 41.5},
        {"provider": "beta", "model": "other", "name": "Other Model", "score": 70, "estimated": True},
        {"provider": "beta", "model": "mystery", "name": "Mystery"}])  # (no benchmark knows it)
    assert "Saved 4" in saved and "beta mystery: Mystery, no benchmark knows it" in saved
    assert "every model has been looked up" in saved  # (beta big: the same id as alpha's)
    found = ranking.ranking(relay.db)
    assert [(m["rank"], m["name"]) for m in found["models"]] == [(1, "Big Model 2"), (1, "Other Model"), (3, "Small Model")]
    assert found["of"] == 3 and found["unknown"] == ["Mystery"] and found["unscored"] == []
    assert found["ranks"] == {"alpha": {"big": 1, "small": 3}, "beta": {"big": 1, "other": 1}}
    big = found["models"][0]
    assert big["models"] == [{"provider_id": "alpha", "model": "big"}, {"provider_id": "beta", "model": "big"}]
    assert big["benchmark"] == ranking.INDEX and big["source"] == "https://example.org/aa"
    assert found["models"][1]["estimated"]
    # Next week: what's kept stays; only a new model is to be looked up, and a name kept needs no score
    assert "Nothing new" in capability(env, "models.list")
    relay.registry.update("alpha", {"id": "alpha", "name": "Alpha", "kind": "command",
                                    "options": {"command": [sys.executable, "-c", "pass"], "models": ["big", "small", "huge"]}})
    assert "Not looked up yet (1): alpha huge" in capability(env, "models.list")
    capability(env, "models.score", message=json.dumps([{"provider": "alpha", "model": "huge", "name": "Huge", "score": 80}]))
    capability(env, "models.score", provider="beta", model="other", name="Small Model")  # (it was the small one after all)
    found = ranking.ranking(relay.db)
    assert found["ranks"]["alpha"] == {"huge": 1, "big": 2, "small": 3} and found["ranks"]["beta"]["other"] == 3
    assert relay.db.one("SELECT score FROM model_scores WHERE name = 'Small Model'")["score"] == 41.5
    # One no benchmark knew is looked up again a month later (benchmarks add models)
    assert ranking.unscored(relay.db, ranking.listed(relay.db)[1]) == []
    later = ranking.listed(relay.db)[0] + ranking.RECHECK + 60
    assert ranking.unscored(relay.db, ranking.listed(relay.db)[1], later) == [("beta", "mystery")]


def test_a_router_is_kept_but_not_ranked(env):
    """A router (Copilot's auto: another model per request) has no score of its own: kept as one, not ranked,
    not looked up again, and said so where the ranks are."""
    relay = env.relay
    assert "save it with router true" in capability(env, "models.list")
    saved = capability(env, "models.score", scores=[
        {"provider": "alpha", "model": "big", "name": "Big Model 2", "score": 70},
        {"provider": "alpha", "model": "small", "name": "Alpha auto", "router": True, "score": 55},  # (score ignored)
        {"provider": "beta", "model": "other", "name": "Other Model", "score": 60},
        {"provider": "beta", "model": "mystery", "name": "Mystery"}])
    assert "alpha small: Alpha auto, a router (a model per request), not ranked" in saved
    found = ranking.ranking(relay.db)
    assert [m["name"] for m in found["models"]] == ["Big Model 2", "Other Model"]
    assert found["routers"] == ["Alpha auto"] and found["routing"] == {"alpha": ["small"]}
    assert found["unknown"] == ["Mystery"] and "small" not in found["ranks"]["alpha"]
    later = ranking.listed(relay.db)[0] + ranking.RECHECK + 60
    assert ranking.unscored(relay.db, ranking.listed(relay.db)[1], later) == [("beta", "mystery")]
    assert "small: Alpha auto, a router (a model per request), not ranked" in capability(env, "models.list")
    conversation = env.post("/api/conversations", json={"provider_id": "alpha"}, headers=H).json()
    listed = relay.delegation.list_agents(SimpleNamespace(conversation_id=conversation["id"]))
    assert "routers (another model per request, so not ranked): small" in listed


def test_a_note_beside_the_name_and_starting_over(env):
    """What isn't the model's name (a tier, a setting) goes in its note, so one model is one name; Start
    over forgets what's kept, and every model is looked up anew."""
    capability(env, "models.list")
    capability(env, "models.score", scores=[
        {"provider": "alpha", "model": "big", "name": "Big Model 2", "score": 70, "note": "max reasoning"},
        {"provider": "beta", "model": "other", "name": "Big Model 2", "note": "a fast tier of it"}])
    found = ranking.ranking(env.relay.db)
    assert found["of"] == 1 and found["models"][0]["note"] == "a fast tier of it"  # (the latest word on it)
    assert found["ranks"]["beta"]["other"] == 1
    assert "[a fast tier of it]" in capability(env, "models.list")
    gone = env.delete("/api/models/ranking", headers=H).json()
    assert gone["of"] == 0 and not env.relay.db.one("SELECT count(*) AS n FROM model_names")["n"]
    listed = capability(env, "models.list")
    assert "Not looked up yet (5)" in listed and "The user started the ranking over on" in listed  # (on purpose)
    assert any(e["kind"] == "ranking.forgotten" and e["actor"] == "user" for e in env.get("/api/agentos").json()["events"])


def test_what_the_governor_gets_wrong_is_refused(env):
    capability(env, "models.list")
    with pytest.raises(AgentOSError, match="No provider 'nope'"):
        capability(env, "models.score", scores=[{"provider": "nope", "model": "x", "name": "X", "score": 1}])
    with pytest.raises(AgentOSError, match="the score is a number"):
        capability(env, "models.score", scores=[{"provider": "alpha", "model": "big", "name": "X", "score": "high"}])
    with pytest.raises(AgentOSError, match="which model is it"):
        capability(env, "models.score", scores=[{"provider": "alpha", "model": "big", "score": 3}])
    with pytest.raises(AgentOSError, match="JSON list"):
        capability(env, "models.score", message="[{not json")
    assert env.relay.db.one("SELECT count(*) AS n FROM model_scores")["n"] == 0  # (all or nothing)


def test_the_ranks_where_models_are_chosen(env):
    assert env.get("/api/models/ranking").json()["of"] == 0  # (before the Governor's first: none)
    assert all(m["rank"] is None for m in env.get("/api/providers/alpha/models").json()["models"])
    capability(env, "models.list")
    capability(env, "models.score", scores=[{"provider": "alpha", "model": "big", "name": "Big", "score": 60},
                                            {"provider": "alpha", "model": "small", "name": "Small", "score": 30}])
    models = env.get("/api/providers/alpha/models").json()
    assert {m["id"]: m["rank"] for m in models["models"]} == {"big": 1, "small": 2} and models["ranked"] == 2
    assert env.get("/api/models/ranking").json()["models"][0]["name"] == "Big"
    # An agent choosing a subagent sees them
    relay = env.relay
    conversation = env.post("/api/conversations", json={"provider_id": "alpha"}, headers=H).json()
    listed = relay.delegation.list_agents(SimpleNamespace(conversation_id=conversation["id"]))
    assert "Intelligence ranks: AgentOS's ranking of the models here" in listed and "#1 is the smartest of 2" in listed
    assert "intelligence ranks: big #1, small #2" in listed


def test_the_weekly_task_wakes_the_governor_only_for_new_models(env):
    relay = env.relay
    relay.agentos.save_settings(env.user, {"enabled": True})
    task = next(t for t in relay.schedules.all(env.user) if t.get("system") == "models-rank")
    assert task["governed"] and task["enabled"] and task["name"] == schedules.SYSTEM_TASKS["models-rank"]["name"]
    # New models: the Governor is woken for it
    assert asyncio.run(relay.scheduler._ranked_already(task, 1000.0)) is False
    capability(env, "models.list")
    capability(env, "models.score", scores=[
        {"provider": "alpha", "model": m, "name": m.title(), "score": 50} for m in ("big", "small")]
        + [{"provider": "beta", "model": m, "name": m.title()} for m in ("other", "mystery")])
    # Nothing new: Relay does it (noted with the task), and the Governor sleeps on
    assert asyncio.run(relay.scheduler._ranked_already(task, 2000.0)) is True
    done = relay.schedules.get(env.user, task["id"])
    assert done["last_run"] == 2000.0 and done["outcome_by"] == "relay" and "the ranking of 2 stands" in done["outcome_note"]
    # AgentOS off: the task goes as any does (noted that nobody carried it out)
    relay.agentos.save_settings(env.user, {"enabled": False})
    assert asyncio.run(relay.scheduler._ranked_already(task, 3000.0)) is False


def test_without_agentos_nothing_changes(env):
    """No ranking yet (AgentOS off): models, loops and the agents list are as before."""
    relay = env.relay
    assert not relay.agentos.settings(env.user)["enabled"]
    assert not any(t.get("system") for t in relay.schedules.all(env.user))
    assert env.get("/api/models/ranking").json() == {"updated": None, "of": 0, "benchmark": None, "models": [],
                                                     "ranks": {}, "unknown": [], "routers": [], "routing": {},
                                                     "unscored": []}
    conversation = env.post("/api/conversations", json={"provider_id": "alpha"}, headers=H).json()
    listed = relay.delegation.list_agents(SimpleNamespace(conversation_id=conversation["id"]))
    assert "ntelligence ranks" not in listed and "- alpha (you)" in listed


def test_a_provider_that_cant_list_its_models_now_keeps_its_place(env):
    before = [("alpha", "big"), ("beta", "big"), ("gone", "x")]
    registry = env.relay.registry
    real = registry.provider

    def provider(provider_id):
        found = real(provider_id)
        if provider_id == "beta":
            found.models = lambda: (_ for _ in ()).throw(RuntimeError("starting"))
        return found
    registry.provider = provider
    try:
        assert ranking.available(registry, before) == [("alpha", "big"), ("alpha", "small"), ("beta", "big")]
    finally:
        registry.provider = real
