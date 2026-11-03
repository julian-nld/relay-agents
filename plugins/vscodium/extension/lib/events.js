// An agent's answer as the chat shows it, built from its run's events: as they come, or as Relay kept them. Blocks
// only grow or change in place (a tool's result, a question answered), so the chat updates each by its place.
"use strict";

const OUTPUT = 20000;  // characters of a tool's output kept
const KINDS = {
  bash: "terminal", shell: "terminal", run_command: "terminal", execute: "terminal",
  edit: "edit", write: "edit", edit_file: "edit", write_file: "edit", multiedit: "edit", patch: "edit",
  apply_patch: "edit", read: "file", read_file: "file", view: "file", grep: "search", glob: "search",
  search: "search", list: "list-tree", ls: "list-tree", webfetch: "globe", fetch: "globe", websearch: "globe",
  web_search: "globe", todowrite: "checklist", think: "lightbulb",
};

// Starting or waiting for subagents shows as their cards instead (as on Relay's page)
function delegating(event) {
  if (/(^|__)relay__delegate$/.test(event.name || "")) return true;
  const command = typeof event.input?.command === "string" ? event.input.command : "";
  return /\/(?:relay-)?tools\/[\w-]+\/relay'?\s+(delegate|wait)\b/.test(command);
}

function firstLine(text, max = 120) {
  const lines = String(text || "").trim().split("\n");
  const line = lines[0] || "";
  if (line.length > max) return `${line.slice(0, max - 1)}…`;
  return lines.length > 1 ? `${line} …` : line;
}

// Relay's tools script, where an agent's command names it by its whole path (as Relay's page shortens it)
const TOOLS = /(?:["']?)[^\s"']*\/(?:relay-)?tools\/[\w-]+\/relay(?:["']?)(?=\s|$)/g;

function shortened(text) {
  return text.replace(TOOLS, "relay");
}

// What a tool call is about, in a line: its command, file, pattern or address
function summary(input) {
  if (input === null || input === undefined) return "";
  if (typeof input !== "object") return firstLine(input);
  if (typeof input.pattern === "string" && input.pattern) {
    const where = typeof input.path === "string" && input.path !== "." ? input.path : "";
    return firstLine(where ? `${input.pattern}  in ${where}` : input.pattern);
  }
  for (const key of ["command", "cmd", "file_path", "filePath", "path", "url", "query", "description", "prompt",
    "title"]) {
    if (typeof input[key] === "string" && input[key]) return firstLine(shortened(input[key]));
  }
  return "";
}

// A tool's file, when it names one by its whole path (/…, ~/…, or C:\… on Windows)
function filePath(input) {
  const found = input && typeof input === "object" ? input.file_path || input.filePath || input.path : null;
  return typeof found === "string" && /^(\/|~[\\/]|[A-Za-z]:[\\/])/.test(found) ? found : null;
}

const DIFF_LINES = 400;  // lines of an edit's diff shown in the chat

// Two texts' lines as a diff: [sign, line] with " " for what both have (the longest common lines between them)
function lineDiff(before, after) {
  const a = before === "" ? [] : before.split("\n");
  const b = after === "" ? [] : after.split("\n");
  // (too big to match)
  if (a.length * b.length > 250000) return [...a.map((l) => ["-", l]), ...b.map((l) => ["+", l])];
  const common = Array.from({ length: a.length + 1 }, () => new Array(b.length + 1).fill(0));
  for (let i = a.length - 1; i >= 0; i--) {
    for (let j = b.length - 1; j >= 0; j--) {
      common[i][j] = a[i] === b[j] ? common[i + 1][j + 1] + 1 : Math.max(common[i + 1][j], common[i][j + 1]);
    }
  }
  const lines = [];
  let i = 0;
  let j = 0;
  while (i < a.length || j < b.length) {
    if (i < a.length && j < b.length && a[i] === b[j]) {
      lines.push([" ", a[i++]]);
      j++;
    } else if (j < b.length && (i === a.length || common[i][j + 1] > common[i + 1][j])) {
      lines.push(["+", b[j++]]);
    } else {
      lines.push(["-", a[i++]]);
    }
  }
  return lines;
}

