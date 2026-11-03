// AgentOS (relay/agentos.py): a dashboard of the Governor, what needs you, the services and the tasks
// (carried out by the Governor, which chooses the agents, or a prompt Relay sends a chosen agent by itself:
// relay/schedules.py), and the activity log.

import { relayUrl, storeKey } from "./base.js";
import { api } from "./api.js";
import { healthDot, managedToggle, serviceDialog } from "./service.js";
import { clear, confirmDialog, h, icon, modal, shortPath, toast } from "./ui.js";

const KINDS = [["daily", "Every day"], ["weekdays", "Weekdays"], ["weekly", "On days of the week"],
  ["interval", "Every … hours or minutes"], ["once", "Once"]];
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function when(seconds) {
  if (!seconds) return "—";
  const date = new Date(seconds * 1000);
  const time = date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const today = new Date().toDateString() === date.toDateString();
  return today ? `today ${time}`
    : `${date.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" })} ${time}`;
}

// How a task's last turn went, as one short label (its row here, and the side column's AgentOS view).
export function taskState(s) {
  if (!s.enabled && !s.last_run) return ["off", "muted"];
  if (s.dispatch_state === "dispatched" || s.dispatch_state === "dispatching") return ["working", "ok"];
  if (s.running) return ["working", "ok"];  // (its script runs, or its agent works: not "ran" yet)
  if (s.dispatch_state === "awaiting") return ["awaiting report", "warn"];
  if (s.last_error || s.outcome === "failed") return ["failed", "bad"];
  if (s.outcome === "interrupted") return ["interrupted", "warn"];
  if (s.outcome === "done") return ["done", "good"];
  // (how it went isn't known yet, or wasn't followed: having run is no "done", a simulated user found)
  if (s.last_run) return ["ran", "muted"];
  return [s.enabled ? "planned" : "off", "muted"];
}

// The kinds of task, as they're grouped: Relay's own, the Governor's, yours
export const TASK_KINDS = [["system", "System tasks"], ["governor", "Governor's tasks"], ["manual", "Manual tasks"]];

// hooks: { providers(), providerName(id), modelsFor(id), modesFor(id), pickFolder(dir), onOpen(id), prefill }
const CAPABILITY_NAMES = { "production.promote": "promotes", "production.rollback": "rolls back",
  "chat.delete": "deletes a chat", "workbox.delete": "deletes a clone", "task.delete": "deletes a task",
  "service.delete": "deletes a service", "service.restore": "puts data back", "service.create": "makes a service",
  "memory.forget": "forgets a memory" };

// A governed task's turn, as Relay keeps it (dispatch_state) and how the last one went, by whose word.
function turnOf(s) {
  if (s.dispatch_state === "dispatched" || s.dispatch_state === "dispatching") return "The Governor is on it now";
  const by = s.outcome_by === "governor" ? "the Governor says" : "Relay saw";
  const note = s.outcome_note ? `: ${s.outcome_note}` : "";
  if (s.dispatch_state === "awaiting") return "Awaiting the Governor's report on its last turn";
  if (s.outcome) return `Last turn ${s.outcome} (${by}${note})`;
  return null;
}

// Where a service's version came from (relay/production.py), for its tooltip.
function provenance(v) {
  const parts = [`Version ${v.number}: ${v.status}`];
  if (v.origin_title) parts.push(
    `from ${v.origin_title}${v.from_version ? ` (a clone of version ${v.from_version})` : ""}`);
  else if (v.source) parts.push(`from ${v.source}`);
  if (v.promoted_by) parts.push(
    `made live by ${v.promoted_by.startsWith("governor") ? "the Governor" : "you"} ${when(v.promoted_at)}`);
  if (v.replaced_version) parts.push(`replaced version ${v.replaced_version}`);
  try {
    const agents = v.agents ? JSON.parse(v.agents) : [];
    if (agents.length) parts.push(`worked on by ${agents.map((a) => a.model || a.provider).join(", ")}`);
    const check = v.validation ? JSON.parse(v.validation) : null;
    if (check) parts.push(`Relay's check: ${check.ok ? "well" : "not well"} (${check.detail})`);
  } catch { /* an older version without these */ }
  return parts.join("\n");
}

