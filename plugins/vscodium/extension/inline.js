// Keep/Undo in the editor (relay.review.inline): each change of the agent's in an open file of the review's task, a
// hunk at a time. Its lines are marked, what it took out shows on hover, and Keep · Undo is above it. Keep is
// remembered (per task, per hunk: the same change at the same place); Undo puts that hunk back and saves. An inline
// edit's answer (Ctrl+I, inlineEdit.js) shows the same way, with Accept · Reject, whatever the setting.
"use strict";

const vscode = require("vscode");
const { hunks, place } = require("./lib/hunks");

const KEPT = "relay.kept";  // (workspaceState: {conversation id: [hunk keys]})
const SOON = 300;  // ms after a change of the text before the hunks are looked at again

const lineCount = (hunk) => hunk.new.length;
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

class Inline {
  constructor(context, review) {
    this.context = context;
    this.review = review;
    this.shown = new Map();  // (a document's address: {kind: "review" | "proposal", hunks, file})
    this.proposals = new Map();  // (a document's address: {before, done(accepted)})
    this.befores = new Map();  // (a review file's text before the agent, for this look at the task)
    this.lenses = new vscode.EventEmitter();
    this.onDidChangeCodeLenses = this.lenses.event;
    this.added = vscode.window.createTextEditorDecorationType({
      isWholeLine: true, backgroundColor: new vscode.ThemeColor("diffEditor.insertedLineBackground"),
      overviewRulerColor: new vscode.ThemeColor("editorOverviewRuler.addedForeground"),
      overviewRulerLane: vscode.OverviewRulerLane.Left });
    this.removed = vscode.window.createTextEditorDecorationType({
      after: { color: new vscode.ThemeColor("editorCodeLens.foreground"), fontStyle: "italic", margin: "0 0 0 2em" },
      overviewRulerColor: new vscode.ThemeColor("editorOverviewRuler.deletedForeground"),
      overviewRulerLane: vscode.OverviewRulerLane.Left });
    let soon = null;
    const later = (event) => {
      if (event && !this.shown.has(event.document.uri.toString()) && !this.isReviewed(event.document.uri)) return;
      clearTimeout(soon);
      soon = setTimeout(() => this.update(), SOON);
    };
    const command = (name, run) => vscode.commands.registerCommand(name, async (...args) => {
      try {
        await run(...args);
      } catch (error) {
        vscode.window.showErrorMessage(`Relay: ${error.message}`);
      }
    });
    context.subscriptions.push(this.added, this.removed, this.lenses,
      vscode.languages.registerCodeLensProvider({ scheme: "file" }, this),
      vscode.workspace.onDidChangeTextDocument(later),
      vscode.window.onDidChangeVisibleTextEditors(() => later()),
      vscode.window.onDidChangeActiveTextEditor(() => this.context_()),
      vscode.workspace.onDidChangeConfiguration((event) =>
        event.affectsConfiguration("relay.review.inline") && later()),
      command("relay.inline.keep", (uri, key) => this.keep(uri, [key])),
      command("relay.inline.keepAll", (uri) => this.keep(uri, (this.shown.get(uri)?.hunks || []).map((h) => h.key))),
      command("relay.inline.undo", (uri, key) => this.undo(uri, key)),
      command("relay.inline.undoAll", (uri) => this.undoAll(uri)),
      command("relay.inline.accept", (uri) => this.settle(uri, true)),
      command("relay.inline.reject", (uri) => this.settle(uri, false)),
      command("relay.review.next", () => this.go(1)),
      command("relay.review.previous", () => this.go(-1)));
  }

  get on() {
    return vscode.workspace.getConfiguration("relay").get("review.inline") !== false;
  }

  // The review looked at its task again: its files' texts before the agent are asked for anew
  refreshed() {
    this.befores.clear();
    this.update();
  }

  fileOf(uri) {
    const source = this.review.source;
    if (!source || uri.scheme !== "file") return null;
    return this.review.fileAt(uri);
  }

  isReviewed(uri) {
    return this.on && !!this.fileOf(uri);
  }

