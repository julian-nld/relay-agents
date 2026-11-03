// Permission control in VSCodium (relay.permissions; lib/permissions.js decides when): an agent's command or file
// change on this computer is asked about here. In its task's chat when that's in sight (a card, as Relay's own
// questions are), else in a notification. A change shows beside as a diff: the file now ↔ the agent's.
"use strict";

const crypto = require("crypto");
const fs = require("fs");
const path = require("path");
const vscode = require("vscode");
const { Gate } = require("./lib/permissions");
const { where } = require("./lib/connector");
const { folders } = require("./conversations");
const { near } = require("./lib/places");

const SCHEME = "relay-proposed";

class Permissions {
  constructor(context, relay, chats, review) {
    Object.assign(this, { relay, chats, review });
    this.asks = new Map();  // (id: {item, job, about, done, change})
    this.proposed = new Map();  // (relay-proposed: addresses' texts)
    this.gate = new Gate({ mode: () => this.mode(), about: (job) => this.about(job),
      ask: (job, about, seconds, signal) => this.ask(job, about, seconds, signal) });
    context.subscriptions.push(vscode.workspace.registerTextDocumentContentProvider(SCHEME, {
      provideTextDocumentContent: (uri) => this.proposed.get(uri.toString()) ?? "" }));
  }

  mode() {
    return vscode.workspace.getConfiguration("relay").get("permissions") || "ask";
  }

  allow(job) {
    return this.gate.allow(job);
  }

  // The conversation the job is for, and its answer at work: {id, run, title}. Its computer task by the job's folder;
  // else (a command without --cwd, or a chat that isn't a computer task) the one at work among the open chats (the
  // one in sight first) and this window's computer tasks
  async about(job) {
    let ids = this.review.tasksAt(job);
    const guessed = !ids.length;
    if (guessed) {
      const open = folders();
      const chats = [...this.chats.panels].filter((p) => p.id).sort((a, b) => b.panel.visible - a.panel.visible)
        .map((p) => p.id);
      const tasks = Object.entries(this.chats.locals()).filter(([, place]) => open.some((f) => near(place.folder, f)))
        .map(([id]) => id).slice(-20);
      ids = [...new Set([...chats, ...tasks])];
    }
    let first = null;
    for (const id of ids) {
      const convo = await this.relay.client.get(`/api/conversations/${encodeURIComponent(id)}`);
      const found = { id, run: convo.running_run || null, title: convo.title };
      if (found.run) return found;
      first = first || found;
    }
    return guessed ? null : first;  // (none at work: another conversation's, as `relay run --on` from a chat)
  }

  ask(job, about, seconds, signal) {
    return new Promise((resolve) => {
      const id = crypto.randomUUID();
      const write = job.kind === "write";
      const file = write ? where(job.path) : null;
      const place = (f) => (about && this.chats.local(about.id)?.folder
        ? path.relative(this.chats.local(about.id).folder, f).replace(/\\/g, "/") : f);
      const size = write ? Buffer.from(String(job.content_b64 || ""), "base64").length : 0;
      const item = { id, run: about?.run || null, change: write, expires: Date.now() / 1000 + seconds,
        action: write ? `change ${place(file)}` : "run a command",
        what: write ? `${file} (${size} bytes${fs.existsSync(file) ? "" : ", a new file"})`
          : `${job.command}\n(in ${where(job.cwd)})` };
      const ask = { item, job, about, file };
      // (its answer stopped, or ended: Relay doesn't wait for the job any more, so neither does the question)
      const watching = about?.run ? setInterval(async () => {
        const convo =
          await this.relay.client.get(`/api/conversations/${encodeURIComponent(about.id)}`).catch(() => null);
        if (convo && convo.running_run !== about.run) ask.done("timeout");
      }, 3000) : null;
      ask.done = (decision) => {
        if (!this.asks.has(id)) return;
        clearInterval(watching);
        this.asks.delete(id);
        this.tell(about);
        this.closeChange(ask);
        resolve(decision);
      };
      this.asks.set(id, ask);
      signal.addEventListener("abort", () => ask.done("timeout"));
      if (write) this.showChange(id);
      const panel = about ? this.chats.find(about.id) : null;
      if (panel?.panel.visible) this.tell(about);
      else this.notify(id);
    });
  }

  answer(id, decision) {
    this.asks.get(id)?.done(["once", "answer"].includes(decision) ? decision : "denied");
  }

  // A chat's cards: the questions for its conversation
  tell(about) {
    for (const panel of this.chats.panels) {
      if (about && panel.id !== about.id) continue;
      panel.post({ type: "asks", items: [...this.asks.values()].filter((a) => a.about?.id === panel.id)
        .map((a) => a.item) });
    }
  }

  // (a chat that just loaded)
  showIn(panel) {
    if ([...this.asks.values()].some((a) => a.about?.id === panel.id)) this.tell({ id: panel.id });
  }

  async notify(id) {
    const ask = this.asks.get(id);
    if (!ask) return;
    const { item, about } = ask;
    const who = about ? `“${about.title || "a computer task"}”` : "An agent";
    const choices =
      ["Allow", ...item.run ? ["Allow for this answer"] : [], "Deny", ...item.change ? ["Show the change"] : []];
    const picked = await vscode.window.showWarningMessage(`Relay: ${who} wants to ${item.action} on this computer: `
      + `${item.what.split("\n")[0].slice(0, 300)}`, ...choices);
    if (!this.asks.has(id)) return;
    if (picked === "Show the change") {
      await this.showChange(id);
      this.notify(id);
      return;
    }
    if (picked) this.answer(id, { Allow: "once", "Allow for this answer": "answer" }[picked] || "denied");
    // (closed without a choice: it waits for one in the chat, or for its time to run out)
  }

  // The change beside: the file as it is ↔ as the agent would have it
  async showChange(id) {
    const ask = this.asks.get(id);
    if (!ask?.file) return;
    const data = Buffer.from(String(ask.job.content_b64 || ""), "base64");
    const name = path.basename(ask.file);
    ask.right = vscode.Uri.from({ scheme: SCHEME, path: `/${id}/${name}` });
    this.proposed.set(ask.right.toString(),
      data.includes(0) ? `(binary: ${data.length} bytes)` : data.toString("utf8"));
    const left = fs.existsSync(ask.file) ? vscode.Uri.file(ask.file)
      : vscode.Uri.from({ scheme: SCHEME, path: `/${id}/none/${name}` });
    // (beside the chat, never over it: its card has the buttons)
    await vscode.commands.executeCommand("vscode.diff", left, ask.right, `${name} (now ↔ the agent's change)`,
      { preview: true, preserveFocus: true, viewColumn: vscode.ViewColumn.Beside });
  }

  closeChange(ask) {
    if (!ask.right) return;
    const key = ask.right.toString();
    const tabs = vscode.window.tabGroups.all.flatMap((g) => g.tabs)
      .filter((t) => t.input?.modified?.toString() === key);
    if (tabs.length) vscode.window.tabGroups.close(tabs, true).then(() => {}, () => {});
    setTimeout(() => this.proposed.delete(key), 1000);
  }
}

module.exports = { Permissions };
