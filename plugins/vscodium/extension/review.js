// Review as Cline and Copilot have it: the changes of the task whose chat was focused last, in Source Control (a
// provider "Relay" of its own), each file in the diff editor, marks in the editor's gutter where the agent changed
// lines, a file put back as it was; and the task's work marked reviewed, committed, merged, thrown away or undone.
// A code task's changes are Relay's (its API); a computer task's, whose folder is here, VSCodium's own checkpoints
// (lib/checkpoints.js), taken before every turn's first change.
"use strict";

const path = require("path");
const vscode = require("vscode");
const { Checkpoints } = require("./lib/checkpoints");
const { where } = require("./lib/connector");
const { before } = require("./lib/patch");

const SCHEME = "relay-before";
const LOOK = 15000;  // ms between looks while a task is shown and the window has focus

async function readText(file) {
  try {
    return new TextDecoder().decode(await vscode.workspace.fs.readFile(vscode.Uri.file(file)));
  } catch {
    return null;
  }
}

async function exists(file) {
  try {
    await vscode.workspace.fs.stat(vscode.Uri.file(file));
    return true;
  } catch {
    return false;
  }
}

// A code task's changes, as Relay has them
class ServerSource {
  constructor(relay, convo) {
    this.relay = relay;
    this.id = convo.id;
    this.title = convo.title;
    this.directory = convo.directory;
    this.info = null;
    this.here = false;  // (its folder is on this machine: Remote - SSH, or VSCodium on Relay's machine)
  }

  path(...more) {
    return `/api/conversations/${encodeURIComponent(this.id)}${more.join("")}`;
  }

  async load() {
    this.info = await this.relay.client.get(this.path("/changes"));
    this.here = await exists(this.root());
    return this.info.files || [];
  }

  get git() {
    return this.info?.checkpoints ? (this.info.git_mode === "direct" ? "direct" : "none")
      : this.info?.git_mode || "none";
  }

  can(files) {
    const git = this.info?.git_mode;
    return { commit: git === "direct" && files.length > 0, merge: git === "worktree" && files.length > 0,
      discard: git === "worktree", undo: git !== "worktree" && files.length > 0 && !!this.info?.checkpoints,
      revert: this.here };
  }

  // The folder its files' paths are in: a task in a folder (checkpoints), its own branch's worktree, or the repository
  root() {
    const info = this.info;
    if (info.checkpoints) return this.directory;
    if (info.git_mode === "worktree") {
      const parts = this.directory.split("/");
      const at = parts.lastIndexOf(this.id);  // (Relay's worktrees/<id>, the task in the same subfolder in it)
      return at > 0 ? parts.slice(0, at + 1).join("/") : this.directory;
    }
    return info.repo_root || this.directory;
  }

  // The file as it was (its diff taken back from it as it is now); null when that can't be exact
  async original(file) {
    if (file.binary) return null;
    const now = file.status === "deleted" ? "" : await readText(path.join(this.root(), file.path));
    return now === null ? null : before(now, file.diff || "");
  }

  async diffText(file) {
    return file.diff || `${file.path}: ${file.status}`;
  }

  async revert(file) {
    // (a renamed file's old name isn't in Relay's list: Undo all, or Relay's page, puts it back)
    const was = file.status === "renamed" ? null : await this.original(file);
    if (was === null) throw new Error(`${file.path} can't be put back here: Undo all (or Relay's page) can`);
    const place = vscode.Uri.file(path.join(this.root(), file.path));
    if (file.status === "added") await vscode.workspace.fs.delete(place);
    else await vscode.workspace.fs.writeFile(place, new TextEncoder().encode(was));
  }

  reviewed() {
    return this.relay.client.post(this.path("/reviewed"));
  }

  async commit(message) {
    return (await this.relay.client.post(this.path("/commit"), { message })).commit;
  }

  async merge() {
    return (await this.relay.client.post(this.path("/merge"))).merged_into;
  }

  discard() {
    return this.relay.client.post(this.path("/discard"));
  }

  async undoAll() {
    return (await this.relay.client.post(this.path("/rewind"), { to_start: true })).undo;
  }

  async undoTurn(run) {
    return (await this.relay.client.post(this.path("/rewind"), { run_id: run })).undo;
  }

  putBack(undo) {
    return this.relay.client.post(this.path("/rewind"), { checkpoint: undo });
  }
}

