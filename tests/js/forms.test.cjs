const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const root = path.resolve(__dirname, "../..");

function setup({ details = false } = {}) {
  const elements = [];
  const streams = [];
  class Element {
    constructor(tag, attrs, children) {
      this.tag = tag.split(".")[0]; this.classes = tag.split(".").slice(1);
      this.children = []; this.text = ""; this.value = ""; this.checked = false; this.callbacks = {};
      this.dataset = {};
      this.classList = {
        contains: (name) => this.classes.includes(name),
        add: (name) => { if (!this.classes.includes(name)) this.classes.push(name); },
        remove: (name) => { this.classes = this.classes.filter((c) => c !== name); },
        toggle: (name, on = !this.classes.includes(name)) => {
          on ? this.classList.add(name) : this.classList.remove(name);
          return on;
        },
      };
      for (const [key, value] of Object.entries(attrs || {})) {
        if (key.startsWith("on")) this.callbacks[key.slice(2)] = value;
        else this[key] = value;
      }
      this.append(...children);
    }
    append(...children) {
      for (const child of children.flat(Infinity).filter((c) => c !== null && c !== undefined)) {
        if (child instanceof Element) { child.remove(); child.parent = this; }
        this.children.push(child);
      }
    }
    replaceChildren(...children) { this.children = []; this.text = ""; this.append(...children); }
    get lastChild() { return this.children.at(-1); }
    get className() { return this.classes.join(" "); }
    set className(value) { this.classes = value.split(" "); }
    get childElementCount() { return this.children.filter((c) => c instanceof Element).length; }
    set textContent(text) { this.text = text; this.children = []; }
    get textContent() {
      return this.text + this.children.map((c) => c instanceof Element ? c.textContent : String(c)).join("");
    }
    remove() { if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this); }
    addEventListener(name, fn) { this.callbacks[name] = fn; }
    setAttribute(name, value) { this[name] = String(value); }
    getAttribute(name) { return this[name] ?? null; }
    querySelectorAll(selector) {
      return this.children.filter((c) => c instanceof Element).flatMap((c) => [
        ...(selector.startsWith(".") ? c.classes.includes(selector.slice(1)) : c.tag === selector) ? [c] : [],
        ...c.querySelectorAll(selector),
      ]);
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0]; }
  }
  function h(tag, attrs, ...children) {
    if (attrs instanceof Element || typeof attrs === "string" || Array.isArray(attrs)) {
      children.unshift(attrs); attrs = {};
    }
    const el = new Element(tag, attrs, children);
    elements.push(el);
    return el;
  }
  const timers = new Map();
  let counter = 0;
  const context =
    { h, icon: () => h("svg"), clock: (seconds) => String(seconds), requestAnimationFrame: (f) => f(),
      renderMarkdown() {}, copyText() {},
      localStorage: { getItem: (key) => key === "relay.details" && details ? "show" : null, setItem() {} },
      document: { querySelectorAll: (selector) => elements.filter((el) => selector === ".tool:not(.subagent)"
        ? el.classes.includes("tool") && !el.classes.includes("subagent")
        : selector === "details.tool-activity" ? el.classes.includes("tool-activity") : false) },
      EventSource: class {
        static CLOSED = 2;
        constructor(url) { this.url = url; this.listeners = {}; streams.push(this); }
        addEventListener(name, cb) { this.listeners[name] = cb; }
        close() { this.closed = true; }
      }, streams,
      setTimeout(fn) { const id = ++counter; timers.set(id, fn); return id; },
      clearTimeout(id) { timers.delete(id); }, timers };
  vm.createContext(context);
  // (./base.js)
  vm.runInContext(fs.readFileSync(path.join(root, "src/relay/static/js/base.js"), "utf8")
    .replace(/^export /gm, ""), context);
  for (const file of ["forms.js", "outcomes.js", "tool-activity.js", "render.js"]) {
    const source = fs.readFileSync(path.join(root, "src/relay/static/js", file), "utf8")
      .replace(/^import .*;\n/gm, "").replace(/^export /gm, "");
    vm.runInContext(source, context);
  }
  vm.runInContext("this.FormCard = FormCard; this.AssistantView = AssistantView;", context);
  return context;
}

