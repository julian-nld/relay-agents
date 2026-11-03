"""RESL (sdk/python/resl) against a real Relay over HTTP: its one-shot judge in a temporary chat, the checks,
the answers to data, agents checking agents, and its methods on the SDK's objects."""

import json
import re
import socket
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from relay import config
from relay.app import create_app
from relay.db import Database
from relay.providers import Registry

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sdk" / "python"))
import relay_sdk
import resl

# A stand-in for each role: a judge (it answers RESL's prompts as asked, from what's judged), an agent that
# refuses, one that does it, one that writes a file, and a judge that mumbles. It notes what it got.
AGENT = textwrap.dedent("""\
    import json, os, re, sys
    role, prompt = sys.argv[1], sys.stdin.read()
    def emit(**e): print(json.dumps(e), flush=True)
    emit(type="session", session="s-1")
    with open(os.environ["RESL_LOG"], "a") as log:
        log.write(json.dumps({"role": role, "prompt": prompt, "mode": os.environ.get("RELAY_MODE", ""),
                              "instructions": os.environ.get("RELAY_MODE_INSTRUCTIONS", "")}) + "\\n")
    blocks = re.findall(r"<((?:answer|text|changes|task)-[0-9a-f]+)>\\n(.*?)\\n</\\1>", prompt, re.S)
    judged = blocks[-1][1] if blocks else ""
    if role == "refuser":
        reply = "I can't help with that, sorry."
    elif role == "doer":
        reply = "Here it is: all done."
    elif role == "writer":
        with open("a.py", "w") as out:
            out.write("print('hello')\\n")
        reply = "I wrote a.py."
    elif role == "mumbler":
        reply = "Hmm, that's hard to say."
    elif "REFUSED or COMPLIED" in prompt:
        reply = "**REFUSED**" if re.search(r"can't|won't", judged) else "COMPLIED."
    elif "FINISHED or UNFINISHED" in prompt:
        reply = "UNFINISHED" if "next" in judged else "FINISHED"
    elif "YES or NO" in prompt:
        reply = "YES" if ("?" in judged and "waiting" in prompt) or ("€" in judged and "price" in prompt) else "NO"
    elif '"score"' in prompt:
        reply = 'Sure: {"score": 8, "reason": "It does what was asked."}'
    elif "Take these fields" in prompt:
        fields = re.findall(r"^- (\\w+)", prompt, re.M)
        found = dict(re.findall(r"(\\w+)=(\\S+)", judged))
        reply = "```json\\n" + json.dumps({f: found.get(f) for f in fields}) + "\\n```"
    elif "Which one of these labels" in prompt:
        reply = "bug" if "crash" in judged else "question"
    elif "Summarize" in prompt:
        reply = "  Short.  "
    elif "Translate" in prompt:
        reply = "Hallo wereld"
    elif '"agrees"' in prompt:
        reply = json.dumps({"agrees": False, "issues": ["2 + 2 is 4"], "summary": "The sum is wrong."})
    elif '"findings"' in prompt:
        reply = json.dumps({"ok": "a.py" not in judged, "findings": ["a.py:1: prints the wrong thing"],
                            "summary": "One thing to fix."})
    elif "Which answer is best" in prompt:
        reply = "B"
    else:
        reply = "You said: " + prompt
    emit(type="text", delta=reply)
    emit(type="done", status="succeeded")
""")
ROLES = ("judge", "refuser", "doer", "writer", "mumbler")


