// Rendering of messages: user bubbles and assistant turns built from events.

import { relayUrl, storeKey } from "./base.js";
import { renderMarkdown } from "./markdown.js";
import { FormCard } from "./forms.js";
import { reviewLocation } from "./outcomes.js";
import { ToolActivity, toolKind } from "./tool-activity.js";
import { copyText, h, icon } from "./ui.js";

export function userMessage(text, attachments = null, created = Date.now() / 1000) {
  return h("div.msg.user",
    attachments,
    text ? h("div.bubble", text) : null,
    h("div.msg-actions", messageTime(created),
      h("button.icon-btn.sm.copy-action",
        { title: "Copy", "aria-label": "Copy", onclick: () => copyText(text) }, icon("copy", "sm"))));
}

// When a message was sent: the time today, the day this week, the date before that; in full on hover.
export function messageTime(seconds) {
  if (!seconds) return null;
  const when = new Date(seconds * 1000);
  const now = new Date();
  const time = when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const days = (new Date(now.toDateString()) - new Date(when.toDateString())) / 86400000;
  const label = days === 0 ? time
    : days === 1 ? `Yesterday ${time}`
      : days < 7 ? `${when.toLocaleDateString([], { weekday: "short" })} ${time}`
        : `${when.toLocaleDateString([], { day: "numeric", month: "short",
          ...(when.getFullYear() === now.getFullYear() ? {} : { year: "numeric" }) })} ${time}`;
  return h("time.msg-time", { dateTime: when.toISOString(), title: when.toLocaleString([], { dateStyle: "full",
    timeStyle: "short" }) }, label);
}

const TOOL_ICONS = [
  [/shell|bash|exec|command|terminal/i, "terminal"],
  [/read|write|edit|patch|glob|grep|file|ls|list/i, "file"],
  [/web|fetch|search|url/i, "globe"],
];

function toolIcon(name) {
  const match = TOOL_ICONS.find(([pattern]) => pattern.test(name || ""));
  return match ? match[1] : "tool";
}

function toolSummary(input) {
  if (input === null || input === undefined) return "";
  if (typeof input !== "object") return String(input);
  // A search says most by what it looks for, and where only when that isn't the project itself.
  if (typeof input.pattern === "string" && input.pattern) {
    const where = typeof input.path === "string" ? shortenPaths(input.path) : "";
    return where && where !== "." ? `${input.pattern}  in ${where}` : input.pattern;
  }
  for (const key of ["command", "cmd", "file_path", "filePath", "path", "url", "query",
    "description", "prompt"]) {
    if (typeof input[key] === "string" && input[key]) return shortenPaths(shortenRelayTools(input[key]));
  }
  return JSON.stringify(input);
}

export function toolCaption(input) {
  if (input && typeof input === "object" && !Object.keys(input).length) return "";
  const description = typeof input?.description === "string" ? input.description.trim() : "";
  return shortenPaths(shortenRelayTools(description || toolSummary(input)));
}

export function toolFailure(output) {
  if (output && typeof output === "object") {
    const message = typeof output.error === "string" ? output.error : output.error?.message;
    if (typeof message === "string" && message.trim()) return toolFailure(message);
    if (typeof output.message === "string" && output.message.trim()) return toolFailure(output.message);
    const code = output.exit_code ?? output.exitCode;
    if (Number.isInteger(code)) return `Exit code ${code}`;
    if (typeof output.stderr === "string" && output.stderr.trim()) return toolFailure(output.stderr);
    return "See Output for details.";
  }
  if (output === null || output === undefined || output === "") return "No error details were provided.";
  // Use reported diagnostics only; keep the complete, unmodified output in the body.
  const lines = String(output).slice(0, 8192).replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, "")
    .split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const exit =
    lines.map((line) =>
      line.match(/^(?:Exit code|(?:Process )?exited with (?:exit )?code)\s*:?\s*(-?\d+)\b/i)).find(Boolean);
  if (exit) return `Exit code ${exit[1]}`;
  const reason =
    lines.find((line) =>
      /^(?:error\b|fatal\b|\w*Error:|file (?:does not exist|not found)\b|permission denied\b)/i.test(line))
    || lines[0] || "No error details were provided.";
  return shortenPaths(shortenRelayTools(reason.replace(/\s+Note:.*$/i, "")));
}

