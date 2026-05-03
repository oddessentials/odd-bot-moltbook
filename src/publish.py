"""Auto-publish orchestrator for odd-bot-moltbook.

Single entry point for the daily pipeline. Replaces the prior manual
`scripts/publish.sh` gate. Invoked by `scripts/run-daily-publish.sh`
under launchd; usable manually by the operator with the same command.

Flow per run:

  1. Acquire process-exclusive filesystem lock (`fcntl.flock`).
     Held → exit 0 silently (sibling running).

  2. Pre-flight: if local commits ahead of origin, push first.
     Push fails → record deferred state, exit 0 WITHOUT entering
     today's flow. This refuses to stack new commits behind an
     unresolved push failure.

  3. Discover work: candidate dates =
        [today − max_backlog + 1, today] ∩ [START_FLOOR, today]
     minus already-published ids in `data/briefs.json`.

  4. Per-date discovery loop, ascending. For each candidate:
        - draft on disk → orphan-promote (no fetch, any date).
        - else if d == today → live-API fetch + synth + persist as draft.
        - else → skip (HC-2: live-API cannot backfill past dates).
     Accumulate proposed merges into an in-memory list. NOTHING in the
     public-surface state (briefs.json, draft-status flips, publish run
     records) is written here — those are deferred until the commit
     pipeline succeeds, so a build/commit failure cannot strand entries
     between local "published" file state and the actual deployed state.

  5. If anything proposed this run:
        a. Snapshot deployed briefs.json (for revert).
        b. Write briefs.json with all proposed merges (Vite reads it).
        c. `pnpm --dir agent-brief build` → /docs/ + 404 fallback.
        d. assert /docs/index.html mtime > build_started_ts.
        e. git add data/briefs.json docs/; git commit.
        f. assert working tree clean after commit.
     If b–f raises, REVERT briefs.json to the deployed snapshot and
     `git checkout -- docs/`. Drafts on disk were never flipped, so
     no further undo needed. run_daily's durable side effects (today's
     summary.json, posts_raw rows, daily run record) are kept — the
     next run reuses them via orphan promotion, no LLM re-call.

  6. After successful commit: flip on-disk draft statuses to published
     and append publish run records. These are post-commit because until
     the deploy snapshot is captured in git, the entries aren't truly
     published.

  7. git push. Failure → record deferred state, exit 0; commit IS the
     local deploy state, push is just remote sync.

  6. Always: write data/.last-run-state.json (telemetry mirror of
     `git rev-list @{u}..@ --count`, the AHEAD-of-remote count).

Invariants (asserted at runtime):
  - live_fetch_invocations <= 1 per run.
  - Working tree clean after commit (or run errors out).
  - /docs/index.html mtime > build_started_ts after build.

Pure (testable in isolation):
  - merge_brief: dedupe by exact id, sort date desc, id desc tiebreak.
    Existing entries this function did not produce (e.g., legacy W18)
    are passed through unchanged — never mutated.
  - discover_work: deterministic candidate set from inputs.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import html
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from src.editorial_time import (
    DAILY_WINDOW_HOUR,
    daily_editorial_state,
    is_daily_window_open_for,
)
from src.git_sync import reconcile_with_origin
from src.summarize import (
    Brief,
    DAILY_SLUG,
    DATA_DIR,
    DIGESTS_DIR,
    REPO_ROOT,
    RUNS_PATH,
    _atomic_write_text,
    append_run_record,
    run_daily,
)


# ---- Paths ----

BRIEFS_PATH = DATA_DIR / "briefs.json"
LOCK_PATH = DATA_DIR / ".run.lock"
RUN_STATE_PATH = DATA_DIR / ".last-run-state.json"
DOCS_PATH = REPO_ROOT / "docs"
DOCS_INDEX = DOCS_PATH / "index.html"
AGENT_BRIEF_DIR = REPO_ROOT / "agent-brief"

# ---- Defaults ----

DEFAULT_MAX_BACKLOG = 3


def _now_utc() -> datetime:
    """Wall-clock UTC. Indirected so tests can patch a single seam to
    advance the clock between `started` capture and the per-date loop —
    the path that closes the "captured-too-early" race.
    """
    return datetime.now(timezone.utc)

# D10-A: hard floor at the cadence-flip date. Prevents the orchestrator
# from ever scanning earlier dates for orphan drafts (e.g., the HF-replay
# 2026-01-30 draft, which is pre-flip provenance and intentionally
# stays as a draft per D5-B).
START_FLOOR = date(2026, 4, 27)

# Canonical site URL — used for per-brief og:url. Mirrors src/post_x.py's
# X_DOMAIN; the duplication is intentional (publish doesn't import post_x).
# If you change one, change both.
SITE_URL = "https://news.oddessentials.ai"

# Pre-compiled patterns for the seven meta tags _render_per_brief_html
# rewrites. Each is attribute-order-insensitive and tolerates `>` vs ` />`
# self-closing styles. Patterns target the SPA template at
# agent-brief/client/index.html — a non-1 match count raises (template
# drift detection).
_TITLE_TAG_RE = re.compile(r"<title[^>]*>[^<]*</title>", re.IGNORECASE)
_OG_TITLE_RE = re.compile(r'<meta[^>]*\bproperty="og:title"[^>]*/?>', re.IGNORECASE)
_OG_DESC_RE = re.compile(r'<meta[^>]*\bproperty="og:description"[^>]*/?>', re.IGNORECASE)
_OG_URL_RE = re.compile(r'<meta[^>]*\bproperty="og:url"[^>]*/?>', re.IGNORECASE)
_OG_TYPE_RE = re.compile(r'<meta[^>]*\bproperty="og:type"[^>]*/?>', re.IGNORECASE)
_TW_TITLE_RE = re.compile(r'<meta[^>]*\bname="twitter:title"[^>]*/?>', re.IGNORECASE)
_TW_DESC_RE = re.compile(r'<meta[^>]*\bname="twitter:description"[^>]*/?>', re.IGNORECASE)


# =============================================================================
# Pure functions — deterministic, side-effect-free
# =============================================================================

def merge_brief(briefs: list[dict], new: dict) -> list[dict]:
    """Merge `new` into `briefs`. Pure.

    Contract:
      - `new["id"]` must match DAILY_SLUG (YYYY-MM-DD). Cadence policy is
        enforced at this boundary — not in the Brief schema, which keeps
        WEEKLY_SLUG support so legacy `2026-W18` continues to validate on
        every read. Reject non-daily `new` entries here so a future code
        path that tries to insert a weekly entry fails fast.
      - Dedupe by exact id match: if `new["id"]` already present, replaced.
        (Retry safety; an in-order run never re-publishes an existing id,
        but if it does, the latest write wins.)
      - Sort by `date` descending; ties broken by `id` descending.
      - Existing entries this function did not insert (e.g., legacy
        `2026-W18` carried forward from the pre-flip era) pass through
        unchanged — never mutated, never reformatted.
    """
    new_id = new.get("id", "")
    if not DAILY_SLUG.match(new_id):
        raise ValueError(
            f"merge_brief: new entries must use daily slug YYYY-MM-DD; "
            f"got id={new_id!r}. Existing weekly entries (e.g., 2026-W18) "
            f"may appear in `briefs` and are passed through, but never "
            f"as `new`."
        )
    out = [b for b in briefs if b.get("id") != new_id]
    out.append(new)
    out.sort(key=lambda b: (b.get("date", ""), b.get("id", "")), reverse=True)
    return out


def discover_work(
    today: date,
    max_backlog: int,
    start_floor: date,
    published_ids: set[str],
) -> list[date]:
    """Compute candidate dates needing work, ascending.

    Window = [today − max_backlog + 1, today], floored at `start_floor`.
    Excludes any date whose ISO form is in `published_ids`.

    Includes both today-fresh-fetch candidates and orphan-draft promotion
    candidates; the orchestrator decides which based on draft existence.
    """
    if max_backlog < 1:
        raise ValueError(f"max_backlog must be >= 1; got {max_backlog}")
    earliest = max(today - timedelta(days=max_backlog - 1), start_floor)
    candidates: list[date] = []
    d = earliest
    while d <= today:
        if d.isoformat() not in published_ids:
            candidates.append(d)
        d += timedelta(days=1)
    return candidates


def _validate_briefs_file(raw: list[dict]) -> None:
    """Reader-side schema validation. Per PLAN §7 'schema validation runs
    at every read.' Fails fast if any entry malforms — refuses to proceed
    on a corrupted source-of-truth.
    """
    for entry in raw:
        Brief(**entry)


def _load_briefs() -> list[dict]:
    if not BRIEFS_PATH.exists():
        return []
    raw = json.loads(BRIEFS_PATH.read_text())
    _validate_briefs_file(raw)
    return raw


# =============================================================================
# Reconciliation — heal local audit state after a prior run committed but
# skipped the post-commit finalization steps (e.g., post-commit clean check
# raised, or machine power loss between commit and flip). Runs BEFORE the
# pre-flight push and any new synthesis so stale local state never persists
# across multiple runs.
# =============================================================================

def _load_publish_record_ids() -> set[str]:
    """Collect Brief IDs that already have an `action: "publish"` record
    in `data/runs.jsonl`. Returns an empty set if the file doesn't exist
    (fresh clone). Tolerates malformed lines — runs.jsonl is append-only
    audit, not load-bearing for orchestrator decisions.
    """
    if not RUNS_PATH.exists():
        return set()
    out: set[str] = set()
    with RUNS_PATH.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("action") == "publish" and rec.get("id"):
                out.add(rec["id"])
    return out


def _reconcile_finalization(briefs: list[dict], dry_run: bool = False) -> None:
    """Self-heal local audit state for already-published Briefs.

    For each daily-slugged published entry in `briefs`:
      - If `data/digests/$id/summary.json` exists with status != "published",
        flip it to published (atomic write). Validates the on-disk payload
        against the Brief schema first; malformed → raise (corruption).
      - If `summary.json` is missing, log INFO and continue (drafts are
        gitignored; on a fresh clone they won't be present).
      - If `data/runs.jsonl` lacks an `action: "publish"` record for the
        id, append a deterministic record (run_id `publish-reconciled-<id>`,
        ts = `<id>T00:00:00+00:00`, `reconciled: true` marker).

    Idempotent: running on already-reconciled state writes nothing.
    Skips non-daily entries (e.g., legacy `2026-W18`) — those can't be
    reconciled (no draft path; pre-existed the daily contract).

    `dry_run=True` reports what would be reconciled without writing. The
    malformed-draft check still raises in dry-run mode (corruption is
    a hard fail regardless of whether we plan to mutate).
    """
    publish_ids_recorded = _load_publish_record_ids()
    prefix = "reconcile (dry-run): would" if dry_run else "reconcile:"

    for entry in briefs:
        brief_id = entry.get("id", "")
        if not DAILY_SLUG.match(brief_id):
            continue
        if entry.get("status") != "published":
            continue

        try:
            d = date.fromisoformat(brief_id)
        except ValueError:
            continue

        # On-disk draft status reconciliation.
        draft_path = _draft_path(d)
        if draft_path.exists():
            try:
                disk_payload = json.loads(draft_path.read_text())
                Brief(**disk_payload)
            except Exception as e:
                # Local audit state is present but corrupt. Refuse to
                # proceed — operator must investigate before next run.
                # Raised in dry-run mode too: corruption is a hard fail
                # whether we plan to mutate or just report.
                raise RuntimeError(
                    f"reconcile: malformed draft at {draft_path}: {e}; "
                    "audit state is corrupt — investigate before next run"
                )
            if disk_payload.get("status") != "published":
                if dry_run:
                    print(f"{prefix} flip {brief_id} draft → published on disk")
                else:
                    published_payload = {**disk_payload, "status": "published"}
                    _atomic_write_text(
                        draft_path,
                        json.dumps(published_payload, indent=2) + "\n",
                    )
                    print(f"{prefix} flipped {brief_id} draft → published on disk")
        else:
            # Drafts are gitignored; absence is normal post-clone.
            print(f"reconcile: {brief_id} draft missing on disk (skipped — INFO)")

        # Run-record reconciliation.
        if brief_id not in publish_ids_recorded:
            if dry_run:
                print(f"{prefix} append publish record for {brief_id}")
            else:
                append_run_record({
                    "run_id": f"publish-reconciled-{brief_id}",
                    "action": "publish",
                    "id": brief_id,
                    "date": brief_id,
                    "ts": f"{brief_id}T00:00:00+00:00",
                    "reconciled": True,
                })
                print(f"{prefix} appended publish record for {brief_id}")
            # Mark as recorded in the in-memory set so subsequent
            # iterations don't double-report (idempotent across the loop).
            publish_ids_recorded.add(brief_id)


# =============================================================================
# Lock — fcntl.flock; auto-released on close (process exit)
# =============================================================================

@contextlib.contextmanager
def acquire_lock(path: Path = LOCK_PATH) -> Iterator[None]:
    """Process-exclusive non-blocking flock.

    Yields on acquisition; raises `BlockingIOError` if the lock is held
    by another process. Caller is expected to catch BlockingIOError at
    the CLI boundary and exit 0 (sibling run in progress).

    Lock is auto-released on close (which fires either through the
    finally block on normal exit OR via the kernel on process death).
    No stale-lock recovery needed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        # Non-blocking exclusive lock. BlockingIOError on contention
        # propagates to the caller; finally still runs to close fd.
        # Do NOT close fd here on the BlockingIOError path — that would
        # double-close (finally also closes), leaking an EBADF that
        # masks the real BlockingIOError at the caller.
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass  # never acquired (e.g., on the BlockingIOError path)
        try:
            os.close(fd)
        except OSError:
            pass  # defensive — fd should always be valid here


# =============================================================================
# Run-state telemetry — `data/.last-run-state.json`
# =============================================================================

def _write_run_state(state: dict) -> None:
    _atomic_write_text(RUN_STATE_PATH, json.dumps(state, indent=2) + "\n")


# =============================================================================
# Git operations
# =============================================================================

def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(REPO_ROOT),
        check=check,
        capture_output=True,
        text=True,
    )


