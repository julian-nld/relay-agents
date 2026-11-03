// OpenHands' part of Relay's page: a button (a dot while it runs, a count when Relay's agent asks you something in
// one of its conversations), its page and a tab in Settings (docs/WRITING-A-PLUGIN.md). While it runs, its page is
// OpenHands' Agent Canvas, framed at Relay's /canvas (the plugin forwards it there, after Relay's sign-in), under a
// slim bar. Canvas shares Relay's origin, so the page first writes Canvas' own storage in this browser: its backend
// (this OpenHands, at Relay's /port/<port>/) and its first-start questions answered. Canvas then opens asking for
// nothing. Relay's address follows Canvas' (/p/openhands/conversations/<id>), so a reload or a link comes back to
// the same place.

const HAND = '<path d="M8 12V5.5a1.5 1.5 0 0 1 3 0V11"/><path d="M11 10V4a1.5 '
  + '1.5 0 0 1 3 0v6"/><path d="M14 10.5V5.5a1.5 1.5 0 0 1 3 0V12"/><path '
  + 'd="M17 11a1.5 1.5 0 0 1 3 0v3a7 7 0 0 1-7 7h-1.5a6 6 0 0 1-4.7-2.3L3.6 15a1.5 1.5 0 0 1 2.3-1.9L8 15.5"/>';
const ROUTES = ["", "{a}", "{a}/{b}", "{a}/{b}/{c}", "{a}/{b}/{c}/{d}", "{a}/{b}/{c}/{d}/{e}"];  // (Canvas' depths)

