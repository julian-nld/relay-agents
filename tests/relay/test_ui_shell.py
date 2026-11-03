"""Real Chromium + a real scratch WorkBox; no models or user conversations."""

import json
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from relay import config, git
from relay.app import create_app
from relay.containers import Containers, built_image
from relay.db import Database
from relay.providers import Registry
from relay.providers.base import ProviderRun


def test_work_container_shell_in_chromium(tmp_path, monkeypatch):
    if not shutil.which("podman"):
        pytest.skip("Chromium/container shell check needs podman")
    registry = Registry(tmp_path / "providers")
    registry.add({"id": "fake", "name": "Test agent", "kind": "command", "options": {"command": ["true"]}})
    monkeypatch.setattr(registry, "ensure_defaults", lambda: None)
    project = tmp_path / "project"
    project.mkdir()
    (project / "file.txt").write_text("original\n")
    git.run(project, "init", "-b", "main")
    git.run(project, "add", ".")
    git.run(project, "commit", "-m", "Initial", identity=True)
    settings = config.Settings(chat_root=str(tmp_path / "chats"), workspace_roots=[str(project)])
    app = create_app(settings, Database(tmp_path / "relay.db"), registry)
    class Run(ProviderRun):
        def __init__(self):
            self.stopped = threading.Event()

        def events(self):
            yield {"type": "text", "delta": "The agent stays connected while you use the shell."}
            self.stopped.wait(120)
            yield {"type": "done", "status": "interrupted"}

        def interrupt(self):
            self.stopped.set()

    provider = registry.provider("fake")
    monkeypatch.setattr(provider, "start", lambda context, prompt: Run())
    monkeypatch.setattr(registry, "provider", lambda _: provider)
    containers = Containers(state=tmp_path / "containers")
    # No provider boxes/model calls: only this test's project is given a real WorkBox.
    app.state.relay.containers = containers
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", ws="wsproto"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(.02)
        assert server.started
        result = subprocess.run(["podman", "run", "--rm", "-i", "--network=host", "--timeout=255", built_image(), "node", "-e", BROWSER_CHECK],
                                input=json.dumps({"base": f"http://127.0.0.1:{sock.getsockname()[1]}", "project": str(project)}),
                                text=True, capture_output=True, timeout=240, check=False)
        assert result.returncode == 0, result.stdout + result.stderr
        # The scratch chat's shell is kept, its page gone (the task's was ended); Relay stopping ends it
        assert list(app.state.relay.shells.kept) == [next(row["id"] for row in app.state.relay.db.all(
            "SELECT id, title FROM conversations") if row["title"] == "Chat scratch")]
        server.should_exit = True
        thread.join(15)
        assert not app.state.relay.terminals and not containers.terminals
        rows = app.state.relay.db.all("SELECT * FROM conversations")
        isolated = next(row for row in rows if row["git_mode"] == "worktree")
        assert (project / "file.txt").read_text() == "original\n"
        assert not (project / "shell-created.txt").exists()
        assert (Path(isolated["directory"]) / "shell-created.txt").read_text() == "created from shell\n"
    finally:
        server.should_exit = True
        thread.join(15)
        sock.close()
        # Only the test's containers/volumes, including worktrees and newly created chat folders.
        for row in app.state.relay.db.all("SELECT directory FROM conversations"):
            containers.discard(Path(row["directory"]))
        assert not thread.is_alive()


