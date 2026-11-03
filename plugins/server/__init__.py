"""Server: how this machine is doing (CPU, memory, disks) and what each container uses, from /proc and podman. Its
page shows it (static/index.js), a notification says when it gets tight, and agents (the Governor too) ask with their
server_status tool before they start something heavy."""

import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

import relay_sdk

MINUTES = 24 * 60  # of history, a point a minute (kept in memory: a restart starts it anew)
DISK_FULL, DISK_OK = 0.90, 0.85  # a disk is full from this much used, and fine again under the other
MEMORY_LOW, MEMORY_OK = 0.10, 0.15  # memory is low under this much available, and fine again over the other
CPU_BUSY, CPU_OK, BUSY_MINUTES = 0.90, 0.70, 10  # busy: over this for that many minutes in a row
NAMES_FOR = 300  # seconds services' and chats' names are kept
CHAT = re.compile(r"/chats/([0-9a-f]{32})$")
BOX_HOME = "/home/relay"  # (an agent's home in its WorkBox, whoever runs Relay here)
# Mounts every WorkBox has (Relay's own code, tools and exchange folders): the rest is the folder it's for
SHARED = ("/relay-containers/", "/builtin_skills", "/src/relay/", "/sdk/python", "/scripts/public", "/tmp/opencode",
          "/var/lib/containers")


# What's measured, each in a function of its own (the tests give their own)

def cpu_times():
    """(idle, total) since the machine started, from /proc/stat."""
    fields = [int(v) for v in Path("/proc/stat").read_text().split("\n", 1)[0].split()[1:9]]
    return fields[3] + fields[4], sum(fields)  # (idle and waiting for disks; user to steal)


def meminfo():
    found = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, _, rest = line.partition(":")
        if rest.split():
            found[key] = int(rest.split()[0]) * 1024
    return found


def uptime():
    return float(Path("/proc/uptime").read_text().split()[0])


