const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const root = path.resolve(__dirname, "../..");

function setup(options = {}) {
  const elements = [];
  let tray;
  function h(tag) {
    const element = { tag, value: "", style: {}, scrollHeight: 20, callbacks: {}, disabled: false, isConnected: true,
      classList: { toggle() {} }, replaceChildren() {}, append() {}, focus() {}, querySelectorAll() { return []; },
      setAttribute(name, value) { this[name] = value; }, removeAttribute(name) { delete this[name]; },
      addEventListener(name, fn) { this.callbacks[name] = fn; }, setSelectionRange() {} };
    elements.push(element);
    return element;
  }
  class AttachmentTray {
    constructor({ onChange }) { tray = this; this.items = []; this.el = {}; this.onChange = onChange; }
    busy() { return this.items.some((i) => i.uploading); }
    ids() { return this.items.filter((i) => i.id).map((i) => i.id); }
    remove(item) { this.items = this.items.filter((i) => i !== item); this.onChange(); }
  }
  const context = { h, icon() {}, clear() {}, closeMenu() {}, showMenu() {}, toast() {},
    acceptFiles() {}, AttachmentTray, innerHeight: 800, document: { activeElement: null } };
  vm.createContext(context);
  // (./base.js)
  vm.runInContext(fs.readFileSync(path.join(root, "src/relay/static/js/base.js"), "utf8")
    .replace(/^export /gm, ""), context);
  const source = fs.readFileSync(path.join(root, "src/relay/static/js/composer.js"), "utf8")
    .replace(/^import .*;\n/gm, "").replace(/^export /gm, "");
  vm.runInContext(source, context);
  const composer = context.createComposer({ placeholder: "Test", onSend: async () => true, ...options });
  return { composer, tray, elements, textarea: elements.find((e) => e.tag === "textarea"),
    send: elements.find((e) => e.tag === "button.send-btn") };
}

for (const queue of [false, true]) {
  test(`${queue ? "queue" : "send"} retains text and uploads added while awaiting acknowledgement`, async () => {
    let finish;
    const sent = [];
    const submit = (text, ids) => { sent.push([text, Array.from(ids)]); return new Promise((r) => { finish = r; }); };
    const env = setup(queue ? { running: true, onQueue: submit } : { onSend: submit });
    const old = { id: "old", name: "a", size: 1, file: { type: "text/plain" } };
    const added = { id: "new", name: "b", size: 2, file: { type: "text/plain" } };
    env.tray.items.push(old);
    env.composer.setText("Sending");
    const pending = env.send.callbacks.click();
    env.composer.setText("Next message");
    env.tray.items.push(added);
    assert.equal(env.send.disabled, true);
    await env.send.callbacks.click(); // a second click/Enter must not send twice
    finish(true);
    await pending;
    assert.deepEqual(sent, [["Sending", ["old"]]]);
    assert.equal(env.textarea.value, "Next message");
    assert.deepEqual(env.tray.items, [added]);
    assert.equal(env.send.disabled, false);
  });
}

test("success clears the unchanged draft; refusal and errors retain it", async () => {
  for (const outcome of [true, false, "throw"]) {
    const env = setup({ onSend: async () => { if (outcome === "throw") throw new Error("offline"); return outcome; } });
    env.composer.setText("Draft");
    if (outcome === "throw") await assert.rejects(env.send.callbacks.click(), /offline/);
    else await env.send.callbacks.click();
    assert.equal(env.textarea.value, outcome === true ? "" : "Draft");
    assert.equal(env.send.disabled, outcome === true);
  }
});

test("settings refresh keeps the composer, live stream and answer view", () => {
  const current = { id: "c", running_run: "r" };
  const following = { runId: "r", source: {}, view: {} };
  let pickers = 0, headers = 0;
  const composer = { refreshPickers() { pickers++; }, draft: "Unsent text", attachments: ["file"] };
  const context = { state: { current, following }, dom: { composer },
    renderHeader() { headers++; }, renderConversation() { throw new Error("live DOM replaced"); },
    showNewChat() { throw new Error("wrong page"); } };
  vm.createContext(context);
  // (./base.js)
  vm.runInContext(fs.readFileSync(path.join(root, "src/relay/static/js/base.js"), "utf8")
    .replace(/^export /gm, ""), context);
  const source = fs.readFileSync(path.join(root, "src/relay/static/js/main.js"), "utf8");
  vm.runInContext(source.slice(source.indexOf("function rerenderMain()"),
    source.indexOf("export function renderHeader()")), context);
  context.rerenderMain();
  assert.equal(context.dom.composer, composer);
  assert.equal(context.state.following, following);
  assert.equal(composer.draft, "Unsent text");
  assert.deepEqual(composer.attachments, ["file"]);
  assert.equal(pickers, 1);
  assert.equal(headers, 1);
});