BROWSER_CHECK = r"""
const fs = require('node:fs');
const assert = require('node:assert/strict');
const { chromium } = require('/usr/local/lib/node_modules/playwright-core');
const { base, project } = JSON.parse(fs.readFileSync(0, 'utf8'));
(async () => {
  const browser = await chromium.launch({ executablePath: '/usr/bin/chromium', args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    page.setDefaultTimeout(15000);
    const errors = [], csp = [];
    page.on('pageerror', (error) => errors.push(error.message));
    page.on('console', (message) => { if (/Content Security Policy|violates.*directive/.test(message.text())) csp.push(message.text()); });
    await page.addInitScript(() => {
      window.shellOutput = '';
      window.shellSizes = [];
      const Native = window.WebSocket;
      window.WebSocket = class extends Native {
        send(data) {
          const message = JSON.parse(data);
          if (message.type === 'resize') window.shellSizes.push(message);
          super.send(data);
        }
        constructor(...args) {
          super(...args);
          const decode = new TextDecoder();
          this.addEventListener('message', (event) => {
            if (event.data instanceof ArrayBuffer) window.shellOutput += decode.decode(event.data, { stream: true });
          });
          window.shellSocket = this;
        }
      };
    });
    await page.goto(base + '/');
    await page.getByPlaceholder('Username').fill('shell-test');
    await page.getByPlaceholder('Password', { exact: true }).fill('correct horse');
    await page.getByPlaceholder('Repeat password', { exact: true }).fill('correct horse');
    await page.getByRole('button', { name: 'Create account', exact: true }).click();
    await page.getByLabel('Message', { exact: true }).waitFor();
    const api = async (method, path, body) => {
      const response = await page.request.fetch(base + path, { method, data: body, headers: { 'x-relay-request': '1', 'x-relay-client': 'web' } });  // (as Relay's pages: the user's own)
      assert.equal(response.ok(), true, await response.text());
      return response.json();
    };
    const chat = await api('POST', '/api/conversations', { provider_id: 'fake', title: 'Scratch shell', mode: 'code', directory: project, isolate: true });
    const running = await api('POST', '/api/conversations/' + chat.id + '/messages', { text: 'Background agent', fallback_loop: 'off' });
    await page.goto(base + '/c/' + chat.id);
    const composer = page.getByLabel('Message', { exact: true });
    await composer.fill('Keep this composer draft');
    await page.locator('.composer input[type=file]').setInputFiles({ name: 'draft.txt', mimeType: 'text/plain', buffer: Buffer.from('draft attachment') });
    await page.locator('.composer .attachment .remove').waitFor();
    await page.locator('.composer-options summary').click();
    await page.evaluate(async () => {
      const { state } = await import('/static/js/state.js');
      window.originalComposer = document.querySelector('.composer textarea');
      window.originalStream = state.following.source;
    });
    // Making a WorkBox (the first shell of a folder) takes a while when the machine is busy (the suite in parallel).
    const making = { timeout: 60000 };
    const open = async (options) => {
      await page.locator('.composer').getByRole('button', { name: 'Shell', exact: true }).click();
      await page.waitForFunction(() => /^(Connected|Reconnected) · /.test(document.querySelector('.shell-status').textContent), null, options);
      await page.waitForFunction(() => document.activeElement.matches('.xterm-helper-textarea'));
    };
    const type = async (text) => {
      await page.locator('.xterm-helper-textarea').focus();
      await page.keyboard.type(text);
      await page.keyboard.press('Enter');
    };
    const output = async (needle) => {
      try { await page.waitForFunction((needle) => window.shellOutput.includes(needle), needle); }
      catch (error) { throw new Error(error.message + '\nOutput: ' + await page.evaluate(() => window.shellOutput)); }
    };
    await open(making);
    assert.equal(await page.locator('.shell-directory').innerText(), chat.directory);
    assert.notEqual(chat.directory, project, 'isolated tasks open their worktree, not the source repository');
    const container = await page.locator('.shell-status').innerText();
    await type('printf "\\nDIR=%s\\n" "$PWD"; test -f /run/.containerenv && printf "in-container\\n"; printf "created from shell\\n" > shell-created.txt');
    await output('in-container\r\n');
    await output('DIR=' + chat.directory + '\r\n');
    await type('cat shell-created.txt');
    await output('created from shell\r\n');
    await type('export KEPT_SHELL=yes; printf "café\\n"');
    await output('caf\u00e9\r\n');
    // Completion goes to Bash, not Relay's slash menu; Escape belongs to shell programs.
    await page.keyboard.type('cat shell-cre');
    await page.keyboard.press('Tab');
    await page.keyboard.press('Enter');
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('.bottom-panel:not([hidden])').count(), 1);
    await page.keyboard.press('Control+g');  // (to readline that Escape began Meta-…: the kept shell would keep it)
    // Closed and opened again: the same shell (what it showed, its variables), in the same container; the shell
    // doesn't enter chat history.
    await page.getByRole('button', { name: 'Close the shell', exact: true }).click();
    assert.equal(await page.evaluate(() => document.activeElement.matches('.composer textarea')), true);
    assert.equal(await composer.inputValue(), 'Keep this composer draft');
    assert.equal(await page.evaluate(() => window.originalComposer === document.querySelector('.composer textarea')), true);
    assert.match(await page.locator('.composer .attachment').innerText(), /draft.txt/);
    assert.equal(await page.locator('.composer-options').getAttribute('open'), '');
    assert.equal((await api('GET', '/api/conversations/' + chat.id)).messages.length, 1);
    assert.equal(await page.evaluate(async () => {
      const { state } = await import('/static/js/state.js'); return state.following.source === window.originalStream;
    }), true);
    await open();
    assert.equal(await page.locator('.shell-status').innerText(), container.replace('Connected', 'Reconnected'));
    await page.waitForFunction(() => document.querySelector('.bottom-panel .xterm-rows').textContent.includes('café'));
    await type('printf "kept=%s\\n" "$KEPT_SHELL"');
    await output('kept=yes\r\n');
    await type('stty size; printf "initial-size-done\\n"');
    await output('initial-size-done\r\n');
    const size = async () => {
      const text = await page.evaluate(() => window.shellOutput);
      const match = [...text.matchAll(/(\d+) (\d+)\r+\n/g)].at(-1);
      assert.ok(match, text);
      return match.slice(1).map(Number);
    };
    const initial = await size();
    // Mobile resize is delivered to the PTY; touch-friendly keys are also useful without a keyboard.
    await page.setViewportSize({ width: 320, height: 844 });
    await page.waitForFunction(() => document.querySelector('.xterm-screen').getBoundingClientRect().width <= 304);  // (the panel's, less its margins)
    await page.waitForFunction(() => window.shellSizes.at(-1).cols < 50);
    // podman passes the new size on to the box's terminal by itself, a moment later when busy
    let phone = initial;
    for (let tries = 0; tries < 20 && phone[1] >= initial[1]; tries++) {
      if (tries) await page.waitForTimeout(250);
      await type(`stty size; printf "phone-size-done-${tries}\\n"`);
      await output(`phone-size-done-${tries}\r\n`);
      phone = await size();
    }
    const resizing = await page.evaluate(() => window.shellOutput);
    if (resizing.includes('Error resizing exec session') && resizing.includes('openByHandleAt failed: operation not permitted')) {
      console.log('ENVIRONMENT: podman opens the real PTY but this host denies OCI resize (openByHandleAt). Frontend resize delivery checked; PTY resize covered separately.');
    } else assert.ok(phone[1] < initial[1] && phone[1] > 10, JSON.stringify({ initial, phone, resizing }));
    assert.equal(await page.evaluate(() => document.querySelector('.bottom-panel').scrollWidth <= document.querySelector('.bottom-panel').clientWidth), true);
    assert.equal(await page.evaluate(() => document.scrollingElement.scrollWidth <= innerWidth), true);
    // (a Ctrl+C before sleep has the terminal misses it: it sleeps on, longer than output() waits)
    await type('printf "sleeping\\n"; sleep 30');
    await output('sleeping\r\n');
    await page.waitForTimeout(250);
    await page.getByRole('button', { name: 'Ctrl+C', exact: true }).click();
    await type('printf "after-interrupt\\n"');
    await output('after-interrupt\r\n');
    // Exercise the real container's guard with a harmless stub, never its real rm executable.
    await type('rm() { printf "STUB_RM_EXECUTED\\n"; }');
    await type('rm -rf /');
    await output('Relay blocked this command:');
    assert.equal((await page.evaluate(() => window.shellOutput)).includes('STUB_RM_EXECUTED\r\n'), false);
    await type('unset -f rm; printf "guard-finished\\n"');
    await output('guard-finished\r\n');
    await type('nano shell-created.txt');
    await output('\x1b[?1049h');  // its full screen (at phone width its title leaves "GNU nano" out)
    await page.keyboard.press('Control+x');
    await output('\x1b[?1049l');
    await type('printf "editor-finished\\n"');
    await output('editor-finished\r\n');
    await type('exit');
    await page.waitForFunction(() => document.querySelector('.shell-status').textContent === 'Shell exited (0)');
    await page.getByRole('button', { name: 'New shell', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('.shell-status').textContent.startsWith('Connected'));
    await type('stty size; printf "reopened\\n"');
    await output('reopened\r\n');
    assert.equal((await size())[1], (await page.evaluate(() => window.shellSizes.at(-1).cols)), 'opening on a phone gets its correct initial PTY size');
    await page.getByRole('button', { name: 'Close the shell', exact: true }).click();
    assert.equal(await composer.inputValue(), 'Keep this composer draft');
    assert.equal((await api('GET', '/api/conversations/' + chat.id)).running_run, running.run_id);
    await api('POST', '/api/runs/' + running.run_id + '/stop');
    await page.setViewportSize({ width: 1280, height: 900 });
    // /shell is a Relay action, not a model turn, and works with a chat's scratch container too.
    const scratch = await api('POST', '/api/conversations', { provider_id: 'fake', title: 'Chat scratch' });
    await page.goto(base + '/c/' + scratch.id);
    await composer.fill('/shell');
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => document.querySelector('.shell-status').textContent.startsWith('Connected'), null, making);
    await type('printf "scratch=%s\\n" "$PWD"');
    await output('scratch=' + scratch.directory + '\r\n');
    // The panel follows the chat that's open: another one's WorkBox; on a page, where a shell is
    const go = (path) => page.evaluate(async (path) => { const { navigate } = await import('/static/js/main.js'); navigate(path); }, path);
    const before = await page.evaluate(() => window.shellSizes.length);
    await go('/c/' + chat.id);
    await page.waitForFunction((dir) => document.querySelector('.bottom-panel .shell-directory').textContent === dir
      && document.querySelector('.bottom-panel .shell-status').textContent.startsWith('Reconnected'), chat.directory, making);
    // Made on a phone, opened again on a desktop: what it showed is written at the phone's size, then wrapped anew
    // at this window's (it came out wrapped wrong before)
    await page.waitForFunction((before) => window.shellSizes.length > before && window.shellSizes.at(-1).cols > 60, before);
    const reopened = await page.evaluate((before) => window.shellSizes.slice(before).map((s) => s.cols), before);
    const narrow = reopened.findIndex((cols) => cols < 50);  // (the scratch chat's shell it left may come first)
    assert.ok(narrow >= 0 && reopened.slice(narrow + 1).some((cols) => cols > 60) && reopened.at(-1) > 60,
      JSON.stringify(reopened));
    await type('printf "after-new=%s\\n" "${KEPT_SHELL:-unset}"');  // (the one New shell made, kept since)
    await output('after-new=unset\r\n');
    // End shell ends it; New shell is then a new one
    await page.getByRole('button', { name: 'End shell', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('.bottom-panel .shell-status').textContent === 'Shell ended');
    assert.equal(await page.getByRole('button', { name: 'New shell', exact: true }).isVisible(), true);
    await go('/tasks');
    await page.locator('.bottom-panel .bottom-empty').waitFor();
    // The bar's Shell closes it (and opens it again)
    const barShell = page.locator('.activity-bar').getByRole('button', { name: 'Shell', exact: true });
    assert.equal(await barShell.getAttribute('aria-pressed'), 'true');
    await barShell.click();
    assert.equal(await page.locator('.bottom-panel').isHidden(), true);
    assert.equal(await barShell.getAttribute('aria-pressed'), 'false');
    assert.deepEqual((await api('GET', '/api/conversations/' + scratch.id)).messages, []);
    assert.deepEqual(errors, []);
    assert.deepEqual(csp, []);
    console.log('PASS: real WorkBox/worktree, PTY, ANSI/Unicode, completion, Ctrl+C, nano, resize at 320px, the same shell reopened, End shell, drafts/uploads/Options, focus, /shell and chat scratch, the panel following the open chat');
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exit(1); });
"""
