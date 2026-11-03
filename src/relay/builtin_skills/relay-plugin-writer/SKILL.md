---
name: relay-plugin-writer
description: Write a Relay plugin, a folder in Relay's plugins folder (~/.local/share/relay/plugins/<name>/) that adds to Relay without changing its code: buttons, pages and Settings tabs on its page, API routes, background tasks, a hook for runs that end, and tools for agents. Use when asked to add a feature to Relay itself, to extend Relay, or to write a plugin for it.
---

# Writing a Relay plugin

Relay's core is complete: anything new comes as a **plugin**, a folder Relay loads when it starts. You write that
folder and change nothing of Relay's own code. The guide is `reference/WRITING-A-PLUGIN.md`, next to this file (in
Relay's folder it's `docs/WRITING-A-PLUGIN.md`): **read it first, all of it.** It builds a complete plugin step by
step, Notes (`reference/notes/`, Relay's `plugins/notes/`), and ends each part with a reference of the whole
contract.

1. **Is it a plugin?** A plugin adds to the page (buttons, pages, Settings tabs), to the server (API routes,
   background tasks, a hook for runs that end) and tools for agents.
   - **A provider** (an agent's program, a chat API) isn't a plugin: that's the relay-provider-writer skill.
   - **A script** that runs now and then, with Relay's SDK, may be all that's needed: the Scripts page.
   - **Changing what Relay's own screens show,** or its own tools, isn't possible with a plugin. Say so.
2. **Find where you work.** The plugin's folder is `<RELAY_HOME>/plugins/<name>/`, `~/.local/share/relay/plugins/`
   by default.
   - **That folder isn't reachable from here?** In a WorkBox it usually isn't. Write the plugin in the project's
     folder instead, and tell the user to link or copy it there.
   - **The name:** lowercase letters, digits and `_`. It's the prefix of its tools, its API and its pages.
3. **Start from the example:** copy `reference/notes/` and make it yours.
   - **`plugin.json`:** its `label`, `description`, `version`, and `"api": 1`.
   - **`__init__.py`:** `def setup(plugin)`, with only what the guide's `plugin` table has.
   - **`static/index.js`:** `export default function setup(relay)`, with only what the guide's `relay` table has.
   - **`static/style.css`:** with Relay's CSS variables.
   - **A `README.md`:** what it does, and how to install it.
4. **Keep to the contract.** `plugin`, the page's `relay`, `relay.plugins`' `PluginError`, `Call` and `ToolCall`,
   and Relay's HTTP API (`plugin.sdk`) stay. Anything else in Relay's code may change.
   - **Plain functions:** routes, tools, the hook and tasks are `def`, not `async def`. They run in the plugin's own
     threads, at the same time as each other.
   - **Data:** keep it in `plugin.store` (it needs no lock), or in files in `plugin.data`. Never keep it in the
     plugin's own folder, which a new version replaces.
   - **`setup()` only registers:** no network calls or slow work there.
   - **Errors:** raise `PluginError` with what the user or the agent should hear. For a polite "no" from a tool,
     answer with text.
   - **A tool's description** is all an agent goes by: say what it's for and when to use it.
   - **A tool's arguments are untrusted.** A path goes through `call.within(path)` before it's opened.
5. **Test what you can.**
   - **Its Python and JavaScript:** that they parse (`python3 -m py_compile`, `node --check`), and what you can
     run of it without Relay.
   - **Where Relay's folder is reachable:** `./relay plugins check <folder>` loads it as Relay would and lists what
     it adds, or says what's wrong.
6. **Hand it over:**
   - **What you made,** and what you couldn't try.
   - **What the user does next:**
     - put the folder (or a link to it) in the plugins folder, outside the workspace roots;
     - restart Relay (Settings → Plugins → Restart Relay);
     - check Settings → Plugins: loaded, or what's wrong.
   - **That a plugin runs with Relay's own rights:** the user should know what it does.
