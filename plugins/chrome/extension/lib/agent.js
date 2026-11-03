// Relay's agents in this browser. Relay's chrome plugin (plugins/chrome) gives agents tools (chrome_read,
// chrome_click, …); what they ask comes here, to the service worker (background.js), while Chrome runs. You allow it
// on its card (lib/cards.js: the side panel's, or a window a notification opens), or did (for that answer, or always on
// that site); it's done in the tab (lib/inpage.js), and the answer goes back to the agent. With the extension in two
// Chromes, the one where it's allowed claims it: it's done once.

import { activeTab } from "./page.js";

const POLL = 20;       // seconds an ask for commands waits for something new
const SETTLE = 700;    // ms after a click or Enter, for what it does to happen
const READ = 20000;    // characters of a page in one read

// What each tool needs allowed: to see your tabs, to read a site, or to act on it.
const KIND = { tabs: "tabs", read: "read", scroll: "read", screenshot: "read", open: "act", click: "act", type: "act" };

export class BrowserAgents {
  constructor({ relay, onChange }) {
    this.relay = relay;
    this.onChange = onChange;
    this.client = crypto.randomUUID();
    this.version = -1;
    // id: { command, state ("checking", "asking", "doing"), kind, origin, what, chat, tab }
    this.items = new Map();
    this.handled = new Set();      // ids answered from here (a poll from before the answer may list them still)
    this.lastTab = new Map();      // conversation: the tab its agent used last
    this.runAllowed = new Set();   // "<run> <origin>" (or "<run> tabs"): allowed for the rest of that answer
    this.titles = new Map();
    this.abort = null;
    this.available = null;         // whether Relay has the plugin; null: not known (yet)
    this.looping = false;          // whether it asks for commands now
  }

  // Asking for commands, if the setting allows and Relay has the plugin.
  async start() {
    if (this.relay.settings.browserAgents === false) return;
    this.abort = new AbortController();
    const { signal } = this.abort;
    try {
      this.available = Boolean(await this.relay.chromePlugin());
    } catch {
      this.available = null;  // (not known: asked for commands all the same)
    }
    this.onChange();
    if (this.available !== false) this.loop(signal);
  }

  stop() {
    this.abort?.abort();
    this.items.clear();
    this.onChange();
  }

  // What's asked or being done, oldest first, as the cards show it.
  get shown() {
    return [...this.items.values()].filter((item) => item.state === "asking" || item.state === "doing")
      .map(({ command, state, kind, what, chat, origin, text, submit }) => ({ command, state, kind, what, chat, origin,
        text, submit }));
  }

  async loop(signal) {
    this.looping = true;
    try {
      await this.asking(signal);
    } finally {
      this.looping = false;
    }
  }

  async asking(signal) {
    while (!signal.aborted) {
      // (an extension API call: Chrome keeps a service worker that makes them, which a fetch alone doesn't)
      await chrome.runtime.getPlatformInfo().catch(() => {});
      // (with something in hand, quick asks that don't hold one of the plugin's threads while it's answered)
      const busy = this.items.size > 0;
      let found;
      try {
        found = await this.relay.chromeCommands(this.version, busy ? 0 : POLL, signal);
      } catch (error) {
        if (signal.aborted) return;
        if (error.status === 404) {  // (the plugin was turned off)
          this.available = false;
          this.items.clear();
          this.onChange();
          return;
        }
        await pause(5000, signal);
        continue;
      }
      this.available = true;
      this.version = found.version;
      this.sync(found.commands || []);
      if (busy) await pause(1500, signal);
    }
  }

  sync(commands) {
    const waiting = new Set(commands.map((c) => c.id));
    for (const [id, item] of this.items) {
      // (gone: answered in another window, or the agent stopped waiting)
      if (item.state === "asking" && !waiting.has(id)) this.items.delete(id);
    }
    for (const command of commands) {
      const item = this.items.get(command.id);
      if (command.claimed && command.claimed !== this.client) {
        if (item?.state === "asking") this.items.delete(command.id);
      } else if (!item && !this.handled.has(command.id)) {
        this.consider(command);
      }
    }
    this.onChange();
  }

  async consider(command) {
    const item = { command, state: "checking", kind: KIND[command.action] };
    this.items.set(command.id, item);
    try {
      if (!item.kind) throw new Error(`This extension doesn't know “${command.action}”: update it`);
      Object.assign(item, await this.target(command));
      item.chat = await this.title(command.conversation_id);
      if (await this.allowed(item)) return await this.carryOut(item);
      item.state = "asking";
      this.onChange();
    } catch (error) {
      await this.finish(item, { ok: false, error: error.message });
    }
  }

