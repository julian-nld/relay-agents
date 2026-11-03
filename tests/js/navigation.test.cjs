const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const root = path.resolve(__dirname, "../..");

function setup(get) {
  const context = { state: {}, dom: {}, location: { pathname: "/" }, api: { get }, taskCounts: (groups) => groups,
    panelData: {}, renderBar() {}, renderPanel() {} };
  vm.createContext(context);
  // (./base.js)
  vm.runInContext(fs.readFileSync(path.join(root, "src/relay/static/js/base.js"), "utf8")
    .replace(/^export /gm, ""), context);
  const source = fs.readFileSync(path.join(root, "src/relay/static/js/main.js"), "utf8");
  vm.runInContext(source.slice(source.indexOf("let taskSnapshot = null;"),
    source.indexOf("// The column's lists other than")), context);
  return context;
}

test("Tasks counts reuse unchanged snapshots and the overview's existing poll", async () => {
  let calls = 0;
  const groups = { total: 3 };
  const env = setup(async (path) => { assert.equal(path, "/api/tasks"); calls++; return groups; });
  const conversations = [{ id: "c", updated: 1 }];
  await env.loadTaskCounts(conversations);
  await env.loadTaskCounts(conversations);
  assert.equal(calls, 1);
  assert.equal(env.state.taskCounts, groups);
  env.location.pathname = "/tasks";
  await env.loadTaskCounts([{ id: "c", updated: 2 }]);
  assert.equal(calls, 1);
  env.location.pathname = "/scheduled";
  await env.loadTaskCounts([{ id: "c", updated: 2 }]);
  assert.equal(calls, 2);
});

test("failed badge refreshes retain the last good count and can retry", async () => {
  let fail = true, calls = 0;
  const fresh = { total: 2 }, previous = { total: 3 };
  const env = setup(async () => { calls++; if (fail) throw new Error("offline"); return fresh; });
  env.state.taskCounts = previous;
  await env.loadTaskCounts([]);
  assert.equal(env.state.taskCounts, previous);
  fail = false;
  await env.loadTaskCounts([]);
  assert.equal(calls, 2);
  assert.equal(env.state.taskCounts, fresh);
});

test("older badge responses cannot overwrite a newer refresh or overview count", async () => {
  const pending = [];
  const env = setup(() => new Promise((resolve) => pending.push(resolve)));
  const first = env.loadTaskCounts([{ id: "c", updated: 1 }]);
  const second = env.loadTaskCounts([{ id: "c", updated: 2 }]);
  const fresh = { total: 4 };
  pending[1](fresh); await second;
  pending[0]({ total: 1 }); await first;
  assert.equal(env.state.taskCounts, fresh);
  const third = env.loadTaskCounts([{ id: "c", updated: 3 }]);
  const overview = { total: 5 };
  env.updateTaskCounts(overview);
  pending[2]({ total: 2 }); await third;
  assert.equal(env.state.taskCounts, overview);
});

test("sidebar conversations show chat, code and computer icons in the same style", () => {
  const icons = [], tags = [];
  const context = { state: { current: null }, watcher: { isUnread: () => false },
    conversationStatus: () => ({ kind: "quiet" }),
    h: (tag, ...rest) => { tags.push([tag, rest.flat()]); return { addEventListener() {} }; },
    icon: (name, cls) => { icons.push({ name, cls }); return { name }; } };
  vm.createContext(context);
  // (./base.js)
  vm.runInContext(fs.readFileSync(path.join(root, "src/relay/static/js/base.js"), "utf8")
    .replace(/^export /gm, ""), context);
  const activity = fs.readFileSync(path.join(root, "src/relay/static/js/activity.js"), "utf8")
    .replace(/^import [^;]*;\n/gm, "").replace(/^export /gm, "");
  vm.runInContext(`${activity}\nthis.chatIcon = chatIcon;`, context);
  const source = fs.readFileSync(path.join(root, "src/relay/static/js/main.js"), "utf8");
  vm.runInContext(source.slice(source.indexOf("function conversationItem("),
    source.indexOf("function renameInline(")), context);
  for (const [conv, expected] of [[{ mode: "chat" }, "chat"], [{ mode: "code" }, "code"],
    [{ mode: "chat", computer: { id: "pc" } }, "monitor"]]) {
    icons.length = 0;
    context.conversationItem({ id: "c", title: "Test", ...conv });
    assert.deepEqual(icons.filter((icon) => icon.cls === "sm mode"), [{ name: expected, cls: "sm mode" }]);
  }
  // Managed by AgentOS: the same icon, with "os" by it (the Tasks view and page use chatIcon too)
  tags.length = 0;
  context.chatIcon({ mode: "code", managed: 1 });
  const wrap = tags.find(([tag]) => tag === "span.mode-wrap.managed");
  assert.ok(wrap && wrap[1].some((part) => part?.name === "code"));
  assert.ok(tags.some(([tag, parts]) => tag === "span.os" && parts.includes("os")));
  tags.length = 0;
  context.chatIcon({ mode: "code" });
  assert.equal(tags.length, 0);
});
