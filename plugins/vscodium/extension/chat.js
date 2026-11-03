// The chat: a conversation of Relay's in an editor tab (a webview), each turn as it comes from the run's events.
// A new one is a draft until its first message: then it's a code task in its folder, or a chat.
"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const vscode = require("vscode");
const { answer, apply, turns, usageLine } = require("./lib/events");
const { instructions } = require("./computer");
const { problemLine } = require("./lib/tools");
const { diagnostics } = require("./tools");
const { inside, near, sameMachine } = require("./lib/places");

const FLUSH = 120;  // ms between the live answer's updates to the page
const QUIET = 1500;  // ms it looks again before it calls a conversation quiet (a next turn starts at once)
const RETRIES = 5;  // times a lost stream is taken up again
const TYPES = { ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
  ".webp": "image/webp", ".pdf": "application/pdf", ".txt": "text/plain", ".md": "text/markdown",
  ".json": "application/json", ".csv": "text/csv" };

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
// Relay's own slash commands (as Relay's page has them, what VSCodium can do of them): done here, not sent
const OWN = [["help", "what the / commands do"], ["clear", "the agent starts afresh (the messages stay)"],
  ["retry", "answer the last message again"], ["fork", "a new conversation with this one's messages"],
  ["stop", "stop the agent"], ["rename", "rename this conversation"], ["model", "switch the agent or its model"],
  ["mode", "switch mode"], ["effort", "how hard the model thinks (low … max)"],
  ["timed", "keep the agent at it: [min] max [optimal] in minutes (or 1h30), then the task; off stops the timer"],
  ["rewind", "the files as they were before an answer"], ["usage", "tokens and cost of this conversation"]];

// An answer's first words (for a list of answers)
function firstText(turn) {
  const text = (turn.blocks || []).find((b) => b.type === "text")?.text || "";
  return text.trim().split("\n")[0].slice(0, 80);
}

// Markdown as VSCodium shows it (its own Markdown extension); null when that can't: the page shows the text
async function markdown(text) {
  try {
    const html = await vscode.commands.executeCommand("markdown.api.render", text);
    return typeof html === "string" ? html : null;
  } catch {
    return null;
  }
}

const LOCAL = "relay.local";  // ({conversation id: {folder, computer}}: computer tasks' folders on this computer)

class Chats {
  constructor(context, relay, computer) {
    this.context = context;
    this.relay = relay;
    this.computer = computer;
    this.panels = new Set();
    this.last = null;  // (the one focused last: where "Ask Relay about this" goes)
    this.onFocus = () => {};
    this.onChanged = () => {};
    this.side = new SideChat(this);
    // ("the open file": the text editor used last, its document and selection, also while a chat has the focus or
    // covers it in the same group; gone when its file is closed)
    this.editor = null;
    let soon = null;
    const tell = () => {
      clearTimeout(soon);
      soon = setTimeout(() => this.panels.forEach((panel) => panel.sendContext()), 150);
    };
    const look = () => {
      const editor = vscode.window.activeTextEditor;
      if (editor && editor.document.uri.scheme === "file") {
        this.editor = { document: editor.document, selection: editor.selection };
      }
      tell();
    };
    context.subscriptions.push(vscode.window.onDidChangeActiveTextEditor(look),
      vscode.window.onDidChangeTextEditorSelection(look),
      vscode.workspace.onDidCloseTextDocument((document) => {
        if (this.editor?.document === document) {
          this.editor = null;
          tell();
        }
      }));
    look();
  }

  find(id) {
    return [...this.panels].find((p) => p.id === id) || null;
  }

  // A computer task's folder here (VSCodium keeps it: Relay knows the computer, not the folder)
  local(id) {
    return (this.context.globalState.get(LOCAL) || {})[id] || null;
  }

  locals() {
    return this.context.globalState.get(LOCAL) || {};
  }

  async remember(id, place) {
    await this.context.globalState.update(LOCAL, { ...(this.context.globalState.get(LOCAL) || {}), [id]: place });
  }

  sideBar(asked) {
    return asked ?? vscode.workspace.getConfiguration("relay").get("openIn") === "sidebar";
  }

  open(id, { side } = {}) {
    const found = this.find(id);
    if (found && (side === undefined || found.panel.side === side)) {
      found.panel.reveal();
      return found;
    }
    if (found) found.panel.dispose();  // (moved: to the side bar, or out of it)
    return this.sideBar(side) ? this.side.show({ id }) : new ChatPanel(this, { id });
  }

  draft(folder, { side } = {}) {
    return this.sideBar(side) ? this.side.show({ folder }) : new ChatPanel(this, { folder });
  }

  // A chat's tab after a reload of the window (VSCodium kept it, with the page's state: its conversation)
  restore(kept, state) {
    if (state?.id) return new ChatPanel(this, { id: String(state.id), kept });
    return new ChatPanel(this, { folder: state?.folder || null, kept });
  }

  // A file an agent wrote on this computer (lib/connector.js): shown beside the chat of its task, if one's in sight
  followed(file, ids) {
    for (const panel of this.panels) {
      if (ids.includes(panel.id)) panel.follow_(file);
    }
  }

  // Where text about a file goes: the chat focused last, if it works in that folder (or is a chat), else a new
  // code task there
  async for(folder) {
    const last = this.last && this.panels.has(this.last) ? this.last : null;
    const place = last && last.where();
    if (last && (!folder || !place || near(folder, place))) return last;
    return this.draft(folder);
  }

  // The conversations as the Conversations view found them: an open chat follows an agent at work in it, or loads
  // what changed since, whoever did it (Relay's page, a timer, a script)
  seen(items) {
    for (const panel of this.panels) {
      const found = panel.id && panel.ready && !panel.following ? items.find((c) => c.id === panel.id) : null;
      if (!found) continue;
      if (found.running_run) panel.follow();
      else if (panel.convo && found.updated > panel.convo.updated) panel.load().catch((error) => panel.fail(error));
    }
  }

