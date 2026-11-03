// A live service (a ProductionBox, relay.production) where a chat would be: agents don't work there, so it's
// its overview (health and its last week, versions, WorkBoxes), a shell in its live box and its log, and Demote
// to clone it into a WorkBox of your own.

import { relayUrl } from "./base.js";
import { api } from "./api.js";
import { pickFolder } from "./picker.js";
import { eligibleProviders, provider, state } from "./state.js";
import { size } from "./system.js";
import { mountTerminal } from "./terminal.js";
import { clear, confirmDialog, h, icon, modal, shortPath, statusDot, timeAgo, toast } from "./ui.js";

// A new service (a ProductionBox): from the AgentOS page, or /promote in a code task (`conversationId`),
// which then goes on as its WorkBox. Resolves with the service made, or null. Code that doesn't keep its data
// in $DATA_DIR is said to lose it at its next version; `ask(text)` has the task's agent see to that first.
export function serviceDialog({ folder = null, conversationId = null, ask = null } = {}) {
  return new Promise((resolve) => {
    let made = null;
    const m = modal(conversationId ? "Make a ProductionBox" : "New service", { onClose: () => resolve(made) });
    const data = { folder };
    const name = h("input.input", { placeholder: "e.g. electionatlas", "aria-label": "Name" });
    const command =
      h("input.input.mono", { placeholder: "e.g. npm start  (it listens on $PORT, keeps data in $DATA_DIR)",
        "aria-label": "Starts with" });
    const port =
      h("input.input.num-short", { type: "number", min: 1024, max: 65535, value: 8500, "aria-label": "Port" });
    const health = h("input.input.mono", { placeholder: "/health  (optional: else a connection is enough)",
      "aria-label": "Health check" });
    const managed = h("input", { type: "checkbox" });
    const dataNote = h("div.data-note", { hidden: true });
    const guess = async () => {
      if (!data.folder) return;
      try {
        const found = await api.get(`/api/services/guess?folder=${encodeURIComponent(data.folder)}`);
        if (!name.value) name.value = found.name;
        if (!command.value) command.value = found.command;
        port.value = found.port;
        clear(dataNote, found.data_dir ? [] : [
          h("p", "Its code doesn't use $DATA_DIR. If it keeps data (a database, uploads) next to its code, that "
            + "stays behind with this version: the next one starts without it."),
          ask ? h("button.btn.small", { type: "button", onclick: () => {
            ask("Before this goes live as a service: have it listen on $PORT and keep all of its data (a database, "
              + "uploads) in $DATA_DIR, which every version of the service shares. A version gets only this folder, "
              + "not what's installed in your box: keep its dependencies in the folder (node_modules, a .venv) or "
              + "have its start command install them. Try it as it will run (PORT=18080 DATA_DIR=.data and its start "
              + "command, from a copy of the folder). Then I'll make it live.");
            m.close();
          } }, "Ask the agent to see to it first") : null]);
        dataNote.hidden = Boolean(found.data_dir);
      } catch { /* (only a head start) */ }
    };
    const folderButton = h("button.picker", { type: "button", disabled: Boolean(conversationId), onclick: async () => {
      const chosen = await pickFolder(data.folder);
      if (chosen) { data.folder = chosen; folderButton.querySelector("span").textContent = shortPath(chosen); guess(); }
    } }, icon("folder", "sm"), h("span.truncate", folder ? shortPath(folder) : "Choose its project folder"));
    const save = h("button.btn.primary", { onclick: async () => {
      save.disabled = true;
      save.textContent = "Starting…";
      try {
        made = await api.post("/api/services", { name: name.value, folder: data.folder, command: command.value,
          port: Number(port.value), health: health.value, managed: managed.checked, conversation_id: conversationId });
        toast(made.live ? `${made.name} is live at /port/${made.public_port}/`
          : `${made.name} was made, but didn't come up well: see its log`, made.live ? "info" : "error");
        m.close();
      } catch (error) {
        toast(error.message, "error");
        save.disabled = false;
        save.textContent = "Make it live";
      }
    } }, "Make it live");
    m.body.append(
      h("p.muted", "Its code is copied from the project folder as version 1 and runs in a ProductionBox; Relay reaches "
        + "it at the port you choose." + (conversationId ? " This task goes on as its WorkBox: /promote makes what you "
          + "change here its next version."
          : " To change it: Demote on its page (a WorkBox for an agent), then Promote "
          + "to live.")),
      h("label.field", h("span", "Name"), name),
      h("div.field", h("span", "Code"), folderButton),
      h("label.field", h("span", "Starts with", h("span.hint.block", "It listens on $PORT and keeps its data in "
        + "$DATA_DIR, which every version shares")), command),
      dataNote,
      h("div.field", h("span", "Port"), h("div.row-inline", port, h("span.muted", "Relay listens here for it"))),
      h("label.field", h("span", "Health check"), health),
      h("label.check-row", managed,
        h("span", "Let AgentOS look after it: its Governor sees it, is woken when it fails, and "
          + "may clone, promote and roll it back. Without this it's your own, and the Governor doesn't know of it.")));
    m.foot.append(h("button.btn", { onclick: () => m.close() }, "Cancel"), save);
    guess();
    name.focus();
  });
}