const form = { type: "form", id: "frm_1", title: "GPU questions", status: "pending", fields: [
  { key: "use", type: "multiselect", title: "Main use?", required: true, custom: true,
    options: [{ value: "Gaming", label: "Gaming", description: "High FPS" }, { value: "AI", label: "AI" }] },
  { key: "gpu", type: "string", title: "Current GPU?", custom: true,
    options: [{ value: "Recent", label: "Recent mid-range", description: "RTX 3060" }] },
] };
const plain = (v) => JSON.parse(JSON.stringify(v));

test("a stopped answer says why when it wasn't the user", () => {
  const env = setup();
  const yours = new env.AssistantView();
  yours.add({ type: "done", status: "interrupted", why: "the user stopped it" });
  assert.match(yours.body.textContent, /Stopped\.$/);
  const restarted = new env.AssistantView();
  restarted.add({ type: "done", status: "interrupted", why: "Relay restarted" });
  assert.match(restarted.body.textContent, /Stopped: Relay restarted\./);
});

test("review actions show file counts and distinguish project files from separate branches", () => {
  const env = setup();
  const view = new env.AssistantView();
  let reviews = 0;
  view.setChanges({ files: 2, additions: 3, deletions: 1 }, () => reviews++, { mode: "direct" });
  assert.match(view.body.textContent, /Review changes · 2 files/);
  assert.match(view.body.textContent, /Already in your project/);
  view.body.querySelector(".turn-changes").callbacks.click();
  assert.equal(reviews, 1);
  view.setChanges({ files: 1, additions: 1, deletions: 0 }, () => reviews++, { mode: "worktree", base_branch: "main" });
  assert.equal(view.body.querySelectorAll(".turn-review").length, 1);
  assert.match(view.body.textContent, /On a separate branch/);
  view.setChanges({ files: 0 }, () => {});
  assert.equal(view.body.querySelectorAll(".turn-review").length, 0);
});

test("tool and subagent disclosures expose their own body and current expanded state", () => {
  const env = setup();
  const view = new env.AssistantView({ live: true });
  const tool = view.toolCard("read-1", "read_file");
  const button = tool.el.querySelector(".tool-head");
  assert.equal(button.getAttribute("aria-expanded"), "false");
  assert.equal(button.getAttribute("aria-controls"), tool.el.querySelector(".tool-body").id);
  assert.equal(tool.el.querySelector(".tool-body").inert, true);
  assert.equal(tool.el.querySelector(".tool-body").getAttribute("aria-hidden"), "true");
  button.callbacks.click();
  assert.equal(button.getAttribute("aria-expanded"), "true");
  assert.equal(tool.el.querySelector(".tool-body").inert, false);
  assert.equal(tool.el.querySelector(".tool-body").getAttribute("aria-hidden"), "false");
  env.setShowDetails(false);
  assert.equal(button.getAttribute("aria-expanded"), "false");
  assert.equal(tool.el.querySelector(".tool-body").inert, true);
  view.toolResult({ id: "read-1", ok: false, output: "Failed" });
  assert.equal(button.getAttribute("aria-expanded"), "true");
  assert.equal(tool.el.querySelector(".tool-body").inert, false, "failure opens details immediately");
  view.subagent({ id: "child", task: "Check it", status: "succeeded" });
  const child = view.subagents.get("child").el;
  const head = child.querySelector(".tool-head");
  assert.equal(head.getAttribute("aria-expanded"), "false");
  assert.equal(child.querySelector(".tool-body").inert, true);
  assert.equal(head.getAttribute("aria-controls"), child.querySelector(".tool-body").id);
  assert.notEqual(head.getAttribute("aria-controls"), button.getAttribute("aria-controls"));
  env.setShowDetails(true);
  assert.equal(head.getAttribute("aria-expanded"), "false");
  head.callbacks.click();
  assert.equal(head.getAttribute("aria-expanded"), "true");
  assert.equal(child.querySelector(".tool-body").inert, false);
});

