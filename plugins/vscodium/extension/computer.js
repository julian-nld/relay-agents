// This computer in Relay while VSCodium runs (lib/connector.js): a conversation in a folder Relay's machine doesn't
// have becomes a computer task here, and the agent works in that folder through Relay's `relay run`, `pull` and
// `push`. Turned on once, when it's first needed; Relay: This computer turns it off again.
"use strict";

const vscode = require("vscode");
const { Connector, token, where } = require("./lib/connector");
const { hostName, sameMachine } = require("./lib/places");

const KEY = "relay.computer";  // ({on, id, name}: in globalState, shared by VSCodium's windows)

class Computer {
  constructor(context, relay) {
    this.context = context;
    this.relay = relay;
    this.connector = null;
    this.online = false;
    this.output = vscode.window.createOutputChannel("Relay: this computer");
    this.before = async () => {};  // (the review's checkpoint before a change: set by the extension)
    this.allow = null;  // (permission control in VSCodium: permissions.js, set by the extension)
    this.after = () => {};  // (each job done: following the agent, set by the extension)
    this.terminal = null;  // (what it ran, as it ran: a terminal of VSCodium's)
    this.onState = () => {};
  }

  get saved() {
    return this.context.globalState.get(KEY) || {};
  }

  say(text) {
    this.output.appendLine(`[${new Date().toLocaleTimeString()}] ${text}`);
  }

  // Polling for work while it's on and signed in; from the start again when the address or sign-in changed
  async start() {
    this.stop();
    const saved = this.saved;
    if (!saved.on || !saved.id || !this.relay.client) return;
    let script;
    try {
      script = await this.relay.client.get(`/api/computers/${encodeURIComponent(saved.id)}/script`);
    } catch (error) {
      this.say(`Relay doesn't have this computer any more (${error.message}): turn it on again (Relay: This computer)`);
      if (error.status === 404) await this.context.globalState.update(KEY, { ...saved, on: false });
      return;
    }
    const key = token(script);
    if (!key) {
      this.say("Relay's connector for this computer has no key in it: this Relay is too new or too old for it");
      return;
    }
    await this.quiet(saved.id);
    const dim = (text) => `\x1b[2m${text}\x1b[0m\n`;
    this.connector = new Connector({ url: this.relay.url(), token: key, say: (text) => this.say(text),
      allow: this.allow && ((job) => this.allow(job)),
      before: async (job) => {
        this.shown(job.kind === "run" ? `\x1b[1m$ ${job.command}\x1b[0m  ${dim(`(in ${where(job.cwd)})`)}`
          : dim(`writes ${where(job.path)}`));
        await this.before(job);
      },
      output: (text) => this.shown(text),
      after: (job, result) => {
        if (job.kind === "read") this.shown(dim(`reads ${where(job.path)}${result.ok ? "" : `: ${result.error}`}`));
        else if (!result.ok) this.shown(
          `\x1b[33m${job.kind === "run" ? job.command : `write ${job.path}`}: ${result.error}\x1b[0m\n`);
        else if (job.kind === "run") this.shown(dim(`(exit ${result.exit})`));
        this.after(job, result);
      },
      state: (state) => {
        this.online = state === "online";
        this.onState(state);
      } });
    this.connector.start();
  }

  // VSCodium asks before an agent's command or change here (relay.permissions), so Relay doesn't too: its own
  // question (the computer's `ask`) is turned off
  async quiet(id) {
    try {
      const found = (await this.relay.client.get("/api/computers")).find((c) => c.id === id);
      if (found?.ask) {
        await this.relay.client.patch(`/api/computers/${encodeURIComponent(id)}`, { ask: false });
        this.say("Relay's own question before each command is off: VSCodium asks (relay.permissions).");
      }
    } catch { /* (asked twice, then) */ }
  }

  // What it runs, in VSCodium's terminal "Relay: <computer>": opened (without the focus) when it first runs something
  shown(text) {
    if (!this.terminal) {
      const write = new vscode.EventEmitter();
      const waiting = [];
      const terminal = { write, opened: false, waiting };
      terminal.view = vscode.window.createTerminal({ name: `Relay: ${this.saved.name || "this computer"}`,
        iconPath: new vscode.ThemeIcon("remote"), isTransient: true, pty: {
          onDidWrite: write.event,
          open: () => {
            terminal.opened = true;
            write.fire("\x1b[2mWhat Relay's agents run on this computer, as it runs.\x1b[0m\r\n");
            waiting.splice(0).forEach((piece) => write.fire(piece));
          },
          close: () => {
            if (this.terminal === terminal) this.terminal = null;
          },
        } });
      this.terminal = terminal;
      if (text) terminal.view.show(true);
    }
    if (!text) return;
    const piece = text.replace(/\r?\n/g, "\r\n");
    if (this.terminal.opened) this.terminal.write.fire(piece);
    else this.terminal.waiting.push(piece);
  }

  stop() {
    this.connector?.stop();
    this.connector = null;
    this.online = false;
  }

  dispose() {
    this.stop();
    this.output.dispose();
  }

