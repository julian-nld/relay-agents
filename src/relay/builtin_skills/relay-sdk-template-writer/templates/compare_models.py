"""Ask several agents the same question at once: each answer as it's done, then a table of their times and costs.

Change PROMPT (or give it as the script's arguments) and AGENTS: (provider, model) pairs, "" for a provider's
default model. Each gets a chat of its own, and they work at the same time. An agent that isn't there is
said, with the models its provider has.
"""

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from relay_sdk import *

PROMPT = "Write a Python function that tells whether a string is a palindrome, ignoring case and punctuation."
AGENTS = [
    ("opencode", "opencode/big-pickle"),
    ("claude-code", "sonnet"),
]
INSTRUCTIONS = ""  # the same for each, e.g. "Answer in at most 200 words."
ALLOW = True  # the answer when an agent asks permission while it works (nobody's here to ask)


def on_question(question):
    """What an agent asks while it works: a permission gets ALLOW, a form is skipped."""
    return ALLOW if question.kind == "permission" else None


def there(provider, model):
    """Whether Relay has that agent; else why not, with what it has."""
    enabled = {found["id"] for found in providers() if found.get("enabled")}
    if provider not in enabled:
        return f"there's no provider {provider} (there are: {', '.join(sorted(enabled))})"
    known = [found["id"] for found in models(provider)]
    if model and known and model not in known:
        return f"{provider} has no model {model} (it has: {', '.join(m for m in known if m)})"
    return None


def ask(provider, model, prompt):
    chat = convo.create(title=f"Compared: {model or provider}", provider=provider, model=model,
                        instructions=INSTRUCTIONS)
    return chat.send(prompt, on_question=on_question)


def main():
    prompt = " ".join(sys.argv[1:]) or PROMPT
    agents = []
    for provider, model in AGENTS:
        try:
            missing = there(provider, model)
        except RelayError as error:
            missing = f"{provider}: {error}"
        if missing:
            print(f"Left out: {missing}", flush=True)
        else:
            agents.append((provider, model))
    if not agents:
        sys.exit(1)
    print(f"{len(agents)} agent(s) at work …", flush=True)

    table = []
    with ThreadPoolExecutor(max_workers=len(agents)) as pool:
        asked = {pool.submit(ask, provider, model, prompt): (provider, model) for provider, model in agents}
        for done in as_completed(asked):
            provider, model = asked[done]
            name = f"{provider} · {model or 'its default model'}"
            try:
                answer = done.result()
            except RelayError as error:
                print(f"\n══ {name}: {error}", flush=True)
                table.append((name, "failed", 0, 0.0, 0))
                continue
            print(f"\n══ {name} ══\n", flush=True)
            print(answer.response if answer.status == "succeeded" else f"(no answer: {answer.error or answer.status})",
                  flush=True)
            table.append((name, answer.status, answer.elapsed_time or 0, answer.cost, len(answer.response)))

    print("\n" + f"{'agent':<40} {'status':<12} {'seconds':>8} {'cost':>9} {'characters':>11}")
    for name, status, seconds, cost, length in table:
        print(f"{name[:40]:<40} {status:<12} {seconds:>8.0f} {cost:>9.4f} {length:>11}")
    if all(row[1] != "succeeded" for row in table):
        sys.exit(1)


if __name__ == "__main__":
    main()
