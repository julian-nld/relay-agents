# Notes, Relay's example plugin

Short notes, yours and your agents':
- **its page** (the book on the activity bar) lists them, and you add your own there;
- **agents add them** with their `notes_add` tool;
- **a run that fails** leaves one, with its chat;
- **notes older than 30 days** go by themselves;
- **its tab in Settings** removes them all.

To use it, put this folder (or a link to it) in Relay's plugins folder, then restart Relay:

```sh
ln -s ~/relay/plugins/notes ~/.local/share/relay/plugins/notes
```

[WRITING-A-PLUGIN.md](../../docs/WRITING-A-PLUGIN.md) explains it line by line.
