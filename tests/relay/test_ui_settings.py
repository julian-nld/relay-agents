"""Settings layout and unchanged saving contracts in Chromium with private data."""

import json
import shutil
import socket
import subprocess
import threading
import time

import pytest
import uvicorn

from relay import config
from relay.app import create_app
from relay.containers import built_image
from relay.db import Database
from relay.providers import Registry


def test_settings_layout_in_chromium(tmp_path, monkeypatch):
    if not shutil.which("podman"):
        pytest.skip("Chromium UI checks need podman")
    registry = Registry(tmp_path / "providers")
    registry.add({"id": "fake", "name": "A very long provider name to exercise the settings action layout",
                  "kind": "command", "options": {"command": ["true"], "models": ["test/small", "test/large"]}})
    registry.add({"id": "disabled", "name": "Disabled test provider", "kind": "command", "enabled": False,
                  "options": {"command": ["true"]}})
    monkeypatch.setattr(registry, "ensure_defaults", lambda: None)
    project = tmp_path / "project-with-a-long-name-for-layout-checks"
    project.mkdir()
    (project / "file.txt").write_text("fixture\n")
    shots = tmp_path / "shots"
    shots.mkdir()
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
        result = subprocess.run(["podman", "run", "--rm", "-i", "--network=host", "--timeout=105", "-v", f"{shots}:/shots",
                                 built_image(), "node", "-e", BROWSER_CHECK],
                                input=json.dumps({"base": f"http://127.0.0.1:{sock.getsockname()[1]}", "project": str(project)}),
                                text=True, capture_output=True, timeout=90, check=False)
        assert result.returncode == 0, result.stdout + result.stderr + f"\nScreenshots: {shots}"
        print(f"Settings screenshots: {shots}")
    finally:
        server.should_exit = True
        thread.join(60)  # (a request still asking a busy podman, General's search status, finishes first)
        sock.close()
        assert not thread.is_alive()


