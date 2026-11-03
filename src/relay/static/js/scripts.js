// The Scripts page: your own Python scripts with Relay's SDK, written here (or by an agent, with the skill
// relay-sdk-template-writer) and run in their folder's WorkBox, signed in as you (relay/scripts.py).

import { relayUrl } from "./base.js";
import { api } from "./api.js";
import { openShare } from "./drive.js";
import { renderMarkdown } from "./markdown.js";
import {
  clear, clock, confirmDialog, copyText, h, icon, modal, promptDialog, showMenu, toast,
} from "./ui.js";

const STARTER = `"""What this script does, and what to change in it.

It runs from Relay's Scripts page, signed in as you: no login() needed.
"""

import sys

from relay_sdk import *

PROMPT = "Hello! Who are you, in one sentence?"


def main():
    chat = convo.create(title="From a script")
    answer = chat.send(" ".join(sys.argv[1:]) or PROMPT)
    print(answer.response)
    if answer.status != "succeeded":
        sys.exit(1)


if __name__ == "__main__":
    main()
`;
const ENDED = { running: "Running", succeeded: "Succeeded", failed: "Failed", stopped: "Stopped",
  "timed out": "Timed out" };
const KIND = { running: "", succeeded: "success", failed: "danger", stopped: "warning", "timed out": "warning" };
const PAINTED = 200_000;  // characters of a script coloured as it's typed (a longer one: plain, to stay quick)
const enc = encodeURIComponent;
const EXAMPLE = /^public\/examples\//;  // Relay's, kept up to date: read-only here
// A script's name as the page shows it: without its folder (private/, public/, public/examples/)
const bare = (name) => name.replace(/^(private|public)\/(examples\/)?/, "");

// Kept while you're elsewhere in Relay, by folder and name: what isn't saved yet, each script's arguments, the
// one open last
const unsaved = new Map();
const argsOf = new Map();
let last = null;

window.addEventListener("beforeunload", (e) => {
  if (!unsaved.size) return;
  e.preventDefault();
  e.returnValue = "";
});

// "my script" → "my_script.py"
function scriptName(text) {
  const name = text.trim().replace(/\s+/g, "_").replace(/\.py$/i, "");  // (UPPER.PY too: not UPPER.PY.py)
  return name ? `${name}.py` : "";
}

function took(run) {
  const seconds = Math.max(0, (run.finished || Date.now() / 1000) - run.started);
  return seconds < 60 ? `${Math.round(seconds)} s` : `${Math.floor(seconds / 60)} min ${Math.round(seconds % 60)} s`;
}

function statusBadge(status) {
  return h(`span.badge.script-status${KIND[status] ? `.${KIND[status]}` : ""}`,
    status === "running" ? h("span.script-running") : null, ENDED[status] || status);
}

// hooks: { onOpen(conversationId), pickFolder(current), agentName(), writeWithAgent(description),
//   version(), host() (where a panel goes: the main area) }
// The scripts as the side column groups them (main.js: the Scripts view): those running now (under their own
// folder too), yours, the public ones, Relay's examples. [id, title, hint, scripts]
export function scriptGroups(scripts) {
  return [
    ["running", "Running", "", scripts.filter((s) => s.running)],
    ["private", "Private", "No agent sees them, not even AgentOS's Governor", scripts.filter((s) => s.private)],
    ["public", "Public", "Every agent may read them; AgentOS's Governor may run them, or make a task that does",
      scripts.filter((s) => !s.private && !s.example)],
    ["examples", "Examples", "Relay keeps them up to date: copy one to change it", scripts.filter((s) => s.example)],
  ];
}

// A script's name as the page shows it (its folder left out), and whether it has changes not saved (here)
export const scriptLabel = (name) => bare(name);
export const isUnsaved = (folder, name) => unsaved.has(`${folder}/${name}`);