// A service's light: green when it's well; red when it's failing, or its version didn't come up (a simulated user
// saw one that never came up shown green); grey when it's stopped or not checked yet.
export function healthDot(s, title = null) {
  if (!s.live && !s.stopped && s.versions?.some((v) => v.status === "failed")) {
    return statusDot("fail", title || "Nothing live: its version didn't come up (see its log)");
  }
  if (s.stopped || !s.live || s.health_ok == null) {
    const said = title || (s.stopped ? "Stopped" : !s.live ? "Nothing live" : "Not checked yet");
    return h("span.status-dot", { title: said, "aria-label": said });
  }
  return statusDot(s.health_ok ? "ok" : "fail",
    title || (s.health_ok ? `Well (${s.health_detail})` : `Failing: ${s.health_detail}`));
}

// Whether AgentOS looks after a service (its Governor sees it and may change it), or it's your own.
export function managedToggle(s, act, after) {
  const button = h(`button.btn.small.agentos-toggle${s.managed ? ".managed" : ""}`, {
    "aria-pressed": String(s.managed),
    title: s.managed ? "AgentOS looks after it: its Governor sees it, is woken when it fails, and may clone, promote "
      + "and roll it back. Click to make it your own." : "Your own: AgentOS's Governor doesn't see it, and you're told "
      + "when it fails. Click to let AgentOS look after it.",
  }, icon("compass", "sm"), s.managed ? "Managed by AgentOS" : "Private");
  button.onclick = act(button, async () => {
    await api.patch(`/api/services/${s.id}`, { managed: !s.managed });
    toast(s.managed ? `${s.name} is your own now: the Governor doesn't see it` : `AgentOS looks after ${s.name} now`);
    await after();
  });
  return button;
}

