const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const root = path.resolve(__dirname, "../..");

function setup() {
  const listeners = {};
  const view = { height: 800, scale: 1, offsetTop: 0,
    addEventListener(name, cb) { listeners[name] = cb; }, removeEventListener(name) { delete listeners[name]; } };
  const scrolled = [];
  const win = { visualViewport: view, scrollY: 0, scrollTo: (x, y) => { scrolled.push([x, y]); win.scrollY = y; } };
  const style = {};
  const page = { style: { setProperty: (name, value) => { style[name] = value; } } };
  const context = {};
  vm.createContext(context);
  const source =
    fs.readFileSync(path.join(root, "src/relay/static/js/viewport.js"), "utf8").replace("export function", "function");
  vm.runInContext(source + "\nthis.followVisualViewport = followVisualViewport;", context);
  const stop = context.followVisualViewport(win, page);
  return { view, win, style, scrolled, listeners, stop };
}

test("the layout follows the visual viewport: above the on-screen keyboard", () => {
  const { view, win, style, scrolled, listeners, stop } = setup();
  assert.equal(style["--app-height"], "800px");
  // The keyboard opens: iOS shrinks the visual viewport and scrolls the page to show the field
  view.height = 450.4;
  view.offsetTop = 300;
  win.scrollY = 300;
  listeners.resize();
  assert.equal(style["--app-height"], "450px");
  assert.deepEqual(scrolled.at(-1), [0, 0]);  // back to the top: the layout fits what's visible
  // Zoomed in: that's the user panning, not the keyboard
  view.scale = 2;
  view.height = 400;
  listeners.resize();
  assert.equal(style["--app-height"], "450px");
  // The keyboard closes
  view.scale = 1;
  view.height = 800;
  view.offsetTop = 0;
  listeners.resize();
  assert.equal(style["--app-height"], "800px");
  stop();
  assert.deepEqual(Object.keys(listeners), []);
});

test("without a visual viewport (an old browser), nothing changes", () => {
  const context = {};
  vm.createContext(context);
  const source =
    fs.readFileSync(path.join(root, "src/relay/static/js/viewport.js"), "utf8").replace("export function", "function");
  vm.runInContext(source + "\nthis.followVisualViewport = followVisualViewport;", context);
  const style = {};
  context.followVisualViewport({}, { style: { setProperty: (n, v) => { style[n] = v; } } })();
  assert.deepEqual(style, {});
});
