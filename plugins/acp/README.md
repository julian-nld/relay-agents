# ACP

Relay's agents as one ACP agent: any client of the [Agent Client Protocol](https://agentclientprotocol.com)
(OpenHands' `ACPAgent`, Agent Canvas, Zed, JetBrains, Toad, …) can work with them.
- **One agent, every Relay agent in its model picker** ("Claude Code · sonnet", "opencode · big-pickle", …), and
  Relay's modes in its mode picker. (OpenHands keeps the list but only offers switching for agents it knows by name:
  there `--model`, or its settings' model, picks one.)
- **They work as they do in Relay:** their logins in their ProviderBoxes, their commands in the folder's WorkBox.
- **A session is a Relay conversation:** a code task when the client's folder is in one of Relay's workspace roots
  (on Relay's machine), else a chat. It shows in Relay, with its changes in Review; loading a session again goes on
  with it.
- **Images and files you attach in the client** reach the agent as Relay attachments, as files attached on Relay's
  own page do.
- **Permission requests** Relay leaves to you go to the client (or stay in Relay, with `--ask-in-relay`); a question
  an agent asks with a form is answered in Relay.

The forwarder is a program the client starts, `static/relay-acp.py`: it speaks ACP over stdin and stdout, and
Relay's HTTP API with an app password (through Relay's Python SDK):

```sh
RELAY_URL=http://<relay>:8420 RELAY_APP_PASSWORD=relay_… python3 relay-acp.py [--provider ID] [--model M] [--chat]
```

- `--provider ID`, `--model M`: the agent a session starts with; with `--provider` only that provider's models.
- `--chat`: always a chat, whatever the client's folder.
- `--map /projects=/home/me/projects`: a client in a container sees Relay's folders elsewhere; its sessions there are
  code tasks in the real folder (OpenHands' plugin uses it).
- `--skip-context`: leave out what a client adds to a prompt for its own agent (OpenHands lists its own skills there,
  which Relay's agents can't use).
- `--ask-in-relay`: what Relay asks you to allow (a command on your own computer, a paid subagent) stays in Relay,
  and the client is told to answer it there. For a client that would allow anything unasked: OpenHands' ACP client
  allows every request by itself (its plugin uses this).

Its tab in Settings (ACP) makes the app password and has the setups to copy: this machine, another machine (the
program and the SDK downloaded from Relay), Zed, OpenHands.

To use it, put this folder (or a link to it) in Relay's plugins folder, then restart Relay:

```sh
ln -s ~/relay/plugins/acp ~/.local/share/relay/plugins/acp
```
