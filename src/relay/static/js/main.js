// Relay web app: boot, sign-in, layout, the sidebar, routing, the header and new chats.

import { relayPath, relayUrl, storeKey } from "./base.js";
import { api, ApiError } from "./api.js";
import { activityBadge, chatIcon, conversationStatus, foldClones, GOVERNOR_GROUP, sidebarGroups, taskCounts }
  from "./activity.js";
import { openChanges } from "./changes.js";
import { draftCommands } from "./commands.js";
import { createComposer } from "./composer.js";
import {
  focusPendingRequest, forgetOpening, forkConversation, moveToCode, openConversation, renderConversation, renderLimit,
  renderTimed, stopFollowing,
} from "./conversation.js";
import { openDrive } from "./drive.js";
import { usageIndicator } from "./limits.js";
import { setServer } from "./markdown.js";
import { loadPlugins, pluginPage } from "./plugins.js";
import { RunWatcher } from "./notifications.js";
import { pickFolder } from "./picker.js";
import {
  computerPicker, fallbackLoops, folderPicker, loopPicker, modelPicker, modePicker, optionsLabel, providerPicker,
} from "./pickers.js";
import { setFormHandler, setModeNames, setPermissionHandler, setProviderNames } from "./render.js";
import { agentosList, matches, scriptsList, servicesList, tasksList } from "./panels.js";
import { scheduledView } from "./schedules.js";
import { isUnsaved, scriptsView } from "./scripts.js";
import { serviceDialog, serviceView } from "./service.js";
import { openSettings } from "./settings.js";
import {
  app, dom, eligibleProviders, loadComputers, loadModes, loadProviders, loadSkills, modelsFor, modesFor, provider,
  saveDraft, state, useNewChat,
} from "./state.js";
import { tasksView } from "./tasks.js";
import { openSwitcher } from "./switcher.js";
import { closeShell, followShell, openShell, shellOpen, shellPanel } from "./terminal.js";
import { clampWidth, scriptOfPath, viewForPath, VIEWS, WIDTH } from "./views.js";
import { followVisualViewport } from "./viewport.js";
import { setTheme } from "./theme.js";
import { newProviderStart, providerPage, repairStart } from "./providerpage.js";
import {
  clear, clearKeepingFocus, clock, closeMenu, closeModals, confirmDialog, debounce, h, icon, initials, promptDialog,
  showMenu, statusDot, toast,
} from "./ui.js";

const SETUP_LOOK = 5000;  // ms between looks while Relay is setting up
export const watcher = new RunWatcher({ onOpen: (id) => navigate(`/c/${id}`) });

async function boot() {
  let status;
  try {
    status = await api.get("/api/state");
  } catch (error) {
    clear(app, h("div.auth", h("div.auth-card", h("h1", "Relay is unreachable"),
      h("p.lead", error.message), h("button.btn.primary", { onclick: boot }, "Try again"))));
    app.classList.remove("booting");
    return;
  }
  state.version = status.version;
  state.loginRequired = status.login_required;
  state.agentos = status.agentos !== false;  // (out of this Relay: Settings → AgentOS)
  state.trial = status.trial || null;  // (a new provider's trial Relay: relay/creations.py)
  app.classList.remove("booting");
  if (status.needs_setup) return showAuth("setup");
  if (!status.user) return showAuth("login");
  state.user = status.user;
  setServer(status.server);
  useNewChat(status.new_chat);  // (what new chats start with: picked last, on any device)
  await startApp();
}

function showAuth(kind) {
  const setup = kind === "setup";
  const username = h("input.input", { placeholder: "Username", autocomplete: "username" });
  const password = h("input.input", { type: "password", placeholder: "Password",
    autocomplete: setup ? "new-password" : "current-password" });
  const repeat = setup ? h("input.input", { type: "password", placeholder: "Repeat password",
    autocomplete: "new-password" }) : null;
  const error = h("div.error-text");
  const submit = h("button.btn.primary", { type: "submit" }, setup ? "Create account" : "Sign in");
  const form = h("form", {
    onsubmit: async (e) => {
      e.preventDefault();
      error.textContent = "";
      if (setup && password.value !== repeat.value) {
        error.textContent = "The passwords don't match.";
        return;
      }
      submit.disabled = true;
      try {
        await api.post(setup ? "/api/setup" : "/api/login",
          { username: username.value, password: password.value });
        await boot();
      } catch (err) {
        error.textContent = err.message;
        submit.disabled = false;
      }
    },
  },
  h("label.field", h("span.sr-only", "Username"), username),
  h("label.field", h("span.sr-only", "Password"), password),
  repeat ? h("label.field", h("span.sr-only", "Repeat password"), repeat) : null,
  error, submit);
  clear(app, h("div.auth", h("div.auth-card",
    h("div.logo", h("img", { src: relayUrl("/static/icon.svg"), alt: "" })),
    h("h1", setup ? "Welcome to Relay" : "Sign in to Relay"),
    h("p.lead", setup ? "Create the account you'll use to sign in." : "Chat and coding agents on your own machine."),
    form)));
  username.focus();
}

window.addEventListener("relay:signed-out", () => {
  if (!state.user) return;
  leave();
});

async function signOut() {
  await api.post("/api/logout").catch(() => {});
  leave();
}

// Signed out: the sign-in page, and nothing of the session left over it (Settings, a dialog, a menu)
function leave() {
  state.user = null;
  stopFollowing();
  closeService();
  closePluginPage();
  closeMenu();
  closeModals();
  showAuth("login");
}

async function startApp() {
  taskSnapshot = null;
  taskCountRequest += 1;
  state.taskCounts = null;
  buildLayout();
  if (state.agentos) {
    loadGovernor();
    loadServices();
  }
  setProviderNames((id) => provider(id)?.name || id);
  setModeNames((id) => (state.modes || []).find((m) => m.id === id)?.name || id);
  setFormHandler((runId, id, answer) => api.post(`/api/runs/${runId}/form`, { id, answer }));
  setPermissionHandler(async (runId, requestId, decision, note) => {
    note.querySelectorAll("button").forEach((b) => { b.disabled = true; });
    try {
      await api.post(`/api/runs/${runId}/permission`, { id: requestId, decision });
    } catch (error) {
      toast(error.message, "error");
      note.querySelectorAll("button").forEach((b) => { b.disabled = false; });
    }
  });
  window.addEventListener("relay:navigate", (e) => navigate(e.detail));
  await Promise.all([loadProviders(), loadModes(), loadSkills(), loadConversations(), fallbackLoops()]);
  window.addEventListener("popstate", route);
  route();
  schedulePoll();
}

// What each view's search box looks through (Chats: its own, dom.search)
const SEARCHES = { tasks: "Search tasks", agentos: "Search AgentOS", governor: "Search the Governor's chats",
  scripts: "Search scripts", production: "Search services" };

