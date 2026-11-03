// VPNs for WorkBoxes: gluetun, which Relay starts and keeps (relay/vpn.py). Profiles are VPN
// accounts (gluetun's environment: the usual settings as fields, from gluetun's own server list,
// the rest as NAME=value lines); tunnels say which WorkBoxes go through which exits (a profile and
// a place, in order), with rotation and a fallback loop.

import { api } from "./api.js";
import { pickFolder } from "./picker.js";
import { clear, clock, confirmDialog, h, icon, modal, statusDot, toast } from "./ui.js";

const MASK = "********";
const NAMES = {
  airvpn: "AirVPN", cyberghost: "CyberGhost", expressvpn: "ExpressVPN", fastestvpn: "FastestVPN",
  hidemyass: "HideMyAss", ipvanish: "IPVanish", ivpn: "IVPN", nordvpn: "NordVPN",
  "private internet access": "Private Internet Access",
  privatevpn: "PrivateVPN", protonvpn: "ProtonVPN", purevpn: "PureVPN", slickvpn: "SlickVPN", torguard: "TorGuard",
  "vpn unlimited": "VPN Unlimited", vpnsecure: "VPNSecure", vyprvpn: "VyprVPN",
};
const TYPES = { openvpn: "OpenVPN", wireguard: "WireGuard" };
// The fields per protocol: [setting, label, secret?, placeholder]
const LOGIN = {
  openvpn: [["OPENVPN_USER", "Username"], ["OPENVPN_PASSWORD", "Password", true]],
  wireguard:
    [["WIREGUARD_PRIVATE_KEY", "Private key", true],
      ["WIREGUARD_ADDRESSES", "Addresses", false, "e.g. 10.64.222.21/32"],
      ["WIREGUARD_PRESHARED_KEY", "Preshared key (if your provider gives one)", true]],
};
const CUSTOM = [["VPN_ENDPOINT_IP", "Server's address", false, "e.g. 203.0.113.7"],
  ["VPN_ENDPOINT_PORT", "Server's port", false, "51820"], ["WIREGUARD_PUBLIC_KEY", "Server's public key"]];

const nameOf = (provider) => NAMES[provider] || provider.charAt(0).toUpperCase() + provider.slice(1);

// NAME=value per line (# comments and blank lines left out) → {NAME: value}
function parseEnv(text) {
  const env = {};
  for (const [index, raw] of text.split("\n").entries()) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const at = line.indexOf("=");
    if (at < 1) throw new Error(`More settings, line ${index + 1}: NAME=value`);
    env[line.slice(0, at).trim()] = line.slice(at + 1).trim();
  }
  return env;
}

function select(options, value, label) {
  return h("select.select", { "aria-label": label || null },
    options.map(([v, text]) => h("option", { value: v, selected: v === value }, text)));
}

function field(label, control, hint) {
  return h("label.field", h("span", label, hint ? h("span.hint.block", hint) : null), control);
}

