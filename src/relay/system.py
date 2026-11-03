"""Settings → System: what Relay runs on this machine, and what it takes.

- The machine: its cores and load, memory and swap, the disk Relay's data is on, Relay itself.
- Relay's boxes (its containers, only this Relay's: podman may be shared with another one), each with
  what it's for (a provider, a project's or chat's WorkBox, a service's version, a VPN tunnel, web
  search or fetch), whether it runs, and its CPU and memory now. One that's idle can be stopped (it
  starts again when it's needed), a stopped WorkBox removed (what was installed in it goes; the
  folder stays). A ProductionBox is its service's: that's where it's started and stopped.
- Storage: podman's images, containers and volumes, and the images: one no container uses (an older
  build of Relay's, a VPN or search image not in use) can go; Relay builds or fetches it again when
  it's needed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from relay import config, forks
from relay.boxes import box_key
from relay.containers import INSTANCE, instance, mine
from relay.production import PROD_LABEL

if TYPE_CHECKING:
    from relay.app import Relay

KINDS = ("provider", "workbox", "production", "vpn", "web", "other")  # (shown in this order)
UNITS = {"b": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9, "tb": 1e12, "kib": 1024, "mib": 1024**2, "gib": 1024**3}


class BoxError(Exception):
    pass


# ------------------------------------------------------------------ the machine

def machine() -> dict[str, Any]:
    info = _meminfo()
    where = config.data_dir()
    while not where.exists() and where != where.parent:  # (not made yet: the disk it will be on)
        where = where.parent
    disk = shutil.disk_usage(where)
    try:
        uptime = float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        uptime = None
    return {
        "cores": os.cpu_count() or 1, "load": list(os.getloadavg()),
        "memory": {"total": info.get("MemTotal", 0), "available": info.get("MemAvailable", 0)},
        "swap": {"total": info.get("SwapTotal", 0), "free": info.get("SwapFree", 0)},
        "disk": {"path": str(config.data_dir()), "total": disk.total, "free": disk.free},
        "relay": _rss(), "uptime": uptime,
    }


def _meminfo() -> dict[str, int]:
    found = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            parts = rest.split()
            if parts and parts[0].isdigit():
                found[key] = int(parts[0]) * (1024 if parts[1:2] == ["kB"] else 1)
    except OSError:
        pass
    return found


def _rss() -> int:
    """Relay's own memory (this process)."""
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


# ------------------------------------------------------------------ Relay's boxes

def boxes(relay: Relay) -> list[dict[str, Any]]:
    """This Relay's containers, what each is for, and what it takes now."""
    listed = _podman_json(["ps", "-a", "--format", "json"]) or []
    services = {s["id"]: s for s in relay.db.all("SELECT id, name, live_version FROM services")}
    ours = []
    for entry in listed:
        name = (entry.get("Names") or [""])[0]
        labels = entry.get("Labels") or {}
        if not name.startswith("relay-"):
            continue
        if PROD_LABEL in labels:
            if labels[PROD_LABEL] not in services:
                continue  # (another Relay's service)
        elif INSTANCE not in labels or not mine(labels[INSTANCE]):
            continue  # (another Relay's: a test run's, say)
        ours.append(entry)
    if not ours:
        return []
    stats = {s.get("name"): s for s in _podman_json(["stats", "--no-stream", "--no-reset", "--format", "json"]) or []}
    folders = {}
    seen = subprocess.run(["podman", "container", "inspect", "--format", "{{.Name}}|{{.Config.WorkingDir}}",
                           *[(e.get("Names") or [""])[0] for e in ours]], capture_output=True, text=True, timeout=60)
    for line in seen.stdout.splitlines():
        name, _, folder = line.partition("|")
        folders[name] = folder
    busy = relay.runs.busy_folders()
    working_boxes = {run.box_key for run in list(relay.runs.active.values()) if run.box_key}
    found = []
    for entry in ours:
        name = (entry.get("Names") or [""])[0]
        labels = entry.get("Labels") or {}
        state = str(entry.get("State") or "")
        stat = stats.get(name) or {}
        box = {"name": name, "state": state, "started": entry.get("StartedAt") if state == "running" else None,
               "exited": entry.get("ExitedAt") if state != "running" else None,
               "cpu": _percent(stat.get("cpu_percent")) if state == "running" else None,
               "memory": _bytes(str(stat.get("mem_usage") or "").split("/")[0]) if state == "running" else None,
               "image": entry.get("Image"), "link": None, "actions": [], "why": None}
        box.update(_purpose(relay, name, labels, folders.get(name, ""), services, busy, working_boxes, state))
        found.append(box)
    order = {kind: n for n, kind in enumerate(KINDS)}
    return sorted(found, key=lambda b: (order.get(b["kind"], 99), b["state"] != "running", b["title"].lower()))


