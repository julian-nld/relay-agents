// Ctrl+K (⌘K): a quick switcher to any chat, from anywhere (Relay 2.11). Titles as you type, then the server's
// search (what was said in them too); the column and the page stay as they are until one is opened.

import { api } from "./api.js";
import { clear, debounce, h, icon, modal, timeAgo } from "./ui.js";

const SHOWN = 8;  // rows at a time: a switcher, not a list to scroll

// The chats a query finds by their titles, best first: the title starting with it, a word in it starting with it,
// then anywhere in it; each kind most recent first. Each word typed must be in the title. Nothing typed: the most
// recent ones, the open one left out (it's where you are).
export function rankChats(conversations, query, { current = null, limit = SHOWN } = {}) {
  const wanted = String(query || "").trim().toLowerCase();
  const words = wanted.split(/\s+/).filter(Boolean);
  const scored = [];
  for (const conv of conversations) {
    const title = String(conv.title || "").toLowerCase();
    if (!words.length) {
      if (conv.id !== current) scored.push([0, conv]);
      continue;
    }
    if (!words.every((word) => title.includes(word))) continue;
    const at = title.indexOf(wanted);
    scored.push([at === 0 ? 0 : at > 0 && /[^\p{L}\p{N}]/u.test(title[at - 1]) ? 1 : at > 0 ? 2 : 3, conv]);
  }
  return scored.sort((a, b) => a[0] - b[0] || (b[1].updated || 0) - (a[1].updated || 0))
    .slice(0, limit).map(([, conv]) => conv);
}

let opened = null;  // the switcher on screen: Ctrl+K again closes it

// listed(): the chats there are (as the chat list has them); allowed(conv): one the server's search found may be
// shown; onOpen(id): one chosen
export function openSwitcher({ listed, allowed = () => true, current, onOpen }) {
  if (opened) {
    opened.close();
    return;
  }
  const dialog = modal("Go to a chat", { onClose: () => { opened = null; } });
  opened = dialog;
  dialog.el.classList.add("switcher");
  dialog.foot.remove();
  const list = h("div.switcher-list", { role: "listbox", id: "switcher-list", "aria-label": "Chats" });
  const input = h("input.input.switcher-input", { type: "search", placeholder: "Go to a chat…",
    "aria-label": "Find a chat", role: "combobox", "aria-expanded": "true", "aria-controls": "switcher-list",
    autocomplete: "off", spellcheck: "false" });
  clear(dialog.body, input, list);
  let rows = [], chosen = 0, asked = 0, found = [];

  function render() {
    const query = input.value.trim();
    const local = rankChats(listed(), query, { current });
    const seen = new Set(local.map((conv) => conv.id));
    // (the server's: in what was said in them, or beyond the list's last 500)
    const more = query ? found.filter((conv) => !seen.has(conv.id)).slice(0, Math.max(0, SHOWN - local.length)) : [];
    rows = [...local.map((conv) => [conv, false]), ...more.map((conv) => [conv, true])];
    chosen = Math.min(chosen, Math.max(0, rows.length - 1));
    clear(list, rows.length ? rows.map(([conv, deeper], index) => h(`button.switcher-item${index === chosen
      ? ".chosen" : ""}`, { type: "button", role: "option", id: `switcher-${index}`,
      "aria-selected": String(index === chosen), title: conv.title, tabindex: "-1",
      onmousemove: () => { if (chosen !== index) { chosen = index; mark(); } },
      onclick: () => choose(index) },
    icon(conv.mode === "code" ? "code" : conv.computer ? "monitor" : "chat", "sm mode"),
    h("span.title.truncate", conv.title),
    h("span.switcher-hint", deeper ? "in its messages" : conv.id === current ? "open" : timeAgo(conv.updated))))
      : h("div.empty-list", !query ? "No chats yet." : query.length < 2 ? "No chat has that in its title."
        : "None by its title: looking in what was said in them…"));
    mark();
  }

  function mark() {
    list.querySelectorAll(".switcher-item").forEach((row, index) => {
      row.classList.toggle("chosen", index === chosen);
      row.setAttribute("aria-selected", String(index === chosen));
    });
    const row = list.querySelector(".switcher-item.chosen");
    if (row) {
      input.setAttribute("aria-activedescendant", row.id);
      row.scrollIntoView({ block: "nearest" });
    } else input.removeAttribute("aria-activedescendant");
  }

  function choose(index) {
    const row = rows[index];
    if (!row) return;
    dialog.close();
    onOpen(row[0].id);
  }

  const search = debounce(async () => {
    const query = input.value.trim();
    const mine = ++asked;
    if (query.length < 2) {
      found = [];
      return;
    }
    try {
      const answer = await api.get(`/api/conversations?q=${encodeURIComponent(query)}`);
      if (mine !== asked || !opened) return;
      found = answer.filter(allowed);
    } catch {
      found = [];  // (Relay restarting: the titles still find them)
    }
    if (mine === asked && !rows.length && !found.length) {
      clear(list, h("div.empty-list", "No chat matches."));
      return;
    }
    render();
  }, 200);

  input.addEventListener("input", () => {
    chosen = 0;
    found = [];
    render();
    search();
  });
  input.addEventListener("keydown", (e) => {
    const step = { ArrowDown: 1, ArrowUp: -1 }[e.key];
    if (step && rows.length) {
      e.preventDefault();
      chosen = (chosen + step + rows.length) % rows.length;
      mark();
    } else if (e.key === "Enter" && !e.isComposing) {
      e.preventDefault();
      choose(chosen);
    }
  });
  render();
  input.focus();
  return dialog;
}
