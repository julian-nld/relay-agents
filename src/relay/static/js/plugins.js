// Plugins' parts of Relay's page (relay.plugins; docs/WRITING-A-PLUGIN.md). Each loaded plugin's static/index.js is
// imported once the page is built, and its default export, setup(relay), gets what it may add: buttons on the
// activity bar, pages (/p/<plugin>/…) and Settings tabs, with Relay's API and UI pieces to build them. They load
// side by side; one whose part fails, or takes over SETUP_WAIT, is said in the console and left out (Settings →
// Plugins says so too); the page and the other plugins go on.

import { api } from "./api.js";
import { relayPath, relayUrl } from "./base.js";
import { renderMarkdown } from "./markdown.js";
import { clear, clock, confirmDialog, h, icon, modal, promptDialog, timeAgo, toast } from "./ui.js";

const SETUP_WAIT = 10000;  // ms a plugin's setup(relay) may take
const pages = [];  // [{plugin, pattern, render}]
const tabs = [];  // [{id, label, icon, render}]: Settings' tabs of plugins
export const failed = new Map();  // plugin name → why its page part didn't load
const loading = new Map();  // plugin name → its part loaded (or not)
let listed = Promise.resolve();  // the list of plugins asked for

// Load the plugins' page parts: their buttons go into `bar`; `navigate` goes to a page of Relay's
export function loadPlugins({ bar, navigate }) {
  pages.length = 0;
  tabs.length = 0;
  failed.clear();
  loading.clear();
  listed = (async () => {
    let plugins = [];
    try {
      plugins = (await api.get("/api/plugins")).plugins.filter((p) => p.loaded);
    } catch {
      return;  // (signed out meanwhile, say: the page goes on without them)
    }
    for (const plugin of plugins) {
      if (plugin.style && !document.querySelector(`link[data-plugin="${plugin.name}"]`)) {
        document.head.append(h("link", { rel: "stylesheet", href: relayUrl(`/plugins/${plugin.name}/static/style.css`),
          dataset: { plugin: plugin.name } }));
      }
      if (plugin.script) loading.set(plugin.name, load(plugin, { bar, navigate }));
    }
  })();
  return listed;
}

async function load(plugin, hooks) {
  let timer;
  try {
    const module = await import(relayUrl(`/plugins/${plugin.name}/static/index.js`));
    if (typeof module.default !== "function") throw new Error("its index.js has no default export: setup(relay)");
    await Promise.race([module.default(pluginApi(plugin, hooks)), new Promise((resolve, reject) => {
      timer = setTimeout(() => reject(new Error(`its setup(relay) didn't end within ${SETUP_WAIT / 1000} seconds`)),
        SETUP_WAIT);
    })]);
  } catch (error) {
    failed.set(plugin.name, error.message || String(error));
    console.warn(`Relay: the ${plugin.name} plugin's page part failed:`, error);
  } finally {
    clearTimeout(timer);
  }
}

