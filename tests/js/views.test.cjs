const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const root = path.resolve(__dirname, "../..");

// A module of static/js as plain functions on a context: its imports left out (what it needs of them: `needs`)
function load(file, needs = {}) {
  const context = { ...needs };
  vm.createContext(context);
  const source = fs.readFileSync(path.join(root, "src/relay/static/js", file), "utf8")
    .replace(/^import [^;]*;\n/gm, "").replace(/^export /gm, "");
  vm.runInContext(`${source}\nthis.exported = { ${(source.match(/^(?:const|function) (\w+)/gm) || [])
    .map((line) => line.split(" ")[1]).join(", ")} };`, context);
  return context.exported;
}

test("Relay starts in the view of the page it opens on; any other page keeps the chats", () => {
  const { viewForPath } = load("views.js");
  assert.equal(viewForPath("/"), "chats");
  assert.equal(viewForPath("/c/abc"), "chats");
  assert.equal(viewForPath("/c/abc", "abc"), "governor");
  assert.equal(viewForPath("/c/abc", "def"), "chats");
  assert.equal(viewForPath("/tasks"), "tasks");
  assert.equal(viewForPath("/agentos"), "agentos");
  assert.equal(viewForPath("/scheduled"), "agentos");
  assert.equal(viewForPath("/scripts"), "scripts");
  assert.equal(viewForPath("/scripts/private/report.py"), "scripts");
  assert.equal(viewForPath("/scriptsfoo"), "chats");
  assert.equal(viewForPath("/s/a07e5"), "production");
});

test("the column's width stays within bounds; what isn't a width gives the standard one", () => {
  const { clampWidth, WIDTH } = load("views.js");
  assert.equal(clampWidth(300), 300);
  assert.equal(clampWidth("301.6"), 302);
  assert.equal(clampWidth(100), WIDTH.min);
  assert.equal(clampWidth(9000), WIDTH.max);
  for (const odd of [null, undefined, "", "wide", NaN, -5, 0, Infinity]) assert.equal(clampWidth(odd), WIDTH.standard);
});

test("a script's address names it, each part decoded; a broken one opens the scripts' page", () => {
  const { scriptOfPath } = load("views.js");
  assert.equal(scriptOfPath("/scripts"), null);
  assert.equal(scriptOfPath("/scripts/"), null);
  assert.equal(scriptOfPath("/scripts/private/report.py"), "private/report.py");
  assert.equal(scriptOfPath("/scripts/public/my%20report.py"), "public/my report.py");
  assert.equal(scriptOfPath("/scripts/private/100%.py"), null);
});

test("the Tasks view groups as the Tasks page: approvals asked by a working task wait for you", () => {
  const { taskGroups } = load("tasks.js");
  const groups = JSON.parse(JSON.stringify(  // (the vm's arrays as this one's)
    taskGroups({ running: [{ id: "a" }, { id: "b", needs_approval: true }], clean: [{ id: "c" }] })));
  const of = Object.fromEntries(groups.map(([key, , tasks]) => [key, tasks.map((t) => t.id)]));
  assert.deepEqual(groups.map(([key]) => key), ["waiting", "running", "review", "clean", "discarded"]);
  assert.deepEqual(of, { waiting: ["b"], running: ["a"], review: [], clean: ["c"], discarded: [] });
  // (the page's own answer, its waiting ones apart already: as they are)
  const page = taskGroups({ waiting: [{ id: "w" }], running: [{ id: "r" }] });
  assert.equal(page[0][2].map((t) => t.id).join(), "w");
  assert.equal(taskGroups({}).every(([, , tasks]) => tasks.length === 0), true);
});
