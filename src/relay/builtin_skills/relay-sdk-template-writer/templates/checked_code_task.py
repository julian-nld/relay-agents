"""Give an agent a job in a folder, then let another model check it before a commit (RESL).

task_done says whether it's done, review_changes whether anything needs fixing; it commits only when both say yes
and COMMIT is True. Change FOLDER (on Relay's machine, in a workspace root) and TASK, or give them as the
script's arguments: the folder first, then the task. AGENT works, JUDGE checks. Exits with 1 when the work isn't
done or needs fixing.
"""

import sys

from resl import *

FOLDER = ""  # e.g. "/home/me/projects/site"
TASK = "Add a function that tells whether a string is a palindrome, with a test for it."
AGENT = (None, None)  # (provider, model) of a provider that works in folders; None: your latest conversation's
JUDGE = ("opencode", "opencode/big-pickle")  # a cheap or free model: it checks twice
FOCUS = "correctness, and tests that really test it"
COMMIT = False
ALLOW = True  # the answer when the agent asks permission while it works (nobody's here to ask)


def on_question(question):
    """What the agent asks while it works: a permission gets ALLOW, a form is skipped."""
    return ALLOW if question.kind == "permission" else None


def main():
    folder = sys.argv[1] if len(sys.argv) > 1 else FOLDER
    task = " ".join(sys.argv[2:]) or TASK
    if not folder:
        print("Which folder? Set FOLDER at the top, or give it as the script's first argument.", flush=True)
        sys.exit(2)
    use(*JUDGE)
    try:
        chat = convo.create(title=task.splitlines()[0][:60], workdir=folder, provider=AGENT[0], model=AGENT[1])
        answer = chat.send(task, stream=True, on_question=on_question)
        for text in answer:
            print(text, end="", flush=True)
        print()
        if answer.status != "succeeded":
            print(f"\nIt stopped: {answer.error or answer.status}", flush=True)
            sys.exit(1)
        done = answer.done()
        review = chat.review_changes(focus=FOCUS)
    except RelayError as error:
        print(f"\nIt didn't work out: {error}", flush=True)
        sys.exit(1)
    print(f"\nDone: {'yes' if done else 'no'}. The review: {'fine' if review.ok else 'something to fix'}. "
          f"{review.text}", flush=True)
    for finding in review.findings:
        print(f"  - {finding}")
    if not (done and review.ok):
        sys.exit(1)
    if COMMIT:
        print(f"Committed: {chat.commit(task.splitlines()[0][:72])[:12]}")


if __name__ == "__main__":
    main()
