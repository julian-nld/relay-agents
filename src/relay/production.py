"""ProductionBoxes (AgentOS): live services, reached through Relay.

A service is software that runs and is reached: a start command, a public port and a health
check. It listens on $PORT (the port Relay gives it) and keeps its data in $DATA_DIR (/data,
a podman volume of its own, `relay-data-<id>`, which every version shares).

A version is a frozen copy of the code (RELAY_HOME/production/<service>/<n>/<name>) run in a
ProductionBox: a long-lived container of the WorkBox image, on an internal port of its own
(host networking, like every box). Relay listens on the service's public port and forwards
each connection (TCP: any protocol, websockets too) to the live version, so the address stays
when another version takes over. The boxes carry a label of their own (PROD_LABEL): the idle
WorkBoxes' tidying never touches them.

How a service changes: by the user (the service's page: Demote; Promote to live in the WorkBox's
chat) or by the Governor (its capabilities, relay.agentos), the same way:
    clone      (Demote, workbox.clone) the live version's code, and a snapshot of its data in
               .data, as a code task (a WorkBox) to work on: the user's own, or a managed one
    promote    (Promote to live, production.promote) that folder as a new version: started,
               health-checked, then the proxy switches and the old version is archived for
               ARCHIVE_DAYS (its data snapshotted first; the data itself stays, shared)
    rollback   back to an archived version, the same way
Relay checks every service's health (Scheduler); a failing one wakes the Governor. Its health as it
changed (service_health) and its data's size once a day (service_data) are kept, for a look back
(status: the last week). A service can be stopped (its versions and data kept: not served, not
checked) and started again.

A version goes building (its code copied) → starting (its box) → verifying (Relay's own health
check) → live, or failed; a live one that's replaced is archived. The switch (the new one live,
the old one archived, the service pointing at it) is one transaction, and only one operation
(promote, rollback, restart, delete) works on a service at a time; at Relay's start
(reconcile) a version left between states by a crash is failed and its box removed, exactly one
version is live, and boxes of no live version are stopped or removed.

Only a clone of the service, made from the version that's live now, can be promoted
(promote_clone): not some other folder, and not a clone the live version has moved on from. A clone's .data (its
copy of the data) never goes into production: the data stays in the service's volume.
"""

from __future__ import annotations

import asyncio
import contextlib
import filecmp
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opencode_manager import ports
from relay import config, tunables
from relay.containers import NESTED, NESTED_STORE

if TYPE_CHECKING:
    from relay.app import Relay

PROD_LABEL = "io.relay.production"
INTERNAL_PORTS = range(21000, 29000)
# Another Relay on this machine (a test run, the UI walkthrough: another data folder) takes its versions'
# ports from a range of its own: each knows only its own database's ports, and the usual one's mustn't meet
# theirs while both start a version at the same time.
OTHER_PORTS = range(29000, 31000)
ARCHIVE_DAYS = 7
DAILY_AFTER = 4  # the hour after which a service's daily snapshot is made (a quiet one: its box pauses)
START_TIMEOUT = 90  # seconds a new version has to answer its health check
HEALTH_EVERY = 60  # seconds between health checks
HISTORY_DAYS = 60  # days a service's health changes and data sizes are kept
FAILED_CHECKS = 2  # in a row before a service is told failing: not one that's starting (after a reboot)
WELL_CHECKS = 10  # in a row before a failing one counts as well again: one that comes and goes is one incident
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,29}$")
IN_FLIGHT = ("building", "starting", "verifying")  # a version on its way; left so by a crash: failed
SHELL_FILES = "/tmp/relay-terminal"  # in a ProductionBox: the user's shell's guard (Shell)
LEFT_OUT = (".data", ".git")  # not part of a version: the clone's data snapshot, history


class ProductionError(Exception):
    pass


def folders() -> Path:
    return config.data_dir() / "production"


def probe(port: int, health: str | None, timeout: float = 5) -> tuple[bool, str]:
    """Whether a version on `port` is well: its health path answers (below 500), or, without
    one, it accepts a connection."""
    if health and health.startswith("/"):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{health}", timeout=timeout) as response:
                return True, f"HTTP {response.status}"
        except urllib.error.HTTPError as exc:
            return exc.code < 500, f"HTTP {exc.code}"
        except (OSError, ValueError) as exc:
            return False, f"no answer ({getattr(exc, 'reason', exc)})"
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True, "accepts connections"
    except OSError as exc:
        return False, f"no connection ({exc})"


class Proxy:
    """Relay's listener on a service's public port: each connection to the live version."""

    def __init__(self) -> None:
        self.server: asyncio.base_events.Server | None = None
        self.target: int | None = None

    async def start(self, host: str, port: int) -> None:
        self.server = await asyncio.start_server(self._connection, host, port, reuse_address=True)

    async def _connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.target is None:
            writer.close()
            return
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection("127.0.0.1", self.target)
        except OSError:
            writer.close()
            return

        async def pipe(source: asyncio.StreamReader, sink: asyncio.StreamWriter, half: bool) -> None:
            try:
                while data := await source.read(65536):
                    sink.write(data)
                    await sink.drain()
                if half and sink.can_write_eof():
                    # The client is done sending, and may still want the answer: pass that on and
                    # keep the way back open
                    sink.write_eof()
                    return
            except (OSError, asyncio.CancelledError):
                pass
            for end in (writer, upstream_writer):  # (the service is done, or a way broke: over)
                end.close()
        try:
            await asyncio.gather(pipe(reader, upstream_writer, True), pipe(upstream_reader, writer, False))
        finally:
            for end in (writer, upstream_writer):
                try:
                    end.close()
                except OSError:
                    pass

    def close(self) -> None:
        if self.server is not None:
            self.server.close()