  focused(panel) {
    this.last = panel;
    this.onFocus(panel);
  }

  closed(panel) {
    this.panels.delete(panel);
    if (this.last === panel) this.last = null;
  }

  // Signed in again (or out): each chat loads anew
  reload() {
    for (const panel of this.panels) panel.load();
  }
}

// Where a chat shows: an editor tab of its own, or the side bar's Chat view (one chat at a time, dragged to the
// secondary side bar if you like). Both look the same to the chat: a webview, a title, whether it's in sight.
function tab(chats, kept = null) {
  const media = vscode.Uri.joinPath(chats.context.extensionUri, "media");
  const beside = vscode.window.activeTextEditor ? vscode.ViewColumn.Beside : vscode.ViewColumn.Active;
  // (a tab VSCodium kept from before a reload of the window: the same chat in it again)
  const panel = kept || vscode.window.createWebviewPanel("relay.chat", "Relay", { viewColumn: beside },
    { enableScripts: true, retainContextWhenHidden: true, localResourceRoots: [media] });
  if (kept) panel.webview.options = { enableScripts: true, localResourceRoots: [media] };
  panel.iconPath =
    { light: vscode.Uri.joinPath(media, "relay-light.svg"), dark: vscode.Uri.joinPath(media, "relay-dark.svg") };
  return {
    side: false, webview: panel.webview,
    get title() { return panel.title; },
    set title(text) { panel.title = text; },
    get visible() { return panel.visible; },
    get viewColumn() { return panel.viewColumn; },
    reveal: () => panel.reveal(),
    dispose: () => panel.dispose(),
    listen: (handle) => panel.webview.onDidReceiveMessage(handle),
    onFocus: (then) => panel.onDidChangeViewState((event) => event.webviewPanel.active && then()),
    onDispose: (then) => panel.onDidDispose(then),
  };
}

class SideChat {
  constructor(chats) {
    this.chats = chats;
    this.view = null;
    this.current = null;  // (the chat it shows)
    this.waiting = [];  // (what to show once VSCodium has made the view)
  }

  resolveWebviewView(view) {
    this.view = view;
    view.webview.options = { enableScripts: true,
      localResourceRoots: [vscode.Uri.joinPath(this.chats.context.extensionUri, "media")] };
    view.webview.onDidReceiveMessage((message) =>
      this.current?.handle(message).catch((error) => this.current?.fail(error)));
    view.onDidChangeVisibility(() => view.visible && this.current && this.chats.focused(this.current));
    view.onDidDispose(() => {
      // (made anew when it's moved, to the secondary side bar say: the same chat shows again then)
      this.again = this.current && !this.current.disposed ? { id: this.current.id, folder: this.current.folder } : null;
      this.view = null;
      this.current?.closing();
      this.current = null;
    });
    if (!this.waiting.length) {
      if (this.again?.id) this.show({ id: this.again.id });
      else if (!this.current) this.chats.draft(this.again?.folder || null, { side: true });  // (a new chat, to start)
    }
    for (const then of this.waiting.splice(0)) then();
  }

  // A chat in the view (instead of the one it shows)
  async show(options) {
    if (!this.view) {
      await new Promise((resolve) => {
        this.waiting.push(resolve);
        vscode.commands.executeCommand("relay.chat.focus");
      });
    }
    this.current?.closing();
    const view = this.view;
    const ends = [];
    const surface = {
      side: true, webview: view.webview,
      get title() { return view.description || "Relay"; },
      set title(text) { view.description = text; },
      get visible() { return view.visible; },
      get viewColumn() { return undefined; },
      reveal: () => view.show(false),
      dispose: () => ends.splice(0).forEach((then) => then()),
      listen: () => ({ dispose() {} }),  // (the view's listener hands everything to the chat it shows)
      onFocus: () => {},
      onDispose: (then) => ends.push(then),
    };
    this.current = new ChatPanel(this.chats, { ...options, surface });
    view.show(false);
    return this.current;
  }
}

class ChatPanel {
  constructor(chats, { id = null, folder = null, surface = null, kept = null }) {
    this.chats = chats;
    this.relay = chats.relay;
    this.id = id;
    this.folder = folder;  // (a draft's: where its code task will be; null: a chat)
    this.convo = null;
    this.turns = [];
    this.live = null;
    this.pick = { provider: null, model: null, mode: null };  // (a draft's agent and mode)
    this.attachments = [];
    this.following = false;
    this.disposed = false;
    this.ready = false;
    this.rendered = new Map();  // (a text's HTML: blocks are made anew each time the conversation is shown)
    this.sent = new Map();  // (each tool's output length as last sent while its answer comes)
    this.pending = null;  // (a message to send once the page is there: Fix with Relay)
    this.panel = surface || tab(chats, kept);
    this.panel.webview.html = page(this.panel.webview, vscode.Uri.joinPath(chats.context.extensionUri, "media"));
    this.panel.listen((message) => this.handle(message).catch((error) => this.fail(error)));
    this.panel.onFocus(() => chats.focused(this));
    this.panel.onDispose(() => this.closing());
    chats.panels.add(this);
    chats.focused(this);
  }

  // Closed, or the side bar shows another chat: what it follows stops
  closing() {
    if (this.disposed) return;
    this.disposed = true;
    this.abort?.abort();
    this.chats.closed(this);
  }

  post(message) {
    if (!this.disposed) this.panel.webview.postMessage(message);
  }

  fail(error) {
    this.post({ type: "error", message: error?.message || String(error) });
  }

