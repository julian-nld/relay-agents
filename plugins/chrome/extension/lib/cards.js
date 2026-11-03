// What an agent asks to do in this browser, as cards: the side panel's, and the window a notification opens while no
// panel is. The work is the service worker's (background.js, lib/agent.js): a page shows its cards and passes your
// answer on, after asking Chrome for its permission to use pages in your click (Chrome asks only then).

import { h, icon } from "./ui.js";

// The service worker's cards for this page, `onItems(items)` each time they change; connected again if Chrome
// restarted the worker. `decide(item, decision)` passes your answer on.
export function agentsLink(onItems) {
  let port = null;
  const link = () => {
    port = chrome.runtime.connect({ name: "agents" });
    port.onMessage.addListener((message) => onItems(message.items || []));
    port.onDisconnect.addListener(() => setTimeout(link, 500));
  };
  link();
  return {
    decide(item, decision) {
      // (Chrome's own question first, while it's still your click)
      const asked = decision === "deny" || item.kind === "tabs" ? Promise.resolve(true)
        : chrome.permissions.request({ origins: ["<all_urls>"] }).catch(() => false);
      asked.then((permitted) => port.postMessage({ decide: item.command.id, decision, permitted }));
    },
  };
}

// The cards in `container`. A card stays the same element until it changes (the worker says there's news every poll:
// a card made anew could lose your click).
export class Cards {
  constructor(container, { decide, openChat }) {
    this.container = container;
    this.decide = decide;
    this.openChat = openChat;
    this.made = new Map();  // the command's id: { el, state }
  }

  render(items) {
    const els = items.map((item) => {
      const known = this.made.get(item.command.id);
      if (known?.state === item.state) return known.el;
      const el = this.card(item);
      this.made.set(item.command.id, { el, state: item.state });
      return el;
    });
    for (const id of this.made.keys()) if (!items.some((item) => item.command.id === id)) this.made.delete(id);
    if (els.length !== this.container.children.length || els.some((el, i) => this.container.children[i] !== el)) {
      this.container.replaceChildren(...els);
    }
  }

  card(item) {
    const { command, state: now, what, chat, origin, text, submit } = item;
    const site = origin ? new URL(origin).host : null;
    const decide = (decision) => this.decide(item, decision);
    return h(`div.ask${now === "doing" ? ".doing" : ""}`,
      h("div.ask-head", icon("globe", 13),
        h("button.ask-chat", { title: "Open this chat", onclick: () => this.openChat(command.conversation_id) },
          chat || "A chat"),
        h("span", now === "doing" ? "is using Chrome:" : "wants to:")),
      h("div.ask-what", what),
      text !== undefined ? h("pre.ask-text", text || "(nothing: the field is emptied)") : null,
      submit ? h("div.faint", "and then press Enter (which sends it)") : null,
      now === "doing" ? h("div.ask-doing", h("span.spinner"), "Working…")
        : h("div.ask-buttons",
          h("button.btn.small", { onclick: () => decide("deny") }, "Deny"),
          h("button.btn.small.primary", { title: "This once (reading this site: for the rest of this answer)",
            onclick: () => decide("once") }, "Allow"),
          h("button.btn.small", { title: site ? `From now on, in every chat: ${site} (Settings can take it back)`
            : "From now on, in every chat (Settings can take it back)", onclick: () => decide("always") },
          site ? `Always on ${site}` : "Always")));
  }
}