test("tool captions prefer descriptions, retain path/search fallbacks and never change full input", () => {
  const env = setup();
  env.setProjectFolder("/workspace/project");
  const input = { command: "echo one;\necho two", description: "Inspect shell, host and environment" };
  assert.equal(env.toolCaption(input), input.description);
  assert.equal(env.toolCaption({}), "");
  assert.equal(env.toolCaption({ command: "pwd", description: "   " }), "pwd");
  assert.equal(env.toolCaption({ file_path: "/workspace/project/file.txt" }), "file.txt");
  assert.equal(env.toolCaption({ pattern: "needle", path: "/workspace/project/src" }), "needle  in src");
  const view = new env.AssistantView({ live: true });
  view.add({ type: "tool_call", id: "bash", name: "Bash", input });
  const card = view.tools.get("bash");
  assert.equal(card.title.textContent, "Bash");
  assert.equal(card.summary.textContent, input.description);
  assert.equal(card.summary.title, input.description);
  assert.equal(card.state.textContent, "Running");
  assert.equal(card.inputPre.textContent, JSON.stringify(input, null, 2));
  assert.equal(card.separator.hidden, false);
  const output = "Exit code 2\nhost=fixture\nFull diagnostic output.";
  view.add({ type: "tool_result", id: "bash", name: "Bash", ok: false, output });
  assert.equal(card.summary.textContent, input.description);
  assert.equal(card.error.textContent, "Exit code 2");
  assert.equal(card.error.hidden, false);
  assert.equal(card.state.textContent, "Failed");
  assert.equal(card.outputPre.textContent, output);
  card.el.querySelector(".tool-head").callbacks.click();
  assert.equal(card.error.hidden, false, "collapsing details keeps the failure preview visible");
  assert.equal(card.outputPre.textContent, output);
});

test("tool failure previews use reported diagnostics, including structured and empty results", () => {
  const { toolFailure } = setup();
  for (const [output, expected] of [["Exit code 2\nMore output", "Exit code 2"],
    ["Progress\nProcess exited with code -1", "Exit code -1"],
    ["File does not exist. Note: the working directory is /fixture", "File does not exist."],
    ["\u001b[31mPermission denied\u001b[0m\nMore output", "Permission denied"],
    ["Progress\nTypeError: Invalid value", "TypeError: Invalid value"],
    [{ error: { message: "Permission denied" } }, "Permission denied"],
    [{ message: "Not available" }, "Not available"], [{ exit_code: 127 }, "Exit code 127"],
    [{ exitCode: 2 }, "Exit code 2"], [{ stderr: "Missing program" }, "Missing program"],
    [{ data: [1, 2] }, "See Output for details."], [null, "No error details were provided."],
    ["  \n", "No error details were provided."]]) {
    assert.equal(toolFailure(output), expected);
  }
});

