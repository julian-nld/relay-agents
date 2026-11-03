"""/timed (relay.timed): the timer from what's typed, its parts, and what Relay tells an agent."""

import pytest

from relay import timed


@pytest.mark.parametrize("text, expected, rest", [
    ("60", (None, 51, 60), ""),  # one: MAX; OPTIMAL 15% before it
    ("30 60", (30, 30, 60), ""),  # MIN MAX: OPTIMAL is MIN (no soft part)
    ("30 60 45 Build it", (30, 45, 60), "Build it"),
    ("1h30 Fix the tests", (None, 76.5, 90), "Fix the tests"),
    ("20m 1h", (20, 20, 60), ""),
])
def test_parse(text, expected, rest):
    timer, task = timed.parse(text, now=1000)
    got = tuple(None if timer[k] is None else (timer[k] - 1000) / 60 for k in ("min", "optimal", "max"))
    assert got == pytest.approx(expected) and task == rest and not timer["asked"] and timer["ended"] is None


@pytest.mark.parametrize("text", ["", "soon", "0", "60 30", "10 60 5", "10 60 90",
                                  "10081", "99999h"])  # (more than a week: an agent kept at it for years)
def test_parse_refuses(text):
    with pytest.raises(timed.TimedError):
        timed.parse(text)


def test_a_timer_from_numbers_as_a_script_gives_them():
    """The SDK and the API give numbers, which can be what /timed can't type: below 0."""
    assert timed.make(None, timed.LONGEST, now=0)["max"] == timed.LONGEST * 60  # (a week is fine)
    for low, high, best in ((-5, 60, None), (None, 60, -1)):
        with pytest.raises(timed.TimedError, match="0 minutes or more"):
            timed.make(low, high, best)


def test_phases_and_what_the_agent_is_told():
    timer, _ = timed.parse("30 60 45", now=0)
    assert timed.phase(timer, 10 * 60) == "hard" and timed.phase(timer, 40 * 60) == "soft"
    assert timed.phase(timer, 50 * 60) == "free" and timed.phase(timer, 60 * 60) == "over"
    prompt, after = timed.next_prompt(timer, 10 * 60)
    assert prompt == "Please continue. Per the timed request, you are free in 20 minutes. Keep working until then."
    assert after is timer  # (it goes on every time)
    prompt, after = timed.next_prompt(timer, 40 * 60)
    assert prompt.startswith("You may stop now, but you have until ") and "(5 minutes more)" in prompt and after["asked"]
    assert timed.next_prompt(after, 41 * 60)[0] is None  # (asked once)
    assert timed.next_prompt(timer, 50 * 60)[0] is None
    assert timed.remaining(65 * 60) == "1 hour 5 minutes" and timed.remaining(20) == "1 minute"
    assert timed.describe(timer) == "at least 30, ideally 45, at most 60 min"
    # (/timed 2 4: the unit after the last, not only the first, as a simulated user read "at most 4")
    assert timed.describe({"started": 0, "min": 120, "optimal": 120, "max": 240}) == "at least 2, at most 4 min"


def test_time_up():
    timer, _ = timed.parse("60", now=0)
    assert not timed.time_up(timer, 59 * 60) and timed.time_up(timer, 60 * 60)
    assert timed.time_up({**timer, "ended": "max"}, 0)
    assert not timed.time_up({**timer, "ended": "you stopped the timer"}, 61 * 60)  # (switched off: holds nothing)
    assert not timed.time_up(None) and not timed.time_up("")
