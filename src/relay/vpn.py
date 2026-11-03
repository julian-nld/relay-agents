"""VPNs for WorkBoxes: gluetun (qmcgaw/gluetun), started and kept by Relay.

The user's settings (Settings → Workspaces → VPN; `settings.vpn`):

- profiles: VPN accounts, as gluetun's environment (VPN_SERVICE_PROVIDER, VPN_TYPE, the login, …),
  without a place;
- tunnels: which WorkBoxes go through which VPN. A tunnel has project folders (the most specific
  one wins), or all WorkBoxes no other tunnel has; its exits, in order (a profile and a place:
  SERVER_COUNTRIES/REGIONS/CITIES/HOSTNAMES); ports to pass through; rotation (every so many
  minutes a new server at the same exit, or the next exit; not while an agent works there, if
  so set); and a fallback loop (an exit without a connection for so many seconds: the next one,
  round and round; back to the first after so many minutes).

A tunnel is two containers. Its network is a holder's (relay-vpnnet-…: pasta, the published ports,
nothing running), and gluetun (relay-vpn-…) and the tunnel's WorkBoxes join it
(--network=container:…). So gluetun can be made anew for another exit while the WorkBoxes keep
their network: only the holder going takes it (Containers.ensure then starts them anew).
Between two gluetuns the firewall's rules are cleared but not its DROP policies: nothing leaves
outside the VPN meanwhile (gluetun's own rules would pile up otherwise). A new server at the same
exit is gluetun's own reconnect (its control server: /v1/vpn/status).

A tunnel with no running WorkBox in it for IDLE_AFTER (Containers.stop_idle stops idle ones) stops:
gluetun goes, the holder stops. Its stopped WorkBoxes keep what they have, and the first of them
used again starts it again (ensure), at its first exit.

What happens to a tunnel is kept (vpn-events.json: its last KEEP_EVENTS, for Settings). Its problems
(it lost its connection and fell back, gluetun stopped, no exit connects) are notified (relay.notify:
ntfy, a webhook; at most every TELL_EVERY), and once that it's connected again.

Connected means gluetun's health server says so and it knows its public address: after a change
the health server may still say yes for a while (a wrong password, say), the address is gone.

In there, 127.0.0.1 is the tunnel's, not this machine's: pasta makes HOST this machine's
127.0.0.1, where a WorkBox reaches Relay (relay_url); gluetun's firewall lets that through, and
the LAN (FIREWALL_OUTBOUND_SUBNETS). A server in such a WorkBox is reachable from the LAN on the
tunnel's ports only.
"""

from __future__ import annotations

import ipaddress
import json
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from opencode_manager import paths
from opencode_manager.jsonfile import read_json, write_json
from relay import config, forks, git
from relay.containers import ContainerError, instance, network_alive, spec_of, started_at, up

IMAGE = "docker.io/qmcgaw/gluetun:latest"
LABEL = "io.relay.vpn"  # =the tunnel's id
EXIT = "io.relay.vpn.exit"  # gluetun's: which of its tunnel's exits it is
READY_TIMEOUT = 120  # seconds gluetun has to connect, without a fallback loop
GRACE = 3  # seconds after a change before asking whether it's connected (no answer from before)
IDLE_AFTER = 10 * 60  # seconds without a running WorkBox in a tunnel before it stops (its WorkBoxes wake it)
KEEP_EVENTS = 50  # per tunnel, kept over restarts (vpn-events.json)
# seconds: at most one notification of a tunnel's problem this often (a loop fails round and round)
TELL_EVERY = 30 * 60
SECRET_WORDS = ("KEY", "PASSWORD", "TOKEN", "SECRET")  # in a setting's name: shown masked
HOST = "169.254.1.3"  # this machine's loopback, from a tunnel's network (pasta --map-host-loopback)
# Where gluetun's server list gives a server's place: these, as its SERVER_… settings
PLACES = (("country", "SERVER_COUNTRIES"), ("region", "SERVER_REGIONS"))
PLACE_KEYS = ("SERVER_COUNTRIES", "SERVER_REGIONS", "SERVER_CITIES", "SERVER_HOSTNAMES")
ROTATE_TO = ("server", "next")
DEFAULT_ROTATE = {"minutes": 0, "to": "server", "idle": True}
DEFAULT_FALLBACK = {"after": 60, "back": 30}  # seconds without a connection; minutes before the first again
# Between two gluetuns in a tunnel's network: no rules of the last one, still nothing let out
RESET = ("for t in iptables ip6tables; do $t -P INPUT DROP; $t -P FORWARD DROP; $t -P OUTPUT DROP; "
         "$t -F; $t -X; done; for f in '' -6; do ip $f rule show | while read -r pref rest; do "
         "pref=${pref%:}; case $pref in 0|32766|32767) ;; *) ip $f rule del pref $pref;; esac; done; done")


class VpnError(ContainerError):
    pass


