"""Ask several agents the same thing at once; a judge picks the best answer (RESL: best_of).

Change PROMPT (or give it as the script's arguments), AGENTS, CRITERIA and JUDGE. Prints the best answer, then
each agent's status, time and cost. KEEP = False deletes the agents' chats afterwards.
"""

import sys

from resl import *

PROMPT = "Explain what a race condition is, with a short example in Python."
AGENTS = [("opencode", "opencode/big-pickle"), ("claude-code", "sonnet")]  # (provider, model); "" its default
CRITERIA = "correct, clear, and as short as it can be"
JUDGE = ("opencode", "opencode/big-pickle")  # a cheap or free model: it judges once
KEEP = True


def main():
    use(*JUDGE)
    prompt = " ".join(sys.argv[1:]) or PROMPT
    print(f"{len(AGENTS)} agent(s) at work …", flush=True)
    try:
        best, answers = best_of(prompt, AGENTS, criteria=CRITERIA, keep=KEEP)
    except RelayError as error:
        print(f"No best answer: {error}", flush=True)
        sys.exit(1)
    print(f"\nBest: {best.provider} · {best.model or 'its default model'}\n", flush=True)
    print(best.response)
    print("\nAll of them:")
    for answer in answers:
        print(f"  {answer.provider} · {answer.model or 'its default model'}: {answer.status}, "
              f"{answer.elapsed_time or 0:.0f} s, ${answer.cost:.4f}")


if __name__ == "__main__":
    main()
