const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const root = path.resolve(__dirname, "../..");

function setup() {
  const stored = new Map();
  const toasts = [];
  const context = {
    toast: (text, kind) => toasts.push([text, kind]),
    localStorage:
      { getItem: (k) => (stored.has(k) ? stored.get(k) : null), setItem: (k, v) => stored.set(k, String(v)) },
    document: { title: "Relay", hidden: false },
    window: {},
  };
  vm.createContext(context);
  // (./base.js)
  vm.runInContext(fs.readFileSync(path.join(root, "src/relay/static/js/base.js"), "utf8")
    .replace(/^export /gm, ""), context);
  const source = fs.readFileSync(path.join(root, "src/relay/static/js/notifications.js"), "utf8")
    .replace(/^import .*;\n/gm, "").replace(/^export /gm, "");
  vm.runInContext(`${source}\nthis.RunWatcher = RunWatcher;`, context);
  return { context, toasts, watcher: new context.RunWatcher({ onOpen: () => {} }) };
}

const conv = (id, extra = {}) => ({ id, title: id, last_run: { id: `${id}-1`, status: "succeeded" }, ...extra });

test("this browser announces per category: the Governor's agents' done runs not by default", () => {
  const { context, toasts, watcher } = setup();
  const working = (id, extra) => conv(id, { running_run: "r", ...extra });
  watcher.update([working("mine"), working("agent", { managed: true }), working("broken", { managed: true })], null);
  watcher.update([
    conv("mine", { last_run: { id: "mine-2", status: "succeeded" } }),
    conv("agent", { managed: true, last_run: { id: "agent-2", status: "succeeded" } }),
    conv("broken", { managed: true, last_run: { id: "broken-2", status: "failed" } }),
  ], null);
  assert.deepEqual(toasts.map(([text]) => text), ["Finished: mine", "Failed: broken"]);
  context.setBrowserAnnounces("run.done", false);
  assert.equal(context.browserAnnounces("run.done"), false);
  assert.equal(context.browserAnnounces("agents.failed"), true);
});

test("an agent that starts waiting for you is announced once, unless you're watching it", () => {
  const { context, toasts, watcher } = setup();
  watcher.update([conv("a", { running_run: "r" }), conv("b", { running_run: "r" })], null);
  watcher.update([conv("a", { running_run: "r", needs_approval: true }),
    conv("b", { running_run: "r", needs_approval: true })], "b");
  watcher.update([conv("a", { running_run: "r", needs_approval: true })], null);
  assert.deepEqual(toasts.map(([text]) => text), ["Waiting for you: a"]);
  context.setBrowserAnnounces("approval", false);
  watcher.update([conv("c", { running_run: "r" })], null);
  watcher.update([conv("c", { running_run: "r", needs_approval: true })], null);
  assert.equal(toasts.length, 1);
});
