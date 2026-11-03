// The Conversations view: Relay's chats and code tasks, this folder's first, each with how it's doing (an agent at
// work, a question for you). The status bar counts them, and a question while its chat isn't in sight is said.
"use strict";

const vscode = require("vscode");
const { near } = require("./lib/places");

const NEAR = 5000;  // ms between looks while the view or a chat is in sight and the window has focus
const FAR = 30000;  // ms otherwise

function ago(seconds) {
  const passed = Math.max(0, Date.now() / 1000 - seconds);
  if (passed < 60) return "now";
  if (passed < 3600) return `${Math.floor(passed / 60)} min`;
  if (passed < 86400) return `${Math.floor(passed / 3600)} h`;
  return `${Math.floor(passed / 86400)} d`;
}

// Whether a conversation is about one of the open folders: it works in it (or in a folder around it), or it's a
// task on its own branch of the repository the folder is in
function here(conversation, folders, local = () => null) {
  const places = [conversation.mode === "code" ? conversation.directory : null,
    conversation.git?.mode === "worktree" ? conversation.git.repo_root : null,
    local(conversation.id)?.folder].filter(Boolean);
  return places.some((place) => folders.some((folder) => near(place, folder)));
}

function folders() {
  return (vscode.workspace.workspaceFolders || []).filter((f) => f.uri.scheme === "file").map((f) => f.uri.fsPath);
}

class Conversations {
  constructor(relay, chats) {
    this.relay = relay;
    this.chats = chats;
    this.items = [];
    this.loaded = false;
    this.asking = new Set();  // (those said to ask you already)
    this.onLoaded = () => {};  // (the open folder's conversations, newest first, each look)
    this.changed = new vscode.EventEmitter();
    this.onDidChangeTreeData = this.changed.event;
    this.view = vscode.window.createTreeView("relay.conversations", { treeDataProvider: this });
    this.status = vscode.window.createStatusBarItem("relay.status", vscode.StatusBarAlignment.Left, 50);
    this.status.name = "Relay";
    this.status.show();
    this.view.onDidChangeVisibility(() => this.view.visible && this.refresh());
    vscode.window.onDidChangeWindowState((state) => state.focused && this.refresh());
    vscode.workspace.onDidChangeWorkspaceFolders(() => this.changed.fire());
  }

  dispose() {
    clearTimeout(this.timer);
    this.view.dispose();
    this.status.dispose();
  }

  schedule() {
    clearTimeout(this.timer);
    const near = (this.view.visible || this.chats.panels.size) && vscode.window.state.focused;
    this.timer = setTimeout(() => this.refresh(), near ? NEAR : FAR);
  }

  async refresh() {
    clearTimeout(this.timer);
    if (!this.relay.client) {
      this.items = [];
      this.loaded = false;
      this.changed.fire();
      this.statusBar();
      return;
    }
    try {
      this.items = await this.relay.client.get("/api/conversations");
      this.view.message = undefined;
      this.questions();
      this.chats.seen(this.items);
      this.onLoaded(this.items.filter((c) => here(c, folders(), (id) => this.chats.local(id))));
      this.loaded = true;
    } catch (error) {
      if (error.status === 401) {
        this.relay.lost(error);
        return;
      }
      this.view.message = `Relay: ${error.message}`;
    }
    this.changed.fire();
    this.statusBar();
    this.schedule();
  }

  // A question for you in a conversation whose chat isn't open: said once (not those of before the first look; an
  // open chat says its own)
  questions() {
    const now = new Set(this.items.filter((c) => c.needs_approval).map((c) => c.id));
    for (const c of this.items) {
      if (!now.has(c.id) || this.asking.has(c.id) || !this.loaded || this.chats.find(c.id)) continue;
      vscode.window.showInformationMessage(`Relay: “${c.title}” asks you something.`, "Open").then((picked) => {
        if (picked) vscode.commands.executeCommand("relay.open", { id: c.id });
      });
    }
    this.asking = now;
  }