// A computer task's changes in its folder here, from VSCodium's own checkpoints
class LocalSource {
  constructor(review, id, place) {
    this.review = review;
    this.id = id;
    this.place = place;
    this.title = place.title || path.basename(place.folder);
    this.points = new Checkpoints(place.folder, review.store());
  }

  get git() {
    return "local";
  }

  root() {
    return this.place.folder;
  }

  async load() {
    if (!this.place.base) await this.review.started(this.id);
    this.place = this.review.chats.local(this.id);
    return (await this.points.changes(this.place.base)).files;
  }

  can(files) {
    return { commit: false, merge: false, discard: false, undo: files.length > 0, revert: true };
  }

  async original(file) {
    if (file.binary) return null;
    if (file.status === "added") return "";
    const was = await this.points.original(this.place.base, file.from || file.path);
    return was === null ? "" : was.toString("utf8");
  }

  diffText(file) {
    return this.points.diff(this.place.base, file);
  }

  revert(file) {
    return this.points.restore(this.place.base, [file]);
  }

  async reviewed() {
    await this.review.keep(this.id, { base: await this.points.take("Reviewed") });
  }

  undoAll() {
    return this.points.restore(this.place.base);
  }

  undoTurn(run) {
    const point = this.place.turns?.[run];
    if (!point) throw new Error("VSCodium has no record of the files before that answer");
    return this.points.restore(point);
  }

  putBack(undo) {
    return this.points.restore(undo);
  }
}

class Review {
  constructor(context, relay, chats) {
    this.context = context;
    this.relay = relay;
    this.chats = chats;
    this.source = null;
    this.files = [];
    this.texts = new Map();  // (relay-before: URIs' contents, made when asked)
    this.locks = new Map();  // (a computer task's checkpoint being taken: one at a time)
    this.contents = new vscode.EventEmitter();
    this.sc = null;
    this.can = {};
    this.onRefreshed = () => {};  // (Keep/Undo in the editor looks again: inline.js)
    context.subscriptions.push(vscode.workspace.registerTextDocumentContentProvider(SCHEME, {
      onDidChange: this.contents.event,
      provideTextDocumentContent: (uri) => this.text(uri),
    }));
    context.subscriptions.push(vscode.workspace.onDidSaveTextDocument(() => this.later()));
    this.context_();
  }

  store() {
    return this.context.globalStorageUri.fsPath;
  }

  dispose() {
    clearTimeout(this.timer);
    this.sc?.dispose();
  }

  // The task of a chat that got focus or loaded, or of the open folder (another kind keeps the one shown)
  show(panel) {
    const convo = panel.convo;
    if (!convo || !this.relay.client) return;
    const place = convo.computer ? this.chats.local(convo.id) : null;
    if (convo.mode !== "code" && !place) return;
    if (this.source?.id !== convo.id) {
      this.source = place ? new LocalSource(this, convo.id, place) : new ServerSource(this.relay, convo);
      this.files = [];
      this.sc?.dispose();
      this.sc = vscode.scm.createSourceControl("relay", `Relay: ${convo.title || "task"}`);
      this.group = this.sc.createResourceGroup("changes", "The agent's changes");
      this.group.hideWhenEmpty = false;
      this.sc.inputBox.placeholder = "A commit message for the task's changes (Ctrl+Enter)";
      this.sc.acceptInputCommand = { command: "relay.review.commit", title: "Commit" };
      this.sc.quickDiffProvider = { provideOriginalResource: (uri) => this.originalFor(uri) };
    }
    this.refresh();
  }

  async refresh() {
    clearTimeout(this.timer);
    if (!this.source || !this.relay.client) return;
    try {
      this.files = await this.source.load();
      this.error = null;
    } catch (error) {
      this.files = [];
      this.error = error.message;
    }
    const can = this.source.can(this.files);
    this.can = { ...can, reviewed: this.files.length > 0 };
    const root = this.source.root();
    this.texts.clear();
    this.group.label = this.error ? `Relay: ${this.error}` : this.files.length ? "The agent's changes"
      : "No changes since the task started or was last reviewed";
    this.group.resourceStates = this.files.map((file) => ({
      resourceUri: vscode.Uri.file(path.join(root, file.path)),
      command: { command: "relay.review.open", title: "Open the change", arguments: [file] },
      contextValue: can.revert ? "relay.revertable" : "relay.file",
      decorations: { strikeThrough: file.status === "deleted", faded: file.status === "deleted",
        tooltip: `${file.status}${file.from ? ` from ${file.from}` : ""}${file.binary ? ", binary"
          : ` · +${file.additions} −${file.deletions}`}`,
        iconPath: new vscode.ThemeIcon({ added: "diff-added", deleted: "diff-removed",
          renamed: "diff-renamed" }[file.status]
          || "diff-modified") },
    }));
    this.sc.count = this.files.length;
    this.sc.inputBox.visible = can.commit;
    this.context_(can);
    for (const file of this.files) this.contents.fire(this.uri(file));  // (the gutter's marks, again)
    this.onRefreshed();
    if (vscode.window.state.focused) this.timer = setTimeout(() => this.refresh(), LOOK);
  }