  // This computer as one of Relay's, for a folder that's only here: {id, name}, or null when the user would rather not
  async ensure() {
    const saved = this.saved;
    if (saved.on && saved.id) {
      if (!this.connector) await this.start();
      return { id: saved.id, name: saved.name };
    }
    if (saved.declined) return null;
    const picked = await vscode.window.showInformationMessage(
      "This folder is on this computer, not on Relay's machine. Let Relay's agents work in your folders here while "
      + "VSCodium runs? They run commands and change files here as you; Relay asks you before each one (in the chat).",
      { modal: true }, "Let them", "Chat only");
    if (picked !== "Let them") {
      if (picked === "Chat only") await this.context.globalState.update(KEY, { ...saved, declined: true });
      return null;
    }
    return this.turnOn();
  }

  // Relay runs elsewhere, so its agents can't reach this computer's files unless it's one of Relay's computers: on by
  // itself when Relay has a computer of this name already (Settings → Computers), else asked once
  async offer() {
    const saved = this.saved;
    if (saved.on || saved.declined || !this.relay.client || sameMachine(this.relay.url())) return;
    const all = await this.relay.client.get("/api/computers").catch(() => null);
    const known = all?.find((c) => c.name === hostName());
    if (known) {
      await this.turnOn();
      vscode.window.showInformationMessage(`Relay: this computer is “${known.name}” in Relay, so its agents can work `
        + "in its folders while VSCodium runs. VSCodium asks you before each command and change.", "Settings")
        .then((picked) => picked && this.manage());
      return;
    }
    if (!all || saved.offered) return;
    await this.context.globalState.update(KEY, { ...saved, offered: true });
    const picked = await vscode.window.showInformationMessage("Relay runs on another machine. Let its agents work in "
      + "this computer's folders while VSCodium runs? VSCodium asks you before each command and change.",
    "Let them", "Not now");
    if (picked === "Let them") await this.turnOn();
  }

  async turnOn() {
    const client = this.relay.client;
    const name = hostName();
    const all = await client.get("/api/computers");
    let found = all.find((c) => c.name === name);
    if (!found) found = await client.post("/api/computers", { name });  // (VSCodium asks itself: relay.permissions)
    await this.context.globalState.update(KEY, { on: true, id: found.id, name: found.name });
    await this.start();
    this.say(`This computer is “${found.name}” in Relay (Settings → Computers).`);
    return { id: found.id, name: found.name };
  }

  // Relay: This computer: on or off, and whether Relay asks before each command
  async manage() {
    const saved = this.saved;
    let computer = null;
    if (saved.id && this.relay.client) {
      try {
        computer = (await this.relay.client.get("/api/computers")).find((c) => c.id === saved.id) || null;
      } catch { /* (said below as not known) */ }
    }
    const settings = vscode.workspace.getConfiguration("relay");
    const asking = { ask: "before each command and file change", commands: "before each command",
      off: "never: agents go ahead" }[settings.get("permissions") || "ask"];
    const inline = settings.get("review.inline") !== false;
    const items = [...saved.on && computer ? [
      { label: "$(debug-stop) Turn it off", description: "agents can't work here until it's on again", act: "off" },
      { label: "$(terminal) Show what it ran", description: "its commands and their output, as they ran", act: "log" },
    ] : [{ label: "$(plug) Turn it on", description: "agents can work in this computer's folders while VSCodium runs",
      act: "on" }],
    { label: "$(shield) VSCodium asks…", description: asking, act: "ask" },
    { label: inline ? "$(eye-closed) Turn Keep/Undo in the editor off" : "$(eye) Turn Keep/Undo in the editor on",
      description: inline ? "each change of the agent's marked, with Keep · Undo"
        : "only Source Control has the changes",
      act: "inline" }];
    const picked = await vscode.window.showQuickPick(items, { title: `Relay: this computer${computer
      ? ` (“${computer.name}”, ${this.online ? "online" : "offline"})` : ""}` });
    if (!picked) return;
    if (picked.act === "off") {
      this.stop();
      await this.context.globalState.update(KEY, { ...saved, on: false, declined: true });
    } else if (picked.act === "on") {
      await this.context.globalState.update(KEY, { ...saved, declined: false });
      await this.turnOn();
    } else if (picked.act === "ask") {
      const how = await vscode.window.showQuickPick([
        { label: "Before each command and file change", description: "a change shows as a diff first", value: "ask" },
        { label: "Before each command", description: "file changes go ahead (Keep/Undo has them)", value: "commands" },
        { label: "Never", description: "agents go ahead", value: "off" }], { title: "Relay: VSCodium asks…" });
      if (how) await settings.update("permissions", how.value, vscode.ConfigurationTarget.Global);
    } else if (picked.act === "inline") {
      await settings.update("review.inline", !inline, vscode.ConfigurationTarget.Global);
    } else {
      this.shown("");
      this.terminal.view.show();
    }
  }
}

// What the agent of a computer task is told about the folder VSCodium has open (its own instructions)
function instructions(folder, name) {
  const quoted = `'${folder.replace(/'/g, "'\\''")}'`;
  return `VSCodium has the folder ${folder} open on the user's computer "${name}": that's the project. Work there, not `
    + `on the machine you run on: run its commands with \`relay run --cwd ${quoted} '…'\`, `
    + `read a file with \`relay pull `
    + `'${folder}/<path>' <here>\` and write it back with \`relay push <here> '${folder}/<path>'\` (or computer_run, `
    + "computer_read_file and computer_write_file). Name files to the user by their path in that folder: they review "
    + "your changes in VSCodium. VSCodium's language servers answer about the code there: vscodium_definition, "
    + "vscodium_references, vscodium_symbols, vscodium_hover and vscodium_problems (paths in that folder).";
}

module.exports = { Computer, instructions };