// What a plugin's setup(relay) gets: the page's whole contract
function pluginApi(plugin, { bar, navigate }) {
  const own = (path = "") => `/api/plugins/${plugin.name}/${String(path).replace(/^\/+/, "")}`;
  return {
    name: plugin.name,
    label: plugin.label,
    // Its own routes (plugin.route), and Relay's API as the page uses it
    api: {
      get: (path) => api.get(own(path)),
      post: (path, body) => api.post(own(path), body),
      put: (path, body) => api.put(own(path), body),
      patch: (path, body) => api.patch(own(path), body),
      del: (path) => api.del(own(path)),
      relay: api,
    },
    // Relay's pieces, so a plugin looks like the rest of the page
    ui: { h, icon: glyph, clear, modal, toast, confirmDialog, promptDialog, timeAgo, clock, markdown },
    // A file of its static/ folder (an image, say)
    url: (path) => relayUrl(`/plugins/${plugin.name}/static/${String(path).replace(/^\/+/, "")}`),
    // One of its pages ("" its first, "notes/12" another), or Relay's own ("/c/<id>", "/tasks")
    navigate: (path = "") => navigate(String(path).startsWith("/") ? path : pagePath(plugin.name, path)),
    // A button on the activity bar, above the Shell; setBadge(n) shows a count on it (0: none)
    barItem({ title, icon: iconName = "star", onClick }) {
      const badge = h("span.bar-badge", { hidden: true });
      const button = h("button.bar-item.plugin-item", { type: "button", title, "aria-label": title,
        dataset: { plugin: plugin.name }, onclick: (event) => onClick?.(event, button) }, glyph(iconName), badge);
      button.classList.toggle("active", new RegExp(`^/p/${plugin.name}(/|$)`).test(relayPath()));  // (main.js: route)
      bar.append(button);
      return {
        el: button,
        setBadge(count) {
          badge.hidden = !count;
          badge.textContent = count > 99 ? "99+" : String(count || "");
        },
      };
    },
    // A page at /p/<plugin>/<path> ({part}s in it come as params): render(el, {params, query}) draws it into el,
    // and may give back a function that tidies up when another page opens
    page(path, render) {
      const parts = String(path).replace(/^\/+|\/+$/g, "").split("/").filter(Boolean);
      const pattern = new RegExp(`^${parts.map((part) => /^\{\w+\}$/.test(part) ? `(?<${part.slice(1, -1)}>[^/]+)`
        : part.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("/")}$`);
      pages.push({ plugin, pattern, render });
    },
    // A tab of Settings: render(pane) draws it
    settingsTab({ label, icon: iconName = "star", render }) {
      tabs.push({ id: `plugin-${plugin.name}-${tabs.length}`, label, icon: iconName, render, plugin: plugin.name });
    },
  };
}

// Markdown (an agent's answer, say) as Relay shows it: sanitised, its code highlighted
function markdown(text) {
  const el = h("div.md");
  renderMarkdown(el, String(text ?? ""));
  return el;
}

function pagePath(name, path) {
  const rest = String(path).replace(/^\/+|\/+$/g, "");
  return `/p/${name}${rest ? `/${rest}` : ""}`;
}

// A Relay icon by its name, or an SVG's inside (paths on a 24×24 grid) of the plugin's own
export function glyph(name, cls = "") {
  if (typeof name !== "string" || !name.trim().startsWith("<")) return icon(name, cls);
  const span = document.createElement("span");
  span.innerHTML = `<svg class="icon ${cls}" viewBox="0 0 24 24" aria-hidden="true">${name}</svg>`;
  return span.firstChild;
}

// Settings' tabs of the plugins: {id, label, icon, render(pane)}
export function pluginTabs() {
  return [...tabs];
}

// The page at `path` (/p/<plugin>/…), drawn into a fresh element: {el, title, close}; the plugin's own error, or
// "nothing here", when it can't
export async function pluginPage(path) {
  const [, , name, ...rest] = path.split("/");
  await listed;
  await loading.get(name);  // (its own part, not the others')
  const sub = rest.join("/");
  const el = h("div.plugin-page", { dataset: { plugin: name } });
  for (const page of pages) {
    const matched = page.plugin.name === name && sub.match(page.pattern);
    if (!matched) continue;
    let tidy = null;
    try {
      const params = Object.fromEntries(Object.entries(matched.groups || {}).map(([key, value]) => {
        try {
          return [key, decodeURIComponent(value)];
        } catch {
          return [key, value];
        }
      }));
      tidy = await page.render(el, { params,
        query: Object.fromEntries(new URLSearchParams(location.search)) });
    } catch (error) {
      console.warn(`Relay: the ${name} plugin's page failed:`, error);
      clear(el, h("p.error-text", `The ${page.plugin.label} plugin's page failed: ${error.message || error}`));
    }
    return { el, title: page.plugin.label, close: () => { if (typeof tidy === "function") tidy(); } };
  }
  clear(el, h("div.plugin-missing", h("h2", "Nothing here"),
    h("p.muted", failed.has(name) ? `The ${name} plugin's page part failed: ${failed.get(name)}`
      : `No plugin page at ${path}. Is the plugin on? Settings → Plugins says.`)));
  return { el, title: "Plugin", close: () => {} };
}
