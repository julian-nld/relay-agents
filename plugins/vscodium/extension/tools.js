// Agents use VSCodium (the plugin's vscodium_* tools): each question answered by VSCodium's own language servers, in
// the folder it's about (a computer task's folder here, else the conversation's own when this window has it open).
"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const vscode = require("vscode");
const { ToolWindow, column, definitionText, placesText, problemsText, symbolsText } = require("./lib/tools");
const { folders } = require("./conversations");
const { inside, near } = require("./lib/places");

const TRIES = 3;  // times a language server is asked while it answers nothing (one that's starting answers nothing yet)
const SETTLE = 4000;  // ms a file in sight may take for its first problems

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// VSCodium's problems in a folder (or in one file), as plain objects: {path, line, col, error, message, source, code}
function diagnostics(folder, place, only = null) {
  const found = [];
  const all = only ? [[only, vscode.languages.getDiagnostics(only)]] : vscode.languages.getDiagnostics();
  for (const [uri, list] of all) {
    if (uri.scheme !== "file" || (folder && !only && !inside(uri.fsPath, folder))) continue;
    for (const d of list) {
      if (d.severity > vscode.DiagnosticSeverity.Warning) continue;
      found.push({ path: place(uri.fsPath), line: d.range.start.line + 1, col: d.range.start.character + 1,
        error: d.severity === vscode.DiagnosticSeverity.Error, message: d.message, source: d.source,
        code: d.code?.value ?? d.code });
    }
  }
  return found;
}

async function asked(command, ...args) {
  let found = [];
  for (let attempt = 0; attempt < TRIES; attempt++) {
    found = (await vscode.commands.executeCommand(command, ...args)) || [];
    if (found.length) return found;
    await sleep(1000 * (attempt + 1));
  }
  return found;
}

// The tab a file has in one of the editor's groups, if it has one
function tabOf(uri) {
  const key = uri.toString();
  return vscode.window.tabGroups.all.flatMap((g) => g.tabs).find((t) => t.input?.uri?.toString() === key) || null;
}

class Tools {
  constructor(relay, chats) {
    this.relay = relay;
    this.chats = chats;
    this.window = null;
    this.watch = [vscode.window.onDidChangeWindowState(() => this.update()),
      vscode.workspace.onDidChangeWorkspaceFolders(() => this.update())];
  }

  start() {
    this.stop();
    if (!this.relay.client) return;
    this.window = new ToolWindow({ client: this.relay.client, report: () => this.report(),
      carry: (question) => this.carry(question) });
    this.window.start();
  }

  stop() {
    this.window?.stop();
    this.window = null;
  }

  update() {
    this.window?.update();
  }

  dispose() {
    this.stop();
    this.watch.forEach((w) => w.dispose());
  }

  // What this window has: its folders, the computer tasks in them, whether it's in front
  report() {
    const open = folders();
    const tasks = Object.entries(this.chats.locals()).filter(([, place]) => open.some((f) => near(place.folder, f)))
      .map(([id]) => id);
    return { folders: open, tasks, focused: vscode.window.state.focused };
  }

  // The folder a question is about
  folderOf(question) {
    const local = this.chats.local(question.conversation_id);
    if (local?.folder) return local.folder;
    const open = folders();
    const given = question.directory;
    if (given && open.some((f) => near(f, given)) && fs.existsSync(given)) return given;
    return open[0] || os.homedir();
  }

  async carry(question) {
    const folder = this.folderOf(question);
    const place = (file) => (inside(file, folder) ? path.relative(folder, file).replace(/\\/g, "/") || "." : file);
    const file = (given) => {
      const home = given === "~" || given.startsWith("~/") ? os.homedir() + given.slice(1) : given;
      return path.isAbsolute(home) ? home : path.join(folder, home);
    };
    const { args } = question;
    if (question.action === "problems") return this.problems(folder, place, args.path ? file(args.path) : null);
    if (question.action === "symbols" && !args.path) return this.across(args.query, place);
    const document = await this.open(file(args.path), args.path);
    if (question.action === "symbols") return this.outline(document, place);
    const position = this.at(document, args, place);
    if (question.action === "definition") return this.definition(document, position, place);
    if (question.action === "references") return this.references(document, position, place, args);
    if (question.action === "hover") return this.hover(document, position, args);
    throw new Error(`No tool “${question.action}” in this extension: update it (Relay: Update the extension)`);
  }

  async open(file, given) {
    try {
      return await vscode.workspace.openTextDocument(vscode.Uri.file(file));
    } catch {
      throw new Error(`No file ${given} (${file})`);
    }
  }

  // The name's place: its line, and on it `symbol` (or the line's first name)
  at(document, args, place) {
    const name = place(document.uri.fsPath);
    if (args.line > document.lineCount) throw new Error(`${name} has ${document.lineCount} lines, not ${args.line}`);
    const text = document.lineAt(args.line - 1).text;
    const col = column(text, args.symbol);
    if (col < 0) {
      throw new Error(args.symbol ? `“${args.symbol}” isn't on line ${args.line} of ${name}: ${text.trim()}`
        : `Line ${args.line} of ${name} has no name on it: give symbol, or another line`);
    }
    return new vscode.Position(args.line - 1, col);
  }

  async lines(uri, from, to) {
    const document = await vscode.workspace.openTextDocument(uri);
    const shown = [];
    for (let n = Math.max(0, from); n <= Math.min(document.lineCount - 1, to); n++) {
      shown.push([n + 1, document.lineAt(n).text]);
    }
    return shown;
  }