test("tool states retain their labels on replay, stop honestly and avoid flashing duplicate results", () => {
  for (const live of [false, true]) {
    const env = setup();
    const view = new env.AssistantView({ live });
    view.add({ type: "tool_result", id: "late", ok: false, output: "File does not exist." });
    const late = view.tools.get("late");
    view.add({ type: "tool_call", id: "late", name: "Read", input: { file_path: "/fixture/missing" } });
    assert.equal(late.title.textContent, "Read");
    assert.equal(late.state.textContent, "Failed");
    assert.equal(late.summary.textContent, "/fixture/missing");
    assert.equal(late.error.textContent, "File does not exist.");
    const glyph = late.state.children[0];
    const text = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(late.outputPre), "textContent");
    let rewrites = 0;
    Object.defineProperty(late.outputPre, "textContent", { get: () => text.get.call(late.outputPre),
      set: (value) => { rewrites++; text.set.call(late.outputPre, value); } });
    view.add({ type: "tool_result", id: "late", ok: false, output: "File does not exist." });
    assert.equal(late.state.children[0], glyph, "duplicate events do not restart status-icon fades");
    assert.equal(rewrites, 0, "duplicate results keep the existing output text node/selection");
    view.add({ type: "tool_call", id: "done", name: "mcp__files__Read", input: {} });
    view.add({ type: "tool_result", id: "done", ok: true, output: 0 });
    const done = view.tools.get("done");
    assert.equal(done.state.textContent, "Completed");
    assert.equal(done.outputPre.textContent, "0");
    assert.equal(done.outputSection.classList.contains("hidden"), false);
    view.add({ type: "tool_call", id: "stopped", name: "custom_tool", input: {} });
    const stopped = view.tools.get("stopped");
    view.add({ type: "done", status: "interrupted" });
    assert.equal(stopped.state.textContent, "Stopped");
    assert.equal(stopped.error.hidden, true);
    assert.equal(done.state.textContent, "Completed");
    assert.equal(late.state.textContent, "Failed");
  }
});

test("thought duration labels format elapsed time without inventing timing for old chats", () => {
  const { thoughtLabel } = setup();
  for (const [milliseconds, label] of [[null, "Thought"], [undefined, "Thought"], [NaN, "Thought"],
    [0, "Thought for <1s"], [900, "Thought for <1s"], [12000, "Thought for 12s"],
    [68000, "Thought for 1m 08s"], [3599600, "Thought for 1h 00m 00s"], [3723000, "Thought for 1h 02m 03s"]]) {
    assert.equal(thoughtLabel(milliseconds), label);
  }
});

test("thinking durations stay identical in live views and replay, across separate blocks and stops", () => {
  for (const live of [true, false]) {
    const env = setup();
    const view = new env.AssistantView({ live });
    view.add({ type: "reasoning", delta: "First thought", reasoning_ms: 0 });
    const first = view.block;
    assert.equal(first.label.textContent, live ? "Thinking…" : "Thought");
    view.add({ type: "reasoning", delta: " continued", reasoning_ms: 8000 });
    view.add({ type: "text", delta: "Answer", reasoning_ms: 12000 });
    assert.equal(first.label.textContent, "Thought for 12s");
    assert.equal(first.text.textContent, "First thought continued");
    assert.equal(first.el.open, false);
    view.add({ type: "reasoning", delta: "Second thought", reasoning_ms: 0 });
    const second = view.block;
    view.add({ type: "done", status: "interrupted", reasoning_ms: 68000 });
    assert.equal(second.label.textContent, "Thought for 1m 08s");
    assert.equal(first.label.textContent, "Thought for 12s");
    const legacy = new env.AssistantView();
    legacy.add({ type: "reasoning", delta: "Old thought" });
    const old = legacy.block;
    legacy.add({ type: "text", delta: "Old answer" });
    assert.equal(old.label.textContent, "Thought");
  }
});

test("reasoning deltas batch within a frame and flush before changing phase", () => {
  const env = setup();
  const frames = [];
  env.requestAnimationFrame = (callback) => frames.push(callback);
  const view = new env.AssistantView({ live: true });
  view.add({ type: "reasoning", delta: "One", reasoning_ms: 0 });
  const first = view.block;
  view.add({ type: "reasoning", delta: " two", reasoning_ms: 1000 });
  assert.equal(frames.length, 1);
  assert.equal(first.text.textContent, "");
  frames.shift()();
  assert.equal(first.text.textContent, "One two");
  view.add({ type: "reasoning", delta: " three", reasoning_ms: 2000 });
  view.add({ type: "tool_call", id: "one", name: "shell", input: {}, reasoning_ms: 3000 });
  assert.equal(first.text.textContent, "One two three", "changing phases cannot lose buffered text");
  assert.equal(first.label.textContent, "Thought for 3s");
  frames.shift()();
  assert.equal(first.text.textContent, "One two three");
});