  // From the page
  async handle(message) {
    switch (message.type) {
      case "ready":
        this.ready = true;
        await this.load();
        this.sendContext();
        this.chats.permissions?.showIn(this);
        if (this.pending) {
          const text = this.pending;
          this.pending = null;
          await this.send(text);
        }
        break;
      case "send":
        await this.send(String(message.text || ""), { context: !!message.context });
        break;
      case "complete":
        this.post({ type: "completions", kind: message.kind, query: message.query,
          items: message.kind === "slash" ? await this.slashes(String(message.query || ""))
            : await this.mentions(String(message.query || "")) });
        break;
      case "edit":
        await this.again(this.client().post(`/api/conversations/${encodeURIComponent(this.id)}/retry`,
          { message_id: message.id, text: String(message.text || "") }));
        break;
      case "retry":
        await this.again(this.client().post(`/api/conversations/${encodeURIComponent(this.id)}/retry`,
          message.prompt ? { message_id: message.prompt } : {}));
        break;
      case "fork":
        await this.fork(message.id || null);
        break;
      case "upload":
        await this.upload(String(message.name || "file"),
          Buffer.from(String(message.data || ""), "base64"), message.mime);
        break;
      case "dropped":
        await this.dropped(message.uris || []);
        break;
      case "queueSend":
        await this.client().post(`/api/conversations/${encodeURIComponent(this.id)}/queue/${Number(message.id)}/send`);
        await this.refresh();
        this.follow();
        break;
      case "queueRemove":
        await this.client().del(`/api/conversations/${encodeURIComponent(this.id)}/queue/${Number(message.id)}`);
        await this.refresh();
        break;
      case "effort":
        await this.pickEffort();
        break;
      case "move":
        await this.move();
        break;
      case "stop":
        await this.stop();
        break;
      case "permission":
        await this.client().post(`/api/runs/${encodeURIComponent(message.run)}/permission`,
          { id: message.id, decision: String(message.decision) });
        break;
      case "localAnswer":  // (VSCodium's own question: permissions.js)
        this.chats.permissions?.answer(String(message.id), String(message.decision));
        break;
      case "localChange":
        await this.chats.permissions?.showChange(String(message.id));
        break;
      case "form":
        await this.client().post(`/api/runs/${encodeURIComponent(message.run)}/form`,
          { id: message.id, answer: message.answer });
        break;
      case "agent":
        await this.pickAgent();
        break;
      case "mode":
        await this.pickMode();
        break;
      case "attach":
        await this.attach();
        break;
      case "unattach":
        this.attachments = this.attachments.filter((a) => a.id !== message.id);
        this.post({ type: "attachments", items: this.attachments });
        break;
      case "changes":
        this.chats.review.show(this);
        await vscode.commands.executeCommand("workbench.view.scm");
        break;
      case "undoTurn":
        await this.chats.review.undoTurn(this, String(message.run));
        break;
      case "open":
        await vscode.commands.executeCommand("relay.open", { id: String(message.id) });
        break;
      case "relay":
        if (this.id) await vscode.commands.executeCommand("relay.openInRelay", { id: this.id });
        break;
      case "file":
        await this.openFile(String(message.path || ""), message.line);
        break;
      case "link":
        await this.link(String(message.href || ""));
        break;
      case "signIn":
        await vscode.commands.executeCommand("relay.signIn");
        break;
      default:
        break;
    }
  }

  client() {
    if (!this.relay.client) throw new Error("Not signed in to Relay: Relay: Sign in");
    return this.relay.client;
  }

  // The conversation as Relay has it now, and its answer still coming followed
  async load() {
    if (!this.ready || this.disposed) return;
    if (!this.relay.client) {
      this.post({ type: "signedOut" });
      return;
    }
    if (!this.id) {
      await this.draftAgent();
      this.post({ type: "conversation", info: this.info() });
      this.post({ type: "turns", turns: [] });
      return;
    }
    const convo = await this.client().get(`/api/conversations/${encodeURIComponent(this.id)}`);
    this.show(convo);
    if (convo.running_run) this.follow();
    if (!this.efforts) this.agentCommands().then(() => this.post({ type: "conversation", info: this.info() }));
  }

  show(convo, running = convo.running_run) {
    this.convo = convo;
    this.panel.title = convo.title || "Relay";
    this.turns = turns(convo.messages, running);
    if (convo.computer && this.chats.local(convo.id)) this.localChanges(convo);
    if (running) {
      this.live = this.live?.run === running ? this.live : answer(running);
      this.turns.push(this.live);
    }
    this.post({ type: "conversation", info: this.info() });
    this.sendTurns();
    if (this.chats.last === this) this.chats.onFocus(this);  // (the review: this task's, now it's known)
  }

  info() {
    const c = this.convo;
    const agent = c
      ? this.relay.agentName(c.running_agent?.provider || c.provider_id, c.running_agent?.model ?? c.model)
      : this.relay.agentName(this.pick.provider, this.pick.model);
    const mode = c ? c.agent_mode : this.pick.mode;
    const folder = this.where();
    return {
      id: this.id, title: c?.title || (this.folder ? "New code task" : "New chat"), agent,
      mode: this.relay.modeName(mode), code: !!folder, folder: folder ? path.basename(folder) : null,
      folderPath: folder, computer: c?.computer ? { name: c.computer.name, online: !!c.computer.online } : null,
      running: !!c?.running_run, asks: !!c?.needs_approval, queued: (c?.queued || []).length,
      queue: (c?.queued || []).map((q) => ({ id: q.id, text: q.text || q.content || "" })),
      changes: c?.changes || null, temporary: !!c?.temporary,
      effort: (c ? c.effort : this.pick.effort) || "default", efforts: this.efforts || [],
      limit: this.relay.limitOf(c ? c.provider_id : this.pick.provider), side: !!this.panel.side,
    };
  }

  // A computer task's answers: what each changed here, from VSCodium's own checkpoints (the review's)
  async localChanges(convo) {
    const known = await this.chats.review.summaries(convo.id, convo.messages).catch(() => ({}));
    const turns = this.chats.local(convo.id)?.turns || {};
    let changed = false;
    for (const turn of this.turns) {
      if (turn.role !== "assistant" || !turn.run || turn === this.live) continue;
      const found = known[turn.run] || null;
      if (found !== turn.changes || !!turns[turn.run] !== turn.undo) {
        Object.assign(turn, { changes: found, undo: !!turns[turn.run] });
        changed = true;
      }
    }
    if (changed && this.convo === convo) this.sendTurns();
  }

