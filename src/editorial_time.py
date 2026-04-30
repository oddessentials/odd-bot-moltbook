"""Editorial-time contract for moltbook publish pipelines.

The publish eligibility contract is anchored to local clock time in
`America/New_York` — not UTC. Daily briefs publish at 05:00 local;
weekly podcast episodes publish on Sunday at 09:00 local.

This module is the single source of truth for that contract. Both the
daily orchestrator (`src/publish.py`) and the weekly podcast wrapper
(`scripts/run-weekly-podcast.sh`) call into it so a Mac mini reboot
that fires `RunAtLoad` after UTC has rolled past midnight cannot
prematurely publish the next local-day's content.

Pure functions only — no I/O. `datetime` inputs are timezone-aware
UTC; outputs are local dates and bool gates. Tests in
`tests/test_editorial_time.py`.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

EDITORIAL_TZ = ZoneInfo("America/New_York")

# Daily brief publish window opens at 05:00 America/New_York.
DAILY_WINDOW_HOUR = 5

# Weekly podcast publish window opens at 09:00 America/New_York every Sunday.
# `datetime.weekday()` numbers Monday=0..Sunday=6.
WEEKLY_WINDOW_WEEKDAY = 6
WEEKLY_WINDOW_HOUR = 9


def daily_editorial_state(now_utc: datetime) -> tuple[date, bool]:
    """Return `(today_local, window_open)` for the daily brief contract.

    `today_local` is the calendar date in `America/New_York` at `now_utc`.
    A brief whose id matches `today_local.isoformat()` is the editorial
    target for the current local day.

    `window_open` is True iff the local time has crossed the daily publish
    boundary (05:00 local) for `today_local`. When False, the orchestrator
    must refuse to create or publish a new brief for `today_local`. Past
    dates and reconciliation flows are unaffected.
    """
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")
    now_local = now_utc.astimezone(EDITORIAL_TZ)
    return now_local.date(), now_local.hour >= DAILY_WINDOW_HOUR


def most_recent_weekly_window_date(now_utc: datetime) -> date:
    """Return the local date of the most-recent weekly publish window
    opening at or before `now_utc`.

    The window opens every Sunday at 09:00 America/New_York. If `now_utc`
    is Sunday before 09:00 local, the most-recent window is the *previous*
    Sunday. If Sunday at-or-after 09:00, it is today.
    """
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")
    now_local = now_utc.astimezone(EDITORIAL_TZ)
    candidate = now_local.replace(
        hour=WEEKLY_WINDOW_HOUR, minute=0, second=0, microsecond=0,
    )
    days_back = (candidate.weekday() - WEEKLY_WINDOW_WEEKDAY) % 7
    candidate = candidate - timedelta(days=days_back)
    if candidate > now_local:
        candidate = candidate - timedelta(days=7)
    return candidate.date()


def weekly_window_satisfied(
    now_utc: datetime, latest_publish_date: date | None,
) -> bool:
    """Return True iff the current weekly publish window has already been
    filled by `latest_publish_date`.

    The wrapper should REFUSE the run when this returns True. When False,
    the wrapper may proceed (subject to its other guards). `None` means
    "no episode has ever published" — never satisfied; first publish is
    eligible as soon as a window has opened.
    """
    if latest_publish_date is None:
        return False
    return latest_publish_date >= most_recent_weekly_window_date(now_utc)
