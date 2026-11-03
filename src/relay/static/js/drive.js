// Network drives: a code task's folder (or the scripts') over WebDAV, to open on your own computer
// (relay/webdav.py).

import { relayUrl } from "./base.js";
import { api } from "./api.js";
import { clear, copyText, h, icon, modal, showMenu, toast } from "./ui.js";

// The address as this browser reaches Relay: the computer it runs on can reach it the same way.
export function driveAddress(share) {
  return `${location.origin}${relayUrl(share.path)}`;
}

// Explorer's address for it: a UNC path (the WebClient service).
function windowsAddress(share) {
  const secure = location.protocol === "https:";
  const port = location.port || (secure ? "443" : "80");
  const where = `${location.hostname}${secure ? "@SSL" : ""}${port === (secure ? "443" : "80") ? "" : `@${port}`}`;
  return `\\\\${where}\\DavWWWRoot${share.path.replace(/\//g, "\\")}`;
}

// The same address as each system's file manager takes it: Explorer a UNC path,
// Linux's file managers dav://, Finder the http address.
function systemAddress(share) {
  const agent = navigator.userAgent;
  if (/Windows/.test(agent)) {
    return { system: "windows", address: windowsAddress(share), hint: "Paste it in File Explorer's address bar." };
  }
  if (/Linux/.test(agent) && !/Android/.test(agent)) {
    return { system: "linux", address: driveAddress(share).replace(/^http/, "dav"),
      hint: "Paste it in the file manager's address bar (Ctrl+L)." };
  }
  if (/Macintosh/.test(agent) && !("ontouchend" in document)) {
    return { system: "mac", address: driveAddress(share),
      hint: "In Finder: Go → Connect to Server (⌘K), paste it, and connect as Guest." };
  }
  return { system: "other", address: driveAddress(share), hint: "Open it in an app that speaks WebDAV." };
}

// "Open files" under a code task's message box: its folder on your own computer,
// as a network drive; no password, the address is the key (relay/webdav.py).
export function filesButton(conv) {
  const button = h("button.picker", { type: "button", title: "Open the files the agent works on, on this computer",
    onclick: async () => {
      let share;
      try {
        share = await api.post(`/api/conversations/${conv.id}/share`, {});
      } catch (error) {
        toast(error.message, "error");
        return;
      }
      const { system, address, hint } = systemAddress(share);
      showMenu(button, [
        system === "linux" ? { label: "Open in the file manager", icon: "folder",
          onclick: () => { h("a", { href: address }).click(); } } : null,
        { label: "Copy the address", icon: "copy", title: address,
          onclick: () => copyText(address, `Copied. ${hint}`) },
        { label: "More ways, read-only…", icon: "settings", onclick: () => openDrive(conv) },
      ].filter(Boolean));
    } }, icon("folder", "sm"), h("span.truncate", "Open files"), icon("chevronDown", "sm"));
  return button;
}

function steps(share) {
  const address = driveAddress(share);
  const dav = address.replace(/^http/, "dav");
  const block = (title, lines) => h("div.drive-os", h("b", title), h("ol", lines.map((l) => h("li", l))));
  return h("div.drive-steps",
    block("Windows", [
      ["Quickest: paste ", h("code", windowsAddress(share)), " in File Explorer's address bar."],
      "To keep it: This PC → Map network drive (… menu, or right-click This PC).",
      ["Folder: ", h("code", address), " → Finish."],
      "Files over 50 MB need a registry change (FileSizeLimitInBytes of the WebClient service).",
    ]),
    block("macOS", [
      "Finder → Go → Connect to Server (⌘K).",
      ["Server address: ", h("code", address), " → Connect → as Guest."],
    ]),
    block("Linux", [
      ["Files → Other Locations → Connect to Server: ", h("code", dav)],
      ["Or: ", h("code", `gio mount ${dav}`), ", or davfs2: ", h("code", `sudo mount -t davfs ${address} /mnt/relay`)],
    ]));
}

// Share a code task's folder (or show its share) with the steps to open it.
export function openDrive(conv) {
  return openShare(`/api/conversations/${conv.id}/share`, {
    lead: "Open this task's folder on your own computer to try what the agent wrote. What you change there is "
      + "recorded with the agent's next turn (Relay keeps the files before each), like any change made outside Relay.",
  });
}

// A folder shared at `endpoint` (POST: made, or as it is; {read_only} changes it): its address, the steps to open
// it on each system, and Stop sharing. `lead`: what it's for; `more(address)`: what else to say.
export async function openShare(endpoint, { title = "Open as a network drive", lead, more = null }) {
  let share;
  try {
    share = await api.post(endpoint, {});
  } catch (error) {
    toast(error.message, "error");
    return;
  }
  const m = modal(title, { wide: true });
  const body = h("div");
  m.body.append(body);
  const render = () => {
    const address = driveAddress(share);
    const readOnly = h("input", { type: "checkbox", checked: share.read_only, onchange: async () => {
      try {
        share = await api.post(endpoint, { read_only: readOnly.checked });
        render();
      } catch (error) {
        toast(error.message, "error");
      }
    } });
    clear(body,
      h("p.muted", lead),
      h("div.skill-folder", h("span.muted", "Address"), h("code", address),
        h("button.icon-btn.sm", { title: "Copy the address", onclick: () => copyText(address) }, icon("copy", "sm"))),
      h("p.muted.small-text", `The folder: ${share.directory}`),
      h("label.field.inline", readOnly, h("span", "Read-only")),
      more ? more(address, systemAddress(share)) : null,
      steps(share),
      h("p.muted.small-text", "Anyone on your network who has this address can open the folder. "
        + "Stop sharing to make the address stop working."));
  };
  render();
  m.foot.append(h("button.btn.danger", { onclick: async () => {
    try {
      await api.del(`/api/shares/${share.token}`);
      toast("No longer shared");
      m.close();
    } catch (error) {
      toast(error.message, "error");
    }
  } }, "Stop sharing"), h("span.grow"), h("button.btn.primary", { onclick: () => m.close() }, "Done"));
}

// Settings → Workspaces: every shared folder, to stop sharing.
export async function drivesSection() {
  const section = h("div");
  const render = async () => {
    let shares;
    try {
      shares = await api.get("/api/shares");
    } catch (error) {
      clear(section, h("p.error-text", error.message));
      return;
    }
    clear(section, shares.length ? shares.map((share) => h("div.provider-row",
      h("div.info", h("div.name", share.name, share.read_only ? h("span.badge", "read-only") : null),
        h("div.sub", share.directory), h("div.sub", h("code", driveAddress(share)))),
      h("div.actions",
        h("button.icon-btn.sm",
          { title: "Copy the address", onclick: () => copyText(driveAddress(share)) }, icon("copy", "sm")),
        h("button.icon-btn.sm", { title: "Stop sharing", onclick: async () => {
          await api.del(`/api/shares/${share.token}`).catch((error) => toast(error.message, "error"));
          render();
        } }, icon("trash", "sm")))))
      : h("p.muted", "None. Type /mount in a code task to open its folder on your computer; the Scripts page "
        + "opens the scripts' folder."));
  };
  await render();
  return section;
}