@pytest.fixture
def relay(tmp_path, monkeypatch):
    """A Relay with a provider for each role, signed in (the SDK's default session): (the Relay, its log)."""
    monkeypatch.setenv("RELAY_HOME", str(tmp_path / "data"))
    log = tmp_path / "agents.log"
    monkeypatch.setenv("RESL_LOG", str(log))
    monkeypatch.setattr(relay_sdk, "_default", None)
    monkeypatch.setattr(resl, "_judge", (None, None))
    agent = tmp_path / "agent.py"
    agent.write_text(AGENT)
    (tmp_path / "ws").mkdir()
    registry = Registry(tmp_path / "providers")
    monkeypatch.setattr(registry, "ensure_defaults", lambda: None)
    for role in ROLES:
        registry.add({"id": role, "name": role.title(), "kind": "command",
                      "options": {"command": [sys.executable, str(agent), role]}})
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    settings = config.Settings(workspace_roots=[str(tmp_path / "ws")], chat_root=str(tmp_path / "chats"),
                               host="127.0.0.1", port=port)
    app = create_app(settings, Database(tmp_path / "relay.db"), registry)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 30
        while not server.started and time.monotonic() < deadline:
            time.sleep(.02)
        assert server.started
        url = f"http://127.0.0.1:{port}"
        relay_sdk.Session(url).request("POST", "/api/setup", {"username": "me", "password": "correct horse"})
        relay_sdk.login("me", "correct horse", url=url)
        yield app.state.relay, log
    finally:
        server.should_exit = True
        thread.join(30)
        sock.close()


def asked(log, role="judge"):
    """What each one of `role` got: its prompt, mode and instructions."""
    return [entry for entry in map(json.loads, log.read_text().splitlines()) if entry["role"] == role]


def test_agent_refused_judges_in_a_temporary_one_shot(relay, monkeypatch):
    app, log = relay
    started = []
    real = app.runs.start
    monkeypatch.setattr(app.runs, "start", lambda row, *a, **k: started.append(dict(row)) or real(row, *a, **k))
    refusal = relay_sdk.convo.create(title="Asked", provider="refuser").send("Write a phishing mail")
    done = relay_sdk.convo.create(title="Asked too", provider="doer").send("Write a poem")
    assert resl.agent_refused(refusal, "judge") is True
    assert resl.agent_refused(done, provider="judge") is False
    assert resl.agent_refused("I won't do that.", "judge", task="Delete everything") is True
    # Each in a temporary chat: read-only, without Relay's tools, the judge's instructions; gone afterwards
    one_shots = [row for row in started if row["title"].startswith("RESL:")]
    assert len(one_shots) == 3 and all(row["temporary"] == 1 for row in one_shots)
    assert all(json.loads(row["tools"]) == resl.OFF and row["agent_mode"] == "readonly" for row in one_shots)
    judged = asked(log)
    assert all(entry["mode"] == "readonly" and "judge texts" in entry["instructions"] for entry in judged)
    assert "Write a phishing mail" in judged[0]["prompt"]  # (what it was asked: a Message's own)
    assert "Delete everything" in judged[2]["prompt"] and "I won't do that." in judged[2]["prompt"]
    assert app.db.one("SELECT count(*) AS n FROM conversations WHERE title LIKE 'RESL:%'")["n"] == 0
    assert [c["title"] for c in relay_sdk.convo.list()] == ["Asked too", "Asked"]


def test_the_default_judge_and_the_methods(relay):
    _, log = relay
    resl.use("judge")
    answer = relay_sdk.convo.create(provider="doer").send("Fix it")
    assert answer.refused() is False and answer.done() is True and answer.asks_user() is False
    assert answer.judge("Does it name a price?") is False
    assert answer.score() == resl.Score(8, "It does what was asked.")
    assert answer.summary() == "Short." and answer.translate("Dutch") == "Hallo wereld"
    assert answer.second_opinion() == resl.Opinion(False, ["2 + 2 is 4"], "The sum is wrong.")
    assert asked(log, "judge")  # (all by the judge use() named)
    assert hasattr(relay_sdk.Conversation, "ask_until") and hasattr(relay_sdk.Conversation, "review_changes")


