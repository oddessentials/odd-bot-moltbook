"""Ingestion + filter/rank for odd-bot-moltbook.

Pluggable adapter at the head of the daily pipeline.

- v1 (current): HF-dataset replay against the Jan 2026 Moltbook snapshot.
- v2 (when the observer-agent key arrives): live Moltbook API. Add
  `src/moltbook_client.py`, then route `fetch_window(source="live-api")`
  through it. No other code changes downstream.

Downstream (filter/rank, scrub, synthesize, persist) is mode-agnostic.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import duckdb
import yaml

HF_DATASET_ID = "ronantakizawa/moltbook"
HF_SNAPSHOT_TAG = "dataset:hf-jan-2026"

DEFAULT_TOP_N = 50

# Per-post engagement threshold. Posts with engagement < MIN_ENGAGEMENT are
# dropped before ranking. Set to 1 in v1 — drops only zero-engagement noise.
MIN_ENGAGEMENT = 1

# Hard ceiling on `content` size at normalization. Truncates the field to
# bound downstream prompt size; chosen empirically for v1.
MAX_CONTENT_LEN = 50_000


def fetch_window(
    window_start: datetime,
    window_end: datetime,
    source: str = "hf-snapshot",
    config: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Pluggable ingestion. Returns (posts, metadata).

    Metadata is empty for `hf-snapshot`. For `live-api` it carries
    `{"chosen_submolts": [...]}` so the orchestrator can persist the
    day's selection in the run record per PLAN §4 step 5.

    `config` is required for `live-api` (provides `top_n`/`exclude`/
    `mandatory` for the submolt selection algorithm). It is ignored
    for `hf-snapshot`.
    """
    if source == "hf-snapshot":
        return _fetch_window_hf(window_start, window_end), {}
    if source == "live-api":
        if config is None:
            raise ValueError(
                "live-api ingestion requires a submolts config (top_n, "
                "exclude, mandatory); pass config= from `load_config(...)`"
            )
        # Lazy import so poll.py stays importable without the live-API
        # adapter being installed (e.g., HF-only environments).
        from src.moltbook_client import fetch_window_live
        return fetch_window_live(window_start, window_end, config)
    raise ValueError(f"unknown source: {source!r}")


def _fetch_window_hf(window_start: datetime, window_end: datetime) -> list[dict[str, Any]]:
    """Fetch posts from the HF snapshot whose created_at falls in the window.

    Per-row tolerance: if `_normalize_post` raises (missing required fields,
    wrong types, empty strings, unparseable timestamp), the row is skipped
    and a WARN line is written to stderr. `_normalize_post` itself stays
    strict — the tolerance lives only here.

    Schema-wide drift detection: if every row in the dataset fails
    normalization (`skipped == total` and `total > 0`), this raises
    `RuntimeError("schema-wide drift: all rows failed normalization")`.
    The distinct exception type (RuntimeError, not ValueError) ensures
    it bypasses `run_daily`'s empty-filter ValueError catch — schema-wide
    drift fails the run loudly, exits non-zero, and writes NO run record.
    A normal empty day (some rows succeed, none in window) still flows
    through the empty-filter path.
    """
    rows = _load_hf_dataset()
    total = len(rows)
    skipped = 0
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            post = _normalize_post(row)
        except (KeyError, TypeError, ValueError) as e:
            row_id = row.get("id", "<missing-id>")
            print(f"WARN: skipping {row_id}: {e}", file=sys.stderr)
            skipped += 1
            continue
        if window_start <= _parse_ts(post["created_at"]) < window_end:
            out.append(post)

    if total > 0 and skipped == total:
        raise RuntimeError("schema-wide drift: all rows failed normalization")

    return out


@lru_cache(maxsize=1)
def _load_hf_dataset() -> tuple[dict[str, Any], ...]:
    try:
        from datasets import load_dataset
    except ImportError as e:
        raise RuntimeError(
            "the 'datasets' package is required for v1 (HF replay); "
            "install via `pip install -e .` from the repo root"
        ) from e
    # The dataset has two configs (posts, submolts); we only consume posts.
    ds = load_dataset(HF_DATASET_ID, "posts", split="train")
    return tuple(dict(row) for row in ds)


