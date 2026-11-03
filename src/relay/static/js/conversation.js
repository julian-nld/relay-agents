// An open conversation: its messages, following a run, retries, edits, rewinds and limits.

import { relayUrl } from "./base.js";
import { api } from "./api.js";
import { attachmentList } from "./attachments.js";
import { conversationCommands, loadAgentInfo } from "./commands.js";
import { createComposer } from "./composer.js";
import { filesButton } from "./drive.js";
import {
  exportConversation, loadConversations, navigate, renderHeader,
  renderSidebarList, showChanges, updateConversation, watcher,
} from "./main.js";
import { answeringAgent, currentLimit, firstShown, recoveryNotice } from "./outcomes.js";
import { pickFolder } from "./picker.js";
import {
  folderPicker, loopPicker, modelPicker, modePicker, optionsLabel, providerPicker, statusText,
} from "./pickers.js";
import { AssistantView, setProjectFolder, stopWatchingSubagents, userMessage } from "./render.js";
import { dom, eligibleProviders, modelsFor, provider, state } from "./state.js";
import { ThreadScroll } from "./thread-scroll.js";
import { followShell, shellButton } from "./terminal.js";
import { clear, clock, confirmDialog, debounce, h, icon, modal, shortPath, showMenu, toast } from "./ui.js";

async function changeMode(id) {
  const conv = state.current;
  if (!conv || (conv.agent_mode || null) === (id || null)) return;
  if (!(await updateConversation(conv.id, { agent_mode: id || "build" }))) return;
  dom.composer?.refreshPickers();
}

// Every navigation takes a new number, so a conversation that loads after the
// user already went elsewhere isn't shown.
let openRequest = 0;

// Navigating elsewhere: a conversation still loading isn't shown.
export function forgetOpening() {
  openRequest += 1;
}

export async function openConversation(id, runId = null) {
  const request = ++openRequest;
  const composer = state.current?.id === id ? dom.composer : null;
  stopFollowing();
  // Until it's loaded, the chat on screen can't take a message meant for this one.
  for (const child of dom.main.children) child.inert = true;
  dom.main.classList.add("loading");
  let conv;
  try {
    conv = await api.get(`/api/conversations/${id}`);
  } catch (error) {
    if (request !== openRequest) return;
    toast(error.message, "error");
    navigate("/");
    return;
  }
  if (request !== openRequest) return;
  state.current = conv;
  followShell(conv);  // (the Shell, when it's open: this chat's WorkBox now)
  // A quick retry may already be in the history; don't append its replay twice.
  const followRun = conv.messages.some((m) => m.run_id === runId) ? conv.running_run : runId || conv.running_run;
  renderConversation(followRun, composer?.el.isConnected ? composer : null);
  renderSidebarList();
}

export function renderConversation(followRun = null, composer = null) {
  const conv = state.current;
  const mine = () => state.current?.id === conv.id;  // (this chat still the one on screen)
  setProjectFolder(conv.directory);
  dom.main.classList.remove("loading");
  watcher.setTitle(`${conv.title} · Relay`);
  const listed = state.conversations.find((c) => c.id === conv.id);
  if (listed?.last_run) watcher.markSeen(conv.id, listed.last_run.id);
  dom.header = h("header.header");
  dom.thread = h("div.thread");
  dom.scroller = h("div.scroller", dom.thread);
  dom.scroll?.destroy();
  dom.scroll = new ThreadScroll(dom.scroller, dom.thread);
  dom.scroller.addEventListener("scroll", () => {
    dom.main.classList.toggle("scrolled", dom.scroller.scrollTop > 4);
  });
  dom.composer = composer || createComposer({
    placeholder: "Message",
    running: Boolean(followRun),
    onSend: (text, attachments, shown) => sendMessage(text, attachments, shown),
    onStop: () => stopRun(),
    onQueue: (text, attachments) => queueMessage(text, attachments),
    commands: () => conversationCommands(),
    // state.current, not conv: finishing a run replaces the conversation object. What's picked goes to this chat,
    // and only while it's the one on screen (a menu still open from another changes nothing here).
    pickers: () => ({ primary: [
      providerPicker(state.current.provider_id, state.current.mode, (id) => mine() && changeProvider(id)),
      modelPicker(state.current.provider_id, state.current.model || "", (model, effort) => {
        if (!mine()) return;
        updateConversation(conv.id, effort === undefined ? { model } : { model, effort: effort || "default" })
          .then(() => { if (mine()) { dom.composer.refreshPickers(); loadAgentInfo(); } });
      }, state.current.effort),
      state.current.mode === "code" && state.current.git?.state !== "discarded" ? filesButton(state.current) : null,
      // A plain chat can still get a project folder: it goes on there as a code task.
      movable(state.current) ? folderPicker({ directory: "", onChoose: (chosen) => moveToCode(state.current, chosen) })
        : null,
      shellButton(state.current),
    ], advanced: [
      modePicker(state.current.provider_id, state.current.agent_mode, (id) => mine() && changeMode(id)),
      loopPicker(state.current.fallback_loop, state.current.provider_id, state.current.model, async (value) => {
        if (!mine() || !(await updateConversation(conv.id, { fallback_loop: value })) || !mine()) return;
        dom.composer?.refreshPickers();
        renderLimit();
      }),
    ], label: optionsLabel(state.current.provider_id, state.current.agent_mode) }),
  });
  if (composer) {
    composer.setRunning(Boolean(followRun));
    composer.refreshPickers();
  }
  const where = conv.git?.mode === "worktree"
    ? `in ${shortPath(conv.git.repo_root)} on branch ${conv.git.branch} (merge it into ${conv.git.base_branch} when `
      + "you're happy)"
    : `in ${shortPath(conv.directory)}`;
  const note = conv.temporary ? temporaryNote(conv)
    : h("div.composer-note", { title: conv.mode === "code" ? conv.directory : null }, conv.mode === "code"
      ? `The agent works ${where} and can run commands there.`
      : conv.computer ? `The agent works on ${conv.computer.name || "a computer that was removed"}`
        + ` (${conv.computer.online ? "online" : "offline: start its connector"}) and can run commands there.`
        : "Answers can be wrong. Check important information.");
  dom.queue = h("div.queued");
  dom.repair = h("div");
  dom.timed = h("div");
  const bottom = conv.git?.state === "discarded"
    ? h("div.task-closed", "This task was discarded. Start a new code task to continue.")
    : h("div", dom.repair, dom.timed, dom.queue, dom.composer.el, note);
  renderLimit();
  renderTimed();
  renderRepair();
  clear(dom.main, dom.header, h("div.thread-pane", dom.scroller, dom.scroll.button), h("div.composer-wrap", bottom));
  renderHeader();

  if (conv.parent) {
    dom.thread.append(h("div.thread-note", icon("fork", "sm"), h("span", "Subagent of ",
      h("a", { href: `/c/${conv.parent.id}`,
        onclick: (e) => { e.preventDefault(); navigate(`/c/${conv.parent.id}`); } },
        conv.parent.title))));
  }
  if (conv.copy) dom.thread.append(copyNote(conv));
  if (conv.forked_from) {
    const origin = conv.origin
      ? h("a", { href: `/c/${conv.origin.id}`,
        onclick: (e) => { e.preventDefault(); navigate(`/c/${conv.origin.id}`); } },
        conv.origin.title)
      : "a chat that was deleted";
    dom.thread.append(h("div.thread-note", icon("fork", "sm"), h("span", "Forked from ", origin)));
  }
  // A long thread from its last messages on; the rest a part at a time (firstShown)
  dom.shownFrom = firstShown(conv.messages);
  if (dom.shownFrom > 0) dom.thread.append(earlierButton());
  dom.thread.append(...messageElements(conv, dom.shownFrom, conv.messages.length));
  if (followRun) follow(followRun, { catchUp: true });
  else decorateTurns();
  renderQueue();
  // after follow(): whether a run is going decides what it offers
  if (state.agentInfo?.id !== conv.id) loadAgentInfo();
  scrollToBottom(true);
  dom.composer.focus();
}

