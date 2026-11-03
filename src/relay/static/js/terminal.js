// On-demand interactive shells: the agent's WorkBox; a provider's own box only as its Shell (Settings, a repair's page);
// never the host.

import { relayUrl, storeKey } from "./base.js";
import { clear, h, icon } from "./ui.js";

let libraries = null;
let styles = null;

// What a Shell in a provider's own box is, where it opens (relay/creations.py _in_its_box)
export function inItsBox(p) {
  const also = p.sign_in ? ` Sign in again here with ${p.sign_in}.` : p.shell_hint ? ` ${p.shell_hint}` : "";
  return `A shell in its own box, with its program and its login.${also}`;
}

export function hasShell(conv) {
  return Boolean(conv?.directory && !conv.computer && conv.git?.state !== "discarded");
}

export function shellButton(conv) {
  // (again: closed, as the bar's Shell does, when it's this chat's that's open)
  return hasShell(conv) ? h("button.picker", { type: "button", title: "A shell in this task's WorkBox, under the page",
    onclick: () => (shellOpen() && panel.conv?.id === conv.id ? closeShell() : openShell(conv)) },
  icon("terminal", "sm"), "Shell") : null;
}

function loadTerminal() {
  if (!styles) {
    styles = new Promise((resolve, reject) => {
      const link = h("link", { id: "xterm-css", rel: "stylesheet", href: relayUrl("/static/vendor/xterm.css"),
        onload: resolve,
        onerror: () => { link.remove(); styles = null; reject(new Error("Can't load terminal styles")); } });
      document.head.append(link);
    });
  }
  if (!libraries) libraries = Promise.all([import("../vendor/xterm.mjs"), import("../vendor/xterm-fit.mjs"), styles])
    .catch((error) => { libraries = null; throw error; });
  return libraries;
}

// The Shell (Relay 2.11): a panel under the page, the open chat's WorkBox; it follows the chat that's open, and
// it's as tall as it's dragged. A service's page has a shell of its own (its Shell tab).
let panel = null;
const HEIGHT = { min: 140, standard: 300 };

export function shellPanel() {
  if (panel) return panel.el;
  const status = h("span.shell-status", { role: "status" });
  const directory = h("span.shell-directory.mono.truncate");
  const keys = h("span.shell-keys-slot");
  const reconnect = h("button.btn.small", { type: "button", hidden: true, onclick: () => connect(panel.conv) },
    "New shell");
  // (closing the panel keeps the chat's shell: this ends it)
  const ending = h("button.btn.small.ghost", { type: "button", hidden: true, title: "End this shell: closing the panel "
    + "keeps it running for when you open it again", onclick: () => panel.shell?.end() }, "End shell");
  const body = h("div.bottom-body");
  const el = h("section.bottom-panel", { hidden: true, "aria-label": "Shell" }, resizeHandle(),
    h("div.bottom-head", icon("terminal", "sm"), h("span.bottom-title", "Shell"), directory, h("span.grow"), status,
      keys, ending, reconnect,
      h("button.icon-btn.sm", { type: "button", title: "Close the shell", "aria-label": "Close the shell",
          onclick: closeShell }, icon("x", "sm"))),
    body);
  panel = { el, status, directory, keys, reconnect, ending, body, conv: null, id: null, shell: null };
  setHeight(kept());
  window.addEventListener("relay:signed-out", closeShell);
  return el;
}

export function shellOpen() {
  return Boolean(panel && !panel.el.hidden);
}

// The composer's Shell, /shell and the bar's: the panel open, on this chat's WorkBox
export function openShell(conv) {
  if (!panel?.el.isConnected) return;
  const opening = panel.el.hidden;
  panel.el.hidden = false;
  show(conv, { focus: true });
  if (opening) window.dispatchEvent(new CustomEvent("relay:shell"));
}

// Closing it leaves the chat's shell running (Relay keeps it a while): opened again, it's that one
export function closeShell() {
  if (!shellOpen()) return;
  const inside = panel.el.contains(document.activeElement);
  end();
  panel.el.hidden = true;
  if (inside) document.querySelector(".composer textarea")?.focus({ preventScroll: true });
  window.dispatchEvent(new CustomEvent("relay:shell"));
}

// The chat open now (null: a page): the panel, while it's open, shows its shell
export function followShell(conv) {
  if (shellOpen()) show(conv);
}

