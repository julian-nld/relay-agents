# Usage limits

Each usage-limit window over time. Settings → Usage and the ring on the activity bar say what's left now; this
plugin keeps what they said:
- **every 15 minutes,** and when its page opens, it asks Relay what's left of each window (Claude Code's 5-hour
  and weekly windows, ChatGPT's, Copilot's requests): the same answer as Settings → Usage;
- **its page** (the clock on the activity bar) draws each window's % left over 1, 7 or 14 days, with the low mark,
  and has a table view; the clock's badge counts the windows that are low now;
- **a notification** (Settings → Notifications) when a window runs low, and when it's back;
- **its tab in Settings** sets when a window is low (10% left at first) and turns the notifications off;
- **a tool for agents and the Governor,** `usage_limits_now` (`relay plugin usage_limits_now [provider=ID]` for
  agents without MCP tools): each window's % left, when it resets, and when it would run out before that at the
  pace of its last hours; also which other logins share it. To look before long or costly work, or to pick a
  provider with room for a subagent;
- **14 days** are kept, in its own store.

To use it, put this folder (or a link to it) in Relay's plugins folder, then restart Relay:

```sh
ln -s ~/relay/plugins/usage_limits ~/.local/share/relay/plugins/usage_limits
```