// The thread's messages `from` … `to` (indexes), with what comes between them: a note where another agent or mode
// took over, and where the agent's memory was cleared (/clear).
function messageElements(conv, from, to) {
  const out = [];
  // Who gave the answer before `from`
  let answered = answeredBy(conv.messages.slice(0, from).findLast((m) => m.role === "assistant")) || null;
  // The clear note goes before the first message after it; after them all when none is
  const after = conv.cleared_after ? conv.messages.findIndex((m) => m.id > conv.cleared_after) : -2;
  const clearAt = after === -1 ? conv.messages.length : after;
  const clearNote = () => h("div.clear-note", { title: "The agent doesn't see the messages above (/clear)" },
    icon("x", "sm"), "Cleared: the agent starts afresh from here");
  for (let i = from; i < to; i += 1) {
    const message = conv.messages[i];
    if (i === clearAt) out.push(clearNote());
    if (message.role === "user") {
      const note = message.relay_action ? null : switchNote(answered, answeringAgent(conv, i));
      if (note) out.push(note);
      out.push(message.relay_action ? relayMessage(message)
        : userMessage(message.content, attachmentList(message.attachments), message.created));
    } else {
      const view = new AssistantView();
      view.created = message.created;
      for (const event of message.events || []) view.add(event);
      if (!view.status) view.finish(message.status || "failed");
      if (message.changes) view.setChanges(message.changes, showChanges, conv.git);
      out.push(view.el);
      answered = answeredBy(message) || answered;
    }
  }
  if (to === conv.messages.length && clearAt === conv.messages.length) out.push(clearNote());
  return out;
}

// Above a long thread's messages on screen: the ones before them, a part at a time, where you're reading.
function earlierButton() {
  const button = h("button.btn.small.earlier-messages", { type: "button" });
  const label = () => {
    button.textContent = `Show ${dom.shownFrom} earlier message${dom.shownFrom === 1 ? "" : "s"}`;
  };
  button.onclick = () => {
    const conv = state.current;
    if (!conv || !button.isConnected) return;
    const to = dom.shownFrom;
    const from = firstShown(conv.messages.slice(0, to));
    const height = dom.scroller.scrollHeight;
    button.after(...messageElements(conv, from, to));
    dom.shownFrom = from;
    if (from === 0) button.remove();
    else label();
    dom.scroller.scrollTop += dom.scroller.scrollHeight - height;  // (where you were reading stays put)
    decorateTurns();
  };
  label();
  return button;
}