  later() {
    clearTimeout(this.soon);
    this.soon = setTimeout(() => this.refresh(), 1500);
  }

  context_(can = {}) {
    const set = (key, value) => vscode.commands.executeCommand("setContext", `relay.review.${key}`, value);
    set("files", this.files.length > 0);
    for (const key of ["commit", "merge", "discard", "undo"]) set(key, !!can[key]);
  }

  uri(file) {
    return vscode.Uri.from({ scheme: SCHEME, path: `/${this.source.id}/${file.path}` });
  }

  async text(uri) {
    const key = uri.toString();
    if (!this.texts.has(key)) {
      const file = this.files.find((f) => this.uri(f).toString() === key);
      this.texts.set(key, file ? this.source.original(file).then((t) => t ?? "") : Promise.resolve(""));
    }
    return this.texts.get(key);
  }

  // The task's file an editor has open, if it's one
  fileAt(uri) {
    if (!this.source || uri.scheme !== "file") return null;
    const root = this.source.root();
    const wanted = uri.toString();  // (as VSCodium writes a file's address: its drive letter, its separators)
    return this.files.find((f) => vscode.Uri.file(path.join(root, f.path)).toString() === wanted) || null;
  }

  // The gutter's marks: what an open file of the task was before it
  originalFor(uri) {
    const file = this.fileAt(uri);
    return file && file.status !== "added" && !file.binary ? this.uri(file) : undefined;
  }

  // A file of the task as it is on disk now
  nowText(file) {
    return readText(path.join(this.source.root(), file.path));
  }

  async open(file) {
    const now = vscode.Uri.file(path.join(this.source.root(), file.path));
    const was = await this.source.original(file);
    if (was === null || (this.source instanceof ServerSource && !this.source.here)) {
      const doc =
        await vscode.workspace.openTextDocument({ content: await this.source.diffText(file), language: "diff" });
      await vscode.window.showTextDocument(doc, { preview: true });
      return;
    }
    const left = this.uri(file);
    const right = file.status === "deleted"
      ? vscode.Uri.from({ scheme: SCHEME, path: `/${this.source.id}/now/${file.path}` })
      : now;
    if (file.status === "deleted") this.texts.set(right.toString(), Promise.resolve(""));
    await vscode.commands.executeCommand("vscode.diff", left, right,
      `${path.basename(file.path)} (before the agent ↔ now)`, { preview: true });
  }

  // What the review's buttons do
  async act(what, file) {
    const source = this.source;
    if (!source) return;
    try {
      if (what === "revert") {
        const sure = await vscode.window.showWarningMessage(`Put ${file.path} back as it was before the agent?`,
          { modal: true }, "Put it back");
        if (sure) await source.revert(file);
      } else if (what === "reviewed") {
        await source.reviewed();
      } else if (what === "commit") {
        const message = this.sc.inputBox.value.trim() || await vscode.window.showInputBox({
          title: "Relay: commit the task's changes", placeHolder: "The commit's message", ignoreFocusOut: true });
        if (!message?.trim()) return;
        const made = await source.commit(message.trim());
        this.sc.inputBox.value = "";
        vscode.window.showInformationMessage(`Relay: committed (${String(made || "").slice(0, 8)}).`);
      } else if (what === "merge") {
        vscode.window.showInformationMessage(`Relay: the task's work is in ${await source.merge()}.`);
      } else if (what === "discard") {
        const sure = await vscode.window.showWarningMessage(`Throw away the task's branch ${source.info?.branch}, with `
          + "all its work?", { modal: true }, "Discard");
        if (sure) await source.discard();
      } else if (what === "undo") {
        const sure = await vscode.window.showWarningMessage("Make the task's files what they were when it started (or "
          + "was last reviewed)? What they are now is kept, to put back.", { modal: true }, "Undo all");
        if (sure) this.offerBack(source, await source.undoAll(), "the start");
      }
    } catch (error) {
      vscode.window.showErrorMessage(`Relay: ${error.message}`);
    }
    await this.refresh();
  }