def _commits_ahead() -> int:
    """`git rev-list @{u}..@ --count`. Returns 0 if upstream not configured."""
    try:
        result = _git("rev-list", "@{u}..@", "--count", check=True)
    except subprocess.CalledProcessError:
        return 0
    try:
        return int(result.stdout.strip())
    except ValueError:
        return 0


def _head_sha() -> str:
    return _git("rev-parse", "HEAD").stdout.strip()


def _working_tree_clean() -> bool:
    return _git("status", "--porcelain").stdout.strip() == ""


def _try_push() -> tuple[bool, str]:
    """Attempt `git push`. Returns (success, classification).

    Classification on failure: "deferred-network" | "deferred-auth" |
    "deferred-conflict". Network is the default since transient
    connectivity is the most common reason and the safest assumption
    (next run retries before doing daily work).
    """
    proc = _git("push", check=False)
    if proc.returncode == 0:
        return True, "ok"
    err = (proc.stderr or "").lower()
    if "non-fast-forward" in err or "rejected" in err or "fetch first" in err:
        return False, "deferred-conflict"
    if "authentication" in err or "permission denied" in err or "could not read" in err:
        return False, "deferred-auth"
    return False, "deferred-network"


# =============================================================================
# Build invocation
# =============================================================================

def _render_per_brief_html(template_html: str, brief: dict) -> str:
    """Rewrite the SPA index.html template with brief-specific OG/Twitter meta.

    Pure (no I/O). Required brief fields: id (str), title (str), dek (str),
    issueNo (int). Returns a new HTML string; template_html is not mutated.

    Raises RuntimeError if any of the seven targeted tags doesn't match
    exactly once in the template — drift detection so a future Vite/SPA
    change that strips or duplicates Card meta surfaces here, not silently
    as broken X cards on the live site.

    Per-brief rewrites:
      <title>          → "{title} — Agent Brief Daily"
      og:title         → brief.title (HTML-escaped)
      og:description   → "Issue {issueNo} · {dek}" (dek HTML-escaped)
      og:url           → SITE_URL/brief/{id}
      og:type          → "article" (was "website")
      twitter:title    → brief.title (HTML-escaped)
      twitter:description → "Issue {issueNo} · {dek}" (dek HTML-escaped)

    Static across all per-brief pages (NOT touched here):
      og:image, og:image:width, og:image:height, og:site_name,
      twitter:card, twitter:image, all favicon/theme tags.
    """
    title = html.escape(brief["title"], quote=True)
    dek_escaped = html.escape(brief["dek"], quote=True)
    issue_no = brief["issueNo"]
    description = f"Issue {issue_no} · {dek_escaped}"
    canonical_url = f"{SITE_URL}/brief/{brief['id']}"

    rewrites: list[tuple[re.Pattern[str], str, str]] = [
        (_TITLE_TAG_RE,
         f"<title>{title} — Agent Brief Daily</title>",
         "<title>"),
        (_OG_TITLE_RE,
         f'<meta property="og:title" content="{title}" />',
         'meta property="og:title"'),
        (_OG_DESC_RE,
         f'<meta property="og:description" content="{description}" />',
         'meta property="og:description"'),
        (_OG_URL_RE,
         f'<meta property="og:url" content="{canonical_url}" />',
         'meta property="og:url"'),
        (_OG_TYPE_RE,
         '<meta property="og:type" content="article" />',
         'meta property="og:type"'),
        (_TW_TITLE_RE,
         f'<meta name="twitter:title" content="{title}" />',
         'meta name="twitter:title"'),
        (_TW_DESC_RE,
         f'<meta name="twitter:description" content="{description}" />',
         'meta name="twitter:description"'),
    ]

    out = template_html
    for pattern, replacement, label in rewrites:
        new_out, count = pattern.subn(replacement, out)
        if count != 1:
            raise RuntimeError(
                f"per-brief HTML render: expected exactly one {label} tag "
                f"in template; got {count}. SPA template may have drifted — "
                f"check agent-brief/client/index.html"
            )
        out = new_out
    return out


