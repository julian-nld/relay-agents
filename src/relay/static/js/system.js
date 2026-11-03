// Settings → System (relay/system.py): what Relay runs on this server and what it takes: the machine,
// Relay's boxes (what each is for, CPU and memory now; stop an idle one, remove a stopped WorkBox),
// and storage (podman's images: remove one nothing uses). It looks again every few seconds while open.

import { api } from "./api.js";
import { relayUrl } from "./base.js";
import { clear, confirmDialog, h, icon, statusDot, timeAgo, toast } from "./ui.js";

const EVERY = 10 * 1000;
const GROUPS = [
  ["provider", "Providers' boxes", "Each provider's own program. Stopped, it starts again with its next run."],
  ["workbox", "WorkBoxes", "Where agents' commands run: one per project folder or chat. A stopped one keeps what was "
    + "installed in it; removed, that goes (the folder stays)."],
  ["production", "ProductionBoxes", "Services' versions: their service starts and stops them (Production boxes)."],
  ["vpn", "VPN tunnels",
    "A tunnel's network and its connection (gluetun). Stopped, its first WorkBox used again starts it."],
  ["web", "Web search and fetch", "They start with the next search or fetch."],
  ["other", "Other", "Relay's, but not one of the above."],
];

export function size(bytes) {
  if (bytes === null || bytes === undefined) return "–";
  const units = [["GB", 1e9], ["MB", 1e6], ["KB", 1e3]];
  for (const [unit, at] of units) if (bytes >= at) return `${(bytes / at).toFixed(bytes >= 10 * at ? 0 : 1)} ${unit}`;
  return `${bytes} B`;
}

function since(seconds) {
  const passed = Math.max(0, Date.now() / 1000 - seconds);
  if (passed < 3600) return `${Math.max(1, Math.round(passed / 60))} min`;
  if (passed < 86400) return `${Math.round(passed / 3600)} h`;
  return `${Math.round(passed / 86400)} d`;
}

function meter(fraction, label, detail) {
  const used = Math.max(0, Math.min(1, fraction));
  const level = used > 0.9 ? ".bad" : used > 0.75 ? ".warn" : "";
  const fill = h("span");
  fill.style.width = `${Math.round(used * 100)}%`;  // (a property: the page's CSP refuses style attributes)
  return h(`div.limit-window.system-card${level}`, h("div.name", label),
    h("div.limit-meter", fill), h("div.sub", detail));
}

function machineCards(m) {
  const memoryUsed = m.memory.total - m.memory.available;
  const swapUsed = m.swap.total - m.swap.free;
  const diskUsed = m.disk.total - m.disk.free;
  return h("div.system-cards",
    meter(memoryUsed / (m.memory.total || 1), "Memory",
      `${size(memoryUsed)} of ${size(m.memory.total)} in use`
        + (m.swap.total ? ` · swap ${size(swapUsed)} of ${size(m.swap.total)}` : "")),
    meter(m.load[0] / m.cores, "Processor",
      `load ${m.load.map((n) => n.toFixed(2)).join(" · ")} (1, 5, 15 min) on `
      + `${m.cores} core${m.cores === 1 ? "" : "s"}`),
    meter(diskUsed / (m.disk.total || 1), "Disk",
      `${size(m.disk.free)} free of ${size(m.disk.total)} (${m.disk.path})`),
    h("div.limit-window.system-card", h("div.name", "Relay itself"),
      h("div.sub",
        `${size(m.relay)} of memory`
        + (m.uptime ? ` · the server is up ${since(Date.now() / 1000 - m.uptime)}` : ""))));
}

