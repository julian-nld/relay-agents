// The side panel: a chat with one of your Relay agents, with what's in the browser going along; and your
// scripts (scripts.js).

import { connect } from "./lib/relay.js";
import { agentsLink, Cards } from "./lib/cards.js";
import { inPage } from "./lib/agent.js";
import { activeTab, askToReadPages, compose, readTab, screenshot, unescape } from "./lib/page.js";
import { renderMarkdown, setRelay } from "./lib/markdown.js";
import { ago, fill, fillIcons, h, icon } from "./lib/ui.js";
import { ScriptsView } from "./scripts.js";

const $ = (id) => document.getElementById(id);
const dom = {
  title: $("title"), titleText: $("title-text"), newChat: $("new"), open: $("open"), settings: $("settings"),
  history: $("history"), search: $("search"), historyList: $("history-list"), setup: $("setup"),
  thread: $("thread"), composer: $("composer"), notice: $("notice"), contexts: $("contexts"), input: $("input"),
  addPage: $("add-page"), addShot: $("add-shot"), provider: $("provider"), model: $("model"), send: $("send"),
  toast: $("toast"), scriptsButton: $("scripts-btn"), scripts: $("scripts"), queue: $("queue"), asks: $("asks"),
};

const state = {
  relay: null,
  providers: [],
  // the conversation as Relay gives it; null: a new chat, made at its first message
  conv: null,
  agent: { provider: "", model: "" },  // who answers the next message
  contexts: [],                        // what goes along with the next message
  sent: [],                            // the messages sent from here to this chat (Relay's copy, conv, may lag)
  following: null,                    // { runId, abort, view }: the answer coming now
  sending: false,
  ready: false,
  stick: true,                         // the thread follows the answer down (unless you scrolled up)
  view: "chat",                        // or "scripts"
  rewrite: null,                       // the field the next message's answer may go back into (the menu's Rewrite)
};
// The fields answers may go back into ("Replace in field"): { runId: { tabId, frameId, target } }, for as long as
// Chrome
// runs (the field is kept by the page, until it's reloaded).
const rewrites = {};
const ONLY = " Answer with only the new text: no quotes around it, nothing before or after it.";

const scripts = new ScriptsView(dom.scripts, {
  relay: () => state.relay,
  toast,
  openChat: async (id) => {
    await setView("chat");
    openConversation(id);
  },
});

const KIND_ICON = { page: "page", selection: "quote", link: "link", screenshot: "image" };
const BROWSER = "\n\nFrom my browser:\n\n";  // (compose's: where what went along starts)

// Starting

async function init() {
  fillIcons();
  bind();
  Object.assign(rewrites, (await chrome.storage.session.get("rewrites")).rewrites || {});
  await start();
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === "local" && changes.settings) start();
    if (area === "session" && changes.pending?.newValue) takePending();
  });
}

async function start() {
  stopFollowing();
  state.ready = false;
  state.relay = await connect();
  setRelay(state.relay.address);
  const problem = await check();
  showSetup(problem);
  if (problem) return;
  try {
    state.providers = (await state.relay.providers()).filter((p) => p.enabled !== false);
  } catch (error) {
    showSetup({ text: error.message, retry: true });
    return;
  }
  const { current, view } = await chrome.storage.local.get(["current", "view"]);
  if (current) await openConversation(current, { quiet: true });
  else await newChat();
  scripts.rows = scripts.current = null;  // (Relay may be another one now)
  if (view === "scripts") await setView("scripts");
  state.ready = true;
  takePending();
}

// What Relay's agents ask to do in this browser: the service worker's cards (lib/cards.js), shown above the message
// box.
const cards = new Cards(dom.asks, {
  decide: (item, decision) => agents.decide(item, decision),
  openChat: async (id) => {
    if (state.view !== "chat") await setView("chat");
    toggleHistory(false);
    openConversation(id);
  },
});
const agents = agentsLink((items) => cards.render(items));

// The chat, or your scripts.
async function setView(view) {
  state.view = view;
  document.body.classList.toggle("scripts-view", view === "scripts");
  dom.scripts.hidden = view !== "scripts";
  dom.scriptsButton.classList.toggle("active", view === "scripts");
  chrome.storage.local.set({ view });
  renderHeader();
  if (view === "scripts") {
    toggleHistory(false);
    await scripts.show();
  } else {
    scripts.hide();
  }
}