// hooks: { onOpen(conversation), pickFolder, agentName(), writeWithAgent(text), version(), host(), wanted() (a
// script to open first: the page's address), onList(data, open name) (the side column's list), onOpened(name) }
export function scriptsView(hooks) {
  const folderLine = h("div.scripts-folder");
  const pane = h("section.script-pane");
  const docsButton = h("button.btn.small", { onclick: () => toggleDocs(), "aria-expanded": "false",
    title: "How scripts run here, and Relay's SDK" }, icon("book", "sm"), "Documentation");
  const el = h("div.scroller", h("div.scripts",
    h("div.scripts-head",
      h("div.scripts-title", h("h1", "Scripts"), h("span.grow"),
        h("button.btn.small", { onclick: openInEditor,
          title: "The scripts' folder as a network drive, to edit them on your computer" }, icon("code", "sm"),
        "Open in VS Code"),
        docsButton),
      h("p.muted", "Your own Python scripts with Relay's SDK. They run in their folder's WorkBox, signed in as you."),
      folderLine),
    pane));
  let data = null;  // {folder, scripts}
  let current = null;  // the open script: {name, text (as saved), runs, modified}
  let settled = false;  // (until the first load opened the one the address names, the address stays)
  let shown = null;  // the run whose output is shown
  let source = null;  // its output as it comes
  let closed = false;
  let starting = false;
  let painting = 0;  // a colouring of the editor's text, to come (requestAnimationFrame)
  let docs = null;  // the documentation's panel, when it's open
  let parts = {};  // the open script's elements
  const key = (name) => `${data?.folder}/${name}`;

  async function load() {
    try {
      const fresh = await api.get("/api/scripts");
      if (closed) return;
      if (JSON.stringify(fresh) !== JSON.stringify(data)) {
        data = fresh;
        renderFolder();
        renderList();
      }
      // The one the page's address names (/scripts/<name>), else the one open last
      const wanted = !settled && !current && hooks.wanted?.();
      const reopen = !current && data.scripts.find((s) => (wanted ? s.name === wanted : key(s.name) === last));
      if (reopen) await open(reopen.name);
      else if (!current && !pane.firstChild) renderPane();
      else noticeDisk();
      if (!settled && !closed) {
        settled = true;
        hooks.onOpened?.(current?.name || null);
      }
    } catch (error) {
      if (!data) {  // (its folder can't be used: another one)
        clear(pane, h("div.scripts-empty", h("p.error-text", error.message),
          h("button.btn.small", { onclick: changeFolder }, icon("folder", "sm"), "Choose another folder")));
      }
    }
  }

  function renderFolder() {
    clear(folderLine, icon("folder", "sm"), h("span.mono.truncate", { title: data.folder }, data.folder),
      h("button.btn.ghost.small", { onclick: changeFolder }, "Change"),
      // (shared once Open in VS Code was opened, and so it stays: said here, as a simulated user missed it)
      data.shared ? h("button.btn.ghost.small.scripts-shared", { onclick: openInEditor,
        title: "Anyone on your network with its address can open the folder: Stop sharing is there" },
      icon("monitor", "sm"), "Shared as a drive") : null);
  }

  // The list is the side column's (main.js: the Scripts view): it's told what's there and which one is open
  function renderList() {
    if (data) hooks.onList?.(data, current?.name || null);
    if (settled) hooks.onOpened?.(current?.name || null);
  }

  // The open script

  async function open(name) {
    stopFollowing();
    let found;
    try {
      found = await api.get(`/api/scripts/${enc(name)}`);
    } catch (error) {
      toast(error.message, "error");
      return;
    }
    if (closed) return;
    last = key(name);
    current = { ...found, modified: data?.scripts.find((s) => s.name === name)?.modified };
    shown = null;
    renderList();
    renderPane();
    if (current.runs.length) show(current.runs[0].id);
  }

  function dirty() {
    return Boolean(current) && parts.editor?.value !== current.text;
  }

  function renderPane() {
    stopFollowing();
    if (!current) {
      parts = {};
      clear(pane, h("div.scripts-empty", icon("fileCode", "lg"),
        h("p", "Open a script in the Scripts list on the left, or start one: from scratch, from a template, or by "
          + "asking an agent to write it."),
        h("div.scripts-new",
          h("button.btn.small", { onclick: newScript }, icon("plus", "sm"), "New"),
          h("button.btn.small", { onclick: fromTemplate }, icon("file", "sm"), "From a template"),
          h("button.btn.small", { onclick: withAgent }, icon("sparkles", "sm"), "With an agent")),
        h("p.faint", "A script imports Relay's SDK (from relay_sdk import *) and can start chats and code tasks, ",
          "follow their answers, and review and commit what they changed.")));
      return;
    }
    const name = current.name;
    const example = EXAMPLE.test(name);  // (Relay's: read-only here, copied to be changed)
    // The text is typed in the textarea (its own letters transparent) over its coloured copy (highlight.js);
    // the line numbers and the copy follow its scrolling
    const editor = h("textarea.script-editor-text", { spellcheck: false, wrap: "off", autocapitalize: "off",
      autocomplete: "off", "aria-label": `${name}'s code`, readOnly: example,
      value: example ? current.text : unsaved.get(key(name)) ?? current.text });
    const code = h("code.hljs.language-python");
    const coloured = h("pre.script-highlight", { "aria-hidden": "true" }, code);
    const numbers = h("div.script-numbers");
    const gutter = h("div.script-lines", { "aria-hidden": "true" }, numbers);
    const follow = () => {
      coloured.style.transform = `translate(${-editor.scrollLeft}px, ${-editor.scrollTop}px)`;
      numbers.style.transform = `translateY(${-editor.scrollTop}px)`;
    };
    const save = example ? null
      : h("button.btn.small", { onclick: () => saveScript(), title: "Save (Ctrl+S)" }, "Save");
    const args = h("input.input.mono.script-args", { placeholder: "Arguments (optional)",
      value: argsOf.get(key(name)) || "", "aria-label": "Its arguments",
      oninput: () => argsOf.set(key(name), args.value),
      onkeydown: (e) => { if (e.key === "Enter") run(); } });
    const runButton = h("button.btn.small.primary", { onclick: run, title: "Run (Ctrl+Enter)" }, icon("play", "sm"),
      "Run");
    const stopButton = h("button.btn.small.danger", { onclick: stop, hidden: true }, icon("stop", "sm"), "Stop");
    const notice = h("div.script-notice", { hidden: true });
    const output = h("div.script-output-box");
    const runs = h("div.script-runs");
    parts = { editor, code, numbers, follow, save, runButton, stopButton, notice, output, runs };

    let escaped = false;
    editor.addEventListener("keydown", (e) => {
      const mod = e.ctrlKey || e.metaKey;
      if (mod && e.key.toLowerCase() === "s") { e.preventDefault(); if (!example) saveScript(); return; }
      if (mod && e.key === "Enter") { e.preventDefault(); run(); return; }
      if (e.key === "Escape") { escaped = true; return; }  // (then Tab leaves the editor)
      if (e.key === "Tab" && !mod && !e.altKey && !escaped && !example) {
        e.preventDefault();
        indent(editor, e.shiftKey);
      }
      escaped = false;
    });
    editor.addEventListener("input", edited);
    editor.addEventListener("scroll", follow);

    clear(pane,
      h("div.script-bar", h("h2.script-name.mono.truncate", { title: name }, name),
        example ? h("span.badge", { title: "Relay keeps it up to date: copy it to change it" }, "Example")
          : name.startsWith("private/") ? h("span.badge", { title: "No agent sees it, not even AgentOS's Governor" },
            "Private")
            : h("span.badge.public", { title: "Every agent may read it; AgentOS's Governor may run it" }, "Public"),
        h("span.grow"),
        example ? h("button.btn.small", { onclick: copyExample }, icon("copy", "sm"), "Copy to my scripts") : save,
        h("button.icon-btn.sm", { title: "More", onclick: (e) => more(e.currentTarget) }, icon("more"))),
      notice,
      h("div.script-editor", gutter, h("div.script-code", coloured, editor)),
      h("div.script-run", args, runButton, stopButton),
      output, runs);
    edited();
    renderOutput();
    renderRuns();
  }

  // Tab: four spaces in (Shift: out), for the selected lines when there are several
  function indent(editor, out) {
    const { selectionStart: start, selectionEnd: end, value } = editor;
    const from = value.lastIndexOf("\n", start - 1) + 1;
    if (!out && !value.slice(start, end).includes("\n")) {
      if (!document.execCommand("insertText", false, "    ")) editor.setRangeText("    ", start, end, "end");
      edited();
      return;
    }
    const lines = value.slice(from, end).split("\n");
    const changed = lines.map((line) => (out ? line.replace(/^ {1,4}/, "") : `    ${line}`)).join("\n");
    editor.setSelectionRange(from, end);
    if (!document.execCommand("insertText", false, changed)) editor.setRangeText(changed, from, end, "select");
    editor.setSelectionRange(from, from + changed.length);
    edited();
  }

  function edited() {
    const { editor, numbers, save } = parts;
    if (!editor) return;
    const lines = editor.value.split("\n").length;
    if (numbers.childElementCount !== lines) {
      clear(numbers, Array.from({ length: lines }, (_, i) => h("span", String(i + 1))));
    }
    if (!painting) painting = requestAnimationFrame(paint);
    const was = unsaved.has(key(current.name));
    if (dirty()) unsaved.set(key(current.name), editor.value);
    else unsaved.delete(key(current.name));
    if (save) save.disabled = !dirty();
    if (was !== unsaved.has(key(current.name))) renderList();
  }

  // Its text coloured as Python, under the textarea (once a frame, however fast one types)
  function paint() {
    painting = 0;
    const { editor, code, follow } = parts;
    if (!editor) return;
    const text = `${editor.value}\n`;  // (its last line shows, an empty one too)
    if (window.hljs?.getLanguage("python") && text.length <= PAINTED) {
      code.innerHTML = window.hljs.highlight(text, { language: "python", ignoreIllegals: true }).value;
    } else {
      code.textContent = text;
    }
    follow();
  }

  async function saveScript() {
    if (!current || !parts.editor) return false;
    const text = parts.editor.value;
    try {
      const saved = await api.put(`/api/scripts/${enc(current.name)}`, { text });
      current.text = text;
      current.modified = saved.modified;
      edited();
      parts.notice.hidden = true;
      load();
      return true;
    } catch (error) {
      toast(error.message, "error");
      return false;
    }
  }

  // Changed in its folder meanwhile (an agent, say): taken in when nothing here is unsaved, else said
  async function noticeDisk() {
    if (!current || !data) return;
    const there = data.scripts.find((s) => s.name === current.name);
    const { notice } = parts;
    if (!there) {
      if (!dirty()) { current = null; renderPane(); renderList(); return; }
      clear(notice, icon("alert", "sm"), h("span.grow", "It's gone from the folder; Save writes it again."));
      notice.hidden = false;
      return;
    }
    if (there.modified === current.modified) return;
    if (!dirty()) {
      const found = await api.get(`/api/scripts/${enc(current.name)}`).catch(() => null);
      if (!found || closed || dirty() || current?.name !== found.name) return;
      Object.assign(current, { text: found.text, runs: found.runs, modified: there.modified });
      const at = parts.editor.selectionStart;
      parts.editor.value = found.text;
      parts.editor.setSelectionRange(at, at);
      edited();
      renderRuns();
      return;
    }
    clear(notice, icon("alert", "sm"), h("span.grow", "It changed in its folder meanwhile."),
      h("button.btn.small", { onclick: async () => {
        const found =
          await api.get(`/api/scripts/${enc(current.name)}`).catch((error) => toast(error.message, "error"));
        if (!found) return;
        unsaved.delete(key(current.name));
        Object.assign(current, { text: found.text, runs: found.runs, modified: there.modified });
        renderPane();
        renderList();
      } }, "Load it"),
      h("button.btn.small.ghost", { onclick: () => { current.modified = there.modified; notice.hidden = true; } },
        "Keep mine"));
    notice.hidden = false;
  }

  function more(anchor) {
    const path = { label: "Copy its path", icon: "copy", onclick: () => copyText(`${data.folder}/${current.name}`) };
    const mine = current.name.startsWith("private/");
    showMenu(anchor, EXAMPLE.test(current.name) ? [
      { label: "Copy to my scripts", icon: "file", onclick: copyExample }, path,
    ] : [
      { label: "Rename", icon: "edit", onclick: rename },
      mine ? { label: "Make public", icon: "globe", title: "Every agent may read it; AgentOS's Governor may run it",
        onclick: () => moveTo("public") }
        : { label: "Make private", icon: "shield", title: "No agent sees it, not even AgentOS's Governor",
          onclick: () => moveTo("private") },
      path,
      "-",
      { label: "Delete", icon: "trash", danger: true, onclick: remove },
    ], { align: "right" });
  }

  // An example as a script of your own (private), to change: Relay names it (ask.py, else ask_2.py, …)
  async function copyExample() {
    await made(() => api.post(`/api/scripts/${enc(current.name)}/copy`, { to: "private" }));
  }

  // Private to public or back: who may see it (and the Governor run it) changes; Relay names it there
  async function moveTo(folder) {
    const name = current.name;
    if (folder === "public" && !(await confirmDialog(`Make ${bare(name)} public?`,
      "Every agent will be able to read it, and AgentOS's Governor to run it, or make a task that does.",
      { confirm: "Make public" }))) return;
    await renamed(name, () => api.post(`/api/scripts/${enc(name)}/move`, { to: folder }));
  }

  async function rename() {
    const name = current.name;
    const answer = await promptDialog("Rename script", { label: "Its name", value: bare(name), confirm: "Rename" });
    const to = answer ? `${name.split("/")[0]}/${scriptName(answer)}` : "";
    if (!to || to === name) return;
    await renamed(name, () => api.post(`/api/scripts/${enc(name)}/rename`, { to }));
  }

  // Renamed or moved by `call` (its answer: the name it has now): what's kept for it here follows, and it opens
  async function renamed(name, call) {
    let to;
    try {
      ({ name: to } = await call());
    } catch (error) {
      toast(error.message, "error");
      return;
    }
    for (const kept of [unsaved, argsOf]) {
      if (kept.has(key(name))) { kept.set(key(to), kept.get(key(name))); kept.delete(key(name)); }
    }
    last = key(to);
    current = null;
    await load();  // (it opens `last`)
  }

  async function remove() {
    const name = current.name;
    // (a task that runs it fails without it: said before, not found out later, as a simulated user did)
    const tasks = current.tasks || [];
    const also = !tasks.length ? "" : ` ${tasks.length === 1 ? "A task runs it" : `${tasks.length} tasks run it`} (`
      + `${tasks.map((t) => `“${t}”`).join(", ")}): without it, ${tasks.length === 1 ? "it fails" : "they fail"}.`;
    if (!(await confirmDialog(`Delete ${name}?`,
      `The script and its runs are deleted. Conversations it started stay.${also}`,
      { confirm: "Delete", danger: true }))) return;
    try {
      await api.del(`/api/scripts/${enc(name)}`);
    } catch (error) {
      toast(error.message, "error");
      return;
    }
    unsaved.delete(key(name));
    argsOf.delete(key(name));
    last = null;
    current = null;
    renderPane();
    load();
  }

  // Runs

  async function run() {
    if (!current || starting) return;  // (Enter and a click: one run)
    starting = true;
    parts.runButton.disabled = true;
    try {
      await start();
    } finally {
      starting = false;
      if (parts.runButton) parts.runButton.disabled = false;
    }
  }

  async function start() {
    if (dirty() && !(await saveScript())) return;
    const name = current.name;
    const args = argsOf.get(key(name)) || "";
    try {
      const { run: id } = await api.post(`/api/scripts/${enc(name)}/run`, { args });
      if (current?.name !== name) return;
      shown = { id, script: name, args, status: "running", output: "", cut: 0,
        started: Date.now() / 1000, finished: null, exit_code: null, conversations: [] };
      current.runs.unshift({ ...shown });
      renderOutput();
      renderRuns();
      parts.output.scrollIntoView({ block: "nearest" });
      follow();
      load();
    } catch (error) {
      toast(error.message, "error");
    }
  }

  async function stop() {
    if (!shown) return;
    try {
      await api.post(`/api/scripts/runs/${shown.id}/stop`);
      toast("Stopping…");
    } catch (error) {
      toast(error.message, "error");
    }
  }

  async function show(id) {
    stopFollowing();
    try {
      const found = await api.get(`/api/scripts/runs/${id}`);
      if (closed || found.script !== current?.name) return;
      shown = found;
    } catch (error) {
      toast(error.message, "error");
      return;
    }
    renderOutput();
    renderRuns();
    if (shown.status === "running") follow();
  }

  // Its output as it's written. (EventSource would reconnect with the characters seen when it started:
  // a lost connection is picked up here instead, with those seen by then.)
  function follow() {
    stopFollowing();
    const run = shown;
    source = new EventSource(relayUrl(`/api/scripts/runs/${run.id}/output?after=${run.cut + run.output.length}`));
    source.onmessage = (e) => {
      if (shown !== run) return;
      const { text } = JSON.parse(e.data);
      run.output += text;
      const box = parts.output?.querySelector("pre");
      if (!box) return;
      const atEnd = box.scrollHeight - box.scrollTop - box.clientHeight < 24;
      box.append(text);
      if (atEnd) box.scrollTop = box.scrollHeight;
      box.hidden = false;
      parts.output.querySelector(".script-hint")?.remove();
    };
    source.addEventListener("conversations", (e) => {
      if (shown !== run) return;
      run.conversations = JSON.parse(e.data);
      renderMade();
    });
    source.addEventListener("end", (e) => {
      stopFollowing();
      if (shown !== run) return;
      Object.assign(run, JSON.parse(e.data));
      const listed = current?.runs.find((r) => r.id === run.id);
      if (listed) Object.assign(listed, { status: run.status, finished: run.finished, exit_code: run.exit_code });
      renderOutput();
      renderRuns();
      load();
    });
    source.onerror = () => {
      stopFollowing();
      setTimeout(() => { if (!closed && shown === run && run.status === "running") show(run.id); }, 1500);
    };
  }

  function stopFollowing() {
    source?.close();
    source = null;
  }

  function renderOutput() {
    const { output, stopButton } = parts;
    if (!output) return;
    const running = shown?.status === "running";
    stopButton.hidden = !running;
    if (!shown) {
      clear(output, h("p.faint.script-hint", "What it prints shows here as it runs. Ctrl+Enter runs it"
        + (EXAMPLE.test(current?.name || "") ? "." : ", Ctrl+S saves it.")));
      return;
    }
    const pre = h("pre.script-output", { hidden: !shown.output }, shown.output);
    clear(output,
      h("div.script-output-head", statusBadge(shown.status),
        h("span.muted", running ? `started ${clock(shown.started)}` : ended(shown)),
        shown.args ? h("span.mono.faint.truncate", { title: shown.args }, shown.args) : null),
      shown.cut ? h("p.faint.script-cut", `Its first ${shown.cut.toLocaleString()} characters aren't kept.`) : null,
      pre,
      running && !shown.output ? h("p.faint.script-hint", "Nothing printed yet…") : null,
      h("div.script-made"));
    renderMade();
    pre.scrollTop = pre.scrollHeight;
  }

  function ended(run) {
    const code = run.exit_code !== null && run.exit_code !== undefined && run.status !== "succeeded"
      ? ` · exit ${run.exit_code}` : "";
    return `${clock(run.started)} · ${took(run)}${code}`;
  }

  function renderMade() {
    const made = parts.output?.querySelector(".script-made");
    if (!made) return;
    const conversations = shown?.conversations || [];
    clear(made, conversations.length ? [h("span.muted", "It started:"), conversations.map((c) =>
      h("button.chip", { onclick: () => hooks.onOpen(c.id), title: "Open this conversation" }, icon("chat", "sm"),
        h("span.truncate", c.title)))] : null);
    made.hidden = !conversations.length;
  }

  function renderRuns() {
    const { runs } = parts;
    if (!runs) return;
    if (!current.runs.length) { clear(runs); return; }
    clear(runs, h("h3", "Runs"), h("div.script-run-list", current.runs.map((r) => h(
      `button.script-run-item${shown?.id === r.id ? ".active" : ""}`, { onclick: () => show(r.id) },
      statusBadge(r.status), h("span", clock(r.started)), h("span.faint", r.status === "running" ? "" : took(r)),
      r.args ? h("span.mono.faint.truncate", r.args) : null))));
  }

  // Starting one

  // A new script (Relay refuses a name that's taken), opened
  async function create(name, text) {
    if (name) await made(() => api.post(`/api/scripts/${enc(name)}`, { text }));
  }

  // A script made by `call` (its answer: its name), opened to change
  async function made(call) {
    let name;
    try {
      ({ name } = await call());
    } catch (error) {
      toast(error.message, "error");
      return;
    }
    await load();
    await open(name);
    // At its start, where its settings are (the caret was at its end, and the editor scrolled there)
    parts.editor?.setSelectionRange(0, 0);
    parts.editor?.focus();
    if (parts.editor) parts.editor.scrollTop = 0;
  }

  async function newScript() {
    const name = await promptDialog("New private script", { label: "Its name (Make public, in its menu, shares it "
      + "with agents)", placeholder: "my_script.py", confirm: "Create" });
    if (name !== null && scriptName(name)) await create(`private/${scriptName(name)}`, STARTER);
  }

  async function fromTemplate() {
    let templates;
    try {
      templates = (await api.get("/api/scripts/templates")).templates;
    } catch (error) {
      toast(error.message, "error");
      return;
    }
    const m = modal("Start from a template", { wide: true });
    m.body.append(h("p.muted", "Each is a working script to change: its settings are at the top."),
      h("div.template-list", templates.map((t) => h("button.template-item", { onclick: async () => {
        m.close();
        const name = await promptDialog("New private script from a template", { label: "Its name",
          value: bare(t.free), confirm: "Create" });
        if (name !== null && scriptName(name)) await create(`private/${scriptName(name)}`, t.text);
      } }, h("span.mono", t.name), h("span.muted", t.description)))));
    m.foot.append(h("button.btn", { onclick: () => m.close() }, "Cancel"));
  }

  function withAgent() {
    if (!data) return;
    const m = modal("Write a script with an agent");
    const what = h("textarea.textarea", { rows: 4, placeholder: "E.g. ask three models the same question and "
      + "compare their answers, or: work through every TODO in ~/projects/site for an hour" });
    const start = async () => {
      const text = what.value.trim();
      if (!text) { what.focus(); return; }
      m.close();
      await hooks.writeWithAgent(text);
    };
    what.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); start(); }
    });
    m.body.append(
      h("p.muted", "An agent writes it in public/, where agents may (your private/ is out of their sight), as a code ",
        "task there with Relay's skill for it (relay-sdk-template-writer). It shows up under Public when it's saved; ",
        "Make private moves it."),
      h("label.field", h("span", "What should it do?"), what),
      h("p.faint", `With ${hooks.agentName()}: the provider and model chosen under the message box.`));
    m.foot.append(h("button.btn", { onclick: () => m.close() }, "Cancel"),
      h("button.btn.primary", { onclick: start }, icon("sparkles", "sm"), "Write it"));
    what.focus();
  }

  async function changeFolder() {
    const folder = await hooks.pickFolder(data?.folder);
    if (!folder || folder === data?.folder) return;
    try {
      data = await api.put("/api/scripts", { folder });
    } catch (error) {
      toast(error.message, "error");
      return;
    }
    current = null;
    last = null;
    renderFolder();
    renderList();
    renderPane();
  }

  // The scripts' folder as a network drive (relay/webdav.py), to open in VS Code or any editor
  function openInEditor() {
    const wheel = `${location.origin}${relayUrl("/sdk/")}relay_sdk-${hooks.version()}-py3-none-any.whl`;
    openShare("/api/scripts/share", {
      title: "Open in VS Code",
      lead: "Your scripts' folder on your computer, as a network drive (WebDAV): edit the scripts in VS Code, or "
        + "any editor, and run them here. What you save there shows on this page.",
      more: (address, { system }) => h("div.drive-steps", h("div.drive-os", h("b", "VS Code"), h("ol",
        h("li", "Open the address as a drive on your computer (the steps for each system are below)."),
        h("li", "In VS Code: File → Open Folder…, and choose the drive",
          { windows: " (the letter Map network drive gave it)", mac: " (in /Volumes)",
            linux: " (gio mounts it in /run/user/<your id>/gvfs)" }[system] || "", "."),
        h("li", "For completions, install the SDK for the Python that VS Code uses: ",
          h("code", `pip install ${wheel}`)),
        h("li", "Run the scripts here: on your own computer a script needs login() first.")))),
    });
  }

  // The documentation beside the page: how scripts run here, and the SDK (the skill's reference)
  async function toggleDocs() {
    if (docs) {
      closeDocs();
      return;
    }
    const body = h("div.md.scripts-docs-body", h("div.spinner"));
    docs = h("aside.scripts-docs", { "aria-label": "Documentation", tabindex: "-1",
      onkeydown: (e) => { if (e.key === "Escape") { e.preventDefault(); closeDocs(); docsButton.focus(); } } },
    h("div.scripts-docs-head", icon("book", "sm"), h("h2.grow", "Documentation"),
      h("button.icon-btn",
        { title: "Close (Escape)", onclick: () => { closeDocs(); docsButton.focus(); } }, icon("x"))),
    body);
    hooks.host().append(docs);
    el.classList.add("with-docs");
    docsButton.setAttribute("aria-expanded", "true");
    docs.focus();
    try {
      renderMarkdown(body, (await api.get("/api/scripts/docs")).markdown);
    } catch (error) {
      clear(body, h("p.error-text", error.message));
    }
  }

  function closeDocs() {
    docs?.remove();
    docs = null;
    el.classList.remove("with-docs");
    docsButton.setAttribute("aria-expanded", "false");
  }

  function close() {
    closed = true;
    stopFollowing();
    closeDocs();
  }

  return { el, load, close, open, newScript, fromTemplate, withAgent, openInEditor, toggleDocs, changeFolder };
}
