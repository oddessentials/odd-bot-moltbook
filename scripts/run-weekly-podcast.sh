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
