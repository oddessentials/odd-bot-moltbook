"""Load + summarise the eligible Episode 1 corpus from data/briefs.json.

Filter mirrors plan §Locked decisions exactly: status == "published" AND
id matches the daily-shape regex. The grandfathered weekly artifact
2026-W18 is excluded by the id-shape filter.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from .config import BRIEFS_PATH, DAILY_ID
from .schema import BriefSummary


def load_eligible_corpus(briefs_path: Path = BRIEFS_PATH) -> list[BriefSummary]:
    """Return every published daily-shape brief, sorted ascending by id."""
    raw = json.loads(briefs_path.read_text())
    if not isinstance(raw, list):
        raise ValueError(f"{briefs_path} is not a JSON list (got {type(raw).__name__})")

    out: list[BriefSummary] = []
    for r in raw:
        if r.get("status") != "published":
            continue
        bid = r.get("id", "")
        if not DAILY_ID.match(bid):
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