test("live activity stays open between consecutive calls and collapses when the block ends", () => {
  const env = setup();
  const view = new env.AssistantView({ live: true });
  view.toolCall({ id: "one", name: "shell", input: { command: "pwd" } });
  const group = view.activity;
  assert.equal(group.el.open, true);
  assert.equal(view.tools.get("one").el.classList.contains("open"), false, "tool input/output stays optional");
  view.toolResult({ id: "one", ok: true });
  assert.equal(group.el.open, true, "waiting for the next call does not end the block");
  view.toolCall({ id: "two", name: "shell", input: { command: "ls" } });
  assert.equal(view.activity, group);
  view.toolResult({ id: "two", ok: true });
  assert.equal(group.el.open, true);
  assert.match(group.label.textContent, /2 commands/);
  view.appendReasoning("Next steps");
  assert.equal(group.el.open, false);
  view.toolCall({ id: "three", name: "read_file", input: { path: "file.txt" } });
  const next = view.activity;
  assert.notEqual(next, group);
  assert.equal(next.el.open, true);
  view.finish("interrupted");
  assert.equal(next.el.open, false);
  assert.equal(view.tools.get("three").outcome, "stopped");
});

test("activity respects manual toggles while running and Show details on completion", () => {
  const env = setup();
  const view = new env.AssistantView({ live: true });
  view.toolCall({ id: "one", name: "shell", input: {} });
  const group = view.activity;
  group.el.open = false;
  view.toolResult({ id: "one", ok: true });
  view.toolCall({ id: "two", name: "shell", input: {} });
  assert.equal(group.el.open, false, "updates must not force a manually closed block open");
  env.setShowDetails(true);
  view.toolResult({ id: "two", ok: true });
  view.appendText("Finished");
  assert.equal(group.el.open, true);
  group.el.open = false;
  group.refresh();
  assert.equal(group.el.open, false, "completed summaries must not reset manual toggles");
});

test("activity waits for outstanding parallel calls after a boundary, using the current details preference", () => {
  const env = setup();
  const view = new env.AssistantView({ live: true });
  view.toolCall({ id: "one", name: "shell", input: {} });
  view.toolCall({ id: "two", name: "shell", input: {} });
  const group = view.activity;
  view.toolResult({ id: "one", ok: false });
  assert.equal(view.activity, null);
  assert.equal(group.el.open, true, "a parallel tool is still running");
  assert.equal(view.tools.get("one").el.parent, view.body, "failure stays outside the group");
  env.setShowDetails(true);
  view.toolResult({ id: "two", ok: true });
  assert.equal(group.el.open, true, "completion reads the current preference");
  env.setShowDetails(false);
  assert.equal(group.el.open, false);
});

test("questions and completed replays close activity without hiding requests or errors", () => {
  const env = setup();
  const view = new env.AssistantView({ live: true });
  view.toolCall({ id: "one", name: "read_file", input: {} });
  const group = view.activity;
  view.toolResult({ id: "one", ok: true });
  view.add(form);
  assert.equal(group.el.open, false);
  assert.equal(view.forms.get(form.id).el.parent, view.body);
  view.toolCall({ id: "two", name: "shell", input: {} });
  const next = view.activity;
  view.error("Provider unavailable");
  assert.equal(next.el.open, true, "pending work closes once stopped");
  view.finish("failed");
  assert.equal(next.el.open, false);
  assert.match(view.body.textContent, /Provider unavailable/);
  for (const details of [false, true]) {
    const replay = new (setup({ details }).AssistantView)();
    replay.toolCall({ id: "one", name: "shell", input: {} });
    const historical = replay.activity;
    assert.equal(historical.el.open, details);
    replay.toolResult({ id: "one", ok: true });
    replay.finish("succeeded");
    assert.equal(historical.el.open, details);
  }
});