  // The tab it's about, the site it needs allowed, and what it is in words.
  async target(command) {
    const { action, args } = command;
    if (action === "tabs") return { origin: null, what: "see which tabs you have open (their titles and addresses)" };
    if (action === "open" && args.url !== "back") {
      let url;
      try {
        url = new URL(args.url);
      } catch {
        throw new Error(`“${args.url}” isn't an address`);
      }
      if (!/^https?:$/.test(url.protocol)) throw new Error("Only http and https addresses can be opened");
      const tab = args.tab != null ? await this.tabFor(command) : null;
      // (in a tab of yours: it leaves that tab's site too, so that one's allowed as well)
      const from = tab && new URL(tab.url);
      return { tab, origin: url.origin, leaves: from?.origin,
        what: `open ${url.href}${tab ? ` in “${clip(tab.title, 60)}” (${from.host})` : " in a new tab"}` };
    }
    const tab = await this.tabFor(command);
    const { origin, host } = new URL(tab.url);
    const page = `“${clip(tab.title || tab.url, 60)}” (${host})`;  // (the site too: a page names itself)
    switch (action) {
      case "open": return { tab, origin, what: `go back a page in ${page}` };
      case "read": return { tab, origin, what: `read ${page}` };
      case "scroll": return { tab, origin, what: `scroll ${page}` };
      case "screenshot": return { tab, origin, what: `take a screenshot of ${page} (it comes to the front)` };
      case "click": return { tab, origin, what: `click ${await about(tab.id, args.ref)} in ${page}` };
      default: return { tab, origin, what: `type into ${await about(tab.id, args.ref)} in ${page}`, text: args.text,
        submit: args.submit };
    }
  }

  async tabFor(command) {
    const wanted = command.args?.tab;
    let tab = null;
    if (wanted != null) {
      tab = await chrome.tabs.get(Number(wanted)).catch(() => null);
      if (!tab) throw new Error(`There's no tab ${wanted} (any more): chrome_tabs lists them`);
    } else {
      const last = this.lastTab.get(command.conversation_id);
      if (last != null) tab = await chrome.tabs.get(last).catch(() => null);
      tab ||= await activeTab();
      if (!tab) throw new Error("No tab is open in front of the user: give `tab` (chrome_tabs lists them)");
    }
    if (!/^https?:/.test(tab.url || "")) {
      throw new Error(
        `Chrome doesn't let extensions use that tab (${tab.url || "one of Chrome's own pages"}): only web pages`);
    }
    return tab;
  }

  async allowed({ kind, origin, leaves, command }) {
    const { agentAllowed: always = {} } = await chrome.storage.local.get("agentAllowed");
    if (kind === "tabs") return Boolean(always.tabs) || this.runAllowed.has(`${command.run_id} tabs`);
    const site = always.sites?.[origin];
    if (site === "act") return !leaves || always.sites?.[leaves] === "act";
    return kind === "read" && (site === "read" || this.runAllowed.has(`${command.run_id} ${origin}`));
  }

  // Your answer on its card: "deny", "once" (and reading that site for the rest of the agent's answer), "always" (on
  // that site). `permitted`: whether Chrome lets the extension use pages (the card asked it in your click: Chrome asks
  // only then, and not here, in the service worker).
  async decide(id, decision, permitted) {
    const item = this.items.get(id);
    if (!item || item.state !== "asking") return;
    if (decision === "deny") {
      await this.finish(item, { denied: true });
      return;
    }
    if (!permitted && item.kind !== "tabs") {
      await this.finish(item, { denied: true, reason: "Chrome wasn't allowed to let Relay use web pages." });
      return;
    }
    const { command, kind, origin } = item;
    this.runAllowed.add(`${command.run_id} ${kind === "tabs" ? "tabs" : origin}`);
    if (decision === "always") {
      const { agentAllowed: always = {} } = await chrome.storage.local.get("agentAllowed");
      if (kind === "tabs") always.tabs = true;
      else always.sites = { ...always.sites, [origin]: kind === "act" ? "act" : always.sites?.[origin] || "read" };
      await chrome.storage.local.set({ agentAllowed: always });
    }
    await this.carryOut(item);
  }

  async carryOut(item) {
    const { id } = item.command;
    item.state = "doing";
    this.onChange();
    try {
      await this.relay.claimChromeCommand(id, this.client);
    } catch {
      // (another window took it, or the agent stopped waiting)
      this.handled.add(id);
      this.items.delete(id);
      this.onChange();
      return;
    }
    let result;
    try {
      result = { ok: true, ...(await this.run(item)) };
    } catch (error) {
      result = { ok: false, error: String(error.message || error) };
    }
    await this.finish(item, result);
  }

  async finish(item, result) {
    const { id } = item.command;
    this.handled.add(id);
    if (this.handled.size > 500) this.handled.delete(this.handled.values().next().value);
    try {
      await this.relay.answerChromeCommand(id, { client: this.client, ...result });
    } catch {
      // (the agent stopped waiting meanwhile, or another window answered first)
    }
    this.items.delete(id);
    this.onChange();
  }