// The layout (Relay 2.11): an activity bar, a side column showing one view at a time (Chats, Tasks, AgentOS,
// Governor, Scripts, Production boxes: they replace each other there, and the main area stays as it is), the main
// area. The column is as wide as you make it, in every view.
function buildLayout() {
  dom.list = h("div.conversations");
  dom.search = h("input", { type: "search", placeholder: "Search chats", "aria-label": "Search chats",
    oninput: debounce(() => { state.search = dom.search.value; loadConversations(); }, 250) });
  dom.govList = h("div.conversations.governor-list");
  dom.panelLists = {};
  dom.views = {};
  dom.viewTitles = {};
  dom.barItems = {};
  const actions = {
    chats: [headButton("New conversation", "edit", () => newChat("chat")),
      headButton("New computer task", "monitor", () => newChat("computer"))],
    tasks: [headButton("All tasks, with their changes", "list", () => navigate("/tasks"))],
    agentos: [headButton("New task", "plus", () => navigate("/agentos?task=new"))],
    governor: [],
    scripts: [scriptsAddButton(), scriptsMoreButton()],
    production: [headButton("New service", "plus", () => serviceDialog())],
  };
  dom.panelSearch = {};
  for (const [id, label] of VIEWS) {
    const list = id === "chats" ? dom.list : id === "governor" ? dom.govList
      : dom.panelLists[id] = h(`div.conversations.panel-list.${id}-list`);
    const search = id === "chats" ? dom.search : dom.panelSearch[id] = h("input", { type: "search",
      placeholder: SEARCHES[id], "aria-label": SEARCHES[id],
      oninput: (e) => { panelFilter[id] = e.target.value; renderPanel(id); } });
    dom.views[id] = h("section.panel-view", { dataset: { view: id }, "aria-label": label },
      h("div.panel-head", dom.viewTitles[id] = h("h2.panel-title", label), h("span.grow"), ...actions[id],
        headButton("Hide the column", "sidebar", toggleSidebar)),
      h("div.side-search", icon("search", "sm"), search,
        id === "chats" ? h("kbd.side-hint", { title: "The chat switcher, from anywhere" }, "Ctrl K") : null),
      list);
  }
  // What's left of the tightest usage limit (relay.limits): a ring, all of them a click away
  dom.usage?.stop();
  dom.usage = usageIndicator({ onOpenUsage: () => openSettings({ ...settingsHooks(), tab: "usage" }) });
  dom.bar = h("nav.activity-bar", { "aria-label": "Views" },
    h("div.bar-group",
      h("span.bar-logo", { title: "Relay" }, h("img", { src: relayUrl("/static/icon.svg"), alt: "Relay" })),
      VIEWS.map(([id, label, iconName]) => {
        dom.barItems[id] = h("button.bar-item", { type: "button", title: label, "aria-label": label,
          dataset: { view: id }, onclick: () => chooseView(id) },
        icon(iconName), h("span.bar-badge", { hidden: true }), h("span.bar-dot", { hidden: true }));
        return dom.barItems[id];
      })),
    h("div.bar-group",
      dom.pluginBar = h("div.bar-plugins"),  // (plugins' buttons: relay.plugins, static/js/plugins.js)
      dom.shellItem =
        h("button.bar-item", { type: "button", title: "Shell", "aria-label": "Shell", onclick: toggleShell },
          icon("terminal")),
      dom.usage.el,
      h("button.bar-item", { type: "button", title: "Settings", "aria-label": "Settings",
        onclick: () => openSettings(settingsHooks()) }, icon("settings")),
      userButton()));
  dom.sidebar = h("aside.sidebar", dom.bar,
    h("div.side-panel", h("div.panel-views", Object.values(dom.views)), resizeHandle()));
  dom.main = h("main.main");
  dom.layout = h("div.layout", dom.sidebar, h("div.overlay", { onclick: toggleSidebar }),
    h("div.work", trialBanner(), setupBanner(), dom.main, shellPanel()));
  setPanelWidth(kept("relay.panelwidth"));
  if (kept("relay.sidebar") === "collapsed" && innerWidth > 800) {
    dom.layout.classList.add("sidebar-collapsed");
  }
  clear(app, dom.layout);
  loadPlugins({ bar: dom.pluginBar, navigate });  // (their pages wait for it: showPluginPage)
  applyAgentOS();
  // At the start, the view the page belongs to (a script's: Scripts); else the one you left it on
  const routed = viewForPath(relayPath());
  showView(routed === "chats" ? kept("relay.view") || "chats" : routed);
}

// A new provider's throwaway Relay (relay/creations.py) says so, with the way back to the Relay it's tried for
function trialBanner() {
  if (!state.trial) return null;
  return h("div.trial-banner", { role: "note" }, icon("flask", "sm"),
    h("span.grow", h("strong", `Trial Relay for ${state.trial.name}`),
      ": a throwaway Relay with only the new provider. "
      + "It's deleted when you add the provider or cancel."),
    // (the other Relay's page: its own path, not this one's)
    state.trial.back ? h("a.btn.small", { href: state.trial.back }, icon("arrowLeft", "sm"), "Back to Relay") : null);
}

// Relay building its containers' images (its first start, or an upgrade that changed them): what agents wait for,
// with nothing else on the page to say so.
function setupBanner() {
  const banner = h("div.setup-banner", { role: "status", hidden: true });
  let built = false;
  const look = async () => {
    let status = null;
    try {
      status = await api.get("/api/state");
    } catch {
      setTimeout(look, SETUP_LOOK);  // (Relay away: a restart)
      return;
    }
    const building = status.building || [];
    banner.hidden = !building.length;
    if (building.length) {
      built = true;
      clear(banner, h("div.spinner.sm"), h("span.grow", h("strong", "Relay is getting ready: "),
        `building ${building.join(" and ")}, a few minutes, once. Chats and models wait for it.`));
    }
    if (status.setting_up) setTimeout(look, SETUP_LOOK);
    else if (built) toast("Relay is ready: its containers are built");
  };
  look();
  return banner;
}

function headButton(title, iconName, onclick) {
  return h("button.icon-btn.sm", { type: "button", title, "aria-label": title, onclick }, icon(iconName, "sm"));
}

// What this browser keeps for you (only conveniences: none of it matters when it's gone)
function kept(key) {
  try { return localStorage.getItem(storeKey(key)); } catch { return null; }
}

function keep(key, value) {
  try { localStorage.setItem(storeKey(key), value); } catch { /* (only a convenience) */ }
}

// A view's icon: that view in the column; its own again folds the column away (as VS Code's)
function chooseView(id) {
  const narrow = innerWidth <= 800;
  const collapsed = dom.layout.classList.contains("sidebar-collapsed");
  if (!narrow && state.view === id && !collapsed) {
    toggleSidebar();
    return;
  }
  if (!narrow && collapsed) toggleSidebar();
  showView(id);
}

// The column shows a view (a crossfade, app.css: .panel-view); what's in the main area stays
function showView(id) {
  if (!dom.views?.[id] || dom.barItems[id]?.hidden) id = "chats";
  state.view = id;
  keep("relay.view", id);
  for (const [key, el] of Object.entries(dom.views)) {
    el.classList.toggle("shown", key === id);
    el.inert = key !== id;
  }
  renderBar();
  loadPanel(id);
}

// The column's width: dragged at its edge, or with the arrow keys there; a double click gives the usual one
function resizeHandle() {
  const handle = h("div.panel-resize", { role: "separator", tabindex: "0", "aria-orientation": "vertical",
    "aria-label": "The column's width", "aria-valuemin": String(WIDTH.min), "aria-valuemax": String(WIDTH.max),
    title: "Drag to make the column wider or narrower (double click: as it was)" });
  handle.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    handle.setPointerCapture(event.pointerId);
    const from = panelWidth();
    const start = event.clientX;
    dom.layout.classList.add("resizing");
    const move = (e) => setPanelWidth(from + e.clientX - start);
    const done = () => {
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", done);
      handle.removeEventListener("pointercancel", done);
      dom.layout.classList.remove("resizing");
      keep("relay.panelwidth", String(panelWidth()));
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", done);
    handle.addEventListener("pointercancel", done);
  });
  handle.addEventListener("keydown", (event) => {
    const step = { ArrowLeft: -16, ArrowRight: 16 }[event.key];
    if (!step) return;
    event.preventDefault();
    setPanelWidth(panelWidth() + step);
    keep("relay.panelwidth", String(panelWidth()));
  });
  handle.addEventListener("dblclick", () => {
    setPanelWidth(WIDTH.standard);
    keep("relay.panelwidth", String(WIDTH.standard));
  });
  return handle;
}

function panelWidth() {
  return clampWidth(parseFloat(dom.layout?.style.getPropertyValue("--panel-width")));
}

function setPanelWidth(value) {
  const width = clampWidth(value);
  dom.layout?.style.setProperty("--panel-width", `${width}px`);
  dom.sidebar?.querySelector(".panel-resize")?.setAttribute("aria-valuenow", String(width));
}

// AgentOS out of this Relay (Settings → AgentOS): its view is your scheduled tasks, and the Governor and the
// Production boxes are gone from the bar (what it made stays, its services run).
function applyAgentOS() {
  if (!dom.barItems?.agentos) return;
  const on = state.agentos;
  const item = dom.barItems.agentos;
  item.querySelector(".icon").replaceWith(icon(on ? "compass" : "clock"));
  item.title = on ? "AgentOS" : "Scheduled tasks";
  item.setAttribute("aria-label", item.title);
  dom.viewTitles.agentos.textContent = on ? "AgentOS" : "Scheduled";
  dom.panelSearch.agentos.placeholder = on ? SEARCHES.agentos : "Search scheduled tasks";
  dom.panelSearch.agentos.setAttribute("aria-label", dom.panelSearch.agentos.placeholder);
  for (const id of ["governor", "production"]) dom.barItems[id].hidden = !on;
  if (!on && ["governor", "production"].includes(state.view)) showView("chats");
}

// Settings → AgentOS switched it: the page as it is now, without a reload
export function setAgentOS(on) {
  state.agentos = on;
  applyAgentOS();
  sidebarSnapshot = null;
  renderSidebarList();
  renderPanel();
  if (on) {
    loadGovernor();
    loadServices();
  }
  if (relayPath().startsWith("/s/") || relayPath() === "/agentos") route();
  else renderHeader();
}