class Production:
    def __init__(self, relay: Relay) -> None:
        self.relay = relay
        self.proxies: dict[str, Proxy] = {}
        self.checked = 0.0
        self.failed: dict[str, int] = {}  # service -> health checks failed in a row
        self.well: dict[str, int] = {}  # service -> health checks well in a row, since it was told failing
        self.sized: dict[str, str] = {}  # service -> the day its data was last measured (or tried)
        self.busy: dict[str, dict[str, Any]] = {}  # service -> the operation on it now ({kind, source})

    @contextlib.asynccontextmanager
    async def operation(self, service: dict[str, Any], kind: str, source: str | None = None):
        """One operation on a service at a time: ProductionError when another is going on. Noted
        in the database too, so a crash in the middle is seen (reconcile)."""
        going = self.busy.get(service["id"])
        if going is not None:
            raise ProductionError(f"{service['name']} is busy ({going['kind']} going on): try again when it's done")
        self.busy[service["id"]] = {"kind": kind, "source": source}
        self.relay.db.execute("UPDATE services SET operation = ?, operation_at = ? WHERE id = ?",
                              (kind, time.time(), service["id"]))
        try:
            yield
        finally:
            self.busy.pop(service["id"], None)
            self.relay.db.execute("UPDATE services SET operation = NULL, operation_at = NULL WHERE id = ?",
                                  (service["id"],))

    def _event(self, service: dict[str, Any], kind: str, ok: bool = True, actor: str = "relay",
               version: int | None = None, **detail: Any) -> None:
        user = service.get("user_id") or (self.relay.db.one("SELECT user_id FROM services WHERE id = ?",
                                                            (service["id"],)) or {}).get("user_id")
        if user is not None:
            self.relay.agentos.event(user, kind, actor=actor, ok=ok, service=service["id"], version=version,
                                     **detail)

    # Podman

    def _podman(self, *args: str, timeout: float = 120) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(["podman", *args], capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProductionError(f"podman: {exc}") from exc

    def _image(self) -> str:
        if self.relay.containers is None:
            raise ProductionError("Services run in containers, and podman isn't available here")
        self.relay.containers.check()  # (the WorkBox image, built if it isn't there)
        return self.relay.containers.image

    # Services

    def services(self, user: int, managed: bool = False) -> list[dict[str, Any]]:
        """The user's services; `managed`: only those AgentOS looks after (what the Governor sees)."""
        rows = self.relay.db.all("SELECT * FROM services WHERE user_id = ? AND (managed = 1 OR ?) ORDER BY name",
                                 (user, int(not managed)))
        return [self._public(r) for r in rows]

    def get(self, user: int, wanted: str, managed: bool = False) -> dict[str, Any]:
        wanted = str(wanted or "").strip()
        rows = [r for r in self.relay.db.all("SELECT * FROM services WHERE user_id = ? AND (managed = 1 OR ?)",
                                             (user, int(not managed)))
                if wanted and (r["name"] == wanted or r["id"].startswith(wanted))]
        # Its name or whole id first: a short id can fit two (or another's name), which was "No service" then
        exact = [r for r in rows if wanted in (r["name"], r["id"])]
        if len(exact) == 1 or len(rows) == 1:
            return (exact or rows)[0]
        if rows:
            raise ProductionError(f"More than one service is {wanted}, or starts with it: give more of its id")
        raise ProductionError(f"No service {wanted or '(none)'}")

    def one(self, user: int, wanted: str) -> dict[str, Any]:
        return self._public(self.get(user, wanted))

    def _public(self, row: dict[str, Any]) -> dict[str, Any]:
        versions = self.relay.db.all("SELECT * FROM service_versions WHERE service_id = ? ORDER BY number DESC",
                                     (row["id"],))
        live = next((v for v in versions if v["id"] == row["live_version"]), None)
        workboxes = self.relay.db.all(  # its clones (Demote, workbox.clone) still there to work in
            "SELECT c.id, c.title, v.number AS from_version FROM service_clones k "
            "JOIN conversations c ON c.id = k.conversation_id LEFT JOIN service_versions v ON v.id = k.from_version "
            "WHERE k.service_id = ? AND coalesce(c.task_state, '') != 'discarded' ORDER BY k.created DESC",
                                      (row["id"],))
        return {**row, "health_ok": None if row["health_ok"] is None else bool(row["health_ok"]),
                "managed": bool(row["managed"]),
                "live": live, "versions": versions, "address": f"/port/{row['public_port']}/",
                "snapshots": self.snapshots(row),
                "workboxes": [{**w, "running": bool(self.relay.runs.active_run_for(w["id"]))} for w in workboxes]}

    async def create(self, user: int, name: str, source: Path, command: str, public_port: int,
                     health: str | None, managed: bool = False, by: str = "user") -> dict[str, Any]:
        """A new service, its first version made from `source` and started; the user's own, unless
        AgentOS is to look after it (`managed`). `by`: user, or governor:<run>."""
        name = str(name or "").strip().lower()
        if not NAME.match(name):
            raise ProductionError("A service's name: a-z, 0-9 and -, at most 30")
        if self.relay.db.one("SELECT 1 FROM services WHERE user_id = ? AND name = ?", (user, name)):
            raise ProductionError(f"There's a service called {name} already")
        if not str(command or "").strip():
            raise ProductionError("Say how it starts (the command; it listens on $PORT)")
        if not source.is_dir():
            raise ProductionError(f"{source} isn't a folder")
        try:
            public_port = int(public_port)
        except (TypeError, ValueError) as exc:
            raise ProductionError("The public port is a number") from exc
        if not 1024 <= public_port <= 65535 or public_port == self.relay.settings.port:
            raise ProductionError("The public port: 1024–65535, not Relay's own")
        if self.relay.db.one("SELECT 1 FROM services WHERE public_port = ?", (public_port,)) or \
                not ports.is_free(public_port, "0.0.0.0"):
            raise ProductionError(f"Port {public_port} is taken")
        service_id = uuid.uuid4().hex
        self.relay.db.execute("INSERT INTO services (id, user_id, name, command, public_port, health, managed, "
                              "created) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                              (service_id, user, name, command.strip(), public_port, (health or "").strip() or None,
                               int(bool(managed)), time.time()))
        service = self.relay.db.one("SELECT * FROM services WHERE id = ?", (service_id,))
        await asyncio.to_thread(self._podman, "volume", "create", "--ignore", _volume(service))
        await self._serve(service)
        self._event(service, "service.created", actor="governor" if by.startswith("governor") else "user",
                    name=name, port=public_port, by=by)
        try:
            await self.promote(service, source, note="first version", by=by)
        except ProductionError:
            pass  # the service stays, without a live version: its failure is in the version's note
        return self._public(self.relay.db.one("SELECT * FROM services WHERE id = ?", (service_id,)))

    async def create_from(self, user: int, task: dict[str, Any], name: str, command: str, public_port: int,
                          health: str | None, managed: bool = False, by: str = "user") -> dict[str, Any]:
        """A new service from a code task's folder (/promote, the Governor's service.create): the task goes on as
        its WorkBox, so what's changed there next becomes its next version (Promote to live, production.promote)."""
        if self.relay.db.one("SELECT 1 FROM service_clones WHERE conversation_id = ?", (task["id"],)):
            raise ProductionError("That task is a service's WorkBox already: promote it as that service's next version")
        made = await self.create(user, name, Path(task["directory"]), command, public_port, health, managed, by)
        # Made from what it has now: the live version, or the first one when that didn't come up
        version = made["live"] or (made["versions"][0] if made["versions"] else None)
        if version is None:
            return made
        self.relay.db.execute("INSERT INTO service_clones (conversation_id, service_id, from_version, created) "
                              "VALUES (?, ?, ?, ?)", (task["id"], made["id"], version["id"], time.time()))
        self._event(made, "workbox.linked", actor="governor" if by.startswith("governor") else "user",
                    conversation=task["id"], version=version["number"])
        return self.one(user, made["id"])

    async def delete(self, service: dict[str, Any]) -> None:
        """The service goes, with its versions, archive and data."""
        async with self.operation(service, "delete"):
            await self._delete(service)
        self._event(service, "service.deleted", name=service["name"])

    async def _delete(self, service: dict[str, Any]) -> None:
        proxy = self.proxies.pop(service["id"], None)
        if proxy is not None:
            proxy.close()
        for version in self.relay.db.all("SELECT * FROM service_versions WHERE service_id = ?", (service["id"],)):
            await asyncio.to_thread(self._podman, "rm", "-f", "-t", "5", _container(service, version))
        await asyncio.to_thread(self._podman, "volume", "rm", "-f", _volume(service), _volume(service, "containers"))
        for part in (folders() / service["id"]).glob("*"):
            if part.name == "work":
                continue  # (its WorkBoxes stay, with their chats: plain code tasks now)
            if part.is_dir() and not part.is_symlink():
                shutil.rmtree(part, ignore_errors=True)
            else:
                part.unlink(missing_ok=True)
        with self.relay.db.transaction() as conn:
            conn.execute("DELETE FROM service_clones WHERE service_id = ?", (service["id"],))
            conn.execute("DELETE FROM service_versions WHERE service_id = ?", (service["id"],))
            conn.execute("DELETE FROM services WHERE id = ?", (service["id"],))

    # Versions

    async def promote(self, service: dict[str, Any], source: Path, note: str = "",
                      provenance: dict[str, Any] | None = None, by: str = "user") -> str:
        """`source` (a WorkBox's folder) as the service's new live version, if it starts and is
        well; the old one is archived. ProductionError (with what went wrong) if not."""
        source = Path(source)
        if not source.is_dir():
            raise ProductionError(f"{source} isn't a folder")
        async with self.operation(service, "promote", str(source)):
            service = self._fresh(service)
            number = (self.relay.db.one("SELECT max(number) AS n FROM service_versions WHERE service_id = ?",
                                        (service["id"],))["n"] or 0) + 1
            folder = folders() / service["id"] / str(number) / service["name"]
            provenance = provenance or {}
            version = {"id": uuid.uuid4().hex, "service_id": service["id"], "number": number, "folder": str(folder),
                       "source": str(source), "status": "building", "note": note, "created": time.time(),
                       "origin_conversation": provenance.get("conversation"), "origin_title": provenance.get("title"),
                       "from_version": provenance.get("from_version"),
                       "agents": json.dumps(provenance["agents"]) if "agents" in provenance else None}
            self.relay.db.execute(
                "INSERT INTO service_versions (id, service_id, number, folder, source, status, note, created, "
                "origin_conversation, origin_title, from_version, agents) VALUES (:id, :service_id, :number, :folder, "
                ":source, :status, :note, :created, :origin_conversation, :origin_title, :from_version, :agents)",
                                  version)
            try:
                await asyncio.to_thread(_copy, source, folder)
            except OSError as exc:
                self._mark(version, "failed", f"Couldn't copy {source}: {exc}")
                raise ProductionError(f"Couldn't copy {source}: {exc}") from exc
            return await self._take_over(service, version, by)

    async def promote_clone(self, service: dict[str, Any], conversation: dict[str, Any], by: str) -> str:
        """A WorkBox as the live version: only one of this service's (Demote, workbox.clone, a code task
        made into it with /promote), made from the version that's live now (or none is live), and not
        working at the moment. Promoted, it's made from that version: it can go on, and promote again."""
        clone = self.relay.db.one("SELECT * FROM service_clones WHERE conversation_id = ?", (conversation["id"],))
        if clone is None or clone["service_id"] != service["id"]:
            raise ProductionError(f"[{conversation['id'][:8]}] isn't a clone of {service['name']}: only what "
                                  f"Demote or workbox.clone service={service['name']} made can become its live version")
        if service["live_version"] and clone["from_version"] != service["live_version"]:
            made = self.relay.db.one("SELECT number FROM service_versions WHERE id = ?", (clone["from_version"],))
            live = self.relay.db.one("SELECT number FROM service_versions WHERE id = ?",
                                     (service["live_version"] or "",))
            raise ProductionError(
                f"The clone was made from version {made['number'] if made else '?'}, but version "
                f"{live['number'] if live else '(none)'} is live now: promoting it would undo what came since. "
                "Update it from the live version first (the user's Update from live merges it in), or clone again.")
        if self.relay.runs.active_run_for(conversation["id"]):
            raise ProductionError("Its agent is still working: promote it when it's done")
        if not Path(conversation["directory"]).is_dir():
            raise ProductionError("The clone's folder is gone")
        made = self.relay.db.one("SELECT number FROM service_versions WHERE id = ?", (clone["from_version"],))
        agents = [{"provider": r["provider_id"], "model": r["model"]} for r in self.relay.db.all(
            "SELECT DISTINCT provider_id, model FROM runs WHERE conversation_id = ? AND provider_id IS NOT NULL",
            (conversation["id"],))]
        message = await self.promote(service, Path(conversation["directory"]),
                                     note=f"from {conversation['title']} ({conversation['id'][:8]})",
                                     provenance={"conversation": conversation["id"], "title": conversation["title"],
                                                 "from_version": made["number"] if made else None, "agents": agents},
                                     by=by)
        self.relay.db.execute("UPDATE service_clones SET from_version = "
                              "(SELECT live_version FROM services WHERE id = ?) WHERE conversation_id = ?",
                              (service["id"], conversation["id"]))
        return message

    async def rollback(self, service: dict[str, Any], number: int | None = None, by: str = "user") -> str:
        """Back to an archived version (the latest before the live one, or `number`)."""
        async with self.operation(service, "rollback"):
            service = self._fresh(service)
            archived = self.relay.db.all("SELECT * FROM service_versions WHERE service_id = ? AND status = 'archived' "
                                         "ORDER BY number DESC", (service["id"],))
            version = next((v for v in archived if number is None or v["number"] == int(number)), None)
            if version is None or not Path(version["folder"]).is_dir():
                raise ProductionError("No archived version to go back to" + (f" (number {number})" if number else ""))
            return await self._take_over(service, version, by)

    def _fresh(self, service: dict[str, Any]) -> dict[str, Any]:
        """The service as it is now (another operation may have changed it), or ProductionError when gone."""
        row = self.relay.db.one("SELECT * FROM services WHERE id = ?", (service["id"],))
        if row is None:
            raise ProductionError(f"The service {service['name']} is gone")
        return row

    async def _take_over(self, service: dict[str, Any], version: dict[str, Any], by: str) -> str:
        """Start `version`, check it, then switch the proxy to it and archive the live one."""
        live = self.relay.db.one("SELECT * FROM service_versions WHERE id = ?", (service["live_version"] or "",))
        snapshot = None
        if live is not None:  # its data as it was before another version touches it
            snapshot = await asyncio.to_thread(self._snapshot, service, version, live)
        # A version that doesn't come up: failed; an archived one (a rollback) stays archived.
        fail = "archived" if version["status"] == "archived" else "failed"
        # The port is taken at once, with no await in between: another service's promote going on at
        # the same time (they're one operation at a time per service only) sees it taken.
        port = _free_port(self.relay.db, version["id"])
        self.relay.db.execute("UPDATE service_versions SET status = 'starting', note = ?, port = ? WHERE id = ?",
                              (version.get("note"), port, version["id"]))
        try:
            await asyncio.to_thread(self._start, service, version, port)
        except ProductionError as exc:
            self.relay.db.execute("UPDATE service_versions SET port = NULL WHERE id = ?", (version["id"],))
            self._mark(version, fail, str(exc))
            self._event(service, "version.failed", ok=False, version=version["number"], by=by, why=str(exc)[:500])
            raise
        self.relay.db.execute("UPDATE service_versions SET status = 'verifying', port = ? WHERE id = ?",
                              (port, version["id"]))
        started = time.monotonic()
        ok, detail = await self._wait_well(service, version, port)
        validation = {"check": service["health"] or "a connection", "ok": ok, "detail": detail,
                      "seconds": round(time.monotonic() - started, 1), "port": port}
        if not ok:
            logs = await asyncio.to_thread(self.logs, service, version, 40)
            await asyncio.to_thread(self._podman, "rm", "-f", "-t", "5", _container(service, version))
            self._mark(version, fail, f"{detail}\n{logs}".strip())
            self.relay.db.execute("UPDATE service_versions SET validation = ?, port = NULL WHERE id = ?",
                                  (json.dumps(validation), version["id"]))
            self._event(service, "version.failed", ok=False, version=version["number"], by=by, validation=validation)
            # It ran on the service's data meanwhile: what it changed there (a migration) stays
            data = (f" It ran on the service's data meanwhile: if it changed it, the data as it was before is in "
                    f"{snapshot} (podman volume import {_volume(service)} …, with the service stopped)."
                    if snapshot else "")
            raise ProductionError(f"Version {version['number']} didn't come up well ({detail}); "
                                  f"{'the live version stays' if live else 'nothing is live'}.{data} Its log:\n{logs}")
        now = time.time()
        replaced = live["number"] if live is not None and live["id"] != version["id"] else None
        with self.relay.db.transaction() as conn:  # the switch: all of it, or none
            conn.execute("UPDATE service_versions SET status = 'live', port = ?, archived = NULL, validation = ?, "
                         "promoted_by = ?, promoted_at = ?, replaced_version = ? WHERE id = ?",
                         (port, json.dumps(validation), by, now, replaced, version["id"]))
            conn.execute("UPDATE services SET live_version = ?, health_ok = 1, health_detail = ?, health_at = ?, "
                         "alerted_at = NULL, stopped = NULL WHERE id = ?", (version["id"], detail, now, service["id"]))
            # Well again, if it was failing: its history says so (the next check sees it well already)
            last = conn.execute("SELECT ok FROM service_health WHERE service_id = ? ORDER BY at DESC LIMIT 1",
                                (service["id"],)).fetchone()
            if last is not None and not last[0]:
                conn.execute("INSERT INTO service_health (service_id, at, ok, detail) VALUES (?, ?, 1, ?)",
                             (service["id"], now, f"version {version['number']} live ({detail})"))
            if replaced is not None:
                conn.execute("UPDATE service_versions SET status = 'archived', archived = ? WHERE id = ?",
                             (now, live["id"]))
        proxy = self.proxies.get(service["id"])
        if proxy is not None:
            proxy.target = port
        if replaced is not None:
            await asyncio.to_thread(self._podman, "stop", "-t", "10", _container(service, live))
        self._event(service, "version.live", version=version["number"], by=by, replaced=replaced,
                    validation=validation, origin=version.get("origin_conversation"))
        return (f"Version {version['number']} of {service['name']} is live ({detail})"
                + (f"; version {live['number']} is archived for {ARCHIVE_DAYS} days." if replaced else "."))

    def _start(self, service: dict[str, Any], version: dict[str, Any], port: int) -> None:
        name = _container(service, version)
        for _ in range(2):  # (one whose conmon is gone may only go the second time)
            if self._podman("rm", "-f", "-t", "5", name).returncode == 0:
                break
        folder = version["folder"]
        self._restore_data(service)
        # --init: the command isn't PID 1, so a stop ends it at once instead of after podman's timeout
        # (Containers in it too, as in a WorkBox: relay.containers NESTED; their images kept over versions)
        argv = ["run", "-d", "--init", "--name", name, "--label", f"{PROD_LABEL}={service['id']}", "--network=host",
                "--restart=on-failure", "-v", f"{_volume(service)}:/data", "-v", f"{folder}:{folder}", "-w", folder,
                *NESTED, "-v", f"{_volume(service, 'containers')}:{NESTED_STORE}",
                "-e", f"PORT={port}", "-e", "DATA_DIR=/data", self._image(), "bash", "-lc", service["command"]]
        started = self._podman(*argv, timeout=600)
        if started.returncode != 0:
            raise ProductionError("Couldn't start its box: " + (started.stderr.strip().splitlines() or ["?"])[-1])

    def _restore_data(self, service: dict[str, Any]) -> None:
        """A backup restored on a machine without the service's data volume (relay backup has it in the
        data folder's volumes/): the volume is made from it, before a box would make it empty. When that
        fails, the box isn't made (ProductionError), and the next start tries again."""
        saved = config.data_dir() / "volumes" / f"{_volume(service)}.tar"
        if not saved.is_file() or self._podman("volume", "exists", _volume(service)).returncode == 0:
            return
        self._podman("volume", "create", _volume(service))
        try:
            done = self._podman("volume", "import", _volume(service), str(saved), timeout=600)
            why = None if done.returncode == 0 else (done.stderr.strip().splitlines() or ["?"])[-1]
        except ProductionError as exc:
            why = str(exc)
        if why is None:
            saved.rename(saved.with_name(saved.name + ".imported"))  # (once: a later backup writes a new one)
            print(f"relay: service {service['name']}: its data is back from the backup", flush=True)
            return
        # Not left empty: an empty volume would look restored, and the service would run on it from then on
        self._podman("volume", "rm", "-f", _volume(service))
        raise ProductionError(f"Its data couldn't be put back from {saved} ({why}): it isn't started without it")

    async def _wait_well(self, service: dict[str, Any], version: dict[str, Any], port: int) -> tuple[bool, str]:
        deadline = time.monotonic() + START_TIMEOUT
        detail = "not started"
        box = _container(service, version)

        async def state() -> tuple[str, int]:
            found = await asyncio.to_thread(self._podman, "inspect", "--format", "{{.State.Status}} {{.RestartCount}}",
                                            box)
            status, _, restarts = found.stdout.strip().partition(" ")
            return status, int(restarts) if restarts.isdigit() else 0
        _, before = await state()  # (a restarted box keeps the count of crashes it had before)
        while time.monotonic() < deadline:
            ok, detail = await asyncio.to_thread(probe, port, service["health"], 3)
            if ok:
                return True, detail
            # One that crashes is started again by --restart=on-failure: "stopped" in between, never
            # "exited" for long, so its restarts tell.
            status, restarts = await state()
            if restarts > before:
                return False, f"it stopped (started again {restarts - before} times)"
            if status in ("exited", "dead", "stopped"):
                return False, "it stopped"
            await asyncio.sleep(.5)
        return False, f"{detail} after {START_TIMEOUT} s"

    # Its data's snapshots: before each version takes over, once a day, before one is put back

    def snapshots(self, service: dict[str, Any]) -> list[dict[str, Any]]:
        """Its data's snapshots, newest first: {name, kind (version, daily, restore), version, at, bytes}."""
        found = []
        for path in (folders() / service["id"] / "snapshots").glob("*.tar"):
            try:
                stat = path.stat()
            except OSError:
                continue
            name = path.name.removesuffix(".tar")
            kind = ("version" if name.startswith("before-") and name[7:].isdigit() else "daily"
                    if name.startswith("daily-") else "restore" if name.startswith("before-restore-") else "other")
            found.append({"name": name, "kind": kind, "version": int(name[7:]) if kind == "version" else None,
                          "at": stat.st_mtime, "bytes": stat.st_size})
        return sorted(found, key=lambda s: s["at"], reverse=True)

    def daily_due(self, now: float) -> bool:
        """Whether a live service's daily snapshot is due (cheap: the scheduler asks every hour)."""
        if not tunables.get("services.daily_snapshots") or time.localtime(now).tm_hour < DAILY_AFTER:
            return False
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        live = self.relay.db.all("SELECT id FROM services WHERE live_version IS NOT NULL AND stopped IS NULL")
        return any(not (folders() / s["id"] / "snapshots" / f"daily-{today}.tar").exists() for s in live)

    async def daily_snapshots(self, now: float) -> None:
        """Each live service's data once a day (after DAILY_AFTER, a quiet hour: its box is paused while it's
        copied), the newest services.daily_snapshots kept. One busy now (an operation) waits for the next hour."""
        keep = tunables.get("services.daily_snapshots")
        if not keep or time.localtime(now).tm_hour < DAILY_AFTER:
            return
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        for service in self.relay.db.all("SELECT * FROM services WHERE live_version IS NOT NULL AND stopped IS NULL"):
            folder = folders() / service["id"] / "snapshots"
            target = folder / f"daily-{today}.tar"
            if target.exists() or service["id"] in self.busy:
                continue
            try:
                async with self.operation(service, "snapshot"):
                    live = self.relay.db.one("SELECT * FROM service_versions WHERE id = ?", (service["live_version"],))
                    if live is None:
                        continue
                    folder.mkdir(parents=True, exist_ok=True)
                    why = await asyncio.to_thread(self._export, service, target, live)
            except ProductionError:
                continue  # (busy after all: next hour)
            if why is not None:
                self._event(service, "data.snapshot_failed", ok=False, why=why[:500], daily=today)
            for old in sorted(folder.glob("daily-*.tar"))[:-keep]:
                old.unlink(missing_ok=True)

    async def restore(self, service: dict[str, Any], name: str, by: str = "user") -> str:
        """Its data put back as one of its snapshots has it. The box stops meanwhile, the data as it is now is
        kept first (before-restore-…: putting that back undoes this), and it's started again and checked."""
        async with self.operation(service, "restore"):
            service = self._fresh(service)
            source = next((folders() / service["id"] / "snapshots" / f"{s['name']}.tar"
                           for s in self.snapshots(service) if s["name"] == name), None)
            if source is None:
                raise ProductionError(f"{service['name']} has no snapshot {name!r} (its page lists them)")
            live = self.relay.db.one("SELECT * FROM service_versions WHERE id = ?", (service["live_version"] or "",))
            running = live is not None and not service.get("stopped")
            box = _container(service, live) if live is not None else None
            stamp = time.strftime("%Y%m%d-%H%M%S")
            kept, more = folders() / service["id"] / "snapshots" / f"before-restore-{stamp}.tar", 2
            while kept.exists():  # (undone in the same second: not over the one put back)
                kept, more = kept.with_name(f"before-restore-{stamp}-{more}.tar"), more + 1
            if running:
                await asyncio.to_thread(self._podman, "stop", "-t", "10", box)
            try:
                why = await asyncio.to_thread(self._export, service, kept, None)
                if why is not None:
                    raise ProductionError(f"The data as it is now couldn't be kept first ({why}): nothing was changed")
                why = await asyncio.to_thread(self._replace_data, service, source)
                if why is not None:
                    back = await asyncio.to_thread(self._replace_data, service, kept)
                    raise ProductionError(f"Its data couldn't be put back from {name} ({why}); "
                                          + ("it's as it was." if back is None else f"nor as it was ({back}): "
                                             f"{kept} has it."))
            finally:
                if running:
                    await asyncio.to_thread(self._podman, "start", box)
            detail = "not running"
            if running:
                ok, detail = await self._wait_well(service, live, live["port"])
                detail = f"{'well' if ok else 'not well'} ({detail})"
            self._event(service, "data.restored", actor=by, snapshot=name, kept=kept.name)
            return (f"{service['name']}'s data is as it was in {name}; the data before is kept as {kept.stem} (put "
                    f"that back to undo this). It's {detail}.")

    def _replace_data(self, service: dict[str, Any], source: Path) -> str | None:
        """Its data volume emptied and filled from `source` (a tar): why it failed, or None."""
        cleared = self._podman("run", "--rm", "--network=none", "-v", f"{_volume(service)}:/data", self._image(),
                               "find", "/data", "-mindepth", "1", "-delete", timeout=300)
        if cleared.returncode != 0:
            return (cleared.stderr.strip().splitlines() or ["couldn't empty it"])[-1]
        imported = self._podman("volume", "import", _volume(service), str(source), timeout=600)
        return None if imported.returncode == 0 else (imported.stderr.strip().splitlines() or ["podman failed"])[-1]

    def _snapshot(self, service: dict[str, Any], version: dict[str, Any], live: dict[str, Any]) -> str | None:
        """The data as it is, before `version` takes over (to put back by hand if needed): where it is, or
        None when it couldn't be made (said in the service's log of events)."""
        target = folders() / service["id"] / "snapshots" / f"before-{version['number']}.tar"
        target.parent.mkdir(parents=True, exist_ok=True)
        why = self._export(service, target, live)
        if why is not None:
            self._event(service, "data.snapshot_failed", ok=False, version=version["number"], why=why[:500])
            return None
        self.relay.db.execute("UPDATE service_versions SET data_snapshot = ? WHERE id = ?",
                              (str(target), version["id"]))
        return str(target)

    def _export(self, service: dict[str, Any], target: Path, live: dict[str, Any] | None) -> str | None:
        """Its data volume into `target`, with its live box paused meanwhile (export_data). Why it failed, or None."""
        return export_data(_volume(service), _container(service, live) if live is not None else None, target,
                           self._podman)

    def _mark(self, version: dict[str, Any], status: str, note: str) -> None:
        self.relay.db.execute("UPDATE service_versions SET status = ?, note = ? WHERE id = ?",
                              (status, note[-4000:], version["id"]))

    def logs(self, service: dict[str, Any], version: dict[str, Any] | None = None, lines: int = 80) -> str:
        version = version or self.relay.db.one("SELECT * FROM service_versions WHERE id = ?",
                                               (service["live_version"] or "",))
        if version is None:
            # Nothing live (a first version that didn't come up): the last one tried, with why (its note has its log)
            last = self.relay.db.one("SELECT * FROM service_versions WHERE service_id = ? ORDER BY number DESC LIMIT 1",
                                     (service["id"],))
            if last is None or last["status"] != "failed" or not last["note"]:
                return "(nothing is live)"
            return f"(nothing is live) Version {last['number']}, {last['status']}:\n{last['note'][-12000:]}"
        found = self._podman("logs", "--tail", str(lines), _container(service, version))
        return (found.stdout + found.stderr).strip()[-12000:] or "(no output)"

    async def restart(self, service: dict[str, Any]) -> str:
        if self._fresh(service).get("stopped"):
            return await self.start_service(service)
        async with self.operation(service, "restart"):
            service = self._fresh(service)
            live = self.relay.db.one("SELECT * FROM service_versions WHERE id = ?", (service["live_version"] or "",))
            if live is None:
                raise ProductionError("Nothing is live")
            restarted = await asyncio.to_thread(self._podman, "restart", "-t", "10", _container(service, live))
            if restarted.returncode != 0:  # (its box is gone, or broken: made anew)
                await asyncio.to_thread(self._start, service, live, live["port"])
            ok, detail = await self._wait_well(service, live, live["port"])
        self._event(service, "service.restarted", ok=ok, version=live["number"], detail=detail)
        return f"Restarted version {live['number']}: {'well' if ok else 'not well'} ({detail})."

    async def stop_service(self, service: dict[str, Any]) -> str:
        """Stopped: not served (its address answers nothing) and not checked; its versions and data stay,
        for start_service."""
        async with self.operation(service, "stop"):
            service = self._fresh(service)
            live = self.relay.db.one("SELECT * FROM service_versions WHERE id = ?", (service["live_version"] or "",))
            proxy = self.proxies.get(service["id"])
            target = proxy.target if proxy is not None else None
            if proxy is not None:
                proxy.target = None
            if live is not None:
                try:
                    stopped = await asyncio.to_thread(self._podman, "stop", "-t", "10", _container(service, live))
                    if stopped.returncode != 0 and "no such container" not in stopped.stderr.lower():
                        raise ProductionError("Couldn't stop its box: "
                                              + (stopped.stderr.strip().splitlines() or ["?"])[-1])
                except ProductionError:
                    if proxy is not None:
                        proxy.target = target  # (it goes on serving, as it still runs)
                    raise
            self.relay.db.execute("UPDATE services SET stopped = ?, alerted_at = NULL WHERE id = ?",
                                  (time.time(), service["id"]))
            self.failed.pop(service["id"], None)
        self._event(service, "service.stopped", version=live["number"] if live else None)
        return f"Stopped {service['name']}: its address answers nothing now; its versions and data are kept."

    async def start_service(self, service: dict[str, Any]) -> str:
        """A stopped service served again: its live version started (made anew when its box is gone),
        checked, then its address goes to it."""
        async with self.operation(service, "start"):
            service = self._fresh(service)
            live = self.relay.db.one("SELECT * FROM service_versions WHERE id = ?", (service["live_version"] or "",))
            if live is None:
                raise ProductionError("Nothing is live to start")
            started = await asyncio.to_thread(self._podman, "start", _container(service, live))
            if started.returncode != 0:
                await asyncio.to_thread(self._start, service, live, live["port"])
            ok, detail = await self._wait_well(service, live, live["port"])
            proxy = self.proxies.get(service["id"])
            if proxy is not None:
                proxy.target = live["port"]
            self.relay.db.execute("UPDATE services SET stopped = NULL WHERE id = ?", (service["id"],))
        self._event(service, "service.started", ok=ok, version=live["number"], detail=detail)
        return f"Started {service['name']} (version {live['number']}): {'well' if ok else 'not well'} ({detail})."

    def history(self, service: dict[str, Any], days: int = 7, now: float | None = None) -> dict[str, Any]:
        """The last `days`: when it failed and was well again (with why), and its data's size then and now."""
        now = time.time() if now is None else now
        since = now - days * 86400
        changes = self.relay.db.all("SELECT at, ok, detail FROM service_health WHERE service_id = ? AND at >= ? "
                                    "ORDER BY at", (service["id"], since))
        sizes = self.relay.db.all("SELECT day, bytes FROM service_data WHERE service_id = ? AND day >= ? ORDER BY day",
                                  (service["id"], time.strftime("%Y-%m-%d", time.localtime(since))))
        # Since when Relay keeps it (a well service has no changes): a history younger than `days` says so
        first = self.relay.db.one("SELECT MIN(at) AS at FROM service_health WHERE service_id = ?", (service["id"],))
        day = self.relay.db.one("SELECT MIN(day) AS day FROM service_data WHERE service_id = ?", (service["id"],))
        kept = [t for t in (first["at"], day["day"] and time.mktime(time.strptime(day["day"], "%Y-%m-%d"))) if t]
        return {"days": days, "changes": changes, "data": sizes, "from": since,
                "kept_since": min(kept) if kept else None}

    def _note_health(self, service: dict[str, Any], ok: bool, detail: str, now: float) -> None:
        """A change (well <-> failing) into its history; once a day its data's size."""
        if service["health_ok"] is None or bool(service["health_ok"]) != ok:
            self.relay.db.execute("INSERT INTO service_health (service_id, at, ok, detail) VALUES (?, ?, ?, ?)",
                                  (service["id"], now, int(ok), detail))
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        if self.sized.get(service["id"]) == day:
            return  # (once a day, also when it couldn't be measured: a huge volume isn't counted every minute)
        self.sized[service["id"]] = day
        if self.relay.db.one("SELECT 1 FROM service_data WHERE service_id = ? AND day = ?", (service["id"], day)):
            return
        size = self._data_size(service)
        if size is not None:
            self.relay.db.execute("INSERT OR REPLACE INTO service_data (service_id, day, bytes) VALUES (?, ?, ?)",
                                  (service["id"], day, size))

    def _data_size(self, service: dict[str, Any]) -> int | None:
        """Its data volume's size in bytes (as its files' owner: podman unshare), or None."""
        try:
            found = self._podman("volume", "inspect", "--format", "{{.Mountpoint}}", _volume(service), timeout=30)
            if found.returncode != 0 or not found.stdout.strip():
                return None
            # (timeout ends du itself in time: killing podman unshare would leave it counting on)
            counted = subprocess.run(["podman", "unshare", "timeout", "50", "du", "-sb", found.stdout.strip()],
                                     capture_output=True, text=True, timeout=60)
            return int(counted.stdout.split()[0]) if counted.returncode == 0 and counted.stdout.split() else None
        except (ProductionError, OSError, subprocess.TimeoutExpired, ValueError):
            return None

    def shell(self, service: dict[str, Any]) -> tuple[Shell, Path]:
        """The user's interactive shell in the live version's box (relay.terminal), and its folder."""
        live = self.relay.db.one("SELECT * FROM service_versions WHERE id = ?", (service["live_version"] or "",))
        if live is None:
            raise ProductionError(f"Nothing of {service['name']} is live")
        state = self._podman("inspect", "--format", "{{.State.Status}}", _container(service, live))
        if state.stdout.strip() != "running":
            raise ProductionError(f"{service['name']}'s box isn't running (restart it)")
        return Shell(_container(service, live)), Path(live["folder"])

    # A WorkBox to work on a service

    async def clone(self, user: int, service: dict[str, Any], provider_id: str, model: str | None = None,
                    agent_mode: str | None = None, governor_run: str | None = None) -> dict[str, Any]:
        """The live version's code, with a snapshot of the data in .data, as a code task whose agent
        is `provider_id` (`model`): the Governor's (`governor_run`) is managed, the user's own isn't."""
        from relay.routes.conversations import open_conversation
        going = self.busy.get(service["id"])
        if going:
            raise ProductionError(f"{service['name']} is busy ({going['kind']} going on): try again when that's done")
        live = self.relay.db.one("SELECT * FROM service_versions WHERE id = ?", (service["live_version"] or "",))
        if live is None:
            raise ProductionError(f"Nothing of {service['name']} is live to work on")
        folder = folders() / service["id"] / "work" / uuid.uuid4().hex[:8] / service["name"]
        await asyncio.to_thread(_copy, Path(live["folder"]), folder)
        export = folder.parent / "data.tar"
        failed = await asyncio.to_thread(self._export, service, export, live)
        (folder / ".data").mkdir(exist_ok=True)
        if failed is None:
            with tarfile.open(export) as archive:
                archive.extractall(folder / ".data", filter="data")
            export.unlink(missing_ok=True)
        else:
            (folder / ".data" / "NOT-COPIED.txt").write_text(f"The service's data couldn't be copied here: {failed}\n")
        row = await open_conversation(self.relay, user, {
            "provider_id": provider_id, "model": model, "mode": "code", "directory": str(folder), "isolate": False,
            "agent_mode": agent_mode, "title": f"{service['name']}: work on version {live['number']}"}, anywhere=True)
        with self.relay.db.transaction() as conn:
            if governor_run:
                conn.execute("UPDATE conversations SET managed = 1 WHERE id = ?", (row["id"],))
            conn.execute("INSERT INTO service_clones (conversation_id, service_id, from_version, "
                         "governor_run, created) VALUES (?, ?, ?, ?, ?)",
                         (row["id"], service["id"], live["id"], governor_run, time.time()))
        self._event(service, "workbox.cloned", actor="governor" if governor_run else "user", version=live["number"],
                    conversation=row["id"])
        return self.relay.db.one("SELECT * FROM conversations WHERE id = ?", (row["id"],))

    def promoting(self, conversation: dict[str, Any]) -> bool:
        """Whether this WorkBox is being promoted right now (its chat can't go meanwhile)."""
        clone = self.relay.db.one("SELECT * FROM service_clones WHERE conversation_id = ?", (conversation["id"],))
        going = self.busy.get(clone["service_id"]) if clone else None
        return bool(going and going.get("source") == conversation["directory"])

    async def update_clone(self, service: dict[str, Any], conversation: dict[str, Any]) -> dict[str, Any]:
        """A WorkBox the live version has moved on from (the Governor's fix, a rollback, another WorkBox
        promoted) brought up to date with it: a three-way merge of the version it was made from, the
        live one and the WorkBox, so both sides' changes stay. {merged, conflicts, kept}: files taken
        or merged; files with conflict markers in them now; files changed on both sides that couldn't
        be merged (binary, a symlink, or reached through a folder that's a symlink), the WorkBox's kept;
        one the WorkBox deleted that changed live is back, the live version's (its change would be lost
        otherwise: delete it again if it should go)."""
        clone = self.relay.db.one("SELECT * FROM service_clones WHERE conversation_id = ?", (conversation["id"],))
        if clone is None or clone["service_id"] != service["id"]:
            raise ProductionError(f"[{conversation['id'][:8]}] isn't a WorkBox of {service['name']}")
        live = self.relay.db.one("SELECT * FROM service_versions WHERE id = ?", (service["live_version"] or "",))
        if live is None:
            raise ProductionError(f"Nothing of {service['name']} is live to update from")
        if clone["from_version"] == live["id"]:
            return {"merged": [], "conflicts": [], "kept": [], "version": live["number"]}
        if self.relay.runs.active_run_for(conversation["id"]):
            raise ProductionError("Its agent is still working: update it when it's done")
        going = self.busy.get(service["id"])
        if going:
            raise ProductionError(f"{service['name']} is busy ({going['kind']} going on): try again when that's done")
        base = self.relay.db.one("SELECT * FROM service_versions WHERE id = ?", (clone["from_version"],))
        if base is None or not Path(base["folder"]).is_dir():
            raise ProductionError(f"The version this WorkBox was made from is gone (archived versions go after "
                                  f"{ARCHIVE_DAYS} days), so there's nothing to merge against: Demote again and "
                                  "carry your changes over")
        ours = Path(conversation["directory"])
        if not ours.is_dir():
            raise ProductionError("The WorkBox's folder is gone")
        found = await asyncio.to_thread(_merge, Path(base["folder"]), Path(live["folder"]), ours,
                                        (f"version {base['number']}", f"version {live['number']} (live)"))
        self.relay.db.execute("UPDATE service_clones SET from_version = ? WHERE conversation_id = ?",
                              (live["id"], conversation["id"]))
        self._event(service, "workbox.updated", actor="user", version=live["number"], conversation=conversation["id"],
                    from_version=base["number"], conflicts=found["conflicts"][:20])
        return {**found, "version": live["number"]}

    def forget_clone(self, conversation: dict[str, Any]) -> None:
        """A WorkBox's chat is gone: its record, and its folder (with its data snapshot) unless another
        conversation works there (a fork of it, a reviewer)."""
        self.relay.db.execute("DELETE FROM service_clones WHERE conversation_id = ?", (conversation["id"],))
        folder = Path(conversation["directory"])
        if folders() in folder.parents and not self.relay.db.one(
                "SELECT 1 FROM conversations WHERE directory = ? AND id != ?", (str(folder), conversation["id"])):
            shutil.rmtree(folder.parent, ignore_errors=True)  # (production/<service>/work/<clone>/)

    # Running

    async def start(self) -> None:
        """At Relay's start: what a crash left between states put right (reconcile), then each
        service's proxy, and its live version running."""
        try:
            await asyncio.to_thread(self.reconcile)
        except Exception as exc:  # noqa: BLE001 - the services still start
            print(f"relay: services: couldn't reconcile: {exc}", file=sys.stderr, flush=True)
        for service in self.relay.db.all("SELECT * FROM services"):
            try:
                await self._start_one(service)
            except Exception as exc:  # noqa: BLE001 - one service's trouble (podman gone or hanging) isn't Relay's
                print(f"relay: service {service['name']}: couldn't start it: {exc}", file=sys.stderr, flush=True)

    async def _start_one(self, service: dict[str, Any]) -> None:
        """At Relay's start: the service's proxy, and its live version running (made anew if it must)."""
        await self._serve(service)
        live = self.relay.db.one("SELECT * FROM service_versions WHERE id = ?", (service["live_version"] or "",))
        if live is not None and not service.get("stopped"):
            state = await asyncio.to_thread(self._podman, "inspect", "--format",
                                            "{{.State.Status}} {{.State.ConmonPid}}",
                                            _container(service, live))
            status, _, conmon = state.stdout.strip().partition(" ")
            # Running, but its conmon is gone (an older Relay's, killed with it under systemd): its
            # output has nowhere to go, so a print fails in it. Made anew.
            orphaned = status == "running" and not _alive(conmon)
            if state.returncode != 0 or status != "running" or orphaned:
                if state.returncode == 0 and not orphaned:
                    started = await asyncio.to_thread(self._podman, "start", _container(service, live))
                    if started.returncode == 0:
                        return
                # Gone (a machine without its containers), orphaned, or it won't start: made anew
                print(f"relay: service {service['name']}: its box is made anew", flush=True)
                await asyncio.to_thread(self._start, service, live, live["port"])

    def reconcile(self) -> list[str]:
        """After a crash (or a stop) in the middle of an operation: versions on their way are failed
        and their boxes removed, exactly one version is live (the service's live_version), and the
        boxes of other versions are stopped (archived) or removed (unknown). What was put right."""
        fixed: list[str] = []
        now = time.time()
        services = {s["id"]: s for s in self.relay.db.all("SELECT * FROM services")}
        for service in services.values():
            if service["operation"]:
                fixed.append(f"{service['name']}: its {service['operation']} broke off")
                self.relay.db.execute("UPDATE services SET operation = NULL, operation_at = NULL WHERE id = ?",
                                      (service["id"],))
            for version in self.relay.db.all("SELECT * FROM service_versions WHERE service_id = ?", (service["id"],)):
                if version["status"] in IN_FLIGHT:
                    self._podman("rm", "-f", "-t", "5", _container(service, version))
                    # (one that was archived, going back to it, stays archived: still one to roll back to)
                    self._mark(version, "archived" if version["archived"] else "failed",
                               f"Broke off while {version['status']} (Relay stopped)")
                    self.relay.db.execute("UPDATE service_versions SET port = NULL WHERE id = ?", (version["id"],))
                    fixed.append(f"{service['name']} version {version['number']}: broke off while {version['status']}")
                elif version["status"] == "live" and version["id"] != service["live_version"]:
                    self.relay.db.execute("UPDATE service_versions SET status = 'archived', archived = ? WHERE id = ?",
                                          (now, version["id"]))
                    fixed.append(f"{service['name']} version {version['number']}: archived (another is live)")
                elif version["id"] == service["live_version"] and version["status"] != "live":
                    self.relay.db.execute("UPDATE service_versions SET status = 'live' WHERE id = ?", (version["id"],))
                    fixed.append(f"{service['name']} version {version['number']}: live again")
            if service["live_version"] and not self.relay.db.one("SELECT 1 FROM service_versions WHERE id = ?",
                                                                 (service["live_version"],)):
                self.relay.db.execute("UPDATE services SET live_version = NULL WHERE id = ?", (service["id"],))
                fixed.append(f"{service['name']}: its live version was gone")
        # Boxes: only the live versions' run; one of a version this service doesn't have (any more) goes.
        # A box of a service this database doesn't know is left alone: podman may be shared with
        # another Relay (another RELAY_HOME, a test run), whose service it is.
        listed = self._podman("ps", "-a", "--filter", f"label={PROD_LABEL}", "--format",
                              f'{{{{.Names}}}} {{{{.State}}}} {{{{index .Labels "{PROD_LABEL}"}}}}')
        live = {(s["id"], v["number"]) for s in services.values() for v in self.relay.db.all(
            "SELECT number FROM service_versions WHERE id = ?", (s["live_version"] or "",))}
        for line in listed.stdout.splitlines() if listed.returncode == 0 else []:
            name, _, rest = line.partition(" ")
            state, _, service_id = rest.partition(" ")
            match = re.match(r"^relay-prod-[0-9a-f]{8}-(\d+)$", name)
            number = int(match.group(1)) if match else None
            if service_id not in services:
                continue
            known = number is not None and self.relay.db.one(
                "SELECT 1 FROM service_versions WHERE service_id = ? AND number = ?", (service_id, number))
            if not known:
                self._podman("rm", "-f", "-t", "5", name)
                fixed.append(f"removed {name} (no such version)")
            elif (service_id, number) not in live and state == "running":
                self._podman("stop", "-t", "10", name)
                fixed.append(f"stopped {name} (not live)")
        for line in fixed:
            print(f"relay: services: {line}", flush=True)
        for service in services.values():
            mine = [line for line in fixed if line.startswith((service["name"] + " ", service["name"] + ":"))]
            if mine:
                self._event(service, "reconciled", what=mine)
        return fixed

    async def _serve(self, service: dict[str, Any]) -> None:
        if service["id"] in self.proxies:
            return
        proxy = Proxy()
        live = self.relay.db.one("SELECT port FROM service_versions WHERE id = ?", (service["live_version"] or "",))
        proxy.target = live["port"] if live and not service.get("stopped") else None
        try:
            await proxy.start(self.relay.settings.host, service["public_port"])
        except OSError as exc:
            print(f"relay: service {service['name']}: can't listen on port {service['public_port']} ({exc})",
                  file=sys.stderr, flush=True)
            return
        self.proxies[service["id"]] = proxy

    def stop(self) -> None:
        for proxy in self.proxies.values():
            proxy.close()
        self.proxies.clear()

    async def check(self, now: float) -> list[dict[str, Any]]:
        """Every service's health (every HEALTH_EVERY seconds); the ones newly failing: FAILED_CHECKS
        in a row, so a box still starting (Relay just started, after a reboot) isn't one. A service
        an operation is working on (a restart, a promote) isn't checked."""
        if now - self.checked < HEALTH_EVERY:
            return []
        self.checked = now
        failing = []
        for service in self.relay.db.all("SELECT * FROM services WHERE live_version IS NOT NULL AND stopped IS NULL"):
            live = self.relay.db.one("SELECT * FROM service_versions WHERE id = ?", (service["live_version"],))
            if live is None or live["port"] is None or service["id"] in self.busy or service["operation"]:
                continue  # (an operation on it: Relay's own, or a backup copying its data with its box paused)
            ok, detail = await asyncio.to_thread(probe, live["port"], service["health"])
            if self.checked != now:
                return []  # (a later check started while this one probed: its results are the ones that count)
            await asyncio.to_thread(self._note_health, service, ok, detail, now)
            self.relay.db.execute("UPDATE services SET health_ok = ?, health_detail = ?, health_at = ? WHERE id = ?",
                                  (int(ok), detail, now, service["id"]))
            if ok:
                self.failed.pop(service["id"], None)
                if service["alerted_at"]:
                    self.well[service["id"]] = self.well.get(service["id"], 0) + 1
                    if self.well[service["id"]] >= WELL_CHECKS:
                        self.well.pop(service["id"], None)
                        self.relay.db.execute("UPDATE services SET alerted_at = NULL WHERE id = ?", (service["id"],))
                continue
            self.well.pop(service["id"], None)
            self.failed[service["id"]] = self.failed.get(service["id"], 0) + 1
            if self.failed[service["id"]] >= FAILED_CHECKS and not service["alerted_at"]:
                failing.append({**service, "health_detail": detail})
        return failing

    def status(self, service: dict[str, Any]) -> dict[str, Any]:
        """What runs, in detail (the Governor's service.status, the service's page): the live version's
        box (state, since when, restarts, last exit, OOM, its conmon), its processes, Relay's last health
        check and one now, an operation going on, and the versions."""
        service = self.relay.db.one("SELECT * FROM services WHERE id = ?", (service["id"],)) or service
        live = self.relay.db.one("SELECT * FROM service_versions WHERE id = ?", (service["live_version"] or "",))
        versions = self.relay.db.all("SELECT number, status, port, note, created, archived FROM service_versions "
                                     "WHERE service_id = ? ORDER BY number DESC LIMIT 6", (service["id"],))
        found: dict[str, Any] = {
            "name": service["name"], "public_port": service["public_port"], "command": service["command"],
            "health_path": service["health"], "operation": (self.busy.get(service["id"]) or {}).get("kind"),
            "health": {"ok": None if service["health_ok"] is None else bool(service["health_ok"]),
                       "detail": service["health_detail"], "at": service["health_at"],
                       "failed_in_a_row": self.failed.get(service["id"], 0), "told_at": service["alerted_at"]},
            "live": {"number": live["number"], "port": live["port"], "folder": live["folder"]} if live else None,
            "versions": [{**v, "note": (v["note"] or "").strip().splitlines()[0][:200] if v["note"] else None}
                         for v in versions],
            "box": None, "processes": None, "probe": None, "stopped": service.get("stopped"),
            "history": self.history(service),
            "snapshots": self.snapshots(service),
        }
        if live is None:
            return found
        box = _container(service, live)
        inspected = self._podman("inspect", "--format", "{{json .State}}|{{.RestartCount}}|{{.HostConfig.Init}}", box)
        if inspected.returncode != 0:
            found["box"] = {"name": box, "status": "missing"}
        else:
            state_json, restarts, init = (inspected.stdout.strip().split("|") + ["", ""])[:3]
            try:
                state = json.loads(state_json)
            except json.JSONDecodeError:
                state = {}
            conmon = str(state.get("ConmonPid") or 0)
            found["box"] = {"name": box, "status": state.get("Status"), "started_at": state.get("StartedAt"),
                            "finished_at": state.get("FinishedAt"), "exit_code": state.get("ExitCode"),
                            "oom_killed": state.get("OOMKilled"), "error": state.get("Error") or None,
                            "restarts": int(restarts) if restarts.isdigit() else None, "init": init == "true",
                            "conmon_alive": _alive(conmon) if state.get("Status") == "running" else None}
            if state.get("Status") == "running":
                top = self._podman("top", box, "pid", "etime", "args", timeout=30)
                found["processes"] = top.stdout.strip().splitlines()[:16] if top.returncode == 0 else None
        if live["port"]:
            ok, detail = probe(live["port"], service["health"], 3)
            found["probe"] = {"ok": ok, "detail": detail}
        return found

    def box_states(self) -> dict[str, str]:
        """Every ProductionBox's state as podman says it (e.g. "running (Up 2 hours)"), by name."""
        listed = self._podman("ps", "-a", "--filter", f"label={PROD_LABEL}", "--format",
                              "{{.Names}}|{{.State}} ({{.Status}})")
        return dict(line.split("|", 1) for line in listed.stdout.splitlines()
                    if "|" in line) if listed.returncode == 0 else {}

    def alerted(self, service: dict[str, Any], now: float) -> None:
        """Its failing was told: not again until it's well (kept, so a restart of Relay doesn't either)."""
        self.relay.db.execute("UPDATE services SET alerted_at = ? WHERE id = ?", (now, service["id"]))

    def tidy(self, now: float) -> None:
        """Archived versions older than ARCHIVE_DAYS go (box and code); their data snapshots too; history
        older than HISTORY_DAYS."""
        self.relay.db.execute("DELETE FROM service_health WHERE at < ?", (now - HISTORY_DAYS * 86400,))
        self.relay.db.execute("DELETE FROM service_data WHERE day < ?",
                              (time.strftime("%Y-%m-%d", time.localtime(now - HISTORY_DAYS * 86400)),))
        for version in self.relay.db.all("SELECT v.*, s.name AS service_name FROM service_versions v JOIN services s "
                                         "ON s.id = v.service_id WHERE v.status IN ('archived', 'failed') "
                                         "AND coalesce(v.archived, v.created) < ?", (now - ARCHIVE_DAYS * 86400,)):
            service = {"id": version["service_id"], "name": version["service_name"]}
            if service["id"] in self.busy:
                continue  # (an operation on it may be going back to this one: next time)
            self._podman("rm", "-f", "-t", "5", _container(service, version))
            shutil.rmtree(Path(version["folder"]).parent, ignore_errors=True)
            if version["data_snapshot"]:
                Path(version["data_snapshot"]).unlink(missing_ok=True)
            self.relay.db.execute("DELETE FROM service_versions WHERE id = ?", (version["id"],))


