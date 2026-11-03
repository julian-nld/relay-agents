// A compact, factual summary of consecutive tool calls; no extra model request.

import { h, icon } from "./ui.js";

export function toolKind(name, input) {
  // Relay's memory saved, changed or forgotten: its MCP tools (mcp__relay__memory_add, relay_memory_add…), a chat
  // model's functions, `relay memory add` in a shell. A turn shows it remembered something.
  if (/memory_(add|change|forget)$|^(remember|change_memory|forget_memory)$/i.test(name || "")) return "memory";
  if (/shell|bash|exec|command|terminal/i.test(name || "")) {
    const command = input?.command || input?.cmd || "";
    if (/\brelay\s+memory\s+(add|change|forget)\b/.test(command)) return "memory";
    return /\b(pytest|vitest|jest|unittest)\b|\b(npm|pnpm|yarn|go|cargo|bun)\s+(run\s+)?test\b/.test(command)
      ? "tests" : "command";
  }
  if (/grep|glob|search|find|list/i.test(name || "")) return "search";
  if (/read/i.test(name || "")) return "read";
  if (/write|edit|patch/i.test(name || "")) return "edit";
  if (/web|fetch|url/i.test(name || "")) return "web";
  return "tool";
}

const COMPLETED = {
  read: ["file read", "files read"], edit: ["edit", "edits"], search: ["search", "searches"],
  tests: ["test run", "test runs"], command: ["command", "commands"], web: ["web request", "web requests"],
  tool: ["tool call", "tool calls"], memory: ["memory updated", "memories updated"],
};
const RUNNING = { read: "Reading files", edit: "Editing files", search: "Searching", tests: "Running tests",
  command: "Running command", web: "Fetching a page", tool: "Running tool", memory: "Updating memory" };

export function activitySummary(cards) {
  const counts = new Map();
  for (const card of cards) {
    if (card.outcome === "done") counts.set(card.kind, (counts.get(card.kind) || 0) + 1);
  }
  const parts = [...counts].map(([kind, count]) => `${count} ${COMPLETED[kind][count === 1 ? 0 : 1]}`);
  const pending = cards.filter((card) => card.outcome === "running");
  if (pending.length) parts.push(`${RUNNING[pending.at(-1).kind]}${pending.length > 1 ? ` (${pending.length})` : ""}`);
  const failed = cards.filter((card) => card.outcome === "failed").length;
  const stopped = cards.filter((card) => card.outcome === "stopped").length;
  if (failed) parts.push(`${failed} failed`);
  if (stopped) parts.push(`${stopped} stopped`);
  return parts.join(" · ");
}

export class ToolActivity {
  constructor(open, live = false) {
    this.cards = [];
    this.label = h("span.activity-summary");
    this.tools = h("div.activity-tools");
    this.el = h("details.tool-activity", { open: live || open },
      h("summary.activity-head", icon("chevronRight", "sm chev"), h("span", "Activity"), this.label), this.tools);
  }

  add(card) {
    this.cards.push(card);
    this.tools.append(card.el);
    card.group = this;
    this.refresh();
  }

  finish(completedOpen) {
    this.completedOpen = completedOpen;
    this.refresh();
  }

  refresh() {
    const summary = activitySummary(this.cards);
    this.label.textContent = summary;
    this.label.title = summary;
    this.el.hidden = this.cards.every((card) => card.outcome === "failed");
    // A block ends at the next non-tool event, not between consecutive calls.
    if (this.completedOpen && !this.cards.some((card) => card.outcome === "running")) {
      this.el.open = this.completedOpen();
      this.completedOpen = null;
    }
  }
}