def normalize(found: Any) -> dict[str, Any]:
    """settings.vpn as profiles and tunnels; the one VPN of before (env, folders, all, ports) is a
    profile and a tunnel with one exit."""
    found = found if isinstance(found, dict) else {}
    if "profiles" not in found and "tunnels" not in found:
        env = {str(k): str(v) for k, v in (found.get("env") or {}).items()}
        if not env:
            return {"profiles": [], "tunnels": [], "image": str(found.get("image") or "")}
        provider = env.get("VPN_SERVICE_PROVIDER") or "vpn"
        profile = {"id": "p1", "name": provider[:1].upper() + provider[1:],
                   "env": {k: v for k, v in env.items() if k not in PLACE_KEYS}}
        tunnel = {"id": "t1", "name": "VPN", "folders": list(found.get("folders") or []), "all": bool(found.get("all")),
                  "ports": list(found.get("ports") or []),
                  "exits": [{"profile": "p1", "place": {k: env[k] for k in PLACE_KEYS if env.get(k)}}]}
        found = {"profiles": [profile], "tunnels": [tunnel], "image": found.get("image") or ""}
    profiles = [{"id": str(p.get("id")), "name": str(p.get("name") or p.get("id")),
                 "env": {str(k): str(v) for k, v in (p.get("env") or {}).items()}}
                for p in found.get("profiles") or [] if isinstance(p, dict) and p.get("id")]
    tunnels = []
    for t in found.get("tunnels") or []:
        if not isinstance(t, dict) or not t.get("id"):
            continue
        tunnels.append({
            "id": str(t["id"]), "name": str(t.get("name") or t["id"]),
            "folders": [str(f) for f in t.get("folders") or []],
            "all": bool(t.get("all")), "ports": [int(p) for p in t.get("ports") or []],
            "exits": [{"profile": str(e.get("profile") or ""),
                       "place": {k: str(v) for k, v in (e.get("place") or {}).items() if k in PLACE_KEYS and v}}
                      for e in t.get("exits") or [] if isinstance(e, dict)],
            "rotate": {**DEFAULT_ROTATE, **(t.get("rotate") or {})},
            "fallback": {**DEFAULT_FALLBACK, **(t.get("fallback") or {})},
        })
    return {"profiles": profiles, "tunnels": tunnels, "image": str(found.get("image") or "")}


