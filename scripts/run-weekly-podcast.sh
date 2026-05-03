#!/usr/bin/env bash
# scripts/run-weekly-podcast.sh — single entry point for weekly podcast publish.
#
# Invoked by launchd via com.oddbot.moltbook.podcast.weekly.plist on a
# weekly cadence (+ RunAtLoad for boot recovery). Local + automation
# parity — operator's manual command is exactly this script.
#
# Mirrors scripts/run-daily-publish.sh structurally with three
# podcast-specific concerns:
#
#   1. Branch guard — refuse if HEAD is not on main. Same rationale as
#      the daily wrapper: HEAD on a feature branch would commit to the
#      wrong branch and silently leave main stale.
#
#   2. Phase 3 downstream guard — refuse if the podcast X-post workflow
#      or sidecar files are missing. The cadence is half-wired without
#      a downstream social path; refusing here prevents the engine from
#      generating public episodes that nobody knows about.
#
#   3. Proxy bypass — keep mitmproxy on for Anthropic + Moltbook
#      observability (matches the daily wrapper) but exempt the new
#      external services (Hedra, ElevenLabs, YouTube/Google APIs +
#      Hedra's S3-presigned download URLs) from interception. Their
#      auth flows are strict-cert-validated and mitmproxy's TLS
#      substitution would 401 them.
#
# DRY_RUN=1 invocation runs all guards + env setup + computes the next
# episode id, then exits without invoking the orchestrator. Used to
# verify wiring under launchd without spending Anthropic / ElevenLabs
# / Hedra credits.
#
# Auth/keys: never set in env. Python loads keys lazily from
# ~/.openclaw/keys/ + repo-local .keys inside the orchestrator. The
# lock at data/.podcast.run.lock and per-episode manifest are managed
# by Python; this wrapper does not touch them.

set -euo pipefail

# ─── Operational knobs ─────────────────────────────────────────────────────
# Minimum days between consecutive episode publishes. Cadence guard
# REFUSES if `data/episodes.json`'s most-recent entry is younger than
# this. 6 (not 7) tolerates clock drift across week boundaries: last
# Sunday published at 12:00, this Sunday cron fires at 09:00 → calendar
# diff = 6 → PROCEED. Tuesday reboot after Sunday publish → diff = 2
# → REFUSE. Operator escape hatch: set FORCE=1 to bypass.
MIN_DAYS=6
# ───────────────────────────────────────────────────────────────────────────

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

LOG_DIR="$REPO_ROOT/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/podcast-weekly.log"

CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "DETACHED")"
if [ "$CURRENT_BRANCH" != "main" ]; then
    {
        echo "----"
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) run-weekly-podcast.sh refused"
        echo "  current branch: $CURRENT_BRANCH"
        echo "  required branch: main"
        echo "  no publish. RunAtLoad will retry on next launchd reload / reboot once HEAD is on main."
    } >>"$LOG_FILE"
    exit 0
fi

PHASE3_FILES=(
    ".github/workflows/podcast-x-post.yml"
    "src/post_podcast_x.py"
    "data/podcast-x-posts.jsonl"
)
for f in "${PHASE3_FILES[@]}"; do
    if [ ! -e "$f" ]; then
        {
            echo "----"
            echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) run-weekly-podcast.sh refused"
            echo "  missing Phase 3 file: $f"
            echo "  refusing to generate a public episode without a downstream social path."
            echo "  fix: ensure the podcast x-post workflow + sidecar are committed on main."
        } >>"$LOG_FILE"
        exit 0
    fi
done

# launchd's default PATH excludes nvm-managed binaries. The orchestrator
# never spawns pnpm directly (the SPA build is on the daily side), but
# parity with the daily wrapper keeps both runtimes identical so future
# refactors can't accidentally diverge.
export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
if [ -s "$NVM_DIR/nvm.sh" ]; then
    # shellcheck disable=SC1091
    . "$NVM_DIR/nvm.sh" >/dev/null 2>&1
fi

# Route Anthropic + Moltbook traffic through the odd-ai-observatory
# mitmproxy so weekly script-gen is visible in observability. NO_PROXY
# excludes:
#   - github.com (mirrors the daily; needed for `git push`)
#   - api.hedra.com / *.hedra.com — Hedra REST API
#   - api.elevenlabs.io / *.elevenlabs.io — ElevenLabs TTS
#   - googleapis.com / *.googleapis.com — YouTube Data + OAuth2
#   - *.amazonaws.com — Hedra serves generated MP4s via S3 presigned URLs
# These services use strict cert validation and would fail under
# mitmproxy's TLS substitution. Trade-off: their network calls aren't
# captured in mitmproxy logs; observability falls back to the
# orchestrator's stdout.
export HTTPS_PROXY="http://127.0.0.1:8080"
export HTTP_PROXY="http://127.0.0.1:8080"
export NO_PROXY="github.com,api.github.com,*.github.com,api.hedra.com,*.hedra.com,api.elevenlabs.io,*.elevenlabs.io,googleapis.com,*.googleapis.com,*.amazonaws.com"

