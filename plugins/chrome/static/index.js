// The Chrome plugin's part of Relay's page: a tab in Settings with the steps to Relay in Chrome. Download the extension
// (unzipped, loaded at chrome://extensions), sign it in with an app password of its own, and see whether it's there.

export default function setup(relay) {
  const { h, icon, clear, toast } = relay.ui;

  function copyable(text, label) {
    const code = h("pre.chr-code", h("code", text));
    const copy =
      h("button.icon-btn.sm.chr-copy", { title: `Copy ${label}`, "aria-label": `Copy ${label}`, onclick: async () => {
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
    return h("div.chr-block", code, copy);
  }

  function step(number, title, ...body) {
    return h("section.chr-step", h("h4", h("span.chr-number", String(number)), title), ...body);
  }

  relay.settingsTab({ label: "Chrome", icon: "globe", render: async (pane) => {
    const version = await relay.api.get("version");
    // (Relay's address as this browser has it: the extension runs in a browser like this one)
    const base = new URL(relay.url("x"), location.href).pathname.replace(/\/plugins\/[^/]+\/static\/x$/, "");
    const here = `${location.origin}${base}`;
    const made = h("div.chr-made");
    const name = h("input.input", { value: "Chrome", maxlength: 80, "aria-label": "What will use it" });
    const connected = h("p.small-text.chr-said");

    const download = h("button.btn.primary.small", { onclick: async () => {
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
    } }, `Download relay-chrome-${version.version}.zip`);

    const make = h("button.btn.primary.small", { onclick: async () => {
      make.disabled = true;
      try {
        const app = await relay.api.relay.post("/api/app-passwords", { name: name.value || "Chrome" });
        clear(made,
          h("p.small-text", h("b", `“${app.name}”: copy it now, it isn't shown again.`),
            " Revoke it in Settings → Account."),
          h("p.small-text", "In the extension's settings (they open when it's installed; later: "
            + "right-click its button → Options), paste Relay's address and the app password, then Save and connect."),
          copyable(here, "Relay's address"),
          copyable(app.password, "the app password"));
      } catch (error) {
        toast(error.message, "error");
      } finally {
        make.disabled = false;
      }
    } }, "Make one");

    async function check() {
      try {
        const found = await relay.api.get("status");
        clear(connected, found.connected
          ? h("span", icon("check", "sm"), " Your Chrome is connected: agents can use it now.")
          : "No Chrome is connected now: start Chrome with the extension signed in.");
      } catch (error) {
        clear(connected, h("span.error-text", error.message));
      }
    }

    clear(pane,
      h("h3", "Chrome"),
      h("p.lead", "Relay next to every page in Chrome: chat with any of Relay's agents about a page, a selection or a "
        + "screenshot, run your scripts, and let agents read and use your own tabs, where you're signed in, each step "
        + "allowed by you."),
      step(1, "Get Relay's extension",
        h("div.chr-row", download),
        h("p.small-text", "Unzip it, open chrome://extensions, turn on Developer mode, choose Load unpacked and pick "
          + "the relay-chrome folder. A later version: unzip it over that folder, then click the extension's reload "
          + "button there.")),
      step(2, "Sign it in",
        h("p.small-text", "With an app password of its own:"),
        h("div.chr-make", name, make),
        made),
      step(3, "Agents in your browser",
        h("p.small-text", "Agents in any chat then have tools for your Chrome (chrome_read, "
          + "chrome_click, …) while it runs. Each step shows first as a card, in the side "
          + "panel or a notification: Deny, Allow, or Always on that site. A page can hold "
          + "text that tries to talk an agent into something, so keep Always for sites you trust."),
        h("div.chr-row", connected, h("button.btn.small", { onclick: check }, "Check again"))));
    check();
  } });
}
