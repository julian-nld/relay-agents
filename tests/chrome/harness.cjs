// What the extension's browser tests share: the extension (plugins/chrome/extension) loaded in Chromium, checks that
// count their failures, and where screenshots go (RELAY_CHROME_SHOTS, else a folder in the system's temp).
//
// They test a copy of the extension with two changes: the host permissions it would be given (Chrome asks for
// them in a dialog), and the active tab found by its address (a panel opened as a tab is its own active tab).
// PLAYWRIGHT_CORE and CHROMIUM say where those are when they're not where a WorkBox has them (on Relay's machine:
// Python Playwright's driver package and its full Chromium; its headless shell loads no extensions).

const { chromium } = require(process.env.PLAYWRIGHT_CORE || "/usr/local/lib/node_modules/playwright-core");
const fs = require("fs");
const os = require("os");
const path = require("path");

// (the extension's copy and Chromium's profile, which keeps the extension's settings and so its app password: gone
// when the test ends)
const made = [];
process.on("exit", () => made.forEach((dir) => fs.rmSync(dir, { recursive: true, force: true })));

function copyExtension() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "relay-ext-"));
  made.push(dir);
  fs.cpSync(path.join(__dirname, "..", "..", "plugins", "chrome", "extension"), dir,
    { recursive: true, dereference: true });
  const manifest = JSON.parse(fs.readFileSync(path.join(dir, "manifest.json"), "utf8"));
  manifest.host_permissions = ["<all_urls>"];
  fs.writeFileSync(path.join(dir, "manifest.json"), JSON.stringify(manifest, null, 2));
  const page = path.join(dir, "lib", "page.js");
  const source = fs.readFileSync(page, "utf8");
  const before = "const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });";
  if (!source.includes(before)) throw new Error("activeTab() changed: update the test's shim");
  fs.writeFileSync(page,
    source.replace(before, 'const [tab] = await chrome.tabs.query({ url: "http://127.0.0.1/__page*" });'));
  return dir;
}

// Chromium with the extension: its context, its service worker, its id, and the errors it had so far.
async function launch() {
  const ext = copyExtension();
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "relay-profile-"));
  made.push(profile);
  const context = await chromium.launchPersistentContext(profile, {
    executablePath: process.env.CHROMIUM || "/usr/bin/chromium",
    headless: true,
    args: [`--disable-extensions-except=${ext}`, `--load-extension=${ext}`, "--no-sandbox"],
    viewport: { width: 400, height: 760 },
  });
  const errors = [];
  context.on("weberror", (e) => errors.push(`page error: ${e.error().message}`));
  const worker = context.serviceWorkers()[0] || await context.waitForEvent("serviceworker");
  worker.on("console", (m) => { if (m.type() === "error") errors.push(`worker: ${m.text()}`); });
  const id = new URL(worker.url()).host;
  console.log(`extension ${id}`);
  const watch = (page, name) =>
    page.on("console", (m) => { if (m.type() === "error") errors.push(`${name}: ${m.text()}`); });
  return { context, worker, id, errors, watch };
}

let failures = 0;

function check(ok, what) {
  console.log(`${ok ? "ok  " : "FAIL"} ${what}`);
  if (!ok) failures++;
}

function finish(errors) {
  check(errors.length === 0, `no errors${errors.length ? `:\n  ${errors.join("\n  ")}` : ""}`);
  console.log(failures ? `\n${failures} failed` : "\nall passed");
  return failures ? 1 : 0;
}

// The last answer's or question's element in the panel, by a function of it run there.
const last = (selector) => `[...document.querySelectorAll(${JSON.stringify(selector)})].pop()`;

const shots = process.env.RELAY_CHROME_SHOTS || path.join(os.tmpdir(), "relay-chrome-shots");
// The plugin's version, as the extension's settings say it.
const pluginVersion =
  JSON.parse(fs.readFileSync(path.join(__dirname, "..", "..", "plugins", "chrome", "plugin.json"), "utf8")).version;

module.exports = { launch, check, finish, last, shots, pluginVersion };
