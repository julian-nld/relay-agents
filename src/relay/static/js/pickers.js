// The pickers under the message box: provider, model (with its efforts), mode, permissions, folder.

import { api } from "./api.js";
import { settingsHooks } from "./main.js";
import { pickFolder } from "./picker.js";
import { openSettings } from "./settings.js";
import { dom, effortsFor, eligibleProviders, modelsFor, modesFor, provider, saveDraft, state } from "./state.js";
import { clock, h, icon, promptDialog, shortPath, showMenu, statusDot, toast } from "./ui.js";

export function computerPicker(onChange) {
  const chosen = (state.computers || []).find((c) => c.id === state.draft.computerId);
  const button = h("button.picker", { type: "button", title: "The computer the work happens on",
    onclick: () => showMenu(button, [
      ...(state.computers || []).map((c) => ({
        label: `${c.id === state.draft.computerId ? "✓ " : ""}${c.name}`, icon: "monitor",
        status: c.online ? "ok" : "fail", title: c.online ? "Online" : "Offline: its connector isn't running",
        onclick: () => { state.draft.computerId = c.id; saveDraft(); onChange(); },
      })),
      "-", { label: "Manage computers…", icon: "settings",
        onclick: () => openSettings({ ...settingsHooks(), tab: "computers" }) },
    ]) },
  icon("monitor", "sm"), h("span.truncate", chosen ? chosen.name : "Choose computer"),
  chosen ? statusDot(chosen.online ? "ok" : "fail", chosen.online ? "Online" : "Offline")
    : null, icon("chevronDown", "sm"));
  return button;
}

export function providerPicker(selectedId, mode, onChange) {
  const selected = provider(selectedId);
  const button = h("button.picker", { type: "button", title: "Provider",
    onclick: () => showMenu(button, eligibleProviders(mode).map((p) => ({
      label: p.id === selectedId ? `${p.name}  ✓` : p.name,
      icon: p.tools ? "code" : "chat",
      // Only a warning: the model used last failed (e.g. a usage limit).
      status: p.last_status && !p.last_status.ok ? "fail" : null,
      title: p.last_status && !p.last_status.ok ? statusText(p.last_status) : null,
      onclick: () => onChange(p.id),
    })).concat(["-", { label: "Manage providers…", icon: "settings",
      onclick: () => openSettings({ ...settingsHooks(), tab: "providers" }) }])),
  }, icon(selected?.tools ? "code" : "chat", "sm"), h("span.truncate", selected?.name || "Choose provider"),
  icon("chevronDown", "sm"));
  return button;
}

export function modelPicker(providerId, model, onChange, effort = null) {
  if (!providerId) return null;
  const withEffort = (name) => (effort ? `${name} · ${effort}` : name);
  const label = h("span.truncate", withEffort(model || "Default model"));
  const button = h("button.picker", { type: "button", title: "Model",
    onclick: async () => {
      const models = await modelsFor(providerId);
      // Gone meanwhile (another chat on screen): no menu for it there (a slow list's opened on the next one)
      if (!button.isConnected) return;
      // Hovering a model shows its effort levels beside it; picking one sets both.
      const levels = (id) => async () => {
        const found = await effortsFor(providerId, id);
        if (!found.length) return [];
        const current = id === (model || "") ? effort || "default" : null;
        return ["default", ...found].map((level) => ({
          label: `${current === level ? "✓ " : ""}${level === "default" ? "Default effort" : level}`,
          onclick: () => onChange(id, level === "default" ? null : level),
        }));
      };
      const items = models.map((m) => ({
        label: (m.id === (model || "") ? "✓ " : "") + (m.name || m.id || "Default model"),
        hint: m.rank ? `#${m.rank}` : null,  // (AgentOS's intelligence ranking: relay/ranking.py)
        status: m.status ? (m.status.ok ? "ok" : "fail") : null,
        title: [m.rank ? `Intelligence index: #${m.rank} (AgentOS's ranking; #1 is the smartest)` : null,
          m.status ? statusText(m.status) : null].filter(Boolean).join("\n") || null,
        onclick: () => onChange(m.id),
        submenu: levels(m.id),
      }));
      if (!models.some((m) => m.id === "")) {
        items.unshift({ label: (model ? "" : "✓ ") + "Default model",
          onclick: () => onChange(""), submenu: levels("") });
      }
      items.push("-", { label: "Custom model…", icon: "edit", onclick: async () => {
        const custom = await promptDialog("Custom model", { label: "Model id", value: model,
          placeholder: "e.g. openrouter/anthropic/claude-sonnet-4" });
        if (custom !== null) onChange(custom.trim());
      } }, { label: `Test ${model || "the default model"}`, icon: "refresh",
        onclick: () => testModel(providerId, model) },
      { label: "All models and tests…", icon: "settings",
        onclick: () => openSettings({ ...settingsHooks(), tab: "providers", models: providerId }) });
      showMenu(button, items);
    },
  }, label, icon("chevronDown", "sm"));
  modelsFor(providerId).then((models) => {
    const found = models.find((m) => m.id === model);
    if (found?.name) label.textContent = withEffort(found.name);
  });
  return button;
}

