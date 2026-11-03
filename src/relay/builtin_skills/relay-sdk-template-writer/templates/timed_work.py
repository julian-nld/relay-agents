"""Keep an agent at a job for a while (/timed): each turn as it comes, until its time is up or it's done.

Change TASK and MINUTES: (at least, at most) keeps it at it for at least the first, one number lets it stop
when it's done. FOLDER: a folder for a code task (in a workspace root), "" for a chat. At the end: its turns,
how long it worked and what it cost. Its most stays within a script's time limit (Settings → Limits → Scripts,
2 hours by default): there the script stops, and its agent with it.
"""

import sys
import time

from relay_sdk import *

TASK = "Go through this project's TODO comments and fix the ones you can, one at a time."
MINUTES = (10, 20)
FOLDER = ""  # e.g. "/home/me/projects/site"; "": a chat
PROVIDER = None  # a provider's id (providers() lists them); None: the agent of your latest conversation
MODEL = None
ALLOW = True  # the answer when the agent asks permission while it works (nobody's here to ask)


def on_question(question):
    """What the agent asks while it works: a permission gets ALLOW, a form is skipped."""
    if question.kind == "permission":
        print(f"\n[It asks permission: {question.title}: {'yes' if ALLOW else 'no'}]", flush=True)
        return ALLOW
    print(f"\n[It asks: {question.title}: skipped]", flush=True)
    return None


def show(number, turn):
    """A turn's text as it comes, then which agent it was and what it took."""
    print(f"\n── Turn {number} ──\n", flush=True)
    for text in turn:
        print(text, end="", flush=True)
    print(f"\n({turn.provider} · {turn.model or 'its default model'} · {turn.elapsed_time or 0:.0f} s · "
          f"${turn.cost:.4f} · {turn.status})", flush=True)
    return turn


def main():
    try:
        chat = convo.create(title=TASK[:60], workdir=FOLDER or None, provider=PROVIDER, model=MODEL)
    except RelayError as error:
        print(f"Relay couldn't start it: {error}", flush=True)
        sys.exit(1)
    started = time.time()
    turns = [show(1, chat.send(TASK, stream=True, timed=MINUTES, on_question=on_question))]
    for turn in chat.follow(on_question=on_question):  # the turns after the first, until it's quiet
        turns.append(show(len(turns) + 1, turn))

    found = chat.get()
    ended = (found.get("timed") or {}).get("ended")
    print(f"\n{len(turns)} turn(s) in {(time.time() - started) / 60:.1f} minutes, "
          f"${sum(turn.cost for turn in turns):.4f}" + (f"; its timer: {ended}" if ended else ""), flush=True)
    if found.get("resume_at"):
        print("It waits for a usage limit; it goes on by itself at "
              + time.strftime("%H:%M", time.localtime(found["resume_at"])), flush=True)
    if any(turn.status == "failed" for turn in turns):
        sys.exit(1)


if __name__ == "__main__":
    main()