  statusBar() {
    if (!this.relay.client) {
      Object.assign(this.status, { text: "$(account) Relay", tooltip: "Sign in to Relay", command: "relay.signIn" });
      return;
    }
    const working = this.items.filter((c) => c.running_run).length;
    const asking = this.items.filter((c) => c.needs_approval);
    const parts = [working ? `${working} working` : "", asking.length ? `${asking.length} asks` : ""].filter(Boolean);
    this.status.text = `${asking.length ? "$(bell-dot)" : working ? "$(loading~spin)" : "$(comment-discussion)"} Relay`
      + (parts.length ? `: ${parts.join(" · ")}` : "");
    const tip = new vscode.MarkdownString();
    tip.appendText(asking.length ? `Asks you: ${asking.map((c) => c.title).join(", ")}` : "Relay's conversations");
    for (const limit of this.relay.limits || []) {
      const windows = (limit.windows || []).filter((w) => typeof w.used === "number");
      if (windows.length) {
        tip.appendMarkdown("\n\n");
        tip.appendText(`${limit.name}: ${windows.map((w) => `${w.label} ${Math.round(w.used * 100)}%`).join(", ")}`);
      }
    }
    this.status.tooltip = tip;
    this.status.command = asking.length ? { command: "relay.open", title: "Open", arguments: [{ id: asking[0].id }] }
      : "relay.conversations.focus";
  }

  getChildren(element) {
    if (!this.relay.client || !this.loaded) return [];
    if (element) return element.members.map((c) => this.item(c));
    const open = folders();
    const mine = this.items.filter((c) => here(c, open, (id) => this.chats.local(id)));
    if (!mine.length) return this.items.map((c) => this.item(c));
    const rest = this.items.filter((c) => !mine.includes(c));
    const group = (id, label, members, state) => Object.assign(new vscode.TreeItem(label, state), { id, members,
      contextValue: "group", description: String(members.length) });
    return [group("group:here", "In this folder", mine, vscode.TreeItemCollapsibleState.Expanded),
      ...(rest.length
          ? [group("group:rest", "Other conversations", rest, vscode.TreeItemCollapsibleState.Collapsed)] : [])];
  }

  getTreeItem(element) {
    return element;
  }

  item(c) {
    const item = new vscode.TreeItem(c.title || "New chat", vscode.TreeItemCollapsibleState.None);
    item.id = c.id;
    item.description = `${this.relay.agentName(c.provider_id, c.model)} · ${ago(c.updated)}`;
    const tip = new vscode.MarkdownString();
    tip.appendText(c.title || "New chat");
    tip.appendMarkdown("\n\n");
    const local = this.chats.local(c.id);
    tip.appendText(c.mode === "code" ? `A code task in ${c.directory}` : local
      ? `A computer task in ${local.folder} on ${local.computer}` : "A chat");
    if (c.git?.branch) tip.appendText(` (on ${c.git.branch})`);
    tip.appendMarkdown("\n\n");
    tip.appendText(`${this.relay.agentName(c.provider_id, c.model)} · ${this.relay.modeName(c.agent_mode)}`);
    if (c.needs_approval) tip.appendMarkdown("\n\n**Asks you something**");
    else if (c.running_run) tip.appendMarkdown("\n\n_Working…_");
    item.tooltip = tip;
    item.iconPath = c.needs_approval ? new vscode.ThemeIcon("bell-dot", new vscode.ThemeColor("charts.orange"))
      : c.running_run ? new vscode.ThemeIcon("loading~spin")
        : new vscode.ThemeIcon(c.mode === "code" ? "code" : this.chats.local(c.id) ? "device-desktop" : "comment",
          c.pinned ? new vscode.ThemeColor("charts.blue") : undefined);
    item.contextValue = `conversation.${c.mode}${c.running_run ? ".running" : ""}`;
    item.command = { command: "relay.open", title: "Open", arguments: [{ id: c.id }] };
    return item;
  }

  find(id) {
    return this.items.find((c) => c.id === id) || null;
  }

  // Find one by its title or its messages (Relay's own search), and open it
  async search() {
    const pick = vscode.window.createQuickPick();
    pick.placeholder = "Find a conversation: its title or what was said in it";
    pick.matchOnDescription = true;
    const show = (found) => {
      pick.items = found.map((c) => ({ label: c.title || "New chat", id: c.id,
        description: `${this.relay.agentName(c.provider_id, c.model)} · ${ago(c.updated)}`,
        detail: c.mode === "code" ? c.directory : undefined }));
    };
    show(this.items);
    let timer;
    pick.onDidChangeValue((value) => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        pick.busy = true;
        try {
          show(await this.relay.client.get(
            `/api/conversations${value.trim() ? `?q=${encodeURIComponent(value.trim())}` : ""}`));
        } catch (error) {
          pick.items = [{ label: error.message }];
        }
        pick.busy = false;
      }, 250);
    });
    pick.onDidAccept(() => {
      const chosen = pick.selectedItems[0];
      if (chosen?.id) vscode.commands.executeCommand("relay.open", { id: chosen.id });
      pick.hide();
    });
    pick.onDidHide(() => pick.dispose());
    pick.show();
  }
}

module.exports = { Conversations, ago, folders, here };
