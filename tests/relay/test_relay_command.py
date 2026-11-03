"""`relay` in a WorkBox (container/bin/relay): runs the conversation's tools script."""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from relay.containers import BIN

RELAY = BIN / "relay"


def run(args, env, cwd):
    return subprocess.run([sys.executable, str(RELAY), *args], env={**os.environ, **env}, cwd=cwd,
                          capture_output=True, text=True, timeout=30)


def test_relay_runs_the_tools_script_it_is_given_or_finds(tmp_path):
    # An agent on the machine: a script of its own
    script = tmp_path / "tools" / "relay"
    script.parent.mkdir()
    script.write_text("import sys\nprint('tools:', *sys.argv[1:])\n")
    env = {"RELAY_TOOLS": str(script)}
    assert run(["agents"], env, tmp_path).stdout == "tools: agents\n"

    asked = []

    class Relay(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            asked.append(self.path)
            known = "project" in self.path and "key=signed" in self.path
            self.send_response(200 if known else 404)
            self.end_headers()
            self.wfile.write(json.dumps({"url": "http://relay/mcp/TOKEN"} if known else {"error": "No agent of Relay "
                                        "is working in this folder now"}).encode())
    server = ThreadingHTTPServer(("127.0.0.1", 0), Relay)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        # In a WorkBox: the generic script next to Relay's address, run for the conversation's address
        public = tmp_path / "public"
        public.mkdir()
        url_file = public / "relay-url"
        url_file.write_text(f"http://127.0.0.1:{server.server_port}\n")
        (public / "relay-tools").write_text("import os, sys\nprint(os.environ['RELAY_MCP_URL'], *sys.argv[1:])\n")
        project = tmp_path / "project"
        project.mkdir()
        env = {"RELAY_TOOLS": "", "RELAY_URL_FILE": str(url_file), "RELAY_MCP_URL": "http://relay/mcp/MINE"}
        assert run(["agents"], env, project).stdout == "http://relay/mcp/MINE agents\n"  # Claude Code's, say
        # opencode's shared service: by the folder, which the box has signed
        env = {"RELAY_TOOLS": "", "RELAY_MCP_URL": "", "RELAY_URL_FILE": str(url_file), "RELAY_BOX": str(project),
               "RELAY_BOX_KEY": "signed"}
        assert run(["help", "task"], env, project).stdout == "http://relay/mcp/TOKEN help task\n"
        assert asked[-1].startswith("/tools-for?folder=") and "project" in asked[-1] and "box=" in asked[-1]
        other = run(["agents"], {**env, "RELAY_BOX_KEY": "forged"}, project)
        assert other.returncode != 0 and "No agent of Relay is working in this folder" in other.stderr
    finally:
        server.shutdown()
    unknown = run(["agents"], {"RELAY_TOOLS": "", "RELAY_MCP_URL": "", "RELAY_URL_FILE": str(tmp_path / "nope")},
                  tmp_path)
    assert unknown.returncode != 0 and "Relay's address isn't known" in unknown.stderr