def _purpose(relay: Relay, name: str, labels: dict[str, str], folder: str, services: dict[str, dict],
             busy: set[Path], working_boxes: set[str], state: str) -> dict[str, Any]:
    """{kind, title, detail, link, actions, why}: what a box is for, and what may be done with it."""
    running = state == "running"
    mark = instance()
    bare = name.removesuffix(f"-{mark}") if mark else name
    if PROD_LABEL in labels:
        service = services[labels[PROD_LABEL]]
        number = re.search(r"-(\d+)$", name)
        return {"kind": "production", "title": f"{service['name']}, version {number.group(1) if number else '?'}",
                "detail": "A ProductionBox: its service starts and stops it", "link": {"service": service["id"]}}
    if bare.startswith("relay-box-"):
        key = bare.removeprefix("relay-box-")
        names = []
        for template in relay.registry.templates():
            try:
                if box_key(relay.registry.provider(template.id).box_key() or "") == key:
                    names.append(template.name)
            except Exception:  # noqa: BLE001 - a broken template: not a name here
                continue
        working = key in working_boxes
        return {"kind": "provider", "title": ", ".join(names) or key,
                "detail": "A ProviderBox: the provider's own program",
                "actions": ["stop"] if running and not working else [],
                "why": "a run of it is going" if running and working else None}
    if "io.relay.vpn" in labels:
        tunnel = relay.vpn.tunnel(labels["io.relay.vpn"])
        what = tunnel["name"] if tunnel else "a tunnel that's gone"
        holder = bare.startswith("relay-vpnnet-")
        joined = 0
        if holder and running:
            found = subprocess.run(["podman", "inspect", "--format", "{{.Id}}", name], capture_output=True, text=True,
                                   timeout=30).stdout.strip()
            from relay.vpn import _joined
            joined = _joined().get(found, 0)
        return {"kind": "vpn", "title": f"VPN {what}", "detail": "Its network" if holder else "gluetun, its connection",
                "actions": ["stop"] if running and holder and tunnel and not joined else [],
                "why": f"{joined} WorkBox{'es' if joined > 1 else ''} in it" if joined else None}
    if bare in ("relay-search", "relay-fetch"):
        return {"kind": "web", "title": "Web search (SearXNG)" if bare == "relay-search" else "Web fetch (a browser)",
                "detail": "Starts with the next search or fetch", "actions": ["stop"] if running else []}
    if "io.relay.managed" in labels and folder.startswith("/"):
        return {"kind": "workbox", **_workbox(relay, Path(folder), busy, running, name)}
    return {"kind": "other", "title": name, "detail": folder or "", "actions": []}


def _workbox(relay: Relay, folder: Path, busy: set[Path], running: bool, name: str) -> dict[str, Any]:
    """A WorkBox: whose folder it is (a project's, a chat's, a subagent's copy, the Governor's)."""
    data = config.data_dir().resolve()
    resolved = folder.resolve()
    detail, link, title = str(folder), None, folder.name or str(folder)
    rows = relay.db.all("SELECT id, title FROM conversations WHERE directory = ? AND parent_run IS NULL "
                        "ORDER BY updated DESC LIMIT 3", (str(folder),))
    if data in resolved.parents and resolved.relative_to(data).parts[:1] == ("chats",):
        title = f"Chat: {rows[0]['title']}" if rows else "A chat's scratch folder"
    elif forks.folders().resolve() in resolved.parents:
        title = "A subagent's copy"
    elif data in resolved.parents and resolved.relative_to(data).parts[:1] == ("agentos",):
        title = "The AgentOS Governor"
    elif data in resolved.parents and resolved.relative_to(data).parts[:1] == ("production",):
        title = f"A service's WorkBox: {rows[0]['title']}" if rows else "A service's WorkBox"
    elif rows:
        title = rows[0]["title"] or folder.name
        if len(rows) > 1:
            detail += f" · and {len(rows) - 1} more conversation{'s' if len(rows) > 2 else ''}"
    if rows:
        link = {"conversation": rows[0]["id"]}
    working = any(resolved == f or resolved in f.parents for f in busy)
    terminal = bool(getattr(relay.containers, "terminals", {}).get(name))
    if running:
        why = "an agent works there" if working else ("a terminal is open" if terminal else None)
        return {"title": title, "detail": detail, "link": link, "actions": [] if why else ["stop"], "why": why}
    return {"title": title, "detail": detail, "link": link, "actions": ["remove"], "why": None, "folder": str(folder)}


def stop(relay: Relay, name: str) -> None:
    """Stop one of Relay's idle boxes: it starts again when it's needed."""
    box = _find(relay, name, "stop")
    if box["kind"] == "workbox":
        relay.containers.stop_box(name)
    elif box["kind"] == "web":
        (relay.search if name == relay.search.name() else relay.fetch).rest()
    elif box["kind"] == "vpn":
        tunnel = relay.vpn.tunnel(_labels(name).get("io.relay.vpn", ""))
        if tunnel is None:
            raise BoxError("That tunnel is gone")
        relay.vpn.rest(tunnel)
    else:
        subprocess.run(["podman", "stop", "-t", "10", name], capture_output=True, timeout=60)


