---
name: relay-sdk-template-writer
description: Write a Python script for Relay's SDK, a template the user runs and adapts on Relay's Scripts page. It starts chats or code tasks, sends them prompts, follows the answers, and reviews and commits changes. Use when asked for an SDK script or template, or to automate work with Relay's agents.
---

# Writing a script for Relay's SDK

The user runs these on Relay's **Scripts** page. Each is one `.py` file in their
scripts folder, which has two parts:
- `private/`: theirs alone, out of every agent's sight;
- `public/`: for agents too. Its README.md starts with "# Relay's scripts".

You write in `public/`. When this task's folder has that README, you're in it
(the page's **With an agent** starts you there). A script runs in its folder's
WorkBox, **signed in as them**, so it needs no password and no `login()`. Write
it so that they can read it, change a few settings at its top, and run it
again.

1. **Find out what it's for:**
   - what goes in (prompts, a folder, which agents);
   - what comes out (printed, a file, a commit);
   - when it's done.

   Decide what you sensibly can; ask only the rest.
2. **Start from the closest template** in `templates/` next to this file (read it):
   - `ask.py`: one question to one agent;
   - `code_task.py`: a job in a folder, its changes, a commit;
   - `timed_work.py`: an agent kept at it for a while, each turn followed;
   - `compare_models.py`: the same prompt to several agents, side by side;
   - `room_left.py`: the agent whose plan has room left;
   - `service_watch.py`: the live services (Production boxes) looked at, a
     notification when one isn't well, a restart if asked;
   - with RESL (below):
     - `refusals.py`: the next agent when one refuses;
     - `best_answer.py`: several agents, a judge picks;
     - `extract_data.py`: an answer as data;
     - `checked_code_task.py`: a code task checked by another model before a
       commit.
3. **Its shape:**
   - **A docstring:** what it does, what to change, what it prints.
   - **The settings at the top, in CAPITALS:** prompts, provider and model,
     folder. A provider is its id (`providers()` lists them); a model is one of
     `models(provider)`'s ids, and `""` is its default.
   - **`main()`** and `if __name__ == "__main__": main()`. On failure, exit
     with a code other than 0 (`sys.exit(1)`); arguments come in `sys.argv`.
   - **Say what happens as it goes:** `print(…, flush=True)`. The page shows
     the output as it's written.
   - **Imports:** whatever it needs. Relay's: `from relay_sdk import *`, or
     `from resl import *` (RESL, which gives the SDK too).
   - **Packages from PyPI:** name them in a `# /// script` block at the top
     (PEP 723). Relay installs them with pip before each run, in the WorkBox
     the script runs in (private or public, wherever it's moved):

     ```python
     # /// script
     # dependencies = ["requests", "beautifulsoup4"]
     # ///
     ```

     To try it as you write it, install them yourself (`pip3 install
     requests beautifulsoup4`).
   - **One file:** the scripts' folder isn't on a script's import path, so
     each script is whole in its one file.
4. **The SDK:** `reference.md` next to this file has all of it. In short:
   - **Creating:** `convo.create(title=, workdir=, provider=, model=,
     agent_mode=, instructions=, tools=)` returns a Conversation (its id as a
     string too). With `workdir` it's a code task; a folder in a workspace
     root, on Relay's machine.
   - **Sending:** `chat.send(prompt)` returns a Message: `response`,
     `elapsed_time`, `tools_called`, `cost`, `text` (Markdown) and `status`.
     With `stream=True`, iterate it for the text as it comes.
   - **Changing the agent:** `chat.switch(provider=, model=)`.
   - **More turns:** `send(…, timed=minutes)`. `chat.follow()` gives each turn
     as it comes, the one going now first, until it's quiet: after reading the
     first answer, the turns after it.
   - **Code tasks:** `changes()`, `commit(message)`, `rewind("start")`.
   - **A temporary chat:** `convo.create(temporary=True)`: in no list, without
     memory; `delete()` it when done.
   - **Relay's other parts:**
     - **`scripts`:** the user's scripts; `scripts.run(name, args)` gives a run
       to iterate for its output; `scripts.write(description)`.
     - **`tasks`:** scheduled ones, `tasks.create(name, "daily 08:00",
       prompt=… or script=…)`.
     - **`services`:** the live ones (Production boxes): `status()`, `logs()`,
       `restart()`, ….
     - **`notify(text, title=)`:** to the user's phone.
   - **RESL** (`resl.md` next to this file has all of it): another model judges
     a text in a one-shot, a temporary chat with only that text in it.
     - **Checks:** did the agent refuse (`agent_refused`), finish
       (`task_done`), or wait for the user (`asks_user`)? Also `judge(answer,
       question)` and `score`.
     - **Data:** `extract`, `classify`, `summarize`, `translate`.
     - **Agents checking agents:** `ask_until` (the next agent while one
       refuses), `best_of`, `second_opinion`, `review_changes`.
     - **Picking an agent:** `pick_agent`, the one with room in its limits.
     - **Methods:** the same on answers and conversations, e.g.
       `answer.refused()`.
5. **What bites:**
   - **Questions:** an agent may ask one (a form or a permission). Pass
     `on_question=` (return the answer), or it waits for Relay's time for
     questions.
   - **Turns after the first** (a timed request's, a fallback agent's at a
     usage limit, the queue's) come with `follow()`, not with `send()`.
   - **Cost:** for trying a script out, a free model is kindest: opencode's
     `opencode/big-pickle` and its `…-free` ones. Leave the user's choice in
     the settings. Each RESL judgement is a model call too. Set the judge with
     `use(provider, model)` in the settings, a cheap or free model; in a loop,
     judge only what needs it.
   - **Don't destroy:** don't delete conversations, discard work or commit
     unless the script's purpose is that, and its settings say so
     (`COMMIT = False` by default). The same goes for a live service's
     restart, rollback or restore (`RESTART = False`).
   - **Notifications:** `notify()` is for what the user wants to hear of, not
     each step: once per run, at its end, is plenty.
   - **The VPN:** `uses_vpn` puts a folder in a VPN tunnel for good;
     `chat.vpn = None` undoes it.
   - **Stop:** Stop on the Scripts page (or its time limit, Settings → Limits)
     ends the script at once: no `finally` or cleanup of its own runs. Relay
     stops the agents of the conversations it started, which the page lists
     with its run. Agents it sent on (`send(…, stream=True)` without waiting)
     go on after a script that ended by itself.
6. **Save and check it:**
   - save it as `<name>.py` at the top of `public/`: lowercase, words joined by
     `_`. A script of that name there already is the user's: never write over
     it, choose another name (`<name>_2.py`). Working in another folder (no
     such README), save it there, and tell the user to copy it into their
     scripts' `public/` or `private/`;
   - check it with `python3 -c "import ast, sys; ast.parse(open(sys.argv[1]).read())" <name>.py`
     (`py_compile` leaves a `__pycache__` folder in `public/`);
   - tell the user its name and what to change: they run it from the Scripts
     page.

   You can't run it against Relay yourself: it needs the user's session.