def _run_build(build_started_ts: float, briefs: list[dict]) -> None:
    """Invoke `pnpm --dir agent-brief build`. Verify /docs/index.html refreshed.

    After the SPA build succeeds, emit per-brief static HTML at
    docs/brief/<id>/index.html for every entry in `briefs`. These files
    override GitHub Pages' SPA fallback for crawler requests at
    /brief/<id>, so X.com's Card crawler reads brief-specific og:title
    and og:description and renders a per-tweet card. Vite's emptyOutDir
    wiped the prior /docs/brief/ tree before this call, so emission
    regenerates cleanly from briefs.json — the source of truth.
    """
    subprocess.run(
        ["pnpm", "--dir", str(AGENT_BRIEF_DIR), "build"],
        cwd=str(REPO_ROOT),
        check=True,
    )
    if not DOCS_INDEX.exists():
        raise RuntimeError(
            f"build completed but {DOCS_INDEX} is missing — "
            "Vite outDir misconfigured"
        )
    if DOCS_INDEX.stat().st_mtime <= build_started_ts:
        raise RuntimeError(
            f"build completed but {DOCS_INDEX} mtime not refreshed — "
            "stale artifact suspected"
        )

    template_html = DOCS_INDEX.read_text()
    _emit_per_brief_pages(briefs, template_html, DOCS_PATH)
    _emit_per_episode_pages(template_html, DOCS_PATH)


