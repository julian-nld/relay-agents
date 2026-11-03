"""Motion stays subtle, optional and independent of focus or streamed content."""

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


def test_motion_and_immediate_interaction_in_chromium(tmp_path, monkeypatch):
    if not shutil.which("podman"):
        pytest.skip("Chromium UI checks need podman")
    registry = Registry(tmp_path / "providers")
    registry.add({"id": "fake", "name": "Test agent", "kind": "command",
                  "options": {"command": ["true"], "models": ["test/model"]}})
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
        assert app.state.relay.db.one("SELECT count(*) AS n FROM runs")["n"] == 0
    finally:
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
  const page = await context.newPage(); page.setDefaultTimeout(10000);
  const errors = [], writes = [];
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('request', (request) => {
    if (request.url().includes('/api/') && !['GET', 'HEAD'].includes(request.method())) writes.push(request.method());
  });
  async function api(method, path, data) {
    const response = await context.request.fetch(base + path, { method, data, headers: { 'X-Relay-Request': '1', 'X-Relay-Client': 'web' } });  // (as Relay's pages: the user's own)
    assert.ok(response.ok(), await response.text()); return response.json();
  }
  async function animation(selector, name, reduced) {
    const style = await page.locator(selector).evaluate((el) => {
      const css = getComputedStyle(el); return { name: css.animationName, duration: parseFloat(css.animationDuration) };
    });
    assert.equal(style.name, name);
    assert.equal(style.duration < .001, reduced);
    if (!reduced) assert.ok(style.duration <= .22, 'animations must not slow interaction');
  }
  async function kept() {
    assert.equal(await page.getByLabel('Message', { exact: true }).inputValue(), 'Keep my draft');
    assert.match(await page.locator('.composer .attachment').innerText(), /^draft\.txt\n5 B$/);
  }
  try {
    await api('POST', '/api/setup', { username: 'ui', password: 'test-password' });
    const before = await api('GET', '/api/settings');
    await page.goto(base + '/');
    await page.getByLabel('Message', { exact: true }).fill('Keep my draft');
    await page.locator('.composer input[type=file]').setInputFiles({ name: 'draft.txt', mimeType: 'text/plain', buffer: Buffer.from('draft') });
    await page.locator('.composer .attachment .remove').waitFor();
    writes.length = 0;
    for (const reduced of [false, true]) {
      await page.emulateMedia({ reducedMotion: reduced ? 'reduce' : 'no-preference' });
      await page.setViewportSize({ width: 1280, height: 900 });
      // Menu positioning stays stationary, and keyboard input works during its fade.
      await page.evaluate(async () => {
        const { showMenu } = await import('/static/js/ui.js');
        showMenu(document.querySelector('.composer .picker'), [{ label: 'More models', onclick() {},
          submenu: () => [{ label: 'Option', onclick() {} }] }]);
      });
      await animation('.menu', 'fade', reduced);
      assert.equal(await page.evaluate(() => document.activeElement.getAttribute('role')), 'menuitem');
      assert.equal(await page.locator('.menu').evaluate((el) => {
        const box = el.getBoundingClientRect(); return Math.abs(box.x - parseFloat(el.style.left)) < .5;
      }), true, 'animation must not move submenu anchors');
      await page.keyboard.press('ArrowRight'); await page.locator('.menu.submenu').waitFor();
      await animation('.menu.submenu', 'fade', reduced);
      await page.keyboard.press('ArrowLeft');
      assert.equal(await page.locator('.submenu').count(), 0);
      await page.keyboard.press('Escape');
      assert.equal(await page.locator('.menu').count(), 0);
      assert.equal(await page.evaluate(() => document.activeElement.matches('.composer .picker')), true);
      // Dialog focus and immediate dismissal do not wait for visual effects.
      await page.evaluate(async () => {
        const { modal, h } = await import('/static/js/ui.js');
        const m = modal('Motion fixture'); m.body.append(h('input.input', { 'aria-label': 'Fixture input' }));
      });
      await animation('.modal', 'rise', reduced);
      assert.equal(await page.evaluate(() => !!document.activeElement.closest('.modal')), true);
      await page.keyboard.press('Tab');
      assert.equal(await page.evaluate(() => document.activeElement.getAttribute('aria-label')), 'Fixture input');
      await page.keyboard.press('Escape');
      assert.equal(await page.locator('.modal-backdrop').count(), 0);
      assert.equal(await page.evaluate(() => document.activeElement.matches('.composer .picker')), true);
      await page.evaluate(async () => {
        const { toast } = await import('/static/js/ui.js');
        window.motionAction = 0;
        toast('Motion fixture', 'info', { label: 'Act now', onclick: () => { window.motionAction++; } });
      });
      await animation('.toast', 'rise', reduced);
      await page.locator('.toast-action').click();
      assert.equal(await page.evaluate(() => window.motionAction), 1);
      assert.equal(await page.locator('.toast').count(), 0);
      await page.getByRole('button', { name: 'Settings', exact: true }).first().click();
      await page.locator('.settings-page-head h3').waitFor();
      await animation('.settings .pane', 'fade', reduced);
      await page.locator('.settings nav').getByRole('button', { name: 'Notifications', exact: true }).click();
      await animation('.settings .pane', 'fade', reduced);
      await page.keyboard.press('Escape'); await kept();
      // The native Options disclosure animates without replacing the composer or upload.
      const options = page.locator('.composer-options');
      await options.locator('summary').click();
      assert.equal(await options.getAttribute('open'), '');
      const disclosure = await options.evaluate((el) => ({
        supported: CSS.supports('interpolate-size: allow-keywords') && CSS.supports('selector(details::details-content)'),
        duration: parseFloat(getComputedStyle(el, '::details-content').transitionDuration),
      }));
      if (disclosure.supported) assert.equal(disclosure.duration < .001, reduced);
      await options.locator('summary').click(); await kept();
      // A desktop-collapsed sidebar still opens on mobile, and its closing overlay never blocks input.
      // (the column hidden on a desktop: its own button; the bar stays)
      await page.locator('.panel-view.shown').getByRole('button', { name: 'Hide the column', exact: true }).click();
      assert.equal(await page.locator('.layout.sidebar-collapsed').count(), 1);
      for (const width of [390, 320]) {
        await page.setViewportSize({ width, height: 844 });
        await page.getByRole('button', { name: 'Open sidebar', exact: true }).click();
        await page.waitForFunction(() => document.querySelector('.sidebar').getBoundingClientRect().left >= -.5);
        const drawer = await page.locator('.sidebar').evaluate((el) => {
          const css = getComputedStyle(el); return { property: css.transitionProperty, duration: parseFloat(css.transitionDuration) };
        });
        assert.equal(drawer.property, 'transform');
        assert.equal(drawer.duration < .001, reduced);
        assert.equal(await page.locator('.overlay').evaluate((el) => getComputedStyle(el).pointerEvents), 'auto');
        await page.locator('.overlay').click({ position: { x: width - 10, y: 30 } });
        assert.equal(await page.locator('.overlay').evaluate((el) => getComputedStyle(el).pointerEvents), 'none');
        await page.waitForFunction(() => document.querySelector('.sidebar').getBoundingClientRect().right < 0);
        assert.equal(await page.evaluate(() => document.scrollingElement.scrollWidth > innerWidth), false);
        await kept();
      }
      await page.setViewportSize({ width: 1280, height: 900 });
      await page.locator('.activity-bar .bar-item[data-view=chats]').click();  // (a view's icon shows the column again)
      assert.equal(await page.locator('.layout.sidebar-collapsed').count(), 0);
      // Individual Bash/Read cards animate their own details, not their command/output text.
      const toolMotion = await page.evaluate(async () => {
        const { AssistantView } = await import('/static/js/render.js');
        const { ThreadScroll } = await import('/static/js/thread-scroll.js');
        const { h } = await import('/static/js/ui.js');
        const thread = h('div.thread', h('div.md', Array.from({ length: 20 }, (_, i) => h('p', `Earlier paragraph ${i}`))));
        const scroller = h('div.scroller', thread);
        Object.assign(scroller.style, { position: 'fixed', top: '0', left: '0', height: '240px', width: '320px', zIndex: '10000' });
        document.body.append(scroller);
        const scroll = new ThreadScroll(scroller, thread);
        const view = new AssistantView({ live: true, onActivity: () => scroll.changed() });
        thread.append(view.el);
        const frame = () => new Promise(requestAnimationFrame);
        const sample = async (body) => {
          const heights = [];
          for (let i = 0; i < 24; i++) { heights.push(body.getBoundingClientRect().height); await frame(); }
          return heights;
        };
        try {
          const command = 'Inspect the fixture environment: ' + 'long-command '.repeat(30);
          view.add({ type: 'tool_call', id: 'bash', name: 'Bash', input: { command, description: 'Inspect fixture' } });
          const card = view.tools.get('bash'), head = card.el.querySelector('.tool-head'), body = card.el.querySelector('.tool-body');
          const readableRunning = card.summary.textContent === 'Inspect fixture' && card.state.textContent === 'Running'
            && JSON.parse(card.inputPre.textContent).command === command;
          card.inputPre.tabIndex = 0;
          await sample(body);
          const closed = body.getBoundingClientRect().height;
          head.focus(); head.click();
          const opening = await sample(body);
          const opened = body.getBoundingClientRect().height;
          scroll.jump(); await frame();
          view.add({ type: 'tool_result', id: 'bash', ok: false, output: 'Exit code 2\n' + 'Fixture output\n'.repeat(30) });
          const immediateFailure = head.getAttribute('aria-expanded') === 'true' && !body.inert
            && card.el.parentElement === view.body && card.outputPre.textContent.startsWith('Exit code 2');
          const readableFailure = card.summary.textContent === 'Inspect fixture' && card.state.textContent === 'Failed'
            && card.error.textContent === 'Exit code 2' && !card.error.hidden;
          await sample(body);
          const follows = scroll.following && scroll.atBottom();
          const icon = getComputedStyle(card.state.querySelector('.icon'));
          const output = getComputedStyle(card.outputSection);
          const quietText = [card.inputPre, card.outputPre, card.summary].every((el) => {
            const css = getComputedStyle(el); return css.animationName === 'none' && css.transform === 'none';
          });
          const fits = scroller.scrollWidth <= scroller.clientWidth;
          const narrowStatus = card.state.getBoundingClientRect().top > card.summary.getBoundingClientRect().bottom;
          // Leave later content beneath the reading anchor, even when the card collapses.
          thread.append(h('div.md', Array.from({ length: 20 }, (_, i) => h('p', `Later paragraph ${i}`))));
          await sample(body);
          scroller.scrollTop += head.getBoundingClientRect().top - scroller.getBoundingClientRect().top - 30;
          await frame(); scroll.following = false; scroll.rememberAnchor();
          const anchor = scroll.anchor.el, top = anchor.getBoundingClientRect().top;
          head.focus({ preventScroll: true }); head.click();
          card.inputPre.focus({ preventScroll: true });
          const blocked = body.inert && body.getAttribute('aria-hidden') === 'true' && document.activeElement === head;
          const closing = await sample(body), afterClose = body.getBoundingClientRect().height;
          const reading = !scroll.following && Math.abs(anchor.getBoundingClientRect().top - top) < 2;
          // Rapid reversal has no stale callback or late close.
          head.click(); await frame(); head.click(); await frame(); head.click();
          await sample(body);
          const reversed = head.getAttribute('aria-expanded') === 'true' && !body.inert && body.getBoundingClientRect().height > 0;
          view.add({ type: 'tool_call', id: 'read', name: 'Read', input: { file_path: '/fixture/missing-file' } });
          view.add({ type: 'tool_result', id: 'read', ok: false, output: 'File does not exist.' });
          const read = view.tools.get('read');
          const secondFailure = read.el.parentElement === view.body && read.el.classList.contains('open');
          const readableRead = read.state.textContent === 'Failed' && read.error.textContent === 'File does not exist.';
          view.finish('interrupted');
          // An open helper does not open/rotate its own nested, closed tool card.
          view.subagent({ id: 'helper', label: 'Fixture helper', status: 'succeeded' });
          const helper = view.subagents.get('helper');
          helper.el.querySelector('.tool-head').click();
          const nested = new AssistantView(); helper.thread.append(nested.el);
          nested.add({ type: 'tool_call', id: 'nested', name: 'Read', input: { file_path: '/fixture/file' } });
          nested.add({ type: 'tool_result', id: 'nested', ok: true, output: 'Fixture contents' });
          const inner = nested.tools.get('nested'); inner.group.el.open = true;
          const completed = inner.state.textContent === 'Completed';
          await sample(inner.el.querySelector('.tool-body'));
          const nestedClosed = inner.el.querySelector('.tool-head').getAttribute('aria-expanded') === 'false'
            && inner.el.querySelector('.tool-body').inert && inner.el.querySelector('.tool-body').getBoundingClientRect().height === 0
            && getComputedStyle(inner.el.querySelector('.tool-head .chev')).transform === 'none';
          return { closed, opened, afterClose,
            opening: opening.some((height) => height > closed + 1 && height < opened - 1),
            closing: closing.some((height) => height > afterClose + 1 && height < closing[0] - 1),
            immediateFailure, secondFailure, follows, blocked, reading, reversed, quietText, fits, nestedClosed,
            readableRunning, readableFailure, readableRead, narrowStatus, completed,
            icon: { name: icon.animationName, duration: parseFloat(icon.animationDuration) },
            output: { name: output.animationName, duration: parseFloat(output.animationDuration) } };
        } finally { scroll.destroy(); scroller.remove(); }
      });
      assert.equal(toolMotion.closed, 0); assert.equal(toolMotion.afterClose, 0);
      assert.ok(toolMotion.opened > 0);
      assert.equal(toolMotion.opening, !reduced); assert.equal(toolMotion.closing, !reduced);
      for (const key of ['immediateFailure', 'secondFailure', 'follows', 'blocked', 'reading', 'reversed', 'quietText', 'fits', 'nestedClosed',
        'readableRunning', 'readableFailure', 'readableRead', 'narrowStatus', 'completed']) {
        assert.equal(toolMotion[key], true, key);
      }
      for (const state of [toolMotion.icon, toolMotion.output]) {
        assert.equal(state.name, 'fade'); assert.equal(state.duration < .001, reduced);
      }
      // Streaming containers have no entrance/repeated animations or transforms.
      assert.equal(await page.evaluate(async () => {
        const { AssistantView } = await import('/static/js/render.js');
        const view = new AssistantView({ live: true }); document.querySelector('.main').append(view.el);
        view.add({ type: 'text', delta: 'First chunk' });
        await new Promise(requestAnimationFrame);
        view.add({ type: 'text', delta: ' and another chunk' });
        await new Promise(requestAnimationFrame);
        const quiet = [view.el, view.body, view.body.querySelector('.md')].every((el) => {
          const css = getComputedStyle(el); return css.animationName === 'none' && css.transform === 'none';
        });
        view.el.remove(); return quiet;
      }), true);
      await kept();
    }
    assert.deepEqual(await api('GET', '/api/settings'), before);
    assert.deepEqual(writes, []); assert.deepEqual(errors, []);
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
