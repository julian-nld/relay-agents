// Relay's app in OpenHands' Agent Canvas, installed and enabled by Relay's OpenHands plugin (its configure()).
// OpenHands' conversations are Relay's tasks: a conversation's ACP session is Relay's conversation, through the
// ACP forwarder. This page shows each with its task (state, agent, changes) and leads back to Relay. Canvas runs at
// Relay's /canvas, so Relay's own API is this page's too, with the user's sign-in (a cookie of the same origin).

const EVERY = 5000;  // ms between two looks while the page is open
const GROUPS = { running: "Working", review: "To review", clean: "Done", discarded: "Discarded" };
const STYLE = `
.relay-app { max-width: 920px; padding: 24px; color: var(--oh-foreground, inherit); }
.relay-app h2 { font-size: 1.25rem; font-weight: 600; margin: 0; }
.relay-app .ra-head { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 4px; }
.relay-app .ra-head h2 { margin-right: auto; }
.relay-app .ra-lead, .relay-app .ra-muted { color: var(--oh-muted, #8b8b8b); }
.relay-app .ra-lead { margin: 0 0 16px; font-size: 0.9rem; }
.relay-app button { font: inherit; cursor: pointer; border-radius: 8px; padding: 5px 12px; font-size: 0.85rem;
  border: 1px solid var(--oh-border, rgba(127, 127, 127, 0.35)); background: transparent; color: inherit; }
.relay-app button:hover { background: var(--oh-interactive-hover, rgba(127, 127, 127, 0.12)); }
.relay-app button:focus-visible { outline: 2px solid var(--oh-focus, #10a37f); outline-offset: 1px; }
.relay-app button.ra-primary { background: var(--oh-color-primary, #10a37f); border-color: transparent;
  color: var(--oh-accent-foreground, #fff); }
.relay-app ul { list-style: none; margin: 0; padding: 0; border: 1px solid var(--oh-border, rgba(127, 127, 127, 0.25));
  border-radius: 10px; overflow: hidden; }
.relay-app li { display: flex; align-items: center; gap: 12px; padding: 10px 14px;
  border-top: 1px solid var(--oh-border-subtle, rgba(127, 127, 127, 0.18)); }
.relay-app li:first-child { border-top: 0; }
.relay-app .ra-what { flex: 1; min-width: 0; }
.relay-app .ra-title { all: unset; cursor: pointer; font-weight: 500; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; display: block; }
.relay-app .ra-title:hover { text-decoration: underline; }
.relay-app .ra-title:focus-visible { outline: 2px solid var(--oh-focus, #10a37f); }
.relay-app .ra-meta { font-size: 0.8rem; color: var(--oh-muted, #8b8b8b); overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; }
.relay-app .ra-state { font-size: 0.75rem; padding: 2px 8px; border-radius: 999px; white-space: nowrap;
  border: 1px solid var(--oh-border, rgba(127, 127, 127, 0.3)); }
.relay-app .ra-state.running { color: var(--oh-color-primary, #10a37f); border-color: currentColor; }
.relay-app .ra-state.review { color: var(--oh-warning, #b7791f); border-color: currentColor; }
.relay-app .ra-state.asks { color: var(--oh-accent-foreground, #fff); background: var(--oh-color-primary, #10a37f);
  border-color: transparent; }
.relay-app .ra-error { color: var(--oh-color-danger, #d93f3f); }
@media (max-width: 640px) { .relay-app { padding: 16px; } .relay-app li { flex-wrap: wrap; } }
`;

export function activate(host) {
  if (host.apiVersion !== "1") throw new Error(`Relay's app needs Canvas' app API 1, not ${host.apiVersion}`);
  return host.registerPage("relay", ({ container, navigate }) => mount(host, container, navigate || host.navigate));
}

// OpenHands' conversations, each with its Relay task when there is one (the ACP session is Relay's conversation)
export function match(conversations, groups) {
  const tasks = new Map();
  for (const [group, items] of Object.entries(groups && typeof groups === "object" ? groups : {})) {
    for (const item of Array.isArray(items) ? items : []) if (item && item.id) tasks.set(item.id, { ...item, group });
  }
  return (Array.isArray(conversations) ? conversations : []).filter((c) => c && c.id).map((c) => {
    const relayId = (c.agent_state && c.agent_state.acp_session_id) || null;
    return { id: c.id, title: c.title || "Untitled", status: c.execution_status || "idle",
      folder: (c.workspace && c.workspace.working_dir) || "", relayId,
      task: relayId ? tasks.get(relayId) || null : null };
  });
}