function describeEvent(e) {
  const d = e.detail || {};
  switch (e.kind) {
    case "capability":
      return `${d.capability}${d.arguments?.service ? ` ${d.arguments.service}` : ""}${e.ok ? "" : `: ${d.why}`}`;
    case "refused": return `refused tool ${d.tool} (not one of its own)`;
    case "report": return `reported: ${d.text}`;
    case "agent.signal": return `an agent may be going wrong: ${d.title}, ${d.signal}`;
    case "agent.resumed": return `went on with ${d.title}: its turn was cut off by a restart`;
    case "memory.changed": return `changed a memory (${d.place}): ${d.text}`;
    case "memory.forgotten": return `forgot a memory (${d.place}): ${d.text}. Why: ${d.why}`;
    case "ranking.forgotten": return "started the model ranking over";
    case "service.stopped": return `stopped the service${e.version ? ` (version ${e.version})` : ""}`;
    case "service.started": return `started the service: ${e.ok ? "well" : `not well (${d.detail})`}`;
    case "service.restarted": return `restarted version ${e.version}: ${e.ok ? "well" : `not well (${d.detail})`}`;
    case "service.deleted": return `deleted service ${d.name}`;
    case "service.created": return `made the service ${d.name}, at port ${d.port}`;
    case "workbox.linked": return `a task goes on as its WorkBox (version ${e.version})`;
    case "data.restored": return `put its data back as ${d.snapshot} had it (the data before kept as ${d.kept})`;
    case "wake": return e.ok ? `woke the Governor: ${d.reason}` : `couldn't wake the Governor: ${d.why}`;
    case "wake.held": return `held the Governor's news (${d.news}): it's woken for news so often an hour; `
      + `again in ${Math.max(1, Math.round(d.until / 60))} min`;
    case "data.snapshot_failed": return `couldn't copy the data before version ${e.version}: ${d.why}`;
    case "task.dispatched": return `gave the Governor task ${d.name}`;
    case "task.reported": return `task ${d.name} ${e.ok ? "done" : "failed"}${d.note ? `: ${d.note}` : ""}`;
    case "task.unreported": return `task ${d.name}: the wake-up ended without a report`;
    case "task.interrupted": return `task ${d.name}: ${d.why}; due again ${when(d.again)}`;
    case "task.given_up": return `gave up on task ${d.name}: ${d.why}`;
    case "policy": return `${d.capability}: ${e.ok ? "allowed" : `not allowed${d.why ? ` (${d.why})` : ""}`}`;
    case "version.live": return `version ${e.version} live${d.replaced ? ` (version ${d.replaced} archived)` : ""}`;
    case "version.failed": return `version ${e.version} didn't come up well`;
    case "workbox.cloned": return `cloned version ${e.version}`;
    case "reconciled": return `put right after a stop: ${[].concat(d.what || []).join("; ")}`;
    case "intervention": return `intervention: ${d.text}`;
    case "intervention.updated": return `updated an intervention: ${d.text}`;
    case "intervention.resolved": return e.actor === "governor"
      ? `settled an intervention: ${d.note}` : "settled an intervention";
    default: return e.kind;
  }
}

