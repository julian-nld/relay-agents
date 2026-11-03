const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const root = path.resolve(__dirname, "../..");

function setup() {
  const context = {};
  vm.createContext(context);
  const source = fs.readFileSync(path.join(root, "src/relay/static/js/service.js"), "utf8")
    .replace(/^import [\s\S]*?;\n/gm, "").replace(/^export /gm, "");
  vm.runInContext(`${source}\nthis.incidentsOf = incidentsOf;`, context);
  return context;
}

test("a service's incidents: how long each lasted, and what made it well again", () => {
  const { incidentsOf } = setup();
  const service = { health_ok: 1, health_at: 10000 };
  // Failing, well again by a version that went live (Relay says so), failing again and still
  const changes = [{ at: 100, ok: 0, detail: "no connection" }, { at: 820, ok: 1, detail: "version 3 live (HTTP 200)" },
    { at: 2000, ok: 0, detail: "HTTP 500" }];
  const found = incidentsOf(changes, { health_ok: 0 });
  assert.equal(found.length, 2);
  assert.deepEqual([found[0].at, found[0].until], [2000, undefined]);  // (the latest first: still going on)
  assert.deepEqual([found[1].until, found[1].fixed], [820, "version 3 live (HTTP 200)"]);
  // Kept without its end (from before a version said so): it ended when the next version went live
  const old = incidentsOf([{ at: 100, ok: 0, detail: "no connection" }], service,
    [{ number: 2, promoted_at: 50 }, { number: 3, promoted_at: 820 }]);
  assert.deepEqual([old[0].until, old[0].fixed], [820, "version 3 live"]);
  // No version since: well when Relay last saw it so
  assert.equal(incidentsOf([{ at: 100, ok: 0 }], service, [])[0].until, 10000);
  assert.equal(incidentsOf([], service).length, 0);
});

test("a service's light: red when its version never came up, grey when stopped or not checked yet", () => {
  const context = setup();
  context.h = (tag, attrs) => ({ tag, title: attrs.title });
  context.statusDot = (status, title) => ({ status, title });
  const dot = (s) => vm.runInContext(`healthDot(${JSON.stringify(s)})`, context);
  // (a simulated user saw one that never came up shown green)
  assert.equal(dot({ live: null, versions: [{ status: "failed" }] }).status, "fail");
  assert.deepEqual(dot({ live: null, stopped: true, versions: [{ status: "failed" }] }),
    { tag: "span.status-dot", title: "Stopped" });
  assert.equal(dot({ live: null, versions: [{ status: "archived" }] }).title, "Nothing live");
  assert.equal(dot({ live: { number: 1 }, health_ok: null }).title, "Not checked yet");
  assert.deepEqual(dot({ live: { number: 1 }, health_ok: 1, health_detail: "HTTP 200" }),
    { status: "ok", title: "Well (HTTP 200)" });
  assert.equal(dot({ live: { number: 1 }, health_ok: 0, health_detail: "HTTP 500" }).status, "fail");
});
