const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const root = path.resolve(__dirname, "../..");

function setup() {
  const context = { h: () => ({}), icon: () => ({}) };
  vm.createContext(context);
  const source = fs.readFileSync(path.join(root, "src/relay/static/js/tool-activity.js"), "utf8")
    .replace(/^import .*;\n/gm, "").replace(/^export /gm, "");
  vm.runInContext(`${source}\nthis.toolKind = toolKind; this.activitySummary = activitySummary;`, context);
  return context;
}

test("a turn shows it updated Relay's memory, however the agent called it", () => {
  const { toolKind, activitySummary } = setup();
  for (const name of ["mcp__relay__memory_add", "relay_memory_change", "memory_forget", "remember", "forget_memory"]) {
    assert.equal(toolKind(name, {}), "memory", name);
  }
  assert.equal(toolKind("Bash", { command: 'relay memory add --category tools "Uses pnpm"' }), "memory");
  assert.equal(toolKind("mcp__relay__memory_list", {}), "search");  // (only a look)
  assert.equal(toolKind("Bash", { command: "relay memoryless" }), "command");
  assert.equal(activitySummary([{ kind: "memory", outcome: "done" }, { kind: "read", outcome: "done" }]),
    "1 memory updated · 1 file read");
  assert.equal(activitySummary([{ kind: "memory", outcome: "running" }]), "Updating memory");
});
