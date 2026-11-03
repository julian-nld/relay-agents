# Relay

Relay's agents in VSCodium (and VS Code). It's an extension of Relay's VSCodium plugin, and talks to your Relay with
an app password.

- **Conversations** (the Relay view): your chats and code tasks, those in the open folder first. Each shows whether an
  agent is at work, or asks you something. The status bar counts them, and its tooltip has your usage limits.
- **The chat:** a conversation in an editor tab, or in the side bar's Chat view (drag it to the secondary side bar if
  you like).
  - **The answer as it comes:** its text, its thinking, and its tools with their output. An edit shows its lines in
    red and green.
  - **What you can pick and add:** the agent, the mode and the effort; files to attach, pasted or dropped.
  - **What the agent asks you,** to allow or to answer.
  - **Each answer:** its tokens and cost, what it changed (with Undo this answer), and Retry, Fork and Copy. Your own
    message has Edit.
  - **`/` commands:** Relay's own (`/clear`, `/timed`, `/rewind`, `/usage`, `/effort`, …), the agent's and your
    skills.
  - **A notification** when an agent is done, or asks something, while its chat isn't in sight.
  - **Files named in an answer** open at their line (`src/app.js:12`). A file the agent changes opens beside its
    chat (`relay.follow`). A chat's tab comes back after Reload Window.
- **Edit with Relay (Ctrl+I):** what should change in the picked lines. An agent's code replaces them, with Accept ·
  Reject (`relay.inlineAgent` picks the agent).
- **Context:**
  - the open file (and its selection) goes along with a message, as a chip that × takes away;
  - `@` names a file of the folder, and `@problems` brings the folder's errors and warnings;
  - **Fix with Relay** on a problem (the light bulb);
  - **Ask Relay about this** (`Ctrl+Alt+R`, or the editor's menu) puts the selection in the chat;
  - **Add to Relay's chat** in the Explorer and the terminal.
- **Agents use VSCodium:** its language servers answer Relay's agents (`vscodium_problems`, `vscodium_definition`,
  `vscodium_references`, `vscodium_symbols`, `vscodium_hover`), in the window with the conversation's folder open.
- **Your folders, wherever they are:**
  - **On Relay's machine** (with Remote - SSH): a conversation is a code task there.
  - **On your own computer:** a conversation is a computer task. This VSCodium lets Relay's agents run commands and
    change files there while it runs (Relay: This computer). It's on by itself when Relay already has a computer of
    this one's name, else asked once. VSCodium asks you before each command and change, a
    change shown as a diff first (`relay.permissions`: also "commands" only, or "off"). What they run shows in the
    terminal "Relay: <computer>", as it runs.
- **Review** (Source Control → Relay): the agent's changes, with marks in the editor's gutter.
  - Each file opens in the diff editor, as it was before the agent and as it is now.
  - **What you can do with them:** put a file back, Mark as reviewed, Commit, Merge, or Undo all.
  - **On your own computer,** VSCodium keeps checkpoints itself (with git) before each answer's first change.
  - **In the editor,** each change has **Keep · Undo** above it (`relay.review.inline` turns that off).

**To sign in:** in Relay, go to Settings → VSCodium → Make one, then use **Sign in VSCodium on this computer**. Or
run **Relay: Sign in** here, with Relay's address and the app password.