export function scheduledView(hooks) {
  const el = h("div.tasks-view.schedules-view");
  const included = () => hooks.agentos?.() !== false;  // (out of this Relay: Settings → AgentOS)
  let editing = false;  // a task's form is open: the page's poll leaves it be
  const load = async () => {
    editing = false;
    hooks.onForm?.(null);
    if (!included()) return loadScheduled();
    let list, os, services, ranked;
    try {
      [list, os, services, ranked] = await Promise.all([api.get("/api/schedules"), api.get("/api/agentos"),
        api.get("/api/services"), api.get("/api/models/ranking").catch(() => null)]);
    } catch (error) {
      clear(el, h("p.error-text", error.message));
      return;
    }
    el.classList.add("os-wide");
    const open = os.interventions.filter((i) => !i.resolved);
    const panels = {};
    // …and what the Governor settled lately itself (it says why): yours to see, not to do
    const settled =
      os.interventions.filter((i) => i.resolved_by === "governor" && Date.now() / 1000 - i.resolved < 3 * 86400);
    panels.needs = open.length
      ? panel("Needs you", null, [...open.map(interventionRow), ...settled.map(settledRow)], "needs")
      // (nothing open: calm)
      : settled.length ? panel("Settled by the Governor", null, settled.map(settledRow)) : null;
    // What the Governor reported (notify.report): to know, not to act on
    const reports = os.events.filter((e) => e.kind === "report" && Date.now() / 1000 - e.at < 7 * 86400).slice(0, 5);
    panels.reports = reports.length ? panel("From the Governor", null, reports.map((e) => h("div.os-row.report-row",
      icon("chat", "sm"), h("div.os-row-main", clamped(e.detail?.text || "", 3), h("div.sub", when(e.at))))), "reports")
      : null;
    panels.services =
      panel("Services", h("button.btn.small", { onclick: () => serviceForm() }, icon("plus", "sm"), "New service"),
        services.length ? services.map(serviceRow)
          : h("p.muted.empty", "None yet: software that's live, kept running by Relay at an address that stays."));
    // Three kinds: Relay's own (system), the Governor's (it may change and delete them), and yours
    const kinds = [["system", "System tasks", "Relay's own, carried out by the Governor. Edit one, or reset it to "
      + "its default; turn it off rather than delete it."],
    ["governor", "Governor's tasks", "Made by the Governor, which may change and delete them."],
    ["manual", "Manual tasks", "Added by you: for the Governor, or a prompt for an agent you choose."]];
    panels.tasks = panel("Tasks", null, list.length ? kinds.map(([kind, title, about]) => {
      const mine = list.filter((t) => (t.kind || "manual") === kind);
      return mine.length
        ? h(`div.os-task-kind.${kind}`, h("h3.os-subtitle", title, h("span.group-count", String(mine.length))),
          h("p.muted.os-kind-about", about), mine.map(taskRow)) : null;
    }) : h("p.muted.empty", "None yet: for the Governor, or a prompt for an agent you choose."));
    panels.governor = governorCard(os);
    panels.ranking = rankingPanel(ranked, list, os);
    panels.activity = activityFeed(os);
    clear(el,
      h("div.pane-head", h("div.grow", h("h1", "AgentOS"),
        h("p.muted", "The Governor steers agents for your tasks while you're away.")),
      h("button.btn.primary.small", { onclick: () => form() }, icon("plus", "sm"), "New task")),
      tiles(os, list, services, open, panels),
      h("div.os-dash",
        h("div.os-main", panels.needs, panels.reports, panels.services, panels.tasks),
        h("div.os-side", panels.governor, panels.ranking, panels.activity)));
  };

  // AgentOS out of this Relay: what's left of the page is your own scheduled tasks (prompts for an agent you
  // choose, as before AgentOS); tasks for the Governor wait until it's back.
  async function loadScheduled() {
    let list;
    try {
      list = await api.get("/api/schedules");
    } catch (error) {
      clear(el, h("p.error-text", error.message));
      return;
    }
    el.classList.remove("os-wide");
    const mine = list.filter((t) => (t.kind || "manual") === "manual" && !t.governed);
    const waiting = list.filter((t) => (t.kind || "manual") === "manual" && t.governed).length;
    clear(el,
      h("div.pane-head", h("div.grow", h("h1", "Scheduled tasks"),
        h("p.muted", "Prompts that run by themselves, for an agent you choose.")),
      h("button.btn.primary.small", { onclick: () => form() }, icon("plus", "sm"), "New task")),
      mine.length ? h("div.os-panel.scheduled-list", mine.map(taskRow))
        : h("p.muted.empty", "None yet: a prompt that runs each day, each week, or once."),
      waiting ? h("p.muted.small-text", `${waiting} task${waiting === 1 ? "" : "s"} for the Governor wait${waiting === 1
        ? "s" : ""} until AgentOS is back (Settings → AgentOS).`) : null);
  }

  // A section of the dashboard: its title, an action, its rows.
  function panel(title, action, body, kind = "") {
    return h(`section.os-panel${kind ? `.${kind}` : ""}`,
      h("div.os-panel-head", h("h2.section-title", title), action), body);
  }

  // The figures at a glance; a tile goes to its section.
  function tiles(os, list, services, open, panels) {
    const s = os.settings;
    const active = list.filter((t) => t.enabled);
    const upcoming = active.map((t) => t.next_run).filter(Boolean).sort((a, b) => a - b)[0];
    const awaiting = list.filter((t) => t.dispatch_state === "awaiting").length;
    // (one with nothing live, not stopped by hand, isn't well either: its first version failed, say)
    const failing = services.filter((v) => v.health_ok === false || (!v.live && !v.stopped));
    const loop = os.loops.find((l) => l.id === s.loop);
    const round = s.enabled && s.last_wake ? s.last_wake + s.round_hours * 3600 : null;
    const tile = (label, value, sub, tone, target) => h(`button.os-tile${tone ? `.${tone}` : ""}`,
      { onclick: () => target?.scrollIntoView({ behavior: "smooth", block: "start" }) },
      h("span.os-tile-label", label), h("span.os-tile-value", value), h("span.os-tile-sub", { title: sub }, sub));
    return h("div.os-tiles",
      tile("Governor", !s.enabled ? "Off" : os.governor?.running ? "Working" : "Idle",
        [loop ? loop.name : "no models chosen", s.last_wake ? ` · woken ${when(s.last_wake)}` : "",
          round ? ` · round ${when(round)}` : ""].join(""),
        !s.enabled ? "muted" : os.governor?.running ? "ok" : "", panels.governor),
      tile("Needs you", String(open.length), open.length ? "interventions to look at" : "nothing open",
        open.length ? "bad" : "", panels.needs),
      tile("Services", String(services.length), services.length ? (failing.length ? `${failing.length} not well`
        : "all well") : "none yet", failing.length ? "bad" : "", panels.services),
      tile("Tasks", String(active.length), [upcoming ? `next ${when(upcoming)}` : "nothing planned",
        awaiting ? ` · ${awaiting} await a report` : ""].join(""), awaiting ? "warn" : "", panels.tasks),
      tile("Spent, 24 h", `$${(os.spent_24h || 0).toFixed(2)}`, `budget about $${s.max_cost} per wake-up`, "",
        panels.activity));
  }

  // Text that can be long (a Governor's note, an intervention): a few lines, the rest on a click.
  function clamped(text, lines = 2, cls = "") {
    const box = h(`div.os-clamp${cls ? `.${cls}` : ""}`, { title: "Click for all of it" }, text);
    box.classList.add(`lines-${lines}`);
    box.addEventListener("click", () => box.classList.toggle("open"));
    return box;
  }

  function taskRow(s) {
    const enabled = h("input", { type: "checkbox", checked: s.enabled, onchange: async () => {
      try {
        await api.put(`/api/schedules/${s.id}`, { enabled: enabled.checked });
      } catch (error) {
        toast(error.message, "error");
      }
      load();
    } });
    const [state, tone] = taskState(s);
    const who = s.governed ? "Governor decides"
      : s.script ? `runs ${s.script}${s.script_args ? ` ${s.script_args}` : ""}`  // (Relay runs it: no agent)
        : `${hooks.providerName(s.provider_id)}${s.model ? ` · ${s.model}` : ""}`
          + `${s.target === "same" ? " · same chat" : ""}`;
    const note = s.last_error ? `Failed: ${s.last_error}`
      : ((s.governed || s.script) && s.outcome_note ? turnOf(s) : null);
    return h("div.os-row.schedule-row",
      h("label.switch", { title: s.enabled ? "On" : "Off" }, enabled, h("span")),
      h("div.os-row-main",
        h("div.name", s.name,
          s.changed ? h("span.badge", { title: "Changed from Relay's default (Reset gives it back)" },
            "edited") : null),
        h("div.sub", `${s.describe} · ${who}${s.directory ? ` · ${shortPath(s.directory)}` : ""}`),
        h("div.sub", s.enabled ? `Next: ${when(s.next_run)}` : "Off",
          s.last_run ? [" · Last: ", s.last_conversation
            ? h("a", { href: relayUrl(`/c/${s.last_conversation}`),
              onclick: (e) => { e.preventDefault(); hooks.onOpen(s.last_conversation); } },
              when(s.last_run))
            : when(s.last_run)] : null),
        note ? clamped(note, 1, s.last_error ? "fail" : "") : null),
      h(`span.os-state.${tone}`, { title: s.running ? "Running now" : turnOf(s) || state }, state),
      h("div.actions",
        h("button.btn.small", { title: "Run it now; the schedule stays", onclick: async () => {
          try {
            const ran = await api.post(`/api/schedules/${s.id}/run`);
            if (ran.conversation_id) hooks.onOpen(ran.conversation_id);
            else if (ran.script_run) {  // (Relay runs it, with no agent: its output is on the Scripts page)
              toast(`${s.script} runs now`, "info", { label: "Scripts", onclick: () => hooks.openScripts?.() });
              load();
            }
          } catch (error) {
            toast(error.message, "error");
          }
        } }, icon("send", "sm"), "Run now"),
        h("button.icon-btn.sm", { title: "Edit", onclick: () => (s.kind === "system" ? editSystem(s) : form(s)) },
          icon("edit", "sm")),
        s.kind === "system"
          ? h("button.icon-btn.sm.reset-task", { title: s.changed ? "Reset to default" : "As Relay ships it",
            disabled: !s.changed, onclick: async () => {
              if (!await confirmDialog("Reset to default?", `${s.name}: its name, prompt and schedule go back to `
                + "Relay's, and it's turned on.", { confirm: "Reset" })) return;
              await api.post(`/api/schedules/${s.id}/reset`).catch((error) => toast(error.message, "error"));
              load();
            } }, icon("undo", "sm"))
          : h("button.icon-btn.sm", { title: "Delete", onclick: async () => {
            if (!await confirmDialog("Delete this task?", `${s.name} won't run any more. Its chats stay.`,
              { confirm: "Delete", danger: true })) return;
            await api.del(`/api/schedules/${s.id}`).catch((error) => toast(error.message, "error"));
            load();
          } }, icon("trash", "sm"))));
  }

  // Editing one of Relay's own tasks: a word first (unless you said not to again).
  async function editSystem(s) {
    let skip = false;
    try { skip = localStorage.getItem(storeKey("relay.systemTaskWarning")) === "off"; } catch { /* (ask) */ }
    if (skip) { form(s); return; }
    const again = h("input", { type: "checkbox" });
    const m = modal("Edit one of Relay's own tasks?");
    const go = h("button.btn.primary", { onclick: () => {
      if (again.checked) {
        try { localStorage.setItem(storeKey("relay.systemTaskWarning"), "off"); } catch { /* (only a convenience) */ }
      }
      m.close();
      form(s);
    } }, "Edit anyway");
    m.body.append(h("p", `${s.name} is one of Relay's own tasks. Your changes stay until you reset it to its `
      + "default; the Governor carries it out either way."),
    h("label.check", again, h("span", "Don't warn me again")));
    m.foot.append(h("button.btn", { onclick: () => m.close() }, "Cancel"), go);
  }

  // AgentOS's model ranking (relay/ranking.py): the Governor's weekly system task looks new models up.
  let allRanks = false;
  function rankingPanel(ranked, list, os) {
    const task = list.find((t) => t.system === "models-rank");
    const update = task && os.settings.enabled
      ? h("button.btn.small", { title: "Have the Governor look up new models now",
        onclick: async () => {
          try {
            const { conversation_id: id } = await api.post(`/api/schedules/${task.id}/run`);
            if (id) hooks.onOpen(id);
          } catch (error) {
            toast(error.message, "error");
          }
        } }, icon("refresh", "sm"), "Update") : null;
    const over = ranked?.of || ranked?.unknown?.length ? h("button.btn.small.ghost", {
      title: "Forget the scores kept: the next update looks every model up anew", onclick: async () => {
        if (!await confirmDialog("Start the ranking over?", "The scores kept go; the next update (or the weekly "
          + "task) looks every model up anew.", { confirm: "Start over", danger: true })) return;
        try {
          await api.del("/api/models/ranking");
          load();
        } catch (error) {
          toast(error.message, "error");
        }
      } }, "Start over") : null;
    const actions = update || over ? h("div.actions", over, update) : null;
    if (!ranked?.of) {
      return panel("Model ranking", actions, h("p.muted.empty", os.settings.enabled
        ? "Not ranked yet: the system task “Models: weekly ranking” has the Governor look the models up."
        : "With AgentOS on, the Governor ranks the models here by intelligence, from benchmarks."), "ranking");
    }
    const score = (n) => String(Math.round(n * 10) / 10);
    const row = (m) => h("div.os-row.rank-row",
      h("span.rank-num", `#${m.rank}`),
      h("div.os-row-main",
        h("div.name", m.name, m.estimated
          ? h("span.badge",
              { title: "Not on the index itself: placed on its scale from other benchmarks" }, "estimated") : null),
        h("div.sub.truncate", { title: m.note || "" },
          m.models.map((x) => `${hooks.providerName(x.provider_id)} · ${x.model}`)
          .join(", ") + (m.note ? ` · ${m.note}` : ""))),
      /^https?:\/\//.test(m.source || "")
        ? h("a.rank-score",
          { href: m.source, target: "_blank", rel: "noopener noreferrer", title: `${m.benchmark}: the source` },
          score(m.score))
        : h("span.rank-score", { title: m.benchmark || "" }, score(m.score)));
    const shown = allRanks ? ranked.models : ranked.models.slice(0, 10);
    const rest = ranked.models.length - shown.length;
    const toggle = ranked.models.length > 10 ? h("button.btn.small.ghost", { onclick: () => {
      allRanks = !allRanks;
      clear(body, rows());
    } }, allRanks ? "Show fewer" : `Show all ${ranked.models.length}`) : null;
    const notes = [`${ranked.benchmark || "Intelligence index"} · updated ${when(ranked.updated)}`,
      ranked.unscored.length ? `${ranked.unscored.length} not looked up yet` : null,
      ranked.unknown.length ? `no benchmark knows ${ranked.unknown.join(", ")}` : null,
      ranked.routers?.length ? `routers, a model per request: ${ranked.routers.join(", ")}` : null].filter(Boolean);
    const rows = () => [...shown.map(row), rest > 0 || allRanks ? toggle : null];
    const body = h("div.rank-list", rows());
    return panel("Model ranking", actions, [h("p.muted.os-kind-about", "#1 is the smartest. Shown where models are "
      + "chosen, and told to agents picking a subagent."), body,
      h("p.muted.small-text", notes.join(" · "))], "ranking");
  }

  // The Governor's settings: on or off, its approved models, its round and budget, what it asks first.
  function governorCard(os) {
    const s = os.settings;
    const save = async (change) => {
      try {
        await api.put("/api/agentos", change);
      } catch (error) {
        toast(error.message, "error");
      }
      load();
    };
    const enabled =
      h("input", { type: "checkbox", checked: s.enabled, onchange: () => save({ enabled: enabled.checked }) });
    const loop = h("select.select", { onchange: () => save({ loop: loop.value || null }), "aria-label": "Models" },
      h("option", { value: "" }, "Choose approved models…"),
      os.loops.map((l) => h("option", { value: l.id, selected: l.id === s.loop, title: l.entries.join(" → ") },
        `${l.name}: ${l.entries.join(" → ")}`)));
    const number = (key, min, step, label) => {
      const input = h("input.input.num-short", { type: "number", min, step, value: s[key], "aria-label": label });
      input.addEventListener("change", () => save({ [key]: Number(input.value) }));
      return input;
    };
    const governor = os.governor;
    return h("section.os-panel.governor-row",
      h("div.os-panel-head", h("h2.section-title", "Governor"),
        h("label.switch", { title: s.enabled ? "On" : "Off" }, enabled, h("span"))),
      h("div.os-fields",
        h("span.os-label", "Models"), loop,
        h("span.os-label", "Round"),
        h("div.row-inline", h("span", "every"), number("round_hours", 1, 1, "Hours between rounds"),
            h("span", "hours")),
        h("span.os-label", "Per wake-up"),
        h("div.row-inline", number("max_agents", 1, 1, "Agents per wake-up"), h("span", "agents"),
            h("span", "· $"), number("max_cost", 0, 0.5, "Cost per wake-up")),
        h("span.os-label", "Its agents"), h("label.check", { title: "Research, reviews and other judgment work "
          + "go to an agent on the Governor's own model (the default when it names none), not to the Governor itself" },
        h("input",
          { type: "checkbox", checked: s.own_model !== false, onchange: (e) => save({ own_model: e.target.checked }) }),
        h("span", "may use its own model")),
        h("span.os-label", "Ask me first"), h("div.os-checks", os.irreversible.map((capability) => {
          const box = h("input", { type: "checkbox", checked: s.ask_first.includes(capability), onchange: () =>
            save({ ask_first: box.checked ? [...s.ask_first, capability]
              : s.ask_first.filter((c) => c !== capability) }) });
          return h("label.check", box, h("span", CAPABILITY_NAMES[capability] || capability));
        }))),
      os.loops.length ? null : h("p.sub", "Make a fallback loop of capable models in Settings → Usage first."),
      h("p.sub.muted", "Woken when a task is due, a service fails, its agents are done or seem to go wrong, or for "
        + "its round; never just to look."),
      h("div.os-actions",
        governor ? h("button.btn.small", { onclick: () => hooks.onOpen(governor.id) },
          icon("chat", "sm"), "Open") : null,
        governor ? h("button.btn.small", { title: "What the Governor keeps in its own memory (Settings → Memory)",
          onclick: () => hooks.openMemory?.("governor") }, icon("book", "sm"),
        `Memory${os.memories ? ` (${os.memories})` : ""}`) : null,
        h("button.btn.small", { title: "Wake it now for a round", onclick: async () => {
          try {
            const { conversation_id: id } = await api.post("/api/agentos/wake");
            hooks.onOpen(id);
          } catch (error) {
            toast(error.message, "error");
          }
        } }, icon("send", "sm"), "Wake now")));
  }

  // A live service (relay/production.py): health, address, versions, and what you can do.
  // A row per service: its health and versions at a glance; what you can do with it is on its page (/s/<id>)
  function serviceRow(s) {
    const address = `${location.origin}${relayUrl(`/port/${s.public_port}/`)}`;  // (through Relay: relay.forward)
    const archived = s.versions.filter((v) => v.status === "archived");
    const failed = s.versions.filter((v) => v.status === "failed");
    const act = (run) => async () => {
      try {
        const result = await run();
        if (result?.message) toast(result.message);
      } catch (error) {
        toast(error.message, "error");
      }
      load();
    };
    const health = s.stopped ? ["stopped", "muted"] : !s.live ? ["nothing live", failed.length ? "bad" : "muted"]
      : s.health_ok === false ? ["failing", "bad"] : s.health_ok ? ["well", "good"] : ["not checked", "muted"];
    return h("div.os-row.service-row",
      healthDot(s),
      h("div.os-row-main",
        h("div.name",
          h("a.service-name", { href: relayUrl(`/s/${s.id}`), title: "Its page: health, versions, shell, log",
            onclick: (e) => { e.preventDefault(); hooks.openService?.(s.id); } }, s.name),
        h("a", { href: address, target: "_blank", rel: "noopener" }, `/port/${s.public_port}/`)),
        h("div.sub", { title: s.versions.map(provenance).join("\n\n") },
          s.live ? `version ${s.live.number} live` : "nothing live",
          archived.length ? ` · ${archived.length} archived` : "", failed.length ? ` · ${failed.length} failed` : "",
          ` · ${s.command}${s.health ? ` · checks ${s.health}` : ""}`),
        s.health_ok === false && !s.stopped ? clamped(s.health_detail || "", 1, "fail") : null),
      h(`span.os-state.${health[1]}`, health[0]),
      h("div.actions",
        managedToggle(s, (button, run) => act(run), () => {}),
        h("button.btn.small", { onclick: () => hooks.openService?.(s.id) }, icon("box", "sm"), "Open")));
  }

  async function serviceForm() {
    if (await serviceDialog()) load();
  }

  // AgentOS's audit trail (relay/agentos.py: AgentOS.event): what the Governor asked or claims, what Relay
  // did or checked itself, what you decided; the latest first, more on a click.
  function activityFeed(os) {
    const actor = { governor: "Governor", relay: "Relay", user: "You", agent: "Agent" };
    const shown = 12;
    // What changed something, or failed; the Governor's looking around (lists, reads, logs) only on asking.
    const looking = (e) => e.kind === "capability" && e.ok
      && /\.(list|read|logs|status)$/.test(e.detail?.capability || "");
    const row = (e) => h(`div.os-event${e.ok ? "" : ".fail"}`,
      h("span.os-event-time", when(e.at)), h(`span.os-actor.${e.actor}`, actor[e.actor] || e.actor),
      h("span.os-event-text", describeEvent(e)));
    const list = h("div.os-events");
    let all = false;
    const show = (count) => {
      const events = os.events.filter((e) => all || !looking(e));
      clear(list, events.slice(0, count).map(row));
      return events.length;
    };
    const total = show(shown);
    const more = total > shown ? h("button.btn.ghost.small", { onclick: () => { show(80); more.remove(); } },
      `Show ${Math.min(total, 80) - shown} more`) : null;
    const reads = h("label.check.os-reads", { title: "Also the Governor's lists, reads and logs" },
      h("input",
        { type: "checkbox",
          onchange: (e) => { all = e.target.checked; show(e.target.checked ? 80 : shown); more?.remove(); } }),
      h("span", "Lookups"));
    return panel("Activity", os.events.length ? reads : null,
      os.events.length ? [list, more] : h("p.muted.empty", "Nothing yet."), "activity-log");
  }

  function settledRow(i) {
    return h("div.os-row.intervention-row.settled",
      icon("check", "sm"),
      h("div.os-row-main", clamped(i.text, 2, "muted"),
        h("div.sub", `Settled by the Governor ${when(i.resolved)}: `, i.resolution || "")));
  }

  function interventionRow(i) {
    return h("div.os-row.intervention-row",
      icon("alert", "sm"),
      h("div.os-row-main", clamped(i.text, 3, "lead"),
        h("div.sub", when(i.created), i.updated ? ` · updated ${when(i.updated)}` : null,
          i.conversation_id ? [" · ", h("a", { href: relayUrl(`/c/${i.conversation_id}`),
          onclick: (e) => { e.preventDefault(); hooks.onOpen(i.conversation_id); } },
          i.repair_id ? "the repair" : "the Governor's run")] : null)),
      h("div.actions",
        // A provider's repair that's ready (provider.review): it's applied in its own chat
        i.repair_id && i.conversation_id && !i.link ? h("button.btn.small.primary", {  // (an old repair's: its chat)
          onclick: () => hooks.onOpen(i.conversation_id) }, icon("tool", "sm"), "Review and apply") : null,
        // A page of Relay's it's about (provider.create: adding a provider with an agent)
        i.link ? h("a.btn.small.primary", { href: relayUrl(i.link), onclick: (e) => {
          e.preventDefault();
          window.dispatchEvent(new CustomEvent("relay:navigate", { detail: i.link }));
        } }, icon("plus", "sm"), "Open") : null,
        h("button.btn.small", { onclick: async () => {
        await api.post(`/api/agentos/interventions/${i.id}/resolve`).catch((error) => toast(error.message, "error"));
        load();
      } }, icon("check", "sm"), "Resolved")));
  }

  function form(existing = null, prefill = {}) {
    editing = true;
    hooks.onForm?.(existing?.id || "new");
    el.classList.remove("os-wide");
    const data = existing ? { ...existing } : {
      name: "", prompt: "", mode: "chat", target: "new", enabled: true,
      governed: !prefill.conversation_id && included(),
      when: { kind: "daily", time: "08:00" }, provider_id: hooks.defaultProvider?.(), ...prefill,
    };
    data.timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    const name = h("input.input", { value: data.name, placeholder: "e.g. Morning summary" });
    const prompt = h("textarea.textarea", { rows: 5, placeholder: "What should be done each time, e.g. Look for new "
      + "election data and add it to my ElectionAtlas site" });
    prompt.value = data.prompt;
    const setup = h("div");
    const whenBox = h("div");
    // What does it: the Governor, an agent you choose, or a script of yours (Relay runs it, with no agent)
    data.runs = data.script ? "script" : data.governed ? "governor" : "agent";
    let scripts = null;
    const renderSetup = async () => {
      const who = (value, label) => h(`button${data.runs === value ? ".on" : ""}`, { type: "button",
        onclick: () => { data.runs = value; data.governed = value === "governor"; renderSetup(); } }, label);
      const whoToggle = data.target === "same" ? null
        : h("div.mode-toggle", included() ? who("governor", "The Governor decides") : null,
          who("agent", "An agent you choose"), who("script", "A script of yours"));
      promptField.hidden = data.runs === "script";
      if (data.runs === "script") {
        scripts ??= await api.get("/api/scripts").then((found) => found.scripts.map((x) => x.name)).catch(() => []);
        // A new task's: the first one. One whose script is gone keeps it, said so, until you choose another (it
        // went to the first one, silently, a simulated user found)
        if (!data.script) data.script = scripts[0] || "";
        const missing = data.script && !scripts.includes(data.script);
        const script = h("select.select", { "aria-label": "Script", onchange: () => { data.script = script.value; } },
          missing ? h("option", { value: data.script, selected: true }, `${data.script} (gone)`) : null,
          scripts.map((name) => h("option", { value: name, selected: name === data.script }, name)));
        const args = h("input.input", { value: data.script_args || "", placeholder: "Arguments (optional)",
          "aria-label": "Arguments", oninput: () => { data.script_args = args.value; } });
        clear(setup, whoToggle,
          h("p.muted", scripts.length ? "Relay runs it at its times, as you, with no agent; its output is on the "
            + "Scripts page." : "No scripts yet: make one on the Scripts page."),
          scripts.length ? h("div.row-inline", script, args) : null);
        return;
      }
      if (data.governed) {
        clear(setup, whoToggle,
          h("p.muted", "The Governor chooses the agents, follows their work and checks it; a project folder tells "
            + "it where the work is (optional)."),
          h("div.row-inline", h("button.picker", { type: "button", onclick: async () => {
            const chosen = await hooks.pickFolder(data.directory);
            if (chosen) { data.directory = chosen; data.mode = "code"; renderSetup(); }
          } }, icon("folder", "sm"),
            h("span.truncate", data.directory ? shortPath(data.directory) : "Project folder (optional)")),
          data.directory ? h("button.icon-btn.sm", { title: "No folder", onclick: () => {
            data.directory = null; data.mode = "chat"; renderSetup();
          } }, icon("x", "sm")) : null));
        return;
      }
      const choices = hooks.providers().filter((p) => data.mode === "chat" || p.tools);
      if (!choices.some((p) => p.id === data.provider_id)) data.provider_id = choices[0]?.id || "";
      const provider =
        h("select.select", { onchange: () => { data.provider_id = provider.value; data.model = ""; renderSetup(); } },
          choices.map((p) => h("option", { value: p.id, selected: p.id === data.provider_id }, p.name)));
      const model = h("select.select", { onchange: () => { data.model = model.value; } },
        h("option", { value: "" }, "Default model"));
      hooks.modelsFor(data.provider_id).then((models) => {
        model.append(...models.filter((m) => m.id)
          .map((m) => h("option", { value: m.id, selected: m.id === data.model }, m.name || m.id)));
        if (data.model
          && !models.some((m) => m.id === data.model)) model.append(h("option",
              { value: data.model, selected: true }, data.model));
      });
      const modes = hooks.modesFor(data.provider_id);
      const mode = h("select.select", { onchange: () => { data.agent_mode = mode.value || null; } },
        h("option", { value: "" }, modes.some((m) => m.id === "build") ? "Build" : "No mode"),
        modes.filter((m) => m.id !== "build")
          .map((m) => h("option", { value: m.id, selected: m.id === data.agent_mode }, m.name)));
      const kind = (value, label) => h(`button${data.mode === value ? ".on" : ""}`, { type: "button",
        onclick: () => { data.mode = value; renderSetup(); } }, icon(value, "sm"), label);
      clear(setup, whoToggle,
        data.target === "same" ? h("p.muted", "It goes on in the chat it was made from.")
          : h("div.mode-toggle", kind("chat", "Chat"), kind("code", "Code task")),
        data.mode === "code" && data.target !== "same" ? h("div.row-inline",
          h("button.picker", { type: "button", onclick: async () => {
            const chosen = await hooks.pickFolder(data.directory);
            if (chosen) { data.directory = chosen; renderSetup(); }
          } }, icon("folder", "sm"),
            h("span.truncate", data.directory ? shortPath(data.directory) : "Choose project folder"))) : null,
        h("div.row-inline", provider, model, mode));
    };
    const renderWhen = () => {
      const w = data.when;
      const kind = h("select.select", { onchange: () => {
        data.when = { kind: kind.value, time: w.time || "08:00", minutes: w.minutes || 60, days: w.days || [0],
          at: w.at || Math.ceil(Date.now() / 3600000) * 3600 };
        renderWhen();
      } }, KINDS.map(([id, label]) => h("option", { value: id, selected: id === w.kind }, label)));
      const time =
        h("input.input", { type: "time", value: w.time || "08:00", onchange: () => { w.time = time.value; } });
      let detail = null;
      if (w.kind === "interval") {
        const hours = (w.minutes || 60) % 60 === 0;
        const amount = h("input.input", { type: "number", min: 1, value: hours ? (w.minutes || 60) / 60 : w.minutes });
        const unit = h("select.select", h("option", { value: "60", selected: hours }, "hours"),
          h("option", { value: "1", selected: !hours }, "minutes"));
        const update = () => { w.minutes = Math.round(Number(amount.value) * Number(unit.value)); };
        amount.addEventListener("change", update);
        unit.addEventListener("change", update);
        detail = [h("span.muted", "every"), amount, unit];
      } else if (w.kind === "once") {
        const local = new Date((w.at || Date.now() / 1000) * 1000);
        const pad = (n) => String(n).padStart(2, "0");
        const stamp = h("input.input", { type: "datetime-local",
          value:
            `${local.getFullYear()}-${pad(local.getMonth() + 1)}-${pad(local.getDate())}T`
            + `${pad(local.getHours())}:${pad(local.getMinutes())}`,
          onchange: () => { w.at = new Date(stamp.value).getTime() / 1000; } });
        detail = [stamp];
      } else {
        detail = [h("span.muted", "at"), time];
        if (w.kind === "weekly") {
          detail.push(h("span.day-picks", DAYS.map((day, i) => {
            const box = h("input", { type: "checkbox", checked: (w.days || []).includes(i), onchange: () => {
              w.days = box.checked ? [...new Set([...(w.days || []), i])] : (w.days || []).filter((d) => d !== i);
            } });
            return h("label.day", box, day);
          })));
        }
      }
      clear(whenBox, h("div.row-inline", kind, ...detail));
    };
    const promptField = h("label.field", h("span", data.governed ? "Task" : "Prompt"), prompt);
    renderSetup();
    renderWhen();
    const save = h("button.btn.primary", { onclick: async () => {
      const body =
        { ...data, name: name.value, prompt: prompt.value, script: data.runs === "script" ? data.script : null };
      if (body.script) delete body.provider_id;  // (a script runs as itself)
      save.disabled = true;
      try {
        if (existing) await api.put(`/api/schedules/${existing.id}`, body);
        else await api.post("/api/schedules", body);
        toast(existing ? "Saved" : "Scheduled");
        load();
      } catch (error) {
        toast(error.message, "error");
        save.disabled = false;
      }
    } }, existing ? "Save" : "Schedule");
    clear(el,
      h("div.pane-head", h("button.icon-btn", { title: "Back", onclick: load }, icon("arrowLeft")),
        h("h1.grow", existing ? `Edit ${existing.name}` : "New task")),
      h("label.field", h("span", "Name"), name),
      promptField,
      h("div.field", h("span", "Who"), setup),
      h("div.field", h("span", "When"), whenBox, h("span.hint", `Times are in ${data.timezone}.`)),
      h("div.row-between", h("button.btn", { onclick: load }, "Cancel"), save));
    name.focus();
  }

  // One task's form, as the side column's AgentOS view asks for it (a system task's own one), or a new task's
  async function openTask(id) {
    let task;
    try {
      task = (await api.get("/api/schedules")).find((s) => s.id === id);
    } catch (error) {
      toast(error.message, "error");
      return;
    }
    if (!task) return load();
    return task.kind === "system" ? editSystem(task) : form(task);
  }

  return {
    el,
    load: () => (hooks.prefill ? form(null, hooks.prefill) : load()),
    // (the page's poll: what's there kept current, a form being filled in left as it is)
    refresh: () => (editing ? null : load()),
    openTask,
    newTask: () => form(),
  };
}