// What an edit tool changes, as diff lines: Claude Code's Edit and MultiEdit, opencode's edit, a Write's content, a
// patch (Codex's apply_patch); null for another tool
function editOf(input) {
  if (typeof input === "string") return /^(\*\*\* Begin Patch|diff --git|--- )/m.test(input) ? patchLines(input) : null;
  if (!input || typeof input !== "object") return null;
  const pairs = [];
  const pair = (e) => {
    const before = e.old_string ?? e.oldString ?? e.old_str;
    const after = e.new_string ?? e.newString ?? e.new_str;
    if (typeof before === "string" && typeof after === "string") pairs.push([before, after]);
  };
  pair(input);
  if (Array.isArray(input.edits)) input.edits.forEach(pair);
  if (pairs.length) {
    const lines = [];
    pairs.forEach(([before, after], n) => {
      if (n) lines.push(["@", "…"]);
      lines.push(...lineDiff(before, after));
    });
    return lines.slice(0, DIFF_LINES);
  }
  const content = input.content ?? input.contents;
  if (typeof content === "string" && (input.file_path || input.filePath || input.path)) {
    return content.split("\n").slice(0, DIFF_LINES).map((l) => ["+", l]);
  }
  const patch = input.patch ?? input.input;
  return typeof patch === "string" ? editOf(patch) : null;
}

// An answer's tokens and cost, as Relay's page says them: "1,200 in · 310 out · $0.0123 API"
function usageLine(usage) {
  if (!usage) return "";
  const t = usage.tokens || {};
  const cache = t.cache || {};
  const input = (t.input_tokens ?? t.input ?? t.prompt_tokens ?? 0) + (t.cache_read_input_tokens || 0)
    + (t.cache_creation_input_tokens || 0) + (cache.read || 0) + (cache.write || 0);
  const output = t.output_tokens ?? t.output ?? t.completion_tokens;
  const dollars = (n) => `$${Number(n).toFixed(n < 0.01 ? 4 : 2)}`;
  const cost = usage.billing === "subscription" ? "in plan" : usage.billing === "free" ? "free"
    : usage.billing === "api" ? (usage.cost ? `${dollars(usage.cost)} API` : "API")
    : usage.cost ? dollars(usage.cost) : "";
  return [input ? `${input.toLocaleString("en")} in` : "", output ? `${output.toLocaleString("en")} out` : "", cost]
    .filter(Boolean).join(" · ");
}

function patchLines(text) {
  return text.split("\n").slice(0, DIFF_LINES).map((l) => [
    /^(\+\+\+|---|\*\*\*)/.test(l) ? "@" : "+-".includes(l[0]) ? l[0] : l.startsWith("@@") ? "@" : " ",
    /^[+\- ]/.test(l) && !/^(\+\+\+|---)/.test(l) ? l.slice(1) : l]);
}

function answer(run, stored = {}) {
  return { role: "assistant", run, status: stored.status || null, error: stored.error || null, blocks: [],
    model: stored.run_model || null, provider: stored.run_provider || null, title: null, hidden: [],
    id: stored.id ?? null, changes: stored.changes || null, undo: !!stored.checkpoint, usage: null };
}

