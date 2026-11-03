import datetime as dt

import pytest

from relay import schedules

AMS = "Europe/Amsterdam"


def at(text, zone=AMS):
    return dt.datetime.fromisoformat(text).replace(tzinfo=schedules._zone(zone)).timestamp()


def test_next_times():
    monday_9 = at("2026-09-28T09:00")  # a Monday
    assert schedules.next_run({"kind": "daily", "time": "08:00"}, AMS, monday_9) == at("2026-09-29T08:00")
    assert schedules.next_run({"kind": "daily", "time": "10:00"}, AMS, monday_9) == at("2026-09-28T10:00")
    friday_9 = at("2026-10-02T09:00")
    assert schedules.next_run({"kind": "weekdays", "time": "08:00"}, AMS, friday_9) == at("2026-10-05T08:00")
    assert schedules.next_run({"kind": "weekly", "days": [2, 6], "time": "07:30"}, AMS, monday_9) == at("2026-09-30T07:30")
    assert schedules.next_run({"kind": "interval", "minutes": 90}, AMS, monday_9) == monday_9 + 5400
    assert schedules.next_run({"kind": "once", "at": monday_9 + 60}, AMS, monday_9) == monday_9 + 60
    assert schedules.next_run({"kind": "once", "at": monday_9 - 60}, AMS, monday_9) is None
    # Across the switch to winter time (25 October 2026), still 08:00 on the clock
    assert schedules.next_run({"kind": "daily", "time": "08:00"}, AMS, at("2026-10-25T01:00")) == at("2026-10-25T08:00")
    # 02:30 that day comes twice: once (the first); a day without it in spring: an hour later on the clock
    first = schedules.next_run({"kind": "daily", "time": "02:30"}, AMS, at("2026-10-25T00:00"))
    assert first == at("2026-10-25T02:30") and schedules.next_run(
        {"kind": "daily", "time": "02:30"}, AMS, first + 20) == at("2026-10-26T02:30")
    assert schedules.next_run({"kind": "daily", "time": "02:30"}, AMS, at("2027-03-28T00:00")) == at("2027-03-28T03:30")
    # An interval on its own beat: from when it was due, though it ran a tick later (each run drifted by that)
    hourly = {"kind": "interval", "minutes": 60}
    assert schedules.next_run(hourly, AMS, monday_9 + 19, since=monday_9) == monday_9 + 3600
    assert schedules.next_run(hourly, AMS, monday_9 + 3 * 3600 + 5, since=monday_9) == monday_9 + 4 * 3600  # (missed)


def test_invalid_whens():
    for when in [{"kind": "hourly"}, {"kind": "daily", "time": "25:00"}, {"kind": "weekly", "time": "08:00"},
                 {"kind": "interval", "minutes": 1}, {"kind": "once"},
                 # Past what a date can say (its next run past year 9999 broke the Governor's task.list)
                 {"kind": "interval", "minutes": 10**12}, {"kind": "once", "at": float("inf")},
                 {"kind": "once", "at": float("nan")}, {"kind": "once", "at": 1e15}]:
        with pytest.raises(schedules.ScheduleError):
            schedules.validate_when(when)
    assert schedules.validate_when({"kind": "interval", "minutes": schedules.MAX_INTERVAL})["minutes"] > 0
    assert schedules.describe({"kind": "weekly", "days": [0, 4], "time": "08:00"}) == "Mon, Fri at 08:00"
    assert schedules.describe({"kind": "interval", "minutes": 150}) == "every 2 h 30 min"


def test_when_from_a_few_words():
    from relay.schedules import parse_when
    assert parse_when("every 72 hours") == {"kind": "interval", "minutes": 4320}
    assert parse_when("every 90m") == {"kind": "interval", "minutes": 90}
    assert parse_when("daily 08:00") == {"kind": "daily", "time": "08:00"}
    assert parse_when("weekly mon,wed 08:00") == {"kind": "weekly", "days": [0, 2], "time": "08:00"}
    assert parse_when("once 2026-10-05 09:00", "Europe/Amsterdam")["at"] == 1791183600
    assert parse_when({"kind": "daily", "time": "07:30"}) == {"kind": "daily", "time": "07:30"}
    # An ISO time with its own zone (a model thinking in UTC), or from now
    for iso in ("once 2026-10-05T07:00Z", "once 2026-10-05T07:00:00z", "2026-10-05 09:00+02:00", "once 2026-10-05T09:00 +0200"):
        assert parse_when(iso)["at"] == 1791183600, iso
    assert parse_when("in 10 minutes", now=1000) == {"kind": "once", "at": 1600}
    assert parse_when("once in 2 hours", now=1000) == {"kind": "once", "at": 8200}
    for wrong in ("sometimes", "every week or so", "once tomorrow", "", "in 3 fortnights", "once 2026-13-05T07:00Z"):
        with pytest.raises(schedules.ScheduleError):
            parse_when(wrong)


def test_a_task_saved_with_when_in_words(tmp_path):
    """Over the API too (Schedules.save, for the SDK as for the Governor): when in a few words, in the task's
    time zone, or as a dict; one renamed keeps when it runs."""
    from relay.db import Database
    db = Database(tmp_path / "relay.db")
    db.execute("INSERT INTO users (id, username, password_hash, created) VALUES (1, 'me', 'x', 0)")
    tasks = schedules.Schedules(db)
    saved = tasks.save(1, {"name": "Check", "prompt": "Anything new?", "provider_id": "fake", "when": "weekdays 09:00",
                           "timezone": AMS}, now=at("2026-10-02T10:00"))  # (a Friday)
    assert saved["when"] == {"kind": "weekdays", "time": "09:00"} and saved["next_run"] == at("2026-10-05T09:00")
    assert tasks.save(1, {"when": "once 2026-10-06 09:00"}, saved["id"], now=at("2026-10-02T10:00"))["next_run"] \
        == at("2026-10-06T09:00")
    later = tasks.save(1, {"when": "in 10 minutes"}, saved["id"], now=1000)
    assert later["when"] == {"kind": "once", "at": 1600}
    assert tasks.save(1, {"name": "Check again"}, saved["id"], now=1000)["when"] == {"kind": "once", "at": 1600}
    with pytest.raises(schedules.ScheduleError):
        tasks.save(1, {"when": "sometimes"}, saved["id"], now=1000)
    # A time zone that isn't one is said (it ran in the server's, unsaid)
    with pytest.raises(schedules.ScheduleError, match="No time zone 'Mars/Olympus'"):
        tasks.save(1, {"timezone": "Mars/Olympus"}, saved["id"], now=1000)
    # Every hour, on its beat: run a tick late, the next is an hour after it was due, not after it ran
    hourly = tasks.save(1, {"when": "every 60 minutes"}, saved["id"], now=at("2026-10-02T10:00"))
    assert hourly["next_run"] == at("2026-10-02T11:00")
    tasks.ran(hourly, None, None, at("2026-10-02T11:00") + 19)
    assert tasks.get(1, saved["id"])["next_run"] == at("2026-10-02T12:00")
