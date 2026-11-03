// Relay in VSCodium: its sign-in, the Conversations view, the chats, review in Source Control, and this computer
// as one of Relay's, with the commands that tie them to the editor. It runs where VSCodium's workspace is
// (extensionKind "workspace"): with Remote - SSH to Relay's machine, localhost is Relay, and a folder's path is
// Relay's; on a computer of its own, a folder there is a computer task.
"use strict";

const path = require("path");
const vscode = require("vscode");
const { Client, newer } = require("./lib/client");
const { Chats } = require("./chat");
const { Conversations, folders } = require("./conversations");
const { Review } = require("./review");
const { Computer } = require("./computer");
const { Tools } = require("./tools");
const { Inline } = require("./inline");
const { InlineEdit } = require("./inlineEdit");
const { Permissions } = require("./permissions");

const SECRET = "relay.appPassword";
const CATALOG = 300000;  // ms the providers' and modes' lists are kept

class Relay {
  constructor(context) {
    this.context = context;
    this.version = context.extension.packageJSON.version;
    this.client = null;
    this.user = null;
    this.catalog = { providers: null, modes: null, at: 0 };
    this.limits = null;
    this.onChange = () => {};
    this.onLimits = () => {};
  }

  url() {
    return vscode.workspace.getConfiguration("relay").get("url") || "http://localhost:8420";
  }

  // Signed in with the app password kept (VSCodium's secret storage), when there is one and Relay knows it
  async connect() {
    const password = await this.context.secrets.get(SECRET);
    this.client = null;
    this.user = null;
    if (password) {
      try {
        const client = new Client(this.url(), password, { version: this.version });
        this.user = await signedIn(client);
        this.client = client;
        await this.lists(true);
      } catch (error) {
        vscode.window.showWarningMessage(`Relay: ${error.message}`, "Sign in").then((picked) => {
          if (picked) vscode.commands.executeCommand("relay.signIn");
        });
      }
    }
    await vscode.commands.executeCommand("setContext", "relay.signedIn", !!this.client);
    this.onChange();
  }

  // Relay no longer takes the app password (revoked): said once, signed out
  lost(error) {
    if (!this.client) return;
    this.client = null;
    vscode.commands.executeCommand("setContext", "relay.signedIn", false);
    vscode.window.showWarningMessage(`Relay: ${error.message}. Sign in again with an app password.`, "Sign in")
      .then((picked) => picked && vscode.commands.executeCommand("relay.signIn"));
    this.onChange();
  }

  async lists(fresh = false) {
    if (!this.client || (!fresh && Date.now() - this.catalog.at < CATALOG && this.catalog.providers)) return;
    const [providers, modes] = await Promise.all([
      this.client.get("/api/providers").then((r) => r.providers).catch(() => this.catalog.providers || []),
      this.client.get("/api/modes").catch(() => this.catalog.modes || [])]);
    this.catalog = { providers, modes, at: Date.now() };
  }

  async providers() {
    await this.lists();
    return this.catalog.providers || [];
  }

  async modes() {
    await this.lists();
    return this.catalog.modes || [];
  }

  // Each provider's usage-limit windows (Relay's Settings → Usage), looked at every few minutes
  async lookAtLimits() {
    if (!this.client) return;
    try {
      this.limits = await this.client.get("/api/usage/limits");
    } catch {
      this.limits = this.limits || [];
    }
    this.onLimits();
  }

  // The fullest window of a provider's limits ({label, used}), or null; a provider that shares another's account
  // (Relay's `also`) has that one's
  limitOf(provider) {
    if (!provider || !this.limits) return null;
    const name = (this.catalog.providers || []).find((p) => p.id === provider)?.name;
    const found = this.limits.find((l) => l.provider_id === provider) || this.limits.find((l) => name && (l.also || [])
      .some((also) => also === name || also.startsWith(`${name} ·`)));
    const windows = (found?.windows || []).filter((w) => typeof w.used === "number");
    if (!windows.length) return null;
    const fullest = windows.reduce((a, b) => (b.used > a.used ? b : a));
    return { label: fullest.label, used: fullest.used };
  }

