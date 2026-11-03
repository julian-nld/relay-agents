import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from relay.toolbox import Toolbox


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_HOME", str(tmp_path / "data"))
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("def greet():\n    return 'hi'\n")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "junk.js").write_text("greet")
    return root


def names(box):
    return [d["function"]["name"] for d in box.definitions()]


def test_which_tools_are_offered(project):
    workspace = ["list_files", "read_file", "search", "run_command", "write_file", "edit_file"]
    # A chat has its scratch folder and WorkBox too, as agents' chats do
    chat = Toolbox(project, code=False)
    assert names(chat) == [*workspace, "web_fetch", "list_skills", "read_skill"]
    code = Toolbox(project, code=True, relay_url="http://x/mcp/t")
    assert {"delegate", "wait", "relay_agents", "merge", *workspace} <= set(names(code))
    # Relay's memory, when the user has it on
    remembering = {"remember", "change_memory", "forget_memory", "list_memories"}
    assert not remembering & set(names(code))
    assert remembering <= set(names(Toolbox(project, code=True, relay_url="http://x/mcp/t", memory=True)))
    # Relay's subagent tools, unless the user turned them off for the conversation (relay.relay_tools)
    alone = Toolbox(project, code=True, relay_url="http://x/mcp/t", subagents=False)
    assert not {"delegate", "wait", "tell", "relay_agents", "merge"} & set(names(alone))
    assert "hands work to another AI agent" not in alone.guidance()
    # A computer task's work is on the user's computer: no folder of its own to confuse it with
    computer = Toolbox(project, code=False, relay_url="http://x/mcp/t", computers=["pc"], focus="pc")
    assert not set(workspace) & set(names(computer)) and "computer_run" in names(computer)


def test_the_model_knows_its_workbox_from_the_users_computers(project, tmp_path):
    shell = tmp_path / "shell"
    chat = Toolbox(project, code=False, shell=shell, relay_url="http://x/mcp/t", computers=["localhost"])
    told = chat.guidance()
    assert f"scratch folder of its own, {project}" in told
    assert "WorkBox: a Debian container on Relay's server (not the user's own computer)" in told
    assert "(localhost) are something else" in told and "only for what the user wants done" in told
    code = Toolbox(project, code=True, shell=shell).guidance()
    assert f"project folder {project}" in code and "not the user's own computer" in code
    # Perplexity's first message says so too, with the tools it has
    from relay.providers.perplexity.provider import _brief
    brief = _brief(chat, None)
    assert "not my own computer" in brief and "on my computer" not in brief
    assert "- run_command " in brief and "- computer_run " in brief


def test_files_and_commands(project):
    box = Toolbox(project, code=True)
    ok, listing = box.call("list_files", {})
    assert ok and "src/" in listing and "node_modules" not in listing
    ok, text = box.call("read_file", '{"path": "src/app.py"}')
    assert ok and "     1\tdef greet():" in text
    ok, found = box.call("search", {"pattern": "greet"})
    assert found == "src/app.py:1: def greet():"  # node_modules is skipped
    assert box.call("edit_file", {"path": "src/app.py", "old_string": "'hi'", "new_string": "'hello'"})[0]
    assert "'hello'" in (project / "src" / "app.py").read_text()
    ok, why = box.call("edit_file", {"path": "src/app.py", "old_string": "nope", "new_string": "x"})
    assert not ok and "isn't in the file" in why
    assert box.call("write_file", {"path": "notes/todo.md", "content": "a\nb\n"})[1] == "Created notes/todo.md (2 lines)."
    ok, out = box.call("run_command", {"command": "ls notes && exit 3"})
    assert ok and out.startswith("Exit code 3") and "todo.md" in out
    # Other places on the machine too (personal use: usability first)
    elsewhere = project.parent / "notes.txt"
    elsewhere.write_text("from elsewhere\n")
    ok, text = box.call("read_file", {"path": str(elsewhere)})
    assert ok and "from elsewhere" in text
    # ... to read: changes stay in the project, like commands (in its container)
    ok, why = box.call("write_file", {"path": str(elsewhere), "content": "x"})
    assert not ok and "Only files in the project folder" in why and elsewhere.read_text() == "from elsewhere\n"


def test_commands_go_through_the_shell_it_was_given(project, tmp_path):
    shell = tmp_path / "shell"
    shell.write_text('#!/bin/sh\necho "via relay: $2"\n')
    shell.chmod(0o755)
    box = Toolbox(project, code=True, shell=shell)
    assert box.call("run_command", {"command": "ls"}) == (True, "Exit code 0\nvia relay: ls")
    assert box.call("read_file", "not json")[1].startswith("The arguments aren't valid JSON")
    assert box.call("nope", {})[1] == "There's no tool 'nope'."