class Shell:
    """What relay.terminal.Terminal needs of a box, for a ProductionBox: the WorkBox shell's
    disaster guard is copied in first (a ProductionBox mounts none of Relay's folders). `shell` and `env`: for
    a ProviderBox (relay.creations), its real shell and what its program needs."""

    def __init__(self, box: str, shell: str = "bash", env: dict[str, str] | None = None) -> None:
        self.box = box
        self.shell = shell
        self.env = env or {}

    def name(self, directory: Path) -> str:
        return self.box

    def terminal_argv(self, directory: Path, tag: str) -> list[str]:
        from relay import guard
        from relay.containers import CONTAINERFILE
        put = [subprocess.run(["podman", "exec", self.box, "mkdir", "-p", SHELL_FILES], capture_output=True,
                              timeout=30)]
        for source, target in ((Path(guard.__file__), "terminal-guard.py"),
                               (CONTAINERFILE.parent / "terminal.bash", "terminal.bash")):
            put.append(subprocess.run(["podman", "cp", str(source), f"{self.box}:{SHELL_FILES}/{target}"],
                                      capture_output=True, text=True, timeout=30))
        if any(done.returncode for done in put):
            raise ProductionError("Couldn't prepare a shell in the box: " + (put[-1].stderr or "").strip())
        return ["podman", "exec", "-it", "-w", str(directory), "-e", "TERM=xterm-256color",
                "-e", f"RELAY_TERMINAL={tag}", "-e", f"RELAY_TERMINAL_GUARD={SHELL_FILES}/terminal-guard.py",
                *[flag for key, value in self.env.items() for flag in ("-e", f"{key}={value}")],
                self.box, self.shell, "--rcfile", f"{SHELL_FILES}/terminal.bash", "-i"]

    def close_terminal(self, name: str, tag: str) -> None:
        from relay.containers import end_terminal
        end_terminal(name, tag)


