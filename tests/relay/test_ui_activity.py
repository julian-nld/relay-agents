"""Real Chromium checks of Relay's activity/navigation UI, with a fake provider."""

import json
import shutil
import socket
import subprocess
import threading
import time

import pytest
import uvicorn

from relay import config, git
from relay.app import create_app
from relay.containers import built_image
from relay.db import Database
from relay.providers import Registry
from relay.providers.base import ProviderRun


def test_activity_questions_and_composer_in_chromium(tmp_path, monkeypatch):
    if not shutil.which("podman"):
        pytest.skip("Chromium UI checks need podman")
    registry = Registry(tmp_path / "providers")
    registry.add({"id": "fake", "name": "Test agent", "kind": "command",
                  "options": {"command": ["true"]}})
    monkeypatch.setattr(registry, "ensure_defaults", lambda: None)
    provider = registry.provider("fake")
    native_runs = {}
    project = tmp_path / "project"
    project.mkdir()
    (project / "file.txt").write_text("original\n")
    git.run(project, "init", "-b", "main")
    git.run(project, "add", ".")
    git.run(project, "commit", "-m", "Initial", identity=True)

    class Run(ProviderRun):
        def __init__(self, context, prompt):
            self.context = context
            self.prompt = prompt
            self.ready = threading.Event()
            self.question_ready = threading.Event()
            self.completed = threading.Event()
            self.answers = []
            native_runs[context.conversation_id] = self

        def events(self):
            if self.prompt == "review fixture":
                (project / "file.txt").write_text("changed\n")
            if self.prompt == "parent":
                child = next(run for run in native_runs.values() if run.prompt == "child question")
                child_id = child.context.conversation_id
                child_run = app.state.relay.runs.active_run_for(child_id)
                parent_run = app.state.relay.runs.active_run_for(self.context.conversation_id)
                app.state.relay.db.execute("UPDATE conversations SET parent_conversation = ? WHERE id = ?",
                                           (self.context.conversation_id, child_id))
                active = app.state.relay.runs.active[child_run]
                active.parent_run, active.parent_conversation = parent_run, self.context.conversation_id
                for index in range(3):
                    yield {"type": "tool_call", "id": f"r{index}", "name": "read_file", "input": {"path": f"file{index}.py"}}
                    yield {"type": "tool_result", "id": f"r{index}", "name": "read_file", "ok": True, "output": "contents"}
                yield {"type": "text", "delta": "\n\n".join(f"Earlier paragraph {i}. " + "Read this at your own pace. " * 6 for i in range(30))}
                yield {"type": "subagent", "id": child_id, "run": child_run, "label": "Researcher",
                       "task": "Check the project and ask about its use", "provider": "fake", "model": "test/model", "status": "running"}
                yield {"type": "subagent", "id": "failed-child", "label": "Unavailable helper", "task": "Inspect dependencies",
                       "status": "failed", "error": "Test provider offline"}
                child.question_ready.set()
                while not child.completed.wait(.15) and not self.ready.is_set():
                    yield {"type": "text", "delta": "\n\nNew parent activity while its helper is running."}
                yield {"type": "subagent", "id": child_id, "run": child_run, "status": "succeeded"}
                yield {"type": "text", "delta": "\n\nThe helper answered; here is the final response."}
                yield {"type": "done", "status": "succeeded"}
                return
            if self.prompt == "child question":
                assert self.question_ready.wait(60)
                yield {"type": "tool_call", "id": "child-read", "name": "read_file", "input": {"path": "project.py"}}
                yield {"type": "tool_result", "id": "child-read", "name": "read_file", "ok": True, "output": "contents"}
            yield {"type": "text", "delta": "Looking into your task."}
            if "question" in self.prompt:
                yield {"type": "form", "id": "frm_ui", "title": "A question for you", "status": "pending",
                       "fields": [{"key": "use", "type": "string", "title": "Which use?", "required": True,
                                   "options": [{"value": "Work", "label": "Work"},
                                               {"value": "Play", "label": "Play"}]}]}
            if "question" in self.prompt or "background" in self.prompt:
                assert self.ready.wait(60)
            yield {"type": "text", "delta": "Your task is complete."}
            yield {"type": "done", "status": "succeeded"}
            self.completed.set()

        def answer_form(self, request_id, answer):
            assert request_id == "frm_ui"
            self.answers.append(answer)
            self.ready.set()

        def interrupt(self):
            self.ready.set()
            self.question_ready.set()

    monkeypatch.setattr(provider, "start", lambda context, prompt: Run(context, prompt))
    monkeypatch.setattr(registry, "provider", lambda _: provider)
    settings = config.Settings(chat_root=str(tmp_path / "chats"), workspace_roots=[str(project)])
    app = create_app(settings, Database(tmp_path / "relay.db"), registry)
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
                                input=json.dumps({"base": f"http://127.0.0.1:{sock.getsockname()[1]}", "project": str(project)}),
                                text=True, capture_output=True, timeout=75, check=False)
        assert result.returncode == 0, result.stdout + result.stderr
        child = next(run for run in native_runs.values() if run.prompt == "child question")
        parent = next(run for run in native_runs.values() if run.prompt == "parent")
        assert child.answers == [{"use": "Work"}] and not parent.answers
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()
        assert not thread.is_alive()


