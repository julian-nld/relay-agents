"""Work that starts by itself later: a task that goes on when a usage limit resets,
scheduled tasks (relay.schedules), and AgentOS's Governor (relay.agentos): woken when
one of its tasks is due, or for its regular round, never just to look.

A loop in Relay's event loop looks every TICK seconds for what is due and
starts it as an ordinary run, so it shows up, is stored and is notified
like any other.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import urllib.parse
from typing import TYPE_CHECKING

from relay import timed, tunables

if TYPE_CHECKING:
    from relay.app import Relay

TICK = 20  # seconds
TIDY_EVERY = 3600  # seconds between clearing out stale uploads and sign-ins
PROVIDERS_EVERY = 60  # seconds between looks for a provider that keeps failing (relay.repair)
TOLD = "providers_failing"  # meta: {provider: when it was told}, until it works again


IDLE_HOURS = 3  # an agent's container no run used for this long is stopped (started again when needed)

class Scheduler:
    def __init__(self, relay: Relay) -> None:
        self.relay = relay
        self.tidied = time.time()
        self.resumed = False  # /timed agents and the Governor's a restart cut off: gone on with at the first tick
        self.providers_checked = 0.0
        self.vpn_keeping: asyncio.Task | None = None  # (its fallback loops and rotation: a while, at times)
        self.snapshots_checked = 0.0
        self.snapshotting: asyncio.Task | None = None  # (services' daily data copies: a while, with big data)
        self.following: set[asyncio.Task] = set()  # script tasks' runs, until they end (their outcome: _script_outcome)

    async def loop(self) -> None:
        while True:
            await asyncio.sleep(TICK)
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001 - the loop must keep going
                print(f"relay: scheduler: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)

    async def tick(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        await asyncio.to_thread(self._timed_over, now)  # (first: a resume past its MAX doesn't start)
        if not self.resumed:  # (once, after Relay started: Relay serves requests by now)
            self.resumed = True
            await asyncio.to_thread(self.relay.runs.resume_timed)
            await asyncio.to_thread(self.relay.agentos.resume_agents, now)
        await asyncio.to_thread(self._resume, now)
        if now - self.tidied > TIDY_EVERY:
            self.tidied = now
            await asyncio.to_thread(self._tidy)
            await self._temporary_gone(now)
        governed: dict[int, list[dict]] = {}
        for schedule in self.relay.schedules.due(now):
            if (schedule.get("system") == "models-rank" and await self._ranked_already(schedule, now)
                    or schedule.get("system") == "memory-tidy" and await self._tidied_already(schedule, now)):
                continue
            if schedule.get("governed"):
                governed.setdefault(schedule["user_id"], []).append(schedule)
            else:
                await self.run_schedule(schedule, now=now)
        await self.wake_governors(governed, now)
        self.relay.agentos.watch(now)  # (its agents at work: one that seems to go wrong)
        await self.relay.agentos.wake_for_agents(now)  # (and those done: so it needn't wait for them)
        for service in await self.relay.production.check(now):
            await self.service_failing(service, now)
        if now - self.providers_checked >= PROVIDERS_EVERY:
            self.providers_checked = now
            await self.providers_failing(now)
        self.relay.plugins.tick(now)  # (their background tasks that are due, each in a thread)
        if now - self.snapshots_checked >= TIDY_EVERY and (self.snapshotting is None or self.snapshotting.done()):
            self.snapshots_checked = now
            if await asyncio.to_thread(self.relay.production.daily_due, now):
                self.snapshotting = asyncio.create_task(self._snapshot_services(now))  # (beside the rest)
        await asyncio.to_thread(self.relay.search.tick, now)  # (unused a while: SearXNG stops,
        await asyncio.to_thread(self.relay.fetch.tick, now)  # and the fetch box)
        if self.relay.vpn.configured() and (self.vpn_keeping is None or self.vpn_keeping.done()):
            # (beside the rest: connecting another exit takes up to a minute)
            self.vpn_keeping = asyncio.create_task(self._keep_vpn(now))

    async def _snapshot_services(self, now: float) -> None:
        try:
            await self.relay.production.daily_snapshots(now)
        except Exception as exc:  # noqa: BLE001 - its own task: said here, or nowhere
            print(f"relay: services' daily snapshots: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)

    async def _keep_vpn(self, now: float) -> None:
        try:
            await asyncio.to_thread(self.relay.vpn.tick, now)
        except Exception as exc:  # noqa: BLE001 - its own task: said here, or nowhere
            print(f"relay: VPN: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)

    async def providers_failing(self, now: float) -> None:
        """A provider whose last runs all failed (relay.repair: failing_providers): its user's Governor
        is woken to look, and repair Relay's support for it if that's what broke; without AgentOS the
        user is told, with the way to repair it. A lost login (LOGIN_LOST) is the user's to sign in again:
        they're told that, either way. Once, until it works again."""
        from relay.agentos import AgentOSError
        from relay.providers import KINDS, has_shell
        from relay.repair import IN_A_ROW, LOGIN_LOST, failing_providers
        db, agentos = self.relay.db, self.relay.agentos
        failing = await asyncio.to_thread(failing_providers, db)
        row = db.one("SELECT value FROM meta WHERE key = ?", (TOLD,))
        told = {k: v for k, v in (json.loads(row["value"]) if row else {}).items() if k in failing}  # (works again)
        for provider_id, errors in failing.items():
            if provider_id in told:
                continue
            last = db.one("SELECT c.user_id FROM runs r JOIN conversations c ON c.id = r.conversation_id "
                          "WHERE r.provider_id = ? ORDER BY r.created DESC LIMIT 1", (provider_id,))
            if last is None:
                continue
            try:
                template = self.relay.registry.get(provider_id)
            except Exception:  # noqa: BLE001 - removed meanwhile: nothing to repair
                continue
            name, user, said = template.name, last["user_id"], "; ".join(e[:300] for e in errors)
            link = f"/providers/repair/new?provider={urllib.parse.quote(provider_id)}"
            sign_in = getattr(KINDS.get(template.kind), "SIGN_IN", None)
            if all(LOGIN_LOST.search(e) for e in errors):  # (the user's to sign in again: no Governor, no repair)
                if has_shell(template):  # (its page has the shell for it)
                    how = "Sign it in again in its Shell (Settings → Providers, or the page this opens)" + (
                        f": `{sign_in}`." if sign_in else ".")
                else:
                    how = "Sign it in again on this machine: it isn't Relay's support for it that broke."
                agentos.intervene(user, f"{name} isn't signed in any more ({errors[0][:300]}). {how}", link=link)
            elif agentos.on(user):
                why = (f"the provider {name} ({provider_id}) failed its last {IN_A_ROW} runs ({said}). If Relay's "
                       f"support layer for it broke (the program behind it changed its output or flags), have it "
                       f"repaired: provider.repair provider={provider_id}")
                try:
                    if await agentos.wake(user, [], now, why) is None:
                        continue  # it's busy: the next look tries again
                except AgentOSError as exc:
                    agentos.intervene(user, f"{name} failed its last {IN_A_ROW} runs ({said}), and the Governor "
                                      f"couldn't be woken: {exc}")
            else:
                agentos.intervene(user, f"{name} failed its last {IN_A_ROW} runs ({said}). If the program behind it "
                                  "changed, another agent can repair Relay's support for it: open its repair page.",
                                  link=link)
            told[provider_id] = now
        db.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                   (TOLD, json.dumps(told)))

    async def service_failing(self, service: dict, now: float) -> None:
        """A live service fails its health check: its user's Governor is woken to see to it, when
        AgentOS looks after it; else the user is told. Once, until it's well again."""
        from relay.agentos import AgentOSError
        agentos, production = self.relay.agentos, self.relay.production
        user, why = service["user_id"], f"the service {service['name']} is failing ({service['health_detail']})"
        told = f"{why[0].upper()}{why[1:]}"
        if service["managed"] and agentos.on(user):
            try:
                if await agentos.wake(user, [], now, why) is None:
                    return  # it's busy: the next check tries again
            except AgentOSError as exc:
                agentos.intervene(user, f"{told}, and the Governor couldn't be woken: {exc}", service_id=service["id"])
        else:
            agentos.intervene(user, f"{told}.", service_id=service["id"])
        production.alerted(service, now)

    async def _ranked_already(self, task: dict, now: float) -> bool:
        """The weekly model ranking with no new model to look up: done by Relay, without waking the
        Governor (relay.ranking). With AgentOS off it goes as any task does."""
        from relay import ranking
        if not self.relay.agentos.on(task["user_id"]):
            return False
        db = self.relay.db
        models = await asyncio.to_thread(ranking.available, self.relay.registry, ranking.listed(db)[1])
        if not models or ranking.unscored(db, models):
            return False
        ranking.note_listed(db, models, now)  # (models gone since drop out of it)
        self.relay.schedules.ran(task, None, None, now)
        db.execute("UPDATE schedules SET outcome = 'done', outcome_by = 'relay', "
                   "outcome_note = ?, outcome_at = ? WHERE id = ?",
                   (f"No new models: the ranking of {ranking.ranking(db)['of']} stands.", now, task["id"]))
        return True

    async def _tidied_already(self, task: dict, now: float) -> bool:
        """The weekly memory tidy-up with nothing to look at: no memory added or changed since it last ran,
        and none of an agent's whose place is gone or that no agent got for long (relay.memory.review): done
        by Relay, without waking the Governor. With AgentOS off it goes as any task does."""
        from relay import memory, tunables
        user = task["user_id"]
        if not self.relay.agentos.on(user):
            return False
        db = self.relay.db
        if task.get("last_run") is None or db.one("SELECT 1 FROM memories WHERE user_id = ? AND updated > ?",
                                                  (user, task["last_run"])):
            return False
        found = await asyncio.to_thread(memory.review, db, user, now, tunables.get("memory.idle_days"))
        if any(m["by"] != "you" for m in found["gone"] + found["idle"]):
            return False
        self.relay.schedules.ran(task, None, None, now)
        db.execute("UPDATE schedules SET outcome = 'done', outcome_by = 'relay', outcome_note = ?, outcome_at = ? "
                   "WHERE id = ?", ("Nothing new in the memory since the last look, and nothing of an agent's gone "
                                    "stale.", now, task["id"]))
        return True

    async def wake_governors(self, governed: dict[int, list[dict]], now: float) -> None:
        """Wake each user's Governor that has something to do: due tasks, or its round."""
        from relay.agentos import AgentOSError
        agentos = self.relay.agentos
        for user in set(governed) | set(agentos.enabled_users()):
            due = governed.get(user, [])
            settings = agentos.settings(user)
            if due and not agentos.on(user, settings):
                for task in due:  # AgentOS is off: noted with the task, which waits for its next time
                    self.relay.schedules.ran(task, None, "AgentOS is off, so nobody carried it out", now)
                continue
            if not due and not agentos.due_for_round(user, now):
                continue
            reason = (f"{len(due)} task{'s are' if len(due) > 1 else ' is'} due" if due
                      else f"your regular round (every {settings['round_hours']} hours)")
            try:
                await agentos.wake(user, due, now, reason)
            except AgentOSError as exc:
                for task in due:
                    self.relay.schedules.ran(task, None, str(exc), now)
                if not settings.get("last_wake") or now - settings["last_wake"] >= settings["round_hours"] * 3600:
                    agentos.intervene(user, f"The Governor couldn't be woken: {exc}")
                    agentos.woken(user, now)  # not again before the next round

    async def run_schedule(self, schedule: dict, move_on: bool = True, now: float | None = None) -> str | None:
        """Send a scheduled task's prompt: in a new chat, or on in its own one
        (queued if that one is busy). Returns the conversation."""
        from relay.routes.conversations import open_conversation  # it imports this module
        from relay.web import ApiError

        relay, now = self.relay, time.time() if now is None else now
        if schedule.get("script"):
            return await self._run_script(schedule, move_on, now)
        conversation_id, error = None, None
        try:
            row = None
            if schedule["target"] == "same":
                row = relay.db.one("SELECT * FROM conversations WHERE id = ? AND user_id = ?",
                                   (schedule["conversation_id"], schedule["user_id"]))
                if row is None:
                    raise ApiError("Its chat was deleted")
                row = self._take_settings(row, schedule)
            else:
                stamp = time.strftime("%a %d %b %H:%M", time.localtime(now))
                row = await open_conversation(relay, schedule["user_id"], {
                    "provider_id": schedule["provider_id"], "model": schedule["model"], "mode": schedule["mode"],
                    "directory": schedule["directory"], "isolate": schedule["isolate"],
                    "agent_mode": schedule["agent_mode"], "effort": schedule["effort"],
                    "title": f"{schedule['name']} · {stamp}"})
            conversation_id = row["id"]
            if relay.runs.active_run_for(row["id"]):
                relay.db.execute("INSERT INTO queued_messages (conversation_id, content, created) VALUES (?, ?, ?)",
                                 (row["id"], schedule["prompt"], now))
            else:
                await asyncio.to_thread(relay.runs.start, row, schedule["prompt"])
            # How it went, once its conversation is quiet (a fallback agent's go-on included): its outcome
            relay.db.execute("UPDATE schedules SET outcome = NULL, outcome_note = NULL WHERE id = ?", (schedule["id"],))
            self.following.add(asyncio.create_task(self._turn_outcome(schedule["id"], row["id"])))
        except Exception as exc:  # noqa: BLE001 - noted with the schedule, shown in its list
            error = str(exc) or type(exc).__name__
            print(f"relay: scheduled task {schedule['name']!r}: {error}", file=sys.stderr, flush=True)
        if move_on:
            relay.schedules.ran(schedule, conversation_id, error, now)
        else:
            relay.db.execute("UPDATE schedules SET last_run = ?, last_conversation = COALESCE(?, last_conversation), "
                             "last_error = ? WHERE id = ?", (now, conversation_id, error, schedule["id"]))
        return conversation_id

    async def _run_script(self, schedule: dict, move_on: bool, now: float) -> str | None:
        """A task that runs one of the user's scripts (relay.scripts), as the user, without an agent; how it ended
        becomes the task's outcome (Relay saw it), its output's last lines the note. Its run's id; None when it
        didn't start (why: the task's last_error)."""
        relay, run_id, error = self.relay, None, None
        try:
            run_id = await asyncio.to_thread(relay.scripts.start, schedule["user_id"], schedule["script"],
                                             schedule.get("script_args") or "")
            relay.db.execute("UPDATE schedules SET last_script_run = ? WHERE id = ?", (run_id, schedule["id"]))
            self.following.add(asyncio.create_task(self._script_outcome(schedule["id"], run_id)))
        except Exception as exc:  # noqa: BLE001 - noted with the schedule, shown in its list
            error = str(exc) or type(exc).__name__
            print(f"relay: scheduled script {schedule['script']!r}: {error}", file=sys.stderr, flush=True)
        if move_on:
            relay.schedules.ran(schedule, None, error, now)
        else:
            relay.db.execute("UPDATE schedules SET last_run = ?, last_error = ? WHERE id = ?",
                             (now, error, schedule["id"]))
        return run_id

    async def _turn_outcome(self, schedule_id: str, conversation_id: str) -> None:
        """A prompt task's outcome, as Relay saw it: how its conversation's last answer ended, once nothing runs or
        waits there (twice in a row: a go-on starts right after its run). Found by a simulated user: a task whose
        agent failed said "done"."""
        quiet = 0
        while quiet < 2:
            await asyncio.sleep(2)
            busy = self.relay.runs.active_run_for(conversation_id) or self.relay.db.one(
                "SELECT 1 FROM queued_messages WHERE conversation_id = ? LIMIT 1", (conversation_id,))
            quiet = 0 if busy else quiet + 1
        last = self.relay.db.one("SELECT status, error FROM runs WHERE conversation_id = ? ORDER BY created DESC "
                                 "LIMIT 1", (conversation_id,))
        if last is not None:
            outcome = {"succeeded": "done", "interrupted": "interrupted"}.get(last["status"], "failed")
            note = None if outcome == "done" else (last["error"] or last["status"])
            self.relay.db.execute(
                "UPDATE schedules SET outcome = ?, outcome_by = 'relay', outcome_note = ?, outcome_at = ?, "
                "last_error = ? WHERE id = ? AND last_conversation = ?",
                (outcome, note, time.time(), note if outcome == "failed" else None, schedule_id, conversation_id))
        self.following.difference_update({t for t in self.following if t.done()})

    async def _script_outcome(self, schedule_id: str, run_id: str) -> None:
        while True:
            found = self.relay.scripts.run(run_id)
            if found is None or found["status"] != "running":
                break
            await asyncio.sleep(2)
        if found is not None:
            took = _took((found["finished"] or time.time()) - found["started"])
            if found["status"] == "succeeded":  # (a good run's end is no note: a simulated user saw "└────┘")
                outcome, said = "done", f"took {took}"
            elif found["status"] == "stopped":  # (someone stopped it, as a chat's Stop)
                outcome, said = "interrupted", f"stopped after {took}"
            else:  # (its output's last line says why; read beside "Failed:", not "failed (exit 1)")
                tail = (found["output"].strip().splitlines() or [""])[-1][-300:]
                why = f"exit {found['exit_code']}" if found["exit_code"] not in (None, 0) else found["status"]
                outcome, said = "failed", f"{why} after {took}" + (f": {tail}" if tail else "")
            self.relay.db.execute(
                "UPDATE schedules SET outcome = ?, outcome_by = 'relay', outcome_note = ?, outcome_at = ?, "
                "last_error = ? WHERE id = ? AND last_script_run = ?",
                (outcome, said, time.time(), said if outcome == "failed" else None, schedule_id, run_id))
        self.following.difference_update({t for t in self.following if t.done()})

    def _take_settings(self, row: dict, schedule: dict) -> dict:
        """A schedule going on in its chat runs as it's set (e.g. read-only Plan), not as the chat
        happens to be now: the chat takes its provider, model, mode and effort, as when the user
        picks them (another provider gets the conversation handed over)."""
        changes = {"agent_mode": schedule.get("agent_mode") or None, "effort": schedule.get("effort") or None}
        if schedule.get("provider_id") and schedule["provider_id"] != row["provider_id"]:
            changes.update(provider_id=schedule["provider_id"], model=schedule.get("model") or None,
                           provider_session=None, fork_session=None, fork_before=None)
        elif schedule.get("provider_id"):
            changes["model"] = schedule.get("model") or None
        if all(row.get(k) == v for k, v in changes.items()):
            return row
        self.relay.db.execute(f"UPDATE conversations SET {', '.join(f'{k} = ?' for k in changes)} WHERE id = ?",
                              (*changes.values(), row["id"]))
        return self.relay.db.one("SELECT * FROM conversations WHERE id = ?", (row["id"],))

    def _tidy(self) -> None:
        """What start-up clears, also while Relay keeps running for weeks."""
        self.relay.uploads.purge_orphans()
        self.relay.accounts.purge_expired()
        self.relay.agentos.tidy_events(time.time())
        try:
            self.relay.production.tidy(time.time())  # archived versions past their days
        except Exception as exc:  # noqa: BLE001
            print(f"relay: couldn't tidy archived versions: {exc}", file=sys.stderr, flush=True)
        try:
            self.relay.containers.stop_idle(IDLE_HOURS)
        except Exception as exc:  # noqa: BLE001 - podman gone meanwhile: nothing to stop
            print(f"relay: couldn't stop idle containers: {exc}", file=sys.stderr, flush=True)

    async def _temporary_gone(self, now: float) -> None:
        """Temporary chats left behind (a tab closed in them, a script stopped while it judged): gone once unused
        for a while, as a DELETE would take them (their agent stopped first)."""
        from relay.routes.conversations import remove_conversation
        idle = tunables.get("chat.temporary_hours") * 3600
        for row in self.relay.db.all("SELECT * FROM conversations WHERE temporary = 1 AND updated < ?", (now - idle,)):
            if self.relay.runs.active_run_for(row["id"]):
                continue  # (still answering: next time)
            try:
                await remove_conversation(self.relay, row)
            except Exception as exc:  # noqa: BLE001 - another time, then
                print(f"relay: couldn't remove a temporary chat: {exc}", file=sys.stderr, flush=True)

    def _timed_over(self, now: float) -> None:
        """/timed requests whose MAX has passed (relay.timed): the agent is stopped, its fallback loop
        with it (nothing goes on, a wait for a usage limit's reset neither)."""
        db, runs = self.relay.db, self.relay.runs
        for row in db.all("SELECT * FROM conversations WHERE timed IS NOT NULL AND json_extract(timed, "
                          "'$.ended') IS NULL AND json_extract(timed, '$.max') <= ?", (now,)):
            db.execute("UPDATE conversations SET timed = ?, resume_at = NULL WHERE id = ?",
                       (json.dumps({**json.loads(row["timed"]), "ended": "max"}), row["id"]))
            run_id = runs.active_run_for(row["id"])
            if run_id:
                runs.stop(run_id, "its time (/timed) was up")

    def _resume(self, now: float) -> None:
        """Conversations whose usage limit has reset: tell the agent to go on."""
        db, runs = self.relay.db, self.relay.runs
        for row in db.all("SELECT * FROM conversations WHERE resume_at IS NOT NULL AND resume_at <= ?", (now,)):
            db.execute("UPDATE conversations SET resume_at = NULL WHERE id = ?", (row["id"],))
            if runs.active_run_for(row["id"]) or row["task_state"] == "discarded" or timed.time_up(row.get("timed")):
                continue  # someone went on already, or a timed request's time is up
            try:
                row = runs.restart_loop(row)  # a fallback loop's next round starts at its first agent
                info = json.loads(row["limit_info"]) if row["limit_info"] else {}
                action = {"kind": "resume",
                          "from": {"provider_id": info.get("provider_id"), "model": info.get("model")}}
                runs.start(row, tunables.get("limits.resume_prompt"), relay_action=action)
            except Exception as exc:  # noqa: BLE001 - e.g. the provider was removed
                print(f"relay: could not resume {row['id']}: {exc}", file=sys.stderr, flush=True)


def _took(seconds: float) -> str:
    seconds = round(seconds)
    return f"{seconds} s" if seconds < 120 else f"{round(seconds / 60)} min"
