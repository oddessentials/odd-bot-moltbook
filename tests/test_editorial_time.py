"""Unit tests for src.editorial_time.

Locks the publish-eligibility contract to America/New_York local time:

  - Daily window opens at 05:00 local every calendar day.
  - Weekly podcast window opens at 09:00 local every Sunday.

Failure mode locked down here: a Mac mini reboot crossing the UTC date
boundary must not pre-fire the next local-day's brief or the next
weekly podcast window. See plans/incident-2026-04-29-runatload-utc.md.

Stdlib unittest only — run via:

    .venv/bin/python -m unittest discover -s tests
"""

from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from src.editorial_time import (
    EDITORIAL_TZ,
    daily_editorial_state,
    is_daily_window_open_for,
    most_recent_weekly_window_date,
    weekly_window_satisfied,
)


def _local(year: int, month: int, day: int, hour: int, minute: int = 0,
           second: int = 0) -> datetime:
    """Construct a UTC instant equivalent to the given America/New_York wall-clock."""
    return (
        datetime(year, month, day, hour, minute, second, tzinfo=EDITORIAL_TZ)
        .astimezone(timezone.utc)
    )


class TestDailyEditorialState(unittest.TestCase):
    """Maps UTC instants to (today_local, window_open) for the daily contract."""

    def test_22_40_EDT_apr_29_is_apr_29_window_open(self):
        # The exact failure-mode instant: a 22:40 EDT reboot on April 29.
        # Pre-fix this resolved to today=April 30 (UTC). Post-fix it must
        # resolve to April 29 with the window open (today's 05:00 EDT
        # already passed earlier in the day).
        d, open_ = daily_editorial_state(_local(2026, 4, 29, 22, 40))
        self.assertEqual(d, date(2026, 4, 29))
        self.assertTrue(open_)

    def test_02_25_UTC_apr_30_is_apr_29_window_open(self):
        # Same wall-clock as above, expressed in UTC. This is what
        # datetime.now(timezone.utc) actually returned during the incident.
        d, open_ = daily_editorial_state(
            datetime(2026, 4, 30, 2, 25, tzinfo=timezone.utc),
        )
        self.assertEqual(d, date(2026, 4, 29))
        self.assertTrue(open_)

    def test_04_59_EDT_apr_30_is_apr_30_window_closed(self):
        d, open_ = daily_editorial_state(_local(2026, 4, 30, 4, 59))
        self.assertEqual(d, date(2026, 4, 30))
        self.assertFalse(open_)

    def test_05_00_EDT_apr_30_is_apr_30_window_open(self):
        d, open_ = daily_editorial_state(_local(2026, 4, 30, 5, 0))
        self.assertEqual(d, date(2026, 4, 30))
        self.assertTrue(open_)

    def test_09_00_UTC_apr_30_scheduled_fire_window_open(self):
        # Scheduled launchd fire: Hour=5 local in EDT = 09:00 UTC. This is
        # the canonical "every day works" path. Must remain green.
        d, open_ = daily_editorial_state(
            datetime(2026, 4, 30, 9, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(d, date(2026, 4, 30))
        self.assertTrue(open_)

    def test_winter_eastern_standard_time_05_00_window_open(self):
        # EST (UTC-5) outside DST. Confirms ZoneInfo handles DST shifts
        # transparently — the 05:00 local boundary is the same regardless
        # of EST vs EDT, and the test covers a January date for safety.
        d, open_ = daily_editorial_state(
            datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc),  # 05:00 EST
        )
        self.assertEqual(d, date(2026, 1, 15))
        self.assertTrue(open_)

    def test_naive_datetime_raises(self):
        with self.assertRaises(ValueError):
            daily_editorial_state(datetime(2026, 4, 30, 5, 0))


class TestMostRecentWeeklyWindowDate(unittest.TestCase):
    """Sunday 09:00 America/New_York is the weekly window opening."""

    def test_sunday_at_09_00_returns_today(self):
        # 2026-05-03 is a Sunday. 09:00 EDT scheduled fire.
        self.assertEqual(
            most_recent_weekly_window_date(_local(2026, 5, 3, 9, 0)),
            date(2026, 5, 3),
        )

    def test_sunday_at_10_00_overslept_returns_today(self):
        self.assertEqual(
            most_recent_weekly_window_date(_local(2026, 5, 3, 10, 0)),
            date(2026, 5, 3),
        )

    def test_sunday_at_04_00_before_window_returns_previous_sunday(self):
        # Sunday before 09:00 — the window hasn't opened yet today, so
        # the most recent opening is the previous Sunday (2026-04-26).
        self.assertEqual(
            most_recent_weekly_window_date(_local(2026, 5, 3, 4, 0)),
            date(2026, 4, 26),
        )

    def test_saturday_evening_returns_previous_sunday(self):
        # The bug shape for the podcast: Saturday 22:25 EDT reboot. The
        # most-recent weekly opening is still the previous Sunday — we
        # haven't crossed THIS Sunday's 09:00 yet.
        self.assertEqual(
            most_recent_weekly_window_date(_local(2026, 5, 2, 22, 25)),
            date(2026, 4, 26),
        )

    def test_tuesday_returns_most_recent_sunday(self):
        # Mid-week: most-recent opening is this past Sunday.
        # 2026-04-28 is a Tuesday; this past Sunday is 2026-04-26.
        self.assertEqual(
            most_recent_weekly_window_date(_local(2026, 4, 28, 14, 0)),
            date(2026, 4, 26),
        )

    def test_naive_datetime_raises(self):
        with self.assertRaises(ValueError):
            most_recent_weekly_window_date(datetime(2026, 5, 3, 9, 0))


class TestWeeklyWindowSatisfied(unittest.TestCase):
    """`weekly_window_satisfied` => REFUSE the run."""

    def test_first_publish_eligible(self):
        # No prior episode → never satisfied → wrapper proceeds.
        self.assertFalse(
            weekly_window_satisfied(_local(2026, 5, 3, 9, 0), None),
        )

    def test_sunday_09_00_after_previous_week_publish_proceeds(self):
        # Scheduled Sunday fire. Latest publish was previous Sunday.
        # Window opened today; latest_date < today → not yet satisfied.
        self.assertFalse(
            weekly_window_satisfied(
                _local(2026, 5, 3, 9, 0), date(2026, 4, 26),
            ),
        )

    def test_sunday_10_00_overslept_proceeds(self):
        # Catch-up scenario: launchd missed the 09:00 fire (machine asleep)
        # and fires at 10:00 instead. Still in today's open window.
        self.assertFalse(
            weekly_window_satisfied(
                _local(2026, 5, 3, 10, 0), date(2026, 4, 26),
            ),
        )

    def test_sunday_morning_before_window_refuses(self):
        # Sunday before 09:00 — the window hasn't opened. Most-recent
        # opening is the previous Sunday, which the latest publish
        # already filled.
        self.assertTrue(
            weekly_window_satisfied(
                _local(2026, 5, 3, 4, 0), date(2026, 4, 26),
            ),
        )

    def test_saturday_evening_reboot_refuses(self):
        # The exact incident shape applied to the podcast pipeline. A
        # late-Saturday reboot one week after a Sunday publish must NOT
        # be eligible — the current weekly window doesn't open until
        # tomorrow at 09:00 EDT.
        self.assertTrue(
            weekly_window_satisfied(
                _local(2026, 5, 2, 22, 25), date(2026, 4, 26),
            ),
        )

    def test_already_published_this_window_refuses(self):
        # Tuesday 14:00 reboot, this past Sunday already published.
        self.assertTrue(
            weekly_window_satisfied(
                _local(2026, 4, 28, 14, 0), date(2026, 4, 26),
            ),
        )


class TestIsDailyWindowOpenFor(unittest.TestCase):
    """`is_daily_window_open_for(d, now_utc)` is the per-iteration helper
    that closes the captured-too-early race in the orchestrator's per-date
    loop."""

    def test_today_before_window_returns_false(self):
        # 04:59 EDT April 30 — today, window not yet opened.
        self.assertFalse(
            is_daily_window_open_for(
                date(2026, 4, 30), _local(2026, 4, 30, 4, 59),
            ),
        )

    def test_today_at_window_returns_true(self):
        self.assertTrue(
            is_daily_window_open_for(
                date(2026, 4, 30), _local(2026, 4, 30, 5, 0),
            ),
        )

    def test_today_after_window_returns_true(self):
        self.assertTrue(
            is_daily_window_open_for(
                date(2026, 4, 30), _local(2026, 4, 30, 22, 30),
            ),
        )

    def test_past_date_always_open_for_catchup(self):
        # Even at a moment when today's window is closed (04:59 EDT April 30),
        # a past date (April 29) must be eligible — orphan-promotion catch-up.
        self.assertTrue(
            is_daily_window_open_for(
                date(2026, 4, 29), _local(2026, 4, 30, 4, 59),
            ),
        )

    def test_future_date_never_open(self):
        # 22:40 EDT April 29 → "today" is April 29. April 30 is the future.
        self.assertFalse(
            is_daily_window_open_for(
                date(2026, 4, 30), _local(2026, 4, 29, 22, 40),
            ),
        )

    def test_captured_too_early_pattern_resolves_correctly_at_decision_time(self):
        # The exact race the orchestrator's per-iteration check exists to
        # close: started captured at 04:59:30, decision happens at 05:00:05
        # after a slow reconciliation. The helper must read True at decision
        # time even though the start-of-run snapshot would have read False.
        started = _local(2026, 4, 30, 4, 59, 30)
        decision = _local(2026, 4, 30, 5, 0, 5)
        self.assertFalse(is_daily_window_open_for(date(2026, 4, 30), started))
        self.assertTrue(is_daily_window_open_for(date(2026, 4, 30), decision))

    def test_naive_datetime_raises(self):
        with self.assertRaises(ValueError):
            is_daily_window_open_for(
                date(2026, 4, 30), datetime(2026, 4, 30, 5, 0),
            )


class TestEditorialTzIdentity(unittest.TestCase):
    def test_zoneinfo_resolves(self):
        # Fails loudly if the system is missing tzdata for the IANA tz.
        self.assertEqual(EDITORIAL_TZ, ZoneInfo("America/New_York"))


if __name__ == "__main__":
    unittest.main()
