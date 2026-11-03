// The same activity language in the sidebar, conversation header and tasks.

import { h, icon, relativeGroup } from "./ui.js";

export function conversationStatus(conv, { running = Boolean(conv.running_run), group = null } = {}) {
  const last = conv.last_run || conv.messages?.findLast((m) => m.role === "assistant");
  if (conv.git?.state === "discarded") return { kind: "quiet", label: "Discarded" };
  if (conv.needs_approval) return { kind: "waiting", label: "Waiting for you" };
  if (running) return { kind: "working", label: "Working" };
  if (conv.resume_at) return { kind: "paused", label: "Resuming later" };
  if (last?.status === "failed") return { kind: "failed", label: "Failed" };
  if (last?.status === "interrupted") return { kind: "quiet", label: "Stopped" };
  if (group === "review" || (conv.changes?.files && conv.git?.mode && conv.git.mode !== "none"
      && !conv.shared_changes)) return { kind: "review", label: "Needs review" };
  if (last?.status === "succeeded") return { kind: "done", label: "Completed" };
  return { kind: "quiet", label: "Ready" };
}

export function activityBadge(status, attrs = {}) {
  return h(`span.activity-badge.${status.kind}`, attrs, h("span.activity-dot"), status.label);
}

export function taskCounts(groups) {
  const pending = groups.running.filter((task) => task.needs_approval).length;
  const working = groups.running.length - pending;
  const waiting = (groups.waiting?.length || 0) + pending;
  const review = groups.review.length;
  return { total: working + waiting + review, working, waiting, review,
    label: `Working: ${working} · Waiting for you: ${waiting} · Needs review: ${review}` };
}

// A service's WorkBoxes (clones: Demote, workbox.clone) folded under the newest one: its `older`, newest
// first. One that's working or waiting for you, or open, stays in its own place; a search folds nothing.
export function foldClones(conversations, { open = null, searching = false } = {}) {
  if (searching) return conversations;
  const byService = new Map();
  for (const conv of conversations) {
    if (conv.clone?.service_id) {
      if (!byService.has(conv.clone.service_id)) byService.set(conv.clone.service_id, []);
      byService.get(conv.clone.service_id).push(conv);
    }
  }
  const heads = new Map();  // conversation id -> it with its older ones
  const folded = new Set();
  for (const clones of byService.values()) {
    if (clones.length < 2) continue;
    // The newest version leads; of one version its clone itself, before a fork of it or a reviewer in it
    const newest = [...clones].sort((a, b) => (b.clone.version ?? 0) - (a.clone.version ?? 0)
      || Number(Boolean(b.clone.own)) - Number(Boolean(a.clone.own)) || b.updated - a.updated);
    const [head, ...rest] = newest;
    const older = rest.filter((c) => !c.needs_approval && !c.running_run && c.id !== open);
    if (!older.length) continue;
    heads.set(head.id, { ...head, older });
    older.forEach((c) => folded.add(c.id));
  }
  return conversations.filter((c) => !folded.has(c.id)).map((c) => heads.get(c.id) || c);
}

export const GOVERNOR_GROUP = "AgentOS conversations";

// `apart`: AgentOS's (managed) chats in a group of their own; without it, as any (the Governor view lists only them)
export function sidebarGroups(conversations, { apart = true } = {}) {
  const groups = new Map([["Waiting for you", []], ["Working", []], ["Pinned", []], [GOVERNOR_GROUP, []]]);
  // The API puts pinned chats first; chronological groups should still be newest first.
  for (const conv of [...conversations].sort((a, b) => b.updated - a.updated)) {
    // The Governor's (managed) together, working ones too; one waiting for you or pinned by you as any
    const group = conv.needs_approval ? "Waiting for you" : apart && conv.managed && !conv.pinned ? GOVERNOR_GROUP
      : conv.running_run ? "Working" : conv.pinned ? "Pinned" : relativeGroup(conv.updated);
    if (!groups.has(group)) groups.set(group, []);
    groups.get(group).push(conv);
  }
  return [...groups].filter(([, items]) => items.length);
}

// A chat's icon: a chat, a code task or a computer task. Managed by AgentOS: in Relay's green, with "os" by it (the
// chat list, the Tasks view, the Tasks page)
export function chatIcon(conv) {
  const mode = icon(conv.mode === "code" ? "code" : conv.computer ? "monitor" : "chat", "sm mode");
  if (conv.managed) {
    return h("span.mode-wrap.managed", { title: "Managed by AgentOS: its Governor may see, change and delete it" },
      mode, h("span.os", { "aria-hidden": "true" }, "os"));
  }
  // Started outside Relay's pages: by a script or program, through the API (who: conv.external)
  return conv.external ? h("span.mode-wrap.external", { title: `Started outside Relay's pages, by ${conv.external}` },
    mode, h("span.os", { "aria-hidden": "true" }, "E")) : mode;
}
