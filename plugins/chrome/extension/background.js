// The extension's service worker: the toolbar button opens the side panel, the right-click menu sends a page,
// a selection or a link there (to a chat, or to a script as its argument) or a field's text to be rewritten,
// "relay <question>" in the address bar starts a chat in Relay, and Relay's agents use this browser (lib/agent.js)
// while Chrome runs.

import { BrowserAgents, inPage } from "./lib/agent.js";
import { connect, loadSettings } from "./lib/relay.js";
import { readTab, shellQuote } from "./lib/page.js";

const MENU = [
  { id: "ask-selection", title: "Ask Relay about “%s”", contexts: ["selection"] },
  { id: "ask-page", title: "Ask Relay about this page", contexts: ["page"] },
  { id: "summarize-page", title: "Summarize this page with Relay", contexts: ["page"] },
  { id: "ask-link", title: "Ask Relay about this link", contexts: ["link"] },
  { id: "script-selection", title: "Run a Relay script with “%s”", contexts: ["selection"] },
  { id: "script-page", title: "Run a Relay script with this page's address", contexts: ["page"] },
  { id: "script-link", title: "Run a Relay script with this link", contexts: ["link"] },
  { id: "rewrite", title: "Rewrite with Relay", contexts: ["editable"] },
  { id: "rewrite-improve", parentId: "rewrite", title: "Improve it", contexts: ["editable"] },
  { id: "rewrite-shorter", parentId: "rewrite", title: "Make it shorter", contexts: ["editable"] },
  { id: "rewrite-formal", parentId: "rewrite", title: "Make it more formal", contexts: ["editable"] },
  { id: "rewrite-english", parentId: "rewrite", title: "Translate to English", contexts: ["editable"] },
  { id: "rewrite-dutch", parentId: "rewrite", title: "Translate to Dutch", contexts: ["editable"] },
  { id: "rewrite-own", parentId: "rewrite", title: "In your own words…", contexts: ["editable"] },
  { id: "open-relay", title: "Open Relay in a tab", contexts: ["action"] },
];

// What each rewrite asks of the agent (its text goes along as the selection; the panel asks for only the new text);
// "own" leaves it for you to say.
const REWRITE = {
  improve: "Improve this text: clearer and better written, in its own language, saying the same.",
  shorter: "Make this text shorter, in its own language, keeping what matters.",
  formal: "Make this text more formal, in its own language.",
  english: "Translate this text into English.",
  dutch: "Translate this text into Dutch.",
};

chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});

chrome.runtime.onInstalled.addListener(async ({ reason }) => {
  await chrome.contextMenus.removeAll();
  for (const item of MENU) chrome.contextMenus.create(item);
  if (reason === "install" && !(await loadSettings()).url) chrome.runtime.openOptionsPage();
});

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId === "open-relay") {
    openRelay("/");
    return;
  }
  // First, while it's still the user's click (Chrome opens a side panel only then); what goes along after.
  chrome.sidePanel.open({ windowId: tab.windowId }).catch(() => {});
  const done = String(info.menuItemId).startsWith("rewrite-") ? rewriteFromMenu(info, tab) : fromMenu(info, tab);
  done.catch((error) => console.warn("Relay:", error));
});

// A field's text to be rewritten: taken (and the field kept, to put the answer back into: Replace in field), and asked
// about in a new chat; "In your own words…" waits in the panel for what you'd like done with it.
async function rewriteFromMenu(info, tab) {
  const target = crypto.randomUUID().slice(0, 12);
  const frameId = info.frameId || 0;
  let field;
  try {
    field = await inPage(tab.id, "field", [target], frameId);
  } catch (error) {
    await chrome.storage.session.set({ pending: { at: Date.now(), problem: error.message } });
    return;
  }
  const how = REWRITE[String(info.menuItemId).slice("rewrite-".length)];
  await chrome.storage.session.set({ pending: {
    at: Date.now(), fresh: true,
    contexts: [{ kind: "selection", text: field.text, url: tab.url, title: tab.title }],
    rewrite: { tabId: tab.id, frameId, target, selected: field.selected },
    send: how || null,
    draft: how ? null : "Rewrite this text: ",
  } });
}

async function fromMenu(info, tab) {
  const { pageLimit } = await loadSettings();
  const page = await readTab(tab.id, pageLimit).catch(() => null);
  const source = { url: page?.url || info.pageUrl || tab.url, title: page?.title || tab.title };
  if (String(info.menuItemId).startsWith("script-")) {
    // (the scripts in the panel, with it as their argument)
    const value = info.menuItemId === "script-selection" ? page?.selection || info.selectionText
      : info.menuItemId === "script-link" ? info.linkUrl : source.url;
    await chrome.storage.session.set({ pending: { at: Date.now(), view: "scripts", args: shellQuote(value) } });
    return;
  }
  let context;
  let send = null;
  if (info.menuItemId === "ask-selection") {
    // (the page's own selection keeps its lines; Chrome's selectionText runs them together)
    context = { kind: "selection", text: page?.selection || info.selectionText, ...source };
  } else if (info.menuItemId === "ask-link") {
    context = { kind: "link", url: info.linkUrl, text: info.linkText || info.selectionText || "", pageUrl: source.url };
  } else {
    context = { kind: "page", ...source, description: page?.description || "", text: page?.text || "",
      truncated: Boolean(page?.truncated) };
    if (info.menuItemId === "summarize-page") send = "Summarize this page.";
  }
  await chrome.storage.session.set({ pending: { at: Date.now(), contexts: [context], send, fresh: Boolean(send) } });
}