// The open chat's folder: paths in it show relative to it (tests/x.py, not /home/…/project/tests/x.py).
let projectFolder = null;

export function setProjectFolder(folder) {
  projectFolder = folder ? folder.replace(/\/+$/, "") : null;
}

function shortenPaths(text) {
  if (!projectFolder) return text;
  const escaped = projectFolder.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return text
    // going where it is anyway
    .replace(new RegExp(`(^|[;\\n]\\s*)cd ['"]?${escaped}['"]?\\s*(?:&&|;|\\n)\\s*`, "g"), "$1")
    .replace(new RegExp(`${escaped}/`, "g"), "")
    .replace(new RegExp(`${escaped}(?![\\w./-])`, "g"), ".");
}

// Relay's tools script lives at a long path with a secret in it; show it as "relay",
// also when the agent put it in a variable first (R=…/relay; $R wait → relay wait).
const TOOLS_PATH = /['"]?\/[^\s'"]*\/(?:relay-)?tools\/[\w-]+\/relay\b['"]?/;

function shortenRelayTools(text) {
  let shown = text;
  const assigned = new RegExp(`(?:^|[\\s;&])(?:export\\s+)?(\\w+)=${TOOLS_PATH.source}\\s*(?:;|&&|\\n)?\\s*`, "g");
  for (const [whole, name] of [...text.matchAll(assigned)]) {
    shown = shown.replace(whole.trimStart(), "")
      .replace(new RegExp(`["']?\\$\\{?${name}\\}?["']?`, "g"), "relay");
  }
  return shown.replace(new RegExp(TOOLS_PATH.source, "g"), "relay");
}

function pretty(value) {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  return JSON.stringify(value, null, 2);
}

// Input tokens, those read from or written to a prompt cache included (Claude and opencode
// count them apart; OpenAI's prompt_tokens includes them).
export function inputTokens(tokens) {
  const cache = tokens.cache || {};
  return (tokens.input_tokens ?? tokens.input ?? tokens.prompt_tokens ?? 0)
    + (tokens.cache_read_input_tokens || 0) + (tokens.cache_creation_input_tokens || 0)
    + (cache.read || 0) + (cache.write || 0);
}

export function usageText(tokens) {
  if (!tokens) return "";
  const input = inputTokens(tokens);
  const output = tokens.output_tokens ?? tokens.output ?? tokens.completion_tokens;
  const parts = [];
  if (input) parts.push(`${input.toLocaleString()} in`);
  if (output) parts.push(`${output.toLocaleString()} out`);
  return parts.join(" · ");
}

function dollars(cost) {
  return `$${Number(cost).toFixed(cost < 0.01 ? 4 : 2)}`;
}

// What a turn cost, telling pay-per-use API costs apart from a flat-rate plan:
// { label, title, kind } or null. See billing.py for the kinds.
export function costLabel(cost, billing) {
  if (billing === "subscription") {
    return { kind: "plan", label: "in plan", title: "Covered by your subscription: no extra cost, "
      + "but it counts toward the plan's usage limits."
      + (cost ? ` At API prices this would have cost about ${dollars(cost)}.` : "") };
  }
  if (billing === "free") {
    return { kind: "free", label: "free", title: "A local or free model: nothing is charged." };
  }
  if (billing === "api") {
    return cost
      ? { kind: "api", label: `${dollars(cost)} API`, title: "Pay per use: charged to your API account." }
      : { kind: "api", label: "API", title: "Pay per use; this provider doesn't report the price." };
  }
  return cost ? { kind: "unknown", label: dollars(cost), title: "Cost reported by the provider." } : null;
}

// Relay's delegate tool shows up as subagent cards instead of a tool card.
function isDelegateTool(name) {
  return /(^|__)relay__delegate$/.test(name || "");
}

// A shell call of Relay's tools script that starts or waits for subagents.
function isSubagentCommand(input) {
  const command = typeof input?.command === "string" ? input.command : "";
  return /\/(?:relay-)?tools\/[\w-]+\/relay'?\s+(delegate|wait)\b/.test(command);
}