const busy = async (button, label, work) => {
  const text = button.textContent;
  button.disabled = true;
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

// ------------------------------------------------------------------ the section

export async function vpnSection() {
  const section = h("div", h("p.muted", "Loading…"));
  // gluetun's server list, for the dialogs: asked for when one opens, as the first time downloads gluetun and runs it
  // (just opening Settings did that). Once; again after it didn't come.
  let providers = null;
  let asking = false;
  const withServers = (open) => async () => {
    if (asking) return;
    asking = true;
    if (!providers) toast("Getting gluetun's server list (the first time, this downloads gluetun: about 50 MB)");
    try {
      providers ||= await api.get("/api/vpn/servers").then((d) => d.providers);
    } catch (failure) {
      toast(`gluetun's server list: ${failure.message}`, "error");
    }
    asking = false;
    open(providers || {});
  };
  const render = async () => {
    let vpn;
    try {
      vpn = await api.get("/api/vpn");
    } catch (failure) {
      clear(section, h("p.error-text", failure.message));
      return;
    }
    const tunnels =
      vpn.tunnels.map((tunnel) => tunnelRow(tunnel,
        withServers((list) => tunnelDialog(tunnel, vpn, list, render)), render));
    const profiles = vpn.profiles.map((profile) => h("div.provider-row",
      h("div.info", h("div.name", profile.name),
        h("div.sub",
          `${nameOf(profile.env.VPN_SERVICE_PROVIDER || "?")} · `
          + `${TYPES[profile.env.VPN_TYPE] || profile.env.VPN_TYPE || "OpenVPN"}`)),
      h("div.actions",
        h("button.btn.small", { onclick: withServers((list) => profileDialog(profile, list, render)) },
          icon("edit", "sm"), "Edit"),
        h("button.icon-btn.sm", { title: "Delete", onclick: async () => {
          if (!await confirmDialog("Delete this profile?", `${profile.name}: its login goes from Relay.`,
            { confirm: "Delete", danger: true })) return;
          await api.del(`/api/vpn/profiles/${profile.id}`).then(render, (failure) => toast(failure.message, "error"));
        } }, icon("trash", "sm")))));
    clear(section,
      vpn.tunnels.length && !vpn.relay_url ? h("p.muted.small-text",
        "Relay listens on one address only: agents behind a VPN can't reach Relay's tools.") : null,
      h("h5.vpn-heading", "Tunnels"),
      tunnels.length ? h("div.settings-list", tunnels) : h("p.muted", vpn.profiles.length
        ? "None yet: no WorkBox goes through a VPN." : "None yet. Add a profile first: your VPN account."),
      h("div.settings-actions",
        h("button.btn.small", { disabled: !vpn.profiles.length,
          onclick: withServers((list) => tunnelDialog(null, vpn, list, render)) }, icon("plus", "sm"), "Add tunnel")),
      h("h5.vpn-heading", "Profiles"),
      profiles.length ? h("div.settings-list", profiles) : h("p.muted", "None yet."),
      h("div.settings-actions",
        h("button.btn.small", { onclick: withServers((list) => profileDialog(null, list, render)) },
          icon("plus", "sm"), "Add profile")));
  };
  render();
  return section;
}

function tunnelRow(tunnel, edit, render) {
  const s = tunnel.status || {};
  const which = tunnel.all ? "All other WorkBoxes"
    : tunnel.folders.length ? tunnel.folders.map((f) => f.split("/").pop()).join(", ") : "No WorkBoxes yet";
  const how = [
    `${tunnel.exits.length} exit${tunnel.exits.length === 1 ? "" : "s"}`,
    tunnel.rotate.minutes
      ? `rotates every ${tunnel.rotate.minutes} min (${tunnel.rotate.to === "next" ? "next exit" : "new server"})`
      : null,
    tunnel.fallback.after && tunnel.exits.length > 1 ? `falls back after ${tunnel.fallback.after} s` : null,
    tunnel.ports.length ? `ports ${tunnel.ports.join(", ")}` : null,
  ].filter(Boolean).join(" · ");
  const now = s.connected ? `Connected · ${s.public_ip}${s.place ? ` (${s.place})` : ""} · ${s.exit_label}`
    : s.running ? `Not connected (yet) · ${s.exit_label}`
    : "Not running: it starts with a WorkBox that goes through it";
  const log = h("button.btn.small", { onclick: async () => {
    const m = modal(`Log of ${tunnel.name} (gluetun)`, { wide: true });
    const pre = h("pre.log", "…");
    m.body.append(pre);
    pre.textContent =
      await api.get(`/api/vpn/tunnels/${tunnel.id}/log`).then((d) => d.log, (failure) => failure.message);
  } }, icon("file", "sm"), "Log");
  // (without WorkBoxes it would only keep a connection nothing uses)
  const reconnect =
    h("button.btn.small",
      { disabled: !tunnel.all && !tunnel.folders.length, onclick: () => busy(reconnect, "Connecting…", async () => {
        const done = await api.post(`/api/vpn/tunnels/${tunnel.id}/reconnect`);
        toast(done.status.public_ip ? `${tunnel.name}: ${done.status.public_ip}` : `${tunnel.name} reconnected`);
        render();
      }) }, icon("refresh", "sm"), "Reconnect");
  return h("div.provider-row",
    s.running ? statusDot(s.connected ? "ok" : "fail", s.connected ? "Connected" : "Not connected")
      : h("span.status-dot", { title: "Not running" }),
    h("div.info", h("div.name", tunnel.name),
      h("div.sub", which), h("div.sub", now), h("div.sub", how),
      s.events?.length ? happened(s.events) : null),
    h("div.actions", h("button.btn.small", { onclick: edit }, icon("edit", "sm"), "Edit"), reconnect, log));
}

// What happened to a tunnel, newest first (kept: relay/vpn.py): its last few, the rest on asking
const MARK = { problem: "⚠ ", recovered: "✓ ", warn: "· " };

function happened(events) {
  const line = (e) => h(`div.sub.vpn-event.${e.kind || "info"}`, `${clock(e.time)} ${MARK[e.kind] || ""}${e.text}`);
  const list = h("div", events.slice(0, 12).map(line));
  const problem = events.find((e) => e.kind === "problem" || e.kind === "recovered");
  return h("details.vpn-events", { open: problem?.kind === "problem" },
    h("summary", "What happened" + (problem?.kind === "problem" ? ": a problem" : "")), list,
    events.length > 12 ? h("button.btn.small", { onclick: (event) => {
      clear(list, events.map(line));
      event.currentTarget.remove();
    } }, `Show all ${events.length}`) : null);
}

// ------------------------------------------------------------------ a profile

function profileDialog(profile, providers, render) {
  const m = modal(profile ? `Profile: ${profile.name}` : "New VPN profile", { wide: true });
  const env = { ...(profile?.env || {}) };
  const take = (key) => { const value = env[key]; delete env[key]; return value; };
  let provider = (take("VPN_SERVICE_PROVIDER") || "").toLowerCase();
  let type = (take("VPN_TYPE") || "openvpn").toLowerCase();
  const values = {};
  for (const [key] of [...LOGIN.openvpn, ...LOGIN.wireguard, ...CUSTOM]) values[key] = take(key) || "";
  const name = h("input.input", { value: profile?.name || "", placeholder: "e.g. Windscribe" });
  const providerSelect = select([["", "Choose…"],
    ...Object.keys(providers).map((p) => [p, nameOf(p)]),
    ...(provider && provider !== "custom" && !providers[provider] ? [[provider, nameOf(provider)]] : []),
    ["custom", "Custom (a WireGuard server of your own)"]], provider, "VPN provider");
  const typeRow = h("div");
  const loginRow = h("div");
  const inputs = {};
  const typesOf = () => provider === "custom" ? ["wireguard"] : Object.keys(providers[provider]?.types || TYPES);
  const input = ([key, label, secret, placeholder]) => {
    inputs[key] =
      h("input.input", { type: secret ? "password" : "text", value: values[key], placeholder: placeholder || "",
        autocomplete: "off", spellcheck: false, oninput: () => { values[key] = inputs[key].value; } });
    return field(label, inputs[key]);
  };
  const showType = () => {
    const types = typesOf();
    if (!types.includes(type)) type = types[0];
    clear(typeRow);
    clear(loginRow);
    if (!provider) return;
    if (types.length > 1) {
      const typeSelect = select(types.map((t) => [t, TYPES[t] || t]), type, "Protocol");
      typeSelect.addEventListener("change", () => { type = typeSelect.value; showType(); });
      typeRow.append(field("Protocol", typeSelect));
    }
    clear(loginRow, h("p.muted.small-text", type === "openvpn"
      ? "Your provider's OpenVPN login: often not your account's own (look for “manual setup” or a config generator)."
      : "From your provider's WireGuard configuration."),
    (LOGIN[type] || []).map(input), provider === "custom" ? CUSTOM.map(input) : null);
  };
  providerSelect.addEventListener("change", () => {
    provider = providerSelect.value;
    if (!name.value.trim() && provider) name.value = provider === "custom" ? "My WireGuard server" : nameOf(provider);
    showType();
  });
  showType();
  const more = h("textarea.textarea.code", { rows: 4, spellcheck: false, placeholder: "e.g. FIREWALL_VPN_INPUT_PORTS=…",
    value: Object.entries(env).map(([k, v]) => `${k}=${v}`).join("\n") });
  const save = h("button.btn.primary", { onclick: () => busy(save, "Saving…", async () => {
    if (!provider) throw new Error("Choose your VPN provider");
    const found = { ...parseEnv(more.value), VPN_SERVICE_PROVIDER: provider, VPN_TYPE: type };
    for (const [key] of [...(LOGIN[type] || []), ...(provider === "custom" ? CUSTOM : [])]) {
      if (values[key].trim()) found[key] = values[key].trim();
    }
    const data = { name: name.value, env: found };
    const saved = profile ? await api.put(`/api/vpn/profiles/${profile.id}`, data)
      : await api.post("/api/vpn/profiles", data);
    if (saved.error) toast(`Saved, but a tunnel didn't connect: ${saved.error}`, "error");
    else toast("Profile saved");
    m.close();
    render();
  }) }, "Save");
  m.body.append(
    field("Name", name),
    field("VPN provider", providerSelect),
    typeRow, loginRow,
    h("details.vpn-more", { open: Object.keys(env).length > 0 }, h("summary", "More settings"),
      field("Other gluetun settings", more,
        "One NAME=value per line, as in gluetun's wiki (github.com/qdm12/gluetun-wiki). "
        + `Keys show as ${MASK}: leave those as they are to keep them. A place is a tunnel's exit's.`)));
  m.foot.append(h("span.grow"), h("button.btn", { onclick: () => m.close() }, "Cancel"), save);
}

// ------------------------------------------------------------------ a tunnel

function tunnelDialog(tunnel, vpn, providers, render) {
  const m = modal(tunnel ? `Tunnel: ${tunnel.name}` : "New VPN tunnel", { wide: true });
  const name = h("input.input", { value: tunnel?.name || "", placeholder: "e.g. Private projects" });
  const all = h("input", { type: "checkbox", checked: Boolean(tunnel?.all) });
  const folders = h("div.list-editor");
  const addRow = (value) => {
    const row = h("div.row", h("input.input.mono", { value, readOnly: true }),
      h("button.icon-btn", { title: "Remove", onclick: () => row.remove() }, icon("trash", "sm")));
    folders.append(row);
  };
  (tunnel?.folders || []).forEach(addRow);
  const chosen = h("div.vpn-folders", folders, h("button.btn.small", { onclick: async () => {
    const folder = await pickFolder();
    if (folder) addRow(folder);
  } }, icon("plus", "sm"), "Add folder"));
  const showFolders = () => { chosen.hidden = all.checked; };
  all.addEventListener("change", showFolders);
  showFolders();

  // Its exits, in order: a profile and a place from its provider's servers
  const exits = (tunnel?.exits || [{ profile: vpn.profiles[0]?.id || "", place: {} }])
    .map((e) => ({ profile: e.profile, place: { ...e.place } }));
  const exitList = h("div.vpn-exits");
  const placesOf = (exit) => {
    const env = vpn.profiles.find((p) => p.id === exit.profile)?.env || {};
    const info = providers[(env.VPN_SERVICE_PROVIDER || "").toLowerCase()];
    return info ? { info, places: info.types?.[(env.VPN_TYPE || "openvpn").toLowerCase()] || {} } : null;
  };
  const showExits = () => {
    clear(exitList, exits.map((exit, index) => {
      const profileSelect = select(vpn.profiles.map((p) => [p.id, p.name]), exit.profile, `Exit ${index + 1}: profile`);
      profileSelect.addEventListener("change",
        () => { exit.profile = profileSelect.value; exit.place = {}; showExits(); });
      const known = placesOf(exit);
      let where = null;
      if (known && Object.keys(known.places).length) {
        const setting = known.info.setting;
        const at = exit.place[setting] || "";
        const placeSelect = select([["", "Anywhere"], ...Object.keys(known.places).map((p) => [p, p]),
          ...(at && !known.places[at] ? [[at, at]] : [])], at,
          `Exit ${index + 1}: ${setting === "SERVER_REGIONS" ? "region" : "country"}`);
        placeSelect.addEventListener("change", () => {
          exit.place = placeSelect.value ? { [setting]: placeSelect.value } : {};
          showExits();
        });
        const cities = known.places[at] || [];
        const city = exit.place.SERVER_CITIES || "";
        const citySelect = known.info.cities && at ? select([["", "Any city"], ...cities.map((c) => [c, c]),
          ...(city && !cities.includes(city) ? [[city, city]] : [])], city, `Exit ${index + 1}: city`) : null;
        citySelect?.addEventListener("change", () => {
          if (citySelect.value) exit.place.SERVER_CITIES = citySelect.value;
          else delete exit.place.SERVER_CITIES;
        });
        where = [placeSelect, citySelect];
      } else if (Object.keys(exit.place).length) {
        where = h("span.muted.small-text", Object.values(exit.place).join(", "));
      }
      const move = (by) => () => { exits.splice(index + by, 0, ...exits.splice(index, 1)); showExits(); };
      return h("div.vpn-exit",
        h("span.vpn-exit-number", `${index + 1}`), profileSelect, where,
        h("span.vpn-exit-actions",
          h("button.icon-btn.sm", { title: "Earlier", disabled: index === 0, onclick: move(-1) }, h("span", "↑")),
          h("button.icon-btn.sm",
            { title: "Later", disabled: index === exits.length - 1, onclick: move(1) }, h("span", "↓")),
          h("button.icon-btn.sm",
            { title: "Remove", disabled: exits.length === 1, onclick: () => { exits.splice(index, 1); showExits(); } },
            icon("trash", "sm"))));
    }));
  };
  showExits();
  const addExit = h("button.btn.small", { onclick: () => {
    exits.push({ profile: exits.at(-1)?.profile || vpn.profiles[0].id, place: {} });
    showExits();
  } }, icon("plus", "sm"), "Add exit");

  const rotate = { minutes: 0, to: "server", idle: true, ...(tunnel?.rotate || {}) };
  const fallback = { after: 60, back: 30, ...(tunnel?.fallback || {}) };
  const minutes =
    h("input.input.vpn-number",
      { type: "number", min: 0, value: String(rotate.minutes), "aria-label": "Rotate every (minutes)" });
  const to = select([["server", "a new server at the same exit"], ["next", "the next exit"]], rotate.to, "Rotate to");
  const idle = h("input", { type: "checkbox", checked: rotate.idle });
  const after =
    h("input.input.vpn-number",
      { type: "number", min: 0, value: String(fallback.after), "aria-label": "Fall back after (seconds)" });
  const back =
    h("input.input.vpn-number",
      { type: "number", min: 0, value: String(fallback.back), "aria-label": "Back to the first after (minutes)" });
  const ports = h("input.input.mono", { value: (tunnel?.ports || []).join(", "), placeholder: "e.g. 3000, 8080" });

  const save = h("button.btn.primary", { onclick: () => busy(save, "Connecting…", async () => {
    const data = {
      name: name.value, all: all.checked, folders: [...folders.querySelectorAll("input")].map((i) => i.value),
      exits, ports: ports.value.split(/[\s,]+/).filter(Boolean),
      rotate: { minutes: Number(minutes.value) || 0, to: to.value, idle: idle.checked },
      fallback: { after: Number(after.value) || 0, back: Number(back.value) || 0 },
    };
    const saved = tunnel ? await api.put(`/api/vpn/tunnels/${tunnel.id}`, data)
      : await api.post("/api/vpn/tunnels", data);
    if (saved.error) toast(`Saved, but it didn't connect: ${saved.error}`, "error");
    else toast(saved.status?.public_ip
      ? `${saved.name}: connected (${saved.status.public_ip})` : `${saved.name} saved`);
    m.close();
    render();
  }) }, "Save");
  const remove = tunnel ? h("button.btn.danger", { onclick: async () => {
    if (!await confirmDialog("Delete this tunnel?", `${tunnel.name}: its WorkBoxes start anew on the machine's network `
      + "(or another tunnel's) when next used.", { confirm: "Delete", danger: true })) return;
    await api.del(`/api/vpn/tunnels/${tunnel.id}`)
      .then(() => { m.close(); render(); }, (failure) => toast(failure.message, "error"));
  } }, icon("trash", "sm"), "Delete") : null;

  m.body.append(
    field("Name", name),
    h("label.field.inline", all, h("span", "All other WorkBoxes",
      h("span.hint.block", "Those no other tunnel has. Or only these project folders' (and their subfolders'):"))),
    chosen,
    field("Exits", h("div", exitList, addExit),
      "In order: the first is where it connects; the next ones are for the fallback loop and rotation."),
    h("div.vpn-rules",
      h("div.field", h("span", "Rotation", h("span.hint.block", "Every so many minutes (0: never), to:")),
        h("div.vpn-inline", minutes, h("span", "minutes, to"), to)),
      h("label.field.inline", idle, h("span", "Not while an agent works in one of its WorkBoxes",
        h("span.hint.block", "A change of server cuts open connections for a few seconds.")))),
    h("div.vpn-rules",
      h("div.field", h("span", "Fallback loop", h("span.hint.block",
        "An exit without a connection this many seconds (0: never): the next one, round and round.")),
      h("div.vpn-inline", after, h("span", "seconds; back to the first exit after"),
        back, h("span", "minutes (0: stay)")))),
    field("Ports to pass through", ports,
      "A server in one of its WorkBoxes is reachable from the LAN on these ports only."));
  m.foot.append(...(remove ? [remove] : []), h("span.grow"),
    h("button.btn", { onclick: () => m.close() }, "Cancel"), save);
}
