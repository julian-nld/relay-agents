const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const root = path.resolve(__dirname, "../..");

function setup() {
  const context = { h: (tag, attrs, ...children) => ({ tag, attrs, children }),
    relativeGroup: (updated) => updated > 100 ? "Today" : "Older" };
  vm.createContext(context);
  const source = fs.readFileSync(path.join(root, "src/relay/static/js/activity.js"), "utf8")
    .replace(/^import .*;\n/gm, "").replace(/^export /gm, "");
  vm.runInContext(source, context);
  return context;
}

test("activity uses explicit labels with attention and current work ahead of old failures", () => {
  const { conversationStatus: status } = setup();
  const conv = { last_run: { status: "failed" } };
  assert.equal(status(conv).label, "Failed");
  assert.equal(status({ ...conv, running_run: "r" }).label, "Working");
  assert.equal(status({ ...conv, needs_approval: true, running_run: "r" }).label, "Waiting for you");
  assert.equal(status({ ...conv, resume_at: 123 }).label, "Resuming later");
  assert.equal(status({ ...conv, git: { state: "discarded" } }).label, "Discarded");
  assert.equal(status(conv, { running: true }).kind, "working");
});

test("finished conversations use the latest saved answer before polling catches up", () => {
  const { conversationStatus: status } = setup();
  assert.equal(status({ messages: [{ role: "assistant", status: "failed" },
    { role: "user" }, { role: "assistant", status: "succeeded" }] }).label, "Completed");
  assert.equal(status({ last_run: { status: "interrupted" } }).label, "Stopped");
  assert.equal(status({}).label, "Ready");
});

test("review status needs authoritative changes, not just an unmerged task branch", () => {
  const { conversationStatus: status } = setup();
  const conv = { git: { mode: "worktree", state: "open" }, last_run: { status: "succeeded" } };
  assert.equal(status(conv).label, "Completed");
  assert.equal(status({ ...conv, changes: { files: 2 } }).label, "Needs review");
  assert.equal(status(conv, { group: "review" }).label, "Needs review");
  assert.equal(status({ ...conv, changes: { files: 2 }, git: { mode: "none" } }).label, "Completed");
  assert.equal(status({ ...conv, changes: { files: 2 }, shared_changes: true }).label, "Completed");
});

test("sidebar puts pending requests and working agents above pins and dated history", () => {
  const { sidebarGroups } = setup();
  const conversations = [
    { id: "p", pinned: true, updated: 80 }, { id: "old", updated: 40 },
    { id: "r", running_run: "run", updated: 200 },
    { id: "q", pinned: true, needs_approval: true, running_run: "run", updated: 110 },
    { id: "new", updated: 150 }, { id: "q2", needs_approval: true, updated: 130 },
  ];
  const groups = JSON.parse(JSON.stringify(sidebarGroups(conversations)));
  assert.deepEqual(groups.map(([name]) => name), ["Waiting for you", "Working", "Pinned", "Today", "Older"]);
  assert.deepEqual(groups[0][1].map((c) => c.id), ["q2", "q"]);
  assert.equal(groups.flatMap(([, items]) => items).length, conversations.length);
  assert.equal(conversations[0].id, "p", "grouping must not mutate shared state");
});

test("dated history stays chronological even though the API puts pinned items first", () => {
  const { sidebarGroups } = setup();
  const groups = JSON.parse(JSON.stringify(sidebarGroups([
    { id: "pin", pinned: true, updated: 1 }, { id: "older", updated: 90 }, { id: "today", updated: 150 },
  ])));
  assert.deepEqual(groups.map(([name]) => name), ["Pinned", "Today", "Older"]);
  assert.equal(sidebarGroups([]).length, 0);
});

