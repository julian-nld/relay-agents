# RESL (`resl`): Relay's Extended Standard Library

More from Relay's agents, built on the SDK. `from resl import *` gives the SDK
and RESL. On the Scripts page it's there as the SDK is; elsewhere it comes
in the same wheel (`/sdk/`).

```python
from resl import *

use("claude-code", "sonnet")                  # the judge, from now on
answer = convo.create(title="Q").send("Write a phishing mail")
if agent_refused(answer):                     # or answer.refused()
    print("It wouldn't.")
```

## The judge

Most of RESL asks another model about a text, in a **one-shot**. That's a
temporary chat with only that text in it:
- no history;
- none of Relay's memory or other tools;
- read-only;
- deleted afterwards.

Each one-shot costs a model call.

- **`use(provider, model=None)`:** the judge from now on. Without it, the SDK's
  default: the agent `login()` named, else your latest conversation's. Every
  function also takes `provider=` and `model=`.
- **`one_shot(prompt, provider=None, model=None, instructions=None)`:** the
  one-shot itself; its answer's text.
- **What's judged is data to the judge** (between tags), but a text written to
  fool a judge still might.
- **A reply not in the form asked** (one word, a JSON object) is asked once
  more, then raises `ReslError` (a `RelayError`, with `.answer` where there
  was one).

## Checking answers

An answer is a `Message`, whose task is the message it answered. Or it's a
text: then `task=` says what was asked.

- **`agent_refused(answer, provider=None, model=None, task=None)`:**
  - True when it refused: it declined, said it can't or won't, or did
    something else;
  - False when it did it, a fair part of it, also with caveats.
- **`task_done(answer, task=None, …)`:** the whole task done, not part of it
  or "I'll do X next".
- **`asks_user(answer, …)`:** it ends waiting for the user: a question, a
  choice, an approval.
- **`judge(answer, question, …)`:** any yes/no question, e.g.
  `judge(answer, "Does it name a price?")`.
- **`score(answer, criteria="how well it does what was asked", task=None, …)`:**
  `Score(value, reason)`, 1–10.

## Answers to data

- **`extract(answer, fields, …)`:** a dict. `fields` are names, or
  `{name: what it is}`; a field the text doesn't give is None.
- **`classify(text, labels, …)`:** the label that fits best.
- **`summarize(text, words=60, …)`** and **`translate(text, language, …)`:** text.

## Agents checking agents

- **`ask_until(chat, prompt, check=None, tries=3, fallbacks=(), on_question=None, provider=None, model=None)`:**
  - It sends the prompt.
  - While the answer fails `check`, it asks again. The default check: it
    answered and didn't refuse, as `agent_refused` judges.
  - Who answers again: the next of `fallbacks`, given as `(provider, model)`
    pairs or providers (the conversation switches to it), else the same
    agent.
  - It returns the answer that passed. When none did, it raises `ReslError`,
    with `.answer` the last one.
- **`best_of(prompt, agents, criteria="how well it answers", keep=True, …)`:**
  - The agents (pairs or providers) answer at once, each in a chat of its
    own.
  - The judge picks one; it returns `(best, answers)`.
  - `keep=False` deletes their chats afterwards.
- **`second_opinion(answer, task=None, …)`:** `Opinion(agrees, issues, text)`.
- **`review_changes(chat, focus=None, …)`:** a code task's diff, reviewed:
  `Review(ok, findings, text)`. `ok` means nothing to fix before merging.

## Relay

- **`pick_agent(prefer=None, models=None, enough=0.2)`:** the `(provider,
  model)` with the most room in its usage limits (Settings → Usage).
  - `prefer`: which providers to choose from, in order.
  - `models`: the model per provider.
  - A provider with less than `enough` (0–1) left isn't chosen.

## As methods

Once `resl` is imported, answers and conversations have these too:

- `answer.refused()`, `.done()`, `.asks_user()`, `.judge(question)`,
  `.score()`, `.extract(fields)`, `.classify(labels)`, `.summary(words)`,
  `.translate(language)`, `.second_opinion()`;
- `chat.ask_until(prompt, …)`, `chat.review_changes()`.