  // The folder it works in: a code task's (on Relay's machine), a computer task's here, a draft's; null for a chat
  where() {
    const c = this.convo;
    if (!c) return this.folder;
    if (c.mode === "code") return c.directory;
    return c.computer ? this.chats.local(this.id)?.folder || null : null;
  }

  // The turns to the page, each text block as HTML (rendered again only when its text changed)
  async withHtml(turn) {
    if (turn.role !== "assistant") return turn;
    turn.used = usageLine(turn.usage);
    const blocks = [];
    for (const block of turn.blocks) {
      if (block.type !== "text") {
        blocks.push(block);
        continue;
      }
      let html = this.rendered.get(block.text);
      if (html === undefined) {
        html = await markdown(block.text);
        if (this.rendered.size > 500) this.rendered.delete(this.rendered.keys().next().value);  // (the oldest)
        this.rendered.set(block.text, html);
      }
      blocks.push({ ...block, html });
    }
    return { ...turn, blocks };
  }

  async sendTurns() {
    const all = [];
    for (const turn of this.turns) all.push(await this.withHtml(turn));
    this.post({ type: "turns", turns: all });
  }

  async sendLive() {
    if (!this.live) return;
    const index = this.turns.indexOf(this.live);
    if (index < 0) return;
    const turn = await this.withHtml(this.live);
    // (a tool's output that's as it was sent before stays as the page has it: an answer with many tools stays light)
    turn.blocks = turn.blocks.map((b) => {
      if (b.type !== "tool") return b;
      const key = `${this.live.run}:${b.id}`;
      if (this.sent.get(key) === b.output.length) return { ...b, output: null, kept: true };
      this.sent.set(key, b.output.length);
      return b;
    });
    this.post({ type: "turn", index, turn });
  }

  // Each turn as it comes, the one going now first, until it's quiet: nothing running, nothing queued (Relay's
  // Python SDK's follow(): a timed task's go-ons, a fallback agent taking over, the queue's messages)
  async follow() {
    if (this.following || !this.id) return;
    this.following = true;
    let lookedAgain = false;
    let lost = 0;
    let ended = null;  // (the last answer: said when it's all quiet)
    const streamed = new Set();
    try {
      while (!this.disposed && this.relay.client) {
        const convo = await this.client().get(`/api/conversations/${encodeURIComponent(this.id)}`);
        if (lost && !convo.running_run) this.post({ type: "notice", message: "" });  // (taken up again: said no more)
        const run = convo.running_run;
        if (run && (!streamed.has(run) || lost)) {
          lookedAgain = false;
          this.show(convo, run);
          try {
            await this.stream(run);
            streamed.add(run);
            if (lost) this.post({ type: "notice", message: "" });
            lost = 0;
            ended = this.live;
          } catch (error) {
            if (this.disposed || ++lost > RETRIES) throw error;
            this.post({ type: "notice", message: `${error.message}: taking it up again…` });
            await sleep(2000);
          }
          continue;
        }
        if (run || (convo.queued || []).length) {
          this.convo = convo;
          this.post({ type: "conversation", info: this.info() });
          await sleep(300);
          continue;
        }
        if (!lookedAgain) {
          lookedAgain = true;
          await sleep(QUIET);
          continue;
        }
        this.live = null;
        this.show(convo);
        this.chats.onChanged(this);
        if (ended) this.done(ended);
        return;
      }
    } catch (error) {
      this.live = null;
      this.fail(error);
    } finally {
      this.following = false;
    }
  }

  async stream(run) {
    this.abort = new AbortController();
    this.sent.clear();  // (a new answer, or the same played back: its tools' output is sent once again)
    let caughtUp = false;
    let timer = null;
    const flush = () => {
      timer = null;
      this.sendLive();
    };
    this.live = answer(run);
    this.turns[this.turns.length - 1] = this.live;
    await this.client().events(run, (event) => {
      if (event.type === "caught-up") {
        caughtUp = true;
        flush();
        return;
      }
      if (event.type === "title" && event.title) this.panel.title = event.title;
      const changed = apply(this.live, event);
      // (following the agent: a file it changed now, beside the chat)
      if (caughtUp && event.type === "tool_result") {
        const tool = this.live.blocks.find((b) => b.type === "tool" && b.id === event.id);
        if (tool?.path && tool.status === "done" && (tool.kind === "edit" || /write/i.test(tool.name))) {
          this.follow_(tool.path, (tool.edit || []).find(([sign]) => sign === "+")?.[1]);
        }
      }
      // (a question that comes now: what's played back before caught-up was asked, and maybe answered, before)
      if (caughtUp && ((event.type === "permission" && event.decision == null)
          || (event.type === "form" && event.status === "pending"))) {
        this.asked(event);
      }
      // (the header says so while it waits for you)
      const asks = this.live.blocks.some((b) => (b.type === "permission" && b.decision === null)
        || (b.type === "form" && b.status === "pending"));
      if (this.convo && caughtUp && asks !== !!this.convo.needs_approval) {
        this.convo.needs_approval = asks;
        this.post({ type: "conversation", info: this.info() });
      }
      if (changed && caughtUp && !timer) timer = setTimeout(flush, FLUSH);
    }, { signal: this.abort.signal });
    clearTimeout(timer);
    if (!this.live.status && !this.disposed) apply(this.live, { type: "done", status: "succeeded" });
    await this.sendLive();
  }

  // An answer done while its chat isn't in sight (or VSCodium isn't): said, with a way to it
  done(last) {
    const ended = last?.status;
    if (!ended || ended === "interrupted"
      || !vscode.workspace.getConfiguration("relay").get("notifyDone", true)) return;
    if (this.panel.visible && vscode.window.state.focused) return;
    const error = [...last.blocks].reverse().find((b) => b.type === "error")?.message;
    const said = ended === "failed" ? `failed${error ? `: ${error.split("\n")[0].slice(0, 120)}` : ""}` : "is done";
    vscode.window.showInformationMessage(`Relay: “${this.panel.title}” ${said}.`, "Open").then((picked) => {
      if (picked) this.panel.reveal();
    });
  }

