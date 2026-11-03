"""Give an agent a job in a folder: what it says as it works, then each file it changed (committed, if you like).

Change FOLDER (on Relay's machine, in a workspace root: Settings → Workspaces) and TASK, or give them as the
script's arguments: the folder first, then the task. COMMIT = True commits the changes to the folder's git
repository when it's done; ISOLATE = True has the agent work on a branch of its own.
"""

import sys

from relay_sdk import *

FOLDER = ""  # e.g. "/home/me/projects/site"
TASK = "Read this project and add a short README.md: what it is, and how to run it."
PROVIDER = None  # a provider that works in folders; None: the agent of your latest conversation
MODEL = None
ISOLATE = False
COMMIT = False
COMMIT_MESSAGE = ""  # "": the task's first line
ALLOW = True  # the answer when the agent asks permission while it works (nobody's here to ask)


def on_question(question):
    """What the agent asks while it works: a permission gets ALLOW, a form is skipped."""
    if question.kind == "permission":
        print(f"\n[It asks permission: {question.title}: {'yes' if ALLOW else 'no'}]", flush=True)
        return ALLOW
    print(f"\n[It asks: {question.title}: skipped]", flush=True)
    return None


def main():
    folder = sys.argv[1] if len(sys.argv) > 1 else FOLDER
    task = " ".join(sys.argv[2:]) or TASK
    if not folder:
        print("Which folder? Set FOLDER at the top, or give it as the script's first argument.", flush=True)
        sys.exit(2)
    try:
        chat = convo.create(title=task.splitlines()[0][:60], workdir=folder, provider=PROVIDER, model=MODEL,
                            isolate=ISOLATE)
    except RelayError as error:
        print(f"Relay couldn't start it: {error}", flush=True)
        sys.exit(1)
    print(f"{chat.provider} · {chat.model or 'its default model'}, in {folder}\n", flush=True)

    answer = chat.send(task, stream=True, on_question=on_question)
    for text in answer:
        print(text, end="", flush=True)
    print()
    if answer.status != "succeeded":
        print(f"\nIt stopped: {answer.error or answer.status}", flush=True)
        sys.exit(1)

    changes = chat.changes()
    summary = changes["summary"]
    print(f"\nChanged: {summary['files']} file(s), +{summary['additions']} -{summary['deletions']}", flush=True)
    for changed in changes["files"]:
        print(f"  {changed['status']:<9} {changed['path']}  +{changed['additions']} -{changed['deletions']}")
    if changes.get("error"):
        print(f"(Relay couldn't read all of them: {changes['error']})")
    if COMMIT and summary["files"]:
        try:
            commit = chat.commit(COMMIT_MESSAGE or task.splitlines()[0][:72])
        except RelayError as error:
            print(f"\nNot committed: {error}", flush=True)
            sys.exit(1)
        print(f"\nCommitted: {commit[:12]}", flush=True)


if __name__ == "__main__":
    main()