export default function setup(relay) {
  const { h, clear, toast, confirmDialog, modal } = relay.ui;
  const button = relay.barItem({ title: "OpenHands", icon: HAND, onClick: () => relay.navigate("") });
  // Where Relay is on its site ("" at its root): Canvas only works there, as it knows no path but /canvas
  const base = new URL(relay.url(""), location.href).pathname.replace(/\/plugins\/[^/]+\/static\/$/, "");
  const canvas = `${base}/canvas/`;
  const ours = `${base}/p/${relay.name}`;

  async function look() {
    const now = await relay.api.get("status");
    button.el.classList.toggle("oh-on", now.state === "running");
    return now;
  }

  // Relay's conversations of OpenHands' that wait for you in Relay (a question with a form, a permission): OpenHands
  // can't show those, as its ACP client answers permissions itself and declines forms
  async function asking() {
    const [{ conversations }, relays] = await Promise.all([relay.api.get("conversations"),
      relay.api.relay.get("/api/conversations")]);
    const found = new Map(conversations.filter((c) => c.relay).map((c) => [c.relay, c]));
    const asks = relays.filter((c) => c.needs_approval && found.has(c.id))
      .map((c) => ({ id: c.id, title: c.title || found.get(c.id).title || "a conversation" }));
    button.setBadge(asks.length);
    return asks;
  }

  async function watch() {
    const now = await look();
    if (now.set_up && now.ready) await asking().catch(() => {});
    else button.setBadge(0);
  }
  watch().catch(() => {});
  setInterval(() => watch().catch(() => {}), 60 * 1000);

  // Canvas' storage in this browser: this OpenHands as its backend (other backends stay), set up already, no
  // telemetry, Relay's theme
  function seed(now) {
    const read = (key, fallback) => {
      try {
        return JSON.parse(localStorage.getItem(key)) ?? fallback;
      } catch {
        return fallback;
      }
    };
    const backend = { id: "relay", name: "Relay", host: `${location.origin}${base}/port/${now.port}`,
      apiKey: now.api_key, kind: "local" };
    const others =
      [read("openhands-backends", [])].flat().filter((b) => b && typeof b === "object" && b.id !== backend.id);
    const active = read("openhands-active-backend", null);
    try {
      localStorage.setItem("openhands-backends", JSON.stringify([backend, ...others]));
      if (!active || ![backend, ...others].some((b) => b.id === active.backendId)) {
        localStorage.setItem("openhands-active-backend", JSON.stringify({ backendId: backend.id, orgId: null }));
      }
      if (!localStorage.getItem("openhands-onboarded")) localStorage.setItem("openhands-onboarded", "1");
      if (!localStorage.getItem("openhands-telemetry-consent")) {
        localStorage.setItem("openhands-telemetry-consent", "denied");
      }
      // (light or dark as Relay is, until another theme is chosen in Canvas)
      const theme = document.documentElement.dataset.theme === "dark" ? "openhands-neutral" : "light-plus";
      const chosen = localStorage.getItem("openhands-color-theme");
      if (!chosen || chosen === localStorage.getItem("relay.openhands-theme")) {
        localStorage.setItem("openhands-color-theme", theme);
        localStorage.setItem("relay.openhands-theme", theme);
      }
    } catch {
      // (no storage in this browser: Canvas asks for its backend itself)
    }
  }

  // Relay's providers and their models (asked once a minute at most: a provider's list may take a while)
  let agents = null, asked = 0;
  function catalog() {
    if (!agents || Date.now() - asked > 60 * 1000) {
      asked = Date.now();
      agents = relay.api.relay.get("/api/providers").then(({ providers }) => Promise.all(
        providers.filter((p) => p.enabled).map(async (provider) => {
          try {
            return { provider, models: (await relay.api.relay.get(
              `/api/providers/${encodeURIComponent(provider.id)}/models`)).models };
          } catch {
            return { provider, models: [{ id: "", name: "Default model" }] };
          }
        })));
    }
    return agents;
  }

  // The Relay agent OpenHands starts its conversations with: one of Relay's providers and its models, or Relay's
  // default. (Canvas' own model picker shows it, but switches only agents OpenHands knows by name.) It's there at
  // once; Relay's whole list fills in when its providers answer.
  function agentSelect(now, onDone) {
    const fallback = () => h("option", { value: "" }, "Relay's default (your latest conversation's)");
    const named = (value) => h("option", { value }, value.replace("|", " · "));
    const select = h("select.select", { "aria-label": "Relay's agent for OpenHands' new conversations" }, fallback(),
      now.agent ? named(now.agent) : null);
    select.value = now.agent;
    catalog().then((found) => {
      const kept = select.value;
      clear(select, fallback(), found.map(({ provider, models }) => h("optgroup", { label: provider.name },
        models.map((m) => h("option", { value: `${provider.id}|${m.id || ""}` },
          `${provider.name} · ${m.name || m.id || "default"}`)))));
      if (![...select.options].some((o) => o.value === kept)) select.prepend(named(kept));
      select.value = kept;
    }).catch(() => {});
    select.addEventListener("change", async () => {
      try {
        await relay.api.put("settings", { agent: select.value });
        toast("OpenHands' next conversation starts with that agent");
        onDone?.();
      } catch (error) {
        toast(error.message, "error");
      }
    });
    return select;
  }
  const agentPicker = (now, onDone) => h("label.oh-field", h("span", "Relay's agent"), agentSelect(now, onDone));

  function portField(now, onDone) {
    const port = h("input.input", { type: "number", min: 1024, max: 65535, value: now.port, "aria-label": "Port" });
    return h("label.oh-field", h("span", "Port"), port, h("button.btn.small", { onclick: async () => {
      try {
        await relay.api.put("settings", { port: Number(port.value) });
        toast("Saved");
        onDone?.();
      } catch (error) {
        toast(error.message, "error");
      }
    } }, "Save"));
  }

  function logBox() {
    const box = h("pre.oh-log");
    return h("details.oh-logs", { ontoggle: async (e) => {
      if (e.target.open) box.textContent = (await relay.api.get("logs")).logs || "(nothing yet)";
    } }, h("summary", "Its log"), box);
  }

  const versionOf = (now) => now.image.split(":").at(-1);

  // Its page, /p/openhands, and Canvas' routes under it (/p/openhands/conversations/<id> frames that conversation)
  async function render(el, { params = {}, query = {} } = {}) {
    const route = Object.keys(params).sort().map((key) => encodeURIComponent(params[key])).join("/");
    const search = new URLSearchParams(query).toString();
    let timer = null, following = null, ticks = 0;
    let framed = null;  // while Canvas is framed: {port, key, bar, notes, tab, agent}

    async function act(path, payload) {
      try {
        await relay.api.post(path, payload);
      } catch (error) {
        toast(error.message, "error");
      }
      draw();
    }

    // Its settings while Canvas is framed: the agent, the port, Update and its log
    function settings(now) {
      const box = modal("OpenHands");
      clear(box.body, h("div.oh-dialog", agentPicker(now), portField(now, () => {
        box.close();
        draw();
      }), h("div.oh-row", h("button.btn.small", { onclick: () => {
        box.close();
        act("update");
      } }, now.newer ? `Update to ${now.newer}` : "Update"), h("span.muted", versionOf(now))), logBox()));
    }

    // Relay's address after Canvas' route (replaced, not pushed: the frame's history has the steps), and "Open in a
    // tab" with it
    function follow(frame) {
      return setInterval(() => {
        let at;
        try {
          at = frame.contentWindow.location;
        } catch {
          return;  // (not Canvas' page: another origin)
        }
        if (!at.pathname.startsWith(canvas) || !el.isConnected) return;
        if (location.pathname !== ours && !location.pathname.startsWith(`${ours}/`)) return;  // (another page now)
        const route = at.pathname.slice(canvas.length).replace(/\/+$/, "");
        const want = `${ours}${route ? `/${route}` : ""}${at.search}`;
        if (`${location.pathname}${location.search}` !== want) history.replaceState(history.state, "", want);
        framed.tab.href = `${at.pathname}${at.search}`;
      }, 1000);
    }

    function drawFramed(now, asks) {
      if (!framed || framed.port !== now.port || framed.key !== now.api_key) {
        seed(now);
        relay.api.post("sync").catch(() => {});  // (Relay's newest project folders in Open Workspace at once)
        const start = framed ? canvas : `${canvas}${route}${search ? `?${search}` : ""}`;
        const frame =
          h("iframe.oh-frame", { src: start, title: "OpenHands", allow: "clipboard-read; clipboard-write" });
        framed = { port: now.port, key: now.api_key, bar: h("div.oh-bar"), notes: h("div.oh-notes"),
          tab: h("a.btn.small.ghost.oh-tab", { href: start, target: "_blank", rel: "noopener" }, "Open in a tab") };
        clear(el, h("div.oh-framed", framed.bar, framed.notes, frame));
        clearInterval(following);
        following = follow(frame);
      }
      if (framed.agent?.dataset.agent !== now.agent) {  // (kept while it's the same: it may be open)
        framed.agent = h("span.oh-agent", { dataset: { agent: now.agent } }, agentSelect(now, draw));
      }
      clear(framed.bar,
        h("span.oh-state.on"), h("strong", "OpenHands"), framed.agent,
        h("span.oh-grow"),
        now.doing ? h("span.oh-doing", h("span.oh-spinner"), `${now.doing[0].toUpperCase()}${now.doing.slice(1)}…`)
          : now.newer ? h("button.btn.small.primary", { title: `This one is ${versionOf(now)}`,
            onclick: () => act("update") }, `Update to ${now.newer}`) : null,
        framed.tab,
        h("button.btn.small.ghost", { onclick: () => settings(now) }, "Settings"),
        h("button.btn.small", { onclick: () => act("stop") }, "Stop"));
      notes(now, asks);
    }

    function notes(now, asks) {
      clear(framed.notes,
        asks.map((ask) => h("p.oh-note.oh-asks", h("span", "Relay's agent asks you something in ", h("b", ask.title),
          ": OpenHands can't show it."),
          h("button.btn.small.primary", { onclick: () => relay.navigate(`/c/${ask.id}`) }, "Answer in Relay"))),
        now.relay_agent === false ? h("p.oh-note", "OpenHands' agent isn't Relay's any more (another was chosen in "
          + "OpenHands): its conversations don't show in Relay.",
        h("button.btn.small", { onclick: () => act("use-relay") }, "Use Relay's agents again")) : null,
        now.app_error ? h("p.oh-note", now.app_error) : null);
    }

    async function drawCard(now) {
      framed = null;
      clearInterval(following);
      const parts = [];
      if (now.doing) parts.push(h("p.oh-doing", h("span.oh-spinner"),
        `${now.doing[0].toUpperCase()}${now.doing.slice(1)}…`));
      if (now.error) parts.push(h("p.error-text", now.error));
      if (!now.set_up && !now.doing) {
        parts.push(h("section.oh-card",
          h("p", "Setting up downloads OpenHands (about 4.4 GB, once) and makes an app password “OpenHands” for it "
            + "(Settings → Account), then starts it."),
          h("button.btn.primary", { onclick: async () => {
            if (now.has_password) return act("start");
            try {
              const app = await relay.api.relay.post("/api/app-passwords", { name: "OpenHands" });
              await act("setup", { app_password: app.password });
            } catch (error) {
              toast(error.message, "error");
            }
          } }, now.has_password ? "Download and start" : "Set up")));
      } else if (now.set_up) {
        const running = now.state === "running";
        parts.push(h("section.oh-card",
          h("div.oh-row", h("span", { class: `oh-state${running ? " on" : ""}` }),
            h("strong", running ? "Running" : "Stopped"),
            h("span.muted", now.newer ? `${versionOf(now)} (${now.newer} is out)` : versionOf(now))),
          base && running ? h("p", `OpenHands works only with Relay at its site's root (it knows no path but /canvas); `
            + `here Relay is at ${base}/.`) : null,
          h("div.oh-row",
            h("button.btn.primary", { disabled: Boolean(now.doing), onclick: () => act(running ? "stop" : "start") },
              running ? "Stop" : "Start"),
            h("button.btn.ghost", { disabled: Boolean(now.doing), onclick: () => act("update") },
              now.newer ? `Update to ${now.newer}` : "Update"))));
        parts.push(h("section.oh-card", agentPicker(now, draw), portField(now, draw), logBox()));
      }
      clear(el, h("div.oh-page", h("h1", "OpenHands"),
        h("p.lead", "OpenHands' Agent Canvas, on this server in a container, working with Relay's agents: a "
          + "conversation in one of your projects is Relay's code task there."),
        h("div.oh-body", parts)));
    }

    async function draw() {
      clearTimeout(timer);
      let now;
      try {
        now = await look();
      } catch (error) {
        framed = null;
        clearInterval(following);
        clear(el, h("div.oh-page", h("h1", "OpenHands"), h("p.error-text", error.message)));
        return;
      }
      if (now.set_up && now.ready && !base) {
        drawFramed(now, await asking().catch(() => []));
        ticks = 0;
        timer = setTimeout(tick, 15 * 1000, now);
      } else {
        await drawCard(now);
        if (now.doing) timer = setTimeout(draw, 2500);
      }
    }

    // While framed: what Relay's agents ask, every 15 seconds; the rest (its state, its agent, a newer one) every
    // minute, or sooner while it's busy
    async function tick(now) {
      if (++ticks % 4 === 0 || now.doing) return draw();
      if (framed) notes(now, await asking().catch(() => []));
      timer = setTimeout(tick, 15 * 1000, now);
    }

    await draw();
    return () => {
      clearTimeout(timer);
      clearInterval(following);
    };
  }
  for (const path of ROUTES) relay.page(path, render);

  // A tab in Settings: its agent, its port, and taking it away
  relay.settingsTab({ label: "OpenHands", icon: HAND, render: async (pane) => {
    const now = await look();
    const also = h("input", { type: "checkbox" });
    clear(pane, h("h3", "OpenHands"),
      h("p.lead", "OpenHands' Agent Canvas in a container, with Relay's agents. Its page (the hand on the bar) sets "
        + "it up, starts it and has it while it runs."),
      now.set_up ? [agentPicker(now), portField(now)] : h("p.muted", "Not set up yet."),
      h("h4", "Remove"),
      h("label.oh-check", also, h("span", "Also its conversations and settings")),
      h("button.btn.danger.small", { onclick: async () => {
        if (!await confirmDialog("Remove OpenHands?", "Its container goes" + (also.checked
          ? ", and its conversations and settings with it." : "; its conversations and settings stay for the next "
          + "start."), { confirm: "Remove", danger: true })) return;
        await relay.api.post("remove", { data: also.checked });
        toast("OpenHands removed");
      } }, "Remove"));
  } });
}