// What's in the way of talking to Relay; null: nothing.
async function check() {
  const relay = state.relay;
  if (!relay.configured) return { text: "Tell the extension where your Relay is: its address, and an app password." };
  if (!(await relay.allowed())) return { text:
    `Chrome doesn't let the extension reach ${relay.address.base} yet: save the address in the settings once more.` };
  try {
    const found = await relay.state();
    if (found.login_required && !found.user) {
      return relay.settings.appPassword
        ? { text: "Relay doesn't know that app password (any more): make a new one in Relay's Settings → Account." }
        : { text:
            "Not signed in to Relay. Add an app password (Relay's Settings → Account) in the extension's settings, or "
            + "sign in to Relay in this browser.", signIn: true };
    }
  } catch (error) {
    return { text: error.message, retry: error.status === 0 };
  }
  return null;
}

function showSetup(problem) {
  dom.setup.hidden = !problem;
  dom.thread.hidden = dom.composer.hidden = Boolean(problem);
  dom.title.disabled = dom.newChat.disabled = dom.scriptsButton.disabled = Boolean(problem);
  if (!problem) return;
  document.body.classList.remove("scripts-view");
  dom.scripts.hidden = true;
  scripts.hide();
  dom.history.hidden = true;
  dom.titleText.textContent = "Not connected";
  dom.setup.replaceChildren(
    h("img.setup-logo", { src: "icons/icon.svg", alt: "" }),
    h("h1", "Relay"),
    h("p", problem.text),
    h("div.setup-actions",
      h("button.btn.primary", { onclick: () => chrome.runtime.openOptionsPage() }, "Open settings"),
      problem.signIn ? h("button.btn", { onclick: () => openTab(state.relay.link("/")) }, "Sign in to Relay") : null,
      h("button.btn", { onclick: () => start() }, "Try again")),
  );
}

function bind() {
  dom.newChat.addEventListener("click", async () => {
    toggleHistory(false);
    if (state.view !== "chat") await setView("chat");
    newChat();
  });
  dom.scriptsButton.addEventListener("click", () => setView(state.view === "scripts" ? "chat" : "scripts"));
  dom.open.addEventListener("click", () => openTab(state.view === "scripts" ? state.relay?.scriptLink(scripts.name)
    : state.relay?.link(state.conv ? `/c/${state.conv.id}` : "/")));
  dom.settings.addEventListener("click", () => chrome.runtime.openOptionsPage());
  dom.title.addEventListener("click", () => toggleHistory());
  dom.search.addEventListener("input", debounce(() => loadHistory(dom.search.value.trim()), 250));
  dom.search.addEventListener("keydown", (e) => { if (e.key === "Escape") toggleHistory(false); });
  dom.addPage.addEventListener("click", () => addPage());
  dom.addShot.addEventListener("click", () => addScreenshot());
  dom.provider.addEventListener("change", () => changeProvider(dom.provider.value));
  dom.model.addEventListener("change", () => changeModel(dom.model.value));
  dom.send.addEventListener("click", () => (state.following ? stop() : send()));
  dom.input.addEventListener("input", autosize);
  dom.input.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || e.isComposing) return;
    const enterSends = state.relay?.settings.enterSends !== false;
    if (e.ctrlKey || e.metaKey || (enterSends && !e.shiftKey)) {
      e.preventDefault();
      send();
    }
  });
  dom.thread.addEventListener("scroll", () => {
    const { scrollHeight, scrollTop, clientHeight } = dom.thread;
    state.stick = scrollHeight - scrollTop - clientHeight < 80;
  }, { passive: true });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !dom.history.hidden) toggleHistory(false);
  });
}

// Conversations

async function openConversation(id, { quiet = false } = {}) {
  stopFollowing();
  let conv;
  try {
    conv = await state.relay.conversation(id);
  } catch (error) {
    if (quiet || error.status === 404) return newChat();
    return toast(error.message);
  }
  state.conv = conv;
  state.sent = [];
  state.agent = { provider: conv.provider_id, model: conv.model || "" };
  chrome.storage.local.set({ current: conv.id });
  renderHeader();
  renderThread();
  await renderAgent();
}

async function newChat() {
  stopFollowing();
  state.conv = null;
  state.sent = [];
  chrome.storage.local.remove("current");
  renderHeader();
  renderThread();
  try {
    state.agent = await state.relay.defaultAgent(state.providers);
  } catch (error) {
    state.agent = { provider: state.providers[0]?.id || "", model: "" };
    toast(error.message);
  }
  await renderAgent();
  dom.input.focus();
}

function renderHeader() {
  const conv = state.conv;
  if (state.view === "scripts") {
    dom.titleText.textContent = "Scripts";
    dom.title.disabled = true;
    dom.open.title = "Open on Relay's Scripts page";
    return;
  }
  dom.title.disabled = false;
  dom.titleText.textContent = conv ? conv.title || "Untitled" : "New chat";
  dom.open.title = conv ? "Open this chat in Relay" : "Open Relay";
}

