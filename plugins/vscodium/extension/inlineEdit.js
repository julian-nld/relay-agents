// Inline editing (Ctrl+I, "Edit with Relay"): the picked lines (or the cursor's place) and what should change go to
// an agent in a temporary chat of Relay's, whose code block replaces them in the editor, unsaved, with Accept ·
// Reject above it (inline.js). The chat goes once it answered. Its agent: relay.inlineAgent, else the one picked for
// a new chat last, else the latest conversation's.
"use strict";

const path = require("path");
const vscode = require("vscode");
const { answer, apply } = require("./lib/events");
const { RULES, code, prompt } = require("./lib/edit");

const OFF = { web: false, memory: false, subagents: false, computers: false };  // (Relay's tools: none needed)

class InlineEdit {
  constructor(relay, inline) {
    this.relay = relay;
    this.inline = inline;
  }

  // Which agent: {provider, model}
  async agent() {
    const set = (vscode.workspace.getConfiguration("relay").get("inlineAgent") || "").trim();
    if (set) {
      const [provider, ...model] = set.split("/");
      return { provider, model: model.join("/") || null };
    }
    const state = await this.relay.client.get("/api/state");
    if (state.new_chat?.provider_id) return { provider: state.new_chat.provider_id,
      model: state.new_chat.model ?? null };
    const latest = (await this.relay.client.get("/api/conversations"))[0];
    if (latest) return { provider: latest.provider_id, model: latest.model };
    throw new Error("Relay has no agent for this yet: pick one in a chat first, or set relay.inlineAgent");
  }

  async run() {
    const editor = vscode.window.activeTextEditor;
    if (!editor) return;
    const document = editor.document;
    const selection = editor.selection;
    // (whole lines: a selection that ends at a line's start ends on the line before it)
    const from = selection.start.line;
    const to =
      Math.max(from, selection.end.line - (selection.end.character === 0 && selection.end.line > from ? 1 : 0));
    const instruction = await vscode.window.showInputBox({ title: "Edit with Relay",
      prompt: selection.isEmpty ? `What should go at line ${from + 1}?`
        : `What should change in lines ${from + 1}-${to + 1}?`,
      placeHolder: "e.g. handle an empty list", ignoreFocusOut: true });
    if (!instruction?.trim()) return;
    const range = new vscode.Range(from, 0, to, document.lineAt(to).text.length);
    const was = document.getText(range);
    const whole = document.getText();
    const folder = vscode.workspace.getWorkspaceFolder(document.uri)?.uri.fsPath;
    const file = document.uri.scheme === "file" && folder
      ? path.relative(folder, document.uri.fsPath).replace(/\\/g, "/")
      : path.basename(document.fileName);
    const text = prompt({ file, language: document.languageId, lines: whole.split(/\r?\n/), from, to, instruction });
    const said = await vscode.window.withProgress({ location: vscode.ProgressLocation.Notification,
      title: "Relay is editing", cancellable: true }, (progress, cancel) => this.ask(text, progress, cancel));
    if (said === null) return;  // (cancelled)
    const fresh = code(said);
    if (!fresh.trim() && !was.trim()) throw new Error(`The agent gave no code: ${said.slice(0, 200)}`);
    if (document.isClosed || document.getText(range) !== was) {
      // (the lines changed while it worked: its answer beside them instead)
      const beside = await vscode.workspace.openTextDocument({ content: fresh, language: document.languageId });
      await vscode.window.showTextDocument(beside, { viewColumn: vscode.ViewColumn.Beside });
      vscode.window.showInformationMessage("Relay: those lines changed while it worked, so its code is beside them.");
      return;
    }
    const before = document.getText();
    const edit = new vscode.WorkspaceEdit();
    edit.replace(document.uri, range, fresh.replace(/\r?\n/g, document.eol === vscode.EndOfLine.CRLF ? "\r\n" : "\n"));
    await vscode.workspace.applyEdit(edit);
    await this.inline.propose(document, before, () => {});
  }

  // The temporary chat: made, asked, its answer's text (null when cancelled), and gone again
  async ask(text, progress, cancel) {
    const client = this.relay.client;
    const { provider, model } = await this.agent();
    progress.report({ message: `with ${this.relay.agentName(provider, model)}…` });
    const chat = await client.post("/api/conversations", { provider_id: provider, model: model || undefined,
      mode: "chat", temporary: true, agent_mode: "readonly", tools: OFF,
      title: "Edit with Relay", instructions: RULES });
    const stop = new AbortController();
    let run = null;
    cancel.onCancellationRequested(() => {
      stop.abort();
      if (run) client.post(`/api/runs/${encodeURIComponent(run)}/stop`).catch(() => {});
    });
    try {
      run = (await client.post(`/api/conversations/${encodeURIComponent(chat.id)}/messages`, { text })).run_id;
      const live = answer(run);
      let shown = 0;
      await client.events(run, (event) => {
        apply(live, event);
        const length = live.blocks.filter((b) => b.type === "text").reduce((n, b) => n + b.text.length, 0);
        if (length - shown > 80) {
          shown = length;
          progress.report({ message: `${length} characters…` });
        }
      }, { signal: stop.signal });
      if (cancel.isCancellationRequested) return null;
      if (live.status === "failed") {
        const error = live.blocks.find((b) => b.type === "error");
        throw new Error(error?.message || "The agent stopped without an answer");
      }
      return live.blocks.filter((b) => b.type === "text").map((b) => b.text).join("");
    } finally {
      client.del(`/api/conversations/${encodeURIComponent(chat.id)}`).catch(() => {});
    }
  }
}

module.exports = { InlineEdit };
