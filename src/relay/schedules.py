"""Scheduled tasks: a prompt that Relay sends by itself, once or again and again.

"Every weekday at 8:00, summarise what changed in the repository", "every
two hours, check the build", "tomorrow at 9, remind me …". Each time is an
ordinary run: in a new chat (or code task) of its own, or on in the same one,
with the provider, model and mode chosen; notifications work as always.

When (`when`):
    {"kind": "once", "at": <unix time>}
    {"kind": "daily", "time": "08:00"}
    {"kind": "weekdays", "time": "08:00"}                 Monday to Friday
    {"kind": "weekly", "days": [0, 3], "time": "08:00"}   0 is Monday
    {"kind": "interval", "minutes": 120}
Times are in the schedule's time zone (the browser's, when it was made).
A time missed while Relay was off runs once when it's back, not for every miss.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import time
import uuid
from typing import Any

from relay.db import Database
from relay.scripts import NAME

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]
    ZoneInfoNotFoundError = Exception  # type: ignore[assignment,misc]

KINDS = ("once", "daily", "weekdays", "weekly", "interval")
MIN_INTERVAL = 5  # minutes
MAX_INTERVAL = 366 * 24 * 60  # minutes: a year (longer is a once; a next run past year 9999 broke task.list)
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


class ScheduleError(Exception):
    pass


def _zone(name: str | None):
    if name and ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return dt.datetime.now().astimezone().tzinfo


def _known(name: str) -> bool:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def validate_when(when: Any) -> dict[str, Any]:
    if not isinstance(when, dict) or when.get("kind") not in KINDS:
        raise ScheduleError(f"When is one of: {', '.join(KINDS)}")
    kind = when["kind"]
    if kind == "once":
        try:
            at = float(when["at"])
            dt.datetime.fromtimestamp(at)  # (a moment a date can say: not NaN, nor past year 9999)
        except (KeyError, TypeError, ValueError, OverflowError, OSError) as exc:
            raise ScheduleError("Say when: a date and time") from exc
        return {"kind": kind, "at": at}
    if kind == "interval":
        try:
            minutes = int(when["minutes"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ScheduleError("Say how many minutes apart") from exc
        if minutes < MIN_INTERVAL:
            raise ScheduleError(f"At least {MIN_INTERVAL} minutes apart")
        if minutes > MAX_INTERVAL:
            raise ScheduleError("At most a year apart (a task further off is a once)")
        return {"kind": kind, "minutes": minutes}
    clock = str(when.get("time") or "")
    if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", clock):
        raise ScheduleError("A time like 08:00")
    found = {"kind": kind, "time": clock}
    if kind == "weekly":
        days = sorted({int(d) for d in when.get("days") or [] if str(d).isdigit() and 0 <= int(d) <= 6})
        if not days:
            raise ScheduleError("Pick at least one day")
        found["days"] = days
    return found


DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
UNITS = {"minute": 1, "minutes": 1, "min": 1, "m": 1, "hour": 60, "hours": 60, "h": 60, "day": 1440, "days": 1440,
         "d": 1440, "week": 10080, "weeks": 10080}


def parse_when(text: Any, zone: str | None = None, now: float | None = None) -> dict[str, Any]:
    """`when` from a few words (for agents: AgentOS's task.create): "every 72 hours", "daily 08:00",
    "weekdays 09:00", "weekly mon,wed 08:00", "once 2026-10-05 09:00" (in `zone`, else the server's),
    "once 2026-10-05T07:00Z" (an ISO time with its own zone), "in 10 minutes"; a dict as it is."""
    if isinstance(text, dict):
        return validate_when(text)
    found = re.fullmatch(r"(?:once\s+)?in\s+(\d+)\s*([a-z]+)", str(text or "").strip().lower())
    if found and found.group(2) in UNITS:
        later = (time.time() if now is None else now) + int(found.group(1)) * UNITS[found.group(2)] * 60
        return validate_when({"kind": "once", "at": later})
    iso = re.fullmatch(r"(?:once\s+)?(\d{4}-\d\d-\d\d[T ]\d\d:\d\d(?::\d\d(?:\.\d+)?)?\s*(?:Z|[+-]\d\d:?\d\d))",
                       str(text or "").strip(), re.IGNORECASE)
    if iso:
        try:
            moment = dt.datetime.fromisoformat(re.sub(r"\s+", "", iso.group(1).upper().replace(" ", "T", 1)))
        except ValueError as exc:
            raise ScheduleError(f"Can't read the time {iso.group(1)!r}") from exc
        return validate_when({"kind": "once", "at": moment.timestamp()})
    words = str(text or "").strip().lower().replace(",", " ").split()
    if not words:
        raise ScheduleError("Say when, e.g. every 72 hours, daily 08:00, weekly mon,wed 08:00, once 2026-10-05 09:00")
    kind, rest = words[0], words[1:]
    if kind == "every" and rest:
        amount, unit = (rest[0], rest[1] if len(rest) > 1 else "")
        found = re.fullmatch(r"(\d+)([a-z]*)", amount)
        if found and (found.group(2) or unit) in UNITS:
            return validate_when({"kind": "interval", "minutes": int(found.group(1)) * UNITS[found.group(2) or unit]})
    elif kind in ("daily", "weekdays") and len(rest) == 1:
        return validate_when({"kind": kind, "time": rest[0]})
    elif kind == "weekly" and len(rest) >= 2:
        days = [DAY_NAMES.index(d[:3]) for d in rest[:-1] if d[:3] in DAY_NAMES]
        return validate_when({"kind": "weekly", "days": days, "time": rest[-1]})
    elif kind == "once" and len(rest) == 2:
        try:
            moment = dt.datetime.strptime(" ".join(rest), "%Y-%m-%d %H:%M").replace(tzinfo=_zone(zone))
        except ValueError as exc:
            raise ScheduleError("once takes a date and time: once 2026-10-05 09:00") from exc
        return validate_when({"kind": "once", "at": moment.timestamp()})
    raise ScheduleError(f"Can't read when {text!r}: e.g. every 72 hours, daily 08:00, weekdays 09:00, "
                        "weekly mon,wed 08:00, once 2026-10-05 09:00, once 2026-10-05T07:00Z, in 10 minutes")


def next_run(when: dict[str, Any], zone: str | None, after: float, since: float | None = None) -> float | None:
    """The first time after `after` the schedule is due; None when it's done. `since`: when it was due last, for an
    interval to count from (it ran a little later, up to a tick: counted from then, "every hour" drifted)."""
    kind = when["kind"]
    if kind == "once":
        return when["at"] if when["at"] > after else None
    if kind == "interval":
        step = when["minutes"] * 60
        if since and since <= after:  # (the next one on its own beat; any it missed meanwhile, skipped)
            return since + (int((after - since) // step) + 1) * step
        return after + step
    tz = _zone(zone)
    hour, minute = map(int, when["time"].split(":"))
    start = dt.datetime.fromtimestamp(after, tz)
    for offset in range(8):
        day = (start + dt.timedelta(days=offset)).date()
        # Build the wall-clock time in the zone itself, so daylight saving is right.
        moment = dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
        if moment.timestamp() <= after:
            continue
        weekday = moment.weekday()
        if kind == "weekdays" and weekday >= 5:
            continue
        if kind == "weekly" and weekday not in when["days"]:
            continue
        return moment.timestamp()
    return None


def describe(when: dict[str, Any]) -> str:
    kind = when["kind"]
    if kind == "once":
        return "once"
    if kind == "interval":
        minutes = when["minutes"]
        return f"every {minutes // 60} h" + (f" {minutes % 60} min" if minutes % 60 else "") if minutes >= 60 \
            else f"every {minutes} min"
    if kind == "daily":
        return f"every day at {when['time']}"
    if kind == "weekdays":
        return f"weekdays at {when['time']}"
    return f"{', '.join(DAYS[d] for d in when['days'])} at {when['time']}"


# Relay's own AgentOS tasks (system tasks): added when AgentOS is on, carried out by the Governor, changed
# only by the user (who can reset one to this), never deleted (turned off instead).
SYSTEM_TASKS = {
    "services-check": {
        "name": "Services: weekly check",
        "when": {"kind": "weekly", "days": [0], "time": "09:00"},
        "prompt": "Look over every live service (service.list, then service.status and service.logs for each): "
                  "errors or warnings in its log since last week, failed health checks, restarts or a box killed "
                  "for memory, data growing fast, and WorkBoxes (clones) left over from earlier work. Fix small, "
                  "clear problems the usual way (workbox.clone, an agent fixes and tests it, production.promote); "
                  "remove clones whose work is done or was promoted (workbox.delete); send an intervention for what "
                  "needs the user's decision. Report per service what you found and did; when all is well, say so "
                  "in a line.",
    },
    "tidy-up": {
        "name": "Tidy up: weekly",
        "when": {"kind": "weekly", "days": [6], "time": "20:00"},
        "prompt": "Tidy AgentOS up. Delete managed chats and code tasks that are finished and no longer needed "
                  "(chat.delete; a service's clone with workbox.delete). Settle interventions that are fixed or no "
                  "longer apply (notify.resolve, saying why). Delete tasks of your own that ran out or aren't "
                  "needed any more (task.delete). Keep whatever is still in use, being worked on, or waiting for the "
                  "user. Report what you removed and why.",
    },
    "memory-tidy": {
        "name": "Memory: weekly tidy-up",
        "when": {"kind": "weekly", "days": [6], "time": "19:00"},
        "prompt": "Tidy Relay's memory up: memory.review lists all of it, with what Relay found. Merge memories that "
                  "say the same (in other words too) into one: the clearest, in the right category (memory.change "
                  "it), the others forgotten (memory.forget, saying why). When two disagree, the newer usually holds: "
                  "forget the other. Forget memories whose place is gone, and those no agent got for long that no "
                  "longer hold (a project that's done, a fact that changed); keep what still holds, however old (the "
                  "user's wishes rarely go stale). The user's own you don't change: name those that seem the same as "
                  "another, or stale, in your report, for the user to decide. Report in a few lines what you merged "
                  "and forgot, and why; when it's all in order, say so in a line.",
    },
    "models-rank": {
        "name": "Models: weekly ranking",
        "when": {"kind": "weekly", "days": [0], "time": "07:00"},
        "prompt": "Keep AgentOS's model ranking up to date: the models available here, from the smartest (#1) down. "
                  "Have an agent on your own model look the new ones up (one, or a few for many, by group), with the "
                  "rules below; check what it found, then save it with models.score. "
                  "models.list shows every provider's models, the score Relay keeps for each and which are not "
                  "looked up yet. Look up only those (the scores kept stay; don't search them again): find each "
                  "model's score on the Artificial Analysis Intelligence Index (artificialanalysis.ai; its models "
                  "leaderboard has most on one page), the index the kept scores are on. A provider's model id is "
                  "often an alias or a route (opus, openai/…, a vendor's prefix): work out which model it is. One "
                  "that's already kept under another provider's id needs only its name. A model the leaderboard page "
                  "doesn't show may still be on the index: search for it by name. Its name is exactly the index's: "
                  "nothing added (a tier, a setting or an alias goes in note), so that one model reached in several "
                  "ways is one name. For one the index doesn't have, "
                  "place it on the index's scale from the benchmarks it is on (estimated: an older or smaller model "
                  "still has scores somewhere); only when no benchmark knows it at all, give its name without a "
                  "score. Save them with models.score (Relay ranks them), and report in a few lines what was new and "
                  "where it ranks.",
    },
}

FIELDS = ("name", "prompt", "provider_id", "model", "agent_mode", "effort", "mode", "directory", "isolate",
          "target", "conversation_id", "timezone", "enabled", "governed", "managed", "script", "script_args")


class Schedules:
    def __init__(self, db: Database) -> None:
        self.db = db

    def all(self, user_id: int) -> list[dict[str, Any]]:
        return [self._public(r) for r in
                self.db.all("SELECT * FROM schedules WHERE user_id = ? ORDER BY name COLLATE NOCASE", (user_id,))]

    def get(self, user_id: int, schedule_id: str) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM schedules WHERE user_id = ? AND id = ?", (user_id, schedule_id))
        if row is None:
            raise ScheduleError("No such scheduled task")
        return self._public(row)

    def save(self, user_id: int, data: dict[str, Any], schedule_id: str | None = None,
             now: float | None = None) -> dict[str, Any]:
        """Add a scheduled task, or change one (fields left out stay as they are)."""
        now = time.time() if now is None else now
        current = self.get(user_id, schedule_id) if schedule_id else {}
        merged = {**current, **{k: v for k, v in data.items() if k in FIELDS or k == "when"}}
        name = " ".join(str(merged.get("name") or "").split())[:80]
        prompt = str(merged.get("prompt") or "").strip()
        # A task that runs a script (relay.scripts) instead: no agent, no prompt needed
        script = str(merged.get("script") or "").strip() or None
        if script and not NAME.fullmatch(script):
            raise ScheduleError("script: one of your scripts, as private/name.py or public/name.py")
        if not name:
            raise ScheduleError("Give the task a name")
        if not prompt and not script:
            raise ScheduleError("Say what the agent should do (the prompt), or which script runs (script)")
        if merged.get("mode", "chat") not in ("chat", "code"):
            raise ScheduleError("mode is chat or code")
        if merged.get("target", "new") not in ("new", "same"):
            raise ScheduleError("target is new or same")
        governed = bool(merged.get("governed")) and not script  # (a script runs by itself)
        if merged.get("mode") == "code" and merged.get("target", "new") == "new" and not merged.get("directory") \
                and not governed:
            raise ScheduleError("A code task needs a project folder")
        if merged.get("target") == "same" and not merged.get("conversation_id"):
            raise ScheduleError("Going on in the same chat needs that chat")
        zone = data.get("timezone") or None  # (as it's given now: one saved before stays, as a task turned off)
        if zone and ZoneInfo is not None and not _known(str(zone)):  # (an unknown one ran in the server's, unsaid)
            raise ScheduleError(f"No time zone {zone!r}: e.g. Europe/Amsterdam")
        when = parse_when(merged.get("when"), merged.get("timezone"), now)  # (in words too: "daily 08:00")
        enabled = bool(merged.get("enabled", True))
        upcoming = next_run(when, merged.get("timezone"), now) if enabled else None
        if (current and enabled and current.get("enabled") and current.get("when") == when
                and (current.get("timezone") or None) == (merged.get("timezone") or None)
                and (current.get("next_run") or 0) > now):
            upcoming = current["next_run"]  # renamed, say: when it runs stays as it was
        if enabled and upcoming is None:
            raise ScheduleError("That time has passed")
        values = {
            "name": name, "prompt": prompt, "provider_id": str(merged.get("provider_id") or ""),
            "model": merged.get("model") or None, "agent_mode": merged.get("agent_mode") or None,
            "effort": merged.get("effort") or None, "mode": merged.get("mode") or "chat",
            "directory": merged.get("directory") or None, "isolate": 1 if merged.get("isolate", False) else 0,
            "target": merged.get("target") or "new", "conversation_id": merged.get("conversation_id") or None,
            "timezone": merged.get("timezone") or None, "enabled": 1 if enabled else 0,
            "when_spec": json.dumps(when), "next_run": upcoming, "governed": 1 if governed else 0,
            "managed": 1 if merged.get("managed") else 0,
            "script": script, "script_args": (str(merged.get("script_args") or "").strip() or None) if script else None,
        }
        if governed:
            values["target"] = "new"  # the Governor chooses who does it, and where it goes on
        elif not values["provider_id"] and not script:
            raise ScheduleError("Choose a provider")
        if schedule_id:
            assignments = ", ".join(f"{k} = ?" for k in values)
            self.db.execute(f"UPDATE schedules SET {assignments} WHERE id = ? AND user_id = ?",
                            (*values.values(), schedule_id, user_id))
        else:
            schedule_id = uuid.uuid4().hex
            columns = ", ".join(["id", "user_id", "created", *values])
            marks = ", ".join("?" * (len(values) + 3))
            self.db.execute(f"INSERT INTO schedules ({columns}) VALUES ({marks})",
                            (schedule_id, user_id, now, *values.values()))
        return self.get(user_id, schedule_id)

    def ensure_system(self, user_id: int) -> list[str]:
        """Relay's own tasks (SYSTEM_TASKS) this user doesn't have yet, added; their keys."""
        have = {r["system"]
                for r in self.db.all("SELECT system FROM schedules WHERE user_id = ? AND system IS NOT NULL",
                                                 (user_id,))}
        added = []
        for key, task in SYSTEM_TASKS.items():
            if key not in have:
                made = self.save(user_id, {**task, "governed": True, "enabled": True})
                self.db.execute("UPDATE schedules SET system = ? WHERE id = ?", (key, made["id"]))
                added.append(key)
        return added

    def reset(self, user_id: int, schedule_id: str) -> dict[str, Any]:
        """A system task as Relay ships it again: its name, prompt and schedule, on, carried out by the Governor."""
        task = self.get(user_id, schedule_id)
        if not task.get("system") or task["system"] not in SYSTEM_TASKS:
            raise ScheduleError("Only Relay's own tasks have a default to go back to")
        default = SYSTEM_TASKS[task["system"]]
        return self.save(user_id, {**default, "governed": True, "enabled": True, "mode": "chat", "directory": None,
                                   "timezone": None}, schedule_id)

    def delete(self, user_id: int, schedule_id: str) -> None:
        if self.get(user_id, schedule_id).get("system"):
            raise ScheduleError("This is one of Relay's own tasks: turn it off instead")
        self.db.execute("DELETE FROM schedules WHERE id = ? AND user_id = ?", (schedule_id, user_id))

    def due(self, now: float) -> list[dict[str, Any]]:
        return [self._public(r) for r in self.db.all(
            "SELECT * FROM schedules WHERE enabled = 1 AND next_run IS NOT NULL AND next_run <= ?", (now,))]

    def ran(self, schedule: dict[str, Any], conversation_id: str | None, error: str | None, now: float) -> None:
        """After a turn: when it ran, where, and when it's next (never again, for a one-off)."""
        upcoming = next_run(schedule["when"], schedule["timezone"], now, schedule.get("next_run"))
        self.db.execute(
            "UPDATE schedules SET last_run = ?, last_conversation = COALESCE(?, last_conversation), "
            "last_error = ?, next_run = ?, enabled = ? WHERE id = ?",
            (now, conversation_id, error, upcoming, 1 if upcoming else 0, schedule["id"]))

    @staticmethod
    def _public(row: dict[str, Any]) -> dict[str, Any]:
        when = json.loads(row["when_spec"])
        default = SYSTEM_TASKS.get(row.get("system") or "")
        return {**{k: v for k, v in row.items() if k != "when_spec"}, "when": when, "describe": describe(when),
                "isolate": bool(row["isolate"]), "enabled": bool(row["enabled"]),
                "governed": bool(row.get("governed")), "managed": bool(row.get("managed")),
                # Relay's own (system), made by the Governor, or added by the user
                "kind": "system" if row.get("system") else "governor" if row.get("managed") else "manual",
                "changed": bool(default) and (row["name"], row["prompt"], when, bool(row.get("governed"))) != (
                    default["name"], default["prompt"], default["when"], True)}
