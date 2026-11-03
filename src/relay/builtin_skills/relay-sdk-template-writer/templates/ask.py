"""Ask an agent one question: its answer as it comes, then how long it took, its cost and the tools it called.

Change PROMPT, or give the question as the script's arguments. PROVIDER and MODEL choose the agent (None: the
agent of your latest conversation); INSTRUCTIONS go with the question, e.g. "Answer in Dutch."
"""

import sys

from relay_sdk import *

PROMPT = "In three sentences: what is a race condition, and how do you avoid one?"
PROVIDER = None  # a provider's id, e.g. "opencode" (providers() lists them); None: your latest conversation's
MODEL = None  # e.g. "opencode/big-pickle" (models(PROVIDER) lists them); None: the provider's default
INSTRUCTIONS = ""
TITLE = "Asked by a script"
ALLOW = True  # the answer when the agent asks permission while it works (nobody's here to ask)


def on_question(question):
    """What the agent asks while it works: a permission gets ALLOW, a form is skipped."""
    if question.kind == "permission":
        print(f"\n[It asks permission: {question.title}: {'yes' if ALLOW else 'no'}]", flush=True)
        return ALLOW
    print(f"\n[It asks: {question.title}: skipped]", flush=True)
    return None


def main():
    prompt = " ".join(sys.argv[1:]) or PROMPT
    try:
        chat = convo.create(title=TITLE, provider=PROVIDER, model=MODEL, instructions=INSTRUCTIONS)
    except RelayError as error:
        print(f"Relay couldn't start it: {error}", flush=True)
        sys.exit(1)
    print(f"{chat.provider} · {chat.model or 'its default model'}\n", flush=True)

    answer = chat.send(prompt, stream=True, on_question=on_question)
    for text in answer:
        print(text, end="", flush=True)
    print()
    if answer.status != "succeeded":
        print(f"\nNo answer: {answer.error or answer.status}", flush=True)
        sys.exit(1)
    tools = ", ".join(call["name"] for call in answer.tools_called) or "none"
    print(f"\n{answer.elapsed_time or 0:.0f} s · ${answer.cost:.4f} · tools: {tools}", flush=True)


if __name__ == "__main__":
    main()