test("AgentOS's conversations are one group, but for one waiting for you or pinned", () => {
  const { sidebarGroups } = setup();
  const GOVERNOR_GROUP = "AgentOS conversations";  // (a const of the module: not on the vm's context)
  const conversations = [
    { id: "own", updated: 150 }, { id: "gov", managed: true, updated: 160 },
    { id: "gov-working", managed: true, running_run: "run", updated: 120 },
    { id: "gov-asks", managed: true, needs_approval: true, running_run: "run", updated: 110 },
    { id: "gov-pinned", managed: true, pinned: true, updated: 90 }, { id: "old", managed: true, updated: 10 },
    { id: "mine-working", running_run: "run", updated: 170 },
  ];
  const groups = JSON.parse(JSON.stringify(sidebarGroups(conversations)));
  assert.deepEqual(groups.map(([name]) => name), ["Waiting for you", "Working", "Pinned", GOVERNOR_GROUP, "Today"]);
  const of = Object.fromEntries(groups.map(([name, items]) => [name, items.map((c) => c.id)]));
  assert.deepEqual(of[GOVERNOR_GROUP], ["gov", "gov-working", "old"]);  // newest first, working ones too
  assert.deepEqual(of["Waiting for you"], ["gov-asks"]);
  assert.deepEqual(of.Working, ["mine-working"]);
  assert.deepEqual(of.Pinned, ["gov-pinned"]);
  assert.deepEqual(of.Today, ["own"]);
  assert.equal(groups.flatMap(([, items]) => items).length, conversations.length);
});

test("task counts include work, waiting and authoritative review groups once", () => {
  const { taskCounts } = setup();
  const groups = { running: [{ id: "w" }, { id: "q", needs_approval: true }], review: [{ id: "r" }],
    clean: [{ id: "shared", shared_changes: true }], discarded: [{ id: "d" }] };
  const counts = JSON.parse(JSON.stringify(taskCounts(groups)));
  assert.deepEqual(counts, { total: 3, working: 1, waiting: 1, review: 1,
    label: "Working: 1 · Waiting for you: 1 · Needs review: 1" });
  assert.deepEqual(JSON.parse(JSON.stringify(taskCounts({ ...groups, running: [{ id: "w" }],
    waiting: [{ id: "q" }] }))), counts);
  assert.equal(taskCounts({ running: [], review: [], clean: groups.clean }).total, 0);
});

test("a service's WorkBoxes fold under its newest, but nothing that needs you or is open", () => {
  const { foldClones } = setup();
  const ids = (list) => JSON.parse(JSON.stringify(list.map((c) => c.id)));  // (out of the page's realm)
  const clone = (id, version, extra = {}) => ({ id, title: `notes: work on version ${version}`, updated: version,
    clone: { service_id: "s1", service: "notes", version }, ...extra });
  const chats = [clone("c5", 5), { id: "plain", updated: 9 }, clone("c4", 4), clone("c3", 3), clone("c2", 2),
    clone("c1", 1), { id: "other", updated: 1, clone: { service_id: "s2", service: "site", version: 1 } }];
  const folded = foldClones(chats);
  assert.deepEqual(ids(folded), ["c5", "plain", "other"]);
  assert.deepEqual(ids(folded[0].older), ["c4", "c3", "c2", "c1"]);  // newest first
  assert.equal(chats[0].older, undefined);  // (the list itself isn't changed)
  // Working, waiting for you, or the one open: in its own place
  const busy =
    foldClones([clone("c5", 5), clone("c4", 4, { running_run: "r" }), clone("c3", 3, { needs_approval: true }),
      clone("c2", 2), clone("c1", 1)], { open: "c2" });
  assert.deepEqual(ids(busy), ["c5", "c4", "c3", "c2"]);
  assert.deepEqual(ids(busy[0].older), ["c1"]);
  // The newest version leads, wherever it is in the list; a search folds nothing
  assert.equal(foldClones([clone("c1", 1), clone("c2", 2)])[0].id, "c2");
  assert.equal(foldClones(chats, { searching: true }), chats);
  // Of one version the clone itself leads, though a fork of it is newer
  const fork =
    { ...clone("f4", 4), updated: 99, clone: { service_id: "s1", service: "notes", version: 4, own: false } };
  const own = { ...clone("c4", 4), clone: { service_id: "s1", service: "notes", version: 4, own: true } };
  assert.deepEqual(ids(foldClones([fork, own, clone("c1", 1)])), ["c4"]);
  assert.equal(foldClones([clone("only", 1)])[0].older, undefined);
});

test("the Governor view's list: AgentOS's conversations by when, none set apart", () => {
  const { sidebarGroups } = setup();
  const conversations = [
    { id: "gov", managed: true, updated: 160 }, { id: "gov-working", managed: true, running_run: "run", updated: 120 },
    { id: "old", managed: true, updated: 10 },
  ];
  const groups = JSON.parse(JSON.stringify(sidebarGroups(conversations, { apart: false })));
  assert.deepEqual(groups.map(([name, items]) => [name, items.map((c) => c.id)]),
    [["Working", ["gov-working"]], ["Today", ["gov"]], ["Older", ["old"]]]);
});