// The bar's Shell: the panel under the page, on the open chat's WorkBox (or why there's none); again: closed
function toggleShell() {
  closeMobileSidebar();  // (a phone: the drawer goes, the panel shows)
  if (shellOpen()) closeShell();
  else openShell(state.current);
}

window.addEventListener("relay:shell", () => {
  dom.shellItem?.classList.toggle("active", shellOpen());
  dom.shellItem?.setAttribute("aria-pressed", String(shellOpen()));
});

function userButton() {
  const button = h("button.bar-item.bar-user", { type: "button", title: state.user.username,
    "aria-label": `${state.user.username}: account and theme`,
    onclick: () => showMenu(button, [
      { label: "Settings", icon: "settings", onclick: () => openSettings(settingsHooks()) },
      { label: "Light theme", icon: "sun", onclick: () => setTheme("light") },
      { label: "Dark theme", icon: "moon", onclick: () => setTheme("dark") },
      { label: "System theme", icon: "monitor", onclick: () => setTheme("system") },
      ...(state.loginRequired ? ["-", { label: "Sign out", icon: "logout", onclick: signOut }] : []),
    ]),
  }, h("span.avatar", initials(state.user.username)));
  return button;
}

function toggleSidebar() {
  if (innerWidth <= 800) {
    dom.layout.classList.toggle("sidebar-open");
    return;
  }
  const collapsed = dom.layout.classList.toggle("sidebar-collapsed");
  keep("relay.sidebar", collapsed ? "collapsed" : "open");
  renderHeader();
}

function closeMobileSidebar() {
  dom.layout?.classList.remove("sidebar-open");
}

export function settingsHooks() {
  return {
    version: state.version,
    user: state.user,
    loginRequired: state.loginRequired,
    onProvidersChanged: async () => { await loadProviders(); rerenderMain(); },
    onModesChanged: async () => { await loadModes(); rerenderMain(); },
    onSkillsChanged: () => loadSkills(),
    onComputersChanged: async () => {
      await loadComputers();
      if (!state.current && relayPath() === "/" && state.draft.mode === "computer") rerenderMain();
    },
    onOpenConversation: (id) => { loadConversations(); navigate(`/c/${id}`); },
    onOpenPage: (path) => navigate(path),
    onOpenService: (id) => navigate(`/s/${id}`),
    providerName: (id) => provider(id)?.name || id,
    onSignOut: signOut,
    agentos: () => state.agentos,
    onAgentOS: setAgentOS,
  };
}

let listRequest = 0;
let lastRuns = null;  // (each conversation's last finished run, at the last full list)

export async function loadConversations() {
  if (!state.user) return;
  const request = ++listRequest;
  try {
    const query = state.search ? `?q=${encodeURIComponent(state.search)}` : "";
    const conversations = await api.get(`/api/conversations${query}`);
    // Requests can overlap (polling, sending); an older answer must not
    // overwrite a newer one.
    if (request !== listRequest) return;
    state.conversations = conversations;
    if (!state.search) loadTaskCounts(conversations);
    if (!state.search) {
      watcher.update(state.conversations, state.current?.id);
      // A run that ended may have used up a limit, or told one: the ring looks again
      const ended = (c) => c.last_run && lastRuns.has(c.id) && lastRuns.get(c.id) !== c.last_run.id;
      if (lastRuns && conversations.some(ended)) dom.usage?.refresh();
      lastRuns = new Map(conversations.map((c) => [c.id, c.last_run?.id]));
    }
    renderSidebarList();
    const open = state.current && conversations.find((c) => c.id === state.current.id);
    if (open) {
      const limitsChanged = open.resume_at !== state.current.resume_at
        || JSON.stringify(open.limit) !== JSON.stringify(state.current.limit);
      const timedChanged = JSON.stringify(open.timed) !== JSON.stringify(state.current.timed);
      const running = Boolean(state.following || state.current.running_run);
      const before = conversationStatus(state.current, { running });
      Object.assign(state.current, { needs_approval: open.needs_approval, last_run: open.last_run,
        resume_at: open.resume_at, limit: open.limit, timed: open.timed });
      const after = conversationStatus(state.current, { running });
      if (before.kind !== after.kind || before.label !== after.label || limitsChanged) renderHeader();
      if (limitsChanged) renderLimit();
      if (timedChanged) renderTimed();
    }
    if (open && open.running_run && !state.following && relayPath() === `/c/${open.id}`) {
      openConversation(open.id, open.running_run);  // Relay went on by itself (a usage limit reset)
    }
  } catch (error) {
    if (!(error instanceof ApiError && error.status === 401)) console.warn(error);
  } finally {
    if (request === listRequest) schedulePoll();
  }
}

// Keeps the list (and so the unread markers and notifications) current:
// quickly while something runs, and still in a background tab, since that's
// when a notification matters most.
let pollTimer = null;

// Every list update plans the next one, at the pace that suits what it showed.
function schedulePoll() {
  clearTimeout(pollTimer);
  const running = state.conversations.some((c) => c.running_run);
  const delay = running ? (document.hidden ? 5000 : 3000) : (document.hidden ? 15000 : 8000);
  pollTimer = setTimeout(() => {
    if (!state.user) return;
    loadConversations();
    // The column's view, kept current (a service that starts failing, a task that ends): the chat list is above
    if (["agentos", "scripts", "production"].includes(state.view) && !document.hidden) loadPanel(state.view);
  }, delay);
}

let taskSnapshot = null;
let taskCountRequest = 0;

function updateTaskCounts(groups) {
  taskCountRequest += 1;  // A Tasks refresh supersedes an older badge request.
  state.taskCounts = taskCounts(groups);
  panelData.tasks = groups;  // (the Tasks view's list too)
  renderBar();
  renderPanel("tasks");
}

async function loadTaskCounts(conversations) {
  if (relayPath() === "/tasks") return;  // Reuse that page's existing poll.
  const snapshot =
    JSON.stringify(conversations.map((conv) => [conv.id, conv.updated, conv.running_run, conv.needs_approval]));
  if (snapshot === taskSnapshot) return;
  taskSnapshot = snapshot;
  const asked = ++taskCountRequest;
  try {
    const groups = await api.get("/api/tasks");
    if (asked === taskCountRequest) updateTaskCounts(groups);
  } catch {
    if (asked === taskCountRequest) taskSnapshot = null;  // Keep the last good count; retry next poll.
  }
}

// The Governor's conversation (AgentOS): its id, once it exists, and whether it's working.
async function loadGovernor() {
  try {
    const governor = (await api.get("/api/agentos")).governor;
    state.governorId = governor?.id || null;
    state.governorWorking = Boolean(governor?.running);
  } catch {
    // (signed out, or Relay restarting: the next look tries again)
  }
  renderBar();
  renderGovernorList();
}

async function openGovernor() {
  await loadGovernor();
  if (state.governorId) {
    navigate(`/c/${state.governorId}`);
  } else {
    toast("The Governor hasn't been woken yet: turn it on and wake it on the AgentOS page");
    navigate("/agentos");
  }
}

async function loadServices() {
  try {
    state.services = await api.get("/api/services");
  } catch {
    // (signed out, or Relay restarting: the next look tries again)
  }
  renderPanel("production");
}

// The column's lists other than the chats': what each shows (loaded when it's shown, and again with the list's
// poll while it is), what's typed in its search, which groups are folded
const panelData = {};
const panelFilter = {};
const panelSnapshots = {};
let discardedOpen = false;
const scriptsFolded = new Set((kept("relay.scriptsfolded") || "").split(",").filter(Boolean));
let openScriptName = null;  // (the script open on the Scripts page: its row in the list is marked)

async function loadPanel(id = state.view) {
  try {
    if (id === "tasks") {
      updateTaskCounts(await api.get("/api/tasks"));
    } else if (id === "agentos") {
      const [os, list] = await Promise.all([api.get("/api/agentos"), api.get("/api/schedules")]);
      panelData.agentos = { os, list };
      state.governorWorking = Boolean(os.governor?.running);
      renderBar();
    } else if (id === "scripts") {
      if (openScripts) return;  // (its page keeps it current: onList)
      panelData.scripts = await api.get("/api/scripts");
    } else if (id === "production") {
      await loadServices();
      return;
    } else if (id === "governor") {
      loadGovernor();
    }
  } catch {
    // (signed out, or Relay restarting: the next look tries again)
  }
  renderPanel(id);
}