BROWSER_CHECK = r"""
const assert = require('node:assert/strict');
const { chromium } = require('/usr/local/lib/node_modules/playwright-core');
const { base, project } = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const tabs = [['general', 'General'], ['providers', 'Providers'], ['modes', 'Modes'], ['subagents', 'Subagents'],
  ['computers', 'Computers'], ['skills', 'Skills'], ['memory', 'Memory'], ['usage', 'Usage'], ['agentos', 'AgentOS'], ['system', 'System'],
  ['plugins', 'Plugins'], ['workspaces', 'Workspaces'], ['limits', 'Limits'], ['notifications', 'Notifications'], ['account', 'Account']];
(async () => {
  const browser = await chromium.launch({ executablePath: '/usr/bin/chromium', args: ['--no-sandbox'] });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();
  page.setDefaultTimeout(10000);
  const errors = [], mutations = [];
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('request', (request) => {
    if (request.url().includes('/api/') && !['GET', 'HEAD'].includes(request.method())) {
      mutations.push(request.method() + ' ' + new URL(request.url()).pathname);
    }
  });
  async function api(method, path, data) {
    const response = await context.request.fetch(base + path, { method, data, headers: { 'X-Relay-Request': '1', 'X-Relay-Client': 'web' } });  // (as Relay's pages: the user's own)
    assert.ok(response.ok(), await response.text());
    return response.json();
  }
  const category = page.getByLabel('Settings category');
  const heading = (label) => page.locator('.settings-page-head h3').filter({ hasText: new RegExp('^' + label + '$') });
  async function select(id, label) {
    if (page.viewportSize().width <= 800) await category.selectOption(id);
    else await page.locator('.settings nav').getByRole('button', { name: label, exact: true }).click();
    await heading(label).waitFor();
    if (id === 'computers') await page.locator('.computer-row').waitFor();
    assert.equal(await category.inputValue(), id);
    assert.equal(await page.locator('.settings nav button[aria-current=page]').innerText(), label);
    assert.equal(await page.locator('.settings .pane').getAttribute('aria-label'), label + ' settings');
  }
  async function fits() {
    assert.equal(await page.evaluate(() => document.scrollingElement.scrollWidth > innerWidth), false);
    const report = await page.evaluate(() => {
      const pane = document.querySelector('.settings .pane'), modal = document.querySelector('.settings-modal');
      const rect = pane.getBoundingClientRect(), box = modal.getBoundingClientRect();
      const overflow = [...pane.querySelectorAll('button,input,textarea,select,.provider-row,.model-row,.settings-actions,.settings-list,.settings-field-grid')]
        .filter((el) => el.getClientRects().length && !el.closest('.usage-scroll, .usage-bars'))
        .filter((el) => { const r = el.getBoundingClientRect(); return r.left < rect.left - .5 || r.right > rect.right + .5; })
        .map((el) => el.className + ' ' + el.textContent.slice(0, 50));
      return { pane: pane.scrollWidth > pane.clientWidth, modal: modal.scrollWidth > modal.clientWidth,
        outside: box.left < 0 || box.top < 0 || box.right > innerWidth + .5 || box.bottom > innerHeight + .5, overflow };
    });
    assert.deepEqual(report, { pane: false, modal: false, outside: false, overflow: [] });
  }
  async function snapshot() {
    return Promise.all(['/api/settings', '/api/limits', '/api/providers', '/api/modes', '/api/subagents', '/api/computers', '/api/skills', '/api/fallback-loops', '/api/memory']
      .map((path) => api('GET', path)));
  }
  try {
    await api('POST', '/api/setup', { username: 'ui', password: 'test-password' });
    await api('POST', '/api/modes', { name: 'A custom mode', description: 'Test description', access: 'read', instructions: 'Fixture instructions' });
    await api('POST', '/api/skills', { name: 'layout-test-skill', description: 'A test skill with a long readable description', instructions: 'Do the test job.', access: 'read' });
    await api('POST', '/api/computers', { name: 'A long computer name that must not hide its connector or remove actions' });
    await api('PUT', '/api/fallback-loops', { loops: [{ name: 'A test fallback loop with a long name', entries: [{ provider: 'fake', model: 'test/small' }] }] });
    await api('PUT', '/api/subagents', { same: true, free: true, local: false, others: 'ask', allowed: [{ provider: 'fake', model: 'test/large' }] });
    await api('POST', '/api/memory', { text: 'A long memory to check the layout: the user would like every answer in Dutch, with code and its comments in English, and short sentences.' });
    const task = await api('POST', '/api/conversations', { provider_id: 'fake', mode: 'code', directory: project, fallback_loop: 'off' });
    await api('POST', '/api/conversations/' + task.id + '/share', {});
    const before = await snapshot();
    await page.goto(base + '/');
    await page.getByLabel('Message', { exact: true }).fill('Keep this draft under settings');
    await page.locator('.composer input[type=file]').setInputFiles({ name: 'draft.txt', mimeType: 'text/plain', buffer: Buffer.from('draft file') });
    await page.locator('.composer .attachment .remove').waitFor();
    mutations.length = 0;
    await page.getByRole('button', { name: 'Settings', exact: true }).first().click();
    await page.locator('.settings-page-head h3').waitFor();
    assert.equal(await page.locator('.settings nav button').count(), tabs.length);
    assert.deepEqual(await category.locator('option').evaluateAll((options) => options.map((o) => [o.value, o.textContent])), tabs);
    // Browsing every category, including the responsive boundary, never saves or tests anything.
    for (const width of [1280, 801, 800, 390, 320]) {
      await page.setViewportSize({ width, height: 900 });
      for (const theme of ['light', 'dark']) {
        await page.evaluate(async (theme) => { const { setTheme } = await import('/static/js/theme.js'); setTheme(theme); }, theme);
        for (const [id, label] of tabs) {
          await select(id, label); await fits();
          if (width > 800) {
            assert.equal(await category.isVisible(), false);
            assert.equal(await page.locator('.settings nav button[aria-current=page]').isVisible(), true);
          } else {
            assert.equal(await category.isVisible(), true);
            assert.equal(await page.locator('.settings nav').isVisible(), false);
          }
          if (theme === 'light' && ((width === 1280 && ['providers', 'limits'].includes(id)) || (width === 320 && id === 'notifications'))) {
            await page.screenshot({ path: '/shots/' + id + '-' + width + '.png' });
          }
        }
      }
    }
    // Both navigation layouts are reachable and operable without a mouse.
    const close = page.locator('.settings-modal .modal-head').getByRole('button', { name: 'Close', exact: true });
    await page.setViewportSize({ width: 1280, height: 900 });
    await close.focus(); await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(() => document.activeElement.textContent.trim()), 'General');
    await page.keyboard.press('Tab'); await page.keyboard.press('Enter');
    await heading('Providers').waitFor();
    assert.equal(await page.evaluate(() => document.activeElement.textContent.trim()), 'Providers');
    assert.equal(await page.evaluate(() => getComputedStyle(document.activeElement).outlineStyle), 'solid');
    await page.keyboard.press('Shift+Tab'); await page.keyboard.press('Space');
    await heading('General').waitFor();
    await page.setViewportSize({ width: 320, height: 360 });
    await close.focus(); await page.keyboard.press('Tab');
    assert.equal(await category.evaluate((el) => el === document.activeElement), true);
    await page.keyboard.press('ArrowDown'); await heading('Providers').waitFor();
    assert.equal(await category.evaluate((el) => el === document.activeElement), true);
    await page.keyboard.press('End'); await heading('Account').waitFor(); await fits();
    await close.focus(); await page.keyboard.press('Shift+Tab');
    assert.equal(await page.evaluate(() => document.activeElement.textContent.trim()), 'an example');  // (the SDK's, last)
    await page.keyboard.press('Tab');
    assert.equal(await close.evaluate((el) => el === document.activeElement), true);
    assert.deepEqual(mutations, []);
    assert.deepEqual(await snapshot(), before, 'Layout/navigation must not change any configuration');
    // Desktop selection retains its button; switching layouts retains local form edits.
    await page.setViewportSize({ width: 1280, height: 900 });
    const limitsButton = page.locator('.settings nav').getByRole('button', { name: 'Limits', exact: true });
    await limitsButton.click(); await page.getByLabel('Dismiss after', { exact: true }).waitFor();
    assert.equal(await limitsButton.evaluate((button) => button === document.activeElement), true);
    const dismiss = page.getByLabel('Dismiss after', { exact: true }); await dismiss.fill('456');
    await page.setViewportSize({ width: 320, height: 900 });
    assert.equal(await dismiss.inputValue(), '456'); await fits();
    assert.equal(await page.locator('.settings .settings-field-grid .field')
      .evaluateAll((fields) => fields.some((field) => getComputedStyle(field).display === 'none')), false);
    const currentLimits = await api('GET', '/api/limits');
    assert.equal(currentLimits.tunables.find((t) => t.key === 'questions.dismiss_timeout').value, 300);
    assert.deepEqual(mutations, [], 'Typing must not save a manual form');
    await dismiss.locator('..').getByRole('button', { name: 'Default', exact: true }).click();
    assert.equal(await dismiss.inputValue(), '300');
    await dismiss.fill('456');
    const [saved] = await Promise.all([page.waitForRequest((r) => r.url().endsWith('/api/limits') && r.method() === 'PUT'),
      page.locator('.settings-actions').getByRole('button', { name: 'Save', exact: true }).click()]);
    const expected = Object.fromEntries(currentLimits.tunables.map((t) => [t.key, t.value])); expected['questions.dismiss_timeout'] = 456;
    assert.deepEqual(saved.postDataJSON(), { values: expected, server: { public_url: currentLimits.server.public_url, session_days: currentLimits.server.session_days } });
    await page.getByText('Limits saved', { exact: true }).waitFor();
    assert.equal((await api('GET', '/api/limits')).tunables.find((t) => t.key === 'questions.dismiss_timeout').value, 456);
    // Editors retain the existing actions and values.
    await page.setViewportSize({ width: 1280, height: 900 }); await select('providers', 'Providers');
    const row = page.locator('.provider-row').filter({ hasText: 'A very long provider name' });
    assert.equal(await row.getByLabel('Enable A very long provider name to exercise the settings action layout').isChecked(), true);
    await row.getByTitle('Edit', { exact: true }).click();
    const template = page.getByLabel('Template (JSON)'); await template.waitFor();
    assert.equal(JSON.parse(await template.inputValue()).id, 'fake');
    await page.setViewportSize({ width: 320, height: 900 }); await fits();
    await page.getByRole('button', { name: 'Back', exact: true }).click();
    await heading('Providers').waitFor();
    await row.getByRole('button', { name: 'Models', exact: true }).click();
    await page.locator('.model-row').first().waitFor(); await fits();
    assert.equal(await page.locator('.model-row').count(), 3);
    assert.equal(await page.getByRole('button', { name: 'Test all', exact: true }).count(), 1);
    await page.getByRole('button', { name: 'Back', exact: true }).click();
    await page.getByRole('button', { name: 'Add provider', exact: true }).click();
    await page.locator('.kind-card').first().waitFor(); await fits();
    await page.getByRole('button', { name: 'Back', exact: true }).click();
    await select('modes', 'Modes'); await page.getByRole('button', { name: 'Add mode', exact: true }).click();
    await page.getByLabel('Instructions', { exact: true }).waitFor(); await fits();
    await page.getByRole('button', { name: 'Cancel', exact: true }).click();
    await select('skills', 'Skills');
    await page.locator('.provider-row').filter({ hasText: 'layout-test-skill' }).getByTitle('Edit', { exact: true }).click();
    await page.locator('.skill-files').waitFor(); await fits();
    assert.equal(await page.getByRole('button', { name: 'Save', exact: true }).count(), 1);
    await page.getByRole('button', { name: 'Back', exact: true }).click();
    await page.getByRole('button', { name: 'New skill', exact: true }).click();
    await page.getByLabel('Instructions', { exact: true }).waitFor(); await fits();
    await page.getByRole('button', { name: 'Cancel', exact: true }).click();
    await page.locator('.provider-row').filter({ has: page.locator('.badge', { hasText: 'built in' }) }).first().getByTitle('View', { exact: true }).click();
    await page.locator('.skill-files').waitFor(); await fits();
    assert.equal(await page.locator('.settings textarea').getAttribute('readonly'), '');
    assert.equal(await page.getByRole('button', { name: 'Save', exact: true }).count(), 0);
    await page.getByRole('button', { name: 'Back', exact: true }).click();
    // A late category response stays in its detached pane rather than replacing the new category.
    let release, requested;
    const gate = new Promise((resolve) => { release = resolve; });
    const started = new Promise((resolve) => { requested = resolve; });
    await page.route('**/api/limits', async (route) => { requested(); await gate; await route.continue(); });
    await category.selectOption('limits'); await started;
    await select('general', 'General');
    const finished = page.waitForResponse((response) => response.url().endsWith('/api/limits'));
    release(); await finished;
    assert.equal(await page.locator('.settings-page-head h3').innerText(), 'General');
    await page.unroute('**/api/limits');
    assert.deepEqual(mutations, ['PUT /api/limits']);
    // A fallback loop's adder says the provider's models are loading, rather than showing another's meanwhile.
    let letModels;
    const modelsGate = new Promise((resolve) => { letModels = resolve; });
    await page.route('**/api/providers/fake/models', async (route) => { await modelsGate; await route.continue(); });
    await select('usage', 'Usage');
    const adder = page.locator('.loop-card .row-inline').first();
    await adder.locator('select').first().dispatchEvent('change');
    assert.deepEqual(await adder.locator('select').nth(1).locator('option').allInnerTexts(), ['Loading models…']);
    letModels();
    await adder.locator('select').nth(1).locator('option', { hasText: 'test/large' }).waitFor({ state: 'attached' });
    assert.deepEqual(await adder.locator('select').nth(1).locator('option').allInnerTexts(), ['Default model', 'test/small', 'test/large']);
    await page.unroute('**/api/providers/fake/models');
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('.settings-modal').count(), 0);
    assert.equal(await page.getByLabel('Message', { exact: true }).inputValue(), 'Keep this draft under settings');
    assert.equal(await page.locator('.composer .attachment .name').innerText(), 'draft.txt');
    await page.evaluate(async () => {
      const { openSettings } = await import('/static/js/settings.js'); const { settingsHooks } = await import('/static/js/main.js');
      openSettings({ ...settingsHooks(), tab: 'providers', models: 'fake' });
    });
    await page.locator('.model-row').first().waitFor(); await fits();
    assert.equal(await category.inputValue(), 'providers');
    await page.keyboard.press('Escape');
    await page.evaluate(async () => { const { openSettings } = await import('/static/js/settings.js'); openSettings({ tab: 'account', loginRequired: false }); });
    await page.locator('.settings-page-head h3').waitFor();
    assert.match(await page.locator('.settings-page-head .lead').innerText(), /Login is disabled/);
    assert.equal(await page.locator('.settings input').count(), 0); await fits();
    await page.keyboard.press('Escape');
    assert.deepEqual(mutations, ['PUT /api/limits']);
    // A name given in a dialog with Enter: the dialog closes and stays closed (that Enter isn't also the button's that
    // opened it, which has the focus back)
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.getByRole('button', { name: 'Settings', exact: true }).first().click();
    await select('usage', 'Usage');
    await page.locator('.loop-card', { hasText: 'A test fallback loop' }).getByRole('button', { name: 'Rename', exact: true }).click();
    await page.getByRole('dialog', { name: 'Rename loop' }).locator('input').fill('A renamed loop');
    await page.getByRole('dialog', { name: 'Rename loop' }).locator('input').press('Enter');
    await page.locator('.loop-card', { hasText: 'A renamed loop' }).waitFor();
    await page.waitForTimeout(300);
    assert.equal(await page.getByRole('dialog', { name: 'Rename loop' }).count(), 0);
    // Signing out (Account): the sign-in page, with nothing of the session left over it
    await select('account', 'Account');
    await page.locator('.settings').getByRole('button', { name: 'Sign out' }).click();
    await page.locator('.auth-card').waitFor();
    assert.equal(await page.locator('.modal').count(), 0);
    assert.deepEqual(errors, []);
    console.log('PASS: all settings categories, responsive navigation, stable focus, layouts/editors, unchanged values, no browsing writes and exact Save payload');
  } catch (error) { await page.screenshot({ path: '/shots/failure.png' }); throw error; }
  finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