function show(conv, { focus = false } = {}) {
  panel.conv = conv;
  if (hasShell(conv)) {
    if (panel.id === conv.id && panel.shell && panel.reconnect.hidden) {
      if (focus) panel.shell.focus();
    } else connect(conv);
    return;
  }
  end();
  clear(panel.body, h("div.bottom-empty", !conv ? "Open a chat or a code task: its WorkBox's shell shows here."
    : conv.computer ? "A computer task's commands run on that computer: there's no shell here."
      : conv.directory ? "This task's branch was thrown away, and its WorkBox with it."
        : "This chat works in no folder: a code task's WorkBox has a shell (or give this chat a project folder)."));
}

function connect(conv) {
  end();
  panel.id = conv.id;
  const host = h("div.shell-terminal", { "aria-label": "Interactive terminal" });
  clear(panel.body, host);
  panel.directory.textContent = conv.directory;
  panel.directory.title = conv.directory;
  panel.status.textContent = "Starting WorkBox…";
  panel.shell = mountTerminal(host, `/api/conversations/${conv.id}/terminal`, panel.status, {
    connected: "Closing the panel keeps your shell running: it's this one again when you open it. End shell (or exit) "
      + "ends it.",
    onReady: () => { panel.ending.hidden = false; },
    onEnd: (message, { exited }) => {
      panel.ending.hidden = true;
      panel.reconnect.textContent = exited ? "New shell" : "Reconnect";  // (it goes on: the same one)
      panel.reconnect.hidden = false;
    } });
  clear(panel.keys, panel.shell.keys);
}

function end() {
  panel.shell?.close();
  panel.shell = null;
  panel.id = null;
  panel.reconnect.hidden = true;
  panel.ending.hidden = true;
  panel.directory.textContent = "";
  panel.status.textContent = "";
  panel.status.removeAttribute("title");
  clear(panel.keys);
  clear(panel.body);
}

// Its height: dragged at its top edge, or with the arrow keys there; a double click gives the usual one
function resizeHandle() {
  const handle = h("div.bottom-resize", { role: "separator", tabindex: "0", "aria-orientation": "horizontal",
    "aria-label": "The shell's height", title: "Drag to make the shell taller or lower (double click: as it was)" });
  handle.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    handle.setPointerCapture(event.pointerId);
    const from = panel.el.getBoundingClientRect().height;
    const start = event.clientY;
    panel.el.classList.add("resizing");
    const move = (e) => setHeight(from + start - e.clientY);
    const done = () => {
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", done);
      handle.removeEventListener("pointercancel", done);
      panel.el.classList.remove("resizing");
      keep();
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", done);
    handle.addEventListener("pointercancel", done);
  });
  handle.addEventListener("keydown", (event) => {
    const step = { ArrowUp: 16, ArrowDown: -16 }[event.key];
    if (!step) return;
    event.preventDefault();
    setHeight(panel.el.getBoundingClientRect().height + step);
    keep();
  });
  handle.addEventListener("dblclick", () => {
    setHeight(HEIGHT.standard);
    keep();
  });
  return handle;
}

// Within bounds: never so tall that the page above it goes (the window's height less 200 px)
export function clampHeight(value, room = innerHeight) {
  const height = Number(value);
  if (!Number.isFinite(height) || height <= 0) return HEIGHT.standard;
  return Math.round(Math.max(HEIGHT.min, Math.min(Math.max(HEIGHT.min, room - 200), height)));
}

function setHeight(value) {
  const height = clampHeight(value);
  panel.el.style.setProperty("--shell-height", `${height}px`);
  panel.el.querySelector(".bottom-resize")?.setAttribute("aria-valuenow", String(height));
}

// (kept in this browser: only a convenience)
function kept() {
  try { return localStorage.getItem(storeKey("relay.shellheight")); } catch { return null; }
}

function keep() {
  try {
    localStorage.setItem(storeKey("relay.shellheight"),
      String(parseFloat(panel.el.style.getPropertyValue("--shell-height"))));
  } catch { /* (only a convenience) */ }
}