  // A fork: a new conversation with this one's messages, in a tab of its own (a computer task's keeps its folder here,
  // and its start)
  async fork(message) {
    const made = await this.client().post(`/api/conversations/${encodeURIComponent(this.id)}/fork`,
      message ? { message_id: message } : {});
    const local = this.chats.local(this.id);
    if (local) await this.chats.remember(made.id, { folder: local.folder, computer: local.computer, base: local.base });
    this.chats.onChanged(this);
    await this.chats.open(made.id);
  }

  // A turn started again (edit, retry): followed, as a message is
  async again(started) {
    await started;
    this.post({ type: "sent" });
    this.follow();
  }

  // The open file (and what's selected in it), for the chip above the message: what goes along when it's sent
  contextOf() {
    const editor = this.chats.editor;
    if (!editor || !vscode.workspace.getConfiguration("relay").get("context.activeFile", true)) return null;
    const file = this.place(editor.document.uri.fsPath);
    const selection = editor.selection;
    const last = selection.end.line
      - (selection.end.character === 0 && selection.end.line > selection.start.line ? 1 : 0);
    const lines = selection.isEmpty ? null
      : selection.start.line === last ? `${selection.start.line + 1}` : `${selection.start.line + 1}-${last + 1}`;
    return { label: `${path.basename(file)}${lines ? `:${lines}` : ""}`,
      title: `${file}${lines ? `, lines ${lines}` : ""}`,
      key: `${editor.document.uri.fsPath}:${lines || ""}` };
  }

  sendContext() {
    if (this.ready) this.post({ type: "context", context: this.contextOf() });
  }

  contextText() {
    const editor = this.chats.editor;
    if (!editor) return "";
    const file = this.place(editor.document.uri.fsPath);
    const where = `in VSCodium${this.elsewhere()}`;
    const selection = editor.selection;
    if (selection.isEmpty) return `\n\n(Open ${where}: ${file})`;
    const shown = this.contextOf();
    const code = editor.document.getText(selection).replace(/\r?\n$/, "").split("\n");
    const cut = code.length > 200 ? `\n… (${code.length - 200} more lines)` : "";
    const fence = code.some((l) => l.startsWith("```")) ? "````" : "```";
    return `\n\n(Selected ${where}: ${file}:${shown.label.split(":").pop()})\n${fence}${editor.document.languageId}\n`
      + `${code.slice(0, 200).join("\n")}${cut}\n${fence}`;
  }

  // Which computer VSCodium's files are on, when that isn't Relay's machine and the chat isn't a task there: the agent
  // reaches them as that computer's (computer_read_file, relay pull), or can't now
  elsewhere() {
    if (this.convo?.computer || this.convo?.mode === "code" || sameMachine(this.relay.url())) return "";
    const on = this.chats.computer.saved;
    return on.on && on.name ? `, on the user's computer “${on.name}”`
      : ", on the user's own computer (Relay can't reach "
      + "it now: they can let it in VSCodium, Relay: This computer)";
  }

  // @problems: the folder's errors and warnings, as VSCodium has them now
  expand(text) {
    if (!/(^|\s)@problems\b/.test(text)) return text;
    const folder = this.where();
    const found = diagnostics(folder, (file) => this.place(file)).map(problemLine);
    return `${text}\n\n${found.length ? `The problems VSCodium sees${folder ? " in the folder" : ""}:\n`
      + found.slice(0, 60).join("\n") + (found.length > 60 ? `\n… and ${found.length - 60} more` : "")
      : "(VSCodium sees no errors or warnings.)"}`;
  }

  // What @ offers: @problems, and the folder's files
  async mentions(query) {
    const items = [];
    if ("problems".startsWith(query.toLowerCase())) {
      items.push({ label: "@problems",
        detail: "the folder's errors and warnings, as VSCodium sees them", insert: "@problems " });
    }
    const folder = this.where();
    const base = folder ? vscode.Uri.file(folder) : vscode.workspace.workspaceFolders?.[0]?.uri;
    if (base && base.scheme === "file") {
      const glob = query.replace(/[[\]{}*?!]/g, "");
      const found = await vscode.workspace.findFiles(new vscode.RelativePattern(base, `**/*${glob}*`),
        "**/{node_modules,.git,dist,build,.venv,__pycache__}/**", 20);
      for (const uri of found) {
        const relative = path.relative(base.fsPath, uri.fsPath);
        items.push({ label: relative, detail: "", insert: `@${relative} ` });
      }
    }
    return items;
  }

  // What / offers: Relay's own commands, the agent's, the skills
  async slashes(query) {
    const all = [...OWN.map(([name, detail]) => ({ name, detail })), ...await this.agentCommands()];
    const q = query.toLowerCase();
    return all.filter((c) => c.name.toLowerCase().startsWith(q)).slice(0, 40)
      .map((c) => ({ label: `/${c.name}`, detail: c.detail, insert: `/${c.name} ` }));
  }

  async agentCommands() {
    if (!this.commandsCache) {
      const found = [];
      try {
        if (this.id) {
          const info = await this.client().get(`/api/conversations/${encodeURIComponent(this.id)}/commands`);
          this.efforts = info.efforts || [];
          for (const c of info.commands || []) {
            if (!OWN.some(([name]) => name === c.name)) found.push({ name: c.name,
              detail: c.description || "the agent's" });
          }
        }
        const skills = await this.client().get("/api/skills");
        for (const skill of skills.skills || []) {
          if (!found.some((c) => c.name === skill.name) && !OWN.some(([name]) => name === skill.name)) {
            found.push({ name: skill.name, detail: `skill: ${String(skill.description || "").split(". ")[0]}` });
          }
        }
      } catch { /* (none known: Relay's own only) */ }
      if (!this.id) return found;  // (a draft: the agent's own are asked once it's a conversation)
      this.commandsCache = found;
    }
    return this.commandsCache;
  }