// "Permission for shell: mkdir -p app … — approved automatically": one line,
// with the rest (a long command, a whole file) behind a toggle.
function permissionLine(action, what, decision) {
  const text = String(what || "");
  const short = firstLine(text, 100);
  const line = `Permission for ${action}${short ? `: ${short}` : ""} — ${decision}`;
  if (short === text.trim()) return [line];
  return [h("details.permission-more", h("summary", line), h("pre", text))];
}

function firstLine(text, max = 80) {
  const lines = (text || "").trim().split("\n");
  const line = lines[0];
  if (line.length > max) return `${line.slice(0, max - 1)}…`;
  return lines.length > 1 ? `${line} …` : line;
}

let providerNames = (id) => id;

// /thinking and /details: expanded or collapsed, remembered in this browser.
function remembered(key) {
  try { return localStorage.getItem(storeKey(key)) === "show"; } catch { return false; }
}
function remember(key, show) {
  try { localStorage.setItem(storeKey(key), show ? "show" : "hide"); } catch { /* private mode */ }
}
let thinkingShown = remembered("relay.thinking");
let detailsShown = remembered("relay.details");

export function showingThinking() {
  return thinkingShown;
}

export function setShowThinking(show) {
  thinkingShown = show;
  remember("relay.thinking", show);
  document.querySelectorAll("details.reasoning").forEach((el) => { el.open = show; });
}

export function showingDetails() {
  return detailsShown;
}

let nextToolBody = 0;

function setToolOpen(el, open) {
  el.classList.toggle("open", open);
  el.querySelector(".tool-head").setAttribute("aria-expanded", String(open));
  const body = el.querySelector(".tool-body");
  body.inert = !open;
  body.setAttribute("aria-hidden", String(!open));
}

export function setShowDetails(show) {
  detailsShown = show;
  remember("relay.details", show);
  document.querySelectorAll(".tool:not(.subagent)").forEach((el) => setToolOpen(el, show));
  document.querySelectorAll("details.tool-activity").forEach((el) => { el.open = show; });
}
let answerPermission = null;
let answerForm = null;

// Subagent cards streaming their subagent's work. Each stream holds one of the few
// connections a browser keeps per server (six): they close when a card closes and when
// the chat is left, or new streams (the chat's own) would wait for a free one.
const watching = new Set();

function stopWatching(card) {
  card.source?.close();
  card.source = null;
  card.shownRun = null;  // opening it again streams it again
  watching.delete(card);
}

export function stopWatchingSubagents() {
  for (const card of [...watching]) stopWatching(card);
}

// Lets the app answer permission requests: (runId, requestId, "allow" | "deny", note).
export function setPermissionHandler(handler) {
  answerPermission = handler;
}

export function setFormHandler(handler) {
  answerForm = handler;
}

// Lets the app say how providers are called.
export function setProviderNames(lookup) {
  providerNames = lookup;
}

// A mode's name for a subagent card ("Read-only" rather than "readonly").
let modeName = (id) => id;

export function setModeNames(lookup) {
  modeName = lookup;
}

function subagentProvider(id) {
  return id ? providerNames(id) : null;
}

const CONTAINERS = new Set(["UL", "OL", "LI", "BLOCKQUOTE"]);
const INLINE_HOSTS = new Set(["P", "LI", "H1", "H2", "H3", "H4", "H5", "H6", "TD", "TH"]);

// Put the blinking cursor at the end of the last line of text, not below it.
function placeCursor(root) {
  let target = root.lastElementChild;
  while (target && CONTAINERS.has(target.tagName) && target.lastElementChild) {
    target = target.lastElementChild;
  }
  const host = target && INLINE_HOSTS.has(target.tagName) ? target : root;
  host.append(h("span.cursor-mark"));
}

export function thoughtLabel(milliseconds) {
  if (!Number.isFinite(milliseconds)) return "Thought";
  if (milliseconds < 1000) return "Thought for <1s";
  const seconds = Math.round(milliseconds / 1000);
  const pad = (value) => String(value).padStart(2, "0");
  const duration = seconds < 60 ? `${seconds}s` : seconds < 3600
    ? `${Math.floor(seconds / 60)}m ${pad(seconds % 60)}s`
    : `${Math.floor(seconds / 3600)}h ${pad(Math.floor(seconds / 60) % 60)}m ${pad(seconds % 60)}s`;
  return `Thought for ${duration}`;
}

