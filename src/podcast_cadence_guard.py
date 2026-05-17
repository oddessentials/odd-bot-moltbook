"""Weekly podcast cadence guard — pre-flight decision logic.

Two layered checks, both anchored to America/New_York editorial time:

  1. Days-since: refuse if `today_local - latest.date < min_days`. Cheap
     anti-spam against rapid manual reinvocations (Tuesday reboot after
     Sunday publish must not re-fire).

  2. Weekly window: refuse if not inside an open Sunday-09:00-or-later
     window, or if the current open window has already been filled by
     `latest`. See `src.editorial_time.weekly_window_satisfied`.

Single-line stdout output (matches the legacy bash-inline format —
`scripts/run-weekly-podcast.sh` parses these prefixes):

    PROCEED:no_episodes_json
    PROCEED:empty_episodes
    PROCEED:<days_since>:<latest_id>:<latest_date>
    REFUSE:cadence:<days_since>:<latest_id>:<latest_date>
    REFUSE:window:<days_since>:<latest_id>:<latest_date>

Malformed `data/episodes.json` (missing/unparseable date on the most-
recent entry) raises — bash sees no PROCEED/REFUSE prefix on stdout
and exits 1 via its wildcard case, surfacing the corruption to the
operator.

Entrypoint:

    python -m src.podcast_cadence_guard --min-days 13
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from src.editorial_time import EDITORIAL_TZ, weekly_window_satisfied

REPO_ROOT = Path(__file__).resolve().parent.parent
EPISODES_PATH_DEFAULT = REPO_ROOT / "data" / "episodes.json"


def evaluate(
    *,
    now_utc: datetime,
    latest_id: str | None,
    latest_date: date,
    min_days: int,
) -> str:
    """Compose days-since + weekly-window into a single output line.

    Pure function; no I/O. `now_utc` must be tz-aware. `latest_date`
    is the calendar date of the most-recent published episode.
    """
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")
    today_local = now_utc.astimezone(EDITORIAL_TZ).date()
    days_since = (today_local - latest_date).days
    latest_id_safe = latest_id or "unknown"
    latest_date_str = latest_date.isoformat()

    if days_since < min_days:
        return (
            f"REFUSE:cadence:{days_since}:{latest_id_safe}:{latest_date_str}"
        )
    if weekly_window_satisfied(now_utc, latest_date):
        return (
            f"REFUSE:window:{days_since}:{latest_id_safe}:{latest_date_str}"
        )
    return f"PROCEED:{days_since}:{latest_id_safe}:{latest_date_str}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--min-days", type=int, required=True)
    parser.add_argument(
        "--episodes",
        type=Path,
        default=EPISODES_PATH_DEFAULT,
    )
    args = parser.parse_args(argv)

    if not args.episodes.exists():
        sys.stdout.write("PROCEED:no_episodes_json\n")
        return 0
    entries = json.loads(args.episodes.read_text())
    if not entries:
        sys.stdout.write("PROCEED:empty_episodes\n")
        return 0

    latest = max(entries, key=lambda e: e.get("episodeNo", 0))
    latest_date = date.fromisoformat(latest["date"])
    latest_id = latest.get("id")

    line = evaluate(
        now_utc=datetime.now(timezone.utc),
        latest_id=latest_id,
        latest_date=latest_date,
        min_days=args.min_days,
    )
    sys.stdout.write(line + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