  // A slash command of Relay's own (as Relay's page has them), done here; false for one that goes to the agent
  async command(text) {
    const [, name, rest] = /^\/([\w-]+)\s*([\s\S]*)$/.exec(text);
    const arg = rest.trim();
    if (!OWN.some(([own]) => own === name)) return false;
    const id = this.id && encodeURIComponent(this.id);
    const needs = () => {
      if (!id) throw new Error(`/${name} works in a conversation: send it a message first`);
    };
    switch (name) {
      case "help": {
        const picked = await vscode.window.showQuickPick([...OWN.map(([n, d]) => ({ label: `/${n}`, detail: d })),
          ...(await this.agentCommands()).map((c) => ({ label: `/${c.name}`, detail: c.detail }))],
        { title: "Relay: the / commands", matchOnDetail: true });
        if (picked) this.insert(`${picked.label} `);
        return true;
      }
      case "clear":
        needs();
        await this.client().post(`/api/conversations/${id}/clear`);
        this.post({ type: "notice", message: "The agent starts afresh from the next message (the messages stay)." });
        return true;
      case "retry":
        needs();
        await this.again(this.client().post(`/api/conversations/${id}/retry`, {}));
        return true;
      case "fork":
        needs();
        await this.fork(null);
        return true;
      case "stop":
        await this.stop();
        return true;
      case "rename": {
        needs();
        const title = arg
          || await vscode.window.showInputBox({ title: "Relay: rename", value: this.convo?.title || "" });
        if (title?.trim()) await this.client().patch(`/api/conversations/${id}`, { title: title.trim() });
        await this.refresh();
        this.chats.onChanged(this);
        return true;
      }
      case "model":
      case "agent":
        await this.pickAgent();
        return true;
      case "mode":
        if (!arg) {
          await this.pickMode();
        } else {
          const found =
            (await this.relay.modes()).find((m) => [m.id, (m.name || "").toLowerCase()].includes(arg.toLowerCase()));
          if (!found) throw new Error(`No mode “${arg}”`);
          await this.setMode(found.id);
        }
        return true;
      case "effort":
        await this.pickEffort(arg || null);
        return true;
      case "timed": {
        needs();
        const timed = await this.client().post(`/api/conversations/${id}/timed`, arg.toLowerCase() === "off"
          ? { off: true } : { text: arg });
        if (timed.task) await this.send(timed.task);
        else this.post({ type: "notice", message: arg.toLowerCase() === "off" ? "Timer stopped: the agent may stop "
          + "when it's done." : "/timed [min] max [optimal] task: minutes (or 1h30); before min the agent goes on, at "
          + "max it stops." });
        return true;
      }
      case "rewind": {
        needs();
        const answers = this.turns.filter((t) => t.role === "assistant" && t.undo).reverse();
        const picked = await vscode.window.showQuickPick(answers.map((t) => ({ label: firstText(t) || "(an answer)",
          description: t.changes?.files
            ? `${t.changes.files} files +${t.changes.additions} −${t.changes.deletions}` : "",
          run: t.run })), { title: "Relay: the files as they were before which answer?" });
        if (picked) await this.chats.review.undoTurn(this, picked.run);
        return true;
      }
      case "usage": {
        const answers = this.turns.filter((t) => t.role === "assistant" && t.usage?.tokens);
        const sum = (key) => answers.reduce((n, t) => n + (Number(t.usage.tokens[key]) || 0), 0);
        const cost = answers.reduce((n, t) => n + (Number(t.usage.cost) || 0), 0);
        vscode.window.showInformationMessage(
          `Relay: ${answers.length} answers, ${sum("input_tokens").toLocaleString()} `
          + `tokens in, ${sum("output_tokens").toLocaleString()} out${cost ? `, about $${cost.toFixed(2)}` : ""}.`);
        return true;
      }
      default:
        return false;
    }
  }

  async pickEffort(given = null) {
    if (this.id && !this.efforts) await this.agentCommands();
    const levels = ["default", ...(this.efforts?.length ? this.efforts : ["low", "medium", "high", "xhigh", "max"])];
    const current = (this.convo ? this.convo.effort : this.pick.effort) || "default";
    const effort = given || (await vscode.window.showQuickPick(levels.map((e) => ({ label: e,
      description: e === current ? "now" : "" })), { title: "Relay: how hard the model thinks" }))?.label;
    if (!effort) return;
    if (!levels.includes(effort)) throw new Error(`Effort is one of ${levels.join(", ")}`);
    if (this.id) {
      await this.client().patch(`/api/conversations/${encodeURIComponent(this.id)}`, { effort });
      await this.refresh();
    } else {
      this.pick = { ...this.pick, effort };
      this.post({ type: "conversation", info: this.info() });
    }
  }

  // Files the page was given (pasted, dropped from the computer): attachments
  async upload(name, bytes, type) {
    const made = await this.client().upload(name, bytes, type || TYPES[path.extname(name).toLowerCase()]);
    this.attachments.push({ id: made.id, name: made.name || name });
    this.post({ type: "attachments", items: this.attachments });
  }

  // Files dropped from VSCodium (the Explorer, with Shift): the folder's text files @-named, the rest attachments
  async dropped(uris) {
    const folder = this.where();
    const named = [];
    for (const given of uris) {
      const uri = vscode.Uri.parse(String(given));
      const kind = TYPES[path.extname(uri.path).toLowerCase()] || "";
      if (uri.scheme === "file" && folder && inside(uri.fsPath, folder) && !/^(image|application\/pdf)/.test(kind)) {
        named.push(`@${path.relative(folder, uri.fsPath)}`);
      } else {
        await this.upload(path.basename(uri.path), await vscode.workspace.fs.readFile(uri), kind);
      }
    }
    if (named.length) this.insert(`${named.join(" ")} `);
  }