// A service's page (/s/<id>): its head (what you can do), and Overview (health and its last week, the live
// version, data, every version to roll back to, its WorkBoxes, how it runs), Shell (in its live box, connected
// when it's opened) and Log.
export function serviceView(id, { onOpen, onChanged, onDeleted }) {
  const el = h("div.service-view");
  let shell = null;
  let closed = false;
  let service = null;
  let detail = null;  // its box, processes and last week (/status: podman asked), fetched when it changed
  let shown = "";  // what the head and Overview show: drawn anew only when that changes (focus, a click in progress)
  let tab = "overview";
  const top = h("div.service-top");
  const panes = { overview: h("div.service-pane.service-overview"), shell: h("div.service-pane.service-shell"),
    log: h("div.service-pane.service-log") };
  const tabButtons = Object.fromEntries([["overview", "Overview", "info"], ["shell", "Shell", "terminal"],
    ["log", "Log", "file"]].map(([key, label, iconName]) => [key, h("button.service-tab", {
    type: "button", role: "tab", onclick: () => select(key) }, icon(iconName, "sm"), label)]));

  const act = (button, run) => async () => {
    button.disabled = true;
    try {
      await run();
    } catch (error) {
      toast(error.message, "error");
    } finally {
      button.disabled = false;
    }
  };

  function select(key) {
    tab = key;
    for (const [name, button] of Object.entries(tabButtons)) {
      button.setAttribute("aria-selected", String(name === key));
      panes[name].hidden = name !== key;
    }
    if (key === "shell" && !shell) openShell();
    if (key === "log") loadLog();
  }

  function head() {
    const s = service;
    const address = `${location.origin}${relayUrl(`/port/${s.public_port}/`)}`;  // (through Relay: relay.forward)
    const health = s.stopped ? "stopped (not checked)" : !s.live ? "nothing to check"
      : s.health_ok === false ? `failing: ${s.health_detail}` : s.health_ok ? "well" : "not checked yet";
    const demote = h("button.btn.primary.small", { title: "A copy of the live version in a WorkBox, for an agent to "
      + "work in (Promote to live in its chat when it's right); this one stays live meanwhile" },
      icon("code", "sm"), "Demote");
    demote.onclick = act(demote, async () => {
      // The agent of a new conversation, when it can work in a folder; else the first that can.
      const mine = provider(state.draft.providerId)?.tools;
      const providerId = mine ? state.draft.providerId : eligibleProviders("code")[0]?.id;
      if (!providerId) throw new Error("A WorkBox needs a provider with tools");
      const conv = await api.post(`/api/services/${s.id}/demote`,
        { provider_id: providerId, model: mine ? state.draft.model || null : null });
      toast(`A WorkBox of ${s.name} version ${s.live.number}: tell the agent what to change`);
      onOpen(conv.id);
    });
    const restart = h("button.btn.small", {}, icon("refresh", "sm"), "Restart");
    // Stopped: not served or checked, its versions and data kept; started again as it was
    const onOff = h("button.btn.small", { title: s.stopped ? "Serve it again (its live version, checked first)"
      : "Take it off the air: its address answers nothing; its versions and data are kept" },
    icon(s.stopped ? "send" : "stop", "sm"), s.stopped ? "Start" : "Stop");
    onOff.onclick = act(onOff, async () => {
      toast((await api.post(`/api/services/${s.id}/${s.stopped ? "start" : "stop"}`)).message);
      await refresh(true);
    });
    const agentos = managedToggle(s, act, refresh);
    restart.onclick = act(restart, async () => {
      toast((await api.post(`/api/services/${s.id}/restart`)).message);
      await refresh(true);
    });
    const remove = h("button.icon-btn.sm.danger", { title: "Delete this service (its versions, archive and data)" },
      icon("trash", "sm"));
    remove.onclick = act(remove, async () => {
      if (!await confirmDialog(`Delete ${s.name}?`, "It stops, and its versions, archive and data go for good. "
        + "Its WorkBoxes stay.", { confirm: "Delete with its data", danger: true })) return;
      toast(`Deleting ${s.name}…`);
      await api.del(`/api/services/${s.id}`);
      toast(`${s.name} is deleted`);
      onDeleted?.();
    });
    return h("div.service-head",
      h("div.pane-head",
        h("div.grow",
          h("h1", icon("box"), s.name),
          h("p.muted.service-meta",
            healthDot(s, s.live ? `Health: ${health}` : null),
            s.live ? `version ${s.live.number} live` : "nothing live", " · ",
            h("a", { href: address, target: "_blank", rel: "noopener" }, `/port/${s.public_port}/`), ` · ${health}`)),
        h("div.actions", agentos, s.live ? demote : null, s.stopped || !s.live ? null : restart, s.live ? onOff : null,
          remove)));
  }

  // What it is now and its last week, at a glance; then every version, its WorkBoxes and how it runs
  function overview() {
    const s = service;
    const box = detail?.box;
    const history = detail?.history;
    // (not "…" for good: a service that never came up has no box to ask, and asking can fail)
    const unasked = !s.live ? "nothing live" : detail?.error ? `couldn't ask: ${detail.error}` : null;
    const incidents = incidentsOf(history?.changes || [], s, s.versions);
    const down = incidents.reduce((total, i) => total + ((i.until || Date.now() / 1000) - i.at), 0);
    const sizes = history?.data || [];
    const latest = sizes.at(-1);
    const live = s.versions.find((v) => v.status === "live");
    const tile = (label, value, sub, tone) => h(`div.os-tile${tone ? `.${tone}` : ""}`,
      h("span.os-tile-label", label), h("span.os-tile-value", value), h("span.os-tile-sub", { title: sub }, sub));
    const tiles = h("div.os-tiles.service-tiles",
      tile("Health", s.stopped ? "Stopped" : s.health_ok === false ? "Failing" : s.health_ok ? "Well" : "Not checked",
        s.stopped ? "not served or checked" : !s.live ? "nothing live to check"
          : `${s.health_detail || "no check yet"}${s.health_at
            ? ` · ${timeAgo(s.health_at)}` : ""}`,
            s.stopped ? "muted" : s.health_ok === false ? "bad" : s.health_ok ? "ok" : "muted"),
      tile("Live", live ? `v${live.number}` : "—",
        live ? `made live by ${by(live)} ${timeAgo(live.promoted_at || live.created)}`
        : "nothing live yet", live ? null : "muted"),
      tile("Incidents, 7 days", history ? String(incidents.length) : unasked ? "—" : "…", history ? (incidents.length
        ? `down ${span(down)} in all` : "well all week") : unasked || "asking its box",
        incidents.length ? "warn" : null),
      tile("Data", latest ? size(latest.bytes) : "—", latest ? (sizes.length > 1
        ? `${growth(sizes[0].bytes, latest.bytes)} since ${day(sizes[0].day)}` : `measured ${day(latest.day)}`)
        : "measured once a day", null),
      tile("Box", box ? box.status || "?" : unasked ? "—" : "…", box ? boxLine(box) : unasked || "asking podman",
        box && box.status !== "running" && !s.stopped ? "bad" : null));
    const refreshed = h("button.btn.small", { title: "Ask its box again (its processes, a health check now)" },
      icon("refresh", "sm"), "Refresh");
    refreshed.onclick = act(refreshed, () => refresh(true));
    return [tiles,
      section(["Health, last 7 days", refreshed], incidents.length ? h("ul.service-incidents", incidents.map((i) =>
        h(`li${i.until ? "" : ".ongoing"}`, h("span.when", clockDay(i.at)),
          h("span", i.until ? ` failing for ${span(i.until - i.at)}` : " failing since then",
            i.detail ? `: ${i.detail}` : ""),
          i.fixed ? h("span.muted", ` · ${i.fixed}`) : null)))
        : h("p.muted.small-text",
            history ? `No failed health checks${history.kept_since && history.kept_since > history.from
            ? ` (Relay has kept them since ${clockDay(history.kept_since)})` : ""}.`
          : !s.live ? "Nothing live to check." : detail?.error ? `Couldn't ask its box: ${detail.error}` : "…")),
      section("Versions", h("div.service-versions", s.versions.map(versionRow))),
      section("Its data", h("p.muted.small-text", "In $DATA_DIR (/data), shared by every version. "
        + "A copy is made before each new version and once a day (Settings → Limits → Services); "
        + "putting one back keeps the data as it is first, so that can be put back too."), s.snapshots?.length
        ? h("div.service-snapshots", s.snapshots.map(snapshotRow)) : h("p.muted.small-text", "No copies yet.")),
      s.workboxes?.length ? section("WorkBoxes", h("div.service-workboxes", s.workboxes.map((w) =>
        h("button.chip",
          { title: w.running ? "An agent is working in it" : "Open its chat", onclick: () => onOpen(w.id) },
          icon("code", "sm"), h("span.truncate", w.title),
          w.from_version ? h("span.muted", `from v${w.from_version}`) : null,
          w.running ? h("span.activity-dot") : null))),
        h("p.muted.small-text",
          "Each is a code task with a copy of a version: Promote to live in its chat makes it the "
          + "next one.")) : null,
      section("How it runs", h("dl.service-facts",
        h("dt", "Starts with"), h("dd", h("code", s.command)),
        h("dt", "Health check"), h("dd", s.health ? h("code", s.health) : "a connection is enough"),
        h("dt", "Address"), h("dd", `port ${s.public_port} on this machine, and /port/${s.public_port}/ through Relay`),
        h("dt", "Its data"), h("dd", "$DATA_DIR (/data), shared by every version; a snapshot before each new one"),
        h("dt", "Looked after"), h("dd", s.managed ? "by AgentOS: its Governor is woken when it fails, and may clone, "
          + "promote and roll it back" : "by you: the Governor doesn't know of it")),
      detail?.processes?.length ? h("details.service-processes", h("summary", "Processes in its box"),
        h("pre.log", detail.processes.join("\n"))) : null)];
  }

  function versionRow(v) {
    const row = h(`div.service-version.${v.status}`,
      h(`span.os-version.${v.status}`, `v${v.number} ${v.status}`),
      h("div.service-version-main",
        h("div", v.promoted_at ? `Made live by ${by(v)} ${clockDay(v.promoted_at)}` : `Made ${clockDay(v.created)}`,
          v.origin_title ? [" from ", v.origin_conversation ? h("a", { href: relayUrl(`/c/${v.origin_conversation}`),
            onclick: (e) => { e.preventDefault(); onOpen(v.origin_conversation); } }, v.origin_title) : v.origin_title]
            : v.note && v.status !== "failed" ? ` (${v.note.split("\n")[0]})` : ""),
        h("div.muted.small-text", checkLine(v))));
    if (v.status === "archived") {
      const back = h("button.btn.small", { title: `Version ${v.number} takes over again, once it's checked` },
        icon("undo", "sm"), "Roll back to this");
      back.onclick = act(back, async () => {
        if (!await confirmDialog(`Roll back to version ${v.number}?`, `Version ${v.number} takes over again (once it's `
          + "checked); the live one is archived. The data stays as it is.", { confirm: "Roll back" })) return;
        toast((await api.post(`/api/services/${service.id}/rollback`, { version: v.number })).message);
        await refresh(true);
      });
      row.append(back);
    } else if (v.status === "failed" && v.note) {
      row.append(h("button.btn.small", { onclick: () => {
        const m = modal(`Why version ${v.number} didn't come up`, { wide: true });
        m.body.append(h("pre.log", v.note));
      } }, icon("info", "sm"), "Why"));
    }
    return row;
  }

  function snapshotRow(snap) {
    const label = snap.kind === "version" ? `Before version ${snap.version}` : snap.kind === "daily" ? "Daily"
      : snap.kind === "restore" ? "Before a put back" : snap.name;
    const back = h("button.btn.small", { title: `Its data as it was then; the data now is kept first` },
      icon("undo", "sm"), "Put back");
    back.onclick = act(back, async () => {
      if (!await confirmDialog(`Put back ${label.toLowerCase()} (${clockDay(snap.at)})?`, `${service.name}'s data `
        + `becomes as it was then. Its box stops for that moment; the data as it is now is kept as a copy first, so `
        + "you can put that back.", { confirm: "Put back" })) return;
      toast((await api.post(`/api/services/${service.id}/restore`, { snapshot: snap.name })).message);
      await refresh(true);
    });
    return h("div.service-snapshot", h("span.service-snapshot-label", label),
      h("span.muted.small-text", `${clockDay(snap.at)} · ${size(snap.bytes)}`), back);
  }

  async function loadLog() {
    const pre = panes.log.querySelector("pre") || h("pre.log.service-log-text", "…");
    if (!pre.isConnected) {
      const again = h("button.btn.small", {}, icon("refresh", "sm"), "Refresh");
      again.onclick = act(again, loadLog);
      clear(panes.log, h("div.service-pane-head", h("span.muted.small-text",
        "The last 200 lines of its live version (or why the last one didn't come up)"), again), pre);
    }
    try {
      pre.textContent = (await api.get(`/api/services/${id}/logs`)).logs;
      pre.scrollTop = pre.scrollHeight;
    } catch (error) {
      pre.textContent = error.message;
    }
  }

  function openShell() {
    const status = h("span.shell-status", { role: "status" }, "Connecting to its box…");
    const host = h("div.shell-terminal", { "aria-label": `Shell in ${service.name}'s ProductionBox` });
    const again = h("button.btn.small", { type: "button", hidden: true, onclick: () => {
      shell.close();
      shell = null;
      openShell();
    } }, "New shell");
    shell = mountTerminal(host, `/api/services/${id}/terminal`, status, {
      connected: "A shell in the live ProductionBox; leaving the page ends it, not the service.",
      onEnd: () => { again.hidden = false; } });
    clear(panes.shell, host, h("div.service-foot", status, h("span.muted.service-note",
      "Code changes here are gone after the next promote; /data stays."), shell.keys, again));
  }

  async function refresh(ask = false) {
    let fresh;
    try {
      fresh = await api.get(`/api/services/${id}`);
    } catch (error) {
      if (!service) clear(el, h("p.error-text", error.message));
      return;
    }
    if (closed) return;
    // (its box is asked again when what it is changed, not every 10 s)
    const changed = ask || !detail
      || ["health_ok", "live_version", "stopped"].some((key) => fresh[key] !== service?.[key]);
    service = fresh;
    onChanged?.(service);
    if (!el.contains(top)) {
      clear(el, top,
        h("div.service-tabs", { role: "tablist", "aria-label": `${service.name}` }, Object.values(tabButtons)),
        ...Object.values(panes));
      select(tab);
    }
    if (!service.live) detail = null;  // (nothing live: no box to ask)
    else if (changed) {
      try {
        detail = await api.get(`/api/services/${id}/status`);
      } catch (error) {
        detail = { error: error.message };  // (said in its tiles; the head and versions still show)
      }
      if (closed) return;
    }
    const now = JSON.stringify([service, detail?.history, detail?.box]);
    if (now !== shown) {
      shown = now;
      clear(top, head());
      clear(panes.overview, overview());
    }
  }

  refresh();
  // Its health, versions and WorkBoxes (Demote, the Governor's), as they change
  const timer = setInterval(() => { if (!document.hidden) refresh(); }, 10000);
  return {
    el,
    refresh,
    close() {
      closed = true;
      clearInterval(timer);
      shell?.close();
    },
  };
}