function renderThread() {
  dom.thread.replaceChildren();
  renderQueue();
  const conv = state.conv;
  const messages = conv?.messages || [];
  if (!messages.length && !conv?.running_run) {
    dom.thread.append(emptyState());
    return;
  }
  for (const message of messages) {
    if (message.role === "user") dom.thread.append(userMessage(message.content || "", message.attachments));
    else if (message.role === "assistant" && message.run_id
      !== conv.running_run) dom.thread.append(AnswerView.stored(message).el);
  }
  state.stick = true;
  if (conv.running_run) follow(conv.running_run);
  scrollDown(true);
}

function emptyState() {
  return h("div.empty",
    h("img.empty-logo", { src: "icons/icon.svg", alt: "" }),
    h("p", "Ask your Relay agents anything, with this page, a selection or a screenshot along."),
    h("div.quick",
      h("button.btn",
        { onclick: async () => { if (await addPage({ selection: false })) send("Summarize this page."); } },
        icon("sparkle"), "Summarize this page"),
      h("button.btn", { onclick: async () => { if (await addPage()) dom.input.focus(); } },
        icon("page"), "Ask about this page")),
    h("p.hint", "Or right-click a page, a selection or a link; Alt+Shift+R opens this panel."));
}

// History

let historyToken = 0;

function toggleHistory(show = dom.history.hidden) {
  dom.history.hidden = !show;
  dom.title.classList.toggle("open", show);
  if (show) {
    dom.search.value = "";
    loadHistory();
    dom.search.focus();
  }
}

async function loadHistory(query = "") {
  const token = ++historyToken;
  let rows;
  try {
    rows = await state.relay.conversations(query);
  } catch (error) {
    dom.historyList.replaceChildren(h("div.faint", error.message));
    return;
  }
  if (token !== historyToken) return;
  rows = Array.isArray(rows) ? rows : rows?.conversations || [];
  dom.historyList.replaceChildren(...rows.slice(0, 60).map((row) => {
    const running = Boolean(row.running_run || row.last_run?.status === "running");
    const when =
      ago(row.updated_at ?? row.updated ?? row.last_activity ?? row.last_message_at ?? row.created_at ?? row.created);
    return h(`button.history-row${row.id === state.conv?.id ? ".current" : ""}`,
      { onclick: () => { toggleHistory(false); openConversation(row.id); } },
      h("span.history-title", running ? h("span.dot", { title: "Working" }) : null, row.title || "Untitled"),
      h("span.history-meta", [row.mode === "code" ? "code task" : null, when].filter(Boolean).join(" · ")));
  }));
  if (!rows.length) dom.historyList.append(h("div.faint", query ? "Nothing found." : "No conversations yet."));
}

// The agent

let modelsToken = 0;

async function renderAgent() {
  const { provider } = state.agent;
  const options = state.providers.map((p) => h("option", { value: p.id }, p.name || p.id));
  if (provider
    && !state.providers.some((p) => p.id === provider)) options.push(h("option", { value: provider }, provider));
  dom.provider.replaceChildren(...options);
  dom.provider.value = provider;
  await renderModels();
}

async function renderModels() {
  const token = ++modelsToken;
  const { provider, model } = state.agent;
  dom.model.replaceChildren(h("option", { value: model }, model || "Default model"));
  if (!provider) return;
  const models = await state.relay.models(provider).catch(() => []);
  if (token !== modelsToken) return;
  const list = models.some((m) => m.id === "") ? models : [{ id: "", name: "Default model" }, ...models];
  if (model && !list.some((m) => m.id === model)) list.push({ id: model, name: model });
  dom.model.replaceChildren(...list.map((m) => h("option", { value: m.id }, m.name || m.id || "Default model")));
  dom.model.value = model;
}

async function changeProvider(provider) {
  state.agent = { provider, model: "" };
  if (state.conv) {
    // (the conversation is handed over to that agent, with its default model, as on Relay's own pages)
    try {
      await state.relay.change(state.conv.id, { provider_id: provider, model: null, effort: "default" });
    } catch (error) {
      toast(error.message);
    }
  }
  await renderModels();
}

async function changeModel(model) {
  state.agent.model = model;
  if (state.conv) await state.relay.change(state.conv.id, { model }).catch((error) => toast(error.message));
}

// What goes along

function addContext(context) {
  const same = (c) => c.kind === context.kind && c.url === context.url && c.text
    === context.text && c.kind !== "screenshot";
  if (state.contexts.some(same)) return;
  state.contexts.push(context);
  renderContexts();
}