function renderPanel(id = state.view) {
  if (id === "chats" || id === "governor") {
    sidebarSnapshot = null;
    renderSidebarList();
    return;
  }
  const list = dom.panelLists?.[id];
  if (!list) return;
  const filter = panelFilter[id] || "";
  const path = relayPath();
  const task = path === "/agentos" ? new URLSearchParams(location.search).get("task") : null;
  const shown = { tasks: panelData.tasks, agentos: panelData.agentos, scripts: panelData.scripts,
    production: state.services }[id];
  // (a script being edited and not saved yet: marked in the list as you type)
  const unsaved = id === "scripts" && shown ? shown.scripts.filter((s) => isUnsaved(shown.folder, s.name)) : [];
  const snapshot = JSON.stringify([shown, filter, path, task, state.current?.id, state.agentos, discardedOpen,
    [...scriptsFolded], openScriptName, unsaved.map((s) => s.name)]);
  if (panelSnapshots[id] === snapshot && list.childElementCount) return;  // (the same: focus and hover stay)
  panelSnapshots[id] = snapshot;
  const content = id === "tasks" ? tasksList(shown, { filter, current: state.current?.id,
    onOpen: (conversation) => navigate(`/c/${conversation}`), discardedOpen,
    toggleDiscarded: () => { discardedOpen = !discardedOpen; renderPanel("tasks"); } })
    : id === "agentos" ? agentosList(shown, { filter, path, task, included: state.agentos,
      onOverview: () => navigate("/agentos"), onNeeds: () => navigate("/agentos"),
      onTask: (found) => navigate(`/agentos?task=${found}`) })
      : id === "scripts" ? scriptsList(shown, { filter, open: scriptOfPath(path) || openScriptName,
        onOpen: (name) => navigate(scriptPath(name)), folded: scriptsFolded,
        toggle: (key) => {
          if (scriptsFolded.has(key)) scriptsFolded.delete(key);
          else scriptsFolded.add(key);
          keep("relay.scriptsfolded", [...scriptsFolded].join(","));
          renderPanel("scripts");
        } })
        : servicesList(state.agentos ? shown : [], { filter, path, onOpen: (service) => navigate(`/s/${service}`) });
  clearKeepingFocus(list, content);
}

// A script's page: /scripts/<its name>, each part of it as an address has it
function scriptPath(name) {
  return name ? `/scripts/${name.split("/").map(encodeURIComponent).join("/")}` : "/scripts";
}

// The Scripts view's + and ⋯: what the Scripts page does, opened first when it isn't
function scriptsAddButton() {
  const button = headButton("New script", "plus", () => showMenu(button, [
    { label: "New script", icon: "plus", onclick: () => withScripts((page) => page.newScript()) },
    { label: "From a template", icon: "file", onclick: () => withScripts((page) => page.fromTemplate()) },
    { label: "With an agent", icon: "sparkles", onclick: () => withScripts((page) => page.withAgent()) },
  ]));
  return button;
}

function scriptsMoreButton() {
  const button = headButton("More", "more", () => showMenu(button, [
    { label: "Open in VS Code", icon: "code", onclick: () => withScripts((page) => page.openInEditor()) },
    { label: "Documentation", icon: "book", onclick: () => withScripts((page) => page.toggleDocs()) },
    { label: "Change the folder", icon: "folder", onclick: () => withScripts((page) => page.changeFolder()) },
  ], { align: "right" }));
  return button;
}

async function withScripts(act) {
  if (!openScripts) navigate("/scripts");
  if (!openScripts) return;
  await openScripts.load();
  if (openScripts) act(openScripts);
}

// The bar: which view is the column's, the Tasks count, the Governor at work (AgentOS's dot)
function renderBar() {
  for (const [id, item] of Object.entries(dom.barItems || {})) {
    item.classList.toggle("active", state.view === id);
    if (state.view === id) item.setAttribute("aria-current", "true");
    else item.removeAttribute("aria-current");
  }
  const counts = state.taskCounts;
  const tasks = dom.barItems?.tasks;
  if (tasks) {
    const badge = tasks.querySelector(".bar-badge");
    badge.textContent = counts?.total ? String(counts.total) : "";
    badge.hidden = !counts?.total;
    tasks.title = counts?.total ? `Tasks · ${counts.label}` : "Tasks";
  }
  const dot = dom.barItems?.agentos?.querySelector(".bar-dot");
  if (dot) {
    dot.hidden = !state.governorWorking || !state.agentos;
    dot.title = "The Governor is at work";
  }
}

let sidebarSnapshot = null;

export function renderSidebarList() {
  renderBar();
  // Do not replace an inline edit or the anchor of an open menu during polling.
  if (dom.list.querySelector(".rename, .menu-open") || dom.govList?.querySelector(".rename, .menu-open")) return;
  const snapshot = JSON.stringify([state.conversations, state.current?.id, state.search, panelFilter.governor,
    state.governorId, state.governorWorking, state.agentos, state.conversations.map((conv) => watcher.isUnread(conv)),
    new Date().toDateString()]);
  if (sidebarSnapshot === snapshot && dom.list.childElementCount) return;
  sidebarSnapshot = snapshot;
  // (AgentOS out of this Relay: its conversations aren't listed; they're kept)
  const listed = state.agentos ? state.conversations : state.conversations.filter((c) => !c.managed && !c.agentos_role);
  renderGovernorList(listed);
  // AgentOS's chats are the Governor view's: here only one waiting for you, or pinned by you (sidebarGroups)
  const folded = foldClones(listed, { open: state.current?.id, searching: Boolean(state.search) });
  const groups = sidebarGroups(folded).filter(([name]) => name !== GOVERNOR_GROUP);
  if (!groups.length) {
    clearKeepingFocus(dom.list, h("div.empty-list", state.search ? "No chats match your search." : "No chats yet."));
    return;
  }
  clearKeepingFocus(dom.list, groups.map(([name, items]) => h(`section.conv-group${name === "Waiting for you"
    ? ".attention" : ""}`, h("h3", name, h("span.group-count", String(items.length))),
  items.map((conv) => conversationItem(conv)))));
}

// The Governor view: its own conversation first, then the chats and code tasks AgentOS made (a service's WorkBoxes
// folded under its newest), as the chat list groups chats
function renderGovernorList(listed = null) {
  if (!dom.govList || dom.govList.querySelector(".rename, .menu-open")) return;
  listed ??= state.agentos ? state.conversations : [];
  const filter = panelFilter.governor || "";
  const managed = listed.filter((conv) => conv.managed && matches(filter, conv.title));
  const governor = state.agentos ? h(`div.conv.governor-chat${state.governorId && state.current?.id === state.governorId
    ? ".active" : ""}`, { role: "link", tabindex: "0", title: "The Governor's conversation", onclick: openGovernor,
    onkeydown: (e) => { if (e.key === "Enter") openGovernor(); } },
  icon("bot", "sm mode"), h("span.title-stack", h("span.title", "Governor"),
    state.governorWorking ? h("span.conv-status.working", "Working") : null)) : null;
  const folded = foldClones(managed, { open: state.current?.id, searching: Boolean(filter) });
  clearKeepingFocus(dom.govList, governor, managed.length
    ? sidebarGroups(folded, { apart: false }).map(([name, items]) => h("section.conv-group",
      h("h3", name, h("span.group-count", String(items.length))), items.map((conv) => conversationItem(conv))))
    : h("div.empty-list", filter ? "No chat matches." : "No chats of AgentOS's yet: its Governor makes them for "
      + "your tasks."));
}