test("questions are visible with descriptions, multiple choices and own answers", async () => {
  const { FormCard } = setup();
  const sent = [];
  const card = new FormCard(async (event, answer) => sent.push([event.id, plain(answer)]));
  card.update(form, true);
  assert.match(card.el.textContent, /Main use\?/);
  assert.match(card.el.textContent, /High FPS/);
  const use = card.controls[0].el.querySelectorAll("input");
  use[0].checked = use[1].checked = true;
  const gpu = card.controls[1].el.querySelectorAll("input");
  const custom = gpu.find((c) => c.placeholder);
  custom.value = "RTX 5070";
  custom.callbacks.input();
  card.update(form, true); // reconnect/duplicate must preserve the draft
  assert.equal(card.controls[1].el.querySelectorAll("input").find((c) => c.placeholder), custom);
  await card.submit(card.answers());
  assert.deepEqual(sent, [["frm_1", { use: ["Gaming", "AI"], gpu: "RTX 5070" }]]);
  assert.equal(card.event.status, "answered");
  assert.equal(card.buttons.hidden, true);
  assert.ok(card.controls.every((c) => c.el.disabled));
});

test("failed replies preserve selections, block double send and can be retried or skipped", async () => {
  const { FormCard } = setup();
  let fail;
  const card = new FormCard(() => new Promise((_, reject) => { fail = reject; }));
  card.update(form, true);
  card.controls[0].el.querySelectorAll("input")[0].checked = true;
  const pending = card.submit(card.answers());
  assert.equal(card.busy, true);
  await card.submit(null);
  fail(new Error("Offline"));
  await pending;
  assert.match(card.error.textContent, /Offline/);
  assert.equal(card.event.status, "pending");
  assert.equal(card.controls[0].el.disabled, false);
  assert.deepEqual(plain(card.answers()).use, ["Gaming"]);
  const sent = [];
  card.reply = async (_, answer) => sent.push(answer);
  await card.submit(null);
  assert.deepEqual(sent, [null]);
  assert.equal(card.event.status, "cancelled");
});

test("conditional fields and maxItems are honoured", () => {
  const { FormCard } = setup();
  const sent = [];
  const card = new FormCard(async (_, answer) => sent.push(answer));
  card.update({ ...form, fields: [
    { key: "many", type: "multiselect", title: "One choice", maxItems: 1, options: form.fields[0].options },
    { key: "details", type: "string", required: true, when: [{ key: "many", op: "eq", value: "never" }] },
  ] }, true);
  assert.equal(card.controls[0].el.querySelectorAll("input")[0].type, "radio");
  assert.equal(card.controls[1].el.hidden, true);
  assert.equal(card.controls[1].el.disabled, true);
  assert.ok(!("details" in card.answers()));
  card.controls[0].el.querySelectorAll("input").forEach((c) => { c.checked = true; });
  card.form.callbacks.submit({ preventDefault() {} });
  assert.equal(sent.length, 0);
  assert.match(card.error.textContent, /One choice/);
});

test("assistant view renders, answers, replays and closes questions through the shared handler", async () => {
  const env = setup();
  const sent = [];
  env.setFormHandler(async (...args) => sent.push(plain(args)));
  const view = new env.AssistantView({ live: true, runId: "r" });
  view.add(form);
  assert.match(view.el.textContent, /Waiting for your answers/);
  const card = view.forms.get("frm_1");
  await card.submit({ use: ["AI"] });
  assert.deepEqual(sent, [["r", "frm_1", { use: ["AI"] }]]);
  view.add({ type: "form", id: "frm_1", status: "answered", answer: { use: ["AI"] } });
  view.add({ ...form, id: "frm_2" });
  view.finish("interrupted");
  assert.equal(view.forms.get("frm_2").event.status, "cancelled");
  const replay = new env.AssistantView();
  replay.add(form);
  replay.add({ type: "form", id: "frm_1", status: "answered", answer: { use: ["AI"] } });
  assert.match(replay.el.textContent, /Answers sent/);
  assert.equal(replay.forms.get("frm_1").buttons.hidden, true);
});