for (const route of ["/", "/tasks", "/scheduled"]) {
  test(`settings refresh preserves the draft and route on ${route}`, () => {
    let pickers = 0, headers = 0, prepared = 0;
    const composer = { el: { isConnected: route === "/" }, draft: "Unsent text", attachments: ["file"],
      refreshPickers() { pickers++; } };
    const context = { state: { current: null, draft: { mode: "chat" } }, dom: { composer },
      location: { pathname: route }, prepareDraft() { prepared++; }, renderHeader() { headers++; },
      showNewChat() { throw new Error("page replaced"); } };
    vm.createContext(context);
  // (./base.js)
  vm.runInContext(fs.readFileSync(path.join(root, "src/relay/static/js/base.js"), "utf8")
    .replace(/^export /gm, ""), context);
    const source = fs.readFileSync(path.join(root, "src/relay/static/js/main.js"), "utf8");
    vm.runInContext(source.slice(source.indexOf("function rerenderMain()"),
      source.indexOf("export function renderHeader()")), context);
    context.rerenderMain();
    assert.equal(context.location.pathname, route);
    assert.equal(composer.draft, "Unsent text");
    assert.deepEqual(composer.attachments, ["file"]);
    assert.equal(pickers, route === "/" ? 1 : 0);
    assert.equal(prepared, route === "/" ? 1 : 0);
    assert.equal(headers, 1);
  });
}

test("advanced options stay open without replacing the draft, uploads or primary controls", () => {
  let label = "Options";
  const { composer, tray, textarea, elements } = setup({
    pickers: () => ({ primary: ["provider", "model"], advanced: ["mode", "loop"], label }),
  });
  const options = elements.find((e) => e.tag === "details.composer-options");
  assert.equal(options.hidden, false);
  options.open = true;
  textarea.value = "Keep my draft";
  tray.items.push({ id: "file", file: { type: "text/plain" } });
  label = "Options · Plan";
  composer.refreshPickers();
  assert.equal(options.open, true);
  assert.equal(textarea.value, "Keep my draft");
  assert.deepEqual(tray.ids(), ["file"]);
  assert.ok(elements.some((e) => e.textContent === "Options · Plan"));
});

test("composers with no advanced controls do not show an empty disclosure", () => {
  const { elements } = setup({ pickers: () => ["provider", "model"] });
  assert.equal(elements.find((e) => e.tag === "details.composer-options").hidden, true);
});

function key(env, extra = {}) {
  const event = { key: "Tab", shiftKey: true, preventDefault() { this.prevented = true; }, ...extra };
  return { event, pending: env.textarea.callbacks.keydown(event) };
}

test("Shift+Tab cycles the mode without clearing slash text, uploads or Options", async () => {
  let mode = "Build";
  const env =
    setup({ commands: () => [{ id: "mode", cycle: async () => { mode = mode === "Build" ? "Plan" : "Build"; } }] });
  assert.equal(env.textarea["aria-keyshortcuts"], "Shift+Tab");
  env.textarea.value = "/mode unfinished draft";
  const options = env.elements.find((el) => el.tag === "details.composer-options");
  options.open = true;
  env.tray.items.push({ id: "file", file: { type: "text/plain" } });
  const first = key(env); assert.equal(first.event.prevented, true); await first.pending;
  assert.equal(mode, "Plan");
  await key(env).pending; assert.equal(mode, "Build");
  assert.equal(env.textarea.value, "/mode unfinished draft");
  assert.deepEqual(env.tray.ids(), ["file"]); assert.equal(options.open, true);
});

test("Stop stays available while a mode save is pending", async () => {
  let finish, stopped = 0;
  const env = setup({ running: true, onStop: () => { stopped++; }, commands: () => [{ id: "mode",
    cycle: () => new Promise((resolve) => { finish = resolve; }) }] });
  const first = key(env);
  assert.equal(env.send.disabled, false);
  await env.send.callbacks.click(); assert.equal(stopped, 1);
  finish(); await first.pending;
});

test("quick mode shortcuts serialize changes and block sending until the last save", async () => {
  const finish = [];
  let mode = 0, sent = 0;
  const env = setup({ onSend: async () => { sent++; }, commands: () => [{ id: "mode",
    cycle: () => new Promise((resolve) => finish.push(() => { mode++; resolve(); })) }] });
  env.textarea.value = "Draft";
  const first = key(env);
  key(env); key(env);
  assert.equal(finish.length, 1); assert.equal(env.send.disabled, true);
  await env.send.callbacks.click(); assert.equal(sent, 0);
  finish.shift()(); await new Promise(setImmediate);
  assert.equal(mode, 1); assert.equal(finish.length, 1);
  finish.shift()(); await new Promise(setImmediate);
  finish.shift()(); await first.pending;
  assert.equal(mode, 3); assert.equal(env.send.disabled, false);
});