// One assistant turn. Feed it events with add(); works live and for replays.
export class AssistantView {
  constructor({ live = false, runId = null, onActivity = null } = {}) {
    this.live = live;
    this.runId = runId;  // to answer permission requests
    this.onActivity = onActivity;
    this.activity = null;
    this.permissions = new Map();
    this.forms = new Map();
    this.body = h("div.body");
    this.actions = h("div.msg-actions");
    this.el = h("div.msg.assistant", this.body, this.actions);
    this.block = null; // the block text/reasoning deltas currently go to
    this.tools = new Map();
    this.subagents = new Map();
    this.hiddenTools = new Set();
    this.text = "";
    this.model = null;
    this.usage = null;
    this.status = null;
    this.renderQueued = false;
    this.working = null;
    this.created = null;  // when the answer was given (a replay's message says; live: when it ends)
    if (live) this.showWorking("Thinking…");
  }

  add(event) {
    if (this.block?.kind === "reasoning" && Number.isFinite(event.reasoning_ms)) {
      this.block.duration = Math.max(0, event.reasoning_ms);
    }
    switch (event.type) {
      case "step":
        this.model = event.model || this.model;
        break;
      case "text":
        this.appendText(event.delta || "");
        break;
      case "reasoning":
        this.appendReasoning(event.delta || "", event.reasoning_ms);
        break;
      case "tool_call":
        // Starting and waiting for subagents shows up as their cards instead.
        if (isDelegateTool(event.name) || isSubagentCommand(event.input)) this.hiddenTools.add(event.id);
        else this.toolCall(event);
        break;
      case "tool_result":
        if (!isDelegateTool(event.name) && !this.hiddenTools.has(event.id)) this.toolResult(event);
        break;
      case "subagent":
        this.subagent(event);
        break;
      case "permission":
        this.permission(event);
        break;
      case "form":
        this.form(event);
        break;
      case "usage":
        this.usage = event;
        break;
      case "error":
        this.error(event.message);
        break;
      case "done":
        this.finish(event.status, event.why);
        break;
      default:
        break;
    }
    if (["text", "reasoning", "tool_call", "tool_result", "subagent", "permission", "form", "error",
      "done"].includes(event.type)) {
      this.onActivity?.();
    }
  }

  // Blocks

  showWorking(label) {
    if (!this.live || this.status) return;
    if (!this.working) this.working = h("div.status-line", h("div.spinner.sm"), h("span"));
    this.working.lastChild.textContent = label;
    // (how long, after a while with nothing to show: main.js's clock reads these)
    Object.assign(this.working.dataset, { since: String(Date.now()), label });
    this.body.append(this.working);
  }

  hideWorking() {
    this.working?.remove();
  }

  appendText(delta) {
    if (!delta) return;
    this.finishActivity();
    this.hideWorking();
    if (!this.block || this.block.kind !== "text") {
      this.finishBlock();
      const el = h("div.md");
      this.block = { kind: "text", el, text: "" };
      this.body.append(el);
    }
    this.block.text += delta;
    this.text += delta;
    this.queueRender();
  }

  queueRender() {
    if (this.renderQueued) return;
    this.renderQueued = true;
    requestAnimationFrame(() => {
      this.renderQueued = false;
      this.renderBlock();
    });
  }

  // `writing`: the block goes on (the cursor at its end); false: it's done, an activity or the end came after it.
  renderBlock(writing = true) {
    const block = this.block;
    if (!block) return;
    if (block.kind === "reasoning") {
      block.text.textContent = block.content;
      return;
    }
    renderMarkdown(block.el, block.text);
    if (writing && this.live && !this.status) placeCursor(block.el);
  }

