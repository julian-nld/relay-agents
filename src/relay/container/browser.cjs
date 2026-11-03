// Relay's browser for opencode: a headless Chromium in the agent's container, driven with
// Playwright, so opencode's own browser tools (browser.tabs.open, snapshot, click,
// screenshot, …; meant for its desktop app) work in Relay.
//
// Relay holds the connection to opencode (opencode_manager/bridge/opencode_browser.py) and
// runs this with `podman exec -i … node browser.cjs`; they speak JSON lines:
//   in:  {"requestID", "command": {action, files, inspect?, …}}   (opencode's Browser.Command)
//   out: {"requestID", "outcome": {type: "success", result: {value, files}} | {type: "failure", code, message}}
//        {"state": {tabs, focusedTabID}}                             (whenever the tabs change)
// Nothing of opencode's (its address, its password) is in here. Chromium starts on the first
// command and closes after a while unused; this ends when its input does.

"use strict";
const crypto = require("crypto");
const fs = require("fs");
const os = require("os");
const path = require("path");

const { chromium } = require("/usr/local/lib/node_modules/playwright-core");

const readline = require("readline");

const IDLE_MS = 20 * 60 * 1000; // Chromium closes after this long without a command
const STEP_MS = 30 * 1000; // an action's own time limit (opencode gives up after 60 s)
const KEEP = 500; // console messages and requests kept per tab
const TEXT = 100000; // opencode's limit for long text
const SHORT = 2048; // and for short strings

function log(...parts) {
  console.error(new Date().toISOString(), ...parts);
}

function send(message) {
  process.stdout.write(JSON.stringify(message) + "\n");
}

// The browser and its tabs

let browser = null;
let context = null;
let lastUsed = Date.now();
const tabs = new Map(); // tab id -> tab
let focused = null;
const downloads = path.join(os.tmpdir(), `relay-browser-${process.pid}`);

