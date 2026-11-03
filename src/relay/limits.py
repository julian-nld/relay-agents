"""Usage limits: noticing them, and when they reset.

A run can stop because the provider's usage limit was reached: a plan's
five-hour window, a weekly cap, an API's rate limit. Providers say so in
different ways:

- Claude Code sends a rate_limit_event with every turn: the window, how much
  of it is used and when it resets (the bridge turns it into a "limit" event);
- chat APIs answer HTTP 429, often with Retry-After or a reset header;
- the rest put it in an error message: "usage limit reached|1759186800",
  "try again in 2 hours 5 minutes", "resets at 3pm (Europe/Amsterdam)", …

Relay shows when the limit resets, and a code task goes on by itself then
(relay.scheduler); see RunManager._after_stop.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import time
import urllib.error
import urllib.request
from typing import Any

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore[assignment]
    ZoneInfoNotFoundError = Exception  # type: ignore[assignment,misc]

LIMIT_WORDS = re.compile(
    r"(usage|rate|session|weekly|daily|monthly|5[- ]hour|five[- ]hour)[ _-]?limit"
    r"|limit (was |has been )?(reached|exceeded|hit)"
    r"|hit (your|the) (usage |rate )?limit|quota exceeded|exceeded (your |the )?(current )?quota"
    r"|insufficient_quota|too many requests"
    r"|(http|status|error|code)[ :]*429\b|\b429 too many|resource[_ ]exhausted|rate[_ ]limited"
    r"|out of (usage|messages)", re.I)
UNITS = {"s": 1, "sec": 1, "second": 1, "m": 60, "min": 60, "minute": 60, "h": 3600, "hr": 3600, "hour": 3600,
         "d": 86400, "day": 86400}
DURATION = re.compile(r"(\d+(?:\.\d+)?)\s*(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d)(?![a-z])", re.I)


def is_limit(message: str | None) -> bool:
    return bool(message) and bool(LIMIT_WORDS.search(message))


# A model that can't be used at all, as programs and their services say it: it doesn't exist, the login can't use
# it, it isn't served where the user is, or not any more ("Model exo-free has been deprecated.", opencode's default
# of the day for a newcomer). Not a limit: waiting for one to reset is no plan (another model is).
UNUSABLE = re.compile(
    r"model (is )?(unavailable|not found|not supported|does not exist)|(unknown|invalid|no such) model"
    r"|doesn't know the model|issue with the selected model|not available in your (country|region)"
    r"|endpoint is unavailable|have access to (it|this model|the model)"
    r"|(has been|is) (deprecated|retired|discontinued)|no longer (available|supported|served)", re.I)


def unusable(message: str | None) -> bool:
    return bool(message) and bool(UNUSABLE.search(message))


def parse_reset(message: str | None, now: float | None = None) -> float | None:
    """When a limit resets, as a Unix time, from what a provider said; None if it doesn't say."""
    if not message:
        return None
    now = time.time() if now is None else now
    # An epoch: "usage limit reached|1759186800", "resetsAt": 1759186800
    match = re.search(r"(?:\||resets?_?at\"?:?\s*|reset=)(\d{10})(?:\d{3})?\b", message, re.I)
    if match:
        return float(match.group(1))
    # An ISO time: "resets at 2026-09-30T15:00:00Z"
    match = re.search(r"(\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d)?(?:\.\d+)?(?:Z|[+-]\d\d:?\d\d)?)", message)
    if match:
        try:
            stamp = dt.datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.astimezone()
            return stamp.timestamp()
        except ValueError:
            pass
    # A duration: "try again in 2 hours 5 minutes", "retry after 30s", "in 1h2m3s"
    match = re.search(r"(?:in|after|wait)\s+((?:\d+(?:\.\d+)?\s*[a-z]+[\s,]*(?:and\s+)?){1,4})", message, re.I)
    if match:
        seconds = sum(float(n) * UNITS[_unit(u)] for n, u in DURATION.findall(match.group(1)))
        if seconds:
            return now + seconds
    # A clock time: "resets at 3pm", "resets 3:30 PM (Europe/Amsterdam)", "resets at 15:00"
    match = re.search(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d\d))?\s*(am|pm)?(?:\s*\(([\w/+-]+)\))?", message, re.I)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2) or 0)
        if match.group(3):
            hour = hour % 12 + (12 if match.group(3).lower() == "pm" else 0)
        if hour < 24 and minute < 60:
            return _next_clock(hour, minute, match.group(4), now)
    return None