def _emit_per_episode_pages(template_html: str, docs_root: Path) -> list[str]:
    """Emit `docs_root/podcast/<id>/index.html` for each entry in
    data/episodes.json.

    Mirrors `_emit_per_brief_pages` for the podcast surface. Vite's
    emptyOutDir wiped the entire `docs_root` tree before this build, so
    every per-episode OG page must be re-emitted on every daily run —
    otherwise the artifacts the engine wrote during a previous podcast
    publish disappear and X.com / search-engine crawlers fall back to
    the SPA shell with generic site-level meta.

    Reads the engine's published list at data/episodes.json. Skips
    silently if the file is missing or empty (no episodes published
    yet → nothing to emit). Returns the ordered list of episode ids
    actually emitted.

    Reaches across module boundaries to import `render_episode_og_html`
    from `src.podcast.og`. The boundary cross is intentional: the daily
    publish IS the rebuild step that owns docs/, and re-emitting episode
    OGs from this single point keeps the persistence invariant local to
    one function rather than scattered across both pipelines.
    """
    from src.podcast.og import render_episode_og_html
    from src.podcast.schema import EpisodeRecord

    episodes_path = DATA_DIR / "episodes.json"
    if not episodes_path.exists():
        return []
    try:
        raw = json.loads(episodes_path.read_text())
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list) or not raw:
        return []

    emitted: list[str] = []
    for entry in raw:
        try:
            record = EpisodeRecord.model_validate(entry)
        except Exception:
            # A malformed entry in episodes.json should not block the
            # rest. The publish-event writer's gate G2 is the authority
            # for shape; this is best-effort re-emission.
            continue
        rendered = render_episode_og_html(template_html, record)
        out_path = docs_root / "podcast" / record.id / "index.html"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered)
        emitted.append(record.id)
    return emitted


