// Slash commands: Relay's own (/mode, /effort, /rewind, …), skills and the agent's.

import { relayPath } from "./base.js";
import { api } from "./api.js";
import {
  clearContext, firstWords, forkConversation, moveToCode, openConversation,
  renderTimed, retry, rewindCommand, rewindPoints, sendOrQueue, stopRun,
} from "./conversation.js";
import { openDrive } from "./drive.js";
import { fallbackLoops, loopChoices, modeChoices } from "./pickers.js";
import {
  exportConversation, loadServices, navigate, promoteClone,
  scheduleFromChat, settingsHooks, showNewChat, updateConversation,
} from "./main.js";
import { inputTokens, setShowDetails, setShowThinking, showingDetails, showingThinking } from "./render.js";
import { openSettings } from "./settings.js";
import { serviceDialog } from "./service.js";
import { hasShell, openShell } from "./terminal.js";
import { dom, modelsFor, modesFor, provider, saveDraft, state } from "./state.js";
import { h, modal, promptDialog, toast } from "./ui.js";

function settingCommands({ providerId, modeId, model, effort, efforts, set }) {
  const found = (items, arg) => items.find((i) => i.id.toLowerCase() === arg.toLowerCase()
    || (i.name || "").toLowerCase() === arg.toLowerCase());
  const check = (on) => (on ? "✓ " : "");
  const commands = [];
  const modes = modesFor(providerId);
  if (modes.length) {
    const cycle = modeChoices(providerId);
    commands.push({
      id: "mode", icon: "tool", arg: "choice", description: `switch mode (${modes.map((m) => m.id).join(", ")})`,
      choices: () => modes.map((m) => ({ value: m.id, icon: m.icon || "tool",
        label: `${check((modeId || "build") === m.id)}${m.name}${m.access === "read" ? " · read-only" : ""}` })),
      cycle: cycle.length > 1 ? async () => {
        const selected = modeId || (provider(providerId)?.tools ? "build" : null);
        const index = cycle.findIndex((m) => m.id === selected);
        const next = cycle[(index + 1) % cycle.length];
        await set({ mode: next.id === "build" ? null : next.id });
        toast(`${next.name}${next.id ? " mode" : ""}`);
      } : null,
      run: async (arg) => {
        const mode = found(modes, arg);
        if (!mode) throw new Error(`No mode “${arg}”; there's ${modes.map((m) => m.id).join(", ")}`);
        await set({ mode: mode.id === "build" ? null : mode.id });
        toast(`${mode.name} mode`);
      },
    });
  }
  commands.push({
    id: "model", icon: "brain", arg: "choice", description: "switch model",
    choices: async () => (await modelsFor(providerId)).map((m) => ({ value: m.id || "default",
      label: `${check((model || "") === m.id)}${m.name || m.id || "Default model"}` })),
    run: async (arg) => {
      const models = (await modelsFor(providerId)).map((m) => ({ id: m.id || "default", name: m.name }));
      const chosen = found(models, arg);
      await set({ model: chosen ? (chosen.id === "default" ? "" : chosen.id) : arg });  // or one it doesn't list
      toast(`Model: ${chosen?.name || arg}`);
    },
  });
  commands.push({
    id: "effort", icon: "brain", arg: "choice", description: "how hard the model thinks (low … max)",
    choices: async () => {
      const levels = await efforts();
      if (!levels.length) return [{ value: "default", label: "This model has no effort levels" }];
      return ["default", ...levels].map((level) => ({ value: level,
        label: `${check((effort || "default") === level)}${level === "default" ? "Default" : level}` }));
    },
    run: async (arg) => {
      const levels = await efforts();
      const level = arg.toLowerCase();
      if (level !== "default" && !levels.includes(level)) {
        throw new Error(levels.length ? `Effort is one of ${levels.join(", ")}, or default`
          : "This model has no effort levels");
      }
      await set({ effort: level === "default" ? null : level });
      toast(level === "default" ? "Effort: the model's default" : `Effort: ${level}`);
    },
  });
  return commands;
}

// /thinking and /details: how much of an agent's work is shown, in this browser.
function displayCommands() {
  const toggle = (id, description, get, apply) => ({
    id, icon: "list", arg: "optional", description,
    choices: () =>
      [{ value: "show", label: `${get() ? "✓ " : ""}Show` }, { value: "hide", label: `${get() ? "" : "✓ "}Hide` }],
    run: (arg) => {
      const show = arg ? arg.toLowerCase() !== "hide" : !get();
      apply(show);
      toast(`${id === "thinking" ? "Thinking" : "Tool details"}: ${show ? "shown" : "hidden"}`);
    },
  });
  return [
    toggle("thinking", "expand or collapse the model's thinking", showingThinking, setShowThinking),
    toggle("details", "expand or collapse tool calls", showingDetails, setShowDetails),
  ];
}