test("question countdown uses the server deadline and never dismisses from the browser", () => {
  const env = setup();
  let now = 1000000;
  env.Date = { now: () => now };
  const sent = [];
  const card = new env.FormCard(async (_, answer) => sent.push(answer));
  card.el.isConnected = true;
  card.update({ ...form, expires: now / 1000 + 300 }, true);
  assert.match(card.status.textContent, /dismiss in 5:00/);
  assert.equal(env.timers.size, 1);
  now += 120000;
  card.update({ ...form, expires: 1300 }, true); // reload uses the original deadline
  assert.match(card.status.textContent, /dismiss in 3:00/);
  assert.equal(env.timers.size, 1);
  now += 180000;
  card.refresh();
  assert.match(card.status.textContent, /Dismissing/);
  assert.deepEqual(sent, []);
  card.update({ status: "cancelled",
    message: "Question dismissed automatically: no answer before the timeout" }, false);
  assert.equal(env.timers.size, 0);
  assert.match(card.status.textContent, /dismissed automatically/);
  assert.equal(card.buttons.hidden, true);
});

test("countdown stops when the card is detached or the question is answered", async () => {
  const env = setup();
  const card = new env.FormCard(async () => {});
  card.el.isConnected = true;
  card.update({ ...form, expires: Date.now() / 1000 + 300 }, true);
  assert.equal(env.timers.size, 1);
  card.el.isConnected = false;
  card.refresh();
  assert.equal(env.timers.size, 0);
  card.el.isConnected = true;
  card.refresh();
  await card.submit({ use: ["AI"] });
  assert.equal(env.timers.size, 0);
  const disabled = new env.FormCard(async () => {});
  disabled.el.isConnected = true;
  disabled.update(form, true);
  assert.match(disabled.status.textContent, /^Waiting for your answers$/);
  assert.equal(env.timers.size, 0);
});

test("consecutive tools share live activity; text and questions stay outside", () => {
  const env = setup();
  const view = new env.AssistantView({ live: true, runId: "main" });
  view.add({ type: "tool_call", id: "r1", name: "read_file", input: { path: "a.py" } });
  view.add({ type: "tool_result", id: "r1", name: "read_file", ok: true, output: "a" });
  view.add({ type: "tool_call", id: "r2", name: "read_file", input: { path: "b.py" } });
  view.add({ type: "tool_result", id: "r2", name: "read_file", ok: true, output: "b" });
  view.add({ type: "tool_call", id: "t1", name: "shell", input: { command: "python3 -m pytest -q" } });
  const group = view.tools.get("r1").group;
  assert.equal(group, view.tools.get("t1").group);
  assert.equal(group.el.open, true);
  assert.match(group.label.textContent, /2 files read · Running tests/);
  view.add({ type: "text", delta: "Here is my answer." });
  view.add({ type: "tool_result", id: "t1", name: "shell", ok: true, output: "Passed" });
  assert.match(group.label.textContent, /2 files read · 1 test run/);
  assert.equal(group.el.open, false);
  view.add({ type: "form", id: "q", fields: [], status: "pending" });
  assert.equal(view.forms.get("q").el.parent, view.body);
  view.add({ type: "tool_call", id: "r3", name: "read_file", input: { path: "c.py" } });
  assert.notEqual(view.tools.get("r3").group, group);
});

test("failed tools and errors are visible outside activity, and unfinished tools settle on stop", () => {
  const env = setup();
  const view = new env.AssistantView({ live: true, runId: "main" });
  view.add({ type: "tool_call", id: "f", name: "shell", input: { command: "false" } });
  const failed = view.tools.get("f");
  view.add({ type: "tool_result", id: "f", name: "shell", ok: false, output: "Exit 1" });
  assert.equal(failed.el.parent, view.body);
  assert.equal(failed.group.el.hidden, true);
  assert.equal(failed.state.textContent, "Failed");
  assert.equal(failed.summary.textContent, "false");
  assert.equal(failed.error.textContent, "Exit 1");
  assert.equal(failed.el.classList.contains("open"), true);
  view.add({ type: "tool_call", id: "r", name: "read_file", input: { path: "a.py" } });
  view.add({ type: "error", message: "Provider unavailable" });
  assert.equal(view.body.querySelector(".run-error").parent, view.body);
  view.finish("interrupted");
  assert.match(view.tools.get("r").group.label.textContent, /stopped/);
  assert.doesNotMatch(view.tools.get("r").group.label.textContent, /Reading/);
});