  appendReasoning(delta, duration = null) {
    if (!delta) return;
    this.finishActivity();
    this.hideWorking();
    if (!this.block || this.block.kind !== "reasoning") {
      this.finishBlock();
      const text = h("div.reasoning-text");
      const label = h("span", this.live ? "Thinking…" : "Thought");
      const el = h("details.reasoning", { open: this.live || thinkingShown },
        h("summary", icon("chevronRight", "sm chev"), icon("brain", "sm"), label), text);
      this.block = { kind: "reasoning", el, text, label, content: "", duration: null };
      this.body.append(el);
    }
    if (Number.isFinite(duration)) this.block.duration = Math.max(0, duration);
    this.block.content += delta;
    this.queueRender();
  }

  finishBlock() {
    this.renderBlock(false);
    if (this.block?.kind === "reasoning") {
      this.block.label.textContent = thoughtLabel(this.block.duration);
      this.block.el.open = thinkingShown;
    }
    this.block = null;
  }

  finishActivity() {
    this.activity?.finish(() => detailsShown);
    this.activity = null;
  }

  toolCall(event) {
    this.finishBlock();
    this.hideWorking();
    const card = this.toolCard(event.id, event.name);
    card.kind = toolKind(event.name, event.input);
    card.input = event.input;
    this.updateToolCard(card);
    const input = pretty(event.input);
    if (card.inputPre.textContent !== input) card.inputPre.textContent = input;
    card.inputSection.classList.toggle("hidden", !card.inputPre.textContent);
    card.group.refresh();
  }

  toolCard(id, name) {
    if (this.tools.has(id)) {
      const card = this.tools.get(id);
      if (name && (!card.name || card.name === "tool")) {
        card.name = name;
        card.title.textContent = name;
        card.title.title = name;
        card.symbol.replaceChildren(icon(toolIcon(name)));
      }
      return card;
    }
    const state = h("span.state");
    const symbol = h("span.tool-symbol", icon(toolIcon(name)));
    const title = h("span.name", { title: name || "Tool" }, name || "Tool");
    const summary = h("span.summary");
    const separator = h("span.tool-separator", { hidden: true, "aria-hidden": "true" }, "·");
    const error = h("span.tool-error", { hidden: true });
    const inputPre = h("pre");
    const outputPre = h("pre");
    const inputSection = h("div.section.hidden", h("div.label", "Input"), inputPre);  // until there is input
    const outputSection = h("div.section.hidden", h("div.label", "Output"), outputPre);
    const bodyId = `tool-body-${++nextToolBody}`;
    const el = h(`div.tool${detailsShown ? ".open" : ""}`,
      h("button.tool-head", { "aria-expanded": String(detailsShown), "aria-controls": bodyId,
        onclick: () => setToolOpen(el, !el.classList.contains("open")) },
        symbol, h("span.tool-heading", h("span.tool-title", title, separator, summary), error), state,
        icon("chevronRight", "sm chev")),
      h("div.tool-body", { id: bodyId, inert: !detailsShown, "aria-hidden": String(!detailsShown) },
        h("div.tool-body-content", inputSection, outputSection)));
    const card =
      { el, state, symbol, title, summary, separator, error, inputPre, outputPre, inputSection, outputSection,
        kind: toolKind(name), outcome: "running", name };
    this.updateToolCard(card);
    this.tools.set(id, card);
    if (!this.activity) {
      this.activity = new ToolActivity(detailsShown, this.live && !this.status);
      this.body.append(this.activity.el);
    }
    this.activity.add(card);
    return card;
  }

  updateToolCard(card) {
    const caption = toolCaption(card.input);
    const summary = firstLine(caption, 120);
    if (card.summary.textContent !== summary) card.summary.textContent = summary;
    card.summary.title = caption;
    card.summary.classList.toggle("description",
      typeof card.input?.description === "string" && !!card.input.description.trim());
    card.separator.hidden = !caption;
    card.error.hidden = card.outcome !== "failed";
    if (!card.error.hidden) {
      const reason = toolFailure(card.output);
      const preview = firstLine(reason, 120);
      if (card.error.textContent !== preview) card.error.textContent = preview;
      card.error.title = reason;
    }
    if (card.shownOutcome === card.outcome) return;
    card.shownOutcome = card.outcome;
    const label = { running: "Running", done: "Completed", failed: "Failed", stopped: "Stopped" }[card.outcome];
    const glyph = card.outcome === "running" ? (this.live && !this.status ? h("div.spinner.sm") : null)
      : icon({ done: "check", failed: "x", stopped: "stop" }[card.outcome], "sm");
    const style = card.outcome === "done" ? "ok" : card.outcome === "failed" ? "fail" : card.outcome;
    card.state.className = `state ${style}`;
    card.state.replaceChildren(...[glyph, h("span.tool-state-label", label)].filter(Boolean));
  }