function renderContexts() {
  dom.contexts.replaceChildren(...state.contexts.map((context, i) => {
    const label = context.kind === "page" ? context.title || context.url
      : context.kind === "selection" ? `“${clip(context.text, 48)}”`
        : context.kind === "link" ? context.text || context.url
          : "Screenshot";
    const thumb = context.kind === "screenshot"
      ? h("img.thumb", { src: context.preview || (context.preview = URL.createObjectURL(context.blob)), alt: "" })
      : icon(KIND_ICON[context.kind] || "page", 14);
    return h("span.chip", { title: context.url || "" }, thumb, h("span.chip-text", clip(label, 60)),
      h("button.chip-x",
        { title: "Don't send this",
          onclick: () => { state.contexts.splice(i, 1); renderContexts(); } }, icon("x", 12)));
  }));
  dom.input.placeholder = state.contexts.length ? "Ask about what goes along…" : "Ask Relay…";
}

// The active tab's page (and its selection, `selection`); whether it could be read.
async function addPage({ selection = true } = {}) {
  hideNotice();
  const tab = await activeTab();
  if (!tab) return false;
  try {
    const page = await readTab(tab.id, state.relay.settings.pageLimit);
    addContext({ kind: "page", url: page.url, title: page.title, description: page.description, text: page.text,
      truncated: page.truncated });
    // (a selection still standing in the tab after it went along once doesn't go again with each page)
    if (selection && page.selection && !goneAlong(page.selection)) {
      addContext({ kind: "selection", text: page.selection, url: page.url, title: page.title });
    }
    return true;
  } catch (error) {
    blocked(error, () => addPage({ selection }));
    return false;
  }
}

// Whether this selection went along to this chat already (or waits in its queue to).
function goneAlong(selection) {
  const tag = `\n${selection}\n</selection>`;
  const prompts = [...(state.conv?.messages || []).filter((m) => m.role === "user").map((m) => m.content),
    ...(state.conv?.queued || []).map((q) => q.content), ...state.sent];
  return prompts.some((prompt) => typeof prompt === "string" && prompt.includes(tag));
}

async function addScreenshot() {
  hideNotice();
  const tab = await activeTab();
  if (!tab) return;
  try {
    const blob = await screenshot(tab.windowId);
    const stamp = new Date().toISOString().slice(0, 19).replace(/[T:]/g, "-");
    addContext({ kind: "screenshot", blob, name: `screenshot-${stamp}.png` });
  } catch (error) {
    blocked(error, addScreenshot);
  }
}

function blocked(error, retry) {
  if (error.never) {
    notice("Chrome doesn't let extensions read this page (one of Chrome's own, or the Web Store).");
  } else if (error.blocked) {
    notice("Relay can read this page once you allow it to read pages (Chrome asks once).", "Allow", async () => {
      if (await askToReadPages()) {
        hideNotice();
        retry();
      }
    });
  } else {
    notice(error.message);
  }
}

// What the right-click menu sent (the service worker leaves it in session storage).
let taking = false;

async function takePending() {
  if (!state.ready || taking) return;
  taking = true;
  try {
    const { pending } = await chrome.storage.session.get("pending");
    if (!pending) return;
    await chrome.storage.session.remove("pending");
    if (Date.now() - pending.at > 5 * 60 * 1000) return;  // (left over from a while ago)
    if (pending.view === "scripts") {
      await setView("scripts");
      scripts.takeArgs(pending.args || "");
      return;
    }
    if (state.view !== "chat") await setView("chat");
    toggleHistory(false);
    if (pending.problem) {
      notice(pending.problem);
      return;
    }
    if (pending.fresh && (state.conv || state.following)) await newChat();
    if (pending.rewrite) {  // (only the field's text goes along with a rewrite)
      state.contexts = [];
      renderContexts();
    }
    for (const context of pending.contexts || []) addContext(context);
    state.rewrite = pending.rewrite || null;
    if (pending.draft) {
      dom.input.value = pending.draft;
      autosize();
    }
    if (pending.send) await send(pending.send);
    else dom.input.focus();
  } finally {
    taking = false;
  }
}

// Talking