def _normalize_post(row: dict[str, Any]) -> dict[str, Any]:
    """Map an HF dataset row to the canonical Moltbook post shape.

    Schema verified 2026-04-27 against
    `huggingface.co/datasets/ronantakizawa/moltbook` (config=`posts`,
    split=`train`). Column names match the API spec exactly — no aliasing.

    Source column           -> Target field
    -----------------------    --------------------
    id            (string)  -> id
    title         (string)  -> title
    content       (string)  -> content (truncated to MAX_CONTENT_LEN)
    upvotes       (int64)   -> upvotes
    downvotes     (int64)   -> downvotes
    comment_count (int64)   -> comment_count
    submolt       (string)  -> submolt
    author        (string)  -> author
    created_at    (string)  -> created_at (canonical UTC ISO 8601)

    Dataset also has `post_url` and `score` columns; not used.

    Validation (every failure path names the offending row id for
    debugging dataset drift):
      - Required fields present (raises KeyError).
      - Counts (upvotes/downvotes/comment_count) are ints, not bools
        (raises TypeError).
      - Text fields (id/title/content/submolt/author/created_at) are
        non-empty strings (raises ValueError).
      - created_at parses; output is canonical UTC ISO 8601 (raises
        ValueError if unparseable).
      - content is truncated to MAX_CONTENT_LEN characters (deterministic
        — no error, no marker appended).
    """
    # Capture id early so error messages can reference it even when the
    # row is malformed beyond the id field itself.
    row_id_ctx = row.get("id", "<missing-id>")

    required = (
        "id", "title", "content", "upvotes", "downvotes",
        "comment_count", "submolt", "author", "created_at",
    )
    missing = [k for k in required if k not in row]
    if missing:
        raise KeyError(
            f"HF row {row_id_ctx!r} missing required fields {missing}; "
            f"row keys: {list(row.keys())}"
        )

    for k in ("upvotes", "downvotes", "comment_count"):
        # bool is a subclass of int in Python — exclude it explicitly.
        if not isinstance(row[k], int) or isinstance(row[k], bool):
            raise TypeError(
                f"HF row {row_id_ctx!r} field {k!r} must be int, "
                f"got {type(row[k]).__name__}"
            )

    for k in ("id", "title", "content", "submolt", "author", "created_at"):
        if not isinstance(row[k], str) or not row[k].strip():
            raise ValueError(
                f"HF row {row_id_ctx!r} field {k!r} must be non-empty str, "
                f"got {row[k]!r}"
            )

    raw_ts = row["created_at"]
    if raw_ts.endswith("Z"):
        raw_ts = raw_ts[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw_ts)
    except ValueError as e:
        raise ValueError(
            f"HF row {row_id_ctx!r} created_at unparseable: {row['created_at']!r}"
        ) from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    canonical_created_at = dt.isoformat()

    content = row["content"]
    if len(content) > MAX_CONTENT_LEN:
        content = content[:MAX_CONTENT_LEN]

    return {
        "id": row["id"],
        "title": row["title"],
        "content": content,
        "upvotes": row["upvotes"],
        "downvotes": row["downvotes"],
        "comment_count": row["comment_count"],
        "submolt": row["submolt"],
        "author": row["author"],
        "created_at": canonical_created_at,
    }


def _parse_ts(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _engagement(post: dict[str, Any]) -> int:
    return post["upvotes"] - post["downvotes"] + post["comment_count"]


def filter_and_rank(posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Dedupe, drop low-signal, sort by engagement, take top-N.

    Steps in order:
      1. Dedupe by `id` keeping the highest-engagement copy. Stable on ties
         (first occurrence wins when engagement is equal).
      2. Drop posts with engagement < MIN_ENGAGEMENT.
      3. Assert the post-threshold list is non-empty. If empty, raise
         `ValueError("filter produced zero posts; upstream drift")`.
         `run_daily` catches this and writes an empty-window run record.
         The assertion surfaces upstream drift (no rows in window OR all
         rows below threshold) instead of silently producing nothing.
      4. Sort by engagement DESC, `id` ASC tiebreak. Deterministic.
      5. Return the top DEFAULT_TOP_N.

    Engagement formula (locked, per PLAN §5):
        engagement(p) = p["upvotes"] - p["downvotes"] + p["comment_count"]

    No config inputs. `submolts.yaml` `exclude`/`mandatory` are reserved
    for v2 submolt selection (live API), not post-level filtering.
    """
    by_id: dict[str, dict[str, Any]] = {}
    for p in posts:
        existing = by_id.get(p["id"])
        if existing is None or _engagement(p) > _engagement(existing):
            by_id[p["id"]] = p
    deduped = list(by_id.values())

    filtered = [p for p in deduped if _engagement(p) >= MIN_ENGAGEMENT]

    if not filtered:
        raise ValueError("filter produced zero posts; upstream drift")

    filtered.sort(key=lambda p: (-_engagement(p), p["id"]))
    return filtered[:DEFAULT_TOP_N]


def persist_raw(posts: list[dict[str, Any]], run_id: str, db_path: Path) -> None:
    """Write the full merged set to `posts_raw` for replayability."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS posts_raw (
                run_id VARCHAR,
                post_id VARCHAR,
                payload JSON,
                PRIMARY KEY (run_id, post_id)
            )
            """
        )
        con.executemany(
            "INSERT OR REPLACE INTO posts_raw VALUES (?, ?, ?)",
            [(run_id, p["id"], json.dumps(p)) for p in posts],
        )
    finally:
        con.close()


def load_config(config_path: Path) -> dict[str, Any]:
    return yaml.safe_load(config_path.read_text()) or {}
