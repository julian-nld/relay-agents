// Shared presentation of where changes live and what Relay will do after a stop.

import { clock } from "./ui.js";

export function reviewLocation(git = {}) {
  if (git.mode === "worktree") return {
    label: "On a separate branch",
    detail:
      `Changes stay on ${git.branch || "the task's branch"} until you merge them into `
      + `${git.base_branch || "your project branch"}.`,
  };
  return { label: "Already in your project",
    detail: git.mode === "none" ? "Relay keeps checkpoints of these files. Reviewing does not change them."
      : "Reviewing does not apply or commit anything. Mark as reviewed when you're done checking the files." };
}

export function recoveryNotice(conv) {
  const limit = currentLimit(conv);
  if (conv.running_run) return "Relay is already working on another attempt.";
  if (limit?.unusable && !conv.resume_at) return "This model can't be used here: choose another one.";
  // (stopped by the user: nothing to say about retries)
  if (!limit && !conv.resume_at && conv.messages?.findLast((m) => m.role === "assistant")?.status === "interrupted") {
    return "";
  }
  if (conv.resume_at) {
    const loop = limit?.restart && limit.loop;
    return (loop ? `Relay will restart fallback loop “${loop.name}”` : "Relay will resume automatically")
      + ` at ${clock(conv.resume_at)}. Retrying now or choosing another model replaces that plan.`;
  }
  return limit?.gave_up ? "Relay has stopped automatic retries. You can try again yourself."
    : "No automatic retry is scheduled.";
}

// Stop information can outlive the attempt that caused it (e.g. after a fallback is interrupted).
export function currentLimit(conv) {
  const limit = conv.limit;
  const last = conv.messages?.findLast((message) => message.role === "assistant");
  if (!limit || !last) return limit || null;
  if (limit.run_id) return limit.run_id === last.run_id ? limit : null;
  // Older chats have no owning run id. Never attach a previous fallback failure to a manual stop.
  return last.status === "failed" && !limit.fell_back && last.run_provider === limit.provider_id
    && (last.run_model || "") === (limit.model || "") ? limit : null;
}

// Who answers a chat's question at `index`: the answer's run; for the question being answered now, the run that's
// answering it (a model chosen meanwhile is the next turn's: "Now with" it showed above a turn that went on as it
// was, a user found); else what the chat is set to now.
export function answeringAgent(conv, index) {
  const answer = conv.messages.slice(index).find((m) => m.role === "assistant");
  if (answer) return { provider: answer.run_provider, model: answer.run_model, mode: answer.run_mode };
  const last = conv.messages.map((m) => m.role).lastIndexOf("user");
  if (index === last && conv.running_agent) return conv.running_agent;
  return { provider: conv.provider_id, model: conv.model, mode: conv.agent_mode };
}

// Where a long thread starts on screen: its last messages, up to about `budget` of their events (each tool call is in
// the page, folded: 16,000 took 2.5 s to open, 160,000 half a minute, and AgentOS's Governor's chat only grows), at
// least its last turn, from a question on. The rest comes a part at a time, on a click.
export function firstShown(messages, budget = 3000) {
  let events = 0;
  let i = messages.length;
  while (i > 0) {
    const cost = 1 + (messages[i - 1].events?.length || 0);
    if (events + cost > budget && messages.length - i >= 2) break;
    events += cost;
    i -= 1;
  }
  while (i > 0 && i < messages.length && messages[i].role !== "user") i -= 1;  // (a turn whole: from its question)
  return i;
}