def test_web_pages_and_skills(project):
    class Page(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            body = b"<html><head><style>x{}</style></head><body><h1>Hello</h1><script>no()</script><p>World</p></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    box = Toolbox(project, code=False)
    ok, text = box.call("web_fetch", {"url": f"http://127.0.0.1:{server.server_address[1]}/"})
    server.shutdown()
    assert ok and "Hello" in text and "World" in text and "no()" not in text and "x{}" not in text
    assert not box.call("web_fetch", {"url": "file:///etc/passwd"})[0]
    assert "relay-subagents:" in box.call("list_skills", {})[1]
    assert box.call("read_skill", {"name": "relay-subagents"})[1].startswith("---\nname: relay-subagents")


def test_the_users_computers_are_tools_only_when_there_are_some(tmp_path):
    with_pc = Toolbox(tmp_path, code=False, relay_url="http://relay/mcp/x", computers=["pc"])
    names = {d["function"]["name"] for d in with_pc.definitions()}
    assert {"computers", "computer_run", "computer_read_file", "computer_write_file"} <= names
    assert "(pc)" in with_pc.guidance()
    without = Toolbox(tmp_path, code=False, relay_url="http://relay/mcp/x")
    assert "computer_run" not in {d["function"]["name"] for d in without.definitions()}


def test_a_command_that_leaves_something_running_doesnt_hang(project):
    import time
    box = Toolbox(project, code=True)
    started = time.monotonic()
    ok, output = box.call("run_command", {"command": "sleep 30 & echo started", "timeout": 1})
    assert ok and output.startswith("Stopped after 1 s") and "started" in output
    assert time.monotonic() - started < 10
    threading.Timer(0.5, box.stop).start()
    ok, output = box.call("run_command", {"command": "sleep 30"})
    assert output.startswith("Stopped") and time.monotonic() - started < 15


def test_a_computer_task_tells_the_model_where_to_work(tmp_path):
    from relay.tools_script import note
    box = Toolbox(tmp_path, code=False, relay_url="http://relay/mcp/x", computers=["pc", "nas"], focus="pc")
    assert 'computer "pc": the work happens there' in box.guidance()
    told = note(tmp_path / "relay", computers=["pc"], focus="pc")
    assert "without --on" in told and 'computer "pc"' in told


def test_the_note_names_one_route(tmp_path):
    from relay.tools_script import note
    script = tmp_path / "tools" / "relay"
    via_mcp = note(script, via_mcp=True)
    assert 'MCP server "relay"' in via_mcp and str(script) not in via_mcp
    assert "Relay's delegate for another provider or model" in via_mcp
    told = note(script)
    assert "`relay help`" in told and str(script) not in told and "MCP" not in told
    assert f"`{script} help`" in note(script, on_path=False)  # no containers: no `relay` on a PATH


def test_the_note_is_short_and_help_has_the_rest():
    from relay.tools_script import help_text, note
    told = note(Path("/x/relay"), ["code-review", "relay-subagents"], ["pc"], code=True, repository="/home/me/p")
    assert len(told) < 1200, len(told)
    commands = help_text(None, computers=["pc"], code=True)
    assert "delegate …" in commands and "merge ID" in commands and "run --on NAME" in commands
    assert "container, task, subagents, computers, skills" in commands
    assert "merge ID" not in help_text(None) and "computers" not in help_text(None).split("More:")[0]
    assert "Debian container" in help_text("container") and "never commits" in help_text("task", code=True)
    assert "No help on 'x'" in help_text("x")
    # Every topic renders (a brace in a JSON example once broke AgentOS's with .format).
    from relay.tools_script import HELP_TOPICS
    for topic in HELP_TOPICS:
        assert help_text(topic, computers=["pc"], code=True)
    assert '[{"provider": P' in help_text("agentos")


def test_the_note_mentions_a_larger_repository_only_when_there_is_one(tmp_path):
    from relay.tools_script import note
    assert "larger git repository" not in note(tmp_path / "relay")
    told = note(tmp_path / "relay", repository="/home/me/projects")
    assert "larger git repository (/home/me/projects)" in told and "git add -- ." in told


def test_agents_search_with_relays_search_first():
    """Found in the user's chats: twice they asked an agent to use Relay's search tools ("Use Relay search tools
    please"). The note, and a subagent's, say so where Relay's search is on."""
    from relay.tools_script import note, search_note
    assert "Relay's web search first" in note(Path("/x/relay"), search=True)
    assert "Relay's web search first" not in note(Path("/x/relay"), search=False)
    assert "search with it first" in search_note() and "search with it first" not in search_note(search=False)

