# VSCodium

Relay in VSCodium (and VS Code), as an extension of its own. It gives you your conversations, a chat with any of
Relay's agents, the agent's changes in Source Control and the diff editor, and the open file and its problems going
along with what you ask.

## What the extension has

- **The conversations** (the Relay view): your chats and code tasks, those in the open folder first. Each shows
  whether an agent is at work, or asks you something. The status bar counts them, and its tooltip has your usage
  limits.
- **The chat:** in an editor tab, or in the side bar's Chat view (`relay.openIn`, or the button in its header).
  - **The answer as it comes:** its text, its thinking, and its tools with their output. An edit shows its lines in
    red and green.
  - **What you can pick and add:** the agent (Relay's providers and their models), the mode and the effort; files
    to attach, pasted or dropped.
  - **What the agent asks you,** to allow or to answer.
  - **Each answer:** its tokens and cost, what it changed (with Undo this answer), and Retry, Fork and Copy. Your
    message has Edit. The queue shows, with Send now and Remove.
  - **`/` commands:**
    - Relay's own (`/clear`, `/retry`, `/fork`, `/stop`, `/rename`, `/model`, `/mode`, `/effort`, `/timed`,
      `/rewind`, `/usage`, `/help`) are done here;
    - the agent's own and your skills go to the agent.
  - **A notification** when an agent is done, or asks something, while its chat isn't in sight (`relay.notifyDone`).
  - **Files in an answer:** a file named in its code (`src/app.js:12`, `README.md`) opens at its line, as do links
    (`path#L12`).
  - **Following the agent** (`relay.follow`): a file it changes opens beside its chat while the chat is in sight.
  - **After Reload Window,** a chat's tab is there again, with its conversation.
- **Context, as Claude Code's extension has it:**
  - the open file (and its selection) goes along with a message, as a chip that × takes away
    (`relay.context.activeFile`);
  - `@` names a file of the folder, and `@problems` brings the folder's errors and warnings;
  - **Fix with Relay** is a quick fix on each problem;
  - **Ask Relay about this** (`Ctrl+Alt+R`) puts the selection in the chat;
  - **Add to Relay's chat** in the Explorer and the terminal.
- **Review, as Cline and Copilot have it:** Source Control → Relay, for the task whose chat was focused last (or the
  open folder's newest).
  - **The agent's changes:** marks in the editor's gutter; each file in the diff editor, as it was before the agent
    and as it is now.
  - **What you can do with them:** put a file back, Mark as reviewed, Commit (its input box), Merge, Discard a
    task's branch, or Undo all.
- **Keep/Undo in the editor:** in an open file of that task, each change of the agent's is marked, with what it took
  out on hover, and **Keep · Undo** above it.
  - **Keep:** the change is good, and its marks go. It's remembered per change, so the same change at the same place
    stays kept. Once every change is kept, it offers Mark as reviewed.
  - **Undo:** puts those lines back and saves.
  - **More:** Keep all and Undo all for the file, and the next and previous change in the editor's title bar.
  - **Turned off** with `relay.review.inline`. Source Control has the changes per file either way.
- **Edit with Relay (Ctrl+I):** pick lines (or put the cursor where code should go) and say what should change.
  - **How:** an agent answers in a temporary chat of Relay's (no tools; it goes once it answered). Its code replaces
    the lines in the editor, unsaved, with **Accept · Reject** above it.
  - **Which agent:** `relay.inlineAgent` (`provider` or `provider/model`). Without it, the agent picked for a new
    chat last, else the latest conversation's.
- **Agents use VSCodium:** its language servers answer Relay's agents, through this plugin's tools.
  - **The tools:**
    - `vscodium_problems`: the errors and warnings VSCodium sees, in the folder or one file;
    - `vscodium_definition`: where a name is defined, with its lines;
    - `vscodium_references`: every place a name is used;
    - `vscodium_symbols`: names across the folder, or one file's outline;
    - `vscodium_hover`: a name's type and docs.
  - **Which window answers:** the one with the conversation's folder open (a computer task's, or a code task's with
    Remote - SSH). Else it's the window you had in front of you last. Without one, the agent hears that VSCodium
    isn't connected.
- **Updates:** when this plugin has a newer version of the extension, VSCodium offers it.