  agentName(provider, model) {
    if (!provider) return "";
    const found = (this.catalog.providers || []).find((p) => p.id === provider);
    return `${found?.name || provider}${model ? ` · ${model}` : ""}`;
  }

  modeName(mode) {
    const found = (this.catalog.modes || []).find((m) => m.id === (mode || "build"));
    return found?.name || (mode ? mode : "Build");
  }

  // Relay's page for a conversation, where the browser reaches it: Relay's own address (Settings → General), else
  // this one through VSCodium (with Remote - SSH it forwards the port)
  async page(id) {
    let base = null;
    try {
      base = (await this.client.get("/api/settings")).notifications?.public_url || null;
    } catch { /* not known: VSCodium's way */ }
    const address = `${(base || this.client.url).replace(/\/+$/, "")}/c/${encodeURIComponent(id)}`;
    return base ? vscode.Uri.parse(address) : vscode.env.asExternalUri(vscode.Uri.parse(address));
  }
}

// Who the app password signs in as; refused in Relay's words
async function signedIn(client) {
  const state = await client.get("/api/state");
  if (state.login_required && !state.user) {
    const error = new Error("Relay doesn't know that app password (revoked? Relay → Settings → Account)");
    error.status = 401;
    throw error;
  }
  return state.user || { username: "(no login)" };
}