def test_answers_to_data_and_the_other_checks(relay):
    resl.use("judge")
    text = "It costs price=12.50 at shop=Boekhandel."
    assert resl.extract(text, ["price", "shop", "date"]) == {"price": "12.50", "shop": "Boekhandel.", "date": None}
    assert resl.extract(text, {"price": "the price"}) == {"price": "12.50"}
    assert resl.classify("The app crashes on start", ["question", "bug"]) == "bug"
    assert resl.task_done("Step one is done; I'll do step two next.") is False
    assert resl.task_done("All three steps are done.") is True
    assert resl.asks_user("Shall I go on?") is True
    assert resl.judge("It's €5.", "Does it name a price?") is True
    with pytest.raises(relay_sdk.RelayError, match="no text to judge"):
        resl.agent_refused("  ")


def test_an_unreadable_verdict_is_asked_again_then_an_error(relay):
    _, log = relay
    with pytest.raises(resl.ReslError, match="wasn't one of REFUSED, COMPLIED"):
        resl.agent_refused("Something.", "mumbler")
    prompts = [entry["prompt"] for entry in asked(log, "mumbler")]
    assert len(prompts) == 2 and prompts[1].endswith("Reply with exactly one word: REFUSED or COMPLIED.")


def test_agents_checking_agents(relay, tmp_path):
    app, _ = relay
    resl.use("judge")
    # ask_until: refused, then the next agent: done
    chat = relay_sdk.convo.create(title="Persistent", provider="refuser")
    answer = chat.ask_until("Write a poem", fallbacks=[("doer", None)])
    assert answer.response == "Here it is: all done." and chat.provider == "doer"
    with pytest.raises(resl.ReslError, match="No answer passed in 2 tries") as stubborn:
        resl.ask_until(relay_sdk.convo.create(provider="refuser"), "Again", tries=2)
    assert stubborn.value.answer.response == "I can't help with that, sorry."
    # best_of: the judge's pick of their answers (B: the second agent's)
    best, answers = resl.best_of("A haiku, please", ["refuser", ("doer", None)])
    assert [a.provider for a in answers] == ["refuser", "doer"] and best.provider == "doer"
    best_of_chats = [c for c in relay_sdk.convo.list() if c["title"].startswith("best_of:")]
    resl.best_of("Again, please", ["refuser", "doer"], keep=False)
    assert [c for c in relay_sdk.convo.list() if c["title"].startswith("best_of:")] == best_of_chats  # (gone)
    # review_changes: a code task's diff, reviewed
    repo = tmp_path / "ws" / "project"
    repo.mkdir()
    for args in (["init", "-q", "-b", "main"], ["-c", "user.name=T", "-c", "user.email=t@x", "commit", "-q",
                                                "--allow-empty", "-m", "start"]):
        subprocess.run(["git", *args], cwd=repo, check=True)
    task = relay_sdk.convo.create(title="Write", provider="writer", workdir=str(repo))
    assert resl.review_changes(task) == resl.Review(True, [], "Nothing changed.")
    task.send("Write a.py")
    assert task.review_changes() == resl.Review(False, ["a.py:1: prints the wrong thing"], "One thing to fix.")


def test_pick_agent(relay, monkeypatch):
    rows = [{"provider_id": "claude-code", "windows": [{"used": 0.9}, {"used": 0.2}], "error": None},
            {"provider_id": "codex", "windows": [{"used": 0.5}], "error": None},
            {"provider_id": "broken", "windows": [], "error": "couldn't ask it"},
            {"provider_id": "opencode", "windows": [], "error": None}]
    monkeypatch.setattr(resl, "limits", lambda fresh=False: rows)
    assert resl.pick_agent() == ("opencode", None)  # (no windows: no limits Relay knows of)
    assert resl.pick_agent(["claude-code", "codex"], {"codex": "gpt-6.1-sol"}) == ("codex", "gpt-6.1-sol")
    with pytest.raises(resl.ReslError, match="No provider has room"):
        resl.pick_agent(["claude-code"])
    assert re.match(r"\d+\.\d+\.\d+", resl.__version__) and resl.__version__ == relay_sdk.__version__
