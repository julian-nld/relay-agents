// Agents use VSCodium (the plugin's vscodium_* tools): this window's questions, asked for while it's signed in and
// answered by `carry` (tools.js: VSCodium's language servers), and how those answers read for an agent. The window
// says which folders it has first, so the plugin knows which one has a conversation's folder. Nothing of VSCodium's
// here: node --test tries it.
"use strict";

const crypto = require("crypto");

const ROUTE = "/api/plugins/vscodium";
const POLL = 20;  // seconds the plugin may hold an ask open
const BUSY = 300;  // ms between asks while it answers one (an ask held open would keep one of the plugin's threads)

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

class ToolWindow {
  // `report()`: {folders, tasks, focused}; `carry(question)`: its answer's text (or it throws what went wrong)
  constructor({ client, report, carry, say = () => {} }) {
    Object.assign(this, { client, report, carry, say });
    this.id = crypto.randomUUID();
    this.version = 0;
    this.doing = new Set();
    this.told = false;
    this.stopped = false;
  }

  async tell() {
    await this.client.post(`${ROUTE}/windows`, { client: this.id, ...this.report() });
    this.told = true;
  }

  // The folders or the focus changed: said again (the next ask says it if this can't)
  update() {
    if (!this.stopped) this.tell().catch(() => { this.told = false; });
  }

  async start() {
    let wait = 2000;
    while (!this.stopped) {
      this.asking = new AbortController();
      try {
        if (!this.told) await this.tell();
        const query = `client=${encodeURIComponent(this.id)}&since=${this.version}&wait=${this.doing.size ? 0 : POLL}`;
        const found = await this.client.get(`${ROUTE}/tools?${query}`, { signal: this.asking.signal });
        this.version = found.version;
        for (const question of found.questions || []) {
          if (!this.doing.has(question.id)) this.answer(question);
        }
        wait = 2000;
        if (this.doing.size) await sleep(BUSY);
      } catch (error) {
        if (this.stopped) return;
        if (error.status === 404 && /Unknown window/.test(error.message)) {
          this.told = false;  // (Relay started again: it doesn't know this window yet)
          continue;
        }
        await sleep(wait);  // (Relay away, or its VSCodium plugin turned off)
        wait = Math.min(wait * 2, 60000);
      }
    }
  }

  stop() {
    this.stopped = true;
    this.asking?.abort();
  }

  async answer(question) {
    this.doing.add(question.id);
    let result;
    try {
      result = { ok: true, text: await this.carry(question) };
    } catch (error) {
      result = { ok: false, error: error.message || String(error) };
    }
    try {
      await this.client.post(`${ROUTE}/tools/${encodeURIComponent(question.id)}/result`,
        { client: this.id, ...result });
    } catch (error) {
      this.say(`Couldn't send VSCodium's answer to Relay: ${error.message}`);
    } finally {
      this.doing.delete(question.id);
    }
  }
}

// Words that start a line but aren't what it's about (its first name, for a tool without `symbol`)
const KEYWORDS = new Set(("abstract async await break case catch class const continue def default defer delete do elif "
  + "else enum export extends final fn for from func function go if impl implements import in interface is let loop "
  + "match mod module mut namespace new not or override package private protected pub public readonly return self "
  + "static struct super switch this throw trait try type typeof use val var void where while with yield").split(" "));

// Where on a line the name is (its first character), or -1: `symbol` as a whole name, else the line's first name
function column(text, symbol) {
  if (symbol) {
    const escaped = symbol.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const found = new RegExp(`(?<![\\w$])${escaped}(?![\\w$])`).exec(text);
    return found ? found.index : text.indexOf(symbol);
  }
  for (const found of text.matchAll(/[A-Za-z_$][\w$]*/g)) {
    if (!KEYWORDS.has(found[0])) return found.index;
  }
  return -1;
}

// A problem as an agent (and the chat's @problems) reads it: path:line:col: error: message (source code)
function problemLine(p) {
  const source = p.source
    ? ` (${p.source}${p.code !== undefined && p.code !== null && p.code !== "" ? ` ${p.code}` : ""})` : "";
  return `${p.path}:${p.line}:${p.col}: ${p.error ? "error" : "warning"}: ${String(p.message).split("\n")[0]}${source}`;
}

function problemsText(found, { most = 100, where = "" } = {}) {
  if (!found.length) return null;
  const errors = found.filter((p) => p.error).length;
  const sorted = [...found].sort((a, b) => (b.error - a.error) || a.path.localeCompare(b.path) || a.line - b.line);
  return `${errors} error${errors === 1 ? "" : "s"} and ${found.length - errors} warning`
    + `${found.length - errors === 1 ? "" : "s"}${where}:\n${sorted.slice(0, most).map(problemLine).join("\n")}`
    + (found.length > most ? `\n… and ${found.length - most} more` : "");
}

// Places in the code ({path, line, col, text}): one line each, grouped by file
function placesText(found, { most = 200, what = "place" } = {}) {
  if (!found.length) return null;
  const files = new Set(found.map((p) => p.path)).size;
  const sorted = [...found].sort((a, b) => a.path.localeCompare(b.path) || a.line - b.line || a.col - b.col);
  return `${found.length} ${what}${found.length === 1 ? "" : "s"} in ${files} file${files === 1 ? "" : "s"}:\n`
    + sorted.slice(0, most).map((p) => `${p.path}:${p.line}:${p.col}: ${p.text.trim()}`).join("\n")
    + (found.length > most ? `\n… and ${found.length - most} more` : "");
}

// A definition with its lines ({path, line, col, lines: [[number, text]]})
function definitionText(found) {
  if (!found.length) return null;
  return found.map((d) => `${d.path}:${d.line}:${d.col}\n`
    + d.lines.map(([n, text]) => `${String(n).padStart(5)}  ${text}`).join("\n")).join("\n\n");
}

// Symbols across the folder ({name, kind, container, path, line}), or a file's outline ({name, kind, line, depth})
function symbolsText(found, { most = 200, outline = false } = {}) {
  if (!found.length) return null;
  const shown = found.slice(0, most).map((s) => outline
    ? `${"  ".repeat(s.depth || 0)}${s.kind} ${s.name} (line ${s.line})`
    : `${s.kind} ${s.name}${s.container ? ` (in ${s.container})` : ""}: ${s.path}:${s.line}`);
  return shown.join("\n") + (found.length > most ? `\n… and ${found.length - most} more` : "");
}

module.exports = { ToolWindow, column, definitionText, placesText, problemLine, problemsText, symbolsText };