// A chat in the list; a service's newest WorkBox also with its older ones, under it when opened.
function conversationItem(conv, nested = false) {
  const active = state.current?.id === conv.id;
  const status = conversationStatus(conv);
  const menuButton = h("button.icon-btn.sm.menu-btn", { title: "More", "aria-label": "More options",
    dataset: { focus: `${conv.id}:menu` } }, icon("more", "sm"));
  const item = h(`div.conv${active ? ".active" : ""}${nested ? ".nested" : ""}`, {
    role: "link", tabindex: "0", title: conv.title, dataset: { focus: conv.id },
    onclick: (e) => { if (!menuButton.contains(e.target)) navigate(`/c/${conv.id}`); },
    onkeydown: (e) => { if (e.key === "Enter" && e.target === item) navigate(`/c/${conv.id}`); },
  },
  chatIcon(conv),
  h("span.title-stack", h("span.title", conv.title),
    conv.resume_at && !conv.running_run ? h("span.resume-badge",
      { title: `Usage limit reached; Relay goes on at ${clock(conv.resume_at)}` },
      `Resumes at ${clock(conv.resume_at)}`)
      : ["waiting", "working", "failed"].includes(status.kind) ? h(`span.conv-status.${status.kind}`, status.label)
        : null),
  watcher.isUnread(conv) && !active
      ? h(`span.unread${conv.last_run.status === "failed" ? ".failed" : ""}`,
        { title: conv.last_run.status === "failed" ? "Failed — not seen yet" : "New result" })
      : null,
  cloneButton(conv),
  menuButton);
  menuButton.addEventListener("click", (e) => {
    e.stopPropagation();
    item.classList.add("menu-open");
    showMenu(menuButton, [
      { label: "Rename", icon: "edit", onclick: () => renameInline(item, conv) },
      { label: conv.pinned ? "Unpin" : "Pin", icon: "pin",
        onclick: () => updateConversation(conv.id, { pinned: !conv.pinned }) },
      { label: "Fork", icon: "fork", onclick: () => forkConversation(conv.id) },
      ...(conv.mode === "chat" ? [{ label: "Move to a code task…", icon: "code", onclick: () => moveToCode(conv) }]
        : [{ label: "Open files…", icon: "folder", onclick: () => openDrive(conv) }]),
      { label: "Export as Markdown", icon: "file", onclick: () => exportConversation(conv.id) },
      "-",
      { label: "Delete", icon: "trash", danger: true, onclick: () => deleteConversation(conv) },
    ], { onClose: () => { item.classList.remove("menu-open"); queueMicrotask(renderSidebarList); } });
  });
  if (!conv.older?.length || !clonesOpen(conv)) return item;
  return [item, ...conv.older.map((older) => conversationItem(older, true))];
}

// The arrow on a service's newest WorkBox: its older ones (activity.js: foldClones), opened or not as
// you left them (in this browser).
function cloneButton(conv) {
  if (!conv.older?.length) return null;
  const open = clonesOpen(conv);
  const count = conv.older.length;
  return h("button.icon-btn.sm.clones-btn", {
    title: `${count} more of ${conv.clone.service}'s WorkBoxes (earlier versions)`, "aria-expanded": String(open),
    onclick: (e) => {
      e.stopPropagation();
      try {
        if (open) localStorage.removeItem(storeKey(`relay.clones.${conv.clone.service_id}`));
        else localStorage.setItem(storeKey(`relay.clones.${conv.clone.service_id}`), "open");
      } catch { /* (only a convenience) */ }
      sidebarSnapshot = null;
      renderSidebarList();
    },
  }, h("span.clones-count", String(count)), icon(open ? "chevronDown" : "chevronRight", "sm"));
}

function clonesOpen(conv) {
  try {
    return localStorage.getItem(storeKey(`relay.clones.${conv.clone.service_id}`)) === "open";
  } catch {
    return false;
  }
}

function renameInline(item, conv) {
  const input = h("input.rename", { value: conv.title });
  const title = item.querySelector(".title");
  title.replaceWith(input);
  input.focus();
  input.select();
  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    if (save && input.value.trim() && input.value.trim() !== conv.title) {
      const saved = await updateConversation(conv.id, { title: input.value.trim() });
      if (saved) title.textContent = input.value.trim();
    }
    const restore = document.activeElement === input;
    input.replaceWith(title);
    if (restore) item.focus({ preventScroll: true });
    renderSidebarList();
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") finish(true);
    if (e.key === "Escape") finish(false);
  });
  input.addEventListener("blur", () => finish(true));
  input.addEventListener("click", (e) => e.stopPropagation());
}

export function exportConversation(id) {
  const link = h("a", { href: relayUrl(`/api/conversations/${id}/export.md`), download: "" });
  document.body.append(link);
  link.click();
  link.remove();
}

// Whether it was saved; when not, the user has been told why.
export async function updateConversation(id, changes) {
  try {
    const updated = await api.patch(`/api/conversations/${id}`, changes);
    if (state.current?.id === id) {
      Object.assign(state.current, updated);
      renderHeader();
    }
    await loadConversations();
    return true;
  } catch (error) {
    toast(error.message, "error");
    return false;
  }
}

async function deleteConversation(conv) {
  const ok = await confirmDialog("Delete chat?", `"${conv.title}" will be deleted permanently.`,
    { confirm: "Delete", danger: true });
  if (!ok) return;
  try {
    await api.del(`/api/conversations/${conv.id}`);
    if (state.current?.id === conv.id) navigate("/");
    await loadConversations();
  } catch (error) {
    toast(error.message, "error");
  }
}

export function navigate(path) {
  if (relayPath() + location.search !== path) history.pushState({}, "", relayUrl(path));  // (?task=: AgentOS's forms)
  route();
}

function route() {
  const left = state.current;  // (a temporary chat that's left goes: below)
  forgetOpening();  // a conversation still loading isn't wanted any more
  dom.main?.classList.remove("loading");
  closeMenu();
  closeMobileSidebar();
  closeService();
  closeNewProvider();
  closePluginPage();
  // From one script to another: the Scripts page stays, the script changes (its documentation stays open)
  const script = /^\/scripts(\/|$)/.test(relayPath());
  if (!script) closeScripts();
  const match = relayPath().match(/^\/c\/([0-9a-f]+)$/);
  const serviceMatch = relayPath().match(/^\/s\/([0-9a-f]+)$/);
  if (!match) followShell(null);  // (a page: the Shell, when it's open, says where one is)
  if (match) openConversation(match[1]);
  else if (serviceMatch && !state.agentos) {  // (AgentOS out of this Relay: no service pages)
    history.replaceState({}, "", relayUrl("/"));
    showNewChat();
  } else if (serviceMatch) showService(serviceMatch[1]);
  else if (relayPath() === "/tasks") showTasks();
  else if (/^\/providers\/(new|repair)(\/[0-9a-f]+|\/new)?$/.test(relayPath())) {
    const [, , kind, id] = relayPath().split("/");
    const query = new URLSearchParams(location.search);
    showProviderPage(kind, id && id !== "new" ? id : null, query.get("about") || "", query.get("provider") || "");
  }
  else if (/^\/p\/[a-z][a-z0-9_]*(\/|$)/.test(relayPath())) showPluginPage(relayPath());
  else if (script && openScripts && scriptOfPath(relayPath())) openScripts.open(scriptOfPath(relayPath()));
  else if (script) showScripts();
  else if (relayPath() === "/agentos" || relayPath() === "/scheduled") {
    if (relayPath() === "/scheduled") history.replaceState({}, "", relayUrl("/agentos"));  // AgentOS replaced it
    // (a task's form, as the column's AgentOS view opens it: ?task=<id>, or new)
    showScheduled(null, new URLSearchParams(location.search).get("task"));
    if (state.agentos) loadGovernor();  // (made or woken from here)
  }
  else {
    // (an address of no page here, or a chat's that's none: a new chat, at its own address)
    if (relayPath().startsWith("/c/")) toast("Conversation not found", "error");
    if (relayPath() !== "/") history.replaceState({}, "", relayUrl("/"));
    showNewChat();
  }
  renderBar();
  // (a plugin's button, while one of its pages is open)
  dom.pluginBar?.querySelectorAll(".plugin-item").forEach((button) => button.classList.toggle("active",
    new RegExp(`^/p/${button.dataset.plugin}(/|$)`).test(relayPath())));
  renderPanel();  // (the page open: its row marked in the column)
  if (left?.temporary && relayPath() !== `/c/${left.id}`) dropTemporary(left.id);
}

// A temporary chat that's left goes (its agent stopped, if it still answers)
function dropTemporary(id) {
  api.del(`/api/conversations/${id}`).catch(() => {});
}

function newChat(mode) {
  state.draft.mode = mode;
  saveDraft();
  const go = () => {
    navigate("/");
    if (relayPath() === "/") showNewChat();
  };
  if (mode === "computer") loadComputers().then(go);  // which are online now
  else go();
}

function rerenderMain() {
  // Settings change controls, not the current route, stream or draft.
  if (!state.current && relayPath() !== "/") { renderHeader(); return; }
  if (!state.current) prepareDraft(state.draft.mode);
  if (state.current || dom.composer?.el.isConnected) {
    dom.composer?.refreshPickers();
    renderHeader();
  } else showNewChat();
}

