// What the whole app shares: its state, the long-lived elements and the data it loads.

import { storeKey } from "./base.js";
import { api } from "./api.js";
import { toast } from "./ui.js";

export const app = document.getElementById("app");

export const state = {
  user: null,
  loginRequired: true,
  version: "",
  providers: [],
  models: new Map(), // provider id -> Promise<models>
  conversations: [],
  taskCounts: null,
  governorId: null, // AgentOS's Governor's conversation (the sidebar's Gov.)
  services: null, // the live services (the sidebar's Production boxes); null until loaded
  current: null, // the open conversation, with messages
  following: null, // { runId, source, view }
  search: "",
  draft: loadDraft(),
};

export const dom = {}; // long-lived elements of the layout

// The draft's mode: "chat" or "computer"; a chat in its folder (`inFolder`) is a code task. A new chat starts without
// one (inFolder is off at the start, and once a chat is started); `directory` is the folder picked last: its picker
// opens there.
function loadDraft() {
  try {
    const draft = { mode: "chat", providerId: null, model: "", directory: "",
      ...JSON.parse(localStorage.getItem(storeKey("relay.draft")) || "{}"), temporary: false, inFolder: false };
    if (draft.mode === "code") draft.mode = "chat";  // (before they were one)
    return draft;
  } catch {
    return { mode: "chat", providerId: null, model: "", directory: "" };
  }
}

export function saveDraft() {
  try { localStorage.setItem(storeKey("relay.draft"), JSON.stringify(state.draft)); } catch { /* private mode */ }
  // What a new chat starts with is the user's, on every device: Relay keeps the provider, model and effort
  const choice = newChatChoice();
  if (remembered !== null && choice.provider_id && JSON.stringify(choice) !== remembered) {
    remembered = JSON.stringify(choice);
    api.put("/api/new-chat", choice).catch(() => { remembered = ""; });  // (said again with the next change)
  }
}

let remembered = null;  // (what Relay has as the user's choice for new chats; null until it's been read)

function newChatChoice() {
  return { provider_id: state.draft.providerId || null, model: state.draft.model || null,
    effort: state.draft.effort || null };
}

// At the start: the choice for new chats as Relay has it (picked last, maybe in another browser), over this one's
export function useNewChat(found) {
  if (found?.provider_id) {
    Object.assign(state.draft,
      { providerId: found.provider_id, model: found.model || "", effort: found.effort || null });
    try { localStorage.setItem(storeKey("relay.draft"), JSON.stringify(state.draft)); } catch { /* private mode */ }
  }
  remembered = JSON.stringify(found?.provider_id ? newChatChoice() : {});
}

export async function loadProviders() {
  try {
    const data = await api.get("/api/providers");
    state.providers = data.providers.filter((p) => p.enabled);
    state.models.clear();
  } catch (error) {
    toast(error.message, "error");
  }
}

export async function loadModes() {
  try {
    state.modes = await api.get("/api/modes");
  } catch (error) {
    toast(error.message, "error");
  }
}

export async function loadSkills() {
  try {
    state.skills = (await api.get("/api/skills")).skills;
  } catch {
    state.skills = [];
  }
}

export function provider(id) {
  return state.providers.find((p) => p.id === id);
}

export function modelsFor(providerId) {
  if (!state.models.has(providerId)) {
    const request = api.get(`/api/providers/${providerId}/models`)
      .then((d) => d.models)
      .catch(() => []);
    state.models.set(providerId, request);
    // Only remember real lists: an agent may still be starting up, so ask
    // again next time when nothing (or just the default) came back.
    request.then((models) => {
      if (models.filter((m) => m.id).length === 0) state.models.delete(providerId);
    });
  }
  return state.models.get(providerId);
}

export function eligibleProviders(mode) {
  return state.providers.filter((p) => mode === "chat" || p.tools);  // code and computer tasks need tools
}

// The user's computers (relay/computers.py), for computer tasks.
export async function loadComputers() {
  try {
    state.computers = await api.get("/api/computers");
  } catch {
    state.computers = [];
  }
  return state.computers;
}

// A model's effort levels, asked once per page load.
const effortsCache = new Map();

export function effortsFor(providerId, model) {
  const key = `${providerId}/${model}`;
  if (!effortsCache.has(key)) {
    effortsCache.set(key, api.get(`/api/providers/${providerId}/efforts?model=${encodeURIComponent(model)}`)
      .catch(() => { effortsCache.delete(key); return []; }));
  }
  return effortsCache.get(key);
}

// Modes a provider can use: all for agents; people's own for chat APIs
// (Build and Plan are about tools).
export function modesFor(providerId) {
  const tools = Boolean(provider(providerId)?.tools);
  return (state.modes || []).filter((m) => tools || !m.tools_only);
}