async function send(asked = null) {
  if (state.sending || !state.relay) return;
  const text = (asked ?? dom.input.value).trim();
  if (!text) {
    dom.input.focus();
    return;
  }
  if (state.following) {
    state.rewrite = null;  // (its answer isn't known yet: it goes back into no field)
    return queue(text, asked === null);
  }
  state.sending = true;
  setRunning(true);
  hideNotice();
  const contexts = state.contexts;
  try {
    if (!state.conv) {
      // (named by what was asked, as Relay names a chat by its first message, without what went along from the browser)
      const title = contexts.some((c) => c.kind !== "screenshot") ? clip(text, 60) : null;
      const made = await state.relay.create({ ...state.agent, title });
      state.conv = await state.relay.conversation(made.id);
      chrome.storage.local.set({ current: made.id });
      renderHeader();
      dom.thread.replaceChildren();  // (the empty state goes)
    }
    const shots = contexts.filter((c) => c.kind === "screenshot");
    const attachments = [];
    for (const shot of shots) attachments.push(shot.upload
      || (shot.upload = await state.relay.upload(shot.blob, shot.name)));
    const rewrite = state.rewrite;
    const prompt = compose(rewrite && !text.includes(ONLY.trim()) ? text + ONLY : text, contexts);
    const { run_id: runId } = await state.relay.send(state.conv.id, prompt, attachments);
    if (rewrite) {
      state.rewrite = null;
      rewrites[runId] = rewrite;
      chrome.storage.session.set({ rewrites });
    }
    state.sent.push(prompt);
    dom.thread.append(userMessage(prompt, shots.map((s) => ({ name: s.name, preview: s.preview }))));
    if (asked === null) dom.input.value = "";
    autosize();
    state.contexts = [];
    renderContexts();
    state.sending = false;
    state.stick = true;
    follow(runId);
    scrollDown(true);
  } catch (error) {
    state.sending = false;
    setRunning(false);
    toast(error.message);
  }
}

// Sent while the agent still answers: Relay sends it once that answer is done (its queue, as on Relay's pages).
async function queue(text, typed) {
  state.sending = true;
  const contexts = state.contexts;
  try {
    const shots = contexts.filter((c) => c.kind === "screenshot");
    const attachments = [];
    for (const shot of shots) attachments.push(shot.upload
      || (shot.upload = await state.relay.upload(shot.blob, shot.name)));
    const prompt = compose(text, contexts);
    const result = await state.relay.queue(state.conv.id, prompt, attachments);
    if (typed) dom.input.value = "";
    autosize();
    state.contexts = [];
    renderContexts();
    if (result?.run_id) {
      state.sending = false;
      await openConversation(state.conv.id);  // (that answer had just ended: it went right away)
      return;
    }
    state.conv.queued = [...(state.conv.queued || []), { id: result?.queued, content: prompt }];
    renderQueue();
  } catch (error) {
    toast(error.message);
  } finally {
    state.sending = false;
  }
}

// The messages that go once the answer now is done; after one that was stopped or failed they wait for you
// (Relay sends them after the next answer that ends well), as on Relay's own pages.
function renderQueue() {
  const idle = !state.following;
  dom.queue.replaceChildren(...(state.conv?.queued || []).map((q) => {
    const { asked, parts } = parsePrompt(q.content || "");
    return h("div.queued-item",
      h("span.faint", idle ? "Waiting:" : "Next:"),
      h("span.queued-text", { title: asked }, asked || "See the attached files."),
      parts.map((part) => Object.assign(icon(KIND_ICON[part.kind], 12), { title: part.label })),
      idle ? h("button.btn.small", { onclick: () => sendQueued(q) }, "Send now") : null,
      h("button.chip-x", { title: "Don't send", onclick: () => unqueue(q) }, icon("x", 12)));
  }));
}

async function sendQueued(q) {
  try {
    await state.relay.sendQueued(state.conv.id, q.id);
    await openConversation(state.conv.id);
  } catch (error) {
    toast(error.message);
  }
}

async function unqueue(q) {
  try {
    await state.relay.unqueue(state.conv.id, q.id);
    state.conv.queued = (state.conv.queued || []).filter((other) => other.id !== q.id);
    renderQueue();
  } catch (error) {
    toast(error.message);
  }
}

async function follow(runId) {
  stopFollowing();
  const view = new AnswerView({ live: true, runId });
  dom.thread.append(view.el);
  const abort = new AbortController();
  const following = state.following = { runId, abort, view };
  setRunning(true);
  renderQueue();
  try {
    for await (const event of state.relay.events(runId, abort.signal)) view.add(event);
  } catch (error) {
    if (!abort.signal.aborted) view.error(error.message);
  }
  if (state.following !== following) return;  // (another conversation was opened meanwhile)
  state.following = null;
  setRunning(false);
  // Relay's own copy of the answer: how it ended, what it cost, and the title Relay gave the chat meanwhile.
  const fresh = await state.relay.conversation(state.conv.id).catch(() => null);
  if (!fresh || fresh.id !== state.conv?.id) {
    if (!view.status) view.finish("failed");
    renderQueue();
    return;
  }
  state.conv = fresh;
  renderHeader();
  renderQueue();
  const stored = fresh.messages?.find((m) => m.run_id === runId && m.role === "assistant");
  if (stored?.error && stored.status === "failed") view.error(stored.error);
  if (!view.status) view.finish(stored?.status || "failed");
  if (stored) view.footer(stored);
  // (a queued message went next, or a fallback agent)
  if (fresh.running_run && fresh.running_run !== runId) renderThread();
}