export function renderHeader() {
  if (!dom.header) return;
  const conv = state.current;
  // (a phone: the bar and the column are a drawer, opened here; elsewhere the bar is always there)
  const narrow = innerWidth <= 800;
  const items = [
    narrow ? h("button.icon-btn", { title: "Open sidebar", onclick: toggleSidebar }, icon("sidebar")) : null,
    narrow ? h("button.icon-btn",
      { title: "New conversation", onclick: () => newChat(state.draft.mode) }, icon("edit")) : null,
  ];
  if (conv) {
    const titleButton = h("button.title-btn", { title: "Rename", onclick: async () => {
      const title = await promptDialog("Rename chat", { value: conv.title });
      if (title && title.trim()) updateConversation(conv.id, { title: title.trim() });
    } }, h("span.truncate", conv.title));
    const status = conversationStatus(conv, { running: Boolean(state.following || conv.running_run) });
    const activity = status.kind === "waiting"
      ? h("button.activity-badge.waiting", { type: "button", title: "Go to the unanswered question or approval",
        onclick: focusPendingRequest }, h("span.activity-dot"), status.label, icon("chevronDown", "sm"))
      : activityBadge(status);
    // (On a phone this strip scrolls sideways, as the pickers under the message box do: one row, always.)
    items.push(h("div.header-strip", h("div.header-heading", titleButton, activity), h("div.meta",
      state.agentos && (conv.managed || conv.agentos_role)
        ? h("span.chip.managed", { title: conv.agentos_role === "governor"
          ? "AgentOS's Governor: it steers other agents for your tasks"
          : "Managed by AgentOS: its Governor may see, change and delete it" }, icon("compass", "sm"),
        conv.agentos_role === "governor" ? "AgentOS Governor" : "Managed by AgentOS") : null,
      conv.mode === "code" ? changesButton(conv) : null,
      state.agentos && conv.clone?.own ? promoteButton(conv) : null,
      // (Its folder and its provider and model are under the message box: the note and the pickers.)
      conv.computer ? h("span.chip.computer", { title: conv.computer.name
        ? `${conv.computer.name}: ${conv.computer.online ? "online" : "offline (its connector isn't running)"}`
        : "This task's computer was removed from Relay" },
      icon("monitor", "sm"), h("span.truncate", conv.computer.name || "removed"),
      statusDot(conv.computer.online ? "ok" : "fail")) : null,
      conv.temporary ? h("span.chip.temporary", { title: "A temporary chat: in no list, without memory; it goes "
        + "when you leave it (Keep it, under the message box, makes it a chat like any other)" },
      icon("temporary", "sm"), "Temporary") : null)));
  } else if (relayPath() === "/" && draftKind() === "chat") {
    // The next chat temporary or not: in no list, without memory, gone when it's left
    items.push(h("span.grow"), h(`button.chip.temporary-toggle${state.draft.temporary ? ".on" : ""}`, {
      "aria-pressed": state.draft.temporary ? "true" : "false",
      title: "A temporary chat is in no list and has no memory; it goes when you leave it",
      onclick: () => { state.draft.temporary = !state.draft.temporary; dom.welcomeRefresh?.(); },
    }, icon("temporary", "sm"), "Temporary"));
  }
  clear(dom.header, items);
}

// A service's WorkBox: its folder as the service's new live version (relay.production: promote_clone),
// or, when the live version moved on since it was made (a fix of the Governor's, a rollback), first
// brought up to date with it (update_clone: a three-way merge).
function promoteButton(conv) {
  const behind = !conv.clone.fresh;
  const button = h("button.chip.promote-btn", {
    title: behind
      ? `Made from version ${conv.clone.version ?? "?"}, but ${conv.clone.service} has another version live `
      + "now: merge that into this WorkBox first (your changes stay), then promote"
      : `Start this folder as ${conv.clone.service}'s new live version; it takes over once its health check passes, `
        + "and the old one is kept to roll back to",
    onclick: () => {
      button.disabled = true;
      (behind ? updateWorkbox(conv) : promoteClone(conv)).catch((error) => toast(error.message, "error"))
        .finally(() => renderHeader());
    },
  }, icon(behind ? "refresh" : "rocket", "sm"), behind ? "Update from live" : "Promote to live");
  return button;
}

async function refreshWorkbox(conv) {
  const fresh = await api.get(`/api/conversations/${conv.id}`).catch(() => null);
  if (fresh && state.current?.id === fresh.id) {
    state.current = fresh;
    renderHeader();
  }
  loadConversations();
  loadServices();
}

// Promote to live, and /promote: the WorkBox's folder as its service's new version.
export async function promoteClone(conv) {
  if (!conv.clone.fresh) {
    const update = await confirmDialog("Update from live first?", `${conv.clone.service} has another version live now `
      + `than the one this WorkBox was made from (${conv.clone.version ?? "?"}): `
      + "promoting it as it is would undo that. "
      + "Updating merges the live version's changes in; yours stay.", { confirm: "Update from live" });
    if (update) await updateWorkbox(conv);
    return;
  }
  try {
    toast((await api.post(`/api/services/${conv.clone.service_id}/promote`, { conversation_id: conv.id })).message);
  } finally {
    await refreshWorkbox(conv);
  }
}

// Update from live: the live version's changes merged into the WorkBox (relay.production: update_clone).
export async function updateWorkbox(conv) {
  try {
    const done = await api.post(`/api/services/${conv.clone.service_id}/update-workbox`, { conversation_id: conv.id });
    const list = (names) => names.slice(0, 6).join(", ") + (names.length > 6 ? ` and ${names.length - 6} more` : "");
    if (done.conflicts.length || done.kept.length) {
      toast(`Updated to version ${done.version}. `
        + (done.conflicts.length ? `Conflicts (<<<<<<< markers) in ${list(done.conflicts)}. ` : "")
        + (done.kept.length ? `Kept yours, changed on both sides: ${list(done.kept)}. ` : "")
        + "Have the agent resolve them, then /promote.", "error");
    } else {
      toast(`Updated to version ${done.version}`
        + (done.merged.length ? `: ${done.merged.length} file${done.merged.length === 1 ? "" : "s"} merged in` : "")
        + ". /promote makes it live.");
    }
  } finally {
    await refreshWorkbox(conv);
  }
}

function latestChanges(conv) {
  if (conv.changes !== undefined && conv.changes !== null) return conv.changes;
  const answers = (conv.messages || []).filter((m) => m.role === "assistant" && m.changes);
  return answers.length ? answers[answers.length - 1].changes : null;
}

function changesButton(conv) {
  const summary = latestChanges(conv);
  const label = conv.git?.state === "discarded" ? "Discarded"
    : "Review changes";
  return h("button.chip.changes-btn", { title: "Review the task's changes", "aria-label": label, onclick: showChanges },
    icon("file", "sm"),
    h("span.changes-label", label), summary?.files ? h("span.change-total", String(summary.files)) : null);
}

export function showChanges() {
  if (!state.current) return;
  const conv = state.current;
  openChanges(dom.main, conv, {
    onChanged: async () => {
      if (state.current?.id !== conv.id) return;
      const fresh = await api.get(`/api/conversations/${conv.id}`).catch(() => null);
      if (!fresh || state.current?.id !== fresh.id) return;
      const discarded = fresh.git?.state === "discarded" && state.current.git?.state !== "discarded";
      state.current = fresh;
      renderHeader();
      if (discarded) renderConversation();
      loadConversations();
    },
  });
}

let tasksTimer = null;

function showTasks() {
  stopFollowing();
  state.current = null;
  dom.composer = null;
  renderSidebarList();
  dom.header = h("header.header");
  const view = tasksView({
    onLoaded: (groups) => { if (relayPath() === "/tasks" && view.el.isConnected) updateTaskCounts(groups); },
    providerName: (id) => provider(id)?.name || id,
    onOpen: (id) => navigate(`/c/${id}`),
    onReview: async (id) => {
      navigate(`/c/${id}`);
      await waitFor(() => state.current?.id === id);
      showChanges();
    },
  });
  clear(dom.main, dom.header, view.el);
  renderHeader();
  watcher.setTitle("Tasks · Relay");
  view.load();
  clearInterval(tasksTimer);
  tasksTimer = setInterval(() => {
    if (relayPath() !== "/tasks") clearInterval(tasksTimer);
    else if (!document.hidden) view.load();
  }, 4000);
}

let scheduledTimer = null;