def remove(relay: Relay, name: str) -> None:
    """Remove a stopped WorkBox, and its home: what was installed in it goes; the folder stays."""
    box = _find(relay, name, "remove")
    relay.containers.discard(Path(box["folder"]))


def remove_stopped(relay: Relay) -> int:
    """Remove every stopped WorkBox; how many."""
    gone = 0
    for box in boxes(relay):
        if box["kind"] == "workbox" and "remove" in box["actions"]:
            relay.containers.discard(Path(box["folder"]))
            gone += 1
    return gone


def _find(relay: Relay, name: str, action: str) -> dict[str, Any]:
    box = next((b for b in boxes(relay) if b["name"] == name), None)
    if box is None:
        raise BoxError(f"No box of Relay's called {name}")
    if action not in box["actions"]:
        raise BoxError(f"{box['title']} can't be {'stopped' if action == 'stop' else 'removed'} now"
                           + (f": {box['why']}" if box.get("why") else ""))
    return box


def _labels(name: str) -> dict[str, str]:
    found = _podman_json(["container", "inspect", "--format", "{{json .Config.Labels}}", name])
    return found if isinstance(found, dict) else {}


# ------------------------------------------------------------------ storage

def storage(relay: Relay) -> dict[str, Any]:
    """podman's images, containers and volumes (sizes), and the images, whose and in use or not."""
    totals = {}
    for row in _podman_json(["system", "df", "--format", "json"]) or []:
        totals[str(row.get("Type", "")).lower().replace("local ", "")] = {
            "count": row.get("TotalCount") or row.get("Total") or 0, "size": row.get("RawSize") or 0,
            "reclaimable": row.get("RawReclaimable") or 0}
    used: dict[str, int] = {}
    for entry in _podman_json(["ps", "-a", "--format", "json"]) or []:
        used[str(entry.get("ImageID") or "")] = used.get(str(entry.get("ImageID") or ""), 0) + 1
    images = []
    workbox_image = (relay.containers.image if relay.containers else "") or ""
    box_image = (relay.boxes.image if relay.boxes else "") or ""
    for image in _podman_json(["images", "--format", "json"]) or []:
        tags = [t for t in image.get("Names") or image.get("RepoTags") or [] if t] or ["<none>"]
        tag = tags[0]
        count = used.get(str(image.get("Id") or ""), 0) or int(image.get("Containers") or 0)
        whose = _whose(tag, workbox_image, box_image)
        images.append({"id": image.get("Id"), "name": tag, "size": image.get("Size") or 0, "containers": count,
                       "whose": whose, "removable": count == 0})
    images.sort(key=lambda i: (i["containers"] == 0, -int(i["size"] or 0)))
    return {"totals": totals, "images": images}


def _whose(tag: str, workbox_image: str, box_image: str) -> str:
    repository = tag.rsplit(":", 1)[0]
    if tag == workbox_image:
        return "Relay's WorkBoxes (and its browser for fetch)"
    if tag == box_image:
        return "Relay's ProviderBoxes"
    if repository in (workbox_image.rsplit(":", 1)[0], box_image.rsplit(":", 1)[0]):
        return "an older build of Relay's"
    if "searxng" in repository:
        return "Relay's web search"
    if "gluetun" in repository:
        return "Relay's VPN tunnels"
    return "not Relay's own"


def remove_image(relay: Relay, image_id: str) -> None:
    """An image no container uses: gone (built or fetched again when it's needed)."""
    image = next((i for i in storage(relay)["images"] if i["id"] == image_id), None)
    if image is None:
        raise BoxError("No such image")
    if not image["removable"]:
        raise BoxError(f"{image['name']} is in use")
    done = subprocess.run(["podman", "rmi", image_id], capture_output=True, text=True, timeout=120)
    if done.returncode != 0:
        raise BoxError(f"podman couldn't remove it: {done.stderr.strip()[-200:]}")


# ------------------------------------------------------------------ helpers

def _podman_json(args: list[str]) -> Any:
    try:
        done = subprocess.run(["podman", *args], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0 or not done.stdout.strip():
        return None
    try:
        return json.loads(done.stdout)
    except ValueError:
        return None


def _percent(text: Any) -> float | None:
    try:
        return round(float(str(text).strip().rstrip("%")), 1)
    except ValueError:
        return None


def _bytes(text: str) -> int | None:
    found = re.match(r"\s*([\d.]+)\s*([a-zA-Z]*)", text or "")
    if not found:
        return None
    return int(float(found.group(1)) * UNITS.get(found.group(2).lower(), 1))


def overview(relay: Relay) -> dict[str, Any]:
    return {"machine": machine(), "boxes": boxes(relay), "time": time.time()}