def podman(*args):
    """podman's output, or "" when it doesn't answer."""
    try:
        done = subprocess.run(["podman", *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout if done.returncode == 0 else ""


def containers_running():
    """The running containers: {id: {name, labels, mounts}} and {id: (cpu seconds so far, memory bytes)}."""
    listed = {c["Id"]: {"name": (c.get("Names") or ["?"])[0], "labels": c.get("Labels") or {},
                        "mounts": c.get("Mounts") or []} for c in json.loads(podman("ps", "--format", "json") or "[]")}
    used = {}
    for line in podman("stats", "--no-stream", "--format",
                       "{{.ContainerStats.ContainerID}}\t{{.ContainerStats.CPUNano}}\t{{.ContainerStats.MemUsage}}").splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
            used[parts[0]] = (int(parts[1]) / 1e9, int(parts[2]))
    return listed, used


def setup(plugin):
    lock = threading.Lock()
    history = deque(maxlen=MINUTES)
    warnings = {}  # key → what it says, while it holds
    last = {"cpu": {}, "containers": {}}  # the previous reads (the page's and the minute's), for what was used since
    names = {"at": 0, "services": {}, "chats": {}}
    disks_at = []  # the folders whose disks are shown: found at the first look

    def folders():
        if not disks_at:
            graph = podman("info", "--format", "{{.Store.GraphRoot}}").strip()
            data = plugin.data.parent.parent  # (Relay's data folder: plugin-data/<name> is in it)
            disks_at.extend([("/", "System"), (str(Path.home()), "Home"), (str(data), "Relay's data")]
                            + ([(graph, "Containers")] if graph else []))
        return disks_at

    def disks():
        found, seen = [], {}
        for path, label in folders():
            try:
                device = os.stat(path).st_dev
                usage = shutil.disk_usage(path)
            except OSError:
                continue
            if device in seen:
                seen[device]["also"].append(label)
                continue
            seen[device] = {"label": label, "path": path, "also": [], "total": usage.total, "free": usage.free,
                            "used": usage.used / usage.total if usage.total else 0}
            found.append(seen[device])
        return found

    def cpu(who):
        """The share of all cores in use since `who`'s last look (None at its first)."""
        idle, total = cpu_times()
        before, last["cpu"][who] = last["cpu"].get(who), (idle, total)
        if before is None or total <= before[1]:
            return None
        return max(0.0, min(1.0, 1 - (idle - before[0]) / (total - before[1])))

    def named():
        """Services' and chats' names, from Relay (asked every few minutes)."""
        if time.time() - names["at"] > NAMES_FOR:
            names["at"] = time.time()
            try:
                names["services"] = {s["id"]: s["name"] for s in relay_sdk.services.list(user=plugin.sdk)}
                names["chats"] = {c["id"]: c["title"] for c in relay_sdk.convo.list(user=plugin.sdk)}
            except Exception:  # noqa: BLE001 - without names, the ids
                pass
        return names

    def containers():
        if not last["containers"]:  # (a first look: a second, a moment later, says what they use)
            _, used = containers_running()
            last["containers"] = {cid: (time.time(), seconds) for cid, (seconds, _) in used.items()}
            time.sleep(1)
        listed, used = containers_running()
        now, cores = time.time(), os.cpu_count() or 1
        known = named()
        found = []
        for cid, c in listed.items():
            labels, name = c["labels"], c["name"]
            row = {"name": name, "kind": "Container", "what": name, "chat": None, "cpu": None,
                   "memory": used.get(cid, (0, 0))[1]}
            if labels.get("io.relay.box"):
                row.update(kind="ProviderBox", what=name.removeprefix("relay-box-"))
            elif labels.get("io.relay.production"):
                service = labels["io.relay.production"]
                row.update(kind="Service", what=known["services"].get(service, service[:8]))
            elif labels.get("io.relay.managed"):
                home = str(Path.home())
                own = [m for m in c["mounts"] if m not in (home, BOX_HOME) and not any(part in m for part in SHARED)]
                folder = max(own, key=len, default="")
                chat = CHAT.search(folder)
                scripts = any("/scripts/public" in m for m in c["mounts"])  # (with no folder of its own: scripts')
                row.update(kind="WorkBox", what=(f"chat: {known['chats'].get(chat.group(1), chat.group(1)[:8])}" if chat
                                                 else folder.replace(home, "~", 1) or ("scripts" if scripts else name)),
                           chat=chat.group(1) if chat else None)
            if cid in used:  # (CPU since the last look: podman counts its seconds)
                before = last["containers"].get(cid)
                if before and now > before[0]:
                    row["cpu"] = max(0.0, (used[cid][0] - before[1]) / (now - before[0]) / cores)
                last["containers"][cid] = (now, used[cid][0])
            found.append(row)
        last["containers"] = {k: v for k, v in last["containers"].items() if k in listed}
        return sorted(found, key=lambda r: (-(r["cpu"] or 0), -r["memory"]))

    def measure(who):
        """Now: the machine and its disks."""
        memory = meminfo()
        total, available = memory.get("MemTotal", 0), memory.get("MemAvailable", 0)
        swap_total = memory.get("SwapTotal", 0)
        load = os.getloadavg()
        return {"cores": os.cpu_count(), "load": [round(v, 2) for v in load], "cpu": cpu(who), "uptime": uptime(),
                "memory": {"total": total, "available": available, "used": 1 - available / total if total else 0},
                "swap": {"total": swap_total, "used": (swap_total - memory.get("SwapFree", 0)) / swap_total
                         if swap_total else 0},
                "disks": disks()}

    def gb(n):
        return f"{n / 1e9:.1f} GB"

    def judge(now, notify=True):
        """The warnings, each said once when it begins (and gone again with some room: no back and forth)."""
        found = {}
        for disk in now["disks"]:
            key = f"disk:{disk['path']}"
            limit = DISK_OK if key in warnings else DISK_FULL
            if disk["used"] >= limit:
                found[key] = f"The disk of {disk['path']} is {round(disk['used'] * 100)}% full: {gb(disk['free'])} free"
        available = 1 - now["memory"]["used"]
        if available < (MEMORY_OK if "memory" in warnings else MEMORY_LOW):
            found["memory"] = f"Memory is low: {round(available * 100)}% available " \
                              f"({gb(now['memory']['available'])} of {gb(now['memory']['total'])})"
        recent = [p["cpu"] for p in list(history)[-BUSY_MINUTES:] if p["cpu"] is not None]
        if ("cpu" in warnings and recent and recent[-1] >= CPU_OK) or \
                (len(recent) == BUSY_MINUTES and all(v >= CPU_BUSY for v in recent)):
            found["cpu"] = f"The CPU has been over {round(CPU_BUSY * 100)}% for {BUSY_MINUTES} minutes"
        new = [text for key, text in found.items() if key not in warnings]
        warnings.clear()
        warnings.update(found)
        if notify:
            for text in new:
                relay_sdk.notify(text, title="This server", url="/p/server", user=plugin.sdk)

    def minute():
        with lock:
            now = measure("minute")
            history.append({"t": round(time.time()), "cpu": now["cpu"], "memory": now["memory"]["used"],
                            "swap": now["swap"]["used"], "disks": {d["path"]: d["used"] for d in now["disks"]}})
            judge(now)

    with lock:
        cpu("minute")  # (a first look each, so the next one says what was used since)
        cpu("page")

    plugin.every(60, minute)

    # Its page's API, at /api/plugins/server/…
    def status(call):
        with lock:
            now = measure("page")
            now["containers"] = containers()
        now["warnings"] = list(warnings.values())
        return now

    plugin.route("status", status)
    plugin.route("history", lambda call: list(history))
    plugin.route("warnings", lambda call: list(warnings.values()))

    # A tool for agents and the Governor: server_status
    def said(call):
        now = status(call)
        lines = [f"CPU: {'?' if now['cpu'] is None else round(now['cpu'] * 100)}% of {now['cores']} cores "
                 f"(load {' '.join(str(v) for v in now['load'])})",
                 f"Memory: {gb(now['memory']['total'] - now['memory']['available'])} of {gb(now['memory']['total'])} "
                 f"used, {gb(now['memory']['available'])} available"
                 + (f"; swap {round(now['swap']['used'] * 100)}% used" if now["swap"]["total"] else ""),
                 "Disks: " + "; ".join(f"{d['label']} ({d['path']}) {round(d['used'] * 100)}% full, "
                                       f"{gb(d['free'])} free" for d in now["disks"])]
        busiest = now["containers"][:5]
        if busiest:
            lines.append("Busiest containers: " + "; ".join(
                f"{c['kind']} {c['what']}: {'?' if c['cpu'] is None else round(c['cpu'] * 100, 1)}% CPU, "
                f"{c['memory'] / 1e6:.0f} MB" for c in busiest))
        lines.append("Warnings: " + ("; ".join(now["warnings"]) if now["warnings"] else "none"))
        return "\n".join(lines)

    plugin.tool("status", "How this server is doing now: CPU, memory, disks, the busiest containers and any warnings. "
                "Look before starting something heavy (a big build, a model on this machine).", None, said,
                governor=True)