def _alive(pid: str) -> bool:
    return pid.isdigit() and int(pid) > 0 and Path(f"/proc/{pid}").exists()


def _container(service: dict[str, Any], version: dict[str, Any]) -> str:
    return f"relay-prod-{service['id'][:8]}-{version['number']}"


def export_data(volume: str, box: str | None, target: Path,
                podman: Callable[..., subprocess.CompletedProcess]) -> str | None:
    """A service's data volume into `target` (a tar), with `box` (its live box) paused meanwhile, so what's
    copied is the data at one moment (as after a power cut: a database recovers from it), not files from
    different moments. `podman(*args, timeout=…)` runs podman. Why it failed, or None. (A box podman
    can't pause is copied running.)"""
    def said(*args: str, timeout: float) -> tuple[bool, str]:
        try:
            done = podman(*args, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - production's podman raises on a time-out: said, like a failure
            return False, str(exc)
        return done.returncode == 0, (done.stderr.strip().splitlines() or [f"podman {args[0]} failed"])[-1]
    paused = box is not None and said("pause", box, timeout=30)[0]
    try:
        copied, why = said("volume", "export", volume, "--output", str(target), timeout=600)
    finally:
        if paused and not said("unpause", box, timeout=30)[0]:
            print(f"relay: {box} couldn't be unpaused after its data was copied: podman unpause {box}",
                  file=sys.stderr, flush=True)
    if not copied:
        target.unlink(missing_ok=True)
        return why
    return None


def _volume(service: dict[str, Any], kind: str = "data") -> str:
    """Its data's volume (/data), or its containers' images' (containers)."""
    return f"relay-{kind}-{service['id'][:12]}"


def guess(db, folder: Path, relay_port: int) -> dict[str, Any]:
    """For a new service from `folder` (/promote's dialog): a name, how it likely starts (it listens on
    $PORT), a public port that's free, and whether its code keeps data in $DATA_DIR (data_dir): one that
    doesn't leaves what it keeps behind with each version."""
    name = re.sub(r"[^a-z0-9-]+", "-", folder.name.lower()).strip("-")[:30] or "service"
    command = ""
    try:
        scripts = json.loads((folder / "package.json").read_text()).get("scripts") or {}
    except (OSError, ValueError, AttributeError):
        scripts = {}
    if "start" in scripts:
        command = "npm start"
    else:
        command = next((f"npm run {s}" for s in ("dev:web", "serve", "dev") if s in scripts), "")
    if not command:
        command = next((f"python3 {f}" for f in ("app.py", "main.py", "server.py") if (folder / f).is_file()), "")
    if not command and any((folder / f).is_file() for f in ("compose.yaml", "compose.yml", "docker-compose.yaml",
                                                            "docker-compose.yml")):
        command = "docker compose up"  # (podman in the box: relay-compose)
    taken = {r["public_port"] for r in db.all("SELECT public_port FROM services")}
    port = next(p for p in range(8500, 9500) if p not in taken and p != relay_port and ports.is_free(p, "0.0.0.0"))
    return {"name": name, "command": command, "port": port, "data_dir": _mentions(folder, "DATA_DIR")}


def _mentions(folder: Path, word: str, files: int = 3000) -> bool:
    """Whether the project's own code (not its dependencies) says `word` anywhere, in its first `files` files."""
    skip = {*LEFT_OUT, "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next", "target"}
    for root, folders, names in os.walk(folder):
        folders[:] = [f for f in folders if f not in skip]
        for name in names:
            files -= 1
            if files < 0:
                return False
            path = Path(root, name)
            try:
                if path.stat().st_size < 1_000_000 and word.encode() in path.read_bytes():
                    return True
            except OSError:
                continue
    return False


def _files(root: Path) -> dict[str, Path]:
    """A version's (or WorkBox's) files by their path in it, without what's LEFT_OUT of a version."""
    found = {}
    for folder, folders, names in os.walk(root):
        if Path(folder) == root:
            folders[:] = [f for f in folders if f not in LEFT_OUT]
            names = [n for n in names if n not in LEFT_OUT]
        for name in names + [f for f in folders if Path(folder, f).is_symlink()]:
            path = Path(folder, name)
            found[str(path.relative_to(root))] = path
    return found


def _same(one: Path | None, other: Path | None) -> bool:
    if one is None or other is None:
        return one is other
    if one.is_symlink() or other.is_symlink():
        return one.is_symlink() and other.is_symlink() and os.readlink(one) == os.readlink(other)
    return filecmp.cmp(one, other, shallow=False)


def _binary(path: Path) -> bool:
    with open(path, "rb") as file:
        return b"\0" in file.read(8192)


def _merge(base: Path, theirs: Path, ours: Path, labels: tuple[str, str]) -> dict[str, list[str]]:
    """Three-way: what changed from `base` to `theirs` goes into `ours`, where ours changed too merged
    (git merge-file, conflict markers where both changed the same lines). Files only ours has stay."""
    merged, conflicts, kept = [], [], []
    old, new, mine = _files(base), _files(theirs), _files(ours)
    for name in sorted(set(old) | set(new)):
        b, t, o = old.get(name), new.get(name), mine.get(name)
        if _same(b, t) or _same(t, o):
            continue  # the live version didn't change it, or the WorkBox has that change already
        target = ours / name
        if not _inside(ours, target):  # (a folder on the way is a symlink an agent made: it could point anywhere)
            kept.append(name)
            continue
        if _same(b, o):  # only the live version changed it: take that
            if t is None:
                target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.is_symlink() or target.exists():
                    target.unlink()
                shutil.copy2(t, target, follow_symlinks=False)
            merged.append(name)
            continue
        # Changed on both sides
        if t is None or o is None or t.is_symlink() or o.is_symlink() or _binary(t) or _binary(o) \
                or (b is not None and _binary(b)):
            if o is None and t is not None:  # deleted in the WorkBox, changed live: the change is kept
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(t, target, follow_symlinks=False)
            kept.append(name)
            continue
        with tempfile.NamedTemporaryFile() as empty:
            done = subprocess.run(["git", "merge-file", "-L", "your WorkBox", "-L", labels[0], "-L", labels[1],
                                   str(target), str(b or empty.name), str(t)], capture_output=True)
        (conflicts if done.returncode > 0 else merged if done.returncode == 0 else kept).append(name)
    return {"merged": merged, "conflicts": conflicts, "kept": kept}


def _inside(root: Path, target: Path) -> bool:
    """Whether writing `target` stays in `root`: no folder on the way there is a symlink. Relay's own process
    writes it, so a symlink a WorkBox's agent made (config -> ~/.config) mustn't be followed."""
    folder = target.parent
    while folder != root:
        if folder.is_symlink():
            return False
        folder = folder.parent
    return True


def _copy(source: Path, target: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, symlinks=True, ignore=shutil.ignore_patterns(*LEFT_OUT))


def _free_port(db, version_id: str | None = None) -> int:
    taken = {r["port"] for r in db.all("SELECT port FROM service_versions WHERE port IS NOT NULL AND id != ? "
                                       "AND status IN ('live', 'starting', 'verifying')", (version_id or "",))}
    from relay.containers import instance
    for port in OTHER_PORTS if instance() else INTERNAL_PORTS:
        if port not in taken and ports.is_free(port):
            return port
    raise ProductionError("No free internal port")


def describe_status(found: dict[str, Any]) -> str:
    """Production.status as text, for the Governor."""
    live = found["live"]
    lines = [f"{found['name']}: public port {found['public_port']}; starts with: {found['command']}; health check: "
             f"{found['health_path'] or 'a connection'}"]
    if found["operation"]:
        lines.append(f"Going on now: {found['operation']} (its box may be down meanwhile)")
    if live is None:
        lines.append("Nothing is live.")
    else:
        lines.append(f"Live: version {live['number']}, internal port {live['port']}, code in {live['folder']}")
    if found.get("stopped"):
        lines.append(f"STOPPED (by hand) since {_stamp(found['stopped'])}: not served or checked; service.start "
                     "brings it back")
    box = found["box"]
    if box and box["status"] == "missing":
        lines.append(f"Box {box['name']}: gone (Relay makes it anew when it starts, "
                     "or production.rollback / a promote)")
    elif box:
        lines.append(f"Box {box['name']}: {box['status']}; started {box['started_at'] or '?'}"
                     + (f"; podman restarted it {box['restarts']} times (it crashed)" if box["restarts"] else
                        "; never restarted by podman") + (f"; last exit code {box['exit_code']} at "
                     f"{box['finished_at']}" if box["finished_at"] and not str(box["finished_at"]).startswith("0001")
                     else "") + ("; KILLED FOR MEMORY (OOM)" if box["oom_killed"] else "")
                     + (f"; error: {box['error']}" if box["error"] else "")
                     + ("; its conmon is gone: its output has nowhere to go (restarting Relay makes it anew)"
                        if box["conmon_alive"] is False else ""))
    if found["processes"]:
        lines.append("Processes in it:\n  " + "\n  ".join(found["processes"]))
    health = found["health"]
    state = "never checked" if health["ok"] is None else ("well" if health["ok"] else "FAILING")
    lines.append(f"Relay's health check: {state} ({health['detail'] or '-'}) at {_stamp(health['at'])}; failed "
                 f"{health['failed_in_a_row']} times in a row; "
                 + (f"told (you or the user) at {_stamp(health['told_at'])}"
                                                                    if health["told_at"] else "not told"))
    if found["probe"]:
        lines.append(f"Checked just now: {'answers' if found['probe']['ok'] else 'NO ANSWER'} "
                     f"({found['probe']['detail']})")
    lines += describe_history(found.get("history"))
    if found.get("snapshots"):
        listed = "; ".join(f"{s['name']} ({_stamp(s['at'])}, {_size(s['bytes'])})" for s in found["snapshots"][:12])
        lines.append(f"Its data's snapshots (service.restore snapshot=NAME puts one back; the data now is kept first): "
                     f"{listed}")
    for version in found["versions"]:
        lines.append(f"- version {version['number']}: {version['status']}"
                     + (f" ({version['note']})" if version["note"] else ""))
    return "\n".join(lines)


def describe_history(history: dict[str, Any] | None) -> list[str]:
    """Its last days, as lines: the times it failed (how long, why), and its data's size then and now."""
    if not history:
        return []
    lines = []
    failed, started = [], None
    for change in history["changes"]:
        if not change["ok"]:
            started = change
        elif started is not None:
            failed.append(f"{_stamp(started['at'])} for {_span(change['at'] - started['at'])} ({started['detail']})")
            started = None
    if started is not None:
        failed.append(f"since {_stamp(started['at'])} ({started['detail']})")
    days, kept = history["days"], history.get("kept_since")
    if kept is None:
        since = " (Relay keeps none yet)"
    elif kept > history.get("from", kept):  # (it began within those days)
        since = f" (Relay has kept it since {time.strftime('%a %d %b', time.localtime(kept))})"
    else:
        since = ""
    lines.append(f"Its last {days} days{since}: " + (f"failing {len(failed)} times: " + "; ".join(failed) if failed
                                                    else "no failed health checks") + ".")
    sizes = history["data"]
    if sizes:
        first, last = sizes[0], sizes[-1]
        lines.append(f"Its data: {_size(last['bytes'])} on {last['day']}"
                     + (f"; {_size(first['bytes'])} on {first['day']}" if first is not last else "") + ".")
    return lines


def _span(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    return f"{minutes} min" if minutes < 120 else f"{round(minutes / 60)} h"


def _size(count: int) -> str:
    for unit, at in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if count >= at:
            return f"{count / at:.1f} {unit}"
    return f"{count} B"


def _stamp(moment: float | None) -> str:
    return time.strftime("%a %d %b %H:%M:%S", time.localtime(moment)) if moment else "never"


def describe(service: dict[str, Any], box: str | None = None) -> str:
    """One line for the Governor (service.list); `box`: its live box's state, from podman."""
    live = service.get("live")
    health = ("well" if service["health_ok"] else f"FAILING: {service['health_detail']}") \
        if service["health_ok"] is not None else "not checked yet"
    versions = ", ".join(f"{v['number']} {v['status']}" for v in service["versions"][:6])
    return (f"- {service['name']} (port {service['public_port']}; "
            f"{'version ' + str(live['number']) + ' live' if live else 'nothing live'}; {health}; "
            f"starts with: {service['command']}; health check: {service['health'] or 'a connection'}; "
            f"versions: {versions or 'none'}" + (f"; box: {box}" if box else "") + ")")