function stopFollowing() {
  if (!state.following) return;
  state.following.abort.abort();
  state.following = null;
  setRunning(false);
}

async function stop() {
  const runId = state.following?.runId;
  if (!runId) return;
  dom.send.disabled = true;
  try {
    await state.relay.stop(runId);
  } catch (error) {
    toast(error.message);
  } finally {
    dom.send.disabled = false;
  }
}

function setRunning(running) {
  dom.send.classList.toggle("stop", running);
  dom.send.title = running ? "Stop" : "Send";
  dom.send.replaceChildren(icon(running ? "stop" : "send"));
  dom.send.disabled = state.sending;
}

// Messages

// A message as compose made it: what was asked, and what went along from the browser (compose's tags).
function parsePrompt(text) {
  const at = text.indexOf(BROWSER);
  const asked = (at < 0 ? text : text.slice(0, at)).trim();
  const parts = [];
  if (at >= 0) {
    for (const [, kind, attrs] of text.slice(at).matchAll(/^<(page|selection|link)\b([^>]*)>/gm)) {
      const attr = (name) => unescape((attrs.match(new RegExp(`\\b${name}="([^"]*)"`)) || [])[1] || "");
      const label = kind === "page" ? attr("title") || attr("url") : kind === "selection"
        ? `Selection on ${attr("page") || attr("url")}`
        : attr("text") || attr("url");
      parts.push({ kind, label, url: attr("url") });
    }
  }
  return { asked, parts };
}

// What you asked; what went along from the browser shown as chips, not as its whole text.
function userMessage(text, attachments = []) {
  const { asked, parts } = parsePrompt(text);
  const chips = parts.map((part) => h("span.chip.small", { title: part.url }, icon(KIND_ICON[part.kind], 12),
    h("span.chip-text", clip(part.label, 50))));
  for (const file of attachments || []) {
    const name = file.name || file.filename || "Attachment";
    chips.push(h("span.chip.small", file.preview ? h("img.thumb", { src: file.preview, alt: "" }) : icon("image", 12),
      h("span.chip-text", clip(name, 40))));
  }
  return h("div.msg.user", h("div.bubble", asked.trim()), chips.length ? h("div.chips", chips) : null);
}

// One answer of an agent, fed its events as they come (or as Relay kept them).
class AnswerView {
  constructor({ live = false, runId = null } = {}) {
    this.live = live;
    this.runId = runId;
    this.body = h("div.body");
    this.foot = h("div.foot");
    this.el = h("div.msg.assistant", this.body, this.foot);
    this.block = null;    // the text that deltas go to now
    this.group = null;    // the tools called since the last text
    this.tools = new Map();
    this.permissions = new Map();
    this.text = "";
    this.status = null;
    this.usage = null;
    this.model = null;
    this.working = null;
    this.queued = false;
    if (live) this.showWorking("Thinking…");
  }

  static stored(message) {
    const view = new AnswerView({ runId: message.run_id });
    for (const event of message.events || []) view.add(event);
    if (!view.text.trim() && message.content) view.appendText(message.content);
    view.renderBlock();
    if (message.status === "failed" && message.error) view.error(message.error);
    if (!view.status) view.finish(message.status || "succeeded");
    view.footer(message);
    return view;
  }

  add(event) {
    switch (event.type) {
      case "step": this.model = event.model || this.model; break;
      case "text": this.appendText(event.delta || ""); break;
      case "reasoning": if (!this.block) this.showWorking("Thinking…"); break;
      case "tool_call": this.toolCall(event); break;
      case "tool_result": this.toolResult(event); break;
      case "permission": this.permission(event); break;
      case "form": this.form(event); break;
      case "usage": this.usage = event; break;
      case "error": this.error(event.message || "Something went wrong"); break;
      case "done": this.finish(event.status); break;
      default: break;
    }
    if (this.live) scrollDown();
  }

  showWorking(label) {
    if (!this.live || this.status) return;
    if (!this.working) this.working = h("div.working", h("span.spinner"), h("span"));
    this.working.lastChild.textContent = label;
    this.body.append(this.working);
  }

  hideWorking() {
    this.working?.remove();
  }