// Copy and the next action stay visible; less frequent actions share More.
function decorateTurns() {
  dom.thread?.querySelectorAll(".turn-action").forEach((el) => el.remove());
  const conv = state.current;
  if (!conv || state.following || conv.git?.state === "discarded") return;
  // Only the chat's own messages: opened subagent cards have messages inside them. A long thread's earlier ones
  // aren't on screen yet (dom.shownFrom).
  const elements = [...dom.thread.querySelectorAll(":scope > .msg")];
  const from = dom.shownFrom || 0;
  if (elements.length !== conv.messages.length - from) return;  // not in sync yet; the next render is
  const rewindable = Boolean(provider(conv.provider_id)?.rewindable);
  const lastQuestion = conv.messages.map((m) => m.role).lastIndexOf("user");
  conv.messages.forEach((message, i) => {
    if (i < from) return;
    const element = elements[i - from];
    const actions = element.querySelector(":scope > .msg-actions");
    if (!actions || !message.id) return;
    if (message.role === "user") {
      actions.append(turnAction("Edit", "edit", "Edit and send again",
        () => editMessage(element, message, i < lastQuestion)));
      return;
    }
    const failed = message.status === "failed" || message.status === "interrupted";
    const latest = i === conv.messages.length - 1;
    const beforeInfo = (button) => actions.insertBefore(button, actions.querySelector(".info, .cost"));
    if (latest && failed) {
      // (a model that can't be used: another one is the way on, not trying it again)
      const unusable = Boolean(currentLimit(conv)?.unusable);
      const another = h(`button.btn.small.recovery-action${unusable ? ".primary" : ""}`,
        { onclick: () => continueElsewhere(another) }, "Choose another model");
      const body = element.querySelector(":scope > .body");
      const details = limitRecovery(conv);
      const notice = recoveryNotice(conv);
      const recovery = h("div.turn-recovery.turn-action",
        notice ? h("p.recovery-notice", notice) : null,
        details.text ? h("details.recovery-details", h("summary", "Retry details"), h("p", details.text)) : null,
        h("div.recovery-buttons",
          h(`button.btn.small.recovery-action${unusable ? "" : ".primary"}`, {
            title: "Send your last message again with the current model", onclick: () => retry() },
          icon("refresh", "sm"), "Retry"), unusable ? null : another, unusable ? another : null, details.buttons));
      const error = body.querySelector(":scope > .run-error")
        || [...body.querySelectorAll(":scope > .run-note")].at(-1);
      if (error) error.after(recovery);
      else body.append(recovery);
    } else if (latest && rewindable) {
      const button = turnAction("Regenerate", "refresh", "Generate this answer again", () => retry());
      button.classList.add("recovery-action");
      beforeInfo(button);
    }
    const more = turnAction("More", "more", "More answer actions", () => showMenu(more, [
      { label: "Fork from here", icon: "fork", onclick: () => forkConversation(conv.id, message.id) },
      { label: "Export chat as Markdown", icon: "file", onclick: () => exportConversation(conv.id) },
      ...(message.checkpoint ? [{ label: "Rewind files to before this answer…", icon: "undo",
        onclick: () =>
          rewindFiles(message, conv.messages.slice(0, i).findLast((m) => m.role === "user")?.content) }] : []),
    ]));
    beforeInfo(more);
  });
  updateRecoveryButtons();
}

function turnAction(label, iconName, title, onclick) {
  return h("button.btn.small.ghost.turn-action", { title, onclick }, icon(iconName, "sm"), label);
}

const recovering = new Set();

function updateRecoveryButtons() {
  const busy = recovering.has(state.current?.id) || Boolean(state.following || state.current?.running_run);
  dom.thread?.querySelectorAll(".recovery-action").forEach((button) => { button.disabled = busy; });
}

// changes: {} to answer the last message again, or { message_id, text } to
// answer an earlier or edited one (the chat is rewound to it).
export async function retry(changes = {}) {
  const conv = state.current;
  if (!conv || recovering.has(conv.id) || state.following || conv.running_run) return;
  recovering.add(conv.id);
  updateRecoveryButtons();
  try {
    let result;
    try {
      result = await api.post(`/api/conversations/${conv.id}/retry`, changes);
    } catch (error) {
      if (!error.data?.others) throw error;
      const again = await confirmDialog("Also undo other commits?",
        `${error.message} Their changes stay in the history.`, { confirm: "Rewind anyway", danger: true });
      if (!again) return;
      result = await api.post(`/api/conversations/${conv.id}/retry`, { ...changes, force: true });
    }
    if (state.current !== conv) {
      loadConversations();
      return;
    }
    if (result.rewound || conv.messages.findLast((m) => m.role === "user")?.relay_action) {
      await openConversation(conv.id, result.run_id);  // what came after it is gone
    } else {
      const lastUser = [...conv.messages].reverse().find((m) => m.role === "user");
      conv.messages.push({ role: "user", content: lastUser.content, attachments: lastUser.attachments });
      dom.thread.append(userMessage(lastUser.content, attachmentList(lastUser.attachments)));
      follow(result.run_id);
      scrollToBottom(true);
    }
    loadConversations();
  } catch (error) {
    toast(error.message, "error");
  } finally {
    recovering.delete(conv.id);
    updateRecoveryButtons();
  }
}

async function editMessage(element, message, earlier) {
  const conv = state.current;
  const index = conv.messages.indexOf(message);
  // The files before the answer to this message, to go back to them as well.
  const checkpoint = conv.mode === "code" && conv.messages[index + 1]?.checkpoint;
  let rewindToo = false;
  if (earlier || conv.mode === "code") {
    const after = conv.messages.length - index - 1;
    const notes = [];
    if (after) notes.push(`The ${after} message${after === 1 ? "" : "s"} after it will be removed.`);
    if (conv.mode === "code") {
      notes.push(checkpoint
        ? "The files can stay as they are, or be rewound to how they were before this message."
        : "Files the agent changed stay as they are.");
    }
    const answer = await confirmDialog("Edit this message?", notes.join(" "),
      { confirm: "Edit", alternative: checkpoint ? "Edit and rewind files" : null });
    if (!answer) return;
    rewindToo = answer === "alternative";
  }
  const bubble = element.querySelector(".bubble");
  const original = message.content || "";
  const area = h("textarea.edit-area", { value: original, rows: Math.min(10, original.split("\n").length + 1) });
  const replaced = bubble || element.querySelector(".msg-actions");
  const editor = h("div.edit-box", area, h("div.edit-actions",
    h("button.btn.small", { onclick: () => { editor.replaceWith(replaced); decorateTurns(); } }, "Cancel"),
    h("button.btn.small.primary", { onclick: () => {
      if (area.value.trim()) {
        retry({ message_id: message.id, text: area.value, ...(rewindToo ? { restore_files: true } : {}) });
      }
    } }, "Save & send")));
  element.querySelectorAll(".turn-action").forEach((el) => el.remove());
  replaced.replaceWith(editor);
  area.focus();
}

