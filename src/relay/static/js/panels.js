// The side column's lists for Tasks, AgentOS, Scripts and Production boxes (Relay 2.11): rows that open what they
// name in the main area. Chats and Governor are main.js's (the chat list's own rows).

import { chatIcon, conversationStatus } from "./activity.js";
import { TASK_KINDS, taskState } from "./schedules.js";
import { isUnsaved, scriptGroups, scriptLabel } from "./scripts.js";
import { healthDot } from "./service.js";
import { taskGroups } from "./tasks.js";
import { h, icon } from "./ui.js";

// Whether a row's words have what's typed in the view's search
export function matches(filter, ...words) {
  const wanted = String(filter || "").trim().toLowerCase();
  return !wanted || words.some((word) => String(word || "").toLowerCase().includes(wanted));
}

// A group of rows under its heading with how many there are; `toggle` folds it (then its rows aren't drawn)
function group(title, rows, { key = "", hint = null, folded = false, toggle = null } = {}) {
  const count = h("span.group-count", String(rows.length));
  const head = toggle
    ? h("button.group-toggle", { type: "button", "aria-expanded": String(!folded), onclick: toggle },
      icon(folded ? "chevronRight" : "chevronDown", "sm"), h("span.grow.truncate", title), count)
    : [h("span.truncate", title), count];
  return h(`section.conv-group.panel-group${key ? `.${key}` : ""}`, { title: hint },
    h("h3", head), folded ? null : rows);
}

// A row: what it is (an icon), its name, what's to know at a glance
function row({ label, iconName = null, lead = null, active = false, onclick, title = null, after = [] }) {
  return h(`button.panel-item${active ? ".active" : ""}`, { type: "button", title: title || label,
    "aria-current": active ? "page" : null, onclick },
  lead || (iconName ? icon(iconName, "sm mode") : null), h("span.title", label), ...after);
}

function empty(text) {
  return h("div.empty-list", text);
}

// Tasks: as the Tasks page groups them; the discarded folded
export function tasksList(answer, { filter, current, onOpen, discardedOpen, toggleDiscarded }) {
  if (!answer) return h("div.spinner");
  const sections = taskGroups(answer).map(([key, title, tasks]) => {
    const shown = tasks.filter((task) => matches(filter, task.title, task.directory));
    if (!shown.length) return null;
    const rows = shown.map((task) => {
      const status = conversationStatus(task, { group: key });
      return row({ label: task.title, lead: chatIcon(task), active: current === task.id,
        onclick: () => onOpen(task.id),
        after: ["waiting", "working", "failed", "review"].includes(status.kind)
          ? [h(`span.conv-status.${status.kind}`, status.label)] : [] });
    });
    return group(title, rows, { key, ...(key === "discarded"
      ? { folded: !discardedOpen && !filter, toggle: toggleDiscarded } : {}) });
  });
  return sections.some(Boolean) ? sections : empty(filter ? "No task matches." : "No tasks yet.");
}

// AgentOS: its overview (the dashboard), what needs you, its tasks by kind. Out of this Relay: your scheduled tasks
export function agentosList(found, { filter, path, task, included, onOverview, onNeeds, onTask }) {
  const overview = row({ label: included ? "Overview" : "Scheduled tasks", iconName: included ? "compass" : "clock",
    active: path === "/agentos" && !task, onclick: onOverview,
    title: included ? "The dashboard: the Governor, services, tasks and what happened" : "Your scheduled tasks" });
  if (!found) return [overview, h("div.spinner")];
  const open = (found.os?.interventions || []).filter((i) => !i.resolved);
  const needs = open.filter((i) => matches(filter, i.text)).map((i) => row({ label: i.text, iconName: "alert",
    onclick: onNeeds, title: i.text }));
  const kinds = included ? TASK_KINDS : TASK_KINDS.filter(([kind]) => kind === "manual");
  const tasks = (found.list || []).filter((t) => (included || !t.governed) && matches(filter, t.name, t.describe));
  const sections = kinds.map(([kind, title]) => {
    const mine = tasks.filter((t) => (t.kind || "manual") === kind);
    return mine.length ? group(title, mine.map((t) => {
      const [state, tone] = taskState(t);
      return row({ label: t.name, iconName: t.script ? "fileCode" : t.governed ? "compass" : "clock",
        active: task === t.id, onclick: () => onTask(t.id), title: `${t.name}: ${t.describe}`,
        after: [h(`span.panel-state.${tone}`, state)] });
    }), { key: kind }) : null;
  });
  return [overview, needs.length ? group("Needs you", needs, { key: "attention" }) : null, ...sections,
    !needs.length && !sections.some(Boolean) && filter ? empty("Nothing matches.") : null];
}

// Scripts: running now, yours, the public ones, Relay's examples
export function scriptsList(data, { filter, open, onOpen, folded, toggle }) {
  if (!data) return h("div.spinner");
  const sections = scriptGroups(data.scripts).map(([key, title, hint, scripts]) => {
    const shown = scripts.filter((s) => matches(filter, s.name));
    if (!shown.length) return null;
    const rows = shown.map((s) => row({ label: scriptLabel(s.name), iconName: s.example ? "book" : "file",
      active: open === s.name, onclick: () => onOpen(s.name), title: s.name,
      after: [
        isUnsaved(data.folder, s.name) ? h("span.script-unsaved", { title: "Not saved" }, "•") : null,
        s.running ? h("span.panel-state.ok", h("span.script-running"), "running") : null,
      ] }));
    const foldable = key === "examples" && !filter;
    return group(title, rows,
      { key, hint, ...(foldable ? { folded: folded.has(key), toggle: () => toggle(key) } : {}) });
  });
  return sections.some(Boolean) ? sections
    : empty(filter ? "No script matches." : "No scripts yet: + starts one.");
}

// Production boxes: the services, how each is doing
export function servicesList(services, { filter, path, onOpen }) {
  if (!services) return h("div.spinner");
  const shown = services.filter((s) => matches(filter, s.name));
  if (!shown.length) {
    return empty(filter ? "No service matches." : "None yet: software that's live, kept running by Relay.");
  }
  return shown.map((s) => row({ label: s.name, active: path === `/s/${s.id}`, onclick: () => onOpen(s.id),
    title: s.health_ok === false ? `Failing: ${s.health_detail}` : `${s.name}: its page, shell and log`,
    // Looked after by AgentOS: its icon in Relay's green, with "os" by it (as a managed chat's)
    lead: s.managed
      ? h("span.mode-wrap.managed", { title: "Managed by AgentOS: its Governor sees it and may change it" },
        icon("box", "sm"), h("span.os", { "aria-hidden": "true" }, "os")) : icon("box", "sm mode"),
    after: [healthDot(s)] }));
}
