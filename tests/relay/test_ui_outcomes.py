"""Review and recovery actions in real Chromium, without a real model or user data."""

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
from relay.containers import built_image
from relay.db import Database
from relay.providers import Registry
from relay.providers.base import ProviderRun


def test_review_and_recovery_in_chromium(tmp_path, monkeypatch):
    if not shutil.which("podman"):
        pytest.skip("Chromium UI checks need podman")
    registry = Registry(tmp_path / "providers")
    attempts = {}

    class Run(ProviderRun):
        def __init__(self, context, prompt):
            self.context, self.prompt = context, prompt
            self.stopped = threading.Event()
            attempts[context.conversation_id] = attempts.get(context.conversation_id, 0) + 1
            self.attempt = attempts[context.conversation_id]

        def events(self):
            yield {"type": "step", "model": self.context.model}
            if self.context.model == "test/fallback-failed":
                yield {"type": "error", "message": "The original model stopped without answering."}
                yield {"type": "done", "status": "failed"}
                return
            if self.context.model == "test/failed" or (self.prompt == "limited" and self.attempt == 1):
                yield {"type": "permission", "decision": "once", "action": "read", "resources": ["fixture.txt"]}
                yield {"type": "error", "message": "Usage limit reached; try again in 2 hours"}
                yield {"type": "done", "status": "failed"}
                return
            if self.prompt == "change":
                Path(self.context.directory, "file.txt").write_text("changed\n")
            if self.prompt == "wait" or self.context.model == "test/fallback-wait":
                yield {"type": "text", "delta": "Partial answer before stopping."}
                assert self.stopped.wait(60)
                yield {"type": "done", "status": "interrupted"}
                return
            yield {"type": "text", "delta": "Your task is complete."}
            yield {"type": "usage", "billing": "free", "cost": 0}
            yield {"type": "done", "status": "succeeded"}

        def interrupt(self):
            self.stopped.set()

    providers = {}
    for provider_id in ("fake", "rewind", "backup"):
        options = {"base_url": "http://unused.invalid"} if provider_id == "rewind" else {"command": ["true"]}
        registry.add({"id": provider_id, "name": provider_id,
                      "kind": "openai-compatible" if provider_id == "rewind" else "command",
                      "options": {**options, "models": ["test/working", "test/failed"]}})
        provider = registry.provider(provider_id)
        providers[provider_id] = provider
        monkeypatch.setattr(provider, "start", lambda context, prompt: Run(context, prompt))
        if provider_id == "rewind":
            monkeypatch.setattr(provider, "REWINDABLE", True)
    monkeypatch.setattr(registry, "provider", lambda provider_id: providers[provider_id])
    monkeypatch.setattr(registry, "ensure_defaults", lambda: None)
    project = tmp_path / "project"
    project.mkdir()
    (project / "file.txt").write_text("original\n")
    git.run(project, "init", "-b", "main")
    git.run(project, "add", ".")
    git.run(project, "commit", "-m", "Initial", identity=True)
    plain = tmp_path / "plain"
    plain.mkdir()
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
        result = subprocess.run(["podman", "run", "--rm", "-i", "--network=host", "--timeout=90", built_image(), "node", "-e", BROWSER_CHECK],
                                input=json.dumps({"base": f"http://127.0.0.1:{sock.getsockname()[1]}",
                                                  "project": str(project), "plain": str(plain)}),
                                text=True, capture_output=True, timeout=75, check=False)
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()
        assert not thread.is_alive()