  appendText(delta) {
    if (!delta) return;
    this.hideWorking();
    this.group = null;
    if (!this.block) {
      this.block = { el: h("div.md"), text: "" };
      this.body.append(this.block.el);
    }
    this.block.text += delta;
    this.text += delta;
    if (!this.live) return;
    if (this.queued) return;
    this.queued = true;
    requestAnimationFrame(() => {
      this.queued = false;
      this.renderBlock();
      scrollDown();
    });
  }

  renderBlock() {
    if (this.block) renderMarkdown(this.block.el, this.block.text);
  }

  finishBlock() {
    this.renderBlock();
    this.block = null;
  }

  toolCall(event) {
    this.finishBlock();
    if (!this.group) {
      this.group = h("div.tools");
      this.body.append(this.group);
    }
    const chip =
      h("span.tool.running", { title: describe(event.input) }, icon("tool", 12), h("span", toolLabel(event)));
    this.tools.set(event.id, chip);
    this.group.append(chip);
    this.showWorking("Working…");
  }

  toolResult(event) {
    const chip = this.tools.get(event.id);
    if (!chip) return;
    chip.classList.remove("running");
    chip.classList.add(event.ok === false ? "failed" : "ok");
    if (event.ok === false && event.output) chip.title = `${chip.title}\n\n${clip(String(event.output), 600)}`;
  }

  // A permission the agent asks: answered automatically (not shown), or with the buttons here; a later event
  // with the same id says how it was answered.
  permission(event) {
    if (event.decision === "once" && !this.permissions.has(event.id)) return;
    this.finishBlock();
    this.group = null;
    let note = this.permissions.get(event.id);
    if (!note) {
      note = h("div.note");
      this.permissions.set(event.id, note);
      this.body.append(note);
    }
    const what = Array.isArray(event.resources) ? event.resources.join("\n") : describe(event.resources);
    const action = event.subagent ? `${event.subagent}: ${event.action}` : event.action || "Permission";
    if (event.decision === null || event.decision === undefined) {
      note.dataset.asked = "1";
      const runId = event.run || this.runId;
      const answer = async (decision) => {
        for (const button of note.querySelectorAll("button")) button.disabled = true;
        try {
          await state.relay.answerPermission(runId, event.id, decision);
        } catch (error) {
          toast(error.message);
          for (const button of note.querySelectorAll("button")) button.disabled = false;
        }
      };
      const buttons = event.options?.length
        ? event.options.map((o, i) =>
            h(`button.btn.small${i === 0 ? ".primary" : ""}`,
              { onclick: () => answer(o.id) }, o.label || o.name || o.id))
        : [h("button.btn.small", { onclick: () => answer("deny") }, "Deny"),
          h("button.btn.small.primary", { onclick: () => answer("allow") }, "Allow")];
      const waiting = this.live && !this.status;
      note.className = "note ask";
      fill(note, h("div.note-title", action), what ? h("pre.note-detail", what) : null,
        waiting ? h("div.note-buttons", buttons) : h("div.faint", "Not answered"));
      if (waiting) this.showWorking("Waiting for your approval…");
      return;
    }
    const asked = note.dataset.asked === "1";
    const ok = !["denied", "timeout"].includes(event.decision);
    const said = event.outcome
      || (event.decision === "once" ? (asked ? "allowed" : "approved automatically") : "denied");
    note.className = `note ${ok ? "ok" : "no"}`;
    fill(note, h("div.note-title", `${action}: ${said}`), what ? h("pre.note-detail", what) : null);
    if (this.live && !this.status) this.showWorking("Working…");
  }

  // A form the agent asks to be filled in: that's done on Relay's own page.
  form(event) {
    this.finishBlock();
    this.group = null;
    const pending = event.status === "pending" && this.live && !this.status;
    const note =
      h(`div.note${pending ? ".ask" : ""}`, h("div.note-title", event.title || "The agent asks you something"),
        pending ? h("div.note-buttons", h("button.btn.small.primary",
          { onclick: () => openTab(state.relay.link(`/c/${state.conv?.id}`)) }, "Answer in Relay")) : null);
    this.body.append(note);
    if (pending) this.showWorking("Waiting for your answer…");
  }

  error(message) {
    this.finishBlock();
    this.hideWorking();
    if ([...this.body.querySelectorAll(".error")].some((e) => e.textContent === message)) return;
    this.body.append(h("div.error", icon("alert", 14), h("span", message)));
  }

  finish(status) {
    this.status = status || "succeeded";
    this.finishBlock();
    this.hideWorking();
    for (const chip of this.tools.values()) chip.classList.remove("running");
    if (this.status === "interrupted") this.body.append(h("div.faint", "Stopped."));
    if (this.live && !this.body.childElementCount) this.body.append(h("div.faint", "No answer."));
  }