def _unit(word: str) -> str:
    word = word.lower().rstrip("s") or "s"
    return word if word in UNITS else {"hr": "hr", "min": "min", "sec": "sec"}.get(word, word[0])


def _next_clock(hour: int, minute: int, zone: str | None, now: float) -> float:
    tz = None
    if zone and ZoneInfo is not None:
        try:
            tz = ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError):
            tz = None
    current = dt.datetime.fromtimestamp(now, tz) if tz else dt.datetime.fromtimestamp(now).astimezone()
    moment = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if moment.timestamp() <= now:
        moment += dt.timedelta(days=1)
    return moment.timestamp()


def from_headers(headers: Any, now: float | None = None) -> float | None:
    """A 429's reset time from Retry-After or the x-ratelimit-reset headers."""
    now = time.time() if now is None else now
    if not headers:
        return None
    retry = headers.get("retry-after")
    if retry:
        try:
            return now + float(retry)
        except ValueError:
            try:
                return dt.datetime.strptime(retry, "%a, %d %b %Y %H:%M:%S %Z").replace(
                    tzinfo=dt.timezone.utc).timestamp()
            except ValueError:
                pass
    for name in ("x-ratelimit-reset-requests", "x-ratelimit-reset-tokens", "x-ratelimit-reset", "ratelimit-reset"):
        value = headers.get(name)
        if not value:
            continue
        if re.fullmatch(r"\d{10}", value.strip()):
            return float(value)  # a moment (epoch seconds)
        if re.fullmatch(r"\d+(\.\d+)?", value.strip()):
            return now + float(value)  # seconds from now, as RateLimit-Reset has it
        found = parse_reset(f"in {value}", now)
        if found:
            return found
    return None


def reached(events: list[dict[str, Any]], now: float | None = None) -> dict[str, Any] | None:
    """Whether a run's events say it stopped on a usage limit: {"resets_at", "message"}."""
    for event in reversed(events):
        if event.get("type") == "limit" and event.get("status") == "rejected":
            return {"resets_at": event.get("resets_at"), "message": event.get("message") or "Usage limit reached"}
    errors = [e.get("message", "") for e in events if e.get("type") == "error"]
    for message in reversed(errors):
        if is_limit(message):
            return {"resets_at": parse_reset(message, now), "message": message}
    return None