// An xterm in `host` on the terminal socket at `path`; `status` says how it's going. Its keys
// (Tab, Ctrl+C, Esc) are for the caller to place; close() leaves it (a chat's shell goes on, a service's ends),
// end() ends it. onEnd(message, {exited}): exited when the shell itself ended, not only this connection.
export function mountTerminal(host, path, status, { connected = "", onReady = () => {}, onEnd = () => {} } = {}) {
  let terminal, socket, observer, frame;
  let closed = false, ready = false, ended = false, replaying = false;
  const send = (data) => {
    if (ready && socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "input", data }));
  };
  const keys = h("div.shell-keys", [
    ["Tab", "\t"], ["Ctrl+C", "\x03"], ["Esc", "\x1b"],
  ].map(([label, data]) => h("button.btn", { type: "button", disabled: true,
    onclick: () => { send(data); terminal?.focus(); } }, label)));
  // Escape and Tab belong to the shell (editors/completion), not the composer's shortcuts or modal.
  host.addEventListener("keydown", (event) => event.stopPropagation());
  function finish(message, exited = false) {
    if (closed || ended) return;
    ended = true; ready = false;
    status.textContent = message;
    keys.querySelectorAll("button").forEach((button) => { button.disabled = true; });
    if (terminal) terminal.options.disableStdin = true;
    onEnd(message, { exited });
  }
  async function start() {
    try {
      const [{ Terminal }, { FitAddon }] = await loadTerminal();
      if (closed) return;
      const colors = getComputedStyle(document.documentElement);
      const nonce = document.querySelector('meta[name="relay-style-nonce"]')?.content;
      const createElement = document.createElement;
      const createWithNonce = (...args) => {
        const element = createElement.call(document, ...args);
        if (args[0] === "style" && nonce) element.nonce = nonce;
        return element;
      };
      // Async renderer styles use the override; xterm's viewport uses ownerDocument during open().
      const terminalDocument = new Proxy(document, { get(target, key) {
        if (key === "createElement") return createWithNonce;
        const value = Reflect.get(target, key, target);
        return typeof value === "function" ? value.bind(target) : value;
      } });
      terminal = new Terminal({ cursorBlink: true, fontSize: 13, scrollback: 5000,
        fontFamily: colors.getPropertyValue("--font-mono").trim(), documentOverride: terminalDocument,
        theme: { background: colors.getPropertyValue("--bg-code").trim(),
          foreground: colors.getPropertyValue("--fg").trim(),
          cursor: colors.getPropertyValue("--accent").trim(),
          selectionBackground: colors.getPropertyValue("--focus").trim() } });
      const fit = new FitAddon();
      terminal.loadAddon(fit);
      document.createElement = createWithNonce;
      try { terminal.open(host); } finally { document.createElement = createElement; }
      fit.fit();
      socket = new WebSocket(`${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}${relayUrl(path)}`);
      socket.binaryType = "arraybuffer";
      const size = () => {
        if (socket.readyState
          === WebSocket.OPEN) socket.send(JSON.stringify({ type: "resize", cols: terminal.cols, rows: terminal.rows }));
      };
      socket.onopen = size;
      socket.onmessage = (event) => {
        if (closed) return;
        if (event.data instanceof ArrayBuffer) { terminal.write(new Uint8Array(event.data)); return; }
        const message = JSON.parse(event.data);
        if (message.type === "ready") {
          ready = true;
          keys.querySelectorAll("button").forEach((button) => { button.disabled = false; });
          // (a chat's shell opened again: the same one, what it showed written first, at the size it was written
          // at; then this window's size, and the terminal wraps it anew: "replayed")
          status.textContent = `${message.resumed ? "Reconnected" : "Connected"} · ${message.container}`;
          status.title = connected;
          if (message.size) {
            replaying = true;
            terminal.resize(...message.size);
          } else size();
          terminal.focus();
          onReady(message);
        } else if (message.type === "replayed") {
          terminal.write("", () => {  // (once what came before is written)
            replaying = false;
            fit.fit();
            size();
          });
        } else if (message.type === "exit") finish(message.ended ? "Shell ended"
          : `Shell exited (${message.code})`, true);
        else if (message.type === "detached") finish(message.message);
        else if (message.type === "error") finish(message.message);
      };
      socket.onclose = () => { if (!ended) finish("Disconnected (Relay restarting, or the network)"); };
      socket.onerror = () => finish("Can't open the shell. Is Relay reachable and are you signed in?");
      terminal.onData(send);
      terminal.onResize(() => { if (ready) size(); });
      observer = new ResizeObserver(() => {
        cancelAnimationFrame(frame);
        frame = requestAnimationFrame(() => { if (!closed && !replaying) fit.fit(); });
      });
      observer.observe(host);
      terminal.focus();
    } catch (error) {
      finish(`Can't open the shell: ${error.message}`);
    }
  }
  start();
  return {
    keys,
    focus: () => (terminal ? terminal.focus() : host.focus()),
    end() {
      if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "end" }));
    },
    close() {
      if (closed) return;
      closed = true;
      observer?.disconnect();
      cancelAnimationFrame(frame);
      socket?.close();
      terminal?.dispose();
    },
  };
}