class Vpn:
    def __init__(self, settings: config.Settings) -> None:
        self.settings = settings
        self.busy: Callable[[], set[Path]] = set  # folders an agent works in now (set by Relay)
        self.locks: dict[str, threading.Lock] = {}
        self.guard = threading.Lock()
        # Per tunnel, in memory: its exit now, since when, why, when it was last seen down, when it
        # last rotated, and what happened (for Settings)
        self.state: dict[str, dict[str, Any]] = {}
        self.pruned = False
        self.notifier: Any = None  # relay.notify.Notifier (set by Relay)
        self.history: dict[str, list[dict[str, Any]]] = {
            k: v for k, v in read_json(config.data_dir() / "vpn-events.json").items() if isinstance(v, list)}
        self.writing = threading.Lock()
        self.told: dict[str, float] = {}  # tunnel -> when its problem was last notified
        self.troubled: set[str] = set()  # tunnels whose problem was notified, not yet that it's connected again

    def lock(self, tunnel_id: str) -> threading.Lock:
        """One tunnel's: one connects it at a time (a WorkBox, the scheduler, Settings)."""
        with self.guard:
            return self.locks.setdefault(tunnel_id, threading.Lock())

    # What the user set

    def config(self) -> dict[str, Any]:
        return normalize(self.settings.vpn)

    def configured(self) -> bool:
        return bool(self.config()["tunnels"])

    def tunnel(self, tunnel_id: str) -> dict[str, Any] | None:
        return next((t for t in self.config()["tunnels"] if t["id"] == tunnel_id), None)

    def choose(self, wanted: Any) -> dict[str, Any]:
        """The tunnel a conversation asks for (made with uses_vpn): by its id or name, or the only one for True;
        VpnError when there's none to choose."""
        tunnels = self.config()["tunnels"]
        names = ", ".join(t["name"] for t in tunnels)
        if wanted is True or str(wanted).lower() == "true":
            if len(tunnels) != 1:
                raise VpnError("There's no VPN tunnel: make one in Settings → Workspaces → VPN" if not tunnels
                               else f"Which VPN tunnel? There are {len(tunnels)}: {names}")
            return tunnels[0]
        found = next((t for t in tunnels if str(wanted) in (t["id"], t["name"])), None)
        if found is None:
            raise VpnError(f"No VPN tunnel {wanted!r}" + (f": there are {names}" if tunnels else
                                                         ": make one in Settings → Workspaces → VPN"))
        return found

    def take(self, tunnel: dict[str, Any], directory: Path | str) -> None:
        """A folder's WorkBoxes through this tunnel from now on, kept as Settings keeps it (its folders)."""
        found = self.config()
        folder = str(origin(directory))
        tunnels = [{**t, "folders": [*t["folders"], folder]} if t["id"] == tunnel["id"] and folder not in t["folders"]
                   else t for t in found["tunnels"]]
        self.settings.vpn = {"profiles": found["profiles"], "tunnels": tunnels,
                             **({"image": found["image"]} if found["image"] else {})}
        config.save_settings(self.settings)

    def leave(self, directory: Path | str) -> None:
        """A folder out of every tunnel's folders (a conversation's VPN undone): its WorkBoxes go on the machine's
        network again, or a tunnel's for all."""
        found = self.config()
        folder = str(origin(directory))
        if not any(folder in t["folders"] for t in found["tunnels"]):
            return
        tunnels = [{**t, "folders": [f for f in t["folders"] if f != folder]} for t in found["tunnels"]]
        self.settings.vpn = {"profiles": found["profiles"], "tunnels": tunnels,
                             **({"image": found["image"]} if found["image"] else {})}
        config.save_settings(self.settings)

    def profile(self, profile_id: str) -> dict[str, Any] | None:
        return next((p for p in self.config()["profiles"] if p["id"] == profile_id), None)

    def tunnel_for(self, directory: Path | str) -> dict[str, Any] | None:
        """The tunnel a folder's WorkBox goes through: the one with the most specific of its
        folders, else the one for all; None: the machine's network."""
        tunnels = [t for t in self.config()["tunnels"] if t["exits"]]
        if not tunnels:
            return None  # (as for most: asked for every command of every WorkBox)
        folder = origin(directory)
        best, depth = None, -1
        for tunnel in tunnels:
            for chosen in tunnel["folders"]:
                path = Path(chosen).resolve()
                if (folder == path or path in folder.parents) and len(path.parts) > depth:
                    best, depth = tunnel, len(path.parts)
        if best is None:
            best = next((t for t in tunnels if t["all"]), None)
        return best

    def covers(self, directory: Path | str) -> bool:
        """Whether a folder's WorkBox goes through a tunnel."""
        return self.tunnel_for(directory) is not None

    def relay_url(self) -> str | None:
        """Relay's address from a tunnel (where it listens on this machine's loopback)."""
        host = self.settings.host
        return f"http://{HOST}:{self.settings.port}" if host in ("", "0.0.0.0", "::", "127.0.0.1",
                                                                 "localhost") else None

    # Its containers

    def names(self, tunnel: dict[str, Any]) -> tuple[str, str]:
        """(the holder of its network, gluetun)"""
        mark = instance()
        suffix = f"{tunnel['id']}-{mark}" if mark else tunnel["id"]
        return f"relay-vpnnet-{suffix}", f"relay-vpn-{suffix}"

    def image(self) -> str:
        return self.config()["image"] or IMAGE

    def holder_argv(self, tunnel: dict[str, Any]) -> list[str]:
        # (DNS: gluetun's, in the network the holder keeps)
        argv = [f"--network=pasta:--map-host-loopback,{HOST}", "--dns=127.0.0.1", "--restart=always"]
        for port in tunnel["ports"]:
            argv += ["-p", f"{port}:{port}"]
        return [*argv, "--entrypoint", "sleep", self.image(), "2147483647"]

    def exit_env(self, tunnel: dict[str, Any], index: int) -> dict[str, str]:
        chosen = tunnel["exits"][index]
        profile = self.profile(chosen["profile"])
        if profile is None:
            raise VpnError(f"The tunnel {tunnel['name']}'s exit {index + 1} has a profile that's gone")
        env = {k: v for k, v in profile["env"].items() if k not in PLACE_KEYS}
        env.update(chosen["place"])
        # Past its firewall: Relay (its WorkBoxes' tools) and the LAN, as without the VPN
        allowed = [s.strip() for s in str(env.get("FIREWALL_OUTBOUND_SUBNETS") or "").split(",") if s.strip()]
        env["FIREWALL_OUTBOUND_SUBNETS"] = ",".join(dict.fromkeys([*allowed, f"{HOST}/32", *lan_subnets()]))
        # Its control server (the public address, a new server) asks for a key: Relay's own
        env["HTTP_CONTROL_SERVER_AUTH_DEFAULT_ROLE"] = json.dumps({"auth": "apikey", "apikey": control_key()})
        return env

    def gluetun_argv(self, tunnel: dict[str, Any], holder_id: str, index: int) -> list[str]:
        # (NET_RAW: its health check pings through the tunnel)
        argv = [f"--network=container:{holder_id}", "--cap-add=NET_ADMIN", "--cap-add=NET_RAW",
                "--device", "/dev/net/tun", "--restart=on-failure"]
        for key, value in sorted(self.exit_env(tunnel, index).items()):
            argv += ["-e", f"{key}={value}"]
        return [*argv, self.image()]

    def exit_label(self, tunnel: dict[str, Any], index: int) -> str:
        chosen = tunnel["exits"][index] if index < len(tunnel["exits"]) else {"profile": "", "place": {}}
        profile = self.profile(chosen["profile"])
        place = chosen["place"]
        where = ", ".join(v for v in (place.get("SERVER_HOSTNAMES"), place.get("SERVER_CITIES"),
                                      place.get("SERVER_REGIONS") or place.get("SERVER_COUNTRIES")) if v)
        return f"{profile['name'] if profile else '?'} · {where or 'any server'}"

    # A WorkBox's network

    def network(self, directory: Path) -> tuple[list[str], float | None] | None:
        """For Containers.network: a folder's WorkBox in its tunnel's network (connected first), and
        when that network last started; None: the machine's."""
        tunnel = self.tunnel_for(directory)
        if tunnel is None:
            return None
        holder = self.ensure(tunnel)
        return [f"--network=container:{holder}"], started_at(self.names(tunnel)[0])

    def ensure(self, tunnel: dict[str, Any]) -> str:
        """The tunnel up and connected (at its exit now, else the next ones with a fallback loop); its
        network's holder's id. VpnError (with gluetun's log) when no exit connects."""
        with self.lock(tunnel["id"]):
            holder_name, name = self.names(tunnel)
            argv = self.holder_argv(tunnel)
            found = _state(holder_name)
            lost = found[0] == "running" and not network_alive(holder_name)
            if (found[1] is not None and found[1] != spec_of(argv)) or lost:
                # (other ports, or its network gone with an older Relay: gluetun and its WorkBoxes with it, anew)
                _remove(holder_name)
                self.state.pop(tunnel["id"], None)
                found = (None, None)
                if lost:
                    self._event(tunnel, "Its network was gone (Relay restarted under it): made anew", "warn")
            up(holder_name, spec_of(argv), argv, "the VPN's network", found, labels=(f"{LABEL}={tunnel['id']}",),
               scoped=True)
            holder = _inspect(holder_name)["id"]
            state = self._state(tunnel, name)
            index = state["exit"]
            argv = self.gluetun_argv(tunnel, holder, index)
            found = _inspect(name)
            state["used"] = time.time()  # (a WorkBox joins it now: not stopped as unused meanwhile)
            if (found.get("spec") == spec_of(argv) and found.get("status") == "running" and self.connected(name)
                    and self.public_ip(name)):
                return holder
            tried, first = [], state["exit"]
            for step in range(len(tunnel["exits"]) if tunnel["fallback"]["after"] else 1):
                index = (first + step) % len(tunnel["exits"])
                why = self._connect(tunnel, holder, index,
                                    "started" if not step else "fell back: the exit before didn't connect")
                if why is None:
                    state["used"] = time.time()
                    return holder
                tried.append(f"{self.exit_label(tunnel, index)}: {why}")
            self._event(tunnel,
                        "No exit connects: " + ", ".join(self.exit_label(tunnel, (first + n) % len(tunnel["exits"]))
                                                                  for n in range(len(tried))), "problem")
            raise VpnError(f"The VPN tunnel {tunnel['name']} didn't connect. " + " · ".join(tried))

    def _state(self, tunnel: dict[str, Any], name: str) -> dict[str, Any]:
        state = self.state.get(tunnel["id"])
        if state is None:  # (after a restart of Relay: the exit its gluetun is at)
            state = self.state[tunnel["id"]] = _fresh(_inspect(name).get("exit") or 0)
        if state["exit"] >= len(tunnel["exits"]):
            state["exit"] = 0
        return state

    def start_over(self, tunnel: dict[str, Any]) -> None:
        """Its settings changed: at its first exit again (ensure makes gluetun anew where it's not that)."""
        with self.lock(tunnel["id"]):
            self.state[tunnel["id"]] = _fresh(0)

    def running(self, tunnel: dict[str, Any]) -> bool:
        return _inspect(self.names(tunnel)[0]).get("status") == "running"

    def _connect(self, tunnel: dict[str, Any], holder: str, index: int, why: str) -> str | None:
        """gluetun anew at one of the tunnel's exits, in its network cleared of the last one's rules;
        None when it connected, else what went wrong (the end of its log)."""
        _, name = self.names(tunnel)
        state = self._state(tunnel, name)
        _remove(name)
        reset = subprocess.run(["podman", "run", "--rm", f"--network=container:{holder}", "--cap-add=NET_ADMIN",
                                "--entrypoint", "sh", self.image(), "-c", RESET], capture_output=True, text=True,
                               timeout=120)
        if reset.returncode != 0:
            return f"its network couldn't be cleared: {reset.stderr.strip()[-300:]}"
        argv = self.gluetun_argv(tunnel, holder, index)
        up(name, spec_of(argv), argv, "the VPN (gluetun)", (None, None),
           labels=(f"{LABEL}={tunnel['id']}", f"{EXIT}={index}"))
        state["exit"] = index  # (connected or not, it's at this one now: a fallback loop goes on from here)
        seen = self._wait(name, tunnel["fallback"]["after"] or READY_TIMEOUT)
        if seen is None:
            self._event(tunnel, f"{self.exit_label(tunnel, index)} didn't connect", "warn")
            return self.log(tunnel, 4)
        state.update(since=time.time(), why=why, down=None, rotated=time.time())
        where = f"{self.exit_label(tunnel, index)}, {seen.get('public_ip')}" + (f" ({_place(seen)})" if _place(seen)
                                                                                else "")
        self._event(tunnel, f"{why.capitalize()}: {where}")
        self._recovered(tunnel, where)
        return None

    def _wait(self, name: str, seconds: float) -> dict[str, Any] | None:
        """gluetun connected within so many seconds: its public address and place; None if not (or it stopped)."""
        time.sleep(GRACE)
        deadline = time.monotonic() + seconds
        while True:
            if _inspect(name).get("status") not in ("running", "created", "configured"):
                return None
            if self.connected(name):
                seen = self.public_ip(name)
                if seen:
                    return seen
            if time.monotonic() >= deadline:
                return None
            time.sleep(2)

    def _event(self, tunnel: dict[str, Any], text: str, kind: str = "info") -> None:
        """What happened to a tunnel (kind: info, warn, problem, recovered), kept; a problem notified."""
        with self.writing:
            kept = self.history.setdefault(tunnel["id"], [])
            kept.append({"time": time.time(), "text": text, "kind": kind})
            del kept[:-KEEP_EVENTS]
            write_json(config.data_dir() / "vpn-events.json", self.history)
        print(f"relay: VPN {tunnel['name']}: {text}", file=sys.stderr, flush=True)
        if kind == "problem" and self.notifier is not None and \
                time.time() - self.told.get(tunnel["id"], 0) >= TELL_EVERY:
            self.told[tunnel["id"]] = time.time()
            self.troubled.add(tunnel["id"])
            self.notifier.vpn(tunnel["name"], text)

    def _recovered(self, tunnel: dict[str, Any], where: str) -> None:
        """Connected again after a problem that was notified: that, once."""
        if tunnel["id"] in self.troubled:
            self.troubled.discard(tunnel["id"])
            self.told.pop(tunnel["id"], None)
            self._event(tunnel, f"Connected again: {where}", "recovered")
            if self.notifier is not None:
                self.notifier.vpn(tunnel["name"], f"Connected again: {where}", recovered=True)

    def forget(self, tunnel_id: str) -> None:
        """A deleted tunnel's history."""
        with self.writing:
            if self.history.pop(tunnel_id, None) is not None:
                write_json(config.data_dir() / "vpn-events.json", self.history)
        self.told.pop(tunnel_id, None)
        self.troubled.discard(tunnel_id)

    # Keeping them: the fallback loop and rotation (Scheduler, every tick)

    def tick(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if not self.pruned:  # (once: a removed tunnel's containers, the one VPN of before)
            self.pruned = True
            self.prune()
        busy = self.busy()
        joined = _joined()
        for tunnel in self.config()["tunnels"]:
            if not tunnel["exits"]:
                continue
            holder_name, name = self.names(tunnel)
            holder = _inspect(holder_name)
            if holder.get("status") != "running":
                continue  # (not used: it starts with the first WorkBox in it)
            if not network_alive(holder_name):  # (its network went with an older Relay: anew, as ensure does)
                try:
                    self.ensure(tunnel)
                except Exception as exc:  # noqa: BLE001 - said in its events; the next tick tries again
                    print(f"relay: VPN {tunnel['name']}: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
                continue
            lock = self.lock(tunnel["id"])
            if not lock.acquire(blocking=False):
                continue  # (it's being connected)
            try:
                state = self._state(tunnel, name)
                if joined.get(holder["id"]) or any((self.tunnel_for(f) or {}).get("id") == tunnel["id"] for f in busy):
                    state["used"] = now
                elif now - state["used"] >= IDLE_AFTER:
                    self._rest(tunnel)
                    continue
                self._keep(tunnel, name, now, busy)
            except Exception as exc:  # noqa: BLE001 - the other tunnels, and the next tick, go on
                print(f"relay: VPN {tunnel['name']}: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            finally:
                lock.release()

    def _keep(self, tunnel: dict[str, Any], name: str, now: float, busy: set[Path]) -> None:
        state = self._state(tunnel, name)
        holder = _inspect(self.names(tunnel)[0])["id"]
        fallback, rotate = tunnel["fallback"], tunnel["rotate"]
        running = _inspect(name).get("status") == "running"
        if not (running and self.connected(name) and self.public_ip(name)):
            state["down"] = state["down"] or now
            down = now - state["down"]
            if fallback["after"] and len(tunnel["exits"]) > 1 and down >= fallback["after"]:
                self._event(tunnel, f"{self.exit_label(tunnel, state['exit'])}: no connection for {int(down)} s; "
                                    "falling back", "problem")
                self._connect(tunnel, holder, (state["exit"] + 1) % len(tunnel["exits"]), "fell back")
            elif not running and down >= (fallback["after"] or READY_TIMEOUT):
                # (gluetun itself is gone, and there's no other exit to go to: anew at its own)
                self._event(tunnel, f"{self.exit_label(tunnel, state['exit'])}: gluetun stopped", "problem")
                self._connect(tunnel, holder, state["exit"], "started again")
            return
        if state["down"] is not None:  # (gluetun reconnected by itself)
            self._recovered(tunnel, self.exit_label(tunnel, state["exit"]))
        state["down"] = None
        if (state["exit"] and fallback["back"] and state["why"].startswith("fell back")
                and now - state["since"] >= fallback["back"] * 60):
            before = state["exit"]
            if self._connect(tunnel, holder, 0, "back to the first exit") is not None:
                self._connect(tunnel, holder, before, "fell back: the first exit still doesn't connect")
            return
        if rotate["minutes"] and now - state["rotated"] >= rotate["minutes"] * 60:
            if rotate["idle"] and any((self.tunnel_for(folder) or {}).get("id") == tunnel["id"] for folder in busy):
                return  # (an agent works there: later)
            if rotate["to"] == "next" and len(tunnel["exits"]) > 1:
                self._connect(tunnel, holder, (state["exit"] + 1) % len(tunnel["exits"]), "rotated")
            else:
                self.new_server(tunnel, "rotated")

    def rest(self, tunnel: dict[str, Any]) -> None:
        """Stopped now (Settings → System), as when unused: its first WorkBox used again starts it."""
        with self.lock(tunnel["id"]):
            self._state(tunnel, self.names(tunnel)[1])
            self._rest(tunnel, "Stopped from Settings → System")

    def _rest(self, tunnel: dict[str, Any], why: str = "") -> None:
        """Unused for a while: gluetun goes, its network's holder stops (its stopped WorkBoxes with
        it, keeping what they have); the first of them used again starts it again, at its first exit."""
        holder_name, name = self.names(tunnel)
        _remove(name)
        subprocess.run(["podman", "stop", "-t", "5", holder_name], capture_output=True, timeout=120)
        self._event(tunnel, why or f"Stopped: no WorkBox in it for {IDLE_AFTER // 60} minutes")
        self.state[tunnel["id"]] = _fresh(0)

    def new_server(self, tunnel: dict[str, Any], why: str = "reconnected") -> None:
        """Another server at the same exit: gluetun's own reconnect, in the same container."""
        _, name = self.names(tunnel)
        state = self._state(tunnel, name)
        for status in ("stopped", "running"):
            _gluetun(name, "http://127.0.0.1:8000/v1/vpn/status", control_key(), {"status": status})
        seen = self._wait(name, tunnel["fallback"]["after"] or READY_TIMEOUT)
        state["rotated"] = time.time()
        if seen is None:
            self._event(tunnel, f"{self.exit_label(tunnel, state['exit'])}: no connection after a new server", "warn")
            return
        state.update(down=None)
        where = f"{self.exit_label(tunnel, state['exit'])}, {seen.get('public_ip')}" + (f" ({_place(seen)})"
                                                                                        if _place(seen) else "")
        self._event(tunnel, f"{why.capitalize()}: {where}")
        self._recovered(tunnel, where)

    def reconnect(self, tunnel: dict[str, Any]) -> str:
        """For Settings: a new server at its exit (or it starts, when it isn't running); the holder's id."""
        before = (self.state.get(tunnel["id"]) or {}).get("since")
        holder = self.ensure(tunnel)
        if (self.state.get(tunnel["id"]) or {}).get("since") == before:
            with self.lock(tunnel["id"]):
                self.new_server(tunnel)
        return holder

    # For Settings

    def status(self, tunnel: dict[str, Any]) -> dict[str, Any]:
        holder_name, name = self.names(tunnel)
        running = _inspect(holder_name).get("status") == "running" and _inspect(name).get("status") == "running"
        connected = running and self.connected(name)
        seen = (self.public_ip(name) or {}) if connected else {}
        state = self.state.get(tunnel["id"]) or {}
        index = state.get("exit", _inspect(name).get("exit") or 0) if running else 0
        return {"running": running, "connected": bool(seen), "public_ip": seen.get("public_ip"),
                "place": _place(seen) or None, "exit": index,
                "exit_label": self.exit_label(tunnel, index) if tunnel["exits"] else None,
                "since": state.get("since") if running else None,
                "events": list(self.history.get(tunnel["id"]) or [])[::-1]}

    def connected(self, name: str) -> bool:
        """Its health server: the tunnel works (it answers with an error while it doesn't)."""
        return _gluetun(name, "http://127.0.0.1:9999/") is not None

    def public_ip(self, name: str) -> dict[str, Any] | None:
        """Its public address and where that is, as its control server knows them once it's connected."""
        answer = _gluetun(name, "http://127.0.0.1:8000/v1/publicip/ip", control_key())
        try:
            found = json.loads(answer or "")
        except ValueError:
            return None
        return found if isinstance(found, dict) and found.get("public_ip") else None

    def log(self, tunnel: dict[str, Any], lines: int = 80) -> str:
        """gluetun's last lines, without its web servers' requests (Relay's own, every few seconds)."""
        _, name = self.names(tunnel)
        done = subprocess.run(["podman", "logs", "--tail", str(lines * 10), name], capture_output=True, text=True,
                              timeout=30)
        kept = [line for line in (done.stdout + done.stderr).splitlines() if "[http server]" not in line][-lines:]
        return "\n".join(kept).strip()[-4000:] or "(no log)"

    def stop(self, tunnel: dict[str, Any]) -> None:
        """The tunnel gone: gluetun, its network, and the WorkBoxes in it (made anew when next used)."""
        holder_name, name = self.names(tunnel)
        with self.lock(tunnel["id"]):
            _remove(name)
            _remove(holder_name)
            self.state.pop(tunnel["id"], None)

    def prune(self) -> list[str]:
        """This Relay's tunnels' containers that no tunnel has any more (a removed tunnel's, the one VPN
        of before): gone, with the WorkBoxes in them. Their names."""
        listed = subprocess.run(["podman", "ps", "-a", "--filter", f"label={LABEL}", "--format",
                                 f'{{{{.Names}}}}|{{{{index .Labels "{LABEL}"}}}}|{{{{index .Labels '
                                 f'"io.relay.instance"}}}}'],
                                capture_output=True, text=True, timeout=60)
        known = {t["id"] for t in self.config()["tunnels"]}
        gone = []
        for line in listed.stdout.splitlines() if listed.returncode == 0 else []:
            name, tunnel_id, mark = (line.split("|") + ["", ""])[:3]
            if mark == instance() and tunnel_id not in known:
                _remove(name)
                gone.append(name)
        return gone


def origin(directory: Path | str) -> Path:
    """The project folder a WorkBox's folder stands for: a subagent's copy (relay.forks) its caller's
    folder, a task's own branch (a git worktree in Relay's data) its repository's; else itself."""
    folder = Path(directory).resolve()
    for _ in range(4):  # (a subagent's copy of a task's branch, of a copy, …)
        copies, branches = forks.folders().resolve(), config.worktrees_dir().resolve()
        if copies in folder.parents:
            fork = forks.load(folder.relative_to(copies).parts[0])
            copy = fork.folder.resolve() if fork else None
            if copy is None or not (folder == copy or copy in folder.parents):
                return folder
            folder = (fork.source / folder.relative_to(copy)).resolve()
        elif branches in folder.parents:
            try:
                top, common = git.run(folder, "rev-parse", "--show-toplevel", "--path-format=absolute",
                                      "--git-common-dir").splitlines()[:2]
            except (git.GitError, ValueError):
                return folder
            if Path(common).name != ".git":
                return folder
            return (Path(common).parent / folder.relative_to(Path(top).resolve())).resolve()
        else:
            return folder
    return folder


def _fresh(index: int) -> dict[str, Any]:
    return {"exit": index, "since": time.time(), "why": "", "down": None, "rotated": time.time(), "used": time.time()}


def _joined() -> dict[str, int]:
    """Per network (a container's id): how many of Relay's other running boxes are in it (the
    WorkBoxes in a tunnel's)."""
    listed = subprocess.run(["podman", "ps", "-q", "--filter", "label=io.relay.managed=1"], capture_output=True,
                            text=True, timeout=60)
    running = listed.stdout.split() if listed.returncode == 0 else []
    if not running:
        return {}
    found = subprocess.run(["podman", "container", "inspect", "--format",
                            f'{{{{.HostConfig.NetworkMode}}}}|{{{{index .Config.Labels "{LABEL}"}}}}', *running],
                           capture_output=True, text=True, timeout=60)
    counts: dict[str, int] = {}
    for line in found.stdout.splitlines():
        mode, _, tunnel = line.partition("|")
        if mode.startswith("container:") and not tunnel:  # (gluetun is in it too: not a WorkBox)
            counts[mode.removeprefix("container:")] = counts.get(mode.removeprefix("container:"), 0) + 1
    return counts


def _place(seen: dict[str, Any]) -> str:
    return ", ".join(p for p in (seen.get("city"), seen.get("country")) if p)


def _remove(name: str) -> None:
    """A container and those in its network (podman keeps one while others are in its network)."""
    for _ in range(2):  # (as containers.remove)
        if subprocess.run(["podman", "rm", "-f", "-t", "5", "--depend", name], capture_output=True,
                          timeout=120).returncode == 0:
            return


def servers(image: str = IMAGE) -> dict[str, Any]:
    """gluetun's providers and their servers' places, for Settings: {provider: {type: {place: [city, …]}},
    and per provider which SERVER_… setting its places are}. gluetun writes its server list where it
    starts (/gluetun/servers) and, without settings, stops: once per image, kept."""
    known = subprocess.run(["podman", "image", "inspect", "--format", "{{.Id}}", image], capture_output=True, text=True,
                           timeout=60)
    if known.returncode != 0:
        subprocess.run(["podman", "pull", "-q", image], capture_output=True, timeout=600)
        known = subprocess.run(["podman", "image", "inspect", "--format", "{{.Id}}", image], capture_output=True,
                               text=True, timeout=60)
    cache = paths.host_cache_dir() / "relay-vpn" / f"servers-{known.stdout.strip()[:12] or 'unknown'}.json"
    found = read_json(cache)
    if found.get("providers"):
        return found
    folder = Path(tempfile.mkdtemp(prefix="relay-vpn-servers-"))
    try:
        subprocess.run(["podman", "run", "--rm", "--cap-add=NET_ADMIN", "-v", f"{folder}:/gluetun/servers", image],
                       capture_output=True, timeout=120)
        providers: dict[str, Any] = {}
        for file in sorted(folder.glob("*.json")):
            listed = read_json(file).get("servers")
            if file.stem == "manifest" or not isinstance(listed, list):
                continue
            key, setting = next(((k, v) for k, v in PLACES if any(s.get(k) for s in listed)), PLACES[0])
            types: dict[str, dict[str, set[str]]] = {}
            for server in listed:
                place = types.setdefault(str(server.get("vpn") or "openvpn"), {}).setdefault(str(server.get(key) or ""),
                                                                                             set())
                if server.get("city"):
                    place.add(str(server["city"]))
            providers[file.stem] = {"setting": setting, "cities": any(s.get("city") for s in listed),
                                    "types": {t: {p: sorted(c) for p, c in sorted(places.items()) if p}
                                              for t, places in sorted(types.items())}}
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    if not providers:
        raise VpnError("gluetun's server list didn't come (podman run docker.io/qmcgaw/gluetun)")
    found = {"image": image, "providers": providers}
    write_json(cache, found)
    return found


def control_key() -> str:
    """The key to gluetun's control server: Relay's, made once."""
    file = config.data_dir() / "vpn-control.json"
    found = read_json(file).get("apikey")
    if not found:
        found = secrets.token_urlsafe(24)
        write_json(file, {"apikey": found})
    return found


def _gluetun(name: str, url: str, key: str | None = None, body: dict[str, Any] | None = None) -> str | None:
    """What gluetun's own servers answer, from inside it (a PUT with `body`); None when they don't
    (or with an error)."""
    header = ["--header", f"X-API-Key: {key}"] if key else []
    if body is not None:
        header += ["--header", "Content-Type: application/json", "--method=PUT", f"--body-data={json.dumps(body)}"]
    try:
        done = subprocess.run(["podman", "exec", name, "wget", "-qO-", "-T", "5", *header, url], capture_output=True,
                              text=True, timeout=15)
    except subprocess.TimeoutExpired:
        return None
    return done.stdout if done.returncode == 0 else None


def _state(name: str) -> tuple[str | None, str | None]:
    found = _inspect(name)
    return found.get("status"), found.get("spec")


def _inspect(name: str) -> dict[str, Any]:
    done = subprocess.run(["podman", "container", "inspect", "--format", "{{json .}}", name], capture_output=True,
                          text=True, timeout=60)
    if done.returncode != 0:
        return {}
    try:
        data = json.loads(done.stdout)
    except ValueError:
        return {}
    data = data[0] if isinstance(data, list) and data else data
    labels = (data.get("Config") or {}).get("Labels") or {}
    at = str(labels.get(EXIT) or "")
    return {"id": data.get("Id"), "status": (data.get("State") or {}).get("Status"),
            "spec": labels.get("io.relay.spec"),
            "exit": int(at) if at.isdigit() else None}


def lan_subnets() -> list[str]:
    """The subnets of this machine's own networks (not the containers'): where Relay is."""
    done = subprocess.run(["ip", "-j", "-4", "addr"], capture_output=True, text=True, timeout=10)
    try:
        links = json.loads(done.stdout)
    except ValueError:
        return []
    found = []
    for link in links:
        if link.get("ifname", "").startswith(("lo", "podman", "veth", "cni", "docker", "tun", "wg")):
            continue
        for address in link.get("addr_info") or []:
            if address.get("family") == "inet":
                found.append(str(ipaddress.ip_network(f"{address['local']}/{address['prefixlen']}", strict=False)))
    return found