// What a task's line says: its state (a question of its agent's first: OpenHands can't show those), its agent, its
// changes
export function describe(task) {
  const parts = [];
  if (task.provider_id) parts.push(task.model ? `${task.provider_id} · ${task.model}` : task.provider_id);
  const changes = task.changes;
  if (changes && changes.files) {
    parts.push(
      `${changes.files} file${changes.files === 1 ? "" : "s"} +${changes.additions || 0} −${changes.deletions || 0}`);
  }
  if (task.needs_approval) return { state: "Asks you", kind: "asks", parts };
  return { state: GROUPS[task.group] || "", kind: task.group, parts };
}

// Relay's page at `path`: the whole page moves there when Canvas is framed in Relay, else this tab
function toRelay(path) {
  const url = new URL(path, location.origin).href;
  try {
    if (window.top !== window && window.top.location.origin === location.origin) {
      window.top.location.assign(url);
      return;
    }
  } catch {
    // (framed by another site: this tab, then)
  }
  location.assign(url);
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  node.append(...children.flat().filter((c) => c !== null && c !== undefined && c !== false));
  return node;
}

function mount(host, container, navigate) {
  const style = el("style", {}, STYLE);
  const list = el("div", {}, el("p", { class: "ra-muted" }, "Looking…"));
  const root = el("section", { class: "relay-app", "aria-labelledby": "relay-app-title" },
    el("div", { class: "ra-head" },
      el("h2", { id: "relay-app-title" }, "Relay"),
      el("button", { type: "button", onclick: () => toRelay("/tasks") }, "Relay's tasks"),
      el("button", { type: "button", class: "ra-primary", onclick: () => toRelay("/") }, "Back to Relay")),
    el("p", { class: "ra-lead" }, "A conversation here works with Relay's agents: in one of your projects it's a "
      + "Relay task, with its changes in Relay's Review."),
    list);
  document.head.append(style);
  container.append(root);
  const stop = new AbortController();
  let timer = null;

  async function conversations() {
    const answer = await host.agentServer.request({ method: "GET",
      path: "/api/conversations/search?limit=50&sort_order=UPDATED_AT_DESC" });
    const page = answer && typeof answer === "object" && "data" in answer && "status" in answer ? answer.data : answer;
    return page && Array.isArray(page.items) ? page.items : [];
  }

  async function relayTasks() {
    const answer = await fetch("/api/tasks", { credentials: "same-origin", headers: { Accept: "application/json" },
      signal: stop.signal });
    if (answer.status === 401) return { signedOut: true };
    const type = answer.headers.get("content-type") || "";
    if (!answer.ok || !type.includes("application/json")) return { elsewhere: true };
    return { groups: await answer.json() };
  }

  function draw(rows, relay) {
    if (relay.elsewhere) {
      list.replaceChildren(el("p", {}, "This OpenHands isn't opened through Relay. Open it from Relay's OpenHands page "
        + "(the hand on Relay's bar): there this page shows your tasks."));
      return;
    }
    if (relay.signedOut) {
      list.replaceChildren(el("p", {}, "Signed out of Relay. ",
        el("button", { type: "button", onclick: () => toRelay("/") }, "Sign in")));
      return;
    }
    if (!rows.length) {
      list.replaceChildren(el("p", { class: "ra-muted" }, "No conversations yet. Start one in a project (Open "
        + "Workspace): Relay's agent takes it on."));
      return;
    }
    list.replaceChildren(el("ul", {}, rows.map((row) => {
      const told = row.task ? describe(row.task) : null;
      const folder = row.folder ? row.folder.split("/").filter(Boolean).at(-1) : "";
      return el("li", {},
        el("div", { class: "ra-what" },
          el("button", { type: "button", class: "ra-title", title: "Open in OpenHands",
            onclick: () => navigate(`/conversations/${encodeURIComponent(row.id)}`) }, row.title),
          el("div", { class: "ra-meta" }, [folder, ...(told ? told.parts : row.relayId ? ["a chat in Relay"]
            : ["not with Relay's agent"])].filter(Boolean).join(" · "))),
        told && told.state ? el("span", { class: `ra-state ${told.kind}` }, told.state) : null,
        row.relayId ? el("button", { type: "button", class: told && told.kind === "asks" ? "ra-primary" : "",
          onclick: () => toRelay(`/c/${encodeURIComponent(row.relayId)}`) }, told && told.kind === "asks"
          ? "Answer in Relay" : told && told.kind === "review" ? "Review in Relay" : "Open in Relay") : null);
    })));
  }

  async function look() {
    try {
      const [rows, relay] = await Promise.all([conversations(), relayTasks()]);
      if (stop.signal.aborted) return;
      draw(match(rows, relay.groups), relay);
    } catch (error) {
      if (stop.signal.aborted) return;
      list.replaceChildren(el("p", { class: "ra-error" }, `Couldn't look: ${error && error.message || error}`));
    }
    timer = setTimeout(look, EVERY);
  }
  look();

  return () => {
    stop.abort();
    clearTimeout(timer);
    root.remove();
    style.remove();
  };
}
