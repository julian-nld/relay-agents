// The VSCodium plugin's part of Relay's page: a tab in Settings with the steps to Relay in VSCodium. Connect to this
// machine with Remote - SSH, get the extension (installed here, or downloaded), and sign it in with an app password.

export default function setup(relay) {
  const { h, icon, clear, toast } = relay.ui;

  function copyable(text, label) {
    const code = h("pre.vsc-code", h("code", text));
    const copy =
      h("button.icon-btn.sm.vsc-copy", { title: `Copy ${label}`, "aria-label": `Copy ${label}`, onclick: async () => {
        try {
          await navigator.clipboard.writeText(text);
          toast("Copied");
        } catch {  // (no clipboard on a page that isn't https or localhost: selected to copy by hand)
          const range = document.createRange();
          range.selectNodeContents(code);
          getSelection().removeAllRanges();
          getSelection().addRange(range);
        }
      } }, icon("copy", "sm"));
    return h("div.vsc-block", code, copy);
  }

  function step(number, title, ...body) {
    return h("section.vsc-step", h("h4", h("span.vsc-number", String(number)), title), ...body);
  }

  relay.settingsTab({ label: "VSCodium", icon: "code", render: async (pane) => {
    const [state, version] = await Promise.all([relay.api.relay.get("/api/state"), relay.api.get("version")]);
    // (Relay's address as the extension reaches it: on this machine, under Relay's own path when it has one)
    const base = new URL(relay.url("x"), location.href).pathname.replace(/\/plugins\/[^/]+\/static\/x$/, "");
    const address = `http://localhost:${state.server?.port || location.port || 8420}${base}`;
    const installed = h("p.small-text.vsc-said");
    const made = h("div.vsc-made");
    const name = h("input.input", { value: "VSCodium", maxlength: 80, "aria-label": "What will use it" });

    const install = h("button.btn.primary.small", { onclick: async () => {
      install.disabled = true;
      try {
        const done = await relay.api.post("install");
        const ok = done.installed.filter((d) => d.ok).map((d) => d.product).join(" and ");
        clear(installed,
          `Installed in ${ok} on this machine (${done.version}). In VSCodium: Developer: Reload Window.`);
      } catch (error) {
        clear(installed, h("span.error-text", error.message));
      } finally {
        install.disabled = false;
      }
    } }, "Install on this machine");

    const download = h("button.btn.small", { onclick: async () => {
      download.disabled = true;
      try {
        const got = await relay.api.get("extension");
        const bytes = Uint8Array.from(atob(got.data), (c) => c.charCodeAt(0));
        const link =
          h("a", { href: URL.createObjectURL(new Blob([bytes], { type: "application/zip" })), download: got.name });
        document.body.append(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(link.href), 10000);
      } catch (error) {
        toast(error.message, "error");
      } finally {
        download.disabled = false;
      }
    } }, `Download relay-${version.version}.vsix`);

    const make = h("button.btn.primary.small", { onclick: async () => {
      make.disabled = true;
      try {
        const app = await relay.api.relay.post("/api/app-passwords", { name: name.value || "VSCodium" });
        const here = `${location.origin}${base}`;  // (Relay as this browser reaches it: VSCodium on this computer)
        const link = (scheme) => `${scheme}://relay.relay/sign-in?url=${encodeURIComponent(here)}`
          + `&password=${encodeURIComponent(app.password)}`;
        clear(made,
          h("p.small-text", h("b", `“${app.name}”: copy it now, it isn't shown again.`),
            " Revoke it in Settings → Account."),
          h("div.vsc-row", h("a.btn.primary.small", { href: link("vscodium") }, "Sign in VSCodium on this computer"),
            h("a.small-text", { href: link("vscode") }, "or VS Code")),
          h("p.small-text", "Or in VSCodium: Relay: Sign in (the command palette, or the Relay view), with Relay's "
            + "address: on this computer the one this browser has; with Remote - SSH on Relay's machine, its own."),
          copyable(here, "this computer's address"),
          copyable(address, "the address on Relay's machine"),
          copyable(app.password, "the app password"));
      } catch (error) {
        toast(error.message, "error");
      } finally {
        make.disabled = false;
      }
    } }, "Make one");

    clear(pane,
      h("h3", "VSCodium"),
      h("p.lead", "Relay in VSCodium (and VS Code): your conversations, a chat with any of "
        + "Relay's agents (in an editor tab or the side bar), the agent's changes in Source "
        + "Control and the diff editor, the open file and its problems going along with what you "
        + "ask. While it runs, agents ask its language servers about the code (definitions, references, problems)."),
      step(1, "Where your folders are",
        h("p.small-text", h("b", "On your own computer: "), "a conversation in a folder there works on that computer. "
          + "The extension lets Relay's agents run commands and change files there while VSCodium runs (it asks you "
          + "once; then VSCodium asks before each command and change, a change shown as a diff), and keeps what they "
          + "changed, to review and undo."),
        h("p.small-text", h("b", "On this machine, with Remote - SSH: "), "the extension Open Remote - SSH "
          + "(jeanp413.open-remote-ssh, on Open VSX; in VS Code: Remote - SSH), then a folder in one of Relay's "
          + "workspace roots: a conversation there is a code task, as on Relay's own page.")),
      step(2, "Get Relay's extension",
        h("div.vsc-row", install, download),
        installed,
        h("p.small-text.muted", "Install puts it in the VSCodium server Remote - SSH left here. Downloaded: Extensions "
          + "view → … → Install from VSIX… Later versions of this plugin offer themselves in VSCodium.")),
      step(3, "Sign it in",
        h("p.small-text", "With an app password of its own:"),
        h("div.vsc-make", name, make),
        made));
  } });
}