def _emit_per_brief_pages(
    briefs: list[dict], template_html: str, docs_root: Path,
) -> list[str]:
    """Emit `docs_root/brief/<id>/index.html` for each daily-slugged brief.

    Filter rationale: the X-post path is daily-only (`src/post_x.py:_DAILY_ID_RE`)
    and the per-brief OG card feature exists to support those tweets. Weekly
    legacy ids (e.g., `2026-W18`) and any future non-daily slug are intentionally
    skipped — emitting public OG-card pages for them would create promotable
    artifacts for content outside the tweet workflow's scope. The SPA's wouter
    route still resolves `/brief/<weekly-id>` client-side via the 404.html
    fallback; we just don't generate a static crawler-targeted shell for them.

    Returns the ordered list of brief ids actually emitted (skipped ids
    are not included). Pure with respect to its inputs; the only side
    effect is the per-id file write.
    """
    emitted: list[str] = []
    for brief in briefs:
        brief_id = brief.get("id", "")
        if not DAILY_SLUG.match(brief_id):
            continue
        rendered = _render_per_brief_html(template_html, brief)
        out_path = docs_root / "brief" / brief_id / "index.html"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered)
        emitted.append(brief_id)
    return emitted


# =============================================================================
# Per-date helpers
# =============================================================================

def _draft_path(d: date) -> Path:
    return DIGESTS_DIR / d.isoformat() / "summary.json"


def _load_draft(d: date) -> Brief:
    return Brief(**json.loads(_draft_path(d).read_text()))


def _flip_draft_to_published(d: date, brief_payload: dict) -> None:
    """Re-write summary.json with status=published. Atomic, idempotent."""
    _atomic_write_text(
        _draft_path(d),
        json.dumps(brief_payload, indent=2) + "\n",
    )


def _format_commit_message(brief_ids: list[str]) -> str:
    if len(brief_ids) == 1:
        title = f"chore(publish): {brief_ids[0]}"
    else:
        title = f"chore(publish): {brief_ids[0]} (+{len(brief_ids) - 1} catchup)"
    body = "\n".join(f"- {i}" for i in brief_ids)
    return f"{title}\n\n{body}\n"


# =============================================================================
# Orchestrator
# =============================================================================

