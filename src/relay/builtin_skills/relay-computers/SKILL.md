---
name: relay-computers
description: Work on the user's own computers through Relay (run commands, read and write files there), e.g. to change a setting on their PC, install something, or fetch a file from it. Use when the user asks for something on "my computer", "my PC" or a computer by name.
---

# The user's own computers

Relay may run on another machine than the one the user means. Their own
computers can run Relay's connector; while it runs, you can work there as
the user who started it.

## How

With a shell, use the `relay` command whose full path Relay told you:

```sh
relay computers                                  # which, online or not, their OS, user, home, shell
relay run --on laptop 'ipconfig /all'            # a command; --cwd DIR, --timeout SECONDS (max 600)
relay run --on laptop <<'CMD'                    # or a longer one on stdin
powershell -NoProfile -Command "Get-ItemProperty 'HKCU:\Control Panel\Desktop' | Select WallPaper"
CMD
relay pull --on laptop "~/Desktop/notes.txt" notes.txt   # a file from there to here
relay push --on laptop report.pdf "~/Documents/report.pdf"
```

Quote the whole command (or pass it on stdin): your own shell would
otherwise expand `~`, `$VARS`, `*` and `>` here, on the wrong machine.

Chat models get the same as tools: `computers`, `computer_run`,
`computer_read_file`, `computer_write_file`.

## Doing it well

- **Look first.** `relay computers` says the system. Windows runs commands
  with `cmd.exe`: use `powershell -NoProfile -Command "…"` for PowerShell.
  macOS has `defaults`, `networksetup`, `osascript`; Linux desktops often
  `gsettings` or `nmcli`. Check the current value before you change it.
- **Keep it reversible.** Note the old value of a setting before changing it,
  and say how to undo it. Back a file up before overwriting it.
- **Ask before anything drastic**: deleting the user's files, uninstalling,
  rebooting, changing security or network settings the user didn't mention.
  Ask in your answer instead of doing it.
- **Say what you did.** End with the changes you made on which computer.
- **Commands must end by themselves.** Nothing interactive (no prompts, no
  editors); a program that keeps running (a server, a GUI app) must be
  started in the background, e.g. `start "" app.exe` on Windows or
  `nohup app >/dev/null 2>&1 &` elsewhere.
- The user may be asked to allow each command; if they deny one, don't try
  it another way.
- An offline computer means the connector isn't running there: tell the
  user to start it (Settings → Computers in Relay has it).