  async problems(folder, place, only) {
    let uri = null;
    if (only) {
      const document = await this.open(only, place(only));
      uri = document.uri;
      if (!vscode.window.visibleTextEditors.some((e) => e.document === document)) {
        // (a language server may only look at what's in sight, as TypeScript's does: shown a moment, without the
        // focus, in a tab of its own that goes again)
        const had = tabOf(uri);
        const settled = this.settle(uri);
        await vscode.window.showTextDocument(document, { preview: false, preserveFocus: true });
        await settled;
        const ours = !had && tabOf(uri);
        if (ours) await vscode.window.tabGroups.close(ours, true);
      } else if (!vscode.languages.getDiagnostics(uri).length) {
        await this.settle(uri);
      }
    }
    const found = diagnostics(folder, place, uri);
    return problemsText(found, { where: only ? ` in ${place(only)}` : " in the folder" })
      || (only ? `VSCodium sees no errors or warnings in ${place(only)}.`
        : "VSCodium sees no errors or warnings in the folder. (A language server may only look at the files in "
          + "sight, as TypeScript's does: give path to check one.)");
  }

  // Until the file's problems come (or a moment passed: a file without any may get no word)
  settle(uri) {
    return new Promise((resolve) => {
      const done = () => {
        clearTimeout(timer);
        watching.dispose();
        resolve();
      };
      const timer = setTimeout(done, SETTLE);
      const watching = vscode.languages.onDidChangeDiagnostics((event) => {
        if (event.uris.some((u) => u.toString() === uri.toString())) setTimeout(done, 200);
      });
    });
  }

  async definition(document, position, place) {
    let found = await asked("vscode.executeDefinitionProvider", document.uri, position);
    // (a name imported in this file: its definition is where it's imported from, one step on)
    const target = (link) => [link.targetUri || link.uri, link.targetSelectionRange || link.targetRange || link.range];
    if (found.length === 1 && target(found[0])[0].toString() === document.uri.toString()) {
      const [, range] = target(found[0]);
      if (/\b(require|import|from|use)\b/.test(document.lineAt(range.start.line).text)) {
        const on = await vscode.commands.executeCommand("vscode.executeDefinitionProvider", document.uri, range.start);
        if (on?.length && on.some((l) => target(l)[0].toString() !== document.uri.toString())) found = on;
      }
    }
    const shown = [];
    for (const link of found.slice(0, 5)) {
      const uri = link.targetUri || link.uri;
      const range = link.targetSelectionRange || link.targetRange || link.range;
      const whole = link.targetRange || link.range;
      const end = Math.min(whole.end.line, range.start.line + 12);
      shown.push({ path: place(uri.fsPath), line: range.start.line + 1, col: range.start.character + 1,
        lines: await this.lines(uri, range.start.line - 2, Math.max(end, range.start.line + 2)) });
    }
    return definitionText(shown) || "VSCodium's language server knows no definition for that.";
  }

  async references(document, position, place, args) {
    const found = await asked("vscode.executeReferenceProvider", document.uri, position);
    const shown = [];
    const texts = new Map();
    for (const reference of found.slice(0, 500)) {
      const key = reference.uri.toString();
      if (!texts.has(key)) texts.set(key, await vscode.workspace.openTextDocument(reference.uri));
      const line = texts.get(key).lineAt(reference.range.start.line).text;
      shown.push({ path: place(reference.uri.fsPath), line: reference.range.start.line + 1,
        col: reference.range.start.character + 1, text: line });
    }
    return placesText(shown, { what: "reference" })
      || `VSCodium's language server knows no references to ${args.symbol || "that"}.`;
  }

  async across(query, place) {
    const found = await asked("vscode.executeWorkspaceSymbolProvider", query);
    const shown = found.filter((s) => s.location?.uri?.scheme === "file").slice(0, 500).map((s) => ({
      name: s.name, kind: vscode.SymbolKind[s.kind]?.toLowerCase() || "symbol", container: s.containerName,
      path: place(s.location.uri.fsPath), line: s.location.range.start.line + 1 }));
    return symbolsText(shown) || `VSCodium's language servers know no names like “${query}” (one that hasn't `
      + "opened a file of the project yet knows none: give path to open one).";
  }

  async outline(document, place) {
    const found = await asked("vscode.executeDocumentSymbolProvider", document.uri);
    const shown = [];
    const walk = (list, depth) => {
      for (const s of list) {
        const range = s.selectionRange || s.range || s.location?.range;
        shown.push({ name: s.name, kind: vscode.SymbolKind[s.kind]?.toLowerCase() || "symbol",
          line: range.start.line + 1, depth });
        if (s.children?.length) walk(s.children, depth + 1);
      }
    };
    walk(found, 0);
    return symbolsText(shown, { outline: true, most: 400 })
      || `VSCodium's language server has no outline for ${place(document.uri.fsPath)}.`;
  }

  async hover(document, position, args) {
    const found = await asked("vscode.executeHoverProvider", document.uri, position);
    const said = found.flatMap((h) => h.contents).map((c) => (typeof c === "string" ? c
      : c.language ? `\`\`\`${c.language}\n${c.value}\n\`\`\`` : c.value)).filter((t) => t && t.trim());
    return said.join("\n\n---\n\n") || `VSCodium shows nothing for ${args.symbol || "that"} there.`;
  }
}

module.exports = { Tools, diagnostics };
