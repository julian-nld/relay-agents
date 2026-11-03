// The settings page: where Relay is, how the extension signs in, and the agent for new chats.

import { DEFAULTS, loadSettings, parseAddress, Relay, saveSettings } from "./lib/relay.js";
import { h } from "./lib/ui.js";

const $ = (id) => document.getElementById(id);
const dom = {
  error: $("error"), url: $("url"), password: $("password"), connect: $("connect"), status: $("status"),
  provider: $("provider"), model: $("model"), enter: $("enter"), limit: $("limit"), save: $("save"), saved: $("saved"),
  agents: $("agents"), plugin: $("plugin"), sites: $("sites"),
};

let relay = null;

async function init() {
  const settings = await loadSettings();
  dom.url.value = settings.url;
  dom.password.value = settings.appPassword;
  dom.enter.checked = settings.enterSends !== false;
  dom.limit.value = settings.pageLimit;
  dom.agents.checked = settings.browserAgents !== false;
  dom.agents.addEventListener("change", () => saveSettings({ browserAgents: dom.agents.checked }));
  renderSites();
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === "local" && changes.agentAllowed) renderSites();
  });
  const said = new URLSearchParams(location.hash.slice(1)).get("error");
  if (said) {
    dom.error.textContent = `Relay couldn't start that chat: ${said}`;
    dom.error.hidden = false;
  }
  dom.connect.addEventListener("click", connectRelay);
  dom.save.addEventListener("click", saveDefaults);
  dom.provider.addEventListener("change", () => loadModels(dom.provider.value, ""));
  $("shortcuts").addEventListener("click", (e) => {
    e.preventDefault();
    chrome.tabs.create({ url: "chrome://extensions/shortcuts" });
  });
  relay = new Relay(settings);
  if (relay.configured && (await relay.allowed())) await test(settings);
  else if (!relay.configured) dom.url.focus();
}

async function connectRelay() {
  const address = parseAddress(dom.url.value);
  if (!address) {
    say("bad", "That isn't an address Relay could be at (http:// or https://).");
    return;
  }
  // (Chrome asks only from a click: so first, before anything else is awaited)
  const granted = await chrome.permissions.request({ origins: [address.pattern] }).catch(() => false);
  if (!granted) {
    say("bad", `Without your OK the extension can't reach ${address.base}.`);
    return;
  }
  const settings = await saveSettings({ url: dom.url.value.trim(), appPassword: dom.password.value.trim() });
  relay = new Relay(settings);
  await test(settings);
}

async function test(settings) {
  say("", "Connecting…");
  try {
    const found = await relay.state();
    if (found.login_required && !found.user) {
      say("bad", settings.appPassword ? "Relay doesn't know that app password."
        : "Not signed in: add an app password, or sign in to Relay in this browser.");
      return;
    }
    const who = found.user?.username ? `signed in as ${found.user.username}` : "connected";
    say("ok", `Relay ${found.version || ""}: ${who}.`.replace(" :", ":"));
    checkPlugin();
    await loadProviders(settings);
  } catch (error) {
    say("bad", error.message);
  }
}

async function loadProviders(settings) {
  const providers = (await relay.providers()).filter((p) => p.enabled !== false);
  dom.provider.replaceChildren(h("option", { value: "" }, "Your latest conversation's"),
    ...providers.map((p) => h("option", { value: p.id }, p.name || p.id)));
  dom.provider.value = providers.some((p) => p.id === settings.provider) ? settings.provider : "";
  await loadModels(dom.provider.value, settings.model);
}

async function loadModels(provider, model) {
  dom.model.replaceChildren(h("option", { value: "" }, "The agent's default"));
  dom.model.disabled = !provider;
  if (!provider || !relay) return;
  const models = await relay.models(provider).catch(() => []);
  dom.model.append(...models.filter((m) => m.id).map((m) => h("option", { value: m.id }, m.name || m.id)));
  dom.model.value = models.some((m) => m.id === model) ? model : "";
}

async function saveDefaults() {
  const limit = Math.max(1000, Number(dom.limit.value) || DEFAULTS.pageLimit);
  dom.limit.value = limit;
  await saveSettings({ provider: dom.provider.value, model: dom.provider.value ? dom.model.value : "",
    enterSends: dom.enter.checked, pageLimit: limit });
  dom.saved.className = "status ok";
  dom.saved.textContent = "Saved.";
  setTimeout(() => { dom.saved.textContent = ""; }, 2000);
}

// Whether Relay has its chrome plugin, which gives agents their tools for this browser.
async function checkPlugin() {
  try {
    const found = await relay.chromePlugin();
    dom.plugin.className = `status ${found ? "ok" : "bad"}`;
    dom.plugin.textContent = found ? `Relay's chrome plugin ${found.version || ""} is there: its agents have the tools.`
      : "Relay doesn't have its chrome plugin (or it's off): turn it on in Relay, Settings → Plugins.";
  } catch (error) {
    dom.plugin.className = "status bad";
    dom.plugin.textContent = `Couldn't ask Relay about its plugins: ${error.message}`;
  }
}

// What's always allowed (the panel's “Always” buttons), each to be taken back.
async function renderSites() {
  const { agentAllowed: always = {} } = await chrome.storage.local.get("agentAllowed");
  const forget = async (change) => {
    const { agentAllowed: now = {} } = await chrome.storage.local.get("agentAllowed");
    change(now);
    await chrome.storage.local.set({ agentAllowed: now });
  };
  const rows = Object.entries(always.sites || {}).sort().map(([origin, level]) => h("div.site-row",
    h("span", origin), h("span.faint", level === "act" ? "reading and using it" : "reading it"),
    h("button.btn.small", { onclick: () => forget((now) => { delete now.sites?.[origin]; }) }, "Take back")));
  if (always.tabs) {
    rows.unshift(h("div.site-row", h("span", "Which tabs you have open"), h("span.faint", "seeing them"),
      h("button.btn.small", { onclick: () => forget((now) => { delete now.tabs; }) }, "Take back")));
  }
  dom.sites.replaceChildren(...(rows.length ? rows : [h("span.faint", "Nothing yet.")]));
}

function say(kind, text) {
  dom.status.className = `status ${kind}`;
  dom.status.textContent = text;
}

init();
