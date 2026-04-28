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
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from src.summarize import (
    Brief,
    DAILY_SLUG,
    DATA_DIR,
    DIGESTS_DIR,
    REPO_ROOT,
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

# D10-A: hard floor at the cadence-flip date. Prevents the orchestrator
# from ever scanning earlier dates for orphan drafts (e.g., the HF-replay
# 2026-01-30 draft, which is pre-flip provenance and intentionally
# stays as a draft per D5-B).
START_FLOOR = date(2026, 4, 27)


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
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


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

def _run_build(build_started_ts: float) -> None:
    """Invoke `pnpm --dir agent-brief build`. Verify /docs/index.html refreshed.

    Phase 2 ships this orchestration unchanged; Phase 4 wires Vite's
    `build.outDir` to repo-root `/docs/` and adds `agent-brief/public/`
    assets (CNAME, .nojekyll). Until Phase 4 lands, this call will fail
    and the run aborts before commit — which is the correct safety
    posture for a partial migration.
    """
    subprocess.run(
        ["pnpm", "--dir", str(AGENT_BRIEF_DIR), "build"],
        cwd=str(REPO_ROOT),
        check=True,
    )
    if not DOCS_INDEX.exists():
        raise RuntimeError(
            f"build completed but {DOCS_INDEX} is missing — "
            "Vite outDir misconfigured (Phase 4 prerequisite)"
        )
    if DOCS_INDEX.stat().st_mtime <= build_started_ts:
        raise RuntimeError(
            f"build completed but {DOCS_INDEX} mtime not refreshed — "
            "stale artifact suspected"
        )


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
) -> int:
    """Main orchestrator. Caller (CLI) must hold the lock.

    Returns process exit code: 0 on success or expected no-op,
    non-zero on hard failure (raises propagate up to main()).
    """
    started = datetime.now(timezone.utc)
    today = started.date()

    # Pre-flight push — if there are unpushed commits from a prior run,
    # resolve them BEFORE doing any new work. Persistent failure halts
    # the run to avoid stacking new commits behind an unresolved push.
    ahead = _commits_ahead()
    if ahead > 0:
        print(f"pre-flight: {ahead} commit(s) ahead of remote; attempting push")
        ok, push_status = _try_push()
        if not ok:
            _write_run_state({
                "phase": "pre-flight-halt",
                "ts": started.isoformat(),
                "today": today.isoformat(),
                "commit": _head_sha(),
                "commits_ahead": ahead,
                "push": "deferred-blocking-from-prior-run",
                "detail": push_status,
            })
            print(
                f"pre-flight push failed ({push_status}); halting before "
                "daily flow to avoid stacking commits. "
                "Resolve the push manually, then next run proceeds normally."
            )
            return 0
        print("pre-flight push ok; continuing into daily flow")

    # Discover work.
    briefs = _load_briefs()
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

    if dry_run:
        for d in candidates:
            if _draft_path(d).exists():
                print(f"  {d}: would orphan-promote existing draft")
            elif d == today:
                print(f"  {d}: would fetch live + synthesize (today)")
            else:
                print(f"  {d}: would skip (no draft; live-API cannot backfill)")
        return 0

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
        _run_build(build_started_ts)

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

    try:
        with acquire_lock():
            return run_daily_publish(
                max_backlog=args.max_backlog,
                dry_run=args.dry_run,
            )
    except BlockingIOError:
        print("another run-daily-publish in progress; exiting 0")
        return 0


if __name__ == "__main__":
    sys.exit(main())