## Where your folders are

- **On your own computer** (VSCodium there, no Remote - SSH): a conversation in a folder there is a computer task, as
  in Settings → Computers.
  - **The connector:** the extension is that computer's connector (`relay-computer.py`'s protocol, in VSCodium's own
    process). While VSCodium runs, Relay's agents run commands and change files there, with `relay run`, `pull` and
    `push`.
  - **When it's turned on:** when Relay runs on another machine (its address isn't this one), right away. If Relay
    has a computer of this one's name already (Settings → Computers), the extension is its connector by itself.
    Otherwise it asks once, and again when a folder here gets a task. **Relay: This computer** turns it off.
  - **The open file** goes along with a message as "(Open in VSCodium, on the user's computer “laptop”: …)", so an
    agent in any chat knows where it is and reads it there.
  - **Permission control, in VSCodium** (`relay.permissions`): before an agent runs a command or changes a file
    there, VSCodium asks. The question is in the task's chat when that's in sight (else a notification): Allow,
    Allow for this answer, or Deny. A change shows beside it first, as a diff of the file now and the agent's.
    - **Without an answer** within 90 seconds (less for a short command), it's denied. A command's own time-out is
      what's left after the asking.
    - **Less or none:** "commands" asks only before commands (Keep/Undo has the changes); "off" never asks. **Relay:
      This computer** sets it too.
    - **Relay's own question** (Settings → Computers, "Ask me before each command") is turned off for this
      computer, so nothing is asked twice. Code tasks on Relay's machine (Remote - SSH) work in Relay's containers,
      which Relay doesn't ask about: Keep/Undo is their review.
  - **What they ran:** each command, with its output as it comes, in VSCodium's terminal "Relay: <computer>". It
    opens without the focus the first time; **Relay: This computer → Show what it ran** opens it again.
  - **What they changed:** Relay doesn't keep that for a computer, so the extension does. It keeps checkpoints of the
    folder (git, in a repository of its own outside the folder) before each answer's first change, for review and
    undo.
- **On Relay's machine, with Remote - SSH:** VSCodium's folders are Relay's. The extension runs there too (it's a
  workspace extension), reaches Relay at `localhost`, and a conversation in your folder is a code task in it, as on
  Relay's own page.

## Setting it up

1. **Install the extension:**
   - **Install on this machine** (Settings → VSCodium) puts it in the VSCodium or VS Code server that Remote - SSH
     left on Relay's machine. Then run **Developer: Reload Window** in VSCodium.
   - Or **Download** the .vsix and use **Install from VSIX…** in VSCodium's Extensions view.
2. **Sign it in:** in the same tab, **Make one** makes an app password for it.
   - **Sign in VSCodium on this computer** opens VSCodium with it. VSCodium asks whether the extension may open the
     link, and the extension asks before it signs in.
   - Or run **Relay: Sign in** in VSCodium, with the address and the app password.

The extension's address is a setting (`relay.url`, `http://localhost:8420` by default). Behind a proxy's own sign-in,
it goes in the address: `https://name:password@host`.

## How it's made

- **The extension** is `extension/`: plain JavaScript, with no dependencies and no build step.
  - **Without VSCodium's API,** `lib/` holds Relay's API with its stream of a run's events, the turns built from those
    events, the file before a change worked out from its diff, the computer's connector, and the checkpoints.
    `tests/js/vscodium.test.cjs` tries these.
  - **The chat's page** is `media/`.
- **The plugin packs it** as a .vsix itself (`zipfile`, with the manifest vsce would write), so neither npm nor a
  packed file is in the repository.
- **Its routes:**
  - `GET version` says the extension's version;
  - `GET extension` gives the .vsix, in base64;
  - `POST install` installs it with the servers' own `--install-extension`;
  - **for the agents' tools,** as `plugins/chrome` routes Chrome's:
    - `POST windows`: a window says its folders, its computer tasks and whether it's in front;
    - `GET tools?client=&since=&wait=`: its questions, held open until there's one;
    - `POST tools/<id>/result`: its answer, which the waiting tool gives the agent.

To use it, put this folder (or a link to it) in Relay's plugins folder, then restart Relay:

```sh
ln -s ~/relay/plugins/vscodium ~/.local/share/relay/plugins/vscodium
```