  footer(message = {}) {
    const parts = [];
    const model = message.run_model || this.model;
    if (model) parts.push(model);
    if (message.run_started && message.run_finished) parts.push(duration(message.run_finished - message.run_started));
    const usage = [...(message.events || [])].reverse().find((e) => e.type === "usage") || this.usage;
    if (usage?.cost) parts.push(`$${Number(usage.cost).toFixed(4)}`);
    const text = this.text.trim();
    const into = rewrites[this.runId];
    fill(this.foot, parts.length ? h("span", parts.join(" · ")) : null,
      text ? h("button.link", { onclick: (e) => copy(e.currentTarget, text) }, "Copy") : null,
      text ? h("button.link", { title: "Where your cursor is in the page, as plain text",
        onclick: (e) => write(e.currentTarget, "insert", this) }, "Insert in page") : null,
      text && into ? h("button.link", { title: "Back into the field it came from", disabled: Boolean(into.done),
        onclick: (e) => write(e.currentTarget, "replace", this, into) },
        into.done ? "Replaced" : "Replace in field") : null);
  }

  // The answer as plain text, as it reads: its paragraphs and lines, without Markdown's signs.
  plain() {
    const parts = [...this.body.querySelectorAll(".md")].map((el) => el.innerText.trim()).filter(Boolean);
    return parts.join("\n\n") || this.text.trim();
  }
}

// An answer written into the page: inserted where the cursor is (in the tab in front), or put back into the field
// it was asked about. When Chrome doesn't let the extension use the page yet, it asks (in the notice's click).
async function write(button, how, view, into = null) {
  if (into?.done) return;
  const label = button.textContent;
  try {
    let did;
    if (how === "replace") {
      ({ did } = await inPage(into.tabId, "replace", [into.target, view.plain()], into.frameId));
      into.done = true;
      chrome.storage.session.set({ rewrites });
    } else {
      const tab = await activeTab();
      if (!tab) throw new Error("No page is open in this window");
      ({ did } = await inPage(tab.id, "insert", [view.plain()]));
    }
    button.textContent = how === "replace" ? "Replaced" : "Inserted";
    if (how === "replace") button.disabled = true;
    else setTimeout(() => { button.textContent = label; }, 1500);
    toast(`${did}.`);
  } catch (error) {
    if (/allowed it to use web pages/.test(error.message)) {
      notice("Chrome doesn't let the extension write in that page yet.", "Allow", async () => {
        hideNotice();
        if (await askToReadPages()) write(button, how, view, into);
      });
    } else {
      toast(error.message);
    }
  }
}

function toolLabel(event) {
  const input = event.input && typeof event.input === "object" ? event.input : {};
  const detail = input.command || input.query || input.url || input.file_path || input.path
    || input.pattern || input.description || "";
  return detail ? `${event.name}: ${clip(String(detail), 48)}` : String(event.name || "tool");
}

function describe(value) {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

// Little things

function scrollDown(force = false) {
  if (force) state.stick = true;
  if (state.stick) dom.thread.scrollTop = dom.thread.scrollHeight;
}

function autosize() {
  dom.input.style.height = "auto";
  dom.input.style.height = `${Math.min(dom.input.scrollHeight, 220)}px`;
}

function notice(text, action = null, onclick = null) {
  fill(dom.notice, h("span", text), action ? h("button.btn.small.primary", { onclick }, action) : null,
    h("button.chip-x", { title: "Close", onclick: hideNotice }, icon("x", 12)));
  dom.notice.hidden = false;
}

function hideNotice() {
  dom.notice.hidden = true;
}

let toastTimer = null;

function toast(text) {
  dom.toast.textContent = text;
  dom.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { dom.toast.hidden = true; }, 5000);
}

async function copy(button, text) {
  await navigator.clipboard.writeText(text);
  button.textContent = "Copied";
  setTimeout(() => { button.textContent = "Copy"; }, 1500);
}

function openTab(url) {
  if (url) chrome.tabs.create({ url });
}

function clip(text, n) {
  const flat = String(text || "").replace(/\s+/g, " ").trim();
  return flat.length > n ? `${flat.slice(0, n - 1)}…` : flat;
}

function duration(seconds) {
  if (!Number.isFinite(seconds)) return "";
  if (seconds < 60) return `${Math.max(1, Math.round(seconds))} s`;
  const minutes = Math.floor(seconds / 60);
  return minutes < 60 ? `${minutes} min ${Math.round(seconds % 60)} s`
    : `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}

function debounce(fn, ms) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

init();
