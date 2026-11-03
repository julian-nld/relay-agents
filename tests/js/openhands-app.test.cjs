// Relay's app in OpenHands' Agent Canvas (plugins/openhands/canvas-app/extension.js): its page registered with
// Canvas, and OpenHands' conversations matched to Relay's tasks.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const root = path.resolve(__dirname, "../..");

function setup() {
  const context = {};
  vm.createContext(context);
  const source = fs.readFileSync(path.join(root, "plugins/openhands/canvas-app/extension.js"), "utf8")
    .replace(/^export /gm, "");
  vm.runInContext(`${source}\nthis.exported = { activate, match, describe };`, context);
  return context.exported;
}

test("the app registers its page with Canvas' app API 1, and only that", () => {
  const { activate } = setup();
  const pages = [];
  const unregister = () => "gone";
  assert.equal(activate({ apiVersion: "1", registerPage: (id) => pages.push(id) && unregister }), unregister);
  assert.deepEqual(pages, ["relay"]);
  assert.throws(() => activate({ apiVersion: "2", registerPage: () => {} }), /API 1/);
  const manifest = JSON.parse(fs.readFileSync(path.join(root, "plugins/openhands/canvas-app/canvas-extension.json")));
  assert.deepEqual(manifest.contributes.pages.map((p) => p.id), ["relay"]);
  assert.equal(manifest.entrypoint, "extension.js");
});

test("OpenHands' conversations come with their Relay tasks, by their ACP session", () => {
  const { match } = setup();
  const conversations = [
    { id: "oh1", title: "Fix the header", execution_status: "running", agent_state: { acp_session_id: "r1" },
      workspace: { working_dir: "/projects/site" } },
    { id: "oh2", title: "", agent_state: { acp_session_id: "r2" } },
    { id: "oh3", title: "Its own agent", agent_state: {} },
    null, { title: "no id" },
  ];
  const groups = { running: [{ id: "r1", provider_id: "opencode", model: "opencode/big-pickle" }], review: [],
    clean: [], discarded: [] };
  const rows = match(conversations, groups);
  assert.deepEqual(rows.map((r) => [r.id, r.relayId, r.task?.group ?? null]),
    [["oh1", "r1", "running"], ["oh2", "r2", null], ["oh3", null, null]]);
  assert.equal(rows[0].folder, "/projects/site");
  assert.equal(rows[1].title, "Untitled");
  assert.equal(rows[1].status, "idle");
  // (not Relay's answer, or none: the conversations alone)
  assert.equal(match(conversations, undefined).filter((r) => r.task).length, 0);
  assert.equal(match(null, groups).length, 0);
});

test("a task's line says its state, its agent and its changes", () => {
  const { describe } = setup();
  assert.deepEqual(JSON.parse(JSON.stringify(describe({ group: "review", provider_id: "claude-code", model: "sonnet",
    changes: { files: 2, additions: 10, deletions: 3 } }))),
  { state: "To review", kind: "review", parts: ["claude-code · sonnet", "2 files +10 −3"] });
  assert.deepEqual(JSON.parse(JSON.stringify(describe({ group: "clean", provider_id: "opencode",
    changes: { files: 1, additions: 1 } }))), { state: "Done", kind: "clean", parts: ["opencode", "1 file +1 −0"] });
  assert.deepEqual(JSON.parse(JSON.stringify(describe({ group: "running", changes: null }))),
    { state: "Working", kind: "running", parts: [] });
  // (a question of its agent's comes first: OpenHands can't show it)
  assert.equal(describe({ group: "running", needs_approval: true }).state, "Asks you");
});
