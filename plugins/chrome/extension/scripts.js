// The side panel's scripts: yours on Relay's Scripts page, run from here (on Relay, signed in as you, as the
// page's Run does), their output as it's written, Stop, earlier runs, the chats a run started.

import { activeTab, askToReadPages, readTab, shellQuote } from "./lib/page.js";
import { ago, fill, h, icon } from "./lib/ui.js";

const GROUPS = [
  ["Private", (s) => s.private && !s.example],
  ["Public", (s) => !s.private && !s.example],
  ["Relay's examples", (s) => s.example],
];
const ENDED = { succeeded: "Succeeded", failed: "Failed", stopped: "Stopped", "timed out": "Timed out" };

export class ScriptsView {
  // `relay()`: the panel's Relay now; `toast(text)`; `openChat(id)`: a conversation in the panel's chat.
  constructor(root, { relay, toast, openChat }) {
    this.root = root;
    this.relay = relay;
    this.toast = toast;
    this.openChat = openChat;
    this.rows = null;      // the scripts, as the list has them
    this.query = "";
    this.current = null;   // the script open: { name, text, runs }
    this.shown = null;     // the run shown: as Relay gives it, its output growing as it's written
    this.abort = null;     // (following the shown run's output)
    this.pending = "";     // arguments the right-click menu sent, for the script you pick
    this.token = 0;
  }

  get name() {
    return this.current?.name || null;
  }

  // Shown: the script that was open (else the list), as Relay has it now.
  async show() {
    if (this.current) return this.open(this.current.name);
    const { script } = await chrome.storage.local.get("script");
    if (script) return this.open(script, { quiet: true });
    return this.list();
  }

  hide() {
    this.stopFollowing();
  }

  // The right-click menu's arguments: into the open script's field, else kept for the one you pick.
  takeArgs(args) {
    if (this.current) {
      const field = this.root.querySelector(".script-args");
      if (field) {
        field.value = args;
        this.keepArgs(args);
        field.focus();
        return;
      }
    }
    this.pending = args;
    if (this.rows) this.renderList();
  }

  // The list

  async list() {
    this.stopFollowing();
    this.current = null;
    chrome.storage.local.remove("script");
    const token = ++this.token;
    if (!this.rows) fill(this.root, h("div.faint.pad", "Loading your scripts…"));
    try {
      this.rows = await this.relay().scripts();
    } catch (error) {
      if (token === this.token) fill(this.root, h("div.error", icon("alert", 14), h("span", error.message)));
      return;
    }
    if (token === this.token) this.renderList();
  }

  renderList() {
    const search =
      h("input.search", { type: "search", placeholder: "Search scripts", value: this.query, autocomplete: "off" });
    const groups = h("div.script-groups");
    const draw = () => {
      const query = this.query.toLowerCase();
      const found = (this.rows || []).filter((s) => s.name.toLowerCase().includes(query));
      fill(groups, ...GROUPS.map(([label, belongs]) => {
        const rows = found.filter(belongs);
        if (!rows.length) return null;
        return h("div.script-group", h("div.group-label", label), rows.map((s) => h("button.script-row",
          { onclick: () => this.open(s.name), title: s.name },
          h("span.script-name", s.running ? h("span.dot", { title: "Running" }) : null, shortName(s.name)),
          h("span.script-meta", s.running ? "running" : ago(s.modified)))));
      }));
      if (!found.length) groups.append(h("div.faint.pad",
        this.rows?.length ? "No script is called that." : "No scripts yet: write one on Relay's Scripts page."));
    };
    search.addEventListener("input", () => { this.query = search.value; draw(); });
    draw();
    fill(this.root,
      this.pending ? h("div.notice", h("span", "Pick a script to run with ", h("code", clip(this.pending, 80))),
        h("button.chip-x",
          { title: "Not with these", onclick: () => { this.pending = ""; this.renderList(); } }, icon("x", 12))) : null,
      search, groups);
    if (!this.pending) search.focus();
  }

  // One script

  async open(name, { quiet = false } = {}) {
    this.stopFollowing();
    const token = ++this.token;
    let found;
    try {
      found = await this.relay().script(name);
    } catch (error) {
      if (token !== this.token) return;
      if (quiet) return this.list();
      this.toast(error.message);
      return;
    }
    if (token !== this.token) return;
    this.current = { name, text: found.text || "", runs: found.runs || [] };
    chrome.storage.local.set({ script: name });
    const { scriptArgs = {} } = await chrome.storage.local.get("scriptArgs");
    const args = this.pending || scriptArgs[name] || "";
    if (this.pending) this.keepArgs(this.pending);
    this.pending = "";
    this.shown = null;
    this.renderScript(args);
    if (this.current.runs[0]) await this.showRun(this.current.runs[0].id);
  }