  // A subagent (relay.delegation): its card updates as its events come in.
  subagent(event) {
    this.finishActivity();
    this.finishBlock();
    this.hideWorking();
    let card = this.subagents.get(event.id);
    if (!card) {
      const title = h("span.name");
      const meta = h("span.summary");
      const state = h("span.state");
      const cost = h("span.cost-slot");
      const task = h("div.subagent-task");
      const prompt = h("span.subagent-prompt");
      const alert = h("div.subagent-alert", { hidden: true });
      const thread = h("div.subagent-thread");
      const bodyId = `tool-body-${++nextToolBody}`;
      const open = h("a.subagent-open", { href: relayUrl(`/c/${event.id}`), onclick: (e) => {
        e.preventDefault();
        window.dispatchEvent(new CustomEvent("relay:navigate", { detail: `/c/${event.id}` }));
      } }, "Open as a chat");
      const el = h("div.tool.subagent",
        h("button.tool-head", { "aria-expanded": "false", "aria-controls": bodyId, onclick: () => {
          setToolOpen(el, !el.classList.contains("open"));
          if (el.classList.contains("open")) this.watchSubagent(card);
          else stopWatching(card);  // a browser has few connections per server to spare
        } },
          icon("fork"), h("span.subagent-heading", title, meta, prompt), cost, state, icon("chevronRight", "sm chev")),
        alert,
        h("div.tool-body", { id: bodyId, inert: true, "aria-hidden": "true" },
          h("div.section", h("div.label", "Task"), task), thread,
          h("div.section.subagent-foot", open)));
      card = { el, title, meta, state, cost, task, prompt, alert, thread, event: {}, view: null, source: null };
      this.subagents.set(event.id, card);
      this.body.append(el);
    }
    card.event = { ...card.event, ...event };
    const e = card.event;
    if (card.source && card.shownRun !== e.run) stopWatching(card);
    card.title.textContent = e.label || firstLine(e.task) || "Subagent";
    card.meta.textContent = [subagentProvider(e.provider), e.model, e.mode && `${modeName(e.mode)} mode`,
      e.fell_back && `took over from ${e.fell_back}`].filter(Boolean).join(" · ");  // (a free model that failed)
    card.task.textContent = e.task || "";
    card.prompt.textContent = firstLine(e.task, 140);
    card.prompt.title = e.task || "";
    const running = e.status === "running" || e.status === "waiting";
    this.updateSubagentState(card);
    const price = e.usage ? costLabel(e.usage.cost, e.usage.billing) : null;
    card.cost.replaceChildren(price ? h(`span.cost.${price.kind}`, { title: price.title }, price.label) : "");
    if (this.live && !this.status && running) this.showWorking("Subagents at work…");
    if (card.el.classList.contains("open")) this.watchSubagent(card);
  }

  updateSubagentState(card) {
    const e = card.event;
    const waiting = e.run
      && ([...this.forms.values()].some((form) => form.event.run === e.run && form.event.status === "pending")
        || [...this.permissions.values()].some((note) => note.dataset.run === e.run && note.classList.contains("ask")));
    const active = ["running", "waiting"].includes(e.status);
    const label = active && waiting ? "Waiting for you" : e.status === "waiting" ? "Queued"
      : e.status === "running" ? "Working" : e.status === "succeeded" ? "Completed"
        : e.status === "interrupted" ? "Stopped" : e.status === "failed" ? "Failed" : "Ready";
    card.state.className = `state subagent-status ${active && waiting ? "waiting" : e.status === "running" ? "working"
      : e.status === "succeeded" ? "ok" : e.status === "failed" ? "fail" : ""}`;
    card.state.replaceChildren(h("span.activity-dot"), label);
    card.alert.hidden = e.status !== "failed";
    card.alert.textContent = e.status === "failed" ? e.error || "Subagent failed. Open its chat for details." : "";
  }