// Skills, called like a command: `/code-review the parser` goes to the agent
// as a message, and Relay adds the skill's instructions. `taken`: names the
// agent or Relay use themselves, which win.
function skillCommands(taken = []) {
  return (state.skills || []).filter((s) => !taken.includes(s.name)).map((s) => ({
    id: s.name, agent: true, skill: true, icon: "brain",
    // its first sentence (not cut at an "e.g. ")
    description: s.description.split(/(?<=[^\s.]{2}\.)\s+(?=[A-Z])/)[0] }));
}

function helpCommand(list, more = "") {
  return { id: "help", icon: "info", description: "what the / commands do", run: () => showHelp(list(), more) };
}

export function draftCommands(refresh) {
  const draft = state.draft;
  const list = () => [
    ...settingCommands({
      providerId: draft.providerId, modeId: draft.agentMode, model: draft.model, effort: draft.effort,
      efforts: () =>
        api.get(`/api/providers/${draft.providerId}/efforts?model=${encodeURIComponent(draft.model || "")}`)
        .catch(() => []),
      set: async (changes) => {
        if ("mode" in changes) draft.agentMode = changes.mode;
        if ("model" in changes) { draft.model = changes.model; draft.effort = null; }
        if ("effort" in changes) draft.effort = changes.effort;
        saveDraft();
        refresh();
      },
    }),
    { id: "loop", icon: "refresh", arg: "choice",
      description: "the fallback loop this chat follows when its model stops: automatic, a loop, or none",
      choices: async () => {
        await fallbackLoops();
        return loopChoices(draft.fallbackLoop || "auto").map((c) => ({ ...c, icon: "refresh" }));
      },
      run: async (arg) => {
        const found = await findLoop(arg);
        if (!found) return;
        draft.fallbackLoop = found;
        saveDraft();
        refresh();
      } },
    ...displayCommands(),
  ];
  const all = () => { const own = list(); return [...own, ...skillCommands(own.map((c) => c.id))]; };
  // (a new chat's are its settings: a simulated user read this list as all there are)
  return [...all(), helpCommand(all, "Once the chat has started, there are more: /retry, /stop, /timed, /fork, /new, "
    + "/rename, /clear, /schedule, /usage and /export; in a code task also /rewind, /mount and /shell.")];
}

// What the chat's agent offers besides Relay: its own commands and effort levels.
export async function loadAgentInfo() {
  const conv = state.current;
  if (!conv) return;
  state.agentInfo = { id: conv.id, commands: [], efforts: [], loading: true };
  try {
    const info = await api.get(`/api/conversations/${conv.id}/commands`);
    if (state.current?.id === conv.id) state.agentInfo = { id: conv.id, ...info };
  } catch {
    if (state.current?.id === conv.id) state.agentInfo = { id: conv.id, commands: [], efforts: [] };
  }
}

async function agentEfforts() {
  if (state.agentInfo?.loading) await loadAgentInfo();
  return state.agentInfo?.id === state.current?.id ? state.agentInfo.efforts : [];
}

