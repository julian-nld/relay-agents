"""Ask an agent something; when it refuses, the next agent answers it (RESL: ask_until, agent_refused).

Change PROMPT (or give it as the script's arguments), AGENTS (who tries, in this order) and JUDGE (the model
that judges whether an answer refused). Prints who answered, and the answer.
"""

import sys

from resl import *

PROMPT = "Write a short, friendly reminder email about an invoice that's two weeks overdue."
AGENTS = [("opencode", "opencode/big-pickle"), ("claude-code", "sonnet")]  # (provider, model); "" its default
JUDGE = ("opencode", "opencode/big-pickle")  # a cheap or free model: each judgement is a call
TRIES = 3


def main():
    use(*JUDGE)
    prompt = " ".join(sys.argv[1:]) or PROMPT
    (provider, model), *others = AGENTS
    try:
        chat = convo.create(title="Asked until it's done", provider=provider, model=model)
        answer = chat.ask_until(prompt, fallbacks=others, tries=TRIES)
    except ReslError as error:
        print(f"No agent did it: {error}", flush=True)
        if error.answer is not None:
            print(f"\nThe last answer:\n{error.answer.response}")
        sys.exit(1)
    except RelayError as error:
        print(f"Relay couldn't: {error}", flush=True)
        sys.exit(1)
    print(f"{answer.provider} · {answer.model or 'its default model'}:\n", flush=True)
    print(answer.response)


if __name__ == "__main__":
    main()