  kept() {
    const source = this.review.source;
    return new Set(source ? (this.context.workspaceState.get(KEPT) || {})[source.id] || [] : []);
  }

  async before(file) {
    const key = `${this.review.source.id}:${file.path}`;
    if (!this.befores.has(key)) this.befores.set(key, this.review.source.original(file).catch(() => null));
    return this.befores.get(key);
  }

  // A document's hunks now: an inline edit's answer, else the agent's changes (less the kept ones)
  async look(document) {
    const address = document.uri.toString();
    const proposal = this.proposals.get(address);
    if (proposal) return { kind: "proposal", hunks: hunks(proposal.before, document.getText()) };
    const file = this.on ? this.fileOf(document.uri) : null;
    if (!file || file.binary || (this.review.source.here === false)) return null;
    const before = await this.before(file);
    if (before === null) return null;
    const kept = this.kept();
    const found = hunks(before, document.getText()).filter((h) => !kept.has(h.key));
    return found.length ? { kind: "review", hunks: found, file } : null;
  }

  async update() {
    const looked = new Map();
    for (const editor of vscode.window.visibleTextEditors) {
      const address = editor.document.uri.toString();
      if (!looked.has(address)) looked.set(address, await this.look(editor.document));
      this.mark(editor, looked.get(address));
    }
    this.shown = new Map([...looked].filter(([, found]) => found));
    this.lenses.fire();
    this.context_();
  }

  mark(editor, found) {
    const added = [];
    const removed = [];
    for (const hunk of found?.hunks || []) {
      const was = hunk.old.length ? new vscode.MarkdownString().appendMarkdown(`**Before ${found.kind === "proposal"
        ? "the edit" : "the agent"}:**`).appendCodeblock(hunk.old.join("").replace(/\n$/, ""),
        editor.document.languageId) : undefined;
      if (lineCount(hunk)) {
        added.push({ range: new vscode.Range(hunk.newStart, 0, hunk.newStart + lineCount(hunk) - 1, 0),
          hoverMessage: was });
      } else {
        const line = Math.max(0, Math.min(hunk.newStart, editor.document.lineCount) - 1);
        removed.push({ range: new vscode.Range(line, Number.MAX_SAFE_INTEGER, line, Number.MAX_SAFE_INTEGER),
          hoverMessage: was, renderOptions: { after: { contentText: `− ${plural(hunk.old.length, "line")}` } } });
      }
    }
    editor.setDecorations(this.added, added);
    editor.setDecorations(this.removed, removed);
  }

  provideCodeLenses(document) {
    const address = document.uri.toString();
    const found = this.shown.get(address);
    if (!found) return [];
    const at = (hunk) => {
      const line = Math.min(lineCount(hunk) ? hunk.newStart : Math.max(0, hunk.newStart - 1), document.lineCount - 1);
      return new vscode.Range(line, 0, line, 0);
    };
    const lens = (range, title, command, tooltip, ...args) => new vscode.CodeLens(range,
      { title, command, tooltip, arguments: [address, ...args] });
    if (found.kind === "proposal") {
      const range = at(found.hunks[0] || { newStart: 0, new: [] });
      return [lens(range, "$(check) Accept", "relay.inline.accept", "Keep Relay's edit"),
        lens(range, "$(discard) Reject", "relay.inline.reject", "Put the text back as it was")];
    }
    const lenses = found.hunks.flatMap((hunk) => [
      lens(at(hunk), "$(check) Keep", "relay.inline.keep", "This change is good: its marks go", hunk.key),
      lens(at(hunk), "$(discard) Undo", "relay.inline.undo", "Put these lines back as they were before the agent",
        hunk.key)]);
    if (found.hunks.length > 1) {
      const top = new vscode.Range(0, 0, 0, 0);
      lenses.unshift(lens(top, `$(check-all) Keep all ${found.hunks.length} changes`, "relay.inline.keepAll",
        "Every change of the agent's in this file is good"),
      lens(top, "$(discard) Undo all", "relay.inline.undoAll", "Put the file back as it was before the agent"));
    }
    return lenses;
  }

