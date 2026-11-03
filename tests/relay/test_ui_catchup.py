"""Opening a chat while its agent works: what it did so far shows at once, as a finished turn does (not
replayed on screen step by step, each opening and closing animated), with the cursor only where it's
still writing."""

import json
import shutil
import socket
import subprocess
import sys
import threading
import time

import pytest
import uvicorn

from relay import config
from relay.app import create_app
from relay.containers import built_image
from relay.db import Database
from relay.providers import Registry

# An agent that says something, reads and runs a command, says more, then works on until it's stopped.
AGENT = """
import json, sys, time
sys.stdin.read()
def emit(**e): print(json.dumps(e), flush=True)
emit(type="session", session="s1")
emit(type="text", delta="I ask three agents to look: one for it, a critic, and one who weighs it up.")
emit(type="tool_call", id="t1", name="Read", input={"file_path": "notes.md"})
emit(type="tool_result", id="t1", name="Read", ok=True, output="notes")
emit(type="tool_call", id="t2", name="Bash", input={"command": "ls"})
emit(type="tool_result", id="t2", name="Bash", ok=True, output="notes.md")
emit(type="text", delta="They looked into it, and")
time.sleep(120)
"""


def test_opening_a_chat_while_its_agent_works_in_chromium(tmp_path, monkeypatch):
    if not shutil.which("podman"):
        pytest.skip("Chromium UI checks need podman")
    (tmp_path / "agent.py").write_text(AGENT)
    registry = Registry(tmp_path / "providers")
    registry.add({"id": "busy", "name": "Busy agent", "kind": "command",
                  "options": {"command": [sys.executable, str(tmp_path / "agent.py")]}})
    monkeypatch.setattr(registry, "ensure_defaults", lambda: None)
    app = create_app(config.Settings(chat_root=str(tmp_path / "chats"), workspace_roots=[str(tmp_path)]),
                     Database(tmp_path / "relay.db"), registry)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 30  # (a busy machine: the suite in parallel)
        while not server.started and time.monotonic() < deadline:
            time.sleep(.02)
        assert server.started
        result = subprocess.run(["podman", "run", "--rm", "-i", "--network=host", "--timeout=90", built_image(),
                                 "node", "-e", BROWSER_CHECK],
                                input=json.dumps({"base": f"http://127.0.0.1:{sock.getsockname()[1]}"}),
                                text=True, capture_output=True, timeout=75, check=False)
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        for run_id in list(app.state.relay.runs.active):
            app.state.relay.runs.stop(run_id)
        server.should_exit = True
        thread.join(10)
        sock.close()
        assert not thread.is_alive()


BROWSER_CHECK = r"""
const assert = require('node:assert/strict');
const { chromium } = require('/usr/local/lib/node_modules/playwright-core');
const { base } = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
(async () => {
  const browser = await chromium.launch({ executablePath: '/usr/bin/chromium', args: ['--no-sandbox'] });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage(); page.setDefaultTimeout(15000);
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  async function api(method, path, data) {
    const response = await context.request.fetch(base + path, { method, data, headers: { 'X-Relay-Request': '1', 'X-Relay-Client': 'web' } });  // (as Relay's pages: the user's own)
    assert.ok(response.ok(), await response.text()); return response.json();
  }
  try {
    await api('POST', '/api/setup', { username: 'me', password: 'correct horse' });
    const conv = await api('POST', '/api/conversations', { provider_id: 'busy' });
    const { run_id: runId } = await api('POST', `/api/conversations/${conv.id}/messages`, { text: 'What do you think?' });
    await new Promise((resolve) => setTimeout(resolve, 2000));  // (it has said and done its part, and works on)
    // Every frame from the start: the live turn on screen, and transitions that start in it
    await page.addInitScript(() => {
      window.seen = { status: 0, open: 0, transitions: [] };
      document.addEventListener('transitionstart', (event) => {
        if (event.target.closest?.('.msg.assistant')) window.seen.transitions.push(`${event.target.className} ${event.propertyName}`);
      }, true);
      const look = () => {
        for (const turn of document.querySelectorAll('.msg.assistant')) {
          if (turn.hidden || !turn.isConnected) continue;
          if (turn.querySelector('.status-line')) window.seen.status++;
          if (turn.querySelector('details.tool-activity[open]')) window.seen.open++;
        }
        requestAnimationFrame(look);
      };
      requestAnimationFrame(look);
    });
    await page.goto(`${base}/c/${conv.id}`);
    const turn = page.locator('.msg.assistant').last();
    await turn.getByText('They looked into it, and').waitFor();
    await page.waitForTimeout(600);
    const seen = await page.evaluate(() => window.seen);
    assert.equal(seen.status, 0, 'no "Working…" flashed by');
    assert.equal(seen.open, 0, 'no activity opened and closed on screen');
    assert.deepEqual(seen.transitions, []);
    assert.equal(await turn.locator('details.tool-activity').count(), 1);
    assert.match(await turn.locator('.activity-summary').innerText(), /1 file read · 1 command/);
    // The cursor: only where it's still writing, not after the text before the activity
    const marks = await turn.locator('.md').evaluateAll((blocks) => blocks.map((md) => md.querySelectorAll('.cursor-mark').length));
    assert.deepEqual(marks, [0, 1]);
    await api('POST', `/api/runs/${runId}/stop`);
    await turn.getByText('Stopped.').waitFor();
    assert.equal(await turn.locator('.cursor-mark').count(), 0);
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
