"""Live Moltbook API ingestion adapter (v2).

Activated on observer-agent key arrival. Same downstream pipeline as v1
HF replay — `_normalize_post_live` produces post dicts in the canonical
Moltbook shape (PLAN §3) so `poll.filter_and_rank`,
`summarize.scrub_post`, and everything downstream are mode-agnostic.

Coverage of PLAN's v2 contract (γ — full v2 minus identity token):
  §3, §4 (this module):
    1. GET /api/v1/submolts?sort=popular&limit=20 — popularity refresh.
    2. Submolt selection: filter by `exclude`, take top `top_n`, union
       with `mandatory` from `config/submolts.yaml`.
    3. GET /api/v1/posts?sort=top&time=day&limit=100 — global top.
    4. GET /api/v1/posts?submolt={name}&sort=top&time=day&limit=100
       per chosen submolt.
    5. Merge global + per-submolt, dedupe by `id`, filter by window.
  §4 step 5 (orchestrator caller):
    chosen_submolts list returned alongside posts so `run_daily` can
    persist it in `runs.jsonl`.

Identity token (PLAN §5 step 1) is intentionally NOT implemented —
"reserved for write/verified actions"; the engine is read-only.

Verified API shape (2026-04-27):
  /posts response top-level:    {success, posts, has_more, next_cursor}
  /submolts response top-level: {success, submolts, count, total_posts}
  Per-post fields (canonical adaptation map below).
  Per-submolt fields used: `name` only.

Auth:
  `~/.openclaw/keys/moltbook-api-key` (chmod 600). Lazy-loaded; held
  only in this process's heap. Never written to env, never logged,
  never propagated to subprocesses.

Rate limits (verified 2026-04-27):
  200 reads / 60s. Headers lowercase: x-ratelimit-{limit,remaining,reset}.
  Daily call budget worst-case ~10 calls (1 submolts + 1 global + N
  per-submolt where N <= top_n + len(mandatory)) — well under ceiling.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

API_BASE = "https://www.moltbook.com/api/v1"
USER_AGENT = "odd-bot-moltbook/0.1 (read-only observer)"
MAX_LIMIT = 100
SUBMOLTS_OVERSAMPLE_LIMIT = 20  # PLAN §4 step 1 — oversample to absorb exclusions


@lru_cache(maxsize=1)
def _api_key() -> str:
    """Lazy, process-local Moltbook observer key.

    Reads from `~/.openclaw/keys/moltbook-api-key` on first call.
    Never written to env, never logged, never propagated to children.
    """
    return (Path.home() / ".openclaw" / "keys" / "moltbook-api-key").read_text().strip()


def fetch_window_live(
    window_start: datetime,
    window_end: datetime,
    config: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Orchestrate v2 ingestion: submolt selection + global + per-submolt.

    Per PLAN §3, §4:
      1. Fetch popular submolts (oversample to limit=20).
      2. Apply selection algorithm against `config` (`exclude`, `top_n`,
         `mandatory`) → `chosen_submolts`.
      3. Fetch global top.
      4. For each chosen submolt, fetch its top-of-day.
      5. Merge, dedupe by `id`, filter by `window_start <= created_at < window_end`.

    Returns:
        `(posts, metadata)` where `metadata = {"chosen_submolts": [...]}`
        for the orchestrator (`run_daily`) to merge into the run record.
    """
    popular = _fetch_popular_submolts()
    chosen = _select_submolts(config, popular)

    posts: list[dict[str, Any]] = []
    posts.extend(_fetch_global_top())
    for submolt_name in chosen:
        posts.extend(_fetch_posts_in_submolt(submolt_name))

    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for p in posts:
        if p["id"] in seen:
            continue
        seen.add(p["id"])
        deduped.append(p)

    in_window: list[dict[str, Any]] = []
    for p in deduped:
        try:
            ts = p["created_at"]
            iso_ts = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
            dt = datetime.fromisoformat(iso_ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            print(
                f"WARN: skipping {p.get('id', '<missing-id>')}: created_at unparseable",
                file=sys.stderr,
            )
            continue
        if window_start <= dt < window_end:
            in_window.append(p)

    return in_window, {"chosen_submolts": chosen}


def _fetch_popular_submolts() -> list[dict[str, Any]]:
    """GET /api/v1/submolts?sort=popular&limit=20 → raw submolt dicts.

    Returns the API's `submolts` array as-is. Selection logic applies
    to a dict's `name` field (verified shape).
    """
    raw = _api_get(
        "/submolts",
        {"sort": "popular", "limit": SUBMOLTS_OVERSAMPLE_LIMIT},
    )
    submolts = raw.get("submolts")
    if not isinstance(submolts, list):
        raise ValueError(
            f"unexpected /submolts response shape; top-level keys: "
            f"{list(raw.keys()) if isinstance(raw, dict) else type(raw).__name__}"
        )
    return submolts


def _select_submolts(
    config: dict[str, Any],
    popular: list[dict[str, Any]],
) -> list[str]:
    """Apply PLAN §4 selection: filter exclude → take top_n → union mandatory.

    Reads `top_n`, `exclude`, `mandatory` from config exactly as defined
    in `config/submolts.yaml`. No reinterpretation.

    Mandatory entries are appended after the dynamic top-N (preserving
    config ordering), deduped to keep the ordering deterministic.
    """
    top_n = int(config.get("top_n") or 0)
    exclude = set(config.get("exclude") or [])
    mandatory = list(config.get("mandatory") or [])

    dynamic: list[str] = []
    for s in popular:
        if not isinstance(s, dict):
            continue
        name = s.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        if name in exclude:
            continue
        dynamic.append(name)
        if len(dynamic) >= top_n:
            break

    chosen: list[str] = list(dynamic)
    for m in mandatory:
        if m not in chosen:
            chosen.append(m)
    return chosen


def _fetch_global_top() -> list[dict[str, Any]]:
    """GET /api/v1/posts?sort=top&time=day&limit=100 → normalized posts."""
    return _fetch_and_normalize_posts({"sort": "top", "time": "day", "limit": MAX_LIMIT})


def _fetch_posts_in_submolt(submolt_name: str) -> list[dict[str, Any]]:
    """GET /api/v1/posts?submolt={name}&sort=top&time=day&limit=100 → normalized."""
    return _fetch_and_normalize_posts(
        {"submolt": submolt_name, "sort": "top", "time": "day", "limit": MAX_LIMIT}
    )


def _fetch_and_normalize_posts(params: dict[str, Any]) -> list[dict[str, Any]]:
    """Internal helper: GET /posts with params, normalize each row.

    Per-row tolerance: rows with `is_deleted` or `is_spam` are dropped
    silently; rows that fail validation are skipped with a stderr WARN.
    Schema-wide drift (every row failed) raises RuntimeError — same
    contract as `_fetch_window_hf` in poll.py.
    """
    raw = _api_get("/posts", params)
    posts = raw.get("posts")
    if not isinstance(posts, list):
        raise ValueError(
            f"unexpected /posts response shape; top-level keys: "
            f"{list(raw.keys()) if isinstance(raw, dict) else type(raw).__name__}"
        )

    total = 0
    skipped = 0
    out: list[dict[str, Any]] = []
    for api_row in posts:
        total += 1
        if not isinstance(api_row, dict):
            print(f"WARN: skipping non-dict row at index {total - 1}", file=sys.stderr)
            skipped += 1
            continue
        if api_row.get("is_deleted") or api_row.get("is_spam"):
            continue
        try:
            post = _normalize_post_live(api_row)
        except (KeyError, TypeError, ValueError) as e:
            row_id = api_row.get("id", "<missing-id>")
            print(f"WARN: skipping {row_id}: {e}", file=sys.stderr)
            skipped += 1
            continue
        out.append(post)

    if total > 0 and skipped == total:
        raise RuntimeError("schema-wide drift: all rows failed normalization")

    return out


def _normalize_post_live(row: dict[str, Any]) -> dict[str, Any]:
    """Map a live-API post row to the canonical Moltbook post shape.

    Schema verified 2026-04-27 against the live API. Author and submolt
    arrive as nested dicts; we explicitly extract their `name` fields
    to produce a string (matches HF dataset shape). No fallbacks, no
    aliasing — every field path is named below.

    Source field          -> Target field
    --------------------     --------------------
    id            (str)   -> id
    title         (str)   -> title
    content       (str)   -> content
    upvotes       (int)   -> upvotes
    downvotes     (int)   -> downvotes
    comment_count (int)   -> comment_count
    submolt.name  (str)   -> submolt
    author.name   (str)   -> author
    created_at    (str)   -> created_at

    Live-only fields (`author_id`, `score`, `hot_score`, `type`,
    `is_deleted`, `is_locked`, `is_pinned`, `is_spam`,
    `verification_status`, `updated_at`) are not used.

    Raises with row id context: KeyError on missing fields, TypeError
    on wrong types, ValueError on empty/whitespace strings.
    """
    row_id_ctx = row.get("id", "<missing-id>")

    required = (
        "id", "title", "content", "upvotes", "downvotes",
        "comment_count", "submolt", "author", "created_at",
    )
    missing = [k for k in required if k not in row]
    if missing:
        raise KeyError(
            f"live row {row_id_ctx!r} missing required fields {missing}"
        )

    for k in ("upvotes", "downvotes", "comment_count"):
        if not isinstance(row[k], int) or isinstance(row[k], bool):
            raise TypeError(
                f"live row {row_id_ctx!r} field {k!r} must be int, "
                f"got {type(row[k]).__name__}"
            )

    for k in ("id", "title", "content", "created_at"):
        if not isinstance(row[k], str) or not row[k].strip():
            raise ValueError(
                f"live row {row_id_ctx!r} field {k!r} must be non-empty str, "
                f"got {row[k]!r}"
            )

    submolt_dict = row["submolt"]
    if not isinstance(submolt_dict, dict):
        raise TypeError(
            f"live row {row_id_ctx!r} field 'submolt' must be dict, "
            f"got {type(submolt_dict).__name__}"
        )
    submolt_name = submolt_dict.get("name")
    if not isinstance(submolt_name, str) or not submolt_name.strip():
        raise ValueError(
            f"live row {row_id_ctx!r} submolt.name must be non-empty str, "
            f"got {submolt_name!r}"
        )

    author_dict = row["author"]
    if not isinstance(author_dict, dict):
        raise TypeError(
            f"live row {row_id_ctx!r} field 'author' must be dict, "
            f"got {type(author_dict).__name__}"
        )
    author_name = author_dict.get("name")
    if not isinstance(author_name, str) or not author_name.strip():
        raise ValueError(
            f"live row {row_id_ctx!r} author.name must be non-empty str, "
            f"got {author_name!r}"
        )

    return {
        "id": row["id"],
        "title": row["title"],
        "content": row["content"],
        "upvotes": row["upvotes"],
        "downvotes": row["downvotes"],
        "comment_count": row["comment_count"],
        "submolt": submolt_name,
        "author": author_name,
        "created_at": row["created_at"],
    }


def _api_get(path: str, params: dict[str, Any]) -> dict[str, Any]:
    """GET against the Moltbook API with auth header.

    One retry on 429 honoring `Retry-After`. All other non-2xx
    responses propagate as `urllib.error.HTTPError`.
    """
    url = f"{API_BASE}{path}?{urllib.parse.urlencode(params)}"
    headers = {
        "Authorization": f"Bearer {_api_key()}",
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    req = urllib.request.Request(url, headers=headers)

    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0:
                retry_after = int(e.headers.get("Retry-After", "60"))
                print(
                    f"WARN: 429 from {path}; sleeping {retry_after}s",
                    file=sys.stderr,
                )
                time.sleep(retry_after)
                continue
            raise
    raise RuntimeError("unreachable")