  // The chat in the side bar (or, from there, in an editor tab)
  async move() {
    if (!this.id) {
      this.panel.dispose();
      await this.chats.draft(this.folder, { side: !this.panel.side });
      return;
    }
    await this.chats.open(this.id, { side: !this.panel.side });
  }

  // Fix with Relay: a message sent once the page is there
  submit(text) {
    if (this.ready) this.send(text).catch((error) => this.fail(error));
    else this.pending = text;
    this.panel.reveal();
  }

  // A question while its tab isn't in sight: said, with a way to it
  asked(event) {
    if (this.panel.visible) return;
    const what = event.type === "form" ? event.title || "a question" : `to allow ${event.action || "a tool"}`;
    vscode.window.showInformationMessage(`Relay: “${this.panel.title}” asks ${what}.`, "Open").then((picked) => {
      if (picked) this.panel.reveal();
    });
  }

  // A draft's agent: the one Relay's page picked for a new chat last, else the latest conversation's
  async draftAgent() {
    if (this.pick.provider) return;
    try {
      const state = await this.client().get("/api/state");
      if (state.new_chat?.provider_id) {
        this.pick = { ...this.pick, provider: state.new_chat.provider_id, model: state.new_chat.model ?? null };
        return;
      }
      const latest = (await this.client().get("/api/conversations"))[0];
      if (latest) this.pick = { ...this.pick, provider: latest.provider_id, model: latest.model };
    } catch (error) {
      this.fail(error);
    }
  }

  // A draft made a conversation: a code task in its folder (a chat when Relay can't work there)
  async create() {
    await this.draftAgent();
    if (!this.pick.provider) throw new Error("Pick an agent first: Relay has none set up for a new chat");
    const data = { provider_id: this.pick.provider, mode: this.folder ? "code" : "chat" };
    if (this.pick.model) data.model = this.pick.model;
    if (this.pick.mode) data.agent_mode = this.pick.mode;
    if (this.pick.effort && this.pick.effort !== "default") data.effort = this.pick.effort;
    let made;
    if (this.folder) {
      try {
        made = await this.client().post("/api/conversations", { ...data, directory: this.folder });
      } catch (error) {
        // (Relay's words for a folder that isn't one of its own: not in a workspace root, or not on its machine)
        if (error.status !== 400 || !/workspace root/i.test(error.message)) throw error;
        // (not a folder of Relay's machine: on this computer, when the user lets Relay's agents work here)
        const computer = await this.chats.computer.ensure();
        if (computer) {
          made = await this.client().post("/api/conversations", { ...data, mode: "chat", computer_id: computer.id,
            instructions: instructions(this.folder, computer.name) });
          await this.chats.remember(made.id, { folder: this.folder, computer: computer.name });
          // (its start, before its first message: what "since it started" counts from)
          await this.chats.review.started(made.id).catch(() => {});
        } else {
          vscode.window.showWarningMessage(`Relay can't work in ${this.folder} (${error.message}): this is a chat, `
            + "without its files. Relay → Settings → Workspace roots says where it can, or Relay: This computer lets "
            + "its agents work in this computer's folders.");
          this.folder = null;
        }
      }
    }
    made = made || await this.client().post("/api/conversations", { ...data, mode: "chat" });
    this.id = made.id;
    this.commandsCache = null;  // (the agent's commands and efforts are known now: looked at again)
    this.efforts = null;
    this.chats.onChanged(this);
  }

  async send(text, { context = false } = {}) {
    if (!text.trim() && !this.attachments.length) return;
    if (/^\/[\w-]+/.test(text.trim()) && await this.command(text.trim())) {
      this.post({ type: "sent" });
      return;
    }
    // (a command for the agent, /compact say, goes as it is: what follows it would be its arguments)
    text = this.expand(text) + (context && !text.trim().startsWith("/") ? this.contextText() : "");
    if (!this.id) await this.create();
    const attachments = this.attachments.map((a) => a.id);
    const id = encodeURIComponent(this.id);
    if (this.convo?.running_run || this.following) {
      await this.client().post(`/api/conversations/${id}/queue`, { text, attachments });
    } else {
      await this.client().post(`/api/conversations/${id}/messages`, { text, attachments });
    }
    this.attachments = [];
    this.post({ type: "attachments", items: [] });
    this.post({ type: "sent" });
    this.follow();
  }

  async stop() {
    const run = this.live?.run || this.convo?.running_run;
    if (run) await this.client().post(`/api/runs/${encodeURIComponent(run)}/stop`);
  }

  // The agent: one of Relay's providers, then one of its models
  async pickAgent() {
    const code = !!this.where();  // (a code or computer task: only providers that work in folders)
    const providers = (await this.relay.providers()).filter((p) => p.enabled && (!code || p.tools));
    const current = this.convo?.provider_id || this.pick.provider;
    const provider = await vscode.window.showQuickPick(providers.map((p) => ({
      label: p.name || p.id, description: p.id === current ? "now" : p.id, id: p.id })),
    { title: "Relay: the agent", placeHolder: "Which of Relay's providers" });
    if (!provider) return;
    let models;
    try {
      models = await this.client().get(`/api/providers/${encodeURIComponent(provider.id)}/models`);
      models = models.models || [];
    } catch {
      models = [];
    }
    if (!models.some((m) => !m.id)) models.unshift({ id: "", name: "Default model" });
    const model = await vscode.window.showQuickPick(models.map((m) => ({
      label: m.name || m.id || "Default model", description: m.id || "", id: m.id || "" })),
    { title: `Relay: ${provider.label}'s model`, placeHolder: "Which model" });
    if (!model) return;
    if (this.id) {
      const change = provider.id === this.convo?.provider_id ? { model: model.id }
        : { provider_id: provider.id, model: model.id };
      await this.client().patch(`/api/conversations/${encodeURIComponent(this.id)}`, change);
      this.commandsCache = null;
      this.efforts = null;
      await this.agentCommands();
      await this.refresh();
    } else {
      this.pick = { ...this.pick, provider: provider.id, model: model.id || null };
      // (as Relay's page does: the next new chats start with it too, in every browser)
      this.client().put("/api/new-chat", { provider_id: provider.id, model: model.id || null }).catch(() => {});
      this.post({ type: "conversation", info: this.info() });
    }
  }