export function conversationCommands() {
  const conv = state.current;
  if (!conv) return [];
  const code = conv.mode === "code" && conv.git?.state !== "discarded";
  const list = () => [
    ...settingCommands({
      providerId: conv.provider_id, modeId: conv.agent_mode, model: conv.model, effort: conv.effort,
      efforts: agentEfforts,
      set: async (changes) => {
        const body = {};
        if ("mode" in changes) body.agent_mode = changes.mode || "build";
        if ("model" in changes) { body.model = changes.model; body.effort = "default"; }
        if ("effort" in changes) body.effort = changes.effort || "default";
        if (!(await updateConversation(conv.id, body))) {
          throw Object.assign(new Error("not saved"), { told: true });  // said why already; no "done"
        }
        dom.composer?.refreshPickers();
        if ("model" in changes) loadAgentInfo();
      },
    }),
    ...displayCommands(),
    { id: "new", icon: "edit", description: "a new chat with the same provider, model and mode",
      run: () => newChatLike(conv) },
    ...(hasShell(conv)
        ? [{ id: "shell", icon: "terminal", description: "a shell in this task's WorkBox, under the chat",
        run: () => openShell(conv) }] : []),
    { id: "fork", icon: "fork", description: "go on in a copy of this chat", run: () => forkConversation(conv.id) },
    // (/promote: AgentOS's ProductionBoxes, only while AgentOS is part of this Relay)
    ...(!state.agentos ? [] : conv.clone?.own ? [{ id: "promote", icon: "rocket",
      description: `make this WorkBox ${conv.clone.service}'s live version (once its health check passes)`,
      run: () => promoteClone(state.current) }]
      : code ? [{ id: "promote", icon: "rocket",
        description: "make this project a ProductionBox (a live service); this task goes on as its WorkBox",
        run: async () => {
          if (await serviceDialog({ folder: conv.directory, conversationId: conv.id, ask: sendOrQueue })) {
            loadServices();
            await openConversation(conv.id);
          }
        } }] : []),
    { id: "clear", icon: "x", arg: "optional",
      description: "the agent forgets this conversation and starts afresh (you still see it); delete-history: "
        + "gone for you too",
      choices: () => [{ value: "delete-history", icon: "trash",
        label: "delete-history: delete the messages for good, for you and the agent" }],
      run: (arg) => {
        if (arg && arg.toLowerCase() !== "delete-history") throw new Error("/clear takes only delete-history");
        return clearContext(Boolean(arg));
      } },
    ...(conv.mode === "chat"
        ? [{ id: "code", icon: "code", description: "move this chat to a code task in a project folder",
        run: () => moveToCode(conv) }] : []),
    ...(code ? [{ id: "mount", icon: "monitor",
      description: "open this task's folder as a network drive on your computer",
      run: () => openDrive(conv) }] : []),
    ...(code ? [{
      id: "rewind", icon: "undo", arg: "optional",
      description: "put the files back to before an answer (or n answers back)",
      run: (arg) => rewindCommand(arg),
      choices: () => rewindPoints().slice(0, 8).map((point, i) => ({ value: String(i + 1), icon: "undo",
        label: `${i === 0 ? "Last answer" : `${i + 1} answers back`}: before “${firstWords(point.prompt)}”` })),
    }] : []),
    { id: "schedule", icon: "bell", arg: "text", description: "send a message here later, once or again and again",
      run: (arg) => scheduleFromChat(conv, arg) },
    { id: "retry", icon: "refresh", description: "answer the last message again", run: () => retry() },
    { id: "loop", icon: "refresh", arg: "choice",
      description: "the fallback loop this chat follows when its model stops: automatic, a loop, or none",
      choices: async () => {
        await fallbackLoops();
        return loopChoices(state.current.fallback_loop).map((c) => ({ ...c, icon: "refresh" }));
      },
      run: (arg) => setLoop(arg) },
    { id: "stop", icon: "stop", description: "stop the agent", run: () => stopRun() },
    { id: "timed", icon: "clock", arg: "text",
      description: "keep the agent at it: [min] max [optimal] in minutes (or 1h30), then the task; off stops the timer",
      run: (arg) => timedCommand(arg) },
    { id: "rename", icon: "edit", arg: "text", description: "rename this chat",
      run: async (arg) => {
        const title = arg || await promptDialog("Rename chat", { value: conv.title });
        if (title && title.trim()) await updateConversation(conv.id, { title: title.trim() });
      } },
    { id: "export", icon: "file", description: "download this chat as Markdown",
      run: () => exportConversation(conv.id) },
    { id: "usage", icon: "list", description: "tokens and cost of this chat", run: () => showUsage(conv) },
    // The agent's own, e.g. /init, /review, /compact, and yours: passed on as a message.
    ...(state.agentInfo?.id === conv.id ? state.agentInfo.commands : [])
      .filter((c) => !["mode", "model", "effort", "help", "clear"].includes(c.name))  // (Relay's own)
      .map((c) => ({ id: c.name, agent: true, description: `${c.description || "the agent's command"}` +
        ` (${provider(conv.provider_id)?.name || "agent"})` })),
  ];
  const all = () => {
    const own = list();
    return [...own, ...skillCommands([...own.map((c) => c.id), "help"])];
  };
  return [...all(), helpCommand(all)];
}

// /new: the same provider, model, mode and effort (and folder, for a code task).
function newChatLike(conv) {
  Object.assign(state.draft, { mode: conv.computer ? "computer" : "chat", providerId: conv.provider_id,
    model: conv.model || "", agentMode: conv.agent_mode, effort: conv.effort });
  if (conv.computer) state.draft.computerId = conv.computer.id;
  else {
    state.draft.inFolder = conv.mode === "code";
    // A task on its own branch works in a copy; the folder picked was in the repository.
    if (conv.mode === "code") state.draft.directory = conv.git?.mode === "worktree"
      ? conv.git.repo_root : conv.directory;
  }
  saveDraft();
  navigate("/");
  if (relayPath() === "/") showNewChat();
}

