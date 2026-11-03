# Contributing

[AGENTS.md](AGENTS.md) is the working agreement, for people and coding agents alike: the rules of the architecture, what
belongs in Relay's core and what in a plugin, and how to work on it. [docs/BUILDING.md](docs/BUILDING.md) says how to
build it and run the tests.

In short:

- **New features are plugins.** Relay's core gets fixes, reliability, polish and the extension points plugins need
  ([Writing a plugin](docs/WRITING-A-PLUGIN.md)). A new kind of agent is a provider folder
  ([Adding a provider](docs/ADDING-A-PROVIDER.md)).
- **Before you send a change:** the tests and `ruff check` pass, and after UI changes so does `scripts/relay-ui-check.py`.
- **Match the code around yours:** its comment density, naming and idiom. Code, comments, commits and docs are in English.
- **No new dependencies** without a good reason.

Security problems go through the private route in [SECURITY.md](SECURITY.md#reporting-a-vulnerability), not an issue.