// /rewind: the answers whose files can be put back, newest first, with the
// message each answered.
export function rewindPoints() {
  const messages = state.current?.messages || [];
  const points = [];
  messages.forEach((m, i) => {
    if (m.role !== "assistant" || !m.checkpoint) return;
    const asked = messages.slice(0, i).reverse().find((q) => q.role === "user");
    points.push({ answer: m, prompt: asked?.content || "" });
  });
  return points.reverse();
}

export function firstWords(text, max = 50) {
  const flat = String(text || "").split(/\s+/).join(" ").trim();
  return flat.length > max ? `${flat.slice(0, max - 1)}…` : flat;
}

// "/rewind" is the last answer, "/rewind 3" three answers back.
export async function rewindCommand(arg) {
  const points = rewindPoints();
  const back = arg ? Number.parseInt(arg, 10) : 1;
  if (!points.length) {
    toast("There's nothing to rewind yet", "error");
    return;
  }
  if (!Number.isInteger(back) || back < 1 || back > points.length) {
    toast(`Rewind 1 to ${points.length} answers back`, "error");
    return;
  }
  const point = points[back - 1];
  await rewindFiles(point.answer, point.prompt);
}

// A code subagent works in a copy of its caller's folder. Changes it didn't hand over (it was
// stopped, failed, conflicted, or you went on with it here) wait for you: apply or discard them.
function copyNote(conv) {
  const files = conv.copy.files;
  const shown = files.slice(0, 6).join(", ") + (files.length > 6 ? ` and ${files.length - 6} more` : "");
  const act = async (action) => {
    if (action === "discard" && !await confirmDialog("Discard these changes?",
      `What this subagent changed in its copy (${shown}) is thrown away; `
      + `${shortPath(conv.copy.source)} stays as it is.`,
      { confirm: "Discard", danger: true })) return;
    try {
      const result = await api.post(`/api/conversations/${conv.id}/copy`, { action });
      if (action === "discard") toast("Changes discarded");
      else if (result.conflicts?.length) toast(
        `Applied; sort out the conflict markers in ${result.conflicts.join(", ")}`, "error");
      else toast(`Applied to ${shortPath(conv.copy.source)}`);
      conv.copy = null;
      if (state.current === conv) note.remove();
    } catch (error) {
      toast(error.message, "error");
    }
  };
  const note = h("div.thread-note.copy-note", icon("fork", "sm"),
    h("span", `Changes not in ${shortPath(conv.copy.source)} yet: ${shown}.`),
    h("button.btn.small.primary", { onclick: () => act("apply") }, "Apply"),
    h("button.btn.small", { onclick: () => act("discard") }, "Discard"));
  return note;
}

// Put a code task's files back as they were before an answer, from Relay's own record (a task on
// its own branch: a new commit there), so nothing is lost and it can be undone.
async function rewindFiles(message, prompt = "") {
  const conv = state.current;
  const ok = await confirmDialog("Rewind the files?",
    `The files go back to how they were before “${firstWords(prompt, 80)}”: what that answer and later ones ` +
    "changed is undone (it stays in the history). The chat stays as it is.", { confirm: "Rewind" });
  if (!ok) return;
  const rewind = async (body) => {
    try {
      const result = await api.post(`/api/conversations/${conv.id}/rewind`, body);
      conv.changes = result.changes;  // the header shows the task's changes from here
      if (state.current === conv) renderHeader();
      return result;
    } catch (error) {
      if (error.data?.others) return { others: error.data.others, error: error.message };
      toast(error.message, "error");
      return null;
    }
  };
  let result = await rewind({ run_id: message.run_id });
  if (result?.others) {
    // Other commits touched these files since: yours, or another task's.
    const again = await confirmDialog("Also undo other commits?",
      `${result.error} Their changes stay in the history.`, { confirm: "Rewind anyway", danger: true });
    result = again ? await rewind({ run_id: message.run_id, force: true }) : null;
  }
  if (result) {
    toast("Files rewound", "info", { label: "Undo", onclick: () => rewind({ checkpoint: result.undo })
      .then((r) => r && toast("Files as they were again")) });
  }
}

// Plain chats only: not a computer task, a subagent's chat or one of AgentOS's own.
function movable(conv) {
  return conv.mode === "chat" && !conv.computer && !conv.parent && !conv.agentos_role && !conv.temporary;
}

// Under a temporary chat's message box: what it is, and Keep it (a chat like any other from then on)
function temporaryNote(conv) {
  const note = h("div.composer-note.temporary-note", icon("temporary", "sm"),
    h("span", "Temporary chat: in no list, without memory. It goes when you leave it."),
    h("button.link-btn", { onclick: async () => {
      if (!(await updateConversation(conv.id, { temporary: false }))) return;
      note.replaceWith(h("div.composer-note", "Kept: it's in your chats now."));
      dom.composer?.refreshPickers();  // (a folder can be chosen for it now)
    } }, "Keep it"));
  return note;
}