def latest(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The last limit event of a run (how much of the window is used), if any."""
    return next((e for e in reversed(events) if e.get("type") == "limit"), None)


# What's left of a plan's limits (Provider.limits; Settings → Usage). Asked the way the providers'
# own programs ask (Claude Code's /usage, Codex's /status, Copilot's quota), with their logins as
# they are: Relay never renews one (that would sign the program out). A window:
# {"label", "used": 0–1, "resets_at": Unix time or None, "left": "187 of 200" or None}.

CLAUDE_USAGE = "https://api.anthropic.com/api/oauth/usage"
CHATGPT_USAGE = "https://chatgpt.com/backend-api/wham/usage"
COPILOT_USER = "https://api.github.com/copilot_internal/user"
CLAUDE_KINDS = {"session": "5-hour", "weekly_all": "Weekly"}


def fetch_json(url: str, headers: dict[str, str], timeout: float = 8) -> tuple[Any, str | None]:
    """(the JSON answer, None), or (None, why not)."""
    request = urllib.request.Request(url, headers={"User-Agent": "relay", "Accept": "application/json", **headers})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read()), None
    except urllib.error.HTTPError as exc:
        return None, ("its login was refused" if exc.code in (401, 403) else f"the provider answered HTTP {exc.code}")
    except (OSError, ValueError) as exc:
        return None, f"no answer ({getattr(exc, 'reason', exc)})"


def _epoch(value: Any) -> float | None:
    """A Unix time from an epoch (seconds or ms) or an ISO time; None if it's neither."""
    if isinstance(value, (int, float)) and value > 0:
        return float(value) / (1000 if value > 1e11 else 1)
    if isinstance(value, str) and value:
        try:
            stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return (stamp if stamp.tzinfo else stamp.replace(tzinfo=dt.timezone.utc)).timestamp()
    return None


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def claude_windows(answer: Any) -> list[dict[str, Any]]:
    """Claude's /api/oauth/usage: its "limits" (percent used), else five_hour/seven_day (utilization, percent)."""
    if not isinstance(answer, dict):
        return []
    windows = []
    for item in answer.get("limits") or []:
        if isinstance(item, dict) and _number(item.get("percent")) is not None:
            kind = str(item.get("kind") or item.get("group") or "limit")
            windows.append({"label": CLAUDE_KINDS.get(kind, kind.replace("_", " ").capitalize()),
                            "used": _number(item["percent"]) / 100, "resets_at": _epoch(item.get("resets_at")),
                            "left": None})
    if windows:
        return windows
    for key, label in (("five_hour", "5-hour"), ("seven_day", "Weekly"), ("seven_day_opus", "Weekly (Opus)"),
                       ("seven_day_sonnet", "Weekly (Sonnet)")):
        window = answer.get(key)
        if isinstance(window, dict) and _number(window.get("utilization")) is not None:
            windows.append({"label": label, "used": _number(window["utilization"]) / 100,
                            "resets_at": _epoch(window.get("resets_at")), "left": None})
    return windows


def chatgpt_windows(answer: Any, now: float | None = None) -> list[dict[str, Any]]:
    """ChatGPT's wham/usage: rate_limit's primary (5-hour) and secondary (weekly) windows."""
    now = time.time() if now is None else now
    limit = answer.get("rate_limit") if isinstance(answer, dict) else None
    if not isinstance(limit, dict):
        return []
    windows = []
    for key in ("primary_window", "secondary_window"):
        window = limit.get(key)
        if not isinstance(window, dict) or _number(window.get("used_percent")) is None:
            continue
        seconds = _number(window.get("limit_window_seconds")) or 0
        label = ("5-hour" if seconds == 5 * 3600 else "Weekly" if seconds == 7 * 86400
                 else f"{round(seconds / 86400)}-day" if seconds >= 86400 else f"{round(seconds / 3600)}-hour"
                 if seconds else "Limit")
        after = _number(window.get("reset_after_seconds"))
        windows.append({"label": label, "used": _number(window["used_percent"]) / 100,
                        "resets_at": _epoch(window.get("reset_at")) or (now + after if after is not None else None),
                        "left": None})
    return windows


def copilot_windows(answer: Any) -> list[dict[str, Any]]:
    """Copilot's copilot_internal/user: its quotas that count (premium requests, chat messages), per month."""
    snapshots = answer.get("quota_snapshots") if isinstance(answer, dict) else None
    if not isinstance(snapshots, dict):
        return []
    resets = _epoch(answer.get("quota_reset_date_utc")) or _epoch(answer.get("quota_reset_date"))
    windows = []
    for key, label in (("premium_interactions", "Premium requests"), ("chat", "Chat messages")):
        quota = snapshots.get(key)
        entitlement = _number((quota or {}).get("entitlement"))
        if not isinstance(quota, dict) or quota.get("unlimited") or not quota.get("has_quota", True) or not entitlement:
            continue  # (unlimited, or none in this plan)
        remaining = _number(quota.get("remaining"))
        if remaining is None:
            remaining = entitlement * (_number(quota.get("percent_remaining")) or 0) / 100
        windows.append({"label": label, "used": max(0.0, 1 - remaining / entitlement), "resets_at": resets,
                        "left": f"{remaining:g} of {entitlement:g}"})
    return windows


def plan_name(value: Any) -> str | None:
    """"pro", "free_limited_copilot", "plus" → Pro, Free, Plus."""
    if not isinstance(value, str) or not value:
        return None
    return "Free" if "free" in value.lower() else value.replace("_", " ").title()


RUN_WINDOWS = {"five_hour": "5-hour", "seven_day": "Weekly", "seven_day_opus": "Weekly (Opus)",
               "seven_day_sonnet": "Weekly (Sonnet)"}


def run_windows(usage: Any) -> list[dict[str, Any]]:
    """The windows a run's limit event told (model_status.usage: utilization 0–1), as limits() gives them."""
    windows = (usage or {}).get("windows") if isinstance(usage, dict) else None
    if not isinstance(windows, dict):
        return []
    return [{"label": RUN_WINDOWS.get(name, name.replace("_", " ").capitalize()), "used": _number(w.get("utilization")),
             "resets_at": _epoch(w.get("resets_at")), "left": None}
            for name, w in windows.items() if isinstance(w, dict) and _number(w.get("utilization")) is not None]
