"""Look at your live services (Production boxes): tell your phone which aren't well, and restart them if you like.

Prints how each one is, and exits with 1 when one still isn't well. ONLY: the names to look at; RESTART: restart
one that isn't well, once, and look again. The phone: Settings → Notifications (ntfy or a webhook). To look every
morning, make it a task (in a script, or ask AgentOS's Governor):
    tasks.create("Services", "daily 08:00", script="public/service_watch.py")
"""

import sys

from relay_sdk import *

ONLY = []  # e.g. ["site", "api"]; []: all of them
RESTART = False  # restart one that isn't well (what Relay did is printed)


def trouble(service):
    """What's wrong with a service (as services.list() or service.get() has it), in words; None when it's well.
    One stopped by hand is so on purpose."""
    if service.get("stopped"):
        return None
    if service.get("health_ok") is False:
        return service.get("health_detail") or "its health check fails"
    return None


def main():
    unwell = []
    try:
        found = [row for row in services.list() if not ONLY or row["name"] in ONLY]
        if not found:
            print("No live services to look at.", flush=True)
            return
        for row in found:
            wrong = trouble(row)
            print(f"{row['name']}: {'stopped by hand' if row.get('stopped') else wrong or 'well'}", flush=True)
            if wrong and RESTART:
                service = services.get(row["id"])
                print(f"  {service.restart()}", flush=True)
                wrong = trouble(service.get())
            if wrong:
                unwell.append(f"{row['name']}: {wrong}")
    except RelayError as error:
        print(f"Relay couldn't: {error}", flush=True)
        sys.exit(1)
    if unwell:
        notify("\n".join(unwell), title=f"Not well: {len(unwell)} service(s)", priority="high")
        sys.exit(1)


if __name__ == "__main__":
    main()