def run_daily_publish(
    max_backlog: int = DEFAULT_MAX_BACKLOG,
    dry_run: bool = False,
    *,
    now_utc: datetime | None = None,
) -> int:
    """Main orchestrator. Caller (CLI) must hold the lock.

    Returns process exit code: 0 on success or expected no-op,
    non-zero on hard failure (raises propagate up to main()).

    `now_utc` is for tests only — pin a deterministic moment to exercise
    the editorial-time guard. Production callers omit it.
    """
    # Pinning `now_utc` (test only) freezes the entire run's clock.
    # In production (`now_utc is None`), each `_now()` call reads the
    # wall-clock fresh — so a slow reconciliation/pre-flight push that
    # crosses the 05:00 local boundary is observed at the per-date
    # decision point, not lost to a stale snapshot.
    def _now() -> datetime:
        return now_utc if now_utc is not None else _now_utc()

    started = _now()
    # Editorial date is anchored to America/New_York, NOT UTC, and is
    # captured ONCE here purely to scope `discover_work`'s candidate set.
    # The publish-window eligibility itself is re-evaluated per-iteration
    # below via `is_daily_window_open_for(d, _now())`. See
    # plans/incident-2026-04-29-runatload-utc.md.
    today, _ = daily_editorial_state(started)

    # Load briefs.json once at the top — used for reconciliation and
    # discovery. The pre-flight push doesn't modify briefs.json content,
    # so no reload is needed.
    briefs = _load_briefs()

    # --- Dry-run path (READ-ONLY) -------------------------------------------
    # Reports what reconciliation, pre-flight push, and discovery WOULD do
    # without mutating local audit state, the remote, or briefs.json.
    # Malformed-draft corruption still raises (hard fail regardless).
    if dry_run:
        print(f"--- dry-run report (today={today.isoformat()}, max_backlog={max_backlog}) ---")
        _reconcile_finalization(briefs, dry_run=True)
        # Dry-run uses the simpler ahead-only `_commits_ahead` since
        # dry-run is read-only by contract (no fetch, no state
        # mutation). The production path (non-dry-run) calls
        # reconcile_with_origin which performs full ahead/behind/
        # divergence reconciliation. The vocabularies intentionally
        # diverge — dry-run is operator-visibility only and never
        # mutates state, so a fetching reconcile would violate the
        # dry-run contract.
        ahead = _commits_ahead()
        if ahead > 0:
            print(f"pre-flight (dry-run, ahead-only): {ahead} commit(s) ahead of cached origin/main; would attempt push")
        else:
            print("pre-flight (dry-run, ahead-only): clean (0 commits ahead of cached origin/main)")
        published_ids = {b.get("id") for b in briefs if b.get("id")}
        candidates = discover_work(today, max_backlog, START_FLOOR, published_ids)
        if not candidates:
            print(f"discovery (dry-run): no candidates (already published through {today})")
            return 0
        print(f"discovery (dry-run): candidates ({len(candidates)}): {[d.isoformat() for d in candidates]}")
        for d in candidates:
            if not is_daily_window_open_for(d, _now()):
                print(
                    f"  {d}: would skip (editorial window not open; need >= "
                    f"{DAILY_WINDOW_HOUR:02d}:00 America/New_York)"
                )
                continue
            if _draft_path(d).exists():
                print(f"  {d}: would orphan-promote existing draft")
            elif d == today:
                print(f"  {d}: would fetch live + synthesize (today)")
            else:
                print(f"  {d}: would skip (no draft; live-API cannot backfill)")
        return 0

    # --- Real (mutating) flow -----------------------------------------------

    # Reconcile finalization gaps from any prior run BEFORE pre-flight
    # push or new synthesis. If a prior run committed but skipped the
    # post-commit flip + run-record steps (working-tree-check raised,
    # power loss between commit and flip, etc.), this self-heals the
    # local audit state. Idempotent on already-reconciled input.
    _reconcile_finalization(briefs)

    # Pre-flight reconciliation — fetch origin/main, fast-forward if
    # behind, push if ahead, rebase if diverged with bot-owned commits
    # only, halt otherwise. Closes the 2026-05-02 race where the prior
    # day's x-post sidecar advanced origin and the next morning's daily
    # committed on stale HEAD; see src/git_sync.py.
    recon = reconcile_with_origin()
    if not recon.is_ok:
        _write_run_state({
            "phase": "pre-flight-halt",
            "ts": started.isoformat(),
            "today": today.isoformat(),
            "commit": _head_sha(),
            "ahead": recon.ahead,
            "behind": recon.behind,
            "reconcile_action": recon.action,
            "reconcile_detail": recon.detail,
        })
        print(
            f"pre-flight reconcile HALT ({recon.action}: {recon.detail}); "
            f"ahead={recon.ahead} behind={recon.behind}. Halting before "
            "daily flow. Resolve manually, then next run proceeds normally."
        )
        return 0
    if recon.action != "noop":
        print(
            f"pre-flight reconcile ok ({recon.action}); ahead={recon.ahead} "
            f"behind={recon.behind}. Continuing into daily flow."
        )

    # Discover work. `briefs` was already loaded at function entry.
    published_ids = {b.get("id") for b in briefs if b.get("id")}
    candidates = discover_work(today, max_backlog, START_FLOOR, published_ids)

    if not candidates:
        print(f"no candidate dates (already published through {today})")
        _write_run_state({
            "phase": "complete",
            "ts": started.isoformat(),
            "today": today.isoformat(),
            "commit": _head_sha(),
            "push": "ok",
            "published": [],
        })
        return 0

    print(f"candidates ({len(candidates)}): {[d.isoformat() for d in candidates]}")

    # Snapshot the deployed-state briefs for revert if the commit pipeline
    # fails. "Deployed state" = whatever briefs.json currently holds, which
    # by the contract reflects the last successful commit (only the orchestrator
    # writes briefs.json, and it only does so when commit pipeline succeeds —
    # see the revert path below).
    deployed_briefs = list(briefs)

    # Per-date discovery loop. Ascending order = chronological audit trail.
    # NOTHING is written to disk for the "publish" event yet — briefs.json
    # write, draft-status flip, and publish run records are all deferred
    # until commit pipeline succeeds. This refuses to strand entries between
    # local "published" file state and the actual deployed state.
    live_fetch_invocations = 0
    proposed_per_date: list[tuple[date, dict, bool]] = []

    for d in candidates:
        # Editorial-time guard: refuse to create OR publish a brief whose
        # id is today's local date until the window has opened (05:00
        # America/New_York). Re-evaluated per-iteration via `_now()` so a
        # slow reconciliation / pre-flight push that crosses the 05:00
        # boundary picks up the now-eligible state instead of carrying a
        # stale `started`-time snapshot. Past dates pass through —
        # orphan-promotion of older drafts is always a valid recovery
        # path. See plans/incident-2026-04-29-runatload-utc.md.
        if not is_daily_window_open_for(d, _now()):
            print(
                f"  {d}: editorial window not open (need >= "
                f"{DAILY_WINDOW_HOUR:02d}:00 America/New_York); skipping"
            )
            continue

        # Snapshot draft existence BEFORE potentially calling run_daily
        # (which writes a new summary.json). The branch-tracked flag below
        # is the source of truth for the per-record annotation.
        draft_pre_existing = _draft_path(d).exists()

        if draft_pre_existing:
            # Orphan-draft promotion — no fetch. Works for any date
            # including today (recovery path: synthesis succeeded earlier
            # but merge/build/commit failed before completing).
            brief = _load_draft(d)
            used_live_fetch = False
            print(f"  {d}: orphan-draft promotion (deferred until commit)")
        elif d == today:
            # Only path that performs a live fetch.
            live_fetch_invocations += 1
            if live_fetch_invocations > 1:
                # Guarded by structure (only d==today reaches here, and
                # today appears at most once in candidates), but belt-
                # and-suspenders for any future refactor.
                raise RuntimeError(
                    "invariant violation: live_fetch_invocations > 1 in one run"
                )
            summary_path = run_daily(d.isoformat(), source="live-api")
            if summary_path is None:
                print(f"  {d}: empty filter, no draft produced; skipping")
                continue
            brief = _load_draft(d)
            used_live_fetch = True
        else:
            # HC-2: live-API cannot backfill past dates without on-disk drafts.
            print(f"  {d}: no draft, skipping (live-API cannot backfill)")
            continue

        new_payload = json.loads(brief.model_dump_json())
        new_payload["status"] = "published"
        briefs = merge_brief(briefs, new_payload)
        proposed_per_date.append((d, new_payload, used_live_fetch))

    # Invariant assertion at run boundary.
    assert live_fetch_invocations <= 1, (
        f"live_fetch_invocations={live_fetch_invocations} violates invariant"
    )

    if not proposed_per_date:
        print("no new entries this run; clean exit")
        _write_run_state({
            "phase": "complete",
            "ts": started.isoformat(),
            "today": today.isoformat(),
            "commit": _head_sha(),
            "push": "ok",
            "published": [],
            "live_fetch_invocations": live_fetch_invocations,
        })
        return 0

    # Write briefs.json so Vite picks it up at build time. From here through
    # the successful commit, anything raising MUST revert briefs.json (and
    # docs/) so the next run's discovery doesn't see a "published" entry
    # that isn't actually deployed.
    _atomic_write_text(BRIEFS_PATH, json.dumps(briefs, indent=2) + "\n")

    published_this_run = [p[1]["id"] for p in proposed_per_date]
    try:
        build_started_ts = datetime.now(timezone.utc).timestamp()
        _run_build(build_started_ts, briefs)

        _git("add", "data/briefs.json", "docs/")
        commit_msg = _format_commit_message(published_this_run)
        _git("commit", "-m", commit_msg)
    except Exception:
        # Revert to the deployed-state snapshot so the next run sees a
        # consistent "what's actually deployed" view. Three steps in order:
        #
        #   (1) Unstage. If `git add` ran but `git commit` failed (e.g., a
        #       pre-commit hook rejected the commit), the INDEX still holds
        #       the staged briefs.json + docs/ changes — leaving them
        #       staged would mean the next manual `git commit` would deploy
        #       the failed-state content. Reset clears the index for these
        #       paths back to HEAD.
        #   (2) Restore briefs.json working tree to the deployed snapshot.
        #   (3) Restore tracked docs/ working tree to HEAD. Any untracked
        #       cruft from the partial build remains but is wiped by the
        #       next successful build (Vite emptyOutDir).
        #
        # Drafts on disk were never flipped this run — nothing else to undo.
        # `run_daily`'s side effects (today's summary.json draft, posts_raw
        # rows, daily run record) are durable; the next run reuses them via
        # orphan-promotion, so the LLM call isn't repeated.
        print(
            "build/commit pipeline failed; reverting index + briefs.json + "
            "docs/ to last-deployed state to avoid stranding entries before deploy"
        )
        try:
            _git("reset", "HEAD", "--", "data/briefs.json", "docs/")
        except Exception:
            pass  # nothing-to-reset is also a 0 exit; tolerate either
        _atomic_write_text(BRIEFS_PATH, json.dumps(deployed_briefs, indent=2) + "\n")
        try:
            _git("checkout", "--", "docs/")
        except Exception:
            pass  # best-effort; next successful build replaces docs/ regardless
        raise

    # Commit landed. Post-commit invariant check: working tree must be
    # clean. This runs OUTSIDE the try/except so a failing check does NOT
    # trigger the revert path (the commit is already in HEAD; reverting
    # briefs.json to the pre-commit snapshot would diverge it from HEAD's
    # content, leaving the working tree inconsistent rather than restoring
    # cleanliness). If this fails, the run hard-aborts; the commit is
    # local-only until operator cleanup, and next run's pre-flight push
    # will retry the deploy.
    if not _working_tree_clean():
        diag = _git("status", "--porcelain").stdout
        raise RuntimeError(
            f"working tree dirty after commit {_head_sha()[:7]} — "
            f"investigate before next run. Commit IS in HEAD; push "
            f"deferred until cleanup. git status:\n{diag}"
        )

    # Now finalize durable post-commit side effects: flip on-disk draft
    # statuses + append publish run records. Drafts are gitignored so
    # they're not part of the commit; they're a local-state mirror of
    # the publish event. Run records are gitignored too (data/runs.jsonl).
    for d, new_payload, used_live_fetch in proposed_per_date:
        _flip_draft_to_published(d, new_payload)
        append_run_record({
            "run_id": f"publish-{d.isoformat()}-{started.strftime('%H%M%SZ')}",
            "action": "publish",
            "id": new_payload["id"],
            "date": d.isoformat(),
            "ts": datetime.now(timezone.utc).isoformat(),
            "live_fetch_invocations": 1 if used_live_fetch else 0,
        })

    commit_sha = _head_sha()
    ok, push_status = _try_push()

    _write_run_state({
        "phase": "complete",
        "ts": started.isoformat(),
        "today": today.isoformat(),
        "commit": commit_sha,
        "push": push_status,
        "published": published_this_run,
        "live_fetch_invocations": live_fetch_invocations,
    })

    if ok:
        print(f"commit ok ({commit_sha[:7]}); push ok; published {published_this_run}")
    else:
        print(
            f"commit ok ({commit_sha[:7]}); push deferred ({push_status}); "
            f"next run will retry push before doing new work"
        )
    return 0