function showHelp(commands, more = "") {
  const m = modal("Commands");
  m.foot.remove();
  const row = (c) => h("div.help-row", h("code", `/${c.id}`), h("span", c.description));
  const own = commands.filter((c) => !c.agent);
  const agent = commands.filter((c) => c.agent && !c.skill);
  const skills = commands.filter((c) => c.skill);
  m.body.append(...[  // (append would show a null as the text "null")
    h("p.muted", "Type / in the message box. Relay's commands are done right away; the agent's go to it as a message, "
      + "and so does anything else starting with /."),
    h("div.help-list", own.map(row)),
    more ? h("p.muted", more) : null,
    agent.length ? h("h3.section-gap", "The agent's own") : null,
    agent.length ? h("div.help-list", agent.map(row)) : null,
    skills.length ? h("h3.section-gap", "Skills") : null,
    skills.length ? h("p.muted", "Sent to the agent with the skill's instructions; add what you want after it, "
      + "e.g. /code-review only the parser.") : null,
    skills.length ? h("div.help-list", skills.map(row)) : null,
  ].filter(Boolean));
}

function showUsage(conv) {
  const totals = { input: 0, output: 0 };
  const costs = {};
  let runs = 0;
  for (const message of conv.messages || []) {
    const usage = [...(message.events || [])].reverse().find((e) => e.type === "usage");
    if (!usage) continue;
    runs += 1;
    const t = usage.tokens || {};
    totals.input += inputTokens(t);
    totals.output += t.output_tokens ?? t.output ?? t.completion_tokens ?? 0;
    const kind = usage.billing || "unknown";
    costs[kind] = (costs[kind] || 0) + Number(usage.cost || 0);
  }
  const money = (v) => `$${v.toFixed(v && v < 1 ? 4 : 2)}`;
  const lines = [
    `${runs} answer${runs === 1 ? "" : "s"} · ${totals.input.toLocaleString()} tokens in · `
    + `${totals.output.toLocaleString()} out`,
    costs.api ? `API (pay per use): ${money(costs.api)}` : null,
    "subscription" in costs
      ? `In plan: no extra cost${costs.subscription ? ` (≈ ${money(costs.subscription)} at API prices)` : ""}` : null,
    "free" in costs ? "Free or local models: no cost" : null,
    costs.unknown ? `Other: ${money(costs.unknown)}` : null,
  ].filter(Boolean);
  const m = modal("Usage of this chat");
  m.foot.remove();
  m.body.append(...lines.map((line) => h("p", line)));
}

// /timed [min] max [optimal] [task] (relay/timed.py): before min the agent is told to go on, before optimal
// asked once, at max stopped; the task goes as the next message. /timed off ends it; /timed alone says how it stands.
async function timedCommand(arg) {
  const conv = state.current;
  const words = (arg || "").trim();
  const minutes = (moment) => Math.round((moment - conv.timed.started) / 60);
  const told = (timer) => [timer.min ? `at least ${Math.round((timer.min - timer.started) / 60)}` : null,
    timer.optimal !== timer.min ? `ideally ${Math.round((timer.optimal - timer.started) / 60)}` : null,
    // (the unit last: always there)
    `at most ${Math.round((timer.max - timer.started) / 60)} min`].filter(Boolean).join(", ");
  if (!words) {
    const going = conv.timed && !conv.timed.ended && conv.timed.max > Date.now() / 1000;
    toast(going ? `Timed: ${told(conv.timed)} (${minutes(Date.now() / 1000)} min gone)`
      : "/timed [min] max [optimal] [task]: minutes (or 1h30); before min the agent goes on, at max it stops");
    return;
  }
  const off = words.toLowerCase() === "off";
  const updated = await api.post(`/api/conversations/${conv.id}/timed`, off ? { off: true } : { text: words });
  if (state.current === conv) Object.assign(conv, { timed: updated.timed });
  renderTimed();
  if (off) {
    toast("Timer stopped: the agent may stop when it's done");
    return;
  }
  toast(`Timed: ${told(updated.timed)}`);
  if (updated.task) await sendOrQueue(updated.task);
}

// /loop [name]: the fallback loop this chat follows; without one: manage them (Settings → Usage).
async function findLoop(arg) {
  if (!arg) {
    openSettings({ ...settingsHooks(), tab: "usage" });
    return null;
  }
  const loops = await fallbackLoops();
  const wanted = arg.trim().toLowerCase();
  const found = ["auto", "off"].includes(wanted) ? wanted
    : loops.find((l) => l.id === arg.trim() || l.name.toLowerCase() === wanted)?.id;
  if (!found) throw new Error(`No loop “${arg}”; there's ${["auto", "off", ...loops.map((l) => l.name)].join(", ")}`);
  return found;
}

async function setLoop(arg) {
  const found = await findLoop(arg);
  if (!found) return;
  if (!(await updateConversation(state.current.id, { fallback_loop: found }))) {
    throw Object.assign(new Error("not saved"), { told: true });
  }
  dom.composer?.refreshPickers();
  const loops = state.loops || [];
  toast(found === "auto" ? "Fallback: the loop the model is in" : found === "off" ? "No fallback loop"
    : `Fallback loop: ${loops.find((l) => l.id === found).name}`);
}