// A model's last result, e.g. "Failed 5 min ago: usage limit reached".
export function statusText(status) {
  const minutes = Math.round((Date.now() / 1000 - status.checked) / 60);
  const when = minutes < 1 ? "just now" : minutes < 60 ? `${minutes} min ago`
    : minutes < 60 * 24 ? `${Math.round(minutes / 60)} h ago` : `${Math.round(minutes / 1440)} d ago`;
  const how = status.source === "test" ? "tested" : "used";
  const windows = Object.entries(status.usage?.windows || {}).map(([name, w]) =>
    `${name.replace("_", "-")} window ${Math.round((w.utilization || 0) * 100)}% used, resets ${clock(w.resets_at)}`);
  if (!status.ok && status.resets_at && status.resets_at * 1000 > Date.now()) {
    return `Usage limit reached; resets at ${clock(status.resets_at)}`;
  }
  return (status.ok ? `Worked (${how} ${when})` : `Failed (${how} ${when}): ${status.message}`)
    + (windows.length ? ` · ${windows.join(" · ")}` : "");
}

async function testModel(providerId, model) {
  toast(`Testing ${model || "the default model"}…`);
  try {
    const status = await api.post(`/api/providers/${providerId}/test`, { model: model || "" });
    toast(status.ok ? status.message : `Doesn't work: ${status.message}`, status.ok ? "success" : "error");
  } catch (error) {
    toast(error.message, "error");
  }
  state.models.delete(providerId);  // their statuses changed
  dom.composer?.refreshPickers();
}

// Fallback loops (Settings → Usage): who goes on when the chat's model stops.
export async function fallbackLoops() {
  state.loops = await api.get("/api/fallback-loops").catch(() => state.loops || []);
  return state.loops;
}

export function loopChoices(current) {
  const check = (on) => (on ? "✓ " : "");
  return [
    { value: "auto", label: `${check(current === "auto")}Automatic: the loop the chat's model is in` },
    ...(state.loops || []).map((loop) => ({ value: loop.id, label: `${check(current === loop.id)}${loop.name}` })),
    { value: "off", label: `${check(current === "off")}None: wait for the model's own limit to reset` },
  ];
}

// The loop that applies: the chosen one, none, or the first one the model is in.
export function loopFor(current, providerId, model) {
  const loops = state.loops || [];
  if (current === "off") return null;
  if (current && current !== "auto") return loops.find((l) => l.id === current) || null;
  return loops.find((l) =>
    l.entries.some((e) => e.provider === providerId && (e.model || "") === (model || ""))) || null;
}