MITM_CA="$HOME/.mitmproxy/mitmproxy-ca-cert.pem"
if [ -f "$MITM_CA" ]; then
    export SSL_CERT_FILE="$MITM_CA"
    export REQUESTS_CA_BUNDLE="$MITM_CA"
fi

exec >>"$LOG_FILE" 2>&1

echo "----"
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) run-weekly-podcast.sh start"

# Pre-flight reconciliation. Fetches origin/main, fast-forwards if
# behind (the common case after a prior x-post sidecar landed),
# pushes if ahead (recovery from a prior failed push), rebases if
# diverged with bot-owned commits only, halts on dirty worktree or
# any non-bot-owned divergence. Closes the 2026-05-02 race where the
# prior day's x-post sidecar advanced origin and the next morning's
# daily committed on stale local HEAD; see src/git_sync.py.
#
# Recovery posture is unchanged: if reconcile resulted in a push
# (action=push or action=rebase), exit WITHOUT generating new content
# — the downstream podcast-x-post fires on the now-pushed commit, and
# the next scheduled cadence (or RunAtLoad invocation) picks up
# steady-state. action=fast-forward and action=noop continue into the
# cadence guard.
RECON_OUT=$("$REPO_ROOT/.venv/bin/python" -m src.git_sync reconcile 2>&1) || RECON_RC=$?
RECON_RC="${RECON_RC:-0}"
echo "$RECON_OUT" | sed 's/^/  /'
RECON_STATUS=$(echo "$RECON_OUT" | grep '^STATUS:' | tail -1)
case "$RECON_STATUS" in
    STATUS:ok:noop*|STATUS:ok:fast-forward*)
        # Local already matches origin (or just fast-forwarded). Safe
        # to continue into the cadence guard and content generation.
        :
        ;;
    STATUS:ok:push*|STATUS:ok:rebase*)
        # We pushed local bot-owned commits to origin. The downstream
        # podcast-x-post workflow fires on that push; this run's job
        # was reconciliation only. Generating a new episode on top of
        # a just-pushed prior episode would burn credits before the
        # downstream sidecar settles and before the operator has a
        # chance to verify origin — so we exit here, NOT continue.
        echo "  reconciled prior bot work and pushed to origin."
        echo "  EXITING THIS RUN — no script-gen / TTS / Hedra / YouTube spend."
        echo "  this run's job was reconciliation only; new content generation"
        echo "  resumes on the next scheduled Sunday fire (or a manual rerun)."
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) run-weekly-podcast.sh finished (pre-flight reconciled prior work; no generation)"
        exit 0
        ;;
    STATUS:halt:*)
        echo "  refusing to generate new content while pre-flight reconcile is halted."
        echo "  Operator: diagnose ($RECON_STATUS) and re-run; halts are intentionally"
        echo "  conservative — no Anthropic/ElevenLabs/Hedra spend until resolved."
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) run-weekly-podcast.sh finished (pre-flight reconcile halt)"
        exit 0
        ;;
    *)
        echo "  unexpected reconcile output (rc=$RECON_RC); refusing to proceed."
        exit 1
        ;;
esac

