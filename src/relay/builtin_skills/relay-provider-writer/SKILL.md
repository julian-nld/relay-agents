---
name: relay-provider-writer
description: Add a provider Relay doesn't know, an agent's program, a chat service or a website, by writing its compatibility layer, a folder of its own in Relay's source (src/relay/providers/<name>/), with its tests. Use when asked to connect Relay to an agent, CLI, API or site it doesn't support yet, or to write a provider kind.
---

# Adding a provider to Relay

You write the part of Relay that connects it to a program it doesn't know: a provider kind. The whole of it goes in
**one folder**, `src/relay/providers/<name>/`, and you change nothing outside it. The guide is
`docs/ADDING-A-PROVIDER.md` in Relay's folder: **read it first, all of it.** It builds a complete layer step by
step, toy-agent (`src/relay/providers/example/`), and ends with a reference of every event, hook and field.

1. **Find where you work.** Two places are right:
   - **A folder Relay made for this** (Settings → Add provider → Add it with an agent): it has `CREATE.md` and
     `reference/` (Relay's source and the guide, `reference/docs/ADDING-A-PROVIDER.md`, read-only). The user
     installed the program in a box of its own and logged in there; its home is the provider's (`box_home()`).
     Follow `CREATE.md`: study the program with `repair_run` (in that box), hand in a plan with `provider_plan`
     and stop. Once it's approved, Relay copies the example to `relay/providers/<name>/`: work only there, and test
     with `repair_check` (Relay's whole check of it, in a box of its own) until it passes. The user tries it in a
     throwaway Relay and adds it.
   - **A repair's folder** (a provider's 🔧, or AgentOS's `provider.repair`): `REPAIR.md`, copies of its support
     files and `reference/`. Follow `REPAIR.md`: a plan first (`provider_plan`, the plan alone), then change only
     the files it names and test with `repair_check`.
   - **Relay's own folder,** with `docs/ADDING-A-PROVIDER.md` and `src/relay/providers/` (a code task there, when
     it's in a workspace root, or a terminal in it).
   - **Neither?** Say so. Don't guess another place.
2. **Does a kind fit already?** Check section 1 of the guide.
   - An OpenAI-style API, an ACP agent, or a model opencode supports needs no code. Tell the user which kind
     and what to fill in, and stop there.
3. **Study the program,** with what you can run here: its `--help`, its documentation, a few prompts when it's
   installed where you are.
   - **Answer the guide's questions (4.1)** in the folder's `README.md`: how it starts, what it prints, sessions,
     models, stopping, permissions, where its login is.
   - **When you can't run it,** say what you couldn't find out, and build on its documentation.
4. **Make the folder** from the example (in a folder Relay made for this, it's there already):
   - `cp -r src/relay/providers/example src/relay/providers/<name>`, and rename what's the example's (its imports
     of itself too: `relay.providers.example` becomes `relay.providers.<name>`);
   - the folder a Python name (`my_agent`); `KIND` lowercase with dashes (`my-agent`), not one Relay has;
   - drop `toy_agent.py` (the real program is installed on the machine), and `LISTED = False`.
5. **Write the layer** (`provider.py`), section by section as the guide does:
   - `FIELDS`, `EXAMPLE` and `check()`;
   - its box: `box_key`, `box_mounts` (its program read-only, its login writable) and `env`; a program installed
     in the provider's own home needs no mounts (`home_program()` finds it);
   - `start()`, using `context.agent_prompt`;
   - `events()`, translating every kind of line the program prints;
   - `interrupt()`;
   - then what it has of permissions, models, efforts, usage and limits.

   Keep to the rules in 4.6: a `session` event, exactly one `done` last, and an `error` that says why before
   `done: failed`.
6. **Test it** in `tests/test_<name>.py` (the example's tests show how):
   - the translation, checked with `conformance.validate_events`;
   - how a turn starts, with a fake `context.box`;
   - a fake of the program for a whole turn, and `conformance.check(provider, "", tmp_path)`;
   - run with `PYTHONPATH=vendor-dev python3 -m pytest -q src/relay/providers/<name>` (in Relay's folder; with
     the suite's other tests when you can).
7. **Hand it over:**
   - **What you made,** and what you couldn't try.
   - **What the user does next:**
     - restart Relay (it finds the folder when it starts; one with a mistake is left out and said);
     - Settings → Providers → Add provider → the new kind;
     - then the provider's shield, **Check it fully** (or `./relay provider-check <id>`).
   - **The folder's `README.md`** keeps what you learned, for whoever repairs it later.

Never change Relay's own files for it. If the layer truly needs something Relay doesn't offer (a hook in
`providers/base.py`, a field on `Context`), say what and why instead: that's a change to Relay, the user's to make.
