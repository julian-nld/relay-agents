"""/timed [MIN] MAX [OPTIMAL] [task]: an agent kept at it for a while, and stopped at MAX.

The times are minutes from the command (90, 90m, 1h, 1h30). One number is MAX, two are MIN MAX,
three MIN MAX OPTIMAL; the rest is the task. Kept with the conversation (conversations.timed):

    before MIN          hard: an agent that stops is told to go on (CONTINUE), every time
    MIN … OPTIMAL       soft: an agent that stops is asked once whether something useful is left
    OPTIMAL … MAX       free: it may stop
    MAX                 over: Relay stops it, with its fallback loop (relay.scheduler)

OPTIMAL is MAX less 15% by default; given MIN and no OPTIMAL, it's MIN (no soft part). Without
MIN there's only the soft part, until OPTIMAL. RunManager asks next_prompt() when a run ends.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

SLACK = 0.15  # OPTIMAL by default: this much of MAX before it
LONGEST = 7 * 24 * 60  # minutes: a week (an agent kept at it for years: "/timed 99999h" was taken)
TIME = re.compile(r"^(?:(\d+(?:\.\d+)?)h(?:(\d+)m?)?|(\d+(?:\.\d+)?)m?)$", re.I)
CONTINUE = "Please continue. Per the timed request, you are free in {left}. Keep working until then."
ASK = ("You may stop now, but you have until {until} ({left} more). If something useful is left (tests, edge cases, "
       "cleaning up), keep working on it until then; if the task is really done, say so briefly.")


class TimedError(ValueError):
    pass


def minutes(word: str) -> float | None:
    """90, 90m, 1h, 1h30, 1.5h → minutes; None when it isn't a time."""
    match = TIME.match(word.strip())
    if not match:
        return None
    hours, rest, plain = match.groups()
    return float(hours) * 60 + float(rest or 0) if hours is not None else float(plain)


def parse(text: str, now: float | None = None) -> tuple[dict[str, Any], str]:
    """The timer (Unix times) and the task after the times; TimedError when it doesn't make sense."""
    now = time.time() if now is None else now
    words = str(text or "").split()
    times = []
    while words and len(times) < 3 and minutes(words[0]) is not None:
        times.append(minutes(words.pop(0)))
    if not times:
        raise TimedError("Say for how long: /timed [min] max [optimal] [task], in minutes (or 1h30)")
    low, high, best = (None, times[0], None) if len(times) == 1 else \
        (times[0], times[1], None) if len(times) == 2 else tuple(times)
    return make(low, high, best, now), " ".join(words)


def make(low: float | None, high: float, best: float | None = None, now: float | None = None) -> dict[str, Any]:
    """The timer for MIN, MAX and OPTIMAL in minutes (MIN and OPTIMAL may be left out, as in /timed); TimedError
    when it doesn't make sense."""
    now = time.time() if now is None else now
    if high is None or high <= 0:
        raise TimedError("The most time it has must be more than 0")
    if high > LONGEST:
        raise TimedError(f"At most a week ({LONGEST} minutes)")
    if (low is not None and low < 0) or (best is not None and best < 0):
        raise TimedError("Times are 0 minutes or more")
    if best is None:
        best = low if low is not None else high * (1 - SLACK)
    if (low or 0) > best or best > high:
        raise TimedError("It must be min ≤ optimal ≤ max")
    return {"started": now, "min": now + low * 60 if low is not None else None,
            "optimal": now + best * 60, "max": now + high * 60, "asked": False, "ended": None}


def phase(timer: dict[str, Any] | None, now: float | None = None) -> str | None:
    """hard (before MIN), soft (before OPTIMAL), free, or over (MAX passed, or ended); None: no timer."""
    if not timer:
        return None
    now = time.time() if now is None else now
    if timer.get("ended") or now >= timer["max"]:
        return "over"
    if timer.get("min") and now < timer["min"]:
        return "hard"
    if now < timer["optimal"]:
        return "soft"
    return "free"


def time_up(found: Any, now: float | None = None) -> bool:
    """Whether a conversation's timed request (its JSON, or the timer) ran out: nothing goes on then. A
    timer the user ended doesn't hold anything back."""
    timer = (json.loads(found) if found else None) if isinstance(found, str) else found
    if not timer:
        return False
    now = time.time() if now is None else now
    return timer.get("ended") == "max" or (not timer.get("ended") and now >= timer["max"])


def next_prompt(timer: dict[str, Any] | None, now: float | None = None) -> tuple[str | None, dict[str, Any] | None]:
    """When an agent stopped: (what Relay tells it, the timer as it is then); (None, …) to let it be."""
    now = time.time() if now is None else now
    where = phase(timer, now)
    if where == "hard":
        return CONTINUE.format(left=remaining(timer["min"] - now)), timer
    if where == "soft" and not timer.get("asked"):
        return ASK.format(until=time.strftime("%H:%M", time.localtime(timer["optimal"])),
                          left=remaining(timer["optimal"] - now)), {**timer, "asked": True}
    return None, timer


def remaining(seconds: float) -> str:
    """23 minutes, 1 hour 5 minutes (a minute at least)."""
    total = max(1, round(seconds / 60))
    hours, mins = divmod(total, 60)
    parts = [f"{hours} hour{'s' if hours != 1 else ''}"] if hours else []
    if mins or not hours:
        parts.append(f"{mins} minute{'s' if mins != 1 else ''}")
    return " ".join(parts)


def describe(timer: dict[str, Any]) -> str:
    """At least 30, ideally 45, at most 60 min (from the start; the unit last, so one alone has it too)."""
    def span(moment: float) -> int:
        return round((moment - timer["started"]) / 60)
    parts = [f"at least {span(timer['min'])}"] if timer.get("min") else []
    if timer["optimal"] != timer.get("min"):
        parts.append(f"ideally {span(timer['optimal'])}")
    parts.append(f"at most {span(timer['max'])} min")
    return ", ".join(parts)