// One event into the answer; whether what it shows changed
function apply(a, event) {
  const blocks = a.blocks;
  const last = blocks[blocks.length - 1];
  const find = (type, id) => blocks.find((b) => b.type === type && b.id === id);
  switch (event.type) {
    case "step":
      a.model = event.model || a.model;
      return false;
    case "text":
    case "reasoning":
      if (!event.delta) return false;
      if (last && last.type === event.type) last.text += event.delta;
      else blocks.push({ type: event.type, text: event.delta });
      return true;
    case "tool_call": {
      if (delegating(event)) {
        a.hidden.push(event.id);
        return false;
      }
      const name = String(event.name || "tool");
      const what = summary(event.input);
      blocks.push({ type: "tool", id: event.id, name, what, kind: KINDS[name.toLowerCase()] || "tools",
        path: filePath(event.input), status: "running", output: "", edit: editOf(event.input) });
      return true;
    }
    case "tool_result": {
      const tool = find("tool", event.id);
      if (!tool) return false;
      tool.status = event.ok === false ? "failed" : "done";
      tool.output = String(event.output ?? "").slice(0, OUTPUT);
      return true;
    }
    case "subagent": {
      let card = find("subagent", event.id);
      if (!card) {
        card = { type: "subagent", id: event.id };
        blocks.push(card);
      }
      Object.assign(card, { label: event.label || firstLine(event.task) || card.label || "Subagent",
        provider: event.provider ?? card.provider, model: event.model ?? card.model,
        status: event.status ?? card.status, run: event.run ?? card.run });
      return true;
    }
    case "permission": {
      let note = find("permission", event.id);
      // (approved by itself, as set up: the tool call itself shows what happened)
      if (!note && event.decision === "once") return false;
      if (!note) {
        note = { type: "permission", id: event.id, asked: false };
        blocks.push(note);
      }
      const what = Array.isArray(event.resources) ? event.resources.join("\n") : summary(event.resources);
      Object.assign(note, { run: event.run || a.run, action: event.subagent ? `${event.subagent}: ${event.action}`
        : event.action || "a tool", what, options: event.options || null, decision: event.decision ?? null,
        outcome: event.outcome || note.outcome || null, expires: event.expires || null });
      if (note.decision === null) note.asked = true;
      return true;
    }
    case "form": {
      let card = find("form", event.id);
      if (!card) {
        card = { type: "form", id: event.id };
        blocks.push(card);
      }
      Object.assign(card, { run: event.run || card.run || a.run, title: event.title ?? card.title,
        fields: event.fields ?? card.fields ?? [], status: event.status ?? card.status, answer: event.answer
        ?? card.answer, message: event.message ?? card.message, url: event.url ?? card.url });
      return true;
    }
    case "error":
      if (blocks.some((b) => b.type === "error" && b.message === event.message)) return false;  // (said twice)
      blocks.push({ type: "error", message: String(event.message || "error") });
      return true;
    case "title":
      a.title = event.title || a.title;
      return false;
    case "usage":
      a.usage = { tokens: event.tokens || null, cost: event.cost ?? null, billing: event.billing || null };
      return true;
    case "done":
      a.status = event.status || "succeeded";
      for (const b of blocks) {
        if (b.type === "tool" && b.status === "running") b.status = "stopped";
        if (b.type === "form" && b.status === "pending") Object.assign(b,
          { status: "cancelled", message: "The run ended" });
      }
      return true;
    default:
      return false;
  }
}

// A conversation's messages (as GET /api/conversations/<id> gives them) as the chat's turns; `running`: the run
// whose answer is still coming (the chat follows it from its events)
function turns(messages, running = null) {
  const found = [];
  let asked = null;  // (the message the next answer answers: what Retry asks again)
  for (const m of messages || []) {
    if (m.role === "user") {
      asked = m.id;
      found.push({ role: "user", id: m.id, text: m.content || "",
        attachments: (m.attachments || []).map((f) => f.name || f.id) });
    } else if (m.role === "assistant" && m.run_id && m.run_id !== running) {
      const a = answer(m.run_id, m);
      a.prompt = asked;
      for (const event of m.events || []) apply(a, event);
      if (!a.blocks.some((b) => b.type === "text") && m.content) a.blocks.push({ type: "text", text: m.content });
      if (m.status === "failed" && m.error && !a.blocks.some((b) => b.type === "error")) {
        a.blocks.push({ type: "error", message: m.error });
      }
      found.push(a);
    } else if (m.role === "assistant" && !m.run_id && m.content) {  // (Relay's own word in the chat)
      found.push({ role: "note", text: m.content });
    }
  }
  return found;
}

module.exports = { KINDS, answer, apply, delegating, editOf, firstLine, lineDiff, summary, turns, usageLine };
