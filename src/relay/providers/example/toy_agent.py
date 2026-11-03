"""toy-agent: a tiny coding agent for the guide (docs/ADDING-A-PROVIDER.md), as a program Relay doesn't know.

It's what a real agent's CLI would be: a program with options, a login of sorts (a folder of its own), sessions it
keeps, a tool (it runs commands in $SHELL), and output in a format of its own, on purpose not Relay's:

    python3 toy_agent.py [--resume ID] [--model NAME]     the prompt on stdin

One JSON object per line, each with a "kind":

    {"kind": "hello", "conversation": ID, "model": NAME}          first: the session it's in
    {"kind": "think", "words": "…"}                               what it thinks
    {"kind": "say", "words": "…"}                                 its answer, a piece at a time
    {"kind": "use", "call": ID, "tool": "run", "args": {"command": "…"}}
    {"kind": "used", "call": ID, "result": "…", "failed": false}
    {"kind": "bye", "outcome": "ok"}                              last; "error" with "why" when it failed

What it answers: "OK" when asked for the word OK; a count, slowly, when asked to count; a command's output when
asked to run one (a line "run: COMMAND", or "Run this shell command …: COMMAND"); otherwise what it heard. Each
session remembers its turns (in TOY_AGENT_HOME, default ~/.toy-agent).
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path


def emit(**event):
    print(json.dumps(event), flush=True)


def main():
    parser = argparse.ArgumentParser(prog="toy-agent")
    parser.add_argument("--resume", help="a session to go on in")
    parser.add_argument("--model", default="toy-small", choices=["toy-small", "toy-large"])
    args = parser.parse_args()
    prompt = sys.stdin.read()
    home = Path(os.environ.get("TOY_AGENT_HOME") or Path.home() / ".toy-agent")
    session = args.resume or uuid.uuid4().hex[:12]
    kept = home / f"{session}.json"
    if args.resume and not kept.exists():
        emit(kind="bye", outcome="error", why=f"no session {args.resume}")
        return 1
    turns = json.loads(kept.read_text()) if kept.exists() else []
    emit(kind="hello", conversation=session, model=args.model)
    emit(kind="think", words="Let me see what's asked.")
    count = re.search(r"Count from 1 to (\d+)", prompt)
    command = re.search(r"^run: (.+)$", prompt, re.M) or re.search(r"Run this shell command[^:]*: (.+)$", prompt, re.M)
    if count:
        for n in range(1, int(count.group(1)) + 1):
            emit(kind="say", words=f"{n}\n")
            time.sleep(0.05)
    elif command:
        call = uuid.uuid4().hex[:8]
        emit(kind="use", call=call, tool="run", args={"command": command.group(1)})
        done = subprocess.run([os.environ.get("SHELL") or "/bin/sh", "-c", command.group(1)],
                              capture_output=True, text=True)
        emit(kind="used", call=call, result=done.stdout + done.stderr, failed=done.returncode != 0)
        emit(kind="say", words=f"It printed: {done.stdout.strip()}")
    elif "word OK" in prompt:
        emit(kind="say", words="OK")
    else:
        heard = " ".join(prompt.split())[:80]
        emit(kind="say", words=f"Toy agent here, turn {len(turns) + 1} of this session. You said: {heard}")
    home.mkdir(parents=True, exist_ok=True)
    kept.write_text(json.dumps([*turns, prompt[:200]]))
    emit(kind="bye", outcome="ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
