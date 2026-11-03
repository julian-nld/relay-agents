"""ACP: Relay's agents as one ACP agent (the Agent Client Protocol), for OpenHands, Zed, JetBrains, Toad and any other
ACP client. The forwarder is a program of its own, static/relay-acp.py, that a client starts: it speaks ACP to the
client and Relay's HTTP API (with an app password) to Relay. Served from static/, another machine downloads it with
an app password. This plugin adds its tab in Settings (static/index.js): the setups to copy."""


def setup(plugin):
    pass  # (nothing on the server: the forwarder uses Relay's own API)
