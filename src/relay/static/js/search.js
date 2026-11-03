// Relay's web search and fetch for agents (relay/search.py: a local SearXNG; relay/fetch.py: a
// browser when it must): search on or off, their route (this server's own connection or a VPN
// tunnel's), and a search or fetch to try.

import { api } from "./api.js";
import { clear, h, icon, toast } from "./ui.js";

export function searchSection() {
  // Drawn when its status comes (podman is asked): the page around it doesn't wait for that
  const section = h("div", h("div.spinner"));
  const render = async () => {
    let found;
    try {
      found = await api.get("/api/search");
    } catch (failure) {
      clear(section, h("p.error-text", failure.message));
      return;
    }
    const save = async (change) => {
      try {
        await api.put("/api/search", change);
        toast("Web search saved");
      } catch (failure) {
        toast(failure.message, "error");
      }
      render();
    };
    const enabled =
      h("input", { type: "checkbox", checked: found.enabled, onchange: () => save({ enabled: enabled.checked }) });
    const route = h("select.select", { "aria-label": "Route", onchange: () => save({ tunnel: route.value }) },
      h("option", { value: "", selected: !found.tunnel }, "This server's own connection"),
      found.tunnels.map((t) =>
        h("option", { value: t.id, selected: t.id === found.tunnel }, `Through the VPN tunnel ${t.name}`)));
    const s = found.status;
    const running = [s.running ? "web search" : null, s.fetching ? "web fetch" : null].filter(Boolean);
    const status =
      h("p.muted.small-text",
        running.length ? `Running: ${running.join(" and ")}${s.route ? `, through ${s.route}` : ""}.`
        : "Stopped: each starts with the next search or fetch (a few seconds), and stops after half an hour unused.");
    const query = h("input.input", { placeholder: "Try a search, or an address to fetch", "aria-label": "Try a search",
      onkeydown: (e) => { if (e.key === "Enter") go.click(); } });
    const results = h("div.search-results");
    const go = h("button.btn.small", { onclick: async () => {
      if (!query.value.trim()) return;
      go.disabled = true;
      clear(results, h("p.muted.small-text", "Searching…"));
      try {
        const answer = await api.post("/api/search/try", { query: query.value });
        if (answer.fetched !== undefined) {
          clear(results, h("p.small-text", answer.fetched),
            h("pre.log.search-fetched", answer.text + (answer.more ? "\n…" : "")));
          return;
        }
        clear(results, answer.results.length ? answer.results.map((r) => h("div.search-result",
          h("a", { href: r.url, target: "_blank", rel: "noopener" }, r.title),
          h("div.faint.truncate", r.url), r.content ? h("div.small-text", r.content) : null))
            : h("p.muted", "Nothing found."),
        answer.unresponsive.length
          ? h("p.faint.small-text", `Didn't answer: ${answer.unresponsive.join(", ")}`) : null);
        status.textContent = `Running${s.route ? `, through ${s.route}` : ""}.`;  // (the one tried, at least)
      } catch (failure) {
        clear(results, h("p.error-text.small-text", failure.message));
      } finally {
        go.disabled = false;
      }
    } }, icon("search", "sm"), "Search");
    clear(section,
      h("label.field.inline", enabled, h("span", "Give agents Relay's web search",
        h("span.hint.block", "Chat models get it as the tool web_search, agents with "
          + "a shell as the command relay search or the MCP tool search; subagents "
          + "too. Their own search tools stay. Relay's web fetch (web_fetch, relay "
          + "fetch, the MCP tool fetch) is always there: a page in a real browser when a plain fetch is refused."))),
      h("label.field", h("span", "Searches and fetches go out", h("span.hint.block",
        "Through a VPN tunnel: never around it when it's down (then nothing is searched).")), route),
      status,
      h("div.search-try", query, go), results);
  };
  render();
  return section;
}