function showScheduled(prefill = null, task = null) {
  stopFollowing();
  state.current = null;
  dom.composer = null;
  renderSidebarList();
  dom.header = h("header.header");
  const view = scheduledView({
    providers: () => state.providers,
    defaultProvider: () => state.draft.providerId,
    providerName: (id) => provider(id)?.name || id,
    modelsFor,
    modesFor,
    pickFolder,
    agentos: () => state.agentos,
    onOpen: (id) => navigate(`/c/${id}`),
    openMemory: (place) => openSettings({ ...settingsHooks(), tab: "memory", memoryPlace: place }),
    openService: (id) => navigate(`/s/${id}`),
    openScripts: () => navigate("/scripts"),
    // A task's form open: its address (/agentos?task=<id>, kept on a reload), its row in the column marked
    onForm: (task) => {
      const path = task ? `/agentos?task=${task}` : "/agentos";
      if (relayPath() === "/agentos" && relayPath() + location.search !== path) {
        history.replaceState({}, "", relayUrl(path));
      }
      renderPanel("agentos");
    },
    prefill,
  });
  clear(dom.main, dom.header, view.el);
  renderHeader();
  watcher.setTitle("AgentOS · Relay");
  const loaded = view.load();
  if (task && !prefill) {
    Promise.resolve(loaded).then(() => {
      if (relayPath() === "/agentos" && view.el.isConnected) return task === "new"
        ? view.newTask() : view.openTask(task);
      return null;
    });
  }
  // Tasks run and the Governor works meanwhile: kept up to date while it's open (a simulated user saw it stand
  // still), but not under your hands: while you type in it, or a menu or dialog is open (it's drawn anew)
  clearInterval(scheduledTimer);
  scheduledTimer = setInterval(() => {
    if (relayPath() !== "/agentos") clearInterval(scheduledTimer);
    else if (!document.hidden && !document.querySelector(".modal, .menu")
             && !(view.el.contains(document.activeElement)
                 && document.activeElement.matches("input, select, textarea"))) {
      view.refresh();
    }
  }, 10000);
}

// Your scripts with Relay's SDK (relay/scripts.py): written and run here, or written by an agent.
let openScripts = null;
let scriptsTimer = null;

function showScripts() {
  stopFollowing();
  state.current = null;
  dom.composer = null;
  renderSidebarList();
  dom.header = h("header.header");
  openScripts = scriptsView({
    onOpen: (id) => navigate(`/c/${id}`),
    pickFolder,
    agentName: () => {
      const chosen = scriptWriter();
      return chosen ? [provider(chosen.providerId)?.name || chosen.providerId, chosen.model].filter(Boolean).join(" · ")
        : "no provider that works in folders";
    },
    writeWithAgent: writeScript,
    version: () => state.version,
    host: () => dom.main,
    // The script the page's address names; the list in the column (Scripts view) as the page has it
    wanted: () => scriptOfPath(relayPath()),
    onList: (data, open) => {
      panelData.scripts = data;
      openScriptName = open;
      renderPanel("scripts");
    },
    onOpened: (name) => {
      openScriptName = name;
      const path = scriptPath(name);
      if (/^\/scripts(\/|$)/.test(relayPath()) && relayPath() !== path) history.replaceState({}, "", relayUrl(path));
      renderPanel("scripts");
    },
  });
  clear(dom.main, dom.header, openScripts.el);
  renderHeader();
  watcher.setTitle("Scripts · Relay");
  openScripts.load();
  clearInterval(scriptsTimer);
  scriptsTimer = setInterval(() => {
    if (!/^\/scripts(\/|$)/.test(relayPath())) clearInterval(scriptsTimer);
    else if (!document.hidden) openScripts?.load();
  }, 4000);
}

function closeScripts() {
  openScripts?.close();
  openScripts = null;
  openScriptName = null;
}

// The agent that writes a script: the one chosen under the message box, when it works in folders
function scriptWriter() {
  const able = eligibleProviders("code");
  const providerId = able.some((p) => p.id === state.draft.providerId) ? state.draft.providerId : able[0]?.id;
  return providerId
    ? { providerId, model: providerId === state.draft.providerId ? state.draft.model || "" : "" } : null;
}

// A code task in the scripts' folder with Relay's skill for it; you watch it work there
async function writeScript(description) {
  const chosen = scriptWriter();
  if (!chosen) {
    toast("This needs a provider that works in folders (Settings → Providers)", "error");
    return;
  }
  try {
    // (Relay makes it a code task in the scripts' public/, with its skill for it)
    const { conversation, run } = await api.post("/api/scripts/write",
      { description, provider_id: chosen.providerId, model: chosen.model });
    closeScripts();
    history.pushState({}, "", relayUrl(`/c/${conversation}`));
    await openConversation(conversation, run);
    renderBar();
    loadConversations();
  } catch (error) {
    toast(error.message, "error");
  }
}

// A live service: a shell in its ProductionBox in the chat's place (agents don't work there).
let openService = null;

function showService(id) {
  stopFollowing();
  state.current = null;
  dom.composer = null;
  renderSidebarList();
  dom.header = h("header.header");
  openService = serviceView(id, {
    onOpen: (conversation) => navigate(`/c/${conversation}`),
    onChanged: (service) => {
      watcher.setTitle(`${service.name} · Relay`);
      const listed = (state.services || []).findIndex((s) => s.id === service.id);
      if (listed >= 0) { state.services[listed] = service; renderPanel("production"); }  // (its health, managed or not)
    },
    onDeleted: () => { loadServices(); navigate("/agentos"); },
  });
  clear(dom.main, dom.header, openService.el);
  renderHeader();
  watcher.setTitle("Production box · Relay");
}

function closeService() {
  openService?.close();
  openService = null;
}

// A provider's page (relay/creations.py): one Relay doesn't know, added with an agent (/providers/new/…), or one it
// has, repaired (/providers/repair/…), step by step.
let openNewProvider = null;

function showProviderPage(kind, id, about, providerId) {
  stopFollowing();
  state.current = null;
  dom.composer = null;
  renderSidebarList();
  dom.header = h("header.header");
  const opened = (made, said = "") => {  // (its address names it from now on; the Governor's words go along)
    history.replaceState({}, "",
      relayUrl(`/providers/${kind}/${made}`) + (said ? `?${new URLSearchParams({ about: said })}` : ""));
    route();
  };
  openNewProvider = id
    ? providerPage(id, { kind, about, onOpenConversation: (conversation) => navigate(`/c/${conversation}`) })
    : kind === "repair" ? repairStart(providerId, { onOpen: opened }) : newProviderStart({ about, onOpen: opened });
  clear(dom.main, dom.header, openNewProvider.el);
  renderHeader();
  watcher.setTitle(kind === "repair" ? "Repair a provider · Relay" : "Add a provider · Relay");
}

function closeNewProvider() {
  openNewProvider?.close();
  openNewProvider = null;
}

// A plugin's page (/p/<plugin>/…: static/js/plugins.js), drawn where a chat would be
let openPlugin = null;
async function showPluginPage(path) {
  stopFollowing();
  state.current = null;
  dom.composer = null;
  renderSidebarList();
  const page = await pluginPage(path);
  if (relayPath() !== path) {  // (another page was opened meanwhile)
    page.close();
    return;
  }
  openPlugin = page;
  dom.header = h("header.header.plugin-header");
  clear(dom.main, dom.header, page.el);
  renderHeader();
  watcher.setTitle(`${page.title} · Relay`);
}

function closePluginPage() {
  openPlugin?.close();
  openPlugin = null;
}

// /schedule: this chat's prompt again later, on in the same chat.
export function scheduleFromChat(conv, prompt) {
  history.pushState({}, "", relayUrl("/agentos"));
  showScheduled({ name: conv.title, prompt: prompt || "", provider_id: conv.provider_id, model: conv.model || "",
    agent_mode: conv.agent_mode, effort: conv.effort, mode: conv.mode, target: "same", conversation_id: conv.id,
    when: { kind: "once", at: Math.ceil(Date.now() / 3600000) * 3600 } });
}

function waitFor(condition, timeout = 5000) {
  return new Promise((resolve) => {
    const started = Date.now();
    const check = () => (condition() || Date.now() - started > timeout ? resolve() : setTimeout(check, 50));
    check();
  });
}

function prepareDraft(mode) {
  if (mode === "computer" && !(state.computers || []).some((c) => c.id === state.draft.computerId)) {
    state.draft.computerId = (state.computers || []).find((c) => c.online)?.id || state.computers?.[0]?.id || null;
  }
  const choices = eligibleProviders(mode);
  if (!choices.some((p) => p.id === state.draft.providerId)) {
    state.draft.providerId = choices[0]?.id || null;
    state.draft.model = "";
  }
  return choices;
}