// /clear: the agent forgets the conversation so far (a fresh session); it stays on screen, unless
// `deleteHistory` (/clear delete-history): then it's gone for good.
export async function clearContext(deleteHistory = false) {
  const conv = state.current;
  if (!conv) return;
  await api.post(`/api/conversations/${conv.id}/clear`, { delete_history: deleteHistory });
  toast(deleteHistory ? "History deleted: the conversation starts over"
    : "Cleared: the agent starts afresh with your next message");
  if (state.current?.id === conv.id) await openConversation(conv.id);
}

// A chat becomes a code task: pick the project folder (and a provider with
// tools, if this one has none); the conversation goes on there. With a folder
// already picked and an agent on it, at once.
export async function moveToCode(conv, directory = null) {
  const choices = eligibleProviders("code");
  if (!choices.length) {
    toast("Code tasks need a provider with tools", "error");
    return;
  }
  if (directory && provider(conv.provider_id)?.tools) {
    await moveChat(conv, directory, conv.provider_id);
    return;
  }
  const choice = { directory: directory || state.draft.directory || null,
    providerId: provider(conv.provider_id)?.tools ? conv.provider_id : choices[0].id };
  const m = modal("Move to a code task");
  const row = h("div.pickers.move-pickers");
  const go = h("button.btn.primary", { onclick: async () => {
    if (!choice.directory) {
      toast("Choose a project folder first", "error");
      return;
    }
    go.disabled = true;
    if (await moveChat(conv, choice.directory, choice.providerId)) m.close();
    else go.disabled = false;
  } }, icon("code", "sm"), "Move");
  const render = () => clear(row,
    h("button.picker", { type: "button", title: choice.directory || "Project folder", onclick: async () => {
      const chosen = await pickFolder(choice.directory);
      if (chosen) { choice.directory = chosen; render(); }
    } }, icon("folder", "sm"),
      h("span.truncate", choice.directory ? shortPath(choice.directory) : "Choose project folder")),
    providerPicker(choice.providerId, "code", (id) => { choice.providerId = id; render(); }));
  render();
  m.body.append(h("p.muted", "The conversation goes on in a project folder, where the agent can read and change "
    + "files. Agents that keep sessions per folder get the conversation so far handed over."), row);
  m.foot.append(h("button.btn", { onclick: () => m.close() }, "Cancel"), go);
}

async function moveChat(conv, directory, providerId) {
  try {
    const moved = await api.post(`/api/conversations/${conv.id}/to-code`, { directory, provider_id: providerId });
    // (The chat's files came along; one the project has already stayed where the chat had it.)
    toast(moved.left_behind ? `Now a code task in ${shortPath(directory)}. Not moved, as the project has them `
      + `already: ${moved.not_moved.join(", ")} (still in ${moved.left_behind})`
      : `Now a code task in ${shortPath(directory)}`,
    moved.left_behind ? "error" : "info");
    await loadConversations();
    if (state.current?.id === conv.id) await openConversation(conv.id);
    else navigate(`/c/${conv.id}`);
    return true;
  } catch (error) {
    toast(error.message, "error");
    return false;
  }
}

// A new provider's or a repair's agent (relay/creations.py): where it is, on its own page
function renderRepair() {
  const conv = state.current;
  if (!dom.repair || !conv?.repair) return;
  const repair = conv.repair;
  const path = `/providers/${repair.creates ? "new" : "repair"}/${repair.id}`;
  const open =
    (e) => { e.preventDefault(); window.dispatchEvent(new CustomEvent("relay:navigate", { detail: path })); };
  const said = { setup: "being started", planning: "being planned", planned: "planned: the plan waits for you",
    building: repair.creates ? "being built" : "being repaired", trying: "being tried in a throwaway Relay",
    applied: repair.creates ? "added to Relay" : "applied", undone: repair.creates ? "taken out again" : "undone",
    discarded: "cancelled", working: "made the old way", reviewed: "made the old way" }[repair.status] || repair.status;
  const what = repair.creates ? `integrates a new provider${repair.target_id ? ` (${repair.target_id})` : ""}`
    : `repairs ${provider(repair.target_id)?.name || repair.target_id}`;
  clear(dom.repair, h("div.repair-note", icon(repair.creates ? "plus" : "tool", "sm"), h("div.grow",
    h("p", `This agent ${what}: ${said}. `, h("a", { href: relayUrl(path), onclick: open }, "Its page"),
      " has the steps."))));
}

// Refresh the latest turn's recovery controls when its resume plan changes.
// /timed (relay/timed.py): how long the agent is kept at it, while the timer runs or once it ran out.
let timedTick = null;

export function renderTimed() {
  const conv = state.current;
  clearTimeout(timedTick);
  if (!dom.timed || !conv) return;
  const timer = conv.timed;
  const now = Date.now() / 1000;
  if (!timer || (timer.ended && timer.ended !== "max")) { clear(dom.timed); return; }
  if (timer.ended === "max" || now >= timer.max) {
    clear(dom.timed, h("div.timed-bar.over", icon("clock", "sm"),
      h("span.grow", `Stopped at ${clock(timer.max)}: the timed request's time was up.`),
      h("button.icon-btn.sm", { title: "Hide", onclick: () => stopTimer(true) }, icon("x", "sm"))));
    return;
  }
  const parts = [];
  if (timer.min && now < timer.min) parts.push(`it goes on until ${clock(timer.min)}`);
  if (timer.optimal !== timer.min && now < timer.optimal) parts.push(`ideally until ${clock(timer.optimal)}`);
  parts.push(`stops at ${clock(timer.max)}`);
  clear(dom.timed, h("div.timed-bar", icon("clock", "sm"), h("span.grow", `Timed · ${parts.join(" · ")}`),
    h("button.btn.small", { onclick: () => stopTimer() }, "Stop timer")));
  timedTick = setTimeout(renderTimed, 30000);  // (its times pass: "goes on until 12:06" isn't so at 12:07)
}

