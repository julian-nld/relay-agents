// The changes panel of a code task: files, diffs, merge / discard / rewind.

import { api } from "./api.js";
import { reviewLocation } from "./outcomes.js";
import { clear, confirmDialog, h, icon, promptDialog, toast } from "./ui.js";

const STATUS_LABEL = { added: "A", modified: "M", deleted: "D", renamed: "R" };

export function changesLabel(summary) {
  if (!summary || !summary.files) return "No changes";
  const files = `${summary.files} file${summary.files === 1 ? "" : "s"} changed`;
  return `${files} +${summary.additions} −${summary.deletions}`;
}

// Opens the panel inside `host`. hooks: { onChanged() } after a merge, discard or rewind.
export function openChanges(host, conversation, hooks = {}) {
  host.querySelector(".changes-panel")?.remove();
  const previous = document.activeElement;
  const body = h("div.changes-body", h("div.spinner"));
  const foot = h("div.changes-foot");
  const title = h("div.grow", h("h2", "Review changes"), h("div.changes-sub"));
  const location = h("div.changes-location");
  const panel = h("aside.changes-panel", { role: "dialog", "aria-label": "Review changes" },
    h("div.changes-head", title,
      h("button.icon-btn", { title: "Refresh", onclick: () => load() }, icon("refresh")),
      h("button.icon-btn", { title: "Close", onclick: () => close() }, icon("x"))),
    location, body, foot);
  host.append(panel);
  const escape = (e) => {
    if (e.key === "Escape" && !e.defaultPrevented && !document.querySelector(".modal-backdrop, .menu")) close();
  };
  document.addEventListener("keydown", escape);
  panel.querySelector("button").focus({ preventScroll: true });

  function close() {
    const restore = panel.contains(document.activeElement);
    panel.remove();
    document.removeEventListener("keydown", escape);
    if (restore && previous?.isConnected) previous.focus({ preventScroll: true });
  }

  // `undoable`: a rewind, which returns the point to go back to (the toast's Undo). `done`: the panel's work is
  // over with it (Mark as reviewed): it closes. Else it's loaded anew before its button is let go (busy()), so the
  // old one can't be pressed twice.
  async function act(request, success, undoable = false, done = false) {
    try {
      const result = await request();
      const undo = undoable && result?.undo ? { label: "Undo", onclick: () => act(
        () => api.post(`/api/conversations/${conversation.id}/rewind`, { checkpoint: result.undo }),
        "Files as they were again") } : null;
      toast(typeof success === "function" ? success(result) : success, "info", undo);
      hooks.onChanged?.();
      if (done) close();
      else await load();
    } catch (error) {
      toast(error.message, "error");
    }
  }

  async function load() {
    let data;
    try {
      data = await api.get(`/api/conversations/${conversation.id}/changes`);
    } catch (error) {
      clear(body, h("p.error-text", error.message));
      return;
    }
    const sub = title.querySelector(".changes-sub");
    const where = reviewLocation({ mode: data.git_mode, branch: data.branch, base_branch: data.base_branch });
    if (data.git_mode === "none" && !data.checkpoints) where.detail = "Relay cannot track the changes in this folder.";
    clear(location, h("strong", where.label), h("p", where.detail));
    location.hidden = data.task_state === "discarded";
    if (data.git_mode === "worktree") {
      sub.textContent = `Branch ${data.branch} → ${data.base_branch}`;
    } else if (data.git_mode === "direct") {
      sub.textContent =
        `${data.repo_root}${data.scope ? `/${data.scope}` : ""} · since the task started or was last reviewed`;
    } else if (data.checkpoints) {
      sub.textContent = "Since the task started (kept by Relay; not a git repository)";
    } else {
      sub.textContent = "Not tracked with git";
    }
    renderBody(data);
    renderActions(data);
  }

  function renderBody(data) {
    if (data.task_state === "discarded") {
      clear(body, h("div.changes-empty", icon("trash", "lg"), h("p", "This task was discarded.")));
      return;
    }
    if (data.git_mode === "none" && !data.checkpoints) {
      clear(body, h("div.changes-empty", icon("folder", "lg"),
        h("p", "Relay can't follow the changes in this folder: it has too many files.")));
      return;
    }
    if (data.error) {
      clear(body, h("p.error-text", data.error));
      return;
    }
    if (!data.files.length) {
      // (true at the start and after Mark as reviewed alike: "Nothing changed yet" wasn't, then)
      clear(body, h("div.changes-empty", icon("check", "lg"), h("p", "No changes to review.")));
      return;
    }
    clear(body,
      h("div.changes-summary", changesLabel(data.summary), data.truncated ? " (list cut off)" : ""),
      data.files.map(fileRow));
  }

  function fileRow(file) {
    const diffBox = h("div.diff.hidden");
    const row = h("div.change-file",
      h("button.change-head", {
        "aria-expanded": "false", onclick: (e) => {
          const open = row.classList.toggle("open");
          if (open && !diffBox.childElementCount) renderDiff(diffBox, file);
          diffBox.classList.toggle("hidden", !open);
          e.currentTarget.setAttribute("aria-expanded", String(open));
        },
      },
      h(`span.change-status.${file.status}`, { title: file.status }, STATUS_LABEL[file.status] || "M"),
      h("span.change-path.grow", file.path),
      file.binary ? h("span.faint", "binary")
        : h("span.change-counts", h("span.add", `+${file.additions}`), " ", h("span.del", `−${file.deletions}`)),
      icon("chevronRight", "sm chev")),
      diffBox);
    return row;
  }

  function renderActions(data) {
    const busy = (button, fn) => async () => {
      button.disabled = true;
      await fn();
      button.disabled = false;
    };
    const buttons = [];
    if (data.task_state === "open" && data.git_mode === "worktree") {
      const discard = h("button.btn.danger");
      discard.append(icon("trash", "sm"), "Discard");
      discard.onclick = busy(discard, async () => {
        const ok = await confirmDialog("Discard this task?",
          `The branch ${data.branch} and everything on it that isn't merged are deleted. The chat stays.`,
          { confirm: "Discard", danger: true });
        if (ok) await act(() => api.post(`/api/conversations/${conversation.id}/discard`), "Task discarded");
      });
      const merge = h("button.btn.primary", { disabled: !data.files.length,
        title: "All of the task's turns as one commit" });
      merge.append(icon("check", "sm"), `Merge into ${data.base_branch}`);
      merge.onclick = busy(merge, () => act(
        () => api.post(`/api/conversations/${conversation.id}/merge`), `Merged into ${data.base_branch}`));
      buttons.push(discard, h("span.grow"), merge);
    } else if (data.task_state === "open" && (data.git_mode === "direct" || data.checkpoints)) {
      // Every turn is kept by Relay (not committed); going back is a rewind.
      const undo = h("button.btn.danger", { disabled: !data.files.length });
      undo.append(icon("undo", "sm"), "Rewind to the start");
      undo.onclick = busy(undo, async () => {
        const ok = await confirmDialog("Rewind to the start?",
          (data.git_mode === "direct" ? "The files go back to how they were when the task started or was last "
            + "marked as reviewed" : "The files go back to how they were when the task started") +
          `. The chat stays${data.git_mode === "direct" ? ", and so does git's history" : ""}; this can be undone (the `
          + "toast's Undo). Rewind to a single answer with /rewind in the message box.",
          { confirm: "Rewind", danger: true });
        if (ok) {
          await act(() => api.post(`/api/conversations/${conversation.id}/rewind`, { to_start: true }),
            "Files as they were at the start", true);
        }
      });
      buttons.push(undo);
      if (data.git_mode === "direct") {
        // Relay never commits by itself: a commit is yours, with your message.
        const commit = h("button.btn", { title: "Commit the task's folder to your repository, with your message" });
        commit.append(icon("fork", "sm"), "Commit…");
        commit.onclick = busy(commit, async () => {
          // (the task's title to start from; none at all was a Commit that did nothing, without a word)
          const message = await promptDialog("Commit", { label: "Message", placeholder: "What these changes do",
            value: conversation.title || "", confirm: "Commit", note: alsoCommitted(data) });
          if (message === null) return;
          if (!message.trim()) {
            toast("A commit needs a message: what these changes do", "error");
            return;
          }
          await act(() => api.post(`/api/conversations/${conversation.id}/commit`, { message }),
            (made) => (made.as_relay ? "Committed, as Relay: git doesn't know your name yet (git config --global "
              + "user.name, user.email)" : "Committed to your repository"), false, true);
        });
        const seen = h("button.btn.primary", { disabled: !data.files.length,
          title: "These changes are seen: the task leaves Needs review, and changes count from here" });
        seen.append(icon("check", "sm"), "Mark as reviewed");
        seen.onclick = busy(seen, () => act(
          () => api.post(`/api/conversations/${conversation.id}/reviewed`), "Marked as reviewed", false, true));
        buttons.push(h("span.grow"), commit, seen);
      }
    }
    clear(foot, buttons);
    foot.classList.toggle("hidden", !buttons.length);
  }

  load();
  return { close, reload: load };
}

