// Settings: general, providers (templates), workspaces and account.

import { BASE, relayUrl } from "./base.js";
import { api } from "./api.js";
import { drivesSection } from "./drive.js";
import { limitRow } from "./limits.js";
import { glyph, failed as pluginsFailed, pluginTabs } from "./plugins.js";
import { renderMarkdown } from "./markdown.js";
import {
  BROWSER_CATEGORIES, browserAnnounces, browserNotificationsOn, browserNotificationsSupported, setBrowserAnnounces,
  setBrowserNotifications,
} from "./notifications.js";
import { restartRelay } from "./restart.js";
import { searchSection } from "./search.js";
import { systemPage } from "./system.js";
import { state } from "./state.js";
import { inItsBox, mountTerminal } from "./terminal.js";
import { setTheme, storedTheme } from "./theme.js";
import {
  clear, confirmDialog, copyText, h, icon, modal, MODE_ICONS, promptDialog, showMenu, statusDot, timeAgo, toast,
} from "./ui.js";
import { vpnSection } from "./vpn.js";

const TABS = [
  ["general", "General", "settings"],
  ["providers", "Providers", "plug"],
  ["modes", "Modes", "tool"],
  ["subagents", "Subagents", "fork"],
  ["computers", "Computers", "monitor"],
  ["skills", "Skills", "brain"],
  ["memory", "Memory", "book"],
  ["usage", "Usage", "list"],
  ["agentos", "AgentOS", "compass"],
  ["system", "System", "box"],
  ["plugins", "Plugins", "puzzle"],
  ["workspaces", "Workspaces", "folder"],
  ["limits", "Limits", "shield"],
  ["notifications", "Notifications", "bell"],
  ["account", "Account", "user"],
];