async function stopTimer(quietly = false) {
  const conv = state.current;
  try {
    const updated = await api.post(`/api/conversations/${conv.id}/timed`, { off: true });
    if (state.current === conv) Object.assign(conv, { timed: updated.timed });
    renderTimed();
    if (!quietly) toast("Timer stopped: the agent may stop when it's done");
  } catch (error) {
    toast(error.message, "error");
  }
}

// A message from a command (/timed's task): sent, or queued when the agent is still at work.
export function sendOrQueue(text) {
  return state.following || state.current?.running_run ? queueMessage(text, []) : sendMessage(text, [], []);
}

export function renderLimit() {
  decorateTurns();
}

function limitRecovery(conv) {
  const limit = currentLimit(conv);
  if (!limit) return { text: "", buttons: [] };
  const who = [provider(limit.provider_id)?.name || limit.provider_id, limit.model].filter(Boolean).join(" · ");
  const act = (body) => async () => {
    try {
      const updated = await api.post(`/api/conversations/${conv.id}/resume`, body);
      if (state.current?.id !== conv.id) { loadConversations(); return; }
      Object.assign(state.current, updated);
      renderLimit();
      renderHeader();  // (its "Resuming later" goes with the plan)
      loadConversations();
    } catch (error) {
      toast(error.message, "error");
    }
  };
  const why = limit.limit ? `${who} hit its usage limit` : limit.unusable ? `${who} can't be used here`
    : `${who} stopped without an answer`;
  let text;
  const buttons = [];
  const loop = limit.loop;
  const first = limit.restart && [provider(limit.restart.provider)?.name || limit.restart.provider,
    limit.restart.model].filter(Boolean).join(" · ");
  if (conv.resume_at && loop && first) {
    text =
      `${why}, and none of the agents in the fallback loop “${loop.name}” can go on now (round ${loop.round || 1}). `
      + `Relay starts the loop again with ${first} at ${clock(conv.resume_at)}`
      + `${limit.guessed ? ", as nobody said when their limits reset" : ""}.`;
    buttons.push(h("button.btn.small.recovery-action", { onclick: act({ cancel: true }) }, "Cancel"));
  } else if (limit.gave_up && loop) {
    text = `${why}. Relay went round the fallback loop “${loop.name}” ${loop.round || 1} times, `
      + "so it stopped going on by itself.";
  } else if (conv.resume_at) {
    text = limit.guessed
      ? `${why}, and didn't say for how long. Relay will tell it to go on at ${clock(conv.resume_at)}, once.`
      : `${why}. Relay will tell it to go on when it resets, at ${clock(conv.resume_at)}.`;
    buttons.push(h("button.btn.small.recovery-action", { onclick: act({ cancel: true }) }, "Cancel"));
  } else if (limit.gave_up && limit.unusable) {
    text = `${why} (it doesn't exist, this login can't use it, or it isn't offered where you are), so Relay doesn't `
      + "try it again by itself.";
  } else if (limit.gave_up) {
    text = `${why} ${limit.attempts} times in a row, so Relay stopped going on by itself.`;
  } else if (limit.resets_at && limit.resets_at * 1000 > Date.now()) {
    text = `${why}. It resets at ${clock(limit.resets_at)}.`;
    buttons.push(h("button.btn.small.recovery-action", { onclick: act({}) }, "Resume then"));
  } else {
    text = `${why}.`;
  }
  return { text, buttons };
}

// Pick a provider, then its model; it takes over without sending the composer draft.
function continueElsewhere(anchor) {
  const conv = state.current;
  if (!conv || state.following || conv.running_run || recovering.has(conv.id)) return;
  const needsTools = conv.mode === "code" || Boolean(conv.computer);
  const go = async (providerId, model) => {
    if (state.current !== conv || state.following || conv.running_run || recovering.has(conv.id)) return;
    recovering.add(conv.id);
    updateRecoveryButtons();
    try {
      const { run_id: runId } =
        await api.post(`/api/conversations/${conv.id}/continue`, { provider_id: providerId, model });
      if (state.current !== conv) { loadConversations(); return; }
      await openConversation(conv.id, runId);
      loadConversations();
    } catch (error) {
      toast(error.message, "error");
    } finally {
      recovering.delete(conv.id);
      updateRecoveryButtons();
    }
  };
  showMenu(anchor, state.providers.filter((p) => !needsTools || p.tools).map((p) => ({
    label: p.name, icon: p.tools ? "code" : "chat",
    status: p.last_status && !p.last_status.ok ? "fail" : null,
    onclick: async () => {
      const models = await modelsFor(p.id);
      if (state.current !== conv || !anchor.isConnected) return;
      const items = models.map((m) => ({ label: m.name || m.id || "Default model",
        status: m.status ? (m.status.ok ? "ok" : "fail") : null, title: m.status ? statusText(m.status) : null,
        onclick: () => go(p.id, m.id) }));
      if (!models.some((m) => m.id === "")) items.unshift({ label: "Default model", onclick: () => go(p.id, "") });
      showMenu(anchor, items);
    },
  })));
}

export async function forkConversation(id, messageId = null) {
  try {
    const fork = await api.post(`/api/conversations/${id}/fork`, messageId === null ? {} : { message_id: messageId });
    await loadConversations();
    navigate(`/c/${fork.id}`);
    toast("Forked: this is the copy; the original stays as it was");
  } catch (error) {
    toast(error.message, "error");
  }
}

