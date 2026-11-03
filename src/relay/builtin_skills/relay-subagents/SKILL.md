---
name: relay-subagents
description: Hand work to other AI agents (another provider, model or mode) through Relay and get their answers back. Use for independent second opinions, to split work into parts that run at the same time, or to give simple parts to cheaper models.
access: read
---

# Subagents through Relay

Relay can run other AI agents for you: any provider, model and mode it has.
Each subagent starts without this conversation, so give it everything it
needs in its task.

## How

Relay told you which route you have. As tools of its MCP server "relay"
(`agents`, `delegate`, `wait`), call them directly. Otherwise, with a shell,
use the `relay` command whose full path Relay told you:

```sh
relay agents                                   # providers, their models, efforts and modes
relay delegate --provider ID --model ID --mode ID --label "Devil's advocate" <<'TASK'
The task, with all the context the subagent needs.
TASK
relay wait                                     # the answers that are in (up to 100 s); run again for the rest
relay delegate --provider ID --wait <<'TASK'    # one subagent, answer right away
…
TASK
relay tell ID "Hurry up: answer with what you have"   # interrupts it; then relay wait ID
```

In a chat without a shell, the same comes as tools: `relay_agents`,
`delegate` and `wait`.

`delegate` returns an id within seconds; start several before waiting, and
they work at the same time. A subagent whose model fails right away is
reported by `delegate` itself, and one that fails later with the next result
of any of Relay's tools: then pick another model.

`tell` talks to a subagent: one still working is interrupted and goes on
with your message (to hurry it, or steer it); a finished one gets it as a
follow-up, with its earlier work in mind. Collect the new answer with `wait`.

`--effort` sets how hard a subagent thinks (the model's efforts, from
`relay agents`); in Fast mode subagents think as little as their model
allows unless you say otherwise.

## When it helps

- **Independent opinions**: one subagent argues for an idea, one against, one
  only weighs costs and benefits. Because none sees the others, their
  answers don't converge. Then draw your own conclusion.
- **Splitting work**: parts that don't depend on each other, e.g. one module
  each, or research while you build.
- **Cheaper models for simple parts**: boilerplate, renames, tests for code
  that's settled. `relay agents` shows what's there.

## Take care

- Check what comes back: subagents make mistakes, and cheaper models more so.
- In a code task, a subagent that may change files works in a copy of your
  folder; its changes come into yours when it's done (its answer says which
  files). If they conflict with yours, or it didn't finish, you get its diff
  and decide: `relay merge ID` applies it with conflict markers to sort out,
  `relay tell ID` lets it go on (with your current files in its copy), or
  take what you need yourself. Parts that don't touch the same files merge
  best. Read-only modes (e.g. `plan`) suit reviewers; they look at your
  folder itself.
- Wait for your subagents before you finish your answer: they stop when you do.
- A few at a time is plenty; `relay agents` says how many Relay runs at once
  and per answer (the user sets that).
- A local server (e.g. Ollama) runs one model at a time: give subagents there
  the model that's already working, or one elsewhere.
- `relay agents` names models that failed on their last run; avoid them.
