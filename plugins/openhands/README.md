# OpenHands

[OpenHands](https://openhands.dev)' Agent Canvas (its UI, its Agent Server and its automations) on this server, in a
container of Relay's, inside Relay and working with Relay's agents:
- **in Relay:** its page (the hand on the activity bar) is Canvas while it runs, behind Relay's own sign-in and at
  Relay's address (also through your HTTPS proxy or Tailscale). No key to copy, no first-start questions: the page
  tells Canvas its backend before it opens. Relay's address follows Canvas' (`/p/openhands/conversations/<id>`), so
  a reload or a bookmark comes back to the same conversation. "Open in a tab" opens it at `/canvas` on its own;
- **its bar** above Canvas: the Relay agent new conversations start with, Settings (the port, Update, its log) and
  Stop, and "Update to <version>" when a newer OpenHands is out (it looks once a day);
- **what Relay asks you** in one of its conversations (a question, something to allow) can't be shown in OpenHands:
  the bar says so with "Answer in Relay", the hand shows a count, and Relay's app in Canvas marks the conversation;
- **its agent is Relay:** through the [ACP plugin](../acp/README.md)'s forwarder, any of Relay's agents (picked in
  the page's Settings), with their logins in their ProviderBoxes and their commands in Relay's WorkBoxes;
- **your projects:** Relay's workspace roots are OpenHands' workspaces (`/projects` in the container); a conversation
  in a project is Relay's code task in that folder, with its changes in Review and its checkpoints. Your private
  scripts stay out of its sight, as they do for Relay's agents;
- **Relay in OpenHands:** a Relay app in Canvas' sidebar (installed and on by itself) lists OpenHands' conversations
  with their Relay tasks (working, to review, done; the agent; the files changed), with "Review in Relay" and "Back
  to Relay";
- **its page while it doesn't run:** Set up (an app password "OpenHands" and the image, about 4.4 GB), Start and
  Stop (remembered: it starts again with Relay when it was on), the agent, the port, Update, and its log when
  something went wrong;
- **its tab in Settings:** the agent and the port, and Remove.

It needs the ACP plugin's folder in the same clone (`plugins/acp`; it doesn't need to be on), podman, Relay listening
on more than 127.0.0.1 (the container reaches it as `host.containers.internal`), and Relay at its site's root (Canvas
knows no path but `/canvas`).

**How it runs:** `ghcr.io/openhands/agent-canvas` (pinned; Update moves it to the newest), as `relay-openhands`, under
rootless podman as you (`--userns=keep-id`), its state in Relay's plugin data, its port (8000) on 127.0.0.1 only:
the browser reaches it through Relay (`/canvas` for its UI, `/port/<port>/` for its API), so its API key is in its
page. Telemetry is off, and Canvas starts in Relay's light or dark.

**Good to know:**
- **The agent** a conversation starts with is the one in the bar (or Settings → Relay's agent). Canvas starts its
  conversations from its active agent profile, so the plugin keeps Relay's profile in step with it, and on. Canvas'
  own model picker shows the agent, but only switches agents OpenHands knows by name (Claude Code, Codex, Gemini,
  …), not Relay's.
- OpenHands makes a project a git repository (`git init`) when it isn't one.
- Choosing another agent profile in OpenHands replaces Relay; the page then offers "Use Relay's agents again".
- OpenHands' ACP client allows whatever an agent asks by itself, so what Relay asks you to allow (a command on your
  own computer, a paid subagent) stays in Relay instead (the forwarder's `--ask-in-relay`).
- New project folders are in Open Workspace once its page opens (and every five minutes otherwise).

To use it, put this folder (or a link to it) in Relay's plugins folder, then restart Relay:

```sh
ln -s ~/relay/plugins/openhands ~/.local/share/relay/plugins/openhands
```
