# The example layer: toy-agent

The worked example of [docs/ADDING-A-PROVIDER.md](../../../../docs/ADDING-A-PROVIDER.md): a provider kind
for a program Relay doesn't know, built in this folder alone. It isn't offered in Settings → Add provider,
but a template of it works, to try it:

```json
{"id": "toy", "name": "Toy agent", "kind": "example", "options": {"models": ["toy-small", "toy-large"]}}
```

| File | What it is |
|---|---|
| `toy_agent.py` | **The program:** a tiny coding agent with options, sessions it keeps, a tool (it runs commands in `$SHELL`) and a JSON format of its own. A real one would be installed on the machine; this one comes along so the example runs anywhere. |
| `provider.py` | **The layer:** `ToyAgentProvider` (the kind: options, check, its box, how a turn starts) and `ToyRun` (its output as Relay's events, and stopping it). |
| `__init__.py` | **What the folder adds:** `KINDS = (ToyAgentProvider,)`. Relay finds the folder by it. |
| `tests/test_example.py` | **Its tests:** the translation, the program's arguments, and the whole check (Settings → Providers → Check it fully) against it. |

Its support layer, what a Repair copies, is this folder.
