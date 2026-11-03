// Knowing when runs finish or an agent waits for you: unread markers, the tab title, toasts and
// (on HTTPS or localhost) system notifications; what's announced, per category, is this browser's choice.

import { relayUrl, storeKey } from "./base.js";
import { toast } from "./ui.js";

const SEEN_KEY = "relay.seen";
const BROWSER_KEY = "relay.browserNotifications";
const CATEGORIES_KEY = "relay.notifyCategories";

// What this browser announces, per category of relay.notify's (Settings → Notifications), and its
// default. Reports, interventions and VPN problems only go to a phone or a webhook.
export const BROWSER_CATEGORIES = { "run.done": true, "run.failed": true, approval: true, "agents.done": false,
  "agents.failed": true };

function load(key, fallback) {
  try {
    const value = localStorage.getItem(storeKey(key));
    return value === null ? fallback : JSON.parse(value);
  } catch {
    return fallback;
  }
}

function store(key, value) {
  try { localStorage.setItem(storeKey(key), JSON.stringify(value)); } catch { /* private mode */ }
}

export class RunWatcher {
  constructor({ onOpen }) {
    this.onOpen = onOpen; // (conversationId) => void
    this.seen = load(SEEN_KEY, null); // conversation id -> id of the last run seen
    // Conversations running at the last update -> id of their last finished
    // run at that time; a different id afterwards means a run just finished.
    this.running = null;
    this.waiting = new Set();  // conversations waiting for the user at the last update
    this.baseTitle = document.title;
  }

  // Called with every fresh conversation list.
  update(conversations, currentId) {
    if (this.seen === null) {
      // First use: whatever finished before now isn't news.
      this.seen = Object.fromEntries(conversations.filter((c) => c.last_run).map((c) => [c.id, c.last_run.id]));
      store(SEEN_KEY, this.seen);
    }
    const nowRunning = new Map(conversations.filter((c) => c.running_run)
      .map((c) => [c.id, c.last_run?.id ?? null]));
    const nowWaiting = new Set(conversations.filter((c) => c.needs_approval).map((c) => c.id));
    if (this.running) {
      for (const conv of conversations) {
        const justFinished = this.running.has(conv.id) && !nowRunning.has(conv.id)
          && conv.last_run && conv.last_run.id !== this.running.get(conv.id);
        const watching = conv.id === currentId && !document.hidden;
        if (justFinished && !watching) this.announce(conv);
        if (nowWaiting.has(conv.id) && !this.waiting.has(conv.id) && !watching) this.announceWaiting(conv);
        if (watching && conv.last_run) this.markSeen(conv.id, conv.last_run.id);
      }
    }
    this.running = nowRunning;
    this.waiting = nowWaiting;
    this.unreadCount = conversations.filter((c) => this.isUnread(c)).length;
    this.renderTitle();
  }

  isUnread(conv) {
    return Boolean(conv.last_run && this.seen && this.seen[conv.id] !== conv.last_run.id);
  }

  markSeen(conversationId, runId) {
    if (!this.seen || !runId || this.seen[conversationId] === runId) return;
    this.seen[conversationId] = runId;
    store(SEEN_KEY, this.seen);
  }

  setTitle(title) {
    this.baseTitle = title;
    this.renderTitle();
  }

  renderTitle() {
    document.title = this.unreadCount ? `(${this.unreadCount}) ${this.baseTitle}` : this.baseTitle;
  }

  announce(conv) {
    const status = conv.last_run.status;
    // The Governor's agents (managed chats) are AgentOS's; a stopped run counts as done
    if (!browserAnnounces(`${conv.managed ? "agents" : "run"}.${status === "failed" ? "failed" : "done"}`)) return;
    const text = status === "succeeded" ? `Finished: ${conv.title}`
      : status === "failed" ? `Failed: ${conv.title}` : `Stopped: ${conv.title}`;
    this.show(text, status === "failed" ? "error" : "info", conv.id);
  }

  announceWaiting(conv) {
    if (browserAnnounces("approval")) this.show(`Waiting for you: ${conv.title}`, "info", conv.id);
  }

  show(text, kind, conversationId) {
    toast(text, kind, { label: "Open", onclick: () => this.onOpen(conversationId) });
    if (browserNotificationsOn()) {
      try {
        const notification =
          new Notification(text, { tag: `relay-${conversationId}`, icon: relayUrl("/static/icon.svg") });
        notification.onclick = () => { window.focus(); this.onOpen(conversationId); notification.close(); };
      } catch { /* not allowed here */ }
    }
  }
}

export function browserAnnounces(category) {
  const chosen = load(CATEGORIES_KEY, {});
  return typeof chosen?.[category] === "boolean" ? chosen[category] : Boolean(BROWSER_CATEGORIES[category]);
}

export function setBrowserAnnounces(category, on) {
  const chosen = load(CATEGORIES_KEY, {});
  store(CATEGORIES_KEY, { ...(chosen && typeof chosen === "object" ? chosen : {}), [category]: on });
}

export function browserNotificationsSupported() {
  return "Notification" in window && window.isSecureContext;
}

export function browserNotificationsOn() {
  return browserNotificationsSupported() && Notification.permission === "granted" && load(BROWSER_KEY, false);
}

export async function setBrowserNotifications(on) {
  if (on && Notification.permission !== "granted") {
    const permission = await Notification.requestPermission();
    if (permission !== "granted") return false;
  }
  store(BROWSER_KEY, on);
  return on;
}