chrome.omnibox.setDefaultSuggestion({ description: "Ask Relay: %s" });

chrome.omnibox.onInputEntered.addListener(async (text, disposition) => {
  const question = text.trim();
  const relay = await connect();
  if (!relay.configured) {
    chrome.runtime.openOptionsPage();
    return;
  }
  if (!question) {
    openRelay("/", disposition);
    return;
  }
  try {
    const agent = await relay.defaultAgent();
    const chat = await relay.create(agent);
    await relay.send(chat.id, question);
    await chrome.storage.local.set({ current: chat.id });
    openRelay(`/c/${chat.id}`, disposition);
  } catch (error) {
    chrome.tabs.create({ url: chrome.runtime.getURL(`options.html#error=${encodeURIComponent(error.message)}`) });
  }
});

async function openRelay(path, disposition = "newForegroundTab") {
  const url = (await connect()).link(path);
  if (!url) {
    chrome.runtime.openOptionsPage();
    return;
  }
  if (disposition === "currentTab") chrome.tabs.update({ url });
  else chrome.tabs.create({ url, active: disposition !== "newBackgroundTab" });
}

// Relay's agents in this browser. They ask for the plugin's commands from here, the side panel open or not; Chrome
// stops an idle service worker, so an alarm starts them again (within the plugin's 45 seconds). A page with cards
// (the side panel, the window a notification opens) is linked by a port: it gets the cards and gives your answers.
// With none open, a step that needs you is a notification.

const ALARM = "relay-agents";
const pages = new Set();      // the ports of the pages that show the cards
const notified = new Set();   // the commands a notification is out for
let agents = null;
let starting = null;

function ensureAgents(again = false) {
  starting = (starting || Promise.resolve()).then(async () => {
    if (agents?.looping && !again) return;
    agents?.stop();
    agents = null;
    const relay = await connect();
    if (relay.configured && relay.settings.browserAgents !== false && await signedIn(relay)) {
      agents = new BrowserAgents({ relay, onChange: changed });
      await agents.start();
    }
    changed();
  }).catch((error) => console.warn("Relay:", error));
  return starting;
}

// (signed in to Relay: else nothing's asked of it, and the alarm looks again)
async function signedIn(relay) {
  try {
    const found = await relay.state();
    return !found.login_required || Boolean(found.user);
  } catch {
    return false;
  }
}

function changed() {
  const items = agents?.shown || [];
  for (const port of pages) port.postMessage({ items });
  const asking = items.filter((item) => item.state === "asking");
  chrome.action.setBadgeText({ text: asking.length ? String(asking.length) : "" }).catch(() => {});
  for (const id of notified) {
    if (pages.size || !asking.some((item) => item.command.id === id)) {
      notified.delete(id);
      chrome.notifications.clear(id).catch(() => {});
    }
  }
  if (pages.size) return;
  for (const item of asking) {
    if (notified.has(item.command.id)) continue;
    notified.add(item.command.id);
    const text = item.text === undefined ? ""
      : `\n“${item.text.length > 120 ? `${item.text.slice(0, 119)}…` : item.text}”`;
    chrome.notifications.create(item.command.id, {
      type: "basic", iconUrl: "icons/icon-128.png", requireInteraction: true, priority: 2,
      title: `${item.chat || "A chat"} wants to:`, message: `${item.what}${text}`,
      contextMessage: "Relay: click to allow or deny",
    }).catch(() => notified.delete(item.command.id));
  }
}

chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== "agents") return;
  pages.add(port);
  port.onMessage.addListener((message) => {
    if (message.decide) agents?.decide(message.decide, message.decision, message.permitted);
  });
  port.onDisconnect.addListener(() => {
    pages.delete(port);
    changed();
  });
  ensureAgents();
  changed();
});

// A notification clicked: the window with its card (and any other waiting).
chrome.notifications.onClicked.addListener((id) => {
  chrome.notifications.clear(id).catch(() => {});
  chrome.windows.create({ url: chrome.runtime.getURL(`approve.html#${encodeURIComponent(id)}`), type: "popup",
    width: 440, height: 560, focused: true });
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM) ensureAgents();
});

// (started again when where Relay is, how it's signed in to, or whether agents may, changed; not for the agent picked
// in the panel, which would drop what waits for you)
chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local" || !changes.settings) return;
  const { oldValue = {}, newValue = {} } = changes.settings;
  if (["url", "appPassword", "browserAgents"].some((key) => oldValue[key] !== newValue[key])) ensureAgents(true);
});

chrome.alarms.get(ALARM).then((alarm) => alarm || chrome.alarms.create(ALARM, { periodInMinutes: 0.5 }));
ensureAgents();