test("Shift+Tab leaves native navigation alone without cycling, with modifiers and during IME", async () => {
  for (const commands of [[], [{ id: "mode", agent: true, cycle: () => { throw Error("agent command"); } }],
    [{ id: "mode", cycle: null }]]) {
    const env = setup({ commands: () => commands });
    assert.equal(key(env).event.prevented, undefined);
  }
  let cycled = 0;
  const env = setup({ commands: () => [{ id: "mode", cycle: async () => { cycled++; } }] });
  for (const modifier of ["ctrlKey", "altKey", "metaKey", "isComposing"]) {
    assert.equal(key(env, { [modifier]: true }).event.prevented, undefined);
  }
  const repeated = key(env, { repeat: true });
  assert.equal(repeated.event.prevented, true); assert.equal(cycled, 0);
  assert.equal(key(env, { shiftKey: false }).event.prevented, undefined);
});

test("failed saves and removed composers do not send or apply queued modes elsewhere", async () => {
  let reject, applied = 0;
  const env = setup({ commands: () => [{ id: "mode", cycle: () => new Promise((_, fail) => { reject = fail; }) }] });
  env.textarea.value = "Draft";
  const first = key(env); key(env);
  reject(Error("offline")); await first.pending;
  assert.equal(env.textarea.value, "Draft"); assert.equal(env.send.disabled, false);
  const removed = setup({ commands: () => [{ id: "mode", cycle: async () => { applied++; } }] });
  removed.composer.el.isConnected = false;
  await key(removed).pending; assert.equal(applied, 0);
  let finish;
  const navigating = setup({ commands: () => [{ id: "mode", cycle: () => {
    applied++; return new Promise((resolve) => { finish = resolve; });
  } }] });
  const pending = key(navigating); key(navigating);
  navigating.composer.el.isConnected = false;
  finish(); await pending.pending;
  assert.equal(applied, 1, "navigation discards queued changes after the in-flight save");
});

test("mode cycling shares picker order and defaults, including custom modes and No mode", async () => {
  for (const tools of [true, false]) {
    let selected = null;
    const modes = tools
      ? [{ id: "build", name: "Build" }, { id: "plan", name: "Plan" }, { id: "custom", name: "Custom" }]
      : [{ id: "custom", name: "Custom" }];
    const context = { modesFor: () => modes, provider: () => ({ tools }), toast() {} };
    vm.createContext(context);
  // (./base.js)
  vm.runInContext(fs.readFileSync(path.join(root, "src/relay/static/js/base.js"), "utf8")
    .replace(/^export /gm, ""), context);
    const pickers = fs.readFileSync(path.join(root, "src/relay/static/js/pickers.js"), "utf8");
    vm.runInContext(pickers.slice(pickers.indexOf("export function modeChoices"),
      pickers.indexOf("export function modePicker"))
      .replace(/^export /gm, ""), context);
    const commands = fs.readFileSync(path.join(root, "src/relay/static/js/commands.js"), "utf8");
    vm.runInContext(commands.slice(commands.indexOf("function settingCommands"),
      commands.indexOf("// /thinking")), context);
    const cycle = async () => context.settingCommands({ providerId: "p", modeId: selected,
      set: async (changes) => { selected = changes.mode; } }).find((c) => c.id === "mode").cycle();
    await cycle(); assert.equal(selected, tools ? "plan" : "custom");
    await cycle(); assert.equal(selected, tools ? "custom" : null);
    if (tools) { await cycle(); assert.equal(selected, null); }
  }
});

test("A skill's command shows the first sentence of its description, not cut at an e.g.", () => {
  const commands = fs.readFileSync(path.join(root, "src/relay/static/js/commands.js"), "utf8");
  const context = { state: { skills: [{ name: "relay-computers", description: "Work on the user's own computers "
    + "(run commands there), e.g. to change a setting on their PC. Use when the user asks for \"my computer\"." }] } };
  vm.createContext(context);
  vm.runInContext(commands.slice(commands.indexOf("function skillCommands"),
    commands.indexOf("function helpCommand")), context);
  assert.equal(context.skillCommands()[0].description,
    "Work on the user's own computers (run commands there), e.g. to change a setting on their PC.");
});
