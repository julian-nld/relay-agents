"""Show what's left of each provider's usage limits, and give PROMPT to the one with the most room.

Prints each provider's windows (e.g. "5 hours: 62% left, resets Fri 14:30"). With PROMPT (or the script's
arguments) it then asks the provider with the most left of its tightest window, in a chat of its own. PREFER:
the providers to choose from; MODELS: the model per provider.
"""

import sys
import time

from relay_sdk import *

PROMPT = ""  # e.g. "Summarise what changed in Python 3.14"; "": only show the limits
PREFER = []  # e.g. ["claude-code", "opencode"]; []: any
MODELS = {}  # e.g. {"claude-code": "sonnet"}; a provider not in it: its default model
ENOUGH = 0.2  # one with less left than this (of its tightest window) isn't chosen
ALLOW = True  # the answer when the agent asks permission while it works (nobody's here to ask)


def on_question(question):
    """What the agent asks while it works: a permission gets ALLOW, a form is skipped."""
    return ALLOW if question.kind == "permission" else None


def room(row):
    """What's left of its tightest window, 0–1: 1 without windows (no limits Relay knows of); None when it
    couldn't be asked."""
    if row.get("error") and not row["windows"]:
        return None
    used = [window["used"] for window in row["windows"] if window.get("used") is not None]
    return 1 - max(used) if used else 1.0


def resets(moment):
    return time.strftime(", resets %a %H:%M", time.localtime(moment)) if moment else ""


def main():
    rows = limits(fresh=True)
    for row in rows:
        names = ", ".join([row["name"], *row.get("also", [])])
        print(f"{names}" + (f" ({row['plan']})" if row.get("plan") else ""), flush=True)
        if row.get("error"):
            print(f"  couldn't ask it: {row['error']}")
        for window in row["windows"]:
            if window.get("used") is not None:
                print(f"  {window['label']}: {100 * (1 - window['used']):.0f}% left{resets(window.get('resets_at'))}")
        if not row["windows"] and not row.get("error"):
            print("  no limits it tells of")

    prompt = " ".join(sys.argv[1:]) or PROMPT
    if not prompt:
        return
    chosen = [(room(row), row) for row in rows if not PREFER or row["provider_id"] in PREFER]
    chosen = [(left, row) for left, row in chosen if left is not None and left >= ENOUGH]
    if not chosen:
        print("\nNo provider has room enough now.", flush=True)
        sys.exit(1)
    order = {provider: number for number, provider in enumerate(PREFER)}
    left, row = max(chosen, key=lambda found: (found[0], -order.get(found[1]["provider_id"], 0)))
    provider = row["provider_id"]
    print(f"\nAsking {row['name']} ({100 * left:.0f}% left) …\n", flush=True)

    chat = convo.create(title=prompt[:60], provider=provider, model=MODELS.get(provider))
    answer = chat.send(prompt, stream=True, on_question=on_question)
    for text in answer:
        print(text, end="", flush=True)
    print()
    if answer.status != "succeeded":
        print(f"\nNo answer: {answer.error or answer.status}", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
