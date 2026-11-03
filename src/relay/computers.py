"""Your own computers: agents in Relay run commands and move files there.

Relay runs on one machine (a home server, say); what you want changed may
be on another: your PC's settings, a program installed there, a file on its
desktop. Such a computer runs a small Python script, downloaded from Relay
(Settings → Computers), that connects to Relay and waits for work. While it
runs, agents can use it; stop it and they can't.

The script calls Relay, not the other way round, so it works from behind a
router or firewall: it asks for work (a long poll, POLL_WAIT seconds), does
it, and sends back the result. Its token, in the script, is its only key.

Agents get it as Relay tools (relay.delegation): `relay computers`, `relay
run --on NAME`, `relay pull` and `relay push`, and chat models as functions
(relay.toolbox). A computer can ask you first for every command and file
change (the default; `ask`), in the chat, and read-only modes don't run
commands or change files there at all.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from relay.db import Database

POLL_WAIT = 25       # seconds a connector's request for work waits
ONLINE_GRACE = 15    # seconds after its last request that it still counts as online
DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 600
MAX_FILE = 20 * 1024 * 1024
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
SCRIPT = Path(__file__).parent / "connector.py"


class ComputerError(Exception):
    """Something agents are told, e.g. that a computer is offline."""


@dataclass
class Job:
    id: str
    computer_id: str
    kind: str  # "run" | "read" | "write"
    args: dict[str, Any]
    result: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future())
    taken: bool = False


class Computers:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.queues: dict[str, list[Job]] = {}
        self.jobs: dict[str, Job] = {}
        self.wakers: dict[str, asyncio.Event] = {}
        self.polling: dict[str, int] = {}  # computer id -> requests for work waiting now

    # The list

    def create(self, user_id: int, name: str) -> dict[str, Any]:
        name = re.sub(r"[^a-z0-9-]+", "-", name.strip().lower()).strip("-")[:40]
        if not NAME.match(name):
            raise ComputerError("Give the computer a name: letters, digits and dashes, e.g. laptop")
        if self.db.one("SELECT 1 FROM computers WHERE user_id = ? AND name = ?", (user_id, name)):
            raise ComputerError(f"There's a computer called {name!r} already")
        computer_id = uuid.uuid4().hex
        self.db.execute("INSERT INTO computers (id, user_id, name, token, ask, created) VALUES (?, ?, ?, ?, 0, ?)",
                        (computer_id, user_id, name, secrets.token_urlsafe(32), time.time()))
        return self.get(user_id, computer_id)

    def get(self, user_id: int, computer_id: str) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM computers WHERE user_id = ? AND id = ?", (user_id, computer_id))
        if row is None:
            raise ComputerError("No such computer")
        return self._public(row)

    def for_user(self, user_id: int) -> list[dict[str, Any]]:
        return [self._public(r) for r in
                self.db.all("SELECT * FROM computers WHERE user_id = ? ORDER BY name", (user_id,))]

    def by_name(self, user_id: int, name: str | None) -> dict[str, Any]:
        mine = self.for_user(user_id)
        if not mine:
            raise ComputerError("The user has no computers connected to Relay (Settings → Computers)")
        if not name:
            if len(mine) == 1:
                return mine[0]
            raise ComputerError(f"Say which computer: {', '.join(c['name'] for c in mine)}")
        found = next((c for c in mine if c["name"] == str(name).strip().lower()), None)
        if found is None:
            raise ComputerError(f"No computer {name!r}; there's {', '.join(c['name'] for c in mine)}")
        return found

    def update(self, user_id: int, computer_id: str, data: dict[str, Any]) -> dict[str, Any]:
        self.get(user_id, computer_id)
        if "ask" in data:
            self.db.execute("UPDATE computers SET ask = ? WHERE id = ?", (1 if data["ask"] else 0, computer_id))
        return self.get(user_id, computer_id)

    def remove(self, user_id: int, computer_id: str) -> None:
        self.get(user_id, computer_id)
        self.db.execute("DELETE FROM computers WHERE id = ?", (computer_id,))
        for job in self.queues.pop(computer_id, []):
            if not job.result.done():
                job.result.set_exception(ComputerError("The computer was removed from Relay"))
        waker = self.wakers.get(computer_id)
        if waker:
            waker.set()

    def token_for(self, user_id: int, computer_id: str) -> str:
        return self.db.one("SELECT token FROM computers WHERE id = ? AND user_id = ?", (computer_id, user_id))["token"]

    def _public(self, row: dict[str, Any]) -> dict[str, Any]:
        seen = row["last_seen"] or 0
        online = self.polling.get(row["id"], 0) > 0 or time.time() - seen < ONLINE_GRACE
        return {"id": row["id"], "name": row["name"], "ask": bool(row["ask"]), "created": row["created"],
                "last_seen": row["last_seen"], "online": online,
                "info": json.loads(row["info"]) if row["info"] else None}

    def script(self, user_id: int, computer_id: str, url: str) -> str:
        """The connector for a computer, with Relay's address and its token in it."""
        computer = self.get(user_id, computer_id)
        return (SCRIPT.read_text().replace("@RELAY_URL@", url.rstrip("/"))
                .replace("@TOKEN@", self.token_for(user_id, computer_id)).replace("@NAME@", computer["name"]))

    # The connector's side

    def _by_token(self, token: str) -> dict[str, Any] | None:
        return self.db.one("SELECT * FROM computers WHERE token = ?", (token,))

    async def poll(self, token: str, info: Any) -> list[dict[str, Any]] | None:
        """Work for the computer, waiting up to POLL_WAIT seconds for some; None: unknown token."""
        row = self._by_token(token)
        if row is None:
            return None
        computer_id = row["id"]
        self.db.execute("UPDATE computers SET last_seen = ?, info = ? WHERE id = ?",
                        (time.time(), json.dumps(info)[:4000] if isinstance(info, dict) else row["info"],
                         computer_id))
        self.polling[computer_id] = self.polling.get(computer_id, 0) + 1
        try:
            waker = self.wakers.setdefault(computer_id, asyncio.Event())
            if not self.queues.get(computer_id):
                waker.clear()
                try:
                    await asyncio.wait_for(waker.wait(), timeout=POLL_WAIT)
                except asyncio.TimeoutError:
                    pass
            jobs = [j for j in self.queues.pop(computer_id, []) if not j.result.done()]
            for job in jobs:
                job.taken = True
            return [{"id": j.id, "kind": j.kind, **j.args} for j in jobs]
        finally:
            self.polling[computer_id] -= 1
            self.db.execute("UPDATE computers SET last_seen = ? WHERE id = ?", (time.time(), computer_id))

    def deliver(self, token: str, job_id: str, result: dict[str, Any]) -> bool:
        row = self._by_token(token)
        job = self.jobs.get(str(job_id))
        if row is None or job is None or job.computer_id != row["id"]:
            return False
        if not job.result.done():
            job.result.set_result(result)
        return True

    # Relay's side

    async def call(self, computer: dict[str, Any], kind: str, args: dict[str, Any], timeout: float) -> dict[str, Any]:
        """Have the computer do a job; its result ({"ok", …}), or ComputerError."""
        if not self._public(self.db.one("SELECT * FROM computers WHERE id = ?", (computer["id"],)))["online"]:
            seen = computer["last_seen"]
            when = f"last seen {time.strftime('%Y-%m-%d %H:%M', time.localtime(seen))}" if seen else "never connected"
            raise ComputerError(f"{computer['name']} is offline ({when}). Ask the user to start Relay's "
                                "connector there: Relay's VSCodium extension (Relay: This computer), or "
                                "relay-computer.py from Settings → Computers.")
        job = Job(uuid.uuid4().hex, computer["id"], kind, args)
        self.jobs[job.id] = job
        self.queues.setdefault(computer["id"], []).append(job)
        self.wakers.setdefault(computer["id"], asyncio.Event()).set()
        try:
            return await asyncio.wait_for(asyncio.shield(job.result), timeout=timeout + POLL_WAIT)
        except asyncio.TimeoutError as exc:
            queue = self.queues.get(computer["id"], [])
            if job in queue:
                queue.remove(job)
            raise ComputerError(f"{computer['name']} didn't answer in time"
                                + ("" if job.taken
                                   else " (it never picked the job up; is the connector still running?)")) from exc
        finally:
            self.jobs.pop(job.id, None)
