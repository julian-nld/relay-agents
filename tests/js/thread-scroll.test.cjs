const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const root = path.resolve(__dirname, "../..");

function setup() {
  const frames = new Map();
  let id = 0, top = 800, anchorTop = 0;
  const scroller = { scrollHeight: 1000, clientHeight: 200, callbacks: {},
    get scrollTop() { return top; },
    set scrollTop(value) { top = Math.max(0, Math.min(value, this.scrollHeight - this.clientHeight)); },
    getBoundingClientRect: () => ({ top: 0 }),
    addEventListener(name, cb) { this.callbacks[name] = cb; },
    removeEventListener(name) { delete this.callbacks[name]; } };
  const anchor =
    { isConnected: true, getBoundingClientRect: () => ({ top: anchorTop, bottom: anchorTop + 30, height: 30 }) };
  const thread = { callbacks: {}, querySelectorAll: () => [anchor],
    addEventListener(name, cb) { this.callbacks[name] = cb; },
    removeEventListener(name) { delete this.callbacks[name]; } };
  let observer;
  const context = { h: (tag, attrs) => ({ tag, ...(typeof attrs === "object" ? attrs : {}) }), icon() {},
    ResizeObserver:
      class { constructor(cb) {
        this.cb = cb;
        observer = this;
      } observe() {} disconnect() { this.disconnected = true; } },
    requestAnimationFrame(cb) { const key = ++id; frames.set(key, cb); return key; },
    cancelAnimationFrame(key) { frames.delete(key); } };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(root, "src/relay/static/js/thread-scroll.js"), "utf8")
    .replace(/^import .*;\n/gm, "").replace(/^export /gm, "") + "\nthis.ThreadScroll = ThreadScroll;", context);
  const scroll = new context.ThreadScroll(scroller, thread);
  const flush = () => { const calls = [...frames.values()]; frames.clear(); calls.forEach((cb) => cb()); };
  return { scroll, scroller, thread, observer, frames, flush,
    moveAnchor: (by) => { anchorTop += by; } };
}

test("large streamed chunks and delayed layout keep following when already at the bottom", () => {
  const { scroll, scroller, observer, flush } = setup();
  scroller.scrollHeight += 800;
  scroll.changed();
  flush();
  assert.equal(scroller.scrollTop, 1600);
  scroller.scrollHeight += 200;
  observer.cb();
  flush();
  assert.equal(scroller.scrollTop, 1800);
  assert.equal(scroll.button.hidden, true);
});

test("reading back preserves position and exposes new activity until Latest is clicked", () => {
  const { scroll, scroller, flush } = setup();
  scroller.scrollTop = 200;
  scroller.callbacks.scroll();
  assert.equal(scroll.following, false);
  assert.equal(scroll.button.hidden, false);
  scroller.scrollHeight += 500;
  scroll.changed();
  flush();
  assert.equal(scroller.scrollTop, 200);
  assert.match(scroll.label.textContent, /New activity/);
  scroll.button.onclick();
  flush();
  assert.equal(scroller.scrollTop, 1300);
  assert.equal(scroll.unread, false);
  assert.equal(scroll.button.hidden, true);
});

test("delayed programmatic scroll events do not stop following after another result grows", () => {
  const { scroll, scroller, observer, flush } = setup();
  scroll.jump(); flush();
  scroller.scrollHeight += 600;
  scroller.callbacks.scroll();
  assert.equal(scroll.following, true);
  observer.cb(); flush();
  assert.equal(scroller.scrollTop, 1400);
  scroller.scrollTop = 500;
  scroller.callbacks.scroll();
  assert.equal(scroll.following, false, "a real readback scroll still pauses immediately");
});

test("anchor compensation does not turn readback into following", () => {
  const { scroll, scroller, moveAnchor, observer, flush } = setup();
  scroller.scrollTop = 600;
  scroller.callbacks.scroll();
  moveAnchor(150); observer.cb(); flush();
  assert.equal(scroller.scrollTop, 750);
  scroller.callbacks.scroll();
  assert.equal(scroll.following, false, "compensation close to the bottom is not a request to follow");
});

test("scrolling up before a queued frame prevents a late automatic jump", () => {
  const { scroll, scroller, flush } = setup();
  scroll.changed();
  scroller.scrollTop = 100;
  scroller.callbacks.scroll();
  flush();
  assert.equal(scroller.scrollTop, 100);
});

test("growth above the reading anchor is compensated, including nested subagents", () => {
  const { scroller, observer, moveAnchor, flush } = setup();
  scroller.scrollTop = 200;
  scroller.callbacks.scroll();
  moveAnchor(150);
  observer.cb();
  flush();
  assert.equal(scroller.scrollTop, 350);
});

test("expanding details pauses following, and disposal cancels stale work and listeners", () => {
  const { scroll, thread, observer, frames, flush } = setup();
  thread.callbacks.click({ target: { closest: () => ({}) } });
  assert.equal(scroll.following, false);
  scroll.changed();
  assert.equal(frames.size, 1);
  scroll.destroy();
  scroll.changed();
  observer.cb();
  flush();
  assert.equal(frames.size, 0);
  assert.equal(observer.disconnected, true);
  assert.equal(thread.callbacks.click, undefined);
});