export function openSettings(hooks) {
  const m = modal("Settings", { wide: true });
  m.el.classList.add("settings-modal");
  m.foot.remove();
  const nav = h("nav", { "aria-label": "Settings categories" });
  // (and the plugins' own tabs, after Relay's: relay.plugins, static/js/plugins.js)
  const extra = pluginTabs();
  const tabs = [...TABS, ...extra.map((tab) => [tab.id, tab.label, tab.icon])];
  const category = h("select.select", { "aria-label": "Settings category", onchange: () => select(category.value) },
    tabs.map(([id, label]) => h("option", { value: id }, label)));
  const buttons = tabs.map(([id, label, iconName]) =>
    h("button", { type: "button", onclick: () => select(id) }, glyph(iconName, "sm"), label));
  nav.append(...buttons);
  let pane = h("div.pane");
  m.body.replaceWith(h("div.settings", nav,
    h("label.settings-category", h("span", "Category"), category), pane));

  function select(tab) {
    category.value = tab;
    buttons.forEach((button, i) => {
      const selected = tabs[i][0] === tab;
      button.classList.toggle("on", selected);
      if (selected) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
    const render =
      { general, providers, modes, subagents, computers, skills, memory: memoryPage, usage, agentos: agentosPage,
        workspaces, plugins: pluginsPage,
        limits, notifications, account,
        system: (pane, hooks, modal) => systemPage(pane, hooks, modal, pageHeading) }[tab]
      || pluginTab(extra.find((t) => t.id === tab));
    // A fresh pane each time: a tab still loading draws into its own, gone, one.
    const fresh =
      h("div.pane",
        { role: "region", "aria-label": `${tabs.find(([id]) => id === tab)[1]} settings` }, h("div.spinner"));
    pane.replaceWith(fresh);
    pane = fresh;
    render(fresh, hooks, m);
  }
  select(tabs.some(([id]) => id === hooks.tab) ? hooks.tab : "general");
}

function pageHeading(title, description, ...actions) {
  return h("div.settings-page-head", h("div.grow", h("h3", title), description ? h("p.lead", description) : null),
    actions.length ? h("div.settings-head-actions", actions) : null);
}

function settingsSection(title, description, ...content) {
  return h("section.settings-section", h("h4", title), description ? h("p.lead", description) : null, content);
}

function settingsActions(...buttons) {
  return h("div.settings-actions", buttons);
}

// ---------------------------------------------------------------- plugins

// A plugin's own tab: what its render(pane) draws (its mistake said there, not the window's)
function pluginTab(tab) {
  return async (pane) => {
    clear(pane);
    try {
      await tab.render(pane);
    } catch (error) {
      console.warn(`Relay: the ${tab.plugin} plugin's Settings tab failed:`, error);
      clear(pane, h("p.error-text", `The ${tab.plugin} plugin's tab failed: ${error.message || error}`));
    }
  };
}

// The plugins in RELAY_HOME/plugins (relay.plugins): on or off, and why one isn't loaded. Off, one stops at once; on
// again, at once if Relay loaded it at its start, else from the next. Its buttons and pages follow at a reload.
async function pluginsPage(pane) {
  let data;
  try {
    data = await api.get("/api/plugins");
  } catch (error) {
    clear(pane, h("p.error-text", error.message));
    return;
  }
  const needs = new Map();  // plugin → what its change needs: "restart", "reload" (for its page's part) or nothing
  const restart = h("button.btn.small", { onclick: async () => {
    if (await confirmDialog("Restart Relay?", "Relay starts anew and loads the plugins that are on; it refuses while "
      + "agents are working. The page reloads when it's back.", { confirm: "Restart" })) restartRelay();
  } }, icon("refresh", "sm"), "Restart Relay");
  const note = h("p.muted.plugins-restart", { hidden: true });
  const sayNeeds = () => {
    const all = [...needs.values()];
    note.textContent = all.includes("restart") ? "Restart Relay for the change to take effect."
      : "Reload the page for the plugin's buttons and pages to follow.";
    note.hidden = !all.some(Boolean);
  };
  const row = (p) => {
    const badge = () => (p.problem ? h("span.badge.danger", "can't be loaded") : !p.enabled ? h("span.badge", "off")
      : p.loaded ? h("span.badge.ok", "loaded") : h("span.badge", "on at the next start"));
    let state = badge();
    const shown = p.script;  // (its page's part: this page has it, or not)
    const on = h("input", { type: "checkbox", checked: p.enabled, "aria-label": `${p.label} on`, onchange: async () => {
      try {
        const saved = await api.patch(`/api/plugins/${encodeURIComponent(p.name)}`, { enabled: on.checked });
        Object.assign(p, saved);
        const now = badge();
        state.replaceWith(now);
        state = now;
        toggle.title = p.enabled ? "On" : "Off";
        needs.set(p.name, saved.restart ? "restart" : saved.script !== shown ? "reload" : null);
        sayNeeds();
      } catch (error) {
        on.checked = !on.checked;
        toast(error.message, "error");
      }
    } });
    const toggle = h("label.switch", { title: p.enabled ? "On" : "Off" }, on, h("span"));
    const errors = [...Object.entries(p.errors).map(([where, why]) => `Its ${where} failed: ${why}`),
      ...(pluginsFailed.has(p.name) ? [`Its page part failed: ${pluginsFailed.get(p.name)}`] : [])];
    return h("div.provider-row.plugin-row", toggle,
      h("div.info",
        h("div.name", p.label, " ", p.version ? h("span.muted", p.version) : null, " ", state),
        p.description ? h("div.sub", p.description) : null,
        h("div.sub", [p.folder, p.tools.length ? `tools: ${p.tools.join(", ")}` : null].filter(Boolean).join(" · ")),
        p.problem ? h("div.sub.fail", p.problem) : null,
        p.exposed ? h("div.sub.fail", "Its folder is in a workspace root, where agents work: they could change its "
          + "code. Keep plugins outside the workspace roots.") : null,
        errors.map((text) => h("div.sub.fail", text))));
  };
  const none = h("p.muted", { hidden: data.plugins.length > 0 }, "No plugins yet.");
  const list = h("div.plugin-list", data.plugins.map(row));
  const bundled = (p) => {
    const item = h("div.provider-row.plugin-row",
      h("div.info", h("div.name", p.label), p.description ? h("div.sub", p.description) : null),
      h("div.actions", h("button.btn.small", { onclick: async () => {
        try {
          list.append(row(await api.post(`/api/plugins/${encodeURIComponent(p.name)}`)));
          item.remove();
          none.hidden = true;
          needs.set(p.name, "restart");
          sayNeeds();
        } catch (error) {
          toast(error.message, "error");
        }
      } }, "Turn on")));
    return item;
  };
  clear(pane,
    pageHeading("Plugins", "What's new in Relay comes as plugins: each a folder in Relay's plugins folder, with "
      + "buttons and pages for this page, API routes, background work and tools for agents. Relay loads them when "
      + "it starts. A plugin runs with Relay's own rights: only add plugins you trust.", restart),
    settingsSection("Installed", `Put a plugin's folder in ${data.folder}, then restart Relay. `
      + "docs/WRITING-A-PLUGIN.md says how to write one.",
    none, list, note),
    data.bundled?.length ? settingsSection("Come with Relay", "Off until you turn one on. Each one's README says more.",
      h("div.plugin-list", data.bundled.map(bundled))) : null);
}

// ---------------------------------------------------------------- AgentOS

// AgentOS in this Relay, or not (relay.agentos: included): one switch. Out, it's gone from Relay's screens.
async function agentosPage(pane, hooks) {
  let os;
  try {
    os = await api.get("/api/agentos");
  } catch (error) {
    clear(pane, h("p.error-text", error.message));
    return;
  }
  const on = h("input", { type: "checkbox", checked: os.settings.included, "aria-label": "AgentOS in Relay",
    onchange: async () => {
      try {
        const saved = await api.put("/api/agentos", { included: on.checked });
        hooks.onAgentOS?.(saved.included);
        toast(saved.included ? "AgentOS is on" : "AgentOS is off: it's gone from Relay's screens");
        agentosPage(pane, hooks);
      } catch (error) {
        on.checked = !on.checked;
        toast(error.message, "error");
      }
    } });
  clear(pane,
    pageHeading("AgentOS", "Relay's own agent, the Governor, which carries out your tasks with other agents and "
      + "looks after your live services while you're away."),
    h("div.provider-row", h("label.switch", on, h("span")),
      h("div.info", h("div.name", "AgentOS"), h("div.sub", os.settings.included
        ? "On: the AgentOS page (the Governor, its tasks, the model ranking), Production boxes (your live services), "
          + "/promote and Promote to live, and AgentOS's conversations."
        : "Off: none of that is on Relay's screens, and the Governor isn't woken."))),
    settingsSection("Turned off", null, h("ul.agentos-off",
      h("li", "What it made is kept: its conversations, its memory, its tasks; on again, all of it is back as it was."),
      h("li",
        "Your live services keep running at their addresses; when one fails, it's pushed to your phone if "
        + "you set that up (Notifications)."),
      h("li", "Your own scheduled tasks are under Scheduled in the sidebar; the Governor's wait."))));
}

// ---------------------------------------------------------------- general

async function general(pane, hooks) {
  const current = storedTheme();
  const options = [["system", "System", "monitor"], ["light", "Light", "sun"], ["dark", "Dark", "moon"]];
  const toggle = h("div.mode-toggle");
  const render = (value) => clear(toggle, options.map(([id, label, iconName]) =>
    h(`button${id === value ? ".on" : ""}`, { onclick: () => { setTheme(id); render(id); } },
      icon(iconName, "sm"), label)));
  render(current);
  clear(pane,
    pageHeading("General", "Appearance and shortcuts for this browser, and Relay's web search and fetch."),
    settingsSection("Appearance", "Theme of this browser. Saved automatically here.", toggle),
    settingsSection("Keyboard", "With a keyboard: Enter sends a message, Shift+Enter adds a line, "
      + "Ctrl+Shift+O starts a new conversation. Shift+Tab in the message box cycles through the available "
      + "modes; outside it, Shift+Tab moves focus backwards. On touchscreens, Enter adds a line; tap Send to send."),
    settingsSection("Web search and fetch", "Relay's own search engine for agents (a local SearXNG, which asks several "
      + "search engines at once), and its own fetch, which opens a page in a browser when a plain fetch is refused.",
    searchSection()),
    settingsSection("About", `Relay ${hooks.version}. Providers run on the machine Relay runs on.`));
}

// ---------------------------------------------------------------- providers

async function providers(pane, hooks, m) {
  let data;
  let kinds;
  try {
    [data, kinds] = await Promise.all([api.get("/api/providers"), api.get("/api/provider-kinds")]);
  } catch (error) {
    clear(pane, h("p.error-text", error.message));
    return;
  }
  const kindInfo = Object.fromEntries(kinds.map((k) => [k.kind, k]));
  const changed = () => hooks.onProvidersChanged?.();
  if (hooks.models) {  // opened from a model picker: straight to that provider's models
    const chosen = data.providers.find((p) => p.id === hooks.models);
    hooks.models = null;
    if (chosen) {
      modelsPane(pane, hooks, m, chosen);
      return;
    }
  }

  const rows = data.providers.map((p) => {
    const result = h("div.check-result");
    const enabled =
      h("input", { type: "checkbox", checked: p.enabled, "aria-label": `Enable ${p.name}`, onchange: async () => {
        try {
          await api.put(`/api/providers/${p.id}`, { id: p.id, name: p.name, kind: p.kind,
            enabled: enabled.checked, options: p.options });
          p.enabled = enabled.checked;
          toast("Provider setting saved");
          changed();
        } catch (error) {
          enabled.checked = !enabled.checked;
          toast(error.message, "error");
        }
      } });
    const test = h("button.btn.small", { onclick: async () => {
      test.disabled = true;
      result.className = "check-result";
      result.textContent = "Checking…";
      try {
        const r = await api.post(`/api/providers/${p.id}/check`);
        result.className = `check-result ${r.ok ? "ok" : "fail"}`;
        result.textContent = r.message;
      } catch (error) {
        result.className = "check-result fail";
        result.textContent = error.message;
      }
      test.disabled = false;
    } }, "Test");
    return h("div.provider-row",
      h("label.switch", { title: p.enabled ? "Enabled" : "Disabled" }, enabled, h("span")),
      h("div.info",
        h("div.name", p.name, " ", h("span.badge", p.tools ? "agent" : "chat"),
          // (added by AgentOS's Governor, which may remove it: relay/agentos.py provider.add)
          p.managed ? h("span.badge.managed",
            { title: "Added by AgentOS's Governor, which may remove it" }, "added by AgentOS")
            : null),
        h("div.sub", [p.label, p.detail, p.id].filter(Boolean).join(" · ")), result),
      h("div.actions", test,
        // (a shell in its box, or in a box with its home)
        p.shell || p.home || p.sign_in
          ? h("button.icon-btn.sm", { title: p.home ? "Shell in its box, in its home: to log in again, say"
            : inItsBox(p), "aria-label": "Shell", onclick: () => homeShell(p) }, icon("terminal", "sm")) : null,
        h("button.btn.small",
          { title: "Which models work; test them", onclick: () => modelsPane(pane, hooks, m, p) }, "Models"),
        h("button.icon-btn.sm", { title: "Check it fully: run it as Relay does, through a few short prompts",
          "aria-label": "Check it fully", onclick: () => conformanceDialog(p) }, icon("shield", "sm")),
        h("button.icon-btn.sm", { title: "Repair: have another agent fix Relay's support for it",
          onclick: () => {
            m.close();
            hooks.onOpenPage?.(`/providers/repair/new?provider=${encodeURIComponent(p.id)}`);
          } },
        icon("tool", "sm")),
        h("button.icon-btn.sm",
          { title: "Edit", onclick: () => editor(pane, hooks, m, kindInfo[p.kind], p) }, icon("edit", "sm")),
        h("button.icon-btn.sm", { title: "Delete", onclick: async () => {
          const ok = await confirmDialog("Delete provider?",
            `${p.name} will be removed. Chats that use it can switch to another provider.`,
            { confirm: "Delete", danger: true, focusCancel: true });
          if (!ok) return;
          try {
            await api.del(`/api/providers/${p.id}`);
            changed();
            providers(pane, hooks, m);
          } catch (error) {
            toast(error.message, "error");
          }
        } }, icon("trash", "sm"))));
  });

  const suggestions = (data.suggestions || []).map((template) => h("div.provider-row.suggestion",
    h("div.info", h("div.name", template.name, " ", h("span.badge", "agent")),
      h("div.sub", `opencode-manager profile “${template.options.profile || "default"}”`)),
    h("div.actions", h("button.btn.small", { onclick: async () => {
      try {
        await api.post("/api/providers", { ...template, enabled: true });
        toast(`${template.name} added`);
        changed();
        providers(pane, hooks, m);
      } catch (error) {
        toast(error.message, "error");
      }
    } }, icon("plus", "sm"), "Add"))));
  const problems = Object.entries(data.problems || {});
  const broken = Object.entries(data.broken_kinds || {});  // (a mistake in a provider folder: the rest work)
  clear(pane,
    pageHeading("Providers",
      "Each provider is a JSON template in Relay's data folder. Enable/disable is saved immediately; edits use Save.",
      h("button.btn.primary.small", { onclick: () => chooseKind(pane, hooks, m, kinds) },
        icon("plus", "sm"), "Add provider")),
    h("div.settings-list", rows.length ? rows : h("p.muted", "No providers yet.")),
    suggestions.length ? settingsSection("Detected opencode profiles",
      "Each profile has its own logins and models, e.g. a ChatGPT account or local models.",
      h("div.settings-list", suggestions)) : null,
    problems.length ? settingsSection("Broken templates", null,
      problems.map(([file, why]) => h("div.run-error", icon("alert"), h("div", h("b", file), ": ", why)))) : null,
    broken.length ? settingsSection("Provider folders that can't be loaded",
      "Relay works without them; fix the folder and restart Relay (docs/ADDING-A-PROVIDER.md).",
      broken.map(([folder, why]) => h("div.run-error", icon("alert"), h("div", h("b", folder), ": ", why)))) : null);
}

// A provider's models: which worked last time (in a chat or a test), and a test for each.
async function modelsPane(pane, hooks, m, p) {
  const back = () => providers(pane, hooks, m);
  const backButton =
    h("button.btn.small.ghost", { title: "Back to providers", onclick: back }, icon("arrowLeft", "sm"), "Back");
  const head = pageHeading(`Models of ${p.name}`, null, backButton);
  clear(pane, head, h("div.spinner"));
  let models;
  let known;
  try {
    [models, known] = await Promise.all([
      api.get(`/api/providers/${p.id}/models`).then((d) => d.models), api.get("/api/models/status")]);
  } catch (error) {
    clear(pane, head, h("p.error-text", error.message));
    return;
  }
  const status = (id) => known[`${p.id}/${id}`] || null;
  if (!models.some((x) => x.id === "")) models.unshift({ id: "", name: "Default model" });
  // Models used in chats that the provider doesn't list (typed in as a custom model).
  for (const entry of Object.values(known)) {
    if (entry.provider_id === p.id && !models.some((x) => x.id === entry.model)) {
      models.push({ id: entry.model, name: entry.model });
    }
  }
  const describe = (s) => {
    if (!s) return ["Not used or tested yet", ""];
    const when = new Date(s.checked * 1000).toLocaleString();
    return s.ok ? [`Worked · ${s.source === "test" ? "tested" : "used"} ${when}`, ""]
      : [`Failed · ${s.source === "test" ? "tested" : "used"} ${when}: ${s.message}`, ".fail"];
  };
  const testOne = async (model, row) => {
    const button = row.querySelector("button");
    button.disabled = true;
    button.textContent = "Testing…";
    try {
      known[`${p.id}/${model.id}`] = await api.post(`/api/providers/${p.id}/test`, { model: model.id });
    } catch (error) {
      toast(error.message, "error");
    }
    row.replaceWith(render(model));
    hooks.onProvidersChanged?.();
  };
  const render = (model) => {
    const s = status(model.id);
    const [text, cls] = describe(s);
    const row = h("div.model-row",
      s ? statusDot(s.ok ? "ok" : "fail") : h("span.status-dot"),
      h("div.info", h("div", model.name || model.id || "Default model",
        model.name && model.id && model.name !== model.id ? h("span.faint", ` · ${model.id}`) : null),
      h(`div.sub${cls}`, text)),
      h("button.btn.small", { onclick: () => testOne(model, row) }, "Test"));
    row.dataset.model = model.id;
    return row;
  };
  const list = h("div.settings-list", models.map(render));
  const testAll = h("button.btn.small", { onclick: async () => {
    if (models.length > 10 && !await confirmDialog("Test every model?",
      `That sends a small prompt to each of the ${models.length} models, one after the other.`,
      { confirm: "Test all" })) return;
    testAll.disabled = true;
    for (const model of models) {
      const row = [...list.children].find((r) => r.dataset.model === model.id);
      if (row) await testOne(model, row);
    }
    testAll.disabled = false;
  } }, icon("refresh", "sm"), "Test all");
  clear(pane,
    pageHeading(`Models of ${p.name}`, "Every chat notes whether its model worked; a "
      + "test sends it a tiny prompt (“Reply with just the word OK”) to see whether "
      + "it works now: a usage limit, an expired login or a model that's gone shows up here.", backButton, testAll),
    list);
}

// Check it fully (relay.providers.conformance): the provider run as Relay runs it, in a scratch folder, through a
// few short prompts to the model chosen; each scenario with how it went
function conformanceDialog(target) {
  const d = modal(`Check ${target.name} fully`);
  const model = h("select.select", { "aria-label": "Model" }, h("option", { value: "" }, "Its default model"));
  api.get(`/api/providers/${target.id}/models`).then(({ models }) => {
    for (const m of models.filter((m) => m.id)) model.append(h("option", { value: m.id }, m.name));
  }).catch(() => {});
  const results = h("div.conformance", { "aria-live": "polite" });
  const start = h("button.btn.primary", { onclick: async () => {
    start.disabled = true;
    clear(results, h("p.muted.conformance-wait", h("span.spinner.sm"),
      "Checking… a few short prompts, each a real answer: a minute or two."));
    try {
      const r = await api.post(`/api/providers/${target.id}/conformance`, { model: model.value });
      clear(results, h(`p.${r.ok ? "conformance-ok" : "error-text"}`,
        (r.ok ? "It does what Relay needs of it."
          : "Not yet: see what failed below (docs/ADDING-A-PROVIDER.md says how).")
        + (r.results.some((x) => x.ok === null)
            ? " A scenario marked – couldn't be shown, which doesn't count against it." : "")),
      r.results.map((x) => h(`div.conformance-row${x.ok === false ? ".fail" : x.ok ? ".ok" : ""}`,
        icon(x.ok === false ? "x" : x.ok ? "check" : "info", "sm"), h("b", x.name), h("span.muted", x.detail))));
    } catch (error) {
      clear(results, h("p.error-text", error.message));
    }
    start.disabled = false;
    start.lastChild.textContent = "Check again";
  } }, icon("shield", "sm"), h("span", "Check"));
  d.body.append(
    h("p.muted",
      `Runs ${target.name} as Relay runs it (its program in its box, its commands in a WorkBox), in a scratch `
      + "folder: a turn, a turn that goes on in its session, one stopped halfway and, for an agent, a command. "
      + "Every event is checked against what Relay reads."),
    h("label.field", h("span", "Model"), model), results);
  d.foot.append(h("button.btn", { onclick: () => d.close() }, "Close"), start);
}

// A provider added with an agent (relay/creations.py): a shell in a box with its home, where its program is installed
// and its login kept, which goes with the shell. Any other with a box: a shell in that box, which stays.
function homeShell(p) {
  const status = h("span.shell-status", { role: "status" }, "Connecting to its box…");
  const host = h("div.shell-terminal.home-shell", { "aria-label": `Shell in ${p.name}'s ${p.home ? "home" : "box"}` });
  let shell = null;
  const d = modal(`Shell: ${p.name}`, { wide: true, onClose: () => shell?.end() });
  shell = mountTerminal(host, `/api/providers/${p.id}/terminal`, status, {
    connected: p.home ? "A shell in a box with its home: log in again here; what you change in the home stays."
      : inItsBox(p) });
  d.body.append(h("p.muted", p.home ? `${p.home}: the program and its login, as its box has them.`
    : `${inItsBox(p)} What its programs start from here, such as a script's shell, goes to a WorkBox, as in its runs.`),
    host, h("div.service-foot", status, shell.keys));
  d.foot.append(h("button.btn", { onclick: () => d.close() }, "Close"));
}

// Fallback loops (relay.fallback): named lists of agents, each going on when the one before stops.
async function loopsEditor() {
  const box = h("div");
  let [loops, all, ranked] = await Promise.all([api.get("/api/fallback-loops").catch(() => []),
    api.get("/api/providers").then((d) => d.providers).catch(() => []),
    api.get("/api/models/ranking").catch(() => null)]);
  const enabled = all.filter((p) => p.enabled);
  const name = (id) => all.find((p) => p.id === id)?.name || id;
  // AgentOS's intelligence ranking (relay/ranking.py), where it ranked the model
  const rankOf = (e) => (e.model && ranked?.ranks?.[e.provider]?.[e.model]) || null;
  const save = async (next) => {
    try {
      loops = await api.put("/api/fallback-loops", { loops: next });
      state.loops = loops;  // the chip under the message box
      toast("Fallback loops saved");
    } catch (error) {
      toast(error.message, "error");
    }
    render();
  };
  const change = (i, entries) => save(loops.map((loop, j) => (j === i ? { ...loop, entries } : loop)));
  const adder = (i) => {
    const provider = h("select.select", enabled.map((p) => h("option", { value: p.id }, p.name)));
    const model = h("select.select");
    const loadModels = async () => {
      const asked = provider.value;
      clear(model, h("option", { value: "" }, "Loading models…"));  // (not the models of the one before)
      const models = await api.get(`/api/providers/${asked}/models`).then((d) => d.models).catch(() => []);
      if (provider.value !== asked) return;  // another provider was picked meanwhile
      clear(model, h("option", { value: "" }, "Default model"));
      model.append(...models.filter((m) => m.id).map((m) => h("option", { value: m.id }, m.name || m.id)));
    };
    provider.addEventListener("change", loadModels);
    loadModels();
    return h("div.row-inline", provider, model, h("button.btn.small", { onclick: () => {
      if (provider.value) change(i, [...loops[i].entries, { provider: provider.value, model: model.value }]);
    } }, icon("plus", "sm"), "Add"));
  };
  const entryRow = (i, entries, e, k) => h("div.provider-row",
    h("div.info", h("div.name.loop-entry", h("span.truncate", `${k + 1}. ${name(e.provider)}`),
      rankOf(e) ? h("span.loop-rank",
        { title: `AgentOS's ranking of the models here: #1 is the smartest of ${ranked.of}` },
        `Intelligence index: #${rankOf(e)}`) : null),
    h("div.sub", e.model || "Default model")),
    h("div.actions",
      k ? h("button.icon-btn.sm", { title: "Earlier", onclick: () => {
        const next = [...entries];
        [next[k - 1], next[k]] = [next[k], next[k - 1]];
        change(i, next);
      } }, icon("send", "sm")) : null,
      h("button.icon-btn.sm", { title: "Remove", onclick: () => change(i, entries.filter((_, j) => j !== k)) },
        icon("trash", "sm"))));
  const loopCard = (loop, i) => h("div.loop-card",
    h("div.row-between",
      h("div", h("strong", loop.name), h("div.muted.small-text",
        loop.entries.length ? loop.entries.map((e) => e.model || name(e.provider)).join(" → ") + " → (again)"
          : "Empty: add the agents in the order they take over")),
      h("div.actions",
        h("button.icon-btn.sm", { title: "Rename", onclick: async () => {
          const named = await promptDialog("Rename loop", { value: loop.name });
          if (named) save(loops.map((l, j) => (j === i ? { ...l, name: named } : l)));
        } }, icon("edit", "sm")),
        h("button.icon-btn.sm", { title: "Delete loop", onclick: async () => {
          if (await confirmDialog("Delete loop?", `Chats following “${loop.name}” follow the loop their model is in.`,
            { confirm: "Delete", danger: true })) save(loops.filter((_, j) => j !== i));
        } }, icon("trash", "sm")))),
    loop.entries.map((e, k) => entryRow(i, loop.entries, e, k)),
    adder(i));
  const render = () => clear(box,
    h("p.muted.small-text", "Changes to fallback loops are saved immediately."),
    loops.length ? loops.map(loopCard)
      : h("p.muted", "No loops: a model that stops waits for its own limit to reset."),
    h("button.btn.small", { onclick: async () => {
      const named = await promptDialog("New fallback loop", { placeholder: "e.g. Opus first" });
      if (named) save([...loops, { name: named, entries: [] }]);
    } }, icon("plus", "sm"), "New loop"));
  render();
  return box;
}

function chooseKind(pane, hooks, m, kinds) {
  clear(pane,
    pageHeading("Add a provider", ["Choose what kind of provider to add. You'll get a template to fill in. None of "
      + "these? ", h("a", { href: "#", onclick: (e) => { e.preventDefault(); providerGuide(pane, hooks, m, kinds); } },
      "Adding one Relay doesn't know"), "."],
      h("button.btn.small.ghost", { onclick: () => providers(pane, hooks, m) }, icon("arrowLeft", "sm"), "Back")),
    // (the guide's example isn't offered: a template of it still works)
    h("div.kind-grid", kinds.filter((kind) => kind.listed !== false).map((kind) => h("button.kind-card", {
      onclick: () => editor(pane, hooks, m, kind) },
      h("div.name", icon(kind.tools ? "code" : "chat", "sm"), kind.label,
        // (one added to Relay: its folder)
        kind.builtin === false ? h("span.badge", { title: "Added in this folder of Relay's" }, kind.folder) : null),
      h("p", kind.description))),
    // One that none of these is: how to write its compatibility layer (docs/ADDING-A-PROVIDER.md)
    h("button.kind-card.guide-card", { onclick: () => providerGuide(pane, hooks, m, kinds) },
      h("div.name", icon("book", "sm"), "A provider Relay doesn't know"),
      h("p", "None of these fits? Add it with an agent: you install it in a box of its own and log in, an agent of "
        + "yours plans and builds its support for Relay, and you try it first. Or the guide, to write it yourself."))));
}

// The guide to adding a provider (GET /api/providers/guide), in the Add provider page's place
async function providerGuide(pane, hooks, m, kinds) {
  const body = h("div.md.provider-guide", h("div.spinner"));
  clear(pane,
    pageHeading("Adding a provider Relay doesn't know", "docs/ADDING-A-PROVIDER.md in Relay's folder",
      h("button.btn.small.ghost", { onclick: () => chooseKind(pane, hooks, m, kinds) },
        icon("arrowLeft", "sm"), "Back")),
    h("div.settings-actions",  // (its own page: relay/creations.py)
      h("button.btn.primary.small", { onclick: () => { m.close(); hooks.onOpenPage?.("/providers/new"); } },
        icon("plus", "sm"), "Add it with an agent")),
    body);
  try {
    // (its title is the page's heading already; its links into Relay's folder lead nowhere here: their words)
    renderMarkdown(body, (await api.get("/api/providers/guide")).markdown.replace(/^# .*\n+/, "")
      .replace(/\[([^\]]+)\]\((?!https?:|#)[^)]*\)/g, "$1"));
  } catch (error) {
    clear(body, h("p.error-text", error.message));
  }
}

// A provider's template: its Save, or (`given`, its JSON as you left it) a new one's Next step, nextStep()
function editor(pane, hooks, m, kind, existing = null, given = null) {
  const template = existing  // (its home, where its program was installed, stays)
    ? { id: existing.id, name: existing.name, kind: existing.kind, enabled: existing.enabled, options: existing.options,
      ...(existing.home ? { home: existing.home } : {}) }
    : { ...kind.example, enabled: true };
  const text = h("textarea.textarea.code", { spellcheck: false, value: given ?? JSON.stringify(template, null, 2) });
  text.addEventListener("keydown", (e) => {
    if (e.key === "Tab") {
      e.preventDefault();
      text.setRangeText("  ", text.selectionStart, text.selectionEnd, "end");
    }
  });
  const error = h("div.error-text");
  const save = h("button.btn.primary", { onclick: async () => {
    error.textContent = "";
    let parsed;
    try {
      parsed = JSON.parse(text.value);
    } catch (err) {
      error.textContent = `Invalid JSON: ${err.message}`;
      return;
    }
    save.disabled = true;
    try {
      if (existing) {
        await api.put(`/api/providers/${existing.id}`, parsed);
        toast("Provider saved");
        hooks.onProvidersChanged?.();
        providers(pane, hooks, m);
      } else {
        const has = await api.post(`/api/provider-kinds/${encodeURIComponent(kind.kind)}/draft`, { template: parsed });
        nextStep(pane, hooks, m, kind, text.value, parsed, has);
      }
    } catch (err) {
      error.textContent = err.message;
      save.disabled = false;
    }
  } }, existing ? "Save" : "Next step");
  clear(pane,
    pageHeading(existing ? `Edit ${existing.name}` : `New ${kind.label} provider`, kind.description,
      h("button.btn.small.ghost", { onclick: () => providers(pane, hooks, m) }, icon("arrowLeft", "sm"), "Back")),
    h("label.field", h("span", "Template (JSON)"), text,
      h("span.hint", existing ? "Secrets show as ******** and are kept unless you replace them."
        : "Change the id and name, then fill in the options.")),
    error,
    settingsActions(save),
    settingsSection("Options", null, h("dl.fields-doc", kind.fields.map((f) => [
      h("dt", f.name, f.required ? " (required)" : ""),
      h("dd", `${f.type} — ${f.help}`),
    ]))));
  text.focus();
}

// Add provider's next step, with what the first page's template has (POST .../draft): a kind with a box gets that box,
// made from the template, and a shell in it to log in; then a test message. Only the last button adds it.
function nextStep(pane, hooks, m, kind, text, template, { shell: inBox, sign_in: signIn, shell_hint: hint }) {
  const route = `/api/provider-kinds/${encodeURIComponent(kind.kind)}`;
  const said = h("span.muted", { role: "status" });
  const error = h("div.error-text");
  let works = false;
  const check = async () => {
    again.disabled = true;
    works = false;
    clear(said, "Checking…");
    try {
      const result = await api.post(`${route}/test`, { template });
      works = result.ok;
      if (result.login_lost && inBox) clear(said, "Not logged in yet: log in in the shell, then check again.");
      else clear(said, result.ok ? "It works: " : "Not working yet: ", result.message);
    } catch (err) {
      clear(said, err.message);
    } finally {
      again.disabled = false;
    }
  };
  const again = h("button.btn.small", { onclick: check }, "Check again");
  const add = h("button.btn.primary", { onclick: async () => {
    error.textContent = "";
    if (!works && !await confirmDialog("Add it without seeing it work?",
      "Relay hasn't seen it work yet. " + (inBox
        ? `You can log in later too: the Shell icon of ${template.name || kind.label} in Settings → Providers.`
        : "You can test it later in Settings → Providers."), { confirm: "Add anyway" })) return;
    add.disabled = true;
    try {
      await api.post("/api/providers", template);
      toast("Provider added");
      hooks.onProvidersChanged?.();
      providers(pane, hooks, m);
    } catch (err) {
      error.textContent = err.message;
      add.disabled = false;
    }
  } }, "Add provider");
  const checked = "Relay will check it using a test message, after which the provider can be added.";
  let shell = null;
  let section;
  if (inBox) {
    const status = h("span.shell-status", { role: "status" }, "Connecting to its box…");
    const host = h("div.shell-terminal", { "aria-label": `Shell running ${kind.label}` });
    clear(said, "Connecting to its box…");
    // (the test once its shell is there: a test's box goes when nothing's open in it)
    shell = mountTerminal(host, `${route}/terminal?template=${encodeURIComponent(text)}`, status, {
      connected: inItsBox({ sign_in: signIn, shell_hint: hint }), onReady: () => { check(); } });
    // (this page has no way out of its own: another tab, Settings closed, Add)
    const leaving = setInterval(() => {
      if (host.isConnected) return;
      clearInterval(leaving);
      shell.end();
    }, 1000);
    section = settingsSection("Log in", [`A shell running ${kind.label} below. If you need to log in, please do so as usual`,
      ...(signIn ? [" (", h("code", signIn), ")"] : []), ". ", hint ? `${hint} ` : "", checked],
    h("div.newprovider-shell", host, h("div.service-foot", status, shell.keys)),
    h("div.settings-actions.signin-check", again, said));
  } else {
    section = settingsSection("Try it", checked, h("div.settings-actions.signin-check", again, said));
    check();
  }
  clear(pane,
    pageHeading(`New ${kind.label} provider`, kind.description,
      h("button.btn.small.ghost", { onclick: () => { shell?.end(); editor(pane, hooks, m, kind, null, text); } },
        icon("arrowLeft", "sm"), "Back")),
    section, error, settingsActions(add));
}

// ---------------------------------------------------------------- workspaces

async function workspaces(pane) {
  let settings;
  try {
    settings = await api.get("/api/settings");
  } catch (error) {
    clear(pane, h("p.error-text", error.message));
    return;
  }
  const list = h("div.list-editor");
  const addRow = (value = "") => {
    const input = h("input.input.mono", { value, placeholder: "/home/pi/projects" });
    const row = h("div.row", input,
      h("button.icon-btn", { title: "Remove", onclick: () => row.remove() }, icon("trash", "sm")));
    list.append(row);
    return input;
  };
  settings.workspace_roots.forEach((root) => addRow(root));
  const save = h("button.btn.primary", { onclick: async () => {
    const roots = [...list.querySelectorAll("input")].map((i) => i.value.trim()).filter(Boolean);
    save.disabled = true;
    save.textContent = "Saving…";
    try {
      await api.put("/api/settings", { workspace_roots: roots });
      toast("Workspaces saved");
    } catch (error) {
      toast(error.message, "error");
    } finally {
      save.disabled = false;
      save.textContent = "Save";
    }
  } }, "Save");
  clear(pane,
    pageHeading("Workspaces", "Project folders, chat storage, shared network drives and a VPN."),
    settingsSection("Workspace roots",
      "Code tasks can only run in folders inside these directories. Click Save to apply changes to these roots.",
      list, settingsActions(h("button.btn.small", { onclick: () => addRow().focus() },
        icon("plus", "sm"), "Add root"), save)),
    settingsSection("Chats", `Chat conversations get a scratch folder each, in ${settings.chat_directory}.`),
    settingsSection("Network drives", "Folders you opened on another computer: code tasks' (/mount) and the scripts' "
      + "(the Scripts page).", await drivesSection()),
    settingsSection("VPN", "WorkBoxes that go through a VPN: gluetun, which Relay starts and keeps. A profile "
      + "is a VPN account; a tunnel takes some folders' WorkBoxes (or all the others) through its exits, a "
      + "profile and a place each, with rotation and a fallback loop if you like. Without a connection they "
      + "have no internet (gluetun's firewall); the LAN and Relay stay reachable.", await vpnSection()));
}

// ---------------------------------------------------------------- modes

async function modes(pane, hooks) {
  let list;
  try {
    list = await api.get("/api/modes");
  } catch (error) {
    clear(pane, h("p.error-text", error.message));
    return;
  }
  const changed = () => { hooks.onModesChanged?.(); modes(pane, hooks); };
  const rows = list.map((mode) => h("div.provider-row",
    h("button.icon-btn.mode-icon",
      { title: "Change the icon", onclick: (e) => pickModeIcon(e.currentTarget, mode, changed) },
      icon(mode.icon || "tool")),
    h("div.info",
      h("div.name", mode.name, " ", h("span.badge", `/${mode.id}`), " ",
        mode.access === "read" ? h("span.badge.warning", "read-only") : null),
      h("div.sub", mode.description || (mode.instructions ? mode.instructions.split("\n")[0].slice(0, 140) : ""))),
    mode.builtin ? h("span.faint.small-text", "built in") : h("div.actions",
      h("button.icon-btn", { title: "Edit", onclick: () => modeEditor(pane, hooks, mode) }, icon("edit", "sm")),
      h("button.icon-btn", { title: "Delete", onclick: async () => {
        const ok = await confirmDialog("Delete mode?", `/${mode.id} is removed; chats in it go back to Build.`,
          { confirm: "Delete", danger: true });
        if (!ok) return;
        try {
          await api.del(`/api/modes/${mode.id}`);
          changed();
        } catch (error) {
          toast(error.message, "error");
        }
      } }, icon("trash", "sm")))));
  clear(pane,
    pageHeading("Modes", "How an agent works in a chat. Pick one under the message box, or type /mode and its name. "
      + "Read-only modes ask the agent to look around without changing anything (its commands run in a "
      + "container either way). Click an icon to change it.",
      h("button.btn.primary.small", { onclick: () => modeEditor(pane, hooks) }, icon("plus", "sm"), "Add mode")),
    h("div.settings-list", rows));
}

// ---------------------------------------------------------------- limits

// Limits and texts that used to be fixed in the code (relay/tunables.py), and the server's settings.
async function limits(pane, hooks = {}) {
  let data;
  try {
    data = await api.get("/api/limits");
  } catch (error) {
    clear(pane, h("p.error-text", error.message));
    return;
  }
  const inputs = new Map();
  const groups = new Map();
  for (const t of data.tunables) {
    if (!groups.has(t.group)) groups.set(t.group, []);
    let input;
    if (t.kind === "bool") {
      input = h("input", { type: "checkbox", checked: t.value });
    } else if (t.kind === "int") {
      input = h("input.input.narrow", { type: "number", min: t.minimum, max: t.maximum, value: t.value });
    } else {
      input = h("textarea.textarea", { rows: 2 });
      input.value = t.value;
    }
    input.id = `limit-${t.key}`;
    if (t.help) input.setAttribute("aria-describedby", `${input.id}-help`);
    inputs.set(t.key, { input, t });
    const reset =
      h("button.btn.small.ghost",
        { type: "button", title: `Back to ${t.kind === "bool" ? (t.default ? "on" : "off") : t.default}`,
          onclick: () => {
            if (t.kind === "bool") input.checked = t.default;
            else input.value = t.default;
          } }, "Default");
    const unit = t.unit ? h("span.muted", t.unit) : null;
    groups.get(t.group).push(t.kind === "bool"
      ? h("label.field.inline", input, h("span", t.label, h("span.hint.block", { id: `${input.id}-help` }, t.help)))
      : h(`div.field${t.kind === "text" ? ".settings-wide-field" : ""}`, h("label", { for: input.id }, t.label),
        h("div.row-inline", input, unit, reset),
        t.help ? h("span.hint", { id: `${input.id}-help` },
          `${t.help}${t.kind === "int" ? ` (${t.minimum}–${t.maximum}; default ${t.default})` : ""}`) : null));
  }
  const publicUrl = h("input.input", { value: data.server.public_url, placeholder: "e.g. http://relay.local:8420" });
  const sessionDays =
    h("input.input.narrow",
      { id: "limit-session-days", type: "number", min: 1, max: 365, value: data.server.session_days });
  const save = h("button.btn.primary", { onclick: async () => {
    const values = {};
    for (const [key, { input, t }] of inputs) {
      values[key] = t.kind === "bool" ? input.checked : t.kind === "int" ? Number(input.value) : input.value;
    }
    save.disabled = true;
    save.textContent = "Saving…";
    try {
      await api.put("/api/limits",
        { values, server: { public_url: publicUrl.value, session_days: Number(sessionDays.value) } });
      toast("Limits saved");
    } catch (error) {
      toast(error.message, "error");
    } finally {
      save.disabled = false;
      save.textContent = "Save";
    }
  } }, "Save");
  clear(pane,
    pageHeading("Limits", "How much, how long and what Relay says by itself. Changes apply when you click Save."),
    // (AgentOS out of this Relay: its settings and its services' aren't shown, and are kept)
    [...groups].filter(([group]) => hooks.agentos?.() !== false || !["AgentOS", "Services"].includes(group))
      .map(([group, fields]) => settingsSection(group, null, h("div.settings-field-grid", fields))),
    settingsSection("Server", null,
      h("label.field", h("span", "Relay's address"), publicUrl,
        h("span.hint",
          "For links in notifications and for the computers' connector; empty: the address your browser uses.")),
      h("div.field", h("label", { for: sessionDays.id }, "Stay signed in"),
        h("div.row-inline", sessionDays, h("span.muted", "days"))),
      h("p.muted.small-text",
        `Listening on ${data.server.host}:${data.server.port}, sign-in ${data.server.auth === "none" ? "off" : "on"}`
        + `${data.server.secure_cookies ? ", secure cookies" : ""}. These change in `
        + "~/.local/share/relay/settings.json, followed by a restart."),
      h("div.row-inline",
        h("button.btn.small", { onclick: async () => {
          if (await confirmDialog("Restart Relay?",
            "Relay starts anew with the code it has now (e.g. after a git pull); "
            + "it refuses while agents are working. The page reloads when it's back.",
            { confirm: "Restart" })) restartRelay();
        } }, "Restart Relay"),
        h("span.muted", "after an upgrade (git pull) or a change to settings.json"))),
    settingsActions(save));
}

// ---------------------------------------------------------------- computers

// The user's own computers, where agents can work while Relay's connector runs (relay/computers.py).
async function computers(pane, hooks) {
  const list = h("div.settings-list");
  const name = h("input.input", { placeholder: "e.g. laptop" });
  const howTo = h("div");
  let timer = null;
  const download = (c) => {  // (set to reach Relay as this page does: behind a proxy, Relay can't tell)
    const here = new URL(relayUrl("/"), location.href).href;
    location.href = relayUrl(`/api/computers/${c.id}/script?url=${encodeURIComponent(here)}`);
  };
  const since = (t) => (t ? new Date(t * 1000).toLocaleString() : "never");
  const render = async () => {
    if (!list.isConnected) {  // another tab, or the settings closed
      clearInterval(timer);
      return;
    }
    let found;
    try {
      found = await api.get("/api/computers");
    } catch (error) {
      clear(list, h("p.error-text", error.message));
      return;
    }
    clear(list, found.length ? found.map((c) => {
      const ask = h("input", { type: "checkbox", checked: c.ask, onchange: async () => {
        await api.patch(`/api/computers/${c.id}`, { ask: ask.checked }).catch((error) => toast(error.message, "error"));
      } });
      const system = c.info ? [c.info.os, c.info.release].filter(Boolean).join(" ")
        + ` · ${c.info.user}@${c.info.hostname}` : null;
      return h("div.provider-row.computer-row",
        statusDot(c.online ? "ok" : "fail", c.online ? "Online" : "Offline"),
        h("div.info", h("div.name", c.name, " ", h("span.badge", c.online ? "online" : "offline")),
          h("div.sub",
            [system, c.online ? null : `last seen ${since(c.last_seen)}`].filter(Boolean)
            .join(" · ") || "Not connected yet"),
          h("label.field.inline.small-text", ask,
            h("span", "Ask me before each command or file change (off: the agent just goes ahead)"))),
        h("div.actions",
          h("button.btn.small", { title: "Download its connector again", onclick: () => download(c) },
            icon("file", "sm"), "Connector"),
          h("button.icon-btn.sm", { title: "Remove", onclick: async () => {
            const ok =
              await confirmDialog("Remove computer?",
                `${c.name}'s connector stops working; agents can't reach it any more.`,
                { confirm: "Remove", danger: true });
            if (!ok) return;
            await api.del(`/api/computers/${c.id}`).catch((error) => toast(error.message, "error"));
            hooks.onComputersChanged?.();
            render();
          } }, icon("trash", "sm"))));
    }) : h("p.muted", "None yet."));
  };
  const add = h("button.btn.primary.small", { onclick: async () => {
    try {
      const c = await api.post("/api/computers", { name: name.value });
      name.value = "";
      hooks.onComputersChanged?.();
      clear(howTo, h("div.drive-steps", h("div.drive-os",
        h("b", `Connect ${c.name}`),
        h("ol",
          h("li", "On that computer, download its connector: ",
            h("button.btn.small", { onclick: () => download(c) }, icon("file", "sm"), "relay-computer.py")),
          h("li", "Run it, and keep it running while Relay may work there: ",
            h("code", "python3 relay-computer.py"), " (Windows: ", h("code", "py relay-computer.py"),
            "). It needs Python 3.8 or newer, nothing else."),
          h("li", "It shows every command it runs. Ctrl+C (or closing the window) stops it; add ",
            h("code", "--ask"), " to confirm each command on the computer itself too.")))));
      render();
    } catch (error) {
      toast(error.message, "error");
    }
  } }, icon("plus", "sm"), "Add computer");
  clear(pane,
    pageHeading("Computers", "Let agents work on your own computers: change a setting, install something, "
      + "fetch a file. A computer runs a small Python script that connects to Relay; while it runs, agents can "
      + "run commands and read and write files there, as you. Tell an agent which computer, e.g. “on my laptop, …”."),
    list,
    settingsSection("Add a computer", null,
      h("label.field", h("span", "Computer name"), name), settingsActions(add), howTo),
    h("p.muted.small-text", "The connector calls Relay, so it works "
      + "from behind a router. Its file holds its key: whoever runs "
      + "it acts as that computer. Read-only modes (like Plan) ask the agent not to change anything there."));
  await render();
  if (list.isConnected) timer = setInterval(render, 4000);
}

// ---------------------------------------------------------------- subagents

// Which subagents agents may start without asking (relay/policy.py).
async function subagents(pane) {
  let rules;
  let providerList;
  try {
    [rules, providerList] = await Promise.all([api.get("/api/subagents"), api.get("/api/providers")]);
  } catch (error) {
    clear(pane, h("p.error-text", error.message));
    return;
  }
  const enabled = providerList.providers.filter((p) => p.enabled);
  const name = (id) => enabled.find((p) => p.id === id)?.name || id;
  const save = async () => {
    try {
      rules = await api.put("/api/subagents", rules);
      toast("Subagent settings saved");
    } catch (error) {
      toast(error.message, "error");
    }
    render();
  };
  const check = (key, label, hint) => {
    const box =
      h("input", { type: "checkbox", checked: rules[key], onchange: () => { rules[key] = box.checked; save(); } });
    return h("label.field.inline", box, h("span", label, h("span.hint.block", hint)));
  };
  const provider = h("select.select", enabled.map((p) => h("option", { value: p.id }, p.name)));
  const model = h("select.select");
  const loadModels = async () => {
    const asked = provider.value;
    clear(model, h("option", { value: "*" }, "Loading models…"));  // (not the models of the one before)
    const models = await api.get(`/api/providers/${asked}/models`).then((d) => d.models).catch(() => []);
    if (provider.value !== asked) return;  // another provider was picked meanwhile
    clear(model, h("option", { value: "*" }, "Any model"));
    if (!models.some((m) => m.id === "")) model.append(h("option", { value: "" }, "Default model"));
    model.append(...models.map((m) => h("option", { value: m.id }, m.name || m.id || "Default model")));
  };
  provider.addEventListener("change", loadModels);
  const list = h("div.settings-list");
  const others = h("select.select", { onchange: () => { rules.others = others.value; save(); } },
    h("option", { value: "ask" }, "Ask me first (denied when there's no answer in time; see Limits)"),
    h("option", { value: "refuse" }, "Refuse them"));
  function render() {
    others.value = rules.others;
    clear(list, rules.allowed.length ? rules.allowed.map((entry, i) => h("div.provider-row",
      h("div.info", h("div.name", name(entry.provider)),
        h("div.sub", entry.model === "*" ? "Any model" : entry.model || "Default model")),
      h("div.actions", h("button.icon-btn.sm", { title: "Remove", onclick: () => {
        rules.allowed.splice(i, 1);
        save();
      } }, icon("trash", "sm"))))) : h("p.muted", "None yet."));
  }
  render();
  loadModels();
  clear(pane,
    pageHeading("Subagents", "Agents can hand work to other providers and models, "
      + "and some of those cost money. These run without asking; for any other, "
      + "the agent asks you first, in the chat (and by notification, if set up). Changes here are saved immediately."),
    settingsSection("Allowed without asking", null,
      check("same", "The calling agent's own provider and model",
        "A subagent on the same model costs what the agent itself does."),
      check("free", "Free and local models",
        "Local servers (e.g. Ollama), OpenRouter's :free models, and models whose last run was free."),
      check("local", "Run subagents on local models",
        "Turn it off while your local server (e.g. Ollama) isn't running: "
        + "agents then pick a model elsewhere.")),
    settingsSection("Also allowed", null,
      list,
      h("div.row-inline", provider, model, h("button.btn.small", { onclick: () => {
        if (!provider.value) return;
        rules.allowed.push({ provider: provider.value, model: model.value });
        save();
      } }, icon("plus", "sm"), "Allow"))),
    settingsSection("Any other subagent", null,
      others,
      h("p.muted.small-text", "When you're asked, you can allow it "
        + "once, always, use an allowed one instead, or deny it. A denied agent hears which subagents it may use.")));
}

// A mode's icon, from a grid of them; also for the built-in modes.
function pickModeIcon(anchor, mode, changed) {
  const set = async (name) => {
    try {
      await api.put(`/api/modes/${mode.id}/icon`, { icon: name });
      changed();
    } catch (error) {
      toast(error.message, "error");
    }
  };
  showMenu(anchor, [
    ...MODE_ICONS.map((name) => ({ label: "", icon: name, title: name, onclick: () => set(name) })),
    { label: "Its own", title: "The icon it came with", onclick: () => set(null) },
  ], { className: "icon-grid" });
}

function modeEditor(pane, hooks, existing = null) {
  const name = h("input.input", { value: existing?.name || "", placeholder: "e.g. Critic" });
  const description = h("input.input", { value: existing?.description || "",
    placeholder: "One line, shown when picking a mode" });
  const access = h("select.select",
    h("option", { value: "edit", selected: existing?.access !== "read" }, "Edit: may change files and run commands"),
    h("option", { value: "read", selected: existing?.access === "read" }, "Read-only: asked to only look"));
  const instructions = h("textarea.textarea", { rows: 8,
    placeholder: "e.g. Look for the weak spots in the idea or the code: what could go wrong, what's missing. "
      + "Don't soften it." });
  instructions.value = existing?.instructions || "";
  const save = h("button.btn.primary", { onclick: async () => {
    const data = { name: name.value, description: description.value, access: access.value,
      instructions: instructions.value };
    try {
      if (existing) await api.put(`/api/modes/${existing.id}`, data);
      else await api.post("/api/modes", data);
      toast(existing ? "Mode saved" : "Mode added");
      hooks.onModesChanged?.();
      modes(pane, hooks);
    } catch (error) {
      toast(error.message, "error");
    }
  } }, existing ? "Save" : "Add mode");
  clear(pane,
    pageHeading(existing ? `Edit /${existing.id}` : "New mode",
      "Agents get the instructions with every message in this mode; chat APIs as a system prompt."),
    h("label.field", h("span", "Name"), name,
      existing ? null : h("span.hint", "You type /mode and this name (lowercase, dashes for spaces) to switch.")),
    h("label.field", h("span", "Description"), description),
    h("label.field", h("span", "Access"), access),
    h("label.field", h("span", "Instructions"), instructions),
    settingsActions(h("button.btn", { onclick: () => modes(pane, hooks) }, "Cancel"), save));
  name.focus();
}

// ---------------------------------------------------------------- skills

async function skills(pane, hooks) {
  let data;
  try {
    data = await api.get("/api/skills");
  } catch (error) {
    clear(pane, h("p.error-text", error.message));
    return;
  }
  const rows = data.skills.map((skill) => h("div.provider-row",
    h("div.info",
      h("div.name", skill.name, " ", h("span.badge", skill.builtin ? "built in" : "yours"), " ",
        skill.access === "read" ? h("span.badge.warning", "read-only") : null),
      h("div.sub", skill.description)),
    h("div.actions",
      h("button.icon-btn",
        { title: skill.builtin ? "View" : "Edit", onclick: () => skillEditor(pane, hooks, skill.name) },
        icon(skill.builtin ? "file" : "edit", "sm")),
      skill.builtin ? null : h("button.icon-btn", { title: "Delete", onclick: async () => {
        const ok = await confirmDialog("Delete skill?", `${skill.name} and every file in its folder are removed.`,
          { confirm: "Delete", danger: true });
        if (!ok) return;
        try {
          await api.del(`/api/skills/${skill.name}`);
          hooks.onSkillsChanged?.();
          skills(pane, hooks);
        } catch (error) {
          toast(error.message, "error");
        }
      } }, icon("trash", "sm")))));
  clear(pane,
    pageHeading("Skills", "Instructions for particular jobs that every agent can use, reading one only "
      + "when it needs it. Claude Code loads them as its own skills; other agents and chat models get them listed.",
      h("button.btn.primary.small", { onclick: () => newSkill(pane, hooks) }, icon("plus", "sm"), "New skill")),
    settingsSection("Your own skills", null,
      h("div.skill-folder", h("span.muted", "Your own skills folder"), h("code", data.folder),
        h("button.icon-btn.sm", { title: "Copy the path", onclick: () => copyText(data.folder) }, icon("copy", "sm"))),
      h("p.muted.small-text", "Add a folder with a SKILL.md there (or ask an agent to: it knows the format), "
        + "or use New skill. A skill of your own with a built-in one's name replaces it. "
        + "You can also call a skill yourself: type /code-review in a chat, or /<name> and what you want.")),
    h("div.settings-list", rows));
}

function newSkill(pane, hooks) {
  const name = h("input.input", { placeholder: "e.g. release-notes" });
  const description = h("input.input", { placeholder: "What it does, and when to use it" });
  const readOnly = h("input", { type: "checkbox" });
  const instructions =
    h("textarea.textarea", { rows: 10, placeholder: "The steps, as you'd explain them to a capable colleague." });
  const save = h("button.btn.primary", { onclick: async () => {
    try {
      const skill = await api.post("/api/skills", { name: name.value, description: description.value,
        instructions: instructions.value, access: readOnly.checked ? "read" : "edit" });
      toast("Skill added");
      hooks.onSkillsChanged?.();
      skillEditor(pane, hooks, skill.name);
    } catch (error) {
      toast(error.message, "error");
    }
  } }, "Add skill");
  clear(pane,
    pageHeading("New skill", "Agents see the name and description, and read the rest when they use the skill."),
    h("label.field", h("span", "Name"), name, h("span.hint", "Lowercase letters, digits and dashes.")),
    h("label.field", h("span", "Description"), description),
    h("label.field.inline", readOnly, h("span", "Read-only: following it never changes files")),
    h("label.field", h("span", "Instructions"), instructions),
    settingsActions(h("button.btn", { onclick: () => skills(pane, hooks) }, "Cancel"), save));
  name.focus();
}

async function skillEditor(pane, hooks, name, file = "SKILL.md") {
  let skill;
  let content;
  try {
    [skill, { content }] = await Promise.all([api.get(`/api/skills/${name}`),
      api.get(`/api/skills/${name}/file?path=${encodeURIComponent(file)}`)]);
  } catch (error) {
    toast(error.message, "error");
    return;
  }
  const area = h("textarea.textarea.code", { rows: 18, readOnly: skill.builtin });
  area.value = content;
  const files = h("div.skill-files", skill.files.map((f) => h(`button.btn.small${f === file ? ".primary" : ""}`,
    { onclick: () => skillEditor(pane, hooks, name, f) }, f)));
  const save = skill.builtin ? null : h("button.btn.primary", { onclick: async () => {
    try {
      await api.put(`/api/skills/${name}/file?path=${encodeURIComponent(file)}`, { content: area.value });
      hooks.onSkillsChanged?.();
      toast("Saved");
    } catch (error) {
      toast(error.message, "error");
    }
  } }, "Save");
  clear(pane,
    pageHeading([skill.name, " ", h("span.badge", skill.builtin ? "built in" : "yours")],
      skill.builtin ? "Built into Relay, so it can't be changed here. To change it, make a skill of your own "
      + "with the same name." : `In ${skill.path.replace(/\/SKILL\.md$/, "")}`),
    files, area,
    settingsActions(h("button.btn", { onclick: () => skills(pane, hooks) }, "Back"), save));
}

// ---------------------------------------------------------------- usage

const money = (value) => `$${Number(value || 0).toFixed(value && value < 1 ? 4 : 2)}`;
const count = (value) => Number(value || 0).toLocaleString();

async function usage(pane, hooks, m, days = 30) {
  let data;
  try {
    data = await api.get(`/api/usage?days=${days}`);
  } catch (error) {
    clear(pane, h("p.error-text", error.message));
    return;
  }
  const t = data.totals;
  const period = h("div.mode-toggle", [7, 30, 90].map((d) =>
    h(`button${d === days ? ".on" : ""}`, { onclick: () => usage(pane, hooks, m, d) }, `${d} days`)));
  const card = (kind, title, value, lines) => h(`div.usage-card.${kind}`,
    h("div.usage-title", title), h("div.usage-value", value), lines.map((l) => h("div.usage-line", l)));
  const cards = h("div.usage-cards",
    card("api", "API — pay per use", money(t.api.cost),
      [`${count(t.api.runs)} runs · ${count(t.api.input)} in · ${count(t.api.output)} out`,
        "Variable: what you're charged."]),
    card("plan", "Subscriptions — in plan", `${count(t.subscription.runs)} runs`,
      [`≈ ${money(t.subscription.cost)} at API prices`, "Fixed: no extra cost; counts toward the plan's limits."]),
    card("free", "Free and local", `${count(t.free.runs)} runs`,
      [`${count(t.free.input)} in · ${count(t.free.output)} out`]));
  const most = Math.max(...data.per_day.map((d) => d.runs), 1);
  const bar = (d) => {
    const fill = h("span");
    fill.style.height = `${Math.max(4, (d.runs / most) * 100)}%`;  // CSSOM: allowed by the CSP
    return h("div.usage-bar", { title: `${d.day}: ${d.runs} runs${d.api ? `, ${money(d.api)} API` : ""}` },
      fill, h("small", d.day.slice(5)));
  };
  const bars = data.per_day.length ? h("div.usage-bars", data.per_day.map(bar))
    : h("p.muted", "No runs in this period.");
  const label = { api: "API", subscription: "in plan", free: "free", unknown: "unknown" };
  const table = data.groups.length ? h("table.usage-table",
    h("thead", h("tr", ["Provider", "Model", "Billing", "Runs", "Tokens in / out", "Cost"].map((c) => h("th", c)))),
    h("tbody", data.groups.map((g) => h("tr",
      h("td", hooks.providerName?.(g.provider_id) || g.provider_id || "—"),
      h("td", g.model || "default"),
      h("td", h(`span.badge${g.billing === "api" ? ".warning" : ""}`, label[g.billing])),
      h("td.num", count(g.runs)),
      h("td.num", `${count(g.input)} / ${count(g.output)}`),
      h("td.num",
        g.billing === "api" ? money(g.cost) : g.billing === "subscription" && g.cost
        ? `≈ ${money(g.cost)}` : "—"))))) : null;
  clear(pane,
    pageHeading("Usage", "What your runs cost, subagents included. Relay only knows what providers report: "
      + "costs for chat APIs appear when the server sends them (OpenRouter does).", period),
    limitsSection(), cards, settingsSection("Runs per day", null, bars),
    table ? settingsSection("By provider and model", null, h("div.usage-scroll", table)) : null,
    settingsSection("Fallback loops", "When a model stops (a usage limit, or no answer at all), "
      + "the next agent of the chat's loop takes over right away, with the conversation so far. "
      + "When none of them can go on, Relay waits for the first reset (or 5 hours when nobody "
      + "says) and goes round the loop again from the start, at most a few times (Settings → "
      + "Limits). A chat follows the first loop its model is in; /loop picks another, or none.", await loopsEditor()));
}

// What's left of each provider's usage limits (Provider.limits; /api/usage/limits): asked with the
// provider's own login, or as its last run told. Loaded apart: the rest of the page doesn't wait.
function limitsSection() {
  const body = h("div.usage-limits", h("p.muted", "Asking the providers…"));
  const refresh = h("button.btn.small", { type: "button", title: "Ask the providers anew", onclick: () => load(true) },
    icon("refresh", "sm"), "Refresh");
  async function load(fresh = false) {
    refresh.disabled = true;
    try {
      const rows = await api.get(`/api/usage/limits${fresh ? "?fresh=1" : ""}`);
      clear(body, rows.length ? rows.map(limitRow) : h("p.muted", "None of your providers tells its limits."));
    } catch (error) {
      clear(body, h("p.error-text", error.message));
    } finally {
      refresh.disabled = false;
    }
  }
  load();
  return h("section.settings-section.limits-section", h("div.limit-tools", h("h4", "Usage limits"), refresh),
    h("p.lead", "What's left of your plans' limits, asked from each provider with its own login: Claude Code, "
      + "ChatGPT through opencode, GitHub Copilot."), body);
}

// ---------------------------------------------------------------- memory

// Relay's memory (relay.memory): what every agent knows in every chat, general and per project; agents add
// to it, the user sees and changes all of it here.
async function memoryPage(pane, hooks = {}) {
  let data;
  try {
    data = await api.get("/api/memory");
  } catch (error) {
    clear(pane, h("p.error-text", error.message));
    return;
  }
  // The bank's filters (Perplexity's: search, a category, a place), kept while the page draws anew
  const filter = { query: "", category: "", place: hooks.memoryPlace ?? null };
  hooks.memoryPlace = null;
  // (AgentOS out of this Relay: the Governor's own memory isn't shown, and is kept)
  const show = (fresh) => {
    data = hooks.agentos?.() === false ? { ...fresh, memories: fresh.memories.filter((m) => m.project !== "governor"),
      projects: fresh.projects.filter((p) => p.key !== "governor") } : fresh;
    draw();
  };
  const categoryName = (id) => data.categories.find((c) => c.id === id)?.label || id;
  const placeName = (key) => (key ? data.projects.find((p) => p.key === key)?.label || key : "General");
  const categorySelect = (value) => {
    const select = h("select.select", { "aria-label": "Category" },
      data.categories.map((c) => h("option", { value: c.id, title: c.about }, c.label)));
    select.value = value || "other";
    return select;
  };
  const scopeSelect = (value) => {
    const select = h("select.select", { "aria-label": "What it's about" },
      h("option", { value: "" }, "General: every chat"),
      data.projects.map((p) => h("option", { value: p.key },
        p.key === "governor" ? "The Governor: its own memory" : p.label)));
    select.value = value || "";
    return select;
  };
  const saved = async (call, done) => {
    try {
      show(await call());
      if (done) toast(done);
    } catch (error) {
      toast(error.message, "error");
    }
  };

  function draw() {
    const on = h("input", { type: "checkbox", checked: data.enabled, "aria-label": "Agents use Relay's memory",
      onchange: () => saved(() => api.put("/api/memory", { enabled: on.checked })) });
    const agentsAdd = h("input", { type: "checkbox", checked: data.agents_add, disabled: !data.enabled,
      "aria-label": "Agents save what they learn",
      onchange: () => saved(() => api.put("/api/memory", { agents_add: agentsAdd.checked })) });
    const text =
      h("textarea.textarea", { rows: 2, maxlength: String(data.longest), "aria-label": "Something to remember",
        placeholder: "Something every agent should know, e.g. I'd like answers in Dutch." });
    const category = categorySelect("other");
    const scope = scopeSelect(filter.place && filter.place !== "general" ? filter.place : "");
    const add = h("button.btn.primary", { onclick: () => saved(() => api.post("/api/memory",
      { text: text.value, category: category.value, project: scope.value || null }), "Remembered") },
    icon("plus", "sm"), "Remember");
    clear(pane,
      pageHeading("Memory", "What agents know of you and your projects, in every chat, whichever provider it is: "
        + "each memory filed in a category. Agents add to it when you tell them something lasting, or ask them to "
        + "remember; the Governor keeps a memory of its own. Change anything here."),
      h("div.provider-row", h("label.switch", on, h("span")),
        h("div.info", h("div.name", "Agents use it"), h("div.sub", data.enabled
          ? "Each agent gets these memories (the general ones, and its project's; the Governor its own)."
          : "Off: agents don't get these memories and can't add to them. What's here is kept."))),
      h("div.provider-row", h("label.switch", agentsAdd, h("span")),
        h("div.info", h("div.name", "Agents save what they learn"), h("div.sub", data.agents_add
          ? "When you tell them something lasting, or ask them to remember, they add it, and change or forget "
            + "their own. Yours are yours to change."
          : "Off: agents only read it; you keep it yourself."))),
      settingsSection("Remember something", null, text, h("div.memory-add", category, scope, add)),
      bank(),
      h("div.memory-foot", h("p.muted.small-text", `${data.memories.length} of at most ${data.most} memories.`),
        data.memories.length ? h("button.btn.small.danger", { onclick: clearAll }, icon("trash", "sm"),
          "Delete all memories") : null));
  }

  // Search, a category's chips, a place: the list below follows as you type
  function bank() {
    const list = h("div.settings-list.memory-list");
    const chips = h("div.memory-chips", { role: "group", "aria-label": "Category" });
    const search =
      h("input.input.memory-search", { type: "search", value: filter.query, "aria-label": "Search memories",
        placeholder: "Search memories", oninput: () => { filter.query = search.value; fill(); } });
    const places = h("select.select.memory-where", { "aria-label": "Where", onchange: () => {
      filter.place = places.value || null;
      fill();
    } }, h("option", { value: "" }, "Everywhere"), h("option", { value: "general" }, "General"),
    data.projects.filter((p) => data.memories.some((m) => m.project === p.key) || p.key === filter.place)
      .map((p) => h("option", { value: p.key }, p.label)));
    places.value = filter.place || "";
    const matches = (m, withCategory = true) => {
      const words = filter.query.trim().toLowerCase().split(/\s+/).filter(Boolean);
      const about = [m.text, categoryName(m.category), placeName(m.project), m.by, m.conversation_title || ""]
        .join(" ").toLowerCase();
      return words.every((w) => about.includes(w))
        && (!filter.place || (filter.place === "general" ? !m.project : m.project === filter.place))
        && (!withCategory || !filter.category || m.category === filter.category);
    };
    function fill() {
      const counts = new Map();
      for (const m of data.memories) {
        if (matches(m, false)) counts.set(m.category, (counts.get(m.category) || 0) + 1);
      }
      const total = [...counts.values()].reduce((a, b) => a + b, 0);
      const chip = (id, label, count) => h(`button.chip.memory-chip${filter.category === id ? ".on" : ""}`, {
        type: "button", "aria-pressed": String(filter.category === id),
        onclick: () => { filter.category = filter.category === id && id ? "" : id; fill(); },
      }, label, h("span.group-count", String(count)));
      clear(chips, chip("", "All", total), data.categories.filter((c) => counts.get(c.id) || filter.category === c.id)
        .map((c) => chip(c.id, c.label, counts.get(c.id) || 0)));
      const shown = data.memories.filter((m) => matches(m)).sort((a, b) => b.updated - a.updated);
      clear(list, shown.length ? shown.map(row) : h("p.muted.memory-empty", data.memories.length
        ? "No memories match." : "Nothing remembered yet. Tell an agent something lasting (\"remember that I…\"), "
          + "or add it above."));
    }
    fill();
    return settingsSection("Your memories", null, h("div.memory-tools", search, places), chips, list);
  }

  function row(m) {
    const who = m.by === "you" ? "by you" : `by ${m.by}`;
    const item = h("div.provider-row.memory-row",
      h("div.info",
        h("div.memory-head", h(`span.badge.memory-category.${m.category}`, categoryName(m.category)),
          m.project ? h("span.memory-place", { title: placeName(m.project) },
            m.project === "governor" ? icon("compass", "sm") : icon("folder", "sm"), placeName(m.project)) : null),
        h("div.name.memory-text", m.text),
        h("div.sub", `Added ${who}`, m.conversation_title ? ` in “${m.conversation_title}”` : "",
          " · ", h("time", { title: new Date(m.updated * 1000).toLocaleString() }, timeAgo(m.updated)))),
      h("div.actions",
        h("button.icon-btn", { title: "Change", "aria-label": `Change: ${m.text}`, onclick: () => change(item, m) },
          icon("edit", "sm")),
        h("button.icon-btn", { title: "Forget", "aria-label": `Forget: ${m.text}`, onclick: () => forget(m) },
          icon("trash", "sm"))));
    return item;
  }

  function change(item, m) {
    const text = h("textarea.textarea", { rows: 2, maxlength: String(data.longest), "aria-label": "The memory" });
    text.value = m.text;
    const category = categorySelect(m.category);
    const scope = scopeSelect(m.project);
    const save = h("button.btn.primary.small", { onclick: () => saved(() => api.patch(`/api/memory/${m.id}`,
      { text: text.value, category: category.value, project: scope.value || null })) }, "Save");
    item.replaceWith(h("div.memory-edit", text,
      h("div.memory-add", category, scope, h("button.btn.small", { onclick: draw }, "Cancel"), save)));
    text.focus();
  }

  async function forget(m) {
    try {
      show(await api.del(`/api/memory/${m.id}`));
      // Undo puts it back as it was: its category, place, who added it, in which chat, since when
      toast("Forgotten", "info", { label: "Undo", onclick: () => saved(() => api.post("/api/memory",
        { text: m.text, project: m.project, category: m.category, restore: m })) });
    } catch (error) {
      toast(error.message, "error");
    }
  }

  async function clearAll() {
    const n = data.memories.length;
    if (!await confirmDialog("Delete all memories?", `All ${n} memor${n === 1 ? "y" : "ies"}: yours, your agents' and `
      + "the Governor's, general and per project. Agents won't know them any more; this can't be undone.",
    { confirm: "Delete all", danger: true })) return;
    saved(() => api.del("/api/memory"), "All memories deleted");
  }

  show(data);
}

// ---------------------------------------------------------------- notifications

async function notifications(pane, hooks = {}) {
  let settings;
  try {
    settings = await api.get("/api/settings");
  } catch (error) {
    clear(pane, h("p.error-text", error.message));
    return;
  }
  const n = settings.notifications;

  const browserToggle = h("input", { type: "checkbox", checked: browserNotificationsOn(),
    "aria-label": "System notifications in this browser",
    disabled: !browserNotificationsSupported(),
    onchange: async () => {
      const on = await setBrowserNotifications(browserToggle.checked);
      browserToggle.checked = on;
      if (browserToggle.checked !== on || (!on && Notification.permission === "denied")) {
        toast("The browser blocked notifications for this site", "error");
      }
    } });
  const browserRow = h("div.provider-row",
    h("label.switch", browserToggle, h("span")),
    h("div.info", h("div.name", "System notifications in this browser"),
      h("div.sub", browserNotificationsSupported()
        ? "Shown when a run finishes while you're elsewhere."
        : "Browsers only allow these on HTTPS or localhost. Relay also shows a toast, "
          + "a marker in the sidebar and a count in the tab title.")));

  const field = (label, input, hint) => h("label.field", h("span", label), input, hint ? h("span.hint", hint) : null);
  const ntfyUrl = h("input.input", { value: n.ntfy_url, placeholder: "https://ntfy.sh/your-secret-topic" });
  const ntfyToken = h("input.input", { value: n.ntfy_token, placeholder: "optional", type: "password",
    autocomplete: "off" });
  const webhook = h("input.input", { value: n.webhook_url, placeholder: "https://example.com/relay-hook" });
  const minSeconds = h("input.input", { type: "number", min: "0", value: String(n.notify_min_seconds) });
  const publicUrl = h("input.input", { value: n.public_url, placeholder: "http://raspberrypi.local:8420" });
  const result = h("div.check-result", { role: "status" });

  // What's notified of, per category: pushed (saved with Save, with its ntfy priority), and announced in
  // this browser (its own choice, kept at once, where the browser hears of it)
  const kinds = (n.categories || []).map((c) => {
    const push = h("input", { type: "checkbox", checked: c.push, "aria-label": `Push: ${c.label}` });
    const priority = h("select.select.notify-priority", { "aria-label": `Priority: ${c.label}`, disabled: !c.push },
      (n.priorities || []).map((p) => h("option", { value: p }, PRIORITY_NAMES[p] || p)));
    priority.value = c.priority;
    push.addEventListener("change", () => { priority.disabled = !push.checked; });
    const here = c.id in BROWSER_CATEGORIES
      ? h("label.notify-cell", h("input", { type: "checkbox", checked: browserAnnounces(c.id),
        "aria-label": `This browser: ${c.label}`, onchange: (e) => setBrowserAnnounces(c.id, e.target.checked) }),
      h("span.notify-cell-label", "This browser"))
      : h("span.notify-cell.none", { title: "This browser doesn't hear of it: only pushed" }, "–");
    const row = h("div.notify-row", h("div.notify-what", h("div.name", c.label), h("div.sub", c.hint)),
      h("label.notify-cell", push, h("span.notify-cell-label", "Push")), here, priority);
    row.hidden = hooks.agentos?.() === false && AGENTOS_ONLY.includes(c.id);  // (kept as they are)
    return { id: c.id, push, priority, row };
  });
  const kindsGrid = h("div.notify-grid",
    h("div.notify-row.head", { "aria-hidden": "true" }, h("span"), h("span", "Push"), h("span", "This browser"),
      h("span", "Priority")),
    kinds.map((k) => k.row));

  const values = () => ({
    ntfy_url: ntfyUrl.value.trim(), ntfy_token: ntfyToken.value, webhook_url: webhook.value.trim(),
    notify_min_seconds: Math.max(0, parseInt(minSeconds.value || "0", 10)), public_url: publicUrl.value.trim(),
    notify_categories:
      Object.fromEntries(kinds.map((k) => [k.id, { push: k.push.checked, priority: k.priority.value }])),
  });
  const save = async () => {
    await api.put("/api/settings", { notifications: values() });
  };
  const saveButton = h("button.btn.primary", { onclick: async () => {
    saveButton.disabled = testButton.disabled = true;
    saveButton.textContent = "Saving…";
    try {
      await save();
      toast("Notifications saved");
    } catch (error) {
      toast(error.message, "error");
    } finally {
      saveButton.disabled = testButton.disabled = false;
      saveButton.textContent = "Save";
    }
  } }, "Save");
  const testButton = h("button.btn", { onclick: async () => {
    saveButton.disabled = testButton.disabled = true;
    result.className = "check-result";
    result.textContent = "Saving…";
    let saved = false;
    try {
      await save();
      saved = true;
      result.textContent = "Settings saved. Sending…";
      const r = await api.post("/api/notifications/test");
      result.className = `check-result ${r.ok ? "ok" : "fail"}`;
      result.textContent = r.ok ? "Settings saved. Test sent; check your phone or endpoint."
        : `Settings saved. ${r.errors.join("; ")}`;
    } catch (error) {
      result.className = "check-result fail";
      result.textContent = `${saved ? "Settings saved. " : ""}${error.message}`;
    } finally {
      saveButton.disabled = testButton.disabled = false;
    }
  } }, icon("bell", "sm"), "Save and send test");

  clear(pane,
    pageHeading("Notifications", "What Relay tells you of, in this browser and on your phone: finished runs, agents "
      + "waiting for you, AgentOS, VPN problems."),
    settingsSection("In the browser",
      "When a run finishes in another chat or while the tab is in the background. This "
      + "browser toggle is saved immediately.",
      browserRow),
    settingsSection("Push to your phone", "Relay can notify you even when "
      + "it isn't open. With ntfy, install the ntfy app, subscribe to a hard-to-guess topic and paste its URL here.",
      field("ntfy topic URL", ntfyUrl, "ntfy.sh or your own ntfy server."),
      field("ntfy access token", ntfyToken, "Only for protected topics."),
      field("Webhook URL", webhook,
        "Receives a JSON POST for each notification pushed, with its category (e.g. for Home Assistant)."),
      field("Only for runs longer than (seconds)", minSeconds, "Quick answers don't need a push."),
      field("Relay's address", publicUrl, "Used for the link in notifications.")),
    settingsSection("What to notify of", "Push: to your phone and the webhook, at its ntfy priority (Save applies it). "
      + "This browser: a toast, and a system notification when those are on; saved at once, for this browser only.",
      kindsGrid,
      h("div.settings-actions.notification-actions", testButton, saveButton), result));
}

const PRIORITY_NAMES = { min: "Min", low: "Low", default: "Normal", high: "High", urgent: "Urgent" };
const AGENTOS_ONLY = ["agents.done", "agents.failed", "report"];  // (categories only AgentOS has)

// ---------------------------------------------------------------- account

// Relay's Python SDK (relay/sdk.py serves it for pip at /sdk/): installed from this Relay's own address.
function sdkSection(hooks) {
  const command = `pip install ${location.origin}${relayUrl("/sdk/")}relay_sdk-${hooks.version}-py3-none-any.whl`;
  return settingsSection("Python SDK", "Talk to Relay's agents from your own Python code: start a chat or a code "
    + "task, send it a message, and read the answer, all at once or as it comes.",
    h("div.sdk-install", h("code", command),
      h("button.icon-btn.sm", { title: "Copy the command", onclick: () => copyText(command) }, icon("copy", "sm"))),
    h("p.muted.small-text", "Then ", h("code", "from relay_sdk import *"), ": ",
      h("a", { href: relayUrl("/sdk/"), target: "_blank", rel: "noopener" }, "an example"), ". A script signs in with ",
      h("code", "login(username, password)"), hooks.loginRequired ? ", or with an app password (above)" : "",
      ". Relay's guide (docs/GUIDE.md) has the rest."));
}

// App passwords (relay/auth.py): one per script or program that uses Relay's API or the SDK, instead of the
// account's password. Shown once when made; revoked one by one here.
function appPasswordsSection() {
  const list = h("div.settings-list");
  const name = h("input.input", { placeholder: "e.g. backup script", maxlength: 80,
    onkeydown: (e) => { if (e.key === "Enter") create.click(); } });
  const made = h("div");
  const render = async () => {
    let found;
    try {
      found = await api.get("/api/app-passwords");
    } catch (error) {
      clear(list, h("p.error-text", error.message));
      return;
    }
    clear(list, found.length ? found.map((p) => h("div.provider-row.app-password-row",
      h("div.info", h("div.name", p.name),
        h("div.sub",
          `Made ${timeAgo(p.created)} · ${p.last_used ? `last used ${timeAgo(p.last_used)}` : "not used yet"}`)),
      h("div.actions", h("button.icon-btn.sm", { title: "Revoke", onclick: async () => {
        const ok =
          await confirmDialog("Revoke app password?", `What uses “${p.name}” can't reach Relay with it any more.`,
            { confirm: "Revoke", danger: true });
        if (!ok) return;
        await api.del(`/api/app-passwords/${p.id}`).catch((error) => toast(error.message, "error"));
        clear(made);
        render();
      } }, icon("trash", "sm"))))) : h("p.muted", "None yet."));
  };
  const create = h("button.btn.primary.small", { onclick: async () => {
    try {
      const app = await api.post("/api/app-passwords", { name: name.value });
      name.value = "";
      const python = `connect("${location.origin}${BASE}", app_password="${app.password}")`;
      clear(made, h("div.app-password-made",
        h("b", `“${app.name}”: copy it now. It isn't shown again.`),
        h("div.sdk-install", h("code", app.password),
          h("button.icon-btn.sm",
            { title: "Copy the app password", onclick: () => copyText(app.password) }, icon("copy", "sm"))),
        h("p.muted.small-text", "In Python, with the SDK: ", h("code", python), ", or set RELAY_URL and "
          + "RELAY_APP_PASSWORD. From any other program: the header ",
          h("code", "Authorization: Bearer <app password>"),
          ", or ", h("code", "X-Relay-App-Password: <app password>"), " behind a proxy that asks a sign-in of its own "
          + "(its name and password then go in the SDK's address: https://name:password@host).")));
      render();
    } catch (error) {
      toast(error.message, "error");
    }
  } }, icon("plus", "sm"), "Create");
  render();
  return settingsSection("App passwords", "For scripts and programs that use Relay's API or the Python SDK, instead "
    + "of your password. Each one is shown once, when it's made. Revoke one here, and only what uses it stops working.",
    list, h("label.field", h("span", "What will use it"), name), settingsActions(create), made);
}

async function account(pane, hooks) {
  if (!hooks.loginRequired) {
    clear(pane, pageHeading("Account",
      "Login is disabled (\"auth\": \"none\" in ~/.local/share/relay/settings.json). Everyone who can open "
        + "Relay uses it as the local user."),
      sdkSection(hooks));
    return;
  }
  const current = h("input.input", { type: "password", autocomplete: "current-password" });
  const next = h("input.input", { type: "password", autocomplete: "new-password" });
  const repeat = h("input.input", { type: "password", autocomplete: "new-password" });
  const error = h("div.error-text");
  const save = h("button.btn.primary", { onclick: async () => {
    error.textContent = "";
    if (next.value !== repeat.value) {
      error.textContent = "The new passwords don't match.";
      return;
    }
    try {
      await api.post("/api/account/password", { current: current.value, new: next.value });
      toast("Password changed; other sessions were signed out");
      current.value = next.value = repeat.value = "";
    } catch (err) {
      error.textContent = err.message;
    }
  } }, "Change password");
  clear(pane,
    pageHeading("Account", `Signed in as ${hooks.user.username}.`),
    settingsSection("Password", null,
      h("label.field", h("span", "Current password"), current),
      h("label.field", h("span", "New password"), next),
      h("label.field", h("span", "Repeat new password"), repeat), error),
    settingsActions(h("button.btn.danger", { onclick: hooks.onSignOut }, icon("logout", "sm"), "Sign out"), save),
    appPasswordsSection(),
    sdkSection(hooks));
}
