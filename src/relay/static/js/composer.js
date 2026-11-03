// The message box: typing, / commands, attachments, sending, queueing and stopping.

import { acceptFiles, AttachmentTray } from "./attachments.js";
import { clear, closeMenu, h, icon, showMenu, toast } from "./ui.js";

// commands: () => [{ id, description, icon?, run(arg), choices?(arg) -> [{ label, icon?, value }]
// (or a promise of them), arg?: "choice" | "text" | "optional", agent?, cycle? }], typed as /id.
// Relay's own commands are done right away; the agent's (agent: true) and
// anything else starting with / go to the agent as a message.
export function createComposer({ placeholder, onSend, onStop, onQueue = null, pickers, running = false,
  commands = null }) {
  const textarea = h("textarea", { rows: 1, placeholder, "aria-label": "Message" });
  const send = h("button.send-btn", { type: "button", title: "Send", disabled: true });
  const pickerRow = h("div.pickers");
  const optionsLabel = h("span", "Options");
  const advanced = h("div.advanced-pickers");
  const options = h("details.composer-options", h("summary", { title: "Mode and fallback options" },
    icon("settings", "sm"), optionsLabel, icon("chevronDown", "sm")), advanced);
  const pickerArea = h("div.composer-pickers", pickerRow, options);
  function refreshPickers() {
    const focused = [...pickerArea.querySelectorAll("button")].indexOf(document.activeElement);
    const choices = pickers ? pickers() : [];
    clear(pickerRow, Array.isArray(choices) ? choices : choices.primary);
    const extras = Array.isArray(choices) ? [] : (choices.advanced || []).filter(Boolean);
    clear(advanced, extras);
    options.hidden = !extras.length;
    optionsLabel.textContent = choices.label || "Options";
    if (commands?.().some((command) => command.id === "mode" && !command.agent && command.cycle)) {
      textarea.setAttribute("aria-keyshortcuts", "Shift+Tab");
    } else textarea.removeAttribute("aria-keyshortcuts");
    if (focused >= 0) pickerArea.querySelectorAll("button")[focused]?.focus({ preventScroll: true });
    // Keep the disclosure open when a picker changes, along with the message draft.
  }
  refreshPickers();
  const tray = new AttachmentTray({ onChange: () => update() });
  const fileInput = h("input", { type: "file", multiple: true, class: "hidden", "aria-hidden": "true",
    onchange: () => { tray.add([...fileInput.files]); fileInput.value = ""; } });
  const attach = h("button.icon-btn.attach-btn", { type: "button", title: "Attach files",
    "aria-label": "Attach files", onclick: () => fileInput.click() }, icon("paperclip"));
  let isRunning = running;
  let changingMode = false;
  let queuedModes = 0;

  function resize() {
    textarea.style.height = "auto";
    textarea.style.height = `${Math.min(textarea.scrollHeight, innerHeight * 0.4)}px`;
  }
  // While the agent works, a typed message is queued instead: the button sends
  // it when the answer is done; with nothing typed it stops the agent.
  function queueing() {
    return isRunning && onQueue && Boolean(textarea.value.trim() || tray.ids().length)
      && !relayCommand(textarea.value.trim());
  }
  function update() {
    const queue = queueing();
    send.replaceChildren(icon(isRunning && !queue ? "stop" : "send"));
    send.classList.toggle("stop", isRunning && !queue);
    send.classList.toggle("queue", queue);
    send.disabled = submitting || (changingMode && (!isRunning || queue))
      || (queue ? tray.busy() : !isRunning && (tray.busy() || (!textarea.value.trim() && !tray.ids().length)));
    send.title = queue ? "Send when this answer is done" : isRunning ? "Stop"
      : tray.busy() ? "Waiting for uploads…" : "Send";
  }
  const allCommands = () => (commands ? commands() : []);
  const modeCommand = () => allCommands().find((command) => command.id === "mode" && !command.agent && command.cycle);
  async function cycleModes() {
    queuedModes++;
    if (changingMode) return;
    changingMode = true;
    closeSlashMenu();
    update();
    try {
      // Read the current mode after each save; quick presses cannot race PATCH requests.
      while (queuedModes && el.isConnected) {
        queuedModes--;
        const command = modeCommand();
        if (!command) break;
        await command.cycle();
      }
    } catch (error) {
      if (!error.told) toast(error.message, "error");
    } finally {
      queuedModes = 0;
      changingMode = false;
      update();
    }
  }
  // "/mode plan" → Relay's /mode with "plan"; null for the agent's commands and plain text.
  function relayCommand(text) {
    const match = text.match(/^\/([\w-]+)(?:\s+([\s\S]*))?$/);
    const command = match ? allCommands().find((c) => c.id === match[1].toLowerCase() && !c.agent) : null;
    return command ? { command, arg: (match[2] || "").trim() } : null;
  }
  function setText(text) {
    textarea.value = text;
    resize();
    update();
    textarea.focus();
  }
  let slashOpen = false;
  function closeSlashMenu() {
    if (slashOpen) closeMenu();
  }
  function openSlashMenu(items) {
    slashOpen = true;
    showMenu(textarea, items, { focus: false, onClose: () => { slashOpen = false; } });
  }
  async function run(command, arg) {
    closeSlashMenu();
    const typed = textarea.value;
    setText("");
    try {
      await command.run(arg);
    } catch (error) {
      if (!error.told) toast(error.message, "error");
      // (what was typed comes back, to fix it: /timed's task, a long /rename)
      if (!textarea.value) { textarea.value = typed; resize(); update(); }
    }
  }
  // The choices of a command, e.g. the modes after "/mode ", narrowed by what's typed.
  async function showChoices(command, typed) {
    let items = await command.choices(typed);
    const still = textarea.value.match(/^\/([\w-]+)\s+(.*)$/);
    if (!still || still[1].toLowerCase() !== command.id) return;  // typed on meanwhile
    const word = typed.toLowerCase();
    items = items.filter((item) => !word || item.label.toLowerCase().includes(word)
      || String(item.value).toLowerCase().startsWith(word));
    if (!items.length) {
      closeSlashMenu();
      return;
    }
    openSlashMenu(items.map((item) => ({ label: item.label, icon: item.icon || command.icon,
      onclick: () => run(command, String(item.value)) })));
  }
  function pick(command) {
    // Chosen from the menu: run it, or wait for what it needs.
    if (command.choices || command.arg === "text" || command.agent) {
      setText(`/${command.id} `);
      if (command.choices) showChoices(command, "");
    } else {
      run(command, "");
    }
  }
  function slashMenu() {
    const withArg = textarea.value.match(/^\/([\w-]+)\s+([^\n]*)$/);
    if (withArg) {
      const command = allCommands().find((c) => c.id === withArg[1].toLowerCase() && c.choices);
      if (command) {
        showChoices(command, withArg[2].trim());
        return;
      }
    }
    const match = textarea.value.match(/^\/([\w-]*)$/);
    const word = match ? match[1].toLowerCase() : null;
    const found = word === null ? [] : allCommands().filter((c) => c.id.startsWith(word));
    if (!found.length) {
      closeSlashMenu();
      return;
    }
    openSlashMenu(found.map((c) => ({ label: `/${c.id} — ${c.description}`,
      icon: c.icon || (c.agent ? "terminal" : "tool"), onclick: () => pick(c) })));
  }
  // Tab completes a command's name, or its choice, as far as it's certain (like a shell).
  async function complete() {
    const text = textarea.value;
    const name = text.match(/^\/([\w-]*)$/);
    if (name) {
      const found = allCommands().filter((c) => c.id.startsWith(name[1].toLowerCase()));
      if (found.length === 1) {
        const [command] = found;
        const needsMore = command.choices || command.arg === "text" || command.agent;
        setText(`/${command.id}${needsMore ? " " : ""}`);
        if (command.choices) showChoices(command, "");
        else closeSlashMenu();
      } else if (found.length) {
        const common = commonPrefix(found.map((c) => c.id));
        if (common.length > name[1].length) {
          setText(`/${common}`);
          slashMenu();
        }
      }
      return;
    }
    const withArg = text.match(/^\/([\w-]+)\s+(\S*)$/);
    const command = withArg && allCommands().find((c) => c.id === withArg[1].toLowerCase() && c.choices);
    if (!command) return;
    const typed = withArg[2];
    const values = (await command.choices(typed)).map((item) => String(item.value))
      .filter((value) => value.toLowerCase().startsWith(typed.toLowerCase()));
    if (!values.length || textarea.value !== text) return;
    const common = values.length === 1 ? values[0] : commonPrefix(values);
    if (common.length > typed.length) {
      setText(`/${command.id} ${common}`);
      slashMenu();
    }
  }
  // One message at a time: a second Enter while the first is on its way would send it twice.
  let submitting = false;
  async function submit() {
    if (submitting) return;
    if (changingMode) {
      if (isRunning && !queueing()) return onStop?.();
      return;
    }
    submitting = true;
    update();
    try {
      await submitNow();
    } finally {
      submitting = false;
      update();
    }
  }
  function clearSent(text, items) {
    // Typing/uploading may have continued while the request was in flight.
    // Clear only the draft we sent; never discard newer text or attachments.
    if (textarea.value === text) textarea.value = "";
    for (const item of items) if (tray.items.includes(item)) tray.remove(item);
    resize();
  }
  async function submitNow() {
    const typed = textarea.value.trim();
    const found = relayCommand(typed);
    if (found) {
      const { command, arg } = found;
      if (!arg && command.choices && command.arg === "choice") {
        setText(`/${command.id} `);  // which one? the menu asks
        showChoices(command, "");
        return;
      }
      await run(command, arg);
      return;
    }
    closeSlashMenu();
    if (queueing()) {
      if (tray.busy()) return;
      const text = textarea.value;
      const items = [...tray.items];
      const ok = await onQueue(text, tray.ids());
      if (ok !== false) clearSent(text, items);
      update();
      return;
    }
    if (isRunning) return onStop?.();
    const text = textarea.value;
    const attachments = tray.ids();
    const items = [...tray.items];
    if (tray.busy() || (!text.trim() && !attachments.length)) return;
    send.disabled = true;
    const ok = await onSend(text, attachments, tray.items.map((i) => ({ id: i.id, name: i.name,
      size: i.size, mime: i.file.type })));
    if (ok !== false) clearSent(text, items);
    update();
  }
  textarea.addEventListener("input", () => { resize(); update(); slashMenu(); });
  textarea.addEventListener("keydown", (e) => {
    if (e.key === "Tab" && e.shiftKey && !e.ctrlKey && !e.altKey && !e.metaKey && !e.isComposing && modeCommand()) {
      e.preventDefault();
      if (!e.repeat && !submitting) return cycleModes();
      return;
    }
    if (e.key === "Tab" && !e.shiftKey && textarea.value.startsWith("/")) {
      e.preventDefault();
      complete();
      return;
    }
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing && !matchMedia("(pointer: coarse)").matches) {
      e.preventDefault();
      if (!isRunning || queueing() || relayCommand(textarea.value.trim())) submit();
    }
  });
  send.addEventListener("click", submit);
  const el = h("div.composer", tray.el, textarea,
    h("div.composer-row", attach, fileInput, pickerArea, send));
  acceptFiles(textarea, el, (files) => tray.add(files));
  update();
  return {
    el,
    focus: () => textarea.focus(),
    setText: (text) => { textarea.value = text; resize(); update(); textarea.focus(); },
    // Part of an answer, quoted, to reply to; under what's typed already.
    quote: (text) => {
      const quoted = text.trim().split("\n").map((line) => `> ${line}`.trimEnd()).join("\n");
      const typed = textarea.value.trimEnd();
      textarea.value = `${typed ? `${typed}\n\n` : ""}${quoted}\n\n`;
      resize(); update(); textarea.focus();
      textarea.setSelectionRange(textarea.value.length, textarea.value.length);
    },
    setRunning: (value) => { isRunning = value; update(); },
    refreshPickers,
  };
}

function commonPrefix(words) {
  let prefix = words[0] || "";
  for (const word of words) {
    while (!word.toLowerCase().startsWith(prefix.toLowerCase())) prefix = prefix.slice(0, -1);
  }
  return prefix;
}
