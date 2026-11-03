const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const root = path.resolve(__dirname, "../..");

function setup() {
  const context = {};
  vm.createContext(context);
  const source = fs.readFileSync(path.join(root, "src/relay/static/js/limits.js"), "utf8")
    .replace(/^import .*;\n/gm, "").replace(/^export /gm, "");
  vm.runInContext(source, context);
  return context;
}

const NOW = 1_000_000;
const row = (name, windows, extra = {}) => ({ name, also: [], windows, ...extra });

test("the ring shows the window with the least left, across logins", () => {
  const { tightest } = setup();
  const claude = row("Claude Code", [{ label: "5-hour", used: 0.49 }, { label: "Weekly", used: 0.7 }]);
  const chatgpt = row("ChatGPT", [{ label: "5-hour", used: 0.95, resets_at: NOW + 600 }]);
  const found = tightest([claude, chatgpt], NOW);
  assert.equal(found.row, chatgpt);
  assert.equal(found.window.label, "5-hour");
  assert.ok(Math.abs(found.left - 0.05) < 1e-9);
  assert.equal(tightest([claude], NOW).window.label, "Weekly");
});

test("a window that reset since it was told has all of it left again", () => {
  const { tightest, tone } = setup();
  const found = tightest([row("Claude Code", [{ label: "5-hour", used: 1, resets_at: NOW - 1 },
    { label: "Weekly", used: 0.5, resets_at: NOW + 3600 }])], NOW);
  assert.equal(found.window.label, "Weekly");
  assert.equal(tone(found.left), "ok");
  const used = tightest([row("Fake Limited", [{ label: "5-hour", used: 1, resets_at: NOW + 60 }])], NOW);
  assert.equal(used.left, 0);
  assert.equal(tone(used.left), "bad");
  assert.equal(tone(0.15), "warn");
});

test("no ring when no provider tells a window", () => {
  const { tightest } = setup();
  assert.equal(tightest([], NOW), null);
  assert.equal(tightest(null, NOW), null);
  // (a login with no limits, and one that couldn't be asked and never ran into one)
  assert.equal(tightest([row("Copilot", []), row("Claude Code", [], { error: "not signed in" })], NOW), null);
});