  async keep(address, keys) {
    const source = this.review.source;
    if (!source || !keys.length) return;
    const all = this.context.workspaceState.get(KEPT) || {};
    await this.context.workspaceState.update(KEPT, { ...all, [source.id]: [...new Set([...(all[source.id] || []),
      ...keys])].slice(-2000) });
    await this.update();
    await this.allKept();
  }

  // Every change of the task's kept: the task as reviewed, if the user likes
  async allKept() {
    const source = this.review.source;
    const files = this.review.files.filter((f) => !f.binary && f.status !== "deleted");
    if (!source || !files.length || !this.review.can?.reviewed) return;
    const kept = this.kept();
    for (const file of files) {
      const before = await this.before(file);
      const now = await this.review.nowText(file);
      if (before === null || now === null || hunks(before, now).some((h) => !kept.has(h.key))) return;
    }
    const picked = await vscode.window.showInformationMessage("Relay: you kept every change of the agent's. Mark "
      + "the task as reviewed?", "Mark as reviewed");
    if (picked) await vscode.commands.executeCommand("relay.review.reviewed");
  }

  document(address) {
    const document = vscode.workspace.textDocuments.find((d) => d.uri.toString() === address);
    if (!document) throw new Error("That file isn't open any more");
    return document;
  }

  async undo(address, key) {
    const document = this.document(address);
    const found = await this.look(document);  // (as the text is now: it may have changed since the marks)
    const hunk = found?.hunks.find((h) => h.key === key);
    if (!hunk) throw new Error("That change isn't there any more");
    const { start, end, text } = place(document.getText(), hunk);
    const edit = new vscode.WorkspaceEdit();
    edit.replace(document.uri, new vscode.Range(document.positionAt(start), document.positionAt(end)), text);
    await vscode.workspace.applyEdit(edit);
    await document.save();
    await this.update();
  }

  async undoAll(address) {
    const document = this.document(address);
    const file = this.fileOf(document.uri);
    if (file) await vscode.commands.executeCommand("relay.review.revert", { command: { arguments: [file] } });
    await this.update();
  }

  // An inline edit's answer, in the text now: shown with Accept · Reject until one is picked
  propose(document, before, done) {
    this.proposals.get(document.uri.toString())?.done(true);
    this.proposals.set(document.uri.toString(), { before, done });
    return this.update();
  }

  async settle(address, accepted) {
    const proposal = this.proposals.get(address);
    if (!proposal) return;
    this.proposals.delete(address);
    if (!accepted) {
      const document = this.document(address);
      const edit = new vscode.WorkspaceEdit();
      const text = document.getText();
      for (const hunk of hunks(proposal.before, text).reverse()) {  // (last first: each place as the text has it)
        const { start, end, text: was } = place(text, hunk);
        edit.replace(document.uri, new vscode.Range(document.positionAt(start), document.positionAt(end)), was);
      }
      await vscode.workspace.applyEdit(edit);
    }
    proposal.done(accepted);
    await this.update();
  }

  // The cursor to the next (or previous) change in the file
  go(step) {
    const editor = vscode.window.activeTextEditor;
    const found = editor && this.shown.get(editor.document.uri.toString());
    if (!found?.hunks.length) return;
    const lines = found.hunks.map((h) => Math.min(h.newStart, editor.document.lineCount - 1));
    const here = editor.selection.active.line;
    const next = step > 0 ? lines.find((l) => l > here) ?? lines[0]
      : [...lines].reverse().find((l) => l < here) ?? lines.at(-1);
    const position = new vscode.Position(next, 0);
    editor.selection = new vscode.Selection(position, position);
    editor.revealRange(new vscode.Range(position, position), vscode.TextEditorRevealType.InCenterIfOutsideViewport);
  }

  context_() {
    const editor = vscode.window.activeTextEditor;
    vscode.commands.executeCommand("setContext", "relay.inline.hunks",
      !!(editor && this.shown.get(editor.document.uri.toString())?.hunks.length));
  }
}

module.exports = { Inline };