test("show details expands activity and tool details, never subagent conversations", () => {
  const env = setup({ details: true });
  const view = new env.AssistantView();
  view.add({ type: "tool_call", id: "r", name: "read_file", input: { path: "a.py" } });
  view.add({ type: "subagent", id: "child", label: "Researcher", task: "Check the project", run: "child-run",
    provider: "test", model: "test/model", status: "running" });
  const card = view.subagents.get("child");
  assert.equal(view.tools.get("r").group.el.open, true);
  assert.equal(card.el.classList.contains("open"), false);
  assert.equal(env.streams.length, 0);
  env.setShowDetails(false);
  assert.equal(view.tools.get("r").group.el.open, false);
  env.setShowDetails(true);
  assert.equal(card.el.classList.contains("open"), false);
});

test("closed subagent cards show task, model, forwarded questions and failures without a stream", () => {
  const env = setup();
  const view = new env.AssistantView({ live: true, runId: "main" });
  view.add({ type: "subagent", id: "child", label: "Researcher", task: "Check the project", run: "child-run",
    provider: "test", model: "test/model", status: "running" });
  const card = view.subagents.get("child");
  assert.match(card.state.textContent, /Working/);
  assert.equal(card.prompt.textContent, "Check the project");
  assert.match(card.meta.textContent, /test\/model/);
  view.add({ type: "subagent", id: "unknown-run", status: "running" });
  view.add({ ...form, id: "parent-question" });
  assert.match(view.subagents.get("unknown-run").state.textContent, /Working/);
  view.add({ ...form, run: "child-run", subagent: "Researcher" });
  assert.match(card.state.textContent, /Waiting for you/);
  assert.equal(view.forms.get(form.id).el.parent, view.body);
  view.add({ ...form, run: "child-run", status: "answered", answer: {} });
  assert.match(card.state.textContent, /Working/);
  view.add({ type: "subagent", id: "child", status: "failed", error: "Provider offline" });
  assert.match(card.state.textContent, /Failed/);
  assert.equal(card.alert.hidden, false);
  assert.equal(card.alert.textContent, "Provider offline");
  assert.equal(card.el.classList.contains("open"), false);
  assert.equal(env.streams.length, 0);
});

test("subagent streams only open on demand, forward activity, and ignore stale callbacks", () => {
  const env = setup();
  let updates = 0;
  const view = new env.AssistantView({ live: true, runId: "main", onActivity: () => updates++ });
  view.add({ type: "subagent", id: "child", run: "first", status: "running" });
  const card = view.subagents.get("child");
  const button = card.el.querySelector(".tool-head");
  assert.equal(env.streams.length, 0);
  button.callbacks.click();
  const first = env.streams[0];
  first.onmessage({ data: JSON.stringify({ type: "tool_call", id: "r", name: "read_file", input: { path: "a" } }) });
  assert.ok(card.view.tools.get("r").group);
  assert.ok(updates > 1);
  view.add({ type: "subagent", id: "child", run: "second", status: "running" });
  assert.equal(first.closed, true);
  const second = env.streams[1];
  first.listeners.end();
  first.onmessage({ data: JSON.stringify({ type: "error", message: "Stale error" }) });
  assert.equal(card.source, second);
  assert.doesNotMatch(card.thread.textContent, /Stale error/);
  button.callbacks.click();
  assert.equal(second.closed, true);
  assert.equal(card.source, null);
});