  updateSubagentStates() {
    for (const card of this.subagents.values()) this.updateSubagentState(card);
  }

  // Show a subagent's own work in its card, live while it runs.
  watchSubagent(card) {
    const run = card.event.run;
    if (!run || card.source || card.shownRun === run || !card.el.classList.contains("open")) return;
    card.shownRun = run;
    card.view = new AssistantView({ live: card.event.status === "running", runId: run, onActivity: this.onActivity });
    card.thread.replaceChildren(card.view.el);
    card.source = new EventSource(relayUrl(`/api/runs/${run}/events`));
    watching.add(card);
    const source = card.source;
    source.onmessage = (message) => {
      if (card.source === source) card.view.add(JSON.parse(message.data));
    };
    const close = () => {
      if (card.source !== source) return;
      card.source?.close();
      card.source = null;
      watching.delete(card);
      if (!card.view.status) card.view.finish(card.event.status === "succeeded" ? "succeeded" : "failed");
    };
    card.source.addEventListener("end", close);
    source.onerror = () => { if (source.readyState === EventSource.CLOSED) close(); };
  }

  toolResult(event) {
    const card = this.toolCard(event.id, event.name);
    card.outcome = event.ok ? "done" : "failed";
    if (event.output !== undefined && event.output !== null) {
      card.output = event.output;
      const output = pretty(event.output);
      if (card.outputPre.textContent !== output) card.outputPre.textContent = output;
      card.outputSection.classList.toggle("hidden", !card.outputPre.textContent);
    }
    this.updateToolCard(card);
    if (!event.ok) {
      setToolOpen(card.el, true);
      this.body.append(card.el);  // a failed tool must remain visible outside collapsed activity
      this.finishActivity();
    }
    card.group.refresh();
    if (card.group.cards.some((tool) => tool.outcome === "running")) this.hideWorking();
    else if (this.live && !this.status) this.showWorking("Working…");
  }

