"""Ask an agent, then turn its answer into data: fields with extract, a label with classify (RESL).

Change PROMPT (or give it as the script's arguments), FIELDS ({name: what it is}), LABELS, AGENT and JUDGE.
Prints the answer, then its fields as JSON and its label.
"""

import json
import sys

from resl import *

PROMPT = "Recommend one laptop for programming that costs less than 1500 euros: its name, its price and why."
FIELDS = {"name": "the laptop's name", "price": "its price in euros, as a number", "reason": "why, in one sentence"}
LABELS = ["budget", "mid-range", "high-end"]
AGENT = ("opencode", "opencode/big-pickle")  # (provider, model): who answers
JUDGE = ("opencode", "opencode/big-pickle")  # who reads the answer: each reading is a call


def main():
    use(*JUDGE)
    try:
        chat = convo.create(title="Asked for data", provider=AGENT[0], model=AGENT[1])
        answer = chat.send(" ".join(sys.argv[1:]) or PROMPT)
        if answer.status != "succeeded":
            print(f"No answer: {answer.error or answer.status}", flush=True)
            sys.exit(1)
        print(f"{answer.response}\n", flush=True)
        print(json.dumps(extract(answer, FIELDS), indent=2, ensure_ascii=False), flush=True)
        print(f"Class: {classify(answer, LABELS)}")
    except RelayError as error:
        print(f"It didn't work out: {error}", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
