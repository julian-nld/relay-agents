// The chat's page, in VSCodium's webview: what the extension sends (the conversation, its turns as they come) drawn in
// VSCodium's own colours, and what you do sent back. It reaches Relay only through the extension.
(() => {
  "use strict";
  const vscode = acquireVsCodeApi();
  const app = document.getElementById("app");
  const ICONS = {
    clip: "M21 11.5l-8.5 8.5a5 5 0 0 1-7-7L14 4.5a3.5 3.5 0 0 1 5 5L10.5 18a2 2 0 0 1-3-3L15 7.5",
    out: "M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5",
    down: "M6 9l6 6 6-6",
    folder: "M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z",
    chat: "M4 5h16v11H9l-5 4z",
    side: "M4 4h16v16H4zM14 4v16",
    tab: "M4 6h16v14H4zM4 10h16",
    file: "M6 3h8l5 5v13H6zM14 3v5h5",
  };

  // An element: h("div.a.b", {attributes, onclick}, children…); the attributes are optional
  function h(spec, ...rest) {
    const [tag, ...classes] = spec.split(".");
    const el = document.createElement(tag || "div");
    if (classes.length) el.className = classes.join(" ");
    const attributes = rest[0] && typeof rest[0] === "object" && !(rest[0] instanceof Node) && !Array.isArray(rest[0])
      ? rest.shift() : {};
    for (const [key, value] of Object.entries(attributes)) {
      if (value === undefined || value === null || value === false) continue;
      if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
      else if (key === "class") el.className = `${el.className} ${value}`.trim();
      else if (key === "hidden") el.hidden = !!value;
      else if (key in el && typeof value !== "string") el[key] = value;
      else el.setAttribute(key, value === true ? "" : value);
    }
    for (const child of rest.flat()) if (child !== null && child !== undefined && child !== false) el.append(child);
    return el;
  }

  function icon(name) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("class", "icon");
    svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", ICONS[name]);
    svg.append(path);
    return svg;
  }

  const send = (message) => vscode.postMessage(message);

  // HTML from VSCodium's Markdown, kept to what an answer needs: no scripts, frames, forms or handlers
  function safe(html) {
    const doc = new DOMParser().parseFromString(`<body>${html}</body>`, "text/html");
    doc.body.querySelectorAll("script, style, iframe, frame, object, embed, form, link, meta, base, button, textarea,"
      + " select, input:not([type=checkbox])").forEach((el) => el.remove());
    for (const el of doc.body.querySelectorAll("*")) {
      for (const { name, value } of [...el.attributes]) {
        const key = name.toLowerCase();
        if (key.startsWith("on") || key === "style" || key === "srcdoc"
            || (["href", "src", "xlink:href", "action", "formaction"].includes(key)
                && /^\s*(javascript|vbscript|data:(?!image\/(png|jpe?g|gif|webp)))/i.test(value))) {
          el.removeAttribute(name);
        }
      }
      if (el.tagName === "INPUT") el.disabled = true;
    }
    for (const pre of doc.body.querySelectorAll("pre")) {  // (a copy button on each code block)
      pre.classList.add("code");
    }
    return [...doc.body.childNodes];
  }

  // A file named in an answer's code (`src/app.js:12`, `README.md`, `~/notes.txt#L3`): a link to it, at its line.
  // A name without a folder needs a file's extension, so `items.map` stays code.
  const KNOWN = new RegExp("\\.(c|cc|cfg|cjs|conf|cpp|cs|css|csv|env|go|h|hpp|html?|ini|ipynb|java|js|json|jsx|kt|less|"
    + "lua|md|mjs|php|pl|ps1|py|rb|rs|scss|sh|sql|svelte|swift|toml|ts|tsx|txt|vue|xml|ya?ml)$", "i");
  const NAMED = new RegExp(String.raw`^((?:[A-Za-z]:[\\/]|~?\/|\.{1,2}\/)?(?:[\w@.+-]+[\\/])*[\w@+-][\w@.+-]*`
    + String.raw`\.[A-Za-z0-9]{1,12})(?::(\d+)(?:[-:]\d+)?|#L(\d+)(?:-L?\d+)?)?$`);
  function fileOf(text) {
    const found = NAMED.exec(text.trim());
    if (!found) return null;
    const [, file, line, anchor] = found;
    return /[\\/]/.test(file) || line || anchor || KNOWN.test(file) ? { file, line: line || anchor || "" } : null;
  }

  function fileLinks(el) {
    for (const code of el.querySelectorAll("code")) {
      if (code.closest("pre, a")) continue;
      const found = fileOf(code.textContent);
      if (!found) continue;
      const link = h("a.file", { href: "#", title: `Open ${found.file}${found.line ? ` at line ${found.line}` : ""}`,
        "data-file": found.file, "data-line": found.line });
      code.replaceWith(link);
      link.append(code);
    }
  }

  function copyButtons(el) {
    for (const pre of el.querySelectorAll("pre.code")) {
      if (pre.querySelector(".copy")) continue;
      pre.append(h("button.copy", { title: "Copy", onclick: async (event) => {
        event.stopPropagation();
        try {
          await navigator.clipboard.writeText(pre.querySelector("code")?.innerText ?? pre.innerText);
          event.target.textContent = "Copied";
          setTimeout(() => { event.target.textContent = "Copy"; }, 1200);
        } catch { /* no clipboard here */ }
      } }, "Copy"));
    }
  }

  // The page
  const title = h("div.title");
  const agent = h("button.chip", { title: "The agent: one of Relay's providers and its model",
    onclick: () => send({ type: "agent" }) });
  const mode = h("button.chip", { title: "Relay's mode for this conversation", onclick: () => send({ type: "mode" }) });
  const place = h("span.place");
  const effort =
    h("button.chip", { hidden: true, title: "How hard the model thinks", onclick: () => send({ type: "effort" }) });
  const changes = h("button.chip.changes", { hidden: true, title: "Its changes: in Source Control",
    onclick: () => send({ type: "changes" }) });
  const limit = h("span.limit", { hidden: true });
  const inRelay = h("button.icon-button", { title: "Open in Relay", "aria-label": "Open in Relay",
    onclick: () => send({ type: "relay" }) }, icon("out"));
  const move = h("button.icon-button", { onclick: () => send({ type: "move" }) });
  const queue = h("div.queue");
  const asks = h("div.asks");  // (VSCodium's own questions about this computer: permissions.js)
  const context = h("div.chips.context");
  const popup = h("div.popup", { hidden: true, role: "listbox" });
  const log = h("main.log", { "aria-live": "polite" });
  const notice = h("div.notice", { hidden: true, role: "status" });
  const chips = h("div.chips");
  const input = h("textarea.input", { rows: 1, "aria-label": "Your message",
    placeholder: "Message the agent (Enter sends)", title: "Enter sends; Shift+Enter: a new line" });
  const status = h("span.status");
  const stop = h("button.secondary", { hidden: true, onclick: () => send({ type: "stop" }) }, "Stop");
  const submit = h("button.primary", { onclick: () => post() }, "Send");
  app.append(h("header", h("div.top", title, move, inRelay), h("div.meta", agent, mode, effort, place, changes, limit)),
    log, queue, asks, notice, h("footer", popup, h("div.chips", context, chips), input,
      h("div.row", h("button.icon-button", { title: "Attach files (or paste or drop them here)",
        "aria-label": "Attach files", onclick: () => send({ type: "attach" }) }, icon("clip")), status, stop, submit)));

  let info = { title: "Relay" };
  let turns = [];
  const views = [];  // (each turn's element and its blocks', by place)

  let shownContext = null;  // (the open file's chip: {label, title, key})
  let dropped = null;  // (the key of the one × took away: it's back when another file is open)

  function post() {
    const text = input.value;
    if (!text.trim() && !chips.childElementCount) return;
    send({ type: "send", text, context: !!shownContext && shownContext.key !== dropped });
  }

  // @ (the folder's files, @problems) and / (commands) at the cursor: a list to pick from
  let offered = [];
  let chosen = 0;
  let asking = null;
  function word() {
    const before = input.value.slice(0, input.selectionStart);
    const slash = /^\/([\w-]*)$/.exec(before);
    if (slash) return { kind: "slash", query: slash[1], start: 0 };
    const at = /(^|\s)@([^\s@]*)$/.exec(before);
    return at ? { kind: "mention", query: at[2], start: before.length - at[2].length - 1 } : null;
  }
  function complete() {
    const found = word();
    clearTimeout(asking);
    if (!found) {
      popup.hidden = true;
      return;
    }
    asking = setTimeout(() => send({ type: "complete", kind: found.kind, query: found.query }), 120);
  }
  function offer(items) {
    offered = items;
    chosen = 0;
    popup.hidden = !items.length || !word();
    popup.replaceChildren(...items.map((item, n) => h(`div.option${n === chosen ? ".chosen" : ""}`,
      { role: "option", onmousedown: (event) => {
        event.preventDefault();
        take(n);
      } }, h("span", item.label), item.detail ? h("small.faint", item.detail) : null)));
  }
  function take(n) {
    const found = word();
    const item = offered[n];
    if (!found || !item) return;
    const end = input.selectionStart;
    input.value = input.value.slice(0, found.start) + item.insert + input.value.slice(end);
    input.selectionStart = input.selectionEnd = found.start + item.insert.length;
    popup.hidden = true;
    input.focus();
    grow();
  }

  input.addEventListener("keydown", (event) => {
    if (!popup.hidden && offered.length) {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        chosen = (chosen + (event.key === "ArrowDown" ? 1 : offered.length - 1)) % offered.length;
        [...popup.children].forEach((el, n) => el.classList.toggle("chosen", n === chosen));
        return;
      }
      if (event.key === "Enter" || event.key === "Tab") {
        event.preventDefault();
        take(chosen);
        return;
      }
      if (event.key === "Escape") {
        popup.hidden = true;
        return;
      }
    }
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      post();
    }
  });
  input.addEventListener("input", () => {
    grow();
    complete();
  });
  input.addEventListener("blur", () => setTimeout(() => { popup.hidden = true; }, 150));

  // Pasted or dropped files: attachments (a file from VSCodium's Explorer, dropped with Shift: its path)
  function files(list) {
    for (const file of list) {
      const reader = new FileReader();
      reader.onload = () => send({ type: "upload", name: file.name || "pasted.png", mime: file.type,
        data: String(reader.result).split(",")[1] || "" });
      reader.readAsDataURL(file);
    }
  }
  input.addEventListener("paste", (event) => {
    if (event.clipboardData?.files?.length) {
      event.preventDefault();
      files(event.clipboardData.files);
    }
  });
  app.addEventListener("dragover", (event) => event.preventDefault());
  app.addEventListener("drop", (event) => {
    event.preventDefault();
    const uris =
      (event.dataTransfer.getData("text/uri-list") || event.dataTransfer.getData("application/vnd.code.uri-list")
        || "").split(/\r?\n/).filter((u) => u && !u.startsWith("#"));
    if (uris.length) send({ type: "dropped", uris });
    else if (event.dataTransfer.files?.length) files(event.dataTransfer.files);
  });

  function grow() {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 240)}px`;
  }

  // Links in answers: a site opens in the browser, a file in an editor (the extension does both)
  log.addEventListener("click", (event) => {
    const link = event.target.closest("a[href], [data-file]");
    if (!link) return;
    event.preventDefault();
    if (link.dataset.file) send({ type: "file", path: link.dataset.file, line: Number(link.dataset.line) || null });
    else send({ type: "link", href: link.getAttribute("href") });
  });

  function header() {
    title.textContent = info.title || "Relay";
    agent.replaceChildren(h("span", info.agent || "Pick an agent"), icon("down"));
    mode.replaceChildren(h("span", info.mode || "Build"), icon("down"));
    const computer = info.computer;
    place.replaceChildren(icon(info.code ? "folder" : "chat"), h("span", info.code ? info.folder : "Chat"),
      ...computer ? [h(`span.computer${computer.online ? "" : ".offline"}`, `on ${computer.name}`)] : []);
    place.title = computer ? `A computer task in ${info.folderPath || "?"} on ${computer.name} (${computer.online
      ? "online" : "offline: VSCodium connects it while it runs"})` : info.code ? `A code task in ${info.folderPath}`
      : "A chat: no folder";
    const c = info.changes;
    changes.hidden = !(c && c.files);
    if (c && c.files) changes.textContent =
      `${c.files} file${c.files === 1 ? "" : "s"} +${c.additions} −${c.deletions}`;
    inRelay.hidden = !info.id;
    move.replaceChildren(icon(info.side ? "tab" : "side"));
    move.title = info.side ? "Open it in an editor tab" : "Open it in the side bar";
    move.setAttribute("aria-label", move.title);
    effort.hidden = !(info.efforts && info.efforts.length);
    effort.replaceChildren(h("span", `Effort: ${info.effort || "default"}`), icon("down"));
    limit.hidden = !info.limit;
    if (info.limit) {
      limit.textContent = `${info.limit.label} ${Math.round(info.limit.used * 100)}%`;
      limit.title =
        `The agent's usage limit: ${Math.round(info.limit.used * 100)}% of its ${info.limit.label} window used`;
      limit.className = `limit${info.limit.used >= 0.8 ? " high" : ""}`;
    }
    queue.replaceChildren(...(info.queue || []).map((q) => h("div.queued", h("div.bubble.faint", q.text),
      h("div.row", h("span.faint", "Queued: sent when the agent is done"),
        h("button.link", { onclick: () => send({ type: "queueSend", id: q.id }) }, "Send now"),
        h("button.link", { onclick: () => send({ type: "queueRemove", id: q.id }) }, "Remove")))));
    stop.hidden = !info.running;
    submit.textContent = info.running ? "Queue" : "Send";
    status.textContent = info.asks ? "Asks you" : info.running ? "Working…"
      : info.queued ? `${info.queued} queued` : "";
  }

  // A path in the task's folder, as the folder has it (a tool's line is about its files)
  function within(text) {
    if (!info.code || !info.folderPath) return String(text || "");
    const folder = info.folderPath.replace(/[\\/]+$/, "");
    return String(text || "").split(`${folder}/`).join("").split(`${folder}\\`).join("");
  }

  function nearBottom() {
    return log.scrollHeight - log.scrollTop - log.clientHeight < 80;
  }

  function settle(follow) {
    if (follow) log.scrollTop = log.scrollHeight;
  }

  function drawAll() {
    views.length = 0;
    log.replaceChildren();
    if (!turns.length) {
      log.append(h("div.empty", h("p", info.code
        ? `A conversation in ${info.folder}: the agent works on its files`
          + `${info.computer ? ` on ${info.computer.name}` : ""}.`
        : "A chat with one of Relay's agents."), h("p.faint", "Pick the agent and the mode above, then write.")));
      return;
    }
    turns.forEach((turn, index) => drawTurn(index, turn));
    settle(true);
  }

  function drawTurn(index, turn) {
    let view = views[index];
    if (!view || view.role !== turn.role || view.run !== turn.run) {
      const el = turn.role === "user" ? h("div.turn.user") : turn.role === "note" ? h("div.turn.note")
        : h("div.turn.assistant", h("div.blocks"), h("div.foot"));
      if (view) view.el.replaceWith(el);
      else log.append(el);
      view = views[index] = { el, role: turn.role, run: turn.run, blocks: [] };
    }
    if (turn.role === "user") {
      const idle = !info.running;
      view.el.replaceChildren(h("div.bubble", turn.text),
        ...turn.attachments.length ? [h("div.chips", turn.attachments.map((name) => h("span.chip.file", name)))] : [],
        h("div.actions",
          ...idle && turn.id ? [h("button.link", { title: "Change it and have it answered anew (what came "
            + "after it goes)", onclick: () => editing(view.el, turn) }, "Edit")] : [],
        h("button.link", { onclick: () => copy(turn.text) }, "Copy")));
      return;
    }
    if (turn.role === "note") {
      view.el.textContent = turn.text;
      return;
    }
    const live = !turn.status;
    const box = view.el.querySelector(".blocks");
    turn.blocks.forEach((block, i) => {
      let shown = view.blocks[i];
      if (!shown || shown.type !== block.type) {
        const made = { type: block.type, el: h("div.block") };
        if (shown) shown.el.replaceWith(made.el);
        else box.append(made.el);
        shown = view.blocks[i] = made;
      }
      update(shown, block, live);
    });
    const waiting = turn.blocks.some((b) => (b.type === "permission" && b.decision === null)
      || (b.type === "form" && b.status === "pending"));
    const foot = view.el.querySelector(".foot");
    const c = turn.changes;
    const said = turn.blocks.filter((b) => b.type === "text").map((b) => b.text).join("\n\n");
    foot.replaceChildren(live ? h("span.working", waiting ? "Waiting for your answer" : "Working")
      : h("span.faint", [turn.model, turn.used, turn.status === "interrupted" ? "Stopped"
        : turn.status === "failed" ? "Failed" : null].filter(Boolean).join(" · ")),
    ...!live ? [h("span.actions", ...!info.running ? [
      h("button.link", { title: "Answer that message again",
        onclick: () => send({ type: "retry", prompt: turn.prompt }) }, "Retry"),
      ...turn.id ? [h("button.link", { title: "A new conversation with the messages up to this answer",
        onclick: () => send({ type: "fork", id: turn.id }) }, "Fork")] : []] : [],
      ...said ? [h("button.link", { onclick: () => copy(said) }, "Copy")] : [])] : [],
    ...!live && c && c.files ? [h("div.turn-changes",
      h("button.link",
        { title: "Review the task's changes (Source Control)", onclick: () => send({ type: "changes" }) },
        `${c.files} file${c.files === 1 ? "" : "s"} `, h("span.add", `+${c.additions}`), " ",
        h("span.del", `−${c.deletions}`)),
      ...turn.undo ? [h("button.link", { title: "Make the files what they were before this answer",
        onclick: () => send({ type: "undoTurn", run: turn.run }) }, "Undo this answer")] : [])] : []);
    if (!live && !turn.blocks.length) box.replaceChildren(h("div.faint", "No answer."));
  }

  function update(shown, block, live) {
    const el = shown.el;
    switch (block.type) {
      case "text":
        if (shown.text === block.text && shown.html === block.html) return;
        shown.text = block.text;
        shown.html = block.html;
        el.className = "block md";
        if (block.html !== null && block.html !== undefined) el.replaceChildren(...safe(block.html));
        else el.replaceChildren(h("div.plain", block.text));
        copyButtons(el);
        fileLinks(el);
        return;
      case "reasoning":
        if (!shown.body) {
          shown.body = h("div.plain");
          el.className = "block thinking";
          el.replaceChildren(h("details", h("summary", "Thinking"), shown.body));
        }
        shown.body.textContent = block.text;
        return;
      case "tool": {
        if (!shown.summary) {
          shown.dot = h("span.dot");
          shown.summary = h("summary", shown.dot, h("span.name", block.name), h("span.what", within(block.what)));
          shown.output = h("pre.output");
          shown.links = h("div.links");
          el.className = "block tool";
          // (an edit's lines, red and green: what the tool changed)
          const diff = block.edit ? h("pre.diff", block.edit.map(([sign, line]) =>
            h(`span.${{ "+": "add", "-": "del", "@": "hunk" }[sign] || "same"}`,
              `${sign === "@" ? "" : sign} ${line}\n`))) : null;
          el.replaceChildren(h("details", shown.summary, shown.links, ...diff ? [diff] : [], shown.output));
        }
        shown.dot.className = `dot ${block.status}`;
        shown.dot.title = block.status;
        if (!block.kept) shown.output.textContent = block.output || (block.status === "running" ? "…" : "(no output)");
        shown.links.replaceChildren(block.path ? h("a", { href: "#", "data-file": block.path }, block.path) : "");
        return;
      }
      case "subagent":
        el.className = "block subagent";
        el.replaceChildren(h("span.dot", { class: block.status === "succeeded" ? "done" : block.status === "failed"
          ? "failed" : ["running", "waiting"].includes(block.status) ? "running" : "" }),
        h("span.name", `Subagent: ${block.label}`),
        h("span.what", [block.provider, block.model].filter(Boolean).join(" · ")),
        h("a", { href: "#", onclick: (event) => {
          event.preventDefault();
          event.stopPropagation();
          send({ type: "open", id: block.id });
        } }, "Open"));
        return;
      case "permission":
        permission(shown, block, live);
        return;
      case "form":
        form(shown, block, live);
        return;
      case "error":
        el.className = "block error";
        el.textContent = block.message;
        return;
      default:
        el.replaceChildren();
    }
  }

  // The open file (and its selection): a chip above the message, going along with it unless × takes it away
  function drawContext() {
    const shown = shownContext && shownContext.key !== dropped;
    context.replaceChildren(...shown ? [h("span.chip.file", { title: `${shownContext.title}: goes along with your `
      + "message (a selection with its lines)" }, icon("file"), h("span", shownContext.label),
    h("button.x", { title: "Not with this message", "aria-label": "Not with this message", onclick: () => {
      dropped = shownContext.key;
      context.replaceChildren();
    } }, "×"))] : []);
  }

  function copy(text) {
    navigator.clipboard.writeText(text).then(() => say("Copied.")).catch(() => {});
  }

  // Your message changed in its place, then answered anew
  function editing(el, turn) {
    const box = h("textarea.input", { value: turn.text, rows: Math.min(12, turn.text.split("\n").length + 1),
      "aria-label": "Your message, changed" });
    el.replaceChildren(box, h("div.row", h("span.faint", "What came after it goes"),
      h("button.secondary", { onclick: () => drawTurn(turns.indexOf(turn), turn) }, "Cancel"),
      h("button.primary", { onclick: () => send({ type: "edit", id: turn.id, text: box.value }) }, "Send")));
    box.focus();
  }

  // Something the agent asks you to allow: its choices while it waits, then what was decided
  function permission(shown, block, live) {
    const el = shown.el;
    clearInterval(shown.ticking);
    const what = String(block.what || "");
    if (block.decision === null && live && block.run) {
      el.className = "block ask";
      const answer = (decision) => {
        el.querySelectorAll("button").forEach((b) => { b.disabled = true; });
        send({ type: "permission", run: block.run, id: block.id, decision });
      };
      const buttons = block.options
        ? block.options.map((o, i) =>
            h(`button.${i === 0 ? "primary" : "secondary"}`, { onclick: () => answer(o.id) }, o.label))
        : [h("button.secondary", { onclick: () => answer("deny") }, "Deny"),
          h("button.primary", { onclick: () => answer("allow") }, "Allow")];
      const countdown = h("span.faint");
      el.replaceChildren(h("div.ask-title", `The agent asks to use ${block.action}`),
        ...what ? [h("pre.ask-what", what)] : [], h("div.row", countdown, ...buttons));
      if (block.expires) {
        const tick = () => {
          const left = Math.max(0, Math.round(block.expires - Date.now() / 1000));
          countdown.textContent = `No answer: denied in ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
        };
        tick();
        shown.ticking = setInterval(tick, 1000);
      }
      return;
    }
    const decided = block.decision === null ? "not answered" : block.outcome
      || (block.decision === "once" ? (block.asked ? "allowed" : "approved automatically")
        : ["denied", "timeout"].includes(block.decision) ? "denied" : "allowed");
    el.className = `block permission ${["denied", "not answered"].includes(decided) ? "no" : "ok"}`;
    const line = `Permission for ${block.action}${what ? `: ${what.split("\n")[0].slice(0, 100)}` : ""} — ${decided}`;
    el.replaceChildren(what.includes("\n") || what.length > 100
      ? h("details", h("summary", line), h("pre.ask-what", what)) : h("span", line));
  }

  // A question the agent asks with a form (Relay's forms: choices, your own answer, yes/no, numbers, text)
  function form(shown, block, live) {
    const pending = block.status === "pending";
    const active = pending && live && block.run;
    if (shown.status === block.status && shown.active === active && shown.controls) {
      refresh(shown, block, active);
      return;
    }
    shown.status = block.status;
    shown.active = active;
    clearInterval(shown.ticking);
    const el = shown.el;
    el.className = "block question";
    const group = `q-${block.id}`;
    shown.controls = (block.fields || []).map((field, index) => control(field, index, block, group));
    shown.error = h("div.error-text", { role: "alert" });
    shown.state = h("div.faint");
    shown.buttons = h("div.row",
      h("button.secondary", { type: "button", onclick: () => answer(null) }, "Dismiss"),
      h("button.primary", { type: "submit" }, "Send answers"));
    const answer = (value) => {
      shown.buttons.querySelectorAll("button").forEach((b) => { b.disabled = true; });
      send({ type: "form", run: block.run, id: block.id, answer: value });
    };
    const formEl = h("form", { oninput: () => refresh(shown, block, active), onsubmit: (event) => {
      event.preventDefault();
      const values = answers(shown);
      for (const c of shown.controls.filter((c) => !c.el.hidden)) {
        const value = values[c.field.key];
        const count = Array.isArray(value) ? value.length : value === "" || value === undefined ? 0 : 1;
        if ((c.field.required && !count) || count < (c.field.minItems || 0)
            || (c.field.maxItems !== undefined && count > c.field.maxItems)) {
          shown.error.textContent = `Check your answer to “${c.field.title || c.field.key}”.`;
          return;
        }
      }
      shown.error.textContent = "";
      answer(values);
    } }, shown.controls.map((c) => c.el), shown.error, shown.state, shown.buttons);
    el.replaceChildren(h("div.ask-title", block.title || "Questions"), formEl);
    refresh(shown, block, active);
  }

  function control(field, index, block, group) {
    const value = block.answer?.[field.key] ?? field.default;
    const el = h("fieldset.field", h("legend", field.title || field.key),
      field.description ? h("div.faint", field.description) : null);
    let read;
    if (field.options) {
      const multiple = field.type === "multiselect" && field.maxItems !== 1;
      const selected = Array.isArray(value) ? value : value === undefined ? [] : [value];
      const choices = field.options.map((option) => {
        const choice = h("input", { type: multiple ? "checkbox" : "radio", name: `${group}-${index}`,
          value: option.value, checked: selected.includes(option.value) });
        el.append(h("label.choice", choice, h("span", h("span", option.label),
          option.description ? h("small.faint", option.description) : null)));
        return choice;
      });
      let custom, customChoice;
      if (field.custom) {
        const other = selected.filter((v) => !field.options.some((o) => o.value === v));
        custom = h("input.input", { placeholder: "Type your own answer", value: other.join(", "),
          "aria-label": `${field.title || field.key}: your own answer` });
        customChoice = h("input", { type: multiple ? "checkbox" : "radio", name: `${group}-${index}`,
          checked: other.length > 0 });
        custom.addEventListener("input", () => { customChoice.checked = !!custom.value.trim(); });
        el.append(h("label.choice", customChoice, h("span", "Your own answer", custom)));
      }
      read = () => {
        const picked = choices.filter((c) => c.checked).map((c) => c.value);
        if (customChoice?.checked && custom.value.trim()) picked.push(custom.value.trim());
        return field.type === "multiselect" ? picked : picked[0] ?? "";
      };
    } else if (field.type === "external") {
      el.append(h("a", { href: field.url }, "Open the form"));
      read = () => undefined;
    } else {
      const numeric = ["number", "integer"].includes(field.type);
      const type = field.type === "boolean" ? "checkbox" : numeric ? "number"
        : { email: "email", uri: "url", date: "date", "date-time": "datetime-local" }[field.format] || "text";
      const box = h("input.input", { type, value: field.type === "boolean" ? undefined : value ?? "",
        checked: field.type === "boolean" && value === true, placeholder: field.placeholder,
        min: field.minimum, max: field.maximum, step: field.type === "integer" ? 1 : numeric ? "any" : undefined,
        minLength: field.minLength, maxLength: field.maxLength, pattern: field.pattern,
        required: field.type !== "boolean" && field.required, "aria-label": field.title || field.key });
      el.append(box);
      read = () => field.type === "boolean" ? box.checked
        : numeric ? (box.value === "" ? undefined : Number(box.value)) : box.value;
    }
    return { field, el, read };
  }

  function answers(shown) {
    return Object.fromEntries(shown.controls.filter((c) => !c.el.hidden)
      .map((c) => [c.field.key, c.read()]).filter(([, value]) => value !== undefined));
  }

  function refresh(shown, block, active) {
    clearInterval(shown.ticking);
    const values = Object.fromEntries(shown.controls.map((c) => [c.field.key, c.read()]));
    const pending = block.status === "pending";
    shown.el.classList.toggle("ask", !!active);
    for (const c of shown.controls) {
      c.el.hidden = !!c.field.hidden || (c.field.when || []).some((condition) =>
        condition.op === "eq" ? values[condition.key] !== condition.value : values[condition.key] === condition.value);
      c.el.disabled = c.el.hidden || !active;
    }
    shown.buttons.hidden = !active;
    const said = () => {
      if (!pending) {
        shown.state.textContent = block.status === "answered" ? "Answers sent" : block.message || "Question dismissed";
      } else if (!active) {
        shown.state.textContent = "Not answered";
      } else if (block.expires) {
        const seconds = Math.max(0, Math.ceil(block.expires - Date.now() / 1000));
        shown.state.textContent = seconds ? `Waiting for your answers · dismissed in ${Math.floor(seconds / 60)}:`
          + `${String(seconds % 60).padStart(2, "0")}` : "Dismissing the unanswered question…";
      } else {
        shown.state.textContent = "Waiting for your answers";
      }
    };
    said();
    if (active && block.expires) shown.ticking = setInterval(said, 1000);
  }

  // VSCodium's own questions before an agent's command or change on this computer: as Relay's, answered here
  let askTicks = null;
  function asksOf(items) {
    clearInterval(askTicks);
    const ticks = [];
    asks.replaceChildren(...items.map((item) => {
      const answer = (decision) => send({ type: "localAnswer", id: item.id, decision });
      const countdown = h("span.faint");
      ticks.push(() => {
        const left = Math.max(0, Math.round(item.expires - Date.now() / 1000));
        countdown.textContent = `No answer: denied in ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
      });
      return h("div.block.ask", h("div.ask-title", `The agent asks to ${item.action} on this computer`),
        h("pre.ask-what", item.what), h("div.row", countdown,
          ...item.change ? [h("button.secondary", { onclick: () => send({ type: "localChange", id: item.id }) },
            "Show the change")] : [],
          h("button.primary", { onclick: () => answer("once") }, "Allow"),
          ...item.run ? [h("button.secondary", { onclick: () => answer("answer") }, "Allow for this answer")] : [],
          h("button.secondary", { onclick: () => answer("denied") }, "Deny")));
    }));
    const tick = () => ticks.forEach((t) => t());
    tick();
    if (items.length) askTicks = setInterval(tick, 1000);
  }

  function chipsOf(items) {
    chips.replaceChildren(...items.map((item) => h("span.chip.file", item.name,
      h("button.x", { title: "Remove", "aria-label": `Remove ${item.name}`,
        onclick: () => send({ type: "unattach", id: item.id }) }, "×"))));
  }

  function say(message, kind) {
    notice.hidden = !message;
    notice.className = `notice ${kind || ""}`;
    notice.replaceChildren(h("span", message || ""), h("button.x", { title: "Close", "aria-label": "Close",
      onclick: () => { notice.hidden = true; } }, "×"));
  }

  window.addEventListener("message", (event) => {
    const message = event.data || {};
    switch (message.type) {
      case "conversation":
        info = message.info;
        // (kept by VSCodium with the tab: after a reload of the window, the same chat in it)
        vscode.setState({ id: info.id || null, folder: info.id ? null : info.folderPath || null });
        header();
        if (!turns.length) drawAll();
        break;
      case "turns": {
        turns = message.turns;
        drawAll();
        break;
      }
      case "turn": {
        const follow = nearBottom();
        turns[message.index] = message.turn;
        drawTurn(message.index, message.turn);
        settle(follow);
        break;
      }
      case "attachments":
        chipsOf(message.items || []);
        break;
      case "asks":
        asksOf(message.items || []);
        break;
      case "context":
        shownContext = message.context;
        drawContext();
        break;
      case "completions":
        if (word()?.query === message.query) offer(message.items || []);
        break;
      case "insert":
        input.value += (input.value && !input.value.endsWith("\n") ? "\n" : "") + message.text;
        grow();
        input.focus();
        break;
      case "sent":
        input.value = "";
        grow();
        say("");
        dropped = null;  // (× was for that message: the open file goes along with the next again)
        drawContext();
        break;
      case "error":
        say(message.message, "error");
        break;
      case "notice":
        say(message.message);
        break;
      case "signedOut":
        info = { title: "Relay" };
        header();
        log.replaceChildren(h("div.empty", h("p", "Not signed in to Relay."),
          h("button.primary", { onclick: () => send({ type: "signIn" }) }, "Sign in")));
        break;
      default:
        break;
    }
  });

  header();
  send({ type: "ready" });
  input.focus();
})();