// A line in the chat where another provider, model or mode took over.
function agentLabel(agent) {
  return agent?.model?.split("/").at(-1) || provider(agent?.provider_id)?.name || agent?.provider_id || "another model";
}

function agentDetails(agent) {
  return [provider(agent?.provider_id)?.name || agent?.provider_id, agent?.model].filter(Boolean).join(" · ");
}

function relayMessage(message) {
  const action = message.relay_action;
  const changed = action.kind !== "retry" && action.from?.provider_id
    && (action.from.provider_id !== action.to.provider_id
      || (action.from.model || "") !== (action.to.model || ""));
  const text = action.kind === "timed" ? `Relay kept ${agentLabel(action.to)} at it (the timed request)`
    : changed ? `${agentLabel(action.to)} took over from ${agentLabel(action.from)}`
    : `${action.kind === "resume" ? "Relay resumed" : action.kind === "retry" ? "Relay retried"
      : "Relay continued with"} ${agentLabel(action.to)}`;
  return h("div.msg.relay", h("details.relay-note",
    h("summary", icon(action.kind === "timed" ? "clock" : "refresh", "sm"), text),
    h("div.relay-note-details",
      h("p", [agentDetails(action.from), agentDetails(action.to)].filter(Boolean).join(" → ")),
      action.handover ? h("p", "The conversation so far was handed over.") : null,
      h("p.muted.small-text", "Relay sent this instruction to continue the task:"), h("pre", message.content))));
}

function switchNote(before, after) {
  if (!before?.provider || !after?.provider) return null;
  const otherAgent = before.provider !== after.provider || (before.model || "") !== (after.model || "");
  const otherMode = (before.mode || "build") !== (after.mode || "build");
  if (!otherAgent && !otherMode) return null;
  const p = provider(after.provider);
  const mode = (state.modes || []).find((m) => m.id === (after.mode || "build"));
  const parts = [];
  if (otherAgent) parts.push(`Now with ${agentLabel({ provider_id: after.provider, model: after.model })}`);
  if (otherMode) parts.push(`${otherAgent ? "" : "Now in "}${mode?.name || after.mode} mode`);
  const handedOver = before.provider !== after.provider && p?.tools;
  return h("details.switch-note", h("summary", parts.join(", ")),
    h("p", agentDetails({ provider_id: after.provider, model: after.model })),
    handedOver ? h("p.faint", "The conversation so far was handed over.") : null);
}

function answeredBy(message) {
  return message ? { provider: message.run_provider, model: message.run_model, mode: message.run_mode } : null;
}

async function changeProvider(id) {
  const conv = state.current;
  if (id === conv.provider_id) return;
  // An agent starts a new session and gets the conversation handed over.
  // (updateConversation copies the server's answer, its model the default now, into the chat on screen)
  if (!(await updateConversation(conv.id, { provider_id: id, model: null, effort: "default" }))) return;
  if (state.current?.id !== conv.id) return;
  loadAgentInfo();
  dom.composer.refreshPickers();
}

async function sendMessage(text, attachments = [], shown = []) {
  const conv = state.current;
  try {
    const { run_id: runId } = await api.post(`/api/conversations/${conv.id}/messages`,
      { text, attachments });
    if (state.current !== conv) {
      loadConversations();  // the user went elsewhere; the sidebar shows it running
      return true;
    }
    const sent = shown.filter((a) => a.id);
    const last = [...conv.messages].reverse().find((m) => m.role === "assistant");
    const note = switchNote(answeredBy(last), { provider: conv.provider_id, model: conv.model, mode: conv.agent_mode });
    if (note) dom.thread.append(note);
    conv.messages.push({ role: "user", content: text, attachments: sent });
    dom.thread.append(userMessage(text, attachmentList(sent)));
    follow(runId);
    scrollToBottom(true);
    loadConversations();
  } catch (error) {
    toast(error.message, "error");
    return false;
  }
  return true;
}

// `catchUp`: the chat is opened while its agent works. What it did so far comes in a burst; the turn is
// shown once that's in ("caught-up"), as a finished turn is, not with every step opening and closing on
// screen; from there on it's live.
function follow(runId, { catchUp = false } = {}) {
  stopFollowing(true);
  const scroll = dom.scroll;
  const view = new AssistantView({ live: true, runId, onActivity: () => scroll?.changed() });
  dom.thread.querySelectorAll(".turn-action").forEach((el) => el.remove());
  view.el.hidden = catchUp;
  dom.thread.append(view.el);
  dom.composer?.setRunning(true);
  const source = new EventSource(relayUrl(`/api/runs/${runId}/events`));
  source.addEventListener("caught-up", () => { view.el.hidden = false; });
  state.following = { runId, source, view };
  renderHeader();
  renderRepair();
  source.onmessage = (message) => {
    if (state.following?.source !== source) return;
    const event = JSON.parse(message.data);
    view.add(event);
    if (["form", "permission", "done"].includes(event.type) && state.current) {
      const wasWaiting = state.current.needs_approval;
      state.current.needs_approval = Boolean(pendingRequest());
      if (wasWaiting !== state.current.needs_approval) renderHeader();
    }
  };
  source.addEventListener("end", () => finishFollowing(runId));
  source.onerror = () => {
    // The browser reconnects by itself (resuming via Last-Event-ID); a
    // closed stream means the server refused it.
    if (source.readyState === EventSource.CLOSED) finishFollowing(runId);
  };
}

function pendingRequest() {
  const view = state.following?.view;
  if (!view || view.status) return null;
  for (const card of view.forms.values()) {
    if (card.event.status === "pending" && card.active) return card.el;
  }
  return [...view.permissions.values()].find((note) => note.classList.contains("ask")) || null;
}