export async function systemPage(pane, hooks, settingsModal, heading) {
  const machine = h("div", h("p.muted", "Loading…"));
  const boxes = h("div");
  const storage = h("div", h("p.muted", "Loading…"));
  let showStopped = false;
  let timer = null;

  const act = async (button, label, work) => {
    button.disabled = true;
    const text = button.textContent;
    button.textContent = label;
    try {
      await work();
    } catch (failure) {
      toast(failure.message, "error");
    } finally {
      button.disabled = false;
      button.textContent = text;
    }
  };
  const open = (link) => {
    if (!link) return null;
    return (event) => {
      event.preventDefault();
      settingsModal?.close();
      if (link.conversation) hooks.onOpenConversation?.(link.conversation);
      else if (link.service) hooks.onOpenService?.(link.service);
    };
  };

  function row(box) {
    const running = box.state === "running";
    const stats = running
      ? [`CPU ${box.cpu ?? "–"}%`, size(box.memory),
          box.started ? `running ${since(box.started)}` : "running"].join(" · ")
      : `stopped${box.exited ? ` ${timeAgo(box.exited)}` : ""}`;
    const title = box.link
      ? h("a", { href: relayUrl(box.link.conversation ? `/c/${box.link.conversation}` : `/s/${box.link.service}`),
        onclick: open(box.link) }, box.title) : box.title;
    const actions = [];
    if (box.actions.includes("stop")) {
      const stop = h("button.btn.small", { onclick: () => act(stop, "Stopping…", async () => {
        render(await api.post(`/api/system/boxes/${encodeURIComponent(box.name)}/stop`));
        toast(`${box.title}: stopped`);
      }) }, icon("stop", "sm"), "Stop");
      actions.push(stop);
    }
    if (box.actions.includes("remove")) {
      const remove = h("button.icon-btn.sm", { title: "Remove (what was installed in it goes; the folder stays)",
        onclick: async () => {
          if (!await confirmDialog("Remove this WorkBox?",
            `${box.title}: what an agent installed in it goes. The folder `
            + "stays; a new WorkBox is made when it's needed again.", { confirm: "Remove", danger: true })) return;
          await act(remove, "…",
            async () => render(await api.post(`/api/system/boxes/${encodeURIComponent(box.name)}/remove`)));
        } }, icon("trash", "sm"));
      actions.push(remove);
    }
    return h("div.provider-row",
      running ? statusDot("ok", "Running") : h("span.status-dot", { title: "Stopped" }),
      h("div.info", h("div.name", title), h("div.sub.truncate", { title: box.detail }, box.detail),
        h("div.sub", stats, box.why ? ` · ${box.why}` : "")),
      h("div.actions", actions));
  }

  function render(data) {
    clear(machine, machineCards(data.machine));
    const running = data.boxes.filter((b) => b.state === "running");
    const memory = running.reduce((sum, b) => sum + (b.memory || 0), 0);
    const sections =
      [h("p.muted.small-text", `${running.length} of ${data.boxes.length} running, using ${size(memory)} of memory.`)];
    for (const [kind, label, about] of GROUPS) {
      const all = data.boxes.filter((b) => b.kind === kind);
      if (!all.length) continue;
      const stopped = kind === "workbox" ? all.filter((b) => b.state !== "running") : [];
      const shown = kind === "workbox" && !showStopped ? all.filter((b) => b.state === "running") : all;
      const more = [];
      if (stopped.length) {
        more.push(h("button.btn.small", { onclick: () => { showStopped = !showStopped; render(data); } },
          showStopped ? "Hide the stopped ones" : `Show ${stopped.length} stopped`));
        const removeAll = h("button.btn.small.danger", { onclick: async () => {
          if (!await confirmDialog(`Remove ${stopped.length} stopped WorkBoxes?`, "What agents installed in them goes. "
            + "Their folders stay; a new WorkBox is made when one's needed again.",
            { confirm: "Remove them", danger: true })) return;
          await act(removeAll, "Removing…", async () => {
            const done = await api.post("/api/system/workboxes/remove-stopped");
            render(done);
            toast(`${done.removed} removed`);
          });
        } }, icon("trash", "sm"), `Remove the ${stopped.length} stopped`);
        more.push(removeAll);
      }
      sections.push(h("h5.vpn-heading", `${label} (${all.length})`), h("p.muted.small-text", about),
        shown.length ? h("div.settings-list", shown.map(row)) : h("p.muted", "None running."),
        more.length ? h("div.settings-actions", more) : null);
    }
    clear(boxes, data.boxes.length ? sections : h("p.muted", "Relay runs no boxes right now."));
  }

  async function load() {
    try {
      render(await api.get("/api/system"));
    } catch (failure) {
      clear(boxes, h("p.error-text", failure.message));
    }
  }

  async function loadStorage() {
    let found;
    try {
      found = await api.get("/api/system/storage");
    } catch (failure) {
      clear(storage, h("p.error-text", failure.message));
      return;
    }
    const totals = Object.entries(found.totals).map(([kind, t]) =>
      `${kind[0].toUpperCase()}${kind.slice(1)}: ${t.count}, ${size(t.size)}`
        + (t.reclaimable ? ` (${size(t.reclaimable)} unused)` : ""));
    const images = found.images.map((image) => {
      const remove = image.removable ? h("button.btn.small", { onclick: async () => {
        if (!await confirmDialog("Remove this image?", `${image.name} (${size(image.size)}): no container uses it. `
          + "Relay builds or fetches it again when it's needed.", { confirm: "Remove", danger: true })) return;
        await act(remove, "Removing…", async () => {
          await api.post(`/api/system/images/${encodeURIComponent(image.id)}/remove`);
          toast(`${image.name}: removed`);
          loadStorage();
        });
      } }, icon("trash", "sm"), "Remove") : null;
      return h("div.provider-row",
        h("div.info", h("div.name.truncate", image.name),
          h("div.sub",
            `${size(image.size)} · ${image.whose} · `
            + `${image.containers ? `used by ${image.containers}` : "not in use"}`)),
        h("div.actions", remove));
    });
    clear(storage, h("p.muted.small-text", totals.join(" · ")), h("div.settings-list", images));
  }

  const refresh =
    h("button.btn.small", { onclick: () => { load(); loadStorage(); } }, icon("refresh", "sm"), "Refresh");
  clear(pane,
    heading("System", "What Relay runs on this server, and what it takes. It looks again every few seconds.", refresh),
    h("section.settings-section", h("h4", "This server"), machine),
    h("section.settings-section", h("h4", "Relay's boxes"), boxes),
    h("section.settings-section", h("h4", "Storage"),
      h("p.lead", "podman's images, containers and volumes. An image no container uses can go: Relay builds or fetches "
        + "it again when it's needed."), storage));
  await load();
  loadStorage();
  timer = setInterval(() => {
    if (!pane.isConnected) {
      clearInterval(timer);
      return;
    }
    if (document.visibilityState === "visible") load();
  }, EVERY);
}