async function ensureBrowser() {
  if (browser) return;
  browser = await chromium.launch({
    executablePath: "/usr/bin/chromium",
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  context = await browser.newContext({ viewport: { width: 1280, height: 800 }, acceptDownloads: true });
  browser.on("disconnected", () => { browser = null; context = null; tabs.clear(); focused = null; pushState(); });
}

async function closeBrowser() {
  const closing = browser;
  browser = null; context = null; tabs.clear(); focused = null;
  if (closing) await closing.close().catch(() => {});
  await pushState();
}

function newTab(page) {
  const tab = { id: "tab_" + crypto.randomUUID(), page, generation: 0, loading: false, loadError: undefined,
    console: [], consoleDropped: 0, requests: new Map(), requestsDropped: 0, dialog: null, files: [],
    frames: new WeakMap(), frameCount: 0, cdp: null };
  page.on("console", (message) => remember(tab, "console", {
    id: `log_${tab.console.length + tab.consoleDropped + 1}`, timestampMs: Date.now(), level: levelOf(message.type()),
    text: message.text(), source: message.location() && message.location().url ? message.location() : undefined }));
  page.on("pageerror", (error) => remember(tab, "console", {
    id: `log_${tab.console.length + tab.consoleDropped + 1}`, timestampMs: Date.now(), level: "error",
    text: String(error && error.stack || error) }));
  page.on("request", (request) => {
    const id = `req_${tab.requests.size + tab.requestsDropped + 1}`;
    request._relayId = id;
    tab.requests.set(id, { request, started: Date.now(), state: "pending" });
    while (tab.requests.size > KEEP) {
      tab.requests.delete(tab.requests.keys().next().value);
      tab.requestsDropped++;
    }
  });
  const settle = (state) => (request) => {
    const entry = tab.requests.get(request._relayId);
    if (entry) Object.assign(entry,
      { state, ended: Date.now(), failure: state === "failed" ? (request.failure() || {}).errorText : undefined });
  };
  page.on("response", (response) => {
    const entry = tab.requests.get(response.request()._relayId);
    if (entry) entry.status = response.status();
  });
  page.on("requestfinished", settle("completed"));
  page.on("requestfailed", settle("failed"));
  page.on("dialog", (dialog) => { tab.dialog = dialog; });
  page.on("download", async (download) => {
    const file =
      { id: "file_" + crypto.randomUUID(), name: download.suggestedFilename(), mime: "application/octet-stream",
        state: "pending", path: null };
    tab.files.push(file);
    try {
      fs.mkdirSync(downloads, { recursive: true });
      file.path = path.join(downloads, file.id);
      await download.saveAs(file.path);
      file.state = "completed";
    } catch {
      file.state = "failed";
    }
  });
  page.on("framenavigated", (frame) => {
    if (frame === page.mainFrame()) { tab.generation++; tab.loading = true; pushState(); }
  });
  page.on("load", () => { tab.loading = false; pushState(); });
  page.on("close", () => { tabs.delete(tab.id); if (focused === tab.id) focused = null; pushState(); });
  tabs.set(tab.id, tab);
  return tab;
}

function remember(tab, kind, entry) {
  entry.text = entry.text.length > TEXT ? entry.text.slice(0, TEXT) : entry.text;
  entry.textTruncated = entry.text.length >= TEXT;
  if (entry.source) entry.source = { url: String(entry.source.url).slice(0, TEXT), line: entry.source.lineNumber || 0,
    column: entry.source.columnNumber || 0 };
  tab.console.push(entry);
  if (tab.console.length > KEEP) { tab.console.shift(); tab.consoleDropped++; }
}

function levelOf(type) {
  return { debug: "debug", warning: "warning", error: "error", assert: "error", trace: "debug" }[type] || "info";
}

async function info(tab) {
  let canGoBack = false, canGoForward = false;
  try {
    tab.cdp = tab.cdp || await context.newCDPSession(tab.page);
    const history = await tab.cdp.send("Page.getNavigationHistory");
    canGoBack = history.currentIndex > 0;
    canGoForward = history.currentIndex < history.entries.length - 1;
  } catch { /* closed meanwhile */ }
  let title = "";
  try { title = await tab.page.title(); } catch { /* navigating */ }
  return { id: tab.id, url: tab.page.url().slice(0, 16384), title: title.slice(0, SHORT), loading: tab.loading,
    ...(tab.loadError ? { loadError: tab.loadError.slice(0, SHORT) } : {}), canGoBack, canGoForward,
    generation: tab.generation };
}

async function state() {
  const listed = [];
  for (const tab of tabs.values()) listed.push(await info(tab));
  return { tabs: listed, focusedTabID: focused };
}

let pushing = Promise.resolve();
function pushState() {
  pushing = pushing.then(async () => {
    try { send({ state: await state() }); } catch (error) { log("state:", error.message); }
  });
  return pushing;
}

// The actions (opencode's Browser.Action), each answering with its output schema

class Failure extends Error {
  constructor(code, message) { super(message); this.code = code; }
}

function tabOf(input) {
  const tab = tabs.get(input.tabID);
  if (!tab) throw new Failure("tab_unavailable",
    "This tab is closed. Call browser.tabs.list({}) and use a returned tabID.");
  return tab;
}

function frameOf(tab, input) {
  if (!input.frameID) return tab.page.mainFrame();
  const frame = tab.page.frames().find((f) => frameId(tab, f) === input.frameID);
  if (!frame) throw new Failure("frame_unavailable", "No such frame in this tab; list them with browser.frames.");
  return frame;
}

function frameId(tab, frame) {
  if (!tab.frames.has(frame)) tab.frames.set(frame,
    frame === tab.page.mainFrame() ? "main" : `frame_${++tab.frameCount}`);
  return tab.frames.get(frame);
}

function element(tab, ref) {
  return tab.page.locator(`aria-ref=${String(ref).replace(/^@/, "")}`);
}

async function load(tab, run) {
  tab.loadError = undefined;
  try {
    await run();
  } catch (error) {
    tab.loadError = String(error.message || error).split("\n")[0];
  }
  tab.loading = false;
  return info(tab);
}

async function snapshotText(tab, input) {
  const options =
    { mode: "ai", ...(input.depth ? { depth: input.depth } : {}), ...(input.boxes ? { boxes: true } : {}) };
  if (input.ref) return element(tab, input.ref).ariaSnapshot(options);
  const frame = frameOf(tab, input);
  return frame === tab.page.mainFrame() ? tab.page.ariaSnapshot(options) : frame.locator(":root").ariaSnapshot(options);
}

function bounded(text) {
  return { content: text.length > TEXT ? text.slice(0, TEXT) : text, truncated: text.length > TEXT };
}

const UNSUPPORTED = "Relay's browser (headless Chromium) doesn't do this; use snapshot, screenshot, evaluate, " +
  "console and network instead.";

const actions = {
  "tabs.list": async () => state(),
  "tabs.open": async (input) => {
    await ensureBrowser();
    const tab = newTab(await context.newPage());
    if (input.focus !== false) focused = tab.id;
    const result = input.url && input.url !== "about:blank"
      ? await load(tab, () => tab.page.goto(input.url, { waitUntil: "load", timeout: STEP_MS }))
      : await info(tab);
    return result;
  },
  "tabs.focus": async (input) => {
    const tab = tabOf(input);
    focused = tab.id;
    await tab.page.bringToFront().catch(() => {});
    return info(tab);
  },
  "tabs.close": async (input) => {
    const tab = tabOf(input);
    await tab.page.close();
    tabs.delete(tab.id);
    if (focused === tab.id) focused = null;
    return state();
  },
  preview: async () => {
    throw new Failure("unsupported", "Relay has no Review pane to show files in: tell the user the file's path " +
      "(Relay shows it in the chat), or take a screenshot of a page.");
  },
  navigate: async (input) => {
    const tab = tabOf(input);
    return load(tab, () => tab.page.goto(input.url, { waitUntil: "load", timeout: STEP_MS }));
  },
  back: async (input) => {
    const tab = tabOf(input);
    return load(tab, () => tab.page.goBack({ waitUntil: "load", timeout: STEP_MS }));
  },
  forward: async (input) => {
    const tab = tabOf(input);
    return load(tab, () => tab.page.goForward({ waitUntil: "load", timeout: STEP_MS }));
  },
  reload: async (input) => {
    const tab = tabOf(input);
    return load(tab, () => tab.page.reload({ waitUntil: "load", timeout: STEP_MS }));
  },
  stop: async (input) => {
    const tab = tabOf(input);
    await tab.page.evaluate(() => window.stop()).catch(() => {});
    tab.loading = false;
    return info(tab);
  },
  frames: async (input) => {
    const tab = tabOf(input);
    const frames = tab.page.frames().map((frame) => ({
      id: frameId(tab, frame), ...(frame.parentFrame() ? { parentID: frameId(tab, frame.parentFrame()) } : {}),
      url: frame.url().slice(0, TEXT), name: (frame.name() || "").slice(0, SHORT) }));
    return { tab: await info(tab), frames };
  },
  snapshot: async (input) => {
    const tab = tabOf(input);
    return { tab: await info(tab), ...bounded(await snapshotText(tab, input)) };
  },
  find: async (input) => {
    const tab = tabOf(input);
    const wanted = String(input.text).toLowerCase();
    const lines = (await snapshotText(tab, input)).split("\n").filter((line) => line.toLowerCase().includes(wanted));
    return { tab: await info(tab), ...bounded(lines.join("\n")) };
  },
  evaluate: async (input) => {
    const tab = tabOf(input);
    const frame = frameOf(tab, input);
    let value;
    try {
      value = await frame.evaluate(input.script);
    } catch (error) {
      if (!/Illegal return|await is only valid/.test(String(error.message))) throw error;
      value = await frame.evaluate(`(async () => { ${input.script}\n})()`);
    }
    return { tab: await info(tab), value: value === undefined ? null : JSON.parse(JSON.stringify(value)) };
  },
  click: async (input) => {
    const tab = tabOf(input);
    await element(tab, input.ref).click({ button: input.button || "left", clickCount: input.count || 1,
      modifiers: input.modifiers || [], timeout: STEP_MS });
    return info(tab);
  },
  hover: async (input) => {
    const tab = tabOf(input);
    await element(tab, input.ref).hover({ timeout: STEP_MS });
    return info(tab);
  },
  drag: async (input) => {
    const tab = tabOf(input);
    await element(tab, input.from).dragTo(element(tab, input.to), { timeout: STEP_MS });
    return info(tab);
  },
  fill: async (input) => {
    const tab = tabOf(input);
    await element(tab, input.ref).fill(input.text, { timeout: STEP_MS });
    return info(tab);
  },
  fill_form: async (input) => {
    const tab = tabOf(input);
    for (const field of input.fields) {
      const target = element(tab, field.ref);
      if (field.type === "text") await target.fill(field.value, { timeout: STEP_MS });
      else if (field.type === "select") await target.selectOption(field.values, { timeout: STEP_MS });
      else await target.setChecked(field.checked, { timeout: STEP_MS });
    }
    return info(tab);
  },
  select: async (input) => {
    const tab = tabOf(input);
    await element(tab, input.ref).selectOption(input.values, { timeout: STEP_MS });
    return info(tab);
  },
  check: async (input) => {
    const tab = tabOf(input);
    await element(tab, input.ref).setChecked(input.checked, { timeout: STEP_MS });
    return info(tab);
  },
  press: async (input) => { const tab = tabOf(input); await tab.page.keyboard.press(input.key); return info(tab); },
  scroll: async (input) => {
    const tab = tabOf(input);
    await tab.page.mouse.wheel(input.deltaX || 0, input.deltaY);
    return info(tab);
  },
  wait: async (input) => {
    const tab = tabOf(input);
    const frame = frameOf(tab, input);
    const timeout = input.timeoutMs || 10000;
    if (input.condition === "load") await frame.waitForLoadState("load", { timeout });
    else {
      const text = frame.getByText(String(input.text || ""), { exact: false }).first();
      await text.waitFor({ state: input.condition === "text" ? "visible" : "hidden", timeout });
    }
    return info(tab);
  },
  screenshot: async (input) => {
    const tab = tabOf(input);
    const type = input.format === "png" || !input.format ? "png" : "jpeg"; // Chromium here: no webp
    const options = { type, ...(type === "jpeg" && input.quality ? { quality: input.quality } : {}),
      ...(input.fullPage ? { fullPage: true } : {}), timeout: STEP_MS };
    const data = input.ref ? await element(tab, input.ref).screenshot(options) : await tab.page.screenshot(options);
    const file = capture(tab, `screenshot.${type === "png" ? "png" : "jpg"}`, `image/${type}`, data);
    return { value: { tab: await info(tab) }, files: [file] };
  },
  dialog: async (input) => {
    const tab = tabOf(input);
    const dialog = tab.dialog;
    const shown = dialog ? { type: dialog.type(), message: dialog.message().slice(0, TEXT),
      defaultValue: (dialog.defaultValue() || "").slice(0, SHORT) } : null;
    if (dialog && input.action === "accept") { await dialog.accept(input.promptText); tab.dialog = null; }
    if (dialog && input.action === "dismiss") { await dialog.dismiss(); tab.dialog = null; }
    return { tab: await info(tab), dialog: shown };
  },
  "files.upload": async (input, command) => {
    const tab = tabOf(input);
    const folder = fs.mkdtempSync(path.join(os.tmpdir(), "relay-upload-"));
    const paths = (command.files || []).map((file) => {
      const target = path.join(folder, path.basename(file.name) || "file");
      fs.writeFileSync(target, Buffer.from(file.data, "base64"));
      return target;
    });
    await element(tab, input.ref).setInputFiles(paths, { timeout: STEP_MS });
    return info(tab);
  },
  "files.drop": async () => { throw new Failure("unsupported", "Dropping files isn't supported in Relay's browser; " +
    "use browser.files.upload on the file input instead."); },
  "files.list": async (input) => {
    const tab = tabOf(input);
    const files = tab.files.map((f) => ({ id: f.id, name: f.name.slice(0, SHORT), mime: f.mime,
      bytes: f.path && fs.existsSync(f.path) ? fs.statSync(f.path).size : 0, state: f.state }));
    return { tab: await info(tab), files };
  },
  "files.get": async (input) => {
    const tab = tabOf(input);
    const file = tab.files.find((f) => f.id === input.fileID);
    if (!file || file.state !== "completed") throw new Failure("file_unavailable",
      "No completed file with that ID in this tab.");
    return { value: { tab: await info(tab) }, files: [{ id: file.id, name: file.name.slice(0, SHORT), mime: file.mime,
      data: fs.readFileSync(file.path).toString("base64") }] };
  },
  console: async (input) => {
    const tab = tabOf(input);
    const order = ["debug", "info", "warning", "error"];
    const minimum = order.indexOf(input.level || "debug");
    const matching = tab.console.filter((m) => order.indexOf(m.level) >= minimum);
    const limit = input.limit || 100;
    return { tab: await info(tab), messages: matching.slice(-limit), truncated: matching.length > limit,
      dropped: tab.consoleDropped };
  },
  "network.list": async (input) => {
    const tab = tabOf(input);
    const matching = [...tab.requests.entries()].map(([id, entry]) => requestInfo(id, entry))
      .filter((r) => (!input.urlContains || r.url.includes(input.urlContains))
        && (!input.resourceType || r.resourceType === input.resourceType));
    const limit = input.limit || 100;
    return { tab: await info(tab), requests: matching.slice(-limit), truncated: matching.length > limit,
      dropped: tab.requestsDropped };
  },
  "network.get": async (input) => {
    const tab = tabOf(input);
    const entry = tab.requests.get(input.id);
    if (!entry) throw new Failure("request_unavailable", "No such request in this tab (IDs expire); list them again.");
    const response = entry.state === "completed" ? await entry.request.response() : null;
    const headers = (list) =>
      list.slice(0, 200).map((h) => ({ name: h.name.slice(0, SHORT), value: h.value.slice(0, TEXT) }));
    const requestHeaders = await entry.request.headersArray();
    const responseHeaders = response ? await response.headersArray() : [];
    const limit = input.maxBodyChars || 20000;
    const body = async (read) => {
      if (!input.includeBody) return { state: "notRequested" };
      try {
        const data = await read();
        if (data === null || data === undefined) return { state: "empty" };
        const text = Buffer.isBuffer(data) ? data.toString("utf8") : String(data);
        if (/�/.test(text.slice(0, 1000))) return { state: "unavailable", reason: "binary" };
        return text
          ? { state: "text", text: text.slice(0, limit), truncated: text.length > limit } : { state: "empty" };
      } catch {
        return { state: "unavailable", reason: "notCaptured" };
      }
    };
    return { tab: await info(tab), request: requestInfo(input.id, entry), requestHeaders: headers(requestHeaders),
      responseHeaders: headers(responseHeaders),
      headersTruncated: requestHeaders.length > 200 || responseHeaders.length > 200,
      requestBody: await body(() => entry.request.postData()),
      responseBody: response ? await body(() => response.body())
        : { state: input.includeBody ? "pending" : "notRequested" } };
  },
};
for (const name of ["trace.start", "trace.stop", "trace.analyze", "cpu.start", "cpu.stop", "cpu.analyze",
  "heap.snapshot", "heap.summary", "heap.query", "heap.object", "heap.compare", "lighthouse"]) {
  actions[name] = async () => { throw new Failure("unsupported", UNSUPPORTED); };
}

const TYPES = new Set(["document", "stylesheet", "image", "media", "font", "script", "xhr", "fetch", "eventsource",
  "websocket", "manifest"]);

function requestInfo(id, entry) {
  const request = entry.request;
  const type = request.resourceType();
  const base = { id, url: request.url().slice(0, TEXT), method: request.method().slice(0, SHORT),
    resourceType: TYPES.has(type) ? type : "other", timestampMs: entry.started };
  if (entry.state === "pending") return { ...base, state: "pending" };
  const durationMs = (entry.ended || Date.now()) - entry.started;
  if (entry.state === "failed") return { ...base, state: "failed", durationMs,
    failure: (entry.failure || "failed").slice(0, SHORT) };
  return { ...base, state: "completed", durationMs, ...(entry.status ? { statusCode: entry.status } : {}) };
}

function capture(tab, name, mime, data) {
  const file = { id: "file_" + crypto.randomUUID(), name, mime, state: "completed", path: null };
  fs.mkdirSync(downloads, { recursive: true });
  file.path = path.join(downloads, file.id);
  fs.writeFileSync(file.path, data);
  tab.files.push(file);
  return { id: file.id, name, mime, data: Buffer.from(data).toString("base64") };
}

// Commands

async function handle(requestID, command) {
  let outcome;
  try {
    const action = command.action;
    lastUsed = Date.now();
    if (command.inspect) {
      // What the action concerns, for opencode's own checks.
      const tab = tabs.get(action.tabID);
      const url = tab ? tab.page.url() : String(action.url || "about:blank");
      outcome = { type: "success",
        result: { value: { resources: [url.slice(0, TEXT)], key: url.slice(0, TEXT) }, files: [] } };
    } else {
      const run = actions[action.type];
      if (!run) throw new Failure("unsupported", `Relay's browser doesn't know browser.${action.type}.`);
      const done = await run(action, command);
      const { value, files } = done && done.files ? done : { value: done, files: [] };
      outcome = { type: "success", result: { value, files } };
    }
  } catch (error) {
    const code = error instanceof Failure ? error.code : "action_failed";
    outcome = { type: "failure", code,
      message: String(error.message || error).split("\n").slice(0, 6).join("\n").slice(0, SHORT) };
  }
  send({ requestID, outcome });
  await pushState();
}

setInterval(() => {
  if (browser && Date.now() - lastUsed > IDLE_MS) { log("idle: closing Chromium"); closeBrowser(); }
}, 60000);
const input = readline.createInterface({ input: process.stdin });
input.on("line", (line) => {
  let message;
  try { message = JSON.parse(line); } catch { return; }
  if (message.requestID && message.command) handle(message.requestID, message.command);
});
input.on("close", async () => {
  if (browser) await browser.close().catch(() => {});
  process.exit(0);
});
pushState();