BROWSER_CHECK = r"""
const assert = require('node:assert/strict');
const { chromium } = require('/usr/local/lib/node_modules/playwright-core');
const { base, project, plain } = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
(async () => {
  const browser = await chromium.launch({ executablePath: '/usr/bin/chromium', args: ['--no-sandbox'] });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage(); page.setDefaultTimeout(10000);
  const errors = []; page.on('pageerror', (error) => errors.push(error.message));
  async function api(method, path, data) {
    const response = await context.request.fetch(base + path, { method, data, headers: { 'X-Relay-Request': '1', 'X-Relay-Client': 'web' } });  // (as Relay's pages: the user's own)
    assert.ok(response.ok(), await response.text()); return response.json();
  }
  async function chat(prompt, data = {}) {
    const conv = await api('POST', '/api/conversations', { provider_id: 'fake', model: 'test/working', fallback_loop: 'off', ...data });
    await api('POST', '/api/conversations/' + conv.id + '/messages', { text: prompt });
    await page.goto(base + '/c/' + conv.id);
    return conv;
  }
  async function completed() {
    await page.waitForFunction(() => document.querySelector('.header .activity-badge')?.textContent === 'Completed');
    await page.locator('.thread > .msg.assistant:last-child .msg-actions').getByRole('button', { name: 'More', exact: true }).waitFor();
  }
  async function draft() {
    await page.getByLabel('Message', { exact: true }).fill('Unsent draft for later');
    await page.locator('.composer input[type=file]').setInputFiles({ name: 'later.txt', mimeType: 'text/plain', buffer: Buffer.from('keep this attachment') });
    await page.locator('.composer .attachment .remove').waitFor();
    await page.locator('.composer-options summary').click();
    await page.evaluate(() => { window.keptComposer = document.querySelector('.composer'); });
  }
  async function kept() {
    assert.equal(await page.evaluate(() => window.keptComposer === document.querySelector('.composer')), true);
    assert.equal(await page.getByLabel('Message', { exact: true }).inputValue(), 'Unsent draft for later');
    assert.equal(await page.locator('.composer .attachment .name').innerText(), 'later.txt');
    assert.equal(await page.locator('.composer-options').getAttribute('open'), '');
  }
  async function fits(selectors) {
    assert.equal(await page.evaluate(() => document.scrollingElement.scrollWidth > innerWidth), false);
    for (const selector of selectors) {
      const box = await page.evaluate((selector) => {
        const el = [...document.querySelectorAll(selector)].at(-1);
        const rect = el.getBoundingClientRect(); return { x: rect.x, width: rect.width, height: rect.height };
      }, selector);
      assert.ok(box.height > 0 && box.x >= 0 && box.x + box.width <= page.viewportSize().width + .5,
        selector + ' overflows: ' + JSON.stringify({ box, errors, path: new URL(page.url()).pathname }));
    }
  }
  try {
    await api('POST', '/api/setup', { username: 'ui', password: 'test-password' });
    const isolated = await chat('change', { mode: 'code', directory: project, isolate: true });
    await page.locator('.turn-review').waitFor();
    assert.equal(await page.locator('.review-location').innerText(), 'On a separate branch');
    await page.locator('.turn-changes').click();
    await page.locator('.change-file').waitFor();
    assert.match(await page.locator('.changes-location').innerText(), /separate branch.*merge.*main/s);
    assert.equal(await page.locator('.changes-foot').getByRole('button', { name: 'Merge into main' }).count(), 1);
    await page.keyboard.press('Escape');
    const direct = await chat('change', { mode: 'code', directory: project });
    await page.locator('.turn-review').waitFor();
    assert.equal(await page.locator('.review-location').innerText(), 'Already in your project');
    await draft();
    await page.locator('.turn-changes').click();
    await page.locator('.change-file').waitFor();
    assert.match(await page.locator('.changes-location').innerText(), /does not apply or commit/);
    await page.locator('.change-head').click();
    assert.equal(await page.locator('.change-head').getAttribute('aria-expanded'), 'true');
    assert.match(await page.locator('.diff').innerText(), /changed/);
    await page.getByRole('button', { name: 'Commit…', exact: true }).click();
    await page.getByLabel('Message', { exact: true }).last().waitFor();
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('.changes-panel').count(), 1, 'Escape closes only the nested commit dialog');
    for (const width of [390, 320]) {
      await page.setViewportSize({ width, height: 844 });
      for (const theme of ['light', 'dark']) {
        await page.evaluate(async (theme) => { const { setTheme } = await import('/static/js/theme.js'); setTheme(theme); }, theme);
        await fits(['.changes-panel', '.changes-foot']);
        for (const box of await page.locator('.changes-foot .btn').evaluateAll((buttons) => buttons.map((button) => ({ left: button.getBoundingClientRect().left, right: button.getBoundingClientRect().right })))) {
          assert.ok(box.left >= 0 && box.right <= width + .5);
        }
      }
    }
    await page.keyboard.press('Escape'); await kept();
    await fits(['.header .activity-badge', '.turn-changes', '.msg-actions', '.composer']);
    // (on a phone the header's actions are in a strip that scrolls sideways: Review changes is a scroll away)
    // (scrolled to and measured in one go: the header is rendered anew as the page polls)
    const changes = await page.evaluate(() => {
      const button = document.querySelector('.header .changes-btn');
      button.scrollIntoView({ block: 'nearest', inline: 'nearest' });
      const box = button.getBoundingClientRect();
      return { x: box.x, width: box.width, height: box.height };
    });
    assert.ok(changes.height > 0 && changes.x >= 0 && changes.x + changes.width <= page.viewportSize().width + .5,
      JSON.stringify(changes));
    assert.ok((await page.locator('.header').boundingBox()).height <= 53, 'the header stays one row');
    await page.locator('.thread > .msg.assistant .msg-actions').getByRole('button', { name: 'More', exact: true }).click();
    await page.getByRole('menuitem', { name: 'Rewind files to before this answer…', exact: true }).click();
    assert.match(await page.getByRole('dialog', { name: 'Rewind the files?' }).innerText(), /change/);
    await page.keyboard.press('Escape'); await kept();
    await page.locator('.changes-btn').click();
    await page.getByRole('button', { name: 'Mark as reviewed', exact: true }).click();
    await page.locator('.changes-panel').waitFor({ state: 'detached' });  // (its work done: it closes)
    await page.locator('.changes-btn').click();
    await page.locator('.changes-empty').waitFor();
    assert.equal(await page.getByRole('button', { name: 'Mark as reviewed', exact: true }).isDisabled(), true);
    await page.keyboard.press('Escape'); await kept();
    await page.setViewportSize({ width: 1280, height: 900 });
    await chat('change', { mode: 'code', directory: plain });
    await page.locator('.turn-review').waitFor();
    await page.locator('.turn-changes').click();
    await page.locator('.change-file').waitFor();
    assert.match(await page.locator('.changes-location').innerText(), /Already in your project.*checkpoints/s);
    await page.keyboard.press('Escape');
    const failed = await chat('limited');
    const recovery = page.locator('.turn-recovery'); await recovery.waitFor();
    assert.equal(await recovery.evaluate((el) => el.previousElementSibling.classList.contains('run-error')), true,
      'Recovery belongs beside the failure, not an earlier permission note');
    assert.match(await recovery.innerText(), /resume automatically.*replaces that plan/);
    await recovery.getByRole('button', { name: 'Cancel', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('.recovery-notice')?.textContent === 'No automatic retry is scheduled.');
    await recovery.getByRole('button', { name: 'Resume then', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('.recovery-notice')?.textContent.includes('resume automatically'));
    await draft();
    for (const width of [390, 320]) {
      await page.setViewportSize({ width, height: 844 });
      await fits(['.turn-recovery', '.recovery-buttons', '.composer']);
    }
    // Hold a failed reply long enough to test disabled actions and double clicks.
    let release, requests = 0;
    const gate = new Promise((resolve) => { release = resolve; });
    await page.route('**/api/conversations/' + failed.id + '/retry', async (route) => {
      requests++; await gate; await route.fulfill({ status: 502, json: { error: 'Retry temporarily unavailable' } });
    });
    await recovery.getByRole('button', { name: 'Retry', exact: true }).evaluate((button) => { button.click(); button.click(); });
    await page.waitForFunction(() => document.querySelector('.turn-recovery button').disabled);
    assert.equal(await recovery.getByRole('button', { name: 'Choose another model' }).isEnabled(), false);
    release(); await page.getByText('Retry temporarily unavailable', { exact: true }).waitFor();
    assert.equal(requests, 1); await kept();
    await page.unroute('**/api/conversations/' + failed.id + '/retry');
    await recovery.getByRole('button', { name: 'Retry', exact: true }).click();
    await completed(); await kept();
    assert.equal(await page.locator('.turn-recovery').count(), 0);
    const retried = await api('GET', '/api/conversations/' + failed.id);
    assert.equal(retried.resume_at, null);
    assert.equal(retried.messages.at(-2).content, 'limited', 'Retry must not send the composer draft');
    const rewind = await chat('hello', { provider_id: 'rewind' }); await completed(); await draft();
    const actions = page.locator('.thread > .msg.assistant:last-child .msg-actions');
    assert.deepEqual(await actions.locator('button').evaluateAll((buttons) => buttons.map((button) => button.getAttribute('aria-label') || button.textContent.trim())), ['Copy', 'Regenerate', 'More']);
    assert.equal(await actions.evaluate((el) => getComputedStyle(el).opacity), '1');
    await actions.getByRole('button', { name: 'More', exact: true }).click();
    assert.deepEqual(await page.getByRole('menuitem').allTextContents(), ['Fork from here', 'Export chat as Markdown']);
    await page.keyboard.press('Escape');
    await actions.getByRole('button', { name: 'Regenerate', exact: true }).click();
    await completed(); await kept();
    assert.equal((await api('GET', '/api/conversations/' + rewind.id)).messages.length, 2);
    assert.equal(await page.locator('.thread > .msg').count(), 2, 'Fast regeneration must not render its completed turn twice');
    await actions.getByRole('button', { name: 'More', exact: true }).click();
    const [download] = await Promise.all([page.waitForEvent('download'),
      page.getByRole('menuitem', { name: 'Export chat as Markdown', exact: true }).click()]);
    const stream = await download.createReadStream(); const chunks = [];
    for await (const chunk of stream) chunks.push(chunk);
    assert.match(Buffer.concat(chunks).toString(), /hello.*Your task is complete/s);
    await kept();
    await actions.getByRole('button', { name: 'More', exact: true }).click();
    await page.getByRole('menuitem', { name: 'Fork from here', exact: true }).click();
    await page.locator('.thread-note').waitFor();
    assert.match(await page.locator('.thread-note').innerText(), /Forked from/);
    assert.equal(await page.locator('.thread > .msg').count(), 2);
    const stopped = await chat('wait');
    await page.locator('.thread > .msg.assistant .md').waitFor();
    const active = await api('GET', '/api/conversations/' + stopped.id);
    await api('POST', '/api/runs/' + active.running_run + '/stop');
    await page.locator('.turn-recovery').waitFor();
    assert.match(await page.locator('.turn-recovery').innerText(), /Retry.*Choose another model/s);
    const choose = await chat('hello', { model: 'test/failed' });
    await page.locator('.turn-recovery').waitFor(); await draft();
    await page.locator('.turn-recovery').getByRole('button', { name: 'Choose another model' }).click();
    await page.getByRole('menuitem', { name: /^fake/ }).click();
    await page.getByRole('menuitem', { name: /^test\/working/ }).click();
    await completed(); await kept();
    const continued = await api('GET', '/api/conversations/' + choose.id);
    assert.equal(continued.model, 'test/working');
    assert.equal(await page.locator('.thread > .msg').count(), continued.messages.length);
    assert.equal(await page.locator('.thread > .msg.relay').count(), 1);
    assert.equal(await page.locator('.thread > .msg.user').count(), 1);
    assert.equal(await page.locator('.msg.relay button').count(), 0);
    // Original model fails, fallback starts, and the user stops that newer attempt.
    const loops = await api('PUT', '/api/fallback-loops', { loops: [{ name: 'Recovery fixture', entries: [
      { provider: 'fake', model: 'test/fallback-failed' }, { provider: 'backup', model: 'test/fallback-wait' } ] }] });
    const fallback = await chat('take over', { model: 'test/fallback-failed', fallback_loop: loops[0].id });
    await page.locator('.msg.relay .relay-note').waitFor();
    await page.locator('.thread > .msg.assistant:last-child .md').waitFor();
    assert.equal(await page.locator('.msg.relay summary').innerText(), 'fallback-wait took over from fallback-failed');
    assert.equal(await page.locator('.switch-note').count(), 0, 'one transition, not both a switch divider and a Relay message');
    assert.equal(await page.locator('.msg.relay .msg-actions').count(), 0, 'an internal instruction is not editable');
    await draft();
    const takingOver = await api('GET', '/api/conversations/' + fallback.id);
    const originalStop = takingOver.limit.run_id;
    assert.notEqual(originalStop, takingOver.running_run);
    await api('POST', '/api/runs/' + takingOver.running_run + '/stop');
    await page.locator('.turn-recovery').waitFor(); await kept();
    for (const reload of [false, true]) {
      if (reload) { await page.reload(); await page.locator('.turn-recovery').waitFor(); }
      assert.equal(await page.getByRole('button', { name: 'Choose another model', exact: true }).count(), 1);
      assert.equal(await page.getByRole('button', { name: 'Retry', exact: true }).count(), 1);
      assert.equal(await page.locator('.limit-note').count(), 0);
      assert.equal(await page.locator('.recovery-details').count(), 0, 'old failure must not describe the stopped fallback');
      assert.equal(await page.locator('.recovery-notice').count(), 0, 'stopped by the user: nothing to say about retries');
      for (const width of [1280, 390, 320]) {
        await page.setViewportSize({ width, height: 844 });
        await fits(['.turn-recovery', '.recovery-buttons', '.relay-note', '.composer']);
        const relay = page.locator('.relay-note');
        await relay.locator('summary').click();
        await relay.locator('pre').waitFor({ state: 'visible' });
        assert.match(await relay.locator('pre').innerText(), /so you take over/);
        await fits(['.relay-note']);
        await relay.locator('summary').click();
      }
    }
    await page.setViewportSize({ width: 320, height: 844 }); await draft();
    const relaySummary = page.locator('.relay-note summary');
    await relaySummary.focus(); await page.keyboard.press('Space');
    assert.equal(await page.locator('.relay-note').getAttribute('open'), '');
    await page.keyboard.press('Space');
    assert.equal(await page.locator('.relay-note').getAttribute('open'), null);
    await page.locator('.turn-recovery').getByRole('button', { name: 'Retry', exact: true }).click();
    await page.locator('.msg.relay').last().locator('summary').filter({ hasText: 'Relay retried fallback-wait' }).waitFor();
    await page.locator('.thread > .msg.assistant:last-child .md').waitFor(); await kept();
    assert.equal(await page.locator('.msg.relay').count(), 2);
    assert.equal(await page.locator('.msg.user').count(), 1, 'Retry must not turn an internal instruction back into a user bubble');
    const retryingFallback = await api('GET', '/api/conversations/' + fallback.id);
    assert.equal(retryingFallback.messages.at(-1).relay_action.kind, 'retry');
    assert.equal(retryingFallback.messages.at(-1).content, retryingFallback.messages[2].content);
    await api('POST', '/api/runs/' + retryingFallback.running_run + '/stop');
    await page.locator('.turn-recovery').waitFor(); await kept();
    assert.equal(await page.getByRole('button', { name: 'Choose another model', exact: true }).count(), 1);
    await page.locator('.turn-recovery').getByRole('button', { name: 'Choose another model' }).click();
    await page.getByRole('menuitem', { name: /^fake/ }).click();
    await page.getByRole('menuitem', { name: /^test\/working/ }).click();
    await completed(); await kept();
    const recovered = await api('GET', '/api/conversations/' + fallback.id);
    const instruction = recovered.messages.at(-2);
    assert.deepEqual(instruction.relay_action.from, { provider_id: 'backup', model: 'test/fallback-wait' });
    assert.match(instruction.content, /^backup · test\/fallback-wait stopped, so you take over/);
    assert.equal(await page.locator('.msg.relay').count(), 3);
    assert.equal(await page.locator('.turn-recovery').count(), 0);
    assert.equal(recovered.limit, null);
    // A real user's identical-looking words must stay an ordinary, editable message.
    const literal = 'fake stopped without an answer, so you take over. Please continue where it left off; if the task was done, say so briefly.';
    await chat(literal); await completed();
    assert.equal(await page.locator('.msg.relay').count(), 0);
    assert.equal(await page.locator('.msg.user .bubble').innerText(), literal);
    assert.equal(await page.locator('.msg.user').getByRole('button', { name: 'Edit', exact: true }).count(), 1);
    // A continuation finishing after navigation must not pull the user back.
    const late = await chat('hello', { model: 'test/failed' }); await page.locator('.turn-recovery').waitFor();
    let continueRelease, started;
    const requestStarted = new Promise((resolve) => { started = resolve; });
    const continueGate = new Promise((resolve) => { continueRelease = resolve; });
    await page.route('**/api/conversations/' + late.id + '/continue', async (route) => { started(); await continueGate; await route.continue(); });
    await page.locator('.turn-recovery').getByRole('button', { name: 'Choose another model' }).click();
    await page.getByRole('menuitem', { name: /^fake/ }).click();
    await page.getByRole('menuitem', { name: /^test\/working/ }).click();
    await requestStarted;
    await page.evaluate(async () => { const { navigate } = await import('/static/js/main.js'); navigate('/'); });
    await page.getByLabel('Message', { exact: true }).fill('Draft on another page');
    continueRelease();
    await page.waitForTimeout(300);
    assert.equal(new URL(page.url()).pathname, '/');
    assert.equal(await page.getByLabel('Message', { exact: true }).inputValue(), 'Draft on another page');
    assert.deepEqual(errors, []);
    console.log('PASS: direct/isolated/no-git review, diffs, nested Escape, More, retry/stop/regenerate/model recovery, automatic plans, drafts/uploads, stale navigation and phone layout');
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