BROWSER_CHECK = r"""
const assert = require('node:assert/strict');
const { chromium } = require('/usr/local/lib/node_modules/playwright-core');
const { base, project } = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
(async () => {
  const browser = await chromium.launch({ executablePath: '/usr/bin/chromium', args: ['--no-sandbox'] });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();
  page.setDefaultTimeout(10000);
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  async function api(method, path, data) {
    const response = await context.request.fetch(base + path, { method, data, headers: { 'X-Relay-Request': '1', 'X-Relay-Client': 'web' } });  // (as Relay's pages: the user's own)
    assert.ok(response.ok(), await response.text());
    return response.json();
  }
  async function chat(title, prompt, pinned = false) {
    const conv = await api('POST', '/api/conversations', { provider_id: 'fake', title, fallback_loop: 'off' });
    if (pinned) await api('PATCH', '/api/conversations/' + conv.id, { pinned: true });
    if (prompt) await api('POST', '/api/conversations/' + conv.id + '/messages', { text: prompt });
    return conv;
  }
  async function status(label) {
    await page.waitForFunction((label) => document.querySelector('.header .activity-badge')?.textContent === label, label);
  }
  async function fits() {
    await page.waitForFunction(() => ['.composer', '.header .activity-badge', '.composer .send-btn'].every((selector) => {
      const box = document.querySelector(selector)?.getBoundingClientRect();
      return box && box.width > 0 && box.x >= 0 && box.right <= innerWidth + .5;
    }));
    assert.equal(await page.evaluate(() => document.scrollingElement.scrollWidth > innerWidth), false);
    // Measured in one go in the page: the header is rendered anew as the run goes on, and a
    // locator's boundingBox() between finding an element and measuring it can lose it (null).
    const selectors = ['.composer', '.header .activity-badge', '.composer .send-btn'];
    const boxes = await page.evaluate((selectors) => selectors.map((selector) => {
      const box = document.querySelector(selector)?.getBoundingClientRect();
      return box ? { x: box.x, width: box.width } : null;
    }), selectors);
    selectors.forEach((selector, i) => {
      const box = boxes[i];
      assert.ok(box && box.width > 0 && box.x >= 0 && box.x + box.width <= page.viewportSize().width + .5, selector + ' overflows');
    });
  }
  try {
    await api('POST', '/api/setup', { username: 'ui', password: 'test-password' });
    // Native disclosures open only for current tool activity, like live Thinking.
    await page.goto(base + '/');
    await page.getByLabel('Message', { exact: true }).waitFor();
    await page.evaluate(async () => {
      const { AssistantView, setShowDetails } = await import('/static/js/render.js');
      setShowDetails(false);
      const view = new AssistantView({ live: true });
      document.body.append(view.el);
      view.el.id = 'activity-fixture';
      window.activityFixture = view;
      view.toolCall({ id: 'one', name: 'shell', input: { command: 'pwd' } });
    });
    const currentActivity = page.locator('#activity-fixture .tool-activity');
    assert.equal(await currentActivity.getAttribute('open'), '');
    assert.equal(await currentActivity.locator('.tool.open').count(), 0);
    await currentActivity.locator('summary').focus();
    await page.keyboard.press('Space');
    await page.waitForFunction(() => !document.querySelector('#activity-fixture .tool-activity').open);
    assert.equal(await currentActivity.getAttribute('open'), null);
    await page.evaluate(() => {
      activityFixture.toolResult({ id: 'one', ok: true });
      activityFixture.toolCall({ id: 'two', name: 'shell', input: { command: 'ls' } });
    });
    assert.equal(await currentActivity.getAttribute('open'), null, 'updates respect manual collapse');
    await currentActivity.locator('summary').focus();
    await page.keyboard.press('Space');
    await page.waitForFunction(() => document.querySelector('#activity-fixture .tool-activity').open);
    await page.evaluate(() => activityFixture.toolResult({ id: 'two', ok: true }));
    assert.equal(await currentActivity.getAttribute('open'), '');
    assert.match(await currentActivity.locator('summary').innerText(), /2 commands/);
    await page.evaluate(() => activityFixture.add({ type: 'reasoning', delta: 'Next steps', reasoning_ms: 0 }));
    assert.equal(await currentActivity.getAttribute('open'), null);
    assert.equal(await page.locator('#activity-fixture .reasoning').getAttribute('open'), '');
    await page.evaluate(async () => {
      const { setShowDetails } = await import('/static/js/render.js');
      activityFixture.add({ type: 'tool_call', id: 'three', name: 'read_file', input: { path: 'file.txt' }, reasoning_ms: 68000 });
      setShowDetails(true);
      activityFixture.toolResult({ id: 'three', ok: true });
      activityFixture.finish('succeeded');
    });
    assert.equal(await page.locator('#activity-fixture .tool-activity[open]').count(), 2);
    assert.match(await page.locator('#activity-fixture .reasoning summary').innerText(), /Thought for 1m 08s/);
    await page.evaluate(async () => {
      const { setShowDetails } = await import('/static/js/render.js');
      setShowDetails(false);
      activityFixture.el.remove(); delete window.activityFixture;
    });
    // Automatic disclosure transitions follow the bottom, but never steal a readback anchor.
    for (const reducedMotion of ['no-preference', 'reduce']) {
      await page.emulateMedia({ reducedMotion });
      const motion = await page.evaluate(async () => {
        const { AssistantView } = await import('/static/js/render.js');
        const { ThreadScroll } = await import('/static/js/thread-scroll.js');
        const { h } = await import('/static/js/ui.js');
        const thread = h('div.thread', h('div.md', Array.from({ length: 20 }, (_, i) => h('p', `Earlier paragraph ${i}`))));
        const scroller = h('div.scroller', thread);
        Object.assign(scroller.style, { position: 'fixed', top: '0', left: '0', height: '240px', width: 'min(700px, 100vw)', zIndex: '10000' });
        document.body.append(scroller);
        const scroll = new ThreadScroll(scroller, thread);
        const view = new AssistantView({ live: true, onActivity: () => scroll.changed() });
        thread.append(view.el);
        const frame = () => new Promise(requestAnimationFrame);
        const settled = async () => { for (let i = 0; i < 24; i++) await frame(); };
        try {
          view.add({ type: 'reasoning', delta: 'Considering this carefully.\n'.repeat(40), reasoning_ms: 0 });
          await settled();
          const reasoning = view.body.querySelector('.reasoning');
          const opened = reasoning.getBoundingClientRect().height;
          view.add({ type: 'text', delta: 'Answer', reasoning_ms: 12000 });
          const duration = getComputedStyle(reasoning, '::details-content').transitionDuration;
          const heights = [];
          for (let i = 0; i < 24; i++) { heights.push(reasoning.getBoundingClientRect().height); await frame(); }
          const closed = reasoning.getBoundingClientRect().height;
          const follows = scroll.following && scroll.atBottom();
          scroller.scrollTop = 0;
          await frame(); scroll.following = false; scroll.rememberAnchor();
          const anchor = scroll.anchor.el, top = anchor.getBoundingClientRect().top;
          view.add({ type: 'reasoning', delta: 'Another thought.\n'.repeat(20), reasoning_ms: 0 });
          await settled();
          view.add({ type: 'text', delta: 'More answer', reasoning_ms: 3000 });
          await settled();
          return { supported: CSS.supports('interpolate-size: allow-keywords') && CSS.supports('selector(details::details-content)'),
            reduced: matchMedia('(prefers-reduced-motion: reduce)').matches, duration,
            intermediate: heights.some((height) => height > closed + 1 && height < opened - 1),
            follows, reading: !scroll.following && Math.abs(anchor.getBoundingClientRect().top - top) < 2,
            labels: [...view.body.querySelectorAll('.reasoning summary')].map((el) => el.innerText) };
        } finally { scroll.destroy(); scroller.remove(); }
      });
      assert.equal(motion.follows, true, 'automatic collapse stays at the bottom');
      assert.equal(motion.reading, true, 'streaming and collapse preserve readback');
      assert.deepEqual(motion.labels, ['Thought for 12s', 'Thought for 3s']);
      if (motion.supported) {
        assert.equal(motion.intermediate, !motion.reduced, 'native height transition respects reduced motion');
        assert.equal(motion.duration, motion.reduced ? '0s' : '0.18s, 0.18s, 0.18s');
      }
    }
    await page.emulateMedia({ reducedMotion: 'no-preference' });
    // Two tasks in one folder share one actual review, not two stale change totals.
    const reviewTask = await api('POST', '/api/conversations', { provider_id: 'fake', mode: 'code', directory: project, title: 'Review task', fallback_loop: 'off' });
    await api('POST', '/api/conversations', { provider_id: 'fake', mode: 'code', directory: project, title: 'Shared task', fallback_loop: 'off' });
    await api('POST', '/api/conversations/' + reviewTask.id + '/messages', { text: 'review fixture' });
    await chat('Pinned history', null, true);
    const working = await chat('Background agent', 'background');
    const question = await chat('A long question title that must still fit comfortably on a small phone screen', 'question', true);
    // Settings only refresh controls, on the welcome screen as well as in chats.
    await page.goto(base + '/');
    await page.getByLabel('Message', { exact: true }).fill('Keep this welcome draft');
    await page.waitForFunction(() => document.querySelector('.activity-bar [data-view=tasks] .bar-badge')?.textContent === '3');
    assert.equal(await page.locator('.panel-view[data-view=chats] .conv > svg.mode').count(), 5);
    assert.equal(await page.locator('.conv').filter({ hasText: 'Pinned history' }).locator('svg.mode path').getAttribute('d'),
      'M21 12a8 8 0 0 1-11.8 7L3 21l2-6.2A8 8 0 1 1 21 12Z');
    assert.match(await page.getByRole('button', { name: /^Tasks/ }).getAttribute('title'), /Working: 1 · Waiting for you: 1 · Needs review: 1/);
    await page.getByLabel('Search chats').fill('Pinned history');
    await page.waitForFunction(() => document.querySelectorAll('.panel-view[data-view=chats] .conv').length === 1);
    assert.equal(await page.locator('.activity-bar [data-view=tasks] .bar-badge').innerText(), '3', 'search must not filter the Tasks count');
    await page.getByLabel('Search chats').fill('');
    await page.waitForFunction(() => document.querySelectorAll('.panel-view[data-view=chats] .conv').length === 5);
    await page.locator('.composer input[type=file]').setInputFiles({ name: 'draft.txt', mimeType: 'text/plain', buffer: Buffer.from('draft attachment') });
    await page.locator('.composer .attachment .remove').waitFor();
    assert.equal(await page.evaluate(async () => {
      const textarea = document.querySelector('.composer textarea'), composer = textarea.closest('.composer');
      const { settingsHooks } = await import('/static/js/main.js');
      await settingsHooks().onProvidersChanged();
      await settingsHooks().onModesChanged();
      return textarea === document.querySelector('.composer textarea') && composer.isConnected && textarea.value === 'Keep this welcome draft'
        && composer.textContent.includes('draft.txt');
    }), true);
    // Ordinary menus own focus, slash suggestions do not interrupt typing.
    await page.locator('.composer button.picker').first().click();
    assert.equal(await page.evaluate(() => !!document.activeElement.closest('.menu')), true);
    await page.keyboard.press('End');
    await page.keyboard.press('Escape');
    assert.equal(await page.evaluate(() => document.activeElement.matches('.composer button.picker')), true);
    await page.getByLabel('Message', { exact: true }).fill('/mo');
    await page.locator('.menu').waitFor();
    assert.equal(await page.evaluate(() => document.activeElement.matches('textarea')), true);
    await page.keyboard.press('Tab');
    assert.match(await page.getByLabel('Message', { exact: true }).inputValue(), /^\/mode/);
    await page.keyboard.press('Escape');
    await page.getByLabel('Message', { exact: true }).fill('Keep this welcome draft');
    // Shift+Tab cycles the welcome draft through the picker's actual modes without sending.
    const draftModes = await page.evaluate(async () => {
      const { modeChoices } = await import('/static/js/pickers.js');
      const { state } = await import('/static/js/state.js');
      window.shortcutState = state;
      return modeChoices(state.draft.providerId).map((m) => m.id);
    });
    await page.getByLabel('Message', { exact: true }).evaluate((el) => el.setSelectionRange(5, 12));
    for (const mode of [...draftModes.slice(1), draftModes[0]]) {
      await page.keyboard.press('Shift+Tab');
      await page.waitForFunction((mode) => (window.shortcutState.draft.agentMode || 'build') === mode
        && !document.querySelector('.composer .send-btn').disabled, mode);
      assert.equal(await page.evaluate(() => document.activeElement.matches('.composer textarea')), true);
      assert.equal(await page.getByLabel('Message', { exact: true }).inputValue(), 'Keep this welcome draft');
      assert.deepEqual(await page.getByLabel('Message', { exact: true }).evaluate((el) => [el.selectionStart, el.selectionEnd]), [5, 12]);
      assert.match(await page.locator('.composer .attachment').innerText(), /draft.txt/);
      assert.equal(await page.locator('.composer-options').getAttribute('open'), null);
    }
    // Polling keeps unchanged nodes, and restores semantic focus if a row changes.
    await page.locator('.conv').first().focus();
    assert.equal(await page.evaluate(async () => {
      const before = document.activeElement;
      const { loadConversations } = await import('/static/js/main.js');
      await loadConversations();
      return before === document.activeElement && before.isConnected;
    }), true);
    const row = page.locator('.conv').filter({ hasText: 'Background agent' });
    await row.locator('.menu-btn').focus();
    await page.keyboard.press('Enter');
    assert.equal(new URL(page.url()).pathname, '/');
    assert.equal(await page.evaluate(() => !!document.activeElement.closest('.menu')), true);
    await api('PATCH', '/api/conversations/' + working.id, { title: 'Updated background agent' });
    await page.evaluate(async () => { const { loadConversations } = await import('/static/js/main.js'); await loadConversations(); });
    await page.getByRole('menuitem', { name: 'Rename', exact: true }).click();
    await page.locator('.conv input.rename').fill('Renamed through the menu');
    await page.keyboard.press('Enter');
    await page.locator('.conv .title').filter({ hasText: 'Renamed through the menu' }).waitFor();
    assert.equal(new URL(page.url()).pathname, '/');
    await page.locator('.conv').filter({ hasText: 'Renamed through the menu' }).focus();
    await api('PATCH', '/api/conversations/' + working.id, { title: 'Background agent' });
    assert.equal(await page.evaluate(async () => {
      const key = document.activeElement.dataset.focus;
      const { loadConversations } = await import('/static/js/main.js'); await loadConversations();
      return key === document.activeElement.dataset.focus;
    }), true);
    // Modal focus starts inside, wraps in both directions and returns on close.
    await page.getByRole('button', { name: 'Settings', exact: true }).first().click();
    await page.locator('.settings .pane h3').first().waitFor();
    // (its web search section draws when podman answers: its controls are the pane's last, so first all of it)
    await page.waitForFunction(() => !document.querySelector('.settings .pane .spinner'));
    assert.match(await page.locator('.settings .pane').innerText(), /On touchscreens, Enter adds a line/);
    assert.equal(await page.evaluate(() => !!document.activeElement.closest('.modal')), true);
    await page.locator('.modal-head button').focus();
    await page.keyboard.press('Shift+Tab');
    assert.equal(await page.evaluate(() => !!document.activeElement.closest('.modal')), true);
    await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(() => document.activeElement.matches('.modal-head button')), true);
    await page.evaluate(async () => {
      const { showMenu } = await import('/static/js/ui.js');
      showMenu(document.activeElement, [{ label: 'Parent menu', onclick() {}, submenu: async () => [{ label: 'Child menu', onclick() {} }] }]);
    });
    await page.keyboard.press('ArrowRight');
    await page.waitForFunction(() => document.activeElement.textContent.trim() === 'Child menu');
    await page.keyboard.press('ArrowLeft');
    assert.match(await page.evaluate(() => document.activeElement.textContent.trim()), /Parent menu/);
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('.modal').count(), 1);
    assert.equal(await page.evaluate(() => document.activeElement.matches('.modal-head button')), true);
    // Nested dialogs close only the top one and restore the parent's focus.
    await page.evaluate(async () => { const { promptDialog } = await import('/static/js/ui.js'); promptDialog('Nested test', { label: 'Test value' }); });
    await page.getByLabel('Test value').fill('local only');
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('.modal').count(), 1);
    assert.equal(await page.evaluate(() => !!document.activeElement.closest('.modal')), true);
    await page.locator('.settings nav button').filter({ hasText: 'Limits' }).click();
    await page.getByLabel('Stay signed in', { exact: true }).waitFor();
    assert.equal(await page.locator('.settings input[type=number], .settings textarea').evaluateAll((inputs) => inputs.some((input) => !input.labels.length)), false);
    assert.match(await page.locator('.settings-page-head .lead').innerText(), /when you click Save/);
    assert.equal(await page.getByLabel('Dismiss after', { exact: true }).getAttribute('aria-describedby'), 'limit-questions.dismiss_timeout-help');
    await page.locator('.settings nav button').filter({ hasText: 'Notifications' }).click();
    await page.getByRole('button', { name: 'Save and send test', exact: true }).waitFor();
    let saves = 0, tests = 0;
    await page.route('**/api/settings', async (route) => {
      if (route.request().method() !== 'PUT') return route.continue();
      saves++;
      if (saves === 1) return route.fulfill({ status: 502, json: { error: 'Save failed' } });
      return route.continue();
    });
    await page.route('**/api/notifications/test', async (route) => {
      tests++;
      return route.fulfill({ json: tests === 1 ? { ok: false, errors: ['Test endpoint offline'] } : { ok: true, errors: [] } });
    });
    const notificationTest = page.getByRole('button', { name: 'Save and send test', exact: true });
    await notificationTest.click();
    await page.waitForFunction(() => document.querySelector('.settings .check-result').textContent === 'Save failed');
    assert.equal(tests, 0, 'a failed save must not send a test');
    assert.equal(await notificationTest.isEnabled(), true);
    await notificationTest.evaluate((button) => { button.click(); button.click(); });
    await page.waitForFunction(() => document.querySelector('.settings .check-result').textContent === 'Settings saved. Test endpoint offline');
    assert.equal(saves, 2, 'disabled controls must not submit twice');
    assert.equal(await notificationTest.isEnabled(), true);
    await notificationTest.click();
    await page.waitForFunction(() => document.querySelector('.settings .check-result').textContent.startsWith('Settings saved. Test sent'));
    assert.equal(saves, 3);
    assert.equal(tests, 2);
    await page.unroute('**/api/settings');
    await page.unroute('**/api/notifications/test');
    for (const width of [390, 320]) {
      await page.setViewportSize({ width, height: 844 });
      for (const theme of ['light', 'dark']) {
        await page.evaluate(async (theme) => { const { setTheme } = await import('/static/js/theme.js'); setTheme(theme); }, theme);
        assert.equal(await page.locator('.settings .pane').evaluate((pane) => pane.scrollWidth > pane.clientWidth), false);
        const box = await notificationTest.boundingBox();
        assert.ok(box.x >= 0 && box.x + box.width <= width + .5);
      }
    }
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.keyboard.press('Escape');
    assert.equal(await page.evaluate(() => document.activeElement.getAttribute('aria-label')), 'Settings');
    await page.goto(base + '/scheduled');
    await page.locator('.schedules-view').waitFor();
    // (opened on its page: the column in the AgentOS view, the bar's icon for it marked)
    assert.equal(await page.locator('.activity-bar .bar-item.active').getAttribute('aria-label'), 'AgentOS');
    assert.equal(await page.locator('.activity-bar .bar-item.active').getAttribute('aria-current'), 'true');
    assert.equal(await page.locator('.panel-view.shown').getAttribute('data-view'), 'agentos');
    await page.evaluate(async () => { const { settingsHooks } = await import('/static/js/main.js'); await settingsHooks().onModesChanged(); });
    assert.equal(new URL(page.url()).pathname, '/agentos');  // the old address goes to AgentOS
    assert.equal(await page.locator('.schedules-view').count(), 1);
    assert.equal(await page.locator('.welcome').count(), 0);
    await page.goto(base + '/c/' + question.id);
    await page.locator('.question-card.pending').waitFor();
    await status('Waiting for you');
    await page.locator('.activity-bar [data-view=chats]').click();  // (the view kept from the AgentOS page: back)
    await page.waitForFunction(() => document.querySelector('.conv-group.attention .conv'));
    assert.match(await page.locator('.conv-group').first().innerText(), /Waiting for you/);
    assert.match(await page.locator('.conv-group').nth(1).innerText(), /Working/);
    await page.locator('.header button.activity-badge').click();
    assert.equal(await page.evaluate(() => document.activeElement.closest('.question-card') !== null), true);
    // Ordinary polls must not replace a focused status button with unchanged activity.
    await page.locator('.header button.activity-badge').focus();
    assert.equal(await page.evaluate(async () => {
      const button = document.activeElement;
      const { loadConversations } = await import('/static/js/main.js');
      await loadConversations();
      return button === document.activeElement && button.isConnected;
    }), true);

    const options = page.locator('.composer-options');
    assert.equal(await options.getAttribute('open'), null);
    await page.getByLabel('Message', { exact: true }).fill('A draft that must survive changing options');
    await options.locator('summary').click();
    await page.locator('.advanced-pickers .picker[title^="Mode"]').click();
    await page.locator('.menu button').filter({ hasText: 'Plan · read-only' }).click();
    await page.waitForFunction(() => document.querySelector('.composer-options summary').textContent.includes('Plan'));
    assert.equal(await page.evaluate(() => document.activeElement.matches('.advanced-pickers .picker[title^="Mode"]')), true);
    assert.equal(await page.getByLabel('Message', { exact: true }).inputValue(), 'A draft that must survive changing options');
    assert.equal(await options.getAttribute('open'), '');
    // While a real fake-provider stream waits, shortcut saves keep its source and draft intact.
    await page.locator('.composer input[type=file]').setInputFiles({ name: 'mode-draft.txt', mimeType: 'text/plain', buffer: Buffer.from('mode draft') });
    await page.locator('.composer .attachment .remove').waitFor();
    await page.getByLabel('Message', { exact: true }).focus();
    await page.evaluate(async () => {
      const { state } = await import('/static/js/state.js');
      window.shortcutState = state; window.modeSource = state.following.source;
    });
    let releaseMode, modeStarted;
    const modeGate = new Promise((resolve) => { releaseMode = resolve; });
    const startedMode = new Promise((resolve) => { modeStarted = resolve; });
    const savedModes = [];
    await page.route('**/api/conversations/' + question.id, async (route) => {
      if (route.request().method() === 'PATCH' && 'agent_mode' in route.request().postDataJSON()) {
        savedModes.push(route.request().postDataJSON().agent_mode);
        if (savedModes.length === 1) { modeStarted(); await modeGate; }
      }
      await route.continue();
    });
    await page.keyboard.press('Shift+Tab'); await startedMode;
    await page.keyboard.press('Shift+Tab');
    assert.equal(savedModes.length, 1, 'quick shortcuts must not race saves');
    assert.equal(await page.locator('.composer .send-btn').isDisabled(), true);
    await page.keyboard.press('Enter');
    assert.equal(await page.getByLabel('Message', { exact: true }).inputValue(), 'A draft that must survive changing options');
    releaseMode();
    await page.waitForFunction(() => window.shortcutState.current.agent_mode === 'fast'
      && !document.querySelector('.composer .send-btn').disabled);
    assert.deepEqual(savedModes, ['readonly', 'fast']);
    assert.equal(await page.evaluate(async () => {
      const { state } = await import('/static/js/state.js'); return state.following.source === window.modeSource;
    }), true);
    assert.equal(await page.evaluate(() => document.activeElement.matches('.composer textarea')), true);
    assert.equal(await options.getAttribute('open'), '');
    assert.match(await page.locator('.composer .attachment').innerText(), /mode-draft.txt/);
    assert.equal((await api('GET', '/api/conversations/' + question.id)).agent_mode, 'fast');
    await page.unroute('**/api/conversations/' + question.id);
    // Continue through Brainstorm and Build back to Plan with Options collapsed on a phone.
    await options.locator('summary').click();
    await page.setViewportSize({ width: 320, height: 844 });
    await page.getByLabel('Message', { exact: true }).focus();
    for (const mode of ['brainstorm', 'build', 'plan']) {
      await page.keyboard.press('Shift+Tab');
      await page.waitForFunction((mode) => (window.shortcutState.current.agent_mode || 'build') === mode
        && !document.querySelector('.composer .send-btn').disabled, mode);
      assert.equal(await options.getAttribute('open'), null);
    }
    await page.setViewportSize({ width: 1280, height: 900 });
    await options.locator('summary').click();
    await options.locator('summary').click();
    assert.match(await options.locator('summary').innerText(), /Plan/);
    await fits();

    // Waiting tasks are separate from agents that are actually working.
    await page.goto(base + '/tasks');
    await page.locator('.task-group.waiting .task-card').waitFor();
    assert.equal(await page.locator('.task-group.waiting .task-card').count(), 1);
    assert.equal(await page.locator('.task-group.running .task-card').count(), 1);
    assert.match(await page.locator('.task-group.running h2').innerText(), /^Working\s*1$/);
    assert.equal(await page.locator('.task-group.review .task-card').count(), 1);
    assert.equal(await page.locator('.activity-bar [data-view=tasks] .bar-badge').innerText(), '3');
    await page.locator('.task-title').first().focus();
    const focusedTask = await page.evaluate(() => document.activeElement.dataset.focus);
    await page.waitForTimeout(4300);
    assert.equal(await page.evaluate(() => document.activeElement.dataset.focus), focusedTask);
    await page.evaluate(async () => { const { settingsHooks } = await import('/static/js/main.js'); await settingsHooks().onModesChanged(); });
    assert.equal(new URL(page.url()).pathname, '/tasks');
    assert.equal(await page.locator('.tasks').count(), 1);
    assert.equal(await page.locator('.welcome').count(), 0);
    await page.locator('.task-group.waiting').getByRole('button', { name: 'Answer', exact: true }).click();
    await page.locator('.question-card.pending').waitFor();
    await status('Waiting for you');

    for (const width of [390, 320]) {
      await page.setViewportSize({ width, height: 844 });
      await fits();
      await page.locator('.header button.activity-badge').click();
      assert.equal(await page.evaluate(() => document.activeElement.closest('.question-card') !== null), true);
      await options.locator('summary').click();
      await fits();
      await options.locator('summary').click();
    }
    await page.evaluate(async () => { const { setTheme } = await import('/static/js/theme.js'); setTheme('dark'); });
    await fits();
    await page.reload();
    await page.locator('.question-card.pending').waitFor();
    await status('Waiting for you');
    await page.locator('.question-card').getByLabel('Work', { exact: true }).check();
    await page.locator('.question-card').getByRole('button', { name: 'Send answers' }).click();
    await status('Completed');
    assert.equal(await page.locator('.question-card.pending').count(), 0);
    await page.waitForFunction(() => !document.querySelector('.conv-group.attention'));
    await page.waitForFunction(() => document.querySelector('.activity-bar [data-view=tasks] .bar-badge')?.textContent === '2');
    await fits();
    await api('POST', '/api/conversations/' + reviewTask.id + '/reviewed');
    await page.evaluate(async () => { const { loadConversations } = await import('/static/js/main.js'); await loadConversations(); });
    await page.waitForFunction(() => document.querySelector('.activity-bar [data-view=tasks] .bar-badge')?.textContent === '1');
    // Real SSE, grouped tools, nested streams and forwarded question ownership.
    await page.setViewportSize({ width: 1280, height: 900 });
    const child = await chat('Research helper', 'child question');
    const parent = await chat('Parallel research', 'parent');
    // Count only the child's known run, not the ordinary parent connection.
    const childRow = await api('GET', '/api/conversations/' + child.id);
    const childRun = childRow.running_run;
    const childRequests = [];
    page.on('request', (request) => {
      if (request.url().endsWith('/api/runs/' + childRun + '/events')) childRequests.push(request.url());
    });
    await page.goto(base + '/c/' + parent.id);
    await page.locator('.question-card.pending').waitFor();
    await status('Waiting for you');
    assert.equal(childRequests.length, 0);
    const activity = page.locator('.thread > .msg.assistant > .body > .tool-activity').first();
    assert.match(await activity.locator('summary').innerText(), /3 files read/);
    assert.equal(await activity.getAttribute('open'), null);
    await activity.locator('summary').click();
    const toolHead = activity.locator('.tool-head').first();
    assert.equal(await toolHead.getAttribute('aria-expanded'), 'false');
    assert.ok(await page.locator('[id="' + await toolHead.getAttribute('aria-controls') + '"]').count());
    await toolHead.click();
    assert.equal(await toolHead.getAttribute('aria-expanded'), 'true');
    await activity.locator('summary').click();
    const helper = page.locator('.tool.subagent').filter({ hasText: 'Researcher' }).first();
    assert.match(await helper.locator('.subagent-status').innerText(), /Waiting for you/);
    assert.match(await helper.locator('.subagent-heading').innerText(), /test\/model/);
    const failure = page.locator('.subagent-alert').filter({ hasText: 'Test provider offline' });
    assert.ok(await failure.isVisible());
    assert.equal(await failure.locator('..').getAttribute('class'), 'tool subagent');
    // Reading back must survive large parent updates, including delayed markdown layout.
    await page.locator('.thread-pane > .scroller').evaluate((el) => { el.scrollTop = 200; });
    await page.waitForFunction(() => document.querySelector('.latest-btn')?.textContent.includes('New activity'));
    const before = await page.locator('.thread-pane > .scroller').evaluate((el) => el.scrollTop);
    await page.waitForTimeout(350);
    assert.ok(Math.abs(await page.locator('.thread-pane > .scroller').evaluate((el) => el.scrollTop) - before) < 3);
    await page.locator('.latest-btn').click();
    await page.waitForFunction(() => {
      const el = document.querySelector('.thread-pane > .scroller');
      return el.scrollHeight - el.scrollTop - el.clientHeight < 72;
    });
    assert.equal(await page.locator('.latest-btn').isVisible(), false);
    // Expanding a helper opens just one connection; Show details does not open it.
    await page.evaluate(async () => { const { setShowDetails } = await import('/static/js/render.js'); setShowDetails(true); });
    assert.equal(await activity.getAttribute('open'), '');
    assert.equal(await toolHead.getAttribute('aria-expanded'), 'true');
    assert.equal(await helper.locator(':scope > .tool-head').getAttribute('aria-expanded'), 'false');
    assert.equal(childRequests.length, 0);
    await page.evaluate(async () => { const { setShowDetails } = await import('/static/js/render.js'); setShowDetails(false); });
    await helper.locator(':scope > .tool-head').click();
    assert.equal(await helper.locator(':scope > .tool-head').getAttribute('aria-expanded'), 'true');
    await helper.locator('.subagent-thread .tool-activity').waitFor();
    assert.equal(childRequests.length, 1);
    assert.match(await helper.locator('.subagent-thread .tool-activity summary').innerText(), /1 file read/);
    await helper.locator(':scope > .tool-head').click();
    assert.equal(await helper.locator(':scope > .tool-head').getAttribute('aria-expanded'), 'false');
    assert.equal(await helper.locator('.subagent-thread').isVisible(), false);
    await page.reload();
    await page.locator('.question-card.pending').first().waitFor();
    assert.equal(childRequests.length, 1, 'reload must not auto-open child streams');
    for (const width of [390, 320]) {
      await page.setViewportSize({ width, height: 844 });
      await fits();
      await page.locator('.header button.activity-badge').click();
      const question = page.locator('.thread > .msg.assistant > .body > .question-card.pending');
      assert.ok(await question.isVisible());
      const box = await page.evaluate(() => {  // (in one go: the thread may be drawn anew meanwhile)
        const card = document.querySelector('.thread > .msg.assistant > .body > .question-card.pending')?.getBoundingClientRect();
        return card && { x: card.x, width: card.width };
      });
      assert.ok(box && box.x >= 0 && box.x + box.width <= width + .5, JSON.stringify(box));
    }
    const forwarded = page.locator('.thread > .msg.assistant > .body > .question-card.pending');
    await forwarded.getByLabel('Work', { exact: true }).check();
    await forwarded.getByRole('button', { name: 'Send answers' }).click();
    await status('Completed');
    assert.match(await helper.locator('.subagent-status').innerText(), /Completed/);
    await page.reload();
    await status('Completed');
    assert.equal(await page.locator('.tool-activity[open]').count(), 0);
    assert.equal(await page.locator('.tool.subagent.open').count(), 0);
    assert.deepEqual(errors, []);
    console.log('PASS: activity, parent/child SSE, deferred subagent streams, forwarded answers, readback/Latest, sidebar, Options, themes and 320/390px layout');
  } finally {
    await browser.close();
  }
})().catch((e) => { console.error(e); process.exitCode = 1; });
"""