function activate(context) {
  const relay = new Relay(context);
  const computer = new Computer(context, relay);
  const chats = new Chats(context, relay, computer);
  const conversations = new Conversations(relay, chats);
  const review = new Review(context, relay, chats);
  chats.review = review;
  const inline = new Inline(context, review);  // (Keep/Undo in the editor, and inline edits' Accept/Reject)
  review.onRefreshed = () => inline.refreshed();
  const inlineEdit = new InlineEdit(relay, inline);
  computer.before = (job) => review.beforeChange(job);  // (a computer task's checkpoint before its turn's first change)
  const permissions = new Permissions(context, relay, chats, review);  // (VSCodium asks before an agent's change here)
  chats.permissions = permissions;
  computer.allow = (job) => permissions.allow(job);
  computer.after = (job, result) => {  // (following the agent: a file it wrote here, beside its task's chat)
    if (job.kind === "write" && result.ok) chats.followed(job.path, review.tasksAt(job));
  };
  const tools = new Tools(relay, chats);  // (agents use VSCodium: its language servers answer the plugin's tools)
  context.subscriptions.push(conversations, review, computer, tools);

  let looked = false;  // (for a newer extension: once a session, when signed in)
  relay.onChange = () => {
    conversations.refresh();
    review.refresh();
    chats.reload();
    if (relay.client) {
      computer.start().then(() => computer.offer()).catch(() => {});  // (this computer in Relay, when it's on)
      tools.start();
    } else {
      computer.stop();
      tools.stop();
    }
    relay.lookAtLimits();
    if (relay.client && !looked) {
      looked = true;
      update(false);
    }
  };
  context.subscriptions.push(vscode.window.registerWebviewViewProvider("relay.chat", chats.side,
    { webviewOptions: { retainContextWhenHidden: true } }));
  // (a chat's tab VSCodium kept through a reload of the window: its conversation in it again)
  context.subscriptions.push(vscode.window.registerWebviewPanelSerializer("relay.chat", {
    deserializeWebviewPanel: async (panel, state) => { chats.restore(panel, state); } }));
  relay.onLimits = () => {
    conversations.statusBar();
    for (const panel of chats.panels) panel.post({ type: "conversation", info: panel.info() });
  };
  const limits = setInterval(() => relay.lookAtLimits(), 180000);
  context.subscriptions.push({ dispose: () => clearInterval(limits) });

  // Fix with Relay: a quick fix on each problem VSCodium shows
  context.subscriptions.push(vscode.languages.registerCodeActionsProvider({ scheme: "file" }, {
    provideCodeActions(document, range, found) {
      if (!relay.client) return [];
      return found.diagnostics.filter((d) => d.severity <= vscode.DiagnosticSeverity.Warning).slice(0, 3).map((d) => {
        const action = new vscode.CodeAction(`Fix with Relay: ${d.message.split("\n")[0].slice(0, 60)}`,
          vscode.CodeActionKind.QuickFix);
        action.command = { command: "relay.fix", title: "Fix with Relay", arguments: [document.uri, d] };
        action.diagnostics = [d];
        return action;
      });
    },
  }, { providedCodeActionKinds: [vscode.CodeActionKind.QuickFix] }));

  chats.onFocus = (panel) => review.show(panel);
  // (no chat shown yet: Source Control has the changes of the open folder's newest task)
  conversations.onLoaded = (mine) => {
    if (review.source) return;
    const task = mine.find((c) => c.mode === "code" || chats.local(c.id));
    if (task) review.show({ convo: task });
  };
  chats.onChanged = (panel) => {
    conversations.refresh();
    review.show(panel);
    tools.update();  // (a new computer task: this window has its folder)
  };

  let quiet = false;

  async function signIn(given = {}) {
    const url = given.url ?? await vscode.window.showInputBox({ title: "Relay: sign in (1/2)", value: relay.url(),
      prompt:
        "Relay's address, as this machine reaches it (with Remote - SSH to Relay's machine: http://localhost:<port>)",
      ignoreFocusOut: true });
    if (!url) return;
    const password = given.password ?? await vscode.window.showInputBox({ title: "Relay: sign in (2/2)", password: true,
      prompt: "An app password: Relay → Settings → VSCodium → Make one",
      placeHolder: "relay_…", ignoreFocusOut: true });
    if (!password) return;
    let user;
    try {
      user = await signedIn(new Client(url.trim(), password.trim(), { version: relay.version }));
    } catch (error) {
      vscode.window.showErrorMessage(`Relay: ${error.message}`);
      return;
    }
    await context.secrets.store(SECRET, password.trim());
    if (url.trim() !== relay.url()) {
      quiet = true;  // (its change of address is this sign-in's: connected once, below)
      try {
        await vscode.workspace.getConfiguration("relay").update("url", url.trim(), vscode.ConfigurationTarget.Global);
      } finally {
        quiet = false;
      }
    }
    await relay.connect();
    vscode.window.showInformationMessage(`Relay: signed in as ${user.username}.`);
  }

  // The conversation a command is about: a tree item's, a chat's ({id}), or the chat focused last
  const which = (given) => given?.id ? String(given.id) : chats.last?.id || null;

  // A new conversation's folder: the open folder (one of them, asked when there are more)
  async function folder() {
    const open = folders();
    if (open.length <= 1) return open[0] || null;
    const picked = await vscode.window.showQuickPick(open.map((f) => ({ label: path.basename(f), description: f, f })),
      { title: "Relay: a code task in which folder?" });
    return picked ? picked.f : undefined;
  }

  // Text about a file for a chat: the chat focused last when it's about the file's folder, else a new code task there
  function chatAbout(uri) {
    const open = uri && vscode.workspace.getWorkspaceFolder(uri);
    return chats.for(open?.uri.scheme === "file" ? open.uri.fsPath : folders()[0] || null);
  }

  const command = (name, run) => context.subscriptions.push(vscode.commands.registerCommand(name, async (...args) => {
    try {
      await run(...args);
    } catch (error) {
      vscode.window.showErrorMessage(`Relay: ${error.message}`);
    }
  }));
  const needed = () => {
    if (!relay.client) throw new Error("Not signed in: Relay: Sign in");
    return relay.client;
  };

  command("relay.signIn", () => signIn());
  command("relay.signOut", async () => {
    await context.secrets.delete(SECRET);
    await relay.connect();
  });
  command("relay.refresh", () => conversations.refresh());
  command("relay.search", () => {
    needed();
    return conversations.search();
  });
  command("relay.newTask", async () => {
    needed();
    const chosen = await folder();
    if (chosen === undefined) return;
    if (chosen === null) vscode.window.showInformationMessage("Relay: no folder is open, so this is a chat.");
    chats.draft(chosen);
  });
  command("relay.newChat", () => {
    needed();
    chats.draft(null);
  });
  command("relay.open", (given) => {
    needed();
    const id = which(given);
    if (id) chats.open(id);
  });
  command("relay.openInRelay", async (given) => {
    const id = which(given);
    if (id) await vscode.env.openExternal(await relay.page(id));
  });
  command("relay.rename", async (given) => {
    const id = which(given);
    const found = conversations.find(id);
    const title = await vscode.window.showInputBox({ title: "Relay: rename", value: found?.title || "" });
    if (!title?.trim()) return;
    await needed().patch(`/api/conversations/${encodeURIComponent(id)}`, { title: title.trim() });
    conversations.refresh();
    chats.find(id)?.refresh();
  });
  command("relay.stop", async (given) => {
    const id = which(given);
    const found = await needed().get(`/api/conversations/${encodeURIComponent(id)}`);
    if (found.running_run) await needed().post(`/api/runs/${encodeURIComponent(found.running_run)}/stop`);
    conversations.refresh();
  });
  command("relay.delete", async (given) => {
    const id = which(given);
    const found = conversations.find(id);
    const sure = await vscode.window.showWarningMessage(`Delete “${found?.title || "this conversation"}” in Relay?`,
      { modal: true, detail: "Its messages go. A code task's files and changes stay." }, "Delete");
    if (sure !== "Delete") return;
    await needed().del(`/api/conversations/${encodeURIComponent(id)}`);
    chats.find(id)?.panel.dispose();
    conversations.refresh();
  });
  command("relay.askAboutSelection", async () => {
    needed();
    const editor = vscode.window.activeTextEditor;
    if (!editor) return;
    const chat = await chatAbout(editor.document.uri);
    const file = editor.document.uri.scheme === "file" ? chat.place(editor.document.uri.fsPath)
      : editor.document.uri.toString();
    const selection = editor.selection;
    if (selection.isEmpty) {
      chat.insert(`${file} `);
      return;
    }
    // (a selection that ends at a line's start ends on the line before it)
    const last = selection.end.line
      - (selection.end.character === 0 && selection.end.line > selection.start.line ? 1 : 0);
    const lines = selection.start.line === last ? `${selection.start.line + 1}`
      : `${selection.start.line + 1}-${last + 1}`;
    const code = editor.document.getText(selection).replace(/\r?\n$/, "");
    const fence = code.includes("```") ? "````" : "```";
    chat.insert(`${file}:${lines}\n${fence}${editor.document.languageId}\n${code}\n${fence}\n`);
  });
  command("relay.inlineEdit", () => {
    needed();
    return inlineEdit.run();
  });
  command("relay.addFile", async (uri, uris) => {
    needed();
    const all = (uris && uris.length ? uris : [uri || vscode.window.activeTextEditor?.document.uri]).filter(Boolean);
    if (!all.length) return;
    const chat = await chatAbout(all[0]);
    chat.insert(all.map((u) => (u.scheme === "file" ? chat.place(u.fsPath) : u.toString())).join(" ") + " ");
  });
  command("relay.update", () => update(true));
  command("relay.fix", async (uri, problem) => {
    needed();
    const document = await vscode.workspace.openTextDocument(uri);
    const chat = await chatAbout(uri);
    const line = problem.range.start.line;
    const from = Math.max(0, line - 3);
    const to = Math.min(document.lineCount - 1, problem.range.end.line + 3);
    const code = document.getText(new vscode.Range(from, 0, to, document.lineAt(to).text.length));
    const source = problem.source
      ? ` (${problem.source}${problem.code ? ` ${problem.code.value ?? problem.code}` : ""})` : "";
    const fence = code.includes("```") ? "````" : "```";
    chat.submit(`Fix this problem in ${chat.place(uri.fsPath)}:${line + 1}: ${problem.message}${source}\n\n`
      + `${fence}${document.languageId}\n${code}\n${fence}\n(lines ${from + 1}-${to + 1})`);
  });
  command("relay.addTerminal", async () => {
    needed();
    // (the terminal's selection, through the clipboard: what was on it is put back)
    const kept = await vscode.env.clipboard.readText();
    await vscode.commands.executeCommand("workbench.action.terminal.copySelection");
    const text = (await vscode.env.clipboard.readText()).replace(/\s+$/, "");
    await vscode.env.clipboard.writeText(kept);
    if (!text || text === kept) {
      vscode.window.showInformationMessage("Relay: select some of the terminal's output first.");
      return;
    }
    const chat = await chatAbout(null);
    chat.insert(`From the terminal:\n\`\`\`text\n${text}\n\`\`\`\n`);
  });
  command("relay.openSide", (given) => {
    needed();
    const id = which(given);
    return id ? chats.open(id, { side: true }) : chats.draft(folders()[0] || null, { side: true });
  });
  command("relay.computer", () => {
    needed();
    return computer.manage();
  });
  command("relay.review.refresh", () => review.refresh());
  command("relay.review.open", (file) => review.open(file));
  for (const what of ["reviewed", "commit", "merge", "discard", "undo", "revert"]) {
    command(`relay.review.${what}`, async (state) => {
      // (a file's line in Source Control: its change)
      const file = state?.command?.arguments?.[0];
      await review.act(what, file);
      const panel = chats.find(review.source?.id);
      if (panel) await panel.refresh();
      conversations.refresh();
    });
  }

  // A newer version of the extension in Relay (its VSCodium plugin): offered, and installed from there
  async function update(asked) {
    if (!relay.client) return;
    let found;
    try {
      found = await relay.client.get("/api/plugins/vscodium/version");
    } catch (error) {
      if (asked) vscode.window.showErrorMessage(`Relay: ${error.message}`);
      return;
    }
    if (!newer(found.version, relay.version)) {
      if (asked) vscode.window.showInformationMessage(`Relay: this is the newest extension (${relay.version}).`);
      return;
    }
    if (!asked) {
      const picked = await vscode.window.showInformationMessage(
        `Relay has a newer version of this extension: ${found.version} (this is ${relay.version}).`, "Update");
      if (picked !== "Update") return;
    }
    const got = await relay.client.get("/api/plugins/vscodium/extension");
    const file = vscode.Uri.joinPath(context.globalStorageUri, got.name);
    await vscode.workspace.fs.createDirectory(context.globalStorageUri);
    await vscode.workspace.fs.writeFile(file, Buffer.from(got.data, "base64"));
    await vscode.commands.executeCommand("workbench.extensions.installExtension", file);
    const reload = await vscode.window.showInformationMessage(`Relay: version ${got.version} is installed. `
      + "It's used after a reload of the window.", "Reload");
    if (reload) vscode.commands.executeCommand("workbench.action.reloadWindow");
  }

  // vscodium://relay.relay/sign-in?url=…&password=… (Relay's Settings → VSCodium): asked first, since any page could
  // send such a link
  context.subscriptions.push(vscode.window.registerUriHandler({ handleUri: async (uri) => {
    if (uri.path !== "/sign-in") return;
    const query = new URLSearchParams(uri.query);
    const url = query.get("url") || relay.url();
    const sure = await vscode.window.showInformationMessage(`Sign in to Relay at ${url}?`,
      { modal: true, detail: "With the app password Relay's Settings → VSCodium made." }, "Sign in");
    if (sure === "Sign in") await signIn({ url, password: query.get("password") || undefined });
  } }));

  context.subscriptions.push(vscode.workspace.onDidChangeConfiguration((event) => {
    if (event.affectsConfiguration("relay.url") && !quiet) relay.connect();
  }));

  relay.connect();
}

function deactivate() {}

module.exports = { activate, deactivate };