// A commit takes everything changed in the folder (relay.tasks.commit), also what isn't this task's
function alsoCommitted(data) {
  const others = (data.uncommitted || []).filter((path) => !data.files.some((file) => file.path === path));
  if (!others.length) return "";
  const named = others.slice(0, 5).join(", ") + (others.length > 5 ? `, and ${others.length - 5} more` : "");
  return `This commits everything changed in the folder, so also what this task didn't change: ${named}.`;
}

function renderDiff(box, file) {
  if (!file.diff) {
    box.append(h("div.diff-note", file.binary ? "Binary file" : "No textual changes"));
    return;
  }
  const lines = file.diff.split("\n");
  const pre = h("pre");
  // Skip git's header (diff --git, index, ---/+++ …) up to the first hunk;
  // after that every line is content, even one that starts with "---".
  let inHunks = false;
  for (const line of lines) {
    if (!inHunks && !line.startsWith("@@")) continue;
    inHunks = true;
    let cls = "ctx";
    if (line.startsWith("@@")) cls = "hunk";
    else if (line.startsWith("+")) cls = "add";
    else if (line.startsWith("-")) cls = "del";
    else if (line.startsWith("\\")) cls = "meta";
    pre.append(h(`span.dl.${cls}`, line || " "));
  }
  box.append(pre);
}
