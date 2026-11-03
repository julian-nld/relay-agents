"""Relay's boxes for the web: web search (relay.search: SearXNG) and web fetch (relay.fetch: a
browser), each a container Relay starts and keeps, the same way.

- Started on demand (the first search, the first fetch); stopped after IDLE_AFTER unused.
- No port on this machine: Relay asks it from inside (podman exec wget), where it listens on
  127.0.0.1:PORT, out of the way of a tunnel's WorkBoxes' servers when it shares their network.
- Its route, the same for both (settings.search.tunnel): this machine's own connection, or one of
  the VPN tunnels (relay.vpn). Through a tunnel it runs in that tunnel's network as a WorkBox
  does, keeping the tunnel up while it runs; when the tunnel doesn't connect, nothing goes out:
  never around it, through this machine's own connection.
"""

from __future__ import annotations

import subprocess
import threading
import time
from typing import TYPE_CHECKING, Any

from relay import config
from relay.containers import ContainerError, instance, spec_of, started_at, up

if TYPE_CHECKING:
    from relay.vpn import Vpn

IDLE_AFTER = 30 * 60  # seconds unused before it stops
READY_TIMEOUT = 60  # seconds it has to answer once started


class WebError(Exception):
    pass


class WebBox:
    KIND = ""  # its container: relay-KIND
    LABEL = ""
    PORT = 0
    WHAT = ""  # what it is, for what goes wrong
    Error = WebError

    def __init__(self, settings: config.Settings, vpn: Vpn) -> None:
        self.settings = settings
        self.vpn = vpn
        self.lock = threading.Lock()
        self.used = 0.0  # when it was last asked (in memory: after a restart of Relay, it's unused)

    def argv(self) -> list[str]:
        """The container's own arguments, after its network: what it runs."""
        raise NotImplementedError

    def route(self) -> str:
        """"" for this machine's own connection, else a VPN tunnel's id."""
        found = self.settings.search if isinstance(self.settings.search, dict) else {}
        return str(found.get("tunnel") or "")

    def name(self) -> str:
        mark = instance()
        return f"relay-{self.KIND}-{mark}" if mark else f"relay-{self.KIND}"

    def ensure(self) -> None:
        """Running and answering, on its route (a tunnel's network, connected first); self.Error
        when it can't be."""
        with self.lock:
            route = self.route()
            network, joined = [], None
            if route:
                tunnel = self.vpn.tunnel(route)
                if tunnel is None:
                    raise self.Error("Web search and fetch go through a VPN tunnel that's gone: choose another in "
                                     "Settings → General → Web search and fetch")
                try:
                    holder = self.vpn.ensure(tunnel)
                except ContainerError as exc:  # (VpnError, say: no exit connects; never around it)
                    raise self.Error(f"{self.WHAT} goes through the VPN tunnel {tunnel['name']}, which isn't up: "
                                     f"{exc}") from exc
                network, joined = [f"--network=container:{holder}"], started_at(self.vpn.names(tunnel)[0])
            name = self.name()
            try:
                argv = [*network, *self.argv()]
                made = up(name, spec_of(argv), argv, f"Relay's {self.WHAT.lower()}", _state(name), labels=(self.LABEL,))
            except ContainerError as exc:
                raise self.Error(str(exc)) from exc
            if not made and joined and (started_at(name) or 0) < joined:
                # (in a tunnel's network: one started before it last did lost its network with it)
                subprocess.run(["podman", "restart", "-t", "1", name], capture_output=True, timeout=60)
            deadline = time.monotonic() + READY_TIMEOUT
            while self.ask("/healthz") is None:
                if time.monotonic() >= deadline:
                    raise self.Error(f"{self.WHAT} didn't answer within {READY_TIMEOUT} s: {self.log(6)}")
                time.sleep(1)
            self.used = time.time()

    def ask(self, path: str, timeout: int = 10) -> str | None:
        """What it answers, from inside its container; None when it doesn't (or with an error)."""
        try:
            done = subprocess.run(["podman", "exec", self.name(), "wget", "-qO-", "-T", str(timeout),
                                   "--header", "X-Real-IP: 127.0.0.1", f"http://127.0.0.1:{self.PORT}{path}"],
                                  capture_output=True, text=True, timeout=timeout + 15)
        except subprocess.TimeoutExpired:
            return None
        return done.stdout if done.returncode == 0 else None

    # Kept: stopped when unused (Scheduler)

    def running(self) -> bool:
        return _state(self.name())[0] == "running"

    def tick(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if now - self.used >= IDLE_AFTER and self.lock.acquire(blocking=False):
            try:
                if self.running():
                    subprocess.run(["podman", "stop", "-t", "5", self.name()], capture_output=True, timeout=60)
            finally:
                self.lock.release()

    def rest(self) -> None:
        """Stopped now (Settings → System): it starts again when next asked."""
        with self.lock:
            subprocess.run(["podman", "stop", "-t", "5", self.name()], capture_output=True, timeout=60)

    def status(self) -> dict[str, Any]:
        route = self.route()
        tunnel = self.vpn.tunnel(route) if route else None
        return {"running": self.running(),
                "route": tunnel["name"] if tunnel else ("(a tunnel that's gone)" if route else None)}

    def stop(self) -> None:
        """Gone (its route changed: made anew when next asked)."""
        with self.lock:
            subprocess.run(["podman", "rm", "-f", "-t", "5", self.name()], capture_output=True, timeout=60)

    def log(self, lines: int = 20) -> str:
        done = subprocess.run(["podman", "logs", "--tail", str(lines), self.name()], capture_output=True, text=True,
                              timeout=30)
        return (done.stdout + done.stderr).strip()[-2000:] or "(no log)"


def _state(name: str) -> tuple[str | None, str | None]:
    """(running/exited/…, its spec), or (None, None) when there's none."""
    done = subprocess.run(["podman", "container", "inspect", "--format",
                           '{{.State.Status}} {{index .Config.Labels "io.relay.spec"}}', name],
                          capture_output=True, text=True, timeout=60)
    if done.returncode != 0:
        return None, None
    state, _, spec = done.stdout.strip().partition(" ")
    return state, spec
