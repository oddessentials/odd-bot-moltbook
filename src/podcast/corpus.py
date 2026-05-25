"""Load + summarise the eligible podcast corpus from data/briefs.json.

Filter applied to every brief read from data/briefs.json:

  - `briefs.status == "published"` AND `briefs.id` matches the daily
    regex (excludes the grandfathered weekly artifact 2026-W18).
  - **Rolling bi-weekly window** (2026-05-25 correction): when at
    least one episode has been published, only briefs whose `date`
    lies in `[window_end - 13 days, window_end]` qualify, where
    `window_end` is the run's local editorial date (an explicit
    `run_date` parameter; defaults to `datetime.now(EDITORIAL_TZ).date()`).
    Missed publish windows are accepted losses per
    `feedback_accepted_single_day_miss.md` — they MUST NOT accumulate
    backward into the next successful run.
  - **Cold-start carve-out**: when `episodes_path` is `None`, missing
    on disk, or contains an empty list, no window filter is applied —
    the Episode 1 case uses the full eligible corpus.

The window is computed deterministically from the run's editorial
date so the test surface can pin a value and assert exact bounds.

Supersedes the 2026-05-21 design (PR #25), which keyed the floor on
the most-recent published episode's date and therefore expanded the
window unboundedly across failed fires.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

from src.editorial_time import EDITORIAL_TZ

from .config import BRIEFS_PATH, DAILY_ID, EPISODES_PUBLIC_PATH
from .schema import BriefSummary


WINDOW_DAYS = 14


def _has_prior_episodes(episodes_path: Path | None) -> bool:
    if episodes_path is None or not episodes_path.exists():
        return False
    eps = json.loads(episodes_path.read_text())
    return isinstance(eps, list) and bool(eps)


def load_eligible_corpus(
    briefs_path: Path = BRIEFS_PATH,
    episodes_path: Path | None = EPISODES_PUBLIC_PATH,
    run_date: date | None = None,
) -> list[BriefSummary]:
    """Return published daily-shape briefs inside the bi-weekly window
    ending at `run_date`, sorted ascending by id."""
    raw = json.loads(briefs_path.read_text())
    if not isinstance(raw, list):
        raise ValueError(f"{briefs_path} is not a JSON list (got {type(raw).__name__})")

    window_start: date | None = None
    window_end: date | None = None
    if _has_prior_episodes(episodes_path):
        if run_date is None:
            run_date = datetime.now(EDITORIAL_TZ).date()
        window_end = run_date
        window_start = window_end - timedelta(days=WINDOW_DAYS - 1)

    out: list[BriefSummary] = []
    for r in raw:
        if r.get("status") != "published":
            continue
        bid = r.get("id", "")
        if not DAILY_ID.match(bid):
            continue
        if window_start is not None:
            try:
                brief_date = date.fromisoformat(r["date"])
            except (KeyError, ValueError, TypeError):
                continue
            if brief_date < window_start or brief_date > window_end:
                continue
        out.append(
            BriefSummary(
                id=bid,
                issue_no=int(r["issueNo"]),
                date=r["date"],
                title=r["title"],
                dek=r["dek"],
                items=tuple(r.get("items", [])),
            )
        )
    out.sort(key=lambda b: b.id)
    return out


def summarize_corpus(corpus: Iterable[BriefSummary]) -> str:
    lines = []
    for b in corpus:
        lines.append(f"  - {b.id} (issue {b.issue_no}): {b.title!r} — {len(b.items)} items")
    return "\n".join(lines)