export function focusPendingRequest() {
  const request = pendingRequest();
  if (!request) { scrollToBottom(true); return; }
  request.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
    block: "center" });
  request.querySelector("input:not(:disabled), textarea:not(:disabled), button:not(:disabled)")
    ?.focus({ preventScroll: true });
}

async function finishFollowing(runId) {
  if (state.following?.runId !== runId) return;
  state.following.source.close();
  const { view } = state.following;
  view.el.hidden = false;  // (a stream that ended before it caught up)
  if (!view.status) view.finish("failed");
  state.following = null;
  dom.composer?.setRunning(false);
  if (document.hidden && state.current) {
    // Announce it via the next list update, as for runs in other chats.
    const listed = state.conversations.find((c) => c.id === state.current.id);
    watcher.running?.set(state.current.id, listed?.last_run?.id ?? null);
  }
  const conv = state.current;
  if (conv) {
    // Keep the local copy in sync so a re-render shows this turn.
    const fresh = await api.get(`/api/conversations/${conv.id}`).catch(() => null);
    if (fresh && state.current?.id === fresh.id && fresh.running_run && fresh.running_run !== runId) {
      await openConversation(fresh.id, fresh.running_run);  // a queued message went next
      loadConversations();
      return;
    }
    if (fresh && state.current?.id === fresh.id) {
      state.current = fresh;
      renderQueue();
      renderLimit();
      renderRepair();
      renderTimed();  // (a Stop ended its timer: the bar went only at a reload, a simulated user found)
      const last = fresh.messages[fresh.messages.length - 1];
      if (last?.role === "assistant" && last.changes) view.setChanges(last.changes, showChanges, fresh.git);
      renderHeader();
      decorateTurns();
    }
  }
  loadConversations();
}

export function stopFollowing(keepScroll = false) {
  if (!keepScroll) { dom.scroll?.destroy(); dom.scroll = null; }
  stopWatchingSubagents();
  if (state.following) {
    state.following.source.close();
    state.following.view.el.hidden = false;
    state.following = null;
  }
}

// Messages waiting for the current answer to finish.
function renderQueue() {
  const conv = state.current;
  if (!dom.queue || !conv) return;
  const idle = !state.following;
  clear(dom.queue, (conv.queued || []).map((q) => h("div.queued-item",
    h("span.faint", idle ? "Waiting:" : "Next:"), h("span.truncate", q.content),
    // After a run that failed or was stopped, the queue waits for you.
    idle ? h("button.btn.small", { onclick: async () => {
      try {
        const { run_id: runId } = await api.post(`/api/conversations/${conv.id}/queue/${q.id}/send`);
        await openConversation(conv.id, runId);
      } catch (error) {
        toast(error.message, "error");
      }
    } }, "Send now") : null,
    h("button.icon-btn.sm", { title: "Don't send", "aria-label": "Don't send", onclick: async () => {
      try {
        await api.del(`/api/conversations/${conv.id}/queue/${q.id}`);
        conv.queued = conv.queued.filter((other) => other.id !== q.id);
        renderQueue();
      } catch (error) {
        toast(error.message, "error");
      }
    } }, icon("x", "sm")))));
}

async function queueMessage(text, attachments) {
  const conv = state.current;
  try {
    const result = await api.post(`/api/conversations/${conv.id}/queue`, { text, attachments });
    if (result.run_id) {
      await openConversation(conv.id, result.run_id);  // it had just finished: sent right away
      return true;
    }
    conv.queued = [...(conv.queued || []), { id: result.queued, content: text.trim() || "See the attached files." }];
    renderQueue();
    return true;
  } catch (error) {
    toast(error.message, "error");
    return false;
  }
}

export async function stopRun() {
  if (!state.following) return;
  try {
    await api.post(`/api/runs/${state.following.runId}/stop`);
  } catch (error) {
    toast(error.message, "error");
  }
}

function scrollToBottom(force = false) {
  if (force) dom.scroll?.jump();
  else dom.scroll?.changed();
}

// Selecting text in an answer shows a Reply button by it, which quotes the text in the message box.
const replyButton = h("button.reply-float", { type: "button", hidden: true,
  // Keep the selection: a click would clear it before the button gets it.
  onmousedown: (e) => e.preventDefault(),
  onclick: () => {
    const text = window.getSelection()?.toString() || "";
    replyButton.hidden = true;
    if (!text.trim() || !dom.composer?.quote) return;
    dom.composer.quote(text);
    window.getSelection()?.removeAllRanges();
  } }, icon("reply", "sm"), "Reply");

document.body.append(replyButton);

document.addEventListener("selectionchange", debounce(() => {
  const selection = window.getSelection();
  const from = selection?.rangeCount ? selection.getRangeAt(0).commonAncestorContainer : null;
  const answer = (from?.nodeType === Node.TEXT_NODE ? from.parentElement : from)?.closest?.(".msg.assistant");
  if (!answer || selection.isCollapsed || !selection.toString().trim() || !dom.thread?.contains(answer)
      || !dom.composer?.quote || answer.closest(".msg-actions")) {
    replyButton.hidden = true;
    return;
  }
  const rect = selection.getRangeAt(0).getBoundingClientRect();
  replyButton.hidden = false;
  const width = replyButton.offsetWidth;
  replyButton.style.left = `${Math.max(8, Math.min(rect.left + rect.width / 2 - width / 2, innerWidth - width - 8))}px`;
  // Above the selection, or below it at the top of the screen.
  const above = rect.top - replyButton.offsetHeight - 8;
  replyButton.style.top = `${above > 8 ? above : rect.bottom + 8}px`;
}, 120));

document.addEventListener("scroll", () => { replyButton.hidden = true; }, true);
