// The window a notification opens while no side panel is: the cards of what Relay's agents ask to do in this browser
// (lib/cards.js), the one notified first. It closes once nothing waits for you.

import { connect } from "./lib/relay.js";
import { agentsLink, Cards } from "./lib/cards.js";
import { fillIcons } from "./lib/ui.js";

const first = decodeURIComponent(location.hash.slice(1));
const asks = document.getElementById("asks");
const none = document.getElementById("none");
let decided = false;
let closing = null;

const cards = new Cards(asks, {
  decide: (item, decision) => {
    decided = true;
    agents.decide(item, decision);
  },
  // (the chat in Relay, in a tab of the last window you used)
  openChat: async (id) => {
    const url = (await connect()).link(`/c/${id}`);
    if (url) chrome.tabs.create({ url });
  },
});

const agents = agentsLink((items) => {
  const shown = [...items].sort((a, b) => (b.command.id === first) - (a.command.id === first));
  cards.render(shown);
  const waiting = shown.some((item) => item.state === "asking");
  none.hidden = shown.length > 0;
  clearTimeout(closing);
  // (done: closed shortly after your last answer; or, opened when nothing waited, after a moment to read that)
  if (!waiting && !shown.some((item) => item.state === "doing")) closing =
    setTimeout(() => window.close(), decided ? 800 : 4000);
});

fillIcons();