  // A permission request: approved automatically, denied, or waiting for the
  // user, who answers with the buttons; a later event with the same id updates it.
  permission(event) {
    this.finishBlock();
    const what = Array.isArray(event.resources) ? event.resources.join("\n")
      : toolSummary(event.resources);
    // A subagent's request, shown here too: answered through its own run.
    const action = event.subagent ? `${event.subagent}: ${event.action}` : event.action;
    const runId = event.run || this.runId;
    // Approved automatically, as set up: the tool call itself shows what happened.
    if (event.decision === "once" && !this.permissions.has(event.id)) return;
    this.finishActivity();
    let note = this.permissions.get(event.id);
    if (!note) {
      note = h("div.run-note");
      this.permissions.set(event.id, note);
      this.body.append(note);
    }
    const asked = note.dataset.asked === "1";
    note.dataset.run = runId || "";
    if (event.decision === null || event.decision === undefined) {
      note.dataset.asked = "1";
      const waiting = this.live && !this.status && runId && answerPermission;
      note.className = "run-note ask";
      const answer = (decision) => answerPermission(runId, event.id, decision, note);
      // Allow and Deny, or the request's own choices (e.g. "Use … instead").
      const buttons = event.options
        ? event.options.map((o, i) =>
            h(`button.btn.small${i === 0 ? ".primary" : ""}`, { onclick: () => answer(o.id) }, o.label))
        : [h("button.btn.small", { onclick: () => answer("deny") }, "Deny"),
          h("button.btn.small.primary", { onclick: () => answer("allow") }, "Allow")];
      const countdown = waiting && event.expires ? h("span.ask-countdown.faint") : null;
      // What you're asked to allow is shown in full (scrolling when long).
      note.replaceChildren(h("div.ask-what", h("span.ask-action", action),
        h("pre.ask-detail", what)),
        waiting ? h("span.ask-buttons", countdown, ...buttons) : h("span.faint", "not answered"));
      if (countdown) {
        const tick = () => {
          const left = Math.max(0, Math.round(event.expires - Date.now() / 1000));
          countdown.textContent = `No answer: denied in ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
          if (left > 0 && countdown.isConnected) setTimeout(tick, 1000);
        };
        setTimeout(tick);
      }
      if (waiting) this.showWorking("Waiting for your approval…");
      this.updateSubagentStates();
      return;
    }
    // (Claude Code reports a refusal again, without Relay's reason: the reason stays.)
    if (event.outcome) note.dataset.outcome = event.outcome;
    const decision = note.dataset.outcome
      || (event.decision === "once" ? (asked ? "allowed" : "approved automatically") : "denied");
    const ok = !["denied", "timeout"].includes(event.decision);
    note.className = `run-note${asked ? ` answered ${ok ? "ok" : "no"}` : ""}`;
    note.replaceChildren(...permissionLine(action, what, decision));
    this.updateSubagentStates();
    if (this.live && !this.status) this.showWorking("Working…");
  }

  error(message) {
    this.finishActivity();
    this.finishBlock();
    this.hideWorking();
    // (once: a program may say the same error twice, as its session's and as its answer's)
    if ([...this.body.querySelectorAll(":scope > .run-error")]
      .some((e) => e.lastElementChild?.textContent === message)) {
      return;
    }
    this.body.append(h("div.run-error", icon("alert"), h("div", message)));
  }

  form(event) {
    this.finishActivity();
    this.finishBlock();
    this.hideWorking();
    let card = this.forms.get(event.id);
    if (!card) {
      card = new FormCard((e, answer) => answerForm(e.run || this.runId, e.id, answer));
      this.forms.set(event.id, card);
      this.body.append(card.el);
    }
    card.update(event, !!(this.live && !this.status && (event.run || this.runId) && answerForm));
    this.updateSubagentStates();
    if (this.live && !this.status) this.showWorking(event.status === "pending"
      ? "Waiting for your answers…" : "Working…");
  }

  finish(status, why = null) {
    this.status = status;
    if (this.live && !this.created) this.created = Date.now() / 1000;
    this.finishBlock();
    this.hideWorking();
    for (const card of this.tools.values()) {
      if (card.outcome === "running") {
        card.outcome = "stopped";
        this.updateToolCard(card);
        card.group.refresh();
      }
    }
    this.finishActivity();
    for (const card of this.forms.values()) {
      card.update(card.event.status === "pending" ? { status: "cancelled", message: "The run ended" } : {}, false);
    }
    this.updateSubagentStates();
    if (status === "interrupted") {
      this.body.append(h("div.run-note", why && why !== "the user stopped it" ? `Stopped: ${why}.` : "Stopped."));
    }
    if (status === "failed" && !this.body.querySelector(".run-error")) {
      this.error("The run failed without an error message.");
    }
    if (!this.body.childElementCount) this.body.append(h("div.run-note", "No response."));
    this.renderActions();
  }

  // A code task's changes after this turn, with a button to review them.
  setChanges(summary, onReview, git = null) {
    this.changes = summary;
    this.onReview = onReview;
    this.git = git;
    this.renderActions();
  }

  renderActions() {
    const info = [this.model, usageText(this.usage?.tokens)].filter(Boolean).join(" · ");
    const cost = costLabel(this.usage?.cost, this.usage?.billing);
    // (replaceChildren would show a null as the text "null")
    this.actions.replaceChildren(...[
      this.text ? h("button.icon-btn.sm.copy-action",
        { title: "Copy", "aria-label": "Copy", onclick: () => copyText(this.text) },
        icon("copy", "sm")) : null,
      info ? h("span.info", info) : null,
      cost ? h(`span.cost.${cost.kind}`, { title: cost.title }, cost.label) : null,
      messageTime(this.created),
    ].filter(Boolean));
    this.body.querySelector(".turn-review")?.remove();
    if (this.changes?.files) {
      const c = this.changes;
      const location = this.git && reviewLocation(this.git);
      this.body.append(h("div.turn-review",
        h("button.turn-changes",
        { title: "Review the current files; counts were recorded after this answer", onclick: () => this.onReview?.() },
        icon("file", "sm"),
        h("strong", "Review changes"), ` · ${c.files} file${c.files === 1 ? "" : "s"} `,
        h("span.add", `+${c.additions}`), " ", h("span.del", `−${c.deletions}`)),
        location ? h("p.review-location", { title: location.detail }, location.label) : null));
    }
  }
}