// A section of the Overview: its title (with an action beside it: [title, button]) and what's in it
function section(title, ...content) {
  const [name, action] = Array.isArray(title) ? title : [title, null];
  return h("section.service-section", h("div.service-section-head", h("h2.section-title", name), action), content);
}

// Its failing spells (from the changes Relay kept): when, how long, why, and what made it well again. One kept
// without its end (before a version going live said so) ended when the next version went live, if one did.
function incidentsOf(changes, service, versions = []) {
  const found = [];
  let open = null;
  for (const change of changes) {
    if (!change.ok && !open) open = { at: change.at, detail: change.detail };
    else if (change.ok && open) {
      found.push({ ...open, until: change.at,
        fixed: /^version \d+ live/.test(change.detail || "") ? change.detail : null });
      open = null;
    }
  }
  if (open && service.health_ok) {
    const next = versions.filter((v) => v.promoted_at > open.at).sort((a, b) => a.promoted_at - b.promoted_at)[0];
    found.push(next ? { ...open, until: next.promoted_at, fixed: `version ${next.number} live` }
      : { ...open, until: service.health_at || open.at });
  } else if (open) {
    found.push(open);
  }
  return found.reverse();
}

function by(version) {
  return version.promoted_by?.startsWith("governor") ? "the Governor" : "you";
}

