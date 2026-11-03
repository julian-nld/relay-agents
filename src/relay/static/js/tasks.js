// The tasks overview: what agents are doing, what needs review.

import { api } from "./api.js";
import { activityBadge, chatIcon, conversationStatus } from "./activity.js";
import { clearKeepingFocus, h, icon, shortPath, timeAgo, toast } from "./ui.js";

const GROUPS = [
  ["waiting", "Waiting for you", "Questions or approvals that need your answer before the agent can continue."],
  ["running", "Working", "Agents at work right now."],
  ["review", "Needs review", "Code tasks with changes you haven't merged or marked as reviewed yet."],
  ["clean", "Up to date", "Code tasks with nothing to merge or review, and tasks in folders without git."],
  ["discarded", "Discarded", "Tasks whose branch was thrown away."],
];

// /api/tasks' answer as the groups shown (the page and the side column alike): [key, title, tasks], the ones with
// a question or approval waiting apart from the ones at work.
export function taskGroups(answer) {
  // (as the page has it already, its waiting ones apart; or as /api/tasks gives them)
  const running = answer.running || [];
  const data = { ...answer, waiting: [...(answer.waiting || []), ...running.filter((task) => task.needs_approval)],
    running: running.filter((task) => !task.needs_approval) };
  return GROUPS.map(([key, title]) => [key, title, data[key] || []]);
}

// hooks: { providerName(id), onOpen(id), onReview(id), onLoaded(groups) }
export function tasksView(hooks) {
  const body = h("div.tasks-body", h("div.spinner"));
  const el = h("div.scroller", h("div.tasks", h("div.tasks-head",
    h("h1", "Tasks"), h("p.muted", "Everything your agents are doing, and what's waiting for you.")), body));
  let data = null;
  let showDiscarded = false;
  let snapshot = null;
  let request = 0;

  async function load() {
    const asked = ++request;
    try {
      const fresh = await api.get("/api/tasks");
      if (asked !== request) return;
      data = fresh;
      data.waiting = data.running.filter((task) => task.needs_approval);
      data.running = data.running.filter((task) => !task.needs_approval);
      hooks.onLoaded?.(data);
      render();
    } catch (error) {
      if (asked !== request) return;
      // A transient poll failure must not remove the task the user is using.
      if (!data) clearKeepingFocus(body, h("p.error-text", error.message));
    }
  }

  function render() {
    const next = JSON.stringify([data, showDiscarded, Math.floor(Date.now() / 60000)]);
    if (snapshot === next) return;
    snapshot = next;
    const sections = GROUPS.filter(([key]) => data[key].length && (key !== "discarded" || showDiscarded))
      .map(([key, title, lead]) => h(`section.task-group.${key}`,
        h("h2", title, h("span.count", String(data[key].length))),
        h("p.muted", lead),
        h("div.task-list", data[key].map((task) => card(task, key)))));
    const hidden = data.discarded.length && !showDiscarded
      ? h("button.btn.ghost.small", { onclick: () => { showDiscarded = true; render(); } },
        `Show ${data.discarded.length} discarded`) : null;
    const empty = !GROUPS.some(([key]) => key !== "discarded" && data[key].length);
    clearKeepingFocus(body, empty ? h("div.changes-empty", icon("code", "lg"),
      h("p", "No tasks yet. Start a code task to let an agent work on a project.")) : null,
    sections, hidden);
  }

  function card(task, group) {
    const status = activityBadge(conversationStatus(task, { group }));
    const where = task.git?.mode === "worktree" ? `${task.git.branch} → ${task.git.base_branch}`
      : task.mode !== "code" ? "Chat"
        : task.git?.mode === "none" ? `${shortPath(task.directory)} · no git` : shortPath(task.directory);
    const changes = task.shared_changes ? h("span.faint", "same changes as a newer task")
      : task.changes?.files
      ? h("span.task-changes", icon("file", "sm"), `${task.changes.files} `,
        h("span.add", `+${task.changes.additions}`), " ", h("span.del", `−${task.changes.deletions}`))
      : null;
    const actions = [h(`button.btn.small${group === "waiting" ? ".primary" : ""}`,
      { dataset: { focus: `${task.id}:open` }, onclick: () => hooks.onOpen(task.id) },
      group === "waiting" ? "Answer" : "Open")];
    if (group === "review") {
      actions.unshift(h("button.btn.small.primary",
        { dataset: { focus: `${task.id}:review` }, onclick: () => hooks.onReview(task.id) },
        icon("file", "sm"), "Review changes"));
    }
    if (["running", "waiting"].includes(group) && task.running_run) {
      actions.push(h("button.btn.small.danger", { dataset: { focus: `${task.id}:stop` }, onclick: async () => {
        try {
          await api.post(`/api/runs/${task.running_run}/stop`);
          toast("Stopping…");
          setTimeout(load, 800);
        } catch (error) {
          toast(error.message, "error");
        }
      } }, icon("stop", "sm"), "Stop"));
    }
    return h("article.task-card",
      h("div.task-top", chatIcon(task),
        h("button.task-title",
          { dataset: { focus: `${task.id}:title` }, onclick: () => hooks.onOpen(task.id),
            title: task.title }, task.title),
        status),
      h("div.task-meta",
        h("span", [hooks.providerName(task.provider_id), task.model].filter(Boolean).join(" · ")),
        h("span.mono", { title: task.directory }, where),
        changes,
        h("span.faint", timeAgo(task.updated))),
      h("div.task-actions", actions));
  }

  return { el, load };
}