  async pickMode() {
    const modes = (await this.relay.modes()).filter((m) => m.id !== "governor");
    const current = (this.convo ? this.convo.agent_mode : this.pick.mode) || "build";
    const picked = await vscode.window.showQuickPick(modes.map((m) => ({
      label: m.name || m.id, description: m.id === current ? "now" : "", detail: m.description || "", id: m.id })),
    { title: "Relay: the mode", matchOnDetail: true });
    if (picked) await this.setMode(picked.id);
  }

  async setMode(mode) {
    if (this.id) {
      await this.client().patch(`/api/conversations/${encodeURIComponent(this.id)}`, { agent_mode: mode });
      await this.refresh();
    } else {
      this.pick = { ...this.pick, mode };
      this.post({ type: "conversation", info: this.info() });
    }
  }

  async refresh() {
    if (!this.id) return;
    this.convo = await this.client().get(`/api/conversations/${encodeURIComponent(this.id)}`);
    this.post({ type: "conversation", info: this.info() });
  }

  async attach() {
    const picked = await vscode.window.showOpenDialog({ canSelectMany: true, openLabel: "Attach",
      defaultUri: this.where() ? vscode.Uri.file(this.where()) : undefined });
    for (const uri of picked || []) {
      const name = path.basename(uri.path);
      const bytes = await vscode.workspace.fs.readFile(uri);
      const made = await this.client().upload(name, bytes, TYPES[path.extname(name).toLowerCase()]);
      this.attachments.push({ id: made.id, name: made.name || name });
    }
    this.post({ type: "attachments", items: this.attachments });
  }

  // Text for the message being written (Ask Relay about this, Add to Relay's chat)
  insert(text) {
    this.panel.reveal();
    this.post({ type: "insert", text });
  }

  // Where a file is, as the message should name it: relative to the task's folder when it's in there
  place(file) {
    const folder = this.where();
    if (folder && inside(file, folder)) return path.relative(folder, file).replace(/\\/g, "/") || ".";
    return file;
  }

  // A file named in the chat, its whole path: ~ is the home folder, the rest is the task's folder's
  fileAt(file) {
    const home = file === "~" || /^~[\\/]/.test(file) ? os.homedir() + file.slice(1) : file;
    const folder = this.where();
    return path.isAbsolute(home) || !folder ? home : path.join(folder, home);
  }

  // A file in an editor beside the chat (at a line, from 1)
  async openFile(file, line = null, { quietly = false } = {}) {
    if (!file) return null;
    const column = this.panel.viewColumn === vscode.ViewColumn.One ? vscode.ViewColumn.Beside : vscode.ViewColumn.One;
    const place = this.fileAt(file);
    if (!fs.existsSync(place)) {
      if (!quietly) vscode.window.showWarningMessage(`Relay: there's no file ${place}.`);
      return null;
    }
    const at = Number(line) > 0 ? new vscode.Position(Number(line) - 1, 0) : null;
    return vscode.window.showTextDocument(vscode.Uri.file(place), { viewColumn: column, preview: true,
      preserveFocus: quietly, selection: at ? new vscode.Range(at, at) : undefined });
  }

  // Following the agent (relay.follow): a file it changed, beside the chat while the chat's in sight, at the
  // (first) line it added
  async follow_(file, added = null) {
    if (!this.panel.visible || vscode.workspace.getConfiguration("relay").get("follow") === false) return;
    const editor = await this.openFile(file, null, { quietly: true }).catch(() => null);
    if (!editor || !added?.trim()) return;
    const text = editor.document.getText();
    const at = text.indexOf(added);
    if (at < 0) return;
    const position = editor.document.positionAt(at);
    editor.revealRange(new vscode.Range(position, position), vscode.TextEditorRevealType.InCenterIfOutsideViewport);
  }

  // A link from an answer: a site in the browser (a server on this machine through VSCodium's port forwarding), a
  // file in an editor (at its line: path:12, path#L12)
  async link(href) {
    if (/^https?:\/\//i.test(href)) {
      await vscode.env.openExternal(await vscode.env.asExternalUri(vscode.Uri.parse(href)));
    } else if (!/^[a-z][a-z0-9+.-]*:/i.test(href) || /^file:/i.test(href) || /^[a-z]:[\\/]/i.test(href)) {
      const plain = /^file:/i.test(href) ? vscode.Uri.parse(href.split("#")[0]).fsPath : decodeURI(href.split("#")[0]);
      const line = /#L(\d+)/.exec(href)?.[1] || /:(\d+)(?::\d+)?$/.exec(plain)?.[1];
      if (plain) await this.openFile(line && !/#L/.test(href) ? plain.replace(/:\d+(:\d+)?$/, "") : plain, line);
    }
  }
}

function nonce() {
  return require("crypto").randomBytes(16).toString("base64").replace(/[^A-Za-z0-9]/g, "");
}

function page(webview, media) {
  const once = nonce();
  const css = webview.asWebviewUri(vscode.Uri.joinPath(media, "chat.css"));
  const js = webview.asWebviewUri(vscode.Uri.joinPath(media, "chat.js"));
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${webview.cspSource}; \
script-src 'nonce-${once}'; img-src ${webview.cspSource} https: data:; font-src ${webview.cspSource};">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="${css}">
<title>Relay</title></head>
<body><div id="app"></div><script nonce="${once}" src="${js}"></script></body></html>`;
}

module.exports = { ChatPanel, Chats, SideChat };