  // Done in the browser: what the agent hears.
  async run({ command, tab }) {
    const { action, args, conversation_id: conversation } = command;
    if (action === "tabs") return { text: await tabsText() };
    if (action === "open") {
      if (args.url === "back") {
        await chrome.tabs.goBack(tab.id).catch(() => { throw new Error("There's no page to go back to in that tab"); });
        tab = await loaded(tab.id);
      } else if (tab) {
        await chrome.tabs.update(tab.id, { url: args.url });
        tab = await loaded(tab.id);
      } else {
        const { id: windowId } = await chrome.windows.getLastFocused();
        tab = await loaded((await chrome.tabs.create({ url: args.url, active: true, windowId })).id);
      }
      this.lastTab.set(conversation, tab.id);
      return { text: await readText(tab, 0) };
    }
    this.lastTab.set(conversation, tab.id);
    switch (action) {
      case "read": return { text: await readText(tab, args.offset || 0) };
      case "screenshot": return screenshot(tab);
      case "scroll": {
        const { did } = await inPage(tab.id, "scroll", [args.direction || "down", args.ref || null]);
        return { text: `${did}. Read the tab again (chrome_read) to see what's there now.` };
      }
      default: {
        const { did } = action === "click" ? await inPage(tab.id, "click", [args.ref])
          : await inPage(tab.id, "type", [args.ref, args.text, Boolean(args.submit)]);
        if (action === "type" && !args.submit) return { text: `${did}.` };
        await pause(SETTLE);
        const now = await loaded(tab.id);
        const went = now.url !== tab.url ? `The tab went to “${now.title}” · ${now.url}`
          : `The tab is still at “${now.title}”`;
        return { text: `${did}. ${went}: read it again (chrome_read) to see what changed.` };
      }
    }
  }

  title(id) {
    if (!this.titles.has(id)) {
      this.titles.set(id, this.relay.conversation(id).then((c) => c.title || "A chat", () => "A chat"));
    }
    return this.titles.get(id);
  }
}

// Something of lib/inpage.js, run in a tab's page (or one of its frames).
export async function inPage(tabId, name, args, frameId = 0) {
  const target = { tabId, frameIds: [frameId] };
  let found;
  try {
    await chrome.scripting.executeScript({ target, files: ["lib/inpage.js"] });
    [found] = await chrome.scripting.executeScript({
      target,
      args: [name, args],
      func: (name, args) => {
        try {
          return { value: globalThis.__relayPage[name](...args) };
        } catch (error) {
          return { error: String(error.message || error) };
        }
      },
    });
  } catch (error) {
    const message = String(error?.message || error);
    if (/permission|cannot access|host/i.test(message)) {
      throw new Error("Chrome doesn't let the extension use this page: the user hasn't allowed it to use web pages");
    }
    throw new Error(`Chrome couldn't run it in the page: ${message}`);
  }
  const answer = found?.result;
  if (!answer) throw new Error("The page didn't answer (it may still be loading): try again");
  if (answer.error) throw new Error(answer.error);
  return answer.value;
}

// An element as the card shows it, [12 button “Send”]; just its number when the page can't be asked yet.
async function about(tabId, ref) {
  return inPage(tabId, "about", [ref]).catch(() => `[${ref}]`);
}

async function readText(tab, offset) {
  const page = await inPage(tab.id, "read", [offset, READ]);
  const now = await chrome.tabs.get(tab.id);
  const from = offset ? ` (from character ${offset} of ${page.total})` : "";
  const more = page.rest > 0
    ? `\n\n(${page.rest} more characters: chrome_read with offset ${page.next} and tab ${now.id})` : "";
  return `Tab ${now.id}: “${now.title}” · ${now.url}${from}\n\n${page.text || "(no text on it)"}${more}`;
}

async function tabsText() {
  const own = chrome.runtime.getURL("");
  const tabs = (await chrome.tabs.query({})).filter((tab) => !tab.url?.startsWith(own));
  if (!tabs.length) return "No tabs are open.";
  const windows = [...new Set(tabs.map((tab) => tab.windowId))];
  return tabs.map((tab) => `${tab.id} · “${tab.title}” · ${tab.url || "(not a web page)"}`
    + `${tab.active ? " (in front)" : ""}${windows.length > 1 ? ` · window ${windows.indexOf(tab.windowId) + 1}` : ""}`)
    .join("\n");
}

async function screenshot(tab) {
  if (!tab.active) {
    await chrome.tabs.update(tab.id, { active: true });
    await pause(400);
  }
  const data = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "jpeg", quality: 80 });
  return { image: data.slice(data.indexOf(",") + 1), mime: "image/jpeg",
    text: `Tab ${tab.id}: “${tab.title}” · ${tab.url}` };
}

// The tab once it's done loading (or after `ms`, as far as it got).
async function loaded(tabId, ms = 20000) {
  await pause(150);
  const end = Date.now() + ms;
  for (;;) {
    const tab = await chrome.tabs.get(tabId);
    if (tab.status === "complete" || Date.now() > end) return tab;
    await pause(200);
  }
}

function clip(text, n) {
  const flat = String(text || "").replace(/\s+/g, " ").trim();
  return flat.length > n ? `${flat.slice(0, n - 1)}…` : flat;
}

function pause(ms, signal) {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener("abort", () => { clearTimeout(timer); resolve(); }, { once: true });
  });
}