function checkLine(version) {
  const parts = [];
  try {
    const check = version.validation ? JSON.parse(version.validation) : null;
    if (check) parts.push(`Relay's check: ${check.ok ? "well" : "not well"} (${check.detail})`);
    const agents = version.agents ? JSON.parse(version.agents) : [];
    if (agents.length) parts.push(`worked on by ${agents.map((a) => a.model || a.provider).join(", ")}`);
  } catch { /* (an older version without these) */ }
  const why = version.status === "failed" && version.note ? version.note.split("\n")[0] : "";
  if (why && !parts.some((part) => part.includes(why))) parts.push(why);
  if (version.status === "archived" && version.archived) parts.push(`archived ${timeAgo(version.archived)}`);
  return parts.join(" · ");
}

function boxLine(box) {
  const parts = [];
  if (box.started_at && box.status === "running") parts.push(`since ${timeAgo(Date.parse(box.started_at) / 1000)}`);
  if (box.restarts) parts.push(`${box.restarts} restart${box.restarts === 1 ? "" : "s"}`);
  if (box.oom_killed) parts.push("out of memory once");
  if (box.status !== "running" && box.exit_code) parts.push(`exit ${box.exit_code}`);
  return parts.join(" · ") || "never restarted";
}

function span(seconds) {
  if (seconds < 90) return `${Math.max(1, Math.round(seconds))} s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min`;
  if (seconds < 172800) return `${Math.round(seconds / 3600)} h`;
  return `${Math.round(seconds / 86400)} days`;
}

function clockDay(seconds) {
  const date = new Date(seconds * 1000);
  const time = date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return date.toDateString() === new Date().toDateString() ? `today ${time}`
    : `${date.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" })} ${time}`;
}

function day(text) {
  const date = new Date(`${text}T12:00:00`);
  return date.toDateString() === new Date().toDateString() ? "today"
    : date.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
}

function growth(before, now) {
  if (!before) return `${size(now)} new`;
  const change = (now - before) / before;
  return Math.abs(change) < 0.01 ? "about the same" : `${change > 0 ? "+" : "−"}${Math.round(Math.abs(change) * 100)}%`;
}