# Cadence guard. After pre-flight (which exits the wrapper if it had to
# push), this is the cheap-Python check that protects against
# RunAtLoad-induced over-firing on Mac mini reboots between Sundays.
# Reads the most-recent entry by episodeNo from data/episodes.json,
# compares its `date` to today. PROCEED if >= MIN_DAYS old or no
# prior episode; REFUSE otherwise. Malformed episodes.json or bad
# date → Python raises → bash exits 1 (operator investigates).
CADENCE_RESULT=$(MIN_DAYS="$MIN_DAYS" "$REPO_ROOT/.venv/bin/python" -c '
import json, os, sys
from datetime import date, datetime, timezone
from pathlib import Path

from src.editorial_time import weekly_window_satisfied

min_days = int(os.environ["MIN_DAYS"])
ep_path = Path("data/episodes.json")
if not ep_path.exists():
    print("PROCEED:no_episodes_json"); sys.exit(0)
entries = json.loads(ep_path.read_text())
if not entries:
    print("PROCEED:empty_episodes"); sys.exit(0)
latest = max(entries, key=lambda e: e.get("episodeNo", 0))
# Missing / empty / malformed date is a corruption signal, not a
# "permissively proceed" case — letting the guard PROCEED on a bad
# entry would re-introduce the over-fire hazard the guard exists to
# prevent. date.fromisoformat raises on None / "" / unparseable
# strings; bash sees no PROCEED|REFUSE prefix on stdout and exits 1
# via the wildcard case below.
latest_date_str = latest.get("date")
latest_date = date.fromisoformat(latest_date_str)
days_since = (date.today() - latest_date).days
latest_id = latest.get("id") or "unknown"

# (1) Operator-tunable days-since gate. Cheap floor against rapid
#     manual reinvocation.
if days_since < min_days:
    print(f"REFUSE:cadence:{days_since}:{latest_id}:{latest_date_str}")
    sys.exit(0)

# (2) Editorial-window gate. The weekly publish window opens at
#     Sunday 09:00 America/New_York; refuse any fire whose most-recent
#     window has already been filled by the latest publish. Anchored
#     to local clock time, not UTC, so a Mac mini reboot crossing the
#     UTC date boundary cannot pre-fire the next window. See
#     plans/incident-2026-04-29-runatload-utc.md.
if weekly_window_satisfied(datetime.now(timezone.utc), latest_date):
    print(f"REFUSE:window:{days_since}:{latest_id}:{latest_date_str}")
    sys.exit(0)

print(f"PROCEED:{days_since}:{latest_id}:{latest_date_str}")
')

if [ "${FORCE:-}" = "1" ]; then
    echo "  cadence guard: FORCE=1 — bypassing ($CADENCE_RESULT)"
else
    case "$CADENCE_RESULT" in
        PROCEED:*)
            echo "  cadence guard: $CADENCE_RESULT"
            ;;
        REFUSE:*)
            echo "  cadence guard: $CADENCE_RESULT"
            echo "  no spend, no publish. Next eligible fire is the next Sunday 09:00"
            echo "  America/New_York at-or-after MIN_DAYS=$MIN_DAYS days from the latest publish."
            echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) run-weekly-podcast.sh finished (cadence guard)"
            exit 0
            ;;
        *)
            echo "  cadence guard: unexpected output ($CADENCE_RESULT) — likely malformed"
            echo "  data/episodes.json. Refusing to proceed; operator: investigate."
            exit 1
            ;;
    esac
fi

NEXT_NO=$("$REPO_ROOT/.venv/bin/python" -c \
    'from src.podcast.manifest import derive_episode_no; print(derive_episode_no())')
NEXT_ID="ep-$(printf '%03d' "$NEXT_NO")"
echo "  next episode_id: $NEXT_ID"

if [ "${DRY_RUN:-}" = "1" ]; then
    echo "  DRY_RUN=1 — guards + env setup OK; skipping orchestrator."
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) run-weekly-podcast.sh finished (dry-run)"
    exit 0
fi

"$REPO_ROOT/.venv/bin/python" -m src.podcast run --episode-id "$NEXT_ID"

# Commit + push the public-surface changes so Phase 3's podcast-x-post
# workflow fires on the push to main. Without this step the cron
# produces a fully-published episode (data/episodes.json populated,
# YouTube unlisted, OG page on disk locally) but main never advances,
# the path-filtered workflow never triggers, and the new episode is
# never tweeted. cmd_run is idempotent on rerun, so nothing to commit
# means nothing to push — the `git diff --cached --quiet` gate makes
# this a no-op when there's no new content.
git add data/episodes.json docs/podcast
if git diff --cached --quiet; then
    echo "  no public-surface changes — nothing to commit, nothing to push."
else
    git -c user.email="odd-bot@oddessentials.ai" \
        -c user.name="odd-bot" \
        commit -m "chore(podcast): publish $NEXT_ID"
    if git push origin main; then
        echo "  pushed; podcast-x-post.yml will fire on the data/episodes.json change."
    else
        echo "  WARN: git push failed — public surface is committed locally but"
        echo "        not on origin. podcast-x-post.yml will NOT fire until the"
        echo "        commit reaches origin/main. Operator: investigate and"
        echo "        rerun this wrapper to re-attempt the push (the orchestrator's"
        echo "        own idempotency means no Anthropic/ElevenLabs/Hedra cost on retry)."
        exit 1
    fi
fi

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) run-weekly-podcast.sh finished"
