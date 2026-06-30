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
# DRY_RUN=1 invocation runs the cheap guards + env setup + computes the
# next episode id, then exits without invoking the orchestrator. Used to
# verify wiring under launchd without spending Anthropic / ElevenLabs
# / Hedra credits. (The Hedra credit pre-flight is skipped under DRY_RUN
# — it makes a live billing call and can post a skip alert, neither of
# which belongs in a wiring check.)
#
# Auth/keys: never set in env. Python loads keys lazily from
# ~/.openclaw/keys/ + repo-local .keys inside the orchestrator. The
# lock at data/.podcast.run.lock and per-episode manifest are managed
# by Python; this wrapper does not touch them.

set -euo pipefail

# ─── Operational knobs ─────────────────────────────────────────────────────
# Minimum days between consecutive episode publishes. Cadence guard
# REFUSES if `data/episodes.json`'s most-recent entry is younger than
# this. MIN_DAYS=13 refuses the first Sunday after a publish at
# days_since=7 and allows the next weekly Sunday fire at days_since=14.
# Net effect: every-other-Sunday emergent publish cadence given a
# weekly launchd fire. Operator escape hatch: set FORCE=1 to bypass.
MIN_DAYS=13
# ───────────────────────────────────────────────────────────────────────────

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Failure/skip notifier (defines notify_discord; posts to #observatory via
# the shared webhook). Sourced early so the EXIT trap below can use it.
# `bash -n` syntax-check before sourcing + a no-op fallback: a missing OR
# corrupt helper must never break the wrapper or turn a clean skip into a
# "command not found" failure. NB: `. file || true` does NOT catch a syntax
# error in the sourced file — under set -e the shell still aborts (verified
# on bash 3.2), killing the wrapper before the EXIT trap is even installed.
# The bash -n precheck is what actually prevents that.
if [ -f "$REPO_ROOT/scripts/notify_discord.sh" ] \
   && bash -n "$REPO_ROOT/scripts/notify_discord.sh" 2>/dev/null; then
    # shellcheck source=scripts/notify_discord.sh
    . "$REPO_ROOT/scripts/notify_discord.sh"
fi
if ! command -v notify_discord >/dev/null 2>&1; then
    notify_discord() { return 0; }
fi

# launchd's default PATH is /usr/bin:/bin:/usr/sbin:/sbin and excludes
# Homebrew (/opt/homebrew/bin on Apple Silicon, /usr/local/bin on Intel).
# The orchestrator's media stage shells out to ffmpeg + ffprobe via
# subprocess; without these on PATH every segment's canary validation
# fails with FileNotFoundError. The 2026-05-10 rerun caught this latent
# bug after the TLS + window fixes unblocked the media stage.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

LOG_DIR="$REPO_ROOT/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/podcast-weekly.log"

# Failure alert. Every *intentional* exit in this wrapper is exit 0
# (branch guard, Phase-3 guard, reconcile push/halt, cadence/balance
# refuse, dry-run, success), so a non-zero exit is always a real failure
# — the 2026-06-28 Hedra 402 died here with no signal. Fire one
# #observatory alert on any non-zero exit. Best-effort: the alert never
# alters the exit status (see scripts/notify_discord.sh).
_on_exit() {
    local rc=$?
    if [ "$rc" -ne 0 ]; then
        echo "  [notify] run failed (exit ${rc}) — alerting #observatory"
        notify_discord "🔴 Podcast pipeline FAILED (exit ${rc}) on $(hostname -s 2>/dev/null || echo host), episode ${NEXT_ID:-unknown}. See logs/podcast-weekly.log (or .launchd.err for very early failures) on the mini." || true
    fi
    exit "$rc"
}
trap _on_exit EXIT

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

# Combined CA bundle = certifi public roots + mitm CA (when present).
# Exporting only the mitm CA breaks NO_PROXY-bypassed services (their
# real public-CA-signed certs cannot be validated against a 1-CA file);
# exporting only certifi breaks mitm-routed traffic. The helper
# materializes both into one PEM and prints its path. See
# `src/ca_bundle.py` for rationale.
COMBINED_CA="$("$REPO_ROOT/.venv/bin/python" -m src.ca_bundle)"
export SSL_CERT_FILE="$COMBINED_CA"
export REQUESTS_CA_BUNDLE="$COMBINED_CA"

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
# Two layered checks (see src/podcast_cadence_guard.py):
#   1. days-since: refuse if today_local - latest.date < MIN_DAYS.
#   2. weekly-window: refuse if local time is not Sunday at-or-after
#      WEEKLY_WINDOW_HOUR ET, or if the open window is already filled.
# Malformed episodes.json or bad date → Python raises → bash sees no
# PROCEED|REFUSE prefix on stdout and exits 1 via the wildcard case.
CADENCE_RESULT=$("$REPO_ROOT/.venv/bin/python" -m src.podcast_cadence_guard \
    --min-days "$MIN_DAYS")

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

# ── Spend pre-flight (Layer 2) ──────────────────────────────────────────────
# Refuse a run that can't afford to finish, BEFORE any Anthropic / ElevenLabs
# / Hedra spend. Covers the two metered services that expose a balance
# endpoint; both the 2026-06-28 and 2026-05-17 (ep-003) runs died with
# partial Hedra credit mid-render, wasting all prior script-gen + TTS + clips.
# Anthropic and YouTube have no pre-flight balance endpoint — they are covered
# by the EXIT-trap failure alert (Layer 1).
#
# Per-service ADVISORY + FAIL-OPEN: a guard-side error (network/parse/
# unexpected) proceeds with a warning — a balance-check hiccup must never
# block a legitimate run; the orchestrator's own error plus the EXIT-trap
# alert are the backstop. FORCE=1 / DRY_RUN=1 skip the whole pre-flight.
# Floors are env-tunable:
#   HEDRA_MIN_CREDITS    default 1800 ≈ one 720p episode (~1,500 credits per
#                        plans/podcast-pipeline.md:214) + ~20% margin.
#   ELEVENLABS_MIN_CHARS default 5000 ≈ ~1.5 episodes of narration.
HEDRA_MIN_CREDITS="${HEDRA_MIN_CREDITS:-1800}"
ELEVENLABS_MIN_CHARS="${ELEVENLABS_MIN_CHARS:-5000}"

# run_spend_guard <module> <floor> <human-label> <units>
# Runs a standalone balance guard whose stdout contract is one of:
#   PROCEED:<unit>:<remaining>:<min> | REFUSE:<unit>:<remaining>:<min>
#   | PROCEED:error:<Name>
# REFUSE → clean exit 0 + an #observatory skip notice (no spend, no publish).
# PROCEED:error / unexpected → fail-open (log + proceed).
run_spend_guard() {
    local module="$1" floor="$2" label="$3" units="$4"
    local result remaining
    result=$("$REPO_ROOT/.venv/bin/python" -m "$module" --min "$floor") || true
    case "$result" in
        REFUSE:*)
            remaining=$(printf '%s' "$result" | cut -d: -f3 || true)
            echo "  spend pre-flight [$label]: $result — insufficient; no spend, no publish."
            notify_discord "⚠️ Podcast skipped on $(hostname -s 2>/dev/null || echo host): ${label} low (${remaining} ${units} < floor ${floor}). Fund to resume; no episode this run." || true
            echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) run-weekly-podcast.sh finished (spend pre-flight: $label)"
            exit 0
            ;;
        PROCEED:error:*)
            echo "  spend pre-flight [$label]: $result — fail-open, proceeding."
            ;;
        PROCEED:*)
            echo "  spend pre-flight [$label]: $result"
            ;;
        *)
            echo "  spend pre-flight [$label]: unexpected ($result) — fail-open, proceeding."
            ;;
    esac
}

if [ "${DRY_RUN:-}" = "1" ]; then
    echo "  spend pre-flight: DRY_RUN=1 — skipped (no balance calls, no alerts)."
elif [ "${FORCE:-}" = "1" ]; then
    echo "  spend pre-flight: FORCE=1 — bypassed (operator override)."
else
    run_spend_guard src.podcast_hedra_balance_guard "$HEDRA_MIN_CREDITS" "Hedra" credits
    run_spend_guard src.podcast_elevenlabs_balance_guard "$ELEVENLABS_MIN_CHARS" "ElevenLabs" characters
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
#
# docs/ (not just docs/podcast) goes in the commit: cmd_run's Phase 7
# rebuilt the SPA, so docs/index.html, docs/assets/*, docs/brief/*,
# docs/404.html, and docs/podcast/* all need to land together. The
# in-process verification gate (rebuild_spa_and_verify) has already
# confirmed the bundle contains this episode's id + youtubeId; if it
# didn't, cmd_run raised and `set -e` killed the wrapper before this.
git add data/episodes.json docs/
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
