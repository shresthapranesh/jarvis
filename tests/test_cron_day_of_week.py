"""Numeric weekdays in a cron expression mean what Unix cron says they mean.

`CronTrigger.from_crontab` is named for the crontab format but does not
implement its day-of-week numbering: Unix cron counts 0=Sunday..6=Saturday,
APScheduler counts 0=Monday..6=Sunday, and `from_crontab` hands the field
straight to the APScheduler field parser. Every numeric weekday therefore fired
one day late — "0 9 * * 1" ran Tuesday, and "0 9 * * 1-5" (which reads as "every
weekday") ran Tuesday through Saturday. `_compute_next_run_at` builds its
trigger the same way, so the card faithfully displayed the wrong day too.

`normalize_crontab` rewrites the field as explicit weekday names, which
APScheduler does interpret unambiguously.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from core.scheduler import _cron, normalize_crontab

TZ = ZoneInfo("America/Los_Angeles")

# Unix cron numbering, which is what a user typing into the schedule field means.
SUN, MON, TUE, WED, THU, FRI, SAT = range(7)


def _weekdays_fired(expr: str) -> set[int]:
    """Which weekdays the trigger actually fires on over a full year, as Unix
    day-of-week numbers. A year covers every month/DST combination."""
    trigger = _cron(expr)
    start, end = datetime(2026, 1, 1, tzinfo=TZ), datetime(2027, 1, 1, tzinfo=TZ)
    fired, cur = set(), start
    while cur < end:
        nxt = trigger.get_next_fire_time(None, cur)
        if nxt is None or nxt >= end:
            break
        fired.add((nxt.weekday() + 1) % 7)  # Python Mon=0 → Unix Sun=0
        cur = nxt + timedelta(minutes=1)
    return fired


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("0 9 * * 0", {SUN}),
        ("0 9 * * 1", {MON}),  # the headline case: read as Monday, used to fire Tuesday
        ("0 9 * * 2", {TUE}),
        ("0 9 * * 3", {WED}),
        ("0 9 * * 4", {THU}),
        ("0 9 * * 5", {FRI}),
        ("0 9 * * 6", {SAT}),
        ("0 9 * * 7", {SUN}),  # Unix accepts 7 as Sunday too
    ],
)
def test_each_numeric_weekday_fires_on_that_weekday(expr, expected):
    assert _weekdays_fired(expr) == expected


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("0 9 * * 1-5", {MON, TUE, WED, THU, FRI}),  # "every weekday" — was Tue–Sat
        ("0 9 * * 0,6", {SAT, SUN}),  # "every weekend"
        ("0 9 * * 6-1", {SAT, SUN, MON}),  # ranges wrap around the week
        ("0 9 * * 1-5/2", {MON, WED, FRI}),  # step counts from the range start
        ("0 9 * * */2", {SUN, TUE, THU, SAT}),
        ("0 9 * * *", {SUN, MON, TUE, WED, THU, FRI, SAT}),
    ],
)
def test_ranges_lists_and_steps(expr, expected):
    assert _weekdays_fired(expr) == expected


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("0 9 * * mon", {MON}),
        ("0 9 * * MON", {MON}),
        ("0 9 * * sun", {SUN}),
        ("0 9 * * mon-fri", {MON, TUE, WED, THU, FRI}),
        ("0 9 * * sat,sun", {SAT, SUN}),
    ],
)
def test_named_weekdays_keep_working(expr, expected):
    """Names were already unambiguous — normalization must not disturb them."""
    assert _weekdays_fired(expr) == expected


@pytest.mark.parametrize(
    "expr",
    [
        "*/5 * * * *",  # the other four fields share Unix semantics already
        "0 */6 * * *",
        "20 * * * *",
        "0 4 1 * *",
        "0 9 * * *",
        "bad",  # malformed — left for APScheduler to reject with its own message
        "1 2 3",
        "0 9 * * xyz",  # syntax we don't model is passed through, never mangled
        "0 9 * * 1#2",
    ],
)
def test_expressions_without_a_numeric_weekday_are_untouched(expr):
    assert normalize_crontab(expr) == expr


def test_displayed_next_run_lands_on_the_weekday_the_user_wrote():
    """The user-visible half: the automation card's next-run must name the same
    weekday as the expression, since it is built from the same helper."""
    from server.automation_runtime import _compute_next_run_at

    auto = type("Auto", (), {"schedule": "0 9 * * 1", "enabled": True})()
    next_run = _compute_next_run_at(auto)
    # Asserting first turns a None return into a readable failure instead of a
    # TypeError inside fromisoformat, and narrows the str | None for the checker.
    assert next_run is not None, "an enabled automation with a valid cron must have a next run"
    shown = datetime.fromisoformat(next_run)
    assert shown.weekday() == 0  # Python Monday
    assert shown.hour == 9