# =============================================================================
# CLI
# =============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(prog="src.publish")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_daily = sub.add_parser(
        "daily-publish",
        help="run daily auto-publish: catch-up scan, fetch today, build, commit, push",
    )
    p_daily.add_argument(
        "--max-backlog",
        type=int,
        default=DEFAULT_MAX_BACKLOG,
        help="how many days back to scan for orphan drafts (default: 3)",
    )
    p_daily.add_argument(
        "--dry-run",
        action="store_true",
        help="discover and report; do not fetch, publish, build, or commit",
    )

    args = parser.parse_args()
    if args.cmd != "daily-publish":
        return 1

    # Dry-run is purely read-only: no lock acquisition (which would also
    # create data/.run.lock as a side effect) and no exclusion against a
    # real parallel run. Atomic writes elsewhere (briefs.json, summary.json,
    # runs.jsonl append) guarantee dry-run readers see consistent state
    # even mid-real-run.
    if args.dry_run:
        return run_daily_publish(
            max_backlog=args.max_backlog,
            dry_run=True,
        )

    try:
        with acquire_lock():
            return run_daily_publish(
                max_backlog=args.max_backlog,
                dry_run=False,
            )
    except BlockingIOError:
        print("another run-daily-publish in progress; exiting 0")
        return 0


if __name__ == "__main__":
    sys.exit(main())