  renderScript(args) {
    const { name, text } = this.current;
    const about = docstring(text);
    const [first, ...rest] = about.split(/\n\s*\n/);
    const field = h("input.script-args", { type: "text", value: args, placeholder: "Arguments, as on the Scripts page",
      spellcheck: false, autocomplete: "off" });
    field.addEventListener("input", () => this.keepArgs(field.value));
    field.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.isComposing) {
        e.preventDefault();
        if (!this.running) this.run();
      }
    });
    const add = (what) => async () => {
      const value = await this.fromPage(what);
      if (!value) return;
      field.value = `${field.value.trimEnd()}${field.value.trim() ? " " : ""}${shellQuote(value)}`;
      this.keepArgs(field.value);
      field.focus();
    };
    fill(this.root,
      h("div.script-head",
        h("button.icon-btn", { title: "Your scripts", onclick: () => this.list() }, icon("back")),
        h("div.script-title", h("strong", shortName(name)), h("span.faint", folder(name)))),
      about ? h("div.script-about", h("p", first.trim()),
        rest.length ? h("details", h("summary", "More"), h("p", rest.join("\n\n").trim())) : null) : null,
      h("label.script-label", "Arguments"),
      field,
      h("div.script-tools",
        h("button.btn.small", { onclick: add("url"), title: "The address of the page you're on, as an argument" },
          icon("link", 12), "Page address"),
        h("button.btn.small", { onclick: add("selection"), title: "What's selected on the page, as an argument" },
          icon("quote", 12), "Selection"),
        h("span.grow"),
        h("button.btn.primary.script-run",
          { onclick: () => (this.running ? this.stop() : this.run()) }, icon("play", 14), "Run")),
      h("div.script-note"),
      h("div.script-output"),
      h("div.script-runs"));
    this.renderOutput();
    this.renderRuns();
  }

  get running() {
    return this.shown?.status === "running";
  }

  async keepArgs(args) {
    const { scriptArgs = {} } = await chrome.storage.local.get("scriptArgs");
    if (args) scriptArgs[this.current.name] = args;
    else delete scriptArgs[this.current.name];
    await chrome.storage.local.set({ scriptArgs });
  }

  // The page's address or its selection; null (and why, beneath the field) when it can't be read.
  async fromPage(what) {
    const note = this.root.querySelector(".script-note");
    fill(note);
    const tab = await activeTab();
    if (!tab) return null;
    try {
      const page = await readTab(tab.id, 0);
      if (what === "url") return page.url;
      if (page.selection) return page.selection;
      fill(note, h("span.faint", "Nothing is selected on the page."));
      return null;
    } catch (error) {
      if (what === "url" && tab.url) return tab.url;
      if (error.blocked) {
        fill(note, h("span", "Relay can read the page once you allow it to read pages."),
          h("button.btn.small.primary", { onclick: async () => { if (await askToReadPages()) fill(note); } }, "Allow"));
      } else {
        fill(note, h("span.faint", error.never ? "Chrome doesn't let extensions read this page." : error.message));
      }
      return null;
    }
  }

  async run() {
    const name = this.current?.name;
    const field = this.root.querySelector(".script-args");
    const button = this.root.querySelector(".script-run");
    if (!name || !field) return;
    const args = field.value.trim();
    button.disabled = true;
    try {
      const id = await this.relay().runScript(name, args);
      if (this.current?.name !== name) return;
      this.shown = { id, script: name, args, status: "running", output: "", cut: 0, started: Date.now() / 1000,
        finished: null, exit_code: null, conversations: [] };
      this.current.runs.unshift({ ...this.shown });
      this.renderOutput();
      this.renderRuns();
      this.follow();
    } catch (error) {
      this.toast(error.message);
    } finally {
      button.disabled = false;
    }
  }

  async stop() {
    if (!this.shown) return;
    try {
      await this.relay().stopScript(this.shown.id);
      this.toast("Stopping…");
    } catch (error) {
      this.toast(error.message);
    }
  }

  async showRun(id) {
    this.stopFollowing();
    const name = this.current?.name;
    let found;
    try {
      found = await this.relay().scriptRun(id);
    } catch (error) {
      this.toast(error.message);
      return;
    }
    if (this.current?.name !== name) return;
    this.shown = { cut: 0, output: "", conversations: [], ...found, id };
    this.renderOutput();
    this.renderRuns();
    if (this.running) this.follow();
  }

  async follow() {
    this.stopFollowing();
    const run = this.shown;
    const abort = this.abort = new AbortController();
    try {
      for await (const part of this.relay().scriptOutput(run.id, (run.cut || 0) + run.output.length, abort.signal)) {
        if (this.shown !== run) return;
        if (part.text !== undefined) {
          run.output += part.text;
          this.appendOutput(part.text);
        } else if (part.conversations) {
          run.conversations = part.conversations;
          this.renderMade();
        } else if (part.end) {
          Object.assign(run, part.end);
        }
      }
    } catch (error) {
      if (!abort.signal.aborted) this.toast(error.message);
    }
    if (abort.signal.aborted || this.shown !== run) return;
    this.abort = null;
    // Relay's own record of how it ended (and the list's dot); a stream that ended without saying, too.
    const fresh = await this.relay().scriptRun(run.id).catch(() => null);
    if (this.shown !== run) return;
    if (fresh) Object.assign(run, { status: fresh.status, exit_code: fresh.exit_code, finished: fresh.finished,
      conversations: fresh.conversations || run.conversations });
    if (run.status === "running") run.status = "failed";
    const listed = this.current?.runs.find((r) => r.id === run.id);
    if (listed) Object.assign(listed, { status: run.status, finished: run.finished, exit_code: run.exit_code });
    this.renderOutput();
    this.renderRuns();
    this.rows = null;  // (the list's running dots, when it's shown next)
  }

  stopFollowing() {
    this.abort?.abort();
    this.abort = null;
  }

  // What the run printed, how it ended, the chats it started.
  renderOutput() {
    const box = this.root.querySelector(".script-output");
    const button = this.root.querySelector(".script-run");
    if (!box) return;
    if (button) fill(button, icon(this.running ? "stop" : "play", 14), this.running ? "Stop" : "Run");
    button?.classList.toggle("stop", this.running);
    const run = this.shown;
    if (!run) {
      fill(box, h("div.faint", "Not run yet."));
      return;
    }
    const pre =
      h("pre.output", run.cut ? `[${run.cut.toLocaleString()} characters before this are gone]\n` : "", run.output);
    fill(box,
      h("div.output-head", h("span", run.args ? h("code", clip(run.args, 60)) : "No arguments"),
        h("span.grow"), h("span.faint", ago(run.started))),
      pre,
      h("div.output-status", status(run)),
      h("div.script-made"));
    pre.scrollTop = pre.scrollHeight;
    this.renderMade();
  }

  appendOutput(text) {
    const pre = this.root.querySelector(".script-output pre.output");
    if (!pre) return;
    const atEnd = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 24;
    pre.append(text);
    if (atEnd) pre.scrollTop = pre.scrollHeight;
  }

  renderMade() {
    const made = this.root.querySelector(".script-made");
    const chats = this.shown?.conversations || [];
    if (!made) return;
    fill(made, chats.length ? h("div.group-label", "Chats it started") : null,
      chats.map((c) => h("button.made-row", { onclick: () => this.openChat(c.id), title: "Open it here" },
        icon("sparkle", 12), h("span", c.title || "Untitled"))));
  }

  renderRuns() {
    const box = this.root.querySelector(".script-runs");
    if (!box) return;
    const runs = (this.current?.runs || []).slice(0, 10);
    fill(box, runs.length ? h("div.group-label", "Runs") : null, runs.map((r) => h(
      `button.run-row${r.id === this.shown?.id ? ".current" : ""}`, { onclick: () => this.showRun(r.id) },
      h(`span.run-state.${(r.status || "").replace(/\s+/g, "-")}`), h("span.run-when", ago(r.started)),
      h("span.run-args", r.args ? clip(r.args, 40) : ""), h("span.faint", ENDED[r.status] || r.status || ""))));
  }
}