// What the new conversation's screen says: a conversation in a project folder is a code task;
// without one, a chat. (Only agents work in folders.)
const WELCOME = {
  chat: { title: "What can I help with?", placeholder: "Ask anything",
    lead: "Your conversations, models and agents. All in one place.", folder: "Choose a project folder",
    folderAfter: " to work on code.",
    suggestions: ["Explain a concept simply", "Help me write an email", "Brainstorm ideas for a weekend project"] },
  code: { title: "What should we build?", placeholder: "Describe a coding task…",
    lead: "Give your agent a clear task. Review the result.",
    suggestions: ["Explain how this project is structured", "Find and fix a failing test",
      "Add a README with setup instructions"] },
  computer: { title: "What should we do on your computer?", placeholder: "What should happen on the computer?",
    lead: "Work on your own computer, with the agent and model you choose.",
    suggestions: ["How much disk space is left, and what takes the most?", "Turn on dark mode",
      "Update the installed programs"] },
  temporary: { title: "Temporary chat", placeholder: "Ask anything",
    lead: "It's in no list and has no memory: nothing of it is used or kept for later. It goes when you leave it.",
    suggestions: ["Explain a concept simply", "Check this text for mistakes", "Think through a decision with me"] },
};

function draftKind() {
  if (state.draft.mode === "computer") return "computer";
  return provider(state.draft.providerId)?.tools && state.draft.inFolder && state.draft.directory ? "code" : "chat";
}

// What the new chat screen says: a temporary chat's words when it's one (a plain chat only)
function welcomeKind() {
  const kind = draftKind();
  return kind === "chat" && state.draft.temporary ? "temporary" : kind;
}

export function showNewChat() {
  stopFollowing();
  state.current = null;
  renderSidebarList();
  const mode = state.draft.mode;
  if (mode === "computer" && state.computers === undefined) {
    loadComputers().then(() => { if (relayPath() === "/" && state.draft.mode === "computer") rerenderMain(); });
  }
  const choices = prepareDraft(mode);
  const title = h("h1");
  const lead = h("p.welcome-lead");
  const suggestions = h("div.suggestions");
  // The words follow the folder; what's typed stays.
  const refresh = () => {
    composer.refreshPickers();
    renderHeader();  // (its Temporary switch: for a plain chat)
    const welcome = WELCOME[welcomeKind()];
    title.textContent = welcome.title;
    // Its folder, right from what says so (on a phone the picker can be a scroll away, a simulated user found)
    const folder = welcome.folder && mode === "chat" && provider(state.draft.providerId)?.tools;
    clear(lead, welcome.lead, folder ? [" ", h("button.link-btn", { type: "button", onclick: async () => {
      const chosen = await pickFolder(state.draft.directory);
      if (chosen) { Object.assign(state.draft, { directory: chosen, inFolder: true }); saveDraft(); refresh(); }
    } }, welcome.folder), welcome.folderAfter] : null);
    composer.el.querySelector("textarea").placeholder = welcome.placeholder;
    clear(suggestions, welcome.suggestions.map((text) => h("button", { onclick: () => composer.setText(text) }, text)));
  };
  dom.header = h("header.header");
  dom.welcomeRefresh = () => refresh();
  const composer = createComposer({
    placeholder: WELCOME[welcomeKind()].placeholder,
    onSend: (text, attachments, shown) => startConversation(text, attachments, shown),
    commands: () => draftCommands(refresh),
    // Changing a picker only redraws the pickers, so the typed message and
    // attachments stay.
    pickers: () => ({ primary: [
      providerPicker(state.draft.providerId, mode, (id) => {
        state.draft.providerId = id; state.draft.model = ""; saveDraft(); refresh();
      }),
      modelPicker(state.draft.providerId, state.draft.model, (model, effort) => {
        state.draft.model = model;
        if (effort !== undefined) state.draft.effort = effort;
        saveDraft(); composer.refreshPickers();
      }, state.draft.effort),
      mode === "chat" && provider(state.draft.providerId)?.tools ? folderPicker({
        directory: state.draft.inFolder ? state.draft.directory : "",
        onChoose: () => { state.draft.inFolder = true; saveDraft(); refresh(); },
        onClear: () => { state.draft.inFolder = false; saveDraft(); refresh(); },
      }) : null,
      mode === "computer" ? computerPicker(() => composer.refreshPickers()) : null,
    ], advanced: [
      modePicker(state.draft.providerId, state.draft.agentMode, (id) => {
        state.draft.agentMode = id; saveDraft(); composer.refreshPickers();
      }),
      loopPicker(state.draft.fallbackLoop, state.draft.providerId, state.draft.model, (value) => {
        state.draft.fallbackLoop = value; saveDraft(); composer.refreshPickers();
      }),
    ], label: optionsLabel(state.draft.providerId, state.draft.agentMode) }),
  });
  dom.composer = composer;
  refresh();
  const noComputers = mode === "computer" && state.computers !== undefined && !state.computers.length;
  const content = noComputers ? [
    h("h1", "No computers yet"),
    h("p.muted", "A computer task works on one of your own computers, which runs Relay's small connector script."),
    h("button.btn.primary", { onclick: () => openSettings({ ...settingsHooks(), tab: "computers" }) },
      icon("monitor", "sm"), "Add a computer"),
  ] : choices.length ? [title, lead, h("div.composer-wrap", composer.el), suggestions] : [
    h("h1", "No providers yet"),
    h("p.muted", mode === "chat" ? "Add a provider to start a conversation."
      : "This needs a provider with tools (Claude Code, opencode, Copilot, a chat model with Relay's tools, …)."),
    h("button.btn.primary", { onclick: () => openSettings({ ...settingsHooks(), tab: "providers" }) },
      icon("plug", "sm"), "Add a provider"),
  ];
  clear(dom.main, dom.header, h("div.welcome", content));
  renderHeader();
  watcher.setTitle("Relay");
  composer.focus();
}

async function startConversation(text, attachments = []) {
  const { providerId, model, computerId } = state.draft;
  const mode = draftKind();
  const directory = mode === "code" ? state.draft.directory : null;
  const agentMode = modesFor(providerId).some((m) => m.id === state.draft.agentMode) ? state.draft.agentMode : null;
  const effort = state.draft.effort || null;
  const fallbackLoop = state.draft.fallbackLoop || "auto";
  if (!providerId) {
    toast("Choose a provider first", "error");
    return false;
  }
  if (mode === "computer" && !computerId) {
    toast("Choose a computer first", "error");
    return false;
  }
  const temporary = mode === "chat" && state.draft.temporary;
  try {
    const conv = await api.post("/api/conversations", mode === "computer"
      ? { provider_id: providerId, model, mode: "chat", computer_id: computerId, agent_mode: agentMode, effort,
        fallback_loop: fallbackLoop }
      : { provider_id: providerId, model, mode, directory, agent_mode: agentMode, effort, fallback_loop: fallbackLoop,
        temporary });
    // (the next new chat chooses again: not temporary, and no project folder; the folder picked stays in the picker)
    Object.assign(state.draft, { temporary: false, inFolder: false });
    saveDraft();
    const { run_id: runId } = await api.post(`/api/conversations/${conv.id}/messages`,
      { text, attachments });
    if (relayPath() !== "/") {
      loadConversations();  // the user went elsewhere meanwhile; don't pull them back
      return true;
    }
    history.pushState({}, "", relayUrl(`/c/${conv.id}`));
    await openConversation(conv.id, runId);
    loadConversations();
  } catch (error) {
    toast(error.message, "error");
    return false;
  }
  return true;
}

// Coming back to the tab: the open conversation's result has been seen now.
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && state.user) loadConversations();
});

document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === "o" && state.user) {
    e.preventDefault();
    newChat(state.draft.mode);
  }
  // Ctrl+K (⌘K): the chat switcher, from anywhere but a terminal (where it's the shell's: kill the line)
  if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === "k" && state.user && dom.layout
      && !e.target.closest?.(".xterm")) {
    e.preventDefault();
    // (the chats the list has, then the server's search: AgentOS's among them when it's in Relay)
    const allowed = (conv) => state.agentos || (!conv.managed && !conv.agentos_role);
    openSwitcher({ current: state.current?.id || null, onOpen: (id) => navigate(`/c/${id}`), allowed,
      listed: () => state.conversations.filter(allowed) });
  }
});

window.addEventListener("resize", debounce(() => renderHeader(), 150));

// "Thinking…" after a while with nothing to show: for how long, so a model that doesn't answer isn't a spinner
// for ever (a simulated user waited 105 s for one). render.js marks when it began.
setInterval(() => {
  for (const line of document.querySelectorAll(".status-line[data-since]")) {
    const seconds = Math.round((Date.now() - Number(line.dataset.since)) / 1000);
    if (seconds >= 10) line.lastChild.textContent = `${line.dataset.label} ${seconds} s`;
  }
}, 1000);

export { loadServices };  // (for /promote; a list here keeps it out of the parts tests/js runs on their own)

followVisualViewport();  // (the on-screen keyboard: the layout fits above it)

// The vendored libraries load with `defer`; module scripts run after them.
boot();
