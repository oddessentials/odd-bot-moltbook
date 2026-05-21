"""Load + summarise the eligible podcast corpus from data/briefs.json.

Filter mirrors plan §Locked decisions plus the 2026-05-21 cadence retune:

  - `briefs.status == "published"` AND `briefs.id` matches the daily
    regex (excludes the grandfathered weekly artifact 2026-W18).
  - `brief.date > max(episode.date)` strict-greater. The brief dated
    same-day as the prior episode is content that prior episode already
    covered; the bi-weekly retune (PR #24) made this lack of windowing
    visible by stretching the inter-episode gap.

Cold-start fallback (preserves the Episode 1 carve-out): if
`episodes_path` is None, doesn't exist, or contains an empty list, no
episode-date filter is applied. Production callers pass the default
`EPISODES_PUBLIC_PATH` so the filter is on by default; tests pass
`episodes_path=None` to exercise the legacy/cold-start branch
explicitly.

Selection floor uses max episode `date`, NOT max `episodeNo`, so a
future backfill that inserts an old episode out of order cannot expand
the corpus floor.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Iterable

from .config import BRIEFS_PATH, DAILY_ID, EPISODES_PUBLIC_PATH
from .schema import BriefSummary


def load_eligible_corpus(
    briefs_path: Path = BRIEFS_PATH,
    episodes_path: Path | None = EPISODES_PUBLIC_PATH,
) -> list[BriefSummary]:
    """Return published daily-shape briefs newer than the most-recent
    published episode's date, sorted ascending by id."""
    raw = json.loads(briefs_path.read_text())
    if not isinstance(raw, list):
        raise ValueError(f"{briefs_path} is not a JSON list (got {type(raw).__name__})")

    floor: date | None = None
    if episodes_path is not None and episodes_path.exists():
        eps = json.loads(episodes_path.read_text())
        if isinstance(eps, list) and eps:
            floor = max(date.fromisoformat(e["date"]) for e in eps if e.get("date"))

    out: list[BriefSummary] = []
    for r in raw:
        if r.get("status") != "published":
            continue
        bid = r.get("id", "")
        if not DAILY_ID.match(bid):
            continue
        if floor is not None and date.fromisoformat(r["date"]) <= floor:
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