function status(run) {
  if (run.status === "running") return h("span.working", h("span.spinner"), h("span", "Running…"));
  const parts = [ENDED[run.status] || run.status];
  if (run.exit_code !== null && run.exit_code !== undefined) parts.push(`exit ${run.exit_code}`);
  if (run.started && run.finished) parts.push(duration(run.finished - run.started));
  return h(`span.ended.${(run.status || "").replace(/\s+/g, "-")}`, parts.filter(Boolean).join(" · "));
}

// A script's own description: its module docstring.
function docstring(text) {
  const found = String(text || "").match(/^\s*(?:#[^\n]*\n\s*)*[rRuU]?("""|''')([\s\S]*?)\1/);
  return found ? found[2].trim() : "";
}

function shortName(name) {
  return name.replace(/^(?:private|public)\/(?:examples\/)?/, "");
}

function folder(name) {
  return name.startsWith("private/") ? "private" : name.startsWith("public/examples/") ? "Relay's example" : "public";
}

function clip(text, n) {
  const flat = String(text || "").replace(/\s+/g, " ").trim();
  return flat.length > n ? `${flat.slice(0, n - 1)}…` : flat;
}

function duration(seconds) {
  if (!Number.isFinite(seconds)) return "";
  if (seconds < 60) return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)} s`;
  const minutes = Math.floor(seconds / 60);
  return minutes < 60 ? `${minutes} min ${Math.round(seconds % 60)} s`
    : `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}