  // An answer undone (from the chat): the files as they were before it, and a way back
  async undoTurn(panel, run) {
    this.show(panel);
    const source = this.source;
    if (!source || source.id !== panel.id) return;
    const sure = await vscode.window.showWarningMessage("Make the files what they were before this answer? What "
      + "they are now is kept, to put back.", { modal: true }, "Undo it");
    if (!sure) return;
    try {
      this.offerBack(source, await source.undoTurn(run), "before that answer");
    } catch (error) {
      vscode.window.showErrorMessage(`Relay: ${error.message}`);
    }
    await this.refresh();
    panel.refresh();
  }

  offerBack(source, undo, when) {
    if (!undo) return;
    vscode.window.showInformationMessage(`Relay: the files are as they were ${when}.`, "Put them back")
      .then(async (picked) => {
        if (!picked) return;
        await Promise.resolve(source.putBack(undo))
          .catch((error) => vscode.window.showErrorMessage(`Relay: ${error.message}`));
        this.refresh();
      });
  }

  // A computer task's own record (in Chats' store): its checkpoints

  async keep(id, more) {
    await this.chats.remember(id, { ...this.chats.local(id), ...more });
  }

  // Its start: the files as they were, for "since the task started"
  async started(id) {
    const place = this.chats.local(id);
    if (!place || place.base) return;
    const points = new Checkpoints(place.folder, this.store());
    await this.keep(id, { base: await points.take("The task started"), turns: {} });
  }

  // The computer tasks a connector's job is in: those whose folder has its folder (or file) in it
  tasksAt(job) {
    const place = where(job.cwd || (job.path ? path.dirname(where(job.path)) : null));
    return Object.entries(this.chats.locals()).filter(([, local]) => {
      const folder = path.resolve(local.folder);
      return place === folder || place.startsWith(folder + path.sep);
    }).map(([id]) => id);
  }

  // Before a change on this computer (lib/connector.js): the files of each computer task in that folder as they are,
  // once for each of its turns, whoever started it (VSCodium, Relay's page, a timer)
  async beforeChange(job) {
    for (const id of this.tasksAt(job)) {
      const lock = (this.locks.get(id) || Promise.resolve()).then(async () => {
        const convo = await this.relay.client.get(`/api/conversations/${encodeURIComponent(id)}`);
        const run = convo.running_run;
        const now = this.chats.local(id);
        if (!run || now.turns?.[run]) return;
        const points = new Checkpoints(now.folder, this.store());
        const point = await points.take(`Before an answer (${run})`);
        await this.keep(id, { base: now.base || point, turns: { ...(now.turns || {}), [run]: point } });
      }).catch(() => {});  // (no checkpoint: the change goes on all the same)
      this.locks.set(id, lock);
      await lock;
    }
  }

  // What each answer of a computer task changed (said under it in the chat): from its checkpoint to the next one's
  async summaries(id, messages) {
    const place = this.chats.local(id);
    if (!place?.turns) return {};
    const points = new Checkpoints(place.folder, this.store());
    const runs = messages.filter((m) => m.role === "assistant" && m.run_id && m.status).map((m) => m.run_id);
    const known = { ...(place.summaries || {}) };
    let changed = false;
    for (const [n, run] of runs.entries()) {
      if (known[run] || !place.turns[run]) continue;
      const next = runs.slice(n + 1).map((r) => place.turns[r]).find(Boolean);
      try {
        // (an earlier answer: up to the next one's checkpoint; the last: up to now)
        const list = next ? await this.between(points, place.turns[run], next)
          : (await points.changes(place.turns[run])).files;
        known[run] = { files: list.length, additions: list.reduce((a, f) => a + f.additions, 0),
          deletions: list.reduce((a, f) => a + f.deletions, 0) };
        changed = true;
      } catch { /* (said nowhere: the answer just shows no changes) */ }
    }
    if (changed) await this.keep(id, { summaries: known });
    return known;
  }

  async between(points, from, to) {
    const numbers = await points.git(["diff", "--numstat", "-M", from, to]);
    return numbers.split("\n").filter(Boolean).map((line) => {
      const [added, deleted] = line.split("\t");
      return { additions: Number(added) || 0, deletions: Number(deleted) || 0 };
    });
  }
}

module.exports = { LocalSource, Review, SCHEME, ServerSource };