// The chip under the message box: which fallback loop the chat follows (Settings → Usage, /loop).
export function loopPicker(current, providerId, model, onChange) {
  current = current || "auto";
  const loop = loopFor(current, providerId, model);
  const label = loop ? loop.name : current === "off" ? "No fallback" : "No fallback loop";
  const title = current === "auto"
    ? (loop ? `Fallback loop “${loop.name}”: the first one this model is in` : "This model is in no fallback loop: "
      + "when it stops, the chat waits for its limit to reset")
    : current === "off" ? "No fallback loop for this chat" : `Fallback loop “${label}”, chosen for this chat`;
  const button =
    h(`button.picker${current !== "auto" ? ".on" : ""}`, { type: "button", title: `${title} — or type /loop`,
      onclick: async () => {
        await fallbackLoops();
        if (!button.isConnected) return;  // (gone meanwhile: no menu for it on another screen)
        showMenu(button, [...loopChoices(current).map((c) => ({ label: c.label, icon: "refresh",
          onclick: () => onChange(c.value) })), "-",
        { label: "Manage loops…", icon: "settings",
          onclick: () => openSettings({ ...settingsHooks(), tab: "usage" }) }]);
      } }, icon("refresh", "sm"), h("span.truncate", label), icon("chevronDown", "sm"));
  return button;
}

export function modeChoices(providerId) {
  const p = provider(providerId);
  const choices = modesFor(providerId);
  if (!p || (!p.tools && !choices.length)) return [];
  return p.tools ? choices : [{ id: null, name: "No mode" }, ...choices];
}

export function modePicker(providerId, modeId, onChange) {
  const p = provider(providerId);
  const choices = modesFor(providerId);
  if (!p || (!p.tools && !choices.length)) return null;
  const current = choices.find((m) => m.id === modeId) || null;
  const defaultLabel = p.tools ? "Build" : "No mode";
  const items = modeChoices(providerId).map((m) => ({
    label: `${(current?.id ?? (p.tools ? "build" : null)) === m.id ? "✓ " : ""}${m.name}`
      + (m.access === "read" ? " · read-only" : ""),
    icon: m.icon || "tool",
    onclick: () => onChange(m.id === "build" ? null : m.id),
  }));
  items.push("-", { label: "Manage modes…", icon: "settings",
    onclick: () => openSettings({ ...settingsHooks(), tab: "modes" }) });
  const button = h(`button.picker${current && current.id !== "build" ? ".on" : ""}`, {
    type: "button",
    title: `Mode${current?.description ? `: ${current.description}` : ""} — /mode or Shift+Tab in the message box`,
    onclick: () => showMenu(button, items),
  }, icon((current || (p.tools && choices.find((m) => m.id === "build")))?.icon || "tool", "sm"),
  h("span.truncate", current?.name || defaultLabel), icon("chevronDown", "sm"));
  return button;
}

export function optionsLabel(providerId, modeId) {
  const mode = modesFor(providerId).find((m) => m.id === modeId && m.id !== "build");
  return mode ? `Options · ${mode.name}` : "Options";
}

// /mode, /model and /effort: for a chat (on the server) or a new chat's draft.
// A conversation's project folder: with one it's a code task, without one a chat. The picker
// opens in the folder picked last.
export function folderPicker({ directory, onChoose, onClear }) {
  const choose = async () => {
    const chosen = await pickFolder(state.draft.directory);
    if (chosen) {
      state.draft.directory = chosen;
      saveDraft();
      onChoose(chosen);
    }
  };
  const button = h("button.picker", { type: "button", title: directory || "Work in a project folder (a code task)",
    onclick: () => (directory ? showMenu(button, [
      { label: "Choose another folder…", icon: "folder", onclick: choose },
      { label: "No project folder", icon: "chat", onclick: onClear },
    ]) : choose()),
  }, icon("folder", "sm"), h("span.truncate", directory ? shortPath(directory) : "No project folder"));
  return button;
}
