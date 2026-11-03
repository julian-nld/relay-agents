# Server

How this machine is doing, and what each container uses:
- **its page** (the monitor on the activity bar) shows CPU, memory, swap and each disk, now and over the last 24
  hours, and the running containers in Relay's words (a provider's box, a service, the WorkBox of a chat or a
  project folder), busiest first;
- **warnings** when a disk is 90% full, memory is under 10% available, or the CPU has been over 90% for 10 minutes:
  on its page, as the monitor's badge, and as a notification (Settings → Notifications), once each;
- **a tool for agents and the Governor,** `server_status`: the same in a few lines, to look before starting
  something heavy.

It reads `/proc` and podman, so it's for Linux, as Relay is. Its history is kept in memory: a restart of Relay
starts it anew.

To use it, put this folder (or a link to it) in Relay's plugins folder, then restart Relay:

```sh
ln -s ~/relay/plugins/server ~/.local/share/relay/plugins/server
```
