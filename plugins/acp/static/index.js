// The ACP forwarder's part of Relay's page: a tab in Settings with how to use Relay from an ACP client (OpenHands,
// Zed, JetBrains, Toad), an app password for it, and the setups to copy (docs/WRITING-A-PLUGIN.md).

export default function setup(relay) {
  const { h, icon, clear, toast } = relay.ui;

  function copyable(text) {
    const code = h("pre.acp-code", h("code", text));
    const copy = h("button.icon-btn.sm.acp-copy", { title: "Copy", "aria-label": "Copy", onclick: async () => {
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
    return h("div.acp-block", code, copy);
  }

  relay.settingsTab({ label: "ACP", icon: "plug", render: async (pane) => {
    const [state, plugins] =
      await Promise.all([relay.api.relay.get("/api/state"), relay.api.relay.get("/api/plugins")]);
    const here = plugins.plugins.find((p) => p.name === relay.name);
    const program = `${here.folder}/static/relay-acp.py`;
    const address = new URL(relay.url("relay-acp.py"), location.href);
    const base = address.href.replace(/\/plugins\/[^/]+\/static\/relay-acp\.py$/, "");
    let password = "relay_…";
    const name = h("input.input", { value: "ACP client", maxlength: 80, "aria-label": "What will use it" });
    const made = h("div.acp-made");
    const setups = h("div");

    function draw() {
      const env = `RELAY_URL=${base} RELAY_APP_PASSWORD=${password}`;
      clear(setups,
        h("h4", "On this machine"),
        copyable(`${env} python3 ${program}`),
        h("h4", "On another machine"),
        h("p.muted.small-text", "Relay's SDK and the program, then the same command:"),
        copyable(`pip install ${base}/sdk/relay_sdk-${state.version}-py3-none-any.whl\n`
          + `curl -fsSL -H "Authorization: Bearer ${password}" ${address.href} -o relay-acp.py\n`
          + `${env} python3 relay-acp.py`),
        h("h4", "Zed"),
        h("p.muted.small-text", "In its settings.json:"),
        copyable(JSON.stringify({ agent_servers: { Relay: { command: "python3", args: [program],
          env: { RELAY_URL: base, RELAY_APP_PASSWORD: password } } } }, null, 2)),
        h("h4", "OpenHands"),
        h("p.muted.small-text", "With its SDK: the conversation's secrets reach the program as its environment."),
        copyable(`from openhands.sdk import Conversation\nfrom openhands.sdk.agent import ACPAgent\n\n`
          + `agent = ACPAgent(acp_command=["python3", "${program}"])\n`
          + `conversation = Conversation(agent=agent, workspace="/home/you/projects/site",\n`
          + `                            secrets={"RELAY_URL": "${base}", "RELAY_APP_PASSWORD": "${password}"})`),
        h("h4", "JetBrains, Toad and other ACP clients"),
        h("p.muted.small-text", `The program: python3 ${program}; its environment: RELAY_URL and RELAY_APP_PASSWORD. `
          + "--provider ID (only that provider's models), --model M (the one to start with), --chat (always a chat), "
          + "--map FROM=TO (a folder a client in a container sees elsewhere), --skip-context (leave out a client's own "
          + "context, such as OpenHands' skills) and --ask-in-relay (what Relay asks you to allow stays in Relay: for "
          + "a client that would allow anything unasked, as OpenHands does) are optional."));
    }

    clear(pane,
      h("h3", "ACP"),
      h("p.lead", "Relay as an agent for any ACP client (OpenHands, Zed, JetBrains, Toad): one agent whose model "
        + "picker has all of Relay's agents. They work as they do here, with their logins in their boxes and their "
        + "commands in the folder's WorkBox. A session in a folder of Relay's workspace roots is a code task there, "
        + "else a chat; both show in Relay."),
      h("h4", "An app password for the client"),
      h("div.acp-make", name, h("button.btn.primary.small", { onclick: async () => {
        try {
          const app = await relay.api.relay.post("/api/app-passwords", { name: name.value || "ACP client" });
          password = app.password;
          clear(made,
            h("p.small-text", h("b", `“${app.name}”: in the setups below now. Copy it: it isn't shown again.`),
              " Revoke it in Settings → Account."));
          draw();
        } catch (error) {
          toast(error.message, "error");
        }
      } }, "Make one")),
      made,
      setups);
    draw();
  } });
}
